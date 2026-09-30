"""Week 3, Day 1: fraud category, auto-coded from the regulations and the narrative.

Implements ``hand/coding_protocol.md`` §3, which was written first. Reads
``derived/order_parties.parquet`` (the violated provisions from Day 5),
``derived/order_actions.parquet`` (the imposing sentences), the listing titles
and the order texts, and writes ``derived/order_categories.parquet``: one row per
order with

* ``fraud_category`` — one of the eight;
* ``fraud_category_confidence`` — ``high`` / ``medium`` / ``low``;
* ``fraud_category_rule`` — which rule decided it (``pit_trading``,
  ``pfutp:narrative``, ``fallback:title`` …);
* ``fraud_category_raw`` — the evidence: the provisions, or the narrative
  phrases that were counted (runbook: "``*_raw`` for each coded field");
* ``fraud_categories_all`` — every category the order's charges support, in
  precedence order (JSON), for robustness checks;
* ``narrative_scores`` — the keyword counts behind a narrative decision (JSON).

How a category is chosen
------------------------
1. Each violated provision (``order_parties.regulations``) maps to a category
   (``category_of``). PFUTP 3/4 and SEBI Act §12A(a)–(c) map to "read the
   narrative"; ICDR/DIP to "diversion if the narrative says so, else disclosure".
2. The narrative is scored by counting phrases for each category in the order
   text (``narrative_scores``). A diversion narrative goes by whose money it
   was (``route_diversion``): issue proceeds → FUND_DIVERSION; the company's own
   funds to promoters / related parties → RPT_TUNNELLING; clients' or investors'
   assets at an intermediary → INTERMEDIARY_MISCONDUCT. The highest score wins,
   unless a category earlier in the precedence order is also a clear narrative
   (>= 5 and at least half the highest): "if two apply, the precedence order decides".
3. The order's category is the first of its categories in the protocol's
   precedence order: INSIDER_TRADING › FUND_DIVERSION › RPT_TUNNELLING ›
   ACCOUNTING_MISSTATEMENT › MARKET_MANIPULATION › DISCLOSURE_FAILURE ›
   INTERMEDIARY_MISCONDUCT › OTHER.
4. Without a violated provision (19% of orders), in order: the penalty
   sections in the imposing sentence; keywords in the title / matter; a strong
   narrative (score >= 3); the regimes cited or named anywhere in the text — all
   ``low`` confidence; else OTHER, ``low``, rule ``unresolved``.
5. PFUTP with no telling narrative: the clauses charged decide (trading clauses
   4(2)(a)/(b)/(e)/(g)/(q) → MARKET_MANIPULATION; only the misleading-information
   clauses 4(2)(f)/(k)/(r) → DISCLOSURE_FAILURE), ``low``.

This stage reads only ``derived/``; no network.
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
from dataclasses import asdict, dataclass

import pyarrow.parquet as pq

from src import cache
from src import extract_actions as ea
from src import extract_windows as ew
from src import regulations as rg
from src.extract_text import assert_safe_output, setup_logging, text_stem

log = logging.getLogger("classify_fraud")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
LOGS_DIR = REPO_ROOT / "logs"

RULES_VERSION = "fraud-v2"  # v2 (2026-09-30): whose money was diverted; precedence among clear narratives

INSIDER_TRADING = "INSIDER_TRADING"
FUND_DIVERSION = "FUND_DIVERSION"
RPT_TUNNELLING = "RPT_TUNNELLING"
ACCOUNTING_MISSTATEMENT = "ACCOUNTING_MISSTATEMENT"
MARKET_MANIPULATION = "MARKET_MANIPULATION"
DISCLOSURE_FAILURE = "DISCLOSURE_FAILURE"
INTERMEDIARY_MISCONDUCT = "INTERMEDIARY_MISCONDUCT"
OTHER = "OTHER"
#: hand/coding_protocol.md §3: the first applicable category wins.
PRECEDENCE = [INSIDER_TRADING, FUND_DIVERSION, RPT_TUNNELLING, ACCOUNTING_MISSTATEMENT, MARKET_MANIPULATION,
              DISCLOSURE_FAILURE, INTERMEDIARY_MISCONDUCT, OTHER]
#: pseudo-categories resolved from the narrative
NARRATIVE, ISSUE = "NARRATIVE", "ISSUE"

INTERMEDIARY_REGIMES = {"STOCK_BROKERS", "INTERMEDIARIES", "MERCHANT_BANKERS", "PORTFOLIO_MANAGERS", "MUTUAL_FUNDS",
                        "INVESTMENT_ADVISERS", "RESEARCH_ANALYSTS", "DEPOSITORIES_PARTICIPANTS", "RTA",
                        "DEBENTURE_TRUSTEES", "CREDIT_RATING", "FPI", "AIF", "VCF", "KRA", "BANKERS_TO_ISSUE",
                        "UNDERWRITERS", "CUSTODIAN", "Depositories Act"}


def _rank(category: str) -> int:
    return PRECEDENCE.index(category)


# --------------------------------------------------------------------------
# Provisions → categories (protocol §3.1–3.8)
# --------------------------------------------------------------------------

def category_of(regime: str, provision: str) -> tuple[str, str] | None:
    """(category or NARRATIVE / ISSUE, rule) for one violated provision, or None."""
    top = provision.split("(")[0]
    if top == "2":
        # Reg./Sec. 2 is the definitions ("insider", "connected person", "related
        # party", "prospectus"): recited with the charge, never a charge itself
        return None
    if regime == "PIT":
        # 1992 Reg. 3/3A/4 and 2015 Reg. 3(1)-(4)/4: dealing on or communicating UPSI.
        # 2015 Reg. 3(5)-(6) (the digital database), 7-9, 12-13 and the codes of
        # conduct are disclosure / compliance duties.
        if top in {"3", "3A", "4"} and not provision.startswith(("3(5)", "3(6)")):
            return INSIDER_TRADING, "pit_trading"
        return DISCLOSURE_FAILURE, "pit_disclosure"
    if regime == "SEBI Act":
        if provision.startswith(("12A(d)", "12A(e)")):
            return INSIDER_TRADING, "sebi_act_12A_de"
        if provision.startswith("12A(f)"):
            return DISCLOSURE_FAILURE, "sebi_act_12A_f"
        if top == "12A":
            return NARRATIVE, "sebi_act_12A"
        if provision.startswith("12(1B)") or top in {"11AA"}:
            return OTHER, "cis"
        if top == "12":
            return INTERMEDIARY_MISCONDUCT, "unregistered"
        if top == "11C":
            return OTHER, "non_cooperation"
        return None
    if regime == "PFUTP":
        return NARRATIVE, "pfutp"
    if regime == "LODR":
        if top == "23":
            return (DISCLOSURE_FAILURE, "lodr_23_9") if provision.startswith("23(9)") else (RPT_TUNNELLING, "lodr_23")
        return DISCLOSURE_FAILURE, "lodr"
    if regime in ("SAST", "Listing Agreement", "DEBT_LISTING"):
        return DISCLOSURE_FAILURE, regime.lower().replace(" ", "_")
    if regime in ("ICDR", "DIP"):
        return ISSUE, regime.lower()
    if regime == "Companies Act":
        if top == "188":
            return RPT_TUNNELLING, "companies_act_188"
        if top in {"56", "60", "67", "73"}:
            return OTHER, "deemed_public_issue"
        return OTHER, "companies_act"
    if regime == "SCRA":
        return (DISCLOSURE_FAILURE, "scra_21") if top == "21" else (OTHER, "scra")
    if regime == "SCRR":
        return OTHER, "listing_compliance"
    if regime in ("DELISTING", "BUYBACK"):
        return OTHER, "listing_compliance"
    if regime == "CIS":
        return OTHER, "cis"
    if regime == "DEPOSITORIES_PARTICIPANTS" and top == "55A":
        return DISCLOSURE_FAILURE, "share_capital_audit"  # an issuer's duty under the DP Regulations
    if regime in INTERMEDIARY_REGIMES:
        return INTERMEDIARY_MISCONDUCT, f"intermediary:{regime}"
    return None


# --------------------------------------------------------------------------
# The narrative (protocol §3, "the PFUTP rule")
# --------------------------------------------------------------------------

_I = re.IGNORECASE
PHRASES: dict[str, list[re.Pattern]] = {c: [re.compile(p, _I) for p in ps] for c, ps in {
    # not the regulation's title: "SEBI (Prohibition of Insider Trading) Regulations",
    # "Model Code of Conduct for Prevention of Insider Trading"
    INSIDER_TRADING: [r"unpublished\s+price\s+sensitive", r"\bUPSI\b",
                      r"(?<!prohibition\sof\s)(?<!prevention\sof\s)\binsider\s+trading\b(?!\s*\)?\s*,?\s*regulations)"],
    # "misutilisation" only of issue money or the company's funds: a broker's
    # misuse of *client* funds is INTERMEDIARY_MISCONDUCT (protocol §3.7)
    # The act of taking money out. Whose money decides the category (see
    # route_diversion): issue proceeds → FUND_DIVERSION; the company's own funds
    # to promoters → RPT_TUNNELLING; clients' funds at an intermediary →
    # INTERMEDIARY_MISCONDUCT (protocol §3.2, §3.3, §3.7).
    FUND_DIVERSION: [r"\bdivert\w*|\bdiversion\b", r"\bsiphon\w*",
                     r"\bmis-?utili[sz]\w*\s+(?:of\s+)?(?:the\s+)?(?:IPO|issue|GDR|proceeds|(?:company'?s\s+)?funds\s+"
                     r"(?:raised|of\s+the\s+company))", r"\bmis-?appropriat\w*",
                     r"\bround[\s-]?trip\w*", r"\blayered\s+transactions?",
                     # GDR schemes: the issuer pledges the proceeds for the subscriber's loan
                     r"\bpledg\w*\s+(?:(?:the|its|of|their)\s+)*(?:\w+\s+)?(?:GDR|IPO|issue)\s+proceeds|"
                     r"\b(?:GDR|IPO|issue)\s+proceeds\s+(?:\w+\s+){0,6}?as\s+(?:a\s+)?(?:collateral|security)\b",
                     r"\bnot\s+(?:been\s+)?utili[sz]ed\s+(?:for|as\s+per|towards)\s+the\s+(?:stated\s+)?objects?|"
                     r"\bdeviat\w*\s+from\s+the\s+(?:stated\s+)?objects?"],
    RPT_TUNNELLING: [r"\brelated[\s-]+part(?:y|ies)\s+transactions?", r"\bRPTs?\b"],
    # Only phrases that say the accounts were false. "Books of accounts" and
    # "financial statements" alone are in broker inspections and adviser cases
    # too; they are counted separately (ACCOUNTING_CONTEXT) and only add to a
    # score that already has one of these.
    ACCOUNTING_MISSTATEMENT: [r"\bfictitious\s+(?:sales|purchases?|invoices?|revenue|income|bank|debtors|receivables|"
                              r"entries|transactions\s+in\s+(?:the\s+)?books)",
                              r"\binflat\w*\s+(?:its\s+|the\s+)?(?:revenue|profits?|sales|income|turnover|assets|"
                              r"receivables|debtors)", r"\bmis-?stat\w*", r"\bover-?stat\w*|\bunder-?stat\w*",
                              r"\bfalsif\w*", r"\bwindow[\s-]dressing", r"\brevenue\s+recognition",
                              r"\baccounting\s+(?:irregularit\w*|fraud|manipulation)",
                              r"\bmanipulat\w*\s+(?:of\s+)?(?:the\s+|its\s+)?(?:books|accounts|financial)",
                              r"\bbogus\s+(?:sales|invoices?|bills|entries|purchases?|debtors)",
                              r"\bmis-?represent\w*\s+(?:of\s+|in\s+)?(?:its\s+|the\s+)?(?:financials?|financial\s+"
                              r"(?:statements|results|position)|accounts|books)"],
    MARKET_MANIPULATION: [r"\bsynchroni[sz]ed", r"\bcircular\s+trad", r"\breversal\s+trad", r"\bself[\s-]+trad",
                          r"\bwash\s+trad", r"\bartificial\s+(?:volume|price|trading|market)",
                          r"\bmanipulat\w*\s+(?:the\s+)?(?:price|volume|market|scrip)", r"\bprice\s+manipulat",
                          r"\blast\s+traded\s+price|\bLTP\b", r"\bmisleading\s+appearance\s+of\s+trading",
                          r"\bfront[\s-]?runn?\w*", r"\bpump\s+and\s+dump", r"\bstructured\s+trades?",
                          r"\bmatched\s+trades?", r"\bfictitious\s+trades?", r"\btrading\s+pattern", r"\brigg\w+",
                          r"\bspoof\w*|\blayering\b", r"\bprice\s+rise", r"\bconnected\s+(?:entities|clients|group)",
                          r"\bnon[\s-]genuine\s+trades?", r"\bbogus\s+(?:LTCG|long\s+term\s+capital|capital\s+gains?)|"
                          r"\bLTCG\b", r"\bexit\s+providers?"],
    # Advice for a fee (registered or not) charged as fraud on clients.
    # "unregistered" / "without registration" alone also describe collective
    # schemes and deemed public issues: INTERMEDIARY_CONTEXT, which only adds.
    INTERMEDIARY_MISCONDUCT: [r"\binvestment\s+advi[sc]\w*", r"\bresearch\s+analyst", r"\badvisory\s+(?:services|fees?)",
                              r"\bportfolio\s+management\s+services?", r"\bguaranteed\s+returns?|\bassured\s+returns?",
                              r"\bmis-?(?:use|utili[sz]\w*)\s+(?:of\s+)?(?:the\s+)?clients?(?:['’]s|s['’]|['’])?\s*"
                              r"(?:funds?|securities)",
                              r"\bclients?(?:['’]s|s['’]|['’])?\s+(?:funds?|securities)\s+(?:were|was|had\s+been)?\s*(?:mis-?used|"
                              r"mis-?utili[sz]ed|pledged|diverted)", r"\bnon[\s-]segregation"],
    # Money pooled from the public without registration (protocol §3.8 c).
    OTHER: [r"\bcollective\s+investment\s+schemes?", r"\bdeemed\s+public\s+issue", r"\bmobili[sz]\w*\s+(?:of\s+)?"
            r"funds\s+from\s+(?:the\s+)?(?:public|investors)"],
}.items()}
ACCOUNTING_CONTEXT = [re.compile(p, _I) for p in (r"\bbooks?\s+of\s+accounts?", r"\bfinancial\s+statements?",
                                                  r"\bforensic\s+audit",
                                                  r"\baccounting\s+(?:treatment|standards?)", r"\breceivables\b")]
FUND_CONTEXT = [re.compile(p, _I) for p in (r"\b(?:IPO|issue|GDR|rights\s+issue|preferential\s+(?:issue|allotment))\s+"
                                            r"proceeds", r"\bobjects?\s+of\s+the\s+issue",
                                            r"\butili[sz]ation\s+of\s+(?:the\s+)?(?:\w+\s+)?proceeds")]
#: Clients' money or securities: with an intermediary, what a "diversion" or
#: "falsified" narrative is about (protocol §3.2 exclusion, §3.7).
CLIENT_ASSETS = re.compile(r"\bclients?(?:['’]s|s['’]|['’])?\s+(?:funds?|securities|shares|monies|money|assets)\b|"
                           r"\b(?:funds?|securities)\s+(?:and\s+securities\s+|and\s+funds\s+)?of\s+(?:the\s+|its\s+)?"
                           r"clients?\b|"
                           # a registrar holding investors' dividends and shares
                           r"\b(?:investors?|shareholders?)(?:['’]s|s['’]|['’])?\s+(?:unclaimed\s+)?"
                           r"(?:dividends?|shares|securities)\b|"
                           r"\b(?:unclaimed\s+)?(?:dividends?|shares|securities)\s+(?:belonging\s+to|of)\s+(?:the\s+)?"
                           r"(?:investors|shareholders)\b", _I)
#: Tips and recommendations: manipulation when used to move a price (scalping,
#: pump-and-dump), but the adviser's own business when adviser phrases score
#: (protocol §3.7: advice-for-fee fraud is INTERMEDIARY_MISCONDUCT).
TIPS = re.compile(r"\b(?:stock\s+)?tips?\b|\brecommendations?\s+(?:given|made|on)", _I)
REGISTRATION_CONTEXT = [re.compile(p, _I) for p in (r"\bunregistered\b", r"\bwithout\s+(?:obtaining\s+)?(?:a\s+|the\s+)?"
                                                    r"(?:certificate\s+of\s+)?registration")]
INTERMEDIARY_CONTEXT = re.compile(r"\b(?:stock[\s-]*brokers?|sub[\s-]*brokers?|trading\s+members?|"
                                  r"depository\s+participants?|portfolio\s+managers?|merchant\s+bankers?|"
                                  r"share\s+transfer\s+agents?|registrars?\s+(?:and|to\s+(?:an?\s+|the\s+)?)"
                                  r"(?:issue|share\s+transfer)|RTAs?)\b", _I)
#: Insiders the company's money went to: tunnelling (protocol §3.3, 2026-09-30).
TUNNEL_CONTEXT = re.compile(r"\bpromoters?\b|\brelated[\s-]+part(?:y|ies)|\bgroup\s+(?:companies|entities)|"
                            r"\bentities\s+(?:(?:directly|indirectly)\s+)?(?:controlled|owned)\s+by", _I)
#: Counts per phrase are capped, so one repeated phrase cannot outvote the rest.
PHRASE_CAP = 5
#: Categories a narrative can decide, for PFUTP / §12A(a)-(c).
NARRATIVE_CATEGORIES = [INSIDER_TRADING, FUND_DIVERSION, RPT_TUNNELLING, ACCOUNTING_MISSTATEMENT, MARKET_MANIPULATION,
                        INTERMEDIARY_MISCONDUCT, OTHER]
#: A narrative category needs at least this score; UPSI / RPT need more, as they
#: are mentioned in passing in many orders.
MIN_SCORE = {INSIDER_TRADING: 3, FUND_DIVERSION: 2, RPT_TUNNELLING: 3, ACCOUNTING_MISSTATEMENT: 2,
             MARKET_MANIPULATION: 1, INTERMEDIARY_MISCONDUCT: 4, OTHER: 4}


def narrative_scores(text: str) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Capped phrase counts per category, and the phrases that matched."""
    scores, hits = {}, {}
    for cat, patterns in PHRASES.items():
        total, found = 0, []
        for p in patterns:
            n = len(p.findall(text))
            if n:
                total += min(n, PHRASE_CAP)
                found.append(f"{p.search(text).group(0).lower()}×{n}")
        scores[cat], hits[cat] = total, found
    for cat, patterns in ((ACCOUNTING_MISSTATEMENT, ACCOUNTING_CONTEXT), (INTERMEDIARY_MISCONDUCT, REGISTRATION_CONTEXT)):
        if strong := scores[cat]:
            # context can at most double what the category's own phrases score
            context = 0
            for p in patterns:
                if n := len(p.findall(text)):
                    context += min(n, PHRASE_CAP)
                    hits[cat].append(f"{p.search(text).group(0).lower()}×{n}")
            scores[cat] += min(context, strong)
    if scores[INTERMEDIARY_MISCONDUCT] < MIN_SCORE[INTERMEDIARY_MISCONDUCT] and (n := len(TIPS.findall(text))):
        scores[MARKET_MANIPULATION] += min(n, PHRASE_CAP)
        hits[MARKET_MANIPULATION].append(f"{TIPS.search(text).group(0).lower()}×{n}")
    route_diversion(text, scores, hits)
    return scores, hits


