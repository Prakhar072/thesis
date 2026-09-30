"""Tests for the regulation citation parser (runbook Week 2, Day 5). Sentences
are copied from corpus orders."""

import pytest

from src import regulations as rg


def cites(sentence, aliases=None):
    found, unresolved = rg.find_citations(sentence, aliases if aliases is not None else rg.find_aliases(sentence))
    return [(c.regime, c.provision, c.role) for c in found], unresolved


def test_pfutp_list_with_its_alias_and_the_sebi_act():
    s = ("the Noticee has violated Regulations 3(a), (b), (c), (d), 4(1) and 4(2)(a), (e) of the SEBI (Prohibition of "
         "Fraudulent and Unfair Trade Practices relating to Securities Market) Regulations, 2003 (hereinafter referred "
         "to as \"PFUTP Regulations\") and Section 12A(a), (b), (c) of the SEBI Act read with Section 15HA of SEBI Act")
    aliases = rg.find_aliases(s)
    assert aliases["pfutp regulations"] == rg.Instrument("PFUTP", 2003, "title")
    found, _ = rg.find_citations(s, aliases)
    assert rg.as_list(found) == ["PFUTP 3(a)", "PFUTP 3(b)", "PFUTP 3(c)", "PFUTP 3(d)", "PFUTP 4(1)",
                                 "PFUTP 4(2)(a)", "PFUTP 4(2)(e)", "SEBI Act 12A(a)", "SEBI Act 12A(b)",
                                 "SEBI Act 12A(c)"]
    assert rg.as_list(found, roles=("penalty",)) == ["SEBI Act 15HA"]


def test_read_with_a_different_instrument_is_its_own_citation():
    got, unresolved = cites("for the alleged violation of Section 12A(a), (b) and (c) read with Regulations 3 and 4 of "
                            "PFUTP Regulations and Clause 49 of the Listing Agreement", {})
    assert got == [("SEBI_ACT", "12A(a)", "violation"), ("SEBI_ACT", "12A(b)", "violation"),
                   ("SEBI_ACT", "12A(c)", "violation"), ("PFUTP", "3", "violation"), ("PFUTP", "4", "violation"),
                   ("LISTING_AGREEMENT", "49", "violation")]
    assert unresolved == 0


def test_the_preamble_is_power_not_violation():
    got, _ = cites("ORDER UNDER SECTIONS 11(1), 11(4), 11A AND 11B OF THE SECURITIES AND EXCHANGE BOARD OF INDIA ACT, "
                   "1992 READ WITH REGULATION 11 OF PFUTP REGULATIONS", {})
    assert got == [("SEBI_ACT", "11(1)", "power"), ("SEBI_ACT", "11(4)", "power"), ("SEBI_ACT", "11A", "power"),
                   ("SEBI_ACT", "11B", "power"), ("PFUTP", "11", "power")]
    got, _ = cites("UNDER SECTION 23-I OF SECURITIES CONTRACTS (REGULATION) ACT, 1956 READ WITH RULE 5 OF SECURITIES "
                   "CONTRACTS (REGULATION) (PROCEDURE FOR HOLDING INQUIRY AND IMPOSING PENALTIES) RULES, 2005", {})
    assert got == [("SCRA", "23I", "power"), ("ADJUDICATION_RULES", "5", "power")]
    got, _ = cites("ORDER UNDER REGULATIONS 33A AND 33B OF THE SEBI (INTERMEDIARIES) REGULATIONS, 2008", {})
    assert got == [("INTERMEDIARIES", "33A", "power"), ("INTERMEDIARIES", "33B", "power")]


def test_penalty_sections_suffixes_and_ocr():
    got, _ = cites("I hereby impose a penalty of Rs.3,00,000/- under the provisions of Section 15 A (a) of the SEBI Act, "
                   "1992 conferred upon me u/s 15- I (2) of the SEBI Act", {})
    assert got == [("SEBI_ACT", "15A(a)", "penalty"), ("SEBI_ACT", "15I(2)", "power")]
    got, _ = cites("in exercise of the powers under Section 151 of the SEBI Act", {})
    assert got == [("SEBI_ACT", "15I", "power")]  # OCR: "15I" read as "151"


def test_summons_under_11c_is_the_duty_violated():
    got, _ = cites("the Noticee failed to comply with summons issued under Section 11C(2) and 11C(3) of SEBI Act and is "
                   "liable under Section 15A(a)", {})
    assert got == [("SEBI_ACT", "11C(2)", "violation"), ("SEBI_ACT", "11C(3)", "violation"),
                   ("SEBI_ACT", "15A(a)", "penalty")]


