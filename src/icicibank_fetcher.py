"""Fetches ICICI Bank India job listings from its own custom candidate portal.

ICICI Bank's marketing "careers" domain (icicicareers.com) is a CNAME/alias
for a bank-hosted, purpose-built React SPA at careers.icici.bank.in/
CareerApplicant/ -- NOT a third-party ATS vendor (no Workday/SuccessFactors/
RippleHire/Oracle CE branding anywhere; confirmed via the site's own webpack
bundle, which calls its API "CareerApplicantApi" and references an internal
partner build system "expresssolutions.in/TeamICICIApi"). This is a fully
in-house-built recruiting platform.

**API surface** (reverse-engineered from the SPA's webpack bundles --
confirmed live 2026-09-07):
  - POST https://careers.icici.bank.in/CareerApplicantApi/Career/Search/1
    with a JSON body `{"data": <encrypted payload>}` returns
    `{"Data": <encrypted array>}` on a hit or
    `{"ResponseMessage":"No Record Found","ResponseCode":103}` on a genuine
    zero-match search (confirmed: this is a real zero, not a broken
    request -- see below).
  - GET .../Career/getSingleJob/1/{jobId} returns the same encrypted-`Data`
    envelope with the full job detail record (job description under the
    `JD` key as raw HTML; a separate `f_description`/`MobileJd1`/
    `MobileJd2` key holds a giant base64+percent-encoded full HTML page
    with feather-icons/tailwind CDN boilerplate for the mobile app's
    in-app browser -- `JD` is the clean one to use).

**Encryption**: every request body and response `Data` field on this API is
AES-128-CBC/PKCS7, key literal `$k@m0u$0172@0r!k` (16 bytes, used directly
as the AES key -- no KDF/passphrase mode). Requests: generate a random
16-char alnum IV, `base64(AES-CBC-encrypt(JSON.stringify(payload)))` then
the raw 16-char IV string appended in plaintext (backslashes in the base64
swapped for `/`, mirroring the site's own `.replace(/\\/g,"/")`).
Responses: the trailing 16 chars of the string ARE the IV (plaintext,
appended the same way); strip them off, base64-decode the remainder, and
AES-CBC-decrypt with the same key. This scheme was recovered directly from
the SPA's own `Object(o.g)`/`Object(o.f)` helpers in its main webpack
chunk and confirmed working end-to-end live (Career/Groups, empty-keyword
Career/Search/1, and Career/getSingleJob/1/{id} all round-tripped
correctly against the real API during onboarding).

**Search payload shape** (recovered from the "Jobs" listing page
component): `{userId, keyword, maingroup, experience, PageNo, limit,
isAllIndia}`. `PageNo` is a 0-indexed page multiplier -- the API computes
`OFFSET = PageNo * limit` server-side (confirmed live: `PageNo=1,
limit=20` against a 24-row pool returned only the last 4 rows, i.e. an
offset of 20, not 1). `isAllIndia=3` is the broadest bucket observed --
empirically `isAllIndia=0` ("domestic", 19/24 rows) plus `isAllIndia=1`
("programs" -- Probationary Officer/CA/apprenticeship postings, 5/24 rows)
exactly sum to `isAllIndia=3`'s 24/24, so 3 behaves like a bitmask/union of
both and is used unconditionally here to avoid under-counting.
`maingroup` filters by a numeric category id from `Career/Groups` (e.g.
"Digital & Technology" = value `91148`) but is left blank -- explicitly
scoping to that category returned zero rows for every `isAllIndia` value
tried, while leaving it blank still surfaces every category's postings
(confirmed: blank-maingroup keyword filtering genuinely narrows results,
e.g. keyword="manager" -> 19/24 rows, so the search is not simply ignoring
its filters).

**Keyword IS genuinely enforced server-side** (confirmed live
2026-09-07): keyword="manager" against the ~24-job full pool returns
19 matches; a nonsense token `zzznonsensequeryabc123` and every one of
this repo's ten standard tech keywords (software engineer, senior software
engineer, .net developer, c# developer, dot net, angular, ai engineer,
machine learning engineer, python developer, generative ai engineer) all
return a genuine, identical-shaped 0 (`ResponseCode: 103`, "No Record
Found") -- not an error, not a truncated/malformed request artifact.

**Signal-to-noise / current state of the board**: ICICI Bank's own public
candidate portal is small -- roughly two dozen total live postings
bank-wide at onboarding time, ALL of them retail-banking/ops/credit-risk/
relationship-manager/Chartered-Accountant/Probationary-Officer roles
(e.g. "Accounts Manager - Transaction Banking", "Credit Manager",
"Financial Crime Prevention Manager - SIU", "ICICI Bank Probationary
Officer Program"). There is a "Digital & Technology" category defined in
the site's own taxonomy (`Career/Groups`), but it currently has zero
postings routed through this portal -- ICICI's tech hiring evidently runs
through other channels (referrals/campus/LinkedIn/Naukri) rather than this
public career site, at least right now. This fetcher is fully functional
and will pick up tech postings the moment ICICI routes any through this
portal; the zero-match result for the standard keyword list reflects the
live board's actual current content, not a fetcher defect.

India detection: `hc_Location` (search results) / `Location` (job detail)
is free text -- observed values are bare city names ("Mumbai"), multi-city
lists ("Delhi, Hyderabad, Mumbai, Kolkata"), "Across India", or null/empty
for a handful of postings. None of the observed 24 live jobs are outside
India (ICICI Bank's retail/ops workforce is entirely domestic), but a
small overseas-branch defensive token list is kept anyway, mirroring the
HDFC/Axis Bank fetchers in this batch, since ICICI Bank does maintain a
handful of international branches.
"""
from __future__ import annotations

