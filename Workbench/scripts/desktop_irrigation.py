"""Step-3 irrigation dialog: inspect, configure on a copy, official export.

水源受限灌溉走官方日程auto+决策表机制（官方导出完整支持，actions.f90
从res/cha/aqu实际扣水、不足不供水）。运行与对照由 reconnect_check 完成，
对话框只给可复现命令。不含日程手工irrm编辑与植物参数编辑（边界见说明区）。
"""
import json
import queue
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
import desktop_ui as ui
from tkinter import ttk, filedialog, messagebox


class IrrigationDialog(tk.Toplevel):
    def __init__(self, parent, root, status):
        super().__init__(parent)
        self.root_path = Path(root); self.status = status; self.busy = False
        self.title("作物与灌溉 · SWAT+ 工作台")
        ui.fit_window(self, 1080, 820)
        self.transient(parent.winfo_toplevel())
        shell = ttk.Frame(self, padding=16); shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="作物与灌溉", style="Title.TLabel").pack(anchor="w")
        ui.wrap_label(ttk.Label(shell, text="先读取项目管理信息，再选择应用范围与取水来源；所有写入均在工程副本中完成。",
                               style="Muted.TLabel")).pack(fill="x", pady=(0, 12))
        project_bar = ttk.Frame(shell); project_bar.pack(fill="x", pady=(0, 12))
        ttk.Label(project_bar, text="项目库").grid(row=0, column=0, padx=(0, 10))
        self.project = tk.StringVar(value=str(self.root_path / "models/teaching/robit_irr01/res01.sqlite"))
        ttk.Entry(project_bar, textvariable=self.project, width=1).grid(row=0, column=1, sticky="ew")
        ttk.Button(project_bar, text="浏览…", command=self.choose_project).grid(row=0, column=2, padx=8)
        ttk.Button(project_bar, text="读取管理信息", command=self.load).grid(row=0, column=3)
        project_bar.columnconfigure(1, weight=1)
        footer = ttk.Frame(shell); footer.pack(side="bottom", fill="x", pady=(12, 0))
        export_row = ttk.Frame(footer); export_row.pack(fill="x")
        ttk.Label(export_row, text="导出位置").pack(side="left", padx=(0, 10))
        self.export_dir = tk.StringVar()
        ttk.Entry(export_row, textvariable=self.export_dir, width=1).pack(side="left", fill="x", expand=True)
        actions = ttk.Frame(footer); actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="官方Editor导出", style="Primary.TButton", command=self.export).pack(side="left")
        ttk.Button(actions, text="运行与对照说明", command=self.show_run_hint).pack(side="left")
        ui.flow_buttons(actions)
        self.message = tk.StringVar(value="请选择项目库并读取；选择表格行可带入地类范围。")
        ui.wrap_label(ttk.Label(footer, textvariable=self.message, style="Hint.TLabel",
                               justify="left")).pack(fill="x", pady=(6, 0))
        self.scroll_pane = ui.ScrollPane(shell, padding=0); self.scroll_pane.pack(fill="both", expand=True)
        body = self.scroll_pane.content
        listing = ttk.LabelFrame(body, text="已有地类与管理日程", padding=10); listing.pack(fill="x")
        self.table = ttk.Treeview(listing, columns=("landuse", "hrus", "area", "schedule", "autos", "plants"), show="headings", height=5)
        for key, caption, width in (("landuse", "地类", 130), ("hrus", "HRU数", 75), ("area", "面积 (ha)", 110),
                                  ("schedule", "管理日程", 160), ("autos", "自动规则", 220), ("plants", "植物群落", 160)):
            self.table.heading(key, text=caption); self.table.column(key, width=ui.px(self, width), minwidth=ui.px(self, 70))
        y = ttk.Scrollbar(listing, orient="vertical", command=self.table.yview)
        x = ttk.Scrollbar(listing, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.table.grid(row=0, column=0, sticky="nsew"); y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew"); listing.columnconfigure(0, weight=1)
        self.table.bind("<<TreeviewSelect>>", self._load_scope_selection)
        editor = ttk.LabelFrame(body, text="受限灌溉 · 规则配置", padding=14); editor.pack(fill="x", pady=12)
        for col in (1, 3): editor.columnconfigure(col, weight=1)
        self.vars = {}; self.fields = {}
        definitions = (("scope", "应用范围（地类或HRU编号）"), ("source_type", "水源类型 res / cha / aqu"),
                       ("source_obj", "取水对象编号"), ("amount", "定额 (mm/次)"),
                       ("threshold", "水分胁迫阈值 (0–1)"), ("max_year", "年最大次数"),
                       ("application", "灌溉方式"), ("rule_name", "新规则名称（小写）"))
        defaults = {"source_type":"res", "source_obj":"1", "amount":"5.0", "threshold":"0.55",
                    "max_year":"20", "application":"sprinkler_med", "rule_name":"irr_teach05"}
        for index, (key, caption) in enumerate(definitions):
            row, col = index // 2, (index % 2) * 2
            ttk.Label(editor, text=caption, wraplength=ui.px(self, 160)).grid(
                row=row, column=col, sticky="w", padx=(0 if col == 0 else 18, 8), pady=6)
            var = tk.StringVar(value=defaults.get(key, "")); self.vars[key] = var
            if key in ("source_type", "scope", "application"):
                widget = ttk.Combobox(editor, textvariable=var, width=1,
                                     state="readonly" if key == "source_type" else "normal",
                                     values=("res", "cha", "aqu") if key == "source_type" else ())
            else: widget = ttk.Entry(editor, textvariable=var, width=1)
            widget.grid(row=row, column=col+1, sticky="ew", pady=6); self.fields[key] = widget
        self.configure_button = ttk.Button(editor, text="保存灌溉规则到项目副本", style="Primary.TButton", command=self.configure)
        self.configure_button.grid(row=4, column=0, columnspan=4, sticky="e", pady=(12, 0))
        assigner = ttk.LabelFrame(body, text="已有管理日程与植物群落 · 选择关联", padding=14); assigner.pack(fill="x")
        assigner.columnconfigure(1, weight=1); assigner.columnconfigure(3, weight=1)
        self.assign_schedule = tk.StringVar(); self.assign_community = tk.StringVar()
        ttk.Label(assigner, text="管理日程").grid(row=0, column=0, padx=(0, 12), pady=6)
        self.schedule_box = ttk.Combobox(assigner, textvariable=self.assign_schedule, state="readonly", width=1)
        self.schedule_box.grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Label(assigner, text="植物群落").grid(row=0, column=2, padx=(18, 12), pady=6)
        self.community_box = ttk.Combobox(assigner, textvariable=self.assign_community, state="readonly", width=1)
        self.community_box.grid(row=0, column=3, sticky="ew", pady=6)
        ttk.Label(assigner, text="应用范围").grid(row=1, column=0, padx=(0, 12), pady=6)
        ttk.Entry(assigner, textvariable=self.vars["scope"], width=1).grid(row=1, column=1, columnspan=3, sticky="ew", pady=6)
        ttk.Button(assigner, text="关联到所选范围", command=self.assign).grid(row=2, column=0, columnspan=4, sticky="e", pady=(8, 0))
        ui.disclosure(body, "适用范围与供水约束", '支持边界：水源受限灌溉=官方日程auto操作+决策表（w_stress触发、res/cha/aqu扣水、不足不供水），全部经官方Editor API导出；HRU范围跟随所选地类的日程，未选地类不受影响。区分：植物生长参数只读未接入编辑；播种/收获等日程操作与按日期手工灌溉(irrm，不扣水源)未接入编辑。水权文件路线(water_hru_irr.wal)在本机62.0.1引擎存在上游缺陷（actions.f90:246访问违例，初始化联动被注释），不可运行，已实测并记录。三阶段分开报告：配置=项目库副本写入；导出=官方API；运行=官方引擎，对照用reconnect_check。')


    def _load_scope_selection(self, event=None):
        selection = self.table.selection()
        if selection:
            values = self.table.item(selection[0], "values")
            if values:
                self.vars["scope"].set(str(values[0]))

    def choose_project(self):
        path = filedialog.askopenfilename(parent=self, initialdir=str(self.root_path / "models"),
                                          filetypes=[("项目库", "*.sqlite")])
        if path:
            self.project.set(path)

    def run_task(self, work, done):
        if self.busy:
            return
        self.busy = True
        events = queue.Queue()

        def poll():
            try:
                outcome = events.get_nowait()
            except queue.Empty:
                self.after(100, poll); return
            self.busy = False
            done(outcome)
        threading.Thread(target=work, args=(events,), daemon=True).start()
        self.after(100, poll)

    def load(self):
        def work(events):
            try:
                sys.path.insert(0, str(self.root_path / "scripts"))
                from irrigation_setup import read_management
                events.put(("ok", read_management(self.project.get())))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                messagebox.showerror("读取失败", outcome[1], parent=self); return
            data = outcome[1]
            self.table.delete(*self.table.get_children())
            schedules = {s["name"]: s for s in data["management_schedules"]}
            for item in data["landuse"]:
                schedule = schedules.get(item["schedule"])
                self.table.insert("", "end", values=(
                    item["name"], item["hrus"], item["area_ha"], item["schedule"] or "（无）",
                    ",".join(rule["name"] or str(rule["id"]) for rule in (schedule["auto_rules"] if schedule else [])) or "（无）",
                    item["plant_community"] or "（无）"))
            self.vars["scope"].set(next((i["name"] for i in data["landuse"] if i["schedule"]), ""))
            self.schedule_box["values"] = [s["name"] for s in data["management_schedules"]]
            communities = sorted({item["plant_community"] for item in data["landuse"] if item["plant_community"]})
            self.community_box["values"] = communities
            apps = [a["name"] for a in data["irrigation_application_types"]]
            self.fields["scope"].configure(values=[item["name"] for item in data["landuse"]])
            self.fields["application"].configure(values=apps)
            if apps:
                self.vars["application"].set(apps[0])
            self.message.set(f"读取到 {len(data['landuse'])} 个地类、{len(data['irrigation_rules'])} 条现有灌溉规则；"
                             f"灌溉方式：{apps}；水源对象：{data['available_sources']}")
        self.run_task(work, done)

    def configure(self):
        def work(events):
            try:
                sys.path.insert(0, str(self.root_path / "scripts"))
                from irrigation_setup import configure_constrained_irrigation
                values = {key: var.get().strip() for key, var in self.vars.items()}
                report = configure_constrained_irrigation(
                    self.project.get(), landuse_name=values["scope"],
                    source_type=values["source_type"], source_obj_id=int(values["source_obj"]),
                    amount_mm=float(values["amount"]), stress_threshold=float(values["threshold"]),
                    max_per_year=int(values["max_year"]), application_name=values["application"],
                    rule_name=values["rule_name"])
                events.put(("ok", report))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                messagebox.showerror("灌溉规则未写入", outcome[1], parent=self); return
            report = outcome[1]
            self.message.set(f"已写入项目库副本（阶段：{report['stage']}）：规则 {report['rule_name']} 挂接日程 "
                             f"{report['schedule_link']['schedule']}（auto），范围 {report['scope']['count']} 个HRU；"
                             "需官方导出后生效。")
        self.run_task(work, done)

    def assign(self):
        def work(events):
            try:
                sys.path.insert(0, str(self.root_path / "scripts"))
                from irrigation_setup import assign_management
                scope = self.vars["scope"].get().strip()
                hru_ids = None
                if "," in scope or scope.isdigit():
                    hru_ids = [int(part) for part in scope.split(",") if part.strip()]
                    landuse = None
                else:
                    landuse = scope
                report = assign_management(self.project.get(), landuse_name=landuse, hru_ids=hru_ids,
                                           schedule_name=self.assign_schedule.get() or None,
                                           plant_community_name=self.assign_community.get() or None)
                events.put(("ok", report))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                messagebox.showerror("关联未写入", outcome[1], parent=self); return
            report = outcome[1]
            self.message.set(f"已关联（阶段：{report['stage']}）：{report['changed']}；未选对象引用保持不变；需官方导出后生效。")
        self.run_task(work, done)

    def export(self):
        def work(events):
            try:
                sys.path.insert(0, str(self.root_path / "scripts"))
                from reservoir_setup import export_project
                requested = self.export_dir.get().strip() or None
                report = export_project(self.project.get(), output_dir=requested,
                                        print_objects=["basin_wb", "basin_res", "channel_sd",
                                                       "reservoir", "hru_wb"])
                events.put(("ok", report))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                messagebox.showerror("官方导出未完成", outcome[1], parent=self); return
            report = outcome[1]
            self.export_dir.set(report["output_dir"])
            self.message.set(f"官方Editor API导出完成：{report['output_dir']}（{report['files_exported']}文件）；"
                             "既有导出保留。")
        self.run_task(work, done)

    def show_run_hint(self):
        try:
            sys.path.insert(0, str(self.root_path / "scripts"))
            from reservoir_setup import current_export_dir, default_run_settings
            export_dir = current_export_dir(self.project.get())
            settings = default_run_settings(self.project.get())
        except Exception as error:
            self.message.set("无法生成运行命令（请先在有效项目库上导出）：" + str(error))
            return
        command = (f'python -X utf8 scripts/reconnect_check.py --input-dir "{export_dir}" '
                   f'--name {settings["task_name"]} '
                   f'--start-year {settings["start_year"]} --end-year {settings["end_year"]} '
                   f'--warmup {settings["warmup_years"]}')
        self.message.set("运行与对照命令（时段/预热/任务名来自本项目；含水库入流/出流/蓄水必比）：" + command)
        try:
            subprocess.Popen(["cmd", "/k", f'cd /d "{self.root_path}" && {command}'], cwd=self.root_path)
        except Exception:
            pass
