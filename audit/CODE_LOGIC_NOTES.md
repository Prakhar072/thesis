# Code logic notes

This document covers only the logic that needs explaining. Each rule gets six
points:
1. **Detects:** what the code is trying to detect.
2. **How:** how it detects it.
3. **Why it works:** why the implementation works.
4. **Assumes:** the assumptions it makes.
5. **Succeeds on:** example inputs that pass.
6. **Fails on:** example inputs that fail or are misclassified.

Where a failure was observed in the corpus it is marked **(observed)**, with an
order id.

---

## A. Fetching and identity

### A1. Cache key and validator (`cache.cache_key`, `cache.fetch`, `default_validator`)
1. **Detects:** whether a request has been made before, and whether a response is SEBI's firewall page rather than content.
2. **How:**
   - The key is sha256 of `METHOD\nURL\nsorted-urlencoded(params)\nsorted-urlencoded(form)\nextra`; headers are not part of it.
   - The validator lower-cases the first 4 KB and looks for "unauthorized request blocked" or "unauthorized activity has been detected". A match counts as a transient failure: the response is retried and not cached.
3. **Why it works:** SEBI's listing pages differ only in the form body, so the body must be in the key. Leaving headers out lets cached pages survive User-Agent changes.
4. **Assumes:**
   - A URL's content never changes. True for PDFs; false for listing pages, which shift as orders are published.
   - The WAF always uses one of those two phrases.
5. **Succeeds on:** POST page 3 and POST page 4 of WTM get different keys. A 530 WAF page is retried and never stored.
6. **Fails on:**
   - A WAF page worded differently, an HTML "404 Not Found" page served with HTTP 200, or an empty 200 body: all are **cached permanently**.
   - For PDFs, `fetch_pdfs.is_pdf_bytes` catches the first two, but the bad body still sits in the cache, so re-runs never retry it without `--force`. Currently 0 such orders.

### A2. Pagination (`scrape_listings.build_form`, `scrape_category`)
1. **Detects:** every page of a category.
2. **How:**
   - `doDirect = page-1`, `next=n`; `nextValue` is sent but ignored by SEBI.
   - The loop stops at the hidden `totalpage` (the leading digits of an unquoted value).
3. **Why it works:** verified by exact count reconciliation (WTM 256×25+24 = 6,424; the current files match all four published counts).
4. **Assumes:** listings do not change during the walk. New orders are prepended, so a row can move from page *k* to page *k+1* while you are between them.
5. **Succeeds on:** a 30-minute scrape with no publications in between.
6. **Fails on:** a publication mid-scrape duplicates one row (removed by de-duplication) and **skips** none; a deletion mid-scrape would skip one row. Neither would show except as a count mismatch.

### A3. Listing de-duplication (`scrape_listings.deduplicate`)
1. **Detects:** the same order listed twice.
2. **How:** the key is `order_id`, else `source_url`. The first row seen is kept; it is replaced only if it `needs_review` and the duplicate does not. Differing fields go to `listing_conflicts.csv`.
3. **Why it works:** `order_id` is SEBI's own unique id.
4. **Assumes:** the same `order_id` means the same order, and category order (AO, ED_CGM, SETTLEMENT, WTM, sorted by name) decides which category wins.
5. **Succeeds on:** the page-shift duplicates.
6. **Fails on:**
   - **Different** ids with the **same** document. 70 PDFs are shared by 145 orders, and 215 orders have text identical to another's. These are not duplicates by id, so all are kept (see AUDIT P-2).

### A4. Which document is the order (`fetch_pdfs.extract_document_ref`)
1. **Detects:** the order document on a detail page.
2. **How:** the first match wins:
   - an `<iframe|embed|object>` whose `src`/`data`, unwrapped from `/web/?file=`, ends in `.pdf`;
   - else inline text of at least 1,500 characters in `section.main_section`;
   - else the first `<a href>` ending in `.pdf`.
3. **Why it works:** the corpus survey found the iframe viewer on modern pages and inline text on 2002–2011 pages. Putting anchors last fixed 32 annexures that had been archived as orders.
4. **Assumes:** the viewer holds the order, not an annexure, and an inline order is at least 1,500 characters.
5. **Succeeds on:** `/web/?file=/sebi_data/attachdocs/feb-2020/1582887604869_2.pdf`.
6. **Fails on:**
   - An inline order shorter than 1,500 characters with no PDF gives NO_DOCUMENT (**observed**: 15272, 722 characters).
   - An iframe URL without `.pdf` (a `.doc`, or a query string) is skipped (**observed**: 31687).

---

## B. Text

### B1. Page classification (`extract_text.classify_page`)
1. **Detects:** whether a page's text layer can be used, or the page needs OCR.
2. **How:** for each page:
   - `native_chars` = non-space characters.
   - `quality` = share of those not in the Unicode categories Cc/Cs/Co/Cn and not U+FFFD.
   - `image_cover` = the sum of image bounding-box areas over the page area, capped at 1.
   - Rules, in order:
     - ≥20 characters and quality < 0.75 → BROKEN_LAYER;
     - ≥100 characters → NATIVE;
     - image cover ≥ 0.5 → SCANNED;
     - ≥300 vector paths → VECTOR_TEXT;
     - otherwise SPARSE.
