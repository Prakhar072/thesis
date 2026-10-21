# Codebase map

> **A snapshot.** This map was traced from commit `44f7c88` (2026-09-28) and covers stages 1–8 and the first
> `build_orders`. For the current guide to every file — including stages 9–10, the Week 3 validation tools and the
> Week 4 market-data modules — see [`CODE_GUIDE.md`](../CODE_GUIDE.md).

What the pipeline does, traced from the code (commit `44f7c88`, 2026-09-28) and
checked against the files it produced in `derived/`. Counts in this document
come from those files unless marked otherwise.

Three kinds of statement are kept apart:
- **Definite** statements describe code I read and output I counted.
- *Intended:* marks what a docstring or design note says the code is for, where I
  have not checked that it achieves it.
- **Uncertain:** marks guesses.

Companion documents:
- `CODE_LOGIC_NOTES.md`: the difficult rules, one at a time.
- `AUDIT_REPORT.md`: silent errors, and the research-integrity audit.
- `VERIFICATION_CHECKLIST.md`: checks to run before analysis.

---

## 1. Project purpose

**What it does.** The pipeline builds a machine-readable dataset of SEBI's own
enforcement orders, one row per published order, for a thesis on detection lag
(Stage 1), fraud prediction (Stage 2) and the market reaction to orders
(Stage 3).

For each order it extracts:
- the **suspected violation window**, i.e. the period of business activity SEBI examined;
- the earliest **prior action** it recites (a show-cause notice, or an interim or confirmatory order);
- the **sanctions** imposed (penalty in rupees, settlement amount, disgorgement, debarment, registration action, warning);
- the **parties** (noticees), with a guessed role;
- the **regulations** it says were violated.

**The final database** is `derived/orders.parquet` (20,861 rows × 62 columns),
one row per order listed on SEBI's website in four categories, with two
companion tables:
- `derived/order_entities.parquet`: 47,972 rows, one per order × party.
- `derived/regulation_mentions.parquet`: 861,082 rows, one per provision cited.

A row is a **published order**, not a fraud, a firm or a matter. SEBI issues
one order per noticee in mass matters. The "Illiquid Stock Options at BSE"
matter alone contributes about 2,243 orders sharing one window, and 215 orders
have text identical to another order's (see `AUDIT_REPORT.md`, P-1 and P-2).

**One input record.** A row in a SEBI listing page (`POST getnewslistinfo.jsp`),
i.e. a table row with:
- the listing date, e.g. `Feb 28, 2020`;
- a link to the detail page, `…/feb-2020/…_46128.html`;
- a title attribute, e.g. `Adjudication Order in the matter of GDR issue by
  Aksh Optifibre Limited`, sometimes with SAT-appeal HTML appended.

The detail page leads to a PDF (usually) or to inline HTML text.

**One output record.** A row of `orders.parquet`, e.g. `text_stem = "46128"`:
- **Listing fields:** `order_id` 46128, `order_date` 2020-02-28, `order_type` AO, title, URLs, SAT appeals.
- **Text fields:** `text_path` `derived/text/46128.txt`, 31 pages, text layer yes.
- **Window:** `violation_start` 2010-08-01 and `violation_end` 2010-09-30, `violation_confidence` high, `violation_method` regex, `window_kind` stated_period, with the source sentence in `violation_raw`.
- **Actions:** `prior_action_date` 2018-06-08 (SCN), `first_action_date` 2018-06-08.
- **Sanctions:** `penalty_total` null (wrongly; see §9 and AUDIT C-1).
- **Matter and parties:** `issuer_guess` "Aksh Optifibre Limited", `n_parties` 6.
- **Regulations:** a JSON list starting `"SEBI Act 12A(a)", …, "PFUTP 3(a)", …`.

---

## 2. End-to-end pipeline

```
SEBI listing pages ──(1) scrape_listings──► listings.parquet
      │ detail pages / PDFs
      └──(2) fetch_pdfs──► order_documents.parquet, raw/pdfs/<stem>.pdf
                 └──(3) extract_text──► derived/text/<stem>.txt, text_documents.parquet, text_pages.parquet
                            ├──(4) extract_windows──► window_candidates.parquet, windows.parquet
                            │        └──(5) window_model──► window_model.parquet, violation_windows.parquet
                            │                 └──(6) check_windows (+ hand/window_review.csv)──► windows_checked.parquet
                            ├──(7) extract_actions (reads windows_checked)──► order_actions, prior_action_mentions, sanction_mentions
                            └──(8) extract_parties (reads sanction_mentions, order_actions, listings)──► order_entities, order_parties, regulation_mentions
(9) build_orders: listings + order_documents + text_documents + windows_checked + order_actions + order_parties ──► orders.parquet
(W3) validation_sample: windows_checked + listings + order_documents ──► hand/validation_sample.csv, derived/validation_key.parquet (not yet run)
```

All the stages share these properties:
- Stages 3–9 never use the network.
- Every stage after stage 2 keys on `text_stem`: `str(order_id)`, or
  `noid_<12 hex of sha256(source_url)>` for the 76 listings without an id.
- Every per-order function has a "never raises" wrapper that turns an
  exception into an error row.

### Stage 1: listings (`src/scrape_listings.py`)

**Functions:** `build_form`, `fetch_listing_page` → `cache.get(POST)`,
`parse_total_pages`, `parse_rows`, `clean_title_attr`, `scrape_category`,
`deduplicate`, `sort_rows`, `write_parquet`.

**Input:** SEBI's AJAX listing endpoint. There are four categories, `smid` 2
(WTM), 3 (Settlement), 6 (AO) and 133 (ED/CGM), with a page selector `doDirect =
page-1`.

**Output:**
- `derived/listings.parquet` (20,861 rows: AO 12,003, WTM 6,424, Settlement 1,994, ED/CGM 440);
- `reports/listing_pages.csv` (836 pages, all `ok`);
- `reports/listing_conflicts.csv` (empty);
- `reports/listing_needs_review.csv` (the 76 id-less rows).

