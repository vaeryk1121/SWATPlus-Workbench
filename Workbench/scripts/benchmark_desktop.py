"""Measure GUI construction and idle cost without displaying a window or running a model.

Example (from the workspace root)::

    python -X utf8 scripts/benchmark_desktop.py --module swatplus_desktop \
        --class Desktop --output runs/desktop_redesign/after.json

The first-frame timing includes module import, Tk construction, Desktop(window),
and Tk's first idle-layout pass. This is a withdrawn-window estimate, not a
measurement of Explorer launch latency or of visible Windows compositing.
"""

import argparse
import ctypes
from ctypes import wintypes
import importlib
import json
import os
from pathlib import Path
import platform
import sys
import time


sys.dont_write_bytecode = True


def memory_counters():
    """Read only this benchmark process; Windows working set includes shared pages."""
    if os.name != "nt":
        return {"working_set_mib": None, "peak_working_set_mib": None}

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    info = PROCESS_MEMORY_COUNTERS()
    info.cb = ctypes.sizeof(info)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(info), info.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "working_set_mib": round(info.WorkingSetSize / 1048576, 3),
        "peak_working_set_mib": round(info.PeakWorkingSetSize / 1048576, 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", default="swatplus_desktop")
    parser.add_argument("--class", dest="class_name", default="Desktop")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    started = time.perf_counter()
    import tkinter as tk

    module = importlib.import_module(args.module)
    imported = time.perf_counter()
    window = tk.Tk()
    window.withdraw()
    try:
        desktop = getattr(module, args.class_name)(window)
        constructed = time.perf_counter()
        window.update_idletasks()
        first_layout = time.perf_counter()
        initial_memory = memory_counters()
        cpu_start = time.process_time()
        idle_start = time.perf_counter()
        # mainloop is intentional: ordinary after callbacks remain active.
        window.after(1000, window.quit)
        window.mainloop()
        idle_seconds = time.perf_counter() - idle_start
        idle_cpu_seconds = time.process_time() - cpu_start
        final_memory = memory_counters()
        result = {
            "module": args.module,
            "class": args.class_name,
            "source": str(Path(module.__file__).resolve()),
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "tk_patchlevel": str(window.tk.call("info", "patchlevel")),
            "window_withdrawn": window.state() == "withdrawn",
            "import_seconds": round(imported - started, 6),
            "construction_seconds": round(constructed - imported, 6),
            "first_layout_seconds": round(first_layout - started, 6),
            "initial_memory": initial_memory,
            "idle_wall_seconds": round(idle_seconds, 6),
            "idle_cpu_seconds": round(idle_cpu_seconds, 6),
            "idle_cpu_percent_one_core": round(100 * idle_cpu_seconds / idle_seconds, 3),
            "final_memory": final_memory,
            "model_started": bool(getattr(desktop, "running", False)),
            "measurement_note": "Withdrawn window; includes import and first idle layout. "
                "Idle CPU uses process CPU time over approximately 1 second. "
                "Working set includes shared resident pages. No model was requested.",
        }
    finally:
        window.destroy()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
