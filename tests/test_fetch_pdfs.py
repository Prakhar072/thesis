"""Tests for the document resolver / PDF fetcher.

The parsing tests run against real cached SEBI detail pages, so a template
change on SEBI's side shows up here rather than as silently empty output.
"""

import datetime as dt
import pathlib

import pyarrow.parquet as pq
import pytest

from conftest import FakeResponse, fixture_html
from src import cache
from src import fetch_pdfs as fp

PDF_BYTES = b"%PDF-1.4\n%fake pdf for tests\n"


@pytest.fixture
def tmp_pdf_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(fp, "REPO_ROOT", tmp_path)
    return tmp_path / "pdfs"


def listing(order_id=123, url="https://www.sebi.gov.in/enforcement/orders/x_123.html",
            kind="HTML_DETAIL", order_type="WTM"):
    return fp.ListingInput(
        order_id=order_id, order_type=order_type, order_date=dt.date(2026, 2, 23),
        title="Some Order", source_url=url, listing_doc_kind=kind,
    )


# --- URL helpers -----------------------------------------------------------

def test_unwrap_viewer_url():
    wrapped = ("https://www.sebi.gov.in/web/?file="
               "https://www.sebi.gov.in/sebi_data/attachdocs/sep-2026/ORDER_1789637783.pdf")
    assert fp.unwrap_viewer_url(wrapped).endswith("ORDER_1789637783.pdf")


def test_unwrap_leaves_plain_urls_alone():
    plain = "https://www.sebi.gov.in/sebi_data/attachdocs/1291435555408.pdf"
    assert fp.unwrap_viewer_url(plain) == plain


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://x/a.pdf", True),
        ("https://x/a.PDF", True),          # SEBI mixes case
        ("/sebi_data/attachdocs/1.pdf", True),
        ("https://x/a.pdf?y=1", True),      # query string after the extension
        ("https://x/a.html", False),
        ("https://x/a.xlsx", False),
        ("https://x/pdf-report.html", False),
    ],
)
def test_looks_like_pdf_url(url, expected):
    assert fp.looks_like_pdf_url(url) is expected


# --- document extraction against real pages --------------------------------

def test_extract_iframe_pdf_from_real_page():
    html = fixture_html("detail_iframe_pdf.html")
    ref = fp.extract_document_ref(html, "https://www.sebi.gov.in/enforcement/orders/x_1.html")
    assert ref.shape == fp.IFRAME_PDF
    assert ref.pdf_url == ("https://www.sebi.gov.in/sebi_data/attachdocs/"
                           "sep-2026/ORDER_1789637783.pdf")


