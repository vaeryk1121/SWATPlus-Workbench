"""Shared, lightweight Tk presentation helpers. No model or file-writing logic."""
import tkinter as tk
from tkinter import font as tkfont, ttk

FONT = "Microsoft YaHei UI"
COLORS = {"bg": "#F4F7FB", "surface": "#FFFFFF", "ink": "#163B67",
          "muted": "#637991", "line": "#DCE6F1", "accent": "#236BC4",
          "tint": "#EAF2FC"}


def scale(widget):
    return max(1.0, float(widget.tk.call("tk", "scaling")) / (96 / 72))


def px(widget, value):
    return max(1, round(value * scale(widget)))


def configure_theme(window):
    """Set real widget fonts as well as ttk styles; entries do not inherit style.font."""
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                 "TkCaptionFont", "TkSmallCaptionFont", "TkTooltipFont"):
        try:
            tkfont.Font(root=window, name=name, exists=True).configure(family=FONT, size=10)
        except tk.TclError:
            pass
    for pattern in ("*TEntry.font", "*TCombobox.font", "*Listbox.font"):
        window.option_add(pattern, (FONT, 10))
    window.option_add("*Text.font", (FONT, 10))
    s = ttk.Style(window)
    c = COLORS
    s.configure("TFrame", background=c["surface"])
    s.configure("TLabel", font=(FONT, 10), foreground=c["ink"],
                background=c["surface"], padding=(0, px(window, 2)))
    s.configure("TLabelframe", background=c["surface"], bordercolor=c["line"],
                lightcolor=c["line"], darkcolor=c["line"], borderwidth=1)
    s.configure("TLabelframe.Label", font=(FONT, 11, "bold"),
                foreground=c["ink"], background=c["surface"])
    s.configure("Title.TLabel", font=(FONT, 17, "bold"), padding=(0, 0, 0, 4))
    s.configure("Muted.TLabel", foreground=c["muted"], font=(FONT, 10))
    s.configure("Hint.TLabel", foreground=c["muted"], font=(FONT, 9))
    s.configure("TButton", font=(FONT, 10), padding=(px(window, 12), px(window, 7)),
                borderwidth=0, background="#E5EFFB", foreground=c["ink"])
    s.map("TButton", background=[("active", "#D5E6FA"), ("disabled", "#F0F4F9")],
          foreground=[("disabled", "#95A5B7")])
    s.configure("Primary.TButton", background=c["accent"], foreground="white",
                font=(FONT, 10, "bold"), padding=(px(window, 16), px(window, 8)))
    s.map("Primary.TButton", background=[("active", "#1756A5"), ("disabled", "#A7C5EB")],
          foreground=[("disabled", "white")])
    s.configure("TEntry", padding=(px(window, 8), px(window, 5)),
                fieldbackground="white", bordercolor=c["line"])
    s.configure("TCombobox", padding=(px(window, 8), px(window, 5)),
                arrowsize=px(window, 14), fieldbackground="white", bordercolor=c["line"])
    s.configure("TCheckbutton", font=(FONT, 10), background="white", padding=(0, 3))
    s.configure("Treeview", font=(FONT, 10), rowheight=tkfont.Font(
        root=window, name="TkDefaultFont", exists=True).metrics("linespace") + px(window, 12),
        background="white", fieldbackground="white", foreground=c["ink"], borderwidth=0)
    s.configure("Treeview.Heading", font=(FONT, 10, "bold"),
                padding=(px(window, 9), px(window, 7)), background=c["tint"], foreground=c["ink"])
    padding = (px(window, 13), px(window, 7))
    s.configure("TNotebook.Tab", font=(FONT, 10), padding=padding)
    s.map("TNotebook.Tab", padding=[("selected", padding), ("!selected", padding)])