import base64
import html as html_mod
import json
import random
import re
import string
import time

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

_AES_KEY = b"$k@m0u$0172@0r!k"
_BASE = "https://careers.icici.bank.in/CareerApplicantApi"
_SEARCH_URL = f"{_BASE}/Career/Search/1"
_DETAIL_URL = f"{_BASE}/Career/getSingleJob/1"
_CANDIDATE_PAGE = "https://careers.icici.bank.in/CareerApplicant/Career/Home/"
_JOB_DETAIL_PAGE = "https://careers.icici.bank.in/CareerApplicant/Career/job-details"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": _CANDIDATE_PAGE,
    "Origin": "https://careers.icici.bank.in",
}

# ICICI Bank has a handful of international branches/offices; left
# unmodified (fails is_india_job() safely) rather than guessed at, same
# defensive spirit as the HDFC Bank/Axis Bank fetchers in this batch.
_NON_INDIA_TOKENS = ("dubai", "bahrain", "hong kong", "singapore", "london", "new york", "usa")

# Pagination-guard state: page-0 first job IDs, per this process run.
_FIRST_PAGE_IDS: set[str] | None = None


class RateLimitError(Exception):
    """Raised on 429 or persistent network/decrypt failure."""


def _rand_iv(n: int = 16) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(n))


def _encrypt_payload(obj: dict) -> str:
    """Mirror the SPA's `Object(o.g)` helper: AES-128-CBC + a plaintext,
    appended random IV."""
    iv_str = _rand_iv(16)
    cipher = AES.new(_AES_KEY, AES.MODE_CBC, iv_str.encode("utf-8"))
    ct = cipher.encrypt(pad(json.dumps(obj).encode("utf-8"), 16))
    b64 = base64.b64encode(ct).decode("utf-8")
    return (b64 + iv_str).replace("\\", "/")


