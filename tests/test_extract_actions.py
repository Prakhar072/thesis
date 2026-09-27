"""Tests for Day 4: prior actions and sanctions. Sentences are from corpus
orders (order id in the comment) — each one a phrasing the rules must get right
or a trap they must not fall into."""

import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src import extract_actions as ea

D = dt.date


def _prior(text, order_date=D(2026, 1, 1), window_start=None):
    return sorted((m.kind, m.date) for m in ea.find_prior_mentions(text, "x", order_date, window_start)
                  if m.plausible)


@pytest.mark.parametrize("text, expected", [
    # 36765: reference number and "(hereinafter …)" between the keyword and "dated"
    ("A common Show Cause Notice (hereinafter referred to as “SCN”) bearing ref no EAD/BJD/NJMR/24445/2017 dated "
     "October 6, 2017 was issued to the Noticees", [("scn", D(2017, 10, 6))]),
    ("Subsequent to receipt of clarifications, the SCN was issued on May 05, 2022.", [("scn", D(2022, 5, 5))]),
    ("Show Cause Notice No EAD-5/ADJ/SVKM/AA/OW/5640/1/2017 dated March 15, 2017.", [("scn", D(2017, 3, 15))]),
    ("SEBI passed an ad interim ex parte Order dated April 27, 2006, under sections 11 and 11B",
     [("interim", D(2006, 4, 27))]),
    ("Interim order was passed by SEBI on August 20, 2015 which was confirmed vide Orders dated July 30, 2016.",
     [("interim", D(2015, 8, 20))]),
    ("five confirmatory orders dated June 15, 2016, September 30, 2016 and July 13, 2017 were passed",
     [("confirmatory", D(2016, 6, 15)), ("confirmatory", D(2016, 9, 30)), ("confirmatory", D(2017, 7, 13))]),
    # 42973: "Confirmatory Order" without "dated"
    ("the restraint imposed on him vide Interim Order dated October 13, 2017 and the Confirmatory Order April 25, "
     "2018 are vacated", [("confirmatory", D(2018, 4, 25)), ("interim", D(2017, 10, 13))]),
    ("Shri D. Sura Reddy was appointed as the Adjudicating Officer vide Order dated April 3, 2008 to inquire",
     [("ao_appointment", D(2008, 4, 3))]),
])
def test_prior_actions_are_found(text, expected):
    assert _prior(text) == expected


@pytest.mark.parametrize("text", [
    "The SCN was served upon the Noticee through SPAD and digitally signed email dated April 25, 2025.",
    "In reply to the SCN, vide letter dated September 21, 2021, she was granted an extension",
    "Interim Order were disposed of vide final Order dated April 05, 2018, without any further directions",
    # 53505, 45248: another document's date after the keyword
    "as confirmed by Winsome in its reply to the SCN, a circular resolution dated September 03, 2010 was signed",
    "The SCN is entirely based on allegation of persons acting in concert only due to a purported MOU dated May 27, 2010",
    # 48002: a reply letter is not the AO's appointment
    "The Adjudicating Officer has been appointed after lapse of 7 years. Noticee vide letter dated September 14, 2020.",
])
def test_dates_of_other_documents_are_not_prior_actions(text):
    assert _prior(text) == []


def test_plausibility():
    assert ea.plausibility(D(1991, 1, 1), D(2010, 1, 1), None) == (False, "before the SEBI Act")
    assert ea.plausibility(D(2011, 1, 1), D(2010, 1, 1), None) == (False, "after the order")
    assert ea.plausibility(D(2005, 1, 1), D(2010, 1, 1), D(2006, 1, 1)) == (False, "before the violation window")
    assert ea.plausibility(D(2007, 1, 1), D(2010, 1, 1), D(2006, 1, 1)) == (True, None)


# --------------------------------------------------------------------------
# Sanctions
# --------------------------------------------------------------------------

def _sanctions(sentences, order_type="AO"):
    """Sentences as the operative part: all at 90% of the text."""
    return ea.find_sanctions([(s, 0.9) for s in sentences], "x", order_type)


def _order(sentences, order_type="AO"):
    job = ea.Job("x", 1, order_type, D(2020, 1, 1), "p", "OK", None)
    return ea.summarise_order(job, [], _sanctions(sentences, order_type))


def test_a_plain_penalty():
    o = _order(["I hereby impose a monetary penalty of Rs.10,00,000/- (Rupees Ten Lakh) on Noticee, viz., Axis "
                "Trustee Services Limited under section 15HB of SEBI Act."])
    assert (o.penalty_total, o.penalty_n, o.penalty_words_checked, o.penalty_outcome) == (1_000_000, 1, 1, "penalty")


def test_statute_and_show_cause_language_are_not_penalties():
    o = _order(["he shall be liable to a penalty which shall not be less than five lakh rupees but which may extend "
                "to twenty-five crore rupees or three times the amount of profits, whichever is higher.",
                "to show cause as to why a penalty of Rs. 25,00,000 should not be imposed on the Noticee."])
    assert o.penalty_total is None