def fit_window(window, width=1100, height=800):
    """Logical dimensions, bounded by the current display; no global DPI changes."""
    sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
    w, h = min(px(window, width), sw-px(window, 32)), min(px(window, height), sh-px(window, 72))
    parent = window.master.winfo_toplevel() if window.master is not None else None
    x = parent.winfo_rootx() + (parent.winfo_width()-w)//2 if parent else (sw-w)//2
    y = parent.winfo_rooty() + (parent.winfo_height()-h)//2 if parent else (sh-h)//2
    x, y = max(px(window, 12), min(x, sw-w-px(window, 12))), max(px(window, 12), min(y, sh-h-px(window, 48)))
    window.geometry(f"{w}x{h}+{x}+{y}")
    window.minsize(min(px(window, 760), w), min(px(window, 520), h))
    tk.Toplevel.configure(window, background=COLORS["bg"])


def wrap_label(label, margin=12):
    label.bind("<Configure>", lambda e: label.configure(
        wraplength=max(px(label, 100), e.width-px(label, margin))), add="+")
    return label


def attach_wheel(canvas, content):
    """Window-local wheel binding; closing a dialog never removes another window's bindings."""
    top = canvas.winfo_toplevel()
    def wheel(event):
        w = event.widget
        if isinstance(w, (tk.Text, ttk.Treeview, ttk.Combobox)):
            return
        while w is not None:
            if w is canvas or w is content:
                canvas.yview_scroll(-1 * (int(event.delta/120) or (1 if event.delta > 0 else -1)), "units")
                return "break"
            w = getattr(w, "master", None)
    binding = top.bind("<MouseWheel>", wheel, add="+")
    def dispose(event):
        if event.widget is canvas:
            try:
                top.unbind("<MouseWheel>", binding)
            except tk.TclError:
                pass
    canvas.bind("<Destroy>", dispose, add="+")


class ScrollPane(ttk.Frame):
    def __init__(self, parent, padding=14):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, background=COLORS["surface"],
                                yscrollincrement=px(self, 22))
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=bar.set)
        self.content = ttk.Frame(self.canvas, padding=padding)
        item = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(item, width=e.width))
        self.content.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        attach_wheel(self.canvas, self.content)


def flow_buttons(frame):
    """Wrap existing buttons into rows, keeping their callbacks and variables intact."""
    pending = [None]
    last = [None]
    def layout():
        pending[0] = None
        if not frame.winfo_exists() or frame.winfo_width() < 100:
            return
        available = frame.winfo_width()
        row = col = used = 0
        placements = []
        gap = px(frame, 8)
        for w in frame.winfo_children():
            width = w.winfo_reqwidth() + gap
            if used and used + width > available:
                row += 1; col = used = 0
            placements.append((w, row, col))
            used += width; col += 1
        key = tuple((str(w), row, col) for w, row, col in placements)
        if key == last[0]:
            return
        last[0] = key
        # Unpack every child before gridding any child: Tk forbids mixing
        # managers in the same frame, even for the duration of a callback.
        for w, _row, _col in placements:
            w.pack_forget()
        for w, row, col in placements:
            w.grid(row=row, column=col, sticky="w", padx=(0, gap), pady=(0, px(frame, 5)))
    def changed(event=None):
        if pending[0] is None:
            pending[0] = frame.after_idle(layout)
    def dispose(event):
        if event.widget is frame and pending[0] is not None:
            try:frame.after_cancel(pending[0])
            except tk.TclError:pass
    frame.bind("<Configure>", changed, add="+")
    frame.bind("<Destroy>", dispose, add="+")
    changed()


def disclosure(parent, title, text):
    box = ttk.Frame(parent)
    box.pack(fill="x", pady=(6, 0))
    opened = tk.BooleanVar(master=box, value=False)
    details = tk.Text(box, height=5, wrap="word", relief="flat", background="#F4F8FD",
                      foreground=COLORS["muted"], padx=12, pady=10, font=(FONT, 10))
    details.insert("1.0", text); details.configure(state="disabled")
    def toggle():
        opened.set(not opened.get())
        button.configure(text=("收起 · " if opened.get() else "查看 · ") + title)
        details.pack(fill="x", pady=(5, 0)) if opened.get() else details.pack_forget()
    button = ttk.Button(box, text="查看 · " + title, style="Quiet.TButton", command=toggle)
    button.pack(anchor="w")
    return box
