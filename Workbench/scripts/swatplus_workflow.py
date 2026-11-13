"""One entry point for SWAT+ construction, bounded search and fixed-parameter validation.

Reuses Swatcup-Auto's actual runner, cancellation, atomic writes and stop comparison.
Search supports the original lightweight LHS/local demonstration and published
SPOTPY DDS. Neither is SUFI-2. Validation data never rank search candidates.
"""
import argparse
import concurrent.futures
import csv
import datetime as dt
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE / "tools/swatcup_auto_upstream"))
from swatcup_auto.runner import run_batch, list_processes
from swatcup_auto.parsers import atomic_write_text, read_text_robust, compare_target
from swatcup_auto.errors import CancelledError
from swatplus_trial import check_clean_source, read_lines, referenced_input_files, relative_key, RUN_ARTIFACT_NAMES, SUPPORTED


def read_json(path):
    return json.loads(read_text_robust(Path(path))[0])


def save_json(path, data):
    atomic_write_text(Path(path), json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def score_pairs(observations, simulation, period):
    """Score only dates inside this named period, without filling missing observations."""
    dates = sorted(d for d in observations if period["start"] <= d <= period["end"] and d in simulation)
    if len(dates) < 2:
        raise ValueError("At least two paired observations are required")
    obs, sim = [observations[d] for d in dates], [simulation[d] for d in dates]
    n = len(dates)
    mean_o, mean_s = sum(obs)/n, sum(sim)/n
    var_o = sum((x-mean_o)**2 for x in obs)/n
    var_s = sum((x-mean_s)**2 for x in sim)/n
    if var_o <= 0 or mean_o <= 0:
        raise ValueError("Observed variance and mean must be positive for NSE/KGE")
    correlation = sum((a-mean_o)*(b-mean_s) for a,b in zip(obs,sim))/n/math.sqrt(var_o*var_s) if var_s > 0 else 0.0
    squared = sum((a-b)**2 for a,b in zip(obs,sim))
    alpha, beta = math.sqrt(var_s/var_o), mean_s/mean_o
    return {"n":n, "NSE":1-squared/(n*var_o),
            "KGE":1-math.sqrt((correlation-1)**2+(alpha-1)**2+(beta-1)**2),
            "RMSE":math.sqrt(squared/n), "bias_percent":100*(sum(sim)-sum(obs))/sum(obs),
            "r":correlation,"mean_reference":mean_o,"mean_simulated":mean_s,
            "first_date":dates[0],"last_date":dates[-1]}


def read_observations(cfg, root):
    obs = cfg["observation"]
    if obs["unit"] != "m3/s" or obs["frequency"] != cfg["frequency"]:
        raise ValueError("Observation frequency/unit must match the selected flow output")
    records, omitted, seen = {}, 0, set()
    with (root/obs["path"]).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            date = dt.datetime.strptime(row[obs["date_column"]],obs["date_format"]).date()
            if cfg["frequency"] == "monthly":
                date = date.replace(day=1)
            key = date.isoformat()
            if key in seen:
                raise ValueError(f"Duplicate observed date: {key}")
            seen.add(key)
            raw = row[obs["value_column"]].strip()
            if raw in obs.get("missing_codes", [""]):
                omitted += 1
                continue
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid observed value at {key}")
            records[key] = value
    if not records:
        raise ValueError("No observation records")
    return records, omitted


def read_simulation(path):
    with Path(path).open(encoding="utf-8-sig",newline="") as stream:
        rows = list(csv.DictReader(stream))
    result = {row["date"]:float(row["flow_m3_s"]) for row in rows}
    if len(result) != len(rows) or any(not math.isfinite(x) or x < 0 for x in result.values()):
        raise ValueError("Invalid/duplicate simulation output")
    return result


def validate_config(cfg, root):
    if cfg.get("workflow_mode") == "toolbox":
        from model_workflow import validate_model_config
        return validate_model_config(cfg, root)
    if cfg.get("workflow_mode")=="research":
        from research_workflow import validate_research_config
        return validate_research_config(cfg,root)
    if cfg["frequency"] not in ("daily","monthly"):
        raise ValueError("Use daily or monthly")
    sim, periods = cfg["simulation"], cfg["periods"]
    first = dt.date(sim["start_year"]+sim["warmup_years"],1,1).isoformat()
    last = dt.date(sim["end_year"],12,31).isoformat()
    cal, val = periods["calibration"],periods["validation"]
    if not first <= cal["start"] <= cal["end"] < val["start"] <= val["end"] <= last:
        raise ValueError("Calibration and later validation periods must be non-overlapping and within model output dates")
    names=set()
    for parameter in cfg["parameters"]:
        if parameter["name"] in names:
            raise ValueError("Parameter names must be unique")
        names.add(parameter["name"])
        lo, hi = parameter["lower"],parameter["upper"]
        if not math.isfinite(lo) or not math.isfinite(hi) or lo >= hi:
            raise ValueError("Parameter search bounds must be finite and increasing")
        if "native_name" in parameter:
            if cfg.get("parameter_backend")!="native_calibration":raise ValueError("native_name requires native_calibration backend")
            from native_calibration import validate_definitions
            for value in [lo,hi]:validate_definitions([{**parameter,"value":value}],expected_version=cfg["model"]["expected_version"])
            continue
        for column in parameter.get("columns",[parameter.get("column")]):
            if parameter["file"] not in SUPPORTED or column not in SUPPORTED[parameter["file"]]:
                raise ValueError(f"Unsupported SWAT+ parameter {parameter['file']}:{column}")
    backend=cfg.get("parameter_backend","direct_file")
    if backend not in ("direct_file","native_calibration"):
        raise ValueError("parameter_backend must be direct_file or native_calibration")
    if backend=="native_calibration":
        for p in cfg["parameters"]:
            if "native_name" in p:continue
            if p["file"]!="hydrology.hyd" or p.get("column") not in ("esco","perco") or p["method"] not in ("absolute","absval") or "columns" in p:
                raise ValueError("Native calibration currently supports only ESCO/PERCO absolute edits")
    search=cfg["search"]
    if search.get("method") not in ("latin_hypercube_then_local","spotpy_dds"):
        raise ValueError("Supported search methods: latin_hypercube_then_local, spotpy_dds")
    if type(search.get("workers",1)) is not int or search.get("workers",1)<1:
        raise ValueError("workers must be a positive integer")
    if type(search.get("seed")) is not int:
        raise ValueError("seed must be an integer")
    if search["method"]=="spotpy_dds":
        if search.get("workers",1)!=1:
            raise ValueError("SPOTPY DDS uses one sequential adaptive search; workers must be 1")
        if type(search.get("repetitions")) is not int or search["repetitions"]<7:
            raise ValueError("DDS repetitions must be at least 7, including 5 initial samples")
        if not 0 < search.get("r",0.2) <= 1:
            raise ValueError("DDS r must be in (0,1]")
        from mature_search import engine_info
        engine_info()
        planned=search["repetitions"]
    else:
        if not search["simulation_schedule"] or any(type(n) is not int or n<1 for n in search["simulation_schedule"]):
            raise ValueError("simulation_schedule requires positive integers")
        planned=sum(search["simulation_schedule"])
    if search.get("objective","NSE") not in ("NSE","KGE"):
        raise ValueError("This first optimizer maximizes NSE or KGE")
    if not 0 < search.get("local_fraction",0.5) <= 1:
        raise ValueError("local_fraction must be in (0,1]")
    obs, omitted=read_observations(cfg,root)
    for label,period in periods.items():
        available={d:v for d,v in obs.items() if period["start"]<=d<=period["end"]}
        score_pairs(available, available, period)
    return {"observation_records":len(obs),"omitted_missing_records":omitted,
            "observation_kind":cfg["observation"]["kind"],
            "calibration_records":sum(cal["start"]<=d<=cal["end"] for d in obs),
            "validation_records":sum(val["start"]<=d<=val["end"] for d in obs),
            "planned_search_trials":planned}


def latin_hypercube(parameters, count, seed, center=None, fraction=1.0):
    rng=random.Random(seed)
    result=[{} for _ in range(count)]
    for p in parameters:
        low,high=p["lower"],p["upper"]
        if center and p["name"] in center:
            width=(high-low)*fraction
            low,high=max(low,center[p["name"]]-width/2),min(high,center[p["name"]]+width/2)
        order=list(range(count));rng.shuffle(order)
        for i,level in enumerate(order):
            result[i][p["name"]]=round(low+(high-low)*(level+rng.random())/count,9)
    return result


def parameter_edits(cfg, values):
    edits=[]
    for p in cfg["parameters"]:
        if p["name"] not in values:
            continue
        if "native_name" in p:
            edits.append({**{k:v for k,v in p.items() if k not in ("name","lower","upper")},"value":values[p["name"]]})
            continue
        for column in p.get("columns",[p.get("column")]):
            edit={"file":p["file"],"column":column,"method":p["method"],"value":values[p["name"]]}
            for key in ("names","min","max"):
                if key in p: edit[key]=p[key]
            edits.append(edit)
    return edits


CONNECT_SLOTS = ("hru.con", "hru-lte.con", "rout_unit.con", "gwflow.con", "aquifer.con", "aquifer2d.con",
                 "channel.con", "reservoir.con", "recall.con", "exco.con", "delratio.con", "outlet.con", "chandeg.con")


def connect_filenames(source):
    """Map the file.cio connect row slots to actual filenames, null slots skipped."""
    rows = read_lines(Path(source) / "file.cio")
    result = {}
    for line in rows[1:]:
        fields = line.split()
        if fields and fields[0] == "connect":
            for slot, filename in zip(CONNECT_SLOTS, fields[1:]):
                if filename.lower() != "null":
                    result[slot] = filename
            return result
    raise ValueError("file.cio has no connect row")


def identify_outlet(source, configured):
    """Resolve the evaluation outlet: terminal channels or terminal reservoirs.

    'auto' only resolves an unambiguous single terminal object; several outlets,
    or a terminal reservoir, must be configured explicitly. An explicitly
    configured channel may also be an interior station, so a station inside the
    watershed can be evaluated without pretending it is the basin outlet.
    """
    source = Path(source)
    channels, reservoirs = [], []
    connect = connect_filenames(source)
    for slot, collection, kind in (("chandeg.con", channels, "channel_sd"), ("reservoir.con", reservoirs, "reservoir")):
        filename = connect.get(slot)
        if not filename or not (source / filename).is_file():
            continue
        rows = read_lines(source / filename)
        header = rows[1].split()
        for line in rows[2:]:
            fields = line.split()
            if fields and int(fields[header.index("out_tot")]) == 0:
                collection.append({"object": kind, "unit": int(fields[0]),
                                   "gis_id": int(fields[header.index("gis_id")]),
                                   "name": fields[header.index("name")]})
    terminals = channels + reservoirs
    if configured == "auto":
        if len(terminals) != 1:
            raise ValueError("Specify the evaluation outlet explicitly; terminal objects found: "
                             f"{terminals}. 多个末端出口或水库末端不能自动唯一确定；内部站点也须显式指定。")
        return terminals[0]
    # 'name' is optional: callers that only know unit/gis_id get the record's
    # name filled in; a provided name must match the record it identifies.
    selected = dict(configured)
    matches = [entry for entry in terminals
               if entry["unit"] == selected.get("unit") and entry["gis_id"] == selected.get("gis_id")]
    if not selected.get("object"):
        kinds = {entry["object"] for entry in matches}
        selected["object"] = kinds.pop() if len(kinds) == 1 else "channel_sd"
    if selected["object"] == "reservoir":
        reservoirs_matched = [entry for entry in matches if entry["object"] == "reservoir"]
        if not reservoirs_matched:
            raise ValueError(f"Configured outlet is not a terminal reservoir; terminal objects: {terminals}")
        record = reservoirs_matched[0]
    else:
        # Explicit channel stations may be interior; any record listed in chandeg.con is valid.
        chandeg = connect.get("chandeg.con")
        if not chandeg:
            raise ValueError(f"file.cio lists no chandeg.con; terminal objects: {terminals}")
        rows = read_lines(source / chandeg)
        header = rows[1].split()
        record = None
        for line in rows[2:]:
            fields = line.split()
            if fields and int(fields[0]) == selected.get("unit"):
                record = {"unit": int(fields[0]), "gis_id": int(fields[header.index("gis_id")]),
                          "name": fields[header.index("name")]}
                break
        if record is None or record["gis_id"] != selected.get("gis_id"):
            raise ValueError(f"Configured outlet is not a listed channel or terminal object; terminal objects: {terminals}")
    if selected.get("name") and selected["name"] != record["name"]:
        raise ValueError(f"Configured outlet name {selected['name']} does not match record {record['name']}")
    selected["name"] = record["name"]
    return selected


def prepare_input_snapshot(source, destination, extensions=None):
    """Copy the complete export tree, preserving subdirectory dependencies.

    A legacy extension whitelist cannot be trusted to cover management,
    decision-table, weather or water-rights files, so it no longer filters
    inputs: the whole tree is copied with its relative structure intact. Prior
    model-run artifacts (success.fin, checker.out, *_day/mon/yr/aa outputs)
    are stripped so the snapshot is a clean input export — except files the
    export itself references (e.g. a recall series named demand_day.txt),
    which are inputs whatever their name and are never removed.
    """
    source, destination = Path(source), Path(destination)
    referenced = referenced_input_files(source)["files"]
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    skipped = []
    for path in sorted(destination.rglob("*"), reverse=True):
        # Same key normalization as the dependency checker: a Windows-style
        # backslash reference and the copied forward-slash path must compare
        # equal, or a referenced input is deleted as a run artifact.
        relative = path.relative_to(destination).as_posix()
        if not path.is_file() or relative_key(relative) in referenced:
            continue
        if path.name in RUN_ARTIFACT_NAMES or re.search(r"_(?:day|mon|yr|aa)\.txt$", path.name):
            skipped.append(relative)
            path.unlink()
    for path in sorted(destination.rglob("*")):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    files = sum(1 for path in destination.rglob("*") if path.is_file())
    directories = sum(1 for path in destination.rglob("*") if path.is_dir())
    return {"files_copied": files, "subdirectories": directories,
            "run_artifacts_removed": sorted(skipped),
            "referenced_inputs_protected": len(referenced),
            "extensions_filter": "ignored; the complete input tree was copied"}


class Workflow:
    # Classic flow-scored calibration reads outlet flow in m^3/s; subclasses
    # that evaluate explicit multi-object targets may accept other outlets.
    requires_channel_outlet = True

    def __init__(self, job, state, cancel, pause_after=0):
        self.job,self.state,self.cancel=Path(job),state,cancel
        self.root=Path(state["workspace_root"])
        self.cfg=read_json(self.job/"config.resolved.json")
        self.pause_after,self.new_trials=pause_after,0
        # Keep the actual observed inputs used for this task, so a later source
        # edit cannot mix old trial scores with new observations during resume.
        if "original_path" not in self.cfg["observation"]:
            original=self.root/self.cfg["observation"]["path"]
            snapshot=self.job/"observation_input.csv"
            if not snapshot.exists(): shutil.copy2(original,snapshot)
            self.cfg["observation"].update(original_path=str(original),path=str(snapshot))
            check_obs,_=read_observations(self.cfg,self.root)
            for entry in ([state["baseline"]] if "baseline" in state else [])+state["trials"]:
                if entry.get("status")=="completed":
                    rescored=score_pairs(check_obs,read_simulation(entry["outlet_csv"]),self.cfg["periods"]["calibration"])
                    if any(abs(rescored[k]-entry["metrics"][k])>1e-10 for k in ["NSE","KGE","RMSE","bias_percent"]):
                        raise ValueError("Observation snapshot disagrees with completed trial scores")
            save_json(self.job/"config.resolved.json",self.cfg)
        self.observations,_=read_observations(self.cfg,self.root)

    def save(self):
        save_json(self.job/"state.json",self.state)

    def message(self,text):
        print(text,flush=True)
        with (self.job/"workflow.log").open("a",encoding="utf-8") as stream:
            stream.write(dt.datetime.now().isoformat(timespec="seconds")+" "+text+"\n")

    def batch(self,directory,name,command,timeout):
        directory.mkdir(parents=True,exist_ok=True)
        # ASCII first lines switch CMD before it reads paths containing Chinese.
        content="@echo off\nchcp 65001 >nul\nset PYTHONIOENCODING=utf-8\n"+subprocess.list2cmdline([str(x) for x in command])+"\nexit /b %errorlevel%\n"
        (directory/name).write_text(content,encoding="utf-8")
        return run_batch(directory,name,directory/(name+".log"),timeout_seconds=timeout,
                         cancel_event=self.cancel,event_callback=None)

    def ensure_model(self):
        calibration_policy = self.cfg["model"].get("calibration_policy", "reject")
        if self.state.get("model_ready"):
            check_clean_source(Path(self.state["input_snapshot"]),self.cfg["model"]["expected_version"],calibration_policy)
            return
        self.state["phase"]="build";self.save()
        model=self.cfg["model"]
        if self.cfg.get("build",{}).get("enabled"):
            attempt=self.state.get("build_attempt",0)
            previous_name=self.state.get("build_name")
            completed=False
            if previous_name:
                record=self.root/f"runs/{previous_name}_build/pipeline_result.json"
                completed=record.exists() and len(read_json(record)) in (4,5) and all(x["exit_code"]==0 for x in read_json(record))
            if not completed:
                attempt+=1
                name=self.state["run_name"]+f"_b{attempt}"
                self.state.update(build_attempt=attempt,build_name=name);self.save()
                from input_paths import CODE_ROOT
                command=[sys.executable,"-X","utf8",CODE_ROOT/"scripts/build_and_run.py","--config",self.root/self.cfg["build"]["pipeline_config"],"--name",name]
                if self.cfg.get("engine_info",{}).get("mode")=="custom" or self.cfg.get("workflow_mode")=="toolbox":
                    selected=self.job/"build_engine.json"
                    save_json(selected,{"model":self.cfg["model"],"engine_info":self.cfg.get("engine_info",{}),"simulation":self.cfg.get("simulation")})
                    command += ["--engine-config",selected]
                self.message(f"自动构建项目 {name}")
                self.batch(self.job/f"build_attempt_{attempt}","build.bat",command,self.cfg["build"].get("timeout_seconds",1200))
            name=self.state["build_name"]
            source=self.root/f"models/examples/{name}/Scenarios/Default/TxtInOut"
            self.state["build_record"]=str(self.root/f"runs/{name}_build/pipeline_result.json")
        else:
            source=self.root/model["input_dir"]
        snapshot=self.job/"model_inputs"
        if snapshot.exists():
            # An interrupted copy is preserved as a failed attempt before retry.
            n=1
            while (self.job/f"model_inputs_incomplete_{n}").exists(): n+=1
            snapshot.rename(self.job/f"model_inputs_incomplete_{n}")
        prepare_input_snapshot(source,snapshot,model.get("input_extensions"))
        check_clean_source(snapshot,model["expected_version"],calibration_policy)
        self.state["outlet"]=identify_outlet(snapshot,model.get("outlet","auto"))
        if self.requires_channel_outlet and self.state["outlet"].get("object","channel_sd")!="channel_sd":
            raise ValueError("末端评价对象是水库（或出口非河道）；经典流量率定需要 m³/s 河道出口。"
                             "请显式配置一个河道unit（可为内部站点），或改用研究型多观测工作流。")
        self.state.update(model_ready=True,input_snapshot=str(snapshot),source_input=str(source),
                          calibration_policy=calibration_policy,
                          print_policy=self.cfg["model"].get("print_policy","preserve"))
        self.save()

    def make_spec(self,values,work,validation=False):
        period=self.cfg["periods"]["validation" if validation else "calibration"]
        sim=self.cfg["simulation"]
        model=self.cfg["model"]
        spec={"root":str(self.root),"input_dir":self.state["input_snapshot"],
                "executable":str(self.root/model["executable"]),
                "expected_version":model["expected_version"],"work_dir":str(work),
                "start_year":sim["start_year"],"end_year":int(period["end"][:4]),"warmup_years":sim["warmup_years"],
                "outlet":self.state["outlet"],"frequency":self.cfg["frequency"],
                "timeout_seconds":model.get("timeout_seconds",600),
                "print_policy":model.get("print_policy","preserve"),
                "calibration_policy":self.state.get("calibration_policy",model.get("calibration_policy","reject")),
                "parameters":parameter_edits(self.cfg,values)}
        if "parameter_backend" in self.cfg:spec["parameter_backend"]=self.cfg["parameter_backend"]
        if self.cfg.get("engine_info"):
            spec["engine_info"]=self.cfg["engine_info"]
        return spec

    def execute_entry(self,entry,validation=False):
        directory=self.job/"trials"/entry["trial_id"]
        # Recover a finished model if interruption occurred just before the
        # controller recorded it. Only identical stored trial specs qualify.
        for result_path in sorted(directory.glob("attempt_*/trial_result.json"),reverse=True):
            previous=read_json(result_path)
            if previous.get("status")=="completed":
                expected=self.make_spec(entry["values"],result_path.parent,validation)
                if read_json(result_path.parent/"trial_spec.json") == expected:
                    metrics=score_pairs(self.observations,read_simulation(previous["outlet_csv"]),self.cfg["periods"]["validation" if validation else "calibration"])
                    return {"status":"completed","attempt":int(result_path.parent.name.split("_")[-1]),
                            "result_path":str(result_path),"outlet_csv":previous["outlet_csv"],
                            "metrics":metrics,"elapsed_seconds":previous["elapsed_seconds"]}
        attempt=entry.get("attempt",0)+1
        while (directory/f"attempt_{attempt:02d}").exists(): attempt+=1
        attempt_dir=directory/f"attempt_{attempt:02d}"
        directory.mkdir(parents=True,exist_ok=True)
        spec=self.make_spec(entry["values"],attempt_dir,validation)
        spec_path=directory/f"spec_{attempt:02d}.json"
        save_json(spec_path,spec)
        command=[sys.executable,"-X","utf8",self.root/"scripts/swatplus_trial.py","--spec",spec_path]
        self.batch(directory,f"trial_{attempt:02d}.bat",command,spec["timeout_seconds"]+60)
        result=read_json(attempt_dir/"trial_result.json")
        if result["status"]!="completed": raise RuntimeError(result)
        simulation=read_simulation(result["outlet_csv"])
        metrics=score_pairs(self.observations,simulation,self.cfg["periods"]["validation" if validation else "calibration"])
        return {"status":"completed","attempt":attempt,"result_path":str(attempt_dir/"trial_result.json"),
                "outlet_csv":result["outlet_csv"],"metrics":metrics,"elapsed_seconds":result["elapsed_seconds"]}

    def one(self,entry,validation=False):
        if entry.get("status")=="completed": return
        entry["status"]="running";self.save()
        try:
            entry.update(self.execute_entry(entry,validation))
            entry.pop("error",None)
        except Exception as error:
            entry.update(status="failed",error=str(error));self.save();raise
        self.save()

    def best(self):
        candidates=[self.state["baseline"]]+[x for x in self.state["trials"] if x.get("status")=="completed"]
        metric=self.cfg["search"].get("objective","NSE")
        return max(candidates,key=lambda x:x["metrics"][metric])

    def write_history(self):
        rows=[]
        best=-float("inf")
        metric=self.cfg["search"].get("objective","NSE")
        for entry in [self.state["baseline"]]+self.state["trials"]:
            if entry.get("status")!="completed": continue
            best=max(best,entry["metrics"][metric])
            rows.append({"trial_id":entry["trial_id"],"round":entry.get("round",0),
                         "score":entry["metrics"][metric],"best_so_far":best,
                         **entry["metrics"],**entry["values"]})
        columns=["trial_id","round","score","best_so_far","n","NSE","KGE","RMSE","bias_percent","r","mean_reference","mean_simulated","first_date","last_date"]+[p["name"] for p in self.cfg["parameters"]]
        with (self.job/"trials.csv").open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(rows)
        return rows

    def run_search(self):
        self.state["phase"]="calibration";self.save()
        self.state.setdefault("baseline",{"trial_id":"baseline_calibration","values":{},"round":0,"status":"pending"})
        self.one(self.state["baseline"])
        self.message(f"基线率定期 {self.cfg['search'].get('objective','NSE')}={self.state['baseline']['metrics'][self.cfg['search'].get('objective','NSE')]:.4f}")
        search=self.cfg["search"]
        if search["method"]=="spotpy_dds":return self.run_dds_search()
        for round_index,count in enumerate(search["simulation_schedule"],1):
            ids=[x for x in self.state["trials"] if x["round"]==round_index]
            if not ids:
                center=self.best()["values"] if round_index>1 else None
                samples=latin_hypercube(self.cfg["parameters"],count,search["seed"]+round_index,
                                       center,search.get("local_fraction",0.5))
                ids=[{"trial_id":f"r{round_index:02d}_{i+1:03d}","round":round_index,"values":values,"status":"pending"} for i,values in enumerate(samples)]
                self.state["trials"].extend(ids);self.save()
            pending=[x for x in ids if x.get("status")!="completed"]
            while pending:
                if self.cancel.is_set(): raise CancelledError("用户停止任务")
                number=min(search.get("workers",1),len(pending))
                if self.pause_after:
                    number=min(number,self.pause_after-self.new_trials)
                    if number<=0:
                        self.state.update(status="paused",phase="calibration");self.save();self.write_history();return False
                chunk=pending[:number]
                for entry in chunk: entry["status"]="running"
                self.save()
                with concurrent.futures.ThreadPoolExecutor(max_workers=number) as executor:
                    futures={executor.submit(self.execute_entry,entry):entry for entry in chunk}
                    for future in concurrent.futures.as_completed(futures):
                        entry=futures[future]
                        try:
                            entry.update(future.result());entry.pop("error",None)
                        except Exception as error:
                            entry.update(status="failed",error=str(error));self.save();self.cancel.set();raise
                        self.new_trials+=1;self.save()
                        self.message(f"{entry['trial_id']} 完成：{search.get('objective','NSE')}={entry['metrics'][search.get('objective','NSE')]:.4f}")
                self.write_history()
                pending=[x for x in ids if x.get("status")!="completed"]
            stop=search.get("target",{})
            if stop.get("enabled") and compare_target(self.best()["metrics"][stop["metric"]],stop["operator"],stop["value"]):
                self.state["stop_reason"]="calibration_target_reached";break
        self.state["selected"]={k:self.best()[k] for k in ["trial_id","values","metrics"]}
        self.state["selection_basis"]="calibration_period_only"
        save_json(self.job/"best_parameters.json",self.state["selected"])
        self.save();return True

    def run_dds_search(self):
        from mature_search import run_dds,engine_info,SearchPaused,SearchTargetReached
        search=self.cfg["search"]
        info=engine_info()
        if self.state.get("search_engine",info)!=info:
            raise ValueError("DDS engine changed; resume requires the recorded library versions")
        self.state["search_engine"]=info;self.save()
        self.message(f"成熟率定后端：SPOTPY {info['versions']['spotpy']} DDS；预算 {search['repetitions']}；单序列")
        position=0
        replayed=0

        def evaluate(values):
            nonlocal position,replayed
            if self.cancel.is_set():raise CancelledError("用户停止任务")
            position+=1
            if position<=len(self.state["trials"]):
                entry=self.state["trials"][position-1]
                if entry["trial_id"]!=f"dds_{position:05d}" or entry["values"]!=values:
                    raise ValueError(f"DDS replay differs at candidate {position}; configuration/engine changed")
                if entry.get("status")=="completed":
                    replayed+=1
                    return entry["metrics"][search.get("objective","NSE")]
            else:
                if self.pause_after and self.new_trials>=self.pause_after:raise SearchPaused()
                entry={"trial_id":f"dds_{position:05d}","round":1,"values":values,"status":"pending"}
                self.state["trials"].append(entry)
            if self.pause_after and self.new_trials>=self.pause_after:raise SearchPaused()
            self.one(entry);self.new_trials+=1;self.write_history()
            metric=search.get("objective","NSE")
            self.message(f"{entry['trial_id']} 完成：{metric}={entry['metrics'][metric]:.4f}")
            stop=search.get("target",{})
            if stop.get("enabled") and compare_target(self.best()["metrics"][stop["metric"]],stop["operator"],stop["value"]):
                raise SearchTargetReached()
            return entry["metrics"][metric]

        try:
            run_dds(self.cfg["parameters"],search["repetitions"],search["seed"],search.get("r",0.2),evaluate)
        except SearchPaused:
            self.state.update(status="paused",phase="calibration");self.save();self.write_history();return False
        except SearchTargetReached:
            self.state["stop_reason"]="calibration_target_reached"
        self.state["replayed_search_trials"]=replayed
        self.state["selected"]={k:self.best()[k] for k in ["trial_id","values","metrics"]}
        self.state["selection_basis"]="calibration_period_only"
        save_json(self.job/"best_parameters.json",self.state["selected"])
        self.save();return True

    def evaluate_and_report(self):
        self.state["phase"]="fixed_parameter_validation";self.save()
        evaluation=self.state.setdefault("evaluation",{})
        evaluation.setdefault("baseline",{"trial_id":"baseline_validation","values":{},"status":"pending"})
        evaluation.setdefault("best",{"trial_id":"selected_validation","values":self.state["selected"]["values"],"status":"pending"})
        if evaluation["best"]["values"] != self.state["selected"]["values"]:
            raise ValueError("Validation parameters differ from calibration selection")
        self.one(evaluation["baseline"],True)
        self.one(evaluation["best"],True)
        baseline=read_simulation(evaluation["baseline"]["outlet_csv"])
        best=read_simulation(evaluation["best"]["outlet_csv"])
        metrics={}
        for name,series in [("baseline",baseline),("best",best)]:
            metrics[name]={label:score_pairs(self.observations,series,period) for label,period in self.cfg["periods"].items()}
        # Extending an unchanged simulation to the validation year must leave
        # printed calibration-period values unchanged.
        chosen=self.state["baseline"] if self.state["selected"]["trial_id"]==self.state["baseline"]["trial_id"] else next(x for x in self.state["trials"] if x["trial_id"]==self.state["selected"]["trial_id"])
        selected_cal=read_simulation(chosen["outlet_csv"])
        baseline_cal=read_simulation(self.state["baseline"]["outlet_csv"])
        for previous,extended in [(selected_cal,best),(baseline_cal,baseline)]:
            if any(extended.get(date)!=value for date,value in previous.items()):
                raise ValueError("Validation extension changed the calibration output")
        rows=[]
        for date in sorted(self.observations):
            period=next((label for label,window in self.cfg["periods"].items() if window["start"]<=date<=window["end"]),None)
            if period and date in baseline and date in best:
                rows.append({"date":date,"reference":self.observations[date],"baseline":baseline[date],"best":best[date],"period":period})
        with (self.job/"paired_results.csv").open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=["date","reference","baseline","best","period"]);writer.writeheader();writer.writerows(rows)
        report={"title":self.cfg["title"],"observation":self.cfg["observation"],"frequency":self.cfg["frequency"],
                "periods":self.cfg["periods"],"metrics":metrics,"best_parameters":self.state["selected"]["values"],
                "trials":self.write_history(),"series":rows,
                "workflow_note":"校准仅使用率定期；验证采用率定期选定的同一参数，连续模拟到验证期末。小预算测试不证明收敛或跨流域有效。",
                "source_reuse_note":"实际调用Swatcup-Auto的运行、取消和文本工具。"+(
                    "参数搜索使用SPOTPY 1.6.2的DDS原实现（单序列）；不是Toolbox程序或SUFI-2，不提供95PPU。"
                    if self.cfg["search"]["method"]=="spotpy_dds" else "参数搜索为LHS及局部采样，不是SUFI-2。"),
                "search_engine":self.state.get("search_engine",{"method":self.cfg["search"]["method"]}),
                "parameter_backend":self.cfg.get("parameter_backend","direct_file")}
        report["engine_info"]=self.cfg.get("engine_info",{})
        save_json(self.job/"report_data.json",report)
        from workflow_report import render_report
        render_report(self.job)
        self.state.update(status="completed",phase="completed",metrics=metrics,
                          validation_fixed_parameters=True,calibration_output_unchanged=True)
        self.save()
        self.message(f"流程完成，报告：{self.job/'report.md'}")

    def run(self):
        previous_pid=self.state.get("owner_pid")
        if self.state.get("status")=="running" and previous_pid and previous_pid!=os.getpid():
            if any(pid==previous_pid for pid,_ in list_processes()):
                raise RuntimeError(f"任务仍由进程 {previous_pid} 运行；请先等待完成或使用stop。")
        self.state["owner_pid"]=os.getpid()
        stop_file=self.job/"stop.request"
        # Explicit resume consumes a prior stop signal; model/results remain.
        if stop_file.exists(): stop_file.unlink()
        finished=threading.Event()
        def watch_stop():
            while not finished.wait(0.25):
                if stop_file.exists():
                    self.cancel.set();return
        threading.Thread(target=watch_stop,daemon=True).start()
        self.state["status"]="running";self.save()
        try:
            self.ensure_model()
            if "selected" not in self.state and not self.run_search():
                self.message("已在完成的试验边界暂停，可用resume续跑");return 0
            self.evaluate_and_report()
            return 0
        except (CancelledError,KeyboardInterrupt) as error:
            self.state.update(status="interrupted",last_error=str(error));self.save();self.message("已停止并保存进度")
            return 130
        except Exception as error:
            self.state.update(status="failed",last_error=str(error));self.save();raise
        finally:
            finished.set()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="action",required=True)
    for name in ["check","run"]:
        p=sub.add_parser(name);p.add_argument("--config",type=Path,required=True)
        if name=="run":
            p.add_argument("--name",required=True);p.add_argument("--pause-after-trials",type=int,default=0)
    for name in ["resume","status","stop"]:
        p=sub.add_parser(name);p.add_argument("--run-dir",type=Path,required=True)
        if name=="resume":
            p.add_argument("--sufi-review",type=Path,help="Pending SUFI-2 calibration-round decision JSON")
    p=sub.add_parser("compare-engines");p.add_argument("--config",type=Path,required=True)
    p.add_argument("--name",required=True);p.add_argument("--input-dir",type=Path)
    args=parser.parse_args()
    if args.action=="compare-engines":
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,20}",args.name):
            parser.error("Use a comparison name of at most 21 letters/digits/underscores")
        from engine_comparison import compare_engines
        compare_engines(read_json(args.config),WORKSPACE,WORKSPACE/"runs"/args.name,args.input_dir)
        return
    cancel=threading.Event()
    signal.signal(signal.SIGINT,lambda *unused:cancel.set())
    if args.action in ("check","run"):
        from input_paths import config_root
        raw_cfg=read_json(args.config)
        root=config_root(args.config,raw_cfg) if raw_cfg.get('workspace_root') else WORKSPACE
        from workflow_runtime import resolve_runtime_config
        cfg=resolve_runtime_config(raw_cfg,root)
        check=validate_config(cfg,root)
        check["model_engine"]=cfg.get("engine_info",{})
        print(json.dumps(check,ensure_ascii=False,indent=2),flush=True)
        if args.action=="check":return
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,20}",args.name):
            parser.error("Use an English name of 1-21 letters/digits/underscores")
        job=root/"runs"/args.name
        job.mkdir(parents=True,exist_ok=False)
        from workflow_runtime import prepare_job_engine
        cfg=prepare_job_engine(cfg,job)
        print("实际模型程序："+cfg["model"]["executable"],flush=True)
        save_json(job/"config.resolved.json",cfg)
        state={"run_name":args.name,"workspace_root":str(root),"status":"created","phase":"created","trials":[],
               "source_config":str(args.config.resolve()),"created_at":dt.datetime.now().isoformat(timespec="seconds"),"input_check":check}
        save_json(job/"state.json",state)
        cls=Workflow
        if cfg.get("workflow_mode")=="toolbox":
            from model_workflow import ModelWorkflow
            cls=ModelWorkflow
        if cfg.get("workflow_mode")=="research":
            from research_workflow import ResearchWorkflow
            cls=ResearchWorkflow
        return cls(job,state,cancel,args.pause_after_trials).run()
    else:
        job=args.run_dir.resolve();state=read_json(job/"state.json")
        if args.action=="stop":
            if state["status"]=="running":
                atomic_write_text(job/"stop.request","stop\n")
                print("已请求停止；当前子进程将退出，已完成结果保留。",flush=True)
            else:
                print(f"任务当前状态为 {state['status']}，无需发送停止信号。",flush=True)
            return
        if args.action=="status":
            print(json.dumps({"run_name":state["run_name"],"status":state["status"],"phase":state["phase"],
                              "completed_search_trials":sum(x.get("status")=="completed" for x in state["trials"]),
                              "selected":state.get("selected"),"last_error":state.get("last_error"),
                              "sufi2_pending_review":state.get("sufi2",{}).get("pending_review"),
                              "sufi2_execution_issue":state.get("sufi2",{}).get("execution_issue")},ensure_ascii=False,indent=2));return
        if state["status"] in ("completed","ready"):
            print("任务已经完成；未重复运行模型。",flush=True);return
        cls=Workflow
        if read_json(job/"config.resolved.json").get("workflow_mode")=="toolbox":
            from model_workflow import ModelWorkflow
            cls=ModelWorkflow
        if read_json(job/"config.resolved.json").get("workflow_mode")=="research":
            from research_workflow import ResearchWorkflow
            cls=ResearchWorkflow
        controller=cls(job,state,cancel)
        if args.sufi_review:
            if not hasattr(controller,"apply_sufi2_review"):
                raise ValueError("--sufi-review requires a research SUFI-2 task")
            controller.apply_sufi2_review(read_json(args.sufi_review))
        return controller.run()


if __name__=="__main__":
    sys.exit(main())
