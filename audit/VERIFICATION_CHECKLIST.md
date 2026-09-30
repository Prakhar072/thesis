# Verification checklist

Checks to run before trusting `derived/orders.parquet` for analysis. Every
snippet runs from the repo root with the project venv, reads only `derived/`,
`reports/`, `logs/` and `hand/`, and changes nothing:

```bash
cd ~/projects/thesis && .venv/bin/python     # then paste a block, or save it as a .py file and run with PYTHONPATH=.
```

Each check gives what to run, the value on 2026-09-28, and what to look for.
Checks marked **[A-x]** / **[P-x]** test an issue in `AUDIT_REPORT.md`.

> **After the 2026-09-30 fixes** (C-1, C-2, H-1, H-2, M-3, M-9; see "Status after fixes" in
> `AUDIT_REPORT.md`), the 2026-09-28 values for those checks describe the old outputs. The expected values are
> now in `results_log.md` (2026-09-30). The checks themselves still apply: for example, tiny penalties should
> number about 2, `matter_raw` with appeal text 0, and descriptor-only window rows 0. Tests: 669 passed.

---

## 1. Is this one consistent run?

### 1.1 Tests pass
```bash
.venv/bin/python -m pytest tests -q          # expect: 591 passed
```

### 1.2 Every stage wrote the full order set
```python
import pandas as pd, pathlib
D = pathlib.Path("derived")
for f in ["listings", "order_documents", "text_documents", "windows", "violation_windows",
          "windows_checked", "order_actions", "order_parties", "orders"]:
    print(f"{f:20s}", len(pd.read_parquet(D / f"{f}.parquet", columns=None)))
```
**Now:** 20,861 in every table. Any other number means a partial run
(`--sample`, `--category`, `--limit`) was written into `derived/`.

### 1.3 The stages ran in order, with the current code **[M-7]**
```python
import pandas as pd, pathlib, datetime as dt
D = pathlib.Path("derived")
order = ["listings", "order_documents", "text_documents", "windows", "violation_windows",
         "windows_checked", "order_actions", "order_parties", "order_entities", "orders"]
for f in order:
    print(f"{f:20s}", dt.datetime.fromtimestamp((D / f"{f}.parquet").stat().st_mtime))
for f, col in [("windows", "rules_version"), ("order_actions", "rules_version"), ("order_parties", "rules_version")]:
    print(f, pd.read_parquet(D / f"{f}.parquet", columns=[col])[col].value_counts().to_dict())
print(pathlib.Path("logs/build_orders.log").read_text().splitlines()[-20:])
```
**Look for:**
- modification times in the listed order;
- one `rules_version` each (`regex-v1.1`, `actions-v1`, `parties-v1`);
- "0 listed orders without a row there" for every join in `build_orders.log`.

### 1.4 No stage swallowed an exception **[L-9]**
```python
import pandas as pd
for f, col in [("windows", "error"), ("order_actions", "error"), ("order_parties", "error")]:
    df = pd.read_parquet(f"derived/{f}.parquet", columns=["text_stem", col])
    print(f, int(df[col].notna().sum()))
m = pd.read_parquet("derived/window_model.parquet", columns=["model_status", "model_digest", "from_cache"])
print(m.model_status.value_counts().to_dict(), "digests:", m.model_digest.nunique(), "from cache:", int(m.from_cache.sum()))
```
**Now:**
- 0 errors in each table;
- model statuses: MODEL_WINDOW 1,634, MODEL_NONE 907, MODEL_UNVERIFIED 135, MODEL_REJECTED 36;
- no MODEL_ERROR; 1 digest; 2,712 answers from cache.

**Any MODEL_ERROR [M-6]** means model windows were lost silently.

---

## 2. Corpus counts: fetched, parsed, rejected, inserted

