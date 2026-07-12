enable_profile()
"""
============================================================
策略名称：ETF 多因子轮动策略（线性乘数版）
策略类型：周线级别、场内基金、多因子动态配置
适用标的：159819.XSHE、513100.XSHG、518880.XSHG（中文名运行时通过聚宽 API 读取）
核心思想：
  趋势门槛判断"能不能买"，动量排序决定"买谁"，风险平价分配基础仓位，
  RSRS 线性修正和拥挤度线性惩罚在价格结构转弱或交易过热时平滑降仓，
  组合波动率控制缩放总仓位。剩余仓位保留现金，不重新归一化到满仓。
模块分工：
  - 趋势门槛（硬过滤）：120 日均线以上才可入选，0/1 离散
  - 动量选择（TopK）：多周期排名分数加权，选前 K 只
  - 风险平价（逆波动率）：σ 越小权重越大，波动率归一化
  - RSRS 修正（只减不加）：High~Low 回归 β 标准化 × R²，线性截断到 [0, 1]
  - 拥挤度惩罚（只减不加）：五指标分位数均值，超阈值线性打折
  - 组合波动率控制（只缩不放）：RawWeight 组合波动率超目标时等比缩放
核心公式：
  FinalWeight_i = RPWeight_i × TrendGate_i × RSRSMultiplier_i
                × CrowdPenalty_i × PortfolioVolScale
调仓频率：每周开盘检查一次
============================================================
"""
import numpy as np
import pandas as pd
FIELD_MAP = {
    "close": "close",
    "high": "high",
    "low": "low",
    "amount": "money",
}
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
def snapshot_params():
    etf_pool = list(g.etf_pool)
    etf_names = build_etf_display_names(etf_pool, list(g.etf_names))
    return {
        "etf_pool": etf_pool,
        "etf_names": etf_names,
        "benchmark": g.benchmark,
        "MA_long": g.MA_long,
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
        "CrowdWindow": g.CrowdWindow,
        "CrowdRetShort": g.CrowdRetShort,
        "CrowdRetMid": g.CrowdRetMid,
        "AmountMAWindow": g.AmountMAWindow,
        "DeviationMAWindow": g.DeviationMAWindow,
        "CrowdVolWindow": g.CrowdVolWindow,
        "CrowdStart": g.CrowdStart,
        "CrowdEnd": g.CrowdEnd,
        "MinCrowdPenalty": g.MinCrowdPenalty,
        "PortfolioVolWindow": g.PortfolioVolWindow,
        "TargetVol": g.TargetVol,
        "MaxPortfolioVolScale": g.MaxPortfolioVolScale,
        "MaxWeight": g.MaxWeight,
        "MinWeight": g.MinWeight,
        "RebalanceThreshold": g.RebalanceThreshold,
        "MaxTotalWeight": g.MaxTotalWeight,
        "use_real_price": g.use_real_price,
        "fq_mode": g.fq_mode,
        "history_buffer": g.history_buffer,
    }
def validate_params(params):
    errors = []
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
    if len(params["etf_pool"]) != len(params["etf_names"]):
        errors.append("etf_pool and etf_names must have the same length")
    for etf, name in zip(params["etf_pool"], params["etf_names"]):
        if str(etf) not in str(name):
            errors.append("etf_names must include JoinQuant security code: %s" % etf)
    if errors:
        raise ValueError("; ".join(errors))
def initialize(context):
    set_parameter(context)
    validate_params(snapshot_params())
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
    run_weekly(
        weekly_check,
        weekday=1,
        time='open',
        reference_security='000300.XSHG'
    )
def set_parameter(context):
    g.etf_pool = [
        '159819.XSHE',
        '513100.XSHG',
        '518880.XSHG',
    ]
    g.etf_names = load_etf_display_names(g.etf_pool)
    g.MA_long = 120
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
    g.CrowdWindow = 500
    g.CrowdRetShort = 20
    g.CrowdRetMid = 60
    g.AmountMAWindow = 20
    g.DeviationMAWindow = 20
    g.CrowdVolWindow = 20
    g.CrowdStart = 0.60
    g.CrowdEnd = 0.95
    g.MinCrowdPenalty = 0.30
    g.PortfolioVolWindow = 60
    g.TargetVol = 0.08
    g.MaxPortfolioVolScale = 1.0
    g.MaxWeight = 0.60
    g.MinWeight = 0.05
    g.RebalanceThreshold = 0.03
    g.MaxTotalWeight = 1.0
    g.use_real_price = False
    g.fq_mode = None
    g.live_days = max(
        g.MA_long, g.MomLong, g.RSRS_M,
        g.CrowdWindow, g.PortfolioVolWindow
    ) + 50
    g.history_buffer = 100
    g.benchmark = '000300.XSHG'
def _log_step(name, cn_name, pool, values, fmt=".4f", etf_names=None):
    labels = build_etf_display_names(pool, etf_names)
    parts = ["%s=%%s" % label for label in labels]
    template = "[%s] %s: " % (cn_name, name) + ", ".join(parts)
    formatted = tuple(format(v, fmt) for v in values)
    log.info(template, *formatted)
def compose_raw_weights(rp_weights, trend_gates, selected, rsrs_multipliers, crowd_penalties):
    n = len(rp_weights)
    raw = np.zeros(n)
    for i in range(n):
        if selected[i]:
            raw[i] = (
                rp_weights[i]
                * trend_gates[i]
                * rsrs_multipliers[i]
                * crowd_penalties[i]
            )
    return raw
