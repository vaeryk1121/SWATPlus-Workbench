"""Small, explicit research-configuration editor; save copies, preserve source files."""
import copy
import json
import math
from pathlib import Path
import tkinter as tk
import desktop_ui as ui
from tkinter import filedialog, messagebox, ttk

METHODS={"DDS · 动态维度搜索":"spotpy_dds", "SCE-UA · 全局优化":"spotpy_sceua",
         "GLUE · LHS集合筛选":"glue_lhs", "DREAM · 贝叶斯采样":"spotpy_dream",
         "SUFI-2 · 经典原程序（SWAT+适配）":"swatcup_sufi2",
         "PSO · 粒子群（手写实现）":"pso", "SPE · SUFI-2系逐轮收缩":"spe_sufi2"}
METHOD_NOTES={"spotpy_dds":"顺序优化联合评分。预算用于参数搜索，不包含基准、验证和敏感性计算。",
              "spotpy_sceua":"复合形全局优化；实际评估次数由算法的初始化与迭代规则决定。",
              "glue_lhs":"按阈值筛选拉丁超立方参数集合；输出行为集合区间，不等同于SUFI-2。",
              "spotpy_dream":"每项观测须为高斯对数似然、正的sigma及weight=1。后验区间须链诊断通过；不自动包含观测或模型结构误差。",
              "swatcup_sufi2":"调用本机经典SWAT-CUP原版统计程序，参数修改由SWAT+原生接口完成；不是SWATplus-CUP/SPE。当前仅单站月径流、m³/s、NSE。率定期按轮更新范围，验证期固定参数及最终轮集合。",
              "pso":"手写gbest粒子群（Kennedy & Eberhart 1995；Clerc & Kennedy 2002）。c1=c2=2.0，惯性0.9→0.4线性递减，vmax=0.5×范围并对称折返。预算=swarm_size×(1+迭代数)；不是SPOTPY，也不是SWATplus-CUP的PSO程序。",
              "spe_sufi2":"手写SUFI-2系逐轮方法（Abbaspour 2004/2007/2015；SWAT-CUP手册）：LH采样→OLS敏感性→t·σ宽度收缩范围（不越过硬边界）→多轮→95PPU。轮次预算写在search.options.round_sizes；不调用经典原版程序，也不是SWATplus-CUP/SPE。"}

CONTINUATION={"自动继续，异常时暂停":"auto","每轮查看后继续":"review"}
SCREENING={"仅给出初筛建议（推荐）":"suggest","自动冻结符合条件的参数":"auto"}


def parse_round_sizes(value):
    """Parse a visible per-round budget without silently discarding empty rounds."""
    if isinstance(value,str):
        try:values=[int(part.strip()) for part in value.replace("，",",").split(",")]
        except ValueError:raise ValueError("SUFI-2逐轮候选数请用逗号分隔整数，如30,300,500")
    else:values=value
    if not isinstance(values,list) or not values or any(type(n) is not int or not 8<=n<=2000 for n in values):
        raise ValueError("SUFI-2每轮候选数须为8至2000之间的整数")
    return values


def finite(value,label,positive=False):
    try:result=float(value)
    except (ValueError,TypeError):raise ValueError(f"{label}必须为数值")
    if not math.isfinite(result) or positive and result<=0:raise ValueError(f"{label}必须为{'正的有限' if positive else '有限'}数值")
    return result


def parameter_hard_bounds(parameter):
    """Hard limits use the same coordinates as the selected change method."""
    lower=finite(parameter.get("lower"),"搜索下限");upper=finite(parameter.get("upper"),"搜索上限")
    minimum=finite(parameter.get("coordinate_min",lower),"初始硬下限")
    maximum=finite(parameter.get("coordinate_max",upper),"初始硬上限")
    if not minimum<=lower<upper<=maximum:
        raise ValueError(f"{parameter.get('name','参数')}：初始搜索范围必须位于硬边界内，且下限小于上限")
    return minimum,maximum


def apply_model_allowed_bounds(parameter,catalog,expected_version):
    """Copy a version-specific absolute interval, never reinterpret a delta/percent."""
    if parameter.get("method") not in ("absval","absolute"):
        raise ValueError("pctchg/abschg填写的是百分比或加减变化量，不能直接使用参数物理值范围；如需替换绝对值，请先选择absval")
    if not expected_version:raise ValueError("配置尚未填写model.expected_version，不能猜测模型允许值范围")
    native=parameter.get("native_name",parameter.get("column",parameter.get("name")))
    entry=next((row for row in catalog if row.get("native_name")==native),None)
    if entry is None:raise ValueError(f"参数目录中未找到{native}，请核对原生参数名")
    bounds=entry.get("model_absolute_bounds",{}).get(expected_version)
    if not isinstance(bounds,list) or len(bounds)!=2:raise ValueError(f"目录尚未核对{native}在{expected_version}版本的允许值范围")
    result=copy.deepcopy(parameter)
    result.update(lower=finite(bounds[0],"模型下限"),upper=finite(bounds[1],"模型上限"),
                  coordinate_min=finite(bounds[0],"模型硬下限"),coordinate_max=finite(bounds[1],"模型硬上限"))
    parameter_hard_bounds(result)
    result["range_source_note"]=f"曾采用目录中SWAT+ {expected_version}模型允许值范围；实际值以本配置为准，运行时仍按输入cal_parms.cal重新核验。不是SWAT-CUP全流域通用率定范围。"
    result.setdefault("sources",copy.deepcopy(entry.get("sources",[])))
    return result