### 2.1 Listings against SEBI's published totals
```python
import pandas as pd
L = pd.read_parquet("derived/listings.parquet")
print(L.order_type.value_counts().to_dict(), "total", len(L))
pg = pd.read_csv("reports/listing_pages.csv")
print(pg.status.value_counts().to_dict())
print(pg.groupby("order_type").agg(pages=("page", "count"), total=("total_pages", "max"), rows=("n_rows", "sum")))
print("conflicts file bytes:", len(open("reports/listing_conflicts.csv").read()))
print("null ids", int(L.order_id.isna().sum()), "dup ids", int(L.order_id.dropna().duplicated().sum()))
```
**Now:**
- AO 12,003, WTM 6,424, Settlement 1,994, ED/CGM 440 (total 20,861);
- 836 pages, all `ok`; conflicts file empty;
- 76 null ids; 0 duplicate ids.

Compare the totals with the counts shown on SEBI's listing pages today; new orders
since 2026-09-23 are not in the corpus.

### 2.2 Documents: what failed
```python
import pandas as pd
O = pd.read_parquet("derived/order_documents.parquet")
print(O.status.value_counts().to_dict())
print(O.doc_shape.value_counts().to_dict())
print(O[~O.status.isin(["OK", "OK_INLINE"])][["order_id", "order_type", "status", "error"]])
```
**Now:**
- OK 19,267; OK_INLINE 1,582;
- NO_DOCUMENT 7, FAILED_DOWNLOAD 4, FAILED_DETAIL 1 (the 12 listed in CLAUDE.md §9).

### 2.3 Archived PDFs are the ones downloaded **[L-2]**
```python
import pandas as pd
from src.extract_text import text_stem
O = pd.read_parquet("derived/order_documents.parquet", dtype_backend="numpy_nullable")
O["text_stem"] = [text_stem(int(i) if pd.notna(i) else None, u) for i, u in zip(O.order_id, O.source_url)]
T = pd.read_parquet("derived/text_documents.parquet", columns=["text_stem", "pdf_sha256"])
m = O.merge(T, on="text_stem", suffixes=("_fetch", "_text"))
both = m.dropna(subset=["pdf_sha256_fetch", "pdf_sha256_text"])
print("mismatches:", int((both.pdf_sha256_fetch != both.pdf_sha256_text).sum()), "of", len(both))
```
**Now:** 0 of 19,267.

### 2.4 Duplicate observations **[P-2]**
```python
import pandas as pd
T = pd.read_parquet("derived/text_documents.parquet")
ok = T[T.status == "OK"]
dup = ok[ok.text_sha256.duplicated(keep=False)]
print("orders with identical text:", len(dup), "groups:", dup.text_sha256.nunique())
O = pd.read_parquet("derived/orders.parquet", columns=["text_stem", "order_type", "order_date", "title", "penalty_total"])
grp = dup[["text_stem", "text_sha256"]].merge(O, on="text_stem").sort_values(["text_sha256", "order_date"])
print(grp.head(20).to_string())
D = pd.read_parquet("derived/order_documents.parquet")
print("orders sharing a PDF:", int(D.pdf_sha256.dropna().duplicated(keep=False).sum()))
```
**Now:** 215 orders in 105 groups; 145 orders share 70 PDFs.

**Decide** for each group whether it is one decision (keep one row) or one
decision per noticee (split the penalty).

Corrigenda and addenda listed as orders **[P-2b]**:
```python
import pandas as pd
O = pd.read_parquet("derived/orders.parquet")
c = O.title.str.contains(r"^\s*(?:corrigendum|addendum|erratum)|\b(?:corrigendum|addendum|erratum)\s+to\b", case=False, na=False)
print(int(c.sum()), O[c].order_type.value_counts().to_dict(), "with window:", int((c & O.violation_start.notna()).sum()))
print(O[c].title.str[:100].head(20).to_string())
```
**Now:** 188 rows (WTM 141). Exclude them from order counts, or link them to the order they amend.

---

## 3. Text quality

