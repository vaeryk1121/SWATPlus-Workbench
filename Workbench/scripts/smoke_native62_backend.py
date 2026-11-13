"""Small SWAT+ 62 binary/interface exercise, not a calibration search.

Run from the workspace: python -X utf8 scripts/smoke_native62_backend.py
Eight independent trials: baseline, six research parameters, version-filtered catalog.
"""
import csv
import json
from pathlib import Path
import sys

from native_calibration import parameter_catalog
from swatplus_trial import run_trial

ROOT = Path(__file__).resolve().parents[1]


def series(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return {row["date"]: float(row["flow_m3_s"]) for row in csv.DictReader(stream)}


def main():
    output = ROOT / "runs/native62_backend_20261021"
    output.mkdir(exist_ok=False)
    runtime = json.loads((ROOT / "config/qgis344_runtime.json").read_text(encoding="utf-8"))
    spec = {
        "root": str(ROOT),
        "input_dir": "models/examples/robit344_check_b1/Scenarios/Default/TxtInOut",
        "executable": runtime["model_executable"],
        "expected_version": "62.0.1",
        "start_year": 1990, "end_year": 1991, "warmup_years": 1,
        "frequency": "monthly", "parameter_backend": "native_calibration",
        "outlet": {"unit": 1, "gis_id": 1, "name": "cha01"},
        "timeout_seconds": 300,
        "output_targets": [
            {"id": "flow", "object": "channel_sd", "frequency": "monthly",
             "variable": "flo_out", "unit_id": 1, "gis_id": 1, "name": "cha01", "unit": "m3/s"},
            {"id": "et", "object": "basin_wb", "frequency": "monthly",
             "variable": "et", "unit_id": 1, "unit": "mm"},
            {"id": "hru_et", "object": "hru_wb", "frequency": "monthly",
             "variable": "et", "unit_id": 1, "gis_id": 1, "unit": "mm"},
        ],
    }
    singles = [("esco", "absval", .5), ("perco", "absval", .4),
               ("cn2", "pctchg", -15), ("awc", "pctchg", 20),
               ("epco", "absval", .9), ("chn", "absval", .12)]
    variants = [("baseline", [])]
    variants += [(name, [{"native_name": name, "method": method, "value": value}])
                 for name, method, value in singles]
    combined = []
    for item in parameter_catalog("62.0.1")["parameters"]:
        bounds = item["suggested_search"]
        combined.append({"native_name": item["native_name"], "object_type": item["object_type"],
                         "method": bounds["method"], "value": (bounds["lower"] + bounds["upper"]) / 2})
    variants.append((f"all_{len(combined)}", combined))
    results, baseline = [], None
    for label, definitions in variants:
        print(f"SWAT+62 interface: {label}", flush=True)
        result = run_trial({**spec, "work_dir": str(output/label), "parameters": definitions})
        values = series(result["outlet_csv"])
        if baseline is None:
            baseline = values
        assert values.keys() == baseline.keys()
        difference = max(abs(value - baseline[date]) for date, value in values.items())
        results.append({
            "case": label, "status": result["status"], "records": len(values),
            "parameters": definitions, "max_flow_difference_m3_s": difference,
            "outlet_changed_at_output_precision": difference > 0,
            "elapsed_seconds": result["elapsed_seconds"],
            "target_csvs": result.get("target_csvs"),
            "trial_result": str(output/label/"trial_result.json"),
            "post_change_internal_values_verified": False,
        })
        (output/"summary.json").write_text(json.dumps(
            {"purpose": "binary/interface checks, not calibration or hydrological validation",
             "engine": runtime["model_executable"], "version": "62.0.1",
             "simulation": "1990-1991, one warmup year, 12 monthly outputs",
             "results": results}, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({"case": label, "max_flow_difference_m3_s": difference,
                          "elapsed_seconds": result["elapsed_seconds"]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
