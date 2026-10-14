"""Tests for the Week 3 Day 5 scorer."""

import datetime as dt
import json
import math

import numpy as np
import pandas as pd
import pytest

from src import score_validation as sv
from src import validation_sample as vs

D = dt.date


def _orders(n=6):
    return pd.DataFrame({
        "text_stem": [str(i) for i in range(1, n + 1)], "order_id": range(1, n + 1),
        "order_date": [D(2012, 6, 30)] * n, "order_type": ["AO", "WTM"] * (n // 2),
        "violation_start": [D(2010, 1, 1)] * n, "violation_end": [D(2010, 12, 31)] * n,
        "window_kind": "stated_period", "violation_method": "regex", "violation_rule": "range",
        "violation_confidence": "high", "violation_raw": "investigation period 2010",
        "fraud_category": "MARKET_MANIPULATION", "fraud_category_confidence": "high",
        "fraud_category_rule": "pfutp:narrative", "fraud_category_raw": "synchronised trades",
        "detection_channel": "SURVEILLANCE", "detection_detail": "alert", "detection_method": "rule",
        "detection_confidence": "high", "detection_raw": "on receipt of an alert",
        "issuer_guess": "Kavveri Telecom Products Limited", "issuer_source": "matter",
        "matter_raw": "In the matter of Kavveri Telecom Products Ltd",
    })


def _key(orders, weight=10.0):
    return pd.DataFrame({"text_stem": orders.text_stem, "stratum": vs.REGEX, "iso": False, "weight": weight})


def _sheet(orders):
    """A sheet coded exactly as the pipeline answered."""
    return pd.DataFrame({
        "sample_no": [str(i) for i in range(1, len(orders) + 1)], "text_stem": orders.text_stem,
        "order_date": orders.order_date.astype(str),
        "violation_start": orders.violation_start.astype(str), "violation_end": orders.violation_end.astype(str),
        "window_kind": orders.window_kind, "fraud_category": orders.fraud_category,
        "detection_channel": orders.detection_channel, "detection_detail": orders.detection_detail,
        "primary_issuer": orders.issuer_guess, "notes": "",
    })


def _scored(sheet, orders, key=None):
    return sv.score_rows(sv.load(sheet, _key(orders) if key is None else key, orders))


# --- names ----------------------------------------------------------------------------------------------------

def test_issuer_names_match_through_the_week4_normaliser():
    assert sv.issuer_match("Kavveri Telecom Products Ltd.", "KAVVERI TELECOM PRODUCTS LIMITED")
    assert sv.issuer_match("M/s. Kavveri Telecom Products Pvt Ltd", "Kavveri Telecom Products Private Limited")
    assert sv.issuer_match("Products Telecom Kavveri Ltd", "Kavveri Telecom Products Limited")  # token sort
    assert not sv.issuer_match("Kavveri Telecom Products Ltd", "Shubh Laxmi Projects Ltd")


def test_former_names_count_and_none_means_no_issuer():
    assert sv.issuer_match("New Name Ltd (formerly Old Name Ltd)", "Old Name Limited")
    assert sv.issuer_match("NONE", None) and sv.issuer_match("none", "")
    assert not sv.issuer_match("NONE", "Kavveri Telecom Products Limited")
    assert not sv.issuer_match("Kavveri Telecom Products Ltd", None)


# --- windows --------------------------------------------------------------------------------------------------

def test_window_classes():
    m = (D(2010, 1, 1), D(2010, 12, 31))
    assert sv.window_fields("stated_period", *m, *m) | {} == {
        "presence": "both", "d_start": 0, "d_end": 0, "win_exact": True, "win_month": True}
    w = sv.window_fields("stated_period", D(2010, 1, 1), D(2011, 1, 20), *m)
    assert (w["d_end"], w["win_exact"], w["win_month"]) == (-20, False, True)
    w = sv.window_fields("conduct_dates", D(2009, 11, 1), D(2010, 12, 31), *m)
    assert (w["d_start"], w["win_exact"], w["win_month"]) == (61, False, False)
    assert sv.window_fields("none", None, None, *m)["presence"] == "spurious"
    assert sv.window_fields("none", None, None, None, None)["presence"] == "neither"
    assert sv.window_fields("stated_period", *m, None, None)["presence"] == "missed"
    assert sv.window_fields("", *m, *m)["presence"] is None  # not coded


def test_an_open_end_is_compared_on_the_stated_end_only():
    w = sv.window_fields("stated_period", D(2010, 1, 1), None, D(2010, 1, 1), D(2010, 12, 31))
    assert (w["d_start"], w["d_end"], w["win_exact"]) == (0, None, True)


def test_window_denominators():
    o = _orders()
    o.loc[[4, 5], ["violation_start", "violation_end"]] = None          # no pipeline window
    s = _sheet(o)
    s.loc[0, "violation_end"] = "2011-01-15"                            # within a month
    s.loc[1, ["violation_start", "violation_end", "window_kind"]] = ["", "", "none"]   # spurious
    s.loc[2, "violation_end"] = ""                                       # open end, start right
    s.loc[2, "notes"] = "till date"
    s.loc[4, ["violation_start", "violation_end", "window_kind"]] = ["2010-01-01", "2010-12-31", "stated_period"]
    s.loc[5, ["violation_start", "violation_end", "window_kind"]] = ["", "", "none"]   # neither
    r = _scored(s, o)
    assert r.presence.tolist() == ["both", "spurious", "both", "both", "missed", "neither"]
    assert r.win_exact_of_machine.tolist() == [False, False, True, True, pd.NA, pd.NA]
    assert r.win_month_of_machine.tolist() == [True, False, True, True, pd.NA, pd.NA]
    assert r.win_exact_of_hand.tolist() == [False, pd.NA, True, True, False, pd.NA]
    assert r.win_exact_of_both.tolist() == [False, pd.NA, True, True, pd.NA, pd.NA]
    assert r.win_present_agree.tolist() == [True, False, True, True, False, True]


# --- κ and rates ----------------------------------------------------------------------------------------------

def test_kappa_textbook_value():
    # 50 items: hand A/B × machine A/B = [[20, 5], [10, 15]] → po .70, pe .50, κ .40
    h = np.array([0] * 25 + [1] * 25)
    m = np.array([0] * 20 + [1] * 5 + [0] * 10 + [1] * 15)
    assert sv.wkappa(h, m, np.ones(50)) == pytest.approx(0.40)
    assert sv.wkappa(h, h, np.ones(50)) == 1.0


def test_weighted_kappa_equals_duplicating_rows():
    h, m = np.array([0, 0, 1, 1, 2]), np.array([0, 1, 1, 1, 2])
    w = np.array([1.0, 3.0, 1.0, 2.0, 1.0])
    dup = np.repeat(np.arange(5), w.astype(int))
    assert sv.wkappa(h, m, w) == pytest.approx(sv.wkappa(h[dup], m[dup], np.ones(len(dup))))


def test_kappa_ignores_uncoded_and_is_undefined_for_one_class():
    assert sv.wkappa(np.array([0, -1, 1]), np.array([0, 1, 1]), np.ones(3)) == 1.0
    assert math.isnan(sv.wkappa(np.array([0, 0]), np.array([0, 0]), np.ones(2)))


def test_weighted_rate_uses_the_cell_weights():
    o = _orders(4)
    s = _sheet(o)
    s.loc[0, "fraud_category"] = "INSIDER_TRADING"
    key = _key(o).assign(stratum=[vs.REGEX, vs.NO_WINDOW, vs.NO_WINDOW, vs.NO_WINDOW], weight=[100.0, 1, 1, 1])
    summary = sv.summarise(_scored(s, o, key), reps=0)
    row = summary[(summary.measure == "cat_agree") & (summary["by"] == "all")].iloc[0]
    assert (row.n, row.matches, row.rate) == (4, 3, 0.75)
    assert row.weighted == pytest.approx(3 / 103)


def test_bootstrap_interval_brackets_the_estimate():
    o = _orders(40)
    s = _sheet(o)
    s.loc[::4, "detection_channel"], s.loc[::4, "detection_detail"] = "COMPLAINT", "complaint"
    summary = sv.summarise(_scored(s, o), reps=200)
    row = summary[(summary.measure == "ch_agree") & (summary["by"] == "all")].iloc[0]
    assert row.ci_low <= row.weighted <= row.ci_high and row.ci_low < row.ci_high


def test_channel_variants():
    o = _orders(4)
    o.loc[1, "detection_detail"] = "trading_pattern_observed"
    o.loc[3, ["detection_channel", "detection_detail", "detection_method"]] = ["UNCLEAR", "none", "none"]
    s = _sheet(o)
    s.loc[1, ["detection_channel", "detection_detail"]] = ["UNCLEAR", "none"]    # hand: the pattern is no trigger
    s.loc[2, "detection_detail"] = "trading_pattern_observed"                    # same channel, other detail
    r = _scored(s, o)
    assert r.ch_agree.tolist() == [True, False, True, True]
    assert r.ch_stated_agree.tolist() == [True, False, True, True]
    assert r.h_ch_flag.tolist() == ["SURVEILLANCE", "UNCLEAR", "UNCLEAR", "UNCLEAR"]
    assert r.m_ch_flag.tolist() == ["SURVEILLANCE", "UNCLEAR", "SURVEILLANCE", "UNCLEAR"]
    assert r.ch_detail_agree.tolist() == [True, pd.NA, False, True]


def test_model_detail_is_not_scored():
    o = _orders(2)
    o.loc[0, ["detection_detail", "detection_method"]] = ["model", "model"]
    r = _scored(_sheet(o).assign(detection_detail="alert"), o)
    assert r.ch_detail_agree.tolist() == [pd.NA, True]


def test_errors_list_each_disagreement_with_its_evidence():
    o = _orders(4)
    s = _sheet(o)
    s.loc[0, "violation_start"] = "2009-12-01"
    s.loc[1, "fraud_category"] = "INSIDER_TRADING"
    s.loc[2, "primary_issuer"] = "NONE"
    s.loc[3, "window_kind"] = "conduct_dates"
    frame = sv.load(s, _key(o), o)
    e = sv.errors(sv.score_rows(frame), frame)
    assert e[["sample_no", "field", "error"]].values.tolist() == [
        [1, "window", "dates_within_month"], [2, "fraud_category", "INSIDER_TRADING → MARKET_MANIPULATION"],
        [3, "primary_issuer", "issuer"], [4, "window_kind", "kind"]]
    assert e.detail.iloc[0] == "pipeline − hand: start +31 d"
    assert e.machine_evidence.iloc[1] == "synchronised trades"


# --- Gate 1 ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rate, kappas, starts", [
    (0.45, {"c": 0.9, "d": 0.9}, "Fall back"),
    (0.58, {"c": 0.8, "d": 0.55}, "Protocol too vague (d"),
    (0.75, {"c": 0.8, "d": 0.72}, "Proceed as planned"),
    (0.58, {"c": 0.8, "d": 0.65}, "Proceed; report drop-out"),
    (0.75, {"c": 0.8, "d": 0.65}, "Proceed; report drop-out"),
    (0.58, {"c": math.nan, "d": math.nan}, "κ undefined"),
])
def test_gate1_reads_the_runbook_table(rate, kappas, starts):
    assert sv.gate1(rate, kappas).startswith(starts)


