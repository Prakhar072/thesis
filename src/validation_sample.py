"""Week 3 validation sample: 150 orders to hand-code blind (runbook Week 3, Days 3–4).

Reads ``derived/windows_checked.parquet`` (the Week 2 window table, after the
Day 3 sanity rules and review), the listings and the document table, and writes

* ``hand/validation_sample.csv`` — the coding sheet: order identity, SEBI's own
  title and date, links to the order, and empty columns to fill in. **No
  pipeline output is shown** (runbook: "if you can see the machine's answer you
  will anchor on it and your validation will overstate accuracy").
* ``hand/validation_pdfs/`` — a copy of each sampled order's PDF (the text, for
  the inline-HTML orders that have none), named ``<sample_no>_<text_stem>``.
  Copies, not links: ``raw/pdfs`` files are hard links into ``raw/cache``, so a
  viewer that saves highlights into one would change ``raw/``. Gitignored.
* ``derived/validation_key.parquet`` — which stratum each sampled order came
  from and how many orders it stands for, to weight accuracy back to the
  corpus. Kept out of ``hand/`` so the coder does not see it.

``--check`` reads the coding sheet while coding is under way and lists values
the Day 5 scorer could not read (a misspelt category, a date Excel turned into
05-11-2019, a detail that belongs to another channel) and protocol reminders
(a blank without a note, OTHER without its group letter). It reads only the
sheet and the sampled ids, never the pipeline's answers.

Why stratify on the pipeline's outcome
--------------------------------------
A simple random 150 would hold about 12 model windows (8% of orders) — too
few to measure the model — and about 17 identical ISO orders. So the sample is
drawn per stratum: regex windows, model windows of each ``window_kind``, and
orders with no window (to measure what the pipeline misses). Within a stratum
the pool is ordered by order type and date and drawn systematically, which
spreads the sample over categories and years in proportion (the runbook's
"stratified by year and order category"). ISO orders are capped within each
stratum, so each (stratum, ISO) cell carries its own weight, and accuracy
weighted back to the corpus is ``sum(weight * correct) / sum(weight)``.

Orders seen while building the pipeline are excluded (``SEEN``): the 90 labelled
while choosing the model (``hand/window_model_devset.csv``,
``hand/window_model_holdout.csv``), the coding protocol's worked examples
(``hand/protocol_examples.csv``), the orders read while tuning the
fraud-category and detection-channel auto-coders (``hand/fraud_category_devset.csv``,
``hand/detection_channel_devset.csv``), the flagged windows the user reviewed
with the machine's window in view (``hand/window_review.csv``), and every other
order whose text or output was read while building them or is quoted in the
audit and project notes (``hand/seen_while_building.csv``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import logging
import pathlib
import random
import re
import shutil

import pandas as pd

from src import cache
from src.classify_fraud import PRECEDENCE
from src.detect_channel import CHANNELS
from src.extract_text import assert_safe_output, setup_logging, text_stem

log = logging.getLogger("validation_sample")

REPO_ROOT = cache.REPO_ROOT
WINDOWS_IN = REPO_ROOT / "derived" / "windows_checked.parquet"
LISTINGS_IN = REPO_ROOT / "derived" / "listings.parquet"
DOCUMENTS_IN = REPO_ROOT / "derived" / "order_documents.parquet"
TEXT_DIR = REPO_ROOT / "derived" / "text"
#: Orders the pipeline's author read while building or documenting the coding:
#: the model dev/held-out sets, the coding protocol's worked examples, the
#: orders read while tuning the fraud-category and channel auto-coders, the
#: flagged windows the user reviewed, and the rest of what was read or quoted.
#: None may be coded blind.
SEEN = (REPO_ROOT / "hand" / "window_model_devset.csv", REPO_ROOT / "hand" / "window_model_holdout.csv",
        REPO_ROOT / "hand" / "protocol_examples.csv", REPO_ROOT / "hand" / "fraud_category_devset.csv",
        REPO_ROOT / "hand" / "detection_channel_devset.csv", REPO_ROOT / "hand" / "window_review.csv",
        REPO_ROOT / "hand" / "seen_while_building.csv")
SHEET_OUT = REPO_ROOT / "hand" / "validation_sample.csv"
FILES_OUT = REPO_ROOT / "hand" / "validation_pdfs"
KEY_OUT = REPO_ROOT / "derived" / "validation_key.parquet"

REGEX, MODEL_STATED, MODEL_CONDUCT, NO_WINDOW = "regex", "model_stated", "model_conduct", "no_window"
SIZES = {REGEX: 60, MODEL_STATED: 25, MODEL_CONDUCT: 25, NO_WINDOW: 40}
#: The Illiquid Stock Options scheme is ~2,400 near-identical orders sharing one
#: window; a few are enough to check it (at most this many per stratum).
ISO_CAP = {REGEX: 3, MODEL_STATED: 1, MODEL_CONDUCT: 1, NO_WINDOW: 1}
SEED = 20261006

#: Columns the coder fills in (runbook Week 3: window, fraud category,
#: detection channel, primary issuer; hand/coding_protocol.md §2–§5).
CODING_COLUMNS = ["violation_start", "violation_end", "window_kind", "fraud_category",
                  "detection_channel", "detection_detail", "primary_issuer", "notes"]
#: The values the protocol allows (§2, §3, §5).
WINDOW_KINDS = ("stated_period", "conduct_dates", "none")
FRAUD_CATEGORIES = tuple(PRECEDENCE)
DETAILS = {
    "SURVEILLANCE": ("alert", "trading_pattern_observed", "filings_observed"),
    "COMPLAINT": ("complaint", "scores", "whistleblower"),
    "AUDIT": ("qualification", "auditor_resignation", "forensic_audit"),
    "MEDIA": ("news", "research_report"),
    "REFERRAL": ("mca_roc", "sfio", "rbi", "tax", "police_ed_cbi", "court", "regulator", "government"),
    "SELF_DISCLOSURE": ("company_disclosure", "confession", "self_report"),
    "INSPECTION": ("sebi_inspection", "exchange_inspection", "thematic"),
    "UNCLEAR": ("none", "exchange_reference", "suo_motu"),
}
assert set(DETAILS) == set(CHANNELS)
#: §3.8: an OTHER order carries its group letter in notes ("OTHER-c").
OTHER_GROUP = re.compile(r"\bOTHER\s*[-–(]?\s*\(?[a-e]\b", re.IGNORECASE)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
#: Windows starting before this are unusual (check_windows flags < 1995); a reminder only.
EARLY_START = dt.date(1990, 1, 1)


def stratum(row) -> str | None:
    """The sampling stratum of one order, from the pipeline's window outcome.

    Args:
        row: a row of the window table (needs text_status, violation_start, violation_method, window_status,
            window_kind).
    Returns:
        str | None: regex, model_stated, model_conduct or no_window; None for an order without text.
    """
    if row.text_status != "OK":
        return None
    if pd.isna(row.violation_start):
        return NO_WINDOW
    # A window fixed by hand in the Day 3 review keeps the stratum of the regex window it replaced.
    if row.violation_method == "regex" or (row.violation_method == "hand" and row.window_status == "PARSED"):
        return REGEX
    return MODEL_STATED if row.window_kind == "stated_period" else MODEL_CONDUCT


def load_frame(windows: pathlib.Path = WINDOWS_IN, listings: pathlib.Path = LISTINGS_IN,
               documents: pathlib.Path = DOCUMENTS_IN) -> pd.DataFrame:
    """One row per order that could be sampled, with its stratum and links.

    Args:
        windows (pathlib.Path): ``derived/windows_checked.parquet``.
        listings (pathlib.Path): ``derived/listings.parquet``.
        documents (pathlib.Path): ``derived/order_documents.parquet``.
    Returns:
        pd.DataFrame: one row per order with text: the window columns, title, source_url, pdf_url, pdf_path,
        stratum, iso (bool).
    """
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
    """Every order read while building the pipeline (never in the blind sample).

    Args:
        paths (tuple[pathlib.Path, ...]): the hand files listing them (default SEEN).
    Returns:
        set[str]: their text stems.
    """
    out: set[str] = set()
    for p in paths:
        if p.exists():
            out |= set(pd.read_csv(p, dtype=str).text_stem)
    return out


def systematic(pool: pd.DataFrame, n: int, rng: random.Random) -> pd.DataFrame:
    """Every k-th order of the pool sorted by type and date, from a random start.

    Args:
        pool (pd.DataFrame): the orders to draw from.
        n (int): how many to draw.
        rng (random.Random): the random source (seeded).
    Returns:
        pd.DataFrame: n rows (the whole pool if it is smaller).
    """
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
    """Draw the blind sample: per stratum, ISO capped, systematic over type × date, then shuffled.

    Args:
        frame (pd.DataFrame): from load_frame.
        sizes (dict[str, int]): orders per stratum (60 / 25 / 25 / 40).
        exclude (set[str]): stems never to draw (seen while building).
        iso_cap (dict[str, int]): at most this many Illiquid Stock Options orders per stratum.
        seed (int): the random seed.
    Returns:
        pd.DataFrame: the sampled orders, in a shuffled order.
    """
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


def local_name(sample_no: int, row) -> str:
    """``012_46128.pdf``; ``.txt`` for an inline-HTML order, which has no PDF.

    Args:
        sample_no (int): the order's place on the sheet (1-based).
        row: a sampled order (text_stem, pdf_path).
    Returns:
        str: the copy's file name.
    """
    return f"{sample_no:03d}_{row.text_stem}.{'pdf' if pd.notna(row.pdf_path) else 'txt'}"


def copy_files(sample: pd.DataFrame, out_dir: pathlib.Path) -> list[str]:
    """Copy each sampled order's PDF (or inline text) to ``out_dir``; return the
    file names in sample order, blank where the source is missing.

    Args:
        sample (pd.DataFrame): the sampled orders, in sheet order.
        out_dir (pathlib.Path): ``hand/validation_pdfs`` (or a scratch folder).
    Returns:
        list[str]: the file name written per order ("" where the source file is missing).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for no, row in enumerate(sample.itertuples(), start=1):
        src = REPO_ROOT / row.pdf_path if pd.notna(row.pdf_path) else TEXT_DIR / f"{row.text_stem}.txt"
        dest = out_dir / local_name(no, row)
        assert_safe_output(dest)
        if not src.is_file():
            log.warning("no local file for sample %d (%s): %s", no, row.text_stem, src)
            names.append("")
            continue
        shutil.copyfile(src, dest)  # reads the source, writes a new file: never a link into raw/
        names.append(dest.name)
    return names


