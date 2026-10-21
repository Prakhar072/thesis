# Project guide — what we built, where it lives, how to use it

*Written 2026-09-24 for Week 1; updated 2026-10-21 (Weeks 3–4 built).*

- **What is left for you:** [TASKS.md](TASKS.md).
- **How each file of the code works:** [CODE_GUIDE.md](CODE_GUIDE.md).
- **Figures with provenance:** [results_log.md](results_log.md).
- **Technical briefing for AI agents:** [CLAUDE.md](CLAUDE.md).
- **Runbook:** [timeline.md](timeline.md). **Thesis design:** [technical_plan.md](technical_plan.md).

Every function in `src/` documents its inputs and output with their types (`Args:` / `Returns:` in its docstring).

---

## 0. Where things stand (2026-10-21)

| Part | State |
|---|---|
| Week 1 — corpus: listings, documents, text | run |
| Week 2 — windows, prior actions, sanctions, parties, regulations | run; `derived/orders.parquet` |
| Week 3 — fraud category, detection channel | run |
| Week 3 — blind validation sample and scorer | built; **yours to draw, code and score** |
| Week 4 — listed universe, matching, analysis sample, prices, ownership, panel | built and dry-run; **yours to run** (two downloads, one hand review) |

The order to do your part in, with instructions: [TASKS.md](TASKS.md).

## 1. Week 1 in one page (the history)

**Result:** every order in SEBI's four primary enforcement categories —
**20,861 orders, Dec 2000 to Sep 2026** — is listed, its document downloaded,
and its text extracted (OCR where needed). Text exists for **20,849 orders
(99.94%)**, and everything rebuilds from disk without touching SEBI again.

| When | What happened | Output |
|---|---|---|
| Before 23 Sep | Repo, venv, first cache, recon notes (cURL + categories), 55 orders downloaded by hand | `hand/recon_notes.md`, `raw/pdfs/order_files_by_category/` |
| 23 Sep | Cache rebuilt to key POST bodies; SEBI's listing protocol worked out (pagination is `doDirect`, not `nextValue`; a firewall needs browser-like headers); all 836 listing pages scraped in 30 min | `derived/listings.parquet` |
| 23–24 Sep | Document fetcher: PDFs sit in an iframe viewer; 1,582 older orders are printed on the page with no PDF; 27.5 h run, 40,017 requests | `raw/pdfs/`, `derived/order_documents.parquet` |
| 24 Sep | Text extraction with OCR fallback; found broken text layers that look fine to a character count; OCR needed for only 3.67% of PDF orders | `derived/text/`, `derived/text_*.parquet` |
| 24 Sep | Verification pass fixed: annexures taken for orders (32), HTML in titles (1,541) — which revealed 1,414 SAT-appealed orders — a dead http link, and OCR of rotated scans (207 bad pages → 25) and outlined text (1 order) | re-run of the extraction |

Two findings to carry into the thesis design (details in `results_log.md`):

- **One matter dominates.** "Illiquid Stock Options at BSE" is 2,243 orders
  (10.8% of the corpus) with essentially one violation window. The 2023→2024
  fall in orders is mostly that wave ending. Decide early whether your unit is
  the order, the matter or the noticee.
- **Titles don't tell you finality.** Only 15.5% of WTM titles say final,
  interim or confirmatory, so finality has to come from the order text.

## 2. Where everything is

