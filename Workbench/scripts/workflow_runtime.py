"""Resolve new-task engines; resumed jobs use their saved program and settings."""
import copy
import json
import shutil
from pathlib import Path


def resolve_runtime_config(config, root, check_paths=True):
    cfg = copy.deepcopy(config)
    root = Path(root)
    selection = cfg.get("engine", {})
    if not isinstance(selection, dict):
        raise ValueError("engine must be an object")
    mode = selection.get("mode", "default")
    if mode not in ("default", "custom"):
        raise ValueError("引擎模式须为default或custom")
    if not cfg.get("runtime_config") and not selection:
        return cfg  # Preserve legacy configuration behavior.
    runtime = {}
    if cfg.get("runtime_config"):
        runtime_path = root / cfg["runtime_config"]
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        cfg.setdefault("model", {}).update(executable=str(root / runtime["model_executable"]),
                                           expected_version=runtime["model_version"])
        cfg["runtime_info"] = {
            "config": str(runtime_path), "qgis_root": runtime["qgis_root"],
            "qgis_version_prefix": runtime["qgis_version_prefix"],
            "plugin_version": runtime["plugin_version"], "editor_version": runtime["editor_version"],
            "model_version": runtime["model_version"], "model_build": runtime.get("model_build"),
            "toolbox_executable": runtime.get("toolbox_executable"),
            "toolbox_version": runtime.get("toolbox_version"),
        }
    if mode == "custom":
        from swatplus_profiles import SUPPORTED_VERSIONS
        if not str(selection.get("executable", "")).strip():
            raise ValueError("请选择自编译引擎exe文件")
        if selection.get("expected_version") not in SUPPORTED_VERSIONS:
            raise ValueError("自编译引擎须选择已适配的输入输出版本：" + ", ".join(sorted(SUPPORTED_VERSIONS)))
        cfg.setdefault("model", {}).update(executable=str((root / selection["executable"]).resolve()),
                                           expected_version=selection["expected_version"])
    model = cfg.get("model", {})
    if not model.get("executable"):
        raise ValueError("配置没有可用模型程序")
    exe = (root / model["executable"]).resolve()
    # Validate the selected program, never require the unused default program.
    if check_paths and not exe.is_file():
        raise FileNotFoundError(f"选定的SWAT+引擎不存在：{exe}")
    model["executable"] = str(exe)
    details = selection if mode == "custom" else {}
    cfg["engine_info"] = {
        "mode": mode, "label": details.get("label") or ("自编译引擎" if mode == "custom" else "官方/默认引擎"),
        "executable": str(exe), "selected_executable": str(exe),
        "expected_version": model.get("expected_version"),
        "source_revision": details.get("source_revision", ""),
        "source_note": details.get("source_note", ""),
        "compiler": details.get("compiler", ""), "build_options": details.get("build_options", ""),
        "build_configuration": details.get("build_configuration", "") or runtime.get("model_build", ""),
    }
    if mode == "custom" and cfg.get("runtime_info"):
        cfg["runtime_info"].update(model_version=model["expected_version"],
                                   model_build=cfg["engine_info"]["build_configuration"])
    return cfg


def prepare_job_engine(config, job):
    """Keep one custom program copy so a later rebuild cannot change a resumed trial."""
    cfg = copy.deepcopy(config)
    info = cfg.get("engine_info", {})
    if info.get("mode") != "custom":
        return cfg
    source = Path(cfg["model"]["executable"])
    folder = Path(job) / "engine"
    folder.mkdir(exist_ok=False)
    target = folder / source.name
    shutil.copy2(source, target)
    cfg["model"]["executable"] = str(target)
    info.update(executable=str(target), task_copy=True)
    return cfg


def engine_markdown(info):
    if not info:
        return []
    lines = ["", "## 实际模型引擎", "", f"- 名称：{info.get('label', '')}。",
             f"- 输入输出适配版本：{info.get('expected_version', '')}。",
             f"- 实际调用程序：`{info.get('executable', '')}`。"]
    for key, label in [("selected_executable", "所选原程序"), ("source_revision", "源码版本"),
                       ("source_note", "源码修改说明"), ("compiler", "编译器"),
                       ("build_configuration", "构建配置"), ("build_options", "编译选项")]:
        if info.get(key):
            lines.append(f"- {label}：{info[key]}。")
    if info.get("task_copy"):
        lines.append("- 本任务使用保存的自编译程序副本；续跑不受后续重新编译影响。")
    return lines


def runtime_caption(config):
    info = config.get("runtime_info", {})
    if info:
        text = (f"QGIS {info['qgis_version_prefix'].rstrip('.')} · "
                f"QSWAT+ {info['plugin_version']} · Editor {info['editor_version']} · "
                f"SWAT+ {info['model_version']}")
    else:
        version = config.get("model", {}).get("expected_version")
        text = f"SWAT+ {version} · 此配置指定的环境" if version else "运行环境未指定"
    engine = config.get("engine_info", {})
    if engine:
        text += " · " + engine["label"] + "\n" + engine["executable"]
    return text
