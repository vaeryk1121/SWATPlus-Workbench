"""Source-backed Python figures for joint calibration and ensemble diagnostics.

Figure argument: assess fit/held-out performance per explicit observation,
then show range-dependent sensitivity and conditional ensemble coverage.
Quantitative panels, white background, unsmoothed series; PNG/PDF/SVG plus
paired CSV. No fitting, parameter selection, or rescaling is done here.
"""
import csv
import datetime as dt
import json
from pathlib import Path
import shutil


def sufi2_round_sections(folder):
    """Read saved calibration decisions only; this never reruns statistics."""
    state_path=Path(folder)/"state.json"
    if not state_path.is_file():
        return ["", "本报告没有可读取的逐轮状态记录。"]
    state=json.loads(state_path.read_text(encoding="utf-8"))
    rounds=state.get("sufi2",{}).get("rounds",[])
    if not rounds:
        return ["", "尚无完成的SUFI-2轮次。"]
    def number(value):
        return "—" if value is None else f"{value:.6g}" if isinstance(value,(int,float)) else str(value)
    def cell(value):
        return str(value).replace("|","／").replace("\n"," ")
    def interval(bounds):
        return "—" if not bounds else f"[{number(bounds[0])}, {number(bounds[1])}]"
    def values(mapping):
        return "；".join(f"{name}={number(value)}" for name,value in mapping.items()) or "无"
    lines=["", "### 每轮记录", "",
           "| 轮次 | 参数组合数 | 参与搜索的参数 | 固定值（仍写入模型） | 本轮最佳NSE | P-factor | R-factor |",
           "|---:|---:|---|---|---:|---:|---:|"]
    for current in rounds:
        result=current.get("result",{});band=result.get("all_interval",{})
        names="、".join(p["name"] for p in current["parameters"])
        lines.append(f"| {current['iteration']} | {len(current['trial_ids'])} | {cell(names)} | "
                     f"{cell(values(current.get('frozen_parameters',{})))} | {number(result.get('best_nse'))} | "
                     f"{number(band.get('p_factor'))} | {number(band.get('r_factor'))} |")
    lines.extend(["", "本表的P/R使用该轮全部样本的原版95PPU。所选单组参数取历轮及初始模型的率定期最佳，"
                  "最终区间使用最后一轮固定集合；两者来源可能不同。验证期不参与筛选或范围更新。"])
    for current in rounds:
        iteration=current["iteration"];result=current.get("result",{})
        lines.extend(["",f"### 第{iteration}轮：范围与筛选","",
                      "| 参数 | 本轮范围 | 原版范围建议 | 与固定硬边界的交集 | 固定硬边界 |",
                      "|---|---|---|---|---|"])
        for p in current["parameters"]:
            name=p["name"];hard=[p.get("coordinate_min",p["lower"]),p.get("coordinate_max",p["upper"])]
            lines.append(f"| {cell(name)} | {interval([p['lower'],p['upper']])} | "
                         f"{interval(result.get('raw_new_ranges',{}).get(name))} | "
                         f"{interval(result.get('new_ranges',{}).get(name))} | {interval(hard)} |")
        diagnostics=current.get("diagnostics",{})
        if diagnostics:
            lines.extend(["",f"相对上一轮的本轮最佳NSE变化：{number(diagnostics.get('improvement'))}；"
                          f"成功组合：{diagnostics.get('n_completed','—')}；曾出现失败的组合：{diagnostics.get('n_failed','—')}。"])
            lines.extend(f"- {warning}" for warning in diagnostics.get("warnings",[]))
        sensitivity=current.get("sensitivity")
        if sensitivity:
            lines.extend(["", "**t/p来源：Python辅助多元线性回归（NumPy OLS、SciPy双侧t检验），不是原版SWAT-CUP输出的t/p表。** "
                          "原版程序仍独立负责本轮范围建议和95PPU；原版Jacobian敏感性保存在Echo文件中。",
                          "",f"样本数 {sensitivity.get('n','—')}，搜索维数 {sensitivity.get('dimensions','—')}，"
                          f"残差自由度 {sensitivity.get('df','—')}。自动筛选条件检查："
                          f"{'通过' if sensitivity.get('automatic_screening_allowed') else '未通过，仅供探索'}。"])
            if sensitivity.get("minimum_auto_screening_samples"):
                lines.append(f"本轮自动筛选最低样本要求：{sensitivity['minimum_auto_screening_samples']}。"
                             "max(30,5×维数)是保守的运行规则，不是统计功效或参数可辨识性的保证。")
            lines.extend(["",f"诊断说明：{sensitivity.get('reason','—')}。",
                          "", "| 参数 | 标准化坐标回归系数 | t值 | p值 |",
                          "|---|---:|---:|---:|"])
            lines.extend(f"| {cell(row['name'])} | {number(row.get('coefficient'))} | {number(row.get('t_stat'))} | "
                         f"{number(row.get('p_value'))} |" for row in sensitivity.get("parameters",[]))
            lines.append("\n较大的p值不证明参数无效；结果依赖本次范围、样本数及线性近似，也不能排除非线性和交互作用。")
        else:
            lines.extend(["","本轮尚未完成统计。" if not result else "旧任务未记录辅助回归t/p，本报告未追算或编造。"])
        screening=current.get("screening",{})
        if screening:
            lines.extend(["",f"建议保留：{'、'.join(screening.get('suggested_retained',[])) or '未记录'}；"
                          f"建议固定：{values(screening.get('suggested_frozen',{}))}；"
                          f"保护参数：{'、'.join(screening.get('protected_parameters',[])) or '无'}。"])
        decision=current.get("next_decision")
        if decision:
            source="用户逐轮审阅" if decision.get("source")=="user_review" else "预先配置的自动规则"
            if decision["action"]=="finish":
                lines.extend(["",f"本轮决定：{source}结束搜索，再执行固定参数验证；不表示已经收敛。"])
            else:
                lines.extend(["",f"本轮决定：{source}；下一轮{decision['next_sample_count']}组，"
                              f"保留{'、'.join(decision['active_parameters'])}，"
                              f"固定{values(decision.get('frozen_parameters',{}))}。",
                              "下一轮实际采用范围："+ "；".join(f"{name}{interval(bounds)}" for name,bounds in decision["ranges"].items())+"。"])
            if decision.get("note"):lines.append("审阅备注："+cell(decision["note"]))
        elif state.get("sufi2",{}).get("pending_review",{}).get("after_round")==iteration:
            lines.extend(["","本轮决定：等待用户审阅，下一轮尚未采样。"])
        else:
            lines.extend(["","本轮试算或统计尚未完成，后续决定尚未生成。" if not result else
                          "本轮未记录后续决定：可能已用完预算，或为未记录该字段的旧任务。"])
    return lines


