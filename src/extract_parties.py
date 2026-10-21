"""Week 2, Day 5: parties and regulations.

Reads ``derived/text_documents.parquet`` (order texts), ``derived/listings.parquet``
(titles) and Day 4's ``derived/sanction_mentions.parquet`` /
``derived/order_actions.parquet`` and writes

* ``derived/order_entities.parquet`` — one row per (order, party): the name as
  printed and cleaned, PAN / DIN / CIN / SEBI registration number, a first guess
  at the role with the evidence for it, the penalty imposed on *that* party
  (runbook: "per party, not per order") and the sanction types it received;
* ``derived/regulation_mentions.parquet`` — every provision cited, with the
  instrument, the provision and its role (violation / penalty / power / cited);
* ``derived/order_parties.parquet`` — one row per order: the matter, a guess at
  the issuer, party counts by role, the regulations list (the runbook's JSON
  list), the penalty sections, and how completely the penalties were linked.

Parties (runbook Day 5; ``hand/recon_notes.md`` §9)
--------------------------------------------------
97% of orders name their noticees in the header, after "In respect of",
"Against", "filed / submitted by" and before "In the matter of" or the first
paragraph:

    In respect of: Rajeev Kansal PAN: AAGHR0927R In the matter of Dealings in …
    In respect of: Noticee No. Noticee Name PAN 1 M/s. Aksh Optifibre Limited
    AAACA0062F 2 Dr. Kailash S Chaudhary AAHPC7797B … In the matter of GDR Issue …
    against M/s Grishma Securities Pvt. Ltd. (PAN:AABCG1300K), Shri Chandrika H.
    Gandhi (PAN: AAMPG4546M), … and Shri Vanraj Kahor (PAN; AGDPK4812F) in the …

The segment is split into entries by its serial numbers (1, 2, 3 … in order), by
PANs, or by commas before an honorific; each entry is cut to the name before
its PAN / DIN / address / role. Orders whose header names no one fall back to
"X (hereinafter referred to as 'the Noticee' / 'Noticee No. 2')" definitions,
then to the listing title ("Adjudication Order in respect of X").

Roles are a rough first pass, corrected in validation (runbook): a SEBI
registration number or broker / member wording → INTERMEDIARY; "Chartered
Accountants" / auditor → AUDITOR; the company named in "In the matter of" →
ISSUER; promoter → PROMOTER; director / MD / CFO / compliance officer or a DIN →
DIRECTOR; trading wording, or a trading matter → TRADER; any other company →
ISSUER (weak, ``role_basis = suffix``); anyone else UNKNOWN.

Penalties per party
-------------------
Day 4's counted penalty rows are linked to parties. With one party every row is
its own. In a table the party precedes its amount ("2 Mr. Narayan Ghumatkar
Section 15HB … Rs. 3,00,000/-"); in prose it follows ("a penalty of Rs. 5 lakh
on Noticee No. 2"). The layout is read from the first amount of the sentence.
An amount marked "each" goes to every party named with it; "jointly and
severally" amounts are given to each named party and flagged, so summing
``penalty_amount`` over parties counts a joint amount more than once — use the
order-level total for money, the party rows for who was penalised.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import functools
import json
import logging
import os
import pathlib
import random
import re
import time
from dataclasses import asdict, dataclass, field

import pyarrow as pa
import pyarrow.parquet as pq

from src import cache
from src import extract_actions as ea
from src import extract_windows as ew
from src import regulations as rg
from src.extract_text import assert_safe_output, setup_logging, text_stem

log = logging.getLogger("extract_parties")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
TEXT_DOCUMENTS_IN = DERIVED_DIR / "text_documents.parquet"
LISTINGS_IN = DERIVED_DIR / "listings.parquet"
SANCTIONS_IN = DERIVED_DIR / "sanction_mentions.parquet"
ACTIONS_IN = DERIVED_DIR / "order_actions.parquet"
LOGS_DIR = REPO_ROOT / "logs"

RULES_VERSION = "parties-v1"
#: The header (where noticees are listed) never runs past this many characters.
HEADER_MAX = 25_000
#: Role windows are read for at most this many parties per order.
ROLE_WINDOW_PARTIES = 80

ISSUER, PROMOTER, DIRECTOR, AUDITOR = "ISSUER", "PROMOTER", "DIRECTOR", "AUDITOR"
INTERMEDIARY, TRADER, UNKNOWN = "INTERMEDIARY", "TRADER", "UNKNOWN"

_I = re.IGNORECASE

# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------

#: PAN: 5 letters (4th = holder type: P person, C company, H HUF, F firm/LLP,
#: T trust, A/B association, G/L/J other), 4 digits, 1 letter.
PAN = re.compile(r"\b[A-Z]{3}[PCHFATBLJGE][A-Z]\d{4}[A-Z]\b")
#: Where a PAN is printed or said to be unavailable: the end of a party's entry.
PAN_ANCHOR = re.compile(r"\b[A-Z]{3}[PCHFATBLJGE][A-Z]\d{4}[A-Z]\b|"
                        r"\bPAN\s*(?:No\.?)?\s*[:\-–]?\s*(?:(?i:Not\s+Available|Not\s+Provided|N\.\s?A\.?|Nil)|NA\b)")
#: An Indian PIN code closing an address ("Mumbai – 400 001", "Ahmedabad-380015").
PIN = re.compile(r"(?<![A-Za-z0-9,./])[1-9]\d{2}\s?\d{3}(?![\d,/])")
CIN = re.compile(r"\b[LU]\s?\d{5}\s?[A-Z]{2}\s?\d{4}\s?[A-Z]{3}\s?\d{6}\b")
DIN_LABELLED = re.compile(r"\bDIN\s*(?:No\.?)?\s*[:\-–]?\s*(\d{7,8})\b", _I)
DIN_BARE = re.compile(r"(?<![\d/,.])(\d{8})(?![\d/,])")
#: SEBI registration: INB/INZ (brokers), INH (research analysts), INA
#: (investment advisers), INM (merchant bankers), INP (portfolio managers),
#: INR (RTAs), IN-DP-… (depository participants).
REGNO = re.compile(r"\bIN[A-Z]\s?\d{6,12}\b|\bIN[\s-]?DP[\s-]?(?:NSDL|CDSL)?[\s-]?[\d-]{2,}\d")
PAN_TYPE = {"P": "individual", "C": "company", "H": "huf", "F": "firm", "T": "trust", "A": "other", "B": "other",
            "G": "other", "L": "other", "J": "other", "E": "firm"}

# --------------------------------------------------------------------------
# Header and noticee segment
# --------------------------------------------------------------------------

_BODY = re.compile(
    r"\b(?:BACKGROUND|BRIEF\s+FACTS|FACTS\s+OF\s+THE\s+CASE|FACTS\s+IN\s+BRIEF|INTRODUCTION|PRELIMINARY)\b"
    r"|\b(?:Background|Brief\s+facts|Facts\s+of\s+the\s+case|Introduction)\b\s*[:.\-]?\s*(?=\d|[A-Z])"
    r"|\b(?:Whereas|WHEREAS)\b"
    r"|(?<![\w./-])1\s*[.)]\s+(?:1\s*[.)]?\s*)?(?=Securities\s+and\s+Exchange|SEBI\b|The\s+(?:Securities|Board|present|"
    r"instant|Noticee|Applicant|[Cc]ompany|facts|matter|brief|SCN|[Ss]how)|This\s|Pursuant|An?\s|It\s|In\s|On\s|By\s|"
    r"Vide\s|During|Based|Upon|As\s|Consequent|Following|Having|These|Brief|I\s|Show|Adjudication|Investigation|"
    r"Examination|Inspection|Proceedings|National|Bombay|BSE|NSE)"
    # "1. M/s. Parnax Lab Limited (hereinafter referred to as the 'applicant') filed …"
    r"|(?<![\w./-])1\s*[.)]\s+[^.()]{0,150}?[(\[]\s*here\s*in\s*after[^)\]]{0,80}[)\]]\s*,?\s*(?:is|was|has|had|have|"
    r"filed|submitted|are|were|being|a|an|made|vide)\b")
_CUE = re.compile(r"\b(?:in\s+respect\s+of|against|(?:filed|submitted|made|preferred)\s+by|on\s+the\s+application\s+of|"
                  r"relating\s+to(?=\s+M/s)|in\s+the\s+case\s+of|noticees?\s*(?=:))\b\s*[:;.\-–]*\s*", _I)
_TABLE_CUE = re.compile(r"(?:\b(?:Sr|Sl|S)\s*\.?\s*No\.?\s+(?:Name|Noticee|Entity|Applicant)|\bNoticee\s+No\.?\s+(?:Name|Noticee)|"
                        r"\bName\s+of\s+(?:the\s+)?(?:Noticees?|Entit(?:y|ies)|Applicants?))", _I)
_SEG_END = re.compile(
    r"\bin\s+the\s+matters?\s+of\b|\bIN\s+RE\b"
    r"|\(\s*(?:the\s+)?(?:aforesaid|above|said|above[\s-]?mentioned)\b"
    r"|[(\[]\s*(?:hereinafter|here\s*in\s*after)[^)\]]{0,80}?\b(?:collectively|individually|respective)"
    r"|\bunder\s+(?:sub[\s-]*)?(?:sections?|regulations?|rules?)\b"
    r"|\bdate\s+of\s+(?:hearing|order|personal)|\bappearances?\b|\bcoram\b|\bpresent\s*:"
    r"|\bfor\s+(?:the\s+)?(?:noticees?|sebi|applicants?)\s*:|\bBEFORE\s+THE\b|_{8,}|-{10,}|={8,}", _I)
_RESTART = re.compile(r"\b(?:filed|submitted|made|preferred)\s+by\b\s*[:;.\-–]*\s*", _I)


def header_end(flat: str) -> int:
    """Where the order's header ends and its body begins (BACKGROUND, WHEREAS, paragraph 1 …).

    Args:
        flat (str): the order's text, normalised.
    Returns:
        int: the offset of the body's start (at most HEADER_MAX).
    """
    m = _BODY.search(flat, 0, HEADER_MAX)
    return m.start() if m else min(len(flat), HEADER_MAX)


def noticee_segment(flat: str, end: int) -> tuple[int, int] | None:
    """(start, end) of the text naming the noticees in the header.

    Args:
        flat (str): the order's text, normalised.
        end (int): the header's end.
    Returns:
        tuple[int, int] | None: the segment's span, or None if no cue ("In respect of", "Against" …) is found.
    """
    for cue in list(_CUE.finditer(flat, 0, end)) + list(_TABLE_CUE.finditer(flat, 0, end)):
        start = cue.end() if cue.re is _CUE else cue.start()
        stop = _SEG_END.search(flat, start + 3, end)
        seg_end = stop.start() if stop else end
        if seg_end - start < 3:
            continue
        # "In respect of: Application No. 6390/2021 submitted by Shri Aayush Agarwal"
        if r := _RESTART.search(flat, start, min(seg_end, start + 120)):
            start = r.end()
        return start, min(seg_end, start + 15_000)
    return None


@functools.lru_cache(maxsize=2048)
def _marker(k: int, punct: str | None = None) -> re.Pattern:
    """Serial number ``k`` before a name. After the first, it must be written like
    the first ("1." → "2.", "1)" → "2)", "1" → "2"), so "Level 7 Maximus Towers"
    in an address is not entry 7; "Flat No. 2" never is.

    Args:
        k (int): the serial number.
        punct (str | None): how the first serial was written ("." , ")" or ""); None for the first.
    Returns:
        re.Pattern: matches serial k before a name.
    """
    no = "" if k == 1 else r"(?<!No\.\s)(?<!No\s)(?<!Nos\.\s)"
    p = r"\s*[.):\]]?" if punct is None else (rf"\s*{re.escape(punct)}" if punct else "")
    return re.compile(rf"(?<![\w/.,:%&-]){no}(?P<open>\()?{k}(?P<p>{p})\s+(?=M/s|[A-Z\[‘'\"“])")


#: Prose, not a row: many lower-case words ("1. Several advertisements … appeared in …").
def _prose(item: str) -> bool:
    """Whether an entry is running prose rather than a list row.

    Args:
        item (str): an entry's text.
    Returns:
        bool: True if its first 300 characters hold 9 or more lower-case words.
    """
    return len(re.findall(r"\b[a-z]{3,}\b", item[:300])) >= 9


def numbered_items(seg: str) -> list[tuple[int, str]] | None:
    """Entries by serial number 1, 2, 3 … in order (at least two).

    Args:
        seg (str): the noticee segment.
    Returns:
        list[tuple[int, str]] | None: (serial, entry text) per entry, or None if fewer than two.
    """
    marks, pos, punct = [], 0, None
    for k in range(1, 500):
        m = _marker(k, punct).search(seg, pos)
        if not m or (marks and m.start() - marks[-1][1] > 2000):
            break
        if k == 1:
            punct = m.group("p").strip()
        marks.append((m.start(), m.end(), k))
        pos = m.end()
    items = [(k, seg[e:marks[i + 1][0] if i + 1 < len(marks) else len(seg)]) for i, (_, e, k) in enumerate(marks)]
    for j, (_, item) in enumerate(items):
        if _prose(item):
            items = items[:j]
            break
    return items if len(items) >= 2 else None


_HONORIFIC = r"(?:M/s\.?|M/S\.?|Messrs\.?|Shri|Shree|Sri|Smt\.?|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Kum\.?|Kumari|Miss|Master|Late)"
_SPLIT = re.compile(
    rf"\s*;\s*|,\s+(?=(?:and\s+)?{_HONORIFIC}\s)|\s+(?:and|&)\s+(?={_HONORIFIC}\s)"
    r"|(?<=[a-z)\]])\s+(?=(?-i:Mr|Mrs|Ms|Shri|Smt|Dr|M/s)\.?\s)"
    r"|(?:(?<=Ltd)|(?<=Ltd\.)|(?<=Limited)|(?<=LLP)|(?<=HUF)|(?<=LIMITED)|(?<=LTD)|(?<=LTD\.))\s*(?:,|\s+and\s+|\s+&\s+)\s*"
    r"(?!\s*(?:Manager|Member|Director|Promoter|Proprietor|Partner|Sub[\s-]*broker|Stock|Broker|Registered|Trading|Clearing|"
    r"Depository|Merchant|Investment|Research|Portfolio|Registrar|a|an|the|its|having|SEBI|NSE|BSE|National|Bombay|"
    r"Mumbai|Kolkata|Delhi|New\s+Delhi|Chennai|Ahmedabad|Hyderabad|Bangalore|Bengaluru|Pune|Jaipur)\b)",
    _I)


def split_entries(seg: str) -> tuple[str, list[tuple[int | None, str]]]:
    """How the segment lists its parties, and the entries.

    Args:
        seg (str): the noticee segment.
    Returns:
        tuple[str, list[tuple[int | None, str]]]: how the list was split ("numbered", "pan", "list" or
        "single"), and (serial or None, entry text) per entry.
    """
    if items := numbered_items(seg):
        return "numbered", [(k, t) for k, t in items]
    if (m := _marker(1).search(seg)) and _prose(seg[m.end():]):
        seg = seg[:m.start()]  # the first paragraph of the body follows the header
    # With PANs, a party ends at its PAN ("PAN: Not Available" counts); what lies
    # between is its address: "Mr. Vipin Sharma PAN: AIDPS3802K 207, Vardhaman
    # Complex Premises, Co-Operative So. Ltd, LBS Marg …" is one party.
    anchors = list(PAN_ANCHOR.finditer(seg))
    if anchors:
        # an address ends at its PIN code: "… Hyderabad- 500 016 P Venkateswara Rao 7/528, …"
        ends = sorted({a.end() for a in anchors} | {m.end() for m in PIN.finditer(seg)})
        out, prev = [], 0
        for e in ends:
            if seg[prev:e].strip():
                out.append((None, seg[prev:e]))
            prev = e
        tail = seg[prev:]
        if re.match(r"[\s).\]]*(?:,|and\b|&)", tail, _I) and not re.search(r"\d{3}", tail) and clean_entry(tail):
            # "X (PAN: …) and Y"
            out.append((None, tail))
        return ("pan" if len(out) > 1 else "single"), out
    parts = [p for p in split_outside_brackets(seg) if p and p.strip()]
    if len(parts) > 1:
        return "list", [(None, p) for p in parts]
    return "single", [(None, seg)]


def _mask_brackets(s: str) -> str:
    """Replace text inside brackets with underscores (same length), so splits never cut inside them.

    Args:
        s (str): the text.
    Returns:
        str: the masked text.
    """
    out, depth = [], 0
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth:
            depth -= 1
            out.append(ch)
            continue
        out.append("_" if depth and ch not in "([" else ch)
    return "".join(out)


def split_outside_brackets(seg: str) -> list[str]:
    """``_SPLIT`` that never cuts inside brackets: "M/s GSL Nova Petrochemicals
    Limited (Erstwhile M/s Nova Petrochemicals Limited)" is one party.

    Args:
        seg (str): a list of names.
    Returns:
        list[str]: the pieces between separators (commas before honorifics, "and", semicolons …).
    """
    masked = _mask_brackets(seg)
    out, prev = [], 0
    for m in _SPLIT.finditer(masked):
        out.append(seg[prev:m.start()])
        prev = m.end()
    out.append(seg[prev:])
    return out


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

_LEAD_JUNK = re.compile(
    r"^(?:[\s,;:.\-–—)(\]\[|/*•]+|(?:Sr|Sl|S)\s*\.?\s*No\.?|Noticee\s+Nos?\.?|Name\s+of\s+(?:the\s+)?(?:Noticees?|"
    r"Entit(?:y|ies)|Applicants?|Intermediar(?:y|ies)|Company|Persons?|Part(?:y|ies))|Names?\b|Noticees?\b|"
    r"Entit(?:y|ies)\b|Applicants?\b|PAN\b|DIN\b|CIN\b|(?:DIN|CIN)\s*/\s*(?:DIN|CIN)|SEBI\s+Regn?\w*\.?\s*No\.?|"
    r"Registration\s+No\.?|Order\s+No\.?|Authori[sz]ed\s+Representatives?|Address\b|Category\b|Designation\b|"
    r"Details\b|of\s+the\b|and\b|&|:|\d{1,3}\s*[.)]\s|\d{1,3}\s+(?=[A-Z])|[a-h]\s*[.)]\s|\((?:[ivx]{1,4}|[a-h])\)\s|"
    r"[ivx]{1,4}\s*[.)]\s|Permanent\s+Account\s+(?:No|Number)\.?|Order\s+Number|Order\s+No\.?|"
    r"(?:[A-Z]{1,6}/)+[\w./-]*\d[\w./-]*\s)+", _I)
_NAME_STOP = re.compile(
    r"\s*[\[(]\s*(?:PAN|DIN|CIN|SEBI|Regn|Registration|Noticee|Address|Add\.|Regd|Registered|R/o|Office|Consent|"
    r"Settlement|Application|Appeal|SAT\b|Acquirer|PACs?\b|Target|Seller|Buyer|Promoter|Director|N\s*-?\s*\d|hereinafter|here\s*in\s*after|since|now\b|"
    r"formerly|earlier|erstwhile|previously|presently|currently|known|proprietor|partner|a\s|an\s|the\s|through|represented|also|w\.?e\.?f|"
    r"Old|New|Reg|Membership|Code|Client|UCC|Trading|Clearing|Member|Stock|Sub|[\"“‘'])"
    r"|\s*\bPAN\b|\bDIN\b|\bCIN\b|\bSEBI\s+Reg|\bRegistration\s+(?:No|Number)|\bNot\s+Available\b|\bN\.\s?A\.?(?=\s|$)|"
    r"(?-i:\bNA\b)|[A-Z]{3}[PCHFATBLJGE][A-Z]\d{4}[A-Z]|[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}|\b\d{7,8}\b|\bIN[A-Z]\d{6,}|"
    r",\s*(?:having|residing|R/o|resident|address|aged|son|daughter|wife|S/o|D/o|W/o|Member|a\s|an\s|the\s|Sub|Stock|"
    r"Trading|Proprietor|Partner|Director|Promoter|Managing|Chairman|Company|Registered|SEBI|Regd|Merchant|"
    r"Investment|Research|Depository|Registrar|Portfolio|[Ff]ormer|Ex-|CEO|CFO|Compliance|Whole|Executive|"
    r"Independent|Non-|Auditor|Statutory|Chartered|Broker|Manager|Trustee|Sponsor)"
    r"|\s+Prop\.(?=\s)|\s+(?:Flat|Door|Plot|H\.?\s?No|House\s+No|Shop|Room)\b|\s+(?:No|Flat|Door|H\.\s?No|Plot|House|Room|Shop|Office)\s*\.?\s*[:\-]?\s*\d|\s+[A-Z]?-?\d{1,4}\s*[/,]"
    r"|\s+(?:having|residing|R/o|S/o|D/o|W/o|aged|through\s+its|represented\s+by|Proprietor|Did\s+not|"
    r"Director|Promoter|Managing\s+Director|Whole\s*[Tt]ime|Chairman|Compliance|Company\s+Secretary)\b"
    r"|\s+[-–]\s+|\s+In\s+the\s+matter\b|\s+\d{3,}|\s+Rs\.?\s|\s+₹"
    r"|(?<=\.)\s+(?:Broker|Member|Sub[\s-]*broker|Trading\s+Member|SEBI|Registered|Regd|Address)\b"
    r"|\s+Page\s+\d+|\s+(?-i:[A-Z]{2,})/[A-Za-z0-9/-]{3,}"
    r"|\s*,?\s+(?:and|&)\s+(?:its|their|his|her)\s+(?:directors?|promoters?|partners?|proprietors?|officers?|KMPs?)\b"
    r"|\s+(?:and|&)\s+(?:some|other|certain|various|connected|related)\b"
    r"|\s+(?-i:M/[sS])\.?\s", _I)  # a second "M/s": the body has begun
_BAD_NAME = re.compile(
    r"^(?:the\s+)?(?:noticees?|applicants?|entit(?:y|ies)|company|not\s+available|directors?|promoters?|"
    r"preferential\s+allottees?|allottees?|others?|persons?|parties|name|pan|din|cin|sr|sl|no|total|sub-?brokers?|"
    r"stock\s+brokers?|members?|brokers?|intermediar(?:y|ies)|nil|none|various|certain|unknown|all|add|address|"
    r"regd\.?\s+office|registered\s+office|office)$", _I)
_SENTENCE_WORD = re.compile(
    r"\b(?:is|are|was|were|has|have|had|which|that|this|referred|hereinafter|conducted|observed|issued|order|orders|"
    r"regulations?|section|sections|act|pursuant|whereas|matter|investigation|examination|proceedings|notice|"
    r"alleged|violation|hearing|dated|vide)\b|Exchange\s+Board"
    # SEBI's own officers named in the header
    r"|Adjudicating\s+Officer|Legal\s+Advis[eo]r|Whole[\s-]+Time\s+Member|Enquiry\s+Officer|General\s+Manager|"
    r"Presenting\s+Officer|Designated\s+Authority|Recovery\s+Officer", _I)
_ORG_WORDS = re.compile(
    r"\b(?:Ltd|Limited|Pvt|Private|LLP|Corporation|Corp|Company|Co|Inc|Industries|Enterprises?|Securities|Capital|"
    r"Finance|Financial|Finvest|Fincap|Investments?|Holdings?|Traders|Trading|Exports?|Impex|Services|Ventures|"
    r"Infra\w*|Technologies|Tech|Solutions|Associates|Consultants|Consultancy|Bank|Fund|Funds|Trust|Society|"
    r"Exchange|Broking|Brokers|Stock|Shares|Commodities|Estates?|Developers|Projects|Agro|Foods|Pharma\w*|"
    r"Chemicals|Textiles|Motors|Steels?|Power|Energy|Media|Entertainment|Hotels|Resorts|Realty|Properties|"
    r"Builders|Construction|Global|International|Overseas|Group|Research|Advisors?|Advisers?|Advisory|Management|"
    r"Partners|Firm|Syndicate|Credit|Leasing|Mercantile|Marketing|Software|Systems|Communications|Telecom|"
    r"Chartered\s+Accountants|Wealth|Assets?|Insurance|Mills|Sugars?|Papers?|Cements?|Plantations?)\b", _I)
_ISSUER_SUFFIX = re.compile(r"\b(?:Ltd\.?|Limited|Corporation|Company|Industries|LLP)\b", _I)
_SUFFIX_TOKENS = {"LIMITED", "LTD", "PVT", "PRIVATE", "THE", "CO", "COMPANY", "INC", "CORP",
                  "CORPORATION", "LLP"}
#: A company's name ends here; what follows (often misspelt: "Private Limted") is not matched.
_CORE_END = {"PRIVATE", "PVT", "LIMITED", "LTD", "LLP", "INC", "CORP", "CORPORATION"}


_ADDRESS = re.compile(r"^\W*[A-Z]?-?\d+[A-Z]?\s*[,/-]|\b(?:Road|Rd|Marg|Street|St|Nagar|Complex|Floor|Flat|Building|Bldg|"
                      r"Sector|Plot|Lane|Colony|Society|Soc|Apartments?|Towers?|Estate|Chambers|House|Bhavan|Bhawan|"
                      r"Near|Opp|Centre|Center|Park|Village|Dist|District|Taluka|Tehsil|Cross|Block|Phase|Wing|Chowk|"
                      r"Bazar|Bazaar|Gali|Mohalla|Premises)\b", _I)


_TAIL_PUNCT = re.compile(r"[\s,;:.\-–(\[/]+$")
_TAIL_WORD = re.compile(r"\s+(?:and|&|or)$", _I)


def _strip_tail(s: str) -> str:
    """Trailing punctuation and "and" / "&" — in a loop: one regex with nested
    repetition anchored at "$" backtracks exponentially on long entries.

    Args:
        s (str): a name with possible trailing punctuation or "and".
    Returns:
        str: the trimmed name.
    """
    prev = None
    while prev != s:
        prev = s
        s = _TAIL_WORD.sub("", _TAIL_PUNCT.sub("", s))
    return s.strip()


def clean_entry(entry: str) -> str | None:
    """The name at the start of one entry, as printed (trimmed).

    Args:
        entry (str): one header entry (name, then PAN, address, role …).
    Returns:
        str | None: the party's name as printed, or None if the entry holds no valid name.
    """
    s = " ".join(entry[:1200].split())[:600]  # a name is at the start
    # "the past Directors of Sunplant Constructions Limited, viz. Shri Yoganand Prasad"
    if m := re.search(r"\b(?:viz|namely|i\.\s?e)\b\.?\s*[:,]?\s*", s, _I):
        s = s[m.end():]
    prev = None
    while prev != s:
        prev = s
        s = _LEAD_JUNK.sub("", s).strip()
    if m := _NAME_STOP.search(s):
        s = s[:m.start()]
    s = _strip_tail(s)
    if (s.lower().endswith((" ltd", " pvt", " co", " corp", " inc")) or re.search(r"\b[A-Z]$", s)) \
            and " ".join(entry.split()).find(s + ".") >= 0:
        s += "."
    if _ADDRESS.search(s) and (re.search(r"\d", s) or "," in s or re.search(r"\d", " ".join(entry[:200].split()))):
        return None  # "4A, Shree Ganesh Business Centre", "Assetz House, 30 Crescent Road, Bangalore"
    if "/" in s and not re.search(r"[a-z]", s.replace("M/s", "")) and " " not in s.strip():
        return None  # "INB/INF"
    return s if valid_name(s) else None


_OTHER_NAME = re.compile(r"[(\[]\s*(?:formerly|now|earlier|erstwhile|previously|presently|currently)\s+"
                         r"(?:known\s+as|called|named)\s*[:\-]?\s*(?P<n>[^)\]]{3,120})[)\]]", _I)


def other_names(entry: str) -> list[str]:
    """Former / current names: "Uttam Financial Services Ltd. (Presently known as Mansukh Stock Brokers Ltd.)".

    Args:
        entry (str): a header entry.
    Returns:
        list[str]: names given in "(formerly / presently known as …)" brackets.
    """
    return [" ".join(m.group("n").split()).strip(" .,") for m in _OTHER_NAME.finditer(entry)]


def valid_name(s: str | None) -> bool:
    """Whether a string can be a party's name (not a heading, a sentence, an address or SEBI's officer).

    Args:
        s (str | None): the candidate.
    Returns:
        bool: True if it looks like a name.
    """
    if not s or not (3 <= len(s) <= 150) or len(re.findall(r"[A-Za-z]", s)) < 3:
        return False
    if not re.match(rf"(?:{_HONORIFIC}\s*)?[A-Z0-9]", s) or re.match(r"(?:19|20)\d\d\b", s):
        return False
    bare = re.sub(rf"^{_HONORIFIC}\s*", "", s, flags=_I)
    if _BAD_NAME.match(bare) or _SENTENCE_WORD.search(bare) or re.match(r"(?i:the|a|an|its|his|her|their)\s", bare):
        return False
    return len(bare.split()) <= 14 and len(re.findall(r"[A-Za-z]", bare)) >= 2


def name_clean(name: str) -> str:
    """Upper case, honorifics and "(formerly …)" dropped, Ltd → LIMITED, Pvt → PRIVATE.

    Args:
        name (str): a name as printed.
    Returns:
        str: the cleaned upper-case name (``entity_name_clean``).
    """
    s = re.sub(r"[(\[]\s*(?:formerly|now|earlier|erstwhile|previously|presently|currently|since|old|new|known)\b"
               r"[^)\]]*[)\]]?", " ", name, flags=_I)
    s = re.sub(rf"^\s*(?:{_HONORIFIC}\s*)+", "", s, flags=_I)
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\bLTD\b", "LIMITED", s)
    s = re.sub(r"\bPVT\b|\bP\b(?=\s+LIMITED)", "PRIVATE", s)
    return " ".join(s.split())


def entity_type(name: str, pan: str | None) -> str:
    """What kind of party a name is: the PAN's 4th letter first, else the name's words.

    Args:
        name (str): the name.
        pan (str | None): its PAN.
    Returns:
        str: individual, company, firm, huf, trust or other.
    """
    if pan:
        return PAN_TYPE.get(pan[3], "other")
    if re.search(r"\bHUF\b", name, _I):
        return "huf"
    if re.search(r"\bChartered\s+Accountants\b|&\s*(?:Co|Associates)\b|\bLLP\b", name, _I):
        return "firm"
    if re.match(r"\s*M/s\b", name, _I) or _ORG_WORDS.search(name):
        return "company"
    return "individual"


@functools.lru_cache(maxsize=65536)
def name_pattern(clean: str) -> re.Pattern | None:
    """A mention of the party in text: its name tokens with any punctuation between.

    Args:
        clean (str): a cleaned name.
    Returns:
        re.Pattern | None: a pattern for the name in running text, or None for a name too short to match safely.
    """
    toks = []
    for t in clean.split():
        if t in _CORE_END and toks:
            break
        if t not in _SUFFIX_TOKENS:
            toks.append(t)
    if not toks or (len(toks) == 1 and len(toks[0]) < 4):
        return None
    parts = [r"(?:AND|&)" if t == "AND" else re.escape(t) for t in toks]
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\W_]*".join(parts) + r"(?![A-Za-z0-9])", _I)


# --------------------------------------------------------------------------
# Definitions in the body ("X (hereinafter referred to as 'Noticee No. 2')")
# --------------------------------------------------------------------------

_DEFN = re.compile(
    r"[(\[{]\s*(?:hereinafter|here\s*in\s*after|herein\s+after)\s+(?:(?:collectively|individually|also|jointly)\s+)?"
    r"(?:referred\s+to\s+as|called|referred\s+as|to\s+be\s+referred\s+(?:to\s+)?as|mentioned\s+as|known\s+as)?\s*"
    r"(?P<body>[^)\]}]{1,160})[)\]}]", _I)
_DEFN_SHORT = re.compile(r"[(\[]\s*(?P<body>[“\"‘'`]{0,2}\s*(?:the\s+)?(?:Noticee|Applicant)\s*[-.]?\s*(?:No\.?\s*)?"
                         r"(?:\d{1,3})?\s*[”\"’'`]{0,2}(?:\s*/\s*[“\"‘'`]{0,2}[^)\]]{1,40})?)\s*[)\]]", _I)
_QUOTED_ALIAS = re.compile(r"[“\"‘'`]{1,2}\s*(?:the\s+)?(?P<a>[^”\"’'`/]{1,60}?)\s*[”\"’'`]{1,2}")
_NOTICEE_ALIAS = re.compile(r"^(?:the\s+)?(?:Noticee|Applicant|N)\s*[-.]?\s*(?:No\.?\s*)?(?P<k>\d{1,3})?$", _I)
_START_WORDS = {"the", "this", "that", "it", "further", "however", "also", "accordingly", "thereafter", "hence",
                "therefore", "whereas", "sebi", "board", "in", "on", "by", "vide", "as", "and", "of", "one", "said",
                "aforesaid", "noticee", "noticees", "namely", "viz", "viz.", "i.e.", "ie", "being", "entity", "when",
                "where", "which", "who", "his", "her", "its", "their"}
_NAME_TOKEN = re.compile(r"^(?:M/s\.?|[A-Z0-9(\[&][\w.&'()\[\]/+-]*|&)$")
_CONNECTORS = {"and", "&", "of", "de", "da", "del", "la", "van", "von", "bin", "for", "the"}


def name_before(text: str, pos: int) -> str | None:
    """The name ending just before ``pos`` (a definition's bracket).

    Args:
        text (str): the order's text.
        pos (int): where a definition's bracket opens.
    Returns:
        str | None: the name before it, or None.
    """
    chunk = text[max(0, pos - 180):pos].rstrip()
    toks = chunk.split(" ")
    out: list[str] = []
    for t in reversed(toks):
        if not t:
            continue
        if out and t.endswith((",", ";", ":")):
            break
        if re.match(r"^\d+[.,)]?$", t):
            break
        bare = t.strip(",;:")
        if _NAME_TOKEN.match(bare) or (out and bare.lower() in _CONNECTORS):
            out.append(bare)
            if len(out) >= 14:
                break
        else:
            break
    out.reverse()
    # the name starts at an honorific if there is one inside ("However Mr. X")
    for i, t in enumerate(out):
        if re.fullmatch(_HONORIFIC, t, _I):
            out = out[i:]
            break
    while out and (out[0].lower().strip(".") in _START_WORDS or out[0].lower() in _CONNECTORS):
        out = out[1:]
    name = " ".join(out)
    return name if valid_name(name) else None


def definitions(flat: str, limit: int) -> list[tuple[str, list[str], int]]:
    """(name, aliases, position) for every definition before ``limit``.

    Args:
        flat (str): the order's text, normalised.
        limit (int): search only before this offset.
    Returns:
        list[tuple[str, list[str], int]]: (name, aliases defined for it, position), by position.
    """
    out = []
    for rx in (_DEFN, _DEFN_SHORT):
        for m in rx.finditer(flat, 0, limit):
            body = m.group("body")
            aliases = [q.group("a").strip() for q in _QUOTED_ALIAS.finditer(body)]
            if not aliases:
                bare = re.split(r"\s*(?:/|,|\bor\b)\s*", body.strip())[0].strip()
                if len(bare) <= 40:
                    aliases = [bare]
            name = name_before(flat, m.start())
            if name and aliases:
                out.append((name, aliases, m.start()))
    out.sort(key=lambda x: x[2])
    return out


# --------------------------------------------------------------------------
# Matter and title
# --------------------------------------------------------------------------

_MATTER = re.compile(r"\bin\s+the\s+matters?\s+of\s*[:\-–]?\s*", _I)
_MATTER_END = re.compile(r"\s*(?:\(\s*the\s+(?:aforesaid|above)|\(\s*hereinafter|\bin\s+respect\s+of\b|\bagainst\b|"
                         r"\bunder\s+(?:sections?|regulations?)\b|\bPAN\b|_{3,}|-{5,}|\bORDER\b|\bBEFORE\b|"
                         r"\bCORAM\b|\bDate\s+of\b|\bAppearance|\bPage\s+\d+\s+of\s+\d+|"
                         r"\b[A-Z]{2,}/[A-Z0-9/-]{6,})", _I)
_MATTER_PREFIX = re.compile(
    r"^(?:(?:the\s+)?(?:IPO|initial\s+public\s+offer(?:ing)?|GDR\s+issue|public\s+issue|rights\s+issue|issue|"
    r"preferential\s+(?:issue|allotment)|trading|dealings?|investigation|non[\s-]*redressal\s+of\s+investor\s+"
    r"grievances?|irregularit\w+|(?:price\s+)?manipulation|insider\s+trading|buy[\s-]*back|open\s+offer|delisting|"
    r"acquisition\s+of\s+shares|proposed\s+acquisition|failure|disclosures?|compliance|violations?|scheme|"
    r"collective\s+investment\s+schemes?|activities|affairs)\s*(?:in|of|by|at|relating\s+to|made\s+by|under)?\s*"
    r"(?:the\s+)?(?:(?:equity\s+)?shares?|scrip|securities|stock)?\s*(?:of|in|by)?\s*)+", _I)


#: SEBI appends appeal annotations to listing titles ("… Limited [SAT Appeal No.:
#: 440/2020 & Misc. App. No. 510/2020] [SAT Appeal No.: …]", "… 328/2020; MA
#: 372/2020]"); they are not part of the matter (AUDIT_REPORT.md M-3).
_APPEAL_TAIL = re.compile(r"\s*\[?\s*(?:SAT|Civil)\s+Appeal\b.*$"
                          r"|(?:\s+\d+\s*/\s*(?:19|20)\d\d(?:\s*;\s*MA\s+\d+\s*/\s*(?:19|20)\d\d)?\s*\])+\s*$", _I | re.S)


def strip_appeals(title: str | None) -> str | None:
    """Remove SEBI's SAT-appeal annotation from a listing title.

    Args:
        title (str | None): the title.
    Returns:
        str | None: the title without the annotation.
    """
    return _APPEAL_TAIL.sub("", title).strip() if title else title


def matter_from(title: str | None, flat: str, end: int) -> tuple[str | None, str | None]:
    """The order's matter: the title's “in the matter of …” if any, else the header's.

    Args:
        title (str | None): the listing title.
        flat (str): the order's text, normalised.
        end (int): the header's end.
    Returns:
        tuple[str | None, str | None]: (matter text, source "title" or "header"), or (None, None).
    """
    title = strip_appeals(title)
    if title and (ms := list(_MATTER.finditer(title))):
        return title[ms[-1].end():].strip(" .,;-–"), "title"
    if m := _MATTER.search(flat, 0, end):
        stop = _MATTER_END.search(flat, m.end(), m.end() + 300)
        text = flat[m.end():stop.start() if stop else m.end() + 200].strip(" .,;:-–")
        if text:
            return text, "header"
    return None, None


#: What the matter is about, before the company: "Inspection of Books of …", "Front
#: Running Trading activity of Dealers of …", "Issuance of Shares by …", "Financial
#: Statements of …". Only at the start: header matters can run on into the order
#: ("… conducted inspection of the books of accounts of M").
_DESCRIPTOR = re.compile(
    r"^(?:(?:investigation|examination)\s+(?:in(?:to)?|of)\s+)?"
    r"(?:(?:thematic|comprehensive|limited\s+purpose|front[\s-]*running|trading|depository\s+participant|GDR|the|"
    r"equity|preference|redeemable|(?:non[\s-]*)?cumulative|participating|(?:un)?secured|optionally|fully|partly|"
    r"compulsorily|(?:non[\s-]*)?convertible)\s+)*"
    r"(?:inspection|financial\s+statements?|activit(?:y|ies)|conversions?|NCDs?|issuance|books(?:\s+of\s+accounts?)?|"
    r"accounts?\s+and\s+other\s+(?:records|documents)|dealers|shares|GDRs?|debentures|front[\s-]*running|trades)"
    r"(?:\s+issue)?\s+(?:of|by)\s+"
    r"|^(?:stock\s*broker|sub[\s-]*broker|custodian|debenture\s+trustee|depository\s+participant)\s*[-–:]\s*", _I)


def strip_descriptor(s: str) -> str:
    """Remove what the matter is about from its start (“Inspection of Books of …”).

    Args:
        s (str): the matter text.
    Returns:
        str: the text after the descriptors.
    """
    while d := _DESCRIPTOR.match(s):
        s = s[d.end():]
    return s


def issuer_from_matter(matter: str | None) -> str | None:
    """The company the matter is about — the source of ``issuer_guess``.

    Args:
        matter (str | None): the matter text.
    Returns:
        str | None: the company's name ending at its suffix (Ltd, Limited …), or None.
    """
    if not matter:
        return None
    s = re.sub(r"[(\[]\s*(?:formerly|now|earlier|erstwhile|previously)\b[^)\]]*[)\]]?", " ", matter, flags=_I)
    s = strip_descriptor(" ".join(s.split()))
    # "UPSI through WhatsApp with respect to Wipro Ltd" — but "Hasti Finance Ltd. with
    # respect to Mr. Amit Chouhan" names the company first
    if (w := re.search(r"\bwith\s+respect\s+to\s+(?:the\s+)?", s, _I)) and not _ISSUER_SUFFIX.search(s[:w.start()]):
        s = s[w.end():]
    of = list(re.finditer(r"\b(?:scrips?|shares?|securities|stock|IPO|GDR\s+issue|public\s+issue|rights\s+issue|"
                          r"affairs|promoters?|directors?)\s+(?:of|in)\s+(?:the\s+)?", s, _I))
    s = s[of[-1].end():] if of else _MATTER_PREFIX.sub("", s).strip()
    s = re.sub(rf"^{_HONORIFIC}\s*", "", s, flags=_I)
    m = _ISSUER_SUFFIX.search(s)
    if not m:
        return None
    name = s[:m.end()].strip(" ,.;")
    # "Trading Activity of <person> with <FII> Ltd" names two parties, no issuer
    if re.search(r"\bwith\b(?!\s+respect\s+to)", name, _I):
        return None
    return name if valid_name(name) and len(name.split()) <= 10 else None


_TITLE_PARTIES = re.compile(r"\b(?:in\s+respect\s+of|against)\s+(?P<p>.+?)(?=\s+in\s+the\s+matter\s+of\b|$)", _I)
_COUNT_PHRASE = re.compile(r"^\s*(?:\d+|two|three|four|five|six|seven|eight|nine|ten|several|various|certain|"
                           r"multiple|many)\s+(?:entities|noticees|persons|parties|brokers|individuals|companies)\b", _I)


def title_parties(title: str | None) -> list[str]:
    """Parties named in the listing title (“Adjudication Order in respect of X and Y”).

    Args:
        title (str | None): the listing title.
    Returns:
        list[str]: the names (empty for "in respect of 12 entities").
    """
    title = strip_appeals(title)
    if not title or not (m := _TITLE_PARTIES.search(title)) or _COUNT_PHRASE.match(m.group("p")):
        return []
    out = []
    for part in split_outside_brackets(m.group("p")):
        if part and (name := clean_entry(part)):
            out.append(name)
    return out


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

_R_AUDITOR = re.compile(r"chartered\s+accountants?|statutory\s+auditors?|\bauditors?\s+of\b|peer[\s-]+review|"
                        r"audit\s+firm", _I)
_R_INTERMEDIARY = re.compile(
    r"(?:stock|sub|share)[\s-]*brokers?|trading\s+members?|clearing\s+members?|member\s*,?\s+(?:of\s+)?(?:the\s+)?(?:NSE|BSE|"
    r"MCX|NCDEX|NSEL|National\s+Stock|Bombay\s+Stock|Calcutta\s+Stock|[A-Z][a-z]+\s+Stock\s+Exchange|stock\s+exchange)|"
    r"depository\s+participants?|merchant\s+bankers?|portfolio\s+managers?|investment\s+advis[eo]rs?|"
    r"research\s+analysts?|registrars?\s+to\s+(?:an\s+)?issue|share\s+transfer\s+agents?|\bRTA\b|credit\s+rating|"
    r"debenture\s+trustees?|asset\s+management\s+compan|custodians?|underwriters?|bankers?\s+to\s+(?:an\s+)?issue|"
    r"KYC\s+registration|foreign\s+(?:portfolio|institutional)\s+investors?|\bFPIs?\b|\bFIIs?\b|"
    r"authori[sz]ed\s+persons?|registered\s+(?:with\s+SEBI\s+)?as\s+an?\s|SEBI[\s-]+registered|"
    r"certificate\s+of\s+registration", _I)
_R_PROMOTER = re.compile(r"\bpromoters?\b|promoter\s+group", _I)
_R_DIRECTOR = re.compile(r"\b(?:managing\s+director|whole[\s-]*time\s+director|executive\s+director|directors?|"
                         r"chairman|chairperson|MD|CEO|CFO|chief\s+(?:executive|financial|operating)\s+officer|"
                         r"compliance\s+officer|company\s+secretary|key\s+managerial|KMP)\b", _I)
_R_ISSUER = re.compile(r"[“\"‘']\s*(?:the\s+)?(?:company|issuer|target\s+company)\s*[”\"’']|\blisted\s+(?:on|at|with)\b|"
                       r"\bshares\s+(?:are|were|of\s+which\s+are)\s+listed|\btarget\s+company\b|\bthe\s+issuer\b", _I)
_R_TRADER = re.compile(r"\btrad(?:e|ed|es|ing|er|ers)\b|\bbought\b|\bsold\b|\bpurchas\w+|\bdealt\b|\bdealings?\b|"
                       r"\bclients?\b|counter[\s-]*part\w*|synchroni[sz]ed|reversal|\bLTP\b|\bvolumes?\b|\bUCC\b|"
                       r"\bpositions?\b|\bsellers?\b|\bbuyers?\b|off[\s-]*market|transfer(?:or|ee)s?\b|"
                       r"\ballott(?:ee|ees|ed)\b|\bsubscri\w+", _I)
_TRADING_MATTER = re.compile(r"stock\s+options|\bscrip\b|\bshares?\s+of\b|trading|dealings?|manipulat|price\s+rigging|"
                             r"synchroni[sz]ed|circular\s+trad|front[\s-]+running|insider\s+trading|\bIPO\b", _I)


def _same_company(a: str, b: str) -> bool:
    """Whether two names are the same company (suffixes ignored; one may contain the other).

    Args:
        a (str), b (str): two names.
    Returns:
        bool: True if they match.
    """
    ta = [t for t in name_clean(a).split() if t not in _SUFFIX_TOKENS]
    tb = [t for t in name_clean(b).split() if t not in _SUFFIX_TOKENS]
    return bool(ta) and bool(tb) and (ta == tb or (len(ta) >= 2 and " ".join(ta) in " ".join(tb))
                                      or (len(tb) >= 2 and " ".join(tb) in " ".join(ta)))


def guess_role(p: Party, window: str, issuer: str | None, matter: str | None) -> tuple[str, str]:
    """(role, basis). Evidence first from the party's own row, then its first mention in the body.

    Args:
        p (Party): the party.
        window (str): text around its first mention in the body.
        issuer (str | None): the issuer guessed from the matter.
        matter (str | None): the matter text.
    Returns:
        tuple[str, str]: (role, basis) — role ISSUER, PROMOTER, DIRECTOR, AUDITOR, INTERMEDIARY, TRADER or
        UNKNOWN; basis is the evidence (registration_no, matter, din, list, context:<word>, suffix, none …).
    """
    row = p.rest or ""
    if re.search(r"chartered\s+accountants?", p.name, _I):
        return AUDITOR, "name"
    if p.regno:
        return INTERMEDIARY, "registration_no"
    if p.hint:
        return p.hint, "list"
    if m := _R_AUDITOR.search(row) or _R_AUDITOR.search(window):
        return AUDITOR, f"context:{m.group(0).lower()}"
    if issuer and p.entity_type in ("company", "firm", "other") and _same_company(p.name, issuer):
        return ISSUER, "matter"
    if m := _R_PROMOTER.search(row) or _R_PROMOTER.search(window):
        return PROMOTER, f"context:{m.group(0).lower()}"
    if p.din and p.entity_type == "individual":
        return DIRECTOR, "din"
    if p.entity_type == "individual" and (m := _R_DIRECTOR.search(row) or _R_DIRECTOR.search(window)):
        return DIRECTOR, f"context:{m.group(0).lower()}"
    if m := _R_INTERMEDIARY.search(row) or _R_INTERMEDIARY.search(window):
        return INTERMEDIARY, f"context:{m.group(0).lower()}"
    if p.entity_type == "company" and _R_ISSUER.search(window):
        return ISSUER, "context:company"
    if m := _R_TRADER.search(window):
        return TRADER, f"context:{m.group(0).lower()}"
    if matter and p.entity_type == "individual" and (m := re.search(r"\b(promoters?|directors?)\b", matter, _I)):
        return (PROMOTER if m.group(1).lower().startswith("promoter") else DIRECTOR), "matter"
    if matter and _TRADING_MATTER.search(matter):
        return TRADER, "trading_matter"
    if p.entity_type == "company":
        return ISSUER, "suffix"
    return UNKNOWN, "none"


# --------------------------------------------------------------------------
# Linking sanctions to parties
# --------------------------------------------------------------------------

_NOTICEE_NOS = re.compile(r"\b(?:Noticee|Applicant)(?:s|\(s\))?\s*(?:Nos?\.?|Numbers?)?\s*"
                          r"(?P<l>\d{1,3}(?:\s*(?:,|and|&|to|-|–)\s*\d{1,3})*)\b", _I)
_PLURAL_GENERIC = re.compile(r"\b(?:noticees|applicants)\b|\b(?:all\s+(?:the\s+)?|each\s+of\s+the\s+|"
                             r"the\s+(?:said\s+|above\s+|aforesaid\s+)?)(?:entities|persons|parties)\b", _I)


def noticee_numbers(text: str) -> list[tuple[int, int, set[int]]]:
    """"Noticee Nos. 1 to 3 and 5" → (start, end, {1, 2, 3, 5}).

    Args:
        text (str): a sentence.
    Returns:
        list[tuple[int, int, set[int]]]: (start, end, the numbers) per "Noticee No(s)." mention.
    """
    out = []
    for m in _NOTICEE_NOS.finditer(text):
        nums: set[int] = set()
        toks = re.findall(r"\d{1,3}|to|-|–", m.group("l"))
        i = 0
        while i < len(toks):
            if i + 2 < len(toks) and toks[i + 1] in ("to", "-", "–") and toks[i].isdigit() and toks[i + 2].isdigit():
                a, b = int(toks[i]), int(toks[i + 2])
                if 0 < a <= b <= a + 400:
                    nums.update(range(a, b + 1))
                i += 3
            else:
                if toks[i].isdigit():
                    nums.add(int(toks[i]))
                i += 1
        out.append((m.start(), m.end(), nums))
    return out


@dataclass
class Party:
    """A party found in the order (before the output row is built).

    Attributes:
        name (str), source (str): where it was found; noticee_no (int | None); pan, din, cin, regno (str | None);
        rest (str | None): the rest of its header entry; hint (str | None): role stated by the list;
        org (bool): named as "X and its directors"; aliases (list[str]); entity_type (str); clean (str).
    """
    name: str
    source: str                       # header_numbered / header_pan / header_list / header / definition / title
    noticee_no: int | None = None
    pan: str | None = None
    din: str | None = None
    cin: str | None = None
    regno: str | None = None
    rest: str | None = None           # the rest of its header entry (row cells, role words)
    hint: str | None = None           # role stated by the list itself ("and its Directors viz. …")
    org: bool = False                 # "X and its directors": X is a company
    aliases: list[str] = field(default_factory=list)
    entity_type: str = "individual"
    clean: str = ""

    def patterns(self) -> list[re.Pattern]:
        """Every way the party may be named in the text: name, PAN, aliases, “Noticee No. k”.

        Returns:
            list[re.Pattern]: the patterns.
        """
        out = []
        if p := name_pattern(self.clean):
            out.append(p)
        if self.pan:
            out.append(re.compile(re.escape(self.pan)))
        for a in self.aliases:
            if (m := _NOTICEE_ALIAS.match(a)) and m.group("k"):
                out.append(re.compile(rf"\b(?:Noticee|Applicant)\s*[-.]?\s*(?:No\.?\s*)?{int(m.group('k'))}\b(?!\s*(?:to|-|–|,)\s*\d)", _I))
            elif not _NOTICEE_ALIAS.match(a) and len(a) >= 2 and not _BAD_NAME.match(a) and a.lower() not in _START_WORDS:
                out.append(re.compile(rf"(?<![\w]){re.escape(a)}(?![\w])"))
        if self.noticee_no:
            out.append(re.compile(rf"\bNoticee\s*[-.]?\s*(?:No\.?\s*)?{self.noticee_no}\b(?!\s*(?:to|-|–|,)\s*\d)", _I))
        return out


def mentions(parties: list[Party], text: str) -> list[tuple[int, int]]:
    """(position, party index) of every party named in ``text``, in order.

    Args:
        parties (list[Party]): the order's parties.
        text (str): a sentence.
    Returns:
        list[tuple[int, int]]: (offset, party index) per mention, sorted.
    """
    found = []
    for i, p in enumerate(parties):
        for rx in p.patterns():
            for m in rx.finditer(text):
                found.append((m.start(), i))
    by_no = {p.noticee_no: i for i, p in enumerate(parties) if p.noticee_no}
    for s, _, nums in noticee_numbers(text):
        for n in nums:
            if n in by_no:
                found.append((s, by_no[n]))
    return sorted(set(found))


@dataclass
class Link:
    """How an order's penalty rows were linked to its parties.

    Attributes:
        amounts (dict[int, int]): party index → rupees; rows (dict[int, int]): party index → rows linked;
        each (set[int]), joint (set[int]): parties with an "each" / joint amount;
        unassigned (int): rupees linked to no one; n_rows (int), n_linked (int);
        expanded_total (int): the total with each "… each" amount counted once per party named.
    """
    amounts: dict[int, int] = field(default_factory=dict)        # party index → rupees
    rows: dict[int, int] = field(default_factory=dict)           # party index → rows linked
    each: set[int] = field(default_factory=set)
    joint: set[int] = field(default_factory=set)
    unassigned: int = 0
    n_rows: int = 0
    n_linked: int = 0
    #: the order's total with every "each" amount counted once per party it names
    expanded_total: int = 0

    def add(self, i: int, rupees: int, each: bool, joint: bool):
        """Credit an amount to a party.

        Args:
            i (int): the party index.
            rupees (int): the amount.
            each (bool), joint (bool): the amount's marks.
        Returns:
            None.
        """
        self.amounts[i] = self.amounts.get(i, 0) + rupees
        self.rows[i] = self.rows.get(i, 0) + 1
        if each:
            self.each.add(i)
        if joint:
            self.joint.add(i)


def link_penalties(parties: list[Party], rows: list[ea.SanctionMention]) -> Link:
    """Give each penalty row to the party it names (table layout: before; prose: after).

    Args:
        parties (list[Party]): the order's parties.
        rows (list[extract_actions.SanctionMention]): the counted penalty rows.
    Returns:
        Link: amounts per party and how complete the linking was.
    """
    link = Link(n_rows=len(rows))
    if not parties or not rows:
        return link
    if len(parties) == 1:
        for r in rows:
            link.add(0, r.amount_rupees, r.each, r.joint)
            link.expanded_total += r.amount_rupees
        link.n_linked = len(rows)
        return link
    by_sentence: dict[str, list[ea.SanctionMention]] = {}
    for r in rows:
        by_sentence.setdefault(r.sentence, []).append(r)
    for sentence, items in by_sentence.items():
        items = sorted(items, key=lambda r: r.amount_pos or 0)
        spans = [(r.amount_pos or 0, (r.amount_pos or 0) + len(r.amount_text or "")) for r in items]
        found = mentions(parties, sentence)
        # Prose names the party after the amount ("… Rs. 5 lakh on Noticee No. 2":
        # Day 4 kept that text as who_text); a table names it before.
        first_start, first_end = spans[0]
        after = spans[1][0] if len(spans) > 1 else first_end + 250
        prose = bool(items[0].who_text) and any(first_end <= pos < after for pos, _ in found)
        layout_before = not prose and any(first_start - 250 <= pos < first_start for pos, _ in found)
        prev_party: list[int] = []
        # prose: "Rs. 10 lakh under 15HA and Rs. 1 lakh under 15HB on ISF": an amount
        # naming no one belongs with the next amount's party
        pending: list[ea.SanctionMention] = []
        for j, r in enumerate(items):
            start, end = spans[j]
            if layout_before:
                lo = spans[j - 1][1] if j else max(0, start - 250)
                named = [i for pos, i in found if lo <= pos < start]
            else:
                hi = spans[j + 1][0] if j + 1 < len(items) else min(len(sentence), end + 250)
                named = [i for pos, i in found if end <= pos < hi]
            if not named and layout_before and prev_party:
                named = prev_party  # "Rs. 30,00,000/- and Rs. 10,00,000/-" on the same row
            joint = r.joint
            if not named and _PLURAL_GENERIC.search(r.who_text or sentence[end:end + 80]):
                named = list(range(len(parties)))
                joint = joint or not r.each  # "Rs. 3 lakh on all the Noticees": one penalty, all liable
            if not named:
                if layout_before:
                    link.unassigned += r.amount_rupees
                    link.expanded_total += r.amount_rupees
                else:
                    pending.append(r)
                prev_party = []
                continue
            targets = list(dict.fromkeys(named)) if (r.each or joint) else [named[-1] if layout_before else named[0]]
            for x in pending + [r]:
                for i in targets:
                    link.add(i, x.amount_rupees, x.each, x.joint or joint)
                link.expanded_total += x.amount_rupees * (len(targets) if x.each else 1)
                # "Rs. 5 lakh each on A and B" found only A: not resolved
                link.n_linked += not (x.each and len(targets) < 2)
            pending = []
            prev_party = targets
        for x in pending:
            link.unassigned += x.amount_rupees
            link.expanded_total += x.amount_rupees
    return link


def link_other(parties: list[Party], sanctions: list[ea.SanctionMention]) -> dict[int, set[str]]:
    """Non-monetary sanctions (and settlement / disgorgement) per party.

    Args:
        parties (list[Party]): the order's parties.
        sanctions (list[extract_actions.SanctionMention]): all its sanction mentions.
    Returns:
        dict[int, set[str]]: party index → sanction types (DEBARMENT, DISGORGEMENT, WARNING, SETTLEMENT,
        SUSPENSION, CANCELLATION).
    """
    names = {ea.DEBARMENT: "DEBARMENT", ea.DISGORGEMENT: "DISGORGEMENT", ea.WARNING: "WARNING",
             ea.SETTLEMENT: "SETTLEMENT"}
    out: dict[int, set[str]] = collections.defaultdict(set)
    if not parties:
        return out
    for s in sanctions:
        kind = names.get(s.kind) or ({"suspension": "SUSPENSION", "cancellation": "CANCELLATION"}.get(s.action)
                                     if s.kind == ea.REGISTRATION else None)
        if not kind:
            continue
        if len(parties) == 1:
            out[0].add(kind)
            continue
        named = {i for _, i in mentions(parties, s.sentence)}
        if not named and _PLURAL_GENERIC.search(s.sentence):
            named = set(range(len(parties)))
        for i in named:
            out[i].add(kind)
    return out


# --------------------------------------------------------------------------
# Per order
# --------------------------------------------------------------------------

@dataclass
class Job:
    """One order to read, with its title and Day 4's sanctions and penalty total.

    Attributes:
        text_stem (str), order_id (int | None), order_type (str | None), order_date (datetime.date | None),
        text_path (str | None), text_status (str | None), title (str | None),
        sanctions (list[dict]): its rows of sanction_mentions; penalty_total (int | None),
        penalty_total_basis (str | None).
    """
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    text_path: str | None
    text_status: str | None
    title: str | None = None
    sanctions: list = field(default_factory=list)   # dicts from sanction_mentions
    penalty_total: int | None = None
    penalty_total_basis: str | None = None


@dataclass
class EntityRow:
    """One (order, party): a row of ``derived/order_entities.parquet`` (columns in CLAUDE.md §8).

    Attributes:
        party_no (int), noticee_no (int | None), entity_name, entity_name_clean, entity_type (str),
        pan, din, cin, sebi_reg_no (str | None), entity_role, role_basis (str), is_issuer_guess (bool),
        source (str), aliases (str, JSON), penalty_amount (int | None), penalty_rows (int),
        penalty_each (bool), penalty_joint (bool), sanction_types (str, JSON).
    """
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    party_no: int
    noticee_no: int | None
    entity_name: str
    entity_name_clean: str
    entity_type: str
    pan: str | None
    din: str | None
    cin: str | None
    sebi_reg_no: str | None
    entity_role: str
    role_basis: str
    is_issuer_guess: bool
    source: str
    aliases: str                      # JSON list
    penalty_amount: int | None
    penalty_rows: int
    penalty_each: bool
    penalty_joint: bool
    sanction_types: str               # JSON list
    rules_version: str = RULES_VERSION


@dataclass
class RegulationMention:
    """One provision cited: a row of ``derived/regulation_mentions.parquet``.

    Attributes:
        text_stem (str), regime (str), regime_year (int | None), provision (str), keyword (str),
        role (str): violation, penalty, power or cited; how (str); rel_pos (float); citation (str);
        context (str): 120 characters before it.
    """
    text_stem: str
    regime: str
    regime_year: int | None
    provision: str
    keyword: str
    role: str
    how: str
    rel_pos: float
    citation: str
    context: str


@dataclass
class OrderParties:
    """One order's parties summary: a row of ``derived/order_parties.parquet`` (CLAUDE.md §8).

    Attributes:
        matter_raw, matter_source, issuer_guess, issuer_source, parties_source (str | None); n_parties (int);
        n_parties_by_role (str, JSON); parties_raw (str | None); penalty_link_status (str | None);
        penalty_rows_n, penalty_rows_linked (int); penalty_unassigned, penalty_total_resolved (int | None);
        regulations, regimes, regimes_cited, penalty_provisions (str, JSON lists); n_citations,
        n_citations_unresolved (int); rules_version; error.
    """
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: dt.date | None
    text_status: str | None
    matter_raw: str | None = None
    matter_source: str | None = None
    issuer_guess: str | None = None
    issuer_source: str | None = None
    parties_source: str | None = None
    n_parties: int = 0
    n_parties_by_role: str | None = None     # JSON {"TRADER": 1}
    parties_raw: str | None = None           # the header segment the list came from
    penalty_link_status: str | None = None   # single_party / all_rows / partial / none
    penalty_rows_n: int = 0
    penalty_rows_linked: int = 0
    penalty_unassigned: int | None = None
    penalty_total_resolved: int | None = None
    regulations: str = "[]"                  # JSON: provisions violated, "PFUTP 3(a)"
    regimes: str = "[]"                      # JSON: instruments violated
    regimes_cited: str = "[]"                # JSON: every substantive instrument cited
    penalty_provisions: str = "[]"           # JSON: "SEBI Act 15HA"
    n_citations: int = 0
    n_citations_unresolved: int = 0
    rules_version: str = RULES_VERSION
    error: str | None = None


def _identifiers(p: Party, entry: str, din_column: bool) -> None:
    """Read PAN, CIN, DIN and SEBI registration number from a header entry onto a party.

    Args:
        p (Party): the party (changed in place).
        entry (str): its header entry.
        din_column (bool): the list has a DIN column (a bare 8-digit number is then a DIN).
    Returns:
        None.
    """
    if m := PAN.search(entry):
        p.pan = m.group(0)
    if m := CIN.search(entry):
        p.cin = re.sub(r"\s", "", m.group(0))
    if m := DIN_LABELLED.search(entry):
        p.din = m.group(1)
    elif din_column and (m := DIN_BARE.search(entry)):
        p.din = m.group(1)
    if m := REGNO.search(entry):
        p.regno = re.sub(r"\s", "", m.group(0))


_GROUP = re.compile(r"\s*,?\s*(?:and\s+)?(?:its|their)\s+(?:(?:erstwhile|past|former)\s+)?(?P<g>directors?|promoters?|"
                    r"partners?|KMPs?)\s*,?\s*(?:viz\.?|namely|i\.\s?e\.?|:)?\s*[:,]?\s*", _I)


def header_parties(flat: str, hend: int) -> tuple[list[Party], str | None, str | None]:
    """The parties listed in the header, with their identifiers.

    Args:
        flat (str): the order's text, normalised.
        hend (int): the header's end.
    Returns:
        tuple[list[Party], str | None, str | None]: the parties, how the list was split, and the segment's
        first 1,500 characters (``parties_raw``).
    """
    seg = noticee_segment(flat, hend)
    if not seg:
        return [], None, None
    text = flat[seg[0]:seg[1]]
    how, entries = split_entries(text)
    din_column = bool(re.search(r"\bDIN\b", text[:400]))
    parties: list[Party] = []
    expanded: list[tuple[int | None, str, str | None]] = []
    carry = None
    for k, entry in entries:
        g = _GROUP.search(entry)
        hint = (PROMOTER if g.group("g").lower().startswith("promoter") else DIRECTOR) if g else None
        if g and clean_entry(entry[g.end():]):
            expanded.append((k, entry[:g.start()], "ORG"))
            expanded.extend((None, part, hint) for part in split_outside_brackets(entry[g.end():]))
        elif g:
            expanded.append((k, entry[:g.start()], "ORG" if clean_entry(entry[:g.start()]) else carry))
            carry = hint  # the entries that follow are its directors / promoters
            continue
        else:
            expanded.append((k, entry, carry))
    for k, entry, hint in expanded:
        name = clean_entry(entry)
        if not name:
            if parties and how == "list":  # "M/s Shreeji Finvest Ltd., MEMBER, NATIONAL STOCK EXCHANGE …"
                parties[-1].rest = ((parties[-1].rest or "") + " " + entry)[:400]
            elif parties and how == "pan" and (m := PAN.search(entry)) and not parties[-1].pan:
                parties[-1].pan = m.group(0)  # "Vikram Farms Pvt Ltd (Address – …) (PAN: AAACV9642C)"
            continue
        p = Party(name, f"header_{how}" if how != "single" else "header", noticee_no=k,
                  hint=None if hint == "ORG" else hint, org=hint == "ORG")
        _identifiers(p, entry, din_column)
        flat_entry = " ".join(entry.split())
        rest_at = flat_entry.find(name)
        p.rest = flat_entry[rest_at + len(name):][:300] if rest_at >= 0 else flat_entry[:300]
        p.aliases.extend(other_names(flat_entry))
        parties.append(p)
    return parties, how, text[:1500]


def _dedupe(parties: list[Party]) -> list[Party]:
    """Merge parties with the same cleaned name (their aliases and identifiers combined).

    Args:
        parties (list[Party]): the parties found.
    Returns:
        list[Party]: one per cleaned name, in first-seen order.
    """
    seen: dict[str, Party] = {}
    out = []
    for p in parties:
        if p.clean in seen:
            q = seen[p.clean]
            q.aliases.extend(a for a in p.aliases if a not in q.aliases)
            q.pan, q.din, q.cin, q.regno = q.pan or p.pan, q.din or p.din, q.cin or p.cin, q.regno or p.regno
            continue
        seen[p.clean] = p
        out.append(p)
    return out


def process(job: Job, repo_root: pathlib.Path = REPO_ROOT):
    """Parties, roles, sanctions per party and regulations for one order. Never raises.

    Args:
        job (Job): the order.
        repo_root (pathlib.Path): base for its text path.
    Returns:
        tuple[OrderParties, list[EntityRow], list[RegulationMention]]: the order row, its parties and its
        citations.
    """
    o = OrderParties(job.text_stem, job.order_id, job.order_type, job.order_date, job.text_status)
    if job.text_status != "OK" or not job.text_path:
        return o, [], []
    try:
        text = (repo_root / job.text_path).read_text(encoding="utf-8")
        stripped = ew.strip_repeated_lines(text)
        flat = ew.normalise_sentence(stripped)
        hend = header_end(flat)

        # ---- parties
        parties, how, raw = header_parties(flat, hend)
        o.parties_raw = raw
        defs = definitions(flat, max(hend, int(len(flat) * 0.4)))
        for p in parties:
            p.clean = name_clean(p.name)
        if not parties:
            for name, aliases, _ in defs:
                if any(_NOTICEE_ALIAS.match(a) for a in aliases):
                    k = next((int(m.group("k")) for a in aliases if (m := _NOTICEE_ALIAS.match(a)) and m.group("k")),
                             None)
                    parties.append(Party(name, "definition", noticee_no=k, clean=name_clean(name)))
        if not parties:
            parties = [Party(n, "title", clean=name_clean(n)) for n in title_parties(job.title)]
        if not parties and job.order_type in ("AO", "SETTLEMENT", "ED_CGM"):
            # "ORDER … IN THE MATTER OF HANUMAN SHARES AND STOCK BROKERS LTD 1. I was appointed …"
            m_text, _ = matter_from(job.title, flat, hend)
            if (c := issuer_from_matter(m_text)) and \
                    name_clean(c) == name_clean(_MATTER_PREFIX.sub("", strip_descriptor(m_text or ""))):
                parties = [Party(c, "matter", clean=name_clean(c))]
        if not parties:
            ms = list(_MATTER.finditer(flat, 0, hend))
            if len(ms) >= 2:
                first = flat[ms[0].end():ms[1].start()]
                parties = [Party(n, "header_matter", clean=name_clean(n))
                           for n in (clean_entry(x) for _, x in split_entries(first)[1]) if n]
        # attach the order's own aliases ("SVAHCL", "Noticee No. 2") to parties
        for name, aliases, _ in defs:
            c = name_clean(name)
            for p in parties:
                if c == p.clean or (name_pattern(p.clean) and name_pattern(p.clean).search(name)):
                    p.aliases.extend(a for a in aliases if a not in p.aliases)
                    if not p.noticee_no:
                        p.noticee_no = next((int(m.group("k")) for a in aliases
                                             if (m := _NOTICEE_ALIAS.match(a)) and m.group("k")), None)
                    break
        for p in parties:
            if not p.pan and p.source.startswith("header") and len(parties) == 1 and (m := PAN.search(flat, 0, hend)):
                p.pan = m.group(0)
            p.entity_type = "company" if p.org and not p.pan else entity_type(p.name, p.pan)
        parties = _dedupe(parties)
        o.parties_source = parties[0].source if parties else None
        o.n_parties = len(parties)

        # ---- matter, issuer, roles
        o.matter_raw, o.matter_source = matter_from(job.title, flat, hend)
        issuer = issuer_from_matter(o.matter_raw)
        o.issuer_guess, o.issuer_source = (issuer, "matter") if issuer else (None, None)
        body = flat[hend:]
        roles: list[tuple[str, str]] = []
        for i, p in enumerate(parties):
            window = ""
            if i < ROLE_WINDOW_PARTIES and (rx := name_pattern(p.clean)) and (m := rx.search(body)):
                w0 = max(0, m.start() - 80)
                window = body[w0:m.end() + 250]
                # stop at the next party named after it
                for q in parties[:ROLE_WINDOW_PARTIES]:
                    if q is not p and (qx := name_pattern(q.clean)) and (qm := qx.search(window, m.end() - w0)):
                        window = window[:qm.start()]
            roles.append(guess_role(p, window, issuer, o.matter_raw))
        if not issuer:
            first = next((p for p, (r, b) in zip(parties, roles) if r == ISSUER and b != "suffix"), None)
            if first:
                o.issuer_guess, o.issuer_source = first.name, "party"

        # ---- sanctions per party
        sanctions = [ea.SanctionMention(**s) for s in job.sanctions]
        pen = [s for s in sanctions if s.kind == ea.PENALTY and s.amount_rupees]
        rows = ea.penalty_rows(pen) or [x for x in pen if x.is_total]  # only a Total stated: Day 4 uses it too
        link = link_penalties(parties, rows)
        if len(parties) == 1 and job.penalty_total_basis == "stated_total" and job.penalty_total:
            link.amounts[0] = job.penalty_total  # one party carries the order's stated total (14949)
        other = link_other(parties, sanctions)
        o.penalty_rows_n, o.penalty_rows_linked = link.n_rows, link.n_linked
        if rows:
            o.penalty_unassigned = link.unassigned
            o.penalty_link_status = ("single_party" if len(parties) == 1 else "none" if not link.n_linked
                                     else "all_rows" if link.n_linked == link.n_rows else "partial")
            o.penalty_total_resolved = job.penalty_total
            if job.penalty_total_basis == "rows_each_unresolved" and link.n_linked == link.n_rows:
                # "Rs. 5 lakh each" resolved: counted once per party it names
                o.penalty_total_resolved = link.expanded_total
        entities = []
        role_counts: collections.Counter = collections.Counter()
        for i, p in enumerate(parties):
            role, basis = roles[i]
            role_counts[role] += 1
            types = sorted(other.get(i, set()) | ({"PENALTY"} if i in link.amounts else set()))
            entities.append(EntityRow(
                job.text_stem, job.order_id, job.order_type, job.order_date, i + 1, p.noticee_no, p.name, p.clean,
                p.entity_type, p.pan, p.din, p.cin, p.regno, role, basis,
                bool(o.issuer_guess) and _same_company(p.name, o.issuer_guess) and p.entity_type != "individual",
                p.source, json.dumps(p.aliases[:10], ensure_ascii=False), link.amounts.get(i), link.rows.get(i, 0),
                i in link.each, i in link.joint, json.dumps(types)))
        o.n_parties_by_role = json.dumps(dict(sorted(role_counts.items())))

        # ---- regulations
        aliases = rg.find_aliases(flat)
        n = max(len(stripped), 1)
        cites: list[rg.Citation] = []
        mentions_out: list[RegulationMention] = []
        for s, e in ew.split_sentences(stripped):
            sentence = ew.normalise_sentence(stripped[s:e])
            found, unresolved = rg.find_citations(sentence, aliases)
            o.n_citations_unresolved += unresolved
            for c in found:
                cites.append(c)
                mentions_out.append(RegulationMention(job.text_stem, c.regime, c.year, c.provision, c.keyword, c.role,
                                                      c.how, s / n, c.text[:300],
                                                      sentence[max(0, c.start - 120):c.start]))
        o.n_citations = len(cites)
        o.regulations = json.dumps(rg.as_list(cites))
        o.regimes = json.dumps(rg.regimes(cites))
        o.regimes_cited = json.dumps(rg.regimes([c for c in cites if c.regime not in rg.PROCEDURAL],
                                                roles=("violation", "cited", "penalty")))
        o.penalty_provisions = json.dumps(rg.as_list(cites, roles=("penalty",)))
        return o, entities, mentions_out
    except Exception as exc:  # pragma: no cover - last-resort guard for a long run
        o.error = f"{type(exc).__name__}: {exc}"[:300]
        return o, [], []


def _process_many(jobs, repo_root):
    """Process a batch of orders (one worker task).

    Args:
        jobs (list[Job]): the batch.
        repo_root (pathlib.Path): base for text paths.
    Returns:
        list[tuple[OrderParties, list[EntityRow], list[RegulationMention]]]: one result per order.
    """
    return [process(j, repo_root) for j in jobs]


# --------------------------------------------------------------------------
# Input / output
# --------------------------------------------------------------------------

def load_jobs(documents: pathlib.Path = TEXT_DOCUMENTS_IN, listings: pathlib.Path = LISTINGS_IN,
              sanctions: pathlib.Path = SANCTIONS_IN, actions: pathlib.Path = ACTIONS_IN) -> list[Job]:
    """Read the orders with their titles, Day 4 sanctions and penalty totals.

    Args:
        documents (pathlib.Path): ``derived/text_documents.parquet``.
        listings (pathlib.Path): ``derived/listings.parquet``.
        sanctions (pathlib.Path): ``derived/sanction_mentions.parquet`` (optional).
        actions (pathlib.Path): ``derived/order_actions.parquet`` (optional).
    Returns:
        list[Job]: one per order.
    """
    docs = pq.read_table(documents, columns=["text_stem", "order_id", "order_type", "order_date",
                                             "text_path", "status"]).to_pylist()
    titles = {}
    for r in pq.read_table(listings, columns=["order_id", "source_url", "title"]).to_pylist():
        titles[text_stem(r["order_id"], r["source_url"])] = r["title"]
    by_stem: dict[str, list] = collections.defaultdict(list)
    if sanctions.exists():
        for r in pq.read_table(sanctions).to_pylist():
            by_stem[r["text_stem"]].append(r)
    totals = {}
    if actions.exists():
        for r in pq.read_table(actions, columns=["text_stem", "penalty_total", "penalty_total_basis"]).to_pylist():
            totals[r["text_stem"]] = (r["penalty_total"], r["penalty_total_basis"])
    return [Job(r["text_stem"], r["order_id"], r["order_type"], r["order_date"], r["text_path"], r["status"],
                titles.get(r["text_stem"]), by_stem.get(r["text_stem"], []),
                *totals.get(r["text_stem"], (None, None))) for r in docs]


def select(jobs, *, categories=None, order_ids=None, stems=None, sample=None, limit=None, seed=7):
    """Restrict the orders (types, ids, stems, a random sample, the first N).

    Args:
        jobs (list[Job]): all orders.
        categories, order_ids, stems (list | None): keep only these.
        sample (int | None), limit (int | None), seed (int).
    Returns:
        list[Job]: the selection.
    """
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


def write_outputs(orders, entities, mentions_out, out_dir: pathlib.Path) -> list[pathlib.Path]:
    """Write ``order_parties``, ``order_entities`` and ``regulation_mentions``.

    Args:
        orders (list[OrderParties]), entities (list[EntityRow]), mentions_out (list[RegulationMention]).
        out_dir (pathlib.Path): ``derived/`` or a scratch directory.
    Returns:
        list[pathlib.Path]: the three paths written.
    """
    assert_safe_output(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / "order_parties.parquet", out_dir / "order_entities.parquet",
             out_dir / "regulation_mentions.parquet"]
    ea._write(sorted(orders, key=lambda o: o.text_stem), OrderParties, paths[0])
    ea._write(sorted(entities, key=lambda e: (e.text_stem, e.party_no)), EntityRow, paths[1])
    ea._write(sorted(mentions_out, key=lambda m: (m.text_stem, m.rel_pos)), RegulationMention, paths[2])
    return paths


def run(jobs, *, workers=1, repo_root=REPO_ROOT, chunk=100):
    """Process every order (in worker processes when workers > 1).

    Args:
        jobs (list[Job]): the orders.
        workers (int): processes.
        repo_root (pathlib.Path): base for text paths.
        chunk (int): orders per worker task.
    Returns:
        tuple[list[OrderParties], list[EntityRow], list[RegulationMention]]: all rows.
    """
    orders, entities, mentions_out = [], [], []

    def collect(res):
        """Gather one order's result.

        Args:
            res (tuple[OrderParties, list[EntityRow], list[RegulationMention]]): one order's output.
        Returns:
            None.
        """
        o, e, m = res
        orders.append(o)
        entities.extend(e)
        mentions_out.extend(m)

    if workers <= 1:
        for j in jobs:
            collect(process(j, repo_root))
        return orders, entities, mentions_out
    batches = [jobs[i:i + chunk] for i in range(0, len(jobs), chunk)]
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process_many, b, repo_root) for b in batches]
        for done, fut in enumerate(concurrent.futures.as_completed(futures), start=1):
            for res in fut.result():
                collect(res)
            if done % 40 == 0 or done == len(futures):
                log.info("%d/%d orders", len(orders), len(jobs))
    return orders, entities, mentions_out


def summarise(orders: list[OrderParties], entities: list[EntityRow]) -> dict:
    """Run summary for the log.

    Args:
        orders (list[OrderParties]): order rows.
        entities (list[EntityRow]): party rows.
    Returns:
        dict: parties found, roles, types, PANs, penalty-link status, regulations and top regimes.
    """
    with_text = [o for o in orders if o.text_status == "OK"]
    by_type = collections.defaultdict(collections.Counter)
    for o in with_text:
        c = by_type[o.order_type]
        c["orders"] += 1
        c["parties"] += o.n_parties > 0
        c["regulations"] += o.regulations != "[]"
        c["issuer"] += o.issuer_guess is not None
    fmt = {k: ", ".join(f"{n} {v}" for n, v in c.items()) for k, c in sorted(by_type.items(), key=lambda kv: str(kv[0]))}
    linked = collections.Counter(o.penalty_link_status for o in with_text if o.penalty_link_status)
    regimes = collections.Counter(r for o in with_text for r in json.loads(o.regimes))
    return {
        "orders_with_text": len(with_text),
        "with_parties": sum(o.n_parties > 0 for o in with_text),
        "parties_source": dict(collections.Counter(o.parties_source for o in with_text if o.parties_source)),
        "entities": len(entities),
        "entities_per_order_median": sorted(o.n_parties for o in with_text)[len(with_text) // 2] if with_text else None,
        "entity_role": dict(collections.Counter(e.entity_role for e in entities).most_common()),
        "entity_type": dict(collections.Counter(e.entity_type for e in entities).most_common()),
        "with_pan": sum(e.pan is not None for e in entities),
        "by_type": fmt,
        "penalty_link_status": dict(linked),
        "with_regulations": sum(o.regulations != "[]" for o in with_text),
        "regimes_violated_top": dict(regimes.most_common(15)),
        "issuer_source": dict(collections.Counter(o.issuer_source for o in with_text if o.issuer_source)),
        "errors": sum(o.error is not None for o in orders),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.extract_parties``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(prog="python -m src.extract_parties",
                                     description="Parties and regulations (runbook Week 2, Day 5)")
    parser.add_argument("--documents", type=pathlib.Path, default=TEXT_DOCUMENTS_IN)
    parser.add_argument("--listings", type=pathlib.Path, default=LISTINGS_IN)
    parser.add_argument("--sanctions", type=pathlib.Path, default=SANCTIONS_IN,
                        help="Day 4 sanction mentions (default derived/sanction_mentions.parquet)")
    parser.add_argument("--actions", type=pathlib.Path, default=ACTIONS_IN)
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
    """Run stage 8: parties, roles, penalties per party and violated regulations.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0.
    """
    args = build_arg_parser().parse_args(argv)
    if args.scratch:
        out_dir, log_file = args.scratch, args.scratch / "extract_parties.log"
    else:
        out_dir, log_file = DERIVED_DIR, LOGS_DIR / "extract_parties.log"
    for p in (out_dir, log_file):
        assert_safe_output(p)
    setup_logging(args.log_level, log_file)
    if not args.sanctions.exists():
        log.warning("%s missing: run python -m src.extract_actions first; penalties will not be linked",
                    args.sanctions)
    jobs = select(load_jobs(args.documents, args.listings, args.sanctions, args.actions), categories=args.category,
                  order_ids=args.order_id, stems=args.stem, sample=args.sample, limit=args.limit)
    log.info("%s: %d orders with %d worker(s) -> %s", RULES_VERSION, len(jobs), args.workers, out_dir)
    started = time.monotonic()
    orders, entities, mentions_out = run(jobs, workers=args.workers)
    paths = write_outputs(orders, entities, mentions_out, out_dir)
    log.info("=" * 72)
    for k, v in summarise(orders, entities).items():
        if k == "by_type":
            for t, line in v.items():
                log.info("  %-10s %s", t, line)
        else:
            log.info("%-26s %s", k, v)
    log.info("regulation mentions: %d", len(mentions_out))
    log.info("elapsed %.1f min; wrote %s", (time.monotonic() - started) / 60, ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
