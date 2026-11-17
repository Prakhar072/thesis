# Your remaining tasks

*Written 2026-10-21. These are the steps only you can do: runs that take hours, hand coding, reviews, decisions
and writing. Tick the boxes as you go.*

Related documents:
- [CODE_GUIDE.md](CODE_GUIDE.md): how each file works, for your review.
- [PROJECT_GUIDE.md](PROJECT_GUIDE.md): how to use the data.
- [CLAUDE.md](CLAUDE.md) §2: status, for agents.

Every command runs from the repository with the virtual environment active:

```bash
cd ~/projects/thesis && source .venv/bin/activate
```

---

## Where things stand

- **Built and run:** the corpus (20,861 orders, text for 20,849); windows, actions, parties, fraud category and
  detection channel for every order; `derived/orders.parquet` (20,861 × 71).
- **Run by you since 2026-10-21:** prices (A1), the universe and the matcher (A2–A3), the match review (A4), the
  sample freeze (A5, 13 Nov, fingerprint `342166281b7a65fa`), the blind sample, its coding and scoring (B1–B4, 17 Nov:
  **Gate 1 = proceed**, category κ 0.74, channel κ 0.63). Price check (A6) done 17 Nov.
- **Skipped by your decision (17 Nov, time):** B5 (validation fixes) and B6 (intra-coder re-code). The errors are
  reported as limitations (`results_log.md`, 2026-11-17).
- **Scope:** all three stages stay; Stage 2 or 3 is dropped only if time truly runs out (your decision, 17 Nov).
- **Built, not yet run:** ownership (A7, you start it) and the panel (A8).
- **Built and run 17 Nov:** matter grouping (D1 = matter) and Stage 1 (`python -m src.stage1`): first estimates in
  `results_log.md`, tables in `paper/tables/stage1_*.tex`, figures in `paper/figures_stage1.pdf`.
- **After the ownership download (A7) finishes:** run `python -m src.build_panel` (A8), then
  `python -m src.stage1 --ownership` to add market cap and ownership to Stage 1's model (4). Don't run `--ownership`
  while the download is still going: it writes partial checkpoints.
- **Not built:** the finality classifier, Stage 3 and Stage 2 estimation code.
- **Thesis:** `paper/paper.tex` drafted on 2026-11-07: everything except the results. §4 refreshed to the frozen sample
  on 17 Nov. See E.
- **Every function** in `src/` documents its inputs and output with their types. All 838 tests pass.

**Open since the draft (see `results_log.md`, 2026-11-07):**
- **Fix the penalty reader.** 498 orders direct payment of a penalty that was not read; 14 of 14 read at random were
  real. `extract_actions` needs the fix and stages 7–11 a re-run. Sanctions are outside the blind sample, so this need not
  wait for the coding. Ask an agent.
- **Stage 2: decided 17 Nov: kept.** Gate 2 passed without Stage 1 results, but you chose not to apply the runbook's cut
  rule; Stage 2 goes only if time runs out.
- **Two steps the estimates need are not built:** matter grouping (D1) and the finality classifier (interim / final).
- **Add the paper behind Damle and Zaveri Shah.** The thesis cites their blog post and dataset; SSRN 4198772 blocked
  automated reading, so its exact title is unconfirmed.

## Recommended order

