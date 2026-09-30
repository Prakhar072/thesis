"""Tests for the Week 2 checkpoint table (derived/orders.parquet)."""

import datetime as dt

import pandas as pd
import pytest

from src import build_orders as bo
from src.extract_text import text_stem

URLS = ["https://www.sebi.gov.in/enforcement/orders/jan-2020/a_101.html",
        "https://www.sebi.gov.in/enforcement/orders/jan-2020/b_102.html",
        "https://www.sebi.gov.in/sebi_data/attachdocs/1234567890.pdf"]   # a listing with no order id
IDS = [101, 102, None]


def _frame(columns, rows):
    return pd.DataFrame([{c: r.get(c) for c in columns} for r in rows], columns=columns)


def make_derived(tmp_path, *, parties_extra=None):
    stems = [text_stem(i, u) for i, u in zip(IDS, URLS)]
    listing_cols = [c for c in bo.LISTING_COLUMNS if c != "text_stem"]
    listings = _frame(listing_cols, [{"order_id": i, "source_url": u, "order_type": "AO",
                                      "order_date": dt.date(2020, 1, n + 1), "title": f"Order {n}"}
                                     for n, (i, u) in enumerate(zip(IDS, URLS))])
    listings["order_id"] = listings["order_id"].astype("Int64")
    listings.to_parquet(tmp_path / "listings.parquet")
    docs = _frame(["order_id", "source_url", "pdf_url", "doc_shape"],
                  [{"order_id": i, "source_url": u, "pdf_url": f"{u}.pdf", "doc_shape": "IFRAME_PDF"}
                   for i, u in zip(IDS, URLS)])
    docs["order_id"] = docs["order_id"].astype("Int64")
    docs.to_parquet(tmp_path / "order_documents.parquet")
    _frame(bo.TEXT_COLUMNS, [{"text_stem": s, "status": "OK", "n_pages": 3} for s in stems]) \
        .to_parquet(tmp_path / "text_documents.parquet")
    _frame(bo.WINDOW_COLUMNS, [{"text_stem": s, "violation_start": dt.date(2015, 1, 1) if k == 0 else None}
                               for k, s in enumerate(stems)]).to_parquet(tmp_path / "windows_checked.parquet")
    _frame(bo.ACTION_COLUMNS, [{"text_stem": s, "penalty_total": 500_000 if k == 1 else None}
                               for k, s in enumerate(stems)]).to_parquet(tmp_path / "order_actions.parquet")
    rows = [{"text_stem": s, "n_parties": 1, "regulations": '["PFUTP 3(a)"]', "regimes": '["PFUTP"]'} for s in stems]
    _frame(bo.PARTY_COLUMNS, rows + (parties_extra or [])).to_parquet(tmp_path / "order_parties.parquet")
    _frame(bo.CATEGORY_COLUMNS, [{"text_stem": s, "fraud_category": "MARKET_MANIPULATION"} for s in stems]) \
        .to_parquet(tmp_path / "order_categories.parquet")
    _frame(bo.CHANNEL_COLUMNS, [{"text_stem": s, "detection_channel": "UNCLEAR"} for s in stems]) \
        .to_parquet(tmp_path / "order_channels.parquet")
    return stems


def test_one_row_per_order_and_the_id_less_order_is_not_cross_joined(tmp_path):
    stems = make_derived(tmp_path)
    orders = bo.build(tmp_path)
    assert len(orders) == 3 and orders["text_stem"].tolist() == stems
    assert orders.loc[0, "violation_start"] == dt.date(2015, 1, 1)
    assert orders.loc[1, "penalty_total"] == 500_000
    assert orders.loc[2, "order_id"] is pd.NA and orders.loc[2, "pdf_url"] == URLS[2] + ".pdf"
    s = bo.summarise(orders)
    assert s["orders"] == 3 and s["violation_window"] == "1 (33.3%)" and s["regulations_violated"] == "3 (100.0%)"


def test_a_duplicate_row_upstream_fails_loudly(tmp_path):
    stems = make_derived(tmp_path, parties_extra=[{"text_stem": text_stem(101, URLS[0]), "n_parties": 2}])
    assert stems
    with pytest.raises(pd.errors.MergeError):
        bo.build(tmp_path)


def test_an_order_not_in_the_listings_fails_loudly(tmp_path):
    make_derived(tmp_path, parties_extra=[{"text_stem": "999999", "n_parties": 1}])
    with pytest.raises(ValueError, match="not in the listings"):
        bo.build(tmp_path)


def test_write_refuses_raw(tmp_path):
    with pytest.raises(ValueError):
        bo.write(pd.DataFrame(), bo.REPO_ROOT / "raw" / "pdfs" / "orders.parquet")