3. **Why it works:** broken Ghostscript layers are control codes (quality near 0); scans have no text but a page-sized image; outlined text has hundreds of paths.
4. **Assumes:** a page with 100 good characters has *all* its text in the text layer.
5. **Succeeds on:** digital orders, scanned orders, and the Ghostscript 7.06 control-code layers.
6. **Fails on:**
   - A digital page with a pasted **image** of a table (noticee lists, penalty tables) is NATIVE, so the image is never OCR'd and its content is lost. **Observed** indirectly: WTM orders whose "In respect of:" is followed by nothing ("In respect of: – ____ The aforesaid entities…", 44442, 45500).
   - Overlapping images inflate `image_cover` (the sum is capped at 1, not unioned), so a sparse page could be sent to OCR. That is harmless.

### B2. OCR retry (`extract_text.ocr_page`)
1. **Detects:** badly OCR'd pages, usually rotated scans or 1-bit scans.
2. **How:** first pass upright at 300 dpi. If mean word confidence is under 70, it tries rotations {0, 90, 180, 270} × dpi {300, 200} and keeps the highest confidence. Both passes are cached separately.
3. **Why it works:** tesseract `--psm 3` does not auto-rotate. Mean confidence is a usable proxy for readability.
4. **Assumes:** higher confidence means correct text.
5. **Succeeds on:** the 182 rotated pages.
6. **Fails on:** a page that is confidently wrong (e.g. dense tables read as columns) is kept. 7 documents still have a mean confidence under 70.

### B3. Header/footer removal (`extract_windows.strip_repeated_lines`)
1. **Detects:** running headers and footers.
2. **How:**
   - A line's key is lower-case, whitespace collapsed, digits → `#`.
   - Keys found among the first or last 5 non-blank lines of at least max(3, half of) the pages are blanked with spaces. The key needs at least 4 non-`#` characters.
3. **Why it works:** headers repeat at the page edges; masking digits unifies "Page 3 of 11".
4. **Assumes:** at least 3 pages; content does not repeat at page edges.
5. **Succeeds on:** "Adjudication Order in the matter of X Page # of #".
6. **Fails on:**
   - A genuine content line repeated at the top of most pages (e.g. a table header of a multi-page noticee table) is blanked. That is harmless for dates but can remove a table's column header.
   - It does nothing for inline HTML orders (no page breaks).

### B4. Sentence splitting and the 2,000-character re-split (`extract_windows.split_sentences`, `_resplit_long`)
1. **Detects:** sentence boundaries.
2. **How:**
   - A boundary is `[.?!]` plus any closing quote or bracket, followed by whitespace and then a capital, digit, quote or bracket.
   - The split is skipped when the token before the full stop is in `ABBREVIATIONS` (ltd, no, rs, m/s, mr, …) or is dotted initials.
   - A sentence over 2,000 characters is cut at blank lines, then at line breaks, into pieces of about 2,000 characters.
3. **Why it works:** legal sentences end with ". " + capital; abbreviations cover SEBI's usage. The re-split stops a flattened table from becoming one 20,000-character "sentence".
4. **Assumes:** anything that must be read together fits in 2,000 characters.
5. **Succeeds on:** "…Rs. 5,00,000/- (Rupees Five Lakh only) on the Noticee. The penalty shall be paid…".
6. **Fails on (observed, 46128):** "I … hereby impose the following penalty on the Noticees – [table]" is 2,454 characters.
   - It is cut at a blank line into "…Penalty (Rs.)" and "Noticee 1 (Aksh …) … Rs. 10,00,00,000 …".
   - Every rule that needs two things in *one* sentence then fails, and the penalty is lost (AUDIT C-1).
   - The same can cut a window range from its label, or a provision list from its instrument (not measured).

### B5. Text identity (`text_stem`)
1. **Detects:** a stable per-order key across stages.
2. **How:** `str(order_id)`; for id-less rows, `noid_` + the first 12 hex digits of sha256(source_url).
3. **Why it works:** ids are unique; URLs are unique among id-less rows.
4. **Assumes:** the source URL of an id-less row never changes.
5. **Succeeds on:** every current row (20,861 unique).
6. **Fails on:** SEBI re-hosting an id-less PDF under a new URL creates a new stem.

---

## C. Dates and windows

### C1. The date grammar (`extract_windows.DATE`, `parse_date`, `_read_date`)
1. **Detects:** dates in any of the forms orders use.
2. **How:** alternatives, longest first:
   - `01-Apr-2019`;
   - `1st April, 2019` / `1 of April 2019`;
   - `April 1, 2019`;
   - `01.04.2019` / `01/04/19` (numeric, **day-first**);
   - `2019-04-01`;
   - `April 2019`;
   - `April'19`.
   - Partial forms (`1 April`, `April 1`, `April`) are only used as range starts.
   - `parse_date` re-reads a matched token with anchored patterns and checks the calendar. 2-digit years: ≥80 → 19xx, else 20xx. Years must fall in 1980–2030.
