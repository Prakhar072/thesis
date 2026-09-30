# CLAUDE.md — briefing for AI agents working on this repository

Last updated: 2026-09-30 (audit fixes; Week 3, Days 1–2 built). Read this before touching code or data.
Companion docs: `timeline.md` (the thesis runbook: weekly plan, 8 rules, target
schema), `technical_plan.md` (the thesis design: stages, estimators, hypotheses),
`results_log.md` (every published figure with provenance), `hand/recon_notes.md`
(verified SEBI protocol notes and how orders state the window), `hand/coding_protocol.md`
(the coding rules for window, fraud category, issuer, detection channel), `audit/`
(the 2026-09-28 audit and its fix status), `PROJECT_GUIDE.md` (the human-facing guide).

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

## 2. Status (audit fixes and Week 3, Days 1–2 built)

**Pending (the user runs; no network except the local model), in order:**
`python -m src.extract_parties` (the §15J fix, ~1 min) → `python -m src.classify_fraud` (fraud-v2, ~1.5 min)
→ `python -m src.detect_channel` (rules ~1.3 min, then the local-model pass on ~5,300 orders, ~1.5–2.5 h; Ollama
must be serving; `--no-model` gives rules only) → `python -m src.build_orders`. The regeneration after the audit
fixes was done on 2026-09-30 15:09–15:12 and committed (78a8eab); `derived/` holds fraud-v1 and no
`order_channels.parquet` until these run (so `build_orders` fails loudly).

**Week 3 Day 2 (detection channel):** protocol §5 was written first. **User decisions (2026-09-30):**
- INSPECTION becomes an eighth channel;
- "SEBI / an exchange observed <a pattern or filing>" is SURVEILLANCE, flagged (`trading_pattern_observed`,
  `filings_observed`);
- an exchange reference with no stated basis is UNCLEAR (`exchange_reference`).

`src/detect_channel.py` (stage 10) writes `derived/order_channels.parquet`. Rules only (scratch):
- UNCLEAR 13,129 (63.0%)
- SURVEILLANCE 3,343
- COMPLAINT 2,553
- INSPECTION 1,239
- REFERRAL 473
- MEDIA 77
- SELF_DISCLOSURE 25
- AUDIT 10

On the 58-order dev set: 54 of 58 right, and all 27 rule decisions right (tuned). The model is a cue-verified,
medium-confidence fallback (`detection_method = model`). **458 orders read while building are excluded from the
blind sample** (`validation_sample.SEEN`, including `hand/seen_while_building.csv`).

**Audit fixes (2026-09-30)** (`audit/AUDIT_REPORT.md`, "Status after fixes"; figures in `results_log.md`),
all local to stages 6–8 and scratch-verified on the full chain:
- **C-1:** penalty tables split from their "hereby impose" clause are read. Penalty orders 8,039 → 8,473, sum
  ₹11,806 → ₹12,485 crore.
- **C-2:** a settlement amount restated in several sentences is counted once. Sum ₹1,913 → ₹1,460 crore.
- **H-1:** whole digit runs are read; Satyam disgorgement ₹1,258.88 crore.
- **H-2:** stated totals are used.
- **M-3:** appeal text is removed from `matter_raw`.
- **M-9:** out-of-dataset window rows carry no descriptors.
- **New:** a single party takes a stated total; `issuer_guess` drops "Inspection of Books of" and similar
  prefixes.

**Week 3 Day 1 (fraud category):** `hand/coding_protocol.md` was written before coding.
`src/classify_fraud.py` (stage 9) writes `derived/order_categories.parquet`, and `build_orders` (stage 10)
joins four of its columns. Categories under **fraud-v2** (after the Day 1 check; scratch-verified):
- MM 7,486
- DISCLOSURE 4,458
- OTHER 4,291
- INTERMEDIARY 3,322
- INSIDER 572
- FUND 342
- ACCOUNTING 232
- RPT 146