def sheet(sample: pd.DataFrame, local_files: list[str] | None = None) -> pd.DataFrame:
    """The blind coding sheet: identity, SEBI's own title and links, and empty coding columns.

    Args:
        sample (pd.DataFrame): the sampled orders, in sheet order.
        local_files (list[str] | None): the copies' paths, one per order.
    Returns:
        pd.DataFrame: ``hand/validation_sample.csv``'s rows (no pipeline output).
    """
    out = pd.DataFrame({
        "sample_no": range(1, len(sample) + 1),
        "text_stem": sample.text_stem.values,
        "order_id": pd.array(sample.order_id, dtype="Int64"),
        "order_date": sample.order_date.values,
        "order_type": sample.order_type.values,
        "title": sample.title.values,
        "order_page": sample.source_url.values,
        "pdf_url": sample.pdf_url.values,
        "local_file": local_files if local_files is not None else [""] * len(sample),
    })
    for col in CODING_COLUMNS:
        out[col] = ""
    return out


def key(sample: pd.DataFrame, frame: pd.DataFrame, exclude: set[str], seed: int) -> pd.DataFrame:
    """Stratum, cell and weight of each sampled order.

    The population is every order of the stratum with text, seen orders
    included, since accuracy is weighted back to the whole corpus. ISO orders
    are capped within each stratum, so a sampled ISO order stands for many more
    orders than a non-ISO one: ``weight = cell_population / cell_sampled`` with
    cells (stratum, iso).

    Args:
        sample (pd.DataFrame): the sampled orders.
        frame (pd.DataFrame): every order that could be sampled (the population).
        exclude (set[str]): the seen orders (only counted).
        seed (int): the draw's seed.
    Returns:
        pd.DataFrame: ``derived/validation_key.parquet``: text_stem, stratum, iso, stratum/cell population and
        sampled counts, weight, seed, excluded_seen.
    """
    cells = ["stratum", "iso"]
    k = sample[["text_stem", "stratum", "iso"]].copy()
    at = pd.MultiIndex.from_frame(k[cells])
    k["stratum_population"] = k.stratum.map(frame.groupby("stratum").size()).astype("int64")
    k["stratum_sampled"] = k.stratum.map(sample.groupby("stratum").size()).astype("int64")
    k["cell_population"] = frame.groupby(cells).size().reindex(at).to_numpy().astype("int64")
    k["cell_sampled"] = sample.groupby(cells).size().reindex(at).to_numpy().astype("int64")
    k["weight"] = k.cell_population / k.cell_sampled
    k["seed"] = seed
    k["excluded_seen"] = len(exclude)
    return k