### 3.1 Text status and OCR
```python
import pandas as pd
T = pd.read_parquet("derived/text_documents.parquet")
print(T.status.value_counts().to_dict(), T.source.value_counts().to_dict())
ok = T[T.status == "OK"]
print("OCR'd orders:", int((~ok.has_text_layer.fillna(True)).sum()), " mean conf < 70:", int((ok.mean_ocr_conf < 70).sum()))
print("text < 1000 chars:", int((ok.text_chars < 1000).sum()), " stopword ratio < 0.15:", int((ok.stopword_ratio < 0.15).sum()))
```
**Now:**
- OK 20,849; NO_SOURCE 12;
- 708 OCR'd orders; 7 with mean OCR confidence < 70;
- 26 orders under 1,000 characters; 25 with stopword ratio < 0.15.

**Read** the short and low-ratio ones: they are tables, annexures, or a wrong document.

### 3.2 Which pages were OCR'd, and how
```python
import pandas as pd
P = pd.read_parquet("derived/text_pages.parquet")
print(P.page_class.value_counts().to_dict())
print(P.method.value_counts().to_dict())
print(P[P.ocr_conf < 70][["text_stem", "page_no", "page_class", "ocr_conf", "ocr_variant"]].head(30))
```

### 3.3 Is the document really the order? (annexure check)
```python
import pandas as pd
T = pd.read_parquet("derived/text_documents.parquet")
O = pd.read_parquet("derived/orders.parquet", columns=["text_stem", "title"])
s = T[T.status == "OK"].merge(O, on="text_stem")
print(s.nsmallest(20, "text_chars")[["text_stem", "order_type", "text_chars", "stopword_ratio", "title"]].to_string())
```
Open the PDFs of the 20 shortest.

### 3.4 Images on digital pages in orders with no parties **[H-4]**
```python
import pandas as pd
P = pd.read_parquet("derived/text_pages.parquet")
OP = pd.read_parquet("derived/order_parties.parquet", columns=["text_stem", "n_parties", "text_status", "order_type"])
none = OP[(OP.n_parties == 0) & (OP.text_status == "OK")].text_stem
img = P[(P.page_class == "NATIVE") & (P.image_cover > 0.3) & P.text_stem.isin(none)]
print(img.groupby("text_stem").size().sort_values(ascending=False).head(20))
```
Open 10 of these PDFs. If the noticee or penalty table is an image, the text layer lost it.

---

## 4. Money

### 4.1 Distribution and extremes
```python
import pandas as pd
A = pd.read_parquet("derived/order_actions.parquet")
print(pd.crosstab(A.order_type, A.penalty_outcome))
print(A.penalty_total.describe(percentiles=[.01, .05, .5, .95, .99]))
print(A.nlargest(15, "penalty_total")[["text_stem", "penalty_total", "penalty_total_basis", "penalty_raw"]].to_string()[:4000])
print(A[A.penalty_total < 10000][["text_stem", "penalty_total", "penalty_raw"]].to_string()[:4000])
```
**Look for:**
- amounts under ₹10,000 (most are H-1 truncations);
- the largest 15 checked against the PDFs. ₹7,269 crore on PACL (30700) is real.

### 4.2 Truncated ungrouped figures **[H-1]**
```python
import pandas as pd
S = pd.read_parquet("derived/sanction_mentions.parquet")
s = S[S.amount_rupees.notna()].copy()
s["next_char"] = [r.sentence[int(r.amount_pos) + len(r.amount_text):][:1] for r in s.itertuples()]
t = s[s.next_char.str.isdigit()]
print(len(t), "mentions in", t.text_stem.nunique(), "orders", t.kind.value_counts().to_dict())
for r in t.head(20).itertuples():
    print(r.text_stem, r.kind, r.amount_rupees, "|", r.sentence[int(r.amount_pos):int(r.amount_pos) + 60])
```
**Now:** 54 mentions in 45 orders. They include decimals before a unit: Satyam's (30647) "₹1258.88 crore" disgorgement is stored as ₹125. There is also one false alarm ("Rs. 9,00,000/-9 Rupees…"), and a registration number "INR200004031" read as a ₹200 penalty (36011).

