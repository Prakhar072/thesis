"""Stage 2 robustness: partial observability of fraud (technical plan §6.5; Poirier 1980; Wang, Winton and Yu 2010).

A fraud year is labelled only when a firm both **commits** fraud and is **detected**:
``P(y = 1) = Φ(x'β_C) · Φ(w'β_D)`` (bivariate probit with partial observability, errors independent: ρ = 0). The
commission equation takes Stage 2's market predictors. The detection equation takes visibility: log median turnover and
whether the firm trades mainly on NSE. **Identification rests on the exclusion of the NSE indicator from commission** (the
exchange where a firm trades shapes surveillance, plausibly not the decision to commit fraud) and on functional form. It
is contestable, as the plan says. The exercise is a sensitivity check, not a headline.

It reports the two equations, the pseudo-log-likelihood against a single probit on the same predictors, the implied
average probabilities of commission and of detection given commission, and whether ranking firms by the commission
probability changes Stage 2's recall at SEBI's capacity on the test years.

Reads ``stage2/frame.parquet`` and ``panel`` (main exchange). Writes ``derived/stage2/partial_observability.csv``,
``derived/partial_observability.json``, ``paper/tables/stage2_partial.tex``, ``logs/partial_observability.log``.
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib

import numpy as np
import pandas as pd
from scipy import optimize, stats

from src import cache
from src.extract_text import setup_logging
from src.paper_tables import tabular
from src.stage2 import FEATURE_LABELS, MARKET, MAIN_SPEC, SEBI_CAPACITY, SPECS, Prepared, recall_at_k

log = logging.getLogger("partial_observability")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
PAPER_DIR = REPO_ROOT / "paper"
LOG_FILE = REPO_ROOT / "logs" / "partial_observability.log"

DETECTION = ["log_turnover", "nse_main"]
EPS = 1e-10


def usable_columns(frame: pd.DataFrame, prep: Prepared) -> list[str]:
    """Commission columns that vary and are not copies of an earlier column (on the training years).

    The missing-value indicators of the prior-year predictors coincide (a firm's first year lacks all of them), and the
    zero-volume share is constant; either makes the Hessian singular.

    Args:
        frame (pd.DataFrame): training firm-years.
        prep (Prepared): fitted on them.

    Returns:
        list[str]: the columns to keep.
    """
    x = prep.linear(frame)
    keep: list[str] = []
    for column in x.columns:
        values = x[column].to_numpy()
        if np.nanstd(values) == 0 or any(np.array_equal(values, x[k].to_numpy()) for k in keep):
            continue
        keep.append(column)
    return keep


def design(frame: pd.DataFrame, prep: Prepared,
           keep: list[str] | None = None) -> tuple[np.ndarray, np.ndarray, list[str], list[str]]:
    """Commission and detection design matrices (with constants), from training-fitted preparation.

    Args:
        frame (pd.DataFrame): firm-years with the market predictors and ``nse_main``.
        prep (Prepared): fitted on the training years.
        keep (list[str] | None): commission columns to keep (from ``usable_columns``); None keeps all.

    Returns:
        tuple[np.ndarray, np.ndarray, list[str], list[str]]: X (commission), W (detection) and their column names.
    """
    x = prep.linear(frame)
    x = x[[c for c in x.columns if c in keep]] if keep is not None else x
    turnover = x["log_turnover"].to_numpy()
    w = np.column_stack([np.ones(len(frame)), turnover, frame.nse_main.to_numpy(dtype=float)])
    xs = np.column_stack([np.ones(len(frame)), x.to_numpy()])
    return xs, w, ["const"] + list(x.columns), ["const"] + DETECTION


def negloglik(params: np.ndarray, y: np.ndarray, x: np.ndarray, w: np.ndarray, weight: np.ndarray) -> float:
    """Weighted negative pseudo-log-likelihood of the partial-observability probit.

    Args:
        params (np.ndarray): β_C then β_D.
        y (np.ndarray): labels.
        x (np.ndarray): commission design.
        w (np.ndarray): detection design.
        weight (np.ndarray): population weights.

    Returns:
        float: the negative log-likelihood.
    """
    beta_c, beta_d = params[:x.shape[1]], params[x.shape[1]:]
    p = stats.norm.cdf(x @ beta_c) * stats.norm.cdf(w @ beta_d)
    p = np.clip(p, EPS, 1 - EPS)
    return float(-(weight * (y * np.log(p) + (1 - y) * np.log(1 - p))).sum())


def probit_negloglik(params: np.ndarray, y: np.ndarray, x: np.ndarray, weight: np.ndarray) -> float:
    """Weighted negative log-likelihood of a single probit (the comparison model).

    Args:
        params (np.ndarray): coefficients.
        y (np.ndarray): labels.
        x (np.ndarray): design.
        weight (np.ndarray): population weights.

    Returns:
        float: the negative log-likelihood.
    """
    p = np.clip(stats.norm.cdf(x @ params), EPS, 1 - EPS)
    return float(-(weight * (y * np.log(p) + (1 - y) * np.log(1 - p))).sum())


def standard_errors(fun, params: np.ndarray, step: float = 1e-4) -> np.ndarray:
    """Standard errors from a finite-difference Hessian of the negative log-likelihood.

    Args:
        fun (Callable[[np.ndarray], float]): the negative log-likelihood.
        params (np.ndarray): the estimate.
        step (float): finite-difference step.

    Returns:
        np.ndarray: standard errors (NaN where the Hessian is not invertible).
    """
    k = len(params)
    hessian = np.zeros((k, k))
    for i in range(k):
        for j in range(i, k):
            e_i, e_j = np.eye(k)[i] * step, np.eye(k)[j] * step
            value = (fun(params + e_i + e_j) - fun(params + e_i - e_j) - fun(params - e_i + e_j)
                     + fun(params - e_i - e_j)) / (4 * step * step)
            hessian[i, j] = hessian[j, i] = value
    try:
        return np.sqrt(np.clip(np.diag(np.linalg.inv(hessian)), 0, None))
    except np.linalg.LinAlgError:
        return np.full(k, np.nan)


def estimate(frame: pd.DataFrame, spec_name: str = MAIN_SPEC) -> tuple[pd.DataFrame, dict]:
    """Fit the partial-observability and single probits on the training years; evaluate on the test years.

    Args:
        frame (pd.DataFrame): Stage 2's frame with ``nse_main``.
        spec_name (str): the Stage 2 specification whose years and label to use.

    Returns:
        tuple[pd.DataFrame, dict]: coefficient rows (``equation``, ``term``, ``coef``, ``se``, ``z``, ``p``) and a
            summary (log-likelihoods, implied probabilities, recall at capacity).
    """
    spec = SPECS[spec_name]
    label = spec["label"]
    frame = frame[frame.nse_main.notna()]
    train = frame[frame.fy.between(*spec["train"])]
    test = frame[frame.fy.between(*spec["test"])]
    prep = Prepared(train, MARKET)
    keep = usable_columns(train, prep)
    x, w, x_names, w_names = design(train, prep, keep)
    y, weight = train[label].to_numpy(dtype=float), train.weight.to_numpy(dtype=float)
    weight = weight / weight.mean()

    probit = optimize.minimize(probit_negloglik, np.zeros(x.shape[1]), args=(y, x, weight), method="BFGS")
    start = np.concatenate([probit.x, [1.0, 0.0, 0.0]])
    start[0] = probit.x[0] / 2
    fit = optimize.minimize(negloglik, start, args=(y, x, w, weight), method="BFGS",
                            options={"maxiter": 20000, "gtol": 1e-4})
    se = standard_errors(lambda b: negloglik(b, y, x, w, weight), fit.x)
    rows = []
    for index, (equation, term) in enumerate([("commission", t) for t in x_names] + [("detection", t) for t in w_names]):
        coef = fit.x[index]
        z = coef / se[index] if se[index] > 0 else np.nan
        rows.append({"equation": equation, "term": term, "coef": coef, "se": se[index], "z": z,
                     "p": 2 * stats.norm.sf(abs(z)) if np.isfinite(z) else np.nan})
    probit_se = standard_errors(lambda b: probit_negloglik(b, y, x, weight), probit.x)
    for index, term in enumerate(x_names):
        z = probit.x[index] / probit_se[index] if probit_se[index] > 0 else np.nan
        rows.append({"equation": "single probit", "term": term, "coef": probit.x[index], "se": probit_se[index],
                     "z": z, "p": 2 * stats.norm.sf(abs(z)) if np.isfinite(z) else np.nan})

    beta_c, beta_d = fit.x[:x.shape[1]], fit.x[x.shape[1]:]
    p_commit = stats.norm.cdf(x @ beta_c)
    p_detect = stats.norm.cdf(w @ beta_d)
    x_test, w_test, _, _ = design(test, prep, keep)
    score_commit = stats.norm.cdf(x_test @ beta_c)
    score_probit = stats.norm.cdf(x_test @ probit.x)

    def recall(score: np.ndarray) -> float:
        """Mean recall at SEBI's capacity over the test years.

        Args:
            score (np.ndarray): scores for the test rows.

        Returns:
            float: recall.
        """
        values = []
        for fy, rows_of in test.assign(score=score).groupby("fy"):
            labels = rows_of[label].to_numpy()
            if labels.sum():
                values.append(recall_at_k(labels, rows_of.score.to_numpy(), rows_of.weight.to_numpy(dtype=float),
                                          SEBI_CAPACITY))
        return float(np.mean(values))

    summary = {"converged": bool(fit.success), "train_rows": int(len(train)), "train_frauds": int(y.sum()),
               "loglik_partial": float(-fit.fun), "loglik_probit": float(-probit.fun),
               "k_partial": int(len(fit.x)), "k_probit": int(len(probit.x)),
               "mean_p_commit": float(np.average(p_commit, weights=weight)),
               "mean_p_detect_given_commit": float(np.average(p_detect, weights=weight * p_commit)),
               "observed_rate": float(np.average(y, weights=weight)),
               "recall_402_commission": recall(score_commit), "recall_402_probit": recall(score_probit),
               "rank_correlation_test": float(stats.spearmanr(score_commit, score_probit).statistic)}
    return pd.DataFrame(rows), summary


def table(coefs: pd.DataFrame, summary: dict) -> str:
    """LaTeX: the two equations beside the single probit, and the implied probabilities.

    Args:
        coefs (pd.DataFrame): from ``estimate``.
        summary (dict): from ``estimate``.

    Returns:
        str: the table body.
    """
    def cell(equation: str, term: str) -> str:
        """One coefficient with stars, or empty.

        Args:
            equation (str): the equation.
            term (str): the term.

        Returns:
            str: the cell.
        """
        hit = coefs[(coefs.equation == equation) & (coefs.term == term)]
        if hit.empty:
            return ""
        row = hit.iloc[0]
        stars = "" if not np.isfinite(row.p) else "***" if row.p < 0.01 else "**" if row.p < 0.05 else "*" if row.p < 0.1 else ""
        return f"{row.coef:+.2f}{stars}"

    labels = {"const": "Constant", **FEATURE_LABELS, "nse_main": "Main exchange NSE"}
    terms = ["const"] + [t for t in MARKET if t in set(coefs.term)] + ["nse_main"]
    rows: list[list[str] | str] = []
    for term in terms:
        rows.append([labels[term], cell("commission", term), cell("detection", term), cell("single probit", term)])
    rows.append("Fit and implied probabilities (training years)")
    rows.append(["Pseudo-log-likelihood", f"{summary['loglik_partial']:.1f}", "", f"{summary['loglik_probit']:.1f}"])
    rows.append(["Mean P(commit)", f"{summary['mean_p_commit']:.3f}", "", ""])
    rows.append(["Mean P(detected $\\mid$ commit)", "", f"{summary['mean_p_detect_given_commit']:.3f}", ""])
    rows.append(["Recall at 402, test years", f"{100 * summary['recall_402_commission']:.1f}\\%", "",
                 f"{100 * summary['recall_402_probit']:.1f}\\%"])
    return tabular("rrr", ["", "Commission", "Detection", "Single probit"], rows)


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.partial_observability``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Estimate the partial-observability model and write its table.

    Args:
        argv (list[str] | None): command-line arguments (None = ``sys.argv``).

    Returns:
        int: exit status (0).
    """
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.log_level, LOG_FILE)
    frame = pd.read_parquet(args.derived / "stage2" / "frame.parquet")
    exchange = pd.read_parquet(args.derived / "panel.parquet", columns=["firm_id", "main_exchange"]) \
        .dropna().drop_duplicates("firm_id")
    frame = frame.merge(exchange, on="firm_id", how="left", validate="many_to_one")
    frame["nse_main"] = (frame.main_exchange == "NSE").astype(float).where(frame.main_exchange.notna())
    coefs, summary = estimate(frame)
    coefs.to_csv(args.derived / "stage2" / "partial_observability.csv", index=False)
    (args.derived / "partial_observability.json").write_text(json.dumps(summary, indent=2))
    (PAPER_DIR / "tables" / "stage2_partial.tex").write_text(table(coefs, summary))
    log.info("summary %s", json.dumps(summary))
    log.info("coefficients\n%s", coefs.round(3).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