def _decrypt_response(s: str):
    """Mirror the SPA's `Object(o.f)` helper: the trailing 16 chars are the
    plaintext IV; the rest is base64 AES-128-CBC ciphertext."""
    if not s or len(s) <= 16:
        raise RateLimitError("ICICI Bank: response too short to decrypt")
    iv_str, b64 = s[-16:], s[:-16]
    try:
        cipher = AES.new(_AES_KEY, AES.MODE_CBC, iv_str.encode("utf-8"))
        pt = unpad(cipher.decrypt(base64.b64decode(b64)), 16)
        return json.loads(pt.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any crypto/parse failure is fatal here
        raise RateLimitError(f"ICICI Bank: decrypt failed: {exc}") from exc


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_mod.unescape(text)
    return " ".join(text.split())


def _parse_date(raw: str) -> str:
    """Convert an ISO datetime ('2023-07-15T00:00:00.000Z') -> 'YYYY-MM-DD'."""
    if not raw or len(raw) < 10:
        return ""
    candidate = raw[:10]
    return candidate if re.match(r"^\d{4}-\d{2}-\d{2}$", candidate) else ""


def _normalize_location(raw: str) -> str:
    loc = (raw or "").strip()
    if not loc:
        return "India"
    low = loc.lower()
    if "india" in low:
        return "India" if low == "across india" else loc
    if any(tok in low for tok in _NON_INDIA_TOKENS):
        return loc
    return f"{loc}, India"


def _post_search(payload: dict, timeout: int):
    body = {"data": _encrypt_payload(payload)}
    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.post(_SEARCH_URL, json=body, headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("ICICI Bank: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ICICI Bank fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"ICICI Bank fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        raise RateLimitError("ICICI Bank: non-JSON response")

    if data.get("ResponseCode") == 103:
        return []  # genuine zero-match search, not an error
    if "Data" not in data or not data["Data"]:
        raise RateLimitError(f"ICICI Bank: unexpected response shape: {data}")

    decrypted = _decrypt_response(data["Data"])
    return decrypted if isinstance(decrypted, list) else []


def fetch_jobs(
    keyword: str,
    location: str,
    *,
    num: int = 20,
    start: int = 0,
    sort_by: str = "date",
    timeout: int = 20,
) -> list[dict]:
    """Return a page of ICICI Bank jobs matching *keyword*.

    The API paginates via a 0-indexed `PageNo` multiplied server-side by
    `limit` to compute the row offset (`OFFSET = PageNo * limit`) rather
    than a start/num row offset, so `PageNo` is derived assuming a constant
    page size across calls. India filtering happens in matcher.py via
    `is_india_job()`; this function only normalizes location text.
    `isAllIndia=3` is used unconditionally -- it is the union of every
    other bucket observed on this tenant (see module docstring).
    """
    global _FIRST_PAGE_IDS

    if not keyword:
        return []

    page_no = start // num if num else 0
    payload = {
        "userId": 1,
        "keyword": keyword,
        "maingroup": "",
        "experience": "",
        "PageNo": page_no,
        "limit": num,
        "isAllIndia": 3,
    }

    records = _post_search(payload, timeout)

    jobs: list[dict] = []
    for rec in records:
        job_id = rec.get("f_jobId")
        title = (rec.get("f_title") or "").strip()
        if not (job_id and title):
            continue
        jobs.append({
            "id": str(job_id),
            "title": title,
            "location": _normalize_location(rec.get("hc_Location") or ""),
            "posting_date": _parse_date(rec.get("f_createdDate") or ""),
            "application_url": f"{_JOB_DETAIL_PAGE}/{job_id}",
        })

    if start == 0:
        _FIRST_PAGE_IDS = {j["id"] for j in jobs}
    elif _FIRST_PAGE_IDS and jobs and {j["id"] for j in jobs} == _FIRST_PAGE_IDS:
        return []  # wraparound detected

    return jobs


def fetch_job_description(
    application_url: str,
    timeout: int = 20,
) -> tuple[str, str]:
    """Return (description, posting_date) for a single ICICI Bank job."""
    m = re.search(r"/job-details/(\d+)", application_url)
    job_id = m.group(1) if m else ""

    last_exc: Exception | None = None
    r = None
    for attempt in range(3):
        try:
            r = requests.get(f"{_DETAIL_URL}/{job_id}", headers=_HEADERS, timeout=timeout)
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RateLimitError("ICICI Bank description: 429 rate-limited")
            r.raise_for_status()
            break
        except RateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RateLimitError(f"ICICI Bank description fetch failed: {exc}") from exc

    if r is None:
        raise RateLimitError(f"ICICI Bank description fetch: no response — {last_exc}")

    try:
        data = r.json()
    except ValueError:
        raise RateLimitError("ICICI Bank description: non-JSON response")

    if data.get("ResponseCode") == 103 or "Data" not in data or not data["Data"]:
        return "", ""

    decrypted = _decrypt_response(data["Data"])
    rec = decrypted[0] if isinstance(decrypted, list) and decrypted else {}

    description = _strip_html(rec.get("JD") or "")
    posting_date = _parse_date(rec.get("f_createdDate") or "")
    return description, posting_date
