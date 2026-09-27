"""Tests for violation-window candidates and regex v1.

Most sentences below are copied from corpus orders (order id in the comment),
because the failure cases that shaped the rules came from real text: dates
that sit before their label, inspection dates next to the period inspected,
tables, and a long sentence split between "September" and "30, 2015".
"""

import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src import extract_windows as ew
from src.extract_text import PAGE_SEPARATOR

D = dt.date


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

@pytest.mark.parametrize("token, expected", [
    ("05.11.2019", (2019, 11, 5)),      # day-first: 5 November, never 11 May
    ("5/11/2019", (2019, 11, 5)),
    ("05-11-2019", (2019, 11, 5)),
    ("16.1.10", (2010, 1, 16)),
    ("24.12.99", (1999, 12, 24)),
    ("April 1, 2014", (2014, 4, 1)),
    ("April 01 ,2014", (2014, 4, 1)),
    ("June, 17, 2005", (2005, 6, 17)),  # 9263-style comma after the month
    ("Sept. 3, 2012", (2012, 9, 3)),
    ("1st April, 2014", (2014, 4, 1)),
    ("3 rd May, 2001", (2001, 5, 3)),   # 8255: space inside the ordinal
    ("01st day of April, 2014", (2014, 4, 1)),
    ("29th of March 2018", (2018, 3, 29)),
    ("01-Apr-2019", (2019, 4, 1)),      # 63097
    ("2014-04-01", (2014, 4, 1)),
    ("April, 2014", (2014, 4, None)),
    ("May 2011", (2011, 5, None)),
    ("July'99", (1999, 7, None)),
    ("February 28", (None, 2, 28)),
    ("June", (None, 6, None)),
])
def test_parse_date(token, expected):
    p = ew.parse_date(token)
    assert (p.year, p.month, p.day) == expected


@pytest.mark.parametrize("token", ["31.02.2014", "11.35.51", "00.01.2014", "April 32, 2014", "13.13.2013"])
def test_impossible_dates_are_rejected(token):
    assert ew.parse_date(token) is None


def test_month_precision_end_is_the_last_day_of_the_month():
    assert ew.as_end(ew.parse_date("February 2012")) == D(2012, 2, 29)
    assert ew.as_end(ew.parse_date("February 2013")) == D(2013, 2, 28)
    assert ew.as_start(ew.parse_date("February 2013")) == D(2013, 2, 1)


def test_date_finder_ignores_words_that_start_like_months():
    assert ew.DATE.findall("the market 2012 and Mayank 2013 and may 1000 shares") == []
    assert ew.DATE.findall("Decided on March 5, 2012") == ["March 5, 2012"]


# --------------------------------------------------------------------------
# Ranges
# --------------------------------------------------------------------------

def _one(sentence):
    windows = ew.find_windows(sentence)
    assert len(windows) == 1, windows
    return windows[0]


@pytest.mark.parametrize("sentence, start, end, rule", [
    ("for the period from November 27, 2003 to December 23, 2003.",
     D(2003, 11, 27), D(2003, 12, 23), "range"),
    ("for the period April 01, 2014 to September 30, 2015 (hereinafter",
     D(2014, 4, 1), D(2015, 9, 30), "range"),
    ("during the period 01.04.2012 to 31.03.2013", D(2012, 4, 1), D(2013, 3, 31), "range"),
    ("the period between March 17, 2005 and September 16, 2005", D(2005, 3, 17), D(2005, 9, 16), "range"),
    # Taksheel: month precision on both ends
    ("the investigation period was May 2011 to December 2011", D(2011, 5, 1), D(2011, 12, 31), "range"),
    ("during the period June, 17, 2005 - September 05, 2005", D(2005, 6, 17), D(2005, 9, 5), "range"),
    ("during the period - 3 rd May, 2001 to 6 th July, 2001 .", D(2001, 5, 3), D(2001, 7, 6), "range"),
    # 15127: "and" after "period" is a range too
    ("for the period October 1999 and March 2001.", D(1999, 10, 1), D(2001, 3, 31), "range"),
    ("from February 28 to April 30, 2002", D(2002, 2, 28), D(2002, 4, 30), "range_year_inferred"),
    ("from June to December 1996", D(1996, 6, 1), D(1996, 12, 31), "range_year_inferred"),
    ("from 29th of March to 1st of April, 2018", D(2018, 3, 29), D(2018, 4, 1), "range_year_inferred"),
    ("from November to February 2012", D(2011, 11, 1), D(2012, 2, 29), "range_year_inferred"),
    ("the period 01-Apr-2019 to 31-Mar-2020", D(2019, 4, 1), D(2020, 3, 31), "range"),
    ("the inspection period FY 2020-21", D(2020, 4, 1), D(2021, 3, 31), "fy"),
    ("the relevant period is FY 2011-12 to FY 2013-14", D(2011, 4, 1), D(2014, 3, 31), "fy_range"),
    ("Financial Years 2009-10 to 2010-11", D(2009, 4, 1), D(2011, 3, 31), "fy_range"),
])
def test_ranges(sentence, start, end, rule):
    w = _one(sentence)
    assert (w.start, w.end, w.rule) == (start, end, rule)


