"""Small configuration wizard for supported spatial inputs or an existing clean export."""
import copy
import json
from pathlib import Path
import re
import tkinter as tk
import desktop_ui as ui
from tkinter import ttk, filedialog, messagebox


def save_setup(root, name, cfg, spatial=None, editor=None):
    root=Path(root)
    code_root=root
    if cfg.get('workspace_root'):root=Path(cfg['workspace_root']).resolve()
    (root/'config').mkdir(parents=True,exist_ok=True)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,20}",name):raise ValueError("配置名称须为字母开头的1至21位英文、数字或下划线")
    cfg=copy.deepcopy(cfg);cfg["workflow_mode"]="toolbox"
    cfg.pop("search",None);cfg.pop("analysis",None);cfg.pop("observation",None)
    files={root/"config"/(name+".json"):cfg}
    if cfg.get("build",{}).get("enabled"):
        if spatial is None or editor is None:raise ValueError("缺少空间或气象配置")
        from build_preflight import check_paths
        check_paths(spatial,editor,root,cfg["simulation"])
        spatial=copy.deepcopy(spatial);editor=copy.deepcopy(editor)
        spatial["align_categorical_to_dem"]=True;editor["simulation"].update(cfg["simulation"])
        from build_and_run import load_pipeline
        _,pipeline,_=load_pipeline(code_root/"config/standard_pipeline.json")
        if cfg.get('workspace_root'):
            pipeline.update(workspace_root=str(root),runtime_config=cfg['runtime_config'],model_config=str(code_root/'config/standard_model.json'))
        pipeline.update(preflight=True,spatial_config=f"config/{name}_spatial.json",editor_config=f"config/{name}_editor.json")
        cfg["build"]["pipeline_config"]=f"config/{name}_pipeline.json"
        files.update({root/"config"/(name+"_pipeline.json"):pipeline,root/"config"/(name+"_spatial.json"):spatial,root/"config"/(name+"_editor.json"):editor})
    else:
        from swatplus_trial import check_clean_source
        check_clean_source(root/cfg["model"]["input_dir"],"62.0.1",cfg["model"].get("calibration_policy","reject"))
    for path in files:
        if path.exists():raise FileExistsError(f"配置已存在，请换名称：{path.name}")
    from model_workflow import validate_model_config
    validate_model_config(cfg,root,check_inputs=False)
    for path,data in files.items():path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return root/"config"/(name+".json")


