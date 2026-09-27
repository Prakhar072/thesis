"""Tests for the model fallback (window_model) and the model cache (llm).

No model server is needed: a fake Ollama client returns scripted answers, so
these tests check what the code does *around* the model — caching, checking
the quotes against the sentences, reading the dates, and the output tables.
"""

import datetime as dt
import json
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src import extract_windows as ew
from src import llm
from src import window_model as wm

D = dt.date

UPSI = ("The investigation observed that Ms. Amla Narain had purchased the shares of CRISIL "
        "Limited during the period when price sensitive information was not public (i.e. "
        "during May 01, 2013 to June 02, 2013) and sold the same immediately.")
BANK = "The bank statement of the company was analysed for the period from 24.04.2010 to 30.06.2017."


class FakeClient:
    """Stands in for ollama.Client: scripted answers, one per chat call."""

    def __init__(self, answers, digest="sha256:abc"):
        self.answers = list(answers)
        self.digest = digest
        self.calls = []

    def list(self):
        return SimpleNamespace(models=[SimpleNamespace(model=wm.DEFAULT_MODEL, digest=self.digest)])

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return SimpleNamespace(message=SimpleNamespace(content=content, thinking=None),
                               prompt_eval_count=100, eval_count=20)


@pytest.fixture
def fake_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "LLM_CACHE", tmp_path / "llm")
    monkeypatch.setattr(llm, "OFFLINE", False)
    monkeypatch.setattr(llm, "MODEL_CALLS", 0)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    holder = {}

    def install(answers, digest="sha256:abc"):
        client = FakeClient(answers, digest)
        monkeypatch.setattr(llm, "_client", lambda: client)
        holder["client"] = client
        return client
    return install


SPEC = llm.ModelSpec(wm.DEFAULT_MODEL, "sha256:abc", options=wm.OPTIONS)


def answer(kind="investigation", sentence=1, start=None, end=None):
    return {"kind": kind, "sentence": sentence, "start_text": start, "end_text": end}


# --------------------------------------------------------------------------
# llm: the cache
# --------------------------------------------------------------------------

def test_second_call_is_served_from_disk(fake_llm):
    client = fake_llm([answer("none", 0)])
    first = llm.chat_json(SPEC, "sys", "user", wm.SCHEMA)
    second = llm.chat_json(SPEC, "sys", "user", wm.SCHEMA)
    assert (first["from_cache"], second["from_cache"]) == (False, True)
    assert first["content"] == second["content"]
    assert len(client.calls) == 1 and llm.MODEL_CALLS == 1
    assert client.calls[0]["format"] == wm.SCHEMA and client.calls[0]["think"] is False


def test_cache_key_changes_with_weights_prompt_and_thinking():
    base = llm.cache_key(SPEC, "sys", "user", wm.SCHEMA)
    assert llm.cache_key(llm.ModelSpec(SPEC.name, "sha256:other", options=SPEC.options),
                         "sys", "user", wm.SCHEMA) != base
    assert llm.cache_key(SPEC, "sys v2", "user", wm.SCHEMA) != base
    assert llm.cache_key(llm.ModelSpec(SPEC.name, SPEC.digest, think=True, options=SPEC.options),
                         "sys", "user", wm.SCHEMA) != base
    # option order does not matter
    assert llm.cache_key(llm.ModelSpec(SPEC.name, SPEC.digest,
                                       options=dict(reversed(list(SPEC.options.items())))),
                         "sys", "user", wm.SCHEMA) == base


def test_offline_miss_raises(fake_llm, monkeypatch):
    fake_llm([])
    monkeypatch.setattr(llm, "OFFLINE", True)
    with pytest.raises(llm.CacheMiss):
        llm.chat_json(SPEC, "sys", "user", wm.SCHEMA)


