"""第4步B（含第1次返修）：管理情景GUI面板（接现有蓝色工作台，复scenario_setup后台）。

用户流程：选项目（浏览/信息）→选规则/填情景→检查与计划→运行/停止→看报告。
不重写科学逻辑：模板/校验/导出/运行/汇总全部复用 scenario_setup 已验收接口。
耗时工作走后台线程+主线程after更新；取消经同一cancel_event透传。
正式入口（per-monitor DPI）下按 tk 缩放自适应尺寸；表单/表格可滚动，运行/停止固定可见。
"""
import json
import queue
from pathlib import Path
import threading
import tkinter as tk
import desktop_ui as ui
from tkinter import ttk, filedialog, messagebox

import scenario_setup as ss

WORKSPACE = Path(__file__).resolve().parents[1]

# 中文选项 ↔ 后台内部值（全部真实值仍存 self.vars，提交给已验收后台的口径不变）
TEMPLATE_CHOICES = (("水库放水规则", "reservoir_release_rule"),
                    ("受限灌溉", "constrained_irrigation"))
MODE_CHOICES = (("替换选中范围", "replace"),
                ("追加并保留原规则", "append"))
SOURCE_CHOICES = (("水库（res）", "res"), ("河道（cha）", "cha"), ("含水层（aqu）", "aqu"))
FAILURE_CHOICES = (("发生错误后继续", "continue"),
                   ("首错即停", "stop"))
STRATEGY_TEXT = {
    "monthly_summary": ("精简月汇总（daily/avann关、月输出开）",
                        {"daily": False, "monthly": True, "yearly": False, "avann": False}),
    "preserve": ("保留来源输出（不改动项目print设置）", None)}
STATUS_CN = {"completed": "完成", "run_failed": "模型运行失败",
             "comparison_failed": "官方对照未通过", "cancelled": "已取消",
             "summary_failed": "汇总失败", "summary_incomplete": "汇总不完整",
             "failed": "失败"}
REASON_CN = {"user_cancel": "用户取消", "skipped_after_failure": "前序失败后跳过"}


def _display_for(key, value):
    choices = {"kind": TEMPLATE_CHOICES, "mode": MODE_CHOICES,
               "source_type": SOURCE_CHOICES, "on_failure": FAILURE_CHOICES}.get(key, ())
    for label, internal in choices:
        if internal == value:
            return label
    return value


def _internal_for(key, display):
    choices = {"kind": TEMPLATE_CHOICES, "mode": MODE_CHOICES,
               "source_type": SOURCE_CHOICES, "on_failure": FAILURE_CHOICES}.get(key, ())
    for label, internal in choices:
        if label == display:
            return internal
    return display            # 允许直接填写内部值（导入表兼容）


