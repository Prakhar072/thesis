"""Thesis tables and figures, built from ``derived/`` in one command.

``python -m src.paper_tables`` writes the LaTeX tables that ``paper/paper.tex`` inputs
(``paper/tables/*.tex``), the multi-page figure file it includes (``paper/figures.pdf``), and
every headline figure the prose quotes (``derived/paper_numbers.json``). Re-run it after any
change to the pipeline: the tables and figures update themselves, and the numbers file shows
which sentences in the text need changing.

Rules kept here:

* **Aggregates only.** The blind validation is not scored yet, so nothing here prints a
  per-order result for an order on the coding sheet.
* **Lags are descriptive.** They are order-level and unvalidated; nothing here estimates a
  model. ``first_action_date`` is the first *public* action, not the date SEBI learnt of a
  matter.
* **Colour.** Illiquid Stock Options orders are the only accent; every other order is neutral
  grey, so a chart reads the same in print and in greyscale.

Reads ``orders``, ``order_issuers``, ``firms``, ``universe``, ``name_matches``, ``price_days``,
``prices/``, ``corporate_actions``, ``validation_key`` and, once frozen, ``sample*`` (``--sample
DIR`` reads a dry run of the sample instead). No network.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import pathlib
import re

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.ticker import FuncFormatter, MaxNLocator

from src import cache
from src.extract_text import setup_logging

log = logging.getLogger("paper_tables")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
PAPER_DIR = REPO_ROOT / "paper"
LOG_FILE = REPO_ROOT / "logs" / "paper_tables.log"

#: The listing was scraped on this day; no order or lag can be observed after it.
CORPUS_END = pd.Timestamp("2026-09-23")
YEAR_DAYS = 365.25
CRORE = 1e7
LAKH = 1e5
#: The regime indicators of Stage 1: windows ending on or after these dates.
LODR_START = pd.Timestamp("2015-12-01")
NFRA_START = pd.Timestamp("2018-11-13")
#: Discount rates used only to illustrate the delay term (runbook Week 9: 8–12%).
DISCOUNT_RATES = (0.08, 0.12)

ERAS = ((2000, 2005), (2006, 2010), (2011, 2015), (2016, 2020), (2021, 2026))
TYPE_LABELS = {"WTM": "Chairperson/Members", "AO": "Adjudicating Officer",
               "SETTLEMENT": "Settlement", "ED_CGM": "ED/CGM"}
CATEGORY_LABELS = {"MARKET_MANIPULATION": "Market manipulation",
                   "DISCLOSURE_FAILURE": "Disclosure failure",
                   "INTERMEDIARY_MISCONDUCT": "Intermediary misconduct",
                   "INSIDER_TRADING": "Insider trading",
                   "FUND_DIVERSION": "Fund diversion",
                   "ACCOUNTING_MISSTATEMENT": "Accounting misstatement",
                   "RPT_TUNNELLING": "RPT and tunnelling",
                   "OTHER": "Other"}
CHANNEL_LABELS = {"SURVEILLANCE": "Surveillance", "COMPLAINT": "Complaint",
                  "INSPECTION": "Inspection", "REFERRAL": "Referral", "MEDIA": "Media",
                  "SELF_DISCLOSURE": "Self-disclosure", "AUDIT": "Audit"}
STRATUM_LABELS = {"regex": "Regex window", "model_stated": "Model window, stated period",
                  "model_conduct": "Model window, conduct dates", "no_window": "No window"}
#: Listed-company frauds in the sense of the plan: the categories with an issuer at their centre.
ISSUER_CATEGORIES = ("INSIDER_TRADING", "FUND_DIVERSION", "RPT_TUNNELLING",
                     "ACCOUNTING_MISSTATEMENT", "MARKET_MANIPULATION", "DISCLOSURE_FAILURE")

#: A direction to pay a penalty near the end of an order. Where the order has no penalty read,
#: this clause marks one the amount reader missed (see ``unread_penalties``).
PAY_CLAUSE = re.compile(r"shall\s+(?:remit|pay)\s*/?\s*(?:pay)?\s*(?:the\s+)?(?:said\s+)?"
                        r"(?:amount\s+of\s+)?penalt", re.I)

INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#e4e3dd"
NEUTRAL = "#bdbcb5"
NEUTRAL_DARK = "#7f7e78"
ACCENT = "#2a78d6"
FONT_DIR = pathlib.Path("/usr/share/texlive/texmf-dist/fonts/opentype/adobe/sourceserifpro")


# ----- reading -------------------------------------------------------------------------------


def years_between(later: pd.Series, earlier: pd.Series) -> pd.Series:
    """Elapsed years between two date columns.

    Args:
        later (pd.Series): datetime column, the end of the span.
        earlier (pd.Series): datetime column, the start of the span.

    Returns:
        pd.Series: float years (days / 365.25); NaN where either date is missing.
    """
    return (later - earlier).dt.days / YEAR_DAYS


def era_of(year: float) -> str | None:
    """The reporting era an order year falls in.

    Args:
        year (float): calendar year, or NaN.

    Returns:
        str | None: a label such as ``"2006--2010"``, or None outside every era.
    """
    if pd.isna(year):
        return None
    for start, end in ERAS:
        if start <= year <= end:
            return f"{start}--{end}"
    return None


def load_orders(derived: pathlib.Path = DERIVED_DIR) -> pd.DataFrame:
    """The order table with the flags and lags every table shares.

    Args:
        derived (pathlib.Path): directory holding ``orders`` and ``order_issuers``.

    Returns:
        pd.DataFrame: one row per order, with ``orders.parquet``'s columns plus ``year``, ``era``,
            ``has_text``, ``has_window``, ``iso``, ``stated`` (a channel other than UNCLEAR),
            ``issuer_firm_id``, ``matched``, and the lags in years: ``t_first`` (window end →
            first action), ``t_resolve`` (first action → order), ``t_order`` (window end →
            order), ``t_scn`` (window end → first SCN, or the order where none is recited).
    """
    orders = pd.read_parquet(derived / "orders.parquet")
    issuers = pd.read_parquet(derived / "order_issuers.parquet",
                              columns=["text_stem", "issuer_firm_id", "match_method"])
    frame = orders.merge(issuers, on="text_stem", how="left", validate="one_to_one")
    assert len(frame) == len(orders), "the issuer join changed the row count"
    for column in ("order_date", "violation_start", "violation_end", "first_action_date",
                   "earliest_scn_date", "prior_action_date"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in ("penalty_total", "settlement_amount", "disgorgement_total", "n_pages"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    frame["year"] = frame.order_date.dt.year
    frame["era"] = frame.year.map(era_of)
    frame["has_text"] = frame.fraud_category.notna()
    frame["has_window"] = frame.violation_start.notna() & frame.violation_end.notna()
    frame["iso"] = (frame.title.str.contains("illiquid stock option", case=False, na=False)
                    | frame.violation_raw.str.contains("illiquid stock option", case=False,
                                                       na=False).fillna(False).astype(bool))
    frame["stated"] = frame.detection_channel.notna() & (frame.detection_channel != "UNCLEAR")
    frame["matched"] = frame.issuer_firm_id.notna()
    frame["t_first"] = years_between(frame.first_action_date, frame.violation_end)
    frame["t_resolve"] = years_between(frame.order_date, frame.first_action_date)
    frame["t_order"] = years_between(frame.order_date, frame.violation_end)
    scn = frame[["earliest_scn_date", "order_date"]].min(axis=1)
    frame["t_scn"] = years_between(scn, frame.violation_end)
    return frame


def cluster_key(frame: pd.DataFrame) -> pd.Series:
    """A crude matter key: the issuer and the exact window, with all ISO orders as one.

    Args:
        frame (pd.DataFrame): orders with ``issuer_firm_id``, ``issuer_guess``,
            ``violation_start``, ``violation_end`` and ``iso``.

    Returns:
        pd.Series: str key per row. Orders with no issuer are grouped by window alone.
    """
    issuer = frame.issuer_firm_id.astype("object").where(frame.issuer_firm_id.notna(),
                                                         frame.issuer_guess.astype("object"))
    issuer = issuer.fillna("").astype(str).str.upper().str.strip()
    key = (issuer + "|" + frame.violation_start.dt.strftime("%Y-%m-%d").fillna("")
           + "|" + frame.violation_end.dt.strftime("%Y-%m-%d").fillna(""))
    return key.where(~frame.iso, "ISO")


def clusters(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse windowed orders to matter clusters.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        pd.DataFrame: one row per cluster: ``n_orders``, ``window_end``, ``first_action``,
            ``last_order``, and the lags ``t_first`` (window end → earliest action in the
            cluster) and ``t_resolve`` (earliest action → latest order in the cluster).
    """
    windowed = frame[frame.has_window].copy()
    windowed["cluster"] = cluster_key(windowed)
    grouped = windowed.groupby("cluster").agg(n_orders=("text_stem", "size"),
                                              window_end=("violation_end", "first"),
                                              first_action=("first_action_date", "min"),
                                              last_order=("order_date", "max"))
    grouped["t_first"] = years_between(grouped.first_action, grouped.window_end)
    grouped["t_resolve"] = years_between(grouped.last_order, grouped.first_action)
    return grouped


