"""
scheduling_penalty.py —— 排程惩罚函数模块
==========================================
提供统一的时间偏离惩罚函数，供 _build_timeline 和 _refine_timeline 复用。

核心思想：用餐时间不应该是二元的"行/不行"，而应该是连续的"最优/勉强/不可接受"。
用惩罚值替代硬 kill，让排程器可以在"跳过一个景点"和"推迟用餐"之间做代价比较。
"""

# ======================================================================
# 用餐时间惩罚配置
# ======================================================================

MEAL_PENALTY_CONFIG = {
    "lunch": {
        "anchor": 720,               # 12:00，单位：分钟
        "comfort_half_width": 60,    # 舒适区半宽：11:00-13:00
        "tolerable_half_width": 150, # 勉强区半宽：09:30-14:30
        "a1": 1.0,                   # 舒适区外线性系数（每分钟惩罚）
        "a2": 4.0,                   # 勉强区外线性系数（每分钟惩罚，更陡）
    },
    "dinner": {
        "anchor": 1110,              # 18:30
        "comfort_half_width": 60,    # 17:30-19:30
        "tolerable_half_width": 150, # 16:00-21:00
        "a1": 1.0,
        "a2": 4.0,
    },
}

# 超出勉强区视为逻辑不可行，但仍返回有限大数值（而非无穷大），
# 方便和"跳过该点"的代价做数值比较
INFEASIBLE_PENALTY = 1e6

# 跳过一个目的地的基础损失分值（供 _build_timeline 和 _refine_timeline 使用）
SKIP_PENALTY_BASE = 200

# 每分钟通勤的机会成本系数（供 _total_cost 使用）
LAMBDA_TRAVEL = 0.3

# ======================================================================
# 品类体力系数（供 fatigue_cost 和 _total_cost 使用）
# ======================================================================

FATIGUE_COEFFICIENT = {
    # 活动类
    "scenic": 1.5,
    "shopping": 1.0,
    "cinema": 0.5,
    "gym": 1.2,
    "cafe": 0.3,
    "restaurant": 0.3,
    "hotpot": 0.3,
    "japanese": 0.3,
    "breakfast": 0.2,
    "hair": 0.4,
    "pet": 0.4,
    "laundry": 0.3,
    "default": 0.8,
    # 交通类（display 系数 ÷ 4.67 映射到优化器尺度）
    "travel_walk": 0.85,    # 步行赶路  → display 4.0
    "travel_plane": 0.65,   # 飞机      → display 3.0
    "travel_car": 0.3,      # 汽车/打车 → display 1.5
    "travel_train": 0.2,    # 高铁/火车 → display 1.0
}

# 体力惩罚权重（供 _total_cost 使用）
LAMBDA_FATIGUE = 0.5


# ======================================================================
# 惩罚函数
# ======================================================================

def meal_time_penalty(meal_type: str, proposed_minutes: float) -> float:
    """
    计算某个用餐时间点相对锚点时间的惩罚值。

    参数:
        meal_type: "lunch" | "dinner"
        proposed_minutes: 计划用餐开始时间（从0点起的分钟数）

    返回:
        惩罚值（>=0，越小越好）
        - 舒适区内: 0
        - 舒适区外但勉强区内: 线性增长（系数 a1）
        - 勉强区外: 更快线性增长（系数 a2），但保持有限值
    """
    cfg = MEAL_PENALTY_CONFIG[meal_type]
    d = abs(proposed_minutes - cfg["anchor"])

    if d <= cfg["comfort_half_width"]:
        return 0.0
    elif d <= cfg["tolerable_half_width"]:
        return cfg["a1"] * (d - cfg["comfort_half_width"])
    else:
        # 勉强区外，仍返回有限但很大的值
        base = cfg["a1"] * (cfg["tolerable_half_width"] - cfg["comfort_half_width"])
        overflow = cfg["a2"] * (d - cfg["tolerable_half_width"])
        return base + overflow


# ======================================================================
# 动态体力模型 —— 时段乘数 γ_time
# ======================================================================

# 烈日暴晒窗口（13:00-15:00），此时间段体力消耗 ×1.3
NOON_FATIGUE_WINDOW = (780, 900)  # 13:00-15:00，单位：分钟
NOON_FATIGUE_MULTIPLIER = 1.3


def time_of_day_fatigue_multiplier(minutes: float) -> float:
    """
    返回给定时刻的体力消耗时段乘数 γ_time。

    参数:
        minutes: 一天中的分钟数（0-1440）

    返回:
        1.0（默认）或 1.3（13:00-15:00 烈日暴晒窗口）
    """
    if NOON_FATIGUE_WINDOW[0] <= minutes <= NOON_FATIGUE_WINDOW[1]:
        return NOON_FATIGUE_MULTIPLIER
    return 1.0


# ======================================================================
# 动态体力模型 —— 多日累积乘数 δ_day
# ======================================================================

MAX_MULTI_DAY_MULTIPLIER = 2.0
MULTI_DAY_ACCUMULATION_RATE = 0.25  # 前日疲劳每 1 单位增加 0.0025 的乘数