### 4.3 Penalty tables split from their clause **[C-1]**
Takes about 2 minutes.
```python
import pandas as pd, pathlib
from src import extract_windows as ew, extract_actions as ea, amounts as am
A = pd.read_parquet("derived/order_actions.parquet")
T = pd.read_parquet("derived/text_documents.parquet", columns=["text_stem", "text_path", "status"])
m = A.merge(T, on="text_stem"); m = m[m.status == "OK"]
missed, partial, early = [], [], []
for r in m.itertuples():
    t = ew.strip_repeated_lines(pathlib.Path(r.text_path).read_text())
    n = max(len(t), 1); sp = ew.split_sentences(t)
    for i, (a, b) in enumerate(sp[:-1]):
        sent = ew.normalise_sentence(t[a:b])
        if not ea._IMPOSE.search(sent) or ea._NOT_OPERATIVE.search(sent):
            continue
        c, d = sp[i + 1]
        nxt = ew.normalise_sentence(t[c:d])
        if a / n >= ea.OPERATIVE_FROM and not t[b:c].strip() and (d - a) > ew.MAX_SENTENCE \
                and am.find_amounts(nxt, bare=True):
            (missed if pd.isna(r.penalty_total) else partial).append(r.text_stem)
            break
print("no penalty recorded:", len(missed), missed[:30])
print("penalty recorded, part of the table ignored:", len(partial), partial[:30])
```
**Now:** 28 and 50.

**Hand-check** every one against its PDF, and record the true totals if you use penalties before a fix.

### 4.4 Settlement and disgorgement restated amounts **[C-2]**
```python
import pandas as pd
S = pd.read_parquet("derived/sanction_mentions.parquet", columns=["text_stem", "kind", "amount_rupees"])
A = pd.read_parquet("derived/order_actions.parquet", columns=["text_stem", "settlement_amount", "disgorgement_total"])
for kind, col in [("settlement", "settlement_amount"), ("disgorgement", "disgorgement_total")]:
    x = S[(S.kind == kind) & S.amount_rupees.notna()].groupby("text_stem").amount_rupees.agg(["max", "nunique"])
    m = A.merge(x, left_on="text_stem", right_index=True)
    m["ratio"] = m[col] / m["max"]
    print(kind, len(m), "ratio quantiles", m.ratio.quantile([.25, .5, .75, .9]).round(2).to_dict(),
          "| one amount but ratio >= 2:", int(((m["nunique"] == 1) & (m.ratio >= 2)).sum()))
```
**Now:**
- settlement: median ratio 2.0; 825 orders with one amount and a ratio ≥ 2;
- disgorgement: 27 orders with a ratio ≥ 2.

**Until fixed**, use the largest single amount per order (`max` above) as the settlement amount.

### 4.5 Stated totals versus computed totals **[H-2]**
```python
import pandas as pd
S = pd.read_parquet("derived/sanction_mentions.parquet"); A = pd.read_parquet("derived/order_actions.parquet")
tot = S[(S.kind == "penalty") & S.is_total & S.amount_rupees.notna()].groupby("text_stem").amount_rupees.max().rename("stated")
m = A.merge(tot, left_on="text_stem", right_index=True)
d = m[m.penalty_total != m.stated]
print(len(m), "orders state a total;", len(d), "disagree")
print(d[["text_stem", "penalty_total", "stated", "penalty_total_basis"]].to_string())
```
**Now:** 750 and 50. Read each; 14949 is a confirmed under-count (₹4 lakh against ₹6 lakh).

### 4.6 Figure against words
```python
import pandas as pd
S = pd.read_parquet("derived/sanction_mentions.parquet")
w = S[S.words_agree == False]
print(len(w)); print(w[["text_stem", "kind", "amount_text", "amount_rupees", "words_rupees"]].to_string())
```
**Now:** the orders' own typos (32 penalty amounts at Day 4). The figure is kept; decide per case.

---

## 5. Dates and windows

