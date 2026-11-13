"""Compare native and direct ESCO/PERCO runs on the two existing clean exports.

This is an interface equivalence experiment, not calibration or validation of
hydrological skill. All calculations use new directories under runs/. Original
models, prior trials, and model installations remain unchanged.

python -X utf8 scripts/smoke_native_calibration.py
"""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path

from swatplus_trial import run_trial


ROOT = Path(__file__).resolve().parents[1]


def example_spec(example, output=None):
    """Build a short experiment from retained inputs, not a disposable old trial."""
    cfg=json.loads((ROOT/"config"/f"{example}_one_stop.json").read_text(encoding="utf-8"))
    if example=="robit":
        from desktop_data import job_state_path
        retained=json.loads(job_state_path(ROOT/"runs/robit_research_v1").read_text(encoding="utf-8"))
        source=ROOT/"runs/robit_research_v1/model_inputs"
        outlet=retained["outlet"]
    else:
        source=ROOT/cfg["model"]["input_dir"]
        outlet=cfg["model"]["outlet"]
        if output is not None:
            # The downloaded example also contains old outputs. Recreate a
            # clean export only for an explicitly requested new experiment.
            from swatplus_workflow import prepare_input_snapshot
            clean=Path(output)/example/"model_inputs"
            prepare_input_snapshot(source,clean,cfg["model"]["input_extensions"])
            source=clean
    return {"root":str(ROOT),"input_dir":str(source),
            "executable":str(ROOT/cfg["model"]["executable"]),
            "expected_version":cfg["model"]["expected_version"],"outlet":outlet,
            "start_year":cfg["simulation"]["start_year"],"end_year":cfg["simulation"]["start_year"]+1,
            "warmup_years":1,"frequency":"monthly","timeout_seconds":300,"parameters":[]}


def run(example, output):
    spec = example_spec(example, output)
    spec.update(end_year=spec["start_year"] + 1, warmup_years=1)
    spec["parameters"] = [{"file": "hydrology.hyd", "column": name, "method": "absolute", "value": value}
                          for name, value in (("esco", 0.73), ("perco", 0.63))]
    results = {}
    series = {}
    for backend in ("direct_file", "native_calibration"):
        print(f"{example}: running {backend} ({spec['expected_version']})", flush=True)
        spec.update(parameter_backend=backend, work_dir=str(output / example / backend))
        results[backend] = run_trial(spec, ROOT)
        with Path(results[backend]["outlet_csv"]).open(encoding="utf-8-sig") as stream:
            series[backend] = {row["date"]: float(row["flow_m3_s"]) for row in csv.DictReader(stream)}
    direct, native = series["direct_file"], series["native_calibration"]
    if direct.keys() != native.keys():
        raise RuntimeError("Run output dates do not match")
    differences = [abs(direct[date] - native[date]) for date in direct]
    summary = {"example": example, "expected_version": spec["expected_version"],
               "purpose": "small interface experiment, not a calibrated hydrological result",
               "years": [spec["start_year"], spec["end_year"]], "warmup_years": 1,
               "parameters": spec["parameters"], "records": len(differences), "frequency": "monthly",
               "max_absolute_flow_difference_m3_s": max(differences),
               "identical_at_output_precision": all(value == 0 for value in differences),
               "direct_elapsed_seconds": results["direct_file"]["elapsed_seconds"],
               "native_elapsed_seconds": results["native_calibration"]["elapsed_seconds"],
               "native_parameter_verification_note": results["native_calibration"]["parameter_verification_note"],
               "native_initialization_checks": results["native_calibration"].get("initialization_checks", [])}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", choices=["oulanka", "robit", "both"], default="both")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "runs" / ("native_adapter_check_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    output = output.resolve()
    if ROOT / "runs" not in output.parents:
        raise ValueError("Smoke output must be a new directory inside this workspace's runs")
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for example in (["oulanka", "robit"] if args.example == "both" else [args.example]):
        results.append(run(example, output))
        (output / "comparison.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Comparison saved: {output / 'comparison.json'}", flush=True)


if __name__ == "__main__":
    main()
