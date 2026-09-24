"""Tests for text extraction and the OCR fallback.

The three real-page fixtures are single pages cut from corpus PDFs:
  pdf_native.pdf        a 2026 digital order
  pdf_scanned.pdf       a 2010 scanned consent order (bleed-through, stamp)
  pdf_broken_layer.pdf  a 2013 AO order whose text layer is control codes
The last one is the case a character count gets wrong, so it is the most
important fixture here.
"""

import datetime as dt
import hashlib
import json
import os
import pathlib
import stat
from dataclasses import dataclass, field

import pyarrow.parquet as pq
import pymupdf
import pytest

from conftest import FIXTURES, FakeResponse, fixture_html
from src import cache
from src import extract_text as et

LOREM = ("Securities and Exchange Board of India conducted an investigation into the "
         "trading in the scrip of the company for the period from April 1, 2009 to "
         "August 12, 2009. The noticee was found to have violated the provisions of the "
         "regulations and a penalty is hereby imposed under section 15HA of the Act. ") * 4


@dataclass
class FakeEngine:
    signature: str = "fake-ocr 1.0|lang=eng|psm=3|dpi=72"
    dpi: int = 72
    text: str = "RECOGNISED TEXT FROM OCR"
    conf: float = 91.5
    calls: list = field(default_factory=list)
    fail: bool = False

    def ocr_png(self, png: bytes, dpi=None):
        self.calls.append(len(png))
        if self.fail:
            raise et.OcrError("engine exploded")
        return self.text, self.conf


@dataclass
class ScriptedEngine:
    """Returns one (text, conf) per call, in order: first pass, then the retry
    variants in OCR_RETRY_DPIS x OCR_RETRY_ROTATIONS order (upright base skipped)."""
    script: list
    signature: str = "scripted 1.0|dpi=300"
    dpi: int = 300
    calls: list = field(default_factory=list)

    def ocr_png(self, png: bytes, dpi=None):
        self.calls.append(dpi)
        return self.script[len(self.calls) - 1]


RETRY_ORDER = [f"r{rot}_{dpi}" for dpi in et.OCR_RETRY_DPIS for rot in et.OCR_RETRY_ROTATIONS
               if f"r{rot}_{dpi}" != "r0_300"]


def make_pdf(kinds: list[str]) -> bytes:
    """Synthetic PDF: 'text' (digital), 'image' (page-sized image, no text),
    'blank', 'vector' (hundreds of paths, no text: outlined lettering),
    'sideways' (a scan of a text page, rotated 90 degrees in the image)."""
    doc = pymupdf.open()
    for kind in kinds:
        page = doc.new_page()
        if kind == "text":
            page.insert_textbox(page.rect + (50, 50, -50, -50), LOREM, fontname="helv", fontsize=10)
        elif kind == "image":
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 280), 0)
            pix.clear_with(220)
            page.insert_image(page.rect, pixmap=pix)
        elif kind == "vector":
            # One path per "glyph", as in the real outlined order.
            shape = page.new_shape()
            for i in range(400):
                x, y = 60 + (i % 40) * 12, 80 + (i // 40) * 20
                shape.draw_line((x, y), (x + 8, y + 10))
                shape.finish(color=(0, 0, 0))
            shape.commit()
        elif kind == "upside_down":
            # A page scanned upside down. (Clean 90-degree text is not a useful
            # test: tesseract reads it unaided; it fails on the corpus's noisy
            # 1-bit sideways scans, which a synthetic page does not reproduce.)
            src = pymupdf.open()
            sp = src.new_page()
            sp.insert_textbox(sp.rect + (50, 50, -50, -50), LOREM, fontname="helv", fontsize=13)
            pix = sp.get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72).prerotate(180),
                                colorspace=pymupdf.csGRAY)
            src.close()
            page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def workspace(tmp_path):
    """A fake repo root with a pdf dir, text dir and OCR cache."""
    (tmp_path / "raw" / "pdfs").mkdir(parents=True)
    return tmp_path


def settings_for(root, engine=None):
    return et.Settings(text_dir=root / "text", ocr_dir=root / "ocr", engine=engine, repo_root=root)