def test_settlement_wording_and_an_unresolved_citation_in_between():
    got, _ = cites("proposing to settle the delayed compliance of Regulation 29(1) read with 29(3) of the SEBI "
                   "(Substantial Acquisition of Shares and Takeovers) Regulations, 2011.", {})
    assert got == [("SAST", "29(1)", "violation"), ("SAST", "29(3)", "violation")]
    got, unresolved = cites("and adjudge under Section 15HB of the Act, the alleged violation of the provisions of "
                            "Clause 1.2 of Model code of Conduct in Part A, Schedule I provided under Regulation 12(1) "
                            "of the PIT Regulations.", {})
    assert got == [("SEBI_ACT", "15HB", "penalty"), ("PIT", "12(1)", "violation")]
    assert unresolved == 1


def test_background_citations_are_cited_only():
    got, _ = cites("The present application is filed under Regulation 11(1) of the Takeover Regulations seeking "
                   "exemption from the obligation to make an open offer", {})
    assert got == [("SAST", "11(1)", "cited")]


def test_generic_names_resolve_through_the_orders_own_alias():
    text = ("Securities and Exchange Board of India (Prohibition of Insider Trading) Regulations, 2015 (hereinafter "
            "referred to as 'the Regulations'). The Noticee violated Regulation 7(2)(a) of the Regulations.")
    aliases = rg.find_aliases(text)
    got, _ = cites("The Noticee violated Regulation 7(2)(a) of the Regulations.", aliases)
    assert got == [("PIT", "7(2)(a)", "violation")]
    assert cites("The Noticee violated Regulation 7(2)(a) of the Regulations.", {}) == ([], 1)


@pytest.mark.parametrize("name, code", [
    ("Prohibition of Fraudulent and Unfair Trade Practices relating to Securities Market", "PFUTP"),
    ("Substantial Acquisition of Shares and Takeovers", "SAST"),
    ("Prohibition of Insider Trading", "PIT"),
    ("Issue of Capital and Disclosure Requirements", "ICDR"),
    ("Listing Obligations and Disclosure Requirements", "LODR"),
    ("Stock Brokers and Sub-Brokers", "STOCK_BROKERS"),
    ("Intermediaries", "INTERMEDIARIES"),
    ("Procedure for Holding Inquiry and Imposing Penalties by Adjudicating Officer", "ADJUDICATION_RULES"),
    ("Procedure for Holding Enquiry by Enquiry Officer and Imposing Penalty", "ENQUIRY_REGS"),
    ("Settlement of Administrative and Civil Proceedings", "SETTLEMENT"),
    ("Depositories and Participants", "DEPOSITORIES_PARTICIPANTS"),
    ("Registrars to an Issue and Share Transfer Agents", "RTA"),
    ("Issue and Listing of Debt Securities", "DEBT_LISTING"),
    ("Collective Investment Schemes", "CIS"),
    ("Investment Advisers", "INVESTMENT_ADVISERS"),
    ("Disclosure and Investor Protection", "DIP"),
])
def test_regime_titles(name, code):
    assert rg.regime_of(name) == code


def test_expand_provision_lists():
    assert rg.expand("3(a), (b), 4(1) and 4(2)(a), (e)") == ["3(a)", "3(b)", "4(1)", "4(2)(a)", "4(2)(e)"]
    assert rg.expand("15 HA and 15HB") == ["15HA", "15HB"]


def test_numbers_that_are_not_provisions():
    assert cites("the SEBI (Prohibition of Insider Trading) Regulations, 2015 came into force", {}) == ([], 0)
    got, _ = cites("SECTION 11 AND 11B OF THE SEBI ACT", {})
    assert [p for _, p, _ in got] == ["11", "11B"]  # "11 AND" is not section "11AND"


def test_sast_1997_directions_are_power():
    got, _ = cites("in exercise of the powers under Regulation 44 of the SEBI (Substantial Acquisition of Shares "
                   "and Takeovers) Regulations, 1997", {})
    assert got == [("SAST", "44", "power")]


def test_recovery_boilerplate_is_not_a_violation():
    got, _ = cites("In the event of failure to pay the said amount of penalty within 45 days of the receipt of this "
                   "Order, recovery proceedings may be initiated under Section 28A of the SEBI Act", {})
    assert got == [("SEBI_ACT", "28A", "power")]


def test_the_15j_explanation_lists_powers_not_penalties():
    got, _ = cites("it is clarified that the power of an adjudicating officer to adjudge the quantum of penalty under "
                   "sections 15A to 15E, clauses (b) and (c) of section 15F, 15G, 15H and 15HA shall be and shall "
                   "always be deemed to have been exercised under the provisions of this section", {})
    assert {r for _, _, r in got} == {"power"} and ("SEBI_ACT", "15G", "power") in got
    got, _ = cites("I hereby impose a penalty of Rs. 10,00,000 under Section 15G of the SEBI Act", {})
    assert got == [("SEBI_ACT", "15G", "penalty")]
