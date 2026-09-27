# Results log

Every figure with its date, scope and the script that produced it (runbook rule 7).
Figures are from the verified outputs in `derived/` unless marked otherwise.

## 2026-09-23/24 — Week 1: corpus foundations

### Corpus: SEBI order listings (`src/scrape_listings.py`, scraped 2026-09-23)

| Figure | Value | Scope / note |
|---|---|---|
| Orders listed, four primary categories | **20,861** | Chairperson/Members (WTM) 6,424 · AO 12,003 · Settlement 1,994 · ED/CGM 440 |
| Excluded categories | SAT, Courts, Special Courts, Reg. 30A | appellate/judicial follow-ups, not SEBI's own orders |
| Date span | 2000-12-07 to 2026-09-23 | order dates as listed |
| Reconciliation with SEBI's published counts | exact for WTM and ED/CGM; Settlement +3, AO +1 | the 4 extra are orders dated after the counts were recorded (2026-09-21) |
| Listing pages walked / failed | 836 / 0 | 257 WTM, 80 Settlement, 481 AO, 18 ED/CGM |
| Duplicate or conflicting order IDs | 0 | across all categories |
| Orders annotated as appealed to SAT | **1,414 (6.8%)** | AO 885 (7.4%), WTM 529 (8.2%); none for Settlement or ED/CGM. Annotated orders are dated 2003–2022 only, so absence of an annotation is not evidence of no appeal for recent orders |
| Corrigenda linked | 105 | |

### Documents (`src/fetch_pdfs.py`, 2026-09-23/24)

