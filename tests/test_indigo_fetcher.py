"""IndiGo's SuccessFactors-backed career-job-list cache, location
normalization, and failure contracts (mocked at the ``_call_api`` seam so no
real Playwright browser is launched)."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
import indigo_fetcher as fetcher


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(fetcher, '_job_cache', [])
    monkeypatch.setattr(fetcher, '_desc_cache', {})
    monkeypatch.setattr(fetcher, '_cache_filled', False)
    monkeypatch.setattr(fetcher, '_cache_error', None)
    monkeypatch.setattr(fetcher, '_live_context', None)
    monkeypatch.setattr(fetcher, '_live_page', None)
    monkeypatch.setattr(fetcher, '_ensure_live_context', lambda timeout=30: None)
    monkeypatch.setattr(fetcher.time, 'sleep', lambda _: None)


def _job(job_id, title, location_names, board_dates, status='Open', desc='<p>Build stuff.</p>'):
    """Build one raw career-job-list entry matching the real SuccessFactors
    OData shape observed live."""
    return {
        'jobReqId': job_id,
        'status': {'results': [{'externalCode': status}]} if status else {'results': []},
        'jobReqLocale': {'results': [{
            'externalTitle': title,
            'externalJobDescription': desc,
        }]},
        'location_obj': {'results': [{'name': n} for n in location_names]},
        'jobReqPostings': {'results': [
            {'boardId': board, 'postStartDate': f'/Date({ts})/'}
            for board, ts in board_dates
        ]},
    }


def _page(jobs):
    return {'status': 200, 'message': 'Jobs list', 'result': jobs}


def test_fills_cache_and_slices(monkeypatch):
    jobs = [
        _job('1', 'Lead AI Engineer', ['Gurgaon'], [('_internal', 1000), ('_external', 2000)]),
        _job('2', 'A320 First Officer', ['Pan-India'], [('_external', 3000)]),
        _job('3', 'Manager - CX', ['Chandigarh'], [('_private_external', 500), ('_external', 4000)]),
    ]
    call_api = Mock(return_value=_page(jobs))
    monkeypatch.setattr(fetcher, '_call_api', call_api)

    result = fetcher.fetch_jobs('AI engineer', 'India', num=2, start=0)
    assert len(result) == 2
    assert result[0]['id'] == '1'
    assert result[0]['title'] == 'Lead AI Engineer'
    assert result[0]['location'] == 'Gurgaon, India'
    assert result[0]['application_url'] == (
        'https://www.goindigo.in/careers/job-details/Lead-AI-Engineer/1.html')

    # Keyword is ignored server-side -- a second, different-keyword call
    # reuses the cache and does not re-invoke the API.
    all_jobs = fetcher.fetch_jobs('completely different keyword', '', num=100, start=0)
    assert len(all_jobs) == 3
    assert call_api.call_count == 1


def test_pan_india_left_unchanged_and_unknown_city_passthrough(monkeypatch):
    jobs = [
        _job('1', 'X', ['Pan-India'], [('_external', 1000)]),
        _job('2', 'Y', ['Timbuktu'], [('_external', 1000)]),
        _job('3', 'Z', ['Mumbai', 'Pune'], [('_external', 1000)]),
    ]
    monkeypatch.setattr(fetcher, '_call_api', lambda *a, **k: _page(jobs))
    result = fetcher.fetch_jobs('', '', num=100)
    by_id = {j['id']: j['location'] for j in result}
    assert by_id['1'] == 'Pan-India'
    assert by_id['2'] == 'Timbuktu'
    assert by_id['3'] == 'Mumbai, India; Pune, India'


def test_posting_date_prefers_external_board_latest(monkeypatch):
    jobs = [
        _job('1', 'X', ['Gurgaon'], [
            ('_internal', 5_000_000), ('_external', 1_000_000), ('_external', 2_000_000),
        ]),
    ]
    monkeypatch.setattr(fetcher, '_call_api', lambda *a, **k: _page(jobs))
    result = fetcher.fetch_jobs('', '', num=100)
    # max of the two _external timestamps (2_000_000 ms), NOT the later
    # _internal-only timestamp.
    assert result[0]['posting_date'] == fetcher._epoch_ms_to_date(2_000_000)


def test_non_open_status_is_dropped(monkeypatch):
    jobs = [
        _job('1', 'Open Job', ['Gurgaon'], [('_external', 1000)], status='Open'),
        _job('2', 'Closed Job', ['Gurgaon'], [('_external', 1000)], status='Closed'),
    ]
    monkeypatch.setattr(fetcher, '_call_api', lambda *a, **k: _page(jobs))
    result = fetcher.fetch_jobs('', '', num=100)
    assert [j['id'] for j in result] == ['1']


def test_description_served_from_cache(monkeypatch):
    jobs = [_job('42', 'Lead AI Engineer', ['Gurgaon'], [('_external', 1000)],
                  desc='<p>Build <b>LLM</b> systems.</p>')]
    monkeypatch.setattr(fetcher, '_call_api', lambda *a, **k: _page(jobs))
    fetcher.fetch_jobs('', '', num=100)
    desc, date = fetcher.fetch_job_description(
        'https://www.goindigo.in/careers/job-details/garbage-slug/42.html')
    assert desc == 'Build LLM systems.'
    assert date == fetcher._epoch_ms_to_date(1000)


def test_missing_result_key_never_becomes_silent_empty_success(monkeypatch):
    from unittest.mock import Mock
    call_api = Mock(return_value={'status': 200})
    monkeypatch.setattr(fetcher, '_call_api', call_api)
    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '', num=100)
    # latched _cache_error guard: the second call must NOT re-invoke the
    # API, and must NOT silently return an empty list as if it succeeded.
    assert call_api.call_count == 1


def test_call_api_failure_propagates_as_ratelimiterror(monkeypatch):
    from unittest.mock import Mock
    boom = Mock(side_effect=RuntimeError('network exploded'))
    monkeypatch.setattr(fetcher, '_call_api', boom)
    for _ in range(2):
        with pytest.raises(fetcher.RateLimitError):
            fetcher.fetch_jobs('', '', num=100)
    assert boom.call_count == 1


@pytest.mark.parametrize('title,expected', [
    ('Lead AI Engineer', 'Lead-AI-Engineer'),
    ('A320 First Officer / Senior First Officer (Type Rated)',
     'A320-First-Officer-Senior-First-Officer-(Type-Rated)'),
    ('Assisting Engineer (RT License)', 'Assisting-Engineer-(RT-License)'),
])
def test_slugify(title, expected):
    assert fetcher._slugify(title) == expected
