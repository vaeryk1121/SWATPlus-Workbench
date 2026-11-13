"""Adapters to published SPOTPY optimizers and SciPy Latin hypercube sampling.

Dependencies live in tools/calibration_python and are loaded only by a DDS task.
The caller owns model execution, scoring, cancellation and completed-trial reuse.
"""
import importlib.metadata
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"spotpy": "1.6.2", "numpy": "1.26.4", "scipy": "1.13.1"}


class SearchPaused(Exception):
    pass


class SearchTargetReached(Exception):
    pass


def load_runtime():
    runtime = ROOT / "tools/calibration_python"
    bundled = ROOT.parent / "runtime/Lib/site-packages"
    if not (runtime / "spotpy").is_dir() and (bundled / "spotpy").is_dir():
        runtime = bundled
    if not (runtime / "spotpy").is_dir():
        raise RuntimeError("缺少DDS依赖。请在工作区执行：python -m pip install --only-binary=:all: "
                           "--target tools/calibration_python -r config/requirements-calibration.txt")
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    import numpy as np
    import scipy
    import spotpy
    actual = {"spotpy": importlib.metadata.version("spotpy"),
              "numpy": np.__version__, "scipy": scipy.__version__}
    if actual != EXPECTED:
        raise RuntimeError(f"DDS运行环境与已核对版本不一致：{actual}；预期 {EXPECTED}")
    return spotpy, np, actual


def engine_info(method="spotpy_dds"):
    if method == "swatcup_sufi2":
        return {"method": method, "algorithm": "SUFI-2",
                "implementation": "Installed classic SWAT-CUP statistical executables with SWAT+ native adapter",
                "parallel": "seq", "objective_direction": "maximize_NSE",
                "reference": "SWAT-CUP 2012 user manual, sequence of program execution",
                "note": "Calls original LH, goal, new_pars and 95PPU kernels; not SWATplus-CUP/SPE or Toolbox. Final-iteration ensemble is held fixed for validation."}
    if method == "pso":
        _, _, versions = load_runtime()
        return {"method": method, "algorithm": "PSO (gbest)",
                "implementation": "Hand-written NumPy implementation, not SPOTPY",
                "versions": versions, "parallel": "seq", "objective_direction": "maximize",
                "reference": "Kennedy and Eberhart (1995), doi:10.1109/ICNN.1995.488968; "
                             "Clerc and Kennedy (2002), doi:10.1109/TSMCB.2002.804360",
                "note": "c1=c2=2.0; inertia 0.9->0.4 linear; vmax=0.5*(max-min) with symmetric position "
                        "fold-back. Not the SWATplus-CUP PSO executable; RE constants were used only as a "
                        "behaviour-level cross-check."}
    if method == "spe_sufi2":
        _, _, versions = load_runtime()
        return {"method": method, "algorithm": "SUFI-2-style sequential parameter estimation (SPE)",
                "implementation": "Hand-written SciPy LHS + NumPy/SciPy OLS rounds; no vendor executable",
                "versions": versions, "parallel": "seq", "objective_direction": "maximize_NSE",
                "reference": "Abbaspour, Johnson and van Genuchten (2004), Vadose Zone J. 3:1340-1352, "
                             "doi:10.2136/vzj2004.1340; Abbaspour et al. (2007), J. Hydrol. 333:413-430, "
                             "doi:10.1016/j.jhydrol.2006.09.014; Abbaspour et al. (2015), J. Hydrol. 524:733-752, "
                             "doi:10.1016/j.jhydrol.2015.03.027; SWAT-CUP manual SUFI-2/95PPU chapters",
                "note": "Latin hypercube rounds, auxiliary OLS sensitivity, range update with a t*sigma width "
                        "centred on the round's best member and clipped to fixed hard bounds, 95PPU from the "
                        "final ensemble in sensitivity_uncertainty. Not classic SWAT-CUP kernels and not "
                        "SWATplus-CUP/SPE."}
    _, _, versions = load_runtime()
    if method == "spotpy_sceua":
        return {"method": method, "algorithm": "SCE-UA", "implementation": "SPOTPY",
                "versions": versions, "parallel": "seq", "objective_direction": "negative_score_minimized",
                "reference": "Duan, Sorooshian and Gupta (1994), Optimal use of the SCE-UA global optimization method for calibrating watershed models; cited by SPOTPY sceua.py",
                "note": "Real model calls have a hard budget. Resume requires deterministic replay, not an internal optimizer checkpoint."}
    if method == "glue_lhs":
        return {"method": method, "algorithm": "Latin hypercube behavioral ensemble", "implementation": "SciPy qmc.LatinHypercube",
                "versions": versions, "parallel": "seq", "objective_direction": "maximize",
                "note": "Uniform independent parameter priors and explicit behavioral threshold; not SUFI-2 or a Bayesian posterior."}
    if method == "spotpy_dream":
        return {"method": method, "algorithm": "DREAM", "implementation": "SPOTPY",
                "versions": versions, "parallel": "seq", "objective_direction": "gaussian_log_likelihood",
                "reference": "Vrugt (2016), Markov chain Monte Carlo simulation using the DREAM software package; cited by SPOTPY dream.py",
                "note": "Uniform priors; acceptance option 2; recorded Markov chain states include repeated rejected states. NSE/KGE cannot replace log likelihood."}
    if method != "spotpy_dds":
        raise ValueError(f"Unsupported mature search method: {method}")
    return {"method": "spotpy_dds", "algorithm": "DDS", "implementation": "SPOTPY",
            "versions": versions, "parallel": "seq",
            "reference": "Tolson and Shoemaker (2007), doi:10.1029/2005WR004723",
            "note": "Published SPOTPY implementation; not a Toolbox API or SUFI-2."}


