"""Tests for the rupee normaliser. The strings are copied from corpus orders
(runbook Week 2, Day 4: "unit-test it on ten real strings")."""

import pytest

from src import amounts as am


@pytest.mark.parametrize("text, rupees, words", [
    ("₹ 5,00,000/- (Rupees Five Lakh only)", 500_000, 500_000),
    ("Rs.10,00,000/- (Rupees Ten Lakh)", 1_000_000, 1_000_000),
    ("Rs. 26,00,00,000", 260_000_000, None),                     # 2-2-3 grouping
    ("` 25,00,000/- (Rupees Twenty Five Lakh only)", 2_500_000, 2_500_000),  # backtick rupee
    ("Rs.1,00.000/- (Rupees One Lakh)", 100_000, 100_000),       # dot typo
    ("Rs. 8, 52, 500/- as settlement", 852_500, None),           # spaces inside the figure
    ("INR 1,20,000/-", 120_000, None),
    ("₹ 12.82 lakh", 1_282_000, None),
    ("Rs. 765 crore", 7_650_000_000, None),
    ("Rs.1,23,55,840", 12_355_840, None),
    ("`2,45,250/- (Rupees Two Lakh Forty Five Thousand Two Hundred and Fifty only)", 245_250, 245_250),
    ("Rs. 5,00,000 (Rs. Five Lakhs only)", 500_000, 500_000),
    ("₹54,61,127.", 5_461_127, None),
    ("Rs. 26,000,000", 26_000_000, None),                        # western grouping
    ("Rs 5,00,000.00", 500_000, None),                           # paise
    ("Rs. 2 crore (Rupees Two Crore Only)", 20_000_000, 20_000_000),
])
def test_real_amount_strings(text, rupees, words):
    [a] = am.find_amounts(text)
    assert (a.rupees, a.words_rupees) == (rupees, words)
    assert a.words_agree in (None, True)


def test_words_disagreeing_with_the_figure_are_visible():
    [a] = am.find_amounts("Rs. 5,00,000/- (Rupees Fifty Lakh only)")
    assert (a.rupees, a.words_rupees, a.words_agree) == (500_000, 5_000_000, False)


def test_several_amounts_in_one_sentence():
    s = ("I hereby impose a penalty of Rs.1,00,000/- (Rupees One Lakh) under section 15A(b) and "
         "Rs.2,00,000/-(Rupees Two Lakh) under section 15HB of SEBI Act")
    assert [a.rupees for a in am.find_amounts(s)] == [100_000, 200_000]


@pytest.mark.parametrize("words, n", [
    ("Rupees Twenty Five Lakh Fifty Thousand only", 2_550_000),
    ("One Crore Twenty Lakh", 12_000_000),
    ("Rupees Twenty-Five Thousand", 25_000),
    ("Rupees One Lakh and Fifty Thousand only", 150_000),
    ("Rupees Fourty Lakh", 4_000_000),
    ("Rupees Ten Lakh lakhs only", 1_000_000),       # 41213: the scale repeated
    ("Rupees Nine lakhs only", 900_000),
    # 30700: SEBI's ₹7,269 crore penalty on PACL, in words
    ("Rs Seven Thousand Two Hundred Sixty Nine Crore Forty Nine Lakhs Seventy Thousand Two Hundred and Ninety Five",
     72_694_970_295),
    ("Two Thousand Four Hundred Twenty Three Crore Sixteen Lakhs Fifty Six Thousand Seven Hundred and Sixty Five",
     24_231_656_765),
    ("Rupees Five Lakh only as settlement", None),   # a non-number word
])
def test_words(words, n):
    assert am.words_to_number(words) == n


@pytest.mark.parametrize("text, months", [
    ("for a period of two years from", 24),
    ("for a period of 4 (four) years", 48),
    ("for a period of 06 (six) months", 6),
    ("for a period of one (1) year", 12),
    ("for a period of 5 years.", 60),
    ("for a period of three months", 3),
    ("until further orders", None),
])
def test_durations(text, months):
    assert am.duration_months(text) == months


def test_bare_figures_in_a_penalty_table():
    # 41198 / 43785: no rupee mark in the table
    s = ("I hereby impose a penalty on the Noticees as under: SHIL Sections 15HA 1, 00, 00,000/- (Rupees One Crore "
         "Only) Shri Satish Kumar 5, 00,000/- (Rupees Five Lakhs Only) Total 1, 10, 00,000/- (Rupees One Crore Ten "
         "Lakh Only)")
    got = [(a.rupees, a.words_agree) for a in am.find_amounts(s, bare=True)]
    assert got == [(10_000_000, True), (500_000, True), (11_000_000, True)]
    assert am.find_amounts(s) == []  # never without bare=True
    # a regulation number is not money
    assert am.find_amounts("Regulation 3(a), 4(1) and 4(2)(a), 2003", bare=True) == []


def test_rupee_mark_followed_by_a_dot():
    [a] = am.find_amounts("a penalty of `.3,00,000/- (Rupees three lakh only )")
    assert (a.rupees, a.words_agree) == (300_000, True)


def test_a_space_does_not_join_numbers():
    assert [a.rupees for a in am.find_amounts("a penalty of Rs. 5,00,000 12 noticees")] == [500_000]


def test_non_amounts_are_ignored():
    assert am.find_amounts("Regulation 3(a) of the Regulations, 2003 and Rule 5") == []
    assert am.find_amounts("Rs. per share") == []


@pytest.mark.parametrize("text, rupees, words", [
    # AUDIT_REPORT.md H-1: figures without grouping kept only their first 1-3 digits
    ("Rs. 500000 (Rupees five lakhs only)", 500_000, 500_000),        # 10087 (was ₹500)
    ("Rs.30000/- (Rupees Thirty Thousand Only)", 30_000, 30_000),     # 10170 (was ₹300)
    ("INR 19908933.85 as mentioned in Table no. 4", 19_908_933, None),  # 101405 (was ₹199); paise dropped
    ("₹1258.88 crore jointly and severally", 12_588_800_000, None),   # 30647 Satyam (was ₹125)
    ("Rs.1,10000/- (Rupees one lacs ten thousand only)", 110_000, 110_000),  # 20061, malformed grouping
    ("Rs. 5.00.000/- (Rupees Five Lakh)", 500_000, 500_000),          # dots for commas
])
def test_whole_figures_are_read(text, rupees, words):
    [a] = am.find_amounts(text)
    assert (a.rupees, a.words_rupees) == (rupees, words)


def test_a_registration_number_is_not_money():
    # 36011: "INR200004031" is an RTA's SEBI registration number
    assert am.find_amounts("registered as RTA vide registration no. INR200004031, PAN No. AACCB8463B") == []
    assert [a.rupees for a in am.find_amounts("a penalty of INR 2,00,000/-")] == [200_000]


def test_two_amounts_separated_by_a_comma_stay_two():
    assert [a.rupees for a in am.find_amounts("Rs 2,00,000, Rs 3,00,000 and Rs. 5,00,000")] == [200_000, 300_000, 500_000]
