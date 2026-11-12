from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import video_scope.cli as cli
from video_scope.analysis_runs import batch_campaign_id, local_campaign_id, resolve_analysis_batch
from video_scope.analysis_store import AnalysisStore
from video_scope.evidence import digest
from video_scope.jobs import Store, read_analysis_batch
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


def test_analyze_file_requires_upload_before_reading_source_or_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "digest",
        lambda *_args, **_kwargs: pytest.fail("source must not be read without upload consent"),
    )
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: pytest.fail("provider startup must not run without upload consent"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "analyze-file",
            str(tmp_path / "missing.mp4"),
            "--budget-usd",
            "1",
            "--data-dir",
            str(tmp_path / "data"),
        ],
    )

    assert result.exit_code == 1
    assert "--allow-upload" in result.output
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--mode", "invalid", "--budget-usd", "1"], "full, opening or both"),
        (["--mode", "full", "--budget-usd", "0"], "finite amount"),
        (["--mode", "full", "--budget-usd", "5.01"], "finite amount"),
        (
            ["--mode", "full", "--budget-usd", "1", "--run-id", "../invalid"],
            "1-48 letters",
        ),
    ],
)
def test_analyze_file_rejects_invalid_scope_before_provider_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    message: str,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"local video")
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: pytest.fail("invalid local scope must fail before provider startup"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "analyze-file",
            str(source),
            *extra,
            "--data-dir",
            str(tmp_path / "data"),
            "--allow-upload",
        ],
    )

    assert result.exit_code == 1
    assert message in result.output
    assert not (tmp_path / "data").exists()


def test_analyze_file_both_uses_one_campaign_and_two_modes_without_source_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"arbitrary local video")
    source_hash = digest(source)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        cli,
        "sync_sources",
        lambda *_args, **_kwargs: pytest.fail("local analysis must not sync remote samples"),
    )
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: (ProviderConfig("secret"), _pricing()),
    )
    calls: list[dict[str, Any]] = []

    def analyze(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        sample = kwargs["sample"]
        return {
            "analysis_id": f"analysis-{sample['mode']}",
            "mode": sample["mode"],
            "package": f"/unused/{sample['mode']}",
            "reused": False,
        }

    monkeypatch.setattr(cli, "analyze_sample", analyze)
    result = CliRunner().invoke(
        cli.app,
        [
            "analyze-file",
            str(source),
            "--mode",
            "both",
            "--budget-usd",
            "1.25",
            "--data-dir",
            str(data_dir),
            "--allow-upload",
            "--stream",
        ],
    )

    assert result.exit_code == 0, result.output
    campaign_id = local_campaign_id(source_hash, "both")
    assert [call["sample"]["mode"] for call in calls] == ["opening", "full"]
    assert {call["campaign_id"] for call in calls} == {campaign_id}
    assert all(call["sample"]["source_sha256"] == source_hash for call in calls)
    assert all(call["sample"]["path"] == str(source.resolve()) for call in calls)
    assert all(call["config"].streaming is True for call in calls)
    payload = json.loads(result.stdout)
    assert payload["campaign_id"] == campaign_id
    assert payload["status"] == "completed"
    assert payload["completed_count"] == 2
    assert [record["mode"] for record in payload["completed"]] == ["opening", "full"]
    assert payload["ledger"]["budget_usd"] == "1.25"
    campaign_dir = data_dir / "campaigns" / campaign_id
    manifest = json.loads((campaign_dir / "input.json").read_text(encoding="utf-8"))
    assert manifest["source_sha256"] == source_hash
    assert manifest["requested_mode"] == "both"
    assert manifest["modes"] == ["opening", "full"]


def test_analyze_file_failure_keeps_completed_mode_and_offline_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"local video with partial progress")
    source_hash = digest(source)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: (ProviderConfig("secret"), _pricing()),
    )

    def analyze(**kwargs: Any) -> dict[str, Any]:
        mode = kwargs["sample"]["mode"]
        if mode == "full":
            raise RuntimeError("supplier stopped")
        return {
            "analysis_id": "opening-analysis",
            "mode": mode,
            "package": "/unused/opening",
            "reused": False,
        }

    monkeypatch.setattr(cli, "analyze_sample", analyze)
    run = CliRunner().invoke(
        cli.app,
        [
            "analyze-file",
            str(source),
            "--mode",
            "both",
            "--budget-usd",
            "1",
            "--data-dir",
            str(data_dir),
            "--allow-upload",
        ],
    )

    assert run.exit_code == 1
    campaign_id = local_campaign_id(source_hash, "both")
    progress = json.loads(
        (data_dir / "campaigns" / campaign_id / "progress.json").read_text(encoding="utf-8")
    )
    assert progress["status"] == "needs_attention"
    assert progress["completed_count"] == 1
    assert progress["completed"][0]["mode"] == "opening"
    assert progress["last_error"] == "RuntimeError: supplier stopped"

    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: pytest.fail("analysis-status must be offline"),
    )
    status = CliRunner().invoke(
        cli.app,
        ["analysis-status", campaign_id, "--data-dir", str(data_dir)],
    )
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.stdout)
    assert status_payload["status"] == "needs_attention"
    assert status_payload["completed_count"] == 1
    assert status_payload["ledger"]["campaign_id"] == campaign_id


