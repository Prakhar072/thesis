"""Tests for the Week 3 validation sample."""

import datetime as dt
import os

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


def test_key_weights_are_per_iso_cell():
    # ISO is capped at 3 of 60 regex orders but is 100 of 500 in the corpus: a
    # stratum-wide weight would count it at 5% instead of 20%.
    f = _frame()
    s = vs.draw(f)
    k = vs.key(s, f, set(), vs.SEED)
    regex = k[k.stratum == vs.REGEX]
    assert regex[regex.iso].weight.unique().tolist() == pytest.approx([100 / 3])
    assert regex[~regex.iso].weight.unique().tolist() == pytest.approx([400 / 57])
    per_cell = k.groupby(["stratum", "iso"]).weight.sum()
    assert per_cell.to_dict() == pytest.approx(f.groupby(["stratum", "iso"]).size().astype(float).to_dict())
    assert vs.uncovered_cells(s, f) == {}


def test_uncovered_cells_are_reported():
    f = _frame()
    s = vs.draw(f, iso_cap={vs.REGEX: 0})
    assert vs.uncovered_cells(s, f) == {"regex ISO": 100}


def test_hand_fixed_window_keeps_the_regex_stratum():
    from types import SimpleNamespace as Row
    base = dict(text_status="OK", violation_start=dt.date(2010, 1, 1), window_kind="stated_period")
    assert vs.stratum(Row(**base, violation_method="hand", window_status="PARSED")) == vs.REGEX
    assert vs.stratum(Row(**base, violation_method="model", window_status="UNPARSED")) == vs.MODEL_STATED
    assert vs.stratum(Row(**{**base, "violation_start": None}, violation_method=None,
                          window_status="PARSED")) == vs.NO_WINDOW
    assert vs.stratum(Row(**{**base, "text_status": "NO_SOURCE"}, violation_method=None,
                          window_status=None)) is None


def test_orders_the_user_reviewed_with_the_machine_window_are_seen():
    assert vs.REPO_ROOT / "hand" / "window_review.csv" in vs.SEEN


