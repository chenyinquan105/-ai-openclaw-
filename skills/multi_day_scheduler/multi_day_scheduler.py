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
                                      LAMBDA_TRAVEL, FATIGUE_COEFFICIENT, LAMBDA_FATIGUE)
except ImportError:
    # 回退：如果模块不存在，使用默认值（保持向后兼容）
    def meal_time_penalty(meal_type, proposed_minutes):
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


def _get_speed(transport: str) -> float:
    """获取交通速度（米/分钟）"""
    for key, speed in TRANSPORT_SPEEDS.items():
        if key in transport:
            return speed
    return 83.3  # 默认步行


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


def _get_duration(category: str) -> int:
    return CATEGORY_DURATIONS.get(category, 60)


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
        coords.append([lng, lat])  # scipy kmeans 用 [x, y]

    # 尝试 scipy
    try:
        from scipy.cluster.vq import kmeans2
        centroids, labels = kmeans2(coords, k, minit='++', missing='raise')
        clusters = [[] for _ in range(k)]
        for i, label in enumerate(labels):
            clusters[int(label)].append(shops[i])
        return clusters
    except ImportError:
        pass
    except Exception as e:
        print(f"[multi_day_scheduler] scipy kmeans 失败: {e}，回退纯 Python", flush=True)

    # 纯 Python KMeans 回退（Lloyd 算法）
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

def _balance_clusters(clusters: list, max_hours_per_day: float = 8.0, max_scenic_per_day: int = 2) -> list:
    """
    贪心迭代重分配，使每天总时间接近目标，且每天 POI 数不超标。
    第一遍：时间均衡（目标: 每天活动 + 旅行时间在 max_hours 的 +-15% 内）。
    第二遍：数量均衡（确保每天不超过 max_shops_per_day 个 POI）。
    """
    target_minutes = max_hours_per_day * 60
    max_iter = 50

    # ── 第一遍：时间均衡 ──
    for _ in range(max_iter):
        # 计算每天预估总时间（正餐每天最多计入2家=1午+1晚）
        day_times = []
        for cluster in clusters:
            meal_count = 0
            total = 0
            for s in cluster:
                cat = s.get("category", "")
                if cat in MAIN_MEAL_CATEGORIES:
                    meal_count += 1
                    if meal_count <= 2:
                        total += _get_duration(cat)  # 最多计入2家正餐（1午+1晚）
                    # 第3+家正餐不计入时间预算（每天最多2顿正餐），但不标记任何内部字段
                else:
                    total += _get_duration(cat)
            # 粗略估计旅行时间 = 点数 × 15min
            total += len(cluster) * 15
            day_times.append(total)

        # 找最超载和最轻载的天
        overloaded_idx = max(range(len(day_times)), key=lambda i: day_times[i])
        underloaded_idx = min(range(len(day_times)), key=lambda i: day_times[i])

        overloaded_time = day_times[overloaded_idx]
        underloaded_time = day_times[underloaded_idx]

        # 都在容忍范围内 → 停止
        if (overloaded_time <= target_minutes * 1.15 and
                underloaded_time >= target_minutes * 0.85):
            break

        if overloaded_time <= target_minutes * 1.15:
            break

        # 从超载天移一个最近的店到轻载天
        if not clusters[overloaded_idx]:
            break

        # 找超载天中离轻载天质心最近的点
        ul_centroid = _cluster_centroid(clusters[underloaded_idx])
        if ul_centroid is None:
            break

        best_shop = None
        best_dist = float("inf")
        for s in clusters[overloaded_idx]:
            lat = float(s.get("lat", 0))
            lng = float(s.get("lng", 0))
            d = _haversine_m(lat, lng, ul_centroid[0], ul_centroid[1])
            if d < best_dist:
                best_dist = d
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

        # 从最多天选一个 scenic 离最少天质心最近的搬过去
        target_centroid = _cluster_centroid(clusters[under_idx])
        if target_centroid is None:
            break

        best_shop = None
        best_dist = float("inf")
        for s in clusters[over_idx]:
            if s.get("category", "") != "scenic":
                continue  # 只搬 scenic（景点），不搬餐厅/购物中心
            lat = float(s.get("lat", 0))
            lng = float(s.get("lng", 0))
            d = _haversine_m(lat, lng, target_centroid[0], target_centroid[1])
            if d < best_dist:
                best_dist = d
                best_shop = s

        if best_shop:
            clusters[over_idx].remove(best_shop)
            clusters[under_idx].append(best_shop)
        else:
            break

    # ── 第二遍后半：数量上限 —— 每天 scenic 数不超上限（不限制饭店/购物中心）──
    prev_max_count = float("inf")
    for _ in range(max_iter):
        # 只统计 scenic，饭店/购物中心不计入上限
        counts = [sum(1 for s in c if s.get("category", "") == "scenic") for c in clusters]
        max_count = max(counts)
        min_count = min(counts)

        # 最多天未超标 → 停止
        if max_count <= max_scenic_per_day:
            break

        # 防抖：如果总scenic超出 days*cap，无法全部满足，停止重分配
        if max_count >= prev_max_count:
            break
        prev_max_count = max_count

        max_idx = counts.index(max_count)
        min_idx = counts.index(min_count)

        # 从最多天选一个 scenic 离最少天质心最近的搬过去
        target_centroid = _cluster_centroid(clusters[min_idx])
        if target_centroid is None:
            break

        best_shop = None
        best_dist = float("inf")
        for s in clusters[max_idx]:
            if s.get("category", "") != "scenic":
                continue  # 只搬 scenic，不动饭店/购物中心
            lat = float(s.get("lat", 0))
            lng = float(s.get("lng", 0))
            d = _haversine_m(lat, lng, target_centroid[0], target_centroid[1])
            if d < best_dist:
                best_dist = d
                best_shop = s

        if best_shop:
            clusters[max_idx].remove(best_shop)
            clusters[min_idx].append(best_shop)
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
# 阶段 3: 每日 TSP —— 最近邻 + 2-opt
# ======================================================================