def test_analyze_file_cannot_silently_change_existing_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same local video")
    data_dir = tmp_path / "data"
    preflights = 0

    def preflight() -> tuple[ProviderConfig, Pricing]:
        nonlocal preflights
        preflights += 1
        return ProviderConfig("secret"), _pricing()

    monkeypatch.setattr(cli, "_provider_preflight", preflight)
    monkeypatch.setattr(
        cli,
        "analyze_sample",
        lambda **kwargs: {
            "analysis_id": "analysis",
            "mode": kwargs["sample"]["mode"],
            "package": "/unused/package",
            "reused": False,
        },
    )
    runner = CliRunner()
    common = [
        "analyze-file",
        str(source),
        "--data-dir",
        str(data_dir),
        "--allow-upload",
    ]
    first = runner.invoke(cli.app, [*common, "--budget-usd", "1"])
    changed = runner.invoke(cli.app, [*common, "--budget-usd", "2"])
    separate = runner.invoke(
        cli.app,
        [*common, "--budget-usd", "2", "--run-id", "second-run"],
    )

    assert first.exit_code == 0, first.output
    assert changed.exit_code == 1
    assert "cannot be reset" in changed.output
    assert separate.exit_code == 0, separate.output
    assert json.loads(separate.stdout)["campaign_id"] == "local-second-run"
    assert preflights == 2


def test_analyze_batch_validates_all_rows_before_provider_or_runtime_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    manifest = tmp_path / "batch.jsonl"
    manifest.write_text(
        json.dumps({"source": source.name, "mode": "full"})
        + "\n"
        + json.dumps({"source": "missing.mp4"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: pytest.fail("a malformed batch must not start provider preflight"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "analyze-batch",
            str(manifest),
            "--budget-usd",
            "2",
            "--data-dir",
            str(tmp_path / "data"),
            "--allow-upload",
        ],
    )

    assert result.exit_code == 1
    assert "line 2 must contain only" in result.output
    assert not (tmp_path / "data").exists()


def test_analyze_batch_runs_ordered_units_under_one_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first video")
    second.write_bytes(b"second video")
    manifest = tmp_path / "batch.jsonl"
    manifest.write_text(
        json.dumps({"source": first.name, "mode": "both"})
        + "\n"
        + json.dumps({"source": second.name, "mode": "opening"})
        + "\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        cli,
        "sync_sources",
        lambda *_args, **_kwargs: pytest.fail("analysis batch must not sync remote samples"),
    )
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: (ProviderConfig("secret"), _pricing()),
    )
    calls: list[dict[str, Any]] = []

    def analyze(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        sample = kwargs["sample"]
        return {
            "analysis_id": f"analysis-{len(calls)}",
            "mode": sample["mode"],
            "package": f"/unused/{len(calls)}",
            "reused": False,
        }

    monkeypatch.setattr(cli, "analyze_sample", analyze)
    result = CliRunner().invoke(
        cli.app,
        [
            "analyze-batch",
            str(manifest),
            "--budget-usd",
            "2.5",
            "--data-dir",
            str(data_dir),
            "--allow-upload",
            "--stream",
        ],
    )

    assert result.exit_code == 0, result.output
    items = resolve_analysis_batch(read_analysis_batch(manifest))
    campaign_id = batch_campaign_id(items)
    assert [(Path(call["sample"]["path"]), call["sample"]["mode"]) for call in calls] == [
        (first, "opening"),
        (first, "full"),
        (second, "opening"),
    ]
    assert {call["campaign_id"] for call in calls} == {campaign_id}
    assert all(call["config"].streaming is True for call in calls)
    payload = json.loads(result.stdout)
    assert payload["campaign_id"] == campaign_id
    assert payload["status"] == "completed"
    assert payload["entry_count"] == 2
    assert payload["completed_count"] == 3
    assert payload["total"] == 3
    assert payload["ledger"]["budget_usd"] == "2.5"
    assert [record["requested_mode"] for record in payload["completed"]] == [
        "both",
        "both",
        "opening",
    ]
    saved = json.loads(
        (data_dir / "campaigns" / campaign_id / "input.json").read_text(encoding="utf-8")
    )
    assert saved["kind"] == "local-analysis-batch"
    assert len(saved["items"]) == 2


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"source": "same.mp4", "mode": "full"},
            {"source": "same.mp4", "mode": "both"},
        ],
        [{"source": f"source-{index}.mp4", "mode": "full"} for index in range(5)],
    ],
)
def test_analyze_batch_rejects_duplicate_or_oversized_work_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, str]],
) -> None:
    for row in rows:
        (tmp_path / row["source"]).write_bytes(row["source"].encode())
    manifest = tmp_path / "batch.jsonl"
    manifest.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: pytest.fail("invalid batch work must fail before provider startup"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "analyze-batch",
            str(manifest),
            "--budget-usd",
            "5",
            "--data-dir",
            str(tmp_path / "data"),
            "--allow-upload",
        ],
    )

    assert result.exit_code == 1
    assert "duplicates" in result.output or "at most four" in result.output
    assert not (tmp_path / "data").exists()


