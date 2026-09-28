"""Tests for parties, roles and per-party sanctions (runbook Week 2, Day 5).
Headers and operative sentences are copied from corpus orders (stems in comments)."""

import time
from dataclasses import asdict

import pytest

from src import extract_actions as ea
from src import extract_parties as xp
from src import extract_windows as ew


def parties_in(header: str) -> list[xp.Party]:
    flat = ew.normalise_sentence(header)
    parties, _, _ = xp.header_parties(flat, xp.header_end(flat))
    return parties


def names(header: str) -> list[str]:
    return [p.name for p in parties_in(header)]


# --------------------------------------------------------------------------
# The noticee list in the header
# --------------------------------------------------------------------------

def test_single_noticee_with_pan():  # 54316 (illiquid stock options)
    [p] = parties_in("ADJUDICATION ORDER NO. Order/PM/NS/2021-22/14404 UNDER SECTION 15-I OF THE SECURITIES AND "
                     "EXCHANGE BOARD OF INDIA ACT, 1992 In respect of: Rajeev Kansal PAN: AAGHR0927R In the matter of "
                     "Dealings in Illiquid Stock Options at the BSE A. BACKGROUND 1. Securities and Exchange Board of "
                     "India (hereinafter referred to as “SEBI”) conducted an investigation")
    assert (p.name, p.pan) == ("Rajeev Kansal", "AAGHR0927R")


def test_numbered_table():  # 46128
    ps = parties_in("In respect of: Noticee No. Noticee Name PAN 1 M/s. Aksh Optifibre Limited AAACA0062F 2 Dr. Kailash "
                    "S Chaudhary AAHPC7797B 3 Mr. P.F. Sundesha AAAPF1710G 4 Mr. B. R. Rakhecha AAEPR7923D 5 Mr. "
                    "Narendra Kumbhat AAJPK8470D 6 Mr. Arun Sood AAJPS3837J In the matter of GDR Issue by Aksh "
                    "Optifibre Limited")
    assert [(p.noticee_no, p.name, p.pan) for p in ps][:2] == [(1, "M/s. Aksh Optifibre Limited", "AAACA0062F"),
                                                             (2, "Dr. Kailash S Chaudhary", "AAHPC7797B")]
    assert len(ps) == 6 and ps[-1].name == "Mr. Arun Sood"


def test_list_separated_by_pans():
    assert names("against M/s Grishma Securities Pvt. Ltd. (PAN:AABCG1300K), Shri Chandrika H. Gandhi (PAN: "
                 "AAMPG4546M), Shri Ketan C. Shah (PAN: AIWPS3413R) and Shri Vanraj Kahor (PAN; AGDPK4812F) in the "
                 "matter of Pyramid Saimira") == ["M/s Grishma Securities Pvt. Ltd.", "Shri Chandrika H. Gandhi",
                                                  "Shri Ketan C. Shah", "Shri Vanraj Kahor"]


def test_an_address_is_not_a_party():  # 43662, 42882
    assert names("In respect of Hindustan Mercantile Ltd PAN: AAACH6220L 4A, Shree Ganesh Business Centre, 216, "
                 "Acharya J.C. Bose Road. Kolkata – 700 017 In the matter of SCORES") == ["Hindustan Mercantile Ltd"]
    assert names("Noticees: MJVVD Prakash (PAN:AGDPM3021C) I-206, Divya Shakthi Complex, Green Lands, Ameerpet, "
                 "Hyderabad- 500 016 P Venkateswara Rao 7/528, Turlapati St., Godugupet, Ward-7, Machilipatnam- "
                 "521001, Andhra Pradesh (PAN: ALEPM3653J) In the matter of Ranklin") \
        == ["MJVVD Prakash", "P Venkateswara Rao"]


