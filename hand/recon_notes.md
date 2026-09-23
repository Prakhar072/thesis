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

# response of type:

<input type='hidden' name='totalpage' value=257/>
<input type='hidden' name='nextDel' value='25'/>
<input type='hidden' name='nextValue' value='3'/>
<div class='pay_now alert-success'>
    For Online Payment of Penalties under sec 11B(2) of SEBI Act, 1992 w.r.t orders passed from September 01, 2020, Click on <a href='https://siportal.sebi.gov.in/intermediary/AOPaymentGateway.html' target='_blank'>PAY NOW</a>
</div>
<div class='main_full top_space'>
    <div class='table-scrollable'>
        <table class='table table-striped table-bordered table-hover bordered fix_table dataTable no-footer' id='sample_1' role='grid' aria-describedby='sample_1_info'>
            <thead>
                <tr role='row'>
                    <th style='width:18%'>Date</th>
                    <th>Title</th>
                </tr>
            </thead>
            <tr role='row' class='odd'>
                <td>Feb 23, 2026</td>
                <td>
                    <a href='https://www.sebi.gov.in/enforcement/orders/feb-2026/enquiry-order-in-respect-of-sai-proficient-research-investment-advisory-proprietor-ms-meeshika-vishwakarma_99913.html' target="_blank" title="Enquiry Order in respect of Sai Proficient Research Investment Advisory Proprietor Ms.  Meeshika Vishwakarma" class='points'>Enquiry Order in respect of Sai Proficient Research Investment Advisory Proprietor Ms.  Meeshika Vishwakarma</a>
                </td>
            </tr>


# (order categories; ssid; smid; total order count):

Orders of SAT; 9; 1; 2447
Orders of Chairperson/Members; 9; 2; 6424
Settlement Orders; 9; 3; 1991
Orders of AO; 9; 6; 12002
Orders of Court; 9; 7; 1118
Orders of Special Courts; 9; 77; 90
Orders of ED / CGM (Quasi-Judicial Authorities); 9; 133; 440
Orders under Regulation 30A of the SEBI (Intermediaries) Regulations, 2008; 9; 138; 46

# ==========================================================================
# Verified listing protocol (2026-09-23, established empirically from WSL)
# Implemented in src/scrape_listings.py. Read this before touching that file.
# ==========================================================================

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

Parsing "every <a> in every table" would add 7 bogus rows per WTM page
(257 pages = 1799 junk rows). Scope to table#sample_1.

## 4. Row shapes to expect

  - Most rows: one <a> to a detail page ending `_<order_id>.html`.
  - Some rows (notably AO): a second <a> labelled "[Corrigendum]" pointing at
    a PDF. It is a supplement to the same order, NOT a separate order.
  - Some rows link straight to a PDF with no numeric id — chiefly SAST
    "Exemption Order"s under WTM. order_id is left null and the row is
    flagged needs_review rather than given an invented id.

## 5. Ordering is date-descending, newest first

New orders therefore enter at page 1 and shift everything down. A long run
that straddles a new publication can see a row twice or miss one; dedup by
order_id plus the row-count reconciliation in the run summary is what catches
this. Re-run to fill gaps — cached pages cost nothing.