"""Run isolated SWAT+ cases and extract genuine daily WB / monthly outlet flow.

Standard library only. All paths in the config resolve relative to this workspace.
Changes affect exported TxtInOut files, not an Editor project database.
"""
import argparse
import calendar
import csv
import datetime as dt
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time


def lines(path):
    # This public archive includes CRCRLF. Normalize in memory for parsing only.
    return path.read_bytes().decode("utf-8-sig").replace("\r\r\n", "\n").replace("\r\n", "\n").splitlines()


def write_lines(path, content):
    path.write_text("\n".join(content) + "\n", encoding="utf-8")


def replace_token(line, index, value):
    spans = list(re.finditer(r"\S+", line))
    match = spans[index]
    return line[:match.start()] + str(value).rjust(match.end() - match.start()) + line[match.end():]


def setup_case(source, target, config, changed):
    target.mkdir(parents=True, exist_ok=False)
    count = 0
    for src in source.iterdir():
        if src.is_file() and src.suffix.lower() in config["input_extensions"]:
            shutil.copy2(src, target / src.name)
            count += 1
    cio = lines(target / "file.cio")
    for line in cio[1:]:
        parts = line.split()
        if parts and parts[0] == "chg" and parts[2].lower() != "null":
            raise ValueError("An active calibration.cal could override direct parameter edits")
        for name in parts[1:]:
            if name.lower() != "null" and not (target / name).is_file():
                raise ValueError(f"file.cio dependency missing in isolated directory: {name}")
    t = lines(target / "time.sim")
    write_lines(target / "time.sim", [t[0], t[1], f"0 {config['start_year']} 0 {config['end_year']} 0"])
    p = lines(target / "print.prt")
    p[2] = f"{config['warmup_years']} 0 0 0 0 1"
    p[6] = "n n n"
    p[8] = "n n n n"
    for i in range(10, len(p)):
        fields = p[i].split()
        if not fields:
            continue
        obj = fields[0]
        flags = "n n n n"
        if obj == "basin_wb":
            flags = "y y y y"
        elif obj in ("basin_aqu", "basin_res", "channel_sd"):
            flags = "n y y y"
        p[i] = f"{obj:24s} {flags}"
    write_lines(target / "print.prt", p)
    hyd = lines(target / config["parameter_file"])
    index = hyd[1].split().index(config["parameter"])
    changes = []
    for i in range(2, len(hyd)):
        row = hyd[i].split()
        if not row:
            continue
        old = float(row[index])
        if not math.isclose(old, config["control_value"], abs_tol=1e-8):
            raise ValueError(f"Unexpected original {config['parameter']} in {row[0]}: {old}")
        if changed:
            new = config["changed_value"]
            if not config["parameter_bounds"][0] <= new <= config["parameter_bounds"][1]:
                raise ValueError("Parameter outside configured physical bounds")
            hyd[i] = replace_token(hyd[i], index, f"{new:.5f}")
            changes.append({"object": row[0], "old": old, "new": new})
    if changed:
        write_lines(target / config["parameter_file"], hyd)
    return {"input_files": count, "parameter_records": len(hyd) - 2, "modified_records": changes}


def read_table(path):
    with path.open(encoding="utf-8-sig") as stream:
        banner = stream.readline().strip()
        headers = stream.readline().split()
        units = stream.readline().rstrip()
        rows = []
        for number, line in enumerate(stream, 4):
            tokens = line.split()
            if not tokens:
                continue
            if len(tokens) != len(headers):
                raise ValueError(f"{path.name}:{number}: {len(tokens)} values / {len(headers)} headers")
            rows.append(dict(zip(headers, tokens)))
    return banner, headers, units, rows


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


WB_FIELDS = ["precip", "snofall", "snomlt", "surq_gen", "latq", "wateryld", "perc", "et", "ecanopy", "eplant", "esoil", "sw_init", "sw_final", "sno_init", "sno_final", "pet", "irr"]


