"""Reuse official terrain delineation to select soils inside the actual catchment."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
from input_paths import CODE_ROOT


def select_catchment_soils(spec,spatial,root,output):
    import numpy as np
    from osgeo import gdal,ogr
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    runtime=json.loads((CODE_ROOT/'config/qgis344_runtime.json').read_text(encoding='utf-8-sig'))
    runtime['plugin_root']=str(CODE_ROOT/runtime['plugin_root'])
    runtime_file=output/'runtime.json';runtime_file.write_text(json.dumps(runtime),encoding='utf-8')
    cfg=json.loads((CODE_ROOT/'config/standard_spatial.json').read_text(encoding='utf-8-sig'))
    cfg.update({k:spatial[k] for k in ('dem','landuse','soil','outlet','landuse_lookup','soil_lookup')})
    name='prep_'+dt.datetime.now().strftime('%m%d_%H%M%S_%f')
    cfg.update(workspace_root=str(root),runtime_config=str(runtime_file),raw_data='',project_name=name,
               project_dir=str(output/'project'),build_log_dir=str(output/'logs'),**spec.get('build_settings',{}))
    cfg_path=output/'spatial.json';cfg_path.write_text(json.dumps(cfg),encoding='utf-8')
    for stage in ('prepare','delineate'):
        command=[str(Path(runtime['qgis_root'])/'bin/python-qgis-ltr.bat'),'-X','utf8',str(CODE_ROOT/'scripts/build_qswat344_project.py'),'--config',str(cfg_path),'--stage',stage]
        env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1'
        with (output/(stage+'.log')).open('wb') as log:
            run=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,env=env,timeout=600,shell=os.name=='nt',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if run.returncode:raise ValueError('原生流域划分失败，请查看 '+str(output/(stage+'.log')))
    shapes=output/'project/Watershed/Shapes/demsubbasins.shp'
    vector=ogr.Open(str(shapes));layer=vector.GetLayer();layer.SetAttributeFilter('"Subbasin">0')
    source=gdal.Open(spatial['soil']);mask=gdal.GetDriverByName('MEM').Create('',source.RasterXSize,source.RasterYSize,1,gdal.GDT_Byte)
    mask.SetProjection(source.GetProjection());mask.SetGeoTransform(source.GetGeoTransform());gdal.RasterizeLayer(mask,[1],layer,burn_values=[1])
    inside=mask.ReadAsArray()>0;values=source.ReadAsArray();nodata=source.GetRasterBand(1).GetNoDataValue()
    if not inside.any():raise ValueError('实际流域掩膜没有像元')
    if np.any(inside & ((values==nodata)|~np.isfinite(values))):raise ValueError('实际流域内土壤分类缺失；未填补')
    ids=list(map(int,np.unique(values[inside])))
    return {'selected_ids':ids,'selection':'official_QSWAT_delineated_catchment','polygon':str(shapes),
            'inside_cells':int(inside.sum()),'logs':str(output),'note':'只按本轮新划分流域选土壤，不复制旧HRU；完整建模前仍保留实际流域覆盖检查。'}
