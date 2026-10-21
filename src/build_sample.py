"""Week 4 Day 3: define the analysis sample, then freeze it.

Runbook: "Fraud firm-years: every (firm, year) falling inside a violation window,
keeping only matched listed issuers. Control firms … sample several hundred
firms rather than taking the universe. Freeze the sample to
derived/sample.parquet and do not change it afterwards."

Reads ``derived/orders.parquet`` (windows, categories, dates), the Day 2 matches
(``order_issuers``, ``entity_matches``) and the Day 1 universe (``firms``), and
writes

* ``derived/sample.parquet`` — one row per sample firm: ``role`` ∈ ``fraud,
  control``, codes for fetching prices and shareholding, and for controls the
  ``weight`` (eligible firms / firms drawn) that turns control counts back into
  universe counts (Stage 2's base rates and recall at SEBI's capacity);
* ``derived/sample_fraud_years.parquet`` — one row per fraud firm-year;
* ``derived/sample_events.parquet`` — one row per (firm, date, kind) of public
  enforcement event, for Stage 3 and for the price windows Day 4 must cover;
* ``hand/sample_frozen.csv`` — the frozen firm list (firm_id, role, weight),
  committed, because ``derived/`` is not.

Definitions
-----------
* **Year** = the Indian financial year, April–March, named by the year it ends
  (FY2012 = 1 Apr 2011 – 31 Mar 2012): the year of annual reports, audit
  opinions and shareholding filings.
* **Fraud firm-year**: a matched issuer (``match_method`` code / exact / auto /
  hand) and a financial year that overlaps one of its orders' violation windows
  (``overlap_days`` kept, so a stricter rule can be applied later).
  ``in_period`` marks FY2010–FY2025, the technical plan's sample period (§4.5).
  Every order category is kept, with flags (``categories``,
  ``issuer_noticee``: the issuer itself was a noticee), so Stage 2 can restrict
  the label to issuer frauds without re-drawing the sample.
* **Stage 2 controls**: a seeded simple random sample of ``N_CONTROLS`` firms
  from the eligible universe — firms with an ISIN that are not the issuer, or a
  review candidate, of any order. Live, suspended and delisted firms enter in
  proportion (no survivorship). Years with trading come from Day 4's prices.
* **Stage 3 matched controls** (industry, size, pre-event return) need prices
  and shareholding, so they are drawn after Days 4–5 from the eligible pool.
* **Events**: each order whose issuer is matched (``order``, with its type),
  and each recited interim order date (``interim_recited``; its ``order_type``
  is that of the order reciting it).

Freeze: writing into ``derived/`` refuses to replace an existing sample unless
``--force``. Run it once, after the Day 2 hand review (a reviewed match can add
a fraud firm).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import pathlib

import numpy as np
import pandas as pd

from src import cache
from src.extract_text import assert_safe_output, setup_logging

log = logging.getLogger("build_sample")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
FROZEN = REPO_ROOT / "hand" / "sample_frozen.csv"
LOG_FILE = REPO_ROOT / "logs" / "build_sample.log"
SAMPLE_VERSION = "sample-v1"

MATCHED = ("code", "exact", "auto", "hand")
PERIOD_FY = (2010, 2025)
N_CONTROLS = 600
SEED = 20261015


def fy(d) -> int:
    """Financial year (April–March) a date falls in, named by its end year.

    Args:
        d (datetime.date): a date.
    Returns:
        int: its financial year (2011-04-01 → 2012).
    """
    return d.year + (1 if d.month >= 4 else 0)


def fy_bounds(year: int) -> tuple[dt.date, dt.date]:
    """The first and last day of a financial year.

    Args:
        year (int): the financial year (its end year).
    Returns:
        tuple[datetime.date, datetime.date]: (1 April of year − 1, 31 March of year).
    """
    return dt.date(year - 1, 4, 1), dt.date(year, 3, 31)


def fraud_years(windows: pd.DataFrame) -> pd.DataFrame:
    """One row per (firm, financial year) a violation window overlaps.

    Args:
        windows (pd.DataFrame): windowed orders with a matched issuer (issuer_firm_id, violation_start/end,
            text_stem, fraud_category, issuer_noticee, window_kind, violation_method).
    Returns:
        pd.DataFrame: firm_id, fy, overlap_days, n_orders, categories (JSON), issuer_noticee,
        stated_period_only, text_stems (JSON), in_period.
    """
    rows = []
    for r in windows.itertuples():
        for y in range(fy(r.violation_start), fy(r.violation_end) + 1):
            lo, hi = fy_bounds(y)
            overlap = (min(hi, r.violation_end) - max(lo, r.violation_start)).days + 1
            rows.append({"firm_id": r.issuer_firm_id, "fy": y, "overlap_days": overlap, "text_stem": r.text_stem,
                         "fraud_category": r.fraud_category, "issuer_noticee": r.issuer_noticee,
                         "window_kind": r.window_kind, "violation_method": r.violation_method})
    if not rows:
        return pd.DataFrame(columns=["firm_id", "fy", "overlap_days", "n_orders", "categories", "issuer_noticee",
                                     "stated_period_only", "text_stems", "in_period"])
    d = pd.DataFrame(rows)
    out = d.groupby(["firm_id", "fy"]).agg(
        overlap_days=("overlap_days", "max"), n_orders=("text_stem", "nunique"),
        categories=("fraud_category", lambda s: json.dumps(sorted(set(s.dropna())))),
        issuer_noticee=("issuer_noticee", "any"),
        stated_period_only=("window_kind", lambda s: bool((s == "stated_period").all())),
        text_stems=("text_stem", lambda s: json.dumps(sorted(set(s))))).reset_index()
    out["in_period"] = out.fy.between(*PERIOD_FY)
    return out


def events(orders: pd.DataFrame) -> pd.DataFrame:
    """Public enforcement events per matched issuer: the order itself and any recited interim order.

    Args:
        orders (pd.DataFrame): orders with issuer_firm_id, order_date, order_type, earliest_interim_date …
    Returns:
        pd.DataFrame: firm_id, event_date, kind (order / interim_recited), order_type, n_orders, text_stems,
        categories.
    """
    o = orders[orders.issuer_firm_id.notna()]
    a = o[["issuer_firm_id", "order_date", "order_type", "text_stem", "fraud_category"]].assign(kind="order")
    b = (o[o.earliest_interim_date.notna()][["issuer_firm_id", "earliest_interim_date", "order_type", "text_stem",
                                             "fraud_category"]]
         .rename(columns={"earliest_interim_date": "order_date"}).assign(kind="interim_recited"))
    e = pd.concat([a, b], ignore_index=True).rename(columns={"issuer_firm_id": "firm_id", "order_date": "event_date"})
    e = (e.sort_values(["firm_id", "event_date", "kind", "text_stem"])
          .groupby(["firm_id", "event_date", "kind"], as_index=False)
          .agg(order_type=("order_type", "first"), n_orders=("text_stem", "nunique"),
               text_stems=("text_stem", lambda s: json.dumps(sorted(set(s)))),
               categories=("fraud_category", lambda s: json.dumps(sorted(set(s.dropna()))))))
    return e


def draw_controls(firms: pd.DataFrame, excluded: set[str], n: int = N_CONTROLS, seed: int = SEED) -> pd.DataFrame:
    """Draw the Stage 2 control firms: a seeded simple random sample of the eligible universe.

    Args:
        firms (pd.DataFrame): Day 1's firm table.
        excluded (set[str]): firms that are an issuer or a review candidate of any order.
        n (int): how many to draw.
        seed (int): the random seed.
    Returns:
        pd.DataFrame: the drawn firms, with role "control", weight (eligible / drawn) and eligible_controls.
    """
    eligible = firms[~firms.firm_id.str.contains(":") & ~firms.firm_id.isin(excluded)]
    picked = eligible.sample(n=min(n, len(eligible)), random_state=np.random.RandomState(seed))
    return picked.assign(role="control", weight=len(eligible) / len(picked), eligible_controls=len(eligible))


def build(orders: pd.DataFrame, order_issuers: pd.DataFrame, entity_matches: pd.DataFrame, firms: pd.DataFrame,
          n_controls: int = N_CONTROLS, seed: int = SEED):
    """Define the sample: fraud firms and their firm-years, controls, events.

    Args:
        orders (pd.DataFrame): orders.parquet columns (dates, window, category, interim date).
        order_issuers (pd.DataFrame), entity_matches (pd.DataFrame): Day 2's matches.
        firms (pd.DataFrame): Day 1's firm table.
        n_controls (int), seed (int): the control draw.
    Returns:
        tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]: (sample, sample_fraud_years, sample_events).
    """
    oi = order_issuers[["text_stem", "issuer_firm_id", "match_method", "candidate_firm_id", "issuer_isin",
                        "issuer_ticker", "issuer_nse_symbol", "issuer_bse_code"]]
    o = orders.merge(oi, on="text_stem", how="left", validate="one_to_one")
    o.loc[~o.match_method.isin(MATCHED), "issuer_firm_id"] = None
    noticed = entity_matches[entity_matches.match_method.isin(MATCHED)][["text_stem", "firm_id"]].drop_duplicates()
    o["issuer_noticee"] = [(s, f) in set(zip(noticed.text_stem, noticed.firm_id))
                           for s, f in zip(o.text_stem, o.issuer_firm_id)]
    windows = o[o.issuer_firm_id.notna() & o.violation_start.notna() & o.violation_end.notna()]
    years = fraud_years(windows)
    ev = events(o)

    fraud_ids = sorted(set(years.firm_id))
    f = firms.set_index("firm_id")
    per_firm = windows.groupby("issuer_firm_id").agg(
        n_windowed_orders=("text_stem", "nunique"), first_window_start=("violation_start", "min"),
        last_window_end=("violation_end", "max"),
        categories=("fraud_category", lambda s: json.dumps(sorted(set(s.dropna())))))
    n_orders = o[o.issuer_firm_id.notna()].groupby("issuer_firm_id").text_stem.nunique()
    fraud = (f.loc[fraud_ids].reset_index().assign(role="fraud", weight=1.0)
             .merge(per_firm, left_on="firm_id", right_index=True, how="left"))
    fraud["n_orders"] = fraud.firm_id.map(n_orders).astype("Int64")
    fy_counts = years.groupby("firm_id").agg(n_fraud_years=("fy", "size"), n_fraud_years_in_period=("in_period", "sum"))
    fraud = fraud.merge(fy_counts, left_on="firm_id", right_index=True, how="left")

    excluded = set(o.issuer_firm_id.dropna()) | set(order_issuers.candidate_firm_id.dropna())
    controls = draw_controls(firms, excluded, n_controls, seed)
    sample = pd.concat([fraud, controls], ignore_index=True)
    codes = (order_issuers.dropna(subset=["issuer_firm_id"]).drop_duplicates("issuer_firm_id")
             .set_index("issuer_firm_id")[["issuer_isin", "issuer_ticker", "issuer_nse_symbol", "issuer_bse_code"]])
    sample = sample.merge(codes, left_on="firm_id", right_index=True, how="left")
    sample["sample_version"] = SAMPLE_VERSION
    sample["seed"] = seed
    cols = ["firm_id", "role", "name", "status", "on_bse", "on_nse", "isins", "bse_codes", "nse_symbols",
            "issuer_isin", "issuer_ticker", "weight", "eligible_controls", "n_orders", "n_windowed_orders",
            "first_window_start", "last_window_end", "categories", "n_fraud_years", "n_fraud_years_in_period",
            "sample_version", "seed"]
    sample = sample[[c for c in cols if c in sample.columns]]
    return sample, years, ev


def fingerprint(sample: pd.DataFrame) -> str:
    """A short hash of the frozen firm list (firm_id, role), for results_log.md.

    Args:
        sample (pd.DataFrame): the sample.
    Returns:
        str: 16 hex characters.
    """
    s = sample[["firm_id", "role"]].sort_values(["role", "firm_id"]).to_csv(index=False)
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.build_sample``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(prog="python -m src.build_sample",
                                     description="Define and freeze the Week 4 analysis sample")
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR,
                        help="where order_issuers, entity_matches and firms are read (orders always from derived/)")
    parser.add_argument("--scratch", type=pathlib.Path, help="dry run: write everything under DIR")
    parser.add_argument("--controls", type=int, default=N_CONTROLS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--force", action="store_true", help="replace a frozen sample (it should never change)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run Week 4 Day 3: build and freeze the sample (refuses to replace a frozen one).

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0, or 1 if the sample is already frozen.
    """
    args = build_arg_parser().parse_args(argv)
    out_dir = args.scratch or DERIVED_DIR
    frozen = (args.scratch / "sample_frozen.csv") if args.scratch else FROZEN
    outs = {k: out_dir / f"{k}.parquet" for k in ("sample", "sample_fraud_years", "sample_events")}
    for p in [*outs.values(), frozen]:
        assert_safe_output(p)
    setup_logging(args.log_level, None if args.scratch else LOG_FILE)
    if (outs["sample"].exists() or frozen.exists()) and not args.force:
        log.error("the sample is frozen (%s); it must not change. --force only to correct a build error", frozen)
        return 1

    d = args.derived
    orders = pd.read_parquet(DERIVED_DIR / "orders.parquet",
                             columns=["text_stem", "order_date", "order_type", "violation_start", "violation_end",
                                      "window_kind", "violation_method", "fraud_category", "earliest_interim_date"])
    sample, years, ev = build(orders, pd.read_parquet(d / "order_issuers.parquet"),
                              pd.read_parquet(d / "entity_matches.parquet"), pd.read_parquet(d / "firms.parquet"),
                              args.controls, args.seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    sample.to_parquet(outs["sample"], index=False)
    years.to_parquet(outs["sample_fraud_years"], index=False)
    ev.to_parquet(outs["sample_events"], index=False)
    frozen.parent.mkdir(parents=True, exist_ok=True)
    sample[["firm_id", "role", "name", "weight"]].to_csv(frozen, index=False)

    fr, ct = sample[sample.role == "fraud"], sample[sample.role == "control"]
    yp = years[years.in_period]
    log.info("fraud firms        %d (%s); with a firm-year in FY%d–%d: %d", len(fr), fr.status.value_counts().to_dict(),
             *PERIOD_FY, yp.firm_id.nunique())
    log.info("fraud firm-years   %d; in period %d; issuer a noticee %d; stated period only %d", len(years), len(yp),
             int(yp.issuer_noticee.sum()), int(yp.stated_period_only.sum()))
    log.info("control firms      %d of %d eligible (weight %.2f); %s", len(ct),
             int(ct.eligible_controls.iloc[0]) if len(ct) else 0, ct.weight.iloc[0] if len(ct) else 0,
             ct.status.value_counts().to_dict())
    log.info("events             %d (%s); from %s to %s", len(ev), ev.kind.value_counts().to_dict(),
             ev.event_date.min(), ev.event_date.max())
    log.info("fingerprint        %s; wrote %s and %s", fingerprint(sample), ", ".join(map(str, outs.values())), frozen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
