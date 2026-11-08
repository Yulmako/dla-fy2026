"""Minimal, polite client for the public USAspending.gov API.

* No credentials, no API keys, no SAM.gov.
* Throttling between calls, retries with exponential backoff on 429/5xx and
  network errors (honours Retry-After), a hard call budget.
* Every request/response pair is written to disk as raw JSON for traceability,
  and every call is logged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://api.usaspending.gov"
USER_AGENT = "federal-competitor-intelligence/0.1 (public-data research; python-requests)"
# Transient statuses retried with bounded exponential backoff. HTTP 500 is
# retried at most `max_retries_on_500` times because USAspending also returns
# 500 for some deterministic request errors (e.g. an unsupported field name).
# All other 4xx (400/404/422 …) are never retried.
RETRY_STATUSES = {429, 502, 503, 504}
MAX_SAVED_TEXT = 200_000  # characters of non-JSON body kept in raw files

log = logging.getLogger("fci.api")


class CallBudgetExceeded(RuntimeError):
    """Raised when the configured maximum number of API calls is reached."""


@dataclass
class ApiResponse:
    method: str
    path: str
    request_body: Any
    status_code: int | None
    data: Any = None
    error: str | None = None
    elapsed_ms: int = 0
    attempts: int = 0
    raw_file: str | None = None
    fetched_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and self.error is None

    def error_detail(self) -> str:
        """Best-effort human-readable error message from a 4xx/5xx body."""
        if isinstance(self.data, dict):
            for key in ("detail", "message", "messages", "error"):
                if key in self.data:
                    return str(self.data[key])[:500]
        return (self.error or "")[:500]


@dataclass
class CallStats:
    calls: int = 0
    attempts: int = 0
    retries: int = 0
    failures: int = 0
    non_retryable_errors: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


class UsaSpendingClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        session: requests.Session | None = None,
        delay_seconds: float = 0.5,
        timeout_seconds: float = 90.0,
        max_retries: int = 4,
        max_retries_on_500: int = 1,
        backoff_base: float = 2.0,
        max_calls: int = 300,
        raw_dir: Path | None = None,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.delay = delay_seconds
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.max_retries_on_500 = max_retries_on_500
        self.backoff_base = backoff_base
        self.max_calls = max_calls
        self.raw_dir = Path(raw_dir) if raw_dir else None
        if self.raw_dir:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._sleep = sleep
        self._last_call = 0.0
        self.stats = CallStats()

    # ------------------------------------------------------------------ public
    def get(self, path: str, params: dict | None = None, label: str = "",
            retry: bool = True) -> ApiResponse:
        return self._request("GET", path, params=params, label=label, retry=retry)

    def post(self, path: str, body: dict, label: str = "", retry: bool = True) -> ApiResponse:
        """`retry=False` for exploratory calls (e.g. field probing) whose failure is
        expected to be deterministic: such calls are made exactly once."""
        return self._request("POST", path, body=body, label=label, retry=retry)

    def _should_retry(self, status: int, attempt: int, retry: bool) -> bool:
        if not retry:
            return False
        if status in RETRY_STATUSES:
            return attempt < self.max_retries
        if status == 500:
            return attempt < min(self.max_retries, self.max_retries_on_500)
        return False

    # ---------------------------------------------------------------- internal
    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.strip().isdigit():
            return min(float(retry_after), 120.0)
        return min(self.backoff_base ** attempt + random.uniform(0, 1), 60.0)

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 params: dict | None = None, label: str = "",
                 retry: bool = True) -> ApiResponse:
        if self.stats.calls >= self.max_calls:
            raise CallBudgetExceeded(f"API call budget of {self.max_calls} reached")
        self.stats.calls += 1
        url = f"{self.base_url}{path}"
        started = time.monotonic()
        resp: requests.Response | None = None
        error: str | None = None
        attempts = 0

        for attempt in range(self.max_retries + 1):
            attempts = attempt + 1
            self.stats.attempts += 1
            self._throttle()
            try:
                resp = self.session.request(method, url, json=body, params=params,
                                            timeout=self.timeout)
                error = None
                if self._should_retry(resp.status_code, attempt, retry):
                    self.stats.retries += 1
                    pause = self._backoff(attempt, resp.headers.get("Retry-After"))
                    log.warning("%s %s -> %s, retry %d in %.1fs", method, path,
                                resp.status_code, attempt + 1, pause)
                    self._sleep(pause)
                    continue
                break
            except (requests.ConnectionError, requests.Timeout) as exc:
                resp = None
                error = f"{type(exc).__name__}: {exc}"
                if retry and attempt < self.max_retries:
                    self.stats.retries += 1
                    pause = self._backoff(attempt, None)
                    log.warning("%s %s -> %s, retry %d in %.1fs", method, path, error,
                                attempt + 1, pause)
                    self._sleep(pause)
                    continue
                break

        elapsed_ms = int((time.monotonic() - started) * 1000)
        status = resp.status_code if resp is not None else None
        data: Any = None
        text = ""
        if resp is not None:
            text = resp.text or ""
            try:
                data = resp.json()
            except ValueError:
                data = None
            if status != 200 and error is None:
                error = f"HTTP {status}"
        key = str(status) if status is not None else "network_error"
        self.stats.by_status[key] = self.stats.by_status.get(key, 0) + 1
        if status != 200:
            self.stats.failures += 1
            if status is not None and 400 <= status < 500 and status != 429:
                self.stats.non_retryable_errors += 1

        result = ApiResponse(
            method=method, path=path, request_body=body if body is not None else params,
            status_code=status, data=data, error=error, elapsed_ms=elapsed_ms,
            attempts=attempts, fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        result.raw_file = self._save_raw(result, label, text)
        log.info("%s %s [%s] -> %s in %d ms (attempts=%d)", method, path, label or "-",
                 status if status is not None else error, elapsed_ms, attempts)
        return result

    def _save_raw(self, r: ApiResponse, label: str, text: str) -> str | None:
        if not self.raw_dir:
            return None
        slug = re.sub(r"[^a-z0-9]+", "_", (label or r.path).lower()).strip("_")[:60]
        name = f"{self.stats.calls:04d}_{slug}.json"
        record = {
            "fetched_at": r.fetched_at,
            "method": r.method,
            "url": f"{self.base_url}{r.path}",
            "request": r.request_body,
            "status_code": r.status_code,
            "elapsed_ms": r.elapsed_ms,
            "attempts": r.attempts,
            "error": r.error,
            "response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "response": r.data if r.data is not None else text[:MAX_SAVED_TEXT],
        }
        path = self.raw_dir / name
        path.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
        with (self.raw_dir / "index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"file": name, "method": r.method, "path": r.path,
                                 "label": label, "status": r.status_code,
                                 "sha256": record["response_sha256"],
                                 "fetched_at": r.fetched_at}) + "\n")
        return str(path)