**Assumptions:**
- `order_type` is the listing category, never inferred from the title.
- The hidden `totalpage` field is authoritative.
- The first `<a>` in a row is the order; later anchors are corrigenda.
- `order_id` is the digits in `_<id>.html`.
- The listing date is the order date.

**Failure modes:**
- A page that errors is logged `failed` and skipped. The run exits with code 1,
  but the parquet is still written without that page's rows.
- A page that parses to zero rows is logged `empty`, not failed.
- A table row with fewer than 2 cells or no anchor is skipped silently.
- New publications during a scrape shift rows between pages.
- A cached listing page is never refreshed without `--force` (stale cache).
- An order listed in two categories keeps its first occurrence; the conflict
  goes to `listing_conflicts.csv` (currently none).

### Stage 2: documents (`src/fetch_pdfs.py`)

**Functions:** `resolve_document` → `cache.get(detail page)` →
`extract_document_ref`, which picks the document in the order iframe PDF →
inline text of at least 1,500 characters → anchor PDF. Then
`download_document` → `cache.get(pdf)`, `is_pdf_bytes`, `archive_pdf` (hard
link), `write_parquet`, `write_failures`.

**Input:** `listings.parquet`.

**Output:**
- `derived/order_documents.parquet`, with statuses OK 19,267, OK_INLINE 1,582, NO_DOCUMENT 7, FAILED_DOWNLOAD 4 and FAILED_DETAIL 1;
- `raw/pdfs/<stem>.pdf`;
- `derived/fetch_failures.csv`.

**Assumptions:**
- A detail page's iframe `src` (unwrapped from `/web/?file=`) that ends in
  `.pdf` is the order.
- Inline text under 1,500 characters is page chrome.
- A body starting `%PDF-` is a PDF.

**Failure modes:**
- A non-PDF body is **cached** by `cache.get` (only WAF pages are rejected), so a
  re-run returns the same body; the status is NOT_A_PDF, and there are currently 0.
- `archive_pdf` returns early if the destination exists, so it never
  overwrites. Checked: 0 of 19,192 archived PDFs differ from their download hash.
- A PDF iframe whose URL does not end in `.pdf` falls through to NO_DOCUMENT.
- The process exits 0 even with failures.

### Stage 3: text (`src/extract_text.py`)

**Functions:**
- `extract_job` dispatches to `extract_pdf`, `extract_inline`, or a NO_SOURCE row.
- For each page: `page.get_text` → `clean_page_text` → `text_quality` →
  `image_cover` → (only for would-be sparse pages) `get_drawings` → `classify_page` →
  `ocr_page` for BROKEN_LAYER / SCANNED / VECTOR_TEXT pages.
- `ocr_page` renders with pymupdf, pipes to `tesseract … tsv`, parses the result
  with `parse_tesseract_tsv`, and caches it under `raw/ocr/`. If confidence is under
  70 it retries over 4 rotations × 2 dpi.
- Pages are joined with `\f`, and `write_atomic` writes `derived/text/<stem>.txt`.

**Input:** `order_documents.parquet`, `raw/pdfs/`, and cached detail pages for
inline orders (offline).

**Output:**
- `text_documents.parquet`: 20,861 rows, OK 20,849, NO_SOURCE 12;
- `text_pages.parquet`;
- 20,849 text files.

**Assumptions:**
- Page class decides whether OCR is needed: at least 100 good characters means
  the text layer is used as is.
- Running headers and footers stay in the text; later stages strip them.
- No dehyphenation.

**Failure modes:**
- Text inside images on a NATIVE page is **never OCR'd**, e.g. a noticee table
  pasted as an image in a digital order.
- A SPARSE page keeps its few native characters.
- OCR failure on one page makes the whole order FAILED (currently 0).
- Pending OCR means NEEDS_OCR and no text (currently 0).
- Inline HTML text is flattened to one line (no `\f`, no line breaks).

### Stage 4: window candidates and regex v1 (`src/extract_windows.py`)

**Functions:**
- Setup: `strip_repeated_lines` → `split_sentences` (with `_resplit_long` at 2,000
  characters) → `normalise_sentence`.
- Scoring: `score_sentence`, i.e. `find_windows` (`RANGE`, `parse_range`,
  `FY_RANGE`, `parse_fy`) → `inspection_visit` → `till_inspection` → `_labels` →
  `pick_window` → `extend_with_fy_list` → `confidence_for`.
- Selection: `extract_candidates` keeps the top 3 sentences and picks the first
  `high`, else the first `medium`.

**Input:** `text_documents.parquet` and the text files.

**Output:**
- `window_candidates.parquet` (28,052 rows for 13,228 orders);
- `windows.parquet`, with PARSED 10,516, NO_CANDIDATES 7,621, UNCONFIRMED_RANGE
  2,362, UNPARSED 350 and NO_TEXT 12.