Confidence: high 12,679 · medium 3,481 · low 4,689 (959 unresolved). Dev agreement is 75 of 81, but it was
tuned on those orders. A fresh 16-order check before the fixes found 11 of 16 right; after them, 16 of 16 on
another fresh 16. **User decision (2026-09-30):** a company's own funds siphoned to insiders are
RPT_TUNNELLING, and FUND_DIVERSION is issue money only. `derived/orders.parquet`: 20,861 × 66.

The rest of this section is the Week 2 record; its sanction and party figures are pre-fix.

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
Run into `derived/` on 2026-09-27.

**Day 5 (regulations, parties, Week 2 checkpoint)**: `src/regulations.py` +
`src/extract_parties.py` (stage 8) + `src/build_orders.py` (stage 9),
scratch-verified on the full corpus (0.7 min + 1 s). Parties for 19,451 of 20,849
orders with text (93.3%; AO 99.4%, settlement 98.3%, ED/CGM 96.8%, WTM 80.0%):
47,972 party rows, 67% with a PAN; roles are a first guess (33.6% UNKNOWN, mostly
individuals in mass orders named only in the header). Penalties linked to parties
in 7,819 of 8,039 penalty orders (97.3%); per-party amounts sum to the order total
in all 7,320 checkable orders. Violated provisions (`regulations`, the runbook's JSON
list) for 16,865 orders (80.9%): PFUTP 8,451, SEBI Act 4,532, SAST 2,610, PIT 2,223.
Agent review (not blind): party lists right in 15 of 18 random orders with parties.
`derived/orders.parquet`: 20,861 × 62. **Pending:** the user runs
`python -m src.extract_parties` then `python -m src.build_orders`.

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
8. Keep `python -m pytest tests -q` green (696 tests at time of writing).
9. Record any new figure in `results_log.md` with date, script and scope.
10. **`hand/coding_protocol.md` changes only through its change log** (date, section, change, reason,
    orders already coded that are affected). Once validation coding starts, a rule change means
    re-coding the affected orders; never edit a rule silently.

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
| 8 parties | `python -m src.extract_parties` | text_documents, `derived/text/`, listings, sanction_mentions, order_actions | `derived/order_entities.parquet`, `derived/order_parties.parquet`, `derived/regulation_mentions.parquet`, `logs/extract_parties.log` | none (~1 min) |
| 9 category | `python -m src.classify_fraud` | order_parties, order_actions, listings, text_documents, `derived/text/` | `derived/order_categories.parquet`, `logs/classify_fraud.log` | none (~1.5 min) |
| 10 channel | `python -m src.detect_channel [--no-model] [--offline]` | listings, text_documents, `derived/text/` | `derived/order_channels.parquet`, `raw/llm/`, `logs/detect_channel.log` | localhost Ollama only (rules ~1.3 min; model ~1.5–2.5 h, seconds when cached) |
| 11 orders | `python -m src.build_orders` | listings, order_documents, text_documents, windows_checked, order_actions, order_parties, order_categories, order_channels | `derived/orders.parquet`, `logs/build_orders.log` | none (seconds) |

Full offline rebuild of `derived/` from `raw/` (no network):
`scrape_listings --offline` → `fetch_pdfs --offline` → `extract_text` → `extract_windows`
→ `window_model --offline` (answers from `raw/llm/`; `model_calls=0` proves it) → `check_windows`
→ `extract_actions` → `extract_parties` → `classify_fraud` → `detect_channel --offline` → `build_orders`.
Scratch dry run of stages 6–10: symlink `listings`, `order_documents`, `text_documents` and the
`violation_windows` input into DIR, copy `hand/window_review.csv`, then pass `--scratch DIR` (and
`--derived DIR` / `--sanctions` / `--actions` where the stage takes them).
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

