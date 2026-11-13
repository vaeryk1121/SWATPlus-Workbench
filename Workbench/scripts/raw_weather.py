"""Dated daily CSV and explicitly described whitespace station records to SWAT+."""
import datetime as dt
import math
from pathlib import Path
import re
from raw_tables import table

FACTORS = {'pcp': {'mm/day': (1, 0), '0.1mm/day': (.1, 0)},
           'tmax': {'C': (1, 0), '0.1C': (.1, 0), 'K': (1, -273.15)},
           'tmin': {'C': (1, 0), '0.1C': (.1, 0), 'K': (1, -273.15)},
           'slr': {'MJ/m2/day': (1, 0), 'W/m2_daily_mean': (.0864, 0)},
           'hmd': {'fraction': (1, 0), '%': (.01, 0)},
           'wnd': {'m/s': (1, 0), '0.1m/s': (.1, 0), 'km/day': (1/86.4, 0)}}
KINDS = {'pcp': ['pcp'], 'tmp': ['tmax', 'tmin'], 'slr': ['slr'], 'hmd': ['hmd'], 'wnd': ['wnd']}
EXTENSIONS = {'pcp':'pcp','tmp':'tem','slr':'slr','hmd':'hmd','wnd':'wnd'}


def date_range(start, end):
    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)


def convert_value(raw, field, name, quality=None):
    if str(raw).strip() in [str(v) for v in field.get('missing_codes', [])]:
        return None, 'source_missing'
    if quality is not None and str(quality) not in [str(v) for v in field['accepted_quality_codes']]:
        return None, 'quality_rejected'
    unit = field.get('unit')
    if unit not in FACTORS[name]:
        raise ValueError(f'{name}: 未支持或未明确单位 {unit}')
    factor, offset = FACTORS[name][unit]
    value = float(raw)*factor + offset
    if not math.isfinite(value):
        raise ValueError(f'{name}: 非有限值')
    if value <= -97:
        raise ValueError('换算结果落入SWAT缺测值范围(<=-97)；请明确源缺测码，不能当作有效观测导入')
    if name in ('pcp', 'slr', 'wnd') and value < 0:
        raise ValueError(f'{name}: 非缺测数值不能为负')
    if name == 'hmd' and not 0 <= value <= 1:
        raise ValueError('湿度单位换算后须在0至1')
    if name in ('tmax', 'tmin') and not -100 < value < 70:
        raise ValueError('气温超出本适配器支持范围，请核对特殊码/单位')
    if name == 'pcp' and value > 3000 or name == 'wnd' and value > 150:
        raise ValueError('疑似尚未解释的特殊气象编码，停止转换')
    return value, None


