"""QGIS --code smoke probe after old-suite removal; writes a new report only."""
import configparser
import importlib
import json
import os
from pathlib import Path
import sys
import traceback
from types import SimpleNamespace

import qgis._core
import qgis.utils
from qgis.core import Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsProviderRegistry
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QMessageBox

# QGIS --code executes in the console namespace, which does not provide __file__.
ROOT = Path(os.environ["SWATPLUS_WORKSPACE"]).resolve()
PLUGIN = os.environ["SWAT_RETIREMENT_PLUGIN"]
REPORT = Path(os.environ["SWAT_RETIREMENT_REPORT"])
REPORT.resolve().relative_to(ROOT.resolve())
messages = []


def message(parent, title, text, *args, **kwargs):
    messages.append({"title": str(title), "text": str(text)})
    return QMessageBox.StandardButton.Ok


QMessageBox.critical = QMessageBox.warning = QMessageBox.information = message


def check():
    result = {"scope": "Post-uninstall independent QGIS/plugin import and small output-reader probe",
              "plugin": PLUGIN, "pid": os.getpid()}
    try:
        config_file = "qgis344_runtime.json" if PLUGIN == "QSWATPlus" else "qswatmod344_runtime.json"
        cfg = json.loads((ROOT / "config" / config_file).read_text(encoding="utf-8"))
        profile = (ROOT / cfg["profile_root"] / "profiles" / cfg["profile_name"]).resolve()
        actual = Path(QgsApplication.qgisSettingsDirPath()).resolve()
        if actual != profile:
            raise RuntimeError(f"Unexpected active profile: {actual}")
        extension = Path(qgis._core.__file__).resolve()
        if not extension.is_relative_to(Path(cfg["qgis_root"]).resolve()):
            raise RuntimeError(f"Old QGIS binding loaded: {extension}")
        if not Qgis.QGIS_VERSION.startswith("3.44."):
            raise RuntimeError("Unexpected QGIS version")
        if any("/qgis3/profiles/default/" in str(p).replace("\\", "/").lower()
               for p in qgis.utils.plugin_paths):
            raise RuntimeError("Old default profile is in plugin search paths")
        if PLUGIN not in qgis.utils.active_plugins:
            if not qgis.utils.loadPlugin(PLUGIN) or not qgis.utils.startPlugin(PLUGIN):
                raise RuntimeError("Plugin did not load/start")
        providers = QgsProviderRegistry.instance().providerList()
        if not {"gdal", "ogr"}.issubset(providers) or not QgsCoordinateReferenceSystem("EPSG:4326").isValid():
            raise RuntimeError("GDAL/OGR/CRS unavailable")
        plugin_root = (ROOT / cfg["plugin_root"]).resolve()
        module_name = ("QSWATPlus.QSWATPlus.QSWATPlusMain"
                       if PLUGIN == "QSWATPlus" else "QSWATMOD2.QSWATMOD2")
        main = importlib.import_module(module_name)
        if not Path(main.__file__).resolve().is_relative_to(plugin_root):
            raise RuntimeError("Plugin loaded outside its new profile")
        result.update(qgis_version=Qgis.QGIS_VERSION, python_version=sys.version,
                      profile=str(actual), binding=str(extension), plugin_module=main.__file__,
                      active_plugins=list(qgis.utils.active_plugins), providers=providers)
        if PLUGIN == "QSWATPlus":
            sys.path.insert(0, str(ROOT / "scripts"))
            from check_qgis344_support import check_support
            support = check_support()
            version = main.QSWATPlus.__version__
            for name in ("dataInC", "jenks", "polygonizeInC", "polygonizeInC2"):
                importlib.import_module("QSWATPlus.QSWATPlus." + name)
            result.update(plugin_version=version, compiled_extensions_loaded=True,
                          support_actual=str(support["support_entry"].resolve()))
        else:
            metadata = configparser.ConfigParser()
            metadata.read(plugin_root / "metadata.txt", encoding="utf-8-sig")
            version = metadata.get("general", "version")
            import pandas as pd
            import matplotlib
            from QSWATMOD2.modules import flopy
            from QSWATMOD2.pyfolder import post_i_sw, post_ii_gw
            case = ROOT / cfg["official_case_run"]
            rivers = post_i_sw.read_output_rch_data(None, str(case))
            cells, heads = post_ii_gw.read_swatmf_out_MF_obs(None, str(case))
            heads = post_ii_gw.update_index(None, heads.copy(), "01/01/1985")
            state = SimpleNamespace(dlg=SimpleNamespace(
                radioButton_day=SimpleNamespace(isChecked=lambda: True),
                radioButton_month=SimpleNamespace(isChecked=lambda: False),
                radioButton_year=SimpleNamespace(isChecked=lambda: False)))
            monthly = post_ii_gw.time_cvt(state, heads.copy(), "Monthly")
            if len(heads) != 365 or len(monthly) != 12:
                raise RuntimeError("Groundwater post-processing row counts differ")
            result.update(plugin_version=version, pandas=pd.__version__,
                          matplotlib=matplotlib.__version__, flopy=flopy.__version__,
                          official_river_rows=len(rivers), official_head_days=len(heads),
                          official_head_months=len(monthly), official_head_cells=len(cells))
        if version != cfg["plugin_version"]:
            raise RuntimeError(f"Plugin version differs: {version}")
        result["passed"] = True
    except Exception:
        result.update(passed=False, error=traceback.format_exc())
    result["dialogs"] = messages
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    QgsApplication.quit()


QTimer.singleShot(2000, check)
