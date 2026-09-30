"""Week 3, Day 2: detection channel — how the matter first came to SEBI's notice.

Implements ``hand/coding_protocol.md`` §5, which was written first. Reads the
order texts (``derived/text_documents.parquet``, ``derived/text/``) and the
listings, and writes ``derived/order_channels.parquet``: one row per order with

* ``detection_channel`` — SURVEILLANCE, COMPLAINT, AUDIT, MEDIA, REFERRAL,
  SELF_DISCLOSURE, INSPECTION or UNCLEAR (protocol §5; INSPECTION by user
  decision, 2026-09-30);
* ``detection_detail`` — the finer code (``alert``, ``trading_pattern_observed``,
  ``scores``, ``sfio``, ``exchange_reference`` …), so robustness checks can move a
  sub-type (e.g. H4 with and without the ISO wave's "SEBI observed …");
* ``detection_confidence`` — ``high`` / ``medium`` / ``low``;
* ``detection_method`` — ``rule``, ``model`` or ``none``;
* ``detection_raw`` — the sentence the channel was read from (runbook rule 2).

How a channel is chosen
-----------------------
1. The *background* of the order (sentences starting in the first 45% of the
   text, at least the first 6,000 characters) is split into sentences.
2. Rules (``find_triggers``) look for a source tied to the start of SEBI's
   work: a link ("pursuant to", "based on", "on receipt of", "following" …) or
   a receipt ("SEBI received a complaint", "MCA forwarded …") before the source,
   and an investigation / examination / inspection cue in that sentence or the
   next two. Negated mentions ("no complaints were received") and noticees'
   submissions are skipped.
3. The earliest stated trigger wins (protocol §5 rule 3). An exchange reference
   with no stated basis, or "SEBI suo motu", is UNCLEAR (user decision) unless the
   next three sentences give the basis.
4. Orders with no rule hit but with candidate sentences go to the local model
   (``--model``; skipped with ``--no-model``), which must name the channel and
   quote the sentence it read; unverified quotes are discarded.
5. Anything left is UNCLEAR (``none``), ``low``.

This stage reads only ``derived/``; the model runs on localhost through
``src/llm.py`` (cached under ``raw/llm/``); no network.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
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

from src import cache, llm
from src import extract_windows as ew
from src.extract_text import assert_safe_output, setup_logging, text_stem

log = logging.getLogger("detect_channel")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
LOGS_DIR = REPO_ROOT / "logs"

RULES_VERSION = "channel-v1"
PROMPT_VERSION = "dc-v2"  # v2: only cue-bearing sentences in the trigger window; quotes must carry the cue
DEFAULT_MODEL = "qwen3.5:4b"
OPTIONS = {"temperature": 0, "seed": 7, "num_ctx": 8192}

SURVEILLANCE, COMPLAINT, AUDIT, MEDIA = "SURVEILLANCE", "COMPLAINT", "AUDIT", "MEDIA"
REFERRAL, SELF_DISCLOSURE, INSPECTION, UNCLEAR = "REFERRAL", "SELF_DISCLOSURE", "INSPECTION", "UNCLEAR"
CHANNELS = [SURVEILLANCE, COMPLAINT, AUDIT, MEDIA, REFERRAL, SELF_DISCLOSURE, INSPECTION, UNCLEAR]
#: Sub-types that name the pattern but not the mechanism: SURVEILLANCE, flagged medium.
FLAGGED = {"trading_pattern_observed", "filings_observed"}

BACKGROUND_SHARE = 0.45
BACKGROUND_MIN_CHARS = 6000
MAX_SENTENCE_CHARS = 700
MODEL_MAX_SENTENCES = 10

_I = re.IGNORECASE
_W = r"(?:[\w’'&./,-]+\s+)"   # one word and its space, for "{0,n}" gaps

# --------------------------------------------------------------------------
# Cues
# --------------------------------------------------------------------------

#: The start of SEBI's (or an exchange's) work on the matter.
START = re.compile(r"\b(?:investigat\w*|examin\w*|inquir\w*|enquir\w*|inspect\w*|probe[ds]?|looked\s+into|"
                   r"scrutin\w*|analy[sz]\w*|show[\s-]+cause|interim\s+order|proceedings?\s+(?:was|were|had\s+been)\s+"
                   r"initiated|initiated\s+(?:adjudication|enquiry|proceedings)|SCN)\b", _I)
LINK = (r"(?:pursuant\s+to|in\s+pursuance\s+of|based\s+(?:up)?on|on\s+the\s+basis\s+of|(?:on|upon|after|on\s+the|upon\s+the|"
        r"after\s+the)\s+receipt\s+of|on\s+receiving|following|consequent\s+(?:up)?on|as\s+a\s+(?:result|follow[\s-]*up)\s+of|"
        r"in\s+view\s+of|in\s+the\s+light\s+of|arising\s+(?:out\s+)?(?:of|from)|emanating\s+from|triggered\s+by|"
        r"prompted\s+by|acting\s+(?:up)?on|on\s+the\s+strength\s+of|in\s+the\s+wake\s+of|the\s+reason\s+for\s+the\s+"
        r"(?:investigation|examination)\s+was\s+that)")
RECEIVED = r"(?:received|lodged|filed|made|forwarded|referred(?!\s+to\s+as)|sent|submitted)"
#: A sentence reporting what the investigation found, not what started it.
FINDING = re.compile(r"\b(?:during|in)\s+the\s+(?:course\s+of\s+(?:the\s+|such\s+)?)?(?:investigations?|examinations?|"
                     r"inspections?)|\b(?:investigations?|examinations?)\s+(?:\w+\s+){0,2}?(?:revealed|found|showed|noticed)|"
                     r"\b(?:pursuant\s+to|based\s+on|on\s+the\s+basis\s+of|in\s+the\s+course\s+of)\s+(?:the\s+|its\s+)?"
                     r"(?:aforementioned\s+|aforesaid\s+|said\s+|above\s+|preliminary\s+)?(?:examination|investigation|"
                     r"inspection)s?\b", _I)
NEGATED = re.compile(r"\b(?:no|not|nor|never|without|neither)\b(?:\s+\S+){0,5}\s*$", _I)
SUBMISSION = re.compile(r"\b(?:noticees?|applicants?|appellants?|he|she|they|the\s+broker|counsel|AR|authori[sz]ed\s+"
                        r"representatives?)\s+(?:\w+\s+){0,3}?(?:submitted|contended|argued|claimed|pleaded|averred|"
                        r"replied|denied)\b|\bsubmitted\s+(?:the\s+following|as\s+under|that)\b|\b(?:contended|argued)\s+"
                        r"that\b|\bcontentions?\b|\bin\s+(?:his|her|its|their)\s+(?:reply|replies|submissions?)\b", _I)
#: SEBI (or an exchange) doing something that starts its work: the trigger is
#: stated before this or within the next few sentences, not in the findings.
WORK_START = re.compile(rf"\b(?:conducted|initiated|carried\s+out|undert(?:ook|aken)|t(?:ook|aken)\s+up|launched|"
                        rf"ordered|commenced|examined|investigated|analy[sz]ed|made)\b", _I)
WORK_WINDOW = 8
NEAR = 5            # an investigation cue within this many sentences of the trigger

EXCH = (r"(?:the\s+)?(?:BSE|NSE|MCX|NCDEX|MSEI|CSE|DSE|ASE|OTCEI|Bombay\s+Stock\s+Exchange|National\s+Stock\s+Exchange|"
        r"Calcutta\s+Stock\s+Exchange|Delhi\s+Stock\s+Exchange|Metropolitan\s+Stock\s+Exchange|(?:the\s+)?(?:concerned\s+)?"
        r"(?:stock\s+)?exchanges?)")
TAIL = r"[”’'\")\],]*"      # "(‘SEBI’) received": a closing quote or bracket before the verb
SEBI_T = rf"(?:SEBI|(?:the\s+)?Board){TAIL}"
EXCH_T = rf"{EXCH}{TAIL}"
#: Who observed a pattern. Not "it": "it was observed" is how every finding is written.
ACTOR = rf"(?:{SEBI_T}|{EXCH_T})"
PATTERN = (r"(?:reversal|synchroni[sz]ed|circular|unusual|abnormal|sharp|steep|sudden|huge|large[\s-]*scale|significant|"
           r"substantial|spurt|artificial|manipulat\w*|suspicious|irregular|speculative|price\s+(?:rise|increase|movement|"
           r"fall|volatility|fluctuations?|variations?)|volume|sell[\s-]*off)")
OFFER_DOC = (r"(?:draft\s+)?(?:letters?\s+of\s+offer|LOO|DLOF|offer\s+documents?|DRHP|RHP|red\s+herring|prospectus|"
             r"shareholding\s+patterns?|annual\s+reports?|financial\s+results)")
AGENCIES: list[tuple[str, str]] = [
    ("mca_roc", r"Ministry\s+of\s+(?:Corporate|Company)\s+Affairs|MCA\b|Registrar\s+of\s+Companies|RoC\b"),
    ("sfio", r"Serious\s+Frauds?\s+Investigation|SFIO\b"),
    ("rbi", r"Reserve\s+Bank|RBI\b"),
    ("tax", r"Income[\s-]*Tax|CBDT\b|Directorate\s+(?:General\s+)?of\s+Income|Central\s+Board\s+of\s+Direct|DGGI\b|"
            r"Directorate\s+General\s+of\s+(?:GST|Goods)|GST\s+(?:authorit\w+|department)|Department\s+of\s+Revenue|"
            r"Directorate\s+of\s+Revenue|DRI\b|Customs"),
    ("police_ed_cbi", r"Enforcement\s+Directorate|Directorate\s+of\s+Enforcement|Central\s+Bureau\s+of\s+Investigation|"
                      r"CBI\b|police|Economic\s+Offences?\s+Wing|EOW\b|Crime\s+Branch"),
    ("court", r"Hon[’']?ble\s+(?:High|Supreme)\s+Court|High\s+Court|Supreme\s+Court|NCLT\b|National\s+Company\s+Law"),
    ("regulator", r"IRDA\w*|Insurance\s+Regulatory|PFRDA\b|Pension\s+Fund\s+Regulatory|Forward\s+Markets?\s+Commission|"
                  r"FMC\b|Competition\s+Commission|Securities\s+and\s+Exchange\s+Commission|foreign\s+regulator\w*|"
                  r"(?:IOSCO|MMoU)"),
    ("government", r"Ministry\s+of\s+Finance|Department\s+of\s+Economic\s+Affairs|Government\s+of\s+India|"
                   r"(?:Central|State)\s+Government|Prime\s+Minister|Cabinet\s+Secretariat"),
]
AGENCY = "(?:" + "|".join(f"(?P<{name}>{rx})" for name, rx in AGENCIES) + ")"
DOC = r"(?:reference|letter|communication|report|complaint|information|intimation|e-?mail|note|list)s?"
#: A noticee inspecting SEBI's documents after the notice is procedure, not an inspection that found anything.
PROCEDURAL_INSPECTION = re.compile(r"\binspection\s+of\s+(?:the\s+)?(?:relevant\s+|said\s+|investigation\s+)?"
                                   r"(?:documents|files|records\s+relied)|\bpresence\s+of\s+(?:the\s+)?(?:AR|authori[sz]ed|"
                                   r"noticee|counsel|advocate)|\bsought\s+inspection|\b(?:availed|granted|provided|allowed|"
                                   r"afforded|request\w*)\s+(?:\w+\s+){0,3}?inspection|\binspection\s+(?:was|were)\s+"
                                   r"(?:granted|provided|allowed|afforded|given|availed)", _I)
OBSERVER = re.compile(rf"\b{ACTOR}\s+(?:had\s+|has\s+)?{_W}{{0,2}}?(?:observed|noticed|detected|noted)\b|\bnoticing\b|"
                      rf"\breported\s+(?:to\s+SEBI\s+)?by\b|\b(?:reference|report|letter)\s+{_W}{{0,4}}?(?:from|of|by)\s+"
                      rf"{EXCH_T}", _I)
#: SEBI receiving a complaint, reference or report *is* the matter coming to its
#: notice: no investigation cue needed ("SEBI received two complaints – …").
RECEIVED_BY_SEBI = re.compile(rf"\b{SEBI_T}\s+(?:had\s+|has\s+|also\s+)*received\b|\breceived\s+(?:by|at)\s+SEBI\b|"
                              rf"\b{SEBI_T}\s+was\s+in\s+receipt\s+of", _I)
SOURCES = {COMPLAINT, AUDIT, MEDIA, REFERRAL, SELF_DISCLOSURE}
BACKREF = re.compile(LINK + r"\s+(?:the\s+|this\s+)?(?:above|aforesaid|said|foregoing|same|this)\b"
                     r"(?:\s+(?:background|facts?|observations?|developments?|circumstances))?", _I)
PRICE_EVENT = re.compile(r"\b(?:rise|increase|spurt|fall|decline|jump|surge|fluctuat\w*|variation)\s+in\s+(?:the\s+)?"
                         r"(?:share\s+)?(?:price|volume)|\bprice\s+(?:rise|increase|fall|movement|variation)|\bsteep\s+"
                         r"(?:increase|rise|fall)|\bsuspended\s+(?:the\s+)?(?:trading|scrip)|speculative\s+trading", _I)
FRAUD_WORDS = re.compile(r"fraud|irregularit|misappropriat|forensic|misstat|siphon|divert|defalcat|embezzl|"
                         r"unauthori[sz]ed|discrepanc|manipulat|falsif", _I)


@dataclass(frozen=True)
class Rule:
    channel: str
    detail: str
    rx: re.Pattern
    linked: bool = True        # the pattern itself ties the source to SEBI's work


def _r(pattern: str) -> re.Pattern:
    return re.compile(pattern, _I)


RULES: list[Rule] = [
    # --- SURVEILLANCE -------------------------------------------------------
    Rule(SURVEILLANCE, "alert", _r(r"\bsurveillance\s+(?:system|alert|activit\w*|department|division|mechanism|measures?|"
                                   r"report|team|wing|function)|\b(?:during|as\s+(?:a\s+)?part\s+of|in\s+the\s+course\s+of|"
                                   r"in|through|by)\s+(?:its\s+|the\s+|their\s+|routine\s+|regular\s+|on[\s-]*going\s+|"
                                   r"continuous\s+)*(?:market\s+|online\s+)?surveillance|\breceived\s+(?:an?\s+|the\s+)?" + _W + r"{0,2}?"
                                   r"alerts?\s+(?:from|of|by)\s+" + EXCH + r"|\balerts?\s+" + _W + r"{0,3}?"
                                   r"generated|\bgenerated\s+" + _W + r"{0,3}?alerts?|" + LINK + r"\s+(?:an?\s+|the\s+)?" + _W +
                                   r"{0,7}?alerts?\b")),
    Rule(SURVEILLANCE, "trading_pattern_observed",
         _r(rf"\b{ACTOR}\s+(?:had\s+|has\s+)?{_W}{{0,2}}?(?:observed|noticed|detected|noted)\s+(?:that\s+)?{_W}{{0,8}}?"
            rf"{PATTERN}|\b(?:on|after|upon)\s+noticing\s+{_W}{{0,4}}?{PATTERN}|"
            rf"\bas\s+it\s+was\s+(?:observed|noticed)\s+that\s+{_W}{{0,8}}?{PATTERN}|"
            rf"{LINK}\s+{_W}{{0,4}}?{PATTERN}\s+{_W}{{0,4}}?(?:noticed|observed|witnessed|detected)|"
            rf"\b(?:scrip|shares?|stock)\b.{{0,160}}?\bwitnessed\s+{_W}{{0,5}}?(?:volume|price|rise|fall|"
            rf"increase|movement|fluctuations?|volatility)|\b(?:rise|increase|fall|spurt|fluctuations?|variations?)\s+in\s+(?:the\s+)?(?:share\s+)?"
            rf"(?:price|volume)\b.{{0,160}}?\b(?:was|were)\s+(?:witnessed|observed|noticed)|"
            rf"{PATTERN}\s+{_W}{{0,4}}?(?:was|were)\s+reported\s+(?:to\s+SEBI\s+)?by\s+(?:the\s+)?{EXCH}|"
            rf"\b(?:price|volume)\s+{_W}{{0,3}}?(?:rise|increase|spurt|movement|fall|volatility)\s+{_W}{{0,8}}?(?:was|were)\s+"
            rf"(?:observed|noticed)|\b(?:report|reference|letter)\s+{_W}{{0,4}}?(?:from|of|by)\s+{EXCH}\s+{_W}{{0,3}}?"
            rf"(?:that|wherein|regarding|indicating|highlighting|pointing)\b.{{0,160}}?\b(?:observed|{PATTERN})")),
    Rule(SURVEILLANCE, "filings_observed",
         _r(rf"\b{SEBI_T}\s+(?:had\s+)?(?:observed|noticed|noted)\s+{_W}{{0,4}}?from\s+(?:the\s+)?"
            rf"(?:shareholding\s+patterns?|disclosures?|filings?|annual\s+reports?|corporate\s+announcements?|"
            rf"(?:website|records|data)\s+of\s+(?:the\s+)?{EXCH})|"
            rf"\b(?:while|during|on|upon|in\s+the\s+course\s+of)\s+(?:the\s+)?(?:examin\w+|scrutin\w+|review\w*|perus\w+|"
            rf"processing)\s+(?:of\s+)?(?:the\s+)?(?:aforesaid\s+|said\s+)?{OFFER_DOC}|"
            rf"{LINK}\s+(?:the\s+)?filing\s+of\s+(?:the\s+|a\s+)?{OFFER_DOC}|"
            rf"\b(?:reference|report|letter)\s+{_W}{{0,4}}?(?:from|of|by)\s+{EXCH_T}\s+.{{0,120}}?\b(?:share)?holding\b"
            rf".{{0,120}}?\b(?:changed|increased|decreased|reduced)")),
    # --- COMPLAINT ------------------------------------------------------------
    Rule(COMPLAINT, "whistleblower", _r(rf"{LINK}\s+{_W}{{0,4}}?whistle[\s-]*blow\w*|\bwhistle[\s-]*blow\w*\s+"
                                        rf"{_W}{{0,6}}?{RECEIVED}|\breceived\s+{_W}{{0,4}}?whistle[\s-]*blow\w*")),
    Rule(COMPLAINT, "scores", _r(rf"\bcomplaints?\s+{_W}{{0,8}}?(?:through|on|via|in)\s+(?:the\s+)?SCORES|\bSCORES\s+"
                                rf"{_W}{{0,6}}?complaints?|{LINK}\s+{_W}{{0,4}}?SCORES")),
    Rule(COMPLAINT, "complaint", _r(rf"{LINK}\s+{_W}{{0,5}}?(?:complaints?|grievances?|representations?)\b|"
                                    rf"\bcomplaints?\s+{_W}{{0,10}}?(?:was|were|had\s+been|has\s+been|have\s+been)\s+"
                                    rf"(?:\w+\s+)?{RECEIVED}\b|\b(?:{SEBI_T}|{EXCH_T})\s+(?:had\s+|has\s+|also\s+)*"
                                    rf"received\s+{_W}{{0,5}}?(?:\w+/)?complaints?\b|\bcomplainants?\s+{_W}{{0,3}}?(?:had\s+)?"
                                    rf"(?:alleged|approached|filed|lodged)|\b(?:investor\s+)?(?:complaints?|grievances?)\s+{_W}{{0,6}}?(?:was|were|is|are|"
                                    rf"remained)\s+"
                                    rf"(?:\w+\s+)?(?:pending|unresolved|unredressed|not\s+redressed|outstanding)|\bpending\s+"
                                    rf"(?:investor\s+)?complaints?|\bnon[\s-]*redressal\s+of\s+(?:investor\s+)?"
                                    rf"(?:complaints?|grievances?)")),
    # --- AUDIT ------------------------------------------------------------------
    Rule(AUDIT, "qualification", _r(rf"(?:{LINK}|\bfrom)\s+{_W}{{0,4}}?(?:qualified\s+(?:audit\s+)?opinion|audit\s+"
                                    rf"qualifications?|qualifi\w+\s+(?:\w+\s+){{0,3}}?(?:by|of)\s+(?:the\s+)?(?:statutory\s+)?"
                                    rf"auditors?)")),
    Rule(AUDIT, "auditor_resignation", _r(rf"{LINK}\s+{_W}{{0,4}}?(?:resignation\s+of\s+(?:the\s+)?(?:\w+\s+)?auditors?|"
                                          rf"auditors?[’']?s?\s+resignation)")),
    Rule(AUDIT, "forensic_audit", _r(rf"{LINK}\s+{_W}{{0,6}}?forensic\s+(?:audit|auditor)|\bforensic\s+(?:audit|auditor)\s+"
                                     rf"{_W}{{0,4}}?(?:commissioned|conducted|appointed|report)\s+{_W}{{0,4}}?(?:by|at\s+the\s+"
                                     rf"instance\s+of)\s+(?:the\s+)?{_W}{{0,3}}?(?:lenders?|banks?|consortium|company|board)")),
    # --- MEDIA ---------------------------------------------------------------------
    Rule(MEDIA, "research_report", _r(rf"{LINK}\s+{_W}{{0,6}}?(?:research\s+reports?|short[\s-]*sell\w*\s+(?:report|firm)|"
                                      rf"Hindenburg)")),
    Rule(MEDIA, "news", _r(rf"{LINK}\s+{_W}{{0,6}}?(?:news\s*(?:paper)?\s*(?:articles?|reports?|items?|clippings?)?|"
                           rf"media\s+(?:reports?|articles?)|press\s+reports?|articles?|(?:television|TV)\s+"
                           rf"(?:channel|programme|program|show))\b|\b(?:SEBI|(?:the\s+)?Board)\s+(?:\w+\s+)?(?:came\s+across|"
                           rf"noticed|observed)\s+(?:an?\s+|the\s+|certain\s+|some\s+)?(?:news|media|press)\s+"
                           rf"(?:articles?|reports?|items?)|\b(?:in\s+receipt\s+of|received)\s+(?:an?\s+|the\s+)?(?:news\s*(?:paper)?\s*"
                           rf"(?:articles?|reports?|items?)|media\s+reports?)|\breports?\s+(?:in|by)\s+(?:the\s+)?"
                           rf"(?:media|press|news\s*papers?)|\b(?:as\s+per|according\s+to)\s+(?:the\s+|certain\s+)?"
                           rf"(?:media|press|news\s*(?:paper)?)\s+reports?")),
    # --- REFERRAL -----------------------------------------------------------------
    Rule(REFERRAL, "agency", _r(rf"\b{DOC}\s+{_W}{{0,10}}?(?:from|by|of)\s+(?:the\s+)?(?:office\s+of\s+(?:the\s+)?)?"
                                rf"{AGENCY}")),
    Rule(REFERRAL, "agency", _r(rf"\b{AGENCY}\s+{_W}{{0,8}}?(?:referred(?!\s+to\s+as)|forwarded|informed|intimated|"
                                rf"sent|brought\s+to\s+the\s+notice|made\s+a\s+reference)")),
    Rule(REFERRAL, "court", _r(rf"{LINK}\s+(?:the\s+)?(?:direct\w*|orders?)\s+{_W}{{0,4}}?(?:of|by|from)\s+(?:the\s+)?"
                               rf"Hon[’']?ble\s+(?:High|Supreme)\s+Court")),
    # --- SELF_DISCLOSURE ---------------------------------------------------------------
    Rule(SELF_DISCLOSURE, "confession", _r(rf"{LINK}\s+.{{0,200}}?\bconfess\w*")),
    Rule(SELF_DISCLOSURE, "company_disclosure",
         _r(rf"\b(?:the\s+)?(?:company|it|listed\s+entity)\s+(?:had\s+|has\s+)?(?:informed|intimated|disclosed|reported|"
            rf"announced)\s+(?:to\s+)?(?:the\s+)?(?:{EXCH}|SEBI)\b"), linked=False),
    Rule(SELF_DISCLOSURE, "self_report", _r(r"\b(?:voluntarily|suo[\s-]*mot[ou])\s+(?:disclosed|reported|informed|"
                                            r"intimated|brought\s+to\s+the\s+notice)|\b(?:filed|submitted|made)\s+"
                                            r"(?:\w+\s+){0,8}?(?:voluntarily|suo[\s-]*mot[ou])\b|\b(?:voluntarily|"
                                            r"suo[\s-]*mot[ou])\s+(?:filed|submitted|made)\s+(?:an?\s+)?(?:consent\s+|"
                                            r"settlement\s+)?application")),
    # --- INSPECTION --------------------------------------------------------------------
    Rule(INSPECTION, "inspection",
         _r(rf"\b(?:{SEBI_T}|{EXCH_T}|CDSL|NSDL|depositor(?:y|ies))\s+(?:had\s+|has\s+)?(?:conducted|carried\s+out|"
            rf"undertook|undertaken|initiated|performed)\s+(?:an?\s+|the\s+|its\s+)?{_W}{{0,3}}?inspection|"
            rf"\b(?:routine\s+|thematic\s+|off[\s-]*site\s+|on[\s-]*site\s+|limited\s+purpose\s+|comprehensive\s+|joint\s+|"
            rf"regular\s+|periodic\s+)*inspection\s+{_W}{{0,14}}?(?:was|were|had\s+been|has\s+been)\s+(?:\w+\s+)?(?:conducted|"
            rf"carried\s+out|undertaken|initiated|done|performed)|\b(?:during|in)\s+(?:the\s+)?(?:course\s+of\s+)?"
            rf"(?:the\s+|its\s+|an?\s+)?(?:routine\s+|thematic\s+|off[\s-]*site\s+|limited\s+purpose\s+|comprehensive\s+)?"
            rf"inspection\b|{LINK}\s+(?:the\s+|an?\s+)?{_W}{{0,2}}?inspection"), linked=False),
    # --- UNCLEAR (user decision 2026-09-30) ---------------------------------------------
    Rule(UNCLEAR, "exchange_reference", _r(rf"\b(?:reference|report|letter|e-?mail|communication|intimation|information)s?\s+"
                                           rf"{_W}{{0,6}}?(?:from|by|of)\s+(?:the\s+)?{EXCH}|\b{EXCH}\s+{_W}{{0,6}}?"
                                           rf"(?:referred(?!\s+to\s+as)|forwarded|made\s+a\s+reference|sent\s+a\s+reference)")),
    Rule(UNCLEAR, "suo_motu", _r(r"\bsuo[\s-]*mot[ou]\b(?![^.;]{0,40}\bapplication)")),
]


@dataclass
class Hit:
    channel: str
    detail: str
    sentence_no: int       # 0-based index into the background sentences
    pos: int
    confidence: str
    evidence: str


def _agency_detail(m: re.Match, default: str) -> str:
    for name, _ in AGENCIES:
        if m.groupdict().get(name):
            return name
    return default


def background(text: str) -> list[str]:
    """The sentences of the order's background, whitespace-normalised."""
    stripped = ew.strip_repeated_lines(text)
    limit = max(BACKGROUND_MIN_CHARS, int(len(stripped) * BACKGROUND_SHARE))
    out = []
    for start, end in ew.split_sentences(stripped):
        if start >= limit:
            break
        s = ew.normalise_sentence(stripped[start:end])
        if s:
            out.append(s)
    return out


