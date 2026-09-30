"""Tests for the fraud-category auto-coder (runbook Week 3, Day 1; hand/coding_protocol.md §3).
Provision lists are the protocol's worked examples; narratives are phrases from corpus orders."""

import json

import pytest

from src import classify_fraud as cf


def job(regulations=(), title="", penalty_raw=None, cited=()):
    return cf.Job("x", 1, "AO", "p", "OK", title, None, json.dumps(list(regulations)), json.dumps(list(cited)),
                  penalty_raw)


@pytest.mark.parametrize("regime, provision, category", [
    ("PIT", "3(1)", cf.INSIDER_TRADING), ("PIT", "4(1)", cf.INSIDER_TRADING), ("PIT", "3A", cf.INSIDER_TRADING),
    ("PIT", "3(5)", cf.DISCLOSURE_FAILURE),          # structured digital database
    ("PIT", "13(3)", cf.DISCLOSURE_FAILURE), ("PIT", "7(2)(a)", cf.DISCLOSURE_FAILURE),
    ("SEBI Act", "12A(d)", cf.INSIDER_TRADING), ("SEBI Act", "12A(a)", cf.NARRATIVE),
    ("SEBI Act", "11C(3)", cf.OTHER), ("SEBI Act", "12(1)", cf.INTERMEDIARY_MISCONDUCT),
    ("SEBI Act", "12(1B)", cf.OTHER),                 # collective investment scheme
    ("LODR", "23(4)", cf.RPT_TUNNELLING), ("LODR", "23(9)", cf.DISCLOSURE_FAILURE), ("LODR", "33(1)", cf.DISCLOSURE_FAILURE),
    ("SAST", "29(1)", cf.DISCLOSURE_FAILURE), ("Companies Act", "188", cf.RPT_TUNNELLING),
    ("Companies Act", "73", cf.OTHER), ("ICDR", "57(1)", cf.ISSUE), ("CIS", "3", cf.OTHER), ("SCRR", "19A", cf.OTHER),
    ("STOCK_BROKERS", "9(g)", cf.INTERMEDIARY_MISCONDUCT),
    ("DEPOSITORIES_PARTICIPANTS", "55A", cf.DISCLOSURE_FAILURE),   # the issuer's share-capital audit
    ("PFUTP", "4(2)(a)", cf.NARRATIVE),
])
def test_provisions(regime, provision, category):
    assert cf.category_of(regime, provision)[0] == category


@pytest.mark.parametrize("regime, provision", [("PIT", "2(1)(g)"), ("PIT", "2(e)"), ("LODR", "2(1)(zb)"),
                                               ("Companies Act", "2(76)"), ("SAST", "2(1)(q)(1)"), ("ICDR", "2")])
def test_definitions_are_not_charges(regime, provision):
    assert cf.category_of(regime, provision) is None


@pytest.mark.parametrize("regs, category", [
    # the protocol's worked examples (hand/coding_protocol.md §3)
    (["PIT 2(1)(g)", "SEBI Act 12A(d)", "PIT 3(1)", "SEBI Act 12A(e)", "PIT 4(1)"], cf.INSIDER_TRADING),   # 75894
    (["LODR 23(4)"], cf.RPT_TUNNELLING),                                                                  # 82086
    (["SAST 29(1)", "SAST 29(3)"], cf.DISCLOSURE_FAILURE),                                                # 45046
    (["STOCK_BROKERS 9(g)"], cf.INTERMEDIARY_MISCONDUCT),                                                 # 65476
    (["SEBI Act 11C(3)"], cf.OTHER),                                                                      # 48699
    (["SEBI Act 12(1B)", "CIS 3"], cf.OTHER),                                                             # 25029
])
def test_worked_examples(regs, category):
    o = cf.classify(job(regs), "")
    assert o.fraud_category == category and o.fraud_category_confidence == "high"


