"""Itineris India careers: server-rendered HiBob jobs on employer WordPress site.

The dedicated India page lists 16 Hyderabad positions. IDs are HiBob UUIDs;
there is no reliable posting date. Select the job content container only.
"""
from __future__ import annotations
import time
import requests
from bs4 import BeautifulSoup

_LIST_URL = 'https://itineris.net/india-careers/search-jobs/'
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15'}
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
                raise RateLimitError(f'Itineris request failed: {exc}') from exc
            time.sleep(2 ** attempt)

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by='date', timeout=20):
    global _jobs, _list_error
    if _list_error:
        raise _list_error
    if _jobs is None:
        try:
            soup = _get(_LIST_URL, timeout)
        except RateLimitError as exc:
            _list_error = exc
            raise
        collected = {}
        for anchor in soup.select('a[href]'):
            url = anchor['href']
            if '/india-careers/search-jobs/detail/' not in url:
                continue
            ident = url.rstrip('/').split('/')[-1]
            title = anchor.get_text(' ', strip=True)
            if ident and title:
                collected[ident] = {'id': ident, 'title': title, 'location': 'Hyderabad, India', 'posting_date': '', 'application_url': url}
        _jobs = list(collected.values())
    return _jobs[start:start + num]

def fetch_job_description(application_url, timeout=20):
    if application_url not in _descriptions:
        soup = _get(application_url, timeout)
        node = soup.select_one('.hibob-job-details-page-container')
        _descriptions[application_url] = (node.get_text(' ', strip=True) if node else '', '')
    return _descriptions[application_url]
