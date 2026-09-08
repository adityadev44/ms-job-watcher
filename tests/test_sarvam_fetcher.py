from pathlib import Path
import sys
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import sarvam_fetcher as f

def test_fetches_and_caches_inline_description(monkeypatch):
    f._CACHE = None; f._DESCRIPTIONS.clear()
    response = Mock(status_code=200); response.raise_for_status = Mock()
    response.json.return_value = {"jobs": [{"id":"abc", "title":"Agent Engineer", "location":"Bengaluru", "publishedAt":"2026-09-01T00:00:00Z", "isListed":True, "jobUrl":"https://jobs.ashbyhq.com/sarvam/abc", "descriptionPlain":"Build retrieval augmented generation systems."}]}
    get = Mock(return_value=response); monkeypatch.setattr(f.requests, "get", get)
    job = f.fetch_jobs("engineer", "India")[0]
    assert job["location"] == "Bengaluru, India"
    assert job["posting_date"] == "2026-09-01"
    assert f.fetch_job_description(job["application_url"])[0].startswith("Build retrieval")
    assert get.call_count == 1
