"""VideoScope evidence preparation and explicitly authorized analysis commands."""

import json
import shutil
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer

from video_scope.analysis import analyze_sample
from video_scope.analysis_store import AnalysisStore, atomic_json
from video_scope.evidence import validate_evidence
from video_scope.export import validate_package
from video_scope.jobs import Store, export_evidence, read_batch
from video_scope.provider import ENDPOINT, MODEL, Pricing, ProviderConfig, fetch_pricing
from video_scope.remote import sync_sources
from video_scope.review import review_package
from video_scope.samples import select_samples

app = typer.Typer(no_args_is_help=True, help="VideoScope 本地证据准备与显式授权的视频拆解。")

CAMPAIGN_ID = "four-examples-v1"
CAMPAIGN_BUDGET = Decimal("5")
DataDir = Annotated[
    Path,
    typer.Option("--data-dir", envvar="VIDEOSCOPE_DATA_DIR", help="代码目录外的运行数据目录"),
]
DEFAULT_DATA = Path.home() / ".local/share/video-scope"
Offline = Annotated[
    bool, typer.Option("--offline", help="明确跳过远端检查，只使用校验通过的已有本地数据源")
]
Stream = Annotated[
    bool, typer.Option("--stream", help="原生 SSE 分块接收并落盘；不重发未知请求或解除预算暂停")
]
AllowUpload = Annotated[
    bool,
    typer.Option("--allow-upload", help="明确授权将四样本之一上传给固定供应商并产生费用"),
]


def processing_store(data_dir: Path, offline: bool) -> Store:
    store = Store(data_dir)
    result = sync_sources(store, offline=offline)
    typer.echo(
        f"数据源：{result['status']}；提交 {result['commit']}；目录 {result['snapshot']}",
        err=True,
    )
    return store


def emit(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def fail(exc: Exception) -> None:
    typer.echo(f"错误：{exc}", err=True)
    raise typer.Exit(1) from exc


def _require_upload(allow_upload: bool) -> None:
    if not allow_upload:
        raise ValueError("supplier upload and paid analysis require explicit --allow-upload")


def _ordered_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    openings = [sample for sample in samples if sample.get("mode") == "opening"]
    full = [sample for sample in samples if sample.get("mode") == "full"]
    if len(openings) != 2 or len(full) != 2 or len(samples) != 4:
        raise ValueError(
            "authorized campaign must contain exactly two opening and two full samples"
        )
    return [openings[0], full[0], openings[1], full[1]]


def _analysis_record(sample: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_sha256": sample["source_sha256"],
        "identity": sample["identity"],
        **result,
    }


def _save_progress(
    ledger: AnalysisStore,
    campaign_dir: Path,
    completed: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    error: Exception | None = None,
) -> dict[str, Any]:
    progress_path = campaign_dir / "progress.json"
    authorized_hashes = [str(sample["source_sha256"]) for sample in samples]
    records: dict[str, dict[str, Any]] = {}
    warning: str | None = None
    if progress_path.is_file():
        try:
            existing = json.loads(progress_path.read_text(encoding="utf-8"))
            if existing.get("campaign_id") != CAMPAIGN_ID or not isinstance(
                existing.get("completed"), list
            ):
                raise ValueError("unexpected progress report shape")
            for record in existing["completed"]:
                if not isinstance(record, dict):
                    raise ValueError("progress completion record is not an object")
                source_hash = record.get("source_sha256")
                if source_hash in authorized_hashes:
                    records[str(source_hash)] = record
        except (OSError, ValueError, AttributeError) as exc:
            warning = f"previous progress report could not be merged: {type(exc).__name__}"
    for record in completed:
        source_hash = str(record["source_sha256"])
        if source_hash not in authorized_hashes:
            raise ValueError("progress record is outside the fixed campaign samples")
        records[source_hash] = record
    ordered = [records[source_hash] for source_hash in authorized_hashes if source_hash in records]
    payload: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "status": "needs_attention"
        if error is not None
        else "completed"
        if len(ordered) == len(samples)
        else "in_progress",
        "completed_count": len(ordered),
        "total": len(samples),
        "completed": ordered,
        "ledger": ledger.summary(CAMPAIGN_ID),
    }
    if error is not None:
        payload["last_error"] = f"{type(error).__name__}: {error}"[:2000]
    if warning is not None:
        payload["warning"] = warning
    atomic_json(progress_path, payload)
    return payload


def _save_failure_progress(
    ledger: AnalysisStore | None,
    campaign_dir: Path | None,
    completed: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    error: Exception,
) -> None:
    if ledger is None or campaign_dir is None or not samples:
        return
    try:
        _save_progress(ledger, campaign_dir, completed, samples, error)
    except (OSError, ValueError):
        pass


