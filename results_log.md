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

`src/extract_actions.py` (actions-v1) with `src/amounts.py`, full corpus, scratch run (deterministic; run into
`derived/` by the user on 2026-09-27). Agent-checked samples, not validation.

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

## 2026-09-28 — Week 2 Day 5: regulations, parties, and the Week 2 checkpoint table

`src/regulations.py` + `src/extract_parties.py` (parties-v1; 0.7 min) and `src/build_orders.py` (1 s), full corpus,
scratch run (deterministic; `derived/` reproduces it once the user runs `python -m src.extract_parties` and
`python -m src.build_orders`). Agent-checked samples, not validation.

| Figure | Value | Scope / note |
|---|---|---|
| Orders with a party list | **19,451 of 20,849 (93.3%)** | AO 99.4% · Settlement 98.3% · ED/CGM 96.8% · WTM 80.0%. WTM misses: noticee tables that are images or in annexures |
| Where the list came from | header 11,616 · numbered table 2,832 · PAN-separated 2,301 · comma list 1,251 · "hereinafter … Noticee" 648 · title 598 · other 205 | |
| Party rows (`order_entities`) | **47,972**; median 1 per order; max 226 | PAN on 32,298 (67.3%) |
| Entity type | individual 28,990 · company 16,927 · HUF 1,341 · firm 592 · trust 111 | PAN 4th letter first, else the name |
| Role (first guess) | UNKNOWN 16,107 (33.6%) · ISSUER 10,236 · TRADER 10,015 · DIRECTOR 5,720 · INTERMEDIARY 3,560 · PROMOTER 2,220 · AUDITOR 114 | evidence kept in `role_basis`; "ISSUER by suffix" is weak |
| Penalty orders with every penalty linked to a party | **7,819 of 8,039 (97.3%)** | one party 6,798 · all rows linked 1,021 · partial 58 · none 162 |
| Per-party amounts sum to the order total | **7,320 of 7,320** checkable orders | fully linked, no "each"/"jointly" |
| "₹X each" totals resolved | 71 of 108 | counted once per party named |
| Parties penalised | 10,958 party rows | sanction types on parties: penalty 10,958 · debarment 9,947 · settlement 2,174 · warning 628 · disgorgement 547 · suspension 442 · cancellation 202 |
| Orders with a violated-provision list (`regulations`) | **16,865 (80.9%)** | AO 86.6% · ED/CGM 86.8% · Settlement 77.1% · WTM 71.0% |
| Instruments violated (orders) | PFUTP 8,451 · SEBI Act 4,532 · SAST 2,610 · PIT 2,223 · Stock Brokers 1,904 · Companies Act 1,185 · SCRA 735 · Listing Agreement 612 · LODR 586 · Investment Advisers 554 | input to the Week 3 fraud-category map |
| Citations | 861,082 resolved (violation 358k · power 233k · cited 152k · penalty 118k); 215,445 provisions unresolved | unresolved: no instrument follows ("Section 67(1)", "Clause 4 of Schedule B") |
| Issuer guess | 13,225 orders (63.4%) | from "In the matter of <company>" 12,840; from a party 385 |
| `derived/orders.parquet` | **20,861 rows × 62 columns** | joins on `text_stem`, one-to-one, row count asserted at each join |
| Development checks | party lists right in 15 of 18 random orders with parties (2 more had none); violated lists right in 13 of 20, the rest empty | fixes came from 50 reviewed orders, every unlinked penalty sampled, and the sum check above; the Section 28A recovery boilerplate had entered 2,717 lists before it was classed as a power |

## 2026-09-30 — Audit fixes (stages 6–8 regenerated) and Week 3 Day 1: fraud category

The fixes follow `audit/AUDIT_REPORT.md` (C-1, C-2, H-1, H-2, M-3, M-9) plus two found afterwards: a single party
now takes a stated total, and descriptive prefixes are dropped from `issuer_guess`. All were local to stages 6–8;
no later stage needed rewriting. Full-corpus scratch run (deterministic); `derived/` reproduces it once the user runs
the five commands in CLAUDE.md §2. Before = `derived/` built 2026-09-27/28.

