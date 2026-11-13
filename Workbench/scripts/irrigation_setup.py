"""Step-3 irrigation tooling on official SWAT+ project databases.

The constrained-irrigation route verified against the local 62.0.1 source is
the SCHEDULE AUTO-OPERATIONS path: the irrigation decision-table rule
(condition w_stress < threshold; action "irrigate" with source type/object,
amount mm, max applications/year, application type in irr.ops) is attached to
a crop management schedule as an auto operation (management_sch_auto ->
management.sch). The engine evaluates it daily per HRU (hru_control.f90, local
auto indices), and the action itself deducts demand from the source storage
and applies NOTHING when the source holds less than the demand (actions.f90) -
a hard supply constraint. Everything in this route exports through the
official Editor API; no post-export file supplementation is needed.

The water-rights route (water_hru_irr.wal + place_of_use.wro) is NOT runnable
on this engine build: wallo_start_day.f90 calls actions() with the GLOBAL
decision-table number, while actions.f90 indexes pcom(j)%dtbl(idtbl) allocated
per-HRU by schedule autos - access violation at actions.f90:246 (verified by a
real run; the upstream init linkage is commented out in
hru_dtbl_actions_init.f90). supplement_engine_inputs() is retained only to
write those engine-format files for reference and reports the limitation.

Stages, reported separately and never conflated:
- inspect: read-only (landuse/HRU distribution, schedules with actual
  operations AND auto rules, plant communities vs plant growth parameters,
  irr.ops records, existing irrigation rules, available source objects);
- configure: writes the rule + schedule auto link through official project
  database tables on a project COPY; HRU scope follows the landuse whose
  schedule is linked, unselected landuses untouched;
- export: official Editor API (reused from reservoir_setup.export_project).

Dated manual irrigation ops (management.sch "irrm", no source deduction) and
plant growth parameter editing are NOT provided; teaching parameters must be
labeled TEACHING.
"""
import argparse
import json
import math
from pathlib import Path
import re
import sqlite3

from reservoir_setup import ROOT, SUPPORT_NOTE, _open, guard_writable, export_project

SOURCE_TYPES = {"res": "reservoir_con", "cha": "chandeg_con", "aqu": "aquifer_con"}
IRR_SUPPORT_NOTE = (SUPPORT_NOTE + " 灌溉边界：水源受限灌溉走官方日程auto操作+决策表机制（官方导出完整支持；"
                    "actions.f90从res/cha/aqu实际扣水，水源不足时不供水）；按日期定额的手工灌溉操作(irrm，不扣水源)与"
                    "植物生长参数编辑未接入；水权文件路线(water_hru_irr.wal)在本机62.0.1引擎会崩溃（actions.f90:246，"
                    "上游初始化联动被注释），仅保留格式写档函数并明确不可运行。")


