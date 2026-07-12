"""
multi_day_scheduler.py —— 多日行程智能排程引擎
================================================
借鉴 x81k25/route-optimization 的 KMeans+贪心均衡+2-opt 模式，
以及 Google OR-Tools "单一时间轴+夜间屏蔽"概念，
实现 5 阶段流水线：
  1. 地理聚类 (KMeans++)
  2. 负载均衡 (贪心重分配)
  3. 每日 TSP (最近邻 + 2-opt)
  4. 用餐插入 (午餐/晚餐窗口)
  5. 全局微调 (跨天边界交换)

入口函数: solve_multi_day(candidate_shops, num_days, checkin_lat, checkin_lng,
                         transport_preference, start_time_str, max_hours_per_day)
"""

import math
import sys
import os
import random
import copy

# 导入惩罚函数模块（批次二：软约束模型）
try:
    from scheduling_penalty import (meal_time_penalty, SKIP_PENALTY_BASE,
                                      LAMBDA_TRAVEL, FATIGUE_COEFFICIENT, LAMBDA_FATIGUE,
                                      dynamic_fatigue_cost)
except ImportError:
    # 回退：如果模块不存在，使用默认值（保持向后兼容）
    def meal_time_penalty(meal_type, proposed_minutes):
        return 0.0
    def dynamic_fatigue_cost(timeline, day_index=0, prev_day_fatigue=0):
        return 0.0
    SKIP_PENALTY_BASE = 200
    LAMBDA_TRAVEL = 0.3
    FATIGUE_COEFFICIENT = {"default": 0.8}
    LAMBDA_FATIGUE = 0.5

