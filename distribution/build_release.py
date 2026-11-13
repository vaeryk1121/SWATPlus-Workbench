"""Build a Windows x64 portable ZIP and per-user installer from this repository.

Run with the Python installation that supplies the pinned dependencies.
The source Python is copied selectively. Installed QGIS and SWAT+ remain external.
"""
import argparse
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from packaging.requirements import Requirement

VERSION = "0.0.1-beta.1"
ROOT = Path(__file__).resolve().parents[1]


def copy_tree(source, target):
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "test", "tests", ".git"))


def copy_runtime(python_root, target, extra_packages=None):
    python_root = Path(python_root).resolve()
    for name in ("python.exe", "pythonw.exe", "python3.dll", "python39.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
        shutil.copy2(python_root / name, target / name)
    copy_tree(python_root / "DLLs", target / "DLLs")
    copy_tree(python_root / "tcl", target / "tcl")
    shutil.copytree(python_root / "Lib", target / "Lib",
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc", "test", "tests"))
    if extra_packages:
        sys.path.insert(0, str(Path(extra_packages).resolve()))
    installed = target / "Lib/site-packages"
    installed.mkdir(parents=True)
    pending = ["numpy", "scipy", "spotpy", "SALib", "multiprocess", "dill",
               "matplotlib", "pandas", "pillow", "packaging"]
    included = {}
    while pending:
        name = pending.pop(0)
        distribution = metadata.distribution(name)
        actual_name = distribution.metadata["Name"]
        key = actual_name.lower().replace("_", "-")
        if key in included:
            continue
        included[key] = distribution.version
        for requirement in distribution.requires or []:
            requirement = Requirement(requirement)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
        for relative in distribution.files or []:
            if ".." in relative.parts or "__pycache__" in relative.parts or str(relative).endswith(".pyc"):
                continue
            if relative.name == "direct_url.json":
                continue
            source = Path(distribution.locate_file(relative))
            if source.is_file():
                destination = installed / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    (target / "Lib/sitecustomize.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "os.environ.setdefault('MPLCONFIGDIR', str(Path(sys.prefix).parent / 'Workbench/runs/matplotlib'))\n",
        encoding="utf-8")
    (target.parent / "PYTHON_PACKAGES.json").write_text(
        json.dumps({"python": sys.version.split()[0], "packages": included}, indent=2) + "\n", encoding="utf-8")
    return included


def archive_tree(source, destination, exclude=()):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and not any(part in exclude for part in path.relative_to(source).parts):
                archive.write(path, path.relative_to(source).as_posix())


def build(repository, python_root, output, extra_packages=None, reuse_runtime=False):
    repository, output = Path(repository).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = output / ("SWATPlus-Workbench-" + VERSION + "-win-x64")
    app.mkdir(exist_ok=reuse_runtime)
    copy_tree(repository / "Workbench", app / "Workbench")
    for filename in ('coverage.json', 'project_parameterizations.md'):
        old_reference = app / 'Workbench/docs/参数参考' / filename
        if old_reference.is_file():
            old_reference.relative_to(app)
            old_reference.unlink()
    # Tests and launcher build sources belong to the source repository.
    for name in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "RELEASE_NOTES.md"):
        shutil.copy2(repository / name, app / name)
    runtime = app / "runtime"
    if reuse_runtime and (runtime / 'python.exe').is_file():
        packages = json.loads((app / 'PYTHON_PACKAGES.json').read_text(encoding='utf-8'))['packages']
    else:
        runtime.mkdir()
        packages = copy_runtime(python_root, runtime, extra_packages)
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not csc.is_file():
        raise FileNotFoundError("Windows .NET Framework C# compiler is required to build the installer")
    launcher = app / "SWATPlus Workbench.exe"
    icon = repository / "Workbench/launcher/launcher.ico"
    base_args = [str(csc), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/codepage:65001"]
    subprocess.run(base_args + ["/win32icon:" + str(icon), "/out:" + str(launcher),
                               str(repository / "Workbench/launcher/Launcher.cs")], check=True)
    portable = output / (app.name + ".zip")
    archive_tree(app, portable, exclude=("__pycache__",))
    installer = output / ("SWATPlus-Workbench-" + VERSION + "-setup.exe")
    subprocess.run(base_args + ["/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
                               "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll",
                               "/r:Microsoft.CSharp.dll", "/win32icon:" + str(icon),
                               "/resource:" + str(portable) + ",payload.zip", "/out:" + str(installer),
                               str(repository / "distribution/Installer.cs")], check=True)
    print(json.dumps({"application": str(app), "portable_zip": str(portable),
                      "installer": str(installer), "packages": packages}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--python-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--extra-packages", type=Path)
    parser.add_argument("--reuse-runtime", action="store_true")
    args = parser.parse_args()
    build(args.repository, args.python_root, args.output, args.extra_packages, args.reuse_runtime)
