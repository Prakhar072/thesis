"""Week 4 Day 1: the listed universe — every BSE and NSE equity, live, suspended or delisted.

Sources (all through ``src/cache.py``; each is a snapshot, dated by its cache file):

* BSE list of scrips (``api.bseindia.com … ListofScripData``), status Active /
  Delisted / Suspended: scrip code, short and full issuer name, ISIN, group,
  face value. Delisted and suspended scrips matter most: fraud firms are often
  among them.
* NSE equity list (``EQUITY_L.csv``) and SME list (``SME_EQUITY_L.csv``):
  symbol, name, series, listing date, ISIN. Current listings only.
* NSE name changes (``namechange.csv``, since 1996) and symbol changes
  (``symbolchange.csv``): the alias table for renamed companies (runbook: "the
  name-change problem").

Writes

* ``derived/universe.parquet`` — one row per listed security (a BSE scrip or an NSE symbol);
* ``derived/firms.parquet`` — one row per firm;
* ``derived/name_aliases.parquet`` — every name a firm is known by, normalised
  (``src/names.py``), with the dates a former name was in use where known.

``firm_id``: the ISIN's first seven characters (``INE117A``) identify the
issuer. The serial after them changes with a face-value split (INE117A01014 →
INE117A01022), so the prefix joins the two, and a partly paid ISIN (IN9117A…)
belongs to the same issuer. A security with no ISIN (old BSE delistings) is its
own firm, ``BSE:<code>``. Mutual-fund and other non-equity ISINs are dropped.

Limit: name-change history is NSE's. A company listed only on BSE and renamed
is known by its current (or last) name only.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import pathlib
import re

import pandas as pd

from src import cache
from src.extract_text import assert_safe_output, setup_logging
from src.names import norm

log = logging.getLogger("universe")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
LOG_FILE = REPO_ROOT / "logs" / "universe.log"

BSE_LIST = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
BSE_STATUSES = ("Active", "Delisted", "Suspended")
BSE_HEADERS = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
               "Accept": "application/json, text/plain, */*"}
NSE_EQUITY = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_SME = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
NSE_NAMECHANGE = "https://nsearchives.nseindia.com/content/equities/namechange.csv"
NSE_SYMBOLCHANGE = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"

ISIN_RE = re.compile(r"^IN[E9][A-Z0-9]{4}[A-Z0-9]{2}[A-Z0-9]{2}\d$")
STATUS_RANK = {"Active": 0, "Suspended": 1, "Delisted": 2}
#: Fund schemes and other non-company rows in NSE's change files.
FUND_RE = re.compile(r"\bMF\b|MUTUAL FUND|\bFTF\b|\bETF\b|\bPLAN\b|\bSCHEME\b|\bFUND\b|\bSERIES\b", re.I)


def bse_params(status: str) -> dict:
    return {"Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": status}


def firm_id(isin, fallback: str) -> str:
    """The issuer part of an ISIN (IN9 → INE), else the fallback id."""
    if isinstance(isin, str) and ISIN_RE.match(isin.strip().upper()):
        isin = isin.strip().upper()
        return "INE" + isin[3:7]
    return fallback


def clean_isin(isin) -> str | None:
    if not isinstance(isin, str):
        return None
    isin = isin.strip().upper()
    return isin if ISIN_RE.match(isin) else None


def _dates(s: pd.Series) -> pd.Series:
    """NSE writes 06-OCT-2008 in one file and 12-Oct-26 in another."""
    s = s.astype(str).str.strip()
    out = pd.to_datetime(s, format="%d-%b-%Y", errors="coerce")
    return out.fillna(pd.to_datetime(s, format="%d-%b-%y", errors="coerce")).dt.date


def _csv(content: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
    df.columns = [re.sub(r"[\s_]+", "_", c.strip().upper()) for c in df.columns]
    df = df.loc[:, [c for c in df.columns if c]]
    return df.apply(lambda c: c.str.strip())


# --- parsing --------------------------------------------------------------------------------------------------

def parse_bse(lists: dict[str, bytes]) -> pd.DataFrame:
    rows = []
    for status, content in lists.items():
        for r in json.loads(content):
            isin = clean_isin(r.get("ISIN_NUMBER"))
            if isinstance(r.get("ISIN_NUMBER"), str) and r["ISIN_NUMBER"].strip().upper().startswith("INF"):
                continue  # a fund scheme
            code = str(r["SCRIP_CD"]).strip()
            rows.append({"exchange": "BSE", "code": code, "symbol": (r.get("scrip_id") or "").strip() or None,
                         "isin": isin, "firm_id": firm_id(isin, f"BSE:{code}"),
                         "name": (r.get("Issuer_Name") or r.get("Scrip_Name") or "").strip(),
                         "short_name": (r.get("Scrip_Name") or "").strip() or None,
                         "status": r.get("Status") or status, "group": (r.get("GROUP") or "").strip() or None,
                         "series": None, "face_value": pd.to_numeric(r.get("FACE_VALUE"), errors="coerce"),
                         "listing_date": None, "source": f"bse_{status.lower()}"})
    return pd.DataFrame(rows)


def parse_nse(content: bytes, source: str) -> pd.DataFrame:
    d = _csv(content)
    return pd.DataFrame({
        "exchange": "NSE", "code": d.SYMBOL, "symbol": d.SYMBOL, "isin": d.ISIN_NUMBER.map(clean_isin),
        "firm_id": [firm_id(i, f"NSE:{s}") for i, s in zip(d.ISIN_NUMBER, d.SYMBOL)],
        "name": d.NAME_OF_COMPANY, "short_name": None, "status": "Active", "group": None,
        "series": d.SERIES, "face_value": pd.to_numeric(d.FACE_VALUE, errors="coerce"),
        "listing_date": _dates(d.DATE_OF_LISTING), "source": source})


def symbol_chain(symbolchange: pd.DataFrame) -> dict[str, str]:
    """Old NSE symbol → its latest symbol, following chains (A → B → C)."""
    step = dict(zip(symbolchange.OLD_SYMBOL, symbolchange.NEW_SYMBOL))
    out = {}
    for old in step:
        seen, cur = {old}, step[old]
        while cur in step and step[cur] not in seen:
            seen.add(cur)
            cur = step[cur]
        out[old] = cur
    return out


def name_changes(namechange: pd.DataFrame, symbolchange: pd.DataFrame, universe: pd.DataFrame,
                 aliases: pd.DataFrame) -> pd.DataFrame:
    """Old and new names from NSE's change file, placed on a firm.

    The symbol is resolved to today's NSE list (directly or through symbol
    changes); failing that, the new or old name must equal (normalised) a name
    already in the universe, and only one firm's."""
    nse_firm = dict(zip(universe[universe.exchange == "NSE"].symbol, universe[universe.exchange == "NSE"].firm_id))
    chain = symbol_chain(symbolchange)
    by_name = aliases.groupby("alias_norm").firm_id.agg(lambda s: set(s))
    rows = []
    for r in namechange.itertuples():
        old, new = r.OLD_NAME_OF_THE_COMPANY, r.NEW_NAME_OF_THE_COMPANY
        if FUND_RE.search(old) or FUND_RE.search(new):
            continue
        when = _dates(pd.Series([r.EFFECTIVE_DATE])).iloc[0]
        fid, how = nse_firm.get(r.SYMBOL), "symbol"
        if fid is None and r.SYMBOL in chain:
            fid, how = nse_firm.get(chain[r.SYMBOL]), "symbol_change"
        if fid is None:
            for n in (norm(new), norm(old)):
                hits = by_name.get(n, set())
                if len(hits) == 1:
                    fid, how = next(iter(hits)), "name"
                    break
        rows.append({"firm_id": fid, "symbol": r.SYMBOL, "old_name": old, "new_name": new,
                     "effective": when, "resolved_by": how if fid else None})
    return pd.DataFrame(rows, columns=["firm_id", "symbol", "old_name", "new_name", "effective", "resolved_by"])


