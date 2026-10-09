"""Citizens' official Oracle CX_1 board; genuine zero India jobs is valid.

Read every page, preserving country evidence. Never infer India from the
employer's GCC announcement, an 'India' keyword or a US-remote label.
"""
import re

from bs4 import BeautifulSoup

from deferred_source_http import RateLimitError, get

BASE = "https://hcgn.fa.us2.oraclecloud.com"
API = BASE + "/hcmRestApi/resources/latest/"
JOB_BASE = BASE + "/hcmUI/CandidateExperience/en/sites/CX_1/job/"
_jobs = None
_error = None


def _india_locations(job):
    locations = []
    country = str(job.get("PrimaryLocationCountry") or "").upper()
    primary = job.get("PrimaryLocation") or ""
    if country in {"IN", "IND", "INDIA"} or (not country and re.search(r"\bindia\b", primary, re.I)):
        locations.append(primary if re.search(r"\bindia\b", primary, re.I) else primary + ", India")
    for secondary in job.get("secondaryLocations") or []:
        name = secondary.get("LocationName") or secondary.get("Location") or ""
        country = str(secondary.get("Country") or "").upper()
        if country in {"IN", "IND", "INDIA"} or (not country and re.search(r"\bindia\b", name, re.I)):
            locations.append(name if re.search(r"\bindia\b", name, re.I) else name + ", India")
    return "; ".join(dict.fromkeys(locations))


def _fill(timeout):
    global _jobs, _error
    if _error:
        raise _error
    if _jobs is not None:
        return
    collected = {}
    offset = 0
    seen = set()
    try:
        while True:
            params = {"onlyData": "true", "expand": "requisitionList.workLocation,requisitionList.secondaryLocations",
                      "finder": f"findReqs;siteNumber=CX_1,limit=100,offset={offset},sortBy=POSTING_DATES_DESC"}
            data = get(API + "recruitingCEJobRequisitions", timeout, params=params).json()
            items = data.get("items")
            if not items or "TotalJobsCount" not in items[0] or "requisitionList" not in items[0]:
                raise RateLimitError("Citizens: invalid Oracle inventory response")
            page = items[0]["requisitionList"]
            total = int(items[0]["TotalJobsCount"])
            if not page:
                if offset < total:
                    raise RateLimitError("Citizens: inventory stopped before advertised total")
                break
            ids = {str(j.get("Id") or "") for j in page}
            if "" in ids or not ids.difference(seen):
                raise RateLimitError("Citizens: missing IDs or repeated inventory page")
            seen.update(ids)
            for job in page:
                location = _india_locations(job)
                if location:
                    title = (job.get("Title") or "").strip()
                    if not title:
                        raise RateLimitError("Citizens: missing title")
                    job_id = str(job["Id"])
                    collected[job_id] = {"id": job_id, "title": title, "location": location,
                                         "posting_date": str(job.get("PostedDate") or "")[:10],
                                         "application_url": JOB_BASE + job_id}
            offset += len(page)
            if offset >= total:
                break
        _jobs = list(collected.values())
    except (RateLimitError, ValueError, TypeError, AttributeError, KeyError) as exc:
        _error = RateLimitError(f"Citizens inventory failed: {exc}")
        raise _error from exc


def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    _fill(timeout)
    return _jobs[start:start + num]


def fetch_job_description(application_url, timeout=20):
    if not re.fullmatch(re.escape(JOB_BASE) + r"\d+", application_url):
        raise RateLimitError("Citizens: invalid job URL")
    job_id = application_url.rsplit("/", 1)[-1]
    params = {"onlyData": "true", "expand": "all", "finder": f'ById;Id="{job_id}",siteNumber=CX_1'}
    items = get(API + "recruitingCEJobRequisitionDetails", timeout, params=params).json().get("items") or []
    if not items:
        raise RateLimitError("Citizens: job detail missing")
    job = items[0]
    content = " ".join(job.get(k) or "" for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
    description = BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
    if not description:
        raise RateLimitError("Citizens: empty description")
    return description, str(job.get("ExternalPostedStartDate") or "")[:10]
