"""Two-engine, same-input/default-parameter comparison; no parameter search."""
import copy
import csv
import json
from pathlib import Path
from workflow_runtime import resolve_runtime_config, prepare_job_engine, engine_markdown
from swatplus_trial import run_trial
from swatplus_workflow import identify_outlet
from multi_observation import read_target_csv
from swatplus_profiles import read_output_header


def compare_engines(config, root, output, input_dir=None):
    root, output = Path(root), Path(output)
    if not config.get("runtime_config"):
        raise ValueError("同参数对照需要runtime_config来指定官方参考引擎")
    if config.get("engine", {}).get("mode") != "custom":
        raise ValueError("请先选择自编译引擎并保存配置")
    cfg = copy.deepcopy(config)
    if input_dir:
        cfg.setdefault("model", {})["input_dir"] = str(Path(input_dir).resolve())
    source = cfg.get("model", {}).get("input_dir")
    if not source:
        raise ValueError("请先指定已有的干净TxtInOut；本命令不会自动建模")
    source = (root / source).resolve()
    selected = resolve_runtime_config(cfg, root)
    default = copy.deepcopy(cfg); default["engine"] = {"mode": "default"}
    default = resolve_runtime_config(default, root)
    if default["model"]["expected_version"] != selected["model"]["expected_version"]:
        raise ValueError("对照的两版程序须使用相同输入输出适配版本")
    outlet = identify_outlet(source, cfg["model"].get("outlet", "auto"))
    if outlet.get("object", "channel_sd") != "channel_sd":
        raise ValueError("引擎对照当前基于河道出口流量；该模型末端不是唯一河道，请先显式配置河道评价对象")
    output.mkdir(parents=True, exist_ok=False)
    results = {}
    for key, engine_cfg in [("official", default), ("custom", selected)]:
        folder = output / key; folder.mkdir()
        engine_cfg = prepare_job_engine(engine_cfg, folder)
        (folder / "config.resolved.json").write_text(json.dumps(engine_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        sim = cfg["simulation"]
        targets = [{"id": freq, "object": "channel_sd", "frequency": freq,
                    "variable": "flo_out", "unit": "m3/s", "unit_id": outlet["unit"],
                    "gis_id": outlet["gis_id"], "name": outlet["name"]} for freq in ("daily", "monthly")]
        spec = {"root": str(root), "input_dir": str(source), "executable": engine_cfg["model"]["executable"],
                "expected_version": engine_cfg["model"]["expected_version"], "work_dir": str(folder / "trial"),
                "start_year": sim["start_year"], "end_year": sim["end_year"], "warmup_years": sim["warmup_years"],
                "outlet": outlet, "frequency": "daily", "parameters": [], "output_targets": targets,
                "print_policy": cfg["model"].get("print_policy", "preserve"),
                "calibration_policy": cfg["model"].get("calibration_policy", "reject"),
                "timeout_seconds": engine_cfg["model"].get("timeout_seconds", 600),
                "engine_info": engine_cfg["engine_info"]}
        print(f"同参数对照：{key} -> {spec['executable']}", flush=True)
        results[key] = run_trial(spec)
    differences = {}
    for freq in ("daily", "monthly"):
        a = read_target_csv(results["official"]["target_csvs"][freq]["path"])
        b = read_target_csv(results["custom"]["target_csvs"][freq]["path"])
        if set(a) != set(b):
            raise ValueError("两版流量日期不一致")
        rows = [{"date": d, "official_m3_s": a[d], "custom_m3_s": b[d], "difference_m3_s": b[d]-a[d]} for d in sorted(a)]
        with (output / (freq + "_comparison.csv")).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        differences[freq] = {"records": len(rows), "maximum_absolute_difference_m3_s": max(abs(r["difference_m3_s"]) for r in rows)}
    variables = ["precip", "et", "surq_gen", "latq", "perc", "wateryld", "pet", "irr"]
    basin = {}
    for key, result in results.items():
        with Path(result["basin_outputs"]["monthly"]["path"]).open(encoding="utf-8-sig") as f:
            _, header, _ = read_output_header(f, selected["model"]["expected_version"], "basin_wb")
            basin[key] = [dict(zip(header, line.split())) for line in f if line.strip()]
    if [(r["yr"], r["mon"]) for r in basin["official"]] != [(r["yr"], r["mon"]) for r in basin["custom"]]:
        raise ValueError("两版流域水量日期不一致")
    water = {v: max(abs(float(a[v])-float(b[v])) for a,b in zip(basin["official"],basin["custom"])) for v in variables}
    report = {"input_dir": str(source), "parameters": "same original model parameters; no search",
              "simulation": cfg["simulation"], "engines": {k: r["engine_info"] for k,r in results.items()},
              "flow": differences, "basin_monthly_maximum_absolute_difference_mm": water,
              "exact_match": all(x["maximum_absolute_difference_m3_s"] == 0 for x in differences.values()) and all(x == 0 for x in water.values())}
    (output / "comparison.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    lines = ["# 官方与自编译引擎同参数对照", "", "使用同一干净输入和原参数，连续计算同一完整时段；未进行参数搜索或率定。", "",
             "| 输出 | 记录数 | 最大绝对差 |", "|---|---:|---:|"]
    lines += [f"| {k}流量 | {v['records']} | {v['maximum_absolute_difference_m3_s']:.9g} m³/s |" for k,v in differences.items()]
    lines += ["", "| 流域月水量变量 | 最大绝对差 mm |", "|---|---:|"] + [f"| {k} | {v:.9g} |" for k,v in water.items()]
    for key, info in report["engines"].items():
        lines += ["", "### " + key] + engine_markdown(info)
    lines += ["", "本检查验证程序接入与数值对照，不代表真实流域精度、机制改进或率定收敛。"]
    (output / "report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    return report
