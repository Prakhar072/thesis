"""Stage 1: detection lags at matter level (runbook Weeks 5–6; technical plan §5; paper §6.1).

For matter *i* (``src/matters.py``), the detection lag is ``T_i = first public action − window end`` and the
resolution lag ``R_i = latest order − first public action``. Every matter in the corpus was acted on, so there is no
censoring; the threat is **right truncation** (a matter is observed only if ``T_i ≤ corpus date − window end``).

What this module does, in the runbook's order:

1. **The variable** (Week 5 Day 1). ``T`` in months; non-positive lags (SEBI acted on or before the window's end)
   go to ``nonpositive.csv`` and are left out of the duration models (robustness: set to one day).
2. **Descriptives** before any model: median and IQR overall, by category, by window-end period, listed or not,
   by the kind of first action; the empirical distribution (Kaplan–Meier with no censoring).
3. **Cox models** (Day 2) on the **cohort** (windows ending by 2018-12-31; the primary sample, Day 5), in blocks:
   fraud characteristics → + regime (post-LODR) → listed issuers + visibility → + ownership (once A7 is in).
   Standard errors clustered by firm (issuer firm, else the matter's grouping key).
4. **Proportional hazards** (Day 3): Schoenfeld-residual tests. With ~3,000 matters most covariates fail at 5%;
   the category (the strongest failure) is moved to strata as a check, and the AFT carries the interpretation.
5. **Weibull AFT** (Day 4): time ratios; robust (sandwich) errors (lifelines' AFT has no cluster option).
6. **Truncation** (Day 5, in R): the Efron–Petrosian NPMLE (``SurvTrunc::cdfDT``), naive beside adjusted. Inside the
   cohort, windows ending in 2016–18 still have less follow-up than older ones, so the regime test is also run on a
   **common horizon**: lags up to ``HORIZON_MONTHS`` (7.73 years, the last cohort's follow-up) for every cohort.
7. **Competing risks** (Week 6 Days 1–2, in R and Python): cumulative incidence by channel (``cmprsk::cuminc``),
   Fine–Gray per main channel (``cmprsk::crr``), cause-specific Cox as the check. UNCLEAR stays a category. H4:
   the surveillance share of first detections, broad and alert-only.
8. **Regime** (Day 3): post-LODR on the cohort only; post-NFRA reported with its count (19 matters).
9. **Robustness** (Day 4): window start; the earliest window end; non-positive lags kept; no settlement-only
   matters; regex/hand windows only; stated periods only; the order as the unit (D1's robustness); the hand-coded
   validation orders.

Reads ``matters``, ``order_matters``, ``orders``, ``order_issuers``, ``universe``, ``prices/``,
``corporate_actions``, ``market_index``, ``hand/validation_sample.csv`` and, with ``--ownership`` (only after the
shareholding download has finished), ``shareholding``. Writes ``derived/stage1/`` (tables as CSV, the analysis frame),
``derived/stage1_numbers.json``, ``paper/tables/stage1_*.tex``, ``paper/figures_stage1.pdf``, ``logs/stage1.log``.
No network. Aggregates only in the paper outputs.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import pathlib
import subprocess
import warnings

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from lifelines import CoxPHFitter, KaplanMeierFitter, WeibullAFTFitter
from lifelines.statistics import proportional_hazard_test
from matplotlib.backends.backend_pdf import PdfPages

from src import cache
from src.extract_text import setup_logging
from src.paper_tables import (ACCENT, CATEGORY_LABELS, CHANNEL_LABELS, CORPUS_END, INK, INK_SOFT, LODR_START,
                              NEUTRAL, NEUTRAL_DARK, NFRA_START, n, pct, set_style, spread, tabular)

log = logging.getLogger("stage1")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
PAPER_DIR = REPO_ROOT / "paper"
LOG_FILE = REPO_ROOT / "logs" / "stage1.log"
R_SCRIPT = REPO_ROOT / "src" / "stage1.R"
VALIDATION_SHEET = REPO_ROOT / "hand" / "validation_sample.csv"

#: The primary sample keeps windows ending by this date (runbook Week 5 Day 5: eight years of follow-up).
COHORT_END = pd.Timestamp("2018-12-31")
MONTH_DAYS = 30.44
#: The follow-up of the last cohort (window ending 2018-12-31 → corpus date): 7.73 years. Keeping only lags up to it
#: gives every cohort the same truncation bound, so cohorts (and the regime indicator) compare like with like.
HORIZON_MONTHS = (CORPUS_END - COHORT_END).days / MONTH_DAYS
YEAR_DAYS = 365.25
#: The rare issuer-fraud categories are pooled (124 cohort matters between them).
FINANCIAL = {"FUND_DIVERSION", "ACCOUNTING_MISSTATEMENT", "RPT_TUNNELLING"}
CATEGORY_GROUPS = ["MARKET_MANIPULATION", "DISCLOSURE_FAILURE", "INTERMEDIARY_MISCONDUCT", "INSIDER_TRADING",
                   "FINANCIAL", "OTHER"]
GROUP_LABELS = {**CATEGORY_LABELS, "FINANCIAL": "Diversion, accounting, RPT"}
#: Channels with few matters are pooled for the competing-risks models.
CHANNEL_GROUPS = ["SURVEILLANCE", "INSPECTION", "COMPLAINT", "REFERRAL", "OTHER_STATED", "UNCLEAR"]
CHANNEL_GROUP_LABELS = {**CHANNEL_LABELS, "OTHER_STATED": "Media, self-disclosure, audit", "UNCLEAR": "Unclear"}
#: Surveillance details that are an observation, not an alert (protocol §5: flagged).
FLAGGED_SURVEILLANCE = {"trading_pattern_observed", "filings_observed"}
CATEGORY_COVARIATES = ["cat_DISCLOSURE_FAILURE", "cat_INTERMEDIARY_MISCONDUCT", "cat_INSIDER_TRADING",
                       "cat_FINANCIAL", "cat_OTHER"]
FRAUD_COVARIATES = CATEGORY_COVARIATES + ["log_parties", "intermediary", "log_window", "listed"]
REGIME_COVARIATES = ["post_lodr"]
VISIBILITY_COVARIATES = ["log_turnover", "log_amihud", "nse_main"]
OWNERSHIP_COVARIATES = ["log_market_cap", "promoter_pct", "pledged_pct"]
COVARIATE_LABELS = {
    "cat_DISCLOSURE_FAILURE": "Disclosure failure", "cat_INTERMEDIARY_MISCONDUCT": "Intermediary misconduct",
    "cat_INSIDER_TRADING": "Insider trading", "cat_FINANCIAL": "Diversion, accounting, RPT",
    "cat_OTHER": "Other category", "log_parties": "Log(1 + parties)", "intermediary": "Intermediary a noticee",
    "log_window": "Log window length (months)", "listed": "Listed issuer", "post_lodr": "Window ends after LODR",
    "post_nfra": "Window ends after NFRA", "log_turnover": "Log median daily turnover",
    "log_amihud": "Log Amihud illiquidity", "nse_main": "Main exchange NSE", "log_market_cap": "Log market cap",
    "promoter_pct": "Promoter holding (\\%)", "pledged_pct": "Promoter shares pledged (\\%)"}
PERIODS = ((1990, 2005, "to 2005"), (2006, 2010, "2006--10"), (2011, 2015, "2011--15"), (2016, 2018, "2016--18"),
           (2019, 2026, "2019--26"))


# ----- the analysis frame ------------------------------------------------------------------------------------------


def fiscal_year(dates: pd.Series) -> pd.Series:
    """The Indian financial year (April–March, named by its end year) of each date.

    Args:
        dates (pd.Series): datetimes.

    Returns:
        pd.Series: Int64 years (NA where the date is missing).
    """
    return (dates.dt.year + (dates.dt.month >= 4).astype(int)).astype("Int64").where(dates.notna())


def category_group(category: object) -> str:
    """Pool the rare issuer-fraud categories.

    Args:
        category (object): a fraud category (str or null).

    Returns:
        str: one of ``CATEGORY_GROUPS`` (a missing category counts as OTHER).
    """
    if not isinstance(category, str):
        return "OTHER"
    return "FINANCIAL" if category in FINANCIAL else category


def channel_group(channel: object) -> str:
    """Pool the rare channels; a missing channel is UNCLEAR.

    Args:
        channel (object): a detection channel (str or null).

    Returns:
        str: one of ``CHANNEL_GROUPS``.
    """
    if not isinstance(channel, str) or channel == "UNCLEAR":
        return "UNCLEAR"
    return channel if channel in CHANNEL_GROUPS else "OTHER_STATED"


def analysis_frame(matters: pd.DataFrame, visibility: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per windowed matter with a first action: the lags, the cohort flag and the covariates.

    Args:
        matters (pd.DataFrame): ``matters.parquet``.
        visibility (pd.DataFrame | None): per matter (``matter_id``) market and ownership covariates, or None.

    Returns:
        pd.DataFrame: the matters with ``t_months``, ``t_years``, ``r_years`` (resolution), ``trunc_months`` (window
            end → corpus date), ``positive``, ``cohort``, ``event`` (1), ``category_group``, ``channel_group``,
            ``cluster``, the covariates of ``FRAUD_COVARIATES`` and ``REGIME_COVARIATES``, ``post_nfra``, and the
            visibility columns when given.
    """
    frame = matters[matters.has_window & matters.first_action_date.notna()].copy()
    frame["t_months"] = (frame.first_action_date - frame.window_end).dt.days / MONTH_DAYS
    frame["t_years"] = frame.t_months * MONTH_DAYS / YEAR_DAYS
    frame["r_years"] = (frame.last_order_date - frame.first_action_date).dt.days / YEAR_DAYS
    frame["trunc_months"] = (CORPUS_END - frame.window_end).dt.days / MONTH_DAYS
    frame["positive"] = frame.t_months > 0
    frame["cohort"] = frame.window_end <= COHORT_END
    frame["event"] = 1
    frame["category_group"] = frame.fraud_category.map(category_group)
    frame["channel_group"] = frame.detection_channel.map(channel_group)
    frame["cluster"] = frame.issuer_firm_id.fillna(frame.component)
    for group in CATEGORY_GROUPS[1:]:
        frame[f"cat_{group}"] = (frame.category_group == group).astype(int)
    frame["log_parties"] = np.log1p(frame.n_parties.astype(float))
    frame["intermediary"] = frame.intermediary_noticee.astype(int)
    frame["log_window"] = np.log1p(frame.window_years.clip(lower=0) * 12)
    frame["listed"] = frame.issuer_firm_id.notna().astype(int)
    frame["post_lodr"] = (frame.window_end >= LODR_START).astype(int)
    frame["post_nfra"] = (frame.window_end >= NFRA_START).astype(int)
    frame["fy_end"] = fiscal_year(frame.window_end)
    if visibility is not None and len(visibility):
        frame = frame.merge(visibility, on="matter_id", how="left", validate="one_to_one")
    return frame.reset_index(drop=True)


