"""Week 4 weekend: merge the sample, prices and ownership into the firm-year panel.

Runbook: "Join everything into derived/panel.parquet. Assert row counts at every join …
Print coverage: what share of fraud firms have prices, have shareholding, have both.
This number goes in the paper."

One row per (sample firm, financial year) from FY2010 to FY2025 (the plan's period), for
every year the firm traded or was in a violation window. Columns:

* the label: ``fraud_year`` (the year overlaps a violation window), ``categories``,
  ``issuer_noticee``, ``overlap_days``; ``first_public_fy`` (the year of the firm's first
  public SEBI action: the earliest order or recited interim order) and ``post_public``
  (later years, when predictors are contaminated by detection — Stage 2 drops them);
* market signals from the adjusted daily prices of the firm's main listing (the exchange
  where it traded on more days; NSE on a tie): ``trading_days``, ``ret`` (financial-year
  price return), ``excess_ret`` (minus the BSE 500's), ``vol`` (daily return s.d.),
  ``turnover_median`` (₹ traded on a median day), ``amihud`` (mean |return| per ₹ crore
  traded), ``zero_volume_share``, ``close_end``, ``gap_days`` (unexplained jumps);
* ownership at the financial year's end (the March quarter): ``promoter_pct``,
  ``pledged_pct``, ``pledged_shares``, ``total_shares``, ``market_cap`` (``close_end`` ×
  ``total_shares``, March closes, in ₹ crore) and the change in promoter holding;
* the sample design: ``role``, ``weight`` (controls stand for ``weight`` firms each).

Securities map: a BSE scrip belongs to its firm through the universe; an NSE symbol
through the ISIN printed beside it (from about 2011), else the universe's current
symbols and NSE's symbol changes.
"""

from __future__ import annotations

import argparse
import logging
import pathlib

import numpy as np
import pandas as pd

from src import cache
from src.build_sample import PERIOD_FY, fy
from src.extract_text import assert_safe_output, setup_logging
from src.prices import adjust, factors

log = logging.getLogger("build_panel")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
LOG_FILE = REPO_ROOT / "logs" / "build_panel.log"
CRORE = 1e7


def _fy_series(dates) -> pd.Series:
    """The financial year of each date.

    Args:
        dates: dates (list, array or Series).
    Returns:
        pd.Series: int years (April–March, named by the end year).
    """
    d = pd.to_datetime(pd.Series(dates))
    return d.dt.year + (d.dt.month >= 4).astype(int)


def security_map(universe: pd.DataFrame, nse_isin_rows: pd.DataFrame, symbolchange: dict[str, str]) -> pd.DataFrame:
    """(exchange, code) → firm_id. NSE symbols: the ISIN beside them first, then today's list and symbol changes.

    Args:
        universe (pd.DataFrame): Day 1's securities.
        nse_isin_rows (pd.DataFrame): (code, isin) pairs seen in NSE price files.
        symbolchange (dict[str, str]): old NSE symbol → latest symbol.
    Returns:
        pd.DataFrame: exchange, code, firm_id, how (universe / isin / symbol_change).
    """
    bse = universe[universe.exchange == "BSE"][["code", "firm_id"]].assign(exchange="BSE", how="universe")
    by_isin = (nse_isin_rows.dropna(subset=["isin"]).assign(firm_id=lambda d: "INE" + d["isin"].str[3:7])
               .drop_duplicates("code", keep="last")[["code", "firm_id"]].assign(exchange="NSE", how="isin"))
    current = universe[universe.exchange == "NSE"][["code", "firm_id"]].assign(exchange="NSE", how="universe")
    cur = dict(zip(current.code, current.firm_id))
    old = pd.DataFrame([{"code": o, "firm_id": cur[n], "exchange": "NSE", "how": "symbol_change"}
                        for o, n in symbolchange.items() if n in cur])
    m = pd.concat([bse, by_isin, current, old], ignore_index=True)
    return m.drop_duplicates(["exchange", "code"], keep="first").reset_index(drop=True)


