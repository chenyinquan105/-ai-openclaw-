"""
generic_poi_searcher.py —— 通用空间商户检索器
=================================================
物理契约 (API Contract):
  search_poi_matrix(center_coord: str, categories: list, radius_meters: int, min_rating: float) -> dict

本模块运行在安全隔离模式：使用本地 Mock 字典模拟高德/美团 POI 数据。
禁止调用任何真实三方 API。
"""

import math
import json
from typing import List

# ======================================================================
# Mock 数据库 —— 三里屯虚拟商户字典
# 硬编码 10+ 家不同品类、不同评分、不同经纬度的商户
# ======================================================================
_MOCK_POI_DB: List[dict] = [
    # --- hair (美发) ---
    {"shop_id": "shop_hair_01", "name": "沙宣三里屯店", "rating": 4.8, "lat": 39.932, "lng": 116.451, "category": "hair"},
    {"shop_id": "shop_hair_02", "name": "托尼形象设计", "rating": 4.6, "lat": 39.935, "lng": 116.448, "category": "hair"},
    {"shop_id": "shop_hair_03", "name": "丝颂烫染专门店", "rating": 4.0, "lat": 39.930, "lng": 116.455, "category": "hair"},
    {"shop_id": "shop_hair_04", "name": "木北造型工体店", "rating": 4.3, "lat": 39.938, "lng": 116.442, "category": "hair"},
    # --- pet (宠物) ---
    {"shop_id": "shop_pet_01", "name": "酷迪宠物三里屯店", "rating": 4.9, "lat": 39.931, "lng": 116.453, "category": "pet"},
    {"shop_id": "shop_pet_02", "name": "宠物家朝阳店", "rating": 4.5, "lat": 39.934, "lng": 116.446, "category": "pet"},
    {"shop_id": "shop_pet_03", "name": "爱派宠物生活馆", "rating": 4.0, "lat": 39.929, "lng": 116.460, "category": "pet"},
    # --- restaurant (餐饮) ---
    {"shop_id": "shop_rest_01", "name": "海底捞三里屯店", "rating": 4.7, "lat": 39.933, "lng": 116.450, "category": "restaurant"},
    {"shop_id": "shop_rest_02", "name": "鼎泰丰太古里店", "rating": 4.5, "lat": 39.936, "lng": 116.452, "category": "restaurant"},
    {"shop_id": "shop_rest_03", "name": "麦当劳三里屯站", "rating": 3.8, "lat": 39.937, "lng": 116.445, "category": "restaurant"},
    # --- cafe (咖啡) ---
    {"shop_id": "shop_cafe_01", "name": "星巴克臻选三里屯", "rating": 4.4, "lat": 39.932, "lng": 116.454, "category": "cafe"},
    {"shop_id": "shop_cafe_02", "name": "瑞幸咖啡三里屯店", "rating": 4.1, "lat": 39.934, "lng": 116.449, "category": "cafe"},
    # --- gym (健身) ---
    {"shop_id": "shop_gym_01", "name": "超级猩猩三里屯", "rating": 4.8, "lat": 39.931, "lng": 116.447, "category": "gym"},
    {"shop_id": "shop_gym_02", "name": "乐刻健身工体店", "rating": 4.2, "lat": 39.939, "lng": 116.443, "category": "gym"},
]


# ======================================================================
# 工具函数
# ======================================================================

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """返回两点之间的球面距离（单位：公里）。"""
    R = 6371.0  # 地球平均半径，km
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _parse_coord(center_coord: str) -> tuple:
    """解析 "lat,lng" 字符串为 (lat, lng) 浮点数元组。"""
    parts = center_coord.strip().split(",")
    if len(parts) != 2:
        raise ValueError(f"center_coord 格式必须为 'lat,lng'，实际收到: {center_coord!r}")
    try:
        lat = float(parts[0].strip())
        lng = float(parts[1].strip())
    except ValueError:
        raise ValueError(f"center_coord 中包含非法数值: {center_coord!r}")
    return lat, lng


# ======================================================================
# 核心主函数（物理契约入口）
# ======================================================================

def search_poi_matrix(
    center_coord: str,
    categories: List[str],
    radius_meters: int,
    min_rating: float,
) -> dict:
    """
    通用空间商户检索器。

    参数:
        center_coord (str): 中心点坐标 "lat,lng"，如 "39.93,116.45"
        categories   (list): 品类字符串数组，如 ["hair", "pet"]
        radius_meters (int): 搜索半径（米）
        min_rating  (float): 最低评分过滤

    返回:
        dict: 符合物理契约的出参字典，可直接 json.dumps。
    """
    # ---------- 入参合法性校验 ----------
    if not isinstance(center_coord, str) or not center_coord:
        return {"status": "ERROR", "message": "center_coord 必须是非空字符串"}
    if not isinstance(categories, list) or len(categories) == 0:
        return {"status": "ERROR", "message": "categories 必须是非空数组"}
    if not isinstance(radius_meters, int) or radius_meters <= 0:
        return {"status": "ERROR", "message": "radius_meters 必须是正整数"}
    if not isinstance(min_rating, (int, float)):
        return {"status": "ERROR", "message": "min_rating 必须是数字"}

    try:
        center_lat, center_lng = _parse_coord(center_coord)
    except ValueError as e:
        return {"status": "ERROR", "message": str(e)}

    # ---------- 核心过滤 ----------
    result_map: dict = {cat: [] for cat in categories}

    for shop in _MOCK_POI_DB:
        # 1) 品类过滤
        if shop["category"] not in categories:
            continue

        # 2) 评分过滤
        if shop["rating"] < min_rating:
            continue

        # 3) 距离过滤（球面 Haversine 公式）
        dist_km = _haversine_km(center_lat, center_lng, shop["lat"], shop["lng"])
        dist_m = dist_km * 1000.0
        if dist_m > radius_meters:
            continue

        # 构造出参条目
        entry = {
            "shop_id": shop["shop_id"],
            "name": shop["name"],
            "rating": shop["rating"],
            "coord": f"{shop['lat']},{shop['lng']}",
            "category": shop["category"],
        }
        result_map[shop["category"]].append(entry)

    return {
        "status": "SUCCESS",
        "search_results": result_map,
    }


# ======================================================================
# __main__ 简单自测入口（可选）
# ======================================================================
if __name__ == "__main__":
    demo_result = search_poi_matrix(
        center_coord="39.93,116.45",
        categories=["hair", "pet", "cafe"],
        radius_meters=1500,
        min_rating=4.0,
    )
    print(json.dumps(demo_result, ensure_ascii=False, indent=2))
