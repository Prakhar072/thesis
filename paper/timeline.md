# Thesis Execution Runbook

2026-09-20 · @Someone · due 2026-12-01

Start at Day 0 tonight and work down. Nothing here needs anything from ICAI or SEBI — every step runs on public data, so you are not blocked on anyone.

The single most common way this project fails is writing a parser before reading twenty orders. Do Week 1 Day 1 properly and the rest gets easier.

## Eight rules

Read these once. They are not general advice — each one prevents a specific failure that would cost you days you do not have.

**1. Cache every network call, permanently.** Every fetch writes its response to disk keyed by a hash of the URL. Re-runs read from disk and never touch the network. You will re-run the pipeline thirty times; if each run re-downloads, you lose the project to bandwidth and rate limits. Write the cache layer *first*, before the scraper.

**2. Store the source sentence with every extracted field.** When you pull a violation window, store the actual sentence it came from in the same row. Every field gets a `*_raw` twin. Without this you cannot validate extraction, and validation accuracy is something you report in the paper. It costs one extra column and saves the credibility of the whole dataset.

**3. `raw/` is append-only.** Downloaded PDFs and HTML go in `raw/` and are never modified or deleted. All processing reads from `raw/` and writes to `derived/`. If you corrupt a derived file, you rebuild it. If you corrupt raw data, you re-scrape — which costs days.

**4. Scrape politely or lose access.** Sequential requests, 1–2 second delay, a real User-Agent with your email, retry with backoff on failure. Do not parallelise the SEBI scrape. Getting blocked from sebi.gov.in in week 1 ends the thesis, and there is no appeal process.

**5. Scripts, not notebooks.** Each step is a script that reads defined inputs and writes defined outputs, re-runnable from scratch. Use notebooks to look at things, never to produce things. Notebook state is invisible and by week 8 you will not remember what was run in what order.

**6. Commit daily, even when broken.** `git commit -am "wip"` at the end of every session. You will need to go back to something that worked, and you will not remember when it worked.

**7. Log every number the day you produce it.** Keep `results_log.md`. Every estimate gets a line: the number, the date, the script that made it, the sample size. When you write up in week 9 you will otherwise spend two days re-deriving where a figure came from.

**8. Respect the timebox.** Each step below has an allotted time. If you exceed it by half, stop and take the stated fallback. The plan has no slack, so time lost to one step comes directly out of another. Pushing on past a timebox is the single most likely way this misses 1 December.

## Day 0 — tonight (1 hour)

### Repo and folders

```bash
mkdir -p thesis/{raw/{listings,pdfs,cache},derived/text,hand,src,analysis,paper,figures}
cd thesis && git init
printf 'raw/\nderived/\n__pycache__/\n.venv/\n*.pyc\n' > .gitignore
```

`raw/` and `derived/` stay out of git — they will reach several GB. Code, hand-coded files and the paper are versioned; data is reproducible from code.

### Environment

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install requests httpx beautifulsoup4 lxml tenacity \
            pymupdf pdfplumber pypdf \
            pandas pyarrow duckdb rapidfuzz \
            statsmodels lifelines scikit-learn lightgbm \
            matplotlib tqdm python-dateutil
pip freeze > requirements.txt
```

Notes on three of these that are not obvious choices:

- **pymupdf** is several times faster than pdfplumber for plain text extraction, which matters across thousands of PDFs. Keep pdfplumber for the awkward ones.
- **duckdb** lets you query parquet files with SQL directly, with no database server. At this data size it is the right analysis layer and will save you a lot of pandas.
- **rapidfuzz** is what you will use for entity-name matching in week 4. It is far faster than the older fuzzywuzzy.

### R, for two specific things

Python cannot do competing risks or right-truncation adjustment well. You need R for those two steps only, in week 6.

```bash
# install R, then:
R -e 'install.packages(c("survival","cmprsk","SurvTrunc"), repos="https://cloud.r-project.org")'
```

If `SurvTrunc` is unavailable, you can implement reverse-time Kaplan–Meier yourself in about forty lines — but check availability now, not in week 6.

### Write the cache layer first

Before any scraping code. `src/cache.py` with one function:

```python
import hashlib, pathlib, httpx, time
CACHE = pathlib.Path("raw/cache")

def get(url: str, force: bool = False) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = CACHE / hashlib.sha256(url.encode()).hexdigest()
    if key.exists() and not force:
        return key.read_bytes()
    time.sleep(1.5)                       # rule 4
    r = httpx.get(url, timeout=60, headers={
        "User-Agent": "Academic research (BITS Pilani) — <your email>"
    })
    r.raise_for_status()
    key.write_bytes(r.content)
    return r.content