def test_pfutp_is_decided_by_the_narrative():
    regs = ["PFUTP 3(a)", "PFUTP 4(1)", "SEBI Act 12A(a)"]
    manip = ("executing Synchronised and Reversal trades with other members of the group and thereby contributed in "
             "creation of artificial volume and price rise above the last traded price")
    assert cf.classify(job(regs), manip).fraud_category == cf.MARKET_MANIPULATION           # 39418
    accounts = ("OTL has manipulated its books of accounts/ financial statement for the FY18 to FY 20 by booking "
                "fictitious sales and purchases and thereafter published false and misleading financial statements; "
                "the misstatement inflated revenue")
    assert cf.classify(job(regs), accounts).fraud_category == cf.ACCOUNTING_MISSTATEMENT     # 99823
    diversion = ("TPL had diverted substantial part of the IPO proceeds, through layered transactions, to certain "
                 "entities; the IPO proceeds were not used for the objects of the issue")
    assert cf.classify(job(regs), diversion).fraud_category == cf.FUND_DIVERSION             # 23691


def test_examining_books_is_not_a_misstatement():
    # a broker inspection that looks at books of accounts and financial statements
    text = ("The inspection team examined the books of accounts and financial statements of the broker, "
            "and the books of accounts showed non-segregation of client funds and misuse of client's funds.")
    scores, _ = cf.narrative_scores(text)
    assert scores[cf.ACCOUNTING_MISSTATEMENT] == 0
    assert cf.read_narrative(scores)[0] != cf.ACCOUNTING_MISSTATEMENT


@pytest.mark.parametrize("text, category", [
    ("certain entities through stock exchange mechanism generated bogus Long Term Capital Gain (LTCG) in many scrips",
     cf.MARKET_MANIPULATION),                                                             # 43974
    ("collected advisory fees from clients promising assured returns while acting as an unregistered investment "
     "adviser, without obtaining a certificate of registration; investment advisory services", cf.INTERMEDIARY_MISCONDUCT),
    ("Osian has been held to be operating a collective investment scheme; collective investment schemes; the company "
     "mobilised funds from the public under the collective investment scheme", cf.OTHER),   # 50313
])
def test_narratives(text, category):
    scores, _ = cf.narrative_scores(text)
    assert cf.read_narrative(scores)[0] == category


def test_precedence_and_confidence():
    o = cf.classify(job(["PIT 4(1)", "SAST 29(1)"]), "")
    assert (o.fraud_category, o.fraud_category_confidence) == (cf.INSIDER_TRADING, "medium")
    assert json.loads(o.fraud_categories_all) == [cf.INSIDER_TRADING, cf.DISCLOSURE_FAILURE]
    # a broker charged under PFUTP for synchronised trades: the fraud, not the kind of noticee
    o = cf.classify(job(["PFUTP 4(2)(a)", "STOCK_BROKERS 7"]), "synchronised trades and reversal trades in the scrip")
    assert o.fraud_category == cf.MARKET_MANIPULATION


def test_pfutp_without_a_telling_narrative_uses_the_clauses():
    assert cf.classify(job(["PFUTP 4(2)(a)", "PFUTP 4(2)(e)"]), "").fraud_category_rule == "pfutp:default"
    o = cf.classify(job(["PFUTP 4(2)(f)", "PFUTP 4(2)(k)"]), "")
    assert (o.fraud_category, o.fraud_category_rule) == (cf.DISCLOSURE_FAILURE, "pfutp:misleading_information")


def test_fallbacks():
    o = cf.classify(job(penalty_raw="I hereby impose a penalty of Rs. 1,00,000 under Section 15C of the SEBI Act"), "")
    assert (o.fraud_category, o.fraud_category_rule, o.fraud_category_confidence) == \
        (cf.OTHER, "fallback:investor_grievances", "low")
    o = cf.classify(job(title="Exemption Order under Regulation 11 of SEBI (SAST) Regulations, 2011 in the matter of X"), "")
    assert (o.fraud_category, o.fraud_category_rule) == (cf.OTHER, "fallback:no_violation")
    o = cf.classify(job(title="Order against X Ltd, member BSE"), "")
    assert o.fraud_category == cf.INTERMEDIARY_MISCONDUCT
    o = cf.classify(job(), "The Noticee was heard.")
    assert (o.fraud_category, o.fraud_category_rule, o.fraud_category_confidence) == (cf.OTHER, "unresolved", "low")


def test_orders_without_text_get_no_category(tmp_path):
    o = cf.process(cf.Job("x", None, "WTM", None, "NO_SOURCE", None, None, "[]", "[]", None), tmp_path)
    assert o.fraud_category is None
