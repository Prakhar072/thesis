"""Week 4 Day 2: match the orders' companies to the listed universe.

Two things are matched:

* **the issuer of each order** (``orders.issuer_guess``: the company whose
  securities the matter is about, often not a noticee);
* **every company among the parties** (``order_entities``, ``entity_type =
  company``): a listed company can itself be a noticee.

Codes first (runbook: "ISIN and scrip code beat names"): an ISIN or a BSE scrip
code printed in the order identifies the company exactly. It is taken as the
issuer when it agrees with the issuer's name, or when the order names no issuer
and prints one company's code. Only 1% of orders print one.

Then names, normalised identically on both sides (``src/names.py``) and scored
with ``rapidfuzz.fuzz.token_sort_ratio`` against every name a listed firm is or
was known by (``derived/name_aliases.parquet``):

* exact (100) or ≥ 92: accepted (``auto``);
* 80–92: hand review (``hand/match_review.csv``);
* < 80: rejected — no listed match (individuals, unlisted firms, regional-exchange-only companies).

Guards: a private company ("Pvt", "Private", LLP) is never listed, so it is not
matched. A fuzzy match must also agree on the distinctive words: words found in
30 or more listed names ("Industries", "Finance", "Paper", "Mills" …) are set
aside and the rest compared by token-set ratio ≥ 80, so "Satia Paper Mills" is
not "Star Paper Mills". A name that is very short after normalising, or two
different firms at ≥ 92, goes to review, not auto-accept.

``hand/match_review.csv`` is the durable review, like ``hand/window_review.csv``:
one row per name in the review band, with the top candidates; the user fills
``decision`` (``accept`` = the first candidate, a ``firm_id`` = that candidate,
``reject`` = none). New names are appended; filled rows are never changed. Applied
decisions give ``match_method = hand``.

Writes ``derived/order_issuers.parquet`` (one row per order),
``derived/entity_matches.parquet`` (one row per company party: the runbook's
``ticker`` / ``isin`` / ``match_confidence`` for ``order_entities``) and
``derived/name_matches.parquet`` (one row per distinct name).
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
from collections import Counter

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

from src import cache
from src.extract_text import assert_safe_output, setup_logging
from src.names import names

log = logging.getLogger("match_entities")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
REVIEW = REPO_ROOT / "hand" / "match_review.csv"
LOG_FILE = REPO_ROOT / "logs" / "match_entities.log"
RULES_VERSION = "match-v1"

AUTO, REVIEW_MIN = 92, 80
#: A word in at least this many distinct listed names is generic; single letters (initials) never are.
GENERIC_DF = 30
CORE_MIN = 80
#: A normalised name shorter than this is too easy to match by accident ("ABC" ~ "ABS").
MIN_FUZZY_CHARS = 6
TOP_K = 3

#: Leftovers of matter titles in issuer_guess: "Audit Report of X", "SCORES against X", "A against B".
DESCRIPTOR = re.compile(r"^(?:.*\bagainst\s+|(?:audit\s+report|inspection(?:\s+of\s+books)?|investigation|"
                        r"examination)\s+(?:of|in(?:to)?)\s+(?:the\s+)?(?:affairs\s+of\s+)?)", re.I)
PRIVATE = re.compile(r"\b(?:PVT|PRIVATE|LLP)\b|\(P\)\s*(?:LTD|LIMITED)", re.I)
ISIN_IN_TEXT = re.compile(r"\bIN[E9][A-Z0-9]{4}[A-Z0-9]{2}[A-Z0-9]{2}\d\b")
SCRIP_IN_TEXT = re.compile(
    r"(?:scrip|security|stock)\s*(?:code|id)\s*(?:no\.?|number)?\s*[:\-–(]?\s*(?:BSE\s*[:\-–]?\s*)?(5\d{5})\b"
    r"|\bBSE\s*(?:scrip\s*)?(?:code|id)?\s*[:\-–]\s*(5\d{5})\b", re.I)
REVIEW_COLUMNS = ["query", "query_norm", "n_orders", "n_parties", "best_score", "candidates", "example_title",
                  "decision", "note"]


# --- names → firms ---------------------------------------------------------------------------------------------

def alias_index(aliases: pd.DataFrame) -> tuple[list[str], list[list[str]]]:
    """Distinct normalised aliases and the firms each belongs to."""
    g = aliases.groupby("alias_norm").firm_id.agg(lambda s: sorted(set(s)))
    return g.index.tolist(), g.tolist()


def generic_words(keys: list[str], min_df: int = GENERIC_DF) -> frozenset[str]:
    df = Counter(t for k in keys for t in set(k.split()))
    return frozenset(t for t, c in df.items() if c >= min_df and len(t) > 1)


def core(name: str, generic: frozenset[str]) -> str:
    """The distinctive words of a normalised name (all of it if every word is generic)."""
    kept = [t for t in name.split() if t not in generic]
    return " ".join(kept) if kept else name


def _prefer_isin(firms: list[str]) -> list[str]:
    """Among firms sharing a name, those identified by an ISIN (a no-ISIN row is often the same company)."""
    with_isin = [f for f in firms if ":" not in f]
    return with_isin or firms


def match_names(queries: list[str], aliases: pd.DataFrame, chunk: int = 2000) -> pd.DataFrame:
    """Best firm for each distinct normalised name: score, band and the runner-up firms."""
    keys, firms_of = alias_index(aliases)
    exact = dict(zip(keys, firms_of))
    generic = generic_words(keys)
    cores = [core(k, generic) for k in keys]
    rows = []
    for start in range(0, len(queries), chunk):
        part = queries[start:start + chunk]
        scores = process.cdist(part, keys, scorer=fuzz.token_sort_ratio, score_cutoff=REVIEW_MIN - 10,
                               dtype=np.uint8, workers=-1)
        for q, row in zip(part, scores):
            rows.append(_best(q, row, keys, firms_of, exact, cores, core(q, generic)))
    return pd.DataFrame(rows)


def query_names(name) -> list[str]:
    """Normalised names to try for an issuer: as written, then without a leading descriptor."""
    out = names(name)
    if isinstance(name, str) and DESCRIPTOR.match(name):
        out += [n for n in names(DESCRIPTOR.sub("", name, count=1)) if n not in out]
    return out


def _best(q: str, row: np.ndarray, keys: list[str], firms_of: list[list[str]], exact: dict,
          cores: list[str] | None = None, q_core: str | None = None) -> dict:
    top = np.argsort(row)[::-1][:50]
    q_core = q_core or q
    by_firm: dict[str, tuple[int, str, str]] = {}   # firm → (score, alias, its distinctive words)
    for i in top:
        if row[i] == 0:
            break
        a_core = cores[i] if cores is not None else keys[i]
        if cores is not None and row[i] < 100 and fuzz.token_set_ratio(q_core, a_core) < CORE_MIN:
            continue  # shares only generic words
        for f in firms_of[i]:
            if f not in by_firm or row[i] > by_firm[f][0]:
                by_firm[f] = (int(row[i]), keys[i], a_core)
    if q in exact:
        for f in exact[q]:
            by_firm[f] = (100, q, q_core)
    ranked = sorted(by_firm.items(), key=lambda kv: (-kv[1][0], ":" in kv[0], kv[0]))
    cands = [{"firm_id": f, "score": s, "alias": a} for f, (s, a, _) in ranked[:TOP_K]]
    best_core = ranked[0][1][2] if ranked else ""
    out = {"query_norm": q, "candidates": json.dumps(cands), "firm_id": None, "score": 0, "alias": None,
           "band": "reject", "reason": None}
    if not cands:
        return out
    best = cands[0]
    out.update(firm_id=best["firm_id"], score=best["score"], alias=best["alias"])
    if best["score"] == 100:
        tied = _prefer_isin([c["firm_id"] for c in cands if c["score"] == 100])
        if len(tied) > 1:
            out.update(band="review", reason="same name, several firms")
        else:
            out.update(firm_id=tied[0], band="auto", reason="exact")
    elif best["score"] >= AUTO:
        rivals = [c for c in cands[1:] if c["score"] >= AUTO]
        if len(q) < MIN_FUZZY_CHARS:
            out.update(band="review", reason="short name")
        elif fuzz.ratio(q_core.split()[0], best_core.split()[0]) < CORE_MIN:
            out.update(band="review", reason="first word differs")
        elif rivals:
            out.update(band="review", reason="two firms ≥ 92")
        else:
            out.update(band="auto", reason="fuzzy")
    elif best["score"] >= REVIEW_MIN:
        out.update(band="review", reason="fuzzy 80–92")
    else:
        out.update(firm_id=None, band="reject")
    return out


# --- codes in the text -----------------------------------------------------------------------------------------

def codes_in_text(text: str) -> tuple[list[str], list[str]]:
    isins = sorted(set(ISIN_IN_TEXT.findall(text)))
    scrips = sorted({a or b for a, b in SCRIP_IN_TEXT.findall(text)})
    return isins, scrips


def code_lookup(universe: pd.DataFrame) -> tuple[set[str], dict[str, str]]:
    b = universe[universe.exchange == "BSE"]
    return set(universe.firm_id), dict(zip(b.code, b.firm_id))


def code_firms(isins: list[str], scrips: list[str], lookup: tuple[set[str], dict[str, str]]) -> list[str]:
    """Firms identified by the codes an order prints (ISIN issuer prefix, BSE scrip code)."""
    known, bse = lookup
    out = []
    for i in isins:
        f = "INE" + i[3:7]
        if f in known and f not in out:
            out.append(f)
    for c in scrips:
        f = bse.get(c)
        if f and f not in out:
            out.append(f)
    return out


# --- review sheet ----------------------------------------------------------------------------------------------

def read_review(path: pathlib.Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=REVIEW_COLUMNS)
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def decisions(review: pd.DataFrame, matches: pd.DataFrame) -> dict[str, str | None]:
    """query_norm → firm_id (or None for reject), from filled review rows."""
    cands = dict(zip(matches.query_norm, matches.candidates))
    out = {}
    for r in review.itertuples():
        d = r.decision.strip()
        if not d:
            continue
        if d.lower() == "reject":
            out[r.query_norm] = None
        elif d.lower() == "accept":
            c = json.loads(cands.get(r.query_norm, "[]"))
            out[r.query_norm] = c[0]["firm_id"] if c else None
        else:
            out[r.query_norm] = d
    return out


def new_review_rows(matches: pd.DataFrame, usage: pd.DataFrame, review: pd.DataFrame,
                    firm_names: dict[str, str], firm_status: dict[str, str]) -> pd.DataFrame:
    """Review-band names not yet on the sheet, most used first."""
    todo = matches[(matches.band == "review") & ~matches.query_norm.isin(set(review.query_norm))]
    todo = todo.merge(usage, on="query_norm", how="left").sort_values(["n_orders", "n_parties"], ascending=False)
    rows = []
    for r in todo.itertuples():
        cands = "; ".join(f"{c['firm_id']} {firm_names.get(c['firm_id'], '?')} [{firm_status.get(c['firm_id'], '?')}] "
                          f"{c['score']}" for c in json.loads(r.candidates))
        rows.append({"query": r.query, "query_norm": r.query_norm, "n_orders": int(r.n_orders),
                     "n_parties": int(r.n_parties), "best_score": int(r.score), "candidates": cands,
                     "example_title": r.example_title, "decision": "", "note": r.reason or ""})
    return pd.DataFrame(rows, columns=REVIEW_COLUMNS)


# --- applying matches ------------------------------------------------------------------------------------------

def resolve(query_names: list[str], by_norm: dict, hand: dict) -> dict:
    """The best match over a name and its bracketed former names."""
    best = {"firm_id": None, "score": 0, "method": "none", "alias": None, "query_norm": None}
    for q in query_names:
        if q in hand:
            if hand[q]:
                return {"firm_id": hand[q], "score": 100, "method": "hand", "alias": None, "query_norm": q}
            best = {**best, "method": "hand_reject", "query_norm": q}
            continue
        m = by_norm.get(q)
        if m is None:
            continue
        method = {"auto": "exact" if m["reason"] == "exact" else "auto", "review": "review_pending"}.get(m["band"], "none")
        rank = {"exact": 3, "auto": 2, "review_pending": 1}.get(method, 0)
        if rank > {"exact": 3, "auto": 2, "review_pending": 1}.get(best["method"], 0):
            best = {"firm_id": m["firm_id"] if method != "review_pending" else None, "score": int(m["score"]),
                    "method": method, "alias": m["alias"], "query_norm": q,
                    "candidate_firm_id": m["firm_id"] if method == "review_pending" else None}
    return best


def firm_codes(universe: pd.DataFrame) -> pd.DataFrame:
    """Per firm: the ISIN, NSE symbol and BSE code to carry (live listings first)."""
    rank = universe.status.map({"Active": 0, "Suspended": 1, "Delisted": 2}).fillna(3)
    u = universe.assign(_rank=rank).sort_values(["firm_id", "_rank", "code"])
    nse = u[u.exchange == "NSE"].groupby("firm_id").agg(nse_symbol=("code", "first"), nse_isin=("isin", "first"))
    bse = u[u.exchange == "BSE"].groupby("firm_id").agg(bse_code=("code", "first"), bse_isin=("isin", "first"),
                                                        bse_status=("status", "first"))
    out = nse.join(bse, how="outer")
    out["isin"] = out.nse_isin.fillna(out.bse_isin)
    out["ticker"] = out.nse_symbol.fillna(out.bse_code)
    return out[["isin", "ticker", "nse_symbol", "bse_code", "bse_status"]]


def match_orders(orders: pd.DataFrame, entities: pd.DataFrame, universe: pd.DataFrame, aliases: pd.DataFrame,
                 review: pd.DataFrame, text_dir_root: pathlib.Path = REPO_ROOT):
    issuers = orders[["text_stem", "issuer_guess", "title", "text_path"]].copy()
    companies = entities[entities.entity_type == "company"][["text_stem", "party_no", "entity_name",
                                                             "entity_name_clean", "entity_role"]].copy()
    issuers["private"] = issuers.issuer_guess.fillna("").str.contains(PRIVATE)
    companies["private"] = companies.entity_name.fillna("").str.contains(PRIVATE)
    issuers["qnames"] = [[] if p else query_names(n) for n, p in zip(issuers.issuer_guess, issuers.private)]
    companies["qnames"] = [[] if p else list(dict.fromkeys(names(a) + names(b)))
                           for a, b, p in zip(companies.entity_name_clean, companies.entity_name, companies.private)]

    distinct = sorted({q for qs in [*issuers.qnames, *companies.qnames] for q in qs})
    matches = match_names(distinct, aliases)
    by_norm = {r["query_norm"]: r for r in matches.to_dict("records")}
    hand = decisions(review, matches)

    codes = firm_codes(universe)
    lookup = code_lookup(universe)
    out_i = []
    for r in issuers.itertuples():
        isins, scrips = ([], [])
        if isinstance(r.text_path, str) and (text_dir_root / r.text_path).exists():
            isins, scrips = codes_in_text((text_dir_root / r.text_path).read_text(errors="ignore"))
        by_code = code_firms(isins, scrips, lookup)
        m = resolve(r.qnames, by_norm, hand)
        m.setdefault("candidate_firm_id", None)
        if by_code:
            agree = [f for f in by_code if f == m["firm_id"] or f == m.get("candidate_firm_id")]
            if agree:
                m = {**m, "firm_id": agree[0], "score": 100, "method": "code"}
            elif not r.qnames and not r.private and len(by_code) == 1 and not isinstance(r.issuer_guess, str):
                m = {**m, "firm_id": by_code[0], "score": 100, "method": "code"}
        if r.private:
            m = {**m, "method": "private"}
        out_i.append({"text_stem": r.text_stem, "issuer_name": r.issuer_guess, "issuer_firm_id": m["firm_id"],
                      "match_method": m["method"], "match_confidence": m["score"] if m["firm_id"] else None,
                      "match_alias": m["alias"], "match_query": m["query_norm"],
                      "candidate_firm_id": m.get("candidate_firm_id"),
                      "codes_found": json.dumps(isins + scrips) if isins or scrips else None,
                      "code_firms": json.dumps(by_code) if by_code else None})
    order_issuers = pd.DataFrame(out_i).merge(codes, left_on="issuer_firm_id", right_index=True, how="left")
    order_issuers = order_issuers.rename(columns={c: f"issuer_{c}" for c in codes.columns})
    order_issuers["rules_version"] = RULES_VERSION

    out_e = []
    for r in companies.itertuples():
        m = resolve(r.qnames, by_norm, hand)
        if r.private:
            m = {**m, "firm_id": None, "method": "private"}
        out_e.append({"text_stem": r.text_stem, "party_no": r.party_no, "entity_name": r.entity_name,
                      "entity_role": r.entity_role, "firm_id": m["firm_id"], "match_method": m["method"],
                      "match_confidence": m["score"] if m["firm_id"] else None, "match_alias": m["alias"],
                      "match_query": m["query_norm"], "candidate_firm_id": m.get("candidate_firm_id")})
    entity_matches = pd.DataFrame(out_e, columns=["text_stem", "party_no", "entity_name", "entity_role", "firm_id",
                                                  "match_method", "match_confidence", "match_alias", "match_query",
                                                  "candidate_firm_id"])
    entity_matches = entity_matches.merge(codes, left_on="firm_id", right_index=True, how="left")
    entity_matches["rules_version"] = RULES_VERSION

    usage = _usage(issuers, companies)
    return order_issuers, entity_matches, matches, usage


def _usage(issuers: pd.DataFrame, companies: pd.DataFrame) -> pd.DataFrame:
    i = issuers.explode("qnames").dropna(subset=["qnames"])
    c = companies.explode("qnames").dropna(subset=["qnames"])
    n_o = i.groupby("qnames").text_stem.nunique().rename("n_orders")
    n_p = c.groupby("qnames").size().rename("n_parties")
    ex_q = pd.concat([i.groupby("qnames").issuer_guess.first(), c.groupby("qnames").entity_name.first()]) \
        .groupby(level=0).first().rename("query")
    ex_t = i.groupby("qnames").title.first().rename("example_title")
    u = pd.concat([n_o, n_p, ex_q, ex_t], axis=1).fillna({"n_orders": 0, "n_parties": 0})
    return u.rename_axis("query_norm").reset_index()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.match_entities",
                                     description="Match order issuers and company parties to the listed universe")
    parser.add_argument("--derived", type=pathlib.Path, default=DERIVED_DIR,
                        help="where orders, order_entities, universe and name_aliases are read")
    parser.add_argument("--scratch", type=pathlib.Path, help="write outputs (and the review sheet) under DIR")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_dir = args.scratch or DERIVED_DIR
    review_path = (args.scratch / "match_review.csv") if args.scratch else REVIEW
    outs = {k: out_dir / f"{k}.parquet" for k in ("order_issuers", "entity_matches", "name_matches")}
    for p in [*outs.values(), review_path]:
        assert_safe_output(p)
    setup_logging(args.log_level, None if args.scratch else LOG_FILE)

    d = args.derived
    orders = pd.read_parquet(DERIVED_DIR / "orders.parquet",
                             columns=["text_stem", "issuer_guess", "title", "text_path", "violation_start"])
    entities = pd.read_parquet(DERIVED_DIR / "order_entities.parquet")
    universe = pd.read_parquet(d / "universe.parquet")
    aliases = pd.read_parquet(d / "name_aliases.parquet")
    firm_table = pd.read_parquet(d / "firms.parquet")
    review = read_review(review_path)

    order_issuers, entity_matches, matches, usage = match_orders(orders, entities, universe, aliases, review)
    new = new_review_rows(matches, usage, review, dict(zip(firm_table.firm_id, firm_table["name"])),
                          dict(zip(firm_table.firm_id, firm_table.status)))
    out_dir.mkdir(parents=True, exist_ok=True)
    order_issuers.to_parquet(outs["order_issuers"], index=False)
    entity_matches.to_parquet(outs["entity_matches"], index=False)
    matches.merge(usage, on="query_norm", how="left").to_parquet(outs["name_matches"], index=False)
    if len(new):
        review_path.parent.mkdir(parents=True, exist_ok=True)
        pd.concat([review, new], ignore_index=True).to_csv(review_path, index=False)

    windowed = orders.violation_start.notna().to_numpy()
    oi = order_issuers
    log.info("distinct names     %d: %s", len(matches), matches.band.value_counts().to_dict())
    log.info("order issuers      %d named; method %s", oi.issuer_name.notna().sum(), oi.match_method.value_counts().to_dict())
    log.info("orders matched     %d (%.1f%%); windowed orders matched %d of %d", oi.issuer_firm_id.notna().sum(),
             100 * oi.issuer_firm_id.notna().mean(), oi[windowed].issuer_firm_id.notna().sum(), windowed.sum())
    log.info("distinct issuer firms %d (windowed orders: %d)", oi.issuer_firm_id.nunique(),
             oi[windowed].issuer_firm_id.nunique())
    log.info("company parties    %d; method %s", len(entity_matches), entity_matches.match_method.value_counts().to_dict())
    log.info("review sheet       %d new names appended (%d rows; %d decided) → %s", len(new), len(review) + len(new),
             (review.decision.str.strip() != "").sum() if len(review) else 0, review_path)
    log.info("wrote %s", ", ".join(map(str, outs.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
