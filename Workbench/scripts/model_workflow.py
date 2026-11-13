"""Build/run without requiring an optimizer or observations; prepare a Toolbox handoff."""
import datetime as dt
import json
from pathlib import Path
from swatplus_workflow import Workflow, read_json, save_json


def validate_model_config(cfg, root, check_inputs=True):
    root = Path(root)
    if cfg["model"]["expected_version"] != "62.0.1":
        raise ValueError("建模交接流程当前适配 SWAT+ 62.0.1")
    sim = cfg["simulation"]
    for key in ("start_year", "end_year", "warmup_years"):
        if type(sim.get(key)) is not int:
            raise ValueError("模拟年份和预热年数必须为整数")
    dt.date(sim["start_year"], 1, 1); dt.date(sim["end_year"], 12, 31)
    if not 0 <= sim["warmup_years"] <= sim["end_year"] - sim["start_year"]:
        raise ValueError("预热后至少需要一个输出年")
    if cfg.get("frequency") not in ("monthly", "daily"):
        raise ValueError("输出频率须为monthly或daily")
    build = cfg.get("build", {})
    if not check_inputs:
        inputs = {"status":"checked separately by setup"}
    elif build.get("enabled"):
        from build_and_run import load_pipeline
        from build_preflight import check_paths
        base, pipeline, stages = load_pipeline(root / build["pipeline_config"])
        if pipeline.get("backend") != "qgis344":
            raise ValueError("新的建模入口使用QGIS344流程")
        inputs = check_paths(stages["spatial"], stages["editor"], base, sim)
    else:
        from swatplus_trial import check_clean_source
        inputs = check_clean_source(root / cfg["model"]["input_dir"], cfg["model"]["expected_version"],
                                    cfg["model"].get("calibration_policy", "reject"))
    periods = cfg.get("periods", {})
    first, last = f"{sim['start_year']+sim['warmup_years']}-01-01", f"{sim['end_year']}-12-31"
    for name, period in periods.items():
        for key in ("start", "end"): dt.date.fromisoformat(period[key])
        if not first <= period["start"] <= period["end"] <= last:
            raise ValueError(f"{name}: 时段须位于预热后的模拟范围内")
    if "calibration" in periods and "validation" in periods and periods["calibration"]["end"] >= periods["validation"]["start"]:
        raise ValueError("率定期与后续验证期不能重叠")
    from multi_observation import read_observation, normalized_unit
    counts = {}; seen = set()
    for obs in cfg.get("observations", []):
        if not obs.get("id") or obs["id"] in seen: raise ValueError("观测编号不能为空或重复")
        seen.add(obs["id"])
        if obs["frequency"] not in ("daily", "monthly"): raise ValueError("观测频率无效")
        number=obs["target"].get("unit_id")
        if number!="auto" and (type(number) is not int or number<1):raise ValueError("输出unit编号须为正整数或auto出口")
        if normalized_unit(obs["unit"]) != normalized_unit(obs["target"]["unit"]): raise ValueError("观测与输出单位不一致")
        values = read_observation(obs, root)
        counts[obs["id"]] = {name: sum(p["start"] <= d <= p["end"] for d in values) for name,p in periods.items()}
        if "calibration" in periods and counts[obs["id"]]["calibration"] < 2:
            raise ValueError(f"{obs['id']}: 率定期至少需要两条有效参考值")
    return {"planned_search_trials": 0, "mode": "model_then_toolbox", "input_checks": inputs,
            "observation_counts": counts, "note": "本任务只建模、基础试跑及准备交接，不进行参数搜索"}