def test_analyze_batch_continues_after_nonblocking_failure_and_reports_all_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    third = tmp_path / "third.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    third.write_bytes(b"third")
    manifest = tmp_path / "batch.jsonl"
    manifest.write_text(
        json.dumps({"source": first.name, "mode": "full"})
        + "\n"
        + json.dumps({"source": second.name, "mode": "opening"})
        + "\n"
        + json.dumps({"source": third.name, "mode": "full"})
        + "\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: (ProviderConfig("secret"), _pricing()),
    )
    attempted: list[str] = []

    def analyze(**kwargs: Any) -> dict[str, Any]:
        mode = str(kwargs["sample"]["mode"])
        attempted.append(mode)
        if len(attempted) == 2:
            raise RuntimeError("second item failed")
        return {
            "analysis_id": "first-analysis",
            "mode": mode,
            "package": "/unused/first",
            "reused": False,
        }

    monkeypatch.setattr(cli, "analyze_sample", analyze)
    result = CliRunner().invoke(
        cli.app,
        [
            "analyze-batch",
            str(manifest),
            "--budget-usd",
            "2",
            "--data-dir",
            str(data_dir),
            "--allow-upload",
        ],
    )

    assert result.exit_code == 1
    assert attempted == ["full", "opening", "full"]
    items = resolve_analysis_batch(read_analysis_batch(manifest))
    campaign_id = batch_campaign_id(items)
    progress = json.loads(
        (data_dir / "campaigns" / campaign_id / "progress.json").read_text(encoding="utf-8")
    )
    assert progress["status"] == "completed_with_failures"
    assert progress["completed_count"] == 2
    assert progress["completed"][0]["line"] == 1
    assert progress["completed"][1]["line"] == 3
    assert progress["failure_count"] == 1
    assert progress["failures"][0]["line"] == 2
    assert progress["failures"][0]["blocking"] is False
    assert progress["failures"][0]["error"] == "RuntimeError: second item failed"


def test_analyze_batch_stops_after_unknown_paid_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = [tmp_path / f"source-{index}.mp4" for index in range(3)]
    for index, source in enumerate(sources):
        source.write_bytes(f"video-{index}".encode())
    manifest = tmp_path / "batch.jsonl"
    manifest.write_text(
        "".join(json.dumps({"source": source.name, "mode": "full"}) + "\n" for source in sources),
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        cli,
        "_provider_preflight",
        lambda: (ProviderConfig("secret"), _pricing()),
    )
    attempts = 0

    def analyze(**kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        ledger = kwargs["ledger"]
        sample = kwargs["sample"]
        analysis_id = ledger.analysis(
            kwargs["campaign_id"], "unknown-analysis", sample["mode"], sample["source_sha256"]
        )
        reserved = ledger.reserve(
            analysis_id,
            "visual",
            "f" * 64,
            Decimal("0.01"),
        )
        ledger.sending(reserved["id"])
        ledger.unknown(reserved["id"], "read timed out after dispatch")
        raise RuntimeError("supplier outcome is unknown")

    monkeypatch.setattr(cli, "analyze_sample", analyze)
    result = CliRunner().invoke(
        cli.app,
        [
            "analyze-batch",
            str(manifest),
            "--budget-usd",
            "2",
            "--data-dir",
            str(data_dir),
            "--allow-upload",
        ],
    )

    assert result.exit_code == 1
    assert attempts == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "needs_attention"
    assert payload["completed_count"] == 0
    assert payload["failure_count"] == 1
    assert payload["failures"][0]["blocking"] is True
    assert payload["ledger"]["unknown_calls"] == 1


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
