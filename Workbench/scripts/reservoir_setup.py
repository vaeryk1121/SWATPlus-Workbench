"""Step-2 reservoir tooling on official SWAT+ project databases.

The workbench reads and writes the same project-database tables the official
Editor uses (reservoir_con/_out, reservoir_res, hydrology_res, initial_res,
chandeg_con_out, d_table_dtl, print_prt_object), and export is performed by
the official Editor API (write_files). Field names and units follow the local
engine source (reservoir_data_module.f90): area ha, volume ha-m, k mm/hr,
evap_co a lake evaporation coefficient, yr_op/mon_op the operational
year/month inside the simulation.

Stages are separate and reported separately:
- inspect / rules: read-only;
- add / set-rule / set-hydrology: write a project COPY (models/examples
  originals are refused);
- export: official Editor API invocation only;
- running and comparison are done by swatplus_trial / reconnect_check.

Teaching parameters must be labeled as such (the default description says
TEACHING); no real storage or release data are fabricated.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

HYDROLOGY_FIELDS = {  # column -> (unit, minimum-exclusive-or-zero)
    "yr_op": ("simulation year", None), "mon_op": ("month 1-12", None),
    "area_ps": ("ha", 0.0), "vol_ps": ("ha-m", 0.0),
    "area_es": ("ha", 0.0), "vol_es": ("ha-m", 0.0),
    "k": ("mm/hr", 0.0), "evap_co": ("coefficient", 0.0),
    "shp_co1": ("coefficient, 0=estimated", 0.0), "shp_co2": ("coefficient, 0=estimated", 0.0),
}
SUPPORT_NOTE = ("支持边界：在官方项目数据库结构内编辑/新增水库对象与属性、选择已有官方放水规则并经官方Editor API导出；"
                "水库空间位置沿用现有河道拓扑（新增对象挂在现有末端河道上），不重新划分流域，不是全自动多水库空间建模；"
                "教学参数须自标识，不代表真实库容或调度。")


def _connect(path, readonly=False):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = path.resolve().as_uri() + ("?mode=ro" if readonly else "")
    return sqlite3.connect(uri, uri=True)


@contextmanager
def _open(path, readonly=False):
    """Connect with explicit close; sqlite's context manager never closes."""
    connection = _connect(path, readonly)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def guard_writable(path):
    """Refuse to modify the retained official example projects."""
    path = Path(path).resolve()
    if (ROOT / "models/examples").resolve() in path.parents:
        raise ValueError(f"拒绝对原始教学案例目录内的项目库写入：{path}；请先复制到独立目录")


def _validated_hydrology(fields):
    """Shared numeric validation for create and edit; raises before any write.

    Finite values only; yr_op/mon_op are whole numbers; mon_op is 1-12;
    physical floors keep official zero defaults legal (volume 0 lets the
    engine estimate) while rejecting negatives.
    """
    validated = {}
    for key, raw in fields.items():
        unit, minimum = HYDROLOGY_FIELDS[key]
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f"{key} 须为有限数值（{unit}）")
        if key in ("yr_op", "mon_op") and not value.is_integer():
            raise ValueError(f"{key} 须为整数（{unit}）")
        if key == "mon_op" and not 1 <= value <= 12:
            raise ValueError(f"mon_op={value} 无效；投运月须为1-12")
        if minimum is not None and value < minimum:
            raise ValueError(f"{key}={value} 低于允许下限（{unit}，>={minimum}）")
        validated[key] = value
    return validated