def test_a_number_inside_an_address_is_not_a_serial_number():  # 48002
    ps = parties_in("In respect of 6. USG Tech Solutions Ltd (PAN - AABCV0870B) having address at - Level 7 Maximus "
                    "Towers Building 2A Mindspace Complex, Hi-Tech City, Hyderabad – 500081. 7. Symphony Dealcomm Pvt. "
                    "Ltd (PAN - AAMCS0254K) having address at - Rabindra Sarani, Kolkata – 700001. In the matter of "
                    "Aplaya Creations Limited.")
    assert "Symphony Dealcomm Pvt. Ltd" in [p.name for p in ps]
    assert not any("Maximus" in p.name for p in ps)


def test_company_and_its_directors():  # 29471
    ps = parties_in("In respect of 1. Parivar Dairies and Allied Limited (CIN: U01211MP2002PLC015368) and its Directors "
                    "viz., Shri Basant Lal Sharma (DIN: 00594879), Shri Ahibaran Singh (DIN: 00625418) In the matter of "
                    "Parivar Dairies")
    assert [(p.name, p.hint, p.din) for p in ps] == [("Parivar Dairies and Allied Limited", None, None),
                                                   ("Shri Basant Lal Sharma", xp.DIRECTOR, "00594879"),
                                                   ("Shri Ahibaran Singh", xp.DIRECTOR, "00625418")]
    assert ps[0].cin == "U01211MP2002PLC015368"


def test_the_first_paragraph_is_not_a_list_item():  # 40986
    flat = ew.normalise_sentence(
        "SETTLEMENT ORDER In respect of M/s. Parnax Lab Limited (PAN No. AAACK1457E) 1. M/s. Parnax Lab Limited "
        "(hereinafter referred to as the ‘applicant’) filed an application dated July 03, 2017 in terms of the SEBI "
        "(Settlement of Administrative and Civil Proceedings) Regulations, 2014. 2. The High Powered Advisory "
        "Committee considered the application. 3. Accordingly the applicant remitted the amount.")
    ps, _, _ = xp.header_parties(flat, xp.header_end(flat))
    assert [(p.name, p.pan) for p in ps] == [("M/s. Parnax Lab Limited", "AAACK1457E")]


def test_bracketed_former_names_stay_with_their_party():  # 24598, 65412
    assert names("In respect of M/s CIL Nova Petrochemicals Limited and M/s GSL Nova Petrochemicals Limited "
                 "(Erstwhile M/s Nova Petrochemicals Limited) In the matter of Nova") \
        == ["M/s CIL Nova Petrochemicals Limited", "M/s GSL Nova Petrochemicals Limited"]
    [p] = parties_in("In respect of Uttam Financial Services Ltd. (Presently known as Mansukh Stock Brokers Ltd.) "
                     "In the matter of Era Construction India Ltd")
    assert p.name == "Uttam Financial Services Ltd." and p.aliases == ["Mansukh Stock Brokers Ltd"]


@pytest.mark.parametrize("entry, name", [
    ("BHUVAN TRIPURA INDUSTRIES LIMITED AND ITS DIRECTORS", "BHUVAN TRIPURA INDUSTRIES LIMITED"),
    ("M/s S P J Stock Brokers Private Limited. Broker: Bombay Stock Exchange Limited.",
     "M/s S P J Stock Brokers Private Limited"),
    ("Nivedan Fin-Invest Lease Ltd. Page 1 of 2 CO/CFD/82/August/2015", "Nivedan Fin-Invest Lease Ltd."),
    ("M/S BHOOMI INFRASTRUCTURE CORPORATION LIMITED (CONSENT APPLICATION NO. 2266/2011)",
     "M/S BHOOMI INFRASTRUCTURE CORPORATION LIMITED"),
    ("Sajjad Abdul Qadir Pavne (Since Deceased)", "Sajjad Abdul Qadir Pavne"),
    ("Kishan Gopal Mohta [PAN: ADQPM0469E] having address at – 14/2, Old China Bazar Street", "Kishan Gopal Mohta"),
    ("M/s New Fashion Prop. Ketan Vora PAN: ADPPV6108C", "M/s New Fashion"),
    ("Capricon Industrials Ltd. (Acquirer)", "Capricon Industrials Ltd."),
    ("PENTA HOMES PRIVATE LIMITED, AND", "PENTA HOMES PRIVATE LIMITED"),
])
def test_clean_entry(entry, name):
    assert xp.clean_entry(entry) == name


