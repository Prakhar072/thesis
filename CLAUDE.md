# context.md — briefing for AI agents working on this repository

Last updated: 2026-09-24 (end of Week 1). Read this before touching code or data.
Companion docs: `paper/timeline.md` (the thesis runbook: weekly plan, 8 rules,
target schema), `results_log.md` (every published figure with provenance),
`hand/recon_notes.md` (verified SEBI protocol notes), `PROJECT_GUIDE.md` (the
human-facing guide).

---

## 1. What this project is

A master's thesis on **SEBI enforcement**, due 2026-12-01. The corpus is every
order in four SEBI listing categories; the aim is to find, for each underlying
matter, the **final/last SEBI order that closes it**, and to extract from the text
the violation (investigation/examination) period, earlier SCNs and interim
orders with dates, and how SEBI came to look at the matter (detection channel).
Those feed survival models of detection lag (Stage 1), an event study (Stage 3)
and fraud prediction (Stage 2) — see `paper/timeline.md`.

- **In scope (primary corpus):** Orders of Chairperson/Members (`WTM`, smid 2),
  Settlement Orders (`SETTLEMENT`, smid 3), Orders of AO (`AO`, smid 6),
  Orders of ED/CGM (`ED_CGM`, smid 133). `order_type` comes from the category,
  never from the title.
- **Out of scope as primary observations:** SAT (smid 1), Courts (7), Special
  Courts (77), Reg. 30A (138) — appellate/judicial follow-ups.
- Future document labels (not built yet): `INTERIM`, `CONFIRMATION_OR_EXTENSION`,
  `FINAL`, `SETTLEMENT_FINAL`, `APPELLATE`, `JUDICIAL`, `UNKNOWN`, `NEEDS_REVIEW`.
