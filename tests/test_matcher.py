"""
Tests for src/matcher.py.

All tests use fabricated or saved sample data — no live API calls.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Speed guard: prevent time.sleep from slowing the suite
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("matcher.time.sleep", lambda *_: None)

from matcher import (
    fetch_job_description,
    find_matching_jobs,
    is_india_job,
    matches_skills,
    passes_exclude_check,
    passes_title_family_check,
    _normalize_text,
    _strip_html,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

TITLE_FAMILY = [
    "software engineer",
    "software development engineer",
    "SDE",
    "SWE",
    "full stack engineer",
    "backend engineer",
    "full-stack developer",
    "backend developer",
    ".NET developer",
    "application developer",
    "application engineer",
]

SKILLS = [
    ".NET", ".NET Core", ".NET Framework", "dotnet",
    "C#", "ASP.NET", "Web API", "REST",
    "SQL Server", "T-SQL", "SQL", "Azure",
    "Angular", "TypeScript", "JavaScript", "React",
    "Entity Framework",
]

EXCLUDE = [
    "intern", "internship", "trainee", "apprentice", "fresher",
    "graduate", "new grad", "university",
    "principal", "director", "vice president", "VP",
    "head of", "engineering manager", "manager",
    "mechanical", "electrical", "industrial", "hardware",
    "firmware", "embedded", "datacenter technician",
    "network engineer", "sales engineer", "solutions engineer",
    "customer engineer", "support engineer", "data scientist",
]

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
# _normalize_text
# ---------------------------------------------------------------------------

def test_normalize_text_lowercase():
    assert _normalize_text("Software Engineer") == "software engineer"


def test_normalize_text_roman_ii_to_arabic():
    assert _normalize_text("Software Engineer II") == "software engineer 2"
    assert _normalize_text("SWE II") == "swe 2"


def test_normalize_text_roman_iii_to_arabic():
    assert _normalize_text("Software Engineer III") == "software engineer 3"


def test_normalize_text_arabic_numeral_unchanged():
    assert _normalize_text("Software Engineer 2") == "software engineer 2"


def test_normalize_text_roman_case_insensitive():
    assert _normalize_text("software engineer ii") == "software engineer 2"


def test_normalize_text_dotnet_variants():
    assert _normalize_text(".NET developer") == "dotnet developer"
    assert _normalize_text("dot net developer") == "dotnet developer"
    assert _normalize_text("dotnet developer") == "dotnet developer"


def test_normalize_text_aspnet_kept_separate():
    # ASP.NET must not become "ASPdotnet" — it gets its own canonical form
    assert _normalize_text("ASP.NET") == "aspnet"
    assert _normalize_text("asp.net") == "aspnet"


def test_normalize_text_csharp_variants():
    assert _normalize_text("C#") == "csharp"
    assert _normalize_text("C-Sharp") == "csharp"
    assert _normalize_text("CSharp") == "csharp"


def test_normalize_text_hyphen_to_space():
    assert _normalize_text("full-stack developer") == "full stack developer"


# ---------------------------------------------------------------------------
# Filter predicates — unit tests
# ---------------------------------------------------------------------------

def test_is_india_job_true():
    assert is_india_job(GOOD_JOB) is True


def test_is_india_job_false():
    assert is_india_job({**GOOD_JOB, "location": "United States, Washington, Redmond"}) is False


# --- passes_title_family_check ---

def test_title_family_senior_software_engineer():
    assert passes_title_family_check(GOOD_JOB, TITLE_FAMILY) is True


def test_title_family_case_insensitive():
    assert passes_title_family_check({**GOOD_JOB, "title": "senior software engineer"}, TITLE_FAMILY) is True


def test_title_family_software_engineer_2_arabic():
    """'Software Engineer 2' must match via the 'software engineer' family term."""
    assert passes_title_family_check({**GOOD_JOB, "title": "Software Engineer 2"}, TITLE_FAMILY) is True


def test_title_family_software_development_engineer_ii():
    """'Software Development Engineer II' must match (Roman numeral normalised to Arabic)."""
    assert passes_title_family_check(
        {**GOOD_JOB, "title": "Software Development Engineer II"}, TITLE_FAMILY
    ) is True


def test_title_family_sde2_nospace():
    """'SDE2' must match the 'SDE' family term (SDE is a prefix of SDE2)."""
    assert passes_title_family_check({**GOOD_JOB, "title": "SDE2 - Backend"}, TITLE_FAMILY) is True


def test_title_family_sde_arabic():
    assert passes_title_family_check({**GOOD_JOB, "title": "SDE 2"}, TITLE_FAMILY) is True


def test_title_family_full_stack_hyphenated():
    assert passes_title_family_check({**GOOD_JOB, "title": "Full-Stack Developer"}, TITLE_FAMILY) is True


def test_title_family_dotnet_developer():
    assert passes_title_family_check({**GOOD_JOB, "title": ".NET Developer"}, TITLE_FAMILY) is True


def test_title_family_principal_passes_family_but_caught_by_exclude():
    """Principal Software Engineer *does* belong to the family — it's the exclude
    check (not the family check) that gates principal-level roles out."""
    assert passes_title_family_check(
        {**GOOD_JOB, "title": "Principal Software Engineer"}, TITLE_FAMILY
    ) is True
    assert passes_exclude_check(
        {**GOOD_JOB, "title": "Principal Software Engineer"}, EXCLUDE
    ) is False


# --- passes_exclude_check ---

def test_passes_exclude_check_good_job():
    assert passes_exclude_check(GOOD_JOB, EXCLUDE) is True


def test_passes_exclude_check_rejects_intern():
    assert passes_exclude_check(INTERN_JOB, EXCLUDE) is False


def test_passes_exclude_check_rejects_director():
    assert passes_exclude_check({**GOOD_JOB, "title": "Director of Engineering"}, EXCLUDE) is False


def test_passes_exclude_check_rejects_principal():
    assert passes_exclude_check({**GOOD_JOB, "title": "Principal Software Engineer"}, EXCLUDE) is False


def test_passes_exclude_check_rejects_manager():
    assert passes_exclude_check({**GOOD_JOB, "title": "Engineering Manager"}, EXCLUDE) is False


# --- matches_skills ---

def test_matches_skills_true():
    assert matches_skills(GOOD_DESCRIPTION, SKILLS) is True


def test_matches_skills_false():
    assert matches_skills(OUT_OF_STACK_DESCRIPTION, SKILLS) is False


def test_matches_skills_case_insensitive():
    assert matches_skills("experience with c# and asp.net required", SKILLS) is True


def test_matches_skills_tsql_only():
    """A JD that mentions only T-SQL must pass — T-SQL is a valid skill match."""
    jd = (
        "The ideal candidate will have strong experience writing complex T-SQL queries, "
        "stored procedures, and performance-tuning database workloads."
    )
    assert matches_skills(jd, SKILLS) is True


def test_matches_skills_dotnet_alias():
    """'dotnet' in a JD must match the '.NET' skill via normalisation."""
    assert matches_skills("Strong dotnet background required.", SKILLS) is True


def test_matches_skills_csharp_alias():
    """'C-Sharp' in a JD must match the 'C#' skill via normalisation."""
    assert matches_skills("We need someone fluent in C-Sharp and WPF.", SKILLS) is True


def test_matches_skills_azure_standalone():
    """'Azure' alone in a JD must match — verifies normalisation didn't break plain words."""
    assert matches_skills("Experience with Azure cloud services required.", SKILLS) is True


