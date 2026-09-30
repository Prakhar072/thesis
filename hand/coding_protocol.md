# Coding protocol

**Written:** 2026-09-30 (Week 3, Day 1). It was fixed before the fraud-category
auto-coder (`src/classify_fraud.py`) was written and before the validation sample
was drawn.

**Scope:** it governs every hand-coded field in `hand/validation_sample.csv`, and
any later hand coding.

**Changes:** a change after coding has started is added to the change log at the
end, with its date and reason. Orders coded before a change are not silently
recoded.

**Excluded orders:** the orders quoted as worked examples below are listed in
`hand/protocol_examples.csv` and are excluded from the validation sample.

---

## 1. General rules

1. **Unit.** One listed order, coded from its own text (PDF). Do not import facts
   from other orders in the same matter, from news, or from what you know
   happened later.
2. **Code what the order states or finds, not what a noticee argued.** If the order
   drops a charge, the dropped charge does not define the category. If the order
   only restates an earlier order's findings (a corrigendum, or a confirmation
   of an interim order), code what it restates.
3. **When the text does not support a value**, leave it blank and write why in
   `notes`. Never guess a date.
4. **Record doubt.** Put `?` at the start of `notes` whenever a value was a
   judgement call; those rows are re-read at the Day 5 reconciliation.
5. **Time.** Aim for 2–3 minutes per order: the operative part (ORDER / directions),
   the charge ("the SCN alleged …", "issues for consideration"), and the
   background paragraph with the dates.

---

## 2. Violation window (`violation_start`, `violation_end`, `window_kind`)

**The quantity is one thing:** the suspected violation period, i.e. the period of
business activity SEBI examined.

**Names it goes by:** "investigation period", "examination period", "inspection
period", "relevant period", "patch", "IP". All of them fill the same pair of dates.
So does "SEBI conducted investigation into the trading … during/for the period X
to Y", which is the activity period.