# --- building -------------------------------------------------------------------------------------------------

def build(bse_lists: dict[str, bytes], nse: bytes, sme: bytes, namechange: bytes, symbolchange: bytes):
    universe = pd.concat([parse_bse(bse_lists), parse_nse(nse, "nse_equity"), parse_nse(sme, "nse_sme")],
                         ignore_index=True)
    universe["name_norm"] = universe["name"].map(norm)

    current = pd.concat([
        universe[["firm_id", "name"]].assign(source=universe.source.str.replace(r"_\w+$", "", regex=True) + "_name"),
        universe[universe.short_name.notna()][["firm_id", "short_name"]].rename(columns={"short_name": "name"})
        .assign(source="bse_short_name"),
    ], ignore_index=True)
    current = current.assign(alias=current["name"], alias_norm=current["name"].map(norm), valid_from=None, valid_to=None)
    current = current[current.alias_norm != ""]

    changes = name_changes(_csv(namechange), _csv(symbolchange), universe, current)
    resolved = changes[changes.firm_id.notna()]
    history = pd.concat([
        resolved.assign(alias=resolved.old_name, source="nse_old_name", valid_from=None, valid_to=resolved.effective),
        resolved.assign(alias=resolved.new_name, source="nse_new_name", valid_from=resolved.effective, valid_to=None),
    ], ignore_index=True)
    history["alias_norm"] = history.alias.map(norm)
    aliases = (pd.concat([current[["firm_id", "alias", "alias_norm", "source", "valid_from", "valid_to"]],
                          history[["firm_id", "alias", "alias_norm", "source", "valid_from", "valid_to"]]],
                         ignore_index=True)
               .drop_duplicates(["firm_id", "alias_norm", "source"]).reset_index(drop=True))
    aliases = aliases[aliases.alias_norm != ""].reset_index(drop=True)
    return universe, firms(universe), aliases, changes


