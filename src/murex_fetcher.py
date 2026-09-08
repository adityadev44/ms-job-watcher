"""Murex Mumbai jobs from the official Workday public API."""
from __future__ import annotations
import re,time,requests
from datetime import date,timedelta
from bs4 import BeautifulSoup
_BASE='https://murex.wd3.myworkdayjobs.com';_SITE='MurexCareerPage1';_SEARCH=f'{_BASE}/wday/cxs/murex/{_SITE}/jobs';_DETAIL=f'{_BASE}/wday/cxs/murex/{_SITE}';_PUBLIC=f'{_BASE}/{_SITE}'
_MUMBAI='7b6eeca552191001f2683ece97100000'
_HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36','Content-Type':'application/json'}
class RateLimitError(Exception):pass
def _posted(s):
 x=(s or '').lower();t=date.today()
 if 'today' in x:return str(t)
 if 'yesterday' in x:return str(t-timedelta(days=1))
 m=re.search(r'(\d+)\s+day',x);return str(t-timedelta(days=int(m.group(1)))) if m else ''
def _request(method,url,timeout,**kw):
 for a in range(3):
  try:
   r=method(url,headers=_HEADERS,timeout=timeout,**kw)
   if r.status_code==429:raise requests.RequestException('429')
   r.raise_for_status();return r
  except requests.RequestException as e:
   if a==2:raise RateLimitError(f'Murex fetch failed: {e}') from e
   time.sleep(2**a)
def fetch_jobs(keyword,location,*,num=20,start=0,sort_by='date',timeout=20):
 r=_request(requests.post,_SEARCH,timeout,json={'appliedFacets':{'locations':[_MUMBAI]},'limit':num,'offset':start,'searchText':keyword});out=[]
 for p in r.json().get('jobPostings',[]):
  path=p.get('externalPath','');title=p.get('title','').strip();jid=next((x for x in p.get('bulletFields',[]) if re.match(r'JR\d+',x,re.I)),'')
  if not jid:
   m=re.search(r'_(JR\d+)',path,re.I);jid=m.group(1).upper() if m else ''
  if jid and title:out.append({'id':jid,'title':title,'location':'Mumbai, India','posting_date':_posted(p.get('postedOn')),'application_url':_PUBLIC+path})
 return out
def fetch_job_description(application_url,timeout=20):
 path=application_url[len(_PUBLIC):] if application_url.startswith(_PUBLIC) else '/'+application_url.rsplit('/',1)[-1]
 info=_request(requests.get,_DETAIL+path,timeout).json().get('jobPostingInfo',{});desc=BeautifulSoup(info.get('jobDescription',''),'html.parser').get_text(' ',strip=True)
 return ' '.join(desc.split()),info.get('startDate','') or ''
