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
| OCR mean word confidence | **93.1** (median 94.2, p5 87.0) | tesseract 5.5.0, English, 300 dpi; 25 pages below 70, 8 below 50 (mostly dense tables) |
| Pages recovered by rotation/re-render retry | 203 (180 rotated) | sideways or upside-down scans, and 1-bit scans blurred by upsampling |
| Orders with broken text layers | 15 | 2013 AO orders from Ghostscript 7.06: text layer of control codes over a perfectly legible page |
| Where OCR need concentrates | 2000–01 (all 28 orders), 2013–15 (10–14%) | not the oldest years generally: 2003–10 range 0–1.1% |
| Orders with Hindi text | 9 | English OCR is sufficient |
| Text recovered | 716 M characters, 0.72 GB | median 23,414 characters per order |

The OCR rows reflect the retry and vector-text fixes, verified by re-extracting the 32 affected orders
(affected pages under 70% confidence: 207 → 25). `derived/` matches them once `python -m src.extract_text` is re-run.

### Corpus composition — affects the unit of analysis

| Figure | Value | Note |
|---|---|---|
| Largest single matter | **Illiquid Stock Options (ISO) at BSE: 2,243 orders** | 10.8% of the corpus, 18.6% of AO orders; 860 of them in 2023 |
| Orders per year, peak and after | 1,710 (2023) → 551 (2024) | mostly the ISO wave ending: excluding ISO, 850 → 490. Not evidence of a general enforcement decline |
| Other large clusters | investor-grievance non-redressal ~228, Titan Company 80, NSEL 52 | titles grouped by "in the matter of" |
| WTM titles naming the order type | 15.5% (998 of 6,424) | the rest say neither final, interim/ex-parte nor confirmatory: finality must be read from the text |

### Method notes for the data section

- SEBI's listing endpoint paginates on `doDirect` (0-based), not the `nextValue` a browser capture suggests; verified by exact count reconciliation.
- A character-count test for a text layer (the runbook's "< 100 characters") misses broken layers; pages are judged on text quality, image coverage and vector-path count.
- Older detail pages print the order inline and link annexures; taking the linked PDF would have archived annexures as orders (32 cases corrected).
- All HTTP goes through a request-keyed disk cache, so the corpus rebuilds from `raw/` without network access.