def _repo(tmp_path, monkeypatch):
    """A throwaway repo root with one PDF hard-linked into raw/cache, as fetch_pdfs leaves them."""
    (tmp_path / "raw" / "cache").mkdir(parents=True)
    (tmp_path / "raw" / "pdfs").mkdir()
    (tmp_path / "derived" / "text").mkdir(parents=True)
    (tmp_path / "raw" / "cache" / "abc").write_bytes(b"%PDF-1.4 order one")
    os.link(tmp_path / "raw" / "cache" / "abc", tmp_path / "raw" / "pdfs" / "1.pdf")
    (tmp_path / "derived" / "text" / "2.txt").write_text("inline order two")
    monkeypatch.setattr(vs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(vs, "TEXT_DIR", tmp_path / "derived" / "text")
    return tmp_path


def test_copies_are_new_files_never_links_into_raw(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch)
    sample = pd.DataFrame({"text_stem": ["1", "2", "3"], "pdf_path": ["raw/pdfs/1.pdf", None, "raw/pdfs/3.pdf"]})
    out = root / "hand" / "validation_pdfs"
    names = vs.copy_files(sample, out)
    assert names == ["001_1.pdf", "002_2.txt", ""]  # 3 has no file: blank, not a failure
    copy, src = out / "001_1.pdf", root / "raw" / "pdfs" / "1.pdf"
    assert copy.read_bytes() == src.read_bytes()
    assert copy.stat().st_ino != src.stat().st_ino and copy.stat().st_nlink == 1
    copy.write_bytes(b"highlighted")  # a viewer saving annotations
    assert (root / "raw" / "cache" / "abc").read_bytes() == b"%PDF-1.4 order one"
    assert (out / "002_2.txt").read_text() == "inline order two"


def test_copies_refuse_to_land_in_raw(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch)
    monkeypatch.setattr("src.extract_text.PROTECTED_DIRS", (root / "raw" / "pdfs", root / "raw" / "cache"))
    sample = pd.DataFrame({"text_stem": ["1"], "pdf_path": ["raw/pdfs/1.pdf"]})
    with pytest.raises(ValueError):
        vs.copy_files(sample, root / "raw" / "pdfs")


def test_main_refuses_to_overwrite_a_coding_sheet(tmp_path, monkeypatch):
    _repo(tmp_path / "repo", monkeypatch)
    monkeypatch.setattr(vs, "load_frame", lambda *_a, **_k: _frame())
    monkeypatch.setattr(vs, "seen_stems", lambda *_a, **_k: set())
    assert vs.main(["--scratch", str(tmp_path)]) == 0
    sheet = tmp_path / "validation_sample.csv"
    sheet.write_text(sheet.read_text() + "\n# my coding")
    assert vs.main(["--scratch", str(tmp_path)]) == 1
    assert sheet.read_text().endswith("# my coding")
    assert vs.main(["--scratch", str(tmp_path), "--force"]) == 0


def test_main_refuses_while_annotated_copies_exist(tmp_path, monkeypatch):
    _repo(tmp_path / "repo", monkeypatch)
    monkeypatch.setattr(vs, "load_frame", lambda *_a, **_k: _frame())
    monkeypatch.setattr(vs, "seen_stems", lambda *_a, **_k: set())
    (tmp_path / "validation_pdfs").mkdir()
    (tmp_path / "validation_pdfs" / "007_99.pdf").write_bytes(b"my highlights")
    assert vs.main(["--scratch", str(tmp_path)]) == 1
    assert vs.main(["--scratch", str(tmp_path), "--force"]) == 0
    assert not (tmp_path / "validation_pdfs" / "007_99.pdf").exists()  # a stale copy from an earlier draw


def test_main_points_the_sheet_at_the_copies(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo", monkeypatch)
    f = _frame(n_regex=60, n_iso=0, n_stated=0, n_conduct=0, n_none=0)
    f["pdf_path"] = None
    f.loc[f.text_stem == "1", "pdf_path"] = "raw/pdfs/1.pdf"
    monkeypatch.setattr(vs, "load_frame", lambda *_a, **_k: f)
    monkeypatch.setattr(vs, "seen_stems", lambda *_a, **_k: set())
    out = root / "hand"
    assert vs.main(["--scratch", str(out)]) == 0
    sheet = pd.read_csv(out / "validation_sample.csv", dtype=str, keep_default_na=False)
    row = sheet[sheet.text_stem == "1"].iloc[0]
    assert row.local_file == f"hand/validation_pdfs/{int(row.sample_no):03d}_1.pdf"
    assert (root / row.local_file).is_file()
    assert (sheet.local_file == "").sum() == 58  # the throwaway repo holds only 1.pdf and 2.txt


# --- checking the coded sheet -------------------------------------------------------------------------------

def _row(**coded):
    r = {"sample_no": "1", "text_stem": "1", "order_date": "2020-06-30", "violation_start": "2018-04-01",
         "violation_end": "2019-03-31", "window_kind": "stated_period", "fraud_category": "MARKET_MANIPULATION",
         "detection_channel": "SURVEILLANCE", "detection_detail": "alert",
         "primary_issuer": "Kavveri Telecom Products Limited", "notes": ""}
    r.update(coded)
    return r


def _levels(r):
    return [level for level, _ in vs.check_row(r)]


def test_a_clean_row_passes():
    assert vs.check_row(_row()) == []
    assert vs.check_row(_row(violation_start="", violation_end="", window_kind="none",
                             detection_channel="UNCLEAR", detection_detail="none", primary_issuer="NONE")) == []


@pytest.mark.parametrize("value", ["05-11-2019", "11/5/2019", "2019-11-05 00:00:00", "2019-02-30", "Nov 2019"])
def test_dates_must_be_iso(value):
    msgs = vs.check_row(_row(violation_end=value))
    assert ("error" in [m[0] for m in msgs]) and any("YYYY-MM-DD" in m[1] for m in msgs)


def test_window_consistency():
    assert "error" in _levels(_row(violation_start="2019-05-01", violation_end="2019-04-30"))
    assert "error" in _levels(_row(window_kind="none"))  # dates with kind none
    assert "error" in _levels(_row(window_kind=""))      # dates without a kind
    assert _levels(_row(violation_end="")) == ["remind"]  # open end needs a note
    assert vs.check_row(_row(violation_end="", notes="till date; end not stated")) == []
    assert _levels(_row(violation_end="2021-01-31")) == ["remind"]  # after the order date
    assert _levels(_row(violation_start="1985-01-01")) == ["remind"]


def test_categories_and_channels_are_checked_with_a_suggestion():
    (level, msg), = vs.check_row(_row(fraud_category="Market manipulation"))
    assert level == "error" and "MARKET_MANIPULATION" in msg
    (level, msg), = vs.check_row(_row(detection_channel="SURVEILANCE"))
    assert level == "error" and "did you mean SURVEILLANCE" in msg
    (level, msg), = vs.check_row(_row(detection_detail="complaint"))  # a COMPLAINT detail
    assert level == "error" and "alert" in msg
    assert "error" in _levels(_row(detection_channel="", detection_detail="alert", notes="x"))
    assert _levels(_row(detection_detail="")) == ["remind"]


def test_other_needs_its_group_letter():
    assert _levels(_row(fraud_category="OTHER")) == ["remind"]
    for note in ("OTHER-c", "OTHER (a): summons", "? OTHER e — exemption order"):
        assert vs.check_row(_row(fraud_category="OTHER", notes=note)) == []


def test_blanks_need_a_note():
    msgs = vs.check_row(_row(primary_issuer=""))
    assert [m[0] for m in msgs] == ["remind"] and "NONE" in msgs[0][1]
    assert vs.check_row(_row(primary_issuer="", notes="issuer not named in the order")) == []


def test_check_counts_progress_and_catches_a_changed_sheet(tmp_path):
    rows = [_row(sample_no=str(i), text_stem=str(i)) for i in (1, 2)]
    rows[1].update({c: "" for c in vs.CODING_COLUMNS})       # not started
    rows.append(_row(sample_no="3", text_stem="3", notes="? two channels named"))
    path = tmp_path / "sheet.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    problems, stats = vs.check(path, stems={"1", "2", "3"})
    assert problems == [] and stats == {"rows": 3, "started": 2, "complete": 2, "doubts": 1,
                                        "errors": 0, "reminders": 0}
    problems, _ = vs.check(path, stems={"1", "2", "4"})
    assert problems[0][1] == "error" and "1 missing, 1 extra" in problems[0][2]


def test_check_reads_a_sheet_excel_saved_as_cp1252(tmp_path):
    path = tmp_path / "sheet.csv"
    pd.DataFrame([_row(primary_issuer="Société Générale")]).to_csv(path, index=False, encoding="cp1252")
    problems, stats = vs.check(path)
    assert stats["complete"] == 1 and problems[0][1] == "remind" and "cp1252" in problems[0][2]


def test_run_check_exit_code(tmp_path, capsys):
    path = tmp_path / "sheet.csv"
    pd.DataFrame([_row(violation_start="05-11-2019")]).to_csv(path, index=False)
    assert vs.run_check(path, tmp_path / "no_key.parquet") == 1
    assert "ERROR" in capsys.readouterr().out
    pd.DataFrame([_row()]).to_csv(path, index=False)
    assert vs.run_check(path, tmp_path / "no_key.parquet") == 0
