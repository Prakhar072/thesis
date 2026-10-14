"""Tests for the Week 4 panel merge."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src import build_panel as bp

D = dt.date


def _universe():
    return pd.DataFrame({"exchange": ["BSE", "NSE", "BSE"], "code": ["500001", "ABC", "500002"],
                         "firm_id": ["INE001A", "INE001A", "INE002B"]})


def test_security_map_prefers_the_isin_beside_an_nse_symbol():
    nse_rows = pd.DataFrame({"code": ["OLDABC", "XYZ"], "isin": ["INE001A01011", "INE009Z01011"]})
    m = bp.security_map(_universe(), nse_rows, {"ABCOLD2": "ABC"})
    got = {(r.exchange, r.code): (r.firm_id, r.how) for r in m.itertuples()}
    assert got[("NSE", "OLDABC")] == ("INE001A", "isin")
    assert got[("NSE", "ABC")] == ("INE001A", "universe")
    assert got[("NSE", "ABCOLD2")] == ("INE001A", "symbol_change")
    assert got[("BSE", "500002")] == ("INE002B", "universe")


def _prices(tmp_path):
    (tmp_path / "prices").mkdir()
    days = pd.bdate_range("2015-03-02", "2017-03-31")
    rows = []
    for i, d in enumerate(days):
        rows.append({"date": d.date(), "exchange": "NSE", "code": "ABC", "series": "EQ", "isin": "INE001A01011",
                     "close": 100.0 * (1.001 ** i), "volume": 10, "turnover": 1e7})
        if i % 2 == 0:  # thinner on BSE
            rows.append({"date": d.date(), "exchange": "BSE", "code": "500001", "series": "B", "isin": None,
                         "close": 100.0 * (1.001 ** i), "volume": 5, "turnover": 5e6})
        rows.append({"date": d.date(), "exchange": "BSE", "code": "500002", "series": "B", "isin": None,
                     "close": 50.0, "volume": 0 if i % 3 else 7, "turnover": 0 if i % 3 else 350.0})
    p = pd.DataFrame(rows)
    for (ex, y), g in p.groupby([p.exchange, pd.to_datetime(p.date).dt.year]):
        g.to_parquet(tmp_path / "prices" / f"{ex}_{y}.parquet", index=False)
    return tmp_path / "prices"


def test_panel_rows_label_and_signals(tmp_path):
    prices_dir = _prices(tmp_path)
    sample = pd.DataFrame({"firm_id": ["INE001A", "INE002B"], "role": ["fraud", "control"], "name": ["A", "B"],
                           "status": "Active", "weight": [1.0, 9.5]})
    fraud_years = pd.DataFrame({"firm_id": ["INE001A"], "fy": [2016], "overlap_days": [200], "n_orders": [1],
                                "categories": ['["MARKET_MANIPULATION"]'], "issuer_noticee": [True],
                                "stated_period_only": [True]})
    events = pd.DataFrame({"firm_id": ["INE001A"], "event_date": [D(2016, 2, 1)]})   # in FY2016
    ca = pd.DataFrame(columns=["scrip_code", "ex_date", "kind", "factor", "purpose"])
    index = pd.DataFrame({"date": [D(2015, 3, 31), D(2016, 3, 31), D(2017, 3, 31)], "close": [100.0, 110.0, 99.0]})
    shp = pd.DataFrame({"firm_id": ["INE001A", "INE001A", "INE001A"], "status": ["ok", "ok", "ok"],
                        "quarter_end": [D(2016, 3, 31), D(2016, 9, 30), D(2017, 3, 31)],
                        "promoter_pct": [60.0, 58.0, 55.0], "pledged_pct": [0.0, 10.0, 20.0],
                        "pledged_shares": [0.0, 1.0, 2.0], "total_shares": [1e7, 1e7, 1e7],
                        "pledge_answer": ["No", "Yes", "Yes"]})
    panel = bp.build(sample, fraud_years, events, _universe(), {}, prices_dir, ca, index, shp, period=(2015, 2017))
    a = panel[panel.firm_id == "INE001A"].set_index("fy")
    assert a.index.tolist() == [2015, 2016, 2017]
    assert a.fraud_year.tolist() == [False, True, False] and a.post_public.tolist() == [False, False, True]
    assert (a.main_exchange == "NSE").all()                                   # more trading days on NSE
    assert a.loc[2016, "ret"] == pytest.approx(1.001 ** (len(pd.bdate_range("2015-04-01", "2016-03-31"))) - 1,
                                               rel=1e-6)
    assert a.loc[2016, "excess_ret"] == pytest.approx(a.loc[2016, "ret"] - 0.10)
    assert (a.loc[2016, "promoter_pct"], a.loc[2017, "promoter_pct"]) == (60.0, 55.0)   # March quarters only
    assert a.loc[2017, "promoter_pct_change"] == -5.0 and a.loc[2017, "pledged_pct"] == 20.0
    assert a.loc[2016, "market_cap"] == pytest.approx(a.loc[2016, "close_end"] * 1e7 / 1e7)
    b = panel[panel.firm_id == "INE002B"].set_index("fy")
    assert b.role.iloc[0] == "control" and b.weight.iloc[0] == 9.5 and not b.fraud_year.any()
    assert 0.6 < b.loc[2016, "zero_volume_share"] < 0.7


def test_coverage_table():
    panel = pd.DataFrame({"firm_id": ["A", "A", "B", "C"], "fy": [2015, 2016, 2016, 2016],
                          "trading_days": [200, 0, 150, np.nan], "promoter_pct": [np.nan, 50.0, 40.0, np.nan],
                          "fraud_year": [True, True, False, False]})
    sample = pd.DataFrame({"firm_id": ["A", "B", "C"], "role": ["fraud", "control", "control"]})
    t = bp.coverage(panel, sample).set_index("unit")
    assert t.loc["fraud firms", ["n", "with_prices", "with_ownership", "with_both"]].tolist() == [1, 1, 1, 1]
    assert t.loc["control firms", ["with_prices", "with_ownership", "with_both"]].tolist() == [1, 1, 1]
    assert t.loc["fraud firm-years", ["n", "with_prices", "with_ownership", "with_both"]].tolist() == [2, 1, 1, 0]