def channel_consistency(frame: pd.DataFrame) -> dict[str, float]:
    """How often orders of one matter cluster disagree about the detection channel.

    Orders with the same issuer and the same exact window describe the same examination, so two
    different stated channels there are a contradiction, whatever the true channel was.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        dict[str, float]: ``clusters`` (those with at least two orders stating a channel),
            ``disagree`` (of those, how many hold more than one channel), ``share_disagree``,
            and the same three with ``_rule`` for rule-coded orders only.
    """
    windowed = frame[frame.has_window & frame.stated & ~frame.iso].copy()
    windowed = windowed[windowed.issuer_firm_id.notna() | windowed.issuer_guess.notna()]
    windowed["cluster"] = cluster_key(windowed)

    def tally(rows: pd.DataFrame) -> tuple[int, int]:
        """Count clusters with two or more stated orders, and those that disagree.

        Args:
            rows (pd.DataFrame): stated orders with a ``cluster`` column.

        Returns:
            tuple[int, int]: clusters checked, clusters with more than one channel.
        """
        sizes = rows.groupby("cluster").detection_channel.agg(["size", "nunique"])
        multi = sizes[sizes["size"] >= 2]
        return len(multi), int((multi["nunique"] > 1).sum())

    n_all, bad_all = tally(windowed)
    n_rule, bad_rule = tally(windowed[windowed.detection_method == "rule"])
    return {"clusters": n_all, "disagree": bad_all,
            "share_disagree": bad_all / n_all if n_all else math.nan,
            "clusters_rule": n_rule, "disagree_rule": bad_rule,
            "share_disagree_rule": bad_rule / n_rule if n_rule else math.nan}


