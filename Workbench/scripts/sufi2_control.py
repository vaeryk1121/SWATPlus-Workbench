"""Small, calibration-only controls around the installed SUFI-2 kernels.

The regression below is an auxiliary OLS parameter-versus-objective diagnostic,
not a replacement for the original new_pars range update. A large p value is
not proof that a parameter is physically unimportant.
"""
import math


def round_plan(parameters, repetitions, options):
    minimum = max(8, len(parameters) + 2)
    if "round_sizes" in options:
        sizes = options["round_sizes"]
        if not isinstance(sizes, list) or not sizes:
            raise ValueError("SUFI-2 round_sizes must be a nonempty list")
    else:
        iterations = options.get("iterations", 2)
        if type(iterations) is not int or iterations < 1:
            raise ValueError("SUFI-2 iterations must be a positive integer")
        count = options.get("samples_per_iteration", repetitions // iterations)
        sizes = [count] * iterations
    # Later rounds may search fewer parameters. Their dimension-specific
    # requirement is checked again after a screening/review decision.
    if any(type(n) is not int or not 8 <= n <= 2000 for n in sizes) or sizes[0] < minimum:
        raise ValueError(f"SUFI-2 needs {minimum}..2000 samples initially and 8..2000 per later round")
    if sum(sizes) != repetitions:
        raise ValueError("SUFI-2 repetitions must equal the sum of round_sizes")
    if options.get("continuation", "auto") not in ("auto", "review"):
        raise ValueError("SUFI-2 continuation must be auto or review")
    retries = options.get("retries", 1)
    if type(retries) is not int or not 0 <= retries <= 5:
        raise ValueError("SUFI-2 retries must be an integer in 0..5")
    settings = options.get("screening", {})
    if not isinstance(settings, dict):
        raise ValueError("SUFI-2 screening must be an object")
    if "enabled" in settings and type(settings["enabled"]) is not bool:
        raise ValueError("screening.enabled must be boolean")
    if settings.get("mode", "suggest") not in ("suggest", "auto"):
        raise ValueError("screening.mode must be suggest or auto")
    if type(settings.get("after_round", 1)) is not int or settings.get("after_round", 1) < 1:
        raise ValueError("screening.after_round must be positive")
    if not 0 < settings.get("p_threshold", .05) < 1:
        raise ValueError("screening.p_threshold must be between 0 and 1")
    if type(settings.get("min_active", 2)) is not int or settings.get("min_active", 2) < 1:
        raise ValueError("screening.min_active must be positive")
    protected = settings.get("protected_parameters", [])
    if not isinstance(protected, list) or any(name not in {p["name"] for p in parameters} for name in protected):
        raise ValueError("screening.protected_parameters must contain known parameter names")
    threshold = options.get("behavioral_threshold", .5)
    if type(threshold) not in (int, float) or not math.isfinite(threshold):
        raise ValueError("SUFI-2 behavioral_threshold must be finite")
    diagnostics = options.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        raise ValueError("SUFI-2 diagnostics must be an object")
    for key in ("repeated_boundary_rounds", "stagnation_rounds"):
        value = diagnostics.get(key, 2)
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"diagnostics.{key} must be positive or null to disable")
    improvement = diagnostics.get("min_improvement", .001)
    if type(improvement) not in (int, float) or not math.isfinite(improvement) or improvement < 0:
        raise ValueError("diagnostics.min_improvement must be finite and nonnegative")
    coverage = diagnostics.get("min_p_factor")
    if coverage is not None and (type(coverage) not in (int, float) or not math.isfinite(coverage) or not 0 <= coverage <= 1):
        raise ValueError("diagnostics.min_p_factor must be null or in 0..1")
    return {"minimum_evaluations": minimum, "iterations": len(sizes),
            "round_sizes": list(sizes),
            "samples_per_iteration": sizes[0] if len(set(sizes)) == 1 else None}