# ----- visibility from prices (and ownership once fetched) ---------------------------------------------------------


def market_visibility(matters: pd.DataFrame, derived: pathlib.Path, use_ownership: bool = False) -> pd.DataFrame:
    """Market covariates of each listed matter's firm in the financial year its window ends (else the year before).

    Uses Week 4's panel functions on the matters' firms over FY2004–2027: median daily turnover, Amihud illiquidity,
    main exchange; with ``use_ownership``, market capitalisation, promoter holding and pledging from the
    shareholding summaries (Dec 2015 onward).

    Args:
        matters (pd.DataFrame): ``matters.parquet``.
        derived (pathlib.Path): the directory with ``universe``, ``prices/``, ``corporate_actions``,
            ``market_index`` (and ``shareholding``).
        use_ownership (bool): read ``shareholding.parquet`` (only once its download has finished).

    Returns:
        pd.DataFrame: ``matter_id``, ``vis_fy``, ``log_turnover``, ``log_amihud``, ``nse_main``,
            ``trading_days`` and, with ownership, ``log_market_cap``, ``promoter_pct``, ``pledged_pct``.
    """
    from src.build_panel import firm_daily, market_signals, ownership, read_prices, security_map
    from src.universe import NSE_SYMBOLCHANGE, _csv, symbol_chain

    listed = matters[matters.has_window & matters.issuer_firm_id.notna()]
    firms = set(listed.issuer_firm_id)
    universe = pd.read_parquet(derived / "universe.parquet")
    symbolchange = symbol_chain(_csv(cache.get(NSE_SYMBOLCHANGE, offline=True)))
    years = range(2004, CORPUS_END.year + 1)
    prices_dir = derived / "prices"
    nse_isin = []
    for year in years:
        path = prices_dir / f"NSE_{year}.parquet"
        if path.exists():
            nse_isin.append(pd.read_parquet(path, columns=["code", "isin"]).dropna().drop_duplicates())
    smap = security_map(universe, pd.concat(nse_isin) if nse_isin else pd.DataFrame(columns=["code", "isin"]),
                        symbolchange)
    smap = smap[smap.firm_id.isin(firms)]
    codes = {exchange: set(smap[smap.exchange == exchange].code) for exchange in ("NSE", "BSE")}
    firm_bse = universe[universe.exchange == "BSE"].groupby("firm_id").code.agg(list).to_dict()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        daily, main = firm_daily(read_prices(prices_dir, codes, years), smap,
                                 pd.read_parquet(derived / "corporate_actions.parquet"), firm_bse)
        signals = market_signals(daily, pd.read_parquet(derived / "market_index.parquet"))
    if use_ownership:
        own = ownership(pd.read_parquet(derived / "shareholding.parquet"))
        signals = signals.merge(own, on=["firm_id", "fy"], how="left", validate="one_to_one")
        signals["market_cap"] = signals.close_end * signals.total_shares / 1e7
    signals = signals.set_index(["firm_id", "fy"])
    rows = []
    for matter in listed.itertuples(index=False):
        fy_end = int(matter.window_end.year + (matter.window_end.month >= 4))
        for fy in (fy_end, fy_end - 1):
            key = (matter.issuer_firm_id, fy)
            if key in signals.index and signals.loc[key, "trading_days"] > 0:
                row = signals.loc[key]
                record = {"matter_id": matter.matter_id, "vis_fy": fy, "trading_days": int(row.trading_days),
                          "turnover_median": row.turnover_median, "amihud": row.amihud,
                          "nse_main": int(main.get(matter.issuer_firm_id) == "NSE")}
                if use_ownership:
                    record.update(market_cap=row.market_cap, promoter_pct=row.promoter_pct,
                                  pledged_pct=row.pledged_pct)
                rows.append(record)
                break
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=["matter_id"])
    out["log_turnover"] = np.log(out.turnover_median.where(out.turnover_median > 0))
    floor = out.amihud[out.amihud > 0].quantile(0.01)
    out["log_amihud"] = np.log(out.amihud.where(out.amihud.notna()).clip(lower=floor))
    if use_ownership:
        out["log_market_cap"] = np.log(out.market_cap.where(out.market_cap > 0))
    return out


