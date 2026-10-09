"""Nemetschek group's employer-managed Drupal vacancies, scoped to India.

The country dropdown exposes India as German label `Indien`, taxonomy 772.
The current country-filtered page is empty; global job-invite details are
verified SuccessFactors server-rendered descriptions. No placeholder jobs.
"""
from __future__ import annotations
import re
import time
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

_LIST = 'https://www.nemetschek.com/en/company/career'
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
                raise RateLimitError(f'Nemetschek request failed: {exc}') from exc
            time.sleep(2 ** attempt)

def fetch_jobs(keyword, location, *, num=20, start=0, sort_by='date', timeout=20):
    global _jobs, _list_error
    if _list_error:
        raise _list_error
    if _jobs is None:
        collected = {}
        url = _LIST + '?field_saf_country_target_id=772'
        visited = set()
        while url and url not in visited:
            visited.add(url)
            try:
                soup = _get(url, timeout)
            except RateLimitError as exc:
                _list_error = exc
                raise
            for anchor in soup.select('a.hoverelemjob[href]'):
                title = anchor.select_one('.content__grid-images_textcontent_content')
                loc = anchor.select_one('.text-left')
                if not title or not loc:
                    continue
                location_text = loc.get_text(' ', strip=True)
                if not re.search(r'\b(?:india|indien)\b', location_text, re.I):
                    continue
                app = anchor['href']
                ident = app.rstrip('/').rsplit('/', 1)[-1]
                collected[ident] = {'id': ident, 'title': title.get_text(' ', strip=True), 'location': location_text.replace('Indien', 'India'), 'posting_date': '', 'application_url': app}
            following = soup.select_one('li.pager__item--next a[href]')
            url = urljoin(_LIST, following['href']) if following else ''
        _jobs = list(collected.values())
    return _jobs[start:start + num]

def fetch_job_description(application_url, timeout=20):
    if application_url not in _descriptions:
        soup = _get(application_url, timeout)
        node = soup.select_one('.jobdescription') or soup.select_one('[itemprop="description"]')
        _descriptions[application_url] = (node.get_text(' ', strip=True) if node else '', '')
    return _descriptions[application_url]
