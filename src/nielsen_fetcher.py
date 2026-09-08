"""Nielsen jobs via its official SmartRecruiters tenant."""
from __future__ import annotations
import html,re,time
import requests
_BASE="https://api.smartrecruiters.com/v1/companies/TheNielsenCompany/postings";_PUBLIC="https://jobs.smartrecruiters.com/TheNielsenCompany";_HEADERS={"User-Agent":"Mozilla/5.0","Accept":"application/json"};_cache={}
class RateLimitError(Exception):pass
def _get(url,timeout,params=None):
 for attempt in range(3):
  try:
   r=requests.get(url,params=params,headers=_HEADERS,timeout=timeout)
   if r.status_code==429:raise requests.RequestException("429")
   r.raise_for_status();return r
  except requests.RequestException as exc:
   if attempt==2:raise RateLimitError(f"Nielsen SmartRecruiters fetch failed: {exc}") from exc
   time.sleep(2**attempt)
def _text(v):return " ".join(html.unescape(re.sub(r"<[^>]+>"," ",v or "")).split())
def fetch_jobs(keyword,location,*,num=20,start=0,sort_by="date",timeout=20):
 data=_get(_BASE,timeout,{"q":keyword,"country":"in","limit":min(num,100),"offset":start}).json();out=[]
 for x in data.get('content',[]):
  jid=str(x.get('id') or '');title=(x.get('name') or '').strip();loc=x.get('location') or {}
  if not jid or not title or (loc.get('country') or '').lower()!='in':continue
  place=(loc.get('fullLocation') or loc.get('city') or 'India').strip()
  if 'india' not in place.lower():place=f"{place}, India"
  out.append({"id":jid,"title":title,"location":place,"posting_date":(x.get('releasedDate') or '')[:10],"application_url":f"{_PUBLIC}/{jid}"})
 return out
def fetch_job_description(application_url,timeout=20):
 if application_url in _cache:return _cache[application_url]
 jid=application_url.rstrip('/').split('/')[-1].split('-')[0];x=_get(f"{_BASE}/{jid}",timeout).json();sections=(x.get('jobAd') or {}).get('sections') or {};body=' '.join(_text((sections.get(k) or {}).get('text') or '') for k in ('jobDescription','qualifications','additionalInformation'));result=(body,(x.get('releasedDate') or '')[:10]);_cache[application_url]=result;return result
