"""Version-specific Toolbox project preparation and honest result import.

Toolbox 1.0.5 performs DDS inside its desktop application.  This module does
not invent a headless API or replace that algorithm.  It prepares an isolated
project, and reads saved results after the user runs the desktop calculation.
"""
import base64
import copy
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALGORITHM = "Dynamically Dimensioned Search (DDS)"
CHANGE_TYPES = {"absval": "Replace", "abschg": "Relative", "pctchg": "Percent"}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")


def _existing_tasks(job):
    records = []
    for path in (job / "integration").glob("toolbox_calibration*/calibration_handoff.json"):
        value = _json(path)
        if Path(value["source_run"]).resolve() == job:
            records.append((path, value))
    return records


def _result_output(job, output_dir):
    if output_dir is not None:
        output = Path(output_dir).resolve()
        output.relative_to(job / "integration")
        return output
    tasks = _existing_tasks(job)
    if not tasks:
        raise FileNotFoundError("Prepare a Toolbox calibration project first")
    return max(tasks, key=lambda item: item[1].get("last_requested", ""))[0].parent


def _number(value, label):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _yaml_scalars(value):
    """YamlDotNet's untyped reader returns scalars as strings."""
    if isinstance(value, dict):
        return {key: _yaml_scalars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_yaml_scalars(item) for item in value]
    if isinstance(value, str):
        if value in ("true", "false"):
            return value == "true"
        if re.fullmatch(r"-?(0|[1-9]\d*)", value):
            return int(value)
        if re.fullmatch(r"-?\d+\.\d+(?:[Ee][+-]?\d+)?", value):
            return float(value)
    return value


def read_spt(path, executable):
    """Read JSON/YAML with the installed Toolbox's own YAML dependency.

    Only YamlDotNet is loaded, not Toolbox or its licensing assemblies.
    No vendor code is changed, and no GUI or model is started.
    """
    path = Path(path).resolve()
    content = path.read_text(encoding="utf-8-sig")
    if content.lstrip().startswith("{"):
        return json.loads(content)
    dll = Path(executable).parent / "YamlDotNet.dll"
    if not dll.is_file():
        raise FileNotFoundError(f"Toolbox YAML reader missing: {dll}")
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false);"
        f"$null=[Reflection.Assembly]::LoadFrom({quote(dll)});"
        "$reader=New-Object YamlDotNet.Serialization.Deserializer;"
        f"$inputText=New-Object IO.StringReader ([IO.File]::ReadAllText({quote(path)}));"
        "$data=$reader.Deserialize($inputText);"
        "$data | ConvertTo-Json -Depth 40 -Compress"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    completed = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, encoding="utf-8", timeout=30, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode:
        raise ValueError("Could not read Toolbox project: " + completed.stderr[-1500:])
    return _yaml_scalars(json.loads(completed.stdout.lstrip("\ufeff")))


def _check_version(executable):
    executable = Path(executable)
    if not executable.is_file():
        raise FileNotFoundError(f"Toolbox executable missing: {executable}")
    config = Path(str(executable) + ".config")
    if not config.is_file() or "<value>1.0.5</value>" not in config.read_text(
            encoding="utf-8-sig"):
        raise ValueError("Full calibration project adapter is verified only for Toolbox 1.0.5")


def _calibration_dictionary(path):
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    header = lines[2].split()
    return {row["name"]: row for row in
            (dict(zip(header, line.split())) for line in lines[3:] if line.strip())}


def _basin_codes(model):
    lines = (Path(model) / "codes.bsn").read_text(encoding="utf-8-sig").splitlines()
    if len(lines) < 3:
        raise ValueError("Invalid codes.bsn")
    names, values = lines[1].split(), lines[2].split()
    if len(names) != 24 or len(values) != 24 or names[2] != "pet" or names[7] != "rte_cha":
        raise ValueError("Unsupported codes.bsn layout for Toolbox 1.0.5 / SWAT+ 60.5.7")
    return names, values