def _move(scores: dict, hits: dict, src: str, dst: str, why: str) -> None:
    scores[dst] += scores[src]
    hits[dst] = hits[dst] + [f"{h} ({why})" for h in hits[src]]
    scores[src], hits[src] = 0, []


def route_diversion(text: str, scores: dict[str, int], hits: dict[str, list[str]]) -> None:
    """Whose money was taken decides the category (protocol §3.2, §3.3, §3.7):

    * clients' funds or securities at a broker / DP (and the records falsified to
      hide it) → INTERMEDIARY_MISCONDUCT;
    * issue money (IPO, GDR, rights, preferential proceeds; the objects of the
      issue) → FUND_DIVERSION, the issue context adding up to the act's score;
    * the company's own funds to promoters, related parties, group entities →
      RPT_TUNNELLING (tunnelling);
    * anything else (a collective scheme's collections, a broker's own money) is
      no diversion category.
    """
    if not scores[FUND_DIVERSION] and not scores[ACCOUNTING_MISSTATEMENT]:
        return
    if len(CLIENT_ASSETS.findall(text)) >= 3 and len(INTERMEDIARY_CONTEXT.findall(text)) >= 3:
        _move(scores, hits, FUND_DIVERSION, INTERMEDIARY_MISCONDUCT, "clients' assets")
        _move(scores, hits, ACCOUNTING_MISSTATEMENT, INTERMEDIARY_MISCONDUCT, "clients' assets")
        return
    if not (act := scores[FUND_DIVERSION]):
        return
    issue = sum(min(len(p.findall(text)), PHRASE_CAP) for p in FUND_CONTEXT)
    if issue >= 2:
        hits[FUND_DIVERSION].extend(f"{m.group(0).lower()}" for p in FUND_CONTEXT if (m := p.search(text)))
        scores[FUND_DIVERSION] += min(issue, act)
    elif scores[OTHER] < MIN_SCORE[OTHER] and len(TUNNEL_CONTEXT.findall(text)) >= 2:
        _move(scores, hits, FUND_DIVERSION, RPT_TUNNELLING, "company funds")
    else:
        scores[FUND_DIVERSION], hits[FUND_DIVERSION] = 0, []


