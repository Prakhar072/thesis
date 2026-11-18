"""Synthesis: the implied optimal penalty S* = (H / p) · e^{rτ} (runbook Week 9 Day 1; paper §6.4 and §2.4).

Each term has a source:

* **H, harm**: Stage 3's shareholder loss around interim orders that were a firm's first public action: (a) the median
  value lost, −CAR(0,+10) × the previous March's market cap; (b) the mean winsorised CAR(0,+10) × the median market cap
  of the same firms. Shareholder loss, not social harm.
* **τ, delay**: Stage 1's cohort median detection lag (and the truncation-adjusted median of all matters).
* **r, discount**: assumed, 8% and 12%.
* **p, detection probability**: not estimable from detected cases alone, so a **sensitivity grid**: 0.20, 0.33 (Dyck,
  Morse and Zingales's US estimate, imported, not an Indian finding) and 0.50.

The table sets S* beside the observed sanctions: the median of all monetary sanctions in the same firms' orders, and the
median penalty in the corpus.

Reads ``stage1_numbers.json``, ``stage3/events.parquet``, ``stage3/estimation.parquet``, ``stage3/cars.parquet``,
``panel``, ``orders``. Writes ``derived/synthesis_numbers.json``, ``paper/tables/synthesis.tex``, ``logs/synthesis.log``.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import pathlib

import numpy as np
import pandas as pd

from src import cache
from src.extract_text import setup_logging
from src.paper_tables import tabular

log = logging.getLogger("synthesis")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
PAPER_DIR = REPO_ROOT / "paper"
LOG_FILE = REPO_ROOT / "logs" / "synthesis.log"

DETECTION_P = (0.20, 0.33, 0.50)
DISCOUNT_R = (0.08, 0.12)
CRORE = 1e7
LAKH = 1e5


def implied_penalty(harm: float, p: float, r: float, tau: float) -> float:
    """The penalty that deters at detection probability p, paid τ years after the harm, discounted at r.

    Args:
        harm (float): the harm (₹).
        p (float): probability of detection.
        r (float): continuous discount rate per year.
        tau (float): years from the harm to the sanction.

    Returns:
        float: S* in ₹.
    """
    return harm / p * math.exp(r * tau)


def harm_terms(derived: pathlib.Path) -> dict:
    """H from Stage 3: interim first public actions with a market cap.

    Args:
        derived (pathlib.Path): directory with Stage 3's outputs and the panel.

    Returns:
        dict: ``n``, ``median_loss`` (₹), ``mean_car``, ``median_cap`` (₹), ``mean_car_x_median_cap`` (₹),
            ``median_sanction`` (₹, all sanctions in the firms' orders).
    """
    events = pd.read_parquet(derived / "stage3" / "events.parquet")
    status = pd.read_parquet(derived / "stage3" / "estimation.parquet").set_index("event_id").status
    cars = pd.read_parquet(derived / "stage3" / "cars.parquet")
    car = cars[cars.window == "(0,+10)"].set_index("event_id")
    car = car.car_w if "car_w" in car else car.car
    panel = pd.read_parquet(derived / "panel.parquet", columns=["firm_id", "fy", "market_cap"])
    caps = panel.dropna(subset=["market_cap"]).set_index(["firm_id", "fy"]).market_cap
    first = events[events.first_public & (events.event_type == "INTERIM")].copy()
    first = first[first.index.map(lambda i: status.get(i) == "ok")]
    fy = pd.to_datetime(first.event_date).dt.year + (pd.to_datetime(first.event_date).dt.month >= 4).astype(int)
    first["cap"] = [caps.get((firm, year - 1), np.nan) * CRORE for firm, year in zip(first.firm_id, fy)]
    first["car"] = car.reindex(first.index)
    with_cap = first.dropna(subset=["cap", "car"])
    loss = -with_cap.car * with_cap.cap
    return {"n": int(len(with_cap)), "median_loss": float(loss.median()), "mean_car": float(first.car.mean()),
            "median_cap": float(with_cap.cap.median()),
            "mean_car_x_median_cap": float(-first.car.mean() * with_cap.cap.median()),
            "median_sanction": float(with_cap.firm_penalty_total.median())}


def build(derived: pathlib.Path) -> tuple[pd.DataFrame, dict]:
    """The S* grid and its inputs.

    Args:
        derived (pathlib.Path): directory with the stage outputs.

    Returns:
        tuple[pd.DataFrame, dict]: rows (``harm``, ``tau_label``, ``p``, ``r``, ``s_star`` ₹) and the inputs.
    """
    stage1 = json.loads((derived / "stage1_numbers.json").read_text())
    harm = harm_terms(derived)
    orders = pd.read_parquet(derived / "orders.parquet", columns=["penalty_total", "penalty_outcome"])
    corpus_median = float(pd.to_numeric(orders.penalty_total, errors="coerce")[orders.penalty_outcome == "penalty"]
                          .median())
    taus = {"cohort median": stage1["median_t_cohort"], "truncation-adjusted median": stage1["trunc_median_adjusted_full"]}
    harms = {"median value lost": harm["median_loss"], "mean CAR × median cap": harm["mean_car_x_median_cap"]}
    rows = []
    for harm_label, value in harms.items():
        for tau_label, tau in taus.items():
            for p in DETECTION_P:
                for r in DISCOUNT_R:
                    rows.append({"harm": harm_label, "harm_value": value, "tau_label": tau_label, "tau": tau,
                                 "p": p, "r": r, "s_star": implied_penalty(value, p, r, tau)})
    inputs = {**harm, "corpus_median_penalty": corpus_median, "taus": taus}
    return pd.DataFrame(rows), inputs


def table(grid: pd.DataFrame, inputs: dict) -> str:
    """LaTeX: S* in ₹ crore, rows p, columns r, for each harm measure (cohort τ), with the observed sanctions.

    Args:
        grid (pd.DataFrame): from ``build``.
        inputs (dict): from ``build``.

    Returns:
        str: the table body.
    """
    rows: list[list[str] | str] = []
    part = grid[grid.tau_label == "cohort median"]
    for harm_label in part.harm.unique():
        value = part[part.harm == harm_label].harm_value.iloc[0]
        rows.append(f"$H$ = {harm_label.replace('×', '$\\times$')} (Rs {value / CRORE:.2f} crore), $\\tau$ = {part.tau.iloc[0]:.2f} years")
        for p in DETECTION_P:
            line = [f"$p$ = {p:.2f}" + (" (US benchmark)" if p == 0.33 else "")]
            for r in DISCOUNT_R:
                hit = part[(part.harm == harm_label) & (part.p == p) & (part.r == r)]
                s_star = hit.s_star.iloc[0]
                line.append(f"{s_star / CRORE:.1f}")
                line.append(f"{s_star / inputs['median_sanction']:.0f}$\\times$" if inputs["median_sanction"] else "--")
            rows.append(line)
    rows.append("Observed")
    rows.append([f"Median sanctions in the same firms' orders (n {inputs['n']})",
                 f"{inputs['median_sanction'] / CRORE:.2f}", "", "", ""])
    rows.append(["Median penalty, all penalty orders in the corpus",
                 f"{inputs['corpus_median_penalty'] / CRORE:.2f}", "", "", ""])
    return tabular("rrrr", ["", "$S^*$, $r$ = 8\\%", "vs observed", "$S^*$, $r$ = 12\\%", "vs observed"], rows)


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.synthesis``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write the synthesis table and numbers.

    Args:
        argv (list[str] | None): command-line arguments (None = ``sys.argv``).

    Returns:
        int: exit status (0).
    """
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.log_level, LOG_FILE)
    grid, inputs = build(args.derived)
    (PAPER_DIR / "tables" / "synthesis.tex").write_text(table(grid, inputs))
    (args.derived / "synthesis_numbers.json").write_text(
        json.dumps({"inputs": inputs, "grid": grid.to_dict(orient="records")}, indent=2, default=float))
    log.info("inputs %s", json.dumps(inputs, default=float))
    log.info("grid (₹ crore)\n%s", grid.assign(s_star=grid.s_star / CRORE).round(2).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
