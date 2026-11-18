"""Stage 2: can fraud be predicted from public information before it is detected? (runbook Week 8; technical plan §6;
paper §6.2).

**Unit and label.** The firm-year of the Week 4 panel (fraud firms and the weighted random draw of controls), labelled by
the **violation year** (``fraud_year``: the financial year overlaps a window), never the order year. Years after a firm's
first public action are dropped (their signals reflect detection). A narrower label keeps only years in which the issuer
itself was a noticee.

**Predictors** (each the previous financial year's value, so it was public when the year began): annual return and
excess return over the BSE 500, volatility, log median turnover, log Amihud illiquidity, share of zero-volume days,
trading days; with ownership (FY2016 onward): log market capitalisation, promoter holding, its change, promoter shares
pledged and whether any are. Continuous predictors are winsorised at the training sample's 1st and 99th percentiles.
Scalers, imputers and winsorising limits are fitted on the training years only; nothing records the future (no
delisting or suspension flag).

**Models.** Logit; L1-penalised logit (the penalty chosen on the last two training years); gradient-boosted trees
(LightGBM, the number of trees chosen the same way). Class imbalance is handled with weights (each class's weighted total
equal), on top of the population weights (controls stand for 9.6 firms each).

**Validation is out of time.** Market-only: train FY2011–2018, test FY2019–2025 (the plan's split). Market and
ownership: train FY2017–2020, test FY2021–2025, beside the market-only model on the same split.

**The headline** is recall at inspection capacity: within each test year, firms ranked by score, controls counting with
their weight, the share of that year's (detected) frauds in the top *k*; *k* from 50 to 1,000, with SEBI's 402
investigations of 2025–26 marked, against random ranking (*k* / population). AUPRC and AUC are reported beside it, and a
permutation test (scores shuffled within each test year) gives H1's p-value against random ranking.

Reads ``panel``. Writes ``derived/stage2/``, ``derived/stage2_numbers.json``, ``paper/tables/stage2_*.tex``,
``paper/figures_stage2.pdf``, ``logs/stage2.log``. No network.
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import warnings

import matplotlib

matplotlib.use("Agg")
import lightgbm as lgb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score

from src import cache
from src.extract_text import setup_logging
from src.paper_tables import ACCENT, INK, NEUTRAL, NEUTRAL_DARK, n, set_style, tabular

log = logging.getLogger("stage2")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
PAPER_DIR = REPO_ROOT / "paper"
LOG_FILE = REPO_ROOT / "logs" / "stage2.log"

#: SEBI's investigations taken up in 2025–26 (Annual Report): the inspection capacity marked on the curve.
SEBI_CAPACITY = 402
K_GRID = list(range(50, 1001, 50)) + [SEBI_CAPACITY]
MARKET = ["ret", "excess_ret", "vol", "log_turnover", "log_amihud", "zero_volume_share", "trading_days"]
OWNERSHIP = ["log_market_cap", "promoter_pct", "promoter_pct_change", "pledged_pct", "pledged_any"]
FEATURE_LABELS = {"ret": "Return", "excess_ret": "Excess return", "vol": "Volatility",
                  "log_turnover": "Log median turnover", "log_amihud": "Log Amihud illiquidity",
                  "zero_volume_share": "Zero-volume days (share)", "trading_days": "Trading days",
                  "log_market_cap": "Log market cap", "promoter_pct": "Promoter holding",
                  "promoter_pct_change": "Change in promoter holding", "pledged_pct": "Promoter shares pledged",
                  "pledged_any": "Any pledge"}
SPECS = {
    "Market, split 2018": {"features": MARKET, "train": (2011, 2018), "test": (2019, 2025), "label": "fraud_year"},
    "Market, split 2020": {"features": MARKET, "train": (2011, 2020), "test": (2021, 2025), "label": "fraud_year"},
    "Market + ownership, split 2020": {"features": MARKET + OWNERSHIP, "train": (2017, 2020), "test": (2021, 2025),
                                       "label": "fraud_year"},
    "Market, split 2018, issuer a noticee": {"features": MARKET, "train": (2011, 2018), "test": (2019, 2025),
                                             "label": "issuer_fraud"},
}
MAIN_SPEC = "Market, split 2018"
MODELS = ("Logit", "LASSO logit", "Gradient boosting")


# ----- the frame ---------------------------------------------------------------------------------------------------


def build_frame(panel: pd.DataFrame) -> pd.DataFrame:
    """Firm-years with their labels and the previous year's predictors.

    Args:
        panel (pd.DataFrame): ``panel.parquet``.

    Returns:
        pd.DataFrame: one row per firm-year before the firm's first public action: ``firm_id``, ``fy``, ``role``,
            ``weight``, ``fraud_year`` (0/1), ``issuer_fraud`` (0/1), and the lagged predictors.
    """
    work = panel.copy()
    work["log_turnover"] = np.log(work.turnover_median.where(work.turnover_median > 0))
    work["log_amihud"] = np.log(work.amihud.where(work.amihud > 0))
    work["log_market_cap"] = np.log(work.market_cap.where(work.market_cap > 0))
    work["pledged_any"] = (work.pledged_pct > 0).astype(float).where(work.pledged_pct.notna())
    lagged = work[["firm_id", "fy"] + MARKET + OWNERSHIP].copy()
    lagged["fy"] = lagged.fy + 1
    frame = work[["firm_id", "fy", "role", "weight", "fraud_year", "issuer_noticee", "post_public"]].merge(
        lagged, on=["firm_id", "fy"], how="left", validate="one_to_one")
    frame = frame[~frame.post_public.astype(bool)].drop(columns="post_public")
    frame["fraud_year"] = frame.fraud_year.astype(bool).astype(int)
    frame["issuer_fraud"] = (frame.fraud_year.astype(bool) & frame.issuer_noticee.fillna(False).astype(bool)).astype(int)
    return frame.drop(columns="issuer_noticee").reset_index(drop=True)


class Prepared:
    """Winsorising limits, imputation values and scales fitted on training rows only.

    Attributes:
        features (list[str]): the predictor columns.
        low (pd.Series): 1st percentiles.
        high (pd.Series): 99th percentiles.
        fill (pd.Series): training medians (for the linear models).
        mean (pd.Series): training means after filling.
        scale (pd.Series): training standard deviations after filling.
    """

    def __init__(self, train: pd.DataFrame, features: list[str]) -> None:
        """Fit on the training rows.

        Args:
            train (pd.DataFrame): training firm-years.
            features (list[str]): predictors.

        Returns:
            None.
        """
        self.features = features
        self.low = train[features].quantile(0.01)
        self.high = train[features].quantile(0.99)
        clipped = train[features].clip(self.low, self.high, axis=1)
        self.fill = clipped.median()
        filled = clipped.fillna(self.fill)
        self.mean = filled.mean()
        self.scale = filled.std().replace(0, 1)

    def trees(self, rows: pd.DataFrame) -> pd.DataFrame:
        """Winsorised predictors, missing left missing (for the trees).

        Args:
            rows (pd.DataFrame): firm-years.

        Returns:
            pd.DataFrame: the predictors.
        """
        return rows[self.features].clip(self.low, self.high, axis=1)

    def linear(self, rows: pd.DataFrame) -> pd.DataFrame:
        """Winsorised, median-filled, standardised predictors plus missing indicators (for the logits).

        Args:
            rows (pd.DataFrame): firm-years.

        Returns:
            pd.DataFrame: the design matrix.
        """
        clipped = self.trees(rows)
        missing = clipped.isna().astype(float).add_suffix("_missing")
        standard = (clipped.fillna(self.fill) - self.mean) / self.scale
        return pd.concat([standard, missing[[c for c in missing if c.removesuffix("_missing") in self.features]]],
                         axis=1)


def fit_weights(rows: pd.DataFrame, label: str) -> np.ndarray:
    """Population weights times class weights that give both classes the same weighted total.

    Args:
        rows (pd.DataFrame): training firm-years with ``weight``.
        label (str): the label column.

    Returns:
        np.ndarray: one weight per row.
    """
    weight = rows.weight.to_numpy(dtype=float)
    y = rows[label].to_numpy()
    positive, negative = weight[y == 1].sum(), weight[y == 0].sum()
    return np.where(y == 1, weight * (positive + negative) / (2 * positive),
                    weight * (positive + negative) / (2 * negative))


# ----- evaluation --------------------------------------------------------------------------------------------------


def recall_at_k(y: np.ndarray, score: np.ndarray, weight: np.ndarray, k: float) -> float:
    """Share of the year's frauds among the top-*k* firms, each firm counting with its population weight.

    Args:
        y (np.ndarray): labels (0/1).
        score (np.ndarray): model scores.
        weight (np.ndarray): population weights.
        k (float): capacity (firms investigated).

    Returns:
        float: recall (NaN when the year has no fraud).
    """
    if y.sum() == 0:
        return float("nan")
    order = np.argsort(-score, kind="stable")
    cumulative = np.cumsum(weight[order])
    taken = np.clip(k - (cumulative - weight[order]), 0, weight[order]) / weight[order]
    return float((y[order] * taken).sum() / y.sum())


def evaluate(test: pd.DataFrame, score: np.ndarray, label: str) -> dict:
    """Recall at each capacity (averaged over test years), AUPRC and AUC (weighted).

    Args:
        test (pd.DataFrame): test firm-years with ``fy``, ``weight`` and the label.
        score (np.ndarray): scores for the rows.
        label (str): the label column.

    Returns:
        dict: ``recall`` {k: mean over years}, ``random`` {k: mean of k / population}, ``auprc``, ``auc``,
            ``base_rate``, ``by_year`` (list of per-year recall at 402).
    """
    test = test.assign(score=score)
    recall = {k: [] for k in K_GRID}
    random = {k: [] for k in K_GRID}
    by_year = []
    for fy, rows in test.groupby("fy"):
        y, s, w = rows[label].to_numpy(), rows.score.to_numpy(), rows.weight.to_numpy(dtype=float)
        if y.sum() == 0:
            continue
        for k in K_GRID:
            recall[k].append(recall_at_k(y, s, w, k))
            random[k].append(min(1.0, k / w.sum()))
        by_year.append({"fy": int(fy), "frauds": int(y.sum()), "population": float(w.sum()),
                        "recall_402": recall_at_k(y, s, w, SEBI_CAPACITY)})
    y, w = test[label].to_numpy(), test.weight.to_numpy(dtype=float)
    return {"recall": {k: float(np.mean(v)) for k, v in recall.items()},
            "random": {k: float(np.mean(v)) for k, v in random.items()},
            "auprc": float(average_precision_score(y, test.score, sample_weight=w)),
            "auc": float(roc_auc_score(y, test.score, sample_weight=w)),
            "base_rate": float((y * w).sum() / w.sum()), "by_year": by_year}


def permutation_test(test: pd.DataFrame, score: np.ndarray, label: str, reps: int = 1000,
                     seed: int = 7) -> tuple[float, float]:
    """H1's test: is mean recall at 402 better than random ranking? Scores are shuffled within each test year.

    (A firm-clustered bootstrap of recall at a fixed capacity was biased upward here, by about five points, so the
    interval is not reported; the permutation test answers the hypothesis as the plan states it.)

    Args:
        test (pd.DataFrame): test firm-years with ``fy``, ``weight`` and the label.
        score (np.ndarray): the model's scores.
        label (str): the label column.
        reps (int): permutations.
        seed (int): random seed.

    Returns:
        tuple[float, float]: one-sided p-value (share of random rankings reaching the model's recall, with the +1
            correction) and the 95th percentile of random recall.
    """
    rng = np.random.default_rng(seed)
    years = [(g[label].to_numpy(), g.weight.to_numpy(dtype=float), s)
             for (_, g), s in zip(test.assign(score=score).groupby("fy"),
                                  [g.score.to_numpy() for _, g in test.assign(score=score).groupby("fy")])
             if g[label].sum()]
    observed = np.mean([recall_at_k(y, s, w, SEBI_CAPACITY) for y, w, s in years])
    random = np.array([np.mean([recall_at_k(y, rng.permutation(s), w, SEBI_CAPACITY) for y, w, s in years])
                       for _ in range(reps)])
    return float((1 + (random >= observed).sum()) / (1 + reps)), float(np.percentile(random, 95))


# ----- models ------------------------------------------------------------------------------------------------------


def inner_split(train: pd.DataFrame, years: tuple[int, int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The last two training years validate the tuning; earlier years fit it.

    Args:
        train (pd.DataFrame): training firm-years.
        years (tuple[int, int]): first and last training year.

    Returns:
        tuple[pd.DataFrame, pd.DataFrame]: inner training and validation rows.
    """
    cut = years[1] - 2
    return train[train.fy <= cut], train[train.fy > cut]