def run_options_from_model(model):
    """Preserve the physical switches the old Toolbox writer can represent.

    Inspection of this installed version's codes_bsn_file() shows it rewrites
    every switch, with only PET and channel routing exposed by run_options.
    Refuse other non-default switches instead of silently resetting them.
    """
    names, values = _basin_codes(model)
    fixed = "null null 1 0 0 1 0 0 0 1 0 0 0 0 0 1 0 0 0 0 0 a 0 0".split()
    for index, (actual, expected) in enumerate(zip(values, fixed)):
        if index not in (2, 7) and actual.lower() != expected:
            raise ValueError(f"Toolbox 1.0.5 would overwrite codes.bsn {names[index]}="
                             f"{actual} with {expected}; this model is not compatible")
    pet = {"0": "Priestly and Taylor", "1": "Penman-Monteith", "2": "Hargreaves"}
    routing = {"0": "Variable Storage", "1": "Muskingum"}
    if values[2] not in pet or values[7] not in routing:
        raise ValueError("Toolbox cannot preserve this PET or routing choice")
    return {"pet_method": pet[values[2]], "time_units": "Hours",
            "routing_method": routing[values[7]]}


def compare_basin_codes(source_model, toolbox_model):
    source_names, source = _basin_codes(source_model)
    current_names, current = _basin_codes(toolbox_model)
    # SWAT+ reads these switches positionally. The old GUI emits older column
    # labels, which are recorded separately from the actual switch values.
    differences = [{"position": index + 1, "source_field": name,
                    "toolbox_field": current_names[index],
                    "source_value": left, "toolbox_value": right}
                   for index, (name, left, right) in enumerate(zip(source_names, source, current))
                   if left.lower() != right.lower()]
    return {"values_match": not differences, "differences": differences,
            "column_labels_match": source_names == current_names,
            "basis": "24 positional codes.bsn switches for SWAT+ 60.5.7"}


def parameter_records(parameters, model, executable, expected_version):
    """Map native operators and connected object IDs without changing units."""
    from native_calibration import validate_definitions, _connected_ids, _database
    metadata = validate_definitions(parameters, model, expected_version)
    model_dictionary = _database(Path(model))[0]
    vendor_dictionary = _calibration_dictionary(Path(executable).parent /
                                               "assets/cal_parms.cal")
    result = []
    names = set()
    for index, (definition, meta) in enumerate(zip(parameters, metadata), 1):
        name = meta["native_name"]
        if name in names:
            raise ValueError("Toolbox adapter needs unique native parameter names")
        names.add(name)
        if "native_name" not in definition:
            raise ValueError("Toolbox requires explicit native_name, not legacy row-name selectors")
        if meta["layer"] != [0, 0]:
            raise ValueError(f"{name}: Toolbox 1.0.5 cannot preserve a selected soil-layer range")
        if any(definition.get(key) for key in ("names", "conditions", "year1", "year2", "day1", "day2")):
            raise ValueError(f"{name}: unsupported Toolbox spatial/time/condition selector")
        native = model_dictionary[name]
        vendor = vendor_dictionary.get(name)
        if not vendor or vendor["obj_typ"] != meta["object_type"]:
            raise ValueError(f"{name}: missing or different object type in Toolbox parameter dictionary")
        if any(float(native[key]) != float(vendor[key]) for key in ("abs_min", "abs_max")):
            raise ValueError(f"{name}: Toolbox and model absolute bounds disagree")
        if any(key in definition and float(definition[key]) != float(native[column])
               for key, column in (("min", "abs_min"), ("max", "abs_max"))):
            raise ValueError(f"{name}: Toolbox cannot preserve custom absolute clipping limits")
        ids = definition.get("object_ids")
        if ids is not None:
            available = set(_connected_ids(Path(model), meta["object_type"]))
            if not set(ids) <= available:
                raise ValueError(f"{name}: connected object IDs do not exist in this model")
        low, high = (_number(definition[key], key) for key in ("lower", "upper"))
        if low >= high:
            raise ValueError(f"{name}: calibration lower bound must be below upper bound")
        result.append({
            "name": name, "minimum": low, "maximum": high,
            "absolute_minimum": float(native["abs_min"]),
            "absolute_maximum": float(native["abs_max"]),
            "value": (low + high) / 2,
            "change_type": CHANGE_TYPES[meta["method"]],
            "object_type": meta["object_type"], "units": "" if native["units"] == "null" else native["units"],
            "sensitivity": 0, "best_sens_parameter": 0, "best_cal_parameter": 0,
            "id": index,
            "attached_objects": [{"is_range": False, "from": 0, "to": 0, "number": item}
                                 for item in (ids or [])],
            "parameter_density": {"parameter_id": "", "par_values": [], "perf_values": []},
        })
    if not result:
        raise ValueError("Choose at least one calibration parameter")
    return result