```

Every later script calls this and nothing else. Once this exists, a re-run costs seconds instead of hours, and you can iterate on parsing without ever re-hitting SEBI.

### Checkpoint

Folders exist, packages import, `cache.get()` fetches and re-reads one SEBI page from disk. Commit.

## The schema — settle this before writing any parser

Changing the schema in week 5 means re-running everything and re-validating. Spend thirty minutes on it now.

**Two tables, never one.** An order names many parties — the company, its promoters, directors, sometimes the auditor and intermediaries. Order-level facts and party-level facts must not share a table or you will double-count penalties.

### `orders` — one row per order

| Column | Type | Notes |
| --- | --- | --- |
| `order_id` | text, PK | SEBI's own numeric id from the URL |
| `source_url`, `pdf_url` | text |  |
| `order_date` | date |  |
| `order_type` | enum | AO, WTM, ED\_CGM, SETTLEMENT, SAT, COURT |
| `text_path`, `has_text_layer`, `n_pages` |  | flags OCR need |
| `violation_start`, `violation_end` | date, nullable | **the key variables** |
| `violation_raw` | text | the sentence they came from — rule 2 |
| `violation_confidence` | enum | high / medium / low / none |
| `violation_method` | enum | regex / model / hand |
| `prior_action_date` | date, nullable | earliest interim order or SCN cited |
| `prior_action_raw` | text |  |
| `regulations` | text (JSON list) | e.g. `["PFUTP 3(a)","SEBI Act 12A"]` |
| `fraud_category` | enum | see below |
| `detection_channel` | enum | see below |
| `*_raw` for each coded field | text | always |
| `amount_involved` | numeric, nullable | sum at stake, where stated |

### `order_entities` — one row per (order, party)

| Column | Type | Notes |
| --- | --- | --- |
| `order_id` | text, FK |  |
| `entity_name`, `entity_name_clean` | text |  |
| `entity_role` | enum | ISSUER, PROMOTER, DIRECTOR, AUDITOR, INTERMEDIARY, TRADER |
| `penalty_amount` | numeric | per party, not per order |
| `sanction_types` | text (JSON list) | PENALTY, DEBARMENT, DISGORGEMENT, WARNING |
| `ticker`, `isin`, `match_confidence` | text / float | filled in week 4 |

### Fraud categories — keep it to eight

More categories means empty cells and unusable interaction terms. Eight is enough to say something and few enough to estimate.

`ACCOUNTING_MISSTATEMENT` · `FUND_DIVERSION` (includes IPO proceeds — your collaborator's original focus) · `MARKET_MANIPULATION` · `INSIDER_TRADING` · `DISCLOSURE_FAILURE` · `RPT_TUNNELLING` · `INTERMEDIARY_MISCONDUCT` · `OTHER`

### Detection channels — seven, including the honest one

`SURVEILLANCE` · `COMPLAINT` · `AUDIT` · `MEDIA` · `REFERRAL` (MCA, RBI, SFIO, IT) · `SELF_DISCLOSURE` · `UNCLEAR`

**Expect `UNCLEAR` to be your largest category, possibly by a wide margin.** Most orders do not say how the matter came to SEBI's attention. This is not a coding failure — it is a finding about the transparency of the enforcement record, and you report the share honestly rather than forcing cases into channels the text does not support. It is also the single most valuable thing the ICAI route could fix later.

### Two rules about nulls

**Distinguish "the order does not say" from "extraction failed."** These are completely different and collapsing them will corrupt your results. Use `violation_confidence = none` with a populated `violation_raw` when the order genuinely has no period; use `low` with an empty raw when your parser could not find one.

**Never impute a date.** A missing violation window means the order drops out of Stage 1, and you report how many dropped and whether they differ systematically from those retained. Guessing dates would silently fabricate your dependent variable.

### Storage

Parquet in `derived/`, queried with duckdb:

```python
import duckdb
duckdb.sql("SELECT fraud_category, count(*) FROM 'derived/orders.parquet' GROUP BY 1").show()
```

## Week 1 (22–28 Sep) — corpus foundations

### Day 1 (Mon) — reconnaissance, no code (3.5h)

**Do not write a scraper today.** Everything downstream depends on facts you do not yet have about what these orders actually look like.

- [ ] **Find the real request parameters.** Open the [orders listing](https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=2&ssid=9&smid=2) in Chrome. Open DevTools → Network. Click to page 2 of the results. Find the request that fires, right-click → **Copy as cURL**. That hands you the exact form parameters, which are not documented and which you should not try to guess. Paste it into `hand/recon_notes.md`.
- [ ] **Enumerate the categories.** From the [enforcement page](https://www.sebi.gov.in/enforcement/orders.html), list all ten order categories and record the `ssid`/`smid` values for each. You want AO orders, Chairperson/WTM orders, ED/CGM orders and Settlement orders. Skip RTI and corporatisation entirely.
- [ ] **Record the total count per category.** The listing shows "1 to 25 of N records" — write down N for each. This is your sample-size reality check and takes five minutes.
- [ ] **Download 25 orders by hand.** Five each from 2012, 2016, 2019, 2022 and 2025, mixed across categories. Save to `raw/pdfs/`.
- [ ] **Read ten of them properly.** Not skim — read. For each, write in `recon_notes.md`: does it state an investigation or examination period, and in exactly what words? Does it reference an earlier interim order or show-cause notice, with a date? Can you tell how SEBI came to look at this? Is there a text layer or is it a scan?

**Output of today:** a list of the actual phrasings you must match, and an honest first read on whether automated extraction will work. This single hour of reading determines whether week 2 takes three days or eight.

Phrasings to expect, from the orders I have checked — confirm and extend:

```
for the period from <date> to <date>
during the period <date> to <date>
investigation period / examination period
the investigation period (hereinafter "IP")
period between <date> and <date>
```

Date formats vary within the same corpus: `May 2011`, `01.05.2011`, `May 01, 2011`, `1st May, 2011`. Your parser must handle all four.

### Day 2 (Tue) — listing scraper (3.5h)

- [ ] `src/scrape_listings.py` — walk every page of every wanted category using the parameters from Day 1, through `cache.get()`.
- [ ] Parse each row to: `order_id` (the trailing number in the detail URL, e.g. `..._80299.html` → `80299`), title, date, detail-page URL, category.
- [ ] Write `derived/listings.parquet`.
- [ ] **Verify against Day 1's counts.** If the listing said 6,424 and you have 4,000, your pagination is silently stopping. Find out why before moving on.

### Day 3 (Wed) — PDF fetcher (3.5h)

- [ ] For each detail page, fetch it and extract the PDF link. The PDFs live under a different path (`/sebi_data/attachdocs/...`), so you must follow the detail page rather than constructing the URL.
- [ ] Download all PDFs to `raw/pdfs/<order_id>.pdf` via the cache. **This is the long one** — at 1.5s per request it runs for hours. Start it early, let it run in the background, and work on Day 4's script while it does.
- [ ] Log failures to `derived/fetch_failures.csv` rather than crashing. Some entries have no attachment.

### Day 4 (Thu) — text extraction (3.5h)

- [ ] `src/extract_text.py` using pymupdf: write `derived/text/<order_id>.txt`, and record `has_text_layer` (a page yielding under \~100 characters is effectively a scan) and `n_pages`.
- [ ] Count how many orders have no text layer. **This number decides Friday.**

### Day 5 (Fri) — OCR triage (3.5h)

- [ ] If under 10% need OCR: run `pytesseract` over just those and move on.
- [ ] If over 30% need OCR: **do not OCR the whole corpus.** Restrict the study period to years with good text layers (likely 2014 onwards, which is also when the 11B summary spreadsheets begin) and say so in the paper as a scope condition. OCR at scale will eat week 2 and give you noisy text anyway.
- [ ] Spot-check 10 extracted text files against their PDFs. Multi-column layouts and tables of noticees are where extraction goes wrong.

### Weekend — buffer and first writing (4h)

- [ ] Catch up on anything that slipped. Something will have.
- [ ] Start `paper/main.tex` and draft the introduction. You can write it now because it does not depend on results.

### Week 1 checkpoint

`derived/listings.parquet` with a row per order matching SEBI's own counts; `raw/pdfs/` populated; `derived/text/` populated; a known OCR share; and `recon_notes.md` listing the phrasings you must parse.

## Week 2 (29 Sep – 5 Oct) — violation windows

This is the week that produces your dependent variable. Nothing else matters as much.

**The approach is hybrid, not one or the other.** Regex alone will get 60–70% and fail on phrasing variation. A model on every order is slow and costs more than it needs to. So: retrieve candidate sentences with regex, parse them with regex where the pattern is clean, and fall back to a model only on what regex could not resolve.

### Day 1 (Mon) — candidate retrieval + regex v1 (3.5h)

- [ ] Split each order's text into sentences.
- [ ] Score each sentence: does it contain a period keyword **and** at least one date-like token? Keep the top three per order.

```python
PERIOD_KW = r"(?:investigation|examination|inspection|relevant|patch)\s+period|period\s+(?:from|between|of)"
DATE_TOKEN = r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*\d{4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})"
```

- [ ] Write the candidates to `derived/window_candidates.parquet` with `order_id`, the sentence, and its position in the document. **Keep the sentence** — it becomes `violation_raw`.
- [ ] Regex v1: extract start and end from candidates matching `period from X to Y`. Use `dateutil.parser` with `dayfirst=True` — Indian orders use DD.MM.YYYY and a US-default parser will silently read 05.11.2019 as 11 May.

### Day 2 (Tue) — model fallback (3.5h)

- [ ] For every order where regex found candidates but could not parse them, send the three candidate sentences to a small fast model asking for `{start, end, confidence}` in JSON, with an explicit "return null if no period is stated" instruction.
- [ ] Cache every response by order\_id — rule 1 applies to model calls too.
- [ ] Set `violation_method` to `regex` or `model` so you can compare accuracy by method during validation.

### Day 3 (Wed) — sanity rules (3.5h)

These catch parser bugs immediately and are not optional:

- [ ] `violation_end <= order_date` — a violation cannot end after the order sanctioning it.
- [ ] `violation_start <= violation_end`.
- [ ] `0 < (order_date − violation_end) < 20 years`.
- [ ] Year is between 1995 and the order year.

Anything failing goes to `derived/window_flagged.csv` for hand review, not into the dataset. **Expect the first run to flag a lot.** Most will be one or two parser bugs, usually day/month inversion or picking up a date from an unrelated citation.

- [ ] Compute your extraction rate: share of orders with a high- or medium-confidence window.

**Read the rate as follows.** Above 70%: proceed as planned. 50–70%: proceed, but report the drop-out rate in the paper and check whether missing-window orders differ systematically by year or category. Below 50%: stop, and take the Gate 1 fallback — hand-code a stratified sample of 300–400 instead.

### Day 4 (Thu) — prior actions and penalties (3.5h)

- [ ] Prior action date: search for `interim order dated`, `ad[- ]interim ex[- ]parte order dated`, `show cause notice dated`, `SCN dated`. Take the **earliest** such date. This matters — it is what separates detection lag from processing lag.
- [ ] Penalties near `penalty of`, `impose a penalty`, `liable to pay`.

**Indian digit grouping will break a naive parser.** Amounts are written `Rs. 26,00,00,000` — two-two-three grouping, not thousands. Also handle `Rs. 26 crore`, `₹1.5 lakh`, `Rs. 26,00,000/-`. Write one normaliser that returns rupees as an integer and unit-test it on ten real strings from your Day 1 sample.

### Day 5 (Fri) — regulations and parties (3.5h)

- [ ] Regulations: match `Regulation \d+`, `Section \d+[A-Z]*`, and the named regimes (PFUTP, ICDR, LODR, PIT, SAST, Intermediaries). Store as a JSON list.
- [ ] Parties: extract the noticee list. Orders usually number them, often in a table near the top. Populate `order_entities` with name and a first guess at role.
- [ ] Role heuristics: "M/s" or Ltd/Limited suffix → likely ISSUER or INTERMEDIARY; named individuals described as promoter/MD/director → PROMOTER/DIRECTOR; "Chartered Accountants" in the name → AUDITOR. Roles get corrected during validation — a rough first pass is fine.

### Weekend — buffer + writing (4h)

- [ ] Fix whatever the sanity rules exposed.
- [ ] Draft the conceptual framework section of the paper.

### Week 2 checkpoint

`derived/orders.parquet` populated with violation windows, confidences, raw sentences, prior action dates, penalties and regulations. A known extraction rate. A flagged file you have actually looked at.

## Week 3 (6–12 Oct) — coding and validation — **Gate 1**

### Day 1 (Mon) — protocol first, then fraud categories (3.5h)

- [ ] **Write `hand/coding_protocol.md` before you code anything.** One paragraph per category with an inclusion rule and a worked example from a real order. Fixing the rules before seeing the data is what makes the coding defensible — and an examiner will ask whether you did.
- [ ] Auto-code fraud category from the regulations, which map more cleanly than you would expect:

| Regulation cited | Category |
| --- | --- |
| PIT Regulations | `INSIDER_TRADING` |
| LODR Reg 23 | `RPT_TUNNELLING` |
| LODR Reg 30, 33 | `DISCLOSURE_FAILURE` |
| SAST | `DISCLOSURE_FAILURE` |
| Intermediaries Regs | `INTERMEDIARY_MISCONDUCT` |
| ICDR (issue context) | `FUND_DIVERSION` |
| PFUTP Reg 3/4 | **ambiguous — needs the text** |

- [ ] PFUTP is the big ambiguous bucket and will cover a large share of your corpus. Disambiguate from the narrative: language about books, revenue, receivables or fictitious sales → `ACCOUNTING_MISSTATEMENT`; language about trades, price, volume, synchronised or circular dealing → `MARKET_MANIPULATION`.

### Day 2 (Tue) — detection channel (3.5h)

Search for channel markers, then a model pass on anything unmatched:

| Channel | Text markers |
| --- | --- |
| `SURVEILLANCE` | surveillance, alert generated, abnormal price movement, exchange observed |
| `COMPLAINT` | complaint, complainant, whistle blower |
| `AUDIT` | qualified opinion, auditor resigned, forensic audit |
| `MEDIA` | news article, media report, research report |
| `REFERRAL` | reference received from, MCA, SFIO, Reserve Bank, Income Tax |
| `SELF_DISCLOSURE` | company informed, suo moto |

- [ ] Everything else is `UNCLEAR`. Do not stretch a marker to avoid that label — the share of `UNCLEAR` is a reportable finding about how much the enforcement record discloses.

### Days 3–4 (Wed–Thu) — hand-code 150 (7h)

- [ ] Draw a random sample of 150, stratified by year and order category. Save the ids to `hand/validation_sample.csv`.
- [ ] **Code them blind.** Build the sheet with only `order_id` and the PDF link — no automated output visible. If you can see the machine's answer you will anchor on it and your validation will overstate accuracy.
- [ ] Code: violation start, violation end, fraud category, detection channel, primary issuer name.
- [ ] About 2–3 minutes per order once you have a rhythm. Do them in two sittings, not one.

### Day 5 (Fri) — measure and fix (3.5h)

- [ ] Violation window: exact match rate, and match-within-one-month rate. Report both.
- [ ] Fraud category and detection channel: agreement rate and **Cohen's kappa** — raw agreement flatters you when one category dominates.
- [ ] Break accuracy down by `violation_method` (regex vs model) and by year. Systematic failure in one era is a scope condition worth knowing now.
- [ ] Fix the systematic errors, then re-run. **Do not re-validate on the same 150** — you have now seen them. If you need a second validation round, draw a fresh sample.

**Solo-coder problem, and a cheap fix.** Inter-coder reliability normally needs two coders. Two options: re-code 30 orders yourself in week 6 and report intra-coder reliability, or send 30 to your ICAI collaborator with the protocol. The second is a genuinely good use of them — low effort for them, and it gives you a reliability statistic you could not otherwise report.

### Weekend — **Gate 1** (4h)

**The decision, made on numbers rather than mood:**

| Extraction rate | Kappa | Decision |
| --- | --- | --- |
| >70% | >0.7 | Proceed as planned |
| 50–70% | >0.6 | Proceed; report drop-out and test whether missing cases differ systematically |
| <50% | any | **Fall back**: hand-code a stratified 300–400 and run Stage 1 on that |
| any | <0.6 | Protocol is too vague — rewrite the category rules, recode |

The fallback is not failure. Three to four hundred hand-coded orders is eight times the original draft's fifty and is entirely sufficient for a duration model. It costs roughly 20 hours, which week 4's buffer plus the weekends can absorb.

- [ ] Write the data section of the paper with the real numbers in it.

### Week 3 checkpoint

Fully coded corpus, a validation table you can paste into the paper, a Gate 1 decision written down in `results_log.md`.

## Week 4 (13–19 Oct) — matching and market data

This is the week most likely to overrun. Two principles keep it contained.

**Sample first, then fetch.** Do not build a market-wide panel. Define your analysis sample — fraud firms plus matched controls — and pull data only for those firms. The difference is a few hundred companies instead of several thousand, and it is the single biggest time saving available in the project.

**ISIN and scrip code beat names.** Many orders quote the scrip code or ISIN somewhere in the text. Search for those first; a code match is exact and needs no review. Fall back to name matching only where no code exists.

### Day 1 (Mon) — listed universe and normalisation (3.5h)

- [ ] Pull the NSE equity list and the BSE scrip list (name, symbol, ISIN, scrip code).
- [ ] Write one normaliser applied identically to both sides:

```python
def norm(name):
    s = name.upper()
    s = re.sub(r"[^A-Z0-9& ]", " ", s)
    s = s.replace("&", " AND ")
    s = re.sub(r"\b(LIMITED|LTD|PVT|PRIVATE|M S|COMPANY|CO|THE|INDIA)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()
```

- [ ] **The name-change problem.** A 2013 order names a company that has since been renamed, and the current NSE list will not contain the old name. BSE publishes name-change history — pull it and build an alias table mapping historical names to current ISINs. Without this you will silently lose a chunk of your older orders, and older orders are exactly the ones with complete detection histories.

### Day 2 (Tue) — matching (3.5h)

- [ ] `rapidfuzz.fuzz.token_sort_ratio` between normalised order entity names and the universe.
- [ ] Three bands: **≥92 auto-accept**, **80–92 hand review**, **<80 auto-reject**.
- [ ] Review the middle band by hand. Expect a few hundred rows; budget two hours and do it in one sitting.
- [ ] Record `match_confidence` and `match_method` (code / auto / hand) on every row. Unmatched entities are not a failure — many noticees are individuals or unlisted firms and correctly have no ticker.

### Day 3 (Wed) — define the analysis sample (3.5h)

- [ ] **Fraud firm-years**: every (firm, year) falling inside a violation window, keeping only matched listed issuers.
- [ ] **Control firms**: for Stage 3's event study you need controls matched on industry, size and pre-event return. For Stage 2 you need a broad cross-section of non-fraud firm-years — sample several hundred firms rather than taking the universe.
- [ ] Freeze the sample to `derived/sample.parquet` and **do not change it afterwards**. A sample that shifts mid-analysis makes results irreproducible and you will not remember which version produced which number.

### Day 4 (Thu) — prices (3.5h)

- [ ] You need daily prices for sample firms plus a market index (NIFTY 50 or NIFTY 500). Nothing else.
- [ ] Pull from NSE/BSE bhavcopy archives through the cache. **Note the archive URL structure changed in 2024** — older and newer dates use different paths, so handle both and verify you get continuous coverage across the boundary.
- [ ] Sanity check: plot one well-known scrip over ten years and look at it. Splits and bonus issues produce discontinuities; use adjusted series or handle corporate actions explicitly.
- [ ] For each event date you need roughly 250 trading days before (estimation window) and 10 after. Pull generously once rather than returning for more later.

### Day 5 (Fri) — ownership (3.5h)

- [ ] Quarterly shareholding patterns from BSE for sample firms: promoter holding percentage and promoter shares pledged. Both are in the standard filing.
- [ ] These are the core Stage 2 predictors in the reduced design, so coverage here matters more than breadth elsewhere.
- [ ] Audit qualification and auditor identity require annual report parsing. **Timebox this to two hours.** If it is not working, drop it — Stage 2 runs without it, and its absence is what makes the network extension infeasible, which you have already planned for.

### Weekend — merge (4h)

- [ ] Join everything into `derived/panel.parquet`. Assert row counts at every join. A join that silently multiplies rows is the classic way to corrupt a panel, and it will not announce itself.
- [ ] Print coverage: what share of fraud firms have prices, have shareholding, have both. **This number goes in the paper.**

### Week 4 checkpoint

A frozen analysis sample, a merged panel, and an honest coverage table. From here the work is estimation, which is fast.

## Weeks 5–6 (20 Oct – 2 Nov) — Stage 1 — **Gate 2**

This is the paper's primary contribution. It must be finished and written before anything else starts.

### Week 5, Day 1 (Mon) — build the variable, then look at it

```python
first_action = df[["prior_action_date", "order_date"]].min(axis=1)
df["T_months"] = (first_action - df["violation_end"]).dt.days / 30.44
```

- [ ] Drop non-positive durations to a flagged file and inspect them. A negative lag means an extraction error, not a fast regulator.
- [ ] **Descriptives before any model.** Median and IQR overall, by fraud category, by order type, by violation-end year. Plot the histogram and look at it.
- [ ] Kaplan–Meier curve (`lifelines.KaplanMeierFitter`), overall and split by category.

**A note on what KM means here.** Every case in your sample was detected, so there is no ordinary right-censoring — the KM curve is essentially the empirical distribution. The real problem is right *truncation*, handled on Day 5. Do not describe these as survival curves with censoring in the paper; it would be wrong and an examiner will catch it.

- [ ] Write the median lag into `results_log.md`. **This is your headline number.**

### Day 2 (Tue) — Cox baseline

```python
from lifelines import CoxPHFitter
cph = CoxPHFitter()
cph.fit(d, duration_col="T_months", event_col="event",
        cluster_col="firm_id")      # firms recur across orders
```

- [ ] Build up in blocks, reporting each: visibility → + opacity/control → + fraud characteristics → + regime. Coefficients that move sharply between blocks tell you what is collinear.

### Day 3 (Wed) — test the assumption

- [ ] `cph.check_assumptions(d, p_value_threshold=0.05)` — lifelines runs the Schoenfeld residual tests for you.
- [ ] If proportional hazards fails for a covariate: stratify on it (`strata=[...]`) or move to AFT. **Do not ignore a failed PH test** — it is the first thing a competent examiner checks, and it takes ten minutes to handle.

### Day 4 (Thu) — Weibull AFT

```python
from lifelines import WeibullAFTFitter
aft = WeibullAFTFitter().fit(d, "T_months", "event")
```

- [ ] AFT coefficients exponentiate into time ratios — "a Big-4 auditor is associated with detection 23% sooner" — which is the form your policy chapter and the ICAI note can actually use.
- [ ] Agreement between Cox and AFT is a specification check. Report both in an appendix table.

### Day 5 (Fri) — truncation

- [ ] **Cohort restriction**: keep violation windows closing on or before 2018, giving eight years of follow-up. Re-run the Cox model on this sample — this becomes your primary specification.
- [ ] **Truncation-adjusted distribution**: in R, `SurvTrunc::cdfDT()` on the full sample. Report naive and adjusted side by side so the size of the distortion is visible.
- [ ] Plot both. The gap between them is a figure worth including.

### Week 6, Days 1–2 (Mon–Tue) — competing risks in R

```r
library(cmprsk)
ci  <- cuminc(ftime = d$T_months, fstatus = d$channel_code)
fg  <- crr(d$T_months, d$channel_code, cov1 = X, failcode = 1)
```

- [ ] Cumulative incidence by channel: the share of first detections attributable to surveillance, complaint, audit, media, referral. **This is the most quotable result in the paper.**
- [ ] Fine–Gray subdistribution models per channel, plus cause-specific Cox as a robustness check.
- [ ] Report `UNCLEAR` as its own category throughout. Excluding it would bias the shares of everything else.

### Day 3 (Wed) — regime effects, carefully

- [ ] Indicators for violation windows closing after LODR (Dec 2015) and after NFRA (Nov 2018).
- [ ] **Run these only on the cohort-restricted sample.** Truncation mechanically mimics faster detection in recent years, so an unrestricted regime result is uninterpretable. Say this explicitly in the text — anticipating the objection is worth more than the result itself.

### Day 4 (Thu) — robustness

- [ ] Alternative definition: violation *start* to first action, instead of window end.
- [ ] Excluding settlement orders.
- [ ] Excluding model-extracted windows, keeping only regex and hand-coded.
- [ ] Hand-coded 150 only — does the sign and rough magnitude survive?

### Day 5 (Fri) — **Gate 2**

- [ ] Stage 1 results section fully drafted, with final tables and figures.

**The rule, and it is not negotiable.** If Stage 1 is not drafted by 2 November, cut Stage 2 entirely and go to Stage 3. The paper works as Stages 1 and 3 — detection lags plus deterrence — and H1 becomes a stated open question. A thoroughly executed two-stage paper beats three rushed stages, and examiners can tell the difference immediately.

### Weeks 5–6 checkpoint

A drafted results chapter with a median detection lag, a hazard model that survives its own assumption tests, a channel decomposition, and a truncation adjustment.

## Weeks 7–8 (3–16 Nov) — Stages 3 and 2 — **Gate 3**

**Stage 3 comes before Stage 2 deliberately.** It is cheaper, its data is already in hand, and it will almost certainly work. Stage 2 is the one that might not. Bank the certain result first.

## Week 7 — Stage 3, the event study

### Day 1 (Mon) — event dates

- [ ] One event per firm: the **earliest** public action. If a firm recurs, use the first and note repeat firms for a robustness cut — later orders are contaminated by the first.
- [ ] Separate the three event types: interim orders, final orders, settlement orders. **Interim orders are your cleanest signal** because they are usually the first public news.
- [ ] Estimation window (−250, −30) trading days; event windows (−1,+1), (−5,+5), (0,+10).
- [ ] Require at least 60% of estimation-window days traded. Log the firms that fail.

### Day 2 (Tue) — abnormal returns

- [ ] Market model per firm, then `AR_it = R_it − (α̂ + β̂ R_mt)`, cumulated over each window.
- [ ] **Thin trading correction.** Many sanctioned firms are small-caps where non-synchronous trading biases beta downward. Use Dimson betas — regress on lagged, contemporaneous and lead market returns and sum the coefficients:

```python
# R_it = a + b_{-1} R_m,t-1 + b_0 R_m,t + b_{+1} R_m,t+1 + e
# beta_dimson = b_{-1} + b_0 + b_{+1}
```

- [ ] Report a liquid subsample separately.

### Day 3 (Wed) — test statistics that survive scrutiny

- [ ] **Do not use a naive t-test.** Event-induced variance inflation makes it reject far too often, and this is a well-known objection.
- [ ] Use the **standardised cross-sectional (Boehmer–Musumeci–Poulsen) test**, which corrects for exactly that.
- [ ] Add **Corrado's rank test** as a non-parametric check. With illiquid small-caps, returns are far from normal and the rank test is robust to it.
- [ ] Agreement across the three is what makes the result defensible.

### Day 4 (Thu) — cross-section

```
CAR_i ~ log(penalty) + debarment + promoter_named + log(mktcap)
        + fraud_category + detection_channel
```

- [ ] The number you actually want is **the gap between total market-value loss and the monetary penalty** — the implied reputational cost. Compute it as `|CAR| × pre-event market cap` versus the penalty, and report the ratio.

### Day 5 (Fri) — suspensions and write-up

- [ ] Firms suspended at the event have no returns exactly when it matters. **Do not silently drop them** — that selects out the most severe cases. Report how many, treat suspension as its own outcome, and bound the main estimate.
- [ ] Draft the Stage 3 results section.

## Week 8 — Stage 2, prediction

### Day 1 (Mon) — the panel

- [ ] Firm-year rows, labelled fraudulent if inside a violation window. **The label is the violation year, never the order year.**
- [ ] Reduced predictor set: promoter holding, promoter pledge, change in both, 12-month return, volatility, turnover, delivery ratio, log market cap, and audit flags if week 4 produced them.
- [ ] Lag every predictor to a realistic publication date. A March year-end is not public in March.

### Day 2 (Tue) — baseline and transfer test

- [ ] Logit with the available predictors, in-sample, as a reference point.
- [ ] If you have accounting data, run Beneish M-score with **original coefficients** as a pure transfer test. If it performs no better than chance on Indian data, that is a genuine finding and worth a paragraph.

### Day 3 (Wed) — real models, honest validation

- [ ] Train on violation years ≤ 2018, test on 2019+. **Out-of-time only. Never a random split.**
- [ ] LASSO logit and LightGBM. Handle imbalance with class weights (`scale_pos_weight`), not by oversampling.
- [ ] **Two leakage traps.** Fit scalers and imputers inside the training fold only — fitting on the full data before splitting leaks the test distribution. And check no predictor encodes the future: a delisting flag, a suspension marker, anything recorded after the fact.

### Day 4 (Thu) — the capacity frontier

This is Stage 2's actual contribution, not the AUC.

```python
# within each test year: rank firms by predicted probability,
# take the top k, compute the share of that year's frauds captured
def recall_at_k(y_true, y_score, k):
    top = np.argsort(y_score)[::-1][:k]
    return y_true[top].sum() / max(y_true.sum(), 1)
```

- [ ] Sweep *k* from 50 to 1000 and plot recall against it.
- [ ] Mark **k = 402**, SEBI's actual investigations taken up in 2025–26.
- [ ] Plot the random-ranking baseline (recall ≈ k/N) on the same axes. The vertical gap between your curve and that line is the entire result.
- [ ] Report AUPRC alongside. Report AUC too, but say in the text why it flatters under this class imbalance.

### Day 5 (Fri) — **Gate 3**

- [ ] Draft the Stage 2 results section.

**The gate.** Start the network extension only if all three stages are drafted *and* week 4 produced auditor identity cheaply. Otherwise week 9 goes entirely to synthesis, and §7.3 of the proposal is described as future work. **Expect this gate to close.** That is the planned outcome, not a failure.

### Weeks 7–8 checkpoint

Three drafted results sections. The paper is now complete in substance and needs assembling.

## Weeks 9–10 (17–30 Nov) — synthesis and assembly

### Week 9, Day 1 (Mon) — the implied penalty

This is what ties the three stages into one argument rather than three chapters.

```latex
S^{*} \;=\; \frac{H}{p} \cdot e^{\,r\tau}
```

Each term now has a source:

| Term | Source | Caution |
| --- | --- | --- |
| *H*, harm | Stage 3: mean \|CAR\| × pre-event market cap | Measures shareholder loss, not total social harm |
| *τ*, delay | Stage 1: median detection lag | Use the cohort-restricted estimate |
| *r*, discount | Assumed, 8–12% | Report both |
| *p*, detection probability | **The weak term** | See below |

**Be honest about *p*.** You cannot estimate the detection probability from your data, because the denominator — frauds committed — is exactly what is unobserved. Do not manufacture a number. Instead:

- [ ] Present *S\** as a **sensitivity table**, not a point estimate: rows for p ∈ {0.20, 0.33, 0.50}, columns for r ∈ {8%, 12%}.
- [ ] Anchor p = 0.33 to Dyck, Morse and Zingales's US estimate and label it clearly as an imported benchmark, not an Indian finding.
- [ ] Compare the resulting range against the **observed median penalty** from your corpus.

A sensitivity table that shows observed penalties falling short across every cell is a far stronger result than a single number resting on a *p* you cannot defend. It also pre-empts the obvious objection.

### Days 2–3 (Tue–Wed) — assemble

- [ ] Stitch the drafted sections into one document. Everything should already exist — if a section is missing now, it is because writing slipped, and this is where that cost lands.
- [ ] Write the conclusion and policy section **from the results**, not from the original draft's recommendations. The whole point of the redesign was that recommendations follow findings.
- [ ] Write the abstract last, with the three headline numbers in it.

### Day 4 (Thu) — robustness appendix

- [ ] Collect every robustness cut already run into one appendix. Do not run new ones now.
- [ ] Extraction validation table from week 3.
- [ ] Coverage table from week 4.

### Day 5 (Fri) — cold read

- [ ] Read the whole thing start to finish without editing, marking problems as you go. Then fix.

### Week 10 — revision

**Days 1–2**: address everything from the cold read; tighten the introduction, which will have drifted from what the paper actually does.

**Day 3**: figures and tables. Every figure needs axis labels and units; every table needs N and a note saying what the sample is.

**Day 4**: final checks.

- [ ] Every number in the text matches `results_log.md`. Check them one by one — this catches real errors every time.
- [ ] Every table reports its sample size.
- [ ] Every figure is readable in greyscale and at print size.
- [ ] Every citation resolves to a real paper with correct year, volume and pages.
- [ ] **Reproducibility**: fresh clone, run the pipeline end to end, confirm the headline numbers come out identical.
- [ ] **Causal language audit.** Search the draft for "causes", "leads to", "results in", "effect of". Stage 1 estimates associations, not causal effects. Stage 3's event study and the placebo-disciplined tests are where causal language is earned. Fix everything else — this is the easiest thing for an examiner to catch and the easiest to prevent.
- [ ] Limitations section is present, specific, and names the selection-on-detection problem directly rather than burying it.

**Day 5**: buffer. Submit 1 December.

### If you are behind at the start of week 10

Cut in this order, and decide quickly rather than agonising: the network extension (already assumed gone), then the partial-observability robustness, then Stage 2 in full — moving H1 to "open question" in the conclusion. **Submitting a complete two-stage paper beats submitting an incomplete three-stage one**, and the difference is obvious to anyone reading it.

## Writing: what goes down each week

At ten weeks, a paper written in the last fortnight is a paper that misses. Roughly a fifth of each week is writing, and each section is drafted when its inputs exist — not later.

| Week | Write | Why it can be written then |
| --- | --- | --- |
| 1 | Introduction, motivation | Needs no results |
| 2 | Conceptual framework, deterrence condition | Theory, not data |
| 3 | Data: sources, corpus, extraction validation | You now have the real counts |
| 4 | Data: sample, matching, coverage | Panel is built |
| 5–6 | Stage 1 results | As estimates land |
| 7 | Stage 3 results | ″ |
| 8 | Stage 2 results | ″ |
| 9 | Synthesis, policy, conclusion, abstract | Everything exists |
| 10 | Revision only | — |

**Three things that make this work.**

**Put the literature review in the tired hours.** It needs reading and care but not concentration in the way debugging a parser does. Use the evenings when you are too flat to code. Spread it across weeks 1–4 rather than blocking a day for it.

**Write the number into the text the day you compute it.** Not "\[TBD\]" — the actual figure, in a sentence. Placeholders accumulate silently and by week 9 you will have thirty of them and no memory of which script produced which.

**Draft sections in whatever state they are in.** A rough results paragraph written in week 5 is worth more than a perfect one planned for week 9, because in week 9 you will be revising rather than drafting, and revision of bad prose is much faster than creation from nothing.

## ICAI: nothing blocks you

**Your assumption is correct.** Every step in this runbook runs on public data. You are not waiting on anyone, and you should not delay Day 0 for any conversation.

The SEBI orders, the annual report tables, exchange price archives and shareholding filings are all public and already accessible. Nothing in the committed scope — Stage 1, Stage 3, or Stage 2 in reduced form — has an external dependency.

### Where ICAI input would genuinely add something

| When | Ask | What it buys | If it does not come |
| --- | --- | --- | --- |
| Week 3 | 30 orders coded against your protocol | An inter-coder reliability statistic you cannot otherwise produce | Re-code 30 yourself in week 6; report intra-coder reliability |
| Week 4 | Database access with financials and auditor fields | Full Stage 2 predictor set; makes the network extension viable | Stage 2 runs reduced; network drops |
| Anytime | Investigation initiation dates | Splits detection lag from processing lag — the single biggest quality gain available | Composite lag, clearly labelled |
| Anytime | Cases closed without action | The missing comparison cell | Stage 2 stays conditional on sanction |

The week 3 ask is the one to make now, because it is small, concrete, easy for them to say yes to, and it lands exactly when you need it.

### The point of no return

**Data arriving after the end of week 5 cannot change the design.** By then the sample is frozen, the corpus is coded and Stage 1 is running. Anything that shows up later can only be a robustness check or a line in the future-work section.

So when you send the collaborator note, say plainly that the study proceeds regardless and give **the end of October** as the date past which new data cannot be incorporated. That is not pressure — it protects both of you from a situation where something arrives in week 8 and you have to choose between ignoring it and wrecking the timeline.

### What to do this week

- [ ] Send the collaborator note.
- [ ] Make the week 3 coding ask in the same message — small, specific, easy to agree to.
- [ ] State the end-of-October cut-off.
- [ ] **Then start Day 0 tonight without waiting for a reply.**
