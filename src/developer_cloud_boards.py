"""GitHub's verified public iCIMS inventory, without delivery or state writes."""
from gcc_public_boards import Board, RateLimitError, india_location, text
from deferred_source_http import get


class GitHub(Board):
    BASE = 'https://www.github.careers'

    def load(self, timeout):
        jobs, seen = [], set()
        page = 1
        expected_total = None
        while True:
            data = get(self.BASE + '/api/jobs', timeout,
                       params={'limit': 100, 'page': page}).json()
            rows = data.get('jobs')
            total = data.get('totalCount')
            if not isinstance(rows, list) or not isinstance(total, int):
                raise RateLimitError('Invalid GitHub inventory')
            if total < 0 or (expected_total is not None and total != expected_total):
                raise RateLimitError('GitHub inventory total changed during pagination')
            expected_total = total
            if not rows:
                if len(seen) < total:
                    raise RateLimitError('GitHub pagination stopped before total')
                break
            for row in rows:
                item = row['data']
                jid = item.get('req_id')
                if not jid or str(jid) in seen:
                    raise RateLimitError('GitHub missing or repeated job ID')
                seen.add(str(jid))
                if item.get('client_code') != 'githubinc':
                    raise RateLimitError('GitHub employer identity changed')
                # Exact structured country is authoritative. Remote UK/US and
                # Indiana are never assumed to permit employment in India.
                if item.get('country') != 'India' and item.get('country_code') != 'IN':
                    continue
                location = item.get('location_name') or ''
                if not india_location(location):
                    location = ', '.join(filter(None, [item.get('city'), item.get('state'), 'India']))
                else:
                    location = india_location(location)
                description = ' '.join(text(item.get(field)) for field in
                                       ['description', 'qualifications', 'responsibilities'])
                url = self.BASE + '/jobs/' + str(jid) + '?lang=en-us'
                jobs.append(self.entry(jid, item.get('title'), location, url,
                                       description.strip(), str(item.get('posted_date') or '')[:10]))
            if len(seen) >= total:
                if len(seen) != total:
                    raise RateLimitError('GitHub count differs from inventory')
                break
            page += 1
        return jobs