def resolve_weather_dir(project_db):
    """Resolve the weather directory from the project's own settings.

    Relative weather_data_dir values are resolved against the project
    directory, matching the official Editor convention. Every station file
    the project's weather_sta_cli row names (pcp/tmp/slr/hmd/wnd) must exist
    there, so a project is never silently paired with another basin's
    weather. Missing directory or stations raise with the exact gaps.
    """
    project_db = Path(project_db)
    with _open(project_db, readonly=True) as con:
        configured = con.execute("SELECT weather_data_dir FROM project_config").fetchone()[0]
        stations = con.execute("SELECT name,pcp,tmp,slr,hmd,wnd FROM weather_sta_cli").fetchall()
    if not configured or str(configured).lower() in ("none", "null"):
        raise ValueError(f"项目未配置气象目录（weather_data_dir），请在界面显式选择：{project_db}")
    weather = Path(configured)
    if not weather.is_absolute():
        weather = project_db.parent / weather
    weather = weather.resolve()
    if not weather.is_dir():
        raise ValueError(f"项目气象目录不存在：{weather}；请确认副本包含该目录或显式选择")
    missing = []
    for name, *files in stations:
        for filename in files:
            if filename is None or str(filename).lower() in ("sim", "null"):
                continue
            if not (weather / filename).is_file():
                missing.append(f"{name}:{filename}")
    if missing:
        raise ValueError("气象目录缺少项目站点文件（不得隐式换用其他流域资料）：" + ", ".join(sorted(set(missing))))
    return weather


def current_export_dir(project_db):
    """The directory the project is currently configured to export into."""
    project_db = Path(project_db)
    with _open(project_db, readonly=True) as con:
        configured = con.execute("SELECT input_files_dir FROM project_config").fetchone()[0]
    directory = Path(configured)
    if not directory.is_absolute():
        directory = project_db.parent / directory
    return directory.resolve()


def default_run_settings(project_db):
    """Run period, warmup and a fresh task name from the project itself.

    Years come from time_sim, warmup from print_prt.nyskip, and the task name
    from project_name plus a timestamp, so other basins never inherit Robit's
    constants and repeated clicks never collide with an existing run name.
    """
    with _open(project_db, readonly=True) as con:
        start_year, end_year = con.execute("SELECT yrc_start,yrc_end FROM time_sim").fetchone()
        warmup = con.execute("SELECT nyskip FROM print_prt").fetchone()
        warmup = warmup[0] if warmup else 0
        project_name = con.execute("SELECT project_name FROM project_config").fetchone()[0]
    if not start_year or not end_year or int(end_year) < int(start_year):
        raise ValueError(f"项目时段无效（yrc_start={start_year}, yrc_end={end_year}），无法生成运行命令")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return {"start_year": int(start_year), "end_year": int(end_year),
            "warmup_years": int(warmup or 0),
            "task_name": f"{re.sub(r'[^A-Za-z0-9_]', '_', str(project_name or 'project'))}_res_compare_{stamp}"}


def read_reservoirs(project_db):
    """Read-only reservoir inventory with links, properties and rule names."""
    with _open(project_db, readonly=True) as con:
        con.row_factory = sqlite3.Row
        reservoirs = []
        rows = con.execute(
            "SELECT c.*, r.init_id, r.hyd_id, r.rel_id, r.sed_id, r.nut_id, r.description AS res_description "
            "FROM reservoir_con c LEFT JOIN reservoir_res r ON r.id = c.res_id ORDER BY c.id").fetchall()
        hyd = {row["id"]: row for row in con.execute("SELECT * FROM hydrology_res")}
        ini = {row["id"]: row for row in con.execute("SELECT * FROM initial_res")}
        rules = {row["id"]: row["name"] for row in con.execute("SELECT id,name FROM d_table_dtl")}
        channels = {row["id"]: row["name"] for row in con.execute("SELECT id,name FROM chandeg_con")}
        for row in rows:
            hydrology = hyd.get(row["hyd_id"])
            reservoirs.append({
                "id": row["id"], "name": row["name"], "gis_id": row["gis_id"],
                "area_ha": row["area"], "lat": row["lat"], "lon": row["lon"],
                "wst_id": row["wst_id"],
                "properties": {
                    "init": (ini[row["init_id"]]["name"] if row["init_id"] in ini else None) if row["init_id"] else None,
                    "hydrology": hydrology["name"] if hydrology else None,
                    "release_rule": rules.get(row["rel_id"]) if row["rel_id"] else None,
                    "release_rule_id": row["rel_id"],
                    "sediment": row["sed_id"], "nutrients": row["nut_id"],
                    "description": row["res_description"],
                },
                "hydrology": {key: hydrology[key] for key in HYDROLOGY_FIELDS} | {
                    "name": hydrology["name"], "units": {k: v[0] for k, v in HYDROLOGY_FIELDS.items()}
                } if hydrology else None,
                "upstream_channels": [
                    {"channel_id": up["chandeg_con_id"], "channel": channels.get(up["chandeg_con_id"]),
                     "hyd_typ": up["hyd_typ"], "frac": up["frac"]}
                    for up in con.execute(
                        "SELECT * FROM chandeg_con_out WHERE obj_typ='res' AND obj_id=?", (row["id"],))],
                "downstream": [dict(out) for out in con.execute(
                    "SELECT `order`,obj_typ,obj_id,hyd_typ,frac FROM reservoir_con_out WHERE reservoir_con_id=?",
                    (row["id"],))],
            })
        return {"project_db": str(Path(project_db).resolve()), "reservoirs": reservoirs,
                "release_rules": [{"id": key, "name": value} for key, value in sorted(rules.items())],
                "support": SUPPORT_NOTE}