def extract(case, config, expected_parameter=None):
    banner, header, units, raw = read_table(case / "basin_wb_day.txt")
    if config["expected_version"] not in banner:
        raise ValueError(f"Unexpected SWAT+ output version: {banner}")
    # SWAT WB units line starts after the 7 metadata fields.
    unit_tokens = units.split()
    unit_map = dict(zip(header[7:], unit_tokens))
    if len(unit_tokens) != len(header) - 7 or any(unit_map[k] != "mm" for k in WB_FIELDS):
        raise ValueError("Unexpected daily water-balance units")
    start = dt.date(config["start_year"] + config["warmup_years"], 1, 1)
    end = dt.date(config["end_year"], 12, 31)
    daily = []
    for row in raw:
        date = dt.date(int(row["yr"]), int(row["mon"]), int(row["day"]))
        if int(row["unit"]) != 1 or int(row["jday"]) != date.timetuple().tm_yday:
            raise ValueError("Unexpected basin ID or day-of-year")
        values = {name: float(row[name]) for name in WB_FIELDS}
        if any(not math.isfinite(v) or v < -0.001 for v in values.values()):
            raise ValueError(f"Nonfinite or negative WB value at {date}")
        daily.append({"date": date.isoformat(), "unit": int(row["unit"]), "gis_id": int(row["gis_id"]), "name": row["name"], **values})
    expected = [(start + dt.timedelta(days=i)).isoformat() for i in range((end-start).days + 1)]
    if [r["date"] for r in daily] != expected:
        raise ValueError("Daily dates incomplete, duplicated or unordered")
    _, channel_header, channel_units, channel = read_table(case / "channel_sd_mon.txt")
    # Placeholder 'null' columns have no unit token in rev.61.0.1.
    channel_variables = [h for h in channel_header[7:] if h != "null"]
    if len(channel_variables) != len(channel_units.split()):
        raise ValueError("Unexpected channel unit alignment")
    channel_unit_map = dict(zip(channel_variables, channel_units.split()))
    if channel_unit_map["flo_out"] != "m^3/s":
        raise ValueError("Channel units do not identify flow as m^3/s")
    monthly = []
    for row in channel:
        if int(row["unit"]) == config["outlet_unit"]:
            if int(row["gis_id"]) != config["outlet_gis_id"] or row["name"] != config["outlet_name"]:
                raise ValueError("Outlet ID mapping disagrees with config")
            value = float(row["flo_out"])
            if not math.isfinite(value) or value < 0:
                raise ValueError("Invalid monthly outlet flow")
            monthly.append({"month": f"{int(row['yr']):04d}-{int(row['mon']):02d}", "unit": int(row["unit"]), "gis_id": int(row["gis_id"]), "name": row["name"], "flo_out_m3_s": value})
    expected_months = [f"{y:04d}-{m:02d}" for y in range(start.year, end.year + 1) for m in range(1, 13)]
    if [r["month"] for r in monthly] != expected_months:
        raise ValueError("Incomplete or duplicate outlet months")
    write_csv(case.parent / (case.name + "_basin_daily.csv"), daily)
    write_csv(case.parent / (case.name + "_outlet_monthly.csv"), monthly)
    check = lines(case / "checker.out")
    check_index = check[1].split().index(config["parameter"]) + 2
    internal_values = [float(row.split()[check_index]) for row in check[3:] if row.strip()]
    expected_hru_count = len([row for row in lines(case / "hru-data.hru")[2:] if row.strip()])
    if len(internal_values) != expected_hru_count:
        raise ValueError("checker.out does not cover every HRU")
    if expected_parameter is not None and any(not math.isclose(v, expected_parameter, abs_tol=1e-6) for v in internal_values):
        raise ValueError("Model-internal parameter values disagree with intended edit")
    totals = {k: sum(r[k] for r in daily) for k in WB_FIELDS if k not in ("sw_init", "sw_final", "sno_init", "sno_final")}
    # Diagnostic for land/soil/snow accounting only, not a full basin closure.
    storage_change = daily[-1]["sw_final"] - daily[0]["sw_init"] + daily[-1]["sno_final"] - daily[0]["sno_init"]
    residual = totals["precip"] + totals["irr"] - totals["et"] - totals["surq_gen"] - totals["latq"] - totals["perc"] - storage_change
    month_days = [calendar.monthrange(*map(int, r["month"].split("-")))[1] for r in monthly]
    weighted_flow = sum(r["flo_out_m3_s"] * days for r, days in zip(monthly, month_days)) / sum(month_days)
    diagnostic_lines = [x.strip() for x in lines(case / "diagnostics.out")[1:] if x.strip()]
    return {
        "banner": banner, "daily_records": len(daily), "first_date": expected[0], "last_date": expected[-1],
        "monthly_records": len(monthly), "totals_mm": totals,
        "internal_parameter_check": {"file": "checker.out", "parameter": config["parameter"],
                                     "hru_records": len(internal_values), "unique_values": sorted(set(internal_values))},
        "soil_snow_storage_change_mm": storage_change,
        "land_accounting_residual_mm": residual,
        "land_accounting_residual_note": "P+irrigation-ET-generated_surface_runoff-lateral_flow-percolation-delta(soil+snow). Excludes canopy/wetland/routing/aquifer/reservoir storage and other transfers; not complete basin water closure.",
        "mean_of_monthly_outlet_flow_m3_s": statistics.mean(r["flo_out_m3_s"] for r in monthly),
        "day_weighted_outlet_flow_m3_s": weighted_flow,
        "diagnostics": {"message_count": len(diagnostic_lines),
                        "res_hyd_not_found": sum("not found (res-hyd)" in x for x in diagnostic_lines),
                        "res_sed_not_found": sum("not found (res-sed)" in x for x in diagnostic_lines),
                        "res_nut_not_found": sum("not found (res-nut)" in x for x in diagnostic_lines),
                        "note": "Original diagnostic messages retained in diagnostics.out; see execution report for source-code interpretation."}
    }, daily, monthly


