"""Lightweight native SWAT+ workbench; the existing CLI remains the compute engine.

Startup imports no scientific/plotting packages. Results images load on demand.
Only small task metadata is polled, and live log rendering is bounded.
"""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import desktop_ui as ui
from tkinter import filedialog, messagebox, ttk
from desktop_data import load_config_summary, load_job_summary, list_recent_jobs, job_state_path

WORKSPACE = Path(__file__).resolve().parent.parent
CLI = WORKSPACE / "scripts/swatplus_workflow.py"
PRESETS = {
    "推荐 · Robit自动建模 → Toolbox": "robit_toolbox.json",
    "新版 Robit · 综合分析": "robit_research.json",
    "新版 Robit · SUFI-2分阶段（830组）": "robit_sufi2_staged.json",
    "新版 Robit · SUFI-2小预算检查（24组）": "robit_sufi2.json",
}
if (WORKSPACE / "config/distribution.json").is_file():
    PRESETS = json.loads((WORKSPACE / "config/distribution.json").read_text(encoding="utf-8"))["presets"]
C = {"bg":"#F4F7FB", "surface":"#FFFFFF", "ink":"#163B67", "muted":"#566F90",
     "line":"#DCE6F1", "accent":"#236BC4", "hover":"#1756A5", "tint":"#E9F1FD",
     "nav":"#153F73", "nav_active":"#2565AA", "nav_text":"#BAD2EF", "amber":"#936323",
     "red":"#AD4B45", "log":"#F5F9FF"}
FONT = "Microsoft YaHei UI"


def enable_high_dpi():
    """Opt this process into crisp Windows rendering before the first Tk window."""
    if os.name != "nt":return "platform-default"
    import ctypes
    try:
        context=ctypes.windll.user32.SetProcessDpiAwarenessContext
        context.argtypes=[ctypes.c_void_p];context.restype=ctypes.c_bool
        if context(ctypes.c_void_p(-4)):return "per-monitor-v2"
    except (AttributeError,OSError):pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2)==0:return "per-monitor"
    except (AttributeError,OSError):pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():return "system-aware"
    except (AttributeError,OSError):pass
    return "already-set-or-unavailable"


def python_command():
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe": executable = executable.with_name("python.exe")
    return [str(executable), "-u", "-X", "utf8", str(CLI)]


def number(value):
    return "—" if value is None else f"{value:.3f}"


def date_range(period):
    if not period.get("start") or not period.get("end"): return "未指定"
    return period["start"][:7].replace("-", ".") + " — " + period["end"][:7].replace("-", ".")


def reference_caption(kind, frequency=""):
    if kind=="verified_observation":return "观测来源已核实"+(" · "+frequency+"流量" if frequency else ""),C["accent"]
    if kind=="official_tutorial_reference":return "教学参考流量 · 用于完整流程演示",C["amber"]
    return "参考数据来源待核实",C["amber"]


def algorithm_caption(summary):
    return summary["method_label"]+" · "+summary["parameter_backend_label"]


def search_plan(summary):
    workers=summary["workers"]
    execution="单序列" if summary["method"] in ("spotpy_dds","spotpy_sceua","spotpy_dream") else "顺序运行" if workers==1 else f"{workers or '—'}并行"
    return f"{summary['params_count'] or '—'}参数 / {summary['trials_total'] or '—'}组 / {execution}"


def read_tail(path, maximum=48000):
    with Path(path).open("rb") as stream:
        size=stream.seek(0,2);stream.seek(max(0,size-maximum));data=stream.read(maximum)
    if size>maximum and b"\n" in data: data=data.split(b"\n",1)[1]
    return data.decode("utf-8",errors="replace")


class PhaseStrip(tk.Canvas):
    def __init__(self,parent):
        super().__init__(parent,height=66,bg=C["surface"],highlightthickness=0)
        self.phase,self.status,self.build_enabled="created","created",True
        self.research=False;self.workflow_mode=None
        self.analysis_enabled=(True,True)
        self.bind("<Configure>",lambda _e:self.draw())

    def update_stage(self,phase,status,build_enabled=True,research=False,analysis_enabled=(True,True),workflow_mode=None):
        self.phase,self.status,self.build_enabled=phase,status,build_enabled;self.research=research;self.workflow_mode=workflow_mode
        self.analysis_enabled=analysis_enabled;self.draw()

    def draw(self):
        self.delete("all");width=max(self.winfo_width(),200)
        stages=["build","sensitivity","calibration","fixed_parameter_validation","uncertainty","report"] if self.research else ["build","calibration","fixed_parameter_validation","report"]
        if self.workflow_mode=="toolbox":stages=["build","baseline","toolbox_ready"]
        stages=[stage for stage in stages if not (stage=="sensitivity" and not self.analysis_enabled[0]) and not (stage=="uncertainty" and not self.analysis_enabled[1])]
        current=len(stages) if self.phase=="completed" else stages.index(self.phase) if self.phase in stages else -1
        labels=[{"baseline":"基础试跑","toolbox_ready":"交给Toolbox","build":"构建模型" if self.build_enabled else "载入模型","sensitivity":"敏感性","calibration":"参数搜索","fixed_parameter_validation":"固定参数验证","uncertainty":"不确定性","report":"生成报告"}[stage] for stage in stages]
        step=width/len(stages)
        for i,label in enumerate(labels):
            x=step*(i+0.5);done=i<current;active=i==current
            color=C["accent"] if done or active else "#B6CEE9"
            if active and self.status in ("failed","interrupted","paused"):color=C["amber"]
            if i<len(stages)-1:self.create_line(x+17,20,x+step-17,20,fill=C["accent"] if done else C["line"],width=2)
            self.create_oval(x-12,8,x+12,32,fill=color,outline="")
            self.create_text(x,20,text="✓" if done else str(i+1),fill="white",font=(FONT,9,"bold"))
            self.create_text(x,49,text=label,fill=C["ink"] if done or active else C["muted"],font=(FONT,10))


class Metric(tk.Frame):
    def __init__(self,parent,title,note):
        super().__init__(parent,bg=C["surface"],highlightbackground=C["line"],highlightcolor=C["line"],highlightthickness=1)
        self.title=tk.Label(self,text=title,bg=C["surface"],fg=C["muted"],font=(FONT,10),anchor="w")
        self.title.pack(fill="x",padx=18,pady=(14,5))
        self.value=tk.Label(self,text="—",bg=C["surface"],fg=C["ink"],font=("Segoe UI",24,"bold"),anchor="w")
        self.value.pack(fill="x",padx=18)
        self.note=tk.Label(self,text=note,bg=C["surface"],fg=C["muted"],font=(FONT,10),anchor="w")
        self.note.pack(fill="x",padx=18,pady=(5,14))

    def set(self,value,note=None,title=None):
        self.value.configure(text=value)
        if note is not None:self.note.configure(text=note)
        if title is not None:self.title.configure(text=title)