def unread_penalties(frame: pd.DataFrame, repo_root: pathlib.Path = REPO_ROOT) -> int:
    """Count orders that direct payment of a penalty the amount reader did not read.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.
        repo_root (pathlib.Path): base for the repo-relative ``text_path``.

    Returns:
        int: orders with no penalty read whose last fifth carries ``PAY_CLAUSE``.
    """
    candidates = frame[frame.has_text & (frame.penalty_outcome != "penalty")
                       & frame.order_type.isin(["AO", "ED_CGM", "WTM"])]
    count = 0
    for relative in candidates.text_path.dropna():
        path = repo_root / relative
        if not path.exists():
            continue
        text = path.read_text(errors="ignore")
        if PAY_CLAUSE.search(text[int(len(text) * 0.8):]):
            count += 1
    return count


# ----- formatting ----------------------------------------------------------------------------


def n(value: float) -> str:
    """A count with thousands separators.

    Args:
        value (float): the count.

    Returns:
        str: ``"20,861"``.
    """
    return f"{int(round(value)):,}"


def pct(part: float, whole: float, digits: int = 1) -> str:
    """A share as a LaTeX percentage.

    Args:
        part (float): numerator.
        whole (float): denominator.
        digits (int): decimal places.

    Returns:
        str: ``"58.2\\%"``, or ``"--"`` when the denominator is zero.
    """
    if not whole:
        return "--"
    return f"{100 * part / whole:.{digits}f}\\%"


def spread(values: pd.Series) -> str:
    """Median and interquartile range of a lag.

    Args:
        values (pd.Series): lags in years.

    Returns:
        str: ``"3.15 (1.48--5.35)"``, or ``"--"`` when empty.
    """
    clean = values.dropna()
    if clean.empty:
        return "--"
    low, mid, high = clean.quantile([0.25, 0.5, 0.75])
    return f"{mid:.2f} ({low:.2f}--{high:.2f})"


def tabular(columns: str, header: list[str], rows: list[list[str] | str]) -> str:
    """Build a full-width booktabs table in the template's ``tabular*`` form.

    Args:
        columns (str): alignment of the columns after the first, e.g. ``"rrr"``.
        header (list[str]): column headings, including the first.
        rows (list[list[str] | str]): data rows; a bare string starts a panel with that heading.

    Returns:
        str: LaTeX for the table body, ready for ``\\input``.
    """
    width = len(columns) + 1
    lines = ["\\begin{tabular*}{\\textwidth}[]{l@{\\extracolsep\\fill}" + columns + "}",
             "\\toprule", " & ".join(header) + "\\\\", "\\midrule"]
    for index, row in enumerate(rows):
        if isinstance(row, str):
            if index:
                lines.append("\\midrule")
            lines.append(f"\\multicolumn{{{width}}}{{l}}{{{row}}}\\\\")
        else:
            lines.append(" & ".join(row) + "\\\\")
    lines += ["\\bottomrule", "\\end{tabular*}", ""]
    return "\n".join(lines)


# ----- tables --------------------------------------------------------------------------------


def table_corpus(frame: pd.DataFrame) -> str:
    """The corpus by listing category.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        str: LaTeX table body.
    """
    rows: list[list[str] | str] = []
    groups = [(TYPE_LABELS[key], frame[frame.order_type == key]) for key in TYPE_LABELS]
    for label, rows_of in groups + [("All orders", frame)]:
        text = rows_of[rows_of.has_text]
        rows.append([label, n(len(rows_of)), pct(len(rows_of), len(frame)), n(len(text)),
                     f"{rows_of.n_pages.median():.0f}", pct(text.has_window.sum(), len(text)),
                     pct(text.prior_action_date.notna().sum(), len(text)),
                     pct(text.matched.sum(), len(text))])
    return tabular("rrrrrrr", ["Listing category", "Orders", "Share", "With text", "Pages",
                               "Window", "Prior action", "Issuer"], rows)


def table_windows(frame: pd.DataFrame) -> str:
    """Where windows come from and where they are missing.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        str: LaTeX table body.
    """
    text = frame[frame.has_text]
    windowed = text[text.has_window]

    def rate_row(label: str, rows_of: pd.DataFrame) -> list[str]:
        """One extraction-rate row.

        Args:
            label (str): the row's label.
            rows_of (pd.DataFrame): the orders in this row.

        Returns:
            list[str]: label, orders with text, windows, share, median window length in days.
        """
        length = (rows_of.violation_end - rows_of.violation_start).dt.days
        return [label, n(len(rows_of)), n(rows_of.has_window.sum()),
                pct(rows_of.has_window.sum(), len(rows_of)),
                f"{length.median():.0f}" if length.notna().any() else "--"]

    rows: list[list[str] | str] = ["A. By listing category"]
    rows += [rate_row(TYPE_LABELS[key], text[text.order_type == key]) for key in TYPE_LABELS]
    rows.append("B. By year of the order")
    rows += [rate_row(f"{start}--{end}", text[text.era == f"{start}--{end}"])
             for start, end in ERAS]
    rows.append("C. By source of the window")
    how = [("Rules, stated period", windowed[windowed.violation_method == "regex"]),
           ("Model, stated period", windowed[(windowed.violation_method == "model")
                                             & (windowed.window_kind == "stated_period")]),
           ("Model, conduct dates", windowed[(windowed.violation_method == "model")
                                             & (windowed.window_kind == "conduct_dates")]),
           ("Hand correction", windowed[windowed.violation_method == "hand"])]
    for label, rows_of in how:
        length = (rows_of.violation_end - rows_of.violation_start).dt.days
        rows.append([label, "", n(len(rows_of)), pct(len(rows_of), len(windowed)),
                     f"{length.median():.0f}"])
    rows.append("D. All orders")
    rows.append(rate_row("Excluding ISO orders", text[~text.iso]))
    rows.append(rate_row("All orders with text", text))
    return tabular("rrrr", ["", "Orders with text", "Windows", "Share", "Median days"], rows)


