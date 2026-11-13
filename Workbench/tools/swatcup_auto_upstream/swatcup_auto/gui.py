from __future__ import annotations

import copy
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

from .config import DEFAULT_CONFIG, load_config, save_config, validate_config
from .errors import ConfigError
from .workflow import CalibrationWorkflow, check_project


class SwatCupAutoApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("SWAT-CUP / SUFI-2 自动率定")
        self.root.geometry("980x780")
        self.root.minsize(880, 680)
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.last_run_dir: Optional[Path] = None
        self.base_config = copy.deepcopy(DEFAULT_CONFIG)
        self.vars = {
            "source_project": tk.StringVar(),
            "runs_root": tk.StringVar(value=str((Path.cwd() / "runs").resolve())),
            "run_name": tk.StringVar(value="swatcup_calibration"),
            "simulation_count": tk.StringVar(),
            "stop_mode": tk.StringVar(value="fixed"),
            "rounds": tk.StringVar(value="2"),
            "max_rounds": tk.StringVar(value="10"),
            "target_source": tk.StringVar(value="best_goal"),
            "target_variable": tk.StringVar(),
            "target_metric": tk.StringVar(value="NS"),
            "target_operator": tk.StringVar(value=">="),
            "target_value": tk.StringVar(value="0.75"),
            "timeout_seconds": tk.StringVar(value="0"),
            "lock_initial": tk.BooleanVar(value=True),
            "validation_enabled": tk.BooleanVar(value=False),
            "validation_source": tk.StringVar(),
            "validation_count": tk.StringVar(),
        }
        self._build()
        self._update_mode()
        self.root.after(150, self._drain_events)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        paths = ttk.LabelFrame(outer, text="工程与运行副本", padding=10)
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, 0, "率定工程", "source_project")
        self._path_row(paths, 1, "运行目录", "runs_root")
        ttk.Label(paths, text="任务名称").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(paths, textvariable=self.vars["run_name"]).grid(
            row=2, column=1, sticky="ew", padx=6, pady=4
        )
        ttk.Label(paths, text="模拟次数计划").grid(row=2, column=2, sticky="w", pady=4)
        ttk.Entry(paths, textvariable=self.vars["simulation_count"], width=22).grid(
            row=2, column=3, sticky="w", padx=6, pady=4
        )
        ttk.Label(paths, text="例 100,200,500,1000；末值后续重复").grid(
            row=2, column=4, sticky="w", pady=4
        )

        controls = ttk.LabelFrame(outer, text="迭代与停止条件", padding=10)
        controls.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for column in range(8):
            controls.columnconfigure(column, weight=1 if column in {3, 5} else 0)
        ttk.Label(controls, text="模式").grid(row=0, column=0, sticky="w")
        mode = ttk.Combobox(
            controls,
            textvariable=self.vars["stop_mode"],
            values=("fixed", "target"),
            state="readonly",
            width=10,
        )
        mode.grid(row=0, column=1, sticky="w", padx=6)
        mode.bind("<<ComboboxSelected>>", lambda _: self._update_mode())
        ttk.Label(controls, text="固定轮数").grid(row=0, column=2, sticky="e")
        self.rounds_entry = ttk.Entry(controls, textvariable=self.vars["rounds"], width=8)
        self.rounds_entry.grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(controls, text="最大轮数").grid(row=0, column=4, sticky="e")
        ttk.Entry(controls, textvariable=self.vars["max_rounds"], width=8).grid(
            row=0, column=5, sticky="w", padx=6
        )
        ttk.Label(controls, text="单阶段超时（秒）").grid(row=0, column=6, sticky="e")
        ttk.Entry(controls, textvariable=self.vars["timeout_seconds"], width=10).grid(
            row=0, column=7, sticky="w", padx=6
        )

        ttk.Label(controls, text="目标来源").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.target_source = ttk.Combobox(
            controls,
            textvariable=self.vars["target_source"],
            values=("best_goal", "summary"),
            state="readonly",
            width=12,
        )
        self.target_source.grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(controls, text="变量").grid(row=1, column=2, sticky="e", pady=(8, 0))
        self.target_variable = ttk.Entry(
            controls, textvariable=self.vars["target_variable"], width=16
        )
        self.target_variable.grid(row=1, column=3, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(controls, text="指标").grid(row=1, column=4, sticky="e", pady=(8, 0))
        self.target_metric = ttk.Entry(
            controls, textvariable=self.vars["target_metric"], width=10
        )
        self.target_metric.grid(row=1, column=5, sticky="w", padx=6, pady=(8, 0))
        self.target_operator = ttk.Combobox(
            controls,
            textvariable=self.vars["target_operator"],
            values=(">=", ">", "<=", "<", "=="),
            state="readonly",
            width=5,
        )
        self.target_operator.grid(row=1, column=6, sticky="e", pady=(8, 0))
        self.target_value = ttk.Entry(
            controls, textvariable=self.vars["target_value"], width=12
        )
        self.target_value.grid(row=1, column=7, sticky="w", padx=6, pady=(8, 0))
        ttk.Checkbutton(
            controls,
            text="把初始参数范围作为安全边界（推荐）",
            variable=self.vars["lock_initial"],
        ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(controls, text="0 表示不限时").grid(
            row=2, column=6, columnspan=2, sticky="e", pady=(8, 0)
        )

        validation = ttk.LabelFrame(outer, text="验证期（可选）", padding=10)
        validation.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        validation.columnconfigure(2, weight=1)
        ttk.Checkbutton(
            validation,
            text="率定完成后运行验证期工程",
            variable=self.vars["validation_enabled"],
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(validation, text="验证工程").grid(row=0, column=1, sticky="e", padx=(14, 0))
        ttk.Entry(validation, textvariable=self.vars["validation_source"]).grid(
            row=0, column=2, sticky="ew", padx=6
        )
        ttk.Button(
            validation,
            text="浏览…",
            command=lambda: self._choose_directory("validation_source"),
        ).grid(row=0, column=3)
        ttk.Label(validation, text="模拟次数").grid(row=0, column=4, sticky="e", padx=(14, 0))
        ttk.Entry(validation, textvariable=self.vars["validation_count"], width=10).grid(
            row=0, column=5, padx=6
        )

        lower = ttk.Frame(outer)
        lower.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(1, weight=1)
        buttons = ttk.Frame(lower)
        buttons.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(buttons, text="载入配置", command=self._load_config_dialog).pack(
            side="left"
        )
        ttk.Button(buttons, text="保存配置", command=self._save_config_dialog).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="只读检查", command=self._check).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="续跑任务", command=self._resume).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="打开结果目录", command=self._open_result).pack(
            side="left", padx=(6, 0)
        )
        self.cancel_button = ttk.Button(
            buttons, text="停止", command=self._cancel, state="disabled"
        )
        self.cancel_button.pack(side="right")
        self.start_button = ttk.Button(buttons, text="开始新任务", command=self._start)
        self.start_button.pack(side="right", padx=(0, 6))

        self.log = ScrolledText(lower, height=16, wrap="word", state="disabled")
        self.log.grid(row=1, column=0, sticky="nsew")
        self.status = tk.StringVar(value="就绪")
        ttk.Label(lower, textvariable=self.status, anchor="w").grid(
            row=2, column=0, sticky="ew", pady=(4, 0)
        )

    def _path_row(self, parent: ttk.LabelFrame, row: int, label: str, key: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.vars[key]).grid(
            row=row, column=1, columnspan=3, sticky="ew", padx=6, pady=4
        )
        ttk.Button(
            parent, text="浏览…", command=lambda selected=key: self._choose_directory(selected)
        ).grid(row=row, column=4, sticky="e", pady=4)

    def _choose_directory(self, key: str) -> None:
        initial = self.vars[key].get() or str(Path.cwd())
        selected = filedialog.askdirectory(initialdir=initial)
        if selected:
            self.vars[key].set(selected)

    def _update_mode(self) -> None:
        target_mode = self.vars["stop_mode"].get() == "target"
        self.rounds_entry.configure(state="disabled" if target_mode else "normal")
        state = "readonly" if target_mode else "disabled"
        self.target_source.configure(state=state)
        self.target_operator.configure(state=state)
        for widget in (self.target_variable, self.target_metric, self.target_value):
            widget.configure(state="normal" if target_mode else "disabled")

    @staticmethod
    def _optional_int(value: str) -> Optional[int]:
        value = value.strip()
        return int(value) if value else None

    @staticmethod
    def _simulation_plan(value: str) -> list[int]:
        value = value.strip().replace("，", ",")
        if not value:
            return []
        parts = [part.strip() for part in value.split(",")]
        if any(not part for part in parts):
            raise ConfigError("模拟次数计划格式错误；请使用例如 100,200,500,1000")
        try:
            plan = [int(part) for part in parts]
        except ValueError as exc:
            raise ConfigError("模拟次数计划只能填写正整数并用逗号分隔") from exc
        if any(value < 1 for value in plan):
            raise ConfigError("模拟次数计划中的数值必须大于等于 1")
        return plan

    def _build_config(self) -> dict:
        config = copy.deepcopy(self.base_config)
        source_value = self.vars["source_project"].get().strip()
        runs_value = self.vars["runs_root"].get().strip()
        if not source_value:
            raise ConfigError("请选择率定工程目录")
        if not runs_value:
            raise ConfigError("请选择运行副本目录")
        config["source_project"] = str(Path(source_value).resolve())
        config["runs_root"] = str(Path(runs_value).resolve())
        config["run_name"] = self.vars["run_name"].get().strip()
        simulation_plan = self._simulation_plan(self.vars["simulation_count"].get())
        config["simulation_count"] = simulation_plan[0] if simulation_plan else None
        config["simulation_schedule"] = (
            simulation_plan if len(simulation_plan) > 1 else None
        )
        config["stop"]["mode"] = self.vars["stop_mode"].get()
        config["stop"]["rounds"] = int(self.vars["rounds"].get())
        config["stop"]["max_rounds"] = int(self.vars["max_rounds"].get())
        config["stop"]["target"].update(
            {
                "source": self.vars["target_source"].get(),
                "variable": self.vars["target_variable"].get().strip(),
                "metric": self.vars["target_metric"].get().strip(),
                "operator": self.vars["target_operator"].get(),
                "value": float(self.vars["target_value"].get()),
            }
        )
        config["execution"]["timeout_seconds"] = int(
            self.vars["timeout_seconds"].get()
        )
        config["parameters"]["lock_to_initial_bounds"] = bool(
            self.vars["lock_initial"].get()
        )
        config["validation"].update(
            {
                "enabled": bool(self.vars["validation_enabled"].get()),
                "source_project": (
                    str(Path(self.vars["validation_source"].get()).resolve())
                    if self.vars["validation_source"].get().strip()
                    else ""
                ),
                "simulation_count": self._optional_int(
                    self.vars["validation_count"].get()
                ),
            }
        )
        config.pop("_config_path", None)
        validate_config(config)
        return config

    def _populate(self, config: dict) -> None:
        self.base_config = copy.deepcopy(config)
        self.base_config.pop("_config_path", None)
        self.vars["source_project"].set(config["source_project"])
        self.vars["runs_root"].set(config["runs_root"])
        self.vars["run_name"].set(config["run_name"])
        simulation_plan = config.get("simulation_schedule") or []
        if simulation_plan:
            simulation_text = ",".join(str(value) for value in simulation_plan)
        else:
            simulation_text = (
                ""
                if config.get("simulation_count") is None
                else str(config["simulation_count"])
            )
        self.vars["simulation_count"].set(simulation_text)
        self.vars["stop_mode"].set(config["stop"]["mode"])
        self.vars["rounds"].set(str(config["stop"]["rounds"]))
        self.vars["max_rounds"].set(str(config["stop"]["max_rounds"]))
        target = config["stop"]["target"]
        self.vars["target_source"].set(target["source"])
        self.vars["target_variable"].set(target.get("variable", ""))
        self.vars["target_metric"].set(target.get("metric", "NS"))
        self.vars["target_operator"].set(target["operator"])
        self.vars["target_value"].set(str(target["value"]))
        self.vars["timeout_seconds"].set(str(config["execution"]["timeout_seconds"]))
        self.vars["lock_initial"].set(config["parameters"]["lock_to_initial_bounds"])
        validation = config["validation"]
        self.vars["validation_enabled"].set(validation["enabled"])
        self.vars["validation_source"].set(validation.get("source_project", ""))
        self.vars["validation_count"].set(
            ""
            if validation.get("simulation_count") is None
            else str(validation["simulation_count"])
        )
        self._update_mode()

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _load_config_dialog(self) -> None:
        path = filedialog.askopenfilename(filetypes=(("JSON", "*.json"), ("所有文件", "*.*")))
        if not path:
            return
        try:
            self._populate(load_config(path))
            self._append_log(f"已载入配置：{path}")
        except Exception as exc:
            messagebox.showerror("载入失败", str(exc))

    def _save_config_dialog(self) -> None:
        try:
            config = self._build_config()
        except Exception as exc:
            messagebox.showerror("配置无效", str(exc))
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=(("JSON", "*.json"),)
        )
        if path:
            save_config(config, path)
            self._append_log(f"已保存配置：{path}")

    def _check(self) -> None:
        try:
            config = self._build_config()
            schedule = config.get("simulation_schedule") or []
            if schedule:
                self._append_log(
                    "模拟次数计划："
                    + " → ".join(str(value) for value in schedule)
                    + f"；第 {len(schedule) + 1} 轮起重复 {schedule[-1]}"
                )
            elif config.get("simulation_count") is not None:
                self._append_log(
                    f"所有轮次模拟次数：{config['simulation_count']}"
                )
            else:
                self._append_log("所有轮次沿用 par_inf.txt 当前模拟次数")
            calibration = check_project(config["source_project"], config)
            self._append_log(json.dumps(calibration.to_dict(), ensure_ascii=False, indent=2))
            all_ok = calibration.ok
            if config["validation"]["enabled"]:
                validation = check_project(config["validation"]["source_project"], config)
                self._append_log(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2))
                all_ok = all_ok and validation.ok
            self.status.set("检查通过" if all_ok else "检查发现错误")
        except Exception as exc:
            messagebox.showerror("检查失败", str(exc))

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        self.status.set("运行中" if running else "就绪")

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self._build_config()
        except Exception as exc:
            messagebox.showerror("配置无效", str(exc))
            return
        self.cancel_event = threading.Event()
        self._set_running(True)

        def work() -> None:
            try:
                workflow = CalibrationWorkflow(
                    config,
                    event_callback=lambda value: self.events.put(("log", value)),
                    cancel_event=self.cancel_event,
                )
                result = workflow.run()
                self.events.put(("done", str(result)))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _resume(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        selected = filedialog.askdirectory(title="选择包含 state.json 的运行目录")
        if not selected:
            return
        self.cancel_event = threading.Event()
        self._set_running(True)

        def work() -> None:
            try:
                workflow = CalibrationWorkflow.from_run_dir(
                    selected,
                    event_callback=lambda value: self.events.put(("log", value)),
                    cancel_event=self.cancel_event,
                )
                result = workflow.run()
                self.events.put(("done", str(result)))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status.set("正在停止当前外部进程…")
        self._append_log("已请求停止；程序将终止当前批处理进程树并保存状态")

    def _open_result(self) -> None:
        target = self.last_run_dir or Path(self.vars["runs_root"].get())
        if not target.exists():
            messagebox.showinfo("目录不存在", str(target))
            return
        os.startfile(str(target))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self.last_run_dir = Path(payload)
                    self._append_log(f"任务完成：{payload}")
                    self._set_running(False)
                    self.status.set("任务完成")
                    messagebox.showinfo("完成", f"任务已完成\n{payload}")
                elif kind == "error":
                    self._append_log(f"任务失败或停止：{payload}")
                    self._set_running(False)
                    self.status.set("任务失败或已停止")
                    messagebox.showerror("任务未完成", payload)
        except queue.Empty:
            pass
        self.root.after(150, self._drain_events)


def launch_gui() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    SwatCupAutoApp(root)
    root.mainloop()
