"""Paths and output readers for the separate QGIS 3.44 / SWAT+ 62 trial."""
import json
from pathlib import Path
import re


def load_config(config_path, require_editor=False, require_model=False):
    config_path = Path(config_path).resolve()
    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    root = (config_path.parent / cfg.get("workspace_root", "..")).resolve()
    runtime_path = (root / cfg.get("runtime_config", "config/qgis344_runtime.json")).resolve()
    runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
    required = []
    if require_editor:
        required += ["editor_api", "editor_version"]
    if require_editor or require_model:
        required += ["model_version"]
    if require_model:
        required += ["model_executable"]
    missing = [name for name in required if not runtime.get(name)]
    if missing:
        raise ValueError(f"{runtime_path} is missing: {', '.join(missing)}")
    if require_editor and not str(runtime["editor_version"]).startswith("4."):
        raise ValueError("This separate adapter requires Editor 4.x")
    if (require_editor or require_model) and str(runtime["model_version"]) != "62.0.1":
        raise ValueError("The current output reader has been checked for SWAT+ 62.0.1 only")
    return root, cfg, runtime


def editor_command(root, runtime):
    """Support a packaged API executable or an explicit Python/script command."""
    command = runtime["editor_api"]
    if isinstance(command, str):
        command = [command]
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("editor_api must be a path or nonempty command array")
    command = list(command)
    command[0] = str((root / command[0]).resolve())
    if not Path(command[0]).is_file():
        raise FileNotFoundError(command[0])
    for index, argument in enumerate(command[1:], 1):
        if argument.endswith(".py"):
            command[index] = str((root / argument).resolve())
            if not Path(command[index]).is_file():
                raise FileNotFoundError(command[index])
    return command


def read_output_header(stream, expected_version, kind):
    banner = stream.readline().strip()
    header = stream.readline().split()
    units = stream.readline().split()
    if not re.search(r"(?<!\d)" + re.escape(expected_version) + r"(?![\d.])", banner):
        raise ValueError(f"Unexpected output model version: {banner}")
    if header[:7] != ["jday", "mon", "day", "yr", "unit", "gis_id", "name"]:
        raise ValueError(f"Unexpected {kind} identity/date columns")
    variables = [name for name in header[7:] if name != "null"]
    # Revision 62.0.1 prints an HRU-derived basin header but does not write
    # the final plant/management labels in the basin data records.
    if kind == "basin_wb" and variables[-2:] == ["plant_cov", "mgt_ops"]:
        variables = variables[:-2]
        header = header[:-2]
    if len(units) != len(variables):
        raise ValueError(f"{kind} units/column mismatch: {len(units)}/{len(variables)}")
    unit_map = dict(zip(variables, units))
    required = {"channel_sd": {"flo_out": "m^3/s"},
                "basin_wb": {"precip": "mm", "et": "mm", "perc": "mm"}}
    if kind not in required or any(unit_map.get(k) != v for k, v in required[kind].items()):
        raise ValueError(f"Unexpected physical units in {kind}")
    return banner, header, unit_map


def output_rows(stream, header):
    for number, line in enumerate(stream, 4):
        fields = line.split()
        if not fields:
            continue
        if len(fields) != len(header):
            raise ValueError(f"Output line {number} has {len(fields)} values for {len(header)} columns")
        yield dict(zip(header, fields))