def test_two_dated_letters_are_not_a_range():
    assert ew.find_windows("SEBI vide letters dated 05.11.2019 and 12.12.2019 sought information.") == []


def test_backwards_range_is_rejected():
    assert ew.find_windows("for the period from 31.03.2013 to 01.04.2012") == []


def test_fy_with_mismatched_years_is_rejected():
    assert ew.find_windows("during FY 2011-14") == []


# --------------------------------------------------------------------------
# Sentences and headers
# --------------------------------------------------------------------------

def _sentences(text):
    return [text[s:e] for s, e in ew.split_sentences(text)]


def test_abbreviations_do_not_end_sentences():
    text = ("SEBI conducted investigation into dealings in the shares of Adani Exports Ltd. "
            "(hereinafter referred to as 'Adani') for the period from November 27, 2003 to "
            "December 23, 2003. The scrip was listed on BSE. M/s. ABC Pvt. Ltd. paid Rs. 5 "
            "lakh, i.e. the fee, to Mr. A. K. Sharma. Next sentence.")
    assert _sentences(text) == [
        "SEBI conducted investigation into dealings in the shares of Adani Exports Ltd. "
        "(hereinafter referred to as 'Adani') for the period from November 27, 2003 to "
        "December 23, 2003.",
        "The scrip was listed on BSE.",
        "M/s. ABC Pvt. Ltd. paid Rs. 5 lakh, i.e. the fee, to Mr. A. K. Sharma.",
        "Next sentence.",
    ]


def test_sentence_ending_in_a_numeric_date_splits():
    assert _sentences("It ended on 31.03.2012. The noticee replied.") == [
        "It ended on 31.03.2012.", "The noticee replied."]


def test_sentences_run_across_page_breaks():
    text = "for the period from April 1, 2014 to\n\fSeptember 30, 2015 (the IP). Next one."
    first = _sentences(text)[0]
    assert ew.normalise_sentence(first) == "for the period from April 1, 2014 to September 30, 2015 (the IP)."


def test_long_legal_sentence_is_not_cut_inside_its_date_range():
    # 74121: a 1,000+ character sentence with the range across a line break.
    text = ("x " * 450 + "while trading in illiquid stock options at BSE during April 01, 2014 to "
            "September \n30, 2015 ( 'investigation period/relevant period') by the Company.")
    [only] = _sentences(text)
    r = ew.score_sentence(ew.normalise_sentence(only), 0.1)
    assert r["window"].end == D(2015, 9, 30)


def test_table_sized_sentences_are_resplit_with_valid_offsets():
    rows = "\n".join(f"{i} Name of entity {i} 1,000 2,000" for i in range(200))
    spans = ew.split_sentences(rows)
    assert len(spans) > 1
    assert all(e - s <= ew.MAX_SENTENCE for s, e in spans)
    assert "".join(rows[s:e] for s, e in spans).replace("\n", "") == rows.replace("\n", "")


