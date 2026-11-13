"""Run the spatial -> Editor -> SWAT+ stages in fresh named project directories."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def load_pipeline(config_path):
    """Resolve the selected build backend without changing legacy pipelines."""
    config_path = Path(config_path).resolve()
    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    from input_paths import config_root
    root = config_root(config_path,cfg)
    backend = cfg.get("backend", "legacy")
    if backend not in ("legacy", "qgis344"):
        raise ValueError(f"Unknown pipeline backend: {backend}")
    configs = {key: json.loads((root / cfg[key + "_config"]).read_text(encoding="utf-8-sig"))
               for key in ("spatial", "editor", "model")}
    if cfg.get('workspace_root'):
        for stage in configs.values():stage['workspace_root']=str(root)
    if backend == "qgis344":
        runtime_config = cfg["runtime_config"]
        runtime = json.loads((root / runtime_config).read_text(encoding="utf-8-sig"))
        cfg["qgis_python"] = str(root / runtime["qgis_root"] / "bin" / "python-qgis-ltr.bat")
        for stage in configs.values():
            stage["runtime_config"] = runtime_config
        configs["spatial"].update(
            qgis_root=runtime["qgis_root"], qswat_plugin_root=runtime["plugin_root"],
            expected_qgis_version_prefix=runtime["qgis_version_prefix"],
            expected_qswat_version=runtime["plugin_version"])
    return root, cfg, configs


def select_build_engine(root, cfg, configs, selected, runtime_target):
    """Override the model stage only; GIS/Editor keep their own environment."""
    if cfg.get("backend", "legacy") != "qgis344":
        raise ValueError("自编译引擎的空间构建目前支持QGIS344；旧版请使用已有TxtInOut")
    runtime = json.loads((root / configs["model"]["runtime_config"]).read_text(encoding="utf-8-sig"))
    if selected["model"]["expected_version"] != runtime["model_version"]:
        raise ValueError("自编译引擎接口版本须与Editor导出的版本一致")
    exe = (root / selected["model"]["executable"]).resolve()
    if not exe.is_file():
        raise FileNotFoundError(exe)
    if runtime_target.exists():
        raise FileExistsError(runtime_target)
    runtime.update(model_executable=str(exe),model_build=selected.get("engine_info",{}).get("build_configuration","自编译"))
    runtime_target.write_text(json.dumps(runtime,ensure_ascii=False,indent=2),encoding="utf-8")
    configs["model"].update(runtime_config=str(runtime_target),engine_info=selected.get("engine_info",{}))
    if selected.get("simulation"):
        configs["model"].update(selected["simulation"])
        configs["editor"]["simulation"].update(selected["simulation"])


def stage_commands(root, cfg, paths, log_path, python=None):
    """Keep the four-stage result format used by task resume."""
    python = python or sys.executable
    from input_paths import CODE_ROOT
    code = CODE_ROOT if cfg.get('workspace_root') else root
    modern = cfg.get("backend", "legacy") == "qgis344"
    spatial = "build_qswat344_project.py" if modern else "build_qswat_project.py"
    editor = "build_editor344_inputs.py" if modern else "build_editor_inputs.py"
    model = "run_swat344_model.py" if modern else "run_swat_model.py"
    return [
        ("spatial", [cfg["qgis_python"], "-X", "utf8", str(code / "scripts" / spatial),
                     "--config", str(paths["spatial"]), "--stage", "all"]),
        ("spatial_check", [cfg["qgis_python"], "-X", "utf8", str(code / "scripts/check_robit_spatial.py"),
                           "--config", str(paths["spatial"]), "--output", str(log_path / "spatial_check.json")]),
        ("editor", [python, "-X", "utf8", str(code / "scripts" / editor), "--config", str(paths["editor"])]),
        ("model", [python, "-X", "utf8", str(code / "scripts" / model), "--config", str(paths["model"])])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--name", required=True, help="New project name; existing results are not overwritten")
    parser.add_argument("--engine-config",type=Path,help="Task engine override for the model stage")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,24}", args.name):
        parser.error("Use a letter followed by at most 24 letters, digits or underscores")
    root, cfg, configs = load_pipeline(args.config)
    project_dir = f"models/examples/{args.name}"
    log_dir = f"runs/{args.name}_build"
    model_run = f"runs/{args.name}_run"
    for rel in [project_dir, log_dir, model_run]:
        if (root/rel).exists():
            raise FileExistsError(f"Choose a new name; already exists: {rel}")
    paths = {key:root/"config"/f"{args.name}_{key}.json" for key in configs}
    if any(p.exists() for p in paths.values()):
        raise FileExistsError("A generated configuration already uses this name")
    (root/'config').mkdir(parents=True,exist_ok=True)
    configs["spatial"].update(project_name=args.name, project_dir=project_dir, build_log_dir=log_dir)
    configs["editor"].update(project_name=args.name, project_db=f"{project_dir}/{args.name}.sqlite",
                             datasets_db=f"{project_dir}/swatplus_datasets.sqlite",
                             output_dir=f"{project_dir}/Scenarios/Default/TxtInOut",
                             weather_output_dir=f"data/processed/{args.name}/weather",
                             log_dir=f"runs/{args.name}_editor",temp_dir=f"runs/{args.name}_editor/temp")
    configs["model"].update(input_dir=configs["editor"]["output_dir"],run_dir=model_run)
    if args.engine_config:
        selected=json.loads(args.engine_config.read_text(encoding="utf-8-sig"))
        select_build_engine(root,cfg,configs,selected,root/"config"/f"{args.name}_engine_runtime.json")
    for key,path in paths.items():
        path.write_text(json.dumps(configs[key],ensure_ascii=False,indent=2),encoding="utf-8")
    log_path = root/log_dir
    log_path.mkdir(parents=True)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    stages = stage_commands(root, cfg, paths, log_path)
    if cfg.get("preflight"):
        from input_paths import CODE_ROOT
        code=CODE_ROOT if cfg.get('workspace_root') else root
        stages.insert(0,("input_check",[cfg["qgis_python"],"-X","utf8",str(code/"scripts/build_preflight.py"),
                       "--spatial",str(paths["spatial"]),"--editor",str(paths["editor"]),"--output",str(log_path/"input_check.json")]))
    result=[]
    for name,command in stages:
        print(f"Starting {name}",flush=True)
        start=time.perf_counter()
        with (log_path/f"{name}.log").open("wb") as log:
            proc=subprocess.run(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,
                                timeout=cfg["stage_timeout_seconds"],shell=command[0].lower().endswith(".bat"))
        result.append({"stage":name,"exit_code":proc.returncode,"seconds":round(time.perf_counter()-start,3),"command":command})
        (log_path/"pipeline_result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        if proc.returncode:
            raise RuntimeError(f"Stage {name} failed. Inspect {log_path/(name+'.log')}")
        print(f"Finished {name}: {result[-1]['seconds']} s",flush=True)
    print(json.dumps({"project":project_dir,"run":model_run,"stages":result},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
