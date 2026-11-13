"""The first workbench step: prepare inputs, then save a model task configuration."""
import json
from pathlib import Path
import subprocess
import tkinter as tk
import desktop_ui as ui
from tkinter import ttk,filedialog,messagebox
from desktop_model_setup import ModelSetup


class InputPage(ttk.Frame):
    def __init__(self,parent,root,get_config,on_save,status):
        super().__init__(parent)
        self.root=Path(root);self.get_config=get_config;self.on_save=on_save;self.status=status;self.form=None
        bar=ttk.Frame(self,padding=(12,7));bar.pack(fill='x')
        ttk.Label(bar,text='第一步：填写资料 → 检查整理 → 另存方案',font=('Microsoft YaHei UI',11,'bold')).pack(side='left')
        ttk.Button(bar,text='打开官方Editor ↗',command=self.open_editor).pack(side='right')
        source=ttk.Frame(self,padding=(12,5));source.pack(fill='x')
        ttk.Button(source,text='新建流域资料',command=lambda:self.load(None,True)).pack(side='left')
        ttk.Button(source,text='原始资料转换…',command=self.open_raw).pack(side='left',padx=7)
        ttk.Button(source,text='读取已有方案…',command=self.choose).pack(side='left',padx=7)
        example = self.root / 'config/demo_existing_model.json'
        if not example.is_file():
            example = self.root / 'config/robit_toolbox.json'
        ttk.Button(source,text='载入教学示例',command=lambda:self.load(example)).pack(side='left')
        ttk.Button(source,text='水库对象…',command=self.open_reservoir).pack(side='left',padx=7)
        ttk.Button(source,text='作物灌溉…',command=self.open_irrigation).pack(side='left')
        ttk.Button(source,text='管理情景…',command=self.open_scenario).pack(side='left',padx=7)
        ui.flow_buttons(source)
        self.source_note=tk.StringVar(value='新流域资料未填写；教学示例需手动载入。')
        from tkinter import font as tkfont
        linefont=ttk.Style(self).lookup('TLabel','font') or 'TkDefaultFont'
        banner=ttk.Frame(self,height=tkfont.Font(font=linefont).metrics('linespace')*2+10)
        banner.pack(fill='x');banner.pack_propagate(False)
        note=ttk.Label(banner,textvariable=self.source_note,wraplength=900,padding=(14,5),anchor='w',justify='left')
        note.pack(fill='both',expand=True)
        banner.bind('<Configure>',lambda e:note.configure(wraplength=max(180,e.width-28)))
        self.host=ttk.Frame(self);self.host.pack(fill='both',expand=True)
        self.load(None,True)

    def load(self,path=None,blank=False):
        if self.form and self.form.busy:
            self.status.set('正在检查资料，请完成后再切换方案。');return
        try:
            if path:
                cfg=json.loads(Path(path).read_text(encoding='utf-8-sig'))
                if 'model' not in cfg or 'simulation' not in cfg:raise ValueError('请选择完整建模方案JSON，而不是空间子配置或环境配置')
            new=ModelSetup(self.host,self.root,self.saved,path,embedded=True,blank=blank)
        except Exception as error:messagebox.showerror('资料方案未载入',str(error),parent=self);return
        if self.form:self.form.window.destroy()
        self.form=new;self.form.window.pack(fill='both',expand=True)
        self.source_note.set('新流域：请填写自己的资料路径与模拟年份。' if blank else '已读取方案：'+Path(path).name+'；保存时另建副本，原文件保留。')

    def saved(self,path):
        self.source_note.set('资料方案已保存：'+Path(path).name)
        self.on_save(path)

    def open_raw(self):
        if self.form and self.form.busy:return
        from desktop_raw_input import RawInputDialog
        RawInputDialog(self,self.root,self.apply_raw,self.load_raw_plan)

    def open_reservoir(self):
        from desktop_reservoir import ReservoirDialog
        ReservoirDialog(self,self.root,self.status)

    def open_irrigation(self):
        from desktop_irrigation import IrrigationDialog
        IrrigationDialog(self,self.root,self.status)

    def open_scenario(self):
        from desktop_scenario import ScenarioDialog
        ScenarioDialog(self,self.root,self.status)

    def load_raw_plan(self,path):
        self.load(path)
        self.on_save(path)

    def apply_raw(self,report):
        if self.form.busy:return
        fields=self.form.vars;parts=report['parts']
        self.form.cfg.update(workspace_root=report['case_root'],runtime_config=str(self.root/'config/qgis344_runtime.json'))
        self.form.spatial['strict_soil_semantics']=True
        source=Path(report['output'])/'source_recipe.json'
        if source.is_file():
            simulation=json.loads(source.read_text(encoding='utf-8-sig')).get('simulation',{})
            for key in ('start_year','end_year','warmup_years'):
                if key in simulation:fields[key].set(str(simulation[key]))
        for section,keys in [('spatial',('dem','landuse','soil','outlet','landuse_lookup')),('soil',('usersoil','usersoil_layer','soil_lookup')),('weather',('source_dir','wgn_station_csv','wgn_monthly_csv','wgn_database','wgn_table')),('wgn',('wgn_station_csv','wgn_monthly_csv'))]:
            for key in keys:
                if parts.get(section,{}).get(key):fields[key].set(parts[section][key])
        if parts.get('soil',{}).get('usersoil'):fields['usersoil_format'].set('主表＋分层表')
        if parts.get('weather',{}).get('source_dir'):fields['weather_format'].set('SWAT+原生')
        if parts.get('wgn',{}).get('wgn_station_csv'):fields['wgn_method'].set('CSV两文件')
        self.source_note.set('原始资料已另存：'+report['output']+'；缺项见conversion_report.json，需补齐后才能通过检查。')

    def choose(self):
        path=filedialog.askopenfilename(parent=self,initialdir=str(self.root/'config'),filetypes=[('建模方案','*.json')])
        if path:self.load(path)

    def open_editor(self):
        try:
            cfg={}
            current=self.get_config()
            if current and Path(current).is_file():cfg=json.loads(Path(current).read_text(encoding='utf-8-sig'))
            runtime=json.loads((self.root/cfg.get('runtime_config','config/qgis344_runtime.json')).read_text(encoding='utf-8-sig'))
            exe=Path(runtime['editor_executable'])
            if not exe.is_file():raise FileNotFoundError(exe)
            subprocess.Popen([str(exe)],cwd=exe.parent)
            self.status.set('已请求打开官方Editor；请选择要查看或编辑的项目。自动建模时同样调用其原生导入API。')
        except Exception as error:messagebox.showerror('Editor未打开',str(error),parent=self)
