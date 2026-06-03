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
    # ===================================================================
    # 距离梯度 ~200m~500m（三里屯太古里核心区）
    # ===================================================================
    # --- hair (美发) 需人在场 ---
    {"shop_id": "shop_hair_01", "name": "沙宣三里屯店", "rating": 4.8, "lat": 39.934, "lng": 116.453, "category": "hair", "category_alias": "美发", "human_needed": True},
    {"shop_id": "shop_hair_02", "name": "托尼形象设计", "rating": 4.6, "lat": 39.935, "lng": 116.450, "category": "hair", "category_alias": "美发", "human_needed": True},
    # --- pet (宠物) 可丢下后台做 ---
    {"shop_id": "shop_pet_01", "name": "酷迪宠物三里屯店", "rating": 4.9, "lat": 39.933, "lng": 116.454, "category": "pet", "category_alias": "宠物店", "human_needed": False},
    {"shop_id": "shop_pet_02", "name": "宠物家朝阳店", "rating": 4.5, "lat": 39.936, "lng": 116.448, "category": "pet", "category_alias": "宠物店", "human_needed": False},
    # --- cafe (咖啡/水吧) 需人在场 ---
    {"shop_id": "shop_cafe_01", "name": "星巴克臻选三里屯", "rating": 4.4, "lat": 39.932, "lng": 116.455, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True},
    {"shop_id": "shop_cafe_05", "name": "瑞幸咖啡三里屯SOHO", "rating": 4.2, "lat": 39.933, "lng": 116.452, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True},
    {"shop_id": "shop_cafe_06", "name": "蜜雪冰城三里屯店", "rating": 4.0, "lat": 39.934, "lng": 116.451, "category": "cafe", "category_alias": "水吧", "human_needed": True},
    {"shop_id": "shop_cafe_07", "name": "茶百道三里屯店", "rating": 4.1, "lat": 39.935, "lng": 116.450, "category": "cafe", "category_alias": "水吧", "human_needed": True},
    # --- gym (健身) 需人在场 ---
    {"shop_id": "shop_gym_01", "name": "超级猩猩三里屯", "rating": 4.8, "lat": 39.932, "lng": 116.446, "category": "gym", "category_alias": "健身房", "human_needed": True},

    # ===================================================================
    # 距离梯度 ~500m~900m（工体北路 / 新东路沿线）
    # ===================================================================
    # --- hair (美发) 需人在场 ---
    {"shop_id": "shop_hair_03", "name": "丝颂烫染专门店", "rating": 4.0, "lat": 39.931, "lng": 116.452, "category": "hair", "category_alias": "美发", "human_needed": True},
    {"shop_id": "shop_hair_04", "name": "木北造型工体店", "rating": 4.3, "lat": 39.939, "lng": 116.446, "category": "hair", "category_alias": "美发", "human_needed": True},
    # --- pet (宠物) 可丢下后台做 ---
    {"shop_id": "shop_pet_03", "name": "爱派宠物生活馆", "rating": 4.0, "lat": 39.930, "lng": 116.458, "category": "pet", "category_alias": "宠物店", "human_needed": False},
    # --- restaurant (餐饮) 需人在场 ---
    {"shop_id": "shop_rest_01", "name": "海底捞三里屯店", "rating": 4.7, "lat": 39.936, "lng": 116.449, "category": "restaurant", "category_alias": "餐厅", "human_needed": True},
    {"shop_id": "shop_rest_02", "name": "鼎泰丰太古里店", "rating": 4.5, "lat": 39.937, "lng": 116.451, "category": "restaurant", "category_alias": "餐厅", "human_needed": True},
    {"shop_id": "shop_rest_03", "name": "麦当劳三里屯站", "rating": 3.8, "lat": 39.938, "lng": 116.443, "category": "restaurant", "category_alias": "餐厅", "human_needed": True},
    # --- cafe (咖啡) 需人在场 ---
    {"shop_id": "shop_cafe_02", "name": "瑞幸咖啡三里屯店", "rating": 4.1, "lat": 39.935, "lng": 116.447, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True},
    # --- gym (健身) 需人在场 ---
    {"shop_id": "shop_gym_02", "name": "乐刻健身工体店", "rating": 4.2, "lat": 39.940, "lng": 116.441, "category": "gym", "category_alias": "健身房", "human_needed": True},

    # ===================================================================
    # 距离梯度 ~1000m（东四十条桥 / 工体北路北段）
    # ===================================================================
    {"shop_id": "shop_hair_05", "name": "东田造型东四十条店", "rating": 4.5, "lat": 39.942, "lng": 116.444, "category": "hair", "category_alias": "美发", "human_needed": True},
    {"shop_id": "shop_rest_04", "name": "金鼎轩东直门店", "rating": 4.3, "lat": 39.943, "lng": 116.441, "category": "restaurant", "category_alias": "餐厅", "human_needed": True},
    {"shop_id": "shop_japanese_01", "name": "鮨然日料居酒屋", "rating": 4.7, "lat": 39.942, "lng": 116.442, "category": "japanese", "category_alias": "日料", "human_needed": True},
    {"shop_id": "shop_hotpot_01", "name": "楠火锅工体店", "rating": 4.6, "lat": 39.941, "lng": 116.443, "category": "hotpot", "category_alias": "火锅", "human_needed": True},
    # --- laundry (干洗) 可丢下后台做 ---
    {"shop_id": "shop_laundry_01", "name": "福奈特洗衣东四十条店", "rating": 4.5, "lat": 39.941, "lng": 116.442, "category": "laundry", "category_alias": "干洗店", "human_needed": False},

    # ===================================================================
    # 距离梯度 ~2000m（东直门外大街 / 东直门地铁站周边）
    # ===================================================================
    {"shop_id": "shop_pet_04", "name": "宠物家东直门店", "rating": 4.2, "lat": 39.948, "lng": 116.432, "category": "pet", "category_alias": "宠物店", "human_needed": False},
    {"shop_id": "shop_cafe_03", "name": "皮爷咖啡东直门店", "rating": 4.5, "lat": 39.947, "lng": 116.431, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True},
    {"shop_id": "shop_gym_03", "name": "乐刻健身东直门店", "rating": 4.0, "lat": 39.946, "lng": 116.433, "category": "gym", "category_alias": "健身房", "human_needed": True},
    {"shop_id": "shop_japanese_02", "name": "鸟贵族烧鸟三里屯店", "rating": 4.4, "lat": 39.948, "lng": 116.430, "category": "japanese", "category_alias": "日料", "human_needed": True},
    {"shop_id": "shop_cinema_01", "name": "保利国际影城朝阳", "rating": 4.6, "lat": 39.947, "lng": 116.434, "category": "cinema", "category_alias": "电影院", "human_needed": True},

    # ===================================================================
    # 距离梯度 ~3000m（雍和宫 / 安定门 / 簋街周边）
    # ===================================================================
    {"shop_id": "shop_rest_05", "name": "花家怡园簋街店", "rating": 4.6, "lat": 39.951, "lng": 116.428, "category": "restaurant", "category_alias": "餐厅", "human_needed": True},
    {"shop_id": "shop_cafe_04", "name": "Manner咖啡雍和宫", "rating": 4.3, "lat": 39.950, "lng": 116.426, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True},
    {"shop_id": "shop_hotpot_02", "name": "卤校长老火锅三里屯店", "rating": 4.5, "lat": 39.950, "lng": 116.429, "category": "hotpot", "category_alias": "火锅", "human_needed": True},
    {"shop_id": "shop_cinema_02", "name": "万达影城CBD店", "rating": 4.7, "lat": 39.951, "lng": 116.432, "category": "cinema", "category_alias": "电影院", "human_needed": True},
    # --- laundry (干洗) 可丢下后台做 ---
    {"shop_id": "shop_laundry_02", "name": "象王洗染东直门店", "rating": 4.3, "lat": 39.949, "lng": 116.431, "category": "laundry", "category_alias": "干洗店", "human_needed": False},
    {"shop_id": "shop_hair_06", "name": "文峰美发雍和宫店", "rating": 3.9, "lat": 39.951, "lng": 116.426, "category": "hair", "category_alias": "美发", "human_needed": True},
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
        # 1) 品类过滤（支持 category 英文编码 + category_alias 中文别名双匹配）
        shop_cats = [shop["category"], shop.get("category_alias", "")]
        matched_cat = next((c for c in categories if c in shop_cats), None)
        if matched_cat is None:
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
            "category_alias": shop.get("category_alias", ""),
            "human_needed": shop.get("human_needed", True),
        }
        result_map[matched_cat].append(entry)

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
