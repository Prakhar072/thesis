"""Tests for the shareholding-pattern parser (Week 4 Day 5). No network."""

import datetime as dt
import json

import pandas as pd

from src import fetch_shareholding as fs

D = dt.date


def _summary(quarter, header, promoter, total, pledge_answer="No"):
    """A summary shaped like BSE's: a quarter heading, the Yes/No table, the header with colspans, the rows."""
    th = "".join(f"<td colspan='{span}'>{name}</td>" if span > 1 else f"<td rowspan='3'>{name}</td>"
                 for name, span in header)
    row = lambda cells: "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"  # noqa: E731
    html = (f"<table><tr><td><table><tr><td colspan='4'><b>Quarter ending :{quarter}</b></td></tr></table></td></tr>"
            f"<tr><td>Whether any shares held by promoters are pledge or otherwise encumbered?</td>"
            f"<td>{pledge_answer}</td></tr>"
            f"<tr>{th}</tr><tr><td>No.(a)</td><td>As a %</td></tr>"
            + row(promoter) + row(total) + "</table>")
    return json.dumps({"Data": html}).encode()


H2019 = [("Category of shareholder", 1), ("No. of shareholders", 1), ("No. of fully paid up equity shares held", 1),
         ("Total no. shares held", 1), ("Shareholding as a % of total no. of shares (calculated as per SCRR, 1957)", 1),
         ("No. of Voting Rights", 1), ("Total as a % of Total Voting right", 1),
         ("No. of Shares pledged or otherwise encumbered", 2), ("Total Number of Shares encumbered", 2),
         ("No. of equity shares held in dematerialized form", 1)]


def test_parse_with_a_pledge_column():
    b = _summary("June 2019", H2019,
                 ["(A) Promoter & Promoter Group", "65", "94,65,82,910", "94,65,82,910", "38.91", "94,65,82,910",
                  "38.91", "18,93,39,382", "20.00", "", "0.00", "94,65,82,910"],
                 ["Grand Total", "5,81,010", "2,43,24,56,975", "2,43,24,56,975", "100.00", "2,43,24,56,975", "100.00",
                  "18,93,39,382", "7.78", "", "0.00", "2,41,33,41,074"], pledge_answer="Yes")
    r = fs.parse_summary(b)
    assert r["status"] == "ok" and r["quarter_label"] == "June 2019"
    assert (r["promoter_pct"], r["promoter_shares"], r["total_shares"]) == (38.91, 946582910.0, 2432456975.0)
    assert (r["pledged_shares"], r["pledged_pct"], r["pledge_answer"]) == (189339382.0, 20.0, "Yes")


def test_no_pledge_column_and_answer_no_means_zero():
    header = [h for h in H2019 if "encumbered" not in h[0]]
    b = _summary("December 2015", header,
                 ["(A) Promoter & Promoter Group", "58", "1,46,39,61,977", "1,46,39,61,977", "46.66", "x", "47.73", "y"],
                 ["Grand Total", "1", "3,23,92,12,335", "3,23,92,12,335", "100.00", "x", "100", "y"])
    r = fs.parse_summary(b)
    assert (r["promoter_pct"], r["pledged_shares"], r["pledged_pct"]) == (46.66, 0.0, 0.0)


def test_no_pledge_column_and_answer_yes_is_missing():
    header = [h for h in H2019 if "encumbered" not in h[0]]
    b = _summary("March 2017", header,
                 ["(A) Promoter & Promoter Group", "5", "100", "100", "60.00", "100", "60", "100"],
                 ["Grand Total", "9", "1,000", "1,000", "100.00", "1,000", "100", "1,000"], pledge_answer="Yes")
    r = fs.parse_summary(b)
    assert r["promoter_pct"] == 60.0 and r["pledged_shares"] is None


