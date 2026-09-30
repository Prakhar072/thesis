"""Tests for the detection-channel stage (runbook Week 3, Day 2; hand/coding_protocol.md §5).
Sentences are taken or adapted from corpus orders; the order ids are in the comments."""

import json

import pytest

from src import detect_channel as dc


def first(sentences):
    h = dc.choose(dc.find_triggers(sentences))
    return (h.channel, h.detail) if h else None


@pytest.mark.parametrize("sentences, expected", [
    # protocol worked examples
    (["Securities and Exchange Board of India (hereinafter referred to as ‘SEBI’), on receipt of an alert generated "
      "from its surveillance system, conducted an examination in the scrip of Nivedita Mercantile & Financing Ltd."],
     (dc.SURVEILLANCE, "alert")),                                                                          # 39468
    (["Securities and Exchange Board of India (“SEBI”) conducted an examination in the matter of AGI Greenpac Limited "
      "based on the complaints received from Soneko Marketing (P) Ltd."], (dc.COMPLAINT, "complaint")),    # 83117
    (["Following a news article titled \"Ranbaxy to sell balance 13.02% stake in Orchid Chem\" published in DNA Money "
      "on September 18, 2010, SEBI conducted investigation into the trading in the shares of Orchid."],
     (dc.MEDIA, "news")),                                                                                  # 23871
    (["SEBI conducted an investigation in the scrip of Twilight Litaka Pharma Limited pursuant to receipt of a "
      "reference from Income Tax Department, Pune alleging booking of bogus purchases."], (dc.REFERRAL, "tax")),  # 66742
    (["An investigation was carried out by SEBI into the affairs of Satyam Computer Services Ltd. after receipt of an "
      "email from Mr. B Ramalinga Raju, Ex-Chairman of SCSL, inter alia, admitting and confessing to the company’s "
      "balance sheet having recorded non-existent bank balances."], (dc.SELF_DISCLOSURE, "confession")),  # 79496
    (["Securities and Exchange Board of India (SEBI) had conducted an inspection of the books of accounts and other "
      "records of B R Jalan Securities Pvt Ltd."], (dc.INSPECTION, "sebi_inspection")),                   # 4891
    (["Based on the report received from NSE, Securities and Exchange Board of India (SEBI) conducted investigation "
      "in the scrip of the PVP Ventures Ltd."], (dc.UNCLEAR, "exchange_reference")),                        # 29459
])
def test_worked_examples(sentences, expected):
    assert first(sentences) == expected


def test_an_observed_trading_pattern_is_flagged_surveillance():
    # 44956: the investigation is five sentences on; "SEBI’)," — quote, bracket and comma before the verb
    s = ["Securities and Exchange Board of India (hereinafter referred to as ‘SEBI’), observed large scale reversal of "
         "trades in Illiquid Stock Options segment of Bombay Stock Exchange.",
         "Reversal trades were considered those trades in which an entity reverse its buy or sell positions.",
         "The said reversal trades are alleged to be non-genuine trades.", "x.", "y.",
         "SEBI conducted an investigation into the trading activities of certain entities in ISO, BSE."]
    hits = dc.find_triggers(s)
    assert (hits[0].channel, hits[0].detail, hits[0].confidence) == (dc.SURVEILLANCE, "trading_pattern_observed", "medium")


def test_a_pattern_needs_a_tie_to_the_start():
    tied = ["The Securities and Exchange Board of India conducted investigations into the dealings in the scrip of "
            "Parsoli Corporation Limited whose shares witnessed abnormal increase in the price and volume traded."]  # 783
    alone = ["The scrip opened at Rs.73/- and witnessed a steep price fall and closed at Rs.55.05.",
             "SEBI conducted an investigation into the IPO of the company."]                                     # 46040
    assert first(tied) == (dc.SURVEILLANCE, "trading_pattern_observed")
    assert first(alone) is None


def test_a_complaint_received_by_sebi_needs_no_investigation_cue():
    s = ["The Securities and Exchange Board of India (‘SEBI’) received two complaints – one against Market Gainer vide "
         "e-mail dated July 07, 2020.", "The complaints, inter alia, alleged as follows.", "MG had charged Rs. 2,000."]
    assert first(s) == (dc.COMPLAINT, "complaint")