class ModelWorkflow(Workflow):
    # Evaluation targets are configured explicitly per observation; a
    # non-channel outlet does not block the model run itself.
    requires_channel_outlet = False

    def __init__(self, job, state, cancel, pause_after=0):
        self.job, self.state, self.cancel = Path(job), state, cancel
        self.root = Path(state["workspace_root"])
        self.cfg = read_json(self.job / "config.resolved.json")
        self.pause_after, self.new_trials = pause_after, 0
        # Preserve optional reference files once, without requiring any reference to build.
        for i, obs in enumerate(self.cfg.get("observations", [])):
            if "original_path" not in obs:
                import shutil
                original = self.root / obs["path"]
                destination = self.job / f"reference_input_{i+1}.csv"
                if not destination.exists(): shutil.copy2(original, destination)
                obs.update(original_path=str(original), path=str(destination))
        save_json(self.job / "config.resolved.json", self.cfg)

    def run(self):
        if self.state.get("status") == "ready": return
        stop=self.job/"stop.request"
        if stop.exists():stop.unlink()
        self.state.update(status="running"); self.save()
        import threading
        finished=threading.Event()
        def watch_stop():
            while not finished.wait(.3):
                if (self.job/"stop.request").exists(): self.cancel.set();return
        threading.Thread(target=watch_stop,daemon=True).start()
        try:
            if self.cancel.is_set(): raise InterruptedError("已停止")
            self.ensure_model()
            for obs in self.cfg.get("observations",[]):
                if obs["target"].get("unit_id")=="auto":
                    outlet=self.state["outlet"]
                    if outlet.get("object","channel_sd")!="channel_sd" and obs["target"]["object"]=="channel_sd":
                        raise ValueError("模型末端是水库（或非河道出口）；channel_sd m³/s 流量参考不能自动挂到该出口。"
                                         "请显式指定要评价的河道unit（可为内部站点），或改用研究型多观测配置。")
                    obs["target"].update(unit_id=outlet["unit"],gis_id=outlet["gis_id"],name=outlet["name"])
            save_json(self.job/"config.resolved.json",self.cfg)
            if self.cancel.is_set() or (self.job/"stop.request").exists(): raise InterruptedError("已停止")
            self.state["phase"] = "baseline"; self.save()
            from swatplus_trial import run_trial
            work = self.job / "baseline"
            attempt=1
            while work.exists() and (not (work/"trial_result.json").is_file() or read_json(work/"trial_result.json").get("status")!="completed"):
                attempt+=1;work=self.job/f"baseline_attempt_{attempt}"
            if (work/"trial_result.json").is_file():
                result = read_json(work/"trial_result.json")
                if result["status"] != "completed": raise ValueError("基础试跑曾失败；请检查日志后以新任务重新运行")
            else:
                spec = {"root":str(self.root), "input_dir":self.state["input_snapshot"],
                        "executable":self.cfg["model"]["executable"], "expected_version":"62.0.1",
                        "work_dir":str(work), **self.cfg["simulation"], "frequency":self.cfg["frequency"],
                        "outlet":self.state["outlet"], "parameters":[], "parameter_backend":"native_calibration",
                        "print_policy":self.cfg["model"].get("print_policy","preserve"),
                        "calibration_policy":self.state.get("calibration_policy",self.cfg["model"].get("calibration_policy","reject")),
                        "engine_info":self.cfg.get("engine_info",{}),
                        "timeout_seconds":self.cfg["model"].get("timeout_seconds",600)}
                result = run_trial(spec,cancel_event=self.cancel)
            self.state["baseline_run"] = result
            if self.cancel.is_set() or (self.job/"stop.request").exists(): raise InterruptedError("已停止")
            from toolbox4_handoff import prepare_handoff
            data = prepare_handoff(self.job)
            self.state.update(status="ready", phase="toolbox_ready", handoff=data["instructions"])
            (self.job/"report.md").write_text(
                "# 建模与Toolbox交接\n\n模型已完成基础试运行；尚未进行参数搜索或验证。\n\n"
                +f"- 模型输入：{self.state['input_snapshot']}\n- 基础输出：{result['outlet_csv']}\n"
                +f"- Toolbox交接说明：{data['instructions']}\n\n"
                +"基础运行成功不等于水文过程合理；请检查空间划分、水量平衡及径流过程。\n", encoding="utf-8")
            self.message("建模和基础试跑完成；请从Toolbox入口继续。")
        except InterruptedError:
            self.state.update(status="interrupted"); self.message("任务已停止，已有结果保留。")
        except Exception as error:
            self.state.update(status="failed", last_error=str(error)); raise
        finally:
            finished.set()
            self.save()