def run_dds(parameters, repetitions, seed, r, evaluate):
    """Pass each candidate dict to evaluate, which returns a score to MAXIMIZE.

    Explicit bounds prevent SPOTPY's sample-based bound estimation. Replaying
    the same inputs/seed and cached scores reproduces subsequent DDS proposals.
    """
    spotpy, np, _ = load_runtime()
    np.random.seed(seed)
    random.seed(seed)
    names = [p["name"] for p in parameters]

    class Setup:
        def __init__(self):
            self.params = [spotpy.parameter.Uniform(
                p["name"], p["lower"], p["upper"], minbound=p["lower"], maxbound=p["upper"],
                optguess=(p["lower"] + p["upper"]) / 2, step=(p["upper"] - p["lower"]) * r
            ) for p in parameters]

        def parameters(self):
            return spotpy.parameter.generate(self.params)

        def simulation(self, vector):
            return [float(evaluate(dict(zip(names, map(float, vector)))))]

        def evaluation(self):
            return [0.0]

        def objectivefunction(self, simulation, evaluation, params=None):
            # The workflow has already computed NSE or KGE using calibration
            # dates only. This scalar is the objective, not simulated flow.
            return float(simulation[0])

    sampler = spotpy.algorithms.dds(Setup(), random_state=seed, parallel="seq", r=r,
                                    dbformat="ram", save_sim=False, db_precision=np.float64)
    sampler.sample(repetitions, trials=1)


def symmetric_fold(values, lower, upper):
    """Reflect positions that left the bounds back inside, once, symmetrically.

    With |v| <= 0.5*(upper-lower) and the previous position inside the bounds,
    one reflection always returns a position inside the bounds; the trailing
    clip only guards floating-point rounding at an edge. This is the documented
    boundary handling of this project's PSO. The SWATplus-CUP binary instead
    clamps the velocity only (RE cross-check note, not ported code).
    """
    folded = []
    for value, low, high in zip(values, lower, upper):
        if value < low:
            value = 2 * low - value
        elif value > high:
            value = 2 * high - value
        folded.append(min(max(value, low), high))
    return folded


