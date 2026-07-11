"""
复现脚本：用美团北京5日游的实际数据运行排程器，追踪算法行为。
"""
import sys
import os
import json
import math

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from multi_day_scheduler import solve_multi_day, _haversine_m, _cluster_by_geo, _balance_clusters
from multi_day_scheduler import _global_fine_tune, _compute_day1_start, _compute_last_day_end
from multi_day_scheduler import _build_travel_day_timeline, _build_timeline, _route_one_day_dynamic
from multi_day_scheduler import _reorder_by_route, _refine_timeline, _timeline_to_refine_format, _refine_format_to_timeline
from multi_day_scheduler import MAIN_MEAL_CATEGORIES, REFINE_ENABLED

# ============================================================
# 实际数据：美团北京5日游
# ============================================================

# 出发/到达信息
travel_info = {
    "outbound_type": "飞机",
    "outbound_departure_time": "08:36",
    "outbound_arrival_time": "13:38",
    "arrival_station": "北京大兴国际机场",
    "departure_city": "上海",
    "return_type": "飞机",
    "return_departure_time": "16:36",
    "return_station": "北京大兴国际机场",
}

# 7个POI（与用户实际选择一致）
candidate_shops = [
    {
        "shop_id": "s1",
        "name": "故宫博物院",
        "category": "scenic",
        "lat": 39.9163,
        "lng": 116.3972,
        "opentime": "08:30-17:00",
        "rating": 4.9,
        "address": "北京市东城区景山前街4号",
    },
    {
        "shop_id": "s2",
        "name": "天安门广场",
        "category": "scenic",
        "lat": 39.9087,
        "lng": 116.3975,
        "opentime": "05:00-22:00",
        "rating": 4.8,
        "address": "北京市东城区",
    },
    {
        "shop_id": "s3",
        "name": "王府井步行街",
        "category": "shopping",
        "lat": 39.9148,
        "lng": 116.4107,
        "opentime": "10:00-22:00",
        "rating": 4.5,
        "address": "北京市东城区王府井大街",
    },
    {
        "shop_id": "s4",
        "name": "圆明园",
        "category": "scenic",
        "lat": 40.0085,
        "lng": 116.2981,
        "opentime": "07:00-19:00",
        "rating": 4.7,
        "address": "北京市海淀区清华西路28号",
    },
    {
        "shop_id": "s5",
        "name": "八达岭长城",
        "category": "scenic",
        "lat": 40.3543,
        "lng": 116.0200,
        "opentime": "06:30-19:00",
        "rating": 4.9,
        "address": "北京市延庆区",
    },
    {
        "shop_id": "s6",
        "name": "天坛公园",
        "category": "scenic",
        "lat": 39.8822,
        "lng": 116.4066,
        "opentime": "06:00-21:00",
        "rating": 4.7,
        "address": "北京市东城区天坛内东里7号",
    },
    {
        "shop_id": "s7",
        "name": "颐和园",
        "category": "scenic",
        "lat": 39.9999,
        "lng": 116.2755,
        "opentime": "06:30-20:00",
        "rating": 4.8,
        "address": "北京市海淀区新建宫门路19号",
    },
]

# 天气数据 (2026-07-11 到 2026-07-15)
weather_data = {
    "2026-07-11": {
        "day_weather": "中雨",
        "day_temp": 28,
        "walking_penalty": 0.5,
        "outdoor_suitable": False,
    },
    "2026-07-12": {
        "day_weather": "雷阵雨",
        "day_temp": 30,
        "walking_penalty": 0.4,
        "outdoor_suitable": False,
    },
    "2026-07-13": {
        "day_weather": "中雨",
        "day_temp": 27,
        "walking_penalty": 0.5,
        "outdoor_suitable": False,
    },
    "2026-07-14": {
        "day_weather": "多云",
        "day_temp": 32,
        "walking_penalty": 0.9,
        "outdoor_suitable": True,
    },
    "2026-07-15": {
        "day_weather": "晴",
        "day_temp": 33,
        "walking_penalty": 1.0,
        "outdoor_suitable": True,
    },
}