def _tables(connection, table):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def read_management(project_db):
    """Read-only crop/management inventory with the plant/operation split."""
    with _open(project_db, readonly=True) as con:
        landuse = [{"id": row[0], "name": row[1], "schedule": row[2], "plant_community": row[3],
                    "hrus": row[4], "area_ha": round(row[5], 3)}
                   for row in con.execute(
                       "SELECT l.id,l.name,s.name,p.name,count(c.id),coalesce(sum(c.area),0.) "
                       "FROM landuse_lum l "
                       "LEFT JOIN management_sch s ON l.mgt_id=s.id "
                       "LEFT JOIN plant_ini p ON l.plnt_com_id=p.id "
                       "LEFT JOIN hru_data_hru h ON h.lu_mgt_id=l.id "
                       "LEFT JOIN hru_con c ON c.hru_id=h.id "
                       "GROUP BY l.id ORDER BY l.id")]
        schedules = [{"id": row[0], "name": row[1], "operations": row[2],
                      "operation_types": [r[0] for r in con.execute(
                          "SELECT DISTINCT op_typ FROM management_sch_op WHERE management_sch_id=? ORDER BY op_typ", (row[0],))],
                      "auto_rules": [{"id": r[0], "name": r[1], "crop": r[2]} for r in con.execute(
                          "SELECT a.d_table_id,d.name,a.plant1 FROM management_sch_auto a "
                          "LEFT JOIN d_table_dtl d ON a.d_table_id=d.id WHERE a.management_sch_id=? ORDER BY a.id", (row[0],))]}
                     for row in con.execute(
                         "SELECT s.id,s.name,count(o.id) FROM management_sch s "
                         "LEFT JOIN management_sch_op o ON o.management_sch_id=s.id GROUP BY s.id ORDER BY s.id")]
        irr_ops = [{"id": row[0], "name": row[1], "amt_mm": row[2], "eff": row[3],
                    "surq_frac": row[4], "sub_irr_depth_mm": row[5]}
                   for row in con.execute("SELECT id,name,amt_mm,eff_frac,sumq_frac,dep_sub FROM irr_ops ORDER BY id")]
        rules = []
        for row in con.execute(
                "SELECT DISTINCT t.id,t.name,t.description FROM d_table_dtl t "
                "JOIN d_table_dtl_act a ON a.d_table_id=t.id WHERE a.act_typ IN ('irrigate','irr_demand') ORDER BY t.id"):
            action = con.execute("SELECT act_typ,obj,obj_num,name,option,const,const2 FROM d_table_dtl_act "
                                 "WHERE d_table_id=? LIMIT 1", (row[0],)).fetchone()
            condition = con.execute("SELECT var,lim_op,lim_const FROM d_table_dtl_cond "
                                    "WHERE d_table_id=? LIMIT 1", (row[0],)).fetchone()
            rules.append({"id": row[0], "name": row[1], "description": row[2],
                          "action": {"type": action[0], "source": action[1], "source_obj": action[2],
                                     "application": action[4], "amount_mm": action[5], "max_per_year": action[6]},
                          "condition": {"variable": condition[0], "operator": condition[1],
                                        "threshold": condition[2]} if condition else None})
        sources = {}
        existing = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for kind, table in SOURCE_TYPES.items():
            sources[kind] = ([row[0] for row in con.execute(f"SELECT id FROM {table} ORDER BY id")]
                             if table in existing else [])
        return {"project_db": str(Path(project_db).resolve()),
                "landuse": landuse, "management_schedules": schedules,
                "irrigation_application_types": irr_ops, "irrigation_rules": rules,
                "available_sources": sources,
                "stage": "inspect (read-only)", "support": IRR_SUPPORT_NOTE,
                "database_written": False}


def expand_scope(project_db, hru_ids=None, landuse_name=None):
    """Resolve the explicit HRU scope in CONNECTION-object numbering.

    External HRU numbers are hru_con ids (the output unit numbers); they are
    resolved to attribute records through the official hru_con.hru_id
    reference, never by assuming the two id sequences match.
    """
    if bool(hru_ids) == bool(landuse_name):
        raise ValueError("范围须且只须指定一种：显式HRU编号列表或地类名称")
    with _open(project_db, readonly=True) as con:
        if landuse_name:
            rows = con.execute(
                "SELECT DISTINCT c.id FROM hru_con c "
                "JOIN hru_data_hru h ON c.hru_id=h.id "
                "JOIN landuse_lum l ON h.lu_mgt_id=l.id WHERE l.name=? ORDER BY c.id",
                (landuse_name,)).fetchall()
            if not rows:
                raise ValueError(f"未知地类：{landuse_name}；可用："
                                 f"{[r[0] for r in con.execute('SELECT name FROM landuse_lum ORDER BY id')]}")
            return [row[0] for row in rows]
        known = {row[0] for row in con.execute("SELECT id FROM hru_con")}
        unknown = sorted(set(hru_ids) - known)
        if unknown:
            raise ValueError(f"未知HRU连接编号：{unknown}；有效连接 1..{max(known)}")
        if not hru_ids:
            raise ValueError("HRU范围不能为空")
        return list(hru_ids)


def _landuse_hrus(con, landuse_id):
    """CONNECTION ids resolving to a landuse through official references."""
    return [row[0] for row in con.execute(
        "SELECT DISTINCT c.id FROM hru_con c "
        "JOIN hru_data_hru h ON c.hru_id=h.id "
        "WHERE h.lu_mgt_id=? ORDER BY c.id", (landuse_id,))]


def _connection_attributes(con, connection_ids):
    """Map connection id -> current attribute record id (hru_con.hru_id)."""
    marks = ",".join("?" * len(connection_ids))
    return {row[0]: row[1] for row in con.execute(
        f"SELECT id,hru_id FROM hru_con WHERE id IN ({marks})", connection_ids)}


