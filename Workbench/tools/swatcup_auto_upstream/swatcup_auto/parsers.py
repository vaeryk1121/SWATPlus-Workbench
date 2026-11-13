from __future__ import annotations

import math
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ParseError
from .models import ParInfDocument, ParameterRange, SummaryResult


FLOAT_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
PARAMETER_LINE = re.compile(
    rf"^(\s*)(\S+)(\s+)({FLOAT_PATTERN})(\s+)({FLOAT_PATTERN})(.*)$"
)
LEADING_COUNT = re.compile(r"^(\s*)(\d+)(.*)$")


def read_text_robust(path: Path) -> Tuple[str, str]:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "gb18030", "cp1252", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ParseError(f"无法识别文本编码：{path}")


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _to_float(value: str) -> float:
    return float(value.replace("D", "E").replace("d", "e"))


def _normalise_name(name: str) -> str:
    return re.sub(r"^\d+:", "", name.strip()).upper()


def parse_par_inf(path: Path | str) -> ParInfDocument:
    target = Path(path)
    text, encoding = read_text_robust(target)
    lines = text.splitlines(keepends=True)
    count_lines: List[Tuple[int, int]] = []
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("#", "::", "rem ", "REM ")):
            continue
        match = re.match(r"^\s*(\d+)\s*(?::|$)", line.rstrip("\r\n"))
        if match:
            count_lines.append((index, int(match.group(1))))
            if len(count_lines) == 2:
                break
    if len(count_lines) != 2:
        raise ParseError(f"未在 par_inf.txt 前部找到参数数目和模拟次数：{target}")

    parameter_count_line, parameter_count = count_lines[0]
    simulation_count_line, simulation_count = count_lines[1]
    parameters: List[ParameterRange] = []
    for index in range(simulation_count_line + 1, len(lines)):
        match = PARAMETER_LINE.match(lines[index].rstrip("\r\n"))
        if not match:
            continue
        try:
            minimum = _to_float(match.group(4))
            maximum = _to_float(match.group(6))
        except ValueError:
            continue
        name = match.group(2)
        if set(name) <= {"-", "_"}:
            continue
        parameters.append(ParameterRange(name, minimum, maximum, index))
        if len(parameters) == parameter_count:
            break
    if len(parameters) != parameter_count:
        raise ParseError(
            f"par_inf.txt 声明 {parameter_count} 个参数，但只解析到 {len(parameters)} 个：{target}"
        )
    for parameter in parameters:
        if not (math.isfinite(parameter.minimum) and math.isfinite(parameter.maximum)):
            raise ParseError(f"参数 {parameter.name} 的上下界不是有限数")
        if parameter.minimum > parameter.maximum:
            raise ParseError(
                f"参数 {parameter.name} 的下界不能大于上界：{parameter.minimum} > {parameter.maximum}"
            )
    return ParInfDocument(
        path=target,
        lines=lines,
        encoding=encoding,
        parameter_count=parameter_count,
        simulation_count=simulation_count,
        parameter_count_line=parameter_count_line,
        simulation_count_line=simulation_count_line,
        parameters=parameters,
    )


def _format_number(value: float) -> str:
    if value == 0:
        value = 0.0
    return f"{value:.10g}"


def set_simulation_count(path: Path | str, simulation_count: int) -> ParInfDocument:
    if simulation_count < 1:
        raise ParseError("模拟次数必须大于等于 1")
    document = parse_par_inf(path)
    line = document.lines[document.simulation_count_line]
    ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
    body = line[: -len(ending)] if ending else line
    match = LEADING_COUNT.match(body)
    if not match:
        raise ParseError(f"无法更新模拟次数行：{document.path}")
    document.lines[document.simulation_count_line] = (
        f"{match.group(1)}{simulation_count}{match.group(3)}{ending}"
    )
    atomic_write_text(document.path, "".join(document.lines), document.encoding)
    return parse_par_inf(document.path)


