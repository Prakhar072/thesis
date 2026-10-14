"""Week 3 Day 5: score the pipeline against the blind hand coding (runbook Week 3, Day 5).

Reads the user's coding (``hand/validation_sample.csv``), the sample key
(``derived/validation_key.parquet``: stratum and weight of each order) and the
pipeline's answers (``derived/orders.parquet``), and writes

* ``derived/validation_scored.parquet`` — one row per sampled order: hand and
  machine values side by side, with the match of each field;
* ``derived/validation_errors.csv`` — one row per field the two disagree on,
  with the machine's evidence (sentence, rule, method), for the fix step;
* ``derived/validation_summary.csv`` — every figure: measure, breakdown, group,
  n, matches, rate, weighted rate, 95% interval;
* ``derived/validation_table.md`` — the tables for the paper and the Gate 1 reading;
* ``logs/score_validation.log``.

What is measured
----------------
* **Window** (protocol §2). Whether the order has a window at all (hand
  ``window_kind`` vs a pipeline window); then *exact* (both ends the same day)
  and *within one month* (both ends within 31 days). Rates are given over the
  pipeline's windows (how good are the windows the analysis uses; a window the
  coder says does not exist counts as wrong), over the hand windows (how many
  true windows are recovered; a missed one counts as wrong), and over orders
  where both give one (date accuracy alone). An end the coder left open is
  compared on the stated end only. Months, years and financial years are stored
  by the protocol's convention on both sides (month end = its last day), so
  dates compare directly.
* **Fraud category, detection channel** (§3, §5): agreement and Cohen's κ. The
  channel also as "a channel is stated" vs UNCLEAR, and with the flagged
  SURVEILLANCE details counted as UNCLEAR on both sides (the H4 robustness).
* **Primary issuer** (§4): the names agree after the Week 4 normaliser, exactly
  or at token-sort ratio ≥ 92; ``NONE`` agrees with no issuer.
* Each by stratum, ``violation_method``, era of the order, order type, the
  auto-coders' method and confidence, and whether the coder marked a doubt.

Weights
-------
Each order carries its key ``weight`` (the stratum × ISO cell it was drawn
from), so weighted rates and κ estimate the corpus; unweighted counts stand
beside them. 95% intervals come from a bootstrap that resamples within cells.

Rules of use
------------
* Only a finished sheet is scored. ``--partial`` scores the rows coded so far;
  seeing where the pipeline goes wrong before the coding is done lets it shape
  the rest of the coding, so use it only to test the plumbing.
* The first scoring of the finished sheet is the validation. ``--official``
  saves its figures to ``hand/validation_official.json`` with fingerprints of
  the coding and of the pipeline's answers for the sample. Once the Day 5
  fixes change those answers, every later scoring is labelled as made on seen
  orders (optimistic): the runbook's "do not re-validate on the same 150".
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import math
import pathlib
import re

import numpy as np
import pandas as pd
from rapidfuzz import fuzz

from src import cache
from src import validation_sample as vs
from src.detect_channel import FLAGGED
from src.extract_text import assert_safe_output, setup_logging

log = logging.getLogger("score_validation")

REPO_ROOT = cache.REPO_ROOT
ORDERS_IN = REPO_ROOT / "derived" / "orders.parquet"
EXTRACTION_IN = REPO_ROOT / "derived" / "extraction_rate.csv"
DERIVED_DIR = REPO_ROOT / "derived"
OFFICIAL = REPO_ROOT / "hand" / "validation_official.json"
LOG_FILE = REPO_ROOT / "logs" / "score_validation.log"

MONTH_DAYS = 31        # "within one month": both ends within this many days
ISSUER_RATIO = 92      # token_sort_ratio for a name match (Week 4's auto-accept band)
REPS = 1000
BOOT_SEED = 20261016
ERA_BINS = [1999, 2005, 2010, 2015, 2020, 2026]      # as check_windows' year bands
ERA_LABELS = ["2000-05", "2006-10", "2011-15", "2016-20", "2021-26"]
KAPPA_MIN_N = 20       # κ by group only for groups at least this large
#: The machine answers fingerprinted for the official scoring.
MACHINE_COLS = ["m_start", "m_end", "m_kind", "m_cat", "m_ch", "m_detail", "m_issuer"]

#: (field, label, measure) for the headline table, in order.
HEADLINE = [
    ("Window", "Has a window: hand and pipeline agree", "win_present_agree"),
    ("Window", "Exact, of the pipeline's windows", "win_exact_of_machine"),
    ("Window", "Within one month, of the pipeline's windows", "win_month_of_machine"),
    ("Window", "Exact, where both give a window", "win_exact_of_both"),
    ("Window", "Within one month, where both give a window", "win_month_of_both"),
    ("Window", "Hand windows recovered exactly", "win_exact_of_hand"),
    ("Window", "Hand windows recovered within one month", "win_month_of_hand"),
    ("Window", "Kind agrees (stated period / conduct dates)", "win_kind_agree"),
    ("Fraud category", "Agreement", "cat_agree"),
    ("Fraud category", "Cohen's κ", "cat_kappa"),
    ("Detection channel", "Agreement", "ch_agree"),
    ("Detection channel", "Cohen's κ", "ch_kappa"),
    ("Detection channel", "κ, channel stated vs UNCLEAR", "ch_stated_kappa"),
    ("Detection channel", "κ, flagged SURVEILLANCE as UNCLEAR", "ch_flag_unclear_kappa"),
    ("Detection channel", "Detail agrees, where the channel does", "ch_detail_agree"),
    ("Primary issuer", "Agreement", "issuer_agree"),
]
#: Column labels for the breakdown tables.
LABELS = {
    "win_exact_of_machine": "Window exact", "win_month_of_machine": "Window within a month",
    "win_month_of_hand": "Hand window recovered (≤ 1 month)", "cat_agree": "Category agrees",
    "ch_agree": "Channel agrees", "ch_stated_agree": "Stated vs UNCLEAR agrees",
}
BY_LABELS = {"m_method": "Window method", "era": "Order year", "order_type": "Order type",
             "m_cat_conf": "Category confidence", "m_ch_method": "Channel method", "m_ch_conf": "Channel confidence"}
#: Agreement flags and the breakdowns each is reported by.
FLAGS = {
    "win_present_agree": ["stratum", "m_method", "era", "order_type", "doubt"],
    "win_exact_of_machine": ["stratum", "m_method", "m_kind", "era", "order_type", "doubt"],
    "win_month_of_machine": ["stratum", "m_method", "m_kind", "era", "order_type", "doubt"],
    "win_exact_of_both": ["m_method", "m_kind", "era"],
    "win_month_of_both": ["m_method", "m_kind", "era"],
    "win_exact_of_hand": ["stratum", "era", "order_type"],
    "win_month_of_hand": ["stratum", "era", "order_type"],
    "win_kind_agree": ["m_method", "era"],
    "cat_agree": ["era", "order_type", "m_cat_conf", "m_cat_rule", "stratum", "doubt"],
    "ch_agree": ["era", "order_type", "m_ch_method", "m_ch_conf", "stratum", "doubt"],
    "ch_stated_agree": ["era", "order_type", "m_ch_method"],
    "ch_detail_agree": ["h_ch"],
    "issuer_agree": ["era", "order_type", "m_issuer_source", "doubt"],
}
#: κ measures: (hand column, machine column), reported overall and by these breakdowns.
KAPPAS = {
    "cat_kappa": ("h_cat", "m_cat"),
    "ch_kappa": ("h_ch", "m_ch"),
    "ch_stated_kappa": ("h_ch_stated", "m_ch_stated"),
    "ch_flag_unclear_kappa": ("h_ch_flag", "m_ch_flag"),
}
KAPPA_BY = ["era", "order_type"]


# --- names ------------------------------------------------------------------------------------------------------

_ALIAS = re.compile(r"\((?:formerly|now|earlier|erstwhile|previously)(?:\s+known\s+as)?\s*:?\s*([^)]*)\)", re.I)


def norm(name: str) -> str:
    """The Week 4 normaliser (timeline Week 4, Day 1), applied to both sides."""
    s = name.upper()
    s = re.sub(r"[^A-Z0-9& ]", " ", s)
    s = s.replace("&", " AND ")
    s = re.sub(r"\b(LIMITED|LTD|PVT|PRIVATE|M S|COMPANY|CO|THE|INDIA)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def names(value) -> list[str]:
    """A name and any former or later name it gives in brackets, normalised."""
    if not isinstance(value, str) or not value.strip():
        return []
    raw = [_ALIAS.sub(" ", value), *_ALIAS.findall(value)]
    return [n for n in map(norm, raw) if n]


def issuer_match(hand: str, machine) -> bool:
    if hand.strip().upper() == "NONE":
        return not names(machine)
    return any(a == b or fuzz.token_sort_ratio(a, b) >= ISSUER_RATIO
               for a in names(hand) for b in names(machine))


# --- one row per order ------------------------------------------------------------------------------------------

def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip()) \
        or v is pd.NA or v is pd.NaT


def window_fields(h_kind: str, h_start, h_end, m_start, m_end) -> dict:
    """Presence class and match flags for one order. ``None`` = not in that measure's denominator."""
    out = dict.fromkeys(["presence", "d_start", "d_end", "win_exact", "win_month"])
    if not h_kind:
        return out
    m_has = not _blank(m_start)
    if h_kind == "none":
        out["presence"] = "spurious" if m_has else "neither"
    else:
        out["presence"] = "both" if m_has else "missed"
    if out["presence"] in ("spurious", "missed"):
        out["win_exact"] = out["win_month"] = False
    elif out["presence"] == "both" and (h_start or h_end):
        ds = []
        if h_start:
            out["d_start"] = (m_start - h_start).days
            ds.append(out["d_start"])
        if h_end and not _blank(m_end):
            out["d_end"] = (m_end - h_end).days
            ds.append(out["d_end"])
        out["win_exact"] = all(d == 0 for d in ds)
        out["win_month"] = all(abs(d) <= MONTH_DAYS for d in ds)
    return out


