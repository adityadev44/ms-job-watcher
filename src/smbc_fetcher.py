"""SMBC Asia jobs from its official SuccessFactors J2W board."""
from __future__ import annotations
import html,re,time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
_BASE="https://careerasia.smbc.co.jp";_SEARCH=f"{_BASE}/SMBC/search/";_HEADERS={"User-Agent":"Mozilla/5.0","Accept":"text/html"};_detail={}
class RateLimitError(Exception):pass
def _get(url,timeout,params=None):
 for attempt in range(3):
  try:
   r=requests.get(url,params=params,headers=_HEADERS,timeout=timeout)
   if r.status_code==429:raise requests.RequestException("429")
   r.raise_for_status();return r
  except requests.RequestException as exc:
   if attempt==2:raise RateLimitError(f"SMBC SuccessFactors fetch failed: {exc}") from exc
   time.sleep(2**attempt)
def fetch_jobs(keyword,location,*,num=20,start=0,sort_by="date",timeout=20):
 r=_get(_SEARCH,timeout,{"q":keyword,"locationsearch":location or "India","startrow":start});s=BeautifulSoup(r.text,'html.parser');out=[];seen=set()
 for row in s.select('tr.data-row'):
  a=row.select_one('a.jobTitle-link');loc=row.select_one('span.jobLocation')
  if not a:continue
  href=a.get('href') or '';jid=href.rstrip('/').split('/')[-1]
  if not jid.isdigit() or jid in seen:continue
  seen.add(jid);place=html.unescape(loc.get_text(' ',strip=True)) if loc else 'India';place=re.sub(r',\s*IN\b',', India',place)
  url=f"{_BASE}{href}";_body,posted,office=_load_detail(url,timeout);place=office or place
  if 'india' not in place.lower():place=f"{place}, India" if place else 'India'
  out.append({"id":jid,"title":html.unescape(a.get_text(' ',strip=True)),"location":place,"posting_date":posted,"application_url":url})
 return out[:num]
def _load_detail(application_url,timeout):
 if application_url in _detail:return _detail[application_url]
 s=BeautifulSoup(_get(application_url,timeout).text,'html.parser');node=s.select_one('span.jobdescription');body=html.unescape(node.get_text(' ',strip=True)) if node else '';meta=s.find('meta',{"itemprop":"datePosted"});raw=meta.get('content','') if meta else ''
 try:d=datetime.strptime(raw.strip(),'%a %b %d %H:%M:%S UTC %Y').strftime('%Y-%m-%d')
 except ValueError:d=''
 office_node=s.select_one('[data-careersite-propertyid="customfield1"]');office=html.unescape(office_node.get_text(' ',strip=True)) if office_node else ''
 _detail[application_url]=(body,d,office);return _detail[application_url]
def fetch_job_description(application_url,timeout=20):
 body,d,_office=_load_detail(application_url,timeout);return body,d
