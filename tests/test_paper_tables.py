"""Tests for the thesis tables: the units, lags and text checks that the prose relies on."""

import numpy as np
import pandas as pd
import pytest

from src import paper_tables as pt


def _orders(**columns):
    """A small order frame with the columns the matter helpers read."""
    base = {"text_stem": ["a", "b", "c", "d"],
            "issuer_firm_id": ["INE1", "INE1", None, None],
            "issuer_guess": ["X Ltd", "X Ltd", "Y Ltd", None],
            "violation_start": pd.to_datetime(["2014-01-01"] * 4),
            "violation_end": pd.to_datetime(["2014-12-31"] * 4),
            "first_action_date": pd.to_datetime(["2016-01-01", "2015-01-01", "2017-01-01",
                                                 "2018-01-01"]),
            "order_date": pd.to_datetime(["2018-01-01", "2019-01-01", "2020-01-01",
                                          "2021-01-01"]),
            "has_window": [True, True, True, True],
            "iso": [False, False, False, False]}
    base.update(columns)
    return pd.DataFrame(base)


def test_years_between_uses_julian_years():
    later = pd.Series(pd.to_datetime(["2020-01-01", None]))
    earlier = pd.Series(pd.to_datetime(["2019-01-01", "2019-01-01"]))
    result = pt.years_between(later, earlier)
    assert result[0] == pytest.approx(365 / 365.25)
    assert np.isnan(result[1])


@pytest.mark.parametrize("year,era", [(2000, "2000--2005"), (2005, "2000--2005"),
                                      (2006, "2006--2010"), (2026, "2021--2026"), (1999, None)])
def test_era_of(year, era):
    assert pt.era_of(year) == era


def test_era_of_missing_year():
    assert pt.era_of(float("nan")) is None


def test_cluster_key_groups_by_issuer_and_exact_window():
    keys = pt.cluster_key(_orders())
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]
    assert keys[2] != keys[3]


def test_cluster_key_separates_windows_of_one_issuer():
    frame = _orders(violation_end=pd.to_datetime(["2014-12-31", "2015-12-31", "2014-12-31",
                                                  "2014-12-31"]))
    keys = pt.cluster_key(frame)
    assert keys[0] != keys[1]


def test_cluster_key_collapses_iso_whatever_the_issuer():
    keys = pt.cluster_key(_orders(iso=[True, False, True, True]))
    assert keys[0] == keys[2] == keys[3] == "ISO"
    assert keys[1] != "ISO"


def test_clusters_take_the_earliest_action_and_the_latest_order():
    grouped = pt.clusters(_orders())
    row = grouped.loc[pt.cluster_key(_orders())[0]]
    assert row.n_orders == 2
    assert row.first_action == pd.Timestamp("2015-01-01")
    assert row.last_order == pd.Timestamp("2019-01-01")
    assert row.t_first == pytest.approx(1 / 365.25)
    assert row.t_resolve == pytest.approx((pd.Timestamp("2019-01-01")
                                           - pd.Timestamp("2015-01-01")).days / 365.25)


def test_clusters_ignore_orders_without_a_window():
    grouped = pt.clusters(_orders(has_window=[True, False, False, False]))
    assert grouped.n_orders.sum() == 1


def test_channel_consistency_counts_only_clusters_with_two_stated_orders():
    frame = _orders(stated=[True, True, True, True],
                    detection_channel=["COMPLAINT", "MEDIA", "COMPLAINT", "AUDIT"],
                    detection_method=["rule", "model", "rule", "rule"])
    result = pt.channel_consistency(frame)
    assert result["clusters"] == 1
    assert result["disagree"] == 1
    assert result["share_disagree"] == 1
    assert result["clusters_rule"] == 0


def test_channel_consistency_agreement():
    frame = _orders(stated=[True, True, False, False],
                    detection_channel=["COMPLAINT", "COMPLAINT", "UNCLEAR", "UNCLEAR"],
                    detection_method=["rule", "rule", "none", "none"])
    result = pt.channel_consistency(frame)
    assert (result["clusters"], result["disagree"]) == (1, 0)
    assert (result["clusters_rule"], result["disagree_rule"]) == (1, 0)


def test_channel_consistency_leaves_out_iso_and_issuerless_orders():
    frame = _orders(stated=[True, True, True, True], iso=[True, True, False, False],
                    detection_channel=["COMPLAINT", "MEDIA", "COMPLAINT", "AUDIT"],
                    detection_method=["rule"] * 4,
                    issuer_firm_id=[None] * 4, issuer_guess=["X", "X", None, None])
    assert pt.channel_consistency(frame)["clusters"] == 0


@pytest.mark.parametrize("text", [
    "The Noticee shall remit / pay the said amount of penalty within 45 days",
    "The noticee shall pay the amount of penalty imposed",
    "Noticees mentioned above shall remit/pay the said amount of penalties within 45 days",
    "shall pay the penalty amount within a period of 45 days",
])
def test_pay_clause_matches_the_forms_in_the_orders(text):
    assert pt.PAY_CLAUSE.search(text)


