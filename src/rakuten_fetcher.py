"""Rakuten India jobs from its verified Zwayam career board.

Official corp.rakuten.co.in homepage now links rakuten.openings.co; old
/careers URLs are dead. Firefox network verified apic2.zwayam.com,
companyId 15124 (search base64 MTUxMjQ=), and public detail endpoint.
Plain HTTP works with the frontend's Origin/Referer/Accept headers.
2026-10-09: seven Bengaluru India roles, some list descriptions empty;
always use longDescription from the verified detail API.
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup

_BASE = "https://rakuten.openings.co/rakuten"
_API = "https://apic2.zwayam.com"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*",
            "Origin": "https://rakuten.openings.co", "Referer": "https://rakuten.openings.co/"}
_jobs = None
_cache_error = None
_desc_cache = {}

class RateLimitError(Exception):
    """The board failed after bounded retries."""

def _post(path, timeout, **kw):
    for attempt in range(3):
        try:
            response = requests.post(_API + path, headers=_HEADERS, timeout=timeout, **kw)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid Zwayam response")
            return data
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Rakuten: {exc}") from exc
            time.sleep(2 ** attempt)

def _date(raw):
    try:
        return datetime.strptime(raw or "", "%d-%b-%Y").date().isoformat()
    except ValueError:
        return ""

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    global _jobs, _cache_error
    if _cache_error is not None:
        raise _cache_error
    if _jobs is None:
        _jobs = []
        offset = 0
        seen = set()
        try:
            for page in range(100):
                criteria = {"paginationStartNo": offset, "selectedCall": "sort",
                            "sortCriteria": {"name": "modifiedDate", "isAscending": False},
                            "anyOfTheseWords": ""}
                data = _post("/jobs/search", timeout,
                             files={"filterCri": (None, json.dumps(criteria)),
                                    "domain": (None, "rakuten.openings.co"),
                                    "companyId": (None, "MTUxMjQ=")}).get("data")
                if not isinstance(data, dict):
                    raise RateLimitError("Rakuten: missing search data")
                rows = data.get("data") or []
                new = 0
                for row in rows:
                    job = row.get("_source") or {}
                    jid = str(job.get("id") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    new += 1
                    loc = job.get("locationSeparatedbySlash") or job.get("location") or ""
                    if not re.search(r"\bindia\b", loc, re.I):
                        continue
                    if not job.get("jobTitle") or not job.get("jobUrl"):
                        continue
                    _jobs.append({"id": jid, "title": job["jobTitle"].strip(), "location": loc,
                                  "posting_date": _date(job.get("createDate")),
                                  "application_url": _BASE + "/jobview/" + job["jobUrl"]})
                if not rows or not new or not data.get("hasMoreData"):
                    break
                offset += len(rows)
            else:
                raise RateLimitError("Rakuten: search exceeded pagination bound")
        except RateLimitError as exc:
            _cache_error = exc
            raise
    return _jobs[start:start + num]

def fetch_job_description(application_url, timeout=20):
    if application_url not in _desc_cache:
        if "/jobview/" not in application_url:
            raise RateLimitError("Rakuten: missing job URL")
        slug = application_url.rsplit("/jobview/", 1)[-1]
        job = _post("/jobs-service/v1/jobs/careersite", timeout,
                    json={"jobUrl": slug, "externalSource": "CAREERSITE",
                          "campusUrl": "empty", "companyId": "15124"})
        desc = BeautifulSoup(job.get("longDescription") or "", "html.parser").get_text(" ", strip=True)
        _desc_cache[application_url] = (desc, _date(job.get("createDate")))
    return _desc_cache[application_url]
