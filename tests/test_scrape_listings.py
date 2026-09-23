"""Tests for the listing scraper: parsing, pagination, dedup and output schema."""

import datetime as dt
import pathlib

import pyarrow.parquet as pq
import pytest

from conftest import FakeResponse, fixture_html
from src import cache
from src import scrape_listings as sl

WTM = sl.CATEGORIES["WTM"]
AO = sl.CATEGORIES["AO"]


@pytest.fixture
def wtm_page1():
    return fixture_html("listing_wtm_page1.html")


def make_row(order_id, date, title="T", url=None, order_type="WTM", page=1, **kw):
    return sl.ListingRow(
        order_id=order_id,
        order_date=date,
        order_date_raw="" if date is None else date.strftime("%b %d, %Y"),
        title=title,
        source_url=url or f"https://www.sebi.gov.in/x/y_{order_id}.html",
        order_type=order_type,
        listing_category="c",
        listing_page=page,
        listing_url=sl.LISTING_URL,
        smid=2,
        doc_kind="HTML_DETAIL",
        **kw,
    )


# --- request construction --------------------------------------------------

def test_category_to_order_type_mapping():
    assert {n: c.order_type for n, c in sl.CATEGORIES.items()} == {
        "WTM": "WTM", "SETTLEMENT": "SETTLEMENT", "AO": "AO", "ED_CGM": "ED_CGM",
    }
    assert {c.smid for c in sl.CATEGORIES.values()} == {2, 3, 6, 133}
    assert all(c.ss_text == "Orders" and c.s_text == "Enforcement"
               for c in sl.CATEGORIES.values())


def test_excluded_categories_are_absent():
    """SAT (1), Court (7), Special Courts (77) and Reg. 30A (138) stay out."""
    assert {c.smid for c in sl.CATEGORIES.values()}.isdisjoint({1, 7, 77, 138})


def test_build_form_uses_zero_based_dodirect():
    assert sl.build_form(WTM, 1)["doDirect"] == "0"
    assert sl.build_form(WTM, 3)["doDirect"] == "2"
    assert sl.build_form(WTM, 3)["nextValue"] == "3"
    assert sl.build_form(WTM, 1)["next"] == "n"


def test_build_form_rejects_page_zero():
    with pytest.raises(ValueError):
        sl.build_form(WTM, 0)


def test_headers_carry_no_session_state():
    headers = sl.build_headers(WTM)
    blob = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
    assert "cookie" not in {k.lower() for k in headers}
    assert "jsessionid" not in blob and "_ga" not in blob
    assert headers["Referer"].endswith("smid=2")


def test_pages_of_same_category_get_distinct_cache_keys():
    keys = {
        cache.cache_key(sl.LISTING_URL, method="POST", data=sl.build_form(WTM, p))
        for p in range(1, 6)
    }
    assert len(keys) == 5


def test_categories_get_distinct_cache_keys():
    keys = {
        cache.cache_key(sl.LISTING_URL, method="POST", data=sl.build_form(c, 1))
        for c in sl.CATEGORIES.values()
    }
    assert len(keys) == 4


# --- parsing ---------------------------------------------------------------

def test_parse_total_pages(wtm_page1):
    assert sl.parse_total_pages(wtm_page1) == 257


def test_parse_total_pages_missing_or_junk():
    assert sl.parse_total_pages("<div>no inputs here</div>") is None
    assert sl.parse_total_pages("<input type='hidden' name='totalpage' value='abc'/>") is None


@pytest.mark.parametrize(
    "markup",
    [
        "<input type='hidden' name='totalpage' value=257 />",  # as SEBI emits it
        "<input type='hidden' name='totalpage' value=257/>",   # '/' glued to the value
        "<input type='hidden' name='totalpage' value='257'/>",
        '<input type="hidden" name="totalpage" value=" 257 ">',
    ],
)
def test_parse_total_pages_tolerates_unquoted_attribute(markup):
    assert sl.parse_total_pages(markup) == 257


