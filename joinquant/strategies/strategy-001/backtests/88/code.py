# 策略名：ETF多因子轮动
enable_profile()
STRATEGY_NAME = "ETF多因子轮动"
try:
    import FeishuRelayTools
except ImportError:
    FeishuRelayTools = None

"""
============================================================
策略名称：ETF 多因子轮动策略（相对倾斜版）
策略类型：周线级别、场内基金、多因子动态配置
适用标的：159819.XSHE、513100.XSHG、518880.XSHG（中文名运行时通过聚宽 API 读取）

核心思想：
  趋势门槛判断"能不能买"，风险平价分配基础仓位，
  动量和 RSRS 作为资产间相对倾斜信号调整权重分配（不改变组合总仓位），
  拥挤度惩罚和组合波动率控制在过热或高波时平滑降仓。
  剩余仓位保留现金，不重新归一化到满仓。

模块分工：
  - 趋势门槛（硬过滤）：按 ETF 专属趋势均线以上才可入选，0/1 离散
  - 风险平价（逆波动率）：所有趋势成立资产参与，σ 越小权重越大
  - 动量倾斜（相对信号）：多周期排名分数去均值，clip 到 [TiltMin, TiltMax]；
    可选将极端高分资产的动量倾斜压回中性
  - RSRS 倾斜（相对信号）：High~Low 回归 β 标准化 × R² 去均值，clip 到 [TiltMin, TiltMax]
  - 倾斜合成：RPWeight × MomentumTilt × RSRSTilt，活跃资产内重新归一化
  - 拥挤度惩罚（只减不加）：五指标分位数均值，超阈值线性打折
  - 组合波动率控制（只缩不放）：RawWeight 组合波动率超目标时等比缩放

核心公式：
  TiltedWeight_i = normalize(RPWeight_i × MomentumTilt_i × RSRSTilt_i)
  RawWeight_i = TiltedWeight_i × TrendGate_i × CrowdPenalty_i
  FinalWeight_i = RawWeight_i × PortfolioVolAssetScale_i

调仓频率：每周开盘检查一次
============================================================
"""

import json
from datetime import date, datetime

import numpy as np
import pandas as pd


# ============================================================
# 常量 — 内部字段名到聚宽字段名的映射
# ============================================================
FIELD_MAP = {
    "close": "close",
    "high": "high",
    "low": "low",
    "amount": "money",
}

JQ_AUTO_AUDIT_TOKEN = "manual"
JQ_AUTO_AUDIT_DIR = "jq_auto_audit"
PARAM_DEFAULTS = {
    "etf_pool": (
        "159819.XSHE",
        "513100.XSHG",
        "518880.XSHG",
    ),
    "MA_long": 120,
    "MA_long_by_etf": (20, 40, 100),
    "MomShort": 20,
    "MomMid": 60,
    "MomLong": 120,
    "w20": 0.2,
    "w60": 0.3,
    "w120": 0.5,
    "TopK": 3,
    "VolWindow": 60,
    "annual_factor": 252,
    "RSRS_N": 18,
    "RSRS_M": 600,
    "RSRS_NegativeFullCut": 1.8,
    "RSRSMinMultiplier": 0.0,
    "RSRSMaxMultiplier": 1.3,
    "MomentumTiltStrength": 0.50,
    "MomentumTiltMin": 0.70,
    "MomentumTiltMax": 1.30,
    "MomentumExtremeScoreStart": None,
    "MomentumExtremeTiltCap": 1.00,
    "RSRSTiltMin": 0.70,
    "RSRSTiltMax": 1.30,
    "CrowdWindow": 500,
    "CrowdRetShort": 20,
    "CrowdRetMid": 60,
    "AmountMAWindow": 20,
    "DeviationMAWindow": 20,
    "CrowdVolWindow": 20,
    "CrowdStart": 0.60,
    "CrowdEnd": 0.95,
    "MinCrowdPenalty": 0.30,
    "CrowdStart_by_etf": (0.60, 0.60, 0.80),
    "CrowdEnd_by_etf": None,
    "MinCrowdPenalty_by_etf": None,
    "CrowdRetShort_by_etf": None,
    "CrowdRetMid_by_etf": None,
    "PortfolioVolWindow": 40,
    "TargetVol": 0.08,
    "MaxPortfolioVolScale": 1.0,
    "PortfolioVolReliefMode": "dyn_marginal",
    "GoldVolReliefFraction": 0.5,
    "GoldVolReliefMaxRatio": 2.0,
    "DynamicVolReliefFraction": 1.0,
    "DynamicVolReliefMaxRatio": 1.5,
    "DynamicVolReliefMomentumWindow": 20,
    "DynamicVolReliefCovWindow": 40,
    "MaxWeight": 0.60,
    "MinWeight": 0.05,
    "RebalanceThreshold": 0.03,
    "MaxTotalWeight": 1.0,
    "ExecutionTimingMode": "baseline",
    "use_real_price": False,
    "fq_mode": None,
    "history_buffer": 100,
    "benchmark": "000300.XSHG",
}
EXECUTION_TIMING_MODES = (
    "baseline",
    "logic-2-delay-only",
    "logic-3-live-like",
)
DEFAULT_EXECUTION_TIMING_MODE = PARAM_DEFAULTS["ExecutionTimingMode"]
PORTFOLIO_VOL_RELIEF_MODES = (
    "baseline",
    "fixed_gold",
    "dyn_marginal",
)
PORTFOLIO_VOL_RELIEF_DEFAULTS = {
    "PortfolioVolReliefMode": PARAM_DEFAULTS["PortfolioVolReliefMode"],
    "GoldVolReliefFraction": PARAM_DEFAULTS["GoldVolReliefFraction"],
    "GoldVolReliefMaxRatio": PARAM_DEFAULTS["GoldVolReliefMaxRatio"],
    "DynamicVolReliefFraction": PARAM_DEFAULTS["DynamicVolReliefFraction"],
    "DynamicVolReliefMaxRatio": PARAM_DEFAULTS["DynamicVolReliefMaxRatio"],
    "DynamicVolReliefMomentumWindow": PARAM_DEFAULTS["DynamicVolReliefMomentumWindow"],
    "DynamicVolReliefCovWindow": PARAM_DEFAULTS["DynamicVolReliefCovWindow"],
}


def fund_code(security):
    """从聚宽证券代码中提取 6 位基金代码，用于报告和日志展示。"""
    return str(security).split(".")[0]


def format_etf_name(security, name):
    """保证基金显示名使用 中文名(聚宽代码) 标准格式。"""
    security = str(security)
    code = fund_code(security)
    display_name = str(name).strip() if name is not None else str(security)
    full_suffix = "(%s)" % security
    short_suffix = "(%s)" % code
    if display_name == security:
        return display_name
    if display_name.endswith(full_suffix):
        return display_name
    if code and display_name.endswith(short_suffix):
        display_name = display_name[:-len(short_suffix)].strip()
    return "%s%s" % (display_name, full_suffix)


def build_etf_display_names(pool, names=None):
    """按 etf_pool 顺序生成带编号的基金显示名列表。"""
    names = names or []
    result = []
    for i, etf in enumerate(pool):
        base_name = names[i] if i < len(names) else etf
        result.append(format_etf_name(etf, base_name))
    return result


def fetch_etf_official_name(security, fallback_name=None):
    """通过聚宽 API 读取基金官方中文名，失败时回退到已有名称或代码。"""
    try:
        info = get_security_info(security)
        for attr in ("display_name", "name"):
            value = getattr(info, attr, None)
            if value:
                return str(value).strip()
    except Exception as exc:
        log.warning("fetch ETF official name failed: security=%s error=%s", security, exc)

    return fallback_name or str(security)


def load_etf_display_names(pool, fallback_names=None):
    """从聚宽官方证券信息生成标准 ETF 显示名。"""
    fallback_names = fallback_names or []
    names = []
    for i, etf in enumerate(pool):
        fallback_name = fallback_names[i] if i < len(fallback_names) else None
        official_name = fetch_etf_official_name(etf, fallback_name=fallback_name)
        names.append(format_etf_name(etf, official_name))
    return names


