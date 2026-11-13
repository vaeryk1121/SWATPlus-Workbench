"""Isolated native-parameter interface experiments on the two existing models.

Runs one baseline and short one-parameter contrasts, plus the entire supported
catalog together. This is binary/interface coverage, NOT hydrological skill
validation or proof of every parameter's sensitivity in every watershed.
"""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path

from native_calibration import parameter_catalog
from swatplus_trial import run_trial
from smoke_native_calibration import example_spec

ROOT = Path(__file__).resolve().parents[1]


def read_series(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return {r["date"]: float(r["flow_m3_s"]) for r in csv.DictReader(stream)}


def run(example, output):
    spec = example_spec(example, output)
    spec.update(end_year=spec["start_year"] + 1, warmup_years=1, parameter_backend="native_calibration")
    variants = [("baseline", [])]
    single = [("cn2", "pctchg", -15), ("awc", "pctchg", 20), ("k", "pctchg", 50),
              ("alpha", "absval", .3), ("chn", "absval", .12), ("surlag", "absval", 10),
              ("epco", "absval", .9), ("latq_co", "absval", .6)]
    if example == "oulanka":
        single = [("snofall_tmp", "absval", -2), ("snomelt_max", "absval", 8)]
    for name, method, value in single:
        variants.append((name, [{"native_name": name, "method": method, "value": value}]))
    all_parameters = []
    for item in parameter_catalog(spec["expected_version"])["parameters"]:
        search = item["suggested_search"]
        all_parameters.append({"native_name": item["native_name"], "object_type": item["object_type"],
                               "method": search["method"], "value": (search["lower"] + search["upper"]) / 2})
    variants.append((f"all_{len(all_parameters)}_parameters", all_parameters))
    baseline, results = None, []
    for label, parameters in variants:
        print(f"{example} {spec['expected_version']}: {label}", flush=True)
        spec.update(parameters=parameters, work_dir=str(output / example / label))
        result = run_trial(spec, ROOT)
        series = read_series(result["outlet_csv"])
        if baseline is None:
            baseline = series
        if series.keys() != baseline.keys():
            raise RuntimeError("Output dates differ from baseline")
        diffs = [abs(series[d] - baseline[d]) for d in baseline]
        summary = {"case": label, "example": example, "version": spec["expected_version"],
                   "parameters": parameters, "elapsed_seconds": result["elapsed_seconds"],
                   "records": len(series), "max_flow_difference_m3_s": max(diffs),
                   "outlet_changed_at_output_precision": any(d > 0 for d in diffs),
                   "binary_finished_successfully": result["status"] == "completed",
                   "internal_parameters_dumped": False, "trial_dir": result["work_dir"]}
        results.append(summary)
        (output / f"{example}_summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: summary[k] for k in ("case", "elapsed_seconds", "records", "max_flow_difference_m3_s")}), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", choices=["robit", "oulanka", "both"], default="both")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = (args.output or ROOT / ("runs/native_parameter_expansion_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
    if ROOT / "runs" not in output.parents:
        raise ValueError("Output must be a new workspace runs directory")
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for example in (["robit", "oulanka"] if args.example == "both" else [args.example]):
        results += run(example, output)
    (output / "summary.json").write_text(json.dumps({"purpose": "interface/binary coverage, not calibration",
         "period": "two simulation years with one warmup year; monthly output", "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved: {output / 'summary.json'}")


if __name__ == "__main__":
    main()