def test_parse_hidden_fields(wtm_page1):
    hidden = sl.parse_hidden_fields(wtm_page1)
    assert hidden["totalpage"] == "257"
    assert hidden["nextDel"] == "25"


def test_parse_rows_returns_one_row_per_order(wtm_page1):
    rows = sl.parse_rows(wtm_page1, WTM, 1)
    assert len(rows) == 25, "25 orders per page, excluding the 11B spreadsheet block"


def test_11b_spreadsheet_block_is_excluded(wtm_page1):
    """table.ordersum repeats seven Order-11B-<year>.xlsx links on every WTM page."""
    assert "Order-11B-2020.xlsx" in wtm_page1
    rows = sl.parse_rows(wtm_page1, WTM, 1)
    assert not any(".xlsx" in r.source_url for r in rows)


def test_parse_normal_row_fields(wtm_page1):
    row = sl.parse_rows(wtm_page1, WTM, 1)[0]
    assert row.order_date == dt.date(2026, 9, 17)
    assert row.order_date_raw == "Sep 17, 2026"
    assert row.title == "Interim Order in the matter of Kore Digital Limited"
    assert row.source_url.endswith("_104570.html")
    assert row.order_id == 104570
    assert row.order_type == "WTM"
    assert row.listing_page == 1
    assert row.doc_kind == "HTML_DETAIL"
    assert row.needs_review is False


def test_order_type_comes_from_category_not_title(wtm_page1):
    """An 'Adjudication Order' scraped under WTM would still be typed WTM."""
    rows = sl.parse_rows(wtm_page1, AO, 1)
    assert {r.order_type for r in rows} == {"AO"}


def test_direct_pdf_rows_are_flagged_not_invented(wtm_page1):
    rows = sl.parse_rows(wtm_page1, WTM, 1)
    pdfs = [r for r in rows if r.doc_kind == "DIRECT_PDF"]
    assert pdfs, "WTM page 1 contains exemption orders linked straight to PDFs"
    for row in pdfs:
        assert row.order_id is None
        assert row.needs_review is True
        assert "no_order_id_in_url" in row.review_reason
        assert row.title and row.order_date is not None


def test_corrigendum_link_does_not_become_a_row():
    ao = fixture_html("listing_ao_page1.html")
    rows = sl.parse_rows(ao, AO, 1)
    assert len(rows) == 25
    assert "[Corrigendum]" in ao
    assert not any(r.title == "[Corrigendum]" for r in rows)
    with_corr = [r for r in rows if r.corrigendum_url]
    assert with_corr and all(r.order_id is not None for r in with_corr)


def test_titles_are_whitespace_normalised(wtm_page1):
    for row in sl.parse_rows(wtm_page1, WTM, 1):
        assert row.title == " ".join(row.title.split())
        assert row.title


# --- order_id extraction ---------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.sebi.gov.in/enforcement/orders/feb-2026/x_99913.html", 99913),
        ("https://www.sebi.gov.in/enforcement/orders/feb-2026/x_1.html", 1),
        ("https://www.sebi.gov.in/a/b_80299.htm", 80299),
        ("https://www.sebi.gov.in/a/b_80299.html?x=1#frag", 80299),
        ("/enforcement/orders/jan-2020/some-order_12345.html", 12345),
    ],
)
def test_extract_order_id_from_normal_urls(url, expected):
    assert sl.extract_order_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        None, "", "   ",
        "https://www.sebi.gov.in/sebi_data/attachdocs/feb-2026/exemption_order.pdf",
        "https://www.sebi.gov.in/enforcement/orders/Order-11B-2020.xlsx",
        "https://www.sebi.gov.in/enforcement/orders/no-id-here.html",
        "https://www.sebi.gov.in/a/b_12345_extra.html",
        "not a url at all",
        "javascript:void(0)",
    ],
)
def test_malformed_urls_yield_no_id(url):
    assert sl.extract_order_id(url) is None


