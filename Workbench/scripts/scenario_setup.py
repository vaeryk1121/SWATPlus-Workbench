"""Step-4A: official rule templates, scenario tables and batch scenario runs.

复用已验证的前3步接口（reservoir_setup / irrigation_setup / swatplus_trial /
reconnect_check），不重写科学逻辑。对外稳定接口（供4B GUI复用）：

  list_templates()                 模板类型与字段/单位
  load_scenario_table(path)        JSON/CSV 情景表读取
  export_scenario_table(rows,path) 情景表导出（round-trip）
  validate_scenario_table(rows)    导入前全量校验（不触碰项目）
  validate_scenario_row(row)       单行校验（apply 内部同样调用）
  apply_scenario(...)              单情景独立副本+模板应用（replace/append）
  plan_scenarios(table,work_dir)   运行前计划（情景数/调用次数/输出策略）
  run_scenario(manifest,...)       官方导出→隔离运行→官方直接对照（可取消）
  summarize_run(run,export_dir)    原生结果汇总（按对象/月分组；缺失标missing）
  write_report(results,path)       报告（按scenario键绑定，含失败/取消项）
  run_scenarios(table,work_dir,...)顺序批量（cancel_event/进度回调/失败策略）

失败语义：run_scenario 的 status ∈ completed | run_failed | comparison_failed |
cancelled；任何失败/取消不计成功；报告按 scenario 键绑定，不用错位zip。
"""
import argparse
import csv
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from reservoir_setup import (export_project, default_run_settings, available_rules,
                             set_release_rule, read_reservoirs, resolve_weather_dir)
from irrigation_setup import configure_constrained_irrigation

TEMPLATE_KINDS = ("reservoir_release_rule", "constrained_irrigation")
APPLICATION_MODES = ("replace", "append")
SCENARIO_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,30}\Z")
PRINT_STRATEGY = {"objects": ["basin_wb", "basin_res", "channel_sd", "reservoir", "hru_wb"],
                  "flags": {"daily": False, "monthly": True, "yearly": False, "avann": False},
                  "note": "精简输出策略：仅开启汇总所需对象的月输出（官方print_prt_object按标志位设置）；来源项目已有输出设置按preserve沿用，不静默关闭"}
FIELDS = {
    "reservoir_release_rule": {
        "required": ("rule_name",),
        "optional": {"reservoir_name": None},
        "units": {"rule_name": "项目已有官方 res_rel.dtl 决策表名称"},
        "note": "官方原模板选择，不改条件/动作语义；按名称解析，不写死规则id"},
    "constrained_irrigation": {
        "required": ("amount_mm", "source_type", "source_obj_id", "application_name"),
        "optional": {"stress_threshold": 0.55, "max_per_year": 20, "rule_name": None},
        "units": {"amount_mm": "mm/次（效率前定额，>0）", "stress_threshold": "0-1",
                  "max_per_year": "次/年>=1", "source_type": "res|cha|aqu",
                  "source_obj_id": "水源对象编号（连接对象号语义）",
                  "application_name": "项目 irr.ops 名称"},
        "note": "用户/教学参数经官方日程auto机制写入；连接对象号语义"},
}


def list_templates():
    field_pool = set()
    for kind in TEMPLATE_KINDS:
        field_pool.update(FIELDS[kind]["required"])
        field_pool.update(FIELDS[kind]["optional"])
    return {"templates": [{"kind": kind, "required": FIELDS[kind]["required"],
                           "optional": sorted(FIELDS[kind]["optional"]),
                           "units": FIELDS[kind]["units"], "note": FIELDS[kind]["note"]}
                          for kind in TEMPLATE_KINDS],
            "application_modes": APPLICATION_MODES,
            "print_strategy": PRINT_STRATEGY,
            "scenario_table_fields": ["scenario", "base_project", "mode", "kind",
                                      "scope", "landuse"] + sorted(field_pool)}


