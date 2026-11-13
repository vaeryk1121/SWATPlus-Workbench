"""Small SUFI-2 round review window; imports only the Python standard library."""
import copy
import datetime as dt
import json
import math
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


def diagnostic_reason(value):
    value=str(value or "未满足诊断要求")
    translations={
        "Exploratory sample only:":"本轮样本较少，仅供探索；自动冻结至少需要30组且不少于参数数的5倍，达到数量也不保证筛选可靠。",
        "Fewer than 5 residual degrees":"剩余自由度少于5，统计结果不稳定，不自动冻结参数。",
        "Collinear or ill-conditioned":"参数组合存在共线性或数值不稳定，不能据此自动筛选。",
        "Insufficient residual degrees":"样本数不足，或某个参数没有变化，不能可靠估计敏感性。",
        "Zero/negligible residual variance":"残差几乎为零，t值和p值计算不稳定。",
        "Nonfinite or misaligned":"评分或参数数据存在无效值，或样本未正确对应。",
    }
    return next((text for prefix,text in translations.items() if value.startswith(prefix)),value)


def validate_review(pending, payload):
    """Reject stale rounds, nonfinite ranges and changes outside initial hard bounds."""
    if not pending or type(payload.get("after_round")) is not int or payload["after_round"]!=pending.get("after_round"):
        raise ValueError("轮次已经变化，请刷新后重新查看")
    if payload.get("action") not in ("continue","finish"):raise ValueError("请选择继续或结束搜索")
    if payload["action"]=="finish":return payload
    active=pending.get("active_parameters",[])
    retained=payload.get("retain_parameters",active)
    if not isinstance(retained,list) or len(set(retained))!=len(retained) or not retained or any(name not in active for name in retained):
        raise ValueError("至少保留一项当前活动参数，已冻结参数不能在此恢复")
    protected=pending.get("screening",{}).get("protected_parameters",[])
    if any(name in active and name not in retained for name in protected):
        raise ValueError("关键参数不能冻结；需保留继续搜索")
    count=payload.get("next_sample_count",pending.get("next_sample_count"))
    if type(count) is not int or not max(8,len(retained)+2)<=count<=2000:
        raise ValueError("下一轮候选数须为8至2000，且至少比活动参数数多2")
    for name,interval in payload.get("ranges",{}).items():
        if name not in retained:raise ValueError("只能修改下一轮继续搜索参数的范围")
        if not isinstance(interval,(list,tuple)) or len(interval)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in interval):
            raise ValueError(f"{name}：范围须为两个有限数值")
        lo,hi=interval;hard=pending.get("hard_bounds",{}).get(name)
        if not hard or lo>=hi or lo<hard[0] or hi>hard[1]:
            raise ValueError(f"{name}：下一轮范围必须位于初始硬边界{hard}内，且下限小于上限")
    return payload


def load_review_data(job):
    job=Path(job)
    state=json.loads((job/"state.json").read_text(encoding="utf-8-sig"))
    data=state.get("sufi2",{})
    return state,data,copy.deepcopy(data.get("pending_review") or {})


def save_review(job,payload):
    """Persist the explicit decision; the workflow validates it again before applying."""
    state,_data,pending=load_review_data(job)
    if state.get("status")!="paused":raise ValueError("任务当前未暂停，请刷新后重新查看")
    validate_review(pending,payload)
    folder=Path(job)/"sufi2"/"reviews";folder.mkdir(parents=True,exist_ok=True)
    stamp=dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path=folder/f"round_{payload['after_round']:02d}_{stamp}.json"
    path.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return path


def centered_geometry(width,height,screen_width,screen_height,parent_rect):
    """Keep a dialog on screen even when its parent spans a negative monitor."""
    margin=12
    width=min(width,max(100,screen_width-90));height=min(height,max(100,screen_height-100))
    parent_x,parent_y,parent_width,parent_height=parent_rect
    if parent_width<100 or parent_height<100:
        parent_x,parent_y,parent_width,parent_height=0,0,screen_width,screen_height
    x=round(parent_x+(parent_width-width)/2);y=round(parent_y+(parent_height-height)/2)
    x=max(margin,min(x,screen_width-width-margin));y=max(margin,min(y,screen_height-height-margin))
    return f"{width}x{height}+{x}+{y}"