class ModelSetup:
    """Reusable form: embedded on the data page, or opened by legacy callers."""
    def __init__(self,parent,root,on_save,config_path=None,embedded=False,blank=False):
        self.root=Path(root);self.on_save=on_save;self.embedded=embedded;self.busy=False
        source=Path(config_path) if config_path else self.root/"config/standard_workflow.json"
        self.cfg=json.loads(source.read_text(encoding="utf-8-sig"))
        pipeline=self.cfg.get("build",{}).get("pipeline_config") or "config/standard_pipeline.json"
        from build_and_run import load_pipeline
        _,_,stages=load_pipeline(self.root/pipeline)
        self.spatial,self.editor=stages['spatial'],stages['editor']
        if embedded:self.window=ttk.Frame(parent)
        else:
            self.window=tk.Toplevel(parent);self.window.title("资料准备 · 另存方案");self.window.transient(parent)
            ui.fit_window(self.window, 1080, 820)
        self.vars={};self.widgets={};self.browse_buttons=[]
        self.field_labels=[];self.canvases=[];self._column_width=None
        self._tab_style=f"InputTabs{id(self)}.TNotebook"
        self._tab_timer=None
        self.notebook=ttk.Notebook(self.window,style=self._tab_style)
        self.notebook.pack(fill="both",expand=True,padx=12,pady=(8,5))
        self.notebook.bind('<<NotebookTabChanged>>',self.animate_tab_selection)
        self.notebook.bind('<Destroy>',lambda e:self.cancel_tab_transition() if e.widget==self.notebook else None)
        self.pages=[]
        for title in ('空间资料','土地与土壤表','气象资料','建模与时间','参考资料（可选）'):
            host=ttk.Frame(self.notebook);self.notebook.add(host,text=title)
            canvas=tk.Canvas(host,highlightthickness=0,bg='white');scroll=ttk.Scrollbar(host,orient='vertical',command=canvas.yview)
            canvas.configure(yscrollcommand=scroll.set);scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
            frame=ttk.Frame(canvas,padding=16);item=canvas.create_window(0,0,window=frame,anchor='nw')
            frame.bind('<Configure>',lambda e,c=canvas:c.configure(scrollregion=c.bbox('all')))
            self.canvases.append(canvas)
            ui.attach_wheel(canvas, frame)
            def resize(e,c=canvas,i=item):
                c.itemconfigure(i,width=e.width)
                self.align_fields(e.width)
            canvas.bind('<Configure>',resize)
            frame.columnconfigure(1,weight=1);self.pages.append(frame)
        self.field(0,'name','方案名称（英文/数字/下划线）', '' if blank else source.stem+'_copy')
        self.field(0,'build','模型来源','空间资料自动建模' if self.cfg.get('build',{}).get('enabled') else '接回已有模型（Editor导出）',choices=['空间资料自动建模','接回已有模型（Editor导出）'])
        self.field(0,'input_dir','已有模型目录', '' if blank else self.cfg.get('model',{}).get('input_dir',''),directory=True)
        self.note(0,'两种输入源：空间资料自动建模=从DEM/土地/土壤/气象重新构建；接回已有模型=直接使用官方Editor导出的TxtInOut（含管理日程、决策表、天气与率定设置），只复制不重建，管理设置不会被覆盖。原始文件保留不变。')
        raw=self.root/self.spatial['raw_data']
        for key,label in [('dem','DEM栅格'),('landuse','土地利用栅格'),('soil','土壤分类栅格'),('outlet','出口Shapefile')]:self.field(0,key,label,'' if blank else str(raw/self.spatial[key]),file=True)
        self.field(0,'burn_stream_file','参考河网Shapefile（可选）','' if blank or not self.spatial.get('burn_streams') else str(raw/self.spatial['burn_stream_file']),file=True)
        self.note(0,'DEM使用合适的米制投影。分类栅格最近邻对齐、出口坐标转换在新模型的项目副本中执行；本页检查不会修改原件。')
        for key,label in [('landuse_lookup','土地利用对应CSV'),('soil_lookup','土壤对应CSV')]:self.field(1,key,label,'' if blank else str(raw/self.spatial[key]),file=True)
        self.field(1,'usersoil_format','土壤属性格式','主表＋分层表' if self.spatial.get('usersoil_format')=='separate' else 'SWAT2012宽表',choices=['SWAT2012宽表','主表＋分层表'])
        self.field(1,'usersoil','土壤属性/主表CSV','' if blank else str(raw/self.spatial['usersoil']),file=True)
        self.field(1,'usersoil_layer','土壤分层CSV','' if blank or not self.spatial.get('usersoil_layer') else str(raw/self.spatial['usersoil_layer']),file=True)
        self.field(1,'plant_csv','自定义植物表（可选）','' if blank else str(raw/self.spatial['plant_csv']) if self.spatial.get('plant_csv') else '',file=True)
        self.field(1,'urban_csv','自定义城镇表（可选）','' if blank else str(raw/self.spatial['urban_csv']) if self.spatial.get('urban_csv') else '',file=True)
        ttk.Button(self.pages[1],text='将所选宽表另存为主表＋分层表',command=self.split_soil).grid(row=self.pages[1].grid_size()[1],column=1,sticky='w',pady=7)
        self.note(1,'植物/城镇表留空时使用随官方QSWAT+项目模板复制的表。土壤可用152列宽表，或11列主表＋17列分层表；导入仍由官方QSWAT+处理。')
        weather=self.editor['weather']
        self.field(2,'weather_format','日气象格式','SWAT+原生' if weather.get('format','2012')=='plus' else 'SWAT2012',choices=['SWAT+原生','SWAT2012'])
        self.field(2,'source_dir','气象资料目录','' if blank else str(self.root/weather['source_dir']),directory=True)
        self.field(2,'wgn_method','天气发生器来源','WGN数据库' if weather.get('wgn_method')=='database' else 'CSV两文件',choices=['CSV两文件','WGN数据库'])
        for key,label in [('wgn_station_csv','WGN站点CSV'),('wgn_monthly_csv','WGN月参数CSV'),('wgn_database','WGN数据库')]:self.field(2,key,label,'' if blank or not weather.get(key) else str(self.root/weather[key]),file=True)
        self.field(2,'wgn_table','WGN站点表名','' if blank else weather.get('wgn_table',''))
        self.field(2,'allow_generated','缺失气象类型处理','允许WGN生成' if weather.get('allow_generated') else '必须提供五类日气象',choices=['必须提供五类日气象','允许WGN生成'])
        self.note(2,'SWAT+原生目录使用pcp.cli/tmp.cli等索引和站点日文件；SWAT2012使用pcp.txt/tmp.txt等站点目录。由Editor原生接口导入或转换。允许WGN生成是显式建模选择，不能将生成量当成实测。暂不在本页导入NetCDF。')
        for key,label in [('stream_threshold_cells','河网阈值（像元）'),('channel_threshold_cells','河道阈值（像元）'),('snap_threshold_m','出口吸附距离（米）'),('hru_minimum_area_ha','HRU最小面积（公顷）')]:self.field(3,key,label,str(self.spatial[key]))
        self.field(3,'slope_limits_percent','坡度分界（%，逗号分隔）',','.join(map(str,self.spatial['slope_limits_percent'])))
        for key,label in [('start_year','模拟起始年'),('end_year','模拟结束年'),('warmup_years','预热年数')]:self.field(3,key,label,'' if blank and key!='warmup_years' else '3' if blank else str(self.cfg['simulation'][key]))
        self.field(3,'frequency','输出频率',self.cfg['frequency'],choices=['monthly','daily'])
        self.field(3,'print_policy','输出策略','最小输出（旧测试行为）' if self.cfg.get('model',{}).get('print_policy')=='reset' else '保留原输出并补充所需',choices=['保留原输出并补充所需','最小输出（旧测试行为）'])
        self.field(3,'calibration_policy','已启用率定文件处理','按原样运行（不叠加参数搜索）' if self.cfg.get('model',{}).get('calibration_policy')=='carry' else '按干净输入拒绝',choices=['按干净输入拒绝','按原样运行（不叠加参数搜索）'])
        outlet=self.cfg.get('model',{}).get('outlet','auto')
        if outlet=='auto' or not isinstance(outlet,dict):
            outlet_object,outlet_unit,outlet_gis='自动（唯一末端）','',''
        else:
            outlet_object='水库对象（unit/gis）' if outlet.get('object')=='reservoir' else '河道站点（unit/gis）'
            outlet_unit,outlet_gis=str(outlet.get('unit','')),str(outlet.get('gis_id',''))
        self.field(3,'outlet_mode','基础运行评价对象',outlet_object,choices=['自动（唯一末端）','河道站点（unit/gis）','水库对象（unit/gis）'])
        self.field(3,'outlet_unit','评价对象unit编号',outlet_unit)
        self.field(3,'outlet_gis_id','评价对象GIS编号',outlet_gis)
        for name,label in [('calibration','率定'),('validation','验证')]:
            for edge,caption in [('start','开始'),('end','结束')]:self.field(3,name+'_'+edge,label+caption+'（可留空）','' if blank else self.cfg.get('periods',{}).get(name,{}).get(edge,''))
        self.note(3,'时段填写YYYY-MM-DD；只有建模时率定/验证时间可留空。气象须覆盖含预热的整个模拟期。阈值来自当前方案，需要按实际流域确认。输出策略默认保留来源模型print.prt原有设置（含作物产量等非目标输出），仅自动补充评价所需的河道/流域输出；“最小输出”为旧测试行为。接回的模型若启用了calibration.cal，默认拒绝以防参数搜索静默覆盖，可选“按原样运行”保留其率定改动且不再叠加参数改动。“基础运行评价对象”是基础运行与出口序列使用的对象（多出口/水库末端/内部站点时必须显式指定，保存时会对照chandeg.con/reservoir.con校验unit与GIS编号并补全名称）；各观测序列的站点在“参考资料”页单独设置，两者互不替代。')
        obs=(self.cfg.get('observations') or [{}])[0];self.obs=copy.deepcopy(obs)
        self.field(4,'obs_path','参考CSV（可留空）','' if blank or not obs.get('path') else str(self.root/obs['path']),file=True)
        for key,label,default in [('date_column','日期列','Date'),('value_column','数值列','Flow'),('date_format','日期格式','%Y/%m')]:self.field(4,key,label,default if blank else obs.get(key,default))
        self.field(4,'kind','资料类型','unverified' if blank else obs.get('kind','unverified'),choices=['unverified','verified_observation','official_tutorial_reference'])
        target=obs.get('target',{})
        self.field(4,'unit_id','河道unit编号（auto为出口）','auto' if blank else str(target.get('unit_id','auto')))
        self.field(4,'gis_id','河道GIS编号','1' if blank else str(target.get('gis_id',1)))
        self.note(4,'无参考资料可以先建模。本入口为日/月平均流量m³/s；观测目标auto只适用于观测站即基础运行评价对象（在“建模与时间”页设置，多出口/水库末端/内部站点须在那里显式指定）。观测unit须另行填写实际河道unit时在本页设置；其他变量和多站可用高级配置。')
        self.vars['build'].trace_add('write',self.update_fields)
        for key in ('usersoil_format','wgn_method'):self.vars[key].trace_add('write',self.update_fields)
        self.message=tk.StringVar(value='先填写资料，再检查或保存；不会启动建模和率定。')
        footer=ttk.Frame(self.window,padding=(12,8));footer.pack(fill='x')
        from tkinter import font as tkfont
        label_font=ttk.Style(self.window).lookup('TLabel','font') or 'TkDefaultFont'
        status=ttk.Frame(footer,height=tkfont.Font(font=label_font).metrics('linespace')*2+8)
        status.pack(fill='x',pady=(0,7));status.pack_propagate(False)
        self.message_label=ttk.Label(status,textvariable=self.message,wraplength=700,anchor='nw',justify='left')
        self.message_label.pack(fill='both',expand=True)
        status.bind('<Configure>',lambda e:self.message_label.configure(wraplength=max(160,e.width)))
        buttons=ttk.Frame(footer);buttons.pack(fill='x')
        self.check_button=ttk.Button(buttons,text='检查并整理资料',command=self.check);self.check_button.pack(side='left')
        self.save_button=ttk.Button(buttons,text='另存方案，进入建模 →',command=self.save);self.save_button.pack(side='right')
        self._label_width=max(label.winfo_reqwidth() for label in self.field_labels)
        self.align_fields(900)
        self.update_fields()

    def cancel_tab_transition(self):
        if self._tab_timer:
            try:self.window.winfo_toplevel().after_cancel(self._tab_timer)
            except tk.TclError:pass
            self._tab_timer=None

    def animate_tab_selection(self,event=None):
        """Fade selection colors briefly; preserve native focus and pane geometry."""
        self.cancel_tab_transition()
        import time
        started=time.perf_counter();style=ttk.Style(self.window)
        def mix(first,last,fraction):
            a=[int(first[i:i+2],16) for i in (1,3,5)];b=[int(last[i:i+2],16) for i in (1,3,5)]
            return '#'+''.join(f'{round(x+(y-x)*fraction):02x}' for x,y in zip(a,b))
        def paint():
            self._tab_timer=None
            if not self.notebook.winfo_exists():return
            progress=min(1.,(time.perf_counter()-started)/.14) if self.notebook.winfo_ismapped() else 1.
            eased=1-(1-progress)**3
            style.map(self._tab_style+'.Tab',
                      background=[('selected',mix('#DDEBFC','#E9F1FD',eased)),('active','#EDF4FD'),('!selected','#DDEBFC')],
                      foreground=[('selected',mix('#566F90','#236BC4',eased)),('!selected','#566F90')])
            if progress<1:self._tab_timer=self.window.winfo_toplevel().after(16,paint)
        paint()

    def align_fields(self,width):
        if not hasattr(self,'_label_width'):return
        shared=min(self._label_width+16,max(170,int((width-32)*.38)))
        if shared==self._column_width:return
        self._column_width=shared
        for frame in self.pages:frame.columnconfigure(0,minsize=shared)
        for label in self.field_labels:label.configure(wraplength=shared-16,justify='left')
        row_height=max(widget.winfo_reqheight() for widget in self.widgets.values())+12
        for page,key in getattr(self,'_field_rows',[]):self.pages[page].rowconfigure(key,minsize=row_height)

    def note(self,page,text):
        frame=self.pages[page];row=frame.grid_size()[1]
        label=ttk.Label(frame,text=text,wraplength=760,justify='left');label.grid(row=row,column=0,columnspan=3,sticky='ew',pady=(9,12))
        frame.bind('<Configure>',lambda e,l=label:l.configure(wraplength=max(220,e.width-36)),add='+')

    def field(self,page,key,label,value,file=False,directory=False,choices=None):
        frame=self.pages[page];row=frame.grid_size()[1];v=tk.StringVar(value=value);self.vars[key]=v
        caption=ttk.Label(frame,text=label,anchor='w');caption.grid(row=row,column=0,sticky='w',padx=(0,16),pady=8)
        self.field_labels.append(caption)
        if not hasattr(self,'_field_rows'):self._field_rows=[]
        self._field_rows.append((page,row))
        widget=ttk.Combobox(frame,textvariable=v,values=choices,state='readonly') if choices else ttk.Entry(frame,textvariable=v)
        widget.grid(row=row,column=1,sticky='ew',pady=8);self.widgets[key]=widget
        if file or directory:
            def choose():
                path=filedialog.askdirectory(parent=self.window) if directory else filedialog.askopenfilename(parent=self.window)
                if path:v.set(path)
            button=ttk.Button(frame,text='浏览…',command=choose);button.grid(row=row,column=2,padx=6);self.browse_buttons.append((key,button))

    def update_fields(self,*unused):
        build=self.vars['build'].get()=='空间资料自动建模';enabled={key:True for key in self.widgets}
        enabled['input_dir']=not build
        enabled['calibration_policy']=not build
        for key in ('outlet_mode','outlet_unit','outlet_gis_id'):enabled[key]=not build
        spatial=('burn_stream_file','dem','landuse','soil','outlet','landuse_lookup','soil_lookup','usersoil_format','usersoil','usersoil_layer','plant_csv','urban_csv','weather_format','source_dir','wgn_method','wgn_station_csv','wgn_monthly_csv','wgn_database','wgn_table','allow_generated')
        for key in spatial:enabled[key]=build
        enabled['usersoil_layer']=build and self.vars['usersoil_format'].get()=='主表＋分层表'
        database=self.vars['wgn_method'].get()=='WGN数据库'
        for key in ('wgn_station_csv','wgn_monthly_csv'):enabled[key]=build and not database
        for key in ('wgn_database','wgn_table'):enabled[key]=build and database
        for key,widget in self.widgets.items():widget.configure(state='disabled' if self.busy or not enabled[key] else 'readonly' if isinstance(widget,ttk.Combobox) else 'normal')
        for key,button in self.browse_buttons:button.configure(state='disabled' if self.busy or not enabled[key] else 'normal')

    def compose(self):
        v={k:x.get().strip() for k,x in self.vars.items()};cfg=copy.deepcopy(self.cfg)
        cfg['title']=v['name']+'：建模与Toolbox交接';cfg['build']['enabled']=v['build']=='空间资料自动建模'
        cfg['model']['input_dir']=v['input_dir'];cfg['workflow_mode']='toolbox'
        cfg['model']['print_policy']='preserve' if v['print_policy'].startswith('保留') else 'reset'
        cfg['model']['calibration_policy']='carry' if v['calibration_policy'].startswith('按原样') else 'reject'
        if cfg['build']['enabled'] or v['outlet_mode'].startswith('自动'):
            cfg['model']['outlet']='auto'
        else:
            try:unit,gis=int(v['outlet_unit']),int(v['outlet_gis_id'])
            except ValueError:raise ValueError('基础运行评价对象：unit与GIS编号须为整数')
            cfg['model']['outlet']={'object':'reservoir' if v['outlet_mode'].startswith('水库') else 'channel_sd',
                                    'unit':unit,'gis_id':gis}
            # Validate against the actual export now, so a saved task never
            # fails later with the generic multi-outlet auto error.
            from swatplus_workflow import identify_outlet
            cfg['model']['outlet']=identify_outlet(self.root/cfg['model']['input_dir'],cfg['model']['outlet'])
        for key in ('start_year','end_year','warmup_years'):
            try:cfg['simulation'][key]=int(v[key])
            except ValueError:raise ValueError('请在“建模与时间”中填写起始年、结束年与预热年数（整数）')
        cfg['frequency']=v['frequency'];cfg['periods']={}
        import datetime as dt
        for name in ('calibration','validation'):
            a,b=v[name+'_start'],v[name+'_end']
            if a or b:
                try:dt.date.fromisoformat(a);dt.date.fromisoformat(b)
                except ValueError:raise ValueError('率定/验证日期请填写YYYY-MM-DD，或两项一起留空')
                cfg['periods'][name]={'start':a,'end':b}
        cfg['observations']=[]
        if v['obs_path']:
            obs=copy.deepcopy(self.obs);obs.update(id='outlet_flow',path=v['obs_path'],date_column=v['date_column'],value_column=v['value_column'],date_format=v['date_format'],frequency=v['frequency'],unit='m3/s',kind=v['kind'],metric='NSE',weight=1)
            obs.pop('original_path',None)
            if str((self.root/self.obs.get('path','')).resolve())!=str(Path(v['obs_path']).resolve()):
                for key in ('source_url','provenance_note','label'):obs.pop(key,None)
                if obs['kind']=='official_tutorial_reference':obs['kind']='unverified'
            obs['target']={'object':'channel_sd','variable':'flo_out','unit':'m3/s','unit_id':'auto' if v['unit_id']=='auto' else int(v['unit_id']),'gis_id':int(v['gis_id'])}
            cfg['observations']=[obs]
        sp=copy.deepcopy(self.spatial);ed=copy.deepcopy(self.editor)
        if cfg['build']['enabled']:
            sp['raw_data']=str(self.root);sp['burn_streams']=bool(v['burn_stream_file']);sp['burn_stream_file']=v['burn_stream_file']
            for key in ('dem','landuse','soil','outlet','landuse_lookup','soil_lookup','usersoil','usersoil_layer'):sp[key]=v[key]
            sp['usersoil_format']='separate' if v['usersoil_format']=='主表＋分层表' else 'wide'
            for key in ('plant_csv','urban_csv'):sp[key]=v[key] or None
            for key in ('stream_threshold_cells','channel_threshold_cells'):sp[key]=int(v[key])
            for key in ('snap_threshold_m','hru_minimum_area_ha'):sp[key]=float(v[key])
            sp['slope_limits_percent']=[float(x) for x in v['slope_limits_percent'].split(',') if x.strip()]
            ed['simulation'].update(cfg['simulation'])
            for key in ('source_dir','wgn_station_csv','wgn_monthly_csv','wgn_database','wgn_table'):ed['weather'][key]=v[key]
            ed['weather'].update(format='plus' if v['weather_format']=='SWAT+原生' else '2012',wgn_method='database' if v['wgn_method']=='WGN数据库' else 'two_file',allow_generated=v['allow_generated']=='允许WGN生成')
        from model_workflow import validate_model_config
        validate_model_config(cfg,self.root,check_inputs=False)
        return v['name'],cfg,sp,ed

    def split_soil(self):
        if self.busy:return
        try:
            from input_data import split_wide_soils
            import datetime
            target=Path(self.cfg.get('workspace_root',self.root))/'data/processed'/('soil_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
            main,layers=split_wide_soils(self.vars['usersoil'].get(),target)
            self.vars['usersoil'].set(main);self.vars['usersoil_layer'].set(layers);self.vars['usersoil_format'].set('主表＋分层表')
            self.message.set('已按官方表结构拆分，原件和各层数值未改：'+str(target))
        except Exception as error:messagebox.showerror('土壤表未转换',str(error),parent=self.window)

    def save(self):
        if self.busy:return
        try:
            name,cfg,sp,ed=self.compose();path=save_setup(self.root,name,cfg,sp,ed)
            self.message.set('已保存：'+str(path));self.on_save(path)
            if not self.embedded:self.window.destroy()
        except Exception as error:messagebox.showerror('资料方案未保存',str(error),parent=self.window)

    def check(self):
        if self.busy:return
        try:
            name,cfg,sp,ed=self.compose()
            data_root=Path(cfg.get('workspace_root',self.root))
        except Exception as error:messagebox.showerror('请补充资料',str(error),parent=self.window);return
        import queue,threading
        self.busy=True;self.update_fields();self.check_button.state(['disabled']);self.save_button.state(['disabled'])
        self.message.set('正在检查和整理资料；不启动模型…');events=queue.Queue()
        def work():
            try:
                import subprocess,datetime,os
                output=data_root/'runs/input_checks'/datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f');output.mkdir(parents=True)
                if cfg['build']['enabled']:
                    from build_preflight import check_paths
                    from input_data import normalize_lookups
                    check_paths(sp,ed,data_root,cfg['simulation'])
                    prepared=normalize_lookups(sp,data_root,data_root/'data/processed'/('inputs_'+output.name)/'lookup_tables')
                    for key,data in [('spatial',prepared),('editor',ed)]: (output/(key+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
                    runtime=json.loads((self.root/cfg.get('runtime_config','config/qgis344_runtime.json')).read_text(encoding='utf-8-sig'))
                    command=[str(Path(runtime['qgis_root'])/'bin/python-qgis-ltr.bat'),'-X','utf8',str(self.root/'scripts/build_preflight.py'),'--spatial',str(output/'spatial.json'),'--editor',str(output/'editor.json'),'--root',str(data_root),'--output',str(output/'input_check.json')]
                    env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1'
                    result=subprocess.run(command,cwd=self.root,env=env,capture_output=True,timeout=120,shell=os.name=='nt',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    (output/'check.log').write_bytes(result.stdout+result.stderr)
                    if result.returncode:raise ValueError('空间检查未通过，请查看 '+str(output/'check.log')+'\n'+(result.stderr or result.stdout).decode('utf-8',errors='replace')[-900:])
                    events.put(('ok',prepared,str(output)))
                else:
                    from swatplus_trial import check_clean_source
                    report=check_clean_source(self.root/cfg['model']['input_dir'],'62.0.1',cfg['model'].get('calibration_policy','reject'))
                    (output/'input_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');events.put(('ok',None,str(output)))
            except Exception as error:events.put(('error',str(error)))
        def poll():
            try:event=events.get_nowait()
            except queue.Empty:self.window.after(100,poll);return
            self.busy=False;self.update_fields();self.check_button.state(['!disabled']);self.save_button.state(['!disabled'])
            if event[0]=='ok':
                if event[1]:
                    for key in ('landuse_lookup','soil_lookup'):self.vars[key].set(event[1][key])
                self.message.set(('检查完成；对应表已另存整理，原件未改。报告：' if event[1] else '已有模型检查完成，未运行模型。报告：')+event[2])
            else:
                self.message.set('检查未通过：'+event[1]);messagebox.showerror('资料检查未通过',event[1],parent=self.window)
        threading.Thread(target=work,daemon=True).start();self.window.after(100,poll)
