"""Regulations cited in an order (runbook Week 2, Day 5).

The runbook stores the regulations as a JSON list such as ``["PFUTP 3(a)",
"SEBI Act 12A"]``. Week 3 maps them to a fraud category (PIT → insider trading,
LODR 23 → related-party tunnelling, Intermediaries → intermediary misconduct,
PFUTP 3/4 → read the narrative), so what matters is *which provision of which
instrument the order says was violated* — not every provision it mentions.

How orders cite (corpus survey, 4,000 orders, ``hand/recon_notes.md`` §9)
------------------------------------------------------------------------
A provision list, then "of" and the instrument::

    Regulations 3(a), (b), (c), (d), 4(1) and 4(2)(a) of the SEBI (Prohibition of
    Fraudulent and Unfair Trade Practices relating to Securities Market)
    Regulations, 2003 (hereinafter referred to as "PFUTP Regulations")
    Section 12A(a), (b), (c) of the SEBI Act
    Clause 49 of the Listing Agreement

The instrument is named by its full title (487 spellings of ~40 instruments),
by a common abbreviation ("PFUTP Regulations", "Takeover Regulations"), or by
the alias the order defines right after the title ("hereinafter referred to as
'the Regulations'"). Each is mapped to one regime code (``PFUTP``, ``PIT`` …)
and the year where stated (PFUTP 1995/2003, PIT 1992/2015, SAST 1997/2011).

Role of a citation
------------------
* ``penalty`` — a penalty section: SEBI Act 15A–15HB, SCRA 23A–23H,
  Depositories Act 19A–19G.
* ``power`` — the power or procedure the order is passed under: SEBI Act 11,
  11A, 11B, 11D, 12(3), 15I, 15J, 15JB, 19, 28A (recovery); SCRA 12A, 23I,
  23JA; Depositories Act 19H;
  the adjudication rules, enquiry and settlement regulations.
* ``violation`` — the text just before the citation (since the previous one, in
  the same sentence) says violated / contravened / breached / failed / alleged,
  or the citation is chained to a violation ("… read with …").
* ``cited`` — anything else (definitions quoted, case law, background).

A citation with no instrument after it ("Section 15HA and 15HB" alone) is
resolved to the SEBI Act only for sections that exist only there (11–11D, 12,
12A, 15A–15JB); otherwise it is left out and counted as unresolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_I = re.IGNORECASE

# --------------------------------------------------------------------------
# Regimes
# --------------------------------------------------------------------------

#: regime code → words that identify it inside a title's parentheses or an
#: abbreviation. Order matters: the first match wins.
_REGIME_WORDS: list[tuple[str, str]] = [
    ("PFUTP", r"fraudulent|unfair\s+trad|\bP?\.?\s?FUTP\b"),
    ("PIT", r"insider\s+trading|\bPIT\b|\binsider\b"),
    ("SAST", r"substantial\s+acquisition|take[\s-]*overs?\b|\bSAST\b"),
    ("ICDR", r"issue\s+of\s+capital|\bICDR\b"),
    ("DIP", r"disclosure\s+and\s+investor\s+protection|\bDIP\b"),
    ("LODR", r"listing\s+obligations?|\bLODR\b|^listing$"),
    ("ENQUIRY_REGS", r"enquiry\s+by\s+(?:the\s+)?enquiry\s+officer|^enquiry$"),
    ("ADJUDICATION_RULES", r"procedure\s+for\s+holding|imposing\s+penalt|^(?:adjudicat\w*|AO)$"),
    ("SETTLEMENT", r"settlement|consent"),
    ("INTERMEDIARIES", r"intermediar"),
    ("STOCK_BROKERS", r"brokers?\b"),
    ("MERCHANT_BANKERS", r"merchant\s+bank"),
    ("PORTFOLIO_MANAGERS", r"portfolio\s+manag|^PMS?$"),
    ("MUTUAL_FUNDS", r"mutual\s+funds?|^MF$"),
    ("INVESTMENT_ADVISERS", r"investment\s+advis|^IA$"),
    ("RESEARCH_ANALYSTS", r"research\s+analyst|^RA$"),
    ("CIS", r"collective\s+investment|^CIS$"),
    ("DEPOSITORIES_PARTICIPANTS", r"depositor(?:y|ies)\s+(?:and\s+)?participants?|^D\s?&\s?P$|^DP$"),
    ("RTA", r"registrars?\s+to\s+(?:an\s+)?issue|share\s+transfer|^RTA$"),
    ("DEBENTURE_TRUSTEES", r"debenture\s+trustee"),
    ("CREDIT_RATING", r"credit\s+rating|^CRA$"),
    ("DEBT_LISTING", r"listing\s+of\s+(?:debt|non[\s-]*convertible)|^(?:ILDS|NCS|ILNCS)$"),
    ("DELISTING", r"delisting"),
    ("BUYBACK", r"buy[\s-]*back"),
    ("FPI", r"foreign\s+(?:portfolio|institutional)|^(?:FPI|FII)$"),
    ("AIF", r"alternative\s+investment|^AIF$"),
    ("VCF", r"venture\s+capital"),
    ("KRA", r"KYC|know\s+your\s+client|^KRA$"),
    ("BANKERS_TO_ISSUE", r"bankers?\s+to\s+(?:an\s+)?issue"),
    ("UNDERWRITERS", r"underwriter"),
    ("CUSTODIAN", r"custodian"),
    ("OMBUDSMAN", r"ombudsman"),
]
_REGIME_RES = [(code, re.compile(p, _I)) for code, p in _REGIME_WORDS]

PROCEDURAL = {"ADJUDICATION_RULES", "ENQUIRY_REGS", "SETTLEMENT", "OMBUDSMAN"}

#: How a regime is written in the ``regulations`` list.
LABELS = {"SEBI_ACT": "SEBI Act", "SCRA": "SCRA", "SCRR": "SCRR", "DEPOSITORIES_ACT": "Depositories Act",
          "COMPANIES_ACT": "Companies Act", "LISTING_AGREEMENT": "Listing Agreement"}


def regime_of(name: str) -> str | None:
    """Regime code for the words inside a title's parentheses or an abbreviation.

    Args:
        name (str): words from a title's parentheses or an abbreviation, e.g. "Prohibition of Insider Trading".
    Returns:
        str | None: the regime code (PFUTP, PIT, SAST …), or None.
    """
    name = " ".join(name.split())
    for code, rx in _REGIME_RES:
        if rx.search(name):
            return code
    return None


def label(regime: str) -> str:
    """How a regime code is written in the ``regulations`` list.

    Args:
        regime (str): a regime code, e.g. "SEBI_ACT".
    Returns:
        str: its label, e.g. "SEBI Act" (other codes are used as they are).
    """
    return LABELS.get(regime, regime)


# --------------------------------------------------------------------------
# Instrument references (what follows "of")
# --------------------------------------------------------------------------

_YEAR = r"(?:\s*,?\s*(?P<year>(?:19|20)\d\d)\b)?"
_KIND = r"(?:Regulations?|Regs?\b\.?|Rules|Guidelines|Code)"
#: "SEBI (Prohibition of Insider Trading) Regulations, 2015"; nested brackets allowed once.
_TITLE = re.compile(
    r"(?:the\s+)?(?:Securities\s+and\s+Exchange\s+Board\s+of\s+India|SEBI)\s*"
    r"\((?P<name>[^()]{2,200}?(?:\([^()]{1,40}\)[^()]{0,120}?)?)\)\s*(?:\(\s*Amendments?\s*\)\s*)?"
    rf"{_KIND}{_YEAR}", _I)
_ACTS: list[tuple[str, re.Pattern]] = [
    # "Securities Contracts (Regulation) (Procedure for Holding Inquiry and Imposing Penalties) Rules, 2005"
    ("ADJUDICATION_RULES", re.compile(r"(?:the\s+)?(?:Securities\s+Contracts?\s*\(?\s*Regulations?\s*\)?|Depositories)"
                                      r"\s*\(\s*Procedure\s+for\s+holding[^()]{0,160}\)\s*Rules"
                                      r"(?:\s*,?\s*(?P<year>(?:19|20)\d\d)\b)?", _I)),
    ("SEBI_ACT", re.compile(r"(?:the\s+)?(?:Securities\s+and\s+Exchange\s+Board\s+of\s+India|SEBI)\s+Act"
                            r"(?:\s*,?\s*(?P<year>1992)\b)?", _I)),
    ("SCRA", re.compile(r"(?:the\s+)?(?:Securities\s+Contracts?\s*\(\s*Regulations?\s*\)\s*Act|SCRA\b|SC\s?\(R\)\s?A\b)"
                        r"(?:\s*,?\s*(?P<year>1956)\b)?", _I)),
    ("SCRR", re.compile(r"(?:the\s+)?(?:Securities\s+Contracts?\s*\(\s*Regulations?\s*\)\s*Rules|SCRR\b|SC\s?\(R\)\s?R\b)"
                        r"(?:\s*,?\s*(?P<year>1957)\b)?", _I)),
    ("DEPOSITORIES_ACT", re.compile(r"(?:the\s+)?Depositories\s+Act(?:\s*,?\s*(?P<year>1996)\b)?", _I)),
    ("COMPANIES_ACT", re.compile(r"(?:the\s+)?Companies\s+Act(?:\s*,?\s*(?P<year>1956|2013)\b)?", _I)),
    ("LISTING_AGREEMENT", re.compile(r"(?:the\s+)?(?:equity\s+|simplified\s+|uniform\s+)?Listing\s+Agreements?"
                                     r"(?P<year>)", _I)),
]
#: "PFUTP Regulations, 2003", "Takeover Regulations", "the Stock Brokers Regulations".
_ABBREV = re.compile(
    r"(?:the\s+)?(?:SEBI\s+)?(?P<abbr>P\.?\s?FUTP|FUTP|PIT|SAST|ICDR|LODR|DIP|CIS|IA|RA|PMS?|MF|AIF|FPI|FII|KRA|NCS|"
    r"ILDS|ILNCS|D\s?&\s?P|DP|CRA|RTA|Take[\s-]*overs?|Insider\s+Trading|Intermediar(?:y|ies)|"
    r"(?:Stock[\s-]+)?(?:Sub[\s-]*)?Brokers?(?:\s+and\s+Sub[\s-]*Brokers?)?|Listing|Settlement|Adjudication|"
    r"Adjudicating|AO|Enquiry|Delisting|Buy[\s-]*back|Mutual\s+Funds?|Portfolio\s+Managers?|Merchant\s+Bankers?|"
    r"Investment\s+Advis[eo]rs?|Research\s+Analysts?|Depositor(?:y|ies)\s+(?:and\s+)?Participants?|"
    r"Debenture\s+Trustees?|Credit\s+Rating(?:\s+Agenc(?:y|ies))?|Collective\s+Investment\s+Schemes?|"
    rf"Registrars?)\s+{_KIND}{_YEAR}", _I)
#: Strong abbreviations that stand alone: "Regulation 3 of PFUTP".
_BARE_ABBREV = re.compile(r"(?:the\s+)?(?P<abbr>P\.?\s?FUTP|FUTP|PIT|SAST|ICDR|LODR)\b" + _YEAR)
#: "the Act", "the said Regulations" — resolved through the order's own aliases.
_GENERIC = re.compile(r"(?:the\s+)?(?:said\s+|aforesaid\s+|above\s+|above[\s-]mentioned\s+)?(?P<g>Act|Regulations|Rules)\b",
                      _I)


@dataclass(frozen=True)
class Instrument:
    """An instrument a citation refers to.

    Attributes:
        regime (str): its code; year (int | None): where stated;
        how (str): how it was named — title, act, abbrev, alias, generic or default.
    """
    regime: str
    year: int | None
    how: str  # title / act / abbrev / alias / generic / default


def _year(m: re.Match) -> int | None:
    """The ``year`` group of a match, as an int.

    Args:
        m (re.Match): a match with an optional ``year`` group.
    Returns:
        int | None: the year, or None.
    """
    y = m.groupdict().get("year")
    return int(y) if y else None


def match_instrument(text: str, pos: int, aliases: dict[str, Instrument]) -> tuple[Instrument, int] | None:
    """The instrument named at ``text[pos:]`` and where its name ends.

    Args:
        text (str): the sentence.
        pos (int): where the instrument's name should start (after "of").
        aliases (dict[str, Instrument]): the order's own aliases (find_aliases).
    Returns:
        tuple[Instrument, int] | None: the instrument and the offset where its name ends, or None.
    """
    m = _TITLE.match(text, pos)
    if m and (code := regime_of(m.group("name"))):
        return Instrument(code, _year(m), "title"), m.end()
    for code, rx in _ACTS:
        if m := rx.match(text, pos):
            return Instrument(code, _year(m), "act"), m.end()
    for rx in (_ABBREV, _BARE_ABBREV):
        m = rx.match(text, pos)
        if m and (code := regime_of(m.group("abbr"))):
            year = _year(m)
            # an abbreviation the order defined carries the defined year
            alias = aliases.get(_akey(m.group(0)))
            if year is None and alias and alias.regime == code:
                year = alias.year
            return Instrument(code, year, "abbrev"), m.end()
    for key, inst in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
        m = re.compile(r"(?:the\s+)?" + r"\s+".join(map(re.escape, key.split())) + r"\b", _I).match(text, pos)
        if m:
            return Instrument(inst.regime, inst.year, "alias"), m.end()
    if m := _GENERIC.match(text, pos):
        g = m.group("g").lower()
        inst = aliases.get(g)
        if inst:
            return Instrument(inst.regime, inst.year, "alias"), m.end()
        if g == "act":
            return Instrument("SEBI_ACT", None, "generic"), m.end()
    return None


# --------------------------------------------------------------------------
# Aliases the order defines
# --------------------------------------------------------------------------

_DEF_OPEN = re.compile(r"\s*,?\s*[(\[]")
_QUOTED = re.compile(r"[“\"‘'`]{1,2}\s*(?:the\s+)?(?P<a>[^”\"’'`()]{2,60}?)\s*[”\"’'`]{1,2}")
_NOT_ALIAS = re.compile(r"^(?:SEBI|Board|Company|Noticee|Noticees|AO|IP|Investigation\s+Period)$", _I)


def _akey(s: str) -> str:
    """An alias's lookup key: lower case, no leading “the”, no trailing year.

    Args:
        s (str): an alias as written.
    Returns:
        str: the key.
    """
    s = re.sub(r"^\s*the\s+", "", s.strip(), flags=_I)
    s = re.sub(r"\s*,?\s*(?:19|20)\d\d\s*$", "", s)
    return " ".join(s.lower().split())


def find_aliases(flat: str) -> dict[str, Instrument]:
    """Aliases defined right after an instrument's title:
    "... Regulations, 2003 (hereinafter referred to as 'PFUTP Regulations' / 'the Regulations')".

    Args:
        flat (str): the order's text (normalised).
    Returns:
        dict[str, Instrument]: alias key → the instrument it stands for.
    """
    out: dict[str, Instrument] = {}
    starts = [m.start() for m in re.finditer(r"Securities\s+and\s+Exchange|SEBI|Securities\s+Contracts?|"
                                             r"Depositories\s+Act|Companies\s+Act|Listing\s+Agreement", flat, _I)]
    for s in starts:
        hit = match_instrument(flat, s, {})
        if not hit or hit[0].how == "generic":
            continue
        inst, end = hit
        if not (o := _DEF_OPEN.match(flat, end)):
            continue
        close = flat.find(")", o.end())
        close = close if 0 <= close - o.end() <= 160 else o.end() + 160
        inside = flat[o.end():close + 1]
        if not re.match(r"\s*(?:here\s*in\s*after|[“\"‘'`]|for\s+short|also)", inside, _I):
            continue
        for q in _QUOTED.finditer(inside):
            a = q.group("a").strip()
            if _NOT_ALIAS.match(a) or not re.search(r"[A-Za-z]{2}", a):
                continue
            out.setdefault(_akey(a), inst)
    return out


# --------------------------------------------------------------------------
# Provision lists
# --------------------------------------------------------------------------

_KW_FAMILY = {"regulation": "regulation", "reg": "regulation", "section": "section", "sec": "section", "u/": "section",
              "clause": "clause", "rule": "rule", "para": "para", "paragraph": "para", "article": "article"}
_KW_WORDS = (r"(?:sub[\s-]*)?(?:regulations?|regs?\.?|sections?|secs?\.?|clauses?|rules?|paragraphs?|paras?\.?|"
             r"articles?)|u/s\.?")
_KW_NC = rf"(?i:{_KW_WORDS})\s*(?i:nos?\.?\s*)?"
#: A sub-clause: "(a)", "(2)", "(iv)", "(1A)".
_SUB = r"\(\s*(?:\d{1,3}[A-Za-z]{0,2}|[a-z]{1,5}|[A-Z])\s*\)"
#: Upper-case words that follow a number but are not a section suffix ("11 AND 11B", "13(4) OF").
_STOP = r"(?:AN|AND|AS|AT|BE|BY|FOR|IN|IS|IT|NO|NOT|OF|ON|OR|THE|TO|WAS|ARE|HAS|BUT|ALL|ANY|R)\b"
#: "3", "12A", "15HA", "15 HA", "15-I", "11 B" — never "3 and", never a year.
_TOP = rf"\d{{1,3}}(?!\d)(?:[A-Z]{{1,3}}\b|\s?-\s?[A-Z]{{1,3}}\b|\s(?!{_STOP})[A-Z]{{1,3}}\b)?"
_ITEM = rf"(?:{_TOP}(?:\s*{_SUB})*|{_SUB}(?:\s*{_SUB})*)"
_SEP = (r"(?:\s*,\s*(?:(?i:and)\s+|&\s*)?|\s+(?i:and|&|or|as\s+well\s+as|read\s+with|r/w|r\.w\.|along\s*with|to)\s+|"
        r"\s*/\s*|\s*&\s*)")
_CITATION = re.compile(rf"\b(?P<kw>(?i:{_KW_WORDS}))\s*(?i:nos?\.?\s*)?(?=\d)"
                       rf"(?P<list>{_TOP}(?:\s*{_SUB})*(?:{_SEP}(?:{_KW_NC})?{_ITEM})*)")
_ITEM_RE = re.compile(rf"(?:{_KW_NC})?(?P<item>{_ITEM})")
_KW_RE = re.compile(rf"(?P<kw>(?i:{_KW_WORDS}))")
_OF = re.compile(r"\s*,?\s*(?:of|under|in|contained\s+in)\s+", _I)

_PENALTY_SECTIONS = {
    "SEBI_ACT": re.compile(r"^15(?:A|B|C|D|E|EA|EB|F|G|H|HA|HAA|HB)$"),
    "SCRA": re.compile(r"^23(?:A|B|C|D|E|F|G|GA|H)$"),
    "DEPOSITORIES_ACT": re.compile(r"^19(?:A|B|C|D|E|F|G)$"),
}
_POWER_SECTIONS = {
    # 28A: recovery ("recovery proceedings may be initiated under Section 28A")
    "SEBI_ACT": re.compile(r"^(?:11|11A|11AA|11B|11D|15I|15J|15JA|15JB|15T|15U|15Y|19|24|26|28A|28B)$"),
    "SCRA": re.compile(r"^(?:12A|23I|23J|23JA|23JB)$"),
    "DEPOSITORIES_ACT": re.compile(r"^(?:19H|19I|19IA|19IB)$"),
}
#: Provisions of regulations that are the Board's power or procedure, not duties:
#: PFUTP 11/12 and PIT 11 (directions), SAST 1997 44/45 and 2011 32
#: (directions), CIS 65/73, Intermediaries 23–38 (action in case of default,
#: summary proceedings 33A/33B).
_REG_POWER = {"PFUTP": {"11", "12"}, "PIT": {"11"}, "CIS": {"65", "73"}, ("SAST", 1997): {"44", "45"},
              ("SAST", 2011): {"32"}, ("SAST", None): {"44", "45"},
              "INTERMEDIARIES": {str(n) for n in range(23, 39)} | {"33A", "33B"}}
#: Sections that exist only in the SEBI Act: a bare "Section 15HA" is the SEBI Act.
_SEBI_ONLY = re.compile(r"^(?:11|11A|11B|11C|11D|12|12A|15[A-Z]{1,3})$")

_VIOLATION_CUE = re.compile(r"violat\w*|contraven\w*|breach\w*|non[\s-]*complian\w*|not\s+compl\w*|"
                            r"fail\w*|infring\w*|alleg\w*|charg\w*|guilty|flout\w*|in\s+default|default\w*|"
                            # settlement orders: "the delayed compliance of Regulation 29(1)"
                            r"delay\w*|late\s+(?:compliance|disclosure|filing|submission)|non[\s-]*disclos\w*|"
                            r"lapses?\b|irregularit\w*", _I)
_VIOLATED_AFTER = re.compile(r"[^.;]{0,40}?\b(?:violated|contravened|breached|not\s+(?:been\s+)?complied|"
                             r"non[\s-]*complian\w*)", _I)
#: The §15J Explanation (2019), quoted in most later AO orders: "the power of an
#: adjudicating officer to adjudge the quantum of penalty under sections 15A to 15E,
#: clauses (b) and (c) of section 15F, 15G, 15H and 15HA shall be … deemed …". The
#: sections it lists are the officer's power, not penalties imposed on anyone.
_ADJUDGE_POWER = re.compile(r"\bpower\s+(?:of\s+(?:an?|the)\s+adjudicating\s+officer\s+)?to\s+adjudge\s+"
                            r"(?:the\s+)?quantum\s+of\s+penalty", _I)
_CHAIN = re.compile(r"^(?:[\s,;]|\band\b|&|\bor\b|read\s+with|r/w|r\.w\.|as\s+well\s+as|along\s*with|\bwith\b|"
                    r"\balso\b)*$", _I)


def _family(kw: str) -> str:
    """The keyword family of a citation word.

    Args:
        kw (str): "Regulations", "Sec.", "sub-clause", "u/s" ….
    Returns:
        str: regulation, section, clause, rule, para, article or other.
    """
    k = re.sub(r"^sub[\s-]*", "", kw.lower()).rstrip(".")
    k = k[:-1] if k.endswith("s") else k
    return _KW_FAMILY.get(k, "other")


def _norm_top(top: str) -> str:
    """A top-level provision number normalised: spaces and hyphens dropped, upper case.

    Args:
        top (str): e.g. "15 ha".
    Returns:
        str: e.g. "15HA".
    """
    return re.sub(r"[\s-]", "", top).upper()


def expand(list_text: str) -> list[str]:
    """"3(a), (b), 4(1) and 4(2)(a), (e)" → ["3(a)", "3(b)", "4(1)", "4(2)(a)", "4(2)(e)"].

    Args:
        list_text (str): a provision list as written.
    Returns:
        list[str]: each provision spelled in full, unique, in order.
    """
    out: list[str] = []
    top, subs = None, []
    for m in _ITEM_RE.finditer(list_text):
        item = m.group("item").strip()
        if item[:1].isdigit():
            t = re.match(_TOP, item)
            top = _norm_top(t.group(0))
            subs = re.findall(r"\(\s*([^()\s]+)\s*\)", item[t.end():])
        elif top is not None:
            found = re.findall(r"\(\s*([^()\s]+)\s*\)", item)
            # a bare "(b)" replaces the deepest level of the previous item
            subs = subs[:max(0, len(subs) - len(found))] + found
        else:
            continue
        out.append(top + "".join(f"({x})" for x in subs))
    return list(dict.fromkeys(out))


def top_of(provision: str) -> str:
    """A provision's top level.

    Args:
        provision (str): e.g. "4(2)(a)".
    Returns:
        str: e.g. "4".
    """
    return provision.split("(", 1)[0]


@dataclass
class Citation:
    """One provision cited in a sentence.

    Attributes:
        regime (str), year (int | None), provision (str), keyword (str), role (str): violation, penalty,
        power or cited; how (str): how the instrument was named; start (int): offset in the sentence;
        text (str): the citation as written.
    """
    regime: str
    year: int | None
    provision: str       # "3(a)", "12A", "4(2)(e)"
    keyword: str         # regulation / section / clause / rule / para / article
    role: str            # violation / penalty / power / cited
    how: str             # title / act / abbrev / alias / generic / default
    start: int           # offset in the sentence
    text: str            # the citation as written


def role_for(regime: str, provision: str, year: int | None = None) -> str | None:
    """The role a provision has by its nature alone (penalty or power sections), if any.

    Args:
        regime (str): the instrument's code.
        provision (str): the provision.
        year (int | None): the instrument's year (SAST 1997 and 2011 differ).
    Returns:
        str | None: "power" or "penalty", or None (decided from the text around it).
    """
    top = top_of(provision)
    if regime in PROCEDURAL:
        return "power"
    if top in _REG_POWER.get(regime, ()) or top in _REG_POWER.get((regime, year), ()):
        return "power"
    if (rx := _PENALTY_SECTIONS.get(regime)) and rx.match(top):
        return "penalty"
    if (rx := _POWER_SECTIONS.get(regime)) and rx.match(top):
        return "power"
    if regime == "SEBI_ACT" and provision.startswith("12(3)"):
        return "power"
    return None


def find_citations(sentence: str, aliases: dict[str, Instrument]) -> tuple[list[Citation], int]:
    """Every resolved citation in one sentence, and the number of provisions left unresolved.

    Args:
        sentence (str): one sentence.
        aliases (dict[str, Instrument]): the order's aliases.
    Returns:
        tuple[list[Citation], int]: the resolved citations, and how many provisions had no instrument.
    """
    out: list[Citation] = []
    unresolved = 0
    masked = sentence       # unresolved citations blanked, so they do not break a chain
    prev_end, prev_role = 0, None
    pos = 0
    while m := _CITATION.search(sentence, pos):
        kw = _family(m.group("kw"))
        list_text = m.group("list")
        # A different keyword after "read with" is a different instrument:
        # "Section 12A … read with Regulation 3 of PFUTP" — cut the list there
        # and read the rest as a citation of its own.
        cut = next((k.start() for k in _KW_RE.finditer(list_text) if _family(k.group("kw")) != kw), None)
        if cut is not None:
            list_text = re.sub(rf"{_SEP}$", "", list_text[:cut])
        end = m.start("list") + len(list_text)
        pos = m.start("list") + cut if cut is not None else m.end()
        provisions = expand(list_text)
        if not provisions:
            continue
        inst = None
        if cut is None and (o := _OF.match(sentence, m.end())):
            if hit := match_instrument(sentence, o.end(), aliases):
                inst, end = hit
                pos = end
        if inst is None and kw == "section" and all(_SEBI_ONLY.match(top_of(p)) for p in provisions):
            inst = Instrument("SEBI_ACT", None, "default")
        if inst is None:
            unresolved += len(provisions)
            masked = masked[:m.start()] + " " * (end - m.start()) + masked[end:]
            continue
        between = re.sub(r"\([^()]*\)", " ", masked[prev_end:m.start()])  # skip "(hereinafter …)"
        chained = prev_role is not None and bool(_CHAIN.match(between))
        local = sentence[max(prev_end, m.start() - 250):m.start()]
        for p in provisions:
            if inst.regime == "SEBI_ACT" and p.startswith("151"):
                p = "15I" + p[3:]  # OCR: "15I" read as "151"
            role = role_for(inst.regime, p, inst.year)
            if role == "penalty" and _ADJUDGE_POWER.search(sentence, max(0, m.start() - 200), m.start()):
                role = "power"
            if role is None:
                if chained and prev_role in ("violation", "power"):
                    role = prev_role
                elif _VIOLATION_CUE.search(local) or _VIOLATED_AFTER.match(sentence, end):
                    role = "violation"
                else:
                    role = "cited"
            out.append(Citation(inst.regime, inst.year, p, kw, role, inst.how, m.start(), sentence[m.start():end]))
        prev_end, prev_role = end, out[-1].role
    return out, unresolved


def as_list(citations: list[Citation], roles=("violation",)) -> list[str]:
    """``["PFUTP 3(a)", "SEBI Act 12A"]`` — unique, in order of first mention.

    Args:
        citations (list[Citation]): an order's citations.
        roles (tuple[str, ...]): which roles to keep (default: violations).
    Returns:
        list[str]: e.g. ["PFUTP 3(a)", "SEBI Act 12A"].
    """
    return list(dict.fromkeys(f"{label(c.regime)} {c.provision}" for c in citations if c.role in roles))


def regimes(citations: list[Citation], roles=("violation",)) -> list[str]:
    """The instruments cited in the given roles, unique, in order.

    Args:
        citations (list[Citation]): an order's citations.
        roles (tuple[str, ...]): which roles to keep.
    Returns:
        list[str]: labels such as "PFUTP", "SEBI Act".
    """
    return list(dict.fromkeys(label(c.regime) for c in citations if c.role in roles))
