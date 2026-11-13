"""Reproducible SALib designs/analysis and explicitly scoped ensemble summaries.

No model is called here. The workflow owns candidate execution and supplies one
finite response per design row, in the original order. A failed/missing row is
an error, never silently removed from a structured sensitivity experiment.
"""
import importlib.metadata
import math

from mature_search import load_runtime


def _salib():
    _, np, versions = load_runtime()
    try:
        version = importlib.metadata.version("SALib")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("敏感性分析需要项目依赖 SALib==1.4.7；请安装 config/requirements-calibration.txt") from exc
    if version != "1.4.7":
        raise RuntimeError(f"SALib version {version} differs from verified 1.4.7")
    return np, {**versions, "SALib": version}


def _problem(parameters):
    names = [str(p["name"]) for p in parameters]
    if not names or len(names) != len(set(names)):
        raise ValueError("Sensitivity parameters require unique names")
    bounds = [[float(p["lower"]), float(p["upper"])] for p in parameters]
    if any(not (math.isfinite(lo) and math.isfinite(hi) and lo < hi) for lo, hi in bounds):
        raise ValueError("Sensitivity bounds must be finite lower < upper")
    return {"num_vars": len(names), "names": names, "bounds": bounds}


def generate_design(parameters, method, settings=None, seed=20261019):
    """Return JSON-safe values and exact cost for Morris or Sobol.

    Morris: trajectories * (D+1). Sobol without second order: N*(D+2);
    with second order: N*(2D+2). N must be a power of two. These costs do
    not include an optional baseline or later uncertainty ensemble.
    """
    settings = dict(settings or {})
    problem = _problem(parameters)
    d = problem["num_vars"]
    notes = []
    if method == "morris":
        n = settings.get("trajectories", 10)
        levels = settings.get("num_levels", 4)
        if type(n) is not int or n < 4:
            raise ValueError("Morris needs at least 4 complete trajectories; this is a diagnostic minimum, not convergence")
        if type(levels) is not int or levels < 4 or levels % 2:
            raise ValueError("Morris num_levels must be an even integer >= 4")
        cost = n * (d + 1)
        resolved = {"trajectories": n, "num_levels": levels}
        if n < 10:
            notes.append("Fewer than 10 Morris trajectories: exploratory screening only; repeat with larger designs.")
    elif method == "sobol":
        n = settings.get("base_sample_size", 256)
        second_order = settings.get("calc_second_order", False)
        if type(n) is not int or n < 2 or n & (n - 1):
            raise ValueError("Sobol base_sample_size must be an integer power of two >= 2")
        if type(second_order) is not bool:
            raise ValueError("Sobol calc_second_order must be boolean")
        cost = n * ((2 * d + 2) if second_order else (d + 2))
        resolved = {"base_sample_size": n, "calc_second_order": second_order}
        if n < 256:
            notes.append("Sobol N < 256: exploratory only; confidence intervals and larger-N stability must be assessed.")
    else:
        raise ValueError("Sensitivity method must be 'morris' or 'sobol'")
    budget = settings.get("max_evaluations")
    if budget is not None and (type(budget) is not int or budget < cost):
        raise ValueError(f"Sensitivity requires {cost} complete model evaluations; max_evaluations={budget} is insufficient")
    np, versions = _salib()
    np.random.seed(seed)
    if method == "morris":
        from SALib.sample.morris import sample
        x = sample(problem, n, num_levels=resolved["num_levels"], seed=seed)
    else:
        from SALib.sample.sobol import sample
        x = sample(problem, n, calc_second_order=resolved["calc_second_order"], scramble=True, seed=seed)
    if x.shape != (cost, d):
        raise RuntimeError(f"Unexpected sensitivity design shape: {x.shape}, expected {(cost,d)}")
    return {"method": method, "problem": problem, "settings": resolved, "seed": seed,
            "evaluations_required": cost, "versions": versions, "notes": notes,
            "values": [dict(zip(problem["names"], map(float, row))) for row in x]}


