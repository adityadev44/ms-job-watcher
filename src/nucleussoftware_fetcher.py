"""Nucleus Software jobs via its official Zoho Recruit public endpoint."""
from __future__ import annotations
import datetime,time,requests
_API='https://nucleussoftware.zohorecruit.in/recruit/v2/public/Job_Openings?pagename=Careers&source=CareerSite'
_HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36'}
_CACHE=None;_DESC={}
class RateLimitError(Exception):pass
def _fill(timeout):
 global _CACHE
 if _CACHE is not None:return
 for a in range(3):
  try:
   r=requests.get(_API,headers=_HEADERS,timeout=timeout)
   if r.status_code==429:raise requests.RequestException('429')
   r.raise_for_status();break
  except requests.RequestException as e:
   if a==2:raise RateLimitError(f'Nucleus Software fetch failed: {e}') from e
   time.sleep(2**a)
 out=[]
 for p in r.json().get('data',[]):
  if str(p.get('Country','')).lower()!='india' or not p.get('Publish',True):continue
  jid=str(p.get('id','')).strip();title=(p.get('Posting_Title') or p.get('Job_Opening_Name') or '').strip();url=p.get('$url','')
  if not jid or not title:continue
  city=(p.get('City') or '').strip();state=(p.get('State') or '').strip();loc=', '.join(x for x in (city,state,'India') if x)
  try:dt=datetime.datetime.strptime(p.get('Date_Opened',''),'%d/%m/%Y').date().isoformat()
  except (ValueError,TypeError):dt=''
  desc=' '.join((p.get('Job_Description') or '').split());out.append({'id':jid,'title':title,'location':loc,'posting_date':dt,'application_url':url});_DESC[url]=(desc,dt)
 _CACHE=out
def fetch_jobs(keyword,location,*,num=20,start=0,sort_by='date',timeout=20):
 _fill(timeout);return _CACHE[start:start+num]
def fetch_job_description(application_url,timeout=20):
 _fill(timeout);return _DESC.get(application_url,('',''))
