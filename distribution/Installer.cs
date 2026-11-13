// Per-user installer with an embedded ZIP. Existing user files are preserved.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: AssemblyTitle("SWATPlus Workbench Setup")]
[assembly: AssemblyProduct("SWATPlus Workbench")]
[assembly: AssemblyVersion("0.0.1.0")]
[assembly: AssemblyFileVersion("0.0.1.0")]

internal static class Installer
{
    private static readonly string DefaultDirectory = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SWATPlusWorkbench");

    [STAThread]
    private static int Main(string[] args)
    {
        bool silent = Array.Exists(args, a => a.Equals("/silent", StringComparison.OrdinalIgnoreCase));
        bool shortcut = !Array.Exists(args, a => a.Equals("/noshortcut", StringComparison.OrdinalIgnoreCase));
        string directory = DefaultDirectory;
        foreach (string arg in args)
            if (arg.StartsWith("/dir=", StringComparison.OrdinalIgnoreCase)) directory = arg.Substring(5).Trim('"');
        if (silent)
        {
            try { Install(directory, shortcut); return 0; }
            catch (Exception error) { Console.Error.WriteLine(error); return 1; }
        }
        Application.EnableVisualStyles();
        Form form = new Form { Text = "SWATPlus Workbench 0.0.1-beta.1 安装",
            ClientSize = new Size(650, 270), StartPosition = FormStartPosition.CenterScreen,
            FormBorderStyle = FormBorderStyle.FixedDialog, MaximizeBox = false,
            Font = new Font("Microsoft YaHei UI", 9F) };
        Label heading = new Label { Text = "安装 SWATPlus Workbench", AutoSize = true,
            Font = new Font("Microsoft YaHei UI", 15F, FontStyle.Bold), Location = new Point(22, 20) };
        Label note = new Label { Text = "随包提供 Python。首次启动连接已安装的 QGIS 3.44 / SWAT+ 4 环境。\n程序与工作目录需要写入权限。开源许可证见安装后的 LICENSE 文件。",
            AutoSize = false, Size = new Size(605, 48), Location = new Point(22, 65) };
        TextBox path = new TextBox { Text = directory, Location = new Point(22, 125), Width = 510 };
        Button browse = new Button { Text = "浏览…", Location = new Point(543, 123), Width = 85 };
        browse.Click += (sender, e) => { using (FolderBrowserDialog dialog = new FolderBrowserDialog())
            { dialog.SelectedPath = path.Text; if (dialog.ShowDialog(form) == DialogResult.OK) path.Text = dialog.SelectedPath; } };
        Label status = new Label { Text = "", Location = new Point(22, 165), Size = new Size(605, 28) };
        Button install = new Button { Text = "安装", Location = new Point(420, 210), Width = 105 };
        Button cancel = new Button { Text = "关闭", Location = new Point(538, 210), Width = 90 };
        bool installing = false;
        form.FormClosing += (sender, e) => { if (installing) e.Cancel = true; };
        cancel.Click += (sender, e) => form.Close();
        install.Click += async (sender, e) => {
            installing = true; install.Enabled = browse.Enabled = cancel.Enabled = path.Enabled = false;
            string destination = path.Text;
            status.Text = "正在解压程序与运行环境，请稍候…";
            try
            {
                await Task.Run(() => Install(destination, true));
                status.Text = "安装完成。可从桌面快捷方式启动，首次启动自动识别环境。";
                install.Enabled = false;
                Button launch = new Button { Text = "打开工作台", Location = install.Location, Size = install.Size };
                install.Hide(); form.Controls.Add(launch);
                launch.Click += (s, ev) => Process.Start(Path.Combine(destination, "SWATPlus Workbench.exe"));
            }
            catch (Exception error) { MessageBox.Show(form, error.Message, "安装失败"); install.Enabled = true; }
            finally { installing = false; cancel.Enabled = true; }
        };
        form.Controls.AddRange(new Control[] { heading, note, path, browse, status, install, cancel });
        Application.Run(form);
        return 0;
    }

    private static void Install(string directory, bool shortcut)
    {
        string destination = Path.GetFullPath(directory);
        Directory.CreateDirectory(destination);
        string prefix = destination.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        using (Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (ZipArchive archive = new ZipArchive(payload, ZipArchiveMode.Read))
        {
            foreach (ZipArchiveEntry entry in archive.Entries)
            {
                string target = Path.GetFullPath(Path.Combine(destination, entry.FullName));
                if (!target.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("安装包路径无效。");
                if (String.IsNullOrEmpty(entry.Name)) { Directory.CreateDirectory(target); continue; }
                string relative = entry.FullName.Replace('\\', '/');
                bool userFile = relative.StartsWith("Workbench/config/", StringComparison.OrdinalIgnoreCase)
                    || relative.StartsWith("Workbench/runs/", StringComparison.OrdinalIgnoreCase)
                    || relative.StartsWith("Workbench/models/", StringComparison.OrdinalIgnoreCase)
                    || relative.StartsWith("Workbench/data/", StringComparison.OrdinalIgnoreCase);
                if (userFile && File.Exists(target)) continue;
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                string temporary = target + ".installing";
                using (Stream input = entry.Open())
                using (Stream output = File.Create(temporary)) input.CopyTo(output);
                if (File.Exists(target)) File.Replace(temporary, target, null);
                else File.Move(temporary, target);
            }
        }
        if (shortcut)
        {
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            dynamic shell = Activator.CreateInstance(shellType);
            string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
            dynamic link = shell.CreateShortcut(Path.Combine(desktop, "SWATPlus Workbench.lnk"));
            link.TargetPath = Path.Combine(destination, "SWATPlus Workbench.exe");
            link.WorkingDirectory = destination;
            link.IconLocation = link.TargetPath + ",0";
            link.Save();
        }
        File.WriteAllText(Path.Combine(destination, "安装说明.txt"),
            "双击 SWATPlus Workbench.exe 启动。环境设置可在主界面重新打开。\r\n" +
            "程序、输入及运行结果位于本文件夹。卸载前请先保存需要保留的资料与结果。\r\n",
            new UTF8Encoding(false));
    }
}