# --- command line ---------------------------------------------------------------------------------------------

@pytest.fixture
def scratch(tmp_path):
    o = _orders(8)
    o.loc[::2, "fraud_category"] = "DISCLOSURE_FAILURE"
    o.loc[::2, ["detection_channel", "detection_detail"]] = ["COMPLAINT", "complaint"]
    o.to_parquet(tmp_path / "orders.parquet", index=False)
    _key(o).to_parquet(tmp_path / "validation_key.parquet", index=False)
    _sheet(o).to_csv(tmp_path / "validation_sample.csv", index=False)
    pd.DataFrame({"by": ["all"], "group": ["all"], "orders_with_window": [58], "orders": [100],
                  "share": [0.58]}).to_csv(tmp_path / "extraction_rate.csv", index=False)
    return tmp_path


def _run(d, *extra):
    return sv.main(["--scratch", str(d), "--orders", str(d / "orders.parquet"),
                    "--extraction", str(d / "extraction_rate.csv"), "--reps", "0", *extra])


def test_main_writes_every_output(scratch):
    assert _run(scratch) == 0
    for name in ("scored.parquet", "errors.csv", "summary.csv", "table.md"):
        assert (scratch / f"validation_{name}").exists()
    table = (scratch / "validation_table.md").read_text()
    assert "| Fraud category | Cohen's κ |" in table and "Proceed; report drop-out" in table