def _order(pages):
    return PAGE_SEPARATOR.join(pages)


def test_running_headers_are_blanked_and_offsets_kept():
    words = ["alpha", "bravo", "charlie", "delta"]
    body = [f"Adjudication Order in the matter of XYZ Ltd\nPage {i} of 4\n"
            f"Paragraph about {w} that is unique to this page.\nLimited\n{w} body line\n"
            f"another {w} line\nLimited\n{w} one\n{w} two\n{w} three\n{w} four\n{w} five\n"
            f"{w} six\n{w} seven" for i, w in enumerate(words, start=1)]
    text = _order(body)
    out = ew.strip_repeated_lines(text)
    assert len(out) == len(text)
    assert "Adjudication Order in the matter" not in out
    assert "Page 3 of 4" not in out
    assert "Paragraph about charlie that is unique to this page." in out
    # A repeated line in the middle of pages is content, not a header.
    assert out.count("Limited") == 4


def test_short_orders_are_left_alone():
    text = _order(["Header\nbody one", "Header\nbody two"])
    assert ew.strip_repeated_lines(text) == text


# --------------------------------------------------------------------------
# Labels, scoring and choosing a range
# --------------------------------------------------------------------------

ISO = ("SEBI conducted an investigation into the trading activities of certain entities in "
       "illiquid stock options on BSE for the period April 01, 2014 to September 30, 2015 "
       "(hereinafter referred to as “investigation period”/“IP”).")
ADANI = ("Securities and Exchange Board of India (hereinafter referred to as ‘SEBI’) conducted "
         "investigation into dealings in the shares of Adani Exports Ltd. (hereinafter referred "
         "to as ‘Adani’) for the period from November 27, 2003 to December 23, 2003.")


def test_defined_investigation_period_is_high():
    r = ew.score_sentence(ISO, 0.05)
    assert r["label_kind"] == ew.DEFINED
    assert (r["window"].start, r["window"].end, r["confidence"]) == (D(2014, 4, 1), D(2015, 9, 30), "high")


def test_older_conducted_investigation_phrasing_is_strong():
    r = ew.score_sentence(ADANI, 0.05)
    assert r["label_kind"] == ew.STRONG
    assert (r["window"].start, r["confidence"]) == (D(2003, 11, 27), "high")


def test_defined_label_outscores_everything_else():
    generic = ew.score_sentence("The company raised funds during the period from 1.4.2000 to 31.3.2004.", 0.05)
    context = ew.score_sentence(
        "An inspection of the books of account of MSL was conducted by SEBI for the period "
        "April 2002 to March 2004.", 0.05)
    assert ew.score_sentence(ISO, 0.5)["score"] > context["score"] > generic["score"]


def test_generic_period_is_a_candidate_but_never_the_window():
    # CLAUDE.md §1: a date range about funds collected is not an investigation period.
    r = ew.score_sentence("The company raised funds during the period from 1.4.2000 to 31.3.2004.", 0.05)
    assert r["label_kind"] == ew.GENERIC
    assert r["window"] is not None and r["confidence"] is None


@pytest.mark.parametrize("sentence", [
    "The offer was kept open from 01.04.2012 to 20.04.2012.",
    "SEBI vide letters dated 05.11.2019 and 12.12.2019 sought information.",
    # a deadline, not a window
    "any person shall make an application within a period of two months from October 15, 1999.",
])
def test_non_candidates(sentence):
    assert ew.score_sentence(sentence, 0.05) is None


def test_context_word_needs_period_before_the_range():
    # 103044: when the investigation ran, with no "period" before the range.
    r = ew.score_sentence(
        "July 2021 - October 2022: IA conducted a detailed investigation; summons were issued.", 0.5)
    assert r["label_kind"] == ew.CONTEXT and r["confidence"] is None
    # 101184: an inspection visit is not even a candidate.
    assert ew.score_sentence("Inspection of books and records of Axis Trustee Services Ltd conducted "
                             "from August 22, 2012 to August 24, 2012 Administrative Warning.", 0.5) is None
    r = ew.score_sentence("An inspection of the books of account of MSL was conducted by SEBI for "
                          "the period April 2002 to March 2004.", 0.05)
    assert (r["confidence"], r["window"].start) == ("medium", D(2002, 4, 1))


