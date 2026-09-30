"""Tests for the Day 3 sanity rules, the review sheet and the extraction rate."""

import datetime as dt

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src import check_windows as cw
from src import window_model as wm

D = dt.date


@pytest.mark.parametrize("end, precision, expected", [
    (D(2023, 3, 31), "month", D(2023, 3, 1)),
    (D(2014, 12, 31), "year", D(2014, 12, 31)),
    (D(2022, 3, 31), "fy", D(2022, 3, 31)),
    (D(2012, 3, 31), "day", D(2012, 3, 31)),
])
def test_earliest_end(end, precision, expected):
    assert cw.earliest_end(end, precision) == expected


@pytest.mark.parametrize("start, end, prec, order, flags", [
    (D(2010, 1, 1), D(2010, 6, 30), "day", D(2014, 1, 1), []),
    # R1: ends after the order (37319: a registration validity period)
    (D(2014, 2, 17), D(2019, 2, 16), "day", D(2018, 1, 8), ["end_after_order"]),
    # month-precision end in the order's month is consistent (68592)
    (D(2018, 3, 1), D(2023, 3, 31), "month", D(2023, 3, 1), ["lag_not_positive"]),
    (D(2018, 3, 1), D(2023, 3, 31), "month", D(2023, 3, 2), []),
    # a financial year still running at the order date is flagged (57262)
    (D(2021, 4, 1), D(2022, 3, 31), "fy", D(2022, 3, 28), ["end_after_order"]),
    # R2
    (D(2012, 1, 1), D(2011, 1, 1), "day", D(2014, 1, 1), ["start_after_end"]),
    # R3: equal dates, and 20 years
    (D(2010, 1, 1), D(2010, 6, 30), "day", D(2010, 6, 30), ["lag_not_positive"]),
    (D(2000, 9, 1), D(2000, 12, 31), "day", D(2022, 9, 5), ["lag_20_years"]),
    # R4
    (D(1994, 8, 1), D(1994, 12, 31), "day", D(2010, 4, 26), ["start_before_1995"]),
])
def test_rules(start, end, prec, order, flags):
    assert cw.flags_for(start, end, prec, order) == flags


def test_severity():
    assert cw.severity(["start_before_1995"]) == cw.IMPLAUSIBLE
    assert cw.severity(["lag_20_years", "end_after_order"]) == cw.IMPOSSIBLE
    assert cw.severity([]) is None


def _windows():
    base = dict(order_type="AO", text_status="OK", window_status="PARSED", model_status=None,
                start_precision="day", end_precision="day", violation_raw="sentence",
                violation_confidence="high", violation_method="regex", violation_rule="range",
                window_kind="stated_period", char_start=1)
    rows = [
        {**base, "text_stem": "ok", "order_id": 1, "order_date": D(2014, 1, 1),
         "violation_start": D(2010, 1, 1), "violation_end": D(2010, 6, 30)},
        {**base, "text_stem": "late", "order_id": 2, "order_date": D(2018, 1, 8),
         "violation_start": D(2014, 2, 17), "violation_end": D(2019, 2, 16)},
        {**base, "text_stem": "old", "order_id": 3, "order_date": D(2003, 4, 3),
         "violation_start": D(1994, 10, 1), "violation_end": D(1995, 12, 30)},
        {**base, "text_stem": "none", "order_id": 4, "order_date": D(2014, 1, 1),
         "violation_start": None, "violation_end": None, "violation_raw": None,
         "violation_confidence": "low", "violation_method": None, "window_kind": None,
         "window_status": "NO_CANDIDATES"},
    ]
    return pd.DataFrame(rows)


def test_flagged_windows_leave_the_dataset_but_keep_their_dates():
    c = cw.check(_windows(), {}).set_index("text_stem")
    assert c.loc["ok", "sanity_status"] == cw.PASS and c.loc["ok", "violation_start"] == D(2010, 1, 1)
    assert c.loc["late", "sanity_status"] == cw.FLAGGED
    assert c.loc["late", "violation_start"] is None
    assert c.loc["late", "flagged_end"] == D(2019, 2, 16)
    assert c.loc["late", "sanity_severity"] == cw.IMPOSSIBLE
    assert c.loc["none", "sanity_status"] == cw.NO_WINDOW


def test_review_decisions_are_applied():
    decided = {"old": (cw.KEEP, None, None, "1994-95 price rigging, as stated"),
               "late": (cw.FIX, D(2014, 2, 17), D(2016, 3, 31), "corrected by hand")}
    c = cw.check(_windows(), decided).set_index("text_stem")
    assert c.loc["old", "sanity_status"] == "REVIEWED_KEEP"
    assert c.loc["old", "violation_start"] == D(1994, 10, 1)
    assert c.loc["late", "sanity_status"] == "REVIEWED_FIX"
    assert (c.loc["late", "violation_end"], c.loc["late", "violation_method"]) == (D(2016, 3, 31), "hand")
    dropped = cw.check(_windows(), {"old": (cw.DROP, None, None, "")}).set_index("text_stem")
    assert dropped.loc["old", "violation_start"] is None


