"""Drive the installed Toolbox 4.0.1 through its own sptAPI command line.

The installed Toolbox ships an official CLI at apis/sptAPI.exe ("SWAT+ Toolbox
API - CLI for SWAT+ model calibration and analysis").  Every command used here
was executed against the local installation on 2026-10-31; nothing is inferred
from documentation alone.  This module prepares a Toolbox project from a
workbench job, starts Toolbox's own DDS calibration, and imports the recorded
result back into the workbench summary convention.

Not claimed: Toolbox 4.0.1 does not expose a verified seed for its calibration
random stream, does not write its best parameters into the scenario's
calibration.cal (they stay in the project database), and its DDS evaluation
count is its own implementation.  An independent fixed-parameter replay
remains a separate explicit step, exactly as for the other adapters.
"""
import argparse
import csv
import datetime as dt
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_VERSION = "4.0.1"
MODEL_VERSION = "62.0.1"
CHANGE_TYPES = {"absval": "Replace", "abschg": "Relative", "pctchg": "Percent"}
OBJECT_TYPES = {"channel_sd": "channel"}
# Verified subset: one monthly channel flow observation in m3/s, matched by
# the unit column, exactly like the Toolbox 1.0.5 adapter's first scope.
OBSERVATION_OBJECT = "channel_sd"
OBSERVATION_VARIABLE = "flo_out"


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                     allow_nan=False) + "\n", encoding="utf-8")


def _final_json(text):
    """Return the last complete JSON object printed by sptAPI."""
    for start in range(text.rfind("{"), -1, -1):
        if text[start] != "{":
            continue
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            continue
    raise ValueError("sptAPI did not print a readable JSON result: " + text[-500:])


def _events(text):
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('{"type"'):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def api_path(executable):
    """Resolve apis/sptAPI.exe next to the installed Toolbox executable."""
    api = Path(executable).parent / "apis" / "sptAPI.exe"
    if not api.is_file():
        raise FileNotFoundError(f"Toolbox API executable missing: {api}")
    return api


def check_installation(executable):
    """Accept only the installation version this adapter was verified with."""
    version = Path(executable).parent / "sq.version"
    if not version.is_file() or "<version>4.0.1</version>" not in version.read_text(
            encoding="utf-8-sig"):
        raise ValueError("sptAPI driver is verified only for Toolbox 4.0.1")
    return api_path(executable)


def run_api(api, arguments, timeout=120):
    """Run one sptAPI command and return its final JSON result and events."""
    completed = subprocess.run(
        [str(api), *[str(item) for item in arguments]], capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode:
        raise ValueError(f"sptAPI {' '.join(str(item) for item in arguments[:2])} failed "
                         f"(exit {completed.returncode}): "
                         f"{(completed.stdout or '') + (completed.stderr or '')}"[-1000:])
    result = _final_json(completed.stdout or "")
    if result.get("success") is not True:
        raise ValueError(f"sptAPI {arguments[0]} reported failure: "
                         f"{result.get('error') or result}")
    return result, _events(completed.stdout or "")


def resolve_api(job):
    """Locate the installed Toolbox 4.0.1 API from the job's runtime record."""
    from input_paths import job_root
    root = job_root(job, ROOT)
    cfg = _json(Path(job) / "config.resolved.json")
    if cfg["model"]["expected_version"] != MODEL_VERSION:
        raise ValueError(f"此驱动适用于 SWAT+ {MODEL_VERSION} 模型")
    state = _json(Path(job) / "state.json")
    if not state.get("model_ready"):
        raise ValueError("请先完成模型输入构建")
    runtime = cfg.get("runtime_info") or _json(root / cfg.get("runtime_config",
                                                             "config/qgis344_runtime.json"))
    if str(runtime.get("toolbox_version")) != TOOLBOX_VERSION:
        raise ValueError(f"此驱动适用于 Toolbox {TOOLBOX_VERSION}，运行配置记录的是 "
                         f"{runtime.get('toolbox_version')}")
    executable = Path(runtime["toolbox_executable"])
    if not executable.is_file():
        raise FileNotFoundError(executable)
    return root, cfg, state, check_installation(executable)


def scenario_directory(project_file):
    """Read the live scenario directory from the .spt document.

    sptAPI keeps calibration runs outside scenarios/Default; the actual
    directory is recorded as current_txtinout relative to base_path.
    """
    text = Path(project_file).read_text(encoding="utf-8-sig")
    current = re.search(r"^current_txtinout:\s*(.+)$", text, re.MULTILINE)
    base = re.search(r"^base_path:\s*(.+)$", text, re.MULTILINE)
    if not current or not base:
        raise ValueError("无法从 .spt 读取当前情景目录；布局与已核对版本不同")
    directory = (Path(base.group(1).strip()) / current.group(1).strip()).resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"Toolbox 情景目录不存在：{directory}")
    return directory


