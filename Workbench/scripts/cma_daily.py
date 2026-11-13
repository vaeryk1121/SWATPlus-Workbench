"""CMA SURF_CLI_CHN_MUL_DAY V3 monthly products, per official FORMAT/DOCU."""
import datetime as dt
import math
from pathlib import Path
from raw_tables import write_table
from raw_weather import normalize_weather
from wgn_sources import distance_km

REFERENCE='https://image.data.cma.cn/static/doc/A/SURF_CLI_CHN_MUL_DAY_V3.0/'
PRODUCTS={'TEM':('12001',13,{'tmax':(8,11,.1),'tmin':(9,12,.1)}),
          'PRE':('13011',13,{'pcp':(9,12,.1)}),'RHU':('13003',11,{'hmd':(7,9,.01)}),
          'WIN':('11002',17,{'wnd':(7,12,.1)}),'SSD':('14032',9,{'sunshine':(7,8,.1)})}


def ddmm(value):
    value=int(value);sign=1 if value>=0 else -1;degrees,minutes=divmod(abs(value),100)
    if minutes>=60:raise ValueError('CMA经纬度的分数部分不能超过59')
    return sign*(degrees+minutes/60)


def solar_angstrom(date,lat,hours,a=.25,b=.5):
    """FAO56 eq21/34/35; default coefficients are an explicit estimate."""
    phi=math.radians(lat);j=date.timetuple().tm_yday
    delta=.409*math.sin(2*math.pi*j/365-1.39)
    ws=math.acos(max(-1,min(1,-math.tan(phi)*math.tan(delta))))
    daylight=24/math.pi*ws
    if hours<0 or hours>daylight+.25:raise ValueError('日照时数超出当地天文昼长，请核对日期/单位')
    ra=24*60/math.pi*.0820*(1+.033*math.cos(2*math.pi*j/365))*(ws*math.sin(phi)*math.sin(delta)+math.cos(phi)*math.cos(delta)*math.sin(ws))
    return 0 if daylight==0 else (a+b*min(hours/daylight,1))*ra


def decode(value,variable,policy):
    value=int(value)
    if value==32766:return None,'missing'
    if variable=='pcp':
        if value==32700:
            if policy.get('trace_precipitation')=='zero':return 0.,'trace_as_zero'
            raise ValueError('遇到微量降水32700，请明确trace_precipitation=zero近似策略或另选处理')
        if 30000<=value<32700:
            kind=value//1000
            if kind not in (30,31,32):raise ValueError('未识别的降水特殊编码')
            return (value%1000)*.1,'hydrometeor_code_'+str(kind)
        if value<0:raise ValueError('CMA降水负值无效')
        return value*.1,None
    if variable=='wnd' and value>=1000:return None,'wind_instrument_limit'
    return value,None