def read_narrative(scores: dict[str, int], among=NARRATIVE_CATEGORIES) -> tuple[str | None, str]:
    """(category, confidence) the narrative supports, among ``among``."""
    ranked = sorted((c for c in among if scores.get(c, 0) >= MIN_SCORE[c]),
                    key=lambda c: (-scores[c], _rank(c)))
    if not ranked:
        return None, "low"
    # Protocol: "if two apply, the precedence order decides". Another category
    # applies when it is a clear narrative in its own right (>= 5) and scores at
    # least half the strongest; a passing mention does not.
    top = min((c for c in ranked if c == ranked[0] or (scores[c] >= 5 and 2 * scores[c] >= scores[ranked[0]])),
              key=_rank)
    if top != ranked[0]:
        return top, "medium"
    second = scores[ranked[1]] if len(ranked) > 1 else 0
    if scores[top] >= 5 and scores[top] >= 2 * second:
        return top, "high"
    return top, ("medium" if scores[top] > second else "low")


# --------------------------------------------------------------------------
# Fallbacks (no violated provision)
# --------------------------------------------------------------------------

#: Penalty sections in the imposing sentence → category.
_PENALTY_SECTION = [
    (re.compile(r"\b15\s*-?\s*G\b", _I), INSIDER_TRADING, "penalty_15G"),
    (re.compile(r"\b15\s*-?\s*C\b", _I), OTHER, "investor_grievances"),
    (re.compile(r"\b15\s*-?\s*A\s*\(\s*a\s*\)", _I), OTHER, "non_cooperation"),
    (re.compile(r"\b15\s*-?\s*A\s*\(\s*b\s*\)|\b15\s*-?\s*H\b(?!\s*[AB])", _I), DISCLOSURE_FAILURE, "penalty_15A_b"),
    (re.compile(r"\b15\s*-?\s*(?:A\s*\(\s*c\s*\)|B|E|EA|EB|F)\b", _I), INTERMEDIARY_MISCONDUCT, "penalty_intermediary"),
    (re.compile(r"\b15\s*-?\s*D\b", _I), OTHER, "cis"),
    (re.compile(r"\b15\s*-?\s*HA\b", _I), NARRATIVE, "penalty_15HA"),
]
_TITLE = [
    (re.compile(r"^\s*(?:corrigendum|addendum|erratum)\b", _I), OTHER, "corrigendum"),
    (re.compile(r"\bexemption\b|\brevok\w+|\brevocation\b", _I), OTHER, "no_violation"),
    (re.compile(r"insider\s+trading|\bUPSI\b", _I), INSIDER_TRADING, "title:insider"),
    (re.compile(r"illiquid\s+stock\s+options?|front[\s-]?running|manipulat|synchroni|circular\s+trad", _I),
     MARKET_MANIPULATION, "title:trading"),
    (re.compile(r"related\s+party", _I), RPT_TUNNELLING, "title:rpt"),
    (re.compile(r"financial\s+statements|accounting|books\s+of\s+account", _I), ACCOUNTING_MISSTATEMENT, "title:accounts"),
    (re.compile(r"diver(?:t|sion)|utili[sz]ation\s+of\s+(?:IPO|issue)", _I), FUND_DIVERSION, "title:diversion"),
    (re.compile(r"investor\s+grievances?|\bSCORES\b", _I), OTHER, "investor_grievances"),
    (re.compile(r"collective\s+investment|\bCIS\b|deemed\s+public\s+issue|mobili[sz]\w+\s+(?:of\s+)?funds", _I),
     OTHER, "cis"),
    (re.compile(r"unregistered\s+(?:investment|portfolio|research)|investment\s+advis", _I), INTERMEDIARY_MISCONDUCT,
     "title:adviser"),
    (re.compile(r"take\s*-?\s*over|\bSAST\b|open\s+offer|disclosures?\b", _I), DISCLOSURE_FAILURE, "title:disclosure"),
    (re.compile(r"summons|non[\s-]cooperation|furnish\s+information", _I), OTHER, "non_cooperation"),
    (re.compile(r"\bmember\b|stock[\s-]*brokers?|sub[\s-]*brokers?|share[\s-]*brokers?|\bbroking\b|"
                r"depository\s+participant|merchant\s+banker|portfolio\s+manager", _I),
     INTERMEDIARY_MISCONDUCT, "title:intermediary"),
]