def regression_sensitivity(parameters, values, scores):
    """OLS with an intercept, standardized coordinates and two-sided t tests.

    The installed classic SUFI2_new_pars echo contains Jacobian sensitivity,
    but no regression t/p table. We therefore label this separate calculation
    explicitly, preserving the vendor's own range/statistical files.

    Besides the standardized coefficient/standard error, each row also carries
    the exact raw-space equivalents (raw_coefficient = coefficient / x_scale,
    raw_standard_error = standard_error / x_scale) for consumers that need a
    width in parameter units, e.g. the hand-written SPE range update.
    """
    from mature_search import load_runtime
    _, np, _ = load_runtime()
    from scipy.stats import t as student_t
    names = [p["name"] for p in parameters]
    n, dimension = len(scores), len(names)
    result = {"method": "auxiliary_multiple_linear_regression",
              "source": "Python NumPy OLS + SciPy two-sided Student t; auxiliary SWAT-CUP-style global regression, not vendor t/p output",
              "n": n, "dimensions": dimension, "df": n - dimension - 1,
              "automatic_screening_allowed": False, "status": "unreliable",
              "reason": "", "rank": None, "condition_number": None, "parameters": [],
              "minimum_auto_screening_samples": max(30, 5 * dimension),
              "note": "Conditional linear association within this sampled range; large p is not proof of no physical or nonlinear effect."}
    matrix = np.asarray([[row[name] for name in names] for row in values], dtype=float)
    y = np.asarray(scores, dtype=float)
    if matrix.shape != (n, dimension) or not np.isfinite(matrix).all() or not np.isfinite(y).all():
        result["reason"] = "Nonfinite or misaligned completed calibration samples"
        return result
    scale = matrix.std(axis=0)
    if n <= dimension + 1 or (scale <= np.finfo(float).eps).any():
        result["reason"] = "Insufficient residual degrees of freedom or a constant parameter"
        return result
    x = np.column_stack((np.ones(n), (matrix - matrix.mean(axis=0)) / scale))
    rank = int(np.linalg.matrix_rank(x)); condition = float(np.linalg.cond(x))
    result.update(rank=rank, condition_number=condition if math.isfinite(condition) else None)
    if rank < dimension + 1 or not math.isfinite(condition) or condition > 1e8:
        result["reason"] = "Collinear or ill-conditioned parameter design"
        return result
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    residual = y - x @ beta
    variance = float(residual @ residual) / result["df"]
    if variance <= np.finfo(float).eps * max(1.0, float(y @ y)):
        result["reason"] = "Zero/negligible residual variance makes t/p inference unstable"
        return result
    covariance = variance * np.linalg.inv(x.T @ x)
    errors = np.sqrt(np.diag(covariance))
    for i, name in enumerate(names, 1):
        t_stat = float(beta[i] / errors[i])
        result["parameters"].append({"name": name, "coefficient": float(beta[i]),
                                     "standard_error": float(errors[i]),
                                     "raw_coefficient": float(beta[i] / scale[i - 1]),
                                     "raw_standard_error": float(errors[i] / scale[i - 1]),
                                     "t_stat": t_stat,
                                     "p_value": float(2 * student_t.sf(abs(t_stat), result["df"]))})
    if result["df"] < 5:
        result["reason"] = "Fewer than 5 residual degrees of freedom; display only, never automatically freeze"
    elif n < result["minimum_auto_screening_samples"]:
        result["reason"] = "Exploratory sample only: auto screening requires max(30, 5*D) samples as a conservative operational rule, not a power guarantee"
    else:
        result.update(status="ok", automatic_screening_allowed=True,
                      reason="Full-rank OLS passes operational sample/conditioning checks; this does not prove sufficient power or absence of nonlinear effects")
    return result


