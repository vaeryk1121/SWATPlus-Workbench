"""Version-aware adapter for the SWAT+ native calibration.cal interface.

Explicit native_name definitions cover HRU, soil, aquifer, channel and snow
processes. The old hydrology.hyd ESCO/PERCO schema remains supported. Bounds
come from the model's own cal_parms.cal, not a SWAT-2012 parameter list.
Native changes are applied after initialization: checker.out is NOT a dump
of the changed values. See config/native_parameter_catalog.json for provenance.
"""

import math
import json
from pathlib import Path
import re
from swatplus_profiles import SUPPORTED_VERSIONS, read_checker, validate_input_layout


SUPPORTED_COLUMNS = {"esco", "perco"}
CATALOG_PATH = Path(__file__).resolve().parents[1] / "config/native_parameter_catalog.json"


def parameter_catalog(expected_version=None):
    """Return entries reviewed for the selected version, or the whole catalog."""
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if expected_version is not None:
        if expected_version not in SUPPORTED_VERSIONS:
            raise ValueError(f"Native calibration is not checked for {expected_version}")
        catalog["parameters"] = [p for p in catalog["parameters"] if expected_version in p["model_absolute_bounds"]]
    return catalog


def validate_definitions(definitions, model=None, expected_version=None):
    """Validate parameter metadata without writing files or running SWAT+.

    ``lower/upper`` are search values in the selected operator's units.
    ``min/max`` are optional final absolute bounds; relative operations use
    exactly the model bounds (SWAT+ clamps internally). With model=None only
    catalog/schema checks are possible. The actual model database wins at run time.
    """
    catalog = {p["native_name"]: p for p in parameter_catalog()["parameters"]}
    database = _database(Path(model))[0] if model is not None else None
    output = []
    if expected_version is not None and expected_version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Native calibration is not checked for {expected_version}")
    if model is not None and expected_version == "62.0.1":
        validate_input_layout(model, expected_version)
    for definition in definitions:
        explicit = "native_name" in definition
        name = str(definition.get("native_name", definition.get("column", ""))).lower()
        if not explicit and (definition.get("file") != "hydrology.hyd" or name not in SUPPORTED_COLUMNS):
            raise ValueError("Legacy native schema supports only hydrology.hyd ESCO/PERCO; "
                             "CN lookup-table factors are not native HRU cn2 changes. Use native_name.")
        if name not in catalog:
            raise ValueError(f"Unsupported native parameter: {name}")
        item = catalog[name]
        if expected_version is not None and expected_version not in item["model_absolute_bounds"]:
            raise ValueError(f"Native {name} is not reviewed for SWAT+ {expected_version}")
        method = str(definition.get("method", "absval")).lower()
        method = {"absolute": "absval"}.get(method, method)
        if method not in item["allowed_methods"] or not explicit and method != "absval":
            raise ValueError(f"Native {name} supports {item['allowed_methods']} with explicit native_name; "
                             "legacy definitions support absolute/absval only")
        obj_type = definition.get("object_type", item["object_type"])
        if obj_type != item["object_type"]:
            raise ValueError(f"Native {name} object_type must be {item['object_type']}, not {obj_type}")
        if explicit and ("file" in definition or "column" in definition or "names" in definition):
            raise ValueError("Do not mix native_name/object_ids and legacy file/column/names selectors")
        object_ids = definition.get("object_ids")
        if object_ids is not None and (not isinstance(object_ids, list) or not object_ids or
                any(type(i) is not int or i <= 0 for i in object_ids) or len(set(object_ids)) != len(object_ids)):
            raise ValueError("object_ids must be a nonempty list of unique positive integers")
        layer = definition.get("layer", [0, 0])
        if (not isinstance(layer, list) or len(layer) != 2 or any(type(i) is not int or i < 0 for i in layer)
                or layer[1] and layer[1] < max(1, layer[0])):
            raise ValueError("layer must be [first, last], [0, 0] means all soil layers")
        if name == "alb":
            if layer == [0, 0]:
                layer = [1, 1]
            if layer != [1, 1]:
                raise ValueError("Native alb affects only surface soil layer [1, 1]")
        if obj_type != "sol" and layer != [0, 0]:
            raise ValueError("layer is supported only for soil parameters")
        if any(definition.get(key, 0) for key in ("year1", "year2", "day1", "day2", "conditions")):
            raise ValueError("Climate/time/condition selectors are not supported by this adapter")
        for pair in (("lower", "upper"), ("min", "max")):
            if (pair[0] in definition) != (pair[1] in definition):
                raise ValueError(f"Specify both {pair[0]} and {pair[1]}")
            if pair[0] in definition and _number(definition[pair[0]], pair[0]) > _number(definition[pair[1]], pair[1]):
                raise ValueError(f"{pair[0]} must not exceed {pair[1]}")
        if "value" in definition:
            value = _number(definition["value"], "Parameter value")
            if "lower" in definition and not _number(definition["lower"], "lower") <= value <= _number(definition["upper"], "upper"):
                raise ValueError(f"Native {name} value is outside search bounds")
        else:
            value = None
        if method == "pctchg" and any(_number(definition[key], key) < -100 for key in ("value", "lower") if key in definition):
            raise ValueError("pctchg uses percent, and changes below -100% are unsupported")
        entry = database.get(name) if database is not None else None
        if database is not None and (not entry or entry["obj_typ"] != obj_type):
            raise ValueError(f"Model calibration database does not define {name} as {obj_type}")
        if entry:
            bounds = [_number(entry[key], key) for key in ("abs_min", "abs_max")]
        elif expected_version:
            bounds = item["model_absolute_bounds"][expected_version]
        else:
            bounds = None
        if bounds is not None:
            low, high = [_number(definition.get(key, default), key) for key, default in zip(("min", "max"), bounds)]
            if low < bounds[0] or high > bounds[1]:
                raise ValueError(f"Native {name} absolute bounds must fit model bounds {bounds}")
            if method == "absval":
                if any(not low <= _number(definition[key], key) <= high for key in ("value", "lower", "upper") if key in definition):
                    raise ValueError(f"Native {name} is outside configured/model bounds [{low}, {high}]")
            elif [low, high] != bounds:
                raise ValueError("Relative native changes use the model's absolute bounds; narrow search lower/upper instead")
        if name == "perco":
            for key in ("value", "lower", "upper"):
                if key in definition and 1e-9 < float(f"{_number(definition[key], key):.9f}") <= 1e-6:
                    raise ValueError("PERCO in (1e-9, 1e-6] is unsafe for this native log-transform")
            if "lower" in definition and _number(definition["lower"], "lower") <= 1e-9 and _number(definition["upper"], "upper") > 1e-9:
                raise ValueError("PERCO continuous search must avoid the unsafe near-zero interval; use lower > 1e-6")
        output.append({"native_name": name, "object_type": obj_type, "method": method,
                       "model_bounds": bounds, "layer": layer})
    return output


