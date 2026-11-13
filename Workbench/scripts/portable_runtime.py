"""Discover an installed QGIS/SWAT+ environment without changing its settings."""
import configparser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/qgis344_runtime.json"
PATH_FIELDS = ("qgis_root", "plugin_root", "support_root", "editor_root", "mpi_dir",
               "toolbox_executable", "sufi2_executable_dir")
REQUIRED_SUPPORT_FILES = (
    "Databases/QSWATPlusProj.sqlite", "Databases/swatplus_datasets.sqlite",
    "Databases/plant.csv", "TauDEM539Bin/PitRemove.exe", "TauDEM539Bin/StreamNet.exe",
)


def read_config(path=CONFIG):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def unique_paths(values):
    result = []
    seen = set()
    for value in values:
        if not value:
            continue
        path = Path(os.path.expandvars(str(value))).expanduser()
        key = os.path.normcase(str(path))
        if key not in seen:
            result.append(path)
            seen.add(key)
    return result


def installed_locations():
    """Read only the relevant uninstall registry entries, with both registry views."""
    if os.name != "nt":
        return []
    import winreg
    locations = []
    key_name = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as entry:
                                name = winreg.QueryValueEx(entry, "DisplayName")[0]
                                if not re.search(r"QGIS|SWAT|Microsoft MPI", name, re.I):
                                    continue
                                try:
                                    locations.append(winreg.QueryValueEx(entry, "InstallLocation")[0])
                                except OSError:
                                    icon = winreg.QueryValueEx(entry, "DisplayIcon")[0]
                                    locations.append(str(Path(icon.strip('"').split(',')[0]).parent))
                        except OSError:
                            continue
            except OSError:
                continue
    return unique_paths(locations)


def plugin_version(path):
    metadata = configparser.ConfigParser(interpolation=None)
    try:
        metadata.read(Path(path) / "metadata.txt", encoding="utf-8-sig")
        return metadata.get("general", "version", fallback="")
    except (OSError, configparser.Error):
        return ""


