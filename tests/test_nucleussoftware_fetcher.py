from pathlib import Path
import sys
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent.parent/'src'))
import nucleussoftware_fetcher as f
def test_zoho_india_inline(monkeypatch):
 f._CACHE=None;f._DESC.clear();r=Mock(status_code=200);r.raise_for_status=Mock();r.json.return_value={'data':[{'id':'1','Posting_Title':'Software Engineer','City':'Noida','State':'Uttar Pradesh','Country':'India','Date_Opened':'18/08/2026','Publish':True,'$url':'https://job/1','Job_Description':'Build ASP.NET systems.'},{'id':'2','Posting_Title':'Engineer','Country':'USA','Publish':True}]};get=Mock(return_value=r);monkeypatch.setattr(f.requests,'get',get)
 j=f.fetch_jobs('engineer','India')[0];assert j['location']=='Noida, Uttar Pradesh, India';assert j['posting_date']=='2026-08-18';assert f.fetch_job_description('https://job/1')[0]=='Build ASP.NET systems.';assert get.call_count==1