| Figure | Value | Scope / note |
|---|---|---|
| Orders with a document | **20,849 of 20,861 (99.94%)** | 19,267 PDFs + 1,582 orders whose full text is on the detail page (no PDF) |
| Orders without a document | 12 | 4 dead PDF links and 1 dead detail page (all HTTP 404 on SEBI's side), 7 pages with no PDF (2 empty, a .doc, a .htm, a multi-page HTML order, a 722-char inline order, a SAT order page) |
| Unique PDF documents | 19,192 | 70 PDFs are shared by 145 orders: the same order listed twice, or one combined order listed per noticee |
| PDF volume | 8.84 GB (8,434 MiB) | all from www.sebi.gov.in over HTTPS |
| Network requests / elapsed | 40,017 / 27.5 h | sequential, 1.5 s delay between requests |

### Text and OCR (`src/extract_text.py`, 2026-09-24)

| Figure | Value | Scope / note |
|---|---|---|
| PDF pages | 318,116 | 19,267 PDF orders; median 12 pages per order (Settlement median 3, ED/CGM 29) |
| Pages with a usable native text layer | **97.55%** | 310,058 pages |
| Orders needing OCR | **708 (3.67% of PDF orders)** | runbook Day 5: under 10%, so OCR only these; no study-period restriction |
| Pages OCR'd | 7,782 (2.45%) | 7,534 scanned · 242 broken text layer · 6 text drawn as vector outlines |
| OCR mean word confidence | **93.1** (median 94.2, p5 87.0) | tesseract 5.5.0, English, 300 dpi; 27 pages below 70, 10 below 50 (mostly dense tables) |
| Pages recovered by rotation/re-render retry | 205 (182 rotated) | sideways or upside-down scans, and 1-bit scans blurred by upsampling |
| Orders with broken text layers | 15 | 2013 AO orders from Ghostscript 7.06: text layer of control codes over a perfectly legible page |
| Where OCR need concentrates | 2000–01 (all 28 orders), 2013–15 (10–14%) | not the oldest years generally: 2003–10 range 0–1.1% |
| Orders with Hindi text | 9 | English OCR is sufficient |
| Text recovered | 716 M characters, 0.72 GB | median 23,414 characters per order |

The OCR rows reflect the retry and vector-text fixes (full re-run finished 2026-09-24 19:15). The 32-order
verification had given 203/180 recovered/rotated and 25 pages under 70; the full run's `derived/text_pages.parquet`
gives the figures above (checked 2026-09-27).

### Corpus composition — affects the unit of analysis

| Figure | Value | Note |
|---|---|---|
| Largest single matter | **Illiquid Stock Options (ISO) at BSE: 2,243 orders** | 10.8% of the corpus, 18.6% of AO orders; 860 of them in 2023 |
| Orders per year, peak and after | 1,710 (2023) → 551 (2024) | mostly the ISO wave ending: excluding ISO, 850 → 490. Not evidence of a general enforcement decline |
| Other large clusters | investor-grievance non-redressal ~228, Titan Company 80, NSEL 52 | titles grouped by "in the matter of" |
| WTM titles naming the order type | 15.5% (998 of 6,424) | the rest say neither final, interim/ex-parte nor confirmatory: finality must be read from the text |

## 2026-09-27 — Week 2 Day 1: violation-window candidates, regex v1

`src/extract_windows.py` (`RULES_VERSION = regex-v1`), full corpus of 20,861 orders. Figures are from a
scratch run of the committed code; the output is deterministic (identical with 4 or 8 workers), so
`derived/windows.parquet` reproduces them once `python -m src.extract_windows` is run.
**These are not the extraction rate**: the Day 3 sanity rules and the Week 3 blind validation come first.

| Figure | Value | Scope / note |
|---|---|---|
| Orders with a regex v1 window | **10,521 of 20,849 with text (50.5%)** | high 8,845 · medium 1,676. AO 66.3% · ED/CGM 43.0% · WTM 31.3% · Settlement 18.6% |
| …of which the ISO window (1 Apr 2014 – 30 Sep 2015) | 2,418 | one matter; excluding ISO-sentence orders the regex share is 44.4% |
| Orders with a date range but no label strong enough (`UNCONFIRMED_RANGE`) | 2,394 (11.5%) | Day 2 model fallback |
| Orders with a labelled period regex cannot read (`UNPARSED`) | 338 (1.6%) | Day 2 model fallback |
| Orders with no candidate sentence (`NO_CANDIDATES`) | 7,596 (36.4%) | Settlement 71%, WTM 48%, AO 25%; only 113 mention an investigation/inspection period anywhere |
| Regex window share by order year | 2000–05 22% · 2006–10 54% · 2011–15 38% · 2016–20 41% · 2021–26 71% | |
| Window precision | day/day 9,258 · month/month 1,205 · mixed 58 | month-precision ends resolve to the month's last day |
| Windows ending after the order date | 2 | Day 3 sanity rule 1 will flag these |
| Candidate sentences kept | 28,140 | top 3 per order |
| Development spot check (not validation) | ~35 of 40 parsed non-ISO windows read correctly | errors were medium-confidence misreads and two label bugs, both fixed after the check |

## 2026-09-27 — Week 2 Day 2: model fallback (evaluation before the full run)

`src/window_model.py` (prompt `wm-v1`) with `qwen3.5:4b` (Ollama 0.34.4, Q4_K_M, 4.7B, RTX 4050 6 GB,
thinking off, temperature 0, seed 7). The model picks the sentence and kind and **quotes** the dates;
code checks the quotes are in the sentence, parses them day-first, and rejects known non-windows.
Labels are the agent's, made from the same sentences the model sees — development figures for choosing
the model and prompt, **not** the Week 3 blind validation.

| Figure | Value | Scope / note |
|---|---|---|
| Dev set (tuned on) | real windows 20/21 · no-window 22/24 · ambiguous 10/15 | 60 orders (40 `UNCONFIRMED_RANGE`, 20 `UNPARSED`); `hand/window_model_devset.csv` |
| Held-out set (not tuned on) | real windows 10/11 · no-window 9/10 · ambiguous 6/9 | 30 orders labelled before the model ran; `hand/window_model_holdout.csv` |
| Precision of accepted windows, unambiguous | **30 of 33 (91%)** | dev + held-out; no accepted window had wrong dates — errors are windows where the gold is none |
| Recall of real windows, unambiguous | 30 of 32 (94%) | |
| Agreement with regex-high windows | 75 of 80 (94%) | 70 non-ISO + 10 ISO; of the 5, regex was wrong once, the model 2–3 times, 1 a toss-up |
| Speed | 1.2 s/order; thinking mode 83 s/order (~4,300 tokens) | thinking dropped: 60× slower, no gain seen |
| Prompt v2 (adds a "what happened" field) | 19/21 · 20/25 on dev | no better than v1; not used |
| Dry run, 150 random sent orders | window 91 (61%): conduct dates 58, stated period 33 · none 49 · unverified 8 · rejected 2 | after the visit/statement fixes below; full run: `python -m src.window_model` |

## 2026-09-27 — Regex v1.1: the window is the activity examined, never the probe's own dates

Prompted by the user's check that "investigation / examination / inspection / relevant period" all mean
the period of business activity SEBI examined (the suspected violation window), not the time the probe
took. Audit of `derived/windows.parquet` (v1) and the model's dry run against that definition.

| Figure | Value | Scope / note |
|---|---|---|
| Regex v1 windows phrased "conducted … investigation/inspection during X to Y" | 102 | read one by one: investigations are the activity period (hereinafter label, months–years); the errors were all inspection *visits* |
| Inspection windows of ≤ 31 days in v1 | 26, of which ~23 visits | 2–20-day on-site visits, several labelled "inspection period" by SEBI itself |
| Orders whose window changes, v1 → v1.1 | **76 of 20,861** | 16 windows removed, all visits (→ model or no candidate) · 20 re-dated (the covered period instead of the visit, "from X till the date of inspection i.e. Y" read in full, financial-year lists) · 11 newly parsed · 29 changes among candidates only (25 visit-only candidates dropped). Every change reviewed |
| Regex v1.1 windows | 10,516 of 20,849 with text (50.4%) | high 8,845 · medium 1,671; the Day 1 figures above are v1 |
| Model-side rule that would have rejected "SEBI conducted an investigation during X to Y" | fixed before any full run | replaced by the shared visit test; bank *credits* over a period (fees collected without registration) now count as conduct dates |
| Model checks re-scored (cached answers) | unambiguous: dev 20/21 · 22/24, held-out 10/11 · 9/10, silver 75/80 | unchanged by the fixes |
| Known wrong after v1.1 | 40883 (a noticee's remark about a different NSE inspection, medium) | v1 had the visit; neither is right |

## 2026-09-27 — Week 2 Day 2: full model run and its analysis

Run by the user: `python -m src.extract_windows` (regex v1.1, 13:08) then `python -m src.window_model`
(13:09–14:06, 56.7 min, qwen3.5:4b, 2,513 model calls, 199 answers shared between orders with identical
sentences). Analysis afterwards found reader bugs; figures after the fix come from re-reading the same cached
answers (0 model calls) and match `derived/` once `python -m src.window_model --offline` is run.

| Figure | Value | Scope / note |
|---|---|---|
| Orders sent to the model | 2,712 | `UNCONFIRMED_RANGE` 2,362 + `UNPARSED` 350 |
| Model outcome, as run | window 1,641 (stated period 652 · conduct dates 989) · none 907 · unverified 131 · rejected 33 | 14:06 run |
| Model outcome, after the reader fixes | window 1,634 (stated 647 · conduct 987) · none 907 · unverified 135 · rejected 36 | 70 windows changed: 63 re-dated (month spans "May-June 1998", day spans "January 21-25, 2008", year spans "2003-05", quoted ranges), 3 more inspection visits caught, 4 malformed quotes no longer widened to a whole year |
| **Orders with a window, regex + model** | **12,150 of 20,849 with text (58.3%)** | before the Day 3 sanity rules; 12,157 as run |
| …by order type | AO 71.7% · ED/CGM 61.1% · WTM 42.6% · Settlement 27.4% | settlement kept in the main sample (user decision) |
| …by order year | 2000–05 31.9% · 2006–10 62.7% · 2011–15 45.5% · 2016–20 48.8% · 2021–26 78.0% | as run |
| Model window precision, 50 random windows (25 per kind) | 42–45 of 50 (84–90%) | agent-judged from the source sentence, after the fixes; errors are the model's choice of sentence (tables, repayment or legal-threshold periods). The Week 3 blind sample will measure it properly |
| Distinct windows among the 12,150 | 3,394 | 188 windows are shared by ≥ 10 orders, covering 5,789 orders |
| Illiquid Stock Options orders with a window | 2,363 (19.4% of windows), one window 1 Apr 2014 – 30 Sep 2015 | order dates 2018–2026; order date − window end: median 7.5 years (IQR 6.1–7.9) vs 5.0 for other orders |
| Titles naming a matter ("in the matter of …") | 10,399 of the windowed orders; 4,903 distinct matter strings; 1,122 with > 1 order holding 6,618 orders | crude string grouping, for scale only |

## 2026-09-27 — Week 2 Day 3: sanity rules and the extraction rate

`src/check_windows.py` on the full chain (regex v1.1 with the fixes below → model answers re-read from
`raw/llm/`, 0 calls → sanity rules), scratch run; deterministic, so `derived/` reproduces it once the
user runs the chain. Before hand review of the flagged windows.

| Figure | Value | Scope / note |
|---|---|---|
| Windows checked | 12,150 | regex 10,516 + model 1,634 |
| Flagged | **43** | end after order 2 · lag ≤ 0 1 · lag ≥ 20 years 14 · start before 1995 27 (one window has two flags). Impossible 3, unusual 40 |
| First reading of the flags | keep 36 · drop 7 | drops: a statutory Special Court Act period, two share lock-in periods, a no-trading period, a complaints table, a registration validity period, a Union Budget year. Keeps: 1994–95 manipulation cases and 1998–2002 trading re-adjudicated in 2021–23 |
| Parser bug exposed | "period i.e." matched as "Period-I", merging a window with a later range | 1 order (16938: Aug–Dec 1994 had become Aug 1994–Mar 1995). Fixed |
| Label fix exposed | regex financial-year windows carried `month` precision | 130 orders; now `fy`, which the rules treat strictly |
| Day/month inversion | none found | 25 windows from documents that also hold month-first dates (trade logs); in 23 a window date has a day > 12 and reads one way only; 2 are day-first by context (40889 a single trade date, 20449 "04.04.2005 to 12.05.2005") |
| **Extraction rate** (high/medium window, after the rules, before review) | **58.1%** (12,107 of 20,849 with text) | runbook band 50–70%: proceed; report the drop-out and test it |
| …excluding Illiquid Stock Options orders | 53.1% (9,876 of 18,606) | |
| …by order type | AO 71.6% · ED/CGM 60.7% · WTM 42.1% · Settlement 27.3% | |
| …by order year | 2000–05 30.7% · 2006–10 62.6% · 2011–15 45.4% · 2016–20 48.6% · 2021–26 77.7% | |
| Drop-out is systematic | by type Cramér's V 0.34, by year 0.30 (χ² p ≈ 0) | moderate associations: orders without a window are mostly Settlement and WTM, and older. Report in the data section; consider weighting or controls in Stage 1 |
| Windows by method | regex 10,486 · model 1,621 (after flags) | model share 13.4% |

**Day 3 final, after the user's review of the 43 flags** (`hand/window_review.csv`: 33 keep, 8 drop, 2 fix with
hand dates): **12,142 windows, 58.2%** of orders with text (regex 10,508 · model 1,632 · hand 2). By type AO 71.7% ·
ED/CGM 61.1% · WTM 42.5% · Settlement 27.4%; excluding Illiquid Stock Options orders 53.3%; drop-out vs type
Cramér's V 0.334, vs year 0.298.

## 2026-09-27 — Week 2 Day 4: prior actions and sanctions

`src/extract_actions.py` (actions-v1) with `src/amounts.py`, full corpus, scratch run (deterministic; `derived/`
reproduces it once the user runs `python -m src.extract_actions`). Agent-checked samples, not validation.

| Figure | Value | Scope / note |
|---|---|---|
| Orders with a prior action recited (plausible) | **16,918 of 20,849 (81%)** | earliest kind: SCN 13,550 · interim / ex-parte 3,347 · confirmatory 21. AO 90% · ED/CGM 94% · WTM 68% · Settlement 68% |
| …among orders with a window | 10,821 of 12,142 (89.1%) | |
| Mentions found | 51,925 | implausible (after the order, before the SEBI Act, or before the window starts — mostly another matter's date) are kept but not used |
| AO-appointment dates (robustness only) | 10,245 orders | SEBI's internal decision to adjudicate |
| **Lag split, windowed orders (medians)** | window end → order **5.50 y** (IQR 3.55–7.61) · → first action **3.15 y** (1.48–5.35) · → first SCN or order 3.86 y (2.42–5.98) · first action → order **1.19 y** (0.42–3.01) | the plan's detection vs resolution lag. First action is the matter's (e.g. the ISO interim order of 20 Aug 2015 against 59 entities); first SCN is the entity's |
| First action on or before the window end | 1,250 (10.3%) | almost all ISO orders citing the Aug 2015 interim order, 41 days before the window closed; Week 5 flags non-positive lags |
| Orders with a penalty imposed | **8,039** | AO 7,912 (66%) · ED/CGM 80 · WTM 46 · Settlement 1 |
| Penalty per order | median ₹5 lakh; p5 ₹50,000; p25 ₹2 lakh; p75 ₹6 lakh; p95 ₹45 lakh; max ₹7,269 crore (30700, PACL, 2015) | distinct amounts imposed; restatements counted once; Total rows not added |
| Figure vs words | 9,704 amounts with words; 32 disagree (0.3%) | typos in the orders ("5,00,000/- (Rupees Eight Lakh only)"): figure kept, flagged. Malformed figures ("2,00,00/-") take their words |
| "each" / "jointly and severally" | 560 orders | "each" with no Total left for Day 5 (`rows_each_unresolved`) |
| AO outcome | penalty 7,912 · no penalty stated 2,481 · unknown 1,604 | |
| Settlement amounts | 1,717 of 1,994 settlement orders (86%) | |
| Restraint (debarment) | 3,111 orders (WTM 2,511) | duration in months where stated |
| Registration action / disgorgement / warning | suspension 462, cancellation 223 / 219 / 348 | |
| Development checks | 20/20 random penalty totals and 20/20 random first prior actions read correctly (after fixes) | fixes came from checking the largest penalties, every figure/words disagreement and the rejected mentions |

### Method notes for the data section

- SEBI's listing endpoint paginates on `doDirect` (0-based), not the `nextValue` a browser capture suggests; verified by exact count reconciliation.
- A character-count test for a text layer (the runbook's "< 100 characters") misses broken layers; pages are judged on text quality, image coverage and vector-path count.
- Older detail pages print the order inline and link annexures; taking the linked PDF would have archived annexures as orders (32 cases corrected).
- All HTTP goes through a request-keyed disk cache, so the corpus rebuilds from `raw/` without network access.