def job_for(root, name, data=None, order_id=1, source=None):
    rel = f"raw/pdfs/{name}"
    path = root / rel
    if data is None:
        data = (FIXTURES / (source or name)).read_bytes()
    path.write_bytes(data)
    return et.Job(order_id=order_id, order_type="AO", order_date=dt.date(2013, 10, 24),
                  source_url=f"https://www.sebi.gov.in/x_{order_id}.html",
                  fetch_status="OK", doc_shape="IFRAME_PDF", pdf_path=rel)


def classify_fixture(name):
    doc = pymupdf.open(FIXTURES / name)
    page = doc[0]
    text = et.clean_page_text(page.get_text("text", flags=et.TEXT_FLAGS))
    n, q = et.text_quality(text)
    cover = et.image_cover(page)
    doc.close()
    return et.classify_page(n, q, cover), n, q, cover, text


# --- text quality and page classification ---------------------------------

def test_quality_of_clean_text_is_one():
    n, q = et.text_quality("Securities and Exchange Board of India, 1992.")
    assert n > 30 and q == 1.0


def test_quality_of_control_codes_is_near_zero():
    n, q = et.text_quality("\x01\x02\x03\x04\x02\x05\x06\x07\x08\x05\x0e\x0f\x10" * 20)
    assert n > 200 and q < 0.05


def test_quality_of_empty_text_is_not_corruption():
    assert et.text_quality("") == (0, 1.0)
    assert et.text_quality(" \n\t ") == (0, 1.0)


def test_hindi_and_zero_width_joiners_count_as_real_text():
    n, q = et.text_quality("भारतीय प्रतिभूति और विनिमय बोर्ड क्\u200d")
    assert q == 1.0


def test_private_use_and_replacement_chars_count_against_quality():
    _, q = et.text_quality("\ue001\ue002\ue003\ufffd" * 10 + "ok")
    assert q < 0.1


@pytest.mark.parametrize(
    "chars,quality,cover,expected",
    [
        (2000, 1.0, 0.0, et.NATIVE),
        (100, 1.0, 0.0, et.NATIVE),           # boundary: exactly MIN_CHARS
        (2000, 1.0, 1.0, et.NATIVE),          # searchable scan: trust its layer
        (2204, 0.03, 0.0, et.BROKEN_LAYER),   # the Ghostscript case
        (60, 0.1, 0.0, et.BROKEN_LAYER),      # short but garbage
        (99, 1.0, 0.9, et.SCANNED),
        (0, 1.0, 1.0, et.SCANNED),
        (0, 1.0, 0.0, et.SPARSE),             # blank page
        (40, 1.0, 0.1, et.SPARSE),            # "Sd/-" signature page with a small logo
        (10, 0.0, 0.0, et.SPARSE),            # a stray control code is not a layer
    ],
)
def test_classify_page(chars, quality, cover, expected):
    assert et.classify_page(chars, quality, cover) == expected


@pytest.mark.parametrize(
    "chars,cover,drawings,expected",
    [
        (0, 0.0, 1462, et.VECTOR_TEXT),       # order 37481: text drawn as outlines
        (0, 0.0, 300, et.VECTOR_TEXT),        # boundary
        (0, 0.0, 99, et.SPARSE),              # a letterhead rule and a logo
        (0, 0.9, 5000, et.SCANNED),           # an image-covered page stays SCANNED
        (500, 0.0, 5000, et.NATIVE),          # real text wins regardless of drawings
    ],
)
def test_classify_vector_text(chars, cover, drawings, expected):
    assert et.classify_page(chars, 1.0, cover, drawings) == expected


def test_real_broken_layer_is_not_mistaken_for_text():
    """REGRESSION: 2,204 characters, all garbage. A count alone says NATIVE."""
    page_class, n, q, _, text = classify_fixture("pdf_broken_layer.pdf")
    assert n > et.MIN_CHARS, "the layer is large — that is what makes it dangerous"
    assert q < et.MIN_QUALITY
    assert page_class == et.BROKEN_LAYER
    assert "ADJUDICATING" not in text.upper()