| Path | What it is | Size | Rebuild if lost? |
|---|---|---|---|
| `src/cache.py` | the only door to the network; every request cached on disk | — | code (git) |
| `src/scrape_listings.py` | stage 1: listing pages → one row per order | — | code |
| `src/fetch_pdfs.py` | stage 2: detail page → the order's PDF or inline text | — | code |
| `src/extract_text.py` | stage 3: text layer, OCR fallback, quality checks | — | code |
| `src/extract_windows.py` | stage 4: violation-window candidate sentences + regex v1 | — | code |
| `src/window_model.py`, `src/llm.py` | stage 5: local-model fallback for windows regex could not settle; cached model calls | — | code |
| `src/check_windows.py` | stage 6: sanity rules, flagged windows, extraction rate | — | code |
| `src/extract_actions.py`, `src/amounts.py` | stage 7: prior SCN / interim-order dates, penalties and other sanctions; the rupee normaliser | — | code |
| `src/extract_parties.py`, `src/regulations.py` | stage 8: noticees (name, PAN/DIN/CIN, role guess, penalty per party) and the regulations each order says were violated | — | code |
| `src/classify_fraud.py` | stage 9: fraud category per order, from the violated provisions and (for PFUTP) the narrative, following `hand/coding_protocol.md` | — | code |
| `src/detect_channel.py` | stage 10: detection channel per order (how the matter reached SEBI), from rules and a checked local-model fallback, following protocol §5 | — | code |
| `src/build_orders.py` | stage 11: `derived/orders.parquet`, one row per order — the Week 2 checkpoint table, now with fraud category and detection channel | — | code |
| `src/validation_sample.py`, `src/score_validation.py` | Week 3: draws the 150-order blind coding sheet; scores it (Day 5) | — | code |
| `src/universe.py`, `src/names.py` | Week 4 Day 1: every BSE/NSE equity (live, suspended, delisted) and the names each firm has had; the one name normaliser | — | code |
| `src/match_entities.py` | Week 4 Day 2: order issuers and company noticees matched to listed firms (codes, then names; a hand-review band) | — | code |
| `src/build_sample.py` | Week 4 Day 3: fraud firm-years, weighted control firms, enforcement events — frozen once | — | code |
| `src/fetch_prices.py`, `src/prices.py` | Week 4 Day 4: NSE/BSE daily prices, corporate actions, BSE 500; split/bonus adjustment and the sanity plot | — | code |
| `src/fetch_shareholding.py` | Week 4 Day 5: promoter holding and pledging from BSE shareholding patterns (Dec 2015 on) | — | code |
| `src/build_panel.py` | Week 4 weekend: the firm-year panel and its coverage table | — | code |
| `tests/` | 807 tests, with real SEBI pages and order sentences as fixtures | — | code |
| `raw/cache/` | every HTTP response ever fetched (opaque hashed names) | 8.7 GB | **no** — needs re-scraping (days) |
| `raw/pdfs/<order_id>.pdf` | the order PDFs, hard-linked to `raw/cache` | ~0 extra | from the cache |
| `raw/pdfs/order_files_by_category/` | your 55 hand-downloaded orders | small | your files |
| `raw/ocr/` | OCR results per PDF page | 35 MB | yes, ~45 min of OCR |
| `raw/llm/` | every model answer, keyed by model digest + prompt | ~10 MB | yes, ~55 min of GPU, but a re-pulled model may answer differently |
| `derived/listings.parquet` | one row per listed order | 1.7 MB | yes, 10 s offline |
| `derived/order_documents.parquet` | where each order's document is, and whether it was fetched | 3.5 MB | yes, ~3 min offline |
| `derived/text/<order_id>.txt` | order text, pages separated by form feed `\f` | 0.73 GB | yes, ~5 min + OCR cache |
| `derived/text_documents.parquet` | one row per order: text source, OCR stats, status | 3.7 MB | with the text |
| `derived/text_pages.parquet` | one row per PDF page: how its text was obtained | 1.6 MB | with the text |
| `derived/window_candidates.parquet` | top 3 candidate sentences per order, with what regex v1 read from each | 5.5 MB | yes, ~1 min |
| `derived/windows.parquet` | one row per order: regex v1 violation window, confidence, source sentence, routing status | 1.4 MB | yes, ~1 min |
| `derived/window_model.parquet` | one row per order sent to the model: its answer, the checks, why rejected | small | from `raw/llm/` in seconds |
| `derived/violation_windows.parquet` | one row per order: regex window, else verified model window — what Day 3 reads | small | with stage 5 |
| `derived/windows_checked.parquet` | one row per order: the window after the sanity rules and your review — the Week 2 window table | small | with stage 6 |
| `derived/window_flagged.csv` | windows that failed a sanity rule | tiny | with stage 6 |
| `derived/order_actions.parquet` | one row per order: prior-action date, first-action date, penalty total, other sanctions | small | yes, ~1 min |
| `derived/prior_action_mentions.parquet`, `derived/sanction_mentions.parquet` | every SCN / interim date and every sanction amount found, with its sentence | small | with stage 7 |
| `derived/order_entities.parquet` | one row per order × party: name, PAN/DIN/CIN, type, role guess (with its evidence), penalty on that party, sanction types | 2.3 MB | yes, ~1 min |
| `derived/order_parties.parquet` | one row per order: matter, issuer guess, party counts by role, violated regulations (JSON), penalty sections, how penalties linked | 3.5 MB | with stage 8 |
| `derived/regulation_mentions.parquet` | every provision cited, with instrument, role (violation / penalty / power / cited) and context | 30 MB | with stage 8 |
| `derived/order_categories.parquet` | one row per order: fraud category, its confidence, the rule that decided it, the evidence, every category charged | small | yes, ~1.5 min (stage 9) |
| `derived/order_channels.parquet` | one row per order: detection channel, finer detail, confidence, method (rule / model / none), the sentence it was read from | small | rules ~1.3 min; model ~2 h first time, seconds from `raw/llm/` (stage 10) |
| `derived/orders.parquet` | **one row per order, 71 columns — the table to browse**: window, prior action, penalty, matter, issuer, regulations, fraud category, detection channel | small | yes, seconds (stage 11) |
| `derived/extraction_rate.csv` | extraction rate overall and by type, year, method | tiny | with stage 6 |
| `derived/validation_key.parquet`, `derived/validation_*` | the blind sample's strata and weights (don't open while coding); later the scoring, the errors and the tables | small | from your sheet |
| `derived/universe.parquet`, `firms.parquet`, `name_aliases.parquet` | every BSE/NSE equity (live, suspended, delisted); one row per firm; every name a firm has had | small | yes, seconds (cached lists) |
| `derived/order_issuers.parquet`, `entity_matches.parquet`, `name_matches.parquet` | each order's issuer matched to a listed firm; company parties matched; one row per distinct name | small | yes, ~35 s + your review |
| `derived/sample.parquet`, `sample_fraud_years.parquet`, `sample_events.parquet` | the frozen analysis sample: fraud firms and controls, fraud firm-years, enforcement events | small | **frozen once** (`hand/sample_frozen.csv` keeps the list) |
| `derived/prices/`, `price_days.parquet`, `corporate_actions.parquet`, `market_index.parquet` | daily NSE/BSE prices from 2004, the download ledger, splits and bonuses, the BSE 500 | ~1–2 GB | from the cache in minutes once downloaded (~7–8 h first time) |
| `derived/shareholding.parquet` | promoter holding and pledging per firm and March quarter (Dec 2015 on) | small | from the cache once downloaded (~6–9 h first time) |
| `derived/panel.parquet`, `panel_coverage.csv` | the firm-year panel FY2010–2025 and its coverage table | small | yes, ~1 min |
| `hand/validation_sample.csv`, `hand/validation_pdfs/` | your blind coding sheet (150 orders) and copies of their PDFs | — | git (your coding); the copies are not in git |
| `hand/match_review.csv` | your accept / reject / firm_id decision on each borderline name match | — | git (your decisions) |
| `hand/sample_frozen.csv`, `hand/validation_official.json` | the frozen firm list; the official validation figures | — | git |
| `hand/window_review.csv` | your keep / fix / drop decision on each flagged window | — | git (your decisions) |
| `derived/fetch_failures.csv` | the orders whose document could not be fetched | tiny | with stage 2 |
| `reports/listing_*.csv` | per-page scrape log, duplicate conflicts (none), rows needing review | tiny | with stage 1 |
| `logs/` | run logs for each stage | small | — |
| `results_log.md` | every figure with its source and date | — | git |
| `hand/recon_notes.md` | SEBI protocol notes; §7 how orders state the violation period; §8 prior actions and sanctions; §9 parties and regulations | — | git |
| `hand/window_model_devset.csv`, `hand/window_model_holdout.csv` | 90 agent-labelled orders used to choose the model and prompt — keep them out of the Week 3 blind sample | — | git |
| `hand/coding_protocol.md` | **the coding rules** for window, fraud category, primary issuer and (Day 2) detection channel — one paragraph, inclusion rule and worked example per category; changes only through its change log | — | git |
| `hand/protocol_examples.csv`, `hand/fraud_category_devset.csv`, `hand/detection_channel_devset.csv`, `hand/seen_while_building.csv` | the protocol's worked examples, the agent-labelled orders used to build the fraud-category (84) and channel (58) rules, and every other order read while building them (206) — all excluded from the blind sample (458 in total) | — | git |
| `audit/` | the 2026-09-28 audit (map, logic notes, silent errors, verification checks); "Status after fixes" in `AUDIT_REPORT.md` | — | git |

