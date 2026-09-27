# CLAUDE.md — briefing for AI agents working on this repository

Last updated: 2026-09-27 (Week 2, Day 4 built). Read this before touching code or data.
Companion docs: `timeline.md` (the thesis runbook: weekly plan, 8 rules, target
schema), `technical_plan.md` (the thesis design: stages, estimators, hypotheses),
`results_log.md` (every published figure with provenance), `hand/recon_notes.md`
(verified SEBI protocol notes and how orders state the window), `PROJECT_GUIDE.md`
(the human-facing guide).

---

## 1. What this project is

A master's thesis on **SEBI enforcement**, due 2026-12-01. The corpus is every
order in four SEBI listing categories; the aim is to find, for each underlying
matter, the **final/last SEBI order that closes it**, and to extract from the text
the violation (investigation/examination) period, earlier SCNs and interim
orders with dates, and how SEBI came to look at the matter (detection channel).
Those feed survival models of detection lag (Stage 1), an event study (Stage 3)
and fraud prediction (Stage 2) — see `timeline.md` and `technical_plan.md`.

- **In scope (primary corpus):** Orders of Chairperson/Members (`WTM`, smid 2),
  Settlement Orders (`SETTLEMENT`, smid 3), Orders of AO (`AO`, smid 6),
  Orders of ED/CGM (`ED_CGM`, smid 133). `order_type` comes from the category,
  never from the title.
- **Out of scope as primary observations:** SAT (smid 1), Courts (7), Special
  Courts (77), Reg. 30A (138) — appellate/judicial follow-ups.
- Future document labels (not built yet): `INTERIM`, `CONFIRMATION_OR_EXTENSION`,
  `FINAL`, `SETTLEMENT_FINAL`, `APPELLATE`, `JUDICIAL`, `UNKNOWN`, `NEEDS_REVIEW`.
- **The window is one quantity: the suspected violation period**, i.e. the
  period of business activity SEBI examined. "Investigation period",
  "examination period", "inspection period" and "relevant period" are alternate
  names for it and all fill the single `violation_start`/`violation_end` pair.
  SEBI's usual "conducted investigation **during** the period X to Y" means that
  activity period too, *not* the time the investigation took — keep it. The
  true fraud start is unknowable; the examined period is the proxy.
- **Never the time the probe itself took.** The only place those dates appear
  as a range is an on-site *inspection visit* ("conducted an inspection of the
  Company during the period February 28, 2005 to March 5, 2005"), sometimes even
  labelled "inspection period". `extract_windows.inspection_visit` screens them
  out on the regex and model paths (inspection windows ≤ 31 days, or ≤ 100 days
  phrased "conducted the inspection during …").
- Where no period is named, the dates of the alleged conduct are the same
  quantity (`window_kind = conduct_dates`; e.g. fees collected without
  registration, trades during a UPSI period). A generic date range that is
  neither (funds raised, bank statement periods, letters) is not a window.
  "SEBI examined the matter" without dates must be kept distinct from a stated
  examination period.

## 2. Status (Week 2, Day 4 built)

Done: listing scrape → document fetch → text extraction + OCR (708 OCR orders,
7,782 OCR pages, 182 rotated) → window candidates + **regex v1.1**
(`derived/windows.parquet`, run 2026-09-27 13:08: 10,516 windows, 50.4% of
orders with text) → **model fallback** (`derived/window_model.parquet`,
`derived/violation_windows.parquet`, run 2026-09-27 13:09–14:06: 2,712 orders
sent, 1,641 windows). Combined: **12,157 of 20,849 orders with text (58.3%)**.
The Week 1 reading tasks were replaced by the corpus-wide phrasing survey
(`hand/recon_notes.md` §7) and are closed.

After that run, an analysis of its output found five bugs in how model quotes
are read (a malformed date widened to its whole year; "January 21-25, 2008",
"May-June 1998", "2003-05", "May 3 to May 20, 2004" read wrongly) and trade-log
rows taken as windows. Fixed and tested (410 tests); re-reading the cached
answers changes 70 windows, all reviewed: combined **12,150 (58.3%)**. **Pending:**
the user re-runs `python -m src.window_model --offline` (seconds, 0 model
calls) so `derived/` holds the fixed reading. A review of 50 random model
windows judged 42–45 of 50 right after the fixes (84–90%; agent-judged, not
blind; the dev/held-out figure of 91% was on only 33 windows).

**Day 3 (sanity rules, extraction rate)**: `src/check_windows.py`, scratch-verified
on the full chain. It exposed one parser bug ("period i.e." read as "Period-I",
merging a window with its settlement months; 1 order) and regex financial-year
windows labelled `month` precision instead of `fy` (130 orders; labels only).
Both fixed. Result: 43 windows flagged (2 impossible + 1 lag ≤ 0 + 40 unusual:
27 start before 1995, 14 lags ≥ 20 years); extraction rate **58.1%** (12,107 of
20,849) — the runbook's 50–70% band; drop-out differs by category (Cramér's V
0.34) and year (0.30). **Day 3 final** (user reviewed all 43 in
`hand/window_review.csv`: 33 keep, 8 drop, 2 fix → `violation_method = hand`):
12,142 windows, **58.2%**, in `derived/windows_checked.parquet`.

