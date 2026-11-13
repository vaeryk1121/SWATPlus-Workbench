"""Render the real one-stop SWAT+ workflow's results with Python/matplotlib.

Figure contract: compare baseline and calibration-selected discharge through the
calibration and held-out periods, then show the calibration-only search history.
Archetype: quantitative grid, hydrograph hero panel and smaller search panel.
Exports: 183 x 165 mm PNG (300 dpi), editable-text SVG/PDF and a Markdown report.
Input: report_data.json; discharge is monthly-mean or daily-mean m3/s. This module
does not simulate, smooth, rescale, fill missing data, fit parameters, or choose a
best trial. Metrics and the selected parameter set come from the workflow.
"""

from workflow_runtime import engine_markdown
import argparse
import datetime as dt
import json
import math
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import MaxNLocator


def _date(value):
    text = str(value).split("T")[0].replace("/", "-")
    parts = text.split("-")
    if len(parts) == 2:
        parts.append("1")
    return dt.datetime(*(int(part) for part in parts))


def _end_exclusive(value):
    text = str(value).split("T")[0].replace("/", "-")
    date = _date(value)
    if len(text.split("-")) == 2:
        return dt.datetime(date.year + (date.month == 12), date.month % 12 + 1, 1)
    return date + dt.timedelta(days=1)


def _number(value):
    """Leave missing/undefined values as gaps, never replace them with zero."""
    if value is None:
        return math.nan
    number = float(value)
    return number if math.isfinite(number) else math.nan


def _fmt(value, digits=3):
    if value is None:
        return "—"
    number = float(value)
    return f"{number:.{digits}f}" if math.isfinite(number) else "—"


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _reference_label(observation):
    kind = observation.get("kind", "")
    if kind == "official_tutorial_reference":
        return "教学参考流量"
    if kind == "verified_observation":
        return "观测流量"
    return "参考流量（来源未核实）"


def _period_text(period):
    return f"{period['start']} 至 {period['end']}"


