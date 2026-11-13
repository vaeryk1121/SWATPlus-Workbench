"""Step-2 reservoir dialog: inspect / edit on copies / official export.

清晰区分三个阶段并分别报告：配置（项目库副本写入，教学参数须自标识）、
官方导出（Editor API）、运行与对照（由命令行 reconnect_check 完成，对话框
只给出可复现命令）。不重新划分流域，不做全自动多水库空间建模。
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

RULE_FIELDS = ("rule_id",)


class ReservoirDialog(tk.Toplevel):
    def __init__(self, parent, root, status):
        super().__init__(parent)
        self.root_path = Path(root); self.status = status; self.busy = False
        self.title("水库管理 · SWAT+ 工作台")
        ui.fit_window(self, 1080, 820)
        self.transient(parent.winfo_toplevel())
        shell = ttk.Frame(self, padding=16); shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="水库管理", style="Title.TLabel").pack(anchor="w")
        ui.wrap_label(ttk.Label(shell, text="读取项目中的水库，编辑属性或选择放水规则；修改仅作用于项目副本。",
                               style="Muted.TLabel")).pack(fill="x", pady=(0, 12))
        project_bar = ttk.Frame(shell); project_bar.pack(fill="x", pady=(0, 12))
        ttk.Label(project_bar, text="项目库").grid(row=0, column=0, padx=(0, 10))
        self.project = tk.StringVar(value=str(self.root_path / "models/teaching/robit_res01/res01.sqlite"))
        ttk.Entry(project_bar, textvariable=self.project, width=1).grid(row=0, column=1, sticky="ew")
        ttk.Button(project_bar, text="浏览…", command=self.choose_project).grid(row=0, column=2, padx=8)
        ttk.Button(project_bar, text="读取水库", command=self.load).grid(row=0, column=3)
        project_bar.columnconfigure(1, weight=1)
        footer = ttk.Frame(shell); footer.pack(side="bottom", fill="x", pady=(12, 0))
        export_row = ttk.Frame(footer); export_row.pack(fill="x")
        ttk.Label(export_row, text="导出位置").pack(side="left", padx=(0, 10))
        self.export_dir = tk.StringVar()
        ttk.Entry(export_row, textvariable=self.export_dir, width=1).pack(side="left", fill="x", expand=True)
        actions = ttk.Frame(footer); actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="生成新版本目录", command=self.suggest_export_dir).pack(side="left")
        ttk.Button(actions, text="官方Editor导出", style="Primary.TButton", command=self.export).pack(side="left")
        ttk.Button(actions, text="运行与对照说明", command=self.show_run_hint).pack(side="left")
        ui.flow_buttons(actions)
        self.message = tk.StringVar(value="请选择项目库并读取；写入操作只针对副本。")
        ui.wrap_label(ttk.Label(footer, textvariable=self.message, style="Hint.TLabel",
                               justify="left")).pack(fill="x", pady=(6, 0))
        self.scroll_pane = ui.ScrollPane(shell, padding=0)
        self.scroll_pane.pack(fill="both", expand=True)
        body = self.scroll_pane.content
        listing = ttk.LabelFrame(body, text="项目中的水库", padding=10); listing.pack(fill="x")
        self.table = ttk.Treeview(listing, columns=("name", "rule", "area_ps", "vol_ps", "yr_op", "upstream", "downstream"),
                                 show="headings", height=5)
        for key, caption, width in (("name", "对象", 100), ("rule", "放水规则", 150),
                                  ("area_ps", "常水位面积 (ha)", 130), ("vol_ps", "常水位库容 (ha·m)", 150),
                                  ("yr_op", "投运年", 85), ("upstream", "上游河道", 150), ("downstream", "下游对象", 150)):
            self.table.heading(key, text=caption)
            self.table.column(key, width=ui.px(self, width), minwidth=ui.px(self, 70))
        y = ttk.Scrollbar(listing, orient="vertical", command=self.table.yview)
        x = ttk.Scrollbar(listing, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.table.grid(row=0, column=0, sticky="nsew"); y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew"); listing.columnconfigure(0, weight=1)
        self.table.bind("<<TreeviewSelect>>", self._load_selected_properties)
        editor = ttk.LabelFrame(body, text="选中水库 · 属性与规则", padding=14); editor.pack(fill="x", pady=12)
        for col in (1, 3): editor.columnconfigure(col, weight=1)
        self.rule = tk.StringVar()
        ttk.Label(editor, text="放水规则").grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.rule_box = ttk.Combobox(editor, textvariable=self.rule, state="readonly", width=1)
        self.rule_box.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 12), pady=(0, 10))
        ttk.Button(editor, text="应用规则", command=self.apply_rule).grid(row=0, column=3, sticky="e", pady=(0, 10))
        self.hydro_vars = {}; self.hydro_widgets = {}
        definitions = (("area_ps", "常水位面积 (ha)"), ("vol_ps", "常水位库容 (ha·m)"),
                       ("area_es", "应急溢洪位面积 (ha)"), ("vol_es", "应急溢洪位库容 (ha·m)"),
                       ("yr_op", "投运年"), ("mon_op", "投运月"),
                       ("k", "渗透系数 (mm/h)"), ("evap_co", "蒸发系数"))
        for index, (key, caption) in enumerate(definitions):
            row, col = 1 + index // 2, (index % 2) * 2
            ttk.Label(editor, text=caption, wraplength=ui.px(self, 160)).grid(
                row=row, column=col, sticky="w", padx=(0 if col == 0 else 18, 8), pady=6)
            var = tk.StringVar(); self.hydro_vars[key] = var
            widget = ttk.Entry(editor, textvariable=var, width=1)
            widget.grid(row=row, column=col+1, sticky="ew", pady=6)
            self.hydro_widgets[key] = widget
        self.apply_hydro_button = ttk.Button(editor, text="保存水库属性到项目副本",
                                             style="Primary.TButton", command=self.apply_hydrology)
        self.apply_hydro_button.grid(row=5, column=0, columnspan=4, sticky="e", pady=(12, 0))
        creator = ttk.LabelFrame(body, text="新增教学水库 · 沿现有末端河道挂接", padding=14)
        creator.pack(fill="x")
        creator.columnconfigure(1, weight=1); creator.columnconfigure(3, weight=1)
        self.new_name = tk.StringVar(value="res01")
        self.new_dir = tk.StringVar(value=str(self.root_path / "models/teaching/robit_res01"))
        self.new_desc = tk.StringVar(value="TEACHING reservoir: synthetic parameters, not a real survey")
        self.source_project = tk.StringVar(value=str(self.root_path / "models/examples/robit344_check_b1/robit344_check_b1.sqlite"))
        self.weather_override = tk.StringVar()
        for row, caption, var in ((0, "对象名称", self.new_name), (1, "副本目录", self.new_dir),
                                  (2, "来源项目库", self.source_project), (3, "气象目录（可选）", self.weather_override),
                                  (4, "参数与来源说明", self.new_desc)):
            ttk.Label(creator, text=caption, wraplength=ui.px(self, 160)).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
            ttk.Entry(creator, textvariable=var, width=1).grid(row=row, column=1, columnspan=3, sticky="ew", pady=6)
        ttk.Label(creator, text="气象目录留空时，按来源项目设置读取；新对象使用上方选定的放水规则。",
                  style="Hint.TLabel", wraplength=ui.px(self, 850)).grid(row=5, column=0, columnspan=4, sticky="w", pady=7)
        ttk.Button(creator, text="复制工程并创建教学水库", command=self.create).grid(row=6, column=0, columnspan=4, sticky="e")
        ui.disclosure(body, "适用范围与单位说明", '支持边界：在官方项目数据库结构内编辑/新增水库对象与属性、选择已有官方放水规则（res_rel.dtl 决策表），导出调用官方Editor API（write_files），运行使用官方SWAT+引擎。水库空间位置沿用现有河道拓扑（新增对象挂在现有唯一末端河道），不重新划分流域，不是全自动多水库空间建模；本机无真实水库工程，示例参数为明确标识的教学值，不代表真实库容或调度。三个阶段分开报告：配置=项目库副本写入；导出=官方API；运行=引擎执行，对照用 reconnect_check。')


    def _load_selected_properties(self, event=None):
        selection = self.table.selection()
        if not selection:
            return
        item = next((r for r in getattr(self, "_reservoirs", [])
                     if str(r["id"]) == str(selection[0])), None)
        if not item:
            return
        for key, var in self.hydro_vars.items():
            value = (item.get("hydrology") or {}).get(key)
            var.set("" if value is None else str(value))
        rule = (item.get("properties") or {}).get("release_rule")
        for candidate in getattr(self, "_rules", []):
            if candidate["name"] == rule:
                self.rule.set(f"{candidate['id']}:{candidate['name']}")
                break

    def choose_project(self):
        path = filedialog.askopenfilename(parent=self, initialdir=str(self.root_path / "models"),
                                          filetypes=[("项目库", "*.sqlite")])
        if path:
            self.project.set(path)

    def selected_id(self):
        selection = self.table.selection()
        if not selection:
            raise ValueError("请先在列表中选择一个水库对象")
        return int(selection[0])

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
                from reservoir_setup import read_reservoirs, available_rules
                data = read_reservoirs(self.project.get())
                rules = available_rules(self.project.get())
                events.put(("ok", data, rules))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                messagebox.showerror("读取失败", outcome[1], parent=self); return
            data, rules = outcome[1], outcome[2]
            self.table.delete(*self.table.get_children())
            self._reservoirs = data["reservoirs"]
            for reservoir in self._reservoirs:
                hydro = reservoir["hydrology"] or {}
                self.table.insert("", "end", iid=str(reservoir["id"]), values=(
                    reservoir["name"], reservoir["properties"]["release_rule"] or "（无）",
                    hydro.get("area_ps", ""), hydro.get("vol_ps", ""), hydro.get("yr_op", ""),
                    ",".join(up["channel"] or str(up["channel_id"]) for up in reservoir["upstream_channels"]) or "（无）",
                    ",".join(f"{out['obj_typ']}:{out['obj_id']}" for out in reservoir["downstream"]) or "末端"))
            self._rules = rules
            self.rule_box["values"] = [f"{rule['id']}:{rule['name']}" for rule in rules]
            if rules:
                self.rule.set(self.rule_box["values"][0])
            if self._reservoirs:
                self.table.selection_set(str(self._reservoirs[0]["id"]))
                self._load_selected_properties()
            self.message.set(f"读取到 {len(self._reservoirs)} 个水库对象；规则来自官方 res_rel.dtl 决策表。")
        self.run_task(work, done)

    def apply_rule(self):
        try:
            sys.path.insert(0, str(self.root_path / "scripts"))
            from reservoir_setup import set_release_rule
            rule_id = int(self.rule.get().split(":", 1)[0])
            report = set_release_rule(self.project.get(), self.selected_id(), rule_id)
            self.message.set(f"规则已写入项目库副本（阶段：{report['stage']}）：{self.rule.get()}；需重新官方导出后生效。")
        except Exception as error:
            messagebox.showerror("规则未写入", str(error), parent=self)

    def apply_hydrology(self):
        try:
            sys.path.insert(0, str(self.root_path / "scripts"))
            from reservoir_setup import set_hydrology
            fields = {key: var.get() for key, var in self.hydro_vars.items() if var.get().strip()}
            report = set_hydrology(self.project.get(), self.selected_id(), fields)
            self.message.set("水力参数已写入项目库副本：" + json.dumps(report["updated"], ensure_ascii=False)
                             + "；单位：" + json.dumps(report["units"], ensure_ascii=False) + "；需重新官方导出后生效。")
        except Exception as error:
            messagebox.showerror("参数未写入", str(error), parent=self)

    def create(self):
        def work(events):
            try:
                sys.path.insert(0, str(self.root_path / "scripts"))
                from reservoir_setup import add_teaching_reservoir, available_rules, resolve_weather_dir
                rules = available_rules(self.source_project.get())
                rule_id = int(self.rule.get().split(":", 1)[0]) if self.rule.get() else rules[0]["id"]
                override = self.weather_override.get().strip()
                # Weather follows the SELECTED source project's own official
                # settings (station files verified) unless the user explicitly
                # chose another directory; other basins never get Robit weather.
                weather = Path(override) if override else resolve_weather_dir(self.source_project.get())
                report = add_teaching_reservoir(self.source_project.get(), self.new_dir.get(),
                                                self.new_name.get(), rule_id,
                                                description=self.new_desc.get() or None,
                                                weather_dir=weather)
                events.put(("ok", report))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                messagebox.showerror("教学水库未创建", outcome[1], parent=self); return
            report = outcome[1]
            self.project.set(report["project_db"])
            self.message.set(f"已创建项目副本并写入教学水库（阶段：{report['stage']}），挂接河道："
                             f"{report['wired_channel']['name']}；下一步：读取对象 → 官方导出。")
            self.load()
        self.run_task(work, done)

    def suggest_export_dir(self):
        """Offer the next free Scenarios/vN/TxtInOut so re-exports never
        overwrite an existing export (edit -> save-as-new-version loop)."""
        project = Path(self.project.get())
        base = project.parent / "Scenarios"
        index = 2
        while (base / f"v{index}" / "TxtInOut").exists():
            index += 1
        self.export_dir.set(str(base / f"v{index}" / "TxtInOut"))

    def export(self):
        def work(events):
            try:
                sys.path.insert(0, str(self.root_path / "scripts"))
                from reservoir_setup import export_project
                requested = self.export_dir.get().strip() or None
                report = export_project(self.project.get(), output_dir=requested,
                                        print_objects=["reservoir", "basin_res"])
                events.put(("ok", report))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                messagebox.showerror("官方导出未完成", outcome[1], parent=self); return
            report = outcome[1]
            self.message.set(f"官方Editor API导出完成（阶段：{report['stage']}）：{report['output_dir']}，"
                             f"{report['files_exported']}个文件；既有导出未改动；日志：{report['log_dir']}。")
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
        self.message.set("运行与对照命令（时段/预热/任务名来自本项目设置；工作台试跑+官方直接运行，"
                         "必比含水库入流/出流/蓄水输出）：" + command)
        try:
            subprocess.Popen(["cmd", "/k", f'cd /d "{self.root_path}" && {command}'], cwd=self.root_path)
        except Exception:
            pass
