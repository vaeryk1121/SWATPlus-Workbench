from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .config import load_config, validate_config
from .errors import CancelledError, ParseError, ProjectValidationError
from .models import ParameterRange, ProjectCheck, RoundResult, SummaryResult
from .parsers import (
    apply_suggested_ranges,
    atomic_write_text,
    compare_target,
    parse_new_pars,
    parse_par_inf,
    parse_summary,
    ranges_to_map,
    select_target_value,
    set_simulation_count,
    set_swedit_range,
)
from .runner import list_processes, run_batch


REQUIRED_OUTPUTS = (
    "SUFI2.OUT/goal.txt",
    "SUFI2.OUT/new_pars.txt",
    "SUFI2.OUT/best_par.txt",
    "SUFI2.OUT/summary_stat.txt",
)


def _is_iteration_root_file(path: Path) -> bool:
    """Match the root files written by SWAT-CUP's Save Iteration command."""
    if not path.is_file():
        return False
    name = path.name.lower()
    return (
        name in {"file.cio", "absolute_swat_values.txt"}
        or name.endswith(".def")
        or (name.startswith("sufi2_") and name.endswith(".bat"))
    )


def _copy_iteration_snapshot(snapshot: Path, project: Path, target: Path) -> None:
    """Create a SWAT-CUP-compatible Iterations child from a round snapshot."""
    target.mkdir(parents=True, exist_ok=True)
    for directory in ("SUFI2.IN", "SUFI2.OUT", "Echo"):
        source = snapshot / directory
        if source.is_dir():
            shutil.copytree(source, target / directory, dirs_exist_ok=True)
    # General project definitions are stable across rounds. Copy them first,
    # then let round-specific files in the snapshot override them.
    for source_root in (project, snapshot):
        for source in source_root.iterdir():
            if _is_iteration_root_file(source):
                shutil.copy2(source, target / source.name)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_run_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return safe or "swatcup_calibration"


def _swatcup_project_copy_name(source: Path) -> str:
    """Return a folder name that SWAT-CUP accepts as an openable project."""
    name = source.name
    if name.lower().endswith(".swatcup"):
        return name
    return f"{_safe_run_name(name)}.Sufi2.SwatCup"


def _clean_config(config: dict) -> dict:
    return {key: value for key, value in config.items() if not key.startswith("_")}


def _file_signature(path: Path) -> Optional[Tuple[int, int]]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def check_project(project: Path | str, config: Optional[dict] = None) -> ProjectCheck:
    root = Path(project).resolve()
    result = ProjectCheck(root)
    if not root.is_dir():
        result.errors.append(f"工程目录不存在：{root}")
        return result
    execution = (config or {}).get("execution", {})
    required = [
        Path("SUFI2.IN") / "par_inf.txt",
        Path("SUFI2_swEdit.def"),
        Path(execution.get("pre_batch", "SUFI2_Pre.bat")),
        Path(execution.get("run_batch", "SUFI2_Run.bat")),
        Path(execution.get("post_batch", "SUFI2_Post.bat")),
    ]
    for relative in required:
        if not (root / relative).is_file():
            result.errors.append(f"缺少必需文件：{relative}")
    if not (root / "Backup").is_dir():
        result.errors.append("缺少 Backup 目录；SUFI-2 无法安全恢复原始 SWAT 输入")
    if not (root / "SUFI2.OUT").is_dir():
        result.warnings.append("缺少 SUFI2.OUT 目录；程序会在运行前创建")
    if any(ord(char) > 127 for char in str(root)):
        result.warnings.append(
            "工程路径包含非 ASCII 字符；旧版 SWAT-CUP 若报路径错误，请把 runs_root 改到纯英文路径"
        )
    project_process_names = {
        name.lower()
        for name in ("Swat_Edit.exe", "SUFI2_execute.exe")
        if (root / name).is_file()
    }
    if project_process_names:
        conflicts = [
            f"{name} (PID {pid})"
            for pid, name in list_processes()
            if name.lower() in project_process_names
        ]
        if conflicts:
            result.errors.append(
                "检测到可能造成单实例锁的遗留进程，请先在任务管理器中关闭："
                + ", ".join(conflicts)
            )
    par_path = root / "SUFI2.IN" / "par_inf.txt"
    if par_path.is_file():
        try:
            document = parse_par_inf(par_path)
            result.parameter_count = document.parameter_count
            result.simulation_count = document.simulation_count
            result.parameters = document.parameters
        except ParseError as exc:
            result.errors.append(str(exc))
    return result


