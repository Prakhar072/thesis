"""Week 3 validation sample: 150 orders to hand-code blind (runbook Week 3, Days 3–4).

Reads ``derived/violation_windows.parquet`` (after Day 2), the listings and the
document table, and writes

* ``hand/validation_sample.csv`` — the coding sheet: order identity, SEBI's own
  title and date, links to the order, and empty columns to fill in. **No
  pipeline output is shown** (runbook: "if you can see the machine's answer you
  will anchor on it and your validation will overstate accuracy").
* ``derived/validation_key.parquet`` — which stratum each sampled order came
  from and how many orders that stratum holds, to weight accuracy back to the
  corpus. Kept out of ``hand/`` so the coder does not see it.

Why stratify on the pipeline's outcome
--------------------------------------
A simple random 150 would hold about 12 model windows (8% of orders) — too
few to measure the model — and about 17 identical ISO orders. So the sample is
drawn per stratum: regex windows, model windows of each ``window_kind``, and
orders with no window (to measure what the pipeline misses). Within a stratum
the pool is ordered by order type and date and drawn systematically, which
spreads the sample over categories and years in proportion (the runbook's
"stratified by year and order category"). Overall accuracy is then
``sum(stratum_share * stratum_accuracy)``.

The 90 orders labelled while choosing the model (``hand/window_model_devset.csv``
and ``hand/window_model_holdout.csv``) were seen by the pipeline's author and are
excluded.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import random

import pandas as pd

from src import cache
from src.extract_text import assert_safe_output, setup_logging, text_stem

log = logging.getLogger("validation_sample")

REPO_ROOT = cache.REPO_ROOT
WINDOWS_IN = REPO_ROOT / "derived" / "violation_windows.parquet"
LISTINGS_IN = REPO_ROOT / "derived" / "listings.parquet"
DOCUMENTS_IN = REPO_ROOT / "derived" / "order_documents.parquet"
SEEN = (REPO_ROOT / "hand" / "window_model_devset.csv", REPO_ROOT / "hand" / "window_model_holdout.csv")
SHEET_OUT = REPO_ROOT / "hand" / "validation_sample.csv"
KEY_OUT = REPO_ROOT / "derived" / "validation_key.parquet"

REGEX, MODEL_STATED, MODEL_CONDUCT, NO_WINDOW = "regex", "model_stated", "model_conduct", "no_window"
SIZES = {REGEX: 60, MODEL_STATED: 25, MODEL_CONDUCT: 25, NO_WINDOW: 40}
#: The Illiquid Stock Options scheme is ~2,400 near-identical orders sharing one
#: window; a few are enough to check it (at most this many per stratum).
ISO_CAP = {REGEX: 3, MODEL_STATED: 1, MODEL_CONDUCT: 1, NO_WINDOW: 1}
SEED = 20261006

#: Columns the coder fills in (runbook Week 3: window, fraud category,
#: detection channel, primary issuer). window_kind: stated_period /
#: conduct_dates / none.
CODING_COLUMNS = ["violation_start", "violation_end", "window_kind", "fraud_category",
                  "detection_channel", "primary_issuer", "notes"]


def stratum(row) -> str | None:
    if row.text_status != "OK":
        return None
    if pd.isna(row.violation_start):
        return NO_WINDOW
    if row.violation_method == "regex":
        return REGEX
    return MODEL_STATED if row.window_kind == "stated_period" else MODEL_CONDUCT


def load_frame(windows: pathlib.Path = WINDOWS_IN, listings: pathlib.Path = LISTINGS_IN,
               documents: pathlib.Path = DOCUMENTS_IN) -> pd.DataFrame:
    """One row per order that could be sampled, with its stratum and links."""
    w = pd.read_parquet(windows)
    lst = pd.read_parquet(listings, dtype_backend="numpy_nullable")
    lst["text_stem"] = [text_stem(int(o) if pd.notna(o) else None, u)
                        for o, u in zip(lst.order_id, lst.source_url)]
    docs = pd.read_parquet(documents, dtype_backend="numpy_nullable")
    docs["text_stem"] = [text_stem(int(o) if pd.notna(o) else None, u)
                         for o, u in zip(docs.order_id, docs.source_url)]
    frame = (w.merge(lst[["text_stem", "title", "source_url"]], on="text_stem", validate="one_to_one")
              .merge(docs[["text_stem", "pdf_url", "pdf_path"]], on="text_stem", how="left",
                     validate="one_to_one"))
    frame["stratum"] = [stratum(r) for r in frame.itertuples()]
    frame["iso"] = (frame.title.str.contains("illiquid stock option", case=False, na=False)
                    | frame.violation_raw.str.contains("illiquid stock option", case=False, na=False))
    return frame[frame.stratum.notna()].reset_index(drop=True)


def seen_stems(paths=SEEN) -> set[str]:
    out: set[str] = set()
    for p in paths:
        if p.exists():
            out |= set(pd.read_csv(p, dtype=str).text_stem)
    return out


def systematic(pool: pd.DataFrame, n: int, rng: random.Random) -> pd.DataFrame:
    """Every k-th order of the pool sorted by type and date, from a random start."""
    if n <= 0 or pool.empty:
        return pool.iloc[0:0]
    if n >= len(pool):
        return pool
    pool = pool.sort_values(["order_type", "order_date", "text_stem"]).reset_index(drop=True)
    k = len(pool) / n
    start = rng.uniform(0, k)
    return pool.iloc[[int(start + i * k) for i in range(n)]]


def draw(frame: pd.DataFrame, *, sizes: dict[str, int] = SIZES, exclude: set[str] = frozenset(),
         iso_cap: dict[str, int] = ISO_CAP, seed: int = SEED) -> pd.DataFrame:
    rng = random.Random(seed)
    frame = frame[~frame.text_stem.isin(exclude)]
    parts = []
    for name, n in sizes.items():
        pool = frame[frame.stratum == name]
        iso = pool[pool.iso]
        k_iso = min(iso_cap.get(name, 0), len(iso), n)
        picked = [systematic(pool[~pool.iso], n - k_iso, rng)]
        if k_iso:
            picked.append(iso.sample(k_iso, random_state=rng.randrange(2**32)))
        parts.append(pd.concat(picked))
    sample = pd.concat(parts)
    # The coder works in a shuffled order, so strata are not visible from position.
    return sample.sample(frac=1, random_state=rng.randrange(2**32)).reset_index(drop=True)


def sheet(sample: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({
        "sample_no": range(1, len(sample) + 1),
        "text_stem": sample.text_stem.values,
        "order_id": pd.array(sample.order_id, dtype="Int64"),
        "order_date": sample.order_date.values,
        "order_type": sample.order_type.values,
        "title": sample.title.values,
        "order_page": sample.source_url.values,
        "pdf_url": sample.pdf_url.values,
        "local_pdf": sample.pdf_path.values,
    })
    for col in CODING_COLUMNS:
        out[col] = ""
    return out


def key(sample: pd.DataFrame, frame: pd.DataFrame, exclude: set[str], seed: int) -> pd.DataFrame:
    """Stratum and weights for each sampled order (population = all orders of
    the stratum with text, dev/held-out orders included, since accuracy is
    weighted back to the whole corpus)."""
    population = frame.groupby("stratum").size()
    drawn = sample.groupby("stratum").size()
    k = sample[["text_stem", "stratum", "iso"]].copy()
    k["stratum_population"] = k.stratum.map(population).astype("int64")
    k["stratum_sampled"] = k.stratum.map(drawn).astype("int64")
    k["weight"] = k.stratum_population / k.stratum_sampled
    k["seed"] = seed
    k["excluded_seen"] = len(exclude)
    return k


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.validation_sample",
        description="Draw the Week 3 validation sample and write the blind coding sheet",
    )
    parser.add_argument("--windows", type=pathlib.Path, default=WINDOWS_IN)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: write the sheet and key under DIR instead of hand/ and derived/")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing coding sheet (it may hold your coding!)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.scratch:
        sheet_out, key_out = args.scratch / "validation_sample.csv", args.scratch / "validation_key.parquet"
    else:
        sheet_out, key_out = SHEET_OUT, KEY_OUT
    for p in (sheet_out, key_out):
        assert_safe_output(p)
    setup_logging(args.log_level, None)
    if sheet_out.exists() and not args.force:
        log.error("%s already exists and may hold coding; pass --force to replace it", sheet_out)
        return 1

    frame = load_frame(args.windows)
    exclude = seen_stems()
    sample = draw(frame, exclude=exclude, seed=args.seed)
    sheet_out.parent.mkdir(parents=True, exist_ok=True)
    key_out.parent.mkdir(parents=True, exist_ok=True)
    sheet(sample).to_csv(sheet_out, index=False)
    key(sample, frame, exclude, args.seed).to_parquet(key_out, index=False)

    log.info("population by stratum: %s", frame.groupby("stratum").size().to_dict())
    log.info("sampled by stratum:    %s", sample.groupby("stratum").size().to_dict())
    log.info("sampled by type:       %s", sample.groupby("order_type").size().to_dict())
    log.info("ISO orders sampled: %d; excluded (seen while tuning): %d", int(sample.iso.sum()), len(exclude))
    log.info("wrote %s (coding sheet) and %s (key — do not open while coding)", sheet_out, key_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