def test_failed_calls_are_retried_then_reported(fake_llm):
    client = fake_llm([ConnectionError("down"), answer("none", 0)])
    assert llm.chat_json(SPEC, "sys", "user")["from_cache"] is False
    assert len(client.calls) == 2
    fake_llm([ConnectionError("down")] * 3)
    with pytest.raises(llm.LlmError):
        llm.chat_json(SPEC, "sys", "other user")


def test_model_digest_names_missing_models(fake_llm):
    fake_llm([])
    assert llm.model_digest(wm.DEFAULT_MODEL) == "sha256:abc"
    with pytest.raises(llm.LlmError, match="not served"):
        llm.model_digest("no-such-model")


# --------------------------------------------------------------------------
# Reading quoted dates
# --------------------------------------------------------------------------

def test_quotes_are_matched_ignoring_spacing_and_punctuation():
    assert wm.quoted_in("April 01, 2014", "for the period April 01 ,2014 to …")
    assert wm.quoted_in("01.04.2014", "from 01-04-2014 to")
    assert not wm.quoted_in("April 02, 2014", "for the period April 01, 2014 to …")
    assert not wm.quoted_in("", "anything")


@pytest.mark.parametrize("text, start, end, precision", [
    ("April 01, 2014", D(2014, 4, 1), D(2014, 4, 1), "day"),
    ("05.11.2019", D(2019, 11, 5), D(2019, 11, 5), "day"),       # day-first
    ("January 2017", D(2017, 1, 1), D(2017, 1, 31), "month"),
    ("July | 08, 2002", D(2002, 7, 8), D(2002, 7, 8), "day"),     # 23751: OCR bar
    ("FY 2011-12", D(2011, 4, 1), D(2012, 3, 31), "fy"),
    ("financial years 2000-2001", D(2000, 4, 1), D(2001, 3, 31), "fy"),
    ("2008", D(2008, 1, 1), D(2008, 12, 31), "year"),
    ("quarter ending June 30, 2016", D(2016, 6, 30), D(2016, 6, 30), "day"),
])
def test_resolve_date(text, start, end, precision):
    r = wm.resolve_date(text)
    assert (r.start, r.end, r.precision) == (start, end, precision)


@pytest.mark.parametrize("text, start, end, precision", [
    # a day range inside one quote is those days, not the whole year
    ("January 21-25, 2008", D(2008, 1, 21), D(2008, 1, 25), "day"),
    ("May 8–9, 2014", D(2014, 5, 8), D(2014, 5, 9), "day"),
    ("February 14 - 15 of 2014", D(2014, 2, 14), D(2014, 2, 15), "day"),
    ("21-25 January 2008", D(2008, 1, 21), D(2008, 1, 25), "day"),
    # a whole range quoted as one date (7900, 17053, 48993)
    ("May 3 to May 20, 2004", D(2004, 5, 3), D(2004, 5, 20), "day"),
    ("May-June 1998", D(1998, 5, 1), D(1998, 6, 30), "month"),
    ("May- July 2008", D(2008, 5, 1), D(2008, 7, 31), "month"),
    ("December 1999 – March 2000", D(1999, 12, 1), D(2000, 3, 31), "month"),
    # spans of years
    ("2003-05", D(2003, 1, 1), D(2005, 12, 31), "year"),
    ("2013-2017", D(2013, 1, 1), D(2017, 12, 31), "year"),
    ("the year 1992", D(1992, 1, 1), D(1992, 12, 31), "year"),
    ("2003 to 2014", D(2003, 1, 1), D(2014, 12, 31), "year"),
    # 33576: a list of financial years runs from the first to the last
    ("Financial Years 2011–12, 2012–13 and 2013–14", D(2011, 4, 1), D(2014, 3, 31), "fy"),
    # a bare "2011-12" stays a financial year
    ("2011-12", D(2011, 4, 1), D(2012, 3, 31), "fy"),
])
def test_quotes_holding_a_span(text, start, end, precision):
    a, b = wm.resolve_date(text, "start"), wm.resolve_date(text, "end")
    assert (a.start, b.end, a.precision) == (start, end, precision)


