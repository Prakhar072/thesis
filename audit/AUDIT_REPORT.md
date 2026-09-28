# Audit report: silent errors and research integrity

This audit covers errors that would not crash the pipeline. Counts come from
the current `derived/` files (built 2026-09-24 → 2026-09-28, commit `44f7c88`)
and were measured with read-only scripts. **No code was changed.**

Severity:
- **CRITICAL:** silently wrong values in a key variable, concentrated where they matter most.
- **HIGH:** a wrong or biased key variable in a measurable subset.
- **MEDIUM:** a weak heuristic or a latent risk that affects interpretation.
- **LOW:** a latent risk that does not currently bite.
- **INFO:** worth knowing.

## Summary

| ID | Severity | Issue | Scope now | Visible? |
|---|---|---|---|---|
| C-1 | CRITICAL | Long penalty tables are split from their "hereby impose" clause, so penalties are lost | ≥28 penalty orders read "no penalty" (≈₹114 crore); ≥50 more are under-counted; largest cases most at risk | silent |
| C-2 | CRITICAL | `settlement_amount` adds the same amount once per sentence that restates it | median 2× inflation; 825 orders state one amount yet record it ≥2×; ₹1,913 crore recorded vs ₹1,342 crore | silent |
| H-1 | HIGH | Ungrouped figures truncated: "Rs. 500000" becomes ₹500, and Satyam's "₹1258.88 crore" disgorgement becomes ₹125 | 54 amounts in 45 orders (28 penalty, 10 settlement, 7 disgorgement) | silent (only tiny values hint) |
| H-2 | HIGH | Equal penalties in separate sentences merged as restatements; a stated total is ignored unless "each" appears | 50 of 750 orders with a stated total disagree with the computed total | silent |
| H-3 | HIGH | `order_date` is SEBI's listing date, not the date the order was signed | settlement: 67% differ (median 13 days); 871 orders differ by >30 days | silent |
| H-4 | HIGH | Text inside images on digital pages is never OCR'd | 1,283 WTM orders have no parties; effect on windows and penalties not measured | silent |
| H-5 | HIGH | `prior_action_date` is the earliest plausible recited SCN/interim date, from any matter or entity | 1,250 windowed orders have their first action on or before the window end; no lower bound for the 8,707 orders without a window | silent |
| H-6 | HIGH | Model windows: roughly 10–16% wrong (agent-judged), stored as `medium` like regex medium | 1,632 windows (13% of windows) | visible via `violation_method` |
| M-1 | MEDIUM | Party names that overlap ("X" and "X HUF") can mis-assign per-party penalties | 297 orders have such name pairs | silent |
| M-2 | MEDIUM | Role guesses are weak | 33.6% UNKNOWN; 6,753 of 10,236 ISSUER guesses rest on a name suffix alone | visible via `role_basis` |
| M-3 | MEDIUM | `matter_raw` includes SEBI's SAT-appeal annotation | 1,209 orders | visible if inspected |
| M-4 | MEDIUM | Regulation "violation" role uses a broad 250-character cue; 19% of orders have no list | 3,984 empty lists; 215,445 unresolved provisions | partly visible |
| M-5 | MEDIUM | A single party's PAN falls back to the first PAN anywhere in the header | not measurable from the outputs (no flag) | silent |
| M-6 | MEDIUM | An offline model run turns cache misses into MODEL_ERROR rows; the digest is taken from the first cached file | 0 now | visible in the log only |
| M-7 | MEDIUM | `build_orders` does not check that its inputs come from one consistent run | 0 missing joins now | silent |
| M-8 | MEDIUM | Window recall limits: top-3 candidates; NO_CANDIDATES never sent to the model; window length unchecked | 7,621 orders never examined; 57 one-day windows; 42 over 10 years | partly visible |
| M-9 | MEDIUM | Dropped or flagged windows keep method, confidence, kind and raw text with null dates | 8 rows | silent to naive filters |
| M-10 | MEDIUM | Duplicate observations are not flagged (identical text, shared PDFs) | 215 orders in 105 groups | silent |
| M-11 | MEDIUM | `debarred` / `warned` = False means "not found", and `penalty_outcome = unknown` mixes three cases | 9,500 unknown outcomes | silent |
| L-1 … L-11 | LOW | see below | | |
| I-1 … I-6 | INFO | see below | | |

