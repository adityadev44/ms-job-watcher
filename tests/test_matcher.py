"""
Tests for src/matcher.py.

All tests use fabricated or saved sample data — no live API calls.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from matcher import (
    fetch_job_description,
    find_matching_jobs,
    is_india_job,
    matches_skills,
    passes_exclude_check,
    passes_level_check,
    _normalize_numerals,
    _strip_html,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SKILLS      = [".NET", "C#", "ASP.NET", "SQL Server", "Angular",
               "TypeScript", "Azure", "full stack", "backend"]
LEVEL_KW    = [
    "Software Engineer II",   # also matches "Software Engineer 2" via normalization
    "SWE II",                 # also matches "SWE 2"
    "SDE II",                 # also matches "SDE 2"
    "SDE2",
    "Senior Software Engineer",
    "Sr Software Engineer",
    "SSE",
]
EXCLUDE     = ["intern", "internship", "new grad", "principal", "director", "data scientist"]

# A job that should pass every filter
GOOD_JOB = {
    "id":               "200099001",
    "title":            "Senior Software Engineer",
    "location":         "India, Telangana, Hyderabad",
    "posting_date":     "2026-06-01",
    "application_url":  "https://apply.careers.microsoft.com/careers/job/1970393556999001?domain=microsoft.com",
}
GOOD_DESCRIPTION = (
    "We are looking for a Senior Software Engineer to join our Azure team. "
    "You will work with C#, ASP.NET, and SQL Server to build scalable backend services."
)

# A job that should be rejected because the title contains 'intern'
INTERN_JOB = {
    "id":               "200099002",
    "title":            "Software Engineer Intern",
    "location":         "India, Karnataka, Bangalore",
    "posting_date":     "2026-06-01",
    "application_url":  "https://apply.careers.microsoft.com/careers/job/1970393556999002?domain=microsoft.com",
}

# A job whose title passes all filters but whose description never mentions our stack
OUT_OF_STACK_JOB = {
    "id":               "200099003",
    "title":            "Senior Software Engineer",
    "location":         "India, Karnataka, Bangalore",
    "posting_date":     "2026-06-01",
    "application_url":  "https://apply.careers.microsoft.com/careers/job/1970393556999003?domain=microsoft.com",
}
OUT_OF_STACK_DESCRIPTION = (
    "Join our machine-learning platform team. You will use Python, Java, "
    "TensorFlow, and Spark to build scalable data pipelines. Strong ML background preferred."
)


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------

def test_strip_html_removes_tags():
    result = _strip_html("<b>Overview</b><br><div><p>Some text</p></div>")
    assert "<" not in result
    assert "Overview" in result
    assert "Some text" in result


def test_strip_html_decodes_entities():
    result = _strip_html("C&#35; &amp; ASP.NET")
    assert "&amp;" not in result
    assert "ASP.NET" in result


# ---------------------------------------------------------------------------
# Filter predicates — unit tests
# ---------------------------------------------------------------------------

def test_is_india_job_true():
    assert is_india_job(GOOD_JOB) is True


def test_is_india_job_false():
    assert is_india_job({**GOOD_JOB, "location": "United States, Washington, Redmond"}) is False


def test_passes_level_check_true():
    assert passes_level_check(GOOD_JOB, LEVEL_KW) is True


def test_passes_level_check_false_principal():
    assert passes_level_check({**GOOD_JOB, "title": "Principal Software Engineer"}, LEVEL_KW) is False


def test_passes_level_check_case_insensitive():
    assert passes_level_check({**GOOD_JOB, "title": "senior software engineer"}, LEVEL_KW) is True


def test_passes_level_check_arabic_numeral_matches_roman():
    """'Software Engineer 2' must match keyword 'Software Engineer II' via numeral normalization."""
    assert passes_level_check({**GOOD_JOB, "title": "Software Engineer 2"}, LEVEL_KW) is True


def test_passes_level_check_sde2_nospace():
    assert passes_level_check({**GOOD_JOB, "title": "SDE2 - Backend"}, LEVEL_KW) is True


def test_passes_level_check_sde_arabic():
    assert passes_level_check({**GOOD_JOB, "title": "SDE 2"}, LEVEL_KW) is True


def test_passes_level_check_sr_software_engineer():
    assert passes_level_check({**GOOD_JOB, "title": "Sr Software Engineer"}, LEVEL_KW) is True


def test_normalize_numerals_roman_to_arabic():
    assert _normalize_numerals("Software Engineer II") == "Software Engineer 2"
    assert _normalize_numerals("Software Engineer III") == "Software Engineer 3"
    assert _normalize_numerals("SWE II") == "SWE 2"


def test_normalize_numerals_arabic_unchanged():
    assert _normalize_numerals("Software Engineer 2") == "Software Engineer 2"


def test_normalize_numerals_case_insensitive():
    assert _normalize_numerals("software engineer ii") == "software engineer 2"


def test_passes_exclude_check_good_job():
    assert passes_exclude_check(GOOD_JOB, EXCLUDE) is True


def test_passes_exclude_check_rejects_intern():
    assert passes_exclude_check(INTERN_JOB, EXCLUDE) is False


def test_passes_exclude_check_rejects_director():
    assert passes_exclude_check({**GOOD_JOB, "title": "Director of Engineering"}, EXCLUDE) is False


def test_matches_skills_true():
    assert matches_skills(GOOD_DESCRIPTION, SKILLS) is True


def test_matches_skills_false():
    assert matches_skills(OUT_OF_STACK_DESCRIPTION, SKILLS) is False


def test_matches_skills_case_insensitive():
    assert matches_skills("experience with c# and asp.net required", SKILLS) is True


# ---------------------------------------------------------------------------
# fetch_job_description — monkeypatched (no live API)
# ---------------------------------------------------------------------------

SAMPLE_DETAIL_FILE = Path(__file__).parent / "sample_job_detail.json"


def test_fetch_job_description_returns_plain_text(monkeypatch):
    with SAMPLE_DETAIL_FILE.open(encoding="utf-8") as f:
        detail_data = json.load(f)

    class _FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return detail_data

    monkeypatch.setattr("matcher.requests.get", lambda *a, **kw: _FakeResponse())

    text = fetch_job_description(GOOD_JOB["application_url"])
    assert isinstance(text, str)
    assert len(text) > 50
    assert "<" not in text, "HTML tags should be stripped"


# ---------------------------------------------------------------------------
# find_matching_jobs — end-to-end with all I/O monkeypatched
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def _make_fake_fetch_jobs(*jobs):
    """Return a drop-in replacement for fetch_jobs that always returns *jobs*."""
    def _fake(keyword, location, *, num=10, start=0, timeout=20):
        return list(jobs)
    return _fake


def test_find_matching_jobs_keeps_good_role(monkeypatch):
    monkeypatch.setattr("matcher.fetch_jobs", _make_fake_fetch_jobs(GOOD_JOB))
    monkeypatch.setattr("matcher.fetch_job_description", lambda *a, **kw: GOOD_DESCRIPTION)

    total, matched = find_matching_jobs(CONFIG_PATH)
    assert any(j["title"] == "Senior Software Engineer" for j in matched), (
        "A good Senior Software Engineer with .NET/C# should be in matched"
    )


def test_find_matching_jobs_rejects_intern(monkeypatch):
    monkeypatch.setattr("matcher.fetch_jobs", _make_fake_fetch_jobs(INTERN_JOB))
    monkeypatch.setattr("matcher.fetch_job_description", lambda *a, **kw: GOOD_DESCRIPTION)

    _, matched = find_matching_jobs(CONFIG_PATH)
    assert all(j["id"] != INTERN_JOB["id"] for j in matched), (
        "Intern role must be excluded even if description matches the stack"
    )


def test_find_matching_jobs_rejects_out_of_stack(monkeypatch):
    monkeypatch.setattr("matcher.fetch_jobs", _make_fake_fetch_jobs(OUT_OF_STACK_JOB))
    monkeypatch.setattr("matcher.fetch_job_description",
                        lambda *a, **kw: OUT_OF_STACK_DESCRIPTION)

    _, matched = find_matching_jobs(CONFIG_PATH)
    assert all(j["id"] != OUT_OF_STACK_JOB["id"] for j in matched), (
        "A Python/Java-only role must be excluded when none of our skills appear"
    )


def test_find_matching_jobs_deduplicates(monkeypatch):
    """Same job returned by two different keyword searches must only appear once."""
    monkeypatch.setattr("matcher.fetch_jobs", _make_fake_fetch_jobs(GOOD_JOB))
    monkeypatch.setattr("matcher.fetch_job_description", lambda *a, **kw: GOOD_DESCRIPTION)

    total, matched = find_matching_jobs(CONFIG_PATH)
    ids = [j["id"] for j in matched]
    assert len(ids) == len(set(ids)), "Duplicate job IDs found in matched results"
