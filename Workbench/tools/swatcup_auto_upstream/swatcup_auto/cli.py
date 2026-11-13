from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import load_config, make_default_config, save_config
from .errors import SwatCupAutoError
from .workflow import CalibrationWorkflow, check_project, load_state


def _print_event(message: str) -> None:
    print(f"[{message}]", flush=True)


def _command_make_config(args: argparse.Namespace) -> int:
    config = make_default_config(args.source, args.runs_root)
    save_config(config, args.output)
    print(f"已生成配置：{Path(args.output).resolve()}")
    return 0


def _command_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = check_project(config["source_project"], config)
    payload = {"calibration": result.to_dict()}
    validation = config.get("validation", {})
    if validation.get("enabled"):
        payload["validation"] = check_project(
            validation["source_project"], config
        ).to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    all_ok = result.ok and payload.get("validation", {"ok": True})["ok"]
    return 0 if all_ok else 2


def _command_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    workflow = CalibrationWorkflow(config, event_callback=_print_event)
    run_dir = workflow.run()
    print(f"RUN_DIR={run_dir}")
    return 0


def _command_resume(args: argparse.Namespace) -> int:
    workflow = CalibrationWorkflow.from_run_dir(args.run_dir, event_callback=_print_event)
    run_dir = workflow.run()
    print(f"RUN_DIR={run_dir}")
    return 0


def _command_status(args: argparse.Namespace) -> int:
    state = load_state(args.run_dir)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def _command_export_iterations(args: argparse.Namespace) -> int:
    workflow = CalibrationWorkflow.from_run_dir(
        args.run_dir, event_callback=_print_event
    )
    exported = workflow.export_swatcup_iterations()
    print(json.dumps({"iterations": [str(path) for path in exported]}, ensure_ascii=False, indent=2))
    return 0


def _command_batch(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    if not jobs:
        raise SwatCupAutoError("批量清单没有 jobs")
    failures = []
    for index, job in enumerate(jobs, start=1):
        if isinstance(job, str):
            config_value = job
            enabled = True
        else:
            config_value = job.get("config", "")
            enabled = job.get("enabled", True)
        if not enabled:
            print(f"[批量 {index}/{len(jobs)}] 已跳过", flush=True)
            continue
        config_path = Path(config_value)
        if not config_path.is_absolute():
            config_path = manifest_path.parent / config_path
        print(f"[批量 {index}/{len(jobs)}] {config_path}", flush=True)
        try:
            workflow = CalibrationWorkflow(
                load_config(config_path), event_callback=_print_event
            )
            workflow.run()
        except Exception as exc:
            failures.append({"config": str(config_path), "error": str(exc)})
            print(f"[批量失败] {exc}", file=sys.stderr, flush=True)
            if not data.get("continue_on_error", False):
                break
    if failures:
        print(json.dumps({"failures": failures}, ensure_ascii=False, indent=2))
        return 3
    print("批量任务全部完成")
    return 0


def _command_gui(_: argparse.Namespace) -> int:
    from .gui import launch_gui

    launch_gui()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swatcup-auto",
        description="SWAT-CUP 2012 / SUFI-2 自动多轮率定与验证工具",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    make_config = subparsers.add_parser("make-config", help="生成示例配置")
    make_config.add_argument("--source", required=True, help="SWAT-CUP SUFI-2 工程目录")
    make_config.add_argument("--runs-root", default="./runs", help="安全运行副本根目录")
    make_config.add_argument("--output", default="config.json", help="配置文件输出路径")
    make_config.set_defaults(handler=_command_make_config)

    check = subparsers.add_parser("check", help="只读检查工程和配置")
    check.add_argument("--config", required=True)
    check.set_defaults(handler=_command_check)

    run = subparsers.add_parser("run", help="创建安全副本并开始新任务")
    run.add_argument("--config", required=True)
    run.set_defaults(handler=_command_run)

    resume = subparsers.add_parser("resume", help="续跑中断或失败的任务")
    resume.add_argument("--run-dir", required=True)
    resume.set_defaults(handler=_command_resume)

    status = subparsers.add_parser("status", help="查看任务状态")
    status.add_argument("--run-dir", required=True)
    status.set_defaults(handler=_command_status)

    export_iterations = subparsers.add_parser(
        "export-iterations", help="把已有任务各轮写入 SWAT-CUP Iteration History"
    )
    export_iterations.add_argument("--run-dir", required=True)
    export_iterations.set_defaults(handler=_command_export_iterations)

    batch = subparsers.add_parser("batch", help="顺序运行多个配置")
    batch.add_argument("--manifest", required=True)
    batch.set_defaults(handler=_command_batch)

    gui = subparsers.add_parser("gui", help="启动 Windows 图形界面")
    gui.set_defaults(handler=_command_gui)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except SwatCupAutoError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("用户中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