| Figure | Before → after | Scope / note |
|---|---|---|
| Orders with a penalty | **8,039 → 8,473** | 434 recovered (₹619.7 crore): tables split from their "hereby impose" clause (C-1). 71 corrected (70 up, net +₹59.3 crore): truncated figures (H-1), stated totals (H-2). 0 lost. AO 8,305 · ED/CGM 99 · WTM 68 · Settlement 1 |
| Penalty sum | ₹11,806 crore → **₹12,485 crore** | median ₹5 lakh (unchanged); p5 ₹50,000 · p25 ₹2 lakh · p75 ₹7 lakh · p95 ₹60.8 lakh; max unchanged (30700, PACL, ₹7,269 crore) |
| AO outcome | penalty 7,912 → 8,305 · no penalty 2,481 → 2,387 · unknown 1,604 → 1,305 | |
| Penalties under ₹1,000 | 22 → 2 | the truncation signature (H-1) |
| Stated totals used | 23 → 27 orders | only when "each" appears, or the total equals every non-total mention |
| Figure vs words | 32 of 9,704 → **53 of 12,053** (0.4%) | the new disagreements are in the penalty tables now read: a row figure printed beside the words of a noticee's subtotal, typos ("Rs. 2,50,0000 (Two Lakhs Fifty Thousand)"). Figure kept, flagged |
| Settlement amounts | 1,717 orders (unchanged); median ₹10 lakh → **₹5.93 lakh**; sum ₹1,913 crore → **₹1,460 crore** | 1,058 orders changed, 1,044 of them down: an amount restated in several sentences had been added each time (C-2). Now the largest sentence value counts. Limitation: where several applicants each pay in separate sentences, this under-counts |
| Disgorgement | 219 orders; sum ₹4,441 crore → ₹5,552 crore | 29 changed; Satyam now ₹1,258.88 crore (was read as ₹125) |
| Windows | 12,142 (58.2%), unchanged | dropped/flagged rows no longer keep method, kind or raw text (M-9: 8 → 0 rows) |
| Prior actions | 16,918, unchanged | |
| `matter_raw` with SAT-appeal text | 1,209 → 0 | M-3 |
| Orders with a party list | 19,451 → **19,460** | 9 "Inspection of <broker>" AO orders gained their one party |
| Party rows | 47,972 → 47,981 | PAN 32,298. Parties penalised 10,958 → 13,242 rows |
| Penalty links | one party 6,798 → 6,922 · all rows 1,021 → 1,301 · partial 58 → 87 · none 162 → 163 | fully linked 8,223 of 8,473 penalty orders (97.0%) |
| Per-party amounts sum to the order total | **7,681 of 7,681** checkable orders | fully linked, no "each", no joint liability. The 10 other fully linked orders all have joint liability, which sits on every party liable by design |
| Issuer guess | 13,225 → **13,314 (63.9%)** | 46 guesses cleaned ("Inspection of Books of X", "Issuance of … Shares by X", "Financial Statements of X" → X); 9 new from the inspection orders above |
| `derived/orders.parquet` | 20,861 × 62 → **20,861 × 66** | four fraud-category columns |

**Week 3 Day 1: fraud category.** Stage `src/classify_fraud.py` (fraud-v1; 1.5 min). The rules come from
`hand/coding_protocol.md`, which was written before coding; its worked examples are in `hand/protocol_examples.csv`.
Violated provisions (`order_parties.regulations`) decide first. PFUTP 3/4 and SEBI Act §12A(a)–(c) are decided by
the narrative (capped keyword scores). Where no violated list exists, the fallbacks are, in order: the penalty
section, the title, a strong narrative, then the instruments cited. Definitions (Reg./Sec. 2) are not charges.