def _database(model):
    cio = _lines(model / "file.cio")
    positions = [i for i, line in enumerate(cio) if line.split() and line.split()[0] == "chg"]
    if len(positions) != 1:
        raise ValueError("file.cio must contain exactly one chg row")
    fields = cio[positions[0]].split()
    if len(fields) < 3 or fields[1].lower() == "null":
        raise ValueError("Native calibration requires cal_parms.cal in file.cio")
    path = model / fields[1]
    lines = _lines(path)
    if len(lines) < 4 or lines[2].split() != ["name", "obj_typ", "abs_min", "abs_max", "units"]:
        raise ValueError("Unverified calibration dictionary header")
    rows = _table(path, header_line=2)
    if int(lines[1]) != len(rows) or len({row["name"] for row in rows}) != len(rows):
        raise ValueError("Calibration dictionary count/unique names mismatch")
    return {row["name"]: row for row in rows}, cio, positions[0], fields


def _connected_ids(model, obj_type):
    if obj_type == "bsn":
        return [1]
    # These indices are the connected model object numbers, NOT GIS IDs or
    # soil database row numbers. sol changes iterate the connected HRUs.
    file = {"hru": "hru.con", "sol": "hru.con", "aqu": "aquifer.con", "rte": "chandeg.con"}[obj_type]
    rows = _table(model / file)
    ids = [int(row["id"]) for row in rows]
    if not ids or ids != list(range(1, len(rows) + 1)):
        raise ValueError(f"Native calibration requires consecutive connected IDs in {file}")
    return ids


