"""Newgen Software's OmniRecruit HTML-panel scraping and cache-fill contracts."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import newgen_fetcher as fetcher


_FORM_HTML = """
<html><body><form id="Form1" method="post">
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="vs" />
<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="vsg" />
<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="ev" />
<input type="text" name="dataListDiv$txtSearchKeyWord" value="" />
</form></body></html>
"""


def _panel(job_id, title, loc="Noida, India", posted="Posted On: 07 Aug 2026",
           desc="Full job description text here."):
    return f"""
    <div id="omnirecruit_referFadeEffects">
      <div class="col-12 carrerportal-font-sm headcount"><b>{title}</b></div>
      <div class="col-sm-6 col-md-8 mb-2"><i class="fa fa-map-marker"></i>&nbsp;{loc}</div>
      <div class="panel-body" style="white-space: pre-line;">{desc}
        <a onclick='return oIShareReferFriend.fbShowHideJobDescription(this)'>...Less</a>
      </div>
      <div class="col-lg-4 col-sm-6 col-md-4 mb-2">{posted}</div>
      <a onclick="return fRedirect(encodeURIComponent('{job_id}'));">APPLY</a>
    </div>
    """


def _page(panels_html):
    return "<html><body>" + "".join(panels_html) + "</body></html>"


@pytest.fixture(autouse=True)
def reset_cache():
    fetcher._job_cache.clear()
    fetcher._desc_cache.clear()
    fetcher._cache_filled = False
    yield
    fetcher._job_cache.clear()
    fetcher._desc_cache.clear()
    fetcher._cache_filled = False


def test_fetch_jobs_parses_panels_and_fills_cache_once(monkeypatch):
    get = Mock(return_value=Mock(status_code=200, text=_FORM_HTML, raise_for_status=lambda: None))
    post = Mock(return_value=Mock(
        status_code=200,
        text=_page([_panel("FY26-27-605", "Software Engineer/Senior Software Engineer", "Mumbai, India")]),
        raise_for_status=lambda: None,
    ))
    monkeypatch.setattr(fetcher.requests.Session, 'get', get)
    monkeypatch.setattr(fetcher.requests.Session, 'post', post)

    jobs = fetcher.fetch_jobs('engineer', 'India')
    assert len(jobs) == 1
    j = jobs[0]
    assert j['id'] == 'FY26-27-605'
    assert j['title'] == 'Software Engineer/Senior Software Engineer'
    assert j['location'] == 'Mumbai, India'
    assert j['posting_date'] == '2026-08-07'
    assert j['application_url'].endswith('JobExternalUsers=FY26-27-605')

    # Cache fill probes multiple search terms once; a second fetch_jobs call
    # must not re-trigger any network calls.
    call_count_after_first = post.call_count
    fetcher.fetch_jobs('developer', 'India')
    assert post.call_count == call_count_after_first


def test_fetch_job_description_served_from_cache(monkeypatch):
    get = Mock(return_value=Mock(status_code=200, text=_FORM_HTML, raise_for_status=lambda: None))
    post = Mock(return_value=Mock(
        status_code=200,
        text=_page([_panel("FY26-27-618", "Graphic Designer", "Noida, India",
                            desc="Design flyers and brochures for the marketing team.")]),
        raise_for_status=lambda: None,
    ))
    monkeypatch.setattr(fetcher.requests.Session, 'get', get)
    monkeypatch.setattr(fetcher.requests.Session, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    description, posting_date = fetcher.fetch_job_description(jobs[0]['application_url'])
    assert 'Design flyers and brochures' in description
    assert posting_date == '2026-08-07'


def test_jobs_missing_id_or_title_are_skipped(monkeypatch):
    no_id_panel = """
    <div id="omnirecruit_referFadeEffects">
      <div class="col-12 carrerportal-font-sm headcount"><b>No Apply Link</b></div>
      <div class="col-sm-6 col-md-8 mb-2"><i class="fa fa-map-marker"></i>&nbsp;Noida, India</div>
      <div class="panel-body" style="white-space: pre-line;">desc</div>
      <div class="col-lg-4 col-sm-6 col-md-4 mb-2">Posted On: 01 Aug 2026</div>
    </div>
    """
    good = _panel("FY26-27-1", "Real Job")
    get = Mock(return_value=Mock(status_code=200, text=_FORM_HTML, raise_for_status=lambda: None))
    post = Mock(return_value=Mock(
        status_code=200,
        text=_page([no_id_panel, good]),
        raise_for_status=lambda: None,
    ))
    monkeypatch.setattr(fetcher.requests.Session, 'get', get)
    monkeypatch.setattr(fetcher.requests.Session, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert len(jobs) == 1
    assert jobs[0]['id'] == 'FY26-27-1'


def test_cache_fill_survives_a_failed_probe(monkeypatch):
    get = Mock(return_value=Mock(status_code=200, text=_FORM_HTML, raise_for_status=lambda: None))
    good_resp = Mock(
        status_code=200,
        text=_page([_panel("FY26-27-1", "Real Job")]),
        raise_for_status=lambda: None,
    )
    # First probe term fails once then succeeds on retry (2 calls), every
    # other probe term succeeds on the first call (1 call each) -- total
    # calls = len(_PROBE_TERMS) + 1 for the one retried failure.
    post = Mock(side_effect=(
        [fetcher.requests.ConnectionError("blip")]
        + [good_resp] * len(fetcher._PROBE_TERMS)
    ))
    monkeypatch.setattr(fetcher.requests.Session, 'get', get)
    monkeypatch.setattr(fetcher.requests.Session, 'post', post)

    jobs = fetcher.fetch_jobs('', 'India')
    assert len(jobs) == 1
    assert jobs[0]['id'] == 'FY26-27-1'