3. **Why it works:** Indian orders write day-first. The grammar never fills a missing day or year from "today" (the dateutil trap).
4. **Assumes:** numeric dates are D/M/Y; a 2-digit year ≥ 80 means the 1900s.
5. **Succeeds on:** "05.11.2019" → 5 Nov 2019; "Sept 30, 2015"; "31st March, 2012"; "April'14".
6. **Fails on:**
   - US-style "11/25/2019" is rejected (month 25); "03/04/2019" meant as 4 March is read as 3 April.
   - "2014-15" alone is not a date (it is a financial year, handled by C3).
   - A year typo "20014" is rejected.
   - OCR "O1.04.2O14" (letter O) is not a date.

### C2. Date ranges (`RANGE`, `parse_range`)
1. **Detects:** "from X to Y", "between X and Y", "X – Y", "period X to Y", "X till the date of inspection i.e. Y".
2. **How:**
   - An optional preamble (`from|between|beginning|starting|commencing|period (of)? [-:,]|period i.e.`), then date *a* (full or partial), an optional short parenthetical, a connector (`to and including|to|till|until|upto|up to|through|and|–|~`), an optional "the date of inspection, i.e.", then date *b* (full).
   - `parse_range` rejects a connector of "and" unless the preamble is "between" or "period" (so "letters dated X and Y" is not a range).
   - A start without a year takes the end's year, or the year before.
   - It rejects start > end, and 29 Feb in an inferred non-leap year.
3. **Why it works:** it covers the phrasings found in the corpus survey (`hand/recon_notes.md` §7).
4. **Assumes:** the end has a year; the start comes before the end in the text.
5. **Succeeds on:**
   - "from April 01, 2014 to September 30, 2015";
   - "between 1.4.2000 and 31.3.2004";
   - "from February 28 to April 30, 2002" (year inferred);
   - "May-June 1998".
6. **Fails on:**
   - "from 2008 to 2013" (bare years) is not read by regex; the model path reads bare years.
   - "for the quarters ended June 2012 and September 2012" is not a range, correctly.
   - "commencing 1.4.2010 for a period of 3 years" has no range and is missed.
   - A range written end-first is rejected.

### C3. Financial years (`FY_RANGE`, `parse_fy`, `extend_with_fy_list`)
1. **Detects:** "FY 2011-12", "financial year 2011-2012", "FY 2011-12 to 2013-14", and lists before a range.
2. **How:**
   - The second year must be the first + 1; "1999-00" is read as 1999–2000.
   - The window is 1 Apr of the first year to 31 Mar of the year after the last.
   - `extend_with_fy_list` moves a range's start back to the earliest financial year listed between "period" and the range ("the period 2000-2001, 2001-2002 and from April 2002 till …"). It does so only if nothing but fillers sits between them.
3. **Why it works:** that is Indian fiscal-year convention.
4. **Assumes:** "FY" means April–March. True for Indian companies, but a few listed firms use calendar years.
5. **Succeeds on:** "FY 2011-12" → 2011-04-01 to 2012-03-31.
6. **Fails on:** "FY 2011-13" (not consecutive) is rejected; "the year 2012-13" without "F.Y." is read only by the model's `_FY`.

### C4. Candidate scoring and the label hierarchy (`LABELS`, `score_sentence`, `extract_candidates`)
1. **Detects:** which sentence states the window.
2. **How:**
   - Only sentences passing `_PREFILTER` (period / investigat / examin / inspect / surveillance / IP / FY) with at least one date or range are considered.
   - The strongest label present sets the score: DEFINED 8 ("hereinafter … 'Investigation Period'", "('IP')"), STRONG 6 ("investigation period", "period of examination", "conducted investigation … for the period"), RELEVANT 4 ("relevant/patch/UPSI/review period"), CONTEXT 3 (an investigation word), GENERIC 1 ("period").
   - Additions: +3 if a range was found; −3 for negative cues (open offer, "letter dated", hearing …) unless the label is DEFINED or STRONG; +1 if the sentence is in the first 25% of the text.
   - The top 3 sentences are kept. The chosen window is the first `high` candidate, else the first `medium`.
3. **Why it works:** a defining sentence names the period and gives the dates together; later sentences repeat only the label.
4. **Assumes:** the window sentence scores in the top 3.
5. **Succeeds on:** 46128's first sentence (score 12, DEFINED).
6. **Fails on:**
   - An order whose defining sentence has no "period" or investigation word ("SEBI observed that between X and Y the noticee traded…") becomes GENERIC or has no candidate at all, and is left to the model or lost.
   - 7,621 orders have NO_CANDIDATES and never reach the model.

### C5. Choosing among several ranges, and confidence (`pick_window`, `confidence_for`)
1. **Detects:** which range in a multi-range sentence is the window, and how sure the pipeline is.
2. **How:**
   - "patch", "first/second period" or "Period-I/II" → the union of all ranges.
   - Otherwise it prefers ranges introduced by "period" (within 40 characters), then the one nearest the label (ties go to the one before it).
   - Confidence is:
     - `high` for a DEFINED/STRONG label with one clear `range`;
     - `medium` for weaker labels, inferred years, financial years or unclear choices;
     - None for GENERIC labels, and for CONTEXT labels unless the range is introduced by "period".
