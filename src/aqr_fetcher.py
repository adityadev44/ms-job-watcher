"""AQR India jobs from its official India-only Greenhouse board."""
from __future__ import annotations
import html, re, time
import requests

_TOKEN = "india"
_URL = f"https://boards-api.greenhouse.io/v1/boards/{_TOKEN}/jobs"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_jobs: list[dict] = []
_descriptions: dict[str, str] = {}
_filled = False

class RateLimitError(Exception): pass

def _text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(value or ""))).split())

def _fill(timeout: int) -> None:
    global _filled, _jobs
    if _filled: return
    _filled = True
    for attempt in range(3):
        try:
            response = requests.get(_URL, params={"content": "true"}, headers=_HEADERS, timeout=timeout)
            if response.status_code == 429: raise requests.RequestException("429")
            response.raise_for_status(); break
        except requests.RequestException as exc:
            if attempt == 2: raise RateLimitError(f"AQR Greenhouse fetch failed: {exc}") from exc
            time.sleep(2 ** attempt)
    rows = []
    for item in response.json().get("jobs", []):
        job_id, title = str(item.get("id") or ""), (item.get("title") or "").strip()
        if not job_id or not title: continue
        location = ((item.get("location") or {}).get("name") or "").strip()
        if "india" not in location.lower(): location = f"{location}, India" if location else "India"
        date = (item.get("updated_at") or "")[:10]
        url = item.get("absolute_url") or f"https://job-boards.greenhouse.io/{_TOKEN}/jobs/{job_id}"
        _descriptions[job_id] = item.get("content") or ""
        rows.append({"id": job_id, "title": title, "location": location, "posting_date": date, "application_url": url})
    _jobs = rows

def fetch_jobs(keyword: str, location: str, *, num: int = 20, start: int = 0, sort_by: str = "date", timeout: int = 20) -> list[dict]:
    _fill(timeout); return _jobs[start:start + num]

def fetch_job_description(application_url: str, timeout: int = 20) -> tuple[str, str]:
    _fill(timeout); job_id = application_url.rstrip("/").split("/")[-1]
    date = next((j["posting_date"] for j in _jobs if j["id"] == job_id), "")
    return _text(_descriptions.get(job_id, "")), date