def _accept(rule: Rule, m: re.Match, sentence: str, has_start_near: bool) -> str | None:
    """The confidence of a match, or None if it is not a trigger."""
    before = sentence[max(0, m.start() - 60):m.start()]
    if NEGATED.search(before) or re.match(r"\s*(?:no|not)\b", sentence[m.start():m.start() + 4], _I):
        return None
    if rule.channel == AUDIT and re.search(r"\b(?:SEBI|Board|exchange)\s+(?:had\s+)?(?:appointed|directed)|appointed\s+by\s+"
                                           r"(?:SEBI|the\s+Board)|(?:directions?|instance)\s+of\s+(?:SEBI|the\s+Board)",
                                           sentence, _I):
        return None
    if rule.channel == SELF_DISCLOSURE and rule.detail == "company_disclosure" and not FRAUD_WORDS.search(sentence):
        return None
    if rule.channel == INSPECTION and PROCEDURAL_INSPECTION.search(sentence):
        return None
    # a document sent to the noticee with the notice ("along with … copy of letter
    # dated December 30, 2014 of Department of Economic Affairs") is not a trigger
    if rule.channel == REFERRAL and re.search(r"\bcop(?:y|ies)\s+of\b|\bprovid\w+\s+therewith|\benclos\w+|\bannex\w+\s+"
                                              r"(?:to|with)\s+the\s+(?:SCN|notice)", sentence[:m.start()], _I):
        return None
    if not has_start_near and not (rule.channel in SOURCES and RECEIVED_BY_SEBI.search(sentence)):
        return None
    if rule.detail in FLAGGED:
        # A pattern stated on its own ("The scrip opened at Rs.73/- and witnessed a
        # steep price fall") is background, not a trigger: it needs the start of the
        # work in the same sentence, SEBI or an exchange as the observer, or a link.
        if not (START.search(sentence) or OBSERVER.search(sentence) or re.search(LINK, sentence, _I)):
            return None
        return "medium"
    return "high" if (rule.linked or START.search(sentence)) else "medium"


