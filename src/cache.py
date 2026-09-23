"""The single network boundary for this project.

Every outbound HTTP request in the pipeline goes through :func:`fetch` (or its
:func:`get` / :func:`post` wrappers). Nothing else may call httpx/requests
directly. Responses are stored on disk keyed by a hash of the *whole request
identity* — method, URL, query params and form body — so that:

* re-runs read from disk and never touch the network (runbook rule 1);
* two POSTs to the same URL with different bodies (the SEBI listing pages,
  which differ only in a form field) get distinct cache entries.

Deliberately **not** part of the cache key: request headers and cookies. A
JSESSIONID or a tweaked User-Agent must not invalidate an otherwise identical
request, or cache reuse across program runs would be impossible.

Paths are resolved against the repository root, so the module behaves the same
whichever directory it is invoked from. Everything stays on the WSL filesystem.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import httpx

log = logging.getLogger(__name__)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = REPO_ROOT / "raw" / "cache"

#: Seconds to wait before every *network* call (runbook rule 4). Cache hits
#: never sleep.
REQUEST_DELAY = 1.5
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 3
#: Base for exponential backoff between retries: 3s, 6s, 12s ...
RETRY_BACKOFF = 3.0
#: Statuses worth retrying. 530 is what SEBI's WAF returns when it throttles.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 524, 530})

#: A browser-shaped User-Agent is required: SEBI's WAF rejects anything that
#: does not look like one (verified — a bare "Academic research" UA gets HTTP
#: 530 "Unauthorized Request Blocked"). Set SEBI_CONTACT to append a contact
#: address so SEBI can reach the operator, as politeness requires.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


def user_agent() -> str:
    contact = os.environ.get("SEBI_CONTACT", "").strip()
    if contact:
        return f"{_BROWSER_UA} (academic research, contact: {contact})"
    return _BROWSER_UA


#: When True, a cache miss raises CacheMiss instead of hitting the network.
#: Used by --offline to *prove* a run is served entirely from disk.
OFFLINE = False

#: Count of actual network requests issued. Tests and the CLI read this to
#: assert that a re-run performed no I/O.
NETWORK_CALLS = 0


class FetchError(RuntimeError):
    """A request failed after exhausting its retries."""


class CacheMiss(FetchError):
    """Offline mode was requested but the response is not cached."""


class BlockedResponse(FetchError):
    """The response body is a WAF interstitial rather than real content."""


def set_offline(value: bool) -> None:
    global OFFLINE
    OFFLINE = bool(value)


def reset_stats() -> None:
    global NETWORK_CALLS
    NETWORK_CALLS = 0


def _canonical_payload(payload: Mapping[str, Any] | Iterable[tuple[str, Any]] | None) -> str:
    """Canonicalise params/form data to a stable, unambiguous string.

    Sorted and percent-encoded, so that neither key order nor a value
    containing ``&`` or ``=`` can make two different requests collide.
    """
    if not payload:
        return ""
    items = payload.items() if isinstance(payload, Mapping) else payload
    pairs = sorted((str(k), str(v)) for k, v in items)
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)


def cache_key(
    url: str,
    *,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    extra: str | None = None,
) -> str:
    """Hex digest identifying a request. See module docstring for what is in it."""
    parts = [
        method.upper(),
        url.strip(),
        _canonical_payload(params),
        _canonical_payload(data),
        extra or "",
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def cache_path(url: str, **kwargs: Any) -> pathlib.Path:
    return CACHE / cache_key(url, **kwargs)


def is_cached(url: str, **kwargs: Any) -> bool:
    return cache_path(url, **kwargs).exists()


def _looks_blocked(content: bytes) -> bool:
    head = content[:4096].lower()
    return (
        b"unauthorized request blocked" in head
        or b"unauthorized activity has been detected" in head
    )


def default_validator(content: bytes) -> bool:
    """Reject WAF interstitials so they never poison the cache.

    A blocked page returns a perfectly well-formed 200-or-530 body; caching it
    would silently corrupt the corpus with pages that contain no orders.
    """
    return not _looks_blocked(content)


def _write_atomic(path: pathlib.Path, content: bytes) -> None:
    """Write via a temp file so an interrupted run cannot leave a truncated entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp-{os.urandom(6).hex()}"
    try:
        tmp.write_bytes(content)
        tmp.replace(path)
    finally:
        if tmp.exists():  # pragma: no cover - only on a failed write
            tmp.unlink(missing_ok=True)


def fetch(
    url: str,
    *,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    force: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    validate: Callable[[bytes], bool] | None = None,
    cache_key_extra: str | None = None,
    offline: bool | None = None,
) -> bytes:
    """Return the response body for a request, from disk when possible.

    Args:
        method: ``GET`` or ``POST`` (anything httpx accepts).
        params: query string parameters; part of the cache key.
        data: form body; part of the cache key.
        headers: sent, but deliberately *not* part of the cache key.
        force: re-fetch and overwrite the cached entry.
        retries: attempts beyond the first for transient failures.
        validate: ``bytes -> bool``; a False result is treated as a transient
            failure and the body is not cached. Defaults to rejecting WAF pages.
        offline: raise :class:`CacheMiss` rather than hit the network.
    """
    global NETWORK_CALLS

    key = cache_path(url, method=method, params=params, data=data, extra=cache_key_extra)
    if key.exists() and not force:
        return key.read_bytes()

    if OFFLINE if offline is None else offline:
        raise CacheMiss(f"not cached and offline: {method.upper()} {url} {_canonical_payload(data)}")

    check = default_validator if validate is None else validate
    request_headers = {"User-Agent": user_agent()}
    if headers:
        request_headers.update(headers)

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            delay = RETRY_BACKOFF * (2 ** (attempt - 1))
            log.warning("retry %d/%d in %.0fs after %s", attempt, retries, delay, last_error)
            time.sleep(delay)
        time.sleep(REQUEST_DELAY)  # runbook rule 4: never hammer SEBI
        NETWORK_CALLS += 1
        try:
            response = httpx.request(
                method.upper(), url, params=params, data=data,
                headers=request_headers, timeout=timeout,
            )
        except httpx.HTTPError as exc:
            last_error = exc
            continue

        if response.status_code in RETRY_STATUSES:
            last_error = FetchError(f"HTTP {response.status_code}")
            continue
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 4xx other than the retryable ones is a permanent answer; retrying
            # it is just rudeness.
            raise FetchError(f"HTTP {response.status_code} for {url}") from exc

        content = response.content
        if not check(content):
            last_error = BlockedResponse(f"rejected response body for {url}")
            continue

        _write_atomic(key, content)
        return content

    raise FetchError(f"giving up on {method.upper()} {url} after {retries + 1} attempts: {last_error}")


def get(
    url: str,
    params: Mapping[str, Any] | None = None,
    force: bool = False,
    **kwargs: Any,
) -> bytes:
    """Cached GET. Also the unified entry point: pass ``method="POST", data=...``.

    ``cache.get(url)`` keeps working exactly as before.
    """
    kwargs.setdefault("method", "GET")
    return fetch(url, params=params, force=force, **kwargs)


def post(url: str, data: Mapping[str, Any], force: bool = False, **kwargs: Any) -> bytes:
    """Cached form-encoded POST — e.g. one SEBI listing page per call."""
    kwargs.pop("method", None)
    return fetch(url, method="POST", data=data, force=force, **kwargs)
