"""Supplement daily channel output and compare it with traceable observations.

Figure: a quantitative two-panel hydrograph, daily hero panel + monthly panel.
Purpose: show timing and volume mismatches without calibrating the existing example;
neither the spatial model nor the parameters were optimized for this figure.
Backend: Python only. Exports: 183 x 164 mm SVG/PDF, 300-dpi PNG, source CSVs.
No smoothing, lag fitting, gap filling, rescaling, or removal of observation flags.
"""
import argparse
import calendar
import csv
import datetime as dt
import json
import math
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

from run_parameter_demo import lines, write_lines


def run_with_daily(source, destination, model, executable):
    destination.mkdir(parents=True, exist_ok=False)
    for p in source.iterdir():
        if p.is_file() and p.suffix in model["input_extensions"]:
            shutil.copy2(p, destination / p.name)
    p = lines(destination / "print.prt")
    found = 0
    for i, line in enumerate(p):
        fields = line.split()
        if fields and fields[0] == "channel_sd":
            fields[1] = "y"
            p[i] = " ".join(fields)
            found += 1
    if found != 1:
        raise ValueError("Expected one channel_sd print setting")
    write_lines(destination / "print.prt", p)
    different = [x.name for x in source.iterdir() if x.is_file() and x.suffix in model["input_extensions"]
                 and x.read_bytes() != (destination / x.name).read_bytes()]
    if different != ["print.prt"]:
        raise ValueError(f"Unexpected input changes: {different}")
    start = time.perf_counter()
    with (destination.parent / (destination.name + "_console.log")).open("w", encoding="utf-8") as log:
        process = subprocess.run([str(executable)], cwd=destination, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 timeout=model["timeout_seconds"], check=False)
    elapsed = time.perf_counter() - start
    if process.returncode != 0 or not (destination / "success.fin").exists():
        raise RuntimeError(f"Run failed: {destination}, exit {process.returncode}")
    print(f"{destination.name}: {elapsed:.3f} seconds, exit {process.returncode}", flush=True)
    return {"elapsed_seconds": round(elapsed, 3), "exit_code": process.returncode,
            "changed_input_files": different}


def read_outlet(path, model, monthly=False):
    """Stream all channel records, retaining only the verified outlet."""
    values = []
    scanned = 0
    with path.open(encoding="utf-8-sig") as stream:
        banner = next(stream).strip()
        header = next(stream).split()
        units = next(stream).split()
        keys = [x for x in header[7:] if x != "null"]
        if len(keys) != len(units) or dict(zip(keys, units))["flo_out"] != "m^3/s":
            raise ValueError("Output flow unit mismatch")
        if model["expected_version"] not in banner:
            raise ValueError("Model version mismatch")
        idx = {x: header.index(x) for x in ["unit", "gis_id", "name", "yr", "mon", "day", "jday", "flo_out"]}
        for line in stream:
            row = line.split()
            if not row:
                continue
            scanned += 1
            if int(row[idx["unit"]]) != model["outlet_unit"]:
                continue
            if row[idx["name"]] != model["outlet_name"] or int(row[idx["gis_id"]]) != model["outlet_gis_id"]:
                raise ValueError("Outlet mapping mismatch")
            date = dt.date(int(row[idx["yr"]]), int(row[idx["mon"]]), 1 if monthly else int(row[idx["day"]]))
            if not monthly and date.timetuple().tm_yday != int(row[idx["jday"]]):
                raise ValueError("Day-of-year mismatch")
            value = float(row[idx["flo_out"]])
            if not math.isfinite(value) or value < 0:
                raise ValueError("Invalid simulated flow")
            values.append((pd.Timestamp(date), value))
    frame = pd.DataFrame(values, columns=["date", "flow"]).set_index("date")
    if frame.index.has_duplicates:
        raise ValueError("Duplicate outlet dates")
    first_year = model["start_year"] + model["warmup_years"]
    end = f"{model['end_year']}-12-01" if monthly else f"{model['end_year']}-12-31"
    expected = pd.date_range(f"{first_year}-01-01", end, freq="MS" if monthly else "D")
    if not frame.index.equals(expected):
        raise ValueError("Incomplete or unordered outlet dates")
    return frame["flow"], {"all_channel_records_scanned": scanned, "outlet_records": len(frame), "banner": banner}


def metrics(obs, sim):
    obs, sim = np.asarray(obs, dtype=float), np.asarray(sim, dtype=float)
    if not np.isfinite(obs).all() or not np.isfinite(sim).all():
        raise ValueError("Nonfinite values in paired data")
    r = float(np.corrcoef(obs, sim)[0, 1])
    alpha = float(np.std(sim) / np.std(obs))
    beta = float(np.mean(sim) / np.mean(obs))
    return {"n": len(obs), "NSE": float(1 - np.sum((sim-obs)**2) / np.sum((obs-np.mean(obs))**2)),
            "KGE_2009": 1 - math.sqrt((r-1)**2 + (alpha-1)**2 + (beta-1)**2),
            "r": r, "std_ratio": alpha, "mean_ratio": beta,
            "bias_percent_sim_minus_obs": float(100 * (np.sum(sim)-np.sum(obs)) / np.sum(obs)),
            "RMSE_m3_s": float(np.sqrt(np.mean((sim-obs)**2))),
            "mean_observed_m3_s": float(np.mean(obs)), "mean_simulated_m3_s": float(np.mean(sim))}


