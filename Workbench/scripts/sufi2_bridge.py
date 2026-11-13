"""File adapter to the user's installed, unmodified classic SUFI-2 executables.

This is NOT a reimplementation of SUFI-2 and is NOT SWATplus-CUP/SPE.
Only the statistical programs are invoked. The caller must run SWAT+ with its
native calibration interface and supply aligned simulation series. Initial
coverage is one flow series, NSE, numeric parameter coordinates, no measurement error.
Every iteration needs its own directory; vendor tools overwrite IN/OUT files.
"""
from pathlib import Path
import json
import math
import re
import statistics
import subprocess
import time


from portable_runtime import read_config

DEFAULT_EXECUTABLE_DIR = Path(read_config().get("sufi2_executable_dir") or
                              "")
PROGRAMS = ("SUFI2_LH_sample.exe", "SUFI2_goal_fn.exe", "SUFI2_new_pars.exe",
            "SUFI2_95ppu.exe", "SUFI2_95ppu_beh.exe")


class Sufi2Error(RuntimeError):
    pass


def _number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("SUFI-2 inputs must be finite")
    return value


def _parameters(parameters):
    result = []
    for index, p in enumerate(parameters):
        native = str(p.get("native_name", p.get("column", p.get("name", "")))).lower()
        name = str(p.get("name", native))
        method = p.get("method", "absval")
        if method not in {"absval", "absolute", "pctchg", "abschg"} or not native or not name:
            raise ValueError("Declare a named native parameter with absval, pctchg or abschg coordinates")
        lower, upper = _number(p["lower"]), _number(p["upper"])
        # These are search-coordinate bounds, not final SWAT+ physical limits.
        # For percentage/additive change, never reuse native min/max: e.g. a
        # cn2 final [35,98] bound says nothing about its [-20,20] percent range.
        minimum = _number(p.get("coordinate_min", p.get("min", lower) if method in {"absval", "absolute"} else lower))
        maximum = _number(p.get("coordinate_max", p.get("max", upper) if method in {"absval", "absolute"} else upper))
        if not minimum <= lower < upper <= maximum:
            raise ValueError("Coordinate bounds must satisfy min <= lower < upper <= max")
        result.append(dict(name=name, native_name=native, coordinate_method=method, token=f"v__P{index+1:03d}.hru",
                           lower=lower, upper=upper, minimum=minimum, maximum=maximum))
    if not result or len(result) > 500 or len({p["name"] for p in result}) != len(result):
        raise ValueError("Provide 1..500 uniquely named parameter coordinates")
    return result


def nse(observed, simulated):
    mean = statistics.mean(observed)
    denominator = sum((x - mean) ** 2 for x in observed)
    if denominator <= 0:
        raise ValueError("NSE requires varying observations")
    return 1 - sum((a - b) ** 2 for a, b in zip(observed, simulated)) / denominator


