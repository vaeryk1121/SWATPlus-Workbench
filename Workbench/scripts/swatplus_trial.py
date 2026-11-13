"""Run one isolated SWAT+ parameter trial (standard library only).

CLI: python -X utf8 swatplus_trial.py --spec trial_spec.json
Paths are absolute or relative to spec['root']; root defaults to the spec's
directory. A trial uses an existing clean TxtInOut export, never edits it, and
does not perform optimization or scoring. min/max bound the resulting input
values, not the multiplier or percentage supplied to a relative edit.
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import time
from swatplus_profiles import SUPPORTED_VERSIONS, read_output_header, validate_input_layout


SUPPORTED = {
    "hydrology.hyd": {"esco": (0.0, 1.0), "perco": (0.0, 1.0)},
    "cntable.lum": {name: (0.0, 100.0) for name in ("cn_a", "cn_b", "cn_c", "cn_d")},
    "aquifer.aqu": {"alpha_bf": (0.0, 1.0), "bf_max": (0.0, None)},
}


def read_lines(path):
    return path.read_bytes().decode("utf-8-sig").replace("\r\r\n", "\n").replace("\r\n", "\n").splitlines()


def write_lines(path, rows):
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def finite(value, label):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def replace_token(line, index, value):
    token = list(re.finditer(r"\S+", line))[index]
    return line[:token.start()] + str(value).rjust(token.end() - token.start()) + line[token.end():]


def table(path):
    rows = read_lines(path)
    if len(rows) < 3:
        raise ValueError(f"Empty or malformed input table: {path}")
    return rows, rows[1].split()


def version_ok(banner, expected):
    return re.search(r"(?<!\d)" + re.escape(expected) + r"(?![\d.])", banner) is not None


RUN_ARTIFACT_NAMES = {"success.fin", "checker.out", "diagnostics.out"}
WEATHER_INDEX_FILES = ("pcp.cli", "tmp.cli", "slr.cli", "hmd.cli", "wnd.cli")
WEATHER_PATH_ROWS = ("pcp_path", "tmp_path", "slr_path", "hmd_path", "wnd_path", "out_path")


def relative_key(value):
    """Canonical comparison key for a relative reference inside an export.

    Windows exports write the same file as series\\demand_day.txt,
    series/demand_day.txt or ./series/demand_day.txt. The dependency checker
    and the snapshot copier must both key references through this function,
    or a referenced input can be mistaken for a run artifact. Case folding
    follows the running system via os.path.normcase (case-insensitive on
    Windows). Drives, roots and '..' are rejected so protection and the
    existence checks never reach outside the export.
    """
    text = str(PureWindowsPath(str(value)))
    path = PureWindowsPath(text)
    if path.drive or path.root or path.is_absolute():
        raise ValueError(f"Reference {value!r} is not a relative path inside the export")
    parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Reference {value!r} does not name a file inside the export")
    return os.path.normcase("/".join(parts))


def referenced_input_files(source):
    """Map the files an export actually references, with how each is used.

    Covers the file.cio table itself, weather station files named inside the
    pcp/tmp/slr/hmd/wnd .cli indexes (honoring relative *_path prefixes), and
    data files named inside the first recall file. The engine opens recall
    data for every record except typ=4 constants (recall_read.f90), so typ=4
    entries are protected from cleanup but not required to exist. Keys are
    normalized through relative_key; the original reference string is kept in
    "path" so existence checks still open exactly the referenced file.
    Returns {"files": {key: {"origin", "path", "required"}}, "prefixes",
    "weather_files", "recall_data_files"}.
    """
    source = Path(source)
    rows = {}
    for line in read_lines(source / "file.cio")[1:]:
        fields = line.split()
        if fields:
            rows[fields[0]] = fields[1:]
    referenced = {}

    def add(value, origin, required):
        referenced[relative_key(value)] = {"origin": origin, "path": str(value), "required": required}

    for label, filenames in rows.items():
        if label in WEATHER_PATH_ROWS:
            continue
        for filename in filenames:
            if filename.lower() != "null":
                add(filename, f"file.cio:{label}", True)
    prefixes = {}
    for label in WEATHER_PATH_ROWS:
        value = (rows.get(label) or ["null"])[0]
        if value.lower() != "null":
            try:
                relative_key(value)
            except ValueError as error:
                raise ValueError(f"{label}={value} points outside the TxtInOut tree; export the weather files "
                                 "inside the model directory with a relative path") from error
            prefixes[label[:-5]] = value
    weather_files = 0
    for index_name in WEATHER_INDEX_FILES:
        if not (source / index_name).is_file():
            continue
        prefix = prefixes.get(index_name.split(".")[0], "")
        for line in read_lines(source / index_name)[1:]:
            token = line.split()
            if not token or token[0].lower() in ("null", "filename"):
                continue
            weather_files += 1
            try:
                add(f"{prefix}/{token[0]}" if prefix else token[0], index_name, True)
            except ValueError as error:
                raise ValueError(f"{index_name}: station file {token[0]!r} must be a relative path inside "
                                 "the model directory") from error
    recall_name = (rows.get("recall") or ["null"])[0]
    recall_data_files = 0
    if recall_name.lower() != "null" and (source / recall_name).is_file():
        for line in read_lines(source / recall_name)[2:]:
            fields = line.split()
            if len(fields) >= 4 and fields[3].lower() != "null":
                add(fields[3], f"{recall_name}:{fields[1]}", fields[2] != "4")
                recall_data_files += 1
    return {"files": referenced, "prefixes": prefixes,
            "weather_files": weather_files, "recall_data_files": recall_data_files}


def check_clean_source(source, expected_version, calibration="reject"):
    """Validate an exported TxtInOut tree before it is used as a trial source.

    Files the export references (file.cio table, weather indexes, recall data)
    count as inputs whatever their name, so a recall series named e.g.
    demand_day.txt is never mistaken for a prior model-run output. Required
    references must exist; an enabled calibration.cal is rejected by default
    and kept unchanged with calibration='carry'.
    """
    if not source.is_dir():
        raise FileNotFoundError(source)
    references = referenced_input_files(source)
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if relative_key(relative) in references["files"]:
            continue
        if path.name in RUN_ARTIFACT_NAMES or re.search(r"_(?:day|mon|yr|aa)\.txt$", path.name):
            raise ValueError(f"Use a clean input export, not a prior model run: {relative}")
    cio = read_lines(source / "file.cio")
    if not version_ok(cio[0], expected_version):
        raise ValueError(f"Input export version does not match {expected_version}: {cio[0]}")
    rows = {}
    for line in cio[1:]:
        fields = line.split()
        if fields:
            rows[fields[0]] = fields[1:]
    if calibration not in ("reject", "carry"):
        raise ValueError("calibration policy must be reject or carry")
    active = rows.get("chg", [])
    carried = None
    if len(active) > 1 and active[1].lower() != "null":
        carried = active[1]
        if calibration == "reject":
            raise ValueError(
                f"Active calibration.cal is not allowed in a clean trial source; the export enables its own "
                f"parameter changes ({carried}). Use a clean re-export for parameter search, or choose "
                "calibration_policy='carry' to run this model unchanged without adding parameter edits")
        if not (source / carried).is_file():
            raise FileNotFoundError(f"Enabled calibration file missing: {carried}")
    missing = []
    for kind, prefix in references["prefixes"].items():
        if not (source / prefix).is_dir():
            missing.append(f"{kind}_path: {prefix}")
    for info in references["files"].values():
        if info["required"] and not (source / info["path"]).is_file():
            missing.append(f"{info['origin']}: {info['path']}")
    if missing:
        raise FileNotFoundError("Input dependencies missing from the export: " + "; ".join(sorted(set(missing))))
    layout = validate_input_layout(source, expected_version)
    layout["dependencies"] = {"weather_station_files": references["weather_files"],
                              "recall_data_files": references["recall_data_files"],
                              "referenced_inputs": len(references["files"]),
                              "subdirectory_prefixes": references["prefixes"]}
    if carried:
        layout["active_calibration"] = {"file": carried, "mode": "carried unchanged; no parameter edits may be added"}
    return layout


def configure_period_and_print(model, spec):
    policy = spec.get("print_policy", "preserve")
    if policy not in ("preserve", "reset"):
        raise ValueError("print_policy must be preserve or reset")
    rows, header = table(model / "time.sim")
    required = ["day_start", "yrc_start", "day_end", "yrc_end", "step"]
    if header != required:
        raise ValueError(f"Unexpected time.sim header: {header}")
    rows[2] = f"0 {spec['start_year']} 0 {spec['end_year']} 0"
    write_lines(model / "time.sim", rows)
    rows = read_lines(model / "print.prt")
    if rows[1].split() != ["nyskip", "day_start", "yrc_start", "day_end", "yrc_end", "interval"]:
        raise ValueError("Unexpected print.prt period header")
    rows[2] = f"{spec['warmup_years']} 0 0 0 0 1"
    if policy == "preserve":
        # Keep the exported crop-yield/CSV/section switches exactly as the
        # source model set them; only turn evaluation-required outputs on.
        positions = [i for i, row in enumerate(rows) if row.split() and row.split()[0] == "csvout"]
        if len(positions) != 1:
            raise ValueError("print.prt missing csvout section")
        flags = rows[positions[0] + 1].split()
        if spec.get("csv_output"):
            flags[0] = "y"
            rows[positions[0] + 1] = " ".join(flags)
    else:
        for section in ("csvout", "crop_yld"):
            positions = [i for i, row in enumerate(rows) if row.split() and row.split()[0] == section]
            if len(positions) != 1:
                raise ValueError(f"print.prt missing {section} section")
            i = positions[0]
            rows[i + 1] = " ".join("y" if section=="csvout" and j==0 and spec.get("csv_output") else "n" for j,_ in enumerate(rows[i].split()))
    positions = [i for i, row in enumerate(rows) if row.split() and row.split()[0] == "objects"]
    if len(positions) != 1 or rows[positions[0]].split()[1:] != ["daily", "monthly", "yearly", "avann"]:
        raise ValueError("Unexpected print.prt objects header")
    outlet_object = (spec.get("outlet") or {}).get("object", "channel_sd")
    if outlet_object not in ("channel_sd", "reservoir"):
        raise ValueError("outlet.object must be channel_sd or reservoir")
    seen = set()
    requested = {}
    for target in spec.get("output_targets", []):
        requested.setdefault(target["object"], set()).add(target["frequency"])
    for i in range(positions[0] + 1, len(rows)):
        fields = rows[i].split()
        if not fields:
            continue
        obj = fields[0]
        seen.add(obj)
        enable = set()
        if obj == "basin_wb":
            enable.add("monthly"); enable.add("yearly")
        elif obj == outlet_object:
            # The selected evaluation object must produce output at the trial
            # frequency, or the outlet extraction has nothing to read.
            enable.add(spec["frequency"])
        enable |= requested.get(obj, set())
        if not enable <= {"daily", "monthly", "yearly", "avann"}:
            raise ValueError(f"Unsupported requested output frequency for {obj}")
        if policy == "preserve":
            flags = fields[1:5]
            if len(flags) != 4:
                raise ValueError(f"Unexpected print.prt flags for {obj}")
        else:
            flags = ["n", "n", "n", "n"]
        order = ("daily", "monthly", "yearly", "avann")
        for frequency in enable:
            flags[order.index(frequency)] = "y"
        rows[i] = f"{obj:24s} {' '.join(flags)}"
    required_rows = {"basin_wb", "channel_sd", outlet_object}
    if not required_rows <= seen:
        raise ValueError(f"print.prt lacks rows required by this trial: {sorted(required_rows - seen)}")
    if not set(requested).issubset(seen):
        raise ValueError(f"print.prt lacks requested objects: {set(requested)-seen}")
    write_lines(model / "print.prt", rows)


def apply_parameters(model, definitions):
    reports = []
    edited = set()
    for definition in definitions:
        filename = definition["file"]
        column = definition["column"].lower()
        if filename not in SUPPORTED or column not in SUPPORTED[filename]:
            raise ValueError(f"Unsupported parameter: {filename}:{column}; use actual supported headers")
        method = definition["method"].lower()
        if method == "absval":
            method = "absolute"
        if method == "multiply":
            method = "factor"
        allowed = {"factor", "pctchg"} if filename == "cntable.lum" else {"absolute"}
        if method not in allowed:
            raise ValueError(f"{filename}:{column} supports {sorted(allowed)}, not {method}")
        value = finite(definition["value"], "Parameter value")
        if method == "factor" and value <= 0 or method == "pctchg" and value <= -100:
            raise ValueError("Relative CN edits must preserve a positive multiplier")
        physical_low, physical_high = SUPPORTED[filename][column]
        lower = finite(definition.get("min", physical_low), "Minimum")
        upper = definition.get("max", physical_high)
        upper = finite(upper, "Maximum") if upper is not None else None
        if lower < physical_low or physical_high is not None and (upper is None or upper > physical_high):
            raise ValueError(f"Configured bounds exceed supported bounds for {column}")
        if upper is not None and upper < lower:
            raise ValueError("Parameter min exceeds max")
        selected = definition.get("names")
        if selected is not None and (not isinstance(selected, list) or not selected or not all(isinstance(x, str) for x in selected)):
            raise ValueError("Optional names must be a nonempty list of input record names")
        selected = set(selected) if selected is not None else None
        rows, header = table(model / filename)
        if column not in header or "name" not in header:
            raise ValueError(f"Missing column/name in {filename}: {header}")
        index, name_index = header.index(column), header.index("name")
        original, written, found = [], [], set()
        modified = 0
        for line_index in range(2, len(rows)):
            fields = rows[line_index].split()
            if not fields:
                continue
            name = fields[name_index]
            if selected is not None and name not in selected:
                continue
            identity = (filename, column, name)
            if identity in edited:
                raise ValueError(f"Overlapping edits would accumulate for {identity}")
            edited.add(identity)
            old = finite(fields[index], f"{filename}:{name}:{column}")
            new = value if method == "absolute" else old * (value if method == "factor" else 1 + value / 100)
            if not math.isfinite(new) or new < lower or upper is not None and new > upper:
                raise ValueError(f"{filename}:{name}:{column} would be {new}, outside [{lower}, {upper}]; no clipping applied")
            # Nine decimals retain optimizer values while remaining readable by
            # the Fortran list-directed input parser used by both revisions.
            token = f"{new:.9f}"
            actual = float(token)
            rows[line_index] = replace_token(rows[line_index], index, token)
            original.append(old)
            written.append(actual)
            modified += actual != old
            found.add(name)
        if not found or selected is not None and found != selected:
            raise ValueError(f"No parameter records or unknown names in {filename}: {sorted((selected or set()) - found)}")
        write_lines(model / filename, rows)
        # Read the actual written values; this also detects formatting mistakes.
        reread, reread_header = table(model / filename)
        actual_values = [finite(row.split()[reread_header.index(column)], column) for row in reread[2:] if row.strip() and row.split()[reread_header.index("name")] in found]
        if actual_values != written:
            raise ValueError(f"Parameter write-back mismatch in {filename}:{column}")
        reports.append({"file": filename, "column": column, "method": method, "value": value,
                        "names": sorted(selected) if selected is not None else "all",
                        "matched_records": len(written), "modified_records": modified,
                        "original_range": [min(original), max(original)], "written_range": [min(written), max(written)],
                        "bounds": [lower, upper], "writeback_verified": True})
    return reports


def check_internal_hydrology(model, definitions, expected_version):
    columns = sorted({d["column"].lower() for d in definitions if d["file"] == "hydrology.hyd"})
    if not columns:
        return []
    checker = read_lines(model / "checker.out")
    if len(checker) < 4 or not version_ok(checker[0], expected_version):
        raise ValueError("Missing checker records or wrong model version")
    check_header = checker[1].split()
    check_rows = [row.split() for row in checker[3:] if row.strip()]
    hyd_lines, hyd_header = table(model / "hydrology.hyd")
    hyd = {row.split()[hyd_header.index("name")]: dict(zip(hyd_header, row.split())) for row in hyd_lines[2:] if row.strip()}
    hru_lines, hru_header = table(model / "hru-data.hru")
    hrus = {int(row.split()[hru_header.index("id")]): dict(zip(hru_header, row.split())) for row in hru_lines[2:] if row.strip()}
    con_lines, con_header = table(model / "hru.con")
    con = [dict(zip(con_header, row.split())) for row in con_lines[2:] if row.strip()]
    if [int(row["id"]) for row in con] != list(range(1, len(con) + 1)) or len(check_rows) != len(con):
        raise ValueError("Cannot align checker.out records to hru.con IDs")
    reports = []
    for column in columns:
        if column not in check_header:
            raise ValueError(f"checker.out lacks {column}")
        values = []
        for connection, check in zip(con, check_rows):
            hru = hrus[int(connection["hru"])]
            expected = float(hyd[hru["hydro"]][column])
            actual = finite(check[check_header.index(column) + 2], f"checker:{column}")
            # checker.out prints four decimals; do not demand unavailable precision.
            if not math.isclose(actual, expected, rel_tol=0, abs_tol=0.000051):
                raise ValueError(f"Internal {column} for HRU {connection['id']} is {actual}; input {expected}")
            values.append(actual)
        reports.append({"column": column, "file": "checker.out", "hru_records": len(values),
                        "range": [min(values), max(values)], "tolerance": 0.000051, "verified": True})
    return reports


def expected_dates(spec):
    first_year = spec["start_year"] + spec["warmup_years"]
    if spec["frequency"] == "monthly":
        for year in range(first_year, spec["end_year"] + 1):
            for month in range(1, 13):
                yield dt.date(year, month, 1)
    else:
        date, end = dt.date(first_year, 1, 1), dt.date(spec["end_year"], 12, 31)
        while date <= end:
            yield date
            date += dt.timedelta(days=1)


def extract_outlet(model, work, spec):
    outlet = dict(spec["outlet"])
    if outlet.get("object", "channel_sd") == "reservoir":
        return extract_reservoir_outlet(model, work, spec)
    source = model / ("channel_sd_day.txt" if spec["frequency"] == "daily" else "channel_sd_mon.txt")
    destination = work / "outlet.csv"
    records, banner = _write_object_series(source, destination, spec, "channel_sd", "flo_out", "flow_m3_s")
    count, first, last = records["count"], records["first"], records["last"]
    return {"banner": banner, "outlet_csv": str(destination), "records": count,
            "first_date": first, "last_date": last, "frequency": spec["frequency"], "units": "m^3/s",
            "outlet_object": "channel_sd"}


def extract_reservoir_outlet(model, work, spec):
    """Extract a terminal-reservoir outlet series without inventing a unit.

    The engine prints reservoir flo columns as volumes (m^3); monthly rows are
    sums of the daily values. Values are written exactly as printed and the
    result states the printed unit, so this series must not be scored against
    m^3/s channel discharge without an explicit conversion decision.
    """
    source = model / ("reservoir_day.txt" if spec["frequency"] == "daily" else "reservoir_mon.txt")
    destination = work / "outlet.csv"
    records, banner = _write_object_series(source, destination, spec, "reservoir", "flo_out", "flo_out_m3",
                                           value_label="reservoir outflow volume")
    return {"banner": banner, "outlet_csv": str(destination), "records": records["count"],
            "first_date": records["first"], "last_date": records["last"], "frequency": spec["frequency"],
            "units": records["units"], "outlet_object": "reservoir",
            "units_note": "engine reservoir output prints volumes (m^3); monthly rows are sums of daily values; no conversion applied"}


def _write_object_series(source, destination, spec, kind, variable, column, value_label="Flow"):
    expected = iter(expected_dates(spec))
    count, first, last = 0, None, None
    units = None
    with source.open(encoding="utf-8-sig") as stream, destination.open("w", encoding="utf-8-sig", newline="") as output:
        banner, header, unit_map = read_output_header(stream, spec["expected_version"], kind)
        if variable not in unit_map:
            raise ValueError(f"{source.name} lacks the {variable} column")
        units = unit_map[variable]
        required = ["unit", "gis_id", "name", "yr", "mon", "day", "jday", variable]
        indices = {key: header.index(key) for key in required}
        writer = csv.writer(output)
        writer.writerow(["date", column])
        for number, line in enumerate(stream, 4):
            fields = line.split()
            if not fields:
                continue
            if len(fields) != len(header):
                raise ValueError(f"{source.name}:{number}: output column count mismatch")
            if int(fields[indices["unit"]]) != spec["outlet"]["unit"]:
                continue
            if int(fields[indices["gis_id"]]) != spec["outlet"]["gis_id"] or fields[indices["name"]] != spec["outlet"]["name"]:
                raise ValueError("Outlet unit, GIS ID and name do not identify the same record")
            date = dt.date(int(fields[indices["yr"]]), int(fields[indices["mon"]]), int(fields[indices["day"]]) if spec["frequency"] == "daily" else 1)
            if date != next(expected, None):
                raise ValueError(f"Outlet dates missing, duplicated, unordered or outside configured period at {date}")
            if spec["frequency"] == "daily" and int(fields[indices["jday"]]) != date.timetuple().tm_yday:
                raise ValueError("Daily outlet day-of-year disagrees with date")
            value = finite(fields[indices[variable]], f"{value_label} at {date}")
            if value < 0:
                raise ValueError(f"Negative outlet {value_label.lower()} at {date}")
            writer.writerow([date.isoformat(), value])
            count += 1
            first = first or date.isoformat()
            last = date.isoformat()
    if next(expected, None) is not None or not count:
        raise ValueError("Outlet output is incomplete or empty")
    return {"count": count, "first": first, "last": last, "units": units}, banner


def check_basin_outputs(model, spec):
    results = {}
    for period, suffix, per_year in (("monthly", "mon", 12), ("yearly", "yr", 1)):
        path = model / f"basin_wb_{suffix}.txt"
        count = 0
        with path.open(encoding="utf-8-sig") as stream:
            _, header, _ = read_output_header(stream, spec["expected_version"], "basin_wb")
            for line in stream:
                fields = line.split()
                if not fields:
                    continue
                if len(fields) != len(header) or int(fields[header.index("unit")]) != 1:
                    raise ValueError(f"Malformed basin record in {path.name}")
                for variable in ("precip", "et", "surq_gen", "latq", "perc", "wateryld"):
                    finite(fields[header.index(variable)], variable)
                count += 1
        expected = (spec["end_year"] - spec["start_year"] - spec["warmup_years"] + 1) * per_year
        if count != expected:
            raise ValueError(f"Incomplete {period} basin output: {count} rather than {expected}")
        results[period] = {"path": str(path), "records": count}
    return results


def run_trial(spec, base_dir=None, cancel_event=None):
    """Execute a spec dictionary; return the same result written to trial_result.json.

    Existing work directories are rejected. Exceptions are re-raised after
    saving status/error and elapsed time, so an optimizer can record failed
    trials while the original model and model console log remain available.
    """
    base = Path(base_dir or ".").resolve()
    root = (base / spec.get("root", ".")).resolve()
    source, executable, work = [(root / spec[key]).resolve() for key in ("input_dir", "executable", "work_dir")]
    if source == work or source in work.parents or work in source.parents:
        raise ValueError("Trial work directory must be separate from the source input tree")
    if spec["expected_version"] not in SUPPORTED_VERSIONS:
        raise ValueError(f"This backend supports the checked {sorted(SUPPORTED_VERSIONS)} input layouts")
    parameter_backend = spec.get("parameter_backend", "direct_file")
    if parameter_backend not in {"direct_file", "native_calibration"}:
        raise ValueError("parameter_backend must be direct_file or native_calibration")
    if spec["frequency"] not in {"daily", "monthly"}:
        raise ValueError("frequency must be daily or monthly")
    if any(type(spec[key]) is not int for key in ("start_year", "end_year", "warmup_years")):
        raise ValueError("Simulation years and warmup_years must be integers")
    if spec["warmup_years"] < 0 or spec["start_year"] + spec["warmup_years"] > spec["end_year"]:
        raise ValueError("Invalid simulation/warmup period")
    dt.date(spec["start_year"], 1, 1)
    dt.date(spec["end_year"], 12, 31)
    if spec["outlet"].get("object", "channel_sd") not in ("channel_sd", "reservoir"):
        raise ValueError("outlet.object must be channel_sd or reservoir")
    for key in ("unit", "gis_id"):
        if type(spec["outlet"][key]) is not int:
            raise ValueError(f"outlet.{key} must be an integer")
    calibration_policy = spec.get("calibration_policy", "reject")
    if calibration_policy not in ("reject", "carry"):
        raise ValueError("calibration_policy must be reject or carry")
    if calibration_policy == "carry" and spec.get("parameters"):
        raise ValueError("calibration_policy=carry keeps the source's enabled calibration.cal; "
                         "it cannot be combined with parameter edits in the same trial")
    if not executable.is_file():
        raise FileNotFoundError(executable)
    timeout = finite(spec.get("timeout_seconds", 600), "timeout_seconds")
    if timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    layout = check_clean_source(source, spec["expected_version"], calibration_policy)
    work.mkdir(parents=True, exist_ok=False)
    model = work / "TxtInOut"
    started = time.perf_counter()
    result = {"status": "running", "input_dir": str(source), "executable": str(executable),
              "work_dir": str(work), "model_dir": str(model), "expected_version": spec["expected_version"],
              "engine_info": spec.get("engine_info", {}),
              "parameter_backend": parameter_backend,
              "input_layout": {key: value for key, value in layout.items() if key != "active_calibration"},
              "input_calibration": layout.get("active_calibration", {"mode": "none"}),
              "outlet_object": spec["outlet"].get("object", "channel_sd"),
              "exit_code": None, "elapsed_seconds": None, "console_log": str(work / "console.log")}
    write_json(work / "trial_spec.json", spec)
    try:
        shutil.copytree(source, model)
        configure_period_and_print(model, spec)
        if not spec.get("parameters"):
            # No requested edits: never touch calibration-related files, so a
            # carried source calibration stays exactly as exported.
            result["parameter_changes"] = []
        elif parameter_backend == "native_calibration":
            import native_calibration
            result["parameter_changes"] = native_calibration.apply_parameters(model, spec["parameters"], spec["expected_version"])
        else:
            result["parameter_changes"] = apply_parameters(model, spec["parameters"])
        result["modified_records"] = sum(report["modified_records"] for report in result["parameter_changes"])
        model_start = time.perf_counter()
        try:
            with (work / "console.log").open("w", encoding="utf-8") as log:
                if cancel_event is None:
                    completed = subprocess.run([str(executable)], cwd=model, stdin=subprocess.DEVNULL,
                                               stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False,
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    result["exit_code"] = completed.returncode
                else:
                    with subprocess.Popen([str(executable)],cwd=model,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                                          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)) as process:
                        try:
                            deadline=time.perf_counter()+timeout
                            while process.poll() is None:
                                if cancel_event.wait(.2):raise InterruptedError("模型运行已停止")
                                if time.perf_counter()>deadline:raise subprocess.TimeoutExpired(str(executable),timeout)
                        except BaseException:
                            process.kill();process.wait();raise
                        result["exit_code"]=process.returncode
        finally:
            result["elapsed_seconds"] = round(time.perf_counter() - model_start, 3)
        if result["exit_code"] != 0 or not (model / "success.fin").is_file():
            raise RuntimeError(f"SWAT+ did not finish successfully; inspect {work / 'console.log'}")
        if parameter_backend == "native_calibration":
            import native_calibration
            result["internal_parameter_checks"] = []
            result["initialization_checks"] = native_calibration.check_initialization(model, result["parameter_changes"], spec["expected_version"])
        else:
            result["internal_parameter_checks"] = check_internal_hydrology(model, spec.get("parameters", []), spec["expected_version"])
        result.update(extract_outlet(model, work, spec))
        if spec.get("output_targets"):
            from multi_observation import extract_targets
            result["target_csvs"]=extract_targets(model,work,spec)
        result["basin_outputs"] = check_basin_outputs(model, spec)
        if result["input_calibration"].get("mode", "none") != "none":
            result["parameter_verification_note"] = ("The source model's enabled calibration.cal was carried unchanged "
                "and executed by SWAT+ as part of the input; this trial added no parameter edits, so no parameter verification applies.")
        elif not spec.get("parameters"):
            result["parameter_verification_note"] = "No parameter edits were requested; the source calibration setting was preserved."
        else:
            result["parameter_verification_note"] = ("Native calibration.cal contents and connected HRU scope were verified; "
                "SWAT+ applies them after initialization. checker.out is pre-calibration and does not verify post-change internal values."
                if parameter_backend == "native_calibration" else
                "Hydrology edits are checked against initialized HRU values in checker.out; CN and aquifer edits are verified in written input only.")
        result["status"] = "completed"
        return result
    except Exception as error:
        result.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        result["total_elapsed_seconds"] = round(time.perf_counter() - started, 3)
        write_json(work / "trial_result.json", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    path = args.spec.resolve()
    spec = json.loads(path.read_text(encoding="utf-8-sig"))
    result = run_trial(spec, path.parent)
    print(json.dumps({"status": result["status"], "work_dir": result["work_dir"],
                      "elapsed_seconds": result["elapsed_seconds"], "records": result["records"],
                      "modified_records": result["modified_records"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
