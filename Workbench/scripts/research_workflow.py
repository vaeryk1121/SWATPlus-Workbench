"""Multi-observation optimization, sensitivity and parameter-ensemble analysis.

Extends the existing isolated SWAT+ worker. Observations and validation periods
remain separate from search. No measured data are manufactured by this module.
"""
import concurrent.futures
import csv
import datetime as dt
import math
from pathlib import Path
import shutil
import statistics
import sys

from swatplus_workflow import Workflow, read_json, save_json, parameter_edits, CancelledError
from multi_observation import (validate_observations,read_observation,joint_score,
                               target_specs,read_target_csv)


def validate_research_config(cfg,root):
    from mature_search import validate_search_settings,engine_info
    from native_calibration import validate_definitions
    sim=cfg["simulation"];cal=cfg["periods"]["calibration"];val=cfg["periods"]["validation"]
    first=dt.date(sim["start_year"]+sim["warmup_years"],1,1).isoformat()
    last=dt.date(sim["end_year"],12,31).isoformat()
    if sim["warmup_years"]<0 or not first<=cal["start"]<=cal["end"]<val["start"]<=val["end"]<=last:
        raise ValueError("Preheat, calibration and later validation periods are inconsistent")
    if cfg.get("parameter_backend")!="native_calibration":raise ValueError("Research workflow requires native_calibration")
    if cfg["search"].get("objective")!="JOINT":raise ValueError("Research search.objective must be JOINT; per-series metrics belong in observations[]")
    names=set()
    for p in cfg["parameters"]:
        if p["name"] in names:raise ValueError("Duplicate parameter name")
        names.add(p["name"])
        if not all(math.isfinite(p[x]) for x in ["lower","upper"]) or p["lower"]>=p["upper"]:
            raise ValueError("Invalid parameter search bounds")
        for value in [p["lower"],p["upper"]]:
            validate_definitions(parameter_edits({"parameters":[p]}, {p["name"]:value}),expected_version=cfg["model"]["expected_version"])
    search=cfg["search"];options=dict(search.get("options",{}))
    if search.get("workers",1)!=1:raise ValueError("Adaptive search uses workers=1; analysis.workers controls independent ensembles")
    if type(search.get("seed")) is not int:raise ValueError("A fixed integer search seed is required")
    if search["method"]=="spotpy_dream":options["objective_kind"]="gaussian_log_likelihood"
    requirements=validate_search_settings(search["method"],cfg["parameters"],search["repetitions"],options)
    engine_info(search["method"])
    counts=validate_observations(cfg,root)
    if search["method"]=="swatcup_sufi2":
        from sufi2_bridge import DEFAULT_EXECUTABLE_DIR,PROGRAMS
        if len(cfg["parameters"])>500:raise ValueError("Legacy SUFI-2 supports at most 500 parameter coordinates")
        for p in cfg["parameters"]:
            low=p.get("coordinate_min",p["lower"]);high=p.get("coordinate_max",p["upper"])
            if not (math.isfinite(low) and math.isfinite(high) and low<=p["lower"]<p["upper"]<=high):
                raise ValueError("SUFI-2 coordinate bounds must contain the initial search interval")
            for value in (low,high):
                validate_definitions(parameter_edits({"parameters":[p]},{p["name"]:value}),
                                     expected_version=cfg["model"]["expected_version"])
        definitions=cfg["observations"]
        if len(definitions)!=1 or definitions[0].get("metric","NSE")!="NSE":
            raise ValueError("Initial SUFI-2 adapter supports one observation series with NSE")
        definition=definitions[0]
        if definition.get("frequency")!="monthly" or cfg.get("frequency")!="monthly":
            raise ValueError("Initial verified SUFI-2 adapter supports monthly discharge only")
        if definition["target"]["variable"]!="flo_out" or definition["unit"].replace("³","3").replace("^","")!="m3/s":
            raise ValueError("Initial SUFI-2 adapter supports discharge flo_out in m3/s")
        if any(not 3<=n<=30000 for n in counts[definition["id"]].values()):
            raise ValueError("SUFI-2 requires 3..30000 observed values per period")
        if not 1<=search["seed"]<2147483647-requirements["iterations"]:
            raise ValueError("SUFI-2 requires a positive 32-bit seed with room for each iteration")
        executable_dir=Path(root)/options.get("executable_dir",str(DEFAULT_EXECUTABLE_DIR))
        for program in PROGRAMS:
            if not (executable_dir/program).is_file():raise FileNotFoundError(executable_dir/program)
    if search["method"]=="spe_sufi2":
        # The hand-written range update never leaves the fixed hard bounds,
        # mirroring the classic SUFI-2 coordinate convention.
        for p in cfg["parameters"]:
            low=p.get("coordinate_min",p["lower"]);high=p.get("coordinate_max",p["upper"])
            if not (math.isfinite(low) and math.isfinite(high) and low<=p["lower"]<p["upper"]<=high):
                raise ValueError("SPE coordinate bounds must contain the initial search interval")
    analysis=cfg.get("analysis",{})
    if type(analysis.get("workers",1)) is not int or analysis.get("workers",1)<1:raise ValueError("analysis.workers must be positive")
    required=0
    sensitivity=analysis.get("sensitivity",{})
    if sensitivity.get("enabled"):
        from sensitivity_uncertainty import generate_design
        design=generate_design(cfg["parameters"],sensitivity["method"],sensitivity,sensitivity.get("seed",search["seed"]))
        required=len(design["values"])
    uncertainty=analysis.get("uncertainty",{})
    if uncertainty.get("enabled") and search["method"] not in ("spotpy_dream","swatcup_sufi2","spe_sufi2"):
        if not math.isfinite(uncertainty.get("threshold",float("nan"))):raise ValueError("An explicit calibration-only behavioral threshold is required")
        if type(uncertainty.get("min_behavioral",20)) is not int or uncertainty.get("min_behavioral",20)<2:raise ValueError("min_behavioral must be at least 2")
        if search["method"]!="glue_lhs" and (type(uncertainty.get("ensemble_size",0)) is not int or uncertainty.get("ensemble_size",0)<2):
            raise ValueError("DDS/SCE uncertainty requires a separate prior ensemble_size")
    return {"observations":counts,"observation_kind":"multiple_explicit_series",
            "planned_search_trials":search["repetitions"],"sensitivity_model_calls":required,
            "uncertainty_note":"Validation is never used to select ensemble members; accepted members require full-period simulations."}