def table_validation(key: pd.DataFrame) -> str:
    """The blind sample's design (no pipeline answers: counts per cell only).

    Args:
        key (pd.DataFrame): ``derived/validation_key.parquet``.

    Returns:
        str: LaTeX table body.
    """
    cells = key.groupby(["stratum", "iso"]).agg(population=("cell_population", "first"),
                                                sampled=("cell_sampled", "first"),
                                                weight=("weight", "first")).reset_index()
    rows: list[list[str] | str] = []
    for stratum, label in STRATUM_LABELS.items():
        for iso_flag, suffix in ((False, "other orders"), (True, "ISO orders")):
            cell = cells[(cells.stratum == stratum) & (cells.iso == iso_flag)]
            if cell.empty:
                continue
            cell = cell.iloc[0]
            rows.append([f"{label}: {suffix}", n(cell.population), n(cell.sampled),
                         f"{cell.weight:.1f}"])
    rows.append(["All cells", n(cells.population.sum()), n(cells.sampled.sum()), ""])
    return tabular("rrr", ["Sampling cell", "Population", "Sampled", "Weight"], rows)


def table_categories(frame: pd.DataFrame) -> str:
    """Fraud categories across all orders and windowed orders.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        str: LaTeX table body.
    """
    text = frame[frame.has_text]
    rows: list[list[str] | str] = []
    for key, label in CATEGORY_LABELS.items():
        rows_of = text[text.fraud_category == key]
        rows.append([label, n(len(rows_of)), pct(len(rows_of), len(text)),
                     n(rows_of.has_window.sum()),
                     pct((rows_of.fraud_category_confidence == "high").sum(), len(rows_of), 0),
                     pct(rows_of.stated.sum(), len(rows_of), 0)])
    rows.append(["All orders with text", n(len(text)), "100.0\\%", n(text.has_window.sum()),
                 pct((text.fraud_category_confidence == "high").sum(), len(text), 0),
                 pct(text.stated.sum(), len(text), 0)])
    return tabular("rrrrr", ["Category", "Orders", "Share", "With window", "High confidence",
                             "Channel stated"], rows)


def table_channels(frame: pd.DataFrame) -> str:
    """Detection channels under the three populations that change the answer.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        str: LaTeX table body.
    """
    text = frame[frame.has_text]
    populations = [text, text[~text.iso],
                   text[~text.iso & text.has_window
                        & text.fraud_category.isin(ISSUER_CATEGORIES)]]
    stated = [rows_of[rows_of.stated] for rows_of in populations]
    rows: list[list[str] | str] = ["A. Share of orders that state a channel"]
    for key, label in CHANNEL_LABELS.items():
        row = [label, n((stated[0].detection_channel == key).sum())]
        row += [pct((rows_of.detection_channel == key).sum(), len(rows_of)) for rows_of in stated]
        rows.append(row)
        if key == "SURVEILLANCE":
            alert = [rows_of[rows_of.detection_detail == "alert"] for rows_of in stated]
            rows.append(["\\quad of which an explicit alert", n(len(alert[0]))]
                        + [pct(len(hits), len(rows_of)) for hits, rows_of in zip(alert, stated)])
    rows.append("B. How much the record discloses")
    rows.append(["Orders stating a channel", n(len(stated[0]))]
                + [n(len(rows_of)) for rows_of in stated])
    rows.append(["Orders in the population", n(len(populations[0]))]
                + [n(len(rows_of)) for rows_of in populations])
    rows.append(["Share unclear", ""]
                + [pct(len(rows_of) - len(done), len(rows_of))
                   for rows_of, done in zip(populations, stated)])
    return tabular("rrrr", ["Channel", "Orders", "All orders", "Excl.\\ ISO",
                            "Issuer frauds, windowed, excl.\\ ISO"], rows)


def table_sanctions(frame: pd.DataFrame) -> str:
    """Monetary and other sanctions as read from the operative parts.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        str: LaTeX table body.
    """
    rows: list[list[str] | str] = ["A. Monetary sanctions"]
    for label, column in (("Penalty", "penalty_total"), ("Settlement amount", "settlement_amount"),
                          ("Disgorgement", "disgorgement_total")):
        amounts = frame[column][frame[column] > 0]
        low, mid, high = amounts.quantile([0.25, 0.5, 0.75]) / LAKH
        rows.append([label, n(len(amounts)), f"{amounts.sum() / CRORE:,.0f}", f"{low:,.1f}",
                     f"{mid:,.1f}", f"{high:,.1f}", f"{amounts.max() / CRORE:,.0f}"])
    rows.append("B. Non-monetary sanctions (orders)")
    other = [("Market restraint or debarment", frame.debarred.fillna(False).astype(bool).sum()),
             ("Suspension of registration", (frame.registration_action == "suspension").sum()),
             ("Cancellation of registration", (frame.registration_action == "cancellation").sum()),
             ("Warning", frame.warned.fillna(False).astype(bool).sum())]
    for label, count in other:
        rows.append([label, n(count), "", "", "", "", ""])
    return tabular("rrrrrr", ["Sanction", "Orders", "Total", "p25", "Median", "p75", "Largest"],
                   rows)


