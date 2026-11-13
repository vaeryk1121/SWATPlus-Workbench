"""Exercise the local QSWATMOD2 compatibility fixes against real model outputs."""

import configparser
import importlib
import json
import os
import sys
import traceback
from calendar import monthrange
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import qgis.utils
from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import QSettings, QTimer
from qgis.PyQt.QtWidgets import QMessageBox

ROOT = Path(os.environ["SWATPLUS_WORKSPACE"])
CFG = json.loads((ROOT / "config/qswatmod344_runtime.json").read_text(encoding="utf-8"))
PROFILE = (ROOT / CFG["profile_root"] / "profiles" / CFG["profile_name"]).resolve()
PLUGIN = (ROOT / CFG["plugin_root"]).resolve()
REPORT = ROOT / "tools/qswatmod344_runtime/compatibility_check_fixed.json"
PLOTS = REPORT.parent / "verification_plots"
messages = []


def dialog_message(self, *args, **kwargs):
    messages.append({"title": self.windowTitle(), "text": self.text()})
    return QMessageBox.StandardButton.Ok


def static_message(parent, title, text, *args, **kwargs):
    messages.append({"title": str(title), "text": str(text)})
    return QMessageBox.StandardButton.Ok


# Only the check process converts blocking error dialogs to recorded messages.
QMessageBox.exec_ = dialog_message
QMessageBox.critical = static_message
QMessageBox.warning = static_message
QMessageBox.information = static_message


