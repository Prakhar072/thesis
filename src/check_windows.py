"""Week 2, Day 3: sanity rules on the violation windows, and the extraction rate.

Reads ``derived/violation_windows.parquet`` (regex + model windows) and writes

* ``derived/windows_checked.parquet`` — one row per order: the window if it
  passes the rules (or was accepted on review), else no window. Flagged
  windows keep their dates in ``flagged_start``/``flagged_end`` but are *not* in
  the dataset (runbook: "not into the dataset").
* ``derived/window_flagged.csv`` — every flagged window, for hand review.
* ``derived/extraction_rate.csv`` — the extraction rate overall and by method,
  window kind, order type and year, with the drop-out test.
* ``hand/window_review.csv`` — the review sheet. Newly flagged orders are
  appended with an empty ``decision``; existing rows (your decisions) are never
  changed. Decisions: ``keep`` (the window is right), ``fix`` (give
  ``violation_start``/``violation_end``; stored as ``violation_method = hand``),
  ``drop`` (no window).

The rules (runbook Week 2, Day 3)
---------------------------------
R1 ``end_after_order``      the window ends after the order date;
R2 ``start_after_end``      the window starts after it ends;
R3 ``lag_not_positive``     order date − window end ≤ 0;
R3 ``lag_20_years``         order date − window end ≥ 20 years;
R4 ``start_before_1995``    the window starts before 1995.

R1–R3 give a month-precision end the benefit of its month: an end stated as
"March 2023" (resolved to 31 March) in an order of 1 March 2023 is consistent
with the order, so it is not flagged. Years and financial years get no such
allowance. R1–R2 and ``lag_not_positive`` are
impossible; ``lag_20_years`` and ``start_before_1995`` are only unusual (the
corpus has genuine 2021–23 orders on 1998–2002 trading, and 2003 orders on 1994
price rigging), so their severity is ``implausible`` and a review will
usually keep them.

This stage reads only ``derived/`` and ``hand/``; no network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import pathlib
import re

import numpy as np
import pandas as pd

from src import cache
from src.extract_text import assert_safe_output, setup_logging, text_stem

log = logging.getLogger("check_windows")

REPO_ROOT = cache.REPO_ROOT
WINDOWS_IN = REPO_ROOT / "derived" / "violation_windows.parquet"
LISTINGS_IN = REPO_ROOT / "derived" / "listings.parquet"
DERIVED_DIR = REPO_ROOT / "derived"
REVIEW = REPO_ROOT / "hand" / "window_review.csv"
LOGS_DIR = REPO_ROOT / "logs"

MIN_YEAR = 1995
MAX_LAG_YEARS = 20

IMPOSSIBLE, IMPLAUSIBLE = "impossible", "implausible"
RULES = {
    "end_after_order": IMPOSSIBLE,
    "start_after_end": IMPOSSIBLE,
    "lag_not_positive": IMPOSSIBLE,
    "lag_20_years": IMPLAUSIBLE,
    "start_before_1995": IMPLAUSIBLE,
}

# sanity_status values
PASS, FLAGGED, NO_WINDOW = "PASS", "FLAGGED", "NO_WINDOW"
KEEP, FIX, DROP = "keep", "fix", "drop"
REVIEWED = {KEEP: "REVIEWED_KEEP", FIX: "REVIEWED_FIX", DROP: "REVIEWED_DROP"}

#: ``suggested`` is an optional pre-reading (e.g. an agent's) — never applied;
#: only ``decision`` is.
REVIEW_COLUMNS = ["text_stem", "decision", "violation_start", "violation_end", "note",
                  "suggested", "flags", "order_date", "flagged_window", "violation_method", "sentence"]


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def earliest_end(end: dt.date, precision: str | None) -> dt.date:
    """The earliest date an end stated at ``precision`` can mean, for R1/R3.

    Only a month gets this benefit ("to March 2023" in an order of 1 March
    2023). A whole year or financial year does not: "FY 2021-22" in an order of
    28 March 2022 (57262, a Union Budget year quoted in an insider-trading
    order) should be looked at, not waved through.

    Args:
        end (datetime.date): the window's end.
        precision (str | None): its precision (day, month, year, fy).
    Returns:
        datetime.date: the 1st of the month for a month-precision end, else ``end``.
    """
    if precision == "month":
        return end.replace(day=1)
    return end


def flags_for(start: dt.date, end: dt.date, end_precision: str | None,
              order_date: dt.date | None) -> list[str]:
    """The sanity rules a window breaks (R1–R4).

    Args:
        start (datetime.date), end (datetime.date): the window.
        end_precision (str | None): the end's precision.
        order_date (datetime.date | None): the order's date.
    Returns:
        list[str]: flags among end_after_order, start_after_end, lag_not_positive, lag_20_years,
        start_before_1995 (empty = passes).
    """
    out = []
    if start > end:
        out.append("start_after_end")
    if order_date is not None:
        e = earliest_end(end, end_precision)
        if e > order_date:
            out.append("end_after_order")
        elif e == order_date:
            out.append("lag_not_positive")
        if (order_date - end).days >= MAX_LAG_YEARS * 365.25:
            out.append("lag_20_years")
    if start.year < MIN_YEAR:
        out.append("start_before_1995")
    return out


def severity(flags: list[str]) -> str | None:
    """The worst severity among a window's flags.

    Args:
        flags (list[str]): its flags.
    Returns:
        str | None: "impossible", "implausible", or None when there are none.
    """
    if not flags:
        return None
    return IMPOSSIBLE if any(RULES[f] == IMPOSSIBLE for f in flags) else IMPLAUSIBLE


# --------------------------------------------------------------------------
# Review sheet
# --------------------------------------------------------------------------

def read_review(path: pathlib.Path) -> pd.DataFrame:
    """Read ``hand/window_review.csv`` (all text; missing columns added empty).

    Args:
        path (pathlib.Path): the review sheet.
    Returns:
        pd.DataFrame: one row per reviewed window, REVIEW_COLUMNS.
    """
    if not path.exists():
        return pd.DataFrame(columns=REVIEW_COLUMNS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in REVIEW_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df


#: Spreadsheets re-save dates in the local format; this one writes 29-12-2005.
#: Read day-first, like the orders; write back as YYYY-MM-DD.
_DAY_FIRST = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})")


def _date(text: str) -> dt.date | None:
    """A review date: YYYY-MM-DD, or DD-MM-YYYY / DD/MM/YYYY as a spreadsheet saves it.

    Args:
        text (str): a cell.
    Returns:
        datetime.date | None: the date, or None if it is not one.
    """
    t = (text or "").strip()
    try:
        return dt.date.fromisoformat(t)
    except ValueError:
        pass
    if m := _DAY_FIRST.fullmatch(t):
        try:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _iso(text: str) -> str:
    """A date cell normalised to YYYY-MM-DD; anything else left as it is.

    Args:
        text (str): a cell.
    Returns:
        str: YYYY-MM-DD if the cell is a date, else the cell unchanged.
    """
    d = _date(text)
    return d.isoformat() if d else text


def decisions(review: pd.DataFrame) -> tuple[dict[str, tuple], list[str]]:
    """Valid decisions by text_stem, and messages for the invalid ones.

    Args:
        review (pd.DataFrame): the review sheet.
    Returns:
        tuple[dict[str, tuple], list[str]]: text_stem → (decision, start, end, note) for valid rows, and one
        message per invalid row.
    """
    out, problems = {}, []
    for r in review.itertuples():
        d = (r.decision or "").strip().lower()
        if not d:
            continue
        if d not in (KEEP, FIX, DROP):
            problems.append(f"{r.text_stem}: unknown decision {r.decision!r} (use keep / fix / drop)")
            continue
        if d == FIX:
            a, b = _date(r.violation_start), _date(r.violation_end)
            if a is None or b is None or a > b:
                problems.append(f"{r.text_stem}: 'fix' needs dates (YYYY-MM-DD or DD-MM-YYYY) "
                                f"with violation_start <= violation_end; got {r.violation_start!r} .. "
                                f"{r.violation_end!r}")
                continue
            out[r.text_stem] = (FIX, a, b, r.note)
        else:
            out[r.text_stem] = (d, None, None, r.note)
    return out, problems


def update_review(review: pd.DataFrame, flagged: pd.DataFrame) -> pd.DataFrame:
    """Append newly flagged orders; never touch existing rows.

    Args:
        review (pd.DataFrame): the current sheet.
        flagged (pd.DataFrame): every flagged window (with its original sentence and method).
    Returns:
        pd.DataFrame: the sheet with new flags appended and dates written as YYYY-MM-DD.
    """
    known = set(review.text_stem)
    new = flagged[~flagged.text_stem.isin(known)]
    rows = pd.DataFrame({
        "text_stem": new.text_stem, "decision": "", "violation_start": "", "violation_end": "",
        "note": "", "suggested": "", "flags": new.sanity_flags,
        "order_date": new.order_date.astype(str),
        "flagged_window": new.flagged_start.astype(str) + " .. " + new.flagged_end.astype(str),
        "violation_method": new.violation_method, "sentence": new.violation_raw,
    })
    out = pd.concat([review, rows], ignore_index=True)[REVIEW_COLUMNS]
    for col in ("violation_start", "violation_end", "order_date"):
        out[col] = out[col].astype(str).map(_iso)
    return out


# --------------------------------------------------------------------------
# Applying rules and reviews
# --------------------------------------------------------------------------

def _clear(w: pd.DataFrame, i) -> None:
    """A window that is not in the dataset carries nothing that says it is: the
    dates *and* its method, rule, kind, confidence and sentence are cleared (the
    flagged dates stay in ``flagged_start``/``flagged_end``, the sentence in
    ``window_flagged.csv``). Leaving ``violation_confidence = high`` on a row with
    no dates let a filter on confidence alone count it (AUDIT_REPORT.md M-9).

    Args:
        w (pd.DataFrame): the window table being checked (changed in place).
        i: the row's index label.
    Returns:
        None.
    """
    for col in ("violation_start", "violation_end", "start_precision", "end_precision", "violation_raw",
                "violation_method", "violation_rule", "window_kind", "char_start"):
        if col in w.columns:
            w.at[i, col] = None
    w.at[i, "violation_confidence"] = "low"


def check(windows: pd.DataFrame, decided: dict[str, tuple]) -> pd.DataFrame:
    """Apply the rules and the review decisions to every window.

    Args:
        windows (pd.DataFrame): ``violation_windows``, one row per order.
        decided (dict[str, tuple]): review decisions by text_stem (from decisions()).
    Returns:
        pd.DataFrame: the table plus sanity_status, sanity_flags, sanity_severity, flagged_start/end,
        review_note; flagged and dropped windows cleared, fixed ones carry the hand dates.
    """
    w = windows.copy()
    for col in ("flagged_start", "flagged_end", "sanity_flags", "sanity_severity", "review_note"):
        w[col] = pd.Series([None] * len(w), index=w.index, dtype=object)
    w["sanity_status"] = NO_WINDOW
    for i, r in w.iterrows():
        if r.violation_start is None or pd.isna(r.violation_start):
            continue
        f = flags_for(r.violation_start, r.violation_end, r.end_precision, r.order_date)
        if not f:
            w.at[i, "sanity_status"] = PASS
            continue
        w.at[i, "sanity_flags"] = ";".join(f)
        w.at[i, "sanity_severity"] = severity(f)
        w.at[i, "flagged_start"], w.at[i, "flagged_end"] = r.violation_start, r.violation_end
        decision = decided.get(r.text_stem)
        if decision is None:
            w.at[i, "sanity_status"] = FLAGGED
            _clear(w, i)
            continue
        kind, a, b, note = decision
        w.at[i, "sanity_status"] = REVIEWED[kind]
        w.at[i, "review_note"] = note or None
        if kind == DROP:
            _clear(w, i)
        elif kind == FIX:
            w.at[i, "violation_start"], w.at[i, "violation_end"] = a, b
            w.at[i, "start_precision"] = w.at[i, "end_precision"] = "day"
            w.at[i, "violation_method"], w.at[i, "violation_rule"] = "hand", "hand"
            w.at[i, "violation_confidence"] = "high"
    return w


# --------------------------------------------------------------------------
# Extraction rate
# --------------------------------------------------------------------------

def _cramers_v(table: pd.DataFrame) -> tuple[float, float]:
    """Cramér's V and the chi-square p-value of a contingency table.

    Args:
        table (pd.DataFrame): counts (e.g. order type × has-a-window).
    Returns:
        tuple[float, float]: (V, p).
    """
    from scipy.stats import chi2_contingency
    chi2, p, _, _ = chi2_contingency(table.values)
    n = table.values.sum()
    k = min(table.shape) - 1
    return float(np.sqrt(chi2 / (n * k))) if k > 0 else 0.0, float(p)


def extraction_rate(checked: pd.DataFrame, iso: pd.Series) -> tuple[pd.DataFrame, dict]:
    """Share of orders with text holding a high- or medium-confidence window.

    Args:
        checked (pd.DataFrame): the checked windows.
        iso (pd.Series): bool per row: an Illiquid Stock Options order.
    Returns:
        tuple[pd.DataFrame, dict]: the rate table (by, group, orders_with_window, orders, share), and
        {"rate", "verdict" (the runbook's band), "drop_out_tests" (Cramér's V by type and year)}.
    """
    t = checked[checked.text_status == "OK"].copy()
    t["has"] = t.violation_start.notna() & t.violation_confidence.isin(["high", "medium"])
    t["year_band"] = pd.cut(pd.to_datetime(t.order_date).dt.year, [1999, 2005, 2010, 2015, 2020, 2026],
                            labels=["2000-05", "2006-10", "2011-15", "2016-20", "2021-26"])
    t["iso"] = iso.reindex(t.index).fillna(False).astype(bool)
    rows = [("all", "all", t.has.sum(), len(t))]
    for col in ("order_type", "year_band"):
        for key, g in t.groupby(col, observed=True):
            rows.append((col, str(key), g.has.sum(), len(g)))
    non_iso = t[~t.iso]
    rows.append(("excluding", "Illiquid Stock Options orders", non_iso.has.sum(), len(non_iso)))
    with_window = t[t.has]
    for col in ("violation_method", "window_kind"):
        for key, g in with_window.groupby(col):
            rows.append((f"windows by {col}", str(key), len(g), len(with_window)))
    table = pd.DataFrame(rows, columns=["by", "group", "orders_with_window", "orders"])
    table["share"] = (table.orders_with_window / table.orders).round(4)

    rate = t.has.mean()
    verdict = ("above 70%: proceed as planned" if rate > 0.70 else
               "50-70%: proceed; report the drop-out and whether missing-window orders differ "
               "systematically by year or category" if rate >= 0.50 else
               "below 50%: Gate 1 fallback — hand-code a stratified 300-400")
    tests = {}
    for col in ("order_type", "year_band"):
        v, p = _cramers_v(pd.crosstab(t[col], t.has))
        tests[col] = {"cramers_v": round(v, 3), "p_value": p}
    return table, {"rate": round(float(rate), 4), "verdict": verdict, "drop_out_tests": tests}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def iso_flags(windows: pd.DataFrame, listings: pathlib.Path = LISTINGS_IN) -> pd.Series:
    """Which orders belong to the Illiquid Stock Options cluster (by title).

    Args:
        windows (pd.DataFrame): the window table (text_stem column).
        listings (pathlib.Path): ``derived/listings.parquet``.
    Returns:
        pd.Series: bool, aligned with ``windows``' index.
    """
    lst = pd.read_parquet(listings, columns=["order_id", "source_url", "title"], dtype_backend="numpy_nullable")
    lst["text_stem"] = [text_stem(int(o) if pd.notna(o) else None, u)
                        for o, u in zip(lst.order_id, lst.source_url)]
    titles = windows[["text_stem"]].merge(lst[["text_stem", "title"]], on="text_stem", how="left").title
    return titles.str.contains("illiquid stock option", case=False, na=False).set_axis(windows.index)


def _write_atomic_csv(df: pd.DataFrame, path: pathlib.Path) -> None:
    """Write a CSV through a temporary file and a rename.

    Args:
        df (pd.DataFrame): the table.
        path (pathlib.Path): the destination.
    Returns:
        None.
    """
    tmp = path.parent / f"{path.name}.tmp-{os.urandom(6).hex()}"
    df.to_csv(tmp, index=False)
    tmp.replace(path)


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.check_windows``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.check_windows",
        description="Sanity rules on violation windows, flagged file for review, extraction rate "
                    "(runbook Week 2, Day 3)",
    )
    parser.add_argument("--windows", type=pathlib.Path, default=WINDOWS_IN)
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: write everything, including the review sheet, under DIR")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run stage 6: rules, review decisions, the flagged file, the review sheet and the extraction rate.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0.
    """
    args = build_arg_parser().parse_args(argv)
    if args.scratch:
        out_dir, review_path = args.scratch, args.scratch / "window_review.csv"
        log_file = args.scratch / "check_windows.log"
    else:
        out_dir, review_path, log_file = DERIVED_DIR, REVIEW, LOGS_DIR / "check_windows.log"
    for p in (out_dir, review_path, log_file):
        assert_safe_output(p)
    setup_logging(args.log_level, log_file)
    out_dir.mkdir(parents=True, exist_ok=True)
    review_path.parent.mkdir(parents=True, exist_ok=True)

    windows = pd.read_parquet(args.windows)
    review = read_review(review_path)
    decided, problems = decisions(review)
    for msg in problems:
        log.warning("review sheet: %s", msg)

    checked = check(windows, decided)
    flagged_all = checked[checked.sanity_flags.notna()].copy()
    # the review sheet and the flagged file show what was flagged, which check()
    # has cleared from rows that are not in the dataset
    for col in ("violation_raw", "violation_method", "violation_rule", "window_kind", "start_precision",
                "end_precision"):
        flagged_all[col] = windows.loc[flagged_all.index, col]
    stale = sorted(set(decided) - set(flagged_all.text_stem))
    if stale:
        log.warning("%d review decisions are for orders no longer flagged (ignored): %s",
                    len(stale), ", ".join(stale[:10]))

    checked.to_parquet(out_dir / "windows_checked.parquet.tmp", index=False)
    (out_dir / "windows_checked.parquet.tmp").replace(out_dir / "windows_checked.parquet")
    flagged_cols = ["text_stem", "order_id", "order_type", "order_date", "sanity_flags",
                    "sanity_severity", "sanity_status", "flagged_start", "flagged_end",
                    "start_precision", "end_precision", "violation_method", "violation_rule",
                    "window_kind", "violation_raw"]
    _write_atomic_csv(flagged_all.sort_values(["sanity_severity", "sanity_flags", "text_stem"])[flagged_cols],
                      out_dir / "window_flagged.csv")
    _write_atomic_csv(update_review(review, flagged_all), review_path)

    table, summary = extraction_rate(checked, iso_flags(checked))
    _write_atomic_csv(table, out_dir / "extraction_rate.csv")

    log.info("=" * 72)
    log.info("windows in                   %d", int(windows.violation_start.notna().sum()))
    log.info("flagged                      %d  by rule: %s", len(flagged_all),
             flagged_all.sanity_flags.str.split(";").explode().value_counts().to_dict())
    log.info("flagged by severity          %s", flagged_all.sanity_severity.value_counts().to_dict())
    log.info("sanity_status                %s", checked.sanity_status.value_counts().to_dict())
    log.info("review decisions applied     %d (pending: %d)", len(decided) - len(stale),
             int((checked.sanity_status == FLAGGED).sum()))
    log.info("EXTRACTION RATE              %.1f%% of orders with text  ->  %s",
             summary["rate"] * 100, summary["verdict"])
    for _, r in table.iterrows():
        log.info("  %-32s %-30s %6d / %-6d %5.1f%%", r.by, r.group, r.orders_with_window, r.orders,
                 r.share * 100)
    for col, res in summary["drop_out_tests"].items():
        log.info("drop-out vs %-12s Cramér's V %.3f (p=%.2g)", col, res["cramers_v"], res["p_value"])
    log.info("wrote %s, %s, %s and the review sheet %s", out_dir / "windows_checked.parquet",
             out_dir / "window_flagged.csv", out_dir / "extraction_rate.csv", review_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
