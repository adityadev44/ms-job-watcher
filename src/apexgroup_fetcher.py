"""Apex Group jobs via its official Workday tenant."""
from __future__ import annotations
import re,time,warnings
from datetime import date,timedelta
import requests
from bs4 import BeautifulSoup
_BASE="https://theapexgroup.wd3.myworkdayjobs.com";_SITE="apexgroupcareers";_TENANT="theapexgroup";_INDIA="c4f78be1a8f14da0ab49ce1162348a5e"
_SEARCH=f"{_BASE}/wday/cxs/{_TENANT}/{_SITE}/jobs";_DETAIL=f"{_BASE}/wday/cxs/{_TENANT}/{_SITE}";_JOB=f"{_BASE}/{_SITE}";_HEADERS={"User-Agent":"Mozilla/5.0","Accept":"application/json","Content-Type":"application/json"};_info={}
class RateLimitError(Exception):pass
def _request(method,url,timeout,**kw):
 for attempt in range(3):
  try:
   with warnings.catch_warnings():warnings.simplefilter("ignore");r=requests.request(method,url,headers=_HEADERS,timeout=timeout,verify=False,**kw)
   if r.status_code==429:raise requests.RequestException("429")
   r.raise_for_status();return r
  except requests.RequestException as exc:
   if attempt==2:raise RateLimitError(f"Apex Workday fetch failed: {exc}") from exc
   time.sleep(2**attempt)
def _date(v):
 s=(v or '').lower();d=date.today();m=re.search(r'(\d+)\s+day',s)
 if 'today' in s:return str(d)
 if 'yesterday' in s:return str(d-timedelta(days=1))
 return str(d-timedelta(days=int(m.group(1)))) if m else ''
def _detail_info(url,timeout):
 if url not in _info:_info[url]=_request('GET',f"{_DETAIL}{url.split(_JOB,1)[-1]}",timeout).json().get('jobPostingInfo',{})
 return _info[url]
def fetch_jobs(keyword,location,*,num=20,start=0,sort_by="date",timeout=20):
 j=_request('POST',_SEARCH,timeout,json={"appliedFacets":{"locationCountry":[_INDIA]},"limit":min(num,20),"offset":start,"searchText":keyword}).json();out=[]
 for x in j.get('jobPostings',[]):
  path=x.get('externalPath') or '';bf=x.get('bulletFields') or [];jid=str((bf[0] if bf else '') or path.rstrip('/').split('/')[-1]);title=(x.get('title') or '').strip();loc=(x.get('locationsText') or '').strip()
  if not jid or not title:continue
  url=f"{_JOB}{path}"
  if re.fullmatch(r'\d+ Locations?',loc,re.I):loc=(_detail_info(url,timeout).get('location') or loc).strip()
  if 'india' not in loc.lower():loc=f"{loc}, India" if loc else 'India'
  out.append({"id":jid,"title":title,"location":loc,"posting_date":_date(x.get('postedOn')),"application_url":url})
 return out
def fetch_job_description(application_url,timeout=20):
 j=_detail_info(application_url,timeout);raw=j.get('jobDescription') or '';return " ".join(BeautifulSoup(raw,'html.parser').get_text(' ').split()),(j.get('startDate') or '')[:10]