### 5.1 Window counts, methods and sanity outcomes
```python
import pandas as pd
K = pd.read_parquet("derived/windows_checked.parquet")
print(K.sanity_status.value_counts().to_dict())
w = K[K.violation_start.notna()]
print(len(w), w.violation_method.value_counts().to_dict(), w.window_kind.value_counts().to_dict(),
      w.violation_confidence.value_counts().to_dict())
print(pd.read_csv("derived/extraction_rate.csv").to_string())
print("rows with confidence but no dates [M-9]:",
      int((K.violation_confidence.isin(["high", "medium"]) & K.violation_start.isna()).sum()))
```
**Now:**
- 12,142 windows: regex 10,508, model 1,632, hand 2;
- extraction rate 58.2%;
- 8 rows with a confidence but no dates. Always filter on `violation_start.notna()`.

### 5.2 Window lengths and lags **[M-8]**
```python
import pandas as pd
K = pd.read_parquet("derived/windows_checked.parquet")
w = K[K.violation_start.notna()].copy()
w["days"] = (pd.to_datetime(w.violation_end) - pd.to_datetime(w.violation_start)).dt.days + 1
w["lag_days"] = (pd.to_datetime(w.order_date) - pd.to_datetime(w.violation_end)).dt.days
print(w.days.describe(percentiles=[.01, .05, .5, .95, .99]))
print(w.lag_days.describe(percentiles=[.01, .5, .99]))
print(w[w.days <= 7][["text_stem", "violation_start", "violation_end", "violation_method", "violation_raw"]].head(30).to_string())
print(w[w.days > 3652][["text_stem", "violation_start", "violation_end", "violation_method", "violation_raw"]].to_string())
print("distinct windows:", w[["violation_start", "violation_end"]].drop_duplicates().shape[0])
```
**Now:**
- 57 one-day windows; 174 of at most 7 days; 42 over 10 years;
- 3,390 distinct windows.

**Read** the short ones for inspection visits, and the long ones for merged patches.

### 5.3 Listing date against the date signed in the order **[H-3]**
Takes about a minute.
```python
import pandas as pd, pathlib, re
from src import extract_windows as ew
O = pd.read_parquet("derived/orders.parquet", columns=["text_stem", "order_date", "order_type", "text_path", "text_status"])
O = O[O.text_status == "OK"]
DATE_LINE = re.compile(rf"\b(?:Date|Dated)\s*[:\-]?\s*(?P<d>{ew._FULL})", re.I)
rows = []
for r in O.itertuples():
    tail = ew.normalise_sentence(pathlib.Path(r.text_path).read_text()[-2500:])
    ms = list(DATE_LINE.finditer(tail))
    p = ew.parse_date(ms[-1].group("d")) if ms else None
    if p and p.year and p.day:
        rows.append((r.text_stem, r.order_type, (r.order_date - ew.as_start(p)).days))
d = pd.DataFrame(rows, columns=["text_stem", "order_type", "diff_days"])
print(d.groupby("order_type").diff_days.agg(lambda x: (x == 0).mean()).round(3).to_dict())
print(d[d.diff_days.abs() > 30].sort_values("diff_days").to_string()[:3000])
```
**Now:**
- the share of orders dated the same day: AO 97.8%, WTM 95.9%, ED/CGM 99.5%, settlement 32.6%;
- 871 orders more than 30 days apart.

**Decide** which date Stage 1 and Stage 3 use.

### 5.4 Prior actions **[H-5]**
```python
import pandas as pd
O = pd.read_parquet("derived/orders.parquet")
t = O[O.text_status == "OK"].copy()
print(t.prior_action_kind.value_counts(dropna=False).to_dict(), t.first_action_source.value_counts().to_dict())
t["years_before_order"] = (pd.to_datetime(t.order_date) - pd.to_datetime(t.prior_action_date)).dt.days / 365.25
print(t.years_before_order.describe(percentiles=[.5, .9, .99]))
print(t[t.years_before_order > 10][["text_stem", "order_date", "prior_action_date", "prior_action_raw"]].head(20).to_string())
w = t[t.violation_start.notna()]
print("first action on/before window end:", int((pd.to_datetime(w.first_action_date) <= pd.to_datetime(w.violation_end)).sum()))
PM = pd.read_parquet("derived/prior_action_mentions.parquet")
print(PM.reason.value_counts(dropna=False).to_dict())
```
**Now:**
- earliest-kind counts: SCN 13,550, interim 3,347, confirmatory 21;
- `first_action_source = order` for 3,940 orders;
- 1,250 windowed orders have their first action on or before the window end;
- implausible mentions: 215 before the window, 29 after the order.