def screening_advice(parameters, sensitivity, best_values, options, iteration):
    settings = options.get("screening", {})
    names = [p["name"] for p in parameters]
    protected = settings.get("protected_parameters", [])
    eligible = (settings.get("enabled", True) and iteration == settings.get("after_round", 1)
                and sensitivity.get("automatic_screening_allowed", False))
    retained = list(names)
    if eligible:
        p_values = {p["name"]: p["p_value"] for p in sensitivity["parameters"]}
        retained = [name for name in names if name in protected or p_values[name] <= settings.get("p_threshold", .05)]
        minimum = min(len(names), settings.get("min_active", 2))
        for name in sorted(names, key=lambda name: p_values[name]):
            if len(retained) >= minimum:
                break
            if name not in retained:
                retained.append(name)
        retained = [name for name in names if name in retained]
    return {"eligible": bool(eligible), "suggested_retained": retained,
            "suggested_frozen": {name: best_values[name] for name in names if name not in retained},
            "protected_parameters": list(protected),
            "min_active": settings.get("min_active", 2),
            "note": "Freeze only by explicit review or an enabled auto-screening rule; a large p value alone does not establish irrelevance."}


def _clipped_boundary_sides(current):
    """Require the best value and the clipped proposal to concern the same edge."""
    result = current["result"]
    sides = {}
    for parameter in current["parameters"]:
        name = parameter["name"]
        lo, hi = parameter["lower"], parameter["upper"]
        best = result["best_parameters"][name]
        raw, used = result["raw_new_ranges"][name], result["new_ranges"][name]
        if best - lo <= .03 * (hi-lo) and raw[0] < used[0]:
            sides[name] = "lower"
        elif hi - best <= .03 * (hi-lo) and raw[1] > used[1]:
            sides[name] = "upper"
    return sides


def round_diagnostics(current, entries, previous=None, history=None, options=None):
    """Use completed calibration rounds only; pause rules are operational hints."""
    settings = (options or {}).get("diagnostics", {})
    history = list(history if history is not None else ([previous] if previous else []))
    # Screening changes the search problem. Do not carry an old search
    # dimension's boundary/stagnation streak into a new active parameter set.
    names = {p["name"] for p in current["parameters"]}
    comparable = []
    for earlier in reversed(history):
        if "result" not in earlier or {p["name"] for p in earlier["parameters"]} != names:
            break
        comparable.append(earlier)
    comparable.reverse()
    result = current["result"]
    ranges = {p["name"]: [p["lower"], p["upper"]] for p in current["parameters"]}
    clipped = [name for name, raw in result["raw_new_ranges"].items()
               if raw != result["new_ranges"][name]]
    boundary = [name for name, (lo, hi) in ranges.items()
                if min(result["best_parameters"][name] - lo, hi - result["best_parameters"][name]) <= .03 * (hi - lo)]
    band = result["all_interval"]
    warnings = []
    if clipped: warnings.append("原版建议超出固定硬边界，实际下一轮范围已取交集")
    if boundary: warnings.append("最佳参数靠近本轮边界，请检查范围和参数物理含义")
    if result["behavioral_count"] < 2: warnings.append("达到筛选阈值的样本不足2组，未生成筛选样本95PPU")
    if not current["sensitivity"]["automatic_screening_allowed"]: warnings.append("回归诊断不足以支持自动冻结参数")
    blocking = []
    sides = _clipped_boundary_sides(current)
    boundary_streak = {}
    for name, side in sides.items():
        streak = 1
        for earlier in reversed(comparable):
            if _clipped_boundary_sides(earlier).get(name) != side:
                break
            streak += 1
        boundary_streak[name] = streak
    boundary_limit = settings.get("repeated_boundary_rounds", 2)
    repeated = [name for name, streak in boundary_streak.items()
                if boundary_limit is not None and streak >= boundary_limit]
    if repeated:
        blocking.append("repeated_clipped_boundary")
        warnings.append("参数连续靠近同一边界且建议被硬边界截断："+ "、".join(repeated) +
                        "；暂停供检查，不自动放宽硬边界")
    completed = comparable + [current]
    stagnant = 0
    for index in range(len(completed)-1, 0, -1):
        improvement = completed[index]["result"]["best_nse"] - completed[index-1]["result"]["best_nse"]
        if improvement > settings.get("min_improvement", .001):
            break
        stagnant += 1
    stagnation_limit = settings.get("stagnation_rounds", 2)
    if stagnation_limit is not None and stagnant >= stagnation_limit:
        blocking.append("stagnant_search")
        warnings.append(f"连续{stagnant}次轮间最佳NSE改善未超过{settings.get('min_improvement', .001):g}；"
                        "这是检查参数范围和预算的工程提示，不证明收敛")
    coverage = settings.get("min_p_factor")
    if coverage is not None and band.get("p_factor") is not None and band["p_factor"] < coverage:
        blocking.append("coverage_below_user_target")
        warnings.append(f"P-factor低于用户预设目标{coverage:g}；目标不是所有流域通用标准")
    return {"best_score": result["best_nse"],
            "improvement": None if previous is None else result["best_nse"] - previous["result"]["best_nse"],
            "n_completed": sum(e.get("status") == "completed" for e in entries),
            "n_failed": sum(bool(e.get("failure_history")) for e in entries),
            "range_clipped": clipped, "best_on_boundary": boundary,
            "clipped_boundary_sides": sides, "boundary_streaks": boundary_streak,
            "stagnation_comparisons": stagnant,
            "diagnostic_settings": {"repeated_boundary_rounds": boundary_limit,
                                    "stagnation_rounds": stagnation_limit,
                                    "min_improvement": settings.get("min_improvement", .001),
                                    "min_p_factor": coverage},
            "p_factor": band.get("p_factor"), "r_factor": band.get("r_factor"),
            "warnings": warnings, "blocking_reasons": blocking}