def run_pso(parameters, repetitions, seed, evaluate, options=None):
    """Hand-written gbest PSO; evaluate returns one finite score to MAXIMIZE.

    Implemented from the publications, not from vendor code:
    - Kennedy & Eberhart (1995), doi:10.1109/ICNN.1995.488968: particle swarm
      with personal/global best updates and x <- x + v.
    - Clerc & Kennedy (2002), doi:10.1109/TSMCB.2002.804360: the fixed
      coefficients c1=c2=2.0 come from their analysed parameter set.
    - Decreasing inertia weight (Shi & Eberhart 1998 variant): linear from w0
      (default 0.9) to w_end (default 0.4) with w_t = w0-(w0-w_end)*t/T for
      iterations t=1..T, so both endpoints are used exactly.
    - vmax = 0.5*(upper-lower) per dimension; velocities are clamped to
      +-vmax and out-of-bounds positions are folded back once symmetrically.
    - Initial positions are uniform random; initial velocity is zero. The
      SWATplus-CUP binary initializes velocity to vmax*r and samples with the
      C runtime RNG; the RE report was compared at magnitude/behaviour level
      only.

    The caller owns model execution through evaluate; pause, cancellation and
    budget exceptions propagate from it. Replaying with the same
    versions/settings/seed and identical cached scores reproduces the same
    proposals; this is not a serialized optimizer checkpoint. Budget
    exhaustion does not establish convergence.
    """
    _, np, _ = load_runtime()
    requirements = validate_search(parameters, "pso", repetitions, options)
    swarm, iterations = requirements["swarm_size"], requirements["iterations"]
    rng = np.random.RandomState(seed)
    names = [p["name"] for p in parameters]
    lower = np.asarray([p["lower"] for p in parameters], dtype=float)
    upper = np.asarray([p["upper"] for p in parameters], dtype=float)
    vmax = 0.5 * (upper - lower)
    w0 = float(options.get("w0", 0.9)) if options else 0.9
    w_end = float(options.get("w_end", 0.4)) if options else 0.4

    positions = lower + rng.uniform(size=(swarm, len(names))) * (upper - lower)
    velocities = np.zeros((swarm, len(names)))
    personal_best = positions.copy()
    scores = np.asarray([float(evaluate(dict(zip(names, map(float, row))))) for row in positions])
    personal_scores = scores.copy()
    gbest = int(np.argmax(scores))
    gbest_score = float(scores[gbest])
    gbest_trace = [gbest_score]
    schedule = []
    for t in range(1, iterations + 1):
        weight = w0 - (w0 - w_end) * t / iterations
        schedule.append(weight)
        for i in range(swarm):
            r1 = rng.uniform(size=len(names))
            r2 = rng.uniform(size=len(names))
            velocities[i] = (weight * velocities[i]
                             + 2.0 * r1 * (personal_best[i] - positions[i])
                             + 2.0 * r2 * (personal_best[gbest] - positions[i]))
            velocities[i] = np.clip(velocities[i], -vmax, vmax)
            candidate = symmetric_fold(positions[i] + velocities[i], lower, upper)
            score = float(evaluate(dict(zip(names, map(float, candidate)))))
            positions[i] = candidate
            if score > personal_scores[i]:
                personal_scores[i] = score
                personal_best[i] = candidate
                if score > gbest_score:
                    gbest, gbest_score = i, score
        gbest_trace.append(gbest_score)
    return {"swarm_size": swarm, "iterations": iterations, "iterations_completed": iterations,
            "inertia_schedule": schedule, "gbest_trace": gbest_trace,
            "best_parameters": {name: float(value) for name, value in zip(names, personal_best[gbest])},
            "best_score": gbest_score}


