"""Input compatibility helpers; official QSWAT+/Editor still perform model imports."""
import calendar
import csv
import datetime as dt
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3

KINDS = {"pcp":1,"tmp":2,"slr":1,"hmd":1,"wnd":1}
INDEX_2012 = {'pcp':('pcp.txt',),'tmp':('tmp.txt',),'slr':('solar.txt','slr.txt'),'hmd':('rh.txt','hmd.txt'),'wnd':('wind.txt','wnd.txt')}
WGN_COLUMNS = "tmp_max_ave tmp_min_ave tmp_max_sd tmp_min_sd pcp_ave pcp_sd pcp_skew wet_dry wet_wet pcp_days pcp_hhr slr_ave dew_ave wnd_ave".split()


def csv_data(path):
    with Path(path).open(encoding="utf-8-sig",newline="") as f:
        sample=f.read(8192);f.seek(0)
        try:dialect=csv.Sniffer().sniff(sample,delimiters=",;\t")
        except csv.Error:dialect=csv.excel
        reader=csv.DictReader(f,dialect=dialect)
        rows=list(reader)
        if not rows or not reader.fieldnames:raise ValueError(f"CSV为空：{path}")
        return [x.strip() for x in reader.fieldnames], [{k.strip():v for k,v in row.items() if k is not None} for row in rows]


def lookup_rows(path, kind):
    header,data=csv_data(path);keys={x.strip("\"'").casefold():x for x in header}
    idname=kind+"_id"
    if idname not in keys and 'value' in keys:keys[idname]=keys['value']
    if idname not in keys:raise ValueError(f"{path}: 缺少{idname.upper()}列")
    idcol=keys[idname]
    if kind=="landuse":
        if 'swat_code' not in keys and 'landuse' in keys:keys['swat_code']=keys['landuse']
        if "swat_code" not in keys:raise ValueError("土地利用对应表缺少SWAT_CODE")
        namecol=keys["swat_code"]
    else:
        namecol=keys.get("snam") or keys.get("name")
        if namecol is None:
            other=[c for c in header if c!=idcol]
            if len(other)!=1:raise ValueError("土壤对应表须明确SNAM/NAME列")
            namecol=other[0]
    output={}
    for row in data:
        number=int(row[idcol]);name=row[namecol].strip()
        if not name or number in output:raise ValueError(f"{kind}: 类别编号重复或名称为空")
        output[number]=name
    return output


