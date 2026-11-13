"""Read-only input checks, using QGIS GDAL for spatial inspection. No source writes."""
import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path


def rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream: return list(csv.DictReader(stream))


def check_paths(spatial, editor, root, simulation=None):
    root = Path(root); raw = root/spatial["raw_data"]
    names = ["dem", "landuse", "soil", "outlet", "landuse_lookup", "soil_lookup", "usersoil"]
    if spatial.get("burn_streams"): names.append("burn_stream_file")
    for key in names:
        if not (raw/spatial[key]).is_file(): raise FileNotFoundError(f"{key}: {raw/spatial[key]}")
    if Path(spatial["outlet"]).suffix.lower() != ".shp": raise ValueError("当前出口入口支持带投影的Shapefile")
    for suffix in (".shp", ".shx", ".dbf", ".prj"):
        if not (raw/spatial["outlet"]).with_suffix(suffix).exists(): raise ValueError(f"出口缺少 {suffix} 配套文件")
    for key in ("stream_threshold_cells", "channel_threshold_cells", "snap_threshold_m"):
        if not math.isfinite(float(spatial[key])) or float(spatial[key]) <= 0: raise ValueError(f"{key}须为正数")
    if spatial["channel_threshold_cells"] > spatial["stream_threshold_cells"]: raise ValueError("河道阈值不能大于河网阈值")
    slopes = spatial["slope_limits_percent"]
    if slopes != sorted(set(slopes)) or any(not math.isfinite(x) or x <= 0 for x in slopes): raise ValueError("坡度分界须为递增正数")
    if spatial.get("hru_minimum_area_ha", 0) < 0: raise ValueError("HRU最小面积不能为负")
    from input_data import lookup_rows,soil_profiles,check_weather
    lookup_rows(raw/spatial["landuse_lookup"],"landuse")
    lookup_rows(raw/spatial["soil_lookup"],"soil")
    profiles=soil_profiles(spatial,root)
    for key,filename in [("plant_csv","plant.csv"),("urban_csv","urban.csv")]:
        selected=spatial.get(key,filename)
        if selected and not (raw/selected).is_file():raise FileNotFoundError(f"{key}: {raw/selected}；可显式留空使用官方项目模板表")
    report=check_weather(editor["weather"],root,simulation or editor["simulation"])
    report.update(spatial_paths_checked=names,soil_profiles=len(profiles))
    return report


def inspect_spatial(cfg, root):
    import numpy as np
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions(); gdal.SetConfigOption("GDAL_PAM_ENABLED","NO")
    raw=Path(root)/cfg["raw_data"]; dem=gdal.Open(str(raw/cfg["dem"]),gdal.GA_ReadOnly)
    if dem.RasterCount!=1: raise ValueError("DEM须为单波段")
    srs=osr.SpatialReference(wkt=dem.GetProjection())
    if not srs.IsProjected() or abs(srs.GetLinearUnits()-1)>1e-9: raise ValueError("请先将DEM整理为适合流域的米制投影坐标系；工作台不自动猜测投影")
    gt=dem.GetGeoTransform()
    if gt[1]<=0 or gt[5]>=0 or gt[2]!=0 or gt[4]!=0: raise ValueError("DEM须为无旋转的北向栅格")
    arr=dem.ReadAsArray(); mask=np.isfinite(arr)
    nd=dem.GetRasterBand(1).GetNoDataValue()
    if nd is not None: mask &= arr!=nd
    if not mask.any(): raise ValueError("DEM没有有效像元")
    bounds=[gt[0],gt[3]+dem.RasterYSize*gt[5],gt[0]+dem.RasterXSize*gt[1],gt[3]]
    info={"dem_crs":srs.GetName(),"resolution_m":[gt[1],-gt[5]],"maps":{}}
    for key,idcol,namecol in [("landuse","LANDUSE_ID","SWAT_CODE"),("soil","SOIL_ID","SNAM")]:
        source=gdal.Open(str(raw/cfg[key]),gdal.GA_ReadOnly)
        if not source.GetProjection() or source.RasterCount!=1: raise ValueError(f"{key}: 缺少投影或不是单波段")
        aligned=gdal.Warp("",source,format="MEM",dstSRS=dem.GetProjection(),outputBounds=bounds,width=dem.RasterXSize,height=dem.RasterYSize,resampleAlg="near",outputType=gdal.GDT_Float64,dstNodata=-2147483648)
        data=aligned.ReadAsArray()[mask]; valid=np.isfinite(data)&(data!=-2147483648)
        values=np.unique(data[valid])
        if not len(values) or any(v!=int(v) for v in values): raise ValueError(f"{key}: 类别必须为整数")
        from input_data import lookup_rows,soil_profiles
        lookup=lookup_rows(raw/cfg[key+"_lookup"],key)
        missing=[int(v) for v in values if int(v) not in lookup or not lookup[int(v)]]
        # Only DEM-rectangle diagnostics here. The watershed is not delineated
        # yet; mappings/profiles outside the actual basin must not reject valid inputs.
        if key=="soil":
            profiles=soil_profiles(cfg,root)
            absent=sorted({lookup[int(v)] for v in values if int(v) in lookup}-profiles)
        info["maps"][key]={"classes":[int(v) for v in values],"missing_percent_in_dem":float(100*(~valid).sum()/len(data)),"resampling":"nearest; project copy only","unmapped_classes_in_dem_rectangle":missing,
                                  "profiles_missing_in_dem_rectangle":absent if key=="soil" else []}
    vector=ogr.Open(str(raw/cfg["outlet"])); layer=vector.GetLayer()
    if not layer.GetSpatialRef() or not layer.GetFeatureCount(): raise ValueError("出口数据缺少坐标系或要素")
    required={"ID","INLET","RES","PTSOURCE"}
    if not required <= {f.GetName() for f in layer.schema}: raise ValueError(f"出口属性须包含 {sorted(required)}")
    info["outlet_count"]=layer.GetFeatureCount()
    info["note"]="DEM范围内覆盖率用于预检；实际流域内部覆盖在划分后复查，未填补缺失分类。"
    return info


if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--spatial",required=True,type=Path);p.add_argument("--editor",required=True,type=Path);p.add_argument("--output",required=True,type=Path);p.add_argument("--root",type=Path)
    a=p.parse_args()
    from input_paths import config_root
    root=a.root.resolve() if a.root else config_root(a.spatial)
    spatial=json.loads(a.spatial.read_text(encoding="utf-8-sig"));editor=json.loads(a.editor.read_text(encoding="utf-8-sig"))
    report=check_paths(spatial,editor,root);report["spatial"]=inspect_spatial(spatial,root)
    a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