def convert_cma(spec,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    start,end=(dt.date.fromisoformat(spec[k]) for k in ('start','end'))
    root=Path(spec['root']);files={}
    for product,(code,count,fields) in PRODUCTS.items():
        selected=[]
        for path in root.rglob(f'SURF_CLI_CHN_MUL_DAY-{product}-{code}-*.TXT'):
            month=path.stem.rsplit('-',1)[-1]
            if start.strftime('%Y%m')<=month<=end.strftime('%Y%m'):selected.append(path)
        if not selected:raise ValueError(f'未找到{product}在所选时段的原始月文件')
        files[product]=sorted(selected)
    stations={}
    for line in files['TEM'][0].read_text(encoding='utf-8-sig').splitlines():
        row=line.split()
        if len(row)!=13:raise ValueError('CMA TEM列数与官方V3格式不符')
        elevation=int(row[3]);estimated=elevation>=100000
        if estimated:elevation-=100000
        stations.setdefault(row[0],{'id':row[0],'lat':ddmm(row[1]),'lon':ddmm(row[2]),'elev':elevation/10,'elevation_estimated':estimated})
    if spec.get('station_ids'):ids=list(map(str,spec['station_ids']))
    else:
        locations=spec.get('locations',[])
        if not locations:raise ValueError('请提供研究区位置或明确站号以筛选全国月资料')
        ids=[]
        for point in locations:
            ordered=sorted(stations.values(),key=lambda s:distance_km(point['lat'],point['lon'],s['lat'],s['lon']))
            ids.extend(s['id'] for s in ordered[:int(spec.get('nearest_count',1))] if distance_km(point['lat'],point['lon'],s['lat'],s['lon'])<=float(spec.get('max_distance_km',150)))
        ids=list(dict.fromkeys(ids))
    if not ids or any(s not in stations for s in ids):raise ValueError('所选站号不存在或超出推荐范围')
    records={};counts={};accepted={str(i) for i in spec.get('accepted_quality_codes',[0])};sources=[]
    for product,paths in files.items():
        _,length,fields=PRODUCTS[product]
        for path in paths:
            sources.append(str(path))
            with path.open(encoding='utf-8-sig') as f:
                for line in f:
                    raw=line.split()
                    if len(raw)!=length:raise ValueError(f'{path.name}: 列数与官方格式不一致')
                    if raw[0] not in ids:continue
                    sid=raw[0];date=dt.date(*map(int,raw[4:7]))
                    if not start<=date<=end:continue
                    station=stations[sid]
                    elevation=int(raw[3]);elevation=(elevation-100000 if elevation>=100000 else elevation)/10
                    if abs(station['lat']-ddmm(raw[1]))>1e-6 or abs(station['lon']-ddmm(raw[2]))>1e-6 or abs(station['elev']-elevation)>1:
                        raise ValueError(f'{sid}: 元数据发生迁站/高程变化，请分段选择，未静默套用首站位置')
                    record=records.setdefault((sid,date),{'station':sid,'date':str(date)})
                    for variable,(column,quality,factor) in fields.items():
                        if variable in record:raise ValueError(f'{sid}/{date}/{variable}: 重复源记录')
                        if raw[quality] not in accepted:value=None;reason='quality_'+raw[quality]
                        else:value,reason=decode(raw[column],variable,spec)
                        if reason:counts[reason]=counts.get(reason,0)+1
                        record[variable]='' if value is None else value if variable=='pcp' else value*factor
    solar=spec.get('solar',{})
    if solar.get('method')!='fao56_angstrom' or not solar.get('source_note'):raise ValueError('只有日照时数时，请明确FAO56辐射推算及系数来源')
    for (sid,date),record in records.items():
        n=record.get('sunshine','')
        record['slr']='' if n=='' else solar_angstrom(date,stations[sid]['lat'],float(n),float(solar['a']),float(solar['b']))
    csv_file=output/'daily_decoded.csv';fields=['station','date','pcp','tmax','tmin','slr','hmd','wnd']
    write_table(csv_file,fields,[records[k] for k in sorted(records)])
    variable_units={'pcp':'mm/day','tmax':'C','tmin':'C','slr':'MJ/m2/day','hmd':'fraction','wnd':'m/s'}
    native={'start':str(start),'end':str(end),'gap_policy':spec.get('gap_policy','error'),'stations':[stations[s] for s in ids],
            'sources':[{'format':'dated_csv','path':str(csv_file),'station_column':'station','date_column':'date',
                        'fields':{v:{'column':v,'unit':u,'missing_codes':['']} for v,u in variable_units.items()},
                        'provenance_note':'CMA V3原始月文件解码；辐射由日照推算，非辐射实测。'}]}
    report=normalize_weather(native,output/'native')
    report.update(product='CMA_daily_V3',selected_stations=[stations[s] for s in ids],special_values=counts,original_sources=sources,
                  format_reference=REFERENCE+'SURF_CLI_CHN_MUL_DAY_FORMAT.doc',quality_reference=REFERENCE+'SURF_CLI_CHN_MUL_DAY_DOCU_C.doc',
                  day_boundary='官方北京时20时日界，保留原日标签；不是UTC日聚合',solar_estimation=solar,policy=spec)
    return report
