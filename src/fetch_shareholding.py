"""Week 4 Day 5: ownership — promoter holding and promoter pledging from BSE shareholding patterns.

Source: BSE's summary statement of each quarterly shareholding pattern
(``api.bseindia.com … shpSecSummery_New``), one request per (scrip, quarter), through
``src/cache.py``. The quarter id steps by one per quarter (60 = Dec 2008, 120 = Dec 2023,
130 = Jun 2026). The summary exists in SEBI's current format from **December 2015**;
earlier quarters come back empty, so ownership before FY2016 is missing (a stated
limitation; BSE's older pages redirect and no other free source was found within the
runbook's timebox).

Parsed from each summary (columns are read by their header, since the table drops
columns that do not apply to a company):

* ``promoter_pct`` — the promoter and promoter group's share of total shares (SCRR basis);
* ``promoter_shares``, ``total_shares`` (total shares also give market capitalisation);
* ``pledged_shares``, ``pledged_pct`` (of the promoter holding) — from the "shares pledged
  or otherwise encumbered" columns; zero when the company answers "No" to "Whether any
  shares held by promoters are pledge or otherwise encumbered?"; missing when it answers
  "Yes" and the summary has no pledge column;
* ``pledge_answer`` — that Yes/No answer.

Requests: by default the financial-year-end quarter only (March), matching the firm-year
panel: about 11 per firm. ``--quarterly`` asks for every quarter (four times as many).
Once Day 4's prices exist, a firm is asked only for quarters inside its BSE trading span
(first to last trade, plus a quarter), and not at all if it has not traded since 2015;
without prices, a delisted firm stops after two empty quarters (at the start or after data).

Writes ``derived/shareholding.parquet`` (one row per firm and quarter asked, with
``status`` ∈ ok, empty, unparsed, error).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import re
import time

import pandas as pd
from bs4 import BeautifulSoup

from src import cache
from src.extract_text import assert_safe_output, setup_logging

log = logging.getLogger("fetch_shareholding")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
LOG_FILE = REPO_ROOT / "logs" / "fetch_shareholding.log"
URL = "https://api.bseindia.com/BseIndiaAPI/api/shpSecSummery_New/w"
HEADERS = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
           "Accept": "application/json, text/plain, */*"}
FIRST_QUARTER = dt.date(2015, 12, 31)
QUARTER_RE = re.compile(r"Quarter ending\s*:\s*([A-Za-z]+)\s+(\d{4})")
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"], start=1)}


def qtrid(quarter_end: dt.date) -> str:
    """BSE's quarter id: 60 for December 2008, one more per quarter."""
    return f"{60 + 4 * (quarter_end.year - 2008) + quarter_end.month // 3 - 4}.00"


def quarter_ends(start: dt.date, end: dt.date, quarterly: bool) -> list[dt.date]:
    out = []
    for y in range(start.year, end.year + 1):
        for m, last in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q = dt.date(y, m, last)
            if start <= q <= end and (quarterly or m == 3):
                out.append(q)
    return out


def _num(text: str) -> float | None:
    t = (text or "").replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _expanded_header(row) -> list[str]:
    out = []
    for cell in row.find_all(["td", "th"], recursive=False):
        out += [cell.get_text(" ", strip=True)] * int(cell.get("colspan") or 1)
    return out


def _col(header: list[str], *words: str) -> int | None:
    for i, h in enumerate(header):
        low = h.lower()
        if all(w in low for w in words):
            return i
    return None