def _soil_layers(model, selected):
    records = _lines(model / "soils.sol")[2:]
    soils, index = {}, 0
    while index < len(records):
        if not records[index].strip():
            index += 1
            continue
        cells = records[index].split()
        count = int(cells[1])
        if count <= 0 or len(records) < index + count + 1:
            raise ValueError("Invalid soils.sol layer records")
        soils[cells[0]] = count
        index += count + 1
    _, objects = _objects(model)
    return {number: soils[record["soil"]] for number, record, hyd in objects if number in selected}


def _explicit_change(model, definition, metadata, database, edited):
    name, typ, method = (metadata[key] for key in ("native_name", "object_type", "method"))
    available = _connected_ids(model, typ)
    ids = definition.get("object_ids") or available
    if not set(ids) <= set(available):
        raise ValueError(f"Unknown {typ} object IDs: {sorted(set(ids)-set(available))}")
    first, last = metadata["layer"]
    layer_counts = _soil_layers(model, ids) if typ == "sol" else {}
    identities = []
    for number in ids:
        if typ == "sol":
            stop = last or layer_counts[number]
            if max(1, first) > stop or stop > layer_counts[number]:
                raise ValueError(f"Requested soil layer is absent from HRU {number}")
            layers = range(max(1, first), stop + 1)
        else:
            layers = [0]
        identities.extend((name, number, layer) for layer in layers)
    if edited.intersection(identities):
        raise ValueError(f"Overlapping native calibration changes: {name}")
    edited.update(identities)
    if name in {"perco", "cn3_swf"}:
        _, objects = _objects(model)
        landuse = {row["name"]: row for row in _table(model / "landuse.lum")}
        for number, hru, hyd in objects:
            if number in ids:
                lu = landuse.get(hru["lu_mgt"])
                if not lu or lu.get("tile", "unknown").lower() != "null":
                    raise ValueError(f"Native {name} with tile drainage is not supported (HRU {number})")
    value = _number(definition["value"], "Parameter value")
    actual = float(f"{value:.9f}")
    bounds = metadata["model_bounds"]
    scope = "0" if definition.get("object_ids") is None else f"{len(ids)} " + " ".join(map(str, ids))
    line = f"{name} {method} {actual:.9f} 0 {first} {last} 0 0 0 0 {scope}"
    report = {"file": "calibration.cal", "source_file": None, "column": name, "native_name": name,
              "method": method, "value": value, "written_value": actual, "object_type": typ,
              "object_ids": ids, "layer": [first, last], "matched_records": len(identities),
              "modified_records": len(identities), "modified_records_estimated": True,
              "record_count_basis": "requested object/layer targets; internal post-change values are not dumped by this executable",
              "original_range": None, "written_range": [actual, actual] if method == "absval" else None,
              "bounds": [definition.get("min", bounds[0]), definition.get("max", bounds[1])],
              "model_bounds": bounds, "units": database[name]["units"],
              "relative_change_unit": "percent" if method == "pctchg" else None,
              "native_clamping": "SWAT+ clips relative changes to its model absolute bounds" if method != "absval" else None,
              "application_stage": "native post-initialization, before simulation",
              "writeback_verified": True, "internal_value_verified": False}
    return line, report


def _lines(path):
    return Path(path).read_text(encoding="utf-8-sig").splitlines()


def _table(path, header_line=1):
    lines = _lines(path)
    if len(lines) <= header_line + 1:
        raise ValueError(f"Empty native-calibration input table: {path}")
    header = lines[header_line].split()
    output = []
    for number, line in enumerate(lines[header_line + 1:], header_line + 2):
        if not line.strip():
            continue
        fields = line.split()
        # Connection tables have a variable number of routing destinations.
        if Path(path).suffix == ".con" and "out_tot" in header:
            width = header.index("out_tot") + 1
            if len(fields) < width or len(fields) != width + 4 * int(fields[width - 1]):
                raise ValueError(f"{path}:{number}: routing column count mismatch")
        elif len(fields) < len(header):
            raise ValueError(f"{path}:{number}: incomplete input table row")
        output.append(dict(zip(header, fields)))
    return output


