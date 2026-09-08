"""Fetch Wolters Kluwer jobs from its official Workday tenant.

Live verified 2026-09-08 against wk.wd3.myworkdayjobs.com/External. The
tenant exposes no usable India country facet, so Workday full-text search is
used and every result is independently gated by its IND-prefixed location.
"""
from __future__ import annotations
import re, time, warnings
import requests
from bs4 import BeautifulSoup

_BASE = "https://wk.wd3.myworkdayjobs.com"
_SEARCH = f"{_BASE}/wday/cxs/wk/External/jobs"
_DETAIL = f"{_BASE}/wday/cxs/wk/External"
_PUBLIC = f"{_BASE}/en-US/External"
_HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36","Accept":"application/json","Content-Type":"application/json"}

class RateLimitError(Exception): pass

def _request(method, url, *, timeout, **kwargs):
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r=requests.request(method,url,headers=_HEADERS,timeout=timeout,verify=False,**kwargs)
            if r.status_code==429: raise RateLimitError("Wolters Kluwer Workday rate limited")
            r.raise_for_status(); return r
        except RateLimitError: raise
        except requests.RequestException as exc:
            if attempt==2: raise RateLimitError(f"Wolters Kluwer fetch failed: {exc}") from exc
            time.sleep(2**attempt)
    raise AssertionError("unreachable")

def _india_location(value):
    text=(value or "").strip()
    return bool(re.search(r"(^|[; ])IND(?:[- ,]|$)",text,re.I) or re.search(r"\bindia\b",text,re.I))

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
    data=_request("POST",_SEARCH,timeout=timeout,json={"appliedFacets":{},"limit":min(num,20),"offset":start,"searchText":keyword}).json()
    out=[]
    for p in data.get("jobPostings",[]):
        loc=(p.get("locationsText") or "").strip()
        if not _india_location(loc): continue
        if "india" not in loc.lower(): loc += ", India"
        path=p.get("externalPath") or ""
        jid=path.rsplit("_",1)[-1] if "_" in path else path
        if jid and path and p.get("title"):
            out.append({"id":jid,"title":p["title"].strip(),"location":loc,"posting_date":"","application_url":f"{_PUBLIC}{path}"})
    return out

def fetch_job_description(application_url, timeout=20):
    path=application_url.split("/en-US/External",1)[-1]
    info=_request("GET",f"{_DETAIL}{path}",timeout=timeout).json().get("jobPostingInfo",{})
    return BeautifulSoup(info.get("jobDescription") or "","html.parser").get_text(" ",strip=True), info.get("startDate") or ""