def set_swedit_range(path: Path | str, start: int, end: int) -> None:
    if start < 1 or end < start:
        raise ParseError(f"SUFI2_swEdit.def 模拟编号范围无效：{start}..{end}")
    target = Path(path)
    text, encoding = read_text_robust(target)
    lines = text.splitlines(keepends=True)
    found: List[int] = []
    for index, line in enumerate(lines):
        if re.match(r"^\s*\d+", line):
            found.append(index)
            if len(found) == 2:
                break
    if len(found) != 2:
        raise ParseError(f"SUFI2_swEdit.def 中未找到起止模拟编号：{target}")
    for index, value in zip(found, (start, end)):
        line = lines[index]
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[: -len(ending)] if ending else line
        lines[index] = re.sub(r"^(\s*)\d+", rf"\g<1>{value}", body, count=1) + ending
    atomic_write_text(target, "".join(lines), encoding)


def parse_new_pars(path: Path | str) -> List[ParameterRange]:
    target = Path(path)
    text, _ = read_text_robust(target)
    results: List[ParameterRange] = []
    seen = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            minimum = _to_float(parts[-2])
            maximum = _to_float(parts[-1])
        except ValueError:
            continue
        name_index = -3
        name = parts[name_index]
        if name.lower() in {"par_name", "parameter_name"}:
            continue
        normalised = _normalise_name(name)
        if normalised in seen:
            raise ParseError(f"new_pars.txt 中参数重复：{name}")
        if not (math.isfinite(minimum) and math.isfinite(maximum)):
            raise ParseError(f"new_pars.txt 中参数 {name} 存在非有限数")
        results.append(ParameterRange(name, minimum, maximum))
        seen.add(normalised)
    if not results:
        raise ParseError(f"未从 new_pars.txt 解析到任何建议范围：{target}")
    return results


def apply_suggested_ranges(
    par_inf_path: Path | str,
    suggestions: Iterable[ParameterRange],
    initial_bounds: Mapping[str, Tuple[float, float]],
    limits: Optional[Mapping[str, Iterable[float]]] = None,
    lock_to_initial_bounds: bool = True,
    minimum_width: float = 1e-12,
    on_invalid_range: str = "stop",
) -> Tuple[List[ParameterRange], List[str]]:
    document = parse_par_inf(par_inf_path)
    suggested_map = {_normalise_name(item.name): item for item in suggestions}
    initial_map = {_normalise_name(name): tuple(bounds) for name, bounds in initial_bounds.items()}
    limit_map = {
        _normalise_name(name): (float(list(bounds)[0]), float(list(bounds)[1]))
        for name, bounds in (limits or {}).items()
    }
    warnings: List[str] = []
    applied: List[ParameterRange] = []

    for current in document.parameters:
        key = _normalise_name(current.name)
        suggestion = suggested_map.get(key)
        if suggestion is None:
            raise ParseError(f"new_pars.txt 缺少活动参数：{current.name}")
        new_min, new_max = suggestion.minimum, suggestion.maximum
        if lock_to_initial_bounds:
            if key not in initial_map:
                raise ParseError(f"缺少参数 {current.name} 的初始安全范围")
            safe_min, safe_max = initial_map[key]
            clipped_min = max(new_min, safe_min)
            clipped_max = min(new_max, safe_max)
            if (clipped_min, clipped_max) != (new_min, new_max):
                warnings.append(
                    f"{current.name}: 建议范围 [{new_min}, {new_max}] 已裁剪到初始范围 [{safe_min}, {safe_max}]"
                )
            new_min, new_max = clipped_min, clipped_max
        if key in limit_map:
            safe_min, safe_max = limit_map[key]
            clipped_min = max(new_min, safe_min)
            clipped_max = min(new_max, safe_max)
            if (clipped_min, clipped_max) != (new_min, new_max):
                warnings.append(
                    f"{current.name}: 建议范围已按用户安全边界 [{safe_min}, {safe_max}] 裁剪"
                )
            new_min, new_max = clipped_min, clipped_max
        invalid = (
            not math.isfinite(new_min)
            or not math.isfinite(new_max)
            or new_max - new_min <= minimum_width
        )
        if invalid:
            message = (
                f"{current.name}: 更新后范围无效 [{new_min}, {new_max}]，"
                f"最小宽度要求为 {minimum_width}"
            )
            if on_invalid_range == "keep_previous":
                warnings.append(message + "；已保留上一轮范围")
                new_min, new_max = current.minimum, current.maximum
            else:
                raise ParseError(message)
        applied.append(ParameterRange(current.name, new_min, new_max, current.line_index))

    lines = list(document.lines)
    for item in applied:
        original = lines[item.line_index]
        ending = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
        body = original[: -len(ending)] if ending else original
        match = PARAMETER_LINE.match(body)
        if not match:
            raise ParseError(f"无法重写参数行：{original.rstrip()}")
        lines[item.line_index] = (
            f"{match.group(1)}{match.group(2)}{match.group(3)}"
            f"{_format_number(item.minimum)}{match.group(5)}{_format_number(item.maximum)}"
            f"{match.group(7)}{ending}"
        )
    atomic_write_text(document.path, "".join(lines), document.encoding)
    parse_par_inf(document.path)
    return applied, warnings