def _audit_jsonable(value):
    """Convert strategy/runtime values to JSON-safe audit payloads."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    try:
        if isinstance(value, np.generic):
            return _audit_jsonable(value.item())
        if isinstance(value, np.ndarray):
            return [_audit_jsonable(item) for item in value.tolist()]
    except Exception:
        pass
    try:
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, (pd.Series, pd.Index)):
            return [_audit_jsonable(item) for item in value.tolist()]
        if isinstance(value, pd.DataFrame):
            return [
                {str(k): _audit_jsonable(v) for k, v in row.items()}
                for row in value.reset_index().to_dict(orient="records")
            ]
    except Exception:
        pass
    if isinstance(value, dict):
        return {str(k): _audit_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_audit_jsonable(item) for item in value]
    return str(value)


def _context_time_fields(context):
    result = {}
    for name in ("current_dt", "previous_date"):
        value = getattr(context, name, None) if context is not None else None
        if value is not None:
            result[name] = _audit_jsonable(value)
    return result


def audit_event(event, context=None, **payload):
    """Write one complete business-audit event to JoinQuant research storage."""
    path = getattr(g, "audit_path", "")
    if not path:
        return
    seq = getattr(g, "audit_seq", 0) + 1
    g.audit_seq = seq
    row = {
        "seq": seq,
        "event": event,
        "audit_token": getattr(g, "audit_token", ""),
    }
    row.update(_context_time_fields(context))
    row.update({key: _audit_jsonable(value) for key, value in payload.items()})
    write_file(path, json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n", append=True)


def _copy_runtime_default(value):
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _runtime_param(name, default):
    if not hasattr(g, name):
        setattr(g, name, _copy_runtime_default(default))
    return getattr(g, name)


def _runtime_list_param(name):
    value = _runtime_param(name, PARAM_DEFAULTS[name])
    if value is None:
        return None
    return list(value)


# ============================================================
# snapshot_params — 参数快照
# ============================================================
def snapshot_params():
    """
    从 g 读取全部策略参数，返回只读快照 dict。

    核心计算函数通过接收 params 而非直接读 g，实现解耦。
    """
    etf_pool = _runtime_list_param("etf_pool")
    raw_etf_names = _runtime_param("etf_names", etf_pool)
    etf_names = build_etf_display_names(etf_pool, list(raw_etf_names))
    return {
        "etf_pool": etf_pool,
        "etf_names": etf_names,
        "benchmark": _runtime_param("benchmark", PARAM_DEFAULTS["benchmark"]),
        "MA_long": _runtime_param("MA_long", PARAM_DEFAULTS["MA_long"]),
        "MA_long_by_etf": _runtime_list_param("MA_long_by_etf"),
        "MomShort": _runtime_param("MomShort", PARAM_DEFAULTS["MomShort"]),
        "MomMid": _runtime_param("MomMid", PARAM_DEFAULTS["MomMid"]),
        "MomLong": _runtime_param("MomLong", PARAM_DEFAULTS["MomLong"]),
        "w20": _runtime_param("w20", PARAM_DEFAULTS["w20"]),
        "w60": _runtime_param("w60", PARAM_DEFAULTS["w60"]),
        "w120": _runtime_param("w120", PARAM_DEFAULTS["w120"]),
        "TopK": _runtime_param("TopK", PARAM_DEFAULTS["TopK"]),
        "VolWindow": _runtime_param("VolWindow", PARAM_DEFAULTS["VolWindow"]),
        "annual_factor": _runtime_param("annual_factor", PARAM_DEFAULTS["annual_factor"]),
        "RSRS_N": _runtime_param("RSRS_N", PARAM_DEFAULTS["RSRS_N"]),
        "RSRS_M": _runtime_param("RSRS_M", PARAM_DEFAULTS["RSRS_M"]),
        "RSRS_NegativeFullCut": _runtime_param("RSRS_NegativeFullCut", PARAM_DEFAULTS["RSRS_NegativeFullCut"]),
        "RSRSMinMultiplier": _runtime_param("RSRSMinMultiplier", PARAM_DEFAULTS["RSRSMinMultiplier"]),
        "RSRSMaxMultiplier": _runtime_param("RSRSMaxMultiplier", PARAM_DEFAULTS["RSRSMaxMultiplier"]),
        "MomentumTiltStrength": _runtime_param("MomentumTiltStrength", PARAM_DEFAULTS["MomentumTiltStrength"]),
        "MomentumTiltMin": _runtime_param("MomentumTiltMin", PARAM_DEFAULTS["MomentumTiltMin"]),
        "MomentumTiltMax": _runtime_param("MomentumTiltMax", PARAM_DEFAULTS["MomentumTiltMax"]),
        "MomentumExtremeScoreStart": _runtime_param("MomentumExtremeScoreStart", PARAM_DEFAULTS["MomentumExtremeScoreStart"]),
        "MomentumExtremeTiltCap": _runtime_param("MomentumExtremeTiltCap", PARAM_DEFAULTS["MomentumExtremeTiltCap"]),
        "RSRSTiltMin": _runtime_param("RSRSTiltMin", PARAM_DEFAULTS["RSRSTiltMin"]),
        "RSRSTiltMax": _runtime_param("RSRSTiltMax", PARAM_DEFAULTS["RSRSTiltMax"]),
        "CrowdWindow": _runtime_param("CrowdWindow", PARAM_DEFAULTS["CrowdWindow"]),
        "CrowdRetShort": _runtime_param("CrowdRetShort", PARAM_DEFAULTS["CrowdRetShort"]),
        "CrowdRetMid": _runtime_param("CrowdRetMid", PARAM_DEFAULTS["CrowdRetMid"]),
        "AmountMAWindow": _runtime_param("AmountMAWindow", PARAM_DEFAULTS["AmountMAWindow"]),
        "DeviationMAWindow": _runtime_param("DeviationMAWindow", PARAM_DEFAULTS["DeviationMAWindow"]),
        "CrowdVolWindow": _runtime_param("CrowdVolWindow", PARAM_DEFAULTS["CrowdVolWindow"]),
        "CrowdStart": _runtime_param("CrowdStart", PARAM_DEFAULTS["CrowdStart"]),
        "CrowdEnd": _runtime_param("CrowdEnd", PARAM_DEFAULTS["CrowdEnd"]),
        "MinCrowdPenalty": _runtime_param("MinCrowdPenalty", PARAM_DEFAULTS["MinCrowdPenalty"]),
        "CrowdStart_by_etf": _runtime_list_param("CrowdStart_by_etf"),
        "CrowdEnd_by_etf": _runtime_list_param("CrowdEnd_by_etf"),
        "MinCrowdPenalty_by_etf": _runtime_list_param("MinCrowdPenalty_by_etf"),
        "CrowdRetShort_by_etf": _runtime_list_param("CrowdRetShort_by_etf"),
        "CrowdRetMid_by_etf": _runtime_list_param("CrowdRetMid_by_etf"),
        "PortfolioVolWindow": _runtime_param("PortfolioVolWindow", PARAM_DEFAULTS["PortfolioVolWindow"]),
        "TargetVol": _runtime_param("TargetVol", PARAM_DEFAULTS["TargetVol"]),
        "MaxPortfolioVolScale": _runtime_param("MaxPortfolioVolScale", PARAM_DEFAULTS["MaxPortfolioVolScale"]),
        "PortfolioVolReliefMode": _runtime_param(
            "PortfolioVolReliefMode",
            PORTFOLIO_VOL_RELIEF_DEFAULTS["PortfolioVolReliefMode"],
        ),
        "GoldVolReliefFraction": _runtime_param(
            "GoldVolReliefFraction",
            PORTFOLIO_VOL_RELIEF_DEFAULTS["GoldVolReliefFraction"],
        ),
        "GoldVolReliefMaxRatio": _runtime_param(
            "GoldVolReliefMaxRatio",
            PORTFOLIO_VOL_RELIEF_DEFAULTS["GoldVolReliefMaxRatio"],
        ),
        "DynamicVolReliefFraction": _runtime_param(
            "DynamicVolReliefFraction",
            PORTFOLIO_VOL_RELIEF_DEFAULTS["DynamicVolReliefFraction"],
        ),
        "DynamicVolReliefMaxRatio": _runtime_param(
            "DynamicVolReliefMaxRatio",
            PORTFOLIO_VOL_RELIEF_DEFAULTS["DynamicVolReliefMaxRatio"],
        ),
        "DynamicVolReliefMomentumWindow": _runtime_param(
            "DynamicVolReliefMomentumWindow",
            PORTFOLIO_VOL_RELIEF_DEFAULTS["DynamicVolReliefMomentumWindow"],
        ),
        "DynamicVolReliefCovWindow": _runtime_param(
            "DynamicVolReliefCovWindow",
            PORTFOLIO_VOL_RELIEF_DEFAULTS["DynamicVolReliefCovWindow"],
        ),
        "MaxWeight": _runtime_param("MaxWeight", PARAM_DEFAULTS["MaxWeight"]),
        "MinWeight": _runtime_param("MinWeight", PARAM_DEFAULTS["MinWeight"]),
        "RebalanceThreshold": _runtime_param("RebalanceThreshold", PARAM_DEFAULTS["RebalanceThreshold"]),
        "MaxTotalWeight": _runtime_param("MaxTotalWeight", PARAM_DEFAULTS["MaxTotalWeight"]),
        "ExecutionTimingMode": _runtime_param(
            "ExecutionTimingMode",
            DEFAULT_EXECUTION_TIMING_MODE,
        ),
        "use_real_price": _runtime_param("use_real_price", PARAM_DEFAULTS["use_real_price"]),
        "fq_mode": _runtime_param("fq_mode", PARAM_DEFAULTS["fq_mode"]),
        "history_buffer": _runtime_param("history_buffer", PARAM_DEFAULTS["history_buffer"]),
        "audit_token": getattr(g, "audit_token", ""),
        "audit_path": getattr(g, "audit_path", ""),
    }


# ============================================================
# validate_params — 参数校验
# ============================================================
def validate_params(params):
    """
    校验参数合法性，不合法时抛出 ValueError。

    校验规则来自技术实现方案 4.1 节参数校验表。
    """
    errors = []

    if params["MA_long"] <= 0:
        errors.append("MA_long must be positive")
    ma_long_by_etf = params.get("MA_long_by_etf")
    if ma_long_by_etf is not None:
        if not isinstance(ma_long_by_etf, (list, tuple)):
            errors.append("MA_long_by_etf must be a list or tuple when provided")
        else:
            if len(ma_long_by_etf) != len(params["etf_pool"]):
                errors.append("MA_long_by_etf length must match etf_pool")
            for window in ma_long_by_etf:
                if not isinstance(window, (int, float)) or window <= 0:
                    errors.append("MA_long_by_etf values must be positive")
                    break
    if abs(params["w20"] + params["w60"] + params["w120"] - 1.0) > 1e-8:
        errors.append("momentum weights must sum to 1")
    if params["TopK"] < 1:
        errors.append("TopK must be >= 1")
    if not (0 < params["MaxWeight"] <= params["MaxTotalWeight"] <= 1):
        errors.append("MaxWeight must be in (0, MaxTotalWeight] and MaxTotalWeight <= 1")
    if not (0 <= params["MinWeight"] <= params["MaxWeight"]):
        errors.append("MinWeight must be in [0, MaxWeight]")
    if params["TargetVol"] <= 0:
        errors.append("TargetVol must be positive")
    if params["PortfolioVolReliefMode"] not in PORTFOLIO_VOL_RELIEF_MODES:
        errors.append("PortfolioVolReliefMode must be one of %s" % (PORTFOLIO_VOL_RELIEF_MODES,))
    for fraction_name in ("GoldVolReliefFraction", "DynamicVolReliefFraction"):
        value = params[fraction_name]
        if not (0.0 <= value <= 1.0):
            errors.append("%s must be in [0.0, 1.0]" % fraction_name)
    for ratio_name in ("GoldVolReliefMaxRatio", "DynamicVolReliefMaxRatio"):
        value = params[ratio_name]
        if value <= 1.0:
            errors.append("%s must be > 1.0" % ratio_name)
    for window_name in ("DynamicVolReliefMomentumWindow", "DynamicVolReliefCovWindow"):
        value = params[window_name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append("%s must be a positive integer" % window_name)
    if params["RSRS_M"] <= 0 or params["RSRS_N"] <= 1:
        errors.append("RSRS_M must be positive and RSRS_N must be > 1")
    if not (0 <= params["CrowdStart"] < params["CrowdEnd"] <= 1):
        errors.append("Crowd thresholds must satisfy 0 <= CrowdStart < CrowdEnd <= 1")
    for per_etf_name in ("CrowdStart_by_etf", "CrowdEnd_by_etf", "MinCrowdPenalty_by_etf",
                         "CrowdRetShort_by_etf", "CrowdRetMid_by_etf"):
        per_etf_val = params.get(per_etf_name)
        if per_etf_val is not None:
            if not isinstance(per_etf_val, (list, tuple)):
                errors.append("%s must be a list or tuple when provided" % per_etf_name)
            elif len(per_etf_val) != len(params["etf_pool"]):
                errors.append("%s length must match etf_pool" % per_etf_name)
    starts_etf = params.get("CrowdStart_by_etf")
    ends_etf = params.get("CrowdEnd_by_etf")
    if starts_etf is not None and ends_etf is not None:
        for idx, (s, e) in enumerate(zip(starts_etf, ends_etf)):
            if not (0 <= s < e <= 1):
                errors.append("CrowdStart_by_etf[%d]=%s must satisfy 0 <= start < end <= 1, got end=%s" % (idx, s, e))
                break
    elif starts_etf is not None:
        for idx, s in enumerate(starts_etf):
            if not (0 <= s < params["CrowdEnd"] <= 1):
                errors.append("CrowdStart_by_etf[%d]=%s must satisfy 0 <= start < CrowdEnd=%s" % (idx, s, params["CrowdEnd"]))
                break
    elif ends_etf is not None:
        for idx, e in enumerate(ends_etf):
            if not (params["CrowdStart"] < e <= 1):
                errors.append("CrowdEnd_by_etf[%d]=%s must satisfy CrowdStart=%s < end <= 1" % (idx, e, params["CrowdStart"]))
                break
    if params["MomentumTiltStrength"] < 0:
        errors.append("MomentumTiltStrength must be >= 0")
    if not (0 < params["MomentumTiltMin"] <= 1 <= params["MomentumTiltMax"]):
        errors.append("Momentum tilt bounds must satisfy 0 < min <= 1 <= max")
    extreme_start = params["MomentumExtremeScoreStart"]
    if extreme_start is not None and not (0 < extreme_start <= 1):
        errors.append("MomentumExtremeScoreStart must be None or satisfy 0 < start <= 1")
    if not (1 <= params["MomentumExtremeTiltCap"] <= params["MomentumTiltMax"]):
        errors.append("MomentumExtremeTiltCap must satisfy 1 <= cap <= MomentumTiltMax")
    if not (0 < params["RSRSTiltMin"] <= 1 <= params["RSRSTiltMax"]):
        errors.append("RSRS tilt bounds must satisfy 0 < min <= 1 <= max")
    if params["ExecutionTimingMode"] not in EXECUTION_TIMING_MODES:
        errors.append("ExecutionTimingMode must be one of %s" % (EXECUTION_TIMING_MODES,))
    if len(params["etf_pool"]) != len(params["etf_names"]):
        errors.append("etf_pool and etf_names must have the same length")
    for etf, name in zip(params["etf_pool"], params["etf_names"]):
        if str(etf) not in str(name):
            errors.append("etf_names must include JoinQuant security code: %s" % etf)

    if errors:
        raise ValueError("; ".join(errors))


def resolve_ma_long_windows(params):
    """返回与 etf_pool 对齐的趋势均线窗口。"""
    ma_long_by_etf = params.get("MA_long_by_etf")
    if ma_long_by_etf is None:
        return [params["MA_long"]] * len(params["etf_pool"])
    return list(ma_long_by_etf)


def resolve_crowd_thresholds(params):
    """返回与 etf_pool 对齐的 (CrowdStart, CrowdEnd, MinCrowdPenalty) 三元组列表。"""
    n = len(params["etf_pool"])
    starts = params.get("CrowdStart_by_etf")
    starts = list(starts) if starts is not None else [params["CrowdStart"]] * n
    ends = params.get("CrowdEnd_by_etf")
    ends = list(ends) if ends is not None else [params["CrowdEnd"]] * n
    mins = params.get("MinCrowdPenalty_by_etf")
    mins = list(mins) if mins is not None else [params["MinCrowdPenalty"]] * n
    return list(zip(starts, ends, mins))


def resolve_crowd_ret_windows(params):
    """返回与 etf_pool 对齐的 (CrowdRetShort, CrowdRetMid) 二元组列表。"""
    n = len(params["etf_pool"])
    shorts = params.get("CrowdRetShort_by_etf")
    shorts = list(shorts) if shorts is not None else [params["CrowdRetShort"]] * n
    mids = params.get("CrowdRetMid_by_etf")
    mids = list(mids) if mids is not None else [params["CrowdRetMid"]] * n
    return list(zip(shorts, mids))


# ============================================================
# initialize — 策略初始化
# ============================================================
def initialize(context):
    """
    由聚宽框架在回测/模拟启动时自动调用一次。

    作用：
    - 向 g 对象写入全部策略参数
    - 设置交易费用（场内基金免印花税，佣金万分之一）
    - 设置固定滑点 0
    - 注册每周开盘调仓任务
    """
    set_parameter(context)
    validate_params(snapshot_params())
    write_file(g.audit_path, "", append=False)
    audit_event("run_start", context, params=snapshot_params())
    set_option('use_real_price', g.use_real_price)
    set_option("avoid_future_data", True)

    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0,
            open_commission=0.0001,
            close_commission=0.0001,
            min_commission=0
        ),
        type='fund'
    )

    set_slippage(FixedSlippage(0.0), type='fund')

    if g.ExecutionTimingMode == "baseline":
        run_weekly(
            weekly_check,
            weekday=1,
            time='open',
            reference_security='000300.XSHG'
        )
    elif g.ExecutionTimingMode == "logic-2-delay-only":
        run_weekly(
            prepare_delay_only_rebalance,
            weekday=1,
            time='open',
            reference_security='000300.XSHG'
        )
        run_daily(
            execute_pending_rebalance,
            time='open',
            reference_security='000300.XSHG'
        )
    else:
        run_weekly(
            mark_live_like_signal_day,
            weekday=1,
            time='open',
            reference_security='000300.XSHG'
        )
        run_daily(
            execute_live_like_rebalance,
            time='open',
            reference_security='000300.XSHG'
        )


def on_strategy_end(context):
    """Record the final audit marker used by local completeness checks."""
    portfolio = getattr(context, "portfolio", None)
    audit_event(
        "run_end",
        context,
        total_value=getattr(portfolio, "total_value", None),
        cash=getattr(portfolio, "cash", None),
    )


# ============================================================
# set_parameter — 策略参数集中设置
# ============================================================
def set_parameter(context):
    """
    将所有策略参数写入 g 全局对象，便于集中管理和回测参数扫描。

    参数类别：资产池、趋势门槛、动量选择、风险平价、RSRS 修正、
    拥挤度惩罚、组合波动率控制、仓位交易约束。
    """

    # ---- 资产池 ----
    g.etf_pool = _copy_runtime_default(PARAM_DEFAULTS["etf_pool"])
    g.etf_names = load_etf_display_names(g.etf_pool)

    # ---- 趋势门槛 ----
    g.MA_long = PARAM_DEFAULTS["MA_long"]
    g.MA_long_by_etf = _copy_runtime_default(PARAM_DEFAULTS["MA_long_by_etf"])

    # ---- 动量选择 ----
    g.MomShort = PARAM_DEFAULTS["MomShort"]
    g.MomMid = PARAM_DEFAULTS["MomMid"]
    g.MomLong = PARAM_DEFAULTS["MomLong"]
    g.w20 = PARAM_DEFAULTS["w20"]
    g.w60 = PARAM_DEFAULTS["w60"]
    g.w120 = PARAM_DEFAULTS["w120"]
    g.TopK = PARAM_DEFAULTS["TopK"]

    # ---- 风险平价 ----
    g.VolWindow = PARAM_DEFAULTS["VolWindow"]
    g.annual_factor = PARAM_DEFAULTS["annual_factor"]

    # ---- RSRS 修正 ----
    g.RSRS_N = PARAM_DEFAULTS["RSRS_N"]        # 回归窗口
    g.RSRS_M = PARAM_DEFAULTS["RSRS_M"]        # 标准化窗口
    g.RSRS_NegativeFullCut = PARAM_DEFAULTS["RSRS_NegativeFullCut"]
    g.RSRSMinMultiplier = PARAM_DEFAULTS["RSRSMinMultiplier"]
    g.RSRSMaxMultiplier = PARAM_DEFAULTS["RSRSMaxMultiplier"]

    # ---- 动量倾斜（资产间相对信号） ----
    g.MomentumTiltStrength = PARAM_DEFAULTS["MomentumTiltStrength"]
    g.MomentumTiltMin = PARAM_DEFAULTS["MomentumTiltMin"]
    g.MomentumTiltMax = PARAM_DEFAULTS["MomentumTiltMax"]
    g.MomentumExtremeScoreStart = PARAM_DEFAULTS["MomentumExtremeScoreStart"]
    g.MomentumExtremeTiltCap = PARAM_DEFAULTS["MomentumExtremeTiltCap"]

    # ---- RSRS 倾斜（资产间相对信号） ----
    g.RSRSTiltMin = PARAM_DEFAULTS["RSRSTiltMin"]
    g.RSRSTiltMax = PARAM_DEFAULTS["RSRSTiltMax"]

    # ---- 拥挤度惩罚 ----
    g.CrowdWindow = PARAM_DEFAULTS["CrowdWindow"]
    g.CrowdRetShort = PARAM_DEFAULTS["CrowdRetShort"]
    g.CrowdRetMid = PARAM_DEFAULTS["CrowdRetMid"]
    g.AmountMAWindow = PARAM_DEFAULTS["AmountMAWindow"]
    g.DeviationMAWindow = PARAM_DEFAULTS["DeviationMAWindow"]
    g.CrowdVolWindow = PARAM_DEFAULTS["CrowdVolWindow"]
    g.CrowdStart = PARAM_DEFAULTS["CrowdStart"]
    g.CrowdEnd = PARAM_DEFAULTS["CrowdEnd"]
    g.MinCrowdPenalty = PARAM_DEFAULTS["MinCrowdPenalty"]
    g.CrowdStart_by_etf = _copy_runtime_default(PARAM_DEFAULTS["CrowdStart_by_etf"])
    g.CrowdEnd_by_etf = PARAM_DEFAULTS["CrowdEnd_by_etf"]
    g.MinCrowdPenalty_by_etf = PARAM_DEFAULTS["MinCrowdPenalty_by_etf"]
    g.CrowdRetShort_by_etf = PARAM_DEFAULTS["CrowdRetShort_by_etf"]
    g.CrowdRetMid_by_etf = PARAM_DEFAULTS["CrowdRetMid_by_etf"]

    # ---- 组合波动率控制 ----
    g.PortfolioVolWindow = PARAM_DEFAULTS["PortfolioVolWindow"]
    g.TargetVol = PARAM_DEFAULTS["TargetVol"]
    g.MaxPortfolioVolScale = PARAM_DEFAULTS["MaxPortfolioVolScale"]
    g.PortfolioVolReliefMode = PARAM_DEFAULTS["PortfolioVolReliefMode"]
    g.GoldVolReliefFraction = PARAM_DEFAULTS["GoldVolReliefFraction"]
    g.GoldVolReliefMaxRatio = PARAM_DEFAULTS["GoldVolReliefMaxRatio"]
    g.DynamicVolReliefFraction = PARAM_DEFAULTS["DynamicVolReliefFraction"]
    g.DynamicVolReliefMaxRatio = PARAM_DEFAULTS["DynamicVolReliefMaxRatio"]
    g.DynamicVolReliefMomentumWindow = PARAM_DEFAULTS["DynamicVolReliefMomentumWindow"]
    g.DynamicVolReliefCovWindow = PARAM_DEFAULTS["DynamicVolReliefCovWindow"]

    # ---- 仓位与交易约束 ----
    g.MaxWeight = PARAM_DEFAULTS["MaxWeight"]
    g.MinWeight = PARAM_DEFAULTS["MinWeight"]
    g.RebalanceThreshold = PARAM_DEFAULTS["RebalanceThreshold"]
    g.MaxTotalWeight = PARAM_DEFAULTS["MaxTotalWeight"]
    g.ExecutionTimingMode = DEFAULT_EXECUTION_TIMING_MODE

    # ---- 数据与基准 ----
    # 复权模式：fq='pre' 在 FQ A/B 对比测试中证实对场内基金会
    # 导致 get_price 返回空数据（2025-04~2026-04 区间复现）。
    # 故默认关闭复权。参考 FQ comparison: R2/backtest_runs/*/report/fq-comparison.md
    g.use_real_price = PARAM_DEFAULTS["use_real_price"]
    g.fq_mode = PARAM_DEFAULTS["fq_mode"]        # 不复权（场内基金默认）
    ma_long_max = max(g.MA_long_by_etf) if g.MA_long_by_etf else g.MA_long
    g.live_days = max(
        ma_long_max, g.MomLong, g.RSRS_M,
        g.CrowdWindow, g.PortfolioVolWindow
    ) + 50
    g.history_buffer = PARAM_DEFAULTS["history_buffer"]
    g.benchmark = PARAM_DEFAULTS["benchmark"]
    g.audit_token = JQ_AUTO_AUDIT_TOKEN
    g.audit_path = "%s/%s.jsonl" % (JQ_AUTO_AUDIT_DIR, g.audit_token)
    g.audit_seq = 0
    g.pending_rebalances = []
    g.pending_live_like_signal_days = []


# ============================================================
# _log_step — 调仓中间量诊断日志
# ============================================================
def _log_step(name, cn_name, pool, values, fmt=".4f", etf_names=None):
    """
    以 "[中文名] name: 基金名(聚宽代码)=value" 格式逐只打印调仓中间量，便于云端回测诊断。

    不在本地单测中验证日志格式，只保证聚宽云端 log.info 可输出。
    """
    labels = build_etf_display_names(pool, etf_names)
    parts = ["%s=%%s" % label for label in labels]
    template = "[%s] %s: " % (cn_name, name) + ", ".join(parts)
    formatted = tuple(format(v, fmt) for v in values)
    log.info(template, *formatted)


# ============================================================
# compose_raw_weights — 权重合成
# ============================================================
def compose_raw_weights(tilted_weights, trend_gates, crowd_penalties):
    """
    合成各模块输出为 RawWeight。

    动量与 RSRS 已经体现在 TiltedWeight 中，不再作为独立乘数。
    TrendGate 仍保留作为二次保护。不重新归一化。
    """
    n = len(tilted_weights)
    raw = np.zeros(n)
    for i in range(n):
        raw[i] = (
            tilted_weights[i]
            * trend_gates[i]
            * crowd_penalties[i]
        )
    return raw


# ============================================================
# weekly_check — 周频调仓主函数
# ============================================================
def _context_trade_date(context):
    """返回当前任务对应的自然日。"""
    current_dt = getattr(context, "current_dt", None)
    if current_dt is not None and hasattr(current_dt, "date"):
        return current_dt.date()
    return current_dt


def build_rebalance_plan(context):
    """生成一次调仓所需的完整信号快照，但不直接下单。

    流程：TrendGate → RPWeight → MomentumScore → MomentumTilt
         → RSRSTilt → TiltedWeight → CrowdPenalty
         → PortfolioVolScale → FinalWeight
    """
    params = snapshot_params()
    pool = params["etf_pool"]
    etf_names = params["etf_names"]

    # 1. 拉取历史数据
    prices = get_history_data(context, pool, params)

    # 2. 计算趋势门槛
    trend_gates = compute_trend_gates(prices, pool, params)
    _log_step("TrendGate", "趋势门槛", pool, trend_gates, fmt=".0f", etf_names=etf_names)

    # 3. 风险平价基础权重（所有趋势成立资产参与）
    active_mask = [gate > 0 for gate in trend_gates]
    rp_weights = compute_rp_weights(prices, pool, active_mask, params)
    _log_step("RPWeight", "风险平价权重", pool, rp_weights, fmt=".4f", etf_names=etf_names)

    # 4. 计算动量分数
    momentum_scores = compute_momentum_scores(prices, pool, trend_gates, params)
    _log_step("MomentumScore", "动量分数", pool, momentum_scores, fmt=".4f", etf_names=etf_names)

    # 5. 动量倾斜乘数
    momentum_tilts = compute_momentum_tilt_multipliers(momentum_scores, trend_gates, params)
    _log_step("MomentumTilt", "动量倾斜乘数", pool, momentum_tilts, fmt=".4f", etf_names=etf_names)

    # 6. RSRS 倾斜乘数
    rsrs_tilts = compute_rsrs_tilt_multipliers(prices, pool, trend_gates, params)
    _log_step("RSRSTilt", "RSRS倾斜乘数", pool, rsrs_tilts, fmt=".4f", etf_names=etf_names)

    # 7. 合成倾斜权重（动量 + RSRS 同时参与相对倾斜，活跃资产内重新归一化）
    tilted_weights = apply_relative_tilts(rp_weights, trend_gates, momentum_tilts, rsrs_tilts)
    _log_step("TiltedWeight", "倾斜合成权重", pool, tilted_weights, fmt=".4f", etf_names=etf_names)

    # 8. 拥挤度线性惩罚乘数
    crowd_penalties = compute_crowd_penalties(prices, pool, params)
    _log_step("CrowdPenalty", "拥挤度惩罚", pool, crowd_penalties, fmt=".4f", etf_names=etf_names)

    # 9. 合成 RawWeight（不重新归一化）
    raw_weights = compose_raw_weights(tilted_weights, trend_gates, crowd_penalties)

    # 10. 组合波动率缩放
    portfolio_vol_asset_scales, portfolio_vol_meta = compute_portfolio_vol_asset_scales(
        prices, pool, raw_weights, params
    )
    portfolio_vol_scale = portfolio_vol_meta["base_scale"]
    log.info("[组合波动率缩放] PortfolioVolScale=%.4f", portfolio_vol_scale)

    # 11. 最终权重
    final_weights = raw_weights * portfolio_vol_asset_scales
    _log_step("FinalWeight", "最终权重", pool, final_weights, fmt=".4f", etf_names=etf_names)

    # 12. 应用交易约束
    final_weights_before_constraints = np.copy(final_weights)
    final_weights = apply_weight_constraints(final_weights, params)
    audit_event(
        "rebalance_signals",
        context,
        pool=pool,
        etf_names=etf_names,
        params=params,
        trend_gates=trend_gates,
        rp_weights=rp_weights,
        momentum_scores=momentum_scores,
        momentum_tilts=momentum_tilts,
        rsrs_tilts=rsrs_tilts,
        tilted_weights=tilted_weights,
        crowd_penalties=crowd_penalties,
        raw_weights=raw_weights,
        portfolio_vol_scale=portfolio_vol_scale,
        portfolio_vol_relief_mode=portfolio_vol_meta["mode"],
        portfolio_vol_ratio=portfolio_vol_meta["vol_ratio"],
        portfolio_vol_base_scale=portfolio_vol_meta["base_scale"],
        portfolio_vol_asset_scales=portfolio_vol_asset_scales,
        portfolio_vol_relief_asset=portfolio_vol_meta["relief_asset"],
        portfolio_vol_relief_weight=portfolio_vol_meta["relief_weight"],
        portfolio_vol_relief_reason=portfolio_vol_meta["reason"],
        final_weights_before_constraints=final_weights_before_constraints,
        final_weights=final_weights,
        execution_timing_mode=params["ExecutionTimingMode"],
        asof_date=getattr(context, "previous_date", None),
        trade_date=_context_trade_date(context),
    )

    return {
        "pool": pool,
        "final_weights": final_weights,
        "params": params,
        "asof_date": getattr(context, "previous_date", None),
        "prepared_date": _context_trade_date(context),
    }


def weekly_check(context):
    """每周开盘时生成信号并立即执行调仓。"""
    plan = build_rebalance_plan(context)
    execute_rebalance(context, plan["pool"], plan["final_weights"], plan["params"])


def prepare_delay_only_rebalance(context):
    """按 baseline 口径先生成信号，延后到下一交易日开盘执行。"""
    plan = build_rebalance_plan(context)
    g.pending_rebalances.append(plan)
    audit_event(
        "rebalance_prepared",
        context,
        execution_timing_mode=plan["params"]["ExecutionTimingMode"],
        asof_date=plan["asof_date"],
        prepared_date=plan["prepared_date"],
        final_weights=plan["final_weights"],
    )


def execute_pending_rebalance(context):
    """执行 logic-2 中已缓存、且至少延后一交易日的调仓。"""
    queue = getattr(g, "pending_rebalances", [])
    if not queue:
        return

    pending = queue[0]
    trade_date = _context_trade_date(context)
    prepared_date = pending.get("prepared_date")
    if trade_date is None or prepared_date is None or trade_date <= prepared_date:
        audit_event(
            "pending_rebalance_wait",
            context,
            execution_timing_mode=pending["params"]["ExecutionTimingMode"],
            asof_date=pending.get("asof_date"),
            prepared_date=prepared_date,
            trade_date=trade_date,
        )
        return

    audit_event(
        "pending_rebalance_execute",
        context,
        execution_timing_mode=pending["params"]["ExecutionTimingMode"],
        asof_date=pending.get("asof_date"),
        prepared_date=prepared_date,
        trade_date=trade_date,
        final_weights=pending["final_weights"],
    )
    execute_rebalance(context, pending["pool"], pending["final_weights"], pending["params"])
    g.pending_rebalances.pop(0)


def mark_live_like_signal_day(context):
    """记录本周首个交易日，供下一交易日开盘生成并执行 logic-3 信号。"""
    signal_date = _context_trade_date(context)
    g.pending_live_like_signal_days.append(signal_date)
    audit_event(
        "live_like_signal_marked",
        context,
        execution_timing_mode=snapshot_params()["ExecutionTimingMode"],
        signal_date=signal_date,
    )


def execute_live_like_rebalance(context):
    """在首个交易日之后的下一交易日开盘，生成并执行 logic-3 信号。"""
    queue = getattr(g, "pending_live_like_signal_days", [])
    if not queue:
        return

    signal_date = queue[0]
    trade_date = _context_trade_date(context)
    if trade_date is None or signal_date is None or trade_date <= signal_date:
        audit_event(
            "live_like_wait",
            context,
            execution_timing_mode=snapshot_params()["ExecutionTimingMode"],
            signal_date=signal_date,
            trade_date=trade_date,
        )
        return

    asof_date = getattr(context, "previous_date", None)
    if asof_date != signal_date:
        audit_event(
            "live_like_skip",
            context,
            execution_timing_mode=snapshot_params()["ExecutionTimingMode"],
            signal_date=signal_date,
            asof_date=asof_date,
            trade_date=trade_date,
            reason="previous_date_not_signal_date",
        )
        queue.pop(0)
        return

    audit_event(
        "live_like_rebalance_execute",
        context,
        execution_timing_mode=snapshot_params()["ExecutionTimingMode"],
        signal_date=signal_date,
        asof_date=asof_date,
        trade_date=trade_date,
    )
    weekly_check(context)
    queue.pop(0)


# ============================================================
# normalize_field_frame — 数据返回结构归一化
# ============================================================
def normalize_field_frame(raw, field, pool):
    """
    将 get_price 返回的单 ETF 结果归一化，辅助测试与本地诊断。

    处理规则：
      - None 或空 DataFrame → 返回 columns=pool 的空 DataFrame
      - 普通 DataFrame → reindex(columns=pool) 补全缺列
    """
    if raw is None:
        return pd.DataFrame(columns=pool)
    if not isinstance(raw, pd.DataFrame):
        return pd.DataFrame(columns=pool)
    if len(raw) == 0:
        return pd.DataFrame(columns=pool)

    raw = raw.reindex(columns=pool)
    return raw.dropna(how='all')


# ============================================================
# compute_history_count — 计算所需历史数据长度
# ============================================================
def compute_history_count(params):
    """
    按模块显式计算所需历史数据长度。

    说明：
      - 动量和收益率类计算需要多取 1 日
      - RSRS 至少需要 RSRS_M + RSRS_N - 1
      - buffer 用于容忍停牌、缺失值、上市初期数据不足
    """
    requirements = [
        max(resolve_ma_long_windows(params)),
        max(params["MomShort"], params["MomMid"], params["MomLong"]) + 1,
        params["VolWindow"] + 1,
        params["RSRS_M"] + params["RSRS_N"] - 1,
        params["CrowdWindow"],
        params["PortfolioVolWindow"] + 1,
    ]
    return max(requirements) + params.get("history_buffer", 50)


# ============================================================
# fetch_field + get_history_data — 拉取历史行情数据
# ============================================================
def fetch_field(pool, field, count, params, end_date=None):
    """
    逐 ETF 拉取单字段数据，返回 DataFrame（index=日期, columns=ETF代码）。

    单标的 + panel=False 在聚宽云端稳定返回 DataFrame（columns=字段名），
    多标的传入 panel=False 可能仍返回 Panel，因此改为逐只拉取后手工组装。

    end_date: 历史数据截止日期。开盘调仓时需传入 context.previous_date
              以避免当日收盘价未来数据。
    """
    series_map = {}
    for etf in pool:
        df = get_price(
            etf,
            count=count,
            end_date=end_date,
            frequency='daily',
            fields=[field],
            skip_paused=True,
            fq=params["fq_mode"],
            panel=False,
        )
        if df is not None and len(df) > 0:
            series_map[etf] = df[field]

    if not series_map:
        return pd.DataFrame(columns=pool)

    result = pd.DataFrame(series_map)
    result = result.reindex(columns=pool)
    return result.dropna(how='all')


def get_history_data(context, pool, params):
    """
    拉取足够长的历史 OHLC + 成交额数据，并预计算日收益率。

    返回 dict：
      close/high/low/amount: DataFrame（index=日期, columns=ETF代码）
      close_ret: DataFrame（index=日期, columns=ETF代码），close 的 pct_change()
    """
    needed = compute_history_count(params)

    prices = {}
    prices['close'] = fetch_field(pool, 'close', needed, params, end_date=context.previous_date)
    prices['high'] = fetch_field(pool, 'high', needed, params, end_date=context.previous_date)
    prices['low'] = fetch_field(pool, 'low', needed, params, end_date=context.previous_date)
    prices['amount'] = fetch_field(pool, 'money', needed, params, end_date=context.previous_date)

    # 预计算日收益率，下游风险平价和组合波动率复用同一份
    prices['close_ret'] = prices['close'].pct_change()

    # 数据新鲜度日志：确保历史数据不晚于 context.previous_date
    close_df = prices['close']
    last_dt = close_df.index[-1] if len(close_df) else None
    log.info("history end_date=%s, context.previous_date=%s", last_dt, context.previous_date)

    return prices


# ============================================================
# compute_trend_gates — 趋势门槛（硬过滤）
# ============================================================
def compute_trend_gates(prices, pool, params):
    """
    使用趋势均线判断方向，支持按 ETF 分别设置窗口。

    返回: list[float]，1.0 表示通过，0.0 表示剔除
    """
    close = prices['close']
    ma_windows = resolve_ma_long_windows(params)

    gates = np.zeros(len(pool))
    for i, etf in enumerate(pool):
        if etf not in close.columns:
            continue
        ma_window = ma_windows[i]
        series = close[etf].dropna()
        if len(series) < ma_window:
            continue
        ma = series.iloc[-ma_window:].mean()
        current_close = series.iloc[-1]
        if current_close > ma:
            gates[i] = 1.0
    return gates


# ============================================================
# compute_momentum_scores — 多周期排名动量分数
# ============================================================
def compute_momentum_scores(prices, pool, trend_gates, params):
    """
    在趋势成立的资产中，计算多周期排名动量分数。

    对 close DataFrame 批量取各周期终点收益，再用 rank(pct=True)
    在截面上生成 0~1 排名分数，最后按权重线性组合。

    返回: np.array，未通过趋势门槛的资产分数为 0
    """
    close = prices['close']
    n = len(pool)
    scores = np.zeros(n)

    windows = [params["MomShort"], params["MomMid"], params["MomLong"]]
    period_weights = [params["w20"], params["w60"], params["w120"]]

    active_indices = [i for i in range(n) if trend_gates[i] > 0]
    if not active_indices:
        return scores

    active_pool = [pool[i] for i in active_indices if pool[i] in close.columns]
    if not active_pool:
        return scores
    active_close = close[active_pool]
    if len(active_close) <= max(windows):
        return scores

    for j, w in enumerate(windows):
        if len(active_close) <= w:
            continue
        latest = active_close.iloc[-1]
        past = active_close.iloc[-(w + 1)]
        period_ret = latest / past - 1  # pd.Series, index=ETF代码

        # rank(pct=True) 将收益率映射到 [0, 1]，收益率越高排名越接近 1
        ranks = period_ret.rank(pct=True).fillna(0.0)

        for idx_pos, active_i in enumerate(active_indices):
            etf_code = pool[active_i]
            scores[active_i] += period_weights[j] * float(ranks.get(etf_code, 0.0))

    return scores


# ============================================================
# select_topk — TopK 入选
# ============================================================
def select_topk(momentum_scores, trend_gates, params):
    """
    在趋势成立资产中按动量分数从高到低选择前 TopK 只。

    返回: list[bool]，入选为 True
    """
    n = len(momentum_scores)
    selected = [False] * n

    active = [(i, momentum_scores[i]) for i in range(n) if trend_gates[i] > 0]
    active.sort(key=lambda x: x[1], reverse=True)

    k = min(params["TopK"], len(active))
    for idx, _ in active[:k]:
        selected[idx] = True

    return selected


# ============================================================
# compute_rp_weights — 逆波动率风险平价
# ============================================================
def compute_rp_weights(prices, pool, active_mask, params):
    """
    对活跃资产计算逆波动率风险平价权重。

    所有趋势成立资产均参与基础权重计算，不再受 TopK 限制。
    使用 get_history_data 预计算的 close_ret，避免重复 pct_change()。

    返回: np.array
    """
    close_ret = prices['close_ret']
    n = len(pool)
    vol_window = params["VolWindow"]
    annual_factor = params["annual_factor"]

    weights = np.zeros(n)
    active_indices = [i for i in range(n) if active_mask[i]]

    if not active_indices:
        return weights

    vols = np.zeros(n)
    for i in active_indices:
        etf = pool[i]
        if etf not in close_ret.columns:
            continue
        daily_ret = close_ret[etf].dropna().iloc[-vol_window:]
        if len(daily_ret) < 5:
            vols[i] = 1.0
        else:
            vols[i] = daily_ret.std() * np.sqrt(annual_factor)
            if vols[i] < 1e-8:
                vols[i] = 1e-8

    inverse_vols = np.zeros(n)
    for i in active_indices:
        if vols[i] > 0:
            inverse_vols[i] = 1.0 / vols[i]

    total_inv_vol = inverse_vols.sum()
    if total_inv_vol > 0:
        weights = inverse_vols / total_inv_vol

    return weights


# ============================================================
# compute_rsrs_multipliers — RSRS 线性修正乘数
# ============================================================
def compute_rsrs_multipliers(prices, pool, params):
    """
    对每只 ETF 计算 RSRS 截断线性乘数（旧接口，保留兼容）。

    委托 compute_rsrs_adjusted_scores 获取原始信号，
    再按旧公式 clip(1 + RSRS_Adj / NegativeFullCut, Min, Max)。

    只减仓，不加仓。
    """
    rsrs_adj = compute_rsrs_adjusted_scores(prices, pool, params)
    full_cut = params["RSRS_NegativeFullCut"]
    n = len(pool)

    multipliers = np.ones(n)
    for i in range(n):
        raw_mult = 1.0 + rsrs_adj[i] / full_cut
        multipliers[i] = np.clip(raw_mult, params["RSRSMinMultiplier"], params["RSRSMaxMultiplier"])

    return multipliers


# ============================================================
# compute_rsrs_adjusted_scores — RSRS 原始结构信号
# ============================================================
def compute_rsrs_adjusted_scores(prices, pool, params):
    """
    计算每只 ETF 的 RSRS 原始调整信号：RSRSAdj_i = RSRS_Z_i × R2_i。

    返回: np.array，数据不足时对应位置为 0。
    """
    high = prices['high']
    low = prices['low']
    n = len(pool)

    N = params["RSRS_N"]
    M = params["RSRS_M"]

    scores = np.zeros(n)

    for i, etf in enumerate(pool):
        if etf not in high.columns or etf not in low.columns:
            continue
        h = high[etf].dropna()
        l = low[etf].dropna()

        common_idx = h.index.intersection(l.index)
        h = h.loc[common_idx]
        l = l.loc[common_idx]

        min_len = M + N - 1
        if len(h) < min_len:
            continue

        h_roll = h.rolling(N)
        l_roll = l.rolling(N)
        cov_vals = h_roll.cov(l).dropna().values
        var_l_vals = l_roll.var().dropna().values
        var_h_vals = h_roll.var().dropna().values

        betas = cov_vals / var_l_vals
        r2s = cov_vals ** 2 / (var_h_vals * var_l_vals)

        bad = (
            (var_l_vals < 1e-10) | (var_h_vals < 1e-10)
            | (~np.isfinite(betas)) | (~np.isfinite(r2s))
        )
        betas[bad] = 1.0
        r2s[bad] = 0.0

        if len(betas) < M:
            continue

        beta_series = betas[-M:]
        mean_beta = np.mean(beta_series)
        std_beta = np.std(beta_series)

        if std_beta < 1e-10:
            rsrs_z = 0.0
        else:
            rsrs_z = (beta_series[-1] - mean_beta) / std_beta

        latest_r2 = r2s[-1]
        scores[i] = rsrs_z * latest_r2

    return scores


# ============================================================
# compute_momentum_tilt_multipliers — 动量相对倾斜乘数
# ============================================================
def compute_momentum_tilt_multipliers(momentum_scores, trend_gates, params):
    """
    将动量分数转换为资产间相对倾斜乘数。

    活跃资产围绕均值上下倾斜，非活跃资产返回 0。
    倾斜公式：clip(1 + strength × (score_i - mean_active), min, max)
    若启用极端高动量弱化，则 score_i 达到阈值后再将高位倾斜压到 cap。
    """
    n = len(momentum_scores)
    tilts = np.zeros(n)

    active_indices = [i for i in range(n) if trend_gates[i] > 0]
    if not active_indices:
        return tilts

    active_scores = momentum_scores[active_indices]
    mean_score = np.mean(active_scores)

    strength = params["MomentumTiltStrength"]
    tilt_min = params["MomentumTiltMin"]
    tilt_max = params["MomentumTiltMax"]
    extreme_start = params["MomentumExtremeScoreStart"]
    extreme_cap = params["MomentumExtremeTiltCap"]

    for i in active_indices:
        edge = momentum_scores[i] - mean_score
        raw_tilt = 1.0 + strength * edge
        tilt = np.clip(raw_tilt, tilt_min, tilt_max)
        if extreme_start is not None and momentum_scores[i] >= extreme_start:
            tilt = min(tilt, extreme_cap)
        tilts[i] = tilt

    return tilts


# ============================================================
# compute_rsrs_tilt_multipliers — RSRS 相对倾斜乘数
# ============================================================
def compute_rsrs_tilt_multipliers(prices, pool, trend_gates, params):
    """
    计算 RSRS 原始结构信号，并转换为资产间相对倾斜乘数。

    活跃资产围绕均值上下倾斜，非活跃资产返回 0。
    倾斜公式：clip(1 + (RSRSAdj_i - mean_active) / NegativeFullCut, min, max)
    """
    rsrs_adj = compute_rsrs_adjusted_scores(prices, pool, params)
    n = len(pool)
    tilts = np.zeros(n)

    active_indices = [i for i in range(n) if trend_gates[i] > 0]
    if not active_indices:
        return tilts

    active_adj = rsrs_adj[active_indices]
    mean_adj = np.mean(active_adj)

    full_cut = params["RSRS_NegativeFullCut"]
    tilt_min = params["RSRSTiltMin"]
    tilt_max = params["RSRSTiltMax"]

    for i in active_indices:
        edge = rsrs_adj[i] - mean_adj
        raw_tilt = 1.0 + edge / full_cut
        tilts[i] = np.clip(raw_tilt, tilt_min, tilt_max)

    return tilts


# ============================================================
# apply_relative_tilts — 倾斜权重合成与归一化
# ============================================================
def apply_relative_tilts(rp_weights, trend_gates, momentum_tilts, rsrs_tilts):
    """
    将风险平价基础权重、动量倾斜和 RSRS 倾斜合成，并在活跃资产内归一化。

    tilted_raw_i = RPWeight_i × MomentumTilt_i × RSRSTilt_i
    归一化到 sum(RPWeight_active)，非活跃资产权重为 0。
    """
    n = len(rp_weights)
    tilted = np.zeros(n)

    active_indices = [i for i in range(n) if trend_gates[i] > 0 and rp_weights[i] > 0]
    if not active_indices:
        return tilted

    tilted_raw = np.zeros(n)
    for i in active_indices:
        tilted_raw[i] = rp_weights[i] * momentum_tilts[i] * rsrs_tilts[i]

    total_raw = tilted_raw[active_indices].sum()
    base_total = rp_weights[active_indices].sum()

    if total_raw <= 0 or base_total <= 0:
        return np.copy(rp_weights)

    for i in active_indices:
        tilted[i] = tilted_raw[i] / total_raw * base_total

    return tilted


# ============================================================
# compute_crowd_penalties — 拥挤度线性惩罚乘数
# ============================================================
def compute_crowd_penalties(prices, pool, params):
    """
    对每只 ETF 计算拥挤度线性惩罚乘数。

    先在 DataFrame 级批量计算五类指标（ret20/ret60/amt_ma20/deviation/vol20），
    再逐 ETF 取最后一行做分位排名，减少 Python 层逐列 rolling/pct_change 开销。

    只减仓，不加仓。

    返回: np.array
    """
    close = prices['close']
    amount = prices['amount']
    n = len(pool)

    crowd_window = params["CrowdWindow"]
    thresholds = resolve_crowd_thresholds(params)    # [(start, end, min_penalty), ...]
    ret_windows = resolve_crowd_ret_windows(params)  # [(short, mid), ...]

    penalties = np.ones(n)

    # 过滤数据不足的 ETF
    eligible_etfs = []
    for i, etf in enumerate(pool):
        if etf in close.columns and len(close[etf].dropna()) >= crowd_window:
            eligible_etfs.append(etf)
        else:
            penalties[i] = 1.0

    if not eligible_etfs:
        return penalties

    # ---- DataFrame 级批量计算：一次算完所有 ETF 的指标 ----
    close_recent = close[eligible_etfs].iloc[-crowd_window:]

    # 1-2) 短/中期涨幅（按 per-ETF 窗口预计算，避免重复 shift）
    ret_short_map = {}
    ret_mid_map = {}
    for i, etf in enumerate(pool):
        if etf not in eligible_etfs:
            continue
        short_w, mid_w = ret_windows[i]
        if short_w not in ret_short_map:
            ret_short_map[short_w] = close_recent / close_recent.shift(short_w) - 1
        if mid_w not in ret_mid_map:
            ret_mid_map[mid_w] = close_recent / close_recent.shift(mid_w) - 1

    # 3) 成交额 MA20（仅对有 amount 数据的 ETF）
    eligible_amount_cols = [e for e in eligible_etfs if e in amount.columns]
    amt_ma20_df = None
    if eligible_amount_cols:
        amt_aligned = amount[eligible_amount_cols].loc[
            amount.index.intersection(close_recent.index)
        ]
        if len(amt_aligned) >= params["AmountMAWindow"]:
            amt_ma20_df = amt_aligned.rolling(params["AmountMAWindow"]).mean()

    # 4) 偏离均线程度
    ma20_df = close_recent.rolling(params["DeviationMAWindow"]).mean()
    deviation_df = close_recent / ma20_df - 1

    # 5) 短期波动率
    vol20_df = close_recent.pct_change().rolling(params["CrowdVolWindow"]).std() * np.sqrt(params["annual_factor"])

    # ---- 逐 ETF 从预计算 DataFrame 中取列做分位排名 ----
    for i, etf in enumerate(pool):
        if etf not in eligible_etfs:
            continue

        indicators = []

        # ret short (per-ETF window)
        short_w = ret_windows[i][0]
        col_short = ret_short_map[short_w][etf].dropna()
        indicators.append(percentile_rank(col_short.iloc[-1], col_short) if len(col_short) > 1 else 0.5)

        # ret mid (per-ETF window)
        mid_w = ret_windows[i][1]
        col_mid = ret_mid_map[mid_w][etf].dropna()
        indicators.append(percentile_rank(col_mid.iloc[-1], col_mid) if len(col_mid) > 1 else 0.5)

        # amt_ma20
        if amt_ma20_df is not None and etf in amt_ma20_df.columns:
            col_amt = amt_ma20_df[etf].dropna()
            indicators.append(percentile_rank(col_amt.iloc[-1], col_amt) if len(col_amt) > 1 else 0.5)
        else:
            indicators.append(0.5)

        # deviation
        col_dev = deviation_df[etf].dropna()
        indicators.append(percentile_rank(col_dev.iloc[-1], col_dev) if len(col_dev) > 1 else 0.5)

        # vol20
        col_vol = vol20_df[etf].dropna()
        indicators.append(percentile_rank(col_vol.iloc[-1], col_vol) if len(col_vol) > 1 else 0.5)

        crowd_score = np.mean(indicators)

        etf_start, etf_end, etf_min = thresholds[i]
        if crowd_score <= etf_start:
            penalty = 1.0
        elif crowd_score >= etf_end:
            penalty = etf_min
        else:
            penalty = 1.0 - (crowd_score - etf_start) / (etf_end - etf_start) * (1.0 - etf_min)
            penalty = max(etf_min, min(1.0, penalty))

        penalties[i] = penalty

    return penalties


# ============================================================
# percentile_rank — 计算分位数排名（0~1）
# ============================================================
def percentile_rank(value, series):
    """
    计算 value 在 series 中的分位数（0~1）。

    返回 0 表示 value 是序列中最小值，返回 1 表示最大值。
    """
    if len(series) == 0:
        return 0.5
    ranked = (series < value).mean()
    return float(ranked)


# ============================================================
# compute_portfolio_vol_scale — 组合波动率缩放系数
# ============================================================
def compute_portfolio_vol_scale_detail(prices, pool, raw_weights, params):
    """
    根据 RawWeight 和协方差矩阵计算组合波动率，按目标波动率缩放。

    使用 get_history_data 预计算的 close_ret，避免重复 pct_change()。
    只缩不放（最大系数为 1.0）。

    返回: (scale, vol_ratio)
    """
    close_ret = prices['close_ret']
    vol_window = params["PortfolioVolWindow"]
    target_vol = params["TargetVol"]
    annual_factor = params["annual_factor"]

    n = len(pool)
    active_indices = [i for i in range(n) if raw_weights[i] > 1e-8]

    if not active_indices:
        return 1.0, None

    returns_list = []
    for i in active_indices:
        etf = pool[i]
        if etf not in close_ret.columns:
            return 1.0, None
        ret = close_ret[etf].dropna().iloc[-vol_window:]
        if len(ret) < vol_window:
            return 1.0, None
        returns_list.append(ret.values)

    if not returns_list:
        return 1.0, None

    ret_matrix = np.column_stack(returns_list)
    cov_daily = np.atleast_2d(np.cov(ret_matrix, rowvar=False))
    cov_annual = cov_daily * annual_factor

    active_weights = np.array([raw_weights[i] for i in active_indices])
    portfolio_var = active_weights @ cov_annual @ active_weights
    portfolio_vol = np.sqrt(max(portfolio_var, 0))
    vol_ratio = float(portfolio_vol / target_vol)

    if portfolio_vol <= target_vol or portfolio_vol < 1e-8:
        return 1.0, vol_ratio

    scale = target_vol / portfolio_vol
    return min(scale, params["MaxPortfolioVolScale"]), vol_ratio


def compute_portfolio_vol_scale(prices, pool, raw_weights, params):
    """返回原有组合级波动率缩放标量。"""
    scale, _vol_ratio = compute_portfolio_vol_scale_detail(prices, pool, raw_weights, params)
    return scale


def compute_portfolio_vol_asset_scales(prices, pool, raw_weights, params):
    """返回每只 ETF 的组合波控缩放系数和审计元数据。"""
    base_scale, vol_ratio = compute_portfolio_vol_scale_detail(prices, pool, raw_weights, params)
    asset_scales = np.full(len(pool), base_scale)
    mode = params["PortfolioVolReliefMode"]
    meta = {
        "mode": mode,
        "vol_ratio": vol_ratio,
        "base_scale": base_scale,
        "relief_asset": None,
        "relief_weight": 0.0,
        "reason": "baseline",
    }
    if mode == "fixed_gold":
        return apply_fixed_gold_vol_relief(pool, raw_weights, asset_scales, meta, params)
    if mode == "dyn_marginal":
        return apply_dynamic_marginal_vol_relief(prices, pool, raw_weights, asset_scales, meta, params)
    return asset_scales, meta


def apply_fixed_gold_vol_relief(pool, raw_weights, asset_scales, meta, params):
    """固定黄金弱缩放：按参数恢复黄金被组合波控压掉的部分仓位。"""
    gold_code = "518880.XSHG"
    vol_ratio = meta["vol_ratio"]
    base_scale = meta["base_scale"]

    if vol_ratio is None or vol_ratio <= 1.0:
        meta["reason"] = "vol_not_above_target"
        return asset_scales, meta
    if vol_ratio > params["GoldVolReliefMaxRatio"]:
        meta["reason"] = "ratio_too_high"
        return asset_scales, meta
    if gold_code not in pool:
        meta["reason"] = "gold_not_in_pool"
        return asset_scales, meta

    gold_idx = pool.index(gold_code)
    if raw_weights[gold_idx] <= 1e-8:
        meta["reason"] = "gold_not_active"
        return asset_scales, meta

    new_scale = min(1.0, base_scale + (1.0 - base_scale) * params["GoldVolReliefFraction"])
    asset_scales[gold_idx] = new_scale
    meta["relief_asset"] = gold_code
    meta["relief_weight"] = float(raw_weights[gold_idx] * (new_scale - base_scale))
    meta["reason"] = "fixed_gold"
    return asset_scales, meta


def apply_dynamic_marginal_vol_relief(prices, pool, raw_weights, asset_scales, meta, params):
    """动态弱缩放：在正动量持仓中选择边际风险最低资产恢复仓位。"""
    vol_ratio = meta["vol_ratio"]
    base_scale = meta["base_scale"]

    if vol_ratio is None or vol_ratio <= 1.0:
        meta["reason"] = "vol_not_above_target"
        return asset_scales, meta
    if vol_ratio > params["DynamicVolReliefMaxRatio"]:
        meta["reason"] = "ratio_too_high"
        return asset_scales, meta

    selected_idx, reason = select_dynamic_marginal_relief_asset(
        prices, pool, raw_weights, params
    )
    if selected_idx is None:
        meta["reason"] = reason
        return asset_scales, meta

    new_scale = min(1.0, base_scale + (1.0 - base_scale) * params["DynamicVolReliefFraction"])
    asset_scales[selected_idx] = new_scale
    meta["relief_asset"] = pool[selected_idx]
    meta["relief_weight"] = float(raw_weights[selected_idx] * (new_scale - base_scale))
    meta["reason"] = "selected_low_marginal_risk"
    return asset_scales, meta


def select_dynamic_marginal_relief_asset(prices, pool, raw_weights, params):
    """返回动态弱缩放资产下标和原因。"""
    active_indices = [i for i, weight in enumerate(raw_weights) if weight > 1e-8]
    if not active_indices:
        return None, "no_active_asset"

    close_ret = prices.get("close_ret")
    if close_ret is None:
        return None, "insufficient_cov_data"

    active_codes = [pool[i] for i in active_indices]
    for code in active_codes:
        if code not in close_ret.columns:
            return None, "insufficient_cov_data"

    cov_window = params["DynamicVolReliefCovWindow"]
    active_returns = close_ret[active_codes].dropna().iloc[-cov_window:]
    if len(active_returns) < cov_window:
        return None, "insufficient_cov_data"

    momentum_window = params["DynamicVolReliefMomentumWindow"]
    positive_candidates = []
    for active_pos, code in enumerate(active_codes):
        momentum_ret = close_ret[code].dropna().iloc[-momentum_window:]
        if len(momentum_ret) < momentum_window:
            continue
        momentum = float(np.prod(1.0 + momentum_ret.values) - 1.0)
        if momentum >= 0.0:
            positive_candidates.append(active_pos)

    if not positive_candidates:
        return None, "no_positive_momentum_asset"

    cov_daily = np.atleast_2d(np.cov(active_returns.values, rowvar=False))
    cov_annual = cov_daily * params["annual_factor"]
    active_weights = np.array([raw_weights[i] for i in active_indices])
    marginal_scores = cov_annual @ active_weights
    best_active_pos = min(
        positive_candidates,
        key=lambda active_pos: marginal_scores[active_pos],
    )
    return active_indices[best_active_pos], "selected_low_marginal_risk"


# ============================================================
# apply_weight_constraints — 应用仓位约束
# ============================================================
def apply_weight_constraints(final_weights, params):
    """
    应用单资产最大仓位、最小有效仓位和总仓位上限约束。

    总仓位约束在单资产约束之后应用，缩放后不重新归一化。
    """
    max_w = params["MaxWeight"]
    min_w = params["MinWeight"]
    max_total = params["MaxTotalWeight"]
    n = len(final_weights)

    result = np.copy(final_weights)

    for i in range(n):
        if result[i] > max_w:
            result[i] = max_w
        if result[i] < min_w:
            result[i] = 0.0

    total = result.sum()
    if total > max_total:
        result = result * max_total / total

    return result


# ============================================================
# execute_rebalance — 执行调仓
# ============================================================
def execute_rebalance(context, pool, final_weights, params):
    """
    根据最终目标权重执行调仓，应用最小调仓阈值。
    剩余仓位保留为现金。

    执行前检查停牌状态，执行后记录订单结果，便于审计和故障定位。
    """
    account_value = context.portfolio.total_value
    current_data = get_current_data()
    etf_names = build_etf_display_names(pool, params.get("etf_names"))

    for i, etf in enumerate(pool):
        etf_name = etf_names[i]
        target_value = account_value * final_weights[i]
        current_pos = context.portfolio.positions[etf]
        current_value = current_pos.total_amount * current_pos.price if current_pos.total_amount > 0 else 0
        current_weight = current_value / account_value if account_value > 0 else 0

        # 如果目标权重为 0 且当前仓位为 0，跳过
        if final_weights[i] == 0 and current_weight == 0:
            audit_event(
                "rebalance_order",
                context,
                action="skip_zero_target_zero_position",
                etf=etf,
                etf_name=etf_name,
                target_weight=final_weights[i],
                current_weight=current_weight,
                target_value=target_value,
            )
            continue

        # 最小调仓阈值
        if abs(final_weights[i] - current_weight) < params["RebalanceThreshold"]:
            audit_event(
                "rebalance_order",
                context,
                action="skip_threshold",
                etf=etf,
                etf_name=etf_name,
                target_weight=final_weights[i],
                current_weight=current_weight,
                target_value=target_value,
                rebalance_threshold=params["RebalanceThreshold"],
            )
            continue

        # 停牌检查
        data = current_data[etf]
        if data.paused:
            log.warning("skip paused ETF: %s security=%s", etf_name, etf)
            audit_event(
                "rebalance_order",
                context,
                action="skip_paused",
                etf=etf,
                etf_name=etf_name,
                target_weight=final_weights[i],
                current_weight=current_weight,
                target_value=target_value,
            )
            continue

        order_obj = order_target_value(etf, target_value)
        if order_obj is None:
            log.error(
                "order failed: %s security=%s target_value=%.2f target_weight=%.4f current_weight=%.4f",
                etf_name, etf, target_value, final_weights[i], current_weight
            )
            audit_event(
                "rebalance_order",
                context,
                action="order_failed",
                etf=etf,
                etf_name=etf_name,
                target_weight=final_weights[i],
                current_weight=current_weight,
                target_value=target_value,
            )
        else:
            log.info(
                "order sent: %s security=%s target_weight=%.4f current_weight=%.4f target_value=%.2f",
                etf_name, etf, final_weights[i], current_weight, target_value
            )
            audit_event(
                "rebalance_order",
                context,
                action="order_sent",
                etf=etf,
                etf_name=etf_name,
                target_weight=final_weights[i],
                current_weight=current_weight,
                target_value=target_value,
                order=order_obj,
            )
