"""Stage 3: order text, from the PDF text layer with an OCR fallback.

Reads ``derived/order_documents.parquet`` and writes, per order,
``derived/text/<order_id>.txt`` (pages separated by form feed ``\\f``), plus

* ``derived/text_documents.parquet`` — one row per order: source, page counts
  by class, ``has_text_layer``, OCR status and confidence, output checksum;
* ``derived/text_pages.parquet``     — one row per PDF page: how its text was
  obtained and why, so every OCR decision can be audited later.

Page classification
-------------------
Each PDF page is judged on its own, because one document can mix a digital
body with a scanned annexure:

``NATIVE``        ≥ ``MIN_CHARS`` non-space characters of clean text: use the
                  text layer.
``BROKEN_LAYER``  a text layer exists but is mostly control / private-use
                  codes. Found in the corpus: Ghostscript 7.06 + PScript5 output
                  whose subset fonts carry no Unicode map, so page one of an
                  order yields 2,204 "characters" of ``\\x01\\x02\\x03…`` while
                  rendering perfectly. A character count alone calls this page
                  fine; it has to be judged on quality, then OCR'd.
``SCANNED``       fewer than ``MIN_CHARS`` characters and an image covering at
                  least half the page: OCR.
``VECTOR_TEXT``   few characters, no page-sized image, but hundreds of vector
                  paths: text drawn as outlines (one 2018 order). OCR.
``SPARSE``        few characters and no page-sized image — blank, signature and
                  "Sd/-" pages. OCR would add nothing, so the native text stays.

``MIN_CHARS`` = 100 follows the runbook's "a page yielding under ~100
characters is effectively a scan", refined by the image test so short digital
pages are not sent to OCR.

Inline-HTML orders (no PDF; the order text is the detail page itself) are read
from the HTTP cache in offline mode — this stage never touches the network.

Text is otherwise kept as extracted, with one normalisation that changes no
meaning: non-breaking and other space-like characters become plain spaces, and
invisible characters that split words (BOM, zero-width space, soft hyphen) are
removed — see ``clean_page_text``. Running headers and footers are *not*
removed; pymupdf emits them where the PDF draws them, often at the top of a
page's text, and pages are ``\\f``-separated so a later stage can strip lines
that repeat on every page.

OCR
---
Pages are rendered with pymupdf and piped to the ``tesseract`` binary, which
must be installed separately (``sudo apt install tesseract-ocr``); there is no
Python OCR dependency. Results are cached under ``raw/ocr/`` keyed by the PDF's
SHA-256, the page number, and the engine version + settings, so re-running
never repeats OCR and changing a setting re-OCRs cleanly. If tesseract is
absent, OCR pages are recorded as pending and the order's status is
``NEEDS_OCR``; re-running after installing it fills them in.

A page that OCRs badly upright (mean word confidence under 70) is retried at
every rotation and at 200 as well as 300 dpi, keeping the most confident
reading; ``text_pages.ocr_variant`` records which won. In the full corpus this
rescued sideways and upside-down scans (tesseract does not rotate pages at
psm 3) and 1-bit scans whose thin strokes blur when upsampled.

Safety
------
Source PDFs are hard-linked to HTTP cache entries (see ``fetch_pdfs``), so a
write to one would corrupt the other. PDFs are only ever opened read-only, and
every output location is checked against ``raw/pdfs`` and ``raw/cache``.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import hashlib
import json
import logging
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf

from src import cache
from src.fetch_pdfs import archive_name, extract_inline_text

log = logging.getLogger("extract_text")

REPO_ROOT = cache.REPO_ROOT
DOCUMENTS_IN = REPO_ROOT / "derived" / "order_documents.parquet"
DERIVED_DIR = REPO_ROOT / "derived"
TEXT_DIR = DERIVED_DIR / "text"
OCR_CACHE = REPO_ROOT / "raw" / "ocr"
LOGS_DIR = REPO_ROOT / "logs"
PROTECTED_DIRS = (REPO_ROOT / "raw" / "pdfs", REPO_ROOT / "raw" / "cache")

MIN_CHARS = 100
#: Share of a page's non-space characters that must be real text. Clean pages
#: sit at ~1.0 and the broken Ghostscript layers near 0, so the cut is not
#: delicate.
MIN_QUALITY = 0.75
#: Non-space characters needed before a low-quality layer counts as broken
#: (a stray control code on an otherwise empty page is not a text layer).
MIN_BROKEN_CHARS = 20
MIN_IMAGE_COVER = 0.5
#: Vector paths on an otherwise empty page before it counts as text drawn as
#: outlines. Across the corpus's 282 sparse pages, 276 have <= 100 paths; the
#: next value up is ~500, on the six pages of an order whose text is outlined.
MIN_VECTOR_DRAWINGS = 300

#: Pages whose first OCR pass scores below this mean word confidence are
#: re-OCR'd at every rotation and at a second resolution, keeping the best.
#: On the full corpus this rescued sideways/upside-down scans and 1-bit scans
#: that upsampling blurs: pages under 70 fell from 207 to 25.
OCR_RETRY_BELOW = 70.0
OCR_RETRY_ROTATIONS = (0, 90, 180, 270)
OCR_RETRY_DPIS = (300, 200)
OCR_RETRY_TAG = "retry-v1"

#: Expand ligatures (ﬁ -> fi) so later regexes match "financial"; keep
#: whitespace; clip to the page. Deliberately no dehyphenation — it would glue
#: genuine hyphens at line ends ("front-" + "running" -> "frontrunning").
TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP

PAGE_SEPARATOR = "\f"

# Page classes
NATIVE = "NATIVE"
BROKEN_LAYER = "BROKEN_LAYER"
SCANNED = "SCANNED"
SPARSE = "SPARSE"
VECTOR_TEXT = "VECTOR_TEXT"
NEEDS_OCR_CLASSES = frozenset({BROKEN_LAYER, SCANNED, VECTOR_TEXT})

# How a page's final text was obtained
M_NATIVE = "native"
M_OCR = "ocr"
M_OCR_PENDING = "ocr_pending"
M_OCR_FAILED = "ocr_failed"

# Document sources and statuses
SRC_PDF = "PDF"
SRC_INLINE = "INLINE_HTML"
SRC_NONE = "NONE"
OK = "OK"
NEEDS_OCR = "NEEDS_OCR"
NO_SOURCE = "NO_SOURCE"
FAILED = "FAILED"

_BAD_CATEGORIES = frozenset({"Cc", "Cs", "Co", "Cn"})  # Cf (ZWJ, soft hyphen) is legitimate
_WORD = re.compile(r"[a-z]{2,}")
_STOPWORDS = frozenset(
    "the of and to in is that for on by as be with was which has this from or at an "
    "are not it have had shall said such any been were under his her their its".split()
)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def text_quality(text: str) -> tuple[int, float]:
    """``(non-space characters, share of them that are real text)``.

    Control, surrogate, private-use and unassigned code points and U+FFFD count
    against quality. Empty text scores 1.0 — absence is not corruption.

    Args:
        text (str): a page's native text.
    Returns:
        tuple[int, float]: (non-space characters, share of them that are real text, 0–1).
    """
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0, 1.0
    bad = sum(1 for c in chars if c == "�" or unicodedata.category(c) in _BAD_CATEGORIES)
    return len(chars), 1.0 - bad / len(chars)


def classify_page(native_chars: int, quality: float, image_cover: float,
                  drawings: int = 0) -> str:
    """Decide how a page's text must be obtained (see the module docstring's table).

    Args:
        native_chars (int): non-space characters in the text layer.
        quality (float): share of them that are real text (text_quality).
        image_cover (float): share of the page covered by images, 0–1.
        drawings (int): vector paths on the page (counted only for would-be sparse pages).
    Returns:
        str: NATIVE, BROKEN_LAYER, SCANNED, VECTOR_TEXT or SPARSE.
    """
    if native_chars >= MIN_BROKEN_CHARS and quality < MIN_QUALITY:
        return BROKEN_LAYER
    if native_chars >= MIN_CHARS:
        return NATIVE
    if image_cover >= MIN_IMAGE_COVER:
        return SCANNED
    if drawings >= MIN_VECTOR_DRAWINGS:
        return VECTOR_TEXT
    return SPARSE


def stopword_ratio(text: str) -> float | None:
    """Share of word tokens that are English function words. Prose sits around
    0.3–0.45; garbled text near 0. A diagnostic, never a gate.

    Args:
        text (str): an order's text.
    Returns:
        float | None: share of words that are English function words; None under 50 words.
    """
    words = _WORD.findall(text.lower())
    if len(words) < 50:
        return None
    return sum(w in _STOPWORDS for w in words) / len(words)


def image_cover(page: pymupdf.Page) -> float:
    """Share of the page area covered by images (overlaps counted twice, capped at 1).

    Args:
        page (pymupdf.Page): an open PDF page.
    Returns:
        float: 0–1.
    """
    area = page.rect.width * page.rect.height
    if area <= 0:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return min(covered / area, 1.0)


#: Invisible characters that sit between letters and silently break regexes
#: ("investi­gation", "Place :﻿Mumbai"). Removing them loses nothing.
#: ZWJ/ZWNJ (U+200C/U+200D) are deliberately kept: Devanagari needs them.
_DROP = dict.fromkeys(map(ord, "\x00﻿​⁠­"))
#: Space-like characters mapped to a plain space or newline. Word-generated
#: PDFs are full of U+00A0 (66,647 of them across 39 of 839 sampled orders),
#: and a pattern with a literal space does not match a non-breaking one.
_SPACES = {0x00A0: " ", 0x2007: " ", 0x202F: " ", 0x2028: "\n", 0x2029: "\n"}


def clean_page_text(text: str) -> str:
    """Normalise invisible and space-like characters; reserve form feed as the
    page separator. Nothing that carries meaning is changed.

    Args:
        text (str): raw page text.
    Returns:
        str: the text with invisible characters removed, space-like ones made plain, form feeds made newlines.
    """
    return text.translate(_DROP).translate(_SPACES).replace(PAGE_SEPARATOR, "\n")


#: The detail page's own caption ("Jan 11, 2005 | Orders : Orders of AO"),
#: which sits ahead of an inline order's text. Anchored and specific: if it
#: does not match exactly, the text is left alone.
_INLINE_HEADER = re.compile(
    r"^[A-Z][a-z]{2} \d{1,2}, \d{4} \| Orders : "
    r"(?:Orders of AO|Orders of Chairman/Members|Orders of Chairperson/Members|"
    r"Settlement Orders?|Orders of ED / CGM \(Quasi-Judicial Authorities\)) "
)


def clean_inline_text(text: str) -> str:
    """Clean an inline-HTML order's text and drop the detail page's caption.

    Args:
        text (str): text from ``section.main_section``.
    Returns:
        str: the order's text.
    """
    text = re.sub(r"[ \t]+", " ", clean_page_text(text)).strip()
    return _INLINE_HEADER.sub("", text, count=1)


def text_stem(order_id: int | None, source_url: str) -> str:
    """Same naming as the PDF archive: ``<order_id>`` or ``noid_<hash>``.

    Args:
        order_id (int | None): SEBI's id.
        source_url (str): the listing URL (names id-less orders).
    Returns:
        str: ``"<order_id>"`` or ``"noid_<12 hex>"`` — the key every derived table joins on.
    """
    return archive_name(order_id, source_url).removesuffix(".pdf")


def assert_safe_output(path: pathlib.Path) -> None:
    """Refuse to write anywhere under raw/pdfs or raw/cache.

    Args:
        path (pathlib.Path): a path about to be written.
    Returns:
        None.
    Raises:
        ValueError: the path is inside raw/pdfs or raw/cache.
    """
    resolved = path.resolve()
    for protected in PROTECTED_DIRS:
        prot = protected.resolve()
        if resolved == prot or prot in resolved.parents:
            raise ValueError(f"refusing to write under {protected}: {path}")


def write_atomic(path: pathlib.Path, data: bytes) -> None:
    """Write bytes through a temporary file and a rename (refuses raw/pdfs and raw/cache).

    Args:
        path (pathlib.Path): destination.
        data (bytes): content.
    Returns:
        None.
    """
    assert_safe_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp-{os.urandom(6).hex()}"
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        if tmp.exists():  # pragma: no cover
            tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------

class OcrError(RuntimeError):
    """Tesseract failed or timed out on a page.

    Raised by:
        TesseractEngine.ocr_png.
    """
    pass


class OcrEngine(Protocol):
    """What an OCR engine must provide (TesseractEngine; tests pass a fake).

    Attributes:
        signature (str): engine + version + settings; part of the OCR cache key.
        dpi (int): the default render resolution.
    """
    #: Identifies engine + version + settings; part of the OCR cache key.
    signature: str
    dpi: int

    def ocr_png(self, png: bytes, dpi: int | None = None) -> tuple[str, float | None]:
        """OCR one rendered page.

        Args:
            png (bytes): the page rendered as a PNG.
            dpi (int | None): the resolution it was rendered at (None = the engine's dpi).
        Returns:
            tuple[str, float | None]: (text, mean word confidence 0–100, or None).
        """


def parse_tesseract_tsv(tsv: str) -> tuple[str, float | None]:
    """Rebuild text and mean word confidence from tesseract's TSV output.

    Lines are grouped by (block, paragraph, line); a blank line separates
    paragraphs. Confidence is the mean over words with conf >= 0.

    Args:
        tsv (str): tesseract's TSV output.
    Returns:
        tuple[str, float | None]: (the page text, mean word confidence 0–100 or None).
    """
    lines: dict[tuple[int, int, int], list[str]] = {}
    order: list[tuple[int, int, int]] = []
    confs: list[float] = []
    for raw in tsv.splitlines()[1:]:
        cols = raw.split("\t")
        if len(cols) < 11 or cols[0] != "5":
            continue
        word = cols[11].strip() if len(cols) > 11 else ""
        if not word:
            continue
        key = (int(cols[2]), int(cols[3]), int(cols[4]))
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(word)
        try:
            conf = float(cols[10])
        except ValueError:
            conf = -1.0
        if conf >= 0:
            confs.append(conf)

    out: list[str] = []
    previous_par = None
    for key in order:
        par = key[:2]
        if previous_par is not None and par != previous_par:
            out.append("")
        out.append(" ".join(lines[key]))
        previous_par = par
    text = "\n".join(out)
    return text, (sum(confs) / len(confs) if confs else None)


def tesseract_version(binary: str = "tesseract") -> str | None:
    """The installed tesseract's version line.

    Args:
        binary (str): the executable's name.
    Returns:
        str | None: e.g. "tesseract 5.5.0", or None if it is not installed.
    """
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    first = (proc.stdout or proc.stderr).decode("utf-8", "replace").splitlines()
    return first[0].strip() if first else None


@dataclass(frozen=True)
class TesseractEngine:
    """The tesseract command-line OCR engine and its settings.

    Attributes:
        version (str): from tesseract_version(); lang (str), dpi (int), psm (int): OCR settings;
        binary (str): the executable; timeout (int): seconds per page.
    """
    version: str
    lang: str = "eng"
    dpi: int = 300
    psm: int = 3
    binary: str = "tesseract"
    timeout: int = 300

    @property
    def signature(self) -> str:
        """The engine's identity for the OCR cache key.

        Returns:
            str: "<version>|lang=…|psm=…|dpi=…".
        """
        return f"{self.version}|lang={self.lang}|psm={self.psm}|dpi={self.dpi}"

    def ocr_png(self, png: bytes, dpi: int | None = None) -> tuple[str, float | None]:
        """``dpi`` is the resolution the PNG was rendered at (default: ``self.dpi``).

        Args:
            png (bytes): a rendered page.
            dpi (int | None): the resolution it was rendered at.
        Returns:
            tuple[str, float | None]: (text, mean word confidence).
        Raises:
            OcrError: tesseract failed or timed out.
        """
        # One thread per tesseract: we parallelise across documents instead,
        # and OpenMP inside each process would oversubscribe the CPUs.
        env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
        cmd = [self.binary, "stdin", "stdout", "-l", self.lang,
               "--psm", str(self.psm), "--dpi", str(dpi or self.dpi), "tsv"]
        try:
            proc = subprocess.run(cmd, input=png, capture_output=True,
                                  timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise OcrError(f"tesseract timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            raise OcrError(proc.stderr.decode("utf-8", "replace").strip()[:300])
        return parse_tesseract_tsv(proc.stdout.decode("utf-8", "replace"))


def ocr_cache_path(ocr_dir: pathlib.Path, pdf_sha256: str, page_no: int,
                   engine: OcrEngine, tag: str = "") -> pathlib.Path:
    """Where a page's OCR result is cached: ``raw/ocr/<sha[:2]>/<sha>/p0001-<config>.json``.

    Args:
        ocr_dir (pathlib.Path): the OCR cache root.
        pdf_sha256 (str): the PDF's content hash.
        page_no (int): 1-based page.
        engine (OcrEngine): the engine (its signature is part of the key).
        tag (str): "" for the first pass, OCR_RETRY_TAG for the rotation/dpi retry.
    Returns:
        pathlib.Path: the JSON file's path.
    """
    key = engine.signature + (f"|{tag}" if tag else "")
    config = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return ocr_dir / pdf_sha256[:2] / pdf_sha256 / f"p{page_no:04d}-{config}.json"


def _render(page: pymupdf.Page, dpi: int, rotation: int = 0) -> bytes:
    """Render a page to a grayscale PNG.

    Args:
        page (pymupdf.Page): the page.
        dpi (int): resolution.
        rotation (int): degrees, 0/90/180/270.
    Returns:
        bytes: the PNG.
    """
    matrix = pymupdf.Matrix(dpi / 72, dpi / 72).prerotate(rotation)
    return page.get_pixmap(matrix=matrix, colorspace=pymupdf.csGRAY).tobytes("png")


def _cached_ocr(path: pathlib.Path, compute) -> dict:
    """Read an OCR result from its cache file, or compute and write it.

    Args:
        path (pathlib.Path): the cache file.
        compute (Callable[[], dict]): produces the result on a miss.
    Returns:
        dict: text, conf, variant, engine, page, pdf_sha256 (and tag for retries).
    """
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    payload = compute()
    write_atomic(path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return payload


def _conf(value: float | None) -> float:
    """A confidence for comparisons: None counts as -1.

    Args:
        value (float | None): a confidence.
    Returns:
        float: the value, or -1.0.
    """
    return -1.0 if value is None else value


def ocr_page(page: pymupdf.Page, page_no: int, pdf_sha256: str, engine: OcrEngine,
             ocr_dir: pathlib.Path) -> tuple[str, float | None, str]:
    """OCR one page, from the OCR cache when possible.

    Returns ``(text, mean word confidence, variant)``, where variant names the
    rendering that won, e.g. ``r0_300`` (upright, 300 dpi) or ``r90_200``.

    A first pass renders the page upright at the engine's dpi. If that scores
    below ``OCR_RETRY_BELOW`` — in this corpus, almost always a page scanned
    sideways or upside down, or a 1-bit scan that upsampling blurs — every
    rotation is tried at every ``OCR_RETRY_DPIS`` resolution and the most
    confident reading wins. Both passes are cached separately, so a retry never
    invalidates first-pass results.

    Args:
        page (pymupdf.Page): the page.
        page_no (int): 1-based page number.
        pdf_sha256 (str): the PDF's content hash (cache key).
        engine (OcrEngine): the OCR engine.
        ocr_dir (pathlib.Path): the OCR cache root.
    Returns:
        tuple[str, float | None, str]: (text, mean word confidence, winning variant such as "r90_200").
    """
    base_variant = f"r0_{engine.dpi}"

    def first_pass() -> dict:
        """Upright OCR at the engine's dpi.

        Returns:
            dict: the cache record for the first pass.
        """
        text, conf = engine.ocr_png(_render(page, engine.dpi), engine.dpi)
        return {"text": text, "conf": conf, "variant": base_variant,
                "engine": engine.signature, "page": page_no, "pdf_sha256": pdf_sha256}

    base = _cached_ocr(ocr_cache_path(ocr_dir, pdf_sha256, page_no, engine), first_pass)
    base.setdefault("variant", base_variant)  # entries written before variants existed
    if _conf(base["conf"]) >= OCR_RETRY_BELOW:
        return base["text"], base["conf"], base["variant"]

    def retry_pass() -> dict:
        """Every rotation at every retry dpi; keeps the most confident reading.

        Returns:
            dict: the cache record for the best retry variant.
        """
        best = {"text": "", "conf": None, "variant": None}
        for dpi in OCR_RETRY_DPIS:
            for rotation in OCR_RETRY_ROTATIONS:
                variant = f"r{rotation}_{dpi}"
                if variant == base_variant:
                    continue
                text, conf = engine.ocr_png(_render(page, dpi, rotation), dpi)
                if _conf(conf) > _conf(best["conf"]):
                    best = {"text": text, "conf": conf, "variant": variant}
        return {**best, "engine": engine.signature, "tag": OCR_RETRY_TAG,
                "page": page_no, "pdf_sha256": pdf_sha256}

    retry = _cached_ocr(ocr_cache_path(ocr_dir, pdf_sha256, page_no, engine, OCR_RETRY_TAG),
                        retry_pass)
    winner = retry if _conf(retry["conf"]) > _conf(base["conf"]) else base
    return winner["text"], winner["conf"], winner["variant"]


# --------------------------------------------------------------------------
# Per-document extraction
# --------------------------------------------------------------------------

@dataclass
class Job:
    """One order to extract: the fields stage 3 needs from ``order_documents``.

    Attributes:
        order_id (int | None), order_type (str), order_date (datetime.date | None), source_url (str),
        fetch_status (str): stage 2's status (OK, OK_INLINE …), doc_shape (str),
        pdf_path (str | None): repo-relative path of the archived PDF.
    """
    order_id: int | None
    order_type: str
    order_date: dt.date | None
    source_url: str
    fetch_status: str
    doc_shape: str
    pdf_path: str | None


@dataclass
class PageResult:
    """One PDF page: a row of ``derived/text_pages.parquet``.

    Attributes:
        order_id (int | None), text_stem (str), page_no (int), page_class (str), method (str): native, ocr,
        ocr_pending, ocr_failed; native_chars (int), quality (float), image_cover (float),
        ocr_conf (float | None), out_chars (int), drawings (int | None), ocr_variant (str | None).
    """
    order_id: int | None
    text_stem: str
    page_no: int
    page_class: str
    method: str
    native_chars: int
    quality: float
    image_cover: float
    ocr_conf: float | None
    out_chars: int
    drawings: int | None = None
    ocr_variant: str | None = None


@dataclass
class DocResult:
    """One order: a row of ``derived/text_documents.parquet`` (see CLAUDE.md §8 for every column).

    Attributes:
        text_stem (str): the join key; source (str): PDF, INLINE_HTML or NONE;
        n_pages and the per-class page counts (int); has_text_layer (bool | None);
        mean_ocr_conf (float | None); text_path (str | None), text_chars (int | None),
        text_sha256 (str | None), stopword_ratio (float | None);
        status (str): OK, NEEDS_OCR, NO_SOURCE or FAILED; error (str | None); extracted_at (datetime).
    """
    order_id: int | None
    order_type: str
    order_date: dt.date | None
    text_stem: str
    source: str
    fetch_status: str
    pdf_path: str | None
    pdf_sha256: str | None
    n_pages: int | None
    n_native: int = 0
    n_sparse: int = 0
    n_scanned: int = 0
    n_broken: int = 0
    n_vector: int = 0
    n_ocr_done: int = 0
    n_ocr_rotated: int = 0
    n_ocr_pending: int = 0
    n_ocr_failed: int = 0
    has_text_layer: bool | None = None
    mean_ocr_conf: float | None = None
    text_path: str | None = None
    text_chars: int | None = None
    text_sha256: str | None = None
    stopword_ratio: float | None = None
    status: str = OK
    error: str | None = None
    extracted_at: dt.datetime | None = None


@dataclass(frozen=True)
class Settings:
    """Where a run writes, and with which OCR engine.

    Attributes:
        text_dir (pathlib.Path): the .txt files; ocr_dir (pathlib.Path): the OCR cache;
        engine (TesseractEngine | None): None leaves OCR pages pending;
        repo_root (pathlib.Path): base for repo-relative paths.
    """
    text_dir: pathlib.Path
    ocr_dir: pathlib.Path
    engine: TesseractEngine | None = None
    repo_root: pathlib.Path = REPO_ROOT


def _relative(path: pathlib.Path, root: pathlib.Path) -> str:
    """A path relative to the repo when possible (for the tables), else absolute.

    Args:
        path (pathlib.Path): any path.
        root (pathlib.Path): the repo root.
    Returns:
        str: the relative (or absolute) path.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _write_text(settings: Settings, stem: str, text: str) -> tuple[str, int, str]:
    """Write an order's text file atomically.

    Args:
        settings (Settings): where to write.
        stem (str): the text stem (file name).
        text (str): the full text, pages joined by form feed.
    Returns:
        tuple[str, int, str]: (repo-relative path, characters, SHA-256 of the UTF-8 bytes).
    """
    path = settings.text_dir / f"{stem}.txt"
    data = text.encode("utf-8")
    write_atomic(path, data)
    return _relative(path, settings.repo_root), len(text), hashlib.sha256(data).hexdigest()


def extract_pdf(job: Job, settings: Settings) -> tuple[DocResult, list[PageResult]]:
    """Extract one PDF order page by page: classify, OCR where needed, write the text.

    Args:
        job (Job): the order (job.pdf_path must be set).
        settings (Settings): output locations and the OCR engine.
    Returns:
        tuple[DocResult, list[PageResult]]: the order row and one row per page. No text file is written
        when any page is still pending or failed.
    """
    stem = text_stem(job.order_id, job.source_url)
    result = DocResult(
        order_id=job.order_id, order_type=job.order_type, order_date=job.order_date,
        text_stem=stem, source=SRC_PDF, fetch_status=job.fetch_status,
        pdf_path=job.pdf_path, pdf_sha256=None, n_pages=None,
        extracted_at=dt.datetime.now(dt.timezone.utc),
    )
    pdf = settings.repo_root / job.pdf_path
    try:
        data = pdf.read_bytes()
    except OSError as exc:
        result.status, result.error = FAILED, f"cannot read PDF: {exc}"
        return result, []
    result.pdf_sha256 = hashlib.sha256(data).hexdigest()

    try:
        # Opened from bytes: the file on disk is never handed to a writer.
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # pymupdf raises several types for damaged files
        result.status, result.error = FAILED, f"cannot open PDF: {exc}"[:300]
        return result, []

    pages: list[PageResult] = []
    texts: list[str] = []
    confs: list[float] = []
    try:
        if doc.needs_pass and not doc.authenticate(""):
            result.status, result.error = FAILED, "password-protected PDF"
            return result, []
        result.n_pages = doc.page_count
        for index in range(doc.page_count):
            page = doc[index]
            page_no = index + 1
            native = clean_page_text(page.get_text("text", flags=TEXT_FLAGS))
            n_chars, quality = text_quality(native)
            cover = image_cover(page)
            # Vector paths only matter for pages that would otherwise be SPARSE,
            # and counting them is the slow part, so only count those.
            drawings = None
            if n_chars < MIN_CHARS and cover < MIN_IMAGE_COVER and quality >= MIN_QUALITY:
                drawings = len(page.get_drawings())
            page_class = classify_page(n_chars, quality, cover, drawings or 0)

            method, text, conf, variant = M_NATIVE, native, None, None
            if page_class in NEEDS_OCR_CLASSES:
                if settings.engine is None:
                    method, text = M_OCR_PENDING, ""
                else:
                    try:
                        text, conf, variant = ocr_page(page, page_no, result.pdf_sha256,
                                                       settings.engine, settings.ocr_dir)
                        method = M_OCR
                        text = clean_page_text(text)
                    except OcrError as exc:
                        method, text = M_OCR_FAILED, ""
                        result.error = f"page {page_no}: {exc}"[:300]

            counter = {NATIVE: "n_native", SPARSE: "n_sparse", SCANNED: "n_scanned",
                       BROKEN_LAYER: "n_broken", VECTOR_TEXT: "n_vector"}[page_class]
            setattr(result, counter, getattr(result, counter) + 1)
            if method == M_OCR:
                result.n_ocr_done += 1
                if variant and not variant.startswith("r0_"):
                    result.n_ocr_rotated += 1
                if conf is not None:
                    confs.append(conf)
            elif method == M_OCR_PENDING:
                result.n_ocr_pending += 1
            elif method == M_OCR_FAILED:
                result.n_ocr_failed += 1

            texts.append(text)
            pages.append(PageResult(
                order_id=job.order_id, text_stem=stem, page_no=page_no,
                page_class=page_class, method=method, native_chars=n_chars,
                quality=round(quality, 4), image_cover=round(cover, 4),
                ocr_conf=None if conf is None else round(conf, 2),
                out_chars=len(text), drawings=drawings, ocr_variant=variant,
            ))
    finally:
        doc.close()

    result.has_text_layer = result.n_scanned + result.n_broken + result.n_vector == 0
    result.mean_ocr_conf = round(sum(confs) / len(confs), 2) if confs else None
    if result.n_ocr_failed:
        result.status = FAILED
        return result, pages
    if result.n_ocr_pending:
        # Incomplete text is not written: nothing downstream should mistake a
        # half-extracted order for a whole one.
        result.status = NEEDS_OCR
        return result, pages

    full = PAGE_SEPARATOR.join(texts)
    result.text_path, result.text_chars, result.text_sha256 = _write_text(settings, stem, full)
    result.stopword_ratio = stopword_ratio(full)
    if result.text_chars == 0:
        result.status, result.error = FAILED, "no text recovered from any page"
    return result, pages


def extract_inline(job: Job, settings: Settings) -> tuple[DocResult, list[PageResult]]:
    """Extract an inline-HTML order from its cached detail page (offline).

    Args:
        job (Job): the order (fetch_status OK_INLINE).
        settings (Settings): output locations.
    Returns:
        tuple[DocResult, list[PageResult]]: the order row, and no page rows.
    """
    stem = text_stem(job.order_id, job.source_url)
    result = DocResult(
        order_id=job.order_id, order_type=job.order_type, order_date=job.order_date,
        text_stem=stem, source=SRC_INLINE, fetch_status=job.fetch_status,
        pdf_path=None, pdf_sha256=None, n_pages=None, has_text_layer=True,
        extracted_at=dt.datetime.now(dt.timezone.utc),
    )
    try:
        html = cache.get(job.source_url, offline=True)  # never the network
    except cache.FetchError as exc:
        result.status, result.error = FAILED, f"detail page not cached: {exc}"[:300]
        return result, []
    text = clean_inline_text(extract_inline_text(html))
    if not text:
        result.status, result.error = FAILED, "no inline text found"
        return result, []
    result.text_path, result.text_chars, result.text_sha256 = _write_text(settings, stem, text)
    result.stopword_ratio = stopword_ratio(text)
    return result, []


def extract_job(job: Job, settings: Settings) -> tuple[DocResult, list[PageResult]]:
    """Dispatch on what the fetch stage found. Never raises.

    Args:
        job (Job): one order.
        settings (Settings): output locations and the OCR engine.
    Returns:
        tuple[DocResult, list[PageResult]]: the order row and its page rows (failures become rows).
    """
    try:
        if job.pdf_path:
            return extract_pdf(job, settings)
        if job.fetch_status == "OK_INLINE":
            return extract_inline(job, settings)
        return DocResult(
            order_id=job.order_id, order_type=job.order_type, order_date=job.order_date,
            text_stem=text_stem(job.order_id, job.source_url), source=SRC_NONE,
            fetch_status=job.fetch_status, pdf_path=None, pdf_sha256=None, n_pages=None,
            status=NO_SOURCE, error=f"fetch stage: {job.fetch_status}",
            extracted_at=dt.datetime.now(dt.timezone.utc),
        ), []
    except Exception as exc:  # pragma: no cover - last-resort guard for a long run
        return DocResult(
            order_id=job.order_id, order_type=job.order_type, order_date=job.order_date,
            text_stem=text_stem(job.order_id, job.source_url), source=SRC_NONE,
            fetch_status=job.fetch_status, pdf_path=job.pdf_path, pdf_sha256=None,
            n_pages=None, status=FAILED, error=f"{type(exc).__name__}: {exc}"[:300],
            extracted_at=dt.datetime.now(dt.timezone.utc),
        ), []


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

def load_jobs(path: pathlib.Path = DOCUMENTS_IN, *, retries: int = 5) -> list[Job]:
    """Read the fetch stage's output.

    While the fetcher is still running it rewrites this file at checkpoints,
    so a read can catch it half-written; retry rather than fail.

    Args:
        path (pathlib.Path): ``derived/order_documents.parquet``.
        retries (int): attempts while the file may be half-written.
    Returns:
        list[Job]: one per order.
    Raises:
        RuntimeError: the file could not be read after the retries.
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            rows = pq.read_table(path).to_pylist()
            break
        except (OSError, pa.ArrowInvalid) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"could not read {path}: {last}")
    return [
        Job(order_id=r["order_id"], order_type=r["order_type"], order_date=r["order_date"],
            source_url=r["source_url"], fetch_status=r["status"],
            doc_shape=r["doc_shape"], pdf_path=r["pdf_path"])
        for r in rows
    ]


def select(jobs: list[Job], *, categories: list[str] | None = None,
           order_ids: list[int] | None = None, limit: int | None = None,
           sample: int | None = None, seed: int = 7) -> list[Job]:
    """Filter jobs. ``sample`` draws a year-stratified random sample.

    Args:
        jobs (list[Job]): all orders.
        categories (list[str] | None): order types to keep.
        order_ids (list[int] | None): ids to keep.
        limit (int | None): keep the first N.
        sample (int | None): draw about N orders, spread evenly over years.
        seed (int): the sample's random seed.
    Returns:
        list[Job]: the selected orders.
    """
    out = jobs
    if categories:
        wanted = set(categories)
        out = [j for j in out if j.order_type in wanted]
    if order_ids:
        wanted_ids = set(order_ids)
        out = [j for j in out if j.order_id in wanted_ids]
    if sample:
        rng = random.Random(seed)
        by_year: dict[int | None, list[Job]] = collections.defaultdict(list)
        for j in out:
            by_year[j.order_date.year if j.order_date else None].append(j)
        years = sorted(by_year, key=lambda y: (y is None, y or 0))
        per_year = max(1, sample // max(1, len(years)))
        picked = []
        for y in years:
            group = by_year[y]
            picked.extend(rng.sample(group, min(per_year, len(group))))
        out = picked
    if limit is not None:
        out = out[:limit]
    return out


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

DOCS_SCHEMA = pa.schema([
    pa.field("order_id", pa.int64()),
    pa.field("order_type", pa.string()),
    pa.field("order_date", pa.date32()),
    pa.field("text_stem", pa.string()),
    pa.field("source", pa.string()),
    pa.field("fetch_status", pa.string()),
    pa.field("pdf_path", pa.string()),
    pa.field("pdf_sha256", pa.string()),
    pa.field("n_pages", pa.int32()),
    pa.field("n_native", pa.int32()),
    pa.field("n_sparse", pa.int32()),
    pa.field("n_scanned", pa.int32()),
    pa.field("n_broken", pa.int32()),
    pa.field("n_vector", pa.int32()),
    pa.field("n_ocr_done", pa.int32()),
    pa.field("n_ocr_rotated", pa.int32()),
    pa.field("n_ocr_pending", pa.int32()),
    pa.field("n_ocr_failed", pa.int32()),
    pa.field("has_text_layer", pa.bool_()),
    pa.field("mean_ocr_conf", pa.float64()),
    pa.field("text_path", pa.string()),
    pa.field("text_chars", pa.int64()),
    pa.field("text_sha256", pa.string()),
    pa.field("stopword_ratio", pa.float64()),
    pa.field("status", pa.string()),
    pa.field("error", pa.string()),
    pa.field("extracted_at", pa.timestamp("us", tz="UTC")),
])

PAGES_SCHEMA = pa.schema([
    pa.field("order_id", pa.int64()),
    pa.field("text_stem", pa.string()),
    pa.field("page_no", pa.int32()),
    pa.field("page_class", pa.string()),
    pa.field("method", pa.string()),
    pa.field("native_chars", pa.int32()),
    pa.field("quality", pa.float64()),
    pa.field("image_cover", pa.float64()),
    pa.field("ocr_conf", pa.float64()),
    pa.field("out_chars", pa.int32()),
    pa.field("drawings", pa.int32()),
    pa.field("ocr_variant", pa.string()),
])


def _table(rows: list, schema: pa.Schema) -> pa.Table:
    """Dataclass rows → an Arrow table with a fixed schema.

    Args:
        rows (list): DocResult or PageResult rows.
        schema (pyarrow.Schema): DOCS_SCHEMA or PAGES_SCHEMA.
    Returns:
        pyarrow.Table: the table.
    """
    columns = {name: [] for name in schema.names}
    for row in rows:
        payload = asdict(row)
        for name in columns:
            columns[name].append(payload[name])
    return pa.Table.from_pydict(columns, schema=schema)


def write_outputs(docs: list[DocResult], pages: list[PageResult],
                  out_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Write ``text_documents.parquet`` and ``text_pages.parquet`` atomically (also at checkpoints).

    Args:
        docs (list[DocResult]): order rows.
        pages (list[PageResult]): page rows.
        out_dir (pathlib.Path): ``derived/`` or a scratch directory.
    Returns:
        tuple[pathlib.Path, pathlib.Path]: the two paths written.
    """
    assert_safe_output(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = sorted(docs, key=lambda r: (
        r.order_type, r.order_date is None,
        -(r.order_date.toordinal() if r.order_date else 0), r.text_stem))
    pages = sorted(pages, key=lambda p: (p.text_stem, p.page_no))
    docs_path = out_dir / "text_documents.parquet"
    pages_path = out_dir / "text_pages.parquet"
    for table, path in ((_table(docs, DOCS_SCHEMA), docs_path),
                        (_table(pages, PAGES_SCHEMA), pages_path)):
        tmp = path.parent / f"{path.name}.tmp-{os.urandom(6).hex()}"
        pq.write_table(table, tmp, compression="snappy")
        tmp.replace(path)  # atomic, so a reader never sees a half-written file
    return docs_path, pages_path


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _worker_init(niceness: int) -> None:
    """Per-process setup: lower the priority and silence MuPDF's error printing.

    Args:
        niceness (int): added to the process's nice value (0 = unchanged).
    Returns:
        None.
    """
    if niceness:
        try:
            os.nice(niceness)
        except OSError:  # pragma: no cover
            pass
    pymupdf.TOOLS.mupdf_display_errors(False)


def run(jobs: list[Job], settings: Settings, *, workers: int = 1, niceness: int = 0,
        progress_every: int = 500,
        checkpoint: callable | None = None,
        checkpoint_every: int = 0) -> tuple[list[DocResult], list[PageResult]]:
    """Extract every job. ``workers <= 1`` runs in-process (used by tests).

    Args:
        jobs (list[Job]): orders to extract.
        settings (Settings): output locations and the OCR engine.
        workers (int): processes (needs ``if __name__ == "__main__"``: Python 3.14 uses forkserver).
        niceness (int): worker priority.
        progress_every (int): log every N orders.
        checkpoint (Callable | None): called with (docs, pages) every checkpoint_every orders.
        checkpoint_every (int): 0 disables checkpoints.
    Returns:
        tuple[list[DocResult], list[PageResult]]: every order and page row (completion order).
    """
    docs: list[DocResult] = []
    pages: list[PageResult] = []
    started = time.monotonic()

    def collect(result: tuple[DocResult, list[PageResult]]) -> None:
        """Gather one order's result, log progress, checkpoint.

        Args:
            result (tuple[DocResult, list[PageResult]]): one order's output.
        Returns:
            None.
        """
        doc, doc_pages = result
        docs.append(doc)
        pages.extend(doc_pages)
        n = len(docs)
        if progress_every and (n % progress_every == 0 or n == len(jobs)):
            rate = (time.monotonic() - started) / n
            log.info("%d/%d docs  %.2fs/doc  ocr_pages=%d  eta=%.1f min", n, len(jobs), rate,
                     sum(d.n_ocr_done for d in docs), rate * (len(jobs) - n) / 60)
        if checkpoint and checkpoint_every and n % checkpoint_every == 0 and n < len(jobs):
            checkpoint(docs, pages)

    if workers <= 1:
        _worker_init(niceness)
        for job in jobs:
            collect(extract_job(job, settings))
        return docs, pages

    with concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, initializer=_worker_init, initargs=(niceness,)) as pool:
        futures = [pool.submit(extract_job, job, settings) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            collect(future.result())
    return docs, pages


def _ocr_pages(d: DocResult) -> int:
    """Pages of an order that need OCR.

    Args:
        d (DocResult): an order row.
    Returns:
        int: scanned + broken-layer + vector-text pages.
    """
    return d.n_scanned + d.n_broken + d.n_vector


def summarise(docs: list[DocResult], pages: list[PageResult]) -> dict:
    """Run summary for the log (OCR shares feed the runbook's Day 5 verdict).

    Args:
        docs (list[DocResult]): order rows.
        pages (list[PageResult]): page rows.
    Returns:
        dict: documents, by_source, by_status, pdf_pages, page_classes, OCR counts and shares.
    """
    pdf_docs = [d for d in docs if d.source == SRC_PDF and d.n_pages is not None]
    needing = [d for d in pdf_docs if _ocr_pages(d) > 0]
    ocr_pages = sum(_ocr_pages(d) for d in pdf_docs)
    total_pages = sum(d.n_pages for d in pdf_docs)
    return {
        "documents": len(docs),
        "by_source": dict(collections.Counter(d.source for d in docs)),
        "by_status": dict(collections.Counter(d.status for d in docs)),
        "pdf_documents": len(pdf_docs),
        "pdf_pages": total_pages,
        "page_classes": dict(collections.Counter(p.page_class for p in pages)),
        "pdf_docs_needing_ocr": len(needing),
        "pdf_docs_needing_ocr_share": round(len(needing) / len(pdf_docs), 4) if pdf_docs else None,
        "fully_scanned_docs": sum(_ocr_pages(d) == d.n_pages for d in needing),
        "ocr_pages": ocr_pages,
        "ocr_pages_share": round(ocr_pages / total_pages, 4) if total_pages else None,
        "ocr_done_pages": sum(d.n_ocr_done for d in docs),
        "ocr_rotated_pages": sum(d.n_ocr_rotated for d in docs),
    }


def setup_logging(level: str, log_file: pathlib.Path | None) -> None:
    """Log to stdout and, unless a dry run, to a file; used by most stages.

    Args:
        level (str): logging level name.
        log_file (pathlib.Path | None): also log here (refused under raw/pdfs and raw/cache).
    Returns:
        None.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        assert_safe_output(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=handlers, force=True)


def build_arg_parser() -> argparse.ArgumentParser:
    """The command-line options of ``python -m src.extract_text``.

    Returns:
        argparse.ArgumentParser: the parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.extract_text",
        description="Extract order text from PDFs (text layer, OCR fallback) into derived/text/",
    )
    parser.add_argument("--documents", type=pathlib.Path, default=DOCUMENTS_IN,
                        help="fetch-stage output to read (default: derived/order_documents.parquet)")
    parser.add_argument("--category", action="append",
                        choices=["WTM", "SETTLEMENT", "AO", "ED_CGM"])
    parser.add_argument("--order-id", action="append", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sample", type=int,
                        help="year-stratified random sample of N orders (dry runs)")
    parser.add_argument("--no-ocr", action="store_true",
                        help="classify pages but do not OCR; OCR pages are left pending")
    parser.add_argument("--ocr-lang", default="eng")
    parser.add_argument("--ocr-dpi", type=int, default=300)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--nice", type=int, default=10,
                        help="lower worker priority so the machine stays usable")
    parser.add_argument("--scratch", type=pathlib.Path,
                        help="dry run: put every output (text, parquets, OCR cache, log) under DIR")
    parser.add_argument("--progress-every", type=int, default=500)
    parser.add_argument("--checkpoint-every", type=int, default=2000)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run stage 3: extract every selected order and write the two tables.

    Args:
        argv (list[str] | None): command-line arguments; None reads ``sys.argv``.
    Returns:
        int: 0.
    """
    args = build_arg_parser().parse_args(argv)
    if args.scratch:
        root = args.scratch
        text_dir, out_dir = root / "text", root
        ocr_dir, log_file = root / "ocr", root / "extract_text.log"
    else:
        text_dir, out_dir, ocr_dir = TEXT_DIR, DERIVED_DIR, OCR_CACHE
        log_file = LOGS_DIR / "extract_text.log"
    for path in (text_dir, out_dir, ocr_dir, log_file):
        assert_safe_output(path)
    setup_logging(args.log_level, log_file)

    engine = None
    if not args.no_ocr:
        version = tesseract_version()
        if version is None:
            log.warning("tesseract not found: OCR pages will be left pending "
                        "(install with: sudo apt install tesseract-ocr)")
        else:
            engine = TesseractEngine(version=version, lang=args.ocr_lang, dpi=args.ocr_dpi)
            log.info("OCR engine: %s", engine.signature)

    jobs = select(load_jobs(args.documents), categories=args.category,
                  order_ids=args.order_id, limit=args.limit, sample=args.sample)
    settings = Settings(text_dir=text_dir, ocr_dir=ocr_dir, engine=engine)
    log.info("extracting %d orders with %d worker(s) -> %s", len(jobs), args.workers, text_dir)

    started = time.monotonic()
    docs, pages = run(
        jobs, settings, workers=args.workers, niceness=args.nice,
        progress_every=args.progress_every,
        checkpoint=lambda d, p: write_outputs(list(d), list(p), out_dir),
        checkpoint_every=args.checkpoint_every,
    )
    docs_path, pages_path = write_outputs(docs, pages, out_dir)
    summary = summarise(docs, pages)

    log.info("=" * 72)
    for key, value in summary.items():
        log.info("%-28s %s", key, value)
    log.info("elapsed %.1f min", (time.monotonic() - started) / 60)
    log.info("wrote %s and %s", docs_path, pages_path)
    share = summary["pdf_docs_needing_ocr_share"]
    if args.order_id or args.sample or args.limit:
        # A hand-picked subset says nothing about the corpus-wide OCR share.
        log.info("runbook Day 5: not assessed on a subset run")
    elif share is not None:
        verdict = ("under 10%: OCR just those and move on" if share < 0.10 else
                   "over 30%: restrict the study period instead of OCRing everything"
                   if share > 0.30 else "10-30%: judgement call — see runbook Day 5")
        log.info("runbook Day 5: %.1f%% of PDF orders need OCR -> %s", share * 100, verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