`derived/order_entities.parquet` — one row per (order, party) (47,972); the runbook's `order_entities`
: `party_no` (1-based, header order), `noticee_no` (the serial the order prints, or from a
"Noticee No. 2" definition), `entity_name` (as printed), `entity_name_clean` (upper case,
honorifics and "(formerly …)" dropped, Ltd → LIMITED, Pvt → PRIVATE), `entity_type` ∈
`individual, company, firm, huf, trust, other` (PAN 4th letter first, else the name), `pan`,
`din`, `cin`, `sebi_reg_no`, `entity_role` ∈ `ISSUER, PROMOTER, DIRECTOR, AUDITOR,
INTERMEDIARY, TRADER, UNKNOWN` (first guess), `role_basis` (the evidence: `registration_no`,
`matter`, `din`, `list`, `context:<word>`, `trading_matter`, `suffix` = weak company default,
`none`), `is_issuer_guess`, `source` ∈ `header, header_numbered, header_pan, header_list,
header_matter, definition, title, matter`, `aliases` (JSON: defined short names, "Noticee No. 2",
former/current names), `penalty_amount` (₹ imposed on this party; null = none linked),
`penalty_rows`, `penalty_each`, `penalty_joint` (**a joint amount sits on every party liable —
never sum `penalty_amount` for money; use `orders.penalty_total`**), `sanction_types` (JSON:
`PENALTY, DEBARMENT, DISGORGEMENT, WARNING, SETTLEMENT, SUSPENSION, CANCELLATION`).
Week 4 adds `ticker`, `isin`, `match_confidence`.

`derived/order_parties.parquet` — one row per order (20,861): `matter_raw` ("In the matter of …",
title first), `matter_source`, `issuer_guess` (the company in the matter, else a party with an
ISSUER role), `issuer_source`, `parties_source`, `n_parties`, `n_parties_by_role` (JSON),
`parties_raw` (the header text the list came from, ≤1,500 chars), `penalty_link_status` ∈
`single_party, all_rows, partial, none`, `penalty_rows_n`, `penalty_rows_linked`,
`penalty_unassigned`, `penalty_total_resolved` (Day 4's total with "₹X each" counted once per
party named, when every row is linked), `regulations` (JSON: provisions *violated*, e.g.
`["PFUTP 3(a)", "SEBI Act 12A(a)"]`), `regimes` (JSON: instruments violated), `regimes_cited`
(JSON: every substantive instrument cited), `penalty_provisions` (JSON: `"SEBI Act 15HA"` …),
`n_citations`, `n_citations_unresolved`, `rules_version`, `error`.

`derived/regulation_mentions.parquet` — every provision cited (861,082): `text_stem`, `regime`
(PFUTP, PIT, SAST, ICDR, DIP, LODR, LISTING_AGREEMENT, INTERMEDIARIES, STOCK_BROKERS, …,
SEBI_ACT, SCRA, SCRR, DEPOSITORIES_ACT, COMPANIES_ACT; procedural ADJUDICATION_RULES,
ENQUIRY_REGS, SETTLEMENT), `regime_year`, `provision` ("4(2)(a)"), `keyword`, `role` ∈
`violation, penalty, power, cited`, `how` (title / act / abbrev / alias / generic / default),
`rel_pos`, `citation` (as written), `context` (120 chars before it).

`derived/order_categories.parquet` — one row per order (20,861); the fraud category (Week 3 Day 1, `fraud-v1`,
rules in `hand/coding_protocol.md` §3)
: `fraud_category` ∈ `INSIDER_TRADING, FUND_DIVERSION, RPT_TUNNELLING, ACCOUNTING_MISSTATEMENT, MARKET_MANIPULATION,
DISCLOSURE_FAILURE, INTERMEDIARY_MISCONDUCT, OTHER` (null only without text); `fraud_category_confidence` ∈ `high`
(one category charged, or a clear narrative) / `medium` (several charged, precedence decided; or a narrative that
only just wins) / `low` (fallbacks, PFUTP decided by its clauses, unresolved); `fraud_category_rule` (e.g.
`pit_trading`, `pfutp:narrative`, `lodr_23`, `fallback:investor_grievances`, `fallback:narrative`, `unresolved`);
`fraud_category_raw` (the provisions or phrases that decided it); `fraud_categories_all` (JSON, every category
charged, precedence order); `narrative_scores` (JSON, capped phrase counts); `rules_version`; `error`.

