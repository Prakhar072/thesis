"""The one company-name normaliser (runbook Week 4, Day 1).

Applied identically to both sides of every name comparison: the names in the
orders (``issuer_guess``, ``order_entities``), the listed universe, the hand
coding (``score_validation``). The runbook's rule, plus a short table of the
abbreviations BSE uses in its short scrip names, expanded the same way on both
sides.
"""

from __future__ import annotations

import re

_DROP = re.compile(r"\b(LIMITED|LTD|PVT|PRIVATE|M S|COMPANY|CO|THE|INDIA)\b")
#: BSE short names abbreviate; orders and full issuer names do not.
_EXPAND = {"CORPN": "CORPORATION", "CORP": "CORPORATION", "INDS": "INDUSTRIES", "INTL": "INTERNATIONAL",
           "MFG": "MANUFACTURING", "ENGG": "ENGINEERING", "SECS": "SECURITIES"}
_EXPAND_RE = re.compile(r"\b(" + "|".join(_EXPAND) + r")\b")
#: "(formerly X)", "(now known as Y)": the bracketed name is another name of the same company.
_ALIAS = re.compile(r"[({\[]\s*(?:formerly|now|earlier|erstwhile|previously)(?:\s+known\s+as)?\s*:?\s*([^)}\]]*)[)}\]]?",
                    re.I)


def norm(name: str) -> str:
    """Upper case, punctuation to spaces, & → AND, legal-form and filler words dropped.

    Args:
        name (str): a company name in any form.
    Returns:
        str: the normalised name, e.g. "M/s. Kavveri Telecom Products Pvt. Ltd." → "KAVVERI TELECOM PRODUCTS".
    """
    s = name.upper()
    s = re.sub(r"[^A-Z0-9& ]", " ", s)
    s = s.replace("&", " AND ")
    s = _EXPAND_RE.sub(lambda m: _EXPAND[m.group(1)], s)
    s = _DROP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def names(value) -> list[str]:
    """A name and any former or later name it gives in brackets, each normalised.

    Args:
        value (str | None): a name, possibly with "(formerly …)" / "{earlier known as …}".
    Returns:
        list[str]: the normalised main name, then each bracketed name; [] for an empty value.
    """
    if not isinstance(value, str) or not value.strip():
        return []
    raw = [_ALIAS.sub(" ", value), *_ALIAS.findall(value)]
    out = []
    for n in map(norm, raw):
        if n and n not in out:
            out.append(n)
    return out