Git tracks code, tests, `hand/`, `paper/` and the `.md` files. `raw/`,
`derived/`, `logs/`, `reports/` are ignored: rebuildable, or too big.

**Order IDs.** Files are named by SEBI's own order ID (`104570.txt`,
`104570.pdf`). The 76 orders without an ID use `noid_<12 hex chars>` instead.

**Don't edit the PDFs.** Files in `raw/pdfs/` share their bytes with
`raw/cache/`. Viewing is fine; annotating and saving in a PDF editor would alter
the cached copy too. Copy a file elsewhere first if you want to mark it up.

## 3. Using the data

Run from the repo root with the venv active (`cd ~/projects/thesis && source .venv/bin/activate`).

### pandas

```python
import pandas as pd

# numpy_nullable keeps order_id as an integer even where it is missing
read = lambda p: pd.read_parquet(p, dtype_backend="numpy_nullable")
L = read("derived/listings.parquet")          # 20,861 orders: title, date, type, sat_appeals …
D = read("derived/order_documents.parquet")   # document shape, PDF path, fetch status
T = read("derived/text_documents.parquet")    # text path, pages, OCR stats, status
P = read("derived/text_pages.parquet")        # one row per PDF page

# the text of one order, split into pages
row = T[T.order_id == 104570].iloc[0]
pages = open(row.text_path, encoding="utf-8").read().split("\f")

# titles next to their text, for all 20,861 orders. Join on text_stem, not
# order_id: pandas matches missing order_ids to each other, which would
# cross-join the 76 orders that have no ID.
from src.extract_text import text_stem
L["text_stem"] = [text_stem(None if pd.isna(o) else int(o), u)
                  for o, u in zip(L.order_id, L.source_url)]
orders = L.merge(T[["text_stem", "text_path", "source", "n_pages", "status"]],
                 on="text_stem", validate="one_to_one")
```