def test_financial_year_needs_an_explicit_label():
    r = ew.score_sentence("This process was started post inspection of SEBI in FY 2015-16.", 0.5)
    assert r["confidence"] is None
    r = ew.score_sentence("So clients for the Inspection period FY 2020-21 had been treated as Inactive.", 0.5)
    assert (r["confidence"], r["window"].rule) == ("medium", "fy")


def test_tcp_ip_and_pre_examination_are_not_labels():
    r = ew.score_sentence("OPG connected to the server for the period from January 2010 to April 5, "
                          "2014 when NSE disseminated TBT data in the TCP/IP format.", 0.5)
    assert r["label_kind"] == ew.GENERIC
    r = ew.score_sentence("the price-volume movement for period April 1, 2006 till December 31, 2007 "
                          "covering pre-examination, examination and post-examination period.", 0.5)
    assert r["label_kind"] == ew.CONTEXT


def test_inspection_dates_lose_to_the_period_inspected():
    # 46484
    r = ew.score_sentence(
        "SEBI conducted inspection of the books and accounts of the Noticee from February 12, 2012 "
        "to February 22, 2012 to examine the irregularities committed by the Noticee during the "
        "course of business for the period April 01, 2010 to February 11, 2012.", 0.05)
    assert (r["window"].start, r["window"].end) == (D(2010, 4, 1), D(2012, 2, 11))
    assert r["confidence"] == "high"  # only one range is introduced by "period"


def test_a_table_of_periods_is_never_high():
    # 43789: a history table with a "Period of inspection" column.
    r = ew.score_sentence(
        "Sl No Inspecting Authority Period of inspection Details of action 1 SEBI period April 2011 "
        "to August 2012 Administrative warning 2 SEBI period April 2012 to March 2014 Adjudication "
        "proceedings initiated.", 0.5)
    assert r["confidence"] == "medium"


def test_label_picks_the_nearest_range():
    # 98000
    r = ew.score_sentence(
        "SEBI carried out inspection of Launchpad Fintech Pvt. Ltd. on July 22-23, 2024 and the "
        "period of inspection was from February 15, 2023 to June 30, 2024 (hereinafter referred to "
        "as “Inspection Period”).", 0.05)
    assert (r["window"].start, r["window"].end) == (D(2023, 2, 15), D(2024, 6, 30))


# --------------------------------------------------------------------------
# The window is the activity SEBI examined, never the time the probe took
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sentence, start, end", [
    # SEBI's usual wording: "conducted investigation during" means the activity period
    ("SEBI conducted an investigation during February 5, 2003 to June 6, 2003 (hereinafter referred to "
     "as investigation period) into the trading.", D(2003, 2, 5), D(2003, 6, 6)),
    ("The investigation in the irregular trading activities was conducted during the period May 1, "
     "2008 to September 30, 2009.", D(2008, 5, 1), D(2009, 9, 30)),
    ("SEBI conducted investigation in the matter of GLLFL during the period January 01, 2013 to February "
     "15, 2014 (hereinafter referred to as “investigation period”).", D(2013, 1, 1), D(2014, 2, 15)),
    # a long covered period phrased with "during" is still the covered period
    ("SEBI had carried out inspection in respect of the Noticee during the period April 2021 to July "
     "2022 (hereinafter referred to as “Inspection Period”).", D(2021, 4, 1), D(2022, 7, 31)),
    # "inspection in the scrip" examines trading
    ("SEBI conducted an inspection in the scrip of International Conveyors Ltd. during the period "
     "June 12, 2009 to July 1, 2009 to ascertain any violation.", D(2009, 6, 12), D(2009, 7, 1)),
    # 30665: an inspection *into the dealings* during a period covers that period
    ("In view of this, a joint inspection was conducted by SEBI and NSE into the dealings of the Noticee "
     "as a stock broker during the period January and February 2005.", D(2005, 1, 1), D(2005, 2, 28)),
    # 5163: a 35-day visit beside the period it covered
    ("M/s. Agarwal Sanjiv & Co. conducted an inspection of books and records of the Noticee on behalf of "
     "SEBI during the period January 12, 2005 to February 15, 2005 covering the period from January 1, "
     "2004 to December 31, 2004.", D(2004, 1, 1), D(2004, 12, 31)),
    # a short investigation period is real (the listing day, a few days of trades)
    ("SEBI conducted an investigation which revealed that during the period February 22, 2008 to "
     "February 27, 2008, APPL had acquired shares.", D(2008, 2, 22), D(2008, 2, 27)),
])
def test_activity_period_is_kept(sentence, start, end):
    w = ew.score_sentence(sentence, 0.05)["window"]
    assert (w.start, w.end) == (start, end)


