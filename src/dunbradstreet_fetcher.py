"""Dun & Bradstreet jobs via the official public Lever board ``dnb``."""
from __future__ import annotations
import re, time, requests

_API = "https://api.lever.co/v0/postings/dnb?mode=json"
_HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"}
_CACHE = None
_DESC = {}
class RateLimitError(Exception): pass

def _fill(timeout):
    global _CACHE
    if _CACHE is not None: return
    for attempt in range(3):
        try:
            r=requests.get(_API,headers=_HEADERS,timeout=timeout)
            if r.status_code==429: raise requests.RequestException("429")
            r.raise_for_status(); break
        except requests.RequestException as exc:
            if attempt==2: raise RateLimitError(f"Dun & Bradstreet fetch failed: {exc}") from exc
            time.sleep(2**attempt)
    out=[]
    for p in r.json():
        loc=(p.get("categories") or {}).get("location","").strip()
        if not re.search(r"\bindia\b",loc,re.I): continue
        jid=str(p.get("id","")).strip(); title=p.get("text","").strip(); url=p.get("hostedUrl","")
        if not jid or not title: continue
        ms=p.get("createdAt"); date=time.strftime("%Y-%m-%d",time.gmtime(ms/1000)) if isinstance(ms,(int,float)) else ""
        desc=" ".join((p.get("descriptionPlain","")+" "+p.get("additionalPlain","")).split())
        out.append({"id":jid,"title":title,"location":loc,"posting_date":date,"application_url":url})
        _DESC[url]=(desc,date)
    _CACHE=out

def fetch_jobs(keyword,location,*,num=20,start=0,sort_by="date",timeout=20):
    _fill(timeout); return _CACHE[start:start+num]
def fetch_job_description(application_url,timeout=20):
    _fill(timeout); return _DESC.get(application_url,("",""))
