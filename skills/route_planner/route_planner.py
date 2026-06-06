"""
route_planner — 多节点路径规划 Skill

遵循 OpenClaw Skill 契约：单一入口函数，返回标准化 JSON。
"""
from math import radians, cos, sin, asin, sqrt
from typing import List, Dict, Optional, Any


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine 球面距离，返回米"""
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    c = 2 * asin(sqrt(a))
    return R * c


def _parse_coord(coord_str: str):
    """解析 "lat,lng" → (float, float)"""
    parts = coord_str.strip().split(",")
    if len(parts) != 2:
        raise ValueError(f"坐标格式无效: {coord_str}")
    return float(parts[0].strip()), float(parts[1].strip())


def _weather_multiplier(weather: Optional[str]) -> float:
    """天气对步行容忍距离的修正系数"""
    if not weather:
        return 1.0
    w = weather.lower()
    if "暴雨" in w or "台风" in w:
        return 0.3
    if "大雨" in w or "大雪" in w:
        return 0.5
    if "小雨" in w or "雪" in w:
        return 0.8
    return 1.0


def _transport_speed(mode: str) -> float:
    """交通方式 → 米/分钟"""
    return {"步行": 80, "打车": 400, "地铁": 300, "公交": 180}.get(mode, 80)


def plan_route(
    start_coord: str,
    waypoints: List[Dict[str, Any]],
    transport_preference: str = "步行优先",
    walking_tolerance_meters: int = 800,
    weather_condition: str = None,
) -> dict:
    """
    多节点路径规划入口。

    参数:
        start_coord: 出发坐标 "lat,lng"
        waypoints: 途经点列表 [{id, name, coord, duration_minutes}, ...]
        transport_preference: "步行优先"/"打车优先"/"地铁优先"
        walking_tolerance_meters: 步行容忍距离（米）
        weather_condition: 天气状况字符串

    返回:
        dict: 路径规划结果
    """
    # ── 入参校验 ──
    if not waypoints:
        return {"status": "ERROR", "message": "无途经点"}

    try:
        prev_lat, prev_lng = _parse_coord(start_coord)
    except ValueError as e:
        return {"status": "ERROR", "message": str(e)}

    # 解析所有途经点坐标
    parsed = []
    for wp in waypoints:
        try:
            lat, lng = _parse_coord(wp.get("coord", ""))
        except ValueError:
            return {"status": "ERROR", "message": f"途经点 {wp.get('id','?')} 坐标无效"}
        parsed.append({
            **wp,
            "lat": lat,
            "lng": lng,
        })

    # ── 最近邻贪心排序 ──
    weather_mult = _weather_multiplier(weather_condition)
    effective_tolerance = walking_tolerance_meters * weather_mult
    remaining = parsed[:]
    ordered = []

    while remaining:
        # 找距离当前坐标最近的节点
        best_idx = 0
        best_dist = float("inf")
        for i, wp in enumerate(remaining):
            d = _haversine_m(prev_lat, prev_lng, wp["lat"], wp["lng"])
            if d < best_dist:
                best_dist = d
                best_idx = i
        chosen = remaining.pop(best_idx)
        ordered.append((chosen, best_dist))
        prev_lat, prev_lng = chosen["lat"], chosen["lng"]

    # ── 构建路径（含交通模式判定） ──
    route = []
    total_travel_min = 0
    total_activity_min = 0
    total_dist = 0.0
    alerts = []
    prev_name = "起点"
    prev_lat, prev_lng = _parse_coord(start_coord)

    for i, (wp, dist) in enumerate(ordered):
        # 交通模式判定
        if dist <= effective_tolerance and transport_preference != "打车优先":
            mode = "步行"
        elif transport_preference in ("打车优先", "地铁优先"):
            mode = transport_preference.replace("优先", "")
        else:
            mode = "打车"

        speed = _transport_speed(mode)
        travel_min = int(dist / speed) + 1  # 至少 1 分钟

        total_dist += dist
        total_travel_min += travel_min
        total_activity_min += wp.get("duration_minutes", 30)

        if mode == "打车" and dist <= effective_tolerance:
            alerts.append(
                f"{prev_name}→{wp['name']} {int(dist)}m 在容忍范围内({int(effective_tolerance)}m)，"
                f"但按 '打车优先' 偏好选择打车"
            )
        elif mode == "打车" and dist > effective_tolerance:
            alerts.append(
                f"{prev_name}→{wp['name']} {int(dist)}m 超出步行容忍"
                f"({int(effective_tolerance)}m)，建议打车"
            )

        route.append({
            "order": i,
            "from": prev_name,
            "to": wp["name"],
            "to_coord": wp["coord"],
            "transport_mode": mode,
            "distance_meters": int(dist),
            "duration_minutes": travel_min,
            "activity": {
                "name": wp.get("name", ""),
                "duration_minutes": wp.get("duration_minutes", 30),
            },
        })
        prev_name = wp["name"]

    return {
        "status": "SUCCESS",
        "route": route,
        "total_distance_meters": int(total_dist),
        "total_travel_minutes": total_travel_min,
        "total_activity_minutes": total_activity_min,
        "weather_applied": weather_condition,
        "effective_walking_tolerance_meters": int(effective_tolerance),
        "alerts": alerts,
    }
