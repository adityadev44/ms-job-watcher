"""Employer-verified collaboration/product SaaS public hiring sources."""
import json
import html
import re

from bs4 import BeautifulSoup
from gcc_public_boards import Board, RateLimitError, get, india_location, text


def role_text(raw):
    """Exclude introductory employer marketing, never actual role requirements."""
    soup = BeautifulSoup(html.unescape(html.unescape(raw or '')), 'html.parser')
    headings = soup.find_all(re.compile(r'^h[1-6]$'))
    markers = {'about the role', 'about this role', 'what you’ll do', "what you'll do",
               'what you will do', 'the role', 'your role', 'what we’re looking for',
               "what we're looking for", 'what you bring'}
    for heading in headings:
        if heading.get_text(' ', strip=True).lower().rstrip(':') in markers:
            # Only remove nodes preceding the heading. Requirements following it
            # remain intact, including headings nested inside container elements.
            for node in list(heading.find_all_previous()):
                if heading not in node.descendants:
                    node.decompose()
            break
    return text(str(soup))


class Ashby(Board):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def load(self, timeout):
        data = get(f'https://api.ashbyhq.com/posting-api/job-board/{self.token}', timeout).json()
        if not isinstance(data.get('jobs'), list):
            raise RateLimitError('Invalid Ashby inventory')
        jobs, seen = [], set()
        for j in data['jobs']:
            if not j.get('id') or j['id'] in seen or not j.get('location'):
                raise RateLimitError('Invalid or duplicate Ashby identity/location')
            seen.add(j['id'])
            if j.get('isListed') is False:
                continue
            locations = [{'location': j['location'], 'address': j.get('address') or {}}]
            locations += j.get('secondaryLocations') or []
            eligible = []
            for loc in locations:
                label = loc.get('location') or ''
                address = (loc.get('address') or {}).get('postalAddress') or {}
                if address.get('addressCountry') and address['addressCountry'] not in ('India', 'IN'):
                    continue
                if address.get('addressCountry') in ('India', 'IN'):
                    city = address.get('addressLocality') or ''
                    if city and city.lower() not in label.lower():
                        label += ', ' + city
                    if not re.search(r'\bindia\b', label, re.I):
                        label += ', India'
                label = india_location(label)
                if label:
                    eligible.append(label)
            location = '; '.join(dict.fromkeys(eligible))
            if not location:
                continue
            url = j.get('jobUrl') or ''
            if not url.startswith(f'https://jobs.ashbyhq.com/{self.token}/'):
                raise RateLimitError('Unexpected Ashby application destination')
            description = role_text(j.get('descriptionHtml')) or j.get('descriptionPlain') or ''
            jobs.append(self.entry(j['id'], j.get('title'), location, url, description,
                                   str(j.get('publishedAt') or '')[:10]))
        return jobs


def next_data(url, timeout):
    soup = BeautifulSoup(get(url, timeout).text, 'html.parser')
    node = soup.select_one('script#__NEXT_DATA__')
    if not node:
        raise RateLimitError('Employer structured job data missing')
    return json.loads(node.get_text())['props']['pageProps']


class Miro(Board):
    BASE = 'https://miro.com/careers'

    def load(self, timeout):
        data = next_data(self.BASE + '/open-positions/', timeout)
        rows = data.get('jobs')
        if not isinstance(rows, list):
            raise RateLimitError('Miro inventory missing')
        jobs, seen = [], set()
        for j in rows:
            if not j.get('id') or j['id'] in seen or not j.get('location'):
                raise RateLimitError('Invalid or duplicate Miro row')
            seen.add(j['id'])
            labels = [j['location']] + [o.get('location') or '' for o in j.get('offices') or []]
            location = '; '.join(dict.fromkeys(filter(None, map(india_location, labels))))
            if not location:
                continue
            url = self.BASE + '/vacancy/' + str(j['id']) + '/'
            detail = next_data(url, timeout)
            if str(detail.get('slug')) != str(j['id']):
                raise RateLimitError('Miro detail identity mismatch')
            jobs.append(self.entry(j['id'], j['title'], location, url,
                                   role_text(detail.get('content')), ''))
        return jobs