`derived/orders.parquet` — **the Week 2 checkpoint**, one row per order (20,861 × 66): the
runbook `orders` table. Listing (title, category, source URL, SAT appeals), PDF URL, text
(path, text layer, pages, OCR pages), the window from `windows_checked` (start, end,
precision, raw sentence, confidence, method, kind, sanity status), prior and first action
and sanctions from `order_actions`, matter / issuer / parties / regulations from
`order_parties`, and `fraud_category`, `fraud_category_confidence`, `fraud_category_rule`,
`fraud_category_raw` from `order_categories`. Built by joins only (on `text_stem`,
one-to-one, row count asserted), and `detection_channel`, `detection_detail`, `detection_confidence`,
`detection_method`, `detection_raw` from `order_channels` (20,861 × 71).

`derived/order_channels.parquet` — one row per order (20,861); the detection channel (Week 3 Day 2, `channel-v1`,
model prompt `dc-v2`; rules in `hand/coding_protocol.md` §5)
: `detection_channel` ∈ `SURVEILLANCE, COMPLAINT, AUDIT, MEDIA, REFERRAL, SELF_DISCLOSURE, INSPECTION, UNCLEAR` (null
only without text); `detection_detail` (SURVEILLANCE: `alert`, `trading_pattern_observed`, `filings_observed` — the
last two flagged; COMPLAINT: `complaint`, `scores`, `whistleblower`; AUDIT: `qualification`, `auditor_resignation`,
`forensic_audit`; MEDIA: `news`, `research_report`; REFERRAL: `mca_roc`, `sfio`, `rbi`, `tax`, `police_ed_cbi`,
`court`, `regulator`, `government`; SELF_DISCLOSURE: `confession`, `company_disclosure`, `self_report`; INSPECTION:
`sebi_inspection`, `exchange_inspection`, `thematic`; UNCLEAR: `none`, `exchange_reference`, `suo_motu`; model
channels: `model`); `detection_confidence` (`high` linked source; `medium` flagged detail, model, or rule UNCLEAR;
`low` nothing found); `detection_method` ∈ `rule, model, none`; `detection_raw` (the sentence); `channels_found`
(JSON, every rule hit in order); `n_background`; `model_status` ∈ `MODEL_CHANNEL, MODEL_UNCLEAR, MODEL_UNVERIFIED,
MODEL_ERROR`; `model_raw`; `rules_version`; `prompt_version`; `error`.