def trigger_window(sentences: list[str]) -> int:
    """Index of the last sentence that can state the trigger: it is stated before,
    or just after, SEBI's work starts — not in the findings or the replies."""
    first_work = next((i for i, s in enumerate(sentences)
                       if START.search(s) and WORK_START.search(s) and not SUBMISSION.search(s)
                       and not re.search(r"\bsettlement\s+application|\bconsent\s+(?:application|terms)", s, _I)), None)
    return len(sentences) - 1 if first_work is None else first_work + WORK_WINDOW


def find_triggers(sentences: list[str]) -> list[Hit]:
    """Every accepted trigger in the background, in text order."""
    hits: list[Hit] = []
    starts = [bool(START.search(s)) for s in sentences]
    for i, s in enumerate(sentences[:trigger_window(sentences) + 1]):
        if SUBMISSION.search(s):
            continue
        near = any(starts[i:i + NEAR + 1])
        clipped = s[:3000]
        finding = bool(FINDING.search(clipped))
        for rule in RULES:
            if finding and rule.channel == SURVEILLANCE and rule.detail in FLAGGED:
                continue
            for m in rule.rx.finditer(clipped):
                conf = _accept(rule, m, clipped, near)
                if conf is None:
                    continue
                detail = _agency_detail(m, rule.detail) if rule.channel == REFERRAL else rule.detail
                if rule.channel == INSPECTION:
                    detail = ("thematic" if re.search(r"thematic", m.group(0), _I) else
                              "exchange_inspection" if re.search(EXCH + r"|CDSL|NSDL|depositor", m.group(0), _I) and
                              not re.search(r"\bSEBI\b", m.group(0)) else "sebi_inspection")
                hits.append(Hit(rule.channel, detail, i, m.start(), conf, s[:MAX_SENTENCE_CHARS]))
                break
        if starts[i] and (b := BACKREF.search(clipped)) and not any(h.sentence_no == i for h in hits):
            if any(PRICE_EVENT.search(p) for p in sentences[max(0, i - 3):i]):
                hits.append(Hit(SURVEILLANCE, "trading_pattern_observed", i, b.start(), "medium", s[:MAX_SENTENCE_CHARS]))
    hits.sort(key=lambda h: (h.sentence_no, h.pos))
    return hits