@pytest.mark.parametrize("text", ["31/04/2003", "Match 30, 2000", "IP", "the date of inspection"])
def test_malformed_dates_are_not_widened_to_a_year(text):
    assert wm.resolve_date(text, "start") is None and wm.resolve_date(text, "end") is None


def test_dates_in_a_trade_log_are_rejected():
    s = ("NARESH CHAND CHANDAK 5/11/1999 15:02:15 25000 742.00 DINESH KUMAR SINGHANIA & CO. "
         "15:02:10 25000 742.00 SANJAY KHEMANI 10/15/1999 14:06:29 6500 1156.50")
    assert _reason(s, "5/11/1999", "5/11/1999") == "row of a trade log"


def test_resolve_date_rejects_garbage():
    assert wm.resolve_date("30.1.20100") is None
    assert wm.resolve_date("the date of inspection") is None


# --------------------------------------------------------------------------
# Checking an answer
# --------------------------------------------------------------------------

def _read(ans, sentences):
    return wm.read_answer(json.dumps(ans), sentences)


def test_verified_answer_becomes_a_window():
    r = _read(answer("violation", 1, "May 01, 2013", "June 02, 2013"), [UPSI])
    assert (r.status, r.kind, r.start, r.end) == (wm.MODEL_WINDOW, "violation", D(2013, 5, 1), D(2013, 6, 2))


def test_hallucinated_dates_are_rejected():
    r = _read(answer("investigation", 1, "May 01, 2012", "June 02, 2013"), [UPSI])
    assert (r.status, r.start) == (wm.MODEL_UNVERIFIED, None)


def test_wrong_sentence_number_is_corrected():
    r = _read(answer("violation", 1, "May 01, 2013", "June 02, 2013"), [BANK, UPSI])
    assert (r.status, r.sentence_no) == (wm.MODEL_WINDOW, 2)


def test_none_and_bad_json():
    assert _read(answer("none", 0), [BANK]).status == wm.MODEL_NONE
    assert wm.read_answer("not json", [BANK]).status == wm.MODEL_ERROR
    assert wm.read_answer('{"kind": "investigation"}', [BANK]).status == wm.MODEL_ERROR


def test_missing_or_backwards_dates_are_rejected():
    s = ["The investigation period was from 01.04.2012 to 31.03.2013."]
    assert _read(answer("investigation", 1, "01.04.2012", None), s).status == wm.MODEL_UNVERIFIED
    r = _read(answer("investigation", 1, "31.03.2013", "01.04.2012"), s)
    assert (r.status, r.note) == (wm.MODEL_UNVERIFIED, "start after end")


def test_zero_padding_does_not_fail_a_correct_quote():
    # 103720: the model wrote "April 01, 2018" for "April 1, 2018".
    s = ["the period of inspection was from April 1, 2018 till the date of inspection, February 15, 2020."]
    r = _read(answer("investigation", 1, "April 01, 2018", "February 15, 2020"), s)
    assert (r.status, r.start, r.end) == (wm.MODEL_WINDOW, D(2018, 4, 1), D(2020, 2, 15))


def test_a_quoted_phrase_is_read_from_its_first_and_last_dates():
    # 15039: the model quoted the whole phrase as the start.
    phrase = "financial years 2000-2001, 2001-2002 and April 2002 till May 21, 2002"
    assert wm.resolve_date(phrase, "start").start == D(2000, 4, 1)
    assert wm.resolve_date(phrase, "end").end == D(2002, 5, 21)
    assert wm.resolve_date("1999-2000 and 2000-2001", "end").end == D(2001, 3, 31)


def _reason(sentence, start_text, end_text):
    a = wm.resolve_date(start_text, "start").start
    b = wm.resolve_date(end_text, "end").end
    return wm.false_window_reason(sentence, start_text, end_text, a, b)