def _separate_shared_attributes(con, selected_connection_ids):
    """Clone attribute records shared with unselected connections.

    If one hru_data_hru record is referenced by both selected and unselected
    hru_con rows, the selected connections get their own clone (fresh id,
    name suffix, every official column kept) and only they are re-pointed;
    unselected connections keep the original record and its chain untouched.
    """
    mapping = _connection_attributes(con, selected_connection_ids)
    all_cons = [row[0] for row in con.execute("SELECT id FROM hru_con")]
    unselected = [c for c in all_cons if c not in set(selected_connection_ids)]
    shared_attrs = set()
    if unselected:
        marks = ",".join("?" * len(unselected))
        shared_attrs = {row[0] for row in con.execute(
            f"SELECT DISTINCT hru_id FROM hru_con WHERE id IN ({marks})", unselected)}
    clones = {}
    for connection_id in selected_connection_ids:
        attribute = mapping[connection_id]
        if attribute in shared_attrs or attribute in clones:
            if attribute not in clones:
                cursor = con.execute("SELECT * FROM hru_data_hru WHERE id=? LIMIT 1", (attribute,))
                columns = [d[0] for d in cursor.description]
                row = list(cursor.fetchone())
                new_id = (con.execute("SELECT max(id) FROM hru_data_hru").fetchone()[0] or 0) + 1
                row[columns.index("id")] = new_id
                row[columns.index("name")] = f"{row[columns.index('name')]}_c{new_id}"
                quoted = ",".join('"' + column + '"' for column in columns)
                con.execute(f'INSERT INTO hru_data_hru ({quoted}) VALUES ({",".join("?" * len(columns))})', row)
                clones[attribute] = new_id
            con.execute("UPDATE hru_con SET hru_id=? WHERE id=?", (clones[attribute], connection_id))
    return clones


def _schedule_sharers(con, schedule_id):
    """Landuse ids whose management chain points at this schedule."""
    return [row[0] for row in con.execute(
        "SELECT id FROM landuse_lum WHERE mgt_id=?", (schedule_id,))]


def _clone_schedule(con, schedule_id):
    """Clone one management schedule with ALL operations and auto rules."""
    row = con.execute("SELECT name FROM management_sch WHERE id=?", (schedule_id,)).fetchone()
    new_id = (con.execute("SELECT max(id) FROM management_sch").fetchone()[0] or 0) + 1
    variant = 1
    while con.execute("SELECT count(*) FROM management_sch WHERE name=?",
                      (f"{row[0]}_v{variant}",)).fetchone()[0]:
        variant += 1
    con.execute("INSERT INTO management_sch (id,name) VALUES (?,?)", (new_id, f"{row[0]}_v{variant}"))
    for op in con.execute("SELECT * FROM management_sch_op WHERE management_sch_id=? ORDER BY `order`,id",
                          (schedule_id,)).fetchall():
        columns = [d[0] for d in con.execute("SELECT * FROM management_sch_op LIMIT 1").description]
        values = list(op)
        values[columns.index("id")] = (con.execute("SELECT max(id) FROM management_sch_op").fetchone()[0] or 0) + 1
        values[columns.index("management_sch_id")] = new_id
        quoted = ','.join('"' + column + '"' for column in columns)
        con.execute(f'INSERT INTO management_sch_op ({quoted}) VALUES ({",".join("?"*len(columns))})', values)
    for auto in con.execute("SELECT * FROM management_sch_auto WHERE management_sch_id=? ORDER BY id",
                            (schedule_id,)).fetchall():
        columns = [d[0] for d in con.execute("SELECT * FROM management_sch_auto LIMIT 1").description]
        values = list(auto)
        values[columns.index("id")] = (con.execute("SELECT max(id) FROM management_sch_auto").fetchone()[0] or 0) + 1
        values[columns.index("management_sch_id")] = new_id
        quoted = ','.join('"' + column + '"' for column in columns)
        con.execute(f'INSERT INTO management_sch_auto ({quoted}) VALUES ({",".join("?"*len(columns))})', values)
    return new_id, f"{row[0]}_v{variant}"