#: PFUTP clauses that describe trading (1995: 4(a)-(d); 2003: 4(2)(a), (b), (e), (g), (q)),
#: and clauses about misleading information (2003: 4(2)(f), (k), (r)).
_PFUTP_TRADING = re.compile(r"^4\((?:2\)\((?:a|b|e|g|q)|a|b|c|d)\)")
_PFUTP_INFORMATION = re.compile(r"^4\(2\)\((?:f|k|r)\)")


def regimes_named(text: str) -> list[str]:
    """Regimes named anywhere by title or abbreviation, even without a provision number
    ("the Code of Conduct under the SEBI (Stock Brokers and Sub-Brokers) Regulations")."""
    out = []
    for rx in (rg._TITLE, rg._ABBREV):
        for m in rx.finditer(text):
            code = rg.regime_of(m.group("name") if "name" in rx.groupindex else m.group("abbr"))
            if code and code not in rg.PROCEDURAL:
                out.append(rg.label(code))
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------
# Per order
# --------------------------------------------------------------------------

@dataclass
class Job:
    text_stem: str
    order_id: int | None
    order_type: str | None
    text_path: str | None
    text_status: str | None
    title: str | None
    matter: str | None
    regulations: str        # JSON list, violated provisions
    regimes_cited: str      # JSON list
    penalty_raw: str | None