def table_lags(frame: pd.DataFrame) -> str:
    """The lag split, by unit of analysis, listing category and fraud category.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        str: LaTeX table body.
    """
    windowed = frame[frame.has_window]

    def lag_row(label: str, rows_of: pd.DataFrame) -> list[str]:
        """One row of the lag table.

        Args:
            label (str): the row's label.
            rows_of (pd.DataFrame): rows with ``t_first`` and ``t_resolve``.

        Returns:
            list[str]: label, count, the two lags, share with no positive lag.
        """
        return [label, n(len(rows_of)), spread(rows_of.t_first), spread(rows_of.t_resolve),
                pct((rows_of.t_first <= 0).sum(), len(rows_of))]

    rows: list[list[str] | str] = ["A. Unit of analysis"]
    rows.append(lag_row("Orders", windowed))
    rows.append(lag_row("Orders, excluding ISO", windowed[~windowed.iso]))
    rows.append(lag_row("Matter clusters", clusters(frame)))
    rows.append("B. Orders by listing category")
    rows += [lag_row(TYPE_LABELS[key], windowed[windowed.order_type == key])
             for key in TYPE_LABELS]
    rows.append("C. Orders by fraud category, excluding ISO")
    rest = windowed[~windowed.iso]
    rows += [lag_row(label, rest[rest.fraud_category == key])
             for key, label in CATEGORY_LABELS.items()]
    return tabular("rrrr", ["", "$N$", "Window end to first action",
                            "First action to order", "No positive lag"], rows)


def table_funnel(frame: pd.DataFrame, derived: pathlib.Path,
                 sample_dir: pathlib.Path | None) -> str:
    """From the corpus to the firm sample.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.
        derived (pathlib.Path): directory with ``firms``, ``universe`` and ``name_matches``.
        sample_dir (pathlib.Path | None): directory with ``sample*.parquet``; None skips panel B.

    Returns:
        str: LaTeX table body.
    """
    firms = pd.read_parquet(derived / "firms.parquet", columns=["firm_id", "status"])
    universe = pd.read_parquet(derived / "universe.parquet", columns=["firm_id"])
    names = pd.read_parquet(derived / "name_matches.parquet", columns=["band"])
    text = frame[frame.has_text]
    windowed = text[text.has_window]
    rows: list[list[str] | str] = ["A. Listed universe and matching"]
    rows.append(["Listed securities (BSE and NSE lists)", n(len(universe))])
    rows.append(["Listed firms", n(len(firms))])
    rows.append(["Orders naming an issuer", n(text.issuer_guess.notna().sum())])
    rows.append(["Orders matched to a listed firm", n(text.matched.sum())])
    rows.append(["Windowed orders matched to a listed firm", n(windowed.matched.sum())])
    rows.append(["Names awaiting hand review", n((names.band == "review").sum())])
    if sample_dir is not None:
        sample = pd.read_parquet(sample_dir / "sample.parquet")
        years = pd.read_parquet(sample_dir / "sample_fraud_years.parquet")
        events = pd.read_parquet(sample_dir / "sample_events.parquet")
        fraud = sample[sample.role == "fraud"]
        control = sample[sample.role == "control"]
        rows.append("B. Sample")
        rows.append(["Fraud firms", n(len(fraud))])
        rows.append(["Fraud firm-years, all", n(len(years))])
        rows.append(["Fraud firm-years, FY2010--FY2025", n(years.in_period.sum())])
        rows.append(["Eligible control firms", n(control.eligible_controls.max())])
        rows.append(["Control firms drawn", n(len(control))])
        rows.append(["Public-action events", n(len(events))])
    return tabular("r", ["Step", "Count"], rows)


def price_coverage(derived: pathlib.Path, sample_dir: pathlib.Path | None) -> dict[str, float]:
    """Price coverage of the listed firms in the sample.

    Args:
        derived (pathlib.Path): directory with ``price_days`` and ``prices/``.
        sample_dir (pathlib.Path | None): directory with ``sample.parquet``; None skips the
            firm shares.

    Returns:
        dict[str, float]: days and rows per exchange, first day per exchange, and the share of
            fraud and control firms with at least one price.
    """
    days = pd.read_parquet(derived / "price_days.parquet")
    ok = days[days.status == "ok"]
    out: dict[str, float] = {}
    for exchange in ("NSE", "BSE"):
        rows_of = ok[ok.exchange == exchange]
        out[f"{exchange}_days"] = len(rows_of)
        out[f"{exchange}_first"] = str(rows_of.date.min())
        out[f"{exchange}_rows"] = int(rows_of.rows.sum())
    if sample_dir is None:
        return out
    seen: dict[str, set[str]] = {"NSE": set(), "BSE": set()}
    for path in sorted((derived / "prices").glob("*.parquet")):
        codes = pd.read_parquet(path, columns=["code"]).code.astype(str).unique()
        seen[path.name.split("_")[0]].update(codes)
    sample = pd.read_parquet(sample_dir / "sample.parquet")

    def has_prices(row: pd.Series) -> bool:
        """Whether a firm has any price on either exchange.

        Args:
            row (pd.Series): a sample row with JSON ``bse_codes`` and ``nse_symbols``.

        Returns:
            bool: True when one of its codes appears in the price files.
        """
        return (any(str(code) in seen["BSE"] for code in json.loads(row.bse_codes or "[]"))
                or any(str(code) in seen["NSE"] for code in json.loads(row.nse_symbols or "[]")))

    priced = sample.apply(has_prices, axis=1)
    for role in ("fraud", "control"):
        out[f"priced_{role}"] = float(priced[sample.role == role].mean())
    return out