@pytest.mark.parametrize("sentence, start, end, reason", [
    # 76872: e-mail dates turned into a range
    ("communications took place vide email dated August 27, 2021, August 28, 2021 and August 30, 2021.",
     "August 27, 2021", "August 30, 2021", "dates of correspondence"),
    # 10262: the inspection visit, not the period it covered
    ("During the inspection of the UPSE conducted by SEBI from November 08, 2001 to November 09, 2001, "
     "it was observed that 14 members were involved.", "November 08, 2001", "November 09, 2001",
     "dates of an inspection visit"),
    # 16441: an eight-day visit
    ("SEBI conducted an inspection of books and records of M/s Mansukh Securities and Finance Limited "
     "(hereinafter referred to as 'Noticee') Member, National Stock Exchange during October 06, 2004 to "
     "October 13, 2004.", "October 06, 2004", "October 13, 2004", "dates of an inspection visit"),
    # 33725: the period of a bank statement
    ("Bank account statement of Operator Tips Global (Account No. 915020038321666) for the period "
     "January 01, 2016 to June 3, 2016.", "January 01, 2016", "June 3, 2016",
     "period of a statement or ledger"),
])
def test_known_false_windows_are_rejected(sentence, start, end, reason):
    assert _reason(sentence, start, end) == reason


@pytest.mark.parametrize("sentence, start, end", [
    ("the period of inspection was from April 1, 2018 till February 15, 2020.", "April 1, 2018",
     "February 15, 2020"),
    ("SEBI conducted an inspection of the broker for the period from April 1, 2000 to October 31, 2002.",
     "April 1, 2000", "October 31, 2002"),
    ("SEBI had conducted investigations in to the price movement in the shares of Granules India Ltd. "
     "during December 19, 2002 to January 23, 2003.", "December 19, 2002", "January 23, 2003"),
    # SEBI's usual wording: "conducted investigation during" = the activity period, not desk time
    ("SEBI conducted an investigation during February 5, 2003 to June 6, 2003 into the trading.",
     "February 5, 2003", "June 6, 2003"),
    ("The investigation in the irregular trading activities was conducted during the period May 1, 2008 "
     "to September 30, 2009.", "May 1, 2008", "September 30, 2009"),
    ("fees were collected for unregistered services during the period November 01, 2019 to April 10, "
     "2023: Bank Name Bank Account Number", "November 01, 2019", "April 10, 2023"),
    # 48388: bank credits over a period are the unregistered activity itself
    ("1,471 credit transactions have been made in the bank accounts of the firm during the period "
     "October 21, 2013 to November 24, 2020.", "October 21, 2013", "November 24, 2020"),
])
def test_real_windows_pass_the_rejection_rules(sentence, start, end):
    assert _reason(sentence, start, end) is None


def test_rejected_answers_keep_their_reason():
    s = ["The inspection was conducted by SEBI from November 08, 2001 to November 09, 2001."]
    r = _read(answer("investigation", 1, "November 08, 2001", "November 09, 2001"), s)
    assert (r.status, r.start) == (wm.MODEL_REJECTED, None)


def test_an_end_alone_is_not_a_period():
    # 48274: "failed to comply ... till March 2014"
    s = ["the Noticee consistently failed to comply with the SEBI Circulars till March 2014."]
    r = _read(answer("violation", 1, "till March 2014", "till March 2014"), s)
    assert (r.status, r.note) == (wm.MODEL_UNVERIFIED, "open-ended quote")


def test_single_day_single_month_and_years():
    day = ["SEBI conducted investigation into the price fall on June 13, 2013 (Investigation period)."]
    r = _read(answer("investigation", 1, "June 13, 2013", "June 13, 2013"), day)
    assert (r.start, r.end) == (D(2013, 6, 13), D(2013, 6, 13))
    month = ["SEBI conducted examination in the scrip in the period January 2017."]
    r = _read(answer("investigation", 1, "January 2017", "January 2017"), month)
    assert (r.start, r.end, r.end_precision) == (D(2017, 1, 1), D(2017, 1, 31), "month")
    years = ["special purpose inspection conducted on May 08, 2014, period of inspection being 2008 to 2013"]
    r = _read(answer("investigation", 1, "2008", "2013"), years)
    assert (r.start, r.end, r.start_precision) == (D(2008, 1, 1), D(2013, 12, 31), "year")


