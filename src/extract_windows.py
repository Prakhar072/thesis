"""Week 2, Day 1: violation-window candidates and regex v1.

Reads ``derived/text_documents.parquet`` and each order's text file, and writes

* ``derived/window_candidates.parquet`` — the top ``TOP_K`` candidate sentences
  per order, each with its score, position, and what regex v1 parsed from it.
  The sentence is kept verbatim (whitespace collapsed): it becomes
  ``violation_raw`` (runbook rule 2).
* ``derived/windows.parquet`` — one row per order: the regex v1 window, if any,
  with ``violation_confidence`` and a ``window_status`` that routes the order to
  the Day 2 model fallback.

What the window is
------------------
The suspected violation period: the period of business activity SEBI
examined. The order may call it the investigation, examination, inspection or
relevant period — one quantity under four names, filling one
``violation_start``/``violation_end`` pair. SEBI's "conducted investigation
during the period X to Y" means that activity period. The dates the probe
itself took appear as a range only for on-site inspection visits, which
``inspection_visit`` removes before a range can become the window.

How orders state the period (corpus survey, 2026-09-27)
-------------------------------------------------------
The defining sentence usually puts the dates *before* the label::

    SEBI conducted an investigation into the trading in the scrip of X for the
    period from April 01, 2014 to September 30, 2015 (hereinafter referred to
    as "investigation period" / "IP").

and later sentences say only "during the investigation period". So the label
and the dates are matched anywhere in one sentence, not as an adjacent phrase.
Most date ranges in an order are *not* the window — offer periods ("kept open
from"), settlement schemes ("three months commencing from"), letters and
orders "dated X and Y", quarters ended — and a generic "during the period from
1.4.2000 to 31.3.2004" about funds raised is not an investigation period. A
range is therefore only taken as the window when the sentence carries an
investigation-type label, or an investigation word with the range introduced
by "period"; other ranges are kept as candidates and left to the model
(``UNCONFIRMED_RANGE``).

``window_status`` routes each order:

``PARSED``             regex v1 found a window (confidence high or medium).
``UNCONFIRMED_RANGE``  candidates hold a date range, but no label strong
                       enough to call it the window — Day 2 model.
``UNPARSED``           labelled candidates ("the investigation period …") with
                       no range regex v1 can read — Day 2 model.
``NO_CANDIDATES``      no sentence with a period keyword and a date. In the
                       survey these are chiefly orders about registration,
                       CIS deadlines, SCORES and shareholding compliance.
``NO_TEXT``            the text stage produced no text.

Dates
-----
Parsed with an explicit grammar rather than ``dateutil``: numeric dates are
day-first by construction (Indian orders: ``05.11.2019`` is 5 November), a
missing day is never filled from today's date, and each end records its
precision. A month-precision end ("to December 2011") resolves to the month's
last day. A start without a year ("from February 28 to April 30, 2002")
inherits the end's year, one year earlier if that would put it after the end.

Confidence (runbook schema)
---------------------------
``high``    an investigation/examination/inspection-period label (or the IP
            defined by "hereinafter") and a parsed range, start <= end.
``medium``  a parsed range under a weaker label (relevant/patch period, or an
            investigation word without "period"), a year inferred for the
            start, or a financial-year range.
``low``     no window: candidates not parsed, only generic ranges, or no
            candidates at all. ``violation_raw`` stays empty (runbook: "low
            with an empty raw when your parser could not find one").
``none`` (the order genuinely states no period) is never assigned here; it
needs a reader or the model to confirm the absence.

This stage reads only ``derived/``; it never touches ``raw/`` or the network.
"""

from __future__ import annotations

import argparse
import calendar
import collections
import concurrent.futures
import datetime as dt
import logging
import os
import pathlib
import random
import re
import sys
import time
from dataclasses import asdict, dataclass

import pyarrow as pa
import pyarrow.parquet as pq

from src import cache
from src.extract_text import PAGE_SEPARATOR, assert_safe_output, setup_logging

log = logging.getLogger("extract_windows")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
TEXT_DOCUMENTS_IN = DERIVED_DIR / "text_documents.parquet"
LOGS_DIR = REPO_ROOT / "logs"

RULES_VERSION = "regex-v1.1"
TOP_K = 3
#: Sentences longer than this (tables flattened into one "sentence") are
#: re-split at blank lines, then at line breaks. Real legal sentences reach
#: ~2,300 characters (99.9th percentile), and a lower cap split one between
#: "September" and "30, 2015".
MAX_SENTENCE = 2000
MIN_YEAR, MAX_YEAR = 1980, 2030

# window_status values
PARSED = "PARSED"
UNCONFIRMED_RANGE = "UNCONFIRMED_RANGE"
UNPARSED = "UNPARSED"
NO_CANDIDATES = "NO_CANDIDATES"
NO_TEXT = "NO_TEXT"

# label kinds, strongest first
DEFINED, STRONG, RELEVANT, CONTEXT, GENERIC = "defined", "strong", "relevant", "context", "generic"
LABEL_WEIGHT = {DEFINED: 8, STRONG: 6, RELEVANT: 4, CONTEXT: 3, GENERIC: 1}
RANGE_WEIGHT = 3
NEGATIVE_WEIGHT = -3
EARLY_WEIGHT = 1  # defining sentences sit in the background section


# --------------------------------------------------------------------------
# Header / footer removal
# --------------------------------------------------------------------------

_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")
#: Only lines this close to the top or bottom of a page can be running
#: headers/footers; a repeated line mid-page ("Limited" wrapped in a table)
#: is content.
EDGE_LINES = 5


def _line_key(line: str) -> str:
    """A line's identity for header/footer detection: lower case, whitespace collapsed, digits as #.

    Args:
        line (str): one text line.
    Returns:
        str: the key ("Page 3 of 11" and "Page 4 of 11" share one).
    """
    return _DIGITS.sub("#", _WS.sub(" ", line).strip().lower())


def strip_repeated_lines(text: str, *, min_pages: int = 3, share: float = 0.5) -> str:
    """Blank out running headers and footers, preserving every offset.

    A line counts as a header/footer when, with digits masked ("Page 3 of 11"
    -> "page # of #"), it sits within ``EDGE_LINES`` non-blank lines of the top
    or bottom of at least ``share`` of the pages (and of ``min_pages`` pages).
    Removed lines are overwritten with spaces, so offsets into the stripped
    text are offsets into the original file.

    Args:
        text (str): an order's text, pages separated by form feed.
        min_pages (int): a line must repeat on at least this many pages.
        share (float): … and on at least this share of the pages.
    Returns:
        str: the text with header/footer lines replaced by spaces (same length, same offsets).
    """
    pages = text.split(PAGE_SEPARATOR)
    if len(pages) < min_pages:
        return text
    counts: collections.Counter[str] = collections.Counter()
    split_pages = []
    for page in pages:
        lines = page.split("\n")
        split_pages.append(lines)
        nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
        edge = set(nonblank[:EDGE_LINES] + nonblank[-EDGE_LINES:])
        keys = {_line_key(lines[i]) for i in edge}
        counts.update(k for k in keys if len(k.replace("#", "").replace(" ", "")) >= 4)
    need = max(min_pages, share * len(pages))
    repeated = {k for k, c in counts.items() if c >= need}
    if not repeated:
        return text
    out = []
    for lines in split_pages:
        nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
        edge = set(nonblank[:EDGE_LINES] + nonblank[-EDGE_LINES:])
        out.append("\n".join(" " * len(ln) if i in edge and _line_key(ln) in repeated else ln
                             for i, ln in enumerate(lines)))
    return PAGE_SEPARATOR.join(out)


