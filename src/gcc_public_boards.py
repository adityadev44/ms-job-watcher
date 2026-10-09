"""Verified public ATS adapters for the lower-priority GCC onboarding wave.

Each instance owns its cache and sticky errors. Failed/partial inventories never
become a successful zero-job result. Location and role filtering remain shared.
"""
import html
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from deferred_source_http import HEADERS, RateLimitError, get


def text(raw):
    soup = BeautifulSoup(html.unescape(html.unescape(raw or "")), "html.parser")
    # Clearly separated Greenhouse employer marketing is not role requirements.
    for node in soup.select('.content-intro,.content-conclusion'):
        node.decompose()
    return soup.get_text(" ", strip=True)


def india_location(location):
    if re.search(r"\bindia\b", location, re.I):
        return location
    if re.search(r"\b(?:bangalore|bengaluru|hyderabad|gurugram|gurgaon|noida|mumbai|pune|chennai)\b", location, re.I):
        return location + ", India"
    return ""


def post(url, body, timeout):
    for attempt in range(3):
        try:
            r = requests.post(url, headers=HEADERS, json=body, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RateLimitError(f"Public ATS request failed: {url}: {exc}") from exc
            time.sleep(2 ** attempt)


class Board:
    def __init__(self):
        self.jobs = None
        self.error = None
        self.descriptions = {}

    def fetch_jobs(self, keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
        if self.error:
            raise self.error
        if self.jobs is None:
            try:
                self.jobs = self.load(timeout)
            except (RateLimitError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.error = RateLimitError(f"{type(self).__name__} inventory failed: {exc}")
                raise self.error from exc
        return self.jobs[start:start + num]

    def fetch_job_description(self, application_url, timeout=20):
        if application_url not in self.descriptions:
            raise RateLimitError("Job description not in verified inventory")
        return self.descriptions[application_url]

    def entry(self, job_id, title, location, url, description, date):
        if not job_id or not title or not location or not url or not description:
            raise RateLimitError("Missing required job identity, location or description")
        self.descriptions[url] = (description, date)
        return {"id": str(job_id), "title": title.strip(), "location": location,
                "application_url": url, "posting_date": date}


class Greenhouse(Board):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def load(self, timeout):
        data = get(f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs?content=true", timeout).json()
        if not isinstance(data.get("jobs"), list) or data.get("meta", {}).get("total") != len(data["jobs"]):
            raise RateLimitError("Incomplete Greenhouse inventory")
        jobs = []
        for j in data['jobs']:
            location = india_location((j.get('location') or {}).get('name') or '')
            if not location:
                continue
            description = text(j.get('content'))
            # Litmos labels roles 'India' but some JDs explicitly identify Pune.
            # Preserve that explicit evidence, without assuming all India is Pune.
            if self.token == 'litmos' and re.search(r'\bpune\b', description, re.I):
                location += ', Pune'
            jobs.append(self.entry(j.get('id'), j.get('title'), location, j.get('absolute_url'),
                                   description, str(j.get('first_published') or '')[:10]))
        return jobs


class Lever(Board):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def load(self, timeout):
        data = get(f'https://api.lever.co/v0/postings/{self.token}?mode=json', timeout).json()
        if not isinstance(data, list):
            raise RateLimitError('Invalid Lever inventory')
        jobs = []
        for j in data:
            categories = j.get('categories') or {}
            locations = categories.get('allLocations') or [categories.get('location') or '']
            location = '; '.join(filter(None, (india_location(x) for x in locations)))
            if not location:
                continue
            description = text(j.get('description') or j.get('descriptionPlain'))
            description += ' ' + ' '.join(text(x.get('text')) + ' ' + text(x.get('content')) for x in j.get('lists') or [])
            description += ' ' + text(j.get('additional') or j.get('additionalPlain'))
            created = j.get('createdAt')
            date = datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat() if created else ''
            jobs.append(self.entry(j.get('id'), j.get('text'), location, j.get('hostedUrl'), description.strip(), date))
        return jobs


def country_facets(facets):
    for facet in facets:
        values = facet.get('values') or []
        for value in values:
            if value.get('descriptor') == 'India' and value.get('id'):
                yield facet['facetParameter'], value['id']
        yield from country_facets([v for v in values if 'facetParameter' in v])


class Workday(Board):
    def __init__(self, host, tenant, site, employer=None):
        super().__init__()
        self.base = 'https://' + host
        self.site = site
        self.api = f'{self.base}/wday/cxs/{tenant}/{site}'
        self.employer = employer

    def load(self, timeout):
        body = {'appliedFacets': {}, 'limit': 20, 'offset': 0, 'searchText': ''}
        first = post(self.api + '/jobs', body, timeout)
        if 'total' not in first or not isinstance(first.get('jobPostings'), list):
            raise RateLimitError('Invalid Workday inventory')
        facets = list(country_facets(first.get('facets') or []))
        if facets:
            body['appliedFacets'] = {key: [value] for key, value in facets}
            first = post(self.api + '/jobs', body, timeout)
        jobs, seen = [], set()
        page = first
        while True:
            rows = page['jobPostings']
            total = int(page['total'])
            if not rows:
                if body['offset'] < total:
                    raise RateLimitError('Workday stopped before total')
                break
            paths = {j['externalPath'] for j in rows}
            if not paths.difference(seen):
                raise RateLimitError('Workday repeated page')
            seen.update(paths)
            for j in rows:
                loc = j.get('locationsText') or ''
                ambiguous = not loc or re.fullmatch(r'\d+ Locations?', loc)
                if not facets and not india_location(loc) and not ambiguous:
                    continue
                detail = get(self.api + j['externalPath'], timeout).json()['jobPostingInfo']
                all_locations = [detail.get('location') or ''] + (detail.get('additionalLocations') or [])
                if not all_locations[0]:
                    all_locations[0] = loc
                location = '; '.join(filter(None, (india_location(x) for x in all_locations)))
                if not location:
                    if facets:
                        raise RateLimitError('India-faceted job missing usable city/country detail')
                    continue
                description = text(detail.get('jobDescription'))
                if self.employer:
                    # Client/product identity must be in role content, not generic
                    # parent-group boilerplate naming a portfolio of businesses.
                    role = re.split(r'\bAbout Us\s*:', description, maxsplit=1, flags=re.I)[0]
                    company = str(detail.get('company') or detail.get('companyName') or '')
                    if not re.search(r'\b' + re.escape(self.employer) + r'\b', company + ' ' + role, re.I):
                        continue
                url = self.base + '/' + self.site + j['externalPath']
                jobs.append(self.entry(detail.get('jobReqId'), detail.get('title') or j.get('title'), location,
                                       url, description, str(detail.get('startDate') or '')[:10]))
            body['offset'] += len(rows)
            if body['offset'] >= total:
                break
            page = post(self.api + '/jobs', body, timeout)
        return jobs


class Phenom(Board):
    BASE = 'https://jobs.kuehne-nagel.com'

    def load(self, timeout):
        jobs, seen = [], set()
        offset = 0
        while True:
            body = {'lang': 'en_global', 'deviceType': 'desktop', 'country': 'global', 'pageName': 'search-results',
                    'ddoKey': 'refineSearch', 'from': offset, 'jobs': True, 'counts': True, 'size': 10,
                    'refNum': 'KUNAGLOBAL', 'selected_fields': {'country': ['India']}}
            page = post(self.BASE + '/widgets', body, timeout)['refineSearch']
            if page.get('status') != 200:
                raise RateLimitError('Phenom search failed')
            rows = page['data']['jobs']
            total = int(page['totalHits'])
            if not rows:
                if offset < total:
                    raise RateLimitError('Phenom stopped before total')
                break
            ids = {j['jobId'] for j in rows}
            if not ids.difference(seen):
                raise RateLimitError('Phenom repeated page')
            seen.update(ids)
            for j in rows:
                if j.get('country') != 'India':
                    raise RateLimitError('Phenom country filter leaked foreign result')
                url = self.BASE + '/global/en/job/' + quote(j['jobSeqNo'], safe='') + '/' + quote(j['title'].replace(' ', '-'), safe='-')
                jobs.append({'id': str(j['jobId']), 'title': j['title'], 'location': j['cityStateCountry'],
                             'posting_date': str(j.get('postedDate') or '')[:10], 'application_url': url})
            offset += len(rows)
            if offset >= total:
                break
        return jobs

    def fetch_job_description(self, application_url, timeout=20):
        if not application_url.startswith(self.BASE + '/global/en/job/'):
            raise RateLimitError('Invalid Phenom job URL')
        if application_url not in self.descriptions:
            page = get(application_url, timeout).text
            marker = 'phApp.ddo ='
            if marker not in page:
                raise RateLimitError('Phenom job data absent')
            data = json.JSONDecoder().raw_decode(page.split(marker, 1)[1].lstrip())[0]
            job = data['jobDetail']['data']['job']
            description = text(job.get('description'))
            if not description:
                raise RateLimitError('Phenom description missing')
            self.descriptions[application_url] = (description, str(job.get('postedDate') or '')[:10])
        return self.descriptions[application_url]
