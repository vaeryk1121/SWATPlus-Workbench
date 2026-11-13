"""Run a newly exported SWAT+ model in a fresh copy and extract basin/outlet data."""
import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path
import shutil
import subprocess
import time

from run_parameter_demo import lines, write_csv, read_table


def extract_outlet(path, config, frequency):
    result = []
    with path.open(encoding="utf-8-sig") as stream:
        banner, header, units = next(stream).strip(), next(stream).split(), next(stream).split()
        if config["expected_version"] not in banner:
            raise ValueError("Unexpected model version")
        variables = [x for x in header[7:] if x != "null"]
        if len(variables) != len(units) or dict(zip(variables, units))["flo_out"] != "m^3/s":
            raise ValueError("Unexpected flow units")
        for line in stream:
            fields = line.split()
            if not fields or int(fields[header.index("unit")]) != config["outlet_unit"]:
                continue
            row = dict(zip(header, fields))
            if int(row["gis_id"]) != config["outlet_gis_id"] or row["name"] != config["outlet_name"]:
                raise ValueError("Outlet identifiers disagree")
            date = dt.date(int(row["yr"]),int(row["mon"]),int(row["day"]) if frequency == "daily" else 1)
            flow = float(row["flo_out"])
            if not math.isfinite(flow) or flow < 0:
                raise ValueError("Invalid flow")
            result.append({"date":date.isoformat(),"unit":int(row["unit"]),"gis_id":int(row["gis_id"]),"name":row["name"],"flow_m3_s":flow})
    start_year = config["start_year"]+config["warmup_years"]
    if frequency == "daily":
        start = dt.date(start_year,1,1)
        n = (dt.date(config["end_year"],12,31)-start).days+1
        expected = [(start+dt.timedelta(days=i)).isoformat() for i in range(n)]
    else:
        expected = [f"{year:04d}-{month:02d}-01" for year in range(start_year,config["end_year"]+1) for month in range(1,13)]
    if [x["date"] for x in result] != expected:
        raise ValueError("Incomplete output dates")
    return banner, result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",type=Path,required=True)
    p.add_argument("--extract-only",action="store_true")
    args = p.parse_args()
    root = args.config.resolve().parent.parent
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    source,run,exe = [(root/cfg[k]).resolve() for k in ["input_dir","run_dir","executable"]]
    model = run/"TxtInOut"
    if not args.extract_only:
        if (source/"success.fin").exists() or (source/"channel_sd_day.txt").exists():
            raise ValueError("Use a clean Editor export as source, not an old run directory")
        run.mkdir(parents=True,exist_ok=False)
        shutil.copytree(source,model)
        (run/"config.json").write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding="utf-8")
        for line in lines(model/"file.cio")[1:]:
            for filename in line.split()[1:]:
                if filename != "null" and not (model/filename).is_file():
                    raise FileNotFoundError(filename)
        start=time.perf_counter()
        with (run/"console.log").open("w",encoding="utf-8") as log:
            proc=subprocess.run([str(exe)],cwd=model,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,timeout=cfg["timeout_seconds"])
        info={"elapsed_seconds":round(time.perf_counter()-start,3),"exit_code":proc.returncode}
        (run/"run_info.json").write_text(json.dumps(info,indent=2),encoding="utf-8")
        if proc.returncode or not (model/"success.fin").exists():
            raise RuntimeError("Model did not complete; inspect console.log")
    info=json.loads((run/"run_info.json").read_text())
    banner,daily=extract_outlet(model/"channel_sd_day.txt",cfg,"daily")
    _,monthly=extract_outlet(model/"channel_sd_mon.txt",cfg,"monthly")
    write_csv(run/"outlet_daily.csv",daily)
    write_csv(run/"outlet_monthly.csv",monthly)
    wb_banner,header,units,rows=read_table(model/"basin_wb_day.txt")
    unit_map=dict(zip(header[7:],units.split()))
    columns=["precip","et","surq_gen","latq","perc","wateryld","pet","irr"]
    if any(unit_map[x]!="mm" for x in columns):
        raise ValueError("Unexpected basin water units")
    basin=[]
    for row in rows:
        day=dt.date(int(row["yr"]),int(row["mon"]),int(row["day"]))
        values={x:float(row[x]) for x in columns}
        if any(not math.isfinite(v) or v < 0 for v in values.values()):
            raise ValueError("Invalid basin water value")
        basin.append({"date":day.isoformat(),**values})
    if [x["date"] for x in basin]!=[x["date"] for x in daily]:
        raise ValueError("Basin and channel dates disagree")
    write_csv(run/"basin_daily.csv",basin)
    totals={x:sum(r[x] for r in basin) for x in columns}
    summary={"banner":banner,"run_info":info,"input_files":len(list(source.iterdir())),
             "daily_records":len(daily),"monthly_records":len(monthly),"start":daily[0]["date"],"end":daily[-1]["date"],
             "mean_flow_m3_s":sum(r["flow_m3_s"] for r in daily)/len(daily),
             "minimum_flow_m3_s":min(r["flow_m3_s"] for r in daily),"maximum_flow_m3_s":max(r["flow_m3_s"] for r in daily),
             "basin_totals_mm":totals,"diagnostics":lines(model/"diagnostics.out")[1:],
             "meaning":"First model built from source spatial data and weather; execution verified, not calibrated or independently validated."}
    (run/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
