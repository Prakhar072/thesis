"""Finality: is an order interim, a confirmation of interim directions, final, or a settlement (runbook Week 7 Day 1).

Stage 3 separates the event types, because an interim order is usually the first public news of a matter while a final
order follows years of proceedings. ``order_type`` decides most orders:

* ``SETTLEMENT`` → ``SETTLEMENT_FINAL`` (a settlement order closes the proceedings it settles);
* ``AO`` → ``FINAL`` (an adjudication order disposes of a show-cause notice);
* ``ED_CGM`` → ``FINAL`` (enforcement orders of executive directors; registration and similar matters);
* corrigenda, clarifications and addenda → ``OTHER``; takeover exemptions (WTM) → ``OTHER``.

Orders of the Chairperson and Members (``WTM``) are classified from SEBI's title when it says what the order is (about
1,000 of 6,424), else from the text, with rules checked against the titled orders (the title hidden):

1. "final order" in the order's opening → ``FINAL``; a takeover exemption or proposed acquisition in the opening →
   ``OTHER``;
2. the operative part confirms, extends or revokes directions ("hereby confirm", "stand confirmed", "hereby revoke")
   → ``CONFIRMATION_OR_EXTENSION``;
3. the operative part keeps directions "until further orders" or "pending investigation", invites objections or treats
   the order as a show-cause notice → ``INTERIM``;
4. the operative part restrains, debars or prohibits "for a period of …", or disposes of a show-cause notice → ``FINAL``;
5. an operative direction ("hereby cancel / suspend / restrain / debar / dispose / warn …") → ``FINAL`` (low);
6. otherwise ``UNKNOWN``.

Reads ``orders`` and ``derived/text/``. Writes ``derived/order_finality.parquet`` and ``logs/classify_finality.log``.
No network; seconds.
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import re

import pandas as pd

from src import cache
from src.extract_text import setup_logging

log = logging.getLogger("classify_finality")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
LOG_FILE = REPO_ROOT / "logs" / "classify_finality.log"
RULES_VERSION = "finality-v1"

INTERIM = "INTERIM"
CONFIRMATION = "CONFIRMATION_OR_EXTENSION"
FINAL = "FINAL"
SETTLEMENT = "SETTLEMENT_FINAL"
OTHER = "OTHER"
UNKNOWN = "UNKNOWN"
LABELS = (INTERIM, CONFIRMATION, FINAL, SETTLEMENT, OTHER, UNKNOWN)

#: The opening of an order (its own heading) and its operative part (the last 40%).
HEAD_CHARS = 4000
TAIL_SHARE = 0.6

TITLE_OTHER = re.compile(r"corrig|clarification|addendum|erratum", re.I)
TITLE_EXEMPTION = re.compile(r"\bexempt|proposed\s+acquisition|regulation\s+11\s+of\s+(?:the\s+)?sebi\s*\(?\s*sast",
                             re.I)
#: Takeover exemptions: SEBI orders, but not enforcement (registration refusals stay: many are CIS enforcement).
HEAD_NONENFORCEMENT = re.compile(
    r"proposed\s+acquisition\s+of\s+shares"
    r"|exemption\b.{0,160}?(?:takeover|substantial\s+acquisition|SAST|regulation\s+(?:3|4|10|11)\b)"
    r"|(?:takeover|substantial\s+acquisition|SAST).{0,160}?\bexemption\b", re.I)
HEAD_NONENFORCEMENT_CHARS = 1500
TITLE_CONFIRM = re.compile(r"confirm|revo[ck]|extension|modif", re.I)
TITLE_FINAL = re.compile(r"\bfinal\b", re.I)
TITLE_INTERIM = re.compile(r"interim|ex[\s-]*parte", re.I)

HEAD_FINAL = re.compile(r"\bfinal\s+order\b", re.I)
TAIL_CONFIRM = re.compile(
    r"hereby\s+confirm|(?:are|is|stand|stands)\s+(?:hereby\s+)?confirmed|confirm(?:s|ed)?\s+the\s+(?:directions|interim)"
    r"|hereby\s+revoke|(?:are|is|stand|stands)\s+(?:hereby\s+)?revoked|revok(?:e|ed)\s+the\s+(?:directions|interim)"
    r"|(?:hereby\s+)?extend(?:s|ed)?\s+the\s+(?:directions|interim|period)", re.I)
TAIL_INTERIM = re.compile(
    r"(?:until|till)\s+further\s+(?:orders?|directions?)"
    r"|pending\s+(?:the\s+)?(?:completion\s+of\s+)?(?:the\s+)?(?:investigation|inquiry|enquiry|examination)"
    r"|(?:treated|considered)\s+as\s+(?:a\s+)?show[\s-]*cause\s+notice"
    r"|(?:may|can|shall)\s+(?:file|submit)\s+(?:their|its|his|her)?\s*(?:reply|replies|objections?|response)", re.I)
#: An operative direction with no interim cue: a final order (low confidence).
TAIL_FINAL_WEAK = re.compile(
    r"hereby\s+(?:cancel|suspend|restrain|debar|prohibit|dispose|impose|warn|caution|direct|reject|order)"
    r"|(?:is|are)\s+(?:hereby\s+)?(?:restrained|debarred|prohibited|warned|cautioned)"
    r"|be\s+careful\s+in\s+future|disposed\s+of\s+(?:accordingly|without)", re.I)
TAIL_FINAL = re.compile(
    r"for\s+a\s+(?:further\s+)?period\s+of\s+\w+\s*(?:\(\s*\d+\s*\)\s*)?(?:years?|months?)"
    r"|show[\s-]*cause\s+notices?\b.{0,120}?(?:is|are|stands?)\s+(?:hereby\s+)?disposed", re.I)


def title_label(order_type: str, title: object) -> str | None:
    """The kind of order SEBI's listing title states, if it states one.

    Args:
        order_type (str): ``WTM``, ``AO``, ``SETTLEMENT`` or ``ED_CGM``.
        title (object): SEBI's title (str or null).

    Returns:
        str | None: a label from ``LABELS``, or None when the title says nothing.
    """
    text = title if isinstance(title, str) else ""
    if TITLE_OTHER.search(text):
        return OTHER
    if order_type == "SETTLEMENT":
        return SETTLEMENT
    if order_type in ("AO", "ED_CGM"):
        return FINAL
    if TITLE_EXEMPTION.search(text):
        return OTHER
    if TITLE_CONFIRM.search(text):
        return CONFIRMATION
    if TITLE_FINAL.search(text):
        return FINAL
    if TITLE_INTERIM.search(text):
        return INTERIM
    return None


def text_label(text: str) -> tuple[str, str, str | None]:
    """The kind of a WTM order from its text (rules 1–5 of the module docstring).

    Args:
        text (str): the order's text.

    Returns:
        tuple[str, str, str | None]: label, the rule that decided it, and the matched phrase.
    """
    flat = re.sub(r"\s+", " ", text)
    head, tail = flat[:HEAD_CHARS], flat[int(len(flat) * TAIL_SHARE):]
    for label, rule, pattern, part in ((FINAL, "head_final", HEAD_FINAL, head),
                                       (OTHER, "head_nonenforcement", HEAD_NONENFORCEMENT,
                                        flat[:HEAD_NONENFORCEMENT_CHARS]),
                                       (CONFIRMATION, "tail_confirm", TAIL_CONFIRM, tail),
                                       (INTERIM, "tail_interim", TAIL_INTERIM, tail),
                                       (FINAL, "tail_final", TAIL_FINAL, tail),
                                       (FINAL, "tail_final_weak", TAIL_FINAL_WEAK, tail)):
        match = pattern.search(part)
        if match:
            return label, rule, match.group(0)
    return UNKNOWN, "none", None


def classify(orders: pd.DataFrame, read_text) -> pd.DataFrame:
    """Label every order.

    Args:
        orders (pd.DataFrame): ``text_stem``, ``order_type``, ``title``, ``text_path``.
        read_text (Callable[[str], str | None]): reads an order's text from its ``text_path`` (None if absent).

    Returns:
        pd.DataFrame: ``text_stem``, ``finality``, ``finality_source`` ∈ ``title, type, text, none``,
            ``finality_rule``, ``finality_confidence`` ∈ ``high, medium, low``, ``finality_raw``, ``title_label``,
            ``text_label`` (WTM orders with text), ``rules_version``.
    """
    rows = []
    for order in orders.itertuples(index=False):
        from_title = title_label(order.order_type, order.title)
        from_text = rule = raw = None
        if order.order_type == "WTM" and isinstance(order.text_path, str):
            text = read_text(order.text_path)
            if text:
                from_text, rule, raw = text_label(text)
        if from_title is not None:
            source = "type" if order.order_type != "WTM" and from_title != OTHER else "title"
            label, confidence = from_title, "high"
            rule = rule if source == "title" and order.order_type == "WTM" else f"{source}:{order.order_type}"
        elif from_text is not None and from_text != UNKNOWN:
            source, label = "text", from_text
            confidence = "medium" if rule in ("head_final", "head_nonenforcement", "tail_confirm", "tail_interim",
                                              "tail_final") else "low"
        else:
            source, label, confidence = "none", UNKNOWN, "low"
        rows.append({"text_stem": order.text_stem, "finality": label, "finality_source": source,
                     "finality_rule": rule, "finality_confidence": confidence, "finality_raw": raw,
                     "title_label": from_title, "text_label": from_text, "rules_version": RULES_VERSION})
    return pd.DataFrame(rows)


def silver_check(labels: pd.DataFrame) -> pd.DataFrame:
    """How the text rules do on WTM orders whose title states the kind (the title hidden from the rules).

    Args:
        labels (pd.DataFrame): from ``classify`` (WTM rows with both a title and a text label).

    Returns:
        pd.DataFrame: a confusion table, title label (rows) × text label (columns).
    """
    both = labels[labels.title_label.isin([INTERIM, CONFIRMATION, FINAL]) & labels.text_label.notna()]
    return pd.crosstab(both.title_label, both.text_label, margins=True)


def reader(repo_root: pathlib.Path):
    """A text reader for ``classify``.

    Args:
        repo_root (pathlib.Path): the repository (``text_path`` is relative to it).

    Returns:
        Callable[[str], str | None]: reads a text file, None when missing.
    """
    def read(path: str) -> str | None:
        """Read one order's text.

        Args:
            path (str): repo-relative or absolute path.

        Returns:
            str | None: the text.
        """
        full = pathlib.Path(path) if pathlib.Path(path).is_absolute() else repo_root / path
        return full.read_text(errors="ignore") if full.exists() else None
    return read


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.classify_finality``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR)
    parser.add_argument("--out", type=pathlib.Path, default=None, help="where to write (default: --derived)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write ``order_finality.parquet``.

    Args:
        argv (list[str] | None): command-line arguments (None = ``sys.argv``).

    Returns:
        int: exit status (0).
    """
    args = build_arg_parser().parse_args(argv)
    out = args.out or args.derived
    setup_logging(args.log_level, LOG_FILE if out == DERIVED_DIR else out / "classify_finality.log")
    orders = pd.read_parquet(args.derived / "orders.parquet", columns=["text_stem", "order_type", "title", "text_path"])
    labels = classify(orders, reader(REPO_ROOT))
    assert len(labels) == len(orders) and labels.text_stem.is_unique
    out.mkdir(parents=True, exist_ok=True)
    labels.to_parquet(out / "order_finality.parquet", index=False)
    merged = labels.merge(orders[["text_stem", "order_type"]], on="text_stem")
    log.info("finality           %s", labels.finality.value_counts().to_dict())
    wtm = merged[merged.order_type == "WTM"]
    log.info("WTM                %s; source %s", wtm.finality.value_counts().to_dict(),
             wtm.finality_source.value_counts().to_dict())
    check = silver_check(wtm)
    log.info("text rules vs titles (WTM, title hidden):\n%s", check.to_string())
    log.info("wrote %s", out / "order_finality.parquet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
