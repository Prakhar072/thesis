"""Week 2 checkpoint: ``derived/orders.parquet``, one row per order.

"``derived/orders.parquet`` populated with violation windows, confidences, raw
sentences, prior action dates, penalties and regulations" (runbook, Week 2
checkpoint; the ``orders`` table of the runbook schema). This stage only joins
what earlier stages wrote — it reads no order text:

=========================  ==========================================================
listings                   title, category, source URL, SAT appeal annotations
order_documents            PDF URL, document shape
text_documents             text path, text layer, pages, OCR share
windows_checked            the violation window (Day 1–3), its confidence and method
order_actions              prior action and first action dates, sanctions (Day 4)
order_parties              matter, issuer guess, parties by role, regulations (Day 5)
order_categories           fraud category, its confidence, rule and evidence (Week 3)
order_channels             detection channel, detail, confidence, method, sentence (Week 3)
=========================  ==========================================================

Every join is on ``text_stem`` and one-to-one (never on ``order_id``: pandas
matches the 76 null ids to each other), and the row count is asserted after
each join. Party-level facts (who was penalised, how much each) stay in
``derived/order_entities.parquet`` — the runbook keeps the two tables apart so
penalties are never double-counted.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import time

import pandas as pd

from src import cache
from src.extract_text import assert_safe_output, setup_logging, text_stem

log = logging.getLogger("build_orders")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
LOGS_DIR = REPO_ROOT / "logs"

LISTING_COLUMNS = ["text_stem", "order_id", "order_date", "order_type", "listing_category", "title", "source_url",
                   "sat_appeals", "corrigendum_url"]
DOCUMENT_COLUMNS = ["text_stem", "pdf_url", "doc_shape"]
TEXT_COLUMNS = ["text_stem", "status", "text_path", "has_text_layer", "n_pages", "n_ocr_done", "mean_ocr_conf"]
WINDOW_COLUMNS = ["text_stem", "window_status", "violation_start", "violation_end", "start_precision", "end_precision",
                  "violation_raw", "violation_confidence", "violation_method", "violation_rule", "window_kind",
                  "sanity_status", "sanity_flags"]
ACTION_COLUMNS = ["text_stem", "prior_action_date", "prior_action_kind", "prior_action_raw", "earliest_scn_date",
                  "earliest_interim_date", "ao_appointment_date", "first_action_date", "first_action_source",
                  "penalty_total", "penalty_total_basis", "penalty_n", "penalty_max", "penalty_outcome", "penalty_raw",
                  "settlement_amount", "disgorgement_total", "debarred", "debarment_months_max",
                  "debarment_indefinite", "registration_action", "warned"]
CATEGORY_COLUMNS = ["text_stem", "fraud_category", "fraud_category_confidence", "fraud_category_rule",
                    "fraud_category_raw"]
CHANNEL_COLUMNS = ["text_stem", "detection_channel", "detection_detail", "detection_confidence", "detection_method",
                   "detection_raw"]
PARTY_COLUMNS = ["text_stem", "matter_raw", "issuer_guess", "issuer_source", "n_parties", "n_parties_by_role",
                 "parties_source", "penalty_link_status", "penalty_total_resolved", "regulations", "regimes",
                 "regimes_cited", "penalty_provisions"]


def _read(path: pathlib.Path, columns: list[str] | None = None) -> pd.DataFrame:
    """Read one stage's table (nullable dtypes, so ids stay integers).

    Args:
        path (pathlib.Path): the Parquet file.
        columns (list[str] | None): the columns to read (None = all).
    Returns:
        pd.DataFrame: the table.
    Raises:
        FileNotFoundError: the stage that writes it has not been run.
    """
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — run the stage that writes it first")
    return pd.read_parquet(path, columns=columns, dtype_backend="numpy_nullable")


def _with_stem(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``text_stem`` (the join key) to a table that has order_id and source_url.

    Args:
        df (pd.DataFrame): listings or order_documents.
    Returns:
        pd.DataFrame: a copy with a ``text_stem`` column.
    """
    df = df.copy()
    df["text_stem"] = [text_stem(int(o) if pd.notna(o) else None, u) for o, u in zip(df["order_id"], df["source_url"])]
    return df


