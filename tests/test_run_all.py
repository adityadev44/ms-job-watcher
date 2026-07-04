"""Tests for bounded all-company orchestration."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import run_all


def test_selection_is_sorted_deduplicated_and_accepts_commas() -> None:
    selected = run_all._selected_companies([["optum,amazon", "optum"]])
    assert selected == ["amazon", "optum"]


def test_unknown_company_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown companies: imaginary"):
        run_all._selected_companies([["imaginary"]])


def test_bounded_runner_continues_and_returns_deterministic_statuses() -> None:
    active = 0
    peak = 0

    def fake_main(argv: list[str]) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            return 1 if argv[0] == "amazon" else 0
        finally:
            active -= 1

    with patch("run_all.run_company.main", side_effect=fake_main) as runner:
        statuses = run_all.run_companies(
            ["amazon", "optum", "siemens"], ROOT / "config.yaml", max_workers=2
        )

    assert statuses == {"amazon": "failed", "optum": "ok", "siemens": "ok"}
    assert runner.call_count == 3
    assert peak <= 2


def test_main_exits_nonzero_if_one_pipeline_fails(capsys) -> None:
    with patch(
        "run_all.run_companies",
        return_value={"optum": "ok", "amazon": "failed"},
    ) as execute:
        result = run_all.main(["--companies", "optum,amazon", "--workers", "3"])

    assert result == 1
    execute.assert_called_once_with(
        ["amazon", "optum"], ROOT / "config.yaml", 3
    )
    output = capsys.readouterr().out
    assert output.index("amazon: failed") < output.index("optum: ok")
    assert "Total: 2 | Succeeded: 1 | Failed: 1" in output


def test_validate_does_not_execute_pipelines(capsys) -> None:
    with (
        patch("run_all.validate_companies", return_value={}) as validate,
        patch("run_all.run_companies") as execute,
    ):
        result = run_all.main(["--companies", "optum", "--validate"])

    assert result == 0
    validate.assert_called_once_with(["optum"], ROOT / "config.yaml")
    execute.assert_not_called()
    assert "VALIDATION SUMMARY" in capsys.readouterr().out


def test_validation_failure_is_reported() -> None:
    with patch("run_all.validate_companies", return_value={"optum": "bad contract"}):
        assert run_all.main(["--companies", "optum", "--validate"]) == 1


def test_invalid_worker_count_is_rejected() -> None:
    with pytest.raises(SystemExit):
        run_all.main(["--workers", "0"])