**Day 4 (prior actions, sanctions)**: `src/extract_actions.py` + `src/amounts.py`,
scratch-verified on the full corpus (1 min). Prior action (earliest plausible SCN /
interim / confirmatory date) for 16,918 orders (81%; 89% of windowed orders);
penalties in 8,039 orders (AO 7,912), median ₹5 lakh, figure vs words disagree on
32 of 9,704; settlement amounts in 1,717 of 1,994 settlement orders; restraints in
2,511 WTM orders. Lag split (windowed orders, medians): window end → order 5.50 y;
→ first action 3.15 y; → first SCN (entity-level) 3.86 y; first action → order 1.19 y.
**Pending:** `python -m src.extract_actions` (~1 min) to write `derived/`.

Built, not yet run into `hand/`: `src/validation_sample.py` (Week 3 blind
sample, see §11); it reads `derived/windows_checked.parquet`, so run it after the
Day 3 review.

## 3. Hard rules — do not break these

1. **All HTTP goes through `src/cache.py`** (`cache.get` / `cache.post` /
   `cache.fetch`). Never call httpx/requests/urllib directly in pipeline code.
   The one exception is the local model server (Ollama, localhost only), reached
   only through `src/llm.py`, which caches every answer permanently under
   `raw/llm/` keyed by model digest + options + prompts + schema.
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
8. Keep `python -m pytest tests -q` green (513 tests at time of writing).
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
| 4 windows | `python -m src.extract_windows` | text_documents, `derived/text/` | `derived/window_candidates.parquet`, `derived/windows.parquet`, `logs/extract_windows.log` | none (~1 min) |
| 5 model | `python -m src.window_model [--offline]` | windows, window_candidates | `derived/window_model.parquet`, `derived/violation_windows.parquet`, `raw/llm/`, `logs/window_model.log` | localhost Ollama only (~55 min; seconds when cached) |
| 6 sanity | `python -m src.check_windows` | violation_windows, `hand/window_review.csv` | `derived/windows_checked.parquet`, `derived/window_flagged.csv`, `derived/extraction_rate.csv`, appends to `hand/window_review.csv`, `logs/check_windows.log` | none (seconds) |
| 7 actions | `python -m src.extract_actions` | text_documents, `derived/text/`, windows_checked | `derived/order_actions.parquet`, `derived/prior_action_mentions.parquet`, `derived/sanction_mentions.parquet`, `logs/extract_actions.log` | none (~1 min) |

Full offline rebuild of `derived/` from `raw/` (no network):
`scrape_listings --offline` → `fetch_pdfs --offline` → `extract_text` → `extract_windows`
→ `window_model --offline` (answers from `raw/llm/`; `model_calls=0` proves it) → `check_windows`
→ `extract_actions`.
Useful flags: `--category`, `--order-id` (repeatable), `--limit`, `--start-page/--end-page`
(listings), `--resolve-only` (fetch), `--sample N`, `--no-ocr`, `--scratch DIR`,
`--workers`, `--nice` (extract), `--top-k` (windows), `--model`, `--think`, `--status`,
`--stem` (model). Checkpoints: fetch every 500, extract every 2000.

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

`derived/window_candidates.parquet` — top 3 candidate sentences per order (28,140)
: `text_stem`, `order_id`, `order_type`, `order_date`, `cand_rank` (1 = best), `score`,
`sent_index`, `char_start`/`char_end` (offsets into the `.txt` file — header stripping
blanks lines in place, so they stay valid), `page_no`, `rel_pos`, `sentence` (verbatim,
whitespace collapsed), `label_kind` ∈ `defined, strong, relevant, context, generic`,
`label_text`, `n_dates`, `n_ranges`, `negative` (open offer / letter dated / … cue),
`range_text`, `parsed_start`, `parsed_end`, `start_precision`/`end_precision` ∈ `day, month`,
`parse_rule` ∈ `range, range_year_inferred, fy, fy_range, patches_union,
range_till_inspection, range_fy_list` (precision `fy` for financial years), `confidence`
(null = a range the regex will not call the window).

