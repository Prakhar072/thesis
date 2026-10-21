"""Week 2, Day 2: model fallback for the windows regex v1 could not settle.

Reads ``derived/windows.parquet`` and ``derived/window_candidates.parquet``
(both from ``extract_windows``) and sends each ``UNCONFIRMED_RANGE`` or
``UNPARSED`` order's candidate sentences (at most ``TOP_K``) to a local model.
Writes

* ``derived/window_model.parquet``  — one row per order sent: what the model
  said, whether it checked out, and why not;
* ``derived/violation_windows.parquet`` — one row per order: the regex window
  where regex v1 found one, else the verified model window. This is the table
  the Day 3 sanity rules read.

The model judges; the code does everything checkable
-----------------------------------------------------
The model is asked *which* sentence states the period, what kind of period it
is, and to **copy** the start and end dates exactly as written. It never
produces a date of its own. The code then

1. checks both quotes occur in the sentence (letters and digits compared, so
   "April 01 ,2014" matches "April 01, 2014") — a quote that is not there is a
   hallucination and the answer is rejected (``MODEL_UNVERIFIED``);
2. reads the quotes with the same day-first grammar as regex v1, extended to
   bare years ("2008 to 2013") and financial years ("FY 2011-12"); a start
   without a year takes the end's year;
3. rejects a start after its end, and a quote that is only an end
   ("till March 2014").

So day/month inversion cannot come from the model, and every accepted window
carries the sentence it was read from (``violation_raw``, rule 2).

What is being measured
----------------------
One quantity: the suspected violation window, i.e. the period of business
activity SEBI looked at. The order may call it the investigation,
examination, inspection or relevant period — alternate names for the same
thing — and SEBI's usual "conducted investigation during the period X to Y"
means that activity period too. It is never the time the probe itself took;
the only place those dates appear as a range is an on-site inspection visit,
which ``extract_windows.inspection_visit`` screens out on both paths.

``window_kind`` records only *how the order states it*, not a different
quantity:

``stated_period``  the order names the period (any of the four labels), or
                   says SEBI investigated trading during it. All regex
                   windows are this kind.
``conduct_dates``  no such period is named, but the sentence gives when the
                   alleged misconduct took place (manipulative trades,
                   non-disclosure, collecting fees without registration,
                   trading on UPSI).

The model answers in its own words (``investigation`` / ``violation`` /
``none``, kept in ``raw_response``); ``KIND_NAMES`` maps them. A model "none"
is not evidence the *order* states no period — the model only sees the
candidate sentences — so it stays ``violation_confidence = low``.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import json
import logging
import os
import pathlib
import random
import re
import time
from dataclasses import asdict, dataclass

import pyarrow as pa
import pyarrow.parquet as pq

from src import cache, llm
from src import extract_windows as ew
from src.extract_text import assert_safe_output, setup_logging

log = logging.getLogger("window_model")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
WINDOWS_IN = DERIVED_DIR / "windows.parquet"
CANDIDATES_IN = DERIVED_DIR / "window_candidates.parquet"
LOGS_DIR = REPO_ROOT / "logs"

DEFAULT_MODEL = "qwen3.5:4b"
PROMPT_VERSION = "wm-v1"
#: Generation settings. Greedy decoding and a fixed seed make a first call as
#: repeatable as the server allows; the cache makes every later call exact.
OPTIONS = {"temperature": 0, "seed": 7, "num_ctx": 8192}
MODEL_STATUSES = (ew.UNCONFIRMED_RANGE, ew.UNPARSED)
#: Sentences are sent as extracted; a table flattened into one sentence is
#: clipped, since the period, when there is one, is stated in running text.
MAX_SENTENCE_CHARS = 1500

# model_status values
MODEL_WINDOW = "MODEL_WINDOW"          # verified and read
MODEL_NONE = "MODEL_NONE"              # the model found no period
MODEL_UNVERIFIED = "MODEL_UNVERIFIED"  # quotes not in the sentence, or unreadable dates
MODEL_REJECTED = "MODEL_REJECTED"      # verified quotes, but a known non-window (see false_window_reason)
MODEL_ERROR = "MODEL_ERROR"            # no usable response

INVESTIGATION, VIOLATION, NONE = "investigation", "violation", "none"  # the model's enum
STATED_PERIOD, CONDUCT_DATES = "stated_period", "conduct_dates"          # stored window_kind
KIND_NAMES = {INVESTIGATION: STATED_PERIOD, VIOLATION: CONDUCT_DATES}

SYSTEM = """\
You read sentences taken from an order of the Securities and Exchange Board of India (SEBI) \
and find the period the order is about. Reply with JSON only.

Decide the kind of period:
- "investigation": a sentence gives the period that SEBI or a stock exchange investigated, \
examined, inspected or enquired into, or calls a period the investigation, examination, \
inspection or relevant period. A sentence saying SEBI investigated or examined trading or \
dealings in a scrip during or for some dates also counts.
- "violation": no such period is given, but a sentence gives the dates during which the \
alleged violation or misconduct happened (for example manipulative or artificial trades, \
failure to make disclosures, collecting money or giving advice without registration, trading \
while in possession of unpublished price sensitive information).
- "none": neither is given.