def run_case(source, executable, case, config, changed):
    info = setup_case(source, case, config, changed)
    start = time.perf_counter()
    with (case.parent / (case.name + "_console.log")).open("w", encoding="utf-8") as log:
        completed = subprocess.run([str(executable)], cwd=case, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT,
                                   timeout=config["timeout_seconds"], check=False)
    info.update(elapsed_seconds=round(time.perf_counter()-start, 3), exit_code=completed.returncode)
    if completed.returncode != 0 or not (case / "success.fin").is_file():
        raise RuntimeError(f"SWAT+ failed in {case}; see console log; exit {completed.returncode}")
    summary, daily, monthly = extract(case, config, config["changed_value"] if changed else config["control_value"])
    info.update(summary)
    print(f"{case.name}: {info['elapsed_seconds']} s, {info['daily_records']} daily + {info['monthly_records']} outlet monthly records", flush=True)
    return info, daily, monthly


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--label", default=dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--repeat-control", action="store_true", help="Third run checks deterministic repeatability")
    args = parser.parse_args()
    config_path = args.config.resolve()
    root = config_path.parent.parent
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    source = (root / config["source_model"]).resolve()
    executable = (root / config["executable"]).resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        parser.error("label must contain only letters, numbers, hyphens and underscores")
    run = root / config["run_root"] / args.label
    run.mkdir(parents=True, exist_ok=False)
    (run / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    results = {"config": str(config_path), "python": sys.version, "executable": str(executable), "cases": {}}
    control, daily0, monthly0 = run_case(source, executable, run / "control", config, False)
    results["cases"]["control"] = control
    change, daily1, monthly1 = run_case(source, executable, run / "changed", config, True)
    results["cases"]["changed"] = change
    differences = []
    for a, b in zip(daily0, daily1):
        differences.append({"date": a["date"], **{k + "_delta_mm": b[k]-a[k] for k in WB_FIELDS}})
    write_csv(run / "daily_response.csv", differences)
    write_csv(run / "monthly_response.csv", [{"month": a["month"], "control_m3_s": a["flo_out_m3_s"], "changed_m3_s": b["flo_out_m3_s"], "delta_m3_s": b["flo_out_m3_s"]-a["flo_out_m3_s"]} for a, b in zip(monthly0, monthly1)])
    delta = {k: change["totals_mm"][k]-v for k, v in control["totals_mm"].items()}
    results["response"] = {"totals_delta_mm": delta, "changed_et_days": sum(abs(r["et_delta_mm"]) > 1e-6 for r in differences),
                           "changed_outlet_months": sum(a["flo_out_m3_s"] != b["flo_out_m3_s"] for a, b in zip(monthly0, monthly1))}
    if daily0 == daily1 or results["response"]["changed_et_days"] == 0:
        raise RuntimeError("No ET response; investigate parameter before claiming success")
    if any(a["precip"] != b["precip"] for a, b in zip(daily0, daily1)):
        raise RuntimeError("Precipitation differs between parameter cases")
    if args.repeat_control:
        repeat, daily2, monthly2 = run_case(source, executable, run / "control_repeat", config, False)
        results["cases"]["control_repeat"] = repeat
        results["repeat_control_identical"] = daily0 == daily2 and monthly0 == monthly2
        if not results["repeat_control_identical"]:
            raise RuntimeError("Control repeats differ; parameter response may be confounded by stochastic variation")
    (run / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run": str(run), "response": results["response"], "repeat_control_identical": results.get("repeat_control_identical")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