# ----- descriptives ------------------------------------------------------------------------------------------------


def describe(frame: pd.DataFrame) -> pd.DataFrame:
    """Median and IQR of both lags, overall and by group (runbook: descriptives before any model).

    Args:
        frame (pd.DataFrame): the analysis frame (all windowed matters, non-positive lags included).

    Returns:
        pd.DataFrame: ``panel``, ``group``, ``n``, ``nonpositive_share``, ``t_median``, ``t_q25``, ``t_q75`` (years,
            over positive lags), ``r_median``, ``r_q25``, ``r_q75``.
    """
    def row(panel: str, group: str, rows: pd.DataFrame) -> dict:
        """Summarise one group.

        Args:
            panel (str): the table panel.
            group (str): the group label.
            rows (pd.DataFrame): its matters.

        Returns:
            dict: the summary row.
        """
        positive = rows[rows.positive]
        return {"panel": panel, "group": group, "n": len(rows), "n_positive": len(positive),
                "nonpositive_share": 1 - len(positive) / len(rows) if len(rows) else np.nan,
                "t_median": positive.t_years.median(), "t_q25": positive.t_years.quantile(.25),
                "t_q75": positive.t_years.quantile(.75), "r_median": rows.r_years.median(),
                "r_q25": rows.r_years.quantile(.25), "r_q75": rows.r_years.quantile(.75)}

    out = [row("All", "All matters", frame), row("All", "Cohort (window ends by 2018)", frame[frame.cohort])]
    cohort = frame[frame.cohort]
    for group in CATEGORY_GROUPS:
        out.append(row("Category (cohort)", GROUP_LABELS[group], cohort[cohort.category_group == group]))
    years = frame.window_end.dt.year
    for low, high, label in PERIODS:
        out.append(row("Window ends", label, frame[years.between(low, high)]))
    out.append(row("Issuer (cohort)", "Listed issuer", cohort[cohort.listed == 1]))
    out.append(row("Issuer (cohort)", "No listed issuer", cohort[cohort.listed == 0]))
    kind = cohort.first_action_kind.fillna("").str.replace(r"^order:.*", "order", regex=True)
    for value, label in (("scn", "Show-cause notice"), ("interim", "Interim order"), ("order", "An order itself")):
        out.append(row("First action (cohort)", label, cohort[kind == value]))
    return pd.DataFrame(out)


# ----- models ------------------------------------------------------------------------------------------------------


def usable(frame: pd.DataFrame, covariates: list[str]) -> list[str]:
    """Covariates that vary in the sample (a constant column breaks the fit).

    Args:
        frame (pd.DataFrame): the estimation sample.
        covariates (list[str]): candidate columns.

    Returns:
        list[str]: the columns with more than one value.
    """
    return [c for c in covariates if c in frame and frame[c].nunique(dropna=True) > 1]


def fit_cox(frame: pd.DataFrame, covariates: list[str], strata: list[str] | None = None,
            event_col: str = "event", duration_col: str = "t_months") -> tuple[CoxPHFitter, pd.DataFrame]:
    """A Cox model with errors clustered by firm.

    Args:
        frame (pd.DataFrame): the estimation sample (positive lags) with ``cluster``.
        covariates (list[str]): the regressors.
        strata (list[str] | None): binary covariates to stratify on instead of estimating.
        event_col (str): the event indicator (1 for every matter; a channel's indicator for cause-specific models).
        duration_col (str): the lag column.

    Returns:
        tuple[CoxPHFitter, pd.DataFrame]: the fitted model and its data.
    """
    strata = strata or []
    columns = [duration_col, event_col, "cluster"] + covariates + strata
    data = frame[columns].dropna().copy()
    model = CoxPHFitter()
    model.fit(data, duration_col=duration_col, event_col=event_col, cluster_col="cluster",
              strata=strata or None)
    return model, data


def cox_table(model: CoxPHFitter, label: str, data: pd.DataFrame) -> pd.DataFrame:
    """The hazard ratios of a fitted Cox model as rows.

    Args:
        model (CoxPHFitter): fitted.
        label (str): the model's name.
        data (pd.DataFrame): its data (for N).

    Returns:
        pd.DataFrame: ``model``, ``covariate``, ``hr``, ``hr_low``, ``hr_high``, ``p``, ``n``, ``events``,
            ``concordance``.
    """
    summary = model.summary
    return pd.DataFrame({
        "model": label, "covariate": summary.index, "hr": summary["exp(coef)"].to_numpy(),
        "hr_low": summary["exp(coef) lower 95%"].to_numpy(), "hr_high": summary["exp(coef) upper 95%"].to_numpy(),
        "p": summary["p"].to_numpy(), "n": len(data), "events": int(data[model.event_col].sum()),
        "concordance": model.concordance_index_})