**What it is never:**
- the dates of the probe itself (an on-site inspection visit: "conducted an
  inspection of the broker during February 28, 2005 to March 5, 2005");
- the dates of letters, SCNs, hearings or orders;
- offer, issue or subscription periods;
- the period of a bank or demat statement requested.

**`window_kind`:**
- `stated_period`: the order names the period.
- `conduct_dates`: no period is named, but the order gives when the alleged
  conduct took place (the trades, the non-disclosure, fees collected without
  registration). Code those dates.
- `none`: neither is given. Leave the dates blank.

**Dates:**
- **Format:** `YYYY-MM-DD`. Day-first reading: 05.11.2019 is 5 November 2019.
- **Month only:** the start is the 1st, the end is the last day of the month.
- **Financial year:** FY 2011-12 is 2011-04-01 to 2012-03-31.
- **Year only:** 1 January to 31 December.
- **Several patches:** the earliest start to the latest end; list the patches in `notes`.
- **Open end:** for "from X till date" or "till the date of inspection (date)",
  code the end that is stated, or leave the end blank with a note.

---

## 3. Fraud category (`fraud_category`)

**Exactly one of eight.** Code the category of the *conduct SEBI found or charged*
in this order, not the kind of noticee. A broker charged with synchronised
trading is MARKET_MANIPULATION; a broker charged with misusing client funds is
INTERMEDIARY_MISCONDUCT.

**When more than one category is charged**, code the first in this precedence order:

`INSIDER_TRADING` › `FUND_DIVERSION` › `RPT_TUNNELLING` › `ACCOUNTING_MISSTATEMENT`
› `MARKET_MANIPULATION` › `DISCLOSURE_FAILURE` › `INTERMEDIARY_MISCONDUCT` › `OTHER`

**Why this order:**
- the specific frauds (trading on inside information; taking investors' money
  out of the company) come before the ways they are concealed (misstated
  accounts) or exploited (manipulated prices);
- fraud comes before compliance (disclosure lapses, intermediary rules);
- compliance comes before the residual.

**The PFUTP question.** PFUTP Regulations 3 and 4, and SEBI Act §12A(a)–(c), are
charged for every kind of fraud. They never decide the category on their own:
read the narrative (§3.4, §3.5 and the PFUTP rule after §3.8).

**Definitions are not charges.** Orders recite the definitions section with
the charge: PIT 2(1)(g) "connected person", Companies Act 2(76) "related party",
PFUTP 2(1)(c) "fraud". A definition never adds a category. Code from the
provisions that impose a duty or a prohibition.

### 3.1 INSIDER_TRADING
**Inclusion:**
- trading while in possession of unpublished price-sensitive information (UPSI);
- communicating or procuring UPSI;
- charged under PIT 1992 Reg. 3/3A/4, PIT 2015 Reg. 3(1)–(4)/4, or SEBI Act §12A(d)/(e);
- or a PFUTP charge whose narrative is trading on UPSI.

**Exclusion:** PIT provisions that are *disclosure* or *compliance* duties are
DISCLOSURE_FAILURE. These are: PIT 1992 Reg. 12–13 and the Model Code; PIT 2015
Reg. 3(5)–(6) (the structured digital database), Reg. 7–9, and the Code of
Conduct (trading-window and contra-trade breaches). They apply when the order
does not find trading on UPSI.

**Worked example:** 75894, Adjudication order in the matter of Rupa and Company
Limited, 2023-08-25.
- The order fixes "the UPSI period … from May 01 to May 31, 2021".
- It charges PIT 2015 Reg. 3(1) and 4(1) with SEBI Act §12A(d)/(e), for trading
  in the company's shares while in possession of UPSI.
- **Code:** INSIDER_TRADING.

### 3.2 FUND_DIVERSION
**Inclusion:** *issue money*, i.e. money raised from investors in an IPO, GDR,
rights or preferential issue, is diverted, siphoned, misutilised or routed away
from its stated use, for example:
- IPO, rights or preferential proceeds not used for the objects of the issue;
- GDR proceeds pledged or routed to related entities;
- issue money moved through layered transactions to give allottees an exit.

**Typical charges:** ICDR/DIP provisions on the use of proceeds, with PFUTP whose
narrative is diversion ("diverted", "siphoned", "misutilised", "utilisation of IPO proceeds").

**Exclusions:**
- An ICDR charge about offer-document disclosures or issue procedure *without* any
  diversion finding is DISCLOSURE_FAILURE.
- A broker's misuse, pledging or diversion of its *clients'* funds or securities is
  INTERMEDIARY_MISCONDUCT, not FUND_DIVERSION: the money was never raised from
  investors by an issuer. The same holds for a registrar's (RTA's) misappropriation of
  investors' dividends or shares, and for records falsified to hide either.
- The company's *own* funds (loans, advances, bank borrowings, revenue) diverted or
  siphoned to promoters, related parties or group entities are RPT_TUNNELLING (§3.3),
  not FUND_DIVERSION.