def observation_record(cfg, destination, root=ROOT):
    from multi_observation import read_observation, normalized_unit, evaluate_one
    definitions = cfg.get("observations") or [cfg["observation"]]
    if len(definitions) != 1:
        raise ValueError("This Toolbox adapter currently verifies one monthly river-flow series")
    definition = copy.deepcopy(definitions[0])
    target = definition.get("target", {})
    if (definition.get("frequency") != "monthly" or
            target.get("object") != "channel_sd" or target.get("variable") != "flo_out" or
            normalized_unit(definition.get("unit")) != "m3/s" or
            normalized_unit(target.get("unit")) != "m3/s"):
        raise ValueError("Toolbox connection requires monthly channel_sd.flo_out in m3/s")
    if definition.get("metric", "NSE") != "NSE" or definition.get("weight", 1) != 1:
        raise ValueError("Toolbox connection currently verifies NSE and one observation with weight 1")
    if type(target.get("unit_id")) is not int or target["unit_id"] < 1:
        raise ValueError("Use a positive connected channel unit_id, not a guessed GIS ID")
    period = cfg["periods"]["calibration"]
    validation = cfg["periods"]["validation"]
    if not (period["end"] < validation["start"] or validation["end"] < period["start"]):
        raise ValueError("Calibration and validation periods overlap")
    reference = read_observation(definition, root)
    selected = {date: value for date, value in reference.items()
                if period["start"] <= date <= period["end"]}
    evaluate_one(definition, selected, selected, period)
    # This old GUI writes full-year simulation windows. Limit the first verified
    # adapter to full calendar years instead of silently changing requested dates.
    if not period["start"].endswith("-01-01") or not period["end"].endswith("-12-31"):
        raise ValueError("Toolbox 1.0.5 adapter currently requires full-year calibration dates")
    if cfg["simulation"]["start_year"] + cfg["simulation"]["warmup_years"] != int(period["start"][:4]):
        raise ValueError("Calibration must start immediately after the configured warmup years")
    dates = sorted(selected)
    expected = [f"{year}-{month:02d}-01"
                for year in range(int(period["start"][:4]), int(period["end"][:4]) + 1)
                for month in range(1, 13)]
    if dates != expected:
        raise ValueError("This Toolbox connection requires complete monthly calibration observations")
    with Path(destination).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Date", "Flow"])
        writer.writerows(sorted(selected.items()))
    number = target["unit_id"]
    record = {"id": 1, "object_type": "Channel", "number": number,
              "file": str(Path(destination).resolve()), "timestep": "Monthly",
              "observed_variable": "River Flow", "tsteps": 0,
              "nse": 0, "mse": 0, "rmse": 0, "pbias": 0,
              "chart_name": f"Channel {number} Monthly River Flow",
              "weight": 1, "statistics": None, "period": None}
    return record, definition, selected