def _clone_landuse(con, landuse_id, schedule_id=None):
    """Clone one landuse row (keeping every official reference) for a subset."""
    cursor = con.execute("SELECT * FROM landuse_lum WHERE id=? LIMIT 1", (landuse_id,))
    columns = [d[0] for d in cursor.description]
    row = list(cursor.fetchone())
    new_id = (con.execute("SELECT max(id) FROM landuse_lum").fetchone()[0] or 0) + 1
    variant = 1
    while con.execute("SELECT count(*) FROM landuse_lum WHERE name=?",
                      (f"{row[columns.index('name')]}_v{variant}",)).fetchone()[0]:
        variant += 1
    row[columns.index("id")] = new_id
    row[columns.index("name")] = f"{row[columns.index('name')]}_v{variant}"
    if schedule_id is not None:
        row[columns.index("mgt_id")] = schedule_id
    quoted = ','.join('"' + column + '"' for column in columns)
    con.execute(f'INSERT INTO landuse_lum ({quoted}) VALUES ({",".join("?"*len(columns))})', row)
    return new_id, row[columns.index("name")]


def _separate_schedule_for_scope(con, landuse_id, selected_connection_ids):
    """Give the selected scope its own schedule chain, preserving everything.

    In-place attachment is only safe when the scope covers the WHOLE landuse
    and no other landuse shares its schedule; otherwise clone the schedule
    (all operations + auto rules) and the landuse row, and re-point only the
    selected HRUs. Unselected HRUs and landuses keep their original chain and
    cannot receive the new rule. selected_connection_ids are hru_con ids.
    """
    landuse_cons = _landuse_hrus(con, landuse_id)
    schedule_id = con.execute("SELECT mgt_id FROM landuse_lum WHERE id=?", (landuse_id,)).fetchone()[0]
    sharers = _schedule_sharers(con, schedule_id) if schedule_id else []
    whole_landuse = set(selected_connection_ids) == set(landuse_cons)
    if schedule_id and whole_landuse and sharers == [landuse_id]:
        return schedule_id, None, landuse_id, None
    new_schedule, schedule_name = _clone_schedule(con, schedule_id)
    new_landuse, landuse_name = _clone_landuse(con, landuse_id, schedule_id=new_schedule)
    # re-point the attribute records that now belong exclusively to the
    # selected connections (shared attributes were cloned beforehand)
    attributes = sorted(set(_connection_attributes(con, selected_connection_ids).values()))
    marks = ",".join("?" * len(attributes))
    con.execute(f"UPDATE hru_data_hru SET lu_mgt_id=? WHERE id IN ({marks})",
                [new_landuse, *attributes])
    return new_schedule, schedule_name, new_landuse, landuse_name


def _enabled_rules(con, schedule_id):
    return [{"id": row[0], "name": row[1]} for row in con.execute(
        "SELECT d.id,d.name FROM management_sch_auto a JOIN d_table_dtl d ON a.d_table_id=d.id "
        "WHERE a.management_sch_id=? ORDER BY a.id", (schedule_id,))]


