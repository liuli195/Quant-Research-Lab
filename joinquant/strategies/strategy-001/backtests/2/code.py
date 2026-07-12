enable_profile()

"""
============================================================
策略名称：ETF 多因子轮动策略（线性乘数版）
策略类型：周线级别、场内基金、多因子动态配置
适用标的：AI ETF（159819）、纳指100 ETF（513100）、黄金 ETF（518880）

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


# ============================================================
# 常量 — 内部字段名到聚宽字段名的映射
# ============================================================
FIELD_MAP = {
    "close": "close",
    "high": "high",
    "low": "low",
    "amount": "money",
}


# ============================================================
# snapshot_params — 参数快照
# ============================================================
def snapshot_params():
    """
    从 g 读取全部策略参数，返回只读快照 dict。

    核心计算函数通过接收 params 而非直接读 g，实现解耦。
    """
    return {
        "etf_pool": list(g.etf_pool),
        "etf_names": list(g.etf_names),
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


# ============================================================
# validate_params — 参数校验
# ============================================================
def validate_params(params):
    """
    校验参数合法性，不合法时抛出 ValueError。

    校验规则来自技术实现方案 4.1 节参数校验表。
    """
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

    if errors:
        raise ValueError("; ".join(errors))


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
    g.etf_pool = [
        '159819.XSHE',   # AI ETF
        '513100.XSHG',   # 纳指100 ETF
        '518880.XSHG',   # 黄金 ETF
    ]
    g.etf_names = ['AI ETF', '纳指100ETF', '黄金ETF']

    # ---- 趋势门槛 ----
    g.MA_long = 120

    # ---- 动量选择 ----
    g.MomShort = 20
    g.MomMid = 60
    g.MomLong = 120
    g.w20 = 0.2
    g.w60 = 0.3
    g.w120 = 0.5
    g.TopK = 2

    # ---- 风险平价 ----
    g.VolWindow = 60
    g.annual_factor = 252

    # ---- RSRS 修正 ----
    g.RSRS_N = 18        # 回归窗口
    g.RSRS_M = 600       # 标准化窗口
    g.RSRS_NegativeFullCut = 1.0
    g.RSRSMinMultiplier = 0.0
    g.RSRSMaxMultiplier = 1.0

    # ---- 拥挤度惩罚 ----
    g.CrowdWindow = 500
    g.CrowdRetShort = 20
    g.CrowdRetMid = 60
    g.AmountMAWindow = 20
    g.DeviationMAWindow = 20
    g.CrowdVolWindow = 20
    g.CrowdStart = 0.60
    g.CrowdEnd = 0.95
    g.MinCrowdPenalty = 0.30

    # ---- 组合波动率控制 ----
    g.PortfolioVolWindow = 60
    g.TargetVol = 0.12
    g.MaxPortfolioVolScale = 1.0

    # ---- 仓位与交易约束 ----
    g.MaxWeight = 0.60
    g.MinWeight = 0.05
    g.RebalanceThreshold = 0.03
    g.MaxTotalWeight = 1.0

    # ---- 数据与基准 ----
    # 复权模式：场内基金拆分/合并披露可能不完整，JoinQuant 不建议给含场内基金
    # 的策略开启动态复权。如需验证，将 use_real_price 改为 False、fq_mode 改为 None
    # 后跑云端短回测对比。
    g.use_real_price = True
    g.fq_mode = 'pre'       # 'pre' | 'post' | None（不复权）
    g.live_days = max(
        g.MA_long, g.MomLong, g.RSRS_M,
        g.CrowdWindow, g.PortfolioVolWindow
    ) + 50
    g.history_buffer = 100
    g.benchmark = '000300.XSHG'


# ============================================================
# _log_step — 调仓中间量诊断日志
# ============================================================
def _log_step(name, cn_name, pool, values, fmt=".4f"):
    """
    以 "[中文名] name: ETF=value" 格式逐只打印调仓中间量，便于云端回测诊断。

    不在本地单测中验证日志格式，只保证聚宽云端 log.info 可输出。
    """
    parts = ["%s=%%s" % etf for etf in pool]
    template = "[%s] %s: " % (cn_name, name) + ", ".join(parts)
    formatted = tuple(format(v, fmt) for v in values)
    log.info(template, *formatted)


# ============================================================
# compose_raw_weights — 权重合成
# ============================================================
def compose_raw_weights(rp_weights, trend_gates, selected, rsrs_multipliers, crowd_penalties):
    """
    合成各模块输出为 RawWeight。

    各输入数组长度均为 len(pool)。未入选资产权重为 0。
    """
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


# ============================================================
# weekly_check — 周频调仓主函数
# ============================================================
def weekly_check(context):
    """每周开盘时执行一次完整的调仓流程。"""
    params = snapshot_params()
    pool = params["etf_pool"]
    n = len(pool)

    # 1. 拉取历史数据
    prices = get_history_data(context, pool, params)

    # 2. 计算趋势门槛
    trend_gates = compute_trend_gates(prices, pool, params)
    _log_step("TrendGate", "趋势门槛", pool, trend_gates, fmt=".0f")

    # 3. 筛选趋势成立资产，计算动量分数
    momentum_scores = compute_momentum_scores(prices, pool, trend_gates, params)
    _log_step("MomentumScore", "动量分数", pool, momentum_scores, fmt=".4f")

    # 4. TopK 选择
    selected = select_topk(momentum_scores, trend_gates, params)
    _log_step("Selected", "TopK入选", pool, [1.0 if s else 0.0 for s in selected], fmt=".0f")

    # 5. 风险平价基础权重
    rp_weights = compute_rp_weights(prices, pool, selected, params)
    _log_step("RPWeight", "风险平价权重", pool, rp_weights, fmt=".4f")

    # 6. RSRS 线性修正乘数
    rsrs_multipliers = compute_rsrs_multipliers(prices, pool, params)
    _log_step("RSRSMultiplier", "RSRS修正乘数", pool, rsrs_multipliers, fmt=".4f")

    # 7. 拥挤度线性惩罚乘数
    crowd_penalties = compute_crowd_penalties(prices, pool, params)
    _log_step("CrowdPenalty", "拥挤度惩罚", pool, crowd_penalties, fmt=".4f")

    # 8. 合成 RawWeight
    raw_weights = compose_raw_weights(
        rp_weights, trend_gates, selected, rsrs_multipliers, crowd_penalties
    )

    # 9. 组合波动率缩放
    portfolio_vol_scale = compute_portfolio_vol_scale(prices, pool, raw_weights, params)
    log.info("[组合波动率缩放] PortfolioVolScale=%.4f", portfolio_vol_scale)

    # 10. 最终权重
    final_weights = raw_weights * portfolio_vol_scale
    _log_step("FinalWeight", "最终权重", pool, final_weights, fmt=".4f")

    # 11. 应用交易约束
    final_weights = apply_weight_constraints(final_weights, params)

    # 12. 执行调仓
    execute_rebalance(context, pool, final_weights, params)


# ============================================================
# normalize_field_frame — 数据返回结构归一化
# ============================================================
def normalize_field_frame(raw, field, pool):
    """
    将 get_price 返回结果归一化为 DataFrame(index=日期, columns=ETF代码)。

    处理规则：
      - None 或空 DataFrame → 返回 columns=pool 的空 DataFrame
      - MultiIndex columns → 用 xs(field) 提取单字段
      - 普通宽表 → reindex(columns=pool) 补全缺列
    """
    if raw is None:
        return pd.DataFrame(columns=pool)
    if not isinstance(raw, pd.DataFrame):
        return pd.DataFrame(columns=pool)
    if len(raw) == 0:
        return pd.DataFrame(columns=pool)

    if isinstance(raw.columns, pd.MultiIndex):
        raw = raw.xs(field, axis=1, level=0)

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
        params["MA_long"],
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
    整池拉取单字段数据，返回 DataFrame（index=日期, columns=ETF代码）。

    panel=False 让 get_price 对多标的直接返回 DataFrame（columns=ETF代码），
    一次调用替代逐 ETF 循环，将 API 请求数从 len(pool) 降到 1。

    end_date: 历史数据截止日期。开盘调仓时需传入 context.previous_date
              以避免当日收盘价未来数据。
    """
    raw = get_price(
        pool,
        count=count,
        end_date=end_date,
        frequency='daily',
        fields=[field],
        skip_paused=True,
        fq=params["fq_mode"],
        panel=False,
    )
    return normalize_field_frame(raw, field, pool)


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
    使用 120 日均线判断趋势方向。

    返回: list[float]，1.0 表示通过，0.0 表示剔除
    """
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
def compute_rp_weights(prices, pool, selected, params):
    """
    对入选资产计算逆波动率风险平价权重。

    使用 get_history_data 预计算的 close_ret，避免重复 pct_change()。

    返回: np.array
    """
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


# ============================================================
# compute_rsrs_multipliers — RSRS 线性修正乘数
# ============================================================
def compute_rsrs_multipliers(prices, pool, params):
    """
    对每只 ETF 计算 RSRS 截断线性乘数。

    步骤：
    1. 向量化滚动窗口：过去 RSRS_N 日 High ~ Low 回归，得 β 和 R²
       β = Cov(low, high) / Var(low)  —— OLS 斜率闭式解
       R² = Cov² / (Var(low) × Var(high))  —— 即 Corr(low, high)²
    2. 过去 RSRS_M 日 β 标准化，得 RSRS_Z
    3. RSRS_Adj = RSRS_Z × R²
    4. RSRSMultiplier = clip(1 + RSRS_Adj / NegativeFullCut, 0, 1)

    只减仓，不加仓。使用 pandas 滚动窗口向量化替代逐日 lstsq 循环。

    返回: np.array
    """
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

        # 对齐索引
        common_idx = h.index.intersection(l.index)
        h = h.loc[common_idx]
        l = l.loc[common_idx]

        min_len = M + N - 1
        if len(h) < min_len:
            continue

        # ---- 向量化滚动窗口：一次算完所有 β 和 R² ----
        # pandas rolling + 闭式公式，替代逐日 for 循环 + lstsq
        h_roll = h.rolling(N)
        l_roll = l.rolling(N)
        cov_vals = h_roll.cov(l).dropna().values
        var_l_vals = l_roll.var().dropna().values
        var_h_vals = h_roll.var().dropna().values

        betas = cov_vals / var_l_vals
        r2s = cov_vals ** 2 / (var_h_vals * var_l_vals)

        # 边界守卫：低方差或数值异常时退化为默认值
        bad = (
            (var_l_vals < 1e-10) | (var_h_vals < 1e-10)
            | (~np.isfinite(betas)) | (~np.isfinite(r2s))
        )
        betas[bad] = 1.0
        r2s[bad] = 0.0

        if len(betas) < M:
            continue

        # ---- 取最近 M 个 β 做标准化 ----
        beta_series = betas[-M:]
        mean_beta = np.mean(beta_series)
        std_beta = np.std(beta_series)

        if std_beta < 1e-10:
            rsrs_z = 0.0
        else:
            rsrs_z = (beta_series[-1] - mean_beta) / std_beta

        # 使用最近一期的 R²
        latest_r2 = r2s[-1]
        rsrs_adj = rsrs_z * latest_r2

        # 截断线性乘数（只减不加）
        raw_mult = 1.0 + rsrs_adj / full_cut
        multipliers[i] = np.clip(raw_mult, params["RSRSMinMultiplier"], params["RSRSMaxMultiplier"])

    return multipliers


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
    start = params["CrowdStart"]
    end = params["CrowdEnd"]
    min_penalty = params["MinCrowdPenalty"]

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

    # 1) 20日涨幅
    ret20_df = close_recent / close_recent.shift(params["CrowdRetShort"]) - 1

    # 2) 60日涨幅
    ret60_df = close_recent / close_recent.shift(params["CrowdRetMid"]) - 1

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

        # ret20
        col20 = ret20_df[etf].dropna()
        indicators.append(percentile_rank(col20.iloc[-1], col20) if len(col20) > 1 else 0.5)

        # ret60
        col60 = ret60_df[etf].dropna()
        indicators.append(percentile_rank(col60.iloc[-1], col60) if len(col60) > 1 else 0.5)

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

        if crowd_score <= start:
            penalty = 1.0
        elif crowd_score >= end:
            penalty = min_penalty
        else:
            penalty = 1.0 - (crowd_score - start) / (end - start) * (1.0 - min_penalty)
            penalty = max(min_penalty, min(1.0, penalty))

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
def compute_portfolio_vol_scale(prices, pool, raw_weights, params):
    """
    根据 RawWeight 和协方差矩阵计算组合波动率，按目标波动率缩放。

    使用 get_history_data 预计算的 close_ret，避免重复 pct_change()。
    只缩不放（最大系数为 1.0）。

    返回: float
    """
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


# ============================================================
# apply_weight_constraints — 应用仓位约束
# ============================================================
def apply_weight_constraints(final_weights, params):
    """
    应用单资产最大仓位和最小有效仓位约束。

    不重新归一化。
    """
    max_w = params["MaxWeight"]
    min_w = params["MinWeight"]
    n = len(final_weights)

    result = np.copy(final_weights)

    for i in range(n):
        # 单资产最大仓位上限
        if result[i] > max_w:
            result[i] = max_w
        # 最小有效仓位裁剪
        if result[i] < min_w:
            result[i] = 0.0

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

    for i, etf in enumerate(pool):
        target_value = account_value * final_weights[i]
        current_pos = context.portfolio.positions[etf]
        current_value = current_pos.total_amount * current_pos.price if current_pos.total_amount > 0 else 0
        current_weight = current_value / account_value if account_value > 0 else 0

        # 如果目标权重为 0 且当前仓位为 0，跳过
        if final_weights[i] == 0 and current_weight == 0:
            continue

        # 最小调仓阈值
        if abs(final_weights[i] - current_weight) < params["RebalanceThreshold"]:
            continue

        # 停牌检查
        data = current_data[etf]
        if data.paused:
            log.warning("skip paused ETF: %s", etf)
            continue

        order_obj = order_target_value(etf, target_value)
        if order_obj is None:
            log.error(
                "order failed: %s target_value=%.2f target_weight=%.4f current_weight=%.4f",
                etf, target_value, final_weights[i], current_weight
            )
        else:
            log.info(
                "order sent: %s target_weight=%.4f current_weight=%.4f target_value=%.2f",
                etf, final_weights[i], current_weight, target_value
            )