# --- date parsing ----------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Feb 23, 2026", dt.date(2026, 2, 23)),
        ("Sep 17, 2026", dt.date(2026, 9, 17)),
        ("  Jan 05, 2013 ", dt.date(2013, 1, 5)),
        ("February 23, 2026", dt.date(2026, 2, 23)),
        ("23 Feb 2026", dt.date(2026, 2, 23)),
    ],
)
def test_parse_order_date(text, expected):
    assert sl.parse_order_date(text) == expected


@pytest.mark.parametrize("text", [None, "", "2020", "not a date", "Feb 30, 2026"])
def test_parse_order_date_rejects_junk(text):
    assert sl.parse_order_date(text) is None


# --- degenerate HTML -------------------------------------------------------

def test_empty_table_yields_no_rows():
    html = """<input type='hidden' name='totalpage' value=18/>
        <table id='sample_1'><thead><tr><th>Date</th><th>Title</th></tr></thead></table>"""
    assert sl.parse_rows(html, WTM, 1) == []
    assert sl.parse_total_pages(html) == 18


@pytest.mark.parametrize(
    "html",
    [
        "", "   ", "<html></html>", "not html at all",
        "<table id='sample_1'><tr><td>Feb 23, 2026</td></tr></table>",       # no anchor
        "<table id='sample_1'><tr><td><a href=''>x</a></td></tr></table>",   # empty href
        "<table id='sample_1'><tr><td>a</td><td><b>no link</b></td></tr></table>",
        "<div class='main_full'><p>server error</p></div>",
    ],
)
def test_malformed_html_does_not_raise(html):
    assert sl.parse_rows(html, WTM, 1) == []


def test_row_with_unparseable_date_is_kept_and_flagged():
    html = """<table id='sample_1'><tr><td>garbage date</td>
        <td><a href='https://www.sebi.gov.in/a/b_555.html' title='Some Order'>Some Order</a></td>
        </tr></table>"""
    row = sl.parse_rows(html, WTM, 1)[0]
    assert row.order_id == 555
    assert row.order_date is None
    assert row.order_date_raw == "garbage date"
    assert row.needs_review and "unparsed_date" in row.review_reason


# --- pagination ------------------------------------------------------------

def _page_html(total_pages, first_id):
    rows = "".join(
        f"<tr><td>Feb {i + 1:02d}, 2026</td>"
        f"<td><a href='https://www.sebi.gov.in/a/o_{first_id + i}.html' "
        f"title='Order {first_id + i}'>Order {first_id + i}</a></td></tr>"
        for i in range(25)
    )
    return (f"<input type='hidden' name='totalpage' value={total_pages} />"
            f"<table id='sample_1'><thead><tr><th>Date</th><th>Title</th></tr></thead>{rows}</table>")


def test_pagination_stops_at_totalpage(monkeypatch, tmp_cache):
    requested = []

    def fake_fetch(category, page, force=False):
        requested.append(page)
        return _page_html(3, 1000 + page * 100)

    monkeypatch.setattr(sl, "fetch_listing_page", fake_fetch)
    result = sl.scrape_category(WTM)
    assert requested == [1, 2, 3], "must stop at totalpage, not run on"
    assert result.total_pages == 3
    assert len(result.rows) == 75


def test_start_and_end_page_limit_the_walk(monkeypatch, tmp_cache):
    requested = []

    def fake_fetch(category, page, force=False):
        requested.append(page)
        return _page_html(257, 1000 + page * 100)

    monkeypatch.setattr(sl, "fetch_listing_page", fake_fetch)
    result = sl.scrape_category(WTM, start_page=1, end_page=3)
    assert requested == [1, 2, 3]
    assert result.total_pages == 257
    assert len(result.rows) == 75


def test_end_page_beyond_totalpage_is_clamped(monkeypatch, tmp_cache):
    requested = []
    monkeypatch.setattr(
        sl, "fetch_listing_page",
        lambda c, page, force=False: requested.append(page) or _page_html(2, page * 100),
    )
    sl.scrape_category(WTM, end_page=99)
    assert requested == [1, 2]


