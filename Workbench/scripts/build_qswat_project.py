"""Build spatial SWAT+ inputs with the installed QSWAT+ plugin, in batch mode.

Run using QGIS's Python wrapper. Source rasters and installed files stay unchanged.
QSettings and QGIS's profile are redirected into this workspace for this process.
Stages permit resuming a failed construction without rerunning unrelated work.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", choices=["prepare", "delineate", "finalize", "hrus", "all"], default="all")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    root = args.config.resolve().parent.parent
    project_dir = root / cfg["project_dir"]
    project_file = project_dir / (cfg["project_name"] + ".qgs")
    runtime = root / "config" / "qgis_runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(runtime / "profile")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["TEMP"] = os.environ["TMP"] = str(runtime / "temp")
    (runtime / "temp").mkdir(exist_ok=True)
    sys.path.insert(0, cfg["qswat_plugin_root"])
    sys.path.append(str(Path(cfg["qgis_prefix"]) / "python" / "plugins"))
    from qgis.core import QgsApplication, QgsProject, QgsCoordinateReferenceSystem
    from qgis.PyQt.QtCore import QCoreApplication, QSettings, QFileInfo
    from osgeo import gdal, ogr
    QgsApplication.setPrefixPath(cfg["qgis_prefix"], True)
    QCoreApplication.setOrganizationName("SWATPlusAutomation")
    QCoreApplication.setApplicationName("QSWATBatch")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(runtime))
    settings = QSettings()
    settings.setValue("/QSWATPlus/SWATPlusDir", cfg["swatplus_dir"])
    settings.setValue("/QSWATPlus/NumProcesses", cfg["num_processes"])
    settings.sync()
    app = QgsApplication([], True, str(runtime / "profile"))
    app.initQgis()
    gdal.UseExceptions()
    ogr.UseExceptions()
    from QSWATPlus.QSWATPlusMain import QSWATPlus
    from QSWATPlus.delineation import Delineation
    from QSWATPlus.hrus import HRUs
    from QSWATPlus.QSWATUtils import QSWATUtils, FileTypes
    from QSWATPlus import TauDEMUtils as taudem_module

    # TauDEM 5.3.9 passes Windows ANSI argv into GDAL. Its child process must
    # interpret those names as ANSI; QGIS itself continues using UTF-8 filenames.
    # Also decode the native command's Chinese messages with the Windows ACP.
    def run_taudem_native(command, **kwargs):
        env = os.environ.copy()
        env["GDAL_FILENAME_IS_UTF8"] = "NO"
        env.pop("GDAL_DRIVER_PATH", None)
        kwargs.update(env=env, encoding="mbcs", errors="replace")
        result = subprocess.run(command, **kwargs)
        log_dir = root / cfg["build_log_dir"]
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "taudem_calls.log").open("a", encoding="utf-8") as stream:
            stream.write(str(command) + "\n" + (result.stdout or "") + (result.stderr or "") + "\n")
        return result

    taudem_module.subprocess = SimpleNamespace(run=run_taudem_native, PIPE=subprocess.PIPE)

    class DummyInterface:
        def __getattr__(self, name):
            return lambda *a, **k: self
        def __iter__(self):
            return iter([])
        def layers(self):
            return list(QgsProject.instance().mapLayers().values())

    app.messageLog().messageReceived.connect(lambda message, tag, level: print(f"[{tag}:{level}] {message}", flush=True))
    start = time.perf_counter()
    project_dir.mkdir(parents=True, exist_ok=True)
    proj = QgsProject.instance()
    if project_file.exists():
        if not proj.read(str(project_file)):
            raise RuntimeError("Cannot read existing QGIS project")
    else:
        proj.setFileName(str(project_file))
        proj.setTitle(cfg["project_name"])
        dem_source = gdal.Open(str(root/cfg["raw_data"]/cfg["dem"]))
        proj.setCrs(QgsCoordinateReferenceSystem.fromWkt(dem_source.GetProjection()))
        dem_source = None
        if not proj.write():
            raise RuntimeError("Cannot create QGIS project")
    plugin = QSWATPlus(DummyInterface())
    if plugin.loadFailed:
        raise RuntimeError("Installed QSWAT+ Cython modules cannot load")
    plugin.setupProject(proj, True)
    gv = plugin._gv
    tree = proj.layerTreeRoot()
    raw = root / cfg["raw_data"]
    dem = Path(gv.demDir) / "dem.tif" if hasattr(gv, "demDir") else project_dir / "Watershed/Rasters/DEM/dem.tif"
    landuse = Path(gv.landuseDir) / "landuse.tif"
    soil = Path(gv.soilDir) / "soil.tif"
    outlet = Path(gv.shapesDir) / "MainOutlet.shp"

    if args.stage in ["prepare", "all"]:
        for source, target in [(raw/cfg["dem"],dem), (raw/cfg["landuse"],landuse), (raw/cfg["soil"],soil)]:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                dataset = gdal.Translate(str(target), str(source), format="GTiff", creationOptions=["COMPRESS=LZW"])
                if dataset is None:
                    raise RuntimeError(f"Raster conversion failed: {source}")
                dataset = None
            print("Prepared", target, flush=True)
        if not outlet.exists():
            QSWATUtils.copyFiles(QFileInfo(str(raw/cfg["outlet"])), gv.shapesDir)
        print("Original rasters retained; project copies prepared", flush=True)

    if args.stage in ["delineate", "finalize", "all"]:
        delin = Delineation(gv, plugin._demIsProcessed)
        delin.init()
        delin._dlg.numProcesses.setValue(cfg["num_processes"])
        delin._dlg.selectDem.setText(str(dem))
        dem_layer, _ = QSWATUtils.getLayerByFilename(tree.findLayers(), str(dem), FileTypes._DEM, gv, None, QSWATUtils._WATERSHED_GROUP_NAME)
        if dem_layer is None or not delin.setDefaultNumCells(dem_layer):
            raise RuntimeError("DEM failed QSWAT initialization")
        delin._dlg.numCellsSt.setText(str(cfg["stream_threshold_cells"]))
        delin._dlg.numCellsCh.setText(str(cfg["channel_threshold_cells"]))
        delin._dlg.snapThreshold.setText(str(cfg["snap_threshold_m"]))
        delin._dlg.selectOutlets.setText(str(outlet))
        delin._dlg.useOutlets.setChecked(True)
        delin._dlg.checkBurn.setChecked(cfg["burn_streams"])
        if cfg["burn_streams"]:
            burn = Path(gv.shapesDir) / Path(cfg["burn_stream_file"]).name
            if not burn.exists():
                QSWATUtils.copyFiles(QFileInfo(str(raw/cfg["burn_stream_file"])), gv.shapesDir)
            delin._dlg.selectBurn.setText(str(burn))
            gv.burnFile = str(burn)
        if args.stage != "finalize":
            print("Starting delineation", flush=True)
            delin.runTauDEM2()
        print("Snapping:", delin._dlg.snappedLabel.text(), flush=True)
        delin.finishDelineation()
        proj.write()
        if not delin.delineationFinishedOK or not delin.finishHasRun:
            raise RuntimeError("Delineation did not complete; inspect log")
        # Delineation.run() normally saves this only after the modal GUI exits.
        # Direct batch calls must perform the same step after successful finish.
        gv.writeProjectConfig(1, 0)
        print("Delineation completed", flush=True)
        delin._dlg.close()

    if args.stage in ["hrus", "all"]:
        if not gv.isDelinDone():
            raise RuntimeError("Delineation must finish before HRUs")
        hrus = HRUs(gv, plugin._odlg.reportsBox)
        hrus.init()
        dialog = hrus._dlg
        hrus.landuseFile, hrus.soilFile = str(landuse), str(soil)
        hrus.landuseLayer, _ = QSWATUtils.getLayerByFilename(tree.findLayers(), str(landuse), FileTypes._LANDUSES, gv, None, QSWATUtils._LANDUSE_GROUP_NAME)
        hrus.soilLayer, _ = QSWATUtils.getLayerByFilename(tree.findLayers(), str(soil), FileTypes._SOILS, gv, None, QSWATUtils._SOIL_GROUP_NAME)
        db = gv.db
        # All plant, soil and lookup tables are imported into this project's DB.
        db.plantSoilDatabase = db.dbFile
        db.plantSoilDatabaseSelected = True
        dialog.plantSoilDatabase.setText(db.dbFile)
        names = [("robit_landuse", "landuse", cfg["landuse_lookup"]),
                 ("robit_soil", "soil", cfg["soil_lookup"]),
                 ("robit_usersoil", "usersoil", cfg["usersoil"]),
                 ("plant", "plant", "plant.csv"), ("urban", "urban", "urban.csv")]
        for table, typ, filename in names:
            if not db.importCsv(table, typ, str(raw/filename)):
                raise RuntimeError(f"Failed to import {typ} table")
        hrus.setLanduseData()
        dialog.usersoilButton.setChecked(True)
        hrus.setSoilData()
        for widget, table in [(dialog.selectLanduseTable,"robit_landuse"), (dialog.selectSoilTable,"robit_soil"), (dialog.selectUsersoilTable,"robit_usersoil")]:
            if widget.findText(table) < 0:
                widget.addItem(table)
            widget.setCurrentIndex(widget.findText(table))
        hrus.landuseTable, hrus.soilTable = "robit_landuse", "robit_soil"
        db.usersoilTable, db.plantTable, db.urbanTable = "robit_usersoil", "plant", "urban"
        db.slopeLimits = cfg["slope_limits_percent"]
        dialog.slopeBrowser.setText(QSWATUtils.slopesToString(db.slopeLimits))
        dialog.channelMergeVal.setText(str(cfg["channel_merge_percent"]))
        dialog.generateFullHRUs.setChecked(True)
        dialog.gwflowButton.setChecked(False)
        print("Reading spatial layers for HRUs", flush=True)
        if not hrus.readFiles():
            raise RuntimeError("QSWAT failed to read HRU source maps")
        dialog.filterAreaButton.setChecked(True)
        dialog.areaButton.setChecked(True)
        dialog.areaVal.setText(str(cfg["hru_minimum_area_ha"]))
        hrus.calcHRUs()
        if not hrus.completed or not hrus.HRUsAreCreated():
            raise RuntimeError("QSWAT did not complete HRU creation")
        proj.write()
        print("HRUs completed", flush=True)
    with sqlite3.connect(gv.db.dbFile) as connection:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'gis_%'")]
        counts = {t: connection.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0] for t in tables}
    print(json.dumps({"project":str(project_file),"database":gv.db.dbFile,"stage":args.stage,"gis_rows":counts,"elapsed_seconds":round(time.perf_counter()-start,3)}, ensure_ascii=False,indent=2), flush=True)
    plugin.finish()
    proj.clear()
    app.exitQgis()


if __name__ == "__main__":
    main()
