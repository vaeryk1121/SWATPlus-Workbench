"""Local raw-source conversion dialog; workers never call Tk widgets."""
import datetime as dt
import json
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
import desktop_ui as ui
from tkinter import ttk,filedialog,messagebox


class RawInputDialog:
    def __init__(self,parent,root,on_apply,on_plan):
        self.root=Path(root);self.on_apply=on_apply;self.on_plan=on_plan;self.busy=False;self.result=None
        self.window=tk.Toplevel(parent);self.window.title('原始资料 → 规范化输入')
        ui.fit_window(self.window, 1120, 800)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        top=ttk.Frame(self.window,padding=12);top.pack(fill='x')
        ttk.Label(top,text='原始文件保留；在案例目录另存转换结果。缺项会列出，不自动补齐。',font=('Microsoft YaHei UI',11)).pack(anchor='w')
        buttons=ttk.Frame(top);buttons.pack(fill='x',pady=(10,0))
        for title,command in [('读取来源方案…',self.load),('从逐日CSV开始…',self.csv_wizard),('另存来源方案…',self.save)]:
            ttk.Button(buttons,text=title,command=command).pack(side='left',padx=(0,8))
        ttk.Button(buttons,text='通用数据源向导…',command=self.product_wizard).pack(side='left')
        ttk.Button(buttons,text='使用说明 ↗',command=self.guide).pack(side='right')
        ui.flow_buttons(buttons)
        notebook=ttk.Notebook(self.window);notebook.pack(fill='both',expand=True,padx=12)
        config=ttk.Frame(notebook);preview=ttk.Frame(notebook);notebook.add(config,text='来源与字段/单位');notebook.add(preview,text='识别预览和转换结果')
        self.notebook=notebook
        ttk.Label(config,text='来源方案可重复使用；请确认出口、坐标系、列名和单位。支持本地GDAL栅格、CSV/TSV/XLSX剖面属性及带日期气象。',wraplength=1000).pack(fill='x',pady=8)
        self.editor=tk.Text(config,wrap='none',font=('Consolas',10),undo=True,relief='flat',padx=12,pady=10)
        xscroll=ttk.Scrollbar(config,orient='horizontal',command=self.editor.xview)
        self.editor.configure(xscrollcommand=xscroll.set);xscroll.pack(side='bottom',fill='x')
        scroll=ttk.Scrollbar(config,command=self.editor.yview);self.editor.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.editor.pack(fill='both',expand=True)
        self.preview=tk.Text(preview,wrap='word',font=('Microsoft YaHei UI',10),state='disabled')
        sc=ttk.Scrollbar(preview,command=self.preview.yview);self.preview.configure(yscrollcommand=sc.set)
        sc.pack(side='right',fill='y');self.preview.pack(fill='both',expand=True)
        foot=ttk.Frame(self.window,padding=12);foot.pack(fill='x')
        self.status=tk.StringVar(value='读取已保存的来源方案，或用逐日CSV向导选择列名和单位。')
        ttk.Label(foot,textvariable=self.status,wraplength=1000).pack(fill='x')
        bar=ttk.Frame(foot);bar.pack(fill='x',pady=(10,0))
        self.buttons=[]
        for text,callback in [('识别/预览（不转换）',lambda:self.run(True)),('转换并检查',lambda:self.run(False)),('将已转换资料带回表单',self.apply)]:
            b=ttk.Button(bar,text=text,command=callback);b.pack(side='left',padx=(0,8));self.buttons.append(b)
        ui.flow_buttons(bar)
        self.editor.insert('1.0',json.dumps({'case_root':str(self.root/'cases/YourBasin'),'title':'新流域','simulation':{'start_year':2001,'end_year':2002,'warmup_years':0}},ensure_ascii=False,indent=2))

    def close(self):
        if self.busy:self.status.set('正在转换，请等待本次完成后关闭。');return
        self.window.destroy()

    def set_recipe(self,spec):
        self.editor.delete('1.0','end');self.editor.insert('1.0',json.dumps(spec,ensure_ascii=False,indent=2))
        self.result=None

    def product_wizard(self):
        if self.busy:return
        from desktop_product_setup import open_product_setup
        open_product_setup(self.window,self.root,self.set_recipe)

    def load(self):
        if self.busy:return
        path=filedialog.askopenfilename(parent=self.window,title='选择原始资料来源方案',filetypes=[('来源方案','*.json')])
        if path:
            try:self.set_recipe(json.loads(Path(path).read_text(encoding='utf-8-sig')))
            except Exception as e:messagebox.showerror('未读取',str(e),parent=self.window)

    def save(self):
        if self.busy:return
        try:spec=json.loads(self.editor.get('1.0','end'))
        except Exception as e:messagebox.showerror('来源方案错误',str(e),parent=self.window);return
        path=filedialog.asksaveasfilename(parent=self.window,defaultextension='.json',filetypes=[('来源方案','*.json')])
        if path:Path(path).write_text(json.dumps(spec,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

    def guide(self):
        import os
        os.startfile(str(self.root/'docs/输入资料规范.md'))

    def csv_wizard(self):
        if self.busy:return
        path=filedialog.askopenfilename(parent=self.window,filetypes=[('CSV/TSV','*.csv *.tsv')])
        if not path:return
        try:
            from raw_tables import table
            rows=table({'path':path});columns=list(rows[0])
        except Exception as e:messagebox.showerror('原表未读取',str(e),parent=self.window);return
        win=tk.Toplevel(self.window);win.title('逐日CSV：选择列与单位')
        ui.fit_window(win, 860, 780)
        pane=ui.ScrollPane(win);pane.pack(fill='both',expand=True)
        frame=pane.content;frame.columnconfigure(1,weight=1)
        values={}
        definitions=[('case_root','案例输出目录',str(self.root/'cases/YourBasin'),None),('station','站号列',columns[0],columns),('date','日期列',columns[0],columns),('date_format','日期格式','%Y-%m-%d',None),('sid','选用站号','',None),('lat','站点纬度','',None),('lon','站点经度','',None),('elev','高程（米）','',None),('start','开始日期','',None),('end','结束日期','',None)]
        from raw_weather import FACTORS
        for name in FACTORS:
            definitions.extend([(name,name+'数值列（留空不导入）','',['',*columns]),(name+'_unit',name+'原始单位',next(iter(FACTORS[name])),list(FACTORS[name]))])
        for i,(key,label,default,choices) in enumerate(definitions):
            values[key]=tk.StringVar(value=default)
            ttk.Label(frame,text=label).grid(row=i,column=0,sticky='w',pady=2)
            widget=ttk.Combobox(frame,textvariable=values[key],values=choices,state='readonly',width=45) if choices is not None else ttk.Entry(frame,textvariable=values[key],width=48)
            widget.grid(row=i,column=1,sticky='ew',padx=10)
        def done():
            try:
                v={k:x.get().strip() for k,x in values.items()};start=dt.date.fromisoformat(v['start']);end=dt.date.fromisoformat(v['end'])
                fields={k:{'column':v[k],'unit':v[k+'_unit'],'missing_codes':['','NA','NaN']} for k in FACTORS if v[k]}
                spec={'case_root':v['case_root'],'simulation':{'start_year':start.year,'end_year':end.year,'warmup_years':0},'weather':{'start':str(start),'end':str(end),'gap_policy':'error','stations':[{'id':v['sid'],'lat':float(v['lat']),'lon':float(v['lon']),'elev':float(v['elev'])}],'sources':[{'format':'dated_csv','path':path,'station_column':v['station'],'date_column':v['date'],'date_format':v['date_format'],'fields':fields}]}}
                self.set_recipe(spec);win.destroy()
            except Exception as e:messagebox.showerror('请核对填写内容',str(e),parent=win)
        ttk.Button(frame,text='使用这些列和单位',command=done).grid(row=len(definitions),column=1,sticky='e',pady=8)

    def run(self,preview):
        if self.busy:return
        try:
            spec=json.loads(self.editor.get('1.0','end'))
            case=Path(spec['case_root']).resolve()
            if not case.is_absolute():raise ValueError('案例根目录必须为绝对路径')
            runtime=json.loads((self.root/'config/qgis344_runtime.json').read_text(encoding='utf-8-sig'))
            import tempfile
            folder=Path(tempfile.mkdtemp(prefix='swat_raw_'));recipe=folder/'recipe.json'
            recipe.write_text(json.dumps(spec,ensure_ascii=False),encoding='utf-8')
        except Exception as e:messagebox.showerror('来源方案有误',str(e),parent=self.window);return
        self.busy=True
        for button in self.buttons:button.state(['disabled'])
        self.status.set('正在读取源资料…' if preview else '正在另存转换结果并检查，原件保持不变…')
        self.editor.configure(state='disabled');events=queue.Queue()
        def worker():
            try:
                import os
                command=[str(Path(runtime['qgis_root'])/'bin/python-qgis-ltr.bat'),'-X','utf8',str(self.root/'scripts/raw_input.py'),'--recipe',str(recipe)]
                if preview:command.append('--preview')
                env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1'
                process=subprocess.run(command,capture_output=True,shell=os.name=='nt',env=env,timeout=900,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                text=process.stdout.decode('utf-8',errors='replace')
                if process.returncode not in (0,2):raise ValueError(process.stderr.decode('utf-8',errors='replace')[-2500:])
                result=json.loads(text)
                events.put((True,result))
            except Exception as error:events.put((False,str(error)))
            finally:
                import shutil
                shutil.rmtree(folder)
        def poll():
            try:ok,result=events.get_nowait()
            except queue.Empty:self.window.after(150,poll);return
            self.busy=False
            for button in self.buttons:button.state(['!disabled'])
            self.editor.configure(state='normal')
            self.preview.configure(state='normal');self.preview.delete('1.0','end');self.preview.insert('1.0',json.dumps(result,ensure_ascii=False,indent=2) if ok else result);self.preview.configure(state='disabled')
            self.notebook.select(1)
            if ok and not preview:
                self.result=result
                self.status.set('转换及既有输入检查通过，可带回建模。' if result['ready'] else '已保留可转换部分；有缺项，暂不能完整建模，详见blockers/missing。')
            else:self.status.set('预览完成；尚未转换。' if ok else '未完成：'+result)
        threading.Thread(target=worker,daemon=True).start();self.window.after(150,poll)

    def apply(self):
        if not self.result:self.status.set('请先执行转换。');return
        if self.result.get('ready') and self.result.get('plan'):
            self.on_plan(self.result['plan'])
        else:self.on_apply(self.result)
        self.status.set('已带回资料准备表单；缺项仍须补充，未启动模型。')
