"""
Shared Veriphone client with automatic API-key failover.

How it works
------------
* Keys are loaded from .env: VERIPHONE_API_KEY_1 ... VERIPHONE_API_KEY_20
  (and optionally a comma-separated VERIPHONE_API_KEYS).
* Keys are used round-robin. If a key is capped / rate-limited / invalid /
  failing, it is put on a cooldown and the SAME number is immediately retried
  with the next key, so no number is skipped.
* If every key is on cooldown, the lookup waits (up to `max_wait` seconds) for
  the soonest key to come back. If it still can't get an answer it returns
  ok=False. Callers must NOT write a fake result in that case; leave the cell
  empty so the number is retried later.
* Successful results are cached in memory so repeated numbers cost no credits.
"""
import os
import re
import time
import threading
from dataclasses import dataclass, field

import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://api.veriphone.io/v2/verify"

# Cooldowns (seconds) applied to a key after a given kind of failure
RATE_LIMIT_COOLDOWN = 60        # HTTP 429 / "rate limit"
QUOTA_COOLDOWN = 3600           # monthly/daily quota used up
AUTH_COOLDOWN = 86400           # invalid / disabled key
ERROR_COOLDOWN = 10             # network error / 5xx / unknown error


def load_keys():
    keys = []
    for i in range(1, 21):
        k = (os.getenv(f"VERIPHONE_API_KEY_{i}") or "").strip()
        if k and k not in keys:
            keys.append(k)
    for k in (os.getenv("VERIPHONE_API_KEYS") or "").split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def normalize_phone(raw):
    """Turn whatever is in the sheet into +E.164-ish format."""
    s = str(raw or "").strip()
    if not s:
        return ""
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if has_plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits


def normalize_line_type(raw):
    t = (raw or "").lower()
    if "voip" in t:
        return "VOIP"
    if "landline" in t or "fixed" in t:
        return "Landline"
    if "mobile" in t or "cell" in t:
        return "Mobile"
    return "Other"


@dataclass
class LookupResult:
    ok: bool
    phone: str = ""
    line_type: str = "Other"
    carrier: str = "Unknown"
    fake: str = "Yes"            # "Yes" / "No"
    valid: bool = False
    error: str = ""
    raw: dict = field(default_factory=dict)
    key_used: str = ""           # masked
    cached: bool = False

    def as_tuple(self):
        return self.line_type, self.carrier, self.fake


def _mask(key):
    return key[:4] + "..." + key[-4:] if len(key) > 10 else "****"


def _classify(status_code, data):
    """Return one of: ok, rate, quota, auth, bad_number, retry."""
    data = data if isinstance(data, dict) else {}
    msg = str(data.get("message", "")).lower()

    if status_code == 200 and data.get("status") == "success":
        return "ok"
    if status_code == 429 or "rate limit" in msg or "too many" in msg:
        return "rate"
    if status_code == 402 or any(w in msg for w in ("quota", "limit", "exceed", "credit", "usage")):
        return "quota"
    if status_code in (401, 403) or ("key" in msg and any(w in msg for w in ("invalid", "missing", "disabled", "inactive", "expired"))):
        return "auth"
    if status_code in (200, 400, 422) and ("phone" in msg or "number" in msg):
        return "bad_number"   # the number itself is bad, not the key
    return "retry"


