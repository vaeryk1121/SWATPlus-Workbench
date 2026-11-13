"""Explicit source-column/unit conversions. Missing properties remain missing."""
import csv
import math
from pathlib import Path

MAIN = 'id name muid seqn s5id cmppct hydgrp sol_zmx anion_excl sol_crk texture'.split()
LAYER = 'id soil_id layer_num dp bd awc soil_k carbon clay silt sand rock alb usle_k ec caco3 ph'.split()
IDENTIFIERS = {'muid','seqn','s5id','cmppct'}
OPTIONAL_LAYERS = {'caco3','ph'}  # nullable in the official Editor soil layer model
UNITS = {
    'dp': {'mm': 1, 'cm': 10, 'm': 1000},
    'sol_zmx': {'mm': 1, 'cm': 10, 'm': 1000},
    'bd': {'g/cm3': 1, 'kg/dm3': 1, 'kg/m3': .001},
    'awc': {'mm/mm': 1, 'm3/m3': 1, '%': .01},
    'soil_k': {'mm/h': 1, 'cm/h': 10, 'cm/day': 10/24, 'm/day': 1000/24},
    **{k: {'%': 1, 'fraction': 100, 'g/kg': .1} for k in ('carbon', 'clay', 'silt', 'sand', 'rock', 'caco3')},
    **{k: {'fraction': 1, '%': .01} for k in ('alb', 'anion_excl', 'sol_crk')},
    'ec': {'dS/m': 1, 'mS/cm': 1, 'uS/cm': .001},
    'ph': {'pH': 1}, 'usle_k': {'SWAT': 1}, 'cmppct': {'%': 1},
}


def table(spec):
    """CSV/TSV or explicitly selected XLSX sheet/header row, read only."""
    path = Path(spec['path'])
    if path.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = book[spec['sheet']]
            iterator = sheet.iter_rows(min_row=int(spec.get('header_row', 1)), values_only=True)
            header = [str(v).strip() if v is not None else '' for v in next(iterator)]
            selected = [i for i, v in enumerate(header) if v]
            if len({header[i] for i in selected}) != len(selected):
                raise ValueError('源表存在重复列名，请明确选择无歧义的表头')
            return [{header[i]: row[i] for i in selected} for row in iterator if any(v is not None for v in row)]
        finally:
            book.close()
    with path.open(encoding=spec.get('encoding', 'utf-8-sig'), newline='') as stream:
        reader = csv.DictReader(stream, delimiter=spec.get('delimiter', '\t' if path.suffix.lower() == '.tsv' else ','))
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError('原表为空或列名重复')
        return list(reader)


def write_table(path, fields, rows):
    with Path(path).open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)


def mapped(row, mapping, name):
    if name not in mapping:
        return None
    spec = mapping[name]
    if isinstance(spec, str):
        spec = {'column': spec}
    if 'constant' in spec:
        if not spec.get('source_note'):
            raise ValueError(f'{name}: 显式固定属性必须说明来源，不能自动补值')
        value = spec['constant']
    else:
        if spec['column'] not in row:
            raise ValueError(f"找不到原表字段 {spec['column']}")
        value = row[spec['column']]
    if value is None or str(value).strip() in ('', *[str(v) for v in spec.get('missing_codes', [])]):
        return None
    if name in UNITS:
        unit = spec.get('unit')
        if unit not in UNITS[name]:
            raise ValueError(f'{name}: 请明确单位，可选 {list(UNITS[name])}')
        value = float(value) * UNITS[name][unit]
        if not math.isfinite(value):
            raise ValueError(f'{name}: 非有限数值')
    return value


def check_soil_row(row, layer=False):
    for key, value in row.items():
        if value is None:
            continue
        if key in UNITS:
            v = float(value)
            if key in ('dp', 'sol_zmx', 'bd') and v <= 0:
                raise ValueError(f'{key}须大于0')
            if key in ('awc', 'alb', 'anion_excl', 'sol_crk') and not 0 <= v <= 1:
                raise ValueError(f'{key}须在0至1')
            if key in ('carbon', 'clay', 'silt', 'sand', 'rock', 'caco3', 'cmppct') and not 0 <= v <= 100:
                raise ValueError(f'{key}须在0至100%')
            if key == 'ph' and not 0 < v <= 14:
                raise ValueError('土壤pH须大于0且不超过14；0不能当作已知属性')
            if key in ('soil_k', 'usle_k', 'ec') and v < 0:
                raise ValueError(f'{key}不能为负')
    if row.get('hydgrp') is not None and row['hydgrp'] not in ('A', 'B', 'C', 'D'):
        raise ValueError('水文分组须为A/B/C/D')
    if layer and all(row.get(k) is not None for k in ('sand', 'silt', 'clay')):
        if abs(sum(row[k] for k in ('sand', 'silt', 'clay')) - 100) > 2:
            raise ValueError('砂粉粘含量合计偏离100%，请核对单位/来源')