@dataclass
class OrderCategory:
    text_stem: str
    order_id: int | None
    order_type: str | None
    text_status: str | None
    fraud_category: str | None = None
    fraud_category_confidence: str | None = None
    fraud_category_rule: str | None = None
    fraud_category_raw: str | None = None
    fraud_categories_all: str = "[]"
    narrative_scores: str | None = None
    rules_version: str = RULES_VERSION
    error: str | None = None


def _split(entry: str) -> tuple[str, str]:
    """ "PFUTP 3(a)" → ("PFUTP", "3(a)"); "SEBI Act 12A(d)" → ("SEBI Act", "12A(d)")."""
    regime, _, provision = entry.rpartition(" ")
    return regime, provision


def classify(job: Job, text: str) -> OrderCategory:
    o = OrderCategory(job.text_stem, job.order_id, job.order_type, job.text_status)
    scores, hits = narrative_scores(text)
    o.narrative_scores = json.dumps({c: s for c, s in scores.items() if s})

    def narrative(among=NARRATIVE_CATEGORIES):
        cat, conf = read_narrative(scores, among)
        return cat, conf, "; ".join(hits.get(cat, [])[:6]) if cat else ""

    found: dict[str, tuple[str, str, str]] = {}   # category -> (rule, confidence, evidence)

    def add(cat: str, rule: str, conf: str, evidence: str):
        if cat not in found or {"high": 0, "medium": 1, "low": 2}[conf] < {"high": 0, "medium": 1, "low": 2}[found[cat][1]]:
            found[cat] = (rule, conf, evidence)

    provisions = [_split(x) for x in json.loads(job.regulations or "[]")]
    by_rule: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for regime, prov in provisions:
        if (m := category_of(regime, prov)) is not None:
            by_rule[m].append(f"{regime} {prov}")
    for (cat, rule), provs in by_rule.items():
        cited = ", ".join(provs[:8])
        if cat == NARRATIVE:
            ncat, conf, ev = narrative()
            if ncat is None:
                # No telling narrative: the clauses charged decide, flagged low.
                clauses = [p for r_, p in provisions if r_ == "PFUTP"]
                if any(_PFUTP_TRADING.match(c) for c in clauses) or not any(_PFUTP_INFORMATION.match(c) for c in clauses):
                    add(MARKET_MANIPULATION, f"{rule}:default", "low", cited)
                else:
                    add(DISCLOSURE_FAILURE, f"{rule}:misleading_information", "low", cited)
            else:
                add(ncat, f"{rule}:narrative", conf, f"{cited} | {ev}")
        elif cat == ISSUE:
            if scores[FUND_DIVERSION] >= MIN_SCORE[FUND_DIVERSION]:
                add(FUND_DIVERSION, f"{rule}:diversion", "medium", f"{cited} | " + "; ".join(hits[FUND_DIVERSION][:4]))
            else:
                add(DISCLOSURE_FAILURE, f"{rule}:issue", "medium", cited)
        else:
            add(cat, rule, "high", cited)

    if found:
        basis = "provisions"
    else:
        basis = "fallback"
        for rx, cat, rule in _PENALTY_SECTION:
            if job.penalty_raw and rx.search(job.penalty_raw):
                if cat == NARRATIVE:
                    ncat, _, ev = narrative()
                    if ncat:
                        add(ncat, f"fallback:{rule}:narrative", "low", ev)
                else:
                    add(cat, f"fallback:{rule}", "low", rx.search(job.penalty_raw).group(0))
        if not found:
            for rx, cat, rule in _TITLE:
                src = f"{job.title or ''} {job.matter or ''}"
                if m := rx.search(src):
                    add(cat, f"fallback:{rule}", "low", m.group(0))
                    break
        if not found:
            ncat, _, ev = narrative()
            if ncat and scores[ncat] >= 3:
                add(ncat, "fallback:narrative", "low", ev)
        if not found:
            for regime in json.loads(job.regimes_cited or "[]") + regimes_named(text):
                m = category_of(regime, "")
                if m and m[0] not in (NARRATIVE, ISSUE):
                    add(m[0], f"fallback:cited:{m[1]}", "low", regime)

    if not found:
        o.fraud_category, o.fraud_category_confidence, o.fraud_category_rule = OTHER, "low", "unresolved"
        return o
    ordered = sorted(found, key=_rank)
    top = ordered[0]
    rule, conf, evidence = found[top]
    if basis == "provisions" and len(ordered) > 1 and conf == "high":
        conf = "medium"  # several categories charged; precedence decided
    o.fraud_category, o.fraud_category_confidence, o.fraud_category_rule = top, conf, rule
    o.fraud_category_raw = evidence[:300] or None
    o.fraud_categories_all = json.dumps(ordered)
    return o


