"""Direct-official vs workbench-run comparison for a re-attached SWAT+ export.

Runs the SAME engine twice on the SAME exported TxtInOut for the SAME period:
(a) "direct": the engine executed in a plain copy whose time.sim period (and
print.prt warmup-skip row) are set to the requested values, with every other
file exactly as the official Editor exported it;
(b) "workbench": scripts/swatplus_trial.py, i.e. the full snapshot copy, period
configuration, preserve print policy and output extraction pipeline.

The two runs must agree value-for-value on the outlet channel and monthly
basin water balance; the report also lists which non-target outputs survived
the workbench run, proving the preserve policy keeps the source's settings.
This is an interface equivalence check, not a hydrological evaluation.

python -X utf8 scripts/reconnect_check.py --input-dir <TxtInOut> --name <run-name>
[--engine <exe>] [--start-year 1990] [--end-year 1992] [--warmup 1]
"""
import argparse
import datetime as dt
import filecmp
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from swatplus_trial import read_lines, write_lines, run_trial
from swatplus_workflow import identify_outlet, prepare_input_snapshot


def default_engine():
    runtime = json.loads((ROOT / "config/qgis344_runtime.json").read_text(encoding="utf-8-sig"))
    return Path(runtime["model_executable"])


def required_output_files(frequency, outlet_object="channel_sd"):
    """Outputs both sides must produce for the comparison to be meaningful.

    The workbench trial guarantees channel_sd at the trial frequency plus
    basin_wb monthly/yearly (its own basin checks read them), and the direct
    official copy runs the same export, so these files must exist on both
    sides. With a reservoir evaluation outlet, the reservoir output at the
    trial frequency is required as well. Anything else the source enabled is
    compared only when present on both sides and never masks a missing
    required file.
    """
    files = [f"channel_sd_{'day' if frequency == 'daily' else 'mon'}.txt",
             "basin_wb_mon.txt", "basin_wb_yr.txt"]
    if outlet_object == "reservoir":
        files.append(f"reservoir_{'day' if frequency == 'daily' else 'mon'}.txt")
    return files


def compare_outputs(direct_model, workbench_model, frequency, extra_files=(),
                    outlet_object="channel_sd", omitted_required=()):
    """Compare two run directories; report both run-level facts and a verdict.

    comparison_passed is True only when every required output exists on both
    sides, at least one comparison was actually performed, and every performed
    required comparison is byte-identical. Differences or absences among
    optional extras are reported but do not fail the verdict on their own.
    omitted_required names required outputs intentionally disabled by a
    declared output strategy on BOTH sides (e.g. yearly under a monthly-only
    strategy); they are excluded from the requirement, never silently.
    """
    required = [f for f in required_output_files(frequency, outlet_object)
                if f not in set(omitted_required)]
    comparisons = {}
    for filename in sorted(set(required) | set(extra_files)):
        left, right = Path(direct_model) / filename, Path(workbench_model) / filename
        entry = {"required": filename in required, "direct": left.is_file(), "workbench": right.is_file()}
        if entry["direct"] and entry["workbench"]:
            entry["identical_bytes"] = filecmp.cmp(left, right, shallow=False)
        comparisons[filename] = entry
    missing_required = sorted(name for name, entry in comparisons.items()
                              if entry["required"] and not (entry["direct"] and entry["workbench"]))
    performed = [entry for entry in comparisons.values() if entry["direct"] and entry["workbench"]]
    required_performed = [entry for entry in performed if entry["required"]]
    comparison_passed = (not missing_required
                         and required_performed
                         and all(entry["identical_bytes"] for entry in required_performed))
    return {"comparisons": comparisons, "missing_required": missing_required,
            "compared_count": len(performed), "required_compared_count": len(required_performed),
            "comparison_passed": comparison_passed}


def source_print_switches(model):
    """Record the objects/section switches the source export arrived with."""
    rows = read_lines(model / "print.prt")
    result = {}
    section = None
    for index, row in enumerate(rows):
        fields = row.split()
        if not fields:
            continue
        if fields[0] in ("csvout", "crop_yld"):
            section = fields[0]
            result[section] = rows[index + 1].split()
        elif fields[0] == "objects":
            section = "objects"
        elif section == "objects" and len(fields) == 5:
            result[fields[0]] = fields[1:]
    return result


