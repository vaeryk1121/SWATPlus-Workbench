"""Build a new Robit spatial project with isolated Windows QGIS 3.44/QSWAT+ 4.0.4.

Run with the new QGIS python-qgis-ltr.bat wrapper. No Editor, SWAT+ engine,
calibration or legacy-profile changes are performed. Runtime installation paths
come from config/qgis344_runtime.json so a later support-directory move requires
configuration changes, not edits to this script or to official plugin code.
"""
import argparse
import ctypes
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", choices=["prepare", "delineate", "finalize", "hrus", "all"], default="all")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    from input_paths import config_root
    root = config_root(args.config,cfg)
    runtime_cfg_path = root / cfg["runtime_config"]
    paths = json.loads(runtime_cfg_path.read_text(encoding="utf-8-sig"))
    qgis_root = Path(paths.get("qgis_root", cfg["qgis_root"]))
    qgis_prefix = qgis_root / "apps" / "qgis-ltr"
    # Runtime-relative plugin files belong to that runtime's root, not to the
    # external case which merely references the installation configuration.
    runtime_base=runtime_cfg_path.resolve().parent.parent
    plugin_root = runtime_base / paths.get("plugin_root", cfg["qswat_plugin_root"])
    support_entry = Path(paths["support_entry"])
    support_root = Path(paths["support_root"])
    if not support_entry.is_dir() or support_entry.resolve() != support_root.resolve():
        raise RuntimeError("Configured support entry is missing or points at a different directory")
    for required in ("Databases/QSWATPlusProj.sqlite", "Databases/swatplus_datasets.sqlite",
                     "TauDEM539Bin/PitRemove.exe", "TauDEM539Bin/StreamNet.exe"):
        if not (support_root / required).is_file():
            raise FileNotFoundError(support_root / required)
    if not (plugin_root / "QSWATPlus" / "QSWATPlusMain.py").is_file():
        raise FileNotFoundError(f"Official plugin implementation not found under {plugin_root}")
    raw = root / cfg["raw_data"]
    if not raw.is_dir():
        raise FileNotFoundError(raw)
    project_dir = root / cfg["project_dir"]
    project_file = project_dir / (cfg["project_name"] + ".qgs")
    log_dir = root / cfg["build_log_dir"]
    log_dir.mkdir(parents=True, exist_ok=True)
    # Keep settings/temp in this case, separate from interactive QGIS and the
    # installed support tree. A configurable ASCII runtime is needed on Windows.
    runtime = root / cfg.get('qgis_runtime_dir', 'runs/qgis_runtime/' + cfg['project_name'])
    from portable_runtime import qgis_runtime_directory
    runtime = qgis_runtime_directory(runtime)
    (runtime / "temp").mkdir(parents=True, exist_ok=True)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(runtime / "profile")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["TEMP"] = os.environ["TMP"] = str(runtime / "temp")
    # Reading the supplied ArcInfo rasters must not create auxiliary sidecars.
    os.environ["GDAL_PAM_ENABLED"] = "NO"
    sys.path.insert(0, str(plugin_root))
    sys.path.append(str(qgis_prefix / "python" / "plugins"))
    from qgis.core import Qgis, QgsApplication, QgsProject, QgsCoordinateReferenceSystem
    import qgis.core
    import qgis._core
    from qgis.PyQt.QtCore import QCoreApplication, QSettings, QFileInfo
    from osgeo import gdal, ogr
    QgsApplication.setPrefixPath(str(qgis_prefix), True)
    QCoreApplication.setOrganizationName("SWATPlusAutomation344")
    QCoreApplication.setApplicationName("QSWAT344Spatial")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(runtime / "settings"))
    QSettings.setPath(QSettings.IniFormat, QSettings.SystemScope, str(runtime / "system-settings"))
    app = QgsApplication([], True, str(runtime / "profile"))
    app.initQgis()
    # QgsApplication can change application identity; configure the instance
    # that QSWAT+ will actually read after QGIS initialization.
    settings = QSettings()
    if not Path(settings.fileName()).resolve().is_relative_to(runtime.resolve()):
        raise RuntimeError(f"QSettings escaped isolated build directory: {settings.fileName()}")
    settings.setValue("/QSWATPlus/SWATPlusDir", str(support_entry))
    settings.setValue("/QSWATPlus/NumProcesses", cfg["num_processes"])
    if paths.get("mpi_dir"):
        settings.setValue("/QSWATPlus/mpiexecDir", paths["mpi_dir"])
    settings.sync()
    gdal.UseExceptions()
    ogr.UseExceptions()
    package = "QSWATPlus"
    main_module = importlib.import_module(package + ".QSWATPlusMain")
    QSWATPlus = main_module.QSWATPlus
    Delineation = importlib.import_module(package + ".delineation").Delineation
    HRUs = importlib.import_module(package + ".hrus").HRUs
    utils_module = importlib.import_module(package + ".QSWATUtils")
    QSWATUtils, FileTypes = utils_module.QSWATUtils, utils_module.FileTypes
    taudem_module = importlib.import_module(package + ".TauDEMUtils")
    loaded_core_dll = ""
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        kernel32.GetModuleFileNameW.restype = ctypes.c_uint
        handle = kernel32.GetModuleHandleW("qgis_core.dll")
        if handle:
            buffer = ctypes.create_unicode_buffer(32768)
            if kernel32.GetModuleFileNameW(handle, buffer, len(buffer)):
                loaded_core_dll = buffer.value
    runtime_proof = {
        "runtime_config": str(runtime_cfg_path), "qgis_version": Qgis.QGIS_VERSION,
        "python_executable": sys.executable, "python_version": sys.version,
        "qgis_core_module": qgis.core.__file__, "qgis_core_extension": qgis._core.__file__,
        "qgis_prefix": QgsApplication.prefixPath(), "loaded_qgis_core_dll": loaded_core_dll,
        "qgis_plugin_path": QgsApplication.pluginPath(), "qswat_module": main_module.__file__,
        "qgis_profile": QgsApplication.qgisSettingsDirPath(), "settings_file": settings.fileName(),
    }
    (log_dir / "runtime_proof.json").write_text(
        json.dumps(runtime_proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("ACTUAL GIS RUNTIME: " + json.dumps(runtime_proof, ensure_ascii=False), flush=True)
    if not Qgis.QGIS_VERSION.startswith(cfg["expected_qgis_version_prefix"]):
        raise RuntimeError("Unexpected QGIS version")
    if not Path(qgis._core.__file__).resolve().is_relative_to(qgis_root.resolve()):
        raise RuntimeError("QGIS bindings loaded from another installation")
    if not Path(sys.executable).resolve().is_relative_to(qgis_root.resolve()):
        raise RuntimeError("Python is not from the selected QGIS installation")
    if loaded_core_dll and not Path(loaded_core_dll).resolve().is_relative_to(qgis_root.resolve()):
        raise RuntimeError("QGIS DLL loaded from another installation")
    if not Path(main_module.__file__).resolve().is_relative_to(plugin_root.resolve()):
        raise RuntimeError("QSWAT+ loaded from another profile")
    if not Path(QgsApplication.qgisSettingsDirPath()).resolve().is_relative_to(runtime.resolve()):
        raise RuntimeError("QGIS is using another profile")

    # TauDEM 5.3.9 uses Windows ANSI argv with its bundled GDAL. QGIS itself
    # retains UTF-8 paths; only the child process changes GDAL's interpretation.
    def run_taudem_native(command, **kwargs):
        env = kwargs.pop("env", os.environ.copy())
        env["GDAL_FILENAME_IS_UTF8"] = "NO"
        env.pop("GDAL_DRIVER_PATH", None)
        kwargs.update(env=env, encoding="mbcs", errors="replace")
        result = subprocess.run(command, **kwargs)
        with (log_dir / "taudem_calls.log").open("a", encoding="utf-8") as stream:
            stream.write(str(command) + "\n" + (result.stdout or "") + (result.stderr or "")
                         + f"\nReturn code: {result.returncode}\n")
        print(f"TauDEM return code: {result.returncode}", flush=True)
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
    plugin.setupProject(proj, True, logFile=str(log_dir / "qswat_messages.log"))
    gv = plugin._gv
    if gv is None:
        raise RuntimeError("QSWAT+ did not initialize the new project")
    if Path(gv.SWATPlusDir).resolve() != support_root.resolve():
        raise RuntimeError(f"QSWAT+ selected unexpected support directory: {gv.SWATPlusDir}")
    if Path(gv.TauDEMDir).resolve() != (support_root / "TauDEM539Bin").resolve():
        raise RuntimeError(f"QSWAT+ selected unexpected TauDEM directory: {gv.TauDEMDir}")
    runtime_proof.update(qswat_version=plugin.__version__,
                         support_entry=str(support_entry), support_root=str(support_root),
                         actual_support_directory=gv.SWATPlusDir,
                         actual_taudem_directory=gv.TauDEMDir,
                         actual_mpiexec=gv.mpiexecPath,
                         project_template=gv.dbProjTemplate,
                         reference_template=gv.dbRefTemplate)
    if plugin.__version__ != cfg["expected_qswat_version"]:
        raise RuntimeError(f"Unexpected QSWAT+ version: {plugin.__version__}")
    (log_dir / "runtime_proof.json").write_text(
        json.dumps(runtime_proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("ACTUAL QSWAT SUPPORT: " + json.dumps(runtime_proof, ensure_ascii=False), flush=True)
    tree = proj.layerTreeRoot()
    dem = Path(gv.demDir) / "dem.tif" if hasattr(gv, "demDir") else project_dir / "Watershed/Rasters/DEM/dem.tif"
    landuse = Path(gv.landuseDir) / "landuse.tif"
    soil = Path(gv.soilDir) / "soil.tif"
    outlet = Path(gv.shapesDir) / "MainOutlet.shp"

    if args.stage in ["prepare", "all"]:
        for source, target in [(raw/cfg["dem"],dem), (raw/cfg["landuse"],landuse), (raw/cfg["soil"],soil)]:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                if cfg.get("align_categorical_to_dem") and target != dem:
                    ref = gdal.Open(str(dem)); gt = ref.GetGeoTransform()
                    bounds = [gt[0],gt[3]+ref.RasterYSize*gt[5],gt[0]+ref.RasterXSize*gt[1],gt[3]]
                    dataset = gdal.Warp(str(target),str(source),format="GTiff",dstSRS=ref.GetProjection(),
                                        outputBounds=bounds,width=ref.RasterXSize,height=ref.RasterYSize,
                                        resampleAlg="near",creationOptions=["COMPRESS=LZW"])
                    ref = None
                else:
                    dataset = gdal.Translate(str(target), str(source), format="GTiff", creationOptions=["COMPRESS=LZW"])
                if dataset is None:
                    raise RuntimeError(f"Raster conversion failed: {source}")
                dataset = None
            print("Prepared", target, flush=True)
        if not outlet.exists():
            dem_ref=gdal.Open(str(dem))
            options={'dstSRS':dem_ref.GetProjection()} if cfg.get('align_categorical_to_dem') else {}
            converted=gdal.VectorTranslate(str(outlet),str(raw/cfg['outlet']),format='ESRI Shapefile',layerName='MainOutlet',**options)
            if converted is None or not outlet.is_file():raise RuntimeError('出口项目副本生成失败')
            converted=None;dem_ref=None
        print("Original rasters retained; project copies prepared", flush=True)

    if args.stage in ["delineate", "finalize", "all"]:
        delin = Delineation(gv, plugin._demIsProcessed)
        delin.init()
        delin._dlg.numProcesses.setValue(cfg["num_processes"])
        delin._dlg.tabWidget.setCurrentIndex(0)
        delin._dlg.useGrid.setChecked(False)
        delin._dlg.selectDem.setText(str(dem))
        dem_layer, _ = QSWATUtils.getLayerByFilename(tree.findLayers(), str(dem), FileTypes._DEM, gv, None, QSWATUtils._WATERSHED_GROUP_NAME)
        if dem_layer is None or not delin.setDefaultNumCells(dem_layer):
            raise RuntimeError("DEM failed QSWAT initialization")
        delin._dlg.numCellsSt.setText(str(cfg["stream_threshold_cells"]))
        delin._dlg.numCellsCh.setText(str(cfg["channel_threshold_cells"]))
        delin._dlg.snapThreshold.setText(str(cfg["snap_threshold_m"]))
        delin._dlg.selectOutlets.setText(str(outlet))
        if args.stage != "finalize":
            delin.btnSetOutlets()
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
        (log_dir / "taudem_output.log").write_text(delin._dlg.taudemOutput.toPlainText(), encoding="utf-8")
        delin.finishDelineation()
        proj.write()
        if not delin.delineationFinishedOK or not delin.finishHasRun:
            raise RuntimeError("Delineation did not complete; inspect log")
        if delin.snapErrors or not Path(gv.snapFile).exists():
            raise RuntimeError("Outlet snapping failed; inspect snapped points")
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
        # Use modern batch-aware loaders, including normal clipping for the large soil map.
        dialog.selectLanduse.setText(str(landuse))
        hrus.getLanduseFile()
        dialog.selectSoil.setText(str(soil))
        hrus.getSoilFile()
        if not hrus.landuseFile or not hrus.soilFile:
            raise RuntimeError("QSWAT+ could not load project HRU rasters")
        db = gv.db
        # All plant, soil and lookup tables are imported into this project's DB.
        db.plantSoilDatabase = db.dbFile
        db.plantSoilDatabaseSelected = True
        dialog.plantSoilDatabase.setText(db.dbFile)
        # Normalize only lookup headers/order in generated project inputs.
        from input_data import normalize_lookups
        normalized=normalize_lookups(cfg,root,project_dir/"input_tables")
        names = [("robit_landuse", "landuse", normalized["landuse_lookup"]),
                 ("robit_soil", "soil", normalized["soil_lookup"]),
                 ("robit_usersoil", "usersoil", str(raw/cfg["usersoil"]))]
        for key,typ in [("plant_csv","plant"),("urban_csv","urban")]:
            selected=cfg.get(key,typ+".csv")
            if selected:names.append((typ,typ,str(raw/selected)))
            elif gv.db.conn.execute(f'SELECT count(*) FROM "{typ}"').fetchone()[0]<1:
                raise ValueError(f"官方项目模板缺少{typ}数据；请明确提供自定义CSV")
        original_reader=db.readCsv
        def configured_layer_reader(typ,tables):
            if typ=="usersoil_layer" and cfg.get("usersoil_format")=="separate":
                return db.importCsv(tables[0]+"_layer",typ,str(raw/cfg["usersoil_layer"]))
            raise ValueError("独立土壤主表必须在配置中指定usersoil_format=separate及分层CSV，批处理中不弹出文件选择框")
        db.readCsv=configured_layer_reader
        try:
            for table,typ,filename in names:
                if not db.importCsv(table,typ,str(raw/filename)):
                    raise RuntimeError(f"Failed to import {typ} table")
        finally:
            db.readCsv=original_reader
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
        dialog.readFromMaps.setChecked(True)
        dialog.floodplainCombo.setCurrentIndex(0)
        dialog.channelPercentButton.setChecked(True)
        dialog.channelMergeSlider.setValue(cfg["channel_merge_percent"])
        dialog.channelMergeVal.setText(str(cfg["channel_merge_percent"]))
        hrus.setChannelMergeChoice()
        dialog.generateFullHRUs.setChecked(True)
        dialog.gwflowButton.setChecked(False)
        print("Reading spatial layers for HRUs", flush=True)
        if not hrus.readFiles():
            raise RuntimeError("QSWAT failed to read HRU source maps")
        dialog.filterAreaButton.setChecked(True)
        dialog.areaButton.setChecked(True)
        hrus.setHRUChoice()
        hrus.setAreaPercentChoice()
        dialog.areaVal.setText(str(cfg["hru_minimum_area_ha"]))
        hrus.readAreaThreshold()
        hrus.calcHRUs()
        if not hrus.completed or not hrus.HRUsAreCreated():
            raise RuntimeError("QSWAT did not complete HRU creation")
        proj.write()
        print("HRUs completed", flush=True)
    gv.db.conn.commit()
    tables = [r[0] for r in gv.db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'gis_%' ORDER BY name")]
    counts = {t: gv.db.conn.execute('SELECT count(*) FROM "' + t + '"').fetchone()[0] for t in tables}
    result = {"project": str(project_file), "database": gv.db.dbFile,
              "datasets_database": gv.db.dbRefFile, "stage": args.stage,
              "qswat_version": plugin.__version__, "gis_rows": counts,
              "elapsed_seconds": round(time.perf_counter() - start, 3),
              "scope": "Spatial build only; no Editor export or SWAT+ simulation"}
    if args.stage in ["hrus", "all"]:
        errors, warnings = gv.db.checkRouting(gv.db.conn, None)
        result["routing_errors"], result["routing_warnings"] = errors, warnings
        result["subbasin_area_ha"] = gv.db.conn.execute("SELECT SUM(area) FROM gis_subbasins").fetchone()[0]
        result["hru_area_ha"] = gv.db.conn.execute("SELECT SUM(arslp) FROM gis_hrus").fetchone()[0]
        result["spatial_complete"] = gv.isDelinDone() and gv.isHRUsDone()
    (log_dir / "spatial_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    plugin.finish()
    proj.clear()
    app.exitQgis()
    if result.get("routing_errors") or result.get("spatial_complete") is False:
        raise RuntimeError("Spatial completion/routing checks failed; see spatial_result.json")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
