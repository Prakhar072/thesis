"""Tests for the synthesis (src/synthesis.py)."""
from __future__ import annotations

import math

import pytest

from src import synthesis as S


def test_implied_penalty():
    assert S.implied_penalty(100.0, 0.5, 0.0, 3.0) == pytest.approx(200.0)
    assert S.implied_penalty(100.0, 1.0, 0.1, 2.0) == pytest.approx(100.0 * math.exp(0.2))
    assert S.implied_penalty(100.0, 0.2, 0.08, 3.0) > S.implied_penalty(100.0, 0.5, 0.08, 3.0)