While a pipeline run is in progress, its parquet files hold the last checkpoint
(e.g. 20,000 of 20,861 rows). Query them after the run finishes.

### DuckDB (SQL straight on the parquet files; the runbook's analysis layer)

```python
import duckdb
duckdb.sql("""
    SELECT year(order_date) AS year, order_type, count(*) AS orders
    FROM 'derived/listings.parquet'
    GROUP BY ALL ORDER BY year, order_type
""").show()
```

More queries you will want:

```sql
-- orders SEBI marks as appealed to SAT, with their text files
SELECT l.order_id, l.order_date, l.title, l.sat_appeals, t.text_path
FROM 'derived/listings.parquet' l
JOIN 'derived/text_documents.parquet' t USING (order_id)
WHERE l.sat_appeals IS NOT NULL;

-- the corpus without the Illiquid Stock Options cluster
SELECT order_type, count(*) FROM 'derived/listings.parquet'
WHERE NOT regexp_matches(lower(title), 'illiquid stock option')
GROUP BY 1;

-- one document per distinct PDF (70 PDFs are shared by 145 orders)
SELECT count(DISTINCT pdf_sha256) FROM 'derived/order_documents.parquet' WHERE status = 'OK';

-- orders whose text came (partly) from OCR, least confident first
SELECT order_id, order_type, order_date, n_pages, n_ocr_done, mean_ocr_conf
FROM 'derived/text_documents.parquet'
WHERE n_ocr_done > 0 ORDER BY mean_ocr_conf;

-- the 12 orders with no text, and why
SELECT d.order_id, d.order_type, d.status, d.error
FROM 'derived/order_documents.parquet' d
WHERE d.status NOT IN ('OK', 'OK_INLINE');
```

