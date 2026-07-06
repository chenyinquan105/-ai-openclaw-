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
MAIN_MEAL_CATEGORIES = {"restaurant", "hotpot", "japanese"}  # 正餐：只排午/晚餐，不进入VISIT循环
SNACK_CATEGORIES = {"cafe"}  # 小吃/饮品：可作VISIT节点，但需间隔≥90min


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

def _balance_clusters(clusters: list, max_hours_per_day: float = 8.0, max_shops_per_day: int = 5) -> list:
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
                        total += _get_duration(cat)  # 最多计入2家正餐
                    # 超过2家的正餐不计入（不会出现在VISIT中）
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

    # ── 第二遍：数量均衡 —— 每天 POI 数不超上限 ──
    for _ in range(max_iter):
        counts = [len(c) for c in clusters]
        max_count = max(counts)
        min_count = min(counts)

        # 最多天未超标 → 停止
        if max_count <= max_shops_per_day:
            break

        max_idx = counts.index(max_count)
        min_idx = counts.index(min_count)

        # 从最多天选一个离最少天质心最近的点搬过去
        target_centroid = _cluster_centroid(clusters[min_idx])
        if target_centroid is None:
            break

        best_shop = None
        best_dist = float("inf")
        for s in clusters[max_idx]:
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


# ======================================================================
# 阶段 4: 用餐插入
# ======================================================================

LUNCH_WINDOW = (11 * 60 + 30, 13 * 60 + 30)   # 11:30-13:30
DINNER_WINDOW = (17 * 60 + 30, 19 * 60 + 30)  # 17:30-19:30


