from pathlib import Path
import sys
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent.parent/'src'))
import dunbradstreet_fetcher as f
def test_india_only_and_inline(monkeypatch):
 f._CACHE=None;f._DESC.clear();r=Mock(status_code=200);r.raise_for_status=Mock();r.json.return_value=[{"id":"x","text":"AI Engineer","createdAt":0,"categories":{"location":"Hyderabad - India"},"hostedUrl":"u","descriptionPlain":"generative ai"},{"id":"y","text":"Engineer","categories":{"location":"Dublin - Ireland"}}];monkeypatch.setattr(f.requests,'get',Mock(return_value=r));j=f.fetch_jobs('', 'India');assert len(j)==1;assert f.fetch_job_description('u')[0]=='generative ai'
