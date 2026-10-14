"""Tests for the price fetcher (Week 4 Day 4): URLs, parsing, holidays, corporate actions. No network."""

import datetime as dt
import io
import zipfile

import pandas as pd
import pytest

from src import fetch_prices as fp

D = dt.date


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


NSE_OLD = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,\n"
           "TATASTEEL,EQ,98.1,102,97.15,100.35,100.2,959.4,137156107,13644336927.1,28-JUL-2022,628262,INE081A01012,\n"
           "GOLDBEES,EQ,1,1,1,1,1,1,1,1,28-JUL-2022,1,INF204KB17I5,\n"
           "TATASTEEL,N1,1,1,1,1,1,1,1,1,28-JUL-2022,1,INE081A01012,\n")
NSE_2010 = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,\n"
            "20MICRONS,EQ,46,47.9,45.7,47.55,47.5,46.05,36282,1719201.35,4-JAN-2010,\n")
BSE_OLD = ("SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,NO_TRADES,NO_OF_SHRS,NET_TURNOV,TDCLOINDI\n"
           "526987,URJA GLOBAL,B ,Q,52.90,53.85,52.20,53.85,53.85,51.30,95,15262,816596.00,\n"
           "800001,SOME BOND,F ,D,100,100,100,100,100,100,1,1,100,\n")
UDIFF = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
         "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
         "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
         "2024-07-08,2024-07-08,CM,BSE,STK,500002,INE117A01022,ABB,A,,,,,ABB INDIA LIMITED,8682.10,8688.35,8494.15,"
         "8558.85,8594.90,8680.85,,8559.55,,,7430,63801238.00,2090,F1,1,,,,,\n")


def test_urls_switch_format_on_8_july_2024():
    assert fp.bhav_url("NSE", D(2024, 7, 5)).endswith("/EQUITIES/2024/JUL/cm05JUL2024bhav.csv.zip")
    assert fp.bhav_url("NSE", D(2024, 7, 8)).endswith("/content/cm/BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip")
    assert fp.bhav_url("BSE", D(2010, 1, 4)).endswith("/Equity/EQ040110_CSV.ZIP")
    assert fp.bhav_url("BSE", D(2024, 7, 8)).endswith("/Equity/BhavCopy_BSE_CM_0_0_0_20240708_F_0000.CSV")


def test_weekdays():
    assert [d.day for d in fp.weekdays(D(2022, 7, 29), D(2022, 8, 2))] == [29, 1, 2]


def test_a_bse_holiday_page_is_not_a_bhavcopy():
    assert fp.csv_bytes(b'<!DOCTYPE html><html lang="en">') is None
    assert fp.csv_bytes(b"PK\x03\x04garbage") is None
    assert fp.csv_bytes(_zip("x.csv", NSE_OLD)).startswith(b"SYMBOL")
    assert fp.csv_bytes(UDIFF.encode()).startswith(b"TradDt")


def test_parse_nse_old_keeps_equity_series_only():
    p = fp.parse_bhav(_zip("cm.csv", NSE_OLD), "NSE", D(2022, 7, 28))
    assert p.code.tolist() == ["TATASTEEL", "GOLDBEES"]  # N1 (a debt series) dropped
    t = p.iloc[0]
    assert (t.close, t.prev_close, t.volume, t.trades, t["isin"]) == (100.35, 959.4, 137156107, 628262, "INE081A01012")
    assert list(p.columns) == fp.COLUMNS


def test_parse_nse_2010_has_no_isin_or_trades():
    p = fp.parse_bhav(_zip("cm.csv", NSE_2010), "NSE", D(2010, 1, 4))
    assert p.iloc[0].close == 47.55 and pd.isna(p.iloc[0]["isin"]) and pd.isna(p.iloc[0].trades)


def test_parse_bse_old_keeps_equities():
    p = fp.parse_bhav(_zip("EQ.CSV", BSE_OLD), "BSE", D(2010, 1, 4))
    assert p.code.tolist() == ["526987"] and p.iloc[0]["name"] == "URJA GLOBAL" and p.iloc[0].series == "B"