def available_rules(project_db):
    with _open(project_db, readonly=True) as con:
        rules = []
        for row in con.execute(
                "SELECT id,name,description FROM d_table_dtl WHERE file_name='res_rel.dtl' ORDER BY id"):
            conds = con.execute("SELECT count(*) FROM d_table_dtl_cond WHERE d_table_id=?", (row[0],)).fetchone()[0]
            acts = con.execute("SELECT count(*) FROM d_table_dtl_act WHERE d_table_id=?", (row[0],)).fetchone()[0]
            rules.append({"id": row[0], "name": row[1], "description": row[2],
                          "conditions": conds, "actions": acts, "file": "res_rel.dtl"})
        if not rules:
            raise ValueError("项目库中没有 res_rel.dtl 放水规则决策表；请用官方数据集初始化项目")
        return rules


def _terminal_channel(con):
    """The unique channel without out links; reservoirs attach to it."""
    rows = con.execute(
        "SELECT id,name FROM chandeg_con WHERE id NOT IN (SELECT chandeg_con_id FROM chandeg_con_out)").fetchall()
    wired = {row[0] for row in con.execute("SELECT obj_id FROM chandeg_con_out WHERE obj_typ='res'")}
    free = [row for row in rows if row[0] not in wired]
    if len(free) != 1:
        raise ValueError(f"需要唯一未被占用的末端河道来挂接水库，找到 {len(free)} 条：{free}；"
                         "多出口模型请先在空间阶段确定水库位置")
    return free[0][0], free[0][1]