def _number(value, label):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _objects(model):
    hyd = {row["name"]: row for row in _table(model / "hydrology.hyd")}
    hru = {int(row["id"]): row for row in _table(model / "hru-data.hru")}
    connected = _table(model / "hru.con")
    ids = [int(row["id"]) for row in connected]
    if ids != list(range(1, len(connected) + 1)):
        raise ValueError("Native calibration needs consecutive hru.con IDs in file order")
    result = []
    for con in connected:
        record = hru[int(con["hru"])]
        if record["hydro"] not in hyd:
            raise ValueError(f"Unknown hydrology record for HRU {con['id']}")
        result.append((int(con["id"]), record, hyd[record["hydro"]]))
    return hyd, result


def apply_parameters(model, definitions, expected_version):
    """Write one native file in an isolated clean trial directory.

    Existing active native changes are rejected even when called independently
    of swatplus_trial. Configured bounds must fit the model's cal_parms table;
    absolute values outside bounds are rejected. Relative operations retain
    SWAT+'s own clipping to model bounds and record that behavior explicitly.
    """
    model = Path(model)
    if expected_version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Native calibration is not checked for {expected_version}")
    if expected_version == "62.0.1":
        validate_input_layout(model, expected_version)
    cio = _lines(model / "file.cio")
    positions = [i for i, line in enumerate(cio) if line.split() and line.split()[0] == "chg"]
    if len(positions) != 1:
        raise ValueError("file.cio must contain exactly one chg row")
    cio_index = positions[0]
    fields = cio[cio_index].split()
    if len(fields) < 3 or fields[2].lower() != "null":
        raise ValueError("Active source calibration changes must be removed from the clean export")
    if not definitions:
        return []
    if fields[1].lower() == "null":
        raise ValueError("Native calibration requires cal_parms.cal in file.cio")
    database = _database(model)[0]
    metadata = validate_definitions(definitions, model, expected_version)
    hyd, objects = None, None
    reports, changes, edited = [], [], set()
    landuse = None
    for definition, normalized in zip(definitions, metadata):
        if "native_name" in definition:
            line, report = _explicit_change(model, definition, normalized, database, edited)
            changes.append(line)
            reports.append(report)
            continue
        if objects is None:
            hyd, objects = _objects(model)
        if not objects:
            raise ValueError("Native calibration requires connected standard HRUs")
        column = definition.get("column", "").lower()
        if definition.get("file") != "hydrology.hyd" or column not in SUPPORTED_COLUMNS:
            raise ValueError("native_calibration supports only hydrology.hyd ESCO/PERCO; "
                             "CN lookup-table factors are not native HRU cn2 changes")
        if definition.get("method", "").lower() not in {"absolute", "absval"}:
            raise ValueError(f"Native {column} currently supports absolute/absval only")
        entry = database.get(column)
        if not entry or entry.get("obj_typ") != "hru":
            raise ValueError(f"Model calibration database does not define {column} as an HRU parameter")
        db_low, db_high = [_number(entry[key], key) for key in ("abs_min", "abs_max")]
        lower = _number(definition.get("min", 0), "Minimum")
        upper = _number(definition.get("max", 1), "Maximum")
        value = _number(definition["value"], "Parameter value")
        if lower < max(0, db_low) or upper > min(1, db_high) or lower > upper or not lower <= value <= upper:
            raise ValueError(f"Native {column}={value} is outside configured/model bounds [{lower}, {upper}]")
        if column == "perco" and 1e-9 < value <= 1e-6:
            raise ValueError("PERCO in (1e-9, 1e-6] is unsafe for the native log-transform in these revisions")
        names = definition.get("names")
        if names is not None:
            if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
                raise ValueError("Optional names must be a nonempty list of hydrology record names")
            missing = set(names) - set(hyd)
            if missing:
                raise ValueError(f"Unknown hydrology names: {sorted(missing)}")
        selected = [obj for obj in objects if names is None or obj[1]["hydro"] in names]
        if not selected or names is not None and set(names) != {obj[1]["hydro"] for obj in selected}:
            raise ValueError("Selected hydrology records must be used by connected HRUs")
        ids = [obj[0] for obj in selected]
        for identity in ((column, number, 0) for number in ids):
            if identity in edited:
                raise ValueError(f"Overlapping native calibration changes: {identity}")
            edited.add(identity)
        if column == "perco":
            if landuse is None:
                landuse = {row["name"]: row for row in _table(model / "landuse.lum")}
            for number, hru, _ in selected:
                lu = landuse.get(hru["lu_mgt"])
                if not lu or "tile" not in lu or lu["tile"].lower() != "null":
                    raise ValueError(f"Native PERCO with tile drainage is not supported (HRU {number})")
        actual = float(f"{value:.9f}")
        if column == "perco" and 1e-9 < actual <= 1e-6:
            raise ValueError("Rounded PERCO falls inside the unsafe native log-transform interval")
        original = [_number(obj[2][column], column) for obj in selected]
        # Zero OBJ_TOT means all connected objects. Otherwise list positive IDs
        # explicitly; the Fortran parser also supports compressed negative ranges.
        scope = "0" if names is None else f"{len(ids)} " + " ".join(map(str, ids))
        changes.append(f"{column} absval {actual:.9f} 0 0 0 0 0 0 0 {scope}")
        reports.append({"file": "calibration.cal", "source_file": "hydrology.hyd", "column": column,
                        "method": "absval", "value": value, "written_value": actual,
                        "names": sorted(set(names)) if names is not None else "all",
                        "object_type": "hru", "object_ids": ids, "matched_records": len(ids),
                        "modified_records": sum(old != actual for old in original),
                        "record_count_basis": "connected HRUs; comparison against pre-initialization input values",
                        "original_range": [min(original), max(original)], "written_range": [actual, actual],
                        "bounds": [lower, upper], "model_bounds": [db_low, db_high],
                        "application_stage": "native post-initialization, before simulation",
                        "writeback_verified": True, "internal_value_verified": False})
    content = ("Native calibration written by SWAT+ automation\n" + str(len(changes)) + "\n"
               "name chg_typ chg_val conds lyr1 lyr2 year1 year2 day1 day2 obj_tot\n"
               + "\n".join(changes) + "\n")
    path = model / "calibration.cal"
    path.write_text(content, encoding="utf-8")
    if path.read_text(encoding="utf-8") != content:
        raise ValueError("Native calibration write-back mismatch")
    fields[2] = "calibration.cal"
    cio[cio_index] = " ".join(fields)
    (model / "file.cio").write_text("\n".join(cio) + "\n", encoding="utf-8")
    return reports