def process(job: Job, repo_root: pathlib.Path = REPO_ROOT) -> OrderCategory:
    """Never raises."""
    if job.text_status != "OK" or not job.text_path:
        return OrderCategory(job.text_stem, job.order_id, job.order_type, job.text_status)
    try:
        text = ew.normalise_sentence(ew.strip_repeated_lines((repo_root / job.text_path).read_text(encoding="utf-8")))
        return classify(job, text)
    except Exception as exc:  # pragma: no cover - last-resort guard for a long run
        o = OrderCategory(job.text_stem, job.order_id, job.order_type, job.text_status)
        o.error = f"{type(exc).__name__}: {exc}"[:300]
        return o


def _process_many(jobs, repo_root):
    return [process(j, repo_root) for j in jobs]


# --------------------------------------------------------------------------
# Input / output
# --------------------------------------------------------------------------

def load_jobs(derived: pathlib.Path = DERIVED_DIR) -> list[Job]:
    parties = {r["text_stem"]: r for r in pq.read_table(
        derived / "order_parties.parquet",
        columns=["text_stem", "matter_raw", "regulations", "regimes_cited"]).to_pylist()}
    actions = {r["text_stem"]: r["penalty_raw"] for r in pq.read_table(
        derived / "order_actions.parquet", columns=["text_stem", "penalty_raw"]).to_pylist()}
    titles = {text_stem(r["order_id"], r["source_url"]): r["title"] for r in pq.read_table(
        derived / "listings.parquet", columns=["order_id", "source_url", "title"]).to_pylist()}
    docs = pq.read_table(derived / "text_documents.parquet",
                         columns=["text_stem", "order_id", "order_type", "text_path", "status"]).to_pylist()
    jobs = []
    for d in docs:
        p = parties.get(d["text_stem"], {})
        jobs.append(Job(d["text_stem"], d["order_id"], d["order_type"], d["text_path"], d["status"],
                        titles.get(d["text_stem"]), p.get("matter_raw"), p.get("regulations") or "[]",
                        p.get("regimes_cited") or "[]", actions.get(d["text_stem"])))
    return jobs


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