def test_real_scanned_page_is_classified_scanned():
    page_class, n, _, cover, _ = classify_fixture("pdf_scanned.pdf")
    assert page_class == et.SCANNED
    assert n < et.MIN_CHARS and cover >= et.MIN_IMAGE_COVER


def test_real_digital_page_is_native_and_readable():
    page_class, _, q, _, text = classify_fixture("pdf_native.pdf")
    assert page_class == et.NATIVE and q > 0.99
    assert "Kore Digital" in text


def test_ligatures_are_expanded():
    doc = pymupdf.open(FIXTURES / "pdf_native.pdf")
    text = doc[0].get_text("text", flags=et.TEXT_FLAGS)
    doc.close()
    assert not any(c in text for c in "\ufb00\ufb01\ufb02\ufb03\ufb04")


def test_stopword_ratio():
    assert et.stopword_ratio(LOREM) > 0.2
    assert et.stopword_ratio("x " * 10) is None                      # too short to judge
    assert et.stopword_ratio("qzv brkt mnpl " * 40) == 0.0


def test_clean_page_text_reserves_the_page_separator():
    assert et.clean_page_text("a\x00b\fc") == "ab\nc"


def test_non_breaking_spaces_become_spaces_so_regexes_match():
    text = et.clean_page_text("for the period from April 1, 2009 to May")
    assert text == "for the period from April 1, 2009 to\nMay"
    assert "period from" in text


def test_invisible_word_splitters_are_removed():
    assert et.clean_page_text("investi­gation") == "investigation"
    assert et.clean_page_text("Place :﻿Mumbai​") == "Place :Mumbai"


def test_devanagari_joiners_are_kept():
    text = "क्‍ष क्‌ष"
    assert et.clean_page_text(text) == text


@pytest.mark.parametrize("category", [
    "Orders of AO", "Orders of Chairman/Members", "Orders of Chairperson/Members",
    "Settlement Order", "Orders of ED / CGM (Quasi-Judicial Authorities)",
])
def test_inline_header_is_stripped(category):
    raw = f"Jan 11, 2005 | Orders : {category} SECURITIES AND EXCHANGE BOARD OF INDIA"
    assert et.clean_inline_text(raw) == "SECURITIES AND EXCHANGE BOARD OF INDIA"


def test_inline_text_without_the_header_is_left_alone():
    raw = "ORDER dated Jan 11, 2005 | Orders : Orders of AO appears later"
    assert et.clean_inline_text(raw) == raw


def test_inline_text_collapses_gaps_left_by_removed_characters():
    assert et.clean_inline_text("Officer ﻿ ﻿ ﻿ Place : Mumbai") == \
        "Officer Place : Mumbai"


# --- naming and safety -----------------------------------------------------

def test_text_stem_matches_pdf_archive_naming():
    assert et.text_stem(99913, "https://x/y.html") == "99913"
    stem = et.text_stem(None, "https://x/a.pdf")
    assert stem.startswith("noid_") and not stem.endswith(".pdf")


@pytest.mark.parametrize("rel", ["raw/pdfs", "raw/pdfs/123.pdf",
                                 "raw/pdfs/order_files_by_category/x.txt",
                                 "raw/cache", "raw/cache/abc"])
def test_refuses_to_write_into_protected_dirs(rel):
    with pytest.raises(ValueError):
        et.assert_safe_output(et.REPO_ROOT / rel)


@pytest.mark.parametrize("rel", ["derived/text", "derived/text/1.txt", "raw/ocr", "logs/x.log"])
def test_allows_normal_output_dirs(rel):
    et.assert_safe_output(et.REPO_ROOT / rel)


def test_write_atomic_refuses_protected_path():
    with pytest.raises(ValueError):
        et.write_atomic(et.REPO_ROOT / "raw" / "pdfs" / "evil.txt", b"x")


# --- tesseract plumbing ----------------------------------------------------

