"""Tests for split/bonus adjustment (Week 4 Day 4)."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src import prices

D = dt.date


def _series(closes, start=D(2022, 7, 25)):
    days = [start + dt.timedelta(days=i) for i in range(len(closes))]
    return pd.DataFrame({"date": days, "close": closes, "volume": 1})


def _fac(rows):
    return pd.DataFrame(rows, columns=["ex_date", "kind", "factor", "purpose"])


def test_a_split_is_continuous_after_adjustment():
    s = _series([950.0, 959.4, 100.35, 107.6])
    d = prices.adjust(s, _fac([(D(2022, 7, 27), "split", 10.0, "Split From Rs.10/- to Rs.1/-")]))
    assert d.ret.iloc[2] == pytest.approx(100.35 * 10 / 959.4 - 1)
    assert d.adj_close.tolist() == pytest.approx([95.0, 95.94, 100.35, 107.6])
    assert not d.gap.any()


def test_an_ex_date_on_a_non_trading_day_lands_on_the_next_trade():
    s = pd.DataFrame({"date": [D(2022, 7, 22), D(2022, 7, 26)], "close": [200.0, 101.0], "volume": 1})
    d = prices.adjust(s, _fac([(D(2022, 7, 25), "bonus", 2.0, "Bonus issue 1:1")]))
    assert d.factor.tolist() == [1.0, 2.0] and d.ret.iloc[1] == pytest.approx(0.01)


def test_unadjusted_actions_and_gaps_are_flagged():
    s = _series([100.0, 10.0, 10.5, 30.0])
    d = prices.adjust(s, _fac([(D(2022, 7, 26), "consolidation", np.nan, "Consolidation of Shares")]))
    assert d.action_unadjusted.tolist() == [False, True, False, False]
    assert d.gap.tolist() == [False, True, False, True]


def test_actions_outside_the_data_are_ignored():
    s = _series([10.0, 11.0])
    d = prices.adjust(s, _fac([(D(2020, 1, 1), "split", 5.0, "x"), (D(2030, 1, 1), "split", 5.0, "x")]))
    assert d.factor.tolist() == [1.0, 1.0] and d.adj_close.tolist() == [10.0, 11.0]


def test_factors_keep_the_firms_splits_bonuses_and_consolidations():
    ca = pd.DataFrame({"scrip_code": ["500470", "500470", "500470", "999999"],
                       "ex_date": [D(2022, 7, 28), D(2022, 6, 1), D(2010, 1, 1), D(2022, 7, 28)],
                       "kind": ["split", "dividend", "consolidation", "split"], "factor": [10.0, None, None, 2.0],
                       "purpose": ["s", "d", "c", "s"]})
    f = prices.factors(ca, ["500470"])
    assert f.kind.tolist() == ["consolidation", "split"]


def test_load_prefers_the_rolling_series(tmp_path):
    (tmp_path / "prices").mkdir()
    pd.DataFrame({"date": [D(2022, 1, 3)] * 2, "exchange": "NSE", "code": ["ABC", "ABC"], "series": ["BE", "EQ"],
                  "close": [1.0, 2.0], "volume": [5, 1]}).to_parquet(tmp_path / "prices" / "NSE_2022.parquet")
    d = prices.load("NSE", ["ABC"], D(2022, 1, 1), D(2022, 12, 31), tmp_path / "prices")
    assert d.series.tolist() == ["EQ"]


def test_bse_codes_for_an_nse_symbol():
    u = pd.DataFrame({"exchange": ["NSE", "BSE", "BSE"], "code": ["TATASTEEL", "500470", "890144"],
                      "firm_id": ["INE081A", "INE081A", "INE081A"]})
    assert prices.bse_codes_for("TATASTEEL", "NSE", u) == ["500470", "890144"]
    assert prices.bse_codes_for("500001", "BSE", u) == ["500001"]
