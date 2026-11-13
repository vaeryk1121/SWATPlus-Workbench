"""Check a QSWAT+ watershed against its original maps without modifying the project.

Run with the QGIS Python wrapper. Paths and field names may be overridden in the
build config's optional spatial_check object. Coverage is counted at DEM cell
centres inside the positively numbered subbasin polygons, not the DEM rectangle.
"""
import argparse
import csv
import json
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def resolved(base, value):
    path = Path(value)
    return path if path.is_absolute() else base / path


def spatial_reference(wkt):
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def geometry_records(path, target_srs, id_field=None, positive_field=None):
    dataset = ogr.Open(str(path), 0)
    if dataset is None:
        raise RuntimeError(f"Cannot read vector: {path}")
    layer = dataset.GetLayer()
    source_srs = layer.GetSpatialRef().Clone()
    source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(source_srs, target_srs)
    fields = [item.GetName() for item in layer.schema]
    if positive_field is not None:
        if positive_field not in fields:
            raise ValueError(f"Missing positive basin field {positive_field}: {fields}")
        layer.SetAttributeFilter(f'"{positive_field}" > 0')
    records = []
    for feature in layer:
        geometry = feature.GetGeometryRef().Clone()
        geometry.Transform(transform)
        records.append({"id": feature.GetField(id_field) if id_field else feature.GetFID(),
                        "fields": feature.items(), "geometry": geometry})
    return records, fields, source_srs.GetName()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    from input_paths import config_root
    root = config_root(args.config)
    project = resolved(root, cfg["project_dir"])
    raw = resolved(root, cfg["raw_data"])
    check = cfg.get("spatial_check", {})
    gdal.UseExceptions()
    ogr.UseExceptions()
    # Read-only source inspection must not create GDAL sidecar files.
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")

    dem_path = resolved(project, check.get("dem", "Watershed/Rasters/DEM/dem.tif"))
    basins_path = resolved(project, check.get("subbasins", "Watershed/Shapes/demsubbasins.shp"))
    basin_field = check.get("positive_basin_field", "Subbasin")
    dem = gdal.Open(str(dem_path), gdal.GA_ReadOnly)
    gt = dem.GetGeoTransform()
    if gt[2] != 0 or gt[4] != 0 or gt[1] <= 0 or gt[5] >= 0:
        raise ValueError("This check requires the north-up DEM used by this build.")
    width, height = dem.RasterXSize, dem.RasterYSize
    target_srs = spatial_reference(dem.GetProjection())
    if not target_srs.IsProjected():
        raise ValueError("DEM must be projected to calculate watershed area in metres.")
    metre_factor = target_srs.GetLinearUnits()
    cell_area = abs(gt[1] * gt[5]) * metre_factor ** 2
    bounds = [gt[0], gt[3] + height * gt[5], gt[0] + width * gt[1], gt[3]]

    basins, fields, crs_name = geometry_records(
        basins_path, target_srs, basin_field, basin_field)
    if not basins:
        raise ValueError("No positively numbered subbasins were found.")
    mem_vector = ogr.GetDriverByName("Memory").CreateDataSource("basin_mask")
    mask_layer = mem_vector.CreateLayer("basins", target_srs, ogr.wkbMultiPolygon)
    for record in basins:
        feature = ogr.Feature(mask_layer.GetLayerDefn())
        feature.SetGeometry(record["geometry"])
        mask_layer.CreateFeature(feature)
    mask_ds = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
    mask_ds.SetGeoTransform(gt)
    mask_ds.SetProjection(dem.GetProjection())
    mask_ds.GetRasterBand(1).Fill(0)
    gdal.RasterizeLayer(mask_ds, [1], mask_layer, burn_values=[1])
    mask = mask_ds.ReadAsArray() == 1
    cell_count = int(mask.sum())
    polygon_area = sum(record["geometry"].GetArea() for record in basins) * metre_factor ** 2
    report = {
        "config": str(args.config.resolve()),
        "coverage_method": "Nearest-neighbour sampling of original source maps at DEM cell centres within Subbasin > 0 polygons; no raster edits or gap filling.",
        "watershed": {
            "subbasins_file": str(basins_path), "selection": f"{basin_field} > 0",
            "fields": fields, "crs": crs_name,
            "subbasin_count": len(basins), "subbasin_ids": sorted(record["id"] for record in basins),
            "polygon_area_m2": polygon_area, "polygon_area_km2": polygon_area / 1e6,
            "dem_cell_area_m2": cell_area, "within_watershed_cells": cell_count,
            "mask_area_m2": cell_count * cell_area,
            "mask_minus_polygon_area_m2": cell_count * cell_area - polygon_area,
        },
        "network_layers": {}, "coverage": {},
    }
    for name, default in [("stream", "Watershed/Shapes/demstream/demstream.shp"),
                          ("channel", "Watershed/Shapes/demchannel/demchannel.shp")]:
        network_path = resolved(project, check.get(name, default))
        records, network_fields, _ = geometry_records(network_path, target_srs, "LINKNO")
        terminal = [r for r in records if r["fields"].get("DSLINKNO") == -1]
        report["network_layers"][name] = {
            "file": str(network_path), "feature_count": len(records),
            "fields": network_fields,
            "terminal_links": [{"link_id": r["id"],
                                "downstream_contributing_area_m2": r["fields"].get("DSContArea")}
                               for r in terminal],
        }

    # Match original and snapped points by the source ID, not feature order.
    original_path = resolved(raw, cfg["outlet"])
    snap_path = resolved(project, check.get("snapped_outlet", "Watershed/Shapes/MainOutlet_snap.shp"))
    point_id = check.get("outlet_id_field", "ID")
    originals, _, _ = geometry_records(original_path, target_srs, point_id)
    snapped, _, _ = geometry_records(snap_path, target_srs, point_id)
    original_by_id = {r["id"]: r for r in originals}
    if len(original_by_id) != len(originals):
        raise ValueError("Outlet ID values must be unique to compare movement.")
    moves = []
    for record in snapped:
        if record["id"] not in original_by_id:
            raise ValueError(f"Snapped outlet {record['id']} has no original ID match.")
        original = original_by_id[record["id"]]["geometry"]
        point = record["geometry"]
        moves.append({"id": record["id"],
                      "original_xy": [original.GetX(), original.GetY()],
                      "snapped_xy": [point.GetX(), point.GetY()],
                      "distance_m": original.Distance(point) * metre_factor})
    report["outlets"] = {
        "original_file": str(original_path), "snapped_file": str(snap_path),
        "original_count": len(originals), "snapped_count": len(snapped),
        "snap_threshold_m": cfg.get("snap_threshold_m"), "movements": moves,
        "unmatched_original_ids": sorted(set(original_by_id) - {r["id"] for r in snapped}),
    }

    from input_data import soil_profiles,lookup_rows
    soil_names=soil_profiles(cfg,root)
    for name in ["landuse", "soil"]:
        raster_path = resolved(raw, cfg[name])
        source = gdal.Open(str(raster_path), gdal.GA_ReadOnly)
        target_nodata = -2147483648
        resampled = gdal.Warp("", source, format="MEM", dstSRS=dem.GetProjection(),
                              outputBounds=bounds, width=width, height=height,
                              resampleAlg=gdal.GRA_NearestNeighbour,
                              outputType=gdal.GDT_Int32, dstNodata=target_nodata)
        pixels = resampled.ReadAsArray()[mask]
        valid = pixels != target_nodata
        values, counts = np.unique(pixels[valid], return_counts=True)
        lookup_path = resolved(raw, cfg[f"{name}_lookup"])
        rows = csv_rows(lookup_path)
        default_id, default_name = (("LANDUSE_ID", "SWAT_CODE") if name == "landuse" else ("SOIL_ID", "SNAM"))
        id_col = check.get(f"{name}_lookup_id_column", default_id)
        label_col = check.get(f"{name}_lookup_name_column", default_name)
        lookup = lookup_rows(lookup_path,name) if not check.get(f"{name}_lookup_id_column") else {int(row[id_col]):row[label_col].strip() for row in rows}
        classes = [{"value": int(value), "mapped_name": lookup.get(int(value)),
                    "cells": int(count), "area_ha": float(count * cell_area / 10000),
                    "percent_of_watershed_cells": float(100 * count / cell_count)}
                   for value, count in zip(values, counts)]
        missing_mapping = [int(value) for value in values if int(value) not in lookup]
        missing_soil = sorted({lookup[int(value)] for value in values if int(value) in lookup} - soil_names) if name == "soil" else []
        report["coverage"][name] = {
            "source_file": str(raster_path), "source_nodata": source.GetRasterBand(1).GetNoDataValue(),
            "source_pixel_size": [source.GetGeoTransform()[1], abs(source.GetGeoTransform()[5])],
            "lookup_file": str(lookup_path),
            "valid_cells": int(valid.sum()), "nodata_or_outside_source_cells": int((~valid).sum()),
            "nodata_or_outside_source_percent": float(100 * (~valid).sum() / cell_count),
            "classes": classes, "unmapped_values": missing_mapping,
            "soil_names_missing_properties": missing_soil,
        }
    report["soil_properties"] = {"source": str(resolved(raw, cfg["usersoil"])),
                                  "profile_count": len(soil_names), "unique_profile_names": len(soil_names)}
    report["checks"] = {
        "cell_mask_matches_polygon_area": abs(cell_count * cell_area - polygon_area) < cell_area * 0.01,
        "all_outlets_matched": len(originals) == len(snapped) and not report["outlets"]["unmatched_original_ids"],
        "all_snapped_within_threshold": all(move["distance_m"] <= cfg["snap_threshold_m"] for move in moves),
        "maps_cover_all_watershed_cell_centres": all(item["nodata_or_outside_source_cells"] == 0 for item in report["coverage"].values()),
        "all_observed_map_classes_have_lookup_entries": all(not item["unmapped_values"] for item in report["coverage"].values()),
        "all_used_soils_have_properties": not report["coverage"]["soil"]["soil_names_missing_properties"],
    }
    output = args.output or resolved(root, cfg["build_log_dir"]) / "spatial_check.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output.resolve()), "watershed_km2": polygon_area / 1e6,
                      "outlet_movement_m": [move["distance_m"] for move in moves],
                      "checks": report["checks"]}, ensure_ascii=False, indent=2))
    failed = [name for name, passed in report["checks"].items() if not passed]
    if failed:
        raise ValueError(f"Spatial data checks failed: {failed}; see {output}")


if __name__ == "__main__":
    main()
