"""Run with the isolated QGIS launcher's --probe option; never run a model."""

import json
import os
import sys
import traceback
from pathlib import Path

import qgis._core
import qgis.utils
from qgis.core import Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsProviderRegistry
from qgis.PyQt.QtCore import QTimer


def check_startup():
    destination = Path(os.environ["SWATPLUS_QGIS344_CHECK"])
    expected_profile = Path(os.environ["SWATPLUS_QGIS344_EXPECTED_PROFILE"]).resolve()
    expected_install = Path(os.environ["QGIS344_HOME"]).resolve()
    result = {"pid": os.getpid(), "scope": "Isolated QGIS startup only; no QSWAT+ or model run."}
    try:
        profile = Path(QgsApplication.qgisSettingsDirPath()).resolve()
        extension = Path(qgis._core.__file__).resolve()
        plugin_paths = [str(p) for p in qgis.utils.plugin_paths]
        active_plugins = list(qgis.utils.active_plugins)
        providers = QgsProviderRegistry.instance().providerList()
        checks = {
            "qgis_344": Qgis.QGIS_VERSION.startswith("3.44."),
            "separate_profile": profile == expected_profile,
            "new_install_bindings": extension.is_relative_to(expected_install),
            "no_default_profile_search": not any(
                "/qgis3/profiles/default/" in str(p).replace("\\", "/").lower()
                for p in plugin_paths
            ),
            "no_legacy_plugins_loaded": not (
                {"ee_plugin", "QSWATMOD2", "QSWATPlus3_9", "MapSWAT_v3"} & set(active_plugins)
            ),
            "gdal_and_ogr_providers": {"gdal", "ogr"}.issubset(providers),
            "crs_database_usable": QgsCoordinateReferenceSystem("EPSG:4326").isValid(),
        }
        result.update(
            qgis_version=Qgis.QGIS_VERSION,
            python_version=sys.version,
            executable=sys.executable,
            qgis_python_extension=str(extension),
            qgis_prefix=QgsApplication.prefixPath(),
            profile=str(profile),
            plugin_search_paths=plugin_paths,
            active_plugins=active_plugins,
            providers=providers,
            checks=checks,
            passed=all(checks.values()),
        )
    except Exception:
        result.update(passed=False, error=traceback.format_exc())
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # This is a separate test process, not the user's already-open QGIS window.
    QgsApplication.quit()


QTimer.singleShot(2500, check_startup)