def headline(frame: pd.DataFrame, repo_root: pathlib.Path = REPO_ROOT) -> dict[str, object]:
    """Every figure the prose quotes that no table shows.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.
        repo_root (pathlib.Path): base for order text paths.

    Returns:
        dict[str, object]: named figures, JSON-serialisable.
    """
    text = frame[frame.has_text]
    windowed = text[text.has_window]
    penalties = frame.penalty_total[frame.penalty_total > 0].sort_values(ascending=False)
    grouped = clusters(frame)
    tau_order = windowed.t_order.median()
    tau_cluster = grouped.t_first.median()
    in_dd = frame[frame.year.between(2011, 2020)]
    out: dict[str, object] = {
        "orders": len(frame), "orders_with_text": len(text), "windows": len(windowed),
        "iso_orders": int(frame.iso.sum()),
        "iso_by_title": int(frame.title.str.contains("illiquid stock option", case=False,
                                                     na=False).sum()),
        "iso_windowed": int(windowed.iso.sum()),
        "window_length_median_days": float((windowed.violation_end
                                            - windowed.violation_start).dt.days.median()),
        "t_first": windowed.t_first.median(), "t_resolve": windowed.t_resolve.median(),
        "t_order": tau_order, "t_scn": windowed.t_scn.median(),
        "t_first_non_iso": windowed[~windowed.iso].t_first.median(),
        "t_order_non_iso": windowed[~windowed.iso].t_order.median(),
        "no_positive_lag": int((windowed.t_first <= 0).sum()),
        "no_positive_lag_non_iso": int((windowed[~windowed.iso].t_first <= 0).sum()),
        "first_action_is_prior": int((windowed.first_action_source == "prior_action").sum()),
        "clusters": len(grouped), "clusters_multi": int((grouped.n_orders > 1).sum()),
        "cluster_t_first": tau_cluster, "cluster_t_resolve": grouped.t_resolve.median(),
        "penalty_orders": len(penalties), "penalty_sum_crore": penalties.sum() / CRORE,
        "penalty_median_lakh": penalties.median() / LAKH,
        "penalty_largest_crore": penalties.iloc[0] / CRORE,
        "penalty_largest_share": penalties.iloc[0] / penalties.sum(),
        "penalty_top10_share": penalties.iloc[:10].sum() / penalties.sum(),
        "penalty_sum_excl_largest_crore": penalties.iloc[1:].sum() / CRORE,
        "unread_penalty_clauses": unread_penalties(frame, repo_root),
        "stated_orders": int(text.stated.sum()),
        "unclear_share": 1 - text.stated.mean(),
        "issuer_frauds_small_three": int(text.fraud_category.isin(
            ["FUND_DIVERSION", "ACCOUNTING_MISSTATEMENT", "RPT_TUNNELLING"]).sum()),
        "issuer_frauds_small_three_windowed": int(windowed.fraud_category.isin(
            ["FUND_DIVERSION", "ACCOUNTING_MISSTATEMENT", "RPT_TUNNELLING"]).sum()),
        "orders_2011_2020": len(in_dd),
        "orders_2011_2020_by_type": {key: int(value)
                                     for key, value in in_dd.order_type.value_counts().items()},
        "penalty_median_2011_2020_lakh": in_dd.penalty_total[in_dd.penalty_total > 0].median()
        / LAKH,
        "channel_consistency": channel_consistency(frame),
    }
    out["discount_factor_order"] = {str(rate): math.exp(-rate * tau_order)
                                    for rate in DISCOUNT_RATES}
    out.update(surveillance_figures(text))
    out.update(cohort_figures(frame))
    return out


def surveillance_figures(text: pd.DataFrame) -> dict[str, object]:
    """How much of the surveillance channel is an explicit alert, and the bounds on its share.

    Args:
        text (pd.DataFrame): orders with text, from ``load_orders``.

    Returns:
        dict[str, object]: ``surveillance_alert`` (alert orders, stated orders and share in each
            population), ``surveillance_bound_low`` and ``_high`` (the surveillance share of all
            orders if no unclear order, or every unclear order, were surveillance).
    """
    populations = {"all": text, "excl_iso": text[~text.iso],
                   "issuer_windowed_excl_iso": text[~text.iso & text.has_window
                                                    & text.fraud_category.isin(ISSUER_CATEGORIES)]}
    alerts = {}
    for name, rows_of in populations.items():
        stated = rows_of[rows_of.stated]
        hits = int((stated.detection_detail == "alert").sum())
        alerts[name] = {"alert": hits, "stated": len(stated), "share": hits / len(stated)}
    surveillance = int((text.detection_channel == "SURVEILLANCE").sum())
    unclear = int((~text.stated).sum())
    return {"surveillance_alert": alerts, "surveillance_bound_low": surveillance / len(text),
            "surveillance_bound_high": (surveillance + unclear) / len(text)}