def test_start_without_a_year_takes_the_ends():
    s = ["The trades were executed from November 15 to December 15, 2006."]
    r = _read(answer("violation", 1, "November 15", "December 15, 2006"), s)
    assert (r.start, r.end) == (D(2006, 11, 15), D(2006, 12, 15))
    s = ["The trades were executed from November to February 2012."]
    r = _read(answer("violation", 1, "November", "February 2012"), s)
    assert (r.start, r.end) == (D(2011, 11, 1), D(2012, 2, 29))


# --------------------------------------------------------------------------
# Orders, tables and the CLI
# --------------------------------------------------------------------------

def test_ask_records_the_source_sentence(fake_llm):
    fake_llm([answer("violation", 2, "May 01, 2013", "June 02, 2013")])
    order = wm.Order("42451", 42451, "SETTLEMENT", D(2019, 3, 22), ew.UNCONFIRMED_RANGE, [BANK, UPSI])
    row = wm.ask(order, SPEC)
    assert row.model_status == wm.MODEL_WINDOW and row.violation_raw == UPSI
    assert row.window_kind == wm.CONDUCT_DATES and row.cache_key and row.from_cache is False
    assert json.loads(row.raw_response)["kind"] == "violation"  # the model's own word is kept


def test_ask_survives_a_dead_server(fake_llm):
    fake_llm([ConnectionError("down")] * 3)
    order = wm.Order("1", 1, "AO", None, ew.UNPARSED, [BANK])
    row = wm.ask(order, SPEC)
    assert row.model_status == wm.MODEL_ERROR and "failed" in row.note


def test_prompt_clips_table_sized_sentences():
    user = wm.build_user(["x" * 5000, "short"])
    assert "[1] " + "x" * wm.MAX_SENTENCE_CHARS + " …" in user and "[2] short" in user


def _windows_rows():
    base = dict(order_type="AO", order_date=D(2020, 1, 1), text_status="OK", n_sentences=10,
                start_precision=None, end_precision=None, violation_raw=None,
                violation_confidence="low", violation_method=None, violation_rule=None,
                cand_rank=None, char_start=None, rules_version=ew.RULES_VERSION, error=None,
                violation_start=None, violation_end=None)
    return [
        {**base, "text_stem": "1", "order_id": 1, "n_candidates": 1, "window_status": ew.PARSED,
         "violation_start": D(2014, 4, 1), "violation_end": D(2015, 9, 30),
         "start_precision": "day", "end_precision": "day", "violation_raw": "regex sentence",
         "violation_confidence": "high", "violation_method": "regex", "violation_rule": "range",
         "cand_rank": 1, "char_start": 10},
        {**base, "text_stem": "2", "order_id": 2, "n_candidates": 2, "window_status": ew.UNCONFIRMED_RANGE},
        {**base, "text_stem": "3", "order_id": 3, "n_candidates": 1, "window_status": ew.UNPARSED},
        {**base, "text_stem": "4", "order_id": 4, "n_candidates": 0, "window_status": ew.NO_CANDIDATES},
    ]


def _write_inputs(tmp_path):
    windows = tmp_path / "windows.parquet"
    pq.write_table(pa.Table.from_pylist(_windows_rows(), schema=ew.WINDOWS_SCHEMA), windows)
    cands = [
        {"text_stem": "1", "cand_rank": 1, "sentence": "regex sentence"},
        {"text_stem": "2", "cand_rank": 2, "sentence": UPSI},
        {"text_stem": "2", "cand_rank": 1, "sentence": BANK},
        {"text_stem": "3", "cand_rank": 1, "sentence": "The investigation period was March 2009."},
    ]
    full = [{name: c.get(name) for name in ew.CANDIDATES_SCHEMA.names} for c in cands]
    candidates = tmp_path / "window_candidates.parquet"
    pq.write_table(pa.Table.from_pylist(full, schema=ew.CANDIDATES_SCHEMA), candidates)
    return windows, candidates


