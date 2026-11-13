"""Export the separate QSWAT+ 4 project through the installed Editor 4 CLI.

Only the configured generated project is updated. The old workbench, its
configuration and the original spatial/weather sources are not changed.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from swat344_runtime import editor_command, load_config


def row_count(connection, table):
    return connection.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]


def require_columns(connection, table, columns):
    actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    missing = set(columns) - actual
    if missing:
        raise ValueError(f"Editor table {table} is missing {sorted(missing)}")


def build(config_path, check_only=False):
    root, cfg, runtime = load_config(config_path, require_editor=True)

    def path(value):
        return (root / value).resolve()

    db, datasets = path(cfg["project_db"]), path(cfg["datasets_db"])
    output, weather_output = path(cfg["output_dir"]), path(cfg["weather_output_dir"])
    weather, sim = cfg["weather"], cfg["simulation"]
    api = editor_command(root, runtime)
    from input_data import check_weather,stage_plus_weather
    weather_check=check_weather(weather,root,sim)
    for target in (db, datasets, path(weather["source_dir"])):
        if not target.exists():
            raise FileNotFoundError(target)
    if db.drive.lower() != datasets.drive.lower():
        raise ValueError("Editor project and datasets databases must be on the same drive")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Export directory is not empty: {output}")
    if not 0 <= sim["warmup_years"] <= sim["end_year"] - sim["start_year"]:
        raise ValueError("Simulation must include at least one output year")
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        counts = {name: row_count(connection, name) for name in
                  ("gis_subbasins", "gis_channels", "gis_hrus", "gis_routing")}
        require_columns(connection, "project_config", ["gis_version", "delineation_done", "hrus_done"])
        gis_version, delineated, hrus_done = connection.execute(
            "SELECT gis_version,delineation_done,hrus_done FROM project_config").fetchone()
        if not all(counts.values()) or not delineated or not hrus_done:
            raise ValueError(f"Spatial construction is incomplete: {counts}")
        if not str(gis_version).startswith("4."):
            raise ValueError(f"This adapter requires QSWAT+ 4 GIS data, found {gis_version}")
    if check_only:
        summary = {"check_only": True, "project_db": str(db), "editor_command": api,
                   "editor_version": runtime["editor_version"], "model_version": runtime["model_version"],
                   "gis_version": gis_version, "gis_counts": counts, "output_dir": str(output)}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary

    log_dir = path(cfg["log_dir"]) / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_dir = path(cfg["temp_dir"])
    for target in (output, weather_output, log_dir, temp_dir):
        target.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(TEMP=str(temp_dir), TMP=str(temp_dir))
    commands = []

    def call(action, **kwargs):
        command = api + [action] + [f"--{key}={value}" for key, value in kwargs.items()]
        started = time.perf_counter()
        print(f"Editor: {action}", flush=True)
        result = subprocess.run(command, cwd=db.parent, env=env, capture_output=True,
                                timeout=cfg.get("timeout_seconds", 600),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        label = f"{len(commands) + 1:02d}_{action}"
        (log_dir / f"{label}.stdout.log").write_bytes(result.stdout)
        (log_dir / f"{label}.stderr.log").write_bytes(result.stderr)
        commands.append({"command": command, "exit_code": result.returncode,
                         "seconds": round(time.perf_counter() - started, 3)})
        (log_dir / "commands.json").write_text(
            json.dumps(commands, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"Editor {action} failed; see {log_dir / (label + '.stderr.log')}")

    call("setup_project", project_db_file=db, datasets_db_file=datasets,
         project_name=cfg["project_name"], editor_version=runtime["editor_version"], is_lte="n")
    with sqlite3.connect(db) as connection:
        imported = connection.execute("SELECT imported_gis FROM project_config").fetchone()[0]
    if not imported:
        call("import_gis", project_db_file=db, delete_existing="y", is_lte="n")
    with sqlite3.connect(db) as connection:
        editor_counts = {name: row_count(connection, name) for name in
                         ("hru_data_hru", "hru_con", "channel_lte_cha", "ls_unit_ele")}
        if not all(editor_counts.values()):
            raise RuntimeError(f"Editor GIS import is incomplete: {editor_counts}")
    method=weather.get("wgn_method","two_file")
    if method=="database":
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE project_config SET wgn_db=?,wgn_table_name=?",(str(path(weather["wgn_database"])),weather["wgn_table"]))
        call("import_weather",project_db_file=db,delete_existing="y",import_type="wgn",create_stations="n",import_method="database")
    else:
        call("import_weather", project_db_file=db, delete_existing="y", import_type="wgn",
             create_stations="n", import_method="two_file",
             file1=path(weather["wgn_station_csv"]), file2=path(weather["wgn_monthly_csv"]))
    # The official WGN importer can reset weather_data_dir. Set the generated
    # destination afterwards; native files are copied there without editing sources.
    if weather.get("format","2012")=="plus":
        stage_plus_weather(path(weather["source_dir"]),weather_output)
    with sqlite3.connect(db) as connection:
        require_columns(connection, "project_config", ["weather_data_dir", "weather_data_format", "input_files_dir"])
        connection.execute("UPDATE project_config SET weather_data_dir=?,weather_data_format=?,input_files_dir=?",
            (os.path.relpath(weather_output, db.parent), "swatplus", os.path.relpath(output, db.parent)))
    if weather.get("format","2012")=="plus":
        call("import_weather",project_db_file=db,delete_existing="y",import_type="observed",create_stations="y")
    else:
        call("import_weather",project_db_file=db,delete_existing="y",import_type="observed2012",create_stations="y",source_dir=path(weather["source_dir"]))
    with sqlite3.connect(db) as connection:
        require_columns(connection, "time_sim", ["day_start", "yrc_start", "day_end", "yrc_end", "step"])
        require_columns(connection, "print_prt",
                        ["nyskip", "day_start", "yrc_start", "day_end", "yrc_end", "interval", "csvout"])
        require_columns(connection, "print_prt_object", ["name", "daily", "monthly", "yearly", "avann"])
        connection.execute(
            "UPDATE time_sim SET day_start=0,yrc_start=?,day_end=0,yrc_end=?,step=0",
            (sim["start_year"], sim["end_year"]))
        connection.execute(
            "UPDATE print_prt SET nyskip=?,day_start=0,yrc_start=0,day_end=0,yrc_end=0,interval=1,csvout=1",
            (sim["warmup_years"],))
        connection.execute("UPDATE print_prt_object SET daily=0,monthly=0,yearly=0,avann=0")
        for name in sim["print_objects"]:
            count = connection.execute(
                "UPDATE print_prt_object SET daily=1,monthly=1,yearly=1,avann=1 WHERE name=?", (name,)).rowcount
            if count != 1:
                raise ValueError(f"Unknown print object: {name}")
        stations = connection.execute(
            "SELECT name,wgn_id,pcp,tmp,slr,hmd,wnd FROM weather_sta_cli").fetchall()
        if not stations or any(row[1] is None for row in stations):
            raise ValueError(f"天气站未对应有效WGN：{stations}")
        if not weather.get("allow_generated",False) and any(any(x is None or str(x).lower()=="sim" for x in row[2:]) for row in stations):
            raise ValueError(f"缺少气象文件且未允许WGN生成：{stations}")
        if connection.execute("SELECT count(*) FROM hru_con WHERE wst_id IS NULL").fetchone()[0]:
            raise ValueError("Some HRUs lack a weather station")
        weather_counts = {name: row_count(connection, name) for name in
                          ("weather_sta_cli", "weather_wgn_cli", "weather_wgn_cli_mon", "weather_file")}
    call("write_files", project_db_file=db, swat_version=runtime["model_version"])
    for name in ("file.cio", "time.sim", "print.prt", "hru-data.hru", "soils.sol", "channel-lte.cha"):
        if not (output / name).is_file():
            raise FileNotFoundError(output / name)
    summary = {"project_db": str(db), "output_dir": str(output), "editor_api": api,
               "editor_version": runtime["editor_version"], "swat_version": runtime["model_version"],
               "simulation": sim, "gis_version": gis_version, "gis_counts": counts,
               "editor_counts": editor_counts, "weather_counts": weather_counts,
               "weather_stations": stations, "weather_input_check":weather_check,"input_files": len(list(output.iterdir())),
               "log_dir": str(log_dir), "model_run_performed": False}
    (log_dir / "export_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--check-only", action="store_true", help="Read-only input/path checks; no Editor invocation")
    arguments = parser.parse_args()
    build(arguments.config, arguments.check_only)
