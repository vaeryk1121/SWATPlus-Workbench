"""Prepare local raw inputs and a model plan in a new case output directory."""
import argparse
import copy
import datetime as dt
import json
from pathlib import Path
from input_paths import CODE_ROOT


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path,data):
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def inspect_recipe(spec):
    from raw_tables import table
    result={'case_root':spec['case_root'],'sources':[]}
    for key in ('spatial','soil','weather'):
        if key not in spec:continue
        item=spec[key]
        if key=='soil':
            if item.get('product') in ('hwsd2','official_global'):
                result['sources'].append({'part':key,'product':item['product'],'database':item['database'],'model_assumptions':item.get('model_assumptions',{}),'note':'按研究区读取配套土壤，不载入整个全球属性表；不得混用产品编号。'})
                continue
            rows=table(item['table'])
            result['sources'].append({'part':key,'path':item['table']['path'],'records':len(rows),'columns':list(rows[0]) if rows else [],'preview':rows[:2]})
        elif key=='weather':
            if item.get('product')=='cma_daily_v3':
                result['sources'].append({'part':key,'product':'CMA_daily_V3','root':item['root'],'start':item['start'],'end':item['end'],'locations':item.get('locations'),'station_ids':item.get('station_ids'),'solar':item.get('solar'),'note':'依照官方V3格式/产品说明解码；日照辐射为推算。'})
                continue
            for source in item['sources']:
                if source['format']=='dated_csv':
                    rows=table(source);result['sources'].append({'part':key,'path':source['path'],'records':len(rows),'columns':list(rows[0]) if rows else [],'preview':rows[:2]})
                else:
                    files=sorted(Path(source['directory']).glob(source['glob']))
                    result['sources'].append({'part':key,'path':source['directory'],'files':len(files),'example':str(files[0]) if files else None,'fields':source['fields']})
        else:
            from osgeo import gdal
            gdal.SetConfigOption('GDAL_PAM_ENABLED','NO')
            for kind in ('dem','landuse','soil'):
                path=item[kind] if kind=='dem' else item[kind]['path']
                ds=gdal.Open(str(path),gdal.GA_ReadOnly)
                entry={'part':kind,'path':path,'size':[ds.RasterXSize,ds.RasterYSize],'crs':ds.GetProjection(),'geotransform':list(ds.GetGeoTransform())}
                if kind=='dem':
                    from raw_spatial import suggest_crs
                    entry['projection_recommendation']=suggest_crs(ds)
                else:
                    import numpy as np
                    values=ds.GetRasterBand(1).ReadAsArray(buf_xsize=min(512,ds.RasterXSize),buf_ysize=min(512,ds.RasterYSize))
                    entry['sample_classes']=np.unique(values).tolist();entry['sample_only']=True
                result['sources'].append(entry)
    if spec.get('wgn',{}).get('product')=='swatplus_global':result['sources'].append({'part':'wgn',**spec['wgn']})
    return result


