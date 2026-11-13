"""Monthly summaries from complete daily records; never invent subdaily rain."""
import calendar
import datetime as dt
import statistics as stat
from pathlib import Path
from raw_tables import write_table
from raw_weather import date_range,EXTENSIONS
from input_data import WGN_COLUMNS


def derive_wgn(weather_spec, weather_report, output, settings):
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    start, end = (dt.date.fromisoformat(weather_spec[k]) for k in ('start', 'end'))
    if (start.month,start.day) != (1,1) or (end.month,end.day) != (12,31):
        raise ValueError('WGN月统计需完整日历年，不能把半个月当全年统计')
    years = end.year-start.year+1
    threshold = float(settings.get('wet_threshold_mm', .1))
    if threshold <= 0: raise ValueError('雨日阈值须为正数')
    stations=[]; monthly=[]; missing=[]
    for i, station in enumerate(weather_spec['stations'],1):
        sid=str(station['id']); series={}
        for kind in weather_report['types']:
            lines=(Path(weather_report['source_dir'])/f's{sid}.{EXTENSIONS[kind]}').read_text().splitlines()[3:]
            for line in lines:
                parts=line.split(); date=dt.date(int(parts[0]),1,1)+dt.timedelta(days=int(parts[1])-1)
                for key,v in zip(['tmax','tmin'] if kind=='tmp' else [kind],parts[2:]):
                    series.setdefault(key,{})[date]=None if float(v)==-99 else float(v)
        halfhour=settings.get('halfhour',{}).get(sid,{})
        rain_years=halfhour.get('rain_yrs')
        stations.append(dict(id=i,name='wgn'+sid,lat=station['lat'],lon=station['lon'],elev=station['elev'],rain_yrs=rain_years))
        if rain_years is None:missing.append(f'{sid}: rain_yrs（半小时雨量记录年数）')
        for month in range(1,13):
            dates=[d for d in date_range(start,end) if d.month==month]
            row=dict(wgn_id=i,month=month,**{k:None for k in WGN_COLUMNS})
            def complete(key):
                values=[series.get(key,{}).get(d) for d in dates]
                return values if values and all(v is not None for v in values) else None
            for variable, ave, sd in [('tmax','tmp_max_ave','tmp_max_sd'),('tmin','tmp_min_ave','tmp_min_sd'),('slr','slr_ave',None),('hmd','dew_ave',None),('wnd','wnd_ave',None)]:
                values=complete(variable)
                if values:
                    row[ave]=stat.mean(values)
                    if sd:row[sd]=stat.stdev(values)
            values=complete('pcp')
            if values:
                row['pcp_ave']=sum(values)/years
                row['pcp_days']=sum(v>=threshold for v in values)/years
                row['pcp_sd']=stat.stdev(values)
                n=len(values);mean=stat.mean(values);sd=row['pcp_sd']
                if sd>0:row['pcp_skew']=n/((n-1)*(n-2))*sum(((v-mean)/sd)**3 for v in values)
                totals={False:[0,0],True:[0,0]}
                for date in dates:
                    previous=series['pcp'].get(date-dt.timedelta(days=1))
                    if previous is not None:
                        pair=totals[previous>=threshold];pair[0]+=1;pair[1]+=series['pcp'][date]>=threshold
                for flag,key in [(False,'wet_dry'),(True,'wet_wet')]:
                    count,wet=totals[flag]
                    if count:row[key]=wet/count
            maxima=halfhour.get('monthly_maxima_mm',[])
            if len(maxima)==12 and halfhour.get('source_note'):
                row['pcp_hhr']=maxima[month-1]
            for key in WGN_COLUMNS:
                if row[key] is None:missing.append(f'{sid}/{month}: {key}')
            monthly.append(row)
    write_table(output/'wgn_station.csv',['id','name','lat','lon','elev','rain_yrs'],stations)
    write_table(output/'wgn_monthly.csv',['wgn_id','month',*WGN_COLUMNS],monthly)
    return {'ready':not missing,'missing':missing,'wgn_station_csv':str(output/'wgn_station.csv'),
            'wgn_monthly_csv':str(output/'wgn_monthly.csv'), 'wgn_method':'two_file',
            'formula_note':f'{start}至{end}，{years}完整年；温度/日辐射/相对湿度/风速日均值，温度和日雨量样本SD；雨量月总量/年数，雨日数/年数，雨日阈值{threshold}mm；日雨量含零无偏样本偏度；转移按当前日月份分组，首日缺前日则排除；dew_ave使用引擎支持的相对湿度小数。全干月偏度/无样本转移概率保留缺项。pcp_hhr及rain_yrs只能来自有来源的半小时记录。'}
