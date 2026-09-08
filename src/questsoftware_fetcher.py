"""Fetch Quest Software jobs from its official iCIMS careers portal.

Live verified 2026-09-08. The public iframe view exposes a small two-page
global board; it is cached once and India is identified from APJ-IN location
codes. This is Quest Software, not Quest Diagnostics or Quest Global.
"""
from __future__ import annotations
import re,time
import requests
from bs4 import BeautifulSoup
_BASE="https://careers-quest.icims.com"
_HEADERS={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"}
_cache=None
class RateLimitError(Exception): pass

def _get(url,timeout):
 for attempt in range(3):
  try:
   r=requests.get(url,headers=_HEADERS,timeout=timeout)
   if r.status_code==429: raise RateLimitError("Quest iCIMS rate limited")
   r.raise_for_status();return r
  except RateLimitError:raise
  except requests.RequestException as exc:
   if attempt==2:raise RateLimitError(f"Quest fetch failed: {exc}") from exc
   time.sleep(2**attempt)
 raise AssertionError("unreachable")

def _all(timeout):
 global _cache
 if _cache is not None:return _cache
 out=[]
 for page in range(10):
  soup=BeautifulSoup(_get(f"{_BASE}/jobs/search?pr={page}&in_iframe=1",timeout).text,"html.parser")
  rows=soup.select(".iCIMS_JobsTable .row")
  if not rows:break
  for row in rows:
   a=row.select_one('a[href*="/jobs/"][href*="/job"]')
   text=row.get_text(" ",strip=True)
   locm=re.search(r"Location\s+(.+?)\s+Category\s+",text)
   idm=re.search(r"Job ID\s+(\S+)",text)
   if not (a and locm and idm):continue
   loc=locm.group(1).strip()
   if not (loc.upper().startswith("APJ-IN") or re.search(r"\bindia\b",loc,re.I)):continue
   if "india" not in loc.lower():loc += ", India"
   url=a.get("href","").replace("&amp;","&")
   out.append({"id":idm.group(1),"title":re.sub(r"^Title\s+","",a.get_text(" ",strip=True)),"location":loc,"posting_date":"","application_url":url})
  if not soup.select_one(f'a[href*="pr={page+1}"]'):break
 _cache=list({j["id"]:j for j in out}.values());return _cache

def fetch_jobs(keyword,location,*,num=20,start=0,sort_by="date",timeout=20):
 return _all(timeout)[start:start+num]

def fetch_job_description(application_url,timeout=20):
 soup=BeautifulSoup(_get(application_url,timeout).text,"html.parser")
 body=soup.select_one(".iCIMS_JobContent") or soup.select_one("#iCIMS_JobContent")
 return (body.get_text(" ",strip=True) if body else "","")

