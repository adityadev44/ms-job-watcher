"""Small public-source HTTP helper for the recovered GCC integrations."""
import time

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"}


class RateLimitError(Exception):
    pass


def get(url, timeout=20, **kwargs):
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if attempt == 2:
                raise RateLimitError(f"Public careers source failed: {url}: {exc}") from exc
            time.sleep(2 ** attempt)