class CalibrationWorkflow:
    def __init__(
        self,
        config: dict,
        run_dir: Optional[Path | str] = None,
        event_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        validate_config(config)
        self.config = config
        self.run_dir = Path(run_dir).resolve() if run_dir else None
        self.event_callback = event_callback
        self.cancel_event = cancel_event or threading.Event()
        self.logger = logging.getLogger(f"swatcup_auto.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.state: dict = {}

    @classmethod
    def from_run_dir(
        cls,
        run_dir: Path | str,
        event_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> "CalibrationWorkflow":
        root = Path(run_dir).resolve()
        config_path = root / "config.resolved.json"
        if not config_path.is_file():
            raise ProjectValidationError(f"运行目录缺少 config.resolved.json：{root}")
        config = load_config(config_path)
        return cls(config, root, event_callback, cancel_event)

    def emit(self, message: str) -> None:
        self.logger.info(message)
        if self.event_callback:
            self.event_callback(message)

    def _setup_logging(self) -> None:
        assert self.run_dir is not None
        if self.logger.handlers:
            return
        log_dir = self.run_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "workflow.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)

    def _close_logging(self) -> None:
        for handler in list(self.logger.handlers):
            try:
                handler.flush()
                handler.close()
            finally:
                self.logger.removeHandler(handler)

    def _write_json(self, path: Path, payload: dict) -> None:
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            "utf-8",
        )

    def _save_state(self) -> None:
        assert self.run_dir is not None
        self.state["updated_at"] = _now()
        self._write_json(self.run_dir / "state.json", self.state)

    def _load_state(self) -> None:
        assert self.run_dir is not None
        path = self.run_dir / "state.json"
        if not path.is_file():
            raise ProjectValidationError(f"运行目录缺少 state.json：{self.run_dir}")
        self.state = json.loads(path.read_text(encoding="utf-8-sig"))

    def _calibration_project(self) -> Path:
        """Resolve the copied calibration project, including legacy task layouts."""
        assert self.run_dir is not None
        relative_name = self.state.get("project_dir_name")
        if relative_name:
            candidate = self.run_dir / str(relative_name)
            if candidate.is_dir():
                return candidate
        saved = self.state.get("project")
        if saved:
            candidate = Path(saved)
            if candidate.is_dir():
                return candidate
        legacy = self.run_dir / "project"
        if legacy.is_dir():
            return legacy
        candidates = [
            path
            for path in self.run_dir.iterdir()
            if path.is_dir() and path.name.lower().endswith(".swatcup")
        ]
        if len(candidates) == 1:
            return candidates[0]
        raise ProjectValidationError(
            f"运行目录中找不到率定工程副本：{self.run_dir}"
        )

    def _swatcup_iteration_name(
        self, round_number: int, simulation_count: int
    ) -> str:
        assert self.run_dir is not None
        match = re.search(r"(\d{8}_\d{6}(?:_\d+)?)$", self.run_dir.name)
        task_tag = match.group(1) if match else _safe_run_name(self.run_dir.name)[-24:]
        return f"Auto_{task_tag}_R{round_number:03d}_N{simulation_count}"

    def _simulation_count_for_round(
        self, round_number: int, fallback: int
    ) -> int:
        schedule = self.config.get("simulation_schedule") or []
        if schedule:
            index = min(round_number - 1, len(schedule) - 1)
            return int(schedule[index])
        configured = self.config.get("simulation_count")
        return int(configured if configured is not None else fallback)

    def _prepare_round_simulation_count(
        self, project: Path, round_number: int
    ) -> int:
        par_path = project / "SUFI2.IN" / "par_inf.txt"
        document = parse_par_inf(par_path)
        simulation_count = self._simulation_count_for_round(
            round_number, document.simulation_count
        )
        if document.simulation_count != simulation_count:
            set_simulation_count(par_path, simulation_count)
        set_swedit_range(project / "SUFI2_swEdit.def", 1, simulation_count)
        self.state["current_simulation_count"] = simulation_count
        self.emit(f"第 {round_number} 轮计划模拟次数：{simulation_count}")
        return simulation_count

    def _write_swatcup_iteration(
        self, project: Path, round_dir: Path, result: RoundResult
    ) -> Path:
        info = result.swatcup_iteration
        name = info.get("name") or self._swatcup_iteration_name(
            result.round_number, result.summary.simulation_count
        )
        target = project / "Iterations" / name
        _copy_iteration_snapshot(round_dir, project, target)
        result.swatcup_iteration = {"name": name, "path": str(target)}
        self.emit(f"已写入 SWAT-CUP Iteration History：{name}")
        return target

    def export_swatcup_iterations(self) -> List[Path]:
        """Backfill SWAT-CUP Iteration History for an existing task."""
        assert self.run_dir is not None
        if not self.state:
            self._load_state()
        project = self._calibration_project()
        exported: List[Path] = []
        changed = False
        for payload in self.state.get("rounds", []):
            round_number = int(payload["round_number"])
            summary = payload.get("summary", {})
            simulation_count = int(summary.get("simulation_count", self.state["simulation_count"]))
            existing = payload.get("swatcup_iteration") or {}
            name = existing.get("name") or self._swatcup_iteration_name(
                round_number, simulation_count
            )
            round_dir = self.run_dir / "history" / f"round_{round_number:03d}"
            if not round_dir.is_dir():
                raise ProjectValidationError(f"缺少第 {round_number} 轮历史目录：{round_dir}")
            target = project / "Iterations" / name
            _copy_iteration_snapshot(round_dir, project, target)
            info = {"name": name, "path": str(target)}
            if payload.get("swatcup_iteration") != info:
                payload["swatcup_iteration"] = info
                changed = True
            result_path = round_dir / "result.json"
            if result_path.is_file():
                result_payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
                if result_payload.get("swatcup_iteration") != info:
                    result_payload["swatcup_iteration"] = info
                    self._write_json(result_path, result_payload)
            exported.append(target)
            self.emit(f"已写入 SWAT-CUP Iteration History：{name}")
        if changed:
            self._save_state()
        return exported

    def _create_run(self) -> Path:
        source = Path(self.config["source_project"]).resolve()
        check = check_project(source, self.config)
        if not check.ok:
            raise ProjectValidationError("；".join(check.errors))
        runs_root = Path(self.config["runs_root"]).resolve()
        if source == runs_root or source in runs_root.parents:
            raise ProjectValidationError(
                "runs_root 不能位于源工程内部，否则复制工程时会递归包含运行目录"
            )
        runs_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"{_safe_run_name(self.config.get('run_name', ''))}_{stamp}"
        run_dir = runs_root / base_name
        suffix = 1
        while run_dir.exists():
            suffix += 1
            run_dir = runs_root / f"{base_name}_{suffix}"
        run_dir.mkdir(parents=False)
        self.run_dir = run_dir
        self._setup_logging()
        for warning in check.warnings:
            self.emit(f"检查警告：{warning}")
        project = run_dir / _swatcup_project_copy_name(source)
        self.emit(f"复制源工程到可由 SWAT-CUP 打开的安全副本：{project}")
        shutil.copytree(source, project)
        self._write_json(run_dir / "config.resolved.json", _clean_config(self.config))
        (project / "SUFI2.OUT").mkdir(exist_ok=True)
        document = parse_par_inf(project / "SUFI2.IN" / "par_inf.txt")
        simulation_count = self._simulation_count_for_round(
            1, document.simulation_count
        )
        set_simulation_count(project / "SUFI2.IN" / "par_inf.txt", simulation_count)
        set_swedit_range(project / "SUFI2_swEdit.def", 1, simulation_count)
        document = parse_par_inf(project / "SUFI2.IN" / "par_inf.txt")
        self.state = {
            "schema_version": 1,
            "status": "ready",
            "phase": "created",
            "created_at": _now(),
            "updated_at": _now(),
            "source_project": str(source),
            "project": str(project),
            "project_dir_name": project.name,
            "completed_rounds": 0,
            "rounds": [],
            "initial_bounds": {
                item.name: [item.minimum, item.maximum] for item in document.parameters
            },
            "simulation_count": simulation_count,
            "simulation_schedule": self.config.get("simulation_schedule"),
            "validation": None,
        }
        self._save_state()
        return run_dir

    def _verify_fresh_outputs(
        self, project: Path, before: Dict[str, Optional[Tuple[int, int]]]
    ) -> None:
        missing: List[str] = []
        stale: List[str] = []
        for relative in REQUIRED_OUTPUTS:
            path = project / Path(relative)
            after = _file_signature(path)
            if after is None:
                missing.append(relative)
            elif before.get(relative) == after:
                stale.append(relative)
        if missing:
            raise ProjectValidationError("Post 完成后缺少输出：" + ", ".join(missing))
        if stale and self.config["execution"].get("require_fresh_outputs", True):
            raise ProjectValidationError("Post 未刷新以下输出：" + ", ".join(stale))

    def _run_stages(self, project: Path, logs_dir: Path, label: str) -> None:
        execution = self.config["execution"]
        timeout = int(execution.get("timeout_seconds", 0) or 0)
        before = {relative: _file_signature(project / Path(relative)) for relative in REQUIRED_OUTPUTS}
        stages = (
            ("pre", execution["pre_batch"]),
            ("run", execution["run_batch"]),
            ("post", execution["post_batch"]),
        )
        for stage, batch in stages:
            if self.cancel_event.is_set():
                raise CancelledError("用户取消运行")
            self.state["phase"] = f"{label}_{stage}"
            self._save_state()
            run_batch(
                project,
                batch,
                logs_dir / f"{stage}.log",
                timeout_seconds=timeout,
                cancel_event=self.cancel_event,
                event_callback=self.emit,
            )
        self._verify_fresh_outputs(project, before)

    def _snapshot_round(self, project: Path, round_dir: Path, result: RoundResult) -> None:
        round_dir.mkdir(parents=True, exist_ok=True)
        for directory in ("SUFI2.IN", "SUFI2.OUT", "Echo"):
            source = project / directory
            if source.is_dir():
                shutil.copytree(source, round_dir / directory, dirs_exist_ok=True)
        for filename in ("SUFI2_swEdit.def", "Swat_EditLog.txt", "model.in"):
            source = project / filename
            if source.is_file():
                shutil.copy2(source, round_dir / filename)
        self._write_json(round_dir / "result.json", result.to_dict())

    def _stop_decision(self, round_number: int, summary: SummaryResult) -> Tuple[bool, str]:
        stop = self.config["stop"]
        mode = stop["mode"]
        if mode == "fixed":
            requested = int(stop["rounds"])
            if round_number >= requested:
                return True, f"已完成固定轮数 {requested}"
            return False, f"固定轮数模式：{round_number}/{requested}"
        target = stop["target"]
        actual, label = select_target_value(summary, target)
        operator = str(target["operator"])
        expected = float(target["value"])
        if compare_target(actual, operator, expected):
            return True, f"目标已达到：{label}={actual:.10g} {operator} {expected:.10g}"
        maximum = int(stop["max_rounds"])
        if round_number >= maximum:
            return True, (
                f"目标未达到，但已到最大轮数 {maximum}："
                f"{label}={actual:.10g}，要求 {operator} {expected:.10g}"
            )
        return False, (
            f"目标尚未达到：{label}={actual:.10g}，要求 {operator} {expected:.10g}；"
            f"继续下一轮（最多 {maximum} 轮）"
        )

    def _append_history_csv(self, result: RoundResult) -> None:
        assert self.run_dir is not None
        path = self.run_dir / "history" / "rounds.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        with path.open("a", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            if not exists:
                writer.writerow(
                    [
                        "round",
                        "started_at",
                        "finished_at",
                        "goal_type",
                        "best_goal",
                        "best_simulation",
                        "simulation_count",
                        "stop_reached",
                        "stop_reason",
                    ]
                )
            writer.writerow(
                [
                    result.round_number,
                    result.started_at,
                    result.finished_at,
                    result.summary.goal_type,
                    f"{result.summary.best_goal:.12g}",
                    result.summary.best_simulation,
                    result.summary.simulation_count,
                    result.stop_reached,
                    result.stop_reason,
                ]
            )

    def _apply_next_ranges(
        self, project: Path, suggestions: Iterable[ParameterRange]
    ) -> List[ParameterRange]:
        settings = self.config["parameters"]
        if not settings.get("update_from_new_pars", True):
            self.emit("配置为不自动更新参数范围；下一轮沿用当前范围")
            return parse_par_inf(project / "SUFI2.IN" / "par_inf.txt").parameters
        initial_bounds = {
            name: (float(bounds[0]), float(bounds[1]))
            for name, bounds in self.state["initial_bounds"].items()
        }
        applied, warnings = apply_suggested_ranges(
            project / "SUFI2.IN" / "par_inf.txt",
            suggestions,
            initial_bounds=initial_bounds,
            limits=settings.get("limits", {}),
            lock_to_initial_bounds=bool(settings.get("lock_to_initial_bounds", True)),
            minimum_width=float(settings.get("minimum_width", 1e-12)),
            on_invalid_range=settings.get("on_invalid_range", "stop"),
        )
        for warning in warnings:
            self.emit("参数安全警告：" + warning)
        self.emit(f"已安全导入 {len(applied)} 个参数的下一轮建议范围")
        return applied

    def _run_calibration(self) -> None:
        assert self.run_dir is not None
        project = self._calibration_project()
        check = check_project(project, self.config)
        if not check.ok:
            raise ProjectValidationError("；".join(check.errors))
        self.state["status"] = "running"
        self.state.pop("last_error", None)
        self._save_state()
        if self.state.get("next_ranges_pending"):
            previous = self.state.get("rounds", [])[-1]
            suggestions = [
                ParameterRange(
                    item["name"], float(item["minimum"]), float(item["maximum"])
                )
                for item in previous.get("suggested_ranges", [])
            ]
            self.emit("恢复上次中断：先应用已完成轮次的建议参数范围")
            self._apply_next_ranges(project, suggestions)
            self.state["next_ranges_pending"] = False
            self.state["phase"] = f"round_{int(self.state['completed_rounds']) + 1}_ready"
            self._save_state()
        while True:
            round_number = int(self.state.get("completed_rounds", 0)) + 1
            planned_simulation_count = self._prepare_round_simulation_count(
                project, round_number
            )
            started_at = _now()
            round_dir = self.run_dir / "history" / f"round_{round_number:03d}"
            round_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                project / "SUFI2.IN" / "par_inf.txt", round_dir / "par_inf.before.txt"
            )
            shutil.copy2(project / "SUFI2_swEdit.def", round_dir / "SUFI2_swEdit.before.def")
            parameters_before = parse_par_inf(
                project / "SUFI2.IN" / "par_inf.txt"
            ).parameters
            self.emit(f"开始第 {round_number} 轮率定")
            self.state["active_round"] = round_number
            self._run_stages(project, round_dir / "logs", f"round_{round_number}")
            summary = parse_summary(project / "SUFI2.OUT" / "summary_stat.txt")
            if summary.simulation_count != planned_simulation_count:
                raise ProjectValidationError(
                    "Post 汇总的模拟次数与本轮计划不一致："
                    f"计划 {planned_simulation_count}，summary_stat.txt 为 "
                    f"{summary.simulation_count}"
                )
            suggestions = parse_new_pars(project / "SUFI2.OUT" / "new_pars.txt")
            stop_reached, stop_reason = self._stop_decision(round_number, summary)
            result = RoundResult(
                round_number=round_number,
                started_at=started_at,
                finished_at=_now(),
                summary=summary,
                parameters_before=parameters_before,
                suggested_ranges=suggestions,
                stop_reached=stop_reached,
                stop_reason=stop_reason,
                swatcup_iteration={
                    "name": self._swatcup_iteration_name(
                        round_number, summary.simulation_count
                    ),
                    "path": str(
                        project
                        / "Iterations"
                        / self._swatcup_iteration_name(
                            round_number, summary.simulation_count
                        )
                    ),
                },
            )
            self._snapshot_round(project, round_dir, result)
            self._write_swatcup_iteration(project, round_dir, result)
            self._append_history_csv(result)
            self.state["completed_rounds"] = round_number
            self.state["rounds"].append(result.to_dict())
            self.state["next_ranges_pending"] = not stop_reached
            self.state["phase"] = f"round_{round_number}_complete"
            self.state["active_round"] = None
            self._save_state()
            self.emit(
                f"第 {round_number} 轮完成：best_goal={summary.best_goal:.10g}；{stop_reason}"
            )
            if stop_reached:
                break
            self._apply_next_ranges(project, suggestions)
            self.state["next_ranges_pending"] = False
            self.state["phase"] = f"round_{round_number + 1}_ready"
            self._save_state()
        self.state["status"] = "calibration_complete"
        self._save_state()

    def _import_calibrated_ranges(self, calibration_project: Path, validation_project: Path) -> None:
        calibration = parse_par_inf(calibration_project / "SUFI2.IN" / "par_inf.txt")
        validation = parse_par_inf(validation_project / "SUFI2.IN" / "par_inf.txt")
        calibration_map = {item.name.upper(): item for item in calibration.parameters}
        suggestions: List[ParameterRange] = []
        missing: List[str] = []
        for parameter in validation.parameters:
            matched = calibration_map.get(parameter.name.upper())
            if matched is None:
                missing.append(parameter.name)
            else:
                suggestions.append(
                    ParameterRange(parameter.name, matched.minimum, matched.maximum)
                )
        if missing:
            raise ProjectValidationError(
                "验证工程包含未在最终率定范围中找到的参数：" + ", ".join(missing)
            )
        applied, _ = apply_suggested_ranges(
            validation_project / "SUFI2.IN" / "par_inf.txt",
            suggestions,
            initial_bounds=ranges_to_map(validation.parameters),
            lock_to_initial_bounds=False,
            minimum_width=float(self.config["parameters"].get("minimum_width", 1e-12)),
            on_invalid_range="stop",
        )
        self.emit(f"验证工程已导入 {len(applied)} 个最终率定参数范围（不再收缩）")

    def _run_validation(self) -> None:
        assert self.run_dir is not None
        settings = self.config.get("validation", {})
        if not settings.get("enabled"):
            return
        source = Path(settings["source_project"]).resolve()
        check = check_project(source, self.config)
        if not check.ok:
            raise ProjectValidationError("验证工程检查失败：" + "；".join(check.errors))
        validation_root = self.run_dir / "validation"
        project = validation_root / _swatcup_project_copy_name(source)
        legacy_project = validation_root / "project"
        if legacy_project.is_dir() and not project.exists():
            project = legacy_project
        if not project.exists():
            validation_root.mkdir(parents=True, exist_ok=True)
            self.emit(f"复制验证期工程到安全副本：{project}")
            shutil.copytree(source, project)
        self.state["status"] = "validating"
        self.state["phase"] = "validation_prepare"
        self._save_state()
        self._import_calibrated_ranges(self._calibration_project(), project)
        completed_rounds = self.state.get("rounds", [])
        calibration_count = int(
            completed_rounds[-1]["summary"]["simulation_count"]
            if completed_rounds
            else self.state["simulation_count"]
        )
        simulation_count = int(settings.get("simulation_count") or calibration_count)
        set_simulation_count(project / "SUFI2.IN" / "par_inf.txt", simulation_count)
        set_swedit_range(project / "SUFI2_swEdit.def", 1, simulation_count)
        self.emit(
            "开始验证期单轮模拟；请确认验证工程的 observed、提取定义、file.cio 和气象数据已预先设置为验证期"
        )
        self._run_stages(project, validation_root / "logs", "validation")
        summary = parse_summary(project / "SUFI2.OUT" / "summary_stat.txt")
        for directory in ("SUFI2.IN", "SUFI2.OUT", "Echo"):
            source_dir = project / directory
            if source_dir.is_dir():
                shutil.copytree(
                    source_dir, validation_root / "result_snapshot" / directory, dirs_exist_ok=True
                )
        payload = {
            "completed_at": _now(),
            "simulation_count": simulation_count,
            "summary": summary.to_dict(),
            "source_project": str(source),
            "project": str(project),
        }
        self._write_json(validation_root / "result.json", payload)
        self.state["validation"] = payload
        self.emit(f"验证期完成：best_goal={summary.best_goal:.10g}")
        self._save_state()

    def run(self) -> Path:
        if self.run_dir is None:
            self._create_run()
        else:
            self._setup_logging()
            self._load_state()
            if self.state.get("status") == "complete":
                self.emit("该任务已经完成，无需续跑")
                self._close_logging()
                return self.run_dir
            self.emit(
                f"从运行目录续跑：已完成 {self.state.get('completed_rounds', 0)} 轮"
            )
        assert self.run_dir is not None
        try:
            if self.state.get("status") not in {"calibration_complete", "validating"}:
                self._run_calibration()
            self._run_validation()
            self.state["status"] = "complete"
            self.state["phase"] = "complete"
            self.state["completed_at"] = _now()
            self._save_state()
            self.emit(f"任务全部完成：{self.run_dir}")
            return self.run_dir
        except CancelledError as exc:
            self.state["status"] = "cancelled"
            self.state["last_error"] = str(exc)
            self._save_state()
            self.emit(str(exc))
            raise
        except Exception as exc:
            self.state["status"] = "failed"
            self.state["last_error"] = f"{type(exc).__name__}: {exc}"
            self._save_state()
            self.logger.exception("任务失败")
            self.emit(f"任务失败：{exc}")
            raise
        finally:
            self._close_logging()


def load_state(run_dir: Path | str) -> dict:
    path = Path(run_dir).resolve() / "state.json"
    if not path.is_file():
        raise ProjectValidationError(f"找不到 state.json：{path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))
