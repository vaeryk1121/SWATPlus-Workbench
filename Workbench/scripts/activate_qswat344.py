"""One-time activation check, run by QGIS --code in the private profile only."""

import importlib
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

import qgis.utils
from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import QSettings, QTimer
from qgis.PyQt.QtWidgets import QMessageBox

ROOT = Path(os.environ["SWATPLUS_WORKSPACE"])
sys.path.insert(0, str(ROOT / "scripts"))
from check_qgis344_support import check_support

messages = []


def nonblocking_message(parent, title, text, *args, **kwargs):
    messages.append({"title": str(title), "text": str(text)})
    return QMessageBox.StandardButton.Ok


# Only this temporary check process suppresses dialogs so an import error is reportable.
QMessageBox.critical = nonblocking_message
QMessageBox.warning = nonblocking_message
QMessageBox.information = nonblocking_message


def activate():
    report = {"pid": os.getpid(), "scope": "Private QSWAT+ activation and file paths; no Editor or model run."}
    try:
        cfg = check_support()
        expected = (cfg["profile_root"] / "profiles" / cfg["profile_name"]).resolve()
        actual = Path(QgsApplication.qgisSettingsDirPath()).resolve()
        settings = QSettings()
        if actual != expected or not Path(settings.fileName()).resolve().is_relative_to(expected):
            raise RuntimeError("Refusing to write settings outside the private swatplus344 profile.")
        if not Qgis.QGIS_VERSION.startswith(cfg["qgis_version_prefix"]):
            raise RuntimeError("Wrong QGIS version.")
        settings.setValue("/QSWATPlus/SWATPlusDir", str(cfg["support_entry"]))
        settings.setValue("/QSWATPlus/mpiexecDir", str(cfg["mpi_dir"]))
        settings.setValue("/QSWATPlus/NumProcesses", 0)
        settings.setValue("PythonPlugins/QSWATPlus", True)
        settings.sync()
        if "QSWATPlus" not in qgis.utils.active_plugins:
            if not qgis.utils.loadPlugin("QSWATPlus") or not qgis.utils.startPlugin("QSWATPlus"):
                raise RuntimeError("QSWAT+ did not load or start.")
        main = importlib.import_module("QSWATPlus.QSWATPlus.QSWATPlusMain")
        parameters = importlib.import_module("QSWATPlus.QSWATPlus.parameters").Parameters
        taudem = importlib.import_module("QSWATPlus.QSWATPlus.TauDEMUtils").TauDEMUtils
        if main.QSWATPlus.__version__ != cfg["plugin_version"]:
            raise RuntimeError("Unexpected QSWAT+ version.")
        if not Path(main.__file__).resolve().is_relative_to(cfg["plugin_root"].resolve()):
            raise RuntimeError("QSWAT+ came from another profile.")
        actual_taudem = Path(taudem.findTauDEMDir(settings, False)[0])
        if actual_taudem.resolve() != (cfg["support_root"] / "TauDEM539Bin").resolve():
            raise RuntimeError("TauDEM did not resolve to the new support directory.")
        extensions = {}
        for name in ("dataInC", "jenks", "polygonizeInC", "polygonizeInC2"):
            module = importlib.import_module("QSWATPlus.QSWATPlus." + name)
            extensions[name] = module.__file__
        databases = {}
        for name in ("QSWATPlusProj.sqlite", "swatplus_datasets.sqlite"):
            path = cfg["support_entry"] / "Databases" / name
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
                databases[name] = connection.execute(
                    "select count(*) from sqlite_master where type='table'"
                ).fetchone()[0]
        report.update(
            passed=True, qgis_version=Qgis.QGIS_VERSION, qswat_version=main.QSWATPlus.__version__,
            python_version=sys.version, profile=str(actual), settings_file=settings.fileName(),
            plugin_module=main.__file__, extensions=extensions,
            support_entry=str(cfg["support_entry"]), support_actual=str(cfg["support_entry"].resolve()),
            plugin_default_support=parameters._SWATPLUSDEFAULTDIR,
            taudem=str(actual_taudem), taudem_actual=str(actual_taudem.resolve()),
            mpi_executable=taudem.findMPIExecPath(settings), database_tables=databases,
            active_plugins=list(qgis.utils.active_plugins),
        )
    except Exception:
        report.update(passed=False, error=traceback.format_exc())
    report["dialogs"] = messages
    (ROOT / "tools/qgis344_runtime/qswat_activation_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    QgsApplication.quit()


QTimer.singleShot(1500, activate)