class ResearchWorkflow(Workflow):
    # Joint evaluation reads explicit per-observation targets; reservoir or
    # interior outlets only affect the auxiliary outlet series.
    requires_channel_outlet = False

    def __init__(self,job,state,cancel,pause_after=0):
        self.job,self.state,self.cancel=Path(job),state,cancel
        self.root=Path(state["workspace_root"]);self.cfg=read_json(self.job/"config.resolved.json")
        self.pause_after,self.new_trials=pause_after,0
        self.references={}
        for definition in self.cfg["observations"]:
            if "original_path" not in definition:
                source=self.root/definition["path"];snapshot=self.job/f"observation_{definition['id']}.csv"
                if not snapshot.exists():shutil.copy2(source,snapshot)
                definition.update(original_path=str(source),path=str(snapshot))
            self.references[definition["id"]]=read_observation(definition,self.root)
        self.cfg["observation"]=self.cfg["observations"][0]
        save_json(self.job/"config.resolved.json",self.cfg)

    def make_spec(self,values,work,validation=False):
        spec=super().make_spec(values,work,validation)
        spec["output_targets"]=target_specs(self.cfg["observations"])
        return spec

    def series(self,entry):
        result=read_json(entry["result_path"])
        return {identity:read_target_csv(item["path"]) for identity,item in result["target_csvs"].items()}

    def execute_entry(self,entry,validation=False):
        directory=self.job/"trials"/entry["trial_id"]
        period=self.cfg["periods"]["validation" if validation else "calibration"]
        for result_path in sorted(directory.glob("attempt_*/trial_result.json"),reverse=True):
            previous=read_json(result_path)
            if previous.get("status")=="completed" and read_json(result_path.parent/"trial_spec.json")==self.make_spec(entry["values"],result_path.parent,validation):
                payload={"result_path":str(result_path)}
                metrics=joint_score(self.cfg["observations"],self.references,self.series(payload),period)
                return {"status":"completed","attempt":int(result_path.parent.name.split("_")[-1]),**payload,
                        "outlet_csv":previous["outlet_csv"],"metrics":metrics,"elapsed_seconds":previous["elapsed_seconds"]}
        attempt=entry.get("attempt",0)+1
        while (directory/f"attempt_{attempt:02d}").exists():attempt+=1
        work=directory/f"attempt_{attempt:02d}";directory.mkdir(parents=True,exist_ok=True)
        spec=self.make_spec(entry["values"],work,validation);path=directory/f"spec_{attempt:02d}.json"
        save_json(path,spec)
        self.batch(directory,f"trial_{attempt:02d}.bat",[sys.executable,"-X","utf8",self.root/"scripts/swatplus_trial.py","--spec",path],spec["timeout_seconds"]+60)
        result=read_json(work/"trial_result.json")
        if result["status"]!="completed":raise RuntimeError(result)
        payload={"result_path":str(work/"trial_result.json")}
        metrics=joint_score(self.cfg["observations"],self.references,self.series(payload),period)
        return {"status":"completed","attempt":attempt,**payload,"outlet_csv":result["outlet_csv"],
                "metrics":metrics,"elapsed_seconds":result["elapsed_seconds"]}

    def best(self):
        items=[t for t in self.state["trials"] if t.get("status")=="completed"]
        if self.cfg["search"]["method"]!="spotpy_dream":items=[self.state["baseline"]]+items
        return max(items,key=lambda e:e["metrics"]["JOINT"])

    def write_history(self):
        rows=[];best=-float("inf")
        for entry in [self.state["baseline"]]+self.state["trials"]:
            if entry.get("status")!="completed":continue
            score=entry["metrics"]["JOINT"];best=max(best,score)
            row={"trial_id":entry["trial_id"],"round":entry.get("round",1),"score":score,"best_so_far":best,**entry["values"]}
            for identity,metrics in entry["metrics"]["by_observation"].items():
                row.update({f"{identity}_{k}":metrics.get(k) for k in ["score","NSE","KGE","RMSE","n"]})
            rows.append(row)
        fields=list(dict.fromkeys(k for row in rows for k in row))
        with (self.job/"trials.csv").open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.DictWriter(stream,fields);writer.writeheader();writer.writerows(rows)
        return rows

    def run_independent(self,entries,validation=False):
        pending=[e for e in entries if e.get("status")!="completed"]
        workers=self.cfg.get("analysis",{}).get("workers",1)
        while pending:
            if self.cancel.is_set():raise CancelledError("用户停止任务")
            chunk=pending[:workers]
            for e in chunk:e["status"]="running"
            self.save()
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures={pool.submit(self.execute_entry,e,validation):e for e in chunk}
                for future in concurrent.futures.as_completed(futures):
                    entry=futures[future]
                    try:entry.update(future.result());entry.pop("error",None)
                    except Exception as exc:entry.update(status="failed",error=str(exc));self.save();self.cancel.set();raise
                    self.save();self.message(f"{entry['trial_id']} 完成：JOINT={entry['metrics']['JOINT']:.4f}")
            pending=[e for e in pending if e.get("status")!="completed"]

    def sensitivity(self):
        settings=self.cfg.get("analysis",{}).get("sensitivity",{})
        if not settings.get("enabled"):return
        from sensitivity_uncertainty import generate_design,analyze_sensitivity
        self.state["phase"]="sensitivity"
        data=self.state.setdefault("sensitivity",{})
        if "design" not in data:
            data["design"]=generate_design(self.cfg["parameters"],settings["method"],settings,settings.get("seed",self.cfg["search"]["seed"]))
            data["trials"]=[{"trial_id":f"sens_{i+1:05d}","values":v,"status":"pending"} for i,v in enumerate(data["design"]["values"])]
        self.save();self.message(f"{settings['method']}敏感性分析：{len(data['trials'])}组设计")
        self.run_independent(data["trials"])
        data["result"]={"joint":analyze_sensitivity(data["design"],[e["metrics"]["JOINT"] for e in data["trials"]]),
                        "by_observation":{d["id"]:analyze_sensitivity(data["design"],[e["metrics"]["by_observation"][d["id"]]["score"] for e in data["trials"]]) for d in self.cfg["observations"]}}
        save_json(self.job/"sensitivity.json",data["result"]);self.save()

    def run_search(self):
        from mature_search import run_search,engine_info,SearchPaused
        self.state.setdefault("baseline",{"trial_id":"baseline_calibration","values":{},"round":0,"status":"pending"})
        self.one(self.state["baseline"]);self.sensitivity()
        self.state["phase"]="calibration";search=self.cfg["search"];info=engine_info(search["method"])
        if self.state.get("search_engine",info)!=info:raise ValueError("Search engine changed since the recorded run")
        self.state["search_engine"]=info;self.save()
        if search["method"]=="swatcup_sufi2":
            return self.run_sufi2_search()
        if search["method"]=="glue_lhs" and not self.pause_after:
            # LHS members are independent (unlike an adaptive DDS/SCE chain).
            # Generate the identical published-library design before dispatch.
            design=[]
            run_search("glue_lhs",self.cfg["parameters"],search["repetitions"],search["seed"],lambda v:design.append(v) or 0.0)
            previous=sum(t.get("status")=="completed" for t in self.state["trials"])
            for i,values in enumerate(design):
                if i<len(self.state["trials"]):
                    if self.state["trials"][i]["values"]!=values:raise ValueError("LHS replay differs from recorded design")
                else:self.state["trials"].append({"trial_id":f"search_{i+1:05d}","values":values,"round":1,"status":"pending"})
            self.run_independent(self.state["trials"]);self.write_history()
            self.state["optimizer_result"]={"method":"glue_lhs","evaluations":len(design),"budget":search["repetitions"],
                                            "best_score":max(t["metrics"]["JOINT"] for t in self.state["trials"]),
                                            "stop_reason":"budget","status":"completed","converged":False}
            self.state["replayed_search_trials"]=previous
            self.state["selected"]={k:self.best()[k] for k in ["trial_id","values","metrics"]}
            self.state["selection_basis"]="calibration_observations_only"
            save_json(self.job/"best_parameters.json",self.state["selected"]);self.save();return True
        index=0;replayed=0
        def evaluate(values):
            nonlocal index,replayed
            if self.cancel.is_set():raise CancelledError("用户停止任务")
            index+=1
            if index<=len(self.state["trials"]):
                entry=self.state["trials"][index-1]
                if entry["values"]!=values:raise ValueError(f"Search replay differs at {index}; settings/library changed")
                if entry.get("status")=="completed":replayed+=1;return entry["metrics"]["JOINT"]
            else:
                if self.pause_after and self.new_trials>=self.pause_after:raise SearchPaused()
                entry={"trial_id":f"search_{index:05d}","values":values,"round":1,"status":"pending"}
                self.state["trials"].append(entry)
            if self.pause_after and self.new_trials>=self.pause_after:raise SearchPaused()
            self.one(entry);self.new_trials+=1;self.write_history()
            self.message(f"{entry['trial_id']} 完成：JOINT={entry['metrics']['JOINT']:.5f}")
            return entry["metrics"]["JOINT"]
        options={**search.get("options",{}),"r":search.get("r",0.2)}
        if search["method"]=="spotpy_dream":options["objective_kind"]="gaussian_log_likelihood"
        try:self.state["optimizer_result"]=run_search(search["method"],self.cfg["parameters"],search["repetitions"],search["seed"],evaluate,options,should_stop=self.cancel.is_set)
        except SearchPaused:
            if self.cancel.is_set():raise CancelledError("用户停止任务")
            self.state.update(status="paused",phase="calibration");self.save();return False
        # Round-structured methods record which evaluation belongs to which
        # round; trials.csv and the report must show the real round numbers.
        trial_rounds=self.state["optimizer_result"].pop("trial_rounds",None)
        if trial_rounds:
            for entry,round_number in zip(self.state["trials"],trial_rounds):entry["round"]=round_number
            self.write_history()
        self.state["replayed_search_trials"]=replayed
        self.state["selected"]={k:self.best()[k] for k in ["trial_id","values","metrics"]}
        self.state["selection_basis"]="calibration_observations_only"
        save_json(self.job/"best_parameters.json",self.state["selected"]);self.save();return True

    def sufi2_bridge(self,folder):
        from sufi2_bridge import Sufi2Bridge,DEFAULT_EXECUTABLE_DIR
        options=self.cfg["search"].get("options",{})
        return Sufi2Bridge(executable_dir=self.root/options.get("executable_dir",str(DEFAULT_EXECUTABLE_DIR)),
                           work_dir=folder,timeout=options.get("kernel_timeout_seconds",60))

    def sufi2_series(self,entries,period):
        """Pair every member against one common, explicit observation schedule."""
        identity=self.cfg["observations"][0]["id"]
        reference=self.references[identity]
        dates=sorted(d for d in reference if period["start"]<=d<=period["end"])
        output=[]
        for entry in entries:
            values=self.series(entry)[identity]
            if any(date not in values for date in dates):
                raise ValueError("SUFI-2 ensemble member misses an observed date")
            output.append([values[date] for date in dates])
        return dates,[reference[date] for date in dates],output

    def run_sufi2_search(self):
        """Original kernels with explicit round plans and calibration-only review."""
        from mature_search import validate_search_settings
        from sufi2_control import regression_sensitivity, screening_advice, round_diagnostics
        search=self.cfg["search"];options=search.get("options",{})
        requirements=validate_search_settings(search["method"],self.cfg["parameters"],search["repetitions"],options)
        data=self.state.setdefault("sufi2",{"rounds":[]})
        data.setdefault("round_sizes",requirements["round_sizes"])
        original=[{**p,"coordinate_min":p.get("coordinate_min",p["lower"]),
                    "coordinate_max":p.get("coordinate_max",p["upper"])} for p in self.cfg["parameters"]]
        data.setdefault("initial_parameters",original)
        if data["initial_parameters"]!=original:
            raise ValueError("SUFI-2 initial parameter definitions/hard bounds changed after sampling")
        if data.get("pending_review"):
            self.state.update(status="paused",phase="sufi2_review");self.save()
            self.message("SUFI-2 等待本轮审阅；使用 resume --sufi-review review.json 提交决定");return False
        replayed=0
        for index,count in enumerate(data["round_sizes"]):
            if self.cancel.is_set():raise CancelledError("用户停止任务")
            definitions=[dict(p) for p in original]
            frozen={}
            if index:
                previous=data["rounds"][index-1]
                decision=previous.get("next_decision")
                if decision and decision["action"]=="finish":
                    break
                # Read old jobs without changing their previously sampled design.
                ranges=decision["ranges"] if decision else previous["result"]["new_ranges"]
                active=decision["active_parameters"] if decision else [p["name"] for p in previous["parameters"]]
                frozen=decision["frozen_parameters"] if decision else previous.get("frozen_parameters",{})
                definitions=[p for p in definitions if p["name"] in active]
                for p in definitions:p["lower"],p["upper"]=ranges[p["name"]]
            if count<max(8,len(definitions)+2):
                raise ValueError("Next SUFI-2 round budget is too small for its active parameter count")
            directory=self.job/"sufi2"/f"iteration_{index+1:02d}"
            bridge=self.sufi2_bridge(directory)
            if index<len(data["rounds"]):
                current=data["rounds"][index]
                if current["parameters"]!=definitions or len(current["trial_ids"])!=count or current.get("frozen_parameters",{})!=frozen:
                    raise ValueError("SUFI-2 saved iteration differs from requested settings")
            else:
                # A previously interrupted sample invocation has no OUT yet and
                # can regenerate this deterministic vendor design with the same seed.
                values=bridge.sample(definitions,count,search["seed"]+index)
                entries=[{"trial_id":f"sufi_r{index+1:02d}_{j+1:05d}","values":{**frozen,**v},
                          "round":index+1,"status":"pending"} for j,v in enumerate(values)]
                current={"iteration":index+1,"parameters":definitions,
                         "frozen_parameters":dict(frozen),
                         "trial_ids":[e["trial_id"] for e in entries],"directory":str(directory)}
                self.state["trials"].extend(entries);data["rounds"].append(current);self.save()
            lookup={e["trial_id"]:e for e in self.state["trials"]}
            entries=[lookup[key] for key in current["trial_ids"]]
            for entry in entries:
                if self.cancel.is_set():raise CancelledError("用户停止任务")
                if entry.get("status")=="completed":replayed+=1;continue
                if self.pause_after and self.new_trials>=self.pause_after:
                    self.state.update(status="paused",phase="calibration");self.save();return False
                succeeded=False
                for retry in range(options.get("retries",1)+1):
                    if self.cancel.is_set():raise CancelledError("用户停止任务")
                    try:
                        self.one(entry);succeeded=True;break
                    except CancelledError:
                        raise
                    except Exception as exc:
                        entry.setdefault("failure_history",[]).append({
                            "error":str(exc),"recorded_at":dt.datetime.now().isoformat(timespec="seconds"),
                            "retry_in_resume":retry})
                        self.save()
                if not succeeded:
                    data["execution_issue"]={"iteration":index+1,"trial_id":entry["trial_id"],
                                             "reason":"Model trial failed after bounded retries; no sample was silently discarded"}
                    self.state.update(status="paused",phase="calibration");self.save()
                    self.message(f"{entry['trial_id']} 重试后仍失败，已暂停并保留失败记录；修复原因后可续跑");return False
                data.pop("execution_issue",None)
                self.new_trials+=1;self.write_history()
                self.message(f"SUFI-2 第{index+1}轮 {entry['trial_id']}：NSE={entry['metrics']['JOINT']:.5f}")
            if "result" not in current:
                if self.cancel.is_set():raise CancelledError("用户停止任务")
                dates,observed,simulated=self.sufi2_series(entries,self.cfg["periods"]["calibration"])
                try:
                    result=bridge.analyze(definitions,[e["values"] for e in entries],observed,simulated,
                                          dates=dates,threshold=options.get("behavioral_threshold",0.5))
                except Exception as exc:
                    data["execution_issue"]={"iteration":index+1,"reason":str(exc),"stage":"original_statistics"}
                    self.state.update(status="paused",phase="calibration");self.save()
                    self.message(f"SUFI-2 原版统计异常，已保留本轮设计及结果：{exc}");return False
                if any(abs(score-e["metrics"]["JOINT"])>5e-5*max(1,abs(e["metrics"]["JOINT"]))
                       for score,e in zip(result["nse"],entries)):
                    raise ValueError("SUFI-2 original NSE differs from workflow calibration scores")
                current["result"]=result;self.save()
                self.message(f"SUFI-2 第{index+1}轮统计完成；最佳NSE={result['best_nse']:.5f}，保存原生95PPU和下一轮范围")
            if "sensitivity" not in current:
                current["sensitivity"]=regression_sensitivity(definitions,[e["values"] for e in entries],
                                                               [e["metrics"]["JOINT"] for e in entries])
                current["screening"]=screening_advice(definitions,current["sensitivity"],
                                                       current["result"]["best_parameters"],options,index+1)
                current["diagnostics"]=round_diagnostics(current,entries,data["rounds"][index-1] if index else None,
                                                         history=data["rounds"][:index],options=options)
                save_json(directory/"round_diagnostics.json",{
                    key:current[key] for key in ("sensitivity","screening","diagnostics")})
                self.save()
            if index+1<len(data["round_sizes"]) and "next_decision" not in current:
                next_count=data["round_sizes"][index+1]
                # A resumed legacy job may already have a later design. Do not
                # introduce a new screening decision into its recorded past.
                legacy_next=index+1<len(data["rounds"])
                advice=current["screening"];settings=options.get("screening",{})
                active=[p["name"] for p in definitions];next_frozen=dict(frozen)
                if settings.get("mode","suggest")=="auto" and advice["eligible"] and not legacy_next:
                    active=advice["suggested_retained"];next_frozen.update(advice["suggested_frozen"])
                decision={"action":"continue","active_parameters":active,"frozen_parameters":next_frozen,
                          "ranges":{name:current["result"]["new_ranges"][name] for name in active},
                          "next_sample_count":next_count,"source":"automatic_rule"}
                reasons=[]
                if not legacy_next:
                    if options.get("continuation","auto")=="review":reasons.append("round_review")
                    if advice["suggested_frozen"] and settings.get("mode","suggest")=="suggest":reasons.append("screening_suggestion")
                    if next_count<max(8,len(active)+2):reasons.append("insufficient_next_budget")
                    reasons.extend(current["diagnostics"].get("blocking_reasons",[]))
                if reasons:
                    data["pending_review"]={
                        "after_round":index+1,"reason":"请查看本轮率定和筛选建议，再决定下一轮",
                        "reasons":reasons,"next_sample_count":next_count,
                        "previous_ranges":{p["name"]:[p["lower"],p["upper"]] for p in definitions},
                        "raw_suggested_ranges":current["result"]["raw_new_ranges"],
                        "next_ranges":current["result"]["new_ranges"],
                        "active_parameters":[p["name"] for p in definitions],"frozen_parameters":dict(frozen),
                        "hard_bounds":{p["name"]:[p["coordinate_min"],p["coordinate_max"]] for p in original},
                        "sensitivity":current["sensitivity"],"screening":advice,"diagnostics":current["diagnostics"]}
                    self.state.update(status="paused",phase="sufi2_review");self.save()
                    self.message("本轮结束，等待审阅下一轮范围、参数保留和运行组数");return False
                current["next_decision"]=decision;self.save()
        chosen=self.best()
        self.state["selected"]={k:chosen[k] for k in ("trial_id","values","metrics")}
        self.state["selection_basis"]="calibration_observations_only"
        self.state["replayed_search_trials"]=replayed
        self.state["optimizer_result"]={"method":"swatcup_sufi2","evaluations":len(self.state["trials"]),
            "budget":sum(data["round_sizes"]),"iterations":len(data["rounds"]),"round_sizes":data["round_sizes"],
            "samples_per_iteration":data["round_sizes"][0] if len(set(data["round_sizes"]))==1 else None,
            "best_score":chosen["metrics"]["JOINT"],"status":"completed","converged":False,
            "stop_reason":"user_finished" if data.get("finished_by_review") else "iteration_budget",
            "final_iteration_member_ids":data["rounds"][-1]["trial_ids"],
            "frozen_parameters":data["rounds"][-1].get("frozen_parameters",{}),
            "note":"Original classic SWAT-CUP kernels with SWAT+ adapter; selected solution uses calibration only. Small iteration budget does not establish convergence."}
        save_json(self.job/"best_parameters.json",self.state["selected"]);self.save();return True

    def apply_sufi2_review(self,payload):
        """Accept only the pending round's future decision, never past designs."""
        from sufi2_control import validate_review
        if self.cfg["search"]["method"]!="swatcup_sufi2":
            raise ValueError("--sufi-review applies only to SUFI-2 tasks")
        data=self.state.get("sufi2",{});pending=data.get("pending_review")
        if self.state.get("status")!="paused" or not pending:
            raise ValueError("SUFI-2 task must be paused with a pending round review")
        if pending["after_round"]!=len(data["rounds"]):
            raise ValueError("Cannot alter a round after its next design already exists")
        current=data["rounds"][-1]
        if "result" not in current or "selected" in self.state or self.state.get("evaluation"):
            raise ValueError("Review is allowed only after completed calibration statistics and before validation")
        decision=validate_review(payload,pending,data["initial_parameters"],current,pending["next_sample_count"])
        if decision["action"]=="finish":
            data["finished_by_review"]=True
        else:
            data["round_sizes"][pending["after_round"]]=decision["next_sample_count"]
        current["next_decision"]=decision
        current["review_input"]=payload
        save_json(Path(current["directory"])/"review.json",payload)
        data.pop("pending_review")
        self.save()

    def uncertainty(self):
        from mature_search import run_search
        from sensitivity_uncertainty import uncertainty_envelope
        settings=self.cfg.get("analysis",{}).get("uncertainty",{})
        if not settings.get("enabled"):return
        if self.cfg["search"]["method"]=="swatcup_sufi2":
            return self.sufi2_uncertainty()
        if self.cfg["search"]["method"]=="spe_sufi2":
            return self.spe_uncertainty()
        self.state["phase"]="uncertainty";data=self.state.setdefault("uncertainty",{});self.save()
        method=self.cfg["search"]["method"]
        posterior=method=="spotpy_dream"
        if posterior:
            optimizer=self.state["optimizer_result"]
            if not optimizer.get("posterior_usable"):
                data.update(status="not_converged",note="DREAM chain diagnostics do not support a usable posterior; no posterior interval reported.")
                save_json(self.job/"uncertainty.json",data);self.save();return
            members=[self.state["trials"][x["evaluation_index"]] for x in optimizer["posterior_samples"]]
        else:
            if method=="glue_lhs":candidates=self.state["trials"]
            else:
                if "trials" not in data:
                    values=[]
                    run_search("glue_lhs",self.cfg["parameters"],settings["ensemble_size"],settings.get("seed",self.cfg["search"]["seed"]+1000),lambda v:values.append(v) or 0.0)
                    data["trials"]=[{"trial_id":f"unc_{i+1:05d}","values":v,"status":"pending"} for i,v in enumerate(values)]
                self.run_independent(data["trials"]);candidates=data["trials"]
            members=[e for e in candidates if e["metrics"]["JOINT"]>=settings["threshold"]]
        minimum=settings.get("min_behavioral",20)
        if len(members)<minimum:
            data.update(status="insufficient_members",n_behavioral=len(members),minimum=minimum,note="Too few acceptable calibration members; no uncertainty band fabricated.")
            save_json(self.job/"uncertainty.json",data);self.save();return
        data["member_ids"]=[e["trial_id"] for e in members]
        validation=data.setdefault("validation",{})
        for e in members:
            validation.setdefault(e["trial_id"],{"trial_id":"env_"+e["trial_id"],"values":e["values"],"status":"pending"})
        self.run_independent(list(validation.values()),True)
        series=[self.series(validation[e["trial_id"]]) for e in members]
        for entry,extended in zip(members,series):
            initial=self.series(entry)
            if any(extended[identity].get(date)!=value for identity,values in initial.items() for date,value in values.items()):
                raise ValueError("Full-period ensemble run changed earlier calibration outputs")
        output={"status":"computed","kind":"posterior_parameter_predictive" if posterior else "behavioral_parameter_ensemble",
                "n_members":len(members),"selection_period":"calibration_only","interval_percent":[2.5,97.5],
                "n_candidates":len(self.state["trials"]) if posterior else len(candidates),
                "unique_member_model_runs":len(validation),
                "calibration_outputs_unchanged":True,
                "note":"Parameter-only interval; not all predictive/measurement/model-structure uncertainty. No SUFI-2 range update is performed.","by_observation":{}}
        for definition in self.cfg["observations"]:
            identity=definition["id"];output["by_observation"][identity]={}
            for label,period in self.cfg["periods"].items():
                reference={d:v for d,v in self.references[identity].items() if period["start"]<=d<=period["end"]}
                if posterior:
                    envelope=uncertainty_envelope([s[identity] for s in series],reference,min_behavioral=minimum,
                                                  ensemble_kind="dream_posterior",diagnostics=self.state["optimizer_result"])
                else:
                    envelope=uncertainty_envelope([s[identity] for s in series],reference,[e["metrics"]["JOINT"] for e in members],settings["threshold"],min_behavioral=minimum)
                output["by_observation"][identity][label]=envelope
        data.update(status="computed",result=output);save_json(self.job/"uncertainty.json",output);self.save()

    def sufi2_uncertainty(self):
        """Extend exactly the final calibration ensemble without validation selection."""
        self.state["phase"]="uncertainty"
        data=self.state.setdefault("uncertainty",{})
        current=self.state["sufi2"]["rounds"][-1]
        lookup={e["trial_id"]:e for e in self.state["trials"]}
        members=[lookup[key] for key in current["trial_ids"]]
        data["member_ids"]=list(current["trial_ids"])
        validation=data.setdefault("validation",{})
        for e in members:
            record=validation.setdefault(e["trial_id"],{"trial_id":"env_"+e["trial_id"],
                                                       "values":e["values"],"status":"pending"})
            if record["values"]!=e["values"]:raise ValueError("SUFI-2 validation ensemble changed")
        self.save();self.run_independent(list(validation.values()),True)
        for e in members:
            before=self.series(e);after=self.series(validation[e["trial_id"]])
            if any(after[identity].get(date)!=value for identity,series in before.items() for date,value in series.items()):
                raise ValueError("Full-period SUFI-2 ensemble changed earlier calibration outputs")
        identity=self.cfg["observations"][0]["id"]
        cal_dates,cal_obs,cal_sims=self.sufi2_series(members,self.cfg["periods"]["calibration"])
        val_entries=[validation[e["trial_id"]] for e in members]
        dates,observed,simulated=self.sufi2_series(val_entries,self.cfg["periods"]["validation"])
        selected_series=self.series(self.state["evaluation"]["best"])[identity]
        bridge=self.sufi2_bridge(self.job/"sufi2"/"validation")
        fixed=bridge.prediction_interval(current["parameters"],[e["values"] for e in members],
            observed,simulated,dates=dates,best_index=current["result"]["best_simulation_number"]-1,
            selected_simulation=[selected_series[date] for date in dates],
            selected_parameters={p["name"]:self.state["selected"]["values"][p["name"]]
                                 for p in current["parameters"] if p["name"] in self.state["selected"]["values"]})
        def envelope(interval,days,obs,sims):
            return {**interval,"rows":[{"date":d,"observed":v,"lower":interval["lower"][i],
                     "upper":interval["upper"][i],"median":statistics.median(row[i] for row in sims),
                     "mean":interval["mean"][i]} for i,(d,v) in enumerate(zip(days,obs))]}
        # The bridge returns the original all-sample interval; no validation
        # threshold, goal-function selection or new-range routine is requested.
        validation_interval=fixed.get("all_interval",fixed)
        output={"status":"computed","kind":"sufi2_final_iteration_ensemble",
                "n_members":len(members),"n_candidates":len(members),
                "selection_period":"calibration_only","interval_percent":[2.5,97.5],
                "unique_member_model_runs":len(validation),"calibration_outputs_unchanged":True,
                "iteration":current["iteration"],"member_ids":data["member_ids"],
                "active_parameters":[p["name"] for p in current["parameters"]],
                "frozen_parameters":current.get("frozen_parameters",{}),
                "selected_complete_parameters":self.state["selected"]["values"],
                "behavioral_calibration_count":current["result"]["behavioral_count"],
                "behavioral_threshold":current["result"]["behavioral_threshold"],
                "note":"Original classic SUFI-2 95PPU from the final iteration only; the identical full ensemble is propagated to validation. Validation neither selects members nor updates bounds. Zero measurement error; not SWATplus-CUP/SPE.",
                "by_observation":{identity:{
                    "calibration":envelope(current["result"]["all_interval"],cal_dates,cal_obs,cal_sims),
                    "validation":envelope(validation_interval,dates,observed,simulated)}}}
        data.update(status="computed",result=output);save_json(self.job/"uncertainty.json",output);self.save()

    def spe_uncertainty(self):
        """Hand-written SUFI-2-style 95PPU from the fixed final SPE round.

        The complete final-round ensemble is propagated to validation without
        member selection or range updates. The all-simulation band follows the
        SWAT-CUP manual 2.5%/97.5% quantile definition; a calibration-only
        behavioral subset is reported separately when at least two members
        pass the declared threshold. P-factor applies the manual's declared
        observation-error allowance and R-factor uses the classic program's
        population observed-SD convention.
        """
        from sensitivity_uncertainty import uncertainty_envelope
        self.state["phase"]="uncertainty"
        data=self.state.setdefault("uncertainty",{})
        rounds=self.state["optimizer_result"]["rounds"]
        current=rounds[-1]
        members=[self.state["trials"][index] for index in current["member_indices"]]
        data["member_ids"]=[e["trial_id"] for e in members]
        validation=data.setdefault("validation",{})
        for e in members:
            record=validation.setdefault(e["trial_id"],{"trial_id":"env_"+e["trial_id"],
                                                        "values":e["values"],"status":"pending"})
            if record["values"]!=e["values"]:raise ValueError("SPE validation ensemble changed")
        self.save();self.run_independent(list(validation.values()),True)
        for e in members:
            before=self.series(e);after=self.series(validation[e["trial_id"]])
            if any(after[identity].get(date)!=value for identity,values in before.items() for date,value in values.items()):
                raise ValueError("Full-period SPE ensemble changed earlier calibration outputs")
        options=self.cfg["search"].get("options",{})
        percent=options.get("measurement_error_percent",0.0)
        threshold=options.get("behavioral_threshold",.5)
        output={"status":"computed","kind":"spe_final_iteration_ensemble",
                "n_members":len(members),
                "n_candidates":sum(len(r["member_indices"]) for r in rounds),
                "selection_period":"calibration_only","interval_percent":[2.5,97.5],
                "unique_member_model_runs":len(validation),"calibration_outputs_unchanged":True,
                "iteration":current["iteration"],"member_ids":data["member_ids"],
                "measurement_error_percent":percent,
                "note":"Hand-written SUFI-2-style 95PPU: 2.5%/97.5% quantiles of the complete final-round "
                       "ensemble (P-factor with the manual's observation-error allowance, R-factor with the "
                       "classic population observed-SD convention). Validation neither selects members nor "
                       "updates ranges. Parameter-only band; not classic SWAT-CUP kernels, not SWATplus-CUP/SPE.",
                "by_observation":{},"behavioral_bands":{}}
        for definition in self.cfg["observations"]:
            identity=definition["id"];output["by_observation"][identity]={}
            for label,period in self.cfg["periods"].items():
                reference={d:v for d,v in self.references[identity].items() if period["start"]<=d<=period["end"]}
                sources=[self.series(validation[e["trial_id"]])[identity] for e in members]
                output["by_observation"][identity][label]=uncertainty_envelope(
                    sources,reference,ensemble_kind="sufi2_ensemble",min_behavioral=2,
                    measurement_error_percent=percent,observed_std_convention="population")
            cal_period=self.cfg["periods"]["calibration"]
            cal_reference={d:v for d,v in self.references[identity].items()
                           if cal_period["start"]<=d<=cal_period["end"]}
            cal_sources=[self.series(validation[e["trial_id"]])[identity] for e in members]
            if sum(1 for e in members if e["metrics"]["JOINT"]>=threshold)>=2:
                output["behavioral_bands"][identity]=uncertainty_envelope(
                    cal_sources,cal_reference,[e["metrics"]["JOINT"] for e in members],threshold,
                    min_behavioral=2,measurement_error_percent=percent,observed_std_convention="population")
            else:
                output["behavioral_bands"][identity]={"status":"skipped",
                    "note":f"Fewer than two members reach the declared behavioral threshold {threshold:g}; "
                           "no behavioral band is fabricated."}
        data.update(status="computed",result=output);save_json(self.job/"uncertainty.json",output);self.save()

    def evaluate_and_report(self):
        self.state["phase"]="fixed_parameter_validation";evaluation=self.state.setdefault("evaluation",{})
        evaluation.setdefault("baseline",{"trial_id":"baseline_validation","values":{},"status":"pending"})
        evaluation.setdefault("best",{"trial_id":"selected_validation","values":self.state["selected"]["values"],"status":"pending"})
        if evaluation["best"]["values"]!=self.state["selected"]["values"]:raise ValueError("Validation parameters changed")
        self.one(evaluation["baseline"],True);self.one(evaluation["best"],True)
        simulations={key:self.series(e) for key,e in evaluation.items()}
        chosen=self.state["baseline"] if self.state["selected"]["trial_id"]==self.state["baseline"]["trial_id"] else next(e for e in self.state["trials"] if e["trial_id"]==self.state["selected"]["trial_id"])
        for old,new in [(self.series(self.state["baseline"]),simulations["baseline"]),(self.series(chosen),simulations["best"])]:
            for identity,values in old.items():
                if any(new[identity].get(d)!=v for d,v in values.items()):raise ValueError("Extending validation changed calibration-period output")
        metrics={key:{label:joint_score(self.cfg["observations"],self.references,series,period) for label,period in self.cfg["periods"].items()} for key,series in simulations.items()}
        self.state.update(metrics=metrics,validation_fixed_parameters=True,calibration_output_unchanged=True);self.save()
        self.uncertainty();self.state["phase"]="report";self.save()
        report={"title":self.cfg["title"],"method":self.cfg["search"]["method"],"observations":self.cfg["observations"],
                "periods":self.cfg["periods"],"metrics":metrics,"best_parameters":self.state["selected"]["values"],
                "references":self.references,"simulations":simulations,"trials":self.write_history(),
                "sensitivity":self.state.get("sensitivity",{}).get("result"),
                "uncertainty":self.state.get("uncertainty",{}).get("result",{k:v for k,v in self.state.get("uncertainty",{}).items() if k in ["status","note","minimum","n_behavioral"]}),
                "optimizer":self.state.get("optimizer_result",{}),"search_engine":self.state["search_engine"]}
        report["engine_info"]=self.cfg.get("engine_info",{})
        save_json(self.job/"research_report_data.json",report)
        from research_report import render_report
        render_report(self.job)
        self.state.update(status="completed",phase="completed");self.save();self.message(f"综合分析完成：{self.job/'report.md'}")
