"""Explicit station/variable/date matching for joint SWAT+ calibration.

No observations are generated, spatially interpolated, or converted implicitly.
Monthly flow is mean discharge; monthly water-balance depths are model totals.
"""
import csv
import datetime as dt
import math
from pathlib import Path
import re
from swatplus_profiles import read_output_header

OBJECTS={"channel_sd","basin_wb","hru_wb","aquifer","basin_aqu","reservoir","basin_res"}


def normalized_unit(value):
    return str(value).replace("³","3").replace("^","").replace(" ","").lower()


def read_observation(obs, root):
    result={};seen=set()
    with (Path(root)/obs["path"]).open(encoding="utf-8-sig",newline="") as stream:
        for row in csv.DictReader(stream):
            date=dt.datetime.strptime(row[obs["date_column"]],obs["date_format"]).date()
            if obs["frequency"]=="monthly":date=date.replace(day=1)
            key=date.isoformat()
            if key in seen:raise ValueError(f"{obs['id']}: duplicate observed date {key}")
            seen.add(key);raw=row[obs["value_column"]].strip()
            if raw in obs.get("missing_codes",[""]):continue
            value=float(raw)
            if not math.isfinite(value):raise ValueError(f"{obs['id']}: nonfinite observation {key}")
            if obs["target"]["variable"]=="flo_out" and value<0:raise ValueError("Discharge cannot be negative")
            result[key]=value
    if not result:raise ValueError(f"{obs['id']}: no usable observations")
    return result


def validate_observations(cfg, root):
    groups=cfg.get("observations",[])
    if not groups:raise ValueError("Research workflow requires observations[]")
    ids=set();targets=set();counts={}
    dream=cfg["search"]["method"]=="spotpy_dream"
    for obs in groups:
        identity=obs.get("id","")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,40}",identity) or identity in ids:
            raise ValueError("Observation IDs must be unique ASCII names, at most 41 characters")
        ids.add(identity)
        if obs.get("frequency") not in ("daily","monthly"):raise ValueError("Observation frequency must be daily or monthly")
        target=obs["target"]
        if target["object"] not in OBJECTS or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",target["variable"]):
            raise ValueError("Unsupported output object/variable")
        if type(target.get("unit_id")) is not int or target["unit_id"]<1:raise ValueError("Output unit_id must be positive integer")
        if normalized_unit(obs["unit"])!=normalized_unit(target["unit"]):raise ValueError("Observation/output units disagree")
        key=(target["object"],target["variable"],target["unit_id"],obs["frequency"])
        if key in targets:raise ValueError("Duplicate observation target/frequency would count the same series twice")
        targets.add(key)
        weight=obs.get("weight",1.0)
        if not math.isfinite(weight) or weight<=0:raise ValueError("Observation weight must be positive")
        metric=obs.get("metric","NSE")
        if metric not in ("NSE","KGE","NRMSE","gaussian_loglik"):raise ValueError("Unknown observation metric")
        if dream and (metric!="gaussian_loglik" or weight!=1):
            raise ValueError("DREAM requires Gaussian log likelihood, explicit sigma and weight=1 for every series")
        if not dream and metric=="gaussian_loglik":raise ValueError("Gaussian likelihood is reserved for the explicitly configured DREAM workflow")
        for name in (["sigma"] if metric=="gaussian_loglik" else ["scale"] if metric=="NRMSE" else []):
            if not math.isfinite(obs.get(name,0)) or obs.get(name,0)<=0:raise ValueError(f"{identity}: {name} must be positive in observation units")
        values=read_observation(obs,root)
        counts[identity]={}
        for label,period in cfg["periods"].items():
            part={d:v for d,v in values.items() if period["start"]<=d<=period["end"]}
            evaluate_one(obs,part,part,period)
            counts[identity][label]=len(part)
    return counts


def statistics(obs,sim):
    n=len(obs);mo=sum(obs)/n;ms=sum(sim)/n
    vo=sum((x-mo)**2 for x in obs)/n;vs=sum((x-ms)**2 for x in sim)/n
    mse=sum((a-b)**2 for a,b in zip(obs,sim))/n
    r=sum((a-mo)*(b-ms) for a,b in zip(obs,sim))/n/math.sqrt(vo*vs) if vo>0 and vs>0 else 0.0
    return {"n":n,"NSE":1-mse/vo if vo>0 else None,
            "KGE":1-math.sqrt((r-1)**2+(math.sqrt(vs/vo)-1)**2+(ms/mo-1)**2) if vo>0 and abs(mo)>1e-15 else None,
            "RMSE":math.sqrt(mse),"bias_percent":100*(ms-mo)/mo if abs(mo)>1e-15 else None,
            "r":r,"mean_reference":mo,"mean_simulated":ms}


