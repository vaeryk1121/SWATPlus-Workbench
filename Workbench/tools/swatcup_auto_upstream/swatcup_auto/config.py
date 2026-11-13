from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from .errors import ConfigError


DEFAULT_CONFIG: Dict[str, Any] = {
    "source_project": "",
    "runs_root": "./runs",
    "run_name": "swatcup_calibration",
    "simulation_count": None,
    "simulation_schedule": None,
    "stop": {
        "mode": "fixed",
        "rounds": 2,
        "max_rounds": 10,
        "target": {
            "source": "best_goal",
            "variable": "",
            "metric": "NS",
            "operator": ">=",
            "value": 0.75,
        },
    },
    "execution": {
        "pre_batch": "SUFI2_Pre.bat",
        "run_batch": "SUFI2_Run.bat",
        "post_batch": "SUFI2_Post.bat",
        "timeout_seconds": 0,
        "require_fresh_outputs": True,
    },
    "parameters": {
        "update_from_new_pars": True,
        "lock_to_initial_bounds": True,
        "on_invalid_range": "stop",
        "minimum_width": 1e-12,
        "limits": {},
    },
    "validation": {
        "enabled": False,
        "source_project": "",
        "simulation_count": None,
    },
}


def _merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def make_default_config(source_project: str = "", runs_root: str = "./runs") -> dict:
    config = deepcopy(DEFAULT_CONFIG)
    config["source_project"] = source_project
    config["runs_root"] = runs_root
    return config


def _resolve_path(value: str, base_dir: Path) -> str:
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def validate_config(config: dict) -> None:
    if not config.get("source_project"):
        raise ConfigError("source_project 不能为空")
    mode = config["stop"].get("mode")
    if mode not in {"fixed", "target"}:
        raise ConfigError("stop.mode 必须是 fixed 或 target")
    rounds = int(config["stop"].get("rounds", 0))
    max_rounds = int(config["stop"].get("max_rounds", 0))
    if rounds < 1:
        raise ConfigError("stop.rounds 必须大于等于 1")
    if max_rounds < 1:
        raise ConfigError("stop.max_rounds 必须大于等于 1")
    if mode == "target":
        target = config["stop"].get("target", {})
        if target.get("operator") not in {">=", ">", "<=", "<", "=="}:
            raise ConfigError("目标比较符必须是 >=、>、<=、< 或 ==")
        try:
            float(target.get("value"))
        except (TypeError, ValueError) as exc:
            raise ConfigError("stop.target.value 必须是数字") from exc
        if target.get("source") not in {"best_goal", "summary"}:
            raise ConfigError("stop.target.source 必须是 best_goal 或 summary")
        if target.get("source") == "summary" and not target.get("metric"):
            raise ConfigError("按汇总指标停止时必须指定 metric")
    sim_count = config.get("simulation_count")
    if sim_count is not None and int(sim_count) < 1:
        raise ConfigError("simulation_count 必须为空或大于等于 1")
    schedule = config.get("simulation_schedule")
    if schedule is not None:
        if not isinstance(schedule, list) or not schedule:
            raise ConfigError("simulation_schedule 必须为空或为非空整数列表")
        try:
            normalized_schedule = [int(value) for value in schedule]
        except (TypeError, ValueError) as exc:
            raise ConfigError("simulation_schedule 必须只包含整数") from exc
        if any(value < 1 for value in normalized_schedule):
            raise ConfigError("simulation_schedule 中的模拟次数必须大于等于 1")
    if config["parameters"].get("on_invalid_range") not in {"stop", "keep_previous"}:
        raise ConfigError("parameters.on_invalid_range 必须是 stop 或 keep_previous")
    limits = config["parameters"].get("limits", {})
    if not isinstance(limits, dict):
        raise ConfigError("parameters.limits 必须是对象")
    for name, bounds in limits.items():
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ConfigError(f"参数 {name} 的安全范围必须是 [最小值, 最大值]")
        if float(bounds[0]) >= float(bounds[1]):
            raise ConfigError(f"参数 {name} 的安全范围上下界无效")
    validation = config.get("validation", {})
    if validation.get("enabled") and not validation.get("source_project"):
        raise ConfigError("启用验证期时必须提供 validation.source_project")
    if validation.get("enabled"):
        calibration_path = Path(str(config["source_project"])).expanduser().resolve()
        validation_path = Path(str(validation["source_project"])).expanduser().resolve()
        if calibration_path == validation_path:
            raise ConfigError("验证工程必须是独立工程，不能与率定工程使用同一目录")
    validation_count = validation.get("simulation_count")
    if validation_count is not None and int(validation_count) < 1:
        raise ConfigError("validation.simulation_count 必须为空或大于等于 1")


def load_config(path: Path | str) -> dict:
    config_path = Path(path).resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 JSON 格式错误：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是对象")
    config = _merge(DEFAULT_CONFIG, data)
    config["source_project"] = _resolve_path(config["source_project"], config_path.parent)
    config["runs_root"] = _resolve_path(config["runs_root"], config_path.parent)
    if config["validation"].get("source_project"):
        config["validation"]["source_project"] = _resolve_path(
            config["validation"]["source_project"], config_path.parent
        )
    config["_config_path"] = str(config_path)
    validate_config(config)
    return config


def save_config(config: dict, path: Path | str) -> None:
    target = Path(path)
    clean = {key: value for key, value in config.items() if not key.startswith("_")}
    target.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
