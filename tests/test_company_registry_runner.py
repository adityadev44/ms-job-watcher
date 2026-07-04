"""Contract tests for the registry-driven company runner."""
from __future__ import annotations

import sys
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import company_registry
import run_company
from company_registry import COMPANY_REGISTRY, CompanyPipeline
from notifier import DeliveryResult
from run_company import (
    _apply_description_filter,
    _delivery_succeeded,
    _load_fetcher,
    _pipeline_config,
    main,
    run_company_pipeline,
)


def test_registry_exactly_covers_fetchers_config_and_state() -> None:
    fetcher_slugs = {
        path.stem.removesuffix("_fetcher")
        for path in (ROOT / "src").glob("*_fetcher.py")
    }
    fetcher_slugs.add("microsoft")  # Microsoft's adapter is fetcher.py.
    assert set(COMPANY_REGISTRY) == fetcher_slugs
    assert len(COMPANY_REGISTRY) == 74

    config = run_company.load_config(ROOT / "config.yaml")
    configured_slugs = {
        "microsoft" if key == "search" else key.removesuffix("_search")
        for key in config
        if key == "search" or key.endswith("_search")
    }
    assert set(COMPANY_REGISTRY) == configured_slugs

    for spec in COMPANY_REGISTRY.values():
        assert (ROOT / spec.seen_file).is_file()
        search = config[spec.config_key]
        assert spec.requires_tech_in_description == bool(
            search.get("require_tech_in_description")
        )


def test_registry_does_not_import_fetchers_eagerly() -> None:
    fetcher_names = {spec.fetcher_module for spec in COMPANY_REGISTRY.values()}
    before = fetcher_names.intersection(sys.modules)
    importlib.reload(company_registry)
    after = fetcher_names.intersection(sys.modules)
    assert after == before


def test_registry_exposes_conservative_fetcher_capabilities() -> None:
    assert COMPANY_REGISTRY["bankofamerica"].supports_keyword_filter is False
    assert COMPANY_REGISTRY["hsbc"].supports_location_filter is True
    assert COMPANY_REGISTRY["amazon"].description_inline is True
    assert COMPANY_REGISTRY["virtusa"].newest_first is True
    assert COMPANY_REGISTRY["barclays"].supports_location_filter is False


def test_keyword_ignoring_pipeline_allows_intentional_empty_query() -> None:
    spec = COMPANY_REGISTRY["metlife"]
    cfg = _pipeline_config(
        {"metlife_search": {"keywords": [""]}, "matching": {}}, spec
    )
    assert cfg["search"]["keywords"] == [""]


def test_fetcher_contract_validation() -> None:
    spec = CompanyPipeline("demo", "Demo", "demo_fetcher", "demo_search", "seen.json")
    incomplete = SimpleNamespace(fetch_jobs=lambda *args: [])
    with patch("run_company.importlib.import_module", return_value=incomplete):
        with pytest.raises(TypeError, match="fetch_job_description, RateLimitError"):
            _load_fetcher(spec)


def test_config_validation_and_description_filter() -> None:
    spec = CompanyPipeline(
        "demo",
        "Demo",
        "demo_fetcher",
        "demo_search",
        "seen.json",
        "require_any_configured_term",
    )
    whole = {
        "demo_search": {
            "keywords": ["engineer"],
            "require_tech_in_description": ["C#", ".NET"],
        },
        "matching": {"skills": ["software"]},
    }
    cfg = _pipeline_config(whole, spec)
    jobs = [
        {"id": "1", "title": "One", "description": "Build services in C#"},
        {"id": "2", "title": "Two", "description": "Java services"},
    ]
    assert [job["id"] for job in _apply_description_filter(jobs, cfg["search"], spec)] == [
        "1"
    ]


def test_keyword_agnostic_fetcher_runs_one_pass_without_mutating_config() -> None:
    spec = COMPANY_REGISTRY["bankofamerica"]
    search = {"keywords": ["dotnet", "csharp", "aspnet"]}
    whole = {spec.config_key: search, "matching": {}}
    cfg = _pipeline_config(whole, spec)
    assert cfg["search"]["keywords"] == ["dotnet"]
    assert search["keywords"] == ["dotnet", "csharp", "aspnet"]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, True),
        (True, True),
        (False, False),
        ({"delivered": True}, True),
        ({"channels": {"email": False}}, False),
        (DeliveryResult(), True),
        (DeliveryResult(email_attempted=True), False),
        (DeliveryResult(email_attempted=True, email_succeeded=True), True),
    ],
)
def test_delivery_result_adapter(result, expected: bool) -> None:
    assert _delivery_succeeded(result) is expected


def test_pipeline_only_advances_state_after_success(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("ignored", encoding="utf-8")
    seen = tmp_path / "seen.json"
    fetcher = SimpleNamespace()
    jobs = [{"id": "new", "title": "Engineer", "description": "C#"}]
    config_data = {"amazon_search": {"keywords": ["engineer"]}, "matching": {}}

    with (
        patch("run_company.load_config", return_value=config_data),
        patch("run_company.load_seen_ids", return_value={"old"}),
        patch("run_company.find_matching_jobs", return_value=(4, jobs)),
        patch("run_company.save_seen_ids") as save,
    ):
        failed = run_company_pipeline(
            "amazon", config, seen, fetcher=fetcher, notify_func=lambda *a, **k: False
        )
        save.assert_not_called()
        assert failed["state_updated"] is False

        successful = run_company_pipeline(
            "amazon", config, seen, fetcher=fetcher, notify_func=lambda *a, **k: True
        )
        save.assert_called_once_with(seen, {"old", "new"})
        assert successful["state_updated"] is True


def test_cli_returns_nonzero_and_records_pipeline_failure() -> None:
    with (
        patch("run_company.run_company_pipeline", side_effect=RuntimeError("boom")),
        patch("run_company.notify_pipeline_error") as report,
    ):
        assert main(["amazon"]) == 1
        report.assert_called_once()


def test_cli_resets_failure_count_after_success() -> None:
    with (
            patch(
                "run_company.run_company_pipeline",
                return_value={"new": 0, "alert_sent": False, "delivery_failed": False},
        ),
        patch("run_company.reset_failure_count") as reset,
    ):
        assert main(["amazon"]) == 0
        reset.assert_called_once_with("Amazon")


def test_cli_returns_nonzero_when_all_delivery_channels_fail() -> None:
    result = {"new": 1, "alert_sent": False, "delivery_failed": True}
    with (
        patch("run_company.run_company_pipeline", return_value=result),
        patch("run_company.notify_pipeline_error") as report,
        patch("run_company.reset_failure_count") as reset,
    ):
        assert main(["amazon"]) == 1
        report.assert_called_once()
        reset.assert_not_called()