def test_load_orders_sends_only_unsettled_orders_in_rank_order(tmp_path):
    windows, candidates = _write_inputs(tmp_path)
    _, orders = wm.load_orders(windows, candidates)
    assert [o.text_stem for o in orders] == ["2", "3"]
    assert orders[0].sentences == [BANK, UPSI]


def test_scratch_run_keeps_the_model_cache_in_scratch(tmp_path, fake_llm):
    windows, candidates = _write_inputs(tmp_path)
    fake_llm([answer("none", 0), answer("none", 0)])
    out = tmp_path / "out"
    wm.main(["--windows", str(windows), "--candidates", str(candidates), "--scratch", str(out),
             "--workers", "1"])
    assert len(list((out / "llm").glob("*/*.json"))) == 2
    assert not llm.LLM_CACHE.exists()


def test_main_end_to_end_then_offline(tmp_path, fake_llm):
    windows, candidates = _write_inputs(tmp_path)
    fake_llm([answer("violation", 2, "May 01, 2013", "June 02, 2013"),
              answer("investigation", 1, "March 2009", "March 2009")])
    out = tmp_path / "out"
    args = ["--windows", str(windows), "--candidates", str(candidates), "--scratch", str(out),
            "--workers", "1"]
    assert wm.main(args) == 0

    model = {r["text_stem"]: r for r in pq.read_table(out / "window_model.parquet").to_pylist()}
    assert model["2"]["model_status"] == wm.MODEL_WINDOW and model["2"]["sentence_no"] == 2
    assert (model["3"]["violation_start"], model["3"]["violation_end"]) == (D(2009, 3, 1), D(2009, 3, 31))

    combined = {r["text_stem"]: r for r in pq.read_table(out / "violation_windows.parquet").to_pylist()}
    assert len(combined) == 4
    assert combined["1"]["violation_method"] == "regex" and combined["1"]["window_kind"] == wm.STATED_PERIOD
    assert combined["1"]["violation_start"] == D(2014, 4, 1)  # regex windows are never overridden
    assert combined["2"]["violation_method"] == "model" and combined["2"]["window_kind"] == wm.CONDUCT_DATES
    assert combined["2"]["violation_raw"] == UPSI and combined["2"]["violation_confidence"] == "medium"
    assert combined["4"]["violation_start"] is None and combined["4"]["model_status"] is None

    # Second run: every answer from raw/llm, no server needed.
    fake_llm([])
    llm.MODEL_CALLS = 0
    assert wm.main(args + ["--offline"]) == 0
    assert llm.MODEL_CALLS == 0
    again = pq.read_table(out / "window_model.parquet").to_pylist()
    assert all(r["from_cache"] for r in again)


def test_subset_run_does_not_write_the_combined_table(tmp_path, fake_llm):
    windows, candidates = _write_inputs(tmp_path)
    fake_llm([answer("none", 0)])
    out = tmp_path / "out"
    wm.main(["--windows", str(windows), "--candidates", str(candidates), "--scratch", str(out),
             "--stem", "3", "--workers", "1"])
    assert (out / "window_model.parquet").exists()
    assert not (out / "violation_windows.parquet").exists()


def test_main_refuses_to_write_under_raw(tmp_path, fake_llm):
    windows, candidates = _write_inputs(tmp_path)
    fake_llm([])
    with pytest.raises(ValueError, match="refusing to write"):
        wm.main(["--windows", str(windows), "--candidates", str(candidates),
                 "--scratch", str(wm.REPO_ROOT / "raw" / "cache" / "x")])