def _plot(data, rows, out):
    font_manager.findfont("Microsoft YaHei", fallback_to_default=False)
    style = {
        "font.family": "sans-serif",
        "font.sans-serif": ["Microsoft YaHei", "DejaVu Sans"],
        "font.size": 8,
        "axes.titlesize": 9,
        "axes.labelsize": 8,
        "axes.unicode_minus": False,
        "svg.fonttype": "none",
        "pdf.fonttype": 42,
        "axes.spines.right": False,
        "axes.spines.top": False,
        "axes.linewidth": 0.65,
        "legend.frameon": False,
    }
    with plt.rc_context(style):
        fig, axes = plt.subplots(
            2, 1, figsize=(183 / 25.4, 165 / 25.4),
            gridspec_kw={"height_ratios": [1.65, 1]},
        )
        fig.subplots_adjust(left=0.11, right=0.98, bottom=0.16, top=0.79, hspace=0.57)
        ax, search = axes
        dates = [_date(row["date"]) for row in rows]
        observation = data["observation"]
        reference_label = _reference_label(observation)
        colors = {"reference": "#333333", "baseline": "#8497A6", "best": "#C0793D"}
        labels = {"reference": reference_label, "baseline": "初始模拟", "best": "率定后模拟（固定参数）"}
        for key in ["baseline", "best", "reference"]:
            ax.plot(
                dates, [_number(row[key]) for row in rows], label=labels[key],
                color=colors[key], linewidth=1.05 if key == "reference" else 1.0,
                linestyle="--" if key == "baseline" else "-",
                marker="o" if key == "reference" and data["frequency"] == "monthly" else None,
                markersize=2.3, markeredgewidth=0,
                zorder=4 if key == "reference" else 3,
            )
        for name, label, color in [
            ("calibration", "率定期", "#EAF0F4"),
            ("validation", "固定参数验证期", "#F5EDE3"),
        ]:
            period = data["periods"][name]
            start, end = _date(period["start"]), _end_exclusive(period["end"])
            ax.axvspan(start, end, facecolor=color, alpha=0.75, zorder=0)
            ax.text(
                start + (end - start) / 2, 0.98, label, ha="center", va="top",
                transform=ax.get_xaxis_transform(), color="#555555", fontsize=7,
            )
        locator = mdates.AutoDateLocator(minticks=4, maxticks=7)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.set_xlim(dates[0], max(dates[-1], dates[0] + dt.timedelta(days=1)))
        all_values = [_number(row[key]) for row in rows for key in colors]
        finite_values = [v for v in all_values if math.isfinite(v)]
        if finite_values and min(finite_values) >= 0:
            ax.set_ylim(bottom=0, top=max(finite_values) * 1.13 or 1.0)
        ax.set_ylabel("流量（m³/s）")
        frequency = "月平均流量" if data["frequency"] == "monthly" else "逐日流量"
        ax.set_title(f"a  {frequency}过程 · {len(rows)}个时段", loc="left", fontweight="bold", pad=10)

        trials = data.get("trials", [])
        if trials:
            order = list(range(1, len(trials) + 1))
            search.plot(order, [_number(row.get("score")) for row in trials],
                        color="#A7B4BE", linewidth=0.7, marker="o", markersize=2.8,
                        label="本次参数得分", zorder=2)
            search.step(order, [_number(row.get("best_so_far")) for row in trials],
                        color="#C0793D", linewidth=1.3, where="post",
                        label="截至当前最佳得分", zorder=3)
            for index in range(1, len(trials)):
                if trials[index].get("round") != trials[index - 1].get("round"):
                    search.axvline(index + 0.5, color="#CFD6DB", linestyle=":", linewidth=0.8)
            search.set_xlim(0.6, max(1.4, len(trials) + 0.4))
            search.xaxis.set_major_locator(MaxNLocator(nbins=8, integer=True))
            search.legend(loc="best", fontsize=6.8, handlelength=2.1)
        else:
            search.text(0.5, 0.5, "本次未提供参数搜索记录", ha="center", va="center",
                        transform=search.transAxes, color="#666666")
        search.set_title("b  参数搜索 · 仅使用率定期评分", loc="left", fontweight="bold", pad=9)
        search.set_xlabel("参数试验顺序（虚线分隔轮次）")
        search.set_ylabel("目标得分（越大越好）")
        for axis in axes:
            axis.tick_params(axis="both", labelsize=7, width=0.6)
            axis.grid(axis="y", color="#DEE3E7", linewidth=0.5, zorder=1)

        title = textwrap.fill(str(data.get("title", "SWAT+ 一站式建模与率定结果")), width=35)
        fig.suptitle(title, x=0.11, y=0.98, ha="left", va="top", fontsize=11.5, fontweight="bold")
        fig.text(0.11, 0.905, "全期曲线使用同一组所选参数；验证期未用于挑选参数。",
                 fontsize=7.3, color="#555555")
        handles, legend_labels = ax.get_legend_handles_labels()
        indices = [legend_labels.index(labels[key]) for key in ["reference", "baseline", "best"]]
        fig.legend([handles[i] for i in indices], [legend_labels[i] for i in indices],
                   loc="upper left", bbox_to_anchor=(0.10, 0.883), ncol=3,
                   fontsize=7.1, columnspacing=1.4, handlelength=2.5)
        footer = "曲线使用原值，未平滑、未移位、未缩放；评分和数据来源详见同目录报告。"
        if observation.get("kind") == "official_tutorial_reference":
            note = "教学参考数据：原始测站观测来源未核实，不能据此宣称真实观测验证完成。"
        elif observation.get("kind") == "verified_observation":
            note = "参考序列为已核实来源的观测；拟合与留出期评分不等同于全部水文过程验证。"
        else:
            note = "参考序列来源尚未核实；本图仅反映给定数据下的计算结果。"
        fig.text(0.11, 0.065, note, fontsize=6.8, color="#555555")
        fig.text(0.11, 0.032, footer, fontsize=6.8, color="#555555")
        try:
            for suffix in ("png", "pdf", "svg"):
                fig.savefig(out / f"comparison.{suffix}", dpi=300, facecolor="white")
        finally:
            plt.close(fig)