def multi_day_fatigue_multiplier(day_index: int, prev_day_fatigue: float) -> float:
    """
    计算多日乳酸堆积滞后乘数 δ_day。

    第 0 天无累积，第 N+1 天的基础体力消耗乘以 (1 + 0.25 × prev/100)，
    模拟连续出行时的乳酸堆积效应。

    参数:
        day_index: 天数索引（0=第一天）
        prev_day_fatigue: 前一天结束时的累积疲劳值（0-100 尺度）

    返回:
        乘数（1.0-2.0），上限钳制在 MAX_MULTI_DAY_MULTIPLIER
    """
    if day_index <= 0:
        return 1.0
    clamped = max(0.0, prev_day_fatigue)
    multiplier = 1.0 + MULTI_DAY_ACCUMULATION_RATE * (clamped / 100.0)
    return min(multiplier, MAX_MULTI_DAY_MULTIPLIER)


# ======================================================================
# 动态体力模型 —— 动态体力消耗计算
# ======================================================================

def _classify_travel_category(action: str, memo: str = "") -> str:
    """根据 action 和 memo 判断交通方式的体力类别。

    返回 FATIGUE_COEFFICIENT / DISPLAY_FATIGUE_COEFFICIENT 的 key。
    """
    # 去程/返程：从 memo 识别飞机 vs 高铁
    if action in ("OUTBOUND_JOURNEY", "RETURN_JOURNEY"):
        if "飞机" in memo:
            return "travel_plane"
        if "高铁" in memo or "火车" in memo:
            return "travel_train"
        # 默认按汽车处理（短途大巴等）
        return "travel_car"

    # 步行回酒店
    if action == "HOTEL_PENDING":
        return "travel_walk"

    # 汽车接送（家→站、站→酒店）
    if action in ("LEAVE_HOME", "TO_STATION", "ARRIVAL_TRANSIT"):
        return "travel_car"

    # 站内步行（到达、出发、到家）
    if action in ("ARRIVAL", "DEPARTURE", "ARRIVE_HOME"):
        return "travel_walk"

    return "travel_car"


def dynamic_fatigue_cost(timeline: list, day_index: int = 0,
                          prev_day_fatigue: float = 0) -> float:
    """
    计算动态体力消耗惩罚值，组合 γ_time（时段）和 δ_day（多日累积）。

    体力初始为 100，每项 VISIT + 交通 活动消耗体力：
        品类系数 × (duration/60) × γ_time(start) × δ_day(day_index, prev_day_fatigue)

    休息/用餐节点恢复体力。体力低于 30 时产生二次惩罚。

    参数:
        timeline: 精修层格式的 timeline 节点列表
        day_index: 天数索引（0=第一天）
        prev_day_fatigue: 前一天结束时的累积疲劳值

    返回:
        体力惩罚值（>=0）
    """
    fatigue_level = 100.0
    total_penalty = 0.0

    delta = multi_day_fatigue_multiplier(day_index, prev_day_fatigue)

    for node in timeline:
        node_type = node.get("type") or node.get("action", "")

        if node_type == "VISIT":
            cat = node.get("category", "default")
            coef = FATIGUE_COEFFICIENT.get(cat, FATIGUE_COEFFICIENT.get("default", 0.8))
            dur_hours = node.get("duration_minutes", 60) / 60.0
            start_min = node.get("start_minutes", 540)
            gamma = time_of_day_fatigue_multiplier(start_min)

            fatigue_level -= coef * dur_hours * gamma * delta

            if fatigue_level < 30:
                total_penalty += (30 - fatigue_level) ** 2
        elif node_type in ("LUNCH", "DINNER", "REST", "BREAKFAST",
                           "LUNCH_NEEDED", "DINNER_NEEDED", "BREAKFAST_NEEDED"):
            fatigue_level = min(100.0, fatigue_level + 10)
        elif node_type in ("LEAVE_HOME", "TO_STATION", "OUTBOUND_JOURNEY",
                           "ARRIVAL", "ARRIVAL_TRANSIT", "HOTEL_PENDING",
                           "RETURN_JOURNEY", "DEPARTURE", "ARRIVE_HOME"):
            # 交通节点：按交通方式取系数 × 时长
            travel_cat = _classify_travel_category(node_type, node.get("memo", ""))
            coef = FATIGUE_COEFFICIENT.get(travel_cat, 0.3)
            dur_hours = node.get("duration_minutes", 30) / 60.0
            start_min = node.get("start_minutes", node.get("time") and _parse_time_minutes(node.get("time", "09:00")) or 540)
            gamma = time_of_day_fatigue_multiplier(start_min)

            fatigue_level -= coef * dur_hours * gamma * delta

            if fatigue_level < 30:
                total_penalty += (30 - fatigue_level) ** 2

    return LAMBDA_FATIGUE * total_penalty


def _parse_time_minutes(time_str: str) -> int:
    """将 HH:MM 字符串转为分钟数。"""
    try:
        h, m = map(int, time_str.split(":"))
        return h * 60 + m
    except (ValueError, TypeError):
        return 540
