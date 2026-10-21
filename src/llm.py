"""Local language-model calls, each cached permanently on disk.

Runbook rule 1 applies to model calls too: a response is stored under
``raw/llm/`` keyed by a hash of everything that could change it — model name
*and* weights digest, generation options, thinking on/off, both prompts and the
JSON schema — so re-runs read from disk, a re-pulled model or an edited prompt
gets fresh calls, and nothing stale is ever reused silently. Like ``raw/ocr/``,
the cache is append-only: entries are written once and never modified.

The model is served by Ollama on this machine (``OLLAMA_HOST``, default
``http://localhost:11434``). This is the one exception to "all HTTP goes
through ``src/cache.py``": that module is built for SEBI (a 1.5 s politeness
delay, a browser User-Agent, WAF checks), none of which applies to a local
model server, and nothing here leaves the machine.

``OFFLINE = True`` (``--offline`` on the CLI) turns a cache miss into
:class:`CacheMiss`, proving a run was served entirely from disk.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import threading
import time
from dataclasses import dataclass, field

from src import cache
from src.extract_text import write_atomic

REPO_ROOT = cache.REPO_ROOT
LLM_CACHE = REPO_ROOT / "raw" / "llm"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

OFFLINE = False
#: Count of real model calls (cache misses) in this process.
MODEL_CALLS = 0
_calls_lock = threading.Lock()


class LlmError(RuntimeError):
    """The model server failed or returned something unusable."""


class CacheMiss(LlmError):
    """Offline mode was requested but the response is not cached."""


@dataclass(frozen=True)
class ModelSpec:
    """Which model answers, and how: every field is part of the cache key.

    Attributes:
        name (str): the Ollama model name, e.g. "qwen3.5:4b".
        digest (str): the weights digest the server reports (a re-pulled model gets new cache keys).
        think (bool): thinking mode on/off (off in this project: no measured gain, ~70x slower).
        options (dict): generation options (temperature, seed, num_ctx …).
        keep_alive (str): how long the server keeps the model loaded between calls.
    """
    name: str
    digest: str  # weights digest from the server: part of the cache key
    think: bool = False
    options: dict = field(default_factory=dict)
    keep_alive: str = "30m"

    def identity(self) -> dict:
        """The fields that identify the model's behaviour, for the cache key and the stored record.

        Returns:
            dict: {"model", "digest", "think", "options" (sorted)}.
        """
        return {"model": self.name, "digest": self.digest, "think": self.think,
                "options": dict(sorted(self.options.items()))}


def _client():
    """An Ollama client for ``OLLAMA_HOST`` (imported lazily, so tests never need the package).

    Returns:
        ollama.Client: the client.
    """
    import ollama  # imported lazily: tests and offline runs never need it
    return ollama.Client(host=OLLAMA_HOST)


def model_digest(name: str) -> str:
    """The server's digest for ``name``; raises LlmError if it is not served.

    Args:
        name (str): the model name.
    Returns:
        str: the weights digest the server reports for it.
    Raises:
        LlmError: the server cannot be reached or does not serve the model.
    """
    try:
        models = _client().list().models
    except Exception as exc:  # connection refused, server down
        raise LlmError(f"cannot reach Ollama at {OLLAMA_HOST}: {exc}") from exc
    for m in models:
        if m.model == name:
            return m.digest
    raise LlmError(f"model {name!r} is not served by Ollama at {OLLAMA_HOST} "
                   f"(have: {', '.join(m.model for m in models) or 'none'})")


def cache_key(spec: ModelSpec, system: str, user: str, schema: dict | None) -> str:
    """The cache key of one chat turn: a hash of the model identity, both prompts and the schema.

    Args:
        spec (ModelSpec): the model.
        system (str): the system prompt.
        user (str): the user prompt.
        schema (dict | None): the JSON schema the answer must follow.
    Returns:
        str: a SHA-256 hex digest.
    """
    payload = {**spec.identity(), "system": system, "user": user, "schema": schema}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def cache_path(key: str, root: pathlib.Path | None = None) -> pathlib.Path:
    """Where a model answer is stored: ``raw/llm/<key[:2]>/<key>.json``.

    Args:
        key (str): the cache key.
        root (pathlib.Path | None): another cache root (tests); default ``raw/llm``.
    Returns:
        pathlib.Path: the JSON file's path.
    """
    return (root or LLM_CACHE) / key[:2] / f"{key}.json"


def chat_json(spec: ModelSpec, system: str, user: str, schema: dict | None = None, *,
              root: pathlib.Path | None = None, timeout_retries: int = 2) -> dict:
    """One chat turn, from the cache when possible.

    Returns the stored record: ``content`` (the model's text, JSON when a schema
    is given), ``thinking``, token counts, ``seconds`` and ``from_cache``.

    Args:
        spec (ModelSpec): the model.
        system (str): the system prompt.
        user (str): the user prompt.
        schema (dict | None): JSON schema for structured output.
        root (pathlib.Path | None): another cache root (tests).
        timeout_retries (int): retries after a failed call (2 s, 4 s … apart).
    Returns:
        dict: key, request, content, thinking, prompt_tokens, output_tokens, seconds, created_at,
        from_cache (bool).
    Raises:
        CacheMiss: ``OFFLINE`` and not cached.
        LlmError: every attempt failed.
    """
    global MODEL_CALLS
    key = cache_key(spec, system, user, schema)
    path = cache_path(key, root)
    if path.exists():
        return {**json.loads(path.read_text(encoding="utf-8")), "from_cache": True}
    if OFFLINE:
        raise CacheMiss(f"model response not cached (key {key[:12]})")

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last: Exception | None = None
    for attempt in range(timeout_retries + 1):
        started = time.monotonic()
        try:
            response = _client().chat(model=spec.name, messages=messages, format=schema,
                                      think=spec.think, options=spec.options,
                                      keep_alive=spec.keep_alive)
            break
        except Exception as exc:  # server restarts, timeouts: retry, then give up
            last = exc
            time.sleep(2 * (attempt + 1))
    else:
        raise LlmError(f"model call failed: {last}") from last

    with _calls_lock:
        MODEL_CALLS += 1
    record = {
        "key": key,
        "request": {**spec.identity(), "system": system, "user": user, "schema": schema},
        "content": response.message.content,
        "thinking": getattr(response.message, "thinking", None),
        "prompt_tokens": response.prompt_eval_count,
        "output_tokens": response.eval_count,
        "seconds": round(time.monotonic() - started, 3),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    write_atomic(path, json.dumps(record, ensure_ascii=False).encode("utf-8"))
    return {**record, "from_cache": False}
