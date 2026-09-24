"""Stage 2: resolve each listing row to its order document and archive the PDF.

Reads ``derived/listings.parquet``, follows each order's detail page to find the
real document, downloads it, and writes ``derived/order_documents.parquet``.

Why the detail page must be followed
------------------------------------
SEBI's PDF filenames are opaque epoch-millisecond stamps
(``/sebi_data/attachdocs/1291435555408.pdf``), so the document URL cannot be
constructed from the listing row — it has to be read off the detail page.

Three document shapes exist in this corpus, established by sampling detail
pages across every year from 2000 on:

``IFRAME_PDF``    The common case. The PDF sits in an ``<iframe src>``, usually
                  wrapped in SEBI's viewer (``/web/?file=<real url>``), and the
                  inner URL may be relative and may use ``.PDF`` or ``.pdf``.
                  There is no ``<a href>`` to the PDF anywhere on the page —
                  scraping anchors alone finds nothing.
``INLINE_HTML``   Chiefly older orders: the full order text is in the page
                  itself (``section.main_section``) with no PDF at all. These
                  need neither downloading nor OCR — the text is already clean,
                  so they are carried forward as text, not treated as failures.
``DIRECT_PDF``    The listing already points at a PDF (SAST exemption orders);
                  there is no detail page to follow.

The two stages are separated so the expensive download can be re-run, resumed
and re-parsed independently::

    resolve (detail page -> document URL)  ->  download (document -> raw/pdfs/)

Both go through :mod:`src.cache`, so re-running costs no network. Archived PDFs
are hard-linked from the cache entry rather than copied, which keeps ``raw/``
append-only (runbook rule 3) without storing every document twice.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import logging
import os
import pathlib
import re
import shutil
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field

import pyarrow as pa
import pyarrow.parquet as pq
from bs4 import BeautifulSoup

from src import cache

log = logging.getLogger("fetch_pdfs")

REPO_ROOT = cache.REPO_ROOT
LISTINGS_IN = REPO_ROOT / "derived" / "listings.parquet"
DOCUMENTS_OUT = REPO_ROOT / "derived" / "order_documents.parquet"
FAILURES_OUT = REPO_ROOT / "derived" / "fetch_failures.csv"
PDF_DIR = REPO_ROOT / "raw" / "pdfs"
REPORTS_DIR = REPO_ROOT / "reports"
LOGS_DIR = REPO_ROOT / "logs"

#: Minimum characters in section.main_section before we call a page a real
#: inline order rather than boilerplate. Sampled inline orders run to
#: thousands of characters; chrome-only pages sit in the low hundreds.
INLINE_TEXT_MIN_CHARS = 1500

SEBI_ORIGIN = "https://www.sebi.gov.in"

# Shapes
IFRAME_PDF = "IFRAME_PDF"
ANCHOR_PDF = "ANCHOR_PDF"
DIRECT_PDF = "DIRECT_PDF"
INLINE_HTML = "INLINE_HTML"
NO_DOCUMENT = "NO_DOCUMENT"
DETAIL_FAILED = "DETAIL_FAILED"

# Statuses
OK = "OK"
OK_INLINE = "OK_INLINE"
FAILED_DETAIL = "FAILED_DETAIL"
FAILED_DOWNLOAD = "FAILED_DOWNLOAD"
NOT_A_PDF = "NOT_A_PDF"
NO_DOC = "NO_DOCUMENT"


@dataclass(frozen=True)
class DocumentRef:
    """What a detail page points at. Pure parse result, no network."""

    shape: str
    pdf_url: str | None = None
    inline_text_chars: int = 0


@dataclass
class DocumentRow:
    order_id: int | None
    order_type: str
    order_date: dt.date | None
    title: str
    source_url: str
    listing_doc_kind: str
    doc_shape: str
    pdf_url: str | None
    pdf_path: str | None
    pdf_bytes: int | None
    pdf_sha256: str | None
    inline_text_chars: int
    status: str
    error: str | None
    resolved_at: dt.datetime | None


# --------------------------------------------------------------------------
# Parsing — pure functions, no network
# --------------------------------------------------------------------------

def unwrap_viewer_url(src: str) -> str:
    """``/web/?file=<real url>`` -> ``<real url>``; anything else unchanged."""
    query = urllib.parse.urlparse(src).query
    target = urllib.parse.parse_qs(query).get("file")
    return target[0] if target and target[0] else src


def looks_like_pdf_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    return path.lower().endswith((".pdf", ".pdf/"))


def extract_inline_text(html: str | bytes) -> str:
    """The order body from an inline-HTML order. Empty string when absent."""
    soup = _soup(html)
    body = soup.find("section", class_="main_section")
    if body is None:
        return ""
    for tag in body.find_all(["script", "style"]):
        tag.decompose()
    return re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()


def _soup(html: str | bytes) -> BeautifulSoup:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    return BeautifulSoup(html, "lxml")


def extract_document_ref(html: str | bytes, base_url: str) -> DocumentRef:
    """Find the document a detail page points at. Never raises on bad HTML."""
    soup = _soup(html)

    # 1. iframe/embed/object — the normal case.
    for node in soup.find_all(["iframe", "embed", "object"]):
        src = node.get("src") or node.get("data")
        if not src or not src.strip():
            continue
        real = urllib.parse.urljoin(base_url, unwrap_viewer_url(src.strip()))
        if looks_like_pdf_url(real):
            return DocumentRef(IFRAME_PDF, real)

    # 2. A plain anchor to a PDF.
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href and looks_like_pdf_url(urllib.parse.urljoin(base_url, href)):
            return DocumentRef(ANCHOR_PDF, urllib.parse.urljoin(base_url, href))

    # 3. The order text is the page itself.
    text = extract_inline_text(html)
    if len(text) >= INLINE_TEXT_MIN_CHARS:
        return DocumentRef(INLINE_HTML, None, len(text))

    return DocumentRef(NO_DOCUMENT, None, len(text))


def is_pdf_bytes(content: bytes) -> bool:
    """SEBI occasionally serves an HTML error page with a .pdf URL and HTTP 200."""
    return content[:5] == b"%PDF-"


# --------------------------------------------------------------------------
# Archive naming
# --------------------------------------------------------------------------

def archive_name(order_id: int | None, source_url: str) -> str:
    """``<order_id>.pdf``; rows with no id get a stable url-derived surrogate."""
    if order_id is not None:
        return f"{order_id}.pdf"
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
    return f"noid_{digest}.pdf"


def archive_pdf(content: bytes, dest: pathlib.Path, cache_file: pathlib.Path | None) -> None:
    """Put the PDF at ``dest``, hard-linking from the cache entry where possible.

    A hard link keeps one copy of the bytes on disk while both paths stay valid,
    so archiving 20k PDFs does not double disk use. Falls back to a real copy
    when linking is not possible (different filesystem, or no cache file).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    if cache_file is not None and cache_file.exists():
        try:
            os.link(cache_file, dest)
            return
        except OSError:
            pass
    tmp = dest.parent / f"{dest.name}.tmp-{os.urandom(6).hex()}"
    try:
        tmp.write_bytes(content)
        tmp.replace(dest)
    finally:
        if tmp.exists():  # pragma: no cover
            tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Network stages