def _parameter_arguments(parameter):
    """Map a workbench parameter onto verified sptAPI switches.

    Toolbox 4.0.1 has no layer/condition/time selector on its CLI, so a
    parameter that narrows scope that way is refused instead of silently
    broadened to all objects.
    """
    name = parameter.get("native_name") or parameter["name"]
    if not name or parameter.get("name") != name:
        raise ValueError(f"Toolbox 4.0.1 CLI requires the native parameter name, not {name!r}")
    if tuple(parameter.get("layer") or (0, 0)) != (0, 0):
        raise ValueError(f"{name}: Toolbox 4.0.1 CLI cannot express a soil-layer range")
    if any(parameter.get(key) for key in ("names", "conditions", "year1", "year2",
                                          "day1", "day2", "file", "column", "columns")):
        raise ValueError(f"{name}: unsupported Toolbox CLI selector")
    method = parameter["method"]
    if method not in CHANGE_TYPES:
        raise ValueError(f"{name}: change method {method!r} has no Toolbox CLI mapping")
    arguments = ["parameter", "add", name, "--min", repr(float(parameter["lower"])),
                 "--max", repr(float(parameter["upper"])),
                 "--change-type", CHANGE_TYPES[method]]
    ids = parameter.get("object_ids")
    if ids:
        if any(type(item) is not int or item < 1 for item in ids):
            raise ValueError(f"{name}: object ids must be positive integers")
        arguments += ["--objects", ",".join(str(item) for item in ids)]
    return arguments


def _observation_file(definition, period, reference, destination):
    """Write the calibration-period reference as the verified Date,Value CSV."""
    if (definition.get("frequency") != "monthly" or
            definition["target"].get("object") != OBSERVATION_OBJECT or
            definition["target"].get("variable") != OBSERVATION_VARIABLE):
        raise ValueError(f"{definition['id']}: 此驱动目前仅核对月尺度 channel_sd.flo_out")
    selected = [(date, value) for date, value in sorted(reference.items())
                if period["start"] <= date <= period["end"]]
    if not selected:
        raise ValueError(f"{definition['id']}: 率定期内没有可导出的参考数据")
    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Date", "Value"])
        writer.writerows(selected)
    return selected