| Figure | Value | Scope / note |
|---|---|---|
| Orders coded | 20,849 (every order with text) | |
| **Category** | MARKET_MANIPULATION 7,408 (35.5%) · DISCLOSURE_FAILURE 4,398 (21.1%) · OTHER 4,229 (20.3%) · INTERMEDIARY_MISCONDUCT 3,353 (16.1%) · INSIDER_TRADING 632 (3.0%) · FUND_DIVERSION 511 (2.5%) · ACCOUNTING_MISSTATEMENT 244 (1.2%) · RPT_TUNNELLING 74 (0.4%) | MM includes 2,223 of the 2,243 Illiquid Stock Options orders (18 are intermediary, 2 other) |
| …among windowed orders (12,142) | MM 6,684 · DISCLOSURE 1,933 · INTERMEDIARY 1,573 · OTHER 852 · INSIDER 567 · FUND 321 · ACCOUNTING 167 · RPT 45 | the Stage 1 base: the fraud categories of the plan (diversion, RPT, accounting) are small |
| …by order type | AO: MM 5,239 · DISCL 2,835 · OTHER 2,109 · INTERM 1,127. WTM: OTHER 1,857 · MM 1,760 · INTERM 1,575 · DISCL 620. Settlement: DISCL 917 · MM 387 · INTERM 353 · OTHER 234. ED/CGM: INTERM 298 | |
| Basis | provisions 16,828 (high 12,630 · medium 3,521 · low 677) · fallback 3,076 (low) · unresolved 945 (OTHER, low) | medium = several categories charged; precedence decided (3,110 orders charge more than one) |
| Top rules | PFUTP narrative 6,220 · SAST 2,011 · §12A narrative 1,182 · PIT disclosure 1,126 · Stock Brokers 1,011 · unresolved 945 · 15C investor grievances 811 · unregistered 635 · 11C non-cooperation 606 · deemed public issue 523 | |
| Development agreement | **58 of 65** agent-labelled orders (89%); 3 UNSURE excluded | `hand/fraud_category_devset.csv`. The rules were tuned on these orders, so this is optimistic. The blind sample (Day 5) measures accuracy and κ. The devset and protocol examples are excluded from that sample |
| Definitions fix | 6 categories changed, 106 moved from medium to high confidence, dev agreement unchanged | PIT 2(1)(g) had counted as a disclosure charge beside PIT 4(1); 31627 (definitions only) had been DISCLOSURE |
| Known limits | NSE co-location cases fall in MM as the nearest category; ISO orders against brokers charged only under the broker regulations are INTERMEDIARY; 945 unresolved orders (no violated list, penalty section, telling title or narrative) | |

## 2026-09-30 — Week 3 Day 1 check: fraud-v2 and the §15J boilerplate

This check ran on the regenerated `derived/`, which matched the scratch run row for row. Every order with text
has a category, and no order charged under PIT 3/4 is coded as anything but insider trading. Two problems
turned up:

1. **The §15J Explanation is boilerplate, not a penalty.** Since 2019 AO orders quote it: "the power of an
   adjudicating officer to adjudge the quantum of penalty under sections 15A to 15E, … 15F, 15G, 15H and 15HA …".
   It had put all those sections into `penalty_provisions`. Orders "penalised under §15G" fall from 2,719 to
   **489** (286 of them insider trading). The fix is in `regulations.py`, where the cited sections become the
   officer's power. It changes only `penalty_provisions` (2,302 orders); the violated lists and the categories
   are unaffected.
2. **Fraud category: the code departed from the protocol in three places.** A fresh stratified spot check of 16
   orders (2 per category, excluding the dev set) found **11 of 16 right**, with 3 clear errors and 2 debatable.
   - **Brokers:** a broker's misappropriation of clients' securities read as diversion or accounting (§3.2
     exclusion).
   - **Offer documents:** "objects of the issue" alone read as diversion.
   - **Two narratives:** the higher score won where the protocol says precedence decides.

   The user decided the open definitional question: a company's own funds siphoned to insiders are
   **RPT_TUNNELLING**, and FUND_DIVERSION is issue money only. This is recorded in the protocol change log.