def analyze_sensitivity(design, scores, *, num_resamples=1000, conf_level=0.95):
    """Analyze one scalar, predeclared response per original design row."""
    np, versions = _salib()
    if design["versions"] != versions:
        raise ValueError("Sensitivity runtime differs from the recorded design")
    problem = design["problem"]
    x = np.asarray([[row[name] for name in problem["names"]] for row in design["values"]], dtype=float)
    y = np.asarray(scores, dtype=float)
    required = design["evaluations_required"]
    if y.ndim != 1 or len(y) != required or len(x) != required:
        raise ValueError(f"Incomplete sensitivity design: exactly {required} ordered responses required")
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("Sensitivity analysis refuses failed/non-finite runs; rerun the missing design rows")
    if float(np.ptp(y)) == 0:
        raise ValueError("Sensitivity response has zero variance; indices cannot identify parameter influence")
    if type(num_resamples) is not int or num_resamples < 100 or not 0 < conf_level < 1:
        raise ValueError("Use num_resamples >= 100 and conf_level in (0,1)")
    np.random.seed(design["seed"])
    if design["method"] == "morris":
        from SALib.analyze.morris import analyze
        result = analyze(problem, x, y, num_levels=design["settings"]["num_levels"],
                         num_resamples=num_resamples, conf_level=conf_level, seed=design["seed"], print_to_console=False)
        keys = ("mu", "mu_star", "sigma", "mu_star_conf")
    elif design["method"] == "sobol":
        from SALib.analyze.sobol import analyze
        result = analyze(problem, y, calc_second_order=design["settings"]["calc_second_order"],
                         num_resamples=num_resamples, conf_level=conf_level, seed=design["seed"], print_to_console=False)
        keys = ("S1", "S1_conf", "ST", "ST_conf")
    else:
        raise ValueError("Unsupported sensitivity method")
    rows = [{"name": name, **{key: float(result[key][i]) for key in keys}}
            for i, name in enumerate(problem["names"])]
    if any(not math.isfinite(row[key]) for row in rows for key in keys):
        raise ValueError("Non-finite sensitivity estimates: increase design size or investigate degenerate responses; no index is reported")
    output = {"method": design["method"], "evaluations": required, "indices": rows,
              "conf_level": conf_level, "bootstrap_resamples": num_resamples,
              "notes": list(design["notes"]), "versions": versions,
              "interpretation": "Sensitivity is conditional on these parameter ranges and this response; confidence intervals do not establish sampling convergence."}
    if design["method"] == "sobol" and design["settings"]["calc_second_order"]:
        output["second_order"] = [{"a": problem["names"][i], "b": problem["names"][j],
                                  "S2": float(result["S2"][i, j]), "S2_conf": float(result["S2_conf"][i, j])}
                                 for i in range(problem["num_vars"]) for j in range(i + 1, problem["num_vars"])]
        if any(not math.isfinite(row[key]) for row in output["second_order"] for key in ("S2", "S2_conf")):
            raise ValueError("Non-finite second-order sensitivity estimate; increase design size")
    return output