def validate_review(payload, pending, original, current, next_count):
    """Validate a decision without mutating past designs, data or config."""
    allowed = {"after_round", "action", "next_sample_count", "ranges", "retain_parameters", "note"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("Review contains unsupported fields; observation/validation/settings edits are forbidden")
    if type(payload.get("after_round")) is not int or payload["after_round"] != pending["after_round"]:
        raise ValueError("Review after_round does not match the waiting round")
    action = payload.get("action", "continue")
    if action not in ("continue", "finish"):
        raise ValueError("Review action must be continue or finish")
    names = [p["name"] for p in current["parameters"]]
    retained = payload.get("retain_parameters", names)
    if not isinstance(retained, list) or not retained or len(set(retained)) != len(retained) or not set(retained) <= set(names):
        raise ValueError("retain_parameters must be a nonempty unique subset of current active parameters")
    if not set(pending.get("screening",{}).get("protected_parameters",[])) <= set(retained):
        raise ValueError("Protected parameters must remain active")
    retained = [name for name in names if name in retained]
    count = payload.get("next_sample_count", next_count)
    if action=="continue" and (type(count) is not int or not max(8, len(retained) + 2) <= count <= 2000):
        raise ValueError("next_sample_count must cover active dimensions and be in 8..2000")
    supplied = payload.get("ranges", {})
    if not isinstance(supplied, dict) or not set(supplied) <= set(retained):
        raise ValueError("Review ranges must refer only to retained active parameters")
    by_name = {p["name"]: p for p in original}
    ranges = {}
    for name in retained:
        bounds = supplied.get(name, current["result"]["new_ranges"][name])
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or any(type(x) not in (int, float) or not math.isfinite(x) for x in bounds):
            raise ValueError("Review ranges must contain two finite numbers")
        low, high = by_name[name]["coordinate_min"], by_name[name]["coordinate_max"]
        if not low <= bounds[0] < bounds[1] <= high:
            raise ValueError(f"Review range for {name} exceeds the fixed initial hard bounds")
        ranges[name] = list(bounds)
    frozen = dict(current.get("frozen_parameters", {}))
    frozen.update({name: current["result"]["best_parameters"][name] for name in names if name not in retained})
    return {"action": action, "active_parameters": retained, "frozen_parameters": frozen,
            "ranges": ranges, "next_sample_count": count, "source": "user_review",
            "note": str(payload.get("note", ""))}