3. **Why it works:** SEBI writes "for the period X to Y (hereinafter 'IP')". The inspection's own dates sit near "conducted inspection from … to …" and are removed first (C6).
4. **Assumes:** patches are consecutive parts of one period.
5. **Succeeds on:** "Patch 1: 1.1.2010–31.3.2010, Patch 2: 1.6.2010–30.9.2010" → 1 Jan–30 Sep 2010.
6. **Fails on:**
   - Two unrelated periods in one sentence (a pre-window and a window) with a "patch" word become one union.
   - A table of periods for different scrips resolves to the nearest one.

### C6. Inspection visits are not windows (`inspection_visit`)
1. **Detects:** the dates SEBI's inspectors were on site, as opposed to the period inspected.
2. **How:**
   - The last "inspect…" (or "examination of the books") within 700 characters before the range, or an inspection word within 80 characters after it, or a gap made only of "period (was|is|of|i.e.|between|from|:)".
   - No activity words between the inspection word and the dates ("observed", "traded", "dealings in", "shares of", …).
   - Then: ≤31 days and (a "conducted/carried out/undertaken/held" verb before it, or labelled) → a visit; or ≤100 days with a "conducted" verb and no "for/covering/pertaining" → a visit.
3. **Why it works:** in the corpus every inspection window of a month or less was a visit; covered periods run months or years.
4. **Assumes:** visits are short; covered periods are long.
5. **Succeeds on:** "SEBI conducted an inspection of the Company during the period February 28, 2005 to March 5, 2005" is rejected, and the covered period elsewhere is kept.
6. **Fails on:**
   - A genuinely short covered period ("inspection covering 1–20 March 2012") with a "conducted" verb is wrongly rejected.
   - A long visit ("conducted inspection from January to June 2004", 182 days) is kept as a window.

### C7. Reading the model's answer (`window_model.read_answer`, `quoted_in`, `resolve_date`, `false_window_reason`)
1. **Detects:** whether the model's quoted start and end are real, readable dates in the sentence, and not a known non-window.
2. **How:**
   - `quoted_in`: after lower-casing, removing non-alphanumerics and dropping leading zeros, is the quote a *substring* of the sentence? If the named sentence fails, any other candidate sentence containing both quotes is used.
   - `resolve_date`, in order:
     - a full date;
     - a day span "January 21-25, 2008";
     - a year span "2003-05" (a span of more than 1 year; "2011-12" is a financial year);
     - any ranges, dates or financial years inside the quote (the start from the first, the end from the last);
     - bare years only if nothing else is in the quote;
     - a partial date taking the end's year.
   - Rejects:
     - open-ended quotes ("till March 2014");
     - start > end;
     - dates right after "dated" (correspondence);
     - `inspection_visit`;
     - statement or ledger periods;
     - trade-log rows (a time of day follows the date).
3. **Why it works:** the model never supplies a date itself; every date is parsed by the same day-first grammar.
4. **Assumes:** a substring match is the quote's location, and the model chose the right range of several.
5. **Succeeds on:** a model quote "April 01 ,2014" against the sentence "April 1, 2014".
6. **Fails on:**
   - A quote such as "2014" matches *anywhere* the digits 2014 occur (substring on alphanumerics).
   - If `_locate` cannot place the quote (punctuation differences), `false_window_reason` returns None, i.e. **no rejection rule is applied**.
   - Wrong-range choices remain: 42–45 of 50 were judged right.

### C8. Sanity rules and review (`check_windows.flags_for`, `earliest_end`, `check`)
1. **Detects:** impossible or unusual windows.
2. **How:**
   - The rules:
     - R1: end after the order date;
     - R2: start after end;
     - R3: lag ≤ 0 or ≥ 20 years;
     - R4: start before 1995.
   - A month-precision end is compared as the 1st of its month (so "to March 2023" in an order of 1 Mar 2023 passes).
   - A flagged window's dates are nulled unless `hand/window_review.csv` says keep or fix. `fix` stores the hand dates with precision `day`, method `hand` and confidence `high`.
