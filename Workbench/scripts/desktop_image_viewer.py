"""Lazy Pillow viewer. Render the visible viewport, never a huge zoomed bitmap."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk


class ResultImageViewer(tk.Frame):
    def __init__(self, parent, *, background="#F4F8FE", foreground="#163B67"):
        super().__init__(parent, bg=background)
        self.path = None
        self.source = self.photo = None
        self.zoom = 1.0
        self.mode = "width"
        self.offset_x = self.offset_y = 0
        self._paint_id = None
        self._stamp = None
        toolbar = tk.Frame(self, bg=background)
        toolbar.pack(fill="x", padx=8, pady=(8, 10))
        for label, command in [("适应宽度", self.fit_width), ("完整图", self.fit_window),
                               ("100%", self.actual_size), ("−", lambda: self.zoom_by(1/1.25)),
                               ("+", lambda: self.zoom_by(1.25))]:
            ttk.Button(toolbar, text=label, command=command, width=9 if len(label)>1 else 3).pack(side="left", padx=(0, 6))
        self.info = tk.Label(toolbar, text="尚未选择图表", bg=background, fg=foreground,
                             font=("Microsoft YaHei UI", 10), anchor="e")
        self.info.pack(side="right", fill="x", expand=True)
        body = tk.Frame(self, bg=background)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1); body.grid_rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, bg="#FFFFFF", highlightthickness=0, borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vertical = ttk.Scrollbar(body, orient="vertical", command=self.scroll_y)
        self.horizontal = ttk.Scrollbar(body, orient="horizontal", command=self.scroll_x)
        self.vertical.grid(row=0, column=1, sticky="ns")
        self.horizontal.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=self.horizontal.set, yscrollcommand=self.vertical.set)
        self.message = tk.Label(self.canvas, bg="#FFFFFF", fg=foreground,
                                font=("Microsoft YaHei UI", 11), wraplength=640,
                                text="选择已完成的任务，查看流量对比与参数搜索结果。")
        self.message.place(relx=.5, rely=.5, anchor="center")
        self.canvas.bind("<Configure>", self._resized)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", lambda _e:self.canvas.configure(cursor=""))
        self.bind("<Destroy>", self._destroyed)

    def clear(self, message="运行完成后可查看结果图表。"):
        self.path = self._stamp = None
        if self.source is not None:self.source.close()
        self.source = self.photo = None
        self.canvas.delete("image")
        self.canvas.configure(scrollregion=(0, 0, 1, 1))
        self.info.configure(text="尚未生成图表")
        self.message.configure(text=message); self.message.place(relx=.5, rely=.5, anchor="center")

    def set_path(self, path):
        path = Path(path)
        try:
            stamp = (str(path.resolve()), path.stat().st_mtime_ns)
            if self._stamp == stamp:return
            from PIL import Image
            with Image.open(path) as loaded:source = loaded.convert("RGB")
            if self.source is not None:self.source.close()
            self.source = source; self.path = path; self._stamp = stamp
            self.message.place_forget()
            self.mode = "width"; self.canvas.xview_moveto(0); self.canvas.yview_moveto(0)
            self._resized()
        except (OSError, ImportError) as error:
            self.clear(f"图表暂不可用：{error}")

    def fit_width(self):
        self.mode="width";self._resized();self.canvas.xview_moveto(0);self.canvas.yview_moveto(0);self._schedule()
    def fit_window(self):
        self.mode="window";self._resized();self.canvas.xview_moveto(0);self.canvas.yview_moveto(0);self._schedule()
    def actual_size(self):self.set_zoom(1.0)
    def zoom_by(self, factor):self.set_zoom(self.zoom*factor)

    def set_zoom(self, value):
        if self.source is None:return
        x=(self.canvas.canvasx(0)+self.canvas.winfo_width()/2-self.offset_x)/self.zoom
        y=(self.canvas.canvasy(0)+self.canvas.winfo_height()/2-self.offset_y)/self.zoom
        self.mode="manual";self.zoom=max(.05,min(8.,value));self._set_region()
        self.canvas.xview_moveto(max(0,x*self.zoom+self.offset_x-self.canvas.winfo_width()/2)/max(self.canvas.winfo_width(),self.source.width*self.zoom))
        self.canvas.yview_moveto(max(0,y*self.zoom+self.offset_y-self.canvas.winfo_height()/2)/max(self.canvas.winfo_height(),self.source.height*self.zoom))
        self._schedule()

    def _set_region(self):
        width=round(self.source.width*self.zoom);height=round(self.source.height*self.zoom)
        viewport_width,viewport_height=self.canvas.winfo_width(),self.canvas.winfo_height()
        self.offset_x=max(0,(viewport_width-width)/2);self.offset_y=max(0,(viewport_height-height)/2)
        self.canvas.configure(scrollregion=(0,0,max(width,viewport_width),max(height,viewport_height)))
        self.info.configure(text=f"{self.zoom:.0%}  ·  {self.source.width} × {self.source.height} px")

    def _resized(self, _event=None):
        if self.source is None or not self.info.winfo_exists():return
        width=max(1,self.canvas.winfo_width()-20);height=max(1,self.canvas.winfo_height()-20)
        if width<30 or height<30:return
        if self.mode=="width":self.zoom=width/self.source.width
        elif self.mode=="window":self.zoom=min(width/self.source.width,height/self.source.height)
        self._set_region();self._schedule()

    def _schedule(self):
        if self._paint_id:self.after_cancel(self._paint_id)
        self._paint_id=self.after(35,self._paint)

    def _paint(self):
        self._paint_id=None
        if self.source is None:return
        from PIL import Image, ImageTk
        width,height=self.canvas.winfo_width(),self.canvas.winfo_height()
        left,top=self.canvas.canvasx(0)-self.offset_x,self.canvas.canvasy(0)-self.offset_y
        box=(max(0,int(left/self.zoom)),max(0,int(top/self.zoom)),
             min(self.source.width,int((left+width)/self.zoom)+2),
             min(self.source.height,int((top+height)/self.zoom)+2))
        if box[2]<=box[0] or box[3]<=box[1]:return
        tile=self.source.crop(box)
        tile=tile.resize((max(1,round(tile.width*self.zoom)),max(1,round(tile.height*self.zoom))),Image.Resampling.LANCZOS)
        self.photo=ImageTk.PhotoImage(tile,master=self.canvas)
        self.canvas.delete("image")
        self.canvas.create_image(self.offset_x+box[0]*self.zoom,self.offset_y+box[1]*self.zoom,anchor="nw",image=self.photo,tags="image")

    def scroll_y(self,*args):self.canvas.yview(*args);self._schedule()
    def scroll_x(self,*args):self.canvas.xview(*args);self._schedule()
    def _wheel(self,event):
        if self.source is None:return "break"
        if event.state & 0x0004:self.zoom_by(1.15 if event.delta>0 else 1/1.15)
        elif event.state & 0x0001:self.scroll_x("scroll",-int(event.delta/120) or (-1 if event.delta>0 else 1),"units")
        else:self.scroll_y("scroll",-3*(int(event.delta/120) or (1 if event.delta>0 else -1)),"units")
        return "break"
    def _drag_start(self,event):self.canvas.scan_mark(event.x,event.y);self.canvas.configure(cursor="fleur")
    def _drag_move(self,event):self.canvas.scan_dragto(event.x,event.y,gain=1);self._schedule()
    def _destroyed(self,event):
        if event.widget is self:
            if self._paint_id:self.after_cancel(self._paint_id)
            if self.source is not None:self.source.close()


def open_image_window(parent, path):
    """Open a native viewer even when Windows has no PNG file association."""
    window=tk.Toplevel(parent);window.title(f"SWAT+ 图表 · {Path(path).name}")
    width=min(parent.winfo_screenwidth()-100, max(1050,parent.winfo_width()))
    height=min(parent.winfo_screenheight()-100, max(750,parent.winfo_height()))
    window.geometry(f"{width}x{height}");window.minsize(720,500)
    viewer=ResultImageViewer(window);viewer.pack(fill="both",expand=True)
    window.after_idle(lambda:viewer.set_path(path))
    return window,viewer