def _route_one_day(shops: list, start_lat: float, start_lng: float,
                   transport: str, weather: dict = None) -> dict:
    """
    为一天规划最优路线（天气感知）。
    起点/终点 = 酒店坐标（如未指定则用第一个店铺坐标）。
    返回: {timeline: [...], total_travel_minutes, total_duration_minutes, route: [...]}
    """
    if not shops:
        return {
            "timeline": [],
            "total_travel_minutes": 0,
            "total_duration_minutes": 0,
            "route": [(start_lat, start_lng)],
        }

    speed = _get_speed(transport)
    # 天气影响步行速度
    weather_penalty = 1.0
    if weather:
        weather_penalty = weather.get("walking_penalty", 1.0)
        # 雨天/恶劣天气降低步行和公共交通速度
        if transport in ("步行优先",):
            speed *= weather_penalty
        elif weather_penalty < 0.5:
            speed *= 0.8  # 极端天气整体放慢
    points = [(start_lat, start_lng)]  # 起点 = 酒店

    for s in shops:
        lat = float(s.get("lat", start_lat))
        lng = float(s.get("lng", start_lng))
        points.append((lat, lng))

    points.append((start_lat, start_lng))  # 终点 = 酒店

    # 最近邻贪心排序
    route = _nearest_neighbor_route(points)
    # 2-opt 优化
    route = _two_opt_improve(route)

    # 计算总旅行距离/时间
    total_travel_m = 0
    for i in range(len(route) - 1):
        total_travel_m += _haversine_m(
            route[i][0], route[i][1],
            route[i + 1][0], route[i + 1][1]
        )
    total_travel_minutes = total_travel_m / speed

    # 计算总活动时间
    total_duration_minutes = sum(_get_duration(s.get("category", "")) for s in shops)

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
# 阶段 4: 用餐插入
# ======================================================================