def firms(universe: pd.DataFrame) -> pd.DataFrame:
    """One row per firm: its best name (NSE's if listed there, else BSE's full name) and its listings."""
    rows = []
    for fid, g in universe.groupby("firm_id", sort=True):
        nse, bse = g[g.exchange == "NSE"], g[g.exchange == "BSE"]
        status = min(g.status, key=lambda s: STATUS_RANK.get(s, 3))
        name = (nse["name"].iloc[0] if len(nse) else
                bse.sort_values("status", key=lambda s: s.map(STATUS_RANK))["name"].iloc[0])
        rows.append({"firm_id": fid, "name": name, "name_norm": norm(name), "status": status,
                     "isins": json.dumps(sorted(set(g["isin"].dropna()))),
                     "bse_codes": json.dumps(sorted(set(bse.code))), "nse_symbols": json.dumps(sorted(set(nse.code))),
                     "on_bse": len(bse) > 0, "on_nse": len(nse) > 0,
                     "bse_status": min(bse.status, key=lambda s: STATUS_RANK.get(s, 3)) if len(bse) else None,
                     "nse_listing_date": nse.listing_date.dropna().min() if len(nse) else None})
    return pd.DataFrame(rows)


def fetch_all(offline: bool = False) -> dict:
    """The six snapshots, from the cache (or the network the first time: six requests)."""
    get = lambda url, **kw: cache.get(url, offline=offline, retries=2, **kw)  # noqa: E731
    return {
        "bse_lists": {s: get(BSE_LIST, params=bse_params(s), headers=BSE_HEADERS) for s in BSE_STATUSES},
        "nse": get(NSE_EQUITY), "sme": get(NSE_SME),
        "namechange": get(NSE_NAMECHANGE), "symbolchange": get(NSE_SYMBOLCHANGE),
    }


def snapshot_dates() -> dict:
    """When each source was fetched (its cache file's mtime)."""
    out = {f"bse_{s.lower()}": cache.cache_path(BSE_LIST, params=bse_params(s)) for s in BSE_STATUSES}
    out |= {"nse_equity": cache.cache_path(NSE_EQUITY), "nse_sme": cache.cache_path(NSE_SME),
            "nse_namechange": cache.cache_path(NSE_NAMECHANGE), "nse_symbolchange": cache.cache_path(NSE_SYMBOLCHANGE)}
    return {k: pd.Timestamp(p.stat().st_mtime, unit="s").strftime("%Y-%m-%d") if p.exists() else None
            for k, p in out.items()}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.universe",
                                     description="Build the listed universe and name aliases (Week 4 Day 1)")
    parser.add_argument("--offline", action="store_true", help="cache only: fail rather than fetch")
    parser.add_argument("--scratch", type=pathlib.Path, help="write outputs under DIR instead of derived/")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_dir = args.scratch or DERIVED_DIR
    outs = {k: out_dir / f"{k}.parquet" for k in ("universe", "firms", "name_aliases")}
    for p in outs.values():
        assert_safe_output(p)
    setup_logging(args.log_level, None if args.scratch else LOG_FILE)
    cache.reset_stats()
    universe, firm_table, aliases, changes = build(**fetch_all(args.offline))
    out_dir.mkdir(parents=True, exist_ok=True)
    universe.to_parquet(outs["universe"], index=False)
    firm_table.to_parquet(outs["firms"], index=False)
    aliases.to_parquet(outs["name_aliases"], index=False)

    log.info("snapshots          %s", snapshot_dates())
    log.info("securities         %d (%s)", len(universe), universe.source.value_counts().to_dict())
    log.info("firms              %d; on BSE %d, on NSE %d, both %d; status %s", len(firm_table),
             firm_table.on_bse.sum(), firm_table.on_nse.sum(), (firm_table.on_bse & firm_table.on_nse).sum(),
             firm_table.status.value_counts().to_dict())
    log.info("firms without ISIN %d", firm_table.firm_id.str.contains(":").sum())
    log.info("name changes       %d company rows; placed on a firm %d (%s); unplaced %d", len(changes),
             changes.firm_id.notna().sum(), changes.resolved_by.value_counts().to_dict(), changes.firm_id.isna().sum())
    log.info("aliases            %d (%s)", len(aliases), aliases.source.value_counts().to_dict())
    log.info("network_calls=%d; wrote %s", cache.NETWORK_CALLS, ", ".join(map(str, outs.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