def test_matches_skills_csharp_and_azure_combined():
    """Both 'C#' and 'Azure' in the same JD must match after normalisation."""
    jd = (
        "We are looking for a developer with strong C# skills who has worked "
        "extensively on the Azure platform with REST APIs and SQL Server."
    )
    assert matches_skills(jd, SKILLS) is True


def test_matches_skills_off_stack_still_false():
    """A Python/ML-only description must still return False (off-stack drop is correct)."""
    assert matches_skills(OUT_OF_STACK_DESCRIPTION, SKILLS) is False


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
    """Return a drop-in for fetch_jobs: one page of *jobs*, then empty (stops pagination)."""
    def _fake(keyword, location, *, num=20, start=0, sort_by="date", timeout=20):
        return [] if start > 0 else list(jobs)
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


def test_known_candidate_skips_description_fetch(monkeypatch, capsys):
    """Seen IDs are pruned after cheap checks and before network-heavy details."""
    calls = []
    monkeypatch.setattr("matcher.fetch_jobs", _make_fake_fetch_jobs(GOOD_JOB))
    monkeypatch.setattr(
        "matcher.fetch_job_description",
        lambda *a, **kw: calls.append(a) or GOOD_DESCRIPTION,
    )

    total, matched = find_matching_jobs(CONFIG_PATH, known_ids={GOOD_JOB["id"]})

    assert total == 1
    assert matched == []
    assert calls == []
    assert "already-seen candidate" in capsys.readouterr().out


def test_known_non_candidate_does_not_change_total(monkeypatch):
    """Fetched count retains its historical meaning even with early pruning."""
    monkeypatch.setattr(
        "matcher.fetch_jobs", _make_fake_fetch_jobs(GOOD_JOB, INTERN_JOB)
    )
    monkeypatch.setattr("matcher.fetch_job_description", lambda *a, **kw: GOOD_DESCRIPTION)

    total, matched = find_matching_jobs(CONFIG_PATH, known_ids={INTERN_JOB["id"]})

    assert total == 2
    assert [job["id"] for job in matched] == [GOOD_JOB["id"]]


def test_find_matching_jobs_keeps_job_when_description_fetch_fails(monkeypatch):
    """If all fetch attempts for a description fail, the job must still be in results.

    Missing a real role is worse than an extra alert, so we keep unverifiable jobs.
    """
    def _always_raise(*a, **kw):
        raise RuntimeError("simulated network error")

    monkeypatch.setattr("matcher.fetch_jobs", _make_fake_fetch_jobs(GOOD_JOB))
    monkeypatch.setattr("matcher.fetch_job_description", _always_raise)

    _, matched = find_matching_jobs(CONFIG_PATH)
    assert any(j["id"] == GOOD_JOB["id"] for j in matched), (
        "A job whose description could not be fetched must still appear in results"
    )


def test_matches_skills_csharp_literal(monkeypatch):
    """Literal 'C#' (as it appears in real job descriptions) must match the C# skill."""
    assert matches_skills("Strong C# and ASP.NET experience required.", SKILLS) is True


def test_find_matching_jobs_keeps_job_when_description_empty(monkeypatch):
    """An empty description (API body had no text) must not cause the job to be dropped."""
    monkeypatch.setattr("matcher.fetch_jobs", _make_fake_fetch_jobs(GOOD_JOB))
    monkeypatch.setattr("matcher.fetch_job_description", lambda *a, **kw: "")

    _, matched = find_matching_jobs(CONFIG_PATH)
    assert any(j["id"] == GOOD_JOB["id"] for j in matched), (
        "A job with an empty description must still appear in results"
    )