def _spe_new_ranges(previous_ranges, best_values, sensitivity, hard_bounds):
    """One SUFI-2-style range update from a completed round's OLS regression.

    Abbaspour et al. (2004; 2007) define the new range from the regression
    uncertainties as b_i +- t*sigma_bi, with sigma_bi used when |t| < 1. The
    SWAT-CUP description of the same algorithm states the new ranges are
    always smaller than the previous ones and are centred around the best
    simulation. A standardized OLS slope lives in score units and cannot be a
    parameter-space centre, so this implementation keeps the documented
    t*sigma width (converted to raw parameter units via the standardization
    scale) and centres it on the round's best member; the deliberate deviation
    from a literal b_i-centre is recorded here rather than hidden.
    """
    rows = {row["name"]: row for row in sensitivity.get("parameters", [])}
    if not rows:
        raise ValueError(f"SPE range update requires a usable OLS regression: "
                         f"{sensitivity.get('reason') or 'no parameter rows'}")
    ranges, raw_ranges = {}, {}
    for name, (low, high) in previous_ranges.items():
        row = rows[name]
        standard_error = float(row["raw_standard_error"])
        if not math.isfinite(standard_error) or standard_error <= 0:
            raise ValueError(f"SPE range update for {name}: nonpositive raw standard error")
        half_width = max(abs(float(row["t_stat"])), 1.0) * standard_error  # |t|<1 -> sigma_bi
        half_width = min(half_width, 0.5 * (high - low))  # never wider than the previous round
        centre = float(best_values[name])
        raw_ranges[name] = [centre - half_width, centre + half_width]
        low_hard, high_hard = hard_bounds[name]
        new_low, new_high = max(raw_ranges[name][0], low_hard), min(raw_ranges[name][1], high_hard)
        if not new_low < new_high:
            raise ValueError(f"SPE range for {name} degenerated after hard-bound clipping")
        ranges[name] = [new_low, new_high]
    return ranges, raw_ranges


def run_spe(parameters, repetitions, seed, evaluate, options=None):
    """SUFI-2-style sequential rounds (SPE); evaluate returns scores to MAXIMIZE.

    Implemented from the publications and the SWAT-CUP manual; the installed
    classic executables and the SWATplus-CUP RE report served only as
    behaviour-level cross-checks, never as ported code:
    - Round 1 samples the initial ranges with a Latin hypercube design
      (Abbaspour et al. 2007, SUFI-2 step 2); later rounds sample the updated
      ranges the same way, with deterministic per-round seeds seed+k-1.
    - After each round an auxiliary OLS regression of the score on
      standardized parameters (sufi2_control.regression_sensitivity) provides
      sensitivity and coefficient standard errors.
    - The next range per parameter has half-width max(|t|,1)*sigma in raw
      parameter units, never wider than the previous round, is centred on the
      round's best member and is clipped to the fixed coordinate bounds
      (Abbaspour et al. 2004/2007; SWAT-CUP manual; see _spe_new_ranges).
    - First version: no parameter freezing/screening and zero declared
      measurement error. The final round's complete ensemble feeds the 95PPU
      post-processing in the workflow (sensitivity_uncertainty.py).

    The caller owns model execution through evaluate; pause, cancellation and
    budget exceptions propagate from it. Failed/non-finite runs abort the
    search (no silent removal), and an unusable OLS update aborts with its
    reason instead of guessing new ranges. Replaying with the same
    versions/settings/seed and identical cached scores reproduces the round
    designs; this is not a serialized checkpoint. Round budget exhaustion
    does not establish convergence.
    """
    load_runtime()
    requirements = validate_search(parameters, "spe_sufi2", repetitions, options)
    round_sizes = requirements["round_sizes"]
    from scipy.stats import qmc
    from sufi2_control import regression_sensitivity
    names = [p["name"] for p in parameters]
    hard_bounds = {p["name"]: (float(p.get("coordinate_min", p["lower"])),
                               float(p.get("coordinate_max", p["upper"]))) for p in parameters}
    ranges = {p["name"]: [float(p["lower"]), float(p["upper"])] for p in parameters}
    rounds, evaluated = [], 0
    best_score, best_values = -math.inf, None
    for iteration, count in enumerate(round_sizes, start=1):
        bounds = [ranges[name] for name in names]
        design = qmc.scale(qmc.LatinHypercube(d=len(names), seed=seed + iteration - 1).random(n=count),
                           [b[0] for b in bounds], [b[1] for b in bounds])
        values = [dict(zip(names, map(float, row))) for row in design]
        member_indices = list(range(evaluated, evaluated + count))
        scores = [float(evaluate(row)) for row in values]
        evaluated += count
        local_best = max(range(count), key=scores.__getitem__)
        if scores[local_best] > best_score:
            best_score, best_values = scores[local_best], values[local_best]
        sensitivity = regression_sensitivity(parameters, values, scores)
        record = {"iteration": iteration, "member_indices": member_indices,
                  "ranges": {name: list(ranges[name]) for name in names},
                  "round_best_member_index": member_indices[local_best],
                  "round_best_score": scores[local_best], "sensitivity": sensitivity}
        if iteration < len(round_sizes):
            record["new_ranges"], record["raw_new_ranges"] = _spe_new_ranges(
                ranges, values[local_best], sensitivity, hard_bounds)
            ranges = record["new_ranges"]
        rounds.append(record)
    return {"round_sizes": list(round_sizes), "rounds": rounds,
            "final_round_member_indices": rounds[-1]["member_indices"],
            "trial_rounds": [r["iteration"] for r in rounds for _ in r["member_indices"]],
            "best_parameters": dict(best_values), "best_score": best_score}