3. **Why it works:** it catches parser slips (day/month inversion, citation dates).
4. **Assumes:** a window cannot end after the order.
5. **Succeeds on:** 43 flags; the user reviewed all of them.
6. **Fails on:**
   - A wrong but plausible window (e.g. the date of a precedent's investigation, 2 years before the order) passes.
   - Window *length* is not checked: 57 one-day windows and 42 over 10 years pass.

---

## D. Prior actions

### D1. SCN / interim / confirmatory dates (`PRIOR_PATTERNS`, `_GAP`, `find_prior_mentions`)
1. **Detects:** the date of the order's earlier show-cause notice, interim / ex-parte / ad-interim order, or confirmatory order.
2. **How:**
   - A keyword (`show cause notice`, `SCN`, `interim order`, `ex-parte ad-interim order`, `confirmatory order`), then a "tempered gap" of up to 110 characters. The gap contains no full stop or semicolon, and none of ~40 words that attach a date to something else (served, reply, letter, vide, received, hearing, disposed, final, appeal, SAT, court, orders, resolution, MOU, circular, report, …).
   - Then "dated", or "was issued on", or "passed on", then a date list ("dated X and Y").
   - "No." and "Ref." lose their dot first, so a reference number does not end the gap.
3. **Why it works:** SEBI writes "Show Cause Notice No. EAD/… dated October 6, 2017". The negative gap words block "SCN was served … by email dated".
4. **Assumes:** the first "dated" after the keyword belongs to the notice.
5. **Succeeds on:**
   - "Show Cause Notice (hereinafter 'SCN') bearing ref no EAD/… dated October 6, 2017";
   - "Interim order was passed by SEBI on August 20, 2015".
6. **Fails on:**
   - "SCN issued to the company in the matter of X dated …": "matter" is not a gap word, so the date may be another document's.
   - An SCN recited from *another* matter or noticee ("similar SCNs were issued to other entities on …") is picked up if plausible (D2).

### D2. Plausibility and "earliest" (`plausibility`, `summarise_order`)
1. **Detects:** dates that cannot be this matter's action.
2. **How:**
   - Reject dates before 1992-01-30 (the SEBI Act), after the order date, or before the window start.
   - `prior_action_date` = the earliest remaining SCN/interim/confirmatory date (ties broken by text position).
   - `first_action_date = min(prior_action_date, order_date)`.
3. **Why it works:** an action cannot precede the conduct, or follow its own closing order.
4. **Assumes:** the earliest plausible recited date is *this* case's first action.
5. **Succeeds on:** ISO orders citing the 20 Aug 2015 interim order.
6. **Fails on:**
   - Without a window (8,707 orders) the lower bound is only 1992.
   - A cited precedent's SCN dated after the window start is accepted.
   - A month-only date resolves to the 1st of the month (biased up to 30 days early).
   - A matter-level interim order (ISO: 59 entities) is used as the entity's first action; that is by design, but a Week 5 decision.

---

## E. Sanctions and money

### E1. The rupee grammar (`amounts.AMOUNT`, `_FIGURE`, `parse_figure`)
1. **Detects:** rupee amounts.
2. **How:**
   - A currency mark (`Rs.|Re.|INR|₹|`|Rupees`), an optional dot, then a figure: `\d{1,3}(?:(?:,\s?|\.)\d{2,3})*(?:\.\d{1,2})?` **or** `\d+(?:\.\d+)?`.
   - Then optional "/-" and an optional unit (crore, cr, lakh, lac, million, thousand).
   - `parse_figure`:
     - drops paise;
     - reads a dot between digit groups as a comma typo;
     - ignores a unit after a grouped figure of at least ₹1 lakh ("10,00,000 lakhs");
     - applies a unit otherwise ("12.82 lakh").
3. **Why it works:** it covers Indian 2-2-3 grouping, western grouping and unit forms.
4. **Assumes:** a figure is grouped, or matched whole by the second alternative.
5. **Succeeds on:** "₹ 5,00,000/-", "Rs. 26,00,00,000", "Rs. 765 crore", "INR 1,20,000".
6. **Fails on (observed):**
   - "Rs. 500000" → **₹500**. Regex alternation is ordered: the first alternative matches the first 1–3 digits, and the match succeeds without trying `\d+`, because the only lookahead is "not a letter".
   - So every ungrouped figure of 4+ digits is truncated: 54 amounts in 45 orders (10087, 10125, 1211, …). The words in brackets are then not attached (they are not adjacent), so the words check cannot catch it.

### E2. Amount in words (`words_to_number`, `_with_words`)
1. **Detects:** the "(Rupees Five Lakh only)" reading, to check the figure.
2. **How:**
   - Tokens are split on spaces, commas and hyphens.
   - Ones and tens accumulate; "hundred" multiplies.
   - A scale word *larger* than the previous one multiplies the running total ("Seven Thousand … Crore"); a smaller one adds.
   - A repeated scale is skipped ("Lakh lakhs").
   - The words win only if the figure is malformed ("2,00,00") or reads under 100 while the words read at least 100.
3. **Why it works:** Indian number words follow crore > lakh > thousand > hundred.
4. **Assumes:** the words sit right after the figure (whitespace, "/-", "(").
5. **Succeeds on:** "Rupees Seven Thousand Two Hundred Sixty Nine Crore Forty Nine Lakhs …" = ₹7,269.5 crore.
6. **Fails on:** "Rupees Five Lakh Fifty" (ambiguous) reads as 500,050; any non-number word ("Rupees Five Lakh as settlement") gives None, and the figure is kept.

### E3. Imposing sentences and the operative part (`_IMPOSE`, `_NOT_OPERATIVE`, `OPERATIVE_FROM`, `find_sanctions`)
1. **Detects:** sentences in which *this* order imposes a penalty.
2. **How:**
   - The sentence starts at or after 40% of the text.
   - It matches `hereby impose` / `I impose` / `impose a penalty` / `penalty … is hereby imposed` / `shall pay a penalty of` / `liable to pay a penalty of`. OCR "tmpose" and "|" for "I" are accepted.
   - It does not match statute or show-cause language ("not be less than", "may extend to", "why … should not", "proposed", "had been imposed", "vide order dated", "in the case of"…).
3. **Why it works:** SEBI's operative paragraph uses "hereby impose"; statute quotes and earlier sanctions use the excluded phrasings.
4. **Assumes:** the operative part is in the last 60% of the text; the amounts are in the same sentence as the imposing words.
5. **Succeeds on:** "I hereby impose a penalty of ₹ 5,00,000/- (Rupees Five Lakh only) on the Noticee under section 15HA".
6. **Fails on:**
   - A table split from its clause (B4; **observed**: 28 orders with no penalty, 50 with part of the table ignored).
   - An operative paragraph before 40% (5 orders).
   - "The Noticee is directed to pay ₹X as penalty" (no "impose" / "penalty of" pattern) is missed.

### E4. Is the amount the sanction? (`_is_sanction_amount`)
1. **Detects:** other money in an imposing sentence (a refund, a misutilisation, gains).
2. **How:**
   - For penalties, the amount must come after the imposing words.
   - The nearest money word before it (within 90 characters) must be the sanction word ("penalty"; for disgorgement "disgorge / gains / profits"; for settlement "settle / remit / paid / pay / sum").
   - No `_OTHER_MONEY` word may sit between that word and the amount, and the amount must not be followed by "is to be refunded / raised / diverted".
3. **Why it works:** it separates "a penalty of ₹X" from "the misutilised amount of ₹Y".
4. **Assumes:** the sanction word precedes its amount within 90 characters.
5. **Succeeds on:** "impose a penalty of Rs. 5 lakh … the refund of Rs. 49,100 is to be made", which keeps only the 5 lakh.
6. **Fails on:** a table whose header "Penalty (Rs.)" is more than 90 characters before a row's amount. It is still accepted for penalties (`last_other < 0`), so any money-looking figure in the table counts: interest, the amount involved.

### E5. The restatement rule (`penalty_rows`)
1. **Detects:** the same penalty stated twice (quantum discussion, then the ORDER paragraph).
2. **How:**
   - Sentences are processed in order, and each sentence's non-total amounts form a group.
   - A sentence is dropped as a restatement if **every** amount in it matches an already-counted (amount, party) pair. The party is `who_text` with non-alphanumerics removed, and a generic party ("the Noticee", or no `who_text`) matches any party.
   - Totals are never rows.
3. **Why it works:** restatements repeat the same figures for the same party.
4. **Assumes:** two equal amounts on generically-named parties in different sentences are the same penalty.
5. **Succeeds on:** "I impose ₹35,00,000 …" in the quantum paragraph and again in the ORDER.
6. **Fails on (observed, 14949):**
   - "I hereby impose ₹2,00,000 … for Reg 7 SAST & ₹2,00,000 … for PIT 13", followed by "Further, I impose a penalty of ₹2,00,000 … under Section 15HA". The third ₹2 lakh is dropped as a restatement, giving `penalty_total` ₹4 lakh against ₹6 lakh.
   - The stated "consolidated penalty of ₹6,00,000" is ignored, because a stated total is only used when "each" appears (`summarise_order`).
   - 50 of 750 orders with a stated total disagree with the computed total.

### E6. Settlement and disgorgement totals (`summarise_order`)
1. **Detects:** the settlement amount paid, and the disgorgement ordered.
2. **How:**
   - Settlement: any sentence of a SETTLEMENT order (any position) with a "settle" word and a pay word, minus "scheme" sentences. Every amount passing E4 counts.
   - The total is the sum over distinct (sentence, amount) pairs.
3. **Why it works:** it finds the amount wherever the order states it.
4. **Assumes:** each sentence states a different payment.
5. **Succeeds on:** a one-sentence settlement order.
6. **Fails on (observed):** modern settlement orders state the same amount three times (IC recommendation, HPAC recommendation, remittance). Each is a distinct sentence, so it is **added each time**.
   - 100354 records ₹1.85 crore for a ₹92.6 lakh settlement.
   - The median `settlement_amount` is 2× the largest single amount; 825 orders state one amount and still record 2× or more (AUDIT C-2).
   - The same design affects 27 disgorgement orders.

### E7. No-penalty outcome (`_NO_PENALTY`, `_PLEA_OR_CITED`)
1. **Detects:** an order that explicitly imposes nothing.
2. **How:** in sentences from 50% of the text onwards: "disposed of without…", "no penalty is…", "not a fit case for imposing", "charges … not established", "exonerated", "proceedings … dropped", "matter … disposed of". Pleas and quoted judgments are excluded.
3. **Why it works:** that is the standard AO wording.
4. **Assumes:** such a sentence refers to the whole order.
5. **Succeeds on:** "the adjudication proceedings are disposed of without imposing any penalty".
6. **Fails on:** "charges against Noticee 2 are not established" in an order that penalises Noticee 1. The outcome is still `penalty` (a penalty wins), but if the penalty itself was missed (E3) the order reads `no_penalty`.

---

## F. Regulations

### F1. Instrument resolution (`match_instrument`, `find_aliases`, `regime_of`)
1. **Detects:** which regulation or act a provision list belongs to.
2. **How:** after "of / under / in (the)", try in order:
   - a full title "SEBI (…) Regulations, YYYY", with the regime read from keywords inside the brackets;
   - the Acts (SEBI Act, SCRA, SCRR, Depositories Act, Companies Act, Listing Agreement, the SCRA/Depositories adjudication rules);
   - abbreviations ("PFUTP Regulations", "Takeover Regulations");
   - the order's own aliases ("… (hereinafter referred to as 'the Regulations')");
   - generic "the Act" (→ SEBI Act if not defined otherwise).
3. **Why it works:** the corpus has about 40 instruments under 487 spellings, and keywords identify each.
4. **Assumes:** "the Act" means the SEBI Act unless defined.
5. **Succeeds on:** "Regulations 3 and 4 of the SEBI (Prohibition of Fraudulent and Unfair Trade Practices …) Regulations, 2003".
6. **Fails on:**
   - An instrument not in the catalogue (Sweat Equity, ESOP Guidelines, SEBI circulars) is unresolved.
   - "the Act" in an order that means the Companies Act without defining it is read as the SEBI Act.

### F2. Provision lists (`_CITATION`, `_TOP`, `expand`)
1. **Detects:** "Regulation 3(a), (b), 4(1) and 4(2)(e)" → 3(a), 3(b), 4(1), 4(2)(e).
2. **How:**
   - A keyword, then items separated by comma / and / & / or / read with / to.
   - A top-level item is 1–3 digits with an optional *upper-case* suffix (12A, 15HA, "15 HA", "15-I"). A suffix after a space may not be a stop word (AND, OF, TO, …).
   - A bare "(b)" replaces the deepest level of the previous item.
   - A different keyword family inside the list ("Section 12A … read with Regulation 3") cuts it, and the rest is re-scanned.
3. **Why it works:** case-sensitive suffixes stop "3 and" becoming "3AND"; the family cut stops SEBI Act sections being filed under PFUTP.
4. **Assumes:** a list shares one instrument named after it.
5. **Succeeds on:** "Section 12A(a), (b), (c) of the SEBI Act read with Regulations 3 and 4 of PFUTP".
6. **Fails on:**
   - "Regulations 3 to 5" gives 3 and 5 (4 is lost).
   - "Clause 1.2 of the Code of Conduct" and "Schedule III" are unresolved.
   - A list whose instrument comes in the next sentence is unresolved (215,445 provisions unresolved in total).

### F3. The role of a citation (`role_for`, `_VIOLATION_CUE`, chaining)
1. **Detects:** which citations are the provisions *violated*.
2. **How:**
   - **Fixed roles:** penalty sections (SEBI Act 15A–15HB, SCRA 23A–23H, Depositories Act 19A–19G) → `penalty`. Power or procedure sections (SEBI Act 11, 11A, 11B, 11D, 12(3), 15I, 15J, 15JB, 19, 24, 26, 28A, 28B; SCRA 12A, 23I, 23JA; the adjudication, enquiry and settlement rules; PFUTP 11/12, PIT 11, SAST 1997 44/45, SAST 2011 32, CIS 65/73, Intermediaries 23–38) → `power`.
   - **Otherwise `violation`** if any of these holds:
     - the 250 characters before the citation (since the previous citation) hold a violation cue (violat…, contraven…, breach…, non-complian…, fail…, alleg…, charg…, default…, delay…, lapse…, irregularit…);
     - "was violated / not complied" follows within 40 characters;
     - it is chained by "and / read with / r/w" to a violation citation.
   - **Otherwise `cited`.**
3. **Why it works:** SEBI writes "for the alleged violation of Regulation …".
4. **Assumes:** a violation word within 250 characters refers to this citation.
5. **Succeeds on:** "alleged violation of Section 12A(a)… read with Regulations 3 and 4 of PFUTP" → all violation.
6. **Fails on:**
   - "failed" or "default" in an unrelated clause within 250 characters marks a background citation as violated.
   - Definitions ("fraud as defined in Regulation 2(1)(c)") next to "violation" become violations.
   - Charges written as circulars ("SEBI circular dated …") are not citations at all; 19% of orders have an empty list.

---

## G. Parties

### G1. Header segment (`header_end`, `noticee_segment`)
1. **Detects:** the text that lists the noticees.
2. **How:**
   - The header ends at the first body marker (BACKGROUND / FACTS / Whereas / "1." followed by a sentence starter / "1. X (hereinafter …) filed"), or at 25,000 characters.
   - The segment starts after the first cue ("in respect of", "against", "filed/submitted by", "Noticees:"), or at a table header ("Sr. No. Name of the Noticee").
   - It ends at "In the matter of", "(The aforesaid entities…", "under Section/Regulation", "Date of hearing", "Coram", or a separator line (at most 15,000 characters).
3. **Why it works:** 97% of orders use this layout (`hand/recon_notes.md` §9).
4. **Assumes:** the first cue is the noticee cue.
5. **Succeeds on:** "In respect of: Noticee No. Noticee Name PAN 1 M/s. Aksh …".
6. **Fails on:**
   - "ORDER AGAINST … UNDER REGULATION …" in a preamble that names no one gives a junk segment.
   - "against" inside "complaints against X" in a title-like header.
   - Headers where the list is an image (B1).

### G2. Entry splitting (`split_entries`, `numbered_items`, `PAN_ANCHOR`, `PIN`)
1. **Detects:** individual parties within the segment.
2. **How:** the first that applies:
   - **Numbered:** serial numbers 1, 2, 3 … in order, each written like the first ("1." → "2."). After the first, a number preceded by "No." is not a serial. A prose item (9 or more lower-case words in the first 300 characters) ends the list.
   - **PAN/PIN-anchored:** an entry ends at a PAN, at "PAN: Not Available", or at a 6-digit PIN code.
   - **Comma list:** split before honorifics or after Ltd/Limited/LLP/HUF, but not before role words or cities, and never inside brackets.
   - **Single.**
3. **Why it works:** it covers the three table styles and the address-laden lists.
4. **Assumes:** serials are sequential and PINs end addresses.
5. **Succeeds on:** 46128's table; "X (PAN) address 500 016 Y address (PAN)".
6. **Fails on:**
   - A list numbered (a), (b), … with no PANs is treated as a comma list.
   - "BHAVESH THAKKAR AND BIMAL THAKKAR" (all capitals, no honorifics) stays one party.
   - A company name containing a PIN-like number is split.

### G3. Name cleaning and validity (`clean_entry`, `valid_name`, `name_clean`)
1. **Detects:** the name at the start of an entry.
2. **How:**
   - Leading table headers, serials and codes are stripped.
   - The name is cut at the first PAN / CIN / DIN / registration number, "Not Available", an address start, a role word after a comma, "and its directors", "(since deceased)", "(formerly…)", a page footer, or a second "M/s".
   - It is valid if it starts with a capital, has at least 3 letters and at most 14 words, is not a generic word, contains no sentence word (is/was/order/section …), and is not a SEBI officer.
   - `name_clean` upper-cases, drops honorifics and "(formerly …)", and normalises LTD/PVT.
3. **Why it works:** those tokens reliably end names in SEBI headers.
4. **Assumes:** a name never contains these words.
5. **Succeeds on:** "Kishan Gopal Mohta [PAN: ADQPM0469E] having address at …" → "Kishan Gopal Mohta".
6. **Fails on:**
   - A company named "… Order Securities Ltd" or "… Act Capital" is rejected (sentence word).
   - A lower-case start ("iGate") is rejected.

### G4. Role guess (`guess_role`)
1. **Detects:** issuer, promoter, director, auditor, intermediary or trader.
2. **How:** the first rule that fires, in this order:
   - "Chartered Accountants" in the name → AUDITOR;
   - a SEBI registration number → INTERMEDIARY;
   - a "role" given by the list itself ("and its Directors viz. …");
   - auditor words in the row or window → AUDITOR;
   - a company matching the matter company → ISSUER;
   - "promoter" → PROMOTER;
   - a DIN (individuals only) → DIRECTOR;
   - director / MD / CFO / compliance officer words (individuals only) → DIRECTOR;
   - broker / member / DP / adviser words → INTERMEDIARY;
   - "'the Company'" / "listed on" (companies) → ISSUER;
   - trading words → TRADER;
   - an individual in a matter that says "directors" / "promoters" → that role;
   - a trading matter → TRADER;
   - any other company → ISSUER (`suffix`);
   - otherwise UNKNOWN.

   The window is the party's first mention after the header, from 80 characters before to 250 after, cut at the next party's name.
3. **Why it works:** the runbook's heuristics, made to record their evidence.
4. **Assumes:** role words near a name describe that name.
5. **Succeeds on:** "Dr. Kailash S Chaudhary was the Managing Director" → DIRECTOR (if named in the body).
6. **Fails on:**
   - Parties referred to only as "Noticee 2" in the body get no window, hence UNKNOWN (46128).
   - A trading company that is not the matter issuer becomes ISSUER (`suffix`).
   - A promoter who traded is PROMOTER, not TRADER, by order of the rules.

### G5. Linking penalties to parties (`link_penalties`, `mentions`, `noticee_numbers`)
1. **Detects:** which party each penalty row belongs to.
2. **How:**
   - With one party, every row is theirs.
   - Otherwise, per sentence:
     - it is **prose** if the first row has Day 4 `who_text` ("on X") and a party is named after the first amount; else it is a **table** if a party is named in the 250 characters before the first amount;
     - in a table, each amount takes the last party named since the previous amount, or continues the previous row's party;
     - in prose it takes the first party named before the next amount, and amounts naming no one wait for the next named party (pending).
   - "each" → every party named. "Jointly and severally", or "on all the Noticees" → every party named (or all parties), flagged joint.
   - Parties are matched by name tokens (company suffixes dropped), PAN, defined aliases, "Noticee No. k", and number lists.
3. **Why it works:** those are SEBI's two layouts.
4. **Assumes:** names are distinctive.
5. **Succeeds on:** 54851's table; 24441's prose; 26894's "each".
6. **Fails on:**
   - Overlapping names: "Anish Pravin Bagadia" also matches inside "Anish Pravin Bagadia HUF". With both at the same position, ties go to the later party index, so an amount can go to the wrong one of the two.
   - An amount whose party is named only as "the broker" is unassigned.

---

## H. Assembly

### H1. `build_orders.build`
1. **Detects:** nothing. It joins.
2. **How:** listings get `text_stem` from (`order_id` as int, `source_url`). Documents do too. Text, windows, actions and parties already carry `text_stem`. Each is left-joined with `validate="one_to_one"` and the row count is asserted afterwards. Before each join it raises if the input holds a stem not in the listings.
3. **Why it works:** it avoids pandas' null-matches-null cross-join on `order_id` (76 null ids).
4. **Assumes:** every stage's output is from the current run.
5. **Succeeds on:** the current files: 20,861 rows, 0 missing per join.
6. **Fails on:** a stage output from an older run (fewer or other orders) joins without complaint. Missing orders become NA columns and are only logged. Nothing compares `rules_version` or file timestamps.