class KeyPool:
    def __init__(self, keys):
        self.keys = list(keys)
        self._cooldown = {k: 0.0 for k in self.keys}
        self._reason = {k: "" for k in self.keys}
        self._index = 0
        self._lock = threading.Lock()

    def __len__(self):
        return len(self.keys)

    def next_key(self):
        """Next available (not cooling down) key, round-robin. None if all are cooling down."""
        with self._lock:
            now = time.time()
            n = len(self.keys)
            for i in range(n):
                k = self.keys[(self._index + i) % n]
                if self._cooldown[k] <= now:
                    self._index = (self._index + i + 1) % n
                    return k
            return None

    def soonest_available(self):
        """Seconds until the first key comes off cooldown (0 if one is available now)."""
        with self._lock:
            if not self.keys:
                return float("inf")
            return max(0.0, min(self._cooldown.values()) - time.time())

    def mark(self, key, seconds, reason):
        with self._lock:
            self._cooldown[key] = time.time() + seconds
            self._reason[key] = reason

    def status(self):
        now = time.time()
        out = []
        with self._lock:
            for i, k in enumerate(self.keys, 1):
                left = self._cooldown[k] - now
                if left > 0:
                    out.append({"Key": f"#{i} {_mask(k)}", "State": f"Paused ({self._reason[k]})",
                                "Back in": f"{int(left)}s"})
                else:
                    out.append({"Key": f"#{i} {_mask(k)}", "State": "Active", "Back in": "-"})
        return out


class VeriphoneClient:
    def __init__(self, keys=None, max_wait=120, timeout=10):
        self.pool = KeyPool(keys if keys is not None else load_keys())
        self.max_wait = max_wait
        self.timeout = timeout
        self.cache = {}
        self._cache_lock = threading.Lock()

    @property
    def key_count(self):
        return len(self.pool)

    def lookup(self, phone):
        phone = normalize_phone(phone)
        if not phone:
            return LookupResult(ok=False, error="Empty phone number")

        with self._cache_lock:
            hit = self.cache.get(phone)
        if hit is not None:
            hit.cached = True
            return hit

        if self.key_count == 0:
            return LookupResult(ok=False, phone=phone, error="No Veriphone API keys found in .env")

        deadline = time.time() + self.max_wait
        last_error = "Unknown error"

        while True:
            key = self.pool.next_key()

            if key is None:
                # Every key is cooling down: wait for the soonest one, if it is within our budget
                wait = self.pool.soonest_available()
                if time.time() + wait > deadline:
                    return LookupResult(ok=False, phone=phone,
                                        error=f"All API keys are capped/unavailable. Last error: {last_error}")
                time.sleep(min(wait + 0.2, 30))
                continue

            try:
                resp = requests.get(API_URL, params={"phone": phone, "key": key}, timeout=self.timeout)
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                kind = _classify(resp.status_code, data)
                msg = data.get("message", "") if isinstance(data, dict) else ""
            except requests.RequestException as e:
                self.pool.mark(key, ERROR_COOLDOWN, "network error")
                last_error = f"Network error: {e}"
                continue

            if kind == "ok":
                valid = bool(data.get("phone_valid", False))
                result = LookupResult(
                    ok=True,
                    phone=data.get("phone", phone),
                    line_type=normalize_line_type(data.get("phone_type")),
                    carrier=data.get("carrier") or "Unknown",
                    fake="No" if valid else "Yes",
                    valid=valid,
                    raw=data,
                    key_used=_mask(key),
                )
                with self._cache_lock:
                    self.cache[phone] = result
                return result

            if kind == "bad_number":
                # The number itself was rejected by the API: that is a real answer, not a key problem.
                result = LookupResult(ok=True, phone=phone, line_type="Other", carrier="Unknown",
                                      fake="Yes", valid=False, error=msg or "Invalid number",
                                      raw=data if isinstance(data, dict) else {}, key_used=_mask(key))
                with self._cache_lock:
                    self.cache[phone] = result
                return result

            # Key problem -> pause this key and retry the same number with the next one
            if kind == "rate":
                self.pool.mark(key, RATE_LIMIT_COOLDOWN, "rate limited")
            elif kind == "quota":
                self.pool.mark(key, QUOTA_COOLDOWN, "quota reached")
            elif kind == "auth":
                self.pool.mark(key, AUTH_COOLDOWN, "invalid key")
            else:
                self.pool.mark(key, ERROR_COOLDOWN, f"HTTP {resp.status_code}")
            last_error = f"{kind} (HTTP {resp.status_code}) {msg}".strip()


# Shared singleton used by app.py and cli_checker.py
client = VeriphoneClient()
