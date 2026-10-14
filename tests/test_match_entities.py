"""Tests for matching order companies to the listed universe (Week 4 Day 2)."""

import json

import pandas as pd
import pytest

from src import match_entities as me
from src.names import norm


def _aliases(rows):
    """rows: (firm_id, name) → name_aliases-shaped frame."""
    return pd.DataFrame([{"firm_id": f, "alias": n, "alias_norm": norm(n), "source": "bse_name"} for f, n in rows])


#: Enough names per word (≥ GENERIC_DF) for these words to count as generic.
FILLER = [(f"INF{j}{i:03d}", f"Filler{chr(65 + j)}{i} {w}") for j, w in enumerate(
    ["Paper Mills", "Financial Services", "Technologies", "Industries", "Securities"]) for i in range(me.GENERIC_DF)]


def _match(q, rows):
    m = me.match_names([norm(q)], _aliases(rows + FILLER))
    return m.iloc[0]


def test_exact_and_fuzzy_auto():
    rows = [("INE641C", "Kavveri Telecom Products Ltd"), ("INE165H", "Pyramid Saimira Theatre Ltd")]
    r = _match("M/s. Kavveri Telecom Products Limited", rows)
    assert (r.firm_id, r.band, r.reason, r.score) == ("INE641C", "auto", "exact", 100)
    r = _match("Pyramid SaimiraTheatre Ltd", rows)
    assert (r.firm_id, r.band, r.reason) == ("INE165H", "auto", "fuzzy")


def test_names_sharing_only_generic_words_are_rejected():
    r = _match("Satia Paper Mills Ltd", [("INE001A", "Star Paper Mills Limited")])
    assert r.band == "reject" and r.firm_id is None


def test_a_different_first_word_goes_to_review():
    r = _match("IDFC Asset Management Company Ltd", [("INE127D", "HDFC Asset Management Company Limited")])
    assert (r.band, r.reason) == ("review", "first word differs")


def test_two_firms_at_auto_level_go_to_review():
    r = _match("Acme Steel Ltd", [("INE001A", "Acme Steels Ltd"), ("INE002B", "Acme Steelz Ltd")])
    assert (r.band, r.reason) == ("review", "two firms ≥ 92")


def test_same_name_prefers_the_firm_with_an_isin():
    r = _match("Akar Laminators Ltd", [("INE984C", "Akar Laminators Ltd"), ("BSE:500005", "Akar Laminators Ltd")])
    assert (r.firm_id, r.band) == ("INE984C", "auto")
    r = _match("Akar Laminators Ltd", [("INE984C", "Akar Laminators Ltd"), ("INE111Z", "Akar Laminators Limited")])
    assert (r.band, r.reason) == ("review", "same name, several firms")


def test_fuzzy_80_to_92_is_review():
    r = _match("Datsons Labs Ltd", [("INE001A", "Dr. Datsons Labs Limited")])
    assert r.band == "review" and 80 <= r.score < 92


def test_query_names_strip_matter_descriptors():
    assert me.query_names("Audit Report of ACIL Cotton Industries")[-1] == "ACIL COTTON INDUSTRIES"
    assert me.query_names("SCORES against N R I Investments Limited")[-1] == "N R I INVESTMENTS"
    assert me.query_names("Kavveri Telecom Products Ltd") == ["KAVVERI TELECOM PRODUCTS"]


def test_private_companies_are_never_listed():
    for n in ("Allied Financial Services Pvt. Ltd", "XYZ Private Limited", "ABC (P) Ltd", "Some Advisors LLP"):
        assert me.PRIVATE.search(n)
    assert not me.PRIVATE.search("Privilege Industries Ltd")


def test_codes_in_text():
    t = "ISIN: INE641C01019 … the scrip (BSE Scrip Code: 590107) … NSE code KAVVERI"
    assert me.codes_in_text(t) == (["INE641C01019"], ["590107"])
    universe = pd.DataFrame({"exchange": ["BSE", "NSE"], "code": ["590107", "KAVVERI"],
                             "firm_id": ["INE641C", "INE641C"]})
    assert me.code_firms(["INE641C01027"], ["590107", "999999"], me.code_lookup(universe)) == ["INE641C"]


def test_decisions_from_the_review_sheet():
    matches = pd.DataFrame({"query_norm": ["A", "B", "C"],
                            "candidates": [json.dumps([{"firm_id": "INE001A", "score": 85, "alias": "A"}])] * 3})
    review = pd.DataFrame({"query_norm": ["A", "B", "C", "D"], "decision": ["accept", "reject", "INE009Z", ""]})
    assert me.decisions(review, matches) == {"A": "INE001A", "B": None, "C": "INE009Z"}


def _universe():
    return pd.DataFrame({
        "exchange": ["BSE", "NSE", "BSE"], "code": ["590107", "KAVVERI", "533302"],
        "symbol": ["KAVVERI", "KAVVERI", "TAKSHEEL"], "isin": ["INE641C01019", "INE641C01019", "INE889I01019"],
        "firm_id": ["INE641C", "INE641C", "INE889I"], "status": ["Active", "Active", "Delisted"]})