def prepare_calibration_project(run_dir, repetitions=100, algorithm="DDS", *,
                                parameters=None, output_dir=None):
    """Prepare a fully configured copy. Never overwrite an existing GUI project."""
    job = Path(run_dir).resolve()
    job.relative_to(ROOT / "runs")
    if algorithm != "DDS":
        raise ValueError("Only Toolbox's DDS has a complete verified project/result adapter")
    if type(repetitions) is not int or repetitions < 10:
        raise ValueError("Toolbox 1.0.5 DDS requires an integer budget of at least 10")
    cfg = _json(job / "config.resolved.json")
    if cfg["model"]["expected_version"] != "60.5.7":
        raise ValueError("This Toolbox 1.0.5 integration has been verified with SWAT+ 60.5.7 only")
    if parameters is not None:
        cfg["parameters"] = copy.deepcopy(parameters)
    state = _json(job / "state.json")
    settings = _json(ROOT / "config/external_tools.json")
    executable = settings["toolbox_executable"]
    _check_version(executable)
    source = Path(state["source_input"]).resolve()
    source_project = source.parents[2]
    if source != source_project / "Scenarios/Default/TxtInOut":
        raise ValueError("Toolbox adapter needs the retained Editor project layout")
    run_options = run_options_from_model(source)
    output = Path(output_dir).resolve() if output_dir else job / "integration/toolbox_calibration"
    output.relative_to(job / "integration")
    if output_dir is None:
        matching = [(path, data) for path, data in _existing_tasks(job)
                    if data["repetitions"] == repetitions and data["config"] == cfg
                    and data.get("physical_run_options") == run_options]
        if matching:
            output = max(matching, key=lambda item: item[1].get("last_requested", ""))[0].parent
        elif output.exists() and any(output.iterdir()):
            index = 2
            while output.exists():
                output = job / f"integration/toolbox_calibration_{index:02d}"
                index += 1
    manifest_path = output / "calibration_handoff.json"
    if manifest_path.exists():
        existing = _json(manifest_path)
        if (existing["repetitions"] != repetitions or existing["config"] != cfg or
                existing.get("physical_run_options") != run_options):
            raise ValueError("Existing Toolbox task has different settings; choose a new output directory")
        if not Path(existing["toolbox_project"]).is_file():
            raise FileNotFoundError("Prepared Toolbox project is missing")
        existing["last_requested"] = dt.datetime.now().astimezone().isoformat()
        _save(manifest_path, existing)
        return existing
    if output.exists() and any(output.iterdir()):
        raise ValueError("Destination already contains files; refusing to overwrite a Toolbox project")
    records = parameter_records(cfg["parameters"], source, executable, cfg["model"]["expected_version"])
    output.mkdir(parents=True, exist_ok=True)
    observed_path = output / "calibration_flow.csv"
    observation, definition, reference = observation_record(cfg, observed_path)
    copied = output / "editor_project"
    shutil.copytree(source_project, copied)
    inputs = copied / "Scenarios/Default/TxtInOut"
    from swatplus_trial import configure_period_and_print, check_clean_source
    check_clean_source(inputs, cfg["model"]["expected_version"])
    simulation = cfg["simulation"]
    calibration_end = int(cfg["periods"]["calibration"]["end"][:4])
    configure_period_and_print(inputs, {"start_year": simulation["start_year"],
                                      "end_year": calibration_end,
                                      "warmup_years": simulation["warmup_years"],
                                      "frequency": "monthly"})
    template = ROOT / "config/toolbox_1_0_5_template.spt"
    project = read_spt(template, executable)
    project_path = copied / "workflow_calibration.spt"
    now = dt.datetime.now().astimezone().isoformat()
    project.update({
        "project_name": project_path.name, "project_path": str(project_path),
        "txtinout": str(inputs), "created": now, "last_modified": now,
        "run_date_start": f"{simulation['start_year']}-01-01T00:00:00",
        "run_date_end": f"{calibration_end}-12-31T00:00:00",
        "run_warmup": simulation["warmup_years"],
        "run_options": run_options,
        "current_observations": [observation], "current_parameters": records,
        "selected_eval_index": 0,
    })
    project["auto_cal_config"].update({
        "algorithm": ALGORITHM, "multi_site": False, "objective_function": "NSE",
        "function_direction": "maximise", "maximum_iterations": repetitions,
        "active_chart": observation["chart_name"], "best_objective_fx_value": -999,
    })
    # JSON is a YAML subset and avoids ambiguous locale/number/path scalars.
    _save(project_path, project)
    result = {
        "source_run": str(job), "source_input": str(source),
        "toolbox_project": str(project_path), "txtinout": str(inputs),
        "toolbox_executable": executable, "toolbox_version": "1.0.5",
        "required_model_revision": cfg["model"]["expected_version"],
        "calibration_observations": str(observed_path), "observation_count": len(reference),
        "algorithm": "DDS", "repetitions": repetitions, "config": cfg,
        "parameter_names": {str(item["native_name"]).lower(): item["name"]
                            for item in cfg["parameters"]},
        "observation_definition": definition,
        "status": "prepared_for_gui_calibration",
        "physical_run_options": run_options,
        "last_requested": now,
        "execution": "Toolbox GUI; no supported headless calibration API is claimed",
        "seed": None, "seed_note": "Toolbox 1.0.5 DDS uses its own System.Random; no seed interface verified",
    }
    _save(manifest_path, result)
    return result


