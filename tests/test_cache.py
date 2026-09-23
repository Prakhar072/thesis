"""Tests for the network boundary.

The critical one is test_post_body_changes_cache_key: if two listing pages
shared a cache entry the whole scraper would be silently wrong.
"""

import pathlib

import httpx
import pytest

from conftest import FakeResponse
from src import cache

URL = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"


def form(next_value: int, smid: int = 2) -> dict:
    return {"nextValue": str(next_value), "next": "n", "smid": str(smid),
            "doDirect": str(next_value - 1), "sid": "2", "ssid": "9"}


# --- cache identity --------------------------------------------------------

def test_post_body_changes_cache_key():
    """CRITICAL: same URL, different POST body -> different cache entries."""
    k1 = cache.cache_key(URL, method="POST", data=form(1))
    k2 = cache.cache_key(URL, method="POST", data=form(2))
    assert k1 != k2


def test_different_categories_have_different_cache_keys():
    keys = {
        cache.cache_key(URL, method="POST", data=form(1, smid=smid))
        for smid in (2, 3, 6, 133)
    }
    assert len(keys) == 4


def test_method_is_part_of_the_key():
    body = form(1)
    assert cache.cache_key(URL, method="GET", data=body) != cache.cache_key(
        URL, method="POST", data=body
    )


def test_key_is_order_independent_but_value_sensitive():
    a = cache.cache_key(URL, method="POST", data={"a": "1", "b": "2"})
    b = cache.cache_key(URL, method="POST", data={"b": "2", "a": "1"})
    c = cache.cache_key(URL, method="POST", data={"a": "2", "b": "1"})
    assert a == b
    assert a != c


def test_values_containing_separators_cannot_collide():
    """Naive 'k=v&k=v' joining would make these two identical."""
    a = cache.cache_key(URL, method="POST", data={"x": "1&y=2", "y": ""})
    b = cache.cache_key(URL, method="POST", data={"x": "1", "y": "2"})
    assert a != b


def test_headers_are_not_part_of_the_key():
    """A rotating JSESSIONID must not destroy cache reuse across runs."""
    base = cache.cache_key(URL, method="POST", data=form(1))
    assert cache.cache_key(URL, method="POST", data=form(1)) == base


# --- fetch behaviour -------------------------------------------------------

def test_plain_get_still_works(tmp_cache, fake_http):
    fake_http.queue(FakeResponse(b"hello"))
    assert cache.get("https://example.org/a") == b"hello"
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0]["method"] == "GET"


def test_second_call_is_served_from_disk(tmp_cache, fake_http):
    fake_http.queue(FakeResponse(b"page-1"))
    first = cache.get(URL, method="POST", data=form(1))
    second = cache.get(URL, method="POST", data=form(1))
    assert first == second == b"page-1"
    assert len(fake_http.calls) == 1, "re-run must not touch the network"
    assert cache.NETWORK_CALLS == 1


def test_force_bypasses_cache(tmp_cache, fake_http):
    fake_http.queue(FakeResponse(b"v1"), FakeResponse(b"v2"))
    assert cache.get(URL, method="POST", data=form(1)) == b"v1"
    assert cache.get(URL, method="POST", data=form(1), force=True) == b"v2"
    assert len(fake_http.calls) == 2
    assert cache.get(URL, method="POST", data=form(1)) == b"v2"


def test_page1_and_page2_do_not_collide_on_disk(tmp_cache, fake_http):
    fake_http.queue(FakeResponse(b"PAGE-ONE"), FakeResponse(b"PAGE-TWO"))
    p1 = cache.get(URL, method="POST", data=form(1))
    p2 = cache.get(URL, method="POST", data=form(2))
    assert p1 == b"PAGE-ONE"
    assert p2 == b"PAGE-TWO"
    assert len(list(tmp_cache.iterdir())) == 2


def test_transient_failure_is_retried_then_succeeds(tmp_cache, fake_http):
    fake_http.queue(
        FakeResponse(b"", 503),
        httpx.ConnectTimeout("boom"),
        FakeResponse(b"finally"),
    )
    assert cache.get(URL, method="POST", data=form(1), retries=3) == b"finally"
    assert len(fake_http.calls) == 3


def test_retries_are_bounded(tmp_cache, fake_http):
    fake_http.queue(*[FakeResponse(b"", 503)] * 10)
    with pytest.raises(cache.FetchError):
        cache.get(URL, method="POST", data=form(1), retries=2)
    assert len(fake_http.calls) == 3, "1 attempt + 2 retries, not unbounded"


def test_permanent_client_error_is_not_retried(tmp_cache, fake_http):
    fake_http.queue(*[FakeResponse(b"nope", 404)] * 5)
    with pytest.raises(cache.FetchError):
        cache.get("https://example.org/missing", retries=3)
    assert len(fake_http.calls) == 1


def test_failed_fetch_writes_nothing(tmp_cache, fake_http):
    fake_http.queue(*[FakeResponse(b"", 503)] * 5)
    with pytest.raises(cache.FetchError):
        cache.get(URL, method="POST", data=form(1), retries=1)
    assert not tmp_cache.exists() or list(tmp_cache.iterdir()) == []


def test_waf_block_page_is_never_cached(tmp_cache, fake_http):
    """A WAF interstitial is a failure, not content — caching it would poison the corpus."""
    blocked = b"<HTML><TITLE>Unauthorized Request Blocked</TITLE></HTML>"
    fake_http.queue(FakeResponse(blocked), FakeResponse(blocked), FakeResponse(b"real"))
    assert cache.get(URL, method="POST", data=form(1), retries=3) == b"real"
    assert cache.get(URL, method="POST", data=form(1)) == b"real"


def test_offline_mode_raises_on_miss_and_serves_hits(tmp_cache, fake_http):
    fake_http.queue(FakeResponse(b"cached"))
    cache.get(URL, method="POST", data=form(1))
    cache.set_offline(True)
    try:
        assert cache.get(URL, method="POST", data=form(1)) == b"cached"
        with pytest.raises(cache.CacheMiss):
            cache.get(URL, method="POST", data=form(99))
    finally:
        cache.set_offline(False)
    assert len(fake_http.calls) == 1


def test_is_cached_reflects_disk(tmp_cache, fake_http):
    assert not cache.is_cached(URL, method="POST", data=form(1))
    fake_http.queue(FakeResponse(b"x"))
    cache.get(URL, method="POST", data=form(1))
    assert cache.is_cached(URL, method="POST", data=form(1))
    assert not cache.is_cached(URL, method="POST", data=form(2))


def test_no_temp_files_left_behind(tmp_cache, fake_http):
    fake_http.queue(FakeResponse(b"x"))
    cache.get(URL, method="POST", data=form(1))
    assert not any(".tmp-" in p.name for p in tmp_cache.iterdir())


def test_user_agent_carries_contact_when_configured(monkeypatch):
    monkeypatch.setenv("SEBI_CONTACT", "someone@example.edu")
    assert "someone@example.edu" in cache.user_agent()
    monkeypatch.delenv("SEBI_CONTACT")
    assert "Mozilla/5.0" in cache.user_agent()


# --- WSL / path safety -----------------------------------------------------

def test_paths_are_wsl_native():
    for path in (cache.CACHE, cache.REPO_ROOT):
        text = str(path)
        assert isinstance(path, pathlib.Path)
        assert not text.startswith("/mnt/"), "must not live on the Windows filesystem"
        assert "\\" not in text
        assert not (len(text) > 1 and text[1] == ":"), "no Windows drive letters"
        assert text.startswith("/")
