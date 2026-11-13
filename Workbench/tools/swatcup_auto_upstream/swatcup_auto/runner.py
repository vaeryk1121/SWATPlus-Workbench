from __future__ import annotations

import os
import subprocess
import threading
import time
import ctypes
import locale
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .errors import BatchExecutionError, CancelledError


EventCallback = Optional[Callable[[str], None]]


if os.name == "nt":
    TH32CS_SNAPPROCESS = 0x00000002
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


def list_processes() -> List[Tuple[int, str]]:
    """List live process IDs and names without requiring process-query access.

    Windows can retain zero-thread process objects after every executable thread has
    exited. Those defunct entries cannot do work or hold a single-instance lock, so
    they are intentionally excluded.
    """
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid_handle:
        return []
    entry = _ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    results: List[Tuple[int, str]] = []
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            if int(entry.cntThreads) > 0:
                results.append((int(entry.th32ProcessID), entry.szExeFile))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return results


class _WindowsJob:
    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not ok:
            kernel32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, process: subprocess.Popen) -> bool:
        if not self.handle or os.name != "nt":
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(
            kernel32.AssignProcessToJobObject(
                self.handle, wintypes.HANDLE(int(process._handle))
            )
        )

    def terminate(self, exit_code: int = 1) -> None:
        if self.handle and os.name == "nt":
            ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject(
                self.handle, exit_code
            )

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            self.handle = None


@dataclass
class BatchResult:
    batch: Path
    return_code: int
    elapsed_seconds: float
    log_path: Path


def _terminate_process_tree(
    process: subprocess.Popen, job: Optional[_WindowsJob] = None
) -> None:
    if process.poll() is not None:
        if job:
            job.terminate()
        return
    if job:
        job.terminate()
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except Exception:
        try:
            process.kill()
        except OSError:
            pass


def run_batch(
    project_dir: Path,
    batch_name: str,
    log_path: Path,
    timeout_seconds: int = 0,
    cancel_event: Optional[threading.Event] = None,
    event_callback: EventCallback = None,
) -> BatchResult:
    batch_path = project_dir / batch_name
    if not batch_path.is_file():
        raise BatchExecutionError(f"批处理文件不存在：{batch_path}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if event_callback:
        event_callback(f"启动 {batch_name}")
    started = time.monotonic()
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with log_path.open("w", encoding="utf-8", newline="") as output:
        output.write(f"[swatcup-auto] cwd={project_dir}\r\n")
        output.write(f"[swatcup-auto] batch={batch_name}\r\n")
        output.flush()
        process = subprocess.Popen(
            ["cmd.exe", "/d", "/c", "call", batch_path.name],
            cwd=str(project_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
        job = _WindowsJob()
        job.assign(process)
        console_encoding = locale.getpreferredencoding(False) or "mbcs"

        def pump_output() -> None:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                output.write(chunk.decode(console_encoding, errors="replace"))
                output.flush()

        pump_thread = threading.Thread(target=pump_output, daemon=True)
        pump_thread.start()
        try:
            while True:
                return_code = process.poll()
                if return_code is not None:
                    break
                if cancel_event is not None and cancel_event.is_set():
                    _terminate_process_tree(process, job)
                    raise CancelledError(f"用户取消：{batch_name}")
                elapsed = time.monotonic() - started
                if timeout_seconds > 0 and elapsed > timeout_seconds:
                    _terminate_process_tree(process, job)
                    raise BatchExecutionError(
                        f"{batch_name} 超过超时限制 {timeout_seconds} 秒；进程树已终止"
                    )
                time.sleep(0.25)
        finally:
            if process.poll() is None:
                _terminate_process_tree(process, job)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            pump_thread.join(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            job.close()
        elapsed = time.monotonic() - started
        output.write(f"\r\n[swatcup-auto] return_code={return_code} elapsed={elapsed:.3f}s\r\n")
    if return_code != 0:
        raise BatchExecutionError(
            f"{batch_name} 返回错误码 {return_code}；详见日志 {log_path}"
        )
    if event_callback:
        event_callback(f"完成 {batch_name}，耗时 {elapsed:.1f} 秒")
    return BatchResult(batch_path, return_code, elapsed, log_path)