### Opening an order's PDF from WSL

```bash
explorer.exe "$(wslpath -w raw/pdfs/104570.pdf)"     # opens in your Windows PDF viewer
```

### Reading a cached SEBI page (no network)

```python
from src import cache
html = cache.get(L.loc[L.order_id == 104570, "source_url"].iloc[0], offline=True)
```

### Columns worth knowing

- `listings.doc_kind`: `HTML_DETAIL` (normal), `DIRECT_PDF` (listing links a PDF), `OTHER`.
- `order_documents.status`: `OK` (PDF archived), `OK_INLINE` (text printed on the
  web page, no PDF), `FAILED_DOWNLOAD`, `FAILED_DETAIL`, `NO_DOCUMENT`.
- `text_documents.source`: `PDF`, `INLINE_HTML`, `NONE`; `status`: `OK` or `NO_SOURCE`.
- `text_pages.page_class`: `NATIVE`, `SCANNED`, `BROKEN_LAYER`, `VECTOR_TEXT`, `SPARSE`;
  `method`: `native` or `ocr`; `ocr_conf` 0–100.
- After your re-run: `text_documents.n_ocr_rotated`, `text_pages.ocr_variant`
  (e.g. `r90_200` = rotated 90°, 200 dpi) and `text_pages.drawings`.

