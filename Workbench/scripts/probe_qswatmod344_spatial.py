"""QGIS --code probe: synthetic geometry/tables, not a watershed simulation."""
import json
import os
import shutil
import traceback
from pathlib import Path
from types import SimpleNamespace as NS

import qgis.utils
from qgis.core import Qgis, QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QTimer

ROOT = Path(os.environ["SWATPLUS_WORKSPACE"])
CFG = json.loads((ROOT / "config/qswatmod344_runtime.json").read_text(encoding="utf-8"))
PROFILE = (ROOT / CFG["profile_root"] / "profiles" / CFG["profile_name"]).resolve()
OUT = ROOT / "runs/qswatmod344_api_checks"


def run_probe():
    report = {"scope": "Synthetic rectangle and tiny mapping tables; no real watershed or model run.",
              "checks": {}, "qgis": Qgis.QGIS_VERSION}
    created = False
    try:
        assert Path(QgsApplication.qgisSettingsDirPath()).resolve() == PROFILE, "Wrong profile"
        assert Qgis.QGIS_VERSION.startswith("3.44."), "Wrong QGIS version"
        project = QgsProject.instance()
        assert not project.fileName() and not project.mapLayers(), "Use an empty test QGIS process"
        OUT.mkdir(parents=True, exist_ok=False)
        created = True
        from QSWATMOD2.pyfolder import modflow_functions as mf, create_swatmf
        assert Path(mf.__file__).resolve().is_relative_to((ROOT / CFG["plugin_root"]).resolve())
        folders = {key: str(OUT / key) for key in ("SMfolder", "org_shps", "Table", "mapping")}
        for folder in folders.values():
            Path(folder).mkdir()
        source = ROOT / "tools/qswatmod211_review/official_source/data/MiddleBosque1000/mf_1000.dis"
        shutil.copy2(source, Path(folders["SMfolder"]) / source.name)
        rows = [line.split() for line in source.read_text().splitlines() if line and not line.startswith("#")]
        nrow, ncol = int(rows[0][1]), int(rows[0][2])
        dx, dy = float(rows[3][1]), float(rows[2][1])
        assert (nrow, ncol, dx, dy) == (23, 40, 1000.0, 1000.0)
        report["dimensions_source"] = str(source)
        project.layerTreeRoot().addGroup("MODFLOW")
        hru = QgsVectorLayer("Polygon?crs=EPSG:32614", "hru (SWAT)", "memory")
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromWkt(
            "POLYGON((500000 3600000,540000 3600000,540000 3577000,500000 3577000,500000 3600000))"))
        assert hru.dataProvider().addFeatures([feature])[0]
        hru.updateExtents()
        project.addMapLayer(hru)
        messages = []
        dlg = NS(textEdit_sm_link_log=NS(append=messages.append),
                 label_StepStatus=NS(setText=lambda _: None),
                 progressBar_step=NS(setValue=lambda _: None),
                 lineEdit_x_coordinate=NS(text=lambda: "500000"),
                 lineEdit_y_coordinate=NS(text=lambda: "3600000"),
                 spinBox_row=NS(value=lambda: 0), spinBox_col=NS(value=lambda: 0),
                 groupBox_add_grid=NS(isChecked=lambda: False))
        owner = NS(dlg=dlg, dirs_and_paths=lambda: folders, iface=qgis.utils.iface)
        try:
            mf.MF_grid(owner)
            mf.create_grid_id(owner)
            mf.create_row(owner)
            mf.create_col(owner)
            layer = project.mapLayersByName("mf_grid (MODFLOW)")[0]
            values = [(int(f["grid_id"]), int(f["row"]), int(f["col"]), f.geometry().area())
                      for f in layer.getFeatures()]
            assert layer.isValid() and len(values) == 920
            assert sorted(v[0] for v in values) == list(range(1, 921))
            assert {(v[1], v[2]) for v in values} == {(r, c) for r in range(1, 24) for c in range(1, 41)}
            assert all(abs(v[3] - 1e6) < 1e-6 for v in values)
            assert project.write(str(OUT / "synthetic_grid.qgz"))
            report["checks"]["grid"] = {"passed": True, "cells": len(values), "cell_area_m2": 1e6}
        except Exception:
            report["checks"]["grid"] = {"passed": False, "traceback": traceback.format_exc()}
        tables = {
            "hru_dhru": "2\n1\nheader\n1 60 1 1 100\n2 40 1 1 100\n",
            "dhru_grid": "2\n1\nheader\n1 100 1 60 60\n1 100 2 40 40\n",
            "grid_dhru": "2\n2\n1\n2\nheader\n1 100 1 60 60\n1 100 2 40 40\n",
            "river_grid": "1\nheader\n1 1 10\n",
        }
        expected = {
            "swatmf_dhru2hru.txt": [[1, 2], [1, 2, 1], [1, 2], [.6, .4]],
            "swatmf_dhru2grid.txt": [[1, 2], [1, 2], [1, 2], [.6, .4]],
            "swatmf_grid2dhru.txt": [[2, 1], [1, 1], [1], [1], [1], [2, 1], [1], [1], [1]],
            "swatmf_river2grid.txt": [[1], [1, 1, 1], [1], [10]],
        }
        try:
            for name, content in tables.items():
                (Path(folders["Table"]) / name).write_text(content, encoding="ascii")
            paths = create_swatmf.create_map_files(folders["Table"], folders["mapping"])
            for path in paths:
                actual = [[float(v) for v in line.split()] for line in Path(path).read_text().splitlines()]
                wanted = expected[Path(path).name]
                assert len(actual) == len(wanted)
                for got, want in zip(actual, wanted):
                    assert len(got) == len(want) and all(abs(a - b) < 1e-5 for a, b in zip(got, want))
            report["checks"]["mapping"] = {"passed": True, "files": paths, "independent_expected_fractions": [.6, .4]}
        except Exception:
            report["checks"]["mapping"] = {"passed": False, "traceback": traceback.format_exc()}
        report["plugin_log"] = messages
    except Exception:
        report["setup_error"] = traceback.format_exc()
    finally:
        report["passed"] = not report.get("setup_error") and len(report["checks"]) == 2 and all(
            c["passed"] for c in report["checks"].values())
        if created:
            (OUT / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            print(json.dumps(report, ensure_ascii=False))
        QgsApplication.quit()


QTimer.singleShot(1500, run_probe)
