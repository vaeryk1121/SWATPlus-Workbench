"""Prepare an isolated Editor project and use Editor's observed Toolbox protocol.

This prepares a separate Editor project and a version-specific .spt document,
then launches the actual installed Toolbox with that project. It is a project
handoff, not an invented headless calibration API.
"""
import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

ROOT=Path(__file__).resolve().parents[1]

# Full, version-specific calibration preparation is kept separate from the
# older baseline handoff.  These names are the desktop workbench's public API.
from toolbox_calibration import (prepare_calibration_project,
                                 launch_calibration_project,
                                 collect_calibration_results, verify_replay,
                                 collect_and_verify)


def prepare_spt(data,cfg):
    """Write only a new project using the schema exported by this local version."""
    path=Path(data["project_db"]).parent/"workflow_handoff.spt"
    if not path.exists():
        configuration=Path(data["toolbox_executable"]+".config")
        if not configuration.is_file() or '<value>1.0.5</value>' not in configuration.read_text(encoding="utf-8-sig"):
            raise ValueError("Automatic .spt creation is verified only for Toolbox 1.0.5; create a project through its own GUI for another version")
        sim=cfg["simulation"]
        substitutions={"project_name":path.name,"project_path":str(path),"txtinout":data["txtinout"],
                       "created":dt.datetime.now().astimezone().isoformat(),"last_modified":dt.datetime.now().astimezone().isoformat(),
                       "run_date_start":f"{sim['start_year']}-01-01T00:00:00","run_date_end":f"{sim['end_year']}-12-31T00:00:00","run_warmup":sim["warmup_years"]}
        text=(ROOT/"config/toolbox_1_0_5_template.spt").read_text(encoding="utf-8")
        for key,value in substitutions.items():
            # JSON quoted strings are valid YAML scalars, including Windows paths.
            text=text.replace("__"+key.upper()+"__",json.dumps(value,ensure_ascii=False))
        path.write_text(text,encoding="utf-8")
    data["toolbox_project"]=str(path)
    return data