def launch_calibration_project(run_dir, repetitions=100, algorithm="DDS", **kwargs):
    data = prepare_calibration_project(run_dir, repetitions, algorithm, **kwargs)
    options = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1
        options["startupinfo"] = startup
    process = subprocess.Popen([data["toolbox_executable"], data["toolbox_project"]],
                               cwd=str(Path(data["toolbox_executable"]).parent), **options)
    return data, process


def read_dds_database(path, parameter_names):
    """Read actual DDS CSV history; model calls are distinct from iterations."""
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames or []
        if not columns or columns[0] != "objfx" or not set(parameter_names) <= set(columns):
            raise ValueError("Unexpected Toolbox DDS database columns")
        if len(set(columns)) != len(columns):
            raise ValueError("Duplicate Toolbox DDS database columns")
        date_columns = {}
        for column in columns:
            if column.startswith("indx_"):
                match = re.fullmatch(r"indx_(\d{1,2})-(\d{1,2})-(\d{4})", column)
                if not match:
                    raise ValueError("Unrecognised DDS output date")
                day, month, year = map(int, match.groups())
                date_columns[column] = dt.date(year, month, day).isoformat()
        if len(set(date_columns.values())) != len(date_columns):
            raise ValueError("Duplicated DDS output date")
        rows = []
        for index, row in enumerate(reader, 1):
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Incomplete Toolbox DDS record; wait for saving to finish")
            rows.append({
                "candidate": index, "objective": _number(row["objfx"], "DDS objective"),
                "values": {name: _number(row[name], name) for name in parameter_names},
                "series": {date: _number(row[column], column)
                           for column, date in date_columns.items()},
            })
    if not rows:
        raise ValueError("Toolbox DDS history contains no completed records")
    return rows