def soil_profiles(spatial, root):
    raw=Path(root)/spatial["raw_data"];path=raw/spatial["usersoil"]
    header,data=csv_data(path);cols={x.casefold():x for x in header}
    mode=spatial.get("usersoil_format","wide")
    if mode not in ("wide","separate"):raise ValueError("土壤格式须为wide或separate")
    namecol=cols.get("snam") if mode=="wide" else cols.get("name")
    if not namecol:raise ValueError("土壤属性缺少SNAM（宽表）或NAME（主表）")
    names=[r[namecol].strip() for r in data]
    if any(not n for n in names) or len(names)!=len(set(names)):raise ValueError("土壤名称为空或重复")
    if mode=="wide":
        expected='OBJECTID MUID SEQN SNAM S5ID CMPPCT NLAYERS HYDGRP SOL_ZMX ANION_EXCL SOL_CRK TEXTURE'.split()
        expected += [prefix+str(i) for i in range(1,11) for prefix in ('SOL_Z','SOL_BD','SOL_AWC','SOL_K','SOL_CBN','CLAY','SILT','SAND','ROCK','SOL_ALB','USLE_K','SOL_EC')]
        expected += ['SOL_CAL'+str(i) for i in range(1,11)]+['SOL_PH'+str(i) for i in range(1,11)]
        if [c.casefold() for c in header]!=[c.casefold() for c in expected]:raise ValueError("QSWAT+宽表须为官方152列顺序；11列主表请选择主表＋分层表")
        for r in data:
            n=int(float(r[cols['nlayers']]))
            if not 1<=n<=10:raise ValueError("宽表土壤层数应为1到10")
    else:
        expected='id name muid seqn s5id cmppct hydgrp sol_zmx anion_excl sol_crk texture'.split()
        if [c.casefold() for c in header]!=expected:raise ValueError("独立土壤主表须采用官方11列顺序，包含ID与NAME")
        layers_path=raw/spatial.get("usersoil_layer", "")
        h,layers=csv_data(layers_path)
        lower={k.casefold():k for k in h}
        if [c.casefold() for c in h]!='id soil_id layer_num dp bd awc soil_k carbon clay silt sand rock alb usle_k ec caco3 ph'.split():raise ValueError("土壤分层表须采用官方17列顺序")
        ids=[int(r[cols['id']]) for r in data]
        if len(ids)!=len(set(ids)):raise ValueError("土壤主表ID重复")
        groups={i:[] for i in ids}
        for r in layers:
            identity=int(r[lower['soil_id']])
            if identity not in groups:raise ValueError("土壤分层表存在未对应的soil_id")
            groups[identity].append((int(r[lower['layer_num']]),float(r[lower['dp']])))
        for identity,values in groups.items():
            values=sorted(values)
            if not values or [x[0] for x in values]!=list(range(1,len(values)+1)):raise ValueError(f"土壤{identity}: 层号须从1连续且不重复")
            depths=[x[1] for x in values]
            if any(not math.isfinite(x) or x<=0 for x in depths) or any(a>=b for a,b in zip(depths,depths[1:])):raise ValueError("土壤层底深度须递增且为正数，单位mm")
        if spatial.get('strict_soil_semantics'):
            from raw_tables import check_soil_row
            for row in data:
                numeric={k:float(row[v]) for k,v in cols.items() if k in ('sol_zmx','anion_excl','sol_crk','cmppct') and not (k=='cmppct' and row[v] in ('',None))}
                numeric['hydgrp']=row[cols['hydgrp']]
                check_soil_row(numeric)
            for row in layers:
                numeric={k:float(row[v]) for k,v in lower.items() if k not in ('id','soil_id','layer_num') and not (k in ('ph','caco3') and row[v] in ('',None))}
                if not all(math.isfinite(v) for v in numeric.values()):raise ValueError('土壤分层表存在非有限值')
                check_soil_row(numeric,True)
    return set(names)


