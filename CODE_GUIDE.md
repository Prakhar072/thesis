# Code guide — every file, what it does, and what to check when you review it

*Written 2026-10-21 for your review of the code base. It covers all of `src/` (25 modules, ~16,000 lines,
466 functions), the tests, the hand files and the other documents.*

Related documents:
- [TASKS.md](TASKS.md): what is left for you to do, with instructions.
- [PROJECT_GUIDE.md](PROJECT_GUIDE.md): how to use the data.
- [CLAUDE.md](CLAUDE.md): the technical briefing (data dictionary in §8).
- [results_log.md](results_log.md): every figure, with its source.

---

## How to use this guide

Every function in `src/` has a docstring giving its inputs and output with their types (`Args:` / `Returns:`),
and every module starts with a docstring explaining the stage. This guide is the map that sits above those. For each
file it gives:

- **Run / reads / writes**: the command, and the files it reads and writes.
- **How it works**: the steps, in order.
- **Key functions**: the ones that carry the logic (the rest are helpers, documented in the code).
- **Check when reviewing**: the judgement calls, i.e. the places where a rule decides something about the thesis
  data. These are where a reviewer's attention pays off most.
- **Tests** and **limits**.

**A suggested way to review a stage** (about 30–60 minutes each):

1. Read its section here, then the module docstring at the top of the file.
2. Read the key functions, top to bottom.
3. Run its tests: `python -m pytest tests/test_<module>.py -q`. Then open the test file: test names say what each
   case guards against.
4. Watch it work on one or two orders without touching `derived/`. For example:
   `python -m src.extract_windows --order-id 46128 --scratch /tmp/review`, then open `/tmp/review/windows.parquet`.
5. Write questions or doubts in a notes file. An agent can then answer or fix them, with a test for each fix.

**Keep the blind sample blind.** Step 4 shows the pipeline's answers for the orders you pick. Once you have drawn
the validation sample ([TASKS.md](TASKS.md) A2), avoid those 150 orders. Better still, finish coding them first.

## The shape of the pipeline

```
SEBI website ──► 1 scrape_listings ──► 2 fetch_pdfs ──► 3 extract_text ──► 4 extract_windows ──► 5 window_model
   (cache)          listings             PDFs / pages        text files          window candidates     model fallback
                                                                                                          │
   ┌──────────────────────────────────────────────────────────────────────────────────────────────────────┘
   ▼
6 check_windows ──► 7 extract_actions ──► 8 extract_parties ──► 9 classify_fraud ──► 10 detect_channel ──► 11 build_orders
  sanity rules        prior actions,        parties, roles,        fraud category       detection channel      orders.parquet
  + your review       sanctions             regulations                                                      (one row per order)

Week 3:  validation_sample (blind sheet) ──► you code 150 orders ──► score_validation (agreement, κ, Gate 1)

Week 4:  universe ──► match_entities ──► build_sample ──► fetch_prices ──► fetch_shareholding ──► build_panel
         listed firms   issuers → firms    frozen sample    daily prices      promoter / pledges      firm-year panel
```

**Conventions shared by every stage.**
- **The join key is `text_stem`**: `"<order_id>"`, or `"noid_<12 hex>"` for the 76 orders without an id
  (`extract_text.text_stem`). Never join on `order_id`: pandas matches missing ids to each other.
- **All network access goes through `src/cache.py`.** Every response is kept forever in `raw/cache/`, so a re-run
  costs no network. `--offline` proves a run used only the cache.
- **`raw/` is never modified.** `extract_text.assert_safe_output` refuses writes under `raw/pdfs` and `raw/cache`.
  Outputs are written to a temporary file and then renamed, so an interrupted run never leaves a half-written table.
- **`--scratch DIR`** sends every output of a stage to DIR instead of `derived/`, for dry runs.
- **Every derived row carries its evidence.** `*_raw` columns hold the sentence a value was read from, and
  `rules_version` / `prompt_version` name the rule set that produced it.
- **Logs** go to `logs/<stage>.log` and end with a summary block. Most figures in `results_log.md` come from
  there.

---

## Infrastructure

### `src/cache.py` — the only door to the network (356 lines)

**Used by** every fetching stage. It is not a command of its own.

**How it works.**
1. A request is identified by method, URL, query parameters, form body and an optional extra key.
2. The SHA-256 of that identity names the file in `raw/cache/`.
3. A cached response is returned without a network call.
4. On a miss, it waits 1.5 s (politeness), then sends the request with a browser User-Agent (plus `SEBI_CONTACT` if
   set).
5. It retries rate-limit and server errors (408, 425, 429, 5xx, 520–530), backing off 3 s, 6 s, then 12 s.
6. Any other 4xx, and every redirect, fails at once.
7. Before caching, a validator rejects SEBI's firewall page ("Unauthorized Request Blocked"), so a firewall page
   can never pose as data.

**Key functions:**
- `fetch`: the core.
- `get` / `post`: wrappers around it.
- `cache_key` / `cache_path`: where a response lives.
- `set_offline`, `NETWORK_CALLS`: offline mode, and the count of real requests.

