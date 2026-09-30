"""Week 2, Day 4: prior actions and sanctions.

Reads ``derived/text_documents.parquet`` (order texts) and
``derived/windows_checked.parquet`` (the Week 2 windows, for plausibility) and
writes

* ``derived/prior_action_mentions.parquet`` — every show-cause notice, interim /
  ex-parte order, confirmatory order and Adjudicating Officer appointment the
  order recites, with its date, snippet and a plausibility verdict;
* ``derived/sanction_mentions.parquet`` — every sanction in the operative part:
  penalty, settlement amount, disgorgement, restraint (debarment), action on a
  certificate of registration, warning, or a statement that no penalty follows;
  one row per amount, with the amount in words where given and the text naming
  whom it is imposed on (for Day 5, which links sanctions to parties);
* ``derived/order_actions.parquet`` — one row per order: ``prior_action_date``,
  ``first_action_date`` and a sanction summary.

Prior actions (runbook Day 4; technical plan §5.1)
--------------------------------------------------
"The first action is the earliest of an interim order, a show-cause notice date
recited in the order, or the adjudication order itself. Where an order recites a
prior interim order, that earlier date governs — otherwise the lag is inflated
by adjudication time." So ``prior_action_date`` is the earliest *plausible* SCN,
interim (incl. ex-parte / ad-interim) or confirmatory order date recited, and
``first_action_date = min(prior_action_date, order_date)``.

The date must belong to the notice or order itself: "SCN dated X" and "SCN No.
EAD/… dated X" count; "SCN was served … by email dated X", "reply to the SCN
vide letter dated X" and "interim order … disposed of vide final order dated X"
do not (no serve / reply / letter / email / vide / final … between the keyword
and "dated"). A date is plausible if it is not after the order, not before 30
January 1992 (the SEBI Act), and not before the order's violation window starts
(an action cannot precede the conduct it addresses; such a date is usually from
another matter the order cites).

The date the Adjudicating Officer was appointed (11,739 AO orders state it) is
SEBI's internal decision to adjudicate, usually months before the SCN. It is
not a public action and not in the plan's definition, so it is kept separately
(``ao_appointment_date``) for a robustness cut.

Sanctions (runbook Day 4; technical plan §4.2)
----------------------------------------------
Only the operative part counts: sentences in the last 60% of the order that
*impose* ("I hereby impose …", "… is hereby imposed", "shall pay a penalty of
…"). Statute quoted in the text ("which shall not be less than five lakh rupees
but which may extend to twenty-five crore rupees", 6,121 orders), show-cause
language ("why penalty should not be imposed") and past sanctions recited
("the penalty imposed vide order dated …") are excluded. In a table of
penalties a "Total" amount is kept but marked, so it is not added twice;
"jointly and severally" and "… each" are marked, because the order-level total
then depends on the parties (Day 5).
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import logging
import os
import pathlib
import random
import re
import time
from dataclasses import asdict, dataclass, field

import pyarrow as pa
import pyarrow.parquet as pq

from src import amounts as am
from src import cache
from src import extract_windows as ew
from src.extract_text import assert_safe_output, setup_logging

log = logging.getLogger("extract_actions")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
TEXT_DOCUMENTS_IN = DERIVED_DIR / "text_documents.parquet"
WINDOWS_IN = DERIVED_DIR / "windows_checked.parquet"
LOGS_DIR = REPO_ROOT / "logs"

RULES_VERSION = "actions-v1"
SEBI_ACT = dt.date(1992, 1, 30)
#: The operative part: sentences from this share of the text onwards.
OPERATIVE_FROM = 0.4

# prior action kinds
SCN, INTERIM, CONFIRMATORY, AO_APPOINTMENT = "scn", "interim", "confirmatory", "ao_appointment"
PRIOR_KINDS = (SCN, INTERIM, CONFIRMATORY)

# sanction kinds
PENALTY, SETTLEMENT, DISGORGEMENT, DEBARMENT = "penalty", "settlement", "disgorgement", "debarment"
REGISTRATION, WARNING, NO_PENALTY = "registration", "warning", "no_penalty"

_I = re.IGNORECASE


# --------------------------------------------------------------------------
# Prior actions
# --------------------------------------------------------------------------

_SCN = r"(?:show[\s-]*cause[\s-]*(?:cum[\s-]+[a-z-]+[\s-]+)?notices?|\bSCNs?\b)"
_INTERIM = (r"(?:\b(?:ex[\s-]*parte|ad[\s-]*interim|interim)(?:[\s,-]+(?:ex[\s-]*parte|ad[\s-]*interim|interim|cum))*"
            r"[\s-]+(?:orders?|directions?)(?:[\s-]*cum[\s-]*show[\s-]*cause[\s-]*notices?)?)")
_CONFIRMATORY = r"\bconfirmatory\s+orders?"
#: Between the keyword and "dated": a reference number or "(hereinafter …)"
#: is fine; words that attach the date to something else are not.
#: Other documents that carry their own "dated" ("reply to the SCN, a circular
#: resolution dated …", "SCN … a purported MOU dated …") end the gap too.
_GAP = (r"(?:(?!\b(?:served|deliver\w*|repl\w*|letters?|e-?mails?|vide|receiv\w*|hearings?|submi\w*|respon\w*|"
        r"dated|disposed|final|appeal\w*|tribunal|SAT|court|judge?ment|orders?|resolutions?|MOU|memorand\w*|"
        r"notifications?|circulars?|agreements?|reports?|minutes|meetings?|announcements?|disclosures?|"
        r"intimations?|applications?|complaints?|summons|affidavits?|articles?|advertisements?)\b)[^.;]){0,110}?")
_SHORT_GAP = _GAP.replace("{0,110}", "{0,60}")
_DATE = ew._FULL
_DATE_LIST = (rf"(?P<dates>{_DATE}(?:\s*(?:,|and|&|as\s+well\s+as)\s*(?:(?:the\s+)?(?:supplementary\s+)?"
              rf"(?:SCNs?|show[\s-]*cause[\s-]*notices?|orders?)\s+)?(?:dated\s+)?{_DATE})*)")

PRIOR_PATTERNS: list[tuple[str, re.Pattern]] = [
    (SCN, re.compile(rf"{_SCN}{_GAP}\bdated\s*[:\-]?\s*{_DATE_LIST}", _I)),
    (SCN, re.compile(rf"{_SCN}{_SHORT_GAP}\b(?:was|were|had\s+been|has\s+been|is|are)\s+issued\b{_SHORT_GAP}"
                     rf"\bon\s+(?:or\s+about\s+)?{_DATE_LIST}", _I)),
    (SCN, re.compile(rf"\bissued\s+(?:an?\s+|the\s+)?(?:common\s+|supplementary\s+)?{_SCN}{_SHORT_GAP}"
                     rf"\bon\s+{_DATE_LIST}", _I)),
    (INTERIM, re.compile(rf"{_INTERIM}{_GAP}\bdated\s*[:\-]?\s*{_DATE_LIST}", _I)),
    (INTERIM, re.compile(rf"{_INTERIM}{_SHORT_GAP}\b(?:was|were|had\s+been|has\s+been)?\s*(?:passed|issued)\s+"
                         rf"(?:by\s+SEBI\s+)?on\s+{_DATE_LIST}", _I)),
    (INTERIM, re.compile(rf"\bpassed\s+(?:an?\s+|the\s+)?{_INTERIM}{_SHORT_GAP}\bon\s+{_DATE_LIST}", _I)),
    (CONFIRMATORY, re.compile(rf"{_CONFIRMATORY}{_GAP}(?:\bdated\s*)?{_DATE_LIST}", _I)),
]
_AO = re.compile(r"\bappoint\w*[^.;]{0,160}?\badjudicating\s+officer\b|\badjudicating\s+officer\b[^.;]{0,120}?\bappoint\w*", _I)
_AO_DATE = re.compile(rf"\b(?:order|communiqu[eé]|communication)\s+dated\s*[:\-]?\s*(?P<d>{_DATE})", _I)
#: "No." / "Ref." inside reference numbers would end the gap at the full stop.
_ABBREV_DOT = re.compile(r"\b(No|Nos|Ref|no|nos|ref|NO|REF)\.")
_PREFILTER = re.compile(r"show[\s-]*cause|\bSCN|interim|ex[\s-]*parte|confirmatory|adjudicating\s+officer", _I)


@dataclass
class PriorMention:
    text_stem: str
    kind: str
    date: dt.date
    precision: str
    match_text: str
    snippet: str
    rel_pos: float
    plausible: bool
    reason: str | None


def _dates(text: str) -> list[ew.ParsedDate]:
    out = []
    for m in ew.DATE.finditer(text):
        p = ew.parse_date(m.group(0))
        if p is not None and p.year is not None:
            out.append(p)
    return out


def plausibility(d: dt.date, order_date: dt.date | None,
                 window_start: dt.date | None) -> tuple[bool, str | None]:
    if d < SEBI_ACT:
        return False, "before the SEBI Act"
    if order_date is not None and d > order_date:
        return False, "after the order"
    if window_start is not None and d < window_start:
        return False, "before the violation window"
    return True, None


def find_prior_mentions(flat: str, stem: str, order_date: dt.date | None,
                        window_start: dt.date | None) -> list[PriorMention]:
    if not _PREFILTER.search(flat):
        return []
    text = _ABBREV_DOT.sub(r"\1", flat)  # same length minus the dots is not needed: snippets use it too
    n = max(len(text), 1)
    out, seen = [], set()
    for kind, pattern in PRIOR_PATTERNS:
        for m in pattern.finditer(text):
            for p in _dates(m.group("dates")):
                d = ew.as_start(p)
                key = (kind, d, m.start() // 200)
                if key in seen:
                    continue
                seen.add(key)
                ok, reason = plausibility(d, order_date, window_start)
                out.append(PriorMention(stem, kind, d, p.precision, m.group(0)[:300],
                                        text[max(0, m.start() - 150):m.end() + 100], round(m.start() / n, 4),
                                        ok, reason))
    for m in _AO.finditer(text):
        sentence_end = text.find(". ", m.end())
        region = text[max(0, m.start() - 200): sentence_end if sentence_end > 0 else m.end() + 200]
        if dm := _AO_DATE.search(region):
            p = ew.parse_date(dm.group("d"))
            if p is None or p.year is None:
                continue
            d = ew.as_start(p)
            key = (AO_APPOINTMENT, d, m.start() // 200)
            if key in seen:
                continue
            seen.add(key)
            ok, reason = plausibility(d, order_date, window_start)
            out.append(PriorMention(stem, AO_APPOINTMENT, d, p.precision, dm.group(0),
                                    region[:450], round(m.start() / n, 4), ok, reason))
    return out


# --------------------------------------------------------------------------
# Sanctions
# --------------------------------------------------------------------------

#: "[it]mpos": OCR reads "impose" as "tmpose" ("| hereby tmpose a penalty of Rs.1,00,000/-").
_IMPOSE = re.compile(
    r"\bhereby\s+[it]mpose|(?:\bI|\|)\s+[it]mpose|\b[it]mpose\s+(?:a\s+|an\s+|the\s+)?(?:total\s+|consolidated\s+)?(?:monetary\s+)?penalt"
    r"|\bpenalt\w*\b[^.;]{0,80}?\b(?:is|are)\s+(?:hereby\s+)?imposed"
    r"|\b(?:shall|to)\s+pay\s+(?:a\s+)?(?:total\s+)?(?:monetary\s+)?penalty\s+of"
    r"|\bliable\s+to\s+pay\s+(?:a\s+)?(?:monetary\s+)?penalty\s+of", _I)
#: Statute, show-cause language and sanctions recited from the past.
_NOT_OPERATIVE = re.compile(
    r"not\s+be\s+less\s+than|may\s+extend\s+to|subject\s+to\s+a\s+maximum|why\s+[^.;]{0,60}?should\s+not"
    r"|\bproposed\b|\bhad\s+(?:been\s+)?imposed|\b(?:was|were)\s+imposed|\bimposed\s+vide|\bvide\s+(?:its\s+)?order\s+dated"
    r"|\bset\s+aside|\bquashed|\bin\s+the\s+case\s+of\b", _I)
_TOTAL_BEFORE = re.compile(r"\b(?:total|aggregate|in\s+all|consolidated)\b[^\d]{0,40}$", _I)
_PER_UNIT_AFTER = re.compile(r"^\s*(?:/-\s*)?(?:per\s+(?:day|share|unit)|for\s+each\s+day|each\s+day)", _I)
_EACH_AFTER = re.compile(r"^\s*(?:/-\s*)?(?:\([^)]{0,80}\)\s*)?(?:each\b|on\s+each\b|upon\s+each\b)", _I)
_JOINT = re.compile(r"\bjointly\s+and\s+severally\b", _I)
_WHO = re.compile(r"^\s*(?:/-\s*)?(?:\([^)]{0,90}\)\s*)?(?:each\s+)?(?:up)?on\s+(?:each\s+of\s+)?(?:the\s+)?"
                  r"(?P<who>[^.;]{2,90}?)\s*(?=\bunder\b|\bu/s\b|\bin\s+terms\b|\bfor\s+(?:the\s+)?(?:violation|non)|"
                  r"\bas\s+per\b|\bwhich\b|[.;,]|$)", _I)

_SETTLEMENT = re.compile(r"\bsettle\w*", _I)
_SETTLEMENT_PAY = re.compile(r"\b(?:remit\w*|paid|pay\w*|deposit\w*|tender\w*)\b", _I)
_DISGORGE = re.compile(r"\bdisgorge\w*", _I)
_DIRECTIVE = re.compile(r"\bhereby\b|\b(?:shall|is|are|to)\s+(?:be\s+)?(?:\w+\s+){0,2}?(?:disgorge|restrain\w*|prohibit\w*|debar\w*|pay)"
                        r"|\bdirect\w*\s+to\b|\bI\s+(?:direct|restrain|prohibit|debar)", _I)
_PAST_OR_CITED = re.compile(r"\bhad\s+(?:been\s+)?|\b(?:was|were)\s+(?:directed|restrained|prohibited|debarred|suspended)"
                            r"|\bvide\s+(?:the\s+)?(?:interim|ex[\s-]*parte|ad[\s-]*interim|confirmatory|order)"
                            r"|\bvacat\w+|\brevok\w+|\bset\s+aside|\bearlier\b|\bprevious\w*", _I)
_RESTRAIN = re.compile(r"\b(?:restrain\w*|debar\w*|prohibit\w*)\b[^.;]{0,200}?\b(?:securities\s+market|buying|selling|"
                       r"dealing|accessing|associat\w+|capital\s+market)", _I)
_PERIOD_OF = re.compile(r"\bfor\s+(?:a\s+(?:further\s+)?period\s+of\s+)?(?P<d>(?:\d{1,3}|[a-z]+(?:[\s-][a-z]+)?)\s*"
                        r"(?:\(\s*[a-z\d\s-]+\s*\)\s*)?(?:years?|months?))\b", _I)
_INDEFINITE = re.compile(r"\b(?:till|until)\s+further\s+(?:orders?|directions?)\b", _I)
_REGISTRATION = re.compile(r"(?P<a>suspen\w+|cancel\w+)\s+(?:of\s+)?(?:the\s+)?(?:its\s+|their\s+|his\s+|her\s+)?"
                           r"certificate\s+of\s+registration|certificate\s+of\s+registration[^.;]{0,80}?\b(?:is|are|be|stands?)"
                           r"\s+(?:hereby\s+)?(?P<b>suspended|cancelled|canceled)", _I)
_WARNING = re.compile(r"\bhereby\s+(?:warn|caution)\w*|\b(?:is|are)\s+(?:hereby\s+)?(?:warned|cautioned)"
                      r"|\b(?:issue|issued|administer\w*)\s+(?:a\s+|an\s+)?(?:strict\s+)?(?:administrative\s+)?warning", _I)
#: A noticee's plea or a quoted judgment is not the outcome.
_PLEA_OR_CITED = re.compile(r"\b(?:pray\w*|request\w*|submit\w*|contend\w*|argu\w*|plea|urg\w+|Hon.?ble|"
                            r"Supreme\s+Court|High\s+Court|Tribunal|SAT\b|judge?ment|held\s+that|in\s+the\s+matter\s+of)", _I)
_NO_PENALTY = re.compile(
    r"disposed\s+(?:of|off)\s+without|without\s+(?:imposing|imposition\s+of)\s+(?:any\s+)?(?:monetary\s+)?penalt"
    r"|\bno\s+(?:monetary\s+)?penalty\s+(?:is|can|shall|needs?|be)\b"
    r"|not\s+(?:find\s+it\s+to\s+be\s+)?(?:a\s+)?(?:fit|appropriate)\s+case\s+(?:for|to)\s+(?:impos|levy)|\babate[sd]?\b"
    r"|charges?\b[^.;]{0,60}?\b(?:are|is|have|has)\s+not\s+(?:been\s+)?(?:established|proved|substantiated)"
    r"|does\s+not\s+stand\s+established|\bexonerat\w+"
    r"|\b(?:proceedings?|SCN)\s+(?:is|are|stands?)\s+(?:hereby\s+)?dropped"
    r"|\bhereby\s+dispose[sd]?\s+(?:of|off)\s+the\s+(?:adjudication|enquiry|proceedings|SCN|show)"
    r"|\b(?:proceedings?|SCN|show\s+cause\s+notice|matter)\b[^.;]{0,200}?\b(?:is|are|stands?|stand)\s+"
    r"(?:accordingly\s+|hereby\s+)?disposed\s+(?:of|off)\s*(?:[.;,]|$)", _I)


@dataclass
class SanctionMention:
    text_stem: str
    kind: str
    sentence: str
    rel_pos: float
    amount_rupees: int | None = None
    amount_text: str | None = None
    words_rupees: int | None = None
    words_agree: bool | None = None
    is_total: bool = False
    each: bool = False
    joint: bool = False
    who_text: str | None = None
    amount_pos: int | None = None  # offset of the amount in the sentence
    months: int | None = None
    indefinite: bool = False
    action: str | None = None


#: Money that is not the sanction, named between the sanction word and the
#: figure: "the whole amount of Rs. 49,100/- is to be refunded", "the
#: misutilisation amount extends upto Rs 294.73 crores".
_OTHER_MONEY = re.compile(r"\b(?:refund\w*|gains?|profits?|turnover|misutili\w*|divert\w*|raised|mobili\w*|"
                          r"collected|loss(?:es)?|outstanding|dues|fees?|consideration|value|price|capital|"
                          r"investments?|deposits?|received|paid\s+up|net\s*worth|income)\b", _I)
#: The word that must be the nearest money word before a sanction amount.
_SANCTION_WORD = {PENALTY: re.compile(r"penalt\w*", _I), DISGORGEMENT: re.compile(r"disgorge\w*|gains?|profits?", _I),
                  SETTLEMENT: re.compile(r"settle\w*|remit\w*|paid|pay\w*|sum", _I)}


_OTHER_MONEY_AFTER = re.compile(r"^\s*(?:/-\s*)?(?:\([^)]{0,90}\)\s*)?(?:is|are|was|were|has|have)?\s*(?:been\s+|to\s+be\s+)?"
                                r"(?:refund\w*|repa\w*|return\w*|collect\w*|rais\w*|mobili\w*|divert\w*|misutili\w*)", _I)


def _is_sanction_amount(kind: str, sentence: str, start: int, end: int, anchor: int) -> bool:
    """Is this amount the sanction? It must follow the imposing words (for a
    penalty), no other money word may sit between the nearest sanction word and
    it, and it must not be money "to be refunded / raised / diverted"."""
    if kind == PENALTY and start < anchor:
        return False
    if _OTHER_MONEY_AFTER.match(sentence[end:end + 120]):
        return False
    before = sentence[max(0, start - 90):start]
    last_word = max((m.end() for m in _SANCTION_WORD[kind].finditer(before)), default=-1)
    if kind == DISGORGEMENT:
        return last_word >= 0
    last_other = max((m.end() for m in _OTHER_MONEY.finditer(before)), default=-1)
    return last_other < 0 or last_other < last_word


def _amount_mentions(stem: str, kind: str, sentence: str, rel: float, joint: bool,
                     anchor: int = 0) -> list[SanctionMention]:
    out = []
    for a in am.find_amounts(sentence, bare=True):
        after = sentence[a.end:a.end + 120]
        if _PER_UNIT_AFTER.match(after):
            continue
        if a.rupees < 100 and a.words_rupees is None:
            continue  # "Penalty amount in ` 1 Mukeshsingh …": a row number after a rupee sign
        if not _is_sanction_amount(kind, sentence, a.start, a.end, anchor):
            continue
        who = _WHO.match(after)
        out.append(SanctionMention(
            stem, kind, sentence, rel, a.rupees, a.text, a.words_rupees, a.words_agree,
            is_total=bool(_TOTAL_BEFORE.search(sentence[max(0, a.start - 40):a.start])),
            each=bool(_EACH_AFTER.match(after)), joint=joint,
            who_text=who.group("who").strip() if who else None, amount_pos=a.start))
    return out


#: How much of the rest of an over-long sentence a sanction clause may take in.
CONTINUATION_MAX = 8000


def _with_continuation(s: str, pieces: list[str] | None) -> str:
    """The clause and the rest of its sentence.

    A penalty paragraph with its table ("I hereby impose the following penalty on
    the Noticees – [table]") often exceeds the 2,000-character sentence cap, and
    ``split_sentences`` cuts it into pieces: the clause in one, the amounts in the
    next (46128; AUDIT_REPORT.md C-1). The following pieces of the same sentence
    are read with the clause, up to ``CONTINUATION_MAX`` characters and never past
    a piece that imposes something itself.
    """
    out = s
    for p in pieces or ():
        if _IMPOSE.search(p) or len(out) + len(p) > CONTINUATION_MAX:
            break
        out += " " + p
    return out


#: The order itself imposing ("I hereby impose", "I impose"), as opposed to "a fit
#: case to impose", "the AO has chosen to impose": only these may read on past
#: their own sentence.
_OPERATIVE_IMPOSE = re.compile(r"\bhereby\s+[it]mpose|(?:\bI|\|)\s*,?\s+[it]mpose", _I)
#: The clause points at a table after it.
_FORWARD = re.compile(r"\b(?:following|as\s+(?:under|follows)|(?:mentioned|detailed|given|shown|specified|set\s+out|"
                      r"tabulated|provided)\s+(?:below|hereunder)|table\s+below|below)\b", _I)
#: …but not a statute being quoted ("which reads as under: …").
_QUOTING = re.compile(r"\b(?:reads?|provides?|stipulates?|reproduced|extracted)\s+(?:as\s+)?(?:under|follows|below)", _I)
#: A sentence that ends a penalty table: another sanction, or the payment instructions.
_TABLE_END = re.compile(r"\b(?:shall|to)\s+(?:be\s+)?(?:remit\w*|pa(?:y|id))\b|\bdisgorge|\brestrain|\bdebar"
                        r"|certificate\s+of\s+registration|\bwarn", _I)


def _with_following(s: str, following: list[str]) -> str:
    """ "I hereby impose the following penalties on the noticees …, as applicable."
    with the table as the next sentence(s) (47376, 53836): the clause names no
    amount, so the sentences after it are read with it — up to
    ``CONTINUATION_MAX`` characters, and never past another imposing sentence,
    another sanction or the payment instructions."""
    out = s
    for p in following:
        if _IMPOSE.search(p) or _TABLE_END.search(p) or _QUOTING.search(p) or len(out) + len(p) > CONTINUATION_MAX:
            break
        if len(re.findall(r"\b[a-z]{3,}\b", p)) >= 25 and not am.find_amounts(p, bare=True):
            break  # running prose, not table rows
        out += " " + p
    return out


def find_sanctions(sentences: list[tuple[str, float]], stem: str, order_type: str | None,
                   continuations: list[list[str]] | None = None) -> list[SanctionMention]:
    """``continuations[i]``: the pieces that follow sentence ``i`` within the same
    over-long sentence (``split_sentences(..., groups=True)``), if any."""
    out: list[SanctionMention] = []
    for i, (s, rel) in enumerate(sentences):
        nearby = " ".join(x for x, _ in sentences[i:i + 3])
        rest = continuations[i] if continuations else None
        # Settlement amounts: anywhere in a settlement order.
        if order_type == "SETTLEMENT" and _SETTLEMENT.search(s) and _SETTLEMENT_PAY.search(s) \
                and "scheme" not in s.lower():
            out.extend(_amount_mentions(stem, SETTLEMENT, s, rel, False))
        if rel < OPERATIVE_FROM:
            continue
        cited = _NOT_OPERATIVE.search(s)
        if (imp := _IMPOSE.search(s)) and not cited:
            joint = bool(_JOINT.search(nearby))
            operative = bool(_OPERATIVE_IMPOSE.search(s))
            found = _amount_mentions(stem, PENALTY, _with_continuation(s, rest) if operative else s, rel, joint,
                                     anchor=imp.start())
            tail = s[imp.start():]
            if not found and operative and _FORWARD.search(tail) and not _QUOTING.search(tail) \
                    and not am.find_amounts(tail, bare=True):
                following = [x for x, _ in sentences[i + 1:i + 40]]
                found = _amount_mentions(stem, PENALTY, _with_following(s, following), rel, joint,
                                         anchor=imp.start())
            out.extend(found)
        if _DISGORGE.search(s) and _DIRECTIVE.search(s) and not _PAST_OR_CITED.search(s):
            out.extend(_amount_mentions(stem, DISGORGEMENT, _with_continuation(s, rest), rel,
                                        bool(_JOINT.search(nearby))))
        if _RESTRAIN.search(s) and _DIRECTIVE.search(s) and not _PAST_OR_CITED.search(s):
            period = _PERIOD_OF.search(s)
            out.append(SanctionMention(stem, DEBARMENT, s, rel,
                                       months=am.duration_months(period.group("d")) if period else None,
                                       indefinite=bool(_INDEFINITE.search(s))))
        if (r := _REGISTRATION.search(s)) and not _PAST_OR_CITED.search(s) \
                and (_DIRECTIVE.search(s) or r.group("b")):
            word = (r.group("a") or r.group("b")).lower()
            period = _PERIOD_OF.search(s)
            out.append(SanctionMention(stem, REGISTRATION, s, rel,
                                       action="suspension" if word.startswith("suspen") else "cancellation",
                                       months=am.duration_months(period.group("d")) if period else None))
        if _WARNING.search(s) and not _PAST_OR_CITED.search(s):
            out.append(SanctionMention(stem, WARNING, s, rel))
        if rel >= 0.5 and _NO_PENALTY.search(s) and not cited and not _PLEA_OR_CITED.search(s):
            out.append(SanctionMention(stem, NO_PENALTY, s, rel))
    return out


# --------------------------------------------------------------------------
# Per order
# --------------------------------------------------------------------------

@dataclass
class Job:
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    text_path: str | None
    text_status: str
    window_start: dt.date | None


@dataclass
class OrderActions:
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    text_status: str
    prior_action_date: dt.date | None = None
    prior_action_kind: str | None = None
    prior_action_raw: str | None = None
    n_prior_mentions: int = 0
    n_prior_implausible: int = 0
    earliest_scn_date: dt.date | None = None
    earliest_interim_date: dt.date | None = None
    ao_appointment_date: dt.date | None = None
    first_action_date: dt.date | None = None
    first_action_source: str | None = None
    penalty_total: int | None = None
    #: rows (sum of the amounts imposed) / stated_total (the order's own Total,
    #: used when rows cannot give it, e.g. "₹50 lakh each upon Noticee No. 3 & 4")
    #: / rows_each_unresolved ("each" with no Total: Day 5 multiplies by parties)
    penalty_total_basis: str | None = None
    penalty_n: int = 0
    penalty_max: int | None = None
    penalty_each: bool = False
    penalty_joint: bool = False
    penalty_words_checked: int = 0
    penalty_words_disagree: int = 0
    penalty_raw: str | None = None
    settlement_amount: int | None = None
    disgorgement_total: int | None = None
    debarred: bool = False
    debarment_months_max: int | None = None
    debarment_indefinite: bool = False
    registration_action: str | None = None
    warned: bool = False
    no_penalty_stated: bool = False
    #: penalty (a penalty is imposed, perhaps not on every noticee) / no_penalty
    #: (none imposed and the order says so) / unknown
    penalty_outcome: str = "unknown"
    rules_version: str = RULES_VERSION
    error: str | None = None


#: "on the Noticee" names no one in particular: it matches any party.
_GENERIC_HEAD = re.compile(r"(?:the\s+)?(?:said\s+|aforesaid\s+|above\s+)?(?:noticees?|applicants?|entity|company|"
                           r"it|him|her|them)\b(?P<rest>.*)", _I)
_IDENTIFYING = re.compile(r"\d|\bviz\b|\(|\bnamely\b|\bi\.?\s?e\b|m/s|\bshri\b|\bmr\b|\bms\b|\bsmt\b|[A-Z][a-z]+\s+[A-Z]")


def _generic(who: str | None) -> bool:
    if not who:
        return True
    m = _GENERIC_HEAD.match(who.strip())
    return bool(m) and not _IDENTIFYING.search(m.group("rest"))


def _who(s: SanctionMention) -> str:
    return "" if _generic(s.who_text) else re.sub(r"[^a-z0-9]", "", s.who_text.lower())


def penalty_rows(pen: list[SanctionMention]) -> list[SanctionMention]:
    """The penalties imposed, each once.

    Every amount in an imposing sentence is a row (two noticees with the same
    penalty in one table are two rows). A later sentence that only repeats
    amounts already imposed on the same parties is a restatement — "I impose a
    penalty of ₹35,00,000 …" in the discussion of quantum, then again in the
    ORDER — and is not counted again. "Total" amounts are not rows.
    """
    by_sentence: dict[str, list[SanctionMention]] = {}
    for s in pen:
        if not s.is_total:
            by_sentence.setdefault(s.sentence, []).append(s)
    counted: list[tuple[int, str]] = []
    rows: list[SanctionMention] = []
    for sentence, items in by_sentence.items():
        pool = list(counted)
        restated = True
        for s in items:
            match = next((k for k in pool if k[0] == s.amount_rupees
                          and (k[1] == _who(s) or k[1] == "" or _who(s) == "")), None)
            if match is None:
                restated = False
                break
            pool.remove(match)
        if restated and counted:
            continue
        counted.extend((s.amount_rupees, _who(s)) for s in items)
        rows.extend(sorted(items, key=lambda s: s.amount_pos or 0))
    return rows


def _sentence_amount(items: list[SanctionMention]) -> int:
    """What one sentence says is paid: its stated total; else, when the largest
    amount is the sum of at least two others ("Rs. 35,964 consisting of Rs. 25,964
    … and Rs. 10,000"), that amount; else the sum (several applicants in a table,
    "Rs. 25,000 towards settlement charges and Rs. 25,000 towards expenses")."""
    totals = [s.amount_rupees for s in items if s.is_total]
    if totals:
        return max(totals)
    amounts = [s.amount_rupees for s in items]
    top = max(amounts)
    rest = list(amounts)
    rest.remove(top)
    if len(rest) >= 2 and sum(rest) == top:
        return top
    return sum(amounts)


def settlement_amount(mentions: list[SanctionMention]) -> int:
    """The settlement amount of an order.

    Settlement orders state it several times — the applicant's proposal, the
    Internal Committee's and HPAC's recommendations, the remittance — and a
    proposal may be revised upwards. Each sentence is read on its own
    (``_sentence_amount``), and the order's amount is the largest of them.
    Summing across sentences counted one ₹92.6 lakh settlement as ₹1.85 crore
    (100354; AUDIT_REPORT.md C-2). An order in which several applicants each pay
    in a sentence of their own is under-counted by this rule.
    """
    by_sentence: dict[str, list[SanctionMention]] = {}
    for s in mentions:
        by_sentence.setdefault(s.sentence, []).append(s)
    return max(_sentence_amount(items) for items in by_sentence.values())


def summarise_order(job: Job, prior: list[PriorMention], sanctions: list[SanctionMention]) -> OrderActions:
    o = OrderActions(job.text_stem, job.order_id, job.order_type, job.order_date, job.text_status)
    o.n_prior_mentions = len(prior)
    o.n_prior_implausible = sum(not p.plausible for p in prior)
    good = [p for p in prior if p.plausible]
    main = [p for p in good if p.kind in PRIOR_KINDS]
    if main:
        first = min(main, key=lambda p: (p.date, p.rel_pos))
        o.prior_action_date, o.prior_action_kind, o.prior_action_raw = first.date, first.kind, first.snippet
    scn = [p.date for p in good if p.kind == SCN]
    interim = [p.date for p in good if p.kind == INTERIM]
    ao = [p.date for p in good if p.kind == AO_APPOINTMENT]
    o.earliest_scn_date = min(scn) if scn else None
    o.earliest_interim_date = min(interim) if interim else None
    o.ao_appointment_date = min(ao) if ao else None
    if job.order_date is not None:
        if o.prior_action_date is not None and o.prior_action_date < job.order_date:
            o.first_action_date, o.first_action_source = o.prior_action_date, "prior_action"
        else:
            o.first_action_date, o.first_action_source = job.order_date, "order"

    pen = [s for s in sanctions if s.kind == PENALTY and s.amount_rupees]
    if pen:
        rows = penalty_rows(pen)
        totals = [s for s in pen if s.is_total]
        rows_sum = sum(s.amount_rupees for s in rows)
        o.penalty_each = any(s.each for s in pen)
        o.penalty_joint = any(s.joint for s in pen)
        if not rows:  # only a Total was stated
            rows = totals
            rows_sum = sum(s.amount_rupees for s in rows)
        stated = {s.amount_rupees for s in totals}
        every_mention = sum(s.amount_rupees for s in pen if not s.is_total)
        # A single stated total that the rows miss is taken when it explains the
        # gap: "₹X each", or equal penalties in separate sentences that the
        # restatement rule merged ("… ₹2,00,000 … & ₹2,00,000 …. Further, … ₹2,00,000
        # under Section 15HA … a consolidated penalty of ₹6,00,000", 14949;
        # AUDIT_REPORT.md H-2).
        if len(stated) == 1 and (total := next(iter(stated))) != rows_sum \
                and (o.penalty_each or total == every_mention):
            o.penalty_total, o.penalty_total_basis = total, "stated_total"
        else:
            o.penalty_total = rows_sum
            o.penalty_total_basis = "rows_each_unresolved" if o.penalty_each else "rows"
        o.penalty_n = len(rows)
        o.penalty_max = max(s.amount_rupees for s in rows)
        o.penalty_words_checked = sum(s.words_agree is not None for s in rows)
        o.penalty_words_disagree = sum(s.words_agree is False for s in rows)
        o.penalty_raw = " || ".join(dict.fromkeys(s.sentence for s in rows))[:4000]
    settle = [s for s in sanctions if s.kind == SETTLEMENT and s.amount_rupees]
    if settle:
        o.settlement_amount = settlement_amount(settle)
    dis = [s for s in sanctions if s.kind == DISGORGEMENT and s.amount_rupees and not s.is_total]
    if dis:
        # the same amount restated in a later sentence is one direction, not two
        o.disgorgement_total = sum(s.amount_rupees for s in penalty_rows(dis))
    deb = [s for s in sanctions if s.kind == DEBARMENT]
    if deb:
        o.debarred = True
        months = [s.months for s in deb if s.months]
        o.debarment_months_max = max(months) if months else None
        o.debarment_indefinite = any(s.indefinite for s in deb)
    reg = [s.action for s in sanctions if s.kind == REGISTRATION]
    if reg:
        o.registration_action = "cancellation" if "cancellation" in reg else "suspension"
    o.warned = any(s.kind == WARNING for s in sanctions)
    o.no_penalty_stated = any(s.kind == NO_PENALTY for s in sanctions)
    o.penalty_outcome = ("penalty" if o.penalty_total is not None else
                         "no_penalty" if o.no_penalty_stated else "unknown")
    return o


def process(job: Job, repo_root: pathlib.Path = REPO_ROOT):
    """Extract one order. Never raises."""
    if job.text_status != "OK" or not job.text_path:
        return OrderActions(job.text_stem, job.order_id, job.order_type, job.order_date, job.text_status), [], []
    try:
        text = (repo_root / job.text_path).read_text(encoding="utf-8")
        stripped = ew.strip_repeated_lines(text)
        flat = ew.normalise_sentence(stripped)
        prior = find_prior_mentions(flat, job.text_stem, job.order_date, job.window_start)
        n = max(len(stripped), 1)
        spans = ew.split_sentences(stripped, groups=True)
        sentences = [(ew.normalise_sentence(stripped[s:e]), s / n) for s, e, _ in spans]
        # the pieces that follow each sentence inside one over-long sentence
        # (pieces of a group are contiguous)
        continuations: list[list[str]] = [[] for _ in spans]
        for i in range(len(spans) - 2, -1, -1):
            if spans[i + 1][2] == spans[i][2]:
                continuations[i] = [sentences[i + 1][0]] + continuations[i + 1]
        sanctions = find_sanctions(sentences, job.text_stem, job.order_type, continuations)
        return summarise_order(job, prior, sanctions), prior, sanctions
    except Exception as exc:  # pragma: no cover - last-resort guard for a long run
        o = OrderActions(job.text_stem, job.order_id, job.order_type, job.order_date, job.text_status)
        o.error = f"{type(exc).__name__}: {exc}"[:300]
        return o, [], []


def _process_many(jobs, repo_root):
    return [process(j, repo_root) for j in jobs]


# --------------------------------------------------------------------------
# Input / output
# --------------------------------------------------------------------------

def load_jobs(documents: pathlib.Path = TEXT_DOCUMENTS_IN, windows: pathlib.Path = WINDOWS_IN) -> list[Job]:
    docs = pq.read_table(documents, columns=["text_stem", "order_id", "order_type", "order_date",
                                             "text_path", "status"]).to_pylist()
    starts = {}
    if windows.exists():
        for r in pq.read_table(windows, columns=["text_stem", "violation_start"]).to_pylist():
            starts[r["text_stem"]] = r["violation_start"]
    return [Job(r["text_stem"], r["order_id"], r["order_type"], r["order_date"], r["text_path"], r["status"],
                starts.get(r["text_stem"])) for r in docs]


def select(jobs, *, categories=None, order_ids=None, stems=None, sample=None, limit=None, seed=7):
    out = jobs
    if categories:
        out = [j for j in out if j.order_type in set(categories)]
    if order_ids:
        out = [j for j in out if j.order_id in set(order_ids)]
    if stems:
        out = [j for j in out if j.text_stem in set(stems)]
    if sample:
        out = random.Random(seed).sample(out, min(sample, len(out)))
    if limit is not None:
        out = out[:limit]
    return out


def _schema(cls) -> pa.Schema:
    types = {"str": pa.string(), "int": pa.int64(), "float": pa.float64(), "bool": pa.bool_(),
             "dt.date": pa.date32()}
    fields = []
    for name, f in cls.__dataclass_fields__.items():
        t = str(f.type).replace(" | None", "")
        fields.append(pa.field(name, types[t]))
    return pa.schema(fields)


def _write(rows: list, cls, path: pathlib.Path) -> None:
    schema = _schema(cls)
    table = pa.Table.from_pylist([asdict(r) for r in rows], schema=schema)
    tmp = path.parent / f"{path.name}.tmp-{os.urandom(6).hex()}"
    pq.write_table(table, tmp, compression="snappy")
    tmp.replace(path)


def write_outputs(orders, prior, sanctions, out_dir: pathlib.Path) -> list[pathlib.Path]:
    assert_safe_output(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / "order_actions.parquet", out_dir / "prior_action_mentions.parquet",
             out_dir / "sanction_mentions.parquet"]
    _write(sorted(orders, key=lambda o: o.text_stem), OrderActions, paths[0])
    _write(sorted(prior, key=lambda p: (p.text_stem, p.rel_pos)), PriorMention, paths[1])
    _write(sorted(sanctions, key=lambda s: (s.text_stem, s.rel_pos)), SanctionMention, paths[2])
    return paths


def run(jobs, *, workers=1, repo_root=REPO_ROOT, chunk=200):
    orders, prior, sanctions = [], [], []

    def collect(res):
        o, p, s = res
        orders.append(o)
        prior.extend(p)
        sanctions.extend(s)

    if workers <= 1:
        for j in jobs:
            collect(process(j, repo_root))
        return orders, prior, sanctions
    batches = [jobs[i:i + chunk] for i in range(0, len(jobs), chunk)]
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process_many, b, repo_root) for b in batches]
        for done, fut in enumerate(concurrent.futures.as_completed(futures), start=1):
            for res in fut.result():
                collect(res)
            if done % 20 == 0 or done == len(futures):
                log.info("%d/%d orders", len(orders), len(jobs))
    return orders, prior, sanctions


def summarise(orders: list[OrderActions]) -> dict:
    with_text = [o for o in orders if o.text_status == "OK"]
    by_type = collections.defaultdict(lambda: collections.Counter())
    for o in with_text:
        c = by_type[o.order_type]
        c["orders"] += 1
        c["prior_action"] += o.prior_action_date is not None
        c["penalty"] += o.penalty_total is not None
        c["settlement_amount"] += o.settlement_amount is not None
        c["debarred"] += o.debarred
        c["no_penalty"] += o.penalty_outcome == "no_penalty"
        c["unknown"] += o.penalty_outcome == "unknown"
    fmt = {k: ", ".join(f"{n} {v}" for n, v in c.items()) for k, c in sorted(by_type.items(), key=lambda kv: str(kv[0]))}
    pens = sorted(o.penalty_total for o in with_text if o.penalty_total)
    words = sum(o.penalty_words_checked for o in with_text)
    disagree = sum(o.penalty_words_disagree for o in with_text)
    q = (lambda x: pens[int(x * (len(pens) - 1))]) if pens else (lambda x: None)
    return {
        "orders_with_text": len(with_text),
        "prior_action_found": sum(o.prior_action_date is not None for o in with_text),
        "prior_action_kind": dict(collections.Counter(o.prior_action_kind for o in with_text if o.prior_action_kind)),
        "first_action_source": dict(collections.Counter(o.first_action_source for o in with_text)),
        "ao_appointment_found": sum(o.ao_appointment_date is not None for o in with_text),
        "by_type": fmt,
        "penalty_orders": len(pens),
        "penalty_quantiles_rupees": {p: q(p) for p in (0.05, 0.25, 0.5, 0.75, 0.95)},
        "penalty_words_checked/disagree": f"{words} / {disagree}",
        "penalty_each_or_joint": sum(o.penalty_each or o.penalty_joint for o in with_text),
        "disgorgement_orders": sum(o.disgorgement_total is not None for o in with_text),
        "registration_action": dict(collections.Counter(o.registration_action for o in with_text
                                                       if o.registration_action)),
        "warned": sum(o.warned for o in with_text),
        "errors": sum(o.error is not None for o in orders),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.extract_actions",
                                     description="Prior actions and sanctions (runbook Week 2, Day 4)")
    parser.add_argument("--documents", type=pathlib.Path, default=TEXT_DOCUMENTS_IN)
    parser.add_argument("--windows", type=pathlib.Path, default=WINDOWS_IN)
    parser.add_argument("--category", action="append", choices=["WTM", "SETTLEMENT", "AO", "ED_CGM"])
    parser.add_argument("--order-id", action="append", type=int)
    parser.add_argument("--stem", action="append")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: write the parquets and the log under DIR instead of derived/")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.scratch:
        out_dir, log_file = args.scratch, args.scratch / "extract_actions.log"
    else:
        out_dir, log_file = DERIVED_DIR, LOGS_DIR / "extract_actions.log"
    for p in (out_dir, log_file):
        assert_safe_output(p)
    setup_logging(args.log_level, log_file)
    jobs = select(load_jobs(args.documents, args.windows), categories=args.category, order_ids=args.order_id,
                  stems=args.stem, sample=args.sample, limit=args.limit)
    log.info("%s: %d orders with %d worker(s) -> %s", RULES_VERSION, len(jobs), args.workers, out_dir)
    started = time.monotonic()
    orders, prior, sanctions = run(jobs, workers=args.workers)
    paths = write_outputs(orders, prior, sanctions, out_dir)
    log.info("=" * 72)
    for k, v in summarise(orders).items():
        if k == "by_type":
            for t, line in v.items():
                log.info("  %-10s %s", t, line)
        else:
            log.info("%-30s %s", k, v)
    log.info("mentions: %d prior actions, %d sanctions", len(prior), len(sanctions))
    log.info("elapsed %.1f min; wrote %s", (time.monotonic() - started) / 60, ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
