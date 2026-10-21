"""Stage 1: walk the SEBI enforcement-order listings into ``derived/listings.parquet``.

Four categories form the primary corpus (SAT, Court, Special Court and
Reg. 30A listings are deliberately excluded — they are appellate/judicial
follow-ups, not SEBI's own closing orders).

The stages are kept strictly separate so the parser can be re-run against
cached responses without any network access::

    fetch (src.cache)  ->  parse HTML  ->  validate/dedupe  ->  write Parquet

How SEBI's pagination actually works
------------------------------------
The AJAX endpoint is a POST to ``getnewslistinfo.jsp``. The browser sends both
``nextValue`` and ``doDirect``, but only **doDirect** selects the page, and it
is **0-based**: ``doDirect = page - 1``. Verified empirically — holding
``doDirect`` fixed while varying ``nextValue`` returns byte-identical HTML,
successive ``doDirect`` values return disjoint row sets in descending date
order, and the row counts reconcile exactly with SEBI's published totals
(ED/CGM: 17*25 + 15 = 440; WTM: 256*25 + 24 = 6424). ``next="n"`` must be sent
for ``doDirect`` to be honoured. ``nextValue`` is still sent, mirroring the
browser and keeping the cache key page-distinct on both fields.

Authoritative page count comes from the response's hidden ``totalpage`` field,
never from the published order counts.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import pathlib
import re
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field, replace

import pyarrow as pa
import pyarrow.parquet as pq
from bs4 import BeautifulSoup

from src import cache

log = logging.getLogger("scrape_listings")

REPO_ROOT = cache.REPO_ROOT
LISTING_URL = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"
LISTING_REFERER = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"

DEFAULT_OUT = REPO_ROOT / "derived" / "listings.parquet"
REPORTS_DIR = REPO_ROOT / "reports"
LOGS_DIR = REPO_ROOT / "logs"

SID = 2
SSID = 9
ROWS_PER_PAGE = 25  # SEBI's nextDel; used only for reconciliation arithmetic
#: Hard stop for the page walk. Only reachable if ``totalpage`` is unreadable
#: and SEBI keeps returning rows; a runaway loop would hammer the site.
MAX_PAGES = 2000


@dataclass(frozen=True)
class Category:
    """One SEBI listing category. ``order_type`` is assigned from here, never
    inferred from an order's title."""

    order_type: str
    smid: int
    sm_text: str
    s_text: str = "Enforcement"
    ss_text: str = "Orders"
    published_count: int | None = None  # sanity check only


CATEGORIES: dict[str, Category] = {
    "WTM": Category("WTM", 2, "Orders of Chairperson/Members", published_count=6424),
    "SETTLEMENT": Category("SETTLEMENT", 3, "Settlement Orders", published_count=1991),
    "AO": Category("AO", 6, "Orders of AO", published_count=12002),
    "ED_CGM": Category(
        "ED_CGM", 133, "Orders of ED / CGM (Quasi-Judicial Authorities)", published_count=440
    ),
}


@dataclass
class ListingRow:
    """One order as SEBI's listing page shows it — one row of ``derived/listings.parquet``.

    Attributes:
        order_id (int | None): SEBI's id from the ``_<id>.html`` URL tail; None for 76 direct-PDF / odd rows.
        order_date (datetime.date | None): the listing date; order_date_raw (str) is the text it came from.
        title (str): the cleaned title; source_url (str): the detail page (or the PDF itself).
        order_type (str): WTM, SETTLEMENT, AO or ED_CGM — from the category, never the title.
        listing_category (str), listing_page (int), listing_url (str), smid (int): where the row was found.
        doc_kind (str): HTML_DETAIL, DIRECT_PDF or OTHER.
        corrigendum_url (str | None), sat_appeals (str | None): supplements SEBI attaches to the row.
        needs_review (bool), review_reason (str | None): missing id, date or title.
        scraped_at (datetime.datetime | None): the cache file's mtime (when the page was fetched).
    """
    order_id: int | None
    order_date: dt.date | None
    order_date_raw: str
    title: str
    source_url: str
    order_type: str
    listing_category: str
    listing_page: int
    listing_url: str
    smid: int
    doc_kind: str
    corrigendum_url: str | None = None
    #: SAT appeal numbers SEBI annotates on the listing ("539/2022; 475/2022"),
    #: i.e. this order was challenged before the Securities Appellate Tribunal.
    sat_appeals: str | None = None
    needs_review: bool = False
    review_reason: str | None = None
    scraped_at: dt.datetime | None = None