`derived/windows.parquet` — one row per order (20,861)
: `text_stem`, `order_id`, `order_type`, `order_date`, `text_status`, `n_sentences`,
`n_candidates`, `window_status` ∈ `PARSED, UNCONFIRMED_RANGE, UNPARSED, NO_CANDIDATES,
NO_TEXT`, `violation_start`, `violation_end`, `start_precision`, `end_precision`,
`violation_raw` (the source sentence, rule 2), `violation_confidence` ∈ `high, medium, low`
(`none` is never assigned by regex), `violation_method` (`regex` or null),
`violation_rule`, `cand_rank`, `char_start`, `rules_version`, `error`. Dates are not
sanity-checked yet (runbook Day 3).

`derived/window_model.parquet` — one row per order sent to the model (2,732)
: `text_stem`, `order_id`, `order_type`, `order_date`, `regex_status`, `n_sentences_sent`,
`model`, `model_digest`, `prompt_version`, `think`, `cache_key` (file under `raw/llm/`),
`raw_response` (the model's JSON), `model_status` ∈ `MODEL_WINDOW, MODEL_NONE,
MODEL_UNVERIFIED` (quotes not in the sentence / unreadable / start after end / open-ended),
`MODEL_REJECTED` (verified, but a known non-window: correspondence dates, an
inspection visit, a statement or ledger period), `MODEL_ERROR`; `window_kind` ∈
`stated_period, conduct_dates` (null when no window; the model's own word —
`investigation`/`violation`/`none` — stays in `raw_response`); `sentence_no`, `start_text`/`end_text` (verbatim quotes),
`violation_start`, `violation_end`, `start_precision`/`end_precision` ∈ `day, month, year, fy`,
`violation_raw`, `note`, `seconds`, `from_cache`.

`derived/violation_windows.parquet` — one row per order (20,861); **the table Day 3 reads**
: regex window where `window_status = PARSED`, else the verified model window. Columns:
`text_stem`, `order_id`, `order_type`, `order_date`, `text_status`, `window_status`,
`model_status`, `violation_start`, `violation_end`, `start_precision`, `end_precision`,
`violation_raw`, `violation_confidence` (model windows are `medium`),
`violation_method` ∈ `regex, model`, `violation_rule` (`model:<kind>` for model rows),
`window_kind` — *how the order states the one quantity*, not a different quantity:
`stated_period` (the order names it: investigation/examination/inspection/relevant
period; all regex windows) or `conduct_dates` (model only: no period named, the dates
of the alleged conduct are given); `char_start` (regex only).

`derived/windows_checked.parquet` — one row per order (20,861); **the Week 2 window table**
: every `violation_windows` column, plus `sanity_status` ∈ `PASS, FLAGGED, REVIEWED_KEEP,
REVIEWED_FIX, REVIEWED_DROP, NO_WINDOW`, `sanity_flags` (`end_after_order`,
`start_after_end`, `lag_not_positive`, `lag_20_years`, `start_before_1995`, `;`-joined),
`sanity_severity` ∈ `impossible, implausible`, `flagged_start`/`flagged_end` (the
flagged window, kept for reference), `review_note`. FLAGGED and REVIEWED_DROP rows
have no `violation_start`/`violation_end` (not in the dataset). REVIEWED_FIX rows
carry the hand dates with `violation_method = hand`. Month-precision ends get the
benefit of their month in R1/R3; years and financial years do not.

`hand/window_review.csv` — the durable review of flagged windows: `decision` ∈ `keep,
fix, drop` (the only column applied), `violation_start`/`violation_end` for `fix`,
`note`, `suggested` (an optional pre-reading, never applied), and reference columns.
New flags are appended; existing rows are never changed.

`derived/order_actions.parquet` — one row per order (20,861); prior actions and sanctions
: `prior_action_date` (earliest *plausible* SCN / interim / confirmatory date recited:
not after the order, not before 30 Jan 1992, not before the window starts),
`prior_action_kind` ∈ `scn, interim, confirmatory`, `prior_action_raw` (snippet),
`n_prior_mentions`, `n_prior_implausible`, `earliest_scn_date`, `earliest_interim_date`,
`ao_appointment_date` (robustness only), `first_action_date = min(prior_action_date,
order_date)`, `first_action_source` ∈ `prior_action, order`; `penalty_total` (₹, imposing
sentences in the operative part; restatements counted once; Total rows not added),
`penalty_total_basis` ∈ `rows, stated_total, rows_each_unresolved`, `penalty_n`,
`penalty_max`, `penalty_each`, `penalty_joint`, `penalty_words_checked`,
`penalty_words_disagree`, `penalty_raw`, `penalty_outcome` ∈ `penalty, no_penalty,
unknown`, `settlement_amount` (settlement orders), `disgorgement_total`, `debarred`,
`debarment_months_max`, `debarment_indefinite`, `registration_action` ∈ `suspension,
cancellation`, `warned`, `no_penalty_stated`, `rules_version`, `error`.

`derived/prior_action_mentions.parquet` — every recited SCN / interim / confirmatory /
AO-appointment date (51,925): `text_stem`, `kind`, `date`, `precision`, `match_text`,
`snippet`, `rel_pos`, `plausible`, `reason`.

`derived/sanction_mentions.parquet` — every sanction statement in the operative part
(one row per amount): `text_stem`, `kind` ∈ `penalty, settlement, disgorgement,
debarment, registration, warning, no_penalty`, `sentence`, `rel_pos`, `amount_rupees`,
`amount_text`, `words_rupees`, `words_agree`, `is_total`, `each`, `joint`, `who_text`
(text after the amount naming whom it is imposed on — for Day 5), `amount_pos`,
`months`, `indefinite`, `action`.

`src/amounts.py` — the one rupee normaliser (runbook Day 4): Indian and western
grouping, crore/lakh, "/-", ₹/Rs./INR/backtick, typos ("1,00.000", "2,00,00"), a
redundant unit after a full figure ("10,00,000 lakhs"), and Indian number words
("Seven Thousand Two Hundred Sixty Nine Crore") to check every figure.

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
- 27 OCR pages remain <70 conf (mostly dense tables). OCR slips: bullets → `e`,
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

**Week 2 (model).**
- Ollama here runs `llama-server -np 1`: one request at a time, so extra workers
  only queue. Thinking mode (`--think`) spends ~4,300 tokens and ~83 s per order
  (vs 1.2 s) with no measured gain on the dev set — leave it off.
- Killing a background eval: `kill` the python PID, not its bash wrapper, or the
  run keeps the GPU busy and every other call queues behind it.
- The model must *quote* dates; code verifies the quote (letters+digits, leading
  zeros ignored) and parses it day-first. Never take a date the model wrote itself.
- `hand/window_model_devset.csv` (60) and `hand/window_model_holdout.csv` (30) are
  agent-labelled and were seen while tuning: **exclude them from the Week 3 blind
  validation sample.**
- Do not "fix" SEBI's "conducted investigation during the period X to Y" into a
  desk-time reading: in 102 such regex windows the hereinafter label and months-to-
  years durations show the activity period. Only inspection visits are desk time;
  a first draft of the model-side rule rejected the activity phrasing — caught by
  a test now (`test_real_windows_pass_the_rejection_rules`).
- Still unread by regex: glued text "fromApril01, 2018" (reaches the model).

## 11. Next steps (runbook Week 2 onward)

1. Week 2 Day 5: regulations and parties → `derived/orders.parquet` (built on
   `windows_checked` + `order_actions`) and `order_entities` (link
   `sanction_mentions.who_text` to parties; resolve `penalty_each`). Settlement orders
   stay in the main sample (user decision); only 27.4% of them state a window, so
   report their under-representation, do not drop them. Both `window_kind`s are the
   suspected violation window; `stated_period` only is a robustness cut. First-action
   definition for Stage 1: `first_action_date` is the matter's first public action
   (ISO: the Aug 2015 interim order against 59 entities, 41 days before the window
   closed — 10.3% of windowed orders have their first action on/before the window
   end); `earliest_scn_date` is the action against the entity. Decide with the unit
   of analysis (item 4).
2. Week 3: draw the blind sample with `python -m src.validation_sample` (writes
   `hand/validation_sample.csv` + `derived/validation_key.parquet`; strata 60
   regex / 25 model stated / 25 model conduct / 40 no window; refuses to
   overwrite a started sheet). Weight accuracy back with the key.
3. Regex dates use an explicit day-first grammar, not `dateutil` (which fills a
   missing day from today's date); keep it that way.
4. **Unit of analysis = what one row of the Stage 1 data stands for.** The
   pipeline has one row per *order*; the plan's lag is per *fraud* ("for each
   fraud i … first regulatory action", technical_plan §5.1; one event per firm,
   timeline Week 7; `cluster_col="firm_id"` because firms recur, timeline Week 5).
   SEBI issues one order per noticee in mass matters: 12,150 windowed orders
   hold only 3,394 distinct windows; ISO alone is 2,363 orders (19.4%) sharing
   one window with order dates 2018–2026, so order-level lags there measure the
   adjudication queue. Before Week 5 decide: order, matter (group orders of one
   case; lag = window end → earliest action), or matter × noticee. Needs a
   matter-grouping step (not built).
5. Build the finality classifier (interim / final / confirmatory) from order text;
   use `sat_appeals` as metadata. Stage 3 needs the event types by Week 7.