def prepare_project(job, repetitions, *, output_dir=None):
    """Create a Toolbox 4.0.1 project configured for this job's calibration.

    Idempotent for an identical prepared project; refuses to reuse or overwrite
    a project with different settings.
    """
    job = Path(job).resolve()
    repetitions = int(repetitions)
    if repetitions < 1:
        raise ValueError("Toolbox DDS 需要至少 1 次迭代")
    root, cfg, state, api = resolve_api(job)
    output = Path(output_dir).resolve() if output_dir else job / "integration/toolbox4_cli"
    if (output / "handoff.json").is_file():
        existing = _json(output / "handoff.json")
        if existing["repetitions"] == repetitions and existing["status"].startswith("prepared"):
            return existing
        raise ValueError("已有不同配置的 sptAPI 工程；请选择新的输出目录")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"此目录已有材料，未覆盖：{output}")
    created_output = not output.exists()

    definitions = cfg.get("observations") or ([cfg["observation"]] if cfg.get("observation") else [])
    if len(definitions) != 1:
        raise ValueError("此驱动目前核对单一观测序列")
    period = cfg.get("periods", {}).get("calibration")
    if not period:
        raise ValueError("请在任务配置中提供率定期")
    simulation = cfg["simulation"]
    calibration_end = int(period["end"][:4])
    if simulation["start_year"] + simulation["warmup_years"] != int(period["start"][:4]):
        raise ValueError("率定期必须紧跟预热期之后；Toolbox 按 start_year+warmup 起算")
    source = root / state["input_snapshot"]
    executable = Path(cfg["model"]["executable"])
    if not executable.is_file():
        raise FileNotFoundError(f"SWAT+ engine missing: {executable}")

    from multi_observation import read_observation
    read_observation(definitions[0], root)  # fail fast on unusable reference data

    # The project copies the inputs itself; stage a configured copy first so
    # Toolbox reads the calibration period, warmup and CSV print settings.
    output.mkdir(parents=True, exist_ok=True)
    try:
        return _build_project(job, api, cfg, state, root, source, output, repetitions,
                              definitions, period, simulation, calibration_end)
    except BaseException:
        if created_output:
            shutil.rmtree(output, ignore_errors=True)
        raise