def cohort_figures(frame: pd.DataFrame, cut: str = "2018-12-31") -> dict[str, object]:
    """Yearly volumes and what survives the cohort restriction.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.
        cut (str): the last window end kept by the restriction, as ``YYYY-MM-DD``.

    Returns:
        dict[str, object]: orders per year with and without ISO, the observations with a window
            ending by ``cut``, and the median lag by year of window end (excluding ISO).
    """
    windowed = frame[frame.has_window]
    grouped = clusters(frame)
    limit = pd.Timestamp(cut)
    kept = windowed[windowed.violation_end <= limit]
    rest = windowed[~windowed.iso]
    medians = rest.groupby(rest.violation_end.dt.year).t_first.agg(["size", "median"])
    follow_up = (CORPUS_END - limit).days / YEAR_DAYS
    older = rest[rest.violation_end <= pd.Timestamp("2010-12-31")]
    shown = rest[rest.violation_end.dt.year.between(2003, 2025)
                 & rest.groupby(rest.violation_end.dt.year).t_first.transform("size").ge(20)]
    return {"orders_by_year": {str(int(year)): int(count)
                               for year, count in frame.groupby("year").size().items()},
            "orders_by_year_excl_iso": {str(int(year)): int(count) for year, count
                                        in frame[~frame.iso].groupby("year").size().items()},
            "cohort_cut": cut, "cohort_orders": len(kept),
            "cohort_orders_excl_iso": int((~kept.iso).sum()),
            "cohort_clusters": int((grouped.window_end <= limit).sum()),
            "cohort_matched_excl_iso": int((kept.matched & ~kept.iso).sum()),
            "cohort_follow_up_years": follow_up,
            "older_lags_beyond_follow_up": float((older.t_first > follow_up).mean()),
            "cohort_post_lodr_excl_iso": int(((rest.violation_end >= LODR_START)
                                              & (rest.violation_end <= limit)).sum()),
            "cohort_post_nfra_excl_iso": int(((rest.violation_end >= NFRA_START)
                                              & (rest.violation_end <= limit)).sum()),
            "cohort_post_nfra_clusters": int(((grouped.window_end >= NFRA_START)
                                              & (grouped.window_end <= limit)).sum()),
            "cohort_figure_n": len(shown),
            "median_lag_by_end_year": {str(int(year)): {"n": int(row["size"]),
                                                        "median": float(row["median"])}
                                       for year, row in medians.iterrows()}}


# ----- figures -------------------------------------------------------------------------------


def set_style() -> None:
    """Match the charts to the paper: its serif face, hairline axes, no chart junk.

    Returns:
        None
    """
    family = "serif"
    if FONT_DIR.exists():
        for path in FONT_DIR.glob("SourceSerifPro-*.otf"):
            font_manager.fontManager.addfont(str(path))
        family = "Source Serif Pro"
    plt.rcParams.update({
        "font.family": family, "font.size": 8.5, "axes.labelsize": 8.5, "axes.titlesize": 8.5,
        "xtick.labelsize": 8, "ytick.labelsize": 8, "legend.fontsize": 8,
        "axes.edgecolor": INK_SOFT, "axes.labelcolor": INK_SOFT, "text.color": INK,
        "xtick.color": INK_SOFT, "ytick.color": INK_SOFT, "axes.linewidth": 0.5,
        "xtick.major.width": 0.5, "ytick.major.width": 0, "xtick.major.size": 2.5,
        "axes.spines.top": False, "axes.spines.right": False, "axes.spines.left": False,
        "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.5,
        "axes.axisbelow": True, "legend.frameon": False, "figure.dpi": 150,
    })


def thousands(ax: plt.Axes) -> None:
    """Group the y-axis tick labels by thousands.

    Args:
        ax (plt.Axes): the axes to change.

    Returns:
        None
    """
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{int(value):,}"))


def figure_orders(frame: pd.DataFrame) -> plt.Figure:
    """Orders per year, with the Illiquid Stock Options wave picked out.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        plt.Figure: a full-width figure.
    """
    years = np.arange(2001, 2027)
    other = frame[~frame.iso].groupby("year").size().reindex(years, fill_value=0)
    iso = frame[frame.iso].groupby("year").size().reindex(years, fill_value=0)
    fig, ax = plt.subplots(figsize=(5.9, 2.7), constrained_layout=True)
    ax.bar(years, other, width=0.78, color=NEUTRAL, edgecolor="white", linewidth=0.4,
           label="All other orders")
    ax.bar(years, iso, width=0.78, bottom=other, color=ACCENT, edgecolor="white", linewidth=0.4,
           label="Illiquid Stock Options orders")
    peak = int(years[np.argmax((other + iso).values)])
    ax.annotate(f"{n(other[peak] + iso[peak])} orders in {peak}",
                xy=(peak, other[peak] + iso[peak]), xytext=(peak - 0.6, other[peak] + iso[peak]
                                                            + 70),
                ha="right", color=INK_SOFT)
    ax.set_ylabel("Orders per year")
    ax.set_xlim(2000.3, 2026.7)
    ax.set_ylim(0, (other + iso).max() * 1.14)
    ax.set_xticks(np.arange(2002, 2027, 4))
    ax.legend(loc="upper left", ncols=1, handlelength=1, handleheight=1)
    thousands(ax)
    return fig


def figure_lag(frame: pd.DataFrame, column: str, label: str, low: float, high: float) -> plt.Figure:
    """A half-width histogram of one lag, stacked by ISO, with the median marked.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.
        column (str): the lag column.
        label (str): the x-axis label.
        low (float): left edge in years.
        high (float): right edge in years; longer lags fall in the last bin.

    Returns:
        plt.Figure: a half-width figure.
    """
    windowed = frame[frame.has_window & frame[column].notna()]
    edges = np.arange(low, high + 0.5, 0.5)
    clipped = windowed[column].clip(low, high - 0.01)
    fig, ax = plt.subplots(figsize=(2.85, 2.35), constrained_layout=True)
    ax.hist([clipped[~windowed.iso], clipped[windowed.iso]], bins=edges, stacked=True,
            color=[NEUTRAL, ACCENT], edgecolor="white", linewidth=0.3,
            label=["All other orders", "ISO orders"])
    median = windowed[column].median()
    ax.set_ylim(0, ax.get_ylim()[1] * 1.14)
    ax.axvline(median, color=INK, linewidth=0.8)
    top = ax.get_ylim()[1]
    ax.text(median + 0.25, top * 0.985, f"Median {median:.2f} years", color=INK, va="top")
    ax.set_xlabel(label)
    ax.set_ylabel("Orders")
    ax.set_xlim(low, high)
    ax.legend(loc="center right", handlelength=1, handleheight=1)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=7))
    thousands(ax)
    return fig


