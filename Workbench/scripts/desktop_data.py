"""Read small workflow metadata for the desktop UI, without importing the model.

All functions are read-only and use only the standard library. Missing or changing
JSON produces an explicit ``error`` and unknown values, never successful results.
Trial counts refer to parameter-search trials, excluding baseline/validation runs.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path


STATUS_LABELS = {
    "ready": "建模完成，待Toolbox", "created": "已创建", "running": "运行中", "completed": "已完成",
    "paused": "已暂停", "interrupted": "已停止", "failed": "运行失败",
    "pending": "等待运行", "unknown": "状态未知", "unreadable": "暂无法读取",
}
PHASE_LABELS = {
    "toolbox_ready": "交给Toolbox", "baseline": "基础试跑", "created": "准备任务", "build": "自动建模", "calibration": "参数搜索",
    "fixed_parameter_validation": "固定参数验证", "report": "生成报告",
    "completed": "流程完成", "unknown": "尚无阶段信息",
    "sensitivity": "敏感性分析", "uncertainty": "不确定性分析",
    "sufi2_review": "SUFI-2轮次审阅",
}
FREQUENCY_LABELS = {"daily": "逐日", "monthly": "逐月"}
METHOD_LABELS = {
    "latin_hypercube_then_local": "LHS + 局部采样",
    "spotpy_dds": "DDS（SPOTPY）",
    "spotpy_sceua": "SCE-UA（SPOTPY）", "glue_lhs": "GLUE · LHS行为集合",
    "spotpy_dream": "DREAM（SPOTPY）",
    "swatcup_sufi2": "SUFI-2（经典原统计程序 / SWAT+适配）",
    "pso": "PSO（手写粒子群）", "spe_sufi2": "SPE（SUFI-2系逐轮收缩）",
}
PARAMETER_BACKEND_LABELS = {
    "direct_file": "直接修改输入文件",
    "native_calibration": "原生 calibration.cal",
}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _read_json(path):
    """Retry once for an in-progress atomic replacement; do not block the UI."""
    error = None
    for _ in range(2):
        try:
            with Path(path).open(encoding="utf-8-sig") as stream:
                value = json.load(stream)
            if not isinstance(value, dict):
                return {}, "JSON顶层内容不是对象"
            return value, None
        except (OSError, ValueError, UnicodeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
    return {}, error


def load_config_summary(config_path):
    """Summarize a configuration. This is a preview, not model-input validation.

    ``calibration`` and ``validation`` contain start/end date strings (or None).
    ``error`` is None on a readable JSON object. Unspecified counts stay unknown;
    workers/objective use the same documented defaults as the workflow backend.
    """
    path = Path(config_path).resolve()
    cfg, error = _read_json(path)
    if error is None and cfg.get("runtime_config") and path.name != "config.resolved.json":
        try:
            from workflow_runtime import resolve_runtime_config
            from input_paths import config_root
            root=config_root(path,cfg) if cfg.get('workspace_root') else Path(__file__).resolve().parents[1]
            cfg = resolve_runtime_config(cfg, root, check_paths=False)
        except (OSError, ValueError, KeyError) as exc:
            error = f"运行环境配置无法读取：{exc}"
    from workflow_runtime import runtime_caption
    search = _mapping(cfg.get("search"))
    periods = _mapping(cfg.get("periods"))
    observations=cfg.get("observations")
    observation = _mapping(observations[0] if isinstance(observations,list) and observations else cfg.get("observation"))
    build = _mapping(cfg.get("build"))
    calibration = _mapping(periods.get("calibration"))
    validation = _mapping(periods.get("validation"))
    schedule = search.get("simulation_schedule")
    valid_schedule = isinstance(schedule, list) and bool(schedule) and all(
        type(n) is int and n > 0 for n in schedule
    )
    params = cfg.get("parameters")
    method = search.get("method", "latin_hypercube_then_local")
    parameter_backend = cfg.get("parameter_backend", "direct_file")
    if method == "swatcup_sufi2":
        options = _mapping(search.get("options"))
        iterations = _count(options.get("iterations",2))
        per_iteration = _count(options.get("samples_per_iteration",12))
        round_sizes=options.get("round_sizes")
        if round_sizes is not None:
            trials_total=sum(round_sizes) if isinstance(round_sizes,list) and round_sizes and all(type(n) is int and n>0 for n in round_sizes) else None
        else:trials_total = iterations * per_iteration if iterations and per_iteration else None
    elif method in {"spotpy_dds","spotpy_sceua","glue_lhs","spotpy_dream","pso","spe_sufi2"}:
        repetitions = _count(search.get("repetitions"))
        trials_total = repetitions if repetitions else None
    elif method == "latin_hypercube_then_local":
        trials_total = sum(schedule) if valid_schedule else None
    else:
        trials_total = None
    toolbox = cfg.get("workflow_mode") == "toolbox"
    if toolbox:
        method="toolbox_external"; trials_total=0
    return {
        "path": str(path), "title": cfg.get("title") or path.stem,
        "runtime_caption": runtime_caption(cfg),
        "engine_info": cfg.get("engine_info", {}),
        "build_enabled": build.get("enabled") if type(build.get("enabled")) is bool else None,
        "frequency": cfg.get("frequency"), "simulation": _mapping(cfg.get("simulation")),
        "frequency_label": FREQUENCY_LABELS.get(cfg.get("frequency"), "未指定"),
        "calibration": {"start": calibration.get("start"), "end": calibration.get("end")},
        "validation": {"start": validation.get("start"), "end": validation.get("end")},
        "calibration_start": calibration.get("start"), "calibration_end": calibration.get("end"),
        "validation_start": validation.get("start"), "validation_end": validation.get("end"),
        "params_count": len(params) if isinstance(params, list) else None,
        "trials_total": trials_total,
        "workers": _count(_mapping(cfg.get("analysis")).get("workers",1) if method=="glue_lhs" and cfg.get("workflow_mode")=="research" else search.get("workers",1)) if error is None else None,
        "method": method if error is None else None,
        "method_label": ("自动建模 → Toolbox" if toolbox else METHOD_LABELS.get(method, f"未知方法：{method}")) if error is None else "未读取",
        "parameter_backend": parameter_backend if error is None else None,
        "parameter_backend_label": PARAMETER_BACKEND_LABELS.get(parameter_backend, f"未知接口：{parameter_backend}") if error is None else "未读取",
        "objective": (None if toolbox else search.get("objective", "NSE")) if error is None else None,
        "reference_label": observation.get("label"), "reference_kind": observation.get("kind"),
        "reference_note": observation.get("provenance_note"),
        "workflow_mode": cfg.get("workflow_mode"), "observations_count":len(observations) if isinstance(observations,list) else 1,
        "sensitivity":_mapping(_mapping(cfg.get("analysis")).get("sensitivity")),
        "uncertainty":_mapping(_mapping(cfg.get("analysis")).get("uncertainty")),
        "sufi2_options":_mapping(search.get("options")) if method=="swatcup_sufi2" else {},
        "error": error,
    }


def job_state_path(job_dir):
    """Use an archived record only when the normal state file is absent."""
    job = Path(job_dir)
    state = job / "state.json"
    archived = job / "state.archived.json"
    return archived if not state.exists() and archived.is_file() else state


def load_job_summary(job_dir):
    """Read state/config metadata for one run, never time-series/model output.

    ``metrics`` preserves the backend's baseline/best, calibration/validation map.
    ``selected_score`` is only set once the backend has selected parameters;
    ``best_so_far_score`` explicitly describes a provisional search result.
    """
    job = Path(job_dir).resolve()
    state_path = job_state_path(job)
    archived = state_path.name == "state.archived.json"
    state, error = _read_json(state_path)
    config_path = job / "config.resolved.json"
    cfg = load_config_summary(config_path)
    objective = cfg["objective"]
    trials = state.get("trials")
    trials = [x for x in trials if isinstance(x, dict)] if isinstance(trials, list) else None
    selected = _mapping(state.get("selected"))
    metrics = _mapping(state.get("metrics"))
    evaluation = _mapping(state.get("evaluation"))
    baseline = _mapping(state.get("baseline"))
    selected_metrics = _mapping(selected.get("metrics"))
    baseline_metrics = _mapping(baseline.get("metrics"))
    validation_metrics = _mapping(_mapping(evaluation.get("best")).get("metrics"))
    total = _count(_mapping(state.get("input_check")).get("planned_search_trials"))
    if total is None:
        total = cfg["trials_total"]
    sufi2=_mapping(state.get("sufi2"))
    if cfg["method"]=="swatcup_sufi2":
        sizes=sufi2.get("round_sizes")
        if isinstance(sizes,list) and sizes and all(type(n) is int and n>0 for n in sizes):
            total=sum(sizes)
        if sufi2.get("finished_by_review"):
            # A finish decision cancels unsampled future rounds. Keep failures
            # visible in the completed/actual-candidate count, not a stale plan.
            if trials is not None:total=len(trials)
            else:
                rounds=sufi2.get("rounds")
                if isinstance(rounds,list) and all(isinstance(row,dict) and isinstance(row.get("trial_ids"),list) for row in rounds):
                    total=sum(len(row["trial_ids"]) for row in rounds)
                else:
                    optimizer=_mapping(state.get("optimizer_result"))
                    actual=_count(optimizer.get("evaluations"))
                    if actual is None:actual=_count(optimizer.get("budget"))
                    if actual is not None:total=actual
    scores = []
    if objective:
        for entry in [baseline] + (trials or []):
            score = _number(_mapping(entry.get("metrics")).get(objective))
            if entry.get("status") == "completed" and score is not None:
                scores.append(score)
    try:
        modified = state_path.stat().st_mtime
        updated_at = dt.datetime.fromtimestamp(modified).isoformat(timespec="seconds")
    except OSError:
        modified, updated_at = None, None
    status = state.get("status", "unknown") if error is None else "unreadable"
    phase = state.get("phase", "unknown")
    pending=_mapping(sufi2.get("pending_review"))
    return {
        "path": str(job), "name": state.get("run_name") or job.name,
        "state_path": str(state_path), "archived": archived,
        "status": status, "status_label": "已归档（只读）" if archived else STATUS_LABELS.get(status, str(status)),
        "phase": phase, "phase_label": PHASE_LABELS.get(phase, str(phase)),
        "completed_trials": sum(x.get("status") == "completed" for x in trials) if trials is not None else None,
        "total_trials": total, "failed_trials": sum(x.get("status") == "failed" for x in trials) if trials is not None else None,
        "selected_score": _number(selected_metrics.get(objective)),
        "baseline_score": _number(baseline_metrics.get(objective)),
        "validation_score": _number(validation_metrics.get(objective)),
        "best_so_far_score": max(scores) if scores else None,
        "selected_trial_id": selected.get("trial_id"),
        "objective": objective, "metrics": metrics,
        "method": cfg["method"], "method_label": cfg["method_label"],
        "parameter_backend": cfg["parameter_backend"], "parameter_backend_label": cfg["parameter_backend_label"],
        "report_exists": (job / "report.md").is_file(),
        "report_path": str(job / "report.md"), "config_path": str(config_path),
        "created_at": state.get("created_at"), "updated_at": updated_at, "mtime": modified,
        "reference_label": cfg["reference_label"], "reference_kind": cfg["reference_kind"],
        "validation_fixed_parameters": state.get("validation_fixed_parameters"),
        "sufi2_pending_review":pending,
        "sufi2_round_count":len(sufi2.get("rounds",[])) if isinstance(sufi2.get("rounds"),list) else 0,
        "error": error, "config_error": cfg["error"], "last_error": state.get("last_error"),
    }


def list_recent_jobs(workspace, limit=8):
    """Return newest direct children of workspace/runs by state.json mtime.

    Sort metadata first, then open at most ``limit`` small state/config pairs.
    Nested trials and other output directories are deliberately not traversed.
    """
    if type(limit) is not int or limit <= 0:
        return []
    runs = Path(workspace).resolve() / "runs"
    try:
        children = list(runs.iterdir())
    except OSError:
        return []
    candidates = []
    for child in children:
        try:
            if child.is_dir() and not child.is_symlink():
                state = child / "state.json"
                if state.is_file():
                    candidates.append((state.stat().st_mtime, child.name, child))
        except OSError:
            # Runs may be moved while the selector refreshes.
            continue
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [load_job_summary(item[2]) for item in candidates[:limit]]