def test_a_restated_penalty_counts_once():
    # 7199: in the discussion of quantum, then again in the ORDER
    o = _order(["I impose a penalty of ` 35,00,000 /- (Rupees Thirty Five Lakhs only) on the noticee under Section "
                "15 A(a) of the SEBI Act, which is appropriate.",
                "In exercise of the powers conferred under Section 15 I, I hereby impose a penalty of ` 35,00,000/- "
                "(Rupees Thirty Five Lakhs only) on Gibs Computers Ltd. in terms of Section 15 A(a)."])
    assert (o.penalty_total, o.penalty_n) == (3_500_000, 1)


def test_equal_penalties_on_different_noticees_both_count():
    o = _order(["I hereby impose the following monetary penalties on the Noticees: 1 Prashant Boorugu Section "
                "15A(b) 4,00,000 (Rs Four Lakhs only) 2 Cronimet Mercantile Section 15A(b) 6,00,000 (Rs Six Lakhs "
                "only) 3 Ravi Kumar Section 15A(b) 4,00,000 (Rs Four Lakhs only)"])
    assert (o.penalty_total, o.penalty_n) == (1_400_000, 3)


def test_other_money_in_an_imposing_sentence_is_not_a_penalty():
    # 59323, 35885
    o = _order(["After taking into consideration that the misutilisation amount extends upto Rs 294.73 crores, I "
                "hereby impose a penalty of Rs. 1,00,00,000 (Rupees One Crore Only) upon the Noticee."])
    assert (o.penalty_total, o.penalty_n) == (10_000_000, 1)
    o = _order(["Before I impose the penalty, I must refer to the situation wherein the whole amount of Rs. 49,100/- "
                "is to be refunded to the investors."])
    assert o.penalty_total is None


def test_redundant_unit_and_malformed_figure():
    # 5339, 42430
    o = _order(["I hereby impose a monetary penalty of Rs. 10,00,000 lakhs (Rupees Ten Lakhs only) on the Noticee."])
    assert o.penalty_total == 1_000_000
    o = _order(["I hereby impose a penalty of ` 2,00,00/- (Rupees Two Lakh Only) on the noticee."])
    assert (o.penalty_total, o.penalty_words_disagree) == (200_000, 1)


def test_each_with_a_stated_total_uses_the_total():
    # 26749
    o = _order(["I hereby impose a penalty of ` 75,00,000/- (Rupees Seventy Five Lakh only) upon Noticee No. 1, "
                "` 25,00,000/- (Rupees Twenty Five Lakh only) upon Noticee No. 2 and ` 50,00,000/- (Rupees Fifty "
                "Lakh only) each upon Noticee No. 3 & 4, i.e. a total of ` 2,00,00,000/- (Rupees Two Crore only)."])
    assert (o.penalty_total, o.penalty_total_basis, o.penalty_each) == (20_000_000, "stated_total", True)


def test_each_without_a_total_is_flagged_for_day_5():
    o = _order(["I hereby impose a penalty of Rs. 5,00,000/- (Rupees Five Lakh only) each on Noticee No. 1 and 2."])
    assert (o.penalty_total, o.penalty_total_basis) == (500_000, "rows_each_unresolved")


def test_a_table_total_is_not_added_to_its_rows():
    # 43344
    o = _order(["I, hereby impose monetary penalty of ` 1,00,000/- (Rupees One Lakh only) under section 15HB of "
                "SEBI Act and ` 3,00,000/- (Rupees Three Lakh only) under section 15A(b) of SEBI Act on the "
                "Noticee, i.e. total of ` 4,00,000/- (Rupees Four Lakh only) on the Noticee."])
    assert (o.penalty_total, o.penalty_n) == (400_000, 2)


def test_a_consolidated_penalty_is_a_total():
    # 20671
    o = _order(["I impose a consolidated penalty of ` 18,00,000/- (Rupees eighteen lakh only) on the Noticees "
                "[ ` 3,00,000/- in terms of section 15 A(b) and ` 15,00,000 lakh in terms of section 15 H(ii) ]."])
    assert (o.penalty_total, o.penalty_n) == (1_800_000, 2)


def test_a_row_number_after_a_rupee_sign_is_not_money():
    # 42430 (also: "2,00,00/-" has lost a zero; its words are right)
    o = _order(["I hereby impose the following penalties on the Noticees: Noticee No. Name of the Noticee Penalty "
                "amount in ` 1 Mukeshsingh M Jagarval ` 2,00,00/- (Rupees Two lakhs only) 2 Sweta Virendra Shah "
                "` 2,00,00/- (Rupees Two lakhs only) 3 Mittali Christachary ` 1,00,000/- (Rupees One lakh only)"])
    assert (o.penalty_total, o.penalty_n) == (500_000, 3)


def test_joint_and_several():
    o = _order(["I hereby impose monetary penalty of ₹ 20,00,000/- (Rupees Twenty Lakh Only) on the Noticees under "
                "section 15H(ii) of the SEBI Act.", "Noticees are jointly and severally liable to pay the said penalty."])
    assert (o.penalty_total, o.penalty_joint) == (2_000_000, True)