def prepare_project(run_dir):
    job=Path(run_dir).resolve()
    job.relative_to(ROOT/"runs")
    state=json.loads((job/"state.json").read_text(encoding="utf-8"))
    cfg=json.loads((job/"config.resolved.json").read_text(encoding="utf-8"))
    if cfg["model"]["expected_version"] == "62.0.1":
        raise ValueError("新版模型请从工作台使用 Toolbox 4 导入材料；旧版 .spt 生成器不适用于新版。")
    settings=json.loads((ROOT/"config/external_tools.json").read_text(encoding="utf-8"))
    exe=Path(settings["toolbox_executable"])
    if not exe.is_file():raise FileNotFoundError(f"Toolbox executable missing: {exe}")
    source_inputs=Path(state["source_input"])
    project=source_inputs.parents[2]
    databases=[p for p in project.glob("*.sqlite") if p.name!="swatplus_datasets.sqlite"]
    if len(databases)!=1 or source_inputs!=project/"Scenarios/Default/TxtInOut":
        raise ValueError("This handoff requires the retained Editor project; cannot guess another project's database")
    output=job/"integration/toolbox"
    record=output/"handoff.json"
    if record.exists():
        data=json.loads(record.read_text(encoding="utf-8"))
        if not Path(data["project_db"]).is_file():raise FileNotFoundError("Previously prepared Toolbox project is missing")
        data=prepare_spt(data,cfg)
        record.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        return data
    output.mkdir(parents=True,exist_ok=True)
    copied=output/"editor_project"
    shutil.copytree(project,copied)
    database=copied/databases[0].name
    # Resolve only the external weather reference in the copied project. Do not
    # invent an Editor-run timestamp or modify the retained/source database.
    with sqlite3.connect(database) as db:
        row=db.execute("SELECT weather_data_dir FROM project_config LIMIT 1").fetchone()
        if row and row[0]:
            weather=(project/row[0]).resolve()
            db.execute("UPDATE project_config SET weather_data_dir=?",(str(weather),))
    inputs=copied/"Scenarios/Default/TxtInOut"
    from swatplus_trial import configure_period_and_print
    sim=cfg["simulation"]
    configure_period_and_print(inputs,{"start_year":sim["start_year"],"end_year":sim["end_year"],
                                      "warmup_years":sim["warmup_years"],"frequency":"monthly"})
    obs=cfg["observations"][0] if cfg.get("observations") else cfg["observation"]
    from multi_observation import read_observation
    values=read_observation({"id":"outlet_flow","target":{"variable":"flo_out"},**obs},ROOT)
    with (output/"reference_flow.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.writer(stream);writer.writerow(["date","flow_m3_s"]);writer.writerows(sorted(values.items()))
    data={"source_run":str(job),"source_project":str(project),"project_db":str(database),
          "txtinout":str(inputs),"toolbox_executable":str(exe),"model_version":cfg["model"]["expected_version"],
          "protocol":"Toolbox.exe <workflow_handoff.spt>; schema exported by local Toolbox 1.0.5 and actual opening verified. Editor SQLite is retained alongside it.",
          "reference_kind":obs.get("kind"),"reference_note":obs.get("provenance_note"),
          "baseline_outlet_csv":state.get("evaluation",{}).get("baseline",{}).get("outlet_csv"),
          "note":"Isolated copied project. Interface import/save/run must be verified in Toolbox; this record alone does not prove success."}
    data=prepare_spt(data,cfg)
    record.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return data


def launch_project(run_dir):
    data=prepare_project(run_dir)
    options={}
    if os.name=="nt":
        # This is an interactive GUI. Do not inherit a hidden startup window
        # from the desktop's console-free Python launcher.
        startup=subprocess.STARTUPINFO()
        startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow=1
        options["startupinfo"]=startup
    process=subprocess.Popen([data["toolbox_executable"],data["toolbox_project"]],cwd=str(Path(data["toolbox_executable"]).parent),**options)
    return data,process


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir",type=Path,required=True)
    parser.add_argument("--launch",action="store_true")
    parser.add_argument("--calibration", action="store_true",
                        help="Prepare a configured Toolbox DDS project, not a headless run")
    parser.add_argument("--collect", action="store_true",
                        help="Read a saved Toolbox DDS result without running the model")
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--verify-replay", action="store_true",
                        help="Compare an already completed native replay with Toolbox DDS")
    parser.add_argument("--collect-and-verify", action="store_true",
                        help="Import DDS results and run/reuse one independent fixed-parameter replay")
    args=parser.parse_args()
    if args.collect_and_verify:
        print(json.dumps(collect_and_verify(args.run_dir), ensure_ascii=False, indent=2))
        return
    if args.verify_replay:
        print(json.dumps(verify_replay(args.run_dir), ensure_ascii=False, indent=2))
        return
    if args.collect:
        print(json.dumps(collect_calibration_results(args.run_dir), ensure_ascii=False, indent=2))
        return
    if args.calibration:
        if args.launch:
            data, process = launch_calibration_project(args.run_dir, args.repetitions)
            print(json.dumps({"toolbox_project": data["toolbox_project"],
                              "required_model_revision": data["required_model_revision"],
                              "toolbox_pid": process.pid, "execution": data["execution"]},
                             ensure_ascii=False), flush=True)
            process.wait()
        else:
            print(json.dumps(prepare_calibration_project(args.run_dir, args.repetitions),
                             ensure_ascii=False, indent=2))
        return
    if args.launch:
        data,process=launch_project(args.run_dir)
        print(json.dumps({"toolbox_project":data["toolbox_project"],"required_model_revision":data["model_version"],"toolbox_pid":process.pid},ensure_ascii=False),flush=True)
        process.wait()
    else:print(json.dumps(prepare_project(args.run_dir),ensure_ascii=False,indent=2))


if __name__=="__main__":main()