def test_a_quarter_before_the_current_format_is_empty():
    html = ("<table><tr><td><b>Quarter ending :December 2013</b></td></tr><tr><td>"
            "<b>Summary statement holding of specified securities</b></td></tr></table>")
    r = fs.parse_summary(json.dumps({"Data": html}).encode())
    assert (r["status"], r["quarter_label"], r["promoter_pct"]) == ("empty", "December 2013", None)


def test_quarter_ids():
    assert fs.qtrid(D(2008, 12, 31)) == "60.00" and fs.qtrid(D(2023, 12, 31)) == "120.00"
    assert fs.qtrid(D(2026, 6, 30)) == "130.00" and fs.qtrid(D(2015, 12, 31)) == "88.00"


def test_quarter_ends_march_only_by_default():
    assert fs.quarter_ends(D(2015, 12, 31), D(2018, 1, 1), False) == [D(2016, 3, 31), D(2017, 3, 31)]
    assert len(fs.quarter_ends(D(2015, 12, 31), D(2016, 12, 31), True)) == 5


def test_firm_codes_prefer_the_issuers_own_code():
    s = pd.DataFrame({"firm_id": ["INE001A", "INE002B", "NSE:X"], "role": ["fraud", "control", "control"],
                      "status": "Active", "bse_codes": ['["500001", "890001"]', '["500002"]', "[]"],
                      "issuer_ticker": ["890001", None, None]})
    f = fs.firm_codes(s)
    assert f.bse_code.tolist() == ["890001", "500002", None]


def test_a_delisted_firm_stops_after_two_empty_quarters(monkeypatch):
    seq = iter(["ok", "ok", "empty", "empty", "ok"])
    calls = []

    def fake_get(url, params=None, **_):
        calls.append(params["qtrid"])
        s = next(seq)
        if s == "ok":
            return _summary("March 2016", H2019, ["(A) Promoter & Promoter Group", "1", "1", "1", "50.00", "1", "50",
                                                  "0", "0.00", "", "0.00", "1"],
                            ["Grand Total", "1", "2", "2", "100", "2", "100", "0", "0", "", "0", "2"])
        return json.dumps({"Data": "<table><tr><td>Quarter ending :March 2019</td></tr></table>"}).encode()

    monkeypatch.setattr(fs.cache, "get", fake_get)
    quarters = fs.quarter_ends(D(2015, 12, 31), D(2021, 6, 30), False)
    rows = fs.fetch_firm({"firm_id": "INE001A", "bse_code": "500001", "status": "Delisted"}, quarters, offline=False)
    assert [r["status"] for r in rows] == ["ok", "ok", "empty", "empty"] and len(calls) == 4


def test_a_delisted_firm_with_nothing_since_2015_stops_after_two(monkeypatch):
    calls = []
    monkeypatch.setattr(fs.cache, "get", lambda url, params=None, **_: calls.append(1) or json.dumps(
        {"Data": "<table><tr><td>Quarter ending :March 2016</td></tr></table>"}).encode())
    quarters = fs.quarter_ends(D(2015, 12, 31), D(2026, 6, 30), False)
    rows = fs.fetch_firm({"firm_id": "INE001A", "bse_code": "500001", "status": "Delisted"}, quarters, offline=False)
    assert len(rows) == len(calls) == 2


def test_quarters_are_limited_to_the_trading_span(tmp_path):
    pd.DataFrame({"code": ["500001", "500001", "500002"], "date": [D(2017, 5, 2), D(2019, 1, 7), D(2014, 3, 3)]}) \
        .to_parquet(tmp_path / "BSE_2017.parquet")
    spans = fs.trading_spans(tmp_path, 2015)
    quarters = fs.quarter_ends(D(2015, 12, 31), D(2022, 6, 30), False)
    # a quarter of margin either side: listed but untraded, or the last filing after the last trade
    assert fs.in_span(quarters, spans.get("500001")) == [D(2017, 3, 31), D(2018, 3, 31), D(2019, 3, 31)]
    assert fs.in_span(quarters, spans.get("999999")) == []