TSV = "\n".join([
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
    "1\t1\t0\t0\t0\t0\t0\t0\t2480\t3508\t-1\t",
    "2\t1\t1\t0\t0\t0\t10\t10\t100\t100\t-1\t",
    "3\t1\t1\t1\t0\t0\t10\t10\t100\t100\t-1\t",
    "4\t1\t1\t1\t1\t0\t10\t10\t100\t20\t-1\t",
    "5\t1\t1\t1\t1\t1\t10\t10\t40\t20\t96.0\tBEFORE",
    "5\t1\t1\t1\t1\t2\t60\t10\t40\t20\t90.0\tTHE",
    "5\t1\t1\t1\t2\t1\t10\t40\t40\t20\t80.0\tBOARD",
    "5\t1\t1\t2\t1\t1\t10\t90\t40\t20\t70.0\tSecond",
    "5\t1\t1\t2\t1\t2\t60\t90\t40\t20\t-1\tparagraph",
    "5\t1\t1\t2\t1\t3\t90\t90\t10\t20\t50.0\t ",
    "garbage line",
])


def test_parse_tesseract_tsv_rebuilds_lines_and_paragraphs():
    text, conf = et.parse_tesseract_tsv(TSV)
    assert text == "BEFORE THE\nBOARD\n\nSecond paragraph"
    # conf -1 ("paragraph") and the blank word (conf 50) are both excluded
    assert conf == pytest.approx((96 + 90 + 80 + 70) / 4)


def test_parse_tesseract_tsv_empty():
    assert et.parse_tesseract_tsv("") == ("", None)
    assert et.parse_tesseract_tsv(TSV.splitlines()[0]) == ("", None)


def test_tesseract_version_none_when_missing(monkeypatch):
    monkeypatch.setattr(et.shutil, "which", lambda _b: None)
    assert et.tesseract_version() is None


def _fake_binary(tmp_path, body):
    script = tmp_path / "fake-tesseract"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_tesseract_engine_subprocess_plumbing(tmp_path):
    """Exercise the real subprocess path with a stand-in binary."""
    record = tmp_path / "argv"
    tsv = TSV.replace("\n", "\\n").replace("\t", "\\t")
    binary = _fake_binary(tmp_path, f'echo "$@ OMP=$OMP_THREAD_LIMIT" > {record}\n'
                                    f"cat > /dev/null\nprintf '{tsv}\\n'\n")
    engine = et.TesseractEngine(version="tesseract 5.3.4", binary=binary, lang="eng", psm=3)
    text, conf = engine.ocr_png(b"\x89PNG fake")
    assert text.startswith("BEFORE THE")
    argv = record.read_text()
    assert "stdin stdout -l eng --psm 3 --dpi 300 tsv" in argv
    assert "OMP=1" in argv, "one OpenMP thread per tesseract when running in parallel"


def test_tesseract_engine_failure_raises_ocr_error(tmp_path):
    binary = _fake_binary(tmp_path, "cat > /dev/null\necho 'bad lang' >&2\nexit 1\n")
    engine = et.TesseractEngine(version="v", binary=binary)
    with pytest.raises(et.OcrError, match="bad lang"):
        engine.ocr_png(b"x")


def test_ocr_cache_key_changes_with_engine_settings(tmp_path):
    a = et.ocr_cache_path(tmp_path, "ab" * 32, 1, FakeEngine(signature="v1|dpi=300"))
    b = et.ocr_cache_path(tmp_path, "ab" * 32, 1, FakeEngine(signature="v1|dpi=400"))
    c = et.ocr_cache_path(tmp_path, "ab" * 32, 2, FakeEngine(signature="v1|dpi=300"))
    assert len({a, b, c}) == 3


# --- extract_pdf end to end ------------------------------------------------

def test_native_pdf_extracts_without_ocr(workspace):
    engine = FakeEngine()
    doc, pages = et.extract_pdf(job_for(workspace, "n.pdf", source="pdf_native.pdf"),
                                settings_for(workspace, engine))
    assert doc.status == et.OK and doc.source == et.SRC_PDF
    assert doc.has_text_layer is True and doc.n_native == 1
    assert engine.calls == []
    text = (workspace / doc.text_path).read_text(encoding="utf-8")
    assert "Kore Digital" in text
    assert doc.text_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert pages[0].method == et.M_NATIVE


