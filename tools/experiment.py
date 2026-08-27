#!/usr/bin/env python3
"""实验配置与运行记录命令行工具。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 由 main 转换成结构化错误
    yaml = None


EXPERIMENT_TYPES = {"main", "baseline", "ablation"}
STATUSES = {"pending", "running", "completed", "failed", "aborted"}
TERMINAL_STATUSES = {"completed", "failed", "aborted"}
CATEGORY_TYPES = {"main": "main", "baselines": "baseline", "ablations": "ablation"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ToolError(Exception):
    def __init__(self, code: str, message: str, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path:
            result["path"] = self.path
        return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def canonical_sha256(data: Any) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_yaml(path: Path) -> Any:
    if yaml is None:
        raise ToolError("MISSING_DEPENDENCY", "缺少 PyYAML，请安装 requirements.txt 中的依赖")
    if not path.is_file():
        raise ToolError("FILE_NOT_FOUND", f"文件不存在：{path}", str(path))
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ToolError("INVALID_YAML", f"无法读取 YAML：{exc}", str(path)) from exc


def atomic_write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def validate_experiment(data: Any, source: Path | None = None, root: Path | None = None) -> tuple[list, list]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(data, dict):
        return [issue("INVALID_ROOT", "", "Experiment 文件根节点必须是映射")], warnings
    metadata = data.get("experiment")
    if not isinstance(metadata, dict):
        return [issue("MISSING_EXPERIMENT", "experiment", "缺少 Experiment 信息")], warnings

    for field, label in (("id", "ID"), ("name", "名称"), ("type", "类型"), ("version", "版本")):
        value = metadata.get(field)
        if value is None or value == "":
            errors.append(issue(f"MISSING_{field.upper()}", f"experiment.{field}", f"缺少 Experiment {label}"))

    experiment_type = metadata.get("type")
    if experiment_type is not None and experiment_type not in EXPERIMENT_TYPES:
        errors.append(issue("INVALID_TYPE", "experiment.type", "类型必须是 main、baseline 或 ablation"))
    experiment_id = metadata.get("id")
    if isinstance(experiment_id, str) and experiment_type in EXPERIMENT_TYPES:
        if not experiment_id.startswith(f"{experiment_type}/"):
            errors.append(issue("ID_TYPE_MISMATCH", "experiment.id", "Experiment ID 前缀必须与类型一致"))
    version = metadata.get("version")
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        errors.append(issue("INVALID_VERSION", "experiment.version", "版本必须是整数或非空字符串"))
    if not isinstance(data.get("spec"), dict):
        errors.append(issue("INVALID_SPEC", "spec", "spec 必须是映射，自由字段应放在其中"))

    stages = data.get("spec", {}).get("stages", []) if isinstance(data.get("spec"), dict) else []
    if stages is not None and not isinstance(stages, list):
        errors.append(issue("INVALID_STAGES", "spec.stages", "stages 必须是列表"))
    elif isinstance(stages, list):
        seen: set[str] = set()
        for index, stage in enumerate(stages):
            stage_path = f"spec.stages[{index}]"
            if not isinstance(stage, dict) or not isinstance(stage.get("id"), str) or not stage["id"]:
                errors.append(issue("INVALID_STAGE", stage_path, "每个 Stage 必须包含非空 id"))
                continue
            if stage["id"] in seen:
                errors.append(issue("DUPLICATE_STAGE_ID", f"{stage_path}.id", "Stage ID 不能重复"))
            seen.add(stage["id"])

    if source is not None and root is not None:
        try:
            relative = source.resolve().relative_to(root.resolve())
        except ValueError:
            relative = None
        if relative and len(relative.parts) >= 3 and relative.parts[0] == "experiments":
            expected = CATEGORY_TYPES.get(relative.parts[1])
            if expected and experiment_type != expected:
                errors.append(issue("DIRECTORY_TYPE_MISMATCH", "experiment.type", f"目录要求类型为 {expected}"))
        if relative == Path("experiments/example.yaml"):
            warnings.append(issue("EXAMPLE_CONFIG", "", "这是结构示例，不能用于创建真实 Run"))
    return errors, warnings


def validate_run(data: Any) -> tuple[list, list]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(data, dict) or not isinstance(data.get("run"), dict):
        return [issue("MISSING_RUN", "run", "缺少 Run 信息")], warnings
    run = data["run"]
    for field in ("id", "name", "status", "created_at", "git"):
        if run.get(field) is None:
            errors.append(issue(f"MISSING_{field.upper()}", f"run.{field}", f"缺少 Run 字段 {field}"))
    if run.get("status") not in STATUSES:
        errors.append(issue("INVALID_STATUS", "run.status", "Run 状态无效"))
    executions = data.get("executions")
    if not isinstance(executions, list) or not executions:
        errors.append(issue("INVALID_EXECUTIONS", "executions", "Run 至少需要一个 Execution"))
        return errors, warnings
    seen: set[str] = set()
    for index, execution in enumerate(executions):
        path = f"executions[{index}]"
        if not isinstance(execution, dict):
            errors.append(issue("INVALID_EXECUTION", path, "Execution 必须是映射"))
            continue
        execution_id = execution.get("id")
        if not isinstance(execution_id, str) or not execution_id:
            errors.append(issue("MISSING_EXECUTION_ID", f"{path}.id", "缺少 Execution ID"))
        elif execution_id in seen:
            errors.append(issue("DUPLICATE_EXECUTION_ID", f"{path}.id", "Execution ID 不能重复"))
        else:
            seen.add(execution_id)
        if execution.get("status") not in STATUSES:
            errors.append(issue("INVALID_STATUS", f"{path}.status", "Execution 状态无效"))
        snapshot_errors, _ = validate_experiment(execution.get("config_snapshot"))
        for entry in snapshot_errors:
            entry = dict(entry)
            entry["path"] = f"{path}.config_snapshot.{entry['path']}".rstrip(".")
            errors.append(entry)
    return errors, warnings


def path_for_output(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def command_validate(root: Path, path_text: str) -> tuple[dict[str, Any], int]:
    path = Path(path_text)
    if not path.is_absolute():
        path = root / path
    try:
        data = load_yaml(path)
    except ToolError as exc:
        return {"ok": False, "action": "validate", "kind": "unknown", "path": path_for_output(path, root), "errors": [exc.as_dict()], "warnings": []}, 1
    if isinstance(data, dict) and "experiment" in data:
        kind = "experiment"
        errors, warnings = validate_experiment(data, path, root)
    elif isinstance(data, dict) and "run" in data:
        kind = "run"
        errors, warnings = validate_run(data)
    else:
        kind = "unknown"
        errors, warnings = [issue("UNKNOWN_DOCUMENT", "", "无法识别 YAML 文档类型")], []
    result = {
        "ok": not errors,
        "action": "validate",
        "kind": kind,
        "path": path_for_output(path, root),
        "sha256": canonical_sha256(data),
        "errors": errors,
        "warnings": warnings,
    }
    return result, 0 if not errors else 1


def git_value(root: Path, *arguments: str) -> str | None:
    completed = subprocess.run(
        ["git", *arguments], cwd=root, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def git_snapshot(root: Path) -> dict[str, Any]:
    status = git_value(root, "status", "--porcelain")
    return {
        "commit": git_value(root, "rev-parse", "HEAD"),
        "branch": git_value(root, "branch", "--show-current"),
        "dirty": bool(status) if status is not None else None,
    }


def validate_identifier(value: str, label: str) -> None:
    if not SAFE_ID.fullmatch(value):
        raise ToolError("INVALID_ID", f"{label} 只能包含字母、数字、点、下划线和连字符")


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug or "run"


def resolve_experiment_path(root: Path, path_text: str) -> Path:
    path = Path(path_text)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def create_run(
    root: Path,
    name: str,
    experiment_paths: list[str],
    run_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    if not name.strip():
        raise ToolError("INVALID_NAME", "Run 名称不能为空", "run.name")
    if not experiment_paths:
        raise ToolError("MISSING_EXPERIMENT", "至少需要一个 Experiment 配置")
    timestamp = now or utc_now()
    if run_id is None:
        compact_time = timestamp.replace("-", "").replace(":", "")[:15].replace("T", "-")
        run_id = f"{compact_time}-{slugify(name)}"
    validate_identifier(run_id, "Run ID")

    run_directory = root / "runs" / run_id
    run_file = run_directory / "run.yaml"
    if run_directory.exists():
        raise ToolError("RUN_EXISTS", f"Run 已存在：{run_id}", path_for_output(run_directory, root))

    loaded: list[tuple[Path, dict[str, Any], str]] = []
    experiment_ids: set[str] = set()
    for path_text in experiment_paths:
        path = resolve_experiment_path(root, path_text)
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise ToolError("INVALID_EXPERIMENT_PATH", "Experiment 配置必须位于当前项目内", str(path))
        if not relative.parts or relative.parts[0] != "experiments":
            raise ToolError("INVALID_EXPERIMENT_PATH", "Experiment 配置必须位于 experiments/ 下", relative.as_posix())
        if relative == Path("experiments/example.yaml"):
            raise ToolError("EXAMPLE_CONFIG", "共享示例配置不能用于创建真实 Run", relative.as_posix())
        data = load_yaml(path)
        errors, _ = validate_experiment(data, path, root)
        if errors:
            raise ToolError("INVALID_EXPERIMENT", "Experiment 配置验证失败", path_for_output(path, root))
        experiment_id = data["experiment"]["id"]
        if experiment_id in experiment_ids:
            raise ToolError("DUPLICATE_EXPERIMENT_ID", f"Run 中出现重复 Experiment ID：{experiment_id}")
        experiment_ids.add(experiment_id)
        loaded.append((path, data, canonical_sha256(data)))

    executions: list[dict[str, Any]] = []
    for index, (path, config, digest) in enumerate(loaded, start=1):
        stages = []
        for stage_config in config.get("spec", {}).get("stages", []) or []:
            stages.append({
                "id": stage_config["id"],
                "name": stage_config.get("name", stage_config["id"]),
                "status": "pending",
                "started_at": None,
                "ended_at": None,
            })
        metadata = config["experiment"]
        executions.append({
            "id": f"exec-{index:03d}",
            "name": metadata["name"],
            "status": "pending",
            "started_at": None,
            "ended_at": None,
            "experiment": {
                "id": metadata["id"],
                "version": metadata["version"],
                "source": path_for_output(path, root),
                "sha256": digest,
            },
            "config_snapshot": config,
            "adjustments": [],
            "stages": stages,
        })

    document = {
        "run": {
            "id": run_id,
            "name": name.strip(),
            "status": "pending",
            "created_at": timestamp,
            "started_at": None,
            "ended_at": None,
            "git": git_snapshot(root),
        },
        "executions": executions,
    }
    try:
        atomic_write_yaml(run_file, document)
    except Exception:
        if run_directory.exists() and not any(run_directory.iterdir()):
            run_directory.rmdir()
        raise
    return {
        "ok": True,
        "action": "create_run",
        "run_id": run_id,
        "run_file": path_for_output(run_file, root),
        "execution_ids": [entry["id"] for entry in executions],
        "status": "pending",
        "created_at": timestamp,
    }


def load_run(root: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    validate_identifier(run_id, "Run ID")
    path = root / "runs" / run_id / "run.yaml"
    data = load_yaml(path)
    errors, _ = validate_run(data)
    if errors:
        raise ToolError("INVALID_RUN", "Run 记录验证失败", path_for_output(path, root))
    if data["run"]["id"] != run_id:
        raise ToolError("RUN_ID_MISMATCH", "目录名与 run.id 不一致", "run.id")
    return path, data


def find_by_id(entries: list[dict[str, Any]], entry_id: str, kind: str) -> dict[str, Any]:
    for entry in entries:
        if entry.get("id") == entry_id:
            return entry
    raise ToolError(f"{kind.upper()}_NOT_FOUND", f"找不到 {kind}：{entry_id}")


STAGE_TRANSITIONS = {
    "pending": {"running", "failed", "aborted"},
    "running": {"completed", "failed", "aborted"},
    "completed": set(),
    "failed": set(),
    "aborted": set(),
}


def update_stage(root: Path, run_id: str, execution_id: str, stage_id: str, status: str, now: str | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ToolError("INVALID_STATUS", f"无效 Stage 状态：{status}")
    path, document = load_run(root, run_id)
    run = document["run"]
    if run["status"] in TERMINAL_STATUSES:
        raise ToolError("RUN_TERMINAL", "终态 Run 不能再更新 Stage")
    execution = find_by_id(document["executions"], execution_id, "execution")
    stage = find_by_id(execution.get("stages", []), stage_id, "stage")
    previous = stage["status"]
    timestamp = now or utc_now()
    if status != previous and status not in STAGE_TRANSITIONS[previous]:
        raise ToolError("INVALID_TRANSITION", f"不允许从 {previous} 转换到 {status}", "stage.status")
    if status != previous:
        stage["status"] = status
        if status == "running":
            stage["started_at"] = stage.get("started_at") or timestamp
        if status in TERMINAL_STATUSES:
            stage["ended_at"] = timestamp

        stage_statuses = [entry["status"] for entry in execution.get("stages", [])]
        if "failed" in stage_statuses:
            execution_status = "failed"
        elif "aborted" in stage_statuses:
            execution_status = "aborted"
        elif stage_statuses and all(value == "completed" for value in stage_statuses):
            execution_status = "completed"
        elif any(value != "pending" for value in stage_statuses):
            execution_status = "running"
        else:
            execution_status = "pending"
        execution["status"] = execution_status
        if execution_status != "pending":
            execution["started_at"] = execution.get("started_at") or timestamp
        if execution_status in TERMINAL_STATUSES:
            execution["ended_at"] = timestamp
        if execution_status != "pending":
            run["status"] = "running"
            run["started_at"] = run.get("started_at") or timestamp
        atomic_write_yaml(path, document)
    return {
        "ok": True,
        "action": "update_stage",
        "run_id": run_id,
        "execution_id": execution_id,
        "stage_id": stage_id,
        "previous_status": previous,
        "status": status,
        "updated_at": timestamp,
        "run_file": path_for_output(path, root),
    }


def required_output_path(root: Path, value: str, run_id: str, execution_id: str) -> Path:
    expanded = value.replace("<run_id>", run_id).replace("<execution_id>", execution_id)
    path = Path(expanded)
    return path if path.is_absolute() else root / path


def completion_errors(root: Path, document: dict[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    run_id = document["run"]["id"]
    for execution_index, execution in enumerate(document["executions"]):
        prefix = f"executions[{execution_index}]"
        if execution.get("stages") and execution["status"] != "completed":
            errors.append(issue("EXECUTION_INCOMPLETE", f"{prefix}.status", f"Execution {execution['id']} 尚未完成"))
        completion = execution.get("config_snapshot", {}).get("spec", {}).get("completion", {})
        if not isinstance(completion, dict):
            continue
        stages = {entry["id"]: entry for entry in execution.get("stages", [])}
        for stage_id in completion.get("required_stages", []) or []:
            if stage_id not in stages or stages[stage_id].get("status") != "completed":
                errors.append(issue("REQUIRED_STAGE_INCOMPLETE", f"{prefix}.stages", f"必需 Stage 未完成：{stage_id}"))
        for output in completion.get("required_outputs", []) or []:
            if not isinstance(output, str):
                errors.append(issue("INVALID_REQUIRED_OUTPUT", f"{prefix}.config_snapshot.spec.completion.required_outputs", "必需产物路径必须是字符串"))
                continue
            resolved = required_output_path(root, output, run_id, execution["id"])
            if not resolved.exists():
                errors.append(issue("REQUIRED_OUTPUT_MISSING", path_for_output(resolved, root), f"必需产物不存在：{output}"))
    return errors


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def build_experiment_log(document: dict[str, Any]) -> str:
    run = document["run"]
    git = run.get("git", {})
    lines = [
        "---",
        "document_type: experiment_log",
        f"run_id: {run['id']}",
        f"status: {run['status']}",
        f"started_at: {run.get('started_at') or ''}",
        f"ended_at: {run.get('ended_at') or ''}",
        f"created_at: {run.get('created_at') or ''}",
        f"branch: {git.get('branch') or ''}",
        f"commit: {git.get('commit') or ''}",
        "---",
        "",
        f"# {run['name']}",
        "",
        "## 执行内容",
        "",
        "| Execution | Experiment | 类型 | 状态 |",
        "|---|---|---|---|",
    ]
    for execution in document["executions"]:
        metadata = execution["config_snapshot"]["experiment"]
        lines.append(
            f"| {markdown_cell(execution['id'])} | {markdown_cell(metadata['id'])}@{markdown_cell(metadata['version'])} "
            f"| {markdown_cell(metadata['type'])} | {markdown_cell(execution['status'])} |"
        )
    lines.extend([
        "",
        f"完整配置和阶段记录：`runs/{run['id']}/run.yaml`",
        "",
        "## 实际过程",
        "",
        "- 完成的主要阶段：",
        "- 重要参数调整：",
        "- 失败、重试或人工干预：",
        "- 与原计划的差异：",
        "",
        "## 结果",
        "",
        "| Execution | 指标 | 结果 |",
        "|---|---|---:|",
    ])
    metric_rows = 0
    for execution in document["executions"]:
        primary = execution.get("results", {}).get("primary_metric", {})
        if isinstance(primary, dict) and primary.get("name") is not None:
            lines.append(f"| {markdown_cell(execution['id'])} | {markdown_cell(primary.get('name'))} | {markdown_cell(primary.get('value'))} |")
            metric_rows += 1
    if not metric_rows:
        lines.append("| - | - | - |")
    lines.extend([
        "",
        "## 结论",
        "",
        "- 实验效果：",
        "- 对实验假设的支持情况：",
        "- 当前不能确定的结论：",
        "",
        "## 异常与限制",
        "",
        "- 异常：",
        "- 缺失结果：",
        "- 可比性限制：",
        "",
        "## 产物",
        "",
        "- 关键结果：",
        "- 模型或检查点：",
        "- 日志：",
        "",
    ])
    return "\n".join(lines)


def finish_run(root: Path, run_id: str, status: str, now: str | None = None) -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise ToolError("INVALID_STATUS", "结束 Run 时状态必须是 completed、failed 或 aborted")
    path, document = load_run(root, run_id)
    run = document["run"]
    if run["status"] in TERMINAL_STATUSES:
        if run["status"] != status:
            raise ToolError("RUN_TERMINAL", f"Run 已以 {run['status']} 结束")
        log_path = path.parent / "experiment-log.md"
        if not log_path.exists():
            atomic_write_text(log_path, build_experiment_log(document))
        return {
            "ok": True, "action": "finish_run", "run_id": run_id, "status": status,
            "ended_at": run.get("ended_at"), "run_file": path_for_output(path, root),
            "experiment_log": path_for_output(log_path, root), "idempotent": True,
        }
    if status == "completed":
        errors = completion_errors(root, document)
        if errors:
            raise ToolError("RUN_INCOMPLETE", f"Run 不满足完成条件，共 {len(errors)} 项；先运行 validate 查看记录并检查产物")

    timestamp = now or utc_now()
    if status == "completed":
        for execution in document["executions"]:
            if not execution.get("stages"):
                execution["status"] = "completed"
                execution["started_at"] = execution.get("started_at") or timestamp
                execution["ended_at"] = timestamp
    if status in {"failed", "aborted"}:
        for execution in document["executions"]:
            if execution["status"] not in TERMINAL_STATUSES:
                execution["status"] = status
                execution["started_at"] = execution.get("started_at") or timestamp
                execution["ended_at"] = timestamp
    run["status"] = status
    run["started_at"] = run.get("started_at") or timestamp
    run["ended_at"] = timestamp
    log_path = path.parent / "experiment-log.md"
    atomic_write_yaml(path, document)
    atomic_write_text(log_path, build_experiment_log(document))
    return {
        "ok": True,
        "action": "finish_run",
        "run_id": run_id,
        "status": status,
        "ended_at": timestamp,
        "run_file": path_for_output(path, root),
        "experiment_log": path_for_output(log_path, root),
    }


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ToolError("INVALID_ARGUMENTS", message)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="项目根目录")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="验证 Experiment 或 Run YAML")
    validate_parser.add_argument("path")
    create_parser = subparsers.add_parser("create-run", help="创建 Run 和配置快照")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--experiment", action="append", required=True)
    create_parser.add_argument("--run-id")
    update_parser = subparsers.add_parser("update-stage", help="更新 Stage 状态")
    update_parser.add_argument("--run", required=True)
    update_parser.add_argument("--execution", required=True)
    update_parser.add_argument("--stage", required=True)
    update_parser.add_argument("--status", required=True, choices=sorted(STATUSES))
    finish_parser = subparsers.add_parser("finish-run", help="结束 Run 并生成实验日志")
    finish_parser.add_argument("--run", required=True)
    finish_parser.add_argument("--status", required=True, choices=sorted(TERMINAL_STATUSES))
    return parser


def main(argv: list[str] | None = None) -> int:
    action = "parse"
    try:
        arguments = build_parser().parse_args(argv)
        action = arguments.command.replace("-", "_")
        root = arguments.root.resolve()
        if arguments.command == "validate":
            payload, exit_code = command_validate(root, arguments.path)
        elif arguments.command == "create-run":
            payload = create_run(root, arguments.name, arguments.experiment, arguments.run_id)
            exit_code = 0
        elif arguments.command == "update-stage":
            payload = update_stage(root, arguments.run, arguments.execution, arguments.stage, arguments.status)
            exit_code = 0
        elif arguments.command == "finish-run":
            payload = finish_run(root, arguments.run, arguments.status)
            exit_code = 0
        else:  # pragma: no cover
            raise ToolError("UNKNOWN_COMMAND", f"未知命令：{arguments.command}")
    except ToolError as exc:
        payload, exit_code = {"ok": False, "action": action, "errors": [exc.as_dict()], "warnings": []}, 1
    except Exception as exc:  # 保证脚本调用方始终得到 JSON
        payload, exit_code = {"ok": False, "action": action, "errors": [issue("INTERNAL_ERROR", "", str(exc))], "warnings": []}, 1
    json_output(payload)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