### 5.5 Distributions by year and category
```python
import pandas as pd
O = pd.read_parquet("derived/orders.parquet")
O["year"] = pd.to_datetime(O.order_date).dt.year
print(pd.crosstab(O.year, O.order_type).to_string())
t = O[O.text_status == "OK"]
print(t.groupby("order_type")[["violation_start", "prior_action_date", "penalty_total", "issuer_guess"]].agg(lambda s: round(s.notna().mean(), 3)))
iso = O.title.str.contains("illiquid stock option", case=False, na=False)
print("ISO orders:", int(iso.sum()), " by year:", O[iso].year.value_counts().sort_index().to_dict())
```
**[P-1]** Repeat every headline statistic with ISO excluded.

---

## 6. Parties and regulations

### 6.1 Coverage and sources
```python
import pandas as pd
OP = pd.read_parquet("derived/order_parties.parquet")
t = OP[OP.text_status == "OK"]
print("no parties:", int((t.n_parties == 0).sum()), t[t.n_parties == 0].order_type.value_counts().to_dict())
print(t.parties_source.value_counts().to_dict())
print(t.penalty_link_status.value_counts(dropna=False).to_dict())
```
**Now:**
- 1,398 orders without parties (WTM 1,283);
- penalty links: single_party 6,798, all_rows 1,021, partial 58, none 162.

### 6.2 Roles and their evidence **[M-2]**
```python
import pandas as pd
E = pd.read_parquet("derived/order_entities.parquet")
print(E.entity_role.value_counts().to_dict())
print(E.role_basis.str.split(":").str[0].value_counts().to_dict())
print(pd.crosstab(E.entity_role, E.entity_type))
print(E.groupby("role_basis").sample(1, random_state=1)[["text_stem", "entity_name", "entity_role", "role_basis"]].head(40).to_string())
```
Check 50 parties stratified by `role_basis`. `suffix` (6,753 ISSUERs) is the weakest.

### 6.3 Per-party penalties add up to the order total
```python
import pandas as pd
E = pd.read_parquet("derived/order_entities.parquet"); O = pd.read_parquet("derived/orders.parquet")
s = E.groupby("text_stem").agg(party_sum=("penalty_amount", "sum"), joint=("penalty_joint", "any"), each=("penalty_each", "any"))
m = O.merge(s, left_on="text_stem", right_index=True)
ok = m[m.penalty_link_status.isin(["single_party", "all_rows"]) & ~m.joint & ~m.each]
print(len(ok), "orders;", int((ok.party_sum != ok.penalty_total).sum()), "mismatches")
```
**Now:** 7,322 orders, 0 mismatches.

This checks the *link*, not the total, which C-1, H-1 and H-2 can still get wrong.

### 6.4 Overlapping names and shared PANs **[M-1] [M-5]**
```python
import pandas as pd
E = pd.read_parquet("derived/order_entities.parquet")
def overlaps(g):
    n = list(g.entity_name_clean)
    return any(a != b and b.startswith(a + " ") for a in n for b in n)
small = E.groupby("text_stem").filter(lambda g: 1 < len(g) <= 60)
ov = small.groupby("text_stem").apply(overlaps)
print("orders with overlapping names:", int(ov.sum()))
pen = E[E.text_stem.isin(ov[ov].index) & E.penalty_amount.notna()]
print(pen[["text_stem", "entity_name", "penalty_amount"]].head(30).to_string())
print("shared PANs within an order:", int(E.dropna(subset=["pan"]).duplicated(["text_stem", "pan"]).sum()))
bad = E[E.pan.notna() & ((E.pan.str[3] == "P") & E.entity_name.str.contains(r"\bLtd|Limited|Pvt", case=False))]
print("person PAN on a company name:", len(bad)); print(bad[["text_stem", "entity_name", "pan", "source"]].head(20).to_string())
```
**Now:** 297 orders with overlapping names; 98 shared PANs.

