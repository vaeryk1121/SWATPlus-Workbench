"""Check normal autostart of both plugins in one QGIS profile; never load them here."""
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
from types import SimpleNamespace as NS

import qgis._core
import qgis.utils
from qgis.core import (
    Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsPointXY, QgsProject, QgsProviderRegistry,
)
from qgis.PyQt.QtCore import QSettings, QTimer
from qgis.PyQt.QtWidgets import QMessageBox, QPlainTextEdit, QTextEdit

ROOT = Path(os.environ["SWATPLUS_WORKSPACE"])
CFG = json.loads((ROOT / "config/qgis344_runtime.json").read_text(encoding="utf-8"))
MF_CFG = json.loads((ROOT / "config/qswatmod344_runtime.json").read_text(encoding="utf-8"))
PROFILE = (ROOT / CFG["profile_root"] / "profiles" / CFG["profile_name"]).resolve()
PLUGIN_DIRS = {name: PROFILE / "python/plugins" / name for name in ("QSWATPlus", "QSWATMOD2")}
REPORT = Path(os.environ.get("SWATPLUS_QGIS344_COMBINED_CHECK",
                            str(ROOT / "tools/qgis344_runtime/combined_check.json")))
dialogs, logs = [], []


def capture_dialog(self, *args, **kwargs):
    dialogs.append({"title": self.windowTitle(), "text": self.text()})
    return QMessageBox.StandardButton.Ok


def capture_static(parent, title, text, *args, **kwargs):
    dialogs.append({"title": str(title), "text": str(text)})
    return QMessageBox.StandardButton.Ok


# Only this disposable process intercepts error dialogs. No settings are changed.
QMessageBox.exec_ = capture_dialog
QMessageBox.critical = capture_static
QMessageBox.warning = capture_static
QMessageBox.information = capture_static
QgsApplication.messageLog().messageReceived.connect(
    lambda message, tag, level: logs.append(
        {"message": str(message), "tag": str(tag), "level": int(level)}))