def ph_tests(model: CoxPHFitter, data: pd.DataFrame, label: str) -> pd.DataFrame:
    """Schoenfeld-residual tests of proportional hazards, one row per covariate.

    Args:
        model (CoxPHFitter): fitted.
        data (pd.DataFrame): its data.
        label (str): the model's name.

    Returns:
        pd.DataFrame: ``model``, ``covariate``, ``statistic``, ``p``.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = proportional_hazard_test(model, data, time_transform="rank")
    summary = result.summary
    covariates = [index[0] if isinstance(index, tuple) else index for index in summary.index]
    return pd.DataFrame({"model": label, "covariate": covariates, "statistic": summary["test_statistic"].to_numpy(),
                         "p": summary["p"].to_numpy()})


def fit_aft(frame: pd.DataFrame, covariates: list[str], label: str) -> pd.DataFrame:
    """A Weibull AFT model; coefficients as time ratios.

    Args:
        frame (pd.DataFrame): the estimation sample (positive lags).
        covariates (list[str]): the regressors.
        label (str): the model's name.

    Returns:
        pd.DataFrame: ``model``, ``covariate``, ``tr`` (time ratio), ``tr_low``, ``tr_high``, ``p``, ``n``,
            ``rho`` (the Weibull shape).
    """
    data = frame[["t_months", "event"] + covariates].dropna()
    model = WeibullAFTFitter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(data, duration_col="t_months", event_col="event", robust=True)
    summary = model.summary.loc["lambda_"].drop(index="Intercept", errors="ignore")
    rho = float(np.exp(model.summary.loc[("rho_", "Intercept"), "coef"]))
    return pd.DataFrame({"model": label, "covariate": summary.index, "tr": np.exp(summary["coef"]).to_numpy(),
                         "tr_low": np.exp(summary["coef lower 95%"]).to_numpy(),
                         "tr_high": np.exp(summary["coef upper 95%"]).to_numpy(), "p": summary["p"].to_numpy(),
                         "n": len(data), "rho": rho})


HORIZON_MODEL = "(2h) + Regime, common horizon"


def model_blocks(frame: pd.DataFrame, has_ownership: bool) -> dict[str, list[str]]:
    """The covariate blocks of the Cox and AFT models (runbook Week 5 Day 2).

    Args:
        frame (pd.DataFrame): the analysis frame.
        has_ownership (bool): ownership columns are present.

    Returns:
        dict[str, list[str]]: block name → covariates (in entry order).
    """
    blocks = {"(1) Fraud": FRAUD_COVARIATES,
              "(2) + Regime": FRAUD_COVARIATES + REGIME_COVARIATES,
              "(3) Listed + visibility": [c for c in FRAUD_COVARIATES if c != "listed"] + REGIME_COVARIATES
              + VISIBILITY_COVARIATES}
    if has_ownership:
        blocks["(4) + Ownership"] = blocks["(3) Listed + visibility"] + OWNERSHIP_COVARIATES
    return blocks


def block_sample(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    """The estimation sample of a block: cohort, positive lags; listed issuers from block 3.

    Args:
        frame (pd.DataFrame): the analysis frame.
        name (str): the block's name.

    Returns:
        pd.DataFrame: the rows.
    """
    rows = frame[frame.cohort & frame.positive]
    if name.startswith("(3)") or name.startswith("(4)"):
        rows = rows[rows.listed == 1]
    return rows


def run_models(frame: pd.DataFrame, has_ownership: bool) -> dict[str, pd.DataFrame]:
    """Cox blocks, their PH tests, the stratified refit, Weibull AFT, NFRA and the unrestricted contrast.

    Args:
        frame (pd.DataFrame): the analysis frame.
        has_ownership (bool): ownership columns are present.

    Returns:
        dict[str, pd.DataFrame]: ``cox``, ``ph``, ``aft`` tables (rows from every model).
    """
    cox_rows, ph_rows, aft_rows = [], [], []
    for name, wanted in model_blocks(frame, has_ownership).items():
        sample = block_sample(frame, name)
        covariates = usable(sample.dropna(subset=[c for c in wanted if c in sample]), wanted)
        if len(sample.dropna(subset=covariates)) < 100:
            log.warning("block %s: too few matters (%d); skipped", name, len(sample.dropna(subset=covariates)))
            continue
        model, data = fit_cox(sample, covariates)
        cox_rows.append(cox_table(model, name, data))
        ph = ph_tests(model, data, name)
        ph_rows.append(ph)
        aft_rows.append(fit_aft(sample, covariates, name))
        log.info("cox %-26s n=%d concordance %.3f; PH fails (p<.05): %s", name, len(data), model.concordance_index_,
                 ph[ph.p < 0.05].covariate.tolist())
    regime = block_sample(frame, "(2)")
    horizon = regime[regime.t_months <= HORIZON_MONTHS]
    covariates = usable(horizon, FRAUD_COVARIATES + REGIME_COVARIATES)
    model, data = fit_cox(horizon, covariates)
    cox_rows.append(cox_table(model, HORIZON_MODEL, data))
    ph = ph_tests(model, data, HORIZON_MODEL)
    ph_rows.append(ph)
    aft_rows.append(fit_aft(horizon, covariates, HORIZON_MODEL))
    log.info("cox %-26s n=%d concordance %.3f; PH fails (p<.05): %s", "(2h) common horizon", len(data),
             model.concordance_index_, ph[ph.p < 0.05].covariate.tolist())
    kept = [c for c in usable(regime, FRAUD_COVARIATES + REGIME_COVARIATES) if not c.startswith("cat_")]
    model, data = fit_cox(regime.assign(stratum=regime.category_group), kept, strata=["stratum"])
    cox_rows.append(cox_table(model, "(2s) + Regime, stratified by category", data))
    covariates = usable(regime, FRAUD_COVARIATES + ["post_lodr", "post_nfra"])
    model, data = fit_cox(regime, covariates)
    cox_rows.append(cox_table(model, "(2b) + NFRA", data))
    full = frame[frame.positive]
    model, data = fit_cox(full, usable(full, FRAUD_COVARIATES + REGIME_COVARIATES))
    cox_rows.append(cox_table(model, "(2u) unrestricted sample", data))
    return {"cox": pd.concat(cox_rows, ignore_index=True), "ph": pd.concat(ph_rows, ignore_index=True),
            "aft": pd.concat(aft_rows, ignore_index=True)}


def cause_specific(frame: pd.DataFrame) -> pd.DataFrame:
    """Cause-specific Cox models per main channel on the cohort (the check on Fine–Gray).

    Args:
        frame (pd.DataFrame): the analysis frame.

    Returns:
        pd.DataFrame: rows of ``cox_table`` with ``model`` = the channel.
    """
    sample = frame[frame.cohort & frame.positive].copy()
    covariates = usable(sample, FRAUD_COVARIATES + REGIME_COVARIATES)
    rows = []
    for channel in ("SURVEILLANCE", "COMPLAINT", "INSPECTION", "REFERRAL"):
        sample["cause"] = (sample.channel_group == channel).astype(int)
        model, data = fit_cox(sample, covariates, event_col="cause")
        rows.append(cox_table(model, channel, data))
    return pd.concat(rows, ignore_index=True)


# ----- channels and H4 ---------------------------------------------------------------------------------------------


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval of a proportion.

    Args:
        successes (int): count.
        total (int): denominator.
        z (float): normal quantile.

    Returns:
        tuple[float, float]: lower and upper bound (NaN when the denominator is zero).
    """
    if total == 0:
        return math.nan, math.nan
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return centre - half, centre + half


def channel_shares(frame: pd.DataFrame) -> pd.DataFrame:
    """H4: the surveillance share of first detections, under each definition and denominator.

    Args:
        frame (pd.DataFrame): the analysis frame (all windowed matters).

    Returns:
        pd.DataFrame: ``sample``, ``measure``, ``count``, ``base``, ``share``, ``low``, ``high``.
    """
    rows = []
    samples = {"All windowed matters": frame, "Cohort": frame[frame.cohort],
               "Listed-issuer matters": frame[frame.listed == 1],
               "Listed-issuer issuer frauds": frame[(frame.listed == 1)
                                                    & ~frame.category_group.isin(["INTERMEDIARY_MISCONDUCT", "OTHER"])]}
    for name, rows_of in samples.items():
        stated = rows_of[rows_of.channel_group != "UNCLEAR"]
        surveillance = rows_of.detection_channel == "SURVEILLANCE"
        alert = surveillance & (rows_of.detection_detail == "alert")
        strict = surveillance & ~rows_of.detection_detail.isin(FLAGGED_SURVEILLANCE)
        measures = [("Surveillance, of stated", int(surveillance.sum()), len(stated)),
                    ("Surveillance without flagged details, of stated", int(strict.sum()), len(stated)),
                    ("Explicit alert, of stated", int(alert.sum()), len(stated)),
                    ("Surveillance, of all (lower bound)", int(surveillance.sum()), len(rows_of)),
                    ("Surveillance or unclear, of all (upper bound)",
                     int(surveillance.sum() + (rows_of.channel_group == "UNCLEAR").sum()), len(rows_of))]
        for group in CHANNEL_GROUPS:
            measures.append((f"Share of all: {CHANNEL_GROUP_LABELS[group]}",
                             int((rows_of.channel_group == group).sum()), len(rows_of)))
        for measure, count, base in measures:
            low, high = wilson(count, base)
            rows.append({"sample": name, "measure": measure, "count": count, "base": base,
                         "share": count / base if base else np.nan, "low": low, "high": high})
    return pd.DataFrame(rows)


# ----- robustness --------------------------------------------------------------------------------------------------


def robustness_row(label: str, rows: pd.DataFrame, duration: str = "t_months") -> dict:
    """Median lag and the main Cox hazard ratios on one variant of the sample.

    Args:
        label (str): the variant.
        rows (pd.DataFrame): its cohort rows with positive ``duration``, ``cluster`` and the covariates.
        duration (str): the lag column (months).

    Returns:
        dict: ``variant``, ``n``, ``median_years``, ``q25``, ``q75``, and HR with CI for ``post_lodr``, ``listed``,
            ``intermediary``, ``log_parties``.
    """
    rows = rows[rows[duration] > 0]
    years = rows[duration] * MONTH_DAYS / YEAR_DAYS
    out = {"variant": label, "n": len(rows), "median_years": years.median(), "q25": years.quantile(.25),
           "q75": years.quantile(.75)}
    covariates = usable(rows, FRAUD_COVARIATES + REGIME_COVARIATES)
    model, _ = fit_cox(rows, covariates, duration_col=duration)
    summary = model.summary
    for covariate in ("post_lodr", "listed", "intermediary", "log_parties"):
        if covariate in summary.index:
            out[f"{covariate}_hr"] = summary.loc[covariate, "exp(coef)"]
            out[f"{covariate}_low"] = summary.loc[covariate, "exp(coef) lower 95%"]
            out[f"{covariate}_high"] = summary.loc[covariate, "exp(coef) upper 95%"]
            out[f"{covariate}_p"] = summary.loc[covariate, "p"]
    return out