def choose(hits: list[Hit]) -> Hit | None:
    """The earliest trigger (protocol §5 rule 3). An UNCLEAR one (an exchange
    reference with no stated basis, SEBI suo motu) gives way to a real channel
    stated within the next three sentences — its basis."""
    if not hits:
        return None
    first = hits[0]
    if first.channel == UNCLEAR:
        for h in hits[1:]:
            if h.channel != UNCLEAR and h.sentence_no <= first.sentence_no + 3:
                return h
    return first


# --------------------------------------------------------------------------
# The model, for orders the rules do not settle
# --------------------------------------------------------------------------

#: The words a trigger for each channel cannot be stated without. The model sees
#: only sentences carrying one, and its quote must carry the cue of the channel
#: it names — so it can find paraphrases the rules miss, but cannot turn
#: "SEBI conducted an investigation" into a channel (dev set: it did, 7 times).
CHANNEL_CUES: dict[str, re.Pattern] = {
    SURVEILLANCE: re.compile(rf"\bsurveillance|\balerts?\b|\b(?:observed|noticed|noticing|detected|witnessed|"
                             rf"reported)\b.{{0,200}}?{PATTERN}|{PATTERN}.{{0,200}}?\b(?:observed|noticed|detected|"
                             rf"witnessed|reported)\b", _I),
    COMPLAINT: re.compile(r"\bcomplain\w*|\bgrievances?|\bwhistle[\s-]*blow\w*|\bSCORES\b|\brepresentations?\b", _I),
    AUDIT: re.compile(r"\baudit\w*|\blimited\s+review|\bforensic", _I),
    MEDIA: re.compile(r"\bnews|\bmedia\b|\bpress\b|\barticles?\b|\bresearch\s+reports?|\bshort[\s-]*sell|"
                      r"\btelevision|\bTV\b|\bnewspapers?", _I),
    REFERRAL: re.compile("|".join(rx for _, rx in AGENCIES), _I),
    SELF_DISCLOSURE: re.compile(r"\bconfess\w*|\bvoluntar\w*|\bself[\s-]*report\w*|\b(?:informed|intimated|"
                                r"disclosed|reported)\b.{0,160}?(?:fraud|irregularit|misappropriat|misstat|siphon|divert|"
                                r"defalcat|embezzl|forensic|discrepanc)", _I),
    INSPECTION: re.compile(r"\binspect\w*", _I),
}

