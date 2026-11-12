from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import video_scope.cli as cli
from video_scope.analysis_store import AnalysisStore
from video_scope.jobs import Store
from video_scope.provider import ENDPOINT, MODEL, Pricing, ProviderConfig


def _samples() -> list[dict[str, Any]]:
    return [
        {
            "mode": "full",
            "identity": "full-1",
            "source_sha256": "1" * 64,
            "path": "/unused/full-1.mp4",
        },
        {
            "mode": "opening",
            "identity": "opening-1",
            "source_sha256": "2" * 64,
            "path": "/unused/opening-1.mp4",
        },
        {
            "mode": "full",
            "identity": "full-2",
            "source_sha256": "3" * 64,
            "path": "/unused/full-2.mp4",
        },
        {
            "mode": "opening",
            "identity": "opening-2",
            "source_sha256": "4" * 64,
            "path": "/unused/opening-2.mp4",
        },
    ]


def _snapshot(tmp_path: Path) -> dict[str, Any]:
    return {
        "status": "offline",
        "commit": "a" * 40,
        "snapshot": str(tmp_path / "snapshot"),
        "catalog": str(tmp_path / "snapshot/catalog.sqlite3"),
    }


def _pricing() -> Pricing:
    return Pricing(Decimal("0.75"), Decimal("3.75"), Decimal("1"), "test", "a" * 64)


def _mock_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, samples: list[dict[str, Any]]
) -> None:
    def sync(store: Store, *, offline: bool = False) -> dict[str, Any]:
        assert offline is True
        return _snapshot(tmp_path)

    monkeypatch.setattr(cli, "sync_sources", sync)
    monkeypatch.setattr(cli, "select_samples", lambda *_args, **_kwargs: samples)
    monkeypatch.setattr(cli, "_provider_preflight", lambda: (ProviderConfig("secret"), _pricing()))


