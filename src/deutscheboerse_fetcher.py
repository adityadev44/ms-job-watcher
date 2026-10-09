"""Deutsche Börse Group jobs via official job-shop Typesense search.

The public, tenant-scoped search key is refreshed from the site's Nuxt payload
on every process. No private token is stored. Descriptions are inline in
Typesense documents. This is distinct from Deutsche Bank's Beesite pipeline.
"""
from __future__ import annotations
import json
import re
import time
import requests
from bs4 import BeautifulSoup

_PAGE = 'https://careers.deutsche-boerse.com/search'
_API = 'https://api.my-job-shop.com/api/typesense/multi_search'
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'}
_jobs = None
_list_error = None
_descriptions = {}

class RateLimitError(Exception):
    pass

def _request(method, url, timeout, **kwargs):
    for attempt in range(3):
        try:
            response = requests.request(method, url, headers=_HEADERS, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f'Deutsche Börse request failed: {exc}') from exc
            time.sleep(2 ** attempt)

def _search_key(page):
    soup = BeautifulSoup(page, 'html.parser')
    node = soup.select_one('#__NUXT_DATA__')
    if not node:
        raise RateLimitError('Deutsche Börse missing Nuxt search configuration')
    values = json.loads(node.text)
    for value in values:
        if isinstance(value, dict):
            for key, reference in value.items():
                if key.startswith('typesenseApiKey-') and isinstance(reference, int):
                    return values[reference]
    raise RateLimitError('Deutsche Börse missing public search key')

def _fill(timeout):
    global _jobs, _list_error
    if _list_error:
        raise _list_error
    if _jobs is not None:
        return
    try:
        key = _search_key(_request('GET', _PAGE, timeout).text)
        collected = {}
        page = 1
        visited = set()
        while True:
            payload = {'searches': [{'collection': 'offers', 'q': '*', 'query_by': 'title,location', 'per_page': 250, 'page': page}]}
            result = _request('POST', _API, timeout, params={'x-typesense-api-key': key}, json=payload).json()['results'][0]
            if 'error' in result:
                raise RateLimitError(f'Deutsche Börse search error: {result["error"]}')
            hits = result.get('hits', [])
            fresh = [h['document'] for h in hits if h['document']['id'] not in visited]
            if not fresh:
                break
            for job in fresh:
                visited.add(job['id'])
                if not any(re.fullmatch(r'india', str(c), re.I) for c in job.get('country', [])):
                    continue
                ident = job.get('external_id') or job['id']
                locs = [str(loc) for loc in job.get('location', [])]
                # Country filtering is authoritative; preserve all location text.
                loc = ', '.join(locs) + ', India'
                url = job.get('url') or job.get('application_url')
                collected[ident] = {'id': ident, 'title': job.get('title', ''), 'location': loc, 'posting_date': '', 'application_url': url}
                body = ' '.join(job.get(k, '') or '' for k in ['introduction', 'description', 'expectation', 'offering', 'additional'])
                _descriptions[url] = (' '.join(BeautifulSoup(body, 'html.parser').get_text(' ').split()), '')
            if page * 250 >= result.get('found', 0):
                break
            page += 1
        _jobs = list(collected.values())
    except (RateLimitError, ValueError, KeyError, TypeError) as exc:
        _list_error = RateLimitError(f'Deutsche Börse fetch failed: {exc}')
        raise _list_error from exc

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by='date', timeout=20):
    _fill(timeout)
    return _jobs[start:start + num]

def fetch_job_description(application_url, timeout=20):
    _fill(timeout)
    return _descriptions.get(application_url, ('', ''))