def render_report(folder):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    root=Path(folder);data=json.loads((root/"research_report_data.json").read_text(encoding="utf-8"))
    style={"font.family":"sans-serif","font.sans-serif":["Microsoft YaHei","DejaVu Sans"],
           "font.size":10,"axes.titlesize":12,"axes.labelsize":10,"axes.unicode_minus":False,
           "svg.fonttype":"none","pdf.fonttype":42,"axes.spines.top":False,"axes.spines.right":False,
           "legend.frameon":False,"figure.facecolor":"white"}
    date=lambda x:dt.datetime.fromisoformat(x)
    fmt=lambda x:"—" if x is None else f"{x:.4f}" if isinstance(x,(int,float)) else str(x)
    def axis_label(obs):
        label={"flo_out":"流量","et":"蒸散量","precip":"降水量","perc":"土壤渗漏量","surq_gen":"地表产流量","water_temp":"水温"}.get(obs["target"]["variable"],obs["target"]["variable"])
        unit={"m3/s":"m³/s","m^3/s":"m³/s","degc":"°C"}.get(obs["unit"],obs["unit"])
        return f"{label}（{unit}）"
    def save(fig,name):
        for extension in ["png","pdf","svg"]:fig.savefig(root/f"{name}.{extension}",dpi=220,bbox_inches="tight",facecolor="white")
        plt.close(fig)
    def time_axis(ax):
        locator=mdates.AutoDateLocator(minticks=4,maxticks=8)
        ax.xaxis.set_major_locator(locator);ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.grid(alpha=.18);ax.margins(x=.01)
    lines=[f"# {data['title']}","",f"算法：{data['method']}。参数仅按率定期资料选择；验证期不参与搜索或集合筛选。",
           "", "联合评分按各观测配置的指标、单位和权重计算；它不是跨变量混合的一个NSE。DREAM使用显式误差标准差的联合高斯对数似然。",
           "", "## 各观测序列的率定与验证", "",
           "| 观测 | 时段 | 设置 | n | NSE | KGE | RMSE | 单位 |", "|---|---|---|---:|---:|---:|---:|---|"]
    with plt.rc_context(style):
        for i,obs in enumerate(data["observations"]):
            identity=obs["id"];reference=data["references"][identity]
            dates=sorted(d for d in reference if any(p["start"]<=d<=p["end"] for p in data["periods"].values()))
            raw=[]
            for d in dates:
                period=next(k for k,p in data["periods"].items() if p["start"]<=d<=p["end"])
                raw.append({"date":d,"reference":reference[d],"baseline":data["simulations"]["baseline"][identity][d],"best":data["simulations"]["best"][identity][d],"period":period})
            with (root/f"paired_{identity}.csv").open("w",encoding="utf-8-sig",newline="") as stream:
                writer=csv.DictWriter(stream,list(raw[0]));writer.writeheader();writer.writerows(raw)
            for period in data["periods"]:
                for key,label in [("baseline","初始"),("best","固定所选参数")]:
                    m=data["metrics"][key][period]["by_observation"][identity]
                    lines.append(f"| {identity} | {period} | {label} | {m['n']} | {fmt(m['NSE'])} | {fmt(m['KGE'])} | {fmt(m['RMSE'])} | {obs['unit']} |")
            fig,ax=plt.subplots(figsize=(10.5,4.7),layout="constrained")
            x=[date(r["date"]) for r in raw]
            ref_label="教学参考" if obs.get("kind")=="official_tutorial_reference" else "观测" if obs.get("kind")=="verified_observation" else "参考（来源待核实）"
            for key,label,color,style_line in [("reference",ref_label,"#273548","-"),("baseline","初始模拟","#8CA0B8","--"),("best","所选参数模拟","#2469B6","-")]:
                ax.plot(x,[r[key] for r in raw],label=label,color=color,linestyle=style_line,linewidth=1.6)
            val=data["periods"]["validation"]
            ax.axvspan(date(val["start"]),date(val["end"]),color="#EAF2FC",alpha=.6,zorder=0,label="验证期")
            ax.set(title=obs.get("label",identity),ylabel=axis_label(obs))
            time_axis(ax);ax.legend(ncol=4,loc="upper left",fontsize=9)
            save(fig,f"observation_{identity}")
            if i==0:
                for ext in ["png","pdf","svg"]:shutil.copy2(root/f"observation_{identity}.{ext}",root/f"comparison.{ext}")
        lines.extend(["", "NSE/KGE越接近1越好；RMSE按各变量自己的单位报告。样本数为实际配对数量，不视为独立重复试验。", "", "## 参数搜索", ""])
        fig,ax=plt.subplots(figsize=(10.5,4),layout="constrained")
        trials=data["trials"];x=list(range(len(trials)))
        ax.plot(x,[r["score"] for r in trials],"o-",color="#A4B4C8",markersize=3,label="本次联合评分")
        ax.step(x,[r["best_so_far"] for r in trials],where="post",color="#236BC4",label="截至当前最佳")
        ax.set(xlabel="计算顺序（0为初始模型）",ylabel="联合评分（越大越好）",title="参数搜索仅使用率定期资料")
        ax.grid(alpha=.18);ax.legend();save(fig,"search_trace")
        lines.extend(["![搜索轨迹](search_trace.png)","",f"实际搜索调用：{data['optimizer'].get('evaluations',len(trials)-1)}；停止原因：{data['optimizer'].get('stop_reason','未知')}。预算结束不等于收敛。","", "| 参数 | 所选值 |","|---|---:|"])
        lines.extend(f"| {k} | {v:.8g} |" for k,v in data["best_parameters"].items())
        if data.get("sensitivity"):
            sensitivity=data["sensitivity"]["joint"];indices=sensitivity["indices"]
            fig,ax=plt.subplots(figsize=(9,max(3,0.4*len(indices)+1.7)),layout="constrained")
            method=sensitivity["method"];key="mu_star" if method=="morris" else "ST";conf="mu_star_conf" if method=="morris" else "ST_conf"
            rows=sorted(indices,key=lambda r:r[key]);ys=list(range(len(rows)))
            ax.barh(ys,[r[key] for r in rows],xerr=[r[conf] for r in rows],color="#639BD3",capsize=3)
            ax.set(yticks=ys,yticklabels=[r["name"] for r in rows],xlabel="平均绝对基本效应 μ*" if method=="morris" else "总效应指数 ST",title=f"{method}：对联合评分的参数敏感性")
            ax.grid(axis="x",alpha=.15);save(fig,"sensitivity")
            lines.extend(["", "## 敏感性分析", "", "![敏感性](sensitivity.png)","",f"{sensitivity['evaluations']}组设计，误差线为指标的{100*sensitivity['conf_level']:.0f}%重采样置信区间。排序依赖本次参数范围和响应定义。"]+sensitivity.get("notes",[]))
        uncertainty=data.get("uncertainty") or {}
        sufi=data["method"]=="swatcup_sufi2"
        if sufi:
            lines.extend(["", "## SUFI-2引擎与范围更新", "",
                          "本次调用已安装的经典SWAT-CUP原版SUFI-2统计程序，SWAT+参数修改与模拟由本工具适配执行。它不是SWATplus-CUP/SPE，也没有调用Toolbox率定。",
                          "每轮采样、目标函数、参数范围建议和95PPU原始文件位于sufi2目录。范围更新采用率定期结果，并限制在预先设置的坐标边界内；预算结束不证明收敛。"])
            lines.extend(sufi2_round_sections(root))
        lines.extend(["", "## 不确定性分析", "",f"状态：{uncertainty.get('status','未启用')}。",uncertainty.get("note","")])
        if uncertainty.get("status")=="computed":
            boundary=("95PPU由原版SUFI-2程序按其经验分位规则计算；验证沿用率定期确定的参数集合。本配置未额外加入测量误差，不能把该范围视为包含全部误差来源的保证。" if sufi else "区间仅描述给定参数集合产生的模型响应，未额外加入观测和模型结构误差；不等同完整SUFI-2。")
            lines.extend(["",f"候选模型数：{uncertainty.get('n_candidates','见配置')}；保留成员/链状态数：{uncertainty['n_members']}。"+boundary, "", "| 观测 | 时段 | P-factor | R-factor |", "|---|---|---:|---:|"])
            for obs in data["observations"]:
                identity=obs["id"];groups=uncertainty["by_observation"][identity];rows=[]
                for label,group in groups.items():
                    rows.extend(group["rows"])
                    lines.append(f"| {identity} | {label} | {fmt(group['p_factor'])} | {fmt(group['r_factor'])} |")
                rows.sort(key=lambda r:r["date"])
                with (root/f"uncertainty_{identity}.csv").open("w",encoding="utf-8-sig",newline="") as stream:
                    writer=csv.DictWriter(stream,list(rows[0]));writer.writeheader();writer.writerows(rows)
                fig,ax=plt.subplots(figsize=(10.5,4.7),layout="constrained");x=[date(r["date"]) for r in rows]
                ax.fill_between(x,[r["lower"] for r in rows],[r["upper"] for r in rows],color="#B4D4F5",alpha=.8,label="SUFI-2原生95PPU" if sufi else "参数集合2.5%–97.5%范围")
                ax.plot(x,[r["mean" if sufi else "median"] for r in rows],color="#2469B6",label="集合均值" if sufi else "集合中位数")
                ax.plot(x,[r["observed"] for r in rows],color="#273548",label="观测/参考")
                ax.axvline(date(data["periods"]["validation"]["start"]),linestyle="--",color="#7E8D9F",label="验证期起点")
                ax.set(title=obs.get("label",identity),ylabel=axis_label(obs))
                time_axis(ax);ax.legend(fontsize=8,ncol=2);save(fig,f"uncertainty_{identity}")
                lines.extend(["",f"![{identity}集合范围](uncertainty_{identity}.png)"])
        if data["method"]=="spotpy_dream":
            lines.extend(["", "## DREAM诊断", "",f"Rhat：{data['optimizer'].get('spotpy_rhat')}；每链烧入后保留数：{data['optimizer'].get('retained_per_chain')}。",
                          f"后验可用筛查：{data['optimizer'].get('posterior_usable')}。仍需检查混合和有效样本量；不能把提议点或未收敛链称为可靠后验。"])
        lines.extend(["", "## 观测与复现信息", "", "参数范围、各观测位置/变量/权重、误差模型、种子、时段和预算均保存在同目录config.resolved.json。原始观测副本单独保留，未填补缺测或改变实测值。"])
        for obs in data["observations"]:
            lines.append(f"- {obs['id']}：{obs.get('label',obs['id'])}；{obs.get('kind','unknown')}；单位{obs['unit']}；目标{obs['target']}；权重{obs.get('weight',1)}；指标{obs.get('metric','NSE')}。")
            if obs.get("source_url"):lines.append(f"  来源：{obs['source_url']}")
            if obs.get("provenance_note"):lines.append("  "+obs["provenance_note"])
    from workflow_runtime import engine_markdown
    lines.extend(engine_markdown(data.get("engine_info")))
    (root/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