def _flagged_unclear(ch, detail):
    if _blank(ch):
        return None
    return "UNCLEAR" if ch == "SURVEILLANCE" and detail in FLAGGED else ch


def score_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Hand and machine values side by side, with every agreement flag (None = not scored)."""
    rows = []
    for r in frame.to_dict("records"):
        h_kind = r["h_window_kind"] or None
        h_start, h_end = vs._date(r["h_violation_start"]), vs._date(r["h_violation_end"])
        w = window_fields(h_kind, h_start, h_end, r["m_start"], r["m_end"])
        dated = bool(h_start or h_end)
        m_has = not _blank(r["m_start"])
        p = w["presence"]
        h_cat, h_ch, h_detail = r["h_fraud_category"] or None, r["h_detection_channel"] or None, \
            r["h_detection_detail"] or None
        h_issuer = r["h_primary_issuer"] or None
        ch_agree = None if h_ch is None or _blank(r["m_ch"]) else h_ch == r["m_ch"]
        rows.append({
            **{k: r[k] for k in ("sample_no", "text_stem", "order_id", "order_date", "order_type", "stratum",
                                 "iso", "weight")},
            "era": r["era"], "doubt": r["h_notes"].startswith("?"), "notes": r["h_notes"],
            "h_kind": h_kind, "h_start": h_start, "h_end": h_end,
            "m_start": r["m_start"], "m_end": r["m_end"], "m_kind": r["m_kind"] if m_has else None,
            "m_method": r["m_method"] if m_has else "none",
            "presence": p, "d_start": w["d_start"], "d_end": w["d_end"],
            "win_present_agree": None if p is None else p in ("both", "neither"),
            "win_exact_of_machine": w["win_exact"] if m_has and p is not None and (p != "both" or dated) else None,
            "win_month_of_machine": w["win_month"] if m_has and p is not None and (p != "both" or dated) else None,
            "win_exact_of_hand": w["win_exact"] if p in ("both", "missed") and dated else None,
            "win_month_of_hand": w["win_month"] if p in ("both", "missed") and dated else None,
            "win_exact_of_both": w["win_exact"] if p == "both" and dated else None,
            "win_month_of_both": w["win_month"] if p == "both" and dated else None,
            "win_kind_agree": h_kind == r["m_kind"] if p == "both" else None,
            "h_cat": h_cat, "m_cat": r["m_cat"], "m_cat_conf": r["m_cat_conf"],
            "m_cat_rule": (r["m_cat_rule"] or "").split(":")[0] or None,
            "cat_agree": None if h_cat is None or _blank(r["m_cat"]) else h_cat == r["m_cat"],
            "h_ch": h_ch, "h_detail": h_detail, "m_ch": r["m_ch"], "m_detail": r["m_detail"],
            "m_ch_method": r["m_ch_method"], "m_ch_conf": r["m_ch_conf"],
            "ch_agree": ch_agree,
            "h_ch_stated": None if h_ch is None else h_ch != "UNCLEAR",
            "m_ch_stated": None if _blank(r["m_ch"]) else r["m_ch"] != "UNCLEAR",
            "h_ch_flag": _flagged_unclear(h_ch, h_detail), "m_ch_flag": _flagged_unclear(r["m_ch"], r["m_detail"]),
            "ch_detail_agree": h_detail == r["m_detail"]
            if ch_agree and h_detail and r["m_detail"] not in (None, "model") else None,
            "h_issuer": h_issuer, "m_issuer": r["m_issuer"], "m_issuer_source": r["m_issuer_source"],
            "issuer_agree": None if h_issuer is None else issuer_match(h_issuer, r["m_issuer"]),
        })
    out = pd.DataFrame(rows)
    out["ch_stated_agree"] = [None if a is None or b is None else a == b
                              for a, b in zip(out.h_ch_stated, out.m_ch_stated)]
    for col in [*FLAGS, "h_ch_stated", "m_ch_stated"]:
        out[col] = out[col].astype("boolean")
    return out


# --- rates, κ, intervals ----------------------------------------------------------------------------------------

def _values(s: pd.Series) -> np.ndarray:
    return s.astype("Float64").to_numpy(dtype=float, na_value=np.nan)


def wrate(v: np.ndarray, w: np.ndarray) -> float:
    m = ~np.isnan(v)
    return float((v[m] * w[m]).sum() / w[m].sum()) if m.any() else math.nan


def codes(h: pd.Series, m: pd.Series) -> tuple[np.ndarray, np.ndarray, list]:
    labels = sorted({str(x) for x in pd.concat([h, m]).dropna()})
    at = {l: i for i, l in enumerate(labels)}
    enc = lambda s: np.array([at[str(x)] if not _blank(x) else -1 for x in s], dtype=int)  # noqa: E731
    return enc(h), enc(m), labels


def wkappa(h: np.ndarray, m: np.ndarray, w: np.ndarray) -> float:
    """Cohen's κ from (weighted) proportions; codes -1 = not coded."""
    ok = (h >= 0) & (m >= 0)
    h, m, w = h[ok], m[ok], w[ok]
    if not len(h):
        return math.nan
    k = int(max(h.max(), m.max())) + 1
    tot = w.sum()
    po = w[h == m].sum() / tot
    pe = float((np.bincount(h, weights=w, minlength=k) / tot) @ (np.bincount(m, weights=w, minlength=k) / tot))
    return math.nan if pe >= 1 else float((po - pe) / (1 - pe))