def uncovered_cells(sample: pd.DataFrame, frame: pd.DataFrame) -> dict:
    """(stratum, iso) cells with orders in the corpus but none sampled: their
    orders carry no weight, so the weights sum short of the corpus.

    Args:
        sample (pd.DataFrame): the sampled orders.
        frame (pd.DataFrame): the population.
    Returns:
        dict: "<stratum>[ ISO]" → orders in that cell, for cells with none sampled.
    """
    pop = frame.groupby(["stratum", "iso"]).size()
    drawn = sample.groupby(["stratum", "iso"]).size().reindex(pop.index, fill_value=0)
    return {f"{s}{' ISO' if i else ''}": int(n) for (s, i), n in pop[drawn == 0].items()}


# --- checking the coded sheet ---------------------------------------------------------------------------------

def _date(value: str) -> dt.date | None:
    """A strict YYYY-MM-DD date from the sheet.

    Args:
        value (str): a cell.
    Returns:
        datetime.date | None: the date, or None if the cell is not exactly YYYY-MM-DD or not a real date.
    """
    if not ISO_DATE.fullmatch(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _suggest(value: str, allowed) -> str:
    """A “did you mean …?” hint for a misspelt value.

    Args:
        value (str): what was typed.
        allowed: the allowed values.
    Returns:
        str: " (did you mean X?)", or "".
    """
    near = difflib.get_close_matches(re.sub(r"[\s-]+", "_", value.strip()).upper(),
                                     [a.upper() for a in allowed], n=1, cutoff=0.6)
    if not near:
        return ""
    return f" (did you mean {next(a for a in allowed if a.upper() == near[0])}?)"


def read_sheet(path: pathlib.Path) -> tuple[pd.DataFrame, list[str]]:
    """The sheet as strings. Excel on Windows may re-save it in cp1252.

    Args:
        path (pathlib.Path): the coding sheet.
    Returns:
        tuple[pd.DataFrame, list[str]]: the sheet (all strings, stripped), and notes about how it was read.
    """
    notes = []
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="cp1252")
        notes.append("the sheet is not UTF-8 (re-saved by Excel?); read as cp1252 — check accented names")
    return df.apply(lambda c: c.str.strip()), notes