@pytest.mark.parametrize("entry", [
    "having address at – 14/2, Old China Bazar Street",
    "he holds the membership of any stock exchange",
    "ITS TRANSACTIONS IN THE SHARES OF DSQ INDUSTRIES LIMITED",
    "4A, Shree Ganesh Business Centre, 216, Acharya J.C. Bose Road",
    "Mr. K.R.C.V. Seshachalam, Dy. Legal Advisor as the Adjudicating Officer",
    "INB/INF",
    "Noticees",
    "Not Available",
])
def test_not_a_name(entry):
    assert xp.clean_entry(entry) is None


def test_clean_entry_is_linear_on_long_entries():
    # a nested "(…+|…)+$" pattern once took minutes on a 15,000-character entry
    started = time.monotonic()
    xp.clean_entry("Suzlon Energy Limited " + ", ; . " * 3000 + " and x")
    assert time.monotonic() - started < 1


def test_names_identifiers_and_types():
    assert xp.name_clean("M/s. Rana Sugars Ltd.") == "RANA SUGARS LIMITED"
    assert xp.name_clean("Shri Amrut Securities Limited (now known as Amrut Dredging)") == "AMRUT SECURITIES LIMITED"
    assert xp.entity_type("Rajeev Kansal", "AAGHR0927R") == "huf"  # 4th PAN letter H
    assert xp.entity_type("Anything", "AAHPC7797B") == "individual"
    assert xp.entity_type("M/s KV Impex", None) == "company"
    assert xp.entity_type("Anoop Kumar Agarwal and Sons HUF", None) == "huf"
    assert xp.entity_type("P. K. Agarwal & Co.", None) == "firm"


def test_a_misspelt_suffix_still_matches():  # 26894
    rx = xp.name_pattern(xp.name_clean("Nilkanth Dealers Private Limted"))
    assert rx.search("each on Sundeep Credits Pvt. Ltd. and Nilkanth Dealers Pvt. Ltd under Section 15HA")


def test_noticee_numbers():
    [(_, _, nums)] = xp.noticee_numbers("Noticee Nos. 1 to 3 and 5 are restrained")
    assert nums == {1, 2, 3, 5}


@pytest.mark.parametrize("matter, issuer", [
    ("trading activities of certain entities in the scrip of Dinesh Allorga Limited", "Dinesh Allorga Limited"),
    ("IPO of RDB Rasayans Ltd", "RDB Rasayans Ltd"),
    ("GDR Issue by Aksh Optifibre Limited", "Aksh Optifibre Limited"),
    ("dealings in Illiquid Stock Options at BSE", None),
    ("Satyam Computers Services Limited", "Satyam Computers Services Limited"),
])
def test_issuer_from_matter(matter, issuer):
    assert xp.issuer_from_matter(matter) == issuer


def test_title_parties():
    assert xp.title_parties("Adjudication order in respect of Sharp Industries Ltd., Shri Hasmukh Sheth , Shri Vinod "
                            "Sheth and Shri Rajesh Sheth") == ["Sharp Industries Ltd.", "Shri Hasmukh Sheth",
                                                               "Shri Vinod Sheth", "Shri Rajesh Sheth"]
    assert xp.title_parties("Adjudication Order against 3 entities in the matter of Delta Leasing and Finance Ltd.") == []


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

def _party(name, **kw):
    p = xp.Party(name, "header", **kw)
    p.clean = xp.name_clean(name)
    p.entity_type = xp.entity_type(name, p.pan)
    return p