SYSTEM = """\
You read sentences from the background of an order of the Securities and Exchange Board of India (SEBI) \
and decide how the matter FIRST came to the notice of SEBI or a stock exchange. Reply with JSON only.

Channels:
- SURVEILLANCE: a surveillance alert or market surveillance by SEBI or an exchange; or SEBI / an exchange \
observed a trading pattern or a filing (e.g. "SEBI observed large scale reversal of trades", "SEBI conducted \
an investigation as the scrip witnessed a sharp price rise"). A price movement described on its own, not tied to \
the start of the investigation, is not a trigger.
- COMPLAINT: a complaint by an investor, client, shareholder or any person; SCORES; a whistle-blower.
- AUDIT: an auditor's qualification or resignation, or a forensic audit commissioned by lenders or the company.
- MEDIA: a news article, media report, research or short-seller report.
- REFERRAL: a reference, letter or report from another public body (MCA, Registrar of Companies, SFIO, RBI, \
Income Tax, Enforcement Directorate, CBI, police, a court, a ministry, another regulator). Not a stock exchange.
- SELF_DISCLOSURE: the company, its management or the noticee itself disclosed or confessed the irregularity.
- INSPECTION: a routine or thematic inspection of an intermediary (broker, depository participant, adviser \
...) by SEBI or an exchange.
- UNCLEAR: the sentences do not say what started it. Use UNCLEAR when SEBI simply "conducted an \
investigation", acted "suo motu", or received a report or reference from a stock exchange without saying \
what it was about.

Rules: the trigger must be what started SEBI's (or the exchange's) examination, investigation or inspection — \
not something found later, not what a noticee argued, not a forensic auditor SEBI itself appointed. If \
several are stated, give the earliest. Do not guess: when in doubt, UNCLEAR.

Give the number of the sentence you used and copy the words that state the trigger exactly as written \
(quote, at least three words). For UNCLEAR give sentence 0 and an empty quote."""