def weekly_check(context):
    params = snapshot_params()
    pool = params["etf_pool"]
    etf_names = params["etf_names"]
    n = len(pool)
    prices = get_history_data(context, pool, params)
    trend_gates = compute_trend_gates(prices, pool, params)
    _log_step("TrendGate", "趋势门槛", pool, trend_gates, fmt=".0f", etf_names=etf_names)
    momentum_scores = compute_momentum_scores(prices, pool, trend_gates, params)
    _log_step("MomentumScore", "动量分数", pool, momentum_scores, fmt=".4f", etf_names=etf_names)
    selected = select_topk(momentum_scores, trend_gates, params)
    _log_step(
        "Selected",
        "TopK入选",
        pool,
        [1.0 if s else 0.0 for s in selected],
        fmt=".0f",
        etf_names=etf_names,
    )
    rp_weights = compute_rp_weights(prices, pool, selected, params)
    _log_step("RPWeight", "风险平价权重", pool, rp_weights, fmt=".4f", etf_names=etf_names)
    rsrs_multipliers = compute_rsrs_multipliers(prices, pool, params)
    _log_step(
        "RSRSMultiplier",
        "RSRS修正乘数",
        pool,
        rsrs_multipliers,
        fmt=".4f",
        etf_names=etf_names,
    )
    crowd_penalties = compute_crowd_penalties(prices, pool, params)
    _log_step("CrowdPenalty", "拥挤度惩罚", pool, crowd_penalties, fmt=".4f", etf_names=etf_names)
    raw_weights = compose_raw_weights(
        rp_weights, trend_gates, selected, rsrs_multipliers, crowd_penalties
    )
    portfolio_vol_scale = compute_portfolio_vol_scale(prices, pool, raw_weights, params)
    log.info("[组合波动率缩放] PortfolioVolScale=%.4f", portfolio_vol_scale)
    final_weights = raw_weights * portfolio_vol_scale
    _log_step("FinalWeight", "最终权重", pool, final_weights, fmt=".4f", etf_names=etf_names)
    final_weights = apply_weight_constraints(final_weights, params)
    execute_rebalance(context, pool, final_weights, params)
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
        params["MA_long"],
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
    ma_window = params["MA_long"]
    gates = np.zeros(len(pool))
    for i, etf in enumerate(pool):
        if etf not in close.columns:
            continue
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
def compute_rp_weights(prices, pool, selected, params):
    close_ret = prices['close_ret']
    n = len(pool)
    vol_window = params["VolWindow"]
    annual_factor = params["annual_factor"]
    weights = np.zeros(n)
    selected_indices = [i for i in range(n) if selected[i]]
    if not selected_indices:
        return weights
    vols = np.zeros(n)
    for i in selected_indices:
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
    for i in selected_indices:
        if vols[i] > 0:
            inverse_vols[i] = 1.0 / vols[i]
    total_inv_vol = inverse_vols.sum()
    if total_inv_vol > 0:
        weights = inverse_vols / total_inv_vol
    return weights
def compute_rsrs_multipliers(prices, pool, params):
    high = prices['high']
    low = prices['low']
    n = len(pool)
    N = params["RSRS_N"]
    M = params["RSRS_M"]
    full_cut = params["RSRS_NegativeFullCut"]
    multipliers = np.ones(n)
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
        rsrs_adj = rsrs_z * latest_r2
        raw_mult = 1.0 + rsrs_adj / full_cut
        multipliers[i] = np.clip(raw_mult, params["RSRSMinMultiplier"], params["RSRSMaxMultiplier"])
    return multipliers
def compute_crowd_penalties(prices, pool, params):
    close = prices['close']
    amount = prices['amount']
    n = len(pool)
    crowd_window = params["CrowdWindow"]
    start = params["CrowdStart"]
    end = params["CrowdEnd"]
    min_penalty = params["MinCrowdPenalty"]
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
    ret20_df = close_recent / close_recent.shift(params["CrowdRetShort"]) - 1
    ret60_df = close_recent / close_recent.shift(params["CrowdRetMid"]) - 1
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
        col20 = ret20_df[etf].dropna()
        indicators.append(percentile_rank(col20.iloc[-1], col20) if len(col20) > 1 else 0.5)
        col60 = ret60_df[etf].dropna()
        indicators.append(percentile_rank(col60.iloc[-1], col60) if len(col60) > 1 else 0.5)
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
        if crowd_score <= start:
            penalty = 1.0
        elif crowd_score >= end:
            penalty = min_penalty
        else:
            penalty = 1.0 - (crowd_score - start) / (end - start) * (1.0 - min_penalty)
            penalty = max(min_penalty, min(1.0, penalty))
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
    n = len(final_weights)
    result = np.copy(final_weights)
    for i in range(n):
        if result[i] > max_w:
            result[i] = max_w
        if result[i] < min_w:
            result[i] = 0.0
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
            continue
        if abs(final_weights[i] - current_weight) < params["RebalanceThreshold"]:
            continue
        data = current_data[etf]
        if data.paused:
            log.warning("skip paused ETF: %s security=%s", etf_name, etf)
            continue
        order_obj = order_target_value(etf, target_value)
        if order_obj is None:
            log.error(
                "order failed: %s security=%s target_value=%.2f target_weight=%.4f current_weight=%.4f",
                etf_name, etf, target_value, final_weights[i], current_weight
            )
        else:
            log.info(
                "order sent: %s security=%s target_weight=%.4f current_weight=%.4f target_value=%.2f",
                etf_name, etf, final_weights[i], current_weight, target_value
            )