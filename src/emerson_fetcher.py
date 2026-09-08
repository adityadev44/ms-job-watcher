"""Emerson jobs via its official Oracle HCM Candidate Experience API."""
from __future__ import annotations
import html,re,time
import requests
_BASE="https://hdjq.fa.us2.oraclecloud.com";_SITE="CX_1";_INDIA=300000000228786;_SEARCH=f"{_BASE}/hcmRestApi/resources/latest/recruitingCEJobRequisitions";_DETAIL=f"{_BASE}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails";_JOB=f"{_BASE}/hcmUI/CandidateExperience/en/sites/{_SITE}/job";_HEADERS={"User-Agent":"Mozilla/5.0","Accept":"application/json","ora-irc-language":"US"}
class RateLimitError(Exception):pass
def _get(url,params,timeout):
 for attempt in range(3):
  try:
   r=requests.get(url,params=params,headers=_HEADERS,timeout=timeout)
   if r.status_code==429:raise requests.RequestException('429')
   r.raise_for_status();return r
  except requests.RequestException as exc:
   if attempt==2:raise RateLimitError(f"Emerson Oracle fetch failed: {exc}") from exc
   time.sleep(2**attempt)
def _text(v):return ' '.join(html.unescape(re.sub(r'<[^>]+>',' ',v or '')).split())
def fetch_jobs(keyword,location,*,num=20,start=0,sort_by='date',timeout=20):
 finder=f'findReqs;siteNumber={_SITE},facetsList=LOCATIONS,limit={num},offset={start},keyword="{keyword}",sortBy=POSTING_DATES_DESC,selectedLocationsFacet={_INDIA}';j=_get(_SEARCH,{"onlyData":"true","expand":"requisitionList.workLocation,requisitionList.secondaryLocations","finder":finder},timeout).json();items=j.get('items') or [];out=[]
 for x in ((items[0].get('requisitionList') or []) if items else []):
  jid=str(x.get('Id') or '');title=(x.get('Title') or '').strip();loc=(x.get('PrimaryLocation') or '').strip()
  if not jid or not title:continue
  if 'india' not in loc.lower():loc=f"{loc}, India" if loc else 'India'
  out.append({"id":jid,"title":title,"location":loc,"posting_date":(x.get('PostedDate') or '')[:10],"application_url":f"{_JOB}/{jid}"})
 return out
def fetch_job_description(application_url,timeout=20):
 jid=application_url.rstrip('/').split('/')[-1];j=_get(_DETAIL,{"onlyData":"true","expand":"all","finder":f'ById;Id="{jid}",siteNumber={_SITE}'},timeout).json();items=j.get('items') or []
 if not items:return '', ''
 x=items[0];return _text(' '.join(x.get(k) or '' for k in ('ExternalDescriptionStr','ExternalResponsibilitiesStr','ExternalQualificationsStr'))),(x.get('ExternalPostedStartDate') or '')[:10]