`src/regulations.py` — the citation parser: instrument catalogue (full titles, abbreviations,
the order's own "hereinafter" aliases), provision lists ("3(a), (b), 4(1) and 4(2)(e)"), and the
role of each citation (see `hand/recon_notes.md` §9).

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
- Parties: 1,398 orders have none (1,283 WTM) — mostly WTM orders whose noticee
  table is an image or sits in an annexure ("In respect of: – ____ The aforesaid
  entities …"). Names in all capitals joined by "AND" without honorifics stay one
  party ("BHAVESH THAKKAR AND BIMAL THAKKAR"). A proprietor's PAN is printed for each
  of his firms, so PANs repeat across parties of one order.
- Roles are a first guess: 33.6% UNKNOWN; `role_basis = suffix` (any company with no
  other evidence → ISSUER) is weak. Penalty links fail in 162 penalty orders and are
  partial in 58 (party named in a form the list does not use).

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

**Week 2 (Day 5).**
- A regex with nested repetition anchored at `$` (`(?:[\s,]+|\s+and)+$`) backtracked
  for minutes on one 15,000-character header entry and stalled the whole pool (the
  rest of the corpus took 40 s). Strip tails in a loop; cap entry length; time every
  order (`signal.alarm`) when a full run is unexpectedly slow.
- `name_pattern` drops company suffix words; never drop single letters ("Kailash S
  Chaudhary" stopped matching when "S" — meant for "M/S" — was dropped).
- "Recovery proceedings may be initiated under Section 28A" is boilerplate in every AO
  order; 28A is a power, not a violated provision (it had entered 2,717 lists).

**Audit fixes and Week 3 Day 1.**
- Sentence splitting cuts sentences over 2,000 characters into pieces
  (`split_sentences(..., groups=True)` says which pieces belong together). An operative
  "hereby impose" clause reads on into its own pieces, never into the next imposing
  sentence. A *non-operative* "impose" ("SEBI may impose …", statute quotes) never reads
  on: doing so once pulled market capitalisation and client balances into penalties
  (8,649 orders, ₹113,284 crore).
- `settlement_amount` is the largest *sentence* value, not a sum over sentences:
  settlement orders restate the amount 2–4 times.
- Classify from what was charged, never from definitions: PIT 2(1)(g), Companies Act
  2(76) and PFUTP 2(1)(c) are recited beside the charge (`classify_fraud.category_of`
  returns None for Reg./Sec. 2).
- Generic accounting words ("books of accounts", "financial statements", "forensic
  audit") appear in broker inspections and adviser cases. They only reinforce a
  misstatement phrase; they never make ACCOUNTING by themselves.
- Boilerplate that lists sections is not a charge. Examples: 28A recovery, the §15J Explanation ("power of an
  adjudicating officer to adjudge the quantum of penalty under sections 15A to 15E, … 15G, 15H and 15HA"),
  and regulation *titles* ("SEBI (Prohibition of Insider Trading) Regulations", "Model Code of Conduct for
  Prevention of Insider Trading"). Each of these had inflated a count by thousands of orders.
- In the fraud category, *whose* money was diverted decides (`classify_fraud.route_diversion`): issue proceeds
  → FUND; company funds to insiders → RPT (tunnelling); clients' or investors' assets at a broker, DP or RTA →
  INTERMEDIARY; a CIS → OTHER. Precedence overrides the top narrative score only for a clear narrative
  (≥ 5 and ≥ half the top); stray mentions flipped dozens of orders.
- Detection channel: a trigger must be *tied to the start* of SEBI's work (a link word, SEBI receiving it, or
  the investigation cue in the same sentence). Everything else in an order mentions complaints, inspections,
  media and courts too: noticee contentions, SCN enclosures, cited judgments, findings ("During the
  investigation it was observed"). `trigger_window` stops the search a few sentences after SEBI's work starts.
- `"(‘SEBI’), observed"`: quotes, brackets and commas sit between "SEBI" and the verb (`SEBI_T`); a pattern
  written `SEBI\s+observed` silently misses thousands of orders.
- An unconstrained model pass invents channels from "SEBI conducted an investigation" (dev set: 7 of 30). The
  model sees only cue-bearing sentences in the trigger window, and its quote must carry the channel's cue.
- `issuer_from_matter` takes the *last* "shares of / scrip of" match, so a header matter that
  runs on into the order can walk past the company. Descriptive prefixes ("Inspection of
  Books of") are stripped only at the start.

## 11. Next steps (runbook Week 2 onward)

1. Week 2 is built (Day 5: `derived/orders.parquet`, `order_entities`). Settlement orders
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
6. Fraud category and detection channel are built (Week 3 Days 1–2). Next are **Days 3–4, the blind
   sample**: `python -m src.validation_sample` (the user codes 150 orders from the PDFs, with the protocol open).
   Day 5 measures agreement and κ for both fields, and window accuracy by method and era. H4 (surveillance
   share < 50%) needs matter-level first detections: order-level shares swing with the ISO wave (43% with,
   27% without) and on listed-company frauds (57%).
7. Week 4 matching: `order_entities.pan` / `cin` and `orders.issuer_guess` are the
   hooks; entity roles are a first guess to correct during validation.