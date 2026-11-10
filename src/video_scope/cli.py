"""Offline evidence commands; semantic analysis is deliberately not exposed yet."""

import json
import shutil
from pathlib import Path
from typing import Annotated, Any

import typer

from video_scope.evidence import validate_evidence
from video_scope.jobs import Store, export_evidence, read_batch

app = typer.Typer(no_args_is_help=True, help="VideoScope 本地证据准备（尚未提供模型拆解）。")
DataDir = Annotated[
    Path,
    typer.Option("--data-dir", envvar="VIDEOSCOPE_DATA_DIR", help="代码目录外的运行数据目录"),
]
DEFAULT_DATA = Path.home() / ".local/share/video-scope"


def emit(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def fail(exc: Exception) -> None:
    typer.echo(f"错误：{exc}", err=True)
    raise typer.Exit(1) from exc


@app.command()
def doctor() -> None:
    """检查本机依赖；不连接供应商、不读取密钥。"""
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    emit(
        {
            "tools": tools,
            "model": "gemini-4-argon",
            "model_account_availability": "unverified",
            "semantic_analysis": "not_implemented",
            "asr_ocr": "not_implemented",
            "supplier_calls": 0,
        }
    )
    if not all(tools.values()):
        raise typer.Exit(1)


@app.command("prepare")
def prepare_command(source: Path, data_dir: DataDir = DEFAULT_DATA) -> None:
    """探测并复制本地视频，提取稀疏帧与音频；不生成拆解结果。"""
    try:
        store = Store(data_dir)
        identifier = store.create(source)
        typer.echo(f"任务：{identifier}", err=True)
        emit(store.run(identifier))
    except (OSError, ValueError, RuntimeError) as exc:
        fail(exc)


@app.command()
def status(task_id: str, data_dir: DataDir = DEFAULT_DATA) -> None:
    """读取本地准备任务状态。"""
    try:
        emit(Store(data_dir).status(task_id))
    except (OSError, ValueError) as exc:
        fail(exc)


@app.command()
def resume(task_id: str, data_dir: DataDir = DEFAULT_DATA) -> None:
    """显式恢复准备任务；完整缓存校验后复用，无供应商调用。"""
    try:
        emit(Store(data_dir).run(task_id))
    except (OSError, ValueError, RuntimeError) as exc:
        fail(exc)


@app.command("prepare-batch")
def prepare_batch(manifest: Path, data_dir: DataDir = DEFAULT_DATA) -> None:
    """按 JSONL 顺序准备独立任务；单条失败不终止其他条目。"""
    try:
        entries = read_batch(manifest)
        if not entries:
            raise ValueError("batch contains no entries")
        store = Store(data_dir)
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


if __name__ == "__main__":
    app()