def test_broken_layer_is_ocrd_not_kept(workspace):
    engine = FakeEngine(text="BEFORE THE ADJUDICATING OFFICER")
    doc, pages = et.extract_pdf(job_for(workspace, "b.pdf", source="pdf_broken_layer.pdf"),
                                settings_for(workspace, engine))
    assert pages[0].page_class == et.BROKEN_LAYER and pages[0].method == et.M_OCR
    assert doc.status == et.OK and doc.has_text_layer is False and doc.n_broken == 1
    assert (workspace / doc.text_path).read_text() == "BEFORE THE ADJUDICATING OFFICER"
    assert "\x01" not in (workspace / doc.text_path).read_text()


def test_scanned_pdf_without_engine_is_pending_and_writes_no_text(workspace):
    doc, pages = et.extract_pdf(job_for(workspace, "s.pdf", source="pdf_scanned.pdf"),
                                settings_for(workspace, None))
    assert doc.status == et.NEEDS_OCR
    assert doc.n_scanned == 1 and doc.n_ocr_pending == 1
    assert doc.text_path is None, "incomplete text must not be written"
    assert not (workspace / "text").exists()
    assert pages[0].method == et.M_OCR_PENDING


def test_scanned_pdf_with_engine_is_ocrd_and_cached(workspace):
    engine = FakeEngine()
    s = settings_for(workspace, engine)
    job = job_for(workspace, "s.pdf", source="pdf_scanned.pdf")
    doc, pages = et.extract_pdf(job, s)
    assert doc.status == et.OK and doc.n_ocr_done == 1
    assert doc.mean_ocr_conf == 91.5 and pages[0].ocr_conf == 91.5
    assert len(engine.calls) == 1 and engine.calls[0] > 0, "a rendered PNG was sent"
    cached = list((workspace / "ocr").rglob("*.json"))
    assert len(cached) == 1
    assert json.loads(cached[0].read_text())["engine"] == engine.signature

    doc2, _ = et.extract_pdf(job, s)
    assert len(engine.calls) == 1, "second run must come from the OCR cache"
    assert doc2.text_sha256 == doc.text_sha256


def test_changing_engine_settings_re_ocrs(workspace):
    job = job_for(workspace, "s.pdf", source="pdf_scanned.pdf")
    e1 = FakeEngine(signature="v1|dpi=300")
    e2 = FakeEngine(signature="v1|dpi=400", text="NEW")
    et.extract_pdf(job, settings_for(workspace, e1))
    doc, _ = et.extract_pdf(job, settings_for(workspace, e2))
    assert len(e2.calls) == 1
    assert (workspace / doc.text_path).read_text() == "NEW"


def test_ocr_failure_is_recorded_not_raised(workspace):
    doc, pages = et.extract_pdf(job_for(workspace, "s.pdf", source="pdf_scanned.pdf"),
                                settings_for(workspace, FakeEngine(fail=True)))
    assert doc.status == et.FAILED and "engine exploded" in doc.error
    assert doc.text_path is None and pages[0].method == et.M_OCR_FAILED


def test_mixed_document_pages_are_classified_individually(workspace):
    engine = FakeEngine(text="SCANNED ANNEXURE")
    data = make_pdf(["text", "image", "blank", "text"])
    doc, pages = et.extract_pdf(job_for(workspace, "m.pdf", data=data),
                                settings_for(workspace, engine))
    assert [p.page_class for p in pages] == [et.NATIVE, et.SCANNED, et.SPARSE, et.NATIVE]
    assert [p.method for p in pages] == [et.M_NATIVE, et.M_OCR, et.M_NATIVE, et.M_NATIVE]
    assert len(engine.calls) == 1, "only the scanned page is OCR'd"
    text = (workspace / doc.text_path).read_text()
    parts = text.split(et.PAGE_SEPARATOR)
    assert len(parts) == 4, "one form feed between each page"
    assert parts[1] == "SCANNED ANNEXURE" and "investigation" in parts[0]
    assert doc.has_text_layer is False
    assert (doc.n_native, doc.n_scanned, doc.n_sparse) == (2, 1, 1)


