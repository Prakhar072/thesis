"""Tests for the finality classifier (src/classify_finality.py)."""
from __future__ import annotations

import pandas as pd

from src import classify_finality as F


def test_title_labels():
    assert F.title_label("SETTLEMENT", "Settlement Order in respect of X") == F.SETTLEMENT
    assert F.title_label("AO", "Adjudication Order in respect of X") == F.FINAL
    assert F.title_label("AO", "Corrigendum to Adjudication Order") == F.OTHER
    assert F.title_label("WTM", "Interim Order - Rahul HiRise Limited") == F.INTERIM
    assert F.title_label("WTM", "Ad-interim ex-parte order in the matter of X") == F.INTERIM
    assert F.title_label("WTM", "Confirmatory Order in the matter of X") == F.CONFIRMATION
    assert F.title_label("WTM", "Revocation Order in the matter of X") == F.CONFIRMATION
    assert F.title_label("WTM", "Final Order in the matter of X") == F.FINAL
    assert F.title_label("WTM", "Exemption Order under Regulation 11 of SEBI (SAST)") == F.OTHER
    assert F.title_label("WTM", "Order in the matter of X") is None


def body(head: str, tail: str) -> str:
    """An order text with a given opening and operative part.

    Args:
        head (str): the opening.
        tail (str): the operative part.

    Returns:
        str: the text, padded so the tail falls in the last 40%.
    """
    return head + " " + "background " * 800 + tail


def test_text_rules_in_order():
    assert F.text_label(body("FINAL ORDER under sections 11 and 11B", "hereby restrain"))[0] == F.FINAL
    label, rule, raw = F.text_label(body("ORDER", "the directions issued vide interim order are hereby confirmed"))
    assert (label, rule) == (F.CONFIRMATION, "tail_confirm")
    assert F.text_label(body("ORDER", "These directions shall remain in force until further orders."))[0] == F.INTERIM
    assert F.text_label(body("ORDER", "This order shall be treated as a show cause notice."))[0] == F.INTERIM
    assert F.text_label(body("ORDER", "debarred from the securities market for a period of five years"))[0] == F.FINAL
    assert F.text_label(body("ORDER", "I hereby cancel the certificate of registration"))[1] == "tail_final_weak"
    assert F.text_label(body("ORDER IN THE MATTER OF PROPOSED ACQUISITION OF SHARES OF X", "granted"))[0] == F.OTHER
    assert F.text_label(body("ORDER", "Place: Mumbai"))[0] == F.UNKNOWN


def test_confirmation_beats_pending_investigation():
    text = body("ORDER", "the interim directions are hereby confirmed pending completion of investigation")
    assert F.text_label(text)[0] == F.CONFIRMATION


def test_classify_prefers_title_and_falls_back_to_text():
    orders = pd.DataFrame({"text_stem": ["1", "2", "3", "4"], "order_type": ["WTM", "WTM", "AO", "WTM"],
                           "title": ["Interim Order - X", "Order in the matter of Y", "Adjudication order", "Order"],
                           "text_path": ["a", "b", "c", None]})
    texts = {"a": body("ORDER", "for a period of two years"), "b": body("ORDER", "until further orders"),
             "c": body("ORDER", "penalty")}
    out = F.classify(orders, texts.get).set_index("text_stem")
    assert out.loc["1", "finality"] == F.INTERIM and out.loc["1", "finality_source"] == "title"
    assert out.loc["1", "text_label"] == F.FINAL      # kept for the silver check
    assert out.loc["2", "finality"] == F.INTERIM and out.loc["2", "finality_source"] == "text"
    assert out.loc["3", "finality"] == F.FINAL and out.loc["3", "finality_source"] == "type"
    assert out.loc["4", "finality"] == F.UNKNOWN