def parse_summary(content: bytes) -> dict:
    """One summary → promoter holding, pledges, total shares; ``status`` empty when the quarter has no table."""
    data = json.loads(content).get("Data") or ""
    soup = BeautifulSoup(data, "lxml")
    text = soup.get_text(" ", strip=True)
    out = {"quarter_label": None, "status": "empty", "promoter_pct": None, "promoter_shares": None,
           "total_shares": None, "pledged_shares": None, "pledged_pct": None, "pledge_answer": None,
           "promoter_holders": None}
    if m := QUARTER_RE.search(text):
        out["quarter_label"] = f"{m.group(1)} {m.group(2)}"
    rows = soup.find_all("tr")
    header = promoter = total = None
    for tr in rows:
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        first = cells[0].get_text(" ", strip=True)
        if first == "Category of shareholder" and header is None:
            header = _expanded_header(tr)
        elif first.startswith("(A) Promoter") and promoter is None:
            promoter = [c.get_text(" ", strip=True) for c in cells]
        elif first.startswith("Grand Total") and total is None:
            total = [c.get_text(" ", strip=True) for c in cells]
        elif "pledge or otherwise encumbered" in first.lower() and len(cells) > 1:
            out["pledge_answer"] = cells[1].get_text(" ", strip=True)
    if header is None or promoter is None:
        return out
    shares, pct = _col(header, "total no. shares held"), _col(header, "% of total no. of shares")
    holders, pledged = _col(header, "no. of shareholders"), _col(header, "pledged")
    get = lambda row, i: _num(row[i]) if row is not None and i is not None and i < len(row) else None  # noqa: E731
    out.update(status="ok", promoter_shares=get(promoter, shares), promoter_pct=get(promoter, pct),
               total_shares=get(total, shares), promoter_holders=get(promoter, holders))
    if pledged is not None:
        out["pledged_shares"] = get(promoter, pledged) or 0.0
        out["pledged_pct"] = get(promoter, pledged + 1) or 0.0
    elif (out["pledge_answer"] or "").strip().lower() == "no":
        out["pledged_shares"], out["pledged_pct"] = 0.0, 0.0
    if out["promoter_pct"] is None:
        out["status"] = "unparsed"
    return out


def firm_codes(sample: pd.DataFrame) -> pd.DataFrame:
    """(firm_id, role, status, bse_code) for sample firms listed on BSE; the issuer's own code first."""
    rows = []
    for r in sample.itertuples():
        codes = json.loads(r.bse_codes) if isinstance(r.bse_codes, str) else []
        code = r.issuer_ticker if isinstance(getattr(r, "issuer_ticker", None), str) and \
            str(r.issuer_ticker).isdigit() and r.issuer_ticker in codes else (codes[0] if codes else None)
        rows.append({"firm_id": r.firm_id, "role": r.role, "status": r.status, "bse_code": code})
    return pd.DataFrame(rows)


def trading_spans(prices_dir: pathlib.Path, since: int) -> dict[str, tuple[dt.date, dt.date]]:
    """BSE scrip code → (first, last) trading day in the price files from ``since`` on."""
    spans: dict[str, list[dt.date]] = {}
    for p in sorted(prices_dir.glob("BSE_*.parquet")):
        if int(p.stem.split("_")[1]) < since:
            continue
        d = pd.read_parquet(p, columns=["code", "date"]).groupby("code").date.agg(["min", "max"])
        for code, lo, hi in zip(d.index, d["min"], d["max"]):
            cur = spans.setdefault(code, [lo, hi])
            cur[0], cur[1] = min(cur[0], lo), max(cur[1], hi)
    return {k: (v[0], v[1]) for k, v in spans.items()}


def in_span(quarters: list[dt.date], span: tuple[dt.date, dt.date] | None) -> list[dt.date]:
    if span is None:
        return []
    lo, hi = span
    return [q for q in quarters if lo - dt.timedelta(days=92) <= q <= hi + dt.timedelta(days=92)]


