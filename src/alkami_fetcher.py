"""Alkami: verified Workday CXS tenant/site, India location facet.

Verified 2026-10-09 from official careers link and live API. SearchText
filters server-side. The location facet includes multi-location openings;
do not filter on the collapsed locationsText before resolving descriptions.
"""
from __future__ import annotations
import re
import time
from datetime import date, timedelta
import requests
from bs4 import BeautifulSoup
_BASE = "https://alkami.wd12.myworkdayjobs.com"
_SITE = "Alkami"
_TENANT = "alkami"
_FACET = "e4db8978e27610015c300d8c03650000"
_API = f"{_BASE}/wday/cxs/{_TENANT}/{_SITE}"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_desc_cache = {}
_first_ids = {}

class RateLimitError(Exception):
    """Bounded HTTP retries failed."""

def _request(method, url, timeout, **kwargs):
    for attempt in range(3):
        try:
            r = requests.request(method, url, headers=_HEADERS, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Alkami: {exc}") from exc
            time.sleep(2 ** attempt)

def _date(raw):
    raw = (raw or "").lower()
    if "today" in raw:
        return date.today().isoformat()
    if "yesterday" in raw:
        return (date.today() - timedelta(days=1)).isoformat()
    # 30+ is a lower-bound estimate, not a genuine posting timestamp.
    if "30+" in raw:
        return ""
    match = re.search(r"(\d+) days? ago", raw)
    return (date.today() - timedelta(days=int(match.group(1)))).isoformat() if match else ""

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    data = _request("POST", _API + "/jobs", timeout,
                    json={"searchText": keyword, "limit": min(num, 20),
                          "offset": start, "appliedFacets": {"locations": [_FACET]}})
    rows = data.get("jobPostings", [])
    ids = {r["externalPath"] for r in rows}
    if start == 0:
        _first_ids[keyword] = ids
    elif ids and ids == _first_ids.get(keyword):
        return []
    result = []
    for row in rows:
        path = row.get("externalPath", "")
        if not path or not row.get("title"):
            continue
        loc = row.get("locationsText") or ""
        if "india" not in loc.lower():
            # This is safe only because the verified India facet is applied.
            loc = "Gurugram, India" if re.search(r"\d+ Locations", loc) else loc + ", India"
        jid = str((row.get("bulletFields") or [path.rsplit("_", 1)[-1]])[0]).replace(" ", "")
        result.append({"id": jid, "title": row["title"], "location": loc,
                       "posting_date": _date(row.get("postedOn")),
                       "application_url": f"{_BASE}/{_SITE}{path}"})
    return result

def fetch_job_description(application_url, timeout=20):
    if application_url not in _desc_cache:
        path = application_url.split(f"/{_SITE}", 1)[-1]
        if not path.startswith("/job/"):
            raise RateLimitError("Invalid Workday job URL")
        info = _request("GET", _API + path, timeout).get("jobPostingInfo", {})
        desc = BeautifulSoup(info.get("jobDescription") or "", "html.parser").get_text(" ", strip=True)
        _desc_cache[application_url] = (desc, (info.get("startDate") or "")[:10])
    return _desc_cache[application_url]