def fit_models(train: pd.DataFrame, test: pd.DataFrame, spec: dict) -> dict[str, dict]:
    """Fit the three models on the training years and score the test years.

    Args:
        train (pd.DataFrame): training firm-years.
        test (pd.DataFrame): test firm-years.
        spec (dict): ``features``, ``train`` years, ``label``.

    Returns:
        dict[str, dict]: model → ``score`` (test scores), ``coef`` (Series, linear models) or ``importance``
            (Series, trees), ``tuned`` (the chosen penalty or number of trees).
    """
    features, label = spec["features"], spec["label"]
    out: dict[str, dict] = {}
    inner_train, inner_valid = inner_split(train, spec["train"])

    prep = Prepared(train, features)
    x_train, x_test = prep.linear(train), prep.linear(test)
    logit = LogisticRegression(C=1e4, max_iter=5000)
    logit.fit(x_train, train[label], sample_weight=fit_weights(train, label))
    out["Logit"] = {"score": logit.decision_function(x_test), "coef": pd.Series(logit.coef_[0], x_train.columns),
                    "tuned": None}

    inner_prep = Prepared(inner_train, features)
    best_c, best_ap = None, -1.0
    for c in (0.003, 0.01, 0.03, 0.1, 0.3, 1.0):
        model = LogisticRegression(penalty="l1", C=c, solver="liblinear", max_iter=5000)
        model.fit(inner_prep.linear(inner_train), inner_train[label], sample_weight=fit_weights(inner_train, label))
        ap = average_precision_score(inner_valid[label], model.decision_function(inner_prep.linear(inner_valid)),
                                     sample_weight=inner_valid.weight)
        if ap > best_ap:
            best_c, best_ap = c, ap
    lasso = LogisticRegression(penalty="l1", C=best_c, solver="liblinear", max_iter=5000)
    lasso.fit(x_train, train[label], sample_weight=fit_weights(train, label))
    out["LASSO logit"] = {"score": lasso.decision_function(x_test), "coef": pd.Series(lasso.coef_[0], x_train.columns),
                          "tuned": best_c}

    params = dict(learning_rate=0.03, num_leaves=15, min_child_samples=40, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, reg_lambda=1.0, verbose=-1, random_state=7)
    inner = lgb.LGBMClassifier(n_estimators=1000, **params)
    inner.fit(inner_prep.trees(inner_train), inner_train[label], sample_weight=fit_weights(inner_train, label),
              eval_set=[(inner_prep.trees(inner_valid), inner_valid[label])],
              eval_sample_weight=[inner_valid.weight.to_numpy(dtype=float)], eval_metric="average_precision",
              callbacks=[lgb.early_stopping(50, verbose=False)])
    trees = max(int(inner.best_iteration_ or 100), 20)
    model = lgb.LGBMClassifier(n_estimators=trees, **params)
    model.fit(prep.trees(train), train[label], sample_weight=fit_weights(train, label))
    out["Gradient boosting"] = {"score": model.predict_proba(prep.trees(test))[:, 1],
                                "importance": pd.Series(model.booster_.feature_importance("gain"), features),
                                "tuned": trees}
    return out