def parse_summary(path: Path | str) -> SummaryResult:
    target = Path(path)
    text, _ = read_text_robust(target)
    header = re.search(
        rf"Goal_type\s*=\s*(.*?)\s+No_sims\s*=\s*(\d+)\s+Best_sim_no\s*=\s*(\d+)\s+Best_goal\s*=\s*({FLOAT_PATTERN})",
        text,
        flags=re.IGNORECASE,
    )
    if not header:
        raise ParseError(f"无法解析 summary_stat.txt 顶部汇总：{target}")
    goal_type = header.group(1).strip()
    simulation_count = int(header.group(2))
    best_simulation = int(header.group(3))
    best_goal = _to_float(header.group(4))

    lines = text.splitlines()
    metrics: Dict[str, Dict[str, float]] = {}
    header_index = next(
        (index for index, line in enumerate(lines) if line.strip().lower().startswith("variable")),
        None,
    )
    if header_index is not None:
        columns = lines[header_index].split()[1:]
        if "---" in columns:
            columns = columns[: columns.index("---")]
        for line in lines[header_index + 1 :]:
            parts = line.split()
            if len(parts) < len(columns) + 1:
                continue
            variable = parts[0]
            values: List[float] = []
            valid = True
            for raw in parts[1 : 1 + len(columns)]:
                try:
                    values.append(_to_float(raw))
                except ValueError:
                    valid = False
                    break
            if valid:
                metrics[variable] = dict(zip(columns, values))
    return SummaryResult(
        goal_type=goal_type,
        simulation_count=simulation_count,
        best_simulation=best_simulation,
        best_goal=best_goal,
        metrics=metrics,
    )


def select_target_value(summary: SummaryResult, target: Mapping[str, object]) -> Tuple[float, str]:
    source = str(target.get("source", "best_goal"))
    if source == "best_goal":
        return summary.best_goal, f"best_goal ({summary.goal_type})"
    metric_name = str(target.get("metric", "")).strip()
    variable_name = str(target.get("variable", "")).strip()
    if not summary.metrics:
        raise ParseError("summary_stat.txt 中没有可用的变量指标")
    if variable_name:
        variable_key = next(
            (name for name in summary.metrics if name.upper() == variable_name.upper()), None
        )
        if variable_key is None:
            available = ", ".join(summary.metrics)
            raise ParseError(f"找不到目标变量 {variable_name}；可选：{available}")
    elif len(summary.metrics) == 1:
        variable_key = next(iter(summary.metrics))
    else:
        available = ", ".join(summary.metrics)
        raise ParseError(f"汇总中有多个变量，必须指定 variable；可选：{available}")
    metric_key = next(
        (name for name in summary.metrics[variable_key] if name.upper() == metric_name.upper()),
        None,
    )
    if metric_key is None:
        available = ", ".join(summary.metrics[variable_key])
        raise ParseError(f"找不到指标 {metric_name}；可选：{available}")
    return summary.metrics[variable_key][metric_key], f"{variable_key}.{metric_key}"


def compare_target(actual: float, operator: str, expected: float) -> bool:
    operations = {
        ">=": lambda a, b: a >= b,
        ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b,
        "<": lambda a, b: a < b,
        "==": lambda a, b: math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12),
    }
    try:
        return operations[operator](actual, expected)
    except KeyError as exc:
        raise ParseError(f"不支持的目标比较符：{operator}") from exc


def ranges_to_map(parameters: Iterable[ParameterRange]) -> Dict[str, Tuple[float, float]]:
    return {item.name: (item.minimum, item.maximum) for item in parameters}