class Desktop:
    def __init__(self,window):
        self.window=window;self.events=queue.Queue()
        self.running=self.closing=self.stop_requested=self.stop_pending=False
        self.process=None;self.job=None;self.active_page="new";self.job_summary=None
        self._job_stamp=None;self._recent_jobs=[];self._history_loaded=False
        self._external_log_stamp=None
        self._preview=None;self._preview_key=None;self._preview_resize=None
        self.controls=[];self.busy_aux=set()
        window.title("SWAT+ 工作台");window.configure(bg=C["bg"])
        self.ui_scale=max(1.,float(window.tk.call("tk","scaling"))/(96/72))
        sw,sh=window.winfo_screenwidth(),window.winfo_screenheight()
        width=min(int(1280*self.ui_scale),sw-64);height=min(int(850*self.ui_scale),sh-100)
        window.geometry(f"{width}x{height}");window.minsize(min(int(1040*self.ui_scale),sw-64),min(int(710*self.ui_scale),sh-100));window.protocol("WM_DELETE_WINDOW",self.close)
        self.icon=tk.PhotoImage(master=window,width=32,height=32)
        self.icon.put(C["nav"],to=(0,0,32,32))
        for x in range(4,29):
            for offset,color in [(0,"#A3D1FF"),(7,"#5CA5ED"),(14,"#A3D1FF")]:
                y=7+offset+round(2*math.sin(x/4))
                self.icon.put(color,to=(x,y,x+1,y+2))
        window.iconphoto(True,self.icon)
        self.preset=tk.StringVar(value=next(iter(PRESETS)));self.config_path=tk.StringVar();self.name=tk.StringVar()
        self.status=tk.StringVar(value="就绪");self.job_text=tk.StringVar(value="尚未选择任务");self.auto_scroll=tk.BooleanVar(value=True)
        self._style();self.build_ui();self.select_preset();self.show_page("data")
        window.after(100,self.poll_events);window.after(180,self.refresh_history);window.after(1000,self.poll_state)

    def _style(self):
        s=ttk.Style(self.window);s.theme_use("clam")
        s.configure("TFrame",background=C["surface"])
        s.configure("TLabel",background=C["surface"],foreground=C["ink"],font=(FONT,10))
        s.configure("TButton",font=(FONT,11),padding=(13,8),borderwidth=0,background="#DDEBFC",foreground=C["ink"],focuscolor=C["line"])
        s.map("TButton",background=[("active","#C9E0FB"),("disabled","#EAF0F9")],foreground=[("disabled","#95A9C3")])
        s.configure("Primary.TButton",background=C["accent"],foreground="white",padding=(18,11),font=(FONT,11,"bold"))
        s.map("Primary.TButton",background=[("active",C["hover"]),("disabled","#A7C5EB")],foreground=[("disabled","white")])
        s.configure("Danger.TButton",background="#FBEFEC",foreground=C["red"])
        s.map("Danger.TButton",background=[("active","#F7DFDA"),("disabled","#F4F4F4")])
        s.configure("Link.TButton",background=C["surface"],foreground=C["accent"],padding=(5,4),font=(FONT,10))
        s.map("Link.TButton",background=[("active",C["tint"])])
        s.configure("Quiet.TButton",background=C["surface"],foreground=C["muted"],font=(FONT,10),padding=(9,6))
        s.map("Quiet.TButton",background=[("active",C["tint"]),("disabled",C["surface"])])
        s.configure("Outline.TButton",background=C["surface"],foreground=C["accent"],font=(FONT,10),padding=(12,8),borderwidth=1,bordercolor=C["line"],lightcolor=C["line"],darkcolor=C["line"])
        s.map("Outline.TButton",background=[("active",C["tint"])])
        s.configure("Quiet.TMenubutton",background=C["surface"],foreground=C["muted"],font=(FONT,10),padding=(9,6),borderwidth=0)
        s.configure("TEntry",padding=(10,9),bordercolor=C["line"],lightcolor=C["line"],darkcolor=C["line"],fieldbackground="white",font=(FONT,11))
        s.map("TEntry",bordercolor=[("focus",C["accent"])])
        s.configure("TCombobox",padding=(9,8),font=(FONT,11),fieldbackground="white",background="white",bordercolor=C["line"],arrowsize=13)
        s.map("TCombobox",fieldbackground=[("readonly","white")],foreground=[("readonly",C["ink"])],selectbackground=[("readonly","white")],selectforeground=[("readonly",C["ink"])])
        self.window.option_add("*TCombobox*Listbox.font",(FONT,11))
        s.configure("Treeview",font=(FONT,11),rowheight=round(32*self.ui_scale),background=C["surface"],fieldbackground=C["surface"],foreground=C["ink"],borderwidth=0,bordercolor=C["surface"],lightcolor=C["surface"],darkcolor=C["surface"])
        s.configure("Treeview.Heading",font=(FONT,10),padding=(8,10),background="#DDEBFC",foreground=C["muted"],relief="flat")
        s.map("Treeview",background=[("selected","#C6DFFE")],foreground=[("selected",C["ink"])])
        s.configure("TNotebook",background=C["surface"],borderwidth=0,bordercolor=C["surface"],lightcolor=C["surface"],darkcolor=C["surface"],tabmargins=(0,0,0,8))
        s.configure("TNotebook.Tab",font=(FONT,11),padding=(16,8),background="#DDEBFC",foreground=C["muted"],borderwidth=0,bordercolor=C["line"],lightcolor=C["line"],darkcolor=C["line"])
        # Clam's selected-state padding (6,4,6,2) otherwise overrides the
        # configured (16,8) and makes the selected tab shorter and narrower.
        s.map("TNotebook.Tab",padding=[("selected",(16,8)),("!selected",(16,8))],
              background=[("selected",C["tint"]),("active","#EDF4FD"),("!selected","#DDEBFC")],
              foreground=[("selected",C["accent"]),("!selected",C["muted"])],
              lightcolor=[("selected",C["line"]),("!selected",C["line"])],
              darkcolor=[("selected",C["line"]),("!selected",C["line"])])
        s.configure("TCheckbutton",background=C["surface"],font=(FONT,10),foreground=C["muted"])
        s.configure("Vertical.TScrollbar",background="#C1D7F0",troughcolor=C["log"],borderwidth=0,arrowsize=11)
        ui.configure_theme(self.window)

    def text(self,parent,content,size=10,color=None,weight="normal",**kwargs):
        size=11 if size==10 else max(10,size)
        return tk.Label(parent,text=content,bg=parent.cget("bg"),fg=color or C["ink"],font=(FONT,size,weight),**kwargs)

    def card(self,parent):
        return tk.Frame(parent,bg=C["surface"],highlightbackground=C["line"],highlightcolor=C["line"],highlightthickness=1)

    def button(self,parent,text,command,style="TButton",guarded=False):
        b=ttk.Button(parent,text=text,command=command,style=style)
        if guarded:self.controls.append(b)
        return b

    def build_ui(self):
        sidebar=tk.Frame(self.window,bg=C["nav"],width=round(184*self.ui_scale));sidebar.pack(side="left",fill="y");sidebar.pack_propagate(False)
        brand=tk.Frame(sidebar,bg=C["nav"]);brand.pack(fill="x",padx=23,pady=(28,36))
        mark=tk.Canvas(brand,width=36,height=31,bg=C["nav"],highlightthickness=0);mark.pack(anchor="w")
        for offset,color in [(0,"#A3D1FF"),(8,"#5CA5ED"),(16,"#A3D1FF")]:
            mark.create_line(0,offset+7,9,offset+1,20,offset+10,35,offset+4,smooth=True,fill=color,width=2.5)
        self.text(brand,"SWAT+",21,"white","bold").pack(anchor="w",pady=(10,1))
        self.text(brand,"水文建模工作台",9,C["nav_text"]).pack(anchor="w")
        self.nav_buttons={}
        for page,label in [("data","1 资料准备"),("new","2 新建任务"),("monitor","运行监控"),("results","结果图表"),("history","历史结果")]:
            b=tk.Button(sidebar,text=label,font=(FONT,11),anchor="w",padx=24,pady=13,bg=C["nav"],fg=C["nav_text"],
                        activebackground=C["nav_active"],activeforeground="white",bd=0,relief="flat",cursor="hand2",command=lambda p=page:self.show_page(p))
            b.pack(fill="x",padx=11,pady=3);self.nav_buttons[page]=b
        bottom=tk.Frame(sidebar,bg=C["nav"]);bottom.pack(side="bottom",fill="x",padx=22,pady=24)
        self.text(bottom,"●  本地运行",9,"#A3D1FF").pack(anchor="w",pady=(0,5))
        self.text(bottom,"文件保留在工作区",8,C["nav_text"]).pack(anchor="w")
        tk.Button(bottom,text="打开工作目录  ↗",font=(FONT,9),bd=0,bg=C["nav"],fg="#DFECFF",activebackground=C["nav_active"],activeforeground="white",cursor="hand2",command=lambda:self.open_path(WORKSPACE)).pack(anchor="w",pady=(12,0))
        content=tk.Frame(self.window,bg=C["bg"]);content.pack(side="left",fill="both",expand=True,padx=27,pady=(24,17))
        header=tk.Frame(content,bg=C["bg"]);header.pack(fill="x",pady=(0,22))
        headings=tk.Frame(header,bg=C["bg"]);headings.pack(side="left",fill="x",expand=True)
        self.page_title=self.text(headings,"创建模拟任务",21,weight="bold",anchor="w");self.page_title.pack(anchor="w")
        self.page_subtitle=self.text(headings,"从输入资料到可检查的计算结果",10,C["muted"],anchor="w");self.page_subtitle.pack(anchor="w",pady=(6,0))
        self.button(header,"使用指南  ↗",lambda:self.open_path(WORKSPACE/"docs/使用指南.md")).pack(side="right",anchor="n",pady=6)
        self.button(header,"环境设置…",self.configure_environment).pack(side="right",anchor="n",padx=8,pady=6)
        self.page_host=tk.Frame(content,bg=C["bg"]);self.page_host.pack(fill="both",expand=True)
        self.pages={name:tk.Frame(self.page_host,bg=C["bg"]) for name in ["data","new","monitor","results","history"]}
        for frame in self.pages.values():frame.grid(row=0,column=0,sticky="nsew")
        self.page_host.grid_rowconfigure(0,weight=1);self.page_host.grid_columnconfigure(0,weight=1)
        self.build_new(self.pages["new"]);self.build_monitor(self.pages["monitor"]);self.build_results(self.pages["results"]);self.build_history(self.pages["history"]);self.build_data(self.pages["data"])
        footer=tk.Frame(content,bg=C["bg"]);footer.pack(side="bottom",fill="x",pady=(12,0),before=self.page_host)
        tk.Label(footer,textvariable=self.status,bg=C["bg"],fg=C["muted"],font=(FONT,10),anchor="w").pack(side="left",fill="x",expand=True)
        self.text(footer,"SWAT+ Workflow · Desktop",8,C["muted"]).pack(side="right")

    def configure_environment(self):
        if self.running or self.toolbox_busy() or self.busy_aux or self.input_page.form.busy:
            self.status.set("请在当前任务结束后修改运行环境。")
            return
        from desktop_environment import configure_environment
        if configure_environment(self.window):
            self.refresh_config()
            self.status.set("环境设置已保存，新任务将使用更新后的路径。")

    def build_data(self,page):
        from desktop_input_page import InputPage
        def saved(path):
            self.preset.set("自选配置");self.config_path.set(str(path));self.refresh_config()
            self.name.set(Path(path).stem[:12]+"_"+dt.datetime.now().strftime("%H%M%S"))
            self.status.set("资料方案已保存；请确认引擎和执行时间，再检查并开始建模。")
            self.show_page("new")
        self.input_page=InputPage(page,WORKSPACE,self.config_path.get,saved,self.status)
        self.input_page.pack(fill="both",expand=True)

    def build_new(self,page):
        page.grid_columnconfigure(0,weight=3,minsize=390);page.grid_columnconfigure(1,weight=2,minsize=280)
        page.grid_rowconfigure(1,weight=1)
        self.setup_card=self.card(page);self.setup_card.grid(row=0,column=0,sticky="nsew",padx=(0,16))
        inside=tk.Frame(self.setup_card,bg=C["surface"]);inside.pack(fill="both",expand=True,padx=22,pady=19)
        self.text(inside,"已准备的模型方案",13,weight="bold").pack(anchor="w")
        tk.Label(inside,text="选择资料方案，确认运行环境后开始建模。",font=(FONT,10),bg=C["surface"],fg=C["muted"]).pack(anchor="w",pady=(5,13))
        line=tk.Frame(inside,bg=C["surface"]);line.pack(fill="x")
        self.preset_combo=ttk.Combobox(line,textvariable=self.preset,values=list(PRESETS),state="readonly",width=22)
        self.preset_combo.pack(side="left",fill="x",expand=True);self.preset_combo.bind("<<ComboboxSelected>>",self.select_preset);self.controls.append(self.preset_combo)
        self.button(line,"导入配置",self.choose_config,"Quiet.TButton",True).pack(side="right",padx=(8,0))
        actions=tk.Frame(inside,bg=C["surface"]);actions.pack(fill="x",pady=(10,0))
        self.wizard_button=self.button(actions,"返回资料准备 →",self.model_setup,"Outline.TButton",True)
        self.wizard_button.pack(side="left")
        self.advanced_button=ttk.Menubutton(actions,text="高级操作",style="Quiet.TMenubutton")
        menu=tk.Menu(self.advanced_button,tearoff=False,font=(FONT,10),bg="white",fg=C["ink"],activebackground=C["tint"],activeforeground=C["accent"])
        menu.add_command(label="内部率定、参数与多站观测…",command=self.edit_configuration)
        menu.add_command(label="同参数引擎对照…",command=self.compare_model_engines)
        menu.add_separator();menu.add_command(label="查看当前JSON配置",command=lambda:self.open_path(Path(self.config_path.get())))
        self.advanced_button.configure(menu=menu);self.advanced_button.pack(side="right");self.controls.append(self.advanced_button)
        self.config_description=tk.Label(inside,text="",font=(FONT,10),bg=C["surface"],fg=C["muted"],anchor="w",justify="left",wraplength=440)
        self.config_description.pack(fill="x",pady=(13,10))
        self.reference_badge=tk.Label(inside,text="",font=(FONT,9),bg="#FFF6E8",fg=C["amber"],padx=10,pady=5,anchor="w",justify="left",wraplength=420)
        self.reference_badge.pack(fill="x")
        tk.Frame(inside,bg=C["line"],height=1).pack(fill="x",pady=(16,13))
        header=tk.Frame(inside,bg=C["surface"]);header.pack(fill="x")
        self.text(header,"运行环境",11,weight="bold").pack(side="left")
        self.engine_details_button=self.button(header,"程序详情  ↗",self.show_engine_details,"Quiet.TButton")
        self.engine_details_button.pack(side="right")
        engine=tk.Frame(inside,bg="#F6F9FD",padx=12,pady=10);engine.pack(fill="x",pady=(7,0));engine.grid_columnconfigure(0,weight=1)
        self.engine_value=tk.Label(engine,text="",font=(FONT,11,"bold"),bg="#F6F9FD",fg=C["ink"],anchor="w")
        self.engine_value.grid(row=0,column=0,sticky="ew",padx=(0,9))
        self.engine_change_button=self.button(engine,"更换引擎…",self.edit_engine,"Outline.TButton",True)
        self.engine_change_button.grid(row=0,column=1,sticky="e")
        self.environment_value=tk.Label(engine,text="",font=(FONT,9),bg="#F6F9FD",fg=C["muted"],anchor="w",justify="left",wraplength=440)
        self.environment_value.grid(row=1,column=0,columnspan=2,sticky="ew",pady=(5,0))
        self.new_phases=PhaseStrip(inside);self.new_phases.configure(height=58);self.new_phases.pack(fill="x",pady=(16,3))
        footer=tk.Frame(inside,bg=C["surface"]);footer.pack(fill="x",pady=(5,0));self._config_footer=footer
        self.config_filename=tk.Label(footer,text="",font=("Segoe UI",9),bg=C["surface"],fg=C["muted"],anchor="w")
        self.config_filename.pack(side="left",fill="x",expand=True)
        self.button(footer,"查看配置  ↗",lambda:self.open_path(Path(self.config_path.get())),"Quiet.TButton").pack(side="right")
        self._setup_inner=inside;inside.bind("<Configure>",self.resize_setup)
        execution=self.card(page);execution.grid(row=0,column=1,sticky="nsew")
        e=tk.Frame(execution,bg=C["surface"]);e.pack(fill="both",expand=True,padx=21,pady=19)
        self._plan_inner=e
        self.execution_heading=self.text(e,"执行计划",13,weight="bold");self.execution_heading.pack(anchor="w",pady=(0,17));self.plan_values={};self.plan_labels={}
        self._plan_rows=[]
        for label,key in [("模拟时间","simulation"),("率定期","calibration"),("验证期","validation"),("后续处理","search")]:
            row=tk.Frame(e,bg=C["surface"]);row.pack(fill="x",pady=5);row.grid_columnconfigure(1,weight=1)
            label_widget=tk.Label(row,text=label,font=(FONT,10),bg=C["surface"],fg=C["muted"],anchor="w")
            label_widget.grid(row=0,column=0,sticky="nw",padx=(0,12));self.plan_labels[key]=label_widget
            value=tk.Label(row,text="—",font=(FONT,10),bg=C["surface"],fg=C["ink"],anchor="e",justify="right",wraplength=300)
            value.grid(row=0,column=1,sticky="e");self.plan_values[key]=value;self._plan_rows.append(row)
        tk.Frame(e,bg=C["line"],height=1).pack(fill="x",pady=(13,14))
        tk.Label(e,text="任务名称",font=(FONT,10),bg=C["surface"],fg=C["muted"]).pack(anchor="w",pady=(0,6))
        self.name_entry=ttk.Entry(e,textvariable=self.name,font=("Segoe UI",11));self.name_entry.pack(fill="x");self.controls.append(self.name_entry)
        self.start_button=self.button(e,"开始建模与交接  →",self.start,"Primary.TButton",True);self.start_button.pack(fill="x",pady=(13,4))
        self.check_button=self.button(e,"先检查配置",self.check_config,"Quiet.TButton",True);self.check_button.pack(fill="x")
        self.execution_note=tk.Label(e,text="",font=(FONT,9),bg=C["surface"],fg=C["muted"],anchor="w",justify="left",wraplength=300)
        self.execution_note.pack(fill="x",pady=(13,0))
        e.bind("<Configure>",self.resize_plan)
        recent=self.card(page);recent.grid(row=1,column=0,columnspan=2,sticky="nsew",pady=(18,0))
        top=tk.Frame(recent,bg=C["surface"]);top.pack(fill="x",padx=22,pady=(15,10))
        self.text(top,"最近任务",12,weight="bold").pack(side="left")
        tk.Label(top,text="双击查看进度与报告",font=(FONT,9),bg=C["surface"],fg=C["muted"]).pack(side="left",padx=14)
        self.button(top,"查看全部  →",lambda:self.show_page("history"),"Quiet.TButton").pack(side="right")
        self.recent_table=self.create_table(recent,height=3);self.recent_table.pack(fill="both",expand=True,padx=14,pady=(0,12))
        self.recent_table.bind("<Double-1>",lambda _e:self.open_table_job(self.recent_table));self.recent_table.bind("<Return>",lambda _e:self.open_table_job(self.recent_table))
        self._home_compact=None;page.bind("<Configure>",self.resize_new_page)

    def resize_new_page(self,event):
        if event.height<10:return
        compact=event.height<round(650*self.ui_scale)
        if compact==self._home_compact:return
        self._home_compact=compact
        self._setup_inner.pack_configure(pady=12 if compact else 19)
        self._plan_inner.pack_configure(pady=12 if compact else 19)
        for row in self._plan_rows:row.pack_configure(pady=2 if compact else 5)
        self.execution_heading.pack_configure(pady=(0,10 if compact else 17))
        self.start_button.pack_configure(pady=(8 if compact else 13,4))
        self.execution_note.pack_configure(pady=(6 if compact else 13,0))
        if compact:
            self.config_description.pack_forget();self.new_phases.pack_forget();self._config_footer.pack_forget()
        else:
            self.config_description.pack(fill="x",pady=(13,10),before=self.reference_badge)
            self._config_footer.pack(fill="x",pady=(5,0))
            self.new_phases.pack(fill="x",pady=(16,3),before=self._config_footer)

    def resize_setup(self,event=None):
        width=max(250,self._setup_inner.winfo_width())
        for label,margin in [(self.config_description,0),(self.reference_badge,24),(self.environment_value,28)]:
            label.configure(wraplength=width-margin)
        from tkinter import font as tkfont
        def shortened(text,label,available):
            font=tkfont.Font(font=label.cget("font"))
            if font.measure(text)<=available:return text
            while text and font.measure(text+"…")>available:text=text[:-1]
            return text+"…"
        self.engine_value.configure(text=shortened(getattr(self,"_engine_caption",""),self.engine_value,max(120,width-self.engine_change_button.winfo_reqwidth()-45)))
        filename=Path(self.config_path.get()).name
        self.config_filename.configure(text=shortened(filename,self.config_filename,max(90,width-110)))

    def resize_plan(self,event=None):
        width=max(220,event.width if event else self.execution_note.master.winfo_width())
        self.execution_note.configure(wraplength=width)
        for label in self.plan_values.values():label.configure(wraplength=max(110,width-85))

    def show_engine_details(self):
        summary=self.config_summary;info=summary.get("engine_info",{})
        window=tk.Toplevel(self.window);window.title("当前模型程序与环境");window.transient(self.window)
        width=min(round(740*self.ui_scale),self.window.winfo_screenwidth()-100)
        height=min(round(440*self.ui_scale),self.window.winfo_screenheight()-130)
        x=max(0,min(self.window.winfo_rootx()+(self.window.winfo_width()-width)//2,window.winfo_screenwidth()-width))
        y=max(0,min(self.window.winfo_rooty()+(self.window.winfo_height()-height)//2,window.winfo_screenheight()-height-60))
        window.geometry(f"{width}x{height}+{x}+{y}");window.configure(bg=C["surface"])
        self.text(window,"当前模型程序",14,weight="bold").pack(anchor="w",padx=22,pady=(20,9))
        details=tk.Text(window,wrap="word",font=(FONT,10),bg="#F6F9FD",fg=C["ink"],relief="flat",padx=14,pady=12,height=10)
        scroll=ttk.Scrollbar(window,orient="vertical",command=details.yview);details.configure(yscrollcommand=scroll.set)
        body=tk.Frame(window,bg=C["surface"]);body.pack(side="bottom",fill="x",padx=22,pady=16)
        self.button(body,"关闭",window.destroy,"Outline.TButton").pack(side="right")
        def copy_path():
            window.clipboard_clear();window.clipboard_append(info.get("executable",""));self.status.set("已复制当前模型程序路径")
        self.button(body,"复制程序路径",copy_path,"Quiet.TButton").pack(side="left")
        scroll.pack(side="right",fill="y",pady=(0,8));details.pack(fill="both",expand=True,padx=(22,5),pady=(0,8))
        lines=["名称："+info.get("label","未记录"),"输入输出版本："+str(info.get("expected_version") or "未记录"),"","程序完整路径：",info.get("executable","未记录"),"","运行环境：",summary.get("runtime_caption","").split("\n")[0]]
        for key,label in [("compiler","编译器"),("build_configuration","构建配置"),("source_revision","源码版本")]:
            if info.get(key):lines += ["",label+"："+str(info[key])]
        lines += ["", "工作台所选程序用于本任务；Toolbox内运行时仍需确认其实际Revision与程序。"]
        details.insert("1.0","\n".join(lines));details.configure(state="disabled")
        return window

    def create_table(self,parent,height=6):
        table=ttk.Treeview(parent,columns=("name","status","progress","cal","val"),show="headings",selectmode="browse",height=height)
        for key,label,width in [("name","任务名称",220),("status","流程状态",185),("progress","搜索组数",90),("cal","率定评分",95),("val","验证评分",95)]:
            table.heading(key,text=label,anchor="w")
            table.column(key,width=round(width*self.ui_scale),minwidth=round((130 if key=="name" else width)*self.ui_scale),anchor="w",stretch=key=="name")
        return table

    def build_monitor(self,page):
        summary=self.card(page);summary.pack(fill="x")
        top=tk.Frame(summary,bg=C["surface"]);top.pack(fill="x",padx=21,pady=(16,4))
        self.monitor_name=self.text(top,"尚未选择任务",13,weight="bold",anchor="w");self.monitor_name.pack(side="left",fill="x",expand=True)
        self.monitor_status=self.text(top,"就绪",9,C["accent"]);self.monitor_status.pack(side="right")
        self.monitor_note=self.text(summary,"从历史结果选择任务，或新建一次运行。",9,C["muted"],anchor="w",wraplength=800);self.monitor_note.pack(fill="x",padx=21,pady=(0,6))
        self.phases=PhaseStrip(summary);self.phases.pack(fill="x",padx=18,pady=(0,8))
        metrics=tk.Frame(page,bg=C["bg"]);metrics.pack(fill="x",pady=15)
        for i in range(3):metrics.grid_columnconfigure(i,weight=1,uniform="metric")
        self.progress_metric=Metric(metrics,"参数搜索进度","搜索组数，不是全流程时间比例")
        self.cal_metric=Metric(metrics,"率定期评分","搜索时显示当前最佳值")
        self.val_metric=Metric(metrics,"验证期评分","使用同一组固定参数")
        for i,card in enumerate([self.progress_metric,self.cal_metric,self.val_metric]):card.grid(row=0,column=i,sticky="ew",padx=(0,12) if i<2 else (0,0))
        actions=tk.Frame(page,bg=C["bg"]);actions.pack(fill="x",pady=(0,13))
        self.resume_button=self.button(actions,"继续运行",self.resume,guarded=True);self.resume_button.pack(side="left",padx=(0,8))
        self.stop_button=self.button(actions,"停止任务",self.stop,"Danger.TButton");self.stop_button.pack(side="left",padx=(0,8))
        self.button(actions,"刷新状态",lambda:self.refresh_selected(True)).pack(side="left")
        self.sufi_review_button=self.button(actions,"逐轮诊断 / 设置",self.open_sufi_review);self.sufi_review_button.pack(side="left",padx=8)
        self.report_button=self.button(actions,"查看结果图表  →",lambda:self.show_page("results"),"Primary.TButton");self.report_button.pack(side="right")
        self.button(actions,"结果目录",self.open_job_folder).pack(side="right",padx=8)
        self.button(actions,"Toolbox 对接",self.toolbox_menu).pack(side="right",padx=8)
        area=self.card(page);area.pack(fill="both",expand=True)
        self.tabs=ttk.Notebook(area);self.tabs.pack(fill="both",expand=True,padx=14,pady=13)
        logs=tk.Frame(self.tabs,bg=C["surface"]);preview=tk.Frame(self.tabs,bg=C["surface"])
        preview.pack_propagate(False)
        self.tabs.add(logs,text="运行日志");self.tabs.add(preview,text="结果图表  ↗");self.tabs.bind("<<NotebookTabChanged>>",self.select_monitor_tab)
        bar=tk.Frame(logs,bg=C["surface"]);bar.pack(fill="x",pady=(0,7))
        self.text(bar,"最近记录；完整日志保存在任务目录",8,C["muted"]).pack(side="left")
        ttk.Checkbutton(bar,text="自动滚动",variable=self.auto_scroll).pack(side="right")
        logbody=tk.Frame(logs,bg=C["log"]);logbody.pack(fill="both",expand=True)
        scrollbar=ttk.Scrollbar(logbody,orient="vertical");scrollbar.pack(side="right",fill="y")
        self.log=tk.Text(logbody,wrap="word",height=9,state="disabled",font=("Consolas",10),bd=0,highlightthickness=0,bg=C["log"],fg="#486384",padx=13,pady=11,yscrollcommand=scrollbar.set)
        self.log.pack(side="left",fill="both",expand=True);scrollbar.configure(command=self.log.yview)
        self.append("就绪。新建任务或双击历史记录，开始查看工作流。\n")

    def select_monitor_tab(self,_event=None):
        if self.tabs.index(self.tabs.select())==1:
            self.tabs.select(0);self.show_page("results")

    def build_results(self,page):
        from desktop_image_viewer import ResultImageViewer
        panel=self.card(page);panel.pack(fill="both",expand=True)
        top=tk.Frame(panel,bg=C["surface"]);top.pack(fill="x",padx=18,pady=(12,5))
        self.result_name=self.text(top,"尚未选择任务",12,weight="bold");self.result_name.pack(side="left")
        self.image_button=self.button(top,"独立图窗 / 原图",self.open_original_image);self.image_button.pack(side="right")
        self.button(top,"完整报告  ↗",self.open_report,"Link.TButton").pack(side="right",padx=8)
        self.result_engine=self.text(panel,"",9,C["muted"],anchor="w",justify="left",wraplength=1000)
        self.result_engine.pack(fill="x",padx=18,pady=(0,4))
        row=tk.Frame(panel,bg=C["surface"]);row.pack(fill="x",padx=18,pady=(6,0))
        self.text(row,"选择图表",9,C["muted"]).pack(side="left",padx=(0,12))
        self.result_choice=tk.StringVar();self.result_combo=ttk.Combobox(row,textvariable=self.result_choice,state="readonly",width=55)
        self.result_combo.pack(side="left",fill="x",expand=True);self.result_combo.bind("<<ComboboxSelected>>",lambda _e:self.load_selected_figure())
        self.result_files={}
        self.image_viewer=ResultImageViewer(panel,background=C["surface"],foreground=C["ink"])
        self.image_viewer.pack(fill="both",expand=True,padx=10,pady=(0,8))
        self.preview_label=self.image_viewer.message
        self.text(panel,"滚轮上下查看 · Ctrl + 滚轮缩放 · 按住左键拖动 · 100%查看原始像素",9,C["muted"]).pack(anchor="w",padx=18,pady=(0,10))

    def open_original_image(self):
        path=self.result_files.get(self.result_choice.get())
        if path and path.is_file():
            from desktop_image_viewer import open_image_window
            window,viewer=open_image_window(self.window,path)
            window.after(150,viewer.actual_size)

    def load_selected_figure(self):
        path=self.result_files.get(self.result_choice.get())
        if path:self.image_viewer.set_path(path)

    def compare_model_engines(self):
        if self.running or "引擎对照" in self.busy_aux:return
        cfg=json.loads(Path(self.config_path.get()).read_text(encoding="utf-8-sig"))
        if cfg.get("engine",{}).get("mode")!="custom":
            messagebox.showinfo("先选择自编译引擎","请先通过引擎选择另存一份自编译配置。",parent=self.window);return
        source=cfg.get("model",{}).get("input_dir")
        if not source or not (WORKSPACE/source/"file.cio").is_file():
            source=filedialog.askdirectory(parent=self.window,title="选择已有的干净TxtInOut输入目录")
            if not source:return
        name=(self.name.get()[:13] or "engine")+"_compare"
        self.compare_report=WORKSPACE/"runs"/name/"report.md"
        self.job=None;self.job_summary=None;self._job_stamp=None
        self.job_text.set(str(self.compare_report.parent))
        self.monitor_name.configure(text="同参数引擎对照")
        self.monitor_status.configure(text="对照运行中")
        self.monitor_note.configure(text="两版程序使用相同输入、相同原参数；本次不进行参数搜索或率定评分。")
        self.progress_metric.set("—","固定两次模型运行")
        self.cal_metric.set("—","本次不评分")
        self.val_metric.set("—","本次不评分")
        self.phases.update_stage("created","running",False)
        self.update_actions()
        self.status.set("开始同参数对照：两次模型运行，不进行参数搜索。")
        self.show_page("monitor")
        self.launch(["compare-engines","--config",self.config_path.get(),"--name",name,"--input-dir",str(WORKSPACE/source)],"引擎对照")

    def edit_engine(self):
        if self.running:return
        from desktop_engine import EngineEditor
        def saved(path):
            self.preset.set("自选配置");self.config_path.set(str(path));self.refresh_config()
            self.status.set("已保存引擎选择；先检查配置，再启动任务。")
        EngineEditor(self.window,self.config_path.get(),WORKSPACE,saved)

    def model_setup(self):
        if self.running:return
        self.show_page("data")

    def edit_configuration(self):
        if self.running:return
        if self.config_summary.get("workflow_mode")=="toolbox":
            self.status.set("当前为建模交接模式；内部率定请从预设中选择综合分析或SUFI-2，另存配置。")
            return
        from desktop_config_editor import ConfigEditor
        def saved(path):
            self.preset.set("自选配置");self.config_path.set(str(path));self.refresh_config()
            self.status.set("已另存配置；先检查配置，再启动任务。")
        ConfigEditor(self.window,self.config_path.get(),WORKSPACE,saved)

    def build_history(self,page):
        panel=self.card(page);panel.pack(fill="both",expand=True)
        top=tk.Frame(panel,bg=C["surface"]);top.pack(fill="x",padx=21,pady=17)
        self.text(top,"本地任务",13,weight="bold").pack(side="left")
        self.button(top,"选择任务目录…",self.choose_job,guarded=True).pack(side="right")
        self.button(top,"刷新列表",self.refresh_history).pack(side="right",padx=8)
        self.history_table=self.create_table(panel,height=7);self.history_table.pack(fill="both",expand=True,padx=16)
        self.history_table.bind("<Double-1>",lambda _e:self.open_table_job(self.history_table));self.history_table.bind("<Return>",lambda _e:self.open_table_job(self.history_table))
        bottom=tk.Frame(panel,bg=C["surface"]);bottom.pack(side="bottom",fill="x",padx=21,pady=17,before=self.history_table)
        self.history_note=self.text(bottom,"",9,C["muted"],anchor="w");self.history_note.pack(side="left",fill="x",expand=True)
        self.button(bottom,"查看所选任务  →",lambda:self.open_table_job(self.history_table),"Primary.TButton").pack(side="right")

    def show_page(self,name):
        self.active_page=name
        for key,frame in self.pages.items():
            if key==name:frame.grid(row=0,column=0,sticky="nsew")
            else:frame.grid_remove()
        titles={"data":("资料输入与预处理","先准备空间、土壤与气象资料，再创建建模任务"),"new":("创建模拟任务","准备资料、自动建模，再交给Toolbox率定"),"monitor":("运行监控","跟踪模型阶段，查看真实评分与输出"),"results":("结果图表","宽幅查看真实输出，缩放检查每一个细节"),"history":("历史结果","继续未完成的任务，或检查已有结果")}
        self.page_title.configure(text=titles[name][0]);self.page_subtitle.configure(text=titles[name][1])
        for key,b in self.nav_buttons.items():b.configure(bg=C["nav_active"] if key==name else C["nav"],fg="white" if key==name else C["nav_text"])
        if name=="history" and self._history_loaded:self.refresh_history()
        if name=="monitor":self.refresh_selected()
        if name=="results":self.window.after_idle(self.maybe_load_preview)

    def select_preset(self,_event=None):
        filename=PRESETS.get(self.preset.get())
        if filename:self.config_path.set(str(WORKSPACE/"config"/filename))
        self.refresh_config()
        prefix="oulanka" if "oulanka" in self.config_path.get().lower() else "robit" if "robit" in self.config_path.get().lower() else "task"
        self.name.set(prefix+"_"+dt.datetime.now().strftime("%m%d_%H%M%S"))

    def refresh_config(self):
        summary=load_config_summary(self.config_path.get());self.config_summary=summary
        if summary["error"]:
            self.config_description.configure(text="配置暂时无法读取",fg=C["red"]);self.status.set(summary["error"]);self.start_button.state(["disabled"]);return
        build=summary["build_enabled"]
        toolbox=summary.get("workflow_mode")=="toolbox"
        description="从空间资料自动建模，再率定与验证。" if build else "复用已有SWAT+模型，率定后验证。"
        if toolbox:description="自动构建模型并基础试跑，再交给Toolbox；本任务不进行参数搜索。" if build else "载入干净模型并基础试跑，再交给Toolbox。"
        self.start_button.configure(text="开始建模与交接  →" if toolbox else "开始内部分析  →")
        self.config_description.configure(text=description if toolbox else description+"\n"+algorithm_caption(summary),fg=C["muted"])
        info=summary.get("engine_info",{})
        self._engine_caption=(info.get("label") or "配置指定的引擎")+" · SWAT+ "+str(info.get("expected_version") or "未指定版本")
        runtime=summary.get("runtime_caption","").split("\n")[0]
        self.environment_value.configure(text=runtime.split(" · SWAT+")[0] if "QGIS" in runtime else "按本配置指定的模型环境运行")
        sim=summary.get("simulation",{})
        self.plan_values["simulation"].configure(text=f"{sim.get('start_year','—')}—{sim.get('end_year','—')} · 预热{sim.get('warmup_years','—')}年")
        self.execution_note.configure(text="本次完成建模和基础试跑；参数搜索在Toolbox中确认后开始。" if toolbox else "使用当前配置在工作台执行分析，参数与预算可在高级设置中查看。")
        caption,color=reference_caption(summary["reference_kind"],summary["frequency_label"])
        if toolbox and not summary.get("observations_count"):caption="未配置参考资料 · 可以先建模，稍后再率定"
        self.reference_badge.configure(text="●  "+caption,fg=color,bg="#FFF6E8" if color==C["amber"] else "#EAF4FF")
        self.new_phases.update_stage("created","created",build,summary.get("workflow_mode")=="research",tuple(bool(summary.get(k,{}).get("enabled")) for k in ("sensitivity","uncertainty")),workflow_mode=summary.get("workflow_mode"));self.config_filename.configure(text=Path(summary["path"]).name)
        self.plan_values["calibration"].configure(text=date_range(summary["calibration"]));self.plan_values["validation"].configure(text=date_range(summary["validation"]))
        self.plan_labels["search"].configure(text={"spotpy_dds":"DDS搜索","spotpy_sceua":"SCE-UA搜索","glue_lhs":"GLUE集合","spotpy_dream":"DREAM采样","latin_hypercube_then_local":"LHS搜索"}.get(summary["method"],"参数搜索"))
        self.plan_labels["search"].configure(text="后续率定" if toolbox else self.plan_labels["search"].cget("text"))
        self.plan_values["search"].configure(text="Toolbox中率定" if toolbox else search_plan(summary))
        self.resize_setup();self.resize_plan()
        if not self.running:self.start_button.state(["!disabled"])

    def choose_config(self):
        if self.running:return
        path=filedialog.askopenfilename(parent=self.window,initialdir=str(WORKSPACE/"config"),filetypes=[("JSON配置","*.json")])
        if path:self.preset.set("自选配置");self.config_path.set(path);self.select_preset()

    def refresh_history(self):
        jobs=list_recent_jobs(WORKSPACE,limit=40)
        try:
            from input_paths import config_root
            selected=Path(self.config_path.get())
            cfg=json.loads(selected.read_text(encoding='utf-8-sig'))
            if cfg.get('workspace_root'):
                case=config_root(selected,cfg)
                if case!=WORKSPACE:jobs=sorted(jobs+list_recent_jobs(case,limit=40),key=lambda j:j.get('mtime') or 0,reverse=True)[:40]
        except (OSError,ValueError):pass
        self._recent_jobs=jobs;self._history_loaded=True
        self._job_by_id={job["path"]:job for job in jobs}
        for table,items in [(self.recent_table,jobs[:4]),(self.history_table,jobs)]:
            selected=table.selection();table.delete(*table.get_children())
            for i,job in enumerate(items):
                done,total=job["completed_trials"],job["total_trials"]
                progress="—" if job.get("method")=="toolbox_external" else f"{done}/{total}" if done is not None and total is not None else "—"
                status="流程完成" if job["status"]=="completed" else job["status_label"]
                metric=job["objective"] or ""
                cal=number(job["selected_score"])+(f" {metric}" if job["selected_score"] is not None else "")
                val=number(job["validation_score"])+(f" {metric}" if job["validation_score"] is not None else "")
                if job.get("method")=="toolbox_external":
                    if job["status"]=="ready":status="建模就绪 · 待Toolbox"
                    from toolbox4_results import latest_reports
                    records=latest_reports(job["path"])
                    if records.get("collection"):
                        values=next(iter(records["collection"]["data"].get("metrics",{}).values()),{})
                        cal=number(values.get("NSE"))+" NSE";status="Toolbox结果已回收"
                    if records.get("validation"):
                        data=records["validation"]["data"];values=next(iter(data.get("metrics",{}).values()),{})
                        val=number(values.get("validation",{}).get("NSE"))+" NSE"
                        status="验证结果已生成" if data.get("calibration_replay_agrees") else "复算有差异 · 待检查"
                table.insert("","end",iid=job["path"],values=(job["name"],status,progress,cal,val))
            if selected and table.exists(selected[0]):table.selection_set(selected[0])
        self.history_note.configure(text=f"{len(jobs)}个本地任务 · 双击查看" if jobs else "还没有任务，创建一次运行即可开始。")

    def open_table_job(self,table):
        if self.running:self.status.set("当前任务仍在运行，停止或完成后可切换任务。");return
        selected=table.selection()
        if selected and selected[0] in self._job_by_id:self.select_job(Path(self._job_by_id[selected[0]]["path"]))

    def select_job(self,path):
        if self.running or self.stop_pending:
            self.status.set("请等待当前运行或停止请求完成后再切换任务。");return
        summary=load_job_summary(path)
        if summary["error"]:messagebox.showerror("无法读取任务",summary["error"],parent=self.window);return
        self.job=Path(path).resolve();self.job_text.set(str(self.job));self._job_stamp=None
        self._external_log_stamp=None
        self.stop_requested=False;self.stop_pending=False
        self._preview_key=None;self._preview=None;self.preview_label.configure(image="",text="选择结果预览，加载已有图表。")
        self.image_viewer.clear("选择结果图表，加载已有输出。")
        self.log.configure(state="normal");self.log.delete("1.0","end");self.log.configure(state="disabled")
        logfile=self.job/"workflow.log"
        if logfile.exists():
            try:self.append(read_tail(logfile))
            except OSError as error:self.append(f"无法读取日志：{error}\n")
        self.show_page("monitor");self.refresh_selected(True)
        self.window.after_idle(lambda:self.log.see("end"))

    def choose_job(self):
        if self.running:return
        path=filedialog.askdirectory(parent=self.window,initialdir=str(WORKSPACE/"runs"),title="选择任务或归档结果目录")
        if path:
            try:
                from input_paths import job_root
                job_root(path)
            except (ValueError,KeyError,OSError):messagebox.showerror("任务位置不正确","请选择含state.json的任务目录，位于其配置的案例runs下。",parent=self.window);return
            self.select_job(path)

    def refresh_selected(self,force=False):
        if not self.job:self.update_actions();return
        try:
            state_path=job_state_path(self.job)
            stamp=(str(state_path),state_path.stat().st_mtime_ns)
        except OSError:
            self.status.set("正在创建任务…" if self.running else "任务状态文件暂时不可用");self.update_actions();return
        if not force and stamp==self._job_stamp:return
        summary=load_job_summary(self.job)
        if summary["error"]:self.status.set("状态正在更新，请稍候");return
        self._job_stamp=stamp;self.job_summary=summary;cfg=load_config_summary(summary["config_path"])
        self.monitor_name.configure(text=summary["name"])
        self.result_name.configure(text=summary["name"])
        engine=cfg.get("engine_info",{})
        self.result_engine.configure(text=(engine["label"]+"\n"+engine["executable"]) if engine else "历史任务：引擎信息以原运行配置为准")
        state_label=summary["status_label"] if summary["archived"] else "流程完成" if summary["status"]=="completed" else summary["status_label"]
        self.monitor_status.configure(text=state_label,fg=C["red"] if summary["status"]=="failed" else C["accent"])
        note=algorithm_caption(summary)+" · "+reference_caption(summary["reference_kind"])[0]
        if cfg.get("engine_info"):
            note+="\n"+cfg["engine_info"]["label"]+"："+cfg["engine_info"]["executable"]
        if summary["status"]=="failed" and summary.get("last_error"):note+="\n运行失败："+str(summary["last_error"])
        if summary["archived"]:
            note+="\n归档结果仅供查看；原评分保留，不再续跑或修改。"
        elif summary.get("sufi2_pending_review"):note+="\n等待查看："+str(summary["sufi2_pending_review"].get("reason","请检查本轮结果和下一轮设置"))
        self.monitor_note.configure(text=note);self.phases.update_stage(summary["phase"],summary["status"],cfg["build_enabled"],cfg.get("workflow_mode")=="research",tuple(bool(cfg.get(k,{}).get("enabled")) for k in ("sensitivity","uncertainty")),workflow_mode=cfg.get("workflow_mode"))
        done,total=summary["completed_trials"],summary["total_trials"]
        self.progress_metric.set(f"{done} / {total}" if done is not None and total is not None else "—")
        objective=summary["objective"] or "评分";selected=summary["selected_score"]
        self.cal_metric.set(number(selected if selected is not None else summary["best_so_far_score"]),"率定期选定参数" if selected is not None else "当前最佳，搜索尚未结束",f"率定期 · {objective}")
        self.val_metric.set(number(summary["validation_score"]),"同一组固定参数",f"验证期 · {objective}")
        if cfg.get("workflow_mode")=="toolbox":
            self.progress_metric.set("不在工作台搜索","建模与交接模式")
            self.cal_metric.set("—","在Toolbox率定后回收", "率定期 · NSE")
            self.val_metric.set("—","回收参数后固定运行", "验证期 · NSE")
            from toolbox4_results import latest_reports
            records=latest_reports(self.job)
            if records.get("collection"):
                values=next(iter(records["collection"]["data"]["metrics"].values()))
                self.cal_metric.set(number(values["NSE"]),"Toolbox实际CSV独立复核","率定期 · NSE")
            if records.get("validation"):
                data=records["validation"]["data"];values=next(iter(data["metrics"].values()))["validation"]
                self.val_metric.set(number(values["NSE"]),"固定参数；复算一致" if data["calibration_replay_agrees"] else "复算有差异，请查看报告","验证期 · NSE")
        self.status.set(state_label if summary["phase_label"]==state_label else summary["phase_label"]+" · "+state_label);self.update_actions()
        if not self.running:self.refresh_external_log()
        if summary["report_exists"]:self.maybe_load_preview()

    def refresh_external_log(self):
        """A task started by CLI/another window still has a live file log."""
        path=self.job/"workflow.log"
        try:
            stat=path.stat();stamp=(str(path),stat.st_mtime_ns,stat.st_size)
            if stamp==self._external_log_stamp:return
            content=read_tail(path)
        except OSError:return
        first,last=self.log.yview()
        self.log.configure(state="normal");self.log.delete("1.0","end");self.log.configure(state="disabled")
        self.append(content)
        if self.auto_scroll.get() and last>.98:self.log.see("end")
        else:self.log.yview_moveto(first)
        self._external_log_stamp=stamp

    def archived_selection(self):
        return bool((self.job_summary or {}).get("archived") or
                    self.job and job_state_path(self.job).name=="state.archived.json")

    def update_actions(self):
        state=(self.job_summary or {}).get("status")
        archived=self.archived_selection()
        resumable=bool(self.job) and state in ("paused","interrupted","failed","created") and not self.running and not archived
        stoppable=bool(self.job) and (self.running or state=="running") and not self.stop_requested and not archived
        self.resume_button.state(["!disabled"] if resumable else ["disabled"])
        self.resume_button.configure(text="归档只读" if archived else "查看并继续" if (self.job_summary or {}).get("sufi2_pending_review") else "继续运行")
        self.sufi_review_button.state(["!disabled"] if self.job and (self.job_summary or {}).get("method")=="swatcup_sufi2" and not archived else ["disabled"])
        self.stop_button.state(["!disabled"] if stoppable else ["disabled"])
        self.report_button.state(["!disabled"] if self.job and (self.job/"report.md").is_file() else ["disabled"])
        self.image_button.state(["!disabled"] if self.job and (self.job/"comparison.png").is_file() else ["disabled"])

    def poll_state(self):
        if self.job and not self.archived_selection() and (self.running or (self.job_summary or {}).get("status")=="running"):self.refresh_selected()
        if self.stop_pending:self.send_pending_stop()
        self.window.after(1400,self.poll_state)

    def append(self,text):
        at_bottom=self.log.yview()[1]>.98
        self.log.configure(state="normal");self.log.insert("end",text)
        count=int(self.log.index("end-1c").split(".")[0])
        if count>2500:self.log.delete("1.0",f"{count-2500+1}.0")
        if self.auto_scroll.get() and at_bottom:self.log.see("end")
        self.log.configure(state="disabled")

    def poll_events(self):
        start=time.perf_counter();text=[];other=[]
        for _ in range(160):
            if time.perf_counter()-start>.015:break
            try:kind,data=self.events.get_nowait()
            except queue.Empty:break
            if kind=="text":text.append(data)
            else:other.append((kind,data))
        if text:self.append("".join(text))
        for kind,data in other:
            if kind=="done":
                label,code,primary=data;self.busy_aux.discard(label);self.append(f"[{label}] 结束，退出码 {code}\n")
                if primary:
                    self.running=False;self.process=None;self.stop_requested=False;self.stop_pending=False
                    for control in self.controls:
                        if isinstance(control,ttk.Combobox):control.configure(state="readonly")
                        else:control.state(["!disabled"])
                    self.refresh_selected(True);self.refresh_history();self.update_actions()
                    if self.closing:self.window.destroy();return
                elif label=="配置检查":
                    if not self.running:
                        self.status.set("配置检查通过，可以开始运行" if code==0 else "配置检查未通过，请查看日志");self.check_button.state(["!disabled"])
                elif label=="引擎对照":
                    self.monitor_status.configure(text="对照完成" if code==0 else "对照失败")
                    self.status.set("同参数对照完成："+str(self.compare_report) if code==0 else "引擎对照失败，请查看日志。")
                    if code==0:self.open_path(self.compare_report)
                elif label=="停止请求" and code!=0:
                    self.stop_requested=False;self.stop_pending=False;self.closing=False;self.update_actions()
                    self.status.set("停止请求未成功，请查看日志后重试。")
                elif label=="Toolbox":
                    self.status.set("已请求打开 Toolbox；运行前核对右下角模型版本。" if code==0 else "Toolbox 对接失败，请查看日志。")
                elif label in ("Toolbox率定准备","Toolbox结果回收","Toolbox回收与独立验证"):
                    self.refresh_selected(True)
                    self.status.set(label+("处理结束，请查看日志中的结果与核验状态。" if code==0 else "未完成，请查看日志。"))
        self.window.after(80 if self.running else 180,self.poll_events)

    def launch(self,arguments,label,primary=False):
        if primary and self.running:return
        if not primary and label in self.busy_aux:return
        if primary:
            self.running=True;self.stop_requested=False
            for control in self.controls:control.configure(state="disabled")
            self.status.set(label+"…");self.update_actions()
        else:self.busy_aux.add(label)
        command=python_command()+arguments;self.append(f"\n[{label}]\n{subprocess.list2cmdline(command)}\n")
        def worker():
            code=None
            try:
                env=os.environ.copy();env.update(PYTHONIOENCODING="utf-8",PYTHONDONTWRITEBYTECODE="1")
                with subprocess.Popen(command,cwd=str(WORKSPACE),env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                      text=True,encoding="utf-8",errors="replace",bufsize=1,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0) as process:
                    if primary:self.process=process
                    for line in process.stdout:self.events.put(("text",line))
                    code=process.wait()
            except Exception as error:self.events.put(("text",f"{label}失败：{type(error).__name__}: {error}\n"))
            finally:self.events.put(("done",(label,code,primary)))
        threading.Thread(target=worker,daemon=True).start()

    def start(self):
        if self.running or self.stop_pending:return
        name=self.name.get().strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,20}",name):messagebox.showerror("任务名称无效","英文字母开头，最多21位，仅使用字母、数字和下划线。",parent=self.window);return
        config=Path(self.config_path.get())
        if not config.is_file():messagebox.showerror("缺少配置",str(config),parent=self.window);return
        from input_paths import config_root
        selected=json.loads(config.read_text(encoding='utf-8-sig'))
        case_root=config_root(config,selected) if selected.get('workspace_root') else WORKSPACE
        job=case_root/"runs"/name
        if job.exists():messagebox.showerror("任务已存在","请使用新名称，或从历史结果中续跑已有任务。",parent=self.window);return
        self.job=job;self.job_summary=None;self._job_stamp=None;self.job_text.set(str(job));self._preview_key=None;self._preview=None
        self.log.configure(state="normal");self.log.delete("1.0","end");self.log.configure(state="disabled")
        total=self.config_summary.get("trials_total")
        self.progress_metric.set(f"0 / {total}" if total is not None else "—")
        objective=self.config_summary.get("objective") or "评分"
        self.cal_metric.set("—","尚未开始搜索",f"率定期 · {objective}")
        self.val_metric.set("—","使用同一组固定参数",f"验证期 · {objective}")
        self.phases.update_stage("created","created",self.config_summary.get("build_enabled"),self.config_summary.get("workflow_mode")=="research",tuple(bool(self.config_summary.get(k,{}).get("enabled")) for k in ("sensitivity","uncertainty")),workflow_mode=self.config_summary.get("workflow_mode"))
        self.monitor_note.configure(text=algorithm_caption(self.config_summary)+" · "+reference_caption(self.config_summary.get("reference_kind"))[0])
        self.preview_label.configure(image="",text="完成运行后生成结果图。");self.monitor_name.configure(text=name);self.monitor_status.configure(text="准备启动")
        self.image_viewer.clear();self.result_name.configure(text=name)
        self.show_page("monitor");self.tabs.select(0);self.launch(["run","--config",str(config),"--name",name],"运行任务",True)

    def check_config(self):
        if self.running:return
        self.check_button.state(["disabled"]);self.status.set("正在检查配置、数据时段和参数范围…")
        self.launch(["check","--config",self.config_path.get()],"配置检查")

    def resume(self):
        if self.archived_selection():
            self.status.set("已归档结果仅供查看，不能继续运行。");return
        if self.job and not self.running and (self.job_summary or {}).get("status") in ("paused","interrupted","failed","created"):
            if (self.job_summary or {}).get("sufi2_pending_review"):
                self.open_sufi_review();return
            self.tabs.select(0);self.launch(["resume","--run-dir",str(self.job)],"续跑任务",True)

    def open_sufi_review(self):
        if not self.job:return
        if self.archived_selection():
            self.status.set("已归档结果仅供查看，请打开历史报告。");return
        from desktop_sufi_review import SufiReview
        def resume_review(path):
            if self.running or self.archived_selection():return
            self.tabs.select(0)
            self.launch(["resume","--run-dir",str(self.job),"--sufi-review",str(path)],"按轮次决定续跑",True)
        SufiReview(self.window,self.job,resume_review)

    def stop(self):
        if not self.job or self.stop_requested:return
        if self.archived_selection():
            self.status.set("已归档结果仅供查看，没有可停止的任务。");return
        self.stop_requested=True;self.stop_pending=True;self.update_actions()
        self.status.set("已请求停止，正在等待任务保存进度…")
        self.send_pending_stop()

    def send_pending_stop(self):
        if not self.stop_pending or not self.job:return
        summary=load_job_summary(self.job)
        if summary["error"] or summary["status"]=="created":return
        self.stop_pending=False
        if summary["status"]=="running":
            self.launch(["stop","--run-dir",str(self.job)],"停止请求")
        else:
            self.stop_requested=False;self.update_actions()

    def open_path(self,path):
        try:
            path=Path(path)
            if not path.exists():raise FileNotFoundError(path)
            os.startfile(str(path))
        except OSError as error:messagebox.showerror("无法打开",str(error),parent=self.window)

    def open_report(self):
        if not self.job:return
        from toolbox4_results import latest_reports
        reports=latest_reports(self.job)
        selected=reports.get("validation") or reports.get("collection")
        self.open_path(Path(selected["path"])/"report.md" if selected else self.job/"report.md")

    def open_job_folder(self):
        if self.job:self.open_path(self.job)

    def open_toolbox(self):
        if self.archived_selection():
            self.status.set("已归档结果仅供查看，不能新建 Toolbox 对接材料。");return
        if not self.job or not (self.job/"state.json").is_file():
            messagebox.showinfo("先选择任务","请先选择一个已经生成模型的任务。",parent=self.window);return
        if self.toolbox_busy():return
        from toolbox4_handoff import is_modern_job
        if is_modern_job(self.job):
            self.open_toolbox4();return
        self.busy_aux.add("Toolbox")
        self.status.set("正在准备 Toolbox 项目副本…")
        job=self.job
        def worker():
            code=1
            try:
                from toolbox_bridge import launch_project
                data,process=launch_project(job)
                code=0
                self.events.put(("text",f"\n已请求打开 Toolbox（PID {process.pid}）。工程：{data['toolbox_project']}\n运行前在 Run Model 右下角选择 Revision {data['model_version']}，旧版可能默认其他版本。观测和率定参数仍需在 Toolbox 中设置；本操作不启动后台率定。\n"))
            except Exception as error:self.events.put(("text",f"Toolbox 对接失败：{error}\n"))
            finally:self.events.put(("done",("Toolbox",code,False)))
        threading.Thread(target=worker,daemon=True).start()

    def toolbox_menu(self):
        if self.archived_selection():
            self.status.set("已归档结果仅供查看，不能修改或重新运行。");return
        if not self.job or not (self.job/"state.json").is_file():
            messagebox.showinfo("先选择任务","请先选择一个已经生成模型的任务。",parent=self.window);return
        from toolbox4_handoff import is_modern_job
        if is_modern_job(self.job):
            window=tk.Toplevel(self.window);window.title("Toolbox 4 · 导入材料");window.transient(self.window)
            window.configure(bg=C["bg"]);window.resizable(False,False)
            self.text(window,"Toolbox 4.0.1",13,weight="bold").pack(anchor="w",padx=20,pady=(18,8))
            tk.Label(window,text="为本任务准备独立模型输入、率定期参考数据和参数范围。\n"
                     "在 Toolbox 4 中新建工程并导入这些材料，设置后启动率定。\n"
                     "完成后选择实际Calibration目录回收结果；固定复算与验证使用独立副本。",bg=C["bg"],fg=C["muted"],
                     font=(FONT,10),justify="left").pack(padx=20,pady=(0,12))
            ttk.Button(window,text="准备材料并打开 Toolbox 4",command=lambda:(window.destroy(),self.open_toolbox4()),
                       style="Primary.TButton").pack(fill="x",padx=20,pady=(0,18))
            ttk.Button(window,text="回收完成的Toolbox结果（不运行模型）",command=lambda:(window.destroy(),self.collect_toolbox4())).pack(fill="x",padx=20,pady=4)
            ttk.Button(window,text="用已回收参数复算并验证（一次模型运行）",command=lambda:(window.destroy(),self.validate_toolbox4())).pack(fill="x",padx=20,pady=(4,12))
            ttk.Button(window,text="查看交接、复核与验证报告",command=lambda:(window.destroy(),self.open_path(self.job/"integration"))).pack(fill="x",padx=20,pady=(0,14))
            return
        tools=json.loads((WORKSPACE/"config/external_tools.json").read_text(encoding="utf-8-sig"))
        if tools.get("legacy_toolbox_retired"):
            messagebox.showinfo("旧版 Toolbox 已卸载",
                "此历史任务使用旧版模型和 Toolbox 1.0.5，旧组件已按你的要求卸载。\n"
                "报告、图表和已有结果仍可查看。需要继续建模或交接 Toolbox，请新建“新版 Robit”任务或选择相应新版配置。",
                parent=self.window)
            return
        window=tk.Toplevel(self.window);window.title("Toolbox 对接");window.transient(self.window)
        window.configure(bg=C["bg"]);window.resizable(False,False)
        self.text(window,"选择Toolbox操作",13,weight="bold").pack(anchor="w",padx=20,pady=(18,8))
        tk.Label(window,text="准备观测、参数与预算后打开真实Toolbox工程。\n仍需在Toolbox界面启动率定；此处不会声称已实现无界面运行。",bg=C["bg"],fg=C["muted"],font=(FONT,10),justify="left").pack(padx=20,pady=(0,12))
        fields=tk.Frame(window,bg=C["bg"]);fields.pack(fill="x",padx=20)
        count=tk.StringVar(value="100");algorithm=tk.StringVar(value="DDS")
        ttk.Label(fields,text="试算预算").grid(row=0,column=0,pady=8,sticky="w")
        ttk.Entry(fields,textvariable=count,width=18).grid(row=0,column=1,padx=12)
        ttk.Label(fields,text="Toolbox算法").grid(row=1,column=0,pady=8,sticky="w")
        ttk.Combobox(fields,textvariable=algorithm,values=["DDS"],state="readonly",width=16).grid(row=1,column=1,padx=12)
        def prepare():
            try:
                repetitions=int(count.get())
                if repetitions<10:raise ValueError("本机Toolbox DDS试算预算至少为10")
            except ValueError as error:messagebox.showerror("预算无效",str(error),parent=window);return
            window.destroy();self.prepare_toolbox_calibration(repetitions,algorithm.get())
        ttk.Button(window,text="准备率定工程并打开",command=prepare,style="Primary.TButton").pack(fill="x",padx=20,pady=(12,8))
        ttk.Button(window,text="回收Toolbox已生成结果",command=lambda:(window.destroy(),self.collect_toolbox_results())).pack(fill="x",padx=20,pady=4)
        ttk.Button(window,text="回收并独立验证（一次模型计算）",command=lambda:(window.destroy(),self.collect_and_verify_toolbox())).pack(fill="x",padx=20,pady=4)
        ttk.Button(window,text="仅打开模型工程",command=lambda:(window.destroy(),self.open_toolbox())).pack(fill="x",padx=20,pady=(4,18))

    def collect_toolbox4(self):
        if self.running or self.toolbox_busy():return
        path=filedialog.askdirectory(parent=self.window,title="选择Toolbox已完成运行的Calibration目录（含success.fin和CSV）")
        if not path:return
        def operation(job):
            from toolbox4_results import collect_results
            data=collect_results(job,path)
            return f"已回收真实输出并独立评分；未运行模型。报告：{data['output']}/report.md\n固定复算限制：{data['replay_blocked'] or '参数已解析，下一步明确选择引擎再复算'}"
        self.toolbox_background("Toolbox结果回收",operation)

    def validate_toolbox4(self):
        if self.running or self.toolbox_busy():return
        path=filedialog.askdirectory(parent=self.window,initialdir=str(self.job/"integration"),title="选择已回收结果目录（toolbox4_result_N）")
        if not path:return
        exe=filedialog.askopenfilename(parent=self.window,title="明确选择用于固定复算的SWAT+ exe（核对Toolbox实际程序）",filetypes=[("SWAT+程序","*.exe")])
        if not exe:return
        def operation(job):
            from toolbox4_results import validate_fixed
            data=validate_fixed(job,path,exe)
            return f"固定参数验证：{data['status']}；率定期复算一致：{data['calibration_replay_agrees']}。报告：{data['output']}/report.md"
        self.toolbox_background("Toolbox回收与独立验证",operation)

    def toolbox_busy(self):
        return any(label in self.busy_aux for label in ("Toolbox","Toolbox率定准备","Toolbox结果回收","Toolbox回收与独立验证"))

    def open_toolbox4(self):
        def operation(job):
            from toolbox4_handoff import launch_handoff
            data,process=launch_handoff(job)
            return (f"已准备 Toolbox 4 导入材料并请求打开软件（PID {process.pid}）。\n"
                    f"模型输入：{data['txtinout']}\n导入说明：{data['instructions']}\n"
                    "请在 Toolbox 新建工程，导入模型、参考数据和参数范围后启动率定。"
                    "本操作没有执行率定，也没有套用旧版工程文件。")
        self.toolbox_background("Toolbox",operation)

    def toolbox_background(self,label,operation):
        if self.archived_selection():
            self.status.set("已归档结果仅供查看，不能修改或重新运行。");return
        if not self.job or self.toolbox_busy():return
        self.busy_aux.add(label);self.status.set(label+"…");job=self.job
        def worker():
            code=1
            try:
                message=operation(job)
                self.events.put(("text","\n"+message+"\n"));code=0
            except Exception as error:self.events.put(("text",f"{label}未完成：{error}\n"))
            finally:self.events.put(("done",(label,code,False)))
        threading.Thread(target=worker,daemon=True).start()

    def prepare_toolbox_calibration(self,repetitions,algorithm):
        def operation(job):
            from toolbox_bridge import launch_calibration_project
            data,process=launch_calibration_project(job,repetitions=repetitions,algorithm=algorithm)
            return (f"Toolbox率定工程已准备并请求打开（PID {process.pid}）。工程：{data['toolbox_project']}\n"
                    f"请在Toolbox确认观测/参数、模型Revision {data.get('required_model_revision',data.get('model_version',''))}，然后点击其开始率定按钮。\n"
                    "运行完成后返回此处选择“回收Toolbox已生成结果”。")
        self.toolbox_background("Toolbox率定准备",operation)

    def collect_toolbox_results(self):
        def operation(job):
            from toolbox_bridge import collect_calibration_results
            result=collect_calibration_results(job)
            return f"Toolbox结果回收状态：{result.get('status','未知')}。报告：{result.get('report_path','尚无报告')}\n没有真实结果时不会生成成功率定结论。"
        self.toolbox_background("Toolbox结果回收",operation)

    def collect_and_verify_toolbox(self):
        def operation(job):
            from toolbox_bridge import collect_and_verify
            result=collect_and_verify(job)
            verification=result.get("verification",{})
            if not isinstance(verification,dict):verification={}
            verified=result.get("independent_replay_verified") is True and result.get("status")=="verified"
            conclusion="独立复算核对通过" if verified else str(result.get("status","尚未证实独立复算一致"))
            return (f"Toolbox回收与独立验证状态：{result.get('status','未知')}。\n"
                    f"独立核验：{conclusion}；"
                    f"报告：{result.get('report_path',verification.get('report_path','尚无报告'))}\n"
                    "该步骤只核验导入结果和固定参数的表现，不用验证期重新搜索参数；未通过的结果不会标成验证成功。")
        self.toolbox_background("Toolbox回收与独立验证",operation)

    def preview_resized(self,_event=None):
        if self._preview_resize:self.window.after_cancel(self._preview_resize)
        self._preview_resize=self.window.after(220,self.maybe_load_preview)

    def maybe_load_preview(self):
        self._preview_resize=None
        if not self.job or self.active_page!="results":return
        current=self.result_choice.get();files={}
        paths=list(self.job.glob("*.png"))+list((self.job/"integration").glob("toolbox4_*_*/comparison_*.png"))
        for path in sorted(paths):
            name=path.stem
            label="主要过程线" if name=="comparison" else "参数搜索轨迹" if name=="search_trace" else "敏感性分析" if name=="sensitivity" else "观测对比 · "+name[len("observation_"):] if name.startswith("observation_") else "预测区间 · "+name[len("uncertainty_"):] if name.startswith("uncertainty_") else name
            if path.parent!=self.job:label=path.parent.name+" · "+name
            files[label]=path
        self.result_files=files;self.result_combo.configure(values=list(files))
        self.image_button.state(["!disabled"] if files else ["disabled"])
        if not files:self.image_viewer.clear("可点击完整报告查看模型与交接材料；回收Toolbox结果后显示过程线。");return
        self.result_choice.set(current if current in files else "主要过程线" if "主要过程线" in files else next(iter(files)))
        self.load_selected_figure()

    def close(self):
        if self.toolbox_busy():
            self.status.set("正在处理Toolbox交接或固定复算，请待本次操作结束后关闭。");return
        if not self.running:self.window.destroy();return
        if messagebox.askyesno("停止后关闭","当前任务仍在运行。是否停止计算、保存进度后关闭？",parent=self.window):self.closing=True;self.stop()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--preview",action="store_true")
    parser.add_argument("--run-dir",type=Path,help="Open an existing completed or resumable task")
    args=parser.parse_args();enable_high_dpi();window=tk.Tk();desktop=Desktop(window)
    if args.preview:window.title("SWAT+ 工作台 · 界面预览")
    if args.run_dir:window.after(250,lambda:desktop.select_job(args.run_dir))
    window.mainloop()


if __name__=="__main__":main()
