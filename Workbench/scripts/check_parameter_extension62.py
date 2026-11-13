"""Small real-engine checks of the 62.0.1 parameter extension; not calibration."""
import argparse
import csv
import json
import math
from pathlib import Path
import shutil

from native_calibration import parameter_catalog
from swatplus_profiles import read_output_header, validate_output_row
from swatplus_trial import run_trial
from workflow_runtime import resolve_runtime_config

ROOT = Path(__file__).resolve().parents[1]
NEW = {"lat_len", "bd", "alb", "evrch"}


def diagnostics(folder):
    result = {}
    for filename, kind, variables in [
        ("basin_wb_mon.txt", "basin_wb", ["et", "latq", "perc", "wateryld"]),
        ("channel_sd_mon.txt", "channel_sd", ["flo_out", "evap"]),
    ]:
        with (folder / filename).open(encoding="utf-8-sig") as stream:
            _, header, _ = read_output_header(stream, "62.0.1", kind)
            rows = {}
            for line in stream:
                fields = line.split()
                if not fields:
                    continue
                validate_output_row(fields, header, kind, "62.0.1")
                key = tuple(fields[header.index(name)] for name in ["yr", "mon", "unit", "gis_id"])
                values = {name: float(fields[header.index(name)]) for name in variables}
                if key in rows or not all(math.isfinite(value) for value in values.values()):
                    raise ValueError("Duplicate or non-finite diagnostic row")
                rows[key] = values
        result[kind] = rows
    return result


def differences(reference, current):
    result = {}
    for kind, before in reference.items():
        after = current[kind]
        if before.keys() != after.keys():
            raise ValueError("Diagnostic dates/object IDs differ")
        result[kind] = {name: max(abs(row[name] - before[key][name]) for key, row in after.items())
                        for name in next(iter(after.values()))}
    return result


def outlet_series(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return {row["date"]: float(row["flow_m3_s"]) for row in csv.DictReader(stream)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "runs/parameter_review_20261022")
    args = parser.parse_args()
    output = args.output.resolve()
    if ROOT / "runs" not in output.parents:
        raise ValueError("Use a new directory under Workbench/runs")
    output.mkdir(parents=True, exist_ok=False)
    cfg = resolve_runtime_config({"runtime_config": "config/qgis344_runtime.json", "engine": {"mode": "default"}}, ROOT)
    exe = Path(cfg["model"]["executable"])
    (output / "engine").mkdir()
    saved_exe = output / "engine" / exe.name
    shutil.copy2(exe, saved_exe)
    info = dict(cfg["engine_info"], executable=str(saved_exe), task_copy=True)
    spec = {"root": str(ROOT), "input_dir": "models/examples/robit344_check_b1/Scenarios/Default/TxtInOut",
            "executable": str(saved_exe), "engine_info": info, "expected_version": "62.0.1",
            "start_year": 1990, "end_year": 1991, "warmup_years": 1,
            "frequency": "monthly", "parameter_backend": "native_calibration",
            "outlet": {"unit": 1, "gis_id": 1, "name": "cha01"}, "timeout_seconds": 300}
    singles = [("lat_len", "pctchg", -30), ("bd", "pctchg", 10),
               ("alb", "absval", .2), ("evrch", "absval", .9)]
    variants = [("baseline", [])] + [(name, [{"native_name": name, "method": method, "value": value}])
                                     for name, method, value in singles]
    combined = [{"native_name": p["native_name"], "object_type": p["object_type"],
                 "method": p["suggested_search"]["method"],
                 "value": (p["suggested_search"]["lower"] + p["suggested_search"]["upper"]) / 2}
                for p in parameter_catalog("62.0.1")["parameters"]]
    variants.append((f"combined_{len(combined)}", combined))
    report = {"purpose": "parameter interface and output response, not calibration or model skill validation",
              "version": "62.0.1", "engine": info, "input_dir": spec["input_dir"],
              "simulation": {"start_year": 1990, "end_year": 1991, "warmup_years": 1},
              "added_parameters": sorted(NEW), "post_change_internal_values_dumped": False, "results": []}
    baseline, baseline_outlet = None, None
    for label, definitions in variants:
        print("Starting " + label, flush=True)
        trial = run_trial(dict(spec, work_dir=str(output / label), parameters=definitions), ROOT)
        current = diagnostics(Path(trial["model_dir"]))
        if baseline is None:
            baseline = current
        records = outlet_series(trial["outlet_csv"])
        if baseline_outlet is None:
            baseline_outlet = records
        if records.keys() != baseline_outlet.keys():
            raise ValueError("Outlet dates differ")
        entry = {"case": label, "parameters": definitions, "status": trial["status"],
                 "records": len(records), "elapsed_seconds": trial["elapsed_seconds"],
                 "max_outlet_flow_difference_m3_s": max(abs(value - baseline_outlet[date]) for date, value in records.items()),
                 "max_absolute_output_differences": differences(baseline, current),
                 "trial_result": str(output / label / "trial_result.json")}
        report["results"].append(entry)
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    print("Saved " + str(output / "summary.json"), flush=True)


if __name__ == "__main__":
    main()