def order_level_frame(derived: pathlib.Path, frame: pd.DataFrame) -> pd.DataFrame:
    """The order as the unit (D1's robustness): each windowed order's own lag, with its matter's covariates.

    Args:
        derived (pathlib.Path): directory with ``orders`` and ``order_matters``.
        frame (pd.DataFrame): the matter analysis frame.

    Returns:
        pd.DataFrame: one row per windowed order in a cohort-window matter, ``t_order_months`` = its own first action
            − its own window end, the matter's covariates, ``cluster`` = the matter.
    """
    orders = pd.read_parquet(derived / "orders.parquet",
                             columns=["text_stem", "violation_end", "first_action_date", "n_parties",
                                      "fraud_category"])
    links = pd.read_parquet(derived / "order_matters.parquet", columns=["text_stem", "matter_id", "link"])
    orders = orders.merge(links, on="text_stem", how="left", validate="one_to_one")
    orders = orders[orders.link == "windowed"]
    for column in ("violation_end", "first_action_date"):
        orders[column] = pd.to_datetime(orders[column])
    orders["t_order_months"] = (orders.first_action_date - orders.violation_end).dt.days / MONTH_DAYS
    keep = ["matter_id", "intermediary", "log_window", "listed", "post_lodr", "post_nfra"]
    merged = orders.merge(frame[keep], on="matter_id", how="inner", validate="many_to_one")
    merged["category_group"] = merged.fraud_category.map(category_group)
    for group in CATEGORY_GROUPS[1:]:
        merged[f"cat_{group}"] = (merged.category_group == group).astype(int)
    merged["log_parties"] = np.log1p(merged.n_parties.fillna(0).astype(float))
    merged["cohort"] = merged.violation_end <= COHORT_END
    merged["cluster"] = merged.matter_id
    merged["event"] = 1
    return merged[merged.cohort]


def hand_check(derived: pathlib.Path, sheet: pathlib.Path = VALIDATION_SHEET) -> pd.DataFrame:
    """The lag on the hand-coded validation orders: hand window end vs the pipeline's, same first action.

    Args:
        derived (pathlib.Path): directory with ``orders``.
        sheet (pathlib.Path): the coded sheet.

    Returns:
        pd.DataFrame: one row per coded order with both a hand and a pipeline window: ``t_hand_years``,
            ``t_machine_years``.
    """
    if not sheet.exists():
        return pd.DataFrame(columns=["text_stem", "t_hand_years", "t_machine_years"])
    hand = pd.read_csv(sheet, dtype=str)
    orders = pd.read_parquet(derived / "orders.parquet", columns=["text_stem", "violation_end", "first_action_date"])
    orders["text_stem"] = orders.text_stem.astype(str)
    merged = hand.merge(orders, on="text_stem", how="left", suffixes=("_hand", ""), validate="one_to_one")
    hand_end = pd.to_datetime(merged.violation_end_hand, errors="coerce")
    first = pd.to_datetime(merged.first_action_date)
    merged["t_hand_years"] = (first - hand_end).dt.days / YEAR_DAYS
    merged["t_machine_years"] = (first - pd.to_datetime(merged.violation_end)).dt.days / YEAR_DAYS
    return merged[["text_stem", "t_hand_years", "t_machine_years"]].dropna()


def robustness(frame: pd.DataFrame, derived: pathlib.Path) -> tuple[pd.DataFrame, dict]:
    """Every robustness variant of the runbook (Week 6 Day 4) and the paper (§6.1).

    Args:
        frame (pd.DataFrame): the analysis frame.
        derived (pathlib.Path): for the order-level frame and the hand check.

    Returns:
        tuple[pd.DataFrame, dict]: one row per variant, and the hand-check summary.
    """
    cohort = frame[frame.cohort].copy()
    variants = [("Main: matter, window end", cohort, "t_months")]
    cohort["t_start_months"] = (cohort.first_action_date - cohort.window_start).dt.days / MONTH_DAYS
    variants.append(("Window start instead of end", cohort, "t_start_months"))
    cohort["t_min_end_months"] = (cohort.first_action_date - cohort.window_end_min).dt.days / MONTH_DAYS
    variants.append(("Earliest window end of the matter's orders", cohort, "t_min_end_months"))
    cohort["t_floor_months"] = cohort.t_months.clip(lower=1 / MONTH_DAYS)
    variants.append(("Non-positive lags set to one day", cohort, "t_floor_months"))
    variants.append(("No settlement-only matters", cohort[cohort.n_settlement < cohort.n_orders], "t_months"))
    no_model = ~cohort.window_methods.fillna("").str.contains("model")
    variants.append(("Regex and hand windows only", cohort[no_model], "t_months"))
    variants.append(("Stated periods only", cohort[cohort.window_kinds == "stated_period"], "t_months"))
    variants.append(("Common 7.7-year horizon", cohort[cohort.t_months <= HORIZON_MONTHS], "t_months"))
    rows = [robustness_row(label, rows_of, duration) for label, rows_of, duration in variants]
    orders = order_level_frame(derived, frame)
    rows.append(robustness_row("Order as the unit (clustered by matter)", orders, "t_order_months"))
    hand = hand_check(derived)
    summary = {"n": len(hand), "median_hand": hand.t_hand_years.median(),
               "median_machine": hand.t_machine_years.median(),
               "correlation": hand[["t_hand_years", "t_machine_years"]].corr().iloc[0, 1] if len(hand) > 2 else np.nan,
               "within_month": float(((hand.t_hand_years - hand.t_machine_years).abs() <= 31 / YEAR_DAYS).mean())
               if len(hand) else np.nan}
    return pd.DataFrame(rows), summary


# ----- R: truncation and competing risks ---------------------------------------------------------------------------


def run_r(frame: pd.DataFrame, out_dir: pathlib.Path) -> dict[str, pd.DataFrame]:
    """Write the R input, run ``src/stage1.R``, read its outputs.

    Args:
        frame (pd.DataFrame): the analysis frame.
        out_dir (pathlib.Path): where R reads and writes.

    Returns:
        dict[str, pd.DataFrame]: ``truncation_curve``, ``truncation_summary``, ``cif_curves``, ``cif_at``,
            ``finegray``.
    """
    rows = frame[frame.positive].copy()
    cohort_covariates = usable(rows[rows.cohort], FRAUD_COVARIATES + REGIME_COVARIATES)
    data = pd.DataFrame({"t": rows.t_months, "r": rows.trunc_months, "cohort": rows.cohort.astype(int),
                         "channel": rows.channel_group})
    for column in cohort_covariates:
        data[f"x_{column}"] = rows[column].astype(float)
    path = out_dir / "r_input.csv"
    data.to_csv(path, index=False)
    result = subprocess.run(["Rscript", str(R_SCRIPT), str(path), str(out_dir)], capture_output=True, text=True,
                            timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"stage1.R failed:\n{result.stderr[-2000:]}")
    log.info("R: %s", result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "(no output)")
    names = ("truncation_curve", "truncation_summary", "cif_curves", "cif_at", "finegray")
    return {name: pd.read_csv(out_dir / f"{name}.csv") for name in names}