@pytest.mark.parametrize("sentence", [
    # 7790: a six-day visit
    "SEBI conducted an inspection of the Company during the period February 28, 2005 to March 5, 2005.",
    # 19924: a visit even when labelled "inspection period"
    "SEBI conducted inspection of the broker, a member of NSE, from October 26, 2009 to October 30, 2009 "
    "(hereinafter referred to as ‘inspection period’).",
    # 40883: labelled before the dates
    "The documents were prepared in the presence of inspection team during inspection period between "
    "22.09.2015 to 28.09.2015.",
    # 52537: "Shares" in the company name is not trading
    "SEBI conducted an inspection of books of accounts of Shreni Shares Private Limited (Stock Broker in "
    "NSE) during September 16, 2019 to September 20, 2019 (hereinafter referred to as “inspection period/IP”).",
    # 6214: a two-month visit; the covered period is given in words regex cannot read
    "I note that SEBI conducted the inspection during the period May 2003 – June 2003 and the period "
    "covered under the inspection was 2001-2002.",
])
def test_inspection_visits_are_never_the_window(sentence):
    r = ew.score_sentence(sentence, 0.05)
    assert r is None or r["window"] is None


def test_covered_period_closes_at_the_date_of_inspection():
    # 33103: the covered period runs "till the date of inspection", the visit gives that date.
    r = ew.score_sentence(
        "SEBI conducted a limited purpose inspection of Investmentor Securities Limited (hereinafter "
        "referred to as ‘Noticee’) during 16.01.2014 - 17.01.2014 for the period 01.04.2012 till the date "
        "of inspection (hereinafter referred to as “inspection period”).", 0.05)
    w = r["window"]
    assert (w.start, w.end, w.rule, r["confidence"]) == (
        D(2012, 4, 1), D(2014, 1, 17), "range_till_inspection", "medium")
    # 103720: the date is written out
    r = ew.score_sentence("The inspection was conducted during February 11-15, 2020, and the period of "
                          "inspection was from April 1, 2018 till the date of inspection, February 15, 2020.",
                          0.05)
    assert (r["window"].start, r["window"].end) == (D(2018, 4, 1), D(2020, 2, 15))


@pytest.mark.parametrize("sentence, start, end", [
    # 15024
    ("1.2 An Inspection of the Books of Accounts, Documents and other records maintained by the broker "
     "during the period 2000-2001, 2001-2002 and from April 2002 till the date of inspection i.e. "
     "24.05.2002 was conducted.", D(2000, 4, 1), D(2002, 5, 24)),
    # 15017
    ("An Inspection of the Books of Accounts maintained by the sub-broker during the period 2001-2002, "
     "2002 - April 2003 till the date of inspection i.e. June 3, 2003 was carried out by SEBI.",
     D(2001, 4, 1), D(2003, 6, 3)),
])
def test_financial_years_listed_before_the_range_move_its_start(sentence, start, end):
    r = ew.score_sentence(sentence, 0.05)
    assert (r["window"].start, r["window"].end, r["window"].rule, r["confidence"]) == (
        start, end, "range_fy_list", "medium")


def test_a_lone_year_after_period_does_not_move_the_start():
    r = ew.score_sentence("the investigation period in 2012 was from April 1, 2013 to March 31, 2014.", 0.05)
    assert r["window"].start == D(2013, 4, 1)


