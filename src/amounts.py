"""Rupee amounts and durations as SEBI orders write them.

One normaliser for money (runbook Week 2, Day 4: "Write one normaliser that
returns rupees as an integer"), and a second, independent reading of the amount
in words, which orders usually give alongside the figure — "₹ 5,00,000/-
(Rupees Five Lakh only)" — so every figure can be checked against its words.

Figures
-------
* Indian grouping: 26,00,00,000 (2-2-3), and western 26,000,000;
* units: "Rs. 26 crore", "₹1.5 lakh", "Rs. 765 cr", "INR 2 million";
* marks: "Rs.", "Rs", "Re.", "INR", "₹", "Rupees", and the backtick an old
  rupee font renders as ("` 25,00,000/-", 3,637 orders);
* "/-" and paise (".00") are dropped: amounts are whole rupees;
* typos seen in the corpus: "1,00.000" (a dot for a comma), "8, 52, 500"
  (spaces inside the figure).

Words
-----
Indian number words: "Rupees Two Lakh Forty Five Thousand Two Hundred and Fifty
only", "Rupees One Crore Twenty Lakh", "Rs. Five Lakhs only", "Twenty-Five".

Durations
---------
"two years", "4 (four) years", "06 (six) months", "one (1) year" -> months.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SCALES = {"crore": 10**7, "crores": 10**7, "cr": 10**7, "cr.": 10**7,
          "lakh": 10**5, "lakhs": 10**5, "lac": 10**5, "lacs": 10**5, "lakhs.": 10**5,
          "million": 10**6, "millions": 10**6, "thousand": 10**3, "thousands": 10**3}

#: "INR200004031" is a SEBI registration number (RTA), not rupees.
_CURRENCY = r"(?:Rs\.?|Re\.?|INR(?!\d{9})|₹|`|Rupees)"
#: Between digit runs: a comma (optionally followed by a space, "8, 52, 500")
#: before at least two digits, or a dot used for a comma ("1,00.000",
#: "5.00.000"): a dot is a group separator only before exactly three digits,
#: or before a group that is itself followed by a separator. A bare space
#: never joins: "Rs. 5,00,000 12 noticees" is five lakh.
_SEP = r"(?:,\s?(?=\d{2})|\.(?=\d{3}(?!\d)|\d{2,3}[.,]\d))"
#: A figure: whole digit runs joined by separators, then paise. Every digit
#: is consumed — an earlier pattern stopped after three ("Rs. 500000" was ₹500,
#: "₹1258.88 crore" ₹125; AUDIT_REPORT.md H-1).
_FIGURE = rf"\d+(?:{_SEP}\d+)*(?:\.\d{{1,2}}(?!\d))?"
_UNIT = r"(?:crores?|cr\.?|lakhs?|lacs?|millions?|thousands?)"

AMOUNT = re.compile(
    rf"(?<![A-Za-z])(?P<cur>{_CURRENCY})\.?\s*(?P<num>{_FIGURE})\s*(?:/-|/)?\s*(?P<unit>{_UNIT})?(?![a-z])",
    re.IGNORECASE)
#: A figure with no rupee mark, in a table of penalties: "5,00,000/- (Rupees
#: Five Lakhs Only)", "1, 00, 00,000/-". Only taken when "/-" or the amount in
#: words follows, which makes it money.
BARE_AMOUNT = re.compile(
    r"(?<![\d,.A-Za-z])(?P<num>\d{1,3}(?:,\s?\d{2,3})+(?:\.\d{1,2})?)(?=\s*(?:/-|\(\s*(?:Rupees|Rs\.?)\s))",
    re.IGNORECASE)

#: The amount in words right after a figure: "(Rupees Five Lakh only)".
WORDS_AFTER = re.compile(r"\s*/?-?\s*\(\s*(?:(?:Rupees|Rs\.?|INR|₹)\s+)?(?P<words>[A-Za-z][A-Za-z\s,-]{2,220}?)"
                         r"\s*(?:only)?\s*\)", re.IGNORECASE)


def parse_figure(num: str, unit: str | None = None) -> int | None:
    """Rupees from a figure and optional unit, or None if it is not a figure.

    A unit after a grouped figure of a lakh or more is redundant: "Rs.
    10,00,000 lakhs (Rupees Ten Lakhs only)", "Rs. 5,00,00,000 Crore" — nobody
    writes ten lakh lakh. The figure is the amount.

    Args:
        num (str): the figure as matched, e.g. "26,00,00,000", "1.5", "1,00.000".
        unit (str | None): "crore", "lakh", "million", "thousand" … if one follows.
    Returns:
        int | None: whole rupees, or None if the figure cannot be read.
    """
    s = re.sub(r"\s", "", num)
    if unit and "," in s and (plain := parse_figure(num)) is not None and plain >= 100_000:
        return plain
    if unit:
        scale = SCALES.get(unit.lower().rstrip(".") if unit.lower() not in SCALES else unit.lower())
        try:
            value = float(s.replace(",", ""))
        except ValueError:
            return None
        return round(value * (scale or 1))
    # Paise: "5,00,000.00" -> 500000.
    if m := re.fullmatch(r"([\d,]+)\.(\d{1,2})", s):
        s = m.group(1)
    # A dot used between digit groups is a typo for a comma: "1,00.000", "5.00.000".
    if "." in s and re.fullmatch(r"\d{1,3}(?:[.,]\d{2,3})+", s) and re.search(r"[.,]\d{3}$", s):
        s = s.replace(".", ",")
    if "." in s:
        try:
            return round(float(s.replace(",", "")))
        except ValueError:
            return None
    digits = s.replace(",", "")
    return int(digits) if digits.isdigit() else None


def malformed(num: str) -> bool:
    """Digit groups that fit neither Indian (2-2-3) nor western (3-3-3)
    grouping: "2,00,00" has lost a zero.

    Args:
        num (str): a figure with separators.
    Returns:
        bool: True if its digit groups fit neither Indian nor western grouping.
    """
    groups = re.split(r",\s?", re.sub(r"\.\d{1,2}$", "", num.strip()))
    if len(groups) < 2 or not all(g.isdigit() for g in groups):
        return False
    tail, middle = groups[-1], groups[1:-1]
    indian = len(tail) == 3 and all(len(g) == 2 for g in middle) and 1 <= len(groups[0]) <= 2
    western = len(tail) == 3 and all(len(g) == 3 for g in middle) and 1 <= len(groups[0]) <= 3
    return not (indian or western)


@dataclass(frozen=True)
class Amount:
    """One rupee amount found in text.

    Attributes:
        rupees (int): the amount (the words' value when the figure is malformed).
        text (str): the figure as written.
        words_rupees (int | None): the "(Rupees … only)" reading.
        start (int), end (int): offsets in the text (end includes the words when read).
        figure_rupees (int | None): the figure's own reading, when the words were preferred.
    """
    rupees: int                   # the amount: the figure, or its words if the figure is malformed
    text: str                     # the figure as written
    words_rupees: int | None      # the "(Rupees … only)" reading, if any
    start: int
    end: int                      # end of the figure, or of its words
    figure_rupees: int | None = None  # the figure's own reading, when the words were preferred

    @property
    def words_agree(self) -> bool | None:
        """Whether the figure and its words say the same amount.

        Returns:
            bool | None: True/False, or None when there are no words.
        """
        figure = self.figure_rupees if self.figure_rupees is not None else self.rupees
        return None if self.words_rupees is None else self.words_rupees == figure


def _with_words(text: str, rupees: int, figure: str, start: int, end: int, num: str) -> Amount:
    """Attach the amount-in-words that follows a figure, and prefer it when the figure is broken.

    Args:
        text (str): the text.
        rupees (int): the figure's value.
        figure (str): the matched figure text.
        start (int), end (int): the figure's span.
        num (str): the digits part (for the malformed check).
    Returns:
        Amount: the amount.
    """
    words = None
    if w := WORDS_AFTER.match(text, end):
        words = words_to_rupees(w.group("words"))
        if words is not None:
            end = w.end()
    if words is not None and words != rupees and (malformed(num) or rupees < 100 <= words):
        # "` 2,00,00/- (Rupees Two lakhs only)", or an OCR'd figure that reads
        # as nothing ("` I, 00, 00,000"): the words are right.
        return Amount(words, figure.strip(), words, start, end, figure_rupees=rupees)
    return Amount(rupees, figure.strip(), words, start, end)


def find_amounts(text: str, bare: bool = False) -> list[Amount]:
    """Every rupee amount in ``text``, with its words where given.

    ``bare=True`` also takes figures without a rupee mark that "/-" or words
    follow — safe only in a sentence already known to state a sanction.

    Args:
        text (str): a sentence or passage.
        bare (bool): also take unmarked figures followed by "/-" or words.
    Returns:
        list[Amount]: every amount, by position.
    """
    out = []
    for m in AMOUNT.finditer(text):
        rupees = parse_figure(m.group("num"), m.group("unit"))
        if rupees is not None:
            out.append(_with_words(text, rupees, m.group(0), m.start(), m.end(), m.group("num")))
    if bare:
        taken = [(a.start, a.end) for a in out]
        for m in BARE_AMOUNT.finditer(text):
            if any(s <= m.start() < e for s, e in taken):
                continue
            rupees = parse_figure(m.group("num"))
            if rupees is not None:
                out.append(_with_words(text, rupees, m.group(0), m.start(), m.end(), m.group("num")))
        out.sort(key=lambda a: a.start)
    return out


# --------------------------------------------------------------------------
# Numbers in words
# --------------------------------------------------------------------------

_ONES = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split())
         if w != "_"}
_TENS["fourty"] = 40  # a common misspelling in the orders
_FILLER = {"rupees", "rupee", "rs", "rs.", "inr", "only", "and", "a", "of"}


def words_to_number(words: str) -> int | None:
    """Indian number words to an integer; None if any word is not a number word.

    Args:
        words (str): e.g. "Two Lakh Forty Five Thousand".
    Returns:
        int | None: the number, or None.
    """
    tokens = [t for t in re.split(r"[\s,-]+", words.lower().strip()) if t]
    # A scale word larger than the last one multiplies everything before it
    # ("Seven Thousand Two Hundred Sixty Nine Crore" = 7,269 crore); a smaller
    # one adds ("One Crore Twenty Lakh").
    total, current, seen, last_scale = 0, 0, False, None
    for tok in tokens:
        tok = tok.rstrip(".")
        if tok in _FILLER:
            continue
        if tok in _ONES:
            current += _ONES[tok]
        elif tok in _TENS:
            current += _TENS[tok]
        elif tok == "hundred":
            current = (current or 1) * 100
        elif tok in SCALES:
            scale = SCALES[tok]
            if scale == last_scale and current == 0:
                continue  # "Ten Lakh lakhs": the scale repeated
            if last_scale is not None and scale > last_scale:
                total = (total + (current or 0)) * scale
            else:
                total += (current or 1) * scale
            current, last_scale = 0, scale
        else:
            return None
        seen = True
    return total + current if seen else None


def words_to_rupees(words: str) -> int | None:
    """Rupees from an amount in words (the same as words_to_number).

    Args:
        words (str): the words.
    Returns:
        int | None: rupees, or None.
    """
    return words_to_number(words)


# --------------------------------------------------------------------------
# Durations
# --------------------------------------------------------------------------

DURATION = re.compile(
    r"(?P<n>\d{1,3}|[a-z]+(?:[\s-][a-z]+)?)\s*(?:\(\s*(?P<alt>\d{1,3}|[a-z]+(?:[\s-][a-z]+)?)\s*\)\s*)?"
    r"(?P<unit>years?|months?)\b", re.IGNORECASE)


def duration_months(text: str) -> int | None:
    """"two years", "4 (four) years", "06 (six) months" -> months; None if not a duration.

    Args:
        text (str): text holding a duration, e.g. "for a period of 4 (four) years".
    Returns:
        int | None: months, or None if there is no duration.
    """
    m = DURATION.search(text)
    if not m:
        return None
    n = m.group("n")
    value = int(n) if n.isdigit() else words_to_number(n)
    if value is None and m.group("alt"):
        alt = m.group("alt")
        value = int(alt) if alt.isdigit() else words_to_number(alt)
    if value is None:
        return None
    return value * 12 if m.group("unit").lower().startswith("year") else value