def test_roles():
    assert xp.guess_role(_party("Satellite Corporate Services Private Ltd.", regno="INR000003639"), "", None, None)[0] \
        == xp.INTERMEDIARY
    assert xp.guess_role(_party("M/s Khandelwal Jain & Co., Chartered Accountants"), "", None, None)[0] == xp.AUDITOR
    assert xp.guess_role(_party("Mr. Rana Ranjit Singh", pan="ADXPS5464N"),
                         "Mr. Rana Ranjit Singh, the Chairman and Managing Director of the company", None, None) \
        == (xp.DIRECTOR, "context:chairman")
    assert xp.guess_role(_party("Ms. Pushpa Gupta", din="02698562"), "", None, None) == (xp.DIRECTOR, "din")
    assert xp.guess_role(_party("M/s. Rana Sugars Limited"), "", "Rana Sugars Ltd.", "GDR issue of Rana Sugars Ltd.") \
        == (xp.ISSUER, "matter")
    # a company is never a DIRECTOR
    assert xp.guess_role(_party("OneLife Capital Advisors Limited"), "its Managing Director Mr. Naig", None, None)[0] \
        != xp.DIRECTOR
    assert xp.guess_role(_party("Rajeev Kansal"), "", None, "Dealings in Illiquid Stock Options at the BSE") \
        == (xp.TRADER, "trading_matter")
    assert xp.guess_role(_party("Mr. Lalit Kumar"), "", None, "RDPL Infrastructure Ltd and its directors") \
        == (xp.DIRECTOR, "matter")


# --------------------------------------------------------------------------
# Penalties per party (rows from Day 4's own extraction)
# --------------------------------------------------------------------------

def penalty_rows(sentence):
    found = ea.find_sanctions([(sentence, 0.9)], "t", "AO")
    return ea.penalty_rows([s for s in found if s.kind == ea.PENALTY and s.amount_rupees])


def amounts(parties, sentence):
    link = xp.link_penalties(parties, penalty_rows(sentence))
    return {parties[i].name: a for i, a in link.amounts.items()}, link


def test_table_layout_names_the_party_before_its_amount():  # 54851
    ps = [_party("K Lifestyle and Industries Limited", noticee_no=1), _party("Mr. Narayan Ghumatkar", noticee_no=2),
          _party("Mr. Jaiprakash Mishra", noticee_no=3), _party("Mr. Sunil Vaid", noticee_no=4),
          _party("Mr. Pravin Kumar", noticee_no=5), _party("Mr. Anil Rao", noticee_no=6),
          _party("Mr. Rajendra Pathak", noticee_no=7)]
    got, link = amounts(ps, "(v) The Noticee no. 1, 2, 3, 5 and 7 are hereby imposed with, the following penalties as "
                            "specified: Notice e No. Name of Noticee Provisions under which penalty imposed Penalties 1 K "
                            "Lifestyle and Industries Limited Section 23H of SCRA, Section 15HB and Section 15A(a) of "
                            "SEBI Act, 1992 Rs. 30,00,000/- (Rupees Thirty Lakh) and Rs. 10,00,000/- (Rupees Ten Lakh) 2 "
                            "Mr. Narayan Ghumatkar Section 15HB of SEBI Act, 1992 Rs. 3,00,000/- (Rupees Three Lakh) 3 "
                            "Mr. Jaiprakash Mishra Section 15HB of SEBI Act, 1992 Rs. 15,00,000/- (Rupees Fifteen Lakh) "
                            "5 Mr. Pravin Kumar Section 15HB of SEBI Act, 1992 Rs. 3,00,000/- (Rupees Three Lakh) 7 Mr. "
                            "Rajendra Pathak Section 15HB of SEBI Act, 1992 Rs. 6,00,000/- (Rupees Six Lakh)")
    assert got == {"K Lifestyle and Industries Limited": 4_000_000, "Mr. Narayan Ghumatkar": 300_000,
                   "Mr. Jaiprakash Mishra": 1_500_000, "Mr. Pravin Kumar": 300_000, "Mr. Rajendra Pathak": 600_000}
    assert link.n_linked == link.n_rows == 6 and link.unassigned == 0


