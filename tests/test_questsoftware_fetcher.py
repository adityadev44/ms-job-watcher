from unittest.mock import Mock,patch
from src import questsoftware_fetcher as fetcher

def test_listing_keeps_quest_india_rows():
 html='''<div class="iCIMS_JobsTable"><div class="row"><a href="https://careers-quest.icims.com/jobs/1/test/job?in_iframe=1">Title Software Engineer</a><p>Job ID 2026-1 Location APJ-IN-TG-Hyderabad Category Engineering Position Type Regular</p></div><div class="row"><a href="/jobs/2/x/job">Title Engineer</a><p>Job ID 2026-2 Location US-UT Category Engineering Position Type Regular</p></div></div>'''
 r=Mock(status_code=200,text=html);r.raise_for_status.return_value=None
 fetcher._cache=None
 with patch.object(fetcher.requests,"get",return_value=r):
  jobs=fetcher.fetch_jobs("ignored","India")
 assert [j["id"] for j in jobs]==["2026-1"]

def test_description():
 r=Mock(status_code=200,text='<div class="iCIMS_JobContent"><p>C# .NET</p></div>');r.raise_for_status.return_value=None
 with patch.object(fetcher.requests,"get",return_value=r):
  assert fetcher.fetch_job_description("https://example/job")==("C# .NET","")

