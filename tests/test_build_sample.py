"""Tests for the Week 4 analysis sample."""

import datetime as dt
import json

import pandas as pd
import pytest

from src import build_sample as bs

D = dt.date


def test_financial_years():
    assert bs.fy(D(2011, 3, 31)) == 2011 and bs.fy(D(2011, 4, 1)) == 2012
    assert bs.fy_bounds(2012) == (D(2011, 4, 1), D(2012, 3, 31))


def test_fraud_years_split_a_window_by_financial_year():
    w = pd.DataFrame([{"issuer_firm_id": "INE001A", "text_stem": "1", "violation_start": D(2011, 1, 1),
                       "violation_end": D(2012, 6, 30), "fraud_category": "MARKET_MANIPULATION",
                       "issuer_noticee": False, "window_kind": "stated_period", "violation_method": "regex"},
                      {"issuer_firm_id": "INE001A", "text_stem": "2", "violation_start": D(2011, 5, 1),
                       "violation_end": D(2011, 5, 31), "fraud_category": "DISCLOSURE_FAILURE",
                       "issuer_noticee": True, "window_kind": "conduct_dates", "violation_method": "model"}])
    y = bs.fraud_years(w).set_index("fy")
    assert y.index.tolist() == [2011, 2012, 2013]
    assert y.loc[2011, "overlap_days"] == 90 and y.loc[2013, "overlap_days"] == 91
    assert y.loc[2012, "n_orders"] == 2 and json.loads(y.loc[2012, "categories"]) == ["DISCLOSURE_FAILURE",
                                                                                       "MARKET_MANIPULATION"]
    assert y.loc[2012, "issuer_noticee"] and not y.loc[2011, "issuer_noticee"]
    assert not y.loc[2012, "stated_period_only"] and y.loc[2011, "stated_period_only"]
    assert y.in_period.all()


def _inputs():
    orders = pd.DataFrame({
        "text_stem": ["1", "2", "3", "4"], "order_date": [D(2014, 6, 1), D(2015, 1, 5), D(2016, 1, 1), D(2016, 2, 2)],
        "order_type": ["AO", "WTM", "AO", "AO"],
        "violation_start": [D(2011, 5, 1), None, D(2012, 1, 1), D(2010, 1, 1)],
        "violation_end": [D(2011, 12, 31), None, D(2012, 3, 1), D(2010, 6, 1)],
        "window_kind": ["stated_period", None, "stated_period", "stated_period"],
        "violation_method": ["regex", None, "regex", "regex"],
        "fraud_category": ["MARKET_MANIPULATION", "OTHER", "INSIDER_TRADING", "OTHER"],
        "earliest_interim_date": [D(2013, 2, 1), None, None, None]})
    oi = pd.DataFrame({"text_stem": ["1", "2", "3", "4"],
                       "issuer_firm_id": ["INE001A", "INE002B", None, "INE003C"],
                       "match_method": ["exact", "auto", "review_pending", "auto"],
                       "candidate_firm_id": [None, None, "INE004D", None],
                       "issuer_isin": ["INE001A01011", "INE002B01011", None, "INE003C01011"],
                       "issuer_ticker": ["AAA", "BBB", None, "CCC"], "issuer_nse_symbol": ["AAA", None, None, None],
                       "issuer_bse_code": ["500001", "500002", None, "500003"]})
    em = pd.DataFrame({"text_stem": ["1"], "firm_id": ["INE001A"], "match_method": ["exact"]})
    firms = pd.DataFrame({"firm_id": [f"INE{i:03d}X" for i in range(10, 40)] +
                          ["INE001A", "INE002B", "INE003C", "INE004D", "BSE:500999"],
                          "name": "n", "status": "Active", "on_bse": True, "on_nse": False,
                          "isins": "[]", "bse_codes": "[]", "nse_symbols": "[]"})
    return orders, oi, em, firms


def test_build_sample_roles_and_exclusions():
    sample, years, ev = bs.build(*_inputs(), n_controls=10, seed=1)
    fraud = sample[sample.role == "fraud"]
    assert set(fraud.firm_id) == {"INE001A", "INE003C"}          # windowed and matched; pending review left out
    controls = sample[sample.role == "control"]
    assert len(controls) == 10 and controls.weight.iloc[0] == pytest.approx(30 / 10)
    # never a control: any order's issuer, a review candidate, a firm without an ISIN
    assert not set(controls.firm_id) & {"INE001A", "INE002B", "INE003C", "INE004D", "BSE:500999"}
    assert years[years.firm_id == "INE001A"].issuer_noticee.all()
    assert fraud.set_index("firm_id").loc["INE001A", "issuer_ticker"] == "AAA"


def test_events_include_unwindowed_orders_and_recited_interims():
    _, _, ev = bs.build(*_inputs(), n_controls=5, seed=1)
    kinds = set(zip(ev.firm_id, ev.kind, ev.event_date))
    assert ("INE002B", "order", D(2015, 1, 5)) in kinds                      # no window, still an event
    assert ("INE001A", "interim_recited", D(2013, 2, 1)) in kinds
    assert not (ev.firm_id == "INE004D").any()


def test_control_draw_is_repeatable():
    a, *_ = bs.build(*_inputs(), n_controls=10, seed=7)
    b, *_ = bs.build(*_inputs(), n_controls=10, seed=7)
    assert bs.fingerprint(a) == bs.fingerprint(b)


def test_main_freezes(tmp_path, monkeypatch):
    orders, oi, em, firms = _inputs()
    for name, df in (("order_issuers", oi), ("entity_matches", em), ("firms", firms)):
        df.to_parquet(tmp_path / f"{name}.parquet", index=False)
    monkeypatch.setattr(bs, "DERIVED_DIR", tmp_path)
    orders.to_parquet(tmp_path / "orders.parquet", index=False)
    args = ["--derived", str(tmp_path), "--scratch", str(tmp_path), "--controls", "5"]
    assert bs.main(args) == 0
    frozen = pd.read_csv(tmp_path / "sample_frozen.csv")
    assert set(frozen.role) == {"fraud", "control"}
    assert bs.main(args) == 1                       # frozen
    assert bs.main(args + ["--force"]) == 0