Full column list: [CLAUDE.md §8](CLAUDE.md#8-data-dictionary).

## 4. Re-running the pipeline

```bash
python -m pytest tests -q                                   # 807 passed
python -m src.scrape_listings --offline                     # stage 1 from cache, ~10 s
python -m src.fetch_pdfs --offline                          # stage 2 from cache, ~3 min
python -m src.extract_text                                  # stage 3, ~5 min with OCR cached
python -m src.extract_windows                               # stage 4, ~1 min
python -m src.window_model                                  # stage 5, ~55 min on the GPU (Ollama must be serving)
python -m src.window_model --offline                        # stage 5 again, from raw/llm/ in seconds
python -m src.check_windows                                 # stage 6, sanity rules + extraction rate, seconds
python -m src.extract_actions                               # stage 7, prior actions + sanctions, ~1 min
python -m src.extract_parties                               # stage 8, parties + regulations, ~1 min
python -m src.classify_fraud                                # stage 9, fraud category, ~1.5 min
python -m src.detect_channel                                # stage 10, detection channel: rules, then the model (~2 h on the GPU)
python -m src.detect_channel --offline                      # stage 10 again, model answers from raw/llm/ (--no-model: rules only)
python -m src.build_orders                                  # stage 11, derived/orders.parquet, seconds

# Week 3 (validation) — see TASKS.md B
python -m src.validation_sample                             # draw the blind sample once; --check while coding
python -m src.score_validation --official                   # score the finished sheet once

# Week 4 (market data) — see TASKS.md A; the two fetches run for hours, one at a time
python -m src.fetch_prices                                  # prices, corporate actions, BSE 500 (~7-8 h first time)
python -m src.universe                                      # listed universe, seconds
python -m src.match_entities                                # issuers -> listed firms, ~35 s (re-run after your review)
python -m src.build_sample                                  # freeze the analysis sample, once
python -m src.prices --check TATASTEEL                      # the price sanity plot
python -m src.fetch_shareholding                            # promoter holding and pledging (~6-9 h first time)
python -m src.build_panel                                   # the firm-year panel and its coverage table, ~1 min
```

`derived/orders.parquet` is the table to open when you want to look at rows: one
order per row with its window, prior action, penalty, matter, issuer guess, the
regulations it says were violated, its fraud category (with `fraud_category_rule`
saying why, and `fraud_category_confidence`) and its detection channel (with
`detection_raw`, the sentence it was read from, and `detection_method`). Who was penalised and for how much is in
`derived/order_entities.parquet` (one row per order × party). Don't add up
`penalty_amount` there for money — a joint penalty sits on every party liable.

Every stage is resumable and safe to interrupt. For a job that should outlive the
terminal: `setsid nohup python -m src.extract_text > logs/extract_full.out 2>&1 < /dev/null &`.
Try a change on a sample first: `python -m src.extract_text --sample 500 --scratch /tmp/try`
writes everything to `/tmp/try` and leaves `derived/` alone.

To pick up orders SEBI publishes later, the listing pages must be re-fetched:
the cache would otherwise return the old page 1, and each new order shifts every
page. Run `SEBI_CONTACT="you@…" python -m src.scrape_listings --force` (836
requests, ~30 min), then `SEBI_CONTACT="you@…" python -m src.fetch_pdfs`, which
downloads only the new orders' documents, then stage 3.

## 5. Known gaps

- 12 orders have no text: 5 dead links on SEBI's side, 7 pages with no PDF (listed by the query above).
- Running headers and footers are still in the text files, often at the top of each
  page; `extract_windows.strip_repeated_lines` blanks them (offsets unchanged)
  before splitting into sentences — reuse it in later stages.
- OCR quirks: bullets come out as `e`; 27 pages stay under 70% confidence,
  mostly dense tables; handwriting and diagrams are noise.
- 70 PDFs are shared by 145 orders (double listings, or one combined order per noticee).
- SAT appeal annotations exist only for orders dated 2003–2022.
- Parties: 7% of orders have none found (mostly WTM orders whose noticee table is an
  image); a third of party roles are UNKNOWN. Roles and issuer guesses are a first
  pass to correct in validation.
- 19% of orders have no violated-regulation list (charges written as circulars, or
  phrasing the parser misses); `regimes_cited` and `penalty_provisions` still help.
- Fraud category: 959 orders are `unresolved` (coded OTHER, low confidence) and about 3,060
  rest on a fallback (low). Development agreement is 75 of 81, measured on the orders
  used to build the rules, so it is optimistic; the blind sample gives the real figure.
- Settlement amounts: where several applicants each pay in separate sentences, only the
  largest counts (an under-count).
- Detection channel: 58.7% of orders do not say what started SEBI's work (UNCLEAR; 83% of
  settlement orders; 66–70% in the eras before 2011, 46% since 2021). That share is a
  finding, not a gap to fill by guessing. Model-read channels (`detection_method = model`)
  are less reliable than rule-read ones: keep them separable in any analysis.
- Listed firms: companies listed only on regional exchanges are in neither BSE's nor NSE's
  lists and stay unmatched; rename history is NSE's only.
- Prices are price returns (splits and bonuses adjusted; dividends, rights and demergers not).
- Ownership (promoter holding, pledging) exists from December 2015 only, so the fraud years
  FY2010–2015 have none. Audit qualifications were dropped (they need annual-report PDFs).

## 6. Next

**Your remaining tasks, in order and with instructions, are in [TASKS.md](TASKS.md):** the two
Week 4 downloads, the match review and the sample freeze; drawing, coding and scoring the 150
validation orders; the decisions only you can make (the unit of analysis first); the code review
(guided by [CODE_GUIDE.md](CODE_GUIDE.md)); and the writing.

Three things to keep in mind throughout:

- The window is the period of business activity SEBI examined — "investigation /
  examination / inspection / relevant period" are one quantity; inspection
  *visit* dates are never it. See CLAUDE.md §1.
- H4 (surveillance is not the dominant first-detection channel) needs first detections per
  *matter*, not per order: the ISO wave alone moves the order-level surveillance share from
  26% to 42%.
- The unit of analysis (order / matter / noticee) decides what one Stage 1 observation is
  (TASKS.md D1).