def validate_search(parameters, method, repetitions, options=None):
    """Check mathematical/sample-size requirements before an expensive model run."""
    options = dict(options or {})
    names = [str(p["name"]) for p in parameters]
    if not names or len(set(names)) != len(names):
        raise ValueError("Search parameters require unique, non-empty names")
    for p in parameters:
        lo, hi = float(p["lower"]), float(p["upper"])
        if not (math.isfinite(lo) and math.isfinite(hi) and lo < hi):
            raise ValueError(f"Parameter {p['name']} requires finite lower < upper")
    if type(repetitions) is not int or repetitions < 2:
        raise ValueError("repetitions must be an integer >= 2")
    if method == "swatcup_sufi2":
        from sufi2_control import round_plan
        return round_plan(parameters, repetitions, options)
    if method == "spe_sufi2":
        from sufi2_control import round_plan
        requirements = round_plan(parameters, repetitions, options)
        percent = options.get("measurement_error_percent", 0.0)
        if type(percent) not in (int, float) or not math.isfinite(percent) or percent < 0:
            raise ValueError("SPE measurement_error_percent must be a finite nonnegative percentage")
        return requirements
    if method == "pso":
        swarm = options.get("swarm_size", 10)
        if type(swarm) is not int or swarm < 2:
            raise ValueError("PSO swarm_size must be an integer >= 2")
        w0 = float(options.get("w0", 0.9))
        w_end = float(options.get("w_end", 0.4))
        for label, value in (("w0", w0), ("w_end", w_end)):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"PSO {label} must be a finite inertia weight in [0,1]")
        if not w0 > w_end:
            raise ValueError("PSO requires w0 > w_end for a decreasing inertia schedule")
        iterations = options.get("iterations")
        if iterations is None:
            # The budget is spent exactly: swarm_size*(1+iterations) calls, no silent remainder.
            if repetitions < 2 * swarm or repetitions % swarm:
                raise ValueError(f"PSO repetitions must be a multiple of swarm_size={swarm} and at least "
                                 f"{2 * swarm} (initial sweep plus one full iteration)")
            iterations = repetitions // swarm - 1
        else:
            if type(iterations) is not int or iterations < 1:
                raise ValueError("PSO iterations must be an integer >= 1")
            if swarm * (1 + iterations) != repetitions:
                raise ValueError(f"PSO budget mismatch: swarm_size*(1+iterations)={swarm * (1 + iterations)} "
                                 f"differs from repetitions={repetitions}")
        return {"minimum_evaluations": 2 * swarm, "swarm_size": swarm, "iterations": iterations}
    if method == "spotpy_sceua":
        ngs = options.get("ngs", max(2, len(parameters) + 1))
        if type(ngs) is not int or ngs < 2:
            raise ValueError("SCE-UA ngs must be an integer >= 2")
        population = (2 * len(parameters) + 1) * ngs
        if repetitions <= population:
            raise ValueError(f"SCE-UA needs more than {population} actual model calls: initial population=(2D+1)*ngs; D={len(parameters)}, ngs={ngs}")
        kstop = options.get("kstop", 10)
        if type(kstop) is not int or kstop < 2:
            raise ValueError("SCE-UA kstop must be an integer >= 2")
        for key, default in (("pcento", 0.1), ("peps", 0.001)):
            if not math.isfinite(float(options.get(key, default))) or float(options.get(key, default)) <= 0:
                raise ValueError(f"SCE-UA {key} must be finite and positive")
        return {"minimum_evaluations": population + 1, "initial_population": population, "ngs": ngs}
    if method == "spotpy_dream":
        if options.get("objective_kind") != "gaussian_log_likelihood":
            raise ValueError("DREAM requires objective_kind='gaussian_log_likelihood'; NSE or KGE are not log likelihoods")
        chains, delta = options.get("n_chains", 7), options.get("delta", 3)
        if type(delta) is not int or delta < 1 or type(chains) is not int or chains < max(4, 2 * delta + 1):
            raise ValueError("DREAM n_chains must be >= max(4,2*delta+1), with integer delta >= 1")
        if type(options.get("n_cr", 3)) is not int or options.get("n_cr", 3) < 1:
            raise ValueError("DREAM n_cr must be a positive integer")
        if repetitions < chains * 10:
            raise ValueError(f"DREAM needs at least {chains*10} calls to initialize and diagnose chains; this minimum does not establish convergence")
        if not 0 < float(options.get("burn_in_fraction", 0.5)) < 1:
            raise ValueError("DREAM burn_in_fraction must be in (0,1)")
        if not 1 < float(options.get("convergence_limit", 1.2)) <= 1.2:
            raise ValueError("DREAM convergence_limit must be in (1,1.2]")
        if type(options.get("min_retained_per_chain", 100)) is not int or options.get("min_retained_per_chain", 100) < 20:
            raise ValueError("DREAM min_retained_per_chain must be an integer >= 20")
        return {"minimum_evaluations": chains * 10, "n_chains": chains, "delta": delta}
    if method == "spotpy_dds":
        if repetitions < 7:
            raise ValueError("DDS requires >= 7 evaluations: 5 initialization points and >= 2 dynamic steps")
        if not 0 < float(options.get("r", 0.2)) <= 1:
            raise ValueError("DDS r must be in (0,1]")
    elif method != "glue_lhs":
        raise ValueError(f"Unsupported mature search method: {method}")
    return {"minimum_evaluations": 7 if method == "spotpy_dds" else 2}


