"""Serko employer careers pages, full server-rendered listings and descriptions.

Workable is the apply backend; the employer's own pages expose complete JDs.
Posting dates are not exposed. URL slug is the stable listing identifier.
"""
from __future__ import annotations
import re
import time
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

_BASE = 'https://www.serko.com'
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'}
_jobs = None
_list_error = None
_descriptions = {}

class RateLimitError(Exception):
    pass

def _get(url, timeout):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f'Serko request failed: {exc}') from exc
            time.sleep(2 ** attempt)

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by='date', timeout=20):
    global _jobs, _list_error
    if _list_error:
        raise _list_error
    if _jobs is None:
        try:
            soup = _get(_BASE + '/careers', timeout)
        except RateLimitError as exc:
            _list_error = exc
            raise
        collected = {}
        for anchor in soup.select('a.job_opening-wrapper[href]'):
            content = anchor.select_one('.job_opening-content')
            fields = content.find_all('div', recursive=False) if content else []
            if len(fields) < 2:
                continue
            title, loc = [x.get_text(' ', strip=True) for x in fields[:2]]
            if not re.search(r'\bindia\b', loc, re.I):
                continue
            url = urljoin(_BASE, anchor['href'])
            ident = url.rstrip('/').rsplit('/', 1)[-1]
            collected[ident] = {'id': ident, 'title': title, 'location': loc, 'posting_date': '', 'application_url': url}
        _jobs = list(collected.values())
    return _jobs[start:start + num]

def fetch_job_description(application_url, timeout=20):
    if application_url not in _descriptions:
        soup = _get(application_url, timeout)
        nodes = soup.select('.job_opening-content-wrapper .job_opening-info')
        _descriptions[application_url] = (' '.join(n.get_text(' ', strip=True) for n in nodes), '')
    return _descriptions[application_url]
