"""Voya India WordPress jobs: official anonymous AJAX table and HTML detail.

The page's own Javascript submits fetch_job_listings without authentication.
Full requisition IDs and locations are exposed, but posting dates are absent.
"""
from __future__ import annotations
import re
import time
import requests
from bs4 import BeautifulSoup

_AJAX = 'https://www.voyaindia.com/wp-admin/admin-ajax.php'
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
            return BeautifulSoup(response.text, 'html.parser')
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f'Voya request failed: {exc}') from exc
            time.sleep(2 ** attempt)

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by='date', timeout=20):
    global _jobs, _list_error
    if _list_error:
        raise _list_error
    if _jobs is None:
        try:
            soup = _request('POST', _AJAX, timeout, data={'action': 'fetch_job_listings', 'search': '', 'location': ''})
        except RateLimitError as exc:
            _list_error = exc
            raise
        collected = {}
        for row in soup.select('table.jobtable tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) < 3:
                continue
            anchor = cells[0].select_one('a[href]')
            if not anchor:
                continue
            ident, title, loc = [c.get_text(' ', strip=True) for c in cells[:3]]
            if not re.search(r'\bindia\b', loc, re.I):
                continue
            collected[ident] = {'id': ident, 'title': title, 'location': loc, 'posting_date': '', 'application_url': anchor['href']}
        _jobs = list(collected.values())
    return _jobs[start:start + num]

def fetch_job_description(application_url, timeout=20):
    if application_url not in _descriptions:
        soup = _request('GET', application_url, timeout)
        node = soup.select_one('.jobdetails')
        _descriptions[application_url] = (node.get_text(' ', strip=True) if node else '', '')
    return _descriptions[application_url]
