// SWATPlus Workbench portable launcher. Paths are relative to this EXE.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
[assembly: AssemblyTitle("SWATPlus Workbench")]
[assembly: AssemblyProduct("SWATPlus Workbench")]
[assembly: AssemblyVersion("0.0.1.0")]
[assembly: AssemblyFileVersion("0.0.1.0")]
internal static class Launcher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr window, string text, string caption, uint type);
    [STAThread]
    private static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string workbench = Path.Combine(root, "Workbench");
        string interpreter = Path.Combine(root, "runtime", "pythonw.exe");
        string script = Path.Combine(workbench, "scripts", "portable_startup.py");
        if (!File.Exists(interpreter) || !File.Exists(script))
        {
            MessageBoxW(IntPtr.Zero, "程序文件不完整，请将 EXE 与 Workbench、runtime 文件夹一起保留。\n" + root, "SWATPlus Workbench", 0x10);
            return 1;
        }
        string arguments = "-E -s -B -X utf8 \"" + script + "\"";
        foreach (string arg in args)
            if (arg == "--configure" || arg == "--smoke-test") arguments += " " + arg;
        ProcessStartInfo start = new ProcessStartInfo(interpreter, arguments);
        start.WorkingDirectory = workbench;
        start.UseShellExecute = false;
        start.EnvironmentVariables.Remove("PYTHONHOME");
        start.EnvironmentVariables.Remove("PYTHONPATH");
        try
        {
            Process process = Process.Start(start);
            if (process == null) throw new InvalidOperationException("无法创建工作台进程。");
            if (Array.IndexOf(args, "--smoke-test") >= 0)
            {
                if (!process.WaitForExit(60000)) throw new InvalidOperationException("界面检查超时。");
                return process.ExitCode;
            }
            System.Threading.Thread.Sleep(800);
            if (process.HasExited && process.ExitCode != 0)
                throw new InvalidOperationException("启动失败。详细信息见 Workbench\\startup-error.log。");
            return 0;
        }
        catch (Exception error)
        {
            MessageBoxW(IntPtr.Zero, error.Message, "SWATPlus Workbench 启动失败", 0x10);
            return 1;
        }
    }
}
