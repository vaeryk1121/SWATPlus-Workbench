"""Check the private QGIS/SWAT+ locations without loading or changing QGIS."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def runtime():
    cfg = json.loads((ROOT / "config/qgis344_runtime.json").read_text(encoding="utf-8"))
    for key in ("qgis_root", "profile_root", "plugin_root", "support_entry", "support_root", "mpi_dir"):
        path = Path(cfg[key])
        cfg[key] = path if path.is_absolute() else ROOT / path
    return cfg


def check_support(cfg=None):
    cfg = cfg or runtime()
    entry = cfg["support_entry"]
    target = cfg["support_root"]
    if not entry.is_dir() or not target.is_dir() or entry.resolve() != target.resolve():
        raise RuntimeError(
            f"SWAT+ directory entry is missing or points to another location: {entry} -> {target}. "
            "Update the directory entry and config/qgis344_runtime.json after migration."
        )
    required = [
        entry / "Databases/QSWATPlusProj.sqlite",
        entry / "Databases/swatplus_datasets.sqlite",
        entry / "Databases/plant.csv",
        entry / "TauDEM539Bin/PitRemove.exe",
        entry / "TauDEM539Bin/StreamNet.exe",
        entry / "TauDEM539Bin/gdal304.dll",
        cfg["plugin_root"] / "metadata.txt",
        cfg["plugin_root"] / "QSWATPlus/dataInC.cp312-win_amd64.pyd",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("New SWAT+ files are incomplete; will not use the old installation:\n" + "\n".join(missing))
    return cfg


if __name__ == "__main__":
    try:
        cfg = check_support()
        coupling = json.loads((ROOT / "config/qswatmod344_runtime.json").read_text(encoding="utf-8-sig"))
        shared_profile = cfg["profile_root"] / "profiles" / cfg["profile_name"]
        coupling_profile = ROOT / coupling["profile_root"] / "profiles" / coupling["profile_name"]
        if shared_profile.resolve() != coupling_profile.resolve():
            raise RuntimeError("The unified entry requires both plugins in the same profile.")
        coupling_root = ROOT / coupling["plugin_root"]
        for name in ("__init__.py", "metadata.txt", "QSWATMOD2.py"):
            if not (coupling_root / name).is_file():
                raise RuntimeError(f"The unified QSWATMOD2 plugin is incomplete: {coupling_root / name}")
    except Exception as exc:
        print(str(exc))
        raise SystemExit(1)