- "Objects of the issue" or "issue proceeds" mentioned without a finding that the
  money was diverted (e.g. a lead manager's wrong figures in an offer document) is
  not a diversion.

**Worked example:** 23691, Order against Pinac Stock Brokers Private Limited in
the matter of IPO of Tijaria Polypipes Limited, 2012-10-31.
- "The preliminary findings of the said investigation, prima facie revealed that
  TPL had diverted substantial part of the IPO proceeds, through layered
  transactions, to certain entities who had allegedly provided an exit to the
  retail allottees … of their shares allotted in the IPO."
- The noticee is a broker, but the conduct is the IPO-proceeds diversion scheme.
- **Code:** FUND_DIVERSION.

### 3.3 RPT_TUNNELLING
**Inclusion:** value moved from a listed company to its insiders:
- transactions between a listed company and its related parties entered into
  without the approvals the law requires, or on terms that transfer value to insiders;
- **tunnelling:** the company's own funds (loans, advances, borrowings, revenue)
  diverted or siphoned to promoters, directors, related parties or group entities,
  whether or not they were disclosed as related parties. Where the order also finds
  the accounts misstated to hide it, precedence puts this first.

Typical charges:
- LODR Reg. 23(1)–(4) (policy, audit-committee and shareholder approval);
- Listing Agreement Clause 49 (VII);
- Companies Act §188;
- PFUTP charges whose narrative is about related-party transactions or the
  diversion of the company's funds to insiders.

**Exclusion:** LODR Reg. 23(9) (the half-yearly *disclosure* of RPTs), when charged
alone, is DISCLOSURE_FAILURE.

**Worked example:** 82086, Adjudication Order in the matter of Niraj Cement
Structurals Limited, 2024-03-06.
- "SEBI examined into the alleged failure of Niraj Cement Structurals Ltd … to
  obtain the prior approval of shareholders for material related party
  transactions ('RPTs') in accordance with … LODR Regulations".
- It is charged under LODR 23(4).
- **Code:** RPT_TUNNELLING.

**Worked example (tunnelling):** 86164, Order in the matter of Rana Sugars Limited,
2024-08-27.
- "the company along with its promoter directors … had devised a scheme to
  divert/siphon off the funds of the company by using certain private limited
  companies indirectly controlled by the Managing Director of RSL and his family
  members. These private companies were not shown as related party …"
- The order also finds "consequent misstatements in the financial statements".
- It is charged under SEBI Act §12A and PFUTP.
- **Code:** RPT_TUNNELLING. The company's own funds went to insiders; the
  misstatement concealed it and comes later in precedence.

### 3.4 ACCOUNTING_MISSTATEMENT
**Inclusion:** false or misleading financial statements, i.e. books of account
manipulated, and revenue, profit, receivables or assets inflated.

**The order must say the accounts were false or misrepresented.** An order that
only examines books of account or financial statements, or asks for a forensic
audit (a broker inspection, a non-cooperation case), is not ACCOUNTING_MISSTATEMENT.
- **Typical conduct:** fictitious sales, purchases or invoices; misstatement in
  annual or quarterly results published to the exchanges.
- **Typical charges:** PFUTP 3/4 (often 4(2)(f), (k), (r)) with LODR Reg. 4, 33 or
  48, whose narrative is about the accounts.

**Worked example:** 99823, Adjudication Order in the matter of Investigation in
the Financial Statements of Oriental Trimex Limited, 2026-02-18.
- "I note that during investigation it was observed that OTL has manipulated its
  books of accounts/ financial statement for the FY18 to FY 20 by booking
  fictitious sales and purchases and thereafter, published and disseminated such
  false and misleading financial statements of the company to the stock exchange."
- **Code:** ACCOUNTING_MISSTATEMENT. The LODR charges are the disclosure channel of
  the same misstatement.

### 3.5 MARKET_MANIPULATION
**Inclusion:** trading that creates a false or misleading appearance of trading or
of the price.
- **Typical conduct:** synchronised, circular, reversal or self trades; trades
  that raise the last traded price; artificial volume; pump-and-dump using tips or
  recommendations; front-running; fictitious trades in illiquid stock options.
- **Typical charges:** PFUTP 3/4 (4(1), 4(2)(a), (b), (e), (g), (q)), SEBI Act
  §12A(a)–(c), whose narrative is about trades, price or volume.

**Worked example:** 39418, Adjudication order in the matter of Kavveri Telecom
Products Limited, 2018-06-29, window 2010-06-01 to 2010-12-31.
- "It was alleged in the SCN that the Noticee 1 – Antala and Noticee 2 – Vishu had
  violated the provisions of Regulation 3(a), (b), (c), (d), 4(1), 4(2)(a), (b),
  (e) and (g) of PFUTP Regulations by executing Synchronised and Reversal trades
  … and thereby contributed in creation of artificial volume and indulged in
  price rise …".
- **Code:** MARKET_MANIPULATION.

### 3.6 DISCLOSURE_FAILURE
**Inclusion:** failure to make, or delay in making, a disclosure the law requires,
with no finding of a fraud above. It covers:
- SAST disclosures (1997 Reg. 6, 7, 8; 2011 Reg. 29, 30, 31);
- the open-offer obligation under SAST 1997 Reg. 10–12 / 2011 Reg. 3–4 (an
  acquisition not disclosed through an offer);
- PIT disclosures and the code of conduct (see §3.1);
- LODR Reg. 30 (material events) and 33 (financial results), and other LODR
  compliance;
- Listing Agreement clauses (35, 36, 41, 49 other than RPT) with SCRA §21;
- ICDR offer-document disclosure lapses without diversion.

**Worked example:** 45046, Adjudication Order in respect of Ms. Preeti Jain in
the matter of Burnpur Cement Limited, 2019-11-22.
- The acquisition "had necessitated disclosure within two working days from the
  date of such acquisition to the Target Company and the stock exchanges … under
  Regulation 29(1) read with Regulation 29(3) of the SEBI (SAST) Regulations, 2011."
- **Code:** DISCLOSURE_FAILURE.

### 3.7 INTERMEDIARY_MISCONDUCT
**Inclusion:** a SEBI-registered intermediary's breach of the rules for its
business, or carrying on such a business without registration, when no fraud
above is found. Intermediaries include:
- stock brokers and sub-brokers;
- depository participants;
- merchant bankers, portfolio managers, investment advisers and research analysts;
- RTAs, debenture trustees, credit-rating agencies, mutual funds, FPIs/FIIs, AIFs and KRAs.

**Typical conduct:**
- misuse or non-segregation of client funds or securities;
- KYC and record-keeping lapses;
- net-worth shortfalls;
- code-of-conduct breaches;
- unregistered investment advice (SEBI Act §12(1) with the IA Regulations).

Fraud on clients by someone giving advice for a fee, registered or not, is here
even when it is charged under PFUTP ("assured returns", advisory fees collected
on false promises).

**Worked example:** 65476, Adjudication Order in the matter of Moneylicious
Securities Private Limited, 2022-11-25.
- "The allegations levelled against the Noticee in the SCN are as follows: A.
  Misuse of client funds in Equity Market and Commodity Market B. Non-segregation
  of client funds C. Incorrect reporting of weekly enhanced supervision data D.
  Non-compliance of net worth requirement …" (Stock Brokers Regulations).
- **Code:** INTERMEDIARY_MISCONDUCT.

### 3.8 OTHER
Anything not above. The main groups:
- **(a) Non-cooperation:** failure to comply with summons or furnish information
  to the investigating authority (SEBI Act §11C(2)/(3), penalty §15A(a)).
- **(b) Investor grievances:** failure to redress investor grievances or obtain
  SCORES authentication (§15C).
- **(c) Unauthorised fund mobilisation:** collective investment schemes without
  registration (CIS Regulations, SEBI Act §12(1B)), and deemed public issues of
  shares or debentures (Companies Act §56/60/67/73 with ICDR/DIP) where no
  diversion finding is made.
- **(d) Listing compliance:** minimum public shareholding (SCRR 19A), delisting and buy-back procedure.
- **(e) No violation found:** exemption orders (e.g. SAST exemptions), revocations
  of earlier directions, and corrigenda or addenda that only correct an earlier
  order's text.

Write the group letter in `notes` (e.g. `OTHER-c`) so the residual can be split later.

**Worked examples:**
- 48699, Satish Vasant Ghone in the matter of Kanchan International Ltd., 2021-01-08:
  "SEBI initiated Adjudication proceedings against the Noticee for violation of the
  provisions of Section … 11C(3) of SEBI Act for not complying with the summons,
  under Section 15 A (a)". **Code:** OTHER (a).
- 25639, Swasti Vinayaka Art and Heritage Corporation Ltd., 2013-10-31: "SEBI …
  observed that Swasti Vinayaka Art and Heritage Corporation Ltd. has failed to
  redress the investor grievances within stipulated time." **Code:** OTHER (b).
- 25029, Sumangal Industries Limited, 2013-07-09: "funds were solicited from the
  public under 'potato purchase scheme(s)'", charged as an unregistered collective
  investment scheme. **Code:** OTHER (c).

### The PFUTP rule (narrative)
When the only charges are PFUTP 3/4 and/or SEBI Act §12A(a)–(c), the category is
decided by what the order says was done:

| Narrative | Category |
|---|---|
| books, accounts, financial statements, fictitious sales / purchases, inflated revenue or profit, receivables, misstatement | ACCOUNTING_MISSTATEMENT |
| IPO / issue / GDR proceeds diverted, siphoned, misutilised, routed or pledged; not used for the objects of the issue | FUND_DIVERSION |
| related-party transactions; the company's own funds diverted or siphoned to promoters, related parties or group entities | RPT_TUNNELLING |
| trading on UPSI (not the name of the Insider Trading Regulations or the Model Code) | INSIDER_TRADING |
| trades, price, volume, synchronised / circular / reversal trades, LTP, artificial volume, tips used to move a price, front-running, bogus long-term capital gains | MARKET_MANIPULATION |
| advice for a fee, advisory fees, assured returns, recommendations sold to clients; misuse of clients' funds or securities at a broker or DP; an RTA's misuse of investors' dividends or shares | INTERMEDIARY_MISCONDUCT |
| a collective investment scheme, or funds mobilised from the public (including their diversion) | OTHER (c) |

**Whose money was diverted decides:** issue money → FUND_DIVERSION; the company's
own money to insiders → RPT_TUNNELLING; clients' or investors' assets held by an
intermediary → INTERMEDIARY_MISCONDUCT; a collective scheme's collections → OTHER (c).

If two apply, the precedence order decides. A category *applies* when the order
finds that conduct, not when it mentions it in passing. If none applies, write
`?` in `notes` and code the closest category.

---

## 4. Primary issuer (`primary_issuer`)

**Code:** the company whose securities the matter is about (shares, debentures,
GDRs, or CIS units), named as in the order, e.g. "Kavveri Telecom Products
Limited". Add "(formerly X)" or "(now Y)" if the order gives it.

**Listed or not:** code it either way. Listing status is established in Week 4.

**`NONE`:** when the matter concerns no company's securities, e.g.:
- options traded on an exchange (Illiquid Stock Options at BSE);
- an intermediary's own compliance;
- unregistered investment advice.

**Several issuers:** if the matter covers several, code the first one named in the
matter title and list the others in `notes`.

---

## 5. Detection channel (`detection_channel`)

**Written 2026-09-30 (Week 3, Day 2)**, before the channel auto-coder
(`src/detect_channel.py`) and before any sample order is coded for this field.

**The quantity is how the matter first came to the notice of SEBI or an exchange,
as the order states it.** It is the input to Stage 1's competing risks and to H4
("automated surveillance is not the dominant first-detection channel").

### Values

There are eight values. The first six are the plan's; INSPECTION was added by user
decision on 2026-09-30.

`SURVEILLANCE` · `COMPLAINT` · `AUDIT` · `MEDIA` · `REFERRAL` · `SELF_DISCLOSURE` · `INSPECTION` · `UNCLEAR`

Each value also carries a finer `detection_detail`, listed in the table. It lets a
robustness check move a sub-type, for example to report H4 without the ISO wave.

### General rules

1. **Code what the order says started SEBI's (or the exchange's) work.** The
   trigger must be tied to the start of the examination, investigation,
   inspection or inquiry: "pursuant to", "based on", "on receipt of", "following",
   "after receipt of", or "SEBI observed … In view of the same, SEBI conducted …".
   A source mentioned elsewhere is not a trigger. Examples:
   - a noticee's submission ("there were no complaints from clients");
   - a police complaint the noticee filed;
   - a news item that is part of the conduct (the planted news in a pump-and-dump).