def check_row(r: dict) -> list[tuple[str, str]]:
    """Problems with one coded row as (level, message); level ``error`` = the
    Day 5 scorer cannot read it, ``remind`` = a protocol rule to look at.

    Args:
        r (dict): one sheet row (column → string).
    Returns:
        list[tuple[str, str]]: (level, message) per problem.
    """
    out = []
    note = r.get("notes", "")
    dates = {}
    for col in ("violation_start", "violation_end"):
        v = r.get(col, "")
        if v:
            dates[col] = _date(v)
            if dates[col] is None:
                out.append(("error", f"{col} {v!r} is not a YYYY-MM-DD date (Excel may have reformatted it)"))
    start, end = dates.get("violation_start"), dates.get("violation_end")
    if start and end and start > end:
        out.append(("error", f"violation_start {start} is after violation_end {end}"))
    order_date = _date(r.get("order_date", "")[:10])
    if end and order_date and end > order_date:
        out.append(("remind", f"the window ends after the order date ({order_date})"))
    if start and start < EARLY_START:
        out.append(("remind", f"the window starts in {start.year}: check the year"))

    kind = r.get("window_kind", "")
    has_dates = bool(r.get("violation_start") or r.get("violation_end"))
    if kind and kind not in WINDOW_KINDS:
        out.append(("error", f"window_kind {kind!r} is not one of {', '.join(WINDOW_KINDS)}{_suggest(kind, WINDOW_KINDS)}"))
    elif kind == "none" and has_dates:
        out.append(("error", "window_kind is none but dates are filled"))
    elif not kind and has_dates:
        out.append(("error", "dates are filled but window_kind is blank (stated_period or conduct_dates)"))
    elif kind in ("stated_period", "conduct_dates") and not (r.get("violation_start") and r.get("violation_end")) \
            and not note:
        out.append(("remind", "a window with a blank date needs a note saying why (§2 open end, §1.3)"))

    cat = r.get("fraud_category", "")
    if cat and cat not in FRAUD_CATEGORIES:
        out.append(("error", f"fraud_category {cat!r} is not a protocol category{_suggest(cat, FRAUD_CATEGORIES)}"))
    elif cat == "OTHER" and not OTHER_GROUP.search(note):
        out.append(("remind", "OTHER: write its group letter in notes, e.g. OTHER-c (§3.8)"))

    ch, detail = r.get("detection_channel", ""), r.get("detection_detail", "")
    if ch and ch not in CHANNELS:
        out.append(("error", f"detection_channel {ch!r} is not a protocol channel{_suggest(ch, CHANNELS)}"))
    elif ch and not detail:
        out.append(("remind", f"add detection_detail: {', '.join(DETAILS[ch])} (§5 table)"))
    elif detail and not ch:
        out.append(("error", "detection_detail is filled but detection_channel is blank"))
    elif detail and detail not in DETAILS[ch]:
        out.append(("error", f"detection_detail {detail!r} is not a {ch} detail: "
                             f"{', '.join(DETAILS[ch])}{_suggest(detail, DETAILS[ch])}"))

    for col in ("window_kind", "fraud_category", "detection_channel", "primary_issuer"):
        if not r.get(col) and not note:
            hint = " (NONE if the matter is about no company's securities, §4)" if col == "primary_issuer" else ""
            out.append(("remind", f"{col} is blank and notes do not say why (§1.3){hint}"))
    return out