SCHEMA = {
    "type": "object",
    "properties": {
        "channel": {"type": "string", "enum": CHANNELS},
        "sentence": {"type": "integer"},
        "quote": {"type": "string"},
    },
    "required": ["channel", "sentence", "quote"],
}


def model_sentences(sentences: list[str]) -> list[str]:
    """The sentences worth sending: in the trigger window, carrying a channel cue."""
    window = sentences[:trigger_window(sentences) + 1]
    return [s[:MAX_SENTENCE_CHARS] for s in window
            if not SUBMISSION.search(s) and any(rx.search(s) for rx in CHANNEL_CUES.values())][:MODEL_MAX_SENTENCES]


def build_user(sentences: list[str]) -> str:
    return "\n".join(["Sentences from the order:"] + [f"[{i}] {s}" for i, s in enumerate(sentences, start=1)])


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def read_answer(content: str, sentences: list[str]) -> tuple[str, str | None, int, str]:
    """(model_status, channel, sentence_no, note)."""
    try:
        ans = json.loads(content)
        channel, no, quote = ans["channel"], int(ans["sentence"]), str(ans.get("quote") or "")
    except (ValueError, KeyError, TypeError):
        return "MODEL_ERROR", None, 0, "unreadable answer"
    if channel not in CHANNELS:
        return "MODEL_ERROR", None, 0, f"unknown channel {channel!r}"
    if channel == UNCLEAR:
        return "MODEL_UNCLEAR", UNCLEAR, 0, ""
    if not 1 <= no <= len(sentences) or len(quote.split()) < 3 or _alnum(quote) not in _alnum(sentences[no - 1]):
        return "MODEL_UNVERIFIED", channel, no, "quote not in the sentence"
    if not CHANNEL_CUES[channel].search(quote):
        return "MODEL_UNVERIFIED", channel, no, f"no {channel} cue in the quote"
    return "MODEL_CHANNEL", channel, no, ""