def _markdown(data, rows):
    observation = data["observation"]
    kind = observation.get("kind", "")
    reference_label = _reference_label(observation)
    frequency = "月平均" if data["frequency"] == "monthly" else "逐日平均"
    lines = [
        f"# {data.get('title', 'SWAT+ 一站式建模与率定结果')}", "",
        *engine_markdown(data.get("engine_info")),
        data.get("workflow_note", ""), "",
        f"本报告对比初始模拟、率定后模拟与{reference_label}。流量口径为{frequency}流量（m³/s）。",
        "参数仅根据率定期选择，随后保持同一组参数进行验证期评分；验证期不参与挑参。", "",
        "![流量对比与参数搜索](comparison.png)", "",
        "上图展示全期流量，下图展示按实际试验顺序排列的率定期得分；竖虚线分隔搜索轮次。",
        "曲线未平滑、未移位、未缩放；缺失或不可计算值保留为空缺。", "",
        "## 分期结果", "",
        f"- 率定期：{_period_text(data['periods']['calibration'])}。",
        f"- 固定参数验证期：{_period_text(data['periods']['validation'])}。", "",
        "| 时段 | 参数设置 | n | NSE | KGE | RMSE（m³/s） | 流量均值偏差（%） |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    if data.get("search_engine", {}).get("method") == "spotpy_dds":
        versions=data["search_engine"]["versions"]
        lines[4:4]=[
            f"率定引擎：SPOTPY {versions['spotpy']} 的 DDS 原实现；单目标、单序列。",
            "参数接口：SWAT+ 原生 calibration.cal（参数、作用对象和修改类型见任务配置）。"
            if data.get("parameter_backend")=="native_calibration" else "参数接口：直接修改输入文件。",
            "此结果不包含95PPU等不确定性分析；随机种子、范围和预算见同目录 config.resolved.json。", ""]
    for period, period_label in [("calibration", "率定期"), ("validation", "验证期")]:
        for setting, label in [("baseline", "初始"), ("best", "率定后固定参数")]:
            values = data["metrics"][setting][period]
            lines.append(
                f"| {period_label} | {label} | {_fmt(values.get('n'), 0)} | "
                f"{_fmt(values.get('NSE'))} | {_fmt(values.get('KGE'))} | "
                f"{_fmt(values.get('RMSE'))} | {_fmt(values.get('bias_percent'), 2)} |"
            )
    lines.extend([
        "", "n 为该时段实际参与评分的配对样本数；NSE、KGE 越接近1越好，RMSE越小越好。",
        "流量均值偏差按 100 ×（模拟均值 / 参考均值 − 1）计算，正值为高估、负值为低估；“—”表示未定义或未提供。",
        "月尺度按各月平均流量等权比较，不等于按月份天数加权的体积偏差；日尺度对应各日平均流量的均值比。",
        "指标直接读取运行流程的实际计算结果，绘图模块不重新挑选或调整参数。", "",
        "## 所选参数", "", "| 参数 | 本次取值 |", "|---|---:|",
    ])
    for name, value in data.get("best_parameters", {}).items():
        rendered = f"{value:.8g}" if isinstance(value, (float, int)) else str(value)
        lines.append(f"| {_cell(name)} | {_cell(rendered)} |")
    trials = data.get("trials", [])
    finite_scores = sum(math.isfinite(_number(row.get("score"))) for row in trials)
    lines.extend([
        "", f"记录了{len(trials)}次参数试验，其中{finite_scores}次具有有效得分。",
        "以上取值的赋值、增量或相对变化含义以运行配置为准，不能脱离配置直接当成原始模型参数。", "",
        "## 数据来源与限制", "",
        f"- 数据性质：{reference_label}。",
    ])
    if observation.get("label"):
        lines.append(f"- 资料名称：{observation['label']}。")
    if observation.get("source_url"):
        lines.append(f"- 来源：[资料发布或说明页面]({observation['source_url']})。")
    if observation.get("provenance_note"):
        lines.append(f"- 来源核查：{observation['provenance_note']}")
    if kind == "official_tutorial_reference":
        lines.append("- 此结果验证的是教学参考数据下的自动建模、参数搜索和固定参数留出评分流程；原始观测来源未核实，不能据此宣称真实观测率定与独立验证已经完成。")
    elif kind != "verified_observation":
        lines.append("- 参考数据来源未完成核实，结果不得当作真实测站观测验证结论。")
    lines.extend([
        "- 最佳仅指本次参数范围、目标函数和试验预算内被选中的方案，不表示全局最优，也不能直接移用到另一流域。",
        "- 本图未计算置信区间或预测不确定性范围；参数搜索次数不构成独立水文样本。", "",
        "## 既有成果复用", "", data.get("source_reuse_note", "本次未提供来源复用说明。"), "",
        "## 输出", "",
        "- [PNG图](comparison.png)、[可编辑SVG](comparison.svg)、[PDF图](comparison.pdf)。",
        "- [报告源数据](report_data.json)：包含逐时段流量、分期指标、参数与试验轨迹。", "",
    ])
    return "\n".join(lines)


def render_report(job_dir):
    """Create only report.md and comparison.{png,pdf,svg} in an existing job."""
    out = Path(job_dir).resolve()
    data = json.loads((out / "report_data.json").read_text(encoding="utf-8-sig"))
    if data["frequency"] not in {"monthly", "daily"}:
        raise ValueError("frequency must be monthly or daily")
    rows = sorted(data["series"], key=lambda row: _date(row["date"]))
    if not rows:
        raise ValueError("Cannot plot an empty discharge series")
    if len({_date(row["date"]) for row in rows}) != len(rows):
        raise ValueError("Duplicate dates in report_data.json")
    for row in rows:
        if row["period"] not in {"calibration", "validation"}:
            raise ValueError("Every series row must belong to calibration or validation")
        for key in ["reference", "baseline", "best"]:
            _number(row[key])
    _plot(data, rows, out)
    (out / "report.md").write_text(_markdown(data, rows), encoding="utf-8")
    return {name: str(out / name) for name in ["report.md", "comparison.png", "comparison.pdf", "comparison.svg"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(render_report(args.run_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