def load_scenario_table(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = data.get("scenarios") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("JSON情景表须为列表或含 scenarios 键的对象")
        return rows
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("CSV情景表为空")
    for row in rows:
        # 空CSV单元格与JSON缺省值语义一致：mode空=replace
        if row.get("mode") is None or not str(row.get("mode")).strip():
            row["mode"] = "replace"
    return rows


def export_scenario_table(rows, path):
    """Export scenario rows to JSON or CSV (round-trip with load)."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps({"scenarios": rows}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    else:
        field_pool = set()
        for kind in TEMPLATE_KINDS:
            field_pool.update(FIELDS[kind]["required"])
            field_pool.update(FIELDS[kind]["optional"])
        fields = ["scenario", "base_project", "mode", "kind", "scope", "landuse",
                  "parameter_identity"] + sorted(field_pool)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    return str(path)


def _scenario_problems(row, index, work_root, deep=True):
    problems = []
    label = f"行{index}({row.get('scenario', '?')})"
    name = str(row.get("scenario", "")).strip()
    if not name:
        problems.append(f"{label}: 缺少scenario名称")
    elif not SCENARIO_NAME.match(name):
        problems.append(f"{label}: 情景名须为字母开头的1-31位英文/数字/下划线/连字符，不能含路径分隔：{name!r}")
    base = str(row.get("base_project", "")).strip()
    base_path = Path(base) if base else None
    if base_path is not None and not base_path.is_absolute():
        base_path = work_root / base_path
    if not base:
        problems.append(f"{label}: 缺少base_project")
    elif not base_path.is_file():
        problems.append(f"{label}: 基础项目库不存在 {base_path}")
    kind = row.get("kind")
    if kind not in TEMPLATE_KINDS:
        problems.append(f"{label}: kind须为 {'/'.join(TEMPLATE_KINDS)}，得到 {kind}")
        return problems, base_path
    for field in FIELDS[kind]["required"]:
        if not str(row.get(field, "")).strip():
            problems.append(f"{label}: {kind} 缺少必填字段 {field}")
    mode = row.get("mode", "replace")
    if mode not in APPLICATION_MODES:
        problems.append(f"{label}: mode须为 {'/'.join(APPLICATION_MODES)}，得到 {mode}")
    if deep and base_path is not None and base_path.is_file():
        problems.extend(_reference_problems(row, label, kind, base_path))
    return problems, base_path


def _reference_problems(row, label, kind, base_path):
    problems = []
    try:
        connection = sqlite3.connect(f"file:{base_path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        return [f"{label}: 无法打开基础项目：{error}"]
    try:
        if kind == "reservoir_release_rule":
            rule = str(row.get("rule_name", "")).strip()
            names = [r[0] for r in connection.execute(
                "SELECT name FROM d_table_dtl WHERE file_name='res_rel.dtl'")]
            if rule and rule not in names:
                problems.append(f"{label}: 基础项目没有放水规则 {rule}；可用：{names}")
            reservoir_name = str(row.get("reservoir_name", "")).strip()
            if reservoir_name:
                res_names = [r[0] for r in connection.execute(
                    "SELECT name FROM reservoir_con") if "reservoir_con" in
                    {x[0] for x in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}]
                if reservoir_name not in res_names:
                    problems.append(f"{label}: 基础项目没有水库对象 {reservoir_name}；可用：{res_names}")
        elif kind == "constrained_irrigation":
            mode = row.get("mode", "replace")
            scope = str(row.get("scope", "")).strip()
            landuse = str(row.get("landuse", "")).strip()
            if bool(scope) == bool(landuse):
                problems.append(f"{label}: 受限灌溉须且只须指定 scope（逗号分隔连接HRU编号）或 landuse 之一")
            custom_rule = str(row.get("rule_name", "")).strip()
            if custom_rule and not re.fullmatch(r"[a-z][a-z0-9_]{0,20}", custom_rule):
                problems.append(f"{label}: rule_name须为小写字母开头的1-21位英文/数字/下划线，得到 {custom_rule!r}")
            elif custom_rule:
                existing = [r[0] for r in connection.execute("SELECT name FROM d_table_dtl")]
                if custom_rule in existing:
                    problems.append(f"{label}: 规则名已存在于基础项目（创建副本前报告）：{custom_rule}")
            if mode == "replace":
                # 混合动作规则预检：scope与地类两条入口都覆盖（校验期即拒绝）
                scope_pre = [int(x) for x in scope.split(",") if x.strip().isdigit()] if scope else None
                schedules_pre = _scope_schedules(connection, landuse or None, scope_pre)
                for schedule_id in schedules_pre:
                    _, mixed = _irrigation_autos_on_schedule(connection, schedule_id)
                    if mixed:
                        problems.append(f"{label}: 日程(id={schedule_id})上的规则 {mixed} 混合灌溉与其他动作，"
                                        "不能安全自动替换；请在官方Editor中手动处理或选择不同范围")
            if scope:
                known = {r[0] for r in connection.execute("SELECT id FROM hru_con")}
                ids = []
                for part in scope.split(","):
                    token = part.strip()
                    if not token.isdigit() or int(token) not in known:
                        problems.append(f"{label}: scope含未知连接HRU编号 {token!r}")
                    else:
                        ids.append(int(token))
                if len(ids) >= 2:
                    marks = ",".join("?" * len(ids))
                    landuses = [r[0] for r in connection.execute(
                        f"SELECT DISTINCT l.name FROM hru_con c JOIN hru_data_hru h ON c.hru_id=h.id "
                        f"JOIN landuse_lum l ON h.lu_mgt_id=l.id WHERE c.id IN ({marks})", ids)]
                    if len(landuses) > 1:
                        problems.append(f"{label}: scope跨地类 {landuses}；第3步接口只允许同一个地类")
            elif landuse:
                names = [r[0] for r in connection.execute("SELECT name FROM landuse_lum")]
                if landuse not in names:
                    problems.append(f"{label}: 未知地类 {landuse}；可用：{names}")
                else:
                    mgt = connection.execute("SELECT mgt_id FROM landuse_lum WHERE name=?",
                                             (landuse,)).fetchone()[0]
                    if not mgt:
                        problems.append(f"{label}: 地类 {landuse} 没有关联管理日程")
            application = str(row.get("application_name", "")).strip()
            if application:
                names = [r[0] for r in connection.execute("SELECT name FROM irr_ops")]
                if application not in names:
                    problems.append(f"{label}: 未知灌溉方式 {application}；可用：{names}")
            source_type = str(row.get("source_type", "")).strip()
            source_table = {"res": "reservoir_con", "cha": "chandeg_con",
                            "aqu": "aquifer_con"}.get(source_type)
            if source_type and source_type not in ("res", "cha", "aqu"):
                problems.append(f"{label}: source_type须为 res/cha/aqu，得到 {source_type}")
            elif source_table:
                tables = {r[0] for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if source_table not in tables:
                    problems.append(f"{label}: 项目没有 {source_table} 表（水源类型 {source_type}）")
                else:
                    raw = str(row.get("source_obj_id", "")).strip()
                    if not raw.isdigit():
                        problems.append(f"{label}: source_obj_id须为正整数，得到 {raw!r}")
                    else:
                        known = {r[0] for r in connection.execute(f"SELECT id FROM {source_table}")}
                        if int(raw) not in known:
                            problems.append(f"{label}: 水源对象不存在 {source_type} {raw}")
        for field, lo, hi in (("amount_mm", 0.0, None), ("stress_threshold", 0.0, 1.0)):
            raw = str(row.get(field, "")).strip()
            if raw:
                try:
                    value = float(raw)
                    if not math.isfinite(value) or value <= lo or (hi is not None and value >= hi):
                        problems.append(f"{label}: {field}={raw} 超出允许范围（>{lo}"
                                        + (f"且<{hi}" if hi is not None else "") + "）")
                except ValueError:
                    problems.append(f"{label}: {field}={raw!r} 不是有限数值")
        raw = str(row.get("max_per_year", "")).strip()
        if raw and (not raw.isdigit() or int(raw) < 1):
            problems.append(f"{label}: max_per_year须为>=1的整数，得到 {raw!r}")
        # weather resolution must be checked at import time too
        try:
            resolve_weather_dir(base_path)
        except (ValueError, FileNotFoundError) as error:
            problems.append(f"{label}: 天气目录无法按项目设置解析：{error}")
    finally:
        connection.close()
    return problems


def project_choices(project_db):
    """Read-only picker metadata for the GUI: available rules/objects by NAME
    plus period/weather. Same official tables the validation reads; no logic."""
    path = Path(project_db)
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    info = {"project_db": str(path), "project_name": None, "start_year": None,
            "end_year": None, "warmup_years": None, "weather_dir": None,
            "period_error": None, "weather_error": None,
            "release_rules": [], "decision_tables": [], "reservoirs": [],
            "channels": [], "aquifers": [], "landuses": [], "irr_ops": [],
            "res_ids": [], "cha_ids": [], "aqu_ids": []}
    try:
        def names(sql):
            try:
                return [r[0] for r in connection.execute(sql)]
            except sqlite3.Error:
                return []
        try:
            settings = default_run_settings(path)
            info.update(project_name=settings["task_name"].rsplit("_res_compare", 1)[0],
                        start_year=settings["start_year"], end_year=settings["end_year"],
                        warmup_years=settings["warmup_years"])
        except Exception as error:
            info["period_error"] = str(error)
        try:
            info["weather_dir"] = str(resolve_weather_dir(path))
        except (ValueError, FileNotFoundError, sqlite3.Error) as error:
            info["weather_error"] = str(error)
        info["release_rules"] = names(
            "SELECT name FROM d_table_dtl WHERE file_name='res_rel.dtl' ORDER BY id")
        info["decision_tables"] = names("SELECT name FROM d_table_dtl")
        info["reservoirs"] = names("SELECT name FROM reservoir_con ORDER BY id")
        info["channels"] = names("SELECT name FROM chandeg_con ORDER BY id")
        info["aquifers"] = names("SELECT name FROM aquifer_con ORDER BY id")
        info["landuses"] = names("SELECT name FROM landuse_lum ORDER BY id")
        info["irr_ops"] = names("SELECT name FROM irr_ops ORDER BY id")
        info["res_ids"] = names("SELECT id FROM reservoir_con ORDER BY id")
        info["cha_ids"] = names("SELECT id FROM chandeg_con ORDER BY id")
        info["aqu_ids"] = names("SELECT id FROM aquifer_con ORDER BY id")
    finally:
        connection.close()
    return info


def validate_scenario_row(row, work_root=None):
    problems, _ = _scenario_problems(row, 1, Path(work_root) if work_root else ROOT, deep=True)
    return problems


def validate_scenario_table(rows, work_root=None):
    seen, problems = set(), []
    for index, row in enumerate(rows, 1):
        row_problems, _ = _scenario_problems(row, index, Path(work_root) if work_root else ROOT)
        problems.extend(row_problems)
        name = str(row.get("scenario", "")).strip()
        if name:
            if name in seen:
                problems.append(f"行{index}: 情景名重复 {name}")
            seen.add(name)
    return problems


def _irrigation_autos_on_schedule(connection, schedule_id):
    """Auto links whose dtbl contains irrigate actions (by action semantics);
    mixed-action rules are flagged separately and never auto-removed."""
    pure, mixed = [], []
    for auto_id, dtbl_id, dtbl_name in connection.execute(
            "SELECT a.id,d.id,d.name FROM management_sch_auto a "
            "JOIN d_table_dtl d ON a.d_table_id=d.id WHERE a.management_sch_id=?", (schedule_id,)):
        actions = [r[0] for r in connection.execute(
            "SELECT act_typ FROM d_table_dtl_act WHERE d_table_id=?", (dtbl_id,))]
        if not actions:
            continue
        if all(action == "irrigate" for action in actions):
            pure.append({"auto_id": auto_id, "dtbl": dtbl_name})
        elif any(action == "irrigate" for action in actions):
            mixed.append(dtbl_name)
    return pure, mixed


def _scope_schedules(connection, landuse_name=None, scope_connections=None):
    """Schedule ids of the actual selected range; either entry resolves through
    the official chain, scope_connections being hru_con ids."""
    if landuse_name:
        return [r[0] for r in connection.execute(
            "SELECT DISTINCT s.id FROM landuse_lum l JOIN management_sch s ON l.mgt_id=s.id "
            "WHERE l.name=?", (landuse_name,))]
    if scope_connections:
        marks = ",".join("?" * len(scope_connections))
        return [r[0] for r in connection.execute(
            f"SELECT DISTINCT s.id FROM hru_con c JOIN hru_data_hru h ON c.hru_id=h.id "
            f"JOIN landuse_lum l ON h.lu_mgt_id=l.id JOIN management_sch s ON l.mgt_id=s.id "
            f"WHERE c.id IN ({marks})", scope_connections)]
    return []


def apply_scenario(base_project, scenario_name, row, target_dir, work_root=None):
    """Create ONE independent scenario copy and apply the template.

    replace: 灌溉模板先移除选中范围日程上已有的纯灌溉auto规则（按动作语义识别，
    不按TEACHING文字），保留播种/收获/轮作与未选对象；混合动作规则明确拒绝。
    append: 保留旧规则并新增，报告列出 old+new。
    """
    problems = validate_scenario_row(row, work_root)
    if problems:
        raise ValueError("情景行校验未通过：\n" + "\n".join(problems))
    base_project = Path(base_project)
    if not base_project.is_absolute():
        base_project = (Path(work_root) if work_root else ROOT) / base_project
    kind = row.get("kind")
    mode = row.get("mode", "replace")
    target_dir = Path(target_dir)
    if target_dir.exists():
        raise FileExistsError(f"情景目录已存在，保留不动：{target_dir}")
    # 混合灌溉规则在只读基础库上先行检测（任何复制之前；scope与landuse两条入口），
    # 拒绝时不留部分副本
    if kind == "constrained_irrigation" and mode == "replace":
        scope_raw = str(row.get("scope", "")).strip()
        scope_pre = [int(x) for x in scope_raw.split(",") if x.strip().isdigit()] if scope_raw else None
        with sqlite3.connect(f"file:{base_project}?mode=ro", uri=True) as con:
            schedules_pre = _scope_schedules(con, row.get("landuse"), scope_pre)
            for schedule_id in schedules_pre:
                _, mixed = _irrigation_autos_on_schedule(con, schedule_id)
                if mixed:
                    raise ValueError(f"日程(id={schedule_id})上的规则 {mixed} 混合灌溉与其他动作，"
                                     "不能安全自动替换；请在官方Editor中手动处理或选择不同范围")
    target_dir.mkdir(parents=True)
    db = target_dir / f"{scenario_name}.sqlite"
    shutil.copy2(base_project, db)
    datasets = base_project.parent / "swatplus_datasets.sqlite"
    if datasets.is_file():
        shutil.copy2(datasets, target_dir / datasets.name)
    weather_source = resolve_weather_dir(base_project)
    shutil.copytree(weather_source, target_dir / "weather_input")
    with sqlite3.connect(db) as con:
        con.execute("UPDATE project_config SET weather_data_dir='weather_input', "
                    "input_files_dir='TxtInOut'")
    changes, replaced, kept = [], [], []
    if kind == "reservoir_release_rule":
        rules = {item["name"]: item["id"] for item in available_rules(db)}
        rule_name = row["rule_name"]
        reservoirs = read_reservoirs(db)["reservoirs"]
        target = row.get("reservoir_name")
        matched = [r for r in reservoirs if not target or r["name"] == target]
        if not matched:
            raise ValueError(f"未找到水库对象（筛选 {target}）")
        for reservoir in matched:
            previous = reservoir["properties"]["release_rule"]
            set_release_rule(db, reservoir["id"], rules[rule_name])
            changes.append({"template": kind, "reservoir": reservoir["name"],
                            "previous_rule": previous, "release_rule": rule_name,
                            "mode": "replace(规则选择按定义替换)"})
    elif kind == "constrained_irrigation":
        scope = str(row.get("scope", "")).strip()
        scope_connections = [int(x) for x in scope.split(",") if x.strip()] if scope else None
        if scope_connections is None:
            # 地类入口：预先解析其连接集合（分离后按连接回查，避免误改共享原日程）
            with sqlite3.connect(f"file:{base_project}?mode=ro", uri=True) as con:
                scope_connections = [r[0] for r in con.execute(
                    "SELECT DISTINCT c.id FROM hru_con c JOIN hru_data_hru h ON c.hru_id=h.id "
                    "JOIN landuse_lum l ON h.lu_mgt_id=l.id WHERE l.name=? ORDER BY c.id",
                    (row["landuse"],))]
        kwargs = dict(amount_mm=float(row["amount_mm"]),
                      source_type=row["source_type"], source_obj_id=int(row["source_obj_id"]),
                      application_name=row["application_name"],
                      stress_threshold=float(row.get("stress_threshold") or 0.55),
                      max_per_year=int(row.get("max_per_year") or 20))
        if row.get("rule_name"):
            kwargs["rule_name"] = row["rule_name"]
        if scope_connections:
            kwargs["hru_ids"] = scope_connections
        else:
            kwargs["landuse_name"] = row["landuse"]
        # 分离先行（configure内部克隆共享日程/地类/属性），新规则挂到选中副本；
        # 之后仅对选中连接现指向的日程移除纯灌溉auto——共享原日程不被先改坏。
        report = configure_constrained_irrigation(db, **kwargs)
        with sqlite3.connect(db) as con:
            schedules = _scope_schedules(con, None, scope_connections)
            for schedule_id in schedules:
                pure, mixed = _irrigation_autos_on_schedule(con, schedule_id)
                if mixed:
                    raise ValueError(f"日程(id={schedule_id})上的规则 {mixed} 混合灌溉与其他动作，"
                                     "不能安全自动替换；请在官方Editor中手动处理或选择不同范围")
                for item in pure:
                    if item["dtbl"] == report["rule_name"]:
                        continue  # 刚添加的新规则不是被替换对象
                    if mode == "replace":
                        con.execute("DELETE FROM management_sch_auto WHERE id=?", (item["auto_id"],))
                        replaced.append(item["dtbl"])
                    else:
                        kept.append(item["dtbl"])
        # 移除/追加完成后沿选中连接实际链重读启用规则，替换manifest旧列表
        with sqlite3.connect(db) as con:
            actual_enabled = sorted({r[0] for cid in scope_connections
                                     for r in con.execute(
                "SELECT d.name FROM hru_con c JOIN hru_data_hru h ON c.hru_id=h.id "
                "JOIN landuse_lum l ON h.lu_mgt_id=l.id "
                "JOIN management_sch_auto a ON a.management_sch_id=l.mgt_id "
                "JOIN d_table_dtl d ON a.d_table_id=d.id WHERE c.id=?", (cid,))})
        changes.append({"template": kind, "mode": mode,
                        "rule": report["rule_name"],
                        "replaced_irrigation_rules": sorted(set(replaced)) or None,
                        "kept_irrigation_rules_append": sorted(set(kept)) or None,
                        "scope_hrus": report["scope"]["count"],
                        "enabled_rules": actual_enabled})
        sidecar = Path(db).parent / "irrigation_setup.json"
        if sidecar.is_file():
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            data["enabled_rules_after_apply"] = actual_enabled
            sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                          allow_nan=False) + chr(10), encoding="utf-8")
    else:
        raise ValueError(f"未知模板类型 {kind}")
    manifest = {"scenario": scenario_name, "base_project": str(base_project.resolve()),
                "project_db": str(db), "kind": kind, "mode": mode,
                "base_project_weather_dir": str(weather_source),
                "changes": changes,
                "parameter_identity": row.get("parameter_identity") or "not declared",
                "database_written": True, "official_export_performed": False,
                "engine_run_performed": False}
    (target_dir / "scenario_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return manifest


def final_status(entry):
    """Final scenario state: run facts first; summary failure downgrades."""
    run = entry.get("run") or {}
    summary = entry.get("summary") or {}
    if summary.get("status") in ("summary_failed", "summary_incomplete"):
        return summary["status"]
    status = run.get("status")
    if status in ("run_failed", "comparison_failed", "cancelled"):
        return status
    if status == "completed" and not entry.get("summary"):
        return "summary_failed"
    if summary.get("status") == "completed":
        return "completed"
    if entry.get("error"):
        return "summary_failed"
    return status or "failed"


def exit_code_for(result):
    """0 all completed, 1 all failed, 2 partial, 3 any cancelled/not-executed.

    Based on final states of EXECUTED items; cancelled or not-executed items
    (user cancel or skipped after failure) always yield 3.
    """
    states = [final_status(entry) for entry in result["results"]]
    if result.get("not_executed") or "cancelled" in states:
        return 3
    succeeded = sum(1 for s in states if s == "completed")
    if not states or succeeded == 0:
        return 1
    if succeeded == len(states) and not result.get("failures"):
        return 0
    return 2


def plan_scenarios(table_path, work_dir, compare_with_direct=True):
    if not compare_with_direct:
        raise ValueError("plan: compare_with_direct=False 与 run_scenario 一致，尚不支持，不能给出可执行计划")
    rows = load_scenario_table(table_path)
    work_dir = Path(work_dir)
    return {"scenarios": len(rows),
            "names": [r.get("scenario") for r in rows],
            "model_calls_per_scenario": 2 if compare_with_direct else 1,
            "total_model_calls": len(rows) * (2 if compare_with_direct else 1),
            "compare_with_direct": compare_with_direct,
            "work_dir": str(work_dir),
            "print_strategy": PRINT_STRATEGY,
            "note": "每情景=官方API导出+隔离试跑；开启直接对照时另有官方直接运行一次"}


def _enable_required_yearly(export_dir):
    """Enable basin_wb yearly in the REAL print.prt (object name and four
    flags share one line) BEFORE any engine run, so both sides actually
    produce the required yearly output. Returns the change report; a missing
    or malformed row is an error, never a silent excuse."""
    from swatplus_trial import read_lines, write_lines
    rows_ = read_lines(Path(export_dir) / "print.prt")
    changed = {}
    for index, line in enumerate(rows_):
        fields = line.split()
        if fields and fields[0] == "basin_wb":
            if len(fields) != 5:
                raise ValueError(f"print.prt basin_wb行格式异常（应为对象名+4开关同行）：{line!r}")
            if fields[3] != "y":
                fields[3] = "y"
                rows_[index] = f"{fields[0]:24s} {fields[1]:12s} {fields[2]:14s} {fields[3]:14s} {fields[4]}"
                changed["basin_wb"] = {"yearly": "enabled"}
            else:
                changed["basin_wb"] = {"yearly": "kept"}
            write_lines(Path(export_dir) / "print.prt", rows_)
            return changed
    raise ValueError("print.prt 缺少 basin_wb 行；无法开启必要的年输出")


def _apply_print_strategy(db, strategy="monthly_summary", objects=None):
    """Apply a declared output strategy to official print_prt_object rows.

    preserve: 只把汇总所需对象补开 monthly（来源 daily/yearly/avann 保留）；
    monthly_summary: 五类对象强制 0/1/0/0（调用方显式选择精简）。
    返回逐对象逐标志的变化报告（kept/added/removed + 原值）。
    """
    objects = objects or PRINT_STRATEGY["objects"]
    changes = {}
    with sqlite3.connect(db) as con:
        for name in objects:
            before = con.execute("SELECT daily,monthly,yearly,avann FROM print_prt_object "
                                 "WHERE name=?", (name,)).fetchone()
            if before is None:
                raise ValueError(f"未知打印对象：{name}")
            if strategy == "preserve":
                target = (before[0], 1, before[2], before[3])
            elif strategy == "monthly_summary":
                target = (0, 1, 0, 0)
            else:
                raise ValueError(f"未知输出策略 {strategy}；可用 preserve/monthly_summary")
            con.execute("UPDATE print_prt_object SET daily=?,monthly=?,yearly=?,avann=? "
                        "WHERE name=?", (*target, name))
            labels = ("daily", "monthly", "yearly", "avann")
            item = {}
            for label, old_v, new_v in zip(labels, before, target):
                item[label] = "kept" if old_v == new_v else (
                    f"{old_v}->{new_v}" if new_v else "removed")
            changes[name] = item
    return changes


def run_scenario(manifest, output_dir, cancel_event=None, progress=None,
                 compare_with_direct=True, start_year=None, end_year=None, warmup=None,
                 output_strategy="monthly_summary"):
    """Official export -> isolated run (+ official direct comparison).

    status: completed | run_failed | comparison_failed | cancelled.
    compare_with_direct=False is rejected BEFORE any export or write.
    """
    if not compare_with_direct:
        raise ValueError("compare_with_direct=False 尚未实现：在任何导出/写入之前明确拒绝")
    db = Path(manifest["project_db"])
    settings = default_run_settings(db)
    start = int(start_year or settings["start_year"])
    end = int(end_year or settings["end_year"])
    warm = int(warmup if warmup is not None else settings["warmup_years"])
    output_dir = Path(output_dir)
    if progress:
        progress(manifest["scenario"], "export")
    print_changes = _apply_print_strategy(db, strategy=output_strategy)
    export = export_project(db, output_dir=output_dir)
    # 必比年输出在两次引擎执行之前开启（导出副本上）；执行后修改会损害复现
    print_changes.update(_enable_required_yearly(export["output_dir"]))
    if cancel_event is not None and cancel_event.is_set():
        return {"scenario": manifest["scenario"], "status": "cancelled",
                "export_dir": str(output_dir), "engine_run_performed": False,
                "official_export_performed": True}
    from swatplus_workflow import identify_outlet, prepare_input_snapshot
    from swatplus_trial import read_lines, write_lines, run_trial
    from reconnect_check import compare_outputs
    runtime = json.loads((ROOT / "config/qgis344_runtime.json").read_text(encoding="utf-8"))
    engine = Path(runtime["model_executable"])
    if not engine.is_file():
        raise FileNotFoundError(engine)
    source = Path(export["output_dir"])
    outlet = identify_outlet(source, "auto")
    direct = output_dir.parent / "direct_run"
    prepare_input_snapshot(source, direct / "TxtInOut")
    direct_model = direct / "TxtInOut"
    rows = read_lines(direct_model / "time.sim")
    rows[2] = f"0 {start} 0 {end} 0"
    write_lines(direct_model / "time.sim", rows)
    rows = read_lines(direct_model / "print.prt")
    rows[2] = f"{warm} 0 0 0 0 1"
    write_lines(direct_model / "print.prt", rows)
    if progress:
        progress(manifest["scenario"], "direct_run")
    with (direct / "console.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run([str(engine)], cwd=direct_model, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, timeout=1800, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    direct_ok = completed.returncode == 0 and (direct_model / "success.fin").is_file()
    if not direct_ok:
        return {"scenario": manifest["scenario"], "status": "run_failed",
                "stage": "official_direct", "direct_exit": completed.returncode,
                "export_dir": str(source), "engine_run_performed": True,
                "official_export_performed": True}
    if cancel_event is not None and cancel_event.is_set():
        return {"scenario": manifest["scenario"], "status": "cancelled",
                "export_dir": str(source), "engine_run_performed": True,
                "official_export_performed": True}
    if progress:
        progress(manifest["scenario"], "workbench_run")
    spec = {"root": str(ROOT), "input_dir": str(source), "executable": str(engine),
            "expected_version": "62.0.1", "work_dir": str(output_dir.parent / "workbench_trial"),
            "start_year": start, "end_year": end, "warmup_years": warm,
            "frequency": "monthly", "outlet": outlet, "parameters": [],
            "print_policy": "preserve", "timeout_seconds": 1800,
            "engine_info": {"mode": "default", "source": "scenario batch"}}
    try:
        workbench = run_trial(spec, ROOT, cancel_event=cancel_event)
    except InterruptedError:
        if cancel_event is not None:
            cancel_event.set()
        return {"scenario": manifest["scenario"], "status": "cancelled",
                "export_dir": str(source), "engine_run_performed": True,
                "official_export_performed": True}
    if cancel_event is not None and cancel_event.is_set():
        return {"scenario": manifest["scenario"], "status": "cancelled",
                "export_dir": str(source), "engine_run_performed": True,
                "official_export_performed": True}
    if workbench["status"] != "completed":
        return {"scenario": manifest["scenario"], "status": "run_failed",
                "stage": "workbench", "workbench_status": workbench["status"],
                "export_dir": str(source), "engine_run_performed": True,
                "official_export_performed": True}
    if progress:
        progress(manifest["scenario"], "comparison")
    outlet_object = outlet.get("object", "channel_sd")
    # 年输出已在执行前开启：无豁免，必比完整
    comparison = compare_outputs(direct_model, Path(workbench["model_dir"]), "monthly",
                                 outlet_object=outlet_object)
    return {"scenario": manifest["scenario"],
            "status": "completed" if comparison["comparison_passed"] else "comparison_failed",
            "comparison_passed": comparison["comparison_passed"],
            "missing_required": comparison["missing_required"],
            "outlet_object": outlet_object,
            "export_dir": str(source),
            "workbench_model_dir": workbench["model_dir"],
            "direct_model_dir": str(direct_model),
            "actual_period": {"start_year": start, "end_year": end, "warmup_years": warm},
            "print_changes": print_changes, "output_strategy": output_strategy,
            "engine_run_performed": True, "official_export_performed": True}


def summarize_run(run_result, export_dir=None):
    """Native-output summary grouped by reservoir object and month; missing
    required outputs are reported as missing, never defaulted to 0."""
    from swatplus_profiles import read_output_header
    model = Path(run_result["workbench_model_dir"])
    export_dir = Path(export_dir or run_result["export_dir"])
    manifest = json.loads((export_dir.parent / "scenario_manifest.json").read_text(encoding="utf-8"))
    db = manifest["project_db"]
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    # NULL/0面积保留原值：完整性检查需区分缺失面积，不能悄悄当0
    area = {row[0]: row[1] for row in connection.execute("SELECT id,area FROM hru_con")}
    settings = default_run_settings(db)
    connection.close()
    actual = run_result.get("actual_period")
    if actual:
        settings = {"start_year": actual["start_year"], "end_year": actual["end_year"],
                    "warmup_years": actual["warmup_years"]}
    first_change = manifest["changes"][0] if manifest.get("changes") else {}
    release_rule = first_change.get("release_rule")
    summary = {"scenario": run_result["scenario"], "status": run_result["status"],
               "comparison_passed": run_result.get("comparison_passed"),
               "base_project": manifest["base_project"], "kind": manifest["kind"],
               "mode": manifest["mode"],
               "enabled_rules": (first_change.get("enabled_rules") if first_change else None),
               "release_rule": release_rule,
               "period": {"start": settings["start_year"], "end": settings["end_year"],
                          "warmup": settings["warmup_years"]},
               "parameter_identity": manifest.get("parameter_identity")}
    hru_mon = model / "hru_wb_mon.txt"
    if hru_mon.is_file():
        field_irr, bad, unknown_units, records = 0.0, 0, set(), 0
        missing_area = set()
        seen_keys = {}
        expected_units = {u for u, a in area.items() if a and a > 0}
        null_area_units = {u for u, a in area.items() if not a}
        expected_months = (settings["end_year"] - settings["start_year"]
                           - settings["warmup_years"] + 1) * 12
        with hru_mon.open(encoding="utf-8-sig") as stream:
            _, header, _ = read_output_header(stream, "62.0.1", "hru_wb")
            iu, ii, iy, im = (header.index("unit"), header.index("irr"),
                              header.index("yr"), header.index("mon"))
            for line in stream:
                f = line.split()
                if len(f) != len(header):
                    bad += 1
                    continue
                unit = int(f[iu])
                if unit not in area or not area[unit]:
                    unknown_units.add(unit)
                    continue
                key = (unit, int(f[iy]), int(f[im]))
                seen_keys[key] = seen_keys.get(key, 0) + 1
                records += 1
                field_irr += float(f[ii]) * area[unit] * 10.0
        reasons = []
        if bad:
            reasons.append(f"{bad} malformed rows")
        if unknown_units:
            reasons.append(f"unknown unit ids {sorted(unknown_units)} (area mapping missing)")
        if null_area_units:
            reasons.append(f"NULL/zero area for units {sorted(null_area_units)}")
        if records == 0:
            reasons.append("no records")
        else:
            expected_keys = {(u, y, m) for u in expected_units
                             for y in range(settings["start_year"] + settings["warmup_years"],
                                            settings["end_year"] + 1)
                             for m in range(1, 13)}
            seen = set(seen_keys)
            missing = expected_keys - seen
            duplicates = sorted(k for k, n in seen_keys.items() if n > 1)
            outside = sorted(k for k in seen if k not in expected_keys)
            if missing:
                reasons.append(f"missing {len(missing)} (unit,year,month) records, "
                               f"e.g. {sorted(missing)[:3]}")
            if duplicates:
                reasons.append(f"duplicate (unit,year,month) records {duplicates[:3]}")
            if outside:
                reasons.append(f"records outside period {outside[:3]}")
        if reasons:
            # 不完整数据不输出看似完整的合计
            summary["field_irrigation_m3_efficiency_after"] = None
            summary["field_irrigation_incomplete_reason"] = "; ".join(reasons)
        else:
            summary["field_irrigation_m3_efficiency_after"] = round(field_irr, 1)
        summary["hru_rows_malformed"] = bad or None
    else:
        summary["field_irrigation_m3_efficiency_after"] = None
        summary["field_irrigation_missing_output"] = str(hru_mon.name)
    basin = model / "basin_wb_mon.txt"
    if basin.is_file():
        with basin.open(encoding="utf-8-sig") as stream:
            _, header, _ = read_output_header(stream, "62.0.1", "basin_wb")
            ii = header.index("irr")
            summary["basin_irrigation_mm"] = round(
                sum(float(line.split()[ii]) for line in stream if line.split()), 3)
    else:
        summary["basin_irrigation_mm"] = None
        summary["basin_irrigation_missing_output"] = str(basin.name)
    res = model / "reservoir_mon.txt"
    con2 = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    tables = {r[0] for r in con2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "reservoir_con" in tables:
        expected_reservoirs = {(r[0], r[1]) for r in con2.execute(
            "SELECT id,name FROM reservoir_con ORDER BY id")}
    else:
        expected_reservoirs = set()
    con2.close()
    reservoirs = None
    missing_whole = []
    if expected_reservoirs:
        if res.is_file():
            try:
                with res.open(encoding="utf-8-sig") as stream:
                    _, header, _ = read_output_header(stream, "62.0.1", "reservoir")
                    ix = {k: header.index(k) for k in ("unit", "name", "yr", "mon",
                                                       "flo_stor", "flo_in", "flo_out")}
                    by_object = {}
                    malformed = 0
                    for line in stream:
                        f = line.split()
                        if len(f) != len(header):
                            malformed += 1
                            continue
                        key = (int(f[ix["unit"]]), f[ix["name"]])
                        by_object.setdefault(key, []).append(
                            {"yr": int(f[ix["yr"]]), "mon": int(f[ix["mon"]]),
                             "stor_m3": float(f[ix["flo_stor"]]),
                             "in_m3": float(f[ix["flo_in"]]), "out_m3": float(f[ix["flo_out"]])})
            except (ValueError, IndexError) as error:
                summary["reservoir_malformed"] = "%s: %s" % (res.name, error)
                by_object = {}
            # 官方连接对象号映射预期水库；缺整库明确列出
            present = {key for key in by_object if key in expected_reservoirs}
            missing_whole = ["%s(unit %d)" % (n, u) for (u, n) in sorted(expected_reservoirs - present)]
            expected_keys = {(y, m) for y in range(settings["start_year"] + settings["warmup_years"],
                                                   settings["end_year"] + 1)
                             for m in range(1, 13)}
            reservoirs = {}
            for (unit, name), rows_ in sorted(by_object.items()):
                if (unit, name) not in expected_reservoirs:
                    continue
                rows_.sort(key=lambda r: (r["yr"], r["mon"]))
                unique_months = {(r["yr"], r["mon"]) for r in rows_}
                duplicates = len(rows_) - len(unique_months)
                missing_months = sorted(expected_keys - unique_months)
                outside = sorted(unique_months - expected_keys)
                entry = {
                    "months": len(unique_months),
                    "first_month": "%04d-%02d" % (min(unique_months)[0], min(unique_months)[1]),
                    "last_month": "%04d-%02d" % (max(unique_months)[0], max(unique_months)[1]),
                    "unit_note": "通量为月合计(m3/月)；stor为月末时点值；多库按对象分别统计，不合并"}
                reasons = []
                if missing_months:
                    reasons.append("missing %d (year,month), e.g. %s" % (len(missing_months), missing_months[:3]))
                if duplicates:
                    reasons.append("%d duplicate rows" % duplicates)
                if outside:
                    reasons.append("outside period %s" % outside[:3])
                if malformed:
                    reasons.append("%d malformed rows in file" % malformed)
                if reasons:
                    entry["incomplete_reason"] = "; ".join(reasons)
                    entry["total_in_m3"] = None
                    entry["total_out_m3"] = None
                    entry["stor_first_month_end_m3"] = None
                    entry["stor_last_month_end_m3"] = None
                else:
                    entry["stor_first_month_end_m3"] = round(rows_[0]["stor_m3"], 0)
                    entry["stor_last_month_end_m3"] = round(rows_[-1]["stor_m3"], 0)
                    entry["total_in_m3"] = round(sum(r["in_m3"] for r in rows_), 0)
                    entry["total_out_m3"] = round(sum(r["out_m3"] for r in rows_), 0)
                reservoirs["%s(unit %d)" % (name, unit)] = entry
            if missing_whole:
                summary["reservoir_missing_objects"] = missing_whole
        else:
            missing_whole = ["%s(unit %d)" % (n, u) for (u, n) in sorted(expected_reservoirs)]
            summary["reservoir_missing_objects"] = missing_whole
    if reservoirs:
        summary["reservoir_monthly_by_object"] = reservoirs
    elif expected_reservoirs:
        summary["reservoir_monthly_by_object"] = None
        summary["reservoir_missing_output"] = "no records" if res.is_file() else str(res.name)
    else:
        # 无水库项目：正常标为不适用，不因缺水库输出误报失败
        summary["reservoir_monthly_by_object"] = "not_applicable"
    summary["estimate_note"] = ("田间灌溉为效率后原生输出(hru_wb irr)面积加权；毛取水如需给出须按成功"
                                "事件推算并标注estimated，本汇总不默认提供；本表为情景间差异描述，"
                                "单因素因果结论须由同规则基线对照支持")
    # 统一最终完整性状态：缺输出/不完整原因/缺整库任一出现即 summary_incomplete。
    # 无水库项目 reservoir_monthly_by_object='not_applicable'（字符串标记），
    # 按空映射处理——不适用不是缺失，不应误报也不得触发字典遍历异常。
    reservoir_stats = summary.get("reservoir_monthly_by_object")
    if not isinstance(reservoir_stats, dict):
        reservoir_stats = {}
    incomplete = ("field_irrigation_incomplete_reason" in summary
                  or "field_irrigation_missing_output" in summary
                  or "reservoir_missing_output" in summary
                  or "reservoir_missing_objects" in summary
                  or "reservoir_malformed" in summary
                  or any(item.get("incomplete_reason")
                         for item in reservoir_stats.values()
                         if isinstance(item, dict)))
    summary["status"] = "summary_incomplete" if incomplete else "completed"
    return summary


def write_report(results, path, not_executed=None):
    """Report bound by scenario key; failures and not-executed items preserved."""
    path = Path(path)
    not_executed = not_executed or []
    path.mkdir(parents=True, exist_ok=True)
    by_scenario = {entry["summary"]["scenario"]: entry for entry in results}
    rows = []
    for name, entry in by_scenario.items():
        run, summary = entry["run"], entry["summary"]
        reservoir = summary.get("reservoir_monthly_by_object")
        if not isinstance(reservoir, dict):
            reservoir = {}  # 'not_applicable' 标记按空映射处理，显示身份保留在summary
        first = next(iter(reservoir.values()), {}) if reservoir else {}
        field = summary.get("field_irrigation_m3_efficiency_after")
        status = final_status(entry)  # 统一使用既有最终状态判定
        # 同一不完整原因进入CSV/Markdown，不只放深层JSON
        reasons = []
        # 同一不完整原因进入CSV/Markdown，不只放深层JSON
        reasons = []
        if summary.get("field_irrigation_missing_output"):
            reasons.append(f"field_irrigation missing {summary['field_irrigation_missing_output']}")
        if summary.get("field_irrigation_incomplete_reason"):
            reasons.append(f"field_irrigation incomplete: {summary['field_irrigation_incomplete_reason']}")
        if summary.get("reservoir_missing_output"):
            reasons.append(f"reservoir missing {summary['reservoir_missing_output']}")
        if summary.get("reservoir_missing_objects"):
            reasons.append(f"reservoir whole objects missing: {summary['reservoir_missing_objects']}")
        for obj_name, item in reservoir.items():
            if item.get("incomplete_reason"):
                reasons.append(f"reservoir {obj_name} incomplete: {item['incomplete_reason']}")
        rows.append({"scenario": name, "status": status,
                     "comparison_passed": summary.get("comparison_passed"),
                     "kind": summary.get("kind"), "mode": summary.get("mode"),
                     "enabled_rules": ";".join(summary.get("enabled_rules") or [])
                                      or summary.get("release_rule") or None,
                     "field_irrigation_m3": field,
                     "field_irrigation_missing": summary.get("field_irrigation_missing_output"),
                     "integrity_reasons": "; ".join(reasons) or None,
                     "reservoir_out_m3_first_object": first.get("total_out_m3"),
                     "export_dir": (run or {}).get("export_dir"),
                     "error": entry.get("error")})
    if rows:
        with (path / "scenario_summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (path / "scenario_summary.json").write_text(
        json.dumps({"results": results, "not_executed": not_executed}, ensure_ascii=False, indent=2,
                   allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# 第4步A：管理情景批量运行汇总", "",
             "| 情景 | 状态 | 对照 | 模板 | 实际启用规则 | 田间灌溉m³ | 水库出流m³(首对象) | 错误 |",
             "|---|---|---|---|---|---:|---:|---|"]
    for row in rows:
        field = row["field_irrigation_m3"]
        lines.append(f"| {row['scenario']} | {row['status']} | {row['comparison_passed']} "
                     f"| {row['kind']} | {row['enabled_rules'] or '—'} "
                     f"| {'缺失' if field is None else field} "
                     f"| {row['reservoir_out_m3_first_object'] or '—'} | "
                     f"{row['error'] or ''} {row.get('integrity_reasons') or ''} |")
    for item in not_executed:
        reason = "用户取消" if item["reason"] == "user_cancel" else "前序失败后未执行"
        lines.append(f"| {item['scenario']} | {reason} | — | — | — | — | — | |")
    lines += ["", "口径：田间灌溉为效率后面积加权（缺输出标缺失，不以0冒充）；水库按对象按月统计，"
              "通量为月合计、蓄水为月末时点值；多库不合并。情景差异不构成单因素因果结论、"
              "精度验收或最优方案推荐；参数身份见manifest。"]
    (path / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"csv": str(path / "scenario_summary.csv"), "json": str(path / "scenario_summary.json"),
            "report": str(path / "report.md")}


def run_scenarios(table_path, work_dir, on_failure="stop", cancel_event=None,
                  progress=None, work_root=None, output_strategy="monthly_summary",
                  start_year=None, end_year=None, warmup_years=None):
    if on_failure not in ("stop", "continue"):
        raise ValueError("on_failure 须为 stop 或 continue")
    work_dir = Path(work_dir)
    if work_dir.exists():
        raise FileExistsError(f"批量目录已存在，保留不动：{work_dir}")
    work_dir.mkdir(parents=True)
    rows = load_scenario_table(table_path)
    problems = validate_scenario_table(rows, work_root)
    if problems:
        raise ValueError("情景表校验未通过：\n" + "\n".join(problems))
    plan = plan_scenarios(table_path, work_dir)
    results, failures, not_executed = [], [], []
    done = set()
    for row in rows:
        name = row["scenario"]
        if cancel_event is not None and cancel_event.is_set():
            not_executed.append({"scenario": name, "reason": "user_cancel"})
            continue
        entry = {"run": None, "summary": None, "error": None}
        try:
            manifest = apply_scenario(row["base_project"], name, row, work_dir / name, work_root)
            run = run_scenario(manifest, work_dir / name / "TxtInOut",
                               cancel_event=cancel_event, progress=progress,
                               output_strategy=output_strategy,
                               start_year=start_year, end_year=end_year,
                               warmup=warmup_years)
            entry["run"] = run
            if run["status"] == "completed":
                try:
                    entry["summary"] = summarize_run(run)
                except Exception as error:
                    # 汇总失败不计成功：最终状态 summary_failed
                    entry["error"] = f"summarize failed: {error}"
                    entry["summary"] = {"scenario": name, "status": "summary_failed",
                                        "comparison_passed": run.get("comparison_passed")}
                    failures.append({"scenario": name, "error": entry["error"],
                                     "type": "SummaryFailure"})
                if entry["summary"].get("status") == "summary_incomplete":
                    # 完整性不完整同样不计成功：stop/continue与运行失败一致
                    entry["error"] = "summary incomplete: " + str(
                        entry["summary"].get("field_irrigation_incomplete_reason")
                        or entry["summary"].get("reservoir_missing_objects")
                        or entry["summary"].get("field_irrigation_missing_output")
                        or entry["summary"].get("reservoir_missing_output") or "")
                    failures.append({"scenario": name, "error": entry["error"],
                                     "type": "IncompleteSummary"})
            else:
                entry["error"] = f"run status={run['status']}"
                failures.append({"scenario": name, "stage": run["status"],
                                 "error": entry["error"], "type": "RunFailure"})
        except Exception as error:
            entry["error"] = str(error)
            failures.append({"scenario": name, "error": str(error), "type": type(error).__name__})
        if entry["run"] is not None or entry["error"] is not None:
            entry["summary"] = entry["summary"] or {
                "scenario": name,
                "status": (entry["run"] or {}).get("status", "failed"),
                "comparison_passed": (entry["run"] or {}).get("comparison_passed")}
            results.append(entry)
            done.add(name)
        if failures and on_failure == "stop":
            for remaining in rows:
                if remaining["scenario"] not in done:
                    not_executed.append({"scenario": remaining["scenario"],
                                         "reason": "skipped_after_failure"})
            break
    report = write_report(results, work_dir, not_executed=not_executed)         if (results or not_executed) else None
    return {"rows": len(rows), "plan": plan, "results": results, "failures": failures,
            "not_executed": not_executed,
            "cancelled": [item["scenario"] for item in not_executed],
            "report": report, "work_dir": str(work_dir)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("templates", help="列出模板类型与字段/单位")
    p = sub.add_parser("validate", help="校验情景表（不触碰任何项目）")
    p.add_argument("table", type=Path)
    p = sub.add_parser("plan", help="运行前计划（情景数/调用次数/输出策略）")
    p.add_argument("table", type=Path)
    p.add_argument("--work-dir", type=Path, required=True)
    p = sub.add_parser("export-table", help="导出情景表（JSON/CSV round-trip）")
    p.add_argument("table", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("run", help="顺序批量：独立副本→官方导出→隔离运行+官方对照→汇总")
    p.add_argument("table", type=Path)
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--on-failure", choices=("stop", "continue"), default="stop")
    args = parser.parse_args()
    if args.action == "templates":
        print(json.dumps(list_templates(), ensure_ascii=False, indent=2))
    elif args.action == "validate":
        problems = validate_scenario_table(load_scenario_table(args.table))
        print(json.dumps({"problems": problems, "ok": not problems}, ensure_ascii=False, indent=2))
        return 1 if problems else 0
    elif args.action == "plan":
        print(json.dumps(plan_scenarios(args.table, args.work_dir), ensure_ascii=False, indent=2))
    elif args.action == "export-table":
        print(export_scenario_table(load_scenario_table(args.table), args.output))
    elif args.action == "run":
        result = run_scenarios(args.table, args.work_dir, args.on_failure)
        print(json.dumps({key: result[key] for key in
                          ("rows", "failures", "not_executed", "cancelled", "report")},
                         ensure_ascii=False, indent=2))
        return exit_code_for(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