def test_prose_names_the_party_after_its_amounts():  # 24441
    ps = [_party("ISF Securities Ltd"), _party("Vishwas Securities Limited")]
    got, link = amounts(ps, "I hereby impose a penalty of ₹ 10,00,000 (Rupees Ten Lakhs Only) under Section 15HA of the "
                            "Act and ₹ 1,00,000 (Rupees One Lakh Only) under Section 15HB of the Act on ISF Securities "
                            "Limited (ISF) and ₹ 10,00,000 (Rupees Ten Lakhs Only) under Section 15HA of the Act on "
                            "Vishwas Securities Limited (VSL) thus, a total of ₹ 21,00,000 (Rupees Twenty One Lakhs "
                            "only) on the Noticees.")
    assert got == {"ISF Securities Ltd": 1_100_000, "Vishwas Securities Limited": 1_000_000}
    assert link.n_linked == link.n_rows == 3


def test_noticee_numbers_in_prose():
    ps = [_party("Mr. A Kumar", noticee_no=1), _party("Mr. B Shah", noticee_no=2)]
    got, _ = amounts(ps, "I hereby impose a penalty of Rs. 5,00,000/- (Rupees Five Lakh only) on Noticee No. 1 and "
                         "Rs. 2,00,000/- (Rupees Two Lakh only) on Noticee No. 2 under Section 15HB of the SEBI Act.")
    assert got == {"Mr. A Kumar": 500_000, "Mr. B Shah": 200_000}


def test_each_is_paid_by_every_party_named():  # 26894
    ps = [_party("Sundeep Credits Private Limited"), _party("Nilkanth Dealers Private Limted"),
          _party("Indo Thai Securities Limited")]
    got, link = amounts(ps, "I hereby impose a penalty of ₹ 5,00,000/- (Rupees Five lakhs Only) each on Sundeep Credits "
                            "Pvt. Ltd. and Nilkanth Dealers Pvt. Ltd under Section 15HA of the Act.")
    assert got == {"Sundeep Credits Private Limited": 500_000, "Nilkanth Dealers Private Limted": 500_000}
    assert link.expanded_total == 1_000_000 and link.each == {0, 1}


def test_a_penalty_on_all_the_noticees_is_joint():  # 27451
    ps = [_party("Kautilya Mutual Benefit Fund Ltd."), _party("Kautilya Laboratories Pvt. Ltd.")]
    got, link = amounts(ps, "I hereby impose a penalty of ₹ 3,00,000/- (Rupees Three Lakh Only) on all the Noticees for "
                            "the non compliance of the provisions of regulations 8(1) and 8(2) of the SAST Regulations.")
    assert got == {"Kautilya Mutual Benefit Fund Ltd.": 300_000, "Kautilya Laboratories Pvt. Ltd.": 300_000}
    assert link.joint == {0, 1} and link.expanded_total == 300_000


def test_one_party_takes_every_row():
    got, link = amounts([_party("Rajeev Kansal")], "I hereby impose a penalty of Rs. 5,00,000/- (Rupees Five Lakh "
                                                   "only) on the Noticee under Section 15HA of the SEBI Act.")
    assert got == {"Rajeev Kansal": 500_000} and link.n_linked == 1


# --------------------------------------------------------------------------
# One order end to end
# --------------------------------------------------------------------------