# --------------------------------------------------------------------------

def resolve_document(row, *, force: bool = False) -> tuple[DocumentRef, str | None]:
    """Stage 1: find the document URL for one listing row.

    Returns ``(ref, error)``. A direct-PDF listing row needs no detail fetch.
    """
    if row.listing_doc_kind == "DIRECT_PDF" or looks_like_pdf_url(row.source_url):
        return DocumentRef(DIRECT_PDF, row.source_url), None
    try:
        html = cache.get(row.source_url, headers={"Referer": f"{SEBI_ORIGIN}/"}, force=force)
    except cache.FetchError as exc:
        return DocumentRef(DETAIL_FAILED), str(exc)
    return extract_document_ref(html, row.source_url), None


def download_document(ref: DocumentRef, referer: str,
                      *, force: bool = False) -> tuple[bytes | None, str | None]:
    """Stage 2: fetch the document bytes. Archiving is the caller's job, so that
    nothing reaches ``raw/`` before it has been checked to be a real PDF."""
    assert ref.pdf_url is not None
    try:
        content = cache.get(ref.pdf_url, headers={"Referer": referer}, force=force)
    except cache.FetchError as exc:
        return None, str(exc)
    return content, None


def process_row(row, *, force: bool = False, skip_download: bool = False) -> DocumentRow:
    """Resolve and download one order. Never raises — failures become rows."""
    ref, error = resolve_document(row, force=force)
    base = DocumentRow(
        order_id=row.order_id,
        order_type=row.order_type,
        order_date=row.order_date,
        title=row.title,
        source_url=row.source_url,
        listing_doc_kind=row.listing_doc_kind,
        doc_shape=ref.shape,
        pdf_url=ref.pdf_url,
        pdf_path=None,
        pdf_bytes=None,
        pdf_sha256=None,
        inline_text_chars=ref.inline_text_chars,
        status=OK,
        error=error,
        resolved_at=dt.datetime.now(dt.timezone.utc),
    )

    if ref.shape == DETAIL_FAILED:
        base.status = FAILED_DETAIL
        return base
    if ref.shape == INLINE_HTML:
        base.status = OK_INLINE
        return base
    if ref.shape == NO_DOCUMENT or not ref.pdf_url:
        base.status = NO_DOC
        return base
    if skip_download:
        base.status = OK
        return base

    content, error = download_document(ref, row.source_url, force=force)
    if content is None:
        base.status, base.error = FAILED_DOWNLOAD, error
        return base
    if not is_pdf_bytes(content):
        # Never let a WAF page or HTML 404 into raw/pdfs/ under a .pdf name.
        base.status = NOT_A_PDF
        base.error = f"served {len(content)} bytes that are not a PDF"
        base.pdf_bytes = len(content)
        return base

    dest = PDF_DIR / archive_name(row.order_id, row.source_url)
    archive_pdf(content, dest, cache.cache_path(ref.pdf_url, method="GET"))
    base.pdf_path = str(dest.relative_to(REPO_ROOT))
    base.pdf_bytes = len(content)
    base.pdf_sha256 = hashlib.sha256(content).hexdigest()
    return base


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