def read_prices(prices_dir: pathlib.Path, codes: dict[str, set[str]], years: range) -> pd.DataFrame:
    """Read the price rows of some codes over some years.

    Args:
        prices_dir (pathlib.Path): ``derived/prices``.
        codes (dict[str, set[str]]): exchange → codes to read.
        years (range): calendar years of files to read.
    Returns:
        pd.DataFrame: the matching rows (empty with the main columns if none).
    """
    frames = []
    for ex, cs in codes.items():
        for y in years:
            p = prices_dir / f"{ex}_{y}.parquet"
            if p.exists() and cs:
                frames.append(pd.read_parquet(p, filters=[("code", "in", sorted(cs))]))
    if not frames:
        return pd.DataFrame(columns=["date", "exchange", "code", "series", "isin", "close", "volume", "turnover"])
    return pd.concat(frames, ignore_index=True)


def firm_daily(prices: pd.DataFrame, smap: pd.DataFrame, ca: pd.DataFrame, firm_bse: dict[str, list[str]]):
    """Adjusted daily rows per firm on its main exchange.

    Args:
        prices (pd.DataFrame): price rows of the sample's securities.
        smap (pd.DataFrame): the security map.
        ca (pd.DataFrame): corporate actions.
        firm_bse (dict[str, list[str]]): firm_id → its BSE codes (for the actions).
    Returns:
        tuple[pd.DataFrame, pd.Series]: adjusted daily rows of each firm's main exchange, and firm_id →
        main exchange.
    """
    p = prices.merge(smap[["exchange", "code", "firm_id"]], on=["exchange", "code"], how="inner", validate="many_to_one")
    p = p.sort_values(["firm_id", "exchange", "date", "volume"], ascending=[True, True, True, False]) \
         .drop_duplicates(["firm_id", "exchange", "date"])
    days = p[p.volume > 0].groupby(["firm_id", "exchange"]).size().unstack(fill_value=0)
    for ex in ("NSE", "BSE"):
        if ex not in days:
            days[ex] = 0
    main = pd.Series(np.where(days.NSE >= days.BSE, "NSE", "BSE"), index=days.index, name="main_exchange")
    p = p.merge(main, left_on="firm_id", right_index=True)
    p = p[p.exchange == p.main_exchange]
    out = []
    for fid, g in p.groupby("firm_id", sort=False):
        a = adjust(g, factors(ca, firm_bse.get(fid, [])))
        out.append(a)
    return (pd.concat(out, ignore_index=True) if out else p.iloc[0:0]), main