---

## CRITICAL

### C-1: Long penalty tables lose their penalties
- **File / function:**
  - `src/extract_windows.py` → `split_sentences`, `_resplit_long`, `MAX_SENTENCE = 2000`;
  - `src/extract_actions.py` → `find_sanctions` (and `process`, which builds the sentence list).
- **Relevant code:**
  ```python
  # extract_windows._resplit_long: a "sentence" over 2,000 chars is cut at blank lines, then line breaks
  if m.start() - chunk_start > MAX_SENTENCE and last > chunk_start: out.append((chunk_start, last)) …
  # extract_actions.find_sanctions: imposing words and amounts must be in the SAME sentence
  if (imp := _IMPOSE.search(s)) and not cited:
      out.extend(_amount_mentions(stem, PENALTY, s, rel, …, anchor=imp.start()))
  ```
- **What it does:** a penalty paragraph followed by a table ("I hereby impose the
  following penalty on the Noticees – [table]") is one "sentence". Beyond 2,000
  characters it is cut into pieces. The piece with "impose" has no amounts; the
  pieces with the amounts have no "impose". So nothing is recorded, or only the
  rows in the first piece.
- **Why this is wrong:** the re-split exists for flattened tables in the window
  stage. The sanctions stage reuses it, and SEBI's largest orders, with many
  noticees and one row each, are exactly the long tables.
- **Example (observed):**
  - 46128 (Aksh Optifibre, 6 noticees): `penalty_total` is null and the outcome
    `unknown`, although the order imposes ₹10 crore + ₹15 lakh on the company and
    ₹10–20 lakh on each director. Its imposing sentence is 539 characters and the
    table 1,691.
  - Other orders with no penalty: 47376, 50927, 51810, 52571, 53836, 54152, 57008,
    58241, 61318 (₹60 crore in its split-off piece).
  - Under-counted: 39890 records ₹13 lakh; the ignored piece holds ₹3.16 crore.
- **Scope:** a scan of all orders found 28 orders with no penalty recorded whose
  imposing clause is followed by a split-off piece holding amounts. Those pieces
  hold ₹114 crore. A further 50 orders (48 AO) with a recorded penalty have such a
  piece.
  - This is a lower bound: only the piece right after the clause was checked.
  - It is an upper bound for the "partial" 50: some of those amounts may be
    totals or restatements.
- **Visible or silent:** silent. The order reads `penalty_outcome = unknown`,
  like any order with no penalty statement.
- **Suggested verification:**
  - Re-run the scan (`VERIFICATION_CHECKLIST.md` §4.3), and hand-check the 78 orders against their PDFs.
  - Rank penalty orders by `n_parties` and check the top 50 by hand. A fix would
    read a sentence *together with the following re-split pieces* when it has an
    imposing clause and no amounts.
- **Also possibly affected:**
  - window ranges split from their label;
  - regulation lists split from their instrument;
  - debarment clauses split from "securities market".

  These were not measured.

### C-2: Settlement (and some disgorgement) amounts are added once per restating sentence
- **File / function:** `src/extract_actions.py` → `summarise_order`.
- **Relevant code:**
  ```python
  settle = {(s.sentence, s.amount_rupees): s for s in sanctions if s.kind == SETTLEMENT and s.amount_rupees}
  if settle:
      o.settlement_amount = sum(s.amount_rupees for s in settle.values())
  dis = {(s.sentence, s.amount_rupees): s for s in sanctions if s.kind == DISGORGEMENT … and not s.is_total}
  ```
- **What it does:**
  - It de-duplicates by (sentence, amount), so the same amount in a *different*
    sentence is added again.
  - Settlement sentences are taken from anywhere in the order: the IC
    recommendation, the HPAC recommendation, the Board's approval, the remittance.
- **Why this is wrong:** modern settlement orders restate one settlement amount
  two to four times.
- **Example (observed):** 100354 has `settlement_amount` = ₹1,85,25,000. The
  order settles for ₹92,62,500, stated in "the IC recommended … settlement at …
  ₹92,62,500" and again in "HPAC … recommended … payment of ₹92,62,500".
- **Scope:**
  - Of 1,717 settlement orders with an amount, only 585 record exactly the largest single amount.
  - 1,013 record ≥2× it, including 825 where only **one** distinct amount occurs.
  - The median ratio is 2.0.
  - The sum over orders is ₹1,913 crore against ₹1,342 crore for the largest single amounts.
  - Disgorgement: 27 of 220 orders record ≥2× their largest amount.
- **Visible or silent:** silent. No coverage figure changes.
- **Suggested verification:**
  - Compute `settlement_amount / max(single amount)` per order (checklist §4.4).
  - Hand-check 20 orders across the ratio distribution.
  - For analysis, use the largest single settlement amount per order, or the
    amount in the remittance sentence, until this is fixed.
  - The Day 4 development checks covered penalties and prior actions, not settlement amounts.

---

## HIGH

### H-1: Ungrouped rupee figures are truncated to their first 1–3 digits
- **File / function:** `src/amounts.py` → `_FIGURE`, `AMOUNT`.
- **Relevant code:**
  ```python
  _FIGURE = r"\d{1,3}(?:(?:,\s?|\.)\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d+)?"
  AMOUNT = re.compile(rf"(?<![A-Za-z])(?P<cur>{_CURRENCY})\.?\s*(?P<num>{_FIGURE})\s*(?:/-|/)?\s*(?P<unit>{_UNIT})?(?![a-z])", re.I)
  ```
- **What it does:**
  - Regex alternation is ordered. The first alternative matches "500" out of
    "500000", and the rest of the pattern accepts, because `(?![a-z])` only
    forbids letters. So the second alternative `\d+` is never tried.
  - The "(Rupees five lakhs only)" words are not adjacent to the truncated
    match, so they are not attached and the words check cannot catch it.
- **Why this is wrong:** figures written without commas ("Rs. 500000/-",
  "Rs.50000", "INR 19908933.85") are common in older orders.
- **Example (observed):**
  - 10087 "Rs. 500000 (Rupees five lakhs only)" → ₹500;
  - 1211 "Rs. 60000" → ₹600;
  - 10170 "Rs.30000" → ₹300;
  - 20061 "Rs.1,10000/-" → ₹1,100;
  - 30250 "Rs. 45, 00000/-" → ₹45,000;
  - a disgorgement "INR 19908933.85" → ₹199;
  - **Satyam (30647): "₹1258.88 crore" jointly and severally → `disgorgement_total` = ₹125.**
- **The same pattern creates amounts:** "INR200004031" (an RTA registration number) is read as ₹200 and added to 36011's `penalty_total` (₹3,00,200).
- **Scope:** 54 amount mentions in 45 orders: 35 penalty mentions (28 orders,
  mostly 2004–2010, 22 of them with `penalty_total` < ₹1,000), 10 settlement
  and 9 disgorgement.
  - Test `tests/test_amounts.py` has no ungrouped figure.
- **Visible or silent:** silent. It shows up only as implausibly small amounts
  when the truncated amount is the whole penalty.
- **Suggested verification:**
  - Find mentions where the character after `amount_text` in `sentence` is a
    digit (checklist §4.2).
  - List `penalty_total < 10,000`.
  - Unit test: `find_amounts("Rs. 500000 (Rupees five lakhs only)")` should give 500000.

### H-2: Separate, equal penalties are merged; stated totals are ignored
- **File / function:** `src/extract_actions.py` → `penalty_rows`, `summarise_order`.
- **Relevant code:**
  ```python
  match = next((k for k in pool if k[0] == s.amount_rupees and (k[1] == _who(s) or k[1] == "" or _who(s) == "")), None)
  …
  if len(totals) == 1 and totals[0].amount_rupees != rows_sum and o.penalty_each:
      o.penalty_total, o.penalty_total_basis = totals[0].amount_rupees, "stated_total"
  ```
- **What it does:** a later sentence whose amounts all equal earlier counted
  amounts, with a generic party ("the noticee", or none), is treated as a
  restatement and dropped. A stated total is used only when "each" was seen.
- **Why this is wrong:** an order can impose ₹2 lakh under 15A for one violation
  and, in the next sentence, ₹2 lakh under 15HA for another.
- **Example (observed):** 14949.
  - The order imposes ₹2 lakh (SAST) + ₹2 lakh (PIT) in one sentence and
    "Further, … ₹2,00,000 … under Section 15HA" in the next.
  - It states "a consolidated penalty of ₹6,00,000".
  - `penalty_total` = ₹4,00,000 with basis `rows`.
- **Scope:** 750 orders state a penalty total; 50 disagree with `penalty_total`
  (17 lower, 33 higher). Some "totals" are subtotals or totals across parties, so
  not all 50 are errors.
- **Visible or silent:** silent.
- **Suggested verification:** list the 50 (checklist §4.5) and read them. Check
  also orders where two penalty sentences cite *different* penalty sections with
  equal amounts (21 candidates, found by the scan in §4.5).

### H-3: `order_date` is the listing (publication) date
- **File / function:** `src/scrape_listings.py` → `parse_rows`
  (`order_date = parse_order_date(cells[0])`).
- **What it does:** takes SEBI's listing date as the order date.
- **Why this may be wrong for your purpose:**
  - **Agreement with the signed date:** the listing date agrees with the date
    printed at the order's foot in 91% of the 20,267 orders that have one: AO
    97.8%, WTM 95.9%, ED/CGM 99.5%, **settlement 32.6%**.
  - **Settlement lag:** settlement orders are listed a median 13 days after
    signing (75th percentile 37 days; 95th percentile 721 days).
  - **Large gaps:** 871 orders differ by more than 30 days and 332 by more than a
    year. Some of these gaps are the "Date:" heuristic finding another date, so
    treat them as a list to check, not as confirmed errors.
- **Example:**
  - 14388: the consent order was "passed on this the 20th of November, 2007" and listed 40 days later.
  - WTM 21173–21192: 459 days apart. Unchecked: either republished orders or the heuristic.
- **Visible or silent:** silent. Every row has a date.
- **Implication:**
  - For the event study, the listing date is arguably the right date (public disclosure).
  - For detection lag (window end → first action), the order's own date is the
    action date, and settlement lags are overstated by weeks.
  - Decide which date each stage uses.
- **Suggested verification:** checklist §5.3 (the same script).

### H-4: Text inside images on digital pages is never read
- **File / function:** `src/extract_text.py` → `classify_page` (a NATIVE page is never OCR'd).
- **What it does:** a page with ≥100 good native characters uses its text layer
  only; images on it are ignored.
- **Why this is wrong:** some orders paste the noticee table or the penalty table
  as an image into a typed page.
- **Example (observed, indirectly):** 44442 and 45500. The header reads "In
  respect of: – ____ The aforesaid entities are hereinafter …", with the table
  itself missing from the text.
- **Scope:** 1,398 orders have no parties, 1,283 of them WTM. Not every such
  order is an image problem. The effect on windows or penalties is not measured.
- **Visible or silent:** silent, except `n_parties = 0`.
- **Suggested verification:**
  - In `text_pages`, list NATIVE pages with large `image_cover` (e.g. > 0.3) in
    orders with `n_parties = 0` (checklist §3.4).
  - Open 10 such PDFs.

### H-5: Prior-action dates are not tied to this entity or matter
- **File / function:** `src/extract_actions.py` → `find_prior_mentions`, `plausibility`, `summarise_order`.
- **What it does:** takes the earliest SCN/interim/confirmatory date recited
  *anywhere* in the text that is ≥ 1992-01-30, ≤ the order date and ≥ the window
  start (when there is one). Month-only dates resolve to the 1st.
- **Why this may be wrong:**
  - An order may recite another entity's SCN, a precedent's interim order, or a
    matter-level interim order (the ISO interim order of 20 Aug 2015 against 59 entities).
  - Without a window (8,707 orders with text) nothing bounds the date from below except 1992.
- **Scope:** 1,250 windowed orders have `first_action_date` on or before the window end, almost all ISO.
- **Visible or silent:** silent. The snippet is in `prior_action_raw`.
- **Suggested verification:**
  - For a sample, read `prior_action_raw` against the order.
  - Compare `prior_action_date` with `earliest_scn_date`.
  - Check for dates more than 10 years before `order_date` (checklist §5.4).

### H-6: Model windows are less accurate, and share a confidence label with regex windows
- **File / function:** `src/window_model.py` → `read_answer`, `combine`
  (`violation_confidence="medium"`); `quoted_in` (alphanumeric substring);
  `false_window_reason` (returns None when `_locate` fails).
- **Scope:** 1,632 windows. An agent review, not blind, judged 42–45 of 50 right.
- **Silent parts:**
  - A quote such as "2014" is "found" anywhere those digits occur.
  - When the quote cannot be located exactly, none of the rejection rules (correspondence, visit, statement, trade log) run.
- **Suggested verification:** the Week 3 blind sample stratifies 50 model windows;
  report accuracy by `violation_method`. Until then, run robustness checks without
  the model windows.

---

## MEDIUM

### M-1: Overlapping party names can mis-assign per-party penalties
- **File / function:** `src/extract_parties.py` → `name_pattern`, `mentions`,
  `link_penalties` (table layout takes `named[-1]`).
- **What it does:** "Anish Pravin Bagadia" also matches inside "Anish Pravin
  Bagadia HUF". Both matches start at the same position; the sort puts the higher
  party index last, and that party gets the amount.
- **Scope:** 297 orders have a party name that is a prefix of another's. It
  matters only where both are penalised in a table.
- **Verification:** for those orders, compare `penalty_amount` per party against the PDF table (checklist §6.4).

### M-2: Role guesses are weak
- **File / function:** `extract_parties.guess_role`.
- **Scope:**
  - UNKNOWN: 16,107 of 47,972 (33.6%).
  - ISSUER: 10,236, of which 6,753 are `role_basis = suffix` (any company with no other evidence).
  - `context:` roles depend on a ±80/+250-character window around the first mention.
  - A party named only as "Noticee 2" in the body gets no window (e.g. 46128).
- **Verification:** the Week 3 coding of primary issuer; a 50-party role check stratified by `role_basis`.

### M-3: SAT-appeal text in `matter_raw`
- **File / function:** `scrape_listings.clean_title_attr` keeps appeal text in
  the title by design, and `extract_parties.matter_from` takes the title's "in
  the matter of …" remainder.
- **Scope:** 1,209 orders. `issuer_from_matter` usually still cuts correctly at
  "Limited". Any grouping by `matter_raw` string will split one matter into
  "with appeal" and "without appeal" versions.
- **Verification:** `orders.matter_raw.str.contains("SAT Appeal")`.

### M-4: Regulation roles and coverage
- **File / function:** `regulations.find_citations` (`_VIOLATION_CUE` within 250 characters; chaining); `match_instrument` ("the Act" → SEBI Act when undefined).
- **Scope:**
  - 3,984 orders with text have an empty `regulations` list.
  - 215,445 provisions are unresolved (no instrument after the list).
  - The violation cue includes "fail", "default", "alleg", "delay" and "charg", which can come from an unrelated clause.
- **Verification:** read 30 random orders' `regulations` against their charge
  paragraph; list orders whose `regulations` is empty but `penalty_provisions` is not.

### M-5: Single-party PAN fallback
- **File / function:** `extract_parties.process`:
  `if not p.pan and p.source.startswith("header") and len(parties) == 1 and (m := PAN.search(flat, 0, hend)): p.pan = m.group(0)`.
- **Risk:** the first PAN in the header may be the company "in the matter of",
  not the noticee's. The PAN 4th letter then also sets `entity_type`.
- **Scope:** unknown; no flag records that the fallback was used.
- **Verification:** for single-party orders, check that the PAN's 4th letter
  agrees with the name (P for a person's name, C for "Ltd"); 98 order-party pairs
  already share a PAN within an order.

### M-6: Offline model runs can lose windows without failing
- **File / function:**
  - `window_model.ask` catches `llm.LlmError` (including `CacheMiss`) and returns a MODEL_ERROR row;
  - `_cached_digest` returns the digest from the first cached JSON (sorted path) whose model name matches.
- **Risk:**
  - If `raw/llm/` holds answers from two model digests (e.g. after a re-pull), an
    offline run may pick the wrong digest.
  - Every order would then be a cache miss and become MODEL_ERROR, with 0 windows from the model.
  - The run exits 0.
- **Now:** 0 MODEL_ERROR rows, one digest, all 2,712 answers from cache.
- **Verification:** after any model re-run, check `window_model.model_status` for MODEL_ERROR and `model_digest.nunique() == 1`.

### M-7: No run-consistency check across stages
- **File / function:** `build_orders.build` (left joins; missing orders only logged); no stage compares input timestamps or `rules_version`.
- **Risk:** re-running stage 6 without re-running stage 7 leaves prior-action
  plausibility computed with old windows. A partial `--scratch` or `--category`
  run copied into `derived/` would leave NA rows.
- **Verification:** checklist §1.3: file modification times in dependency order, `rules_version` values, and "0 listed orders without a row" in `logs/build_orders.log`.

### M-8: Window recall limits and unchecked lengths
- **Top-3 candidates:** a window in a lower-ranked sentence is lost.
- **NO_CANDIDATES:** 7,621 orders never reach the model. The Week 3 sample has a no-window stratum of 40 to measure what is missed.
- **Length:** no rule checks window length. 57 windows last one day and 174 at
  most 7 days (possibly uncaught inspection visits, or single-day
  manipulation); 42 last more than 10 years.
- **Verification:** checklist §5.2.

### M-9: Flagged or dropped windows keep their descriptors
- **File / function:** `check_windows.check` nulls only `violation_start` and `violation_end`.
- **Scope:** 8 rows (REVIEWED_DROP) keep `violation_method`, `violation_confidence`
  (5 medium, 3 high), `window_kind` and `violation_raw`. A filter on
  `violation_confidence.isin(["high","medium"])` alone includes them.
- **Verification:** always filter on `violation_start.notna()`.

### M-10: Duplicate observations are not flagged
See P-2.

### M-11: Absence is recorded as negative or "unknown"
- `debarred`, `warned`: `False` when no matching sentence was found.
- `penalty_outcome = unknown` (9,500 orders with text) mixes three cases: orders
  that impose no penalty and do not say so, penalties the extractor missed
  (C-1), and orders whose type rarely penalises (5,629 WTM, 1,976 settlement).
- **Verification:** never read `unknown` or `False` as "no sanction"; stratify by `order_type`.

---

## LOW

| ID | File / function | Issue | Now | Verification |
|---|---|---|---|---|
| L-1 | `cache.fetch`, `default_validator` | Non-PDF, empty, or differently-worded blocked bodies are cached forever; a re-run never retries without `--force` | 0 NOT_A_PDF | `order_documents.status` |
| L-2 | `fetch_pdfs.archive_pdf` | Never overwrites an existing `raw/pdfs/<stem>.pdf` | 0 of 19,192 hash mismatches between text and fetch stages | checklist §2.3 |
| L-3 | `scrape_listings` + cache | Listing pages are cached forever; the corpus is a snapshot of 2026-09-23 | — | re-scrape with `--force` to update |
| L-4 | `scrape_listings.write_parquet`, `fetch_pdfs.write_parquet` | Not atomic; `fetch_pdfs` exits 0 with failures; a failed listing page still writes a partial parquet (exit 1) | 836/836 pages ok | `reports/listing_pages.csv` |
| L-5 | `extract_windows.parse_date` | Numeric dates always day-first; 2-digit years pivot at 80; years 1980–2030 only | — | spot-check windows before 1995 (reviewed) |
| L-6 | `as_start` / `as_end` | Month precision → 1st / last day; year → 1 Jan / 31 Dec; FY → 1 Apr / 31 Mar; prior actions → 1st | 1,537 month ends, 182 FY, 20 year | use `*_precision` in sensitivity checks |
| L-7 | `extract_text.extract_inline` | 1,582 inline orders (2002–2011) are one flattened line: no header stripping, no line-based splitting | — | compare extraction rates, inline vs PDF |
| L-8 | `extract_windows` | `violation_confidence = "none"` (the runbook's "order says no period") is never assigned | — | — |
| L-9 | every `process()` | `except Exception` turns a crash into an error row; in `extract_windows` the row gets `window_status = NO_TEXT`, indistinguishable from no text except via `error` | 0 errors in windows, actions, parties | checklist §1.4 |
| L-10 | `extract_text.image_cover` | Sums overlapping image areas (capped at 1) | harmless | — |
| L-11 | all stages | The 12 orders without text stay in every table with nulls | 12 | `text_status != "OK"` |

## INFO

| ID | Note |
|---|---|
| I-1 | `CATEGORIES.published_count` constants (Settlement 1,991, AO 12,002) are older than the scraped counts (1,994, 12,003). They are only used in a log line. |
| I-2 | The stage order is not enforced by code; the current file timestamps are in dependency order. |
| I-3 | Model answers are reproducible: all 2,712 from `raw/llm/`, one digest, `prompt_version wm-v1`. |
| I-4 | SAT, court and Reg. 30A orders are out of scope. SAT annotations exist only for 2003–2022 listings. |
| I-5 | Parallel stages sort rows before writing, so outputs are deterministic. `validation_sample` uses fixed seeds. |
| I-6 | Timestamps (`scraped_at`, `resolved_at`, `extracted_at`) are UTC. All analysis dates are naive calendar dates; no timezone boundary issue was found. |

---

## Phase 4: research and database integrity

### P-1: Unit of analysis and the ISO cluster
- One row is a published order.
- "Illiquid Stock Options at BSE" is 2,243 orders (2,231 with a window, 18.4% of all windows), all sharing one window (Apr 2014 – Sep 2015) and one matter-level interim order.
- The 12,142 windows contain only **3,390 distinct** (start, end) pairs.
- Order-level statistics (median lag, extraction rate by year, penalty distributions) are dominated by this cluster; the 2023 spike in orders is this wave.
- **Diagnostic:** `title.str.contains("illiquid stock option", case=False)`; distinct windows; results with and without ISO.

### P-2: Duplicate observations
- **Identical text:** 215 orders fall in 105 groups (102 pairs, two triples, one group of five).
  - 3 groups span two categories.
  - 19 groups have different listing dates.
  - Examples: 12784 / 14191; 4145 / 4147 (a consent order listed twice with different titles); 82613 / 82625 ("in respect of 4 entities").
- **Shared PDFs:** 145 orders share 70 PDFs, often one combined order per noticee.
- **Title and date:** 108 listing rows share a title and date with another row. Not all are duplicate documents.
- **Effect:** each copy carries the full order's window, penalty total and party list. Summing penalties over orders double-counts, and counts of "orders" overstate distinct decisions.
- **Diagnostic:** group `text_documents.text_sha256` and `order_documents.pdf_sha256` (checklist §2.4). Decide per group whether to keep one row, or one row per noticee with the penalty split.

### P-2b: Documents that are not orders
- **What:** SEBI lists corrigenda and addenda as orders in their own right. By
  title, about 188 rows (WTM 141, AO 17, ED/CGM 17, settlement 13) are
  "Corrigendum to …" or "Addendum to …" documents. A few of these may be orders
  whose title merely mentions a corrigendum.
- **How it shows:** they are among the shortest texts (e.g. 32520, 28627, 45522).
- **Impact:** 6 of them carry a window and 1 a penalty. They inflate order counts,
  and their "window" or "penalty" restates the original order's.
- **Diagnostic:** `title.str.contains(r"^\s*(?:corrigendum|addendum|erratum)|\b(?:corrigendum|addendum|erratum)\s+to\b", case=False)` (checklist §2.4).

### P-3: Paths by which a record disappears or loses a field without an error

| Stage | Path | Records now | What reveals it |
|---|---|---|---|
| 1 | A listing row with <2 cells or no anchor is skipped | not counted; the listing total reconciles exactly with SEBI's counts, so 0 now | `reports/listing_pages.csv` n_rows vs 25 per page |
| 1 | A failed or empty page | 0 | `listing_pages.csv` status |
| 1 | Published after 2026-09-23 | all later orders | re-scrape |
| 2 | FAILED_DETAIL / FAILED_DOWNLOAD / NO_DOCUMENT | 12 | `order_documents.status`, `fetch_failures.csv` |
| 2 | The iframe is an annexure, not the order | unknown (32 fixed earlier) | short text, low stopword ratio; checklist §3.3 |
| 3 | Text in images on NATIVE pages | unknown (H-4) | `text_pages.image_cover` on NATIVE pages |
| 3 | Short or garbled text | 26 orders < 1,000 characters; 25 with stopword ratio < 0.15; 7 with mean OCR confidence < 70 | `text_documents` |
| 4 | NO_CANDIDATES (never examined further) | 7,621 | `windows.window_status` |
| 4 | The window sentence outside the top 3 | unknown | Week 3 no-window stratum |
| 5 | MODEL_NONE / UNVERIFIED / REJECTED | 907 / 135 / 36 | `window_model.model_status`, `note` |
| 6 | Flagged → dropped | 8 dropped; 0 pending | `windows_checked.sanity_status` |
| 7 | Penalty table split from its clause | ≥28 missing, ≥50 partial | checklist §4.3 |
| 7 | Imposing sentence in the first 40% | 5 | checklist §4.3 variant |
| 7 | Truncated figures | 45 orders | checklist §4.2 |
| 7 | Restatement merge | ≤50 (of the stated-total disagreements) | checklist §4.5 |
| 7 | No plausible prior action | 3,931 → `first_action_date = order_date` | `first_action_source = "order"` |
| 8 | No parties found | 1,398 (WTM 1,283) | `n_parties == 0` |
| 8 | Penalty not linked to a party | 162 none, 58 partial | `penalty_link_status` |
| 8 | No violated-regulation list | 3,984 | `regulations == "[]"` |
| 9 | A stage output missing an order | 0 | `logs/build_orders.log` |

### P-4: Selection and classification effects to report
- **Windows** exist for 58.2% of orders with text, with a strong gradient by type (AO 71.7%, ED/CGM 61.1%, WTM 42.5%, settlement 27.4%). Drop-out is associated with type (Cramér's V 0.33) and year (0.30). Any Stage 1 sample is therefore selected.
- **Dates** (H-3, L-6):
  - `order_date` is the publication date.
  - Windows at month / year / FY precision are widened to full months or years (1,537 month ends, 182 FY, 20 year).
  - Month-only prior actions are moved to the 1st.
- **Entity identification:**
  - 67% of parties have a PAN.
  - `issuer_guess` exists for 63.4% of orders and comes from the title text, not from a match to any listed-company master (that is Week 4).
  - Two parties in the same order share a PAN 98 times (a proprietor's PAN printed for each firm).
- **False positives:**
  - windows from uncaught inspection visits or wrong model ranges;
  - regulation citations marked "violated" from a nearby cue;
  - prior actions from other matters;
  - inflated settlement amounts.
- **False negatives:**
  - windows in unscored sentences;
  - penalties in split tables;
  - parties in images;
  - regulations cited only as circulars.

### P-5: What must be verified before statistical use
See `VERIFICATION_CHECKLIST.md`. The shortest list:
1. C-1, C-2, H-1 and H-2 before *any* use of rupee amounts.
2. H-3 before computing lags.
3. P-1 and P-2 before counting orders or summing across them.
4. H-6 and the Week 3 blind sample before trusting windows.