def collect_calibration_results(run_dir, *, output_dir=None):
    """Import DDS records; independent re-simulation is a separate explicit step."""
    job = Path(run_dir).resolve()
    job.relative_to(ROOT / "runs")
    output = _result_output(job, output_dir)
    manifest = _json(output / "calibration_handoff.json")
    cfg = manifest["config"]
    project = read_spt(manifest["toolbox_project"], manifest["toolbox_executable"])
    if (project["auto_cal_config"]["algorithm"] != ALGORITHM or
            project["auto_cal_config"]["objective_function"] != "NSE" or
            project["auto_cal_config"]["function_direction"] != "maximise"):
        raise ValueError("Toolbox project algorithm/objective changed; cannot label it as this DDS task")
    if int(project["auto_cal_config"]["maximum_iterations"]) != manifest["repetitions"]:
        raise ValueError("Toolbox budget changed; prepare the matching task before importing results")
    if (str(project["run_date_start"])[:10] != f"{cfg['simulation']['start_year']}-01-01" or
            str(project["run_date_end"])[:10] != cfg["periods"]["calibration"]["end"] or
            int(project["run_warmup"]) != cfg["simulation"]["warmup_years"]):
        raise ValueError("Toolbox simulation/warmup dates changed")
    observed = project.get("current_observations", [])
    if len(observed) != 1 or Path(observed[0]["file"]).resolve() != Path(
            manifest["calibration_observations"]).resolve():
        raise ValueError("Toolbox observation selection changed")
    for key, expected in (("object_type", "Channel"), ("timestep", "Monthly"),
                          ("observed_variable", "River Flow"), ("weight", 1),
                          ("number", manifest["observation_definition"]["target"]["unit_id"])):
        if observed[0].get(key) != expected:
            raise ValueError(f"Toolbox observation setting changed: {key}")
    from multi_observation import read_observation, statistics
    definition = manifest["observation_definition"]
    reference = read_observation(definition, ROOT)
    period = cfg["periods"]["calibration"]
    reference = {date: value for date, value in reference.items()
                 if period["start"] <= date <= period["end"]}
    with Path(manifest["calibration_observations"]).open(encoding="utf-8-sig", newline="") as stream:
        transferred = {row["Date"]: float(row["Flow"]) for row in csv.DictReader(stream)}
    if transferred != reference:
        raise ValueError("Transferred calibration observations changed or include validation data")
    expected_records = parameter_records(cfg["parameters"], manifest["source_input"],
                                        manifest["toolbox_executable"],
                                        manifest["required_model_revision"])
    parameters = project.get("current_parameters", [])
    if len(parameters) != len(expected_records):
        raise ValueError("Toolbox parameter count changed")
    for expected, actual in zip(expected_records, parameters):
        for key in ("name", "minimum", "maximum", "change_type", "object_type", "attached_objects"):
            if expected[key] != actual.get(key):
                raise ValueError(f"Toolbox parameter setting changed: {expected['name']}.{key}")
    inputs = Path(manifest["txtinout"])
    from swatplus_trial import version_ok
    output_file = inputs / "channel_sd_mon.txt"
    with output_file.open(encoding="utf-8-sig") as stream:
        banner = stream.readline().strip()
    if not version_ok(banner, manifest["required_model_revision"]):
        raise ValueError(f"Toolbox model version differs from requested revision: {banner}")
    rows = read_dds_database(inputs / "db.dds", manifest["parameter_names"])
    physical_comparison = compare_basin_codes(manifest["source_input"], inputs)
    for row in rows:
        for parameter in expected_records:
            if not parameter["minimum"] <= row["values"][parameter["name"]] <= parameter["maximum"]:
                raise ValueError("A Toolbox candidate lies outside the declared parameter range")
    best = max(rows, key=lambda item: item["objective"])
    saved_score = _number(project["auto_cal_config"]["best_objective_fx_value"], "Saved best score")
    if not math.isclose(best["objective"], saved_score, rel_tol=1e-8, abs_tol=1e-8):
        raise ValueError("Saved project and DDS history disagree; save the completed Toolbox project")
    for expected, actual in zip(expected_records, parameters):
        score_value = _number(actual["best_cal_parameter"], "Best parameter")
        if not math.isclose(score_value, best["values"][expected["name"]], rel_tol=1e-7, abs_tol=1e-7):
            raise ValueError("Saved best parameters disagree with DDS history")
    trace_checks = []
    for row in rows:
        if set(row["series"]) != set(reference):
            raise ValueError("DDS time-series dates do not match the calibration-only observations")
        dates = sorted(reference)
        score = statistics([reference[date] for date in dates],
                           [row["series"][date] for date in dates])["NSE"]
        trace_checks.append(score is not None and
                            math.isclose(score, row["objective"], rel_tol=1e-6, abs_tol=1e-6))
    native_best = {manifest["parameter_names"][name]: value for name, value in best["values"].items()}
    # The installed program's set_new_parameters() uses format 0.00000.
    # Replaying its unrounded optimisation coordinates would change the model.
    applied_best = {name: float(f"{value:.5f}") for name, value in native_best.items()}
    result = {
        "status": ("imported_requires_independent_replay" if physical_comparison["values_match"]
                   else "imported_physical_settings_mismatch"),
        "provider": "SWAT+ Toolbox 1.0.5 DDS", "toolbox_project": manifest["toolbox_project"],
        "actual_model_banner": banner, "best_parameters": native_best,
        "applied_best_parameters": applied_best,
        "parameter_precision": "Toolbox writes calibration.cal with five decimal places",
        "objective": {"metric": "NSE", "value": best["objective"]},
        "records": len(rows), "requested_model_runs": manifest["repetitions"],
        "budget_complete": len(rows) >= manifest["repetitions"],
        "history_series_match_scores": all(trace_checks),
        "best_toolbox_series_csv": str(output / "best_toolbox_series.csv"),
        "history_series_note": "DDS exported series checked against each saved NSE; "
                               "independent native replay is still required for final verification.",
        "reference_kind": definition.get("kind"),
        "reference_note": definition.get("provenance_note"),
        "validation_used_for_selection": False,
        "physical_settings_match_source": physical_comparison["values_match"],
        "physical_settings_comparison": physical_comparison,
        "independent_replay_verified": False,
        "report_path": str(output / "calibration_import.md"),
        "best_native_parameters": [{**item, "value": applied_best[item["name"]]}
                                   for item in cfg["parameters"]],
    }
    state = _json(job / "state.json")
    target = definition["target"]
    replay = {
        "root": str(ROOT), "input_dir": manifest["source_input"],
        "executable": str(ROOT / cfg["model"]["executable"]),
        "expected_version": manifest["required_model_revision"],
        "work_dir": str(output / "independent_replay"),
        "start_year": cfg["simulation"]["start_year"],
        "end_year": int(cfg["periods"]["validation"]["end"][:4]),
        "warmup_years": cfg["simulation"]["warmup_years"], "frequency": "monthly",
        "outlet": state["outlet"], "parameter_backend": "native_calibration",
        "parameters": result["best_native_parameters"],
        "timeout_seconds": cfg["model"].get("timeout_seconds", 600),
        "output_targets": [{"id": definition["id"], "frequency": "monthly", **target}],
    }
    _save(output / "independent_replay_spec.json", replay)
    result["replay_spec"] = str(output / "independent_replay_spec.json")
    _save(output / "calibration_import.json", result)
    with Path(result["best_toolbox_series_csv"]).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "value"])
        writer.writerows(sorted(best["series"].items()))
    with (output / "candidate_history.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["candidate", "NSE"] + list(manifest["parameter_names"]))
        writer.writerows([row["candidate"], row["objective"]] +
                         [row["values"][name] for name in manifest["parameter_names"]] for row in rows)
    Path(result["report_path"]).write_text(
        "# Toolbox DDS 结果回收\n\n"
        f"- 来源：Toolbox 1.0.5 的实际 DDS 记录，共 {len(rows)} 组。\n"
        f"- 率定期 NSE：{best['objective']:.9f}。\n"
        f"- 最佳参数：`{json.dumps(native_best, ensure_ascii=False)}`。\n"
        f"- 实际模型标识：`{banner}`。\n"
        f"- 模型物理开关与源模型一致：{'是' if physical_comparison['values_match'] else '否，本次只能作为接口兼容性诊断'}。\n"
        f"- 物理设置差异：`{json.dumps(physical_comparison['differences'], ensure_ascii=False)}`。\n"
        f"- 参考数据性质：{definition.get('kind', '未说明')}；"
        f"{definition.get('provenance_note', '请核对测站、单位与来源')}。\n"
        f"- 导出序列与记录评分逐组一致：{'是' if all(trace_checks) else '否，不能将该导出序列当作可信模拟结果'}。\n"
        "- 验证期观测未导入 Toolbox 率定项目。\n"
        "- 这是已保存结果的回收；必须用同一参数独立运行模型后，才能声称复算通过。\n",
        encoding="utf-8")
    return result


def verify_replay(run_dir, *, output_dir=None):
    """Check an already executed native replay; this function never runs SWAT+."""
    job = Path(run_dir).resolve()
    job.relative_to(ROOT / "runs")
    output = _result_output(job, output_dir)
    manifest = _json(output / "calibration_handoff.json")
    imported = _json(output / "calibration_import.json")
    spec = _json(imported["replay_spec"])
    work = Path(spec["work_dir"])
    if _json(work / "trial_spec.json") != spec:
        raise ValueError("Replay settings do not match the selected Toolbox parameters")
    trial = _json(work / "trial_result.json")
    if trial.get("status") != "completed":
        raise ValueError("Independent SWAT+ replay is not completed")
    from multi_observation import read_observation, read_target_csv, evaluate_one
    definition = manifest["observation_definition"]
    reference = read_observation(definition, ROOT)
    simulation = read_target_csv(trial["target_csvs"][definition["id"]]["path"])
    cfg = manifest["config"]
    calibration = evaluate_one(definition, reference, simulation, cfg["periods"]["calibration"])
    validation = evaluate_one(definition, reference, simulation, cfg["periods"]["validation"])
    difference = calibration["NSE"] - imported["objective"]["value"]
    score_matched = math.isclose(difference, 0, abs_tol=1e-6)
    exported = read_target_csv(imported["best_toolbox_series_csv"])
    if any(date not in simulation for date in exported):
        raise ValueError("Independent replay is missing Toolbox's calibrated dates")
    maximum_difference = max(abs(simulation[date] - value) for date, value in exported.items())
    series_matched = maximum_difference <= 1e-9 and imported["history_series_match_scores"]
    matched = score_matched and series_matched and imported["physical_settings_match_source"]
    imported.update({
        "status": ("verified" if matched else "physical_settings_mismatch"
                   if not imported["physical_settings_match_source"] else "replay_result_mismatch"),
        "independent_replay_verified": matched,
        "replay_score_matches": score_matched,
        "replay_series_matches": series_matched,
        "replay_max_abs_difference_m3_s": maximum_difference,
        "history_series_note": ("Saved DDS series and NSE checked; independent native replay "
                                "also matches the best monthly series and source physical settings."
                                if matched else
                                "Independent replay was evaluated but full equivalence did not pass; "
                                "inspect physical settings, monthly differences, and scores."),
        "calibration": calibration, "validation": validation,
        "replay_score_difference": difference,
        "replay_result": str(work / "trial_result.json"),
        "replay_target_csv": trial["target_csvs"][definition["id"]]["path"],
    })
    _save(output / "calibration_import.json", imported)
    with Path(imported["report_path"]).open("a", encoding="utf-8") as stream:
        stream.write("\n## 同参数独立复算\n\n"
                     f"- 模型计算：完成。率定期 NSE={calibration['NSE']:.9f}，"
                     f"验证期 NSE={validation['NSE']:.9f}。\n"
                     f"- 与Toolbox所记NSE差值：{difference:.12g}。\n"
                     f"- 与Toolbox最佳月序列最大绝对差：{maximum_difference:.12g} m³/s。\n"
                     f"- 复算核对：{'通过' if matched else '不通过，需检查外部算法评分或参数应用'}。\n"
                     "- 验证期只评价固定参数，不参与重新选优。\n")
    return imported


def collect_and_verify(run_dir, output_dir=None):
    """Read actual Toolbox DDS results and run/reuse one fixed-parameter replay.

    This starts our existing native SWAT+ runner, never Toolbox's private
    methods. The calibration-only check and the held-out validation are both
    extracted from that one fixed-parameter model execution.
    """
    imported = collect_calibration_results(run_dir, output_dir=output_dir)
    job = Path(run_dir).resolve()
    output = _result_output(job, output_dir)
    for field, reason in (
            ("physical_settings_match_source", "Toolbox physical settings differ from the source model"),
            ("budget_complete", "Toolbox DDS candidate budget is not completed"),
            ("history_series_match_scores", "Toolbox exported series do not reproduce its saved scores")):
        if not imported[field]:
            raise ValueError(reason + f"; see {imported['report_path']}")
    spec_path = Path(imported["replay_spec"])
    spec = _json(spec_path)
    base_work = output / "independent_replay"
    candidates = [base_work] + sorted(output.glob("independent_replay_[0-9]*"))
    reusable = None
    for candidate in candidates:
        try:
            expected = {**spec, "work_dir": str(candidate)}
            stored = _json(candidate / "trial_spec.json")
            result = _json(candidate / "trial_result.json")
            target = result.get("target_csvs", {}).get(
                _json(output / "calibration_handoff.json")["observation_definition"]["id"], {})
            if (stored == expected and result.get("status") == "completed"
                    and Path(target.get("path", "")).is_file()):
                reusable = candidate
                break
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    if reusable is not None:
        spec["work_dir"] = str(reusable)
        imported["replay_execution"] = "reused_completed"
    else:
        destination = base_work
        index = 2
        while destination.exists():
            destination = output / f"independent_replay_{index:02d}"
            index += 1
        spec["work_dir"] = str(destination)
        imported["replay_execution"] = "ran_once"
    _save(spec_path, spec)
    _save(output / "calibration_import.json", imported)
    if reusable is None:
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe" and python.with_name("python.exe").is_file():
            python = python.with_name("python.exe")
        command = [str(python), "-B", "-X", "utf8",
                   str(Path(__file__).with_name("swatplus_trial.py")), "--spec", str(spec_path)]
        try:
            completed = subprocess.run(
                command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                encoding="utf-8", errors="replace", check=False,
                timeout=float(spec["timeout_seconds"]) + 60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if completed.returncode:
                raise RuntimeError("Independent SWAT+ replay failed: " +
                                   (completed.stderr or completed.stdout)[-1500:])
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
            imported.update(status="replay_failed", independent_replay_verified=False,
                            replay_error=str(error))
            _save(output / "calibration_import.json", imported)
            with Path(imported["report_path"]).open("a", encoding="utf-8") as stream:
                stream.write(f"\n独立复算未完成：{error}\n")
            raise
    try:
        return verify_replay(job, output_dir=output)
    except (OSError, ValueError, KeyError) as error:
        imported.update(status="replay_failed", independent_replay_verified=False,
                        replay_error=str(error))
        _save(output / "calibration_import.json", imported)
        raise