def test_vector_text_page_is_ocrd(workspace):
    """REGRESSION: order 37481 draws its text as paths; it came out empty."""
    engine = FakeEngine(text="BEFORE THE ADJUDICATING OFFICER")
    doc, pages = et.extract_pdf(job_for(workspace, "v.pdf", data=make_pdf(["vector", "blank"])),
                                settings_for(workspace, engine))
    assert [p.page_class for p in pages] == [et.VECTOR_TEXT, et.SPARSE]
    assert pages[0].drawings >= et.MIN_VECTOR_DRAWINGS and pages[0].method == et.M_OCR
    assert doc.n_vector == 1 and doc.has_text_layer is False and len(engine.calls) == 1
    assert (workspace / doc.text_path).read_text().startswith("BEFORE THE ADJUDICATING")


def test_confident_first_pass_is_not_retried(workspace):
    engine = ScriptedEngine(script=[("upright text", 95.0)])
    doc, pages = et.extract_pdf(job_for(workspace, "s.pdf", source="pdf_scanned.pdf"),
                                settings_for(workspace, engine))
    assert engine.calls == [300]
    assert pages[0].ocr_variant == "r0_300" and doc.n_ocr_rotated == 0


def test_low_confidence_page_is_retried_and_best_variant_wins(workspace):
    confs = {"r90_300": 55.0, "r90_200": 91.0, "r180_200": 60.0}
    script = [("garbage", 31.0)] + [(f"text {v}", confs.get(v, 10.0)) for v in RETRY_ORDER]
    engine = ScriptedEngine(script=script)
    s = settings_for(workspace, engine)
    job = job_for(workspace, "s.pdf", source="pdf_scanned.pdf")
    doc, pages = et.extract_pdf(job, s)
    assert len(engine.calls) == 1 + len(RETRY_ORDER), "first pass + every other variant"
    assert pages[0].ocr_variant == "r90_200" and pages[0].ocr_conf == 91.0
    assert (workspace / doc.text_path).read_text() == "text r90_200"
    assert doc.n_ocr_rotated == 1 and doc.mean_ocr_conf == 91.0
    assert sorted(set(engine.calls)) == sorted(set(et.OCR_RETRY_DPIS)), "dpi passed through"

    et.extract_pdf(job, s)
    assert len(engine.calls) == 1 + len(RETRY_ORDER), "both passes come from the cache"


def test_retry_that_does_no_better_keeps_the_first_pass(workspace):
    script = [("first pass", 40.0)] + [("worse", 12.0)] * len(RETRY_ORDER)
    doc, pages = et.extract_pdf(job_for(workspace, "s.pdf", source="pdf_scanned.pdf"),
                                settings_for(workspace, ScriptedEngine(script=script)))
    assert pages[0].ocr_variant == "r0_300" and pages[0].ocr_conf == 40.0
    assert (workspace / doc.text_path).read_text() == "first pass"


def test_first_pass_cache_written_before_variants_existed_is_reused(workspace):
    """The full run's 7,731 cached pages have no 'variant' key; they must not be re-OCR'd."""
    job = job_for(workspace, "s.pdf", source="pdf_scanned.pdf")
    sha = hashlib.sha256((workspace / job.pdf_path).read_bytes()).hexdigest()
    engine = ScriptedEngine(script=[])
    path = et.ocr_cache_path(workspace / "ocr", sha, 1, engine)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"text": "old cached text", "conf": 93.0, "engine": engine.signature}))
    doc, pages = et.extract_pdf(job, settings_for(workspace, engine))
    assert engine.calls == [] and pages[0].ocr_variant == "r0_300"
    assert (workspace / doc.text_path).read_text() == "old cached text"


@pytest.mark.skipif(et.tesseract_version() is None, reason="tesseract not installed")
def test_real_tesseract_recovers_an_upside_down_scan(workspace):
    """End to end with the real engine: the upright pass reads mirrored
    gibberish (~42% confidence) and the retry must find the 180-degree turn."""
    engine = et.TesseractEngine(version=et.tesseract_version())
    doc, pages = et.extract_pdf(job_for(workspace, "r.pdf", data=make_pdf(["upside_down"])),
                                settings_for(workspace, engine))
    text = (workspace / doc.text_path).read_text()
    assert pages[0].page_class == et.SCANNED
    assert pages[0].ocr_variant.startswith("r180_"), pages[0].ocr_variant
    assert pages[0].ocr_conf > 80 and doc.n_ocr_rotated == 1
    assert "Securities and Exchange Board of India" in text
    assert "investigation" in text