def test_main_refuses_an_unfinished_sheet_unless_partial(scratch):
    p = scratch / "validation_sample.csv"
    s = pd.read_csv(p, dtype=str, keep_default_na=False)
    s.loc[5:, vs.CODING_COLUMNS] = ""
    s.to_csv(p, index=False)
    assert _run(scratch) == 1
    assert _run(scratch, "--official") == 1
    assert _run(scratch, "--partial") == 0
    assert "PARTIAL" in (scratch / "validation_table.md").read_text()
    assert len(pd.read_parquet(scratch / "validation_scored.parquet")) == 5


def test_main_refuses_a_sheet_with_errors(scratch):
    p = scratch / "validation_sample.csv"
    s = pd.read_csv(p, dtype=str, keep_default_na=False)
    s.loc[0, "violation_start"] = "05-11-2019"
    s.to_csv(p, index=False)
    assert _run(scratch) == 1


def test_official_scoring_is_saved_once_and_later_changes_are_labelled(scratch):
    assert _run(scratch, "--official") == 0
    saved = json.loads((scratch / "validation_official.json").read_text())
    assert saved["orders"] == 8 and saved["headline"]["cat_agree"]["rate"] == 1.0
    assert _run(scratch, "--official") == 1                       # once
    assert _run(scratch) == 0
    assert "Same coding and pipeline answers" in (scratch / "validation_table.md").read_text()
    o = pd.read_parquet(scratch / "orders.parquet")               # a Day 5 fix changes an answer
    o.loc[0, "fraud_category"] = "INSIDER_TRADING"
    o.to_parquet(scratch / "orders.parquet", index=False)
    assert _run(scratch) == 0
    table = (scratch / "validation_table.md").read_text()
    assert "NOT THE VALIDATION" in table and "overstate accuracy" in table
