# order listing request (when second page is loaded)

curl --url 'https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp' \
  -H 'Accept: */*' \
  -H 'Accept-Language: en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7,hi;q=0.6,gl;q=0.5' \
  -H 'Connection: keep-alive' \
  -H 'Content-type: application/x-www-form-urlencoded' \
  -b 'JSESSIONID=15F0F68029BE338349C83592AD2B5C39; _ga=GA1.3.1510768749.1789903871; _gid=GA1.3.776908183.1789903871; _ga_9HZ5J5Q3K5=GS2.3.s1789986984$o2$g1$t1789987049$j60$l0$h0' \
  -H 'Origin: https://www.sebi.gov.in' \
  -H 'Referer: https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=2&ssid=9&smid=2' \
  -H 'Sec-Fetch-Dest: empty' \
  -H 'Sec-Fetch-Mode: cors' \
  -H 'Sec-Fetch-Site: same-origin' \
  -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36' \
  -H 'sec-ch-ua: "Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "Windows"' \
  --data-raw 'nextValue=2&next=n&search=&fromDate=&toDate=&fromYear=&toYear=&deptId=-1&sid=2&ssid=9&smid=2&ssidhidden=9&intmid=-1&sText=Enforcement&ssText=Orders&smText=Orders of Chairperson/Members&doDirect=2'

# (order categories; ssid; smid; total order count;):

Orders of SAT; 9; 1; 2447;
Orders of Chairperson/Members; 9; 2; 6424;
Settlement Orders; 9; 3; 1991;
Orders of AO; 9; 6; 12002;
Orders of Court; 9; 7; 1118;
Orders of Special Courts; 9; 77; 90;
Orders of ED / CGM (Quasi-Judicial Authorities); 9; 133; 440;
Orders under Regulation 30A of the SEBI (Intermediaries); Regulations, 2008; 9; 138; 46;


## Verified listing protocol (2026-09-23, established empirically from WSL)
# Implemented in src/scrape_listings.py. Read this before touching that file.


## 1. `doDirect` is the page selector, NOT `nextValue`

The browser sends both, but only `doDirect` changes the result, and it is
**0-based**:

    doDirect = page - 1        (page 1 -> doDirect=0)

Evidence:
  - Holding doDirect fixed and varying nextValue 1/2/3 returned byte-identical
    HTML. Varying doDirect returned different pages.
  - doDirect 0,1,2,3,4,5 gave disjoint row sets (0 overlap) in strictly
    descending date order.
  - Row counts reconcile exactly with SEBI's published totals:
        ED/CGM  totalpage=18  -> 17*25 + 15 = 440   (published 440)
        WTM     totalpage=257 -> 256*25 + 24 = 6424 (published 6424)
  - Beyond the last page the table is empty (0 rows), not an error.

`next="n"` must be sent or doDirect is ignored (next="p" or "" snaps back to
page 1). `nextValue` is still sent to mirror the browser.

The response's hidden `nextValue` is NOT a cursor: it is just doDirect+1 echoed
back. Do not drive the loop from it. Drive it from hidden `totalpage`.

## 2. Headers: no cookies needed at all

SEBI sits behind a WAF that returns HTTP 530 "Unauthorized Request Blocked"
for non-browser User-Agents. A plain "Academic research (BITS Pilani)" UA is
blocked outright — this is why the runbook's original cache.py could never
have worked against this endpoint.

Minimal header set that works (verified from a fresh WSL shell, no cookie jar):

    User-Agent:    a browser-shaped string   <- REQUIRED, WAF rejects otherwise
    Content-type:  application/x-www-form-urlencoded
    Origin:        https://www.sebi.gov.in
    Referer:       .../HomeAction.do?doListing=yes&sid=2&ssid=9&smid=<smid>

JSESSIONID, _ga, _gid, sec-ch-ua etc. are all unnecessary. Nothing is
session-stateful, so cache entries stay valid across runs and machines.
Set SEBI_CONTACT=<email> to append a contact address to the UA (politeness).

## 3. Two tables in the response — only one holds orders

    table#sample_1     the 25 order rows
    table.ordersum     seven static "Order-11B-<year>.xlsx" summary
                       spreadsheets, repeated on EVERY WTM page

Parsing "every \<a> in every table" would add 7 bogus rows per WTM page
(257 pages = 1799 junk rows). Scope to table#sample_1.

## 4. Row shapes to expect

  - Most rows: one \<a> to a detail page ending `_<order_id>.html`.
  - Some rows (notably AO): a second \<a> labelled "[Corrigendum]" pointing at
    a PDF. It is a supplement to the same order, NOT a separate order.
  - Some rows link straight to a PDF with no numeric id — chiefly SAST
    "Exemption Order"s under WTM. order_id is left null and the row is
    flagged needs_review rather than given an invented id.

## 5. Ordering is date-descending, newest first