# --------------------------------------------------------------------------
# Per order
# --------------------------------------------------------------------------

@dataclass
class Job:
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: object
    text_status: str | None
    text_path: str | None


@dataclass
class OrderChannel:
    text_stem: str
    order_id: int | None
    order_type: str | None
    order_date: object
    text_status: str | None
    detection_channel: str | None = None
    detection_detail: str | None = None
    detection_confidence: str | None = None
    detection_method: str | None = None
    detection_raw: str | None = None
    channels_found: str = "[]"
    n_background: int = 0
    model_status: str | None = None
    model_raw: str | None = None
    rules_version: str = RULES_VERSION
    prompt_version: str | None = None
    error: str | None = None
    # carried to the model stage, not written
    candidates: list = field(default_factory=list, repr=False)


def classify_text(o: OrderChannel, text: str) -> OrderChannel:
    sentences = background(text)
    o.n_background = len(sentences)
    hits = find_triggers(sentences)
    o.channels_found = json.dumps(list(dict.fromkeys(f"{h.channel}:{h.detail}" for h in hits)))
    if (h := choose(hits)) is not None:
        o.detection_channel, o.detection_detail, o.detection_confidence = h.channel, h.detail, h.confidence
        if h.channel == UNCLEAR:
            o.detection_confidence = "medium"
        o.detection_method, o.detection_raw = "rule", h.evidence
        return o
    o.candidates = model_sentences(sentences)
    o.detection_channel, o.detection_detail, o.detection_confidence, o.detection_method = UNCLEAR, "none", "low", "none"
    return o