PARQUET_SCHEMA = pa.schema(
    [
        pa.field("order_id", pa.int64()),
        pa.field("order_type", pa.string()),
        pa.field("order_date", pa.date32()),
        pa.field("title", pa.string()),
        pa.field("source_url", pa.string()),
        pa.field("listing_doc_kind", pa.string()),
        pa.field("doc_shape", pa.string()),
        pa.field("pdf_url", pa.string()),
        pa.field("pdf_path", pa.string()),
        pa.field("pdf_bytes", pa.int64()),
        pa.field("pdf_sha256", pa.string()),
        pa.field("inline_text_chars", pa.int32()),
        pa.field("status", pa.string()),
        pa.field("error", pa.string()),
        pa.field("resolved_at", pa.timestamp("us", tz="UTC")),
    ]
)


def rows_to_table(rows: list[DocumentRow]) -> pa.Table:
    columns = {name: [] for name in PARQUET_SCHEMA.names}
    for row in rows:
        payload = asdict(row)
        for name in columns:
            columns[name].append(payload[name])
    return pa.Table.from_pydict(columns, schema=PARQUET_SCHEMA)


def write_parquet(rows: list[DocumentRow], path: pathlib.Path = DOCUMENTS_OUT) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(rows_to_table(rows), path, compression="snappy")
    return path


def write_failures(rows: list[DocumentRow], path: pathlib.Path = FAILURES_OUT) -> pathlib.Path:
    """Failures go to CSV rather than crashing the run (runbook Day 3)."""
    failures = [r for r in rows if r.status not in (OK, OK_INLINE)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["order_id", "order_type", "order_date", "source_url",
                         "doc_shape", "pdf_url", "status", "error"])
        for r in failures:
            writer.writerow([r.order_id, r.order_type, r.order_date, r.source_url,
                             r.doc_shape, r.pdf_url, r.status, r.error])
    return path


def sort_rows(rows: list[DocumentRow]) -> list[DocumentRow]:
    return sorted(
        rows,
        key=lambda r: (
            r.order_type,
            r.order_date is None,
            -(r.order_date.toordinal() if r.order_date else 0),
            r.order_id if r.order_id is not None else 1 << 62,
            r.source_url,
        ),
    )


def setup_logging(level: str = "INFO", log_file: pathlib.Path | None = None) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

@dataclass
class ListingInput:
    """The subset of a listing row this stage needs."""

    order_id: int | None
    order_type: str
    order_date: dt.date | None
    title: str
    source_url: str
    listing_doc_kind: str