@dataclass
class PageLog:
    """One listing page fetched: a row of ``reports/listing_pages.csv``.

    Attributes:
        order_type (str), page (int), total_pages (int | None), n_rows (int),
        status (str): ok, failed or empty; from_cache (bool); error (str | None).
    """
    order_type: str
    page: int
    total_pages: int | None
    n_rows: int
    status: str  # ok | failed | empty
    from_cache: bool
    error: str | None = None


@dataclass
class CategoryResult:
    """Everything one category's walk produced.

    Attributes:
        category (Category): the category walked.
        total_pages (int | None): from the hidden ``totalpage`` field.
        rows (list[ListingRow]): every row parsed, before de-duplication.
        pages (list[PageLog]): one entry per page fetched.
    """
    category: Category
    total_pages: int | None = None
    rows: list[ListingRow] = field(default_factory=list)
    pages: list[PageLog] = field(default_factory=list)

    @property
    def failed_pages(self) -> list[PageLog]:
        """Pages whose fetch failed (to retry by re-running).

        Returns:
            list[PageLog]: the pages with status "failed".
        """
        return [p for p in self.pages if p.status == "failed"]


# --------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------

def build_form(category: Category, page: int) -> dict[str, str]:
    """Form body for one listing page (1-based ``page``).

    Args:
        category (Category): the listing category.
        page (int): 1-based page number.
    Returns:
        dict[str, str]: the POST form fields (``doDirect = page - 1`` selects the page).
    Raises:
        ValueError: page < 1.
    """
    if page < 1:
        raise ValueError(f"page must be >= 1, got {page}")
    return {
        "nextValue": str(page),
        "next": "n",
        "search": "",
        "fromDate": "",
        "toDate": "",
        "fromYear": "",
        "toYear": "",
        "deptId": "-1",
        "sid": str(SID),
        "ssid": str(SSID),
        "smid": str(category.smid),
        "ssidhidden": str(SSID),
        "intmid": "-1",
        "sText": category.s_text,
        "ssText": category.ss_text,
        "smText": category.sm_text,
        "doDirect": str(page - 1),  # 0-based: this is the real page selector
    }


def build_headers(category: Category) -> dict[str, str]:
    """Minimal headers the WAF accepts. No cookies, no JSESSIONID, no analytics.

    Args:
        category (Category): the listing category (its smid goes into the Referer).
    Returns:
        dict[str, str]: Accept, Content-type, Origin and Referer headers.
    """
    referer = (
        f"{LISTING_REFERER}?doListing=yes&sid={SID}&ssid={SSID}&smid={category.smid}"
    )
    return {
        "Accept": "*/*",
        "Content-type": "application/x-www-form-urlencoded",
        "Origin": "https://www.sebi.gov.in",
        "Referer": referer,
    }


def fetch_listing_page(category: Category, page: int, *, force: bool = False) -> bytes:
    """Fetch one listing page through the cache. The only network call here.

    Args:
        category (Category): the listing category.
        page (int): 1-based page number.
        force (bool): re-fetch even if cached.
    Returns:
        bytes: the page's HTML.
    Raises:
        cache.FetchError: the request failed after retries.
    """
    return cache.get(
        LISTING_URL,
        method="POST",
        data=build_form(category, page),
        headers=build_headers(category),
        force=force,
    )


