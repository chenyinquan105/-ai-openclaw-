"""
hotel_decision.py —— 酒店决策引擎
================================

"非必要不换房"的舒适优先策略：在平衡"转场搬行李的痛苦"与"多跑路的时间损耗"之间，
通过 ROI 判定做出最优决策。

规则：晚上8点前结束行程 + 体力很好 → 推荐换房，否则不换。

纯决策逻辑，无 I/O 依赖。
"""

# ======================================================================
# 决策阈值常量
# ======================================================================

THETA_FATIGUE = 0.7          # 疲劳一票否决阈值（0-1 尺度）
DELTA_T_SINGLE_DAY = 60      # 单日通勤节省阈值（分钟）
DELTA_T_CUMULATIVE = 90      # 累计通勤节省阈值（分钟）

# 换房策略阈值
SWITCH_MAX_END_TIME = 1200   # 20:00（分钟），晚于此不推荐换房
SWITCH_MAX_FATIGUE = 0.3     # 疲劳 >= 0.3 不推荐换房


def should_switch_hotel(fatigue: float, time_saved_single: float,
                         time_saved_cumulative: float) -> tuple:
    """
    换房 ROI 判定：体力一票否决 + 时间节省门槛。

    参数:
        fatigue: 当日疲劳值（0-1 尺度，1=极度疲乏）
        time_saved_single: 单日预估节省时间（分钟）
        time_saved_cumulative: 多日累计预估节省时间（分钟）

    返回:
        (should_switch: bool, reason: str)
    """
    # 体力一票否决
    if fatigue >= THETA_FATIGUE:
        return (False, "fatigue_veto")

    # 时间节省门槛
    if time_saved_single >= DELTA_T_SINGLE_DAY:
        return (True, "roi_met_single_day")
    if time_saved_cumulative >= DELTA_T_CUMULATIVE:
        return (True, "roi_met_cumulative")

    return (False, "below_threshold")


def determine_strategy(fatigue: float, end_time_minutes: float) -> str:
    """
    根据疲劳 + 当日结束时间选择住宿策略。

    两种策略:
      - "switch": 推荐换房（行程20:00前结束 + 体力好）
      - "sustained": 不换房（结束晚 或 体力不足）

    参数:
        fatigue: 当日疲劳值（0-1 尺度）
        end_time_minutes: 当日预计结束时间（分钟数）

    返回:
        策略字符串
    """
    # 晚上8点前结束 + 体力好 → 推荐换房
    if end_time_minutes < SWITCH_MAX_END_TIME and fatigue < SWITCH_MAX_FATIGUE:
        return "switch"

    return "sustained"