def _build_timeline(day_plan: dict, shops: list, start_time_str: str = "09:00",
                    weather: dict = None, wake_time_str: str = "07:30",
                    bedtime_str: str = "22:00", week_day: int = 0) -> dict:
    """
    智能时间线构建：就近用餐 + 休息缓冲 + 天气标记 + 营业时间感知。

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
    visitable_shops = [s for s in ordered_shops if s.get("category", "") not in MAIN_MEAL_CATEGORIES]

    timeline = []
    closed_conflicts = []
    unknown_hours_shops = []
    MEAL_CATS = MEAL_CATEGORIES
    meal_map = {s.get("shop_id"): s for s in shops if s.get("category", "") in MEAL_CATS}
    _last_food_end_minutes = None  # 追踪上一次进食结束时间（用于小吃间隔控制）

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

    # ── 早餐插入（07:00-09:00）──
    bf_meal = None
    for mid, m in meal_map.items():
        if _is_breakfast(m):
            bf_meal = m
            break
    bf_time = max(7 * 60, wake_minutes + 20)
    if bf_meal:
        timeline.append({
            "time": f"{bf_time // 60:02d}:{bf_time % 60:02d}",
            "action": "BREAKFAST",
            "memo": f"🥐 {bf_meal.get('name', '早餐')}",
            "category": bf_meal.get("category", "breakfast"),
            "shop_id": bf_meal.get("shop_id", ""),
            "duration_minutes": 45,
        })
    else:
        timeline.append({
            "time": f"{bf_time // 60:02d}:{bf_time % 60:02d}",
            "action": "BREAKFAST_NEEDED",
            "memo": "🥐 早餐（待搜索）",
            "category": "breakfast",
            "shop_id": "",
            "duration_minutes": 45,
        })
    current_minutes = max(current_minutes, bf_time + 45 + 15)  # 推进到 09:00+
    _last_food_end_minutes = bf_time + 45  # 早餐结束时间

    # ── 按路线顺序遍历（正餐不参与VISIT循环）──
    last_shop_lat, last_shop_lng = None, None
    for idx, shop in enumerate(visitable_shops):
        cat = shop.get("category", "")
        # 跳过已用作早餐的店铺
        if bf_meal and shop.get("shop_id") == bf_meal.get("shop_id"):
            continue
        dur = _get_duration(cat)
        s_lat = shop.get("lat", 0)
        s_lng = shop.get("lng", 0)

        # ── 午餐插入（11:00-14:00）：从正餐池中选1家最近的 ──
        lunch_start = 11 * 60
        lunch_end = 14 * 60
        if main_meal_shops and current_minutes < lunch_end and not any(t.get("action") == "LUNCH" for t in timeline):
            # 如果当前时间在午餐窗口内或即将进入
            if idx == 0 or current_minutes > lunch_start - 60:
                # 选离当前位置最近的正餐
                nearest_meal = min(main_meal_shops, key=lambda m:
                    _haversine_m(s_lat, s_lng, m.get("lat", s_lat), m.get("lng", s_lng)))
                if current_minutes < lunch_start:
                    current_minutes = lunch_start
                meal_dur = _get_duration(nearest_meal.get("category", ""))
                time_str = f"{current_minutes // 60:02d}:{current_minutes % 60:02d}"
                timeline.append({
                    "time": time_str, "action": "LUNCH",
                    "memo": f"🍽️ {nearest_meal.get('name', '')}",
                    "category": nearest_meal.get("category", ""),
                    "shop_id": nearest_meal.get("shop_id", ""),
                    "duration_minutes": meal_dur,
                    "opentime": nearest_meal.get("opentime", "未知"),
                })
                current_minutes += meal_dur + 30  # 午餐后休息
                _last_food_end_minutes = current_minutes
                main_meal_shops.remove(nearest_meal)  # 用过的不复用
                timeline.append({
                    "time": f"{current_minutes // 60:02d}:{current_minutes % 60:02d}",
                    "action": "REST", "memo": "☕ 午休片刻", "category": "rest",
                    "shop_id": "", "duration_minutes": 0,
                })

        # ── 晚餐插入（17:00-19:30）：在活动推过晚饭时间前插入 ──
        dinner_start = 17 * 60
        dinner_end = 19 * 60 + 30
        if main_meal_shops and current_minutes + dur > dinner_start and current_minutes < dinner_end \
                and not any(t.get("action") == "DINNER" for t in timeline):
            d_time = max(dinner_start, current_minutes)
            # 距上次进食至少90min
            if _last_food_end_minutes is not None:
                d_time = max(d_time, _last_food_end_minutes + 90)
            if d_time < dinner_end:
                nearest_d = min(main_meal_shops, key=lambda m:
                    _haversine_m(s_lat, s_lng, m.get("lat", s_lat), m.get("lng", s_lng)))
                d_dur = _get_duration(nearest_d.get("category", ""))
                timeline.append({
                    "time": f"{d_time // 60:02d}:{d_time % 60:02d}",
                    "action": "DINNER",
                    "memo": f"🍽️ {nearest_d.get('name', '')}",
                    "category": nearest_d.get("category", ""),
                    "shop_id": nearest_d.get("shop_id", ""),
                    "duration_minutes": d_dur,
                    "opentime": nearest_d.get("opentime", "未知"),
                })
                current_minutes = d_time + d_dur + 30
                _last_food_end_minutes = current_minutes
                main_meal_shops.remove(nearest_d)

        # ── 小吃间隔检查（café类距上次进食≥90min）──
        if cat in SNACK_CATEGORIES and _last_food_end_minutes is not None:
            gap_needed = _last_food_end_minutes + 90
            if current_minutes < gap_needed:
                current_minutes = gap_needed

        # ── 活动间缓冲 ──
        if len(timeline) > 0:
            if last_shop_lat and last_shop_lng:
                travel_m = _haversine_m(last_shop_lat, last_shop_lng, s_lat, s_lng)
                travel_min = max(5, round(travel_m / _get_speed("步行优先")))
                current_minutes += travel_min
            else:
                current_minutes += 15

        # ── 夜间截止（由 bedtime 决定）──
        bed_h, bed_m = map(int, bedtime_str.split(":"))
        night_cutoff = bed_h * 60 + bed_m - 60  # 就寝前1小时不再排活动
        if current_minutes >= night_cutoff:
            continue

        # ── 营业时间检查 ──
        opentime_str = shop.get("opentime", "未知")
        hours = _parse_opentime(opentime_str, week_day)
        open_check = _check_open(hours, current_minutes, dur)

        if open_check["status"] == "after_close":
            closed_conflicts.append({
                "shop_name": shop.get("name", ""),
                "shop_id": shop.get("shop_id", ""),
                "category": cat,
                "visit_time": f"{current_minutes // 60:02d}:{current_minutes % 60:02d}",
                "opentime": opentime_str,
                "reason": open_check["message"],
            })
            continue  # 跳过此 POI

        if open_check["status"] == "before_open":
            # 推迟到开门时间
            suggested = open_check.get("suggested_time", current_minutes)
            if suggested <= night_cutoff:
                current_minutes = suggested

        time_str = f"{current_minutes // 60:02d}:{current_minutes % 60:02d}"

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
        last_shop_lat, last_shop_lng = s_lat, s_lng
        # 小吃/饮品结束时间记录（用于后续间隔控制）
        if cat in SNACK_CATEGORIES:
            _last_food_end_minutes = current_minutes

    # ── 兜底午餐：主循环未插入时补插 ──
    if main_meal_shops and not any(t.get("action") in ("LUNCH", "LUNCH_NEEDED") for t in timeline):
        fb_lunch = max(11*60+30, min(14*60, current_minutes))
        if fb_lunch < 19 * 60:
            nearest = min(main_meal_shops, key=lambda m:
                _haversine_m(last_shop_lat or 0, last_shop_lng or 0,
                             m.get("lat", last_shop_lat or 0), m.get("lng", last_shop_lng or 0)))
            timeline.append({
                "time": f"{fb_lunch // 60:02d}:{fb_lunch % 60:02d}",
                "action": "LUNCH", "memo": f"🍽️ {nearest.get('name', '')}",
                "category": nearest.get("category", ""),
                "shop_id": nearest.get("shop_id", ""),
                "duration_minutes": _get_duration(nearest.get("category", "")),
                "opentime": nearest.get("opentime", "未知"),
            })
            _last_food_end_minutes = fb_lunch + _get_duration(nearest.get("category", ""))
            main_meal_shops.remove(nearest)

    # ── 兜底晚餐：主循环未插入时补插（所有活动在17:00前结束的罕见情况）──
    if main_meal_shops and not any(t.get("action") == "DINNER" for t in timeline) and current_minutes < 20 * 60:
        dinner_time = max(17 * 60 + 30, current_minutes)
        # 距上次进食至少90min（避免吃完小吃立刻吃正餐）
        if _last_food_end_minutes is not None:
            dinner_time = max(dinner_time, _last_food_end_minutes + 90)
        current_minutes = dinner_time
        # 选离最后位置最近的1家正餐（不是全部！）
        ref_lat = last_shop_lat or 0
        ref_lng = last_shop_lng or 0
        nearest_dinner = min(main_meal_shops, key=lambda m:
            _haversine_m(ref_lat, ref_lng, m.get("lat", ref_lat), m.get("lng", ref_lng)))
        time_str = f"{current_minutes // 60:02d}:{current_minutes % 60:02d}"
        opentime_str = nearest_dinner.get("opentime", "未知")
        dinner_dur = _get_duration(nearest_dinner.get("category", ""))
        timeline.append({
            "time": time_str, "action": "DINNER",
            "memo": f"🍽️ {nearest_dinner.get('name', '')}",
            "category": nearest_dinner.get("category", ""),
            "shop_id": nearest_dinner.get("shop_id", ""),
            "duration_minutes": dinner_dur,
            "opentime": opentime_str,
        })
        current_minutes += dinner_dur + 10
        _last_food_end_minutes = current_minutes
        main_meal_shops.remove(nearest_dinner)

    # ── 兜底：无餐厅时插入占位节点（确保三餐始终可见）──
    has_lunch = any(t.get("action") == "LUNCH" for t in timeline)
    has_dinner = any(t.get("action") == "DINNER" for t in timeline)
    if not has_lunch:
        timeline.append({
            "time": "12:00", "action": "LUNCH_NEEDED",
            "memo": "🍽️ 午餐（待搜索）", "category": "lunch_needed",
            "shop_id": "", "duration_minutes": 60, "opentime": "未知",
        })
    if not has_dinner:
        timeline.append({
            "time": "18:00", "action": "DINNER_NEEDED",
            "memo": "🍽️ 晚餐（待搜索）", "category": "dinner_needed",
            "shop_id": "", "duration_minutes": 60, "opentime": "未知",
        })

    # ── 智能就寝时间 ──
    bed_h, bed_m = map(int, bedtime_str.split(":"))
    default_bedtime = bed_h * 60 + bed_m
    # 最后活动结束后 90min 就寝，约束在 21:00-23:30
    bedtime_minutes = max(21 * 60, min(23 * 60 + 30, current_minutes + 90))
    # 如果默认就寝时间更合理（用户偏好），用默认值
    if bedtime_minutes > default_bedtime + 60:
        bedtime_minutes = default_bedtime

    timeline.append({
        "time": f"{bedtime_minutes // 60:02d}:{bedtime_minutes % 60:02d}",
        "action": "BEDTIME",
        "memo": "🌙 就寝",
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
            time_str = f"{current_minutes // 60:02d}:{current_minutes % 60:02d}"
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

    return {
        "timeline": timeline,
        "closed_conflicts": closed_conflicts,
        "unknown_hours_shops": unknown_hours_shops,
    }


def _time_to_minutes(time_str: str) -> int:
    """ "HH:MM" → 分钟数 """
    try:
        h, m = map(int, time_str.split(":"))
        return h * 60 + m
    except (ValueError, TypeError):
        return 0


def _is_breakfast(shop: dict) -> bool:
    """判断是否为早餐类店铺"""
    cat = shop.get("category", "")
    name = shop.get("name", "")
    if cat == "breakfast":
        return True
    if any(kw in name for kw in ["早餐", "早点", "豆浆", "油条", "包子", "粥"]):
        return True
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
    clusters = _balance_clusters(clusters, max_hours_per_day, max_shops_per_day=5)

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

        # ── 阶段 4: 智能时间线构建（就近用餐 + 休息缓冲 + 天气标记 + 营业时间感知）──
        tl_result = _build_timeline(route_result, cluster, start_time_str, day_weather,
                                     wake_time_str="07:30", bedtime_str="22:00",
                                     week_day=(i % 7))
        timeline = tl_result["timeline"]
        closed_conflicts_day = tl_result.get("closed_conflicts", [])
        unknown_hours_day = tl_result.get("unknown_hours_shops", [])

        # 构建 selected_pairs 格式
        pairs = []
        task_list = []
        for s in cluster:
            cat = s.get("category", "")
            sid = s.get("shop_id", "")
            sname = s.get("name", "")
            pairs.append((cat, sid, sname))
            task_list.append({
                "task_id": sid,
                "name": sname,
                "category": cat,
                "lat": s.get("lat", checkin_lat),
                "lng": s.get("lng", checkin_lng),
                "duration_minutes": _get_duration(cat),
                "human_needed": True,
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

    # 汇总闭店冲突和未知营业时间
    all_closed_conflicts = []
    all_unknown_hours = []
    for dr in day_results:
        for cc in dr.get("closed_conflicts", []):
            cc["day_index"] = dr["day_index"]
            all_closed_conflicts.append(cc)
        for uh in dr.get("unknown_hours_shops", []):
            if uh not in all_unknown_hours:
                all_unknown_hours.append(uh)

    return {
        "days": day_results,
        "unassigned": [],
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
