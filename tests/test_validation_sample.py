"""Tests for the Week 3 validation sample."""

import datetime as dt

import pandas as pd
import pytest

from src import validation_sample as vs


def _frame(n_regex=400, n_iso=100, n_stated=60, n_conduct=80, n_none=300):
    rows = []
    i = 0

    def add(n, stratum, iso=False):
        nonlocal i
        for _ in range(n):
            i += 1
            rows.append({"text_stem": str(i), "order_id": i,
                         "order_date": dt.date(2000 + i % 26, 1 + i % 12, 1),
                         "order_type": ["AO", "WTM", "SETTLEMENT", "ED_CGM"][i % 4],
                         "title": "Order in the matter of Illiquid Stock Options at BSE" if iso else f"Order {i}",
                         "source_url": f"https://www.sebi.gov.in/o_{i}.html", "pdf_url": None,
                         "pdf_path": f"raw/pdfs/{i}.pdf", "stratum": stratum, "iso": iso})
    add(n_regex, vs.REGEX)
    add(n_iso, vs.REGEX, iso=True)
    add(n_stated, vs.MODEL_STATED)
    add(n_conduct, vs.MODEL_CONDUCT)
    add(n_none, vs.NO_WINDOW)
    return pd.DataFrame(rows)


def test_strata_sizes_and_iso_cap():
    s = vs.draw(_frame())
    assert s.groupby("stratum").size().to_dict() == vs.SIZES
    assert s[s.stratum == vs.REGEX].iso.sum() == vs.ISO_CAP[vs.REGEX]
    assert s.text_stem.is_unique


def test_iso_is_capped_in_every_stratum():
    f = _frame()
    extra = f[f.stratum == vs.NO_WINDOW].head(50).copy()
    extra["text_stem"] = "iso" + extra.text_stem
    extra["iso"] = True
    s = vs.draw(pd.concat([f, extra]))
    assert s[s.stratum == vs.NO_WINDOW].iso.sum() == vs.ISO_CAP[vs.NO_WINDOW]


def test_seen_orders_are_excluded():
    f = _frame()
    seen = set(f[f.stratum == vs.MODEL_STATED].text_stem[:50])
    s = vs.draw(f, exclude=seen)
    assert not set(s.text_stem) & seen


def test_small_strata_are_taken_whole():
    s = vs.draw(_frame(n_stated=10))
    assert (s.stratum == vs.MODEL_STATED).sum() == 10


def test_draw_is_repeatable_and_seed_dependent():
    f = _frame()
    assert vs.draw(f, seed=1).text_stem.tolist() == vs.draw(f, seed=1).text_stem.tolist()
    assert vs.draw(f, seed=1).text_stem.tolist() != vs.draw(f, seed=2).text_stem.tolist()


def test_systematic_draw_spreads_over_types():
    f = _frame()
    s = vs.draw(f)
    regex = s[(s.stratum == vs.REGEX) & ~s.iso]
    assert set(regex.order_type) == {"AO", "WTM", "SETTLEMENT", "ED_CGM"}


def test_sheet_is_blind():
    f = _frame()
    f["violation_start"] = dt.date(2010, 1, 1)
    f["violation_raw"] = "machine output"
    out = vs.sheet(vs.draw(f))
    assert "stratum" not in out.columns and "iso" not in out.columns
    assert "violation_raw" not in out.columns
    assert (out[vs.CODING_COLUMNS] == "").all().all()  # coding columns start empty
    assert out.sample_no.tolist() == list(range(1, len(out) + 1))


def test_key_weights_back_to_the_corpus():
    f = _frame()
    s = vs.draw(f)
    k = vs.key(s, f, set(), vs.SEED)
    per = k.groupby("stratum").weight.sum()
    assert per.to_dict() == pytest.approx(f.groupby("stratum").size().astype(float).to_dict())


def test_main_refuses_to_overwrite_a_coding_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "load_frame", lambda *_a, **_k: _frame())
    monkeypatch.setattr(vs, "seen_stems", lambda *_a, **_k: set())
    assert vs.main(["--scratch", str(tmp_path)]) == 0
    sheet = tmp_path / "validation_sample.csv"
    sheet.write_text(sheet.read_text() + "\n# my coding")
    assert vs.main(["--scratch", str(tmp_path)]) == 1
    assert sheet.read_text().endswith("# my coding")
    assert vs.main(["--scratch", str(tmp_path), "--force"]) == 0