def boot_ci(stat, cells: list[np.ndarray], reps: int, seed: int) -> tuple[float, float]:
    """95% percentile interval of ``stat(idx)`` over resamples drawn within each cell."""
    if reps <= 0:
        return math.nan, math.nan
    rng = np.random.default_rng(seed)
    vals = np.array([stat(np.concatenate([rng.choice(c, size=len(c)) for c in cells])) for _ in range(reps)])
    vals = vals[~np.isnan(vals)]
    return (float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))) if len(vals) else (math.nan,) * 2


def summarise(scored: pd.DataFrame, reps: int = REPS, seed: int = BOOT_SEED) -> pd.DataFrame:
    """Every figure: rates and κ overall (with intervals) and by each breakdown."""
    scored = scored.reset_index(drop=True)
    w = scored.weight.to_numpy(dtype=float)
    cells = list(scored.groupby(["stratum", "iso"]).indices.values())
    rows = []
    for flag, bys in FLAGS.items():
        v = _values(scored[flag])
        ci = boot_ci(lambda i: wrate(v[i], w[i]), cells, reps, seed)
        rows.append(_rate_row(flag, "all", "all", v, w, ci))
        for by in bys:
            for g, idx in scored.groupby(by, dropna=False).indices.items():
                if (~np.isnan(v[idx])).any():
                    rows.append(_rate_row(flag, by, g, v[idx], w[idx]))
    for name, (hc, mc) in KAPPAS.items():
        h, m, _ = codes(scored[hc], scored[mc])
        ci = boot_ci(lambda i: wkappa(h[i], m[i], w[i]), cells, reps, seed)
        rows.append(_kappa_row(name, "all", "all", h, m, w, ci))
        for by in KAPPA_BY:
            for g, idx in scored.groupby(by, dropna=False).indices.items():
                if ((h[idx] >= 0) & (m[idx] >= 0)).sum() >= KAPPA_MIN_N:
                    rows.append(_kappa_row(name, by, g, h[idx], m[idx], w[idx]))
    return pd.DataFrame(rows)


