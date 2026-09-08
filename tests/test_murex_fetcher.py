from pathlib import Path
import sys
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent.parent/'src'))
import murex_fetcher as f
def test_mumbai_facet_and_parse(monkeypatch):
 r=Mock(status_code=200);r.raise_for_status=Mock();r.json.return_value={'jobPostings':[{'title':'Technical Integration Consultant','externalPath':'/job/x/Role_JR102168-1','bulletFields':['JR102168'],'postedOn':'Posted 2 Days Ago'}]};post=Mock(return_value=r);monkeypatch.setattr(f.requests,'post',post)
 j=f.fetch_jobs('engineer','India')[0];assert j['id']=='JR102168';assert j['location']=='Mumbai, India';assert post.call_args.kwargs['json']['appliedFacets']=={'locations':[f._MUMBAI]}
