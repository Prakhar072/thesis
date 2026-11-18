"""Tests for the partial-observability probit (src/partial_observability.py)."""
from __future__ import annotations

import numpy as np
from scipy import optimize, stats

from src import partial_observability as P


def test_likelihood_recovers_a_simulated_model():
    rng = np.random.default_rng(3)
    n = 20000
    x = np.column_stack([np.ones(n), rng.normal(size=n)])
    w = np.column_stack([np.ones(n), rng.normal(size=n), rng.integers(0, 2, n)])
    beta_c, beta_d = np.array([-0.5, 0.8]), np.array([0.3, 0.6, -0.8])
    commit = (x @ beta_c + rng.normal(size=n)) > 0
    detect = (w @ beta_d + rng.normal(size=n)) > 0
    y = (commit & detect).astype(float)
    weight = np.ones(n)
    fit = optimize.minimize(P.negloglik, np.zeros(5), args=(y, x, w, weight), method="BFGS")
    assert np.allclose(fit.x, np.concatenate([beta_c, beta_d]), atol=0.15)


def test_probit_likelihood_matches_scipy():
    x = np.column_stack([np.ones(4), [0.0, 1.0, -1.0, 2.0]])
    y = np.array([0.0, 1.0, 0.0, 1.0])
    params = np.array([0.1, 0.5])
    p = stats.norm.cdf(x @ params)
    expected = -(y * np.log(p) + (1 - y) * np.log(1 - p)).sum()
    assert abs(P.probit_negloglik(params, y, x, np.ones(4)) - expected) < 1e-9