def check_wgn(weather, root):
    root=Path(root);method=weather.get("wgn_method","two_file")
    if method=="database":
        path=root/weather["wgn_database"];table=weather["wgn_table"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",table):raise ValueError("WGN表名须为英文、数字和下划线")
        with sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True) as conn:
            conn.row_factory=sqlite3.Row
            for name,required in [(table,{"id","name","lat","lon","elev","rain_yrs"}),(table+"_mon",{"wgn_id","month",*WGN_COLUMNS})]:
                fields={r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}
                if not required<=fields or conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]==0:raise ValueError(f"WGN数据库表{name}缺列或为空")
            stations=[dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
            monthly=[dict(r) for r in conn.execute(f'SELECT * FROM "{table}_mon"')]
            validate_wgn_rows(stations,monthly)
        return {"method":method,"database":str(path),"table":table}
    if method!="two_file":raise ValueError("本入口支持WGN两文件CSV或官方数据库方式")
    _,stations=csv_data(root/weather["wgn_station_csv"]);_,monthly=csv_data(root/weather["wgn_monthly_csv"])
    required={"id","name","lat","lon","elev","rain_yrs"}
    if not required<=stations[0].keys():raise ValueError("WGN站点CSV列名不完整")
    monthly=[{("wgn_id" if k=="weather_wgn_cli" else k):v for k,v in r.items()} for r in monthly]
    if not {"wgn_id","month",*WGN_COLUMNS}<=monthly[0].keys():raise ValueError("WGN月参数CSV列名不完整")
    validate_wgn_rows(stations,monthly)
    return {"method":method,"stations":len(stations)}


def validate_wgn_rows(stations,monthly):
    """Same checks for CSV and database; never silently repair probabilities."""
    ids=[int(r['id']) for r in stations];pairs=[]
    if len(ids)!=len(set(ids)):raise ValueError("WGN站点ID重复")
    for r in stations:
        if not r['name'] or not all(math.isfinite(float(r[k])) for k in ('lat','lon','elev','rain_yrs')):raise ValueError('WGN站点名称/坐标/雨量记录年数无效')
        if not -90<=float(r['lat'])<=90 or not -180<=float(r['lon'])<=180 or float(r['rain_yrs'])<=0:raise ValueError('WGN经纬度或rain_yrs超界')
    for r in monthly:
        pairs.append((int(r['wgn_id']),int(r['month'])))
        if not all(math.isfinite(float(r[k])) for k in WGN_COLUMNS):raise ValueError("WGN月参数有非有限值")
        for key in ('wet_dry','wet_wet'):
            if not 0<=float(r[key])<=1:raise ValueError(f'WGN {key}应为0至1的转移概率，不能把天数当概率')
        for key in ('tmp_max_sd','tmp_min_sd','pcp_ave','pcp_sd','pcp_days','pcp_hhr','slr_ave','wnd_ave'):
            if float(r[key])<0:raise ValueError(f'WGN {key}不能为负')
        month=int(r['month'])
        if not 1<=month<=12 or float(r['pcp_days'])>calendar.monthrange(2000,month)[1]:raise ValueError('WGN月雨日数超出日历范围')
        if float(r['tmp_max_ave'])<float(r['tmp_min_ave']):raise ValueError('WGN最高温均值小于最低温均值')
    if len(pairs)!=len(set(pairs)) or set(pairs)!={(i,m) for i in ids for m in range(1,13)}:raise ValueError("WGN每个站点必须有且仅有12个月参数")


def check_weather(weather, root, simulation):
    root=Path(root);folder=root/weather['source_dir'];mode=weather.get('format','2012')
    if mode not in ('2012','plus'):raise ValueError("气象格式须为2012或plus")
    result={};missing=[]
    for kind,columns in KINDS.items():
        candidates=[folder/name for name in INDEX_2012[kind]] if mode=='2012' else [folder/(kind+'.cli')]
        existing=[p for p in candidates if p.is_file()]
        if len(existing)>1 and any(p.read_bytes()!=existing[0].read_bytes() for p in existing[1:]):raise ValueError(f'{kind}: 官方名与旧别名索引同时存在且内容不同，请明确数据来源')
        index=existing[0] if existing else candidates[0]
        if not index.is_file():missing.append(kind);continue
        if mode=='2012':
            _,stations=csv_data(index)
            if not {'ID','NAME','LAT','LONG','ELEVATION'}<=stations[0].keys():raise ValueError(f"{index}: 气象站目录列名不正确")
            files=[(folder/(r['NAME']+'.txt'),r) for r in stations]
        else:
            lines=index.read_text(encoding='utf-8-sig').splitlines()
            names=[line.strip() for line in lines[2:] if line.strip()]
            if not names:raise ValueError(f"{index}: 气象索引为空")
            files=[(folder/name,None) for name in dict.fromkeys(names)]
        for path,station in files:
            if path.resolve().parent!=folder.resolve():raise ValueError("气象站文件应位于所选目录中")
            first=None;last=None;count=0
            with path.open(encoding='utf-8-sig') as stream:
                if mode=='2012':
                    first=dt.datetime.strptime(stream.readline().strip(),'%Y%m%d').date()
                    lat,lon=float(station['LAT']),float(station['LONG'])
                    float(station['ELEVATION'])
                else:
                    stream.readline();stream.readline();meta=stream.readline().split()
                    if len(meta)!=5 or int(meta[1])!=0:raise ValueError("当前气象入口支持日格式nbyr,tstep=0,lat,lon,elev")
                    lat,lon=float(meta[2]),float(meta[3]);float(meta[4])
                if not -90<=lat<=90 or not -180<=lon<=180:raise ValueError("气象站经纬度超界")
                for number,line in enumerate(stream,2 if mode=='2012' else 4):
                    raw=line.strip().split(',') if mode=='2012' else line.split()
                    if mode=='plus':
                        year,day=int(raw[0]),int(raw[1])
                        if not 1<=day<=365+calendar.isleap(year):raise ValueError("气象文件年积日超界")
                        date=dt.date(year,1,1)+dt.timedelta(days=day-1);raw=raw[2:]
                        if last and date!=last+dt.timedelta(days=1):raise ValueError(f"{path}:{number}: 日期不连续或重复")
                        if first is None:first=date
                    else:date=first+dt.timedelta(days=count)
                    values=[float(x) for x in raw]
                    if len(values)!=columns or not all(math.isfinite(x) for x in values):raise ValueError(f"{path}:{number}: 列数或数值无效")
                    valid=[x for x in values if x!=-99]
                    if -999 in values:raise ValueError("请先明确将缺测整理为-99，不能直接混入-999")
                    if kind in ('pcp','slr','wnd') and any(x<0 for x in valid):raise ValueError("非缺测值不能为负")
                    if kind=='hmd' and any(not 0<=x<=1 for x in valid):raise ValueError("湿度须为0到1的小数")
                    if kind=='tmp' and -99 not in values and values[0]<values[1]:raise ValueError("温度顺序应为最高温、最低温")
                    last=date;count+=1
            if not count:raise ValueError(f"气象站文件无日数据：{path}")
            if first>dt.date(simulation['start_year'],1,1) or last<dt.date(simulation['end_year'],12,31):raise ValueError(f"{path}: 气象资料未覆盖含预热的模拟期")
            result[kind+':'+path.stem]={'start':str(first),'end':str(last),'days':count}
    if not result:raise ValueError("至少提供一种实际日气象资料；纯发生器方案请在Editor中另行配置")
    if missing and not weather.get('allow_generated',False):raise ValueError('缺少气象类型'+','.join(missing)+'；若确需用WGN生成，请显式启用该选项')
    return {'weather':result,'weather_format':mode,'generated_types':missing,'wgn':check_wgn(weather,root)}


def normalize_lookups(spatial, root, output):
    """Only standardize headers/order; never modify category values or source CSVs."""
    output=Path(output);output.mkdir(parents=True,exist_ok=False);updated=dict(spatial)
    for kind in ('landuse','soil'):
        values=lookup_rows(Path(root)/spatial['raw_data']/spatial[kind+'_lookup'],kind)
        path=output/(kind+'_lookup.csv')
        with path.open('w',encoding='utf-8',newline='') as f:
            writer=csv.writer(f);writer.writerow(['LANDUSE_ID','SWAT_CODE'] if kind=='landuse' else ['SOIL_ID','SNAM']);writer.writerows(values.items())
        updated[kind+'_lookup']=str(path)
    return updated


def stage_plus_weather(source, destination):
    source=Path(source);destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    for kind in (*KINDS,'pet'):
        index=source/(kind+'.cli')
        if index.is_file():
            shutil.copy2(index,destination/index.name)
            for name in dict.fromkeys(l.strip() for l in index.read_text(encoding='utf-8-sig').splitlines()[2:] if l.strip()):
                path=source/name
                if path.resolve().parent!=source.resolve():raise ValueError("气象依赖不能指向所选目录外")
                shutil.copy2(path,destination/path.name)


def split_wide_soils(source,output):
    """Rearrange the official 152 columns into native 11+17 tables; values unchanged."""
    header,data=csv_data(source)
    if len(header)!=152:raise ValueError("请选择152列SWAT2012土壤宽表")
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    main=output/"usersoil.csv";layers=output/"usersoil_layer.csv"
    with main.open('w',encoding='utf-8',newline='') as fm,layers.open('w',encoding='utf-8',newline='') as fl:
        wm=csv.writer(fm);wl=csv.writer(fl)
        wm.writerow('ID NAME MUID SEQN S5ID CMPPCT HYDGRP SOL_ZMX ANION_EXCL SOL_CRK TEXTURE'.split())
        wl.writerow('id soil_id layer_num dp bd awc soil_k carbon clay silt sand rock alb usle_k ec caco3 ph'.split())
        counter=0
        for row in data:
            sid=row['OBJECTID'];n=int(float(row['NLAYERS']))
            if not 1<=n<=10:raise ValueError("土壤层数须为1至10")
            wm.writerow([sid,row['SNAM'],row['MUID'],row['SEQN'],row['S5ID'],row['CMPPCT'],row['HYDGRP'],row['SOL_ZMX'],row['ANION_EXCL'],row['SOL_CRK'],row['TEXTURE']])
            for i in range(1,n+1):
                counter+=1
                keys=[prefix+str(i) for prefix in ('SOL_Z','SOL_BD','SOL_AWC','SOL_K','SOL_CBN','CLAY','SILT','SAND','ROCK','SOL_ALB','USLE_K','SOL_EC','SOL_CAL','SOL_PH')]
                wl.writerow([counter,sid,i,*[row[key] for key in keys]])
    return str(main),str(layers)