def write_outputs(rows: list[OrderCategory], out_dir: pathlib.Path) -> pathlib.Path:
    assert_safe_output(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "order_categories.parquet"
    ea._write(sorted(rows, key=lambda r: r.text_stem), OrderCategory, path)
    return path


def run(jobs, *, workers=1, repo_root=REPO_ROOT, chunk=200) -> list[OrderCategory]:
    if workers <= 1:
        return [process(j, repo_root) for j in jobs]
    out: list[OrderCategory] = []
    batches = [jobs[i:i + chunk] for i in range(0, len(jobs), chunk)]
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(_process_many, b, repo_root) for b in batches]):
            out.extend(fut.result())
    return out


def summarise(rows: list[OrderCategory]) -> dict:
    ok = [r for r in rows if r.text_status == "OK"]
    by_type = collections.defaultdict(collections.Counter)
    for r in ok:
        by_type[r.order_type][r.fraud_category] += 1
    return {
        "orders_with_text": len(ok),
        "fraud_category": dict(collections.Counter(r.fraud_category for r in ok).most_common()),
        "confidence": dict(collections.Counter(r.fraud_category_confidence for r in ok).most_common()),
        "basis": dict(collections.Counter(("fallback" if (r.fraud_category_rule or "").startswith("fallback")
                                           else "unresolved" if r.fraud_category_rule == "unresolved"
                                           else "provisions") for r in ok).most_common()),
        "by_type": {t: dict(c.most_common()) for t, c in sorted(by_type.items(), key=lambda kv: str(kv[0]))},
        "top_rules": dict(collections.Counter(r.fraud_category_rule for r in ok).most_common(15)),
        "errors": sum(r.error is not None for r in rows),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.classify_fraud",
                                     description="Fraud category from regulations and narrative (runbook Week 3, Day 1)")
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR, help="where the inputs are")
    parser.add_argument("--category", action="append", choices=["WTM", "SETTLEMENT", "AO", "ED_CGM"])
    parser.add_argument("--order-id", action="append", type=int)
    parser.add_argument("--stem", action="append")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: write order_categories.parquet and the log under DIR instead of derived/")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_dir = args.scratch or DERIVED_DIR
    log_file = (args.scratch / "classify_fraud.log") if args.scratch else LOGS_DIR / "classify_fraud.log"
    for p in (out_dir, log_file):
        assert_safe_output(p)
    setup_logging(args.log_level, log_file)
    jobs = select(load_jobs(args.derived), categories=args.category, order_ids=args.order_id, stems=args.stem,
                  sample=args.sample, limit=args.limit)
    log.info("%s: %d orders with %d worker(s) -> %s", RULES_VERSION, len(jobs), args.workers, out_dir)
    started = time.monotonic()
    rows = run(jobs, workers=args.workers)
    path = write_outputs(rows, out_dir)
    log.info("=" * 72)
    for k, v in summarise(rows).items():
        if k == "by_type":
            for t, line in v.items():
                log.info("  %-10s %s", t, line)
        else:
            log.info("%-18s %s", k, v)
    log.info("elapsed %.1f min; wrote %s", (time.monotonic() - started) / 60, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