class ScenarioDialog(tk.Toplevel):
    """管理情景：导入/编辑/校验/计划/运行停止/结果查看/已有结果载入。"""

    def __init__(self, parent, root, status):
        super().__init__(parent)
        self.root_path = Path(root)
        self.status = status
        self.busy = False           # 后台任务占线（含校验/计划/时段），由 _queue_task 维护
        self.batch_running = False  # 批处理运行中（编辑锁/停止语义），仅任务真正提交后为 True
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        # 测试可置 True：_queue_task 同步执行（无主线程/事件循环的环境）
        self.synchronous = False
        self.rows = []              # 情景表行（dict，内部值口径）
        self.last_result = None
        self._project_info = None
        self._project_info_path = None
        self._plan_signature = None  # 生成计划时的参数签名（None=尚未生成有效计划）
        self.title("管理情景 · SWAT+ 工作台")
        self._apply_scaled_geometry()
        self.transient(parent.winfo_toplevel())
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="管理情景", style="Title.TLabel").pack(anchor="w")
        ui.wrap_label(ttk.Label(body, text="选择基础工程，配置独立情景；运行后在结果页查看报告。",
                               style="Muted.TLabel")).pack(fill="x", pady=(0, 12))
        footer = ttk.Frame(body)
        footer.pack(side="bottom", fill="x")
        self.tabs = ttk.Notebook(body)
        self.settings_page = ttk.Frame(self.tabs)
        self.results_page = ttk.Frame(self.tabs)
        self.tabs.add(self.settings_page, text="情景配置")
        self.tabs.add(self.results_page, text="结果与报告")
        self.tabs.pack(fill="both", expand=True)

        # ---- 可滚动主体（表单/表格/计划），运行/停止与结果固定可见 ----------
        outer = ttk.Frame(self.settings_page)
        outer.pack(fill="both", expand=True)   # 可滚动主体占据剩余高度；运行/结果固定在下
        self.canvas = tk.Canvas(outer, highlightthickness=0, background="#FFFFFF")
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll_body = ttk.Frame(self.canvas)
        scroll_body.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=scroll_body, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            "all", width=e.width))
        inner = scroll_body
        ui.attach_wheel(self.canvas, scroll_body)

        # ---- 顶部：项目与教学表 ----------
        top = ttk.LabelFrame(inner, text="项目与情景表", padding=12)
        top.pack(fill="x")
        ttk.Button(top, text="载入教学情景表（robit_res01）",
                   command=self.load_teaching_table).pack(side="left")
        ttk.Button(top, text="导入JSON/CSV…", command=self.import_table).pack(side="left", padx=6)
        ttk.Button(top, text="导出JSON…", command=self.export_table).pack(side="left")
        ttk.Button(top, text="导出CSV…", command=self.export_table_csv).pack(side="left", padx=6)

        # ---- 情景行表格（稳定行号iid；横/纵滚动） ----------
        ttk.Button(top, text="选择基础项目…", command=self.browse_base_project).pack(side="left", padx=6)
        ui.flow_buttons(top)
        table_frame = ttk.Frame(inner)
        table_frame.pack(fill="x", pady=(6, 4))
        columns = ("scenario", "base", "kind", "mode", "rule", "detail")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=3)
        scale = self._ui_scale()
        for col, cap, w in (("scenario", "情景名", 110), ("base", "基础项目", 220),
                            ("kind", "模板", 150), ("mode", "模式", 70),
                            ("rule", "规则/定额", 150), ("detail", "范围/水源/方式", 240)):
            self.table.heading(col, text=cap)
            self.table.column(col, width=int(w * scale), stretch=True)
        ysb = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        xsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        self.table.bind("<<TreeviewSelect>>", self.load_selected_into_editor)
        self.table.bind("<Double-1>", self.load_selected_into_editor)
        table_btns = ttk.Frame(inner)
        table_btns.pack(fill="x")
        ttk.Button(table_btns, text="移除选中行（不动文件）",
                   command=self.remove_selected).pack(side="left")
        ttk.Button(table_btns, text="预检（validate）", command=self.validate_table).pack(
            side="left", padx=6)
        ttk.Button(table_btns, text="查看模板与字段", command=self.show_templates).pack(side="left")

        # ---- 单行情景编辑（随模板显隐；中文选项映射后台内部值） ----------
        editor = ttk.LabelFrame(inner, text="编辑情景 · 选中表格中的一行进行修改", padding=14)
        editor.pack(fill="x", pady=4)
        self.vars = {}
        self._display_vars = {}
        self._field_widgets = {}
        self._editing_index = None
        fields = (("scenario", "情景名称", "entry"),
                  ("base_project", "基础项目库", "entry"),
                  ("kind", "模板", "kind"),
                  ("mode", "模式", "mode"),
                  ("landuse", "地类名称", "choice"),
                  ("scope", "HRU连接编号（逗号分隔）", "entry"),
                  ("rule_name", "", "choice"),         # 文案随模板切换；候选=项目已有规则/新名自由
                  ("reservoir_name", "水库对象（可选）", "choice"),
                  ("amount_mm", "定额（mm/次）", "entry"),
                  ("source_type", "水源", "source_type"),
                  ("source_obj_id", "取水对象编号", "choice"),
                  ("application_name", "灌溉方式", "choice"),
                  ("stress_threshold", "胁迫阈值（0–1）", "entry"),
                  ("max_per_year", "年最大次数（≥1）", "entry"),
                  ("parameter_identity", "参数来源说明", "entry"))
        self._captions = {key: caption for key, caption, _ in fields}
        self._kinds = {key: kind for key, _, kind in fields}
        self._candidate_widgets = set()
        for index, (key, caption, _kind) in enumerate(fields):
            var = tk.StringVar()
            self.vars[key] = var
            label = ttk.Label(editor, text=caption, wraplength=ui.px(self, 150))
            # 待修2：两组/行，避免第三列横向越界（1列标签+1列输入为一组）
            label.grid(row=index // 2, column=(index % 2) * 2, sticky="nw", padx=(5, 1))
            width = 1
            if _kind == "entry":
                widget = ttk.Entry(editor, textvariable=var, width=width)
            else:
                if _kind in ("kind", "mode", "source_type"):
                    # 中文显示 ↔ 后台内部值：独立显示变量+同步（映射键）
                    display = tk.StringVar()
                    self._display_vars[key] = display
                    widget = ttk.Combobox(editor, textvariable=display, width=18, state="readonly",
                                          values=[c[0] for c in {"kind": TEMPLATE_CHOICES,
                                                                 "mode": MODE_CHOICES,
                                                                 "source_type": SOURCE_CHOICES}[_kind]])
                    display.trace_add("write", lambda _a, _b, _c, k=key: self._display_changed(k))
                    display.set(_display_for(key, ""))
                else:
                    # 待修1：editable候选Combobox直接绑定内部var（application/rule/reservoir/
                    # landuse/source_obj_id 无中文映射）——控件值即提交值，不另建状态
                    widget = ttk.Combobox(editor, textvariable=var, width=width)
                    self._candidate_widgets.add(key)
            widget.grid(row=index // 2, column=(index % 2) * 2 + 1, padx=(1, 8),
                        pady=(1, 1), sticky="we")
            self._field_widgets[key] = {"label": label, "widget": widget}
        editor.grid_columnconfigure(1, weight=1)
        editor.grid_columnconfigure(3, weight=1)
        self.vars["kind"].trace_add("write", lambda _a, _b, _c: self._apply_template_layout())
        self.vars["source_type"].trace_add(
            "write", lambda _a, _b, _c: self._refresh_source_candidates())
        row_btns = ttk.Frame(editor)
        self._row_actions = row_btns
        row_btns.grid(row=(len(fields) + 1) // 2, column=0, columnspan=4, pady=(6, 0),
                      sticky="w")
        ttk.Button(row_btns, text="追加/更新到表", command=self.commit_row).pack(side="left", padx=6)
        ttk.Button(row_btns, text="校验此行", command=self.validate_row).pack(side="left")
        self.project_info = tk.StringVar(value="尚未选择项目；浏览或提交行后将显示项目时段/对象/规则。")
        self._project_info_label = ttk.Label(editor, textvariable=self.project_info,
                                            wraplength=ui.px(self, 850), justify="left", style="Hint.TLabel")
        self._project_info_label.grid(row=(len(fields) + 1) // 2 + 1, column=0,
                                      columnspan=4, sticky="ew", pady=(8, 0))
        ui.wrap_label(self._project_info_label)
        self._apply_project_candidate_values()
        self._apply_template_layout()

        # ---- 计划与输出策略（真实反映当前选择；过期标记） ----------
        plan_frame = ttk.LabelFrame(inner, text="运行计划与输出策略", padding=8)
        plan_frame.pack(fill="x", pady=4)
        plan_row1 = ttk.Frame(plan_frame)
        plan_row1.pack(fill="x")
        ttk.Label(plan_row1, text="输出策略").pack(side="left")
        self.strategy = tk.StringVar(value="monthly_summary")
        ttk.Combobox(plan_row1, textvariable=self.strategy, state="readonly",
                     values=("monthly_summary", "preserve"), width=18).pack(side="left", padx=6)
        ttk.Label(plan_row1, text="开始年(留空=项目设置)").pack(side="left", padx=(8, 1))
        self.period_start = tk.StringVar()
        ttk.Entry(plan_row1, textvariable=self.period_start, width=7).pack(side="left")
        ttk.Label(plan_row1, text="结束年").pack(side="left", padx=(8, 1))
        self.period_end = tk.StringVar()
        ttk.Entry(plan_row1, textvariable=self.period_end, width=7).pack(side="left")
        ttk.Label(plan_row1, text="预热年").pack(side="left", padx=(8, 1))
        self.period_warmup = tk.StringVar()
        ttk.Entry(plan_row1, textvariable=self.period_warmup, width=5).pack(side="left")
        ttk.Label(plan_row1, text="失败处理").pack(side="left", padx=(8, 1))
        self.on_failure_display = tk.StringVar(value=FAILURE_CHOICES[0][0])
        ttk.Combobox(plan_row1, textvariable=self.on_failure_display, state="readonly",
                     values=[c[0] for c in FAILURE_CHOICES], width=26).pack(side="left")
        self.on_failure_display.trace_add("write", lambda _a, _b, _c: self._mark_plan_stale())
        plan_row2 = ttk.Frame(plan_frame)
        plan_row2.pack(fill="x", pady=(5, 0))
        ttk.Button(plan_row2, text="生成运行计划", command=self.make_plan).pack(side="left")
        self.plan_caption = (
            "计划未生成。生成后若修改情景表/策略/时段/失败处理，会标记过期，请重新生成。")
        self.plan_text = ttk.Label(plan_row2, text=self.plan_caption, anchor="w", justify="left",
                                  wraplength=1100, foreground="#163B67")
        self.plan_text.pack(side="left", fill="x", expand=True, padx=(10, 0))
        for var in (self.strategy, self.period_start, self.period_end, self.period_warmup):
            var.trace_add("write", lambda *_a: self._mark_plan_stale())

        # ---- 运行/停止（固定可见） ----------
        run_frame = ttk.LabelFrame(footer, text="运行控制", padding=10)
        run_frame.pack(fill="x", pady=(6, 2))
        self.run_button = ttk.Button(run_frame, text="运行批处理（官方对照）", command=self.start_batch)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(run_frame, text="停止（阶段边界/工作台可终止）",
                                      command=self.request_stop, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(run_frame, text="查看项目设置时段", command=self.show_period).pack(side="left")
        self.progress = tk.StringVar(value="")
        ttk.Label(run_frame, textvariable=self.progress).pack(side="left", padx=12)

        # ---- 结果（final_status统一状态；不完整/缺失≠0） ----------
        result_frame = ttk.LabelFrame(self.results_page, text="结果与报告", padding=14)
        result_frame.pack(fill="both", expand=True)
        result_wrap = ttk.Frame(result_frame)
        result_wrap.pack(side="left", fill="both", expand=True)
        self.result_text = tk.Text(result_wrap, height=12, wrap="word", relief="flat",
                                   background="#F4F7FB")
        self.result_text.insert("1.0", "尚未运行。可选择“载入已有结果…”查看既有批次（无需重新模拟）。")
        self.result_text.configure(state="disabled")
        self.result_text.pack(side="left", fill="both", expand=True)
        rsb = ttk.Scrollbar(result_wrap, orient="vertical", command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=rsb.set)
        rsb.pack(side="right", fill="y")
        open_btns = ttk.Frame(result_frame)
        open_btns.pack(side="left", fill="y", padx=6)
        ttk.Button(open_btns, text="载入已有结果…", command=self.load_existing_result).pack(
            fill="x", pady=2)
        for caption, attr in (("打开报告", "report_path"), ("打开CSV", "csv_path"),
                              ("打开结果目录", "workdir_path")):
            ttk.Button(open_btns, text=caption, command=lambda a=attr: self.open_path(a)).pack(
                fill="x", pady=2)

        self.status_message = tk.StringVar(value="选择项目→编辑/导入情景→预检→计划→运行。"
                                                "耗时步骤不阻塞界面；停止为阶段边界/工作台终止，"
                                                "官方导出需在阶段末响应。")
        ui.wrap_label(ttk.Label(footer, textvariable=self.status_message,
                               style="Hint.TLabel", justify="left")).pack(fill="x", pady=(6, 0))

    # ------------------------------------------------------------ 窗口/滚动
    def _ui_scale(self):
        try:
            return max(1.0, float(self.tk.call("tk", "scaling")) / (96 / 72))
        except Exception:
            return 1.0

    def _apply_scaled_geometry(self):
        ui.fit_window(self, 1140, 820)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _say(self, text):
        self.status_message.set(text)

    # ------------------------------------------------------------ 输入映射
    def _display_changed(self, key):
        display = self._display_vars[key].get().strip()
        if not display:
            return
        internal = _internal_for(key, display)
        if self.vars[key].get() != internal:   # 防显示↔内部 tracer 互写
            self.vars[key].set(internal)

    def _sync_displays(self):
        for key, display in self._display_vars.items():
            value = _display_for(key, self.vars[key].get())
            if display.get() != value:         # 防显示↔内部 tracer 互写
                display.set(value)

    def _apply_template_layout(self):
        kind = self.vars["kind"].get().strip() or "constrained_irrigation"
        reservoir_only = {"reservoir_name"}
        irrigation_only = {"amount_mm", "source_type", "source_obj_id", "application_name",
                           "stress_threshold", "max_per_year", "landuse", "scope"}
        visible_index = 0
        for key, parts in self._field_widgets.items():
            visible = not ((kind == "reservoir_release_rule" and key in irrigation_only) or
                           (kind == "constrained_irrigation" and key in reservoir_only))
            if visible:
                parts["label"].grid(row=visible_index // 2, column=(visible_index % 2) * 2)
                parts["widget"].grid(row=visible_index // 2, column=(visible_index % 2) * 2 + 1)
                visible_index += 1
            else:
                parts["widget"].grid_remove()
                parts["label"].grid_remove()
        if hasattr(self, "_row_actions"):
            self._row_actions.grid(row=(visible_index + 1)//2, column=0, columnspan=4)
        if hasattr(self, "_project_info_label"):
            self._project_info_label.grid(row=(visible_index + 1)//2 + 1, column=0, columnspan=4)
        rule_caption = ("放水规则" if kind == "reservoir_release_rule" else "新灌溉规则名称")
        self._captions["rule_name"] = rule_caption
        self._field_widgets["rule_name"]["label"].configure(text=rule_caption)
        self._apply_project_candidate_values()
        self._sync_displays()

    def _apply_project_candidate_values(self):
        """待修1：项目候选进控件——按 project_choices 只读结果填充候选下拉；
        editable Combobox 手填非法值仍由4A校验按实际所填拒绝，不回退旧值。"""
        info = self._project_info or {}
        def put(key, values):
            if key in self._candidate_widgets:
                self._field_widgets[key]["widget"].configure(
                    values=[str(v) for v in (values or [])])
        put("application_name", info.get("irr_ops"))
        # 放水规则模板→项目已有放水规则按名选择；受限灌溉→新规则名自由填写（不给候选）
        put("rule_name", info.get("release_rules")
            if self.vars["kind"].get().strip() == "reservoir_release_rule" else [])
        put("reservoir_name", info.get("reservoirs"))
        put("landuse", info.get("landuses"))
        self._refresh_source_candidates()

    def _refresh_source_candidates(self):
        info = self._project_info or {}
        source = self.vars["source_type"].get().strip() or "res"   # 行默认 res
        ids = {"res": info.get("res_ids"), "cha": info.get("cha_ids"),
               "aqu": info.get("aqu_ids")}.get(source)
        if "source_obj_id" in self._candidate_widgets:
            self._field_widgets["source_obj_id"]["widget"].configure(
                values=[str(v) for v in (ids or [])])

    # ------------------------------------------------------------ 导航/文件
    def _queue_task(self, work, done):
        if self.busy:
            self._say("已有后台任务在运行（预检/计划/时段读取），请等待完成。")
            return False

        def settle(message):
            if message and message[0] == "progress":
                if hasattr(self, "progress"):
                    self.progress.set(str(message[1]))
                return False          # 进度消息：继续等待最终结果
            self.busy = False
            done(message)
            return True

        if getattr(self, "synchronous", False):
            events = queue.Queue()
            work(events)
            while not settle(events.get_nowait()):
                pass
            return True
        self.busy = True
        events = queue.Queue()

        def poll():
            try:
                message = events.get_nowait()
            except queue.Empty:
                self.after(120, poll)
                return
            settle(message) or self.after(120, poll)
        threading.Thread(target=work, args=(events,), daemon=True).start()
        self.after(120, poll)
        return True

    def open_path(self, path_attr):
        import os
        path = getattr(self, path_attr, None)
        if not path or not Path(path).exists():
            messagebox.showerror("路径不存在", f"实际路径：{path or '未生成'}\n"
                                           "可改从“载入已有结果…”选择有效批次。", parent=self)
            return
        os.startfile(path)

    def choose_base_file(self, initial="res01.sqlite"):
        path = filedialog.askopenfilename(parent=self, title="选择Editor项目库",
                                          initialdir=str(self.root_path / "models"),
                                          filetypes=[("项目库", "*.sqlite")])
        return path or None

    def _busy_guard(self):
        """批处理运行中：锁住会改动情景表的编辑操作（运行/停止除外）。"""
        if getattr(self, "batch_running", False):
            messagebox.showinfo("批处理运行中",
                                "后台运行已开始：情景表编辑已锁定；如需中止请点“停止”。", parent=self)
        return getattr(self, "batch_running", False)

    # ------------------------------------------------------------------ 项目信息
    def browse_base_project(self):
        if self._busy_guard():
            return
        path = self.choose_base_file()
        if not path:
            return
        self.vars["base_project"].set(path)
        self.show_project_info(path)

    def show_project_info(self, path):
        """只读展示：项目名/时段/预热/水库/规则/地类/灌溉方式/水源对象（不写库、不猜值）。"""
        if getattr(self, "batch_running", False):
            return
        base = Path(path)
        if not base.is_absolute():
            base = Path(self.root_path) / base
        if not base.is_file():
            self.project_info.set(f"基础项目不存在：{base}")
            return
        if self._project_info_path == str(base) and self._project_info:
            self._apply_project_info(self._project_info)
            return

        def work(events):
            try:
                events.put(("ok", ss.project_choices(base)))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                self.project_info.set("项目信息读取失败：" + outcome[1])
                return
            info = outcome[1]
            self._project_info = info
            self._project_info_path = str(base)
            self._apply_project_info(info)

        self._queue_task(work, done)

    def _apply_project_info(self, info):
        # 允许部分信息（合成信息/旧缓存）：缺失键按空处理，不抛 KeyError
        self._project_info = dict(info)          # 模板/水源切换时以同源数据刷新候选
        empty = ("", None)
        project_name = info.get("project_name") or empty[0] or \
            (Path(info["project_db"]).name if info.get("project_db") else "未知项目")
        parts = [f"项目：{project_name}"]
        if info.get("start_year") is not None:
            parts.append(f"时段 {info['start_year']}–{info['end_year']}"
                         f"（预热{info['warmup_years']}）")
        parts.append(f"水库{len(info.get('reservoirs') or [])}个" +
                     (f"：{'、'.join(map(str, info['reservoirs']))}" if info.get("reservoirs") else ""))
        rules = info.get("release_rules") or []
        parts.append(f"放水规则{len(rules)}个" + (f"（如 {('、'.join(map(str, rules[:3])))}"
                                             + ("…" if len(rules) > 3 else "") + "）" if rules else ""))
        parts.append("地类：" + ("、".join(map(str, info.get("landuses") or [])) or "无"))
        ops = info.get("irr_ops") or []
        parts.append("灌溉方式：" + ("、".join(map(str, ops)) or "无"))
        parts.append(f"水源对象 res{len(info.get('res_ids') or [])}"
                     f"/cha{len(info.get('cha_ids') or [])}"
                     f"/aqu{len(info.get('aqu_ids') or [])}")
        if info.get("weather_error"):
            parts.append("天气目录未能解析：" + info["weather_error"])
        if info.get("period_error"):
            parts.append("时段读取异常：" + info["period_error"])
        self.project_info.set("；".join(parts))
        self._apply_project_candidate_values()

    # ------------------------------------------------------------------ table
    def load_teaching_table(self):
        if self._busy_guard():
            return
        path = self.root_path / "config/management_scenarios.json"
        if not path.is_file():
            messagebox.showerror("教学表不存在",
                                 f"实际路径：{path}\n可另行导入JSON/CSV。", parent=self)
            return
        self.rows = ss.load_scenario_table(path)
        self._refresh_table()
        self._mark_plan_stale()
        self._say(f"已载入教学情景表 {len(self.rows)} 行（教学参数，须自标识）。")

    def import_table(self):
        if self._busy_guard():
            return
        path = filedialog.askopenfilename(parent=self, title="导入情景表",
                                          filetypes=[("情景表", "*.json *.csv"), ("全部", "*.*")])
        if not path:
            return
        try:
            self.rows = ss.load_scenario_table(path)
            problems = ss.validate_scenario_table(self.rows)
        except Exception as error:
            messagebox.showerror("读取失败", str(error), parent=self)
            return
        self._refresh_table()       # 稳定行号iid：重复情景名不再触发TclError
        self._mark_plan_stale()
        if problems:
            messagebox.showwarning("情景表校验问题", "\n".join(problems[:12]), parent=self)
        self._say(f"已载入 {len(self.rows)} 行" + ("；存在校验问题，见弹窗（表格仍可修正）。"
                                               if problems else "。"))
        if self.rows:
            self.show_project_info(self.rows[0].get("base_project", ""))

    def export_table(self, fmt="json"):
        if self._busy_guard():
            return
        if not self.rows:
            messagebox.showerror("无数据", "请先载入或编辑情景行。", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension="." + fmt,
                                            initialfile="scenarios." + fmt,
                                            filetypes=[(fmt.upper(), "*." + fmt)])
        if not path:
            return
        ss.export_scenario_table(self.rows, path)
        self._say(f"已导出：{path}")

    def export_table_csv(self):
        self.export_table(fmt="csv")

    def _refresh_table(self):
        """行号作为稳定iid：同名行/重复导入不再碰撞。渲染不做选择保持——
        由调用方（commit）按情景名显式恢复，避免更新后选中落在别行。"""
        self.table.delete(*self.table.get_children())
        for index, row in enumerate(self.rows):
            base = row.get("base_project", "")
            kind = row.get("kind", "")
            detail = f"{row.get('landuse', '') or row.get('scope', '') or '—'} / {row.get('source_type', 'res')} {row.get('source_obj_id', '')} / {row.get('application_name', '')}"
            rule = (row.get("rule_name") or "—") + (f" {row.get('amount_mm')}mm" if row.get("amount_mm") else "")
            values = (row.get("scenario", "?"), base, _display_for("kind", kind),
                      _display_for("mode", row.get("mode", "replace")), rule, detail)
            self.table.insert("", "end", iid=str(index), values=values)

    def _select_row_by_name(self, name, refill=False):
        """按情景名把选中与（可选）编辑器定位到同一条情景。"""
        for index, row in enumerate(self.rows):
            if row.get("scenario") == name:
                self._suppress_refill_message = True   # 提交文案不被回填文案覆盖
                self.table.selection_set(str(index))
                self.table.see(str(index))
                if refill:
                    self.load_selected_into_editor()
                if getattr(self, "synchronous", False):
                    self._suppress_refill_message = False   # 事件已同步处理完
                else:
                    self.after(300, lambda: setattr(self, "_suppress_refill_message", False))
                return index
        return None

    def _selected_indexes(self):
        indexes = []
        for iid in self.table.selection():
            if str(iid).isdigit():
                index = int(iid)
                if 0 <= index < len(self.rows):
                    indexes.append(index)
        return sorted(indexes)

    def remove_selected(self):
        if self._busy_guard():
            return
        indexes = self._selected_indexes()
        if not indexes:
            messagebox.showinfo("未选择", "请先在表中选中一行（只移除表行，不删除文件）。", parent=self)
            return
        names = [self.rows[i].get("scenario") for i in indexes]
        self.rows = [row for index, row in enumerate(self.rows) if index not in set(indexes)]
        self._refresh_table()
        self._mark_plan_stale()
        self._say(f"已移除表行：{names}；不删除任何已有文件。")

    def show_templates(self):
        templates = ss.list_templates()
        lines = ["支持模板与字段/单位："]
        for item in templates["templates"]:
            lines.append(f"· {item['kind']}：必填 {item['required']}；可选 {item['optional']}")
            lines.append(f"   单位：{item['units']}")
        lines.append(f"模式：{templates['application_modes']}"
                     "（replace替换选中范围；append追加并列列出old+new）")
        lines.append("边界：每行仅一种模板操作；HRU编号为连接对象号（hru_con.id）。")
        messagebox.showinfo("模板与字段", "\n".join(lines), parent=self)

    # ------------------------------------------------------- 行编辑与回填
    def load_selected_into_editor(self, event=None):
        """选中/双击表行 → 完整原始row字典回填全部字段（不从截断的显示文本重建）。"""
        indexes = self._selected_indexes()
        if not indexes:
            return
        row = dict(self.rows[indexes[0]])
        self._editing_index = indexes[0]
        for key, var in self.vars.items():
            value = row.get(key, "")
            var.set("" if value is None else str(value))
        self._apply_template_layout()
        if not getattr(self, "_suppress_refill_message", False):
            self._say(f"已回填第{indexes[0] + 1}行（{row.get('scenario')}）；"
                      "仅改需要的字段后点“追加/更新到表”。")
        if row.get("base_project"):
            self.show_project_info(row["base_project"])

    def validate_row(self):
        row = self._row_from_editor()
        problems = ss.validate_scenario_row(row)
        if problems:
            messagebox.showwarning("此行有问题", "\n".join(problems), parent=self)
        else:
            self._say("此行校验通过（仅结构/引用；运行前还会整体校验）。")

    def _row_from_editor(self):
        row = {key: var.get().strip() for key, var in self.vars.items() if var.get().strip()}
        return row

    def commit_row(self):
        if self._busy_guard():
            return
        row = self._row_from_editor()
        problems = ss.validate_scenario_row(row)
        if problems:
            messagebox.showwarning("此行未能通过校验", "\n".join(problems), parent=self)
            return
        name = row["scenario"]
        editing_index = getattr(self, "_editing_index", None)
        updating = (isinstance(editing_index, int) and 0 <= editing_index < len(self.rows))
        if updating:
            # 待修1：正在编辑的同一条情景就地更新（保持原行位置与选中，不跳到别行）
            self.rows[editing_index] = row
        else:
            self.rows = [r for r in self.rows if r.get("scenario") != name]
            self.rows.append(row)
        self._refresh_table()
        self._mark_plan_stale()
        self._select_row_by_name(name)     # 选中回到刚提交的这条情景
        note = ""
        if updating and self.rows[editing_index].get("scenario") != name:
            note = "（该行已改名；更新发生在原行位置）"
        self._editing_index = None
        self._say(f"已加入/更新情景 {name}（就地更新，选中保持）。{note}")
        self.show_project_info(row.get("base_project", ""))

    # ------------------------------------------------------- validate & plan
    def validate_table(self):
        if self._busy_guard():
            return
        snapshot = [dict(r) for r in self.rows]

        def work(events):
            try:
                problems = ss.validate_scenario_table(snapshot)
                events.put(("ok", problems))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                messagebox.showerror("校验失败", outcome[1], parent=self)
                return
            problems = outcome[1]
            if problems:
                messagebox.showwarning("情景表校验问题（不调用模型）", "\n".join(problems[:12]),
                                       parent=self)
                self._say(f"校验未通过（{len(problems)}项）：请按提示修正后重新校验。")
            else:
                self._say("校验通过：结构/引用/范围/天气均无问题（未调用模型）。")
        self._queue_task(work, done)

    def _parameter_snapshot(self):
        return {"rows": [dict(r) for r in self.rows],
                "strategy": self.strategy.get().strip() or "monthly_summary",
                "start": self.period_start.get().strip(),
                "end": self.period_end.get().strip(),
                "warmup": self.period_warmup.get().strip(),
                "on_failure": _internal_for("on_failure", self.on_failure_display.get().strip())}

    def _current_signature(self, snapshot=None):
        snapshot = snapshot or self._parameter_snapshot()
        return json.dumps(snapshot, sort_keys=True, ensure_ascii=False)

    def _mark_plan_stale(self):
        if self._plan_signature is not None and \
                self._plan_signature != self._current_signature():
            self.plan_text.configure(
                text="计划已过期（情景表/策略/时段/失败处理已变化）——请重新生成。",
                foreground="#AD4B45")

    def _period_problems(self, snapshot):
        """时段输入在模型调用前检查：年份整数、起止顺序、预热为非负年数（至少留1个评估年）。"""
        problems = []

        def as_constrained(caption, raw, lo, hi):
            if raw == "":
                return None
            try:
                value = int(raw)
                assert lo <= value <= hi
                return value
            except (ValueError, AssertionError):
                problems.append(f"{caption}应为{lo}–{hi}的整数，得到：{raw or '空'}")
                return None

        start = as_constrained("开始年", snapshot["start"], 1901, 2099)
        end = as_constrained("结束年", snapshot["end"], 1901, 2099)
        warmup = as_constrained("预热年", snapshot["warmup"], 0, 100)
        if start is not None and end is not None and end < start:
            problems.append(f"结束年 {end} 早于开始年 {start}")
        if warmup is not None and start is not None and end is not None \
                and warmup > end - start:
            problems.append(f"预热年 {warmup} 超出可用范围（须≤ {end - start}，"
                            "至少保留1个评估年）")
        return problems

    def make_plan(self):
        if self._busy_guard():
            return
        snapshot = self._parameter_snapshot()
        problems = self._period_problems(snapshot)
        if problems:
            messagebox.showwarning("时段输入有误（未生成计划/未调用模型）", "\n".join(problems),
                                   parent=self)
            return
        if not snapshot["rows"]:
            messagebox.showinfo("无情景", "请先载入或编辑情景行。", parent=self)
            return
        preview_dir = Path(self.root_path) / "runs/scenario_plan_preview"

        def work(events):
            try:
                plan = ss.plan_scenarios(self._dump_table(snapshot["rows"]), preview_dir)
                events.put(("ok", plan))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                self.plan_text.configure(text="计划不可生成：" + str(outcome[1]))
                return
            plan = outcome[1]
            self._plan_signature = self._current_signature(snapshot)
            strategy, flags = STRATEGY_TEXT.get(snapshot["strategy"],
                                                (snapshot["strategy"], None))
            period = (f"覆盖时段 {snapshot['start']}–{snapshot['end']}"
                      f"（预热{snapshot['warmup'] or '项目设置'}）"
                      if snapshot["start"] or snapshot["end"] or snapshot["warmup"]
                      else "按各情景基础项目设置")
            lines = [f"情景 {plan['scenarios']} 个；每情景 {plan['model_calls_per_scenario']}"
                     f"次模型调用（工作台+官方对照）；共 {plan['total_model_calls']} 次。",
                     f"输出策略：{strategy}" + (f" {flags}" if flags else ""),
                     f"时段：{period}｜失败处理：{_display_for('on_failure', snapshot['on_failure'])}",
                     "结果目录：runs/scenario_gui_batch/<运行时刻自动新建，不覆盖旧成果>",
                     "注：精简月汇总仍会在执行前开启 basin_wb 必要年输出。"]
            self.plan_text.configure(text=" ".join(lines), foreground="#163B67")
        self._queue_task(work, done)

    def _dump_table(self, rows):
        table = self.root_path / "runs/_scenario_gui_table.json"
        ss.export_scenario_table(rows, table)
        return table

    def show_period(self):
        if self._busy_guard():
            return
        if not self.rows:
            self._say("请先载入或编辑情景行，再查看其基础项目设置时段。")
            return
        base = Path(self.rows[0].get("base_project", ""))
        if not base.is_absolute():       # 情景表可存相对路径（相对工作台根目录）
            base = Path(self.root_path) / base

        def work(events):
            try:
                settings = ss.default_run_settings(base)
                events.put(("ok", settings))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            if outcome[0] == "error":
                self._say("无法读取时段：" + outcome[1])
                return
            settings = outcome[1]
            self._say(f"基础项目时段 {settings['start_year']}–{settings['end_year']}，"
                      f"预热 {settings['warmup_years']} 年（来自 {base.name}）。")
        self._queue_task(work, done)

    # ------------------------------------------------------------- run / stop
    def request_stop(self):
        if not getattr(self, "batch_running", False):
            self._say("当前没有运行中的批处理。")
            return
        self.cancel_event.set()
        self._say("已请求停止：工作台运行可终止，官方导出/直接运行在阶段边界响应；"
                  "未执行项会记录。")

    def start_batch(self):
        if self._busy_guard():
            return
        if not self.rows:
            messagebox.showinfo("无情景", "请先载入或编辑情景行。", parent=self)
            return
        snapshot = self._parameter_snapshot()
        problems = self._period_problems(snapshot)
        if problems:
            messagebox.showwarning("时段输入有误（未调用模型）", "\n".join(problems), parent=self)
            return
        # 预检成功且任务真正提交后才进入 batch_running；预检失败后台复核后同样复位
        self.batch_running = True
        self.cancel_event = threading.Event()
        self.run_button.state(("disabled",))
        self.stop_button.state(("!disabled",))
        table = self._dump_table(snapshot["rows"])
        import datetime
        batch_dir = (Path(self.root_path) / "runs/scenario_gui_batch" /
                     datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.progress.set("后台预检+运行中…")

        def work(events):
            def progress_cb(scenario, stage):
                events.put(("progress", f"{scenario}: {stage}"))
            try:
                problems = ss.validate_scenario_table(snapshot["rows"], WORKSPACE)
                if problems:
                    events.put(("precheck_failed", problems))
                    return
                result = ss.run_scenarios(table, batch_dir, on_failure=snapshot["on_failure"],
                                          cancel_event=self.cancel_event,
                                          progress=progress_cb, work_root=WORKSPACE,
                                          output_strategy=snapshot["strategy"],
                                          start_year=snapshot["start"] or None,
                                          end_year=snapshot["end"] or None,
                                          warmup_years=snapshot["warmup"] or None)
                events.put(("done", result))
            except Exception as error:
                events.put(("error", str(error)))

        def done(outcome):
            self.batch_running = False
            self.run_button.state(("!disabled",))
            self.stop_button.state(("disabled",))
            if outcome[0] == "precheck_failed":
                self.progress.set("预检未通过，未调用模型。")
                messagebox.showwarning("预检未通过（未调用模型，可修正后再次提交）",
                                       "\n".join(outcome[1][:12]), parent=self)
                self._say(f"预检未通过（{len(outcome[1])}项）；修正后可直接再次运行。")
                return
            if outcome[0] == "error":
                self.progress.set("批处理失败：" + outcome[1])
                messagebox.showerror("批处理失败", outcome[1], parent=self)
                return
            self._show_result(outcome[1])
        if not self._queue_task(work, done):
            # 其他后台任务占线：本次运行没有发起，恢复按钮态与可编辑状态
            self.batch_running = False
            self.run_button.state(("!disabled",))
            self.stop_button.state(("disabled",))

    # ------------------------------------------------------------- 结果
    def _show_result(self, result):
        self.last_result = result
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        results = result.get("results") or []
        failures = result.get("failures") or []
        not_executed = result.get("not_executed") or []
        report = result.get("report") or {}
        self.report_path = report.get("report")
        self.csv_path = report.get("csv")
        self.workdir_path = result.get("work_dir")
        lines = [f"情景 {result.get('rows', len(results))} 个｜失败 {len(failures)}"
                 f"｜未执行 {len(not_executed)}。", ""]
        if not results and not failures:
            lines.append("没有已执行的情景（全部未执行/取消）；未调用或未完成模型运行。")
            lines.append("")
        for entry in results:
            run = entry.get("run") or {}
            summary = entry.get("summary") or {}
            final = ss.final_status(entry)
            name = summary.get("scenario") or run.get("scenario") or "?"
            compare = summary.get("comparison_passed", run.get("comparison_passed"))
            compare_cn = "通过" if compare is True else ("未通过" if compare is False else "无对照信息")
            line = f"· {name}｜{STATUS_CN.get(final, final)}（{final}）｜官方对照：{compare_cn}"
            rules = summary.get("enabled_rules") or summary.get("release_rule")
            if isinstance(rules, (list, tuple)):
                rules = "、".join(str(x) for x in rules) if rules else "—"
            line += f"｜启用规则：{rules or '—'}"
            field = summary.get("field_irrigation_m3_efficiency_after")
            if field is None:
                line += "｜田间灌溉：缺失/不完整"
            else:
                line += f"｜田间灌溉：{field} m³（效率后，面积加权）"
            reservoir = summary.get("reservoir_monthly_by_object")
            if reservoir == "not_applicable":
                line += "｜水库：不适用"
            elif isinstance(reservoir, dict) and reservoir:
                parts = []
                for unit, item in reservoir.items():
                    part = f"{unit}：{item.get('months')}月"
                    if item.get("incomplete_reason"):
                        part += f"（不完整：{item['incomplete_reason']}）"
                    parts.append(part)
                line += "｜水库：" + "；".join(parts)
            else:
                line += "｜水库：缺失/不适用"
            period = summary.get("period") or run.get("actual_period")
            if isinstance(period, dict) and period:
                p_start = period.get("start", period.get("start_year"))
                p_end = period.get("end", period.get("end_year"))
                p_warm = period.get("warmup", period.get("warmup_years"))
                line += f"｜时段 {p_start}–{p_end}（预热{p_warm}）"
            if summary.get("parameter_identity"):
                line += f"｜参数身份 {summary['parameter_identity']}"
            if entry.get("error"):
                line += f"｜原因：{entry['error']}"
            lines.append(line)
        for failure in failures:
            lines.append(f"· [失败] {failure.get('scenario')}"
                         f"（{failure.get('type', '失败')}）：{failure.get('error', '')}")
        for item in not_executed:
            reason = item.get("reason")
            reason_cn = REASON_CN.get(reason, reason or "未说明")
            extra = "；".join(f"{k}={v}" for k, v in item.items()
                             if k not in ("scenario", "reason"))
            lines.append(f"· [未执行] {item.get('scenario')}：{reason_cn}"
                         + (f"；{extra}" if extra else ""))
        lines.append("")
        lines.append(f"报告：{self.report_path or '（未生成）'}")
        lines.append(f"CSV：{self.csv_path or '（未生成）'}")
        lines.append(f"结果目录：{self.workdir_path or '（无）'}")
        self.result_text.insert("1.0", "\n".join(lines))
        self.result_text.configure(state="disabled")
        self.progress.set("批处理结束。" if results else "批处理结束（无已执行项）。")
        if hasattr(self, "tabs"):
            self.tabs.select(self.results_page)

    def load_existing_result(self):
        """读取既有批次的 scenario_summary.json 直接展示（完全复用，不重算指标）。"""
        if self._busy_guard():
            return
        path = filedialog.askopenfilename(
            parent=self, title="选择已有批次的 scenario_summary.json",
            filetypes=[("情景结果", "scenario_summary*.json"), ("JSON", "*.json"),
                       ("全部", "*.*")])
        if not path:
            return

        def work(events):
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception as error:
                events.put(("error", str(error)))
                return
            batch_dir = Path(path).parent
            report = batch_dir / "report.md"
            csv = batch_dir / "scenario_summary.csv"
            results = data.get("results", [])
            not_executed = data.get("not_executed", [])
            # 待修3：旧结果JSON只持久化results/not_executed——失败数从final_status推导，
            # 总数=已执行+未执行；与实时批次的rows/failures口径一致，不重复计数。
            failures = []
            for entry in results:
                if ss.final_status(entry) != "completed":
                    summary = entry.get("summary") or {}
                    failures.append({
                        "scenario": summary.get("scenario")
                        or (entry.get("run") or {}).get("scenario") or "?",
                        "type": ss.final_status(entry),
                        "error": entry.get("error")
                        or summary.get("field_irrigation_incomplete_reason")
                        or "状态=" + str(summary.get("status") or (entry.get("run") or {}).get("status"))})
            events.put(("ok", {
                "rows": len(results) + len(not_executed),
                "results": results,
                "failures": failures,
                "not_executed": not_executed,
                "work_dir": str(batch_dir),
                "report": {"report": str(report) if report.is_file() else None,
                           "csv": str(csv) if csv.is_file() else None}}))

        def done(outcome):
            if outcome[0] == "error":
                messagebox.showerror("已有结果读取失败", outcome[1], parent=self)
                return
            self._say(f"已载入既有结果：{path}（未重新运行）。")
            self.progress.set("已载入既有结果（未重新运行）。")
            self._show_result(outcome[1])
        self._queue_task(work, done)
