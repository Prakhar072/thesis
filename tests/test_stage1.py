"""Tests for Stage 1 (src/stage1.py): the lag variable, groupings, shares and the model plumbing."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src import stage1 as S


def matters(n: int = 240, seed: int = 0) -> pd.DataFrame:
    """Synthetic matters in the shape of ``matters.parquet``.

    Args:
        n (int): number of matters.
        seed (int): random seed.

    Returns:
        pd.DataFrame: the matters.
    """
    rng = np.random.default_rng(seed)
    end = pd.Series(pd.Timestamp("2004-01-01") + pd.to_timedelta(rng.integers(0, 365 * 18, n), unit="D"))
    lag = pd.Series(pd.to_timedelta(rng.integers(-60, 3000, n), unit="D"))
    categories = rng.choice(["MARKET_MANIPULATION", "DISCLOSURE_FAILURE", "INTERMEDIARY_MISCONDUCT",
                             "INSIDER_TRADING", "FUND_DIVERSION", "OTHER"], n)
    channels = rng.choice(["SURVEILLANCE", "COMPLAINT", "INSPECTION", "REFERRAL", "MEDIA", "UNCLEAR"], n)
    return pd.DataFrame({
        "matter_id": [f"M{i:04d}" for i in range(n)], "component": [f"N:X{i}" for i in range(n)],
        "matter_name": "x", "issuer_firm_id": np.where(rng.random(n) < 0.4, "INE001A", None),
        "has_window": True, "window_start": end - pd.Timedelta(days=365), "window_end": end,
        "window_end_min": end, "first_action_date": end + lag, "first_action_kind": "scn",
        "last_order_date": end + lag + pd.Timedelta(days=400), "fraud_category": categories,
        "detection_channel": channels, "detection_detail": np.where(channels == "SURVEILLANCE", "alert", "none"),
        "n_parties": rng.integers(1, 20, n), "intermediary_noticee": rng.random(n) < 0.3,
        "window_years": 1.0, "n_orders": 1, "n_settlement": 0, "window_methods": "regex",
        "window_kinds": "stated_period", "text_stems": json.dumps(["1"])})


def test_analysis_frame_lags_and_flags():
    frame = S.analysis_frame(matters())
    row = frame.iloc[0]
    expected = (row.first_action_date - row.window_end).days / S.MONTH_DAYS
    assert row.t_months == pytest.approx(expected)
    assert (frame.positive == (frame.t_months > 0)).all()
    assert (frame.cohort == (frame.window_end <= S.COHORT_END)).all()
    assert (frame.post_lodr == (frame.window_end >= S.LODR_START).astype(int)).all()
    assert set(frame.category_group) <= set(S.CATEGORY_GROUPS)
    assert (frame.loc[frame.fraud_category == "FUND_DIVERSION", "category_group"] == "FINANCIAL").all()
    assert (frame.loc[frame.detection_channel == "MEDIA", "channel_group"] == "OTHER_STATED").all()
    assert frame.cluster.notna().all()


def test_horizon_is_last_cohort_follow_up():
    assert S.HORIZON_MONTHS * S.MONTH_DAYS / S.YEAR_DAYS == pytest.approx(7.73, abs=0.01)


def test_groupings_of_missing_values():
    assert S.category_group(None) == "OTHER"
    assert S.channel_group(None) == "UNCLEAR"
    assert S.channel_group("AUDIT") == "OTHER_STATED"


def test_wilson_interval():
    low, high = S.wilson(50, 100)
    assert low == pytest.approx(0.404, abs=0.002) and high == pytest.approx(0.596, abs=0.002)
    assert all(np.isnan(S.wilson(0, 0)))


def test_channel_shares_bounds_bracket_the_stated_share():
    shares = S.channel_shares(S.analysis_frame(matters()))
    cohort = shares[shares["sample"] == "Cohort"].set_index("measure").share
    assert cohort["Surveillance, of all (lower bound)"] <= cohort["Surveillance, of stated"]
    assert cohort["Surveillance, of stated"] <= cohort["Surveillance or unclear, of all (upper bound)"]
    group_shares = [cohort[f"Share of all: {S.CHANNEL_GROUP_LABELS[g]}"] for g in S.CHANNEL_GROUPS]
    assert sum(group_shares) == pytest.approx(1.0)


def test_describe_counts_and_medians_use_positive_lags():
    frame = S.analysis_frame(matters())
    desc = S.describe(frame)
    overall = desc.iloc[0]
    assert overall.n == len(frame)
    assert overall.t_median == pytest.approx(frame[frame.positive].t_years.median())


def test_cox_and_aft_run_and_point_the_same_way():
    frame = S.analysis_frame(matters(400))
    sample = frame[frame.positive]
    covariates = S.usable(sample, S.FRAUD_COVARIATES + S.REGIME_COVARIATES)
    model, data = S.fit_cox(sample, covariates)
    table = S.cox_table(model, "t", data)
    aft = S.fit_aft(sample, covariates, "t")
    assert set(table.covariate) == set(covariates) == set(aft.covariate)
    merged = table.merge(aft, on="covariate")
    strong = merged[merged.p_x < 0.05]
    # a higher hazard means a shorter time: HR > 1 goes with TR < 1
    assert ((strong.hr > 1) == (strong.tr < 1)).all()


def test_usable_drops_constant_columns():
    frame = pd.DataFrame({"a": [1, 1, 1], "b": [0, 1, 0]})
    assert S.usable(frame, ["a", "b", "missing"]) == ["b"]