**Assumptions:**
- The window is stated in one sentence that holds both a label and a date range.
- Numeric dates are day-first.
- An inspection *visit* (≤31 days, or ≤100 days if phrased "conducted the
  inspection during") is not the window.

**Failure modes:**
- Only the top 3 candidates survive, so a window in a 4th-ranked sentence is lost.
- A window stated across a sentence boundary is missed.
- An exception gives `window_status = NO_TEXT` with `error` set (currently 0).

### Stage 5: model fallback (`src/window_model.py`, `src/llm.py`)

**Functions:**
- `load_orders` selects the UNCONFIRMED_RANGE / UNPARSED orders together with
  their candidate sentences.
- `ask` calls `llm.chat_json`, which uses Ollama `qwen3.5:4b` with temperature 0
  and seed 7 and caches every answer under `raw/llm/` by a content hash.
- `read_answer` then checks the answer: `quoted_in`, `_OPEN_ENDED`,
  `resolve_date`, `_with_year` and `false_window_reason`.
- `combine` writes the result.

**Input:** `windows.parquet` and `window_candidates.parquet`.

**Output:**
- `window_model.parquet` (2,712 rows: MODEL_WINDOW 1,634, MODEL_NONE 907,
  MODEL_UNVERIFIED 135, MODEL_REJECTED 36, MODEL_ERROR 0; all from cache, one
  model digest);
- `violation_windows.parquet` (12,150 windows: regex 10,516, model 1,634).

**Assumptions:**
- The model only chooses and quotes; the code verifies the quote and parses the
  dates itself.
- A model window gets `violation_confidence = "medium"`.

**Failure modes:**
- The model can pick the wrong range; an agent review judged 42–45 of 50 right.
- NO_CANDIDATES orders are never sent.
- An offline cache miss becomes a MODEL_ERROR row, and the run does not fail.

### Stage 6: sanity rules and review (`src/check_windows.py`)

**Functions:** `flags_for` (R1 end after the order, R2 start after end, R3 lag ≤
0 or ≥ 20 years, R4 start before 1995), `earliest_end` (month-precision
leniency), `read_review`, `decisions`, `check`, `update_review`,
`extraction_rate` (Cramér's V), `iso_flags`.

**Input:** `violation_windows.parquet`, `hand/window_review.csv`, and listings
(for ISO flags).

**Output:**
- `windows_checked.parquet`, with PASS 12,107, NO_WINDOW 8,711, REVIEWED_KEEP
  33, REVIEWED_DROP 8 and REVIEWED_FIX 2; 12,142 windows;
- `window_flagged.csv`;
- `extraction_rate.csv`;
- appended rows in `hand/window_review.csv`.

**Assumptions:** a flagged window leaves the dataset unless it is reviewed
`keep` or `fix`.

**Failure modes:**
- Window length is never checked: 57 windows last 1 day and 42 last more than
  10 years.
- A decision for an order no longer flagged is ignored, with a warning.

### Stage 7: prior actions and sanctions (`src/extract_actions.py`, `src/amounts.py`)

**Prior actions:** `find_prior_mentions` applies `PRIOR_PATTERNS` (SCN, interim,
confirmatory) and `_AO`/`_AO_DATE` to the whole flattened text, then
`plausibility`.

**Sanctions:** `find_sanctions` works sentence by sentence and only from 40% of
the text onwards. It applies:
- `_IMPOSE` and `_NOT_OPERATIVE`;
- `_amount_mentions`, which calls `amounts.find_amounts(bare=True)` and then `_is_sanction_amount`;
- the disgorgement, restraint, registration, warning and no-penalty rules.

**Summary:** `summarise_order` calls `penalty_rows` (the restatement rule),
handles totals, "each" and joint penalties, and sets the outcome.

**Input:** text, and `windows_checked.violation_start` (for plausibility).

**Output:** `order_actions.parquet`, `prior_action_mentions.parquet` (51,925
rows) and `sanction_mentions.parquet` (27,622 rows).

**Failure modes:**
- A penalty table longer than the 2,000-character sentence cap is split from its
  "hereby impose" clause, so its amounts are ignored (AUDIT C-1).
- An ungrouped figure is truncated: "Rs. 500000" is read as ₹500 (AUDIT H-1).
- Separate, equal penalties are merged as "restatements" (AUDIT H-2).

### Stage 8: parties and regulations (`src/extract_parties.py`, `src/regulations.py`)

**Parties:**
- **Header list:** `header_end`, `noticee_segment` and `split_entries`
  (numbered, PAN/PIN-anchored, comma list, or single) → `clean_entry` →
  `_identifiers`.
- **Fallbacks, in order:** `definitions` ("hereinafter … Noticee"), then
  `title_parties`, then the matter company (AO / settlement / ED orders only),
  then a second "in the matter of" block.
- **Matter and roles:** `_dedupe` → `matter_from` → `issuer_from_matter` →
  `guess_role`.
- **Sanctions per party:** `link_penalties` and `link_other`.

**Regulations:** `find_aliases`, then `find_citations` sentence by sentence,
which uses `match_instrument`, `expand` and `role_for`.

**Input:**
- text;
- listings (titles);
- `sanction_mentions` (the Day 4 rows are re-read into `SanctionMention` objects);
- `order_actions` (the penalty total and basis).

**Output:** `order_entities.parquet`, `order_parties.parquet` and
`regulation_mentions.parquet`.

### Stage 9: the checkpoint table (`src/build_orders.py`)

**Functions:** `_with_stem` rebuilds `text_stem` for listings and documents.
`build` then left-joins documents, text, windows, actions and parties onto
listings on `text_stem`. Each join uses `validate="one_to_one"`, first checks
that the input holds no order missing from the listings, and asserts the row
count afterwards. `summarise` and `write` (atomic) finish.

**Output:** `derived/orders.parquet`.

**Failure mode:** an input missing some orders gives NA columns for them. That
is logged as "N listed orders without a row there", not raised (currently 0
everywhere).

---

## 3. File map

| File | Purpose | Key functions / classes | Called by | Calls | Inputs → outputs, side effects | Critical to final data? |
|---|---|---|---|---|---|---|
| `src/cache.py` | the only HTTP boundary; disk cache keyed by sha256(method, URL, params, form) | `fetch`, `get`, `post`, `cache_key`, `default_validator`, `_write_atomic` | scrape_listings, fetch_pdfs, extract_text (offline reads of inline pages) | httpx | writes `raw/cache/<sha256>`; sleeps 1.5 s per network call; module globals `OFFLINE`, `NETWORK_CALLS` | yes (everything upstream) |
| `src/scrape_listings.py` | stage 1 | `Category`, `CATEGORIES`, `build_form`, `parse_rows`, `clean_title_attr`, `scrape_category`, `deduplicate` | CLI | cache, bs4 | → `derived/listings.parquet`, `reports/*.csv`, `logs/scrape_listings.log` | yes: sample frame, `order_type`, `order_date`, title |
| `src/fetch_pdfs.py` | stage 2 | `extract_document_ref`, `resolve_document`, `process_row`, `archive_name`, `archive_pdf`, `extract_inline_text` | CLI; `archive_name` and `extract_inline_text` are imported by extract_text | cache, bs4 | → `order_documents.parquet`, `raw/pdfs/` (hard links), `fetch_failures.csv` | yes: which document is "the order" |
| `src/extract_text.py` | stage 3; also shared helpers | `extract_pdf`, `classify_page`, `ocr_page`, `clean_page_text`, `clean_inline_text`, **`text_stem`**, **`assert_safe_output`**, **`setup_logging`**, `write_atomic` | CLI; helpers imported by every later stage | pymupdf, tesseract (subprocess), cache (offline) | → `derived/text/*.txt`, `text_documents`, `text_pages`, `raw/ocr/` | yes: all text |
| `src/extract_windows.py` | stage 4; also shared text utilities | `strip_repeated_lines`, `split_sentences`, `normalise_sentence`, `DATE`, `_FULL`, `parse_date`, `RANGE`, `find_windows`, `inspection_visit`, `score_sentence`, `extract_candidates` | CLI; its utilities are imported by window_model, extract_actions, extract_parties, regulations tests | — | → `window_candidates`, `windows` | yes: windows; its sentence splitter feeds sanctions and regulations |
| `src/llm.py` | cached local-model calls | `ModelSpec`, `chat_json`, `cache_key`, `model_digest` | window_model | ollama (localhost) | `raw/llm/<key[:2]>/<key>.json` | yes for 1,632 windows |
| `src/window_model.py` | stage 5 | `SYSTEM`, `SCHEMA`, `read_answer`, `resolve_date`, `quoted_in`, `false_window_reason`, `combine`, `_cached_digest` | CLI | llm, extract_windows | → `window_model`, `violation_windows` | yes |
| `src/check_windows.py` | stage 6 | `flags_for`, `earliest_end`, `check`, `decisions`, `update_review`, `extraction_rate` | CLI | pandas, scipy | → `windows_checked`, `window_flagged.csv`, `extraction_rate.csv`; **appends to `hand/window_review.csv`** | yes: the final window |
| `src/amounts.py` | rupee and duration normaliser | `AMOUNT`, `BARE_AMOUNT`, `parse_figure`, `find_amounts`, `words_to_number`, `malformed`, `duration_months` | extract_actions | — | pure | yes: every amount |
| `src/extract_actions.py` | stage 7 | `PRIOR_PATTERNS`, `find_prior_mentions`, `plausibility`, `_IMPOSE`, `find_sanctions`, `penalty_rows`, `summarise_order`, `SanctionMention`, `_write` | CLI; extract_parties imports `SanctionMention`, `penalty_rows`, `_write` and the constants | amounts, extract_windows | → `order_actions`, `prior_action_mentions`, `sanction_mentions` | yes |
| `src/regulations.py` | citation parser | `match_instrument`, `find_aliases`, `find_citations`, `expand`, `role_for`, `as_list` | extract_parties | — | pure | yes: `regulations` |
| `src/extract_parties.py` | stage 8 | `header_parties`, `split_entries`, `clean_entry`, `guess_role`, `link_penalties`, `link_other`, `process` | CLI | extract_actions, extract_windows, regulations | → `order_entities`, `order_parties`, `regulation_mentions` | yes |
| `src/build_orders.py` | stage 9 | `build`, `summarise`, `write` | CLI | pandas | → `orders.parquet` | yes |
| `src/validation_sample.py` | Week 3 blind sample | `stratum`, `draw`, `systematic`, `sheet`, `key` | CLI (not yet run) | pandas | → `hand/validation_sample.csv`, `derived/validation_key.parquet` | no (validation) |
| `tests/` | 591 tests (pytest), fixtures from real pages and sentences | — | pytest | all modules | none on real data | guards regressions only |
| `hand/window_review.csv` | your keep/fix/drop decisions on flagged windows | — | check_windows | — | read and appended | yes (43 windows) |
| `hand/window_model_devset.csv`, `…holdout.csv` | 90 agent-labelled orders | — | validation_sample (exclusion) | — | — | no |
| `requirements.txt`, `.gitignore` | environment; `raw/`, `derived/`, `logs/`, `reports/` untracked | — | — | — | — | no |

There are no SQL files, schema files or notebooks. The schema lives in the
dataclasses and the `pa.schema(...)` constants of each stage.

---

## 4. Entry points and execution order

Every stage is a module with `main()` (`python -m src.<name>`). Nothing chains
them; you run them in order. Each writes `logs/<stage>.log`, and most accept
`--scratch DIR` (dry run), `--category`, `--order-id`, `--sample` and
`--limit`.

1. `python -m src.scrape_listings [--offline] [--force]`
2. `SEBI_CONTACT=… python -m src.fetch_pdfs [--offline]`
3. `python -m src.extract_text` (OCR from `raw/ocr/` when cached)
4. `python -m src.extract_windows`
5. `python -m src.window_model --offline`: replays `raw/llm/`. Without
   `--offline` it needs Ollama serving `qwen3.5:4b`. It writes
   `violation_windows.parquet` only on a full run (no `--sample`, `--limit`,
   `--order-id`, `--stem` or `--status`).
6. `python -m src.check_windows` (applies `hand/window_review.csv`)
7. `python -m src.extract_actions`
8. `python -m src.extract_parties`
9. `python -m src.build_orders`
10. (Week 3) `python -m src.validation_sample`

**Order dependencies that are not enforced.**
- Stage 7 reads `windows_checked` for plausibility, so re-running stage 6 means
  stage 7 must be re-run.
- Stage 8 reads stage 7's `sanction_mentions` and `order_actions`.
- Stage 9 reads everything.
- No stage checks that its inputs are newer than its outputs, or come from the
  same code version. The `rules_version` columns (`regex-v1.1`, `actions-v1`,
  `parties-v1`) are the only markers.

The current `derived/` timestamps are in dependency order:
- text 09-24;
- windows 09-27 15:10;
- checked 16:15;
- actions 16:50;
- parties and orders 09-28 07:21.

---

## 5. Data lineage (the fields you will analyse)

"Source" is where the value physically comes from. "Chain" is the code path.
**Missing** says when the field is null.

| Field | Source | Chain (extraction → transformation → insertion) | Missing / default behaviour |
|---|---|---|---|
| `order_id` | listing link URL | `scrape_listings.extract_order_id` (`_(\d+)\.html?$`) → listings → build_orders join | null for 76 listings (74 direct PDFs, 2 odd pages) |
| `text_stem` | derived | `extract_text.text_stem(order_id, source_url)` = `str(id)` or `noid_<sha12>` | never null |
| `order_date` | listing page's date cell (SEBI's publication date) | `parse_order_date` (formats `%b %d, %Y` …) | never null now. **It is the listing date, not the date signed in the order**: they agree in 91% of orders with a dated footer, but only 33% of settlement orders (see AUDIT H-3) |
| `order_type` | listing category | `Category.order_type` (never from the title) | never null |
| `title`, `sat_appeals`, `corrigendum_url` | listing `title` attribute | `clean_title_attr` strips HTML, keeps SAT-appeal text *inside the title*, and puts the appeal numbers in `sat_appeals` | SAT annotations only exist for 2003–2022 orders |
| `source_url`, `pdf_url`, `doc_shape` | listing / detail page | `extract_document_ref` (iframe → inline → anchor) | `pdf_url` null for inline, no-document and failed orders |
| `text_path`, `has_text_layer`, `n_pages`, `n_ocr_done`, `mean_ocr_conf`, `text_status` | the PDF / inline page | `extract_pdf` / `extract_inline` | `has_text_layer` = no page needed OCR; `n_pages` null for inline orders |
| `violation_start`, `violation_end` | one sentence of the order text | regex: `split_sentences` → `score_sentence` → `find_windows`/`parse_range` → `pick_window` → `extract_candidates` **or** model: `ask` → `read_answer` → `resolve_date`, then `combine` → `check_windows.check` (flagged ones set to null unless reviewed; `fix` replaces them) | null for 8,707 orders with text: no candidate, model said none or unverified, or flagged-and-dropped. Never imputed. Month precision resolves to the 1st and last day of the month; a year to 1 Jan and 31 Dec; a financial year to 1 Apr and 31 Mar |
| `start_precision`, `end_precision` | derived | `ParsedDate.precision` / `Resolved.precision`; `hand` fixes are set to `day` | with the window |
| `violation_raw` | the sentence (whitespace collapsed) | the candidate sentence (regex) or `order.sentences[n-1]` (model) | `check()` nulls only the two dates of a FLAGGED / REVIEWED_DROP window; `violation_raw`, `violation_method`, `violation_confidence` (high / medium) and `window_kind` stay populated on those 8 rows (AUDIT M-9) |
| `violation_confidence` | derived | regex `confidence_for` (high/medium); model is always `medium`; hand fix `high`; no window `low` | never `none` (the runbook's "none" is not implemented) |
| `violation_method`, `violation_rule`, `window_kind` | derived | `regex` / `model` / `hand`; the rule is the parse rule or `model:<kind>`; the kind is `stated_period` for regex, the model's word mapped by `KIND_NAMES` | null without a window |
| `sanity_status`, `sanity_flags` | derived | `check_windows.flags_for` / `check` | `NO_WINDOW` when there is no window |
| `prior_action_date`, `prior_action_kind`, `prior_action_raw` | the whole text (flattened) | `find_prior_mentions` (regexes `PRIOR_PATTERNS`, dates via `ew.DATE`/`parse_date`, `as_start`) → `plausibility` (≥ 1992-01-30, ≤ order date, ≥ window start) → earliest (by date, then position) | null for 3,931 orders with text. Month-only dates resolve to the 1st of the month |
| `earliest_scn_date`, `earliest_interim_date`, `ao_appointment_date` | same | same, by kind | null when not recited |
| `first_action_date`, `first_action_source` | derived | `min(prior_action_date, order_date)` | falls back to `order_date` (`first_action_source = order`) for 3,940 orders |
| `penalty_total`, `penalty_total_basis`, `penalty_n`, `penalty_max`, `penalty_raw` | operative sentences (≥40% of the text) | `find_sanctions` (`_IMPOSE`, `_NOT_OPERATIVE`) → `amounts.find_amounts(bare=True)` → `_is_sanction_amount` → `penalty_rows` (restatements dropped, totals excluded) → sum; a stated total is used only with "each" | null = no imposing sentence with an amount found (not the same as "no penalty"; see `penalty_outcome`) |
| `penalty_outcome` | derived | `penalty` if a total, else `no_penalty` if `_NO_PENALTY` matched, else `unknown` | `unknown` for 9,512 orders |
| `settlement_amount`, `disgorgement_total` | sentences with settle+pay words (settlement orders, anywhere in the text) / disgorge + directive | **sum over distinct (sentence, amount) pairs**, so the same amount restated in several sentences is added once per sentence: settlement amounts are inflated (median 2×; AUDIT C-2) | null when none found |
| `debarred`, `debarment_months_max`, `debarment_indefinite`, `registration_action`, `warned` | operative sentences | `_RESTRAIN`, `_PERIOD_OF` → `duration_months`, `_INDEFINITE`, `_REGISTRATION`, `_WARNING` | `False` / null when not found (**absence is recorded as `False`, not unknown**) |
| `matter_raw`, `issuer_guess`, `issuer_source` | title (preferred), else the header's "In the matter of" | `matter_from` → `issuer_from_matter` (last "shares/scrip/IPO … of X", else prefix-strip; cut at Ltd/Limited/Corporation/Company/Industries/LLP) → else the first party with a non-weak ISSUER role | `matter_raw` includes SAT-appeal text for titles carrying it (1,414 titles) |
| `n_parties`, `n_parties_by_role`, `parties_source` | header (or fallbacks) | `header_parties` / fallbacks → `_dedupe` → `guess_role` | 0 for 1,398 orders with text |
| `penalty_link_status`, `penalty_total_resolved` | derived | `link_penalties`; the resolved total differs from `penalty_total` only for fully linked "each" orders | null when there are no penalty rows |
| `regulations`, `regimes`, `regimes_cited`, `penalty_provisions` | every sentence | `find_aliases` → `find_citations` (`_CITATION`, `expand`, `match_instrument`, `role_for`, the violation cue in the 250 characters before, chaining) → `as_list` / `regimes` → JSON | `"[]"` when there is none (16,865 orders have a non-empty `regulations`) |
| **order_entities** `entity_name` | header entry / definition / title | `clean_entry` (junk stripped, cut at PAN/address/role) | — |
| `entity_name_clean` | derived | `name_clean` (upper case, honorifics and "(formerly…)" dropped, LTD→LIMITED, PVT→PRIVATE) | — |
| `pan`, `din`, `cin`, `sebi_reg_no` | the party's header entry | `_identifiers` regexes; for a single header party with no PAN, the **first PAN anywhere in the header** is used | null when not printed |
| `entity_type` | PAN 4th letter, else the name | `entity_type`; `org` flag for "X and its directors" | defaults to `individual` |
| `entity_role`, `role_basis` | row cells, first body mention (±80/+250 characters), matter | `guess_role` precedence (see logic notes) | `UNKNOWN` (33.6%); company with no evidence → `ISSUER` / `suffix` |
| `penalty_amount`, `penalty_rows`, `penalty_each`, `penalty_joint` | Day 4 penalty rows | `link_penalties` (table vs prose layout, pending rows, "each", generic "all the Noticees" → joint) | null when no row was linked to the party |
| `sanction_types` | Day 4 mentions | `link_other` + PENALTY | `[]` |

---

## 6. Data transformations (plain English)

**Text normalisation** (`clean_page_text`, `normalise_sentence`):
- Non-breaking and figure spaces become spaces.
- Line and paragraph separators become newlines.
- The BOM, zero-width space, word joiner and soft hyphen are deleted.
- Form feed is reserved for page breaks.
- Ligatures are expanded by pymupdf.
- There is no dehyphenation.
- Sentence text collapses all whitespace to single spaces.

**Header/footer stripping** (`strip_repeated_lines`):
- A line counts as a header or footer if it sits in the first or last 5
  non-blank lines of at least half the pages, and of at least 3 pages. Digits
  are masked first, so "Page 3 of 11" becomes "page # of #".
- Such a line is overwritten with spaces, so offsets are unchanged.
- Nothing happens for documents under 3 pages, or for inline orders (no `\f`).

**Sentences** (`split_sentences`):
- A sentence ends at `.?!` (plus closing quotes or brackets) when the next
  token starts with a capital, a digit, a quote or a bracket, unless the full
  stop ends a known abbreviation or an initial.
- A sentence longer than 2,000 characters is re-split at blank lines, then at
  line breaks. **This re-split separates long penalty tables from their clause
  (AUDIT C-1).**

**Dates** (`parse_date`, `RANGE`, `FY_RANGE`, `resolve_date`):
- An explicit grammar, not dateutil. Numeric dates are always day-first.
- 2-digit years: 80–99 → 19xx, otherwise 20xx.
- Years must fall in 1980–2030, and days are checked against the calendar.
- A month with no day is "month precision": the start is the 1st, the end the
  last day.
- A range whose start lacks a year takes the end's year, or the year before if
  that would put the start after the end.
- "FY 2011-12" → 1 Apr 2011 to 31 Mar 2012.

**Entity extraction:**
- Parties: see stage 8 and the logic notes.
- Regulations: provision lists expand ("3(a), (b)" → 3(a), 3(b)), and an
  instrument is resolved by title, abbreviation, the order's own alias, or a
  generic word.

**Classification:**
- `order_type` comes from the category.
- `window_kind` comes from the model's word.
- `confidence` comes from the label strength.
- `penalty_outcome`, entity roles and citation roles are rule-based. There is
  **no fraud-category or detection-channel classification yet** (Week 3).

**De-duplication:**
- Listings are de-duplicated by `order_id`, or by URL when there is no id.
- Prior-action mentions are de-duplicated by (kind, date, 200-character block).
- Penalty "restatements" are dropped (`penalty_rows`).
- Settlement and disgorgement amounts are de-duplicated by (sentence, amount).
- Parties are de-duplicated by `entity_name_clean`.
- **Orders are never de-duplicated against each other**: 215 orders share their
  exact text with another.

**Filtering:**
- The four categories only.
- Sanctions only from 40% of the text onwards.
- Sentences must pass `_PREFILTER` to be window candidates.
- Only the top 3 candidates are kept.
- Only UNCONFIRMED_RANGE and UNPARSED orders go to the model.
- Flagged windows are removed unless reviewed.
- Implausible prior-action dates are kept in the mentions table but not used.

**Merging:**
- `combine` takes the regex window if PARSED, else the verified model window.
- `build_orders` does left joins onto listings, one-to-one on `text_stem`.

**Missing values:**
- Dates, amounts and names are never imputed.
- The fallbacks that *are* substitutions are:
  - `first_action_date` → `order_date`;
  - a single party's PAN → the first PAN in the header;
  - a company role → ISSUER;
  - `debarred` / `warned` → False.

---

## 7. External data fetching

**Sources.**
- **SEBI listing endpoint:** `https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp` (POST, form).
- **Detail pages:** `…/enforcement/orders/<mon-yyyy>/<slug>_<id>.html` (GET).
- **PDFs:** `…/sebi_data/attachdocs/<epoch-ms>.pdf` and `…/commondocs/…` (GET).
- **Local model:** Ollama at `localhost:11434`, via `llm.py` only.
- No other APIs.

**Requests** (`cache.fetch`):
- A browser User-Agent, with an optional `SEBI_CONTACT` suffix.
- Listing requests add `Content-type`, `Origin` and `Referer`; detail and PDF
  requests add a `Referer`.
- No cookies.
- Redirects are not followed; httpx raises on 3xx, so a redirect becomes a
  FetchError.

**Pagination:** `doDirect = page-1` with `next=n`. The loop runs to the hidden
`totalpage` (leading digits parsed), with a 2,000-page safety cap. If
`totalpage` is unreadable, it walks until the first empty page.

**Timeouts and retries:**
- 60 s timeout.
- 3 retries with backoff of 3, 6 and 12 s, plus the 1.5 s delay before every
  network attempt.
- Retried statuses: 408, 425, 429, 500, 502, 503, 504, 520, 522, 524 and 530
  (the WAF).
- Any other 4xx is a permanent FetchError.

**Rate limiting:** sequential only, 1.5 s before each network call; cache hits
never sleep.

**Caching:**
- `raw/cache/<sha256(method\nurl\nparams\nform\nextra)>`, written atomically,
  append-only.
- Headers are deliberately not in the key.
- Nothing expires: a cached page is returned forever unless `--force` is given.
  That is right for immutable PDFs, and stale for listing pages.

**Download validation:**
- The cache rejects bodies whose first 4 KB contain SEBI's WAF messages.
- `fetch_pdfs` rejects bodies that don't start with `%PDF-`; those are recorded
  NOT_A_PDF but **remain cached**.
- Empty bodies are accepted and cached.

**When a request fails:**
- **Listing page:** logged, recorded `failed` in `reports/listing_pages.csv`,
  and skipped; the run exits 1.
- **Detail page or PDF:** a status row (FAILED_DETAIL / FAILED_DOWNLOAD) in
  `order_documents` and `fetch_failures.csv`; the run exits 0.

**Can the pipeline continue silently after a failed download?** Yes, by design:
- The order stays in every table with `text_status = NO_SOURCE` and null
  extractions (currently 12 orders).
- It is not "silent" in the files (each has a status), but nothing stops a
  downstream stage or warns in `build_orders`.

---

## 8. Output structure

Legend:
- **E**: directly extracted from source text or pages.
- **D**: derived by computation.
- **C**: classified by rules.
- **I**: inferred (a heuristic guess).
- **F**: a defaulted or fallback value can appear.
- **M**: potentially missing.

### `derived/orders.parquet` (20,861 × 62)

| Column(s) | Meaning | Kind |
|---|---|---|
| `text_stem` | row key (`order_id` or `noid_…`) | D |
| `order_id` | SEBI's id from the URL | E, M (76) |
| `order_date` | SEBI's listing (publication) date | E |
| `order_type`, `listing_category` | category (WTM / AO / SETTLEMENT / ED_CGM) | E |
| `title`, `sat_appeals`, `corrigendum_url`, `source_url` | listing metadata | E, M |
| `pdf_url`, `doc_shape` | the document the pipeline treats as the order | E, M |
| `text_status`, `text_path`, `has_text_layer`, `n_pages`, `n_ocr_done`, `mean_ocr_conf` | text provenance | D, M |
| `window_status` | regex routing (PARSED / UNCONFIRMED_RANGE / UNPARSED / NO_CANDIDATES / NO_TEXT) | C |
| `violation_start`, `violation_end` | suspected violation window | E + I (model), M (42% of orders with text) |
| `start_precision`, `end_precision` | day / month / year / fy | D |
| `violation_raw` | source sentence | E, M |
| `violation_confidence` | high / medium / low | C, F (`low` when there is no window) |
| `violation_method`, `violation_rule`, `window_kind` | how the window was found and stated | C |
| `sanity_status`, `sanity_flags` | rule outcome | C |
| `prior_action_date`, `prior_action_kind`, `prior_action_raw` | earliest plausible SCN / interim / confirmatory date | E + C, M |
| `earliest_scn_date`, `earliest_interim_date`, `ao_appointment_date` | by kind | E, M |
| `first_action_date`, `first_action_source` | `min(prior, order)` | D, F |
| `penalty_total`, `penalty_total_basis`, `penalty_n`, `penalty_max`, `penalty_raw` | penalty imposed by the order | E + D, M |
| `penalty_outcome` | penalty / no_penalty / unknown | C |
| `settlement_amount`, `disgorgement_total` | ₹ | E + D, M |
| `debarred`, `debarment_months_max`, `debarment_indefinite`, `registration_action`, `warned` | non-monetary sanctions | C, F (`False` = not found) |
| `matter_raw`, `issuer_guess`, `issuer_source` | matter text and guessed issuer | E + I, M |
| `n_parties`, `n_parties_by_role`, `parties_source` | party counts (JSON by role) | D + I |
| `penalty_link_status`, `penalty_total_resolved` | how penalties map to parties | D |
| `regulations`, `regimes`, `regimes_cited`, `penalty_provisions` | JSON lists | E + C |

### `derived/order_entities.parquet` (47,972)

| Column(s) | Meaning | Kind |
|---|---|---|
| `text_stem`, `order_id`, `order_type`, `order_date` | the order the party belongs to | E |
| `party_no`, `noticee_no` | position in the list / serial printed | D / E |
| `entity_name`, `entity_name_clean` | name as printed / normalised | E / D |
| `entity_type` | individual, company, firm, HUF, trust or other | I, F |
| `pan`, `din`, `cin`, `sebi_reg_no` | identifiers | E, F (the single-party PAN fallback), M |
| `entity_role`, `role_basis` | role guess and its evidence | I, F |
| `is_issuer_guess` | name matches `issuer_guess` | I |
| `source` | where the party was found | D |
| `aliases` | JSON | E |
| `penalty_amount`, `penalty_rows`, `penalty_each`, `penalty_joint` | penalty linked to this party | D + I, M |
| `sanction_types` | JSON | I |

The other tables are described in CLAUDE.md §8. They are all stage outputs;
`orders.parquet` is built only from them.

---

## 9. One record traced end to end: order 46128

The order is an adjudication order dated 28 Feb 2020 against 6 noticees in the
matter of Aksh Optifibre Limited's GDR issue. It was chosen because it
exercises every stage: a noticee table, a stated window, an SCN, a penalty
table, per-party penalties and regulations.

**SOURCE.** Listing AO page (smid 6), row `Feb 28, 2020 | Adjudication Order in
the matter of GDR issue by Aksh Optifibre Limited` with a SAT-appeal HTML
suffix. `scrape_listings.parse_rows` produces:
- `order_id` 46128;
- `order_date` 2020-02-28 (from the listing cell);
- `title` with "[SAT Appeal No.: 440/2020 …]" **kept inside the title** by `clean_title_attr`;
- `sat_appeals` "440/2020 …; 438/2020 …; 439/2020 …".

*Loss points:* the title now carries appeal text (it later leaks into
`matter_raw`), and the date is the listing date.

**RAW DATA.**
- `fetch_pdfs.resolve_document` fetches the detail page through the cache, and
  `extract_document_ref` finds an iframe pointing to
  `/sebi_data/attachdocs/feb-2020/1582887604869_2.pdf`, so `doc_shape` is
  IFRAME_PDF.
- `download_document`, `is_pdf_bytes` and `archive_pdf` give
  `raw/pdfs/46128.pdf` (610,289 bytes, sha256 `1070286f…`).

*Loss point:* if SEBI's iframe pointed at an annexure, the wrong document would
be "the order".

**PARSED DATA.** `extract_text.extract_pdf` finds 31 pages, all NATIVE (≥100
good characters each), so there is no OCR. `text_chars` is 73,797 and the
stopword ratio 0.40. The pages are joined with `\f` into `derived/text/46128.txt`.

*Loss point:* any image-embedded text on those pages is dropped.

**NORMALISED DATA.** `strip_repeated_lines` blanks the running header lines;
`split_sentences` then cuts the text into sentences.

The ORDER paragraph ("…I, in exercise of the powers conferred upon me … hereby
impose the following penalty on the Noticees – [table]") runs 2,454 characters
with its table. `_resplit_long` cuts it at a blank line:
- **sentence 311** (539 characters) ends at "…hereby impose the following penalty
  on the Noticees – Noticee Violation Penal Provisions Penalty (Rs.)";
- **sentence 312** (1,691 characters) holds the rows "Noticee 1 (Aksh Optifibre
  Limited) … Rs. 10,00,00,000 … Rs. 15,00,000/- …".

*This is where information is lost* (see below).

**CLASSIFICATION and EXTRACTION.**
- **Window:**
  - `score_sentence` on the first sentence of the order, "SEBI conducted
    investigations into … GDRs issued by Aksh Optifibre Limited … during the
    period August 01, 2010 to September 30, 2010 (hereinafter referred to as
    'Investigation Period')", finds the label DEFINED and the range `period
    August 01, 2010 to September 30, 2010`, so the rule is `range` and the
    confidence `high`, with score 12 (rank 1).
  - Ranks 2 and 3 are generic "period August-September 2010" sentences.
  - `windows.parquet`: PARSED, 2010-08-01 to 2010-09-30.
  - The model is not asked. `check_windows` flags nothing (end 2010-09-30 is
    before the order; lag 9.4 years), so the status is PASS.
- **Prior actions:** `find_prior_mentions` over the flat text finds "Show Cause
  Notice dated June 08, 2018" (scn) and AO appointment dates (15 May 2018, 6 Jul
  2018). `prior_action_date` is therefore 2018-06-08 and `first_action_date`
  2018-06-08.
- **Sanctions:** `find_sanctions` sees `_IMPOSE` in sentence 311 but no amounts
  there. Sentence 312 has the amounts but no imposing words, so it is ignored.
  The result is **no penalty**: `penalty_total` null and `penalty_outcome`
  "unknown". The order imposes at least ₹10 crore + ₹15 lakh on Noticee 1, ₹20
  lakh on Noticee 2 and ₹10 lakh on Noticees 3 and 4 (and more on 5–6), a
  table worth about ₹11 crore that is entirely missing.
- **Parties:**
  - `noticee_segment` starts after "In respect of:" and ends at "In the matter
    of"; `split_entries` recognises a numbered table (`1 M/s. Aksh Optifibre
    Limited AAACA0062F 2 Dr. Kailash S Chaudhary AAHPC7797B …`), giving 6 parties
    with PANs.
  - `guess_role` makes Aksh ISSUER (it matches the matter company) and the 5
    individuals UNKNOWN: they are named in the body as "Noticee 2", not by name
    near role words, and the matter "GDR issue by …" is not a trading matter.
  - Because no penalty rows exist, every `penalty_amount` is null and
    `penalty_link_status` is null.
- **Regulations:** 231 citations: 130 violation, 62 cited, 21 power, 18
  penalty. The `regulations` list begins with SEBI Act 12A(a)–(c) and PFUTP
  3(a)–(d), 4(1) …, and `penalty_provisions` includes SEBI Act 15HA and SCRA 23E.
- **Matter:** `matter_raw` is "GDR issue by Aksh Optifibre Limited [SAT Appeal
  No.: 440/2020 & …]" (title-derived, appeal text included); `issuer_guess` is
  "Aksh Optifibre Limited".

**DATABASE RECORD.** `build_orders` joins the six tables on `text_stem` = "46128"
into one `orders.parquet` row, and `order_entities` holds 6 rows.

**Where this record lost or changed information:**
1. The penalty table was split from its clause, so the penalty is **missing**
   (a silent false negative; the outcome reads "unknown", not an error).
2. The director roles are UNKNOWN although the text calls them directors
   (e.g. "Noticee 2 in the capacity of Managing Director").
3. `matter_raw` carries SAT-appeal text.
4. `order_date` is the listing date (it matches the signed date here).
5. `first_action_date` is the entity's SCN (2018). Here the matter's first
   public action may have been earlier; nothing in this order says.
