"""Small engine-selection dialog; changes are saved to a new task configuration."""
import copy
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from workflow_runtime import resolve_runtime_config


class EngineEditor:
    def __init__(self, parent, config_path, root, saved):
        self.root, self.source, self.saved = Path(root), Path(config_path), saved
        self.config = json.loads(self.source.read_text(encoding="utf-8-sig"))
        engine = self.config.get("engine", {})
        catalog = self.root / "config/research_engine_release.json"
        known = json.loads(catalog.read_text(encoding="utf-8-sig")) if catalog.exists() else {}
        custom = copy.deepcopy(known if engine.get("mode", "default") == "default" else engine)
        if custom.get("executable"):
            custom["executable"] = str((self.root / custom["executable"]).resolve())
        default_config = copy.deepcopy(self.config)
        default_config["engine"] = {"mode": "default"}
        effective = resolve_runtime_config(default_config, self.root, check_paths=False)
        model = effective["model"]
        build = effective["engine_info"].get("build_configuration", "") or ""
        official = {
            "executable": model["executable"], "expected_version": model.get("expected_version", ""),
            "label": "官方/默认 SWAT+ " + str(model.get("expected_version", "")),
            "source_revision": "发行版本 " + str(model.get("expected_version", "")) + "；对应源码提交未记录",
            "source_note": "沿用当前环境或原配置指定的程序；此处只读，不修改安装文件",
            "compiler": "Intel ifort（具体版本未记录）" if "ifort" in build.lower() else "原配置未提供编译器信息",
            "build_configuration": build or "原配置未提供构建信息",
            "build_options": "原配置未提供编译选项；以发行程序为准",
        }
        self.profiles = {"default": official, "custom": custom}
        self.window = tk.Toplevel(parent)
        self.window.title("选择模型引擎")
        width = min(1040, self.window.winfo_screenwidth() - 80)
        height = min(760, self.window.winfo_screenheight() - 100)
        self.window.geometry(f"{width}x{height}")
        self.mode = tk.StringVar(value=engine.get("mode", "default"))
        box = ttk.Frame(self.window, padding=18); box.pack(fill="both", expand=True)
        ttk.Label(box, text="选择本任务运行的SWAT+程序", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Radiobutton(box, text="官方/默认引擎（沿用环境或原配置）", variable=self.mode, value="default").pack(anchor="w", pady=(12, 4))
        ttk.Radiobutton(box, text="自编译引擎（优先采用下面指定的程序）", variable=self.mode, value="custom").pack(anchor="w", pady=(0, 10))
        # Reserve the action area before the scrollable form, including at high DPI.
        footer = ttk.Frame(box); footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.hint = tk.StringVar()
        hint = ttk.Label(footer, textvariable=self.hint, wraplength=720)
        hint.pack(anchor="w", fill="x", pady=(0, 12))
        footer.bind("<Configure>", lambda event: hint.configure(wraplength=max(180, event.width)))
        buttons = ttk.Frame(footer); buttons.pack(fill="x")
        self.save_button = ttk.Button(buttons, text="另存配置并使用", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(buttons, text="取消", command=self.window.destroy).pack(side="right", padx=8)
        body = ttk.Frame(box); body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        form = ttk.Frame(self.canvas); form.columnconfigure(1, weight=1)
        form_id = self.canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(form_id, width=event.width))
        self.values, self.entries = {}, {}
        self.displayed_mode = self.mode.get()
        defaults = self.profiles[self.displayed_mode]
        fields = [("executable", "程序exe"), ("expected_version", "输入输出版本"), ("label", "引擎名称"),
                  ("source_revision", "源码版本"), ("source_note", "修改说明"),
                  ("compiler", "编译器"), ("build_configuration", "构建配置"), ("build_options", "编译选项")]
        for row, (key, label) in enumerate(fields):
            value = defaults.get(key, "")
            if key == "expected_version" and not value:
                value = self.config.get("model", {}).get("expected_version", "62.0.1")
            self.values[key] = tk.StringVar(value=value)
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 10))
            if key == "expected_version":
                entry = ttk.Combobox(form, textvariable=self.values[key], values=["60.5.7", "61.0.1", "62.0.1"], state="readonly")
            else:
                entry = ttk.Entry(form, textvariable=self.values[key])
            entry.grid(row=row, column=1, sticky="ew", pady=6)
            self.entries[key] = entry
            if key == "executable":
                self.browse_button = ttk.Button(form, text="浏览…", command=self.browse)
                self.browse_button.grid(row=row, column=2, padx=(8, 0))
        self.mode.trace_add("write", self.switch_mode)
        self.switch_mode()

    def switch_mode(self, *_):
        previous = self.displayed_mode
        self.profiles[previous] = {key: value.get() for key, value in self.values.items()}
        mode = self.mode.get()
        for key, value in self.values.items():
            value.set(self.profiles[mode].get(key, ""))
            self.entries[key].configure(state="readonly" if mode == "default" or key == "expected_version" else "normal")
        self.displayed_mode = mode
        self.browse_button.configure(state="disabled" if mode == "default" else "normal")
        self.hint.set(
            "当前显示：官方/默认配置（只读）。直接另存即可使用；选择自编译后会显示另一套配置。"
            if mode == "default" else
            "当前显示：自编译配置（已预填，可直接使用）。切换后保留本次编辑；启动任务时保存程序副本。"
        )
        self.canvas.yview_moveto(0)

    def browse(self):
        path = filedialog.askopenfilename(parent=self.window, title="选择SWAT+引擎", filetypes=[("Windows程序", "*.exe")])
        if path:
            self.mode.set("custom"); self.values["executable"].set(path)

    def save(self):
        cfg = copy.deepcopy(self.config)
        cfg.pop("engine_info", None); cfg.pop("runtime_info", None)
        cfg["engine"] = {"mode": self.mode.get()}
        if self.mode.get() == "custom":
            cfg["engine"].update({k: v.get().strip() for k, v in self.values.items()})
        try:
            effective = resolve_runtime_config(cfg, self.root)
            cfg.setdefault("model", {}).update(executable=effective["model"]["executable"], expected_version=effective["model"]["expected_version"])
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror("引擎配置不可用", str(error), parent=self.window); return
        path = filedialog.asksaveasfilename(parent=self.window, initialdir=str(self.root / "config"),
                                           initialfile=self.source.stem + "_engine.json", defaultextension=".json", filetypes=[("JSON配置", "*.json")])
        if not path:
            return
        if Path(path).resolve() == self.source.resolve():
            messagebox.showerror("请另存配置", "请选择新文件名，保留当前配置。", parent=self.window); return
        Path(path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.saved(Path(path)); self.window.destroy()