def listing_is_cached(category: Category, page: int) -> bool:
    """Whether a listing page is already in the cache (for the log's cached/network note).

    Args:
        category (Category): the listing category.
        page (int): 1-based page number.
    Returns:
        bool: True if cached.
    """
    return cache.is_cached(LISTING_URL, method="POST", data=build_form(category, page))


def listing_cached_at(category: Category, page: int) -> dt.datetime | None:
    """When a listing page was fetched: its cache file's mtime (becomes ``scraped_at``).

    Args:
        category (Category): the listing category.
        page (int): 1-based page number.
    Returns:
        datetime.datetime | None: the UTC mtime, or None if not cached.
    """
    path = cache.cache_path(LISTING_URL, method="POST", data=build_form(category, page))
    if not path.exists():
        return None
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)


# --------------------------------------------------------------------------
# Parsing — pure functions over HTML, no network
# --------------------------------------------------------------------------

_ORDER_ID_RE = re.compile(r"_(\d+)\.html?$", re.IGNORECASE)

_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%d-%m-%Y", "%Y-%m-%d")


def _soup(html: str | bytes) -> BeautifulSoup:
    """Parse HTML (bytes are decoded as UTF-8, bad bytes replaced) with lxml.

    Args:
        html (str | bytes): the page.
    Returns:
        BeautifulSoup: the parsed document.
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    return BeautifulSoup(html, "lxml")


def parse_total_pages(html: str | bytes) -> int | None:
    """Read the authoritative hidden ``totalpage`` field. None if absent.

    SEBI emits the value unquoted (``value=257 />``), so a stray ``/`` can end
    up inside the attribute; take the leading digits rather than trusting it.

    Args:
        html (str | bytes): a listing response.
    Returns:
        int | None: the page count, or None if the field is missing or unreadable.
    """
    node = _soup(html).find("input", attrs={"name": "totalpage"})
    if node is None:
        return None
    match = re.match(r"\s*(\d+)", str(node.get("value", "")))
    return int(match.group(1)) if match else None


def parse_hidden_fields(html: str | bytes) -> dict[str, str]:
    """All hidden inputs (``totalpage``, ``nextDel``, ``nextValue``) for diagnostics.

    Args:
        html (str | bytes): a listing response.
    Returns:
        dict[str, str]: hidden input name → value.
    """
    out: dict[str, str] = {}
    for node in _soup(html).find_all("input", attrs={"type": "hidden"}):
        name = node.get("name")
        if name:
            out[str(name)] = str(node.get("value", "")).strip()
    return out


def extract_order_id(url: str | None) -> int | None:
    """SEBI's numeric id, taken from the ``_99913.html`` tail of a detail URL.

    Returns None rather than guessing when the link is a direct PDF or any
    other shape — those rows are flagged for review instead.

    Args:
        url (str | None): a detail-page URL.
    Returns:
        int | None: the id, or None for a PDF link or any other shape.
    """
    if not url:
        return None
    path = urllib.parse.urlparse(url.strip()).path
    match = _ORDER_ID_RE.search(path)
    return int(match.group(1)) if match else None


def parse_order_date(text: str | None) -> dt.date | None:
    """Parse SEBI's listing date (``Feb 23, 2026``). None if unparseable.

    Args:
        text (str | None): the listing's date text.
    Returns:
        datetime.date | None: the date, or None if no known format fits.
    """
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip().rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _normalise(text: str | None) -> str:
    """Collapse runs of whitespace and strip.

    Args:
        text (str | None): any text.
    Returns:
        str: the cleaned text ("" for None).
    """
    return re.sub(r"\s+", " ", (text or "")).strip()


_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")


def clean_title_attr(attr: str | None) -> tuple[str, str | None, list[str]]:
    """Clean SEBI's ``title`` attribute, which can carry HTML.

    SEBI appends markup to the title of 1,541 orders: corrigendum links
    (``…Limited<br><a href='…_p.pdf'>[Corrigendum]</a>``) and SAT appeal
    annotations (``<span>[SAT Appeal No.:</span><a href='sat.gov.in/…'>539/2022]</a>``).
    Corrigendum links are removed and returned separately; other links keep
    their text, so an appeal annotation stays readable in the title.

    Returns ``(clean title, corrigendum url, SAT appeal numbers)``.

    Args:
        attr (str | None): the anchor's raw ``title`` attribute (may contain HTML).
    Returns:
        tuple[str, str | None, list[str]]: (clean title, corrigendum URL or None, SAT appeal numbers).
    """
    attr = attr or ""
    if not _TAG_RE.search(attr):
        return _normalise(attr), None, []
    fragment = BeautifulSoup(attr, "lxml")
    corrigendum: str | None = None
    appeals: list[str] = []
    for anchor in fragment.find_all("a"):
        text = _normalise(anchor.get_text(" "))
        href = (anchor.get("href") or "").strip()
        if "corrigendum" in f"{text} {href}".lower():
            corrigendum = corrigendum or href or None
            anchor.decompose()
            continue
        if "sat.gov.in" in href.lower() and text:
            appeals.append(text.rstrip("]").strip())
        anchor.replace_with(text)
    return _normalise(fragment.get_text(" ")), corrigendum, appeals


def _select_rows_table(soup: BeautifulSoup):
    """The orders table only.

    ``table#sample_1`` holds the orders. A second table, ``table.ordersum``,
    carries the seven static "Order-11B-<year>.xlsx" summary spreadsheets and
    is repeated on every WTM page — those are not orders and must not enter
    the corpus.

    Args:
        soup (BeautifulSoup): a parsed listing response.
    Returns:
        bs4.Tag | None: the orders table, or None if there is none.
    """
    table = soup.find("table", id="sample_1")
    if table is not None:
        return table
    for candidate in soup.find_all("table"):
        classes = candidate.get("class") or []
        if "ordersum" not in classes:
            return candidate
    return None


def parse_rows(
    html: str | bytes,
    category: Category,
    page: int,
    *,
    scraped_at: dt.datetime | None = None,
) -> list[ListingRow]:
    """Extract order rows from one listing response. Never raises on bad HTML.

    Args:
        html (str | bytes): one listing response.
        category (Category): the category it belongs to (sets order_type).
        page (int): its page number.
        scraped_at (datetime.datetime | None): when the page was fetched.
    Returns:
        list[ListingRow]: one row per order on the page (supplement links are not rows).
    """
    table = _select_rows_table(_soup(html))
    if table is None:
        return []

    rows: list[ListingRow] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue  # header row, or a layout row with nothing in it
        anchors = [a for a in tr.find_all("a", href=True) if _normalise(a["href"])]
        if not anchors:
            continue

        # The first anchor is the order; later ones are supplements such as
        # "[Corrigendum]" and must not become rows of their own.
        primary = anchors[0]
        corrigendum = next(
            (
                _normalise(a["href"])
                for a in anchors[1:]
                if "corrigendum" in _normalise(a.get_text()).lower()
                or "corrigendum" in _normalise(a.get("title")).lower()
            ),
            None,
        )

        href = _normalise(primary["href"])
        date_raw = _normalise(cells[0].get_text(" "))
        order_date = parse_order_date(date_raw)
        # The attribute is preferred because the visible text is cut short where
        # a SAT appeal link begins; the visible text is the fallback.
        title, title_corrigendum, appeals = clean_title_attr(primary.get("title"))
        title = title or _normalise(primary.get_text(" "))
        corrigendum = corrigendum or title_corrigendum
        order_id = extract_order_id(href)

        reasons = []
        if order_id is None:
            reasons.append("no_order_id_in_url")
        if order_date is None:
            reasons.append("unparsed_date")
        if not title:
            reasons.append("empty_title")

        if order_id is not None:
            doc_kind = "HTML_DETAIL"
        elif href.lower().split("?")[0].endswith(".pdf"):
            doc_kind = "DIRECT_PDF"
        else:
            doc_kind = "OTHER"

        rows.append(
            ListingRow(
                order_id=order_id,
                order_date=order_date,
                order_date_raw=date_raw,
                title=title,
                source_url=href,
                order_type=category.order_type,
                listing_category=category.sm_text,
                listing_page=page,
                listing_url=LISTING_URL,
                smid=category.smid,
                doc_kind=doc_kind,
                corrigendum_url=corrigendum,
                sat_appeals="; ".join(appeals) or None,
                needs_review=bool(reasons),
                review_reason=",".join(reasons) or None,
                scraped_at=scraped_at,
            )
        )
    return rows


# --------------------------------------------------------------------------
# Scraping a category
# --------------------------------------------------------------------------

def scrape_category(
    category: Category,
    *,
    start_page: int = 1,
    end_page: int | None = None,
    force: bool = False,
) -> CategoryResult:
    """Walk pages ``start_page..end_page`` (default: to ``totalpage``).

    A failing page is logged and skipped; it never aborts the run. Because
    successful pages stay cached, re-running resumes at no network cost.

    Args:
        category (Category): the category to walk.
        start_page (int): first page (1-based).
        end_page (int | None): last page, inclusive; None = up to ``totalpage``.
        force (bool): re-fetch cached pages.
    Returns:
        CategoryResult: rows, page logs and the page count.
    """
    result = CategoryResult(category=category)
    page = max(1, start_page)
    hard_end = end_page

    while True:
        if hard_end is not None and page > hard_end:
            break
        if result.total_pages is not None and page > result.total_pages:
            break
        if page - max(1, start_page) >= MAX_PAGES:
            log.error("%s: hit the %d-page safety cap, stopping", category.order_type, MAX_PAGES)
            break

        from_cache = listing_is_cached(category, page)
        try:
            html = fetch_listing_page(category, page, force=force)
        except cache.FetchError as exc:
            log.error("%s page %d: FAILED (%s)", category.order_type, page, exc)
            result.pages.append(
                PageLog(category.order_type, page, result.total_pages, 0, "failed",
                        from_cache, str(exc))
            )
            page += 1
            if hard_end is None and result.total_pages is None:
                # Could not establish totalpage from the first page; stop rather
                # than walk blindly into the site.
                log.error("%s: no totalpage yet, aborting category", category.order_type)
                break
            continue

        if result.total_pages is None:
            result.total_pages = parse_total_pages(html)
            if result.total_pages is None:
                log.error(
                    "%s page %d: no readable totalpage; falling back to walking "
                    "until an empty page (capped at %d)",
                    category.order_type, page, MAX_PAGES,
                )

        rows = parse_rows(html, category, page, scraped_at=listing_cached_at(category, page))
        result.rows.extend(rows)
        result.pages.append(
            PageLog(category.order_type, page, result.total_pages, len(rows),
                    "ok" if rows else "empty", from_cache)
        )
        log.info(
            "%-11s page %4d/%-4s rows=%3d %s",
            category.order_type, page,
            result.total_pages if result.total_pages is not None else "?",
            len(rows), "(cached)" if from_cache else "(network)",
        )

        if result.total_pages is None and not rows:
            break  # unknown extent and nothing came back — stop
        page += 1

    return result


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------

_CONFLICT_FIELDS = ("order_date", "title", "source_url", "order_type")


def _dedupe_key(row: ListingRow) -> tuple[str, str]:
    """order_id where present; otherwise the URL, so PDF-only rows still dedupe.

    Args:
        row (ListingRow): a parsed row.
    Returns:
        tuple[str, str]: ("order_id", id) or ("source_url", url).
    """
    if row.order_id is not None:
        return ("order_id", str(row.order_id))
    return ("source_url", row.source_url)


def deduplicate(rows: list[ListingRow]) -> tuple[list[ListingRow], list[dict]]:
    """One canonical row per order_id, plus a report of conflicting duplicates.

    Duplicates are never silently dropped: any duplicate whose metadata
    disagrees with the row we keep is emitted as a conflict record.

    Args:
        rows (list[ListingRow]): rows from every page and category.
    Returns:
        tuple[list[ListingRow], list[dict]]: the canonical rows, and one conflict record per duplicate
        whose date, title, URL or type disagrees with the kept row.
    """
    canonical: dict[tuple[str, str], ListingRow] = {}
    conflicts: list[dict] = []

    for row in rows:
        key = _dedupe_key(row)
        kept = canonical.get(key)
        if kept is None:
            canonical[key] = row
            continue
        differing = {
            f: (getattr(kept, f), getattr(row, f))
            for f in _CONFLICT_FIELDS
            if getattr(kept, f) != getattr(row, f)
        }
        if differing:
            conflicts.append(
                {
                    "key_kind": key[0],
                    "key": key[1],
                    "fields": ";".join(sorted(differing)),
                    "kept_order_type": kept.order_type,
                    "kept_page": kept.listing_page,
                    "kept_date": kept.order_date,
                    "kept_title": kept.title,
                    "kept_url": kept.source_url,
                    "other_order_type": row.order_type,
                    "other_page": row.listing_page,
                    "other_date": row.order_date,
                    "other_title": row.title,
                    "other_url": row.source_url,
                }
            )
        if kept.needs_review and not row.needs_review:
            canonical[key] = row  # prefer the better-parsed copy

    return list(canonical.values()), conflicts


def sort_rows(rows: list[ListingRow]) -> list[ListingRow]:
    """Deterministic order: order_type asc, order_date desc (undated last), order_id asc.

    Args:
        rows (list[ListingRow]): rows to sort.
    Returns:
        list[ListingRow]: sorted by type, date (newest first, undated last), then id.
    """
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


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

PARQUET_SCHEMA = pa.schema(
    [
        pa.field("order_id", pa.int64()),
        pa.field("order_date", pa.date32()),
        pa.field("order_date_raw", pa.string()),
        pa.field("title", pa.string()),
        pa.field("source_url", pa.string()),
        pa.field("order_type", pa.string()),
        pa.field("listing_category", pa.string()),
        pa.field("listing_page", pa.int32()),
        pa.field("listing_url", pa.string()),
        pa.field("smid", pa.int32()),
        pa.field("doc_kind", pa.string()),
        pa.field("corrigendum_url", pa.string()),
        pa.field("sat_appeals", pa.string()),
        pa.field("needs_review", pa.bool_()),
        pa.field("review_reason", pa.string()),
        pa.field("scraped_at", pa.timestamp("us", tz="UTC")),
    ]
)


def rows_to_table(rows: list[ListingRow]) -> pa.Table:
    """Rows → an Arrow table with the fixed ``PARQUET_SCHEMA``.

    Args:
        rows (list[ListingRow]): the rows.
    Returns:
        pyarrow.Table: one row per listing row.
    """
    columns = {name: [] for name in PARQUET_SCHEMA.names}
    for row in rows:
        payload = asdict(row)
        for name in columns:
            columns[name].append(payload[name])
    return pa.Table.from_pydict(columns, schema=PARQUET_SCHEMA)


def write_parquet(rows: list[ListingRow], path: pathlib.Path = DEFAULT_OUT) -> pathlib.Path:
    """Write the listing table (snappy-compressed Parquet).

    Args:
        rows (list[ListingRow]): the rows.
        path (pathlib.Path): output file, default ``derived/listings.parquet``.
    Returns:
        pathlib.Path: the path written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(rows_to_table(rows), path, compression="snappy")
    return path