def check(path: pathlib.Path, stems: set[str] | None = None) -> tuple[list[tuple[str, str, str]], dict]:
    """Check a coding sheet: (sample_no, level, message) problems and progress counts.

    Args:
        path (pathlib.Path): the coding sheet.
        stems (set[str] | None): the sampled stems from the key (None skips the identity check).
    Returns:
        tuple[list[tuple[str, str, str]], dict]: problems, and counts {rows, started, complete, doubts, errors,
        reminders}.
    """
    df, read_notes = read_sheet(path)
    problems = [("—", "remind", n) for n in read_notes]
    missing = [c for c in ["sample_no", "text_stem", "order_date", *CODING_COLUMNS] if c not in df.columns]
    if missing:
        return [("—", "error", f"columns missing from the sheet: {', '.join(missing)}")], {}
    if stems is not None:
        got = list(df.text_stem)
        if set(got) != stems or len(got) != len(set(got)):
            problems.append(("—", "error", f"the sheet's orders differ from the sample drawn: "
                                           f"{len(stems - set(got))} missing, {len(set(got) - stems)} extra, "
                                           f"{len(got) - len(set(got))} duplicated"))
    coded_cols = ["window_kind", "fraud_category", "detection_channel", "primary_issuer"]
    started = done = doubts = 0
    for r in df.to_dict("records"):
        if not any(r[c] for c in CODING_COLUMNS):
            continue
        started += 1
        done += all(r[c] for c in coded_cols)
        doubts += r["notes"].startswith("?")
        problems += [(r["sample_no"], level, msg) for level, msg in check_row(r)]
    stats = {"rows": len(df), "started": started, "complete": done, "doubts": doubts,
             "errors": sum(p[1] == "error" for p in problems),
             "reminders": sum(p[1] == "remind" for p in problems)}
    return problems, stats


