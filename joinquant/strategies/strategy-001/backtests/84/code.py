enable_profile()
STRATEGY_NAME = "ETF多因子轮动"
import FeishuRelayTools
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
  FinalWeight_i = RawWeight_i × PortfolioVolScale
调仓频率：每周开盘检查一次
============================================================
"""
import json
from datetime import date, datetime
import numpy as np
import pandas as pd
FIELD_MAP = {
    "close": "close",
    "high": "high",
    "low": "low",
    "amount": "money",
}
JQ_AUTO_AUDIT_TOKEN = 'etf_factor_rotation-s01-execution-timing-worktree-20260518213041-48a2c65d'
JQ_AUTO_AUDIT_DIR = "jq_auto_audit"
EXECUTION_TIMING_MODES = (
    "baseline",
    "logic-2-delay-only",
    "logic-3-live-like",
)
def fund_code(security):
    return str(security).split(".")[0]
def format_etf_name(security, name):
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
    names = names or []
    result = []
    for i, etf in enumerate(pool):
        base_name = names[i] if i < len(names) else etf
        result.append(format_etf_name(etf, base_name))
    return result
def fetch_etf_official_name(security, fallback_name=None):
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
    fallback_names = fallback_names or []
    names = []
    for i, etf in enumerate(pool):
        fallback_name = fallback_names[i] if i < len(fallback_names) else None
        official_name = fetch_etf_official_name(etf, fallback_name=fallback_name)
        names.append(format_etf_name(etf, official_name))
    return names
def _audit_jsonable(value):
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
def snapshot_params():
    etf_pool = list(g.etf_pool)
    etf_names = build_etf_display_names(etf_pool, list(g.etf_names))
    return {
        "etf_pool": etf_pool,
        "etf_names": etf_names,
        "benchmark": g.benchmark,
        "MA_long": g.MA_long,
        "MA_long_by_etf": None if g.MA_long_by_etf is None else list(g.MA_long_by_etf),
        "MomShort": g.MomShort,
        "MomMid": g.MomMid,
        "MomLong": g.MomLong,
        "w20": g.w20,
        "w60": g.w60,
        "w120": g.w120,
        "TopK": g.TopK,
        "VolWindow": g.VolWindow,
        "annual_factor": g.annual_factor,
        "RSRS_N": g.RSRS_N,
        "RSRS_M": g.RSRS_M,
        "RSRS_NegativeFullCut": g.RSRS_NegativeFullCut,
        "RSRSMinMultiplier": g.RSRSMinMultiplier,
        "RSRSMaxMultiplier": g.RSRSMaxMultiplier,
        "MomentumTiltStrength": g.MomentumTiltStrength,
        "MomentumTiltMin": g.MomentumTiltMin,
        "MomentumTiltMax": g.MomentumTiltMax,
        "MomentumExtremeScoreStart": g.MomentumExtremeScoreStart,
        "MomentumExtremeTiltCap": g.MomentumExtremeTiltCap,
        "RSRSTiltMin": g.RSRSTiltMin,
        "RSRSTiltMax": g.RSRSTiltMax,
        "CrowdWindow": g.CrowdWindow,
        "CrowdRetShort": g.CrowdRetShort,
        "CrowdRetMid": g.CrowdRetMid,
        "AmountMAWindow": g.AmountMAWindow,
        "DeviationMAWindow": g.DeviationMAWindow,
        "CrowdVolWindow": g.CrowdVolWindow,
        "CrowdStart": g.CrowdStart,
        "CrowdEnd": g.CrowdEnd,
        "MinCrowdPenalty": g.MinCrowdPenalty,
        "CrowdStart_by_etf": None if g.CrowdStart_by_etf is None else list(g.CrowdStart_by_etf),
        "CrowdEnd_by_etf": None if g.CrowdEnd_by_etf is None else list(g.CrowdEnd_by_etf),
        "MinCrowdPenalty_by_etf": None if g.MinCrowdPenalty_by_etf is None else list(g.MinCrowdPenalty_by_etf),
        "CrowdRetShort_by_etf": None if g.CrowdRetShort_by_etf is None else list(g.CrowdRetShort_by_etf),
        "CrowdRetMid_by_etf": None if g.CrowdRetMid_by_etf is None else list(g.CrowdRetMid_by_etf),
        "PortfolioVolWindow": g.PortfolioVolWindow,
        "TargetVol": g.TargetVol,
        "MaxPortfolioVolScale": g.MaxPortfolioVolScale,
        "MaxWeight": g.MaxWeight,
        "MinWeight": g.MinWeight,
        "RebalanceThreshold": g.RebalanceThreshold,
        "MaxTotalWeight": g.MaxTotalWeight,
        "ExecutionTimingMode": g.ExecutionTimingMode,
        "use_real_price": g.use_real_price,
        "fq_mode": g.fq_mode,
        "history_buffer": g.history_buffer,
        "audit_token": getattr(g, "audit_token", ""),
        "audit_path": getattr(g, "audit_path", ""),
    }
def validate_params(params):
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
    ma_long_by_etf = params.get("MA_long_by_etf")
    if ma_long_by_etf is None:
        return [params["MA_long"]] * len(params["etf_pool"])
    return list(ma_long_by_etf)
def resolve_crowd_thresholds(params):
    n = len(params["etf_pool"])
    starts = params.get("CrowdStart_by_etf")
    starts = list(starts) if starts is not None else [params["CrowdStart"]] * n
    ends = params.get("CrowdEnd_by_etf")
    ends = list(ends) if ends is not None else [params["CrowdEnd"]] * n
    mins = params.get("MinCrowdPenalty_by_etf")
    mins = list(mins) if mins is not None else [params["MinCrowdPenalty"]] * n
    return list(zip(starts, ends, mins))
def resolve_crowd_ret_windows(params):
    n = len(params["etf_pool"])
    shorts = params.get("CrowdRetShort_by_etf")
    shorts = list(shorts) if shorts is not None else [params["CrowdRetShort"]] * n
    mids = params.get("CrowdRetMid_by_etf")
    mids = list(mids) if mids is not None else [params["CrowdRetMid"]] * n
    return list(zip(shorts, mids))
def initialize(context):
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
    portfolio = getattr(context, "portfolio", None)
    audit_event(
        "run_end",
        context,
        total_value=getattr(portfolio, "total_value", None),
        cash=getattr(portfolio, "cash", None),
    )
def set_parameter(context):
    g.etf_pool = [
        '159819.XSHE',
        '513100.XSHG',
        '518880.XSHG',
    ]
    g.etf_names = load_etf_display_names(g.etf_pool)
    g.MA_long = 120
    g.MA_long_by_etf = [20, 40, 100]
    g.MomShort = 20
    g.MomMid = 60
    g.MomLong = 120
    g.w20 = 0.2
    g.w60 = 0.3
    g.w120 = 0.5
    g.TopK = 3
    g.VolWindow = 60
    g.annual_factor = 252
    g.RSRS_N = 18
    g.RSRS_M = 600
    g.RSRS_NegativeFullCut = 1.8
    g.RSRSMinMultiplier = 0.0
    g.RSRSMaxMultiplier = 1.3
    g.MomentumTiltStrength = 0.50
    g.MomentumTiltMin = 0.70
    g.MomentumTiltMax = 1.30
    g.MomentumExtremeScoreStart = None
    g.MomentumExtremeTiltCap = 1.00
    g.RSRSTiltMin = 0.70
    g.RSRSTiltMax = 1.30
    g.CrowdWindow = 500
    g.CrowdRetShort = 20
    g.CrowdRetMid = 60
    g.AmountMAWindow = 20
    g.DeviationMAWindow = 20
    g.CrowdVolWindow = 20
    g.CrowdStart = 0.60
    g.CrowdEnd = 0.95
    g.MinCrowdPenalty = 0.30
    g.CrowdStart_by_etf = [0.60, 0.60, 0.80]
    g.CrowdEnd_by_etf = None
    g.MinCrowdPenalty_by_etf = None
    g.CrowdRetShort_by_etf = None
    g.CrowdRetMid_by_etf = None
    g.PortfolioVolWindow = 40
    g.TargetVol = 0.08
    g.MaxPortfolioVolScale = 1.0
    g.MaxWeight = 0.60
    g.MinWeight = 0.05
    g.RebalanceThreshold = 0.03
    g.MaxTotalWeight = 1.0
    g.ExecutionTimingMode = 'logic-3-live-like'
    g.use_real_price = False
    g.fq_mode = None
    ma_long_max = max(g.MA_long_by_etf) if g.MA_long_by_etf else g.MA_long
    g.live_days = max(
        ma_long_max, g.MomLong, g.RSRS_M,
        g.CrowdWindow, g.PortfolioVolWindow
    ) + 50
    g.history_buffer = 100
    g.benchmark = '000300.XSHG'
    g.audit_token = JQ_AUTO_AUDIT_TOKEN
    g.audit_path = "%s/%s.jsonl" % (JQ_AUTO_AUDIT_DIR, g.audit_token)
    g.audit_seq = 0
    g.pending_rebalances = []
    g.pending_live_like_signal_days = []
def _log_step(name, cn_name, pool, values, fmt=".4f", etf_names=None):
    labels = build_etf_display_names(pool, etf_names)
    parts = ["%s=%%s" % label for label in labels]
    template = "[%s] %s: " % (cn_name, name) + ", ".join(parts)
    formatted = tuple(format(v, fmt) for v in values)
    log.info(template, *formatted)
def compose_raw_weights(tilted_weights, trend_gates, crowd_penalties):
    n = len(tilted_weights)
    raw = np.zeros(n)
    for i in range(n):
        raw[i] = (
            tilted_weights[i]
            * trend_gates[i]
            * crowd_penalties[i]
        )
    return raw
def _context_trade_date(context):
    current_dt = getattr(context, "current_dt", None)
    if current_dt is not None and hasattr(current_dt, "date"):
        return current_dt.date()
    return current_dt
def build_rebalance_plan(context):
    params = snapshot_params()
    pool = params["etf_pool"]
    etf_names = params["etf_names"]
    prices = get_history_data(context, pool, params)
    trend_gates = compute_trend_gates(prices, pool, params)
    _log_step("TrendGate", "趋势门槛", pool, trend_gates, fmt=".0f", etf_names=etf_names)
    active_mask = [gate > 0 for gate in trend_gates]
    rp_weights = compute_rp_weights(prices, pool, active_mask, params)
    _log_step("RPWeight", "风险平价权重", pool, rp_weights, fmt=".4f", etf_names=etf_names)
    momentum_scores = compute_momentum_scores(prices, pool, trend_gates, params)
    _log_step("MomentumScore", "动量分数", pool, momentum_scores, fmt=".4f", etf_names=etf_names)
    momentum_tilts = compute_momentum_tilt_multipliers(momentum_scores, trend_gates, params)
    _log_step("MomentumTilt", "动量倾斜乘数", pool, momentum_tilts, fmt=".4f", etf_names=etf_names)
    rsrs_tilts = compute_rsrs_tilt_multipliers(prices, pool, trend_gates, params)
    _log_step("RSRSTilt", "RSRS倾斜乘数", pool, rsrs_tilts, fmt=".4f", etf_names=etf_names)
    tilted_weights = apply_relative_tilts(rp_weights, trend_gates, momentum_tilts, rsrs_tilts)
    _log_step("TiltedWeight", "倾斜合成权重", pool, tilted_weights, fmt=".4f", etf_names=etf_names)
    crowd_penalties = compute_crowd_penalties(prices, pool, params)
    _log_step("CrowdPenalty", "拥挤度惩罚", pool, crowd_penalties, fmt=".4f", etf_names=etf_names)
    raw_weights = compose_raw_weights(tilted_weights, trend_gates, crowd_penalties)
    portfolio_vol_scale = compute_portfolio_vol_scale(prices, pool, raw_weights, params)
    log.info("[组合波动率缩放] PortfolioVolScale=%.4f", portfolio_vol_scale)
    final_weights = raw_weights * portfolio_vol_scale
    _log_step("FinalWeight", "最终权重", pool, final_weights, fmt=".4f", etf_names=etf_names)
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
    plan = build_rebalance_plan(context)
    execute_rebalance(context, plan["pool"], plan["final_weights"], plan["params"])
def prepare_delay_only_rebalance(context):
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
    signal_date = _context_trade_date(context)
    g.pending_live_like_signal_days.append(signal_date)
    audit_event(
        "live_like_signal_marked",
        context,
        execution_timing_mode=snapshot_params()["ExecutionTimingMode"],
        signal_date=signal_date,
    )
def execute_live_like_rebalance(context):
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
def normalize_field_frame(raw, field, pool):
    if raw is None:
        return pd.DataFrame(columns=pool)
    if not isinstance(raw, pd.DataFrame):
        return pd.DataFrame(columns=pool)
    if len(raw) == 0:
        return pd.DataFrame(columns=pool)
    raw = raw.reindex(columns=pool)
    return raw.dropna(how='all')
def compute_history_count(params):
    requirements = [
        max(resolve_ma_long_windows(params)),
        max(params["MomShort"], params["MomMid"], params["MomLong"]) + 1,
        params["VolWindow"] + 1,
        params["RSRS_M"] + params["RSRS_N"] - 1,
        params["CrowdWindow"],
        params["PortfolioVolWindow"] + 1,
    ]
    return max(requirements) + params.get("history_buffer", 50)
def fetch_field(pool, field, count, params, end_date=None):
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
    needed = compute_history_count(params)
    prices = {}
    prices['close'] = fetch_field(pool, 'close', needed, params, end_date=context.previous_date)
    prices['high'] = fetch_field(pool, 'high', needed, params, end_date=context.previous_date)
    prices['low'] = fetch_field(pool, 'low', needed, params, end_date=context.previous_date)
    prices['amount'] = fetch_field(pool, 'money', needed, params, end_date=context.previous_date)
    prices['close_ret'] = prices['close'].pct_change()
    close_df = prices['close']
    last_dt = close_df.index[-1] if len(close_df) else None
    log.info("history end_date=%s, context.previous_date=%s", last_dt, context.previous_date)
    return prices
def compute_trend_gates(prices, pool, params):
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
def compute_momentum_scores(prices, pool, trend_gates, params):
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
        period_ret = latest / past - 1
        ranks = period_ret.rank(pct=True).fillna(0.0)
        for idx_pos, active_i in enumerate(active_indices):
            etf_code = pool[active_i]
            scores[active_i] += period_weights[j] * float(ranks.get(etf_code, 0.0))
    return scores
def select_topk(momentum_scores, trend_gates, params):
    n = len(momentum_scores)
    selected = [False] * n
    active = [(i, momentum_scores[i]) for i in range(n) if trend_gates[i] > 0]
    active.sort(key=lambda x: x[1], reverse=True)
    k = min(params["TopK"], len(active))
    for idx, _ in active[:k]:
        selected[idx] = True
    return selected
def compute_rp_weights(prices, pool, active_mask, params):
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
def compute_rsrs_multipliers(prices, pool, params):
    rsrs_adj = compute_rsrs_adjusted_scores(prices, pool, params)
    full_cut = params["RSRS_NegativeFullCut"]
    n = len(pool)
    multipliers = np.ones(n)
    for i in range(n):
        raw_mult = 1.0 + rsrs_adj[i] / full_cut
        multipliers[i] = np.clip(raw_mult, params["RSRSMinMultiplier"], params["RSRSMaxMultiplier"])
    return multipliers
def compute_rsrs_adjusted_scores(prices, pool, params):
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
def compute_momentum_tilt_multipliers(momentum_scores, trend_gates, params):
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
def compute_rsrs_tilt_multipliers(prices, pool, trend_gates, params):
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
def apply_relative_tilts(rp_weights, trend_gates, momentum_tilts, rsrs_tilts):
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
def compute_crowd_penalties(prices, pool, params):
    close = prices['close']
    amount = prices['amount']
    n = len(pool)
    crowd_window = params["CrowdWindow"]
    thresholds = resolve_crowd_thresholds(params)
    ret_windows = resolve_crowd_ret_windows(params)
    penalties = np.ones(n)
    eligible_etfs = []
    for i, etf in enumerate(pool):
        if etf in close.columns and len(close[etf].dropna()) >= crowd_window:
            eligible_etfs.append(etf)
        else:
            penalties[i] = 1.0
    if not eligible_etfs:
        return penalties
    close_recent = close[eligible_etfs].iloc[-crowd_window:]
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
    eligible_amount_cols = [e for e in eligible_etfs if e in amount.columns]
    amt_ma20_df = None
    if eligible_amount_cols:
        amt_aligned = amount[eligible_amount_cols].loc[
            amount.index.intersection(close_recent.index)
        ]
        if len(amt_aligned) >= params["AmountMAWindow"]:
            amt_ma20_df = amt_aligned.rolling(params["AmountMAWindow"]).mean()
    ma20_df = close_recent.rolling(params["DeviationMAWindow"]).mean()
    deviation_df = close_recent / ma20_df - 1
    vol20_df = close_recent.pct_change().rolling(params["CrowdVolWindow"]).std() * np.sqrt(params["annual_factor"])
    for i, etf in enumerate(pool):
        if etf not in eligible_etfs:
            continue
        indicators = []
        short_w = ret_windows[i][0]
        col_short = ret_short_map[short_w][etf].dropna()
        indicators.append(percentile_rank(col_short.iloc[-1], col_short) if len(col_short) > 1 else 0.5)
        mid_w = ret_windows[i][1]
        col_mid = ret_mid_map[mid_w][etf].dropna()
        indicators.append(percentile_rank(col_mid.iloc[-1], col_mid) if len(col_mid) > 1 else 0.5)
        if amt_ma20_df is not None and etf in amt_ma20_df.columns:
            col_amt = amt_ma20_df[etf].dropna()
            indicators.append(percentile_rank(col_amt.iloc[-1], col_amt) if len(col_amt) > 1 else 0.5)
        else:
            indicators.append(0.5)
        col_dev = deviation_df[etf].dropna()
        indicators.append(percentile_rank(col_dev.iloc[-1], col_dev) if len(col_dev) > 1 else 0.5)
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
def percentile_rank(value, series):
    if len(series) == 0:
        return 0.5
    ranked = (series < value).mean()
    return float(ranked)
def compute_portfolio_vol_scale(prices, pool, raw_weights, params):
    close_ret = prices['close_ret']
    vol_window = params["PortfolioVolWindow"]
    target_vol = params["TargetVol"]
    annual_factor = params["annual_factor"]
    n = len(pool)
    active_indices = [i for i in range(n) if raw_weights[i] > 1e-8]
    if not active_indices:
        return 1.0
    returns_list = []
    for i in active_indices:
        etf = pool[i]
        if etf not in close_ret.columns:
            return 1.0
        ret = close_ret[etf].dropna().iloc[-vol_window:]
        if len(ret) < vol_window:
            return 1.0
        returns_list.append(ret.values)
    if not returns_list:
        return 1.0
    ret_matrix = np.column_stack(returns_list)
    cov_daily = np.atleast_2d(np.cov(ret_matrix, rowvar=False))
    cov_annual = cov_daily * annual_factor
    active_weights = np.array([raw_weights[i] for i in active_indices])
    portfolio_var = active_weights @ cov_annual @ active_weights
    portfolio_vol = np.sqrt(max(portfolio_var, 0))
    if portfolio_vol <= target_vol or portfolio_vol < 1e-8:
        return 1.0
    scale = target_vol / portfolio_vol
    return min(scale, params["MaxPortfolioVolScale"])
def apply_weight_constraints(final_weights, params):
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
def execute_rebalance(context, pool, final_weights, params):
    account_value = context.portfolio.total_value
    current_data = get_current_data()
    etf_names = build_etf_display_names(pool, params.get("etf_names"))
    for i, etf in enumerate(pool):
        etf_name = etf_names[i]
        target_value = account_value * final_weights[i]
        current_pos = context.portfolio.positions[etf]
        current_value = current_pos.total_amount * current_pos.price if current_pos.total_amount > 0 else 0
        current_weight = current_value / account_value if account_value > 0 else 0
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