ORDER = (
    "BEFORE THE ADJUDICATING OFFICER SECURITIES AND EXCHANGE BOARD OF INDIA [ADJUDICATION ORDER NO. Order/AB/2021-22/1] "
    "UNDER SECTION 15-I OF THE SECURITIES AND EXCHANGE BOARD OF INDIA ACT, 1992 READ WITH RULE 5 OF SEBI (PROCEDURE "
    "FOR HOLDING INQUIRY AND IMPOSING PENALTIES) RULES, 1995 In respect of: Noticee No. Noticee Name PAN 1 M/s. Aksh "
    "Optifibre Limited AAACA0062F 2 Dr. Kailash S Chaudhary AAHPC7797B In the matter of GDR Issue by Aksh Optifibre "
    "Limited\n\nBACKGROUND\n1. Securities and Exchange Board of India (hereinafter referred to as “SEBI”) observed that "
    "Aksh Optifibre Limited (hereinafter referred to as “the Company”), whose shares are listed on BSE, issued GDRs. "
    "Dr. Kailash S Chaudhary was the Managing Director of the Company.\n"
    "2. The Noticees violated Section 12A(a), (b) and (c) of the SEBI Act read with Regulations 3 and 4 of the SEBI "
    "(Prohibition of Fraudulent and Unfair Trade Practices relating to Securities Market) Regulations, 2003 "
    "(hereinafter referred to as “PFUTP Regulations”).\n"
    + "".join(f"{n}. The Noticees submitted their replies and the matter was heard on the date fixed for it.\n"
              for n in range(3, 12)) +
    "ORDER\n12. I hereby impose a penalty of Rs. 10,00,000/- (Rupees Ten Lakh only) on Aksh Optifibre Limited and "
    "Rs. 5,00,000/- (Rupees Five Lakh only) on Dr. Kailash S Chaudhary under Section 15HA of the SEBI Act.\n")


def test_process_one_order(tmp_path):
    (tmp_path / "derived" / "text").mkdir(parents=True)
    (tmp_path / "derived" / "text" / "1.txt").write_text(ORDER, encoding="utf-8")
    a_job = ea.Job("1", 1, "AO", None, "derived/text/1.txt", "OK", None)
    actions, _, sanctions = ea.process(a_job, tmp_path)
    job = xp.Job("1", 1, "AO", None, "derived/text/1.txt", "OK", "Adjudication Order in the matter of GDR Issue by "
                 "Aksh Optifibre Limited", [asdict(s) for s in sanctions], actions.penalty_total,
                 actions.penalty_total_basis)
    o, entities, mentions = xp.process(job, tmp_path)
    assert o.error is None
    assert (o.matter_raw, o.issuer_guess, o.n_parties) == ("GDR Issue by Aksh Optifibre Limited",
                                                           "Aksh Optifibre Limited", 2)
    got = [(e.entity_name, e.pan, e.entity_role, e.penalty_amount, e.sanction_types) for e in entities]
    assert got == [("M/s. Aksh Optifibre Limited", "AAACA0062F", xp.ISSUER, 1_000_000, '["PENALTY"]'),
                   ("Dr. Kailash S Chaudhary", "AAHPC7797B", xp.DIRECTOR, 500_000, '["PENALTY"]')]
    assert entities[0].is_issuer_guess and not entities[1].is_issuer_guess
    assert o.penalty_link_status == "all_rows" and o.penalty_unassigned == 0
    assert '"SEBI Act 12A(a)"' in o.regulations and '"PFUTP 3"' in o.regulations
    assert o.penalty_provisions == '["SEBI Act 15HA"]'
    assert {m.role for m in mentions} >= {"violation", "power", "penalty"}


def test_orders_without_text_are_kept(tmp_path):
    o, entities, mentions = xp.process(xp.Job("x", None, "WTM", None, None, "NO_SOURCE"), tmp_path)
    assert (o.text_status, o.n_parties, entities, mentions) == ("NO_SOURCE", 0, [], [])


@pytest.mark.parametrize("entry, name", [
    ("Shri Ketan Parekh and some entities associated with him (hereinafter referred to as KP Group)",
     "Shri Ketan Parekh"),
    ("SHAREDEAL FINANCIAL CONSULTANTS PVT. LTD. M/s Sharedeal Financial Consultants Pvt. Ltd. (hereinafter",
     "SHAREDEAL FINANCIAL CONSULTANTS PVT. LTD."),
])
def test_where_a_name_stops(entry, name):
    assert xp.clean_entry(entry) == name
