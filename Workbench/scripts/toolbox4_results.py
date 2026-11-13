"""Read real Toolbox4 scenario artifacts; replay fixed values on independent clean inputs."""
import csv
import calendar
import datetime as dt
import json
import math
from pathlib import Path
import shutil
from multi_observation import read_observation, evaluate_one, normalized_unit

ROOT = Path(__file__).resolve().parents[1]


def read_json(path): return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, data):
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def model_settings(folder):
    folder=Path(folder); result={}
    for filename,key in [("time.sim","time"),("codes.bsn","codes")]:
        p=folder/filename
        if p.is_file():
            lines=p.read_text(encoding="utf-8-sig").splitlines()
            if len(lines)<3 or len(lines[1].split())!=len(lines[2].split()): raise ValueError(f"{filename}: 无法解析")
            result[key]=dict(zip(lines[1].split(),lines[2].split()))
    lines=(folder/"print.prt").read_text(encoding="utf-8-sig").splitlines()
    result["print_period"]=dict(zip(lines[1].split(),lines[2].split()))
    result["outputs"]={}
    in_objects=False
    for line in lines:
        fields=line.split()
        if fields and fields[0]=="objects": in_objects=True;continue
        if in_objects and len(fields)==5: result["outputs"][fields[0]]=fields[1:]
    return result


def parse_calibration(path, model, version="62.0.1"):
    """Only replay reviewed native parameters, explicit IDs, without conditions/time selectors."""
    from native_calibration import parameter_catalog, validate_definitions
    catalog={p["native_name"]:p for p in parameter_catalog(version)["parameters"]}
    lines=Path(path).read_text(encoding="utf-8-sig").splitlines()
    n=int(lines[1].strip()); rows=[line.split() for line in lines[3:] if line.strip()]
    if n<=0 or len(rows)!=n: raise ValueError("calibration.cal记录数不符或为空；条件/跨行范围当前不自动重放")
    definitions=[]
    for row in rows:
        if len(row)<11: raise ValueError("calibration.cal记录不完整")
        name,method=row[:2]; value=float(row[2]); selectors=[int(x) for x in row[3:11]]
        cond,lyr1,lyr2,yr1,yr2,day1,day2,total=selectors
        if cond or any((yr1,yr2,day1,day2)): raise ValueError("条件和时间选择器尚不支持自动固定复算")
        if name not in catalog: raise ValueError(f"{name}: 尚未审查的参数可回收原文件，但不自动复算")
        ids=[int(x) for x in row[11:]]
        if total!=len(ids) or any(x<=0 for x in ids): raise ValueError("暂不支持压缩对象范围或跨行对象列表")
        d={"name":name,"native_name":name,"object_type":catalog[name]["object_type"],"method":method,"value":value,"layer":[lyr1,lyr2]}
        if total:d["object_ids"]=ids
        definitions.append(d)
    validate_definitions(definitions,Path(model),expected_version=version)
    return definitions


def read_series(folder, definition, version="62.0.1"):
    from swatplus_profiles import version_ok
    target=definition["target"]; freq=definition["frequency"]
    if freq not in ("daily","monthly"): raise ValueError("只支持日/月输出")
    path=Path(folder)/f"{target['object']}_{'day' if freq=='daily' else 'mon'}.csv"
    values={}
    with path.open(encoding="utf-8-sig",newline="") as stream:
        banner=stream.readline()
        if not version_ok(banner,version): raise ValueError("输出引擎版本与工作台任务不一致")
        reader=csv.reader(stream); header=[x.strip() for x in next(reader)]; units=[x.strip() for x in next(reader)]
        ix={key:header.index(key) for key in ["yr","mon","day","unit","gis_id","name",target["variable"]]}
        if normalized_unit(units[ix[target["variable"]]])!=normalized_unit(definition["unit"]): raise ValueError("输出与参考单位不同，未进行隐式换算")
        for row in reader:
            if not row or not any(x.strip() for x in row):continue
            if len(row)!=len(header):raise ValueError(f"{path.name}: 列数不符")
            if int(row[ix["unit"]])!=target["unit_id"]:continue
            if target.get("gis_id") is not None and int(row[ix["gis_id"]])!=target["gis_id"]:raise ValueError("站点GIS编号不符")
            if target.get("name") and row[ix["name"]].strip()!=target["name"]:raise ValueError("站点名称不符")
            date=dt.date(int(row[ix["yr"]]),int(row[ix["mon"]]),int(row[ix["day"]]) if freq=="daily" else 1).isoformat()
            if date in values: raise ValueError("输出日期重复")
            value=float(row[ix[target["variable"]]])
            if not math.isfinite(value):raise ValueError("输出包含非有限值")
            values[date]=value
    if not values:raise ValueError("没有匹配到目标输出；核对站点编号")
    return values,path