LUNCH_WINDOW = (11 * 60 + 30, 13 * 60 + 30)   # 11:30-13:30
DINNER_WINDOW = (17 * 60 + 30, 19 * 60 + 30)  # 17:30-19:30


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
                    transport: str = "步行优先") -> dict:
    """
    智能时间线构建：就近用餐 + 休息缓冲 + 天气标记 + 营业时间感知。

    参数:
        transport: 用户选择的交通方式，用于活动间 travel 耗时估算

    返回:
      {"timeline": [...], "closed_conflicts": [...], "unknown_hours_shops": [...]}
    """
    start_h, start_m = map(int, start_time_str.split(":"))
    current_minutes = start_h * 60 + start_m

    ordered_shops = list(shops)  # 保持原顺序（TSP已优化）

    # ── 品类分流：正餐不入VISIT循环，小吃需间隔控制 ──
    main_meal_shops = [s for s in ordered_shops if s.get("category", "") in MAIN_MEAL_CATEGORIES]
    snack_shops = [s for s in ordered_shops if s.get("category", "") in SNACK_CATEGORIES]
    # 非餐类 + 小吃 = VISIT循环遍历对象（正餐排除在外）
    # 重排：非购物类在前（优先占上午/下午），购物类在后（可晚间）
    _all_visitable = [s for s in ordered_shops if s.get("category", "") not in MAIN_MEAL_CATEGORIES]
    non_shopping = [s for s in _all_visitable if s.get("category", "") != "shopping"]
    shopping_only = [s for s in _all_visitable if s.get("category", "") == "shopping"]
    visitable_shops = non_shopping + shopping_only

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

    # ── 智能起床时间（基于当天首个景点开门时间）──
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
        dur = _get_duration(cat)
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
                    meal_dur = _get_duration(meal.get("category", ""))
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

        # ── 活动间缓冲 ──
        if len(timeline) > 0:
            if last_shop_lat and last_shop_lng:
                travel_m = _haversine_m(last_shop_lat, last_shop_lng, s_lat, s_lng)
                speed = _get_speed(transport)
                # 远距离（>3km）自动切驾车速度，避免步行数小时跨城
                if travel_m > 3000 and speed < 500:
                    speed = 667  # 驾车 40km/h
                travel_min = max(5, round(travel_m / speed))
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
            d_dur = _get_duration(d_meal.get("category", ""))
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
    # 最后活动结束后 90min 就寝，不设上限（行程紧时自动后延）
    bedtime_minutes = current_minutes + 90
    # 不低于 21:00（太早睡不合理），不设上限
    bedtime_minutes = max(21 * 60, bedtime_minutes)
    # 如果超过次日凌晨，添加提示
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
            dur = _get_duration(cat)
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

    # 按时间排序
    timeline.sort(key=lambda t: _time_to_minutes(t.get("time", "00:00")))

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
    }


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

                    # 如果离邻天明显更近（至少近 20%）
                    if d_to_other < d_to_own * 0.8:
                        # 计算移动前后的方差变化
                        old_var = _calc_cluster_variance(clusters)
                        clusters[i].remove(shop)
                        clusters[j].append(shop)
                        new_var = _calc_cluster_variance(clusters)

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
        t = sum(_get_duration(s.get("category", "")) for s in c) + len(c) * 15
        times.append(t)
    if not times:
        return 0
    mean = sum(times) / len(times)
    return sum((t - mean) ** 2 for t in times) / len(times)


# ======================================================================
# 主入口
# ======================================================================

