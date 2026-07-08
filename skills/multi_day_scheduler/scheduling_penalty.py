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