def _write_csv(path: pathlib.Path, records: list[dict]) -> None:
    """Write dicts as a CSV (an empty file when there are none).

    Args:
        path (pathlib.Path): output file.
        records (list[dict]): rows; the first one's keys are the header.
    Returns:
        None.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        path.write_text("")
        return
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def setup_logging(level: str = "INFO", log_file: pathlib.Path | None = None) -> None:
    """Log to stdout (and a file); silence httpx's per-request lines.

    Args:
        level (str): logging level name, e.g. "INFO".
        log_file (pathlib.Path | None): also log here.
    Returns:
        None.
    """
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
    # httpx logs a line per request; ours already says more.
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.scrape_listings``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.scrape_listings",
        description="Scrape SEBI enforcement-order listings into derived/listings.parquet",
    )
    parser.add_argument(
        "--category", action="append", choices=sorted(CATEGORIES),
        help="category to scrape; repeatable. Default: all four.",
    )
    parser.add_argument("--all", action="store_true", help="scrape all four categories (default)")
    parser.add_argument("--force", action="store_true", help="bypass the cache and re-fetch")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument(
        "--end-page", type=int, default=None,
        help="last page (inclusive). Default: the response's totalpage.",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="fail on a cache miss instead of hitting the network; proves a run is served from disk",
    )
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run stage 1: scrape the categories, de-duplicate, write the table and the three reports.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: exit code — 0, or 1 if any page failed (re-run to retry it).
    """
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.log_level, LOGS_DIR / "scrape_listings.log")

    names = sorted(set(args.category)) if args.category and not args.all else sorted(CATEGORIES)
    cache.set_offline(args.offline)
    cache.reset_stats()
    started = dt.datetime.now(dt.timezone.utc)

    results = [
        scrape_category(
            CATEGORIES[name],
            start_page=args.start_page,
            end_page=args.end_page,
            force=args.force,
        )
        for name in names
    ]

    all_rows = [row for r in results for row in r.rows]
    unique_rows, conflicts = deduplicate(all_rows)
    unique_rows = sort_rows(unique_rows)
    out_path = write_parquet(unique_rows, args.out)

    page_logs = [asdict(p) for r in results for p in r.pages]
    _write_csv(REPORTS_DIR / "listing_pages.csv", page_logs)
    _write_csv(REPORTS_DIR / "listing_conflicts.csv", conflicts)
    _write_csv(
        REPORTS_DIR / "listing_needs_review.csv",
        [asdict(r) for r in unique_rows if r.needs_review],
    )

    log.info("=" * 72)
    for r in results:
        cat = r.category
        scraped = [p for p in r.pages if p.status != "failed"]
        n_rows = len(r.rows)
        ids = {row.order_id for row in r.rows if row.order_id is not None}
        expected = (
            None if r.total_pages is None
            else (r.total_pages - 1) * ROWS_PER_PAGE  # lower bound; last page is partial
        )
        log.info(
            "%-11s pages=%d/%s rows=%d unique_ids=%d no_id=%d failed=%d published~%s",
            cat.order_type, len(scraped), r.total_pages, n_rows, len(ids),
            sum(1 for row in r.rows if row.order_id is None),
            len(r.failed_pages), cat.published_count,
        )
        if expected is not None and args.end_page is None and args.start_page == 1:
            if not (expected <= n_rows <= expected + ROWS_PER_PAGE):
                log.warning(
                    "%s: %d rows outside expected %d..%d for %d pages — investigate",
                    cat.order_type, n_rows, expected, expected + ROWS_PER_PAGE, r.total_pages,
                )
    log.info(
        "total rows=%d unique=%d conflicts=%d needs_review=%d network_calls=%d elapsed=%s",
        len(all_rows), len(unique_rows), len(conflicts),
        sum(1 for r in unique_rows if r.needs_review), cache.NETWORK_CALLS,
        dt.datetime.now(dt.timezone.utc) - started,
    )
    log.info("wrote %s", out_path)

    failed = [p for r in results for p in r.failed_pages]
    if failed:
        log.warning("%d page(s) failed; re-run to retry them: %s", len(failed),
                    ", ".join(f"{p.order_type}:{p.page}" for p in failed[:20]))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
