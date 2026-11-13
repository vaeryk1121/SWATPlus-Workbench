"""One small form for reusable HWSD2/CMA/global-WGN source recipes."""
import json
from pathlib import Path
import tkinter as tk
import desktop_ui as ui
from tkinter import ttk,filedialog,messagebox


def open_product_setup(parent,code_root,on_recipe):
    code_root=Path(code_root);sources=json.loads((code_root/'config/input_sources.json').read_text(encoding='utf-8-sig'))
    from portable_runtime import read_config
    support = read_config().get('support_root')
    if support:
        for name in ('official_soil', 'official_landuse'):
            sources[name]['database'] = str(Path(support)/'Databases/QSWATPlusProj.sqlite')
    win=tk.Toplevel(parent);win.title('通用数据源 · 土壤、气象与WGN');ui.fit_window(win, 1040, 820)
    outer=ttk.Frame(win,padding=14);outer.pack(fill='both',expand=True)
    ttk.Label(outer,text='同一来源换流域只改范围、出口和时段。原始资料保留；最终按SWAT+官方格式导入。',wraplength=900).pack(fill='x')
    source_choice=tk.StringVar(value='官方配套全球土壤（粗分辨率，直接使用配套属性）')
    ttk.Combobox(outer,textvariable=source_choice,state='readonly',values=['官方配套全球土壤（粗分辨率，直接使用配套属性）','HWSD2原始属性＋矿质土参数估算']).pack(fill='x',pady=8)
    footer=ttk.Frame(outer);footer.pack(side='bottom',fill='x',pady=(8,0))
    canvas=tk.Canvas(outer,highlightthickness=0,background='white');scroll=ttk.Scrollbar(outer,command=canvas.yview);canvas.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right',fill='y');canvas.pack(fill='both',expand=True)
    frame=ttk.Frame(canvas);item=canvas.create_window(0,0,window=frame,anchor='nw')
    frame.columnconfigure(1,weight=1);frame.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>',lambda e:canvas.itemconfigure(item,width=e.width))
    ui.attach_wheel(canvas,frame)
    definitions=[('case_root','本案例输出目录','','dir'),('dem','原始DEM（高程米、带坐标系）','','file'),('landuse','土地利用分类栅格','','file'),('landuse_csv','与该图配套的土地利用对应CSV','','file'),
                 ('lon','出口经度（WGS84）','',''),('lat','出口纬度（WGS84）','',''),('crs','目标米制投影EPSG（留空按出口建议UTM）','',''),('resolution','DEM处理分辨率（米）','30',''),
                 ('cma_root','CMA V3全国月文件根目录','','dir'),('station_ids','指定站号（逗号分隔，空则推荐最近站）','',''),('start','模拟起始年（含预热）','',''),('end','模拟结束年','',''),('warmup','预热年数','0',''),
                 ('hwsd_database','HWSD2原始MDB',sources['hwsd2']['database'],'file'),('hwsd_raster','与该库配套的全球土壤图',sources['hwsd2']['raster'],'file'),('wgn_database','官方全球WGN数据库',str(code_root/sources['global_wgn']['database']),'file'),
                 ('root_depths','HWSD2根深等级1—4的情景值（mm）','1500,750,300,75',''),('water_table','HWSD2地下水位深度假设（mm）','2000',''),('restriction','HWSD2限制层深度假设（mm）','2000','')]
    values={}
    for row,(key,label,default,browse) in enumerate(definitions):
        ttk.Label(frame,text=label).grid(row=row,column=0,sticky='w',pady=5)
        values[key]=tk.StringVar(value=default);ttk.Entry(frame,textvariable=values[key]).grid(row=row,column=1,sticky='ew',padx=8)
        if browse:
            def choose(k=key,mode=browse):
                value=filedialog.askdirectory(parent=win) if mode=='dir' else filedialog.askopenfilename(parent=win)
                if value:values[k].set(value)
            ttk.Button(frame,text='浏览…',command=choose).grid(row=row,column=2)
    note='参数方案：矿质土Saxton–Rawls估算AWC/Ksat，NEH2009水文组；OC→OM因子1/0.58。阴离子排除0.5、裂隙0.01、反照率0.1采用显式模型假设。根深/水位/限制层为情景值，不是实测，可在来源方案中修改。CMA微量雨按0近似；辐射以FAO56默认系数0.25/0.50推算。'
    ttk.Label(outer,text=note,wraplength=900,justify='left').pack(fill='x',pady=8)
    ttk.Label(outer,text='上述土壤估算仅用于HWSD2路线。官方配套路线不混接HWSD2编号；图约0.0675度，适合流程试验，不代表精细研究最佳数据。HWSD2中的城市等无剖面类别会报缺项，不自动插补。',wraplength=900).pack(fill='x')
    gap=tk.StringVar(value='缺测保留-99，由WGN生成（会记录）')
    ttk.Combobox(outer,textvariable=gap,state='readonly',values=['缺测保留-99，由WGN生成（会记录）','有缺测就停止']).pack(fill='x',pady=5)
    def create():
        try:
            v={k:x.get().strip() for k,x in values.items()}
            lon,lat=float(v['lon']),float(v['lat']);start,end,warmup=map(int,(v['start'],v['end'],v['warmup']))
            if not -180<=lon<=180 or not -80<=lat<=84 or not 0<=warmup<=end-start:raise ValueError('请核对出口坐标与模拟/预热年数')
            epsg=v['crs'] or str((32600 if lat>=0 else 32700)+min(60,int((lon+180)//6)+1))
            if not epsg.upper().startswith('EPSG:'):epsg='EPSG:'+epsg
            from input_data import lookup_rows
            if v['landuse'] and v['landuse_csv']:
                mapping={str(k):name for k,name in lookup_rows(v['landuse_csv'],'landuse').items()}
            elif not v['landuse'] and not v['landuse_csv']:
                import sqlite3
                with sqlite3.connect(Path(sources['official_landuse']['database']).as_uri()+'?mode=ro',uri=True) as c:
                    mapping={str(k):name for k,name in c.execute('SELECT LANDUSE_ID,SWAT_CODE FROM global_landuses')}
                v['landuse']=str(code_root/sources['official_landuse']['raster'])
            else:raise ValueError('土地利用图和配套对应表须一起提供；都留空时使用官方GLCC旧资料')
            depths=[float(s) for s in v['root_depths'].split(',')]
            if len(depths)!=4:raise ValueError('请提供四个根深等级情景值')
            spec={'case_root':v['case_root'],'title':'通用原始资料建模','simulation':{'start_year':start,'end_year':end,'warmup_years':warmup},
                  'spatial':{'dem':v['dem'],'target_crs':epsg,'resolution_m':float(v['resolution']),'dem_resampling':'bilinear',
                             'landuse':{'path':v['landuse'],'mapping':mapping,'product':'用户提供的配套分类图和对应表','year':'unknown','mapping_note':'用户选择配套CSV；年代/地表管理仍需核对','unknown_policy':'error'},
                             'soil':{'path':v['hwsd_raster'],'auto_hwsd2_names':True},'outlet':{'x':lon,'y':lat,'crs':'EPSG:4326'}},
                  'soil':{'product':'hwsd2','selection':'delineated_catchment','database':v['hwsd_database'],'hydraulics_method':'saxton_rawls_2006','carbon_to_organic_matter_factor':1/.58,
                          'model_assumptions':{'root_depth_mm_by_class':{str(i):d for i,d in enumerate(depths,1)},'water_table_mm':float(v['water_table']),'restricting_depth_mm':float(v['restriction']),'anion_excl':.5,'sol_crk':.01,'alb':.1,'source_note':note}},
                  'weather':{'product':'cma_daily_v3','root':v['cma_root'],'start':f'{start}-01-01','end':f'{end}-12-31','locations':[{'lon':lon,'lat':lat}],
                             'accepted_quality_codes':[0],'gap_policy':'error','trace_precipitation':'zero','solar':{'method':'fao56_angstrom','a':.25,'b':.5,'source_note':'FAO56默认系数推算，非辐射实测'}},
                  'wgn':{'product':'swatplus_global','database':v['wgn_database'],'table':sources['global_wgn']['table'],'source_note':sources['global_wgn']['source_note']}}
            if v['station_ids']:spec['weather']['station_ids']=[s.strip() for s in v['station_ids'].split(',')]
            spec['weather']['gap_policy']='missing' if gap.get().startswith('缺测保留') else 'error'
            if source_choice.get().startswith('官方'):
                database=sources['official_soil']['database']
                spec['soil']={'product':'official_global','database':database,'selection':'delineated_catchment'}
                spec['spatial']['soil']={'path':str(code_root/sources['official_soil']['raster']),'official_database':database}
            on_recipe(spec);win.destroy()
        except Exception as e:messagebox.showerror('请核对资料',str(e),parent=win)
    ttk.Button(footer,text='生成来源方案，再预览/转换',style='Primary.TButton',command=create).pack(side='right',pady=7)
    return win
