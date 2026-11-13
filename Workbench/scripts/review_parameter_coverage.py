"""Read-only SWAT-CUP/SWAT+ parameter coverage review; writes only new reports."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CUP_MANUAL_URL = "https://swat.tamu.edu/media/114860/usermanual_swatcup.pdf"
DOCS = [
    ("SWAT-CUP用户手册", CUP_MANUAL_URL, "绝对边界第27页；参数化与分组第51—54页；敏感性第59—60页"),
    ("SWAT2012输入输出手册", "https://swat.tamu.edu/media/ovfp5zeb/swat-io-documentation-2012.pdf", "经典文件字段、对象、管理与气象输入参考"),
    ("SWAT+62.0.1参数变更", "https://docs.swat.tamu.edu/62.0.1/calibration/parameter-change-files/", "字典、原生操作、实际分支及已知错误/无效项"),
    ("SWAT+62.0.1条件与区域化", "https://docs.swat.tamu.edu/62.0.1/calibration/conditions-regionalization/", "对象、HRU条件及特殊路由限制"),
    ("SWAT+62.0.1硬率定", "https://docs.swat.tamu.edu/62.0.1/calibration/hard-calibration/", "原生启动变更与外部采样/评价的分工"),
    ("SWAT-CUP开发方", "https://www.2w2e.com/home/SwatCup", "经典SWAT-CUP产品说明"),
    ("SWATplus-CUP开发方", "https://www.2w2e.com/home/SwatPlusCup", "面向SWAT+的独立产品；不等于经典工程格式"),
]
ALIASES = {
    "CN2":"cn2", "ESCO":"esco", "EPCO":"epco", "LAT_TTIME":"lat_ttime", "CANMX":"canmx",
    "SOL_AWC":"awc", "SOL_K":"k", "SURLAG":"surlag", "ALPHA_BF":"alpha",
    "RCHRG_DP":"deep_seep", "GW_SPYLD":"sp_yld", "GWQMN":"flo_min", "GW_REVAP":"revap_co",
    "REVAPMN":"revap_min", "CH_N2":"chn", "CH_K2":"chk", "SFTMP":"snofall_tmp",
    "SMTMP":"snomelt_tmp", "SMFMX":"snomelt_max", "SMFMN":"snomelt_min", "TIMP":"snomelt_lag",
    "SLSOIL":"lat_len", "SOL_BD":"bd", "SOL_ALB":"alb", "EVRCH":"evrch",
    "OV_N":"ovn", "HRU_SLP":"slope", "SLSUBBSN":"slope_len", "SOL_CRK":"crk", "SOL_Z":"z",
    "SOL_CBN":"cbn", "CH_W2":"chw", "CH_D":"chd", "CH_S2":"chs", "CH_L2":"chl",
    "CH_SIDE":"chss", "CH_COV1":"cherod", "CH_COV2":"cov",
}
# These are documented review obstacles, not an expanded executable allowlist.
CACHED = {
    "ffcb", "evlai", "msk_co1", "msk_co2", "msk_x", "plaps", "tlaps", "adj_pkr", "dorm_hr",
    "cmn", "nperco", "pperco", "phoskd", "psp", "nperco_lchtile", "rsdco", "cbn", "lab_p", "hum_c_n", "hum_c_p",
}
GEOMETRY = {"ovn", "slope", "slope_len", "chw", "chd", "chs", "chl", "chss", "fpn", "bankfull_flo", "dep_bot", "crk"}
SPECIAL_PLANT = {"phu_mat", "lai_pot", "harv_idx", "bio_e"}
WRONG_TARGET = {"tile_latk":"原生分支写入tile lag而非latksat", "rk5":"原生分支写入rs2而非rk5"}


def read_text(path):
    return Path(path).read_text(encoding="utf-8-sig")


def classic_table(path):
    rows, section = [], "未分组"
    pattern = re.compile(r"^\s*(\S+)\s+([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)\s+([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)\s*(.*)$")
    for number, line in enumerate(read_text(path).splitlines(), 1):
        text = line.strip()
        if text.startswith("//"):
            title = text[2:].strip()
            if title and not set(title) <= set("- "):
                section = title
        elif text:
            match = pattern.match(line)
            if not match:
                raise ValueError(f"Unparsed table line {path}:{number}")
            rows.append(dict(name=match[1], lower=float(match[2]), upper=float(match[3]),
                             description=match[4], section=section, line=number))
    if len({row["name"].upper() for row in rows}) != len(rows):
        raise ValueError("Duplicate classic names need explicit disambiguation")
    return rows


def active_project(directory):
    inf = next(iter(directory.glob("[Ss][Uu][Ff][Ii]2.[Ii][Nn]/par_inf.txt")), None)
    if inf is None:
        return None
    lines = read_text(inf).splitlines()
    count, budget = int(lines[0].split()[0]), int(lines[1].split()[0])
    rows = []
    for number, line in enumerate(lines[2:], 3):
        if not line.strip():
            continue
        fields = line.split()
        identifier = fields[0]
        if not re.match(r"^[rva]__", identifier, re.I):
            raise ValueError(f"Invalid active parameter {inf}:{number}")
        rows.append(dict(identifier=identifier, lower=float(fields[1]), upper=float(fields[2]), line=number,
                         base_name=identifier.split("__",1)[1].split(".",1)[0].split("(",1)[0].upper()))
        if len(rows) == count:
            break
    if len(rows) != count:
        raise ValueError("Parameter count differs from active block")
    result = dict(phase=directory.name, configuration=str(inf), parameter_dimensions=count,
                  distinct_base_names=len({row["base_name"] for row in rows}), declared_simulations=budget, parameters=rows)
    summary = next(iter(directory.glob("[Ss][Uu][Ff][Ii]2.[Oo][Uu][Tt]/summary_stat.txt")), None)
    if summary:
        text = read_text(summary)
        result["stored_summary_file"] = str(summary)
        match = re.search(r"Best_goal\s*=\s*([-+\d.eE]+)", text)
        if match:
            result["stored_best_goal"] = float(match[1])
        result["stored_report_line"] = next((line.strip() for line in text.splitlines() if line.strip().startswith("FLOW_")), "")
    return result


def source_cases(path):
    result = {}
    for number, line in enumerate(read_text(path).splitlines(), 1):
        match = re.match(r"\s*case\s*\((.*?)\)", line, re.I)
        if match:
            for name in re.findall(r'["\']([^"\']+)["\']', match[1]):
                result.setdefault(name.strip(), []).append(number)
    return result


def review_modern(model, source, catalog):
    lines = read_text(model/"cal_parms.cal").splitlines()
    count = int(lines[1]); cases = source_cases(source/"src/cal_parm_select.f90")
    curated = {p["native_name"]:p for p in catalog}
    rows = []
    for number, line in enumerate(lines[3:], 4):
        if not line.strip():continue
        fields=line.split();name, obj=fields[:2]
        note="结构核对不等于功能/响应验证；未开放项不能直接投入搜索"
        if name in curated:
            status="已接入（含适用限制）" if name=="chn" else "已接入目录"
            note=curated[name].get("note", "") or "按当前目录的方法、范围和过程适用条件运行"
        elif name in SPECIAL_PLANT:
            status="植物特殊路由待处理"
            note="cal_conditions的plt路径与当前字典对象/分支不匹配；不按普通HRU或植物库参数直接开放"
        elif name not in cases:
            status="缺少同名字典分派分支"
            note="未找到cal_parm_select同名case；已检查特殊植物路径，不能据字典存在就宣称生效"
        elif name in WRONG_TARGET:
            status="已知错目标写入";note=WRONG_TARGET[name]
        elif name in CACHED:
            status="初始化/复制状态待处理"
            note="当前调用顺序或状态复制使原生赋值可能被覆盖或不影响实际过程；不能只增加目录条目"
        elif obj not in {"hru","sol","bsn","aqu","rte"}:
            status="当前缺少对象适配器"
            note="本轮未实现此对象的编号、文件、模块开关与输出验证；GWFlow等另行讨论"
        elif name in GEOMETRY:
            status="几何/衍生状态待核验"
            note="需核对曲线、汇流、裂隙或蓄水等派生状态的一致更新"
        elif name in {"z","clay","silt","sand"}:
            status="需要联合物理约束"
            note="土层深度递增/厚度或质地总和等约束不能用独立上下界代替"
        else:
            status="有分支但尚未验证接入"
        rows.append(dict(name=name, object_type=obj, lower=float(fields[2]), upper=float(fields[3]), units=fields[4],
                         dictionary_line=number, source_lines=cases.get(name, []), status=status, note=note,
                         catalog_enabled=name in curated))
    if len(rows)!=count or len({r["name"] for r in rows})!=count:
        raise ValueError("Dictionary count or names mismatch")
    extras=[dict(name=name,source_lines=positions,status="当前输入字典未登记；对象/边界/路由待核验")
            for name,positions in cases.items() if name not in {r["name"] for r in rows}]
    return rows,extras


def esc(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_reports(output, data):
    (output/"coverage.json").write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    classic=["# SWAT-CUP共享表：完整条目索引", "", "这是全部参考条目，不是可直接导入SWAT+的允许列表。未建立映射不表示SWAT+不存在相应过程。", "",
             "| 行号 | 分组 | 原名 | 下限 | 上限 | SWAT+候选 | 状态 |", "|---:|---|---|---:|---:|---|---|"]
    for row in data["classic_parameters"]:
        classic.append("| "+" | ".join(map(esc,[row["line"],row["section"],row["name"],row["lower"],row["upper"],row["modern_candidate"] or "—",row["status"]]))+" |")
    (output/"classic_parameters.md").write_text("\n".join(classic)+"\n",encoding="utf-8")
    modern=["# 当前SWAT+62.0.1模型字典：完整条目索引", "", "221是此模型导出的字典数量，不是全部SWAT+参数总数。以下按字典与源码做结构核查，待核查项均未新增到工作台允许列表。", "",
            "| 字典行 | 参数 | 对象 | 下限 | 上限 | 字典单位 | 源码分支行 | 状态 |", "|---:|---|---|---:|---:|---|---|---|"]
    for row in data["modern_parameters"]:
        modern.append("| "+" | ".join(map(esc,[row["dictionary_line"],row["name"],row["object_type"],row["lower"],row["upper"],row["units"],','.join(map(str,row["source_lines"])) or "—",row["status"]]))+" |")
    modern += ["", "## 当前字典以外的源码case", "", "这里只列源码名字，不据此补造对象、单位或边界；case存在也不证明能通过对象路由执行。", "", "| 名字 | case行号 |", "|---|---|"]
    for row in data["source_not_in_dictionary"]:modern.append(f"| {row['name']} | {','.join(map(str,row['source_lines']))} |")
    (output/"swatplus62_parameters.md").write_text("\n".join(modern)+"\n",encoding="utf-8")
    phases=["# 旧工程参数化与留存阶段记录", "", "所有评分均为已有文件记载，未重新运行、重算或确认观测来源；不将它们当作当前工作台率定成果。", "",
            "| 阶段 | 声明维数 | 不同基础名 | 声明模拟次数 | 留存Best_goal |", "|---|---:|---:|---:|---:|"]
    for phase in data["project_phases"]:phases.append(f"| {phase['phase']} | {phase['parameter_dimensions']} | {phase['distinct_base_names']} | {phase['declared_simulations']} | {phase.get('stored_best_goal','未读取')} |")
    phases += ["", "## 当前根目录活动定义", "", "只读头部声明的前N项，后续示例不计入活动参数。", "", "| 原定义 | 搜索下限 | 搜索上限 |", "|---|---:|---:|"]
    for row in data["project_phases"][0]["parameters"]:phases.append(f"| `{row['identifier']}` | {row['lower']} | {row['upper']} |")
    (output/"project_parameterizations.md").write_text("\n".join(phases)+"\n",encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cup-root",type=Path,required=True)
    parser.add_argument("--project-root",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    if not output.is_relative_to(ROOT):raise ValueError("Keep derived reports inside this workspace")
    output.mkdir(parents=True,exist_ok=False)
    shared=args.cup_root/"ExternalData/SourceData/Shared/Absolute_SWAT_Values.txt"
    local=args.project_root/"Absolute_SWAT_Values.txt"
    a,b=classic_table(shared),classic_table(local)
    table=lambda rows:{r["name"].upper():(r["lower"],r["upper"]) for r in rows}
    catalog=json.loads(read_text(ROOT/"config/native_parameter_catalog.json"))["parameters"]
    model=ROOT/"models/examples/robit344_check_b1/Scenarios/Default/TxtInOut"
    source=ROOT.parent/"Engine/swatplus"
    modern,extras=review_modern(model,source,catalog)
    native_names={r["name"] for r in modern};enabled={p["native_name"] for p in catalog}
    for row in a:
        name=row["name"].upper();candidate=ALIASES.get(name)
        if candidate is None and name.lower() in native_names:candidate=name.lower()
        row["modern_candidate"]=candidate
        if re.fullmatch(r"R\d+",name) and row["lower"]==row["upper"]==0:status="外部程序占位项"
        elif row["lower"]==row["upper"]:status="固定/非连续项，需核对类型"
        elif candidate in enabled:status="已有关联目录项，语义/范围仍需核对"
        elif candidate:status="同名或过程候选，未验证等价"
        elif row["section"] in {"MGT_OPT",".ops",".wus"}:status="管理/操作/用水，需专门适配"
        elif row["section"] in {".pcp",".tmp",".wgn"}:status="驱动/气候资料，需专门适配"
        else:status="尚未建立跨模型映射"
        row["status"]=status
    phases=[active_project(args.project_root)]
    phases += [p for directory in sorted((args.project_root/"Iterations").iterdir()) if directory.is_dir() and (p:=active_project(directory))]
    phases=[p for p in phases if p]
    log=read_text(args.project_root/"Swat_EditLog.txt");bd=[]
    for line in log.splitlines():
        if "Parameter: 'SOL_BD(" in line:
            match=re.search(r"New: '([^']+)'\s+Original: '([^']+)'",line)
            if match:bd.append((float(match[1]),float(match[2])))
    data=dict(purpose="full reference coverage inspection, not enabling all parameters or guaranteeing skill",
              checked_on="2026-10-22",sources=dict(cup_root=str(args.cup_root),project_root=str(args.project_root),
              shared_table=str(shared),model_dictionary=str(model/"cal_parms.cal"),source_selector=str(source/"src/cal_parm_select.f90")),
              official_sources=[dict(title=t,url=u,role=r) for t,u,r in DOCS],
              installation_and_project_names_ranges_equal=table(a)==table(b),classic_parameters=a,modern_parameters=modern,
              source_not_in_dictionary=extras,project_phases=phases,
              edit_log=dict(path=str(args.project_root/"Swat_EditLog.txt"),soil_bd_rows=len(bd),rows_at_upper_2_5=sum(abs(new-2.5)<1e-8 for new,old in bd),
              meaning="single retained edit log only; not all search candidates, not proof of the cause of poor scores"),
              statistics=dict(classic_entries=len(a),classic_groups=dict(Counter(r["section"] for r in a)),
              modern_entries=len(modern),modern_types=dict(Counter(r["object_type"] for r in modern)),
              modern_statuses=dict(Counter(r["status"] for r in modern)),source_cases_without_dictionary=len(extras),
              classic_statuses=dict(Counter(r["status"] for r in a))))
    write_reports(output,data)
    print(json.dumps(data["statistics"],ensure_ascii=False,indent=2))
    print("Names/ranges equal:",data["installation_and_project_names_ranges_equal"])
    print("Read project phases:",len(phases),"BD clipping log:",data["edit_log"])
    print("Reports:",output)


if __name__=="__main__":main()
