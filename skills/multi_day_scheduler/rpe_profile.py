"""
rpe_profile.py —— RPE 画像系统
==============================

基于 Keep RPE（主观劳累程度）的轻量画像 + 夜间反馈机制。
仅需 2 题轻量画像，每日 21:00 一键反馈，系统自适应调整次日体力容量。

纯数据模型 + 纯函数，无 I/O 依赖。
"""

import copy

# ======================================================================
# Onboarding 映射表
# ======================================================================

# Q1: "和谁出行" → E_max（最大耐力分钟数，归一化到 0-100 尺度）
COMPANION_TO_EMAX = {
    "独自出行": 90,
    "情侣出行": 70,
    "朋友结伴": 75,
    "带娃": 50,
    "带老人": 45,
}

# Q2: "日常体力段位" → mental_multiplier（精神疲惫乘数）
FITNESS_TO_MENTAL = {
    "经常运动": 0.9,
    "日常运动": 1.0,
    "偶尔运动": 1.15,
    "很少运动": 1.3,
}

# RPE 反馈调整系数
RPE_ADJUSTMENTS = {
    "green":  {"e_max_factor": 1.0,  "mental_add": 0.0,  "force_rest": 0},
    "yellow": {"e_max_factor": 0.85, "mental_add": 0.05, "force_rest": 0},
    "red":    {"e_max_factor": 0.65, "mental_add": 0.1,  "force_rest": 90},
}

# 边界钳制
E_MAX_FLOOR = 20
E_MAX_CEILING = 100
MENTAL_FLOOR = 0.6
MENTAL_CEILING = 1.5


def create_rpe_profile(companion: str, fitness_level: str) -> dict:
    """
    从 2 题轻量画像创建 RPE 画像。

    参数:
        companion: 和谁出行（"独自出行"|"情侣出行"|"朋友结伴"|"带娃"|"带老人"）
        fitness_level: 日常体力段位（"经常运动"|"日常运动"|"偶尔运动"|"很少运动"）

    返回:
        {"e_max": int, "mental_multiplier": float, "rpe_status": "green",
         "original_e_max": int, "original_mental": float}
    """
    if companion not in COMPANION_TO_EMAX:
        raise ValueError(f"未知的同伴类型: {companion}，可选: {list(COMPANION_TO_EMAX.keys())}")
    if fitness_level not in FITNESS_TO_MENTAL:
        raise ValueError(f"未知的体力段位: {fitness_level}，可选: {list(FITNESS_TO_MENTAL.keys())}")

    e_max = COMPANION_TO_EMAX[companion]
    mental = FITNESS_TO_MENTAL[fitness_level]

    return {
        "e_max": e_max,
        "mental_multiplier": mental,
        "rpe_status": "green",
        "original_e_max": e_max,
        "original_mental": mental,
    }


def apply_rpe_feedback(profile: dict, rpe_status: str) -> dict:
    """
    应用夜间 RPE 反馈，调整次日体力容量与精神乘数。

    🟢 green  → 不变
    🟡 yellow → E_max × 0.85，mental +0.05
    🔴 red    → E_max × 0.65，mental +0.1，强制插入 ≥90min 休息

    参数:
        profile: create_rpe_profile 的返回值（会被深拷贝，不修改原对象）
        rpe_status: "green" | "yellow" | "red"

    返回:
        调整后的 profile dict
    """
    if rpe_status not in RPE_ADJUSTMENTS:
        raise ValueError(f"未知的 RPE 状态: {rpe_status}，可选: green/yellow/red")

    result = copy.deepcopy(profile)
    adj = RPE_ADJUSTMENTS[rpe_status]

    result["e_max"] = max(E_MAX_FLOOR, min(E_MAX_CEILING, result["e_max"] * adj["e_max_factor"]))
    result["mental_multiplier"] = min(MENTAL_CEILING, result["mental_multiplier"] + adj["mental_add"])
    result["mental_multiplier"] = max(MENTAL_FLOOR, result["mental_multiplier"])
    result["rpe_status"] = rpe_status
    result["force_rest_minutes"] = adj["force_rest"]

    return result