def assign_management(project_db, landuse_name=None, hru_ids=None,
                      schedule_name=None, plant_community_name=None):
    """Link an EXISTING schedule and/or plant community to a landuse/HRU scope.

    Whole-landuse scope updates that landuse row directly (other landuses keep
    their references); an explicit HRU subset is separated first through a
    cloned landuse variant so unselected HRUs keep the original references.
    Only references stored in official columns are touched; plant growth
    parameters themselves are not edited.
    """
    guard_writable(project_db)
    if not schedule_name and not plant_community_name:
        raise ValueError("须至少指定管理日程或植物群落之一")
    if bool(landuse_name) == bool(hru_ids):
        raise ValueError("范围须且只须指定一种：地类名称或显式HRU编号列表")
    with _open(project_db) as con:
        if hru_ids:
            scope = expand_scope(project_db, hru_ids=hru_ids)  # connection ids
            rows = con.execute(
                "SELECT DISTINCT l.id FROM hru_con c "
                "JOIN hru_data_hru h ON c.hru_id=h.id "
                "JOIN landuse_lum l ON h.lu_mgt_id=l.id "
                "WHERE c.id IN (%s)" % ",".join("?" * len(scope)), scope).fetchall()
            if len(rows) != 1:
                raise ValueError(f"显式HRU范围须同属于同一个地类；当前涉及 {len(rows)} 个地类")
            landuse_id = rows[0][0]
            attribute_clones = _separate_shared_attributes(con, scope)
            landuse_cons = _landuse_hrus(con, landuse_id)
            whole = set(scope) == set(landuse_cons)
            if whole:
                target_landuse, separation = landuse_id, None
            else:
                schedule = con.execute("SELECT mgt_id FROM landuse_lum WHERE id=?", (landuse_id,)).fetchone()[0]
                new_schedule, schedule_variant = _clone_schedule(con, schedule) if schedule else (None, None)
                target_landuse, variant_name = _clone_landuse(con, landuse_id, schedule_id=new_schedule)
                attributes = sorted(set(_connection_attributes(con, scope).values()))
                marks = ",".join("?" * len(attributes))
                con.execute(f"UPDATE hru_data_hru SET lu_mgt_id=? WHERE id IN ({marks})",
                            [target_landuse, *attributes])
                separation = {"landuse_variant": variant_name, "schedule_variant": schedule_variant,
                              "attribute_clones": {str(k): v for k, v in attribute_clones.items()}}
        else:
            row = con.execute("SELECT id FROM landuse_lum WHERE name=?", (landuse_name,)).fetchone()
            if not row:
                raise ValueError(f"未知地类：{landuse_name}")
            target_landuse, separation = row[0], None
        updates, changed = {}, {}
        if schedule_name:
            schedule = con.execute("SELECT id FROM management_sch WHERE name=?", (schedule_name,)).fetchone()
            if not schedule:
                raise ValueError(f"未知管理日程：{schedule_name}；可选："
                                 f"{[r[0] for r in con.execute('SELECT name FROM management_sch ORDER BY id')]}")
            con.execute("UPDATE landuse_lum SET mgt_id=? WHERE id=?", (schedule[0], target_landuse))
            updates["mgt_id"], changed["schedule"] = schedule[0], schedule_name
        if plant_community_name:
            community = con.execute("SELECT id FROM plant_ini WHERE name=?", (plant_community_name,)).fetchone()
            if not community:
                raise ValueError(f"未知植物群落：{plant_community_name}；可选："
                                 f"{[r[0] for r in con.execute('SELECT name FROM plant_ini ORDER BY id')]}")
            con.execute("UPDATE landuse_lum SET plnt_com_id=? WHERE id=?", (community[0], target_landuse))
            updates["plnt_com_id"], changed["plant_community"] = community[0], plant_community_name
        return {"stage": "configure (project copy)", "landuse_id": target_landuse,
                "scope": {"hru_ids": sorted(scope) if hru_ids else "whole landuse",
                          "landuse": landuse_name},
                "changed": changed, "separated_variants": separation,
                "database_written": True, "official_export_performed": False, "engine_run_performed": False}


