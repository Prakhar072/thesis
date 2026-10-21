"""Week 4 Day 4: split- and bonus-adjusted prices and daily returns, and the runbook's sanity check.

Bhavcopy closes are not adjusted for corporate actions (``src/fetch_prices.py``). A split or
bonus with ex-date *d* and factor *f* (shares after / shares before) is applied on the first
day the security trades on or after *d*:

    r_t = f_t · close_t / close_{t-1} − 1,     adj_close_t = close_t / Π_{s > t} f_s

so returns are continuous and the adjusted close is in today's share units. Not adjusted:
consolidations (BSE's list gives no ratio; flagged ``action_unadjusted``), rights issues,
demergers and dividends — returns are price returns. A day whose return is above +100% or
below −50% after adjustment is flagged ``gap`` for Stage 3 to check or drop.

BSE's corporate actions are keyed by scrip code; an NSE series takes the factors of its
firm's BSE scrips (the same company and ex-date).

Sanity check (runbook Day 4: "plot one well-known scrip over ten years and look at it"):
``python -m src.prices --check TATASTEEL`` → ``figures/price_check_TATASTEEL.png``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import pathlib

import numpy as np
import pandas as pd

from src import cache

log = logging.getLogger("prices")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
FIGURES = REPO_ROOT / "figures"
#: NSE series order of preference when a symbol has two rows on one day.
SERIES_RANK = {"EQ": 0, "BE": 1, "BZ": 2, "BT": 3, "SM": 4, "ST": 5, "SZ": 6}
GAP_UP, GAP_DOWN = 1.0, -0.5


def load(exchange: str, codes, start: dt.date, end: dt.date, prices_dir: pathlib.Path = DERIVED_DIR / "prices"):
    """Daily rows for the given codes, one per code and date.

    Args:
        exchange (str): "NSE" or "BSE".
        codes: NSE symbols or BSE scrip codes.
        start (datetime.date), end (datetime.date): the range.
        prices_dir (pathlib.Path): ``derived/prices``.
    Returns:
        pd.DataFrame: one row per (code, date); the rolling series preferred when a symbol has two.
    """
    codes = [str(c) for c in codes]
    frames = []
    for y in range(start.year, end.year + 1):
        p = prices_dir / f"{exchange}_{y}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p, filters=[("code", "in", codes)]))
    if not frames:
        return pd.DataFrame(columns=["date", "exchange", "code", "series", "close", "volume"])
    d = pd.concat(frames, ignore_index=True)
    d = d[(d.date >= start) & (d.date <= end)]
    d = d.assign(_rank=d.series.map(SERIES_RANK).fillna(9)).sort_values(["code", "date", "_rank", "volume"],
                                                                         ascending=[True, True, True, False])
    return d.drop_duplicates(["code", "date"]).drop(columns="_rank").reset_index(drop=True)


def factors(ca: pd.DataFrame, bse_codes) -> pd.DataFrame:
    """Splits and bonuses with a factor, and consolidations (no factor), for the given BSE scrips.

    Args:
        ca (pd.DataFrame): corporate_actions.
        bse_codes: the firm's BSE scrip codes.
    Returns:
        pd.DataFrame: ex_date, kind, factor (NaN for consolidations), purpose — sorted by ex_date.
    """
    c = ca[ca.scrip_code.isin([str(x) for x in bse_codes]) & ca.kind.isin(["split", "bonus", "consolidation"])]
    return (c.sort_values("ex_date").drop_duplicates(["ex_date", "kind", "factor"])
             [["ex_date", "kind", "factor", "purpose"]].reset_index(drop=True))


def adjust(series: pd.DataFrame, fac: pd.DataFrame) -> pd.DataFrame:
    """One security's rows (date, close, …) → with factor, ret, adj_close, gap, action_unadjusted.

    Args:
        series (pd.DataFrame): one security's daily rows (date, close, …).
        fac (pd.DataFrame): its actions (from factors()).
    Returns:
        pd.DataFrame: the rows sorted by date, plus factor, ret, adj_close, gap, action_unadjusted.
    """
    d = series.sort_values("date").reset_index(drop=True).copy()
    f = np.ones(len(d))
    unadjusted = np.zeros(len(d), dtype=bool)
    dates = pd.to_datetime(d.date).to_numpy()
    for r in fac.itertuples():
        i = int(np.searchsorted(dates, np.datetime64(pd.Timestamp(r.ex_date))))
        if i == 0 or i >= len(d):
            continue    # the action falls outside the data: no return spans it
        if pd.notna(r.factor) and r.factor > 0:
            f[i] *= r.factor
        else:
            unadjusted[i] = True
    d["factor"] = f
    d["ret"] = d.close * f / d.close.shift(1) - 1
    later = np.concatenate([np.cumprod(f[::-1])[::-1][1:], [1.0]])   # Π of factors after each day
    d["adj_close"] = d.close / later
    d["gap"] = (d.ret > GAP_UP) | (d.ret < GAP_DOWN)
    d["action_unadjusted"] = unadjusted
    return d


def check_plot(d: pd.DataFrame, fac: pd.DataFrame, title: str, out: pathlib.Path) -> pathlib.Path:
    """Unadjusted close above, adjusted close below; ex-dates marked. One series per panel.

    Args:
        d (pd.DataFrame): adjusted rows (from adjust()).
        fac (pd.DataFrame): the actions, marked on the plot.
        title (str): the figure's title.
        out (pathlib.Path): the PNG to write.
    Returns:
        pathlib.Path: the path written.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    surface, ink, ink2, grid, series = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#2a78d6"
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True, facecolor=surface)
    dates = pd.to_datetime(d.date)
    for ax, col, label in ((axes[0], "close", "Close as traded (bhavcopy)"),
                           (axes[1], "adj_close", "Adjusted for splits and bonuses")):
        ax.set_facecolor(surface)
        ax.plot(dates, d[col], color=series, linewidth=1.5)
        values = d[col].dropna()
        log_scale = len(values) and values.max() / max(values.min(), 1e-9) > 3   # long ranges only
        if log_scale:
            ax.set_yscale("log")
            ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
            ax.yaxis.set_minor_formatter(NullFormatter())
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}" if v >= 1000 else f"{v:.4g}"))
        ax.set_ylabel("₹ (log scale)" if log_scale else "₹", color=ink2, fontsize=8)
        ax.set_title(label, loc="left", fontsize=10, color=ink)
        ax.grid(axis="y", color=grid, linewidth=0.8)
        ax.tick_params(colors=ink2, labelsize=8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(grid)
        for r in fac.itertuples():
            x = pd.Timestamp(r.ex_date)
            if dates.min() <= x <= dates.max():
                ax.axvline(x, color=ink2, linewidth=0.8, linestyle=(0, (3, 3)))
                if ax is axes[0]:
                    ax.annotate(f"{r.kind} ×{r.factor:g}" if pd.notna(r.factor) else r.kind, (x, 1), xycoords=("data",
                                "axes fraction"), xytext=(3, -12), textcoords="offset points", fontsize=8, color=ink2)
    locator = mdates.AutoDateLocator()
    axes[1].xaxis.set_major_locator(locator)
    axes[1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=ink)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130, facecolor=surface)
    plt.close(fig)
    return out