**Check when reviewing.**
- Headers are deliberately not part of the key, so changing the User-Agent does not invalidate the cache.
- `force=True` overwrites an entry. It is never used on data, because `raw/` is append-only. To ask again, use
  `cache_key_extra="retry-1"`, which stores a second copy.

**Tests:** `tests/test_cache.py`, 20.

### `src/llm.py` — the local model, cached (195 lines)

**Used by** stages 5 and 10. It talks to Ollama on localhost (`OLLAMA_HOST`), the only HTTP that bypasses
`cache.py`.

**How it works.**
- Each answer is stored in `raw/llm/<key[:2]>/<key>.json`.
- The key is a hash of the model name and its *weights digest*, the generation options, thinking on/off, both
  prompts and the JSON schema. A re-pulled model or an edited prompt therefore gets fresh calls, and nothing stale
  is reused.
- `OFFLINE = True` turns a miss into `CacheMiss`.

**Key functions:**
- `ModelSpec`: the model's identity.
- `model_digest`: the weights digest, asked from the server.
- `chat_json`: one cached chat turn.

**Check when reviewing.** The model never produces data that is taken on trust. Both stages that use it verify its
quotes against the text (see stages 5 and 10).

**Tests:** run through `test_window_model.py` and `test_detect_channel.py`, with a fake client.

---

## Building the corpus (Week 1)

### `src/scrape_listings.py` — stage 1: the list of orders (907 lines)

| | |
|---|---|
| **Run** | `python -m src.scrape_listings [--offline] [--category AO] [--start-page/--end-page]` |
| **Reads** | SEBI's listing endpoint, through the cache (836 pages, already cached) |
| **Writes** | `derived/listings.parquet` (20,861 rows); `reports/listing_pages.csv`, `listing_conflicts.csv`, `listing_needs_review.csv` |

**How it works.**
1. For each of the four categories (WTM smid 2, Settlement 3, AO 6, ED/CGM 133), POST page after page to
   `getnewslistinfo.jsp`.
2. The real page selector is `doDirect = page − 1`; `nextValue` is ignored. The page count comes from the hidden
   `totalpage` field.
3. Parse the rows of `table#sample_1`, never the `ordersum` table of 11B spreadsheets.
4. Clean the HTML that SEBI puts in titles: corrigendum links come out, and SAT appeal numbers go to
   `sat_appeals`.
5. Take the order id from `_<id>.html`, and de-duplicate, reporting conflicts.

**Key functions:**
- `build_form`: the page selector.
- `parse_rows`: one page into ListingRows.
- `clean_title_attr`: titles, corrigenda and SAT appeals.
- `scrape_category`: the page walk.
- `deduplicate`.

**Check when reviewing.**
- `order_type` comes from the category, never from the title.
- A row without an id is kept and flagged, not guessed.
- The count reconciliation is logged per category: (pages − 1) × 25 ≤ rows ≤ pages × 25.

**Tests:** `test_scrape_listings.py`, 82, on real SEBI pages kept in `tests/fixtures/`.

**Limits.** New orders need `--force` (836 requests): every new publication shifts the pages.

### `src/fetch_pdfs.py` — stage 2: each order's document (699 lines)

| | |
|---|---|
| **Run** | `SEBI_CONTACT=… python -m src.fetch_pdfs [--offline] [--resolve-only]` |
| **Reads** | listings; detail pages and PDFs through the cache |
| **Writes** | `derived/order_documents.parquet`, `derived/fetch_failures.csv`, `raw/pdfs/<stem>.pdf` (hard links into `raw/cache`) |