2. **Never infer the channel from the kind of fraud.** An insider-trading order
   that does not say how SEBI noticed the trades is UNCLEAR, even though insider
   trading is usually caught by surveillance.
3. **The earliest stated source wins.** In "a complaint was received … SEBI
   advised BSE to examine … BSE's report showed …", the channel is COMPLAINT.
4. **What SEBI does after it has noticed a matter is not a trigger.** This covers
   the investigation itself, summons, a forensic auditor SEBI appoints, and
   information obtained from banks or exchanges during the investigation.
5. **Follow-up orders** (non-cooperation with summons, failure to pay a penalty,
   confirmation of an interim order, a corrigendum): code the underlying matter's
   trigger if this order states it, else UNCLEAR.
6. **Do not stretch a marker to avoid UNCLEAR.** The share of UNCLEAR is itself a
   finding about how much the enforcement record discloses.

### The channels

| Value | Inclusion | `detection_detail` |
|---|---|---|
| **SURVEILLANCE** | Market surveillance by SEBI or an exchange: a surveillance alert, "routine market surveillance", surveillance measures. Also, flagged medium, a trading pattern or filing observed by SEBI or an exchange where the order names the pattern but not the mechanism: "SEBI observed large scale reversal of trades", "the scrip witnessed large trading volume and price fall", "BSE reported that a sell-off by the preferential allottees was observed", "SEBI observed from the shareholding pattern filed with BSE that …" | `alert` (alerts, surveillance system, surveillance activity) · `trading_pattern_observed` · `filings_observed` |
| **COMPLAINT** | A complaint from an investor, a client, a shareholder, a creditor or any person; SCORES complaints; a whistle-blower; complaints forwarded by an exchange's investor-grievance cell. The investor-grievance orders (15C, non-redressal) are COMPLAINT: the complaints are how SEBI learnt of it | `complaint` · `scores` · `whistleblower` |
| **AUDIT** | Audit findings that brought the matter to SEBI: a statutory auditor's qualification or resignation (with concerns), or a forensic or special audit commissioned by lenders or the company whose report reached SEBI. Not a forensic audit that SEBI or an exchange ordered after it started looking (rule 4) | `qualification` · `auditor_resignation` · `forensic_audit` |
| **MEDIA** | A published news item, media report, research or short-seller report, or TV programme about the matter that prompted SEBI | `news` · `research_report` |
| **REFERRAL** | A reference, letter or report from another public body, including an inspection it did and referred. Such bodies are MCA, RoC, SFIO, RBI, the Income Tax Department / CBDT, ED, CBI, police / EOW, courts and tribunals, ministries, other regulators (IRDAI, PFRDA, FMC, foreign regulators) and DGGI / GST authorities. Not a stock exchange (see UNCLEAR). The MCA's list of suspected shell companies (2017) is REFERRAL | `mca_roc` · `sfio` · `rbi` · `tax` · `police_ed_cbi` · `court` · `regulator` · `government` |
| **SELF_DISCLOSURE** | The company, its management, or the noticee itself disclosed the irregularity: a confession, the company informing the exchanges of a fraud or misstatement, or an intermediary reporting its own lapse. Not "SEBI suo motu", which means on SEBI's own motion | `company_disclosure` · `confession` · `self_report` |
| **INSPECTION** | A routine, thematic, offsite or limited-purpose inspection of an intermediary (or of a listed company's books) by SEBI or an exchange, when no earlier trigger for the inspection is stated. An inspection by another body that was referred to SEBI is REFERRAL | `sebi_inspection` · `exchange_inspection` · `thematic` |
| **UNCLEAR** | No trigger stated: "SEBI conducted an investigation into the trading in the scrip of X" alone; "SEBI suo motu"; an exchange reference or report whose basis is not stated ("Based on the report received from NSE, SEBI conducted investigation …"; user decision 2026-09-30) | `none` · `exchange_reference` · `suo_motu` |

**If two sources are stated as joint triggers in one sentence** ("based on
complaints and a reference from RBI"), code the first named and write `?` in `notes`.

### Clarifications from the development review (2026-09-30, before any validation coding)

These come from labelling 58 random orders (`hand/detection_channel_devset.csv`):

1. **A trading pattern must be tied to the start.** Code "SEBI conducted
   investigation … as the scrip witnessed …" as SURVEILLANCE, as also "… whose shares
   witnessed abnormal increase …", "In view of the aforesaid unusual price movement
   noticed …, SEBI conducted …", and "Based on the above background [price rise],
   SEBI took up the case". A price movement stated on its own is background, not a
   trigger ("The scrip opened at Rs.73/- and witnessed a steep price fall"), so it
   stays UNCLEAR.
2. **Findings are not triggers.** "During the investigation it was observed …" and
   "Pursuant to the aforementioned examination, SEBI observed …" report results.
3. **SEBI receiving a complaint, reference or report is the matter coming to its
   notice.** "SEBI received two complaints …" is COMPLAINT even where the order never
   says "investigation".
4. **An exchange reference that states what the exchange saw** (a price or volume
   pattern, a change in holding) is SURVEILLANCE, flagged. An exchange reference
   that does not say what it was about stays UNCLEAR (`exchange_reference`).
5. **SCORES orders.** A company's failure to obtain SCORES authentication is
   COMPLAINT only when pending investor complaints are part of the charge; otherwise
   UNCLEAR.
6. **Suo-motu settlement applications.** A consent or settlement application filed
   voluntarily, before proceedings ("apprehending that SEBI would initiate
   enforcement actions"), is SELF_DISCLOSURE (`self_report`). A settlement
   application filed after a notice is not a trigger.
7. **What is enclosed with a notice is not a trigger.** For example: "along with a
   copy of letter dated … of Department of Economic Affairs".
8. **A matter found during another investigation** ("During the course of such
   investigation, it came to SEBI's knowledge …") is UNCLEAR unless the order states
   what started that investigation.
9. **A noticee's admission during the investigation is not SELF_DISCLOSURE.**

### Worked examples

- **SURVEILLANCE (`alert`):** 39468, Adjudication Order in the matter of Nivedita
  Mercantile and Financing Limited, 2018-06-29. "SEBI, on receipt of an alert
  generated from its surveillance system, conducted an examination in the scrip of
  Nivedita Mercantile & Financing Ltd."
- **SURVEILLANCE (`trading_pattern_observed`, flagged):** 69454, Adjudication Order in
  the matter of trading in Illiquid stock option at BSE in respect of Navneet
  Garodia HUF, 2023-03-28. "SEBI observed large scale reversal of trades in stock
  options segment of Bombay Stock Exchange."
- **COMPLAINT:** 83117, Adjudication order in the matter of AGI Greenpac Limited,
  2024-04-30. "SEBI conducted an examination in the matter of AGI Greenpac Limited …
  based on the complaints received from Soneko Marketing (P) Ltd and HNG Industries
  Thozhilalar Nala Sangam."
- **AUDIT (`qualification`):** 76787, Adjudication Order in the matter of Reliance
  Home Finance Limited, 2023-09-12. "From the qualified opinion of the statutory
  auditor, SEBI observed that the proceeds raised from issue of debt NCDs have been
  diverted towards certain body corporates including its group companies …"
- **MEDIA:** 23871, Adjudication Order in respect of Shri Rajendra Janardhan
  Sarangdhar in the matter of Orchid Chemicals and Pharmaceuticals Ltd, 2012-11-29.
  "Following a news article titled 'Ranbaxy to sell balance 13.02% stake in Orchid
  Chem' published in DNA Money on September 18, 2010, SEBI conducted investigation
  into the trading in the shares of M/s Orchid Chemicals …"
- **REFERRAL (`tax`):** 66742, Adjudication order in respect of 16 entities in the
  matter of Twilight Litaka Pharma Limited, 2022-12-29. "SEBI conducted an
  investigation … in the scrip of Twilight Litaka Pharma Limited … pursuant to
  receipt of a reference from Income Tax Department, Pune alleging booking of bogus
  purchases and sales …"
- **REFERRAL (another body's inspection):** 55522, Adjudication order in the matter
  of ISG Traders Limited, 2022-01-24. "SEBI received a letter dated August 21, 2019
  from the office of Registrar of Companies (West Bengal), wherein it is mentioned
  that RoC had conducted an inspection of books of accounts …"
- **SELF_DISCLOSURE (`confession`):** 79496, Order in the matter of Satyam Computers
  Services Limited, 2023-11-30. SEBI investigated "the affairs of Satyam Computer
  Services Ltd. … after receipt of an email from Mr. B Ramalinga Raju, Ex-Chairman
  of SCSL, inter alia, admitting and confessing to the company's balance sheet
  having recorded non-existent bank balances …"
- **INSPECTION:** 28712, Settlement Order in respect to M/s. Indiabulls Securities
  Ltd, 2014-12-19. "SEBI conducted an inspection of the books of accounts and other
  records of M/s. Indiabulls Securities Ltd …"
- **UNCLEAR (`exchange_reference`):** 29459, Adjudication Order against Prasad V
  Potluri and PVP Energy Pvt. Ltd., 2015-03-27. "Based on the report received from
  NSE, SEBI conducted investigation in the scrip of the PVP Ventures Ltd." The basis
  of NSE's report is not stated.

---

## Change log

| Date | Section | Change | Reason | Orders already coded |
|---|---|---|---|---|
| 2026-09-30 | all | first version | — | none |
| 2026-09-30 | 3.2, 3.4, 3.7, 3.8, PFUTP rule | Clarifications. (1) Misuse of clients' funds is intermediary misconduct, not diversion. (2) Accounting misstatement needs a finding that the accounts were false; examining books is not enough. (3) Advisory fraud on clients is intermediary misconduct. (4) OTHER (e): orders that find no violation. (5) PFUTP narrative rows for advisory and collective-investment-scheme cases. | Found in the auto-coder's development review of 68 orders (`hand/fraud_category_devset.csv`, excluded from the validation sample). No inclusion rule was reversed. | none (no validation coding has started) |
| 2026-09-30 | 3 (intro) | Definitions (Reg./Sec. 2) are not charges. | A definition had counted as a charge in about 560 orders. For example, PIT 2(1)(g) competed with PIT 4(1) as a disclosure charge, and in 31627 the definitions alone gave DISCLOSURE_FAILURE. Auto-coder change: 6 categories changed, 106 moved from medium to high confidence, dev-set agreement unchanged (58 of 65). | none (no validation coding has started) |
| 2026-09-30 | 3.2, 3.3, 3.7, PFUTP rule | **User decision:** a listed company's own funds diverted or siphoned to promoters, related parties or group entities are RPT_TUNNELLING. FUND_DIVERSION is issue money only (IPO, GDR, rights, preferential). "Whose money was diverted decides." Clarifications: an RTA's misappropriation of investors' dividends and shares is INTERMEDIARY_MISCONDUCT (like a broker's use of clients' assets); "objects of the issue" without a diversion finding is not diversion; "applies" means the order finds the conduct; the Insider Trading Regulations and the Model Code named by title are not insider trading. New worked example 86164. | §3.2 said "money raised from investors", but its precedence rationale said "taking investors' money out of the company", which left company-funds siphoning (Rana Sugars, RHFL, DB Realty) undefined. A fresh 16-order spot check found 3 clear errors and 2 debatable cases. Auto-coder fraud-v2: 360 orders change category; dev agreement 75 of 81; a second fresh 16 all agree. Dev labels 86164, 56029, 91617, 31458 → RPT_TUNNELLING and 43388 → DISCLOSURE_FAILURE (Model Code contra-trade, §3.1 exclusion). | none (no validation coding has started) |
| 2026-09-30 | 5 | Detection-channel rules written (Week 3 Day 2), before the channel auto-coder and before any order is coded for this field. **User decisions:** INSPECTION added as an eighth channel; a trading pattern or filing that SEBI or an exchange *observed* is SURVEILLANCE, flagged (`trading_pattern_observed`, `filings_observed`); an exchange reference that does not state its basis is UNCLEAR (`exchange_reference`). Ten worked examples added to `hand/protocol_examples.csv`. | the plan's six channels had no place for routine inspections, and the ISO wave (~2,240 orders) states only that SEBI "observed" the reversal trades | none |
| 2026-09-30 | 5 | Clarifications 1–9 ("Clarifications from the development review"): a pattern must be tied to the start; findings are not triggers; SEBI receiving a complaint or reference is enough; an exchange reference that states what the exchange saw is SURVEILLANCE (flagged); SCORES orders are COMPLAINT only with pending complaints; suo-motu settlement applications are SELF_DISCLOSURE; notice enclosures are not triggers; matters found in another investigation are UNCLEAR; admissions during the investigation are not SELF_DISCLOSURE. | Found while labelling the 58-order development set and checking the auto-coder's rule and model outputs. None reverses a §5 inclusion rule or the user's decisions. Orders read while building are excluded from the validation sample (`hand/detection_channel_devset.csv`, `hand/seen_while_building.csv`). | none (no validation coding has started) |