def _existing_campaign(ledger: AnalysisStore) -> Path:
    with ledger.connect() as database:
        row = database.execute("SELECT * FROM campaigns WHERE id=?", (CAMPAIGN_ID,)).fetchone()
    if row is None:
        raise ValueError(
            f"campaign {CAMPAIGN_ID} is not authorized; run the acceptance command first"
        )
    if (
        Decimal(row["budget_usd"]),
        str(row["model"]),
        str(row["endpoint"]),
    ) != (CAMPAIGN_BUDGET, MODEL, ENDPOINT):
        raise ValueError("existing campaign does not match the fixed acceptance authorization")
    directory = ledger.root / "campaigns" / CAMPAIGN_ID
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("authorized campaign directory is missing or invalid")
    return directory


def _provider_preflight() -> tuple[ProviderConfig, Pricing]:
    config = ProviderConfig.load()
    return config, fetch_pricing(config)


@app.command()
def doctor() -> None:
    """检查本机依赖；不连接供应商、不读取密钥。"""
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    emit(
        {
            "tools": tools,
            "model": MODEL,
            "model_account_availability": "unverified",
            "semantic_analysis": "implemented_explicit_upload_only",
            "asr_ocr": "not_separate_gemini_multimodal",
            "supplier_calls": 0,
        }
    )
    if not all(tools.values()):
        raise typer.Exit(1)


@app.command("acceptance")
def acceptance_command(
    data_dir: DataDir = DEFAULT_DATA,
    offline: Offline = False,
    allow_upload: AllowUpload = False,
    stream: Stream = False,
) -> None:
    """按固定顺序运行四样本、共享 5 美元预算的验收活动。"""
    ledger: AnalysisStore | None = None
    campaign_dir: Path | None = None
    samples: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    try:
        _require_upload(allow_upload)
        store = Store(data_dir)
        snapshot = sync_sources(store, offline=offline)
        typer.echo(
            f"数据源：{snapshot['status']}；提交 {snapshot['commit']}；目录 {snapshot['snapshot']}",
            err=True,
        )
        ledger = AnalysisStore(store)
        campaign_dir = ledger.campaign(CAMPAIGN_ID, CAMPAIGN_BUDGET, MODEL, ENDPOINT)
        samples = _ordered_samples(select_samples(store, snapshot, campaign_dir))
        config, pricing = _provider_preflight()
        config = replace(config, streaming=stream)
        report: dict[str, Any] | None = None
        for sample in samples:
            result = analyze_sample(
                ledger=ledger,
                campaign_id=CAMPAIGN_ID,
                sample=sample,
                config=config,
                pricing=pricing,
            )
            completed.append(_analysis_record(sample, result))
            report = _save_progress(ledger, campaign_dir, completed, samples)
        assert report is not None
        emit(report)
    except (OSError, ValueError, RuntimeError) as exc:
        _save_failure_progress(ledger, campaign_dir, completed, samples, exc)
        fail(exc)


@app.command("analyze")
def analyze_command(
    source_sha256: str,
    mode: Annotated[
        str | None,
        typer.Option("--mode", help="可选的一致性检查：full 或 opening；不改变样本模式"),
    ] = None,
    data_dir: DataDir = DEFAULT_DATA,
    offline: Offline = False,
    allow_upload: AllowUpload = False,
    stream: Stream = False,
    retry_unknown_call: Annotated[
        str | None,
        typer.Option(
            "--retry-unknown-call",
            help="保留指定未知请求为 abandoned 审计记录，并明确授权同阶段重新请求",
        ),
    ] = None,
) -> None:
    """只拆解固定验收活动中已选择且哈希匹配的一个样本。"""
    ledger: AnalysisStore | None = None
    campaign_dir: Path | None = None
    samples: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    try:
        _require_upload(allow_upload)
        if mode not in {None, "full", "opening"}:
            raise ValueError("--mode must be full or opening")
        store = Store(data_dir)
        ledger = AnalysisStore(store)
        campaign_dir = _existing_campaign(ledger)
        if not (campaign_dir / "samples.json").is_file():
            raise ValueError("authorized campaign has no existing selected-sample manifest")
        samples = _ordered_samples(select_samples(store, {}, campaign_dir))
        matches = [sample for sample in samples if sample.get("source_sha256") == source_sha256]
        if len(matches) != 1:
            raise ValueError("source hash is not one of the four authorized campaign samples")
        sample = matches[0]
        if mode is not None and sample["mode"] != mode:
            raise ValueError("--mode does not match the selected sample's fixed campaign mode")
        snapshot = sync_sources(store, offline=offline)
        typer.echo(
            f"数据源：{snapshot['status']}；提交 {snapshot['commit']}；目录 {snapshot['snapshot']}",
            err=True,
        )
        config, pricing = _provider_preflight()
        config = replace(config, streaming=stream)
        if retry_unknown_call is not None:
            ledger.abandon_unknown(
                retry_unknown_call,
                source_hash=source_sha256,
                reason=(
                    "user explicitly authorized forgetting the unresolved billing outcome "
                    "and retrying the same analysis stage"
                ),
            )
        result = analyze_sample(
            ledger=ledger,
            campaign_id=CAMPAIGN_ID,
            sample=sample,
            config=config,
            pricing=pricing,
        )
        record = _analysis_record(sample, result)
        completed.append(record)
        _save_progress(ledger, campaign_dir, completed, samples)
        emit(record)
    except (OSError, ValueError, RuntimeError) as exc:
        _save_failure_progress(ledger, campaign_dir, completed, samples, exc)
        fail(exc)