def normalize_weather(spec, output):
    """Reject duplicates; gaps require explicit 'missing' (SWAT -99, never zero)."""
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    start, end = (dt.date.fromisoformat(spec[k]) for k in ('start', 'end'))
    if start > end:
        raise ValueError('气象起止日期颠倒')
    stations = {str(s['id']): s for s in spec['stations']}
    if len(stations) != len(spec['stations']) or not stations:
        raise ValueError('站号为空或重复')
    for sid, s in stations.items():
        if not re.fullmatch(r'[A-Za-z0-9_]+', sid):
            raise ValueError('站号只能使用英文数字下划线')
        if not -90 <= float(s['lat']) <= 90 or not -180 <= float(s['lon']) <= 180 or not math.isfinite(float(s['elev'])):
            raise ValueError('必须提供明确有效的站点经纬度与高程')
    data = {}; reasons = {}; sources = []; used_files = set()
    for source in spec['sources']:
        fields = source['fields']; sources.append(source)
        if not fields or any(k not in FACTORS for k in fields):
            raise ValueError('气象字段仅支持pcp/tmax/tmin/slr/hmd/wnd；日照不能当作辐射')
        if source['format'] == 'dated_csv':
            records = table(source)
            iterator = ((str(row[source['station_column']]), dt.datetime.strptime(row[source['date_column']], source.get('date_format', '%Y-%m-%d')).date(), row, str(source['path'])) for row in records)
        elif source['format'] == 'station_whitespace':
            # Column offsets, scale, quality and sentinel definitions are explicit,
            # allowing actual CMA monthly files without inventing metadata.
            if not source.get('format_reference'):
                raise ValueError('空白分隔原始站文件必须记录格式/质量码依据')
            def rows_from_files():
                files = sorted(Path(source['directory']).glob(source['glob']))
                if not files:
                    raise ValueError('未找到符合规则的原始气象文件')
                for path in files:
                    with path.open(encoding=source.get('encoding', 'utf-8-sig')) as stream:
                        for n, line in enumerate(stream, 1):
                            if not line.strip(): continue
                            row = line.split()
                            if len(row) != source['column_count']:
                                raise ValueError(f'{path}:{n}: 与声明的列数不一致')
                            sid = row[source['station_column']]
                            if sid not in stations: continue
                            date = dt.date(*(int(row[source[k]]) for k in ('year_column', 'month_column', 'day_column')))
                            yield sid, date, row, str(path)
            iterator = rows_from_files()
        else:
            raise ValueError('原始气象支持dated_csv或station_whitespace')
        for sid, date, row, filename in iterator:
            if sid not in stations or not start <= date <= end: continue
            used_files.add(filename)
            for name, field in fields.items():
                key = (sid, name, date)
                if key in data:
                    raise ValueError(f'{sid} {date} {name}: 重复记录，未取平均或静默覆盖')
                quality = row[field['quality_column']] if 'quality_column' in field else None
                if quality is not None and 'accepted_quality_codes' not in field:
                    raise ValueError('需要明确选择可接受质量码')
                value, why = convert_value(row[field['column']], field, name, quality)
                data[key] = value
                if why: reasons[key] = why
    kinds = set()
    for source in sources:
        for kind, names in KINDS.items():
            if all(any(n in s['fields'] for s in sources) for n in names): kinds.add(kind)
    if not kinds or any(any(n in s['fields'] for s in sources) for n in ('tmax', 'tmin')) and 'tmp' not in kinds:
        raise ValueError('温度须同时指定最高与最低温；至少提供一种气象类型')
    missing = []; files = []
    for kind in KINDS:
        if kind not in kinds: continue
        names = []
        for sid, station in stations.items():
            name = f's{sid}.{EXTENSIONS[kind]}'; names.append(name)
            records = []
            for date in date_range(start, end):
                values = []
                for variable in KINDS[kind]:
                    key = (sid, variable, date); value = data.get(key)
                    if value is None:
                        missing.append({'station': sid, 'date': str(date), 'variable': variable, 'reason': reasons.get(key, 'date_missing')})
                    values.append(value)
                if kind == 'tmp' and all(v is not None for v in values) and values[0] < values[1]:
                    raise ValueError(f'{sid} {date}: 最高温小于最低温')
                records.append((date, values))
            files.append((output/name, station, records))
        (output/(kind+'.cli')).write_text('raw-source daily conversion\nfilename\n'+'\n'.join(sorted(names))+'\n', encoding='utf-8')
    if missing and spec.get('gap_policy', 'error') != 'missing':
        from raw_tables import write_table
        write_table(output/'missing_records.csv', ['station', 'date', 'variable', 'reason'], missing)
        raise ValueError(f'发现{len(missing)}条缺测/质量拒收/缺日，详见missing_records.csv；未生成可用站点文件')
    if spec.get('gap_policy', 'error') not in ('error', 'missing'):
        raise ValueError('缺日策略仅支持error或显式missing(-99)')
    for path, station, records in files:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(f"raw conversion; -99 means missing, not observation\nnbyr tstep lat lon elev\n{end.year-start.year+1} 0 {station['lat']} {station['lon']} {station['elev']}\n")
            for date, values in records:
                stream.write(f"{date.year} {date.timetuple().tm_yday} "+' '.join('-99' if v is None else f'{v:.10g}' for v in values)+'\n')
    if missing:
        from raw_tables import write_table
        write_table(output/'missing_records.csv', ['station', 'date', 'variable', 'reason'], missing)
    pending = ['来源格式/质量码尚未核实：'+s.get('format_reference','') for s in sources if s.get('format_verified') is False]
    return {'format': 'plus', 'source_dir': str(output), 'stations': len(stations),
            'days_per_station': (end-start).days+1, 'types': sorted(kinds),
            'missing_values': len(missing), 'missing_examples': missing[:10],
            'source_files': sorted(used_files), 'sources': sources,
            'warnings': pending, 'pending_metadata': pending,
            'ready': not pending, 'note': '缺测若显式允许则写-99，后续由模型发生器处理；没有插值/补零/太阳辐射推算。来源格式待核实时可查看转换文件，但不作为已验收建模输入。'}