def run_check():
    report = {"pid": os.getpid(), "source_unmodified": False,
              "local_patch": "pandas3 and matplotlib3 compatibility; model calculations unchanged",
              "checks": {}}
    try:
        if Path(QgsApplication.qgisSettingsDirPath()).resolve() != PROFILE:
            raise RuntimeError("Not the separate qswatmod344 profile.")
        settings = QSettings()
        if not Path(settings.fileName()).resolve().is_relative_to(PROFILE):
            raise RuntimeError("Refusing to change settings outside qswatmod344.")
        settings.setValue("PythonPlugins/QSWATMOD2", True)
        settings.sync()
        if "QSWATMOD2" not in qgis.utils.active_plugins:
            if not qgis.utils.loadPlugin("QSWATMOD2") or not qgis.utils.startPlugin("QSWATMOD2"):
                raise RuntimeError("Plugin failed to load/start.")
        main = importlib.import_module("QSWATMOD2.QSWATMOD2")
        if not Path(main.__file__).resolve().is_relative_to(PLUGIN):
            raise RuntimeError("Loaded a different QSWATMOD2.")
        metadata = configparser.ConfigParser()
        metadata.read(PLUGIN / "metadata.txt", encoding="utf-8-sig")
        version = metadata.get("general", "version")
        if version != CFG["plugin_version"]:
            raise RuntimeError("Unexpected plugin version.")
        import numpy as np
        import pandas as pd
        import matplotlib
        import matplotlib.pyplot as plt
        from QSWATMOD2.modules import flopy
        from QSWATMOD2.pyfolder import post_i_sw, post_ii_gw, post_iv_gwsw
        PLOTS.mkdir(exist_ok=True)
        plt.ioff()

        report.update(
            qgis_version=Qgis.QGIS_VERSION, python_version=sys.version,
            plugin_version=version, plugin_file=main.__file__, profile=str(PROFILE),
            settings_file=settings.fileName(),
            dependencies={"numpy": np.__version__, "pandas": pd.__version__,
                          "matplotlib": matplotlib.__version__, "flopy": flopy.__version__},
            plugin_loaded=True,
            dialog_constructed=hasattr(qgis.utils.plugins["QSWATMOD2"], "dlg"),
            active_plugins=list(qgis.utils.active_plugins),
        )

        def check(name, action):
            start = len(messages)
            try:
                value = action()
                if value is None and len(messages) > start:
                    raise RuntimeError(messages[-1]["text"])
                report["checks"][name] = {"passed": True, "detail": value}
            except Exception as exc:
                report["checks"][name] = {"passed": False, "error": f"{type(exc).__name__}: {exc}",
                                          "traceback": traceback.format_exc()}

        def state(input_period="Daily"):
            return SimpleNamespace(dlg=SimpleNamespace(
                radioButton_day=SimpleNamespace(isChecked=lambda: input_period == "Daily"),
                radioButton_month=SimpleNamespace(isChecked=lambda: input_period == "Monthly"),
                radioButton_year=SimpleNamespace(isChecked=lambda: input_period == "Annual"),
                checkBox_depthTowater=SimpleNamespace(isChecked=lambda: True)))

        def independent_means(frame, period):
            """Manual grouping, with no pandas resample/date-range implementation."""
            if period == "Daily":
                return frame.copy()
            groups = defaultdict(list)
            for stamp, values in zip(frame.index, frame.to_numpy()):
                groups[(stamp.year, stamp.month if period == "Monthly" else 12)].append(values)
            dates, means = [], []
            for (year, month), values in sorted(groups.items()):
                dates.append(datetime(year, month, monthrange(year, month)[1]))
                means.append([sum(float(v[c]) for v in values) / len(values)
                              for c in range(frame.shape[1])])
            return pd.DataFrame(means, index=pd.DatetimeIndex(dates), columns=frame.columns)

        def assert_series(actual, expected):
            assert actual is not None
            assert actual.index.tolist() == expected.index.tolist()
            assert actual.columns.tolist() == expected.columns.tolist()
            np.testing.assert_allclose(actual.to_numpy(), expected.to_numpy(), rtol=1e-12, atol=1e-12)
            return {"rows": len(actual), "max_absolute_difference":
                    float(np.max(np.abs(actual.to_numpy() - expected.to_numpy())))}

        case = ROOT / CFG["official_case_run"]
        river = post_i_sw.read_output_rch_data(None, str(case))
        sub = int(max(river.index))
        stream = post_i_sw.update_index(state(), river.loc[sub].copy(), "01/01/1985")
        observations, heads = post_ii_gw.read_swatmf_out_MF_obs(None, str(case))
        heads = post_ii_gw.update_index(None, heads.copy(), "01/01/1985")
        assert len(stream) == len(heads) == 365
        report["source_output"] = {"folder": str(case), "river_rows": len(river),
                                   "selected_reach": sub, "head_rows": len(heads),
                                   "head_columns": len(observations)}
        for label, function, frame in (("stream", post_i_sw.time_cvt, stream),
                                       ("groundwater", post_ii_gw.time_cvt, heads)):
            for period in ("Daily", "Monthly", "Annual"):
                check(label + "_" + period.lower(), lambda f=function, d=frame, p=period:
                      assert_series(f(state(), d.copy(), p), independent_means(d, p)))
            # A previous plot's data must not contaminate this request.
            def stale_annual(f=function, d=frame):
                owner = state()
                owner.df = d.copy() * 0 + 987654.0
                return assert_series(f(owner, d.copy(), "Annual"), independent_means(d, "Annual"))
            check(label + "_annual_after_other_series", stale_annual)

        monthly = independent_means(stream, "Monthly")
        check("stream_monthly_input_to_annual", lambda: assert_series(
            post_i_sw.time_cvt(state("Monthly"), monthly.copy(), "Annual"),
            independent_means(monthly, "Annual")))
        annual = independent_means(stream, "Annual")
        check("stream_annual_input", lambda: assert_series(
            post_i_sw.update_index(state("Annual"), annual.copy(), "01/01/1985"), annual))

        grid = str(observations.index[0])
        elevation = float(observations.at[int(grid), "mf_elev"])
        def render_stream(period):
            fig, ax = plt.subplots(figsize=(8, 3))
            try:
                post_i_sw.plot_simulated(state(), ax, str(case), sub, "01/01/1985", period)
                expected = independent_means(stream, period)["stf_sim"]
                assert len(ax.lines) == 1, [text.get_text() for text in ax.texts]
                np.testing.assert_allclose(ax.lines[0].get_ydata(), expected.to_numpy(), rtol=1e-12)
                fig.canvas.draw()
                path = PLOTS / ("stream_" + period.lower() + ".png")
                fig.savefig(path, dpi=140, bbox_inches="tight")
                return {"points": len(expected), "path": str(path)}
            finally:
                plt.close(fig)

        def render_groundwater(period):
            fig, ax = plt.subplots(figsize=(8, 3))
            try:
                post_ii_gw.plot_simulated(state(), ax, str(case), grid, "01/01/1985", period)
                expected = independent_means(heads, period)[grid] - elevation
                assert len(ax.lines) == 1, [text.get_text() for text in ax.texts]
                assert ax.get_title(loc="left").startswith(period)
                np.testing.assert_allclose(ax.lines[0].get_ydata(), expected.to_numpy(), rtol=1e-12)
                fig.canvas.draw()
                path = PLOTS / ("groundwater_depth_" + period.lower() + ".png")
                fig.savefig(path, dpi=140, bbox_inches="tight")
                return {"points": len(expected), "elevation_m": elevation, "path": str(path),
                        "sign_convention": "unchanged: simulated head minus land surface elevation"}
            finally:
                plt.close(fig)

        for period in ("Monthly", "Annual"):
            check("render_stream_" + period.lower(), lambda p=period: render_stream(p))
            check("render_groundwater_depth_" + period.lower(), lambda p=period: render_groundwater(p))
        def render_colormap():
            fig, ax = plt.subplots(figsize=(4, 2))
            try:
                color = post_iv_gwsw.plt.get_cmap("viridis")
                ax.imshow(np.array([[-1.0, 0.0, 1.0]]), cmap=color)
                ax.set_title("Synthetic colormap API check")
                fig.canvas.draw()
                path = PLOTS / "gwsw_colormap_api.png"
                fig.savefig(path, dpi=100)
                return {"name": color.name, "path": str(path),
                        "scope": "colormap rendering only; not a spatial GW/SW map"}
            finally:
                plt.close(fig)
        check("gwsw_colormap_api_render", render_colormap)
        report["functional_checks_passed"] = all(item["passed"] for item in report["checks"].values())
    except Exception:
        report.update(plugin_loaded=False, error=traceback.format_exc())
    report["dialogs"] = messages
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    QgsApplication.quit()


QTimer.singleShot(1500, run_check)
