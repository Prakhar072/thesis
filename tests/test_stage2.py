"""Tests for Stage 2 (src/stage2.py): the lagged frame, weighting, recall at capacity, and the permutation test."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import stage2 as S


def panel() -> pd.DataFrame:
    """A tiny panel: one fraud firm over four years, one control over three.

    Returns:
        pd.DataFrame: the panel's columns build_frame reads.
    """
    rows = []
    for fy in (2015, 2016, 2017, 2018):
        rows.append({"firm_id": "F", "fy": fy, "role": "fraud", "weight": 1.0, "fraud_year": fy == 2017,
                     "issuer_noticee": True if fy == 2017 else None, "post_public": fy == 2018,
                     "turnover_median": 10.0 * fy, "amihud": 0.1, "market_cap": 100.0, "pledged_pct": 0.0,
                     "ret": 0.01 * (fy - 2014), "excess_ret": 0.0, "vol": 0.02, "zero_volume_share": 0.0,
                     "trading_days": 240, "promoter_pct": 50.0, "promoter_pct_change": 0.0})
    for fy in (2016, 2017, 2018):
        rows.append({"firm_id": "C", "fy": fy, "role": "control", "weight": 9.6, "fraud_year": False,
                     "issuer_noticee": None, "post_public": False, "turnover_median": 5.0, "amihud": 0.2,
                     "market_cap": None, "pledged_pct": None, "ret": 0.0, "excess_ret": 0.0, "vol": 0.03,
                     "zero_volume_share": 0.0, "trading_days": 200, "promoter_pct": None,
                     "promoter_pct_change": None})
    return pd.DataFrame(rows)


def test_frame_lags_predictors_and_drops_post_public_years():
    frame = S.build_frame(panel()).set_index(["firm_id", "fy"])
    assert ("F", 2018) not in frame.index                       # after the first public action
    assert frame.loc[("F", 2017), "ret"] == pytest.approx(0.02)   # FY2016's return
    assert np.isnan(frame.loc[("F", 2015), "ret"])               # no earlier year
    assert frame.loc[("F", 2017), "fraud_year"] == 1 and frame.loc[("F", 2017), "issuer_fraud"] == 1
    assert frame.loc[("C", 2017), "fraud_year"] == 0
    assert frame.loc[("F", 2017), "pledged_any"] == 0.0


def test_class_weights_balance_the_weighted_classes():
    rows = pd.DataFrame({"weight": [1.0, 1.0, 9.6, 9.6, 9.6], "y": [1, 1, 0, 0, 0]})
    weights = S.fit_weights(rows, "y")
    assert weights[:2].sum() == pytest.approx(weights[2:].sum())
    assert weights[2] / weights[3] == pytest.approx(1.0)


def test_recall_at_k_counts_weights_against_capacity():
    y = np.array([1, 0, 1, 0])
    score = np.array([0.9, 0.8, 0.7, 0.1])
    weight = np.array([1.0, 10.0, 1.0, 10.0])
    assert S.recall_at_k(y, score, weight, 1) == pytest.approx(0.5)     # the top fraud only
    assert S.recall_at_k(y, score, weight, 11) == pytest.approx(0.5)    # the control uses up the rest
    assert S.recall_at_k(y, score, weight, 12) == pytest.approx(1.0)
    assert np.isnan(S.recall_at_k(np.zeros(4), score, weight, 5))


def test_prepared_fits_on_training_rows_only():
    train = pd.DataFrame({"a": np.arange(100, dtype=float)})
    test = pd.DataFrame({"a": [1000.0, np.nan]})
    prep = S.Prepared(train, ["a"])
    trees = prep.trees(test)
    assert trees.a.iloc[0] == pytest.approx(train.a.quantile(0.99))
    assert np.isnan(trees.a.iloc[1])
    linear = prep.linear(test)
    assert list(linear.columns) == ["a", "a_missing"] and linear.a_missing.tolist() == [0.0, 1.0]


def test_permutation_test_separates_signal_from_noise():
    rng = np.random.default_rng(0)
    rows = []
    for fy in (2020, 2021):
        y = (rng.random(2000) < 0.05).astype(int)
        rows.append(pd.DataFrame({"fy": fy, "weight": 1.0, "y": y, "signal": y + rng.normal(0, 0.5, 2000)}))
    test = pd.concat(rows, ignore_index=True)
    p_signal, _ = S.permutation_test(test, test.signal.to_numpy(), "y", reps=200)
    p_noise, _ = S.permutation_test(test, rng.normal(size=len(test)), "y", reps=200)
    assert p_signal < 0.01 and p_noise > 0.05