@app.command("prepare")
def prepare_command(
    source: Path, data_dir: DataDir = DEFAULT_DATA, offline: Offline = False
) -> None:
    """同步数据源后准备本地视频证据；不生成拆解结果。"""
    try:
        store = processing_store(data_dir, offline)
        identifier = store.create(source)
        typer.echo(f"任务：{identifier}", err=True)
        emit(store.run(identifier))
    except (OSError, ValueError, RuntimeError) as exc:
        fail(exc)


@app.command()
def status(
    task_id: Annotated[
        str | None,
        typer.Argument(help="本地准备任务 ID；省略时显示固定验收活动的账本摘要"),
    ] = None,
    data_dir: DataDir = DEFAULT_DATA,
) -> None:
    """读取本地准备任务状态，或读取固定验收活动的本地账本摘要。"""
    try:
        store = Store(data_dir)
        if task_id is not None and task_id != CAMPAIGN_ID:
            emit(store.status(task_id))
        else:
            emit(AnalysisStore(store).summary(CAMPAIGN_ID))
    except (OSError, ValueError) as exc:
        fail(exc)


@app.command()
def resume(task_id: str, data_dir: DataDir = DEFAULT_DATA, offline: Offline = False) -> None:
    """同步数据源后恢复准备任务；完整缓存校验后复用，无供应商调用。"""
    try:
        emit(processing_store(data_dir, offline).run(task_id))
    except (OSError, ValueError, RuntimeError) as exc:
        fail(exc)


@app.command("prepare-batch")
def prepare_batch(
    manifest: Path, data_dir: DataDir = DEFAULT_DATA, offline: Offline = False
) -> None:
    """同步一次数据源后按 JSONL 准备独立任务；单条失败不终止其他条目。"""
    try:
        entries = read_batch(manifest)
        if not entries:
            raise ValueError("batch contains no entries")
        store = processing_store(data_dir, offline)
        failures = 0
        for entry in entries:
            identifier = None
            try:
                if "error" in entry:
                    raise ValueError(entry["error"])
                identifier = store.create(entry["source"])
                result = store.run(identifier)
                emit({"line": entry["line"], "task": result})
            except (OSError, ValueError, RuntimeError) as exc:
                failures += 1
                emit({"line": entry["line"], "task_id": identifier, "error": str(exc)})
        if failures:
            raise typer.Exit(1)
    except (OSError, ValueError, RuntimeError) as exc:
        fail(exc)


@app.command("validate-evidence")
def validate_command(directory: Path) -> None:
    """校验准备清单、相对路径、时间与媒体哈希，不验收拆解内容。"""
    try:
        manifest = validate_evidence(directory)
        emit({"valid": True, "status": manifest.status, "source_id": manifest.source_id})
    except (OSError, ValueError) as exc:
        fail(exc)


@app.command("validate-package")
def validate_package_command(directory: Path) -> None:
    """离线校验拆解结果包的清单、哈希、业务结构和媒体引用。"""
    try:
        emit({"valid": True, "manifest": validate_package(directory)})
    except (OSError, ValueError) as exc:
        fail(exc)


@app.command("review-package")
def review_package_command(directory: Path) -> None:
    """离线校验结果包并输出按时间排序的待复核清单，不修改结果或账本。"""
    try:
        emit(review_package(directory))
    except (OSError, ValueError) as exc:
        fail(exc)


@app.command("export-evidence")
def export_command(
    task_id: str,
    output: Annotated[Path, typer.Option(help="新的本地目标目录，不覆盖已有目录")],
    allow_source_copy: Annotated[
        bool, typer.Option("--allow-source-copy", help="确认有权将原片复制到该交付目录")
    ] = False,
    data_dir: DataDir = DEFAULT_DATA,
) -> None:
    """复制本地证据目录；这不是整片/黄金开场的分析结果包。"""
    try:
        if not allow_source_copy:
            raise ValueError("export includes source video; --allow-source-copy is required")
        export_evidence(Store(data_dir), task_id, output)
        emit({"exported": str(output), "analysis_status": "not_implemented"})
    except (OSError, ValueError) as exc:
        fail(exc)


@app.command("sync-sources")
def sync_command(data_dir: DataDir = DEFAULT_DATA, offline: Offline = False) -> None:
    """同步三个远程 JSON，并构建跨数据源去重目录；不下载视频。"""
    try:
        emit(sync_sources(Store(data_dir), offline=offline))
    except (OSError, ValueError, RuntimeError) as exc:
        fail(exc)


if __name__ == "__main__":
    app()
