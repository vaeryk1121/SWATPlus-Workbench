"""Published estimates, not measured hydraulic properties or calibration."""
import math


def saxton_rawls(sand_percent,clay_percent,organic_matter_percent):
    """Saxton-Rawls 2006: S/C fractions, OM percent; no SPAW density/salt tweaks."""
    s,c,om=float(sand_percent)/100,float(clay_percent)/100,float(organic_matter_percent)
    if not 0<=s<=1 or not 0<=c<=1 or s+c>1.02 or not 0<=om<=8:
        raise ValueError('Saxton-Rawls矿质土估算要求砂/粘比例有效且OM在0—8%；泥炭等需另选资料/方法')
    t15=-.024*s+.487*c+.006*om+.005*s*om-.013*c*om+.068*s*c+.031
    wp=1.14*t15-.02
    t33=-.251*s+.195*c+.011*om+.006*s*om-.027*c*om+.452*s*c+.299
    fc=t33+1.283*t33*t33-.374*t33-.015
    ts=.278*s+.034*c+.022*om-.018*s*om-.027*c*om-.584*s*c+.078
    sat=fc+1.636*ts-.107-.097*s+.043
    if not 0<wp<fc<sat<1:raise ValueError('PTF产生不合理水分特征，未裁剪/补常数')
    lam=(math.log(fc)-math.log(wp))/(math.log(1500)-math.log(33))
    k=1930*(sat-fc)**(3-lam)
    return {'awc':fc-wp,'soil_k':k,'field_capacity':fc,'wilting_point':wp,'saturation':sat}


def epic_usle_k(sand,silt,clay,carbon):
    """Williams EPIC estimate in SWAT's customary K units; no SI multiplier."""
    sand,silt,clay,carbon=map(float,(sand,silt,clay,carbon))
    if min(silt,clay,carbon)<0 or clay+silt<=0:raise ValueError('USLE_K估算的质地/有机碳无效')
    sn=1-sand/100
    return ((.2+.3*math.exp(-.0256*sand*(1-silt/100)))*(silt/(clay+silt))**.3
            *(1-.25*carbon/(carbon+math.exp(3.72-2.95*carbon)))
            *(1-.7*sn/(sn+math.exp(-5.51+22.9*sn))))


def neh2009_group(layers,restricting_depth_mm,water_table_mm):
    """NEH630 Ch7 Table7-1; input depths must be supplied or explicit assumptions."""
    restriction,water=map(float,(restricting_depth_mm,water_table_mm))
    if restriction<=0 or water<0:raise ValueError('限制层/地下水位深度无效')
    if restriction<500 or water<600:return 'D'
    depth=1000 if min(restriction,water)>1000 else 500
    relevant=[float(r['soil_k']) for r in layers if r['top_mm']<depth]
    if not relevant or max(r['dp'] for r in layers)<depth:raise ValueError('土层未覆盖水文分组所需诊断深度')
    k=min(relevant)/3.6  # mm/h -> micrometers/s
    a,b,c=(10,4,.4) if depth==1000 else (40,10,1)
    return 'A' if k>a else 'B' if k>b else 'C' if k>c else 'D'