def discover(current=None, root=ROOT):
    """Prefer saved choices, then registry and bounded standard installation folders."""
    cfg = dict(current or {})
    installed = installed_locations()
    bases = unique_paths([os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                          os.environ.get("LOCALAPPDATA"), Path.home(), "C:/", "D:/", "E:/", "F:/"])
    candidates = list(installed)
    for base in bases:
        for folder in (base, base / "QGIS", base / "SWAT", base / "Programs"):
            if not folder.is_dir():
                continue
            try:
                candidates.extend(p for p in folder.iterdir() if p.is_dir()
                                  and re.search(r"QGIS|SWAT|Microsoft MPI", p.name, re.I))
            except OSError:
                continue
    qgis = unique_paths([cfg.get("qgis_root"), os.environ.get("QGIS_ROOT"), *candidates])
    matches = [p for p in qgis if (p / "bin/python-qgis-ltr.bat").is_file()]
    if not cfg.get("qgis_root"):
        matches.sort(key=lambda p: "3.44" not in p.name)
    if matches:
        cfg["qgis_root"] = str(matches[0])
    roaming = Path(os.environ.get("APPDATA", str(Path.home())))
    plugins = [cfg.get("plugin_root")]
    profiles = roaming / "QGIS/QGIS3/profiles"
    if profiles.is_dir():
        plugins.extend(p / "python/plugins/QSWATPlus" for p in profiles.iterdir() if p.is_dir())
    plugins.append(Path(root) / "tools/qgis344_runtime/profiles/swatplus344/python/plugins/QSWATPlus")
    for p in unique_paths(plugins):
        if plugin_version(p) == "4.0.4":
            cfg["plugin_root"] = str(p)
            break
    supports = unique_paths([cfg.get("support_root"), cfg.get("support_entry"),
                             Path.home() / "SWATPlus", "C:/SWAT/SWATPlus", *candidates])
    for p in supports:
        if all((p / name).is_file() for name in REQUIRED_SUPPORT_FILES):
            cfg["support_root"] = str(p)
            break
    editors = unique_paths([cfg.get("editor_root"),
                            str(Path(cfg["editor_executable"]).parent) if cfg.get("editor_executable") else None,
                            *candidates, *[p / "SWATPlusEditor" for p in supports]])
    for p in editors:
        if (p / "resources/app.asar.unpacked/static/api_dist/swatplus_api.exe").is_file():
            cfg["editor_root"] = str(p)
            break
    mpi = shutil.which("mpiexec.exe")
    mpis = unique_paths([cfg.get("mpi_dir"), str(Path(mpi).parent) if mpi else None,
                        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft MPI/Bin"])
    for p in mpis:
        if (p / "mpiexec.exe").is_file():
            cfg["mpi_dir"] = str(p)
            break
    if cfg.get("support_root"):
        support = Path(cfg["support_root"])
        toolboxes = unique_paths([cfg.get("toolbox_executable"),
                                 support / "SWATPlusToolbox/swatplusToolboX.exe"])
        for p in toolboxes:
            if p.is_file():
                cfg["toolbox_executable"] = str(p)
                break
    sufi_candidates = unique_paths([cfg.get("sufi2_executable_dir"),
                                    *[p / "ExternalData/SourceData/SUFI2_x64" for p in candidates]])
    for p in sufi_candidates:
        if (p / "SUFI2_LH_sample.exe").is_file():
            cfg["sufi2_executable_dir"] = str(p)
            break
    return complete_config(cfg)


def complete_config(values):
    cfg = dict(values)
    cfg.update(qgis_version_prefix="3.44.", plugin_version="4.0.4",
               editor_version="4.0.2", model_version="62.0.1",
               model_build="official Windows Intel ifort")
    cfg.setdefault("profile_root", "tools/qgis344_runtime")
    cfg.setdefault("profile_name", "swatplus344")
    if cfg.get("support_root"):
        support = Path(cfg["support_root"]).resolve()
        cfg["support_root"] = cfg["support_entry"] = str(support)
        editor = Path(cfg.get("editor_root") or support / "SWATPlusEditor")
        cfg["editor_root"] = str(editor)
        static = editor / "resources/app.asar.unpacked/static"
        cfg.update(editor_executable=str(editor / "SWATPlusEditor.exe"),
                   editor_api=str(static / "api_dist/swatplus_api.exe"),
                   model_executable=str(static / "swat_exe/swatplus-62.0.1-ifo-win_amd64-Rel.exe"))
    return cfg


def validate(values, probe=False, root=ROOT):
    cfg = complete_config(values)
    errors = []
    for key in ("qgis_root", "plugin_root", "support_root", "mpi_dir"):
        if not cfg.get(key):
            errors.append("请选择 " + key)
    if errors:
        return errors
    def require(path):
        if not Path(path).is_file():
            errors.append("缺少文件 " + str(path))
    qgis = Path(cfg["qgis_root"])
    require(qgis / "bin/python-qgis-ltr.bat")
    for name in REQUIRED_SUPPORT_FILES:
        require(Path(cfg["support_root"]) / name)
    for key in ("editor_executable", "editor_api", "model_executable"):
        require(cfg[key])
    require(Path(cfg["mpi_dir"]) / "mpiexec.exe")
    plugin = Path(root) / cfg["plugin_root"]
    require(plugin / "QSWATPlus/QSWATPlusMain.py")
    if plugin_version(plugin) != "4.0.4":
        errors.append("QSWAT+ 需要兼容版本 4.0.4")
    if probe and not errors:
        code = ("from qgis.core import Qgis; import sys; "
                "print(Qgis.QGIS_VERSION); "
                "sys.exit(0 if Qgis.QGIS_VERSION.startswith('3.44.') else 2)")
        env = os.environ.copy()
        # QGIS must load its own Python packages, not this application's runtime.
        for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            env.pop(name, None)
        try:
            command = [str(qgis / "bin/python-qgis-ltr.bat"), "-c", code]
            result = subprocess.run(command, capture_output=True, timeout=60, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                errors.append("QGIS 3.44 环境检查失败 " + result.stdout.decode("utf-8", "replace")[-300:])
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append("QGIS 检查失败 " + str(error))
    return errors


def save(values, path=CONFIG):
    cfg = complete_config(values)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return cfg


def qgis_runtime_directory(requested):
    """QGIS's isolated settings require an ASCII scratch path on Windows."""
    requested = Path(requested)
    if str(requested).isascii():
        return requested
    candidates = [Path(tempfile.gettempdir()),
                  Path(os.environ.get("PUBLIC", "C:/Users/Public")) / "Documents"]
    for base in candidates:
        if not str(base).isascii() or not base.is_dir():
            continue
        try:
            return Path(tempfile.mkdtemp(prefix="swatplus_qgis_", dir=base))
        except OSError:
            continue
    raise RuntimeError("无法创建QGIS英文临时目录，请在空间配置中指定可写的英文 qgis_runtime_dir。")
