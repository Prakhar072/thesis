import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES = REPO_ROOT / "tests" / "fixtures"

from src import cache  # noqa: E402


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Point the cache at a throwaway directory and neutralise the polite delay."""
    monkeypatch.setattr(cache, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(cache.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cache, "OFFLINE", False)
    cache.reset_stats()
    yield tmp_path / "cache"
    cache.reset_stats()


class FakeResponse:
    def __init__(self, content: bytes = b"ok", status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )


@pytest.fixture
def fake_http(monkeypatch):
    """Record every httpx call the cache makes and serve scripted responses."""

    calls: list[dict] = []
    script: list = []

    def _request(method, url, params=None, data=None, headers=None, timeout=None):
        calls.append({"method": method, "url": url, "params": params,
                      "data": dict(data) if data else None, "headers": headers})
        item = script.pop(0) if script else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(cache.httpx, "request", _request)

    class Handle:
        def __init__(self):
            self.calls = calls

        def queue(self, *items):
            script.extend(items)

    return Handle()


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