| Figure | fraud-v1 → fraud-v2 | Scope / note |
|---|---|---|
| Orders changing category | **360** | largest flows: FUND → RPT 42 · FUND → DISCL 43 (offer-document lapses) · INTERM → OTHER 43 (CIS) · FUND → MM 40 · INSIDER → MM 38 (the regulations named by title) · ACC → RPT 25 · INSIDER → DISCL 19 (Model Code breaches) |
| Category | MM 7,408 → 7,486 · DISCLOSURE 4,398 → 4,458 · OTHER 4,229 → 4,291 · INTERMEDIARY 3,353 → 3,322 · INSIDER 632 → **572** · FUND 511 → **342** · ACCOUNTING 244 → 232 · RPT 74 → **146** | FUND is issue money only; RPT now includes tunnelling |
| …among windowed orders | MM 6,741 · DISCL 1,964 · INTERM 1,571 · OTHER 859 · INSIDER 514 · FUND 244 · ACC 154 · RPT 95 | |
| Confidence | high 12,679 · medium 3,481 · low 4,689 (fallback 3,062; unresolved 959) | |
| Dev agreement | **75 of 81** (93%) | the dev set now includes the 16 spot-check orders it was tuned on (84 orders; 3 UNSURE). Five labels changed: four under the decision, and 43388 is Model Code, so DISCLOSURE by §3.1 |
| Second fresh spot check (no tuning) | **16 of 16** | 2 per category, agent-judged from the charges and the quoted evidence, not a full reading; ISO orders excluded from the draw |

## 2026-09-30 — Week 3 Day 2: detection channel (rules; model pass pending)

`hand/coding_protocol.md` §5 was written before coding. The user decided three definitions: INSPECTION became an
eighth channel; a trading pattern or filing that SEBI or an exchange *observed* is SURVEILLANCE, flagged; an exchange
reference with no stated basis is UNCLEAR. `src/detect_channel.py` (channel-v1) looks for a source tied to the start
of SEBI's work, then applies the earliest-trigger rule. Orders it leaves undecided that carry a channel cue go to the
local model (dc-v2, cue-verified quotes). Figures below are **rules only**: a full-corpus scratch run, 1.3 min. The
model pass (~5,300 orders) is for the user to run.

| Figure | Value | Scope / note |
|---|---|---|
| Rules decide | 8,208 orders (39.4% of 20,849 with text) | UNCLEAR by rule (exchange reference, suo motu) included; 5,325 more carry a cue and go to the model; 7,316 have no candidate sentence |
| **Channel** (rules only) | UNCLEAR 13,129 (63.0%) · SURVEILLANCE 3,343 · COMPLAINT 2,553 · INSPECTION 1,239 · REFERRAL 473 · MEDIA 77 · SELF_DISCLOSURE 25 · AUDIT 10 | SURVEILLANCE detail: trading_pattern_observed 2,596 (ISO 1,532) · alert 415 · filings_observed 332 |
| Share among orders with a channel | SURVEILLANCE 43.3% · COMPLAINT 33.1% · INSPECTION 16.0% · REFERRAL 6.1% · MEDIA 1.0% · SELF_DISCLOSURE 0.3% · AUDIT 0.1% | order level, not matter level (H4 needs the matter-level count; unit of analysis still open) |
| …excluding ISO | COMPLAINT 42.7% · SURVEILLANCE 26.7% · INSPECTION 20.7% · REFERRAL 7.9% · MEDIA 1.3% | |
| …windowed listed-company frauds (not INTERMEDIARY/OTHER), excl. ISO (1,763) | SURVEILLANCE 57.2% · COMPLAINT 24.0% · REFERRAL 9.4% · INSPECTION 6.4% · MEDIA 2.3% · SELF_DISCLOSURE 0.4% · AUDIT 0.3% | preliminary; the decided orders are a selected subset (UNCLEAR 60.9% of windowed orders) |
| **UNCLEAR share** (reportable: how much the record discloses) | 63.0% overall · Settlement 86.3% · WTM 63.0% · AO 59.5% · ED/CGM 51.1%; by era 2000–05 72.9% · 2006–10 73.6% · 2011–15 67.5% · 2016–20 64.0% · 2021–26 49.2% | falls with time; by fraud category from 56.8% (OTHER) to 86.0% (FUND_DIVERSION) |
| Dev agreement (rules, UNCLEAR default) | **54 of 58** (93%); rules decided 27, all right | `hand/detection_channel_devset.csv`: 58 random orders stratified by type (2 ISO), agent-labelled, tuned on. The 4 misses are 3 judgement calls (`?`) and 1 exchange snap investigation |
| Protocol examples | 9 of 10 by rules | 76787 (AUDIT) is stated in the SCN summary, outside the trigger window |
| Model pass (dev and probes) | unconstrained: dev fell 55 → 50 (7 invented channels); cue-verified: no false channels on dev; on 80 undecided real orders it assigned 24 channels, about three quarters right | kept as a medium-confidence fallback, `detection_method = model` |
| Orders seen while building (excluded from the blind sample) | 458 | dev sets, protocol examples, `hand/seen_while_building.csv` (206 read in surveys, samples and probes) |
| `penalty_provisions` §15J fix | orders citing §15G as a penalty 2,719 → 489 | see the Day 1 check above |
| `derived/orders.parquet` (scratch) | 20,861 × 71 | five channel columns added |

