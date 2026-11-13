"""First-run environment selection and an explicit settings entry for later moves."""
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from portable_runtime import CONFIG, PATH_FIELDS, discover, read_config, save, validate


LABELS = {
    "qgis_root": "QGIS 3.44 安装目录",
    "plugin_root": "QSWAT+ 4.0.4 插件目录",
    "support_root": "SWATPlus 支持目录（数据库、TauDEM）",
    "editor_root": "Editor 4.0.2 安装目录",
    "mpi_dir": "Microsoft MPI 的 Bin 目录",
    "toolbox_executable": "Toolbox 4.0.1 程序（可选）",
    "sufi2_executable_dir": "原版 SUFI-2 程序目录（可选）",
}


def configure_environment(parent=None, initial=None, automatic=False):
    own_root = parent is None
    if own_root:
        from desktop_workbench import enable_high_dpi
        enable_high_dpi()
    window = tk.Tk() if own_root else tk.Toplevel(parent)
    window.title("SWAT+ Workbench · 环境设置")
    import desktop_ui as ui
    ui.fit_window(window, 1040, 560)
    if parent:
        window.transient(parent)
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    ttk.Label(frame, text="连接这台电脑上的建模环境", font=("Microsoft YaHei UI", 15, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
    ttk.Label(frame, text="自动查找已安装的软件。路径有误时可浏览选择，设置保存在本工作台中。",
              wraplength=800).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))
    values = initial if initial is not None else read_config()
    fields = {}
    result = {"saved": False}
    buttons = []
    for row, key in enumerate(PATH_FIELDS, 2):
        ttk.Label(frame, text=LABELS[key]).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        field = fields[key] = tk.StringVar(value=values.get(key, ""))
        ttk.Entry(frame, textvariable=field).grid(row=row, column=1, sticky="ew", pady=6)
        def browse(name=key, variable=field):
            chosen = (filedialog.askopenfilename(parent=window, filetypes=[("程序", "*.exe")])
                      if name == "toolbox_executable" else filedialog.askdirectory(parent=window))
            if chosen:
                variable.set(chosen)
        button = ttk.Button(frame, text="浏览…", command=browse)
        button.grid(row=row, column=2, padx=(8, 0))
        buttons.append(button)
    status = tk.StringVar(value="QGIS、QSWAT+、Editor、数据库、TauDEM 和 MPI 用于完整自动建模。")
    ttk.Label(frame, textvariable=status, wraplength=820).grid(
        row=10, column=0, columnspan=3, sticky="w", pady=12)
    bar = ttk.Frame(frame)
    bar.grid(row=11, column=0, columnspan=3, sticky="e")

    def start_operation(detect=False, close=False):
        for button in buttons:
            button.state(["disabled"])
        current = {key: variable.get().strip() for key, variable in fields.items()}
        status.set("正在查找并检查环境，请稍候…")
        completed = {}
        def worker():
            try:
                cfg = discover(current) if detect else current
                errors = validate(cfg, probe=True)
                completed.update(cfg=cfg, errors=errors)
            except Exception as error:
                completed.update(errors=[str(error)])
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        def finish():
            if thread.is_alive():
                window.after(100, finish)
                return
            for button in buttons:
                button.state(["!disabled"])
            cfg = completed.get("cfg", current)
            for key, variable in fields.items():
                variable.set(cfg.get(key, ""))
            errors = completed["errors"]
            if errors:
                status.set("环境尚需补齐，请选择相应目录。")
                if not detect:
                    messagebox.showerror("环境检查", "\n".join(errors), parent=window)
                return
            if close:
                try:
                    save(cfg)
                except OSError as error:
                    messagebox.showerror("设置保存失败", str(error), parent=window)
                    return
                result["saved"] = True
                window.destroy()
            else:
                status.set("检查通过，点击“保存并使用”保存这台电脑的路径。")
        window.after(100, finish)

    buttons.extend([
        ttk.Button(bar, text="自动查找", command=lambda: start_operation(detect=True)),
        ttk.Button(bar, text="保存并使用", command=lambda: start_operation(close=True)),
    ])
    for button in buttons[-2:]:
        button.pack(side="left", padx=6)
    ttk.Button(bar, text="取消", command=window.destroy).pack(side="left", padx=6)
    if automatic:
        window.after(100, lambda: start_operation(detect=True, close=True))
    if own_root:
        window.mainloop()
    else:
        window.grab_set()
        parent.wait_window(window)
    return result["saved"]