# 酒店坐标（假设在市中心）
checkin_lat = 39.9150
checkin_lng = 116.4040


def print_separator(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def analyze_step_by_step():
    """逐步追踪排程算法，打印中间结果。"""

    print_separator("输入数据")
    print(f"POI 数量: {len(candidate_shops)}")
    for s in candidate_shops:
        print(f"  {s['name']} ({s['category']}) @ ({s['lat']}, {s['lng']})")
    print(f"天数: 5")
    print(f"交通: 飞机去 {travel_info['outbound_departure_time']}→{travel_info['outbound_arrival_time']}, "
          f"飞机回 {travel_info['return_departure_time']}")

    # ============================================================
    # Step 0: 计算 Day1 开始和 Day5 结束
    # ============================================================
    print_separator("Step 0: 计算 Day1 到达时间 & Day5 返程约束")
    skip_day1, day1_start, station_to_hotel_min, afternoon_ok, evening_ok = _compute_day1_start(
        travel_info, hotel_lat=checkin_lat, hotel_lng=checkin_lng)
    last_day_end, last_day_morning_feasible, must_leave_hotel = _compute_last_day_end(travel_info)

    print(f"Day 1:")
    print(f"  skip_day1 = {skip_day1}")
    print(f"  day1_start = {day1_start}")
    print(f"  station_to_hotel_min = {station_to_hotel_min}")
    print(f"  afternoon_ok = {afternoon_ok}")
    print(f"  evening_ok = {evening_ok}")
    print(f"Day 5:")
    print(f"  last_day_end = {last_day_end}")
    print(f"  last_day_morning_feasible = {last_day_morning_feasible}")
    print(f"  must_leave_hotel = {must_leave_hotel}")

    # ============================================================
    # Step 1: 地理聚类
    # ============================================================
    print_separator("Step 1: 地理聚类 (KMeans++, k=5)")

    # 只对POI聚类（不含餐）
    poi_shops = [s for s in candidate_shops if s.get("category", "") not in MAIN_MEAL_CATEGORIES]
    clusters = _cluster_by_geo(poi_shops, 5)

    for i, cluster in enumerate(clusters):
        names = [s['name'] for s in cluster]
        centroid = _cluster_centroid(cluster) if hasattr(sys.modules[__name__], '_cluster_centroid') else None
        print(f"  Day {i+1}: {len(cluster)} POIs → {names}")
        for s in cluster:
            # 计算与其他POI的距离
            dists = []
            for s2 in poi_shops:
                if s2['shop_id'] != s['shop_id']:
                    d = _haversine_m(float(s['lat']), float(s['lng']),
                                    float(s2['lat']), float(s2['lng']))
                    dists.append((s2['name'], int(d)))
            dists.sort(key=lambda x: x[1])
            print(f"    {s['name']}: 距离最近的: {dists[:3]}")

    # ============================================================
    # Step 1.5: 全局微调
    # ============================================================
    print_separator("Step 1.5: 全局微调 (边界交换)")
    clusters = _global_fine_tune(clusters, checkin_lat, checkin_lng)
    for i, cluster in enumerate(clusters):
        names = [s['name'] for s in cluster]
        print(f"  Day {i+1}: {names}")

    # ============================================================
    # Step 2: 负载均衡
    # ============================================================
    print_separator("Step 2: 负载均衡")
    clusters = _balance_clusters(clusters, max_hours_per_day=8.0, max_scenic_per_day=5,
                                  transport_preference="公共交通")
    for i, cluster in enumerate(clusters):
        names = [s['name'] for s in cluster]
        scenic_count = sum(1 for s in cluster if s.get('category') == 'scenic')
        print(f"  Day {i+1}: {len(cluster)} POIs ({scenic_count} scenic) → {names}")

    # ============================================================
    # Step 2.1: 均衡后重新微调
    # ============================================================
    clusters = _global_fine_tune(clusters, checkin_lat, checkin_lng)

    # ============================================================
    # Step 3: 完整运行 solve_multi_day
    # ============================================================
    print_separator("Step 3: 完整 solve_multi_day 运行结果")

    result = solve_multi_day(
        candidate_shops=candidate_shops,
        num_days=5,
        checkin_lat=checkin_lat,
        checkin_lng=checkin_lng,
        transport_preference="公共交通",
        start_time_str="09:00",
        max_hours_per_day=8.0,
        travel_info=travel_info,
        weather_data=weather_data,
        preferences={"commute": {"walking_tolerance_meters": 3000}},
    )

    # 输出每天的时间线
    for day in result.get("days", []):
        print(f"\n{'─'*60}")
        print(f"  {day['label']} (day_index={day.get('day_index')})")
        print(f"  总活动时间: {day.get('total_duration_minutes', 0)}分钟")
        print(f"  总交通时间: {day.get('total_travel_minutes', 0)}分钟")
        print(f"  POI pairs: {day.get('pairs', [])}")
        print(f"  时间线:")
        for node in day.get("timeline", []):
            action = node.get('action', '')
            time_str = node.get('time', '??:??')
            memo = node.get('memo', '')
            duration = node.get('duration_minutes', 0)
            shop_id = node.get('shop_id', '')

            # 高亮异常
            flag = ""
            if time_str == "00:00" or (action == "VISIT" and time_str < "05:00"):
                flag = " ⚠️ BUG: 时间异常!"
            if action == "VISIT" and time_str < "13:00" and day.get('day_index') == 0:
                flag = " ⚠️ BUG: Day1到达前就排了景点!"

            print(f"    {time_str} | {action:20s} | {memo[:50]:50s} | {duration:3d}min{flag}")

        # 检查 unassigned
        unassigned = day.get("unassigned_shops", [])
        if unassigned:
            print(f"  ⚠️ 未排入的店铺:")
            for us in unassigned:
                print(f"    - {us.get('name', '?')} ({us.get('status', '?')})")

    # 输出全局未分配
    unassigned_all = result.get("unassigned", [])
    if unassigned_all:
        print(f"\n{'─'*60}")
        print(f"  全局未分配 ({len(unassigned_all)} 项):")
        for ua in unassigned_all:
            print(f"    - {ua.get('name', '?')} ({ua.get('unassigned_type', '?')})")

    # 输出排程推理
    reasoning = result.get("algorithm_metadata", {}).get("schedule_reasoning", [])
    if reasoning:
        print(f"\n{'─'*60}")
        print(f"  排程推理:")
        for r in reasoning:
            print(f"    • {r}")

    return result


def _cluster_centroid(cluster):
    """计算聚类的质心"""
    if not cluster:
        return None
    lats = [float(s.get('lat', 0)) for s in cluster]
    lngs = [float(s.get('lng', 0)) for s in cluster]
    return (sum(lats) / len(lats), sum(lngs) / len(lngs))


if __name__ == "__main__":
    result = analyze_step_by_step()

    # 额外分析
    print_separator("额外分析")

    # 分析圆明园和颐和园的距离
    d_yuanmingyi_yiheyuan = _haversine_m(40.0085, 116.2981, 39.9999, 116.2755)
    print(f"圆明园 ↔ 颐和园 距离: {d_yuanmingyi_yiheyuan:.0f}m ({d_yuanmingyi_yiheyuan/1000:.1f}km)")

    # 分析故宫和天坛的距离
    d_gugong_tiantan = _haversine_m(39.9163, 116.3972, 39.8822, 116.4066)
    print(f"故宫 ↔ 天坛 距离: {d_gugong_tiantan:.0f}m ({d_gugong_tiantan/1000:.1f}km)")

    # 检查Day1时间线中的00:00节点
    for day in result.get("days", []):
        for node in day.get("timeline", []):
            if node.get("time") == "00:00":
                print(f"\n⚠️ 发现 00:00 时间节点!")
                print(f"  Day: {day.get('label')}")
                print(f"  Action: {node.get('action')}")
                print(f"  Memo: {node.get('memo')}")
                print(f"  Shop ID: {node.get('shop_id')}")