def configure_constrained_irrigation(project_db, hru_ids=None, landuse_name=None,
                                     source_type="res", source_obj_id=1, amount_mm=5.0,
                                     stress_threshold=0.55, max_per_year=20,
                                     application_name="sprinkler_med", rule_name="irr_teach01"):
    """Write the teaching irrigation rule through official DB tables (copy only).

    Mirrors the official irr_str8_* decision-table rows: one condition
    (w_stress < threshold) and one action (irrigate from source, amount mm,
    max applications/year, application type from irr.ops). No existing rule,
    schedule, landuse or HRU record is modified; the HRU scope lives in the
    sidecar and later in water_hru_irr.wal, so unselected HRUs are untouched.
    """
    guard_writable(project_db)
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"未知水源类型 {source_type}；可用：{sorted(SOURCE_TYPES)}")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,20}", rule_name):
        raise ValueError("规则名须为小写字母开头的1-21位英文/数字/下划线")
    amount = float(amount_mm)
    threshold = float(stress_threshold)
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("灌溉定额须为正数（mm）")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("胁迫阈值须在0-1之间")
    if int(max_per_year) < 1:
        raise ValueError("年最大灌溉次数须>=1")
    scope = expand_scope(project_db, hru_ids, landuse_name)
    with _open(project_db) as con:
        table = SOURCE_TYPES[source_type]
        if con.execute(f"SELECT count(*) FROM {table} WHERE id=?", (source_obj_id,)).fetchone()[0] == 0:
            raise ValueError(f"水源对象不存在：{source_type} {source_obj_id}")
        # the constrained rule rides on a crop management schedule (auto op);
        # explicit-HRU scopes need the landuse that owns the schedule
        if landuse_name is None:
            # scope holds CONNECTION ids; the landuse must be resolved through
            # the official chain, not by filtering hru_data_hru.id with them
            lu_rows = con.execute(
                "SELECT DISTINCT l.name FROM hru_con c "
                "JOIN hru_data_hru h ON c.hru_id=h.id "
                "JOIN landuse_lum l ON h.lu_mgt_id=l.id "
                "WHERE c.id IN (%s)" % ",".join("?" * len(scope)), scope).fetchall()
            if len(lu_rows) != 1:
                raise ValueError(f"显式HRU范围必须属于同一个地类（其日程承接auto规则）；当前范围涉及地类：{[r[0] for r in lu_rows]}")
            landuse_name = lu_rows[0][0]
        row = con.execute("SELECT l.id,l.mgt_id FROM landuse_lum l WHERE l.name=?", (landuse_name,)).fetchone()
        if not row:
            raise ValueError(f"未知地类：{landuse_name}")
        if not row[1]:
            raise ValueError(f"地类 {landuse_name} 没有关联管理日程；请先用 assign 关联一个已有日程，"
                             "受限灌溉规则须挂在日程的auto操作上")
        landuse_id = row[0]
        # attribute records shared with unselected connections are cloned and
        # only the selected connections are re-pointed, before any landuse or
        # schedule change, so unselected chains cannot be affected
        attribute_clones = _separate_shared_attributes(con, scope)
        app = con.execute("SELECT id,amt_mm,eff_frac FROM irr_ops WHERE name=?", (application_name,)).fetchone()
        if not app:
            raise ValueError(f"未知灌溉方式：{application_name}；可用："
                             f"{[r[0] for r in con.execute('SELECT name FROM irr_ops ORDER BY id')]}")
        if con.execute("SELECT count(*) FROM d_table_dtl WHERE name=?", (rule_name,)).fetchone()[0]:
            raise ValueError(f"规则名已存在：{rule_name}")
        rule_id = (con.execute("SELECT max(id) FROM d_table_dtl").fetchone()[0] or 0) + 1
        con.execute("INSERT INTO d_table_dtl (id,name,file_name,description) VALUES (?,?,?,?)",
                    (rule_id, rule_name, "lum.dtl",
                     f"TEACHING irrigation rule: w_stress<{threshold} irrigate {amount}mm "
                     f"from {source_type} {source_obj_id} via {application_name}"))
        cond_id = (con.execute("SELECT max(id) FROM d_table_dtl_cond").fetchone()[0] or 0) + 1
        con.execute("INSERT INTO d_table_dtl_cond (id,d_table_id,var,obj,obj_num,lim_var,lim_op,lim_const,description)"
                    " VALUES (?,?,'w_stress','hru',0,'null','-',?,'TEACHING stress threshold')",
                    (cond_id, rule_id, threshold))
        alt_id = (con.execute("SELECT max(id) FROM d_table_dtl_cond_alt").fetchone()[0] or 0) + 1
        con.execute("INSERT INTO d_table_dtl_cond_alt (id,cond_id,alt) VALUES (?,?,'<')", (alt_id, cond_id))
        act_id = (con.execute("SELECT max(id) FROM d_table_dtl_act").fetchone()[0] or 0) + 1
        # engine xwalks the option column against irr.ops record names
        # (dtbl_lum_read.f90 case "irrigate"); the name column is a display label.
        con.execute("INSERT INTO d_table_dtl_act (id,d_table_id,act_typ,obj,obj_num,name,option,const,const2,fp)"
                    " VALUES (?,?,'irrigate',?,?,'irrigation',?,?,?,'null')",
                    (act_id, rule_id, source_type, source_obj_id, application_name,
                     amount, int(max_per_year)))
        out_id = (con.execute("SELECT max(id) FROM d_table_dtl_act_out").fetchone()[0] or 0) + 1
        con.execute("INSERT INTO d_table_dtl_act_out (id,act_id,outcome) VALUES (?,?,1)", (out_id, act_id))
        # Separate the schedule chain for the selected scope first so the new
        # rule can never reach HRUs outside the declared scope (shared
        # schedules and landuse-internal subsets both require a variant).
        schedule_id, schedule_variant, variant_landuse, variant_name = \
            _separate_schedule_for_scope(con, landuse_id, scope)
        # official schedule auto-operation link; engine exports it inside
        # management.sch and evaluates the rule daily with per-HRU counters
        auto_id = (con.execute("SELECT max(id) FROM management_sch_auto").fetchone()[0] or 0) + 1
        con.execute("INSERT INTO management_sch_auto (id,management_sch_id,d_table_id,plant1,plant2) "
                    "VALUES (?,?,?,?,NULL)", (auto_id, schedule_id, rule_id, None))
        # Actual reach through the official chain AFTER the change; must equal
        # the declared scope, otherwise the separation failed.
        actual_reach = sorted(row[0] for row in con.execute(
            "SELECT DISTINCT c.id FROM hru_con c "
            "JOIN hru_data_hru h ON c.hru_id=h.id "
            "JOIN landuse_lum l ON h.lu_mgt_id=l.id "
            "JOIN management_sch_auto a ON a.management_sch_id=l.mgt_id "
            "JOIN d_table_dtl d ON a.d_table_id=d.id WHERE d.name=?", (rule_name,)))
        if actual_reach != sorted(scope):
            raise RuntimeError(f"作用范围校验失败：声明 {sorted(scope)}，实际关联 {actual_reach}")
        enabled = _enabled_rules(con, schedule_id)
        original = con.execute("SELECT name FROM management_sch WHERE id=?", (schedule_id,)).fetchone()[0]
    sidecar = {"stage": "configure (project copy)",
               "project_db": str(Path(project_db).resolve()),
               "rule_name": rule_name, "rule_id": rule_id,
               "scope": {"hru_ids": scope, "count": len(scope),
                         "landuse": landuse_name, "explicit": bool(hru_ids)},
               "source": {"type": source_type, "obj_id": source_obj_id},
               "schedule_link": {"schedule_id": schedule_id, "schedule": original,
                                 "separated_variant": schedule_variant,
                                 "landuse_variant": variant_name,
                                 "route": "schedule_auto (official export, no file supplementation)"},
               "actual_reach": actual_reach, "enabled_rules": enabled,
               "id_semantics": "hru_con.id (connection/output unit numbering)",
               "separated_attribute_clones": {str(k): v for k, v in attribute_clones.items()},
               "amount_mm": amount, "stress_threshold": threshold,
               "max_per_year": int(max_per_year), "application": {"name": application_name, "id": app[0]},
               "teaching_parameters": True, "support": IRR_SUPPORT_NOTE,
               "database_written": True, "official_export_performed": False,
               "engine_input_supplemented": False, "engine_run_performed": False}
    Path(project_db).parent.joinpath("irrigation_setup.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sidecar


def supplement_engine_inputs(export_dir, project_db=None, sidecar=None):
    """Write the engine-format water-rights inputs into an EXPORT COPY.

    Writes water_hru_irr.wal (HRU -> rule links), place_of_use.wro (one POU of
    type 'irr') and an empty valid point_of_diver.wro, then points the export
    copy's file.cio water_rights row at them - all in the exact formats the
    local engine 62.0.1 reads. The official Editor export is not modified
    elsewhere; this is a workbench supplementation stage, reported separately.
    """
    export_dir = Path(export_dir)
    sidecar_path = Path(project_db).parent / "irrigation_setup.json" if project_db else None
    data = sidecar or json.loads(sidecar_path.read_text(encoding="utf-8"))
    rule = data["rule_name"]
    hrus = data["scope"]["hru_ids"]
    if not hrus:
        raise ValueError("sidecar中没有HRU范围")
    cio_path = export_dir / "file.cio"
    if not cio_path.is_file():
        raise FileNotFoundError(cio_path)
    wal = export_dir / "water_hru_irr.wal"
    wal.write_text(
        "water_hru_irr: teaching irrigation HRU links (workbench, engine format)\n"
        f"{1}\n"
        "name                       hrus\n"
        f"{'irr_district_teach':<26}{len(hrus)}\n"
        + "".join(f"{hru:<10}{rule}\n" for hru in sorted(hrus)), encoding="utf-8")
    pou = export_dir / "place_of_use.wro"
    pou.write_text(
        "place_of_use: teaching irrigation district (workbench, engine format)\n"
        f"{1}\n"
        "name                     typ      typ_num     pods       pors       dtbl_mx            rate_max      dtbl_pod_fr       dtbl_por_fr\n"
        f"{1:<10}{'irr_district_teach':<21}{'irr':<11}{1:<12}{0:<12}{0:<12}{'null':<20}{0.0:<15}{'null':<18}{'null':<18}\n",
        encoding="utf-8")
    pod = export_dir / "point_of_diver.wro"
    pod.write_text(
        "point_of_diver: none (teaching irrigation uses in-rule source deduction)\n"
        f"{0}\n"
        "name                     typ      obj_num     conv_typ   conv_num\n", encoding="utf-8")
    lines = cio_path.read_text(encoding="utf-8-sig").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.split() and line.split()[0] == "water_rights":
            lines[index] = f"water_rights      place_of_use.wro  point_of_diver.wro"
            replaced = True
            break
    if not replaced:
        raise ValueError("导出副本的file.cio中没有water_rights行")
    cio_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = {"stage": "supplement (workbench, engine input formats)",
              "export_dir": str(export_dir.resolve()),
              "files_written": ["water_hru_irr.wal", "place_of_use.wro", "point_of_diver.wro"],
              "file_cio_water_rights": "place_of_use.wro point_of_diver.wro",
              "hru_links": len(hrus), "rule": rule,
              "note": "本机Editor 4.0.2不导出这三件水权输入；此处按引擎62.0.1官方格式写入导出副本，官方导出未被改写",
              "database_written": False, "official_export_performed": False,
              "engine_input_supplemented": True, "engine_run_performed": False}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("inspect", help="只读列出地类/HRU分布、管理日程、灌溉方式与规则、水源对象")
    p.add_argument("--project-db", type=Path, required=True)
    p = sub.add_parser("configure", help="在项目副本上写入教学水源受限灌溉规则（官方决策表结构）")
    p.add_argument("--project-db", type=Path, required=True)
    p.add_argument("--landuse", default=None, help="按地类名选择HRU范围")
    p.add_argument("--hru-ids", type=int, nargs="+", default=None, help="或显式HRU编号")
    p.add_argument("--source-type", choices=sorted(SOURCE_TYPES), default="res")
    p.add_argument("--source-obj-id", type=int, default=1)
    p.add_argument("--amount-mm", type=float, default=5.0)
    p.add_argument("--stress-threshold", type=float, default=0.55)
    p.add_argument("--max-per-year", type=int, default=20)
    p.add_argument("--application", default="sprinkler_med")
    p.add_argument("--rule-name", default="irr_teach01")
    p = sub.add_parser("assign", help="把已有管理日程/植物群落关联到指定地类或HRU范围")
    p.add_argument("--project-db", type=Path, required=True)
    p.add_argument("--landuse", default=None)
    p.add_argument("--hru-ids", type=int, nargs="+", default=None)
    p.add_argument("--schedule", default=None)
    p.add_argument("--plant-community", default=None)
    p = sub.add_parser("export", help="官方Editor API导出（复用reservoir_setup.export_project）")
    p.add_argument("--project-db", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--print-object", action="append", default=[])
    args = parser.parse_args()
    if args.action == "inspect":
        print(json.dumps(read_management(args.project_db), ensure_ascii=False, indent=2))
    elif args.action == "configure":
        print(json.dumps(configure_constrained_irrigation(
            args.project_db, args.hru_ids, args.landuse, args.source_type, args.source_obj_id,
            args.amount_mm, args.stress_threshold, args.max_per_year, args.application, args.rule_name),
            ensure_ascii=False, indent=2))
    elif args.action == "assign":
        print(json.dumps(assign_management(args.project_db, args.landuse, args.hru_ids,
                                           args.schedule, args.plant_community),
                         ensure_ascii=False, indent=2))
    elif args.action == "export":
        print(json.dumps(export_project(args.project_db, args.output_dir, print_objects=args.print_object),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