# ----- paper tables ------------------------------------------------------------------------------------------------


def years_cell(median: float, low: float, high: float) -> str:
    """A median with its IQR, in years.

    Args:
        median (float): median.
        low (float): first quartile.
        high (float): third quartile.

    Returns:
        str: ``"2.32 (0.99--3.98)"`` or ``"--"``.
    """
    if pd.isna(median):
        return "--"
    return f"{median:.2f} ({low:.2f}--{high:.2f})"


def ratio_cell(value: float, low: float, high: float, p: float) -> str:
    """A ratio with its interval and significance stars.

    Args:
        value (float): the ratio.
        low (float): lower bound.
        high (float): upper bound.
        p (float): p-value.

    Returns:
        str: ``"0.82** (0.71--0.95)"`` or ``"--"``.
    """
    if pd.isna(value):
        return "--"
    stars = "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.1 else ""
    return f"{value:.2f}{stars} ({low:.2f}--{high:.2f})"


def table_descriptive(desc: pd.DataFrame) -> str:
    """LaTeX: the lag descriptives.

    Args:
        desc (pd.DataFrame): from ``describe``.

    Returns:
        str: the table body.
    """
    rows: list[list[str] | str] = []
    panel = None
    for row in desc.itertuples(index=False):
        if row.panel != panel:
            panel = row.panel
            if panel != "All":
                rows.append(panel)
        rows.append([row.group, n(row.n), pct(row.n - row.n_positive, row.n),
                     years_cell(row.t_median, row.t_q25, row.t_q75),
                     years_cell(row.r_median, row.r_q25, row.r_q75)])
    return tabular("rrrr", ["", "Matters", "Non-positive", "Detection lag $T$", "Resolution lag $R$"], rows)


def table_models(cox: pd.DataFrame, aft: pd.DataFrame) -> str:
    """LaTeX: hazard ratios of the Cox blocks and time ratios of the AFT on block 2.

    Args:
        cox (pd.DataFrame): Cox rows.
        aft (pd.DataFrame): AFT rows.

    Returns:
        str: the table body.
    """
    wanted = ["(1) Fraud", "(2) + Regime", HORIZON_MODEL, "(3) Listed + visibility", "(4) + Ownership"]
    blocks = [m for m in wanted if m in set(cox.model)]
    aft_model = HORIZON_MODEL
    covariates = [c for c in COVARIATE_LABELS if c in set(cox[cox.model.isin(blocks)].covariate)]
    header = [""] + [m.split(" ", 1)[0] for m in blocks] + ["AFT (2h)"]
    rows: list[list[str] | str] = []
    for covariate in covariates:
        line = [COVARIATE_LABELS[covariate]]
        for block in blocks:
            hit = cox[(cox.model == block) & (cox.covariate == covariate)]
            line.append("" if hit.empty else ratio_cell(*hit[["hr", "hr_low", "hr_high", "p"]].iloc[0]))
        hit = aft[(aft.model == aft_model) & (aft.covariate == covariate)]
        line.append("" if hit.empty else ratio_cell(*hit[["tr", "tr_low", "tr_high", "p"]].iloc[0]))
        rows.append(line)
    rows.append("Sample")
    n_row, c_row = ["Matters"], ["Concordance"]
    for block in blocks:
        first = cox[cox.model == block].iloc[0]
        n_row.append(n(first.n))
        c_row.append(f"{first.concordance:.3f}")
    aft_rows = aft[aft.model == aft_model]
    n_row.append(n(aft_rows.n.iloc[0]) if len(aft_rows) else "--")
    c_row.append(f"$\\rho$ = {aft_rows.rho.iloc[0]:.2f}" if len(aft_rows) else "--")
    rows += [n_row, c_row]
    return tabular("r" * (len(header) - 1), header, rows)


def table_channels(shares: pd.DataFrame, cif_at: pd.DataFrame) -> str:
    """LaTeX: first-detection channels at matter level, with the cumulative incidence at 2 and 5 years.

    Args:
        shares (pd.DataFrame): from ``channel_shares``.
        cif_at (pd.DataFrame): R's cumulative incidence at fixed times (cohort).

    Returns:
        str: the table body.
    """
    rows: list[list[str] | str] = []
    cohort = shares[shares["sample"] == "Cohort"]
    for group in CHANNEL_GROUPS:
        share = cohort[cohort.measure == f"Share of all: {CHANNEL_GROUP_LABELS[group]}"].iloc[0]
        at = cif_at[cif_at.channel == group].set_index("time").cif
        rows.append([CHANNEL_GROUP_LABELS[group], n(share["count"]), pct(share["count"], share.base),
                     pct(at.get(24, np.nan) or 0, 1), pct(at.get(60, np.nan) or 0, 1)])
    return tabular("rrrr", ["Channel", "Matters", "Share", "CIF 2 years", "CIF 5 years"], rows)


def table_h4(shares: pd.DataFrame) -> str:
    """LaTeX: H4, the surveillance share of first detections under each definition and denominator.

    Args:
        shares (pd.DataFrame): from ``channel_shares``.

    Returns:
        str: the table body.
    """
    rows: list[list[str] | str] = []
    labels = {"Surveillance, of stated": "Surveillance, of matters stating a channel",
              "Surveillance without flagged details, of stated": "\\quad without observed patterns or filings",
              "Explicit alert, of stated": "\\quad explicit alerts only",
              "Surveillance, of all (lower bound)": "Surveillance, of all matters (lower bound)",
              "Surveillance or unclear, of all (upper bound)": "Surveillance or unclear, of all (upper bound)"}
    for sample in ("Cohort", "All windowed matters", "Listed-issuer issuer frauds"):
        rows.append(sample)
        part = shares[shares["sample"] == sample].set_index("measure")
        for measure, label in labels.items():
            row = part.loc[measure]
            rows.append([label, n(row["count"]), n(row.base), pct(row["count"], row.base),
                         f"{100 * row.low:.1f}--{100 * row.high:.1f}"])
    return tabular("rrrr", ["", "Matters", "Of", "Share", "95\\% CI"], rows)


def table_robustness(robust: pd.DataFrame, trunc: pd.DataFrame, hand: dict) -> str:
    """LaTeX: robustness of the median lag and the main hazard ratios; naive vs truncation-adjusted quantiles.

    Args:
        robust (pd.DataFrame): from ``robustness``.
        trunc (pd.DataFrame): R's truncation summary (months).
        hand (dict): the hand-check summary.

    Returns:
        str: the table body.
    """
    rows: list[list[str] | str] = []
    for row in robust.itertuples(index=False):
        rows.append([row.variant, n(row.n), years_cell(row.median_years, row.q25, row.q75),
                     ratio_cell(row.post_lodr_hr, row.post_lodr_low, row.post_lodr_high, row.post_lodr_p),
                     ratio_cell(row.listed_hr, row.listed_low, row.listed_high, row.listed_p)])
    rows.append("Right truncation: naive and adjusted quantiles of $T$ (years)")
    for sample in ("full", "cohort"):
        part = trunc[trunc["sample"] == sample].set_index("quantile")
        label = "All windowed matters" if sample == "full" else "Cohort"
        naive = "/".join(f"{part.loc[q, 'naive'] / 12:.2f}" for q in (0.25, 0.5, 0.75))
        adjusted = "/".join(f"{part.loc[q, 'adjusted'] / 12:.2f}" for q in (0.25, 0.5, 0.75))
        rows.append([label, n(part.n.iloc[0]), f"naive {naive}", f"adjusted {adjusted}", ""])
    if hand.get("n"):
        rows.append("Hand-coded validation orders (order level)")
        rows.append([f"Hand window vs pipeline window, same first action", n(hand["n"]),
                     f"{hand['median_hand']:.2f} vs {hand['median_machine']:.2f}",
                     f"corr. {hand['correlation']:.2f}", f"{100 * hand['within_month']:.0f}\\% within a month"])
    return tabular("rrrr", ["Variant", "Matters", "Median $T$ (IQR)", "HR post-LODR", "HR listed"], rows)


