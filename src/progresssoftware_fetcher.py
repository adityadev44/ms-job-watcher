"""Fetch Progress Software jobs from its official server-rendered careers site.

Live verified 2026-09-08. The board's query parameters do not restrict its
server-rendered rows, so the full small board is cached and filtered to India.
"""
from __future__ import annotations
import re,time
import requests
from bs4 import BeautifulSoup
_LIST="https://www.progress.com/company/careers/open-positions"
_HEADERS={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"}
_cache=None
class RateLimitError(Exception): pass

def _get(url,timeout):
 for attempt in range(3):
  try:
   r=requests.get(url,headers=_HEADERS,timeout=timeout)
   if r.status_code==429: raise RateLimitError("Progress careers rate limited")
   r.raise_for_status(); return r
  except RateLimitError: raise
  except requests.RequestException as exc:
   if attempt==2: raise RateLimitError(f"Progress careers fetch failed: {exc}") from exc
   time.sleep(2**attempt)
 raise AssertionError("unreachable")

def _all(timeout):
 global _cache
 if _cache is not None:return _cache
 soup=BeautifulSoup(_get(_LIST,timeout).text,"html.parser"); out=[]
 for a in soup.select('a[href*="/company/careers/open-positions/"]'):
  row=a.find_parent(attrs={"item-id":True})
  if not row: continue
  text=row.get_text(" ",strip=True)
  if not re.search(r"\bindia\b",text,re.I): continue
  loc=text.replace(a.get_text(" ",strip=True),"",1).strip()
  url=a.get("href",""); jid=row.get("item-id") or url.rsplit("-",1)[-1]
  if url and jid: out.append({"id":jid,"title":a.get_text(" ",strip=True),"location":loc,"posting_date":"","application_url":url})
 _cache=list({j["id"]:j for j in out}.values()); return _cache

def fetch_jobs(keyword,location,*,num=20,start=0,sort_by="date",timeout=20):
 return _all(timeout)[start:start+num]

def fetch_job_description(application_url,timeout=20):
 soup=BeautifulSoup(_get(application_url,timeout).text,"html.parser")
 heading=next((h for h in soup.find_all(["h1","h2"]) if h.get_text(" ",strip=True)=="Job Summary"),None)
 container=heading.parent if heading else soup.find("main")
 return (container.get_text(" ",strip=True) if container else "","")