def _build_project(job, api, cfg, state, root, source, output, repetitions,
                   definitions, period, simulation, calibration_end):
    executable = Path(cfg["model"]["executable"])
    if not executable.is_file():
        raise FileNotFoundError(f"SWAT+ engine missing: {executable}")
    from multi_observation import read_observation, target_specs
    reference = read_observation(definitions[0], root)

    # The project copies the inputs itself; stage a configured copy first so
    # Toolbox reads the calibration period, warmup and CSV print settings.
    staged = output / "staged_inputs"
    shutil.copytree(source, staged)
    from swatplus_trial import check_clean_source, configure_period_and_print
    check_clean_source(staged, MODEL_VERSION)
    configure_period_and_print(staged, {
        "start_year": simulation["start_year"], "end_year": calibration_end,
        "warmup_years": simulation["warmup_years"], "frequency": cfg["frequency"],
        "output_targets": target_specs(definitions), "csv_output": True})

    created, events = run_api(api, ["create", staged, "--name", "toolbox_calibration",
                                    "--dest", output], timeout=600)
    data = created["data"]
    project_file = Path(data["projectFile"]).resolve()
    target = definitions[0]["target"]
    if type(target.get("unit_id")) is not int or target["unit_id"] < 1:
        raise ValueError("使用已连接的 channel unit_id，而不是猜测的 GIS 编号")
    observation_csv = output / "calibration_reference.csv"
    selected = _observation_file(definitions[0], period, reference, observation_csv)
    run_api(api, ["observation", "add", "-o", OBJECT_TYPES[target["object"]],
                  "-n", str(target["unit_id"]), "-v", target["variable"],
                  "-f", observation_csv, "--name", str(definitions[0]["id"]),
                  "--timestep", "monthly", "--match-by", "unit",
                  "-p", project_file])
    run_api(api, ["calibration", "set-observation", "--name", str(definitions[0]["id"]),
                  "-p", project_file])
    if definitions[0].get("metric", "NSE") != "NSE":
        raise ValueError("此驱动目前核对 NSE（Toolbox 方向 Maximize）")
    run_api(api, ["calibration", "set-algorithm", "dds", "-p", project_file])
    run_api(api, ["calibration", "set-objective-function", "nse", "--direction", "Maximize",
                  "-p", project_file])
    run_api(api, ["calibration", "set-max-iterations", str(repetitions), "-p", project_file])
    run_api(api, ["calibration", "configure-dds", "--iterations", str(repetitions),
                  "-p", project_file])
    run_api(api, ["run-options", "set-period", "--start", str(simulation["start_year"]),
                  "--end", str(calibration_end), "-p", project_file])
    run_api(api, ["run-options", "set-warmup", str(simulation["warmup_years"]),
                  "-p", project_file])
    # create() defaults print_csv to false; it does not inherit print.prt.
    run_api(api, ["run-options", "set-csv-output", "true", "-p", project_file])
    # Toolbox also rewrites the print.prt object table without channel_sd;
    # re-enable every observed component explicitly instead of trusting CSV
    # output alone, then verify the exact flag that collection depends on.
    print_options = None
    for component in sorted({d["target"]["object"] for d in definitions}):
        run_api(api, ["print-options", "set", component, "monthly", "true", "-p", project_file])
        print_options = run_api(api, ["get", "print-options", "-p", project_file])[0]["data"]
        if not print_options["components"].get(component, {}).get("monthly"):
            raise ValueError(f"Toolbox 未启用 {component} 月度输出；无法回收该序列")
    options = run_api(api, ["get", "run-options", "-p", project_file])[0]["data"]
    expected_options = {"run_date_start": f"{simulation['start_year']}-01-01T00:00:00",
                        "run_date_end": f"{calibration_end}-12-31T00:00:00",
                        "run_warmup": simulation["warmup_years"], "print_csv": True}
    for key, expected in expected_options.items():
        if options.get(key) != expected:
            raise ValueError(f"Toolbox run-options {key}={options.get(key)!r} 与任务配置 "
                             f"{expected!r} 不符")

    parameters = cfg.get("parameters", [])
    if not parameters:
        raise ValueError("请在任务配置中选择率定参数")
    from native_calibration import validate_definitions
    validate_definitions([dict(item) for item in parameters], source,
                         expected_version=MODEL_VERSION)
    for parameter in parameters:
        run_api(api, _parameter_arguments(parameter) + ["-p", project_file])
    listed = run_api(api, ["parameter", "list", "-p", project_file])[0]["data"]
    configured = sorted(item["name"] for item in listed)
    expected_names = sorted((item.get("native_name") or item["name"]) for item in parameters)
    if configured != expected_names:
        raise ValueError(f"Toolbox 参数清单 {configured} 与任务配置 {expected_names} 不一致")

    now = dt.datetime.now().astimezone().isoformat()
    (output / "create_log.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "handoff_schema": 3, "driver": "sptAPI", "source_run": str(job),
        "source_input": str(source), "staged_inputs": str(staged),
        "toolbox_project": str(project_file), "txtinout": str(Path(data["txtInOut"]).resolve()),
        "api_executable": str(api), "toolbox_version": TOOLBOX_VERSION,
        "model_version": MODEL_VERSION,
        "engine": {"requested_executable": str(executable),
                   "requested_version": MODEL_VERSION},
        "algorithm": "DDS", "objective": {"metric": "NSE", "direction": "Maximize"},
        "repetitions": repetitions,
        "iterations_note": "Toolbox DDS 的评估计数是其自身实现；summary 报告记录的评估行数，"
                           "不宣称等于模型调用次数。",
        "calibration_observations": str(observation_csv), "observation_count": len(selected),
        "observation_definition": definitions[0], "parameters": parameters,
        "run_options_checked": options,
        "print_options_checked": {d["target"]["object"]: print_options["components"][d["target"]["object"]]
                                   for d in definitions},
        "status": "prepared_for_api_calibration",
        "seed_note": "Toolbox 4.0.1 的 seed 命令标注用于敏感性分析；率定随机流无可验证的种子接口。",
        "created": now, "last_requested": now,
    }
    _save(output / "handoff.json", manifest)
    return manifest


def _existing_project(job, output_dir=None):
    job = Path(job).resolve()
    output = Path(output_dir).resolve() if output_dir else job / "integration/toolbox4_cli"
    manifest_path = output / "handoff.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"请先准备 sptAPI 工程（prepare）：{manifest_path}")
    manifest = _json(manifest_path)
    if manifest.get("driver") != "sptAPI" or not Path(manifest["toolbox_project"]).is_file():
        raise FileNotFoundError("请先准备 sptAPI 工程")
    return job, output, manifest