def validate_search_settings(method, parameters, repetitions, options=None):
    """Method-first convenience API used by the workflow's preflight check."""
    return validate_search(parameters, method, repetitions, options)


class _BudgetExhausted(Exception):
    pass


def run_search(method, parameters, repetitions, seed, evaluate, options=None, should_stop=None):
    """Evaluate bounded candidates with a score to MAXIMIZE.

    The caller assigns candidate IDs, persists scores and owns all model runs.
    SearchPaused and SearchTargetReached from evaluate propagate unchanged.
    Replaying requires the same versions/settings/seed and identical cached
    scores. It is not a serialized SCE-UA optimizer checkpoint. No parallel
    calls are made, so model concurrency must not alter candidate ordering.
    """
    options = dict(options or {})
    requirements = validate_search(parameters, method, repetitions, options)
    if method == "swatcup_sufi2":
        raise ValueError("SUFI-2 requires ResearchWorkflow's file-based kernel bridge, not the SPOTPY scalar callback")
    spotpy, np, _ = load_runtime()
    count = 0
    best = -math.inf
    evaluation_lookup = {}

    def checked(values):
        nonlocal count, best
        if should_stop is not None and should_stop():
            raise SearchPaused()
        if count >= repetitions:
            raise _BudgetExhausted()
        score = float(evaluate(values))
        if not math.isfinite(score):
            raise ValueError("Search objective must be finite; failed runs must be explicitly handled by the caller")
        evaluation_lookup[tuple(values[p["name"]] for p in parameters)] = count
        count += 1
        best = max(best, score)
        return score

    if method == "spotpy_dds":
        run_dds(parameters, repetitions, seed, options.get("r", 0.2), checked)
        return {"method": method, "evaluations": count, "best_score": best, "stop_reason": "budget",
                "budget": repetitions, "status": "completed", "converged": False}
    if method == "glue_lhs":
        from scipy.stats import qmc
        design = qmc.LatinHypercube(d=len(parameters), seed=seed).random(n=repetitions)
        bounds = [(float(p["lower"]), float(p["upper"])) for p in parameters]
        design = qmc.scale(design, [b[0] for b in bounds], [b[1] for b in bounds])
        names = [p["name"] for p in parameters]
        for vector in design:
            checked(dict(zip(names, map(float, vector))))
        return {"method": method, "evaluations": count, "best_score": best, "stop_reason": "budget",
                "budget": repetitions, "status": "completed", "converged": False,
                "ensemble_kind": "independent_uniform_prior_lhs"}
    if method == "pso":
        state = run_pso(parameters, repetitions, seed, checked, options)
        return {"method": method, "evaluations": count, "best_score": best, "stop_reason": "budget",
                "budget": repetitions, "status": "completed", "converged": False, **state,
                "note": "Hand-written gbest PSO per Kennedy & Eberhart (1995) and Clerc & Kennedy (2002); "
                        "replay requires the same versions/settings/seed and identical cached scores, not a "
                        "serialized optimizer checkpoint. Budget exhaustion does not establish convergence."}
    if method == "spe_sufi2":
        state = run_spe(parameters, repetitions, seed, checked, options)
        return {"method": method, "evaluations": count, "best_score": best, "stop_reason": "budget",
                "budget": repetitions, "status": "completed", "converged": False, **state,
                "note": "SUFI-2-style rounds implemented from Abbaspour et al. (2004/2007/2015) and the "
                        "SWAT-CUP manual; the final-round ensemble feeds the workflow 95PPU. Replay requires "
                        "the same versions/settings/seed and identical cached scores, not a serialized "
                        "checkpoint. Round budget exhaustion does not establish convergence."}

    np.random.seed(seed)
    random.seed(seed)
    names = [p["name"] for p in parameters]

    class Setup:
        def __init__(self):
            self.params = [spotpy.parameter.Uniform(
                p["name"], p["lower"], p["upper"], minbound=p["lower"], maxbound=p["upper"],
                optguess=(p["lower"] + p["upper"]) / 2,
                step=(p["upper"] - p["lower"]) * 0.2) for p in parameters]

        def parameters(self):
            return spotpy.parameter.generate(self.params)

        def simulation(self, vector):
            return [checked(dict(zip(names, map(float, vector))))]

        def evaluation(self):
            return [0.0]

        def objectivefunction(self, simulation, evaluation, params=None):
            # SPOTPY SCE-UA is a MINIMIZER; DREAM requires the original log likelihood.
            return -float(simulation[0]) if method == "spotpy_sceua" else float(simulation[0])

    if method == "spotpy_dream":
        states = []

        class RecordedDream(spotpy.algorithms.dream):
            def update_mcmc_status(self, par, like, sim, cur_chain):
                super().update_mcmc_status(par, like, sim, cur_chain)
                vector = tuple(map(float, par))
                states.append({"chain": int(cur_chain), "iteration": int(self.nChainruns[cur_chain]),
                               "values": dict(zip(names, vector)), "log_likelihood": float(like),
                               "evaluation_index": evaluation_lookup[vector]})

        sampler = RecordedDream(Setup(), random_state=seed, parallel="seq", dbformat="ram",
                                save_sim=False, db_precision=np.float64)
        stop_reason = "library_stopping_criterion"
        try:
            # In SPOTPY option 2 accepts by exp(logL_new-logL_old). Its default
            # option 6 expects a different SSR convention and must not be used.
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                sampler.sample(repetitions, nChains=requirements["n_chains"], delta=requirements["delta"],
                               nCr=options.get("n_cr", 3), convergence_limit=options.get("convergence_limit", 1.2),
                               runs_after_convergence=repetitions, acceptance_test_option=2)
        except _BudgetExhausted:
            stop_reason = "budget"
        if count >= repetitions:
            stop_reason = "budget"
        burn_fraction = float(options.get("burn_in_fraction", 0.5))
        retained = []
        chain_counts = []
        for chain in range(requirements["n_chains"]):
            chain_rows = [row for row in states if row["chain"] == chain]
            keep = chain_rows[int(len(chain_rows) * burn_fraction):]
            retained.extend(keep)
            chain_counts.append(len(keep))
        rhat = next((np.asarray(x, dtype=float) for x in reversed(getattr(sampler, "r_hats", []))
                     if np.asarray(x).size == len(parameters)), np.full(len(parameters), np.nan))
        limit = float(options.get("convergence_limit", 1.2))
        min_retained = options.get("min_retained_per_chain", 100)
        diagnosed = bool(np.isfinite(rhat).all() and (rhat < limit).all() and min(chain_counts) >= min_retained)
        return {"method": method, "evaluations": count, "best_score": best, "stop_reason": stop_reason,
                "budget": repetitions, "status": "completed", "converged": False,
                "chain_states": states, "posterior_samples": retained,
                "burn_in_fraction": burn_fraction, "retained_per_chain": chain_counts,
                "min_retained_per_chain": min_retained,
                "spotpy_rhat": [float(x) if np.isfinite(x) else None for x in rhat],
                "convergence_limit": limit, "posterior_usable": diagnosed,
                "convergence_proven": False,
                "note": "Rhat and retained counts are screening diagnostics, not proof of mixing; inspect traces and effective sample size before scientific inference."}

    sampler = spotpy.algorithms.sceua(Setup(), random_state=seed, parallel="seq",
                                     dbformat="ram", save_sim=False, db_precision=np.float64)
    stop_reason = "library_stopping_criterion"
    try:
        sampler.sample(repetitions, ngs=requirements["ngs"], kstop=options.get("kstop", 10),
                       pcento=options.get("pcento", 0.1), peps=options.get("peps", 0.001))
    except _BudgetExhausted:
        # SCE-UA counts saved simplex outputs separately from reflection and
        # contraction calls. Limit actual expensive calls, never silently exceed.
        stop_reason = "budget"
    if count >= repetitions:
        stop_reason = "budget"
    elif getattr(sampler.status, "stop", False):
        stop_reason = "library_evaluation_limit"
    return {"method": method, "evaluations": count, "best_score": best,
            "budget": repetitions, "status": "completed", "converged": False,
            "stop_reason": stop_reason, "initial_population": requirements["initial_population"],
            "convergence_proven": False}