def process(job: Job, repo_root: pathlib.Path = REPO_ROOT) -> OrderChannel:
    """Rules only. Never raises."""
    o = OrderChannel(job.text_stem, job.order_id, job.order_type, job.order_date, job.text_status)
    if job.text_status != "OK" or not job.text_path:
        return o
    try:
        return classify_text(o, (repo_root / job.text_path).read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - last-resort guard for a long run
        o.error = f"{type(exc).__name__}: {exc}"[:300]
        return o


def ask_model(o: OrderChannel, spec: llm.ModelSpec, root: pathlib.Path | None = None) -> OrderChannel:
    """Model pass for an order the rules left UNCLEAR with candidate sentences. Never raises."""
    o.prompt_version = PROMPT_VERSION
    try:
        record = llm.chat_json(spec, SYSTEM, build_user(o.candidates), SCHEMA, root=root)
    except llm.LlmError as exc:
        o.model_status, o.error = "MODEL_ERROR", str(exc)[:300]
        return o
    o.model_raw = record["content"]
    status, channel, no, _ = read_answer(record["content"], o.candidates)
    o.model_status = status
    if status == "MODEL_CHANNEL":
        o.detection_channel, o.detection_detail, o.detection_confidence = channel, "model", "medium"
        o.detection_method, o.detection_raw = "model", o.candidates[no - 1]
    elif status == "MODEL_UNCLEAR":
        o.detection_method, o.detection_confidence = "model", "medium"
    return o


def _process_many(jobs, repo_root):
    return [process(j, repo_root) for j in jobs]


# --------------------------------------------------------------------------
# Input / output
# --------------------------------------------------------------------------

def load_jobs(derived: pathlib.Path = DERIVED_DIR) -> list[Job]:
    listings = pq.read_table(derived / "listings.parquet",
                             columns=["order_id", "source_url", "order_type", "order_date"]).to_pylist()
    meta = {text_stem(r["order_id"], r["source_url"]): r for r in listings}
    docs = pq.read_table(derived / "text_documents.parquet", columns=["text_stem", "status", "text_path"]).to_pylist()
    jobs = []
    for d in docs:
        m = meta.get(d["text_stem"], {})
        jobs.append(Job(d["text_stem"], m.get("order_id"), m.get("order_type"), m.get("order_date"), d["status"],
                        d["text_path"]))
    return jobs


def select(jobs: list[Job], *, order_ids=None, stems=None, categories=None, sample=None, limit=None,
           seed: int = 7) -> list[Job]:
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


SCHEMA_OUT = pa.schema([
    pa.field("text_stem", pa.string()),
    pa.field("order_id", pa.int64()),
    pa.field("order_type", pa.string()),
    pa.field("order_date", pa.date32()),
    pa.field("text_status", pa.string()),
    pa.field("detection_channel", pa.string()),
    pa.field("detection_detail", pa.string()),
    pa.field("detection_confidence", pa.string()),
    pa.field("detection_method", pa.string()),
    pa.field("detection_raw", pa.string()),
    pa.field("channels_found", pa.string()),
    pa.field("n_background", pa.int32()),
    pa.field("model_status", pa.string()),
    pa.field("model_raw", pa.string()),
    pa.field("rules_version", pa.string()),
    pa.field("prompt_version", pa.string()),
    pa.field("error", pa.string()),
])


def write(rows: list[OrderChannel], path: pathlib.Path) -> None:
    assert_safe_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([{k: asdict(r)[k] for k in SCHEMA_OUT.names} for r in
                                  sorted(rows, key=lambda r: r.text_stem)], schema=SCHEMA_OUT)
    tmp = path.parent / f"{path.name}.tmp-{os.urandom(6).hex()}"
    pq.write_table(table, tmp, compression="snappy")
    tmp.replace(path)


def summarise(rows: list[OrderChannel]) -> dict:
    ok = [r for r in rows if r.text_status == "OK"]
    return {
        "orders_with_text": len(ok),
        "detection_channel": dict(collections.Counter(r.detection_channel for r in ok).most_common()),
        "method": dict(collections.Counter(r.detection_method for r in ok).most_common()),
        "confidence": dict(collections.Counter(r.detection_confidence for r in ok).most_common()),
        "top_details": dict(collections.Counter(f"{r.detection_channel}:{r.detection_detail}" for r in ok)
                            .most_common(14)),
        "by_type": {t: dict(collections.Counter(r.detection_channel for r in ok if r.order_type == t).most_common())
                    for t in ("AO", "WTM", "SETTLEMENT", "ED_CGM")},
        "model_status": dict(collections.Counter(r.model_status for r in ok if r.model_status)),
        "model_calls": llm.MODEL_CALLS,
        "errors": sum(1 for r in rows if r.error),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.detect_channel",
                                     description="Detection channel per order (runbook Week 3, Day 2)")
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR, help="where the inputs are")
    parser.add_argument("--no-model", action="store_true", help="rules only; unmatched orders stay UNCLEAR")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--offline", action="store_true", help="serve every model answer from raw/llm/")
    parser.add_argument("--category", action="append", choices=["WTM", "SETTLEMENT", "AO", "ED_CGM"])
    parser.add_argument("--order-id", action="append", type=int)
    parser.add_argument("--stem", action="append")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: write order_channels.parquet, the log and the model cache under DIR")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_dir = args.scratch or DERIVED_DIR
    log_file = (args.scratch / "detect_channel.log") if args.scratch else LOGS_DIR / "detect_channel.log"
    cache_root = (args.scratch / "llm") if args.scratch else llm.LLM_CACHE
    for p in (out_dir, log_file):
        assert_safe_output(p)
    setup_logging(args.log_level, log_file)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    jobs = select(load_jobs(args.derived), order_ids=args.order_id, stems=args.stem, categories=args.category,
                  sample=args.sample, limit=args.limit)
    log.info("%s: %d orders with %d worker(s) -> %s", RULES_VERSION, len(jobs), args.workers, out_dir)
    started = time.monotonic()
    rows: list[OrderChannel] = []
    if args.workers <= 1:
        rows = [process(j) for j in jobs]
    else:
        chunks = [jobs[i:i + 200] for i in range(0, len(jobs), 200)]
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            for part in pool.map(_process_many, chunks, [REPO_ROOT] * len(chunks)):
                rows.extend(part)
    to_model = [r for r in rows if r.detection_method == "none" and r.candidates]
    log.info("rules: %d orders decided, %d to the model, %d with no candidate sentence (%.1f min)",
             sum(r.detection_method == "rule" for r in rows), len(to_model),
             sum(r.detection_method == "none" and not r.candidates for r in rows), (time.monotonic() - started) / 60)

    if to_model and not args.no_model:
        llm.OFFLINE = args.offline
        digest = _cached_digest(args.model, cache_root) if args.offline else llm.model_digest(args.model)
        spec = llm.ModelSpec(args.model, digest, options=OPTIONS)
        log.info("%s %s (digest %s): %d orders", PROMPT_VERSION, spec.name, spec.digest[:12], len(to_model))
        t0 = time.monotonic()
        for n, r in enumerate(to_model, start=1):
            ask_model(r, spec, cache_root)
            if n % 200 == 0 or n == len(to_model):
                rate = (time.monotonic() - t0) / n
                log.info("model %d/%d  %.2fs/order  calls=%d  eta=%.1f min", n, len(to_model), rate, llm.MODEL_CALLS,
                         rate * (len(to_model) - n) / 60)

    path = out_dir / "order_channels.parquet"
    write(rows, path)
    log.info("=" * 72)
    for k, v in summarise(rows).items():
        log.info("%-20s %s", k, v)
    log.info("elapsed %.1f min; wrote %s", (time.monotonic() - started) / 60, path)
    return 0


def _cached_digest(name: str, root: pathlib.Path) -> str:
    for path in sorted(root.glob("*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("request", {}).get("model") == name and record["request"]["system"] == SYSTEM:
            return record["request"]["digest"]
    raise llm.CacheMiss(f"no cached channel answers for {name}; run once online first")


if __name__ == "__main__":
    raise SystemExit(main())
