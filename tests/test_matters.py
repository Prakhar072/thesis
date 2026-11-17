"""Tests for the matter grouping (src/matters.py)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import matters as M


def order(stem, *, title="", matter=None, issuer=None, firm=None, start=None, end=None, date="2015-01-01",
          prior=None, prior_kind=None, otype="AO", category="MARKET_MANIPULATION", channel="UNCLEAR",
          detail="none", method="none", confidence="low", raw=None):
    """One synthetic order row with the columns build_matters reads.

    Args:
        stem (str): text stem.
        title, matter, issuer, firm, start, end, date, prior, prior_kind, otype, category, channel, detail,
            method, confidence, raw: the order's fields (str or None).

    Returns:
        dict: the row.
    """
    start_ts = pd.Timestamp(start) if start else pd.NaT
    end_ts = pd.Timestamp(end) if end else pd.NaT
    order_ts = pd.Timestamp(date)
    prior_ts = pd.Timestamp(prior) if prior else pd.NaT
    first = min(order_ts, prior_ts) if prior else order_ts
    return {"text_stem": stem, "title": title, "matter_raw": matter, "issuer_guess": issuer,
            "issuer_firm_id": firm, "violation_start": start_ts, "violation_end": end_ts,
            "violation_raw": raw, "violation_method": "regex" if start else None,
            "window_kind": "stated_period" if start else None, "order_date": order_ts,
            "order_type": otype, "prior_action_date": prior_ts, "prior_action_kind": prior_kind,
            "first_action_date": first, "first_action_source": "prior_action" if prior else "order",
            "fraud_category": category, "detection_channel": channel, "detection_detail": detail,
            "detection_method": method, "detection_confidence": confidence, "n_parties": 1.0,
            "penalty_total": np.nan, "settlement_amount": np.nan, "debarred": False}


def build(rows, entities=None):
    """Run build_matters on synthetic rows.

    Args:
        rows (list[dict]): from ``order``.
        entities (pd.DataFrame | None): parties (default: none).

    Returns:
        tuple[pd.DataFrame, pd.DataFrame]: matters and order_matters.
    """
    frame = pd.DataFrame(rows)
    if entities is None:
        entities = pd.DataFrame({"text_stem": pd.Series(dtype=str), "entity_name_clean": pd.Series(dtype=str),
                                 "entity_role": pd.Series(dtype=str), "sebi_reg_no": pd.Series(dtype=str)})
    return M.build_matters(frame, entities)


def test_iso_flag_spellings_and_nse_excluded():
    titles = pd.Series(["Order in the matter of Illiquid Stock Options",
                        "AO in the matter of dealings in illiquid stocks options at BSE",
                        "AO in respect of X in the matter of ISO at BSE",
                        "Order in the matter of Ill-liquid stock option",
                        "AO in the matter of three-way reversals in Illiquid Stock Options segment on NSE",
                        "Order in the matter of Titan"])
    empty = pd.Series([None] * len(titles))
    assert M.is_iso(titles, empty, empty).tolist() == [True, True, True, True, False, False]


def test_name_key_drops_generic_headings():
    assert M.name_key("ADJUDICATION PROCEEDINGS") is None
    assert M.name_key("Non-redressal of investor grievance(s) BACKGROUND 1. Securities") is None
    assert M.name_key("Non compliance with requirement of minimum public shareholding") is None
    assert M.name_key("Directions") is None
    assert M.name_key("dealings in the scrip of Karuna Cables Ltd") == "N:KARUNA CABLES"
    assert M.name_key("Today's Writing Products") == M.name_key("Todays Writing Products")


def test_merge_windows_tolerance():
    starts = np.array(["2010-01-01", "2010-06-01", "2011-02-01", "2015-01-01"], dtype="datetime64[ns]")
    ends = np.array(["2010-12-31", "2010-08-01", "2011-06-30", "2015-06-30"], dtype="datetime64[ns]")
    groups = M.merge_windows(starts, ends, tolerance_days=90)
    assert groups[0] == groups[1] == groups[2]      # Feb 2011 is 32 days after Dec 2010
    assert groups[3] != groups[0]


def test_shared_names_are_not_links():
    keys = pd.Series([["F:A", "N:SHARED"], ["F:B", "N:SHARED"], ["F:A", "N:OWN"]])
    kept, shared = M.drop_shared_names(keys)
    assert shared == {"N:SHARED"}
    assert kept.tolist() == [["F:A"], ["F:B"], ["F:A", "N:OWN"]]


def test_firm_and_name_link_and_separate_windows_split():
    rows = [order("1", issuer="Alpha Ltd", firm="INE001A", start="2010-01-01", end="2010-12-31",
                  date="2013-01-01", prior="2012-01-01", prior_kind="scn"),
            order("2", issuer="Alpha Limited", start="2010-03-01", end="2011-01-31", date="2014-01-01"),
            order("3", issuer="Alpha Ltd", firm="INE001A", start="2018-01-01", end="2018-06-30",
                  date="2020-01-01")]
    matters, links = build(rows)
    by_stem = links.set_index("text_stem").matter_id
    assert by_stem["1"] == by_stem["2"] != by_stem["3"]
    first = matters.set_index("matter_id").loc[by_stem["1"]]
    assert first.window_end == pd.Timestamp("2011-01-31")
    assert first.window_end_min == pd.Timestamp("2010-12-31")
    assert first.first_action_date == pd.Timestamp("2012-01-01") and first.first_action_kind == "scn"
    assert first.last_order_date == pd.Timestamp("2014-01-01")
    assert first.n_orders == 2


def test_windowless_order_attaches_to_latest_started_matter():
    rows = [order("1", issuer="Beta Ltd", start="2010-01-01", end="2010-12-31", date="2014-01-01"),
            order("2", issuer="Beta Ltd", start="2016-01-01", end="2016-12-31", date="2019-01-01"),
            order("3", issuer="Beta Ltd", date="2017-06-01", otype="WTM", prior="2015-05-01",
                  prior_kind="interim"),
            order("4", issuer="Beta Ltd", date="2009-01-01")]
    matters, links = build(rows)
    link = links.set_index("text_stem")
    assert link.loc["3", "matter_id"] == link.loc["2", "matter_id"]
    assert link.loc["3", "link"] == "attached"
    assert link.loc["4", "link"] == "windowless"
    second = matters.set_index("matter_id").loc[link.loc["2", "matter_id"]]
    # the attached order's own date counts; its recited interim (before the window start) does not
    assert second.first_action_date == pd.Timestamp("2017-06-01")
    assert second.first_action_kind == "order:WTM"


def test_iso_is_one_matter_and_unkeyed_orders_are_singletons():
    rows = [order("1", title="AO in the matter of Illiquid Stock Options", start="2014-04-01",
                  end="2015-09-30", date="2019-01-01"),
            order("2", title="AO in the matter of dealings in ISO at BSE", start="2014-04-01",
                  end="2015-09-30", date="2020-01-01"),
            order("3", matter="ADJUDICATION PROCEEDINGS", start="2014-04-01", end="2015-09-30"),
            order("4", matter="ADJUDICATION PROCEEDINGS", start="2014-04-01", end="2015-09-30")]
    matters, links = build(rows)
    link = links.set_index("text_stem").matter_id
    assert link["1"] == link["2"]
    assert len({link["2"], link["3"], link["4"]}) == 3
    assert matters.iso.sum() == 1


def test_channel_is_earliest_stated_and_category_plurality():
    rows = [order("1", issuer="Gamma Ltd", start="2010-01-01", end="2010-12-31", date="2012-01-01",
                  channel="UNCLEAR", category="DISCLOSURE_FAILURE"),
            order("2", issuer="Gamma Ltd", start="2010-01-01", end="2010-12-31", date="2013-01-01",
                  channel="COMPLAINT", detail="complaint", method="model", confidence="medium",
                  category="DISCLOSURE_FAILURE"),
            order("3", issuer="Gamma Ltd", start="2010-01-01", end="2010-12-31", date="2014-01-01",
                  channel="SURVEILLANCE", detail="alert", method="rule", confidence="high",
                  category="INSIDER_TRADING")]
    matters, _ = build(rows)
    row = matters.iloc[0]
    assert row.detection_channel == "COMPLAINT" and row.detection_detail == "complaint"
    assert row.fraud_category == "DISCLOSURE_FAILURE"
    assert M.matter_category(pd.Series(["OTHER", "INSIDER_TRADING"])) == "INSIDER_TRADING"


def test_parties_counted_across_orders():
    rows = [order("1", issuer="Delta Ltd", start="2010-01-01", end="2010-12-31"),
            order("2", issuer="Delta Ltd", start="2010-01-01", end="2010-12-31")]
    entities = pd.DataFrame({"text_stem": ["1", "1", "2"], "entity_name_clean": ["A", "B", "A"],
                             "entity_role": ["TRADER", "INTERMEDIARY", "TRADER"],
                             "sebi_reg_no": [None, None, None]})
    matters, _ = build(rows, entities)
    assert matters.iloc[0].n_parties == 2
    assert bool(matters.iloc[0].intermediary_noticee)
