"""Version-specific SWAT+ input/output readers.

The 62.0.1 profile is based on the tagged upstream source and real exported
tables, not an assumption that every older parameter still has the same meaning.
Export banners identify the writer; output banners identify the executing model.
"""
import math
from pathlib import Path
import re

SUPPORTED_VERSIONS = {"60.5.7", "61.0.1", "62.0.1"}
IDENTITY_COLUMNS = ["jday", "mon", "day", "yr", "unit", "gis_id", "name"]
CHECKER_COLUMNS = (
    "zmx usle_k sumfc sumul usle_p usle_ls esco epco cn3_swf perco latq_co tiledrain"
).split()
HYDROLOGY_COLUMNS = (
    "name lat_ttime lat_sed can_max esco epco orgn_enrich orgp_enrich "
    "cn3_swf bio_mix perco lat_orgn lat_orgp pet_co latq_co"
).split()


def version_ok(banner, expected):
    return re.search(r"(?<!\d)" + re.escape(expected) + r"(?![\d.])", banner) is not None


def _lines(path):
    return Path(path).read_text(encoding="utf-8-sig").splitlines()


def _checked_table(path, required, exact=False):
    rows = _lines(path)
    if len(rows) < 3:
        raise ValueError(f"Empty input table: {path}")
    header = rows[1].split()
    if (header != required if exact else not set(required) <= set(header)):
        raise ValueError(f"Unverified {Path(path).name} layout: {header}")
    data = []
    for index, line in enumerate(rows[2:], 3):
        cells = line.split()
        if not cells:
            continue
        routing_width = None
        if Path(path).suffix == ".con" and "out_tot" in header:
            base_width = header.index("out_tot") + 1
            if len(cells) >= base_width:
                routing_width = base_width + 4 * int(cells[base_width - 1])
        malformed = (len(cells) != routing_width if routing_width is not None else
                     len(cells) < len(header) or exact and len(cells) != len(header))
        if malformed:
            raise ValueError(f"{Path(path).name}:{index}: input column count mismatch")
        data.append(dict(zip(header, cells)))
    if not data:
        raise ValueError(f"Empty input records: {path}")
    return header, data


def validate_input_layout(model, expected_version):
    """Validate the actual standard-HRU tables for the requested engine profile.

    Old profiles retain their existing checks in swatplus_trial. A modern export
    with an absent revision banner is permitted only after structural validation;
    a stated different engine revision is rejected, never relabeled.
    """
    model = Path(model)
    if expected_version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Unsupported SWAT+ version {expected_version}")
    if expected_version != "62.0.1":
        return {"profile": expected_version, "layout_checked": "legacy"}
    banner = _lines(model / "file.cio")[0]
    declared = re.search(r"(?:rev(?:ision)?\.?\s*|SWAT\+\s*)(\d+\.\d+\.\d+)", banner, re.I)
    if declared and declared.group(1) != expected_version:
        raise ValueError(f"Input export declares {declared.group(1)}, expected {expected_version}; re-export it")
    header = _lines(model / "hydrology.hyd")[1].split()
    expected_hyd = list(HYDROLOGY_COLUMNS)
    # topohyd_init.f90 explicitly retains the old harg_pet coefficient encoding.
    if "harg_pet" in header:
        expected_hyd[13] = "harg_pet"
    _, hyd = _checked_table(model / "hydrology.hyd", expected_hyd, exact=True)
    names = [row["name"] for row in hyd]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate hydrology record names")
    for row in hyd:
        for column in expected_hyd[1:]:
            if not math.isfinite(float(row[column])):
                raise ValueError(f"Non-finite hydrology value: {column}")
        for column in ("esco", "perco"):
            value = float(row[column])
            if not 0 <= value <= 1:
                raise ValueError(f"Initialization {column} is outside [0, 1]")
            if column == "perco" and 1e-9 < value <= 1e-6:
                raise ValueError("Initial PERCO is in the unsafe topohyd_init log-transform interval")
    _, hrus = _checked_table(model / "hru-data.hru",
        "id name topo hydro soil lu_mgt soil_plant_init surf_stor snow field".split(), exact=True)
    _, connections = _checked_table(model / "hru.con",
        "id name gis_id area lat lon elev hru wst cst ovfl rule out_tot".split())
    ids = [int(row["id"]) for row in connections]
    if ids != list(range(1, len(ids) + 1)):
        raise ValueError("Modern profile requires consecutive connected HRU IDs")
    by_id = {int(row["id"]): row for row in hrus}
    if len(by_id) != len(hrus):
        raise ValueError("Duplicate HRU database IDs")
    for row in connections:
        hru = by_id.get(int(row["hru"]))
        if not hru or hru["hydro"] not in names:
            raise ValueError("Unresolved connected HRU/hydrology reference")
    _checked_table(model / "time.sim",
        "day_start yrc_start day_end yrc_end step".split(), exact=True)
    return {"profile": expected_version, "export_banner": banner,
            "layout_checked": "standard_hru_hydrology_and_connections",
            "hydrology_pet_column": expected_hyd[13],
            "connected_hrus": len(connections)}


