"""Read-only inspection of the installed Robit teaching data copied into data/raw.

Run with QGIS's Python launcher; prints a small JSON report to stdout.
No delineation, raster modification, or model simulation is performed.
"""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr


parser = argparse.ArgumentParser()
parser.add_argument("data_dir", type=Path)
args = parser.parse_args()
root = args.data_dir.resolve()
gdal.UseExceptions()
rasters = {
    "dem": "DEM/srtm_30m/hdr.adf",
    "landuse": "Landuse/roblandusenew/hdr.adf",
    "soil": "Soil/mowr_soil90/hdr.adf",
}
report = {"data_directory": str(root), "rasters": {}}
datasets = {}
for name, relative in rasters.items():
    dataset = gdal.Open(str(root / relative), gdal.GA_ReadOnly)
    datasets[name] = dataset
    gt = dataset.GetGeoTransform()
    srs = osr.SpatialReference(wkt=dataset.GetProjection())
    srs.AutoIdentifyEPSG()
    report["rasters"][name] = {
        "file": relative,
        "driver": dataset.GetDriver().ShortName,
        "size_columns_rows": [dataset.RasterXSize, dataset.RasterYSize],
        "pixel_size_m": [gt[1], abs(gt[5])],
        "epsg": srs.GetAuthorityCode(None),
        "bounds": [gt[0], gt[3] + dataset.RasterYSize * gt[5],
                   gt[0] + dataset.RasterXSize * gt[1], gt[3]],
        "nodata": dataset.GetRasterBand(1).GetNoDataValue(),
    }
    if name != "soil":
        array = dataset.ReadAsArray()
        valid = array[array != dataset.GetRasterBand(1).GetNoDataValue()]
        report["rasters"][name]["valid_min_max"] = [float(valid.min()), float(valid.max())]
        if name == "landuse":
            report["rasters"][name]["valid_classes"] = np.unique(valid).tolist()

# The soil map is regional. Inspect just the DEM rectangle with nearest-neighbor
# resampling to avoid loading the full regional raster into memory.
dem = report["rasters"]["dem"]
soil_clip = gdal.Warp("", datasets["soil"], format="MEM", outputBounds=dem["bounds"],
                     width=dem["size_columns_rows"][0], height=dem["size_columns_rows"][1],
                     resampleAlg="near")
array = soil_clip.ReadAsArray()
nodata = soil_clip.GetRasterBand(1).GetNoDataValue()
report["soil_classes_in_dem_rectangle"] = np.unique(array[array != nodata]).tolist()

outlet = ogr.Open(str(root / "MainOutlet/MainOutlet.shp"), 0)
layer = outlet.GetLayer(0)
report["outlet"] = {"count": layer.GetFeatureCount(), "features": []}
for feature in layer:
    geom = feature.GetGeometryRef()
    report["outlet"]["features"].append({
        "attributes": dict(feature.items()), "geometry_wkt": geom.ExportToWkt()
    })

with (root / "Robit_usersoil_main.csv").open(newline="") as stream:
    main_soils = list(csv.DictReader(stream))
with (root / "Robit_usersoil_layers.csv").open(newline="") as stream:
    layers = list(csv.DictReader(stream))
report["soil_profiles"] = [{"name": row["NAME"], "id": row["ID"],
                            "layers": sum(layer["soil_id"] == row["ID"] for layer in layers)}
                           for row in main_soils]
with (root / "Robit_usersoil.csv").open(newline="") as stream:
    wide_soils = list(csv.DictReader(stream))
with (root / "Robit_soils.csv").open(newline="") as stream:
    soil_lookup = {int(row["SOIL_ID"]): row["SNAM"] for row in csv.DictReader(stream)}
wide_names = {row["SNAM"] for row in wide_soils}
report["wide_soil_table"] = {
    "profile_count": len(wide_soils), "names": sorted(wide_names),
    "dem_rectangle_class_mapping": [
        {"class": code, "name": soil_lookup[code], "profile_available": soil_lookup[code] in wide_names}
        for code in report["soil_classes_in_dem_rectangle"]
    ],
}

report["weather"] = {}
for name in ("robpcp", "robtmp", "robrhum", "robsol", "robwind"):
    lines = (root / "ClimateRobit" / (name + ".txt")).read_text().splitlines()
    first = dt.datetime.strptime(lines[0], "%Y%m%d").date()
    values = np.array([[float(token) for token in line.split(",")] for line in lines[1:]])
    report["weather"][name] = {
        "start": str(first), "end": str(first + dt.timedelta(days=len(values) - 1)),
        "daily_rows": len(values), "columns": int(values.shape[1]),
        "nonfinite_values": int((~np.isfinite(values)).sum()),
        "minus99_or_minus999_values": int(((values == -99) | (values == -999)).sum()),
        "min_each_column": values.min(axis=0).tolist(),
        "max_each_column": values.max(axis=0).tolist(),
    }
with (root / "Observed/observedFlow.csv").open(newline="") as stream:
    observation = list(csv.DictReader(stream))
report["observation_file"] = {"rows": len(observation), "first": observation[0],
                              "last": observation[-1], "units_in_header": False}
print(json.dumps(report, indent=2, ensure_ascii=False))