def evaluate_one(definition,reference,simulation,period):
    dates=sorted(d for d in reference if period["start"]<=d<=period["end"])
    if len(dates)<2:raise ValueError(f"{definition['id']}: needs at least two paired values in this period")
    if any(d not in simulation for d in dates):raise ValueError(f"{definition['id']}: model output misses observed dates")
    obs=[reference[d] for d in dates];sim=[simulation[d] for d in dates]
    if not all(math.isfinite(x) for x in sim):raise ValueError("Nonfinite simulation values")
    result=statistics(obs,sim);metric=definition.get("metric","NSE")
    if metric=="gaussian_loglik":
        sigma=definition["sigma"]
        score=sum(-.5*((b-a)/sigma)**2-math.log(sigma*math.sqrt(2*math.pi)) for a,b in zip(obs,sim))
    elif metric=="NRMSE":score=-result["RMSE"]/definition["scale"]
    else:score=result[metric]
    if score is None or not math.isfinite(score):raise ValueError(f"{definition['id']}: {metric} is undefined")
    return {**result,"score":score,"metric":metric,"weight":definition.get("weight",1),
            "first_date":dates[0],"last_date":dates[-1],"unit":definition["unit"]}


def joint_score(definitions,references,simulations,period):
    by={d["id"]:evaluate_one(d,references[d["id"]],simulations[d["id"]],period) for d in definitions}
    likelihood=all(d.get("metric")=="gaussian_loglik" for d in definitions)
    total=sum(v["weight"]*v["score"] for v in by.values())
    if not likelihood:total/=sum(v["weight"] for v in by.values())
    return {"JOINT":total,"by_observation":by,"objective_kind":"gaussian_log_likelihood" if likelihood else "weighted_dimensionless_score"}


def target_specs(definitions):
    return [{"id":d["id"],"frequency":d["frequency"],**d["target"]} for d in definitions]


def read_target_csv(path):
    with Path(path).open(encoding="utf-8-sig",newline="") as stream:
        return {r["date"]:float(r["value"]) for r in csv.DictReader(stream)}


def extract_targets(model,work,spec):
    """Read actual output units and identifiers; supports interior river stations."""
    from swatplus_trial import expected_dates,version_ok
    result={}
    for target in spec.get("output_targets",[]):
        obj,freq=target["object"],target["frequency"]
        if obj not in OBJECTS or freq not in ("daily","monthly"):raise ValueError("Unsupported output target")
        path=Path(model)/f"{obj}_{'day' if freq=='daily' else 'mon'}.txt"
        values={};expected={d.isoformat() for d in expected_dates({**spec,"frequency":freq})}
        with path.open(encoding="utf-8-sig") as stream:
            _,header,unit_map=read_output_header(stream,spec["expected_version"],obj)
            variable=target["variable"]
            if variable not in unit_map or normalized_unit(unit_map[variable])!=normalized_unit(target["unit"]):
                raise ValueError(f"{target['id']}: output {variable} unit {unit_map.get(variable)} != {target['unit']}")
            ix={key:header.index(key) for key in ["unit","gis_id","name","yr","mon","day",variable]}
            for line in stream:
                tokens=line.split()
                if not tokens:continue
                if len(tokens)!=len(header):raise ValueError(f"{path.name}: column count mismatch")
                if int(tokens[ix["unit"]])!=target["unit_id"]:continue
                if "gis_id" in target and int(tokens[ix["gis_id"]])!=target["gis_id"]:raise ValueError("Output GIS ID mismatch")
                if target.get("name") and tokens[ix["name"]]!=target["name"]:raise ValueError("Output object name mismatch")
                date=dt.date(int(tokens[ix["yr"]]),int(tokens[ix["mon"]]),int(tokens[ix["day"]]) if freq=="daily" else 1).isoformat()
                if date in values:raise ValueError("Duplicated target output date")
                value=float(tokens[ix[variable]])
                if not math.isfinite(value):raise ValueError("Nonfinite target output")
                values[date]=value
        if set(values)!=expected:raise ValueError(f"{target['id']}: incomplete or extra output dates")
        destination=Path(work)/f"target_{target['id']}.csv"
        with destination.open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.writer(stream);writer.writerow(["date","value"]);writer.writerows(sorted(values.items()))
        result[target["id"]]={"path":str(destination),"target":target,"records":len(values)}
    return result
