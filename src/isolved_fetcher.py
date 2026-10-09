"""isolved public SSR inventory and JobPosting JSON-LD, no browser needed."""
import json
import re

from bs4 import BeautifulSoup

from deferred_source_http import RateLimitError, get

BASE = "https://isolved.isolvedhire.com"
_jobs = None
_error = None
_descriptions = {}


def _detail(url, timeout):
    if not re.fullmatch(r"https://isolved\.isolvedhire\.com/jobs/\d+\.html", url):
        raise RateLimitError("isolved: invalid job URL")
    soup = BeautifulSoup(get(url, timeout).text, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        data = json.loads(script.get_text())
        candidates = data if isinstance(data, list) else data.get("@graph", [data])
        for job in candidates:
            if job.get("@type") == "JobPosting":
                description = BeautifulSoup(job.get("description") or "", "html.parser").get_text(" ", strip=True)
                if not description:
                    raise RateLimitError("isolved: missing job description")
                return job, description
    raise RateLimitError("isolved: missing JobPosting data")


def _fill(timeout):
    global _jobs, _error
    if _error:
        raise _error
    if _jobs is not None:
        return
    try:
        soup = BeautifulSoup(get(BASE + "/jobsandemployment/", timeout).text, "html.parser")
        marker = re.search(r"Displaying\s+(\d+)\s+listing", soup.get_text(" ", strip=True))
        if not marker:
            raise RateLimitError("isolved: inventory marker missing, possible downtime")
        links = {}
        all_links = set()
        for a in soup.find_all("a", href=True):
            url = a["href"]
            if re.fullmatch(re.escape(BASE) + r"/jobs/\d+\.html", url):
                all_links.add(url)
                # The anchor contains the title, department AND structured location.
                if re.search(r"\bIND\b|\bIndia\b", a.get_text(" ", strip=True), re.I):
                    links[url] = a.get_text(" ", strip=True)
        if len(all_links) != int(marker.group(1)):
            raise RateLimitError("isolved: inventory count disagrees with parsed job links")
        collected = []
        for url in links:
            job, description = _detail(url, timeout)
            places = job.get("jobLocation") or []
            if isinstance(places, dict):
                places = [places]
            locations = []
            for place in places:
                address = place.get("address") or {}
                country = address.get("addressCountry")
                if isinstance(country, dict):
                    country = country.get("name")
                if str(country).upper() in {"IN", "IND", "INDIA"}:
                    locations.append(", ".join(str(address[k]) for k in ("addressLocality", "addressRegion") if address.get(k)) + ", India")
            if not locations:
                raise RateLimitError("isolved: India card lacks matching structured country")
            date = str(job.get("datePosted") or "")[:10]
            title = (job.get("title") or "").strip()
            if not title:
                raise RateLimitError("isolved: missing title")
            collected.append({"id": re.search(r"/jobs/(\d+)", url).group(1), "title": title,
                              "location": "; ".join(locations), "posting_date": date, "application_url": url})
            _descriptions[url] = (description, date)
        _jobs = collected
    except (RateLimitError, ValueError, TypeError, AttributeError) as exc:
        _error = RateLimitError(f"isolved inventory failed: {exc}")
        raise _error from exc


def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    _fill(timeout)
    return _jobs[start:start + num]


def fetch_job_description(application_url, timeout=20):
    if application_url in _descriptions:
        return _descriptions[application_url]
    job, description = _detail(application_url, timeout)
    return description, str(job.get("datePosted") or "")[:10]