def _group(g) -> str:
    return "(none)" if _blank(g) else str(g)


def _rate_row(measure, by, group, v, w, ci=(math.nan, math.nan)) -> dict:
    m = ~np.isnan(v)
    n, k = int(m.sum()), int(v[m].sum())
    return {"measure": measure, "by": by, "group": _group(group), "n": n, "matches": k,
            "rate": k / n if n else math.nan, "weighted": wrate(v, w), "ci_low": ci[0], "ci_high": ci[1]}


def _kappa_row(measure, by, group, h, m, w, ci=(math.nan, math.nan)) -> dict:
    ok = (h >= 0) & (m >= 0)
    return {"measure": measure, "by": by, "group": _group(group), "n": int(ok.sum()),
            "matches": int((h[ok] == m[ok]).sum()), "rate": wkappa(h, m, np.ones(len(h))),
            "weighted": wkappa(h, m, w), "ci_low": ci[0], "ci_high": ci[1]}


def confusion(scored: pd.DataFrame, hc: str, mc: str) -> pd.DataFrame:
    """Unweighted counts: hand (rows) × pipeline (columns)."""
    d = scored[scored[hc].notna() & scored[mc].notna()]
    return pd.crosstab(d[hc].rename("hand"), d[mc].rename("pipeline"), margins=True, margins_name="total")


