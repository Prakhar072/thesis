import hashlib, pathlib, httpx, time
CACHE = pathlib.Path("raw/cache")

def get(url: str, force: bool = False) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = CACHE / hashlib.sha256(url.encode()).hexdigest()
    if key.exists() and not force:
        return key.read_bytes()
    time.sleep(1.5)                       # rule 4
    r = httpx.get(url, timeout=60, headers={
        "User-Agent": "Academic research (BITS Pilani) — <your email>"
    })
    r.raise_for_status()
    key.write_bytes(r.content)
    return r.content