def test_settlement_amount_in_a_settlement_order():
    o = _order(["The applicant has remitted a sum of `2,00,000/- (Rupees Two Lakhs only) towards the settlement "
                "terms."], order_type="SETTLEMENT")
    assert (o.settlement_amount, o.penalty_total) == (200_000, None)


def test_disgorgement_and_its_citation():
    o = _order(["The Noticees are hereby directed to disgorge the unlawful gain of ₹54,61,127 along with interest."])
    assert o.disgorgement_total == 5_461_127
    o = _order(["SAT had set aside the order which had directed them to disgorge the profits of Rs. 10,00,000."])
    assert o.disgorgement_total is None


def test_restraint_and_its_duration():
    o = _order(["The Noticees are hereby restrained from accessing the securities market and are further prohibited "
                "from buying, selling or otherwise dealing in securities, directly or indirectly, for a period of 4 "
                "(four) years from the date of this order."], order_type="WTM")
    assert (o.debarred, o.debarment_months_max) == (True, 48)
    # a restraint recited from an interim order is not this order's sanction
    o = _order(["the restraint imposed on him vide Interim Order dated October 13, 2017 restraining him from "
                "accessing the securities market is vacated with immediate effect."], order_type="WTM")
    assert o.debarred is False


def test_registration_action_and_warning():
    o = _order(["I hereby suspend the certificate of registration of the Noticee for a period of six months.",
                "The Noticee is hereby warned to be careful in future."], order_type="WTM")
    assert (o.registration_action, o.warned) == ("suspension", True)


def test_no_penalty_outcome_and_a_noticees_plea():
    o = _order(["the SCN dated January 10, 2024 is disposed of, without imposition of penalty."])
    assert o.penalty_outcome == "no_penalty"
    o = _order(["It is therefore, humbly prayed that the SCN be dropped without imposing any monetary penalty on us."])
    assert o.penalty_outcome == "unknown"


def test_only_the_operative_part_counts():
    s = "I hereby impose a penalty of Rs. 5,00,000/- (Rupees Five Lakh only) on the Noticee."
    assert ea.find_sanctions([(s, 0.2)], "x", "AO") == []


def test_first_action_is_the_earliest_prior_action_or_the_order():
    job = ea.Job("x", 1, "AO", D(2020, 1, 1), "p", "OK", D(2015, 1, 1))
    prior = [ea.PriorMention("x", "scn", D(2018, 5, 1), "day", "", "", 0.1, True, None),
             ea.PriorMention("x", "interim", D(2017, 3, 1), "day", "", "", 0.2, True, None),
             ea.PriorMention("x", "ao_appointment", D(2016, 1, 1), "day", "", "", 0.1, True, None),
             ea.PriorMention("x", "scn", D(2010, 1, 1), "day", "", "", 0.3, False, "before the violation window")]
    o = ea.summarise_order(job, prior, [])
    assert (o.prior_action_date, o.prior_action_kind, o.first_action_date, o.first_action_source) == (
        D(2017, 3, 1), "interim", D(2017, 3, 1), "prior_action")
    assert (o.earliest_scn_date, o.ao_appointment_date, o.n_prior_implausible) == (D(2018, 5, 1), D(2016, 1, 1), 1)
    o = ea.summarise_order(job, [], [])
    assert (o.first_action_date, o.first_action_source) == (D(2020, 1, 1), "order")


def test_main_end_to_end(tmp_path):
    text = ("BEFORE THE ADJUDICATING OFFICER\n" + "Background of the matter. " * 40 +
            "\nA Show Cause Notice dated March 15, 2017 was issued to the Noticee.\n" + "Findings follow. " * 40 +
            "\nORDER\nI hereby impose a penalty of Rs. 5,00,000/- (Rupees Five Lakh only) on the Noticee under "
            "section 15HA of SEBI Act.\n")
    tp = tmp_path / "1.txt"
    tp.write_text(text)
    docs = tmp_path / "docs.parquet"
    pq.write_table(pa.Table.from_pylist([
        {"text_stem": "1", "order_id": 1, "order_type": "AO", "order_date": D(2018, 1, 1), "text_path": str(tp),
         "status": "OK"},
        {"text_stem": "2", "order_id": 2, "order_type": "AO", "order_date": D(2018, 1, 1), "text_path": None,
         "status": "NO_SOURCE"}]), docs)
    out = tmp_path / "out"
    assert ea.main(["--documents", str(docs), "--windows", str(tmp_path / "none.parquet"),
                    "--scratch", str(out), "--workers", "1"]) == 0
    rows = {r["text_stem"]: r for r in pq.read_table(out / "order_actions.parquet").to_pylist()}
    assert rows["1"]["prior_action_date"] == D(2017, 3, 15) and rows["1"]["penalty_total"] == 500_000
    assert rows["1"]["first_action_date"] == D(2017, 3, 15)
    assert rows["2"]["penalty_total"] is None
    assert pq.read_table(out / "sanction_mentions.parquet").num_rows == 1
