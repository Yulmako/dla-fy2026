"""Official USAspending transaction downloads (POST /api/v2/download/transactions/).

Flow: check size with /download/count/ -> submit job -> poll /download/status/
-> stream the generated zip from files.usaspending.gov (official file host) ->
checksum -> extract CSV(s). Raw zips are kept under data/raw/ for traceability.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
import logging
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from src.api.client import UsaSpendingClient

log = logging.getLogger("fci.download")


class DownloadError(RuntimeError):
    pass


@dataclass
class DownloadResult:
    basis: str
    file_name: str
    file_url: str
    zip_path: Path
    sha256: str
    bytes: int
    csv_paths: list[Path] = field(default_factory=list)
    expected_rows: int | None = None
    status_payload: dict[str, Any] = field(default_factory=dict)
    plan: Any = None


def check_count(client: UsaSpendingClient, filters: dict) -> dict[str, Any]:
    r = client.post("/api/v2/download/count/", {"filters": filters}, label="download_count")
    if not r.ok:
        raise DownloadError(f"download/count failed: {r.error_detail()}")
    d = r.data or {}
    count = d.get("calculated_transaction_count", d.get("calculated_count"))
    limit = d.get("maximum_transaction_limit", d.get("maximum_limit"))
    if count is None or limit is None:
        raise DownloadError(f"download/count returned unexpected payload: {d}")
    if count > limit:
        raise DownloadError(f"{count} rows exceed the download limit {limit}; split the extract")
    return {"count": int(count), "limit": int(limit)}


def submit(client: UsaSpendingClient, filters: dict, label: str) -> dict[str, Any]:
    r = client.post("/api/v2/download/transactions/",
                    {"filters": filters, "file_format": "csv"}, label=f"download_submit_{label}")
    if not r.ok or not (r.data or {}).get("file_name"):
        raise DownloadError(f"download submit failed: {r.error_detail() or r.data}")
    return r.data


def wait_until_ready(client: UsaSpendingClient, file_name: str, poll_seconds: float = 15,
                     timeout_seconds: float = 1800, sleep=time.sleep) -> dict[str, Any]:
    waited = 0.0
    while True:
        r = client.get("/api/v2/download/status/", params={"file_name": file_name},
                       label="download_status")
        d = r.data or {}
        status = str(d.get("status", "")).lower()
        if r.ok and status == "finished" and d.get("file_url"):
            return d
        if status == "failed":
            raise DownloadError(f"download job failed: {d.get('message')}")
        if waited >= timeout_seconds:
            raise DownloadError(f"download not ready after {timeout_seconds}s (status={status})")
        log.info("download %s status=%s, waiting %ss", file_name, status or r.status_code,
                 poll_seconds)
        sleep(poll_seconds)
        waited += poll_seconds


def fetch_file(url: str, dest_dir: Path, session: requests.Session | None = None,
               timeout: float = 300, attempts: int = 5, sleep=time.sleep) -> tuple[Path, str, int]:
    """Stream a generated download zip to disk and return (path, sha256, bytes).
    A dropped connection (incomplete read) or a short/corrupt file is retried
    from scratch up to `attempts` times; a file is accepted only when its size
    matches Content-Length (if sent) and it opens as a valid zip."""
    if not url.startswith("https://files.usaspending.gov/"):
        raise DownloadError(f"refusing to fetch from non-official host: {url}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / url.rsplit("/", 1)[-1]
    sess = session or requests.Session()
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        h = hashlib.sha256()
        size = 0
        try:
            with sess.get(url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                expected = int(resp.headers.get("Content-Length") or 0)
                with path.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
                            h.update(chunk)
                            size += len(chunk)
            if expected and size != expected:
                raise DownloadError(f"short file: {size} of {expected} bytes")
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
                if bad:
                    raise DownloadError(f"corrupt member {bad}")
            return path, h.hexdigest(), size
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout, zipfile.BadZipFile, DownloadError) as exc:
            last = exc
            log.warning("download of %s failed (attempt %d/%d): %s", url, attempt, attempts, exc)
            if attempt < attempts:
                sleep(min(60, 5 * attempt))
    raise DownloadError(f"could not download {url} after {attempts} attempts: {last}")


def extract_contract_csvs(zip_path: Path, dest_dir: Path) -> list[Path]:
    """Extract prime contract transaction CSV(s); ignore other files in the zip."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = []
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        # prime transactions only: the zip also carries a Contracts_Subawards file
        preferred = [n for n in names if "primetransaction" in n.lower().replace("_", "")]
        if not preferred:
            preferred = [n for n in names if "contract" in n.lower() and "subaward" not in n.lower()]
        for n in preferred:
            target = dest_dir / Path(n).name
            with zf.open(n) as src, target.open("wb") as dst:
                while True:
                    buf = src.read(1 << 20)
                    if not buf:
                        break
                    dst.write(buf)
            out.append(target)
    if not out:
        raise DownloadError(f"no CSV found in {zip_path}")
    return sorted(out)


@dataclass
class JobPlan:
    label: str
    basis: str
    market: str
    filters: dict
    expected_rows: int
    file_name: str | None = None