def uncertainty_envelope(series, observations, scores=None, threshold=None, min_behavioral=20,
                         *, ensemble_kind="glue_behavioral", diagnostics=None,
                         measurement_error_percent=0.0, observed_std_convention="sample"):
    """Summarize equal-weight conditional model ensembles on observed dates.

    series is a list of {date: value}; observations is {date: value}.
    GLUE uses the explicit calibration-only threshold scores >= threshold.
    DREAM requires true post-burn-in chain states (including repeats), no score
    threshold, and successful caller-supplied chain diagnostics. The
    sufi2_ensemble kind is the complete final-round ensemble of the
    hand-written SPE method: no threshold and no diagnostics apply. None of
    these bands adds measurement/structural model error by itself.

    measurement_error_percent (default 0) applies the SWAT-CUP manual
    observation-error allowance to the P-factor criterion only:
    U95PPU + obs*0.01*Pcnt >= obs >= L95PPU - obs*0.01*Pcnt. The R-factor
    denominator uses the declared observed_std_convention: "sample" (ddof=1,
    GLUE/DREAM convention) or "population" (ddof=0, the classic SWAT-CUP
    program's convention). The two conventions must not be mixed into one
    number; the vendor additionally floors its sigma at 1 in flow units,
    which is unit-dependent and deliberately not adopted here.
    """
    _, np, _ = load_runtime()
    if type(min_behavioral) is not int or min_behavioral < 2:
        raise ValueError("min_behavioral must be >= 2; small ensembles remain exploratory")
    if type(measurement_error_percent) not in (int, float) or not math.isfinite(float(measurement_error_percent)) \
            or measurement_error_percent < 0:
        raise ValueError("measurement_error_percent must be a finite nonnegative percentage")
    if observed_std_convention not in ("sample", "population"):
        raise ValueError("observed_std_convention must be 'sample' or 'population'")
    if not observations or len(observations) < 2:
        raise ValueError("At least two observed dates are required for interval diagnostics")
    if ensemble_kind == "glue_behavioral":
        if scores is None or len(scores) != len(series) or threshold is None or not math.isfinite(float(threshold)):
            raise ValueError("GLUE requires one calibration score per member and an explicit finite threshold")
        if any(not math.isfinite(float(value)) for value in scores):
            raise ValueError("Failed/non-finite ensemble scores must be resolved before analysis")
        indices = [i for i, value in enumerate(scores) if float(value) >= float(threshold)]
        interval_kind = "equal_weight_behavioral_parameter_ensemble_95_percent"
    elif ensemble_kind == "dream_posterior":
        if scores is not None or threshold is not None:
            raise ValueError("DREAM posterior states must not be filtered by a GLUE objective threshold")
        if not diagnostics or not diagnostics.get("posterior_usable"):
            raise ValueError("DREAM chain diagnostics are insufficient; no posterior interval will be claimed")
        indices = list(range(len(series)))
        interval_kind = "post_burn_in_parameter_posterior_conditional_model_95_percent"
    elif ensemble_kind == "sufi2_ensemble":
        if scores is not None or threshold is not None:
            raise ValueError("The SUFI-2-style final-round band uses the complete round ensemble; no GLUE threshold applies")
        indices = list(range(len(series)))
        interval_kind = "final_iteration_all_simulation_95_percent"
    else:
        raise ValueError("ensemble_kind must be glue_behavioral, dream_posterior or sufi2_ensemble")
    if len(indices) < min_behavioral:
        raise ValueError(f"Only {len(indices)} eligible ensemble members; need >= {min_behavioral}. Do not silently relax the threshold")
    dates = sorted(observations)
    obs = np.asarray([observations[date] for date in dates], dtype=float)
    try:
        matrix = np.asarray([[series[i][date] for date in dates] for i in indices], dtype=float)
    except KeyError as exc:
        raise ValueError(f"Ensemble member lacks required observed date {exc}; dates cannot be silently dropped") from exc
    if not np.isfinite(obs).all() or not np.isfinite(matrix).all():
        raise ValueError("Non-finite observations/predictions cannot enter interval diagnostics")
    lower, median, upper = np.quantile(matrix, [0.025, 0.5, 0.975], axis=0, method="linear")
    # SWAT-CUP manual P-factor criterion with the declared measurement-error allowance.
    allowance = obs * (0.01 * float(measurement_error_percent))
    p_factor = float(np.mean((obs >= lower - allowance) & (obs <= upper + allowance)))
    observed_std_ddof = 1 if observed_std_convention == "sample" else 0
    obs_sd = float(np.std(obs, ddof=observed_std_ddof))
    r_factor = float(np.mean(upper - lower) / obs_sd) if obs_sd > 0 else None
    notes = ["Conditional on parameter ranges, observations and threshold/error model; excludes added measurement and structural error.",
             "P-factor is coverage (0-1); R-factor is mean band width / observed standard deviation.",
             "This is not full SUFI-2 and cannot establish uncertainty coverage from a single small experiment."]
    if ensemble_kind == "sufi2_ensemble":
        notes.append("Complete final-round ensemble of the hand-written SPE method; the behavioral subset is reported separately and never replaces this band.")
    if measurement_error_percent:
        notes.append(f"P-factor applies the SWAT-CUP manual observation-error allowance of "
                     f"{measurement_error_percent:g}% (U+0.01*Pcnt*obs >= obs >= L-0.01*Pcnt*obs); "
                     "declared explicitly, not inferred.")
    return {"ensemble_kind": ensemble_kind, "interval_kind": interval_kind,
            "n_total": len(series), "n_behavioral": len(indices), "member_indices": indices,
            "threshold": float(threshold) if threshold is not None else None,
            "dates": dates, "lower": lower.tolist(), "median": median.tolist(), "upper": upper.tolist(),
            "rows": [{"date": date, "observed": float(obs[i]), "lower": float(lower[i]),
                      "median": float(median[i]), "upper": float(upper[i])} for i, date in enumerate(dates)],
            "p_factor": p_factor, "r_factor": r_factor,
            "measurement_error_percent": float(measurement_error_percent),
            "observed_std_convention": observed_std_convention,
            "observed_std_ddof": observed_std_ddof,
            "quantiles": [0.025, 0.975], "quantile_method": "linear", "equal_weight": True,
            "notes": notes}