def test_source_pdf_is_never_modified(workspace):
    """PDFs are hard-linked to cache entries; a write would corrupt both."""
    job = job_for(workspace, "s.pdf", source="pdf_scanned.pdf")
    path = workspace / job.pdf_path
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    et.extract_pdf(job, settings_for(workspace, FakeEngine()))
    after = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    assert before == after


def test_corrupt_pdf_is_a_failure_not_a_crash(workspace):
    doc, pages = et.extract_pdf(job_for(workspace, "c.pdf", data=b"%PDF-1.4 garbage \x00\x01"),
                                settings_for(workspace))
    assert doc.status == et.FAILED and pages == []


def test_missing_pdf_is_a_failure(workspace):
    job = et.Job(1, "AO", None, "https://x/1.html", "OK", "IFRAME_PDF", "raw/pdfs/missing.pdf")
    doc, _ = et.extract_pdf(job, settings_for(workspace))
    assert doc.status == et.FAILED and "cannot read" in doc.error


# --- inline HTML and no-source orders --------------------------------------

def test_inline_html_order_reads_the_cache_offline(workspace, tmp_cache, fake_http):
    url = "https://www.sebi.gov.in/enforcement/orders/dec-2005/x_10125.html"
    fake_http.queue(FakeResponse(fixture_html("detail_inline_html.html").encode()))
    cache.get(url)                      # seed the cache
    calls_before = len(fake_http.calls)
    job = et.Job(10125, "AO", dt.date(2005, 12, 30), url, "OK_INLINE", "INLINE_HTML", None)
    doc, pages = et.extract_job(job, settings_for(workspace))
    assert len(fake_http.calls) == calls_before, "extraction must never touch the network"
    assert doc.status == et.OK and doc.source == et.SRC_INLINE and pages == []
    text = (workspace / doc.text_path).read_text()
    assert "ADJUDICATING OFFICER" in text.upper()
    assert not text.startswith("Dec 30, 2005 |"), "detail-page caption is stripped"
    assert "﻿" not in text and "  " not in text


def test_inline_html_not_in_cache_fails_without_network(workspace, tmp_cache, fake_http):
    job = et.Job(5, "AO", None, "https://www.sebi.gov.in/x_5.html", "OK_INLINE", "INLINE_HTML", None)
    doc, _ = et.extract_job(job, settings_for(workspace))
    assert doc.status == et.FAILED and fake_http.calls == []


def test_failed_fetch_becomes_no_source(workspace):
    job = et.Job(6, "WTM", None, "https://x/6.html", "FAILED_DETAIL", "DETAIL_FAILED", None)
    doc, _ = et.extract_job(job, settings_for(workspace))
    assert doc.status == et.NO_SOURCE and doc.source == et.SRC_NONE


# --- selection, input, output ---------------------------------------------

def _jobs(n_per_year=10, years=(2005, 2015, 2025)):
    return [et.Job(y * 100 + i, "AO" if i % 2 else "WTM", dt.date(y, 1, 1),
                   f"https://x/{y}_{i}.html", "OK", "IFRAME_PDF", f"raw/pdfs/{y}{i}.pdf")
            for y in years for i in range(n_per_year)]


def test_select_filters():
    jobs = _jobs()
    assert {j.order_type for j in et.select(jobs, categories=["AO"])} == {"AO"}
    assert [j.order_id for j in et.select(jobs, order_ids=[200501])] == [200501]
    assert len(et.select(jobs, limit=4)) == 4


def test_select_sample_is_year_stratified_and_reproducible():
    jobs = _jobs()
    picked = et.select(jobs, sample=6)
    assert len(picked) == 6
    assert sorted({j.order_date.year for j in picked}) == [2005, 2015, 2025]
    assert [j.order_id for j in picked] == [j.order_id for j in et.select(jobs, sample=6)]