def test_failed_page_is_logged_and_the_walk_continues(monkeypatch, tmp_cache, caplog):
    def fake_fetch(category, page, force=False):
        if page == 2:
            raise cache.FetchError("HTTP 503 after 3 attempts")
        return _page_html(3, 1000 + page * 100)

    monkeypatch.setattr(sl, "fetch_listing_page", fake_fetch)
    with caplog.at_level("ERROR"):
        result = sl.scrape_category(WTM)

    assert len(result.rows) == 50, "pages 1 and 3 still collected"
    assert [p.page for p in result.failed_pages] == [2]
    assert result.failed_pages[0].status == "failed"
    assert "503" in result.failed_pages[0].error
    assert any("FAILED" in r.message or "FAILED" in r.getMessage() for r in caplog.records)


def test_missing_totalpage_does_not_loop_forever(monkeypatch, tmp_cache):
    requested = []

    def fake_fetch(category, page, force=False):
        requested.append(page)
        return "<table id='sample_1'></table>"  # no totalpage, no rows

    monkeypatch.setattr(sl, "fetch_listing_page", fake_fetch)
    sl.scrape_category(WTM)
    assert requested == [1]


def test_unreadable_totalpage_is_capped_not_infinite(monkeypatch, tmp_cache):
    """Rows keep coming but totalpage is junk: the walk must still terminate."""
    requested = []

    def fake_fetch(category, page, force=False):
        requested.append(page)
        return _page_html(1, 1000).replace("value=1 ", "value=junk ")

    monkeypatch.setattr(sl, "MAX_PAGES", 5)
    monkeypatch.setattr(sl, "fetch_listing_page", fake_fetch)
    result = sl.scrape_category(WTM)
    assert result.total_pages is None
    assert requested == [1, 2, 3, 4, 5]


def test_walk_stops_on_first_empty_page_when_totalpage_unknown(monkeypatch, tmp_cache):
    requested = []

    def fake_fetch(category, page, force=False):
        requested.append(page)
        body = _page_html(1, 1000) if page <= 2 else "<table id='sample_1'></table>"
        return body.replace("value=1 ", "value=junk ")

    monkeypatch.setattr(sl, "fetch_listing_page", fake_fetch)
    sl.scrape_category(WTM)
    assert requested == [1, 2, 3]


def test_rerun_over_cached_pages_makes_no_network_call(tmp_cache, fake_http, monkeypatch):
    """The whole point of the cache layer: parsing again must not hit SEBI."""
    fake_http.queue(*[FakeResponse(_page_html(2, 100 * p).encode()) for p in (1, 2)])
    first = sl.scrape_category(WTM)
    assert cache.NETWORK_CALLS == 2

    cache.reset_stats()
    cache.set_offline(True)
    try:
        second = sl.scrape_category(WTM)
    finally:
        cache.set_offline(False)

    assert cache.NETWORK_CALLS == 0
    assert len(fake_http.calls) == 2
    assert [r.order_id for r in second.rows] == [r.order_id for r in first.rows]
    assert all(p.from_cache for p in second.pages)


# --- deduplication ---------------------------------------------------------

def test_deduplicate_by_order_id():
    d = dt.date(2026, 2, 23)
    rows = [make_row(1, d), make_row(2, d), make_row(1, d)]
    unique, conflicts = sl.deduplicate(rows)
    assert sorted(r.order_id for r in unique) == [1, 2]
    assert conflicts == []


def test_identical_duplicate_on_same_page_is_not_a_conflict():
    d = dt.date(2026, 2, 23)
    unique, conflicts = sl.deduplicate([make_row(7, d), make_row(7, d)])
    assert len(unique) == 1 and conflicts == []


def test_conflicting_duplicate_is_reported_not_silently_dropped():
    unique, conflicts = sl.deduplicate([
        make_row(9, dt.date(2026, 2, 23), title="Order A"),
        make_row(9, dt.date(2020, 1, 1), title="Order B"),
    ])
    assert len(unique) == 1
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["key"] == "9"
    assert "title" in c["fields"] and "order_date" in c["fields"]
    assert c["kept_title"] == "Order A" and c["other_title"] == "Order B"