def load_listings(path: pathlib.Path = LISTINGS_IN) -> list[ListingInput]:
    table = pq.read_table(path)
    data = table.to_pylist()
    return [
        ListingInput(
            order_id=d["order_id"],
            order_type=d["order_type"],
            order_date=d["order_date"],
            title=d["title"],
            source_url=d["source_url"],
            listing_doc_kind=d["doc_kind"],
        )
        for d in data
    ]


def select(rows: list[ListingInput], *, categories: list[str] | None = None,
           limit: int | None = None, order_ids: list[int] | None = None) -> list[ListingInput]:
    out = rows
    if categories:
        wanted = set(categories)
        out = [r for r in out if r.order_type in wanted]
    if order_ids:
        wanted_ids = set(order_ids)
        out = [r for r in out if r.order_id in wanted_ids]
    if limit is not None:
        out = out[:limit]
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.fetch_pdfs",
        description="Resolve SEBI orders to their PDFs and archive them under raw/pdfs/",
    )
    parser.add_argument("--category", action="append",
                        choices=["WTM", "SETTLEMENT", "AO", "ED_CGM"],
                        help="restrict to a category; repeatable")
    parser.add_argument("--limit", type=int, default=None,
                        help="process only the first N orders (development)")
    parser.add_argument("--order-id", action="append", type=int, default=None,
                        help="process specific order ids; repeatable")
    parser.add_argument("--resolve-only", action="store_true",
                        help="find document URLs but do not download the PDFs")
    parser.add_argument("--force", action="store_true", help="bypass the cache")
    parser.add_argument("--offline", action="store_true",
                        help="fail on a cache miss instead of hitting the network")
    parser.add_argument("--listings", type=pathlib.Path, default=LISTINGS_IN)
    parser.add_argument("--out", type=pathlib.Path, default=DOCUMENTS_OUT)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--checkpoint-every", type=int, default=500,
                        help="write the parquet/failures output every N orders, so a "
                             "long run that is interrupted does not lose its results")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.log_level, LOGS_DIR / "fetch_pdfs.log")

    cache.set_offline(args.offline)
    cache.reset_stats()
    started = dt.datetime.now(dt.timezone.utc)

    listings = load_listings(args.listings)
    selected = select(listings, categories=args.category, limit=args.limit,
                      order_ids=args.order_id)
    log.info("processing %d of %d listing rows", len(selected), len(listings))

    rows: list[DocumentRow] = []
    for i, listing in enumerate(selected, 1):
        rows.append(process_row(listing, force=args.force,
                                skip_download=args.resolve_only))
        if i % max(1, args.progress_every) == 0 or i == len(selected):
            done = len(rows)
            ok = sum(1 for r in rows if r.status in (OK, OK_INLINE))
            log.info("%d/%d  ok=%d  failed=%d  network=%d",
                     done, len(selected), ok, done - ok, cache.NETWORK_CALLS)
        if args.checkpoint_every and i % args.checkpoint_every == 0 and i < len(selected):
            write_parquet(sort_rows(list(rows)), args.out)
            write_failures(rows)
            log.info("checkpoint written at %d/%d", i, len(selected))

    rows = sort_rows(rows)
    out_path = write_parquet(rows, args.out)
    fail_path = write_failures(rows)

    by_status: dict[str, int] = {}
    by_shape: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_shape[r.doc_shape] = by_shape.get(r.doc_shape, 0) + 1
    total_bytes = sum(r.pdf_bytes or 0 for r in rows if r.pdf_path)

    log.info("=" * 72)
    log.info("shapes:   %s", dict(sorted(by_shape.items())))
    log.info("statuses: %s", dict(sorted(by_status.items())))
    log.info("pdfs archived=%d  bytes=%.1f MiB  network_calls=%d  elapsed=%s",
             sum(1 for r in rows if r.pdf_path), total_bytes / 1048576,
             cache.NETWORK_CALLS, dt.datetime.now(dt.timezone.utc) - started)
    log.info("wrote %s", out_path)
    failures = sum(1 for r in rows if r.status not in (OK, OK_INLINE))
    if failures:
        log.warning("%d failure(s) recorded in %s — re-run to retry", failures, fail_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