def _count(client: UsaSpendingClient, filters: dict) -> tuple[int, int]:
    r = client.post("/api/v2/download/count/", {"filters": filters}, label="download_count")
    if not r.ok:
        raise DownloadError(f"download/count failed: {r.error_detail()}")
    d = r.data or {}
    count = d.get("calculated_transaction_count", d.get("calculated_count"))
    limit = d.get("maximum_transaction_limit", d.get("maximum_limit"))
    if count is None or limit is None:
        raise DownloadError(f"download/count returned unexpected payload: {d}")
    return int(count), int(limit)


def plan_jobs(client: UsaSpendingClient, filters: dict, basis: str, market: str,
              fiscal_months: list[int] | None = None, halves: bool = False) -> list[JobPlan]:
    """One job per basis; split into fiscal months when over the row limit.
    With `fiscal_months` only those FY2026 months are planned (one job each) –
    a partial, clearly labelled test extract. `halves` splits each month into
    1st–15th and 16th–end: a request USAspending has not seen before, so it builds
    a fresh file instead of re-serving a cached (possibly incomplete) one."""
    from src.fiscal import fiscal_month_range
    if not fiscal_months:
        count, limit = _count(client, filters)
        if count <= limit:
            return [JobPlan(f"{market}_{basis}", basis, market, filters, count)]
    plans = []
    for fm in (sorted(set(fiscal_months)) if fiscal_months else range(1, 13)):
        start, end = fiscal_month_range(fm)
        ranges = [(start, end, "")]
        if halves:
            mid = start.replace(day=15)
            ranges = [(start, mid, "a"), (mid.replace(day=16), end, "b")]
        for a, b, tag in ranges:
            f = dict(filters, time_period=[{"start_date": a.isoformat(), "end_date": b.isoformat()}])
            c, lim = _count(client, f)
            if c > lim:
                raise DownloadError(f"{market}/{basis} fiscal month {fm}{tag} has {c} rows > limit "
                                    f"{lim}; narrow the market or split further")
            if c:
                plans.append(JobPlan(f"{market}_{basis}_fm{fm:02d}{tag}", basis, market, f, c))
    log.info("%s/%s: %d monthly jobs (months %s)", market, basis, len(plans),
             [p.label.rsplit("_fm", 1)[-1] for p in plans])
    return plans


def plan_range(plan: "JobPlan") -> tuple[date, date]:
    tp = plan.filters["time_period"][0]
    return date.fromisoformat(tp["start_date"]), date.fromisoformat(tp["end_date"])


def split_plan(client: UsaSpendingClient, plan: "JobPlan") -> list["JobPlan"]:
    """Two jobs covering the plan's date range (first half, second half). Used
    when USAspending returns a truncated file: smaller, never-seen requests make
    the server build new files. A one-day plan cannot be split (returns [])."""
    a, b = plan_range(plan)
    if a >= b:
        return []
    mid = a + (b - a) // 2
    out = []
    for i, (x, y) in enumerate(((a, mid), (mid + timedelta(days=1), b))):
        f = dict(plan.filters, time_period=[{"start_date": x.isoformat(), "end_date": y.isoformat()}])
        c, _ = _count(client, f)
        if c:
            out.append(JobPlan(f"{plan.label}_{x:%m%d}-{y:%m%d}", plan.basis, plan.market, f, c))
    return out


def run_jobs(client: UsaSpendingClient, plans: list[JobPlan], raw_dir: Path,
             session: requests.Session | None = None, poll_seconds: float = 15,
             timeout_seconds: float = 3600, sleep=time.sleep) -> list[DownloadResult]:
    """Submit every job first (USAspending builds them in parallel), then poll and fetch."""
    for p in plans:
        p.file_name = submit(client, p.filters, p.label)["file_name"]
    results = []
    for p in plans:
        status = wait_until_ready(client, p.file_name, poll_seconds=poll_seconds,
                                  timeout_seconds=timeout_seconds, sleep=sleep)
        zip_path, sha, size = fetch_file(status["file_url"], raw_dir, session=session)
        csvs = extract_contract_csvs(zip_path, raw_dir / f"{p.label}_csv")
        results.append(DownloadResult(basis=p.basis, file_name=p.file_name,
                                      file_url=status["file_url"], zip_path=zip_path, sha256=sha,
                                      bytes=size, csv_paths=csvs, expected_rows=p.expected_rows,
                                      status_payload={"market": p.market, "label": p.label,
                                                      **status}, plan=p))
    return results


def download_basis(client: UsaSpendingClient, filters: dict, basis: str, raw_dir: Path,
                   session: requests.Session | None = None, poll_seconds: float = 15,
                   sleep=time.sleep, market: str = "market") -> DownloadResult:
    """Single-job convenience wrapper (kept for tests and small extracts)."""
    plans = plan_jobs(client, filters, basis, market)
    if len(plans) != 1:
        raise DownloadError("extract needs a monthly split; use plan_jobs/run_jobs")
    return run_jobs(client, plans, raw_dir, session=session, poll_seconds=poll_seconds,
                    sleep=sleep)[0]
