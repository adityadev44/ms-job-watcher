from pathlib import Path
import sys
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import observeai_fetcher as f

def test_filters_global_board_and_caches_description(monkeypatch):
    f._CACHE = None; f._DESCRIPTIONS.clear()
    response = Mock(status_code=200); response.raise_for_status = Mock()
    response.json.return_value = {"jobs": [
      {"id":1,"title":"AI Agent Engineer","location":{"name":"Bengaluru"},"first_published":"2026-04-15T00:00:00Z","absolute_url":"https://job/1","content":"<p>LangChain systems</p>"},
      {"id":2,"title":"Engineer","location":{"name":"California"},"absolute_url":"https://job/2","content":"x"}]}
    monkeypatch.setattr(f.requests, "get", Mock(return_value=response))
    jobs = f.fetch_jobs("engineer", "India")
    assert [j["id"] for j in jobs] == ["1"]
    assert jobs[0]["location"] == "Bengaluru, India"
    assert f.fetch_job_description("https://job/1") == ("LangChain systems", "2026-04-15")