def test_match_orders_end_to_end(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "1.txt").write_text("BSE Scrip Code: 533302")
    (tmp_path / "t" / "2.txt").write_text("nothing")
    (tmp_path / "t" / "3.txt").write_text("nothing")
    orders = pd.DataFrame({"text_stem": ["1", "2", "3"],
                           "issuer_guess": [None, "Kavveri Telecom Products Ltd", "Allied Finance Pvt Ltd"],
                           "title": ["t1", "t2", "t3"], "text_path": ["t/1.txt", "t/2.txt", "t/3.txt"]})
    entities = pd.DataFrame({"text_stem": ["2", "2"], "party_no": [1, 2], "entity_type": ["company", "individual"],
                             "entity_name": ["Kavveri Telecom Products Limited", "A Person"],
                             "entity_name_clean": ["KAVVERI TELECOM PRODUCTS LIMITED", "A PERSON"],
                             "entity_role": ["ISSUER", "TRADER"]})
    aliases = _aliases([("INE641C", "Kavveri Telecom Products Ltd"), ("INE889I", "Taksheel Solutions Ltd")] + FILLER)
    oi, em, matches, usage = me.match_orders(orders, entities, _universe(), aliases,
                                             me.read_review(tmp_path / "none.csv"), text_dir_root=tmp_path)
    oi = oi.set_index("text_stem")
    assert (oi.loc["1", "issuer_firm_id"], oi.loc["1", "match_method"]) == ("INE889I", "code")  # no name, one code
    assert oi.loc["1", "issuer_ticker"] == "533302"                                               # BSE only
    assert (oi.loc["2", "issuer_firm_id"], oi.loc["2", "match_method"]) == ("INE641C", "exact")
    assert oi.loc["2", "issuer_ticker"] == "KAVVERI" and oi.loc["2", "issuer_isin"] == "INE641C01019"
    assert oi.loc["3", "match_method"] == "private" and pd.isna(oi.loc["3", "issuer_firm_id"])
    assert len(em) == 1 and em.iloc[0].firm_id == "INE641C"                                      # companies only


def test_hand_decisions_win(tmp_path):
    orders = pd.DataFrame({"text_stem": ["1"], "issuer_guess": ["Datsons Labs Ltd"], "title": ["t"],
                           "text_path": [None]})
    entities = pd.DataFrame(columns=["text_stem", "party_no", "entity_type", "entity_name", "entity_name_clean",
                                     "entity_role"])
    aliases = _aliases([("INE001A", "Dr. Datsons Labs Limited")] + FILLER)
    universe = pd.DataFrame({"exchange": ["BSE"], "code": ["500001"], "symbol": ["DATSONS"],
                             "isin": ["INE001A01011"], "firm_id": ["INE001A"], "status": ["Delisted"]})
    oi, *_ = me.match_orders(orders, entities, universe, aliases, me.read_review(tmp_path / "x.csv"))
    assert oi.iloc[0].match_method == "review_pending" and oi.iloc[0].candidate_firm_id == "INE001A"
    review = pd.DataFrame({"query_norm": ["DATSONS LABS"], "decision": ["accept"]})
    oi, *_ = me.match_orders(orders, entities, universe, aliases, review)
    assert (oi.iloc[0].issuer_firm_id, oi.iloc[0].match_method) == ("INE001A", "hand")
    review = pd.DataFrame({"query_norm": ["DATSONS LABS"], "decision": ["reject"]})
    oi, *_ = me.match_orders(orders, entities, universe, aliases, review)
    assert pd.isna(oi.iloc[0].issuer_firm_id) and oi.iloc[0].match_method == "hand_reject"


def test_review_rows_are_appended_never_rewritten():
    matches = pd.DataFrame({"query_norm": ["A", "B"], "band": ["review", "review"], "score": [85, 88],
                            "reason": ["fuzzy 80–92"] * 2,
                            "candidates": [json.dumps([{"firm_id": "INE001A", "score": 85, "alias": "A"}])] * 2})
    usage = pd.DataFrame({"query_norm": ["A", "B"], "n_orders": [1, 5], "n_parties": [0, 0],
                          "query": ["a", "b"], "example_title": ["ta", "tb"]})
    review = pd.DataFrame([{**dict.fromkeys(me.REVIEW_COLUMNS, ""), "query_norm": "A", "decision": "reject"}])
    new = me.new_review_rows(matches, usage, review, {"INE001A": "Firm A"}, {"INE001A": "Active"})
    assert new.query_norm.tolist() == ["B"] and new.decision.tolist() == [""]
    assert "INE001A Firm A [Active] 85" in new.candidates.iloc[0]


@pytest.mark.parametrize("name, expected", [
    ("Lumax Industries Limited", "LUMAX INDUSTRIES"),
    ("J. R. Organics Limited {earlier known as Somaiya Organics Ltd}", "SOMAIYA ORGANICS"),
])
def test_query_names_include_bracketed_names(name, expected):
    assert expected in me.query_names(name)