def run_calibration(job, *, output_dir=None):
    """Run Toolbox's own DDS through sptAPI, then rerun the best parameters once.

    Toolbox does not write its best parameters into the scenario's
    calibration.cal; after calibration the project parameter values hold the
    best set, so one further model run materializes the best simulation and
    the CSV outputs this module collects.
    """
    job, output, manifest = _existing_project(job, output_dir)
    if (output / "run_result.json").is_file():
        return _json(output / "run_result.json")
    root, cfg, _state, api = resolve_api(job)
    project_file = Path(manifest["toolbox_project"])
    executable = Path(cfg["model"]["executable"])
    if not executable.is_file():
        raise FileNotFoundError(f"SWAT+ engine missing: {executable}")
    per_run = float(cfg["model"].get("timeout_seconds", 600))
    timeout = max(600.0, manifest["repetitions"] * per_run * 3 + 300.0)

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [str(api), "run", "calibration", "-e", str(executable), "-p", str(project_file)],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        partial = error.stdout if isinstance(error.stdout, str) else (error.stdout or b"")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        (output / "console.log").write_text(partial + (error.stderr or ""), encoding="utf-8",
                                            errors="replace")
        raise RuntimeError(f"sptAPI 率定超过 {timeout:.0f}s 上限；检查引擎与预算") from None
    text = (completed.stdout or "") + (completed.stderr or "")
    (output / "console.log").write_text(text, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise ValueError(f"sptAPI run calibration failed (exit {completed.returncode}): "
                         f"{text[-1000:]}")
    result = _final_json(completed.stdout or "")
    if result.get("success") is not True:
        raise ValueError(f"sptAPI run calibration reported failure: {result.get('error') or result}")
    data = result["data"]
    if not data.get("success"):
        raise ValueError("Toolbox 率定未成功完成；检查 console.log 与历史记录")
    elapsed = round(time.perf_counter() - started, 3)
    final = run_api(api, ["run", "model", "-e", str(executable), "-p", str(project_file)],
                    timeout=per_run + 60)[0]["data"]
    if not final.get("success"):
        raise ValueError("最佳参数的最终模型运行未完成；不能回收该序列")
    performance = run_api(api, ["observation", "evaluate", "-p", project_file])[0]["data"]
    run_record = {
        "status": "api_calibration_completed", "driver": "sptAPI",
        "algorithm": data.get("algorithm", manifest["algorithm"]),
        "requested_iterations": manifest["repetitions"],
        "iterations_completed": data.get("iterations_completed"),
        "final_parameters": data.get("final_parameters"),
        "reported_best": {key: data.get(key) for key in
                          ("best_objective_value", "best_nse", "best_kge",
                           "best_rmse", "best_pbias")},
        "observation_evaluate": performance,
        "calibration_seconds": data.get("duration_seconds"),
        "final_model_seconds": final.get("duration_seconds"),
        "elapsed_seconds": elapsed, "exit_code": completed.returncode,
        "engine": {"executable": str(executable),
                   "requested_version": manifest["model_version"]},
        "api_executable": str(api), "console_log": str(output / "console.log"),
        "note": "Toolbox DDS 评估计数与并行进程是其自身实现；最终一次模型运行用于落盘最佳序列。",
    }
    _save(output / "run_result.json", run_record)
    return run_record


def collect_results(job, *, output_dir=None):
    """Import the recorded Toolbox result and rescore the actual CSV outputs.

    Writes summary.json in the workbench run conventions and an
    independent_replay_spec.json for the existing native runner; the replay
    itself stays an explicit separate step.
    """
    job, output, manifest = _existing_project(job, output_dir)
    run_record = _json(output / "run_result.json") if (output / "run_result.json").is_file() else None
    if run_record is None:
        raise ValueError("请先通过 sptAPI 完成率定（run_calibration）")
    api = Path(manifest["api_executable"])
    project_file = Path(manifest["toolbox_project"])
    base = project_file.parent
    scenario = scenario_directory(project_file)
    from swatplus_trial import version_ok

    history = _json(base / "spt.data/calibration_history.json")
    if history.get("success") is not True:
        raise ValueError("Toolbox 历史记录标记为未成功；拒绝回收")
    samples = base / "spt.data/calibration-samples"
    evaluations = []
    performance_rows = []
    if (samples / "performance.csv").is_file():
        with (samples / "performance.csv").open(encoding="utf-8-sig", newline="") as stream:
            performance_rows = list(csv.DictReader(stream))
        evaluations = [row for row in performance_rows if row.get("nse")]

    parameters = []
    for definition in manifest["parameters"]:
        name = definition.get("native_name") or definition["name"]
        record = run_api(api, ["parameter", "get", name, "-p", project_file])[0]["data"]
        parameters.append({"name": name, "value": record["value"],
                           "best_cal_parameter": record["best_cal_parameter"],
                           "change_type": record["change_type"],
                           "object_type": record["object_type"]})

    definition = manifest["observation_definition"]
    from input_paths import job_root
    root = job_root(job, ROOT)
    from multi_observation import evaluate_one, read_observation
    reference = read_observation(definition, root)
    cfg = _json(Path(manifest["source_run"]) / "config.resolved.json")
    period = cfg["periods"]["calibration"]
    from toolbox4_results import read_series
    values, series_path = read_series(scenario, definition)
    scored = evaluate_one(definition, reference, values, period)
    toolbox_nse = run_record["reported_best"].get("best_nse")
    # Toolbox scores from its own series; the CSV rounds outputs, so an exact
    # tie is not expected. 1e-4 is the documented CSV-precision allowance.
    score_difference = (None if toolbox_nse is None or scored["NSE"] is None
                        else toolbox_nse - scored["NSE"])
    score_matches = (score_difference is not None and
                     abs(score_difference) <= 1e-4)
    banner = series_path.read_text(encoding="utf-8-sig").splitlines()[0]
    if not version_ok(banner, manifest["model_version"]):
        raise ValueError(f"输出横幅与请求版本不符：{banner}")

    best = {item["name"]: float(item["best_cal_parameter"]) for item in parameters
            if isinstance(item["best_cal_parameter"], (int, float))}
    applied = {item["name"]: float(f"{item['best_cal_parameter']:.5f}") for item in parameters
               if isinstance(item["best_cal_parameter"], (int, float))}
    staged_codes = (Path(manifest["staged_inputs"]) / "codes.bsn").read_text(encoding="utf-8-sig")
    final_codes = (scenario / "codes.bsn").read_text(encoding="utf-8-sig")
    codes_match = staged_codes == final_codes

    collected_at = dt.datetime.now().isoformat(timespec="seconds")
    result = {
        "status": "collected_independent_replay_pending",
        "collected_at": collected_at, "source_run": str(job),
        "provider": f"SWAT+ Toolbox {TOOLBOX_VERSION} DDS (sptAPI)",
        "toolbox_project": str(project_file), "scenario_directory": str(scenario),
        "actual_model_banner": banner.strip(),
        "algorithm": manifest["algorithm"],
        "objective": {"metric": "NSE", "direction": "Maximize",
                      "value": toolbox_nse},
        "own_rescored_metrics": scored, "score_matches_toolbox": score_matches,
        "score_difference": score_difference,
        "score_note": "Toolbox 用其内部序列评分；本项按落盘 CSV 复核，两者相差属于输出精度量级"
                      "（容差 1e-4），差异值单独记录。",
        "requested_iterations": manifest["repetitions"],
        "iterations_completed": run_record["iterations_completed"],
        "evaluations_recorded": len(evaluations),
        "best_parameters": best, "applied_best_parameters": applied,
        "parameter_precision": "Toolbox 历史按五位小数写 cal_parms 值；复算规格采用同样精度。",
        "records": run_record, "run_record": str(output / "run_result.json"),
        "history_success": history.get("success"),
        "reported_best_metrics": history.get("best_metrics"),
        "physical_settings_match_source": codes_match,
        "engine": {"executable": run_record["engine"]["executable"],
                   "requested_version": manifest["model_version"],
                   "confirmation": "版本由输出横幅核对；自编译引擎需人工确认与请求一致"},
        "independent_replay_verified": False,
        "validation_used_for_selection": False,
        "observation_definition": definition,
        "reference_kind": definition.get("kind"),
        "reference_note": definition.get("provenance_note"),
        "note": "结果来自 Toolbox 官方 CLI 率定与随后一次最佳参数运行；"
                "评分已用工作台统计对实际 CSV 复核；固定参数独立复算仍需单独执行。",
    }
    _save(output / "summary.json", result)
    for name in ("calibration_history.json",):
        if (base / "spt.data" / name).is_file():
            shutil.copy2(base / "spt.data" / name, output / name)
    if samples.is_dir():
        for path in samples.iterdir():
            if path.is_file():
                shutil.copy2(path, output / path.name)
    shutil.copy2(series_path, output / series_path.name)
    simulation = cfg["simulation"]
    replay = {
        "root": str(ROOT), "input_dir": manifest["staged_inputs"],
        "executable": run_record["engine"]["executable"],
        "expected_version": manifest["model_version"],
        "work_dir": str(output / "independent_replay"),
        "start_year": simulation["start_year"], "end_year": int(period["end"][:4]),
        "warmup_years": simulation["warmup_years"], "frequency": "monthly",
        "outlet": _json(Path(manifest["source_run"]) / "state.json")["outlet"],
        "parameter_backend": "native_calibration",
        "parameters": [{"name": item["name"], "native_name": item["name"],
                        "method": {v: k for k, v in CHANGE_TYPES.items()}[item["change_type"]],
                        "value": applied[item["name"]], "layer": [0, 0]}
                       for item in parameters
                       if isinstance(item["best_cal_parameter"], (int, float))],
        "timeout_seconds": cfg["model"].get("timeout_seconds", 600),
        "output_targets": [{"id": definition["id"], "frequency": "monthly",
                            **definition["target"]}],
    }
    _save(output / "independent_replay_spec.json", replay)
    (output / "report.md").write_text(
        "# Toolbox 4.0.1 sptAPI 率定回收\n\n"
        f"- 提供方：{result['provider']}；算法 {result['algorithm']}，目标 NSE（Maximize）。\n"
        f"- 实际模型横幅：`{banner.strip()}`。\n"
        f"- Toolbox 记录的最佳 NSE：{toolbox_nse}；工作台按实际 CSV 复核 NSE：{scored['NSE']}，"
        f"差值 {score_difference}（CSV 精度量级，容差 1e-4）："
        f"{'一致' if score_matches else '超出容差，须检查输出精度或时段'}。\n"
        f"- 评估记录 {len(evaluations)} 行（请求 {manifest['repetitions']} 次迭代；"
        "Toolbox 的评估计数与模型调用数不等同）。\n"
        f"- 最佳参数（复算写入精度）：`{json.dumps(applied, ensure_ascii=False)}`。\n"
        f"- 物理开关与源模型一致：{'是' if codes_match else '否，仅作接口诊断'}。\n"
        f"- 参考资料性质：{definition.get('kind', '未说明')}；"
        f"{definition.get('provenance_note', '请核对测站、单位与来源')}。\n"
        "- 验证期观测未参与率定；固定验证需另行执行 independent_replay_spec.json。\n"
        "- Toolbox 未提供可验证的率定种子；同配置重跑不保证同轨迹。\n",
        encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare", help="Create and configure the Toolbox project")
    prepare.add_argument("--repetitions", type=int, default=100)
    sub.add_parser("run", help="Run Toolbox DDS via sptAPI, then the best parameters once")
    sub.add_parser("collect", help="Import results and rescore the actual CSV outputs")
    arguments = parser.parse_args()
    if arguments.action == "prepare":
        print(json.dumps(prepare_project(arguments.job, arguments.repetitions,
                                         output_dir=arguments.output_dir),
                         ensure_ascii=False, indent=2))
    elif arguments.action == "run":
        print(json.dumps(run_calibration(arguments.job, output_dir=arguments.output_dir),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(collect_results(arguments.job, output_dir=arguments.output_dir),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