### 6.5 Regulations **[M-4]**
```python
import pandas as pd, json
O = pd.read_parquet("derived/orders.parquet")
t = O[O.text_status == "OK"]
print("empty regulations:", int((t.regulations == "[]").sum()), t[t.regulations == "[]"].order_type.value_counts().to_dict())
print(pd.Series([r for x in t.regimes for r in json.loads(x)]).value_counts().head(20).to_dict())
print("empty regulations but a penalty section:", int(((t.regulations == "[]") & (t.penalty_provisions != "[]")).sum()))
print(t.sample(10, random_state=3)[["text_stem", "regulations", "penalty_provisions"]].to_string())
```
Read 30 random orders' `regulations` against the order's charge paragraph.

### 6.6 Matter text **[M-3]**
```python
import pandas as pd
O = pd.read_parquet("derived/orders.parquet", columns=["text_stem", "matter_raw", "issuer_guess", "issuer_source"])
print("matter with SAT text:", int(O.matter_raw.str.contains("SAT Appeal", na=False).sum()))
print(O.issuer_source.value_counts(dropna=False).to_dict())
print(O.issuer_guess.value_counts().head(20).to_dict())
```

---

## 7. Consistency between related fields

```python
import pandas as pd
O = pd.read_parquet("derived/orders.parquet")
c = {
  "window end after order (should be 0 except month-precision / reviewed)":
      (pd.to_datetime(O.violation_end) > pd.to_datetime(O.order_date)).sum(),
  "start after end": (pd.to_datetime(O.violation_start) > pd.to_datetime(O.violation_end)).sum(),
  "prior action after order": (pd.to_datetime(O.prior_action_date) > pd.to_datetime(O.order_date)).sum(),
  "prior action before window start": (pd.to_datetime(O.prior_action_date) < pd.to_datetime(O.violation_start)).sum(),
  "first action after order": (pd.to_datetime(O.first_action_date) > pd.to_datetime(O.order_date)).sum(),
  "penalty with outcome != penalty": (O.penalty_total.notna() & (O.penalty_outcome != "penalty")).sum(),
  "settlement amount on a non-settlement order": (O.settlement_amount.notna() & (O.order_type != "SETTLEMENT")).sum(),
  "window but text not OK": (O.violation_start.notna() & (O.text_status != "OK")).sum(),
  "parties but text not OK": ((O.n_parties > 0) & (O.text_status != "OK")).sum(),
  "penalty linked but no parties": (O.penalty_link_status.notna() & (O.n_parties == 0)).sum(),
}
for k, v in c.items(): print(f"{int(v):6d}  {k}")
```
**Now:**
- window end after the order: 2 (month-precision ends; a third window ends on the order date);
- penalty linked but no parties: 42 (these count as link status "none");
- every other check: 0.

---

## 8. Manual review sets (in order of value)

1. **C-1:** the 78 orders from §4.3. Record their true penalty totals.
2. **C-2:** 20 settlement orders across the ratio distribution in §4.4.
3. **H-2:** the 50 stated-total disagreements in §4.5.
4. **H-1:** the 45 truncated-figure orders in §4.2.
5. **Top penalties:** the 50 largest penalty totals and the 50 largest settlement amounts, against their PDFs.
6. **Windows:** the Week 3 blind sample (`python -m src.validation_sample`, 150 orders). Report exact-match and within-a-month accuracy by `violation_method` and year.
7. **Short windows:** the 174 windows of 7 days or less, and the 42 of more than 10 years (§5.2).
8. **Missing parties:** 10 WTM orders with no parties and image-heavy native pages (§3.4).
9. **Duplicates:** the 105 identical-text groups (§2.4). Decide one row or one per noticee.
10. **Dates:** 30 orders from §5.3 that are more than 30 days apart. Decide which date is the order date.

Record every decision and correction in `hand/`, and every figure you report in `results_log.md`.