def test_invalid_decisions_are_reported_not_applied():
    review = pd.DataFrame({"text_stem": ["a", "b", "c"], "decision": ["maybe", "fix", "keep"],
                           "violation_start": ["", "2014-01-01", ""], "violation_end": ["", "", ""],
                           "note": ["", "", ""]})
    decided, problems = cw.decisions(review)
    assert set(decided) == {"c"} and len(problems) == 2


def test_review_sheet_only_appends():
    flagged = cw.check(_windows(), {})
    flagged = flagged[flagged.sanity_flags.notna()]
    review = cw.update_review(cw.read_review(pd.io.common.Path("/nonexistent")), flagged)
    assert set(review.text_stem) == {"late", "old"}
    review.loc[review.text_stem == "old", "decision"] = "keep"
    again = cw.update_review(review, flagged)
    assert len(again) == 2 and again.set_index("text_stem").loc["old", "decision"] == "keep"


def test_extraction_rate_and_verdict():
    c = cw.check(_windows(), {})
    table, summary = cw.extraction_rate(c, pd.Series(False, index=c.index))
    assert summary["rate"] == pytest.approx(1 / 4)
    assert summary["verdict"].startswith("below 50%")
    assert table.set_index(["by", "group"]).loc[("all", "all"), "orders"] == 4


def test_main_end_to_end_keeps_decisions_across_runs(tmp_path, monkeypatch):
    windows = tmp_path / "violation_windows.parquet"
    pq.write_table(pa.Table.from_pandas(_windows(), schema=wm.COMBINED_SCHEMA, preserve_index=False), windows)
    monkeypatch.setattr(cw, "iso_flags", lambda w, *a, **k: pd.Series(False, index=w.index))
    out = tmp_path / "out"
    assert cw.main(["--windows", str(windows), "--scratch", str(out)]) == 0
    flagged = pd.read_csv(out / "window_flagged.csv", dtype=str)
    assert set(flagged.text_stem) == {"late", "old"}
    review = pd.read_csv(out / "window_review.csv", dtype=str, keep_default_na=False)
    review.loc[review.text_stem == "old", "decision"] = "keep"
    review.to_csv(out / "window_review.csv", index=False)

    assert cw.main(["--windows", str(windows), "--scratch", str(out)]) == 0
    checked = pd.read_parquet(out / "windows_checked.parquet").set_index("text_stem")
    assert checked.loc["old", "sanity_status"] == "REVIEWED_KEEP"
    assert checked.loc["late", "sanity_status"] == cw.FLAGGED
    kept = pd.read_csv(out / "window_review.csv", dtype=str, keep_default_na=False).set_index("text_stem")
    assert kept.loc["old", "decision"] == "keep" and len(kept) == 2
    assert (out / "extraction_rate.csv").exists()


@pytest.mark.parametrize("text, expected", [
    ("2002-01-01", D(2002, 1, 1)),
    ("01-06-1994", D(1994, 6, 1)),      # as a spreadsheet re-saves it: day first
    ("31/03/1996", D(1996, 3, 31)),
    ("31-02-1996", None),
    ("", None),
    ("June 1994", None),
])
def test_review_dates_accept_spreadsheet_format(text, expected):
    assert cw._date(text) == expected


def test_review_sheet_is_written_back_in_iso_format():
    review = pd.DataFrame([{c: "" for c in cw.REVIEW_COLUMNS}])
    review.loc[0, ["text_stem", "decision", "violation_start", "violation_end", "order_date"]] = [
        "16659", "fix", "01-06-1994", "31-03-1996", "10-04-2003"]
    empty = cw.check(_windows(), {}).iloc[0:0]
    out = cw.update_review(review, empty)
    assert out.loc[0, ["violation_start", "violation_end", "order_date"]].tolist() == [
        "1994-06-01", "1996-03-31", "2003-04-10"]
    decided, problems = cw.decisions(out)
    assert decided["16659"][1:3] == (D(1994, 6, 1), D(1996, 3, 31)) and not problems


def test_a_backwards_fix_is_refused():
    review = pd.DataFrame({"text_stem": ["57126"], "decision": ["fix"], "violation_start": ["01-01-2022"],
                           "violation_end": ["31-12-2002"], "note": [""]})
    decided, problems = cw.decisions(review)
    assert not decided and "2022" in problems[0]


def test_a_window_out_of_the_dataset_keeps_no_descriptors():
    # AUDIT_REPORT.md M-9: dropped rows kept violation_confidence = "high" with no dates
    import datetime as dt
    import pandas as pd
    from src import check_windows as cw
    w = pd.DataFrame([{"text_stem": "1", "order_date": dt.date(2005, 1, 1), "violation_start": dt.date(1990, 1, 1),
                       "violation_end": dt.date(1991, 1, 1), "start_precision": "day", "end_precision": "day",
                       "violation_raw": "…", "violation_confidence": "high", "violation_method": "regex",
                       "violation_rule": "range", "window_kind": "stated_period", "char_start": 10}])
    for decided in ({}, {"1": ("drop", None, None, "")}):
        r = cw.check(w, decided).iloc[0]
        assert r.violation_start is None and r.violation_raw is None and r.violation_method is None
        assert r.window_kind is None and r.violation_confidence == "low"
        assert r.flagged_start == dt.date(1990, 1, 1)