def test_acceptance_requires_explicit_upload_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "sync_sources",
        lambda *_args, **_kwargs: pytest.fail("startup must not run without --allow-upload"),
    )

    result = CliRunner().invoke(
        cli.app, ["acceptance", "--data-dir", str(tmp_path / "data"), "--offline"]
    )

    assert result.exit_code == 1
    assert "--allow-upload" in result.output
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("streaming", [False, True])
def test_acceptance_uses_fixed_order_shared_ledger_and_durable_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    samples = _samples()
    _mock_startup(monkeypatch, tmp_path, samples)
    analyzed: list[str] = []

    def analyze(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["campaign_id"] == cli.CAMPAIGN_ID
        assert kwargs["config"].streaming is streaming
        analyzed.append(kwargs["sample"]["identity"])
        return {
            "analysis_id": kwargs["sample"]["identity"],
            "mode": kwargs["sample"]["mode"],
            "package": "/unused/package",
            "reused": False,
        }

    monkeypatch.setattr(cli, "analyze_sample", analyze)
    real_save_progress = cli._save_progress
    writes: list[dict[str, Any]] = []

    def save_progress(
        ledger: AnalysisStore,
        campaign_dir: Path,
        completed: list[dict[str, Any]],
        samples: list[dict[str, Any]],
        error: Exception | None = None,
    ) -> dict[str, Any]:
        value = real_save_progress(ledger, campaign_dir, completed, samples, error)
        writes.append(copy.deepcopy(value))
        return value

    monkeypatch.setattr(cli, "_save_progress", save_progress)
    data_dir = tmp_path / "data"
    result = CliRunner().invoke(
        cli.app,
        [
            "acceptance",
            "--data-dir",
            str(data_dir),
            "--offline",
            "--allow-upload",
            *(["--stream"] if streaming else []),
        ],
    )

    assert result.exit_code == 0, result.output
    assert analyzed == ["opening-1", "full-1", "opening-2", "full-2"]
    assert [item["completed_count"] for item in writes] == [1, 2, 3, 4]
    progress_path = data_dir / "campaigns" / cli.CAMPAIGN_ID / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["completed_count"] == 4
    assert progress["ledger"]["budget_usd"] == "5"
    assert progress["ledger"]["campaign_id"] == cli.CAMPAIGN_ID


def test_acceptance_stops_at_first_analysis_error_and_keeps_prior_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples = _samples()
    _mock_startup(monkeypatch, tmp_path, samples)
    analyzed: list[str] = []

    def analyze(**kwargs: Any) -> dict[str, Any]:
        identity = str(kwargs["sample"]["identity"])
        analyzed.append(identity)
        if len(analyzed) == 2:
            raise RuntimeError("supplier stopped")
        return {
            "analysis_id": identity,
            "mode": kwargs["sample"]["mode"],
            "package": "/unused/package",
            "reused": False,
        }

    monkeypatch.setattr(cli, "analyze_sample", analyze)
    data_dir = tmp_path / "data"
    result = CliRunner().invoke(
        cli.app,
        [
            "acceptance",
            "--data-dir",
            str(data_dir),
            "--offline",
            "--allow-upload",
        ],
    )

    assert result.exit_code == 1
    assert analyzed == ["opening-1", "full-1"]
    progress = json.loads((data_dir / "campaigns" / cli.CAMPAIGN_ID / "progress.json").read_text())
    assert progress["completed_count"] == 1
    assert progress["completed"][0]["identity"] == "opening-1"
    assert progress["status"] == "needs_attention"
    assert progress["last_error"] == "RuntimeError: supplier stopped"


def test_analyze_runs_only_matching_selected_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    ledger = AnalysisStore(Store(data_dir))
    campaign_dir = ledger.campaign(cli.CAMPAIGN_ID, Decimal("5"), MODEL, ENDPOINT)
    (campaign_dir / "samples.json").write_text("[]", encoding="utf-8")
    samples = _samples()
    _mock_startup(monkeypatch, tmp_path, samples)
    analyzed: list[str] = []

    def analyze(**kwargs: Any) -> dict[str, Any]:
        analyzed.append(str(kwargs["sample"]["identity"]))
        return {
            "analysis_id": "selected-analysis",
            "mode": kwargs["sample"]["mode"],
            "package": "/unused/package",
            "reused": False,
        }

    monkeypatch.setattr(cli, "analyze_sample", analyze)
    result = CliRunner().invoke(
        cli.app,
        [
            "analyze",
            "4" * 64,
            "--data-dir",
            str(data_dir),
            "--offline",
            "--allow-upload",
        ],
    )

    assert result.exit_code == 0, result.output
    assert analyzed == ["opening-2"]
    payload = json.loads(result.stdout)
    assert payload["source_sha256"] == "4" * 64
    assert payload["identity"] == "opening-2"


def test_analyze_merges_progress_by_selected_source_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    ledger = AnalysisStore(Store(data_dir))
    campaign_dir = ledger.campaign(cli.CAMPAIGN_ID, Decimal("5"), MODEL, ENDPOINT)
    (campaign_dir / "samples.json").write_text("[]", encoding="utf-8")
    samples = _samples()
    _mock_startup(monkeypatch, tmp_path, samples)

    def analyze(**kwargs: Any) -> dict[str, Any]:
        sample = kwargs["sample"]
        return {
            "analysis_id": sample["identity"],
            "mode": sample["mode"],
            "package": "/unused/package",
            "reused": False,
        }

    monkeypatch.setattr(cli, "analyze_sample", analyze)
    runner = CliRunner()
    common = ["--data-dir", str(data_dir), "--offline", "--allow-upload"]
    first = runner.invoke(cli.app, ["analyze", "2" * 64, *common])
    second = runner.invoke(cli.app, ["analyze", "1" * 64, *common])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    progress = json.loads((campaign_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["completed_count"] == 2
    assert [record["source_sha256"] for record in progress["completed"]] == [
        "2" * 64,
        "1" * 64,
    ]


def test_analyze_mode_is_consistency_check_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    ledger = AnalysisStore(Store(data_dir))
    campaign_dir = ledger.campaign(cli.CAMPAIGN_ID, Decimal("5"), MODEL, ENDPOINT)
    (campaign_dir / "samples.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(cli, "select_samples", lambda *_args, **_kwargs: _samples())
    monkeypatch.setattr(
        cli,
        "sync_sources",
        lambda *_args, **_kwargs: pytest.fail("mode mismatch must fail before source sync"),
    )
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: pytest.fail("mode mismatch must fail before pricing preflight"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "analyze",
            "2" * 64,
            "--mode",
            "full",
            "--data-dir",
            str(data_dir),
            "--offline",
            "--allow-upload",
        ],
    )

    assert result.exit_code == 1
    assert "does not match" in result.output
    progress = json.loads((campaign_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "needs_attention"
    assert progress["completed_count"] == 0


def test_analyze_rejects_hash_outside_existing_campaign_before_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    ledger = AnalysisStore(Store(data_dir))
    campaign_dir = ledger.campaign(cli.CAMPAIGN_ID, Decimal("5"), MODEL, ENDPOINT)
    (campaign_dir / "samples.json").write_text("[]", encoding="utf-8")
    samples = _samples()
    _mock_startup(monkeypatch, tmp_path, samples)
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: pytest.fail("unauthorized hash must fail before pricing preflight"),
    )
    monkeypatch.setattr(
        cli,
        "analyze_sample",
        lambda **_kwargs: pytest.fail("unauthorized hash must never be analyzed"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "analyze",
            "f" * 64,
            "--data-dir",
            str(data_dir),
            "--offline",
            "--allow-upload",
        ],
    )

    assert result.exit_code == 1
    assert "not one of the four authorized" in result.output


def test_analyze_does_not_create_missing_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        cli,
        "sync_sources",
        lambda *_args, **_kwargs: pytest.fail("missing authorization must fail before startup"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "analyze",
            "1" * 64,
            "--data-dir",
            str(data_dir),
            "--offline",
            "--allow-upload",
        ],
    )

    assert result.exit_code == 1
    ledger = AnalysisStore(Store(data_dir))
    with ledger.connect() as database:
        count = database.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    assert count == 0


def test_status_without_task_id_emits_local_campaign_ledger(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ledger = AnalysisStore(Store(data_dir))
    ledger.campaign(cli.CAMPAIGN_ID, Decimal("5"), MODEL, ENDPOINT)

    result = CliRunner().invoke(cli.app, ["status", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["campaign_id"] == cli.CAMPAIGN_ID
    assert payload["budget_usd"] == "5"
    assert payload["call_count"] == 0


def test_validate_package_command_is_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    monkeypatch.setattr(
        cli,
        "validate_package",
        lambda path: (
            {"mode": "opening", "source_id": "1" * 64}
            if path == package
            else pytest.fail("wrong package path")
        ),
    )

    result = CliRunner().invoke(cli.app, ["validate-package", str(package)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "valid": True,
        "manifest": {"mode": "opening", "source_id": "1" * 64},
    }