def table_finegray(finegray: pd.DataFrame, cause: pd.DataFrame) -> str:
    """LaTeX: Fine–Gray subdistribution hazard ratios per channel, with the cause-specific post-LODR check.

    Args:
        finegray (pd.DataFrame): R's ``crr`` rows (covariates prefixed ``x_``).
        cause (pd.DataFrame): cause-specific Cox rows (``model`` = channel).

    Returns:
        str: the table body.
    """
    channels = [c for c in ("SURVEILLANCE", "COMPLAINT", "INSPECTION", "REFERRAL") if c in set(finegray.channel)]
    rows: list[list[str] | str] = []
    for covariate in COVARIATE_LABELS:
        part = finegray[finegray.covariate == f"x_{covariate}"]
        if part.empty:
            continue
        line = [COVARIATE_LABELS[covariate]]
        for channel in channels:
            hit = part[part.channel == channel]
            if hit.empty:
                line.append("--")
                continue
            se = hit.se.iloc[0]
            coef = hit.coef.iloc[0]
            line.append(ratio_cell(hit.shr.iloc[0], math.exp(coef - 1.96 * se), math.exp(coef + 1.96 * se),
                                   hit.p.iloc[0]))
        rows.append(line)
    rows.append("Check: cause-specific Cox")
    line = ["Window ends after LODR"]
    for channel in channels:
        hit = cause[(cause.model == channel) & (cause.covariate == "post_lodr")]
        line.append("--" if hit.empty else ratio_cell(*hit[["hr", "hr_low", "hr_high", "p"]].iloc[0]))
    rows.append(line)
    events = ["First detections"] + [n(finegray[finegray.channel == c].n_events.iloc[0]) for c in channels]
    rows.append(events)
    return tabular("r" * len(channels), ["", *[CHANNEL_GROUP_LABELS[c] for c in channels]], rows)


# ----- figures -----------------------------------------------------------------------------------------------------


def figures(frame: pd.DataFrame, r_out: dict[str, pd.DataFrame], path: pathlib.Path) -> None:
    """The Stage 1 figures, one per page: distribution, by category, truncation, cumulative incidence.

    Args:
        frame (pd.DataFrame): the analysis frame.
        r_out (dict[str, pd.DataFrame]): R's outputs.
        path (pathlib.Path): the PDF to write.

    Returns:
        None.
    """
    set_style()
    positive = frame[frame.positive]
    cohort = positive[positive.cohort]
    with PdfPages(path) as pdf:
        fig, axes = plt.subplots(1, 2, figsize=(6.3, 2.6))
        bins = np.arange(0, 16.5, 0.5)
        axes[0].hist(positive.t_years.clip(upper=16), bins=bins, color=NEUTRAL, label="All")
        axes[0].hist(cohort.t_years.clip(upper=16), bins=bins, color=ACCENT, alpha=0.8, label="Cohort")
        axes[0].set_xlabel("Detection lag $T$ (years)")
        axes[0].set_ylabel("Matters")
        axes[0].legend()
        for rows, colour, label in ((positive, NEUTRAL_DARK, "All"), (cohort, ACCENT, "Cohort")):
            km = KaplanMeierFitter().fit(rows.t_years, rows.event)
            axes[1].step(km.timeline, 1 - km.survival_function_.iloc[:, 0], where="post", color=colour,
                         label=f"{label} (median {rows.t_years.median():.2f})")
        axes[1].set_xlim(0, 15)
        axes[1].set_xlabel("Years after the window's end")
        axes[1].set_ylabel("Share acted on")
        axes[1].legend(loc="lower right")
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(6.3, 3.0))
        palette = [ACCENT, INK, NEUTRAL_DARK, "#c0712a", "#6a8f3a", NEUTRAL]
        for colour, group in zip(palette, CATEGORY_GROUPS):
            rows = cohort[cohort.category_group == group]
            if len(rows) < 20:
                continue
            km = KaplanMeierFitter().fit(rows.t_years, rows.event)
            ax.step(km.timeline, 1 - km.survival_function_.iloc[:, 0], where="post", color=colour,
                    label=f"{GROUP_LABELS[group]} ({len(rows)})")
        ax.set_xlim(0, 15)
        ax.set_xlabel("Years after the window's end (cohort: windows ending by 2018)")
        ax.set_ylabel("Share acted on")
        ax.legend(loc="lower right", fontsize=7)
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(6.3, 2.8))
        curve = r_out["truncation_curve"]
        for (estimator, colour) in (("naive", NEUTRAL_DARK), ("adjusted", ACCENT)):
            part = curve[(curve["sample"] == "full") & (curve.estimator == estimator)]
            ax.step(part.time / 12, part.cdf, where="post", color=colour,
                    label="Naive (empirical)" if estimator == "naive" else "Adjusted for right truncation")
        ax.set_xlim(0, 20)
        ax.set_xlabel("Detection lag $T$ (years), all windowed matters")
        ax.set_ylabel("Cumulative share")
        ax.legend(loc="lower right")
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(6.3, 2.8))
        cif = r_out["cif_curves"]
        palette = dict(zip(CHANNEL_GROUPS, [ACCENT, INK, "#c0712a", "#6a8f3a", NEUTRAL_DARK, NEUTRAL]))
        for group in CHANNEL_GROUPS:
            part = cif[cif.channel == group]
            if part.empty:
                continue
            ax.step(part.time / 12, part.cif, where="post", color=palette[group], label=CHANNEL_GROUP_LABELS[group])
        ax.set_xlim(0, 15)
        ax.set_xlabel("Years after the window's end (cohort)")
        ax.set_ylabel("Cumulative incidence")
        ax.legend(loc="upper left", fontsize=7)
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)


# ----- main --------------------------------------------------------------------------------------------------------