def test_real_page_has_no_anchor_to_the_pdf():
    """Guards the bug that cost the first probe: anchors alone find nothing."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(fixture_html("detail_iframe_pdf.html"), "lxml")
    assert not [a for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()]


def test_relative_iframe_src_is_resolved_against_the_page():
    html = fixture_html("detail_iframe_relative.html")
    ref = fp.extract_document_ref(html, "https://www.sebi.gov.in/enforcement/orders/a/b_2.html")
    assert ref.shape == fp.IFRAME_PDF
    assert ref.pdf_url.startswith("https://www.sebi.gov.in/sebi_data/")
    assert "//" not in ref.pdf_url.removeprefix("https://")


def test_inline_html_order_is_detected_not_treated_as_failure():
    html = fixture_html("detail_inline_html.html")
    ref = fp.extract_document_ref(html, "https://www.sebi.gov.in/x_10125.html")
    assert ref.shape == fp.INLINE_HTML
    assert ref.pdf_url is None
    assert ref.inline_text_chars > fp.INLINE_TEXT_MIN_CHARS


def test_inline_text_is_the_order_not_the_navigation():
    text = fp.extract_inline_text(fixture_html("detail_inline_html.html"))
    assert "ADJUDICATING OFFICER" in text.upper()
    assert len(text) > 5000
    assert text == " ".join(text.split())


def test_synthetic_viewer_wrapped_iframe():
    html = ("<div><iframe src='https://www.sebi.gov.in/web/?file="
            "/sebi_data/attachdocs/dec-2020/1609417328895.PDF'></iframe></div>")
    ref = fp.extract_document_ref(html, "https://www.sebi.gov.in/enforcement/orders/x_1.html")
    assert ref.shape == fp.IFRAME_PDF
    assert ref.pdf_url == "https://www.sebi.gov.in/sebi_data/attachdocs/dec-2020/1609417328895.PDF"


def test_anchor_pdf_fallback():
    html = "<div><a href='/sebi_data/attachdocs/9.pdf'>Order</a></div>"
    ref = fp.extract_document_ref(html, "https://www.sebi.gov.in/x_1.html")
    assert ref.shape == fp.ANCHOR_PDF
    assert ref.pdf_url == "https://www.sebi.gov.in/sebi_data/attachdocs/9.pdf"


def test_iframe_wins_over_anchor():
    html = ("<iframe src='/sebi_data/attachdocs/real.pdf'></iframe>"
            "<a href='/some/other.pdf'>other</a>")
    ref = fp.extract_document_ref(html, "https://www.sebi.gov.in/x_1.html")
    assert ref.pdf_url.endswith("real.pdf")


@pytest.mark.parametrize(
    "html",
    ["", "   ", "<html></html>", "not html",
     "<iframe></iframe>", "<iframe src=''></iframe>",
     "<section class='main_section'>too short</section>"],
)
def test_no_document_cases_do_not_raise(html):
    ref = fp.extract_document_ref(html, "https://www.sebi.gov.in/x_1.html")
    assert ref.shape == fp.NO_DOCUMENT
    assert ref.pdf_url is None


def test_short_inline_text_is_not_mistaken_for_an_order():
    html = "<section class='main_section'>" + ("x " * 200) + "</section>"
    assert fp.extract_document_ref(html, "https://x/y_1.html").shape == fp.NO_DOCUMENT


# --- pdf sniffing ----------------------------------------------------------

def test_is_pdf_bytes():
    assert fp.is_pdf_bytes(b"%PDF-1.7 ...")
    assert not fp.is_pdf_bytes(b"<html>404 not found</html>")
    assert not fp.is_pdf_bytes(b"")


# --- archive naming and linking --------------------------------------------

def test_archive_name_uses_order_id():
    assert fp.archive_name(99913, "https://x/y.html") == "99913.pdf"


def test_archive_name_for_rows_without_an_id_is_stable_and_distinct():
    a = fp.archive_name(None, "https://x/a.pdf")
    b = fp.archive_name(None, "https://x/b.pdf")
    assert a.startswith("noid_") and a.endswith(".pdf")
    assert a != b
    assert a == fp.archive_name(None, "https://x/a.pdf")


def test_archive_pdf_hardlinks_from_cache(tmp_path):
    cache_file = tmp_path / "cached"
    cache_file.write_bytes(PDF_BYTES)
    dest = tmp_path / "pdfs" / "1.pdf"
    fp.archive_pdf(PDF_BYTES, dest, cache_file)
    assert dest.read_bytes() == PDF_BYTES
    assert dest.stat().st_ino == cache_file.stat().st_ino, "should share one copy on disk"


def test_archive_pdf_falls_back_to_copy(tmp_path):
    dest = tmp_path / "pdfs" / "2.pdf"
    fp.archive_pdf(PDF_BYTES, dest, None)
    assert dest.read_bytes() == PDF_BYTES


def test_archive_pdf_is_idempotent(tmp_path):
    cache_file = tmp_path / "cached"
    cache_file.write_bytes(PDF_BYTES)
    dest = tmp_path / "pdfs" / "3.pdf"
    fp.archive_pdf(PDF_BYTES, dest, cache_file)
    fp.archive_pdf(PDF_BYTES, dest, cache_file)
    assert dest.read_bytes() == PDF_BYTES


def test_archive_leaves_no_temp_files(tmp_path):
    dest = tmp_path / "pdfs" / "4.pdf"
    fp.archive_pdf(PDF_BYTES, dest, None)
    assert not any(".tmp-" in p.name for p in dest.parent.iterdir())


# --- process_row end to end ------------------------------------------------

def test_direct_pdf_row_skips_the_detail_fetch(tmp_cache, tmp_pdf_dir, fake_http):
    fake_http.queue(FakeResponse(PDF_BYTES))
    row = fp.process_row(listing(order_id=None, url="https://www.sebi.gov.in/a/eo.pdf",
                                 kind="DIRECT_PDF"))
    assert row.doc_shape == fp.DIRECT_PDF
    assert row.status == fp.OK
    assert len(fake_http.calls) == 1, "no detail page should be fetched"
    assert row.pdf_path.endswith(".pdf")
    assert row.pdf_sha256 and row.pdf_bytes == len(PDF_BYTES)


def test_iframe_row_fetches_detail_then_pdf(tmp_cache, tmp_pdf_dir, fake_http):
    detail = b"<iframe src='/sebi_data/attachdocs/1.pdf'></iframe>"
    fake_http.queue(FakeResponse(detail), FakeResponse(PDF_BYTES))
    row = fp.process_row(listing(order_id=123))
    assert row.doc_shape == fp.IFRAME_PDF
    assert row.status == fp.OK
    assert [c["url"] for c in fake_http.calls][1].endswith("/sebi_data/attachdocs/1.pdf")
    assert (tmp_pdf_dir / "123.pdf").read_bytes() == PDF_BYTES
    assert row.pdf_path == "pdfs/123.pdf"


def test_inline_html_row_downloads_nothing(tmp_cache, tmp_pdf_dir, fake_http):
    fake_http.queue(FakeResponse(fixture_html("detail_inline_html.html").encode()))
    row = fp.process_row(listing(order_id=10125))
    assert row.doc_shape == fp.INLINE_HTML
    assert row.status == fp.OK_INLINE
    assert row.pdf_path is None
    assert row.inline_text_chars > 5000
    assert len(fake_http.calls) == 1


def test_detail_404_is_recorded_not_raised(tmp_cache, tmp_pdf_dir, fake_http):
    fake_http.queue(FakeResponse(b"nope", 404))
    row = fp.process_row(listing(order_id=7))
    assert row.status == fp.FAILED_DETAIL
    assert row.doc_shape == fp.DETAIL_FAILED
    assert "404" in row.error


def test_pdf_download_failure_is_recorded(tmp_cache, tmp_pdf_dir, fake_http):
    fake_http.queue(FakeResponse(b"<iframe src='/a/1.pdf'></iframe>"),
                    *[FakeResponse(b"", 503)] * 6)
    row = fp.process_row(listing(order_id=8))
    assert row.status == fp.FAILED_DOWNLOAD
    assert row.pdf_url.endswith("/a/1.pdf")
    assert row.pdf_path is None


def test_html_served_as_pdf_is_flagged_not_archived(tmp_cache, tmp_pdf_dir, fake_http):
    """SEBI sometimes returns an error page with HTTP 200 at a .pdf URL."""
    fake_http.queue(FakeResponse(b"<iframe src='/a/1.pdf'></iframe>"),
                    FakeResponse(b"<html>Page not found</html>"))
    row = fp.process_row(listing(order_id=9))
    assert row.status == fp.NOT_A_PDF
    assert row.pdf_path is None
    assert not (tmp_pdf_dir / "9.pdf").exists()


def test_resolve_only_skips_download(tmp_cache, tmp_pdf_dir, fake_http):
    fake_http.queue(FakeResponse(b"<iframe src='/a/1.pdf'></iframe>"))
    row = fp.process_row(listing(order_id=11), skip_download=True)
    assert row.doc_shape == fp.IFRAME_PDF
    assert row.pdf_url.endswith("/a/1.pdf")
    assert row.pdf_path is None
    assert len(fake_http.calls) == 1


def test_rerun_uses_cache_and_makes_no_network_call(tmp_cache, tmp_pdf_dir, fake_http):
    fake_http.queue(FakeResponse(b"<iframe src='/a/1.pdf'></iframe>"), FakeResponse(PDF_BYTES))
    first = fp.process_row(listing(order_id=123))
    assert cache.NETWORK_CALLS == 2

    cache.reset_stats()
    cache.set_offline(True)
    try:
        second = fp.process_row(listing(order_id=123))
    finally:
        cache.set_offline(False)
    assert cache.NETWORK_CALLS == 0
    assert second.pdf_sha256 == first.pdf_sha256
    assert second.status == fp.OK


# --- selection and output --------------------------------------------------

def test_select_by_category_and_limit():
    rows = [listing(order_id=i, order_type=t)
            for i, t in enumerate(["WTM", "AO", "WTM", "SETTLEMENT"])]
    assert len(fp.select(rows, categories=["WTM"])) == 2
    assert len(fp.select(rows, limit=2)) == 2
    assert [r.order_id for r in fp.select(rows, order_ids=[1, 3])] == [1, 3]


def test_parquet_schema_and_roundtrip(tmp_path):
    rows = [
        fp.DocumentRow(order_id=1, order_type="WTM", order_date=dt.date(2026, 5, 5),
                       title="t", source_url="u", listing_doc_kind="HTML_DETAIL",
                       doc_shape=fp.IFRAME_PDF, pdf_url="https://x/1.pdf",
                       pdf_path="raw/pdfs/1.pdf", pdf_bytes=10, pdf_sha256="ab",
                       inline_text_chars=0, status=fp.OK, error=None,
                       resolved_at=dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)),
        fp.DocumentRow(order_id=2, order_type="AO", order_date=None, title="t2",
                       source_url="u2", listing_doc_kind="HTML_DETAIL",
                       doc_shape=fp.INLINE_HTML, pdf_url=None, pdf_path=None,
                       pdf_bytes=None, pdf_sha256=None, inline_text_chars=9000,
                       status=fp.OK_INLINE, error=None, resolved_at=None),
    ]
    out = fp.write_parquet(rows, tmp_path / "docs.parquet")
    table = pq.read_table(out)
    assert table.schema.equals(fp.PARQUET_SCHEMA)
    d = table.to_pydict()
    assert d["order_id"] == [1, 2]
    assert d["pdf_path"] == ["raw/pdfs/1.pdf", None]
    assert d["status"] == [fp.OK, fp.OK_INLINE]


def test_failures_csv_lists_only_failures(tmp_path):
    rows = [
        fp.DocumentRow(1, "WTM", None, "t", "u", "HTML_DETAIL", fp.IFRAME_PDF,
                       "https://x/1.pdf", "raw/pdfs/1.pdf", 10, "ab", 0, fp.OK, None, None),
        fp.DocumentRow(2, "AO", None, "t", "u2", "HTML_DETAIL", fp.DETAIL_FAILED,
                       None, None, None, None, 0, fp.FAILED_DETAIL, "HTTP 404", None),
        fp.DocumentRow(3, "AO", None, "t", "u3", "HTML_DETAIL", fp.INLINE_HTML,
                       None, None, None, None, 9000, fp.OK_INLINE, None, None),
    ]
    out = fp.write_failures(rows, tmp_path / "fetch_failures.csv")
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2, "header + the one real failure"
    assert "HTTP 404" in lines[1]
    assert "OK_INLINE" not in out.read_text()


def test_write_parquet_handles_empty(tmp_path):
    out = fp.write_parquet([], tmp_path / "empty.parquet")
    assert pq.read_table(out).num_rows == 0


# --- CLI / paths -----------------------------------------------------------

def test_cli_development_invocation():
    args = fp.build_arg_parser().parse_args(["--category", "ED_CGM", "--limit", "20"])
    assert args.category == ["ED_CGM"] and args.limit == 20
    assert args.force is False and args.offline is False


def test_cli_rejects_excluded_category():
    with pytest.raises(SystemExit):
        fp.build_arg_parser().parse_args(["--category", "SAT"])


def test_paths_are_wsl_native():
    for path in (fp.PDF_DIR, fp.DOCUMENTS_OUT, fp.FAILURES_OUT, fp.LISTINGS_IN):
        text = str(path)
        assert isinstance(path, pathlib.Path)
        assert text.startswith("/") and not text.startswith("/mnt/")
        assert "\\" not in text and ":" not in text
