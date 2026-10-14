"""Tests for the name normaliser and the listed universe (Week 4 Day 1)."""

import datetime as dt
import json

from src import names
from src import universe as uv


def test_norm_follows_the_runbook():
    assert names.norm("M/s. Kavveri Telecom Products Pvt. Ltd.") == "KAVVERI TELECOM PRODUCTS"
    assert names.norm("Saatal Kattha & Chemicals Ltd") == names.norm("Saatal Kattha and Chemicals Limited")
    assert names.norm("The ABB India Limited") == "ABB"
    assert names.norm("Hindustan Construction Co. Ltd") == "HINDUSTAN CONSTRUCTION"


def test_norm_expands_bse_abbreviations_on_both_sides():
    assert names.norm("Bharat Heavy Electricals Corpn Ltd") == names.norm("Bharat Heavy Electricals Corporation Limited")
    assert names.norm("Apex Inds Ltd") == names.norm("Apex Industries Limited")


def test_names_keeps_former_names():
    assert names.names("New Name Ltd (formerly known as Old Name Ltd)") == ["NEW NAME", "OLD NAME"]
    assert names.names("") == [] and names.names(None) == []


def test_firm_id_is_the_isin_issuer_prefix():
    assert uv.firm_id("INE117A01022", "x") == uv.firm_id("INE117A01014", "x") == "INE117A"
    assert uv.firm_id("IN9117A01011", "x") == "INE117A"     # partly paid
    assert uv.firm_id("NA", "BSE:526811") == "BSE:526811"
    assert uv.firm_id(None, "NSE:XYZ") == "NSE:XYZ"


def _bse(status, rows):
    return json.dumps([{"SCRIP_CD": c, "Scrip_Name": short, "Status": status, "GROUP": "B", "FACE_VALUE": "10.00",
                        "ISIN_NUMBER": isin, "INDUSTRY": None, "scrip_id": sym, "Segment": "Equity",
                        "Issuer_Name": name, "Mktcap": "0"} for c, short, name, isin, sym in rows]).encode()


NSE = (b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
       b"KAVVERI,Kavveri Defence & Wireless Technologies Limited,EQ,06-OCT-2008,10,1,INE641C01019,10\n"
       b"NEWSYM,Renamed Twice Limited,EQ,01-JAN-2010,10,1,INE999Z01011,10\n")
SME = (b"SYMBOL,NAME_OF_COMPANY,SERIES,DATE_OF_LISTING,PAID_UP_VALUE,ISIN_NUMBER,FACE_VALUE,\n"
       b"RKFAL,R.K. Fashion Accessories Limited,ST,12-Oct-26,10,INE2O2Y01016,10,\n")
NAMECHANGE = (b"SYMBOL,OLD NAME OF THE COMPANY,NEW NAME OF THE COMPANY,EFFECTIVE DATE\n"
              b"KAVVERI,Kavveri Telecom Products Limited,Kavveri Defence & Wireless Technologies Limited,10-MAR-2021\n"
              b"OLDSYM,First Name Limited,Renamed Twice Limited,05-MAY-2012\n"
              b"GONE,Delisted Old Name Limited,Taksheel Solutions Limited,01-JAN-2011\n"
              b"1IADD,Deutsche MF - DWS Hybrid FTF Series 40,DHFL Pramerica MF - Hybrid FTF Series 40,08-MAR-2016\n"
              b"ZZZ,Nowhere Old Limited,Nowhere New Limited,01-JAN-2000\n")
SYMBOLCHANGE = (b"NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"
                b" Renamed Twice Limited,OLDSYM,MIDSYM,01-JAN-2013\n Renamed Twice Limited,MIDSYM,NEWSYM,01-JAN-2015\n")


def _build():
    bse = {"Active": _bse("Active", [("590107", "Kavveri Defence", "Kavveri Defence & Wireless Technologies Ltd",
                                      "INE641C01019", "KAVVERI"),
                                     ("590999", "Nippon ETF", "Nippon India ETF", "INF204K01ZH0", "NETF")]),
           "Delisted": _bse("Delisted", [("533302", "Taksheel Solutions Ltd", "Taksheel Solutions Ltd",
                                          "INE889I01019", "TAKSHEEL"),
                                         ("526811", "Saatal Kattha", "Saatal Kattha & Chemicals Ltd.", "NA", "SAATAL")]),
           "Suspended": _bse("Suspended", [])}
    return uv.build(bse, NSE, SME, NAMECHANGE, SYMBOLCHANGE)


def test_universe_rows_and_firms():
    universe, firms, aliases, changes = _build()
    assert set(universe.firm_id) == {"INE641C", "INE889I", "BSE:526811", "INE999Z", "INE2O2Y"}  # fund dropped
    k = firms.set_index("firm_id").loc["INE641C"]
    assert k.on_bse and k.on_nse and k["name"] == "Kavveri Defence & Wireless Technologies Limited"
    assert firms.set_index("firm_id").loc["INE889I"].status == "Delisted"
    assert universe[universe.code == "RKFAL"].listing_date.iloc[0] == dt.date(2026, 10, 12)


def test_name_changes_are_placed_on_firms():
    _, _, aliases, changes = _build()
    c = changes.set_index("symbol")
    assert c.loc["KAVVERI"].firm_id == "INE641C" and c.loc["KAVVERI"].resolved_by == "symbol"
    assert c.loc["OLDSYM"].firm_id == "INE999Z" and c.loc["OLDSYM"].resolved_by == "symbol_change"  # chained
    assert c.loc["GONE"].firm_id == "INE889I" and c.loc["GONE"].resolved_by == "name"  # delisted from NSE
    assert c.loc["ZZZ"].firm_id is None
    assert "1IADD" not in c.index                                                          # a fund scheme
    old = aliases[(aliases.firm_id == "INE641C") & (aliases.source == "nse_old_name")].iloc[0]
    assert old.alias_norm == "KAVVERI TELECOM PRODUCTS" and old.valid_to == dt.date(2021, 3, 10)


def test_symbol_chain_survives_a_cycle():
    import pandas as pd
    sc = pd.DataFrame({"OLD_SYMBOL": ["A", "B"], "NEW_SYMBOL": ["B", "A"]})
    assert set(uv.symbol_chain(sc)) == {"A", "B"}