- A generic date range is **not** an investigation period ("during the period
  from 1.4.2000 to 31.3.2004" about funds collected is not one). "SEBI examined
  the matter" without dates must be kept distinct from a stated examination period.

## 2. Status at end of Week 1

Done: listing scrape → document fetch → text extraction + OCR, all verified.
**Pending at time of writing:** the user was re-running `python -m src.extract_text`
to apply the OCR rotation/vector fix (started 2026-09-24 18:46). If
`derived/text_documents.parquet` lacks the `n_ocr_rotated` column, that run has
not finished or failed — check `logs/extract_text.log`. After it, expect
`pdf_docs_needing_ocr 708`, `ocr_done_pages 7782`, `ocr_rotated_pages 180`.

Not done (runbook Week 1): the Day 1 close reading of ~10 orders with the
phrasings recorded in `hand/recon_notes.md` (and the Day 5 spot-check of 10
text files). Week 2 regex work depends on it; the user does the reading.

## 3. Hard rules — do not break these

1. **All HTTP goes through `src/cache.py`** (`cache.get` / `cache.post` /
   `cache.fetch`). Never call httpx/requests/urllib directly in pipeline code.
2. **SEBI politeness:** sequential only, never parallel; 1.5 s delay is built into
   the cache. Set `SEBI_CONTACT=<email>` so the User-Agent carries a contact.
3. **`raw/` is append-only.** Never delete or modify under `raw/`.
4. **`raw/pdfs/<id>.pdf` are hard links to `raw/cache/` entries.** Writing to
   one corrupts both. Open PDFs read-only (the code opens from bytes). The
   extractor refuses to write under `raw/pdfs` or `raw/cache`.
5. **Stages after the fetch never use the network.** Read cached pages with
   `cache.get(url, offline=True)`; `--offline` on any CLI proves a run is
   cache-only (`network_calls=0`).
6. **Never hard-code cookies/JSESSIONID.** None are needed (see §5).
7. **User working preferences (observed in Week 1):** the user runs full-corpus
   and long-running commands themselves and commits manually. Agents build,
   test, and dry-run into a scratch directory (`extract_text --scratch DIR`),
   then hand over exact commands. Ask before any run that hits SEBI's servers.
8. Keep `python -m pytest tests -q` green (242 tests at time of writing).
9. Record any new figure in `results_log.md` with date, script and scope.

## 4. Environment

- WSL2 (Linux 6.6, systemd on). Repo: `/home/prakharu/projects/thesis`. Keep all
  data on the WSL filesystem — never `/mnt/c`. Paths in code are resolved from
  `cache.REPO_ROOT`, so modules work from any cwd.
- `.venv`: **Python 3.14.4**, httpx 0.28.1, beautifulsoup4 + lxml, pandas 2.3.3,
  pyarrow 25, pymupdf 1.28.2, duckdb, pytest 9.1.1. Do not pip-install into the
  venv while a pipeline run is using it.
- System: tesseract 5.5.0 (`eng`, `osd`); R with survival 3.8.12, cmprsk 2.2.12,
  SurvTrunc 0.2.0. Agents have **no sudo**.
- 16 cores, ~11 GB RAM, appears to have ~930 GB free disk but is only actually ~ 35 GB.

## 5. SEBI facts established empirically (the non-obvious ones)

**Listing endpoint** `POST https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp`
- The page selector is **`doDirect`, 0-based** (`doDirect = page - 1`), with
  `next=n`. `nextValue` is ignored (identical HTML for any value) though still
  sent. Verified by exact count reconciliation (WTM 256×25+24 = 6424).
- Loop bound = hidden `totalpage` (emitted unquoted: `value=257 />`; parse the
  leading digits). Past the last page the table is empty.
- WAF returns HTTP 530 "Unauthorized Request Blocked" unless the request has a
  browser-like User-Agent plus `Content-type`, `Origin`, `Referer`. No cookies.
  The cache rejects WAF pages so they are never cached.
- Orders are in `table#sample_1`. `table.ordersum` repeats seven
  `Order-11B-<year>.xlsx` links on every WTM page — not orders; excluded.
- A row's second anchor labelled `[Corrigendum]` is a supplement, not an order.
- The link's `title` attribute can contain HTML: corrigendum links and
  **SAT appeal annotations** (`<span>[SAT Appeal No.:</span><a href='sat.gov.in/…'>539/2022]</a>`).
  `clean_title_attr` strips markup, keeps appeal text, fills `sat_appeals`.
- `order_id` = digits in `_<id>.html`. 76 rows have none: 74 link straight to a
  PDF (34 of them SAST exemption orders), 2 are oddly named `.html` pages.
  Their files are named `noid_<first 12 hex of sha256(source_url)>`.
- Listing order is newest-first; new publications shift pages during a long run.

**Detail pages**
- The PDF sits in `<iframe src="https://www.sebi.gov.in/web/?file=<real url>">`
  — no `<a href>` to it. Inner URLs may be relative and use `.PDF`.
- PDF filenames are opaque epoch-ms stamps: the URL must be read off the page.
- Inline-HTML orders (full text in `section.main_section`, no PDF): 1,582, all
  dated 2002–2011, mostly 2004–06.
- Resolver precedence is **iframe → inline text (≥1,500 chars) → PDF anchor**.
  Old pages print the order inline and link annexures; anchor-first archived
  annexures as orders (32 cases, fixed).
- `http://www.sebi.gov.in` is unreachable (port 80); `upgrade_scheme` rewrites
  SEBI links to https. Some pages link sat.gov.in or supremecourtofindia.nic.in
  documents; none are in the corpus.

## 6. Pipeline

| Stage | Command | Reads | Writes | Network |
|---|---|---|---|---|
| 1 listings | `python -m src.scrape_listings [--offline]` | cache | `derived/listings.parquet`, `reports/listing_*.csv`, `logs/scrape_listings.log` | 836 POSTs (cached) |
| 2 documents | `SEBI_CONTACT=… python -m src.fetch_pdfs` | listings | `derived/order_documents.parquet`, `derived/fetch_failures.csv`, `raw/pdfs/`, `logs/fetch_pdfs.log` | ~40k GETs (cached) |
| 3 text | `python -m src.extract_text` | order_documents, `raw/pdfs`, cache | `derived/text/*.txt`, `derived/text_documents.parquet`, `derived/text_pages.parquet`, `raw/ocr/`, `logs/extract_text.log` | none |

Full offline rebuild of `derived/` from `raw/` (no network):
`scrape_listings --offline` → `fetch_pdfs --offline` → `extract_text`.
Useful flags: `--category`, `--order-id` (repeatable), `--limit`, `--start-page/--end-page`
(listings), `--resolve-only` (fetch), `--sample N`, `--no-ocr`, `--scratch DIR`,
`--workers`, `--nice` (extract). Checkpoints: fetch every 500, extract every 2000.

## 7. Text extraction details (`src/extract_text.py`)

Per PDF page: native text → `text_quality` (share of non-space chars that are not
control/private-use/unassigned/U+FFFD) → image coverage → (for would-be sparse
pages) vector-path count → class:

| Class | Rule | Action |
|---|---|---|
| `NATIVE` | ≥100 non-space chars, quality ≥0.75 | keep text layer |
| `BROKEN_LAYER` | ≥20 chars, quality <0.75 (Ghostscript 7.06 control-code layers) | OCR |
| `SCANNED` | <100 chars, images cover ≥50% | OCR |
| `VECTOR_TEXT` | <100 chars, <50% images, ≥300 vector paths (outlined text) | OCR |
| `SPARSE` | otherwise (blank, signature pages) | keep text layer |

- OCR: render grayscale → `tesseract stdin stdout -l eng --psm 3 --dpi N tsv`
  (subprocess, `OMP_THREAD_LIMIT=1`), text rebuilt from TSV, mean word conf kept.
- **Retry:** if first-pass conf < 70, try rotations {0,90,180,270} × dpi {300,200},
  keep highest conf; `text_pages.ocr_variant` records the winner (`r90_200` …).
- OCR cache: `raw/ocr/<sha[:2]>/<pdf_sha256>/p0001-<cfg12>.json`; first pass and
  retry (`|retry-v1` tag) are separate files; keyed by PDF content, so duplicate
  PDFs are OCR'd once. Changing lang/psm/dpi/tesseract version re-OCRs cleanly.
- Text normalisation: NBSP/narrow NBSP/figure space → space; U+2028/2029 → `\n`;
  BOM, zero-width space, word joiner, soft hyphen removed; ZWJ/ZWNJ kept;
  ligatures expanded; **no dehyphenation**; pages joined by `\f`.
- Inline orders: cached detail page (offline) → `extract_inline_text` →
  `clean_inline_text` (drops the "Mon DD, YYYY | Orders : <category>" caption).
- Incomplete text is never written: `NEEDS_OCR` orders get no text file.
- Running headers/footers are **not** removed; pymupdf often emits them at the
  top of a page. Strip repeated lines per document downstream.

## 8. Data dictionary

`derived/listings.parquet` — one row per listed order (20,861)
: `order_id` (int64, null for 76), `order_date`, `order_date_raw`, `title` (clean),
`source_url` (detail page or direct PDF), `order_type`, `listing_category`,
`listing_page`, `listing_url`, `smid`, `doc_kind` (`HTML_DETAIL`/`DIRECT_PDF`/`OTHER`),
`corrigendum_url`, `sat_appeals` ("539/2022; …" or null), `needs_review`,
`review_reason`, `scraped_at` (cache-file mtime).

`derived/order_documents.parquet` — one row per order
: `doc_shape` ∈ `IFRAME_PDF, ANCHOR_PDF, DIRECT_PDF, INLINE_HTML, NO_DOCUMENT, DETAIL_FAILED`;
`status` ∈ `OK` (PDF archived), `OK_INLINE`, `FAILED_DOWNLOAD`, `FAILED_DETAIL`,
`NO_DOCUMENT`, `NOT_A_PDF`; `pdf_url`, `pdf_path` (repo-relative), `pdf_bytes`,
`pdf_sha256`, `inline_text_chars`, `error`, `resolved_at`.

`derived/text_documents.parquet` — one row per order
: `text_stem` (file stem: `<order_id>` or `noid_<hash>`), `source` ∈ `PDF, INLINE_HTML, NONE`;
`status` ∈ `OK, NEEDS_OCR, NO_SOURCE, FAILED`; page counts `n_pages, n_native,
n_sparse, n_scanned, n_broken, n_vector, n_ocr_done, n_ocr_rotated,
n_ocr_pending, n_ocr_failed`; `has_text_layer`; `mean_ocr_conf`; `text_path`;
`text_chars`; `text_sha256`; `stopword_ratio` (English function-word share,
diagnostic only; tables score low); `error`; `extracted_at`.

`derived/text_pages.parquet` — one row per PDF page (318,116)
: `text_stem`, `page_no` (1-based), `page_class`, `method` ∈ `native, ocr,
ocr_pending, ocr_failed`, `native_chars`, `quality`, `image_cover`, `ocr_conf`,
`out_chars`, `drawings` (only for would-be sparse pages), `ocr_variant`.

**Joins.** `order_id` is unique where non-null, but **pandas `merge` matches
nulls to nulls**: joining on `order_id` cross-joins the 76 id-less orders (20,785
→ 25,700 rows). Join on `text_stem` instead — build it for listings/documents
with `src.extract_text.text_stem(order_id_or_None, source_url)` — and pass
`validate="one_to_one"`. SQL joins (DuckDB `USING (order_id)`) are safe: NULLs
never match. `text_documents` has no `source_url`. pandas turns nullable int64
into float unless read with `dtype_backend="numpy_nullable"` — cast to `int`
before building stems (`104646.0` ≠ `104646`). Deduplicate documents on
`pdf_sha256` (70 PDFs shared by 145 orders).

## 9. Known issues and limitations

- 12 orders have no text: 96064, 22883, 35482, the no-id "VTX industries" PDF (dead
  links, 404), 31359 (detail page 404); 31687 (.doc), 31543 (.htm), 16864
  (multi-page HTML), 15272 (722-char inline order), 15254 (SAT order page), 13589,
  16655 (empty pages).
- ~25 OCR pages remain <70 conf (mostly dense tables). OCR slips: bullets → `e`,
  "IA" read as "JA" once, handwriting and diagrams are noise.
- Evidence screenshots inside digital orders (e.g. WhatsApp chats in 67218) are
  partly OCR'd; their timestamps could pose as dates — filter OCR pages of mixed
  documents if needed.
- 16 annexure/copy PDFs remain in `raw/pdfs/` unreferenced (append-only raw).
- **Unit of analysis:** "Illiquid Stock Options at BSE" = 2,243 orders (10.8% of
  corpus, 18.6% of AO; 860 in 2023) sharing one violation window. The 2023→2024
  drop in orders (1,710→551) is mostly this wave ending, not an enforcement
  decline. Other clusters: investor-grievance non-redressal ~228, Titan 80, NSEL 52.
- SAT appeal annotations exist only for orders dated 2003–2022 — no annotation
  ≠ not appealed for recent orders.
- Only 15.5% of WTM titles say final/interim/ex-parte/confirmatory: classify
  finality from text, not titles.

## 10. Gotchas that cost time in Week 1

- Python 3.14 defaults multiprocessing to **forkserver**: any script using
  `ProcessPoolExecutor` needs `if __name__ == "__main__":` or it crashes.
- `pgrep -f PATTERN` inside a wait loop matches the loop's own command line and
  never exits. Wait on a PID instead.
- The agent's shell tool times out at 600 s: background long jobs, or have the
  user run them (`setsid nohup … &`) so they survive the session.
- pymupdf page indexing rejects numpy ints (`doc[int(n)]`).
- `fetch_pdfs.write_parquet` is not atomic; `extract_text` outputs are. Retry a
  read that hits a half-written file.
- httpx raises on 3xx and the cache does not follow redirects — deliberately:
  the only redirects seen went to sat.gov.in.
- Tesseract reads clean 90°-rotated text unaided but fails at 180° and on noisy
  1-bit sideways scans; the real-engine test uses an upside-down page.
- 1-bit ~200-dpi scans upsampled to 300 dpi OCR worse than at 200 dpi.

## 11. Next steps (runbook Week 2 onward)

1. User reads ~10 orders from `derived/text/` next to the PDFs; phrasings go in
   `hand/recon_notes.md`.
2. Decide the unit of analysis (order / matter / noticee) before modelling;
   handle the ISO cluster and `pdf_sha256` duplicates explicitly.
3. Week 2 (runbook): sentence candidates for violation windows (keep the source
   sentence, rule 2), regex v1 with `dateutil` `dayfirst=True`, model fallback
   with cached responses, sanity rules, prior-action (SCN/interim) dates,
   penalties (Indian digit grouping), regulations, parties → `derived/orders.parquet`.
4. Strip repeated header/footer lines per document before sentence splitting.
5. Build the finality classifier from order text; use `sat_appeals` as metadata.