def headline(frame: pd.DataFrame, desc: pd.DataFrame, models: dict[str, pd.DataFrame], shares: pd.DataFrame,
             r_out: dict[str, pd.DataFrame], robust: pd.DataFrame, hand: dict, cause: pd.DataFrame) -> dict:
    """The numbers the text quotes.

    Args:
        frame, desc, models, shares, r_out, robust, hand, cause: the stage's results.

    Returns:
        dict: JSON-ready headline figures.
    """
    def pick(table: pd.DataFrame, model: str, covariate: str, column: str = "hr") -> float | None:
        """One coefficient.

        Args:
            table (pd.DataFrame): a model table.
            model (str): the model name.
            covariate (str): the covariate.
            column (str): the column.

        Returns:
            float | None: the value.
        """
        hit = table[(table.model == model) & (table.covariate == covariate)]
        return None if hit.empty else float(hit[column].iloc[0])

    trunc = r_out["truncation_summary"].set_index(["sample", "quantile"])
    cohort_shares = shares[shares["sample"] == "Cohort"].set_index("measure")
    out = {
        "matters_windowed": int(len(frame)), "matters_positive": int(frame.positive.sum()),
        "nonpositive_share": float(1 - frame.positive.mean()),
        "cohort_matters": int(frame.cohort.sum()), "cohort_positive": int((frame.cohort & frame.positive).sum()),
        "median_t_all": float(desc.iloc[0].t_median), "iqr_t_all": [float(desc.iloc[0].t_q25), float(desc.iloc[0].t_q75)],
        "median_t_cohort": float(desc.iloc[1].t_median),
        "iqr_t_cohort": [float(desc.iloc[1].t_q25), float(desc.iloc[1].t_q75)],
        "median_r_cohort": float(desc.iloc[1].r_median),
        "trunc_median_naive_full": float(trunc.loc[("full", 0.5), "naive"] / 12),
        "trunc_median_adjusted_full": float(trunc.loc[("full", 0.5), "adjusted"] / 12),
        "trunc_median_naive_cohort": float(trunc.loc[("cohort", 0.5), "naive"] / 12),
        "trunc_median_adjusted_cohort": float(trunc.loc[("cohort", 0.5), "adjusted"] / 12),
        "hr_post_lodr_block2": pick(models["cox"], "(2) + Regime", "post_lodr"),
        "p_post_lodr_block2": pick(models["cox"], "(2) + Regime", "post_lodr", "p"),
        "hr_post_lodr_unrestricted": pick(models["cox"], "(2u) unrestricted sample", "post_lodr"),
        "hr_post_lodr_horizon": pick(models["cox"], HORIZON_MODEL, "post_lodr"),
        "p_post_lodr_horizon": pick(models["cox"], HORIZON_MODEL, "post_lodr", "p"),
        "ci_post_lodr_horizon": [pick(models["cox"], HORIZON_MODEL, "post_lodr", "hr_low"),
                                 pick(models["cox"], HORIZON_MODEL, "post_lodr", "hr_high")],
        "tr_post_lodr_horizon": pick(models["aft"], HORIZON_MODEL, "post_lodr", "tr"),
        "hr_post_lodr_stratified": pick(models["cox"], "(2s) + Regime, stratified by category", "post_lodr"),
        "horizon_years": HORIZON_MONTHS * MONTH_DAYS / YEAR_DAYS,
        "horizon_matters": int(models["cox"][models["cox"].model == HORIZON_MODEL].n.iloc[0]),
        "finegray_post_lodr": {row.channel: [float(row.shr), float(row.p)] for row in
                               r_out["finegray"][r_out["finegray"].covariate == "x_post_lodr"].itertuples()},
        "hr_post_nfra": pick(models["cox"], "(2b) + NFRA", "post_nfra"),
        "n_post_nfra_cohort": int(frame[frame.cohort & frame.positive].post_nfra.sum()),
        "tr_post_lodr_aft": pick(models["aft"], "(2) + Regime", "post_lodr", "tr"),
        "surveillance_of_stated_cohort": float(cohort_shares.loc["Surveillance, of stated", "share"]),
        "alert_of_stated_cohort": float(cohort_shares.loc["Explicit alert, of stated", "share"]),
        "surveillance_bounds_cohort": [float(cohort_shares.loc["Surveillance, of all (lower bound)", "share"]),
                                       float(cohort_shares.loc["Surveillance or unclear, of all (upper bound)",
                                                               "share"])],
        "unclear_share_cohort": float(cohort_shares.loc["Share of all: Unclear", "share"]),
        "ph_failures": models["ph"][models["ph"].p < 0.05][["model", "covariate"]].values.tolist(),
        "hand_check": hand,
        "robustness_median_range": [float(robust.median_years.min()), float(robust.median_years.max())],
    }
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.stage1``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR, help="where the inputs are")
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="where to write (default: derived/stage1 and paper/; a scratch dir writes everything there)")
    parser.add_argument("--ownership", action="store_true",
                        help="use shareholding.parquet (only after the shareholding download has finished)")
    parser.add_argument("--no-visibility", action="store_true", help="skip the price-based covariates")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run Stage 1 end to end.

    Args:
        argv (list[str] | None): command-line arguments (None = ``sys.argv``).

    Returns:
        int: exit status (0).
    """
    args = build_arg_parser().parse_args(argv)
    scratch = args.out is not None
    out_dir = args.out if scratch else args.derived / "stage1"
    tables_dir = out_dir if scratch else PAPER_DIR / "tables"
    figure_path = (out_dir if scratch else PAPER_DIR) / "figures_stage1.pdf"
    numbers_path = out_dir / "stage1_numbers.json" if scratch else args.derived / "stage1_numbers.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.log_level, out_dir / "stage1.log" if scratch else LOG_FILE)
    warnings.filterwarnings("ignore", category=FutureWarning)

    matters = pd.read_parquet(args.derived / "matters.parquet")
    visibility = None if args.no_visibility else market_visibility(matters, args.derived, args.ownership)
    if visibility is not None:
        log.info("visibility         %d listed matters with prices in the window-end year", len(visibility))
    frame = analysis_frame(matters, visibility)
    frame.to_parquet(out_dir / "analysis.parquet", index=False)
    nonpositive = frame[~frame.positive][["matter_id", "matter_name", "n_orders", "window_start", "window_end",
                                          "window_end_min", "first_action_date", "first_action_kind", "t_years",
                                          "text_stems"]]
    nonpositive.sort_values("t_years").to_csv(out_dir / "nonpositive.csv", index=False)
    log.info("matters            %d windowed; positive %d; non-positive %d (%.1f%%); cohort %d (positive %d)",
             len(frame), frame.positive.sum(), (~frame.positive).sum(), 100 * (1 - frame.positive.mean()),
             frame.cohort.sum(), (frame.cohort & frame.positive).sum())

    desc = describe(frame)
    desc.to_csv(out_dir / "descriptives.csv", index=False)
    log.info("detection lag      all %.2f y (IQR %.2f–%.2f); cohort %.2f y (IQR %.2f–%.2f); resolution (cohort) %.2f y",
             desc.iloc[0].t_median, desc.iloc[0].t_q25, desc.iloc[0].t_q75, desc.iloc[1].t_median,
             desc.iloc[1].t_q25, desc.iloc[1].t_q75, desc.iloc[1].r_median)

    has_ownership = args.ownership and "promoter_pct" in frame
    models = run_models(frame, has_ownership)
    for name, table in models.items():
        table.to_csv(out_dir / f"{name}.csv", index=False)
    cause = cause_specific(frame)
    cause.to_csv(out_dir / "cause_specific.csv", index=False)
    shares = channel_shares(frame)
    shares.to_csv(out_dir / "channel_shares.csv", index=False)
    robust, hand = robustness(frame, args.derived)
    robust.to_csv(out_dir / "robustness.csv", index=False)
    r_out = run_r(frame, out_dir)

    (tables_dir / "stage1_descriptive.tex").write_text(table_descriptive(desc))
    (tables_dir / "stage1_models.tex").write_text(table_models(models["cox"], models["aft"]))
    (tables_dir / "stage1_channels.tex").write_text(table_channels(shares, r_out["cif_at"]))
    (tables_dir / "stage1_h4.tex").write_text(table_h4(shares))
    (tables_dir / "stage1_robustness.tex").write_text(table_robustness(robust, r_out["truncation_summary"], hand))
    (tables_dir / "stage1_finegray.tex").write_text(table_finegray(r_out["finegray"], cause))
    figures(frame, r_out, figure_path)
    numbers = headline(frame, desc, models, shares, r_out, robust, hand, cause)
    numbers_path.write_text(json.dumps(numbers, indent=2, default=float))
    log.info("headline           %s", json.dumps(numbers, default=float)[:1500])
    log.info("wrote %s, %s, %s and the stage1_*.tex tables", out_dir, figure_path, numbers_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