def figure_cohort(frame: pd.DataFrame, first_year: int = 2003, last_year: int = 2025,
                  minimum: int = 20) -> plt.Figure:
    """The lag by year of window end against the longest lag the corpus could show.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.
        first_year (int): first cohort shown.
        last_year (int): last cohort shown.
        minimum (int): cohorts with fewer orders are not drawn.

    Returns:
        plt.Figure: a full-width figure.
    """
    rows = frame[frame.has_window & ~frame.iso & frame.t_first.notna()].copy()
    rows["cohort"] = rows.violation_end.dt.year
    table = rows.groupby("cohort").t_first.describe(percentiles=[0.25, 0.5, 0.75])
    table = table[(table["count"] >= minimum) & (table.index >= first_year)
                  & (table.index <= last_year)]
    ceiling = [(CORPUS_END - pd.Timestamp(f"{year}-12-31")).days / YEAR_DAYS
               for year in table.index]
    fig, ax = plt.subplots(figsize=(5.9, 2.7), constrained_layout=True)
    ax.fill_between(table.index, table["25%"], table["75%"], color=NEUTRAL, alpha=0.55,
                    linewidth=0, label="Interquartile range")
    ax.plot(table.index, table["50%"], color=INK_SOFT, linewidth=1.3, marker="o", markersize=2.6,
            label="Median lag")
    ax.plot(table.index, ceiling, color=INK, linewidth=1, linestyle=(0, (4, 2)),
            label="Longest lag observable by September 2026")
    ax.set_xlabel("Year the examined period ended")
    ax.set_ylabel("Years to first public action")
    ax.set_ylim(bottom=min(0, float(table["25%"].min())))
    ax.legend(loc="upper right", handlelength=1.8)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=9))
    return fig


def figure_categories(frame: pd.DataFrame) -> plt.Figure:
    """Orders per year by fraud category, excluding ISO, as small multiples.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.

    Returns:
        plt.Figure: a full-width figure.
    """
    years = np.arange(2001, 2027)
    rows = frame[frame.has_text & ~frame.iso]
    fig, axes = plt.subplots(2, 4, figsize=(5.9, 3.2), constrained_layout=True, sharex=True)
    for ax, (key, label) in zip(axes.flat, CATEGORY_LABELS.items()):
        counts = rows[rows.fraud_category == key].groupby("year").size().reindex(
            years, fill_value=0)
        ax.bar(years, counts, width=0.8, color=NEUTRAL_DARK, linewidth=0)
        ax.set_title(f"{label}\n{n(counts.sum())} orders", loc="left", fontsize=7.5, color=INK)
        ax.set_yticks([0, int(counts.max())])
        ax.set_xticks([2005, 2015, 2025])
        ax.tick_params(labelsize=7)
        thousands(ax)
    return fig


def write_figures(frame: pd.DataFrame, path: pathlib.Path) -> int:
    """Write the multi-page figure file the template includes page by page.

    Args:
        frame (pd.DataFrame): orders from ``load_orders``.
        path (pathlib.Path): the PDF to write.

    Returns:
        int: pages written.
    """
    set_style()
    pages = [figure_orders(frame),
             figure_lag(frame, "t_first", "Years from window end to first action", -2, 16),
             figure_lag(frame, "t_resolve", "Years from first action to order", 0, 16),
             figure_cohort(frame), figure_categories(frame)]
    with PdfPages(path) as pdf:
        for fig in pages:
            pdf.savefig(fig)
            plt.close(fig)
    return len(pages)


# ----- entry point ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The command line.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(description="Build the thesis tables and figures")
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR)
    parser.add_argument("--paper", type=pathlib.Path, default=PAPER_DIR,
                        help="where tables/ and figures.pdf are written")
    parser.add_argument("--sample", type=pathlib.Path,
                        help="read sample*.parquet from DIR (a dry run) instead of derived/")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Build every table and figure, and write the headline figures.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.

    Returns:
        int: 0 on success.
    """
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, LOG_FILE)
    sample_dir = args.sample
    if sample_dir is None and (args.derived / "sample.parquet").exists():
        sample_dir = args.derived
    frame = load_orders(args.derived)
    key = pd.read_parquet(args.derived / "validation_key.parquet")
    tables = {"corpus": table_corpus(frame), "windows": table_windows(frame),
              "validation": table_validation(key), "categories": table_categories(frame),
              "channels": table_channels(frame), "sanctions": table_sanctions(frame),
              "lags": table_lags(frame),
              "funnel": table_funnel(frame, args.derived, sample_dir)}
    out_dir = args.paper / "tables"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, body in tables.items():
        (out_dir / f"{name}.tex").write_text(body, encoding="utf-8")
    pages = write_figures(frame, args.paper / "figures.pdf")
    numbers = headline(frame)
    numbers["prices"] = price_coverage(args.derived, sample_dir)
    numbers["sample_source"] = str(sample_dir) if sample_dir else None
    (args.derived / "paper_numbers.json").write_text(json.dumps(numbers, indent=2, default=float),
                                                     encoding="utf-8")
    log.info("wrote %d tables under %s, %d figure pages, derived/paper_numbers.json",
             len(tables), out_dir, pages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