def test_trading_observed_during_an_inspection_is_activity():
    r = ew.score_sentence("In the inspection it was observed that the noticee executed trades during the "
                          "period March 1, 2010 to March 10, 2010.", 0.05)
    assert (r["window"].start, r["window"].end) == (D(2010, 3, 1), D(2010, 3, 10))


def test_period_ie_is_not_a_patch():
    # 16938: sold at rigged prices in one period, settled in the next
    r = ew.score_sentence(
        "During investigations it was found that the member also sold substantial quantities of SLML "
        "shares at the rigged prices during the same period i.e., 1.8.1994 to 31.12.1994 by way of "
        "off-the-floor Spot transactions, but deliveries and payments of these transactions were "
        "effected during 1.1.95 to 31.3.95.", 0.1)
    assert (r["window"].start, r["window"].end) == (D(1994, 8, 1), D(1994, 12, 31))
    # real patches are still merged
    r = ew.score_sentence("SEBI conducted an investigation in the scrip of STL during the period from May 29, "
                          "2001 to September 07, 2001 (hereinafter referred to as “Period I”) and February 25, "
                          "2002 to May 09, 2002 (hereinafter referred to as “Period II”).", 0.1)
    assert (r["window"].start, r["window"].end, r["window"].rule) == (D(2001, 5, 29), D(2002, 5, 9), "patches_union")


def test_ip_telephony_is_not_a_label():
    assert ew.score_sentence("The personal hearing was held through Video IP telephony on 15/10/2018.",
                             0.5) is None


def test_patches_are_merged():
    # 49694
    r = ew.score_sentence(
        "Following patches were chosen by the IA: Patch 1: 07.05.2007 to 05.11.2007 (i.e. pricing "
        "period of FCCB) Patch 2: 05.11.2007 to 12.06.2008 Patch 3: 13.06.2008 to 12.12.2008 for "
        "the investigation period.", 0.5)
    assert (r["window"].start, r["window"].end, r["window"].rule) == (
        D(2007, 5, 7), D(2008, 12, 12), "patches_union")
    assert r["confidence"] == "medium"


# --------------------------------------------------------------------------
# Per order
# --------------------------------------------------------------------------

def _job(stem="123", text_path=None, status="OK"):
    return ew.Job(text_stem=stem, order_id=int(stem) if stem.isdigit() else None,
                  order_type="AO", order_date=D(2020, 1, 1), text_path=text_path, text_status=status)


ORDER = _order([
    "Adjudication Order in the matter of XYZ\nPage 1 of 3\n"
    "BEFORE THE ADJUDICATING OFFICER\n1. SEBI vide letter dated 05.11.2019 sought details.\n"
    "2. The company raised funds during the period from 1.4.2000 to 31.3.2004.",
    "Adjudication Order in the matter of XYZ\nPage 2 of 3\n" + ISO + "\n"
    "3. During the investigation period the noticee traded heavily.",
    "Adjudication Order in the matter of XYZ\nPage 3 of 3\n"
    "4. A penalty is imposed on the noticee.",
])


def test_order_with_a_defined_period_is_parsed():
    result, cands = ew.extract_candidates(_job(), ORDER)
    assert result.window_status == ew.PARSED
    assert (result.violation_start, result.violation_end) == (D(2014, 4, 1), D(2015, 9, 30))
    assert result.violation_confidence == "high" and result.violation_method == "regex"
    assert result.violation_raw == ISO  # rule 2: the source sentence travels with the dates
    top = cands[0]
    assert top.cand_rank == 1 and top.page_no == 2
    # Offsets point into the original file, headers and all.
    assert ew.normalise_sentence(ORDER[top.char_start:top.char_end]) == ISO
    assert [c.cand_rank for c in cands] == list(range(1, len(cands) + 1))


def test_top_k_limits_candidates():
    _, cands = ew.extract_candidates(_job(), ORDER, top_k=1)
    assert len(cands) == 1