These are never the period, so answer "none" if they are all you find: dates of letters, \
notices, summons, emails, hearings or orders; periods of bank statements, demat statements, \
ledgers or data that were requested or analysed; the dates on which an inspection was carried \
out; offer, issue, subscription or scheme periods; how long a director held office; financial \
results or quarters reported; interest calculations; rows of a table.

Copy start_text and end_text exactly as they are written in the sentence, for example \
"April 01, 2014" or "01.04.2014". For a single day or a single month, give the same text for \
both. If the period is given in parts (patches, first and second period), start_text is the \
earliest start and end_text the latest end. Give the number of the sentence you used; \
0 and null dates when the kind is "none"."""

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": [INVESTIGATION, VIOLATION, NONE]},
        "sentence": {"type": "integer"},
        "start_text": {"type": ["string", "null"]},
        "end_text": {"type": ["string", "null"]},
    },
    "required": ["kind", "sentence", "start_text", "end_text"],
}


def build_user(sentences: list[str]) -> str:
    """The user prompt: the order's candidate sentences, numbered, long ones clipped.

    Args:
        sentences (list[str]): the candidate sentences, best first.
    Returns:
        str: "Sentences from the order:" followed by "[1] …", "[2] …".
    """
    lines = ["Sentences from the order:"]
    for i, s in enumerate(sentences, start=1):
        clipped = s if len(s) <= MAX_SENTENCE_CHARS else s[:MAX_SENTENCE_CHARS] + " …"
        lines.append(f"[{i}] {clipped}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Checking and reading the model's answer
# --------------------------------------------------------------------------

_LEADING_ZEROS = re.compile(r"(?<![0-9])0+(?=[0-9])")


def _alnum(text: str) -> str:
    """Letters and digits only, leading zeros dropped: "April 01, 2018" and
    "April 1 ,2018" compare equal.

    Args:
        text (str): any text.
    Returns:
        str: lower-case letters and digits only, leading zeros dropped.
    """
    return re.sub(r"[^0-9a-z]", "", _LEADING_ZEROS.sub("", text.lower()))


def quoted_in(quote: str, sentence: str) -> bool:
    """Does the quote occur in the sentence, ignoring spacing and punctuation?

    Args:
        quote (str): the model's quoted date.
        sentence (str): the sentence it claims to quote.
    Returns:
        bool: True if the quote occurs in it.
    """
    q = _alnum(quote)
    return bool(q) and q in _alnum(sentence)


@dataclass(frozen=True)
class Resolved:
    """A quoted date read into a span.

    Attributes:
        start (datetime.date | None), end (datetime.date | None): the span it covers; None without a year.
        precision (str): day, month, year or fy.
        month (int | None), day (int | None): kept for a partial date that takes the end's year later.
    """
    start: dt.date | None  # None when the year is missing (partial)
    end: dt.date | None
    precision: str         # day | month | year | fy
    month: int | None = None
    day: int | None = None


_YEAR_ONLY = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_FY = re.compile(r"(?:F\.?\s?Y\.?|financial\s+years?)?\s*['’]?\s*((?:19|20)\d{2})\s*[-–/]\s*(\d{2}|\d{4})(?!\d)", re.I)
#: OCR debris and wrapping punctuation that can sit inside a quoted date.
_JUNK = re.compile(r"[|()\[\]{}\"“”‘’`*_]")


def _fy(m: re.Match) -> Resolved | None:
    """A financial-year match as a span (1 April – 31 March).

    Args:
        m (re.Match): a match of _FY.
    Returns:
        Resolved | None: the span, or None if the years are not consecutive.
    """
    y1, second = int(m.group(1)), m.group(2)
    y2 = int(second) if len(second) == 4 else (y1 // 100) * 100 + int(second)
    if len(second) == 2 and y2 < y1:
        y2 += 100
    return Resolved(dt.date(y1, 4, 1), dt.date(y2, 3, 31), "fy") if y2 == y1 + 1 else None


#: A day range inside one quote: "January 21-25, 2008", "May 8–9, 2014",
#: "February 14 - 15 of 2014", "21-25 January 2008".
_DAY_SPAN = re.compile(
    rf"(?:(?P<m1>{ew._MON})\s*(?P<a1>\d{{1,2}}){ew._ORD}\s*[-–]\s*(?P<b1>\d{{1,2}}){ew._ORD}\s*,?\s*(?:of\s+)?(?P<y1>(?:19|20)\d{{2}})"
    rf"|(?P<a2>\d{{1,2}}){ew._ORD}\s*[-–]\s*(?P<b2>\d{{1,2}}){ew._ORD}\s+(?:of\s+)?(?P<m2>{ew._MON})\s*,?\s*(?P<y2>(?:19|20)\d{{2}}))",
    re.IGNORECASE)
#: A span of years: "2003-05", "2009-16", "2013-2017" (a bare "2011-12" is a
#: financial year and is read by _FY).
_YEAR_SPAN = re.compile(r"((?:19|20)\d{2})\s*[-–]\s*(\d{2}|(?:19|20)\d{2})")
#: What may surround bare years for a quote to count as years only.
_YEAR_WORDS = re.compile(r"\b(?:the|calendar|financial|years?|and|to|from|till|upto|between|of|i\.?\s?e\.?)\b|[\s,.;:&\-–/]", re.I)


def _day_span(t: str) -> Resolved | None:
    """A day range written as one date, e.g. “January 21-25, 2008”.

    Args:
        t (str): the cleaned quote.
    Returns:
        Resolved | None: the span, or None if the quote is not one.
    """
    m = _DAY_SPAN.fullmatch(t)
    if not m:
        return None
    mon = ew.MONTHS.get((m.group("m1") or m.group("m2")).lower().rstrip("."))
    y = int(m.group("y1") or m.group("y2"))
    a, b = int(m.group("a1") or m.group("a2")), int(m.group("b1") or m.group("b2"))
    try:
        start, end = dt.date(y, mon, a), dt.date(y, mon, b)
    except (TypeError, ValueError):
        return None
    return Resolved(start, end, "day") if start <= end else None


def _year_span(t: str) -> Resolved | None:
    """A span of years written as one token, e.g. “2003-05” (not a financial year).

    Args:
        t (str): the cleaned quote.
    Returns:
        Resolved | None: 1 January of the first year to 31 December of the last.
    """
    m = _YEAR_SPAN.fullmatch(t)
    if not m:
        return None
    y1, second = int(m.group(1)), m.group(2)
    y2 = int(second) if len(second) == 4 else (y1 // 100) * 100 + int(second)
    if y2 <= y1 + 1:
        return None  # "2011-12" is a financial year; handled elsewhere
    return Resolved(dt.date(y1, 1, 1), dt.date(y2, 12, 31), "year")


def resolve_date(text: str, which: str = "start") -> Resolved | None:
    """Read one quoted date: a day, a month, a financial year, a year, or a
    short span written as one ("January 21-25, 2008", "2003-05").

    A quote is normally one date. A quoted range ("May 3 to May 20, 2004",
    "May-June 1998") is read with the regex range grammar. When the model
    quotes a whole phrase
    ("financial years 2000-2001, 2001-2002 and April 2002 till May 21, 2002"),
    the start is read from its first date and the end from its last. A quote
    is read as bare years only when it holds nothing else ("the year 1992",
    "2003 to 2014"); a malformed date ("31/04/2003", "Match 30, 2000") is
    unreadable rather than silently widened to its whole year.

    Args:
        text (str): the model's quote.
        which (str): "start" or "end" — which end of a multi-date quote to take.
    Returns:
        Resolved | None: the date span, or None if the quote is unreadable.
    """
    t = ew.normalise_sentence(_JUNK.sub(" ", text)).strip(" ,.;:-")
    whole = ew.parse_date(t)
    if whole is None:
        if (r := _day_span(t) or _year_span(t)) is not None:
            return r
        # Everything readable in the quote, in order: whole ranges ("May 3 to
        # May 20, 2004", "May-June 1998", read with the regex grammar), single
        # dates, financial years. The start comes from the first, the end from
        # the last ("financial years 2000-2001, 2001-2002 and April 2002 till
        # May 21, 2002" runs from April 2000 to May 21, 2002).
        found: list[tuple[int, Resolved, Resolved]] = []
        taken: list[tuple[int, int]] = []
        for w in ew.find_windows(t):
            if w.rule.startswith("range"):
                found.append((w.span[0], Resolved(w.start, w.end, w.start_precision),
                              Resolved(w.start, w.end, w.end_precision)))
                taken.append(w.span)

        def free(a: int, b: int) -> bool:
            """Whether a span of the quote is not yet taken by an earlier match.

            Args:
                a (int), b (int): the span.
            Returns:
                bool: True if no taken span overlaps it.
            """
            return not any(s0 < b and a < e0 for s0, e0 in taken)
        for m in ew.DATE.finditer(t):
            if free(*m.span()) and (p := ew.parse_date(m.group(0))) is not None and p.year is not None:
                r = Resolved(ew.as_start(p), ew.as_end(p), p.precision)
                found.append((m.start(), r, r))
                taken.append(m.span())
        for m in _FY.finditer(t):
            if free(*m.span()) and (r := _fy(m)):
                found.append((m.start(), r, r))
        if found:
            found.sort(key=lambda item: item[0])
            return found[0][1] if which == "start" else found[-1][2]
        years = _YEAR_ONLY.findall(t)
        if years and not _YEAR_WORDS.sub("", _YEAR_ONLY.sub("", t)):
            y = int(years[0] if which == "start" else years[-1])
            return Resolved(dt.date(y, 1, 1), dt.date(y, 12, 31), "year")
        if not _YEAR_ONLY.search(t) and (partial := re.fullmatch(ew._PARTIAL, t, re.I)) is not None:
            whole = ew.parse_date(partial.group(0))  # "November" in "from November to February 2012"
    if whole is None:
        return None
    if whole.year is None:
        return Resolved(None, None, "day" if whole.day else "month", whole.month, whole.day)
    return Resolved(ew.as_start(whole), ew.as_end(whole), whole.precision)


_OPEN_ENDED = re.compile(r"\s*(?:till|until|upto|up\s+to|by|before|after|since|as\s+on|as\s+at)\b", re.I)


def _with_year(r: Resolved, year: int) -> Resolved | None:
    """Give a partial date (no year) a year.

    Args:
        r (Resolved): a partial date (month and maybe day).
        year (int): the year to use.
    Returns:
        Resolved | None: the dated span, or None if invalid (29 February in a non-leap year).
    """
    p = ew.ParsedDate(year, r.month, r.day)
    try:
        return Resolved(ew.as_start(p), ew.as_end(p), r.precision, r.month, r.day)
    except ValueError:
        return None


_DATED = re.compile(r"\b(?:dated|dt\.?)\s*$", re.I)
#: A statement or ledger is a document; its period is not the activity. Bank
#: *credits* or *transactions* over a period can be the activity itself (fees
#: collected without registration), so "bank" alone does not trigger this.
_STATEMENTS = re.compile(r"\b(?:statements?|ledgers?)\b", re.I)
#: "5/11/1999 15:02:15 25000 742.00": a date in a row of an order or trade log.
_CLOCK = re.compile(r"\s*\d{1,2}:\d{2}(?::\d{2})?\b")


def _locate(sentence: str, quote: str) -> int | None:
    """Character offset of ``quote`` in ``sentence`` (spacing-insensitive).

    Args:
        sentence (str): the sentence.
        quote (str): a quoted date.
    Returns:
        int | None: the offset where it starts, or None.
    """
    flat = re.escape(ew.normalise_sentence(quote)).replace(r"\ ", r"\s*")
    m = re.search(flat, sentence, re.I)
    return m.start() if m else None


def false_window_reason(sentence: str, start_text: str, end_text: str,
                        start: dt.date, end: dt.date) -> str | None:
    """Patterns the model mistakes for the window, caught in code:

    * "vide email dated August 27, 2021, August 28, 2021 …" — dates of
      correspondence, turned into a range;
    * "SEBI conducted an inspection of the Company during the period February
      28, 2005 to March 5, 2005" — an inspection visit (the same test as the
      regex path, ``extract_windows.inspection_visit``). SEBI's "conducted
      investigation during the period X to Y" is the activity period and is
      *not* caught;
    * "Bank account statement … for the period January 01, 2016 to …" —
      the period of a document that was requested or analysed.

    Args:
        sentence (str): the sentence the quotes came from.
        start_text (str), end_text (str): the model's quotes.
        start (datetime.date), end (datetime.date): the dates read from them.
    Returns:
        str | None: why this is not the window (correspondence, inspection visit, statement period, trade-log
        row), or None if it may be.
    """
    at = _locate(sentence, start_text)
    if at is None:
        return None
    before = sentence[max(0, at - 80):at]
    if _DATED.search(before):
        return "dates of correspondence"
    end_at = _locate(sentence[at:], end_text)
    stop = at + (end_at or 0) + len(ew.normalise_sentence(end_text)) if end_at is not None else at + len(start_text)
    window = ew.Window(start, end, "day", "day", "model", sentence[at:stop], (at, stop))
    if ew.inspection_visit(sentence, window):
        return "dates of an inspection visit"
    if _STATEMENTS.search(before) and not re.search(r"investigat|inspect|examin", before, re.I):
        return "period of a statement or ledger"
    if _CLOCK.match(sentence, at + len(start_text)) or (
            end_at is not None and _CLOCK.match(sentence, stop)):
        return "row of a trade log"
    return None


@dataclass
class Reading:
    """The model's answer after the checks.

    Attributes:
        status (str): MODEL_WINDOW, MODEL_NONE, MODEL_UNVERIFIED, MODEL_REJECTED or MODEL_ERROR.
        kind (str | None): the model's own word (investigation / violation / none).
        sentence_no (int | None), start_text (str | None), end_text (str | None): what it quoted.
        start, end (datetime.date | None), start_precision, end_precision (str | None): the dates read.
        note (str | None): why it was not accepted.
    """
    status: str
    kind: str | None = None
    sentence_no: int | None = None
    start_text: str | None = None
    end_text: str | None = None
    start: dt.date | None = None
    end: dt.date | None = None
    start_precision: str | None = None
    end_precision: str | None = None
    note: str | None = None


def read_answer(content: str, sentences: list[str]) -> Reading:
    """Check the model's JSON against the sentences and turn it into dates.

    Args:
        content (str): the model's JSON answer.
        sentences (list[str]): the sentences it was shown.
    Returns:
        Reading: the checked reading (status says whether a window was accepted).
    """
    try:
        ans = json.loads(content)
        kind = ans["kind"]
        number = int(ans["sentence"] or 0)
        start_text, end_text = ans["start_text"], ans["end_text"]
    except (ValueError, TypeError, KeyError) as exc:
        return Reading(MODEL_ERROR, note=f"bad JSON: {exc}"[:200])
    r = Reading(MODEL_NONE, kind=kind, sentence_no=number or None,
                start_text=start_text, end_text=end_text)
    if kind not in (INVESTIGATION, VIOLATION):
        return r
    if not start_text or not end_text:
        r.status, r.note = MODEL_UNVERIFIED, "a date is missing"
        return r
    # The quotes must be in the sentence the model named; if it named the
    # wrong one, accept a single other sentence that holds both.
    pool = [number] if 1 <= number <= len(sentences) else []
    pool += [i for i in range(1, len(sentences) + 1) if i not in pool]
    found = next((i for i in pool if quoted_in(start_text, sentences[i - 1])
                  and quoted_in(end_text, sentences[i - 1])), None)
    if found is None:
        r.status, r.note = MODEL_UNVERIFIED, "quoted dates not found in the sentences"
        return r
    r.sentence_no = found
    if _OPEN_ENDED.match(start_text) or _OPEN_ENDED.match(end_text):
        # "till March 2014" quoted as both ends: an end alone, not a period.
        r.status, r.note = MODEL_UNVERIFIED, "open-ended quote"
        return r
    a, b = resolve_date(start_text, "start"), resolve_date(end_text, "end")
    if a is None or b is None:
        r.status, r.note = MODEL_UNVERIFIED, "quoted dates not readable"
        return r
    if b.end is None:
        r.status, r.note = MODEL_UNVERIFIED, "end date has no year"
        return r
    if a.start is None:
        a = _with_year(a, b.end.year)
        if a is None:
            r.status, r.note = MODEL_UNVERIFIED, "start date not valid in the end's year"
            return r
        if a.start > b.end:
            a = _with_year(a, b.end.year - 1) or a
    if a.start > b.end:
        r.status, r.note = MODEL_UNVERIFIED, "start after end"
        return r
    if (reason := false_window_reason(sentences[found - 1], start_text, end_text,
                                      a.start, b.end)) is not None:
        r.status, r.note = MODEL_REJECTED, reason
        return r
    r.status = MODEL_WINDOW
    r.start, r.end = a.start, b.end
    r.start_precision, r.end_precision = a.precision, b.precision
    return r


# --------------------------------------------------------------------------
# Per order
# --------------------------------------------------------------------------

@dataclass
class ModelRow:
    """One order sent to the model: a row of ``derived/window_model.parquet`` (columns in CLAUDE.md §8).

    Attributes:
        text_stem (str), regex_status (str), n_sentences_sent (int), model, model_digest, prompt_version (str),
        think (bool), cache_key (str | None), raw_response (str | None), model_status (str),
        window_kind (str | None): stated_period or conduct_dates, the checked dates and quotes,
        violation_raw (str | None), note (str | None), seconds (float | None), from_cache (bool | None).
    """
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    regex_status: str
    n_sentences_sent: int
    model: str
    model_digest: str
    prompt_version: str
    think: bool
    cache_key: str | None = None
    raw_response: str | None = None
    model_status: str = MODEL_ERROR
    window_kind: str | None = None
    sentence_no: int | None = None
    start_text: str | None = None
    end_text: str | None = None
    violation_start: dt.date | None = None
    violation_end: dt.date | None = None
    start_precision: str | None = None
    end_precision: str | None = None
    violation_raw: str | None = None
    note: str | None = None
    seconds: float | None = None
    from_cache: bool | None = None


@dataclass
class Order:
    """An order to send: its identity and candidate sentences.

    Attributes:
        text_stem (str), order_id (int | None), order_type (str | None), order_date (datetime.date | None),
        regex_status (str): UNCONFIRMED_RANGE or UNPARSED, sentences (list[str]).
    """
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    regex_status: str
    sentences: list[str]


def ask(order: Order, spec: llm.ModelSpec, root: pathlib.Path | None = None) -> ModelRow:
    """Ask the model about one order. Never raises.

    Args:
        order (Order): the order and its sentences.
        spec (llm.ModelSpec): the model.
        root (pathlib.Path | None): the model cache root (scratch runs).
    Returns:
        ModelRow: the answer, checked; errors become MODEL_ERROR rows.
    """
    row = ModelRow(order.text_stem, order.order_id, order.order_type, order.order_date,
                   order.regex_status, len(order.sentences), spec.name, spec.digest,
                   PROMPT_VERSION, spec.think)
    user = build_user(order.sentences)
    try:
        record = llm.chat_json(spec, SYSTEM, user, SCHEMA, root=root)
    except llm.LlmError as exc:
        row.note = str(exc)[:300]
        return row
    row.cache_key, row.raw_response = record["key"], record["content"]
    row.seconds, row.from_cache = record.get("seconds"), record["from_cache"]
    reading = read_answer(record["content"], order.sentences)
    row.model_status, row.window_kind, row.note = reading.status, KIND_NAMES.get(reading.kind), reading.note
    row.sentence_no, row.start_text, row.end_text = reading.sentence_no, reading.start_text, reading.end_text
    if reading.status == MODEL_WINDOW:
        row.violation_start, row.violation_end = reading.start, reading.end
        row.start_precision, row.end_precision = reading.start_precision, reading.end_precision
        row.violation_raw = order.sentences[reading.sentence_no - 1]
    return row


# --------------------------------------------------------------------------
# Input / output
# --------------------------------------------------------------------------

def load_orders(windows_path: pathlib.Path, candidates_path: pathlib.Path, *,
                statuses: tuple[str, ...] = MODEL_STATUSES) -> tuple[list[dict], list[Order]]:
    """All regex rows, plus the orders to send to the model with their sentences.

    Args:
        windows_path (pathlib.Path): ``derived/windows.parquet``.
        candidates_path (pathlib.Path): ``derived/window_candidates.parquet``.
        statuses (tuple[str, ...]): which regex statuses go to the model.
    Returns:
        tuple[list[dict], list[Order]]: every regex row (dicts), and the orders to send.
    """
    windows = pq.read_table(windows_path).to_pylist()
    cands = pq.read_table(candidates_path, columns=["text_stem", "cand_rank", "sentence"]).to_pylist()
    by_stem: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    for c in cands:
        by_stem[c["text_stem"]].append((c["cand_rank"], c["sentence"]))
    orders = [
        Order(w["text_stem"], w["order_id"], w["order_type"], w["order_date"], w["window_status"],
              [s for _, s in sorted(by_stem[w["text_stem"]])])
        for w in windows if w["window_status"] in statuses and by_stem.get(w["text_stem"])
    ]
    return windows, orders


def select(orders: list[Order], *, order_ids: list[int] | None = None,
           stems: list[str] | None = None, sample: int | None = None,
           limit: int | None = None, seed: int = 7) -> list[Order]:
    """Restrict the orders to send (ids, stems, a random sample, the first N).

    Args:
        orders (list[Order]): all orders to send.
        order_ids (list[int] | None), stems (list[str] | None): keep only these.
        sample (int | None): a random sample of this size.
        limit (int | None): the first N.
        seed (int): the sample's random seed.
    Returns:
        list[Order]: the selection.
    """
    out = orders
    if order_ids:
        out = [o for o in out if o.order_id in set(order_ids)]
    if stems:
        out = [o for o in out if o.text_stem in set(stems)]
    if sample:
        out = random.Random(seed).sample(out, min(sample, len(out)))
    if limit is not None:
        out = out[:limit]
    return out


def combine(windows: list[dict], model_rows: list[ModelRow]) -> list[dict]:
    """One row per order: the regex window if regex found one, else the
    verified model window. Regex windows are all ``stated_period``.

    Args:
        windows (list[dict]): every regex row (from load_orders).
        model_rows (list[ModelRow]): the model's answers.
    Returns:
        list[dict]: one row per order with COMBINED_SCHEMA's columns (``violation_windows``).
    """
    by_stem = {m.text_stem: m for m in model_rows}
    out = []
    for w in windows:
        row = {k: w[k] for k in ("text_stem", "order_id", "order_type", "order_date",
                                 "text_status", "window_status", "violation_start",
                                 "violation_end", "start_precision", "end_precision",
                                 "violation_raw", "violation_confidence", "violation_method",
                                 "violation_rule", "char_start")}
        row["window_kind"] = STATED_PERIOD if w["window_status"] == ew.PARSED else None
        m = by_stem.get(w["text_stem"])
        row["model_status"] = m.model_status if m else None
        if m and m.model_status == MODEL_WINDOW:
            row.update(violation_start=m.violation_start, violation_end=m.violation_end,
                       start_precision=m.start_precision, end_precision=m.end_precision,
                       violation_raw=m.violation_raw, violation_confidence="medium",
                       violation_method="model", violation_rule=f"model:{m.window_kind}",
                       window_kind=m.window_kind, char_start=None)
        out.append(row)
    return out


MODEL_SCHEMA = pa.schema([
    pa.field("text_stem", pa.string()),
    pa.field("order_id", pa.int64()),
    pa.field("order_type", pa.string()),
    pa.field("order_date", pa.date32()),
    pa.field("regex_status", pa.string()),
    pa.field("n_sentences_sent", pa.int8()),
    pa.field("model", pa.string()),
    pa.field("model_digest", pa.string()),
    pa.field("prompt_version", pa.string()),
    pa.field("think", pa.bool_()),
    pa.field("cache_key", pa.string()),
    pa.field("raw_response", pa.string()),
    pa.field("model_status", pa.string()),
    pa.field("window_kind", pa.string()),
    pa.field("sentence_no", pa.int8()),
    pa.field("start_text", pa.string()),
    pa.field("end_text", pa.string()),
    pa.field("violation_start", pa.date32()),
    pa.field("violation_end", pa.date32()),
    pa.field("start_precision", pa.string()),
    pa.field("end_precision", pa.string()),
    pa.field("violation_raw", pa.string()),
    pa.field("note", pa.string()),
    pa.field("seconds", pa.float64()),
    pa.field("from_cache", pa.bool_()),
])

COMBINED_SCHEMA = pa.schema([
    pa.field("text_stem", pa.string()),
    pa.field("order_id", pa.int64()),
    pa.field("order_type", pa.string()),
    pa.field("order_date", pa.date32()),
    pa.field("text_status", pa.string()),
    pa.field("window_status", pa.string()),
    pa.field("model_status", pa.string()),
    pa.field("violation_start", pa.date32()),
    pa.field("violation_end", pa.date32()),
    pa.field("start_precision", pa.string()),
    pa.field("end_precision", pa.string()),
    pa.field("violation_raw", pa.string()),
    pa.field("violation_confidence", pa.string()),
    pa.field("violation_method", pa.string()),
    pa.field("violation_rule", pa.string()),
    pa.field("window_kind", pa.string()),
    pa.field("char_start", pa.int64()),
])


def _write(table: pa.Table, path: pathlib.Path) -> None:
    """Write a Parquet table atomically (temporary file + rename).

    Args:
        table (pyarrow.Table): the table.
        path (pathlib.Path): the destination.
    Returns:
        None.
    """
    tmp = path.parent / f"{path.name}.tmp-{os.urandom(6).hex()}"
    pq.write_table(table, tmp, compression="snappy")
    tmp.replace(path)  # atomic


def write_outputs(windows: list[dict], model_rows: list[ModelRow], out_dir: pathlib.Path,
                  *, combined: bool = True) -> list[pathlib.Path]:
    """Write ``window_model.parquet`` and, for a full run, ``violation_windows.parquet``.

    Args:
        windows (list[dict]): every regex row.
        model_rows (list[ModelRow]): the model's answers.
        out_dir (pathlib.Path): ``derived/`` or a scratch directory.
        combined (bool): also write the combined table.
    Returns:
        list[pathlib.Path]: the paths written.
    """
    assert_safe_output(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(model_rows, key=lambda r: r.text_stem)
    model_table = pa.Table.from_pylist([{k: asdict(r)[k] for k in MODEL_SCHEMA.names} for r in rows],
                                       schema=MODEL_SCHEMA)
    paths = [out_dir / "window_model.parquet"]
    _write(model_table, paths[0])
    if combined:
        merged = sorted(combine(windows, model_rows), key=lambda r: r["text_stem"])
        paths.append(out_dir / "violation_windows.parquet")
        _write(pa.Table.from_pylist([{k: r[k] for k in COMBINED_SCHEMA.names} for r in merged],
                                    schema=COMBINED_SCHEMA), paths[1])
    return paths


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def run(orders: list[Order], spec: llm.ModelSpec, *, workers: int = 1,
        root: pathlib.Path | None = None, progress_every: int = 100) -> list[ModelRow]:
    """Ask about every order. Threads, not processes: the work is waiting on
    the model server, which queues requests beyond its own parallelism.

    Args:
        orders (list[Order]): orders to ask about.
        spec (llm.ModelSpec): the model.
        workers (int): concurrent requests (the local server answers one at a time).
        root (pathlib.Path | None): model cache root.
        progress_every (int): log every N orders.
    Returns:
        list[ModelRow]: one row per order (completion order).
    """
    rows: list[ModelRow] = []
    started = time.monotonic()

    def collect(row: ModelRow) -> None:
        """Gather one answer and log progress.

        Args:
            row (ModelRow): one order's answer.
        Returns:
            None.
        """
        rows.append(row)
        n = len(rows)
        if progress_every and (n % progress_every == 0 or n == len(orders)):
            rate = (time.monotonic() - started) / n
            log.info("%d/%d orders  %.2fs/order  calls=%d  eta=%.1f min", n, len(orders), rate,
                     llm.MODEL_CALLS, rate * (len(orders) - n) / 60)

    if workers <= 1:
        for o in orders:
            collect(ask(o, spec, root))
        return rows
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for future in concurrent.futures.as_completed([pool.submit(ask, o, spec, root) for o in orders]):
            collect(future.result())
    return rows


def summarise(rows: list[ModelRow], windows: list[dict]) -> dict:
    """Run summary for the log.

    Args:
        rows (list[ModelRow]): the model's answers.
        windows (list[dict]): every regex row.
    Returns:
        dict: counts by model status, kind, rejection reasons; windows by method; the combined window share.
    """
    ok = [r for r in rows if r.model_status == MODEL_WINDOW]
    with_text = [w for w in windows if w["window_status"] != ew.NO_TEXT]
    regex = sum(w["window_status"] == ew.PARSED for w in with_text)
    return {
        "orders_sent": len(rows),
        "model_status": dict(collections.Counter(r.model_status for r in rows)),
        "window_kind": dict(collections.Counter(r.window_kind for r in ok)),
        "unverified_reasons": dict(collections.Counter(r.note for r in rows
                                                       if r.model_status == MODEL_UNVERIFIED)),
        "rejected_reasons": dict(collections.Counter(r.note for r in rows
                                                     if r.model_status == MODEL_REJECTED)),
        "from_cache": sum(bool(r.from_cache) for r in rows),
        "model_calls": llm.MODEL_CALLS,
        "orders_with_window_regex": regex,
        "orders_with_window_model": len(ok),
        "window_share_all_methods": (round((regex + len(ok)) / len(with_text), 4)
                                     if with_text else None),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.window_model``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.window_model",
        description="Model fallback for violation windows regex v1 could not settle (runbook Week 2, Day 2)",
    )
    parser.add_argument("--windows", type=pathlib.Path, default=WINDOWS_IN)
    parser.add_argument("--candidates", type=pathlib.Path, default=CANDIDATES_IN)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--think", action="store_true", help="let the model reason before answering (slower)")
    parser.add_argument("--status", action="append", choices=[ew.PARSED, *MODEL_STATUSES],
                        help="regex statuses to send (default: UNCONFIRMED_RANGE and UNPARSED); "
                             "PARSED is for checking the model against regex")
    parser.add_argument("--order-id", action="append", type=int)
    parser.add_argument("--stem", action="append", help="text_stem to send (repeatable)")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=1,
                        help="concurrent requests; only useful if Ollama serves several at once "
                             "(OLLAMA_NUM_PARALLEL), which this machine's default does not")
    parser.add_argument("--offline", action="store_true",
                        help="serve every answer from raw/llm/; fail on a cache miss")
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: write the parquets, the log and the model cache under DIR")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run stage 5: ask the model about the orders regex could not settle, and combine.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0.
    """
    args = build_arg_parser().parse_args(argv)
    if args.scratch:
        # A dry run keeps its model answers under DIR too, like extract_text's
        # OCR cache, so it leaves raw/ untouched.
        out_dir, log_file = args.scratch, args.scratch / "window_model.log"
        cache_root = args.scratch / "llm"
    else:
        out_dir, log_file = DERIVED_DIR, LOGS_DIR / "window_model.log"
        cache_root = llm.LLM_CACHE
    for path in (out_dir, log_file):
        assert_safe_output(path)
    setup_logging(args.log_level, log_file)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per model call otherwise

    llm.OFFLINE = args.offline
    if args.offline:
        # The digest is part of the cache key; offline, it cannot be asked for,
        # so take it from a cached answer for this model.
        digest = _cached_digest(args.model, cache_root)
    else:
        digest = llm.model_digest(args.model)
    spec = llm.ModelSpec(args.model, digest, think=args.think, options=OPTIONS)

    statuses = tuple(args.status) if args.status else MODEL_STATUSES
    windows, orders = load_orders(args.windows, args.candidates, statuses=statuses)
    orders = select(orders, order_ids=args.order_id, stems=args.stem, sample=args.sample,
                    limit=args.limit)
    log.info("%s %s (digest %s, think=%s): %d orders -> %s", PROMPT_VERSION, spec.name,
             spec.digest[:12], spec.think, len(orders), out_dir)

    started = time.monotonic()
    rows = run(orders, spec, workers=args.workers, root=cache_root)
    # The combined table only makes sense for a full run of the default statuses.
    full = not (args.order_id or args.stem or args.sample or args.limit or args.status)
    paths = write_outputs(windows, rows, out_dir, combined=full)

    log.info("=" * 72)
    for key, value in summarise(rows, windows).items():
        log.info("%-28s %s", key, value)
    log.info("elapsed %.1f min", (time.monotonic() - started) / 60)
    log.info("wrote %s", ", ".join(str(p) for p in paths))
    if not full:
        log.info("subset run: violation_windows.parquet not written")
    return 0


def _cached_digest(name: str, root: pathlib.Path) -> str:
    """The model's weights digest taken from a cached answer (offline runs cannot ask the server).

    Args:
        name (str): the model name.
        root (pathlib.Path): the model cache root.
    Returns:
        str: the digest.
    Raises:
        llm.CacheMiss: no cached answer for this model.
    """
    for path in sorted(root.glob("*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("request", {}).get("model") == name:
            return record["request"]["digest"]
    raise llm.CacheMiss(f"no cached answers for {name}; run once online first")


if __name__ == "__main__":
    raise SystemExit(main())