def make_figure(daily, monthly, scores, out, stem):
    font_manager.findfont("Microsoft YaHei", fallback_to_default=False)
    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Microsoft YaHei", "DejaVu Sans"],
                         "font.size": 8, "axes.titlesize": 9, "axes.labelsize": 8,
                         "axes.unicode_minus": False, "svg.fonttype": "none", "pdf.fonttype": 42,
                         "axes.spines.right": False, "axes.spines.top": False,
                         "axes.linewidth": 0.65, "legend.frameon": False})
    fig, axes = plt.subplots(2, 1, figsize=(183/25.4, 164/25.4), gridspec_kw={"height_ratios": [1.25, 1]})
    fig.subplots_adjust(left=0.105, right=0.98, bottom=0.12, top=0.81, hspace=0.39)
    colors = {"observed_m3_s": "#333333", "control_m3_s": "#277DA8", "changed_m3_s": "#C47A36"}
    labels = {"observed_m3_s": "观测：SYKE Oulankajoki站", "control_m3_s": "模拟：ESCO=0.95", "changed_m3_s": "模拟：ESCO=0.50"}
    for ax, frame, frequency, tag in zip(axes, [daily, monthly], ["daily", "monthly"], ["a", "b"]):
        for col in colors:
            is_obs = col == "observed_m3_s"
            ax.plot(frame.index, frame[col], color=colors[col], label=labels[col],
                    linewidth=1.0 if is_obs else 0.85, linestyle="--" if col == "changed_m3_s" else "-",
                    marker="o" if frequency == "monthly" and is_obs else None,
                    markersize=2.6, zorder=3 if is_obs else 2)
        ax.set_ylabel("流量（m³/s）")
        ax.set_ylim(bottom=0)
        ax.set_xlim(pd.Timestamp("2010-12-26"), pd.Timestamp("2013-01-05"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 4, 7, 10]))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.tick_params(axis="both", labelsize=7, width=0.6)
        ax.grid(axis="y", color="#E4E7EA", linewidth=0.5, zorder=0)
        text = "逐日流量 · 731天" if frequency == "daily" else "月平均流量 · 24个月"
        ax.set_title(f"{tag}  {text}", loc="left", fontweight="bold", pad=11)
        s = scores[frequency]["control"]
        ax.text(1, 1.04, f"原参数：NSE {s['NSE']:.3f}  |  KGE {s['KGE_2009']:.3f}",
                transform=ax.transAxes, ha="right", fontsize=7, color="#555555")
    fig.suptitle("Oulanka 案例：模拟与观测流量", x=0.105, y=0.978, ha="left", fontsize=13, fontweight="bold")
    fig.text(0.105, 0.928, "现成模型由脚本自动运行 · 本次未率定 · 比较期 2011—2012年", fontsize=8.5, color="#555555")
    handles, legend_labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, legend_labels, loc="upper left", bbox_to_anchor=(0.095, 0.897), ncol=3,
               fontsize=7, columnspacing=1.6, handlelength=2.3)
    fig.text(0.105, 0.041, "观测来源：SYKE官方日流量；月值为自然月日均。2010年仅用于模型预热。", fontsize=7, color="#555555")
    fig.text(0.105, 0.014, "两条模拟线非常接近；曲线未平滑、未移位，原始流量未缩放。", fontsize=7, color="#555555")
    for suffix in ["png", "svg", "pdf"]:
        fig.savefig(out / f"{stem}.{suffix}", dpi=300, facecolor="white")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Create new copies and run model; omit to re-extract and redraw existing run")
    args = parser.parse_args()
    root = args.config.resolve().parent.parent
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    old = root / cfg["source_run"]
    out = root / cfg["output_run"]
    model = json.loads((old / "config.json").read_text(encoding="utf-8-sig"))
    if args.run:
        out.mkdir(parents=True, exist_ok=False)
        timings = {}
        for case in ["control", "changed"]:
            timings[case] = run_with_daily(old / case, out / case, model, root / model["executable"])
        (out / "run_info.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
    timings = json.loads((out / "run_info.json").read_text())
    official = {}
    flags = {}
    for filename in cfg["official_daily_files"]:
        response = json.loads((root / filename).read_text(encoding="utf-8-sig"))
        for row in response["value"]:
            date = pd.Timestamp(row["Aika"])
            if date in official:
                raise ValueError("Duplicate date across official API pages")
            official[date] = float(row["Arvo"])
            flags[date] = row["Lippu_id"]
    official = pd.Series(official).sort_index()
    supplied = pd.read_csv(root / cfg["observed_daily"], parse_dates=["date"], float_precision="round_trip").set_index("date")["discharge"]
    supplied_aligned = supplied.reindex(official.index)
    daily_obs_error = float(np.max(np.abs(supplied_aligned.values-official.values)))
    if supplied.index.has_duplicates or supplied_aligned.isna().any() or daily_obs_error > 1e-9:
        raise ValueError("Supplied observations disagree with official source")
    daily = pd.DataFrame({"observed_m3_s": official, "observation_flag": pd.Series(flags)})
    monthly = pd.DataFrame({"observed_m3_s": official.resample("MS").mean()})
    provided_monthly = pd.read_csv(root / cfg["observed_monthly"], parse_dates=["date"], float_precision="round_trip").set_index("date")["mean"]
    monthly_obs_error = float(np.max(np.abs(provided_monthly.reindex(monthly.index) - monthly["observed_m3_s"])))
    if monthly_obs_error > 1e-9:
        raise ValueError("Supplied monthly observations differ from daily means")
    verification = {"official_daily_rows": len(official), "supplied_daily_agree_within_1e_minus_9": True,
                    "supplied_daily_max_abs_error_m3_s": daily_obs_error,
                    "supplied_monthly_max_abs_error_m3_s": monthly_obs_error,
                    "observation_flags": {str(k): int(v) for k,v in pd.Series(flags).value_counts(dropna=False).items()},
                    "cases": {}}
    scores = {"daily": {}, "monthly": {}}
    for case in ["control", "changed"]:
        day, d_info = read_outlet(out / case / "channel_sd_day.txt", model)
        month, m_info = read_outlet(out / case / "channel_sd_mon.txt", model, monthly=True)
        if not day.index.equals(daily.index) or not month.index.equals(monthly.index):
            raise ValueError("Observation and simulation dates do not align")
        previous_month = pd.read_csv(old / f"{case}_outlet_monthly.csv", float_precision="round_trip")["flo_out_m3_s"].values
        if not np.array_equal(previous_month, month.values):
            raise ValueError("Opening daily output changed the previous monthly result")
        daily[case + "_m3_s"] = day
        monthly[case + "_m3_s"] = month
        month_from_day = day.resample("MS").mean()
        max_difference = float((month_from_day-month).abs().max())
        # 61.0.1 channel output uses four significant digits (0.xxxxE+yy).
        day_bounds = pd.Series(5*np.power(10.0,np.floor(np.log10(np.maximum(day.values,1e-300)))-4),index=day.index)
        month_bounds = 5*np.power(10.0,np.floor(np.log10(np.maximum(month.values,1e-300)))-4)
        allowed = day_bounds.resample("MS").mean().values + month_bounds + 1e-10
        if np.any(np.abs(month_from_day.values-month.values) > allowed):
            raise ValueError("Daily means disagree with monthly output beyond print rounding")
        verification["cases"][case] = {"daily": d_info, "monthly": m_info,
                                        "monthly_identical_to_previous_run": True,
                                        "max_daily_aggregate_vs_monthly_difference_m3_s": max_difference}
        scores["daily"][case] = metrics(daily["observed_m3_s"], day)
        scores["monthly"][case] = metrics(monthly["observed_m3_s"], month)
        scores["daily"][case]["total_volume_bias_percent"] = scores["daily"][case]["bias_percent_sim_minus_obs"]
        weights = np.array([calendar.monthrange(t.year,t.month)[1] for t in month.index])
        scores["monthly"][case]["total_volume_bias_percent"] = float(100*np.sum((month.values-monthly["observed_m3_s"].values)*weights)/np.sum(monthly["observed_m3_s"].values*weights))
    daily.to_csv(out / "paired_daily.csv", index_label="date", encoding="utf-8-sig")
    monthly.to_csv(out / "paired_monthly.csv", index_label="date", encoding="utf-8-sig")
    metric_rows = [{"frequency":f,"case":c,**v} for f,items in scores.items() for c,v in items.items()]
    pd.DataFrame(metric_rows).to_csv(out / "performance_metrics.csv", index=False, encoding="utf-8-sig")
    result = {"station": cfg["station"], "station_number": cfg["station_number"], "period_start":str(daily.index[0].date()),
              "period_end":str(daily.index[-1].date()), "run_info":timings, "verification":verification, "metrics":scores,
              "interpretation": "Existing example automatically executed; no calibration in this workflow. Upstream calibration history is unknown. Not an automatically constructed basin or independent validation.",
              "metric_definitions": {"NSE":"1-sum((sim-obs)^2)/sum((obs-mean(obs))^2)",
                 "KGE_2009":"1-sqrt((r-1)^2+(std(sim)/std(obs)-1)^2+(mean(sim)/mean(obs)-1)^2)",
                 "bias_percent_sim_minus_obs":"100*sum(sim-obs)/sum(obs); negative=underprediction. Monthly values here treat each monthly mean equally.",
                 "total_volume_bias_percent":"Daily equal-time sums; monthly means weighted by calendar days. Negative=underprediction."}}
    (out / "comparison_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    make_figure(daily, monthly, scores, out, cfg["figure_stem"])
    print(json.dumps({"output":str(out), "verification":verification, "metrics":scores},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