def parse_layer(value):
    """Normalize the soil-layer form to the native [first,last] representation."""
    if value is None or value=="":return None
    if isinstance(value,str):
        value=value.strip()
        if not value:return None
        try:
            values=json.loads(value) if value.startswith("[") else [int(part.strip()) for part in value.split(",")]
        except (ValueError,TypeError):raise ValueError("土层请输入1、1,2或[1,2]；留空表示全部土层")
    elif type(value) is int:values=[value]
    else:values=value
    if not isinstance(values,list) or len(values) not in (1,2) or any(type(v) is not int or v<=0 for v in values):
        raise ValueError("土层编号必须为正整数，填写一层或起止两层")
    if len(values)==1:values=[values[0],values[0]]
    if values[0]>values[1]:raise ValueError("土层起始编号不能大于结束编号")
    return values


def display_layer(value):
    if value is None or value==[0,0]:return ""
    return ",".join(map(str,value)) if isinstance(value,list) else str(value)


def validate_edit(cfg):
    """UI-level checks only. Backend still validates model objects and observations."""
    search=cfg["search"];method=search["method"]
    if method not in METHODS.values():raise ValueError("请选择已接入的方法")
    if type(search["repetitions"]) is not int or search["repetitions"]<2:raise ValueError("候选预算至少为2")
    parameters=cfg.get("parameters",[])
    if not parameters:raise ValueError("至少需要一项参数")
    names=[]
    for item in parameters:
        if not item.get("name"):raise ValueError("参数名称不能为空")
        names.append(item["name"])
        parameter_hard_bounds(item)
        if item.get("method") not in ("absval","abschg","pctchg","absolute"):raise ValueError("原生参数方法必须为absval、abschg或pctchg")
        if item.get("layer") is not None and item["layer"]!=[0,0]:parse_layer(item["layer"])
    if len(set(names))!=len(names):raise ValueError("参数名称不能重复；同一参数的不同区域应使用不同名称")
    observations=cfg.get("observations",[])
    if not observations:raise ValueError("至少需要一项观测")
    ids=[]
    for obs in observations:
        if not obs.get("id") or not obs.get("path"):raise ValueError("观测名称和文件路径不能为空")
        if obs.get("frequency") not in ("daily","monthly"):raise ValueError(f"{obs['id']}：请选择逐日daily或逐月monthly时间步长")
        ids.append(obs["id"])
        finite(obs.get("weight",1),"观测权重",True)
        if obs.get("metric")=="NRMSE":finite(obs.get("scale"),"NRMSE尺度scale",True)
        if obs.get("metric")=="gaussian_loglik":finite(obs.get("sigma"),"观测误差sigma",True)
        if method=="spotpy_dream" and (obs.get("metric")!="gaussian_loglik" or obs.get("weight",1)!=1):raise ValueError("DREAM要求全部观测 metric=gaussian_loglik、weight=1，并填写正的sigma")
        if not obs.get("target",{}).get("variable"):raise ValueError("观测目标变量不能为空")
        unit_id=obs.get("target",{}).get("unit_id")
        if type(unit_id) is not int or unit_id<=0:raise ValueError(f"{obs['id']}：输出单元编号unit_id必须填写正整数，不能自动猜测站点")
    if len(set(ids))!=len(ids):raise ValueError("观测id不能重复")
    if method=="swatcup_sufi2":
        options=search.get("options",{})
        iterations=options.get("iterations",2);per_iteration=options.get("samples_per_iteration",12)
        seed=search.get("seed",20261020)
        if type(seed) is not int or not 1<=seed<2147483647:raise ValueError("SUFI-2随机种子需为正的32位整数")
        if "round_sizes" in options:sizes=parse_round_sizes(options["round_sizes"])
        else:
            if type(iterations) is not int or iterations<1:raise ValueError("SUFI-2轮次必须为正整数")
            if type(per_iteration) is not int:raise ValueError("SUFI-2每轮候选需为整数")
            sizes=parse_round_sizes([per_iteration]*iterations)
        if sizes[0]<max(8,len(parameters)+2):
            raise ValueError("SUFI-2首轮候选数至少比初始参数数多2；后续轮按实际活动参数数检查")
        if search["repetitions"]!=sum(sizes):raise ValueError("SUFI-2总预算必须等于逐轮候选数之和")
        if options.get("continuation","auto") not in CONTINUATION.values():raise ValueError("SUFI-2下一轮方式须为auto或review")
        screening=options.get("screening",{})
        if screening.get("mode","suggest") not in SCREENING.values():raise ValueError("SUFI-2初筛模式须为suggest或auto")
        if screening.get("enabled",True):
            threshold=finite(screening.get("p_threshold",.05),"敏感性p值阈值")
            if not 0<threshold<1:raise ValueError("敏感性p值阈值须大于0、小于1")
            after=screening.get("after_round",1);minimum=screening.get("min_active",min(2,len(parameters)))
            if type(after) is not int or not 1<=after<=len(sizes):raise ValueError("初筛轮次须在计划轮次之内")
            if type(minimum) is not int or not 1<=minimum<=len(parameters):raise ValueError("最少保留参数数须为1至初始参数数")
            protected=screening.get("protected_parameters",[])
            if not isinstance(protected,list) or any(name not in names for name in protected):raise ValueError("关键参数必须填写参数表中已有的名称")
        finite(options.get("behavioral_threshold",.5),"SUFI-2行为阈值")
        if not str(options.get("executable_dir","")).strip():raise ValueError("请指定本机SUFI-2统计程序目录")
        if len(observations)!=1:raise ValueError("当前SUFI-2接口仅支持单站月径流")
        obs=observations[0]
        if (obs.get("metric")!="NSE" or obs.get("frequency")!="monthly" or
                obs.get("target",{}).get("variable")!="flo_out" or obs.get("unit")!="m3/s"):
            raise ValueError("当前SUFI-2接口要求单站月径流、单位m3/s、指标NSE")
    if cfg.get("parameter_backend")=="native_calibration":
        from native_calibration import validate_definitions
        validate_definitions(parameters,expected_version=cfg.get("model",{}).get("expected_version"))
    return cfg