def next_output(job, stem):
    folder=Path(job)/"integration";folder.mkdir(exist_ok=True)
    i=1
    while (folder/f"{stem}_{i}").exists():i+=1
    return folder/f"{stem}_{i}"


def collect_results(job, scenario, actual_engine=None):
    job=Path(job).resolve()
    from input_paths import job_root
    root=job_root(job,ROOT)
    cfg=read_json(job/"config.resolved.json");state=read_json(job/"state.json")
    if cfg["model"]["expected_version"]!="62.0.1": raise ValueError("回收器当前适配62.0.1")
    scenario=Path(scenario).resolve()
    for filename in ("success.fin","file.cio","calibration.cal","time.sim","print.prt","codes.bsn"):
        if not (scenario/filename).is_file():raise FileNotFoundError(f"请选择完成运行的Calibration目录，缺少 {filename}")
    cio=[l.split() for l in (scenario/"file.cio").read_text(encoding="utf-8-sig").splitlines() if l.split()]
    change=[r for r in cio if r[0]=="chg"]
    if len(change)!=1 or len(change[0])<3 or change[0][2]!="calibration.cal":raise ValueError("最终输出目录未激活calibration.cal，不能将旁置参数文件当作实际执行参数")
    source=root/state["input_snapshot"]
    differences=[]
    for f in source.iterdir():
        if not f.is_file() or f.name in ("file.cio","time.sim","print.prt","codes.bsn","calibration.cal"):continue
        other=scenario/f.name
        if not other.is_file() or f.read_bytes()!=other.read_bytes():differences.append(f.name)
    settings=model_settings(scenario)
    definitions=cfg.get("observations") or ([cfg["observation"]] if cfg.get("observation") else [])
    period=cfg.get("periods",{}).get("calibration")
    if not definitions or not period:raise ValueError("回收评价前请在任务配置中提供参考资料和率定期；建模本身不需要参考资料")
    metrics={}; series_files=[];series={}
    for definition in definitions:
        values,path=read_series(scenario,definition)
        reference=read_observation(definition,root)
        metrics[definition["id"]]=evaluate_one({**definition,"metric":"NSE"},reference,values,period)
        series[definition["id"]]=values;series_files.append(path)
    replay_error=None
    try: parameters=parse_calibration(scenario/"calibration.cal",source)
    except (ValueError,KeyError,IndexError) as error: parameters=[];replay_error=str(error)
    output=next_output(job,"toolbox4_result");output.mkdir()
    for filename in ("calibration.cal","time.sim","print.prt","codes.bsn","file.cio","success.fin"):
        shutil.copy2(scenario/filename,output/filename)
    for path in set(series_files):shutil.copy2(path,output/path.name)
    history=None
    for parent in [scenario,*scenario.parents]:
        path=parent/"spt.data/calibration_history.json"
        if path.is_file():
            history=read_json(path);shutil.copy2(path,output/"calibration_history.json");break
    result={"status":"collected", "source_scenario":str(scenario),"collected_at":dt.datetime.now().isoformat(timespec="seconds"),
            "output":str(output),"source_run":str(job),"metrics":metrics,"settings":settings,"parameters":parameters,
            "model_input_differences":differences,"replay_blocked":replay_error or ("模型输入与本任务不同" if differences else None),
            "engine":{"expected_version":"62.0.1","actual_executable":str(Path(actual_engine).resolve()) if actual_engine else None,
                      "confirmation":"user_selected_for_replay" if actual_engine else "Toolbox实际exe未确认；版本由输出横幅核对"},
            "history_success":history.get("success") if isinstance(history,dict) else None,
            "reported_best_metrics":history.get("best_metrics") if isinstance(history,dict) else None,
            "note":"复核实际CSV与任务参考值，不等于独立时段验证；参数取最终calibration.cal写入值；未自动控制Toolbox"}
    expected=model_settings(source)
    result["codes_differences"]={key:{"workbench":value,"toolbox":settings.get("codes",{}).get(key)} for key,value in expected.get("codes",{}).items() if settings.get("codes",{}).get(key)!=value}
    result["reference_kinds"]={d["id"]:d.get("kind","unverified") for d in definitions}
    save(output/"result.json",result)
    lines=["# Toolbox结果回收与独立评分", "",result["note"],"",f"原结果目录：{scenario}","", "|参考序列|配对数|NSE|KGE|RMSE|偏差%（模拟－参考）|","|---|---:|---:|---:|---:|---:|"]
    for key,m in metrics.items():lines.append(f"|{key}|{m['n']}|{m['NSE']}|{m['KGE']}|{m['RMSE']}|{m['bias_percent']}|")
    lines += ["", "- Toolbox实际exe："+str(result["engine"]["actual_executable"] or "未确认，不能仅由版本号判定为同一二进制"),
              "- 固定复算限制："+str(result["replay_blocked"] or "已解析参数；运行前仍须核对模拟/物理设置与引擎"),
              "- 历史记录的成功标记仅作参考；最终CSV为本次复核依据。",
              "- 物理开关差异："+json.dumps(result["codes_differences"],ensure_ascii=False),
              "- 资料类型："+json.dumps(result["reference_kinds"],ensure_ascii=False),
              "- 指标中的偏差定义为100×(模拟均值－参考均值)/参考均值；其他软件PBIAS可能使用相反符号。"]
    try:
        plot_comparison(output,definitions,{d["id"]:read_observation(d,root) for d in definitions},series,{"calibration":period})
        lines += ["", "过程线与逐值对照表保存在本目录的comparison_*文件。"]
    except ImportError as error:
        lines += ["", "绘图依赖未找到，指标与原始CSV已保留："+str(error)]
    (output/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return result


def validate_fixed(job, collected, executable=None):
    """One continuous run from original start through validation end, no search."""
    from swatplus_trial import run_trial
    from multi_observation import target_specs
    job=Path(job).resolve()
    from input_paths import job_root
    root=job_root(job,ROOT)
    cfg=read_json(job/"config.resolved.json");state=read_json(job/"state.json")
    collected=Path(collected).resolve();collected.relative_to(job/"integration")
    record=read_json(collected/"result.json")
    if record.get("source_run")!=str(job):raise ValueError("请选择本任务回收的Toolbox结果")
    if record.get("replay_blocked"):raise ValueError(record["replay_blocked"])
    validation=cfg.get("periods",{}).get("validation")
    if not validation:raise ValueError("未配置独立验证期")
    exe=Path(executable or record["engine"].get("actual_executable") or "")
    if not exe.is_file():raise ValueError("请明确选择用于固定复算的SWAT+程序；Toolbox实际exe不能由版本号推断")
    actual=record["settings"]; time=actual["time"];pr=actual["print_period"]
    sim=cfg["simulation"]
    if int(time["yrc_start"])!=sim["start_year"] or int(pr["nyskip"])!=sim["warmup_years"]:raise ValueError("Toolbox起始年/预热与任务不同，请先核对；不能混用不同初始条件")
    if int(time.get("step",0))!=0:raise ValueError("当前固定复算支持日步长，不能自动将亚日模型改为日模型")
    if any(int(time[k]) not in (0,1 if k=="day_start" else 365+calendar.isleap(int(time['yrc_end']))) for k in ("day_start","day_end")):
        raise ValueError("当前固定复算只支持完整日历年模拟")
    definitions=cfg.get("observations") or [cfg["observation"]]
    # Check references before spending a model run.
    references={d["id"]:read_observation(d,root) for d in definitions}
    for d in definitions:
        if sum(validation["start"]<=date<=validation["end"] for date in references[d["id"]])<2:raise ValueError("验证期资料不足两条")
    output=next_output(job,"toolbox4_validation");output.mkdir()
    source=output/"model_inputs"
    shutil.copytree(root/state["input_snapshot"],source)
    shutil.copy2(Path(collected)/"codes.bsn",source/"codes.bsn")
    spec={"root":str(root),"input_dir":str(source),"work_dir":str(output/"run"),
          "executable":str(exe.resolve()),"expected_version":"62.0.1",**sim,"end_year":int(validation["end"][:4]),
          "outlet":state["outlet"],"frequency":cfg["frequency"],"parameters":record["parameters"],
          "parameter_backend":"native_calibration","output_targets":target_specs(definitions),"csv_output":True,
          "print_policy":cfg["model"].get("print_policy","preserve"),
          "calibration_policy":cfg["model"].get("calibration_policy","reject"),
          "timeout_seconds":cfg["model"].get("timeout_seconds",600)}
    result=run_trial(spec);metrics={};agreement={}
    for d in definitions:
        values,_=read_series(result["model_dir"],d)
        original,_=read_series(collected,d)
        metrics[d["id"]]={name:evaluate_one({**d,"metric":"NSE"},references[d["id"]],values,period) for name,period in cfg["periods"].items()}
        dates=[date for date in original if cfg["periods"]["calibration"]["start"]<=date<=cfg["periods"]["calibration"]["end"]]
        if any(date not in values for date in dates):raise ValueError("固定复算缺少率定期输出日期")
        difference=max(abs(values[date]-original[date]) for date in dates)
        agreement[d["id"]]={"n":len(dates),"maximum_absolute_difference":difference,
                            "within_csv_precision":all(math.isclose(values[date],original[date],rel_tol=1e-5,abs_tol=1e-6) for date in dates)}
    verified=all(v["within_csv_precision"] for v in agreement.values())
    final={"status":"completed" if verified else "replay_differs", "fixed_parameters":record["parameters"],"model_run":result,
           "metrics":metrics,"calibration_replay":agreement,"calibration_replay_agrees":verified,
           "selection_basis":"Toolbox最终calibration.cal；验证期不搜索参数", "source_collection":str(collected),
           "note":"复算容差rel=1e-5/abs=1e-6（各变量原单位）；复算不一致时不将验证标为可比，运行成功不代表拟合达标"}
    save(output/"result.json",final)
    lines=["# 固定参数复算与验证", "",final["selection_basis"],"",final["note"],"",f"率定期复算一致：{verified}","", "|序列|时段|配对数|NSE|KGE|RMSE|偏差%|","|---|---|---:|---:|---:|---:|---:|"]
    for key,periods in metrics.items():
        for name,m in periods.items():lines.append(f"|{key}|{name}|{m['n']}|{m['NSE']}|{m['KGE']}|{m['RMSE']}|{m['bias_percent']}|")
    lines += ["", "复算差异："+json.dumps(agreement,ensure_ascii=False),"",f"实际程序：{exe.resolve()}"]
    try:
        plot_comparison(output,definitions,references,{d["id"]:read_series(result["model_dir"],d)[0] for d in definitions},cfg["periods"])
    except ImportError as error:lines += ["", "绘图依赖未找到："+str(error)]
    (output/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return {**final,"output":str(output)}


def plot_comparison(output, definitions, references, simulations, periods):
    """Workbench preview only: plot matched actual values, without smoothing/filling."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    for d in definitions:
        key=d["id"]; simulation=simulations[key]; reference=references[key]
        dates=sorted(date for date in reference if date in simulation and any(p["start"]<=date<=p["end"] for p in periods.values()))
        with (Path(output)/f"comparison_{key}.csv").open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.writer(stream);writer.writerow(["date","reference","simulated"])
            writer.writerows((date,reference[date],simulation[date]) for date in dates)
        x=[dt.date.fromisoformat(date) for date in dates]
        fig,ax=plt.subplots(figsize=(11,4));ax.plot(x,[reference[date] for date in dates],label="Reference",color="#2864ad")
        ax.plot(x,[simulation[date] for date in dates],label="Simulated",color="#e57e34")
        if "validation" in periods:
            p=periods["validation"];ax.axvspan(dt.date.fromisoformat(p["start"]),dt.date.fromisoformat(p["end"]),color="#d5e7f5",alpha=.45,label="Validation (fixed parameters)")
        ax.set_ylabel(d["unit"]);ax.set_title(key+" | "+d.get("kind","unverified"));ax.grid(alpha=.2);ax.legend()
        ax.xaxis.set_major_locator(mdates.AutoDateLocator());ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
        fig.tight_layout();fig.savefig(Path(output)/f"comparison_{key}.png",dpi=150);plt.close(fig)


def latest_reports(job):
    """Read only the newest numbered collection/validation, preserving every older report."""
    integration=Path(job)/"integration";result={}
    for key,stem in [("collection","toolbox4_result"),("validation","toolbox4_validation")]:
        paths=[p for p in integration.glob(stem+"_*") if (p/"result.json").is_file()]
        if paths:
            path=max(paths,key=lambda p:(p/"result.json").stat().st_mtime_ns)
            result[key]={"path":str(path),"data":read_json(path/"result.json")}
    return result


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest="action",required=True)
    p=sub.add_parser("collect");p.add_argument("--job",type=Path,required=True);p.add_argument("--scenario",type=Path,required=True)
    p=sub.add_parser("validate");p.add_argument("--job",type=Path,required=True);p.add_argument("--collected",type=Path,required=True);p.add_argument("--exe",type=Path,required=True)
    args=parser.parse_args()
    data=collect_results(args.job,args.scenario) if args.action=="collect" else validate_fixed(args.job,args.collected,args.exe)
    print(json.dumps(data,ensure_ascii=False,indent=2))