def test_generic_range_only_is_unconfirmed():
    result, cands = ew.extract_candidates(
        _job(), "The company raised funds during the period from 1.4.2000 to 31.3.2004.")
    assert result.window_status == ew.UNCONFIRMED_RANGE
    assert result.violation_start is None and result.violation_raw is None
    assert result.violation_confidence == "low"  # runbook: low with an empty raw
    assert cands[0].parsed_start == D(2000, 4, 1)  # kept for the model


def test_labelled_but_unreadable_is_unparsed():
    result, _ = ew.extract_candidates(
        _job(), "The investigation period ended on 31.03.2012, and trading resumed later.")
    assert result.window_status == ew.UNPARSED


def test_no_candidates():
    result, cands = ew.extract_candidates(_job(), "A penalty of Rs. 5 lakh is imposed. Dated 5.1.2020.")
    assert (result.window_status, cands) == (ew.NO_CANDIDATES, [])


def test_order_without_text():
    result, cands = ew.process(_job(status="NO_SOURCE"))
    assert (result.window_status, cands) == (ew.NO_TEXT, [])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _write_documents(tmp_path, texts):
    rows = []
    for stem, text in texts.items():
        path = None
        if text is not None:
            path = tmp_path / "text" / f"{stem}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        rows.append({"text_stem": stem, "order_id": int(stem) if stem.isdigit() else None,
                     "order_type": "AO", "order_date": D(2020, 1, 1),
                     "text_path": str(path) if path else None,  # absolute: joins to itself
                     "status": "OK" if text is not None else "NO_SOURCE"})
    table = pa.Table.from_pylist(rows, schema=pa.schema([
        ("text_stem", pa.string()), ("order_id", pa.int64()), ("order_type", pa.string()),
        ("order_date", pa.date32()), ("text_path", pa.string()), ("status", pa.string())]))
    docs = tmp_path / "text_documents.parquet"
    pq.write_table(table, docs)
    return docs


def test_main_writes_both_tables_to_scratch(tmp_path):
    docs = _write_documents(tmp_path, {
        "1": ORDER,
        "2": "The company raised funds during the period from 1.4.2000 to 31.3.2004.",
        "noid_abcdef012345": "A penalty is imposed.",
        "4": None,
    })
    scratch = tmp_path / "out"
    assert ew.main(["--documents", str(docs), "--scratch", str(scratch), "--workers", "1"]) == 0

    windows = pq.read_table(scratch / "windows.parquet").to_pylist()
    assert [w["text_stem"] for w in windows] == ["1", "2", "4", "noid_abcdef012345"]
    status = {w["text_stem"]: w["window_status"] for w in windows}
    assert status == {"1": ew.PARSED, "2": ew.UNCONFIRMED_RANGE, "4": ew.NO_TEXT,
                      "noid_abcdef012345": ew.NO_CANDIDATES}
    assert windows[0]["violation_start"] == D(2014, 4, 1)
    assert windows[0]["rules_version"] == ew.RULES_VERSION

    cands = pq.read_table(scratch / "window_candidates.parquet")
    assert cands.schema.equals(ew.CANDIDATES_SCHEMA)
    assert set(cands.column("text_stem").to_pylist()) == {"1", "2"}
    assert (scratch / "extract_windows.log").exists()


def test_main_refuses_to_write_under_raw(tmp_path):
    docs = _write_documents(tmp_path, {"1": ORDER})
    with pytest.raises(ValueError, match="refusing to write"):
        ew.main(["--documents", str(docs), "--scratch", str(ew.REPO_ROOT / "raw" / "pdfs" / "x")])


def test_parallel_run_matches_serial(tmp_path):
    docs = _write_documents(tmp_path, {str(i): ORDER for i in range(1, 6)})
    jobs = ew.load_jobs(docs)
    serial = ew.run(jobs, workers=1)
    parallel = ew.run(jobs, workers=2, chunk=2)
    key = lambda pair: sorted((w.text_stem, w.violation_start) for w in pair[0])
    assert key(serial) == key(parallel)
    assert len(serial[1]) == len(parallel[1])