def check_initialization(model, reports, expected_version):
    """Read pre-calibration diagnostics, never label them post-change values."""
    if not reports:
        return []
    header, records = read_checker(model, expected_version)
    _, objects = _objects(Path(model))
    if len(records) != len(objects):
        raise ValueError("Cannot align native initialization diagnostics with HRUs")
    checks = []
    for report in reports:
        column = report["column"]
        if report["object_type"] != "hru" or column not in header:
            checks.append({"column": column, "file": "checker.out", "object_type": report["object_type"],
                           "stage": "before native calibration", "diagnostic_available": False,
                           "reason": "This parameter is not exposed in the HRU checker table",
                           "post_calibration_values_verified": False})
            continue
        values = [_number(records[number - 1][header.index(column) + 2], column) for number in report["object_ids"]]
        if column == "perco":
            tiles = [int(records[number - 1][header.index("tiledrain") + 2]) for number in report["object_ids"]]
            if any(tiles):
                raise ValueError("Model initialized tile-drained HRUs; native PERCO scope is unsupported")
        verified = False
        if expected_version == "62.0.1" and column in {"esco", "perco", "epco", "latq_co"}:
            by_number = {number: (hru, hyd) for number, hru, hyd in objects}
            for number, actual in zip(report["object_ids"], values):
                hru, hyd = by_number[number]
                if records[number - 1][0] != hru["soil"]:
                    raise ValueError(f"checker.out soil/HRU order mismatch at HRU {number}")
                expected = _number(hyd[column], column)
                if not math.isclose(actual, expected, rel_tol=0, abs_tol=0.000051):
                    raise ValueError(f"Pre-calibration {column} at HRU {number} is {actual}; expected source {expected}")
            verified = True
        checks.append({"column": column, "file": "checker.out", "hru_records": len(values),
                       "initialized_range": [min(values), max(values)],
                       "pre_calibration_source_verified": verified,
                       "stage": "before native calibration", "post_calibration_values_verified": False})
    return checks
