"""Complete an existing QSWAT+ GIS database with Editor CLI and export inputs.

Uses the installed Editor's documented CLI actions. SQLite updates only target
the generated project and the exact time/output fields inspected in Editor 2.3.3.
All configured paths resolve relative to this workspace (config/.. by default).
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time


def row_count(connection, table):
    return connection.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]


def build(config_path):
    config_path = Path(config_path).resolve()
    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    root = (config_path.parent / cfg.get("workspace_root", "..")).resolve()

    def path(value):
        return (root / value).resolve()

    db = path(cfg["project_db"])
    datasets = path(cfg["datasets_db"])
    api = path(cfg["editor_api"])
    output = path(cfg["output_dir"])
    weather_output = path(cfg["weather_output_dir"])
    log_dir = path(cfg["log_dir"]) / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_dir = path(cfg["temp_dir"])
    weather = cfg["weather"]
    sim = cfg["simulation"]
    for p in (db, datasets, api, path(weather["source_dir"]),
              path(weather["wgn_station_csv"]), path(weather["wgn_monthly_csv"])):
        if not p.exists():
            raise FileNotFoundError(p)
    # QSWAT+ already copies this reference DB beside the GIS project. Editor
    # uses os.path.relpath internally and cannot use a reference on another drive.
    if db.drive.lower() != datasets.drive.lower():
        raise ValueError("Project and datasets databases must be on the same drive")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Export directory is not empty; choose a new directory: {output}")
    with sqlite3.connect(db) as c:
        counts = {t: row_count(c, t) for t in
                  ("gis_subbasins", "gis_channels", "gis_hrus", "gis_routing")}
        if not all(counts.values()):
            raise ValueError(f"QSWAT+ GIS construction is incomplete: {counts}")
    for p in (output, weather_output, log_dir, temp_dir):
        p.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(TEMP=str(temp_dir), TMP=str(temp_dir))
    commands = []

    def call(action, **kwargs):
        cmd = [str(api), action] + [f"--{k}={v}" for k, v in kwargs.items()]
        started = time.perf_counter()
        print(f"Editor: {action}", flush=True)
        result = subprocess.run(cmd, cwd=db.parent, env=env, capture_output=True,
                                timeout=cfg.get("timeout_seconds", 600))
        label = f"{len(commands) + 1:02d}_{action}"
        (log_dir / f"{label}.stdout.log").write_bytes(result.stdout)
        (log_dir / f"{label}.stderr.log").write_bytes(result.stderr)
        commands.append({"command": cmd, "exit_code": result.returncode,
                         "seconds": round(time.perf_counter() - started, 3)})
        (log_dir / "commands.json").write_text(
            json.dumps(commands, indent=2, ensure_ascii=False), encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"Editor {action} failed; see {log_dir / (label + '.stderr.log')}")

    call("setup_project", project_db_file=db, datasets_db_file=datasets,
         project_name=cfg["project_name"], editor_version=cfg["editor_version"], is_lte="n")
    with sqlite3.connect(db) as c:
        imported = c.execute("SELECT imported_gis FROM project_config").fetchone()[0]
    # setup_project normally imports a completed GIS project itself. This
    # explicit action handles an existing initialized project not yet imported.
    if not imported:
        call("import_gis", project_db_file=db, delete_existing="y", is_lte="n")
    with sqlite3.connect(db) as c:
        editor_counts = {t: row_count(c, t) for t in
                         ("hru_data_hru", "hru_con", "channel_lte_cha", "ls_unit_ele")}
        if not all(editor_counts.values()):
            raise RuntimeError(f"Editor GIS import did not create required objects: {editor_counts}")
        c.execute("UPDATE project_config SET weather_data_dir=?, weather_data_format=?, input_files_dir=?",
                  (os.path.relpath(weather_output, db.parent), "swatplus",
                   os.path.relpath(output, db.parent)))
    call("import_weather", project_db_file=db, delete_existing="y", import_type="wgn",
         create_stations="n", import_method="two_file",
         file1=path(weather["wgn_station_csv"]), file2=path(weather["wgn_monthly_csv"]))
    call("import_weather", project_db_file=db, delete_existing="y",
         import_type="observed2012", create_stations="y", source_dir=path(weather["source_dir"]))
    # The weather import sets time_sim to its entire date range; set the requested
    # experiment period afterwards. Warm-up means skipping printed output only.
    with sqlite3.connect(db) as c:
        c.execute("UPDATE time_sim SET day_start=0, yrc_start=?, day_end=0, yrc_end=?, step=0",
                  (sim["start_year"], sim["end_year"]))
        c.execute("UPDATE print_prt SET nyskip=?, day_start=0, yrc_start=0, day_end=0, yrc_end=0, interval=1",
                  (sim["warmup_years"],))
        c.execute("UPDATE print_prt_object SET daily=0, monthly=0, yearly=0, avann=0")
        for name in sim.get("print_objects", ["basin_wb", "channel_sd"]):
            changed = c.execute("UPDATE print_prt_object SET daily=1, monthly=1, yearly=1, avann=1 WHERE name=?",
                                (name,)).rowcount
            if changed != 1:
                raise ValueError(f"Unknown print object: {name}")
        stations = c.execute("SELECT name,wgn_id,pcp,tmp,slr,hmd,wnd FROM weather_sta_cli").fetchall()
        if not stations or any(any(x is None or str(x).lower() == "sim" for x in row[1:]) for row in stations):
            raise ValueError(f"Incomplete five-variable weather station assignment: {stations}")
        missing_hru_weather = c.execute("SELECT count(*) FROM hru_con WHERE wst_id IS NULL").fetchone()[0]
        if missing_hru_weather:
            raise ValueError(f"{missing_hru_weather} HRUs lack a weather station")
        weather_counts = {t: row_count(c, t) for t in
                          ("weather_sta_cli", "weather_wgn_cli", "weather_wgn_cli_mon", "weather_file")}
    call("write_files", project_db_file=db, swat_version=cfg["swat_version"])
    for name in ("file.cio", "time.sim", "print.prt", "hru-data.hru", "soils.sol", "channel-lte.cha"):
        if not (output / name).is_file():
            raise FileNotFoundError(output / name)
    summary = {"project_db": str(db), "output_dir": str(output), "editor_version": cfg["editor_version"],
               "swat_version": cfg["swat_version"], "simulation": sim, "gis_counts": counts,
               "editor_counts": editor_counts, "weather_counts": weather_counts,
               "weather_stations": stations, "input_files": len(list(output.iterdir())),
               "log_dir": str(log_dir), "model_run_performed": False}
    (log_dir / "export_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    build(parser.parse_args().config)