def bse_codes_for(code: str, exchange: str, universe: pd.DataFrame) -> list[str]:
    """The BSE scrips of the firm a code belongs to (for its corporate actions).

    Args:
        code (str): an NSE symbol or a BSE scrip code.
        exchange (str): which of the two it is.
        universe (pd.DataFrame): Day 1's securities.
    Returns:
        list[str]: the firm's BSE scrip codes (the code itself for an unknown BSE code).
    """
    fid = universe[(universe.exchange == exchange) & (universe.code == code)].firm_id
    if fid.empty:
        return [code] if exchange == "BSE" else []
    return universe[(universe.exchange == "BSE") & universe.firm_id.isin(fid)].code.tolist()


def main(argv: list[str] | None = None) -> int:
    """The sanity check: adjust one security's prices, plot raw and adjusted, log the biggest moves.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0, or 1 if there are no prices for the security in the range.
    """
    parser = argparse.ArgumentParser(prog="python -m src.prices", description="Adjusted prices: the sanity check")
    parser.add_argument("--check", required=True, help="an NSE symbol or a BSE scrip code")
    parser.add_argument("--exchange", choices=("NSE", "BSE"), default="NSE")
    parser.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2014, 1, 1))
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.date(2024, 12, 31))
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR,
                        help="where prices/, corporate_actions and universe are read")
    parser.add_argument("--out", type=pathlib.Path, help="figure path (default figures/price_check_<code>.png)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows = load(args.exchange, [args.check], args.start, args.end, args.derived / "prices")
    if rows.empty:
        log.error("no %s rows for %s between %s and %s", args.exchange, args.check, args.start, args.end)
        return 1
    ca = pd.read_parquet(args.derived / "corporate_actions.parquet")
    fac = factors(ca, bse_codes_for(args.check, args.exchange, pd.read_parquet(args.derived / "universe.parquet")))
    d = adjust(rows, fac)
    out = args.out or FIGURES / f"price_check_{args.check}.png"
    check_plot(d, fac, f"{args.check} ({args.exchange}), {d.date.min()} to {d.date.max()}", out)
    raw = d.close / d.close.shift(1) - 1
    log.info("%d trading days; actions in range:\n%s", len(d),
             fac[(fac.ex_date >= d.date.min()) & (fac.ex_date <= d.date.max())].to_string(index=False))
    log.info("largest unadjusted moves (close/prev close − 1):\n%s",
             d.assign(raw=raw).reindex(raw.abs().sort_values(ascending=False).index[:5])
             [["date", "close", "raw", "factor", "ret"]].to_string(index=False))
    log.info("days still beyond +100%%/−50%% after adjustment: %d; unadjusted actions: %d; wrote %s",
             int(d.gap.sum()), int(d.action_unadjusted.sum()), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