def fetch_firm(firm: dict, quarters: list[dt.date], offline: bool) -> list[dict]:
    out, seen_data, empties_after = [], False, 0
    for q in quarters:
        row = {"firm_id": firm["firm_id"], "bse_code": firm["bse_code"], "quarter_end": q, "qtrid": qtrid(q)}
        try:
            b = cache.get(URL, params={"qtrid": qtrid(q), "scripcode": firm["bse_code"]}, headers=HEADERS,
                          retries=2, timeout=60, offline=offline)
            row.update(parse_summary(b))
        except cache.CacheMiss:
            row["status"] = "not_cached"
        except (cache.FetchError, ValueError) as e:
            row.update(status="error", error=str(e)[:200])
        out.append(row)
        if row["status"] == "ok":
            seen_data, empties_after = True, 0
        elif row["status"] == "empty":
            empties_after += 1
            if firm["status"] == "Delisted" and empties_after >= 2:
                break       # delisted before the quarters asked, or since: nothing more will come
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.fetch_shareholding",
                                     description="Promoter holding and pledging from BSE shareholding patterns")
    parser.add_argument("--sample", type=pathlib.Path, default=DERIVED_DIR / "sample.parquet")
    parser.add_argument("--start", type=dt.date.fromisoformat, default=FIRST_QUARTER)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--quarterly", action="store_true", help="every quarter, not only March (×4 requests)")
    parser.add_argument("--role", choices=("fraud", "control"), action="append", help="default both")
    parser.add_argument("--limit", type=int, help="first N firms only (a dry run)")
    parser.add_argument("--prices", type=pathlib.Path, default=DERIVED_DIR / "prices",
                        help="Day 4 price files: limit each firm to its trading span (skipped if absent)")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--scratch", type=pathlib.Path, help="write under DIR instead of derived/")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_path = (args.scratch or DERIVED_DIR) / "shareholding.parquet"
    assert_safe_output(out_path)
    setup_logging(args.log_level, None if args.scratch else LOG_FILE)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cache.reset_stats()
    firms = firm_codes(pd.read_parquet(args.sample))
    if args.role:
        firms = firms[firms.role.isin(args.role)]
    no_bse = firms.bse_code.isna().sum()
    firms = firms[firms.bse_code.notna()]
    if args.limit:
        firms = firms.head(args.limit)
    quarters = quarter_ends(args.start, args.end, args.quarterly)
    log.info("%d firms on BSE (%d sample firms not on BSE: no data); %d quarters each, %s to %s", len(firms), no_bse,
             len(quarters), quarters[0], quarters[-1])
    spans = trading_spans(args.prices, quarters[0].year - 1) if args.prices.is_dir() else None
    if spans is not None:
        planned = sum(len(in_span(quarters, spans.get(c))) for c in firms.bse_code)
        log.info("prices found: %d requests planned (%d firms have not traded on BSE since %d)", planned,
                 sum(c not in spans for c in firms.bse_code), quarters[0].year - 1)
    else:
        log.warning("no price files at %s: asking every quarter (delisted firms stop after two empties)", args.prices)
    rows, t0 = [], time.time()
    for i, f in enumerate(firms.to_dict("records"), start=1):
        rows += fetch_firm(f, in_span(quarters, spans.get(f["bse_code"])) if spans is not None else quarters,
                           args.offline)
        if i % 50 == 0:
            rate = (time.time() - t0) / i
            log.info("%d/%d firms  network_calls=%d  eta %.0f min", i, len(firms), cache.NETWORK_CALLS,
                     rate * (len(firms) - i) / 60)
            pd.DataFrame(rows).to_parquet(out_path, index=False)    # checkpoint
    out = pd.DataFrame(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, index=False)
    ok = out[out.status == "ok"]
    log.info("rows %d; status %s", len(out), out.status.value_counts().to_dict())
    log.info("firms with data %d of %d; promoter %% median %.1f; firm-quarters with pledges %d (%.1f%% of parsed with "
             "a pledge value)", ok.firm_id.nunique(), len(firms), ok.promoter_pct.median() if len(ok) else float("nan"),
             int((ok.pledged_shares > 0).sum()), 100 * (ok.pledged_shares > 0).mean() if len(ok) else 0)
    log.info("network_calls=%d; elapsed %.1f min; wrote %s", cache.NETWORK_CALLS, (time.time() - t0) / 60, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
