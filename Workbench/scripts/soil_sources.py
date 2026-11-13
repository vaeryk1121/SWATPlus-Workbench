"""HWSD2 spatial unit/component/horizon adaptation to official QSWAT+ tables."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import sqlite3
from raw_tables import MAIN,LAYER,write_table,check_soil_row
from soil_hydraulics import saxton_rawls,epic_usle_k,neh2009_group


def normalize_official_soils(spec,spatial,output):
    ids=list(map(int,spec.get('selected_ids') or spatial['maps']['soil']['classes_in_dem']))
    database=Path(spec['database']).resolve();main=[];layers=[]
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        for identity in ids:
            mapped=c.execute('SELECT SNAM FROM global_soils WHERE SOIL_ID=?',(identity,)).fetchone()
            if not mapped:raise ValueError(f'官方图编号{identity}没有配套global_soils记录')
            records=c.execute('SELECT * FROM global_usersoil WHERE SNAM=?',(mapped[0],)).fetchall()
            if len(records)!=1:raise ValueError(f'{mapped[0]}: 官方配套剖面不唯一/不存在')
            r=dict(records[0]);main.append(dict(zip(MAIN,[identity,r['SNAM'],r['MUID'],r['SEQN'],r['S5ID'],r['CMPPCT'],r['HYDGRP'],r['SOL_ZMX'],r['ANION_EXCL'],r['SOL_CRK'],r['TEXTURE']])))
            for n in range(1,int(r['NLAYERS'])+1):
                prefix=['SOL_Z','SOL_BD','SOL_AWC','SOL_K','SOL_CBN','CLAY','SILT','SAND','ROCK','SOL_ALB','USLE_K','SOL_EC','SOL_CAL','SOL_PH']
                values=[r.get(k+str(n)) for k in prefix]
                if values[-1] in (0,'0'):values[-1]=None  # official legacy placeholder, not measured pH
                layers.append(dict(zip(LAYER,[len(layers)+1,identity,n,*values])))
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    write_table(output/'usersoil.csv',MAIN,main);write_table(output/'usersoil_layer.csv',LAYER,layers)
    write_table(output/'soil_lookup.csv',['SOIL_ID','SNAM'],[{'SOIL_ID':r['id'],'SNAM':r['name']} for r in main])
    return {'ready':True,'usersoil':str(output/'usersoil.csv'),'usersoil_layer':str(output/'usersoil_layer.csv'),'usersoil_format':'separate',
            'soil_lookup':str(output/'soil_lookup.csv'),'profiles':len(main),'layers':len(layers),'source':spec,
            'note':'仅适用于官方配套全球土壤图；保留官方属性，旧pH=0按未知留空，不当作实测。旧数据精度和时期限制仍需评估。'}


def read_hwsd2(database,ids):
    """Jet reads a temporary copy only: no original .ldb/sidecars or edits."""
    ids=sorted({int(i) for i in ids})
    if not ids:raise ValueError('研究区没有可读取的土壤图斑ID')
    with tempfile.TemporaryDirectory(prefix='hwsd2_') as folder:
        folder=Path(folder);copy=folder/'source.mdb';shutil.copy2(database,copy)
        request=folder/'request.json';result=folder/'result.json'
        request.write_text(json.dumps({'database':str(copy),'ids':ids}),encoding='utf-8')
        command=['C:/Windows/SysWOW64/WindowsPowerShell/v1.0/powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(Path(__file__).with_name('read_hwsd2.ps1')),'-Request',str(request),'-Output',str(result)]
        run=subprocess.run(command,capture_output=True,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if run.returncode:raise ValueError('HWSD2只读查询失败：'+run.stderr.decode('utf-8',errors='replace')[-1000:])
        return json.loads(result.read_text(encoding='utf-8-sig'))


def normalize_hwsd2(spec,spatial,output):
    ids=sorted(map(int,spec.get('selected_ids') or spatial['maps']['soil']['classes_in_dem']))
    data=read_hwsd2(spec['database'],ids)
    groups={i:{} for i in ids}
    for row in data['HWSD2_LAYERS']:
        groups[int(row['HWSD2_SMU_ID'])].setdefault(int(row['SEQUENCE']),[]).append(row)
    units={str(r['FIELD']).strip():r['UNIT'] for r in data['HWSD2_LAYERS_METADATA']}
    if units.get('ORG_CARBON')!='% weight':raise ValueError('当前HWSD2有机碳元数据不是已核验的% weight，需另行核对版本')
    options=spec.get('model_assumptions',{})
    required=('anion_excl','sol_crk','alb','root_depth_mm_by_class','water_table_mm','restricting_depth_mm','source_note')
    absent=[k for k in required if k not in options]
    if absent:raise ValueError('请明确模型假设（非实测）：'+','.join(absent))
    if spec.get('hydraulics_method')!='saxton_rawls_2006':raise ValueError('请选择已对照的saxton_rawls_2006方法或使用已有属性库')
    carbon_to_om=float(spec['carbon_to_organic_matter_factor'])
    if carbon_to_om<=0:raise ValueError('OC到OM换算因子须明确且为正数')
    main=[];layers=[];choices=[];estimates=[]
    smu={int(r['HWSD2_SMU_ID']):r for r in data['HWSD2_SMU']}
    for identity,components in groups.items():
        if not components or identity not in smu:raise ValueError(f'HWSD2图斑{identity}缺少配套属性')
        chosen=min(components,key=lambda sequence:(-float(components[sequence][0]['SHARE']),sequence))
        rows=sorted(components[chosen],key=lambda r:float(r['TOPDEP']))
        if len(rows)!=7 or [r['LAYER'] for r in rows]!=['D'+str(i) for i in range(1,8)]:raise ValueError(f'{identity}: 所选组分缺完整七层资料')
        root_class=smu[identity]['ROOT_DEPTH']
        if root_class is None:raise ValueError(f'{identity}: 根深等级缺失，请提供可靠根深')
        root=float(options['root_depth_mm_by_class'][str(int(root_class))])
        if not 0<root<=2000:raise ValueError('根深假设超出该产品剖面范围')
        ranges={1:(1000,2000),2:(500,1000),3:(100,500),4:(0,100)}
        lower,upper=ranges[int(root_class)]
        if not lower<root<=upper:raise ValueError('根深假设与HWSD2根深等级范围不符')
        actual=[]
        for row in rows:
            top,bottom=float(row['TOPDEP'])*10,float(row['BOTDEP'])*10
            values={k:float(row[s]) for k,s in [('bd','BULK'),('carbon','ORG_CARBON'),('clay','CLAY'),('silt','SILT'),('sand','SAND'),('ec','ELEC_COND'),('caco3','TCARBON_EQ'),('ph','PH_WATER')]}
            if any(v<0 for v in values.values()):raise ValueError(f'{identity}: 存在源缺测负值，不能作为参数')
            if abs(values['sand']+values['silt']+values['clay']-100)>2:raise ValueError('HWSD质地比例不闭合，不能默改')
            coarse=float(row['COARSE'])/100
            if not 0<=coarse<1:raise ValueError('粗颗粒体积分数无效')
            fine=saxton_rawls(values['sand'],values['clay'],values['carbon']*carbon_to_om)
            # fine-earth AWC converted to whole-layer storage; rock weight %
            # uses explicit mineral particle density 2.65 and fine bulk density.
            rock=100*coarse*2.65/(coarse*2.65+(1-coarse)*values['bd'])
            layer={'soil_id':identity,'layer_num':len(actual)+1,'top_mm':top,'dp':bottom,**values,
                   'awc':fine['awc']*(1-coarse),'soil_k':fine['soil_k'],'rock':rock,'alb':float(options['alb']),
                   'usle_k':epic_usle_k(values['sand'],values['silt'],values['clay'],values['carbon'])}
            check_soil_row(layer,True);actual.append(layer)
            estimates.append({'soil_id':identity,'layer':layer['layer_num'],'ptf':fine,'coarse_volume_fraction':coarse})
        hyd=neh2009_group(actual,options['restricting_depth_mm'],options['water_table_mm'])
        profile={'id':identity,'name':f'hw2_{identity}','muid':str(identity),'seqn':chosen,'s5id':'','cmppct':rows[0]['SHARE'],
                 'hydgrp':hyd,'sol_zmx':root,'anion_excl':float(options['anion_excl']),'sol_crk':float(options['sol_crk']),
                 'texture':'HWSD2_'+str(rows[0]['TEXTURE_USDA'])}
        check_soil_row(profile);main.append(profile);layers.extend(actual)
        choices.append({'soil_id':identity,'sequence':chosen,'share_percent':rows[0]['SHARE'],'available_components':len(components),'root_class':root_class,'root_depth_assumed_mm':root})
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    for n,row in enumerate(layers,1):row['id']=n
    write_table(output/'usersoil.csv',MAIN,main);write_table(output/'usersoil_layer.csv',LAYER,layers)
    write_table(output/'soil_lookup.csv',['SOIL_ID','SNAM'],[{'SOIL_ID':r['id'],'SNAM':r['name']} for r in main])
    return {'ready':True,'usersoil':str(output/'usersoil.csv'),'usersoil_layer':str(output/'usersoil_layer.csv'),'usersoil_format':'separate',
            'soil_lookup':str(output/'soil_lookup.csv'),'profiles':len(main),'layers':len(layers),'components':choices,'hydraulic_estimates':estimates,
            'source':spec,'model_assumptions':options,'carbon_unit_basis':'当前FAO官方MDB与本地字段字典均为% weight；报告g/kg表述不据此改数值',
            'note':'AWC/Ksat和USLE_K为估算；根深、地下水位/限制层及反照率等为显式情景假设，非当地实测；此实现不等同完整SPAW。'}