@pytest.mark.parametrize("text", [
    "The appeal accordingly abates.",
    "the Noticee may be liable to a penalty",
    "failure to pay the penalty may attract recovery proceedings",
])
def test_pay_clause_ignores_other_mentions(text):
    assert not pt.PAY_CLAUSE.search(text)


def test_unread_penalties_counts_clauses_in_the_last_fifth_only(tmp_path):
    (tmp_path / "late.txt").write_text("x" * 900 + " The Noticee shall pay the penalty amount ")
    (tmp_path / "early.txt").write_text(" The Noticee shall pay the penalty amount " + "x" * 900)
    (tmp_path / "read.txt").write_text("x" * 900 + " The Noticee shall pay the penalty amount ")
    frame = pd.DataFrame({"has_text": [True, True, True],
                          "penalty_outcome": ["unknown", "no_penalty", "penalty"],
                          "order_type": ["AO", "AO", "AO"],
                          "text_path": ["late.txt", "early.txt", "read.txt"]})
    assert pt.unread_penalties(frame, tmp_path) == 1


def test_unread_penalties_skips_settlement_orders_and_missing_files(tmp_path):
    (tmp_path / "s.txt").write_text("x" * 900 + " The Noticee shall pay the penalty amount ")
    frame = pd.DataFrame({"has_text": [True, True], "penalty_outcome": ["unknown", "unknown"],
                          "order_type": ["SETTLEMENT", "AO"],
                          "text_path": ["s.txt", "gone.txt"]})
    assert pt.unread_penalties(frame, tmp_path) == 0


def test_pct_handles_an_empty_denominator():
    assert pt.pct(1, 0) == "--"
    assert pt.pct(1, 4) == "25.0\\%"
    assert pt.pct(1, 3, 0) == "33\\%"


def test_spread_reports_median_and_quartiles():
    assert pt.spread(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])) == "3.00 (2.00--4.00)"
    assert pt.spread(pd.Series([np.nan])) == "--"


def test_n_groups_thousands():
    assert pt.n(20861) == "20,861"


def test_tabular_shape_and_panels():
    body = pt.tabular("rr", ["", "A", "B"], ["Panel one", ["x", "1", "2"], "Panel two",
                                             ["y", "3", "4"]])
    assert body.startswith("\\begin{tabular*}{\\textwidth}[]{l@{\\extracolsep\\fill}rr}")
    assert body.count("\\multicolumn{3}{l}") == 2
    assert body.count("\\midrule") == 2  # after the header, and before the second panel only
    assert body.rstrip().endswith("\\end{tabular*}")


def _text_orders():
    """Orders with the channel and window columns the surveillance figures read."""
    return pd.DataFrame({"iso": [False, False, False, True],
                         "has_window": [True, True, False, True],
                         "fraud_category": ["INSIDER_TRADING", "OTHER", "OTHER",
                                            "MARKET_MANIPULATION"],
                         "stated": [True, True, False, True],
                         "detection_channel": ["SURVEILLANCE", "COMPLAINT", "UNCLEAR",
                                               "SURVEILLANCE"],
                         "detection_detail": ["alert", "complaint", "none",
                                              "trading_pattern_observed"]})


def test_surveillance_figures_separate_alerts_from_observed_patterns():
    result = pt.surveillance_figures(_text_orders())
    assert result["surveillance_alert"]["all"] == {"alert": 1, "stated": 3, "share": 1 / 3}
    assert result["surveillance_alert"]["excl_iso"]["stated"] == 2
    assert result["surveillance_alert"]["issuer_windowed_excl_iso"]["stated"] == 1


def test_surveillance_bounds_treat_unclear_orders_as_none_or_all():
    result = pt.surveillance_figures(_text_orders())
    assert result["surveillance_bound_low"] == 2 / 4
    assert result["surveillance_bound_high"] == 3 / 4


def test_cohort_figures_keep_windows_ending_by_the_cut():
    frame = _orders(iso=[False, False, False, True], matched=[True, True, False, False],
                    violation_end=pd.to_datetime(["2014-12-31", "2014-12-31", "2019-06-30",
                                                  "2014-12-31"]),
                    year=[2018, 2019, 2020, 2021], t_first=[1.0, 2.0, 3.0, 4.0])
    result = pt.cohort_figures(frame, cut="2018-12-31")
    assert result["cohort_orders"] == 3
    assert result["cohort_orders_excl_iso"] == 2
    assert result["cohort_matched_excl_iso"] == 2
    assert result["median_lag_by_end_year"]["2014"] == {"n": 2, "median": 1.5}
    assert result["orders_by_year_excl_iso"] == {"2018": 1, "2019": 1, "2020": 1}