def test_load_jobs_retries_a_half_written_file_then_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(et.time, "sleep", lambda _s: None)
    bad = tmp_path / "docs.parquet"
    bad.write_bytes(b"PAR1 truncated")
    with pytest.raises(RuntimeError, match="could not read"):
        et.load_jobs(bad, retries=3)


def test_write_outputs_schema_and_atomicity(workspace):
    doc, pages = et.extract_pdf(job_for(workspace, "n.pdf", source="pdf_native.pdf"),
                                settings_for(workspace))
    docs_path, pages_path = et.write_outputs([doc], pages, workspace / "out")
    assert pq.read_table(docs_path).schema.equals(et.DOCS_SCHEMA)
    assert pq.read_table(pages_path).schema.equals(et.PAGES_SCHEMA)
    assert not any(".tmp-" in p.name for p in (workspace / "out").iterdir())
    row = pq.read_table(docs_path).to_pylist()[0]
    assert row["status"] == et.OK and row["n_pages"] == 1 and row["has_text_layer"] is True


def test_write_outputs_refuses_protected_dir():
    with pytest.raises(ValueError):
        et.write_outputs([], [], et.REPO_ROOT / "raw" / "cache")


def test_run_in_process_and_in_pool_agree(workspace):
    jobs = [job_for(workspace, f"{i}.pdf", source=src, order_id=i)
            for i, src in enumerate(["pdf_native.pdf", "pdf_scanned.pdf",
                                     "pdf_broken_layer.pdf"], start=1)]
    s = settings_for(workspace, None)
    serial, _ = et.run(jobs, s, workers=1, progress_every=0)
    pooled, _ = et.run(jobs, s, workers=2, progress_every=0)
    key = lambda d: (d.order_id, d.status, d.n_native, d.n_scanned, d.n_broken, d.text_sha256)
    assert sorted(map(key, serial)) == sorted(map(key, pooled))
    assert {d.order_id: d.status for d in serial} == {1: et.OK, 2: et.NEEDS_OCR, 3: et.NEEDS_OCR}


def test_summarise_reports_the_day5_number(workspace):
    jobs = [job_for(workspace, f"{i}.pdf", source=src, order_id=i)
            for i, src in enumerate(["pdf_native.pdf", "pdf_native.pdf",
                                     "pdf_native.pdf", "pdf_scanned.pdf"], start=1)]
    docs, pages = et.run(jobs, settings_for(workspace, None), workers=1, progress_every=0)
    s = et.summarise(docs, pages)
    assert s["pdf_documents"] == 4 and s["pdf_docs_needing_ocr"] == 1
    assert s["pdf_docs_needing_ocr_share"] == 0.25
    assert s["ocr_pages"] == 1 and s["fully_scanned_docs"] == 1


# --- CLI -------------------------------------------------------------------

def test_cli_defaults():
    args = et.build_arg_parser().parse_args([])
    assert args.documents == et.DOCUMENTS_IN
    assert args.no_ocr is False and args.scratch is None and args.nice == 10
    assert args.workers >= 1


def test_cli_dry_run_invocation():
    args = et.build_arg_parser().parse_args(
        ["--sample", "300", "--no-ocr", "--scratch", "/tmp/x", "--workers", "4"])
    assert args.sample == 300 and args.no_ocr and args.scratch == pathlib.Path("/tmp/x")


def test_cli_rejects_excluded_category():
    with pytest.raises(SystemExit):
        et.build_arg_parser().parse_args(["--category", "SAT"])


def test_default_paths_are_wsl_native_and_outside_protected_dirs():
    for path in (et.TEXT_DIR, et.DERIVED_DIR, et.OCR_CACHE, et.DOCUMENTS_IN):
        text = str(path)
        assert text.startswith("/") and not text.startswith("/mnt/") and "\\" not in text
    for path in (et.TEXT_DIR, et.DERIVED_DIR, et.OCR_CACHE, et.LOGS_DIR):
        et.assert_safe_output(path)