def test_cross_category_duplicate_is_reported():
    d = dt.date(2026, 2, 23)
    unique, conflicts = sl.deduplicate([
        make_row(5, d, order_type="WTM"), make_row(5, d, order_type="AO"),
    ])
    assert len(unique) == 1
    assert conflicts[0]["kept_order_type"] == "WTM"
    assert conflicts[0]["other_order_type"] == "AO"


def test_rows_without_ids_dedupe_by_url_and_are_not_merged():
    d = dt.date(2026, 2, 23)
    a = make_row(None, d, url="https://sebi.gov.in/a.pdf", title="A")
    b = make_row(None, d, url="https://sebi.gov.in/b.pdf", title="B")
    unique, conflicts = sl.deduplicate([a, b, a])
    assert len(unique) == 2, "different PDFs must not collapse into one row"
    assert conflicts == []


def test_sort_is_deterministic():
    rows = [
        make_row(3, dt.date(2020, 1, 1), order_type="AO"),
        make_row(1, dt.date(2026, 5, 5), order_type="WTM"),
        make_row(2, dt.date(2026, 5, 5), order_type="WTM"),
        make_row(None, None, url="https://x/z.pdf", order_type="WTM"),
    ]
    once = [r.order_id for r in sl.sort_rows(rows)]
    twice = [r.order_id for r in sl.sort_rows(list(reversed(rows)))]
    assert once == twice == [3, 1, 2, None]


# --- output ----------------------------------------------------------------

def test_parquet_schema_and_roundtrip(tmp_path):
    rows = sl.sort_rows([
        make_row(1, dt.date(2026, 5, 5)),
        make_row(None, dt.date(2026, 5, 4), url="https://x/y.pdf",
                 needs_review=True, review_reason="no_order_id_in_url"),
    ])
    rows[0].scraped_at = dt.datetime(2026, 9, 23, 10, 0, tzinfo=dt.timezone.utc)
    out = sl.write_parquet(rows, tmp_path / "listings.parquet")

    table = pq.read_table(out)
    assert table.schema.equals(sl.PARQUET_SCHEMA)
    assert table.num_rows == 2

    required = {"order_id", "order_date", "title", "source_url", "order_type",
                "listing_category", "listing_page", "listing_url", "scraped_at"}
    assert required.issubset(set(table.column_names))

    d = table.to_pydict()
    assert d["order_id"] == [1, None]
    assert d["order_date"][0] == dt.date(2026, 5, 5)
    assert d["order_type"] == ["WTM", "WTM"]
    assert d["needs_review"] == [False, True]


def test_write_parquet_handles_empty_input(tmp_path):
    out = sl.write_parquet([], tmp_path / "empty.parquet")
    table = pq.read_table(out)
    assert table.num_rows == 0
    assert table.schema.equals(sl.PARQUET_SCHEMA)


# --- CLI / paths -----------------------------------------------------------

def test_cli_parses_development_invocation():
    args = sl.build_arg_parser().parse_args(
        ["--category", "WTM", "--start-page", "1", "--end-page", "3"]
    )
    assert args.category == ["WTM"] and args.start_page == 1 and args.end_page == 3


def test_cli_defaults_are_safe_and_resumable():
    args = sl.build_arg_parser().parse_args([])
    assert args.category is None  # -> all four
    assert args.force is False    # -> reuse the cache
    assert args.start_page == 1 and args.end_page is None


def test_cli_rejects_unknown_category(capsys):
    with pytest.raises(SystemExit):
        sl.build_arg_parser().parse_args(["--category", "SAT"])


def test_output_paths_are_wsl_native():
    for path in (sl.DEFAULT_OUT, sl.REPORTS_DIR, sl.LOGS_DIR, sl.REPO_ROOT):
        text = str(path)
        assert isinstance(path, pathlib.Path)
        assert text.startswith("/") and not text.startswith("/mnt/")
        assert "\\" not in text and ":" not in text
    assert sl.DEFAULT_OUT.is_relative_to(sl.REPO_ROOT)