# --------------------------------------------------------------------------
# Sentences
# --------------------------------------------------------------------------

#: Tokens that end in a full stop without ending a sentence. Single letters
#: (initials, "S.E.B.I.") are handled separately.
ABBREVIATIONS = frozenset("""
    ltd pvt co corp inc bros no nos rs re m/s mr mrs ms dr sh shri smt kum sr jr st
    i.e e.g viz vs v u/s r/w w.e.f reg regn regs sec secs cl art para paras annex
    encl govt dept addl asst dy gen mgr ld hon hon'ble ex dt ref vol pg fig approx
    admn jt ch sub cf al sl misc prop nr opp dist distt ph tel cr amt qty est max
    min avg bldg rd
""".split())

_BOUNDARY = re.compile(r"[.?!][\"'”’)\]]*(?=\s+[\"'“‘(]?[A-Z0-9])")
_LAST_TOKEN = re.compile(r"(\S+)$")
_DOTTED = re.compile(r"(?:[a-z]\.)*[a-z]")


def _is_abbreviation(before: str) -> bool:
    """Whether the token before a full stop is an abbreviation or initial (no sentence break).

    Args:
        before (str): the text up to the full stop.
    Returns:
        bool: True for "Ltd", "No", "S.E.B.I" and the like.
    """
    m = _LAST_TOKEN.search(before)
    if not m:
        return False
    token = m.group(1).lower().lstrip("(“\"'‘")
    return token in ABBREVIATIONS or bool(_DOTTED.fullmatch(token))


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """Move a span's ends inward past whitespace.

    Args:
        text (str): the text.
        start (int), end (int): the span.
    Returns:
        tuple[int, int]: the trimmed span.
    """
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _resplit_long(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Break an over-long "sentence" at blank lines, then single line breaks.

    Args:
        text (str): the text.
        start (int), end (int): an over-long sentence's span.
    Returns:
        list[tuple[int, int]]: spans of at most about MAX_SENTENCE characters.
    """
    pieces = [(start, end)]
    for pattern in (re.compile(r"\n[ \t]*\n"), re.compile(r"\n")):
        out = []
        for s, e in pieces:
            if e - s <= MAX_SENTENCE:
                out.append((s, e))
                continue
            chunk_start = s
            last = s
            for m in pattern.finditer(text, s, e):
                if m.start() - chunk_start > MAX_SENTENCE and last > chunk_start:
                    out.append((chunk_start, last))
                    chunk_start = last
                last = m.end()
            out.append((chunk_start, e))
        pieces = out
    return pieces


def split_sentences(text: str, groups: bool = False) -> list[tuple[int, int]] | list[tuple[int, int, int]]:
    """``(start, end)`` offsets of sentences in ``text``.

    Splits after ``.``/``?``/``!`` (and any closing quote or bracket) when the
    next word starts with a capital, digit, quote or bracket, unless the full
    stop ends a known abbreviation or an initial. Page breaks are treated as
    whitespace: sentences run across pages.

    With ``groups=True`` each span is ``(start, end, group)``: pieces cut from
    one over-long sentence by ``_resplit_long`` share a group, so a stage that
    must read a clause with the table after it ("I hereby impose the following
    penalty … – [table]") can put them back together.

    Args:
        text (str): an order's text (headers stripped).
        groups (bool): also return each span's group id.
    Returns:
        list[tuple[int, int]] | list[tuple[int, int, int]]: (start, end) offsets, or (start, end, group).
    """
    if groups:
        return _split_sentences(text)
    return [(s, e) for s, e, _ in _split_sentences(text)]


def _split_sentences(text: str) -> list[tuple[int, int, int]]:
    """Sentence spans with group ids (the implementation of split_sentences).

    Args:
        text (str): the text.
    Returns:
        list[tuple[int, int, int]]: (start, end, group) per sentence piece.
    """
    spans = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        if m.group(0)[0] == "." and _is_abbreviation(text[start:m.start()]):
            continue
        spans.append((start, m.end()))
        start = m.end()
    spans.append((start, len(text)))
    out = []
    for g, (s, e) in enumerate(spans):
        s, e = _trim(text, s, e)
        if e <= s:
            continue
        for ps, pe in _resplit_long(text, s, e):
            ps, pe = _trim(text, ps, pe)
            if pe > ps:
                out.append((ps, pe, g))
    return out


def normalise_sentence(text: str) -> str:
    """A sentence as stored: form feeds and runs of whitespace become single spaces.

    Args:
        text (str): raw sentence text.
    Returns:
        str: the normalised sentence (``violation_raw`` and candidate sentences use this form).
    """
    return _WS.sub(" ", text.replace(PAGE_SEPARATOR, " ")).strip()


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "febuary": 2, "feburary": 2,
    "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MON = r"(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")(?![a-z])\.?"
_ORD = r"(?:\s?(?:st|nd|rd|th)(?![a-z]))?"  # "3rd", and "3 rd" from some PDFs
_DAY = r"\d{1,2}" + _ORD
_Y4 = r"(?:19|20)\d{2}(?!\d)"

# Finding patterns. No capture groups: RANGE embeds them next to its own
# named groups, and parse_date re-reads each matched token.
_F_DMYH = rf"\d{{1,2}}\s?[-/.]\s?{_MON}\s?[-/.,]\s?(?:{_Y4}|\d{{2}}(?!\d))"  # 01-Apr-2019
_F_DMY = rf"{_DAY}\s*(?:day\s+of\s+|of\s+)?{_MON}\s*[,.]?\s*{_Y4}"
_F_MDY = rf"{_MON}\s*,?\s*{_DAY}\s*[,.]?\s*{_Y4}"
_F_NUM = r"(?<![\d.])\d{1,2}\s?[./-]\s?\d{1,2}\s?[./-]\s?(?:\d{4}|\d{2})(?![\d])"
_F_ISO = r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)"
_F_MY = rf"{_MON}\s*,?\s*{_Y4}"
_F_MYY = rf"{_MON}\s*['’]\s*\d{{2}}(?!\d)"
#: No year — valid only as the start of a range whose end has one.
_F_DM = rf"{_DAY}\s+(?:of\s+)?{_MON}"
_F_MD = rf"{_MON}\s*,?\s*{_DAY}(?!\d)"
_F_M = _MON

# Order matters: alternation takes the first form that matches, so the
# longer forms come first ("April 1, 2014" before "April").
_FULL = rf"(?:{_F_DMYH}|{_F_DMY}|{_F_MDY}|{_F_NUM}|{_F_ISO}|{_F_MY}|{_F_MYY})"
_PARTIAL = rf"(?:{_F_DM}|{_F_MD}|{_F_M})"

DATE = re.compile(rf"(?<![A-Za-z0-9]){_FULL}", re.IGNORECASE)

_CONNECTOR = r"(?:to\s+and\s+including|to|till|until|upto|up\s+to|through|and|[-–—~])"

RANGE = re.compile(
    rf"(?:(?P<pre>from|between|beginning|starting|commencing|period(?:\s+of)?(?:\s*[-–:,]|\s+i\.?\s?e\.?,?)?)"
    rf"\s+(?:(?:on|from)\s+)?)?"
    rf"(?<![A-Za-z0-9])(?P<a>{_FULL}|{_PARTIAL})"
    rf"\s*,?\s*(?:\(\s*[^)]{{0,30}}\)\s*)?(?P<conn>{_CONNECTOR})\s*(?:the\s+)?"
    # "from April 1, 2018 till the date of inspection, February 15, 2020"
    rf"(?:date\s+of\s+(?:the\s+)?inspection\s*[,(]?\s*(?:i\.?\s?e\.?\s*,?\s*)?)?(?:on\s+)?"
    rf"(?P<b>{_FULL})",
    re.IGNORECASE,
)

_FY_ONE = r"(\d{4})\s*[-–/]\s*(\d{2}|\d{4})"
FY_RANGE = re.compile(
    rf"(?:F\.?\s?Y\.?|financial\s+years?)\s*['’]?\s*{_FY_ONE}"
    rf"(?:\s*(?:to|till|upto|up\s+to|and|[-–])\s*(?:(?:F\.?\s?Y\.?|financial\s+year)\s*['’]?\s*)?{_FY_ONE})?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedDate:
    """A date as read from the text, with its precision.

    Attributes:
        year (int | None): None for a partial date ("February 28" at a range start).
        month (int): 1–12.
        day (int | None): None = month precision.
    """
    year: int | None
    month: int
    day: int | None  # None: month precision

    @property
    def precision(self) -> str:
        """How precise the date is.

        Returns:
            str: "day" or "month".
        """
        return "day" if self.day is not None else "month"


def _month(token: str) -> int | None:
    """A month name or abbreviation as a number.

    Args:
        token (str): e.g. "Sept." or "march".
    Returns:
        int | None: 1–12, or None if not a month.
    """
    return MONTHS.get(token.lower().rstrip("."))


def _year(text: str) -> int:
    """A year token as a four-digit year (two digits: 80–99 → 1900s, else 2000s).

    Args:
        text (str): "2014" or "14".
    Returns:
        int: the year.
    """
    y = int(text)
    if len(text) == 2:
        y += 1900 if y >= 80 else 2000
    return y


_P_DMYH = re.compile(rf"(\d{{1,2}})\s?[-/.]\s?({_MON})\s?[-/.,]\s?(\d{{4}}|\d{{2}})", re.I)
_P_DMY = re.compile(rf"(\d{{1,2}}){_ORD}\s*(?:day\s+of\s+|of\s+)?({_MON})\s*[,.]?\s*(\d{{4}})", re.I)
_P_MDY = re.compile(rf"({_MON})\s*,?\s*(\d{{1,2}}){_ORD}\s*[,.]?\s*(\d{{4}})", re.I)
_P_NUM = re.compile(r"(\d{1,2})\s?[./-]\s?(\d{1,2})\s?[./-]\s?(\d{4}|\d{2})")
_P_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_P_MY = re.compile(rf"({_MON})\s*,?\s*(\d{{4}})", re.I)
_P_MYY = re.compile(rf"({_MON})\s*['’]\s*(\d{{2}})", re.I)
_P_DM = re.compile(rf"(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MON})", re.I)
_P_MD = re.compile(rf"({_MON})\s*,?\s*(\d{{1,2}}){_ORD}", re.I)
_P_M = re.compile(rf"({_MON})", re.I)


def _read_date(t: str) -> tuple[int | None, int | None, int | None] | None:
    """``(year, month, day)`` from a token, day-first for numeric dates.

    Args:
        t (str): a normalised date token.
    Returns:
        tuple[int | None, int | None, int | None] | None: (year, month, day), or None if no form fits.
    """
    if m := _P_DMYH.fullmatch(t):
        return _year(m.group(3)), _month(m.group(2)), int(m.group(1))
    if m := _P_DMY.fullmatch(t):
        return int(m.group(3)), _month(m.group(2)), int(m.group(1))
    if m := _P_MDY.fullmatch(t):
        return int(m.group(3)), _month(m.group(1)), int(m.group(2))
    if m := _P_ISO.fullmatch(t):
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m := _P_NUM.fullmatch(t):
        return _year(m.group(3)), int(m.group(2)), int(m.group(1))
    if m := _P_MY.fullmatch(t):
        return int(m.group(2)), _month(m.group(1)), None
    if m := _P_MYY.fullmatch(t):
        return _year(m.group(2)), _month(m.group(1)), None
    if m := _P_DM.fullmatch(t):
        return None, _month(m.group(2)), int(m.group(1))
    if m := _P_MD.fullmatch(t):
        return None, _month(m.group(1)), int(m.group(2))
    if m := _P_M.fullmatch(t):
        return None, _month(m.group(1)), None
    return None


def parse_date(token: str) -> ParsedDate | None:
    """Parse one date token matched by ``DATE`` or a range end.

    Numeric dates are day-first. Returns None for anything that is not a real
    calendar date in ``MIN_YEAR..MAX_YEAR`` (a partial date has year None).

    Args:
        token (str): a date token.
    Returns:
        ParsedDate | None: the date, or None if it is not a valid calendar date in range.
    """
    read = _read_date(normalise_sentence(token))
    if read is None:
        return None
    year, month, day = read
    if month is None or not 1 <= month <= 12:
        return None
    if year is not None and not MIN_YEAR <= year <= MAX_YEAR:
        return None
    if day is not None:
        # 29 February needs a year to check; 2000 is a leap year.
        last = calendar.monthrange(year if year is not None else 2000, month)[1]
        if not 1 <= day <= last:
            return None
    return ParsedDate(year, month, day)


def as_start(p: ParsedDate) -> dt.date:
    """A parsed date as the start of a window (month precision → the 1st).

    Args:
        p (ParsedDate): a date with a year.
    Returns:
        datetime.date: the start date.
    """
    return dt.date(p.year, p.month, p.day or 1)


def as_end(p: ParsedDate) -> dt.date:
    """A parsed date as the end of a window (month precision → the month's last day).

    Args:
        p (ParsedDate): a date with a year.
    Returns:
        datetime.date: the end date.
    """
    return dt.date(p.year, p.month, p.day or calendar.monthrange(p.year, p.month)[1])


@dataclass
class Window:
    """A date range read from a sentence.

    Attributes:
        start (datetime.date), end (datetime.date): the window.
        start_precision (str), end_precision (str): "day", "month" or "fy".
        rule (str): range, range_year_inferred, fy, fy_range, patches_union, range_till_inspection, range_fy_list.
        text (str): the matched text, verbatim.
        span (tuple[int, int]): its offsets in the sentence.
    """
    start: dt.date
    end: dt.date
    start_precision: str
    end_precision: str
    rule: str       # range | range_year_inferred | fy | fy_range
    text: str       # the matched range, verbatim
    span: tuple[int, int]


def parse_range(m: re.Match) -> Window | None:
    """Turn a ``RANGE`` match into a window, or None if it is not one.

    Args:
        m (re.Match): a match of RANGE.
    Returns:
        Window | None: the range, or None (two dates listed with "and", unparseable ends, start after end).
    """
    pre = (m.group("pre") or "").lower()
    if m.group("conn").lower() == "and" and not pre.startswith(("between", "period")):
        # "letters dated X and Y" lists two dates; "between X and Y" and "for
        # the period October 1999 and March 2001" are ranges.
        return None
    a, b = parse_date(m.group("a")), parse_date(m.group("b"))
    if a is None or b is None or b.year is None:
        return None
    rule = "range"
    if a.year is None:
        # "from February 28 to April 30, 2002": the start takes the end's year,
        # or the year before when that would put it after the end.
        rule = "range_year_inferred"
        a = ParsedDate(b.year, a.month, a.day)
        try:
            if as_start(a) > as_end(b):
                a = ParsedDate(b.year - 1, a.month, a.day)
        except ValueError:
            return None
    try:
        start, end = as_start(a), as_end(b)
    except ValueError:  # 29 February in an inferred non-leap year
        return None
    if start > end:
        return None
    return Window(start, end, a.precision, b.precision, rule, m.group(0), m.span())


def parse_fy(m: re.Match) -> Window | None:
    """``FY 2011-12`` -> 1 Apr 2011..31 Mar 2012; ``FY 2011-12 to 2013-14`` spans both.

    Args:
        m (re.Match): a match of FY_RANGE.
    Returns:
        Window | None: 1 April of the first year to 31 March after the last, or None if the years are not
        consecutive.
    """
    def fy(first: str, second: str) -> int | None:
        """The first year of a financial year written as two years.

        Args:
            first (str): "2011".
            second (str): "12" or "2012".
        Returns:
            int | None: 2011, or None if the second year does not follow the first.
        """
        y1 = int(first)
        y2 = int(second) if len(second) == 4 else (y1 // 100) * 100 + int(second)
        if len(second) == 2 and y2 < y1:  # 1999-00
            y2 += 100
        return y1 if y2 == y1 + 1 and MIN_YEAR <= y1 <= MAX_YEAR else None

    y_start = fy(m.group(1), m.group(2))
    if y_start is None:
        return None
    y_end = y_start
    if m.group(3):
        y_end = fy(m.group(3), m.group(4))
        if y_end is None or y_end < y_start:
            return None
    return Window(dt.date(y_start, 4, 1), dt.date(y_end + 1, 3, 31), "fy", "fy",
                  "fy_range" if m.group(3) else "fy", m.group(0), m.span())


# --------------------------------------------------------------------------
# Inspection visits
# --------------------------------------------------------------------------
#
# The window wanted is the period of business activity SEBI examined — under
# any of its names (investigation, examination, inspection, relevant period).
# SEBI's usual "conducted investigation during the period X to Y" means that
# activity period, and is kept. The one place the dates of the probe itself
# appear is the on-site inspection of an intermediary: "SEBI conducted an
# inspection of the Company during the period February 28, 2005 to March 5,
# 2005" (a six-day visit), sometimes even labelled "inspection period". In the
# corpus every inspection window of a month or less read as a visit; the
# periods inspections cover run for months or years.

MAX_VISIT_DAYS = 31
#: A longer window counts as a visit only when phrased as one ("conducted the
#: inspection during the period May 2003 – June 2003 and the period covered
#: under the inspection was 2001-2002").
MAX_PHRASED_VISIT_DAYS = 100
_INSPECT = re.compile(r"\binspect\w*|\bexamination\s+of\s+(?:the\s+)?books", re.IGNORECASE)
#: Between "inspection" and the dates, words that make the dates the activity:
#: "inspection in the scrip of X", "in the inspection it was observed that the
#: noticee executed trades during …". Company names such as "Shreni Shares
#: Private Limited" must not count, hence "shares of", not "shares".
_ACTIVITY = re.compile(
    r"\b(?:observed|found|noted|revealed|executed|traded|bought|sold|dealt|transactions?|trades|scrip"
    r"|dealings?\s+(?:in|of)|trading\s+(?:in|of|activit\w*)|shares\s+of)\b", re.IGNORECASE)
_CONDUCTED = re.compile(r"\b(?:conduct\w*|carri\w*\s+out|undert\w*|taken\s+up|take\s+up|held|done)\b", re.IGNORECASE)
_COVERING = re.compile(r"\b(?:for|covering|covered|pertaining|relating|respect)\b", re.IGNORECASE)
_LABEL_GAP = re.compile(r"\s*period\s*(?:(?:was|is|of|i\.?\s?e\.?|between|from|being)\b|[:,(\-–])*\s*",
                        re.IGNORECASE)
#: How far back "inspection" may be: broker orders put long registration
#: details between the entity and the dates.
VISIT_LOOKBACK = 700
_TILL_INSPECTION = re.compile(
    rf"(?<![A-Za-z0-9])(?P<a>{_FULL})\s*,?\s*(?:till|to|until|upto|up\s+to)\s+(?:the\s+)?"
    rf"date\s+of\s+(?:the\s+)?inspection", re.IGNORECASE)


def inspection_visit(sentence: str, window: Window) -> bool:
    """Are these the dates an inspection was carried out, not the period it covered?

    A visit is (a) an inspection window of ``MAX_VISIT_DAYS`` or less, stated
    as an inspection being conducted/carried out or labelled the inspection
    period; or (b) up to ``MAX_PHRASED_VISIT_DAYS`` when phrased as "conducted
    the inspection during …" with no "for/covering" before the dates. Either
    way, nothing between "inspection" and the dates may make them the activity
    ("in the scrip", "observed that … traded").

    Args:
        sentence (str): the normalised sentence.
        window (Window): a range found in it.
    Returns:
        bool: True if the range is the inspection visit itself (never the window).
    """
    s, e = window.span
    before = sentence[max(0, s - VISIT_LOOKBACK):s]
    hits = list(_INSPECT.finditer(before))
    last = hits[-1] if hits else None
    between = before[last.end():] if last else ""
    # "… (hereinafter referred to as 'inspection period')" after the dates, or
    # "during inspection period between 22.09.2015 to 28.09.2015" right before.
    labelled = (bool(_INSPECT.search(sentence[e:e + 80]))
                or (last is not None and bool(_LABEL_GAP.fullmatch(between))))
    if last is None and not labelled:
        return False
    if _ACTIVITY.search(between):
        return False
    conducted = bool(_CONDUCTED.search(sentence[:s]))
    days = (window.end - window.start).days + 1
    if days <= MAX_VISIT_DAYS:
        return conducted or labelled
    return (days <= MAX_PHRASED_VISIT_DAYS and last is not None and conducted
            and not _COVERING.search(between + window.text[:12]))


def till_inspection(sentence: str, visits: list[Window]) -> list[Window]:
    """ "for the period 01.04.2012 till the date of inspection", with the visit
    dates elsewhere in the sentence: the covered period ends when the visit does.

    Args:
        sentence (str): the normalised sentence.
        visits (list[Window]): the inspection-visit ranges found in it.
    Returns:
        list[Window]: covered periods that end with the visit (rule range_till_inspection).
    """
    if not visits:
        return []
    last = max(visits, key=lambda w: w.end)
    out = []
    for m in _TILL_INSPECTION.finditer(sentence):
        a = parse_date(m.group("a"))
        if a is None or a.year is None:
            continue
        start = as_start(a)
        if start < last.end:
            out.append(Window(start, last.end, a.precision, "day", "range_till_inspection",
                              m.group(0), m.span()))
    return out


_BARE_FY = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-–]\s*(\d{2}|(?:19|20)\d{2})(?!\d)")
_FY_LIST_FILLER = re.compile(r"^[\s,&\-–]*(?:(?:and|from|of|the)\b[\s,&\-–]*)*$", re.IGNORECASE)


def extend_with_fy_list(sentence: str, window: Window) -> Window:
    """ "the period 2000-2001, 2001-2002 and from April 2002 till the date of
    inspection i.e. 24.05.2002": the range read is only the last part; the
    financial years listed after "period" move its start back.

    Args:
        sentence (str): the normalised sentence.
        window (Window): the range read.
    Returns:
        Window: the range with its start moved back to the first listed financial year, or unchanged.
    """
    s = window.span[0]
    before = sentence[max(0, s - 80):s]
    at = before.lower().rfind("period")
    if at < 0:
        return window
    listed = before[at + len("period"):]
    starts = []
    for m in _BARE_FY.finditer(listed):
        y1, second = int(m.group(1)), m.group(2)
        y2 = int(second) if len(second) == 4 else (y1 // 100) * 100 + int(second)
        if y2 == y1 + 1:
            starts.append(dt.date(y1, 4, 1))
    rest = _FILLER_YEARS.sub(" ", _BARE_FY.sub(" ", listed))
    if not starts or not _FY_LIST_FILLER.match(rest) or min(starts) >= window.start:
        return window
    return Window(min(starts), window.end, "fy", window.end_precision, "range_fy_list",
                  sentence[max(0, s - len(listed)):window.span[1]], (s - len(listed), window.span[1]))


_FILLER_YEARS = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def find_windows(sentence: str) -> list[Window]:
    """Every date range and financial-year span in a sentence, in order.

    Args:
        sentence (str): a normalised sentence.
    Returns:
        list[Window]: the ranges and financial-year spans, by position.
    """
    found = [w for m in RANGE.finditer(sentence) if (w := parse_range(m))]
    taken = [w.span for w in found]
    for m in FY_RANGE.finditer(sentence):
        if any(s < m.end() and m.start() < e for s, e in taken):
            continue
        if w := parse_fy(m):
            found.append(w)
    return sorted(found, key=lambda w: w.span)


# --------------------------------------------------------------------------
# Candidate scoring
# --------------------------------------------------------------------------

_I = re.IGNORECASE
LABELS: list[tuple[str, re.Pattern]] = [
    (DEFINED, re.compile(
        r"(?:hereinafter|herein\s*after)[^.;]{0,40}?[“\"‘'`]+\s*(?:the\s+)?"
        r"(?:investigation|examination|inspection|investigating|relevant|review|patch)?\s*"
        r"period(?:\s+of\s+(?:investigation|examination|inspection))?", _I)),
    (DEFINED, re.compile(r"[“\"‘'(]\s*(?:IP|EP)\s*[”\"’')]")),
    # Not "pre-/post-examination period": those flank the window.
    (STRONG, re.compile(
        r"(?<!pre-)(?<!post-)(?<!pre )(?<!post )"
        r"\b(?:investigation|examination|inspection|investigating)\s+period\b"
        r"|\bperiod\s+(?:of|under)\s+(?:the\s+)?(?:investigation|examination|inspection)\b", _I)),
    (STRONG, re.compile(r"(?<![A-Za-z])(?<!TCP/)IP(?![A-Za-z])(?!\s+(?:address|telephony))")),
    # "SEBI conducted investigation into dealings in the shares of X for the
    # period from ..." — the older orders' way of stating the window.
    (STRONG, re.compile(
        r"\b(?:conduct\w*|carri\w*\s+out|undert\w*|initiat\w*|ordered)\s+(?:an?\s+|the\s+)?"
        r"(?:detailed\s+|preliminary\s+)?(?:investigations?|examinations?|inspections?)\b"
        r"[^;]{0,250}?\b(?:for|during|covering|in)\s+the\s+period\b", _I)),
    (RELEVANT, re.compile(
        r"\b(?:relevant|patch|upsi|review)\s+period\b|\bperiod\s+of\s+upsi\b"
        r"|\bperiod\s+under\s+(?:review|consideration|reference|scrutiny)\b", _I)),
    (CONTEXT, re.compile(r"\b(?:investigat\w*|examin\w*|inspect\w*|surveillance)\b", _I)),
    (GENERIC, re.compile(r"\bperiod\b", _I)),
]

NEGATIVE = re.compile(
    r"open\s+offer|kept\s+open|remained\s+open|offer\s+period|tendering\s+period"
    r"|settlement\s+scheme|\bscheme\s+(?:was|commenced|shall)|commencing\s+from"
    r"|quarters?\s+end(?:ed|ing)|trading\s+window"
    r"|\b(?:letter|order|notice|e-?mail|summons|scn|reply|communication)s?\s+dated"
    r"|personal\s+hearing|hearing\s+(?:was|on)",
    _I,
)

#: Cheap pre-filter: a sentence without any of these cannot be a candidate.
_PREFILTER = re.compile(r"period|investigat|examin|inspect|surveillance|\bIP\b|F\.?\s?Y|financial\s+year", _I)


@dataclass
class Candidate:
    """One candidate sentence: a row of ``derived/window_candidates.parquet`` (columns in CLAUDE.md §8).

    Attributes:
        text_stem (str), cand_rank (int, 1 = best), score (int), sent_index (int), char_start (int),
        char_end (int), page_no (int), rel_pos (float), sentence (str), label_kind (str), label_text (str),
        n_dates (int), n_ranges (int), negative (str | None), and what regex v1 read from it:
        range_text, parsed_start, parsed_end, start_precision, end_precision, parse_rule, confidence.
    """
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    cand_rank: int
    score: int
    sent_index: int
    char_start: int
    char_end: int
    page_no: int
    rel_pos: float
    sentence: str
    label_kind: str
    label_text: str
    n_dates: int
    n_ranges: int
    negative: str | None
    range_text: str | None = None
    parsed_start: dt.date | None = None
    parsed_end: dt.date | None = None
    start_precision: str | None = None
    end_precision: str | None = None
    parse_rule: str | None = None
    confidence: str | None = None


def _labels(sentence: str) -> list[tuple[str, re.Match]]:
    """Every period label in a sentence, with its kind.

    Args:
        sentence (str): a normalised sentence.
    Returns:
        list[tuple[str, re.Match]]: (kind, match) for each label found (kinds: defined … generic).
    """
    out = []
    for kind, pattern in LABELS:
        for m in pattern.finditer(sentence):
            out.append((kind, m))
    return out


def _best_kind(labels: list[tuple[str, re.Match]]) -> tuple[str, re.Match] | None:
    """The strongest label (earliest on a tie).

    Args:
        labels (list[tuple[str, re.Match]]): from _labels.
    Returns:
        tuple[str, re.Match] | None: the best label, or None if there is none.
    """
    order = list(LABEL_WEIGHT)
    return min(labels, key=lambda km: (order.index(km[0]), km[1].start()), default=None)


_PERIOD_BEFORE = re.compile(r"\bperiods?\b[^.;]{0,40}$", _I)
#: "Patch 1: … Patch 2: …", "first period … second period", "Period-I/II".
#: Not "period i.e." — a check found "period i" there merging a manipulation
#: window with the months its trades settled in (16938).
_PATCHES = re.compile(r"\bpatch|\b(?:first|second|third)\s+period\b|\bperiod[- ]?(?:i{1,3}|[123])(?![.\w])", _I)


def period_introduced(sentence: str, window: Window) -> bool:
    """Is the range introduced by "period" ("for the period April 2000 to …")?

    Args:
        sentence (str): the normalised sentence.
        window (Window): a range in it.
    Returns:
        bool: True if "period" introduces the range.
    """
    return window.text.lower().startswith("period") or bool(
        _PERIOD_BEFORE.search(sentence[:window.span[0]]))


def pick_window(windows: list[Window], label: re.Match | None,
                sentence: str) -> tuple[Window | None, bool]:
    """Choose the sentence's window; also say whether the choice was clear.

    Several ranges in one sentence usually mean the inspection's own dates
    beside the period it covered ("conducted inspection from 12 to 22 Feb 2012
    … for the period April 1, 2010 to February 11, 2012"), patches of one
    investigation, or a table. Preference: ranges introduced by "period";
    among those, the one nearest the label (on a tie, the one before it, as in
    "from X to Y (hereinafter 'IP')"). Patches are merged into one span. The
    choice is clear when one range is left after preferring "period" ones.

    Args:
        windows (list[Window]): the sentence's ranges (inspection visits already removed).
        label (re.Match | None): the best label (None for a generic one).
        sentence (str): the normalised sentence.
    Returns:
        tuple[Window | None, bool]: the chosen range (patches merged), and whether the choice was clear.
    """
    if not windows:
        return None, False
    if len(windows) == 1:
        return windows[0], True
    if _PATCHES.search(sentence) and not any(w.rule.startswith("fy") for w in windows):
        first, last = min(windows, key=lambda w: w.start), max(windows, key=lambda w: w.end)
        return Window(first.start, last.end, first.start_precision, last.end_precision,
                      "patches_union", sentence[first.span[0]:last.span[1]],
                      (first.span[0], last.span[1])), False
    pool = [w for w in windows if period_introduced(sentence, w)] or windows
    if label is None:
        return pool[0], len(pool) == 1

    def distance(w: Window) -> tuple[int, int]:
        """How far a range is from the label (and on which side).

        Args:
            w (Window): a range.
        Returns:
            tuple[int, int]: (characters between them, 0 if before the label else 1).
        """
        s, e = w.span
        if e <= label.start():
            return label.start() - e, 0
        if s >= label.end():
            return s - label.end(), 1
        return 0, 0
    return min(pool, key=distance), len(pool) == 1


def confidence_for(kind: str, window: Window | None, sentence: str,
                   clear: bool = True) -> str | None:
    """Regex v1 confidence for a sentence's chosen range, or None if the range
    should not be taken as the window without a reader (or the model).

    An investigation word alone is not enough: in the corpus it also sits next
    to the dates the inspection was *carried out*, table rows, and certificate
    validity. Only a range introduced by "period" is taken on context alone,
    and a financial year only under an explicit label. A choice among several
    ranges that are all introduced by "period" (a table, or two periods in one
    sentence) is never ``high``.

    Args:
        kind (str): the label kind.
        window (Window | None): the chosen range.
        sentence (str): the normalised sentence.
        clear (bool): whether pick_window's choice was unambiguous.
    Returns:
        str | None: "high", "medium", or None (not a window without a reader or the model).
    """
    if window is None or kind == GENERIC:
        return None
    if kind == CONTEXT and (window.rule.startswith("fy") or not period_introduced(sentence, window)):
        return None
    if kind in (DEFINED, STRONG) and window.rule == "range" and clear:
        return "high"
    return "medium"


def score_sentence(sentence: str, rel_pos: float) -> dict | None:
    """Score one normalised sentence, or None if it is not a candidate.

    A candidate has at least one date and either a period keyword or an
    investigation-type word next to a date range.

    Args:
        sentence (str): a normalised sentence.
        rel_pos (float): its position in the order, 0–1.
    Returns:
        dict | None: score, label_kind, label_text, n_dates, n_ranges, negative, window (Window | None),
        confidence (str | None); None when the sentence is not a candidate.
    """
    if not _PREFILTER.search(sentence):
        return None
    dates = DATE.findall(sentence)
    found = find_windows(sentence)
    if not dates and not found:
        return None
    # Inspection visits are never the window; they can still close a covered
    # period that runs "till the date of inspection".
    visits = [w for w in found if inspection_visit(sentence, w)]
    windows = [w for w in found if w not in visits] + till_inspection(sentence, visits)
    labels = _labels(sentence)
    best = _best_kind(labels)
    if best is None:
        return None
    kind, label = best
    if kind in (CONTEXT, GENERIC) and not windows:
        # "investigation" or "within a period of two months" next to a lone
        # date is a letter or a deadline, not a window.
        return None
    negative = NEGATIVE.search(sentence)
    score = LABEL_WEIGHT[kind] + (RANGE_WEIGHT if windows else 0)
    if negative and kind not in (DEFINED, STRONG):
        score += NEGATIVE_WEIGHT
    if rel_pos < 0.25:
        score += EARLY_WEIGHT
    window, clear = pick_window(windows, label if kind != GENERIC else None, sentence)
    if window is not None:
        window = extend_with_fy_list(sentence, window)
    return {
        "score": score, "label_kind": kind, "label_text": label.group(0),
        "n_dates": len(dates), "n_ranges": len(windows),
        "negative": negative.group(0) if negative else None, "window": window,
        "confidence": confidence_for(kind, window, sentence, clear),
    }


# --------------------------------------------------------------------------
# Per-order extraction
# --------------------------------------------------------------------------

@dataclass
class Job:
    """One order to scan: the fields stage 4 needs from ``text_documents``.

    Attributes:
        text_stem (str), order_id (int | None), order_type (str | None), order_date (datetime.date | None),
        text_path (str | None), text_status (str).
    """
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    text_path: str | None
    text_status: str


@dataclass
class OrderWindow:
    """One order's regex v1 result: a row of ``derived/windows.parquet`` (columns in CLAUDE.md §8).

    Attributes:
        window_status (str): PARSED, UNCONFIRMED_RANGE, UNPARSED, NO_CANDIDATES or NO_TEXT;
        violation_start / violation_end (datetime.date | None) with their precisions;
        violation_raw (str | None): the source sentence; violation_confidence (str | None);
        violation_method ("regex" | None), violation_rule, cand_rank, char_start, rules_version, error.
    """
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    text_status: str
    n_sentences: int = 0
    n_candidates: int = 0
    window_status: str = NO_TEXT
    violation_start: dt.date | None = None
    violation_end: dt.date | None = None
    start_precision: str | None = None
    end_precision: str | None = None
    violation_raw: str | None = None
    violation_confidence: str | None = "low"
    violation_method: str | None = None
    violation_rule: str | None = None
    cand_rank: int | None = None
    char_start: int | None = None
    rules_version: str = RULES_VERSION
    error: str | None = None


def extract_candidates(job: Job, text: str, top_k: int = TOP_K) -> tuple[OrderWindow, list[Candidate]]:
    """Score every sentence of one order, keep the top k, and choose the window.

    Args:
        job (Job): the order.
        text (str): its text.
        top_k (int): candidates to keep.
    Returns:
        tuple[OrderWindow, list[Candidate]]: the order's window row and its top candidates.
    """
    stripped = strip_repeated_lines(text)
    spans = split_sentences(stripped)
    n = max(len(text), 1)
    scored = []
    for index, (s, e) in enumerate(spans):
        sentence = normalise_sentence(stripped[s:e])
        result = score_sentence(sentence, s / n)
        if result is not None:
            scored.append((index, s, e, sentence, result))

    result = OrderWindow(job.text_stem, job.order_id, job.order_type, job.order_date,
                         job.text_status, n_sentences=len(spans), n_candidates=len(scored))
    # Highest score first, earliest in the document on a tie.
    scored.sort(key=lambda row: (-row[4]["score"], row[1]))
    candidates = []
    for rank, (index, s, e, sentence, r) in enumerate(scored[:top_k], start=1):
        w: Window | None = r["window"]
        c = Candidate(
            text_stem=job.text_stem, order_id=job.order_id, order_type=job.order_type,
            order_date=job.order_date, cand_rank=rank, score=r["score"], sent_index=index,
            char_start=s, char_end=e, page_no=text.count(PAGE_SEPARATOR, 0, s) + 1,
            rel_pos=round(s / n, 4), sentence=sentence, label_kind=r["label_kind"],
            label_text=r["label_text"], n_dates=r["n_dates"], n_ranges=r["n_ranges"],
            negative=r["negative"],
        )
        if w is not None:
            c.range_text, c.parsed_start, c.parsed_end = normalise_sentence(w.text), w.start, w.end
            c.start_precision, c.end_precision, c.parse_rule = w.start_precision, w.end_precision, w.rule
            c.confidence = r["confidence"]
        candidates.append(c)

    if not candidates:
        result.window_status = NO_CANDIDATES
        return result, candidates
    chosen = next((c for c in candidates if c.confidence == "high"), None) \
        or next((c for c in candidates if c.confidence == "medium"), None)
    if chosen is None:
        has_range = any(c.parse_rule for c in candidates)
        result.window_status = UNCONFIRMED_RANGE if has_range else UNPARSED
        return result, candidates
    result.window_status = PARSED
    result.violation_start, result.violation_end = chosen.parsed_start, chosen.parsed_end
    result.start_precision, result.end_precision = chosen.start_precision, chosen.end_precision
    result.violation_raw = chosen.sentence
    result.violation_confidence = chosen.confidence
    result.violation_method = "regex"
    result.violation_rule = chosen.parse_rule
    result.cand_rank, result.char_start = chosen.cand_rank, chosen.char_start
    return result, candidates


def process(job: Job, repo_root: pathlib.Path = REPO_ROOT,
            top_k: int = TOP_K) -> tuple[OrderWindow, list[Candidate]]:
    """Read one order's text and extract its candidates. Never raises.

    Args:
        job (Job): the order.
        repo_root (pathlib.Path): base for job.text_path.
        top_k (int): candidates to keep.
    Returns:
        tuple[OrderWindow, list[Candidate]]: as extract_candidates (NO_TEXT without text).
    """
    if job.text_status != "OK" or not job.text_path:
        return OrderWindow(job.text_stem, job.order_id, job.order_type, job.order_date,
                           job.text_status, window_status=NO_TEXT), []
    try:
        text = (repo_root / job.text_path).read_text(encoding="utf-8")
        return extract_candidates(job, text, top_k)
    except Exception as exc:  # pragma: no cover - last-resort guard for a long run
        return OrderWindow(job.text_stem, job.order_id, job.order_type, job.order_date,
                           job.text_status, window_status=NO_TEXT,
                           error=f"{type(exc).__name__}: {exc}"[:300]), []


def _process_many(jobs: list[Job], repo_root: pathlib.Path, top_k: int):
    """Process a batch of orders (one worker task).

    Args:
        jobs (list[Job]): the batch.
        repo_root (pathlib.Path): base for text paths.
        top_k (int): candidates to keep.
    Returns:
        list[tuple[OrderWindow, list[Candidate]]]: one result per order.
    """
    return [process(job, repo_root, top_k) for job in jobs]


# --------------------------------------------------------------------------
# Input / output
# --------------------------------------------------------------------------

def load_jobs(path: pathlib.Path = TEXT_DOCUMENTS_IN) -> list[Job]:
    """Read the stage 3 table into the fields this stage needs.

    Args:
        path (pathlib.Path): ``derived/text_documents.parquet``.
    Returns:
        list[Job]: one per order.
    """
    rows = pq.read_table(path, columns=["text_stem", "order_id", "order_type", "order_date",
                                        "text_path", "status"]).to_pylist()
    return [Job(r["text_stem"], r["order_id"], r["order_type"], r["order_date"],
                r["text_path"], r["status"]) for r in rows]


def select(jobs: list[Job], *, categories: list[str] | None = None,
           order_ids: list[int] | None = None, limit: int | None = None,
           sample: int | None = None, seed: int = 7) -> list[Job]:
    """Filter jobs. ``sample`` draws a year-stratified random sample.

    Args:
        jobs (list[Job]): all orders.
        categories (list[str] | None): order types to keep.
        order_ids (list[int] | None): ids to keep.
        limit (int | None): keep the first N.
        sample (int | None): draw about N orders spread evenly over years.
        seed (int): the sample's random seed.
    Returns:
        list[Job]: the selected orders.
    """
    out = jobs
    if categories:
        out = [j for j in out if j.order_type in set(categories)]
    if order_ids:
        out = [j for j in out if j.order_id in set(order_ids)]
    if sample:
        rng = random.Random(seed)
        by_year: dict[int | None, list[Job]] = collections.defaultdict(list)
        for j in out:
            by_year[j.order_date.year if j.order_date else None].append(j)
        per_year = max(1, sample // max(1, len(by_year)))
        out = [j for y in sorted(by_year, key=lambda y: (y is None, y or 0))
               for j in rng.sample(by_year[y], min(per_year, len(by_year[y])))]
    if limit is not None:
        out = out[:limit]
    return out


CANDIDATES_SCHEMA = pa.schema([
    pa.field("text_stem", pa.string()),
    pa.field("order_id", pa.int64()),
    pa.field("order_type", pa.string()),
    pa.field("order_date", pa.date32()),
    pa.field("cand_rank", pa.int8()),
    pa.field("score", pa.int16()),
    pa.field("sent_index", pa.int32()),
    pa.field("char_start", pa.int64()),
    pa.field("char_end", pa.int64()),
    pa.field("page_no", pa.int32()),
    pa.field("rel_pos", pa.float64()),
    pa.field("sentence", pa.string()),
    pa.field("label_kind", pa.string()),
    pa.field("label_text", pa.string()),
    pa.field("n_dates", pa.int16()),
    pa.field("n_ranges", pa.int16()),
    pa.field("negative", pa.string()),
    pa.field("range_text", pa.string()),
    pa.field("parsed_start", pa.date32()),
    pa.field("parsed_end", pa.date32()),
    pa.field("start_precision", pa.string()),
    pa.field("end_precision", pa.string()),
    pa.field("parse_rule", pa.string()),
    pa.field("confidence", pa.string()),
])

WINDOWS_SCHEMA = pa.schema([
    pa.field("text_stem", pa.string()),
    pa.field("order_id", pa.int64()),
    pa.field("order_type", pa.string()),
    pa.field("order_date", pa.date32()),
    pa.field("text_status", pa.string()),
    pa.field("n_sentences", pa.int32()),
    pa.field("n_candidates", pa.int32()),
    pa.field("window_status", pa.string()),
    pa.field("violation_start", pa.date32()),
    pa.field("violation_end", pa.date32()),
    pa.field("start_precision", pa.string()),
    pa.field("end_precision", pa.string()),
    pa.field("violation_raw", pa.string()),
    pa.field("violation_confidence", pa.string()),
    pa.field("violation_method", pa.string()),
    pa.field("violation_rule", pa.string()),
    pa.field("cand_rank", pa.int8()),
    pa.field("char_start", pa.int64()),
    pa.field("rules_version", pa.string()),
    pa.field("error", pa.string()),
])


def _table(rows: list, schema: pa.Schema) -> pa.Table:
    """Dataclass rows → an Arrow table with a fixed schema.

    Args:
        rows (list): OrderWindow or Candidate rows.
        schema (pyarrow.Schema): WINDOWS_SCHEMA or CANDIDATES_SCHEMA.
    Returns:
        pyarrow.Table: the table.
    """
    columns = {name: [] for name in schema.names}
    for row in rows:
        payload = asdict(row)
        for name in columns:
            columns[name].append(payload[name])
    return pa.Table.from_pydict(columns, schema=schema)


def write_outputs(windows: list[OrderWindow], candidates: list[Candidate],
                  out_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Write ``windows.parquet`` and ``window_candidates.parquet`` atomically.

    Args:
        windows (list[OrderWindow]): order rows.
        candidates (list[Candidate]): candidate rows.
        out_dir (pathlib.Path): ``derived/`` or a scratch directory.
    Returns:
        tuple[pathlib.Path, pathlib.Path]: the two paths written.
    """
    assert_safe_output(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    windows = sorted(windows, key=lambda w: w.text_stem)
    candidates = sorted(candidates, key=lambda c: (c.text_stem, c.cand_rank))
    windows_path = out_dir / "windows.parquet"
    candidates_path = out_dir / "window_candidates.parquet"
    for table, path in ((_table(windows, WINDOWS_SCHEMA), windows_path),
                        (_table(candidates, CANDIDATES_SCHEMA), candidates_path)):
        tmp = path.parent / f"{path.name}.tmp-{os.urandom(6).hex()}"
        pq.write_table(table, tmp, compression="snappy")
        tmp.replace(path)  # atomic, so a reader never sees a half-written file
    return windows_path, candidates_path


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def run(jobs: list[Job], *, workers: int = 1, repo_root: pathlib.Path = REPO_ROOT,
        top_k: int = TOP_K, chunk: int = 200) -> tuple[list[OrderWindow], list[Candidate]]:
    """Extract every job. ``workers <= 1`` runs in-process (used by tests).

    Args:
        jobs (list[Job]): orders to scan.
        workers (int): processes.
        repo_root (pathlib.Path): base for text paths.
        top_k (int): candidates to keep per order.
        chunk (int): orders per worker task.
    Returns:
        tuple[list[OrderWindow], list[Candidate]]: every order row and candidate.
    """
    windows: list[OrderWindow] = []
    candidates: list[Candidate] = []
    if workers <= 1:
        for job in jobs:
            w, cs = process(job, repo_root, top_k)
            windows.append(w)
            candidates.extend(cs)
        return windows, candidates
    batches = [jobs[i:i + chunk] for i in range(0, len(jobs), chunk)]
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process_many, b, repo_root, top_k) for b in batches]
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            for w, cs in future.result():
                windows.append(w)
                candidates.extend(cs)
            if done % 20 == 0 or done == len(futures):
                log.info("%d/%d orders", len(windows), len(jobs))
    return windows, candidates


def summarise(windows: list[OrderWindow], candidates: list[Candidate]) -> dict:
    """Run summary for the log.

    Args:
        windows (list[OrderWindow]): order rows.
        candidates (list[Candidate]): candidate rows.
    Returns:
        dict: counts by window_status, confidence, rule, precision; regex share overall and by type.
    """
    with_text = [w for w in windows if w.window_status != NO_TEXT]
    parsed = [w for w in with_text if w.window_status == PARSED]
    by_type = collections.defaultdict(lambda: [0, 0])
    for w in with_text:
        by_type[w.order_type][1] += 1
        by_type[w.order_type][0] += w.window_status == PARSED
    return {
        "orders": len(windows),
        "orders_with_text": len(with_text),
        "window_status": dict(collections.Counter(w.window_status for w in windows)),
        "confidence": dict(collections.Counter(w.violation_confidence for w in parsed)),
        "rule": dict(collections.Counter(w.violation_rule for w in parsed)),
        "precision_start_end": dict(collections.Counter(
            f"{w.start_precision}/{w.end_precision}" for w in parsed)),
        "regex_share_high_or_medium": round(len(parsed) / len(with_text), 4) if with_text else None,
        "regex_share_by_type": {k: f"{p}/{n} = {p / n:.1%}" for k, (p, n) in sorted(by_type.items(), key=lambda kv: str(kv[0]))},
        "candidates_written": len(candidates),
        "candidate_label_kinds": dict(collections.Counter(c.label_kind for c in candidates)),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.extract_windows``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.extract_windows",
        description="Violation-window candidate sentences and regex v1 (runbook Week 2, Day 1)",
    )
    parser.add_argument("--documents", type=pathlib.Path, default=TEXT_DOCUMENTS_IN,
                        help="text-stage output to read (default: derived/text_documents.parquet)")
    parser.add_argument("--category", action="append", choices=["WTM", "SETTLEMENT", "AO", "ED_CGM"])
    parser.add_argument("--order-id", action="append", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sample", type=int, help="year-stratified random sample of N orders")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: write both parquets and the log under DIR instead of derived/")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run stage 4: scan every selected order and write the two tables.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0.
    """
    args = build_arg_parser().parse_args(argv)
    if args.scratch:
        out_dir, log_file = args.scratch, args.scratch / "extract_windows.log"
    else:
        out_dir, log_file = DERIVED_DIR, LOGS_DIR / "extract_windows.log"
    for path in (out_dir, log_file):
        assert_safe_output(path)
    setup_logging(args.log_level, log_file)

    jobs = select(load_jobs(args.documents), categories=args.category,
                  order_ids=args.order_id, limit=args.limit, sample=args.sample)
    log.info("%s: scanning %d orders with %d worker(s) -> %s",
             RULES_VERSION, len(jobs), args.workers, out_dir)
    started = time.monotonic()
    windows, candidates = run(jobs, workers=args.workers, top_k=args.top_k)
    windows_path, candidates_path = write_outputs(windows, candidates, out_dir)

    log.info("=" * 72)
    for key, value in summarise(windows, candidates).items():
        log.info("%-28s %s", key, value)
    log.info("elapsed %.1f min", (time.monotonic() - started) / 60)
    log.info("wrote %s and %s", windows_path, candidates_path)
    log.info("regex share is before the Day 3 sanity rules; it is not the extraction rate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