def run(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Every specification and model: metrics, recall curves, coefficients and importances.

    Args:
        frame (pd.DataFrame): from ``build_frame``.

    Returns:
        tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]: metrics (one row per spec × model), recall curves
            (spec, model, k, recall, random), coefficients/importances (spec, model, feature, value), and an empty
            dict kept for extensions.
    """
    metric_rows, curve_rows, coef_rows = [], [], []
    extra: dict = {}
    for spec_name, spec in SPECS.items():
        label = spec["label"]
        train = frame[frame.fy.between(*spec["train"])]
        test = frame[frame.fy.between(*spec["test"])]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fitted = fit_models(train, test, spec)
        log.info("%-40s train %d (%d frauds) test %d (%d frauds)", spec_name, len(train), train[label].sum(), len(test),
                 test[label].sum())
        for model_name, result in fitted.items():
            metrics = evaluate(test, result["score"], label)
            p_value, random_95 = permutation_test(test, result["score"], label)
            metric_rows.append({"spec": spec_name, "model": model_name, "train_rows": len(train),
                                "train_frauds": int(train[label].sum()), "test_rows": len(test),
                                "test_frauds": int(test[label].sum()), "base_rate": metrics["base_rate"],
                                "auc": metrics["auc"], "auprc": metrics["auprc"],
                                "recall_402": metrics["recall"][SEBI_CAPACITY],
                                "random_402": metrics["random"][SEBI_CAPACITY],
                                "p_vs_random": p_value, "random_402_p95": random_95, "tuned": result["tuned"],
                                "by_year": json.dumps(metrics["by_year"])})
            for k in K_GRID:
                curve_rows.append({"spec": spec_name, "model": model_name, "k": k, "recall": metrics["recall"][k],
                                   "random": metrics["random"][k]})
            values = result.get("coef", result.get("importance"))
            for feature, value in values.items():
                coef_rows.append({"spec": spec_name, "model": model_name, "feature": feature, "value": float(value)})
            log.info("  %-18s AUC %.3f AUPRC %.3f (base %.3f) recall@402 %.3f (random %.3f, 95th pct %.3f, p %.4f) "
                     "tuned %s", model_name, metrics["auc"], metrics["auprc"], metrics["base_rate"],
                     metrics["recall"][SEBI_CAPACITY], metrics["random"][SEBI_CAPACITY], random_95, p_value,
                     result["tuned"])
    return pd.DataFrame(metric_rows), pd.DataFrame(curve_rows), pd.DataFrame(coef_rows), extra


# ----- outputs -----------------------------------------------------------------------------------------------------


def table_metrics(metrics: pd.DataFrame) -> str:
    """LaTeX: out-of-time performance of every specification and model.

    Args:
        metrics (pd.DataFrame): from ``run``.

    Returns:
        str: the table body.
    """
    rows: list[list[str] | str] = []
    for spec in SPECS:
        part = metrics[metrics.spec == spec]
        if part.empty:
            continue
        first = part.iloc[0]
        rows.append(f"{spec}: train {n(first.train_rows)} firm-years ({n(first.train_frauds)} fraud), "
                    f"test {n(first.test_rows)} ({n(first.test_frauds)})")
        for row in part.itertuples(index=False):
            p_text = "$<$0.001" if row.p_vs_random < 0.001 else f"{row.p_vs_random:.3f}"
            rows.append([row.model, f"{row.auc:.3f}", f"{row.auprc:.3f}", f"{row.base_rate:.3f}",
                         f"{100 * row.recall_402:.1f}", f"{100 * row.random_402:.1f}",
                         f"{row.recall_402 / row.random_402:.2f}", p_text])
    header = ["", "AUC", "AUPRC", "Base rate", "Recall@402 (\\%)", "Random (\\%)", "Lift", "$p$ vs random"]
    return tabular("rrrrrrr", header, rows)


def table_coefficients(coefs: pd.DataFrame) -> str:
    """LaTeX: LASSO coefficients (standardised) and tree importance shares, main and ownership specifications.

    Args:
        coefs (pd.DataFrame): from ``run``.

    Returns:
        str: the table body.
    """
    columns = [(MAIN_SPEC, "LASSO logit"), (MAIN_SPEC, "Gradient boosting"),
               ("Market + ownership, split 2020", "LASSO logit"), ("Market + ownership, split 2020", "Gradient boosting")]
    rows: list[list[str] | str] = []
    for feature, label in FEATURE_LABELS.items():
        line = [label]
        for spec, model in columns:
            part = coefs[(coefs.spec == spec) & (coefs.model == model)]
            hit = part[part.feature == feature]
            if hit.empty:
                line.append("")
            elif model == "Gradient boosting":
                line.append(f"{100 * hit.value.iloc[0] / part.value.sum():.0f}\\%")
            else:
                line.append(f"{hit.value.iloc[0]:+.2f}" if hit.value.iloc[0] else "0")
        rows.append(line)
    header = ["", "LASSO (2018)", "Trees (2018)", "LASSO (own., 2020)", "Trees (own., 2020)"]
    return tabular("rrrr", header, rows)


def figures(curves: pd.DataFrame, path: pathlib.Path) -> None:
    """Recall against inspection capacity, with random ranking and SEBI's capacity marked.

    Args:
        curves (pd.DataFrame): from ``run``.
        path (pathlib.Path): the PDF to write.

    Returns:
        None.
    """
    set_style()
    with PdfPages(path) as pdf:
        for spec in (MAIN_SPEC, "Market + ownership, split 2020"):
            part = curves[(curves.spec == spec) & (curves.k != SEBI_CAPACITY)]
            if part.empty:
                continue
            fig, ax = plt.subplots(figsize=(6.3, 3.0))
            for model, colour in zip(MODELS, (NEUTRAL_DARK, INK, ACCENT)):
                rows = part[part.model == model].sort_values("k")
                ax.plot(rows.k, 100 * rows.recall, color=colour, label=model)
            rows = part[part.model == MODELS[0]].sort_values("k")
            ax.plot(rows.k, 100 * rows.random, color=NEUTRAL, linestyle="--", label="Random ranking")
            ax.axvline(SEBI_CAPACITY, color=NEUTRAL, linewidth=0.6)
            ax.text(SEBI_CAPACITY + 8, 2, "SEBI's 402", fontsize=7, color=NEUTRAL_DARK)
            ax.set_xlabel(f"Firms investigated per year, k ({spec})")
            ax.set_ylabel("Share of the year's frauds reached (%)")
            ax.legend(loc="upper left", fontsize=7)
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.stage2``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR)
    parser.add_argument("--out", type=pathlib.Path, default=None, help="scratch directory (everything goes there)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run Stage 2 end to end.

    Args:
        argv (list[str] | None): command-line arguments (None = ``sys.argv``).

    Returns:
        int: exit status (0).
    """
    args = build_arg_parser().parse_args(argv)
    scratch = args.out is not None
    out_dir = args.out if scratch else args.derived / "stage2"
    tables_dir = out_dir if scratch else PAPER_DIR / "tables"
    figure_path = (out_dir if scratch else PAPER_DIR) / "figures_stage2.pdf"
    numbers_path = out_dir / "stage2_numbers.json" if scratch else args.derived / "stage2_numbers.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.log_level, out_dir / "stage2.log" if scratch else LOG_FILE)

    frame = build_frame(pd.read_parquet(args.derived / "panel.parquet"))
    frame.to_parquet(out_dir / "frame.parquet", index=False)
    log.info("frame              %d firm-years (fraud %d, issuer fraud %d); by fy %s", len(frame), frame.fraud_year.sum(),
             frame.issuer_fraud.sum(), frame.groupby("fy").fraud_year.sum().to_dict())
    metrics, curves, coefs, _ = run(frame)
    metrics.to_csv(out_dir / "metrics.csv", index=False)
    curves.to_csv(out_dir / "recall_curves.csv", index=False)
    coefs.to_csv(out_dir / "coefficients.csv", index=False)
    (tables_dir / "stage2_metrics.tex").write_text(table_metrics(metrics))
    (tables_dir / "stage2_coefficients.tex").write_text(table_coefficients(coefs))
    figures(curves, figure_path)
    numbers = {row.spec + " | " + row.model: {"auc": row.auc, "auprc": row.auprc, "base_rate": row.base_rate,
                                              "recall_402": row.recall_402, "random_402": row.random_402,
                                              "p_vs_random": row.p_vs_random, "random_402_p95": row.random_402_p95}
               for row in metrics.itertuples(index=False)}
    numbers_path.write_text(json.dumps(numbers, indent=2, default=float))
    log.info("wrote %s, %s, %s and the stage2_*.tex tables", out_dir, figure_path, numbers_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
