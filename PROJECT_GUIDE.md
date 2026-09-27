# Project guide — what we built in Week 1, where it lives, how to use it

*As of 2026-09-24.* Figures with provenance: [results_log.md](results_log.md).
Technical briefing for AI agents: [context.md](context.md). Runbook: [paper/timeline.md](paper/timeline.md).

---

## 1. The week in one page

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
| `tests/` | 318 tests, with real SEBI pages and order sentences as fixtures | — | code |
| `raw/cache/` | every HTTP response ever fetched (opaque hashed names) | 8.7 GB | **no** — needs re-scraping (days) |
| `raw/pdfs/<order_id>.pdf` | the order PDFs, hard-linked to `raw/cache` | ~0 extra | from the cache |
| `raw/pdfs/order_files_by_category/` | your 55 hand-downloaded orders | small | your files |
| `raw/ocr/` | OCR results per PDF page | 35 MB | yes, ~45 min of OCR |
| `derived/listings.parquet` | one row per listed order | 1.7 MB | yes, 10 s offline |
| `derived/order_documents.parquet` | where each order's document is, and whether it was fetched | 3.5 MB | yes, ~3 min offline |
| `derived/text/<order_id>.txt` | order text, pages separated by form feed `\f` | 0.73 GB | yes, ~5 min + OCR cache |
| `derived/text_documents.parquet` | one row per order: text source, OCR stats, status | 3.7 MB | with the text |
| `derived/text_pages.parquet` | one row per PDF page: how its text was obtained | 1.6 MB | with the text |
| `derived/window_candidates.parquet` | top 3 candidate sentences per order, with what regex v1 read from each | 5.5 MB | yes, ~1 min |
| `derived/windows.parquet` | one row per order: regex v1 violation window, confidence, source sentence, routing status | 1.4 MB | yes, ~1 min |
| `derived/fetch_failures.csv` | the orders whose document could not be fetched | tiny | with stage 2 |
| `reports/listing_*.csv` | per-page scrape log, duplicate conflicts (none), rows needing review | tiny | with stage 1 |
| `logs/` | run logs for each stage | small | — |
| `results_log.md` | every figure with its source and date | — | git |
| `hand/recon_notes.md` | SEBI protocol notes; §7 how orders state the violation period | — | git |

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

Full column list: [context.md §8](context.md#8-data-dictionary).

## 4. Re-running the pipeline

```bash
python -m pytest tests -q                                   # 318 passed
python -m src.scrape_listings --offline                     # stage 1 from cache, ~10 s
python -m src.fetch_pdfs --offline                          # stage 2 from cache, ~3 min
python -m src.extract_text                                  # stage 3, ~5 min with OCR cached
python -m src.extract_windows                               # stage 4, ~1 min
```

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

## 6. Next

- Week 2 Day 2: model fallback for the 2,732 orders regex v1 could not settle
  (`window_status` `UNCONFIRMED_RANGE` / `UNPARSED`), then Day 3 sanity rules.
- Read ~10 orders next to their PDFs before the Week 3 blind validation;
  `hand/recon_notes.md` §7 lists the phrasings and traps to look for.
- Settle the unit of analysis (order / matter / noticee), given the ISO cluster.
- Rest of Week 2: prior SCN/interim-order dates, penalties, regulations and
  parties into `derived/orders.parquet`.