### Method notes for the data section

- SEBI's listing endpoint paginates on `doDirect` (0-based), not the `nextValue` a browser capture suggests; verified by exact count reconciliation.
- A character-count test for a text layer (the runbook's "< 100 characters") misses broken layers; pages are judged on text quality, image coverage and vector-path count.
- Older detail pages print the order inline and link annexures; taking the linked PDF would have archived annexures as orders (32 cases corrected).
- All HTTP goes through a request-keyed disk cache, so the corpus rebuilds from `raw/` without network access.

## 2026-09-30 — Week 3 Day 2: detection channel, final (model pass; logged 2026-10-14)

The user ran stages 8–11 into `derived/` on 2026-09-30 (`extract_parties` 16:56 → `classify_fraud` 16:58 →
`detect_channel` 16:58–18:08 → `build_orders` 18:08). The model pass (dc-v2, `qwen3.5:4b`) read the 5,325 orders the
rules left undecided with a cue: 4,256 model calls, 69.7 min, 0 errors. Figures from `logs/detect_channel.log` and
`derived/orders.parquet` (20,861 × 71).

| Figure | Value | Scope / note |
|---|---|---|
| **Channel** (rules + model) | UNCLEAR 12,232 (**58.7%**) · SURVEILLANCE 3,609 · COMPLAINT 2,790 · INSPECTION 1,317 · REFERRAL 603 · MEDIA 194 · SELF_DISCLOSURE 73 · AUDIT 31 | 20,849 orders with text; rules only had UNCLEAR at 63.0% |
| Method | rule 8,208 · model 4,499 (897 channels, 3,602 UNCLEAR) · none 8,142 (7,316 with no candidate sentence, 826 model answers unverified → UNCLEAR, low) | model channels: SURVEILLANCE 266 · COMPLAINT 237 · REFERRAL 130 · MEDIA 117 · INSPECTION 78 · SELF_DISCLOSURE 48 · AUDIT 21, all medium confidence |
| Share among orders with a channel (8,617) | SURVEILLANCE 41.9% · COMPLAINT 32.4% · INSPECTION 15.3% · REFERRAL 7.0% · MEDIA 2.3% · SELF_DISCLOSURE 0.8% · AUDIT 0.4% | order level, not matter level |
| …excluding ISO (6,726) | COMPLAINT 41.5% · SURVEILLANCE 25.6% · INSPECTION 19.6% · REFERRAL 8.9% · MEDIA 2.9% · SELF_DISCLOSURE 1.1% · AUDIT 0.5% | |
| …windowed listed-company frauds (not INTERMEDIARY/OTHER), excl. ISO: 2,002 with a channel of 7,367 | SURVEILLANCE 54.9% · COMPLAINT 23.4% · REFERRAL 9.2% · INSPECTION 5.9% · MEDIA 5.0% · AUDIT 0.8% · SELF_DISCLOSURE 0.6% | preliminary for H4; the decided orders are a selected subset (72.8% of this group is UNCLEAR) |
| **UNCLEAR share** | 58.7% overall (56.9% of windowed orders) · Settlement 83.0% · AO 56.5% · WTM 55.9% · ED/CGM 46.8%; by era 2000–05 65.8% · 2006–10 69.6% · 2011–15 63.0% · 2016–20 58.7% · 2021–26 46.3% | by fraud category from 45.8% (INTERMEDIARY) to 82.5% (FUND_DIVERSION) |
| Accuracy | not measured yet | the Week 3 blind sample measures it (Day 5) |

## 2026-10-14 — Week 3 Days 3–4: the blind validation sample (built; the user draws and codes it)

`src/validation_sample.py`, built in Week 2, was brought up to date before the draw. Figures are from a scratch dry
run (`--scratch`, seed 20261006, 1.6 s); the real draw uses the same seed and inputs, so it gives the same 150.

| Figure | Value | Scope / note |
|---|---|---|
| Design | 150 = 60 regex windows · 25 model stated · 25 model conduct · 40 no window; ISO capped at 3 / 1 / 1 / 1; systematic within stratum over type × date | unchanged from Week 2 (runbook: "stratified by year and order category") |
| Orders excluded as seen | **539** (was 458) | +43 flagged windows the user reviewed with the machine's window shown (`hand/window_review.csv`); +39 orders quoted with their pipeline output in `audit/`, `results_log.md` or `CLAUDE.md`. Week 2's agent-read samples (40 windows, 150 dry-run, 50 model windows, 20 penalty and 20 prior-action checks, 18 party lists) were not recorded and cannot be excluded; at random they would put about 3 orders in the sample |
| Weights | per (stratum, ISO) cell; sum 20,849 | **fixed:** ISO is 2,361 of the 10,510 regex windows (22.5%) but 3 of 60 sampled. The Week 2 key weighted the whole stratum alike, which would have counted ISO at 5% of regex accuracy. Now ISO 787 per order, other regex 143, model conduct 39, model stated 27, no window 223 |
| Sample by type | AO 77 · WTM 54 · Settlement 15 · ED/CGM 4 | weighted 58.2 / 30.3 / 9.8 / 1.7% vs corpus 57.5 / 30.8 / 9.6 / 2.1% |
| Sample by order year | 2000–04: 9 · 2005–09: 24 · 2010–14: 38 · 2015–19: 32 · 2020–24: 39 · 2025–26: 8 | weighted shares within 5 points of the corpus (largest gap: 2020–24, 24.2% vs 29.1%) |
| ISO orders | 5 | |
| Local copies | 136 PDFs + 14 inline-order texts, 0 missing, 80 MB | `hand/validation_pdfs/`, gitignored. Copies because `raw/pdfs` files are hard links into `raw/cache` |
| Rare classes (expected at corpus shares) | RPT ≈ 1 · ACCOUNTING ≈ 2 · FUND ≈ 2 · INSIDER ≈ 4; AUDIT, SELF_DISCLOSURE ≈ 0–1 · MEDIA ≈ 1–2 | κ and agreement are measurable overall; per-category accuracy only for MM, DISCLOSURE, INTERMEDIARY, OTHER and UNCLEAR, SURVEILLANCE, COMPLAINT. A rare-class check would need a separate draw |
| Coded fields | `violation_start`, `violation_end`, `window_kind`, `fraud_category`, `detection_channel`, `detection_detail` (new), `primary_issuer`, `notes` | protocol §2–§5; `--check` validates values without showing pipeline output |
