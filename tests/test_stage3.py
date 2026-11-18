"""Tests for the Stage 3 event study (src/stage3.py)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src import stage3 as S


def test_event_type_precedence():
    assert S.event_type(["FINAL", "INTERIM"], "order") == "INTERIM"
    assert S.event_type(["SETTLEMENT_FINAL"], "order") == "SETTLEMENT"
    assert S.event_type(["CONFIRMATION_OR_EXTENSION", "FINAL"], "order") == "CONFIRMATION"
    assert S.event_type(["UNKNOWN"], "order") == "FINAL"
    assert S.event_type(["OTHER"], "order") is None
    assert S.event_type([], "interim_recited") == "INTERIM"


def test_dimson_beta_recovers_lagged_exposure():
    rng = np.random.default_rng(1)
    m = rng.normal(0, 0.01, 400)
    r = 0.0005 + 0.6 * m[1:-1] + 0.4 * m[:-2] + rng.normal(0, 0.002, 398)
    alpha, beta_d, beta_ols = S.dimson_fit(r, m[:-2], m[1:-1], m[2:])
    assert beta_d == pytest.approx(1.0, abs=0.05)
    assert beta_ols < 0.75 < beta_d                       # the plain beta misses the lagged part
    assert alpha == pytest.approx(0.0005, abs=0.0005)


def synthetic_ars(n_events: int, shift: float, seed: int = 0) -> pd.DataFrame:
    """A long AR table with noise and a shift on days 0 to +2.

    Args:
        n_events (int): events.
        shift (float): abnormal return added on days 0–2.
        seed (int): random seed.

    Returns:
        pd.DataFrame: ``event_id``, ``rel``, ``ar``.
    """
    rng = np.random.default_rng(seed)
    rel = np.arange(S.ESTIMATION[0], 11)
    frames = []
    for event in range(n_events):
        ar = rng.normal(0, 0.02, len(rel))
        ar[(rel >= 0) & (rel <= 2)] += shift
        frames.append(pd.DataFrame({"event_id": event, "rel": rel, "ar": ar}))
    return pd.concat(frames, ignore_index=True)


def test_rank_test_detects_a_shift_and_not_noise():
    assert S.rank_test(synthetic_ars(60, -0.03), list(range(60)), 0, 2) < -3
    assert abs(S.rank_test(synthetic_ars(60, 0.0, seed=3), list(range(60)), 0, 2)) < 2.5
    assert math.isnan(S.rank_test(synthetic_ars(3, 0.0), [0, 1, 2], 0, 2))


def test_cars_and_group_test():
    ars = synthetic_ars(40, -0.03)
    est = pd.DataFrame({"event_id": range(40), "sigma": 0.02})
    table = S.cars(ars, est)
    window = table[table.window == "(-1,+1)"]
    assert len(window) == 40
    assert window.car.mean() == pytest.approx(-0.06, abs=0.02)
    result = S.test_group(window, ars, "(-1,+1)")
    assert result["n"] == 40 and result["p_bmp"] < 0.01 and result["mean"] < 0


def test_stars():
    assert S.stars(0.001) == "***" and S.stars(0.03) == "**" and S.stars(0.07) == "*" and S.stars(0.5) == ""
    assert S.stars(float("nan")) == ""