def market_signals(daily: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    """Per firm and financial year: trading, return, risk and liquidity from adjusted daily rows.

    Args:
        daily (pd.DataFrame): from firm_daily.
        index (pd.DataFrame): the BSE 500 (date, close).
    Returns:
        pd.DataFrame: firm_id, fy, trading_days, close_end, vol, turnover_median, gap_days, zero_volume_share,
        exchange, code, amihud, ret, index_ret, excess_ret.
    """
    if daily.empty:
        return pd.DataFrame(columns=["firm_id", "fy"])
    d = daily.assign(fy=_fy_series(daily.date).to_numpy())
    d["traded_cr"] = d.turnover / CRORE
    g = d.sort_values("date").groupby(["firm_id", "fy"])
    out = g.agg(trading_days=("volume", lambda v: int((v > 0).sum())), close_end=("close", "last"),
                adj_start=("adj_close", "first"), adj_end=("adj_close", "last"), vol=("ret", "std"),
                turnover_median=("turnover", "median"), gap_days=("gap", "sum"),
                zero_volume_share=("volume", lambda v: float((v == 0).mean())),
                exchange=("exchange", "first"), code=("code", "first"))
    d["illiq"] = d.ret.abs() / d.traded_cr.where(d.traded_cr > 0)
    out["amihud"] = d.groupby(["firm_id", "fy"]).illiq.mean()
    # the year's return runs from the last close of the previous year where there is one
    prev_end = out.adj_end.groupby(level=0).shift(1)
    out["ret"] = out.adj_end / prev_end.fillna(out.adj_start) - 1
    ix = index.assign(fy=_fy_series(index.date).to_numpy()).sort_values("date").groupby("fy").close.last()
    out["index_ret"] = out.index.get_level_values("fy").map(ix / ix.shift(1) - 1)
    out["excess_ret"] = out.ret - out.index_ret
    return out.drop(columns=["adj_start", "adj_end"]).reset_index()


def ownership(shp: pd.DataFrame) -> pd.DataFrame:
    """Ownership at each financial year's end (the March quarter).

    Args:
        shp (pd.DataFrame): ``derived/shareholding.parquet``.
    Returns:
        pd.DataFrame: firm_id, fy, promoter_pct, promoter_pct_change, pledged_pct, pledged_shares,
        total_shares, pledge_answer.
    """
    s = shp[(shp.status == "ok") & (pd.to_datetime(shp.quarter_end).dt.month == 3)].copy()
    s["fy"] = pd.to_datetime(s.quarter_end).dt.year
    s = s.sort_values(["firm_id", "fy"]).drop_duplicates(["firm_id", "fy"], keep="last")
    s["promoter_pct_change"] = s.groupby("firm_id").promoter_pct.diff()
    return s[["firm_id", "fy", "promoter_pct", "promoter_pct_change", "pledged_pct", "pledged_shares", "total_shares",
              "pledge_answer"]]


def first_public(events: pd.DataFrame) -> pd.Series:
    """The financial year of each firm's first public SEBI action (its earliest event).

    Args:
        events (pd.DataFrame): sample_events.
    Returns:
        pd.Series: firm_id → first_public_fy.
    """
    return events.groupby("firm_id").event_date.min().map(fy).rename("first_public_fy")


def build(sample, fraud_years, events, universe, symbolchange, prices_dir, ca, index, shp,
          period=PERIOD_FY) -> pd.DataFrame:
    """Merge the panel, asserting the row count after every join.

    Args:
        sample, fraud_years, events (pd.DataFrame): Day 3's frozen tables.
        universe (pd.DataFrame): Day 1's securities.
        symbolchange (dict[str, str]): NSE symbol changes.
        prices_dir (pathlib.Path): ``derived/prices``.
        ca (pd.DataFrame), index (pd.DataFrame): corporate actions and the BSE 500.
        shp (pd.DataFrame): shareholding.
        period (tuple[int, int]): the financial years kept (2010, 2025).
    Returns:
        pd.DataFrame: one row per (firm, fy) traded or in a window, with the label, signals and ownership.
    """
    years = range(period[0] - 1, period[1] + 1)
    firm_bse = universe[universe.exchange == "BSE"].groupby("firm_id").code.agg(list).to_dict()
    nse_isin_rows = []
    for y in years:
        p = prices_dir / f"NSE_{y}.parquet"
        if p.exists():
            nse_isin_rows.append(pd.read_parquet(p, columns=["code", "isin"]).dropna().drop_duplicates())
    nse_isin = pd.concat(nse_isin_rows) if nse_isin_rows else pd.DataFrame(columns=["code", "isin"])
    smap = security_map(universe, nse_isin, symbolchange)
    smap = smap[smap.firm_id.isin(set(sample.firm_id))]
    codes = {ex: set(smap[smap.exchange == ex].code) for ex in ("NSE", "BSE")}
    daily, main = firm_daily(read_prices(prices_dir, codes, years), smap, ca, firm_bse)
    signals = market_signals(daily, index)

    # the rows: every in-period year a sample firm traded or was in a window
    keys = pd.concat([signals[["firm_id", "fy"]], fraud_years[["firm_id", "fy"]]], ignore_index=True)
    keys = keys[keys.fy.between(*period) & keys.firm_id.isin(set(sample.firm_id))].drop_duplicates()
    n = len(keys)
    panel = keys.merge(sample[["firm_id", "role", "name", "status", "weight"]], on="firm_id", how="left",
                       validate="many_to_one")
    assert len(panel) == n
    fy_cols = ["firm_id", "fy", "overlap_days", "n_orders", "categories", "issuer_noticee", "stated_period_only"]
    panel = panel.merge(fraud_years[fy_cols], on=["firm_id", "fy"], how="left", validate="one_to_one")
    assert len(panel) == n
    panel["fraud_year"] = panel.overlap_days.notna()
    panel = panel.merge(first_public(events), left_on="firm_id", right_index=True, how="left", validate="many_to_one")
    panel["post_public"] = panel.first_public_fy.notna() & (panel.fy > panel.first_public_fy)
    panel = panel.merge(signals, on=["firm_id", "fy"], how="left", validate="one_to_one")
    assert len(panel) == n
    panel = panel.merge(ownership(shp), on=["firm_id", "fy"], how="left", validate="one_to_one")
    assert len(panel) == n
    panel["market_cap"] = panel.close_end * panel.total_shares / CRORE
    panel["main_exchange"] = panel.firm_id.map(main)
    return panel.sort_values(["role", "firm_id", "fy"]).reset_index(drop=True)


def coverage(panel: pd.DataFrame, sample: pd.DataFrame) -> pd.DataFrame:
    """The paper's coverage table: share of firms (and fraud firm-years) with prices, ownership, both.

    Args:
        panel (pd.DataFrame): the panel.
        sample (pd.DataFrame): the sample.
    Returns:
        pd.DataFrame: unit, n, with_prices, with_ownership, with_both, and their shares.
    """
    has_p = panel.trading_days.fillna(0) > 0
    has_o = panel.promoter_pct.notna()
    rows = []
    for label, mask in (("fraud firms", sample.role == "fraud"), ("control firms", sample.role == "control")):
        firms = set(sample[mask].firm_id)
        sub = panel[panel.firm_id.isin(firms)]
        fp = set(sub[has_p.loc[sub.index]].firm_id)
        fo = set(sub[has_o.loc[sub.index]].firm_id)
        rows.append({"unit": label, "n": len(firms), "with_prices": len(fp), "with_ownership": len(fo),
                     "with_both": len(fp & fo)})
    fyrs = panel[panel.fraud_year]
    for label, sub in (("fraud firm-years", fyrs), ("fraud firm-years FY2016+", fyrs[fyrs.fy >= 2016])):
        p, o = has_p.loc[sub.index], has_o.loc[sub.index]
        rows.append({"unit": label, "n": len(sub), "with_prices": int(p.sum()), "with_ownership": int(o.sum()),
                     "with_both": int((p & o).sum())})
    t = pd.DataFrame(rows)
    for c in ("with_prices", "with_ownership", "with_both"):
        t[c + "_share"] = (t[c] / t.n).round(3)
    return t


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.build_panel``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(prog="python -m src.build_panel", description="Merge the Week 4 panel")
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR, help="where every input is read")
    parser.add_argument("--scratch", type=pathlib.Path, help="write the panel and coverage under DIR")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the Week 4 merge: write ``panel.parquet`` and ``panel_coverage.csv``.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0.
    """
    args = build_arg_parser().parse_args(argv)
    out_dir = args.scratch or DERIVED_DIR
    outs = {"panel": out_dir / "panel.parquet", "coverage": out_dir / "panel_coverage.csv"}
    for p in outs.values():
        assert_safe_output(p)
    setup_logging(args.log_level, None if args.scratch else LOG_FILE)
    d = args.derived
    from src.universe import NSE_SYMBOLCHANGE, _csv, symbol_chain
    symbolchange = symbol_chain(_csv(cache.get(NSE_SYMBOLCHANGE, offline=True)))
    shp_path = d / "shareholding.parquet"
    shp = pd.read_parquet(shp_path) if shp_path.exists() else pd.DataFrame(
        columns=["firm_id", "quarter_end", "status", "promoter_pct", "pledged_pct", "pledged_shares", "total_shares",
                 "pledge_answer"])
    sample = pd.read_parquet(d / "sample.parquet")
    panel = build(sample, pd.read_parquet(d / "sample_fraud_years.parquet"), pd.read_parquet(d / "sample_events.parquet"),
                  pd.read_parquet(d / "universe.parquet"), symbolchange, d / "prices",
                  pd.read_parquet(d / "corporate_actions.parquet"), pd.read_parquet(d / "market_index.parquet"), shp)
    cov = coverage(panel, sample)
    out_dir.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(outs["panel"], index=False)
    cov.to_csv(outs["coverage"], index=False)
    log.info("panel %d firm-years (%s); fraud years %d; post-public %d", len(panel), panel.role.value_counts().to_dict(),
             int(panel.fraud_year.sum()), int(panel.post_public.sum()))
    log.info("coverage\n%s", cov.to_string(index=False))
    log.info("wrote %s", ", ".join(map(str, outs.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
