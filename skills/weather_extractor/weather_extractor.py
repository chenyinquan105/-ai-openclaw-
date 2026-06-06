"""
weather_extractor — 天气活动抽取 Skill

模拟天气数据查询，基于坐标+日期生成伪随机天气，输出活动建议。
"""
import hashlib
import random
from typing import Optional


# 天气条件枚举（中文 → 属性）
_WEATHER_CONDITIONS = [
    {"condition": "晴", "en": "sunny", "outdoor": True, "temp_mod": 3},
    {"condition": "多云", "en": "cloudy", "outdoor": True, "temp_mod": 0},
    {"condition": "阴", "en": "overcast", "outdoor": True, "temp_mod": -1},
    {"condition": "小雨", "en": "light_rain", "outdoor": False, "temp_mod": -2, "walking_penalty": 0.8},
    {"condition": "中雨", "en": "moderate_rain", "outdoor": False, "temp_mod": -4, "walking_penalty": 0.6},
    {"condition": "大雨", "en": "heavy_rain", "outdoor": False, "temp_mod": -6, "walking_penalty": 0.4},
    {"condition": "暴雨", "en": "storm", "outdoor": False, "temp_mod": -8, "walking_penalty": 0.2, "alert": "暴雨预警！"},
    {"condition": "小雪", "en": "light_snow", "outdoor": True, "temp_mod": -5, "walking_penalty": 0.7},
    {"condition": "大雪", "en": "heavy_snow", "outdoor": False, "temp_mod": -10, "walking_penalty": 0.3, "alert": "暴雪预警！"},
]


def _seeded_random(coord: str, date: str) -> random.Random:
    """用坐标+日期生成确定性随机种子"""
    seed_str = f"{coord}|{date}"
    seed_hash = hashlib.md5(seed_str.encode()).hexdigest()
    seed_int = int(seed_hash[:8], 16)
    return random.Random(seed_int)


def extract_weather(
    coord: str,
    date: str = "2026-06-06",
) -> dict:
    """
    查询指定坐标和日期的天气状况。

    参数:
        coord: 坐标 "lat,lng"
        date: 日期 "YYYY-MM-DD"

    返回:
        dict: 天气数据 + 活动建议
    """
    # ── 入参校验 ──
    if not coord or "," not in coord:
        return {"status": "ERROR", "message": "坐标格式无效"}
    try:
        parts = coord.strip().split(",")
        float(parts[0].strip())
        float(parts[1].strip())
    except ValueError:
        return {"status": "ERROR", "message": "坐标格式无效"}

    rng = _seeded_random(coord, date)

    # ── 基准天气 ──
    # 6月北京：偏热，偶有雨
    month = int(date.split("-")[1]) if "-" in date else 6
    if month in (6, 7, 8):
        weights = [0.35, 0.20, 0.10, 0.15, 0.10, 0.05, 0.03, 0.01, 0.01]
    elif month in (12, 1, 2):
        weights = [0.40, 0.20, 0.10, 0.05, 0.03, 0.02, 0.01, 0.10, 0.09]
    else:
        weights = [0.30, 0.25, 0.15, 0.10, 0.08, 0.05, 0.03, 0.02, 0.02]

    cond = rng.choices(_WEATHER_CONDITIONS, weights=weights, k=1)[0]

    # 基准温度
    if month in (6, 7, 8):
        base_temp = 30
    elif month in (12, 1, 2):
        base_temp = 2
    elif month in (3, 4, 5):
        base_temp = 18
    else:
        base_temp = 20

    temp = base_temp + cond["temp_mod"] + rng.randint(-2, 2)
    humidity = 60 + rng.randint(-10, 20) + (15 if "雨" in cond["condition"] else 0)
    wind_kmh = rng.randint(5, 25) + (10 if "暴" in cond["condition"] else 0)
    uv = max(0, min(11, 8 - (2 if "雨" in cond["condition"] or "雪" in cond["condition"] else 0) + rng.randint(-2, 1)))

    # ── 逐小时天气 ──
    hourly = []
    for h in range(8, 22):
        if rng.random() < 0.15:
            sub = rng.choices(_WEATHER_CONDITIONS, weights=weights, k=1)[0]
        else:
            sub = cond
        hourly.append({
            "time": f"{h:02d}:00",
            "condition": sub["condition"],
            "temp": base_temp + sub["temp_mod"] + rng.randint(-1, 1),
        })

    # ── 活动建议 ──
    outdoor_friendly = cond["outdoor"]
    reason = ""
    suggestions = []
    transport_advice = ""
    walking_penalty = cond.get("walking_penalty", 1.0)

    if "暴" in cond["condition"]:
        reason = f"{cond['condition']}天气，户外活动存在安全风险"
        suggestions = ["建议取消户外活动", "如必须出行请选择打车", "步行路段需格外小心"]
        transport_advice = "极端天气，强烈建议打车出行，避免步行"
        walking_penalty = cond.get("walking_penalty", 0.3)
    elif "雨" in cond["condition"]:
        reason = f"{cond['condition']}将持续，路面湿滑"
        suggestions = ["建议带伞", "户外活动建议改期", "步行路段注意防滑"]
        transport_advice = "雨天路滑，建议减少步行路段"
    elif "雪" in cond["condition"]:
        reason = "降雪天气，路面可能结冰"
        suggestions = ["注意保暖", "步行注意防滑", "建议公共交通出行"]
        transport_advice = "雪天路滑，建议乘地铁出行"
    elif cond["condition"] == "晴":
        reason = "天气晴朗，适合户外活动"
        suggestions = ["注意防晒", "适合户外运动", "建议步行或骑行"]
        transport_advice = "天气晴好，步行舒适"

    traffic_risk = "高" if walking_penalty <= 0.4 else ("中等" if walking_penalty <= 0.7 else "低")

    alternative = "推荐室内活动：电影院/商场/咖啡馆" if not outdoor_friendly else None

    return {
        "status": "SUCCESS",
        "weather": {
            "condition": cond["condition"],
            "condition_en": cond["en"],
            "temperature_c": temp,
            "humidity": humidity,
            "wind_kmh": wind_kmh,
            "uv_index": uv,
            "hourly": hourly,
            "alert": cond.get("alert"),
        },
        "activity_advice": {
            "outdoor": "推荐" if outdoor_friendly else "不推荐",
            "reason": reason,
            "suggestions": suggestions,
            "alternative": alternative,
        },
        "transport_impact": {
            "walking_penalty": walking_penalty,
            "traffic_risk": traffic_risk,
            "advice": transport_advice,
        },
    }
