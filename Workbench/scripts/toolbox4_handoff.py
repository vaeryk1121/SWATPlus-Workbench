"""Prepare readable Toolbox 4 import materials without assuming a private API."""
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def is_modern_job(job):
    return read_json(Path(job) / "config.resolved.json")["model"]["expected_version"] == "62.0.1"


def prepare_handoff(job):
    job = Path(job).resolve()
    from input_paths import job_root
    root=job_root(job,ROOT)
    cfg = read_json(job / "config.resolved.json")
    if cfg["model"]["expected_version"] != "62.0.1":
        raise ValueError("此导入说明适用于 SWAT+ 62.0.1 模型")
    state = read_json(job / "state.json")
    if not state.get("model_ready"):
        raise ValueError("请先完成模型输入构建")
    runtime = cfg.get("runtime_info") or read_json(root / cfg.get("runtime_config", "config/qgis344_runtime.json"))
    executable = root / runtime["toolbox_executable"]
    if str(runtime.get("toolbox_version")) != "4.0.1":
        raise ValueError("请先核对新版本 Toolbox 的导入流程")
    if not executable.is_file():
        raise FileNotFoundError(executable)
    output = job / "integration/toolbox4"
    record = output / "handoff.json"
    edition=1
    while record.exists():
        data=read_json(record)
        if data.get("handoff_schema")==2:
            if not Path(data["txtinout"]).is_dir():raise FileNotFoundError(data["txtinout"])
            return data
        edition+=1
        output=job/"integration"/f"toolbox4_{edition}"
        record=output/"handoff.json"
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"此目录已有材料，未覆盖：{output}")
    source = root / state["input_snapshot"]
    from swatplus_trial import check_clean_source, configure_period_and_print
    check_clean_source(source, cfg["model"]["expected_version"])
    from multi_observation import read_observation, target_specs
    definitions = cfg.get("observations") or ([cfg["observation"]] if cfg.get("observation") else [])
    period = cfg.get("periods",{}).get("calibration", {"start":f"{cfg['simulation']['start_year']+cfg['simulation']['warmup_years']}-01-01", "end":f"{cfg['simulation']['end_year']}-12-31"})
    selected_references = []
    for definition in definitions:
        reference = read_observation(definition, root)
        selected = [(date, value) for date, value in sorted(reference.items())
                    if period["start"] <= date <= period["end"]]
        if not selected:
            raise ValueError(f"{definition.get('id', 'reference')}: 率定期内没有可导出的参考数据")
        selected_references.append(selected)
    output.mkdir(parents=True, exist_ok=True)
    inputs = output / "TxtInOut"
    shutil.copytree(source, inputs)
    sim = cfg["simulation"]
    configure_period_and_print(inputs, {
        "start_year": sim["start_year"], "end_year": int(period["end"][:4]),
        "warmup_years": sim["warmup_years"], "frequency": cfg["frequency"],
        "output_targets": target_specs(definitions), "csv_output": True})
    observations = []
    for definition, selected in zip(definitions, selected_references):
        path = output / f"reference_{len(observations) + 1}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["Date", "Value"])
            writer.writerows(selected)
        observations.append({"file": str(path), "definition": definition, "records": len(selected)})
    # Human-readable table: native parameter/method/range/scope remain explicit.
    parameter_file = output / "parameters.csv"
    fields = ["name", "native_name", "method", "lower", "upper", "object_type",
              "object_ids", "layer", "file", "column", "columns", "names"]
    with parameter_file.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        writer.writeheader()
        for parameter in cfg.get("parameters", []):
            writer.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v
                             for k, v in parameter.items()})
    instructions = output / "Toolbox4导入说明.txt"
    instructions.write_text(
        "Toolbox 4.0.1 手动导入材料\n\n"
        f"模型输入：{inputs}\n模型引擎：{cfg['model']['expected_version']}\n"
        f"建议对应的本机可执行文件：{cfg['model']['executable']}\n"
        f"模拟从 {sim['start_year']} 年开始，预热 {sim['warmup_years']} 年。\n"
        f"率定期：{period}\n独立验证期：{cfg.get('periods',{}).get('validation','未配置')}\n\n"
        "1. 在 Toolbox 4 中新建工程，选择本目录的 TxtInOut。\n"
        "2. 核对模拟日期、预热期、引擎及物理设置；Print CSV Output选Yes，避免缺少回收所需CSV。\n"
        "   运行设置核对.json记录实际文件中的代码；pet=1为Penman-Monteith，rte_cha=0为Variable Storage。\n"
        "3. 在 Observations 导入 reference_*.csv；Date 为日期，Value 为参考值。\n"
        "   handoff.json 中保留了对应站点/变量/单位/频率和数据来源。仅导出了率定期。\n"
        "4. 在 Parameters 按 parameters.csv 设置参数。absval 为替换值，pctchg 为百分比变化，abschg 为加减量；核对作用对象和土层。\n"
        "5. 在 Calibration 选择方法和预算，在 Toolbox 界面启动。范围表是配置参考，不声称是 Toolbox 可直接导入的项目格式。\n\n"
        "完成率定后，返回工作台并选择实际Calibration目录，回收输出并独立复核；固定验证需另点一次运行。\n"
        "无参考资料时仅交接模型，先完成基础检查，补充资料后再开展率定。\n"
        "parameters.csv是范围参考，不能作为完整搜索配置直接导入；calibration.cal只保存修改方式、作用范围和值。\n"
        "请勿将验证期数据导入本率定工程以反复挑选参数；工作台固定验证不搜索参数。\n"
        "不要将本目录或旧 Toolbox 1.0.5 的 .spt 文件当作新版已验证工程。\n"
        "Robit 参考流量属于教学数据，不能当作已核实的实测流量。\n",
        encoding="utf-8")
    data = {"handoff_schema":2, "source_run": str(job), "txtinout": str(inputs), "toolbox_executable": str(executable),
            "toolbox_version": "4.0.1", "model_version": cfg["model"]["expected_version"],
            "observations": observations, "parameters": str(parameter_file),
            "parameter_definitions": cfg.get("parameters", []), "instructions": str(instructions),
            "status": "materials_prepared_for_manual_import", "calibration_executed": False}
    from toolbox4_results import model_settings
    data["expected_settings"] = model_settings(inputs)
    data["requested_engine"] = cfg.get("engine_info", {"executable":cfg["model"]["executable"]})
    data["toolbox_actual_engine"] = "未确认；须在Toolbox中核对实际Revision，自编译选择不会自动传入Toolbox"
    validation_references = []
    validation = cfg.get("periods",{}).get("validation")
    if validation:
        folder = output / "validation_references"; folder.mkdir(exist_ok=True)
        for i, definition in enumerate(definitions,1):
            selected = [(d,v) for d,v in sorted(read_observation(definition,root).items()) if validation["start"] <= d <= validation["end"]]
            if selected:
                path = folder / f"reference_{i}.csv"
                with path.open("w",encoding="utf-8-sig",newline="") as stream:
                    writer=csv.writer(stream);writer.writerow(["Date","Value"]);writer.writerows(selected)
                validation_references.append({"file":str(path),"definition":definition,"records":len(selected)})
    data["validation_observations"] = validation_references
    (output/"运行设置核对.json").write_text(json.dumps({"settings":data["expected_settings"],"requested_engine":data["requested_engine"],"toolbox_actual_engine":data["toolbox_actual_engine"]},ensure_ascii=False,indent=2),encoding="utf-8")
    record.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def launch_handoff(job):
    data = prepare_handoff(job)
    options = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1
        options["startupinfo"] = startup
    process = subprocess.Popen([data["toolbox_executable"]],
                               cwd=str(Path(data["toolbox_executable"]).parent), **options)
    return data, process
