"""Subset an authentic global WGN database before checking/importing it."""
import math
from pathlib import Path
import re
import sqlite3
from raw_tables import write_table
from input_data import WGN_COLUMNS,validate_wgn_rows


def distance_km(lat1,lon1,lat2,lon2):
    a,b=map(math.radians,(lat1,lat2));dl=math.radians(lon2-lon1)
    h=math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(dl/2)**2
    return 6371.0088*2*math.asin(min(1,math.sqrt(h)))


def subset_wgn(spec, locations, output):
    source=Path(spec['database']).resolve()
    with source.open('rb') as f:
        if f.read(16)!=b'SQLite format 3\x00':raise ValueError('WGN不是SQLite数据库；Git LFS指针/网页不能作为数据')
    name=spec.get('table','wgn_cfsr_world')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name):raise ValueError('WGN表名无效')
    if not locations:raise ValueError('WGN区域选择需要目标位置/天气站')
    selected={};matches=[]
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        for location in locations:
            lat,lon=float(location['lat']),float(location['lon'])
            if not -90<=lat<=90 or not -180<=lon<=180:raise ValueError('WGN目标经纬度超界')
            radius=float(spec.get('max_distance_km',200))
            if not 0<radius<=2000:raise ValueError('WGN筛选距离须为0—2000km')
            dy=radius/110.;dx=min(180,dy/max(.001,math.cos(math.radians(min(89.99,abs(lat)+dy)))))
            # Bounding latitude first; longitude is wrapped at the dateline.
            candidates=[dict(r) for r in c.execute(f'SELECT id,name,lat,lon,elev,rain_yrs FROM "{name}" WHERE lat BETWEEN ? AND ?', (max(-90,lat-dy),min(90,lat+dy)))
                        if abs((float(r['lon'])-lon+180)%360-180)<=dx]
            if not candidates:raise ValueError('所选区域没有WGN候选站，请扩大距离或另选资料')
            best=min(candidates,key=lambda r:distance_km(lat,lon,r['lat'],r['lon']))
            distance=distance_km(lat,lon,best['lat'],best['lon'])
            if distance>radius:raise ValueError(f'最近WGN距目标{distance:.1f}km，超过所选范围，请另选资料')
            selected[best['id']]=best
            matches.append({'target':location,'wgn_id':best['id'],'wgn_name':best['name'],'distance_km':round(distance,3)})
        fields={r[1] for r in c.execute(f'PRAGMA table_info("{name}_mon")')}
        fk='wgn_id' if 'wgn_id' in fields else 'weather_wgn_cli_id'
        if fk not in fields:raise ValueError('全球WGN月表缺少站点关联字段')
        marks=','.join('?' for _ in selected)
        columns=','.join('"'+k+'"' for k in WGN_COLUMNS)
        monthly=[dict(r) for r in c.execute(f'SELECT "{fk}" AS wgn_id,month,{columns} FROM "{name}_mon" WHERE "{fk}" IN ({marks})',list(selected))]
    stations=list(selected.values());validate_wgn_rows(stations,monthly)
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    station_file=output/'wgn_station.csv';month_file=output/'wgn_monthly.csv'
    write_table(station_file,['id','name','lat','lon','elev','rain_yrs'],stations)
    write_table(month_file,['id','wgn_id','month',*WGN_COLUMNS],[{'id':i,**r} for i,r in enumerate(monthly,1)])
    return {'ready':True,'wgn_method':'two_file','wgn_station_csv':str(station_file),'wgn_monthly_csv':str(month_file),
            'stations':len(stations),'months':len(monthly),'matches':matches,'source_database':str(source),'source_table':name,
            'provenance':spec.get('source_note','官方全球WGN；CFSR气候统计参数，非当地实测日资料'),
            'selection_note':'最近球面距离候选；最终连接对象匹配由官方Editor执行，地形/气候代表性仍需审阅。'}