class ConfigEditor(tk.Toplevel):
    def __init__(self,parent,path,workspace,on_save):
        super().__init__(parent);self.withdraw()
        self.workspace=Path(workspace);self.path=Path(path);self.on_save=on_save
        try:self.config=json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError,ValueError) as error:
            messagebox.showerror("无法读取配置",str(error),parent=parent);self.destroy();return
        self.parameters=copy.deepcopy(self.config.get("parameters",[]))
        self.observations=copy.deepcopy(self.config.get("observations",[]))
        if not self.observations and self.config.get("observation"):
            obs=copy.deepcopy(self.config["observation"]);obs.setdefault("id","outlet_flow")
            obs.setdefault("metric",self.config.get("search",{}).get("objective","NSE"));obs.setdefault("weight",1.)
            target={"object":"channel_sd","variable":"flo_out","unit":"m3/s"}
            outlet=self.config.get("model",{}).get("outlet")
            if type(outlet) is int:target["unit_id"]=outlet
            elif isinstance(outlet,dict):
                if outlet.get("unit"):target["unit_id"]=outlet["unit"]
                for key in ("gis_id","name"):
                    if outlet.get(key) is not None:target[key]=outlet[key]
            obs.setdefault("target",target);self.observations=[obs]
        self.ui_scale=max(1.,float(parent.tk.call("tk","scaling"))/(96/72))
        self.title("方法、参数与观测设置 · 另存配置")
        self.geometry(self.dialog_geometry(1120,820))
        self.minsize(min(round(940*self.ui_scale),self.winfo_screenwidth()-100),min(round(650*self.ui_scale),self.winfo_screenheight()-120));self.configure(bg="#F4F8FE");self.transient(parent)
        top=tk.Frame(self,bg="#F4F8FE");top.pack(fill="x",padx=20,pady=16)
        tk.Label(top,text="设置研究方案",font=("Microsoft YaHei UI",16,"bold"),bg="#F4F8FE",fg="#163B67").pack(anchor="w")
        tk.Label(top,text="修改方法、参数和观测后另存为新配置；原配置、模型输入和观测文件保持不变。",font=("Microsoft YaHei UI",10),bg="#F4F8FE",fg="#566F90").pack(anchor="w",pady=(5,0))
        if any(item.get("method") not in ("absolute","absval","abschg","pctchg") for item in self.parameters):
            tk.Label(top,text="此配置含旧的直接文件系数参数（如CN factor），不能直接等同于原生cn2。请从原生/综合分析方案开始，或移除旧参数后从目录重新选择；软件不会自动改写含义。",font=("Microsoft YaHei UI",10),bg="#FFF2DE",fg="#785021",wraplength=round(1020*self.ui_scale),justify="left",padx=10,pady=10).pack(fill="x",pady=(12,0))
        bottom=tk.Frame(self,bg="#F4F8FE");bottom.pack(side="bottom",fill="x",padx=20,pady=16)
        ttk.Button(bottom,text="另存并使用",style="Primary.TButton",command=self.save).pack(side="right")
        ttk.Button(bottom,text="取消",command=self.destroy).pack(side="right",padx=8)
        tk.Label(bottom,text="保存后仍需点击“先检查配置”核对模型、单位和资料。",font=("Microsoft YaHei UI",10),bg="#F4F8FE",fg="#566F90").pack(side="left")
        book=ttk.Notebook(self);book.pack(fill="both",expand=True,padx=20)
        method=tk.Frame(book,bg="#EAF3FF");params=tk.Frame(book,bg="#EAF3FF");obs=tk.Frame(book,bg="#EAF3FF")
        book.add(method,text="方法与分析");book.add(params,text="参数范围");book.add(obs,text="站点与变量")
        self.build_method(method);self.parameter_table=self.build_table(params,"parameters")
        self.observation_table=self.build_table(obs,"observations");self.refresh_tables()
        self.deiconify()

    def dialog_geometry(self,width,height):
        return f"{min(round(width*self.ui_scale),self.winfo_screenwidth()-100)}x{min(round(height*self.ui_scale),self.winfo_screenheight()-120)}"

    def scroll_form(self,parent):
        holder=tk.Frame(parent,bg="#EAF3FF");holder.pack(fill="both",expand=True)
        canvas=tk.Canvas(holder,bg="#EAF3FF",highlightthickness=0)
        scrollbar=ttk.Scrollbar(holder,orient="vertical",command=canvas.yview)
        scrollbar.pack(side="right",fill="y");canvas.pack(side="left",fill="both",expand=True)
        canvas.configure(yscrollcommand=scrollbar.set,yscrollincrement=20)
        body=tk.Frame(canvas,bg="#EAF3FF");window_id=canvas.create_window((24,16),window=body,anchor="nw")
        body.bind("<Configure>",lambda _e:canvas.configure(scrollregion=(0,0,canvas.winfo_width(),body.winfo_reqheight()+32)))
        canvas.bind("<Configure>",lambda e:canvas.itemconfigure(window_id,width=max(50,e.width-48)))
        return body,canvas

    def wheel_form(self,event,canvas):
        widget=event.widget
        while widget is not None:
            if widget is canvas or widget.master is canvas:
                canvas.yview_scroll(-3*(int(event.delta/120) or (1 if event.delta>0 else -1)),"units");return "break"
            widget=getattr(widget,"master",None)

    def field(self,parent,label,value,row,choices=None):
        ttk.Label(parent,text=label,background="#EAF3FF",font=("Microsoft YaHei UI",10),wraplength=ui.px(self,220)).grid(row=row,column=0,sticky="w",pady=7,padx=(0,18))
        var=tk.StringVar(value=value)
        if choices:widget=ttk.Combobox(parent,textvariable=var,values=choices,state="readonly",width=40)
        else:widget=ttk.Entry(parent,textvariable=var,width=42)
        widget.grid(row=row,column=1,sticky="ew",pady=7)
        return var,widget

    def build_method(self,page):
        body,self.method_canvas=self.scroll_form(page);body.grid_columnconfigure(1,weight=1)
        self.bind("<MouseWheel>",lambda event:self.wheel_form(event,self.method_canvas),add="+")
        search=self.config.get("search",{});method=search.get("method","spotpy_dds")
        label=next((label for label,key in METHODS.items() if key==method),next(iter(METHODS)))
        self.method,combo=self.field(body,"搜索算法",label,0,list(METHODS))
        self.method_note=ttk.Label(body,text="",background="#EAF3FF",foreground="#566F90",wraplength=740,font=("Microsoft YaHei UI",10));self.method_note.grid(row=1,column=0,columnspan=2,sticky="w",pady=(0,12))
        combo.bind("<<ComboboxSelected>>",lambda _e:self.update_budget())
        self.repetitions,self.repetitions_widget=self.field(body,"候选总预算",str(search.get("repetitions",100)),2)
        self.seed,_=self.field(body,"随机种子",str(search.get("seed",20261019)),3)
        self.ngs,self.ngs_widget=self.field(body,"SCE-UA复合形数量",str(search.get("options",{}).get("ngs",2)),4)
        analysis=self.config.get("analysis",{});sensitivity=analysis.get("sensitivity",{});uncertainty=analysis.get("uncertainty",{})
        sensitivity_name=sensitivity.get("method","morris") if sensitivity.get("enabled",False) else "关闭"
        self.sensitivity,_=self.field(body,"敏感性分析",sensitivity_name,5,["关闭","morris","sobol"])
        sample=sensitivity.get("base_sample_size",8) if sensitivity_name=="sobol" else sensitivity.get("trajectories",4)
        self.samples,_=self.field(body,"Morris轨迹数 / Sobol基础样本数",str(sample),6)
        self.uncertainty=tk.BooleanVar(value=uncertainty.get("enabled",False))
        self.uncertainty_check=ttk.Checkbutton(body,text="计算行为集合预测区间（按联合评分阈值筛选）",variable=self.uncertainty)
        self.uncertainty_check.grid(row=7,column=0,columnspan=2,sticky="w",pady=8)
        self.threshold,self.threshold_widget=self.field(body,"行为集合最低联合评分",str(uncertainty.get("threshold",0.)),8)
        self.minimum,self.minimum_widget=self.field(body,"最少行为样本数",str(uncertainty.get("min_behavioral",20)),9)
        self.sufi_options=tk.Frame(body,bg="#EAF3FF");self.sufi_options.grid(row=10,column=0,columnspan=2,sticky="ew",pady=(8,0));self.sufi_options.grid_columnconfigure(1,weight=1)
        options=search.get("options",{})
        sizes=options.get("round_sizes") or [options.get("samples_per_iteration",12)]*options.get("iterations",2)
        self.sufi_round_sizes,_=self.field(self.sufi_options,"逐轮候选数（逗号分隔）",",".join(map(str,sizes)),0)
        self.sufi_continuation,_=self.field(self.sufi_options,"下一轮衔接",next((k for k,v in CONTINUATION.items() if v==options.get("continuation","auto")),next(iter(CONTINUATION))),1,list(CONTINUATION))
        self.sufi_threshold,_=self.field(self.sufi_options,"SUFI-2 行为集合最低NSE",str(options.get("behavioral_threshold",.5)),2)
        from sufi2_bridge import DEFAULT_EXECUTABLE_DIR
        self.sufi_directory,_=self.field(self.sufi_options,"本机原版统计程序目录",str(options.get("executable_dir",DEFAULT_EXECUTABLE_DIR)),3)
        screening=options.get("screening",{})
        self.sufi_screen_enabled=tk.BooleanVar(value=screening.get("enabled",True))
        ttk.Checkbutton(self.sufi_options,text="小预算初筛后查看敏感性建议（不显著不等于确定不敏感）",variable=self.sufi_screen_enabled).grid(row=4,column=0,columnspan=2,sticky="w",pady=8)
        self.sufi_screen_mode,_=self.field(self.sufi_options,"初筛处理",next((k for k,v in SCREENING.items() if v==screening.get("mode","suggest")),next(iter(SCREENING))),5,list(SCREENING))
        self.sufi_screen_round,_=self.field(self.sufi_options,"第几轮结束后初筛",str(screening.get("after_round",1)),6)
        self.sufi_screen_p,_=self.field(self.sufi_options,"敏感性p值阈值",str(screening.get("p_threshold",.05)),7)
        self.sufi_screen_min,_=self.field(self.sufi_options,"自动初筛最少保留参数数",str(screening.get("min_active",min(2,len(self.parameters)))),8)
        self.sufi_protected,_=self.field(self.sufi_options,"关键参数（名称，逗号分隔）",",".join(screening.get("protected_parameters",[])),9)
        ttk.Label(self.sufi_options,text="未单独填写硬边界时，初始搜索范围就是本次试验的硬边界；下一轮推荐范围只取硬边界内交集。\n示例30,300,500表示先试30组，再试300组、500组。冻结参数固定为选定值，不从模型中删除。",background="#EAF3FF",foreground="#566F90",wraplength=760).grid(row=10,column=0,columnspan=2,sticky="w",pady=(10,0))
        self.budget=ttk.Label(body,text="",background="#EAF3FF",foreground="#566F90",wraplength=760,font=("Microsoft YaHei UI",10));self.budget.grid(row=11,column=0,columnspan=2,sticky="w",pady=(12,0))
        for var in (self.sensitivity,self.samples,self.repetitions,self.ngs,self.sufi_round_sizes):var.trace_add("write",lambda *_:self.update_budget())
        self.update_budget()

    def update_budget(self):
        method=METHODS.get(self.method.get(),"spotpy_dds");self.method_note.configure(text=METHOD_NOTES[method])
        dream=method=="spotpy_dream";sufi=method=="swatcup_sufi2"
        self.uncertainty_check.configure(text="用率定最终轮固定集合计算验证期区间（率定区间总会生成）" if sufi else "计算后验参数预测区间（需链诊断通过）" if dream else "计算行为集合预测区间（按联合评分阈值筛选）")
        for widget in (self.threshold_widget,self.minimum_widget):widget.configure(state="disabled" if dream or sufi else "normal")
        self.ngs_widget.configure(state="normal" if method=="spotpy_sceua" else "disabled")
        self.repetitions_widget.configure(state="disabled" if sufi else "normal")
        if sufi:self.sufi_options.grid()
        else:self.sufi_options.grid_remove()
        try:
            n=int(self.samples.get());d=len(self.parameters);kind=self.sensitivity.get()
            second=self.config.get("analysis",{}).get("sensitivity",{}).get("calc_second_order",False)
            extra=0 if kind=="关闭" else n*(d+1) if kind=="morris" else n*((2*d+2) if second else (d+2))
            sce=f" SCE-UA初始化至少约 {int(self.ngs.get())*(2*d+1)} 次评估，预算应覆盖初始化与后续搜索。" if method=="spotpy_sceua" else ""
            if sufi:
                sizes=parse_round_sizes(self.sufi_round_sizes.get());total=sum(sizes)
                if self.repetitions.get()!=str(total):self.repetitions.set(str(total))
                sce=f" SUFI-2：{' → '.join(map(str,sizes))}组，共{len(sizes)}轮、{total}次率定试算，另有基准与验证。首轮至少{max(8,d+2)}组。"
            self.budget.configure(text=f"{d}项参数；敏感性计划约 {extra} 次额外模拟。实际总数以配置检查为准。{sce}\nMorris至少4条轨迹，少于10条仅用于探索；Sobol建议用2的幂次样本数。")
        except ValueError:self.budget.configure(text="请输入整数预算。")

    def build_table(self,page,kind):
        bar=tk.Frame(page,bg="#EAF3FF");bar.pack(fill="x",padx=16,pady=14)
        text="absval替换值；abschg加减值；pctchg百分数变化（-10表示减少10%）。SUFI-2后续范围不得越过初始硬边界；硬边界留空则沿用初始搜索范围。absval可采用此版本模型允许值，运行时仍核验cal_parms.cal。" if kind=="parameters" else "每行是一处站点的一种变量；单位必须与目标输出匹配。双击编辑。"
        tk.Label(bar,text=text,bg="#EAF3FF",fg="#566F90",font=("Microsoft YaHei UI",10),wraplength=750,justify="left").pack(anchor="w",pady=(0,10))
        for label,command in [("新增",lambda:self.edit_row(kind)),("编辑所选",lambda:self.edit_row(kind,True)),("移除所选",lambda:self.remove_row(kind))]:ttk.Button(bar,text=label,command=command).pack(side="left",padx=(0,8))
        if kind=="parameters":ttk.Button(bar,text="从参数目录添加…",command=self.add_catalog).pack(side="right")
        columns=("name","native","scope","method","range","hard") if kind=="parameters" else ("id","target","variable","metric","weight")
        titles=("名称","SWAT+参数","对象","变化方式","初始搜索范围","全程硬边界") if kind=="parameters" else ("观测id","目标对象","变量","指标","权重")
        table=ttk.Treeview(page,columns=columns,show="headings",height=10)
        for key,title in zip(columns,titles):table.heading(key,text=title);table.column(key,width=170,minwidth=90)
        table.pack(fill="both",expand=True,padx=16,pady=(0,16));table.bind("<Double-1>",lambda _e:self.edit_row(kind,True))
        return table

    def refresh_tables(self):
        for table,rows,kind in [(self.parameter_table,self.parameters,"parameters"),(self.observation_table,self.observations,"observations")]:
            table.delete(*table.get_children())
            for index,item in enumerate(rows):
                if kind=="parameters":values=(item.get("name"),item.get("native_name",item.get("column",item.get("name"))),item.get("object_type","hru"),item.get("method"),f"{item.get('lower')} ～ {item.get('upper')}",f"{item.get('coordinate_min',item.get('lower'))} ～ {item.get('coordinate_max',item.get('upper'))}")
                else:values=(item.get("id"),item.get("target",{}).get("object"),item.get("target",{}).get("variable"),item.get("metric"),item.get("weight",1))
                table.insert("","end",iid=str(index),values=values)
        self.update_budget()

    def remove_row(self,kind):
        table=self.parameter_table if kind=="parameters" else self.observation_table
        selected=table.selection()
        if selected:getattr(self,kind).pop(int(selected[0]));self.refresh_tables()

    def add_catalog(self):
        try:catalog=json.loads((self.workspace/"config/native_parameter_catalog.json").read_text(encoding="utf-8"))["parameters"]
        except (OSError,ValueError,KeyError) as error:messagebox.showerror("参数目录不可用",str(error),parent=self);return
        window=tk.Toplevel(self);window.title("SWAT+原生参数目录");window.transient(self);window.geometry(self.dialog_geometry(1040,700));window.configure(bg="#EAF3FF")
        window.minsize(min(round(800*self.ui_scale),self.winfo_screenwidth()-100),min(round(500*self.ui_scale),self.winfo_screenheight()-120))
        version=self.config.get("model",{}).get("expected_version")
        catalog=[entry for entry in catalog if version in entry.get("model_absolute_bounds",{})]
        tk.Label(window,text=f"当前模型版本：{version or '未设置'}；已核对{len(catalog)}项参数。可添加探索起始范围，或为absval采用模型允许值范围。",font=("Microsoft YaHei UI",10),bg="#EAF3FF",fg="#163B67").pack(anchor="w",padx=18,pady=14)
        tk.Label(window,text="参数必须存在于当前模型cal_parms.cal中；物理边界、作用对象与版本在配置检查时核对。",font=("Microsoft YaHei UI",10),bg="#EAF3FF",fg="#566F90").pack(anchor="w",padx=18,pady=(0,12))
        table_body=tk.Frame(window,bg="#EAF3FF");table_body.pack(fill="both",expand=True,padx=18)
        table=ttk.Treeview(table_body,columns=("name","label","process","method","range"),show="headings",selectmode="extended")
        for key,label,width in [("name","参数",130),("label","物理含义",260),("process","过程",170),("method","变化方式",130),("range","探索范围",180)]:table.heading(key,text=label);table.column(key,width=width)
        scroll=ttk.Scrollbar(table_body,orient="vertical",command=table.yview);scroll.pack(side="right",fill="y")
        table.configure(yscrollcommand=scroll.set);table.pack(fill="both",expand=True)
        for index,entry in enumerate(catalog):
            search=entry["suggested_search"];table.insert("","end",iid=str(index),values=(entry["native_name"],entry["label"],entry["process"],search["method"],f"{search['lower']} ～ {search['upper']}"))
        note=tk.Label(window,text="可按Ctrl选择多项。已存在的同名参数会保留当前设置。",bg="#EAF3FF",fg="#566F90",font=("Microsoft YaHei UI",10),wraplength=980,justify="left");note.pack(fill="x",padx=18,pady=12)
        def selected(_event=None):
            selection=table.selection()
            compatible=bool(selection) and all(catalog[int(key)]["suggested_search"]["method"] in ("absval","absolute") for key in selection)
            model_button.configure(state="normal" if compatible else "disabled")
            if selection:
                entry=catalog[int(selection[0])]
                units=entry.get("physical_units",entry.get("units_in_model_dictionary",{}).get(version,"未记录"))
                text=f"作用对象：{entry['object_type']}；单位：{units}；本版本允许值：{entry['model_absolute_bounds'][version]}。\n"
                text+=entry.get("note","")+"  "+entry["suggested_search"].get("basis","")
                if not compatible:text+="\n当前选择含百分比/加减变化量，不能直接套物理值范围。可先添加，再编辑为absval后采用模型允许值。"
                note.configure(text=text)
        table.bind("<<TreeviewSelect>>",selected)
        def add(use_model_bounds=False):
            names={entry.get("name") for entry in self.parameters}
            additions=[]
            for key in table.selection():
                source=catalog[int(key)];name=source["native_name"]
                if name in names:continue
                search=source["suggested_search"]
                parameter={"name":name,"native_name":name,"object_type":source["object_type"],"method":search["method"],"lower":search["lower"],"upper":search["upper"],
                           "note":source.get("note",""),"sources":copy.deepcopy(source.get("sources",[])),
                           "range_source_note":search.get("basis","探索性起始范围，需按流域资料核验")}
                if source.get("default_layer"):parameter["layer"]=copy.deepcopy(source["default_layer"])
                try:
                    if use_model_bounds:parameter=apply_model_allowed_bounds(parameter,catalog,version)
                except ValueError as error:messagebox.showerror("无法采用模型范围",str(error),parent=window);return
                additions.append(parameter);names.add(name)
            self.parameters.extend(additions)
            self.refresh_tables();window.destroy()
        actions=tk.Frame(window,bg="#EAF3FF");actions.pack(fill="x",padx=18,pady=(0,14))
        ttk.Button(actions,text="添加探索范围",command=add).pack(side="right")
        model_button=ttk.Button(actions,text="采用此版本模型允许值范围",style="Primary.TButton",command=lambda:add(True),state="disabled")
        model_button.pack(side="right",padx=8)

    def edit_row(self,kind,existing=False):
        table=self.parameter_table if kind=="parameters" else self.observation_table
        selected=table.selection();index=int(selected[0]) if existing and selected else None
        if existing and index is None:return
        item=copy.deepcopy(getattr(self,kind)[index]) if index is not None else {}
        editor=tk.Toplevel(self);editor.withdraw();editor.title("参数设置" if kind=="parameters" else "站点与变量设置");editor.transient(self)
        editor.configure(bg="#EAF3FF");editor.geometry(self.dialog_geometry(820,980))
        bottom=tk.Frame(editor,bg="#EAF3FF");bottom.pack(side="bottom",fill="x",padx=20,pady=14)
        body,canvas=self.scroll_form(editor);body.grid_columnconfigure(1,weight=1)
        editor.bind("<MouseWheel>",lambda event:self.wheel_form(event,canvas),add="+")
        variables={}
        if kind=="parameters":
            specs=[("name","名称",item.get("name",""),None),("native_name","SWAT+原生参数名",item.get("native_name",item.get("column",item.get("name",""))),None),
                   ("object_type","对象类型",item.get("object_type","hru"),["hru","sol","aqu","cha","rte","bsn"]),
                   ("method","变化方式",{"absolute":"absval"}.get(item.get("method"),item.get("method","absval")),["absval","abschg","pctchg"]),
                   ("lower","搜索下限",item.get("lower",""),None),("upper","搜索上限",item.get("upper",""),None),
                   ("coordinate_min","初始硬下限（留空=搜索下限）",item.get("coordinate_min",""),None),
                   ("coordinate_max","初始硬上限（留空=搜索上限）",item.get("coordinate_max",""),None),
                   ("object_ids","对象编号（逗号分隔；空=全部）",",".join(map(str,item.get("object_ids",[]))),None),("layer","土层范围（如1或1,2；空=全部）",display_layer(item.get("layer")),None)]
        else:
            target=item.get("target",{})
            specs=[("id","观测id",item.get("id",""),None),("path","观测CSV文件",item.get("path",""),None),("date_column","日期列",item.get("date_column","Date"),None),
                   ("value_column","数值列",item.get("value_column","Flow"),None),("date_format","日期格式",item.get("date_format","%Y-%m-%d"),None),
                   ("frequency","时间步长",item.get("frequency","monthly"),["daily","monthly"]),("unit","观测及输出单位",item.get("unit","m3/s"),None),
                   ("metric","评价指标",item.get("metric","NSE"),["NSE","KGE","NRMSE","gaussian_loglik"]),("weight","权重",item.get("weight",1),None),
                   ("scale","NRMSE尺度（仅该指标必填）",item.get("scale",""),None),("sigma","误差sigma（高斯似然必填）",item.get("sigma",""),None),
                   ("object","SWAT+输出对象",target.get("object","channel_sd"),["channel_sd","basin_wb","hru_wb","aquifer"]),
                   ("variable","输出变量名",target.get("variable","flo_out"),None),("unit_id","输出单元编号（运行前必须填写）",target.get("unit_id",""),None),
                   ("gis_id","GIS编号（可留空）",target.get("gis_id",""),None),("kind","资料类型",item.get("kind","unverified"),["unverified","verified_observation","official_tutorial_reference"]),
                   ("label","资料说明",item.get("label",""),None),("missing_codes","缺测标记（逗号分隔）",",".join(map(str,item.get("missing_codes",["-9999"]))),None)]
        for row,(key,label,value,choices) in enumerate(specs):variables[key],_=self.field(body,label,str(value),row,choices)
        if kind=="parameters":
            source_note=tk.StringVar(value=item.get("range_source_note",item.get("note","")))
            version=self.config.get("model",{}).get("expected_version")
            explanatory=ttk.Label(body,text="",background="#EAF3FF",foreground="#566F90",wraplength=640,justify="left")
            explanatory.grid(row=len(specs),column=0,columnspan=2,sticky="w",pady=12)
            ttk.Label(body,textvariable=source_note,background="#EAF3FF",foreground="#566F90",wraplength=640,justify="left").grid(row=len(specs)+1,column=0,columnspan=2,sticky="w",pady=8)
            def adopt_model_bounds():
                try:
                    catalog=json.loads((self.workspace/"config/native_parameter_catalog.json").read_text(encoding="utf-8"))["parameters"]
                    definition=copy.deepcopy(item);definition.update({key:variables[key].get().strip() for key in ("name","native_name","method")})
                    adopted=apply_model_allowed_bounds(definition,catalog,version)
                    for key in ("lower","upper","coordinate_min","coordinate_max"):variables[key].set(str(adopted[key]))
                    item.setdefault("sources",adopted.get("sources",[]));source_note.set(adopted["range_source_note"])
                except (ValueError,KeyError,OSError) as error:messagebox.showerror("无法采用模型范围",str(error),parent=editor)
            model_button=ttk.Button(bottom,text="采用此版本模型允许值范围",command=adopt_model_bounds)
            model_button.pack(side="left")
            def update_parameter_method(*_):
                absolute=variables["method"].get() in ("absval","absolute")
                model_button.configure(state="normal" if absolute else "disabled")
                explanatory.configure(text=(f"当前版本 {version or '未设置'}：absval以参数绝对值为坐标，可采用模型允许值范围。初始搜索须处于硬边界内。" if absolute else
                    "当前填写的是百分比（pctchg）或加减变化量（abschg），搜索与硬边界均采用同一变化量坐标，不能直接套参数物理值范围。")+
                    "\n范围来自版本模型字典，不是全流域通用率定标准。运行时仍按实际输入cal_parms.cal核验。")
            variables["method"].trace_add("write",update_parameter_method);update_parameter_method()
            editor.parameter_variables=variables;editor.model_bounds_button=model_button
        if kind=="observations":
            def choose():
                path=filedialog.askopenfilename(parent=editor,filetypes=[("CSV","*.csv"),("全部文件","*.*")])
                if path:variables["path"].set(path)
            ttk.Button(bottom,text="选择观测文件…",command=choose).pack(side="left")
        def accept():
            try:
                values={key:var.get().strip() for key,var in variables.items()}
                if kind=="parameters":
                    item.update({key:values[key] for key in ("name","native_name","object_type","method")})
                    item.update(lower=finite(values["lower"],"下限"),upper=finite(values["upper"],"上限"))
                    for key,label in (("coordinate_min","初始硬下限"),("coordinate_max","初始硬上限")):
                        item.pop(key,None)
                        if values[key]:item[key]=finite(values[key],label)
                    parameter_hard_bounds(item)
                    if source_note.get():item["range_source_note"]=source_note.get()
                    for key in ("object_ids","layer"):item.pop(key,None)
                    if values["object_ids"]:item["object_ids"]=[int(value.strip()) for value in values["object_ids"].split(",")]
                    layer=parse_layer(values["layer"])
                    if layer is not None:item["layer"]=layer
                else:
                    for key in ("id","path","date_column","value_column","date_format","frequency","unit","metric","kind","label"):item[key]=values[key]
                    item["weight"]=finite(values["weight"],"权重",True)
                    item["missing_codes"]=values["missing_codes"].split(",")
                    for key in ("scale","sigma"):
                        item.pop(key,None)
                        if values[key]:item[key]=finite(values[key],key,True)
                    target=copy.deepcopy(item.get("target",{}));target.update(object=values["object"],variable=values["variable"],unit=values["unit"])
                    for key in ("unit_id","gis_id"):
                        target.pop(key,None)
                        if values[key]:target[key]=int(values[key])
                    item["target"]=target
                if index is None:getattr(self,kind).append(item)
                else:getattr(self,kind)[index]=item
                self.refresh_tables();editor.destroy()
            except ValueError as error:messagebox.showerror("请检查输入",str(error),parent=editor)
        editor.save_row_button=ttk.Button(bottom,text="保存本行",style="Primary.TButton",command=accept)
        editor.save_row_button.pack(side="right")
        editor.deiconify()
        return editor

    def save(self):
        try:
            cfg=copy.deepcopy(self.config);cfg["workflow_mode"]="research";cfg["parameter_backend"]="native_calibration"
            method=METHODS[self.method.get()];cfg.setdefault("search",{}).update(method=method,repetitions=int(self.repetitions.get()),seed=int(self.seed.get()),workers=1,objective="JOINT")
            if method=="spotpy_sceua":
                ngs=int(self.ngs.get())
                if ngs<2:raise ValueError("SCE-UA复合形数量至少为2")
                cfg["search"].setdefault("options",{})["ngs"]=ngs
            if method=="swatcup_sufi2":
                sizes=parse_round_sizes(self.sufi_round_sizes.get())
                cfg["search"]["repetitions"]=sum(sizes)
                options=cfg["search"].setdefault("options",{})
                options.pop("iterations",None);options.pop("samples_per_iteration",None)
                options.update(round_sizes=sizes,continuation=CONTINUATION[self.sufi_continuation.get()],
                    behavioral_threshold=finite(self.sufi_threshold.get(),"SUFI-2行为阈值"),executable_dir=self.sufi_directory.get().strip(),
                    screening={"enabled":self.sufi_screen_enabled.get(),"mode":SCREENING[self.sufi_screen_mode.get()],
                        "after_round":int(self.sufi_screen_round.get()),"p_threshold":finite(self.sufi_screen_p.get(),"敏感性p值阈值"),
                        "min_active":int(self.sufi_screen_min.get()),
                        "protected_parameters":[x.strip() for x in self.sufi_protected.get().replace("，",",").split(",") if x.strip()]})
            cfg["parameters"]=copy.deepcopy(self.parameters);cfg["observations"]=copy.deepcopy(self.observations);cfg["observation"]=copy.deepcopy(self.observations[0]) if self.observations else {}
            for parameter in cfg["parameters"]:
                if "layer" in parameter:
                    layer=parse_layer(display_layer(parameter["layer"]))
                    if layer is None:parameter.pop("layer")
                    else:parameter["layer"]=layer
            kind=self.sensitivity.get();samples=int(self.samples.get())
            if samples<(4 if kind=="morris" else 2):raise ValueError("Morris至少4条轨迹；Sobol基础样本至少为2")
            minimum=int(self.minimum.get())
            if minimum<2:raise ValueError("最少行为样本数至少为2")
            previous=cfg.get("analysis",{}).get("sensitivity",{})
            cfg.setdefault("analysis",{}).setdefault("sensitivity",{}).update(enabled=kind!="关闭",method=kind if kind!="关闭" else "morris",trajectories=samples,base_sample_size=samples,calc_second_order=previous.get("calc_second_order",False),seed=int(self.seed.get()))
            cfg["analysis"].setdefault("uncertainty",{}).update(enabled=self.uncertainty.get(),threshold=finite(self.threshold.get(),"行为阈值"),min_behavioral=minimum)
            validate_edit(cfg)
        except (ValueError,KeyError) as error:messagebox.showerror("配置尚未完成",str(error),parent=self);return
        target=filedialog.asksaveasfilename(parent=self,initialdir=str(self.workspace/"config"),initialfile=self.path.stem+"_custom.json",defaultextension=".json",filetypes=[("JSON配置","*.json")])
        if not target:return
        target=Path(target)
        if target.resolve()==self.path.resolve():messagebox.showerror("请另存配置","请选择新文件名，保留当前配置。",parent=self);return
        try:target.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        except OSError as error:messagebox.showerror("保存失败",str(error),parent=self);return
        self.on_save(target);self.destroy()