def build(derived: pathlib.Path = DERIVED_DIR, parties: pathlib.Path | None = None,
          categories: pathlib.Path | None = None, channels: pathlib.Path | None = None) -> pd.DataFrame:
    """Join every stage's per-order table onto the listings, one to one, asserting the row count each time.

    Args:
        derived (pathlib.Path): where the inputs are.
        parties (pathlib.Path | None): another order_parties.parquet (scratch runs).
        categories (pathlib.Path | None): another order_categories.parquet.
        channels (pathlib.Path | None): another order_channels.parquet.
    Returns:
        pd.DataFrame: one row per listed order (20,861 × 71), sorted by date and stem.
    Raises:
        ValueError: an input has orders that are not in the listings.
    """
    listings = _with_stem(_read(derived / "listings.parquet"))[LISTING_COLUMNS]
    n = len(listings)
    assert listings["text_stem"].is_unique, "duplicate text_stem in listings"
    docs = _with_stem(_read(derived / "order_documents.parquet"))[DOCUMENT_COLUMNS]
    text = _read(derived / "text_documents.parquet", TEXT_COLUMNS).rename(columns={"status": "text_status"})
    windows = _read(derived / "windows_checked.parquet", WINDOW_COLUMNS)
    actions = _read(derived / "order_actions.parquet", ACTION_COLUMNS)
    party = _read(parties or derived / "order_parties.parquet", PARTY_COLUMNS)
    category = _read(categories or derived / "order_categories.parquet", CATEGORY_COLUMNS)
    channel = _read(channels or derived / "order_channels.parquet", CHANNEL_COLUMNS)

    out = listings
    for name, df in (("order_documents", docs), ("text_documents", text), ("windows_checked", windows),
                     ("order_actions", actions), ("order_parties", party), ("order_categories", category),
                     ("order_channels", channel)):
        if not df["text_stem"].isin(listings["text_stem"]).all():
            raise ValueError(f"{name} has orders not in the listings")
        out = out.merge(df, on="text_stem", how="left", validate="one_to_one")
        assert len(out) == n, f"join with {name} changed the row count: {n} -> {len(out)}"
        missing = int((~listings["text_stem"].isin(df["text_stem"])).sum())
        log.info("joined %-16s %6d rows (%d listed orders without a row there)", name, len(df), missing)
    return out.sort_values(["order_date", "text_stem"], na_position="last").reset_index(drop=True)


def summarise(orders: pd.DataFrame) -> dict:
    """Coverage summary for the log.

    Args:
        orders (pd.DataFrame): the built table.
    Returns:
        dict: shares of orders with a window, prior action, penalty, parties, regulations, issuer;
        fraud categories, channels and top regimes.
    """
    with_text = orders[orders["text_status"] == "OK"]
    n = len(with_text)

    def share(mask) -> str:
        """Count and percentage of orders with text where a condition holds.

        Args:
            mask (pd.Series): bool per order with text.
        Returns:
            str: e.g. "12142 (58.2%)".
        """
        k = int(mask.sum())
        return f"{k} ({k / n:.1%})" if n else "0"

    return {
        "orders": len(orders),
        "orders_with_text": n,
        "violation_window": share(with_text["violation_start"].notna()),
        "prior_action_date": share(with_text["prior_action_date"].notna()),
        "penalty_total": share(with_text["penalty_total"].notna()),
        "parties_found": share(with_text["n_parties"] > 0),
        "regulations_violated": share(with_text["regulations"].fillna("[]") != "[]"),
        "issuer_guess": share(with_text["issuer_guess"].notna()),
        "window_and_prior_action": share(with_text["violation_start"].notna() & with_text["prior_action_date"].notna()),
        "fraud_category": {k: int(v) for k, v in with_text["fraud_category"].value_counts().items()},
        "detection_channel": {k: int(v) for k, v in with_text["detection_channel"].value_counts().items()},
        "top_regimes": {k: int(v) for k, v in pd.Series([r for x in with_text["regimes"].dropna()
                                                         for r in json.loads(x)]).value_counts().head(8).items()},
    }


def write(orders: pd.DataFrame, path: pathlib.Path) -> None:
    """Write ``orders.parquet`` atomically.

    Args:
        orders (pd.DataFrame): the table.
        path (pathlib.Path): the destination.
    Returns:
        None.
    """
    assert_safe_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp-{os.urandom(6).hex()}"
    orders.to_parquet(tmp, index=False, compression="snappy")
    tmp.replace(path)


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.build_orders``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(prog="python -m src.build_orders",
                                     description="Week 2 checkpoint: derived/orders.parquet (one row per order)")
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR, help="where the inputs are")
    parser.add_argument("--parties", type=pathlib.Path,
                        help="order_parties.parquet to use (default <derived>/order_parties.parquet)")
    parser.add_argument("--categories", type=pathlib.Path,
                        help="order_categories.parquet to use (default <derived>/order_categories.parquet)")
    parser.add_argument("--channels", type=pathlib.Path,
                        help="order_channels.parquet to use (default <derived>/order_channels.parquet)")
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: write orders.parquet and the log under DIR instead of derived/")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run stage 11: build and write ``derived/orders.parquet``.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0.
    """
    args = build_arg_parser().parse_args(argv)
    out_dir = args.scratch or DERIVED_DIR
    log_file = (args.scratch / "build_orders.log") if args.scratch else LOGS_DIR / "build_orders.log"
    for p in (out_dir, log_file):
        assert_safe_output(p)
    setup_logging(args.log_level, log_file)
    started = time.monotonic()
    orders = build(args.derived, args.parties, args.categories, args.channels)
    path = out_dir / "orders.parquet"
    write(orders, path)
    log.info("=" * 72)
    for k, v in summarise(orders).items():
        log.info("%-26s %s", k, v)
    log.info("%d columns; elapsed %.1f s; wrote %s", orders.shape[1], time.monotonic() - started, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