def solve_multi_day(
    candidate_shops: list,
    num_days: int,
    checkin_lat: float,
    checkin_lng: float,
    transport_preference: str = "步行优先",
    start_time_str: str = "09:00",
    max_hours_per_day: float = 8.0,
    weather_data: dict = None,
    preferences: dict = None,
) -> dict:
    """
    多日行程智能排程主入口。

    参数:
        candidate_shops: [{"shop_id", "name", "category", "lat", "lng", "coord", ...}, ...]
        num_days: 计划天数
        checkin_lat, checkin_lng: 酒店坐标
        transport_preference: 交通方式
        start_time_str: 每天开始时间 "09:00"
        max_hours_per_day: 每天最大活动小时数
        weather_data: {"2026-07-15": {day_weather, day_temp, walking_penalty, outdoor_suitable}, ...}
        preferences: {"commute": {walking_tolerance_meters, transport_priority}, "taste": {cuisine_preference}, ...}

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
        return {"days": [], "unassigned": [], "algorithm_metadata": {}}

    if num_days < 1:
        num_days = 1
    if num_days > len(candidate_shops):
        num_days = len(candidate_shops)  # 每天至少一个

    # 确保所有 shop 有 lat/lng
    for s in candidate_shops:
        if "lat" not in s or "lng" not in s:
            coord = s.get("coord", f"{checkin_lat},{checkin_lng}")
            parts = coord.split(",")
            if len(parts) == 2:
                try:
                    s["lat"] = float(parts[0].strip())
                    s["lng"] = float(parts[1].strip())
                except (ValueError, TypeError):
                    s["lat"] = checkin_lat
                    s["lng"] = checkin_lng
            else:
                s["lat"] = checkin_lat
                s["lng"] = checkin_lng

    # ── 阶段 1: 地理聚类 ──
    clusters = _cluster_by_geo(candidate_shops, num_days)

    # ── 阶段 1.5: 全局微调（跨天边界交换，修正聚类边界错误）──
    clusters = _global_fine_tune(clusters, checkin_lat, checkin_lng)

    # ── 阶段 2: 负载均衡 ──
    clusters = _balance_clusters(clusters, max_hours_per_day, max_scenic_per_day=2)

    # 准备天气和偏好数据
    wdata = weather_data or {}
    prefs = preferences or {}
    walking_tolerance = prefs.get("commute", {}).get("walking_tolerance_meters", 3000)
    cuisine_prefs = prefs.get("taste", {}).get("cuisine_preference", [])

    # ── 阶段 3: 每日 TSP + 天气感知 ──
    day_results = []
    for i, cluster in enumerate(clusters):
        # 获取当天天气
        day_weather = None
        if wdata:
            sorted_keys = sorted(wdata.keys())
            if i < len(sorted_keys):
                day_weather = wdata[sorted_keys[i]]

        route_result = _route_one_day(cluster, checkin_lat, checkin_lng, transport_preference, day_weather)

        # 按 TSP 优化后的路线顺序重排 cluster
        ordered_cluster = _reorder_by_route(cluster, route_result.get("route", []))
        if not ordered_cluster:
            ordered_cluster = cluster

        # ── 阶段 4: 智能时间线构建（就近用餐 + 休息缓冲 + 天气标记 + 营业时间感知）──
        tl_result = _build_timeline(route_result, ordered_cluster, start_time_str, day_weather,
                                     wake_time_str="07:30", bedtime_str="22:00",
                                     week_day=(i % 7), transport=transport_preference)
        timeline = tl_result["timeline"]
        closed_conflicts_day = tl_result.get("closed_conflicts", [])
        unknown_hours_day = tl_result.get("unknown_hours_shops", [])

        # ── 阶段 5.5: 精修层（局部搜索优化，始终运行）──
        if REFINE_ENABLED:
            # 转换 timeline 格式以适配精修层（需要 start_minutes 而非 time string）
            refined_timeline_raw = _refine_timeline(
                _timeline_to_refine_format(timeline),
                ordered_cluster,
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
                "duration_minutes": _get_duration(cat),
                "human_needed": True,
                "status": status,
                "warnings": task_warnings,
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

    # 汇总闭店冲突、未知营业时间、未排程餐厅
    all_closed_conflicts = []
    all_unknown_hours = []
    all_unassigned_meals = []
    for dr in day_results:
        for cc in dr.get("closed_conflicts", []):
            cc["day_index"] = dr["day_index"]
            all_closed_conflicts.append(cc)
        for uh in dr.get("unknown_hours_shops", []):
            if uh not in all_unknown_hours:
                all_unknown_hours.append(uh)
        for um in dr.get("unassigned_meals", []):
            um["day_index"] = dr["day_index"]
            all_unassigned_meals.append(um)

    return {
        "days": day_results,
        "unassigned": all_unassigned_meals,
        "algorithm_metadata": {
            "cluster_method": "kmeans++",
            "balance_variance": balance_variance,
            "total_cost_km": round(total_travel_m / 1000, 1),
            "num_shops": len(candidate_shops),
            "num_days": num_days,
            "schedule_reasoning": reasoning,
        },
        "closed_conflicts": all_closed_conflicts,
        "unknown_hours_shops": all_unknown_hours,
    }


# ======================================================================
# 阶段 5.5: 精修层（局部搜索优化）
# ======================================================================

# 精修层配置参数
REFINE_ENABLED = True
REFINE_MAX_ITERATIONS = 80


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
                start_min = rn.get("start_minutes", 0)
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
            start_min = rn.get("start_minutes", 0)
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

    # 按时间排序
    result.sort(key=lambda t: _time_to_minutes(t.get("time", "00:00")))
    return result


def _total_cost(timeline: list, all_shops: list) -> float:
    """
    计算时间线的综合代价。

    包含三项：
    1. 用餐时间偏离惩罚（午餐/晚餐偏离锚点）
    2. 未访问店铺的损失（SKIP_PENALTY_BASE × 权重）
    3. 通勤时间的机会成本（LAMBDA_TRAVEL × 通勤分钟数）
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

    # 体力消耗惩罚
    cost += fatigue_cost(timeline)

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