**How it works.**
1. Read each order's detail page.
2. Find its document, in this order of precedence:
   - an iframe or embed (SEBI's viewer `?file=<url>`);
   - else inline order text of at least 1,500 characters (1,582 old orders have no PDF);
   - else a PDF link.
3. Download the PDF and check that it starts with `%PDF-` (HTML error pages are refused).
4. Archive it as a hard link to the cache entry, so the bytes are stored once.
5. Write a checkpoint every 500 orders.

**Key functions:**
- `extract_document_ref`: the precedence.
- `process_row`: resolve, then download and archive.
- `archive_pdf`.

**Check when reviewing.**
- The precedence order. Taking links first archived annexures as orders (32 cases, fixed).
- `upgrade_scheme`: SEBI's plain-HTTP links are rewritten to https.

**Tests:** `test_fetch_pdfs.py`, 57.

**Limits.** Five dead links, and a few pages with no document (CLAUDE.md §9).

### `src/extract_text.py` — stage 3: text, with OCR where needed (1,313 lines)

| | |
|---|---|
| **Run** | `python -m src.extract_text [--no-ocr] [--sample N] [--workers N] [--scratch DIR]` |
| **Reads** | order_documents, `raw/pdfs`, cached detail pages (inline orders) |
| **Writes** | `derived/text/<stem>.txt` (pages separated by `\f`), `derived/text_documents.parquet`, `derived/text_pages.parquet`, `raw/ocr/` (the OCR cache) |

**How it works.** Each PDF page is judged on its own:

| Class | When | What happens |
|---|---|---|
| NATIVE | ≥ 100 characters of clean text | the text layer is used |
| BROKEN_LAYER | ≥ 20 characters but < 75% real text (Ghostscript control codes) | OCR |
| SCANNED | < 100 characters and images covering ≥ half the page | OCR |
| VECTOR_TEXT | < 100 characters, no page image, ≥ 300 vector paths (text drawn as outlines) | OCR |
| SPARSE | anything else (blank, signature pages) | the text layer is kept |

- OCR runs the tesseract command line (`--psm 3`, 300 dpi, TSV output).
- A page that scores below 70 confidence is retried at every rotation and at 200 dpi.
- Results are cached by PDF content hash, page and engine settings.
- Text is normalised without changing meaning: special spaces become plain spaces, invisible characters are
  removed, ligatures are expanded, and nothing is dehyphenated.
- An order with a page still waiting for OCR gets no text file.

**Key functions:**
- `text_quality`, `classify_page`: the page classes.
- `ocr_page`: the first pass, then the rotation retry.
- `extract_pdf`, `extract_inline`.
- `text_stem`, `assert_safe_output`, `write_atomic`, `setup_logging`: shared by later stages.

**Check when reviewing.**
- The thresholds (`MIN_CHARS`, `MIN_QUALITY`, `MIN_IMAGE_COVER`, `MIN_VECTOR_DRAWINGS`), each justified by a comment.
- PDFs are opened from bytes, never handed to a writer.

**Tests:** `test_extract_text.py`, 83, including a real tesseract run on an upside-down page.

**Limits.**
- 27 OCR pages stay under 70 confidence.
- Running headers and footers are kept in the text; later stages strip them.

---

## The violation window (Week 2, Days 1–3)

### `src/extract_windows.py` — stage 4: window candidates and regex v1 (1,454 lines)

| | |
|---|---|
| **Run** | `python -m src.extract_windows [--order-id N] [--scratch DIR]` |
| **Reads** | text_documents, `derived/text/` |
| **Writes** | `derived/window_candidates.parquet` (top 3 sentences per order), `derived/windows.parquet` (one row per order) |

**How it works.**
1. Strip running headers and footers (`strip_repeated_lines`), keeping offsets.
2. Split into sentences: abbreviations are known; over-long "sentences" from flattened tables are re-split.
3. Read every date and date range with an explicit **day-first** grammar (05.11.2019 is 5 November). A missing day
   is never filled from today's date; precision is recorded.
4. Remove ranges that are an **inspection visit** (`inspection_visit`): the dates the probe itself took.
5. Score each sentence by its period label:
   - defined ("hereinafter … investigation period") > strong ("investigation period") > relevant (patch, UPSI
     period) > context (an investigation word) > generic ("period").
   - Bonuses for a range and an early position; a penalty for negative cues (open offer, letter dated).
6. Keep the top 3 sentences, choose a window from the best confident candidate, and set `window_status`:
   - PARSED: regex found it.
   - UNCONFIRMED_RANGE or UNPARSED: these go to stage 5.
   - NO_CANDIDATES, NO_TEXT.

**Key functions:**
- `parse_date`, `parse_range`, `parse_fy`.
- `inspection_visit`.
- `score_sentence`.
- `pick_window`: several ranges in one sentence.
- `confidence_for`.
- `extract_candidates`.

**Check when reviewing.**
- **The window definition (CLAUDE.md §1).** "Conducted investigation during the period X to Y" is the *activity*
  period and is kept. Only inspection visits are removed. A test guards this.
- Confidence: `high` needs a strong label, a clean range and a clear choice; a bare investigation word is not
  enough.
- `split_sentences(..., groups=True)` is reused by stage 7.

**Tests:** `test_extract_windows.py`, 96, mostly real sentences from orders.

### `src/window_model.py` — stage 5: model fallback for unsettled windows (968 lines)

| | |
|---|---|
| **Run** | `python -m src.window_model [--offline]` (Ollama must be serving unless `--offline`) |
| **Reads** | windows, window_candidates |
| **Writes** | `derived/window_model.parquet` (one row per order sent), `derived/violation_windows.parquet` (regex window, else the verified model window) |

**How it works.**
1. Each UNCONFIRMED_RANGE / UNPARSED order's candidate sentences go to `qwen3.5:4b`. The prompt is in `SYSTEM` and
   the JSON schema in `SCHEMA`.
2. The model answers the *kind* (investigation / violation / none), the sentence number, and **quotes** of the
   start and end dates.
3. The code checks the quotes occur in that sentence (letters and digits only, leading zeros ignored).
4. It reads them with the same day-first grammar, extended to years, financial years and day spans.
5. It rejects a start after the end, an open-ended quote ("till March 2014"), and known non-windows
   (`false_window_reason`): correspondence dates, inspection visits, statement periods, trade-log rows.
6. Model windows are `medium` confidence. `window_kind` is `stated_period` or `conduct_dates` — two ways of
   stating the one quantity.

**Key functions:**
- `read_answer`: every check.
- `resolve_date`.
- `false_window_reason`.
- `combine`.

**Check when reviewing.**
- The model never supplies a date of its own: it only points at text.
- `MODEL_NONE` is not evidence that the order has no window.

**Tests:** `test_window_model.py`, 64.

**Limits.** Agent-judged precision is 84–90% on 50 windows. The blind sample measures it properly.

### `src/check_windows.py` — stage 6: sanity rules, your review, the extraction rate (511 lines)

| | |
|---|---|
| **Run** | `python -m src.check_windows` |
| **Reads** | violation_windows, listings, `hand/window_review.csv` |
| **Writes** | `derived/windows_checked.parquet` (**the window table**), `derived/window_flagged.csv`, `derived/extraction_rate.csv`; appends new flags to `hand/window_review.csv` |

**How it works.**
1. Apply the rules:
   - R1: the window ends after the order.
   - R2: it starts after it ends.
   - R3: the lag from window end to order is ≤ 0, or ≥ 20 years.
   - R4: it starts before 1995.
2. Flagged windows leave the dataset (their dates are kept in `flagged_*`), unless your decision in
   `hand/window_review.csv` keeps or fixes them.
3. The extraction rate is computed with a drop-out test (Cramér's V by order type and by year).

**Key functions:**
- `flags_for`, `earliest_end`: a month-precision end gets the benefit of its month.
- `check`.
- `decisions`, `update_review`: the durable sheet.
- `extraction_rate`.

**Check when reviewing.**
- Which rules are "impossible" and which only "implausible".
- A flagged window is cleared completely, confidence included (AUDIT M-9).

**Tests:** `test_check_windows.py`, 29.

---

## Actions, sanctions, parties (Week 2, Days 4–5)

### `src/amounts.py` — the one rupee normaliser (293 lines)

**Used by** stage 7.

**What it handles.**
- Indian (26,00,00,000) and western grouping.
- Units: crore, lakh, million, thousand.
- Rupee marks: Rs., ₹, INR, "Rupees", and the backtick an old rupee font renders as.
- "/-", paise, and typos such as "1,00.000" and "2,00,00".
- Amounts written in Indian number words ("Seven Thousand Two Hundred Sixty Nine Crore"), so every figure can be
  checked against its words.
- Durations: "4 (four) years" → 48 months.

**Key functions:**
- `find_amounts`: figures with their words.
- `parse_figure`.
- `words_to_number`.
- `malformed`.
- `duration_months`.

**Check when reviewing.**
- A malformed figure gives way to its words.
- "INR200004031" is a registration number, not ₹200.

**Tests:** `test_amounts.py`, 47.

### `src/extract_actions.py` — stage 7: prior actions and sanctions (1,082 lines)

| | |
|---|---|
| **Run** | `python -m src.extract_actions` |
| **Reads** | text_documents, `derived/text/`, windows_checked |
| **Writes** | `derived/order_actions.parquet` (one row per order), `derived/prior_action_mentions.parquet`, `derived/sanction_mentions.parquet` |

**How it works: prior actions.**
- Every recited SCN, interim / ex-parte / ad-interim order, confirmatory order and AO appointment, with the date
  that belongs to *it*. "SCN dated X" counts; "reply to the SCN vide letter dated X" does not.
- A date is plausible if it falls between the SEBI Act (30 Jan 1992) and the order, and not before the window
  starts.
- `prior_action_date` is the earliest plausible SCN, interim or confirmatory date.
- `first_action_date = min(prior_action_date, order_date)`.
- AO appointments are kept apart, for robustness only.

**How it works: sanctions.**
- Only the operative part counts (from 40% of the text on), and only sentences that *impose*.
- Excluded: statute quoted, show-cause language, past sanctions recited.
- Penalty tables split across sentences are read with their clause (`_with_continuation`, `_with_following`).
- A restated penalty is counted once (`penalty_rows`).
- A settlement amount is the largest per-sentence amount (orders restate it).
- Also read: disgorgement, debarment (months, indefinite), registration action, warning, "no penalty".

**Key functions:**
- `find_prior_mentions`, `plausibility`.
- `find_sanctions`.
- `penalty_rows`.
- `settlement_amount`.
- `summarise_order`.

**Check when reviewing.**
- The `_GAP` word list (what may sit between "SCN" and "dated").
- `_NOT_OPERATIVE`.
- The audit fixes C-1, C-2, H-1 and H-2, each with a test.

**Tests:** `test_extract_actions.py`, 46.

**Limits.** Several applicants each paying in separate sentences are under-counted.

### `src/regulations.py` — the citation parser (533 lines)

**Used by** stages 8 and 9.

**What it does.**
- Recognises ~40 instruments: by full title ("SEBI (Prohibition of Insider Trading) Regulations, 2015"), by
  abbreviation ("PIT Regulations"), or by the order's own "hereinafter" alias.
- Expands provision lists: "3(a), (b), 4(1) and 4(2)(a)" → four provisions.
- Gives each citation a role:
  - *penalty*: SEBI Act 15A–15HB and similar.
  - *power*: 11, 11B, 15I, 15J, 28A and similar.
  - *violation*: from the text before it, or chained to a violation.
  - *cited*: anything else.

**Key functions:**
- `match_instrument`.
- `find_aliases`.
- `expand`.
- `find_citations`.
- `as_list`: produces `["PFUTP 3(a)", "SEBI Act 12A"]`.

**Check when reviewing.** Boilerplate is not a charge. Examples are the §15J explanation, 28A recovery, and
regulation titles (CLAUDE.md §10).

**Tests:** `test_regulations.py`, 29.

### `src/extract_parties.py` — stage 8: parties, roles, penalties per party, violated regulations (1,674 lines)

| | |
|---|---|
| **Run** | `python -m src.extract_parties` |
| **Reads** | text_documents, `derived/text/`, listings, sanction_mentions, order_actions |
| **Writes** | `derived/order_entities.parquet` (one row per order × party), `derived/order_parties.parquet` (one row per order), `derived/regulation_mentions.parquet` |

**How it works: parties.**
1. Find the noticee segment in the header ("In respect of", "Against", "filed by" … up to "In the matter of").
2. Split it into entries: by serial numbers, by PANs, or by commas before honorifics.
3. Cut each entry to its name.
4. Read PAN, DIN, CIN and the SEBI registration number.
5. Fallbacks, in order: "X (hereinafter 'Noticee No. 2')" definitions; then the listing title; then the matter.

**How it works: roles and issuer.**
- Roles are a first guess, with their evidence in `role_basis`. They are decided in this order: registration
  number → intermediary, "Chartered Accountants" → auditor, the matter's company → issuer, promoter, director or
  DIN, intermediary wording, trading wording, any other company → issuer (weak, `suffix`).
- The issuer guess is the company in "in the matter of …".

**How it works: penalties and regulations.**
- Penalties go to the party each one names: in a table the party comes before the amount; in prose, after it.
- "Each" amounts go to every party named. Joint amounts go to each party and are flagged.
- Regulations come through `regulations.py`.

**Key functions:**
- `noticee_segment`, `split_entries`, `clean_entry`.
- `issuer_from_matter`.
- `guess_role`.
- `link_penalties`.
- `process`.

**Check when reviewing.**
- **Never sum `penalty_amount` over parties for money**: a joint penalty sits on every party liable. Use
  `orders.penalty_total`.
- The `role_basis = suffix` default is weak.

**Tests:** `test_extract_parties.py`, 60.

**Limits.**
- About 1,390 orders with text have no parties (noticee tables that are images).
- 33.6% of roles are UNKNOWN.

---

## Coding the orders (Week 3)

### `src/classify_fraud.py` — stage 9: the fraud category (817 lines)

| | |
|---|---|
| **Run** | `python -m src.classify_fraud` |
| **Reads** | order_parties, order_actions, listings, text_documents, `derived/text/` |
| **Writes** | `derived/order_categories.parquet` |

**How it works** (`hand/coding_protocol.md` §3, written first):
1. Each violated provision maps to a category (`category_of`):
   - PIT trading clauses → insider trading; PIT disclosure duties → disclosure.
   - LODR 23 → RPT.
   - SAST, LODR and the Listing Agreement → disclosure.
   - Intermediary regulations → intermediary misconduct.
   - ICDR/DIP → diversion if the narrative says so, else disclosure.
   - PFUTP and SEBI Act 12A(a)–(c) → *read the narrative*.
   - Definitions (Reg./Sec. 2) are never charges.
2. The narrative is scored by capped phrase counts per category. Whose money was diverted decides the diversion
   category (`route_diversion`):
   - issue money → FUND_DIVERSION;
   - the company's own funds to insiders → RPT_TUNNELLING (your decision, 2026-09-30);
   - clients' assets at an intermediary → INTERMEDIARY_MISCONDUCT.
3. Precedence decides between several categories: insider › fund › RPT › accounting › manipulation › disclosure ›
   intermediary › other.
4. With no violated provision, the fallbacks are tried in turn (all `low`): penalty sections, then title
   keywords, then a strong narrative, then regimes cited.
5. If nothing fits, the order is OTHER, `unresolved`.

**Key functions:**
- `category_of`.
- `narrative_scores`.
- `route_diversion`.
- `read_narrative`.
- `classify`.

**Check when reviewing.**
- The phrase lists in `PHRASES`.
- The `MIN_SCORE` thresholds.
- The rule that generic accounting words only reinforce a misstatement phrase.

**Tests:** `test_classify_fraud.py`, 52.

**Limits.** 959 orders are unresolved. Development agreement was 75 of 81, measured on the orders the rules were
built on.

### `src/detect_channel.py` — stage 10: the detection channel (924 lines)

| | |
|---|---|
| **Run** | `python -m src.detect_channel [--no-model] [--offline]` |
| **Reads** | listings, text_documents, `derived/text/` |
| **Writes** | `derived/order_channels.parquet` |

**How it works** (`hand/coding_protocol.md` §5):
1. Take the background: the first 45% of the text, at least 6,000 characters.
2. Run the rules per channel. A source counts only if it is **tied to the start** of SEBI's work: a link
   ("pursuant to", "on receipt of"), SEBI receiving it, or an investigation cue nearby.
3. Skip negated mentions, noticees' submissions, enclosures with a notice, findings ("during the investigation it
   was observed"), and procedural inspections.
4. The search stops a few sentences after SEBI's work starts (`trigger_window`).
5. The earliest trigger wins. An exchange reference with no basis, or "suo motu", is UNCLEAR (your decisions,
   2026-09-30).
6. Undecided orders whose sentences carry a channel cue go to the model. Its quote must contain that channel's cue.

**Key functions:**
- `RULES`.
- `find_triggers`.
- `_accept`.
- `trigger_window`.
- `choose`.
- `read_answer`: the model checks.

**Check when reviewing.**
- The flagged SURVEILLANCE details (`trading_pattern_observed`, `filings_observed`): the H4 robustness depends on
  them.
- The UNCLEAR share is itself a finding.

**Tests:** `test_detect_channel.py`, 18. Plus the 58-order development set in `hand/`.

### `src/build_orders.py` — stage 11: the order table (237 lines)

| | |
|---|---|
| **Run** | `python -m src.build_orders` |
| **Reads** | listings, order_documents, text_documents, windows_checked, order_actions, order_parties, order_categories, order_channels |
| **Writes** | `derived/orders.parquet` (20,861 × 71) |

**How it works.** Joins only: one to one, on `text_stem`, asserting the row count after every join. Party-level
facts stay in `order_entities`.

**Tests:** `test_build_orders.py`, 4.

### `src/validation_sample.py` — the blind sample (582 lines)

| | |
|---|---|
| **Run** | `python -m src.validation_sample` (once), then `python -m src.validation_sample --check` while you code |
| **Reads** | windows_checked, listings, order_documents, `raw/pdfs`, `derived/text/`, the "seen" lists in `hand/` |
| **Writes** | `hand/validation_sample.csv` (the sheet), `hand/validation_pdfs/` (copies), `derived/validation_key.parquet` (strata and weights; **not for you to open while coding**) |

**How it works.**
1. Strata, from the pipeline's window outcome: 60 regex, 25 model stated-period, 25 model conduct-dates, 40 with
   no window.
2. Illiquid Stock Options (ISO) orders are capped per stratum.
3. Within each stratum the draw is systematic over order type × date, which spreads it over categories and years.
   The draw is seeded.
4. 539 orders seen while building are excluded.
5. Weights are set per stratum × ISO cell, so accuracy can be weighted back to the corpus.
6. `--check` validates your sheet: dates, allowed values, OTHER group letters, blanks without notes. It never shows
   the pipeline's answers.

**Key functions:**
- `draw`.
- `key`: the weights.
- `copy_files`.
- `check_row`.

**Tests:** `test_validation_sample.py`, 30.

### `src/score_validation.py` — Day 5: agreement, κ, Gate 1 (897 lines)

| | |
|---|---|
| **Run** | `python -m src.score_validation --official` (once, on the finished sheet) |
| **Reads** | your sheet, the key, orders, extraction_rate |
| **Writes** | `derived/validation_scored.parquet`, `validation_errors.csv`, `validation_summary.csv`, `validation_table.md`; `hand/validation_official.json` |

**What it measures.**
- **Window:** whether hand and pipeline agree there is one. Then exact match and within 31 days, measured three
  ways: over the pipeline's windows, over your windows, and where both give one.
- **Category and channel:** agreement and weighted Cohen's κ. Intervals come from a bootstrap within cells.
- **Issuer:** agreement, after the Week 4 name normaliser.
- **Breakdowns:** by method, era and order type.
- **Gate 1:** read off the runbook table.

**Safeguards.**
- It refuses an unfinished sheet (`--partial` exists for plumbing tests only).
- It refuses a sheet that fails `--check`.
- Later scorings after any change are labelled NOT THE VALIDATION.

**Key functions:**
- `window_fields`.
- `score_rows`.
- `wkappa`.
- `boot_ci`.
- `errors`.
- `gate1`.

**Tests:** `test_score_validation.py`, 23.

---

## Market data (Week 4)

### `src/names.py` — the one company-name normaliser (55 lines)

**What it does.** The runbook's `norm()`: upper case, punctuation removed, & → AND, and LIMITED / LTD / PVT /
PRIVATE / M/S / COMPANY / CO / THE / INDIA dropped. It also expands BSE abbreviations (CORPN, INDS, INTL …) on
both sides. `names()` also returns bracketed former names.

**Used by** `universe`, `match_entities` and `score_validation`, so every comparison normalises identically.

**Tests:** in `test_universe.py`.

### `src/universe.py` — Day 1: every listed equity (380 lines)

| | |
|---|---|
| **Run** | `python -m src.universe` |
| **Reads** | BSE lists of scrips (active, delisted, suspended), NSE equity and SME lists, NSE name and symbol changes — 7 requests, cached 2026-10-14 |
| **Writes** | `derived/universe.parquet` (13,451 securities), `derived/firms.parquet` (10,180 firms), `derived/name_aliases.parquet` (23,900 names) |

**How it works.**
- `firm_id` is the ISIN's issuer prefix (`INE117A`). It is unchanged by a face-value split, and a partly paid
  `IN9…` belongs to the same issuer.
- NSE's rename history is placed on firms by symbol, by symbol chain, or by a unique name.

**Key functions:**
- `firm_id`.
- `parse_bse`, `parse_nse`.
- `name_changes`.
- `firms`.

**Check when reviewing.** BSE's industry field is always empty. Rename history exists for NSE only.

**Tests:** `test_universe.py`, 7.

### `src/match_entities.py` — Day 2: orders' companies → listed firms (565 lines)

| | |
|---|---|
| **Run** | `python -m src.match_entities` (again after your review) |
| **Reads** | orders, order_entities, universe, name_aliases, firms, `hand/match_review.csv` |
| **Writes** | `derived/order_issuers.parquet` (one per order), `derived/entity_matches.parquet` (one per company party), `derived/name_matches.parquet` (one per distinct name); appends new names to `hand/match_review.csv` |

**How it works.**
1. **Codes first.** An ISIN or BSE scrip code printed in the order (only 230 orders) is taken when it agrees with
   the issuer name, or when there is no name.
2. **Then names**, scored by token-sort against every alias: exact or ≥ 92 is auto, 80–92 goes to your review,
   < 80 is rejected.
3. Guards:
   - private companies (Pvt, LLP) are never matched;
   - a fuzzy match must also agree on the **distinctive words** (words in ≥ 30 listed names are generic) and on
     its first distinctive word;
   - very short names and two firms ≥ 92 go to review.

**Key functions:**
- `match_names`, `_best`.
- `core`, `generic_words`.
- `query_names`.
- `resolve`.
- `match_orders`.

**Check when reviewing.** The bands, and the guards that change which band a name lands in. Agent spot checks
found 43–44 of 45 fuzzy auto-matches right.

**Tests:** `test_match_entities.py`, 15.

**Limits.** Companies listed only on regional exchanges are in no list.

### `src/build_sample.py` — Day 3: the frozen analysis sample (302 lines)

| | |
|---|---|
| **Run** | `python -m src.build_sample` (once; it refuses to overwrite) |
| **Reads** | orders, order_issuers, entity_matches, firms |
| **Writes** | `derived/sample.parquet`, `derived/sample_fraud_years.parquet`, `derived/sample_events.parquet`, `hand/sample_frozen.csv` |

**How it works.**
- **Fraud firm-years:** a matched issuer, and any financial year (April–March) overlapping one of its windows.
  Overlap days are kept; `in_period` marks FY2010–2025.
- **Controls:** 600 firms drawn at random (seeded) from firms with an ISIN that are never an issuer or review
  candidate. Each is weighted by eligible ÷ drawn.
- **Events:** every order with a matched issuer, plus each recited interim date.

**Check when reviewing.** Every order category is kept, with flags (`categories`, `issuer_noticee`), so Stage 2
can narrow the label without re-drawing.

**Tests:** `test_build_sample.py`, 6.

### `src/fetch_prices.py` — Day 4: daily prices, corporate actions, the index (476 lines)

| | |
|---|---|
| **Run** | `python -m src.fetch_prices` (~7–8 h; resumable) |
| **Reads** | NSE and BSE bhavcopy archives, BSE corporate actions and BSE 500 archive — all through the cache |
| **Writes** | `derived/prices/<NSE\|BSE>_<year>.parquet`, `derived/price_days.parquet`, `derived/corporate_actions.parquet`, `derived/market_index.parquet` |

**How it works.**
1. Every weekday from 2004 is fetched, both formats: the old ones, and the common UDiFF format from 8 July 2024.
2. Holidays: NSE answers 404, and the ledger stops it being asked again. BSE answers an HTML page with status 200,
   which is cached and read as "no file".
3. A one-sided holiday is asked again once, under a new cache key.
4. Each year's parquet is written as the year completes.

**Key functions:**
- `bhav_url`.
- `parse_bhav`: four layouts, one schema.
- `fetch_day`.
- `run`.
- `parse_purpose`: split and bonus factors.

**Tests:** `test_fetch_prices.py`, 19.

### `src/prices.py` — Day 4: adjustment and the sanity check (226 lines)

**Run:** `python -m src.prices --check TATASTEEL` → `figures/price_check_TATASTEEL.png`.

**How it works.** Bhavcopy prices are **not** adjusted.
- `adjust` applies split and bonus factors on the first trading day on or after the ex-date:
  `r = f·close / previous close − 1`.
- Adjusted closes divide earlier prices by later factors.
- Consolidations (no ratio given) are flagged.
- Moves beyond +100% or −50% are flagged as `gap`.

**Tests:** `test_prices.py`, 7.

**Limits.** Returns are price returns: dividends, rights and demergers are not adjusted.

### `src/fetch_shareholding.py` — Day 5: promoter holding and pledging (343 lines)

| | |
|---|---|
| **Run** | `python -m src.fetch_shareholding` (~6–9 h; after prices) |
| **Reads** | the frozen sample, price files (trading spans), BSE's shareholding summary API |
| **Writes** | `derived/shareholding.parquet` |

**How it works.**
- One request per firm and March quarter (`--quarterly` asks for all four).
- The quarter id is computed (60 = Dec 2008, +1 per quarter).
- Columns are read **by header**, because the table omits columns that do not apply.
- Pledges are taken as zero when the company answers "No" to the pledge question.
- Only quarters within the firm's trading span are asked for. A delisted firm stops after two empty quarters.

**Tests:** `test_fetch_shareholding.py`, 10.

**Limits.** **Data starts in December 2015.** Earlier quarters come back empty.

### `src/build_panel.py` — Week 4 weekend: the firm-year panel (324 lines)

| | |
|---|---|
| **Run** | `python -m src.build_panel` |
| **Reads** | the sample tables, universe, prices, corporate actions, the index, shareholding |
| **Writes** | `derived/panel.parquet` (one row per firm × financial year, FY2010–2025), `derived/panel_coverage.csv` (**the coverage table for the paper**) |

**How it works.**
1. A security map links BSE codes to firms through the universe, and NSE symbols through the ISIN printed beside
   them, then today's list and symbol changes.
2. Each firm's main exchange is the one where it traded on more days.
3. Per financial year, the market signals are: trading days, return, excess return over the BSE 500, volatility,
   median turnover, Amihud illiquidity, zero-volume share and gap days.
4. Ownership is taken at the March quarter.
5. `first_public_fy` and `post_public` flag years after the first public action, for Stage 2 to drop.
6. The row count is asserted after every join.

**Tests:** `test_build_panel.py`, 3.

---

## Tests (`tests/`, 807 in all)

Each file tests the module of the same name.

- **Fixtures.** `tests/fixtures/` holds real SEBI pages and PDF pages. Most other tests use real sentences copied
  from orders.
- **No test touches the network or `derived/`.** Network calls are faked, and outputs go to temporary folders.
- **Run them all:** `python -m pytest tests -q` (~15 s). Keep them green after any change.

| File | Tests | | File | Tests |
|---|---|---|---|---|
| test_cache | 20 | | test_classify_fraud | 52 |
| test_scrape_listings | 82 | | test_detect_channel | 18 |
| test_fetch_pdfs | 57 | | test_build_orders | 4 |
| test_extract_text | 83 | | test_validation_sample | 30 |
| test_extract_windows | 96 | | test_score_validation | 23 |
| test_window_model | 64 | | test_universe | 7 |
| test_check_windows | 29 | | test_match_entities | 15 |
| test_amounts | 47 | | test_build_sample | 6 |
| test_extract_actions | 46 | | test_fetch_prices | 19 |
| test_regulations | 29 | | test_prices | 7 |
| test_extract_parties | 60 | | test_fetch_shareholding | 10 |
| | | | test_build_panel | 3 |

## Hand files (`hand/`, committed: your decisions and the coding rules)

| File | What it is | Who writes it | Read by |
|---|---|---|---|
| `coding_protocol.md` | the rules for window, fraud category, issuer and channel; changes only through its change log | written before coding; you own it | you, while coding; the auto-coders implement it |
| `recon_notes.md` | how SEBI's site works; how orders state windows, prior actions, sanctions, parties | Week 1–2 research | reference |
| `window_review.csv` | your keep / fix / drop on each flagged window | you (43 rows done) | `check_windows` |
| `window_model_devset.csv`, `window_model_holdout.csv` | 90 orders labelled to choose the model and prompt | agent | excluded from the blind sample |
| `fraud_category_devset.csv`, `detection_channel_devset.csv`, `protocol_examples.csv`, `seen_while_building.csv` | orders read while building the coders, or quoted in documents | agent | excluded from the blind sample (539 orders in all) |
| `validation_sample.csv`, `validation_pdfs/` | the blind coding sheet and PDF copies (`validation_pdfs/` is gitignored) | `validation_sample`, then you | `score_validation` |
| `validation_official.json` | the official validation scoring | `score_validation --official` | the paper |
| `match_review.csv` | your decisions on the 80–92 match band | `match_entities`, then you | `match_entities` |
| `sample_frozen.csv` | the frozen firm list | `build_sample` | the record of what the analysis used |

## Other documents

| File | What it is |
|---|---|
| `TASKS.md` | what is left for you, with instructions |
| `PROJECT_GUIDE.md` | how to use the data: files, queries, re-running, known gaps |
| `CLAUDE.md` | the briefing for AI agents: rules, status, data dictionary (§8), gotchas (§10) |
| `results_log.md` | every figure with date, script and scope; the source for numbers in the paper |
| `timeline.md` | the runbook: week by week to 1 December, the gates |
| `technical_plan.md` | the thesis design: stages, estimators, hypotheses |
| `audit/` | the 2026-09-28 audit of stages 1–9 (a snapshot) and its fixes |
| `paper/` | the LaTeX template the thesis will be written in |