def run_check(sheet_path: pathlib.Path, key_path: pathlib.Path) -> int:
    """Print the check of the coding sheet (``--check``).

    Args:
        sheet_path (pathlib.Path): the coding sheet.
        key_path (pathlib.Path): the key (for the sampled stems; optional).
    Returns:
        int: 1 if any error, else 0.
    """
    if not sheet_path.exists():
        log.error("no coding sheet at %s", sheet_path)
        return 1
    stems = set(pd.read_parquet(key_path, columns=["text_stem"]).text_stem) if key_path.exists() else None
    problems, stats = check(sheet_path, stems)
    for no, level, msg in problems:
        print(f"{'ERROR ' if level == 'error' else 'remind'}  #{no:>3}  {msg}")
    if stats:
        print(f"\n{stats['complete']} of {stats['rows']} orders complete ({stats['started']} started); "
              f"{stats['doubts']} marked '?'; {stats['errors']} errors, {stats['reminders']} reminders")
    return 1 if any(p[1] == "error" for p in problems) else 0


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.validation_sample``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.validation_sample",
        description="Draw the Week 3 validation sample and write the blind coding sheet",
    )
    parser.add_argument("--windows", type=pathlib.Path, default=WINDOWS_IN)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: write the sheet, copies and key under DIR instead of hand/ and derived/")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing coding sheet and its copies (they may hold your coding!)")
    parser.add_argument("--check", action="store_true",
                        help="check the coding sheet for values the scorer cannot read (no pipeline output shown)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Draw the sample and write the sheet, the copies and the key — or, with ``--check``, check the sheet.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0, or 1 (a sheet already exists, or ``--check`` found errors).
    """
    args = build_arg_parser().parse_args(argv)
    if args.scratch:
        sheet_out, files_out = args.scratch / "validation_sample.csv", args.scratch / "validation_pdfs"
        key_out = args.scratch / "validation_key.parquet"
    else:
        sheet_out, files_out, key_out = SHEET_OUT, FILES_OUT, KEY_OUT
    setup_logging(args.log_level, None)
    if args.check:
        return run_check(sheet_out, key_out)
    for p in (sheet_out, files_out, key_out):
        assert_safe_output(p)
    old_copies = sorted(files_out.glob("[0-9][0-9][0-9]_*")) if files_out.is_dir() else []
    if (sheet_out.exists() or old_copies) and not args.force:
        log.error("%s or %s already exists and may hold coding; pass --force to replace them", sheet_out, files_out)
        return 1

    frame = load_frame(args.windows)
    exclude = seen_stems()
    sample = draw(frame, exclude=exclude, seed=args.seed)
    for p in old_copies:
        p.unlink()
    names = copy_files(sample, files_out)
    try:
        shown = files_out.relative_to(REPO_ROOT)
    except ValueError:
        shown = files_out
    sheet_out.parent.mkdir(parents=True, exist_ok=True)
    key_out.parent.mkdir(parents=True, exist_ok=True)
    sheet(sample, [str(shown / n) if n else "" for n in names]).to_csv(sheet_out, index=False)
    key(sample, frame, exclude, args.seed).to_parquet(key_out, index=False)

    log.info("population by stratum: %s", frame.groupby("stratum").size().to_dict())
    log.info("sampled by stratum:    %s", sample.groupby("stratum").size().to_dict())
    log.info("sampled by type:       %s", sample.groupby("order_type").size().to_dict())
    log.info("ISO orders sampled: %d; excluded (seen while building): %d", int(sample.iso.sum()), len(exclude))
    if uncovered := uncovered_cells(sample, frame):
        log.warning("cells with no sampled order (unweighted): %s", uncovered)
    log.info("local copies: %d PDFs, %d texts, %d missing → %s", sum(n.endswith(".pdf") for n in names),
             sum(n.endswith(".txt") for n in names), names.count(""), files_out)
    log.info("wrote %s (coding sheet) and %s (key — do not open while coding)", sheet_out, key_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