class Sufi2Bridge:
    """Run locally installed classic SUFI-2 kernels in a dedicated scratch directory."""

    def __init__(self, executable_dir=DEFAULT_EXECUTABLE_DIR, work_dir=None, timeout=60):
        if work_dir is None:
            raise ValueError("An explicit iteration work_dir is required")
        self.executable_dir = Path(executable_dir).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.timeout = timeout
        if self.work_dir == self.executable_dir or self.executable_dir in self.work_dir.parents:
            raise ValueError("Do not write into the installed vendor program directory")
        for name in PROGRAMS:
            if not (self.executable_dir / name).is_file():
                raise FileNotFoundError(self.executable_dir / name)
        self.input_dir = self.work_dir / "SUFI2.IN"
        self.output_dir = self.work_dir / "SUFI2.OUT"
        for directory in (self.input_dir, self.output_dir, self.work_dir / "Echo"):
            directory.mkdir(parents=True, exist_ok=True)

    def _write(self, path, text):
        path.write_text(text, encoding="ascii")

    def _run(self, program, required):
        # Tools sometimes return 0 even after a fatal input-file error. Require
        # fresh expected output as well as checking exit status and console text.
        previous = {p: (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in required}
        start = time.perf_counter()
        try:
            proc = subprocess.run([str(self.executable_dir / program)], cwd=str(self.work_dir),
                                  input=b"", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=self.timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise Sufi2Error(f"{program} timed out; inspect the iteration directory") from exc
        output = proc.stdout.decode("utf-8", errors="replace")
        with (self.work_dir / "kernel.log").open("a", encoding="utf-8") as log:
            log.write(f"\n{program}: returncode={proc.returncode}, seconds={time.perf_counter()-start:.3f}\n{output}\n")
        if re.search(r"licen[cs]e|registration|activation|register", output, re.I):
            raise Sufi2Error(f"{program} reported a possible license prompt; stopped without bypassing it")
        if proc.returncode or re.search(r"cannot open|error opening|fatal error|singular matrix", output, re.I):
            raise Sufi2Error(f"{program} failed: {output[-1500:]}")
        for p in required:
            if not p.exists() or not p.stat().st_size or (p.stat().st_mtime_ns, p.stat().st_size) == previous[p]:
                raise Sufi2Error(f"{program} did not create fresh expected output: {p.name}")

    def _prepare_parameters(self, definitions, count):
        if type(count) is not int or not max(8, len(definitions) + 2) <= count <= 2000:
            raise ValueError("Use 8..2000 simulations per iteration; this legacy executable has a 2000 limit")
        p = _parameters(definitions)
        self._write(self.input_dir / "par_inf.txt", f"{len(p)} : parameters\n{count} : simulations\n\n" +
                    "".join(f'{x["token"]} {x["lower"]:.12g} {x["upper"]:.12g}\n' for x in p))
        self._write(self.work_dir / "Par_Name.out", "".join(f'{i+1}:{x["token"].upper().replace(".HRU", ".hru")}\n' for i, x in enumerate(p)))
        self._write(self.work_dir / "absolute_SWAT_values.txt", "".join(
            f'P{i+1:03d} {x["minimum"]:.12g} {x["maximum"]:.12g}\n' for i, x in enumerate(p)))
        return p

    def sample(self, parameters, count, seed):
        """Return the original LH executable's sample, preserving its six decimals.

        trk.txt is the vendor RNG tracking input. Retain par_val.txt/str.txt as
        the reproducible design; do not assume its RNG matches NumPy's seed.
        """
        if any(self.output_dir.iterdir()):
            raise ValueError("Sampling requires an empty SUFI2.OUT; use a new iteration directory")
        if type(seed) is not int or not 1 <= seed < 2147483647:
            raise ValueError("seed must be a positive 32-bit integer")
        p = self._prepare_parameters(parameters, count)
        self._write(self.input_dir / "trk.txt", str(seed) + "\n")
        self._run("SUFI2_LH_sample.exe", [self.input_dir / "par_val.txt", self.input_dir / "str.txt"])
        rows = [line.split() for line in (self.input_dir / "par_val.txt").read_text().splitlines() if line.strip()]
        if len(rows) != count:
            raise Sufi2Error("LH sample count mismatch")
        values = []
        for i, row in enumerate(rows):
            if len(row) != len(p) + 1 or int(row[0]) != i + 1:
                raise Sufi2Error("Invalid LH sample row")
            value = {x["name"]: _number(v) for x, v in zip(p, row[1:])}
            if any(not x["lower"] - 1e-6 <= value[x["name"]] <= x["upper"] + 1e-6 for x in p):
                raise Sufi2Error("LH generated a value outside the declared range")
            values.append(value)
        return values

    def _interval(self, prefix, expected_count, observed):
        rows = []
        for line in (self.output_dir / "95ppu.txt").read_text().splitlines():
            cols = line.split()
            if len(cols) == 5:
                try:
                    rows.append([_number(x) for x in cols])
                except ValueError:
                    pass
        if prefix == "behavioral" and len(rows) == 2 * expected_count:
            # This installed build appends the behavioral block to the all-run
            # file. Other versions may overwrite it; accept either single block
            # or this verified two-block layout, never mix their rows.
            rows = rows[-expected_count:]
        if len(rows) != expected_count:
            raise Sufi2Error("Invalid 95PPU data count")
        for row, obs in zip(rows, observed):
            if abs(row[0] - obs) > 2e-5 * max(1, abs(obs)) or row[1] > row[2]:
                raise Sufi2Error("Invalid 95PPU observed value or interval order")
        for name in ("95ppu.txt", "95ppu_g.txt", "summary_stat.txt"):
            self._write(self.output_dir / (prefix + "_" + name), (self.output_dir / name).read_text())
        # The old kernel uses population SD; also preserve its rounded metrics.
        pf = sum(lo <= obs <= hi for (obs, lo, hi, _, _) in rows) / len(rows)
        rf = statistics.mean(r[2] - r[1] for r in rows) / statistics.pstdev(observed)
        summary = (self.output_dir / "summary_stat.txt").read_text()
        stat_rows = [x.split() for x in summary.splitlines() if x.startswith("FLOW_OUT_1 ")]
        vendor_p, vendor_r = map(float, stat_rows[-1][1:3])
        if abs(pf - vendor_p) > .011 or abs(rf - vendor_r) > .011:
            raise Sufi2Error("Parsed interval does not reproduce the vendor's P/R factors")
        return {"lower": [r[1] for r in rows], "upper": [r[2] for r in rows],
                "best_simulation": [r[3] for r in rows], "mean": [r[4] for r in rows],
                "p_factor": pf, "r_factor": rf, "vendor_p_factor": vendor_p, "vendor_r_factor": vendor_r,
                "quantiles": "Original SUFI2_95ppu executable; not replaced by NumPy quantile"}

    def analyze(self, parameters, parameter_values, observations, simulations, dates=None, threshold=0.5):
        """Analyze aligned candidate x time values with original NSE/new-range/95PPU kernels.

        Values must use one common unit (initial verified use: monthly m3/s).
        Dates are labels; caller is responsible for alignment and calibration-
        only selection. Missing values must be jointly removed before this call.
        """
        count = len(parameter_values)
        p = self._prepare_parameters(parameters, count)
        obs = [_number(v) for v in observations]
        sims = [[_number(v) for v in row] for row in simulations]
        if not 3 <= len(obs) <= 30000 or len(sims) != count or any(len(s) != len(obs) for s in sims):
            raise ValueError("Expected 3..30000 aligned observations and one complete series per candidate")
        if any(v < 0 for v in obs) or any(v < 0 for row in sims for v in row):
            raise ValueError("This initial flow adapter requires nonnegative flow values")
        expected = [nse(obs, s) for s in sims]
        threshold = _number(threshold)
        if dates is None:
            dates = [f"step_{i+1}" for i in range(len(obs))]
        if len(dates) != len(obs) or any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", str(d)) for d in dates):
            raise ValueError("dates must be one ASCII label without spaces per time step")
        for row in parameter_values:
            for x in p:
                value = _number(row[x["name"]])
                if not x["lower"] - 1e-6 <= value <= x["upper"] + 1e-6:
                    raise ValueError("Parameter sample outside declared iteration bounds")
        self._write(self.input_dir / "par_val.txt", "".join(str(i+1) + " " + " ".join(
            f'{row[x["name"]]:.12g}' for x in p) + "\n" for i, row in enumerate(parameter_values)))
        self._write(self.input_dir / "observed.txt", f"1\n5\n{threshold:.12g}\n1\n\nFLOW_OUT_1\n1\n-1\n-1\n1\n1\n0\n{len(obs)}\n: index label value\n" +
                    "".join(f"{i+1} {date} {value:.12g}\n" for i, (date, value) in enumerate(zip(dates, obs))))
        self._write(self.input_dir / "var_file_name.txt", "FLOW_OUT_1.txt\n")
        self._write(self.output_dir / "FLOW_OUT_1.txt", "".join(str(k+1) + "\n" +
                    "".join(f"{i+1} {value:.12g}\n" for i, value in enumerate(row)) for k, row in enumerate(sims)))
        # The vendor omits this small derived file when no candidate passes.
        # Remove only a previous adapter-generated count to prevent stale reuse.
        count_file = self.output_dir / "no_beh_sims.txt"
        if count_file.exists():
            count_file.unlink()
        self._run("SUFI2_goal_fn.exe", [self.output_dir / n for n in ("goal.txt", "best_par.txt", "best_sim.txt")])
        goal_lines = (self.output_dir / "goal.txt").read_text().splitlines()[4:]
        scores = [float(line.split()[-1]) for line in goal_lines if line.strip()]
        if len(scores) != count or any(not math.isfinite(x) for x in scores):
            raise Sufi2Error("Invalid NSE results")
        max_error = max(abs(a-b) for a, b in zip(scores, expected))
        if any(abs(a-b) > 5e-5 * max(1, abs(b)) for a, b in zip(scores, expected)):
            raise Sufi2Error(f"Vendor NSE disagrees with independent NSE: maximum difference {max_error}")
        best_text = (self.output_dir / "best_par.txt").read_text()
        best_id = int(re.search(r"Best_sim_no=\s*(\d+)", best_text)[1])
        if not 1 <= best_id <= count or abs(expected[best_id-1] - max(expected)) > 5e-5:
            raise Sufi2Error("Vendor best candidate is inconsistent with NSE")
        best_values = parameter_values[best_id-1].copy()
        self._run("SUFI2_new_pars.exe", [self.output_dir / "new_pars.txt"])
        echo = (self.work_dir / "Echo/echo_new_pars.txt").read_text()
        best_section = echo.split("best parameters", 1)[-1].split("Sensitivity", 1)[0]
        for x in p:
            found = re.search(r"\d+\s+\d+:" + re.escape(x["token"]) + r"\s+([-+0-9.eE]+)", best_section, re.I)
            if not found or abs(float(found[1]) - best_values[x["name"]]) > 2e-6:
                raise Sufi2Error("new_pars did not read the actual best parameter; refuse zero-placeholder results")
        new_ranges, raw_ranges = {}, {}
        for line in (self.output_dir / "new_pars.txt").read_text().splitlines():
            row = line.split()
            if len(row) == 3:
                for x in p:
                    if row[0].lower() == x["token"].lower():
                        lo, hi = map(_number, row[1:])
                        raw_ranges[x["name"]] = [lo, hi]
                        lower, upper = max(lo, x["minimum"]), min(hi, x["maximum"])
                        if lower >= upper:
                            raise Sufi2Error("Vendor range is degenerate after physical-bound clipping")
                        new_ranges[x["name"]] = [lower, upper]
        if len(new_ranges) != len(p):
            raise Sufi2Error("Incomplete new parameter ranges")
        self._run("SUFI2_95ppu.exe", [self.output_dir / "95ppu.txt", self.output_dir / "summary_stat.txt"])
        all_interval = self._interval("all", len(obs), obs)
        behavioral_count = int(re.search(r"no_behav_sims=\s*(\d+)", count_file.read_text())[1]) if count_file.exists() else 0
        behavior = None
        if behavioral_count >= 2:
            self._run("SUFI2_95ppu_beh.exe", [self.output_dir / "95ppu.txt", self.output_dir / "summary_stat.txt"])
            behavior = self._interval("behavioral", len(obs), obs)
        result = {"engine": "Installed classic SWAT-CUP SUFI-2 statistical executables (not SWATplus-CUP/SPE)",
                  "executable_directory": str(self.executable_dir), "objective": "NSE", "nse": scores,
                  "independent_nse_max_difference": max_error, "best_simulation_number": best_id,
                  "best_parameters": best_values, "best_nse": expected[best_id-1],
                  "raw_new_ranges": raw_ranges, "new_ranges": new_ranges,
                  "range_policy": "Original new_pars proposal clipped to declared coordinate_min/coordinate_max; SWAT+ changes are applied by caller",
                  "parameter_coordinate_mapping": p,
                  "all_interval": all_interval, "behavioral_threshold": threshold,
                  "behavioral_count": behavioral_count, "behavioral_interval": behavior,
                  "behavioral_note": "" if behavior else "Fewer than two behavioral runs; no behavioral interval computed",
                  "dates": [str(d) for d in dates], "work_dir": str(self.work_dir),
                  "measurement_error_percent": 0}
        (self.work_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def prediction_interval(self, parameters, parameter_values, observations, simulations,
                            dates=None, best_index=0, selected_simulation=None, selected_parameters=None):
        """Evaluate a fixed calibration ensemble on validation data, using only 95ppu.

        best_index is zero-based and fixed by calibration, never chosen here.
        An independently selected calibration run may be supplied separately via
        selected_simulation/selected_parameters; it is NOT added to the ensemble.
        No validation goal-ranking, range-update or behavioral filtering occurs.
        """
        p = self._prepare_parameters(parameters, len(parameter_values))
        obs = [_number(v) for v in observations]
        sims = [[_number(v) for v in row] for row in simulations]
        if not 3 <= len(obs) <= 30000 or len(sims) != len(parameter_values) or any(len(s) != len(obs) for s in sims):
            raise ValueError("Provide one aligned validation simulation per calibration parameter vector")
        if type(best_index) is not int or not 0 <= best_index < len(sims):
            raise ValueError("best_index must identify a calibration-selected candidate (zero-based)")
        chosen = sims[best_index] if selected_simulation is None else [_number(v) for v in selected_simulation]
        chosen_parameters = parameter_values[best_index] if selected_parameters is None else selected_parameters
        if len(chosen) != len(obs) or any(v < 0 for v in obs + chosen) or any(v < 0 for row in sims for v in row):
            raise ValueError("Provide aligned, nonnegative validation flow series")
        chosen_nse = nse(obs, chosen)  # assessment of the fixed run, not selection
        if dates is None:
            dates = [f"step_{i+1}" for i in range(len(obs))]
        if len(dates) != len(obs) or any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", str(d)) for d in dates):
            raise ValueError("dates must be one ASCII label without spaces per step")
        self._write(self.input_dir / "par_val.txt", "".join(str(i+1) + " " + " ".join(
            f'{_number(row[x["name"]]):.12g}' for x in p) + "\n" for i, row in enumerate(parameter_values)))
        self._write(self.input_dir / "observed.txt", f"1\n5\n0\n1\n\nFLOW_OUT_1\n1\n-1\n-1\n1\n1\n0\n{len(obs)}\n: index label value\n" +
                    "".join(f"{i+1} {date} {value:.12g}\n" for i, (date, value) in enumerate(zip(dates, obs))))
        self._write(self.input_dir / "var_file_name.txt", "FLOW_OUT_1.txt\n")
        self._write(self.output_dir / "FLOW_OUT_1.txt", "".join(str(k+1) + "\n" +
                    "".join(f"{i+1} {value:.12g}\n" for i, value in enumerate(row)) for k, row in enumerate(sims)))
        # The original 95ppu program expects these legacy names. Their content
        # is explicitly supplied from calibration; no goal_fn invocation here.
        self._write(self.output_dir / "best_sim.txt", "FLOW_OUT_1\nobserved simulated\n" +
                    "".join(f"{o:.12g} {s:.12g}\n" for o, s in zip(obs, chosen)))
        # An unchanged baseline has no candidate coordinate vector. 95ppu reads
        # the header and supplied best_sim series; zero is an explicit external
        # (baseline) index, with no invented parameter-value rows.
        selected_number = best_index+1 if chosen_parameters else 0
        self._write(self.output_dir / "best_sim_nr.txt", str(selected_number) + "\n")
        parameter_rows = "" if not chosen_parameters else "".join(
            f'{i+1}:{x["token"].upper().replace(".HRU", ".hru")} {_number(chosen_parameters[x["name"]]):.12g} {x["lower"]:.12g} {x["upper"]:.12g}\n'
            for i, x in enumerate(p))
        self._write(self.output_dir / "best_par.txt", f"Goal_type= Nash_Sutcliff    No_sims= {len(sims)}     Best_sim_no= {selected_number}    Best_goal = {chosen_nse:.12g}\n\n" +
                    " Parameter_Name Fitted_Value Min_value Max_value\n" + parameter_rows)
        self._run("SUFI2_95ppu.exe", [self.output_dir / "95ppu.txt", self.output_dir / "summary_stat.txt"])
        interval = self._interval("fixed_validation", len(obs), obs)
        result = {"all_interval": interval, "selected_nse": chosen_nse,
                  "selected_parameters": chosen_parameters, "dates": [str(d) for d in dates],
                  "ensemble_size": len(sims), "selection": "Fixed by calibration; no validation selection or parameter-range update",
                  "selected_series_external_to_ensemble": selected_simulation is not None,
                  "selected_parameter_origin": "candidate coordinates" if chosen_parameters else "unchanged baseline; no candidate parameter vector",
                  "programs_called": ["SUFI2_95ppu.exe"], "work_dir": str(self.work_dir)}
        (self.work_dir / "validation_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
