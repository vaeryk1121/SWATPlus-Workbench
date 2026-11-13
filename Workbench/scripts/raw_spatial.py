"""GDAL-only source preparation; no source writes or catchment boundary clipping."""
import math
from pathlib import Path
import re
from raw_tables import write_table


def reference(value):
    from osgeo import osr
    srs = osr.SpatialReference()
    if not value or srs.SetFromUserInput(value) != 0:
        raise ValueError('缺少/无法识别坐标系，不自动猜测或强行定义')
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def suggest_crs(dataset):
    """A recommendation for a compact single-zone basin; caller must confirm."""
    from osgeo import osr
    source=reference(dataset.GetProjection())
    if source.IsProjected() and abs(source.GetLinearUnits()-1)<1e-9:
        return {'suggestion':source.ExportToWkt(),'reason':'源图已使用米制投影；仍须确认该投影适合研究区'}
    geographic=reference('EPSG:4326');transform=osr.CoordinateTransformation(source,geographic)
    gt=dataset.GetGeoTransform()
    points=[transform.TransformPoint(gt[0]+x*gt[1]+y*gt[2],gt[3]+x*gt[4]+y*gt[5])[:2] for x,y in [(0,0),(dataset.RasterXSize,0),(0,dataset.RasterYSize),(dataset.RasterXSize,dataset.RasterYSize)]]
    zones={min(60,int((lon+180)//6)+1) for lon,lat in points}
    if len(zones)!=1 or any(not -180<=lon<=180 or not -80<=lat<=84 for lon,lat in points) or min(p[1] for p in points)*max(p[1] for p in points)<0:
        return {'suggestion':None,'reason':'跨UTM分区/赤道或超出UTM适用纬度；请按流域选择投影，不能自动猜测'}
    epsg=(32600 if sum(p[1] for p in points)>=0 else 32700)+next(iter(zones))
    return {'suggestion':f'EPSG:{epsg}','reason':'四角位于同一UTM分区及半球，仅给出建议，转换前需在来源方案确认'}


def normalize_spatial(spec, output):
    import numpy as np
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions(); ogr.UseExceptions(); gdal.SetConfigOption('GDAL_PAM_ENABLED', 'NO')
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    dem = gdal.Open(str(spec['dem']), gdal.GA_ReadOnly)
    if dem.RasterCount != 1:
        raise ValueError('DEM须为单波段')
    source = reference(dem.GetProjection())
    target = reference(spec['target_crs'])
    if not target.IsProjected() or abs(target.GetLinearUnits()-1) > 1e-9:
        raise ValueError('目标坐标系必须是适合本流域的米制投影')
    resolution = float(spec['resolution_m'])
    if not math.isfinite(resolution) or resolution <= 0:
        raise ValueError('请明确DEM输出分辨率（米）')
    method = spec.get('dem_resampling', 'bilinear')
    if method not in ('near', 'bilinear', 'cubic'):
        raise ValueError('DEM重采样支持near/bilinear/cubic')
    options = {}
    if spec.get('processing_bounds'):
        if len(spec['processing_bounds']) != 4 or not spec.get('processing_bounds_crs') or not spec.get('domain_note'):
            raise ValueError('裁剪须明确处理域边界、CRS及上游/缓冲依据，不能用无缓冲旧流域边界')
        reference(spec['processing_bounds_crs'])
        options.update(outputBounds=spec['processing_bounds'], outputBoundsSRS=spec['processing_bounds_crs'])
    dem_path = output/'dem.tif'
    ds = gdal.Warp(str(dem_path), dem, format='GTiff', dstSRS=target.ExportToWkt(),
                   xRes=resolution, yRes=resolution, resampleAlg=method,
                   dstNodata=-9999, outputType=gdal.GDT_Float32,
                   creationOptions=['COMPRESS=LZW'], **options)
    gt = ds.GetGeoTransform(); width, height = ds.RasterXSize, ds.RasterYSize
    bounds = [gt[0], gt[3]+height*gt[5], gt[0]+width*gt[1], gt[3]]
    valid_dem = ds.GetRasterBand(1).GetMaskBand().ReadAsArray() != 0
    if not valid_dem.any(): raise ValueError('目标域内DEM没有有效像元')
    ds = None
    result = {'dem': str(dem_path), 'resolution_m': resolution, 'target_crs': spec['target_crs'],
              'dem_resampling': method, 'processing_bounds': bounds, 'source_dem': spec['dem'],
              'domain_note': spec.get('domain_note', '保留整个输入DEM域，不按旧流域边界裁剪'), 'maps': {}}
    for kind in ('landuse', 'soil'):
        item = dict(spec[kind])
        if kind=='landuse' and item.get('mapping_profile'):
            import json
            from input_paths import CODE_ROOT
            profiles=json.loads((CODE_ROOT/'config/landuse_sources.json').read_text(encoding='utf-8-sig'))
            saved=profiles[item['mapping_profile']]
            item={**saved,**item}
        raw = gdal.Open(str(item['path']), gdal.GA_ReadOnly)
        reference(raw.GetProjection())
        if raw.RasterCount != 1: raise ValueError(f'{kind}: 分类图须为单波段')
        # Float intermediate catches accidentally continuous classifications.
        warped = gdal.Warp('', raw, format='MEM', dstSRS=target.ExportToWkt(), outputBounds=bounds,
                           width=width, height=height, resampleAlg='near', dstNodata=-9999,
                           outputType=gdal.GDT_Float64)
        values = warped.ReadAsArray(); valid = (warped.GetRasterBand(1).GetMaskBand().ReadAsArray() != 0) & np.isfinite(values)
        if np.any(values[valid] != np.rint(values[valid])):
            raise ValueError(f'{kind}: 分类编码必须为整数')
        categories, counts = np.unique(values[valid & valid_dem].astype(np.int64), return_counts=True)
        if kind=='soil' and item.get('official_database'):
            import sqlite3
            database=Path(item['official_database']).resolve()
            with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as c:
                mapping={int(i):name for i,name in c.execute('SELECT SOIL_ID,SNAM FROM global_soils')}
        else:
            mapping = ({int(k):f'hw2_{int(k)}' for k in categories} if kind=='soil' and item.get('auto_hwsd2_names')
                       else {int(k): str(v).strip() for k, v in item['mapping'].items()})
        if not mapping or any(not v for v in mapping.values()):
            raise ValueError(f'{kind}: 请提供明确分类映射')
        unknown = [int(v) for v in categories if int(v) not in mapping]
        if unknown and item.get('unknown_policy', 'error') != 'nodata':
            raise ValueError(f'{kind}: 未对应类别{unknown}；请补充映射，或显式保留为NoData并在实际流域内复查')
        if item.get('unknown_policy', 'error') not in ('error', 'nodata'):
            raise ValueError('未知类别策略须为error或nodata')
        for code in unknown: valid &= values != code
        values = np.where(valid, values, -9999)
        if np.any((values[valid] < -2147483648) | (values[valid] > 2147483647)):
            raise ValueError('分类编码超出Int32范围')
        path = output/(kind+'.tif')
        target_ds = gdal.GetDriverByName('GTiff').Create(str(path), width, height, 1, gdal.GDT_Int32, options=['COMPRESS=LZW'])
        target_ds.SetProjection(target.ExportToWkt()); target_ds.SetGeoTransform(gt)
        target_ds.GetRasterBand(1).SetNoDataValue(-9999); target_ds.GetRasterBand(1).WriteArray(values.astype(np.int32)); target_ds = None
        if kind == 'landuse':
            if not item.get('product') or not item.get('year') or not item.get('mapping_note'):
                raise ValueError('土地利用需明确产品、年代、映射依据；未知年代须显式注明unknown')
            if any(not re.fullmatch(r'[A-Za-z0-9_]+', v) for v in mapping.values()):
                raise ValueError('SWAT土地代码须为明确英文代码')
        lookup = output/(kind+'_lookup.csv')
        fields = ['LANDUSE_ID', 'SWAT_CODE'] if kind == 'landuse' else ['SOIL_ID', 'SNAM']
        write_table(lookup, fields, [dict(zip(fields, (i, name))) for i, name in mapping.items()])
        result[kind] = str(path); result[kind+'_lookup'] = str(lookup)
        result['maps'][kind] = {'source': item, 'classes_in_dem': dict(zip(map(str, categories), map(int, counts))),
                                'unknown_set_to_nodata': unknown, 'resampling': 'nearest',
                                'missing_cells_in_dem': int((valid_dem & ~valid).sum())}
    outlet = spec['outlet']; origin = reference(outlet['crs'])
    transform = osr.CoordinateTransformation(origin, target)
    x, y, _ = transform.TransformPoint(float(outlet['x']), float(outlet['y']))
    if not bounds[0] <= x < bounds[2] or not bounds[1] < y <= bounds[3]:
        raise ValueError('出口不在DEM处理域内')
    col, row = int((x-gt[0])/gt[1]), int((y-gt[3])/gt[5])
    if not valid_dem[row, col]: raise ValueError('出口所在DEM像元缺测')
    path = output/'outlet.shp'; vector = ogr.GetDriverByName('ESRI Shapefile').CreateDataSource(str(path))
    layer = vector.CreateLayer('outlet', srs=target, geom_type=ogr.wkbPoint)
    for field in ('ID', 'INLET', 'RES', 'PTSOURCE'): layer.CreateField(ogr.FieldDefn(field, ogr.OFTInteger))
    feature = ogr.Feature(layer.GetLayerDefn())
    for field, value in [('ID', 1), ('INLET', 0), ('RES', 0), ('PTSOURCE', 0)]: feature.SetField(field, value)
    point = ogr.Geometry(ogr.wkbPoint); point.AddPoint_2D(x, y); feature.SetGeometry(point); layer.CreateFeature(feature); vector = None
    result['outlet'] = str(path); result['outlet_xy'] = [x, y]
    result['warnings'] = ['分类图仍须在实际划分的流域内检查覆盖；不填补NoData。']
    if 'WATR' in result['maps']['landuse']['source']['mapping'].values():
        result['warnings'].append('QSWAT+可能把WATR转为湿地WETW，不能据此声称已建立真实水体。')
    return result
