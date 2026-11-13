"""Run the separate SWAT+ 62 model and extract day/month flow and basin water."""
import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path
import shutil
import subprocess
import time

from swat344_runtime import load_config, output_rows, read_output_header


def text_lines(path):
    return Path(path).read_text(encoding="utf-8-sig").splitlines()


def write_csv(path, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def identify_outlet(source):
    records = text_lines(source / "chandeg.con")
    header = records[1].split()
    outlets = [dict(zip(header, row.split())) for row in records[2:]
               if row.strip() and int(row.split()[header.index("out_tot")]) == 0]
    if len(outlets) != 1:
        raise ValueError(f"Expected one unrouted channel, found {len(outlets)}")
    return {"outlet_unit": int(outlets[0]["id"]), "outlet_gis_id": int(outlets[0]["gis_id"]),
            "outlet_name": outlets[0]["name"]}


def expected_dates(cfg, frequency):
    first_year = cfg["start_year"] + cfg["warmup_years"]
    if frequency == "daily":
        start, end = dt.date(first_year, 1, 1), dt.date(cfg["end_year"] + 1, 1, 1)
        return [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days)]
    return [f"{year:04d}-{month:02d}-01" for year in range(first_year, cfg["end_year"] + 1)
            for month in range(1, 13)]


def extract_outlet(path, cfg, frequency, version):
    result = []
    with Path(path).open(encoding="utf-8-sig") as stream:
        banner, header, _ = read_output_header(stream, version, "channel_sd")
        for row in output_rows(stream, header):
            if int(row["unit"]) != cfg["outlet_unit"]:
                continue
            if int(row["gis_id"]) != cfg["outlet_gis_id"] or row["name"] != cfg["outlet_name"]:
                raise ValueError("Outlet identifiers disagree")
            day = dt.date(int(row["yr"]), int(row["mon"]), int(row["day"]) if frequency == "daily" else 1)
            flow = float(row["flo_out"])
            if not math.isfinite(flow) or flow < 0:
                raise ValueError("Invalid outlet flow")
            result.append({"date": day.isoformat(), "unit": int(row["unit"]), "gis_id": int(row["gis_id"]),
                           "name": row["name"], "flow_m3_s": flow})
    if [row["date"] for row in result] != expected_dates(cfg, frequency):
        raise ValueError(f"Incomplete {frequency} output dates")
    return banner, result


def run_model(config_path, extract_only=False, check_only=False):
    root, cfg, runtime = load_config(config_path, require_model=True)
    source, run, exe = [(root / item).resolve() for item in
                        (cfg["input_dir"], cfg["run_dir"], runtime["model_executable"])]
    if not exe.is_file():
        raise FileNotFoundError(exe)
    for line in text_lines(source / "file.cio")[1:]:
        for filename in line.split()[1:]:
            if filename != "null" and not (source / filename).is_file():
                raise FileNotFoundError(source / filename)
    sim_rows = text_lines(source / "time.sim")
    sim = dict(zip(sim_rows[1].split(), sim_rows[2].split()))
    print_rows = text_lines(source / "print.prt")
    output_settings = dict(zip(print_rows[1].split(), print_rows[2].split()))
    if (int(sim["yrc_start"]) != cfg["start_year"] or int(sim["yrc_end"]) != cfg["end_year"]
            or int(output_settings["nyskip"]) != cfg["warmup_years"]):
        raise ValueError("Exported simulation period differs from run configuration")
    if cfg.get("outlet") == "auto":
        cfg.update(identify_outlet(source))
    if check_only:
        summary = {"check_only": True, "executable": str(exe), "version": runtime["model_version"],
                   "input_dir": str(source), "run_dir": str(run), "outlet": cfg["outlet_name"]}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary
    model = run / "TxtInOut"
    if not extract_only:
        if any((source / name).exists() for name in ("success.fin", "channel_sd_day.txt", "basin_wb_day.txt")):
            raise ValueError("Source must be a clean Editor export")
        run.mkdir(parents=True, exist_ok=False)
        shutil.copytree(source, model)
        record = dict(cfg, executable=str(exe), expected_version=runtime["model_version"])
        (run / "config.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        started = time.perf_counter()
        with (run / "console.log").open("w", encoding="utf-8") as log:
            result = subprocess.run([str(exe)], cwd=model, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=cfg["timeout_seconds"],
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = {"elapsed_seconds": round(time.perf_counter() - started, 3), "exit_code": result.returncode}
        (run / "run_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        if result.returncode or not (model / "success.fin").exists():
            raise RuntimeError("Model did not complete; inspect console.log")
    info = json.loads((run / "run_info.json").read_text(encoding="utf-8"))
    version = runtime["model_version"]
    banner, daily = extract_outlet(model / "channel_sd_day.txt", cfg, "daily", version)
    _, monthly = extract_outlet(model / "channel_sd_mon.txt", cfg, "monthly", version)
    with (model / "basin_wb_day.txt").open(encoding="utf-8-sig") as stream:
        _, header, units = read_output_header(stream, version, "basin_wb")
        columns = ["precip", "et", "surq_gen", "latq", "perc", "wateryld", "pet", "irr"]
        if any(units.get(name) != "mm" for name in columns):
            raise ValueError("Unexpected basin water units")
        basin = []
        for row in output_rows(stream, header):
            day = dt.date(int(row["yr"]), int(row["mon"]), int(row["day"]))
            values = {name: float(row[name]) for name in columns}
            if any(not math.isfinite(value) or value < 0 for value in values.values()):
                raise ValueError("Invalid basin water value")
            basin.append({"date": day.isoformat(), **values})
    if [row["date"] for row in basin] != [row["date"] for row in daily]:
        raise ValueError("Basin and outlet dates disagree")
    write_csv(run / "outlet_daily.csv", daily)
    write_csv(run / "outlet_monthly.csv", monthly)
    write_csv(run / "basin_daily.csv", basin)
    summary = {
        "banner": banner, "run_info": info, "engine_info": cfg.get("engine_info", {}), "input_dir": str(source), "executable": str(exe),
        "input_files": len(list(source.iterdir())), "daily_records": len(daily), "monthly_records": len(monthly),
        "start": daily[0]["date"], "end": daily[-1]["date"],
        "mean_flow_m3_s": sum(row["flow_m3_s"] for row in daily) / len(daily),
        "minimum_flow_m3_s": min(row["flow_m3_s"] for row in daily),
        "maximum_flow_m3_s": max(row["flow_m3_s"] for row in daily),
        "basin_totals_mm": {name: sum(row[name] for row in basin) for name in columns},
        "diagnostics": text_lines(model / "diagnostics.out")[1:],
        "meaning": "Windows QGIS 3.44 / QSWAT+ 4 / Editor 4 / SWAT+ 62 execution check; not calibration."
    }
    (run / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="Read-only export/path checks; do not run model")
    arguments = parser.parse_args()
    run_model(arguments.config, arguments.extract_only, arguments.check_only)
