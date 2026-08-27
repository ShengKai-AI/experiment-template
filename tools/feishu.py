#!/usr/bin/env python3
"""通过 lark-cli 发送尽力而为的实验进度通知。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_CONFIG_PATH = Path("~/.config/experiment-template/feishu.json").expanduser()
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def format_beijing_time(value: str | None = None) -> str:
    """将通知时间统一显示为北京时间。"""

    if value is None:
        moment = datetime.now(BEIJING_TIMEZONE)
    else:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            moment = datetime.fromisoformat(normalized)
        except ValueError:
            return value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=BEIJING_TIMEZONE)
        else:
            moment = moment.astimezone(BEIJING_TIMEZONE)
    return moment.strftime("%Y-%m-%d %H:%M:%S 北京时间 (UTC+8)")


def environment_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def configured_chat_id() -> str:
    """读取目标群 ID；环境变量优先于服务器本地配置。"""

    environment_value = os.getenv("LARK_CHAT_ID", "").strip()
    if environment_value:
        return environment_value
    configured_path = os.getenv("FEISHU_NOTIFY_CONFIG", "").strip()
    path = Path(configured_path).expanduser() if configured_path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("无法读取飞书通知配置：%s", str(exc)[:500])
        return ""
    value = data.get("chat_id") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) else ""


def format_metric_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


STATUS_PRESENTATION = {
    "pending": ("⏳", "等待中"),
    "running": ("🔄", "运行中"),
    "completed": ("✅", "完成"),
    "failed": ("❌", "失败"),
    "aborted": ("⚠️", "已中止"),
}


def build_message(
    run_id: str,
    execution_id: str,
    stage: str,
    status: str,
    completed: int | None = None,
    total: int | None = None,
    metrics: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> str:
    """构造可直接在移动端阅读的飞书 Markdown 消息。"""

    icon, status_label = STATUS_PRESENTATION.get(status, ("ℹ️", status))
    subject = "Run" if stage == "run" else "Stage"
    lines = [
        f"## {icon} {subject} {status_label}",
        "",
        f"- **Run**：`{run_id}`",
        f"- **Execution**：`{execution_id}`",
        f"- **Stage**：`{stage}`",
        f"- **状态**：{status_label} (`{status}`)",
        f"- **时间**：`{format_beijing_time(occurred_at)}`",
    ]
    if completed is not None:
        progress = f"{completed}/{total}" if total is not None else str(completed)
        lines.append(f"- **进度**：`{progress}`")
    if metrics:
        lines.extend(["", "**指标**", ""])
        lines.extend(
            f"- `{name}`：`{format_metric_value(value)}`"
            for name, value in sorted(metrics.items())
        )
    return "\n".join(lines)


def build_idempotency_key(
    run_id: str,
    execution_id: str,
    stage: str,
    status: str,
    completed: int | None,
    total: int | None,
    metrics: dict[str, Any] | None,
) -> str:
    """生成稳定事件标识，避免任务重试产生重复消息。"""

    event = {
        "run_id": run_id,
        "execution_id": execution_id,
        "stage": stage,
        "status": status,
        "completed": completed,
        "total": total,
        "metrics": metrics or {},
    }
    canonical = json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _send_feishu_notification(
    *,
    run_id: str,
    execution_id: str,
    stage: str,
    status: str,
    completed: int | None = None,
    total: int | None = None,
    metrics: dict[str, Any] | None = None,
    occurred_at: str | None = None,
    simulate: bool | None = None,
    dry_run: bool = False,
    timeout: int = 15,
) -> dict[str, Any]:
    """执行一次通知发送并返回结构化结果。"""

    message = build_message(
        run_id=run_id,
        execution_id=execution_id,
        stage=stage,
        status=status,
        completed=completed,
        total=total,
        metrics=metrics,
        occurred_at=occurred_at,
    )
    idempotency_key = build_idempotency_key(
        run_id, execution_id, stage, status, completed, total, metrics
    )
    result: dict[str, Any] = {
        "ok": True,
        "attempted": False,
        "sent": False,
        "simulated": False,
        "dry_run": dry_run,
        "message_format": "markdown",
        "idempotency_key": idempotency_key,
        "message": message,
    }

    if simulate is None:
        simulate = environment_flag("FEISHU_NOTIFY_SIMULATE")
    if simulate:
        result["simulated"] = True
        return result

    chat_id = configured_chat_id()
    if not chat_id:
        result.update({
            "skipped": True,
            "reason": "未通过 LARK_CHAT_ID 或本地配置文件设置 chat_id",
        })
        return result

    executable = os.getenv("LARK_CLI", "lark-cli").strip() or "lark-cli"
    command = [
        executable,
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--chat-id",
        chat_id,
        "--markdown",
        message,
        "--idempotency-key",
        idempotency_key,
        "--format",
        "json",
    ]
    if dry_run:
        command.append("--dry-run")

    child_environment = os.environ.copy()
    child_environment.setdefault("LARKSUITE_CLI_NO_UPDATE_NOTIFIER", "1")
    child_environment.setdefault("LARKSUITE_CLI_NO_SKILLS_NOTIFIER", "1")
    child_environment.setdefault("LARK_CLI_NO_PROXY", "1")
    result["attempted"] = True
    try:
        completed_process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=child_environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = str(exc)[:500]
        logging.warning("飞书通知调用失败：%s", detail)
        result.update({"ok": False, "error": detail})
        return result

    stdout = completed_process.stdout.strip()
    stderr = completed_process.stderr.strip()[:500]
    try:
        response = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        response = None
    success = (
        completed_process.returncode == 0
        and isinstance(response, dict)
        and response.get("ok") is True
    )
    if not success:
        detail = stderr or stdout[:500] or f"lark-cli 退出码 {completed_process.returncode}"
        logging.warning("飞书通知发送失败：%s", detail)
        result.update({"ok": False, "error": detail})
        return result

    result["sent"] = not dry_run
    result["response"] = response
    return result


def send_feishu_notification(
    *,
    run_id: str,
    execution_id: str,
    stage: str,
    status: str,
    completed: int | None = None,
    total: int | None = None,
    metrics: dict[str, Any] | None = None,
    occurred_at: str | None = None,
    simulate: bool | None = None,
    dry_run: bool = False,
    timeout: int = 15,
) -> dict[str, Any]:
    """尽力发送通知；包括意外错误在内的失败都不会向调用方抛出。"""

    try:
        return _send_feishu_notification(
            run_id=run_id,
            execution_id=execution_id,
            stage=stage,
            status=status,
            completed=completed,
            total=total,
            metrics=metrics,
            occurred_at=occurred_at,
            simulate=simulate,
            dry_run=dry_run,
            timeout=timeout,
        )
    except Exception as exc:  # 通知属于旁路功能，不能影响实验主流程
        detail = str(exc)[:500]
        logging.warning("飞书通知发生意外错误：%s", detail)
        return {
            "ok": False,
            "attempted": False,
            "sent": False,
            "simulated": bool(simulate),
            "dry_run": dry_run,
            "error": detail,
        }


def parse_metric(values: list[str]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"指标必须使用 NAME=VALUE 格式：{value}")
        name, raw = value.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError("指标名称不能为空")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        metrics[name] = parsed
    return metrics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    send_parser = subparsers.add_parser("send", help="发送一条实验通知")
    test_parser = subparsers.add_parser("test", help="生成一条固定的测试通知")
    for target in (send_parser, test_parser):
        target.add_argument("--simulate", action="store_true", help="只生成消息，不调用 lark-cli")
        target.add_argument("--dry-run", action="store_true", help="调用 lark-cli 的预览模式")
    send_parser.add_argument("--run", required=True)
    send_parser.add_argument("--execution", required=True)
    send_parser.add_argument("--stage", required=True)
    send_parser.add_argument("--status", required=True)
    send_parser.add_argument("--completed", type=int)
    send_parser.add_argument("--total", type=int)
    send_parser.add_argument("--metric", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "test":
            payload = send_feishu_notification(
                run_id="simulated-run",
                execution_id="exec-001",
                stage="evaluate",
                status="completed",
                completed=4,
                total=4,
                metrics={"accuracy": 0.78, "loss": 0.42},
                simulate=arguments.simulate,
                dry_run=arguments.dry_run,
            )
        else:
            payload = send_feishu_notification(
                run_id=arguments.run,
                execution_id=arguments.execution,
                stage=arguments.stage,
                status=arguments.status,
                completed=arguments.completed,
                total=arguments.total,
                metrics=parse_metric(arguments.metric),
                simulate=arguments.simulate,
                dry_run=arguments.dry_run,
            )
    except (ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            raise
        payload = {"ok": False, "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