| # | Task | Your time | Runs for | Section |
|---|---|---|---|---|
| 1 | Start the price download | 5 min | 7–8 h, in the background | [A1](#a1-start-the-price-download) |
| 2 | Draw the blind validation sample | 2 min | seconds | [B1](#b1-draw-the-blind-sample) |
| 3 | Build the universe, run the matcher | 5 min | 1 min | [A2–A3](#a2-build-the-listed-universe) |
| 4 | Review the 564 borderline matches | ~2 h | — | [A4](#a4-review-the-borderline-matches) |
| 5 | Decide when to freeze, then freeze the sample | 15 min | seconds | [D2](#d2-when-to-freeze-the-sample), [A5](#a5-freeze-the-analysis-sample) |
| 6 | Hand-code the 150 orders | ~7 h, two sittings | — | [B2](#b2-hand-code-the-150-orders) |
| 7 | Score them; record Gate 1 | 30 min | seconds | [B3–B4](#b3-score-the-coding) |
| 8 | Check the prices; start the ownership download | 15 min | 6–9 h, in the background | [A6–A7](#a6-sanity-check-the-prices) |
| 9 | Merge the panel; record coverage | 15 min | 1 min | [A8](#a8-merge-the-panel-and-record-coverage) |
| 10 | Decide the unit of analysis for Stage 1 | 30 min | — | [D1](#d1-the-unit-of-analysis-for-stage-1) |
| 11 | Fix the systematic errors validation finds (with an agent) | 1–3 h | — | [B5](#b5-fix-the-systematic-errors) |
| 12 | Review the code base | ~10 h, spread out | — | [C](#c-review-the-code-base) |
| 13 | Write | through to 1 December | — | [E](#e-write-the-paper) |

**Why this order.**
- The two downloads are long and need nothing from you while they run, so start them early.
- Draw the blind sample before you look at more of the pipeline's output, so the 150 are fixed while you are
  still blind to them.
- Code the 150 before the code review: reviewing means running stages on example orders, which shows the
  pipeline's answers.
- Stage 1 needs three things from you: the unit of analysis (D1), the validated windows (B), and the frozen sample
  (A5).

**The calendar (from `timeline.md`).**

| When | Milestone | What it needs |
|---|---|---|
| 20 Oct – 2 Nov (Weeks 5–6) | Stage 1, detection lags | D1, B, A5 |
| 2 Nov | **Gate 2**: Stage 1 results section drafted | |
| 3–9 Nov (Week 7) | Stage 3, event study | A1, A5, A8 |
| 10–16 Nov (Week 8) | Stage 2, prediction | A7, A8 |
| 16 Nov | **Gate 3** | |
| 17–30 Nov (Weeks 9–10) | Synthesis, assembly, revision | |
| 1 Dec | Submit | |

---

## A. Week 4 data runs

### A1. Start the price download
- [x] Started
- [x] Finished

**Why.** Daily prices for every listed equity from 2004, the BSE 500 index and corporate actions. Stage 3 needs
them for the event study, and Stage 2 for its market signals. This is the longest step, and it needs nothing else.

```bash
setsid nohup .venv/bin/python -m src.fetch_prices > logs/fetch_prices.out 2>&1 < /dev/null &
```

**While it runs:**
- `tail -f logs/fetch_prices.log` shows a progress line every 250 days, with an ETA.
- `ls derived/prices/` gains one file per exchange per year as each year completes.
- About 12,200 requests, ~7–8 hours, ~1–2 GB of disk.
- Do not start the ownership download (A7) until this one finishes: both use BSE's servers, and the project
  never runs two fetches at once.

**If it stops** (the laptop sleeps, WSL restarts): run the same command again. Finished days come back from the
cache in seconds, and days NSE marked as holidays are not asked again.

**Done when** the log ends with "days by status" and "network_calls=… elapsed …". Look at two lines:
- "weekdays only one exchange has" should be a handful.
- "rows" should run to millions for each exchange.

### A2. Build the listed universe
- [x] Done

```bash
python -m src.universe
```

This takes seconds and makes no network calls: its seven lists were cached on 2026-10-14. The log should show
10,180 firms and 23,900 aliases.

### A3. Run the matcher
- [x] Done

```bash
python -m src.match_entities
```

This takes about 35 seconds. The log should show about 8,600 orders with a matched issuer, and 564 names written
to `hand/match_review.csv` for your review.

### A4. Review the borderline matches
- [x] Reviewed (all 564; 13 Nov)
- [x] Re-ran the matcher

**Why.** Names that score 80–92 against a listed company, or that look alike for other reasons, are neither
accepted nor rejected automatically. Each decision can add or remove a fraud firm.

**Steps.**
1. Open `hand/match_review.csv` in a spreadsheet. Rows are sorted with the most-used names first.
2. Read each row. The columns:
   - `query`: the name as an order writes it.
   - `n_orders`: orders that name it as their issuer.
   - `n_parties`: company parties with that name.
   - `candidates`: up to three listed firms, each written as "firm_id name [status] score".
   - `example_title`: SEBI's title of one order that uses the name.
   - `note`: why the name is here. One of: "fuzzy 80–92", "first word differs", "two firms ≥ 92",
     "same name, several firms", "short name".
3. Fill in `decision`:
   - **`accept`**: the first candidate is the same company. Renamed companies and spelling slips count:
     "Gati Limited" and "Allcargo Gati Limited" are one company, and so are "Datsons Labs" and "Dr. Datsons Labs".
   - **`reject`**: no candidate is that company. Examples are different companies that share words (Satia vs
     Star Paper Mills), and an unlisted subsidiary or collective scheme.
   - **a `firm_id`** (for example `INE641C`), copied from the second or third candidate when that one is right.
   - **blank**: you are not sure. The name stays unmatched, so a blank is never a wrong match.
4. If you can't tell from the names, search BSE's website. Or search the universe:
   ```bash
   python -c "import pandas as pd; f = pd.read_parquet('derived/firms.parquet'); print(f[f['name'].str.contains('Datsons', case=False)].to_string())"
   ```
5. Don't edit other columns, and don't delete rows. Save as CSV.
6. Re-run `python -m src.match_entities`. The log's "review sheet" line should count your decisions, and the
   issuer methods should now include `hand`.

**Time.** About 2 hours for all 564. Rows with `n_orders` ≥ 1 matter most (issuers); names used only by parties
can wait.

### A5. Freeze the analysis sample
- [x] D2 decided (froze first)
- [x] Frozen (13 Nov, fingerprint `342166281b7a65fa`)
- [x] Recorded (`results_log.md`, 17 Nov)

**Why.** The sample (fraud firms, their firm-years, 600 weighted control firms, enforcement events) decides which
firms get ownership data. The runbook says it must not change afterwards.

```bash
python -m src.build_sample
```

This runs once. It refuses to overwrite a frozen sample.

**Record** in `results_log.md` (a new "Week 4: sample frozen" entry):
- the log's fraud firms, fraud firm-years (and the in-period count), controls with their weight, and events;
- the sample's **fingerprint**.

Then commit `hand/sample_frozen.csv`.

### A6. Sanity-check the prices
- [x] Done (17 Nov: TATASTEEL and 532532 clean; `results_log.md`)

The runbook asks you to "plot one well-known scrip over ten years and look at it". After A1 has finished:

```bash
python -m src.prices --check TATASTEEL
explorer.exe "$(wslpath -w figures/price_check_TATASTEEL.png)"
```

**What to look for.**
- **Top panel:** a cliff at the 28 July 2022 split (1:10).
- **Bottom panel (adjusted):** no cliff there, and no other cliffs.

**The log** lists the five largest raw daily moves and "days still beyond +100% / −50% after adjustment"; that
count should be zero.

**Try one fraud firm as well**, for example Jaiprakash Associates on BSE:
`python -m src.prices --check 532532 --exchange BSE`.

**Record** a line in `results_log.md`.

### A7. Download ownership
- [ ] Started
- [ ] Finished

Promoter holding and promoter pledging for each sample firm, from BSE's shareholding filings. This runs after A1
(it uses each firm's trading dates to skip quarters) and after A5 (it reads the frozen sample).

```bash
setsid nohup .venv/bin/python -m src.fetch_shareholding > logs/fetch_shareholding.out 2>&1 < /dev/null &
```

- **Size:** up to 11 March quarters per firm, for 1,732 firms on BSE; ~6–9 hours.
- **Progress:** logged every 50 firms. A re-run resumes from the cache.
- **Short on time?** `--role fraud` fetches the fraud firms first.
- **Data starts in December 2015.** Earlier quarters do not exist in this source, so ownership covers FY2016
  onward.

### A8. Merge the panel and record coverage
- [ ] Merged
- [ ] Recorded

```bash
python -m src.build_panel
```

This writes `derived/panel.parquet`, one row per firm and financial year for FY2010–2025, and
`derived/panel_coverage.csv`.

**The coverage table goes in the paper** (runbook: "This number goes in the paper"). It gives the share of fraud
firms, of control firms and of fraud firm-years with prices, with ownership, and with both. Copy it into
`results_log.md`.

---

## B. Week 3 validation (deferred until now)

### B1. Draw the blind sample
- [x] Done

```bash
python -m src.validation_sample
```

This takes seconds and runs once. It writes:
- `hand/validation_sample.csv`: your coding sheet, 150 orders, shuffled.
- `hand/validation_pdfs/`: a copy of each order's PDF. You may annotate these; never annotate the files in
  `raw/pdfs`.
- `derived/validation_key.parquet`: strata and weights. **Don't open it.**

From now until B3, don't browse `derived/orders.parquet` or the `order_*` tables for orders on your sheet. The
coding must be blind to the pipeline's answers.

### B2. Hand-code the 150 orders
- [x] Sitting 1 (rows 1–75)
- [x] Sitting 2 (rows 76–150)

**Set-up.** Open `hand/coding_protocol.md` and the folder of copies:
`explorer.exe "$(wslpath -w hand/validation_pdfs)"`. Each row's `local_file` names its copy, and the copies are
numbered in sheet order (`001_…pdf`).

**For each order, fill in:**

| Column | What to write | Protocol |
|---|---|---|
| `violation_start`, `violation_end` | YYYY-MM-DD. A month alone: the 1st, or the month's last day. FY 2011-12: 2011-04-01 to 2012-03-31. Several patches: earliest start to latest end | §2 |
| `window_kind` | `stated_period` (the order names the period), `conduct_dates` (only the conduct's dates are given) or `none` | §2 |
| `fraud_category` | one of the eight; when several are charged, the first in the precedence order | §3 |
| `detection_channel`, `detection_detail` | one of the eight channels, and a detail from the §5 table | §5 |
| `primary_issuer` | the company whose securities the matter is about, as named; `NONE` if none | §4 |
| `notes` | `?` first for a judgement call; `OTHER-a` … `OTHER-e` for OTHER; why, for any blank | §1 |

**The window is one quantity:** the period of business activity SEBI examined. "Investigation period",
"examination period", "inspection period" and "relevant period" all fill the same two dates. Inspection *visit*
dates never count.

**Tips.**
- Read in this order: the operative part, then the charge, then the background paragraph with the dates. That is
  about 2–3 minutes per order once you have a rhythm.
- **Excel turns dates into its own format.** Type a date as text (start with `'`), or edit in LibreOffice with
  text columns, or in VS Code.
- After each sitting, run `python -m src.validation_sample --check`. It lists bad dates, misspelt values and
  protocol reminders, never the pipeline's answers. Fix every ERROR line.
- Don't change the identity columns (`sample_no`, `text_stem` …), and don't add or remove rows.
- Do the two sittings on different days.

### B3. Score the coding
- [x] Done (17 Nov, `hand/validation_official.json`)

```bash
python -m src.score_validation --official
```

This writes:
- `derived/validation_table.md`, which has:
  - the headline: window exact and within a month; category and channel agreement with Cohen's κ; issuer
    agreement;
  - breakdowns by method, era and order type;
  - the confusion matrices;
  - the Gate 1 reading.
- `derived/validation_errors.csv`: every disagreement, with the pipeline's evidence.
- `hand/validation_official.json`: the official figures. They are saved once.

If it refuses, it says why. Either rows are still uncoded, or the sheet has errors that `--check` lists.

### B4. Record the Week 3 checkpoint
- [x] Done (17 Nov: Gate 1 = proceed)

1. Copy the headline table and the Gate 1 line from `derived/validation_table.md` into `results_log.md`.
2. Write the Gate 1 decision in your own words. The runbook's table:
   - The extraction rate is 58.2%, the 50–70% band.
   - With κ > 0.6 for both fields: proceed, and report the drop-out. The drop-out test is already in
     `results_log.md`: Cramér's V 0.34 by order type, 0.30 by year.
   - With κ < 0.6 for either field: the protocol is too vague. The category rules are rewritten through the
     protocol's change log, and the orders recoded.
3. Commit `hand/validation_sample.csv` and `hand/validation_official.json`.

### B5. Fix the systematic errors
- [ ] Skipped (your decision, 17 Nov: time). Errors reported as limitations

1. Open `derived/validation_errors.csv` and sort by `field` and `error`.
2. Look for patterns: three or more disagreements of one kind (a phrasing the regex misreads, a category rule that
   fires wrongly).
3. Hand the patterns to an agent. It fixes the rules, adds a test for each pattern, and gives you the re-run
   commands.

**Don't re-validate on the same 150.** The scorer labels any later scoring "NOT THE VALIDATION". If you need a
second round, a fresh sample has to be drawn (an agent can build that draw).

### B6. Later, in Week 6: intra-coder reliability
- [ ] Skipped (your decision, 17 Nov: time)

Re-code 30 of the 150 orders without looking at your first coding, then report agreement and κ between your two
codings. An agent can build the re-code sheet.

---

## C. Review the code base

- [ ] Infrastructure (`cache`, `llm`), ~30 min
- [ ] Stages 1–3 (listings, PDFs, text), ~1.5 h
- [ ] Stages 4–6 (windows), ~2 h
- [ ] Stages 7–8 (actions, sanctions, parties, regulations), ~2 h
- [ ] Stages 9–10 (fraud category, channel), ~1.5 h
- [ ] Stage 11 and Week 3 (orders, validation), ~1 h
- [ ] Week 4 (universe, matching, sample, prices, ownership, panel), ~2 h

**How.** `CODE_GUIDE.md` has a section per file, with what it does, how it works, its key functions, and what to
check when reviewing. Its opening section suggests a routine for each stage: read, run the tests, watch it on one
order in a scratch folder.

**When.** After B2, so that running stages on example orders cannot show you answers for your coding sample.
Before then, limit yourself to reading code.

**What deserves the most attention:** the "Check when reviewing" items. They are rules that decide something about
your data:
- the window definition;
- what counts as an operative penalty;
- the fraud-category precedence and narrative phrases;
- what ties a detection channel to the start of SEBI's work;
- the matching bands.

Write questions into a notes file (for example `REVIEW_NOTES.md`). An agent can answer them, or fix the code with a
test for each fix.

---

## D. Decisions only you can make

### D1. The unit of analysis for Stage 1
- [x] Decided (17 Nov): **(b) matter**, with (a) order as the robustness check

**Needed before** the Stage 1 variable is built (runbook Week 5, Day 1).

**The problem.** The pipeline has one row per *order*, but the plan's lag is per *fraud*: "for each fraud i … first
regulatory action". SEBI issues one order per noticee in mass matters, so:
- the 12,150 windowed orders hold only 3,394 distinct windows;
- the ISO wave alone is 2,363 orders sharing one window, with order dates from 2018 to 2026, so order-level lags
  there measure the adjudication queue.

**The options:**
- (a) **order**: as now.
- (b) **matter**: orders of one case grouped, with lag = window end → the earliest public action.
- (c) **matter × noticee**.

**Recommendation: (b), with (a) as a robustness check.** It matches the plan's definition, and it stops mass
matters from dominating. It needs a matter-grouping step, which an agent can build in about a day.

### D2. When to freeze the sample
- [x] Decided: froze right after A4

| Option | What happens |
|---|---|
| Freeze right after the match review (A4) — **recommended** | Week 4 stays on schedule. A later window fix from B5 then becomes a documented robustness re-label of fraud years, not a change to the sample. |
| Wait until the validation fixes (B5) are in | The fixes are included, but ownership (A7) and the panel (A8) are delayed until then. |

### D3. Industry for Stage 3's matched controls
- [ ] Decided (by Week 7)

The runbook wants event-study controls matched on industry, size and pre-event return. BSE's list has no industry.

| Option | What it costs |
|---|---|
| (a) No industry matching — **recommended** | None. The main estimate uses market-model abnormal returns, which need no controls; matched controls are a robustness check. Size and pre-event return will exist after A7–A8. |
| (b) Fetch each sample firm's industry from BSE's per-company header | About 1,800 requests, plus a little code. |

### D4. Ownership frequency
- [ ] Decided

| Option | What it costs |
|---|---|
| March quarters only — **recommended (the default)** | Matches the firm-year panel. |
| Every quarter (`--quarterly`) | Four times the requests (~25–35 h). Gives quarterly changes in pledging. |

### D5. The ownership gap before December 2015
- [ ] Decided

| Option | What it means |
|---|---|
| Accept it and report it — **recommended** | Ownership predictors in Stage 2 then cover FY2016–2025 only. About half of the in-period fraud years (FY2010–2015) have none. |
| Search for an older source | No free one was found within the runbook's timebox, and more time may find nothing. |

---

## E. Write the paper

- [x] Data: sources and corpus (drafted 2026-11-07)
- [x] Data: extraction and coding (drafted 2026-11-07)
- [ ] Data: validation (design drafted; results in `derived/validation_table.md` and `results_log.md`, 17 Nov, to write in)
- [ ] Data: sample, matching and coverage (refreshed to the frozen sample 17 Nov; coverage after A8)
- [x] Introduction and framework (drafted 2026-11-07; tighten at the end)
- [ ] Stage 1 results (estimates and tables done 17 Nov; prose to write; re-run with `--ownership` after A7–A8)
- [ ] Stage 3 results (Week 7)
- [ ] Stage 2 results (Week 8; Gate 3, 16 November)
- [ ] Synthesis, policy, conclusion, abstract (Week 9; interim versions drafted 2026-11-07)
- [ ] Revision and final checks (Week 10)

**Where.** `paper/paper.tex`, with `paper/paper.bib`. The tables and figures are generated: after any pipeline change run
`python -m src.paper_tables`, then recompile with `latexmk -pdf paper.tex` in `paper/`. Until the sample is frozen, add
`--sample DIR` with a `build_sample --scratch DIR` dry run, or panel B of the funnel table is left out.

**What changes as results arrive.** `grep -n UPDATE paper/paper.tex` lists every passage to rewrite. Prose numbers are typed
by hand: `derived/paper_numbers.json` and `results_log.md` hold their sources. Results sections do not exist yet; add
them after section 5 as the estimates arrive, and rewrite the discussion from them.

---

## F. Housekeeping

- [ ] **Commit today's changes:** typed docstrings in `src/`, `CODE_GUIDE.md`, `TASKS.md`, and the updated
  `PROJECT_GUIDE.md` and `CLAUDE.md`.
  ```bash
  git add -A && git commit -m "docstrings with typed inputs and outputs; code guide; task list"
  ```
- [ ] **Commit after each task above.** Your decisions live in `hand/` and are committed. `derived/` is not
  committed; it is rebuilt from code.
- [ ] **Keep the tests green** after any change: `python -m pytest tests -q`.
- [ ] **Log every number** in `results_log.md` with its date, script and scope.
- [ ] **Optional but wise:** copy `raw/` (8.7 GB) to an external drive once. Re-scraping it would take days.