def add_teaching_reservoir(source_project, target_dir, name, rule_id, hydrology=None,
                           channel_id=None, description=None, weather_dir=None):
    """Create a project COPY under target_dir and add one labeled reservoir.

    The copy receives the same rows the official Editor maintains, the unique
    terminal channel is re-wired into the reservoir, and (optionally) the
    weather directory is copied so the project is self-contained for export.
    Nothing in the source project is modified.
    """
    source_project = Path(source_project)
    target_dir = Path(target_dir)
    guard_writable(target_dir / "project.sqlite")
    if target_dir.exists() and any(target_dir.iterdir()):
        raise FileExistsError(f"目标目录已非空：{target_dir}")
    hydrology = _validated_hydrology({
        "yr_op": 1990, "mon_op": 1, "area_ps": 1.0, "vol_ps": 10.0,
        "area_es": 1.5, "vol_es": 12.0, "k": 0.01, "evap_co": 0.7,
        "shp_co1": 0.0, "shp_co2": 0.0, **(hydrology or {})})
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,20}", name):
        raise ValueError("水库对象名须为字母开头的1-21位英文/数字/下划线")
    target_dir.mkdir(parents=True)
    db = target_dir / f"{name.lower()}.sqlite"
    shutil.copy2(source_project, db)
    datasets = source_project.parent / "swatplus_datasets.sqlite"
    if datasets.is_file():
        shutil.copy2(datasets, target_dir / datasets.name)
    if weather_dir:
        shutil.copytree(weather_dir, target_dir / "weather")
    with _open(db) as con:
        rules = {rule["id"] for rule in available_rules(db)}
        if rule_id not in rules:
            raise ValueError(f"未知放水规则 id={rule_id}；可用：{sorted(rules)}")
        if con.execute("SELECT count(*) FROM reservoir_con").fetchone()[0]:
            raise ValueError("该副本已有水库对象；新增多水库请先完成最终空间结构")
        if channel_id is None:
            channel_id, channel_name = _terminal_channel(con)
        else:
            row = con.execute("SELECT name FROM chandeg_con WHERE id=?", (channel_id,)).fetchone()
            if not row:
                raise ValueError(f"未知河道 id={channel_id}")
            channel_name = row[0]
            if con.execute("SELECT count(*) FROM chandeg_con_out WHERE chandeg_con_id=?",
                           (channel_id,)).fetchone()[0]:
                raise ValueError(f"河道 {channel_name} 已有下游连接；水库只能挂在现有末端河道上")
        lat, lon = con.execute("SELECT lat,lon FROM chandeg_con WHERE id=?", (channel_id,)).fetchone()
        wst = con.execute("SELECT wst_id FROM chandeg_con WHERE id=?", (channel_id,)).fetchone()[0]
        om = con.execute("SELECT id,name FROM om_water_ini ORDER BY id LIMIT 1").fetchone()
        if not om:
            raise ValueError("项目库缺少 om_water_ini 初始水质记录")
        con.execute("INSERT INTO initial_res (id,name,org_min_id,description) VALUES (1,?,?,?)",
                    (f"{name.lower()}_init", om[0],
                     description or f"TEACHING reservoir {name}: synthetic parameters, not a real survey"))
        con.execute("INSERT INTO hydrology_res (id,name,yr_op,mon_op,area_ps,vol_ps,area_es,vol_es,k,evap_co,shp_co1,shp_co2)"
                    " VALUES (1,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{name.lower()}_hyd", *(hydrology[key] for key in HYDROLOGY_FIELDS)))
        con.execute("INSERT INTO reservoir_res (id,name,init_id,hyd_id,rel_id,description) VALUES (1,?,1,1,?,?)",
                    (name, rule_id,
                     description or f"TEACHING reservoir {name}: synthetic parameters, not a real survey"))
        con.execute("INSERT INTO reservoir_con (id,name,gis_id,area,lat,lon,elev,wst_id,cst_id,ovfl,rule,res_id)"
                    " VALUES (1,?,1,?,?,?,NULL,?,NULL,0,0,1)",
                    (name, float(hydrology["area_es"]), lat, lon, wst))
        out_id = (con.execute("SELECT max(id) FROM chandeg_con_out").fetchone()[0] or 0) + 1
        con.execute("INSERT INTO chandeg_con_out (id,`order`,obj_typ,obj_id,hyd_typ,frac,chandeg_con_id)"
                    " VALUES (?,1,'res',1,'tot',1.0,?)", (out_id, channel_id))
        con.execute("UPDATE project_config SET project_name=?, input_files_dir='Scenarios/Default/TxtInOut'"
                    + (", weather_data_dir='weather'" if weather_dir else ""),
                    (name.lower(),))
        report = {"stage": "configure (project copy)", "source_project": str(source_project.resolve()),
                  "project_db": str(db), "reservoir": {"id": 1, "name": name, "rule_id": rule_id},
                  "wired_channel": {"id": channel_id, "name": channel_name},
                  "hydrology": {key: hydrology[key] for key in HYDROLOGY_FIELDS},
                  "hydrology_units": {key: unit for key, (unit, _) in HYDROLOGY_FIELDS.items()},
                  "teaching_parameters": True, "support": SUPPORT_NOTE,
                  "database_written": True, "official_export_performed": False, "engine_run_performed": False}
    (target_dir / "reservoir_setup.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _clone_row(con, table, row_id, name_column, name_suffix):
    """Copy one row under a fresh id, renaming its name column; return the new id."""
    cursor = con.execute(f'SELECT * FROM "{table}" WHERE id=? LIMIT 1', (row_id,))
    columns = [description[0] for description in cursor.description]
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"{table} 中不存在 id={row_id}")
    new_id = (con.execute(f'SELECT max(id) FROM "{table}"').fetchone()[0] or 0) + 1
    values = list(row)
    if name_column in columns:
        values[columns.index(name_column)] = f"{row[columns.index(name_column)]}{name_suffix}"
    values[columns.index("id")] = new_id
    con.execute(f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ({",".join("?" * len(columns))})', values)
    return new_id


def _exclusive_reservoir_res(con, reservoir_id):
    """Give the selected reservoir its own reservoir_res row before editing.

    Official projects may point several reservoir_con rows at one shared
    reservoir_res record; editing through it would silently change every
    reservoir that references it. Shared rows are cloned (fresh id, suffixed
    name) and only the selected reservoir is re-linked. Nothing else moves.
    """
    row = con.execute("SELECT res_id FROM reservoir_con WHERE id=?", (reservoir_id,)).fetchone()
    if not row:
        raise ValueError(f"未知水库对象 id={reservoir_id}")
    res_id = row[0]
    shared = con.execute("SELECT count(*) FROM reservoir_con WHERE res_id=?", (res_id,)).fetchone()[0] > 1
    separated = None
    if shared:
        new_id = _clone_row(con, "reservoir_res", res_id, "name", f"_r{reservoir_id}")
        con.execute("UPDATE reservoir_con SET res_id=? WHERE id=?", (new_id, reservoir_id))
        separated = f"reservoir_res {res_id} 共享，已克隆为 {new_id} 并仅关联水库 {reservoir_id}"
        res_id = new_id
    return res_id, separated


def _exclusive_hydrology_res(con, res_id):
    """Same separation for hydrology_res rows shared across reservoir_res."""
    hyd_id = con.execute("SELECT hyd_id FROM reservoir_res WHERE id=?", (res_id,)).fetchone()[0]
    shared = con.execute("SELECT count(*) FROM reservoir_res WHERE hyd_id=?", (hyd_id,)).fetchone()[0] > 1
    separated = None
    if shared:
        new_id = _clone_row(con, "hydrology_res", hyd_id, "name", f"_r{res_id}")
        con.execute("UPDATE reservoir_res SET hyd_id=? WHERE id=?", (new_id, res_id))
        separated = f"hydrology_res {hyd_id} 共享，已克隆为 {new_id} 并仅关联 reservoir_res {res_id}"
        hyd_id = new_id
    return hyd_id, separated


def set_release_rule(project_db, reservoir_id, rule_id):
    guard_writable(project_db)
    with _open(project_db) as con:
        rules = {rule["id"]: rule["name"] for rule in available_rules(project_db)}
        if rule_id not in rules:
            raise ValueError(f"未知放水规则 id={rule_id}；可用：{rules}")
        res_id, separated = _exclusive_reservoir_res(con, reservoir_id)
        con.execute("UPDATE reservoir_res SET rel_id=? WHERE id=?", (rule_id, res_id))
        return {"stage": "configure (project copy)", "reservoir_id": reservoir_id,
                "release_rule": rules[rule_id], "separated_shared_records": separated,
                "database_written": True,
                "official_export_performed": False, "engine_run_performed": False}


def set_hydrology(project_db, reservoir_id, fields):
    guard_writable(project_db)
    unknown = set(fields) - set(HYDROLOGY_FIELDS)
    if unknown:
        raise ValueError(f"未知水力字段：{sorted(unknown)}；可用：{sorted(HYDROLOGY_FIELDS)}")
    # Validate every field before touching the database: a rejected edit
    # leaves no partial update behind.
    validated = _validated_hydrology(fields)
    with _open(project_db) as con:
        res_id, separated_res = _exclusive_reservoir_res(con, reservoir_id)
        hyd_id, separated_hyd = _exclusive_hydrology_res(con, res_id)
        for key, value in validated.items():
            con.execute(f'UPDATE hydrology_res SET "{key}"=? WHERE id=?', (value, hyd_id))
        separations = [note for note in (separated_res, separated_hyd) if note]
        return {"stage": "configure (project copy)", "reservoir_id": reservoir_id,
                "hydrology_id": hyd_id, "updated": validated,
                "units": {key: HYDROLOGY_FIELDS[key][0] for key in validated},
                "separated_shared_records": separations or None,
                "database_written": True, "official_export_performed": False, "engine_run_performed": False}


def export_project(project_db, output_dir=None, runtime_config=None, print_objects=(), timeout=600,
                   require_reservoir=None):
    """Run the official Editor API write_files; verify exported files.

    require_reservoir=None auto-detects from the project (reservoir_con rows);
    True enforces the reservoir file/wiring checks, False runs the generic
    verification (core files) for projects without reservoirs.
    """
    project_db = Path(project_db).resolve()
    guard_writable(project_db)
    runtime = json.loads((ROOT / (runtime_config or "config/qgis344_runtime.json")).read_text(encoding="utf-8"))
    api = Path(runtime["editor_api"])
    if not api.is_file():
        raise FileNotFoundError(api)
    datasets = project_db.parent / "swatplus_datasets.sqlite"
    if not datasets.is_file():
        raise FileNotFoundError(datasets)
    if project_db.drive.lower() != datasets.drive.lower():
        raise ValueError("Editor 项目库与数据集库须在同一驱动器")
    with _open(project_db) as con:
        if print_objects:
            for name in print_objects:
                count = con.execute("UPDATE print_prt_object SET daily=1,monthly=1,yearly=1,avann=1 "
                                    "WHERE name=?", (name,)).rowcount
                if count != 1:
                    raise ValueError(f"未知打印对象：{name}")
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        reservoir_rows = (con.execute("SELECT count(*) FROM reservoir_con").fetchone()[0]
                          if "reservoir_con" in tables else 0)
        schedule_rows = (con.execute("SELECT count(*) FROM management_sch").fetchone()[0]
                         if "management_sch" in tables else 0)
        if require_reservoir is None:
            require_reservoir = reservoir_rows > 0
        input_dir = con.execute("SELECT input_files_dir FROM project_config").fetchone()[0]
    default_output = (project_db.parent / input_dir).resolve()
    output = Path(output_dir).resolve() if output_dir else default_output
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"导出目录已非空：{output}；既有导出保留，请指定新的输出目录"
                              "（界面可自动生成新版本目录）后另存导出")
    # The official API exports into project_config.input_files_dir, so an
    # explicitly requested directory must be configured there first; the
    # previous setting (and any export already written to it) stays untouched.
    configured_relative = None
    if output != default_output:
        configured_relative = os.path.relpath(output, project_db.parent)
        with _open(project_db) as con:
            con.execute("UPDATE project_config SET input_files_dir=?", (configured_relative,))
    log_dir = project_db.parent / "export_logs" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir.mkdir(parents=True, exist_ok=True)
    command = [str(api), "write_files", f"--project_db_file={project_db}",
               f"--swat_version={runtime['model_version']}"]
    started = dt.datetime.now()
    result = subprocess.run(command, cwd=project_db.parent, capture_output=True, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (log_dir / "write_files.stdout.log").write_bytes(result.stdout)
    (log_dir / "write_files.stderr.log").write_bytes(result.stderr)
    (log_dir / "command.json").write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"官方Editor导出失败（exit {result.returncode}）；日志：{log_dir}")
    if require_reservoir:
        required = ["file.cio", "chandeg.con", "reservoir.con", "reservoir.res", "hydrology.res", "initial.res"]
    else:
        required = ["file.cio", "landuse.lum"] + (["management.sch"] if schedule_rows else [])
    missing = [name for name in required if not (output / name).is_file()]
    if missing:
        raise RuntimeError(f"官方导出缺少{('水库相关' if require_reservoir else '核心')}文件：{missing}；日志：{log_dir}")
    if require_reservoir:
        wiring = (output / "chandeg.con").read_text(encoding="utf-8-sig")
        if not re.search(r"\bres\s+\d+\s+tot\s+", wiring):
            raise RuntimeError("导出的 chandeg.con 中没有河道→水库连接行；请检查项目库 chandeg_con_out")
    report = {"stage": "export (official Editor API)", "project_db": str(project_db),
              "editor_api": str(api), "editor_version": runtime["editor_version"],
              "swat_version": runtime["model_version"], "output_dir": str(output),
              "project_input_files_dir": configured_relative or input_dir,
              "print_objects_enabled": sorted(print_objects),
              "reservoir_files_checked": bool(require_reservoir),
              "reservoir_files": required[2:] if require_reservoir else [],
              "files_exported": len(list(output.iterdir())),
              "exit_code": result.returncode, "log_dir": str(log_dir),
              "started_at": started.isoformat(timespec="seconds"),
              "database_written": False, "official_export_performed": True, "engine_run_performed": False}
    (log_dir / "export_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("inspect", help="只读列出水库对象、连接、属性与规则")
    p.add_argument("--project-db", type=Path, required=True)
    p = sub.add_parser("rules", help="列出可用官方放水规则（res_rel.dtl）")
    p.add_argument("--project-db", type=Path, required=True)
    p = sub.add_parser("add", help="在项目副本上新增一个明确标识的教学水库")
    p.add_argument("--source-project", type=Path, required=True)
    p.add_argument("--target-dir", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--rule-id", type=int, required=True)
    p.add_argument("--channel-id", type=int, default=None)
    p.add_argument("--weather-dir", type=Path, default=None)
    p.add_argument("--description", default=None)
    for key in HYDROLOGY_FIELDS:
        p.add_argument(f"--{key.replace('_', '-')}", type=float, default=None)
    p = sub.add_parser("set-rule", help="修改已有水库的放水规则（项目副本）")
    p.add_argument("--project-db", type=Path, required=True)
    p.add_argument("--reservoir-id", type=int, required=True)
    p.add_argument("--rule-id", type=int, required=True)
    p = sub.add_parser("set-hydrology", help="修改已有水库水力参数（项目副本）")
    p.add_argument("--project-db", type=Path, required=True)
    p.add_argument("--reservoir-id", type=int, required=True)
    p.add_argument("fields", nargs="+", help="key=value，见 HYDROLOGY_FIELDS")
    p = sub.add_parser("export", help="官方Editor API导出含水库TxtInOut")
    p.add_argument("--project-db", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--print-object", action="append", default=[])
    args = parser.parse_args()
    if args.action == "inspect":
        print(json.dumps(read_reservoirs(args.project_db), ensure_ascii=False, indent=2))
    elif args.action == "rules":
        print(json.dumps(available_rules(args.project_db), ensure_ascii=False, indent=2))
    elif args.action == "add":
        hydrology = {key: getattr(args, key.replace("-", "_")) for key in HYDROLOGY_FIELDS
                     if getattr(args, key.replace("-", "_")) is not None}
        # Weather comes from the selected project's own settings unless the
        # user explicitly provides another directory.
        weather = args.weather_dir if args.weather_dir is not None else resolve_weather_dir(args.source_project)
        print(json.dumps(add_teaching_reservoir(
            args.source_project, args.target_dir, args.name, args.rule_id,
            hydrology, args.channel_id, args.description, weather),
            ensure_ascii=False, indent=2))
    elif args.action == "set-rule":
        print(json.dumps(set_release_rule(args.project_db, args.reservoir_id, args.rule_id),
                         ensure_ascii=False, indent=2))
    elif args.action == "set-hydrology":
        fields = {}
        for item in args.fields:
            key, _, value = item.partition("=")
            fields[key] = value
        print(json.dumps(set_hydrology(args.project_db, args.reservoir_id, fields),
                         ensure_ascii=False, indent=2))
    elif args.action == "export":
        print(json.dumps(export_project(args.project_db, args.output_dir,
                                        print_objects=args.print_object),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