def test_what_is_not_a_trigger():
    # 76787: a noticee inspecting SEBI's documents after the notice
    assert first(["The inspection of documents was conducted on October 13, 2022 in the presence of AR of Noticees."]) is None
    # negated
    assert first(["SEBI conducted an investigation; no complaints were received against the company."]) is None
    # a noticee's submission (19618)
    assert first(["The authorised representative of the Noticee appeared and submitted the following: his client "
                  "traded based on electronic and print media reports and the investigation was flawed."]) is None
    # a finding of the investigation (14812)
    assert first(["SEBI conducted an investigation into the scrip of KDIL.",
                  "During the investigation it was observed that there was an abnormal price rise."]) is None
    # a cited judgment (58460)
    assert first(["In this connection, the Noticees place reliance on the Orders of the Hon’ble Supreme Court in the "
                  "matter of SEBI Vs Bhavesh Pabari and the investigation."]) is None
    # SEBI's own press release (16578)
    assert first(["Pursuant to the press release and the public notice issued by SEBI on November 26, 1997, the company "
                  "filed certain information and SEBI examined it."]) is None


def test_the_trigger_is_stated_before_the_findings():
    s = ["SEBI conducted an investigation into the trading in the scrip of X Ltd."] + [f"Finding {i}." for i in range(12)] \
        + ["Pursuant to the complaint of the noticee's client, the investigation was widened."]
    assert first(s) is None


def test_an_exchange_reference_gives_way_to_its_stated_basis():
    s = ["Based on a reference from BSE, SEBI examined the matter.",
         "BSE had received complaints from investors about the company, which SEBI examined."]
    assert first(s) == (dc.COMPLAINT, "complaint")
    # the reference itself states what BSE saw (41495)
    s = ["Securities and Exchange Board of India (hereinafter referred to as ‘SEBI’) received a reference from BSE and "
         "as per the reference, the holding of the Promoter of AIL, Mr. Abhishek Mehta, had changed.",
         "SEBI examined the disclosures."]
    assert first(s) == (dc.SURVEILLANCE, "filings_observed")


def test_suo_motu():
    assert first(["The Applicants filed a suo-motu settlement application proposing to settle the enforcement "
                  "proceedings, without admitting the findings of the examination."]) == (dc.SELF_DISCLOSURE, "self_report")
    assert first(["SEBI suo motu initiated an examination into the dealings in the scrip."]) == (dc.UNCLEAR, "suo_motu")


def test_referral_details():
    s = ["MCA vide letter dated June 09, 2017 forwarded a list of 331 suspected shell companies and SEBI examined them."]
    assert first(s) == (dc.REFERRAL, "mca_roc")
    s = ["SEBI received an investigation report from Serious Fraud Investigation Office (SFIO), in the matter of IFIN, "
         "and conducted an examination."]
    assert first(s) == (dc.REFERRAL, "sfio")


def test_the_model_answer_is_verified():
    sents = ["In view of the aforesaid unusual price movement noticed in the shares, SEBI conducted an investigation.",
             "SEBI conducted investigations into the dealings in the shares of Prosoft."]
    ok = json.dumps({"channel": "SURVEILLANCE", "sentence": 1, "quote": "unusual price movement noticed"})
    assert dc.read_answer(ok, sents)[0] == "MODEL_CHANNEL"
    # the quote must be in the sentence …
    bad = json.dumps({"channel": "SURVEILLANCE", "sentence": 1, "quote": "a sharp price rise was seen"})
    assert dc.read_answer(bad, sents)[0] == "MODEL_UNVERIFIED"
    # … and carry the cue of the channel it names (dev set: the model turned "SEBI conducted investigations" into SURVEILLANCE)
    no_cue = json.dumps({"channel": "SURVEILLANCE", "sentence": 2, "quote": "SEBI conducted investigations into the dealings"})
    assert dc.read_answer(no_cue, sents) == ("MODEL_UNVERIFIED", dc.SURVEILLANCE, 2, "no SURVEILLANCE cue in the quote")
    assert dc.read_answer(json.dumps({"channel": "UNCLEAR", "sentence": 0, "quote": ""}), sents)[0] == "MODEL_UNCLEAR"
    assert dc.read_answer("not json", sents)[0] == "MODEL_ERROR"


def test_only_cue_bearing_sentences_go_to_the_model():
    sents = ["SEBI conducted investigations into the dealings in the shares of Prosoft.",
             "It came to the notice that a news item had appeared about the company."]
    assert dc.model_sentences(sents) == [sents[1]]


def test_orders(tmp_path):
    no_text = dc.Job("x", None, "AO", None, "NO_SOURCE", None)
    assert dc.process(no_text).detection_channel is None
    (tmp_path / "t.txt").write_text("ORDER\n\nSEBI conducted an investigation into the trading in the scrip of X Ltd. "
                                    "The investigation revealed synchronised trades.\n", encoding="utf-8")
    o = dc.process(dc.Job("t", 1, "AO", None, "OK", "t.txt"), tmp_path)
    assert (o.detection_channel, o.detection_detail, o.detection_method, o.detection_confidence) == \
        (dc.UNCLEAR, "none", "none", "low")