class SufiReview(tk.Toplevel):
    def __init__(self,parent,job,on_resume):
        super().__init__(parent);self.withdraw()
        self.job=Path(job);self.on_resume=on_resume
        try:self.state,self.data,self.pending=load_review_data(self.job)
        except (OSError,ValueError) as error:
            messagebox.showerror("无法读取轮次",str(error),parent=parent);self.destroy();return
        self.title("SUFI-2 每轮诊断与下一轮设置");self.transient(parent)
        scale=max(1.,float(parent.tk.call("tk","scaling"))/(96/72))
        self.geometry(centered_geometry(round(1180*scale),round(800*scale),self.winfo_screenwidth(),self.winfo_screenheight(),
            (parent.winfo_rootx(),parent.winfo_rooty(),parent.winfo_width(),parent.winfo_height())))
        self.configure(bg="#F4F8FE")
        self.can_review=bool(self.pending) and self.state.get("status")=="paused"
        self.retained=set(self.pending.get("active_parameters",[]))
        self.ranges=copy.deepcopy(self.pending.get("next_ranges",{}))
        self.active=list(self.pending.get("active_parameters",[]))
        self.protected=set(self.pending.get("screening",{}).get("protected_parameters",[]))
        title="第{}轮结束 · 等待决定".format(self.pending["after_round"]) if self.pending else "SUFI-2 已完成轮次"
        tk.Label(self,text=title,bg="#F4F8FE",fg="#163B67",font=("Microsoft YaHei UI",16,"bold")).pack(anchor="w",padx=20,pady=(16,8))
        reason=self.pending.get("reason") or "没有等待审阅的下一轮；历史轮次仅供查看。"
        tk.Label(self,text=reason,bg="#F4F8FE",fg="#785021",wraplength=round(1100*scale),justify="left",font=("Microsoft YaHei UI",10)).pack(fill="x",padx=20,pady=(0,10))
        book=self.book=ttk.Notebook(self);book.pack(fill="both",expand=True,padx=20)
        plan=tk.Frame(book,bg="#EAF3FF");history=tk.Frame(book,bg="#EAF3FF")
        book.add(plan,text="下一轮范围与敏感性");book.add(history,text="已完成轮次")
        self.build_plan(plan);self.build_history(history)
        if not self.pending:book.select(history)
        bar=tk.Frame(self,bg="#F4F8FE");bar.pack(fill="x",padx=20,pady=16)
        ttk.Button(bar,text="关闭",command=self.destroy).pack(side="left")
        if self.can_review:
            ttk.Button(bar,text="结束搜索，转固定验证",command=lambda:self.submit("finish")).pack(side="right",padx=8)
            ttk.Button(bar,text="采用当前设置并继续",style="Primary.TButton",command=self.submit).pack(side="right")
        self.deiconify()

    def build_plan(self,page):
        top=tk.Frame(page,bg="#EAF3FF");top.pack(fill="x",padx=14,pady=12)
        self.count=tk.StringVar(value=str(self.pending.get("next_sample_count","")))
        ttk.Label(top,text="下一轮候选数",background="#EAF3FF").pack(side="left",padx=(0,8))
        editing_state="normal" if self.can_review else "disabled"
        self.edit_controls=[]
        count_entry=ttk.Entry(top,textvariable=self.count,width=10,state=editing_state);count_entry.pack(side="left")
        screening_button=ttk.Button(top,text="采用初筛建议",command=self.use_screening,state=editing_state);screening_button.pack(side="left",padx=12)
        self.edit_controls.extend([count_entry,screening_button])
        tk.Label(page,text="双击参数切换“继续搜索 / 冻结”。冻结固定在已选参数值；硬边界保持不变。\n默认保留全部活动参数。t/p为Python辅助线性回归，小样本结果仅作初筛线索；关键参数始终保留。",bg="#EAF3FF",fg="#566F90",justify="left",font=("Microsoft YaHei UI",10)).pack(anchor="w",padx=14,pady=(0,8))
        body=tk.Frame(page,bg="#EAF3FF");body.pack(fill="both",expand=True,padx=14)
        columns=("action","name","hard","range","t","p","note")
        self.table=ttk.Treeview(body,columns=columns,show="headings",selectmode="browse",height=10)
        for key,label,width in zip(columns,("下一轮","参数","初始硬边界","下一轮搜索范围","t-stat","p-value","初筛提示"),(100,90,140,150,90,90,200)):
            self.table.heading(key,text=label);self.table.column(key,width=width,minwidth=60)
        scroll=ttk.Scrollbar(body,orient="vertical",command=self.table.yview);scroll.pack(side="right",fill="y")
        self.table.configure(yscrollcommand=scroll.set);self.table.pack(fill="both",expand=True)
        self.table.bind("<Double-1>",lambda _e:self.toggle_parameter())
        self.table.bind("<<TreeviewSelect>>",lambda _e:self.select_parameter())
        edit=tk.Frame(page,bg="#EAF3FF");edit.pack(fill="x",padx=14,pady=12)
        self.lower=tk.StringVar();self.upper=tk.StringVar()
        for label,var in (("所选下限",self.lower),("所选上限",self.upper)):
            ttk.Label(edit,text=label,background="#EAF3FF").pack(side="left",padx=(0,6))
            entry=ttk.Entry(edit,textvariable=var,width=12,state=editing_state);entry.pack(side="left",padx=(0,12));self.edit_controls.append(entry)
        edit_button=ttk.Button(edit,text="更新所选范围",command=self.edit_range,state=editing_state);edit_button.pack(side="left")
        self.edit_controls.append(edit_button)
        self.range_explanation=tk.Label(page,text="选择参数，查看本轮范围、原版建议与最终采用范围。",bg="#EAF3FF",fg="#566F90",justify="left",wraplength=1000)
        self.range_explanation.pack(fill="x",padx=14,pady=(0,8))
        details=self.pending.get("sensitivity",{})
        message=self.pending.get("screening",{}).get("note","")
        if details.get("status")=="unreliable":message="敏感性分析暂不可靠："+diagnostic_reason(details.get("reason"))+"\n"+message
        tk.Label(page,text=message,bg="#EAF3FF",fg="#785021",justify="left",wraplength=1000).pack(fill="x",padx=14,pady=(0,12))
        self.refresh_table()

    @staticmethod
    def number(value):
        return f"{value:.4g}" if type(value) in (int,float) and math.isfinite(value) else "—"

    @staticmethod
    def interval(value):
        return " ～ ".join(SufiReview.number(x) for x in value) if isinstance(value,(list,tuple)) and len(value)==2 else "—"

    def refresh_table(self):
        selected=self.table.selection();self.table.delete(*self.table.get_children())
        sensitivities={row["name"]:row for row in self.pending.get("sensitivity",{}).get("parameters",[]) if row.get("name")}
        frozen=self.pending.get("frozen_parameters",{})
        suggestions=self.pending.get("screening",{}).get("suggested_frozen",{})
        clipped=self.pending.get("diagnostics",{}).get("range_clipped",[])
        for name in self.active+list(frozen):
            item=sensitivities.get(name,{})
            action="已冻结" if name in frozen else "继续搜索" if name in self.retained else "拟冻结"
            note="关键参数，保留" if name in self.protected else "建议查看是否冻结" if name in suggestions else ""
            if name in clipped:note+="；推荐范围已按硬边界截取"
            interval=[frozen[name],frozen[name]] if name in frozen else self.ranges.get(name)
            self.table.insert("","end",iid=name,values=(action,name,self.interval(self.pending.get("hard_bounds",{}).get(name)),self.interval(interval),self.number(item.get("t_stat")),self.number(item.get("p_value")),note))
        if selected and self.table.exists(selected[0]):self.table.selection_set(selected)

    def select_parameter(self):
        selection=self.table.selection()
        if selection and selection[0] in self.ranges:
            name=selection[0];lo,hi=self.ranges[name];self.lower.set(str(lo));self.upper.set(str(hi))
            rounds=self.data.get("rounds",[]);current=rounds[-1] if rounds else {}
            parameter=next((p for p in current.get("parameters",[]) if p.get("name")==name),{})
            previous=self.pending.get("previous_ranges",{}).get(name,[parameter.get("lower"),parameter.get("upper")])
            raw=self.pending.get("raw_suggested_ranges",{}).get(name,current.get("result",{}).get("raw_new_ranges",{}).get(name))
            self.range_explanation.configure(text=f"{name}：本轮范围 {self.interval(previous)} → 原版建议 {self.interval(raw)} → 下一轮采用 {self.interval([lo,hi])}。")

    def toggle_parameter(self):
        if not self.can_review:return
        selection=self.table.selection()
        if not selection:return
        name=selection[0]
        if name not in self.active:return
        if name in self.protected:
            messagebox.showinfo("关键参数",f"{name}已设置为关键参数，继续搜索。",parent=self);return
        if name in self.retained:self.retained.remove(name)
        else:self.retained.add(name)
        self.refresh_table()

    def use_screening(self):
        if not self.can_review:return
        suggested=self.pending.get("screening",{}).get("suggested_retained")
        if not suggested:
            messagebox.showinfo("没有冻结建议","当前初筛不足以提出冻结建议，保留原参数继续搜索。",parent=self);return
        self.retained=set(suggested)|self.protected
        self.refresh_table()

    def edit_range(self):
        if not self.can_review:return
        selection=self.table.selection()
        if not selection:return
        name=selection[0]
        try:
            if name not in self.retained:raise ValueError("请先将参数设为继续搜索")
            interval=[float(self.lower.get()),float(self.upper.get())]
            payload={"after_round":self.pending["after_round"],"action":"continue",
                     "retain_parameters":[n for n in self.active if n in self.retained],
                     "next_sample_count":int(self.count.get()),"ranges":{name:interval}}
            validate_review(self.pending,payload);self.ranges[name]=interval;self.refresh_table()
        except (ValueError,KeyError) as error:messagebox.showerror("范围未采用",str(error),parent=self)

    def build_history(self,page):
        text=self.history_text=tk.Text(page,wrap="word",bg="#F5F9FF",fg="#163B67",font=("Microsoft YaHei UI",10),bd=0,padx=12,pady=12)
        scroll=ttk.Scrollbar(page,orient="vertical",command=text.yview);scroll.pack(side="right",fill="y")
        text.configure(yscrollcommand=scroll.set);text.pack(fill="both",expand=True,padx=10,pady=10)
        for index,current in enumerate(self.data.get("rounds",[]),1):
            diagnostic=current.get("diagnostics",{})
            text.insert("end",f"第{index}轮 · {len(current.get('trial_ids',[]))}组 · {'统计完成' if current.get('result') else current.get('status','试算记录')}\n")
            if diagnostic:
                text.insert("end",f"最佳评分 {self.number(diagnostic.get('best_score'))}    P-factor {self.number(diagnostic.get('p_factor'))}    R-factor {self.number(diagnostic.get('r_factor'))}\n")
                for warning in diagnostic.get("warnings",[]):text.insert("end",str(warning)+"\n")
            result=current.get("result",{})
            previous={p["name"]:[p.get("lower"),p.get("upper")] for p in current.get("parameters",[]) if p.get("name")}
            for name,value in result.get("new_ranges",{}).items():
                text.insert("end",f"  {name}：本轮 {self.interval(previous.get(name))} → 原版建议 {self.interval(result.get('raw_new_ranges',{}).get(name))} → 边界内建议 {self.interval(value)}\n")
            sensitivity=current.get("sensitivity",{})
            text.insert("end","\n回归敏感性（Python辅助计算，不是原版程序导出的t/p表）\n")
            if sensitivity:
                text.insert("end",f"样本数 {sensitivity.get('n','—')}；活动参数 {sensitivity.get('dimensions','—')}；剩余自由度 {sensitivity.get('df','—')}。\n")
                if sensitivity.get("status")=="unreliable":
                    text.insert("end","暂不可靠："+diagnostic_reason(sensitivity.get("reason"))+"\n")
                elif not sensitivity.get("automatic_screening_allowed",False):
                    text.insert("end","小样本或其他诊断条件不足：仅用于探索，不自动据此冻结参数。\n")
                for entry in sensitivity.get("parameters",[]):
                    text.insert("end",f"  {entry.get('name','—')}    t-stat = {self.number(entry.get('t_stat'))}    p-value = {self.number(entry.get('p_value'))}\n")
                text.insert("end","较大p值不等于水文过程不重要，也不能排除非线性效应。\n")
                if current.get("screening",{}).get("note"):
                    text.insert("end","初筛说明："+diagnostic_reason(current["screening"]["note"])+"\n")
            else:text.insert("end","该历史轮次未保存回归t/p表；界面不会推测缺失统计结果。\n")
            decision=current.get("next_decision")
            if decision:
                action={"continue":"继续","finish":"结束搜索"}.get(decision.get('action'),decision.get('action'))
                text.insert("end",f"下一轮已采用决定：{action}；继续搜索：{', '.join(decision.get('active_parameters',[]))}\n")
                if decision.get("frozen_parameters"):text.insert("end","冻结固定值："+str(decision["frozen_parameters"])+"\n")
            text.insert("end","\n")
        if not self.data.get("rounds"):text.insert("end","暂无已完成轮次。\n")
        text.configure(state="disabled")

    def submit(self,action="continue"):
        try:
            payload={"after_round":self.pending["after_round"],"action":action}
            if action=="continue":
                payload.update(next_sample_count=int(self.count.get()),retain_parameters=[n for n in self.active if n in self.retained],
                               ranges={n:self.ranges[n] for n in self.active if n in self.retained})
            path=save_review(self.job,payload)
        except (ValueError,KeyError,OSError) as error:
            messagebox.showerror("尚未继续",str(error),parent=self);return
        self.on_resume(path);self.destroy()