def check_combined():
    result = {"pid": os.getpid(), "scope": "One profile, both plugins naturally autostarted",
              "checks": {}, "dialogs": dialogs, "message_log": logs}

    def check(name, action):
        before = len(dialogs)
        try:
            detail = action()
            assert len(dialogs) == before, dialogs[before:]
            result["checks"][name] = {"passed": True, "detail": detail}
        except Exception:
            result["checks"][name] = {"passed": False, "traceback": traceback.format_exc()}

    try:
        assert Path(QgsApplication.qgisSettingsDirPath()).resolve() == PROFILE, "Wrong profile"
        assert Qgis.QGIS_VERSION.startswith("3.44."), "Wrong QGIS"
        assert Path(qgis._core.__file__).resolve().is_relative_to(Path(CFG["qgis_root"]).resolve())
        assert (ROOT / MF_CFG["plugin_root"]).resolve() == PLUGIN_DIRS["QSWATMOD2"]
        active = list(qgis.utils.active_plugins)
        result.update(qgis_version=Qgis.QGIS_VERSION, python_version=sys.version,
                      profile=str(PROFILE), active_plugins_on_entry=active,
                      plugin_search_paths=[str(p) for p in qgis.utils.plugin_paths])
        # Never call loadPlugin/startPlugin: those would conceal failed autostart.
        assert set(PLUGIN_DIRS).issubset(active), "A requested plugin did not autostart"
        settings = QSettings()
        assert Path(settings.fileName()).resolve().is_relative_to(PROFILE)
        assert all(settings.value("PythonPlugins/" + name, False, type=bool) for name in PLUGIN_DIRS)
        assert not {"QSWATPlus3_9", "ee_plugin", "MapSWAT_v3"}.intersection(active)
        assert not any("/qgis3/profiles/default/" in str(p).replace("\\", "/").lower()
                       for p in qgis.utils.plugin_paths), "Legacy plugin directory in search path"
        result["checks"]["natural_autostart"] = {"passed": True, "detail": active}

        # --code runs after plugin initialization; retain existing message-pane tracebacks too.
        existing = []
        text_widgets = (qgis.utils.iface.mainWindow().findChildren(QTextEdit) +
                        qgis.utils.iface.mainWindow().findChildren(QPlainTextEdit))
        for widget in text_widgets:
            text = widget.toPlainText()
            if text and any(word in text for word in ("Traceback", "QSWAT", "Python")):
                existing.append({"object_name": widget.objectName(), "text": text[-20000:]})
        result["existing_message_text"] = existing
        assert not any("Traceback" in item["text"] for item in existing), "Startup traceback in message pane"
        plus = qgis.utils.plugins["QSWATPlus"]
        mod = qgis.utils.plugins["QSWATMOD2"]

        def constructors():
            assert not plus.loadFailed and plus._odlg is not None and mod.dlg is not None
            assert type(plus).__module__.startswith("QSWATPlus.")
            assert type(mod).__module__.startswith("QSWATMOD2.")
            assert plus._odlg is not mod.dlg and plus.action not in mod.actions
            assert plus.action.isEnabled() and all(a.isEnabled() for a in mod.actions)
            return {"swatplus_class": type(plus).__module__, "qswatmod_class": type(mod).__module__,
                    "distinct_dialogs": True, "distinct_toolbar_actions": True}
        check("both_constructors_and_actions", constructors)

        def module_origins():
            origins, versions = {}, {}
            for name, expected in PLUGIN_DIRS.items():
                metadata = configparser.ConfigParser()
                metadata.read(expected / "metadata.txt", encoding="utf-8-sig")
                versions[name] = metadata.get("general", "version")
                origins[name] = {}
                for key, module in list(sys.modules.items()):
                    if key == name or key.startswith(name + "."):
                        path = getattr(module, "__file__", None)
                        if path:
                            assert Path(path).resolve().is_relative_to(expected), (key, path)
                            origins[name][key] = str(path)
            assert versions == {"QSWATPlus": CFG["plugin_version"],
                                "QSWATMOD2": MF_CFG["plugin_version"]}
            assert (PLUGIN_DIRS["QSWATMOD2"] / "LOCAL_COMPATIBILITY.txt").is_file()
            result["module_origins"] = origins
            return {"versions": versions, "module_counts": {n: len(v) for n, v in origins.items()}}
        check("separate_namespaces_shared_profile", module_origins)

        def compiled_modules():
            loaded = {}
            for name in ("dataInC", "polygonizeInC", "polygonizeInC2", "jenks"):
                module = importlib.import_module("QSWATPlus.QSWATPlus." + name)
                path = Path(module.__file__).resolve()
                assert path.is_relative_to(PLUGIN_DIRS["QSWATPlus"]) and ".cp312-" in path.name
                loaded[name] = str(path)
            data = sys.modules["QSWATPlus.QSWATPlus.dataInC"]
            first = data.CellData(1, 100, 10, 2, 3, 4, 1)
            first.addCells(data.CellData(1, 200, 20, 3, 4, 5, 1))
            assert first.cellCount == 2 and first.area == 300 and first.totalElevation == 30
            return {"extensions": loaded, "synthetic_cell_accumulation": "passed"}
        check("swatplus_python312_extensions", compiled_modules)

        import numpy as np
        import pandas as pd
        from osgeo import gdal
        from QSWATMOD2.pyfolder import post_i_sw, post_ii_gw

        def gis_runtime():
            assert {"gdal", "ogr"}.issubset(QgsProviderRegistry.instance().providerList())
            raster = gdal.GetDriverByName("MEM").Create("", 2, 2, 1, gdal.GDT_Float32)
            raster.GetRasterBand(1).WriteArray(np.array([[1, 2], [3, 4]], dtype=np.float32))
            assert raster.ReadAsArray().sum() == 10
            transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                               QgsCoordinateReferenceSystem("EPSG:3857"),
                                               QgsProject.instance())
            point = transform.transform(QgsPointXY(1, 1))
            assert abs(point.x() - 111319.490793) < .01 and abs(point.y() - 111325.142866) < .01
            return {"gdal": gdal.VersionInfo(), "numpy": np.__version__, "pandas": pd.__version__,
                    "projected_point": [point.x(), point.y()]}
        check("gdal_numpy_crs_with_both_plugins", gis_runtime)

        def owner():
            return NS(dlg=NS(radioButton_day=NS(isChecked=lambda: True),
                             radioButton_month=NS(isChecked=lambda: False),
                             radioButton_year=NS(isChecked=lambda: False)))

        case = ROOT / MF_CFG["official_case_run"]
        river = post_i_sw.read_output_rch_data(None, str(case))
        stream = post_i_sw.update_index(owner(), river.loc[int(max(river.index))].copy(), "01/01/1985")
        observations, heads = post_ii_gw.read_swatmf_out_MF_obs(None, str(case))
        heads = post_ii_gw.update_index(None, heads.copy(), "01/01/1985")
        assert len(stream) == len(heads) == 365
        result["real_output_rows"] = {"river": len(river), "selected_reach": len(stream),
                                     "heads": len(heads), "observation_cells": len(observations)}

        def aggregate(function, frame, period, stale=False):
            state = owner()
            if stale:
                state.df = frame * 0 + 123456
            actual = function(state, frame.copy(), period)
            groups = defaultdict(list)
            for date, values in zip(frame.index, frame.to_numpy()):
                groups[(date.year, date.month if period == "Monthly" else 12)].append(values)
            dates, values = [], []
            for (year, month), items in sorted(groups.items()):
                dates.append(datetime(year, month, monthrange(year, month)[1]))
                values.append([sum(float(v[c]) for v in items) / len(items)
                               for c in range(frame.shape[1])])
            assert actual.index.tolist() == dates
            np.testing.assert_allclose(actual.to_numpy(), values, rtol=1e-12, atol=1e-12)
            return {"rows": len(actual), "stale_previous_data": stale,
                    "maximum_difference": float(np.max(np.abs(actual.to_numpy() - values)))}
        for name, function, frame in (("stream", post_i_sw.time_cvt, stream),
                                      ("groundwater", post_ii_gw.time_cvt, heads)):
            for period in ("Monthly", "Annual"):
                check(name + "_" + period.lower(), lambda f=function, d=frame, p=period: aggregate(f, d, p))
            check(name + "_annual_stale", lambda f=function, d=frame: aggregate(f, d, "Annual", True))
        check("constructors_remain_distinct_after_processing", constructors)
        critical = [item for item in logs if item["level"] == int(Qgis.Critical) and
                    any(word in item["message"] + item["tag"] for word in ("Python", "QSWAT", "Traceback"))]
        assert not critical and not dialogs, {"critical": critical, "dialogs": dialogs}
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        result["passed"] = not result.get("error") and bool(result["checks"]) and all(
            check["passed"] for check in result["checks"].values())
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        QgsApplication.quit()


QTimer.singleShot(2500, check_combined)
