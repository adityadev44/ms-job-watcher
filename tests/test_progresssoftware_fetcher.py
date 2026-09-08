from unittest.mock import Mock,patch
from src import progresssoftware_fetcher as fetcher

def test_listing_filters_india_and_deduplicates():
 html='''<div item-id="1"><a href="https://www.progress.com/company/careers/open-positions/a-1">Engineer</a><div>Hyderabad, India</div></div><div item-id="2"><a href="https://www.progress.com/company/careers/open-positions/b-2">Engineer</a><div>Sofia, Bulgaria</div></div>'''
 r=Mock(status_code=200,text=html);r.raise_for_status.return_value=None
 fetcher._cache=None
 with patch.object(fetcher.requests,"get",return_value=r):
  jobs=fetcher.fetch_jobs("ignored","India")
 assert [j["id"] for j in jobs]==["1"]

def test_description_uses_job_summary():
 r=Mock(status_code=200,text="<main><div><h2>Job Summary</h2><p>C# and .NET</p></div></main>");r.raise_for_status.return_value=None
 with patch.object(fetcher.requests,"get",return_value=r):
  text,date=fetcher.fetch_job_description("https://example/job")
 assert "C# and .NET" in text and date==""