def test_parse_udiff_uses_the_scrip_code_at_bse_and_the_symbol_at_nse():
    b = fp.parse_bhav(UDIFF.encode(), "BSE", D(2024, 7, 8))
    assert (b.iloc[0].code, b.iloc[0]["isin"], b.iloc[0].close) == ("500002", "INE117A01022", 8558.85)
    n = fp.parse_bhav(UDIFF.replace(",BSE,", ",NSE,").encode(), "NSE", D(2024, 7, 8))
    assert n.code.tolist() == []                                  # series A is not an NSE equity series
    n = fp.parse_bhav(UDIFF.replace(",ABB,A,", ",ABB,EQ,").encode(), "NSE", D(2024, 7, 8))
    assert n.code.tolist() == ["ABB"]


@pytest.mark.parametrize("purpose, kind, factor", [
    ("Stock  Split From Rs.10/- to Rs.1/-", "split", 10.0),
    ("Stock  Split From Rs.2/- to Re.1/-", "split", 2.0),
    ("Bonus issue 1:1", "bonus", 2.0),
    ("Bonus issue 1:2", "bonus", 1.5),
    ("Bonus issue 7:10", "bonus", 1.7),
    ("Consolidation of Shares ", "consolidation", None),
    ("Right Issue of Equity Shares ", "rights", None),
    ("Final Dividend - Rs. - 0.1200", "dividend", None),
    ("E.G.M. ", "other", None),
])
def test_parse_purpose(purpose, kind, factor):
    assert fp.parse_purpose(purpose) == (kind, factor)


def test_month_ranges():
    r = list(fp.month_ranges(D(2023, 12, 15), D(2024, 2, 10)))
    assert r == [(D(2023, 12, 1), D(2023, 12, 31)), (D(2024, 1, 1), D(2024, 1, 31)), (D(2024, 2, 1), D(2024, 2, 10))]


def test_run_ledger_retries_a_one_sided_holiday_once(tmp_path, monkeypatch):
    calls = []
    nse = _zip("cm.csv", NSE_OLD)
    bse = _zip("EQ.CSV", BSE_OLD)

    def fake(exchange, d, *, offline, retry_key=None):
        calls.append((exchange, d, retry_key))
        if d == D(2022, 8, 9):
            return "holiday", None                       # both shut
        if exchange == "BSE" and d == D(2022, 8, 10):
            return ("ok", bse) if retry_key else ("holiday", None)   # a stray HTML page, fixed on retry
        return "ok", nse if exchange == "NSE" else bse

    monkeypatch.setattr(fp, "fetch_day", fake)
    ledger = fp.run(D(2022, 8, 8), D(2022, 8, 10), ("NSE", "BSE"), tmp_path, offline=False, retry_holidays=False)
    st = {(r.exchange, r.date): r.status for r in ledger.itertuples()}
    assert st[("NSE", D(2022, 8, 9))] == st[("BSE", D(2022, 8, 9))] == "holiday"
    assert st[("BSE", D(2022, 8, 10))] == "ok"
    assert ("BSE", D(2022, 8, 10), "retry-1") in calls and ("BSE", D(2022, 8, 9), "retry-1") not in calls
    prices = pd.read_parquet(tmp_path / "prices" / "NSE_2022.parquet")
    assert sorted(prices.date.unique()) == [D(2022, 8, 8), D(2022, 8, 10)]
    # a re-run does not ask NSE again for its holiday
    calls.clear()
    fp.run(D(2022, 8, 8), D(2022, 8, 10), ("NSE", "BSE"), tmp_path, offline=False, retry_holidays=False)
    assert ("NSE", D(2022, 8, 9), None) not in calls


def test_years_are_written_as_they_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "fetch_day", lambda ex, d, **_: ("ok", _zip("cm.csv", NSE_OLD)))
    fp.run(D(2021, 12, 30), D(2022, 1, 4), ("NSE",), tmp_path, offline=True, retry_holidays=False)
    assert {p.name for p in (tmp_path / "prices").iterdir()} == {"NSE_2021.parquet", "NSE_2022.parquet"}
    days = pd.read_parquet(tmp_path / "price_days.parquet")
    assert len(days) == 4 and (days.status == "ok").all()