# ======================================================================
# Haversine 距离计算（与 route_planner 保持一致）
# ======================================================================

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """计算两点间的地球表面距离（米）"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ======================================================================
# 交通速度（米/分钟）
# ======================================================================

TRANSPORT_SPEEDS = {
    "步行": 83.3,      # 5 km/h
    "步行优先": 83.3,
    "地铁": 500,       # 30 km/h
    "地铁优先": 500,
    "公交": 333,       # 20 km/h
    "打车": 667,       # 40 km/h
    "打车优先": 667,
    "驾车": 667,
    "驾车优先": 667,
}


# ======================================================================
# 中国主要机场/火车站坐标字典（用于门到门交通时间计算）
# ======================================================================

STATION_COORDS = {
    # 北京
    "北京大兴国际机场": (39.509, 116.410),
    "大兴机场": (39.509, 116.410),
    "北京首都国际机场": (40.080, 116.584),
    "首都机场": (40.080, 116.584),
    "北京南站": (39.865, 116.379),
    "北京西站": (39.895, 116.322),
    "北京站": (39.904, 116.428),
    "北京北站": (39.945, 116.349),
    "北京朝阳站": (39.922, 116.486),
    "北京丰台站": (39.848, 116.310),
    "北京清河站": (40.043, 116.338),
    # 上海
    "上海虹桥国际机场": (31.198, 121.336),
    "虹桥机场": (31.198, 121.336),
    "上海浦东国际机场": (31.144, 121.808),
    "浦东机场": (31.144, 121.808),
    "上海虹桥站": (31.196, 121.323),
    "上海站": (31.252, 121.456),
    "上海南站": (31.155, 121.429),
    # 广州
    "广州白云国际机场": (23.392, 113.299),
    "白云机场": (23.392, 113.299),
    "广州南站": (22.991, 113.270),
    "广州东站": (23.152, 113.325),
    "广州站": (23.135, 113.257),
    # 深圳
    "深圳宝安国际机场": (22.639, 113.815),
    "宝安机场": (22.639, 113.815),
    "深圳北站": (22.611, 114.030),
    "深圳站": (22.539, 114.119),
    # 成都
    "成都天府国际机场": (30.320, 104.442),
    "天府机场": (30.320, 104.442),
    "成都双流国际机场": (30.579, 103.947),
    "双流机场": (30.579, 103.947),
    "成都东站": (30.630, 104.141),
    "成都站": (30.699, 104.075),
    "成都南站": (30.613, 104.074),
    # 杭州
    "杭州萧山国际机场": (30.236, 120.428),
    "萧山机场": (30.236, 120.428),
    "杭州东站": (30.293, 120.213),
    "杭州站": (30.246, 120.181),
    # 重庆
    "重庆江北国际机场": (29.719, 106.642),
    "江北机场": (29.719, 106.642),
    "重庆北站": (29.612, 106.549),
    "重庆西站": (29.504, 106.433),
    # 武汉
    "武汉天河国际机场": (30.784, 114.208),
    "天河机场": (30.784, 114.208),
    "武汉站": (30.607, 114.426),
    # 南京
    "南京禄口国际机场": (31.742, 118.862),
    "禄口机场": (31.742, 118.862),
    "南京南站": (31.971, 118.798),
    "南京站": (32.089, 118.796),
    # 西安
    "西安咸阳国际机场": (34.441, 108.751),
    "咸阳机场": (34.441, 108.751),
    "西安北站": (34.377, 108.935),
    "西安站": (34.279, 108.959),
    # 昆明
    "昆明长水国际机场": (25.102, 102.929),
    "长水机场": (25.102, 102.929),
    "昆明南站": (24.875, 102.863),
    # 厦门
    "厦门高崎国际机场": (24.545, 118.128),
    "高崎机场": (24.545, 118.128),
    "厦门北站": (24.641, 118.069),
    # 三亚
    "三亚凤凰国际机场": (18.303, 109.412),
    "凤凰机场": (18.303, 109.412),
    # 长沙
    "长沙黄花国际机场": (28.190, 113.220),
    "黄花机场": (28.190, 113.220),
    "长沙南站": (28.150, 113.065),
    # 青岛
    "青岛胶东国际机场": (36.362, 120.090),
    "胶东机场": (36.362, 120.090),
    "青岛站": (36.066, 120.314),
    "青岛北站": (36.169, 120.372),
    # 大连
    "大连周水子国际机场": (38.966, 121.539),
    "周水子机场": (38.966, 121.539),
    # 天津
    "天津滨海国际机场": (39.124, 117.346),
    "滨海机场": (39.124, 117.346),
    "天津站": (39.136, 117.210),
    "天津西站": (39.158, 117.165),
    # 苏州
    "苏州站": (31.333, 120.610),
    "苏州北站": (31.424, 120.646),
    # 郑州
    "郑州新郑国际机场": (34.520, 113.841),
    "新郑机场": (34.520, 113.841),
    "郑州东站": (34.759, 113.774),
    # 哈尔滨
    "哈尔滨太平国际机场": (45.624, 126.251),
    "太平机场": (45.624, 126.251),
}


# ======================================================================
# 门到门出行时间常量（统一用于飞机/高铁/火车）
# ======================================================================
HOME_TO_STATION_MIN = 90       # 家↔站点（高铁站/机场）默认交通时间（分钟）
STATION_SECURITY_BUFFER = 60   # 进站安检 + 预防堵车缓冲时间（分钟）
TOTAL_ADVANCE_MIN = 150        # 总提前量：HOME_TO_STATION(90) + SECURITY_BUFFER(60)
MORNING_PREP_MIN = 45          # 起床后洗漱/收拾/早餐时间（分钟）


def _lookup_station_coord(station_name: str):
    """根据站点名称查找坐标。支持模糊匹配（名称包含关键词即可）。

    返回: (lat, lng) 或 None
    """
    if not station_name:
        return None
    # 精确匹配
    if station_name in STATION_COORDS:
        return STATION_COORDS[station_name]
    # 模糊匹配：站点名称包含关键词
    for name, coord in STATION_COORDS.items():
        if name in station_name or station_name in name:
            return coord
    return None


def _get_speed(transport: str) -> float:
    """获取交通速度（米/分钟）"""
    for key, speed in TRANSPORT_SPEEDS.items():
        if key in transport:
            return speed
    return 83.3  # 默认步行


def _region_cohesion_guard(dist_m: float, weather: dict = None, preference: dict = None) -> dict:
    """
    路网多模态决策：根据距离、天气、用户偏好推荐最优出行方式。

    决策分层：
    1. 距离阈值：极短<200m 步行不可替代 → 中等距离按天气/偏好选择 → 长距离驾车
    2. 天气影响：恶劣天气（walking_penalty<0.4）强制驾车，一般坏天气避免步行
    3. 用户偏好：walking_tolerance_meters 扩大步行范围，prefer_transport 覆盖决策

    Args:
        dist_m: 两点间直线距离（米）
        weather: 天气 dict，含 walking_penalty (0.0-1.0) 和 condition 描述
        preference: 用户偏好 dict，含 walking_tolerance_meters, prefer_transport

    Returns:
        dict: {
            "transport": 推荐出行方式（"步行优先"/"地铁优先"/"驾车优先"）,
            "warning": 天气/距离警告或 None,
            "reason": 决策理由（可读文本）,
            "weather_penalty": 天气惩罚因子（默认 1.0）,
        }
    """
    # ── 天气因子提取 ──
    weather_penalty = 1.0
    weather_bad = False
    weather_severe = False
    if weather:
        weather_penalty = float(weather.get("walking_penalty", 1.0))
        weather_bad = weather_penalty < 0.7
        weather_severe = weather_penalty < 0.4

    # ── 用户偏好提取 ──
    walking_tolerance = 1000  # 默认步行容忍距离（米）
    prefer_transport = None
    if preference:
        walking_tolerance = int(preference.get("walking_tolerance_meters", walking_tolerance))
        prefer_transport = preference.get("prefer_transport")

    # ── 距离分层决策 ──
    warning = None
    reason_parts = []

    # 极短距离：步行不可替代（即使天气差也走几步就到了）
    if dist_m <= 200:
        transport = "步行优先"
        reason_parts.append(f"距离极短 ({dist_m:.0f}m)，步行不可替代")
        if weather_severe:
            warning = "天气恶劣，但距离极短仍建议步行"

    # 短距离（在步行容忍范围内）：天气好→步行，天气差→地铁
    elif dist_m <= walking_tolerance:
        if not weather_severe:
            transport = "步行优先"
            reason_parts.append(f"距离 {dist_m:.0f}m 在步行容忍范围 ({walking_tolerance}m) 内")
        else:
            transport = "驾车优先"
            reason_parts.append(f"距离 {dist_m:.0f}m 但天气恶劣，调整为驾车")
            warning = "恶劣天气预警，建议驾车出行"

    # 中等距离 1（步行容忍~3000m）：天气好→步行，坏→地铁，恶劣→驾车
    elif dist_m <= 3000:
        if not weather_bad:
            transport = "步行优先"
            reason_parts.append(f"距离 {dist_m:.0f}m 适中，天气可接受")
        elif weather_severe:
            transport = "驾车优先"
            reason_parts.append(f"距离 {dist_m:.0f}m + 恶劣天气 → 驾车")
            warning = "恶劣天气预警，建议驾车出行"
        else:
            transport = "地铁优先"
            reason_parts.append(f"距离 {dist_m:.0f}m 适中但天气不佳，推荐地铁")
            warning = "天气影响，建议优先地铁出行"

    # 中等距离 2（3-8km）：地铁优先（除非恶劣天气→驾车）
    elif dist_m <= 8000:
        if weather_severe:
            transport = "驾车优先"
            reason_parts.append(f"距离 {dist_m:.0f}m + 恶劣天气 → 驾车")
            warning = "恶劣天气预警，建议驾车出行"
        else:
            transport = "地铁优先"
            reason_parts.append(f"距离 {dist_m:.0f}m，推荐地铁")

    # 长距离（>8km）：驾车优先
    else:
        transport = "驾车优先"
        reason_parts.append(f"距离 {dist_m:.0f}m 较远，推荐驾车")

    # ── 用户偏好覆盖 ──
    if prefer_transport:
        reason_parts.append(f"（用户偏好覆盖: {prefer_transport}）")
        transport = prefer_transport

    return {
        "transport": transport,
        "warning": warning,
        "reason": "；".join(reason_parts),
        "weather_penalty": weather_penalty,
    }


# ======================================================================
# 品类默认时长（分钟）
# ======================================================================

CATEGORY_DURATIONS = {
    "scenic": 180,
    "restaurant": 60,
    "hotpot": 90,
    "japanese": 60,
    "cafe": 30,
    "shopping": 90,
    "cinema": 120,
    "hair": 60,
    "pet": 30,
    "laundry": 30,
    "gym": 60,
    "hotel": 480,
    "breakfast": 45,
}

MEAL_CATEGORIES = {"restaurant", "hotpot", "japanese", "cafe", "breakfast"}
MAIN_MEAL_CATEGORIES = {"restaurant", "hotpot", "japanese", "food", "dining", "buffet", "barbecue"}  # 正餐：只排午/晚餐，不进入VISIT循环
SNACK_CATEGORIES = {"cafe"}  # 小吃/饮品：可作VISIT节点，但需间隔≥90min

# 早餐类店铺名称关键词（用于识别 category 被标为 restaurant 但实际经营早餐的店铺）
BREAKFAST_NAME_KEYWORDS = [
    "早餐", "早点", "早饭", "早茶", "豆浆", "油条", "包子", "粥", "煎饼",
    "豆腐脑", "馄饨", "烧饼", "小笼包", "面馆", "豆花", "肠粉", "馒头",
    "蒸饺", "锅贴", "粢饭", "麻团", "糖饼", "炸糕", "豆汁", "焦圈",
    "鸡蛋灌饼", "肉夹馍", "手抓饼", "葱油饼", "生煎", "汤包", "抄手",
]


def _get_duration(category: str, shop: dict = None, dynamic_durations: dict = None) -> int:
    """获取 POI 的游玩/停留时长（分钟）。

    优先级：
    1. shop['duration_minutes']（LLM 个体估算，已在 server 层注入）
    2. dynamic_durations[shop_id]（外部传入的时长字典回退）
    3. CATEGORY_DURATIONS 品类默认值
    """
    # 优先用 shop 自身的 duration_minutes（最精确，可能是 LLM 估算的结果）
    if shop:
        if "duration_minutes" in shop and shop["duration_minutes"] is not None:
            return int(shop["duration_minutes"])
        # 回退：外部 dynamic_durations 字典
        if dynamic_durations and shop.get("shop_id") in dynamic_durations:
            return int(dynamic_durations[shop["shop_id"]])
    return CATEGORY_DURATIONS.get(category, 60)


def _ensure_coords(shops: list, arrival_lat: float = None, arrival_lng: float = None,
                   geocode_callback=None):
    """确保所有 shop 都有 lat/lng（Never-Crash 兜底防御）。

    优先级：
    1. shop 已有 lat/lng → 不变
    2. shop 有 coord 字符串 → 解析补全
    3. 有 geocode_callback → 调用 API 查询（名称+地址→坐标）
    4. 全缺失 → 用到达站点坐标兜底，标记 is_imputed=True

    geocode_callback 签名: (name: str, address: str) -> (lat: float, lng: float) | None
    """
    # 兜底坐标：用第一个有效 shop 的坐标或 (0,0)
    fallback_lat = arrival_lat or 0.0
    fallback_lng = arrival_lng or 0.0
    # 尝试从 shop 中找到更合理的兜底坐标
    if fallback_lat == 0.0 and fallback_lng == 0.0:
        for s in shops:
            lat = s.get("lat")
            lng = s.get("lng")
            if lat is not None and lng is not None:
                try:
                    fallback_lat = float(lat)
                    fallback_lng = float(lng)
                    break
                except (ValueError, TypeError):
                    continue

    for s in shops:
        has_lat = "lat" in s and s["lat"] is not None
        has_lng = "lng" in s and s["lng"] is not None

        if has_lat and has_lng:
            continue  # 已有坐标，跳过

        # 尝试从 coord 字符串解析
        coord = s.get("coord", "")
        parsed = False
        if coord and isinstance(coord, str) and "," in coord:
            parts = coord.split(",")
            try:
                s["lat"] = float(parts[0].strip())
                s["lng"] = float(parts[1].strip())
                parsed = True
            except (ValueError, TypeError):
                pass

        if not parsed:
            # ── 调用 geocode_callback 尝试 API 查询 ──
            geocoded = False
            if geocode_callback is not None:
                try:
                    name = s.get("name", "")
                    address = s.get("address", "")
                    result = geocode_callback(name, address)
                    if result and len(result) == 2:
                        s["lat"] = float(result[0])
                        s["lng"] = float(result[1])
                        s["is_geocoded"] = True
                        geocoded = True
                except Exception:
                    pass  # API 调用失败，继续兜底

            if not geocoded:
                # 兜底：用到达站点坐标，打上 is_imputed 标签
                s["lat"] = fallback_lat
                s["lng"] = fallback_lng
                s["is_imputed"] = True


# ======================================================================
# 阶段 0.5: 餐饮前置就近绑定与 20km 强拦截
# ======================================================================

MEAL_BINDING_MAX_DISTANCE_KM = 20.0  # 餐厅绑定到 POI 的最大距离（公里）


def _pre_bind_meals_and_filter(shops: list) -> tuple:
    """餐饮前置就近绑定 + 极端大跨度拦截。

    在聚类之前调用。将所有餐饮类店铺（restaurant/hotpot/cafe 等）过滤出来：
    - 对每家餐厅，计算到所有非餐饮 POI 的 Haversine 距离
    - 若最近距离 ≤ 20km：将餐厅作为"子挂件"注册到该 POI 的 bound_meals 列表
    - 若最近距离 > 20km：移入 pending_user_confirmation_meals 隔离区

    返回:
        (poi_shops: list, pending_meals: list)
        - poi_shops: 非餐饮 POI（带 bound_meals 字段）+ 无目标 POI 可绑的餐厅（原样保留）
        - pending_meals: 距离任何 POI 均 >20km 的餐厅，留给 Agent 与用户协商
    """
    # 分离餐饮与非餐饮
    meal_shops = []
    poi_shops = []
    for s in shops:
        cat = s.get("category", "")
        if cat in MAIN_MEAL_CATEGORIES or cat in {"cafe", "breakfast"}:
            meal_shops.append(s)
        else:
            poi_shops.append(s)

    if not meal_shops:
        return list(shops), []

    if not poi_shops:
        # 全是餐饮（极端情况）：全部保留，不绑定
        return list(shops), []

    # 初始化 POI 的 bound_meals
    for p in poi_shops:
        if "bound_meals" not in p:
            p["bound_meals"] = []

    pending_meals = []
    max_dist_m = MEAL_BINDING_MAX_DISTANCE_KM * 1000  # 20km → 米

    for meal in meal_shops:
        mlat = float(meal.get("lat", 0))
        mlng = float(meal.get("lng", 0))
        best_poi = None
        best_dist = float("inf")

        for poi in poi_shops:
            plat = float(poi.get("lat", 0))
            plng = float(poi.get("lng", 0))
            d = _haversine_m(mlat, mlng, plat, plng)
            if d < best_dist:
                best_dist = d
                best_poi = poi

        if best_poi is not None and best_dist <= max_dist_m:
            # 策略 A: 顺路绑定
            meal_copy = dict(meal)
            meal_copy["_bind_distance_m"] = round(best_dist)
            best_poi["bound_meals"].append(meal_copy)
        else:
            # 策略 B: 极端大跨度拦截
            meal_copy = dict(meal)
            meal_copy["reason"] = "distance_exceeds_20km"
            meal_copy["_min_distance_km"] = round(best_dist / 1000, 1) if best_dist < float("inf") else None
            pending_meals.append(meal_copy)

    # 对每个 POI 的 bound_meals 按距离排序（近的在前）
    for p in poi_shops:
        if p.get("bound_meals"):
            p["bound_meals"].sort(key=lambda m: m.get("_bind_distance_m", float("inf")))

    return poi_shops, pending_meals


# ======================================================================
# 阶段 1: 地理聚类 —— KMeans++
# ======================================================================

def _cluster_by_geo(shops: list, k: int) -> list:
    """
    将店铺按地理位置聚为 k 组。
    优先使用 scipy.cluster.vq.kmeans2(minit='++')；
    scipy 不可用时回退到纯 Python Lloyd 算法。

    返回: [[shop_dict, ...], ...] 每组一个列表
    """
    if k <= 0:
        k = 1
    if len(shops) <= k:
        # 店铺数 ≤ 天数：每天至少一个
        result = [[] for _ in range(k)]
        for i, s in enumerate(shops):
            result[i % k].append(s)
        return result

    # 提取坐标
    coords = []
    for s in shops:
        lat = float(s.get("lat", 0))
        lng = float(s.get("lng", 0))
        coords.append([lng, lat])  # 原始经纬度（回退路径使用）

    # 转换为近似米制坐标（与 Haversine 距离度量一致）
    # equirectangular 投影：城市级范围内精度足够
    if coords:
        mean_lat = math.radians(sum(c[1] for c in coords) / len(coords))
        cos_lat = math.cos(mean_lat)
        meter_coords = []
        for lng, lat in coords:
            x = lng * cos_lat * 111320.0  # 经度 → 米
            y = lat * 111320.0            # 纬度 → 米
            meter_coords.append([x, y])
    else:
        meter_coords = coords

    # 尝试 scipy（使用米制坐标，欧几里得距离 ≈ Haversine）
    try:
        from scipy.cluster.vq import kmeans2
        centroids, labels = kmeans2(meter_coords, k, minit='++', missing='raise')
        clusters = [[] for _ in range(k)]
        for i, label in enumerate(labels):
            clusters[int(label)].append(shops[i])
        return clusters
    except ImportError:
        pass
    except Exception as e:
        print(f"[multi_day_scheduler] scipy kmeans 失败: {e}，回退纯 Python", flush=True)

    # 纯 Python KMeans 回退（Lloyd 算法，使用 Haversine 距离）
    return _simple_kmeans(shops, coords, k)


def _simple_kmeans(shops: list, coords: list, k: int, max_iter: int = 100) -> list:
    """纯 Python Lloyd KMeans，使用 Haversine 距离"""
    n = len(coords)

    # 随机初始化质心（均匀采样）
    step = max(1, n // k)
    centroids = [coords[i * step] for i in range(k)]

    for iteration in range(max_iter):
        # 分配：每个点到最近的质心
        clusters = [[] for _ in range(k)]
        for i, (lng, lat) in enumerate(coords):
            best_c = 0
            best_d = float("inf")
            for c_idx, (clng, clat) in enumerate(centroids):
                d = _haversine_m(lat, lng, clat, clng)
                if d < best_d:
                    best_d = d
                    best_c = c_idx
            clusters[best_c].append(i)

        # 更新质心
        new_centroids = []
        for cluster in clusters:
            if not cluster:
                # 空簇：保持旧质心
                new_centroids.append(centroids[len(new_centroids)])
                continue
            avg_lng = sum(coords[i][0] for i in cluster) / len(cluster)
            avg_lat = sum(coords[i][1] for i in cluster) / len(cluster)
            new_centroids.append([avg_lng, avg_lat])

        # 检查收敛
        max_shift = 0
        for c_old, c_new in zip(centroids, new_centroids):
            shift = _haversine_m(c_old[1], c_old[0], c_new[1], c_new[0])
            max_shift = max(max_shift, shift)
        if max_shift < 10:  # 收敛阈值 10 米
            break
        centroids = new_centroids

    # 最终分配
    result = [[] for _ in range(k)]
    for i, (lng, lat) in enumerate(coords):
        best_c = 0
        best_d = float("inf")
        for c_idx, (clng, clat) in enumerate(centroids):
            d = _haversine_m(lat, lng, clat, clng)
            if d < best_d:
                best_d = d
                best_c = c_idx
        result[best_c].append(shops[i])

    return result


# ======================================================================
# 阶段 2: 负载均衡 —— 贪心重分配
# ======================================================================

def _balance_clusters(clusters: list, max_hours_per_day: float = 8.0, max_scenic_per_day: int = 5,
                      transport_preference: str = "驾车优先",
                      day_hours: list = None) -> list:
    """
    增强贪心迭代重分配，使每天总时间接近目标，且每天 POI 数不超标。

    相比旧版的增强：
    1. 边际插入成本：用 TSP 路径增量代替纯地理距离评分
    2. 疲劳度感知：优先从超载天移出高疲劳店铺，降低当天体力负担
    3. 多米诺级联：scenic 上限强约束时触发级联滚动，避免局部最优

    三遍处理：
    第一遍：时间均衡（目标: 每天活动 + 旅行时间在目标时间的 +-15% 内）。
    第二遍前半：数量均衡（各天 scenic 数量差距 <= 1）。
    第二遍后半：数量上限（每天 scenic <= max_scenic_per_day，含多米诺级联）。

    参数:
        day_hours: 每天的有效可用小时数列表（如 Day1 晚到仅 4.5h，DayN 早走仅 5h）。
                   为 None 时所有天使用统一的 max_hours_per_day。
    """
    # 每日目标分钟数（支持逐天差异化）
    if day_hours and len(day_hours) == len(clusters):
        day_target_minutes = [max(h, 1.0) * 60 for h in day_hours]  # 最少 1h
    else:
        day_target_minutes = [max_hours_per_day * 60] * len(clusters)
    target_minutes = max_hours_per_day * 60  # 保留兼容旧逻辑的默认值
    max_iter = 50

    # ── 第一遍：时间均衡（增强：疲劳度感知 + 边际成本参考）──
    for _ in range(max_iter):
        # 计算每天预估总时间（正餐每天最多计入2家=1午+1晚）
        day_times = []
        day_deviations = []  # 偏离各自目标的比例
        for i, cluster in enumerate(clusters):
            meal_count = 0
            total = 0
            for s in cluster:
                cat = s.get("category", "")
                if cat in MAIN_MEAL_CATEGORIES:
                    meal_count += 1
                    if meal_count <= 2:
                        total += _get_duration(cat, s)  # 最多计入2家正餐（1午+1晚）
                else:
                    total += _get_duration(cat, s)
            # 粗略估计旅行时间 = 点数 × 15min
            total += len(cluster) * 15
            day_times.append(total)
            tgt = day_target_minutes[i] if i < len(day_target_minutes) else target_minutes
            day_deviations.append(total - tgt)

        # 找最超载和最轻载的天（基于各自目标的偏差）
        overloaded_idx = max(range(len(day_deviations)), key=lambda i: day_deviations[i])
        underloaded_idx = min(range(len(day_deviations)), key=lambda i: day_deviations[i])

        overloaded_time = day_times[overloaded_idx]
        underloaded_time = day_times[underloaded_idx]
        ol_target = day_target_minutes[overloaded_idx] if overloaded_idx < len(day_target_minutes) else target_minutes
        ul_target = day_target_minutes[underloaded_idx] if underloaded_idx < len(day_target_minutes) else target_minutes

        # 都在容忍范围内 → 停止（各自目标的 ±15%）
        if (overloaded_time <= ol_target * 1.15 and
                underloaded_time >= ul_target * 0.85):
            break

        if overloaded_time <= ol_target * 1.15:
            break

        # 从超载天移一个最近的店到轻载天
        if not clusters[overloaded_idx]:
            break

        # 找超载天中最佳迁移候选：地理连贯性 + 疲劳度综合评分
        ul_centroid = _cluster_centroid(clusters[underloaded_idx])
        if ul_centroid is None:
            break
        ol_centroid = _cluster_centroid(clusters[overloaded_idx])

        # 计算超载天疲劳度（用于优先移出高疲劳店铺）
        ol_fatigue = _compute_day_fatigue(clusters[overloaded_idx])

        # 第一轮：只考虑地理上合理的边界店铺（ratio < 3，即离目标不是离谱远）
        best_shop = None
        best_score = float("inf")
        for s in clusters[overloaded_idx]:
            lat = float(s.get("lat", 0))
            lng = float(s.get("lng", 0))
            d_to_target = _haversine_m(lat, lng, ul_centroid[0], ul_centroid[1])
            if ol_centroid:
                d_to_own = _haversine_m(lat, lng, ol_centroid[0], ol_centroid[1])
                ratio = d_to_target / max(d_to_own, 1.0)
            else:
                ratio = 1.0
            if ratio < 3.0:
                # 边际 TSP 插入成本（替代纯几何距离评分，对齐规格书 Phase 3）
                marginal = _marginal_insertion_cost(s, clusters[underloaded_idx],
                                                     transport_preference, max_hours_per_day)
                # 疲劳度调整：当超载天疲劳度高时，优先移出高疲劳店铺
                # 高 fatigue_weight 的店铺获得评分折扣（更容易被选中移出）
                fw = float(s.get("fatigue_weight", 1.0))
                fatigue_discount = 1.0 - 0.15 * (fw - 1.0) * ol_fatigue
                fatigue_discount = max(fatigue_discount, 0.5)  # 折扣不低于 0.5
                score = marginal * fatigue_discount
                # 地理分离惩罚：避免拆散距离近的 POI（如圆明园+颐和园仅 2.1km）
                # 当 shop 在同 cluster 内有近距离邻居时，移走它的代价更高
                ol_cluster = clusters[overloaded_idx]
                if len(ol_cluster) > 1:
                    nearest_neighbor_dist = min(
                        _haversine_m(lat, lng,
                                     float(s2.get("lat", 0)), float(s2.get("lng", 0)))
                        for s2 in ol_cluster if s2.get("shop_id") != s.get("shop_id")
                    )
                    # 邻居在 3km 内时增加惩罚（越近惩罚越高）
                    if nearest_neighbor_dist < 3000:
                        separation_penalty = (3000 - nearest_neighbor_dist) * 0.03
                        score += separation_penalty
                if score < best_score:
                    best_score = score
                    best_shop = s

        # 第二轮：无合理边界店铺时，选离自己质心最远的（地理异常点优先移走）
        if best_shop is None:
            best_score = -1.0
            for s in clusters[overloaded_idx]:
                lat = float(s.get("lat", 0))
                lng = float(s.get("lng", 0))
                if ol_centroid:
                    d_to_own = _haversine_m(lat, lng, ol_centroid[0], ol_centroid[1])
                else:
                    d_to_own = 0
                if d_to_own > best_score:
                    best_score = d_to_own
                    best_shop = s

        if best_shop:
            clusters[overloaded_idx].remove(best_shop)
            clusters[underloaded_idx].append(best_shop)
        else:
            break

    # ── 第二遍前半：景点数量均衡 —— 只统计 scenic，不包含饭店/购物中心 ──
    for _ in range(max_iter):
        # 只统计 scenic 类别（景区/景点）
        scenic_counts = [
            sum(1 for s in c if s.get("category", "") == "scenic")
            for c in clusters
        ]
        max_vc = max(scenic_counts)
        min_vc = min(scenic_counts)
        # 数量差距 <= 1 则认为已均衡
        if max_vc - min_vc <= 1:
            break

        over_idx = scenic_counts.index(max_vc)
        under_idx = scenic_counts.index(min_vc)

        # 从最多天选一个 scenic：地理连贯性评分（优先边界店铺）+ 疲劳度感知
        target_centroid = _cluster_centroid(clusters[under_idx])
        if target_centroid is None:
            break
        source_centroid = _cluster_centroid(clusters[over_idx])

        # 计算源天疲劳度
        src_fatigue = _compute_day_fatigue(clusters[over_idx])

        best_shop = None
        best_score = float("inf")
        for s in clusters[over_idx]:
            if s.get("category", "") != "scenic":
                continue  # 只搬 scenic（景点），不搬餐厅/购物中心
            lat = float(s.get("lat", 0))
            lng = float(s.get("lng", 0))
            d_to_target = _haversine_m(lat, lng, target_centroid[0], target_centroid[1])
            if source_centroid:
                d_to_own = _haversine_m(lat, lng, source_centroid[0], source_centroid[1])
                ratio = d_to_target / max(d_to_own, 1.0)
            else:
                ratio = 1.0
            if ratio < 3.0:
                # 边际 TSP 插入成本（替代纯几何距离评分）
                marginal = _marginal_insertion_cost(s, clusters[under_idx],
                                                     transport_preference, max_hours_per_day)
                # 疲劳度折扣：超载天高疲劳时优先移出高疲劳店铺
                fw = float(s.get("fatigue_weight", 1.0))
                fatigue_discount = 1.0 - 0.15 * (fw - 1.0) * src_fatigue
                fatigue_discount = max(fatigue_discount, 0.5)
                score = marginal * fatigue_discount
                # 地理分离惩罚：避免拆散邻近 scenic POI
                src_cluster = clusters[over_idx]
                if len(src_cluster) > 1:
                    nearest_dist = min(
                        _haversine_m(lat, lng,
                                     float(s2.get("lat", 0)), float(s2.get("lng", 0)))
                        for s2 in src_cluster
                        if s2.get("category") == "scenic" and s2.get("shop_id") != s.get("shop_id")
                    )
                    if nearest_dist < 3000:
                        score += (3000 - nearest_dist) * 0.03
                if score < best_score:
                    best_score = score
                    best_shop = s

        # 回退：无合理边界店铺时接受地理损失
        if best_shop is None:
            for s in clusters[over_idx]:
                if s.get("category", "") != "scenic":
                    continue
                lat = float(s.get("lat", 0))
                lng = float(s.get("lng", 0))
                d = _haversine_m(lat, lng, target_centroid[0], target_centroid[1])
                if d < best_score:
                    best_score = d
                    best_shop = s

        if best_shop:
            clusters[over_idx].remove(best_shop)
            clusters[under_idx].append(best_shop)
        else:
            break

    # ── 第二遍后半：数量上限 —— 每天 scenic 数不超上限 + 多米诺级联 ──
    prev_max_count = float("inf")
    prev_counts_tuple = None  # 振荡检测：记录上轮状态
    for _ in range(max_iter):
        # 只统计 scenic，饭店/购物中心不计入上限
        counts = [sum(1 for s in c if s.get("category", "") == "scenic") for c in clusters]
        max_count = max(counts)
        min_count = min(counts)

        # 最多天未超标 → 停止
        if max_count <= max_scenic_per_day:
            break

        # 防抖：如果总scenic超出 days*cap，无法全部满足，停止重分配
        total_scenic = sum(counts)
        if total_scenic > len(clusters) * max_scenic_per_day:
            break

        if max_count >= prev_max_count:
            break
        prev_max_count = max_count

        # 振荡检测：当前状态与上轮完全相同时停止
        counts_tuple = tuple(counts)
        if counts_tuple == prev_counts_tuple:
            break
        prev_counts_tuple = counts_tuple

        max_idx = counts.index(max_count)
        min_idx = counts.index(min_count)

        # 从最多天选一个 scenic：地理连贯性评分（优先边界店铺）+ 疲劳度感知
        target_centroid = _cluster_centroid(clusters[min_idx])
        if target_centroid is None:
            break
        source_centroid = _cluster_centroid(clusters[max_idx])

        src_fatigue = _compute_day_fatigue(clusters[max_idx])

        best_shop = None
        best_score = float("inf")
        for s in clusters[max_idx]:
            if s.get("category", "") != "scenic":
                continue  # 只搬 scenic，不动饭店/购物中心
            lat = float(s.get("lat", 0))
            lng = float(s.get("lng", 0))
            d_to_target = _haversine_m(lat, lng, target_centroid[0], target_centroid[1])
            if source_centroid:
                d_to_own = _haversine_m(lat, lng, source_centroid[0], source_centroid[1])
                ratio = d_to_target / max(d_to_own, 1.0)
            else:
                ratio = 1.0
            if ratio < 3.0:
                # 边际 TSP 插入成本（替代纯几何距离评分）
                marginal = _marginal_insertion_cost(s, clusters[min_idx],
                                                     transport_preference, max_hours_per_day)
                # 疲劳度折扣
                fw = float(s.get("fatigue_weight", 1.0))
                fatigue_discount = 1.0 - 0.15 * (fw - 1.0) * src_fatigue
                fatigue_discount = max(fatigue_discount, 0.5)
                score = marginal * fatigue_discount
                if score < best_score:
                    best_score = score
                    best_shop = s

        # 回退：无合理边界店铺时接受地理损失
        if best_shop is None:
            for s in clusters[max_idx]:
                if s.get("category", "") != "scenic":
                    continue
                lat = float(s.get("lat", 0))
                lng = float(s.get("lng", 0))
                d = _haversine_m(lat, lng, target_centroid[0], target_centroid[1])
                if d < best_score:
                    best_score = d
                    best_shop = s

        if best_shop:
            clusters[max_idx].remove(best_shop)
            clusters[min_idx].append(best_shop)
            # ── 多米诺级联：检查目标天是否因此超载 ──
            _domino_shift(clusters, from_idx=max_idx, to_idx=min_idx,
                          moved_shop=best_shop,
                          max_hours_per_day=max_hours_per_day,
                          max_scenic_per_day=max_scenic_per_day)
        else:
            break

    return clusters


def _cluster_centroid(cluster: list):
    """计算簇的地理中心"""
    if not cluster:
        return None
    lats = [float(s.get("lat", 0)) for s in cluster]
    lngs = [float(s.get("lng", 0)) for s in cluster]
    return (sum(lats) / len(lats), sum(lngs) / len(lngs))


# ======================================================================
# Phase 3.5: 多锚点路径合成 —— 每日起点/终点锚点计算
# ======================================================================

def _compute_day_anchors(day_index: int, total_days: int, hotel_plan: list,
                         travel_info: dict = None,
                         checkin_lat: float = 0.0, checkin_lng: float = 0.0) -> tuple:
    """
    计算每天的起点和终点锚点坐标，替代 solve_multi_day 中的内联逻辑。

    锚点规则：
    - Day 0 起点：到达站点（travel_info.arrival_station）→ 否则默认酒店
    - Day 0 终点：hotel_plan[0] 当晚酒店 → 否则默认酒店
    - 中间天起点：hotel_plan[day_index-1] 前一晚酒店 → 否则默认酒店
    - 中间天终点：hotel_plan[day_index] 当晚酒店 → 否则默认酒店
    - 最后一天终点：返程站点（travel_info.return_station）→ 否则默认酒店

    hotel_plan 来自 _dynamic_hotel_decision，长度为 total_days-1（每个边界一个决策）。
    hotel_plan[i] 包含 plan, hotel_lat, hotel_lng, cost 字段，
    表示第 i 天与第 i+1 天之间的换住决策（即第 i 天结束后的酒店）。

    Args:
        day_index: 当前天索引（0-based）
        total_days: 总天数
        hotel_plan: _dynamic_hotel_decision 返回的酒店方案列表（长度 = total_days-1）
        travel_info: 旅行信息，含 arrival_station / return_station 字段
        checkin_lat: 默认酒店纬度（无 hotel_plan 时使用）
        checkin_lng: 默认酒店经度

    Returns:
        (start_lat, start_lng, end_lat, end_lng)
    """
    is_first_day = (day_index == 0)
    is_last_day = (day_index == total_days - 1)

    # ── 默认：酒店坐标 ──
    start_lat, start_lng = checkin_lat, checkin_lng
    end_lat, end_lng = checkin_lat, checkin_lng

    # ── Day 0 起点：到达站点优先 ──
    if is_first_day and travel_info:
        arrival_station = travel_info.get("arrival_station", "")
        station_c = _lookup_station_coord(arrival_station)
        if station_c:
            start_lat, start_lng = station_c

    # ── 最后一天终点：返程站点优先 ──
    if is_last_day and travel_info:
        return_station = travel_info.get("return_station", "")
        station_c = _lookup_station_coord(return_station)
        if station_c:
            end_lat, end_lng = station_c

    # ── hotel_plan 覆盖酒店坐标 ──
    if hotel_plan:
        # 当天终点：hotel_plan[day_index]（当晚入住的酒店）
        if day_index < len(hotel_plan):
            h = hotel_plan[day_index]
            end_lat = float(h.get("hotel_lat", end_lat))
            end_lng = float(h.get("hotel_lng", end_lng))

        # 当天起点（非 Day 0）：hotel_plan[day_index-1]（前一晚的酒店 → 今早出发）
        if not is_first_day:
            prev_idx = day_index - 1
            if prev_idx < len(hotel_plan):
                h = hotel_plan[prev_idx]
                start_lat = float(h.get("hotel_lat", start_lat))
                start_lng = float(h.get("hotel_lng", start_lng))

    return (start_lat, start_lng, end_lat, end_lng)


# ======================================================================
# 阶段 3: 每日 TSP —— 最近邻 + 2-opt
# ======================================================================

def _route_one_day(shops: list, start_lat: float, start_lng: float,
                   transport: str, weather: dict = None) -> dict:
    """为一天规划最优路线。起点/终点均为同一坐标。兼容旧调用。"""
    return _route_one_day_dynamic(shops, start_lat, start_lng, start_lat, start_lng, transport, weather)


def _route_one_day_dynamic(shops: list, start_lat: float, start_lng: float,
                           end_lat: float, end_lng: float,
                           transport: str, weather: dict = None) -> dict:
    """
    为一天规划最优路线（天气感知），支持不同起点/终点。
    起点 = 到达站点（Day 1）或酒店，终点 = 返程站点（最后一天）或酒店。
    返回: {total_travel_minutes, total_duration_minutes, route: [...]}
    """
    if not shops:
        return {
            "timeline": [],
            "total_travel_minutes": 0,
            "total_duration_minutes": 0,
            "route": [(start_lat, start_lng)],
        }

    speed = _get_speed(transport)
    weather_penalty = 1.0
    if weather:
        weather_penalty = weather.get("walking_penalty", 1.0)
        if transport in ("步行优先",):
            speed *= weather_penalty
        elif weather_penalty < 0.5:
            speed *= 0.8

    points = [(start_lat, start_lng)]  # 起点
    for s in shops:
        lat = float(s.get("lat", start_lat))
        lng = float(s.get("lng", start_lng))
        points.append((lat, lng))
    points.append((end_lat, end_lng))  # 终点（可能与起点不同）

    route = _nearest_neighbor_route(points)
    route = _two_opt_improve(route)

    total_travel_m = 0
    for i in range(len(route) - 1):
        total_travel_m += _haversine_m(
            route[i][0], route[i][1],
            route[i + 1][0], route[i + 1][1]
        )
    total_travel_minutes = total_travel_m / speed
    total_duration_minutes = sum(_get_duration(s.get("category", ""), s) for s in shops)

    return {
        "total_travel_minutes": round(total_travel_minutes),
        "total_duration_minutes": total_duration_minutes,
        "route": [(lat, lng) for lat, lng in route],
    }


def _nearest_neighbor_route(points: list) -> list:
    """贪心最近邻路由"""
    if len(points) <= 2:
        return list(points)

    unvisited = set(range(1, len(points) - 1))  # 排除起点(0)和终点(-1)
    route = [points[0]]
    current = 0

    while unvisited:
        best_next = None
        best_dist = float("inf")
        for nxt in unvisited:
            d = _haversine_m(
                route[-1][0], route[-1][1],
                points[nxt][0], points[nxt][1]
            )
            if d < best_dist:
                best_dist = d
                best_next = nxt
        route.append(points[best_next])
        unvisited.remove(best_next)

    route.append(points[-1])  # 回到原点
    return route


def _two_opt_improve(route: list) -> list:
    """2-opt 局部搜索优化"""
    improved = True
    best_route = list(route)

    def _route_dist(r):
        total = 0
        for i in range(len(r) - 1):
            total += _haversine_m(r[i][0], r[i][1], r[i+1][0], r[i+1][1])
        return total

    best_dist = _route_dist(best_route)

    while improved:
        improved = False
        for i in range(1, len(best_route) - 2):
            for j in range(i + 1, len(best_route) - 1):
                # 反转段 [i:j+1]
                new_route = best_route[:i] + best_route[i:j+1][::-1] + best_route[j+1:]
                new_dist = _route_dist(new_route)
                if new_dist < best_dist - 1:  # 至少改善 1 米
                    best_dist = new_dist
                    best_route = new_route
                    improved = True
                    break
            if improved:
                break

    return best_route


def _reorder_by_route(shops: list, route: list) -> list:
    """
    将 shops 按 TSP 优化后的 route 坐标顺序重排。
    route 是 [(lat, lng), ...]，包含起止酒店坐标。
    返回按旅行顺序排列的 shops 列表。
    """
    if not route or len(route) < 3:
        return list(shops)

    # 构建 (rounded_lat, rounded_lng) → shop 映射（处理 GPS 精度差异）
    coord_to_shops = {}
    for s in shops:
        key = (round(float(s.get("lat", 0)), 4), round(float(s.get("lng", 0)), 4))
        if key not in coord_to_shops:
            coord_to_shops[key] = []
        coord_to_shops[key].append(s)

    ordered = []
    seen_ids = set()
    # 跳过首尾酒店坐标，只处理中间途经点
    for point in route[1:-1]:
        key = (round(point[0], 4), round(point[1], 4))
        candidates = coord_to_shops.get(key, [])
        for shop in candidates:
            sid = shop.get("shop_id", "")
            if sid not in seen_ids:
                ordered.append(shop)
                seen_ids.add(sid)
                break

    # 兜底：未被 route 匹配到的 shop 保持原顺序追加
    for s in shops:
        if s.get("shop_id", "") not in seen_ids:
            ordered.append(s)

    return ordered


# ======================================================================
# 阶段 1.5: 开放式 TSP（无酒店锚点）
# ======================================================================


def _nearest_neighbor_open_loop(points: list, start_idx: int = 0) -> list:
    """最近邻构建开放路径（不闭合，无固定起点/终点锚点）。"""
    if len(points) <= 1:
        return list(points)

    unvisited = set(range(len(points)))
    unvisited.discard(start_idx)
    route = [points[start_idx]]

    while unvisited:
        best_next = None
        best_dist = float("inf")
        for nxt in unvisited:
            d = _haversine_m(
                route[-1][0], route[-1][1],
                points[nxt][0], points[nxt][1]
            )
            if d < best_dist:
                best_dist = d
                best_next = nxt
        route.append(points[best_next])
        unvisited.remove(best_next)

    return route


def _two_opt_improve_open_loop(route: list) -> list:
    """2-opt 局部搜索优化开放路径（无闭合约束）。"""
    improved = True
    best_route = list(route)

    def _route_dist(r):
        total = 0
        for i in range(len(r) - 1):
            total += _haversine_m(r[i][0], r[i][1], r[i+1][0], r[i+1][1])
        return total

    best_dist = _route_dist(best_route)

    while improved:
        improved = False
        for i in range(len(best_route) - 1):
            for j in range(i + 1, len(best_route)):
                # 反转段 [i:j+1]
                new_route = best_route[:i] + best_route[i:j+1][::-1] + best_route[j+1:]
                new_dist = _route_dist(new_route)
                if new_dist < best_dist - 1:  # 至少改善 1 米
                    best_dist = new_dist
                    best_route = new_route
                    improved = True
                    break
            if improved:
                break

    return best_route


def _route_open_loop(shops: list, transport: str) -> dict:
    """开放式 TSP：不设酒店锚点，纯 POI 间最短路径。

    对 shops 进行开放式旅行商路径规划（无固定起点/终点），
    返回途经所有 POI 的最优访问顺序及总行程时间。

    返回:
        {
            "route": [(lat, lng), ...],      # 按访问顺序排列的坐标列表
            "total_travel_minutes": float,    # 总行程时间（分钟）
        }
    """
    if not shops:
        return {"route": [], "total_travel_minutes": 0}

    speed = _get_speed(transport)

    # 构建坐标点列表
    points = []
    for s in shops:
        lat = float(s.get("lat", 0))
        lng = float(s.get("lng", 0))
        points.append((lat, lng))

    if len(points) == 1:
        return {"route": [(points[0][0], points[0][1])], "total_travel_minutes": 0}

    if len(points) == 2:
        total_m = _haversine_m(points[0][0], points[0][1], points[1][0], points[1][1])
        return {
            "route": [(p[0], p[1]) for p in points],
            "total_travel_minutes": round(total_m / speed, 1),
        }

    # 尝试多个起点，选择总距离最小的路径
    best_route = None
    best_dist = float("inf")
    # 启发式：尝试前 min(3, n) 个点作为起点，或选取地理中心最远的点
    candidates = list(range(min(len(points), 3)))

    for start_idx in candidates:
        route = _nearest_neighbor_open_loop(points, start_idx)
        route = _two_opt_improve_open_loop(route)
        dist = sum(
            _haversine_m(route[i][0], route[i][1], route[i+1][0], route[i+1][1])
            for i in range(len(route) - 1)
        )
        if dist < best_dist:
            best_dist = dist
            best_route = route

    return {
        "route": [(lat, lng) for lat, lng in best_route],
        "total_travel_minutes": round(best_dist / speed, 1),
    }


# ======================================================================
# 阶段 1.5: 每日边界提取（用于酒店决策）
# ======================================================================


def _extract_day_boundaries(clusters: list, transport: str = "驾车优先") -> list:
    """提取每天行程的首尾 POI，用于跨日酒店决策。

    对每一天的 cluster 执行开放式 TSP，找到当天行程的最后一个 POI
    和次日行程的第一个 POI。这些边界点用于评估酒店位置方案。

    参数:
        clusters: [[shop, ...], ...]  按天分组的 POI 列表
        transport: 交通方式

    返回:
        [
            {
                "end_poi": shop,     # 第 d 天行程末位 POI
                "start_poi": shop,   # 第 d+1 天行程首位 POI
            },
            ...  # 共 n-1 个边界（n 天之间的 n-1 个晚上）
        ]
    """
    boundaries = []

    for d in range(len(clusters) - 1):
        today = clusters[d]
        tomorrow = clusters[d + 1]

        end_shop = today[-1]   # 默认取列表最后一个
        start_shop = tomorrow[0]  # 默认取列表第一个

        if today:
            today_route = _route_open_loop(today, transport)
            today_points = today_route["route"]
            if today_points:
                end_pt = today_points[-1]
                # 匹配回 shop 对象（按坐标四舍五入匹配）
                for s in today:
                    if (round(float(s.get("lat", 0)), 4) == round(end_pt[0], 4) and
                            round(float(s.get("lng", 0)), 4) == round(end_pt[1], 4)):
                        end_shop = s
                        break

        if tomorrow:
            tomorrow_route = _route_open_loop(tomorrow, transport)
            tomorrow_points = tomorrow_route["route"]
            if tomorrow_points:
                start_pt = tomorrow_points[0]
                for s in tomorrow:
                    if (round(float(s.get("lat", 0)), 4) == round(start_pt[0], 4) and
                            round(float(s.get("lng", 0)), 4) == round(start_pt[1], 4)):
                        start_shop = s
                        break

        boundaries.append({
            "end_poi": end_shop,
            "start_poi": start_shop,
        })

    return boundaries


# ======================================================================
# 阶段 2: 动态换住决策矩阵 —— DP 状态机评估 3 种酒店方案
# ======================================================================

# 换住决策常量
LUGGAGE_PENALTY_MINUTES = 60      # 换酒店行李搬运固定时间惩罚（分钟）
HEAVY_LOAD_BONUS_MINUTES = 30     # 次日高负载减免（分钟）
HEAVY_LOAD_THRESHOLD = 0.8        # 触发减免的次日负载阈值


def _evaluate_hotel_plan(
    plan: str,
    prev_hotel: tuple,
    end_poi_lat: float,
    end_poi_lng: float,
    start_poi_lat: float,
    start_poi_lng: float,
    next_day_load: float,
    fatigue: float,
    speed: float,
) -> tuple:
    """评估单个酒店方案的代价。

    三种方案：
    - A（原店续住）：行李不动，代价 = 当天末位→酒店 + 酒店→次日起点
    - B（换住当天终点附近）：行李搬运惩罚 + 回原酒店取行李的交通
    - C（换住次日起点附近）：行李搬运惩罚 + 移动到次日起点的交通
      （次日负载 > 80% 时触发 30min 减免）

    参数:
        plan: "A" | "B" | "C"
        prev_hotel: (lat, lng) 前一晚酒店坐标
        end_poi_lat/lng: 当天最后一个 POI 坐标
        start_poi_lat/lng: 次日第一个 POI 坐标
        next_day_load: 次日负载比例 [0, 1]
        fatigue: 当日累积疲劳度 [0, 1]
        speed: 交通速度（米/分钟）

    返回:
        (cost: float, reason: str)
    """
    prev_lat, prev_lng = prev_hotel

    if plan == "A":
        # 原店续住：仅往返交通代价，无行李惩罚
        back_to_hotel = _haversine_m(end_poi_lat, end_poi_lng, prev_lat, prev_lng) / speed
        morning_to_start = _haversine_m(prev_lat, prev_lng, start_poi_lat, start_poi_lng) / speed
        cost = back_to_hotel + morning_to_start
        reason = "A-原店续住"

    elif plan == "B":
        # 就近当天终点：行李惩罚 + 回酒店取行李的交通
        back_to_hotel = _haversine_m(end_poi_lat, end_poi_lng, prev_lat, prev_lng) / speed
        cost = LUGGAGE_PENALTY_MINUTES + back_to_hotel
        reason = "B-就近终点"

    elif plan == "C":
        # 就近次日起点：行李惩罚 + 从终点到次日起点附近
        to_start_area = _haversine_m(end_poi_lat, end_poi_lng, start_poi_lat, start_poi_lng) / speed
        # 新酒店 HC 到次日首发景点的距离（Plan C 酒店在起点附近）
        hotel_to_start_m = _haversine_m(start_poi_lat, start_poi_lng, start_poi_lat, start_poi_lng)
        cost = LUGGAGE_PENALTY_MINUTES + to_start_area
        # 双重条件：负载 > 80% AND HC 距离首发景点 < 3km 才触发减免
        if next_day_load > HEAVY_LOAD_THRESHOLD and hotel_to_start_m < 3000:
            cost -= HEAVY_LOAD_BONUS_MINUTES
            reason = "C-就近起点(次日重载+近距减免)"
        else:
            reason = "C-就近起点"

    else:
        raise ValueError(f"Unknown hotel plan: {plan}")

    # 疲劳度放大代价（疲劳越高，换酒店的代价越大）
    cost *= (1.0 + fatigue * 0.5)

    return round(cost, 1), reason


def _pick_best_hotel_plan(
    prev_hotel: tuple,
    end_poi_lat: float,
    end_poi_lng: float,
    start_poi_lat: float,
    start_poi_lng: float,
    next_day_load: float,
    fatigue: float,
    speed: float,
) -> tuple:
    """DP 自动选择最小代价的酒店方案。

    对 A/B/C 三种方案分别评估，选择总代价最小的。

    返回:
        (best_plan: str, best_cost: float, best_hotel: tuple)
    """
    plans = {}
    for plan in ("A", "B", "C"):
        cost, reason = _evaluate_hotel_plan(
            plan, prev_hotel, end_poi_lat, end_poi_lng,
            start_poi_lat, start_poi_lng, next_day_load, fatigue, speed
        )
        plans[plan] = (cost, reason)

    best_plan = min(plans, key=lambda p: plans[p][0])
    best_cost = plans[best_plan][0]
    best_reason = plans[best_plan][1]

    # 确定最优方案对应的新酒店坐标
    prev_lat, prev_lng = prev_hotel
    if best_plan == "A":
        best_hotel = (prev_lat, prev_lng)
    elif best_plan == "B":
        best_hotel = (end_poi_lat, end_poi_lng)
    else:  # C
        best_hotel = (start_poi_lat, start_poi_lng)

    return best_plan, best_cost, best_hotel


def _dynamic_hotel_decision(
    clusters: list,
    initial_hotel: tuple,
    transport: str = "驾车优先",
    user_provided_hotel: bool = False,
) -> list:
    """完整换住决策 pipeline：每晚都产出 hotel_plan。

    对每对相邻天的边界进行 DP 评估，在方案 A（续住）、B（换到终点）、
    C（换到起点）中选择最优方案。

    参数:
        clusters: [[shop, ...], ...]  按天分组的 POI 列表
        initial_hotel: (lat, lng) 初始酒店坐标
        transport: 交通方式
        user_provided_hotel: 用户是否已指定酒店（True=全部续住，不换房）

    返回:
        [
            {
                "plan": "A"|"B"|"C",
                "hotel_lat": float,
                "hotel_lng": float,
                "cost": float,
            },
            ...  # 共 n-1 个决策（n 天之间的 n-1 个晚上）
        ]
    """
    if len(clusters) < 2:
        return []

    decisions = []
    boundaries = _extract_day_boundaries(clusters, transport)
    speed = _get_speed(transport)
    current_hotel = initial_hotel

    for i, b in enumerate(boundaries):
        end_poi = b["end_poi"]
        start_poi = b["start_poi"]

        end_lat = float(end_poi.get("lat", current_hotel[0]))
        end_lng = float(end_poi.get("lng", current_hotel[1]))
        start_lat = float(start_poi.get("lat", current_hotel[0]))
        start_lng = float(start_poi.get("lng", current_hotel[1]))

        # 计算次日负载比例
        tomorrow_cluster = clusters[i + 1]
        tomorrow_duration = sum(
            _get_duration(s.get("category", ""), s) for s in tomorrow_cluster
        )
        max_duration = 8 * 60  # 每天最大 8 小时活动
        next_day_load = min(tomorrow_duration / max_duration, 1.0) if max_duration > 0 else 0.0

        # 累积疲劳度估算（每天增加 ~0.12）
        fatigue = min(i * 0.12, 0.6)

        if user_provided_hotel:
            best_plan = "A"
            best_cost = 0.0
            best_hotel = current_hotel
        else:
            best_plan, best_cost, best_hotel = _pick_best_hotel_plan(
                current_hotel, end_lat, end_lng, start_lat, start_lng,
                next_day_load, fatigue, speed
            )

        decisions.append({
            "plan": best_plan,
            "hotel_lat": best_hotel[0],
            "hotel_lng": best_hotel[1],
            "cost": best_cost,
            "needs_change": best_plan in ("B", "C"),
        })

        # 更新当前酒店坐标，供下一轮决策使用
        current_hotel = best_hotel

    return decisions


# ======================================================================
# 阶段 3: 边际插入成本负载均衡、疲劳度模型、多米诺级联
# ======================================================================

# 疲劳度计算常量
FATIGUE_MAX_BASELINE = 600.0       # 最大基准疲劳（10小时 × 1.0 权重）
FATIGUE_RECOVERY_RATE = 0.15       # 每晚疲劳自然恢复率（睡眠恢复 15%）


def _compute_day_fatigue(cluster: list) -> float:
    """计算一个 cluster 的累积疲劳度 [0, 1]。

    考虑每个 shop 的 duration_minutes 和 fatigue_weight：
    - fatigue_weight 来自 LLM 注入（1-10，默认 1.0）
    - 时长越长的店铺贡献越大
    - 高 fatigue_weight 的店铺显著增加疲劳

    归一化到 [0, 1]，超过 1.0 时截断（极端情况保护）。
    """
    if not cluster:
        return 0.0

    total_fatigue = 0.0
    for s in cluster:
        duration = _get_duration(s.get("category", ""), s)
        fw = float(s.get("fatigue_weight", 1.0))
        total_fatigue += duration * fw

    return min(total_fatigue / FATIGUE_MAX_BASELINE, 1.0)


def _marginal_insertion_cost(shop: dict, target_cluster: list, transport: str = "驾车优先",
                              max_hours_per_day: float = 8.0) -> float:
    """计算将 shop 插入 target_cluster 的边际 TSP 成本（分钟）。

    使用 _route_open_loop 精确计算插入前后的路径成本差：
    - 空 cluster：成本 = 0
    - 非空 cluster：成本 = 插入后路径耗时 - 插入前路径耗时

    时间预算压力放大：当目标天已接近或超过 max_hours_per_day 时，
    边际成本乘以压力因子（1.0 + 超出比例），使插入已经满载的天成本更高。

    返回：边际成本（分钟），总是 >= 0。
    """
    if not target_cluster:
        return 0.0

    # 插入前的 TSP 路径耗时
    before = _route_open_loop(target_cluster, transport)
    before_minutes = before["total_travel_minutes"]

    # 插入后的 TSP 路径耗时
    after_cluster = list(target_cluster) + [shop]
    after = _route_open_loop(after_cluster, transport)
    after_minutes = after["total_travel_minutes"]

    marginal_travel = max(after_minutes - before_minutes, 0.0)

    # 时间预算压力因子
    current_minutes = 0
    for s in target_cluster:
        current_minutes += _get_duration(s.get("category", ""), s)
    current_minutes += _get_duration(shop.get("category", ""), shop)
    # 旅行时间粗略估算（每店 15min）
    current_minutes += (len(target_cluster) + 1) * 15

    target_minutes = max_hours_per_day * 60
    if current_minutes > target_minutes and target_minutes > 0:
        pressure = 1.0 + (current_minutes - target_minutes) / target_minutes
    else:
        pressure = 1.0

    return marginal_travel * pressure


def _domino_shift(clusters: list, from_idx: int, to_idx: int, moved_shop: dict = None,
                  max_hours_per_day: float = 8.0, max_scenic_per_day: int = 5) -> None:
    """多米诺级联滚动：当 to_idx 天超载时，将超载店铺继续向后传递。

    级联触发条件：
    - 目标天 scenic 数量超过 max_scenic_per_day
    级联策略：
    - 从超载天选择离下一天质心最近的 scenic 店向下传递
    - 最大级联深度 = len(clusters) - 1（每晚最多一次传递）
    - 级联终止：所有天都不超载 或 到达最后一天 或 达到最大深度

    原地修改 clusters，不返回值。
    """
    max_depth = len(clusters) - 1
    depth = 0
    current_idx = to_idx

    while depth < max_depth and current_idx < len(clusters):
        cluster = clusters[current_idx]
        scenic_count = sum(1 for s in cluster if s.get("category", "") == "scenic")

        if scenic_count <= max_scenic_per_day:
            break  # 不超载，级联终止

        # 当前天超载，向后级联
        next_idx = current_idx + 1
        if next_idx >= len(clusters):
            break  # 已经是最后一天，无处可移

        # 选择最佳级联候选：离下一天质心最近的 scenic
        next_centroid = _cluster_centroid(clusters[next_idx])
        if next_centroid is None:
            break

        best_shop = None
        best_dist = float("inf")
        for s in cluster:
            if s.get("category", "") != "scenic":
                continue
            lat = float(s.get("lat", 0))
            lng = float(s.get("lng", 0))
            d = _haversine_m(lat, lng, next_centroid[0], next_centroid[1])
            if d < best_dist:
                best_dist = d
                best_shop = s

        if best_shop and best_shop is not moved_shop:
            cluster.remove(best_shop)
            clusters[next_idx].append(best_shop)
            current_idx = next_idx
            depth += 1
        else:
            break  # 无合适候选，终止级联


# ======================================================================
# 阶段 4: 用餐插入
# ======================================================================

LUNCH_WINDOW = (11 * 60 + 30, 13 * 60 + 30)   # 11:30-13:30
DINNER_WINDOW = (17 * 60 + 30, 19 * 60 + 30)  # 17:30-19:30


def _l3_loosest_day_backfill(unassigned: list, days: list,
                               transport: str = "步行优先") -> tuple:
    """Phase 6: L3 逆向扫描最宽松天，将未分配店铺插入最合适的日间空隙。

    改进版：扫描每天所有相邻节点间的 gap（不只是 BEDTIME 前），
    选择最大的可用空隙插入，使备选打卡出现在合理的日间时段。

    Args:
        unassigned: 未分配店铺列表（需含 unassigned_type 字段）
        days: 每日结果列表（含 timeline、total_duration_minutes、total_travel_minutes）
        transport: 交通方式（供距离估算用，当前版本简化处理）

    Returns:
        (days, still_unassigned, backup_count)
    """

    # 交通/返程节点 —— L3 回填不能跨越这些节点插入
    TRAVEL_ACTIONS = {"TO_STATION", "DEPARTURE", "RETURN_JOURNEY", "ARRIVE_HOME", "LEAVE_HOME"}

    def _t2m(t_str):
        try:
            h, m = map(int, str(t_str).split(":"))
            return h * 60 + m
        except (ValueError, TypeError):
            return 0  # 与 _time_str_to_minutes 保持一致

    def _m2t(m):
        m = max(0, min(23 * 60 + 59, m))
        return f"{m // 60:02d}:{m % 60:02d}"

    if not unassigned:
        return days, [], 0

    still_unassigned = []
    backup_count = 0

    for us in unassigned:
        shop_id = us.get("shop_id", "")
        shop_name = us.get("name", us.get("shop_name", shop_id))
        shop_cat = us.get("category", "default")
        shop_lat = float(us.get("lat", 0))
        shop_lng = float(us.get("lng", 0))
        shop_dur = int(us.get("duration_minutes", 60))
        # 极简打卡模式：取 min(shop_dur, 60) 保证至少能塞入
        min_dur = min(shop_dur, 60)

        # 扫描所有天所有间隙，找最大可用 gap
        best_day_idx = None
        best_gap = -1
        best_insert_pos = None
        best_insert_time = None

        for di, day in enumerate(days):
            timeline = day.get("timeline", [])
            if not timeline:
                continue

            # 收集非特殊节点（排除 BEDTIME/WAKE_UP + 交通/返程节点）
            regular_nodes = [n for n in timeline
                           if n.get("action") not in ("BEDTIME", "WAKE_UP")
                           and n.get("action") not in TRAVEL_ACTIONS]
            regular_nodes.sort(key=lambda n: _t2m(n.get("time", "00:00")))

            # 找 BEDTIME 时间
            bedtime_min = 22 * 60
            for n in timeline:
                if n.get("action") == "BEDTIME":
                    bedtime_min = _t2m(n.get("time", "22:00"))
                    break

            # 找 TO_STATION 时间作为硬截止（离开日不能超过此时间）
            cutoff_min = bedtime_min  # 默认等于 bedtime
            for n in timeline:
                if n.get("action") == "TO_STATION":
                    cutoff_min = _t2m(n.get("time", "22:00"))
                    break

            if not regular_nodes:
                continue

            # 扫描相邻节点间 gap
            for j in range(len(regular_nodes) - 1):
                prev_time = _t2m(regular_nodes[j].get("time", "09:00"))
                prev_dur = regular_nodes[j].get("duration_minutes", 0)
                prev_end = prev_time + prev_dur
                next_time = _t2m(regular_nodes[j + 1].get("time", "22:00"))
                gap = next_time - prev_end

                # 插入时间 + 持续时间不能超过当天硬截止
                if prev_end >= cutoff_min:
                    continue
                effective_gap = min(next_time, cutoff_min) - prev_end

                if effective_gap >= min_dur + 10:  # 至少能塞入极简打卡 + 缓冲
                    if effective_gap > best_gap:
                        best_gap = effective_gap
                        best_day_idx = di
                        best_insert_pos = None
                        actual_dur = min(shop_dur, effective_gap - 10)
                        best_insert_time = prev_end + max(5, (effective_gap - actual_dur) // 2)

            # 也检查截止时间前 gap（兜底，不再使用 BEDTIME 前的 ARRIVE_HOME）
            if regular_nodes:
                last_node = regular_nodes[-1]
                last_end = _t2m(last_node.get("time", "09:00")) + last_node.get("duration_minutes", 0)
                pre_cutoff_gap = cutoff_min - last_end
                if pre_cutoff_gap >= 15 and pre_cutoff_gap > best_gap:
                    best_gap = pre_cutoff_gap
                    best_day_idx = di
                    best_insert_time = max(last_end + 5, cutoff_min - 15)
                    best_insert_pos = None

        if best_day_idx is not None:
            # 找到 BEDTIME 在 timeline 中的位置用于插入
            timeline = days[best_day_idx]["timeline"]
            insert_pos = len(timeline)
            for j, n in enumerate(timeline):
                if n.get("action") == "BEDTIME":
                    insert_pos = j
                    break

            actual_dur = min(shop_dur, max(10, best_gap - 5))
            backup_node = {
                "time": _m2t(best_insert_time),
                "action": "VISIT",
                "memo": f"⚠️ 弹性备选：{shop_name}（系统自动补入日间空闲时段）",
                "category": shop_cat,
                "shop_id": shop_id,
                "duration_minutes": actual_dur,
                "opentime": us.get("opentime", "未知"),
                "is_backup": True,
                "lat": shop_lat,
                "lng": shop_lng,
            }
            timeline.insert(insert_pos, backup_node)
            backup_count += 1
            print(f"[L3逆向倒灌] ✅ '{shop_name}' ({shop_id}) → 第{best_day_idx+1}天 "
                  f"{_m2t(best_insert_time)} (日间gap={best_gap}min, dur={actual_dur}min)", flush=True)
        else:
            still_unassigned.append(us)
            print(f"[L3逆向倒灌] ❌ '{shop_name}' ({shop_id}) 无法塞入任何一天", flush=True)

    # 所有天重新按时间排序（保持 WAKE_UP 最前、BEDTIME 最后）
    for day in days:
        timeline = day.get("timeline", [])
        wake_node = None
        bedtime_node = None
        rest = []
        for n in timeline:
            if n.get("action") == "WAKE_UP":
                wake_node = n
            elif n.get("action") == "BEDTIME":
                bedtime_node = n
            else:
                rest.append(n)
        rest.sort(key=lambda n: _t2m(n.get("time", "00:00")))
        day["timeline"] = []
        if wake_node:
            day["timeline"].append(wake_node)
        day["timeline"].extend(rest)
        if bedtime_node:
            day["timeline"].append(bedtime_node)

    return days, still_unassigned, backup_count


def _bind_meals_to_destinations(main_meal_shops: list, visitable_shops: list) -> dict:
    """
    将每家正餐餐厅绑定到距离最近的非餐饮目的地。
    返回: {meal_shop_id: {"dest_name": str, "dest_shop_id": str, "distance_m": float}}
    用于后续在同一 day 内将餐厅安排在其绑定目的地附近。
    """
    bindings = {}
    for meal in main_meal_shops:
        best = None
        best_dist = float('inf')
        for v in visitable_shops:
            d = _haversine_m(meal.get('lat', 0), meal.get('lng', 0),
                             v.get('lat', 0), v.get('lng', 0))
            if d < best_dist:
                best_dist = d
                best = v
        if best:
            bindings[meal.get('shop_id')] = {
                "dest_name": best.get('name', ''),
                "dest_shop_id": best.get('shop_id', ''),
                "distance_m": best_dist,
            }
    return bindings


def _build_timeline(day_plan: dict, shops: list, start_time_str: str = "09:00",
                    weather: dict = None, wake_time_str: str = "07:30",
                    bedtime_str: str = "22:00", week_day: int = 0,
                    transport: str = "步行优先",
                    travel_info: dict = None,
                    hotel_plan: list = None,
                    day_index: int = 0) -> dict:
    """
    智能时间线构建：就近用餐 + 休息缓冲 + 天气标记 + 营业时间感知。

    参数:
        transport: 用户选择的交通方式，用于活动间 travel 耗时估算
        travel_info: 出行信息（用于出行日联动起床时间）

    返回:
      {"timeline": [...], "closed_conflicts": [...], "unknown_hours_shops": [...]}
    """
    start_h, start_m = map(int, start_time_str.split(":"))
    current_minutes = start_h * 60 + start_m

    # ── 解析 bedtime 约束（用于最后一天返程截止）──
    try:
        bed_h, bed_m = map(int, bedtime_str.split(":"))
        bedtime_cap = bed_h * 60 + bed_m
    except (ValueError, TypeError):
        bedtime_cap = 22 * 60  # 默认 22:00

    ordered_shops = list(shops)  # 保持原顺序（TSP已优化）
    # 因时间不够（超出 bedtime 约束）而未排入的店铺
    unassigned_shops = []

    # ── 品类分流：正餐不入VISIT循环，小吃需间隔控制 ──
    main_meal_shops = [s for s in ordered_shops if s.get("category", "") in MAIN_MEAL_CATEGORIES]
    snack_shops = [s for s in ordered_shops if s.get("category", "") in SNACK_CATEGORIES]
    # 非餐类 + 小吃 = VISIT循环遍历对象（正餐排除在外）
    # 重排：day 优先在前（上午/下午），night 在后（晚间），both/unknown 在中间
    _all_visitable = [s for s in ordered_shops if s.get("category", "") not in MAIN_MEAL_CATEGORIES]
    day_only = [s for s in _all_visitable if s.get("suitable_time") == "day"]
    both_unknown = [s for s in _all_visitable if s.get("suitable_time") not in ("day", "night")]
    night_only = [s for s in _all_visitable if s.get("suitable_time") == "night"]
    visitable_shops = day_only + both_unknown + night_only

    timeline = []
    closed_conflicts = []
    unknown_hours_shops = []
    MEAL_CATS = MEAL_CATEGORIES
    _last_food_end_minutes = None  # 追踪上一次进食结束时间（用于小吃间隔控制）
    used_meal_shop_ids = set()     # 追踪所有已用于餐食的店铺ID，防止同一店铺用于多餐

    # ── 天气信息 ──
    weather_alert = None
    if weather:
        if not weather.get("outdoor_suitable", True):
            weather_alert = "🌧️ 建议带伞" if weather.get("walking_penalty", 1.0) > 0.5 else "⛈️ 天气影响，注意安全"
        if weather.get("day_temp", 25) > 35:
            weather_alert = (weather_alert or "") + " 🔥 高温，注意防暑"

    # ── 智能起床时间 ──
    # 出行日（有 travel_info）：起床 = 出门时间 - 洗漱准备时间（赶早班机自动提前）
    if travel_info and travel_info.get("outbound_departure_time"):
        outbound_dep = travel_info.get("outbound_departure_time", "")
        dep_min = _time_str_to_minutes(outbound_dep) if outbound_dep else None
        if dep_min is not None:
            leave_home_min = dep_min - TOTAL_ADVANCE_MIN
            wake_minutes = max(5 * 60, leave_home_min - MORNING_PREP_MIN)  # 不早于凌晨5:00
            wake_memo = f"⏰ 起床（{outbound_dep}{travel_info.get('outbound_type','')}出发，提前准备）"
        else:
            wh, wm = map(int, wake_time_str.split(":"))
            wake_minutes = wh * 60 + wm
            wake_memo = "⏰ 起床"
    else:
        # 非出行日：基于最早景点开门时间
        earliest_open = None
        earliest_open_name = ""
        for shop in visitable_shops:
            opentime_str = shop.get("opentime", "未知")
            hours = _parse_opentime(opentime_str, week_day)
            if hours:
                if earliest_open is None or hours["open"] < earliest_open:
                    earliest_open = hours["open"]
                    earliest_open_name = shop.get("name", "")
            else:
                if opentime_str not in ("", "未知") or shop.get("category", "") not in MEAL_CATS:
                    unknown_hours_shops.append(shop.get("name", ""))

        if earliest_open is not None:
            # 起床 = 最早开门 - 60min（洗漱+早餐+交通），但不早于 06:30
            wake_minutes = max(6 * 60 + 30, earliest_open - 60)
            wake_memo = f"⏰ 起床（{earliest_open_name}{earliest_open//60:02d}:{earliest_open%60:02d}开门）"
        else:
            wh, wm = map(int, wake_time_str.split(":"))
            wake_minutes = wh * 60 + wm
            wake_memo = "⏰ 起床"

    timeline.append({
        "time": f"{wake_minutes // 60:02d}:{wake_minutes % 60:02d}",
        "action": "WAKE_UP",
        "memo": wake_memo,
        "category": "wake_up",
        "shop_id": "",
        "duration_minutes": 0,
    })

    # ── 跨日换酒店退房（前一天 hotel_plan 指示 B/C 时触发）──
    if day_index > 0 and hotel_plan:
        prev_decision_idx = day_index - 1
        if prev_decision_idx < len(hotel_plan):
            prev_decision = hotel_plan[prev_decision_idx]
            if prev_decision.get("needs_change"):
                checkout_time = wake_minutes + 10
                timeline.append({
                    "time": f"{checkout_time // 60:02d}:{checkout_time % 60:02d}",
                    "action": "CHECK_OUT",
                    "memo": "🧳 办理退房 · 行李打包与托管（换住日专项准备）",
                    "category": "hotel",
                    "shop_id": "",
                    "duration_minutes": 20,
                })
                current_minutes = max(current_minutes, checkout_time + 20 + 15)
                wake_minutes = checkout_time + 20  # 更新起床后可用时间基准

    # ── 早餐插入：一律不自动排程，由用户手动添加 ──
    bf_meal = None  # 不再自动匹配早餐店
    bf_time = max(7 * 60, wake_minutes + 20)
    timeline.append({
        "time": f"{bf_time // 60:02d}:{bf_time % 60:02d}",
        "action": "BREAKFAST_NEEDED",
        "memo": "🥐 早餐（待添加）",
        "category": "breakfast",
        "shop_id": "",
        "duration_minutes": 45,
    })
    current_minutes = max(current_minutes, bf_time + 45 + 15)  # 推进到 09:00+
    _last_food_end_minutes = bf_time + 45  # 早餐结束时间

    # ── 餐厅-目的地绑定：每家正餐绑定到最近的非餐饮目的地 ──
    meal_bindings = _bind_meals_to_destinations(main_meal_shops, visitable_shops)

    # ── 按路线顺序遍历（正餐不参与VISIT循环）──
    lunch_assigned = False
    dinner_assigned = False
    deferred_dinner = None  # 延迟到 VISIT 后插入的晚餐
    last_shop_lat, last_shop_lng = None, None
    for idx, shop in enumerate(visitable_shops):
        cat = shop.get("category", "")
        dur = _get_duration(cat, shop)
        s_lat = shop.get("lat", 0)
        s_lng = shop.get("lng", 0)
        shop_name = shop.get('name', '')
        shop_id = shop.get('shop_id', '')

        # ── 基于绑定的午餐/晚餐插入 ──
        # 查找绑定到当前 VISIT 目的地的未分配正餐餐厅
        bound_meals = [m for m in main_meal_shops
                       if meal_bindings.get(m.get('shop_id'), {}).get('dest_name') == shop_name
                       and m.get('shop_id') not in used_meal_shop_ids]

        if bound_meals:
            # 判断当前 visit 的时段：上午→午餐，下午→晚餐
            visit_is_morning = current_minutes < 12 * 60

            for meal in bound_meals:
                if not lunch_assigned:
                    # 午餐：从候选时间中选惩罚最小的（软约束）
                    candidates = [
                        max(11 * 60, current_minutes - 60),
                        max(11 * 60, current_minutes),
                        current_minutes + 30,
                    ]
                    lunch_time = min(candidates, key=lambda t: meal_time_penalty("lunch", t))
                    meal_dur = _get_duration(meal.get("category", ""), meal)
                    timeline.append({
                        "time": f"{lunch_time // 60:02d}:{lunch_time % 60:02d}",
                        "action": "LUNCH",
                        "memo": f"🍽️ 午餐：{meal.get('name', '')}",
                        "category": meal.get("category", ""),
                        "shop_id": meal.get("shop_id", ""),
                        "duration_minutes": meal_dur,
                        "opentime": meal.get("opentime", "未知"),
                    })
                    current_minutes = max(current_minutes, lunch_time + meal_dur + 30)
                    _last_food_end_minutes = current_minutes
                    main_meal_shops.remove(meal)
                    used_meal_shop_ids.add(meal.get("shop_id"))
                    lunch_assigned = True
                    timeline.append({
                        "time": _safe_time_str(current_minutes),
                        "action": "REST", "memo": "☕ 午休片刻", "category": "rest",
                        "shop_id": "", "duration_minutes": 0,
                    })
                elif not dinner_assigned:
                    # 晚餐：延迟到 VISIT 之后再插入，避免把景点挤到晚上
                    deferred_dinner = meal
                    dinner_assigned = True

        # ── 小吃间隔检查（café类距上次进食≥90min）──
        if cat in SNACK_CATEGORIES and _last_food_end_minutes is not None:
            gap_needed = _last_food_end_minutes + 90
            if current_minutes < gap_needed:
                current_minutes = gap_needed

        # ── 活动间缓冲（region_cohesion_guard 多模态决策）──
        if len(timeline) > 0:
            if last_shop_lat and last_shop_lng:
                travel_m = _haversine_m(last_shop_lat, last_shop_lng, s_lat, s_lng)
                # 使用区域凝聚守卫进行多模态决策（天气 + 距离 + 用户交通偏好）
                pref = {"prefer_transport": transport} if transport else None
                cohesion = _region_cohesion_guard(travel_m, weather, pref)
                guard_transport = cohesion.get("transport", transport)
                speed = _get_speed(guard_transport)
                travel_min = max(5, round(travel_m / speed))
                # 远距离（>3km）：插入 10min taxi/等待缓冲（规格书 Phase 4.5）
                if travel_m > 3000:
                    timeline.append({
                        "time": _safe_time_str(current_minutes),
                        "action": "TRANSIT_BUFFER",
                        "memo": f"🚕 打车/等候车辆调度缓冲（{cohesion.get('reason', '长距离移动')}）",
                        "category": "transit",
                        "shop_id": "",
                        "duration_minutes": 10,
                    })
                    current_minutes += 10
                current_minutes += travel_min
            else:
                current_minutes += 15

        # ── 品类时间窗约束 ──
        if cat != "shopping":
            # 午餐：活动开始于 11:30-13:30 时推到 13:30（下午）
            if LUNCH_WINDOW[0] <= current_minutes < LUNCH_WINDOW[1]:
                current_minutes = max(current_minutes, LUNCH_WINDOW[1])

            # 非购物类白天约束：应在晚餐前完成，晚餐后留给购物/夜市
            if current_minutes >= DINNER_WINDOW[0]:
                # 标记 warning（正常情况不应走到这里，因为非购物优先排+晚餐延迟插入）
                closed_conflicts.append({
                    "shop_name": shop.get("name", ""),
                    "shop_id": shop.get("shop_id", ""),
                    "category": cat,
                    "visit_time": _safe_time_str(current_minutes),
                    "opentime": shop.get("opentime", "未知"),
                    "reason": (
                        f"非购物活动排在了晚间（{_safe_time_str(current_minutes)}），"
                        f"体验可能不佳"
                    ),
                    "type": "evening_non_shopping",
                })

        # ── 营业时间检查（仅记录警告，不跳过）──
        opentime_str = shop.get("opentime", "未知")
        hours = _parse_opentime(opentime_str, week_day)
        open_check = _check_open(hours, current_minutes, dur)

        if open_check["status"] == "after_close":
            # 店铺已关门，仍排入但追加警告
            closed_conflicts.append({
                "shop_name": shop.get("name", ""),
                "shop_id": shop.get("shop_id", ""),
                "category": cat,
                "visit_time": _safe_time_str(current_minutes),
                "opentime": opentime_str,
                "reason": open_check["message"] + "（仍排入行程，请留意）",
                "type": "business_hours_warning",
            })
            # 不 continue —— 所有目的地都必须排入

        if open_check["status"] == "before_open":
            # 推迟到开门时间
            suggested = open_check.get("suggested_time", current_minutes)
            current_minutes = suggested

        # ── bedtime 硬约束：活动结束时间超过 bedtime_cap 则不排入 ──
        visit_end = current_minutes + dur
        if visit_end > bedtime_cap:
            # 超出截止时间，不排入当日但保留为 unassigned（不丢弃）
            unassigned_shops.append({
                "shop_id": shop.get("shop_id", ""),
                "name": shop.get("name", ""),
                "category": cat,
                "lat": shop.get("lat", 0),
                "lng": shop.get("lng", 0),
                "rating": shop.get("rating", 0),
                "status": "未排入（超出当日时间）",
            })
            continue  # 跳过此 VISIT，不排入 timeline

        time_str = _safe_time_str(current_minutes)

        # ── 构建 memo ──
        memo = shop.get("name", "")
        if weather_alert and cat == "scenic":
            memo = f"{memo} {weather_alert}"
        if open_check["status"] != "ok":
            memo = f"{memo} {open_check['message']}"

        timeline.append({
            "time": time_str,
            "action": "VISIT",
            "memo": memo,
            "category": cat,
            "shop_id": shop.get("shop_id", ""),
            "duration_minutes": dur,
            "opentime": opentime_str,
        })
        current_minutes += dur + 10

        # ── 延迟晚餐插入：在 VISIT 之后（而非之前）──
        if deferred_dinner is not None:
            d_meal = deferred_dinner
            dinner_time = max(17 * 60, current_minutes + 30)
            if _last_food_end_minutes is not None:
                dinner_time = max(dinner_time, _last_food_end_minutes + 90)
            d_dur = _get_duration(d_meal.get("category", ""), d_meal)
            timeline.append({
                "time": f"{dinner_time // 60:02d}:{dinner_time % 60:02d}",
                "action": "DINNER",
                "memo": f"🍽️ 晚餐：{d_meal.get('name', '')}",
                "category": d_meal.get("category", ""),
                "shop_id": d_meal.get("shop_id", ""),
                "duration_minutes": d_dur,
                "opentime": d_meal.get("opentime", "未知"),
            })
            current_minutes = dinner_time + d_dur + 30
            _last_food_end_minutes = current_minutes
            main_meal_shops.remove(d_meal)
            used_meal_shop_ids.add(d_meal.get("shop_id"))
            deferred_dinner = None

        last_shop_lat, last_shop_lng = s_lat, s_lng
        # 小吃/饮品结束时间记录（用于后续间隔控制）
        if cat in SNACK_CATEGORIES:
            _last_food_end_minutes = current_minutes

    # ── 兜底：无餐厅时插入占位节点（确保三餐始终可见）──
    # 不再自动从候选池抓剩余餐厅填充——只用 meal binding 分配的餐厅，不够就显示待排程
    has_lunch = any(t.get("action") == "LUNCH" for t in timeline)
    has_dinner = any(t.get("action") == "DINNER" for t in timeline)
    if not has_lunch:
        timeline.append({
            "time": "12:00", "action": "LUNCH_NEEDED",
            "memo": "⚠️ 午餐（待补充）", "category": "lunch_needed",
            "shop_id": "", "duration_minutes": 60, "opentime": "未知",
        })
    if not has_dinner:
        timeline.append({
            "time": "18:00", "action": "DINNER_NEEDED",
            "memo": "⚠️ 晚餐（待补充）", "category": "dinner_needed",
            "shop_id": "", "duration_minutes": 60, "opentime": "未知",
        })

    # ── 弹性就寝时间 ──
    # 优先使用 bedtime_str 约束（返程日）；否则取最后活动+90min
    # 晚返程（>=21:00出发）：就寝推迟到 departure-150min 之后（不需要早睡）
    if bedtime_cap < 22 * 60:
        # 有返程约束：就寝在 bedtime_cap 后 30min（收拾行李）
        bedtime_minutes = bedtime_cap + 30
        # 晚返程（>=21:00）→ 就寝不早于 departure-60min（到家晚）
        if bedtime_cap >= 19 * 60 + 30:  # bedtime_cap = depart-150, 即 depart>=21:00
            bedtime_minutes = max(bedtime_cap + 30, bedtime_cap + 60)
        bedtime_memo = "🌙 就寝（次日返程）"
    else:
        bedtime_minutes = current_minutes + 90
        bedtime_minutes = max(22 * 60 + 30, bedtime_minutes)
        bedtime_memo = "🌙 就寝"

    timeline.append({
        "time": _safe_time_str(bedtime_minutes),
        "action": "BEDTIME",
        "memo": bedtime_memo,
        "category": "bedtime",
        "shop_id": "",
        "duration_minutes": 0,
    })

    # ── 收尾：保证至少有一个 VISIT ──
    has_visit = any(t.get("action") == "VISIT" for t in timeline)
    if not has_visit:
        current_minutes = start_h * 60 + start_m
        for shop in ordered_shops:
            cat = shop.get("category", "")
            dur = _get_duration(cat, shop)
            time_str = _safe_time_str(current_minutes)
            timeline.append({
                "time": time_str, "action": "VISIT",
                "memo": shop.get("name", ""),
                "category": cat,
                "shop_id": shop.get("shop_id", ""),
                "duration_minutes": dur,
                "opentime": shop.get("opentime", "未知"),
            })
            current_minutes += dur + 10

    # ── 排序：WAKE_UP 必须在最前、BEDTIME 必须在最后 ──
    # 先分离 WAKE_UP / BEDTIME，其余按时间排序，再组合
    wake_node = None
    bedtime_node = None
    rest_nodes = []
    for t in timeline:
        if t.get("action") == "WAKE_UP":
            wake_node = t
        elif t.get("action") == "BEDTIME":
            bedtime_node = t
        else:
            rest_nodes.append(t)
    rest_nodes.sort(key=lambda t: _time_to_minutes(t.get("time", "00:00")))
    timeline = []
    if wake_node:
        timeline.append(wake_node)
    timeline.extend(rest_nodes)
    if bedtime_node:
        timeline.append(bedtime_node)

    # ── 收集未排程的正餐店铺 → 供前端展示"待排程" ──
    unassigned_meals = []
    for m in main_meal_shops:
        unassigned_meals.append({
            "shop_id": m.get("shop_id", ""),
            "name": m.get("name", ""),
            "category": m.get("category", ""),
            "lat": m.get("lat", 0),
            "lng": m.get("lng", 0),
            "rating": m.get("rating", 0),
            "status": "待排程",
        })

    return {
        "timeline": timeline,
        "closed_conflicts": closed_conflicts,
        "unknown_hours_shops": unknown_hours_shops,
        "unassigned_meals": unassigned_meals,
        "unassigned_shops": unassigned_shops,  # 因 bedtime 约束未排入的店铺
    }


# ======================================================================
# Phase 4.5: 时间线状态机（显式状态推演，替代隐式指令流）
# ======================================================================

# 状态机状态定义
_TIMELINE_STATES = {
    "INIT": "初始化",
    "WAKE_UP": "起床准备",
    "CHECK_OUT": "退房（换住日专项）",
    "BREAKFAST": "早餐时段",
    "MORNING_LOOP": "上午活动遍历",
    "LUNCH_WINDOW": "午餐窗口",
    "LUNCH": "午餐",
    "AFTERNOON_LOOP": "下午活动遍历",
    "DINNER_WINDOW": "晚餐窗口",
    "DINNER": "晚餐",
    "EVENING_LOOP": "晚间活动遍历",
    "BEDTIME": "就寝",
    "DONE": "完成",
}

# 状态转移表：每个状态 → 下一个状态（确定性，不含条件分支）
_STATE_TRANSITIONS = {
    "INIT": "WAKE_UP",
    "WAKE_UP": "CHECK_OUT",
    "CHECK_OUT": "BREAKFAST",
    "BREAKFAST": "MORNING_LOOP",
    "LUNCH_WINDOW": "LUNCH",
    "LUNCH": "AFTERNOON_LOOP",
    "DINNER_WINDOW": "DINNER",
    "DINNER": "EVENING_LOOP",
    "EVENING_LOOP": "BEDTIME",
    "BEDTIME": "DONE",
}


def _timeline_state_machine(shops: list, start_time_str: str = "09:00",
                            transport: str = "步行优先", bedtime_str: str = "22:00",
                            week_day: int = 0, weather: dict = None,
                            wake_time_str: str = "07:30",
                            travel_info: dict = None) -> dict:
    """
    状态机推演时间线（Phase 4.5）。

    将隐式的指令流转为显式状态机，每个状态有明确的：
    - 进入条件（guard）
    - 状态行为（action）
    - 退出转移（transition）

    状态序列：
    INIT → WAKE_UP → BREAKFAST → MORNING_LOOP → LUNCH_WINDOW → LUNCH
         → AFTERNOON_LOOP → DINNER_WINDOW → DINNER → EVENING_LOOP → BEDTIME → DONE

    MORNING_LOOP / AFTERNOON_LOOP / EVENING_LOOP 内部循环消费 visitable shops，
    时间到达午餐/晚餐窗口时自动转移。

    Returns: 与 _build_timeline 相同的 dict 结构
    """
    # ── 初始化上下文 ──
    start_h, start_m = map(int, start_time_str.split(":"))
    current_minutes = start_h * 60 + start_m

    try:
        bed_h, bed_m = map(int, bedtime_str.split(":"))
        bedtime_cap = bed_h * 60 + bed_m
    except (ValueError, TypeError):
        bedtime_cap = 22 * 60

    # 品类分流
    ordered_shops = list(shops)
    main_meal_shops = [s for s in ordered_shops if s.get("category", "") in MAIN_MEAL_CATEGORIES]
    _all_visitable = [s for s in ordered_shops if s.get("category", "") not in MAIN_MEAL_CATEGORIES]
    day_only = [s for s in _all_visitable if s.get("suitable_time") == "day"]
    both_unknown = [s for s in _all_visitable if s.get("suitable_time") not in ("day", "night")]
    night_only = [s for s in _all_visitable if s.get("suitable_time") == "night"]
    visitable_shops = day_only + both_unknown + night_only

    timeline = []
    closed_conflicts = []
    unknown_hours_shops = []
    unassigned_shops = []

    # 天气
    weather_alert = None
    if weather:
        if not weather.get("outdoor_suitable", True):
            weather_alert = ("🌧️ 建议带伞" if weather.get("walking_penalty", 1.0) > 0.5
                           else "⛈️ 天气影响，注意安全")
        if weather.get("day_temp", 25) > 35:
            weather_alert = (weather_alert or "") + " 🔥 高温，注意防暑"

    # 用餐状态追踪
    lunch_assigned = False
    dinner_assigned = False
    deferred_dinner = None
    last_shop_lat, last_shop_lng = None, None
    visit_idx = 0  # 当前遍历到的 visitable index

    # 餐厅绑定
    meal_bindings = _bind_meals_to_destinations(main_meal_shops, visitable_shops)

    # ── 状态机主循环 ──
    state = "INIT"

    while state != "DONE":
        if state == "INIT":
            state = _STATE_TRANSITIONS[state]

        elif state == "WAKE_UP":
            # 智能起床时间
            if travel_info and travel_info.get("outbound_departure_time"):
                outbound_dep = travel_info.get("outbound_departure_time", "")
                dep_min = _time_str_to_minutes(outbound_dep) if outbound_dep else None
                if dep_min is not None:
                    leave_home_min = dep_min - TOTAL_ADVANCE_MIN
                    wake_minutes = max(5 * 60, leave_home_min - MORNING_PREP_MIN)
                    wake_memo = f"⏰ 起床（{outbound_dep}{travel_info.get('outbound_type','')}出发，提前准备）"
                else:
                    wh, wm = map(int, wake_time_str.split(":"))
                    wake_minutes = wh * 60 + wm
                    wake_memo = "⏰ 起床"
            else:
                earliest_open = None
                earliest_open_name = ""
                for shop in visitable_shops:
                    opentime_str = shop.get("opentime", "未知")
                    hours = _parse_opentime(opentime_str, week_day)
                    if hours:
                        if earliest_open is None or hours["open"] < earliest_open:
                            earliest_open = hours["open"]
                            earliest_open_name = shop.get("name", "")
                    else:
                        if opentime_str not in ("", "未知") or shop.get("category", "") not in MEAL_CATEGORIES:
                            unknown_hours_shops.append(shop.get("name", ""))
                if earliest_open is not None:
                    wake_minutes = max(6 * 60 + 30, earliest_open - 60)
                    wake_memo = f"⏰ 起床（{earliest_open_name}{earliest_open//60:02d}:{earliest_open%60:02d}开门）"
                else:
                    wh, wm = map(int, wake_time_str.split(":"))
                    wake_minutes = wh * 60 + wm
                    wake_memo = "⏰ 起床"

            timeline.append({
                "time": f"{wake_minutes // 60:02d}:{wake_minutes % 60:02d}",
                "action": "WAKE_UP", "memo": wake_memo, "category": "wake_up",
                "shop_id": "", "duration_minutes": 0,
            })
            state = _STATE_TRANSITIONS[state]

        elif state == "BREAKFAST":
            bf_time = max(7 * 60, wake_minutes + 20)
            timeline.append({
                "time": f"{bf_time // 60:02d}:{bf_time % 60:02d}",
                "action": "BREAKFAST_NEEDED", "memo": "🥐 早餐（待添加）",
                "category": "breakfast", "shop_id": "", "duration_minutes": 45,
            })
            current_minutes = max(current_minutes, bf_time + 45 + 15)
            _last_food_end_minutes = bf_time + 45
            state = _STATE_TRANSITIONS[state]

        elif state == "MORNING_LOOP":
            # 上午活动遍历：消费 visitable_shops[visit_idx:] 直到午餐时间
            state = _process_visit_loop(
                state, visitable_shops, main_meal_shops, meal_bindings,
                timeline, closed_conflicts, unassigned_shops,
                current_minutes, _last_food_end_minutes, used_meal_shop_ids,
                lunch_assigned, dinner_assigned, deferred_dinner,
                last_shop_lat, last_shop_lng, visit_idx,
                transport, bedtime_cap, week_day, weather_alert
            )
            # 解包上下文
            (state, visitable_shops, main_meal_shops, meal_bindings,
             timeline, closed_conflicts, unassigned_shops,
             current_minutes, _last_food_end_minutes, used_meal_shop_ids,
             lunch_assigned, dinner_assigned, deferred_dinner,
             last_shop_lat, last_shop_lng, visit_idx,
             transport, bedtime_cap, week_day, weather_alert) = state

            if state == "MORNING_LOOP":
                state = "LUNCH_WINDOW"

        elif state == "LUNCH_WINDOW":
            state = _STATE_TRANSITIONS[state]

        elif state == "LUNCH":
            # 午餐已在 MORNING_LOOP 中通过 meal_binding 插入
            if not lunch_assigned:
                # 兜底：无餐厅时插入占位
                timeline.append({
                    "time": "12:00", "action": "LUNCH_NEEDED",
                    "memo": "⚠️ 午餐（待补充）", "category": "lunch_needed",
                    "shop_id": "", "duration_minutes": 60, "opentime": "未知",
                })
            state = _STATE_TRANSITIONS[state]

        elif state == "AFTERNOON_LOOP":
            state = _process_visit_loop(
                state, visitable_shops, main_meal_shops, meal_bindings,
                timeline, closed_conflicts, unassigned_shops,
                current_minutes, _last_food_end_minutes, used_meal_shop_ids,
                lunch_assigned, dinner_assigned, deferred_dinner,
                last_shop_lat, last_shop_lng, visit_idx,
                transport, bedtime_cap, week_day, weather_alert
            )
            (state, visitable_shops, main_meal_shops, meal_bindings,
             timeline, closed_conflicts, unassigned_shops,
             current_minutes, _last_food_end_minutes, used_meal_shop_ids,
             lunch_assigned, dinner_assigned, deferred_dinner,
             last_shop_lat, last_shop_lng, visit_idx,
             transport, bedtime_cap, week_day, weather_alert) = state

            if state == "AFTERNOON_LOOP":
                state = "DINNER_WINDOW"

        elif state == "DINNER_WINDOW":
            state = _STATE_TRANSITIONS[state]

        elif state == "DINNER":
            if not dinner_assigned:
                timeline.append({
                    "time": "18:00", "action": "DINNER_NEEDED",
                    "memo": "⚠️ 晚餐（待补充）", "category": "dinner_needed",
                    "shop_id": "", "duration_minutes": 60, "opentime": "未知",
                })
            state = _STATE_TRANSITIONS[state]

        elif state == "EVENING_LOOP":
            state = _process_visit_loop(
                state, visitable_shops, main_meal_shops, meal_bindings,
                timeline, closed_conflicts, unassigned_shops,
                current_minutes, _last_food_end_minutes, used_meal_shop_ids,
                lunch_assigned, dinner_assigned, deferred_dinner,
                last_shop_lat, last_shop_lng, visit_idx,
                transport, bedtime_cap, week_day, weather_alert
            )
            (state, visitable_shops, main_meal_shops, meal_bindings,
             timeline, closed_conflicts, unassigned_shops,
             current_minutes, _last_food_end_minutes, used_meal_shop_ids,
             lunch_assigned, dinner_assigned, deferred_dinner,
             last_shop_lat, last_shop_lng, visit_idx,
             transport, bedtime_cap, week_day, weather_alert) = state

            if state == "EVENING_LOOP":
                state = "BEDTIME"

        elif state == "BEDTIME":
            # 弹性就寝时间
            if bedtime_cap < 22 * 60:
                bedtime_minutes = bedtime_cap + 30
                if bedtime_cap >= 19 * 60 + 30:
                    bedtime_minutes = max(bedtime_cap + 30, bedtime_cap + 60)
                bedtime_memo = "🌙 就寝（次日返程）"
            else:
                bedtime_minutes = current_minutes + 90
                bedtime_minutes = max(22 * 60 + 30, bedtime_minutes)
                bedtime_memo = "🌙 就寝"

            timeline.append({
                "time": _safe_time_str(bedtime_minutes),
                "action": "BEDTIME", "memo": bedtime_memo, "category": "bedtime",
                "shop_id": "", "duration_minutes": 0,
            })

            # 收尾：保证至少有一个 VISIT
            has_visit = any(t.get("action") == "VISIT" for t in timeline)
            if not has_visit:
                cur = start_h * 60 + start_m
                for shop in ordered_shops:
                    cat = shop.get("category", "")
                    dur = _get_duration(cat, shop)
                    timeline.append({
                        "time": _safe_time_str(cur), "action": "VISIT",
                        "memo": shop.get("name", ""), "category": cat,
                        "shop_id": shop.get("shop_id", ""),
                        "duration_minutes": dur, "opentime": shop.get("opentime", "未知"),
                    })
                    cur += dur + 10

            state = _STATE_TRANSITIONS[state]

    # ── 排序与收尾 ──
    wake_node = None
    bedtime_node = None
    rest_nodes = []
    for t in timeline:
        if t.get("action") == "WAKE_UP":
            wake_node = t
        elif t.get("action") == "BEDTIME":
            bedtime_node = t
        else:
            rest_nodes.append(t)
    rest_nodes.sort(key=lambda t: _time_to_minutes(t.get("time", "00:00")))
    timeline = []
    if wake_node:
        timeline.append(wake_node)
    timeline.extend(rest_nodes)
    if bedtime_node:
        timeline.append(bedtime_node)

    unassigned_meals = []
    for m in main_meal_shops:
        unassigned_meals.append({
            "shop_id": m.get("shop_id", ""),
            "name": m.get("name", ""),
            "category": m.get("category", ""),
            "lat": m.get("lat", 0),
            "lng": m.get("lng", 0),
            "rating": m.get("rating", 0),
            "status": "待排程",
        })

    return {
        "timeline": timeline,
        "closed_conflicts": closed_conflicts,
        "unknown_hours_shops": unknown_hours_shops,
        "unassigned_meals": unassigned_meals,
        "unassigned_shops": unassigned_shops,
    }


def _process_visit_loop(state_name: str, visitable_shops: list,
                        main_meal_shops: list, meal_bindings: dict,
                        timeline: list, closed_conflicts: list,
                        unassigned_shops: list, current_minutes: int,
                        _last_food_end_minutes, used_meal_shop_ids: set,
                        lunch_assigned: bool, dinner_assigned: bool,
                        deferred_dinner, last_shop_lat, last_shop_lng,
                        visit_idx: int, transport: str, bedtime_cap: int,
                        week_day: int, weather_alert):
    """
    状态机内部：遍历 visitable shops 的通用循环。
    由 MORNING_LOOP / AFTERNOON_LOOP / EVENING_LOOP 三个状态复用。

    Returns: 更新后的全部上下文（元组），最后一项为 next_state
    """
    # 解包可变类型以便修改
    tl = list(timeline)
    cc = list(closed_conflicts)
    us = list(unassigned_shops)
    cm = current_minutes
    lfe = _last_food_end_minutes
    umsi = set(used_meal_shop_ids)
    la = lunch_assigned
    da = dinner_assigned
    dd = deferred_dinner
    lslat, lslng = last_shop_lat, last_shop_lng
    mms = list(main_meal_shops)
    idx = visit_idx
    vs = list(visitable_shops)

    # 根据状态确定循环终止条件
    if state_name == "MORNING_LOOP":
        end_boundary = 11 * 60 + 30  # 午餐窗口开始
        preferred_cats = None  # 上午：非购物优先（已经排好）
    elif state_name == "AFTERNOON_LOOP":
        end_boundary = 17 * 60 + 30  # 晚餐窗口开始
        preferred_cats = None
    else:  # EVENING_LOOP
        end_boundary = bedtime_cap
        preferred_cats = {"shopping"}  # 晚间：购物优先

    while idx < len(vs):
        # 检查是否到达时间边界
        if cm >= end_boundary:
            break

        shop = vs[idx]
        cat = shop.get("category", "")
        suitable = shop.get("suitable_time")
        # ── 时间适配跳过逻辑 ──
        if state_name == "EVENING_LOOP" and suitable == "day":
            # day-only 活动跳过晚间循环，标记为延迟到次日
            idx += 1
            continue
        if state_name in ("MORNING_LOOP", "AFTERNOON_LOOP") and suitable == "night":
            # night-only 活动跳过白天循环，等待晚间
            idx += 1
            continue

        # ── 品类优先：当前时段有优先品类时，扫描前方优先处理 ──
        if preferred_cats and cat not in preferred_cats:
            # 在剩余店铺中寻找符合优先品类的店铺
            pref_found = False
            for ahead in range(idx + 1, min(idx + 6, len(vs))):
                ahead_shop = vs[ahead]
                ahead_cat = ahead_shop.get("category", "")
                ahead_suitable = ahead_shop.get("suitable_time")
                # 跳过不适配时段的活动
                if state_name == "EVENING_LOOP" and ahead_suitable == "day":
                    continue
                if ahead_cat in preferred_cats:
                    # 将优先品类店铺提前到当前位置
                    vs.insert(idx, vs.pop(ahead))
                    shop = vs[idx]
                    cat = shop.get("category", "")
                    suitable = shop.get("suitable_time")
                    pref_found = True
                    break
            if not pref_found:
                # 没找到优先品类，清除限制允许普通处理
                preferred_cats = None

        dur = _get_duration(cat, shop)
        s_lat = shop.get("lat", 0)
        s_lng = shop.get("lng", 0)
        shop_name = shop.get("name", "")
        shop_id = shop.get("shop_id", "")

        # ── 基于绑定的午餐/晚餐插入 ──
        bound_meals = [m for m in mms
                       if meal_bindings.get(m.get("shop_id"), {}).get("dest_name") == shop_name
                       and m.get("shop_id") not in umsi]

        if bound_meals:
            for meal in bound_meals:
                if not la:
                    candidates = [
                        max(11 * 60, cm - 60),
                        max(11 * 60, cm),
                        cm + 30,
                    ]
                    lunch_time = min(candidates, key=lambda t: meal_time_penalty("lunch", t))
                    meal_dur = _get_duration(meal.get("category", ""), meal)
                    tl.append({
                        "time": f"{lunch_time // 60:02d}:{lunch_time % 60:02d}",
                        "action": "LUNCH",
                        "memo": f"🍽️ 午餐：{meal.get('name', '')}",
                        "category": meal.get("category", ""),
                        "shop_id": meal.get("shop_id", ""),
                        "duration_minutes": meal_dur,
                        "opentime": meal.get("opentime", "未知"),
                    })
                    cm = max(cm, lunch_time + meal_dur + 30)
                    lfe = cm
                    mms.remove(meal)
                    umsi.add(meal.get("shop_id"))
                    la = True
                    tl.append({
                        "time": _safe_time_str(cm),
                        "action": "REST", "memo": "☕ 午休片刻", "category": "rest",
                        "shop_id": "", "duration_minutes": 0,
                    })
                elif not da:
                    dd = meal
                    da = True

        # ── 小吃间隔检查 ──
        if cat in SNACK_CATEGORIES and lfe is not None:
            gap_needed = lfe + 90
            if cm < gap_needed:
                cm = gap_needed

        # ── 活动间缓冲（region_cohesion_guard 多模态决策）──
        if len(tl) > 0:
            if lslat and lslng:
                travel_m = _haversine_m(lslat, lslng, s_lat, s_lng)
                # 使用区域凝聚守卫进行多模态决策（距离 + 用户交通偏好）
                pref = {"prefer_transport": transport} if transport else None
                cohesion = _region_cohesion_guard(travel_m, None, pref)
                guard_transport = cohesion.get("transport", transport)
                speed = _get_speed(guard_transport)
                travel_min = max(5, round(travel_m / speed))
                # 远距离（>3km）：插入 10min taxi/等待缓冲
                if travel_m > 3000:
                    cm += 10
                cm += travel_min
            else:
                cm += 15

        # ── 品类时间窗约束 ──
        if cat != "shopping":
            if LUNCH_WINDOW[0] <= cm < LUNCH_WINDOW[1]:
                cm = max(cm, LUNCH_WINDOW[1])
            if cm >= DINNER_WINDOW[0]:
                cc.append({
                    "shop_name": shop_name, "shop_id": shop_id, "category": cat,
                    "visit_time": _safe_time_str(cm), "opentime": shop.get("opentime", "未知"),
                    "reason": f"非购物活动排在了晚间（{_safe_time_str(cm)}），体验可能不佳",
                    "type": "evening_non_shopping",
                })

        # ── 营业时间检查 ──
        opentime_str = shop.get("opentime", "未知")
        hours = _parse_opentime(opentime_str, week_day)
        open_check = _check_open(hours, cm, dur)

        if open_check["status"] == "after_close":
            cc.append({
                "shop_name": shop_name, "shop_id": shop_id, "category": cat,
                "visit_time": _safe_time_str(cm), "opentime": opentime_str,
                "reason": open_check["message"] + "（仍排入行程，请留意）",
                "type": "business_hours_warning",
            })
        if open_check["status"] == "before_open":
            cm = open_check.get("suggested_time", cm)

        # ── bedtime 硬约束 ──
        visit_end = cm + dur
        if visit_end > bedtime_cap:
            us.append({
                "shop_id": shop_id, "name": shop_name, "category": cat,
                "lat": s_lat, "lng": s_lng, "rating": shop.get("rating", 0),
                "status": "未排入（超出当日时间）",
            })
            idx += 1
            continue

        # ── 构建 VISIT 节点 ──
        time_str = _safe_time_str(cm)
        memo = shop_name
        if weather_alert and cat == "scenic":
            memo = f"{memo} {weather_alert}"
        if open_check["status"] != "ok":
            memo = f"{memo} {open_check['message']}"

        tl.append({
            "time": time_str, "action": "VISIT", "memo": memo,
            "category": cat, "shop_id": shop_id,
            "duration_minutes": dur, "opentime": opentime_str,
        })
        cm += dur + 10

        # ── 延迟晚餐插入 ──
        if dd is not None:
            d_meal = dd
            dinner_time = max(17 * 60, cm + 30)
            if lfe is not None:
                dinner_time = max(dinner_time, lfe + 90)
            d_dur = _get_duration(d_meal.get("category", ""), d_meal)
            tl.append({
                "time": f"{dinner_time // 60:02d}:{dinner_time % 60:02d}",
                "action": "DINNER",
                "memo": f"🍽️ 晚餐：{d_meal.get('name', '')}",
                "category": d_meal.get("category", ""),
                "shop_id": d_meal.get("shop_id", ""),
                "duration_minutes": d_dur,
                "opentime": d_meal.get("opentime", "未知"),
            })
            cm = dinner_time + d_dur + 30
            lfe = cm
            mms.remove(d_meal)
            umsi.add(d_meal.get("shop_id"))
            dd = None

        lslat, lslng = s_lat, s_lng
        if cat in SNACK_CATEGORIES:
            lfe = cm

        idx += 1

    # ── 打包返回全部上下文 ──
    return (state_name, vs, mms, meal_bindings,
            tl, cc, us, cm, lfe, umsi,
            la, da, dd, lslat, lslng, idx,
            transport, bedtime_cap, week_day, weather_alert)


def _time_to_minutes(time_str: str) -> int:
    """ "HH:MM" → 分钟数 """
    try:
        h, m = map(int, time_str.split(":"))
        return h * 60 + m
    except (ValueError, TypeError):
        return 0


def _safe_time_str(minutes: float) -> str:
    """
    将分钟数安全格式化为 "HH:MM"，处理溢出午夜的情况。
    minutes >= 1440 时显示为 "次日 HH:MM"。
    """
    if minutes < 0:
        minutes = 0
    if minutes < 1440:
        h = int(minutes) // 60
        m = int(minutes) % 60
        return f"{h:02d}:{m:02d}"
    else:
        # 跨午夜：显示 "次日 HH:MM"
        remaining = int(minutes) - 1440
        h = remaining // 60
        m = remaining % 60
        return f"次日{h:02d}:{m:02d}"


def _has_breakfast_name(name: str) -> bool:
    """检查店铺名称是否含早餐类关键词（用于 category 被标为 restaurant 的早餐店识别）"""
    if not name:
        return False
    return any(kw in name for kw in BREAKFAST_NAME_KEYWORDS)


def _is_breakfast(shop: dict) -> bool:
    """判断是否为早餐类店铺（category + 名称关键词回退）"""
    cat = shop.get("category", "")
    # 明确：category 为 breakfast 的始终是早餐
    if cat == "breakfast":
        return True
    # 名称关键词回退：category 是正餐类但名称含早餐关键词 → 识别为早餐
    if cat in MAIN_MEAL_CATEGORIES:
        name = shop.get("name", "")
        if _has_breakfast_name(name):
            return True
        return False
    return False


# ======================================================================
# 营业时间解析工具
# ======================================================================

def _parse_opentime(opentime_str: str, week_day: int = 0) -> dict | None:
    """
    解析 Amap deep_info.opentime 字符串为分钟数。

    支持格式:
      - "10:00-22:00" → {open: 600, close: 1320}
      - "周一至周五 09:00-18:00; 周六,周日 10:00-20:00" → 根据 week_day 匹配
      - "未知" / "" / None / 无法解析 → None

    week_day: 0=周一, 6=周日 (Python datetime.weekday())
    """
    import re as _re
    if not opentime_str or opentime_str == "未知":
        return None

    time_pattern = _re.compile(r'(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})')

    # 尝试按分号拆分为多段（如 "周一至周五 09:00-18:00; 周六,周日 10:00-20:00"）
    segments = opentime_str.split(";")

    # 如果有多段，尝试匹配星期
    weekday_map = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}
    today_cn = weekday_map.get(week_day, "一")

    if len(segments) > 1:
        for seg in segments:
            seg = seg.strip()
            # 检查是否匹配今天
            # 模式: "周一至周五", "周六,周日", "周一至周日", "工作日", "周末"
            if any(kw in seg for kw in ["周一至周日", "每天", "全天"]):
                m = time_pattern.search(seg)
                if m:
                    return _make_hours(m)
                continue
            if ("周" + today_cn) in seg:
                m = time_pattern.search(seg)
                if m:
                    return _make_hours(m)
                continue
            if today_cn <= 4 and "周一至周五" in seg:
                m = time_pattern.search(seg)
                if m:
                    return _make_hours(m)
                continue
            if today_cn >= 5 and ("周六" in seg or "周日" in seg or "周末" in seg):
                m = time_pattern.search(seg)
                if m:
                    return _make_hours(m)
                continue
        # 多段中无匹配 → 尝试第一段的时间
        m = time_pattern.search(segments[0])
        if m:
            return _make_hours(m)
        return None

    # 单段模式：直接提取时间范围
    m = time_pattern.search(opentime_str)
    if m:
        return _make_hours(m)

    return None


def _make_hours(m) -> dict:
    """从正则匹配结果构建 {open, close} 分钟数"""
    open_h, open_m = int(m.group(1)), int(m.group(2))
    close_h, close_m = int(m.group(3)), int(m.group(4))
    return {
        "open": open_h * 60 + open_m,
        "close": close_h * 60 + close_m,
    }


def _check_open(hours: dict | None, visit_start_minutes: int,
                visit_duration: int = 60) -> dict:
    """
    检查 visit_start_minutes 是否在营业时间范围内。

    返回: {status, message}
      status: "ok" | "before_open" | "after_close" | "unknown"
    """
    if hours is None:
        return {"status": "unknown", "message": "🕐 营业时间未知"}

    open_t = hours["open"]
    close_t = hours["close"]
    visit_end = visit_start_minutes + visit_duration

    if visit_start_minutes < open_t:
        return {
            "status": "before_open",
            "message": f"⚠️ 尚未开门（{open_t//60:02d}:{open_t%60:02d}开门），已推迟",
            "suggested_time": open_t,
        }
    if visit_start_minutes >= close_t:
        return {
            "status": "after_close",
            "message": f"🚫 到达时已关门（{close_t//60:02d}:{close_t%60:02d}关门）",
        }
    if visit_end > close_t:
        return {
            "status": "close_soon",
            "message": f"⏰ 注意：需在{close_t//60:02d}:{close_t%60:02d}前离开",
        }

    return {"status": "ok", "message": ""}


# ======================================================================
# 阶段 5: 全局微调 —— 跨天边界交换
# ======================================================================

def _global_fine_tune(clusters: list, checkin_lat: float, checkin_lng: float) -> list:
    """
    检查每天边界店铺：如果某个店铺离邻天质心比自己天质心更近，
    尝试移动它，若总方差减小则接受。
    """
    if len(clusters) <= 1:
        return clusters

    for _ in range(3):  # 最多 3 轮
        improved = False
        centroids = [_cluster_centroid(c) for c in clusters]

        for i in range(len(clusters)):
            if centroids[i] is None:
                continue
            for j in range(len(clusters)):
                if i == j or centroids[j] is None:
                    continue
                # 检查第 i 天的每个店铺
                for shop in list(clusters[i]):
                    lat = float(shop.get("lat", 0))
                    lng = float(shop.get("lng", 0))
                    d_to_own = _haversine_m(lat, lng, centroids[i][0], centroids[i][1])
                    d_to_other = _haversine_m(lat, lng, centroids[j][0], centroids[j][1])

                    # 如果离邻天更近（使用空间方差裁决：优先地理连贯性）
                    if d_to_other < d_to_own:
                        # 计算移动前后的空间方差变化（衡量地理紧凑度）
                        old_var = _calc_spatial_variance(clusters)
                        clusters[i].remove(shop)
                        clusters[j].append(shop)
                        new_var = _calc_spatial_variance(clusters)

                        if new_var < old_var:
                            improved = True
                            centroids = [_cluster_centroid(c) for c in clusters]
                            break
                        else:
                            # 恢复
                            clusters[j].remove(shop)
                            clusters[i].append(shop)
                if improved:
                    break
            if improved:
                break

        if not improved:
            break

    return clusters


def _calc_cluster_variance(clusters: list) -> float:
    """计算簇间时间方差（越小越均衡）"""
    times = []
    for c in clusters:
        t = sum(_get_duration(s.get("category", ""), s) for s in c) + len(c) * 15
        times.append(t)
    if not times:
        return 0
    mean = sum(times) / len(times)
    return sum((t - mean) ** 2 for t in times) / len(times)


def _calc_spatial_variance(clusters: list) -> float:
    """计算簇内空间方差（衡量地理紧凑度，越小越紧凑）。
    每个簇：所有店铺到质心的平均距离平方和。
    空间方差小 = 地理上更紧凑 = 更好的聚类质量。"""
    total = 0.0
    n = 0
    for c in clusters:
        if not c:
            continue
        centroid = _cluster_centroid(c)
        if centroid is None:
            continue
        for s in c:
            lat = float(s.get("lat", 0))
            lng = float(s.get("lng", 0))
            d = _haversine_m(lat, lng, centroid[0], centroid[1])
            total += d * d
            n += 1
    if n == 0:
        return 0
    return total / n


# ======================================================================
# 主入口
# ======================================================================

def _time_str_to_minutes(t: str) -> int:
    """将 "HH:MM" 转换为分钟数"""
    try:
        parts = t.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError, AttributeError):
        return 0


def _compute_day1_start(travel_info: dict = None, hotel_lat: float = None, hotel_lng: float = None):
    """根据酒店到达时间判断 Day 1 排程规则（双阈值：13:00白天/18:00夜间）。

    规则：
      - 酒店到达 < 13:00（下午1点前）→ 下午可排白天活动 + 晚间可排夜间活动
      - 13:00 <= 酒店到达 < 18:00（下午6点前）→ 下午不可排白天活动，但晚间可排夜间活动
      - 酒店到达 >= 18:00 → 全天跳过，不可排任何活动
      - 无到达时间 → 正常 09:00 开始

    返回: (should_skip_day1: bool, effective_start: str or None,
           station_to_hotel_min: int or None,
           afternoon_ok: bool, evening_ok: bool)
    """
    if not travel_info:
        return (False, "09:00", None, True, True)
    arrival_str = travel_info.get("outbound_arrival_time", "")
    if not arrival_str:
        return (False, "09:00", None, True, True)
    try:
        parts = arrival_str.split(":")
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, TypeError):
        return (False, "09:00", None, True, True)
    arrival_min = h * 60 + m

    # ── 动态缓冲计算 ──
    outbound_type = travel_info.get("outbound_type", "")
    arrival_station = travel_info.get("arrival_station", "")

    # 出站缓冲：飞机 30min，火车 15min
    exit_buffer = 30 if outbound_type == "飞机" else 15

    # 站点到酒店交通时间
    station_to_hotel_min = 0
    station_coord = _lookup_station_coord(arrival_station)
    if station_coord and hotel_lat is not None and hotel_lng is not None:
        dist_m = _haversine_m(station_coord[0], station_coord[1], hotel_lat, hotel_lng)
        station_to_hotel_min = max(10, int(dist_m / 667) + 1)
    else:
        # 无坐标时估算：机场 45min，火车站 20min
        station_to_hotel_min = 45 if outbound_type == "飞机" else 20

    # 酒店入住缓冲：30min
    checkin_buffer = 30
    # 休整缓冲：30min（放行李、换衣服等）
    settle_buffer = 30

    # ── 酒店到达时间 = 交通到达 + 出站 + 去酒店 + 入住 ──
    hotel_arrival_min = arrival_min + exit_buffer + station_to_hotel_min + checkin_buffer

    # ── 双阈值判定 ──
    AFTERNOON_THRESHOLD = 13 * 60  # 下午1点
    EVENING_THRESHOLD = 18 * 60    # 下午6点
    EVENING_START = 17 * 60 + 30   # 傍晚5:30（夜间活动最早开始时间）

    afternoon_ok = hotel_arrival_min < AFTERNOON_THRESHOLD
    evening_ok = hotel_arrival_min < EVENING_THRESHOLD

    if not afternoon_ok and not evening_ok:
        # 酒店到达 >= 18:00 → 全天跳过
        return (True, None, None, False, False)

    if afternoon_ok:
        # 下午可排白天活动，起始时间 >= 13:00
        start_min = max(hotel_arrival_min + settle_buffer, 13 * 60)
    else:
        # 仅夜间可排，起始时间取 max(安顿好, 傍晚17:30)
        start_min = max(hotel_arrival_min + settle_buffer, EVENING_START)

    start_h, start_m = start_min // 60, start_min % 60
    return (False, f"{start_h:02d}:{start_m:02d}", station_to_hotel_min, afternoon_ok, evening_ok)


def _compute_last_day_end(travel_info: dict = None):
    """根据返程时间倒推最后一天的行程结束上限 + 上午排程可行性。

    规则：
      - 统一提前 150min（家到站点90min + 安检堵车缓冲60min）作为活动截止时间
      - 从起床吃完早饭(8:00)后，完成典型上午活动(3h)+返回酒店(30min)+去站点+安检
        能否在出发时间前完成 → morning_feasible
      - 无返程信息 → 默认 22:00, morning_feasible=True

    返回: (effective_end_time: str, morning_feasible: bool, must_leave_hotel: str or None)
    """
    if not travel_info:
        return ("22:00", True, None)
    return_dep = travel_info.get("return_departure_time", "")
    if not return_dep:
        return ("22:00", True, None)
    try:
        parts = return_dep.split(":")
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, TypeError):
        return ("22:00", True, None)
    dep_min = h * 60 + m

    # ── 活动截止时间（必须在出发前 TOTAL_ADVANCE_MIN 离开酒店）──
    end_min = max(0, dep_min - TOTAL_ADVANCE_MIN)
    end_h, end_m = end_min // 60, end_min % 60
    effective_end = f"{end_h:02d}:{end_m:02d}"

    # ── 上午排程可行性校验 ──
    # 早餐结束时间（起床7:00 + 洗漱早餐60min）
    breakfast_end = 8 * 60  # 08:00
    # 典型上午活动时长（3小时）
    typical_morning_activity = 180
    # 返回酒店取行李
    return_to_hotel = 30
    # 酒店到站点 + 安检缓冲
    return_type = travel_info.get("return_type", "")
    security_buffer = 60 if return_type == "飞机" else 30
    # 酒店到站点交通（估算）
    return_station = travel_info.get("return_station", "")
    station_coord = _lookup_station_coord(return_station)
    if station_coord:
        hotel_to_station = 60  # 默认估算
    else:
        hotel_to_station = 60 if return_type == "飞机" else 30

    # 上午活动完成需要的总时间
    morning_end_min = breakfast_end + typical_morning_activity + return_to_hotel + hotel_to_station + security_buffer
    morning_feasible = morning_end_min <= dep_min

    # 必须离开酒店的时间
    must_leave_min = dep_min - hotel_to_station - security_buffer
    must_leave_h, must_leave_m = max(0, must_leave_min) // 60, max(0, must_leave_min) % 60
    must_leave_hotel = f"{must_leave_h:02d}:{must_leave_m:02d}"

    return (effective_end, morning_feasible, must_leave_hotel)


def _build_travel_day_timeline(travel_info: dict, hotel_lat: float = None, hotel_lng: float = None) -> list:
    """构建 Day 1（出行日）的完整门到门时间线。

    生成节点序列：
      WAKE_UP → LEAVE_HOME → TO_STATION → OUTBOUND_JOURNEY → ARRIVAL → ARRIVAL_TRANSIT → HOTEL_CHECKIN

    时间逻辑：
      - 出门时间 = 交通出发 - 150min（家到站点90min + 安检60min）
      - 起床时间 = 出门时间 - 45min（洗漱收拾）
      - 赶早班机自动提前起床

    用于 Day 1 被跳过（下午到达）或作为上午到达 Day 1 的前置节点。
    """
    if not travel_info:
        return [{"time": "09:00", "action": "TRAVEL_DAY", "memo": "出行日",
                 "category": "travel", "shop_id": "", "duration_minutes": 0}]

    outbound_type = travel_info.get("outbound_type", "")
    outbound_dep = travel_info.get("outbound_departure_time", "")
    outbound_arrival = travel_info.get("outbound_arrival_time", "")
    arrival_station = travel_info.get("arrival_station", "")
    departure_city = travel_info.get("departure_city", "")

    timeline = []
    dep_minutes = _time_str_to_minutes(outbound_dep) if outbound_dep else 8 * 60

    # 统一时间常量
    station_buffer = STATION_SECURITY_BUFFER   # 60min
    home_to_station = HOME_TO_STATION_MIN       # 90min
    leave_home_min = dep_minutes - TOTAL_ADVANCE_MIN  # 出发 - 150min
    wake_min = leave_home_min - MORNING_PREP_MIN      # 出门 - 45min

    # 0. 起床（赶早班机自动提前，由出发时间倒推计算）
    transport_icon = '✈️' if outbound_type == '飞机' else '🚄'
    timeline.append({
        "time": f"{wake_min // 60:02d}:{wake_min % 60:02d}",
        "action": "WAKE_UP",
        "memo": f"⏰ 起床（{transport_icon} {outbound_type}{outbound_dep}出发，{departure_city or '出发城市'}）",
        "category": "wake_up",
        "shop_id": "",
        "duration_minutes": 0,
    })

    # 1. 从家出发
    if leave_home_min > 0:
        timeline.append({
            "time": f"{leave_home_min // 60:02d}:{leave_home_min % 60:02d}",
            "action": "LEAVE_HOME",
            "memo": f"🏠 从家出发（{departure_city or '出发城市'} → 约{home_to_station}分钟到站点）",
            "category": "travel",
            "shop_id": "",
            "duration_minutes": 0,
        })

    # 2. 到达出发站点（需提前60min进站安检）
    arrive_station_min = dep_minutes - station_buffer
    if arrive_station_min > 0:
        station_icon = '✈️ 机场' if outbound_type == '飞机' else '🚄 车站'
        timeline.append({
            "time": f"{arrive_station_min // 60:02d}:{arrive_station_min % 60:02d}",
            "action": "TO_STATION",
            "memo": f"🚗 到达出发{station_icon} · 安检/进站（提前{station_buffer}分钟）",
            "category": "travel",
            "shop_id": "",
            "duration_minutes": station_buffer,
        })

    # 3. 去程交通（飞机/高铁）
    if outbound_dep:
        timeline.append({
            "time": outbound_dep,
            "action": "OUTBOUND_JOURNEY",
            "memo": f"{'✈️' if outbound_type == '飞机' else '🚄'} {outbound_type}{outbound_dep}出发",
            "category": "travel",
            "shop_id": "",
            "duration_minutes": 0,
        })

    # 4. 到达目的地站点
    if outbound_arrival:
        timeline.append({
            "time": outbound_arrival,
            "action": "ARRIVAL",
            "memo": f"🛬 到达{arrival_station or '目的地'}",
            "category": "travel",
            "shop_id": "",
            "duration_minutes": 0,
        })

    # 5. 从站点去酒店
    station_coord = _lookup_station_coord(arrival_station)
    transit_min = 30  # 默认
    if station_coord and hotel_lat is not None and hotel_lng is not None:
        dist_m = _haversine_m(station_coord[0], station_coord[1], hotel_lat, hotel_lng)
        transit_min = max(10, int(dist_m / 667) + 1)
    elif outbound_type == "飞机":
        transit_min = 45

    arrival_min = _time_str_to_minutes(outbound_arrival) if outbound_arrival else 12 * 60
    hotel_arrival_min = arrival_min + 30 + transit_min  # 出站30min + 交通
    timeline.append({
        "time": f"{hotel_arrival_min // 60:02d}:{hotel_arrival_min % 60:02d}",
        "action": "ARRIVAL_TRANSIT",
        "memo": f"🚗 从{'站点' if not arrival_station else arrival_station}前往酒店（约{transit_min}分钟）",
        "category": "travel",
        "shop_id": "",
        "duration_minutes": transit_min,
    })

    # 6. 办理入住
    checkin_min = hotel_arrival_min + transit_min
    timeline.append({
        "time": f"{checkin_min // 60:02d}:{checkin_min % 60:02d}",
        "action": "HOTEL_CHECKIN",
        "memo": "🏨 办理入住 · 安顿行李",
        "category": "hotel",
        "shop_id": "",
        "duration_minutes": 30,
    })

    # 按时间排序
    timeline.sort(key=lambda t: _time_str_to_minutes(t.get("time", "00:00")))
    return timeline


def _build_return_timeline(travel_info: dict, hotel_lat: float = None, hotel_lng: float = None) -> list:
    """构建最后一天返程链路：TO_STATION → DEPARTURE → RETURN_JOURNEY → ARRIVE_HOME。

    时间逻辑：
      - 离开酒店 = 返程出发 - 150min（酒店到站点90min + 安检60min）
      - 晚返程（>=21:00）自动推迟出发

    在最后一天的 timeline 末尾追加，确保用户能看到完整的返程规划。
    """
    if not travel_info:
        return []

    return_type = travel_info.get("return_type", "")
    return_dep = travel_info.get("return_departure_time", "")
    return_station = travel_info.get("return_station", "")
    departure_city = travel_info.get("departure_city", "")

    timeline = []
    dep_minutes = _time_str_to_minutes(return_dep) if return_dep else 18 * 60

    # 统一时间常量
    station_buffer = STATION_SECURITY_BUFFER   # 60min
    transit_min = HOME_TO_STATION_MIN           # 90min（默认酒店→站点）

    # 如能查到坐标，用实际距离计算
    station_coord = _lookup_station_coord(return_station)
    if station_coord and hotel_lat is not None and hotel_lng is not None:
        dist_m = _haversine_m(hotel_lat, hotel_lng, station_coord[0], station_coord[1])
        transit_min = max(10, int(dist_m / 667) + 1)

    leave_hotel_min = dep_minutes - TOTAL_ADVANCE_MIN  # 出发 - 150min

    # 晚返程（>=21:00）：标记推迟
    is_late_return = dep_minutes >= 21 * 60
    late_memo_suffix = "（晚返程）" if is_late_return else ""

    # 1. 离开酒店前往站点
    if leave_hotel_min > 0:
        timeline.append({
            "time": f"{leave_hotel_min // 60:02d}:{leave_hotel_min % 60:02d}",
            "action": "TO_STATION",
            "memo": f"🚗 从酒店前往{return_station or '返程站点'}（约{transit_min}分钟）{late_memo_suffix}",
            "category": "travel",
            "shop_id": "",
            "duration_minutes": transit_min,
        })

    # 2. 到达站点（需提前60min安检进站）
    arrive_station_min = dep_minutes - station_buffer
    if arrive_station_min > 0:
        station_icon = '✈️ 机场' if return_type == '飞机' else '🚄 车站'
        timeline.append({
            "time": f"{arrive_station_min // 60:02d}:{arrive_station_min % 60:02d}",
            "action": "DEPARTURE",
            "memo": f"🔙 到达{return_station or '站点'} · {station_icon}安检进站（{return_type}{return_dep}返程）",
            "category": "travel",
            "shop_id": "",
            "duration_minutes": station_buffer,
        })

    # 3. 返程交通
    if return_dep:
        timeline.append({
            "time": return_dep,
            "action": "RETURN_JOURNEY",
            "memo": f"{'✈️' if return_type == '飞机' else '🚄'} {return_type}{return_dep}出发 · 返程",
            "category": "travel",
            "shop_id": "",
            "duration_minutes": 0,
        })

    # 4. 到家（估算返程耗时 + 站点到家90min）
    if return_type == "飞机":
        journey_hours = 3
    elif return_type == "高铁":
        journey_hours = 5
    else:
        journey_hours = 8
    arrive_home_min = dep_minutes + journey_hours * 60 + HOME_TO_STATION_MIN
    timeline.append({
        "time": f"{arrive_home_min // 60:02d}:{arrive_home_min % 60:02d}",
        "action": "ARRIVE_HOME",
        "memo": f"🏠 到家（{departure_city or '出发城市'}）",
        "category": "travel",
        "shop_id": "",
        "duration_minutes": 0,
    })

    return timeline


def _match_and_apply_template(poi_shops: list, templates: list, num_days: int) -> list:
    """
    将候选池中的 POI 景点与用户保存的排程模板进行匹配。
    全部 match_spots 匹配成功时返回预分配的 clusters，否则返回 None。

    模糊匹配：双向子串包含（"故宫" 可匹配 "故宫博物院"）。
    额外景点分配到最空闲的天，无空天时从最满的天匀一个景点过去。
    """
    if not templates or not poi_shops:
        return None

    candidate_names = [s.get("name", "") for s in poi_shops]

    for template in templates:
        match_spots = template.get("match_spots", [])
        if not match_spots:
            continue

        # 全部 match_spots 必须在候选池中找到（模糊匹配）
        matched = True
        for t_spot in match_spots:
            found = False
            for c_name in candidate_names:
                if t_spot in c_name or c_name in t_spot:
                    found = True
                    break
            if not found:
                matched = False
                break

        if not matched:
            continue

        # ── 模板匹配成功，构建预分配 clusters ──
        clusters = [[] for _ in range(num_days)]

        # 构建模板景点名 → 候选 shop 的映射
        name_to_shop = {}
        for shop in poi_shops:
            s_name = shop.get("name", "")
            for t_spot in match_spots:
                if t_spot in s_name or s_name in t_spot:
                    name_to_shop[t_spot] = shop
                    break

        # 按模板将景点分配到对应天
        for key, spot_list in template.items():
            if not (key.startswith("day_") or key == "last_day_spots"):
                continue

            if key == "last_day_spots":
                day_idx = num_days - 1
            else:
                try:
                    day_num = int(key.split("_")[1])
                    day_idx = day_num - 1
                except (IndexError, ValueError):
                    continue

            if day_idx < 0:
                continue
            # 天数不足时，超出部分 clamp 到最后一天
            if day_idx >= num_days:
                day_idx = num_days - 1

            for spot_name in (spot_list if isinstance(spot_list, list) else []):
                shop = name_to_shop.get(spot_name.strip())
                if shop is not None:
                    clusters[day_idx].append(shop)

        # 处理未在模板中的额外景点 → 分配到最空闲的天
        assigned_names = set()
        for cluster in clusters:
            for s in cluster:
                assigned_names.add(s.get("name", ""))
        unassigned = [s for s in poi_shops if s.get("name", "") not in assigned_names]
        for shop in unassigned:
            emptiest_day = min(range(num_days), key=lambda d: len(clusters[d]))
            clusters[emptiest_day].append(shop)

        # 确保没有空 cluster（从最满的天匀一个）
        for i in range(num_days):
            if not clusters[i]:
                fullest = max(range(num_days), key=lambda d: len(clusters[d]))
                if len(clusters[fullest]) > 1:
                    clusters[i].append(clusters[fullest].pop())

        print(f"[template_match] Matched template '{template.get('template_id')}', "
              f"pre-assigned {sum(len(c) for c in clusters)} shops to {num_days} days", flush=True)

        return clusters

    return None


def solve_multi_day(
    candidate_shops: list,
    num_days: int,
    checkin_lat: float = None,
    checkin_lng: float = None,
    transport_preference: str = "步行优先",
    start_time_str: str = "09:00",
    max_hours_per_day: float = 8.0,
    weather_data: dict = None,
    preferences: dict = None,
    travel_info: dict = None,
    travel_preference: str = "公共交通",
    dynamic_durations: dict = None,
    fatigue_weights: dict = None,
    suitable_times: dict = None,
    geocode_callback=None,
) -> dict:
    """
    多日行程智能排程主入口。

    参数:
        candidate_shops: [{"shop_id", "name", "category", "lat", "lng", "coord", ...}, ...]
        num_days: 计划天数
        checkin_lat, checkin_lng: 酒店坐标（可选，Phase 2 动态酒店决策后注入）
        transport_preference: 交通方式
        start_time_str: 每天开始时间 "09:00"
        max_hours_per_day: 每天最大活动小时数
        weather_data: {"2026-07-15": {day_weather, day_temp, walking_penalty, outdoor_suitable}, ...}
        preferences: {"commute": {walking_tolerance_meters, transport_priority}, "taste": {cuisine_preference}, ...}
        travel_info: {"outbound_arrival_time", "return_departure_time", "outbound_type", "return_type", ...}
        travel_preference: "公共交通" 或 "打车"，超出步行范围时的出行方式
        dynamic_durations: {shop_id: minutes} LLM 动态时长字典（优先级高于 CATEGORY_DURATIONS）
        fatigue_weights: {shop_id: weight} LLM 疲劳系数权重（1-10，用于疲劳度计算）

    返回:
        {
            "days": [
                {
                    "day_index": 0,
                    "label": "第1天",
                    "pairs": [(cat, shop_id, name), ...],
                    "timeline": [{time, action, memo, ...}, ...],
                    "total_duration_minutes": int,
                    "total_travel_minutes": int,
                    "route": [(lat, lng), ...],
                    "task_list": [...],
                    "spatial_matrix": {...},
                },
                ...
            ],
            "unassigned": [],
            "algorithm_metadata": {
                "cluster_method": "kmeans++",
                "balance_variance": float,
                "total_cost_km": float,
            }
        }
    """
    if not candidate_shops:
        return {"days": [], "unassigned": [], "algorithm_metadata": {},
                "pending_user_confirmation_meals": [], "hotel_plan": []}

    if num_days < 1:
        num_days = 1
    if num_days > len(candidate_shops):
        num_days = len(candidate_shops)  # 每天至少一个

    # ── Phase 0: 坐标兜底防御 + LLM 常识时序注入 ──
    # 确保所有 shop 有 lat/lng（Never-Crash：不崩溃）
    arrival_lat = travel_info.get("arrival_station_lat", None) if travel_info else None
    arrival_lng = travel_info.get("arrival_station_lng", None) if travel_info else None
    _ensure_coords(candidate_shops, arrival_lat, arrival_lng, geocode_callback=geocode_callback)

    # 注入 LLM 常识数据：dynamic_durations + fatigue_weights
    if dynamic_durations:
        for s in candidate_shops:
            sid = s.get("shop_id", "")
            if sid in dynamic_durations and "duration_minutes" not in s:
                s["duration_minutes"] = dynamic_durations[sid]
    if fatigue_weights:
        for s in candidate_shops:
            sid = s.get("shop_id", "")
            if sid in fatigue_weights:
                s["fatigue_weight"] = fatigue_weights[sid]

    # 注入 LLM 常识数据：suitable_time（白天/夜间适配）
    if suitable_times:
        for s in candidate_shops:
            sid = s.get("shop_id", "")
            if sid in suitable_times:
                s["suitable_time"] = suitable_times[sid]

    # ── Phase 0.5: 餐饮前置就近绑定 + 20km 强拦截 ──
    poi_shops, pending_meals = _pre_bind_meals_and_filter(candidate_shops)

    # 展开 bound_meals 回 candidate_shops 用于后续处理
    # POI + bound_meals 视为完整 shops 集合
    expanded_shops = list(poi_shops)
    for p in poi_shops:
        for m in p.get("bound_meals", []):
            expanded_shops.append(m)

    # 如果没有酒店坐标，用 POI 的几何中心作为虚拟锚点
    _hotel_lat = checkin_lat if checkin_lat is not None else (
        sum(float(s.get("lat", 0)) for s in poi_shops) / max(len(poi_shops), 1) if poi_shops else 0.0
    )
    _hotel_lng = checkin_lng if checkin_lng is not None else (
        sum(float(s.get("lng", 0)) for s in poi_shops) / max(len(poi_shops), 1) if poi_shops else 0.0
    )
    # 向下兼容：统一用回 checkin_lat/checkin_lng 作为当前锚点引用
    checkin_lat = _hotel_lat
    checkin_lng = _hotel_lng

    # ── Phase 0.8: 景点排程模板匹配 ──
    pre_defined_clusters = None
    templates = (preferences or {}).get("itinerary_templates", [])
    if templates and poi_shops:
        pre_defined_clusters = _match_and_apply_template(poi_shops, templates, num_days)

    # ── 阶段 1: 地理聚类（仅 POI，餐厅作为挂件跟随）──
    if pre_defined_clusters is not None:
        clusters = pre_defined_clusters
        # 模板匹配成功，跳过全局微调（用户显式覆盖优先）
    else:
        clusters = _cluster_by_geo(poi_shops, num_days)
        # ── 阶段 1.5: 全局微调（跨天边界交换，修正聚类边界错误）──
        clusters = _global_fine_tune(clusters, _hotel_lat, _hotel_lng)

    # ── 阶段 2: 负载均衡 ──
    # 模板匹配时跳过负载均衡和微调，保留用户显式指定的分天方案
    if pre_defined_clusters is None:
        # 提前计算 Day 1 到达和最后一天返程约束，用于差异化每日目标时间
        skip_day1, day1_start, station_to_hotel_min, afternoon_ok, evening_ok = _compute_day1_start(
            travel_info, hotel_lat=_hotel_lat, hotel_lng=_hotel_lng)
        last_day_end, last_day_morning_feasible, must_leave_hotel = _compute_last_day_end(travel_info)

        # 构建每日有效可用小时数（Day 1 晚到 / Day N 早走会影响实际可排程时间）
        _day_hours = []
        for _di in range(num_days):
            if _di == 0 and day1_start:
                eff_end = 22 * 60
                eff_start = _time_str_to_minutes(day1_start)
                _day_hours.append(max(1.0, (eff_end - eff_start) / 60))
            elif _di == num_days - 1 and last_day_end:
                eff_end = _time_str_to_minutes(last_day_end)
                eff_start = 9 * 60
                _day_hours.append(max(1.0, (eff_end - eff_start) / 60))
            else:
                _day_hours.append(max_hours_per_day)

        clusters = _balance_clusters(clusters, max_hours_per_day, max_scenic_per_day=5,
                                      transport_preference=transport_preference,
                                      day_hours=_day_hours)
        # ── 阶段 2.1: 均衡后重新微调，修复可能引入的边界错误 ──
        clusters = _global_fine_tune(clusters, _hotel_lat, _hotel_lng)
    else:
        # 模板匹配成功，跳过负载均衡和微调，设置默认值
        skip_day1, day1_start = False, "18:00"  # 模板显式安排了 Day 1，傍晚开始（入住后）
        station_to_hotel_min, afternoon_ok, evening_ok = 30, True, True
        last_day_end, last_day_morning_feasible = "18:00", True
        must_leave_hotel = "14:00"
        _day_hours = [max_hours_per_day] * num_days

    # ── Phase 2.5: 动态换住决策（行程决定酒店，非酒店绑架行程）──
    hotel_plan = _dynamic_hotel_decision(
        clusters,
        initial_hotel=(checkin_lat, checkin_lng),
        transport=transport_preference,
        user_provided_hotel=False,
    )

    # ── 展开 cluster：将 bound_meals 追加到对应 POI 所在的 cluster ──
    expanded_clusters = []
    for cluster in clusters:
        expanded = list(cluster)
        for poi in cluster:
            for meal in poi.get("bound_meals", []):
                expanded.append(meal)
        expanded_clusters.append(expanded)

    # 准备天气和偏好数据
    wdata = weather_data or {}
    prefs = preferences or {}
    walking_tolerance = prefs.get("commute", {}).get("walking_tolerance_meters", 3000)
    cuisine_prefs = prefs.get("taste", {}).get("cuisine_preference", [])

    # ── 备注: Day 1 到达和最后一天返程判定已在阶段 2 前完成 ──

    # ── Bug 4 修复：Day 1 跳过后，将 clusters[0] 的景点重新分配到其他天 ──
    if skip_day1 and len(clusters) > 1:
        orphan_shops = list(clusters[0])  # Day 1 被丢弃的景点
        clusters[0] = []  # 清空 Day 1
        # Day 1 跳过时有效时间为 0
        if _day_hours:
            _day_hours[0] = 0
        # 按最近质心原则分配到剩余天
        for shop in orphan_shops:
            best_day = 1  # 默认 Day 2
            best_dist = float("inf")
            s_lat = float(shop.get("lat", _hotel_lat))
            s_lng = float(shop.get("lng", _hotel_lng))
            for d in range(1, len(clusters)):
                centroid = _cluster_centroid(clusters[d])
                if centroid is None:
                    continue
                d_m = _haversine_m(s_lat, s_lng, centroid[0], centroid[1])
                if d_m < best_dist:
                    best_dist = d_m
                    best_day = d
            clusters[best_day].append(shop)
        # 重新均衡（传入每日差异化目标时间）
        clusters = _balance_clusters(clusters, max_hours_per_day, max_scenic_per_day=5,
                                      transport_preference=transport_preference,
                                      day_hours=_day_hours)
        clusters = _global_fine_tune(clusters, _hotel_lat, _hotel_lng)

        # 重新展开（均衡后 bound_meals 跟随 POI）
        expanded_clusters = []
        for cluster in clusters:
            expanded = list(cluster)
            for poi in cluster:
                for meal in poi.get("bound_meals", []):
                    expanded.append(meal)
            expanded_clusters.append(expanded)

    # ── 阶段 3: 每日 TSP + 天气感知 ──
    day_results = []
    effective_day_index = 0  # 实际排程的天数索引（跳过 Day 1 时与实际 i 不一致）
    # 追踪前一天疲劳（用于多日累积模型）
    prev_day_fatigue = 0.0

    for i, cluster in enumerate(expanded_clusters):
        is_first_day = (i == 0)
        is_last_day = (i == len(clusters) - 1)

        # Day 1 跳过判定（cluster 已重分配到其他天）
        if is_first_day and skip_day1:
            # 生成门到门出行日时间线（纯交通日，下午到达酒店后不排行程）
            travel_timeline = _build_travel_day_timeline(travel_info, checkin_lat, checkin_lng)
            # 追加 BEDTIME（到达日下午安顿休息）
            travel_timeline.append({
                "time": "22:00",
                "action": "BEDTIME",
                "memo": "🌙 就寝（到达日休息）",
                "category": "bedtime",
                "shop_id": "",
                "duration_minutes": 0,
            })
            day_results.append({
                "day_index": i,
                "label": f"第{i+1}天",
                "pairs": [],
                "timeline": travel_timeline,
                "total_duration_minutes": 0,
                "total_travel_minutes": 0,
                "route": [],
                "task_list": [],
                "spatial_matrix": {},
            })
            continue

        # 当天有效起始时间
        effective_start = day1_start if (is_first_day and day1_start) else start_time_str
        # 当天 bedtime 上限
        effective_bedtime = last_day_end if is_last_day else "22:00"
        # 最后一天上午排程不可行 → 当天不排任何活动，仅保留返程链路
        if is_last_day and not last_day_morning_feasible:
            effective_bedtime = "08:00"  # 早餐后立即截止，阻止任何活动排入

        # ── Phase 3.5: 多锚点路径合成（替代旧内联逻辑）──
        route_start_lat, route_start_lng, route_end_lat, route_end_lng = _compute_day_anchors(
            day_index=i, total_days=len(clusters), hotel_plan=hotel_plan,
            travel_info=travel_info, checkin_lat=checkin_lat, checkin_lng=checkin_lng,
        )

        # 获取当天天气
        day_weather = None
        if wdata:
            sorted_keys = sorted(wdata.keys())
            if effective_day_index < len(sorted_keys):
                day_weather = wdata[sorted_keys[effective_day_index]]

        route_result = _route_one_day_dynamic(
            cluster, route_start_lat, route_start_lng,
            route_end_lat, route_end_lng,
            transport_preference, day_weather)

        # 按 TSP 优化后的路线顺序重排 cluster
        ordered_cluster = _reorder_by_route(cluster, route_result.get("route", []))
        if not ordered_cluster:
            ordered_cluster = cluster

        # ── 阶段 4: 智能时间线构建（就近用餐 + 休息缓冲 + 天气标记 + 营业时间感知）──
        tl_result = _build_timeline(route_result, ordered_cluster, effective_start, day_weather,
                                     wake_time_str="07:30", bedtime_str=effective_bedtime,
                                     week_day=(i % 7), transport=transport_preference,
                                     travel_info=travel_info if is_first_day else None,
                                     hotel_plan=hotel_plan,
                                     day_index=effective_day_index)
        timeline = tl_result["timeline"]
        closed_conflicts_day = tl_result.get("closed_conflicts", [])
        unknown_hours_day = tl_result.get("unknown_hours_shops", [])

        # Day 1 到达日：插入门到门前置节点（travel_timeline 自带 WAKE_UP，替换通用 WAKE_UP）
        if is_first_day and day1_start and travel_info:
            travel_timeline = _build_travel_day_timeline(travel_info, checkin_lat, checkin_lng)
            # 酒店入住时间 = 旅途结束的截止线，之前的时间都在路上，不应有其他节点
            checkin_node = next((n for n in travel_timeline if n.get("action") == "HOTEL_CHECKIN"), None)
            travel_end_min = _time_str_to_minutes(checkin_node["time"]) if checkin_node else 12 * 60
            # 保留路线节点 + 过滤：移除通用 WAKE_UP + 旅途中（入住前）的非活动节点
            # VISIT/LUNCH/DINNER/BREAKFAST 保留（模板分天/用户意图不应被旅行合并静默删除）
            post_travel = [n for n in timeline
                           if n.get("action") != "WAKE_UP"
                           and (n.get("action") in ("VISIT", "LUNCH", "DINNER", "BREAKFAST")
                                or _time_str_to_minutes(n.get("time", "00:00")) >= travel_end_min)]
            timeline = travel_timeline + post_travel

        # 最后一天：插入返程链路（TO_STATION→RETURN_JOURNEY→ARRIVE_HOME）
        if is_last_day and travel_info:
            return_timeline = _build_return_timeline(travel_info, checkin_lat, checkin_lng)
            # 找到离开酒店的时间（返程开始），之后不能再有观光/用餐节点
            leave_hotel_node = next((n for n in return_timeline if n.get("action") == "TO_STATION"), None)
            return_start_min = _time_str_to_minutes(leave_hotel_node["time"]) if leave_hotel_node else 18 * 60
            # 找到到家时间，用于计算最终就寝时间
            arrive_home_node = next((n for n in return_timeline if n.get("action") == "ARRIVE_HOME"), None)
            arrive_home_min = _time_str_to_minutes(arrive_home_node["time"]) if arrive_home_node else 22 * 60
            # 移除旧的 DEPARTURE + 离开酒店后的节点（人在路上不能观光/用餐）
            timeline = [n for n in timeline
                        if n.get("action") != "DEPARTURE"
                        and n.get("action") != "BEDTIME"
                        and _time_str_to_minutes(n.get("time", "00:00")) < return_start_min]
            timeline.extend(return_timeline)
            # 就寝时间 = 到家 + 30分钟收拾
            bedtime_min = arrive_home_min + 30
            bedtime_h, bedtime_m = bedtime_min // 60, bedtime_min % 60
            timeline.append({
                "time": f"{bedtime_h:02d}:{bedtime_m:02d}",
                "action": "BEDTIME",
                "memo": "🌙 就寝（到家休息）",
                "category": "bedtime",
                "shop_id": "",
                "duration_minutes": 0,
            })

        # ── 排序：WAKE_UP 在最前、BEDTIME 在最后，其余按时间 ──
        wake_node = None
        bedtime_node = None
        rest_nodes = []
        for n in timeline:
            if n.get("action") == "WAKE_UP":
                wake_node = n
            elif n.get("action") == "BEDTIME":
                bedtime_node = n
            else:
                rest_nodes.append(n)
        rest_nodes.sort(key=lambda n: _time_str_to_minutes(n.get("time", "00:00")))
        timeline = []
        if wake_node:
            timeline.append(wake_node)
        timeline.extend(rest_nodes)
        if bedtime_node:
            timeline.append(bedtime_node)
        effective_day_index += 1

        # ── 阶段 5.5: 精修层（局部搜索优化，始终运行）──
        if REFINE_ENABLED:
            # 计算当天的 bedtime 截止时间（分钟），用于精修层约束
            _bedtime_str = effective_bedtime if is_last_day else "22:00"
            _bedtime_cap = _time_str_to_minutes(_bedtime_str)

            # 转换 timeline 格式以适配精修层（需要 start_minutes 而非 time string）
            refined_timeline_raw = _refine_timeline(
                _timeline_to_refine_format(timeline),
                ordered_cluster,
                day_index=effective_day_index,
                bedtime_cap=_bedtime_cap,
                weather=day_weather,
            )
            # 将精修后的时间线转回原有格式
            timeline = _refine_format_to_timeline(refined_timeline_raw, timeline)
            # closed_conflicts 保留不变（全部为 warning 信息，不受精修影响）

        # 构建 status 映射表
        # scheduled: timeline 中有 VISIT/LUNCH/DINNER 节点
        timeline_shop_ids = set()
        for node in timeline:
            sid = node.get("shop_id", "")
            if sid and node.get("action") in ("VISIT", "LUNCH", "DINNER"):
                timeline_shop_ids.add(sid)
        # unassigned_meal: unassigned_meals 中的 shop_id
        unassigned_meal_ids = {um["shop_id"] for um in tl_result.get("unassigned_meals", []) if um.get("shop_id")}
        # unassigned_shops（被 bedtime 约束截断的店铺）
        unassigned_shop_ids = {us["shop_id"] for us in tl_result.get("unassigned_shops", []) if us.get("shop_id")}
        # 构建 warnings 映射（仅记录预警，不影响排入）
        warnings_map = {}
        for cc in closed_conflicts_day:
            sid = cc.get("shop_id", "")
            if sid:
                if sid not in warnings_map:
                    warnings_map[sid] = []
                warnings_map[sid].append(cc.get("reason", ""))

        # 构建 selected_pairs 格式（按 TSP 顺序）
        pairs = []
        task_list = []
        for s in ordered_cluster:
            cat = s.get("category", "")
            sid = s.get("shop_id", "")
            sname = s.get("name", "")

            # 确定状态（所有非正餐默认 scheduled）
            if sid in unassigned_meal_ids:
                status = "unassigned_meal"
            elif sid in unassigned_shop_ids:
                status = "unassigned_time"  # 因时间不够未排入
            elif cat in MAIN_MEAL_CATEGORIES and sid not in timeline_shop_ids:
                status = "unassigned_meal"
            else:
                status = "scheduled"

            # 收集该 task 的所有 warnings
            task_warnings = warnings_map.get(sid, [])

            pairs.append((cat, sid, sname))
            task_list.append({
                "task_id": sid,
                "name": sname,
                "category": cat,
                "lat": s.get("lat", checkin_lat),
                "lng": s.get("lng", checkin_lng),
                "duration_minutes": _get_duration(cat, s, dynamic_durations),
                "human_needed": True,
                "status": status,
                "warnings": task_warnings,
                "is_imputed": s.get("is_imputed", False),
            })

        # 构建 spatial_matrix
        spatial_matrix = {
            "locations": {},
            "distances": {},
        }
        all_locs = [{"loc_id": "checkin", "lat": checkin_lat, "lng": checkin_lng, "name": "酒店"}]
        for s in cluster:
            all_locs.append({
                "loc_id": s.get("shop_id", ""),
                "lat": s.get("lat", checkin_lat),
                "lng": s.get("lng", checkin_lng),
                "name": s.get("name", ""),
            })
        for loc in all_locs:
            spatial_matrix["locations"][loc["loc_id"]] = loc
        for a_idx, a in enumerate(all_locs):
            for b_idx, b in enumerate(all_locs):
                if a_idx < b_idx:
                    d = _haversine_m(a["lat"], a["lng"], b["lat"], b["lng"])
                    key = f"{a['loc_id']}->{b['loc_id']}"
                    spatial_matrix["distances"][key] = {
                        "distance_m": round(d),
                        "duration_minutes": round(d / _get_speed(transport_preference)),
                        "mode": transport_preference,
                    }

        day_results.append({
            "day_index": i,
            "label": f"第{i+1}天",
            "pairs": pairs,
            "timeline": timeline,
            "total_duration_minutes": route_result.get("total_duration_minutes", 0),
            "total_travel_minutes": route_result.get("total_travel_minutes", 0),
            "route": route_result.get("route", []),
            "task_list": task_list,
            "spatial_matrix": spatial_matrix,
            "closed_conflicts": closed_conflicts_day,
            "unknown_hours_shops": unknown_hours_day,
            "unassigned_meals": tl_result.get("unassigned_meals", []),
            "unassigned_shops": tl_result.get("unassigned_shops", []),
        })

    # ── 阶段 5: 全局微调已在阶段 1.5 执行（_global_fine_tune）──
    # 聚类边界已在路由前修正，此处保留统计信息收集

    # ── 生成排程解释（schedule_reasoning）──
    reasoning = []

    # 1. 聚类解释：相近景点分到同一天
    for i, cluster in enumerate(clusters):
        scenic_names = [s.get("name", "") for s in cluster
                       if s.get("category", "") not in MEAL_CATEGORIES]
        if len(scenic_names) >= 2:
            reasoning.append(
                f"第{i+1}天：{'、'.join(scenic_names[:3])}{'等' if len(scenic_names) > 3 else ''}"
                f"地理位置相近，安排在同一天游览"
            )
        elif len(scenic_names) == 1:
            # 单独一个景点也说明
            reasoning.append(f"第{i+1}天：{scenic_names[0]}作为当天主要游览目的地")

    # 2. 天气决策
    sorted_wkeys = sorted(wdata.keys()) if wdata else []
    for i, dr in enumerate(day_results):
        if i < len(sorted_wkeys) and wdata:
            dw = wdata.get(sorted_wkeys[i], {})
            if dw and not dw.get("outdoor_suitable", True):
                reasoning.append(f"第{i+1}天天气{dw.get('day_weather', '不佳')}，优先安排室内景点")

    # 3. 起床时间依据
    for dr in day_results:
        for n in dr.get("timeline", []):
            if n.get("action") == "WAKE_UP" and "开门" in n.get("memo", ""):
                reasoning.append(f"{dr['label']}：{n['memo']}")
                break

    # 4. 餐厅安排说明
    total_meals = sum(
        1 for dr in day_results
        for n in dr.get("timeline", [])
        if n.get("action") in ("BREAKFAST", "LUNCH", "DINNER")
    )
    total_needed = sum(
        1 for dr in day_results
        for n in dr.get("timeline", [])
        if n.get("action") in ("BREAKFAST_NEEDED", "LUNCH_NEEDED", "DINNER_NEEDED")
    )
    if total_meals > 0:
        reasoning.append(f"已为您规划每日三餐共{total_meals}顿，优先选择高评分餐厅（评分≥4.0）")
    if total_needed > 0:
        reasoning.append(f"有{total_needed}餐待搜索补充，可点击展开详情查看")

    # 计算算法元数据
    total_travel_m = 0
    all_times = []
    for dr in day_results:
        total_travel_m += dr.get("total_travel_minutes", 0) * _get_speed(transport_preference)
        all_times.append(dr.get("total_duration_minutes", 0) + dr.get("total_travel_minutes", 0))

    balance_variance = 0
    if all_times:
        mean_t = sum(all_times) / len(all_times)
        balance_variance = round(sum((t - mean_t) ** 2 for t in all_times) / len(all_times), 1)

    # 汇总闭店冲突、未知营业时间、未排程（餐+时间截断）
    all_closed_conflicts = []
    all_unknown_hours = []
    all_unassigned = []
    for dr in day_results:
        for cc in dr.get("closed_conflicts", []):
            cc["day_index"] = dr["day_index"]
            all_closed_conflicts.append(cc)
        for uh in dr.get("unknown_hours_shops", []):
            if uh not in all_unknown_hours:
                all_unknown_hours.append(uh)
        for um in dr.get("unassigned_meals", []):
            um["day_index"] = dr["day_index"]
            um["unassigned_type"] = "meal"  # Phase 6: 标记类型以便 L3 倒灌区分
            all_unassigned.append(um)
        for us in dr.get("unassigned_shops", []):
            us["day_index"] = dr["day_index"]
            us["unassigned_type"] = "time"  # Phase 6: 因时间截断未排入的店铺
            all_unassigned.append(us)

    # ── 溢出通知：适配时间段排不下时告知用户 ──
    overflow_notifications = []
    if suitable_times:
        for u in all_unassigned:
            sid = u.get("shop_id", "")
            s_name = u.get("name", "")
            st = suitable_times.get(sid)
            if st == "day":
                overflow_notifications.append({
                    "type": "time_slot_overflow",
                    "shop_id": sid,
                    "shop_name": s_name,
                    "suitable_time": "day",
                    "message": f"「{s_name}」适合白天游览，但日间时段已满，建议取消或改为夜间游览",
                    "recommendations": ["取消该行程", "改为夜间游览（体验可能不佳）"],
                })
            elif st == "night":
                overflow_notifications.append({
                    "type": "time_slot_overflow",
                    "shop_id": sid,
                    "shop_name": s_name,
                    "suitable_time": "night",
                    "message": f"「{s_name}」适合夜间游览，但晚间时段已满，建议取消或改为白天游览",
                    "recommendations": ["取消该行程", "改为白天游览（体验可能不佳）"],
                })

    return {
        "days": day_results,
        "unassigned": all_unassigned,
        "overflow_notifications": overflow_notifications,
        "pending_user_confirmation_meals": pending_meals,
        "hotel_plan": hotel_plan,  # Phase 6: 动态换住决策暴露给前端
        "algorithm_metadata": {
            "cluster_method": "kmeans++",
            "balance_variance": balance_variance,
            "total_cost_km": round(total_travel_m / 1000, 1),
            "num_shops": len(candidate_shops),
            "num_days": num_days,
            "schedule_reasoning": reasoning,
            "travel_constraints": {
                "day1_afternoon_ok": afternoon_ok,
                "day1_evening_ok": evening_ok,
                "last_day_morning_feasible": last_day_morning_feasible,
                "must_leave_hotel": must_leave_hotel,
            },
        },
        "closed_conflicts": all_closed_conflicts,
        "unknown_hours_shops": all_unknown_hours,
    }


# ======================================================================
# 阶段 5.5: 精修层（局部搜索优化）
# ======================================================================

# 精修层配置参数
REFINE_ENABLED = True
REFINE_MAX_ITERATIONS = 100


def _timeline_to_refine_format(timeline: list) -> list:
    """
    将 _build_timeline 产出的 timeline 格式转换为精修层使用的格式。

    _build_timeline 格式: {"time": "09:00", "action": "VISIT", "shop_id": ..., "duration_minutes": ...}
    精修层格式: {"type": "VISIT", "shop_id": ..., "start_minutes": 540, "duration_minutes": ..., "travel_minutes": ...}
    """
    result = []
    prev_end_minutes = None
    for node in timeline:
        action = node.get("action", "")
        refined_node = {
            "shop_id": node.get("shop_id", ""),
            "category": node.get("category", ""),
            "duration_minutes": node.get("duration_minutes", 0),
        }

        # 计算 start_minutes
        time_str = node.get("time", "00:00")
        try:
            h, m = map(int, time_str.split(":"))
            start_minutes = h * 60 + m
        except (ValueError, TypeError):
            start_minutes = 0
        refined_node["start_minutes"] = start_minutes

        # 计算 travel_minutes（从前一个节点到当前节点的时间差 - 前一个节点的duration）
        if prev_end_minutes is not None and start_minutes > prev_end_minutes:
            refined_node["travel_minutes"] = start_minutes - prev_end_minutes
        else:
            refined_node["travel_minutes"] = 0

        if action == "VISIT":
            refined_node["type"] = "VISIT"
        elif action == "LUNCH":
            refined_node["type"] = "LUNCH"
        elif action == "DINNER":
            refined_node["type"] = "DINNER"
        elif action == "BREAKFAST" or action == "BREAKFAST_NEEDED":
            refined_node["type"] = "BREAKFAST"
        elif action == "LUNCH_NEEDED":
            refined_node["type"] = "LUNCH"
        elif action == "DINNER_NEEDED":
            refined_node["type"] = "DINNER"
        elif action == "REST":
            refined_node["type"] = "REST"
        elif action == "BEDTIME":
            refined_node["type"] = "BEDTIME"
        elif action == "WAKE_UP":
            refined_node["type"] = "WAKE_UP"
        else:
            refined_node["type"] = action

        result.append(refined_node)
        prev_end_minutes = start_minutes + node.get("duration_minutes", 0)

    return result


def _refine_format_to_timeline(refined: list, original_timeline: list) -> list:
    """
    将精修层格式转回 _build_timeline 的时间线格式。

    保持原 timeline 中非 VISIT/LUNCH/DINNER 节点不变，
    用精修后的 VISIT/LUNCH/DINNER 节点替换对应的原有节点。
    通过 shop_id 精确匹配，避免位置错位导致数据混乱。
    """
    result = []
    refined_visit_meals = [n for n in refined if n.get("type") in ("VISIT", "LUNCH", "DINNER")]

    # 构建 refined 节点的 shop_id → node 映射（用于精确匹配）
    refined_by_shop_id = {}
    refined_visit_no_shop = []  # 无 shop_id 的精修节点（如 LUNCH_NEEDED）
    for rn in refined_visit_meals:
        sid = rn.get("shop_id", "")
        if sid:
            refined_by_shop_id[sid] = rn
        else:
            refined_visit_no_shop.append(rn)

    # 按原始顺序重建，用 shop_id 精确匹配精修后的数据
    used_refined_shop_ids = set()
    for orig in original_timeline:
        action = orig.get("action", "")
        if action in ("VISIT", "LUNCH", "DINNER"):
            orig_sid = orig.get("shop_id", "")
            rn = None

            # 优先通过 shop_id 精确匹配
            if orig_sid and orig_sid in refined_by_shop_id:
                rn = refined_by_shop_id[orig_sid]
                used_refined_shop_ids.add(orig_sid)
            # 无 shop_id 的节点（如 LUNCH_NEEDED）用位置匹配
            elif not orig_sid and refined_visit_no_shop:
                rn = refined_visit_no_shop.pop(0)

            if rn:
                start_min = int(rn.get("start_minutes", 0))
                result.append({
                    "time": f"{start_min // 60:02d}:{start_min % 60:02d}",
                    "action": action,
                    "memo": orig.get("memo", ""),
                    "category": rn.get("category", orig.get("category", "")),
                    "shop_id": rn.get("shop_id", orig_sid),
                    "duration_minutes": rn.get("duration_minutes", orig.get("duration_minutes", 0)),
                    "opentime": orig.get("opentime", "未知"),
                })
            else:
                result.append(orig)
        else:
            result.append(orig)

    # 追加精修中新增的 VISIT/LUNCH/DINNER 节点（不在原始 timeline 中）
    for rn in refined_visit_meals:
        sid = rn.get("shop_id", "")
        if sid and sid not in used_refined_shop_ids:
            start_min = int(rn.get("start_minutes", 0))
            rtype = rn.get("type", "VISIT")
            action_map = {"VISIT": "VISIT", "LUNCH": "LUNCH", "DINNER": "DINNER"}
            # 尝试从原始 shops 信息中找回 memo
            result.append({
                "time": f"{start_min // 60:02d}:{start_min % 60:02d}",
                "action": action_map.get(rtype, "VISIT"),
                "memo": "",
                "category": rn.get("category", ""),
                "shop_id": sid,
                "duration_minutes": rn.get("duration_minutes", 0),
                "opentime": "未知",
            })

    # 排序：WAKE_UP 在最前、BEDTIME 在最后，其余按时间
    wake_node = None
    bedtime_node = None
    rest_nodes = []
    for n in result:
        if n.get("action") == "WAKE_UP":
            wake_node = n
        elif n.get("action") == "BEDTIME":
            bedtime_node = n
        else:
            rest_nodes.append(n)
    rest_nodes.sort(key=lambda t: _time_to_minutes(t.get("time", "00:00")))
    sorted_result = []
    if wake_node:
        sorted_result.append(wake_node)
    sorted_result.extend(rest_nodes)
    if bedtime_node:
        sorted_result.append(bedtime_node)
    return sorted_result


def _total_cost(timeline: list, all_shops: list, day_index: int = 0,
                 prev_day_fatigue: float = 0) -> float:
    """
    计算时间线的综合代价。

    包含四项：
    1. 用餐时间偏离惩罚（午餐/晚餐偏离锚点）
    2. 未访问店铺的损失（SKIP_PENALTY_BASE × 权重）
    3. 通勤时间的机会成本（LAMBDA_TRAVEL × 通勤分钟数）
    4. 动态体力消耗惩罚（使用 dynamic_fatigue_cost 替代旧 fatigue_cost）
    """
    cost = 0.0

    for node in timeline:
        if node.get("type") == "LUNCH":
            cost += meal_time_penalty("lunch", node.get("start_minutes", 720))
        elif node.get("type") == "DINNER":
            cost += meal_time_penalty("dinner", node.get("start_minutes", 1110))

    # 未访问点的损失
    scheduled_ids = {n.get("shop_id", "") for n in timeline if n.get("type") == "VISIT"}
    for shop in all_shops:
        sid = shop.get("shop_id", "")
        if sid and sid not in scheduled_ids:
            rating = shop.get("rating", 0)
            if rating >= 4.5:
                weight = 2.0
            elif rating >= 4.0:
                weight = 1.5
            elif rating >= 3.0:
                weight = 1.0
            else:
                weight = 0.5
            cost += weight * SKIP_PENALTY_BASE

    # 通勤时间的机会成本
    for node in timeline:
        cost += LAMBDA_TRAVEL * node.get("travel_minutes", 0)

    # 动态体力消耗惩罚（替代旧 fatigue_cost）
    cost += dynamic_fatigue_cost(timeline, day_index, prev_day_fatigue)

    return cost


def fatigue_cost(timeline: list) -> float:
    """
    计算体力消耗惩罚值。

    体力初始为 100，每项 VISIT 活动消耗体力（品类系数 × 时长），
    休息/用餐节点恢复体力。体力低于 30 时产生二次惩罚。
    """
    fatigue_level = 100.0
    total_penalty = 0.0

    for node in timeline:
        if node.get("type") == "VISIT":
            cat = node.get("category", "default")
            coef = FATIGUE_COEFFICIENT.get(cat, FATIGUE_COEFFICIENT.get("default", 0.8))
            fatigue_level -= coef * node.get("duration_minutes", 60) / 60.0
            if fatigue_level < 30:
                total_penalty += (30 - fatigue_level) ** 2
        elif node.get("type") in ("LUNCH", "DINNER", "REST", "BREAKFAST"):
            fatigue_level = min(100.0, fatigue_level + 10)

    return LAMBDA_FATIGUE * total_penalty


def _multi_factor_cost(timeline: list, all_shops: list, day_index: int = 0,
                       prev_day_fatigue: float = 0, bedtime_cap: int = None,
                       weather: dict = None, meal_shop_ids: set = None) -> float:
    """
    多因子综合代价函数（Phase 4.5 增强版，替代 _total_cost）。

    因子：
    1. MEAL_TIME_DEVIATION  — 用餐时间偏离锚点（午餐 12:00 / 晚餐 18:30）
    2. MISSED_SHOP          — 未访问店铺损失（rating 加权 × SKIP_PENALTY_BASE）
    3. TRAVEL_OVERHEAD      — 通勤时间机会成本（LAMBDA_TRAVEL × travel_minutes）
    4. DYNAMIC_FATIGUE      — 动态体力消耗（含时间×天数叠加）
    5. CONSECUTIVE_STRAIN   — 连续高强度活动无休息惩罚
    6. OVERTIME             — 超出 bedtime 硬约束（二次惩罚）
    7. EVENING_NON_SHOPPING — 晚间非购物活动警告惩罚
    8. WEATHER_PENALTY      — 恶劣天气放大通勤成本

    Returns: float（越低越好）
    """
    cost = 0.0

    # ── 因子 1: 用餐时间偏离 ──
    for node in timeline:
        if node.get("type") == "LUNCH":
            cost += meal_time_penalty("lunch", node.get("start_minutes", 720))
        elif node.get("type") == "DINNER":
            cost += meal_time_penalty("dinner", node.get("start_minutes", 1110))

    # ── 因子 2: 未访问店铺损失（rating 加权）──
    scheduled_ids = {n.get("shop_id", "") for n in timeline if n.get("type") == "VISIT"}
    for shop in all_shops:
        sid = shop.get("shop_id", "")
        if sid and sid not in scheduled_ids:
            rating = shop.get("rating", 0)
            if rating >= 4.5:
                weight = 2.0
            elif rating >= 4.0:
                weight = 1.5
            elif rating >= 3.0:
                weight = 1.0
            else:
                weight = 0.5
            cost += weight * SKIP_PENALTY_BASE

    # ── 因子 3: 通勤时间机会成本 ──
    weather_multiplier = 1.0
    if weather:
        wp = float(weather.get("walking_penalty", 1.0))
        if wp < 0.7:
            weather_multiplier = 1.0 + (1.0 - wp) * 1.5  # 坏天气放大通勤成本
    for node in timeline:
        travel = node.get("travel_minutes", 0)
        cost += LAMBDA_TRAVEL * travel * weather_multiplier

    # ── 因子 4: 动态体力消耗 ──
    cost += dynamic_fatigue_cost(timeline, day_index, prev_day_fatigue)

    # ── 因子 5: 连续高强度活动检测 ──
    consecutive_high_fatigue_count = 0
    for node in timeline:
        if node.get("type") == "VISIT":
            cat = node.get("category", "default")
            coef = FATIGUE_COEFFICIENT.get(cat, FATIGUE_COEFFICIENT.get("default", 0.8))
            if coef >= 0.7:  # 高强度活动
                consecutive_high_fatigue_count += 1
            else:
                consecutive_high_fatigue_count = 0
        elif node.get("type") in ("LUNCH", "DINNER", "REST", "BREAKFAST"):
            consecutive_high_fatigue_count = 0

        if consecutive_high_fatigue_count >= 3:
            cost += (consecutive_high_fatigue_count - 2) * 50  # 连续3个及以上高强度活动惩罚

    # ── 因子 6: 超出 bedtime 约束 ──
    if bedtime_cap is not None:
        for node in timeline:
            if node.get("type") == "VISIT":
                end_min = node.get("start_minutes", 0) + node.get("duration_minutes", 0)
                if end_min > bedtime_cap:
                    overtime = end_min - bedtime_cap
                    cost += (overtime / 60.0) ** 2 * 100  # 二次惩罚

    # ── 因子 7: 晚间非购物活动 ──
    dinner_start = 17 * 60 + 30
    for node in timeline:
        if node.get("type") == "VISIT" and node.get("category", "") != "shopping":
            if node.get("start_minutes", 0) >= dinner_start:
                cost += 30  # 温和惩罚（不阻止，但标记不佳）

    # ── 因子 8: 天气对户外活动的惩罚 ──
    if weather and float(weather.get("walking_penalty", 1.0)) < 0.5:
        for node in timeline:
            if node.get("type") == "VISIT" and node.get("category", "") == "scenic":
                cost += 20  # 恶劣天气下的户外活动额外代价

    return cost


def _accept_prob(old_cost: float, new_cost: float, iteration: int, max_iterations: int) -> float:
    """
    模拟退火接受概率：温度随迭代次数下降。
    """
    temperature = max(0.01, 1.0 - iteration / max_iterations)
    if new_cost <= old_cost:
        return 1.0
    return math.exp(-(new_cost - old_cost) / (temperature * 50))


def _swap_visit_meal(timeline: list) -> list:
    """
    邻域操作 4: 交换 VISIT 与相邻 meal（午餐/晚餐）的位置。
    如果 VISIT 在午餐前且午餐很近，交换后可改善用餐时间。
    """
    import copy as _copy
    new_timeline = _copy.deepcopy(timeline)

    visit_indices = [i for i, n in enumerate(new_timeline) if n.get("type") == "VISIT"]
    meal_indices = [i for i, n in enumerate(new_timeline) if n.get("type") in ("LUNCH", "DINNER")]

    if not visit_indices or not meal_indices:
        return new_timeline

    # 找一个距离 meal 最近的 VISIT 来交换
    vi = random.choice(visit_indices)
    mi = random.choice(meal_indices)

    if abs(vi - mi) <= 2:  # 相邻或间隔一个节点
        # 交换位置
        new_timeline[vi], new_timeline[mi] = new_timeline[mi], new_timeline[vi]
        # 交换 start_minutes
        sv = new_timeline[vi].get("start_minutes", 0)
        sm = new_timeline[mi].get("start_minutes", 0)
        new_timeline[vi]["start_minutes"] = sm
        new_timeline[mi]["start_minutes"] = sv

    return new_timeline


def _compress_duration(timeline: list) -> list:
    """
    邻域操作 5: 压缩 VISIT 时长（在品类合理范围内）。
    对超过品类默认 80% 时长的活动进行 10-20% 压缩，
    释放时间给其他活动或减少 overtime。
    """
    import copy as _copy
    new_timeline = _copy.deepcopy(timeline)

    visit_indices = [i for i, n in enumerate(new_timeline) if n.get("type") == "VISIT"]
    if not visit_indices:
        return new_timeline

    vi = random.choice(visit_indices)
    node = new_timeline[vi]
    cat = node.get("category", "")
    current_dur = node.get("duration_minutes", 60)
    default_dur = CATEGORY_DURATIONS.get(cat, 60)

    # 只压缩超过默认值 80% 的活动
    if current_dur >= default_dur * 0.8:
        ratio = random.uniform(0.80, 0.90)  # 压缩 10-20%
        new_dur = max(int(default_dur * 0.6), int(current_dur * ratio))
        node["duration_minutes"] = new_dur

    return new_timeline


def _reorder_morning_block(timeline: list) -> list:
    """
    邻域操作 6: 交换两个上午 VISIT 的顺序（午餐前）。
    上午顺序对通勤效率影响大，因为距离可能差别显著。
    """
    import copy as _copy
    new_timeline = _copy.deepcopy(timeline)

    # 找到午餐节点（或午餐窗口），确定上午区间
    lunch_idx = None
    for i, n in enumerate(new_timeline):
        if n.get("type") in ("LUNCH", "LUNCH_NEEDED"):
            lunch_idx = i
            break

    morning_visits = []
    for i, n in enumerate(new_timeline):
        if n.get("type") == "VISIT":
            if lunch_idx is None or i < lunch_idx:
                morning_visits.append(i)

    if len(morning_visits) >= 2:
        i1, i2 = random.sample(morning_visits, 2)
        new_timeline[i1], new_timeline[i2] = new_timeline[i2], new_timeline[i1]
        # 交换 start_minutes
        s1 = new_timeline[i1].get("start_minutes", 0)
        s2 = new_timeline[i2].get("start_minutes", 0)
        new_timeline[i1]["start_minutes"] = s2
        new_timeline[i2]["start_minutes"] = s1

    return new_timeline


def _random_neighbor_move(timeline: list, killed_shops: list, all_shops: list,
                          bedtime_cap: int = None) -> list:
    """
    随机选择一种邻域操作并应用，返回新的 timeline（深拷贝后操作）。

    邻域操作：
    1. 平移用餐时间（±15/30 min）
    2. 尝试补回一个被 kill 的点
    3. 交换相邻 VISIT 顺序
    4. [NEW] 交换 VISIT 与相邻 meal 位置
    5. [NEW] 压缩 VISIT 时长释放时间
    6. [NEW] 交换上午 VISIT 顺序优化通勤
    """
    import copy as _copy
    new_timeline = _copy.deepcopy(timeline)

    ops = [1, 2, 3, 4, 5, 6]
    # 如果没有被 kill 的点，跳过操作2
    if not killed_shops:
        ops.remove(2)
    # VISIT 计数检查
    visit_indices = [i for i, n in enumerate(new_timeline) if n.get("type") == "VISIT"]
    if len(visit_indices) < 2:
        for bad_op in [3, 4, 6]:
            if bad_op in ops:
                ops.remove(bad_op)
    if len(visit_indices) == 0:
        if 5 in ops:
            ops.remove(5)  # compress_duration needs at least 1 VISIT
    # 如果没有 meal 节点，跳过 1 和 4
    meal_indices = [i for i, n in enumerate(new_timeline)
                    if n.get("type") in ("LUNCH", "DINNER")]
    if not meal_indices:
        for bad_op in [1, 4]:
            if bad_op in ops:
                ops.remove(bad_op)

    if not ops:
        return new_timeline

    op = random.choice(ops)

    if op == 1:
        # 平移用餐时间
        idx = random.choice(meal_indices)
        shift = random.choice([-30, -15, 15, 30])
        old_time = new_timeline[idx].get("start_minutes", 720)
        new_time = max(0, min(1440, old_time + shift))
        new_timeline[idx]["start_minutes"] = new_time

    elif op == 2:
        # 尝试补回一个被 kill 的点
        if killed_shops:
            shop = random.choice(killed_shops)
            cat = shop.get("category", "")
            dur = _get_duration(cat, shop)
            best_pos = None
            best_extra_travel = float("inf")
            for i in range(len(new_timeline)):
                if new_timeline[i].get("type") != "VISIT":
                    continue
                extra_travel = random.uniform(5, 20)
                if extra_travel < best_extra_travel:
                    best_extra_travel = extra_travel
                    best_pos = i + 1

            if best_pos is not None and best_pos <= len(new_timeline):
                # 计算插入位置的实际 start_minutes：取前一个节点的结束时间
                if best_pos > 0:
                    prev_node = new_timeline[best_pos - 1]
                    prev_start = prev_node.get("start_minutes", 8 * 60)
                    prev_dur = prev_node.get("duration_minutes", 0)
                    prev_travel = prev_node.get("travel_minutes", 0)
                    prev_end = prev_start + prev_dur + prev_travel
                else:
                    prev_end = 8 * 60  # 默认 08:00
                insert_start = max(prev_end, 8 * 60)  # 不早于 08:00
                # 仅在插入后不严重超时才补回，避免 bedtime 冲突
                _bedtime = bedtime_cap if bedtime_cap is not None else 22 * 60
                if insert_start + dur <= _bedtime + 60:  # 允许 1h 余量
                    new_timeline.insert(best_pos, {
                        "type": "VISIT",
                        "shop_id": shop.get("shop_id", ""),
                        "start_minutes": insert_start,
                        "category": cat,
                        "duration_minutes": dur,
                        "travel_minutes": best_extra_travel,
                    })

    elif op == 3:
        # 交换相邻 VISIT 顺序
        if len(visit_indices) >= 2:
            i = random.choice(visit_indices[:-1])
            j = i + 1
            while j < len(new_timeline) and new_timeline[j].get("type") != "VISIT":
                j += 1
            if j < len(new_timeline) and new_timeline[j].get("type") == "VISIT":
                new_timeline[i], new_timeline[j] = new_timeline[j], new_timeline[i]

    elif op == 4:
        # [NEW] 交换 VISIT 与相邻 meal
        if visit_indices and meal_indices:
            vi = random.choice(visit_indices)
            mi = random.choice(meal_indices)
            if abs(vi - mi) <= 2:
                new_timeline[vi], new_timeline[mi] = new_timeline[mi], new_timeline[vi]
                sv = new_timeline[vi].get("start_minutes", 0)
                sm = new_timeline[mi].get("start_minutes", 0)
                new_timeline[vi]["start_minutes"] = sm
                new_timeline[mi]["start_minutes"] = sv

    elif op == 5:
        # [NEW] 压缩 VISIT 时长
        if visit_indices:
            vi = random.choice(visit_indices)
            node = new_timeline[vi]
            cat = node.get("category", "")
            current_dur = node.get("duration_minutes", 60)
            default_dur = CATEGORY_DURATIONS.get(cat, 60)
            if current_dur >= default_dur * 0.8:
                ratio = random.uniform(0.80, 0.90)
                new_dur = max(int(default_dur * 0.6), int(current_dur * ratio))
                node["duration_minutes"] = new_dur

    elif op == 6:
        # [NEW] 交换上午 VISIT 顺序
        lunch_idx = None
        for i, n in enumerate(new_timeline):
            if n.get("type") in ("LUNCH", "LUNCH_NEEDED"):
                lunch_idx = i
                break
        morning_visits = [i for i in visit_indices if lunch_idx is None or i < lunch_idx]
        if len(morning_visits) >= 2:
            i1, i2 = random.sample(morning_visits, 2)
            new_timeline[i1], new_timeline[i2] = new_timeline[i2], new_timeline[i1]
            s1 = new_timeline[i1].get("start_minutes", 0)
            s2 = new_timeline[i2].get("start_minutes", 0)
            new_timeline[i1]["start_minutes"] = s2
            new_timeline[i2]["start_minutes"] = s1

    return new_timeline


def _refine_timeline(timeline: list, all_shops: list,
                     max_iterations: int = None, day_index: int = 0,
                     prev_day_fatigue: float = 0, bedtime_cap: int = None,
                     weather: dict = None) -> list:
    """
    局部搜索精修（Phase 5 增强版）：在 _build_timeline 产出的基线时间线上，
    通过模拟退火（100 次迭代 + 6 种邻域操作）尝试改善用餐时间、景点覆盖率和体力分布。

    使用 _multi_factor_cost（8 因子）替代旧 _total_cost（4 因子）。

    参数:
        timeline: 基线时间线（精修层格式：type + start_minutes + duration_minutes）
        all_shops: 所有店铺（用于 cost 计算）
        max_iterations: 最大迭代次数（默认 REFINE_MAX_ITERATIONS = 100）
        day_index: 天数索引（传递到动态体力模型）
        prev_day_fatigue: 前一日累积疲劳度
        bedtime_cap: 就寝截止时间（分钟，用于 overtime 检测）
        weather: 天气 dict（用于通勤成本放大）

    返回:
        优化后的 timeline（不修改原对象）
    """
    if max_iterations is None:
        max_iterations = REFINE_MAX_ITERATIONS

    if not REFINE_ENABLED:
        return list(timeline)

    # ── 提取 killed shops（不在 timeline VISIT 中的 all_shops）──
    scheduled_ids = {n.get("shop_id", "") for n in timeline if n.get("type") == "VISIT"}
    killed_shops = [s for s in all_shops
                    if s.get("shop_id", "") and s.get("shop_id") not in scheduled_ids]

    current = copy.deepcopy(timeline)
    current_cost = _multi_factor_cost(
        current, all_shops, day_index=day_index,
        prev_day_fatigue=prev_day_fatigue,
        bedtime_cap=bedtime_cap, weather=weather,
    )
    best = copy.deepcopy(current)
    best_cost = current_cost

    for i in range(max_iterations):
        neighbor = _random_neighbor_move(current, killed_shops, all_shops,
                                          bedtime_cap=bedtime_cap)
        neighbor_cost = _multi_factor_cost(
            neighbor, all_shops, day_index=day_index,
            prev_day_fatigue=prev_day_fatigue,
            bedtime_cap=bedtime_cap, weather=weather,
        )

        if (neighbor_cost < current_cost or
                random.random() < _accept_prob(current_cost, neighbor_cost, i, max_iterations)):
            current = neighbor
            current_cost = neighbor_cost
            if current_cost < best_cost:
                best = copy.deepcopy(current)
                best_cost = current_cost

    return best


# ======================================================================
# 桥接函数：供 server.py 调用
# ======================================================================

def solve(candidate_shops, num_days, checkin_lat, checkin_lng,
          transport="步行优先", start_time="09:00", max_hours=8.0,
          weather_data=None, preferences=None, travel_info=None):
    """与 server.py 桥接的简化入口"""
    return solve_multi_day(
        candidate_shops, num_days,
        float(checkin_lat), float(checkin_lng),
        transport, start_time, max_hours,
        weather_data, preferences,
        travel_info=travel_info
    )