def read_output_header(stream, expected_version, kind):
    """Return (banner, header, units_by_variable); consume three output lines.

    The 62 HRU water-balance table has two trailing
    label columns without units. No other arbitrary missing units are accepted.
    """
    if expected_version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Unsupported SWAT+ version {expected_version}")
    banner = stream.readline().strip()
    header = stream.readline().split()
    units = stream.readline().split()
    if not version_ok(banner, expected_version):
        raise ValueError(f"Unexpected output model version: {banner}")
    if header[:7] != IDENTITY_COLUMNS:
        raise ValueError(f"Unexpected {kind} identity/date header: {header[:7]}")
    variables = [name for name in header[7:] if name != "null"]
    if expected_version == "62.0.1" and kind in {"hru_wb", "basin_wb"}:
        if variables[-2:] != ["plant_cov", "mgt_ops"]:
            raise ValueError("Unexpected modern water-balance label columns")
        variables = variables[:-2]
        if kind == "basin_wb":
            # 62.0.1 reuses the HRU header but basin records do not serialize
            # the plant/mgt labels. Canonicalize only this proven mismatch.
            header = header[:-2]
    if len(units) != len(variables):
        raise ValueError(f"Unexpected {kind} units/column count: {len(units)}/{len(variables)}")
    unit_map = dict(zip(variables, units))
    required = {"channel_sd": {"flo_out": "m^3/s"},
                "basin_wb": {"precip": "mm", "et": "mm", "perc": "mm"},
                "hru_wb": {"precip": "mm", "et": "mm", "perc": "mm"}}
    known = {"channel_sd", "basin_wb", "hru_wb", "aquifer", "basin_aqu", "reservoir", "basin_res"}
    if kind not in known or any(unit_map.get(key) != value for key, value in required.get(kind, {}).items()):
        raise ValueError(f"Unexpected {kind} physical variable/units")
    return banner, header, unit_map


def validate_output_row(fields, header, kind, expected_version):
    """Fail closed on malformed row width; return the same token list."""
    if len(fields) != len(header):
        raise ValueError(f"{kind} output column count mismatch: {len(fields)}/{len(header)}")
    return fields


def read_checker(model, expected_version):
    """Return the header and rows of pre-calibration HRU diagnostics."""
    rows = _lines(Path(model) / "checker.out")
    if len(rows) < 4 or not version_ok(rows[0], expected_version):
        raise ValueError("Missing checker records or wrong model version")
    header = rows[1].split()
    records = [line.split() for line in rows[3:] if line.strip()]
    if expected_version == "62.0.1" and header != CHECKER_COLUMNS:
        raise ValueError(f"Unverified modern checker.out layout: {header}")
    for row in records:
        if len(row) != len(header) + 2:
            raise ValueError("checker.out column count mismatch")
        if any(not math.isfinite(float(value)) for value in row[2:]):
            raise ValueError("Non-finite initialized HRU value in checker.out")
    return header, records