def _accept_prob(old_cost: float, new_cost: float, iteration: int, max_iterations: int) -> float:
    """
    模拟退火接受概率：温度随迭代次数下降。
    """
    temperature = max(0.01, 1.0 - iteration / max_iterations)
    if new_cost <= old_cost:
        return 1.0
    return math.exp(-(new_cost - old_cost) / (temperature * 50))


def _random_neighbor_move(timeline: list, killed_shops: list, all_shops: list) -> list:
    """
    随机选择一种邻域操作并应用，返回新的 timeline（深拷贝后操作）。

    邻域操作：
    1. 平移用餐时间（±15/30 min）
    2. 尝试补回一个被 kill 的点
    3. 交换相邻 VISIT 顺序
    """
    import copy as _copy
    new_timeline = _copy.deepcopy(timeline)

    ops = [1, 2, 3]
    # 如果没有被 kill 的点，跳过操作2
    if not killed_shops:
        ops.remove(2)
    # 如果没有 VISIT 节点，跳过操作3
    visit_indices = [i for i, n in enumerate(new_timeline) if n.get("type") == "VISIT"]
    if len(visit_indices) < 2:
        if 3 in ops:
            ops.remove(3)

    if not ops:
        return new_timeline

    op = random.choice(ops)

    if op == 1:
        # 平移用餐时间
        meal_indices = [i for i, n in enumerate(new_timeline)
                       if n.get("type") in ("LUNCH", "DINNER")]
        if meal_indices:
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
            dur = CATEGORY_DURATIONS.get(cat, 60)
            # 插入到通勤增量最小的位置
            best_pos = None
            best_extra_travel = float("inf")
            for i in range(len(new_timeline)):
                if new_timeline[i].get("type") != "VISIT":
                    continue
                # 粗略估算插入后的额外通勤
                extra_travel = random.uniform(5, 20)  # 简化估算
                if extra_travel < best_extra_travel:
                    best_extra_travel = extra_travel
                    best_pos = i + 1

            if best_pos is not None and best_pos <= len(new_timeline):
                new_timeline.insert(best_pos, {
                    "type": "VISIT",
                    "shop_id": shop.get("shop_id", ""),
                    "start_minutes": 0,  # 后续需要重新推算时间
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

    return new_timeline


def _refine_timeline(timeline: list, all_shops: list,
                     max_iterations: int = None) -> list:
    """
    局部搜索精修：在 _build_timeline 产出的基线时间线上，
    通过模拟退火尝试改善用餐时间和景点覆盖率。

    参数:
        timeline: 基线时间线（_build_timeline 产出）
        all_shops: 所有店铺（用于 _total_cost 计算）
        max_iterations: 最大迭代次数（默认使用 REFINE_MAX_ITERATIONS）

    返回:
        优化后的 timeline（不修改原对象）
    """
    if max_iterations is None:
        max_iterations = REFINE_MAX_ITERATIONS

    if not REFINE_ENABLED:
        return list(timeline)

    current = copy.deepcopy(timeline)
    current_cost = _total_cost(current, all_shops)
    best = copy.deepcopy(current)
    best_cost = current_cost

    for i in range(max_iterations):
        neighbor = _random_neighbor_move(current, [], all_shops)
        neighbor_cost = _total_cost(neighbor, all_shops)

        if neighbor_cost < current_cost or random.random() < _accept_prob(current_cost, neighbor_cost, i, max_iterations):
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
          weather_data=None, preferences=None):
    """与 server.py 桥接的简化入口"""
    return solve_multi_day(
        candidate_shops, num_days,
        float(checkin_lat), float(checkin_lng),
        transport, start_time, max_hours,
        weather_data, preferences
    )