def monthly_enabled_outputs(model):
    return sorted(name for name, flags in source_print_switches(model).items()
                  if isinstance(flags, list) and len(flags) == 4 and flags[1] == "y"
                  and name not in ("csvout", "crop_yld"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--engine", type=Path, default=None)
    parser.add_argument("--start-year", type=int, default=1990)
    parser.add_argument("--end-year", type=int, default=1992)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--frequency", choices=["daily", "monthly"], default="daily")
    args = parser.parse_args()
    source = args.input_dir.resolve()
    engine = (args.engine or default_engine()).resolve()
    if not engine.is_file():
        raise FileNotFoundError(engine)
    output = (ROOT / "runs" / args.name).resolve()
    if ROOT / "runs" not in output.parents:
        raise ValueError("输出目录必须位于本工作区 runs/ 内")
    output.mkdir(parents=True, exist_ok=False)

    # (a) direct official run: only the period rows are set, nothing else touched.
    outlet = identify_outlet(source, "auto")
    direct = output / "direct_run"
    prepare_input_snapshot(source, direct / "TxtInOut")
    direct_model = direct / "TxtInOut"
    rows = read_lines(direct_model / "time.sim")
    rows[2] = f"0 {args.start_year} 0 {args.end_year} 0"
    write_lines(direct_model / "time.sim", rows)
    rows = read_lines(direct_model / "print.prt")
    rows[2] = f"{args.warmup} 0 0 0 0 1"
    # A reservoir evaluation outlet needs the reservoir output row on in the
    # direct copy too; this is a print switch, not a simulation change.
    if outlet.get("object", "channel_sd") == "reservoir":
        index = ("daily", "monthly", "yearly", "avann").index(args.frequency)
        for position, row in enumerate(rows):
            fields = row.split()
            if fields and fields[0] == "reservoir" and len(fields) == 5:
                flags = fields[1:]
                flags[index] = "y"
                rows[position] = f"{'reservoir':24s} {' '.join(flags)}"
                break
        else:
            raise ValueError("print.prt lacks a reservoir row for the reservoir outlet comparison")
    write_lines(direct_model / "print.prt", rows)
    with (direct / "console.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run([str(engine)], cwd=direct_model, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, timeout=1800, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    direct_exit = completed.returncode
    direct_success = direct_exit == 0 and (direct_model / "success.fin").is_file()

    # (b) workbench trial through the normal isolated runner.
    spec = {"root": str(ROOT), "input_dir": str(source), "executable": str(engine),
            "expected_version": "62.0.1", "work_dir": str(output / "workbench_trial"),
            "start_year": args.start_year, "end_year": args.end_year,
            "warmup_years": args.warmup, "frequency": args.frequency, "outlet": outlet,
            "parameters": [], "print_policy": "preserve", "timeout_seconds": 1800,
            "engine_info": {"mode": "default", "source": "reconnect_check direct comparison"}}
    workbench = run_trial(spec, ROOT)
    workbench_model = Path(workbench["model_dir"])

    # Compare required outputs strictly; optional extras when both sides have them.
    outlet_object = outlet.get("object", "channel_sd")
    extra = [f"{name}_mon.txt" for name in monthly_enabled_outputs(direct_model)
             if f"{name}_mon.txt" not in required_output_files(args.frequency, outlet_object)]
    comparison = compare_outputs(direct_model, workbench_model, args.frequency,
                                 extra_files=extra, outlet_object=outlet_object)
    comparisons = comparison["comparisons"]

    # Which source-enabled non-target outputs survived the workbench run.
    period_suffix = {"daily": "day", "monthly": "mon", "yearly": "yr"}
    retention = {}
    for name, flags in source_print_switches(source).items():
        if name in ("csvout", "crop_yld") or len(flags) != 4:
            continue
        for period, index in (("daily", 0), ("monthly", 1), ("yearly", 2)):
            if flags[index] == "y":
                key = f"{name}_{period_suffix[period]}.txt"
                retention[key] = (workbench_model / key).is_file()

    runs_ok = direct_success and workbench["status"] == "completed"
    result = {"status": "completed" if runs_ok else "failed",
              "purpose": "同一引擎/输入/时段的官方直接运行与工作台运行对照；接口一致性检查，非水文评价",
              "input_dir": str(source), "engine": str(engine),
              "period": {"start_year": args.start_year, "end_year": args.end_year, "warmup_years": args.warmup},
              "outlet": outlet, "frequency": args.frequency,
              "direct": {"exit_code": direct_exit, "success_fin": direct_success,
                         "dir": str(direct_model)},
              "workbench": {"status": workbench["status"], "exit_code": workbench["exit_code"],
                            "outlet_csv": workbench["outlet_csv"], "records": workbench["records"],
                            "units": workbench["units"],
                            "input_dependencies": workbench["input_layout"].get("dependencies"),
                            "print_policy": "preserve",
                            "elapsed_seconds": workbench["elapsed_seconds"]},
              "output_comparison": comparisons,
              "comparison_verdict": {key: comparison[key] for key in
                                     ("missing_required", "compared_count", "required_compared_count",
                                      "comparison_passed")},
              "required_outputs": required_output_files(args.frequency, outlet_object),
              "preserve_retention": retention,
              "all_source_enabled_outputs_retained": all(retention.values())}
    result["comparison_passed"] = runs_ok and comparison["comparison_passed"]
    (output / "comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    lines = ["# 完整模型接回：官方直接运行 vs 工作台运行对照", "",
             f"- 输入导出：{source}", f"- 引擎：{engine}（官方62.0.1 Intel）",
             f"- 时段：{args.start_year}–{args.end_year}，预热{args.warmup}年，{args.frequency}频率",
             f"- 出口：{outlet}", "",
             "两次运行均使用同一引擎与同一输入；直接运行仅设置time.sim时段与print.prt预热行"
             + ("，并按工作台同样方式开启reservoir输出行（仅打印开关，不影响模拟）" if outlet.get("object") == "reservoir" else "")
             + "，其余文件保持Editor导出原样。",
             "工作台运行走完整试跑管道（快照复制、时段配置、保留输出策略、结果提取）。", "",
             f"必比输出：{', '.join(required_output_files(args.frequency, outlet_object))}（缺失或内容不同即判对照失败）", "",
             "| 输出文件 | 必比 | 直接运行 | 工作台运行 | 逐字节一致 |", "|---|---|---|---|---|"]
    for filename, item in comparisons.items():
        compared = item.get("direct") and item.get("workbench")
        lines.append(f"| {filename} | {'是' if item['required'] else '附加'} | {'有' if item.get('direct') else '无'} |"
                     f" {'有' if item.get('workbench') else '无'} |"
                     f" {'—' if not compared else str(item.get('identical_bytes'))} |")
    lines += ["", "来源模型已启用输出的保留情况（工作台运行后文件存在）：", ""]
    for key, kept in sorted(retention.items()):
        lines.append(f"- {key}: {'保留' if kept else '缺失'}")
    lines += ["",
              f"两次运行均完成：{runs_ok}",
              f"对照验收通过（必比输出齐全且逐字节一致）：{comparison['comparison_passed']}",
              f"来源启用输出全部保留：{result['all_source_enabled_outputs_retained']}",
              "", "本对照只验证运行链一致性，不代表水文精度；未涉及水库/灌溉机制的实跑验证。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"],
                      "runs_completed": runs_ok,
                      "comparison_passed": result["comparison_passed"],
                      "missing_required": comparison["missing_required"],
                      "all_source_enabled_outputs_retained": result["all_source_enabled_outputs_retained"]},
                     ensure_ascii=False))
    # Run completion and comparison acceptance are separate verdicts: a model
    # can finish on both sides while the outputs differ, which must not exit 0.
    return 0 if result["comparison_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