def normalize_soils(spec, output):
    """Long horizon table or explicit wide horizon mappings, not a guessed PTF."""
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    source = table(spec['table'])
    profile_map = spec['profile_fields']; layer_specs = spec['layers']
    profiles = {}; horizons = {}; missing = []; optional_missing=[];excluded_rows=0
    select = {str(v) for v in spec.get('select_ids', [])}
    for record in source:
        identity = mapped(record, profile_map, 'id')
        if identity is None:
            if select:
                excluded_rows+=1;continue
            raise ValueError('土壤必须明确ID字段')
        value=float(identity)
        if not math.isfinite(value) or value!=int(value):raise ValueError('土壤ID须为有限整数')
        identity = int(value)
        if select and str(identity) not in select:
            excluded_rows+=1;continue
        profile = {k: mapped(record, profile_map, k) for k in MAIN if k != 'id'}
        profile['id'] = identity
        if not profile['name']:
            raise ValueError(f'土壤{identity}没有名称')
        if spec.get('append_id_to_name'):
            profile['name'] = f"{profile['name']}_{identity}"
        if identity in profiles and profiles[identity] != profile:
            raise ValueError(f'土壤{identity}存在多个不同组分；请先显式选择SEQ/组分，不能静默取第一行')
        profiles[identity] = profile
        check_soil_row(profile)
        for item in layer_specs:
            horizon = {k: mapped(record, item['fields'], k) for k in LAYER if k not in ('id', 'soil_id', 'layer_num')}
            number = int(mapped(record, item['fields'], 'layer_num') if 'layer_num' in item['fields'] else item['number'])
            horizon.update(soil_id=identity, layer_num=number)
            check_soil_row(horizon, True)
            key = (identity, number)
            if key in horizons:
                raise ValueError(f'土壤{identity}第{number}层重复')
            horizons[key] = horizon
    if not profiles or select - {str(i) for i in profiles}:
        raise ValueError('所选土壤ID在属性表中缺失')
    if len({r['name'] for r in profiles.values()}) != len(profiles):
        raise ValueError('土壤名称重复，应明确使用唯一名称')
    for identity, profile in profiles.items():
        layers = sorted((r for (i, _), r in horizons.items() if i == identity), key=lambda r: r['layer_num'])
        if [r['layer_num'] for r in layers] != list(range(1, len(layers)+1)):
            raise ValueError('土壤层号须从1连续')
        depths = [r['dp'] for r in layers]
        if all(d is not None for d in depths) and any(a >= b for a, b in zip(depths, depths[1:])):
            raise ValueError('土壤层底深度须递增，不能输入单层厚度')
        for row in [profile, *layers]:
            for key, value in row.items():
                if value is None:
                    detail={'soil_id': identity, 'layer': row.get('layer_num'), 'field': key}
                    if key in IDENTIFIERS or key in OPTIONAL_LAYERS:optional_missing.append(detail)
                    else:missing.append(detail)
    main = output/'usersoil.csv'; layer_file = output/'usersoil_layer.csv'
    for n, row in enumerate(horizons.values(), 1):
        row['id'] = n
    write_table(main, MAIN, profiles.values())
    write_table(layer_file, LAYER, horizons.values())
    lookup = output/'soil_lookup.csv'
    write_table(lookup, ['SOIL_ID', 'SNAM'], [{'SOIL_ID': i, 'SNAM': p['name']} for i, p in profiles.items()])
    return {'usersoil': str(main), 'usersoil_layer': str(layer_file), 'soil_lookup': str(lookup),
            'usersoil_format': 'separate', 'profiles': len(profiles), 'layers': len(horizons),
            'missing': missing, 'optional_missing':optional_missing, 'ready': not missing, 'source': spec, 'excluded_by_id_selection':excluded_rows,
            'estimation': '无自动估算；仅按明确字段和单位转换'}