def per_class(scored: pd.DataFrame, hc: str, mc: str) -> pd.DataFrame:
    d = scored[scored[hc].notna() & scored[mc].notna()]
    rows = []
    for c in sorted(set(d[hc]) | set(d[mc])):
        tp = int(((d[hc] == c) & (d[mc] == c)).sum())
        nh, nm = int((d[hc] == c).sum()), int((d[mc] == c).sum())
        rows.append({"class": c, "hand": nh, "pipeline": nm, "both": tp,
                     "recall": tp / nh if nh else math.nan, "precision": tp / nm if nm else math.nan})
    return pd.DataFrame(rows)


# --- errors -----------------------------------------------------------------------------------------------------

def errors(scored: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    """One row per disagreement, with the machine's evidence (to find systematic errors)."""
    ev = frame.set_index("text_stem")
    rows = []

    def add(r, field, error, hand, machine, method, rule, conf, evidence, detail=""):
        rows.append({"sample_no": r.sample_no, "text_stem": r.text_stem, "order_id": r.order_id,
                     "order_date": r.order_date, "order_type": r.order_type, "era": r.era, "field": field,
                     "error": error, "hand": hand, "machine": machine, "detail": detail,
                     "machine_method": method, "machine_rule": rule, "machine_confidence": conf,
                     "machine_evidence": evidence, "hand_notes": r.notes})

    def no(x) -> bool:  # an agreement flag that is False (not NA)
        return not pd.isna(x) and not bool(x)

    for r in scored.itertuples():
        e = ev.loc[r.text_stem]
        win = (r.m_method, e.m_rule, e.m_conf, e.m_raw)
        h_win = "none" if r.h_kind == "none" else f"{r.h_start or '?'} .. {r.h_end or '?'}"
        m_win = "none" if r.m_method == "none" else f"{r.m_start} .. {r.m_end}"
        if r.presence in ("missed", "spurious"):
            add(r, "window", r.presence, h_win, m_win, *win)
        elif r.presence == "both" and no(r.win_exact_of_both):
            off = ", ".join(f"{k} {int(d):+d} d" for k, d in (("start", r.d_start), ("end", r.d_end))
                            if not pd.isna(d) and d != 0)
            add(r, "window", "dates_within_month" if r.win_month_of_both else "dates", h_win, m_win, *win,
                f"pipeline − hand: {off}")
        if no(r.win_kind_agree):
            add(r, "window_kind", "kind", r.h_kind, r.m_kind, *win)
        if no(r.cat_agree):
            add(r, "fraud_category", f"{r.h_cat} → {r.m_cat}", r.h_cat, r.m_cat, "rule", e.m_cat_rule,
                r.m_cat_conf, e.m_cat_raw)
        if no(r.ch_agree):
            add(r, "detection_channel", f"{r.h_ch} → {r.m_ch}", r.h_ch, r.m_ch, r.m_ch_method, r.m_detail,
                r.m_ch_conf, e.m_ch_raw)
        if no(r.ch_detail_agree):
            add(r, "detection_detail", f"{r.h_detail} → {r.m_detail}", r.h_detail, r.m_detail, r.m_ch_method,
                r.m_detail, r.m_ch_conf, e.m_ch_raw)
        if no(r.issuer_agree):
            add(r, "primary_issuer", "issuer", r.h_issuer, r.m_issuer, "rule", r.m_issuer_source, None,
                e.m_matter)
    cols = ["sample_no", "text_stem", "order_id", "order_date", "order_type", "era", "field", "error", "hand",
            "machine", "detail", "machine_method", "machine_rule", "machine_confidence", "machine_evidence",
            "hand_notes"]
    return pd.DataFrame(rows, columns=cols)


# --- Gate 1 -----------------------------------------------------------------------------------------------------

def gate1(extraction: float, kappas: dict[str, float]) -> str:
    """The runbook's Gate 1 table (Week 3 weekend), read with the weaker κ."""
    if extraction < 0.50:
        return "Fall back: hand-code a stratified 300–400 and run Stage 1 on that"
    known = {k: v for k, v in kappas.items() if not math.isnan(v)}
    if not known:
        return "κ undefined (nothing coded, or one category on both sides): no decision"
    field, k = min(known.items(), key=lambda kv: kv[1])
    if k < 0.6:
        return f"Protocol too vague ({field} κ {k:.2f} < 0.6): rewrite the category rules, recode"
    if extraction > 0.70 and k > 0.7:
        return "Proceed as planned"
    return ("Proceed; report drop-out and test whether missing cases differ systematically"
            + (f" (κ {k:.2f} for {field} is between 0.6 and 0.7)" if extraction > 0.70 else ""))


def extraction_rate(path: pathlib.Path = EXTRACTION_IN) -> float:
    t = pd.read_csv(path)
    return float(t[(t["by"] == "all") & (t.group == "all")].share.iloc[0])


# --- inputs and fingerprints ------------------------------------------------------------------------------------

def load(sheet: pd.DataFrame, key: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    """Sheet ⋈ key ⋈ orders on text_stem, one to one; hand columns h_*, machine columns m_*."""
    h = sheet[["sample_no", "text_stem", *vs.CODING_COLUMNS]].rename(columns={c: f"h_{c}" for c in vs.CODING_COLUMNS})
    m = orders.rename(columns={
        "violation_start": "m_start", "violation_end": "m_end", "window_kind": "m_kind", "violation_method": "m_method",
        "violation_rule": "m_rule", "violation_confidence": "m_conf", "violation_raw": "m_raw",
        "fraud_category": "m_cat", "fraud_category_confidence": "m_cat_conf", "fraud_category_rule": "m_cat_rule",
        "fraud_category_raw": "m_cat_raw", "detection_channel": "m_ch", "detection_detail": "m_detail",
        "detection_method": "m_ch_method", "detection_confidence": "m_ch_conf", "detection_raw": "m_ch_raw",
        "issuer_guess": "m_issuer", "issuer_source": "m_issuer_source", "matter_raw": "m_matter"})
    m = m[["text_stem", "order_id", "order_date", "order_type", "m_start", "m_end", "m_kind", "m_method", "m_rule",
           "m_conf", "m_raw", "m_cat", "m_cat_conf", "m_cat_rule", "m_cat_raw", "m_ch", "m_detail", "m_ch_method",
           "m_ch_conf", "m_ch_raw", "m_issuer", "m_issuer_source", "m_matter"]]
    f = (h.merge(key[["text_stem", "stratum", "iso", "weight"]], on="text_stem", how="left", validate="one_to_one")
          .merge(m, on="text_stem", how="left", validate="one_to_one"))
    if len(f) != len(sheet) or f.weight.isna().any() or f.order_date.isna().any():
        raise ValueError("the sheet's orders do not all match the key and orders.parquet")
    f["sample_no"] = f.sample_no.astype(int)
    f["era"] = pd.cut(pd.to_datetime(f.order_date).dt.year, ERA_BINS, labels=ERA_LABELS).astype(str)
    return f


def fingerprint(df: pd.DataFrame, cols: list[str]) -> str:
    d = df.sort_values("text_stem")[["text_stem", *cols]].astype(str)
    return hashlib.sha256(d.to_csv(index=False).encode()).hexdigest()[:16]


def provenance(frame: pd.DataFrame, official: pathlib.Path) -> tuple[dict, str]:
    fp = {"coding": fingerprint(frame, [f"h_{c}" for c in vs.CODING_COLUMNS]),
          "pipeline": fingerprint(frame, MACHINE_COLS)}
    if not official.exists():
        return fp, "First scoring (no official scoring saved yet)."
    saved = json.loads(official.read_text())["fingerprints"]
    if saved == fp:
        return fp, "Same coding and pipeline answers as the official scoring."
    notes = []
    if saved["pipeline"] != fp["pipeline"]:
        notes.append("the pipeline's answers for the sample changed since the official scoring: these figures are "
                     "on orders already seen and overstate accuracy (validate fixes on a fresh sample)")
    if saved["coding"] != fp["coding"]:
        notes.append("the hand coding changed since the official scoring")
    return fp, "NOT THE VALIDATION: " + "; ".join(notes) + f". Official figures: {official.name}."


# --- report -----------------------------------------------------------------------------------------------------

def _pct(x) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"


def _k(x) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.2f}"


def _md(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def headline(summary: pd.DataFrame) -> pd.DataFrame:
    s = summary[summary["by"] == "all"].set_index("measure")
    rows = []
    for field, label, measure in HEADLINE:
        r = s.loc[measure]
        fmt = _k if measure.endswith("kappa") else _pct
        rows.append({"Field": field, "Measure": label, "n": r.n,
                     "Unweighted": fmt(r.rate) if measure.endswith("kappa") else f"{r.matches}/{r.n} ({_pct(r.rate)})",
                     "Weighted (corpus)": fmt(r.weighted), "95% CI": f"{fmt(r.ci_low)}–{fmt(r.ci_high)}"})
    return pd.DataFrame(rows)


def breakdown(summary: pd.DataFrame, measures: list[str], by: str) -> pd.DataFrame:
    s = summary[(summary["by"] == by) & summary.measure.isin(measures)]
    if s.empty:
        return s
    t = s.assign(cell=[f"{m}/{n} ({_pct(r)}; w {_pct(w)})" if not meas.endswith("kappa") else f"{_k(w)} (n {n})"
                       for meas, m, n, r, w in zip(s.measure, s.matches, s.n, s.rate, s.weighted)])
    t = t.pivot(index="group", columns="measure", values="cell").reindex(columns=measures).fillna("—")
    return t.rename(columns=LABELS).rename_axis(None, axis=1).reset_index().rename(columns={"group": BY_LABELS[by]})


def report(scored: pd.DataFrame, summary: pd.DataFrame, status: str, extraction: float, decision: str,
           partial: bool) -> str:
    n_cat = scored.h_cat.notna().sum()
    parts = [
        "# Week 3 validation: pipeline vs blind hand coding", "",
        f"{status}" + (" **PARTIAL: rows coded so far only.**" if partial else ""), "",
        f"{len(scored)} orders coded; weights from `derived/validation_key.parquet` (stratum × ISO cells) estimate "
        f"the corpus of 20,849 orders with text. Window \"within one month\" = both ends within {MONTH_DAYS} days. "
        "Intervals: bootstrap within cells. Cells read \"matches/n (unweighted; w weighted)\".", "",
        "## Headline", "", _md(headline(summary)), "",
        "## Window accuracy by extraction method (of the pipeline's windows)", "",
        _md(breakdown(summary, ["win_exact_of_machine", "win_month_of_machine"], "m_method")), "",
        "## By era of the order", "",
        _md(breakdown(summary, ["win_month_of_machine", "win_month_of_hand", "cat_agree", "ch_agree"], "era")), "",
        "## By order type", "",
        _md(breakdown(summary, ["win_month_of_machine", "win_month_of_hand", "cat_agree", "ch_agree"], "order_type")),
        "",
        "## Fraud category by auto-coder confidence", "",
        _md(breakdown(summary, ["cat_agree"], "m_cat_conf")), "",
        "## Detection channel by method and confidence", "",
        _md(breakdown(summary, ["ch_agree", "ch_stated_agree"], "m_ch_method")), "",
        _md(breakdown(summary, ["ch_agree"], "m_ch_conf")), "",
        "## Fraud category: hand (rows) × pipeline (columns), unweighted", "",
        _md(confusion(scored, "h_cat", "m_cat").reset_index()) if n_cat else "—", "",
        _md(per_class(scored, "h_cat", "m_cat").round(2)) if n_cat else "", "",
        "## Detection channel: hand (rows) × pipeline (columns), unweighted", "",
        _md(confusion(scored, "h_ch", "m_ch").reset_index()) if scored.h_ch.notna().any() else "—", "",
        _md(per_class(scored, "h_ch", "m_ch").round(2)) if scored.h_ch.notna().any() else "", "",
        "## Gate 1", "",
        f"Extraction rate {_pct(extraction)} (`derived/extraction_rate.csv`); fraud-category κ "
        f"{_k(_kappa(summary, 'cat_kappa'))}, detection-channel κ {_k(_kappa(summary, 'ch_kappa'))} (weighted).", "",
        f"**Reading of the runbook table:** {decision}.", "",
        "Record the decision in `results_log.md`.",
    ]
    return "\n".join(parts) + "\n"


def _kappa(summary: pd.DataFrame, measure: str) -> float:
    s = summary[(summary.measure == measure) & (summary["by"] == "all")]
    return float(s.weighted.iloc[0]) if len(s) else math.nan


# --- command line -----------------------------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.score_validation",
        description="Score the pipeline against the blind hand coding (Week 3 Day 5)",
    )
    parser.add_argument("--orders", type=pathlib.Path, default=ORDERS_IN)
    parser.add_argument("--extraction", type=pathlib.Path, default=EXTRACTION_IN)
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="read the sheet and key from DIR and write every output there (dry run)")
    parser.add_argument("--partial", action="store_true",
                        help="score the rows coded so far (plumbing only: the errors would anchor the rest)")
    parser.add_argument("--official", action="store_true",
                        help="save this scoring as the validation (once, on the finished sheet)")
    parser.add_argument("--force", action="store_true", help="with --official: replace a saved official scoring")
    parser.add_argument("--reps", type=int, default=REPS, help="bootstrap resamples (0 = no intervals)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.scratch:
        sheet_in, key_in = args.scratch / "validation_sample.csv", args.scratch / "validation_key.parquet"
        out_dir, official, log_file = args.scratch, args.scratch / "validation_official.json", None
    else:
        sheet_in, key_in, out_dir, official, log_file = vs.SHEET_OUT, vs.KEY_OUT, DERIVED_DIR, OFFICIAL, LOG_FILE
    outs = {k: out_dir / f"validation_{k}" for k in ("scored.parquet", "errors.csv", "summary.csv", "table.md")}
    for p in [*outs.values(), official]:
        assert_safe_output(p)
    setup_logging(args.log_level, log_file)

    for p in (sheet_in, key_in, args.orders, args.extraction):
        if not p.exists():
            log.error("missing input %s", p)
            return 1
    key = pd.read_parquet(key_in)
    problems, stats = vs.check(sheet_in, set(key.text_stem))
    bad = [p for p in problems if p[1] == "error"]
    if bad:
        for no, _, msg in bad:
            log.error("sheet #%s: %s", no, msg)
        log.error("fix these first (python -m src.validation_sample --check)")
        return 1
    if stats["started"] == 0:
        log.error("nothing coded yet in %s", sheet_in)
        return 1
    if stats["started"] < stats["rows"] and not args.partial:
        log.error("%d of %d orders coded; score a finished sheet, or pass --partial to test the plumbing "
                  "(seeing the pipeline's errors before you finish would anchor the rest)",
                  stats["started"], stats["rows"])
        return 1
    if args.official and args.partial:
        log.error("--official needs the finished sheet, not --partial")
        return 1
    if args.official and official.exists() and not args.force:
        log.error("%s exists: the validation was already scored (pass --force only to correct a scoring error)",
                  official)
        return 1

    sheet, _ = vs.read_sheet(sheet_in)
    sheet = sheet[[any(r[c] for c in vs.CODING_COLUMNS) for r in sheet.to_dict("records")]]
    frame = load(sheet, key, pd.read_parquet(args.orders))
    fp, status = provenance(frame, official)
    scored = score_rows(frame)
    summary = summarise(scored, reps=args.reps)
    kappas = {"fraud category": _kappa(summary, "cat_kappa"), "detection channel": _kappa(summary, "ch_kappa")}
    extraction = extraction_rate(args.extraction)
    decision = gate1(extraction, kappas)

    scored.to_parquet(outs["scored.parquet"], index=False)
    errors(scored, frame).to_csv(outs["errors.csv"], index=False)
    summary.to_csv(outs["summary.csv"], index=False)
    outs["table.md"].write_text(report(scored, summary, status, extraction, decision, args.partial))
    if args.official:
        top = summary[summary["by"] == "all"].set_index("measure")
        official.write_text(json.dumps({
            "scored_at": dt.datetime.now().isoformat(timespec="seconds"), "orders": len(scored),
            "fingerprints": fp, "extraction_rate": extraction, "gate1": decision,
            "headline": {m: {c: (None if pd.isna(v) else (round(float(v), 4) if isinstance(v, float) else int(v)))
                             for c, v in top.loc[m, ["n", "matches", "rate", "weighted", "ci_low", "ci_high"]].items()}
                         for _, _, m in HEADLINE},
        }, indent=2) + "\n")
        log.info("saved the official scoring to %s", official)

    log.info(status)
    for row in headline(summary).itertuples(index=False):
        log.info("%-18s %-46s n=%-4s %-18s weighted %-7s CI %s", row.Field, row.Measure, row.n, row.Unweighted,
                 row[4], row[5])
    log.info("Gate 1: extraction %.1f%%; %s", 100 * extraction, decision)
    log.info("wrote %s", ", ".join(str(p) for p in outs.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