def prepare(spec, output=None):
    from raw_tables import normalize_soils
    from raw_weather import normalize_weather
    spec=copy.deepcopy(spec)
    if not Path(spec['case_root']).is_absolute():raise ValueError('case_root必须为绝对路径，避免案例写到程序当前目录')
    root=Path(spec['case_root']).resolve()
    # Each conversion is a new directory; existing case files are never rewritten.
    output=Path(output) if output else root/'data/processed'/('raw_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    output=output.resolve();output.relative_to(root)
    output.mkdir(parents=True,exist_ok=False)
    save(output/'source_recipe.json',spec)
    report={'output':str(output),'case_root':str(root),'parts':{},'blockers':[],'ready':False}
    for key in ('spatial','soil','weather'):
        if key not in spec:
            report['blockers'].append('未提供'+key+'原始资料');continue
        try:
            if key=='spatial':
                from raw_spatial import normalize_spatial
                if spec.get('soil',{}).get('product')=='hwsd2':spec[key]['soil']['auto_hwsd2_names']=True
                result=normalize_spatial(spec[key],output/key)
            elif key=='soil':
                if spec[key].get('product') in ('hwsd2','official_global'):
                    from soil_sources import normalize_hwsd2,normalize_official_soils
                    if spec[key].get('selection')=='delineated_catchment':
                        from input_catchment import select_catchment_soils
                        selected=select_catchment_soils(spec,report['parts']['spatial'],root,output/'catchment_selection')
                        spec[key]['selected_ids']=selected['selected_ids'];report['parts']['soil_selection']=selected
                    converter=normalize_hwsd2 if spec[key]['product']=='hwsd2' else normalize_official_soils
                    result=converter(spec[key],report['parts']['spatial'],output/key)
                else:result=normalize_soils(spec[key],output/key)
            elif spec[key].get('product')=='cma_daily_v3':
                from cma_daily import convert_cma
                result=convert_cma(spec[key],output/key)
            else:result=normalize_weather(spec[key],output/key)
            report['parts'][key]=result
            if result.get('ready') is False:
                report['blockers'].append(f'{key}: 来源格式/质量码说明待核实' if result.get('pending_metadata') else f'{key}: 属性仍有缺项，见missing列表')
        except Exception as error:
            report['parts'][key]={'ready':False,'error':str(error)}
            report['blockers'].append(f'{key}: {error}')
    weather=report['parts'].get('weather',{})
    if weather.get('source_dir') and not weather.get('error'):
        if spec.get('wgn',{}).get('product')=='swatplus_global':
            from wgn_sources import subset_wgn
            try:
                locations=weather.get('selected_stations') or spec.get('weather',{}).get('stations') or spec['wgn'].get('locations')
                result=subset_wgn(spec['wgn'],locations,output/'wgn')
                report['parts']['wgn']=result
                weather.update({k:result[k] for k in ('wgn_method','wgn_station_csv','wgn_monthly_csv')})
            except Exception as error:report['blockers'].append('全球WGN: '+str(error))
        elif spec.get('derive_wgn') is not None:
            from raw_wgn import derive_wgn
            try:
                result=derive_wgn(spec['weather'],weather,output/'wgn',spec['derive_wgn'])
                report['parts']['wgn']=result
                if result['ready']:weather.update({k:result[k] for k in ('wgn_method','wgn_station_csv','wgn_monthly_csv')})
                else:report['blockers'].append('WGN月统计仍有缺项，尤其半小时最大雨量及其记录年数不能从逐日量推算')
            except Exception as error:report['blockers'].append('WGN: '+str(error))
        else:weather.update(spec.get('wgn',{}))
        weather['allow_generated']=spec.get('allow_generated',False)
    if not report['blockers']:
        try:report['plan']=emit_plan(spec,report,output)
        except Exception as error:report['blockers'].append('既有输入检查: '+str(error))
    report['ready']=not report['blockers']
    save(output/'conversion_report.json',report)
    return report


def emit_plan(spec,report,output):
    from build_preflight import check_paths,inspect_spatial
    root=Path(spec['case_root']).resolve()
    sp=read(CODE_ROOT/'config/standard_spatial.json');ed=read(CODE_ROOT/'config/standard_editor.json')
    cfg=read(CODE_ROOT/'config/standard_workflow.json');pipeline=read(CODE_ROOT/'config/standard_pipeline.json')
    spatial=report['parts']['spatial'];soil=report['parts']['soil'];weather=report['parts']['weather']
    sp.update({k:spatial[k] for k in ('dem','landuse','soil','outlet','landuse_lookup')})
    sp.update({k:soil[k] for k in ('usersoil','usersoil_layer','usersoil_format','soil_lookup')})
    sp.update(raw_data='',strict_soil_semantics=True,**spec.get('build_settings',{}))
    ed['weather']={k:v for k,v in weather.items() if k in ('format','source_dir','wgn_method','wgn_station_csv','wgn_monthly_csv','wgn_database','wgn_table','allow_generated')}
    sim=spec['simulation'];cfg['simulation'].update(sim);ed['simulation'].update(sim)
    check=check_paths(sp,ed,root,sim);check['spatial']=inspect_spatial(sp,root)
    save(output/'input_check.json',check)
    runtime=read(CODE_ROOT/'config/qgis344_runtime.json')
    for key in ('plugin_root','profile_root'):
        runtime[key]=str((CODE_ROOT/runtime[key]).resolve())
    runtime_path=output/'runtime.json';save(runtime_path,runtime)
    for data in (sp,ed,cfg,pipeline):data.update(workspace_root=str(root),runtime_config=str(runtime_path))
    for key in ('spatial','editor','model'):pipeline[key+'_config']=str(output/(key+'.json'))
    model=read(CODE_ROOT/'config/standard_model.json');model.update(workspace_root=str(root),runtime_config=str(runtime_path))
    for key,data in [('spatial',sp),('editor',ed),('model',model),('pipeline',pipeline)]:save(output/(key+'.json'),data)
    cfg['build']['pipeline_config']=str(output/'pipeline.json')
    cfg['title']=spec.get('title','原始资料规范化建模')
    cfg['frequency']=spec.get('frequency','monthly')
    save(output/'workflow.json',cfg)
    return str(output/'workflow.json')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe',type=Path,required=True)
    parser.add_argument('--preview',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();spec=read(args.recipe)
    report=inspect_recipe(spec) if args.preview else prepare(spec,args.output)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    return 0 if args.preview or report['ready'] else 2


if __name__=='__main__':raise SystemExit(main())