New orders therefore enter at page 1 and shift everything down. A long run
that straddles a new publication can see a row twice or miss one; dedup by
order_id plus the row-count reconciliation in the run summary is what catches
this. Re-run to fill gaps — cached pages cost nothing.

## 6. Important for next session

Two supplementary official sources sharpen the corpus. SEBI publishes downloadable 11B order summary spreadsheets by year for 2014–2022, which give structured metadata for cross-validating the extraction. And SEBI's Annual Report carries its own categorised enforcement tables, including Table 10.5 (Fraudulent and Unfair Trade Practices cases), Table 10.6 (Financial Statements Fraud cases), and age-wise analyses of adjudication and prosecution proceedings in Tables 10.10 and 10.11. The 2025–26 Annual Report records 402 investigations taken up and 338 completed in that year. These tables provide an independent benchmark: if the constructed corpus does not reproduce SEBI's own published counts by year and category, the extraction is wrong


## 7. How orders state the violation period (corpus survey, 2026-09-27)

Survey of all 20,849 order texts, done in place of the Week 1 Day 1 close
reading so Week 2 could start. It tells us what the text *says*, not whether
the reading is right: the ten-order close reading is still worth doing before
the Week 3 blind validation. Implemented in `src/extract_windows.py`.

**The defining sentence puts the dates before the label.** The runbook's
patterns assume "investigation period … from X to Y"; the corpus mostly writes

    SEBI conducted an investigation into the trading activities of certain
    entities in illiquid stock options on BSE for the period April 01, 2014 to
    September 30, 2015 (hereinafter referred to as "investigation period"/"IP").

and afterwards only "during the investigation period" (no dates). So label and
dates are matched anywhere in one sentence, not as an adjacent phrase.

Phrasings that produced a regex v1 window (full corpus, orders):

| Phrasing | Orders | Example |
|---|---|---|
| dates … (hereinafter referred to as "investigation/inspection/examination period") | 5,227 | ISO orders, most post-2012 AO/WTM |
| "conducted/initiated/undertook (an) investigation into … for/during the period X to Y" | 1,936 | pre-2012 orders: *conducted investigation into dealings in the shares of Adani Exports Ltd. (…) for the period from November 27, 2003 to December 23, 2003* |
| investigation/inspection/examination word + "period X to Y", no label | 1,158 | *Inspection of the books … maintained by the broker for the period April 2000 to September 2001 was carried out* |
| dates … ("IP") / ("EP") | 811 | |
| "investigation/inspection/examination period" in the sentence, not defined | 721 | *The inspection period was April 01, 2020 to March 31, 2021* |
| "the period of investigation/inspection was from X to Y" | 416 | |
| relevant / UPSI / review period | 166 | insider trading: *the period of UPSI was …* |

Other label words seen: "patch period", "Patch 1 / Patch 2" (one investigation
in patches), "first/second period", "period under review/consideration",
"exchange examination period", "SMS period".

**Date formats** (orders containing each, anywhere in the text): `April 1, 2014`
20,523 · `April 2014` 13,004 · `01.04.2014` 8,300 · `01/04/2014` 7,110 ·
`1st April 2014` 6,063 · `01-04-2014` 2,136 · two-digit years `01.04.14` ·
`01-Apr-2019` · `July'99` 115 · `3 rd May, 2001` (space in the ordinal) ·
`June, 17, 2005` (comma after the month). Partial ranges: *from February 28 to
April 30, 2002* (130), *from June to December 1996* (134). Month-only ends:
*May 2011 to December 2011* (Taksheel). Financial years: *FY 2014-15 to FY
2019-20* (window-labelled in ~70 orders; FY appears in 3,325).

**Ranges that are not the violation window** (must not be taken as one):

| Phrase | Orders | What it is |
|---|---|---|
| "confirmed vide orders dated X and Y" | 894 | prior orders (Day 4 material) |
| "was kept open from X to Y" | 780 | open-offer period |
| "three months commencing from" / "the scheme commenced from" | ~390 | settlement schemes |
| "letters/summons/e-mails dated X and Y" | 4,299 | correspondence |
| "quarter(s) ended/ending <date>" | 1,182 | financial results, shareholding filings |
| "conducted inspection from 12 to 22 Feb 2012 for the period April 2010 to Feb 2012" | — | the inspection's own dates beside the period covered |
| "July 2021 - October 2022: IA conducted a detailed investigation" | — | when the investigation ran |
| "during the period from 1.4.2000 to 31.3.2004" (funds collected) | — | a generic period, not an investigation period |
| "TCP/IP format", "pre-/post-examination period" | — | look like labels, are not |

**Orders that state no period.** 7,596 orders (36%) have no sentence with a
period keyword and a date; only 113 of them mention an investigation/inspection
period anywhere, and most of those only refer back to it. By title they are
settlement orders (71% of Settlement has no candidate), SAST exemption orders,
SCORES/investor-grievance non-redressal, CIS/deemed-public-issue and
registration matters. Settlement orders typically name the alleged violation
without its dates — expect them to drop out of Stage 1 disproportionately.