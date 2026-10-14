"""Week 4 Day 4: daily prices — NSE and BSE bhavcopies, BSE corporate actions, the BSE 500 index.

All through ``src/cache.py`` (sequential, 1.5 s apart, cached permanently).

Bhavcopies (one file per exchange per trading day, every listed equity):

* NSE to 5 Jul 2024: ``content/historical/EQUITIES/<YYYY>/<MON>/cm<DD><MON><YYYY>bhav.csv.zip``
  (SYMBOL, SERIES, OHLC, PREVCLOSE, TOTTRDQTY, TOTTRDVAL; TOTALTRADES and ISIN only from ~2011);
* BSE to 5 Jul 2024: ``download/BhavCopy/Equity/EQ<DDMMYY>_CSV.ZIP`` (SC_CODE, SC_NAME, SC_GROUP,
  SC_TYPE, OHLC, PREVCLOSE, NO_TRADES, NO_OF_SHRS, NET_TURNOV);
* both from 8 Jul 2024 (the runbook's "URL structure changed in 2024"): the common UDiFF file
  ``BhavCopy_<NSE|BSE>_CM_0_0_0_<YYYYMMDD>_F_0000.csv[.zip]`` (TckrSymb / FinInstrmId, ISIN, …).

Weekdays only (a few special Saturday and Muhurat sessions are missed). A holiday is
an HTTP 404 at NSE, not cached; at BSE it is an HTML page with status 200, which the
cache keeps: it reads as "no file" and is never re-requested. ``derived/price_days.parquet``
records every day's status, so a re-run does not re-ask NSE for its holidays. A weekday
one exchange has and the other lacks is asked again once under a new cache key
(``retry-1``), never by overwriting a cached entry (``raw/`` is append-only).

Corporate actions: BSE's corporate-action list, one request per year: splits ("Stock
Split From Rs.10/- to Rs.1/-"), bonuses ("Bonus issue 1:2"), consolidations, rights,
dividends. **Bhavcopy prices are not adjusted** (on Tata Steel's 1:10 split day,
28 Jul 2022, NSE's PREVCLOSE is 959.40 and CLOSE 100.35): ``src/prices.py`` adjusts
for splits and bonuses with these factors.

Market index: the BSE 500, daily, from BSE's index archive, one request per month
(responses stop at 26 rows). NSE's daily index file exists only from about 2013.

Writes ``derived/prices/<NSE|BSE>_<year>.parquet`` (one row per security per trading
day: date, exchange, code, series, isin, name, OHLC, last, prev_close, volume,
turnover, trades), ``derived/price_days.parquet``, ``derived/corporate_actions.parquet``,
``derived/market_index.parquet``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import logging
import pathlib
import re
import time
import zipfile

import pandas as pd

from src import cache
from src.extract_text import assert_safe_output, setup_logging

log = logging.getLogger("fetch_prices")

REPO_ROOT = cache.REPO_ROOT
DERIVED_DIR = REPO_ROOT / "derived"
LOG_FILE = REPO_ROOT / "logs" / "fetch_prices.log"

START = dt.date(2004, 1, 1)
NEW_FORMAT_FROM = dt.date(2024, 7, 8)
EXCHANGES = ("NSE", "BSE")
BSE_HEADERS = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
               "Accept": "application/json, text/plain, */*"}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
#: NSE equity series kept: rolling (EQ), trade-for-trade (BE, BZ, BT), SME (SM, ST, SZ).
NSE_SERIES = {"EQ", "BE", "BZ", "BT", "SM", "ST", "SZ"}
CA_URL = "https://api.bseindia.com/BseIndiaAPI/api/DefaultData/w"
INDEX_URL = "https://api.bseindia.com/BseIndiaAPI/api/IndexArchDaily/w"
INDEX = "BSE500"
COLUMNS = ["date", "exchange", "code", "series", "isin", "name", "open", "high", "low", "close", "last",
           "prev_close", "volume", "turnover", "trades"]


def bhav_url(exchange: str, d: dt.date) -> str:
    if exchange == "NSE":
        if d >= NEW_FORMAT_FROM:
            return f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
        mon = MONTHS[d.month - 1]
        return (f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d.year}/{mon}/"
                f"cm{d.day:02d}{mon}{d.year}bhav.csv.zip")
    if d >= NEW_FORMAT_FROM:
        return f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"
    return f"https://www.bseindia.com/download/BhavCopy/Equity/EQ{d:%d%m%y}_CSV.ZIP"


def weekdays(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def csv_bytes(content: bytes) -> bytes | None:
    """The CSV inside a bhavcopy (zipped or not); None for an HTML page or anything else."""
    if content[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(content))
            return z.read(z.namelist()[0])
        except (zipfile.BadZipFile, IndexError, KeyError):
            return None
    head = content[:200].lstrip(b"\xef\xbb\xbf").upper()
    return content if head.startswith((b"TRADDT", b"SYMBOL", b"SC_CODE")) else None


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "").str.strip(), errors="coerce")


def parse_bhav(content: bytes, exchange: str, d: dt.date) -> pd.DataFrame:
    """One bhavcopy → the common columns, equities only."""
    raw = csv_bytes(content)
    if raw is None:
        raise ValueError("not a bhavcopy")
    t = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    t.columns = [c.strip().upper() for c in t.columns]
    t = t.apply(lambda c: c.str.strip())
    if "TRADDT" in t.columns:                                   # UDiFF, both exchanges, from Jul 2024
        t = t[t.FININSTRMTP == "STK"]
        out = pd.DataFrame({"code": t.TCKRSYMB if exchange == "NSE" else t.FININSTRMID, "series": t.SCTYSRS,
                            "isin": t.ISIN, "name": t.FININSTRMNM, "open": t.OPNPRIC, "high": t.HGHPRIC,
                            "low": t.LWPRIC, "close": t.CLSPRIC, "last": t.LASTPRIC, "prev_close": t.PRVSCLSGPRIC,
                            "volume": t.TTLTRADGVOL, "turnover": t.TTLTRFVAL, "trades": t.TTLNBOFTXSEXCTD})
        if exchange == "NSE":
            out = out[out.series.isin(NSE_SERIES)]
    elif exchange == "NSE":
        t = t[t.SERIES.isin(NSE_SERIES)]
        out = pd.DataFrame({"code": t.SYMBOL, "series": t.SERIES, "isin": t.get("ISIN"), "name": None,
                            "open": t.OPEN, "high": t.HIGH, "low": t.LOW, "close": t.CLOSE, "last": t.LAST,
                            "prev_close": t.PREVCLOSE, "volume": t.TOTTRDQTY, "turnover": t.TOTTRDVAL,
                            "trades": t.get("TOTALTRADES")})
    else:
        t = t[t.SC_TYPE == "Q"]
        out = pd.DataFrame({"code": t.SC_CODE, "series": t.SC_GROUP, "isin": t.get("ISIN_CODE"), "name": t.SC_NAME,
                            "open": t.OPEN, "high": t.HIGH, "low": t.LOW, "close": t.CLOSE, "last": t.LAST,
                            "prev_close": t.PREVCLOSE, "volume": t.NO_OF_SHRS, "turnover": t.NET_TURNOV,
                            "trades": t.NO_TRADES})
    for c in ("open", "high", "low", "close", "last", "prev_close", "volume", "turnover", "trades"):
        out[c] = _num(out[c]) if c in out and out[c] is not None else pd.NA
    out["isin"] = out["isin"].where(out["isin"].astype(str).str.match(r"^IN[A-Z0-9]{10}$"), None) \
        if "isin" in out else None
    out.insert(0, "exchange", exchange)
    out.insert(0, "date", d)
    return out.reindex(columns=COLUMNS).reset_index(drop=True)


def fetch_day(exchange: str, d: dt.date, *, offline: bool, retry_key: str | None = None) -> tuple[str, bytes | None]:
    """(status, content): ok, holiday (no file that day), missing (not cached, offline), error."""
    try:
        b = cache.get(bhav_url(exchange, d), headers=BSE_HEADERS if exchange == "BSE" else None, retries=2,
                      timeout=90, offline=offline, cache_key_extra=retry_key)
    except cache.CacheMiss:
        return "missing", None
    except cache.FetchError as e:
        return ("holiday", None) if "HTTP 404" in str(e) else ("error", None)
    return ("ok", b) if csv_bytes(b) is not None else ("holiday", None)


# --- corporate actions and the index ---------------------------------------------------------------------------

SPLIT_RE = re.compile(r"split.*?from\s*rs\.?\s*([\d.]+).*?to\s*(?:rs\.?|re\.?)\s*([\d.]+)", re.I)
BONUS_RE = re.compile(r"bonus.*?(\d+)\s*:\s*(\d+)", re.I)


def parse_purpose(purpose: str) -> tuple[str, float | None]:
    """(kind, factor): shares after / shares before, for the actions prices are adjusted for."""
    p = purpose or ""
    if m := SPLIT_RE.search(p):
        old, new = float(m.group(1)), float(m.group(2))
        return "split", (old / new if new else None)
    if m := BONUS_RE.search(p):
        a, b = int(m.group(1)), int(m.group(2))
        return "bonus", ((a + b) / b if b else None)
    low = p.lower()
    for word, kind in (("consolidat", "consolidation"), ("right", "rights"), ("dividend", "dividend"),
                       ("demerger", "demerger"), ("spin", "demerger"), ("amalgamat", "merger"), ("buy back", "buyback")):
        if word in low:
            return kind, None
    return "other", None


def ca_params(year: int, end: dt.date) -> dict:
    to = min(dt.date(year, 12, 31), end)
    return {"Fdate": f"{year}0101", "TDate": f"{to:%Y%m%d}", "Purposecode": "", "ddlcategorys": "E",
            "ddlindustrys": "", "scripcode": "", "segment": "0", "strSearch": "S"}


def corporate_actions(start: dt.date, end: dt.date, offline: bool) -> pd.DataFrame:
    rows = []
    for y in range(start.year, end.year + 1):
        for r in json.loads(cache.get(CA_URL, params=ca_params(y, end), headers=BSE_HEADERS, retries=2,
                                      timeout=120, offline=offline)):
            kind, factor = parse_purpose(r.get("Purpose", ""))
            rows.append({"scrip_code": str(r["scrip_code"]), "short_name": r.get("short_name"),
                         "long_name": r.get("long_name"), "ex_date": dt.datetime.strptime(r["exdate"], "%Y%m%d").date(),
                         "purpose": (r.get("Purpose") or "").strip(), "kind": kind, "factor": factor})
    return pd.DataFrame(rows).drop_duplicates().reset_index(drop=True)


def month_ranges(start: dt.date, end: dt.date):
    d = dt.date(start.year, start.month, 1)
    while d <= end:
        nxt = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        yield d, min(nxt - dt.timedelta(days=1), end)
        d = nxt


def market_index(start: dt.date, end: dt.date, offline: bool) -> pd.DataFrame:
    rows = []
    for lo, hi in month_ranges(start, end):
        b = cache.get(INDEX_URL, params={"fmdt": f"{lo:%d/%m/%Y}", "index": INDEX, "period": "D", "todt": f"{hi:%d/%m/%Y}"},
                      headers=BSE_HEADERS, retries=2, timeout=60, offline=offline)
        for r in json.loads(b).get("Table", []):
            rows.append({"date": dt.date.fromisoformat(r["tdate"][:10]), "index": INDEX, "open": r["I_open"],
                         "high": r["I_high"], "low": r["I_low"], "close": r["I_close"],
                         "prev_close": r.get("Previousclose")})
    return pd.DataFrame(rows).drop_duplicates("date").sort_values("date").reset_index(drop=True)


# --- the run ---------------------------------------------------------------------------------------------------

def read_days(path: pathlib.Path) -> dict[tuple[str, dt.date], str]:
    if not path.exists():
        return {}
    d = pd.read_parquet(path)
    return dict(zip(zip(d.exchange, d.date), d.status))


def run(start: dt.date, end: dt.date, exchanges, out_dir: pathlib.Path, *, offline: bool, retry_holidays: bool,
        progress_every: int = 250) -> pd.DataFrame:
    days_path = out_dir / "price_days.parquet"
    known = read_days(days_path)
    ledger = []
    buffers: dict[tuple[str, int], list[pd.DataFrame]] = {}
    t0, n_done, todo = time.time(), 0, list(weekdays(start, end))
    for d in todo:
        if buffers and d.year not in {y for _, y in buffers}:
            _flush(buffers, out_dir)                    # a year is complete
            _write_days(days_path, ledger)
        status = {}
        content = {}
        for ex in exchanges:
            if ex == "NSE" and not retry_holidays and known.get((ex, d)) in ("holiday", "holiday_confirmed"):
                status[ex], content[ex] = known[(ex, d)], None   # NSE 404s are not cached; do not ask again
                continue
            status[ex], content[ex] = fetch_day(ex, d, offline=offline)
        if not offline and set(exchanges) == {"NSE", "BSE"}:
            for ex, other in (("NSE", "BSE"), ("BSE", "NSE")):
                if status[ex] == "holiday" and status[other] == "ok" and known.get((ex, d)) != "holiday_confirmed":
                    status[ex], content[ex] = fetch_day(ex, d, offline=offline, retry_key="retry-1")
                    if status[ex] == "holiday":
                        status[ex] = "holiday_confirmed"   # the other exchange traded; asked twice
        for ex in exchanges:
            rows = 0
            if status[ex] == "ok":
                try:
                    frame = parse_bhav(content[ex], ex, d)
                    buffers.setdefault((ex, d.year), []).append(frame)
                    rows = len(frame)
                except Exception as e:  # noqa: BLE001
                    log.warning("%s %s: unreadable bhavcopy (%s)", ex, d, e)
                    status[ex] = "unreadable"
            ledger.append({"date": d, "exchange": ex, "status": status[ex], "rows": rows, "url": bhav_url(ex, d)})
        n_done += 1
        if n_done % progress_every == 0:
            rate = (time.time() - t0) / n_done
            log.info("%s  %d/%d days  network_calls=%d  eta %.0f min", d, n_done, len(todo), cache.NETWORK_CALLS,
                     rate * (len(todo) - n_done) / 60)
    _flush(buffers, out_dir)
    _write_days(days_path, ledger)
    return pd.DataFrame(ledger)


def _flush(buffers, out_dir: pathlib.Path) -> None:
    for (ex, year), frames in list(buffers.items()):
        p = out_dir / "prices" / f"{ex}_{year}.parquet"
        assert_safe_output(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        new = pd.concat(frames, ignore_index=True)
        if p.exists():   # a partial year from an earlier run
            new = pd.concat([pd.read_parquet(p), new], ignore_index=True)
        new.drop_duplicates(["date", "exchange", "code", "series"], keep="last").to_parquet(p, index=False)
        del buffers[(ex, year)]


def _write_days(path: pathlib.Path, ledger: list[dict]) -> None:
    new = pd.DataFrame(ledger)
    if path.exists():
        new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
    new.drop_duplicates(["date", "exchange"], keep="last").sort_values(["date", "exchange"]).to_parquet(path, index=False)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.fetch_prices",
                                     description="Fetch and parse NSE/BSE bhavcopies, corporate actions, BSE 500")
    parser.add_argument("--start", type=dt.date.fromisoformat, default=START)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today() - dt.timedelta(days=1))
    parser.add_argument("--exchange", choices=EXCHANGES, action="append", help="default both")
    parser.add_argument("--offline", action="store_true", help="cache only (a re-parse): no network")
    parser.add_argument("--skip-bhavcopies", action="store_true", help="corporate actions and the index only")
    parser.add_argument("--retry-holidays", action="store_true", help="ask NSE again for days it 404ed")
    parser.add_argument("--scratch", type=pathlib.Path, help="write outputs under DIR instead of derived/")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_dir = args.scratch or DERIVED_DIR
    for p in (out_dir / "prices", out_dir / "price_days.parquet", out_dir / "corporate_actions.parquet",
              out_dir / "market_index.parquet"):
        assert_safe_output(p)
    setup_logging(args.log_level, None if args.scratch else LOG_FILE)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per request is noise over 12,000 days
    cache.reset_stats()
    exchanges = tuple(args.exchange or EXCHANGES)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    ca = corporate_actions(args.start, args.end, args.offline)
    ca.to_parquet(out_dir / "corporate_actions.parquet", index=False)
    log.info("corporate actions  %d (%s)", len(ca), ca.kind.value_counts().to_dict())
    idx = market_index(args.start, args.end, args.offline)
    idx.to_parquet(out_dir / "market_index.parquet", index=False)
    log.info("market index       %s: %d days, %s to %s", INDEX, len(idx), idx.date.min(), idx.date.max())

    if not args.skip_bhavcopies:
        ledger = run(args.start, args.end, exchanges, out_dir, offline=args.offline,
                     retry_holidays=args.retry_holidays)
        st = ledger.groupby(["exchange", "status"]).size().unstack(fill_value=0)
        log.info("days by status\n%s", st.to_string())
        both = ledger.pivot_table(index="date", columns="exchange", values="status", aggfunc="first")
        if set(exchanges) == {"NSE", "BSE"}:
            odd = both[(both.NSE == "ok") != (both.BSE == "ok")]
            log.info("weekdays only one exchange has: %d (first: %s)", len(odd), odd.head(5).to_dict("index"))
        log.info("rows               %s", ledger.groupby("exchange").rows.sum().to_dict())
    log.info("network_calls=%d; elapsed %.1f min; wrote under %s", cache.NETWORK_CALLS, (time.time() - t0) / 60,
             out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
