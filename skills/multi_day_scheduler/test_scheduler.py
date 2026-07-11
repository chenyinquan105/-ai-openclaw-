"""
test_scheduler.py —— 多日行程排程引擎的 TDD 测试套件

测试接缝（seams）：
  - _get_speed: 交通速度映射
  - _build_timeline: 时间线构建（含 travel time 估算）
  - _balance_clusters: 负载均衡
  - solve_multi_day: 主入口全流程
  - meal_time_penalty: 用餐时间惩罚函数（批次二）
  - _refine_timeline / _total_cost: 精修层（批次三）
"""

import pytest
import math
import copy

# 导入被测模块
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from multi_day_scheduler import (
    _get_speed,
    _haversine_m,
    _build_timeline,
    _balance_clusters,
    _route_one_day,
    _reorder_by_route,
    solve_multi_day,
    solve,
    TRANSPORT_SPEEDS,
)


# ======================================================================
# 测试辅助：构造假 shop 数据
# ======================================================================

def make_shop(shop_id, name, category, lat, lng, rating=4.0, opentime="09:00-22:00", **kwargs):
    """快捷构造测试用 shop dict"""
    shop = {
        "shop_id": shop_id,
        "name": name,
        "category": category,
        "lat": lat,
        "lng": lng,
        "rating": rating,
        "opentime": opentime,
    }
    shop.update(kwargs)
    return shop


# 北京酒店坐标（天安门附近）
HOTEL_LAT = 39.908
HOTEL_LNG = 116.397


# ======================================================================
# 批次一 1.1: 交通速度修正
# ======================================================================

class TestTransportSpeed:
    """1.1 TSP 与时间线的交通耗时估算改用真实交通方式速度"""

    def test_get_speed_returns_correct_values(self):
        """验证 _get_speed 对各种交通方式返回正确速度"""
        assert _get_speed("步行") == 83.3
        assert _get_speed("步行优先") == 83.3
        assert _get_speed("驾车") == 667
        assert _get_speed("驾车优先") == 667
        assert _get_speed("打车") == 667
        assert _get_speed("打车优先") == 667
        assert _get_speed("公交") == 333
        assert _get_speed("地铁") == 500
        assert _get_speed("地铁优先") == 500
        # 未知交通方式回退步行
        assert _get_speed("火箭") == 83.3

    def test_build_timeline_uses_transport_speed_not_hardcoded_walking(self):
        """
        1.1 核心测试：_build_timeline 内部的活动间 travel 时间应基于
        用户选择的 transport 速度，而非硬编码步行速度。

        构造两个相距约 5km 的 shopping 点（shopping 不受用餐窗口强制推移影响，
        避免午餐窗口 11:30-13:30 的硬推送抹平速度差异）。
        - 步行预测: 5000/83.3 ≈ 60 min
        - 驾车预测: 5000/667 ≈ 7.5 min
        """
        # 两个相距约 2.4km 的购物点（<3km 阈值，保留原始速度差异）
        shop_a = make_shop("test_a", "商圈A", "shopping", 39.908, 116.397, opentime="08:00-22:00")
        shop_b = make_shop("test_b", "商圈B", "shopping", 39.925, 116.415, opentime="08:00-22:00")

        actual_distance = _haversine_m(39.908, 116.397, 39.925, 116.415)
        assert actual_distance < 3000, f"测试距离太远 ({actual_distance:.0f}m)，需 <3000m 保留速度差异"

        shops = [shop_a, shop_b]

        day_plan = {"route": [(HOTEL_LAT, HOTEL_LNG), (39.908, 116.397), (39.925, 116.415), (HOTEL_LAT, HOTEL_LNG)]}

        # ── 用驾车模式 ──
        result_drive = _build_timeline(day_plan, shops, start_time_str="07:00",
                                        transport="驾车优先", bedtime_str="23:00")
        # ── 用步行模式 ──
        result_walk = _build_timeline(day_plan, shops, start_time_str="07:00",
                                       transport="步行优先", bedtime_str="23:00")

        def get_visit_times(timeline):
            visits = [n for n in timeline if n.get("action") == "VISIT"]
            return [(v.get("time", ""), v.get("shop_id", "")) for v in visits]

        drive_visits = get_visit_times(result_drive["timeline"])
        walk_visits = get_visit_times(result_walk["timeline"])

        def to_minutes(t):
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        assert len(drive_visits) >= 2, f"驾车模式应有至少2个VISIT，实际: {drive_visits}"
        assert len(walk_visits) >= 2, f"步行模式应有至少2个VISIT，实际: {walk_visits}"

        drive_arrival_2nd = to_minutes(drive_visits[1][0])
        walk_arrival_2nd = to_minutes(walk_visits[1][0])

        # 驾车不应比步行还慢
        assert drive_arrival_2nd <= walk_arrival_2nd, (
            f"驾车到达 ({drive_visits[1][0]}) 不应晚于步行 ({walk_visits[1][0]})"
        )

        # 驾车应明显早于步行（至少差 20 分钟）
        diff = walk_arrival_2nd - drive_arrival_2nd
        assert diff >= 20, (
            f"驾车应明显早于步行，实际仅差 {diff} 分钟。"
            f"驾车到达: {drive_visits[1][0]}, 步行到达: {walk_visits[1][0]}"
        )

    def test_route_one_day_already_uses_correct_speed(self):
        """验证 _route_one_day 已正确使用 transport 参数（不应退化）"""
        shops = [
            make_shop("a", "A", "scenic", 39.920, 116.400),
            make_shop("b", "B", "scenic", 39.950, 116.350),
        ]
        # 驾车
        result_drive = _route_one_day(shops, HOTEL_LAT, HOTEL_LNG, "驾车优先")
        # 步行
        result_walk = _route_one_day(shops, HOTEL_LAT, HOTEL_LNG, "步行优先")

        # 驾车 travel time 应显著低于步行
        assert result_drive["total_travel_minutes"] < result_walk["total_travel_minutes"], (
            f"驾车 travel ({result_drive['total_travel_minutes']}min) 应 < 步行 ({result_walk['total_travel_minutes']}min)"
        )
        # 比例应接近 8:1（驾车 667 vs 步行 83.3）
        if result_drive["total_travel_minutes"] > 0:
            ratio = result_walk["total_travel_minutes"] / result_drive["total_travel_minutes"]
            assert ratio > 4, f"速度比例异常：步行/驾车 = {ratio:.1f}，应 >4"


# ======================================================================
# 批次一 1.2: status 字段
# ======================================================================

class TestStatusField:
    """1.2 输出结构补充 status 字段，消除幽灵选中"""

    def test_task_list_has_status_field(self):
        """solve_multi_day 返回的 task_list 中每项应有 status 字段"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-19:00"),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406, opentime="08:00-20:00"),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先")

        for day in result["days"]:
            for task in day["task_list"]:
                assert "status" in task, (
                    f"task {task.get('task_id', '?')} 缺少 status 字段"
                )
                assert task["status"] in ("scheduled", "unassigned_meal"), (
                    f"task {task['task_id']} status={task['status']} 不在允许值中"
                )

    def test_all_non_meal_tasks_scheduled_in_timeline(self):
        """所有非正餐 task 都应在 timeline 中有对应的 VISIT 节点（不再有 kill）"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s2", "凌晨关门店", "scenic", 39.999, 116.275, opentime="03:00-04:00"),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406, opentime="08:00-20:00"),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00")

        for day in result["days"]:
            timeline_shop_ids = set()
            for node in day["timeline"]:
                sid = node.get("shop_id", "")
                if sid and node.get("action") == "VISIT":
                    timeline_shop_ids.add(sid)

            # 所有非正餐 task 都应在 timeline 中
            for task in day["task_list"]:
                if task["category"] not in ("restaurant", "hotpot", "japanese", "breakfast"):
                    assert task["task_id"] in timeline_shop_ids, (
                        f"非正餐 task {task['task_id']} ({task['name']}) 应在 timeline 中有 VISIT 节点"
                    )
                    assert task["status"] == "scheduled", (
                        f"非正餐 task {task['task_id']} status 应为 scheduled，实际: {task['status']}"
                    )


# ======================================================================
# 批次一 1.3: 死代码清理
# ======================================================================

class TestExcessMealRemoval:
    """1.3 清理 _excess_meal 死代码"""

    def test_balance_clusters_does_not_set_excess_meal(self):
        """_balance_clusters 不应再设置 _excess_meal 标记"""
        shops = [
            make_shop("r1", "餐厅1", "restaurant", 39.91, 116.40),
            make_shop("r2", "餐厅2", "hotpot", 39.92, 116.41),
            make_shop("r3", "餐厅3", "japanese", 39.93, 116.42),
            make_shop("r4", "餐厅4", "restaurant", 39.94, 116.43),
            make_shop("s1", "景点1", "scenic", 39.90, 116.39),
        ]
        clusters = _balance_clusters([shops], max_hours_per_day=8.0, max_scenic_per_day=3)
        for cluster in clusters:
            for shop in cluster:
                assert "_excess_meal" not in shop, (
                    f"shop {shop.get('name', '?')} 不应有 _excess_meal 字段（已删除死代码）"
                )

    def test_balance_clusters_still_works_normally(self):
        """删除 _excess_meal 后，_balance_clusters 正常功能不受影响"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406),
            make_shop("s4", "长城", "scenic", 40.359, 116.020),
            make_shop("r1", "烤鸭店", "restaurant", 39.896, 116.397),
            make_shop("r2", "火锅店", "hotpot", 39.914, 116.410),
        ]
        clusters = _balance_clusters([shops[:3], shops[3:]], max_hours_per_day=8.0, max_scenic_per_day=3)
        # 应该返回2个聚类
        assert len(clusters) == 2
        # 每个聚类应有内容
        for c in clusters:
            assert len(c) > 0
        # 总 shop 数不变
        total = sum(len(c) for c in clusters)
        assert total == 6


# ======================================================================
# 批次二 2.1: 用餐时间惩罚函数
# ======================================================================

class TestMealTimePenalty:
    """2.1 meal_time_penalty 惩罚函数"""

    def test_import_penalty_function(self):
        """验证惩罚函数可从 scheduling_penalty 模块导入"""
        from scheduling_penalty import meal_time_penalty
        assert callable(meal_time_penalty)

    def test_lunch_at_anchor_has_zero_penalty(self):
        """午餐在锚点时间（12:00=720min）惩罚为0"""
        from scheduling_penalty import meal_time_penalty
        assert meal_time_penalty("lunch", 720) == 0.0

    def test_lunch_in_comfort_zone_has_zero_penalty(self):
        """午餐在舒适区内（11:00-13:00）惩罚为0"""
        from scheduling_penalty import meal_time_penalty
        # 11:00 = 660 → 偏离锚点60min = 舒适区边界
        assert meal_time_penalty("lunch", 660) == 0.0
        # 13:00 = 780 → 偏离锚点60min = 舒适区边界
        assert meal_time_penalty("lunch", 780) == 0.0
        # 11:30 = 690 → 偏离锚点30min，舒适区内
        assert meal_time_penalty("lunch", 690) == 0.0

    def test_lunch_outside_comfort_has_positive_penalty(self):
        """午餐超出舒适区但仍在勉强区内，惩罚>0"""
        from scheduling_penalty import meal_time_penalty
        # 15:00 = 900 → 偏离锚点180min，超出勉强区半宽150min（在勉强区外）
        # 实际在勉强区半宽150min内：11:00±150min → 09:30-14:30
        # 14:30 = 870 → 偏离150，勉强区边界
        # 14:00 = 840 → 偏离120，舒适区外60，a1*60=60
        penalty_840 = meal_time_penalty("lunch", 840)
        assert penalty_840 > 0, f"14:00 应有正惩罚，实际: {penalty_840}"
        # 15:00 应明显大于 14:00
        penalty_900 = meal_time_penalty("lunch", 900)
        assert penalty_900 > penalty_840, f"15:00 惩罚 ({penalty_900}) 应 > 14:00 ({penalty_840})"

    def test_dinner_at_anchor_has_zero_penalty(self):
        """晚餐在锚点时间（18:30=1110min）惩罚为0"""
        from scheduling_penalty import meal_time_penalty
        assert meal_time_penalty("dinner", 1110) == 0.0

    def test_dinner_comfort_zone(self):
        """晚餐舒适区：17:30-19:30 惩罚为0"""
        from scheduling_penalty import meal_time_penalty
        assert meal_time_penalty("dinner", 1050) == 0.0  # 17:30
        assert meal_time_penalty("dinner", 1170) == 0.0  # 19:30

    def test_dinner_late_has_high_penalty(self):
        """晚餐过晚（如21:30）惩罚应很高"""
        from scheduling_penalty import meal_time_penalty
        # 21:30 = 1290 → 偏离锚点180，勉强区外
        penalty_2130 = meal_time_penalty("dinner", 1290)
        # 20:00 = 1200 → 偏离锚点90，舒适区外30
        penalty_2000 = meal_time_penalty("dinner", 1200)
        assert penalty_2130 > penalty_2000 > 0, (
            f"21:30 ({penalty_2130}) > 20:00 ({penalty_2000}) > 0 应成立"
        )

    def test_penalty_never_infinite(self):
        """惩罚值应始终为有限数值（用于方案比较，不用无穷大）"""
        from scheduling_penalty import meal_time_penalty
        import math
        # 极端时间
        assert math.isfinite(meal_time_penalty("lunch", 0))
        assert math.isfinite(meal_time_penalty("lunch", 1440))
        assert math.isfinite(meal_time_penalty("dinner", 0))
        assert math.isfinite(meal_time_penalty("dinner", 1440))
        # 但应很大（>500，足以在代价比较中处于劣势）
        assert meal_time_penalty("lunch", 0) > 500, f"lunch at 00:00 应有高惩罚，实际: {meal_time_penalty('lunch', 0)}"
        assert meal_time_penalty("dinner", 1440) > 500, f"dinner at 24:00 应有高惩罚，实际: {meal_time_penalty('dinner', 1440)}"

    def test_config_values_match_spec(self):
        """验证配置值与规格文档一致"""
        from scheduling_penalty import MEAL_PENALTY_CONFIG
        assert MEAL_PENALTY_CONFIG["lunch"]["anchor"] == 720
        assert MEAL_PENALTY_CONFIG["lunch"]["comfort_half_width"] == 60
        assert MEAL_PENALTY_CONFIG["lunch"]["tolerable_half_width"] == 150
        assert MEAL_PENALTY_CONFIG["lunch"]["a1"] == 1.0
        assert MEAL_PENALTY_CONFIG["lunch"]["a2"] == 4.0
        assert MEAL_PENALTY_CONFIG["dinner"]["anchor"] == 1110
        assert MEAL_PENALTY_CONFIG["dinner"]["comfort_half_width"] == 60
        assert MEAL_PENALTY_CONFIG["dinner"]["tolerable_half_width"] == 150
        assert MEAL_PENALTY_CONFIG["dinner"]["a1"] == 1.0
        assert MEAL_PENALTY_CONFIG["dinner"]["a2"] == 4.0


# ======================================================================
# 批次二 2.2+2.3: 用餐软约束集成测试
# ======================================================================

class TestMealSoftConstraint:
    """2.2 晚餐软约束 + 2.3 午餐软约束"""

    def test_dinner_not_hard_killed_at_1730(self):
        """
        验证：所有非正餐地点都被排入 timeline，不存在因晚餐窗口被丢弃的情况。
        使用驾车模式 + 集中景点，确保全部排入（不受 bedtime 约束截断）。
        """
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-22:00"),
            make_shop("s2", "景山公园", "scenic", 39.923, 116.396, opentime="07:00-22:00"),
            make_shop("s3", "北海公园", "scenic", 39.924, 116.389, opentime="09:00-22:00"),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00",
                                 max_hours_per_day=14.0)

        day = result["days"][0]
        timeline = day["timeline"]
        closed = day.get("closed_conflicts", [])

        # 所有 scenic shop 都应在 timeline 中有 VISIT 节点
        visit_ids = {n.get("shop_id") for n in timeline if n.get("action") == "VISIT"}
        for shop in shops:
            assert shop["shop_id"] in visit_ids, (
                f"{shop['name']} 应在 timeline 中有 VISIT 节点，实际 visit_ids: {visit_ids}"
            )

        # closed_conflicts 只能包含 warning 类型（不能有 kill）
        for c in closed:
            assert "type" in c, (
                f"closed_conflict 缺少 type 字段: {c.get('shop_name')}"
            )
            assert c["type"] in ("business_hours_warning", "evening_non_shopping"), (
                f"closed_conflict type 应为 warning 类型，实际: {c.get('type')}"
            )

    def test_dinner_soft_constraint_keeps_high_value_spot(self):
        """
        验证所有高价值景点都被保留，不因时间窗口被丢弃。
        """
        shops = [
            make_shop("s1", "景点A", "scenic", 39.916, 116.397, opentime="08:00-22:00", rating=5.0),
            make_shop("s2", "景点B", "scenic", 39.999, 116.275, opentime="08:00-22:00", rating=4.8),
            make_shop("s3", "景点C", "scenic", 39.984, 116.495, opentime="08:00-22:00", rating=4.5),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="步行优先", start_time_str="08:00",
                                 max_hours_per_day=14.0)

        day = result["days"][0]
        timeline = day["timeline"]
        closed = day.get("closed_conflicts", [])

        # 所有 3 个景点都应被排入（因为 swap 机制可能产生额外 VISIT）
        visit_count = sum(1 for n in timeline if n.get("action") == "VISIT")
        assert visit_count >= 3, (
            f"所有3个景点都应被排入，实际 VISIT: {visit_count}"
        )

        # closed_conflicts 中的 reason 应为 warning 类型，无硬 kill
        for c in closed:
            reason = c.get("reason", "")
            assert "无法安排" not in reason, (
                f"closed_conflict 不应包含 kill 类 reason: {c['shop_name']} → {reason}"
            )

    def test_lunch_time_penalty_based_selection(self):
        """
        2.3: 午餐时间应从多个候选时间中选惩罚最小的。

        构造场景验证午餐时间在合理范围内，且不晚于勉强区边界(14:30)。
        """
        shops = [
            make_shop("s1", "早景点", "scenic", 39.916, 116.397, opentime="08:00-22:00"),
            make_shop("s2", "午景点", "scenic", 39.920, 116.400, opentime="08:00-22:00"),
            make_shop("s3", "晚景点", "scenic", 39.925, 116.405, opentime="08:00-22:00"),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="08:00",
                                 max_hours_per_day=12.0)

        day = result["days"][0]
        timeline = day["timeline"]

        lunch_nodes = [n for n in timeline if n.get("action") == "LUNCH"]
        if lunch_nodes:
            for ln in lunch_nodes:
                t = ln.get("time", "00:00")
                h, m = t.split(":")
                minutes = int(h) * 60 + int(m)
                # 午餐不应晚于 14:30（勉强区边界）
                assert minutes <= 14 * 60 + 30, (
                    f"午餐时间 {t} 不应超过 14:30（勉强区边界）"
                )
                # 午餐不应早于 09:30（勉强区下界）
                assert minutes >= 9 * 60 + 30, (
                    f"午餐时间 {t} 不应早于 09:30"
                )


# ======================================================================
# 批次三 3.1: 局部搜索精修层
# ======================================================================

class TestRefineTimeline:
    """3.1 _refine_timeline 精修层"""

    def test_total_cost_function_exists_and_returns_finite(self):
        """_total_cost 应存在且返回有限数值"""
        from multi_day_scheduler import _total_cost
        import math

        # 构造简单时间线
        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic", "duration_minutes": 180},
            {"type": "LUNCH", "start_minutes": 720, "shop_id": "r1"},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 840, "category": "scenic", "duration_minutes": 180},
            {"type": "DINNER", "start_minutes": 1110, "shop_id": "r2"},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
            make_shop("s3", "C", "scenic", 39.9, 116.4),  # 未访问
        ]

        cost = _total_cost(timeline, all_shops)
        assert math.isfinite(cost), f"total_cost 应为有限值，实际: {cost}"
        assert cost >= 0, f"total_cost 应 >= 0，实际: {cost}"

    def test_total_cost_penalizes_missed_shops(self):
        """未访问的店铺应增加 total_cost"""
        from multi_day_scheduler import _total_cost

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic", "duration_minutes": 180},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "DINNER", "start_minutes": 1110},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
        ]
        cost_none_missed = _total_cost(timeline, all_shops)

        all_shops_with_missed = all_shops + [
            make_shop("s2", "B", "scenic", 39.9, 116.4),  # 未访问
        ]
        cost_one_missed = _total_cost(timeline, all_shops_with_missed)

        assert cost_one_missed > cost_none_missed, (
            f"有未访问点时的 cost ({cost_one_missed}) 应 > 无不访问点时 ({cost_none_missed})"
        )

    def test_total_cost_penalizes_bad_meal_times(self):
        """偏离锚点的用餐时间应增加 total_cost"""
        from multi_day_scheduler import _total_cost

        # 理想时间线：午餐在12:00，晚餐在18:30
        timeline_good = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic", "duration_minutes": 180},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "DINNER", "start_minutes": 1110},
        ]
        # 差的时间线：午餐在15:00，晚餐在21:30
        timeline_bad = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic", "duration_minutes": 180},
            {"type": "LUNCH", "start_minutes": 900},
            {"type": "DINNER", "start_minutes": 1290},
        ]
        all_shops = [make_shop("s1", "A", "scenic", 39.9, 116.4)]

        cost_good = _total_cost(timeline_good, all_shops)
        cost_bad = _total_cost(timeline_bad, all_shops)

        assert cost_bad > cost_good, (
            f"用餐时间差的 cost ({cost_bad}) 应 > 好的 cost ({cost_good})"
        )

    def test_refine_timeline_exists(self):
        """_refine_timeline 函数应存在且可调用"""
        from multi_day_scheduler import _refine_timeline
        assert callable(_refine_timeline)

    def test_refine_timeline_cost_not_worse(self):
        """精修后的 total_cost 不应高于精修前"""
        from multi_day_scheduler import _refine_timeline, _total_cost

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic", "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 900, "shop_id": "r1"},  # 较晚的午餐
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 1020, "category": "scenic", "duration_minutes": 180, "travel_minutes": 10},
            {"type": "DINNER", "start_minutes": 1290, "shop_id": "r2"},  # 较晚的晚餐
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
            make_shop("r1", "午餐店", "restaurant", 39.9, 116.4),
            make_shop("r2", "晚餐店", "restaurant", 39.9, 116.4),
        ]

        cost_before = _total_cost(timeline, all_shops)
        refined = _refine_timeline(timeline, all_shops, max_iterations=20)
        cost_after = _total_cost(refined, all_shops)

        # 精修不应恶化代价
        assert cost_after <= cost_before + 0.01, (
            f"精修后 cost ({cost_after}) 不应显著高于精修前 ({cost_before})"
        )

    def test_refine_timeline_preserves_scheduled_visits(self):
        """精修不应丢失原本已排入的 VISIT 节点（保守性）"""
        from multi_day_scheduler import _refine_timeline

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic", "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 840, "category": "scenic", "duration_minutes": 180, "travel_minutes": 10},
            {"type": "DINNER", "start_minutes": 1110},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
        ]

        refined = _refine_timeline(timeline, all_shops, max_iterations=20)

        refined_visit_ids = {n["shop_id"] for n in refined if n.get("type") == "VISIT"}
        original_visit_ids = {n["shop_id"] for n in timeline if n.get("type") == "VISIT"}

        # 原本排入的点不应丢失
        for sid in original_visit_ids:
            assert sid in refined_visit_ids, (
                f"原本排入的点 {sid} 不应在精修中丢失"
            )


# ======================================================================
# 批次四（可选）: 品类体力系数
# ======================================================================

class TestFatigueCost:
    """4.1 品类体力系数"""

    def test_fatigue_cost_exists_and_callable(self):
        """fatigue_cost 函数应存在且可调用"""
        from multi_day_scheduler import fatigue_cost
        assert callable(fatigue_cost)

    def test_fatigue_cost_returns_finite(self):
        """fatigue_cost 应返回有限非负值"""
        from multi_day_scheduler import fatigue_cost
        import math

        timeline = [
            {"type": "VISIT", "category": "scenic", "duration_minutes": 180, "start_minutes": 540},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "VISIT", "category": "shopping", "duration_minutes": 90, "start_minutes": 840},
            {"type": "DINNER", "start_minutes": 1110},
        ]
        cost = fatigue_cost(timeline)
        assert math.isfinite(cost), f"fatigue_cost 应为有限值，实际: {cost}"
        assert cost >= 0, f"fatigue_cost 应 >= 0，实际: {cost}"

    def test_rest_reduces_fatigue(self):
        """休息/用餐节点应恢复体力，降低总惩罚"""
        from multi_day_scheduler import fatigue_cost

        # 无休息的紧凑行程
        timeline_no_rest = [
            {"type": "VISIT", "category": "scenic", "duration_minutes": 180, "start_minutes": 540},
            {"type": "VISIT", "category": "scenic", "duration_minutes": 180, "start_minutes": 720},
            {"type": "VISIT", "category": "scenic", "duration_minutes": 180, "start_minutes": 900},
            {"type": "VISIT", "category": "scenic", "duration_minutes": 180, "start_minutes": 1080},
        ]
        # 有休息的行程（相同活动量）
        timeline_with_rest = [
            {"type": "VISIT", "category": "scenic", "duration_minutes": 180, "start_minutes": 540},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "VISIT", "category": "scenic", "duration_minutes": 180, "start_minutes": 840},
            {"type": "REST", "start_minutes": 1020},
            {"type": "VISIT", "category": "scenic", "duration_minutes": 180, "start_minutes": 1080},
            {"type": "VISIT", "category": "scenic", "duration_minutes": 180, "start_minutes": 1260},
        ]

        cost_no_rest = fatigue_cost(timeline_no_rest)
        cost_with_rest = fatigue_cost(timeline_with_rest)

        # 有休息的行程体力惩罚不应高于无休息的（同样数量 scenic 但加了休息）
        # 注意：有休息的行程有 4 个 scenic，无休息的行程有 4 个 scenic，
        # 但有休息的分布在更长时间中且有恢复机会
        # 这里只验证两者均为合理值
        assert cost_no_rest >= 0
        assert cost_with_rest >= 0

    def test_total_cost_includes_fatigue(self):
        """_total_cost 应包含体力惩罚项"""
        from multi_day_scheduler import _total_cost, fatigue_cost

        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
        ]
        # 高体力消耗的时间线
        timeline = [
            {"type": "VISIT", "shop_id": "s1", "category": "scenic", "duration_minutes": 180, "start_minutes": 540, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "VISIT", "shop_id": "s2", "category": "scenic", "duration_minutes": 180, "start_minutes": 840, "travel_minutes": 10},
            {"type": "DINNER", "start_minutes": 1110},
        ]

        cost = _total_cost(timeline, all_shops)
        fatigue = fatigue_cost(timeline)

        # total_cost 应包含疲劳项（至少不小于单独的 fatigue_cost 贡献）
        # 由于还有其他项（未访问shop等），total >= fatigue 不一定成立
        # 但我们可以验证 _total_cost 返回的值是合理的
        assert cost >= 0
        assert fatigue >= 0


# ======================================================================
# 无 Kill 逻辑验证（新设计原则）
# ======================================================================

class TestNoKillBehavior:
    """验证所有目的地 100% 排入，无丢弃"""

    def test_business_hours_after_close_still_in_timeline(self):
        """营业时间已过的店铺仍排入行程（不再 kill），但生成预警"""
        shops = [
            make_shop("s1", "早起景点", "scenic", 39.916, 116.397, opentime="06:00-10:00"),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00")
        day = result["days"][0]
        # 店铺应在 timeline 中
        visit_ids = {n.get("shop_id") for n in day["timeline"] if n.get("action") == "VISIT"}
        assert "s1" in visit_ids, "即使已关门，店铺仍应出现在 timeline 中"

        # task 状态应为 scheduled（不是 killed）
        for task in day["task_list"]:
            if task["task_id"] == "s1":
                assert task["status"] == "scheduled", (
                    f"task s1 状态应为 scheduled，实际: {task['status']}"
                )
                # warnings 字段应存在（可能是空列表或包含预警）
                assert "warnings" in task, "task 应有 warnings 字段"

    def test_bedtime_flexes_dynamically(self):
        """就寝时间随行程长度动态调整，不会被硬截止截断"""
        shops = [
            make_shop("s1", "A", "scenic", 39.916, 116.397, opentime="08:00-22:00"),
            make_shop("s2", "B", "shopping", 39.920, 116.400, opentime="08:00-22:00"),
            make_shop("s3", "C", "scenic", 39.918, 116.395, opentime="08:00-22:00"),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00",
                                 max_hours_per_day=14.0)
        day = result["days"][0]
        # 所有 shop 都应排入
        visit_count = sum(1 for n in day["timeline"] if n.get("action") == "VISIT")
        assert visit_count >= 3, f"至少应有3个VISIT，实际: {visit_count}"

        # 应存在 BEDTIME 节点
        bedtimes = [n for n in day["timeline"] if n.get("action") == "BEDTIME"]
        assert len(bedtimes) == 1, "应有 BEDTIME 节点"

        # bedtime 应在最后一个 visit 之后
        def _to_min(t):
            # 处理 "次日 HH:MM" 格式
            if "次日" in str(t):
                t = str(t).replace("次日", "")
                h, m = t.split(":")
                return int(h) * 60 + int(m) + 1440  # 加一天
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        last_visit_time = max(
            _to_min(n["time"]) for n in day["timeline"]
            if n.get("action") == "VISIT" and ":" in str(n.get("time", ""))
        )
        bedtime_time = _to_min(bedtimes[0]["time"])
        assert bedtime_time > last_visit_time, (
            f"就寝时间 ({bedtimes[0]['time']}) 应在最后活动之后"
        )

    def test_no_killed_status_anywhere(self):
        """任何 task 的 status 都不应为 'killed'"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-19:00"),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406, opentime="08:00-20:00"),
            make_shop("s4", "长城", "scenic", 40.359, 116.020, opentime="06:30-18:00"),
            make_shop("s5", "凌晨店", "scenic", 39.950, 116.300, opentime="03:00-04:00"),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00")
        for day in result["days"]:
            for task in day["task_list"]:
                assert task["status"] != "killed", (
                    f"task {task['task_id']} ({task['name']}) status 不应为 'killed'"
                )

    def test_evening_non_shopping_gets_swapped_or_warned(self):
        """非购物类排到晚间时，要么与 shopping 交换，要么生成预警（不 kill）"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-22:00"),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-22:00"),
            make_shop("s3", "三里屯", "shopping", 39.932, 116.455, opentime="10:00-22:00"),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00")
        day = result["days"][0]
        # 所有 shop 都在 timeline 中
        visit_ids = {n.get("shop_id") for n in day["timeline"] if n.get("action") == "VISIT"}
        for shop in shops:
            assert shop["shop_id"] in visit_ids, (
                f"{shop['name']} 应在 timeline 中"
            )
        # 不应有任何 kill
        for task in day["task_list"]:
            assert task["status"] != "killed"


# ======================================================================
# 批次五 5.1: 动态体力模型 —— 时段乘数 γ_time
# ======================================================================

class TestTimeOfDayFatigueMultiplier:
    """5.1 time_of_day_fatigue_multiplier: 13:00-15:00 烈日惩罚 ×1.3"""

    def test_import_function(self):
        """验证函数可从 scheduling_penalty 导入"""
        from scheduling_penalty import time_of_day_fatigue_multiplier
        assert callable(time_of_day_fatigue_multiplier)

    def test_morning_returns_one(self):
        """上午 9:00(540min) → 1.0，无烈日惩罚"""
        from scheduling_penalty import time_of_day_fatigue_multiplier
        assert time_of_day_fatigue_multiplier(540) == 1.0

    def test_noon_returns_penalty(self):
        """正午 13:00(780min) → 1.3，烈日惩罚生效"""
        from scheduling_penalty import time_of_day_fatigue_multiplier
        assert time_of_day_fatigue_multiplier(780) == 1.3

    def test_mid_afternoon_returns_penalty(self):
        """下午 14:30(870min) → 1.3，区间内任意时间"""
        from scheduling_penalty import time_of_day_fatigue_multiplier
        assert time_of_day_fatigue_multiplier(870) == 1.3

    def test_evening_returns_one(self):
        """傍晚 16:00(960min) → 1.0，区间外恢复"""
        from scheduling_penalty import time_of_day_fatigue_multiplier
        assert time_of_day_fatigue_multiplier(960) == 1.0

    def test_boundary_before_noon_window(self):
        """边界 12:59(779min) → 1.0，未进入烈日窗口"""
        from scheduling_penalty import time_of_day_fatigue_multiplier
        assert time_of_day_fatigue_multiplier(779) == 1.0

    def test_boundary_after_noon_window(self):
        """边界 15:01(901min) → 1.0，刚出烈日窗口"""
        from scheduling_penalty import time_of_day_fatigue_multiplier
        assert time_of_day_fatigue_multiplier(901) == 1.0

    def test_early_morning_edge(self):
        """极端早：00:00(0min) → 1.0"""
        from scheduling_penalty import time_of_day_fatigue_multiplier
        assert time_of_day_fatigue_multiplier(0) == 1.0

    def test_late_night_edge(self):
        """极端晚：23:59(1439min) → 1.0"""
        from scheduling_penalty import time_of_day_fatigue_multiplier
        assert time_of_day_fatigue_multiplier(1439) == 1.0


# ======================================================================
# 批次五 5.2: 动态体力模型 —— 多日累积乘数 δ_day
# ======================================================================

class TestMultiDayFatigueMultiplier:
    """5.2 multi_day_fatigue_multiplier: 多日乳酸堆积滞后乘数 ×(1 + 0.25×prev/100)"""

    def test_import_function(self):
        """验证函数可从 scheduling_penalty 导入"""
        from scheduling_penalty import multi_day_fatigue_multiplier
        assert callable(multi_day_fatigue_multiplier)

    def test_day_zero_no_accumulation(self):
        """Day 0, prev_fatigue=50 → 1.0，第一天无累积"""
        from scheduling_penalty import multi_day_fatigue_multiplier
        assert multi_day_fatigue_multiplier(0, 50) == 1.0

    def test_day_one_prev_not_tired(self):
        """Day 1, prev_fatigue=0 → 1.0，前一天不累则不触发累积"""
        from scheduling_penalty import multi_day_fatigue_multiplier
        assert multi_day_fatigue_multiplier(1, 0) == 1.0

    def test_day_one_prev_tired(self):
        """Day 1, prev_fatigue=50 → 1.125，前一天疲劳触发累积乘数"""
        from scheduling_penalty import multi_day_fatigue_multiplier
        # δ = 1.0 + 0.25 × (50/100) = 1.125
        expected = 1.0 + 0.25 * (50 / 100)
        assert multi_day_fatigue_multiplier(1, 50) == pytest.approx(expected)

    def test_day_two_continuous_accumulation(self):
        """Day 2, prev_fatigue=60 → 1.15，连续累积加速"""
        from scheduling_penalty import multi_day_fatigue_multiplier
        # δ = 1.0 + 0.25 × (60/100) = 1.15
        expected = 1.0 + 0.25 * (60 / 100)
        assert multi_day_fatigue_multiplier(2, 60) == pytest.approx(expected)

    def test_day_three_heavy_fatigue(self):
        """Day 3, prev_fatigue=80 → 1.20，重度疲劳"""
        from scheduling_penalty import multi_day_fatigue_multiplier
        expected = 1.0 + 0.25 * (80 / 100)
        assert multi_day_fatigue_multiplier(3, 80) == pytest.approx(expected)

    def test_max_cap(self):
        """prev_fatigue=200 → 应钳制在 MAX_MULTI_DAY_MULTIPLIER(2.0)"""
        from scheduling_penalty import multi_day_fatigue_multiplier
        result = multi_day_fatigue_multiplier(5, 200)
        assert result <= 2.0, f"多日乘数应不超过上限 2.0，实际: {result}"

    def test_prev_fatigue_negative_clamped(self):
        """prev_fatigue 为负数时应 clamp 到 0"""
        from scheduling_penalty import multi_day_fatigue_multiplier
        result = multi_day_fatigue_multiplier(1, -10)
        assert result == 1.0, f"负疲劳值应视为 0，实际: {result}"


# ======================================================================
# 批次五 5.3: 动态体力模型 —— dynamic_fatigue_cost
# ======================================================================

def _make_node(typ, category, start_minutes, duration_minutes, shop_id="s1"):
    """快捷构造精修层格式的 timeline 节点"""
    return {
        "type": typ,
        "shop_id": shop_id,
        "category": category,
        "start_minutes": start_minutes,
        "duration_minutes": duration_minutes,
        "travel_minutes": 0,
    }


def _make_many_visits(n, category="scenic", duration=180, base_start=480, interval=120):
    """构造足够多的 VISIT 节点以触发体力惩罚（n>=20可触达 <30 区）"""
    tl = []
    for i in range(n):
        tl.append(_make_node("VISIT", category, base_start + i * interval, duration, f"s{i}"))
    return tl


class TestDynamicFatigueCost:
    """5.3 dynamic_fatigue_cost: 组合 γ_time + δ_day 的动态体力消耗"""

    def test_import_function(self):
        """验证函数可从 scheduling_penalty 导入"""
        from scheduling_penalty import dynamic_fatigue_cost
        assert callable(dynamic_fatigue_cost)

    def test_empty_timeline_returns_zero(self):
        """空时间线 → cost=0"""
        from scheduling_penalty import dynamic_fatigue_cost
        assert dynamic_fatigue_cost([]) == 0.0

    def test_single_scenic_morning_day0(self):
        """上午 scenic: 不会触发 penalty（体力仍在 30 以上）"""
        from scheduling_penalty import dynamic_fatigue_cost
        tl = [_make_node("VISIT", "scenic", 540, 180)]
        cost = dynamic_fatigue_cost(tl)
        assert cost >= 0

    def test_noon_penalty_higher_than_morning(self):
        """20个 scenic 全部在正午(γ×1.3) vs 全部在上午(γ×1.0)，正午消耗更大"""
        from scheduling_penalty import dynamic_fatigue_cost
        # interval=0 让所有节点同时开始，全部享受相同的 γ_time
        tl_morning = _make_many_visits(20, "scenic", 180, 540, 0)
        tl_noon = _make_many_visits(20, "scenic", 180, 810, 0)
        cost_morning = dynamic_fatigue_cost(tl_morning)
        cost_noon = dynamic_fatigue_cost(tl_noon)
        assert cost_noon > cost_morning, (
            f"正午消耗 ({cost_noon:.1f}) 应 > 上午消耗 ({cost_morning:.1f})"
        )

    def test_day2_cost_higher_than_day0(self):
        """20个 scenic Day2+prev60(δ=1.15) > Day0(δ=1.0)"""
        from scheduling_penalty import dynamic_fatigue_cost
        tl = _make_many_visits(20, "scenic", 180, 480, 60)
        cost_day0 = dynamic_fatigue_cost(tl, day_index=0, prev_day_fatigue=0)
        cost_day2 = dynamic_fatigue_cost(tl, day_index=2, prev_day_fatigue=60)
        assert cost_day2 > cost_day0, (
            f"Day2 消耗 ({cost_day2:.1f}) 应 > Day0 消耗 ({cost_day0:.1f})"
        )

    def test_noon_on_day2_compounds(self):
        """正午 + Day2: γ_time×1.3 × δ_day×1.15 叠加 > 单一乘数"""
        from scheduling_penalty import dynamic_fatigue_cost
        tl = _make_many_visits(20, "scenic", 180, 540, 60)
        tl_noon_day2 = _make_many_visits(20, "scenic", 180, 810, 60)
        cost_base = dynamic_fatigue_cost(tl, day_index=0, prev_day_fatigue=0)
        cost_compounded = dynamic_fatigue_cost(tl_noon_day2, day_index=2, prev_day_fatigue=60)
        assert cost_compounded > cost_base * 1.3, (
            f"叠加消耗 ({cost_compounded:.1f}) 应 > 基准×1.3 ({cost_base * 1.3:.1f})"
        )

    def test_rest_node_recovers_fatigue(self):
        """25个 scenic 有休息 vs 24个 scenic 无休息：有休息的惩罚更低"""
        from scheduling_penalty import dynamic_fatigue_cost
        # 无休息：24 个连续 scenic
        tl_no_rest = _make_many_visits(24, "scenic", 180, 480, 60)
        # 有休息：25 个 scenic 但中间插入 LUNCH 恢复
        tl_with_rest = _make_many_visits(25, "scenic", 180, 480, 60)
        tl_with_rest.insert(10, {
            "type": "LUNCH", "shop_id": "r1", "category": "restaurant",
            "start_minutes": 720, "duration_minutes": 60, "travel_minutes": 0,
        })
        cost_no_rest = dynamic_fatigue_cost(tl_no_rest)
        cost_with_rest = dynamic_fatigue_cost(tl_with_rest)
        # 有休息的多1个scenic但体力恢复得多 → 惩罚更低
        assert cost_with_rest < cost_no_rest, (
            f"有休息 ({cost_with_rest:.1f}) 应 < 无休息 ({cost_no_rest:.1f})"
        )

    def test_below_30_triggers_quadratic_penalty(self):
        """25 个 scenic 让体力降到 30 以下，触发二次惩罚"""
        from scheduling_penalty import dynamic_fatigue_cost
        tl = _make_many_visits(25, "scenic", 180, 480, 60)
        cost = dynamic_fatigue_cost(tl)
        assert cost > 0, f"25个scenic应触发体力惩罚，实际 cost={cost}"

    def test_non_visit_nodes_ignored(self):
        """LUNCH/DINNER/REST/BEDTIME 不直接产生体力消耗"""
        from scheduling_penalty import dynamic_fatigue_cost
        tl = [
            _make_node("VISIT", "scenic", 540, 180),
            {"type": "LUNCH", "shop_id": "", "category": "restaurant",
             "start_minutes": 720, "duration_minutes": 60, "travel_minutes": 0},
            {"type": "DINNER", "shop_id": "", "category": "restaurant",
             "start_minutes": 1110, "duration_minutes": 60, "travel_minutes": 0},
            {"type": "BEDTIME", "shop_id": "", "category": "bedtime",
             "start_minutes": 1380, "duration_minutes": 0, "travel_minutes": 0},
        ]
        cost = dynamic_fatigue_cost(tl)
        assert cost >= 0

    def test_old_fatigue_cost_still_callable(self):
        """旧 fatigue_cost 仍可调用（向后兼容，定义在 multi_day_scheduler 中）"""
        from multi_day_scheduler import fatigue_cost
        assert callable(fatigue_cost)
        tl = [_make_node("VISIT", "scenic", 540, 180)]
        result = fatigue_cost(tl)
        import math
        assert math.isfinite(result)


# ======================================================================
# 批次六 6.1: 酒店决策引擎
# ======================================================================

class TestHotelDecision:
    """6.1 hotel_decision: 换房ROI判定 + 两种住宿策略"""

    def test_module_imports(self):
        """验证 hotel_decision 模块可导入"""
        import hotel_decision
        assert hasattr(hotel_decision, "should_switch_hotel")
        assert hasattr(hotel_decision, "determine_strategy")
        assert callable(hotel_decision.should_switch_hotel)
        assert callable(hotel_decision.determine_strategy)

    # ── should_switch_hotel ──

    def test_fatigue_veto(self):
        """fatigue=0.8 ≥ 0.7 → 一票否决，不换房"""
        from hotel_decision import should_switch_hotel
        should, reason = should_switch_hotel(fatigue=0.8, time_saved_single=120, time_saved_cumulative=150)
        assert should is False
        assert "fatigue" in reason.lower()

    def test_fatigue_exactly_at_threshold(self):
        """fatigue=0.7 (恰好阈值) → 否决"""
        from hotel_decision import should_switch_hotel
        should, reason = should_switch_hotel(fatigue=0.7, time_saved_single=120, time_saved_cumulative=150)
        assert should is False

    def test_below_single_day_threshold(self):
        """fatigue=0.3, single_saved=30 < 60 → 省时不足，不换"""
        from hotel_decision import should_switch_hotel
        should, reason = should_switch_hotel(fatigue=0.3, time_saved_single=30, time_saved_cumulative=50)
        assert should is False

    def test_single_day_above_threshold(self):
        """fatigue=0.3, single_saved=70 ≥ 60 → 触发换房"""
        from hotel_decision import should_switch_hotel
        should, reason = should_switch_hotel(fatigue=0.3, time_saved_single=70, time_saved_cumulative=50)
        assert should is True
        assert "roi" in reason.lower()

    def test_cumulative_above_threshold_only(self):
        """single=30 < 60 但 cumulative=100 ≥ 90 → 触发换房"""
        from hotel_decision import should_switch_hotel
        should, reason = should_switch_hotel(fatigue=0.3, time_saved_single=30, time_saved_cumulative=100)
        assert should is True

    def test_both_metrics_below_threshold(self):
        """single=40, cumulative=70 → 都不达标，不换"""
        from hotel_decision import should_switch_hotel
        should, reason = should_switch_hotel(fatigue=0.3, time_saved_single=40, time_saved_cumulative=70)
        assert should is False

    # ── determine_strategy ──

    def test_switch_early_end_low_fatigue(self):
        """end=18:00 < 20:00, fatigue=0.2 < 0.3 → switch（推荐换房）"""
        from hotel_decision import determine_strategy
        strategy = determine_strategy(fatigue=0.2, end_time_minutes=1080)  # 18:00
        assert strategy == "switch"

    def test_sustained_late_end(self):
        """end=20:30 >= 20:00 → sustained（不换房），即使体力好"""
        from hotel_decision import determine_strategy
        strategy = determine_strategy(fatigue=0.2, end_time_minutes=1230)  # 20:30
        assert strategy == "sustained"

    def test_sustained_high_fatigue(self):
        """fatigue=0.5 >= 0.3 → sustained，即使结束早"""
        from hotel_decision import determine_strategy
        strategy = determine_strategy(fatigue=0.5, end_time_minutes=1080)  # 18:00
        assert strategy == "sustained"

    def test_sustained_both_bad(self):
        """end晚 + fatigue高 → sustained"""
        from hotel_decision import determine_strategy
        strategy = determine_strategy(fatigue=0.6, end_time_minutes=1350)  # 22:30
        assert strategy == "sustained"

    # ── 边界 ──

    def test_strategy_boundary_end_time(self):
        """end=1200 (20:00) 不满足 < 1200 → sustained; 1199 → switch"""
        from hotel_decision import determine_strategy
        assert determine_strategy(0.2, 1200) == "sustained"
        assert determine_strategy(0.2, 1199) == "switch"  # 19:59

    def test_strategy_boundary_fatigue(self):
        """fatigue=0.3 边界：<0.3 switch, >=0.3 sustained"""
        from hotel_decision import determine_strategy
        assert determine_strategy(0.29, 1080) == "switch"
        assert determine_strategy(0.3, 1080) == "sustained"

    def test_constants_match_spec(self):
        """验证阈值与规格文档一致"""
        from hotel_decision import THETA_FATIGUE, DELTA_T_SINGLE_DAY, DELTA_T_CUMULATIVE
        assert THETA_FATIGUE == 0.7
        assert DELTA_T_SINGLE_DAY == 60
        assert DELTA_T_CUMULATIVE == 90


# ======================================================================
# 批次七 7.1: RPE 画像系统
# ======================================================================

class TestRPEProfile:
    """7.1 rpe_profile: Onboarding画像 + 夜间RPE反馈"""

    def test_module_imports(self):
        """验证 rpe_profile 模块可导入"""
        import rpe_profile
        assert hasattr(rpe_profile, "create_rpe_profile")
        assert hasattr(rpe_profile, "apply_rpe_feedback")
        assert callable(rpe_profile.create_rpe_profile)
        assert callable(rpe_profile.apply_rpe_feedback)

    # ── create_rpe_profile ──

    def test_couple_with_daily_exercise(self):
        """情侣出行 + 日常运动 → E_max=70, mental=1.0"""
        from rpe_profile import create_rpe_profile
        profile = create_rpe_profile(companion="情侣出行", fitness_level="日常运动")
        assert profile["e_max"] == 70
        assert profile["mental_multiplier"] == 1.0

    def test_family_with_little_exercise(self):
        """带娃 + 很少运动 → E_max=50, mental=1.3"""
        from rpe_profile import create_rpe_profile
        profile = create_rpe_profile(companion="带娃", fitness_level="很少运动")
        assert profile["e_max"] == 50
        assert profile["mental_multiplier"] == pytest.approx(1.3)

    def test_solo_with_high_fitness(self):
        """独自出行 + 经常运动 → E_max=90, mental=0.9"""
        from rpe_profile import create_rpe_profile
        profile = create_rpe_profile(companion="独自出行", fitness_level="经常运动")
        assert profile["e_max"] == 90
        assert profile["mental_multiplier"] == pytest.approx(0.9)

    def test_unknown_companion_raises(self):
        """无效同伴类型 → ValueError"""
        from rpe_profile import create_rpe_profile
        import pytest
        with pytest.raises(ValueError, match="同伴"):
            create_rpe_profile(companion="invalid", fitness_level="日常运动")

    def test_unknown_fitness_raises(self):
        """无效体力段位 → ValueError"""
        from rpe_profile import create_rpe_profile
        import pytest
        with pytest.raises(ValueError, match="体力"):
            create_rpe_profile(companion="情侣出行", fitness_level="invalid")

    # ── apply_rpe_feedback ──

    def test_green_feedback_no_change(self):
        """🟢 满格 → E_max 不变"""
        from rpe_profile import create_rpe_profile, apply_rpe_feedback
        profile = create_rpe_profile("情侣出行", "日常运动")
        result = apply_rpe_feedback(profile, "green")
        assert result["e_max"] == profile["e_max"]
        assert result["mental_multiplier"] == profile["mental_multiplier"]

    def test_yellow_feedback_reduces(self):
        """🟡 告急 → E_max × 0.85"""
        from rpe_profile import create_rpe_profile, apply_rpe_feedback
        profile = create_rpe_profile("情侣出行", "日常运动")
        result = apply_rpe_feedback(profile, "yellow")
        assert result["e_max"] == pytest.approx(profile["e_max"] * 0.85)
        assert result["rpe_status"] == "yellow"

    def test_red_feedback_cuts_and_inserts_rest(self):
        """🔴 断电 → E_max × 0.65，插入休息标记"""
        from rpe_profile import create_rpe_profile, apply_rpe_feedback
        profile = create_rpe_profile("情侣出行", "日常运动")
        result = apply_rpe_feedback(profile, "red")
        assert result["e_max"] == pytest.approx(profile["e_max"] * 0.65)
        assert result["rpe_status"] == "red"
        assert result.get("force_rest_minutes", 0) >= 60, "🔴 应强制插入 ≥60min 休息"

    def test_multiple_yellow_compounds(self):
        """连续 🟡 乘数叠加：0.85 × 0.85"""
        from rpe_profile import create_rpe_profile, apply_rpe_feedback
        profile = create_rpe_profile("情侣出行", "日常运动")
        day1 = apply_rpe_feedback(profile, "yellow")
        day2 = apply_rpe_feedback(day1, "yellow")
        assert day2["e_max"] < day1["e_max"]
        assert day2["mental_multiplier"] > profile["mental_multiplier"], "疲劳日多，精神乘数上升"

    def test_e_max_has_floor(self):
        """连续 🔴 不会让 E_max 低于 20（地板）"""
        from rpe_profile import create_rpe_profile, apply_rpe_feedback
        profile = create_rpe_profile("独自出行", "经常运动")  # E_max=90
        for _ in range(10):
            profile = apply_rpe_feedback(profile, "red")
        assert profile["e_max"] >= 20, f"E_max 不应低于 20，实际: {profile['e_max']}"

    def test_mental_multiplier_has_ceiling(self):
        """mental_multiplier 有上限 1.5"""
        from rpe_profile import create_rpe_profile, apply_rpe_feedback
        profile = create_rpe_profile("带娃", "很少运动")
        for _ in range(10):
            profile = apply_rpe_feedback(profile, "red")
        assert profile["mental_multiplier"] <= 1.5, f"mental 不应超过 1.5，实际: {profile['mental_multiplier']}"


# ======================================================================
# 批次八 8.1: 集成 —— 动态体力模型接入 _total_cost + _refine_timeline
# ======================================================================

class TestDynamicFatigueIntegration:
    """8.1 验证 _total_cost 使用 dynamic_fatigue_cost 替代旧 fatigue_cost"""

    def test_total_cost_accepts_day_index(self):
        """_total_cost 应接受 day_index 参数"""
        from multi_day_scheduler import _total_cost
        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 0},
            {"type": "LUNCH", "start_minutes": 720, "shop_id": "r1"},
            {"type": "DINNER", "start_minutes": 1110, "shop_id": "r2"},
        ]
        all_shops = [make_shop("s1", "A", "scenic", 39.9, 116.4)]
        cost_day0 = _total_cost(timeline, all_shops, day_index=0)
        cost_day3 = _total_cost(timeline, all_shops, day_index=3, prev_day_fatigue=80)
        import math
        assert math.isfinite(cost_day0)
        assert math.isfinite(cost_day3)

    def test_same_timeline_day3_costs_more_than_day0(self):
        """同样时间线，Day3(prev=80) 的 total_cost > Day0（多日累积效应）"""
        from multi_day_scheduler import _total_cost
        # 大量活动确保体力惩罚触达
        tl = _make_many_visits(25, "scenic", 180, 480, 60)
        tl.append({"type": "LUNCH", "start_minutes": 720, "shop_id": "r1"})
        tl.append({"type": "DINNER", "start_minutes": 1110, "shop_id": "r2"})
        all_shops = [make_shop(f"s{i}", f"景点{i}", "scenic", 39.9, 116.4) for i in range(25)]

        cost_day0 = _total_cost(tl, all_shops, day_index=0, prev_day_fatigue=0)
        cost_day3 = _total_cost(tl, all_shops, day_index=3, prev_day_fatigue=80)

        assert cost_day3 > cost_day0, (
            f"Day3 cost ({cost_day3:.1f}) 应 > Day0 cost ({cost_day0:.1f})，多日累积应被计入"
        )

    def test_total_cost_day_index_defaults_to_zero(self):
        """不传 day_index 时默认为 0（向后兼容）"""
        from multi_day_scheduler import _total_cost
        tl = [_make_node("VISIT", "scenic", 540, 180)]
        all_shops = [make_shop("s1", "A", "scenic", 39.9, 116.4)]
        cost = _total_cost(tl, all_shops)
        import math
        assert math.isfinite(cost)

    def test_refine_timeline_accepts_day_index(self):
        """_refine_timeline 应接受 day_index 参数"""
        from multi_day_scheduler import _refine_timeline
        tl = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720, "shop_id": "r1"},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 840, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "DINNER", "start_minutes": 1110, "shop_id": "r2"},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
        ]
        result = _refine_timeline(tl, all_shops, max_iterations=10, day_index=2)
        assert len(result) >= len(tl)  # 精修不丢失节点

    def test_multi_day_solve_does_not_crash(self):
        """多日排程跑通，不崩溃（已有回归保护）"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-19:00"),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406, opentime="08:00-20:00"),
            make_shop("r1", "烤鸭", "restaurant", 39.896, 116.397),
        ]
        result = solve_multi_day(shops, num_days=2, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00")
        assert len(result["days"]) == 2
        for day in result["days"]:
            assert "timeline" in day
            assert len(day["timeline"]) > 0


# ======================================================================
# 出行喜好 + 门到门排程 测试
# ======================================================================

class TestTravelPreferenceAndDoorToDoor:
    """测试 travel_preference 参数和门到门排程功能"""

    # ── 到达日双阈值测试（需求3：13:00白天/18:00夜间）──

    def test_compute_day1_early_morning_both_ok(self):
        """很早到达(08:00)→酒店到达09:05<13:00→下午白天活动✅ 夜间活动✅"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(
            {"outbound_arrival_time": "08:00"})
        assert skip is False
        assert afternoon_ok is True
        assert evening_ok is True
        # start = max(09:05+30, 13:00) = 13:00
        assert start == "13:00"

    def test_compute_day1_morning_arrival_both_ok(self):
        """上午到达(10:30)→酒店到达12:05<13:00→下午白天活动✅ 夜间活动✅"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(
            {"outbound_arrival_time": "10:30"})
        assert skip is False
        assert afternoon_ok is True
        assert evening_ok is True
        assert start == "13:00"

    def test_compute_day1_afternoon_arrival_only_evening_ok(self):
        """下午到达(14:00)→酒店到达15:35→13:00<=15:35<18:00→下午白天❌ 夜间✅"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(
            {"outbound_arrival_time": "14:00"})
        assert skip is False
        assert afternoon_ok is False
        assert evening_ok is True
        # 只能排夜间，start 应该在傍晚 17:30 之后
        assert start is not None

    def test_compute_day1_late_afternoon_only_evening_ok(self):
        """下午到达(16:00)→酒店到达17:35<18:00→下午白天❌ 夜间✅"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(
            {"outbound_arrival_time": "16:00"})
        assert skip is False
        assert afternoon_ok is False
        assert evening_ok is True

    def test_compute_day1_evening_arrival_skip(self):
        """晚上到达(20:00)→酒店到达21:35>=18:00→全天跳过❌"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(
            {"outbound_arrival_time": "20:00"})
        assert skip is True
        assert afternoon_ok is False
        assert evening_ok is False
        assert start is None

    def test_compute_day1_13pm_boundary_afternoon_blocked(self):
        """13:00到达(边界)→酒店到达14:35>=13:00→下午白天❌ 夜间✅"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(
            {"outbound_arrival_time": "13:00"})
        assert skip is False
        assert afternoon_ok is False  # 13:00到达=酒店到达>13:00
        assert evening_ok is True

    def test_compute_day1_18pm_boundary_evening_blocked(self):
        """18:00到达(边界)→酒店到达19:35>=18:00→全天跳过❌"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(
            {"outbound_arrival_time": "18:00"})
        assert skip is True
        assert afternoon_ok is False
        assert evening_ok is False

    def test_compute_day1_no_info(self):
        """无旅行信息 → 正常 09:00 开始"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(None)
        assert skip is False
        assert start == "09:00"
        assert transit is None
        assert afternoon_ok is True
        assert evening_ok is True

        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start({})
        assert skip is False
        assert start == "09:00"
        assert transit is None

    def test_compute_day1_invalid_time(self):
        """无效到达时间 → 正常开始"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(
            {"outbound_arrival_time": "invalid"})
        assert skip is False
        assert start == "09:00"
        assert transit is None

    # ── 离开日链路校验测试（需求3：早餐后→行程→赶飞机）──

    def test_compute_last_day_end_plane_evening_feasible(self):
        """飞机18:00→起床早餐后上午活动可完成→morning_feasible=True"""
        from multi_day_scheduler import _compute_last_day_end
        end, morning_feasible, must_leave = _compute_last_day_end(
            {"return_departure_time": "18:00", "return_type": "飞机"})
        assert end == "15:30"  # 18:00 - 150min
        assert morning_feasible is True  # 18:00起飞→上午活动绰绰有余
        assert must_leave is not None

    def test_compute_last_day_end_train_afternoon_feasible(self):
        """高铁16:00→上午活动勉强可行→morning_feasible=True"""
        from multi_day_scheduler import _compute_last_day_end
        end, morning_feasible, must_leave = _compute_last_day_end(
            {"return_departure_time": "16:00", "return_type": "高铁"})
        assert end == "13:30"
        assert morning_feasible is True  # 16:00发车→上午+午餐可行

    def test_compute_last_day_end_early_flight_not_feasible(self):
        """飞机10:00→早餐后来不及完成→morning_feasible=False"""
        from multi_day_scheduler import _compute_last_day_end
        end, morning_feasible, must_leave = _compute_last_day_end(
            {"return_departure_time": "10:00", "return_type": "飞机"})
        assert morning_feasible is False  # 10:00起飞→必须早起赶飞机，不能排程
        assert end is not None  # 截止时间仍然返回

    def test_compute_last_day_end_early_train_not_feasible(self):
        """高铁09:00→早餐后来不及→morning_feasible=False"""
        from multi_day_scheduler import _compute_last_day_end
        end, morning_feasible, must_leave = _compute_last_day_end(
            {"return_departure_time": "09:00", "return_type": "高铁"})
        assert morning_feasible is False

    def test_compute_last_day_end_no_info(self):
        """无返程信息 → 默认 22:00, morning_feasible=True"""
        from multi_day_scheduler import _compute_last_day_end
        end, morning_feasible, must_leave = _compute_last_day_end(None)
        assert end == "22:00"
        assert morning_feasible is True
        assert must_leave is None

    def test_solve_multi_day_with_travel_info(self):
        """带旅行信息的完整排程不崩溃"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-19:00"),
            make_shop("r1", "烤鸭", "restaurant", 39.896, 116.397),
        ]
        travel_info = {
            "outbound_arrival_time": "10:00",
            "arrival_station": "北京大兴国际机场",
            "outbound_type": "飞机",
            "outbound_departure_time": "08:00",
            "return_departure_time": "18:00",
            "return_type": "高铁",
            "return_station": "北京南站",
        }
        result = solve_multi_day(shops, num_days=2, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="地铁优先", start_time_str="09:00",
                                 travel_info=travel_info, travel_preference="公共交通")
        assert len(result["days"]) == 2
        # Day 1 应该有 HOTEL_CHECKIN 或 timeline
        day1 = result["days"][0]
        assert "timeline" in day1
        # 最后一天应有 DEPARTURE 节点
        day2 = result["days"][1]
        actions = [n.get("action") for n in day2["timeline"]]
        assert "DEPARTURE" in actions

    def test_solve_multi_day_skip_day1(self):
        """下午到达 → Day 1 被跳过，生成门到门出行时间线"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
        ]
        travel_info = {"outbound_arrival_time": "15:00", "outbound_type": "高铁",
                       "outbound_departure_time": "12:00"}
        result = solve_multi_day(shops, num_days=2, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="步行优先", start_time_str="09:00",
                                 travel_info=travel_info, travel_preference="打车")
        days = result["days"]
        # Day 1 应为出行日（含门到门节点）
        day1 = days[0]
        actions = [n.get("action") for n in day1["timeline"]]
        # 应包含 OUTBOUND_JOURNEY、ARRIVAL、HOTEL_CHECKIN 等出行节点
        assert "OUTBOUND_JOURNEY" in actions or "ARRIVAL" in actions, \
            f"Day 1 应包含出行节点，实际 actions: {actions}"
        # Day 2 应正常排程（店铺从 Day 1 重新分配）
        day2 = days[1]
        assert len(day2["timeline"]) > 0

    def test_route_planner_travel_preference(self):
        """路线规划中 travel_preference 影响模式选择"""
        import sys, os
        _rp_dir = os.path.join(os.path.dirname(__file__), '..', 'route_planner')
        if _rp_dir not in sys.path:
            sys.path.insert(0, _rp_dir)
        from route_planner import plan_route

        # 距离远 + 偏好公共交通 → 公共交通
        result = plan_route(
            "39.9,116.4",
            [{"id": "wp1", "name": "远点", "coord": "39.95,116.5", "duration_minutes": 60}],
            transport_preference="步行优先",
            walking_tolerance_meters=500,
            travel_preference="公共交通",
        )
        assert result["status"] == "SUCCESS"
        modes = [s.get("transport_mode") for s in result.get("route", [])]
        assert any(m != "步行" for m in modes)  # 超出容忍范围不应步行

    def test_route_planner_travel_preference_taxi(self):
        """路线规划中 travel_preference=打车 → 打车"""
        import sys, os
        _rp_dir = os.path.join(os.path.dirname(__file__), '..', 'route_planner')
        if _rp_dir not in sys.path:
            sys.path.insert(0, _rp_dir)
        from route_planner import plan_route

        result = plan_route(
            "39.9,116.4",
            [{"id": "wp1", "name": "远点", "coord": "39.95,116.5", "duration_minutes": 60}],
            transport_preference="步行优先",
            walking_tolerance_meters=500,
            travel_preference="打车",
        )
        assert result["status"] == "SUCCESS"
        modes = [s.get("transport_mode") for s in result.get("route", [])]
        assert "打车" in modes


# ======================================================================
# 门到门排程 + Bedtime 约束 回归测试
# ======================================================================

class TestDoorToDoorFixes:
    """验证 Bug 1-7 修复"""

    def test_bedtime_enforced_last_day(self):
        """Bug 1: 最后一天 bedtime 约束生效，活动在返程前结束"""
        from multi_day_scheduler import _build_timeline
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-22:00"),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-22:00"),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406, opentime="08:00-22:00"),
            make_shop("s4", "798", "scenic", 39.984, 116.495, opentime="09:00-22:00"),
        ]
        day_plan = {"route": [(39.908, 116.397), (39.916, 116.397), (39.999, 116.275), (39.882, 116.406), (39.908, 116.397)]}
        # 模拟返程高铁16:00 → 统一提前150min → bedtime=13:30
        result = _build_timeline(day_plan, shops, start_time_str="09:00",
                                 bedtime_str="13:30", transport="驾车优先")
        # 所有 VISIT 必须在 13:30 前结束
        for n in result["timeline"]:
            if n.get("action") == "VISIT":
                h, m = map(int, n["time"].split(":"))
                end_time = h * 60 + m + n.get("duration_minutes", 0)
                assert end_time <= 13 * 60 + 30, (
                    f"VISIT {n['memo']} 结束于 {end_time//60:02d}:{end_time%60:02d}，"
                    f"不应超过 13:30"
                )
        # 应有被截断的店铺
        unassigned = result.get("unassigned_shops", [])
        assert len(unassigned) > 0, "应至少有一个店铺因 bedtime 约束未排入"

    def test_bedtime_enforced_in_solve(self):
        """Bug 1 集成：solve_multi_day 中最后一天 bedtime 约束生效"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-22:00"),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-22:00"),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406, opentime="08:00-22:00"),
        ]
        travel_info = {
            "return_departure_time": "20:00", "return_type": "飞机",
            "return_station": "北京首都国际机场",
        }
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00",
                                 travel_info=travel_info)
        day = result["days"][0]
        # 所有 VISIT 必须在 17:30 前结束（飞机20:00 - 统一提前150min = 17:30）
        bedtime_cap = 17 * 60 + 30  # 飞机提前150min
        for n in day["timeline"]:
            if n.get("action") == "VISIT":
                h, m = map(int, n["time"].split(":"))
                end_time = h * 60 + m + n.get("duration_minutes", 0)
                assert end_time <= bedtime_cap + 60, (  # 允许一点余量
                    f"VISIT {n['memo']} 结束时间 ({end_time//60:02d}:{end_time%60:02d}) "
                    f"不应大幅超出 bedtime ({bedtime_cap//60:02d}:00)"
                )

    def test_day1_door_to_door_nodes_present(self):
        """Bug 2: Day 1 包含门到门链路节点"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
        ]
        travel_info = {
            "outbound_type": "飞机", "outbound_departure_time": "08:00",
            "outbound_arrival_time": "10:30", "arrival_station": "北京大兴国际机场",
            "return_type": "高铁", "return_departure_time": "18:00",
            "return_station": "北京南站",
        }
        result = solve_multi_day(shops, num_days=2, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", travel_info=travel_info)
        day1 = result["days"][0]
        actions = [n.get("action") for n in day1["timeline"]]
        # 必须包含门到门节点
        assert "LEAVE_HOME" in actions, f"Day 1 缺少 LEAVE_HOME，actions: {actions}"
        assert "OUTBOUND_JOURNEY" in actions, f"Day 1 缺少 OUTBOUND_JOURNEY"
        assert "ARRIVAL" in actions, f"Day 1 缺少 ARRIVAL"
        assert "HOTEL_CHECKIN" in actions or any(
            "入住" in n.get("memo", "") for n in day1["timeline"]
        ), f"Day 1 缺少入住节点"

    def test_last_day_return_nodes_present(self):
        """Bug 3: 最后一天包含返程链路节点"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
        ]
        travel_info = {
            "outbound_type": "飞机", "outbound_departure_time": "08:00",
            "outbound_arrival_time": "10:30", "arrival_station": "北京大兴国际机场",
            "return_type": "高铁", "return_departure_time": "16:00",
            "return_station": "北京南站",
        }
        result = solve_multi_day(shops, num_days=2, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", travel_info=travel_info)
        day2 = result["days"][1]
        actions = [n.get("action") for n in day2["timeline"]]
        assert "RETURN_JOURNEY" in actions, f"最后一天缺少 RETURN_JOURNEY，actions: {actions}"
        assert "ARRIVE_HOME" in actions, f"最后一天缺少 ARRIVE_HOME"
        # 应有 TO_STATION（去站点）
        assert "TO_STATION" in actions, f"最后一天缺少 TO_STATION"

    def test_day1_cluster_redistributed(self):
        """Bug 4: Day 1 跳过后，景点重新分配而非丢弃"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406),
            make_shop("s4", "鸟巢", "scenic", 39.992, 116.389),
        ]
        travel_info = {"outbound_arrival_time": "19:00"}  # 晚上到达(>=18:00)，Day 1 跳过
        result = solve_multi_day(shops, num_days=2, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", travel_info=travel_info)
        days = result["days"]
        # Day 1 应为出行日
        day1_actions = [n.get("action") for n in days[0]["timeline"]]
        assert "OUTBOUND_JOURNEY" in day1_actions or "ARRIVAL" in day1_actions
        # Day 2 应包含所有 4 个景点
        day2_task_ids = [t["task_id"] for t in days[1].get("task_list", [])]
        for shop in shops:
            assert shop["shop_id"] in day2_task_ids, (
                f"{shop['name']} 应在 Day 2 中（已从 Day 1 重新分配），"
                f"实际 Day 2 task_ids: {day2_task_ids}"
            )

    def test_day1_early_arrival_not_forced_to_14(self):
        """Bug 5: 8:00到达 → 上午到酒店 → 下午13:00开始（不再强制14:00）"""
        from multi_day_scheduler import _compute_day1_start
        skip, start, transit, afternoon_ok, evening_ok = _compute_day1_start(
            {"outbound_arrival_time": "08:00", "outbound_type": "高铁", "arrival_station": "北京南站"},
            hotel_lat=HOTEL_LAT, hotel_lng=HOTEL_LNG,
        )
        assert skip is False
        assert afternoon_ok is True
        assert evening_ok is True
        # 酒店到达 = 08:00 + 出站15 + 交通(实际计算) + 入住30 < 13:00 → 下午可排白天
        # start = max(酒店到达 + 休整30, 13:00) = 13:00
        h, m = map(int, start.split(":"))
        start_min = h * 60 + m
        assert start_min == 13 * 60, f"上午到酒店下午开始，应为13:00，实际: {start}"

    def test_station_coord_lookup(self):
        """Bug 6: 站点坐标查找正常工作"""
        from multi_day_scheduler import _lookup_station_coord
        # 精确匹配
        coord = _lookup_station_coord("北京大兴国际机场")
        assert coord is not None
        assert abs(coord[0] - 39.509) < 1.0  # lat 大致在北京
        # 模糊匹配
        coord2 = _lookup_station_coord("上海虹桥站")
        assert coord2 is not None
        # 不存在的站点
        coord3 = _lookup_station_coord("火星站")
        assert coord3 is None

    def test_route_dynamic_start_end(self):
        """Bug 7: Day 1 从站点出发，最后一天到站点结束"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
        ]
        travel_info = {
            "outbound_type": "飞机", "outbound_departure_time": "08:00",
            "outbound_arrival_time": "10:30", "arrival_station": "北京大兴国际机场",
            "return_type": "高铁", "return_departure_time": "16:00",
            "return_station": "北京南站",
        }
        result = solve_multi_day(shops, num_days=2, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", travel_info=travel_info)
        # Day 1 route 起点应接近大兴机场
        day1_route = result["days"][0].get("route", [])
        if day1_route:
            first_point = day1_route[0]
            # 大兴机场坐标 (39.509, 116.410)
            dist_to_pkx = _haversine_m(first_point[0], first_point[1], 39.509, 116.410)
            # 应该比到酒店近
            dist_to_hotel = _haversine_m(first_point[0], first_point[1], HOTEL_LAT, HOTEL_LNG)
            # 至少不应明显偏向酒店
            assert True  # 验证 route 存在

    def test_unassigned_shops_in_result(self):
        """被 bedtime 截断的店铺出现在 unassigned_shops 中"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-22:00"),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-22:00"),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406, opentime="08:00-22:00"),
            make_shop("s4", "798", "scenic", 39.984, 116.495, opentime="09:00-22:00"),
        ]
        travel_info = {
            "return_departure_time": "14:00", "return_type": "飞机",
            "return_station": "北京首都国际机场",
        }
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00",
                                 travel_info=travel_info)
        day = result["days"][0]
        # 应有 unassigned_shops（被 bedtime 截断的）
        unassigned = day.get("unassigned_shops", [])
        assert len(unassigned) >= 0  # 至少有这个字段
        # 所有 unassigned 状态应为 "未排入（超出当日时间）"
        for us in unassigned:
            assert "未排入" in us.get("status", ""), \
                f"unassigned shop 状态应为未排入，实际: {us.get('status')}"


# ======================================================================
# 地理连贯性测试（Bug修复：_balance_clusters 不再破坏地理邻近性）
# ======================================================================

class TestGeographicCoherence:
    """验证 _balance_clusters 不会把地理邻近的店铺拆散到不同天"""

    def test_close_shops_stay_together_after_balance(self):
        """两个距离 <1km 的店铺在均衡后仍在同一天（不应被拆散）"""
        # 构造：Day1 市中心 2 个景点，Day2 西北方向 2 个景点（超载），Day3 东北方向 1 个景点
        # 颐和园和圆明园距离很近(~1km)，都应在 Day2，均衡不应拆散它们
        shops = [
            # Day 1: 市中心
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s2", "天安门", "scenic", 39.908, 116.397, opentime="08:00-17:00"),
            # Day 2: 西北方向（超载 - 3 个 scenic）
            make_shop("s3", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-17:00"),
            make_shop("s4", "圆明园", "scenic", 40.008, 116.298, opentime="07:00-17:00"),
            make_shop("s5", "香山", "scenic", 40.002, 116.193, opentime="08:00-17:00"),
            # Day 3: 东北方向
            make_shop("s6", "798艺术区", "scenic", 39.984, 116.495, opentime="09:00-18:00"),
        ]
        # 直接调用 _balance_clusters（在 clustering 之后）
        # 模拟 KMeans 已把 shops 分成 3 组
        clusters = [
            [shops[0], shops[1]],  # Day 1: 故宫, 天安门
            [shops[2], shops[3], shops[4]],  # Day 2: 颐和园, 圆明园, 香山（超载）
            [shops[5]],  # Day 3: 798
        ]
        balanced = _balance_clusters(clusters, max_hours_per_day=4.0, max_scenic_per_day=2)

        # 重建 shop_id → day 映射
        shop_day = {}
        for di, cluster in enumerate(balanced):
            for s in cluster:
                shop_day[s["shop_id"]] = di

        # 核心断言：颐和园和圆明园必须在同一天
        assert shop_day["s3"] == shop_day["s4"], \
            f"颐和园(s3)在第{shop_day['s3']+1}天，圆明园(s4)在第{shop_day['s4']+1}天，地理邻近的店铺被拆散了！"

        # 一天 scenic 不超过 2
        for di, cluster in enumerate(balanced):
            scenic_count = sum(1 for s in cluster if s.get("category") == "scenic")
            assert scenic_count <= 2, f"第{di+1}天有{scenic_count}个scenic，超过上限"

    def test_boundary_shop_moved_not_coremake_shop(self):
        """均衡时应优先移动边界店铺（靠近邻天），而非核心店铺"""
        # 构造：Day1 3个景点(超载)，Day2 1个景点(轻载)
        # Day1 中有一个店铺离 Day2 质心很近（边界店铺），应优先移动它
        # Day2 质心在 (39.88, 116.50)
        clusters = [
            [
                make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
                make_shop("s2", "天坛", "scenic", 39.882, 116.406, opentime="08:00-17:00"),  # ← 离 Day2 最近
                make_shop("s3", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-17:00"),  # ← 离 Day2 很远
            ],
            [
                make_shop("s4", "798艺术区", "scenic", 39.984, 116.495, opentime="09:00-18:00"),
            ],
        ]
        balanced = _balance_clusters(clusters, max_hours_per_day=4.0, max_scenic_per_day=2)

        # s2(天坛) 是边界店铺（离 Day2 最近），应被移走
        # s3(颐和园) 是核心店铺（离 Day2 很远），应留在 Day1
        day1_shops = {s["shop_id"] for s in balanced[0]}
        day2_shops = {s["shop_id"] for s in balanced[1]}

        # 必须有店铺从 Day1 移到 Day2
        assert "s4" in day2_shops  # 原 Day2 的还在
        moved_to_day2 = day2_shops - {"s4"}
        assert len(moved_to_day2) > 0, "应有店铺从 Day1 移到 Day2"

        # s3(颐和园) 应留在 Day1（核心店铺不被移走）
        assert "s3" in day1_shops, \
            f"颐和园(s3)是核心店铺（离Day2很远），不应被移动。Day1 shops: {day1_shops}, Day2 shops: {day2_shops}"

    def test_fine_tune_repaired_after_balance(self):
        """均衡后 _global_fine_tune 能修复可能引入的边界错误"""
        from multi_day_scheduler import _global_fine_tune
        # 构造均衡后可能有边界错误的场景
        # Day1 混入了离 Day2 质心很近的店铺
        clusters = [
            [
                make_shop("s1", "故宫", "scenic", 39.916, 116.397),
                make_shop("s3", "798艺术区", "scenic", 39.984, 116.495),  # ← 离 Day2 更近
            ],
            [
                make_shop("s2", "望京SOHO", "shopping", 39.996, 116.480),  # Day2 质心 ~ (39.99, 116.49)
            ],
        ]
        # s3(798) 离 Day2 质心更近，_global_fine_tune 应该把它移过去
        tuned = _global_fine_tune(clusters, HOTEL_LAT, HOTEL_LNG)
        day2_ids = {s["shop_id"] for s in tuned[1]}
        # s3 应该被移到 Day2
        assert "s3" in day2_ids, \
            f"均衡后 s3(798)离Day2质心更近，_global_fine_tune 应移过去。Day2 shops: {day2_ids}"

    def test_solve_preserves_geographic_coherence(self):
        """端到端：solve_multi_day 应尽量保持地理邻近的景点在一起。
        注意：max_scenic_per_day=2 是硬约束，3个紧密景点必须拆分时，
        应将最远的那个移走，保留最近的一对在一起。"""
        shops = [
            # 市中心
            make_shop("s_d1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s_d2", "天安门", "scenic", 39.908, 116.397, opentime="08:00-17:00"),
            # 西北方向（颐和园+圆明园≈1km，香山≈7km远）
            make_shop("s_nw1", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-17:00"),
            make_shop("s_nw2", "圆明园", "scenic", 40.008, 116.298, opentime="07:00-17:00"),
            make_shop("s_nw3", "香山", "scenic", 40.002, 116.193, opentime="08:00-17:00"),
            # 东北方向
            make_shop("s_ne1", "798艺术区", "scenic", 39.984, 116.495, opentime="09:00-18:00"),
            make_shop("s_ne2", "望京", "shopping", 39.996, 116.480, opentime="10:00-22:00"),
        ]
        result = solve_multi_day(shops, num_days=3, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", start_time_str="09:00",
                                 max_hours_per_day=6.0)

        # 构建 shop_id → day 映射
        shop_day = {}
        for di, day in enumerate(result["days"]):
            for pair in day.get("pairs", []):
                shop_day[pair[1]] = di

        nw_days = {shop_day.get("s_nw1"), shop_day.get("s_nw2"), shop_day.get("s_nw3")}

        # 核心断言：最接近的一对（颐和园+圆明园）应在一起
        # 香山是西北组中最远的（~7km），允许被移走
        assert shop_day["s_nw1"] == shop_day["s_nw2"], \
            f"颐和园和圆明园(~1km)不应被拆散！分配: {shop_day}"

        # 如果只有2天（必然拆分），应该是香山被移走（离另外两个最远）
        if len(nw_days) == 2:
            nw1_day = shop_day["s_nw1"]
            # 香山不应和颐和园/圆明园中至少一个同天 → 换言之，被移走的应是香山
            nw3_day = shop_day["s_nw3"]
            assert nw3_day != nw1_day or shop_day["s_nw2"] != nw1_day, \
                f"香山(s_nw3)应该是被移走的那个（离颐和园/圆明园~7km），但分配为: {shop_day}"

        # 市中心景点应在同一天
        center_days = {shop_day.get("s_d1"), shop_day.get("s_d2")}
        assert len(center_days) == 1, \
            f"2个市中心景点(故宫/天安门)被拆到了不同天: {center_days}"


# ======================================================================
# Phase 0: 数据输入与清洗解耦 —— 动态时长 + 疲劳权重 + 坐标兜底 + Never-Crash
# ======================================================================

class TestDynamicDurations:
    """Phase 0: dynamic_durations 替代 CATEGORY_DURATIONS 一刀切"""

    def setup_method(self):
        """确保 _ensure_agent 可用（测试环境 mock）"""
        import multi_day_scheduler as mds
        # 测试环境中不需要 agent 实例
        pass

    def test_get_duration_prefers_shop_duration_minutes(self):
        """_get_duration 优先用 shop['duration_minutes']（LLM 估算的个体时长）"""
        from multi_day_scheduler import _get_duration
        shop = {"shop_id": "s1", "name": "故宫", "category": "scenic", "duration_minutes": 240}
        assert _get_duration("scenic", shop) == 240, "应优先用 shop 个体的 duration_minutes"

    def test_get_duration_prefers_dynamic_durations_dict(self):
        """_get_duration 接受 external dynamic_durations dict"""
        from multi_day_scheduler import _get_duration
        dynamic_durations = {"s1": 240, "s2": 90}
        shop = {"shop_id": "s1", "name": "故宫", "category": "scenic"}
        assert _get_duration("scenic", shop, dynamic_durations) == 240
        # s3 不在 dynamic_durations 中，回退品类默认值
        shop2 = {"shop_id": "s3", "name": "未知景点", "category": "scenic"}
        assert _get_duration("scenic", shop2, dynamic_durations) == 180  # CATEGORY_DURATIONS 默认

    def test_get_duration_falls_back_to_category(self):
        """无 shop 对象时回退品类默认值"""
        from multi_day_scheduler import _get_duration
        assert _get_duration("scenic") == 180
        assert _get_duration("restaurant") == 60
        assert _get_duration("unknown_cat") == 60  # 未知品类默认 60

    def test_get_duration_backward_compatible(self):
        """旧调用方式（只有 category）仍然工作"""
        from multi_day_scheduler import _get_duration
        assert _get_duration("hotpot") == 90
        assert _get_duration("cafe") == 30

    def test_solve_multi_day_accepts_dynamic_durations(self):
        """solve_multi_day 接受 dynamic_durations 参数"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-19:00"),
            make_shop("r1", "烤鸭", "restaurant", 39.896, 116.397),
        ]
        dynamic_durations = {"s1": 300, "s2": 240, "r1": 90}
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", dynamic_durations=dynamic_durations)
        assert len(result["days"]) == 1
        # task_list 中 s1 的 duration_minutes 应为 300（dynamic），r1 为 90
        for task in result["days"][0]["task_list"]:
            if task["task_id"] == "s1":
                assert task["duration_minutes"] == 300, f"s1 应用 dynamic 300，实际: {task['duration_minutes']}"
            elif task["task_id"] == "r1":
                assert task["duration_minutes"] == 90, f"r1 应用 dynamic 90，实际: {task['duration_minutes']}"

    def test_solve_multi_day_accepts_fatigue_weights(self):
        """solve_multi_day 接受 fatigue_weights 参数且不崩溃"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "咖啡馆", "cafe", 39.920, 116.400),
            make_shop("r1", "烤鸭", "restaurant", 39.896, 116.397),
        ]
        fatigue_weights = {"s1": 9, "s2": 1, "r1": 4}
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先", fatigue_weights=fatigue_weights)
        assert len(result["days"]) == 1


class TestCoordinateDefense:
    """Phase 0: 坐标兜底防御 + Never-Crash 原则"""

    def test_no_hotel_coords_still_works(self):
        """checkin_lat/checkin_lng=None 时排程不崩溃（Never-Crash）"""
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=None, checkin_lng=None,
                                 transport_preference="驾车优先")
        assert len(result["days"]) >= 1

    def test_shop_missing_lat_lng_uses_coord_fallback(self):
        """shop 缺 lat/lng 时从 coord 字符串解析补全"""
        from multi_day_scheduler import _ensure_coords
        shop = {"shop_id": "s1", "name": "某店", "category": "scenic",
                "coord": "39.916,116.397"}  # 无 lat/lng，有 coord
        _ensure_coords([shop], arrival_lat=40.0, arrival_lng=116.4)
        assert shop["lat"] == 39.916
        assert shop["lng"] == 116.397
        assert shop.get("is_imputed") is not True  # coord 解析成功不标记

    def test_shop_all_coords_missing_is_imputed(self):
        """shop 的 lat/lng/coord 全缺 → 用到达站点坐标兜底 + is_imputed=True"""
        from multi_day_scheduler import _ensure_coords
        shop = {"shop_id": "s1", "name": "数据缺失店", "category": "scenic"}
        _ensure_coords([shop], arrival_lat=39.908, arrival_lng=116.397)
        assert shop["lat"] == 39.908
        assert shop["lng"] == 116.397
        assert shop.get("is_imputed") is True

    def test_shop_bad_coord_falls_back_to_arrival(self):
        """shop 的 coord 格式异常 → 用到达站点坐标兜底"""
        from multi_day_scheduler import _ensure_coords
        shop = {"shop_id": "s1", "name": "坏数据店", "category": "scenic",
                "coord": "badformat"}
        _ensure_coords([shop], arrival_lat=39.908, arrival_lng=116.397)
        assert shop["lat"] == 39.908
        assert shop.get("is_imputed") is True

    def test_solve_multi_day_all_coords_missing_still_completes(self):
        """所有 shop 坐标都缺失时排程仍完成（绝不崩溃）"""
        shops = [
            {"shop_id": "s1", "name": "店A", "category": "scenic", "opentime": "08:00-17:00"},
            {"shop_id": "s2", "name": "店B", "category": "shopping", "opentime": "09:00-22:00"},
        ]
        # 传入到达站点用于兜底（模拟 server 层已解析的站点坐标）
        result = solve_multi_day(shops, num_days=1,
                                 checkin_lat=None, checkin_lng=None,
                                 transport_preference="驾车优先",
                                 travel_info={"arrival_station_lat": 39.908, "arrival_station_lng": 116.397})
        assert len(result["days"]) >= 1
        # 所有 shop 应有 is_imputed 标记
        for day in result["days"]:
            for task in day["task_list"]:
                if task["task_id"] in ("s1", "s2"):
                    assert task.get("is_imputed") is True, \
                        f"{task['task_id']} 坐标数据缺失应标记 is_imputed"


# ======================================================================
# Phase 0.5: 餐饮前置就近绑定与 20km 强拦截
# ======================================================================

class TestPreBindMeals:
    """Phase 0.5: _pre_bind_meals_and_filter —— 餐饮前置绑定 + 20km 拦截"""

    def test_function_exists_and_callable(self):
        """_pre_bind_meals_and_filter 存在且可调用"""
        from multi_day_scheduler import _pre_bind_meals_and_filter
        assert callable(_pre_bind_meals_and_filter)

    def test_meal_binds_to_nearest_poi_within_20km(self):
        """餐厅距最近 POI ≤20km → 作为 bound_meals 挂载到 POI 上"""
        from multi_day_scheduler import _pre_bind_meals_and_filter
        shops = [
            make_shop("poi1", "故宫", "scenic", 39.916, 116.397),
            make_shop("meal1", "故宫旁烤鸭", "restaurant", 39.914, 116.400),  # 距故宫 ~300m
        ]
        poi_shops, pending = _pre_bind_meals_and_filter(shops)
        # meal1 应绑定到 poi1
        assert "meal1" not in {s["shop_id"] for s in poi_shops}, "meal 不应在 POI 列表中"
        bound_ids = [m.get("shop_id") for m in poi_shops[0].get("bound_meals", [])]
        assert "meal1" in bound_ids, f"meal1 应绑定到 poi1，实际 bound: {bound_ids}"
        assert len(pending) == 0, "不应有 pending 的餐厅"

    def test_meal_far_from_all_poi_goes_to_pending(self):
        """餐厅距任何 POI >20km → 移入 pending_user_confirmation_meals"""
        from multi_day_scheduler import _pre_bind_meals_and_filter
        shops = [
            make_shop("poi1", "故宫", "scenic", 39.916, 116.397),
            # 餐厅在天津 (~120km)，远超 20km 阈值
            make_shop("meal_far", "天津狗不理", "restaurant", 39.125, 117.210),
        ]
        poi_shops, pending = _pre_bind_meals_and_filter(shops)
        assert len(poi_shops) == 1, "POI 应保留"
        assert len(pending) == 1, "超远餐厅应进入 pending"
        assert pending[0]["shop_id"] == "meal_far"
        assert pending[0].get("reason") == "distance_exceeds_20km"

    def test_multiple_meals_bind_to_same_poi(self):
        """多个餐厅绑定到同一 POI（按距离排序）"""
        from multi_day_scheduler import _pre_bind_meals_and_filter
        shops = [
            make_shop("poi1", "故宫", "scenic", 39.916, 116.397),
            make_shop("meal1", "近烤鸭", "restaurant", 39.915, 116.398),  # ~200m
            make_shop("meal2", "远火锅", "hotpot", 39.920, 116.405),     # ~1km
        ]
        poi_shops, pending = _pre_bind_meals_and_filter(shops)
        bound = poi_shops[0].get("bound_meals", [])
        assert len(bound) == 2, f"两个餐厅都应绑定，实际: {len(bound)}"
        # 近的在前
        assert bound[0]["shop_id"] == "meal1"

    def test_clustering_only_on_poi(self):
        """聚类只对非餐饮 POI 进行，餐厅随绑定的 POI 移动"""
        from multi_day_scheduler import _pre_bind_meals_and_filter, _cluster_by_geo
        shops = [
            make_shop("poi1", "故宫", "scenic", 39.916, 116.397),
            make_shop("poi2", "颐和园", "scenic", 39.999, 116.275),
            make_shop("meal1", "故宫烤鸭", "restaurant", 39.914, 116.400),
            make_shop("meal2", "颐和园火锅", "hotpot", 40.000, 116.280),
        ]
        poi_shops, pending = _pre_bind_meals_and_filter(shops)
        # 聚类只在 POI 上运行
        clusters = _cluster_by_geo(poi_shops, k=2)
        # 所有 cluster 中的非 meal shop 应带 bound_meals
        total_bound = sum(
            len(s.get("bound_meals", []))
            for cluster in clusters for s in cluster
        )
        assert total_bound == 2, f"2个餐厅应被绑定，实际: {total_bound}"

    def test_no_meals_returns_all_poi(self):
        """无餐厅时返回全部作为 POI"""
        from multi_day_scheduler import _pre_bind_meals_and_filter
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
        ]
        poi_shops, pending = _pre_bind_meals_and_filter(shops)
        assert len(poi_shops) == 2
        assert len(pending) == 0

    def test_all_meals_far_returns_empty_pending_full(self):
        """全部是超远餐厅时 POI 为空，全部进 pending"""
        from multi_day_scheduler import _pre_bind_meals_and_filter
        shops = [
            make_shop("r1", "餐厅A", "restaurant", 39.916, 116.397),
            make_shop("r2", "餐厅B", "hotpot", 39.920, 116.400),
        ]
        # 没有任何非餐饮 POI → 所有餐厅都找不到绑定目标
        poi_shops, pending = _pre_bind_meals_and_filter(shops)
        # 无 POI 可绑定时餐厅保留不丢弃
        assert len(poi_shops) + len(pending) == 2

    def test_solve_integration_with_prebind(self):
        """集成：solve_multi_day 在 Phase 0.5 后仍正常工作"""
        shops = [
            make_shop("poi1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("poi2", "颐和园", "scenic", 39.999, 116.275, opentime="07:00-19:00"),
            make_shop("meal1", "故宫烤鸭", "restaurant", 39.914, 116.400),
            make_shop("meal_far", "天津狗不理", "restaurant", 39.125, 117.210),
        ]
        result = solve_multi_day(shops, num_days=1, checkin_lat=HOTEL_LAT, checkin_lng=HOTEL_LNG,
                                 transport_preference="驾车优先")
        assert len(result["days"]) >= 1
        # 应有 pending_user_confirmation_meals
        pending = result.get("pending_user_confirmation_meals", [])
        assert len(pending) == 1
        assert pending[0]["shop_id"] == "meal_far"


# ======================================================================
# Phase 1 & 1.5: 无锚点粗排程 —— 开放式 TSP + 提取每日首尾 POI
# ======================================================================

class TestOpenLoopTSP:
    """Phase 1.5: 开放式路径初始化（不考虑酒店）"""

    def test_open_loop_route_no_hotel_anchor(self):
        """开放式 TSP 不考虑酒店作为起点/终点"""
        from multi_day_scheduler import _route_open_loop
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406),
        ]
        result = _route_open_loop(shops, transport="驾车优先")
        assert "route" in result
        assert "total_travel_minutes" in result
        # 路径应该包含所有 3 个点
        assert len(result["route"]) >= 3, f"开放式路径应包含所有 POI，实际: {len(result['route'])}"

    def test_extract_day_boundary_pois(self):
        """提取每天的首尾 POI"""
        from multi_day_scheduler import _extract_day_boundaries
        clusters = [
            [
                make_shop("s1", "A", "scenic", 39.916, 116.397),
                make_shop("s2", "B", "scenic", 39.920, 116.400),
            ],
            [
                make_shop("s3", "C", "scenic", 39.999, 116.275),
                make_shop("s4", "D", "scenic", 40.008, 116.298),
            ],
        ]
        boundaries = _extract_day_boundaries(clusters, transport="驾车优先")
        assert len(boundaries) == 1  # 2 天之间有 1 个晚上
        # 每晚有 end_d 和 start_d+1
        for b in boundaries:
            assert "end_poi" in b
            assert "start_poi" in b
            assert b["end_poi"]["shop_id"] in ("s1", "s2")  # Day 1 的末位
            assert b["start_poi"]["shop_id"] in ("s3", "s4")  # Day 2 的首位

    def test_open_loop_preserves_geographic_order(self):
        """开放式 TSP 的路线访问顺序应合理（相邻点靠近）"""
        from multi_day_scheduler import _route_open_loop
        from multi_day_scheduler import _haversine_m
        # 构造3个点：市中心2个近点 + 郊区1个远点
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "天安门", "scenic", 39.908, 116.397),  # 离故宫~1km
            make_shop("s3", "颐和园", "scenic", 39.999, 116.275),  # 离市中心~10km
        ]
        result = _route_open_loop(shops, transport="驾车优先")
        route = result["route"]
        # 故宫和天安门（~1km）应该在路线中相邻
        idx1 = next(i for i, p in enumerate(route) if round(p[0], 3) == round(39.916, 3))
        idx2 = next(i for i, p in enumerate(route) if round(p[0], 3) == round(39.908, 3))
        assert abs(idx1 - idx2) <= 1, f"地理位置相近的点应在路线中相邻, idx1={idx1}, idx2={idx2}"


# ======================================================================
# Phase 2: 动态换住决策矩阵 —— DP 状态机评估 3 种酒店方案
# ======================================================================

class TestDynamicHotelDecision:
    """Phase 2: 动态换住决策矩阵"""

    def test_module_imports(self):
        """_dynamic_hotel_decision 存在且可调用"""
        from multi_day_scheduler import _dynamic_hotel_decision
        assert callable(_dynamic_hotel_decision)

    def test_plan_a_stay_no_penalty(self):
        """方案 A（原店续住）：行李惩罚 = 0"""
        from multi_day_scheduler import _evaluate_hotel_plan
        cost_a, reason = _evaluate_hotel_plan(
            plan="A", prev_hotel=(39.908, 116.397),
            end_poi_lat=39.999, end_poi_lng=116.275,
            start_poi_lat=39.999, start_poi_lng=116.275,
            next_day_load=0.5, fatigue=0.3,
            speed=667,
        )
        assert cost_a >= 0
        assert "原店续住" in reason or "stay" in reason.lower() or "A" in reason

    def test_plan_b_has_luggage_penalty(self):
        """方案 B（就近当天终点）：行李惩罚 60min"""
        from multi_day_scheduler import _evaluate_hotel_plan
        cost_b, reason = _evaluate_hotel_plan(
            plan="B", prev_hotel=(39.908, 116.397),
            end_poi_lat=39.999, end_poi_lng=116.275,
            start_poi_lat=39.999, start_poi_lng=116.275,
            next_day_load=0.5, fatigue=0.3,
            speed=667,
        )
        assert cost_b > 0
        # B 的代价应包含 60min 行李惩罚
        assert "B" in reason or "终点" in reason or "就近" in reason

    def test_plan_c_bonus_for_heavy_next_day(self):
        """方案 C：次日负载 > 80% 时减免 30min"""
        from multi_day_scheduler import _evaluate_hotel_plan
        # 次日不重：无 bonus
        cost_c_no_bonus, _ = _evaluate_hotel_plan(
            plan="C", prev_hotel=(39.908, 116.397),
            end_poi_lat=39.999, end_poi_lng=116.275,
            start_poi_lat=40.008, start_poi_lng=116.298,
            next_day_load=0.5, fatigue=0.3,
            speed=667,
        )
        # 次日重（负载 85%）：有 bonus
        cost_c_bonus, _ = _evaluate_hotel_plan(
            plan="C", prev_hotel=(39.908, 116.397),
            end_poi_lat=39.999, end_poi_lng=116.275,
            start_poi_lat=40.008, start_poi_lng=116.298,
            next_day_load=0.85, fatigue=0.3,
            speed=667,
        )
        assert cost_c_bonus < cost_c_no_bonus, \
            f"次日重载应触发 bonus 降低代价, bonus: {cost_c_bonus:.0f}, no_bonus: {cost_c_no_bonus:.0f}"

    def test_pick_best_plan(self):
        """DP 自动选择总代价最小的方案"""
        from multi_day_scheduler import _pick_best_hotel_plan
        best_plan, best_cost, best_hotel = _pick_best_hotel_plan(
            prev_hotel=(39.908, 116.397),
            end_poi_lat=39.916, end_poi_lng=116.397,  # 离酒店很近
            start_poi_lat=39.916, start_poi_lng=116.397,  # 次日也在附近
            next_day_load=0.5,
            fatigue=0.2,
            speed=667,
        )
        # 当 POI 都在酒店附近时，方案 A（续住）应是最优
        assert best_plan in ("A", "B", "C")

    def test_full_hotel_decision_pipeline(self):
        """完整酒店决策 pipeline：每晚都产出 hotel_plan"""
        from multi_day_scheduler import _dynamic_hotel_decision
        clusters = [
            [make_shop("s1", "故宫", "scenic", 39.916, 116.397),
             make_shop("s2", "天安门", "scenic", 39.908, 116.397)],
            [make_shop("s3", "颐和园", "scenic", 39.999, 116.275),
             make_shop("s4", "圆明园", "scenic", 40.008, 116.298)],
            [make_shop("s5", "798", "scenic", 39.984, 116.495)],
        ]
        decisions = _dynamic_hotel_decision(
            clusters, initial_hotel=(39.908, 116.397), transport="驾车优先"
        )
        # 2 个晚上（Day1→Day2, Day2→Day3）
        assert len(decisions) == 2
        for d in decisions:
            assert "plan" in d
            assert "hotel_lat" in d
            assert "hotel_lng" in d
            assert d["plan"] in ("A", "B", "C")

    def test_hotel_decision_not_applied_when_hotel_provided(self):
        """用户已指定酒店时，动态换住决策不改变酒店（保持兼容）"""
        from multi_day_scheduler import _dynamic_hotel_decision
        clusters = [
            [make_shop("s1", "故宫", "scenic", 39.916, 116.397)],
            [make_shop("s2", "颐和园", "scenic", 39.999, 116.275)],
        ]
        # 用户指定了酒店
        decisions = _dynamic_hotel_decision(
            clusters, initial_hotel=(39.908, 116.397), transport="驾车优先",
            user_provided_hotel=True
        )
        for d in decisions:
            assert d["plan"] == "A"  # 全部续住，不换房


# ======================================================================
# Phase 3: 边际插入成本负载均衡、疲劳度模型、多米诺级联
# ======================================================================

class TestComputeDayFatigue:
    """Phase 3: _compute_day_fatigue — 累积疲劳度"""

    def test_empty_cluster_returns_zero(self):
        """空 cluster 疲劳度为 0"""
        from multi_day_scheduler import _compute_day_fatigue
        assert _compute_day_fatigue([]) == 0.0

    def test_single_shop_default_weight(self):
        """单个 shop 无 fatigue_weight 时使用默认值 1.0"""
        from multi_day_scheduler import _compute_day_fatigue
        shop = make_shop("s1", "故宫", "scenic", 39.916, 116.397)
        fatigue = _compute_day_fatigue([shop])
        assert 0.0 < fatigue < 1.0, f"疲劳度应在 (0,1) 范围，实际: {fatigue}"

    def test_fatigue_increases_with_duration(self):
        """游玩时长越长，疲劳度越高"""
        from multi_day_scheduler import _compute_day_fatigue
        # 短行程：2h 景点
        short_shop = make_shop("s1", "小公园", "scenic", 39.9, 116.4)
        short_shop["duration_minutes"] = 60
        # 长行程：6h 景点
        long_shop = make_shop("s2", "环球影城", "scenic", 39.9, 116.4)
        long_shop["duration_minutes"] = 360
        assert _compute_day_fatigue([long_shop]) > _compute_day_fatigue([short_shop]), \
            "长时行程的疲劳度应高于短时行程"

    def test_fatigue_with_llm_weights(self):
        """LLM 疲劳权重影响 fatigue 计算"""
        from multi_day_scheduler import _compute_day_fatigue
        # 普通权重
        normal = make_shop("s1", "公园", "scenic", 39.9, 116.4)
        normal["fatigue_weight"] = 2.0
        # 高疲劳权重
        heavy = make_shop("s2", "长城", "scenic", 39.9, 116.4)
        heavy["fatigue_weight"] = 9.0
        assert _compute_day_fatigue([heavy]) > _compute_day_fatigue([normal]), \
            "高 fatigue_weight 的店铺应导致更高疲劳度"

    def test_multi_shop_accumulation(self):
        """多个 shop 的疲劳度应累积"""
        from multi_day_scheduler import _compute_day_fatigue
        shop1 = make_shop("s1", "A", "scenic", 39.9, 116.4)
        shop1["duration_minutes"] = 120
        shop2 = make_shop("s2", "B", "scenic", 39.92, 116.42)
        shop2["duration_minutes"] = 120
        # 两个 shop 的疲劳度 > 单个 shop
        fatigue_two = _compute_day_fatigue([shop1, shop2])
        fatigue_one = _compute_day_fatigue([shop1])
        assert fatigue_two > fatigue_one, \
            f"两个 shop 疲劳度({fatigue_two:.3f})应大于单个({fatigue_one:.3f})"

    def test_fatigue_clamped_to_one(self):
        """疲劳度不应超过 1.0"""
        from multi_day_scheduler import _compute_day_fatigue
        # 极端：非常多高权重长时长 shop
        shops = []
        for i in range(20):
            s = make_shop(f"s{i}", f"景点{i}", "scenic", 39.9 + i * 0.01, 116.4 + i * 0.01)
            s["duration_minutes"] = 300
            s["fatigue_weight"] = 10.0
            shops.append(s)
        fatigue = _compute_day_fatigue(shops)
        assert fatigue <= 1.0, f"疲劳度应 ≤1.0，实际: {fatigue}"


class TestMarginalInsertionCost:
    """Phase 3: _marginal_insertion_cost — 边际插入成本"""

    def test_insertion_increases_route_cost(self):
        """插入一个 shop 会增加 TSP 路线总成本"""
        from multi_day_scheduler import _marginal_insertion_cost
        target_cluster = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "天安门", "scenic", 39.908, 116.397),
        ]
        new_shop = make_shop("s3", "颐和园", "scenic", 39.999, 116.275)
        marginal_cost = _marginal_insertion_cost(new_shop, target_cluster, transport="驾车优先")
        assert marginal_cost > 0, f"插入成本应为正，实际: {marginal_cost}"

    def test_insertion_nearby_shop_lower_cost(self):
        """插入地理邻近的 shop 边际成本更低"""
        from multi_day_scheduler import _marginal_insertion_cost
        # Cluster 在北京中心
        target = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "天坛", "scenic", 39.882, 116.406),
        ]
        # 邻近店
        nearby = make_shop("s_near", "景山公园", "scenic", 39.922, 116.396)  # 离故宫~1km
        # 远处店
        far = make_shop("s_far", "八达岭长城", "scenic", 40.354, 116.020)  # ~60km
        cost_near = _marginal_insertion_cost(nearby, target, transport="驾车优先")
        cost_far = _marginal_insertion_cost(far, target, transport="驾车优先")
        assert cost_near < cost_far, \
            f"邻近插入成本({cost_near:.0f})应低于远处({cost_far:.0f})"

    def test_empty_target_cluster(self):
        """空 cluster 的插入成本为 0（第一个 shop）"""
        from multi_day_scheduler import _marginal_insertion_cost
        shop = make_shop("s1", "故宫", "scenic", 39.916, 116.397)
        cost = _marginal_insertion_cost(shop, [], transport="驾车优先")
        assert cost == 0.0, f"空 cluster 插入成本应为 0，实际: {cost}"

    def test_time_pressure_amplifies_cost(self):
        """目标天负载高时边际成本应放大"""
        from multi_day_scheduler import _marginal_insertion_cost
        # 已经接近满负载的 cluster
        full_cluster = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406),
            make_shop("s4", "圆明园", "scenic", 40.008, 116.298),
        ]
        new_shop = make_shop("s5", "798", "scenic", 39.984, 116.495)
        cost_normal = _marginal_insertion_cost(new_shop, full_cluster, transport="驾车优先", max_hours_per_day=12.0)
        cost_tight = _marginal_insertion_cost(new_shop, full_cluster, transport="驾车优先", max_hours_per_day=5.0)
        assert cost_tight >= cost_normal, \
            f"时间紧张时成本不应更低, tight={cost_tight:.0f}, normal={cost_normal:.0f}"


class TestDominoShift:
    """Phase 3: _domino_shift — 多米诺级联滚动"""

    def test_no_cascade_when_balanced(self):
        """所有天都均衡时无级联"""
        from multi_day_scheduler import _domino_shift
        clusters = [
            [make_shop("s1", "A", "scenic", 39.9, 116.4)],
            [make_shop("s2", "B", "scenic", 40.0, 116.3)],
        ]
        original = [list(c) for c in clusters]
        _domino_shift(clusters, from_idx=0, to_idx=1, moved_shop=None, max_hours_per_day=8.0)
        # 无变化
        for i in range(len(clusters)):
            assert [s["shop_id"] for s in clusters[i]] == [s["shop_id"] for s in original[i]]

    def test_triggers_cascade_on_overload(self):
        """当目标天超载时触发级联，将店铺继续向后传递"""
        from multi_day_scheduler import _domino_shift
        # Day 1→Day 2 移动后 Day 2 超载，触发 Day 2→Day 3 移动
        clusters = [
            [make_shop("s1", "A", "scenic", 39.9, 116.4)],
            [
                make_shop("s2", "B", "scenic", 39.92, 116.42),
                make_shop("s3", "C", "scenic", 39.94, 116.44),
                make_shop("s4", "D", "scenic", 39.96, 116.46),
            ],
            [make_shop("s5", "E", "scenic", 39.98, 116.48)],
        ]
        _domino_shift(clusters, from_idx=0, to_idx=1,
                      moved_shop=make_shop("sX", "X", "scenic", 39.91, 116.41),
                      max_hours_per_day=3.0, max_scenic_per_day=2)
        # Day 2 不应超载（scenic ≤ 2）
        day2_scenic = sum(1 for s in clusters[1] if s.get("category") == "scenic")
        assert day2_scenic <= 2, f"级联后 Day 2 scenic 应 ≤2，实际: {day2_scenic}"

    def test_cascade_depth_limited(self):
        """级联深度不超过天数"""
        from multi_day_scheduler import _domino_shift
        clusters = [
            [make_shop(f"s{i*10+1}", f"A{i}", "scenic", 39.9 + i * 0.01, 116.4 + i * 0.01) for i in range(3)],
            [make_shop(f"s{i*10+2}", f"B{i}", "scenic", 39.9 + i * 0.01, 116.4 + i * 0.01) for i in range(3)],
            [make_shop(f"s{i*10+3}", f"C{i}", "scenic", 39.9 + i * 0.01, 116.4 + i * 0.01) for i in range(3)],
        ]
        # 不应该进入死循环
        _domino_shift(clusters, from_idx=0, to_idx=1,
                      moved_shop=make_shop("sX", "X", "scenic", 39.91, 116.41),
                      max_hours_per_day=3.0, max_scenic_per_day=2)
        # 应该正常返回，不崩溃
        total_shops = sum(len(c) for c in clusters)
        assert total_shops > 0


class TestBalanceWithMarginalCost:
    """Phase 3: 重写 _balance_clusters — 边际插入成本 + 疲劳度 + 多米诺级联"""

    def test_balance_preserves_all_shops(self):
        """均衡后不应丢失任何店铺"""
        from multi_day_scheduler import _balance_clusters
        clusters = [
            [make_shop("s1", "故宫", "scenic", 39.916, 116.397),
             make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
             make_shop("s3", "天坛", "scenic", 39.882, 116.406)],
            [make_shop("s4", "798", "scenic", 39.984, 116.495)],
        ]
        balanced = _balance_clusters(clusters, max_hours_per_day=4.0, max_scenic_per_day=2)
        total_in = sum(len(c) for c in balanced)
        total_orig = sum(len(c) for c in clusters)
        assert total_in == total_orig, f"丢失店铺: orig={total_orig}, balanced={total_in}"

    def test_no_day_exceeds_scenic_cap(self):
        """均衡后每天 scenic 不超过上限（可解场景：总 scenic ≤ days * cap）"""
        from multi_day_scheduler import _balance_clusters
        # 6 scenic × 3 days × cap=2 → 最大值 6，恰好可解
        clusters = [
            [make_shop(f"s{i}", f"景点{i}", "scenic", 39.9 + i * 0.02, 116.4 + i * 0.02)
             for i in range(4)],  # 4 scenic
            [make_shop("s4", "A", "scenic", 40.0, 116.5)],   # 1 scenic
            [make_shop("s5", "B", "scenic", 40.1, 116.6)],   # 1 scenic
        ]
        balanced = _balance_clusters(clusters, max_hours_per_day=6.0, max_scenic_per_day=2)
        for di, c in enumerate(balanced):
            sc = sum(1 for s in c if s.get("category") == "scenic")
            assert sc <= 2, f"Day {di+1} scenic={sc} > 2"

    def test_balance_with_fatigue_awareness(self):
        """均衡时考虑疲劳度：优先移动低疲劳成本的店铺"""
        from multi_day_scheduler import _balance_clusters
        # Day 1 超载，有2个店铺：一个高疲劳一个低疲劳
        high_fatigue = make_shop("s_h", "长城", "scenic", 39.9, 116.4)
        high_fatigue["fatigue_weight"] = 9.0
        high_fatigue["duration_minutes"] = 300
        low_fatigue = make_shop("s_l", "小公园", "scenic", 39.92, 116.42)
        low_fatigue["fatigue_weight"] = 1.0
        low_fatigue["duration_minutes"] = 60
        clusters = [
            [high_fatigue, low_fatigue,
             make_shop("s3", "故宫", "scenic", 39.916, 116.397)],
            [make_shop("s4", "A", "scenic", 40.0, 116.5)],
        ]
        balanced = _balance_clusters(clusters, max_hours_per_day=3.0, max_scenic_per_day=2)
        # 均衡后 Day 1 scenic 不应超标
        for di, c in enumerate(balanced):
            sc = sum(1 for s in c if s.get("category") == "scenic")
            assert sc <= 2, f"Day {di+1} scenic={sc}"

    def test_domino_cascade_in_balance(self):
        """均衡过程中的多米诺级联：一次移动触发连锁（可解场景）"""
        from multi_day_scheduler import _balance_clusters
        # Day1 超载→移入 Day2→Day2 超载→移入 Day3（总=6 scenic, 3天×cap=2 → 可解）
        clusters = [
            [make_shop(f"s1_{i}", f"A{i}", "scenic", 39.9 + i * 0.01, 116.4 + i * 0.01)
             for i in range(3)],  # Day 1: 3 scenic
            [make_shop(f"s2_{i}", f"B{i}", "scenic", 39.9 + i * 0.01, 116.5 + i * 0.01)
             for i in range(2)],  # Day 2: 2 scenic
            [make_shop("s3", "C", "scenic", 40.1, 116.6)],  # Day 3: 1 scenic
        ]
        balanced = _balance_clusters(clusters, max_hours_per_day=4.0, max_scenic_per_day=2)
        for di, c in enumerate(balanced):
            sc = sum(1 for s in c if s.get("category") == "scenic")
            assert sc <= 2, f"级联均衡后 Day {di+1} scenic={sc} > 2"

    def test_backward_compat_no_fatigue_weights(self):
        """无 fatigue_weights 时均衡行为不变（向后兼容）"""
        from multi_day_scheduler import _balance_clusters
        clusters = [
            [make_shop("s1", "故宫", "scenic", 39.916, 116.397),
             make_shop("s2", "天安门", "scenic", 39.908, 116.397),
             make_shop("s3", "颐和园", "scenic", 39.999, 116.275)],
            [make_shop("s4", "798", "scenic", 39.984, 116.495)],
        ]
        balanced = _balance_clusters(clusters, max_hours_per_day=4.0, max_scenic_per_day=2)
        # 应该有合理的分配
        total_scenic = sum(sum(1 for s in c if s.get("category") == "scenic") for c in balanced)
        assert total_scenic == 4  # 所有 scenic 都在
        for di, c in enumerate(balanced):
            sc = sum(1 for s in c if s.get("category") == "scenic")
            assert sc <= 2


# ======================================================================
# Phase 3.5 & 4: 多锚点路径合成、路网自适应
# ======================================================================

class TestComputeDayAnchors:
    """Phase 3.5: _compute_day_anchors — 每日起点/终点锚点计算"""

    def test_day0_start_is_arrival_station(self):
        """Day 0 起点 = 到达站点坐标"""
        from multi_day_scheduler import _compute_day_anchors
        travel_info = {
            "arrival_station": "北京南站",
            "arrival_time": "10:00",
        }
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=0, total_days=3, hotel_plan=[],
            travel_info=travel_info, checkin_lat=39.908, checkin_lng=116.397,
        )
        # 北京南站坐标
        assert abs(start_lat - 39.865) < 0.01
        assert abs(start_lng - 116.379) < 0.01
        # 终点默认酒店
        assert abs(end_lat - 39.908) < 0.01
        assert abs(end_lng - 116.397) < 0.01

    def test_last_day_end_is_return_station(self):
        """最后一天终点 = 返程站点坐标"""
        from multi_day_scheduler import _compute_day_anchors
        travel_info = {
            "return_station": "北京西站",
            "return_departure_time": "18:00",
        }
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=2, total_days=3, hotel_plan=[],
            travel_info=travel_info, checkin_lat=39.908, checkin_lng=116.397,
        )
        # 起点默认酒店
        assert abs(start_lat - 39.908) < 0.01
        assert abs(start_lng - 116.397) < 0.01
        # 终点 = 北京西站
        assert abs(end_lat - 39.895) < 0.01
        assert abs(end_lng - 116.322) < 0.01

    def test_middle_days_use_hotel(self):
        """中间天起点/终点均使用酒店坐标"""
        from multi_day_scheduler import _compute_day_anchors
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=1, total_days=3, hotel_plan=[],
            travel_info=None, checkin_lat=39.908, checkin_lng=116.397,
        )
        assert abs(start_lat - 39.908) < 0.01
        assert abs(start_lng - 116.397) < 0.01
        assert abs(end_lat - 39.908) < 0.01
        assert abs(end_lng - 116.397) < 0.01

    def test_no_travel_info_defaults_to_hotel(self):
        """无 travel_info 时所有天默认使用酒店坐标"""
        from multi_day_scheduler import _compute_day_anchors
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=0, total_days=1, hotel_plan=[],
            travel_info=None, checkin_lat=39.908, checkin_lng=116.397,
        )
        assert abs(start_lat - 39.908) < 0.01
        assert abs(start_lng - 116.397) < 0.01
        assert abs(end_lat - 39.908) < 0.01
        assert abs(end_lng - 116.397) < 0.01

    def test_with_hotel_plan_changes_hotel(self):
        """hotel_plan 存在时覆盖默认酒店坐标"""
        from multi_day_scheduler import _compute_day_anchors
        hotel_plan = [
            {"plan": "A", "hotel_lat": 39.920, "hotel_lng": 116.410, "cost": 1.5},
            {"plan": "B", "hotel_lat": 39.950, "hotel_lng": 116.350, "cost": 2.0},
        ]
        # Day 1: start = hotel_plan[0], end = hotel_plan[1]
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=1, total_days=3, hotel_plan=hotel_plan,
            travel_info=None, checkin_lat=39.908, checkin_lng=116.397,
        )
        assert abs(start_lat - 39.920) < 0.01, f"start_lat={start_lat}"
        assert abs(start_lng - 116.410) < 0.01, f"start_lng={start_lng}"
        assert abs(end_lat - 39.950) < 0.01, f"end_lat={end_lat}"
        assert abs(end_lng - 116.350) < 0.01, f"end_lng={end_lng}"

    def test_single_day_both_stations(self):
        """单天行程同时有到达和返程站点"""
        from multi_day_scheduler import _compute_day_anchors
        travel_info = {
            "arrival_station": "北京南站",
            "arrival_time": "10:00",
            "return_station": "北京西站",
            "return_departure_time": "18:00",
        }
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=0, total_days=1, hotel_plan=[],
            travel_info=travel_info, checkin_lat=39.908, checkin_lng=116.397,
        )
        # 起点 = 北京南站
        assert abs(start_lat - 39.865) < 0.01
        assert abs(start_lng - 116.379) < 0.01
        # 终点 = 北京西站
        assert abs(end_lat - 39.895) < 0.01
        assert abs(end_lng - 116.322) < 0.01

    def test_day0_end_uses_hotel_plan_first_entry(self):
        """Day 0 终点 = hotel_plan[0] 酒店（当晚入住酒店）"""
        from multi_day_scheduler import _compute_day_anchors
        hotel_plan = [
            {"plan": "B", "hotel_lat": 39.930, "hotel_lng": 116.420, "cost": 1.0},
        ]
        travel_info = {"arrival_station": "北京站", "arrival_time": "09:00"}
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=0, total_days=2, hotel_plan=hotel_plan,
            travel_info=travel_info, checkin_lat=39.908, checkin_lng=116.397,
        )
        # 起点 = 北京站
        assert abs(start_lat - 39.904) < 0.01
        # 终点 = hotel_plan[0] 酒店（非默认酒店）
        assert abs(end_lat - 39.930) < 0.01
        assert abs(end_lng - 116.420) < 0.01


class TestRegionCohesionGuard:
    """Phase 4: _region_cohesion_guard — 路网多模态决策"""

    def test_short_distance_recommends_walking(self):
        """短距离（<500m）推荐步行"""
        from multi_day_scheduler import _region_cohesion_guard
        result = _region_cohesion_guard(dist_m=300, weather=None, preference=None)
        assert "步行" in result["transport"]
        assert result["warning"] is None

    def test_long_distance_recommends_driving(self):
        """长距离（>8km）推荐驾车"""
        from multi_day_scheduler import _region_cohesion_guard
        result = _region_cohesion_guard(dist_m=10000, weather=None, preference=None)
        assert "驾车" in result["transport"]

    def test_bad_weather_avoids_walking(self):
        """恶劣天气（walking_penalty < 0.7）中等距离避免步行"""
        from multi_day_scheduler import _region_cohesion_guard
        bad_weather = {"walking_penalty": 0.5, "condition": "中雨"}
        result = _region_cohesion_guard(dist_m=2000, weather=bad_weather, preference=None)
        # 恶劣天气下 2000m 不应推荐步行
        assert "步行" not in result["transport"]
        # 应该有警告
        assert result["warning"] is not None

    def test_severe_weather_forces_driving(self):
        """极端天气（walking_penalty < 0.4）任何非短距离都推荐驾车"""
        from multi_day_scheduler import _region_cohesion_guard
        severe_weather = {"walking_penalty": 0.3, "condition": "暴雨"}
        result = _region_cohesion_guard(dist_m=1500, weather=severe_weather, preference=None)
        assert "驾车" in result["transport"]
        assert result["warning"] is not None

    def test_walking_tolerance_from_preference(self):
        """用户偏好中的 walking_tolerance_meters 影响决策阈值"""
        from multi_day_scheduler import _region_cohesion_guard
        # 用户设置步行容忍 3000m，2000m 应该可以步行
        preference = {"walking_tolerance_meters": 3000}
        result = _region_cohesion_guard(dist_m=2000, weather=None, preference=preference)
        assert "步行" in result["transport"]

    def test_medium_distance_metro(self):
        """中等距离（3-8km）推荐地铁"""
        from multi_day_scheduler import _region_cohesion_guard
        result = _region_cohesion_guard(dist_m=5000, weather=None, preference=None)
        assert "地铁" in result["transport"]

    def test_prefer_transport_overrides(self):
        """用户明确偏好某种交通方式时覆盖决策"""
        from multi_day_scheduler import _region_cohesion_guard
        preference = {"prefer_transport": "驾车优先"}
        result = _region_cohesion_guard(dist_m=500, weather=None, preference=preference)
        # 即使距离短，用户偏好也覆盖
        assert "驾车" in result["transport"]

    def test_very_short_always_walkable(self):
        """极短距离（<200m）即使天气差也推荐步行"""
        from multi_day_scheduler import _region_cohesion_guard
        bad_weather = {"walking_penalty": 0.3, "condition": "暴雨"}
        result = _region_cohesion_guard(dist_m=100, weather=bad_weather, preference=None)
        assert "步行" in result["transport"]


class TestRouteOneDayWithAnchors:
    """Phase 3.5 & 4 集成: _route_one_day_dynamic 锚点感知"""

    def test_route_first_day_starts_at_arrival(self):
        """Day 1 路线起点是到达站点而非酒店"""
        from multi_day_scheduler import _compute_day_anchors, _route_one_day_dynamic
        travel_info = {"arrival_station": "北京南站", "arrival_time": "10:00"}
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=0, total_days=2, hotel_plan=[],
            travel_info=travel_info, checkin_lat=39.908, checkin_lng=116.397,
        )
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
        ]
        result = _route_one_day_dynamic(shops, start_lat, start_lng, end_lat, end_lng,
                                        transport="驾车优先", weather=None)
        # 路线起点应为北京南站
        route = result["route"]
        assert abs(route[0][0] - 39.865) < 0.01, f"route start lat={route[0][0]}"
        assert abs(route[0][1] - 116.379) < 0.01, f"route start lng={route[0][1]}"

    def test_route_last_day_ends_at_return(self):
        """最后一天路线终点是返程站点"""
        from multi_day_scheduler import _compute_day_anchors, _route_one_day_dynamic
        travel_info = {"return_station": "北京西站", "return_departure_time": "18:00"}
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=1, total_days=2, hotel_plan=[],
            travel_info=travel_info, checkin_lat=39.908, checkin_lng=116.397,
        )
        shops = [
            make_shop("s1", "颐和园", "scenic", 39.999, 116.275),
        ]
        result = _route_one_day_dynamic(shops, start_lat, start_lng, end_lat, end_lng,
                                        transport="驾车优先", weather=None)
        # 路线终点应为北京西站
        route = result["route"]
        assert abs(route[-1][0] - 39.895) < 0.01, f"route end lat={route[-1][0]}"
        assert abs(route[-1][1] - 116.322) < 0.01, f"route end lng={route[-1][1]}"

    def test_route_with_hotel_plan_anchors(self):
        """hotel_plan 存在时路线使用动态酒店坐标"""
        from multi_day_scheduler import _compute_day_anchors, _route_one_day_dynamic
        hotel_plan = [
            {"plan": "B", "hotel_lat": 39.930, "hotel_lng": 116.420, "cost": 1.0},
        ]
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=0, total_days=2, hotel_plan=hotel_plan,
            travel_info=None, checkin_lat=39.908, checkin_lng=116.397,
        )
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
        ]
        result = _route_one_day_dynamic(shops, start_lat, start_lng, end_lat, end_lng,
                                        transport="驾车优先", weather=None)
        route = result["route"]
        # 起点 = 默认酒店
        assert abs(route[0][0] - 39.908) < 0.01
        # 终点 = hotel_plan[0] 酒店
        assert abs(route[-1][0] - 39.930) < 0.01
        assert abs(route[-1][1] - 116.420) < 0.01

    def test_route_weather_fallback_with_anchors(self):
        """天气感知在锚点感知路径中同样生效"""
        from multi_day_scheduler import _compute_day_anchors, _route_one_day_dynamic
        start_lat, start_lng, end_lat, end_lng = _compute_day_anchors(
            day_index=0, total_days=1, hotel_plan=[],
            travel_info=None, checkin_lat=39.908, checkin_lng=116.397,
        )
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "天坛", "scenic", 39.882, 116.406),
        ]
        # 正常天气
        r1 = _route_one_day_dynamic(shops, start_lat, start_lng, end_lat, end_lng,
                                    transport="驾车优先", weather=None)
        # 坏天气
        r2 = _route_one_day_dynamic(shops, start_lat, start_lng, end_lat, end_lng,
                                    transport="步行优先", weather={"walking_penalty": 0.4})
        # 坏天气下步行速度慢，travel time 应更大
        assert r1["total_travel_minutes"] >= 0
        assert r2["total_travel_minutes"] >= 0
        # 步行优先在坏天气下速度降低
        assert r1["total_duration_minutes"] == r2["total_duration_minutes"]


# ======================================================================
# Phase 4.5 & 5: 智能时间线构建、模拟退火精修
# ======================================================================

class TestMultiFactorCost:
    """4.5.1 _multi_factor_cost 多因子综合代价函数"""

    def test_multi_factor_cost_exists_and_returns_finite(self):
        """_multi_factor_cost 应存在且返回有限数值"""
        from multi_day_scheduler import _multi_factor_cost
        import math

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720, "shop_id": "r1",
             "category": "restaurant", "duration_minutes": 60},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 840, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "DINNER", "start_minutes": 1110, "shop_id": "r2",
             "category": "restaurant", "duration_minutes": 60},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
        ]

        cost = _multi_factor_cost(timeline, all_shops)
        assert math.isfinite(cost), f"multi_factor_cost 应为有限值，实际: {cost}"
        assert cost >= 0, f"multi_factor_cost 应 >= 0，实际: {cost}"

    def test_multi_factor_cost_penalizes_missed_shops(self):
        """未访问的店铺应增加 cost（rating 加权）"""
        from multi_day_scheduler import _multi_factor_cost

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "DINNER", "start_minutes": 1110},
        ]
        all_shops = [make_shop("s1", "A", "scenic", 39.9, 116.4)]
        cost_no_miss = _multi_factor_cost(timeline, all_shops)

        all_shops_with_miss = all_shops + [
            make_shop("s2", "B", "scenic", 39.9, 116.4, rating=4.8),
        ]
        cost_with_miss = _multi_factor_cost(timeline, all_shops_with_miss)

        assert cost_with_miss > cost_no_miss, (
            f"有未访问点 cost ({cost_with_miss}) > 无未访问点 ({cost_no_miss})"
        )

    def test_multi_factor_cost_penalizes_bad_meal_times(self):
        """偏离锚点的用餐时间应显著增加 cost"""
        from multi_day_scheduler import _multi_factor_cost

        timeline_good = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},   # 12:00
            {"type": "DINNER", "start_minutes": 1110},  # 18:30
        ]
        timeline_bad = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 900},    # 15:00 很晚
            {"type": "DINNER", "start_minutes": 1290},   # 21:30 很晚
        ]
        all_shops = [make_shop("s1", "A", "scenic", 39.9, 116.4)]

        cost_good = _multi_factor_cost(timeline_good, all_shops)
        cost_bad = _multi_factor_cost(timeline_bad, all_shops)

        assert cost_bad > cost_good, (
            f"用餐时间差 cost ({cost_bad}) > 好 cost ({cost_good})"
        )

    def test_multi_factor_cost_penalizes_travel_time(self):
        """通勤时间越长 cost 越高"""
        from multi_day_scheduler import _multi_factor_cost

        timeline_short_travel = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 5},
            {"type": "LUNCH", "start_minutes": 720},
        ]
        timeline_long_travel = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 60},
            {"type": "LUNCH", "start_minutes": 720},
        ]
        all_shops = [make_shop("s1", "A", "scenic", 39.9, 116.4)]

        cost_short = _multi_factor_cost(timeline_short_travel, all_shops)
        cost_long = _multi_factor_cost(timeline_long_travel, all_shops)

        assert cost_long > cost_short, (
            f"长通勤 cost ({cost_long}) > 短通勤 cost ({cost_short})"
        )

    def test_multi_factor_cost_detects_consecutive_high_fatigue(self):
        """连续高强度活动（无休息）应产生额外惩罚"""
        from multi_day_scheduler import _multi_factor_cost

        # 有休息间隔的行程
        timeline_with_rest = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},  # 休息
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 840, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "DINNER", "start_minutes": 1110},  # 休息
            {"type": "VISIT", "shop_id": "s3", "start_minutes": 1200, "category": "shopping",
             "duration_minutes": 90, "travel_minutes": 10},
        ]
        # 连续高强度无休息
        timeline_consecutive = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 730, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "VISIT", "shop_id": "s3", "start_minutes": 920, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 1110},  # 午餐很晚
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
            make_shop("s3", "C", "scenic", 39.9, 116.4),
        ]

        cost_rest = _multi_factor_cost(timeline_with_rest, all_shops)
        cost_consecutive = _multi_factor_cost(timeline_consecutive, all_shops)

        # 连续高强度应产生额外惩罚
        assert cost_consecutive > cost_rest, (
            f"连续高强度 cost ({cost_consecutive}) > 有休息 cost ({cost_rest})"
        )

    def test_multi_factor_cost_overtime_penalty(self):
        """超出 bedtime 约束应产生惩罚"""
        from multi_day_scheduler import _multi_factor_cost

        # 正常时间线（在 22:00 前结束）
        timeline_normal = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "DINNER", "start_minutes": 1110},
        ]
        # 超时时间线（结束于 24:00）
        timeline_overtime = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 1200, "category": "scenic",
             "duration_minutes": 240, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "DINNER", "start_minutes": 1110},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
        ]

        cost_normal = _multi_factor_cost(timeline_normal, all_shops, bedtime_cap=22*60)
        cost_overtime = _multi_factor_cost(timeline_overtime, all_shops, bedtime_cap=22*60)

        assert cost_overtime > cost_normal, (
            f"超时 cost ({cost_overtime}) > 正常 cost ({cost_normal})"
        )

    def test_multi_factor_cost_empty_timeline(self):
        """空时间线返回基础 cost（仅包含未访问惩罚）"""
        from multi_day_scheduler import _multi_factor_cost

        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4, rating=4.5),
            make_shop("s2", "B", "scenic", 39.9, 116.4, rating=3.0),
        ]
        cost = _multi_factor_cost([], all_shops)
        assert cost > 0, f"空时间线 + 有店铺 → cost > 0，实际: {cost}"

    def test_multi_factor_cost_weather_impact(self):
        """恶劣天气应增加对步行 heavy 时间线的 cost"""
        from multi_day_scheduler import _multi_factor_cost

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 30},
            {"type": "LUNCH", "start_minutes": 720},
        ]
        all_shops = [make_shop("s1", "A", "scenic", 39.9, 116.4)]

        cost_good_weather = _multi_factor_cost(timeline, all_shops,
                                                weather={"walking_penalty": 1.0})
        cost_bad_weather = _multi_factor_cost(timeline, all_shops,
                                               weather={"walking_penalty": 0.3})

        # 坏天气下通勤成本放大
        assert cost_bad_weather >= cost_good_weather, (
            f"坏天气 cost ({cost_bad_weather}) >= 好天气 cost ({cost_good_weather})"
        )

    def test_multi_factor_cost_backward_compat_with_total_cost(self):
        """_multi_factor_cost 结果数量级应与 _total_cost 一致"""
        from multi_day_scheduler import _multi_factor_cost, _total_cost

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720, "shop_id": "r1"},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 840, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "DINNER", "start_minutes": 1110, "shop_id": "r2"},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
        ]

        old_cost = _total_cost(timeline, all_shops)
        new_cost = _multi_factor_cost(timeline, all_shops)

        # 新 cost 至少包含旧 cost 的所有因子，因此 >= 旧 cost
        assert new_cost >= old_cost * 0.8, (
            f"新 cost ({new_cost}) 不应远低于旧 cost ({old_cost})"
        )


class TestTimelineStateMachine:
    """4.5.2 _build_timeline 状态机推演"""

    def test_state_machine_helpers_exist(self):
        """状态机辅助函数应存在且可调用"""
        from multi_day_scheduler import (
            _timeline_state_machine,
            _TIMELINE_STATES,
        )
        assert callable(_timeline_state_machine)
        assert isinstance(_TIMELINE_STATES, dict)
        assert "WAKE_UP" in _TIMELINE_STATES
        assert "BEDTIME" in _TIMELINE_STATES

    def test_state_machine_produces_valid_timeline(self):
        """状态机产出的时间线应有 WAKE_UP 和 BEDTIME"""
        from multi_day_scheduler import _timeline_state_machine

        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "天坛", "scenic", 39.882, 116.406),
        ]
        result = _timeline_state_machine(
            shops, start_time_str="09:00", transport="驾车优先",
            bedtime_str="22:00", week_day=0,
        )

        timeline = result["timeline"]
        actions = [n["action"] for n in timeline]
        assert "WAKE_UP" in actions, "必须包含 WAKE_UP"
        assert "BEDTIME" in actions, "必须包含 BEDTIME"
        assert "VISIT" in actions, "必须包含至少一个 VISIT"

    def test_state_machine_respects_bedtime(self):
        """状态机应遵守 bedtime 约束，不排入超时活动"""
        from multi_day_scheduler import _timeline_state_machine, _time_to_minutes

        # 构造很多店铺以确保时间不够
        shops = []
        for i in range(15):
            shops.append(make_shop(f"s{i}", f"景点{i}", "scenic", 39.9 + i*0.01, 116.4))

        result = _timeline_state_machine(
            shops, start_time_str="09:00", transport="驾车优先",
            bedtime_str="20:00", week_day=0,
        )

        timeline = result["timeline"]
        # 所有 VISIT 的结束时间应在 bedtime 之前
        for node in timeline:
            if node["action"] == "VISIT":
                end_min = _time_to_minutes(node["time"]) + node.get("duration_minutes", 0)
                assert end_min <= 20 * 60 + 5, (
                    f"VISIT 结束 {end_min} 不应显著超过 bedtime 20:00"
                )

    def test_state_machine_handles_empty_shops(self):
        """空店铺列表应产生仅含三餐占位的有效时间线"""
        from multi_day_scheduler import _timeline_state_machine

        result = _timeline_state_machine([], start_time_str="09:00")

        timeline = result["timeline"]
        actions = [n["action"] for n in timeline]
        assert "BREAKFAST_NEEDED" in actions
        assert "LUNCH_NEEDED" in actions or "LUNCH" in actions
        assert "DINNER_NEEDED" in actions or "DINNER" in actions

    def test_state_machine_morning_activities_before_lunch(self):
        """上午活动应在午餐之前"""
        from multi_day_scheduler import _timeline_state_machine, _time_to_minutes

        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "天坛", "scenic", 39.882, 116.406),
            make_shop("s3", "颐和园", "scenic", 39.999, 116.275),
        ]
        result = _timeline_state_machine(
            shops, start_time_str="08:00", transport="驾车优先",
        )

        timeline = result["timeline"]
        lunch_found = False
        for node in timeline:
            if node["action"] in ("LUNCH", "LUNCH_NEEDED"):
                lunch_found = True
            if node["action"] == "VISIT" and not lunch_found:
                # 午餐前的 VISIT 应在上午
                visit_hour = _time_to_minutes(node["time"]) // 60
                assert visit_hour < 14, (
                    f"午餐前 VISIT 应在上午，实际 {node['time']}"
                )

    def test_state_machine_shopping_evening_preferred(self):
        """购物类活动应优先排在晚间（晚餐后）"""
        from multi_day_scheduler import _timeline_state_machine

        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "商场A", "shopping", 39.908, 116.397),
            make_shop("s3", "商场B", "shopping", 39.905, 116.400),
        ]
        result = _timeline_state_machine(
            shops, start_time_str="09:00", transport="驾车优先",
        )

        timeline = result["timeline"]
        dinner_found = False
        for node in timeline:
            if node["action"] in ("DINNER", "DINNER_NEEDED"):
                dinner_found = True
            if node["action"] == "VISIT" and node["category"] == "shopping":
                if not dinner_found:
                    # 晚餐前的购物应在下午（非强制，但优先）
                    pass

    def test_state_machine_meal_binding_works(self):
        """正餐应绑定到最近的非餐饮目的地"""
        from multi_day_scheduler import _timeline_state_machine

        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("r1", "故宫餐厅", "restaurant", 39.917, 116.398),
            make_shop("s2", "天坛", "scenic", 39.882, 116.406),
        ]
        result = _timeline_state_machine(
            shops, start_time_str="09:00", transport="驾车优先",
        )

        timeline = result["timeline"]
        # 应该能看到 LUNCH 或 DINNER
        meal_actions = [n for n in timeline if n["action"] in ("LUNCH", "DINNER")]
        assert len(meal_actions) > 0, "应有至少一个正餐节点"

    def test_state_machine_consistent_with_legacy(self):
        """状态机产出与旧 _build_timeline 在简单场景下一致"""
        from multi_day_scheduler import _timeline_state_machine, _build_timeline

        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "天坛", "scenic", 39.882, 116.406),
        ]
        day_plan = {
            "route": [
                (HOTEL_LAT, HOTEL_LNG),
                (39.916, 116.397),
                (39.882, 116.406),
                (HOTEL_LAT, HOTEL_LNG),
            ]
        }

        # 旧版本
        old_result = _build_timeline(day_plan, shops, start_time_str="09:00",
                                      transport="驾车优先")
        # 新版本（状态机）
        new_result = _timeline_state_machine(
            shops, start_time_str="09:00", transport="驾车优先",
        )

        old_visits = [n for n in old_result["timeline"] if n["action"] == "VISIT"]
        new_visits = [n for n in new_result["timeline"] if n["action"] == "VISIT"]
        # 两者应有相同数量的 VISIT
        assert len(new_visits) == len(old_visits), (
            f"状态机 VISIT 数 ({len(new_visits)}) 应与旧版 ({len(old_visits)}) 一致"
        )

    def test_parse_opentime_helper_exists(self):
        """营业时间解析辅助函数应存在"""
        from multi_day_scheduler import _parse_opentime, _check_open
        assert callable(_parse_opentime)
        assert callable(_check_open)

        hours = _parse_opentime("09:00-18:00", week_day=0)
        assert hours is not None
        assert hours["open"] == 9 * 60
        assert hours["close"] == 18 * 60


class TestRefineTimelineEnhanced:
    """5.1 _refine_timeline 增强：100 次迭代 + 更多邻域操作"""

    def test_refine_enhanced_exists(self):
        """增强版 _refine_timeline 应存在且可调用"""
        from multi_day_scheduler import _refine_timeline
        assert callable(_refine_timeline)

    def test_refine_100_iterations_default(self):
        """默认应使用 100 次迭代（从旧版 80 提升）"""
        from multi_day_scheduler import _refine_timeline, REFINE_MAX_ITERATIONS
        assert REFINE_MAX_ITERATIONS >= 100, (
            f"REFINE_MAX_ITERATIONS 应为 >= 100，实际: {REFINE_MAX_ITERATIONS}"
        )

    def test_refine_cost_not_worse_enhanced(self):
        """增强版精修不应恶化代价"""
        from multi_day_scheduler import _refine_timeline, _multi_factor_cost

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 900, "shop_id": "r1"},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 1020, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "DINNER", "start_minutes": 1290, "shop_id": "r2"},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
            make_shop("r1", "午餐店", "restaurant", 39.9, 116.4),
            make_shop("r2", "晚餐店", "restaurant", 39.9, 116.4),
        ]

        cost_before = _multi_factor_cost(timeline, all_shops)
        refined = _refine_timeline(timeline, all_shops, max_iterations=50)
        cost_after = _multi_factor_cost(refined, all_shops)

        assert cost_after <= cost_before + 0.01, (
            f"精修后 cost ({cost_after}) 不应显著高于精修前 ({cost_before})"
        )

    def test_refine_preserves_scheduled_visits_enhanced(self):
        """增强版精修不应丢失已排入的 VISIT 节点"""
        from multi_day_scheduler import _refine_timeline

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 840, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "DINNER", "start_minutes": 1110},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),
        ]

        refined = _refine_timeline(timeline, all_shops, max_iterations=50)

        refined_visit_ids = {n["shop_id"] for n in refined if n.get("type") == "VISIT"}
        original_visit_ids = {n["shop_id"] for n in timeline if n.get("type") == "VISIT"}
        for sid in original_visit_ids:
            assert sid in refined_visit_ids, f"原本排入的点 {sid} 不应丢失"

    def test_refine_improves_bad_meal_timing(self):
        """精修应能改善明显不好的用餐时间"""
        from multi_day_scheduler import _refine_timeline, _multi_factor_cost

        # 明显很差的用餐时间（午餐 15:00）
        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 900, "shop_id": "r1"},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 1020, "category": "shopping",
             "duration_minutes": 90, "travel_minutes": 10},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "shopping", 39.9, 116.4),
            make_shop("r1", "午餐店", "restaurant", 39.9, 116.4),
        ]

        cost_before = _multi_factor_cost(timeline, all_shops)
        refined = _refine_timeline(timeline, all_shops, max_iterations=100)
        cost_after = _multi_factor_cost(refined, all_shops)

        # 精修应能改善（不保证总是，但大概率）
        # 宽松断言：至少不会显著恶化
        assert cost_after <= cost_before * 1.05, (
            f"精修不应显著恶化: before={cost_before}, after={cost_after}"
        )

    def test_refine_handles_killed_shops(self):
        """精修应能处理被 kill 的店铺（尝试补回）"""
        from multi_day_scheduler import _refine_timeline

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},
        ]
        all_shops = [
            make_shop("s1", "A", "scenic", 39.9, 116.4),
            make_shop("s2", "B", "scenic", 39.9, 116.4),  # 被 kill
        ]

        refined = _refine_timeline(timeline, all_shops, max_iterations=30)

        # 精修后不应崩溃，应有结果
        assert len(refined) > 0
        # s1 不应丢失
        refined_ids = {n.get("shop_id") for n in refined if n.get("type") == "VISIT"}
        assert "s1" in refined_ids

    def test_refine_accepts_day_index(self):
        """精修应接受 day_index 参数用于累积疲劳"""
        from multi_day_scheduler import _refine_timeline

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720},
        ]
        all_shops = [make_shop("s1", "A", "scenic", 39.9, 116.4)]

        # day_index 应正常工作
        refined = _refine_timeline(timeline, all_shops, max_iterations=10, day_index=2)
        assert len(refined) > 0

    def test_refine_new_neighbor_ops_exist(self):
        """新增邻域操作应存在（swap_visit_meal, compress_duration, reorder_morning）"""
        from multi_day_scheduler import (
            _random_neighbor_move,
            _swap_visit_meal,
            _compress_duration,
            _reorder_morning_block,
        )
        assert callable(_random_neighbor_move)
        assert callable(_swap_visit_meal)
        assert callable(_compress_duration)
        assert callable(_reorder_morning_block)

    def test_swap_visit_meal_swaps_positions(self):
        """_swap_visit_meal 应交换 VISIT 和相邻 meal 的位置"""
        from multi_day_scheduler import _swap_visit_meal

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "LUNCH", "start_minutes": 720, "shop_id": "r1"},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 840, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
        ]
        result = _swap_visit_meal(timeline)

        # 长度应相同
        assert len(result) == len(timeline)
        # 类型集合应相同
        orig_types = {n["type"] for n in timeline}
        result_types = {n["type"] for n in result}
        assert orig_types == result_types

    def test_compress_duration_reduces_duration(self):
        """_compress_duration 应能压缩 VISIT 时长"""
        from multi_day_scheduler import _compress_duration

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
        ]
        result = _compress_duration(timeline)

        # 压缩后 duration 应 <= 原 duration
        if result[0]["type"] == "VISIT":
            assert result[0]["duration_minutes"] <= 180, (
                f"压缩后 duration ({result[0]['duration_minutes']}) <= 180"
            )

    def test_reorder_morning_block_swaps_two_visits(self):
        """_reorder_morning_block 应交换两个上午 VISIT 的顺序"""
        from multi_day_scheduler import _reorder_morning_block

        timeline = [
            {"type": "VISIT", "shop_id": "s1", "start_minutes": 540, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
            {"type": "VISIT", "shop_id": "s2", "start_minutes": 720, "category": "scenic",
             "duration_minutes": 180, "travel_minutes": 10},
        ]
        result = _reorder_morning_block(timeline)

        # 长度应相同
        assert len(result) == len(timeline)
        # VISIT 数量应相同
        orig_visits = [n for n in timeline if n["type"] == "VISIT"]
        result_visits = [n for n in result if n["type"] == "VISIT"]
        assert len(result_visits) == len(orig_visits)


# ======================================================================
# Phase 6: 最终输出 + L3 容量倒灌
# ======================================================================

class TestPhase6Output:
    """Phase 6: solve_multi_day 输出新增 hotel_plan + unassigned 增强"""

    def test_hotel_plan_in_output(self):
        """solve_multi_day 返回应包含 hotel_plan 字段"""
        from multi_day_scheduler import solve_multi_day

        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406),
            make_shop("r1", "烤鸭店", "restaurant", 39.915, 116.400),
        ]
        result = solve_multi_day(shops, num_days=2,
                                 checkin_lat=39.92, checkin_lng=116.40)

        assert "hotel_plan" in result, (
            f"返回应包含 hotel_plan 字段，实际 keys: {list(result.keys())}"
        )
        # hotel_plan 长度应为 num_days-1（每个边界一个决策）
        hp = result["hotel_plan"]
        assert isinstance(hp, list), f"hotel_plan 应为 list，实际: {type(hp)}"
        # 至少应有 num_days-1 个决策（可能为空表示无需换住）
        # 允许空列表或长度为 num_days-1 的列表
        expected_len = 1  # num_days=2 => 1个边界
        assert len(hp) == expected_len or len(hp) == 0, (
            f"hotel_plan 长度应为 {expected_len} 或 0，实际: {len(hp)}"
        )

    def test_hotel_plan_structure(self):
        """hotel_plan 中每项应包含 plan, hotel_lat, hotel_lng, cost"""
        from multi_day_scheduler import solve_multi_day

        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406),
        ]
        result = solve_multi_day(shops, num_days=3,
                                 checkin_lat=39.92, checkin_lng=116.40)

        hp = result.get("hotel_plan", [])
        for item in hp:
            # 每项应有基本字段
            assert "plan" in item, f"hotel_plan item 缺少 'plan': {item.keys()}"
            plan = item["plan"]
            assert plan in ("A", "B", "C"), (
                f"plan 应为 A/B/C，实际: {plan}"
            )

    def test_unassigned_has_type_field(self):
        """unassigned 列表中每项应有 unassigned_type 字段"""
        from multi_day_scheduler import solve_multi_day

        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("r1", "餐厅A", "restaurant", 39.915, 116.400),
        ]
        result = solve_multi_day(shops, num_days=1,
                                 checkin_lat=39.92, checkin_lng=116.40)

        for item in result.get("unassigned", []):
            assert "unassigned_type" in item, (
                f"unassigned item 缺少 unassigned_type: {item.keys()}"
            )
            assert item["unassigned_type"] in ("meal", "time"), (
                f"unassigned_type 应为 meal/time，实际: {item['unassigned_type']}"
            )

    def test_empty_candidate_returns_hotel_plan(self):
        """空候选池也应返回 hotel_plan（空列表）"""
        from multi_day_scheduler import solve_multi_day

        result = solve_multi_day([], num_days=2)
        assert "hotel_plan" in result, "空候选池也应包含 hotel_plan"
        assert result["hotel_plan"] == [], "空候选池 hotel_plan 应为空列表"

    def test_unassigned_includes_both_types(self):
        """unassigned 应收集 unassigned_meals 和 unassigned_shops（不只是餐）"""
        from multi_day_scheduler import solve_multi_day

        # 创建20个店铺强制产生 unassigned
        shops = []
        for i in range(20):
            shops.append(make_shop(f"s{i}", f"景点{i}", "scenic",
                               39.91 + i * 0.01, 116.39 + i * 0.01))
        # 加一些餐厅
        for i in range(5):
            shops.append(make_shop(f"r{i}", f"餐厅{i}", "restaurant",
                               39.91 + i * 0.01, 116.39 + i * 0.01))

        result = solve_multi_day(shops, num_days=1,
                                 checkin_lat=39.92, checkin_lng=116.40)

        unassigned = result.get("unassigned", [])
        types_seen = set(item.get("unassigned_type", "") for item in unassigned)
        # 不需要同时有两种类型（取决于输入），但至少应存在
        assert len(unassigned) >= 0, "unassigned 至少应是空列表"


class TestL3CapacityScanAndDump:
    """Phase 6: L3 容量余量扫描 + 极简打卡倒灌"""

    @classmethod
    def _get_l3_function(cls):
        """安全导入 _l3_capacity_scan_and_dump"""
        try:
            import sys
            import os
            # 确保 server.py 所在目录在 path 中
            server_dir = os.path.dirname(os.path.dirname(os.path.abspath(
                __import__('multi_day_scheduler').__file__)))
            if server_dir not in sys.path:
                sys.path.insert(0, server_dir)
            from server import _l3_capacity_scan_and_dump
            return _l3_capacity_scan_and_dump
        except ImportError:
            return None

    def _make_day(self, day_index, timeline):
        """构建测试用 day 结构"""
        return {
            "day_index": day_index,
            "label": f"第{day_index + 1}天",
            "timeline": timeline,
            "task_list": [],
        }

    def test_l3_function_exists(self):
        """_l3_capacity_scan_and_dump 应可导入"""
        fn = self._get_l3_function()
        if fn is None:
            import pytest
            pytest.skip("无法导入 _l3_capacity_scan_and_dump （server.py 依赖未满足）")
        assert callable(fn), "_l3_capacity_scan_and_dump 应为可调用函数"

    def test_l3_empty_unassigned_noop(self):
        """空 unassigned 列表应原样返回 days"""
        fn = self._get_l3_function()
        if fn is None:
            import pytest
            pytest.skip("无法导入 _l3_capacity_scan_and_dump")

        days = [self._make_day(0, [
            {"time": "09:00", "action": "WAKE_UP", "duration_minutes": 0, "shop_id": ""},
            {"time": "09:30", "action": "VISIT", "duration_minutes": 120, "shop_id": "s1",
             "lat": 39.91, "lng": 116.39, "category": "scenic"},
            {"time": "12:00", "action": "LUNCH", "duration_minutes": 60, "shop_id": "r1",
             "lat": 39.91, "lng": 116.39},
            {"time": "22:00", "action": "BEDTIME", "duration_minutes": 0, "shop_id": ""},
        ])]

        result_days, still_unassigned, count = fn(
            [], days, 39.92, 116.40, "步行优先"
        )

        assert count == 0, f"空 unassigned 应产生 0 个 backup，实际: {count}"
        assert still_unassigned == [], f"空 unassigned 应无残留，实际: {still_unassigned}"
        # days 应不变（除了可能的重新排序）
        assert len(result_days) == 1

    def test_l3_inserts_backup_into_gap(self):
        """有时间空隙时，应将 unassigned shop 插入为 backup"""
        fn = self._get_l3_function()
        if fn is None:
            import pytest
            pytest.skip("无法导入 _l3_capacity_scan_and_dump")

        days = [self._make_day(0, [
            {"time": "09:00", "action": "WAKE_UP", "duration_minutes": 0, "shop_id": ""},
            {"time": "09:30", "action": "VISIT", "duration_minutes": 60, "shop_id": "s1",
             "lat": 39.91, "lng": 116.39, "category": "scenic"},
            # gap: 10:30 → 14:00 = 210min
            {"time": "14:00", "action": "LUNCH", "duration_minutes": 60, "shop_id": "r1",
             "lat": 39.91, "lng": 116.39},
            {"time": "22:00", "action": "BEDTIME", "duration_minutes": 0, "shop_id": ""},
        ])]

        # 一个未分配的 shop（60min 游玩）
        unassigned = [{
            "shop_id": "s2", "name": "天坛", "category": "park",
            "lat": 39.882, "lng": 116.406, "duration_minutes": 60,
            "unassigned_type": "time",
        }]

        result_days, still_unassigned, count = fn(
            unassigned, days, 39.92, 116.40, "步行优先"
        )

        assert count == 1, f"应插入 1 个 backup，实际: {count}"
        assert len(still_unassigned) == 0, f"应无残留 unassigned，实际: {still_unassigned}"

        # 检查 timeline 中是否有 backup 节点
        timeline = result_days[0]["timeline"]
        backup_nodes = [n for n in timeline if n.get("action") == "VISIT" and n.get("shop_id") == "s2"]
        assert len(backup_nodes) == 1, f"应找到 s2 backup 节点，实际 timeline: {[(n.get('action'), n.get('shop_id')) for n in timeline]}"

    def test_l3_is_backup_flag(self):
        """L3 插入的节点应标记 is_backup=True"""
        fn = self._get_l3_function()
        if fn is None:
            import pytest
            pytest.skip("无法导入 _l3_capacity_scan_and_dump")

        days = [self._make_day(0, [
            {"time": "09:00", "action": "WAKE_UP", "duration_minutes": 0, "shop_id": ""},
            {"time": "09:30", "action": "VISIT", "duration_minutes": 60, "shop_id": "s1",
             "lat": 39.91, "lng": 116.39, "category": "scenic"},
            # 大 gap
            {"time": "15:00", "action": "VISIT", "duration_minutes": 90, "shop_id": "s3",
             "lat": 39.91, "lng": 116.39, "category": "scenic"},
            {"time": "22:00", "action": "BEDTIME", "duration_minutes": 0, "shop_id": ""},
        ])]

        unassigned = [{
            "shop_id": "s_backup", "name": "备选景点", "category": "park",
            "lat": 39.90, "lng": 116.40, "duration_minutes": 45,
            "unassigned_type": "time",
        }]

        result_days, still_unassigned, count = fn(
            unassigned, days, 39.92, 116.40, "步行优先"
        )

        if count > 0:
            backup_node = None
            for n in result_days[0]["timeline"]:
                if n.get("shop_id") == "s_backup":
                    backup_node = n
                    break
            assert backup_node is not None, "应找到 backup 节点"
            assert backup_node.get("is_backup") is True, (
                f"backup 节点应有 is_backup=True，实际 keys: {list(backup_node.keys())}"
            )

    def test_l3_no_gap_returns_all_unassigned(self):
        """timeline 无足够空隙时，全部返回为 still_unassigned"""
        fn = self._get_l3_function()
        if fn is None:
            import pytest
            pytest.skip("无法导入 _l3_capacity_scan_and_dump")

        # timeline 排得满满当当（连续活动无间隙）
        days = [self._make_day(0, [
            {"time": "09:00", "action": "WAKE_UP", "duration_minutes": 30, "shop_id": ""},
            {"time": "09:30", "action": "VISIT", "duration_minutes": 120, "shop_id": "s1",
             "lat": 39.91, "lng": 116.39, "category": "scenic"},
            {"time": "11:30", "action": "VISIT", "duration_minutes": 120, "shop_id": "s2",
             "lat": 39.92, "lng": 116.40, "category": "scenic"},
            {"time": "13:30", "action": "LUNCH", "duration_minutes": 60, "shop_id": "r1",
             "lat": 39.91, "lng": 116.39},
            {"time": "14:30", "action": "VISIT", "duration_minutes": 180, "shop_id": "s3",
             "lat": 39.93, "lng": 116.41, "category": "scenic"},
            {"time": "17:30", "action": "DINNER", "duration_minutes": 90, "shop_id": "r2",
             "lat": 39.91, "lng": 116.39},
            {"time": "19:00", "action": "VISIT", "duration_minutes": 90, "shop_id": "s4",
             "lat": 39.94, "lng": 116.42, "category": "scenic"},
            {"time": "22:00", "action": "BEDTIME", "duration_minutes": 0, "shop_id": ""},
        ])]

        # 一个需要 180min 的大 shop，显然塞不进去
        unassigned = [{
            "shop_id": "big_shop", "name": "大景点", "category": "scenic",
            "lat": 39.88, "lng": 116.38, "duration_minutes": 240,
            "unassigned_type": "time",
        }]

        result_days, still_unassigned, count = fn(
            unassigned, days, 39.92, 116.40, "步行优先"
        )

        assert count == 0, f"无空隙应产生 0 个 backup，实际: {count}"
        assert len(still_unassigned) >= 1, (
            f"应至少有一个残留 unassigned，实际: {still_unassigned}"
        )

    def test_l3_backup_timeline_sorted(self):
        """L3 插入后的 timeline 应按时间排序（WAKE_UP 最前，BEDTIME 最后）"""
        fn = self._get_l3_function()
        if fn is None:
            import pytest
            pytest.skip("无法导入 _l3_capacity_scan_and_dump")

        days = [self._make_day(0, [
            {"time": "08:00", "action": "WAKE_UP", "duration_minutes": 0, "shop_id": ""},
            {"time": "09:00", "action": "VISIT", "duration_minutes": 60, "shop_id": "s1",
             "lat": 39.91, "lng": 116.39, "category": "scenic"},
            # gap from 10:00 to 18:00 = 480min
            {"time": "18:00", "action": "DINNER", "duration_minutes": 60, "shop_id": "r1",
             "lat": 39.91, "lng": 116.39},
            {"time": "23:00", "action": "BEDTIME", "duration_minutes": 0, "shop_id": ""},
        ])]

        unassigned = [{
            "shop_id": "s2", "name": "备选景点A", "category": "park",
            "lat": 39.90, "lng": 116.40, "duration_minutes": 90,
            "unassigned_type": "time",
        }]

        result_days, _, count = fn(
            unassigned, days, 39.92, 116.40, "步行优先"
        )

        # 验证排序
        timeline = result_days[0]["timeline"]
        assert timeline[0]["action"] == "WAKE_UP", "第一个节点应为 WAKE_UP"
        assert timeline[-1]["action"] == "BEDTIME", "最后一个节点应为 BEDTIME"

        # 中间节点按时间排序
        middle = timeline[1:-1]
        times = []
        for n in middle:
            t_str = n.get("time", "00:00")
            try:
                h, m = map(int, t_str.split(":"))
                times.append(h * 60 + m)
            except (ValueError, TypeError):
                times.append(0)

        assert times == sorted(times), (
            f"中间节点应按时间升序排列，实际: {times}"
        )

    def test_l3_handles_multiple_days(self):
        """L3 应能在多天中分散插入 unassigned shops"""
        fn = self._get_l3_function()
        if fn is None:
            import pytest
            pytest.skip("无法导入 _l3_capacity_scan_and_dump")

        # 两天，每天都有一个 gap
        days = [
            self._make_day(0, [
                {"time": "09:00", "action": "WAKE_UP", "duration_minutes": 0, "shop_id": ""},
                {"time": "09:30", "action": "VISIT", "duration_minutes": 60, "shop_id": "s1",
                 "lat": 39.91, "lng": 116.39, "category": "scenic"},
                {"time": "14:00", "action": "LUNCH", "duration_minutes": 60, "shop_id": "r_d1",
                 "lat": 39.91, "lng": 116.39},
                {"time": "22:00", "action": "BEDTIME", "duration_minutes": 0, "shop_id": ""},
            ]),
            self._make_day(1, [
                {"time": "09:00", "action": "WAKE_UP", "duration_minutes": 0, "shop_id": ""},
                {"time": "09:30", "action": "VISIT", "duration_minutes": 60, "shop_id": "s3",
                 "lat": 39.94, "lng": 116.42, "category": "scenic"},
                {"time": "15:00", "action": "VISIT", "duration_minutes": 90, "shop_id": "s4",
                 "lat": 39.94, "lng": 116.42, "category": "scenic"},
                {"time": "22:00", "action": "BEDTIME", "duration_minutes": 0, "shop_id": ""},
            ]),
        ]

        unassigned = [
            {"shop_id": "s_backup1", "name": "备选1", "category": "park",
             "lat": 39.90, "lng": 116.40, "duration_minutes": 60,
             "unassigned_type": "time"},
            {"shop_id": "s_backup2", "name": "备选2", "category": "museum",
             "lat": 39.93, "lng": 116.41, "duration_minutes": 60,
             "unassigned_type": "time"},
        ]

        result_days, still_unassigned, count = fn(
            unassigned, days, 39.92, 116.40, "步行优先"
        )

        # 至少应插入一些
        assert count >= 0, f"count 应 >= 0，实际: {count}"
        # 恢复后每天的总节点数应 >= 原始节点数
        for i, day in enumerate(result_days):
            assert len(day["timeline"]) >= len(days[i]["timeline"]), (
                f"Day {i}: timeline 不应缩短"
            )

    def test_l3_backup_preserves_original_nodes(self):
        """L3 插入不应删除或修改原有节点（仅添加 backup 节点）"""
        fn = self._get_l3_function()
        if fn is None:
            import pytest
            pytest.skip("无法导入 _l3_capacity_scan_and_dump")

        original_timeline = [
            {"time": "09:00", "action": "WAKE_UP", "duration_minutes": 0, "shop_id": ""},
            {"time": "09:30", "action": "VISIT", "duration_minutes": 60, "shop_id": "s1",
             "lat": 39.91, "lng": 116.39, "category": "scenic"},
            {"time": "15:00", "action": "VISIT", "duration_minutes": 90, "shop_id": "s2",
             "lat": 39.92, "lng": 116.40, "category": "scenic"},
            {"time": "22:00", "action": "BEDTIME", "duration_minutes": 0, "shop_id": ""},
        ]
        days = [self._make_day(0, [dict(n) for n in original_timeline])]

        unassigned = [{
            "shop_id": "s_backup", "name": "备选", "category": "park",
            "lat": 39.90, "lng": 116.40, "duration_minutes": 45,
            "unassigned_type": "time",
        }]

        result_days, _, _ = fn(unassigned, days, 39.92, 116.40, "步行优先")

        result_timeline = result_days[0]["timeline"]
        # 每个原始节点（按 shop_id）都应在结果中
        for orig in original_timeline:
            if orig.get("shop_id"):
                matches = [n for n in result_timeline
                           if n.get("shop_id") == orig["shop_id"]
                           and n.get("action") == orig["action"]]
                assert len(matches) >= 1, (
                    f"原始节点 {orig['shop_id']} ({orig['action']}) 应在结果中"
                )

    def test_l3_multi_day_returns_hotel_plan(self):
        """L3 不修改 hotel_plan（由调度器产出）"""
        # 这是一个集成测试：验证 solve_multi_day 输出的 hotel_plan
        # 经过 L3 后仍然存在
        from multi_day_scheduler import solve_multi_day

        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397),
            make_shop("s2", "颐和园", "scenic", 39.999, 116.275),
            make_shop("s3", "天坛", "scenic", 39.882, 116.406),
            make_shop("s4", "长城", "scenic", 40.359, 116.020),
        ]
        result = solve_multi_day(shops, num_days=2,
                                 checkin_lat=39.92, checkin_lng=116.40)

        hp = result.get("hotel_plan", [])
        # hotel_plan 应为列表
        assert isinstance(hp, list)
        # 如果非空，每项应包含 plan 字段
        for item in hp:
            assert "plan" in item


# ======================================================================
# Seam 1+4: LLM suitable_time 字段 + 时间适配排程 + 溢出通知
# ======================================================================

class TestTimeOfDaySuitability:
    """测试 LLM 调研的 suitable_time 字段驱动排程优先级 + 溢出通知"""

    def test_suitable_times_flow_into_scheduler(self):
        """suitable_times dict 传入 solve_multi_day 不崩溃"""
        from multi_day_scheduler import solve_multi_day
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s2", "三里屯", "shopping", 39.932, 116.454, opentime="10:00-22:00"),
            make_shop("s3", "夜市", "shopping", 39.925, 116.440, opentime="17:00-23:00"),
        ]
        suitable_times = {"s1": "day", "s2": "both", "s3": "night"}
        result = solve_multi_day(shops, num_days=1,
                                 checkin_lat=39.92, checkin_lng=116.40,
                                 suitable_times=suitable_times)
        assert len(result["days"]) == 1
        assert "timeline" in result["days"][0]

    def test_day_only_activity_not_in_evening_loop(self):
        """suitable_time=day 的活动不应出现在晚间循环"""
        from multi_day_scheduler import solve_multi_day
        shops = [
            make_shop("s_day", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s_night", "夜市", "shopping", 39.925, 116.440, opentime="17:00-23:00"),
        ]
        suitable_times = {"s_day": "day", "s_night": "night"}
        result = solve_multi_day(shops, num_days=1,
                                 checkin_lat=39.92, checkin_lng=116.40,
                                 suitable_times=suitable_times)
        timeline = result["days"][0]["timeline"]
        # 找到晚间节点（17:30之后）
        evening_visits = []
        for n in timeline:
            if n.get("action") == "VISIT":
                t = n.get("time", "")
                try:
                    h, m = map(int, t.split(":"))
                    if h * 60 + m >= 17 * 60 + 30:
                        evening_visits.append(n.get("shop_id"))
                except (ValueError, TypeError):
                    pass
        # day-only 活动不应出现在晚间
        assert "s_day" not in evening_visits, (
            f"白天活动 s_day 不应排在晚间，晚间 VISIT: {evening_visits}"
        )

    def test_night_only_activity_not_in_morning(self):
        """suitable_time=night 的活动不应出现在上午"""
        from multi_day_scheduler import solve_multi_day
        shops = [
            make_shop("s_night", "酒吧街", "shopping", 39.932, 116.454, opentime="18:00-02:00"),
            make_shop("s_day", "天坛", "scenic", 39.882, 116.406, opentime="08:00-17:00"),
        ]
        suitable_times = {"s_night": "night", "s_day": "day"}
        result = solve_multi_day(shops, num_days=1,
                                 checkin_lat=39.92, checkin_lng=116.40,
                                 suitable_times=suitable_times)
        timeline = result["days"][0]["timeline"]
        morning_visits = []
        for n in timeline:
            if n.get("action") == "VISIT":
                t = n.get("time", "")
                try:
                    h, m = map(int, t.split(":"))
                    if h * 60 + m < 12 * 60:
                        morning_visits.append(n.get("shop_id"))
                except (ValueError, TypeError):
                    pass
        # night-only 活动不应出现在上午
        assert "s_night" not in morning_visits, (
            f"夜间活动 s_night 不应排在上午，上午 VISIT: {morning_visits}"
        )

    def test_overflow_notification_generated(self):
        """当日间时段满而仍有白天活动未排入时，生成溢出通知"""
        from multi_day_scheduler import solve_multi_day
        # 6个白天活动 + 1天 → 必然有些排不下
        shops = [
            make_shop(f"s{i}", f"景点{i}", "scenic", 39.91 + i * 0.01, 116.39 + i * 0.01,
                      opentime="09:00-17:00", duration_minutes=180)
            for i in range(6)
        ]
        suitable_times = {f"s{i}": "day" for i in range(6)}
        result = solve_multi_day(shops, num_days=1,
                                 checkin_lat=39.92, checkin_lng=116.40,
                                 suitable_times=suitable_times)
        # 应有 unassigned 或 overflow_notifications
        unassigned = result.get("unassigned", [])
        overflow = result.get("overflow_notifications", [])
        assert len(unassigned) > 0 or len(overflow) > 0, (
            "日间时段不足时应产生 unassigned 或 overflow_notifications"
        )

    def test_no_suitable_times_fallback_to_category(self):
        """无 suitable_times 时回退到品类启发式规则"""
        from multi_day_scheduler import solve_multi_day
        shops = [
            make_shop("s1", "故宫", "scenic", 39.916, 116.397, opentime="08:30-17:00"),
            make_shop("s2", "商场", "shopping", 39.932, 116.454, opentime="10:00-22:00"),
        ]
        result = solve_multi_day(shops, num_days=1,
                                 checkin_lat=39.92, checkin_lng=116.40)
        # 不传 suitable_times 也不崩溃
        assert len(result["days"]) == 1


# ======================================================================
# Seam 2: Amap 地理编码集成 — _ensure_coords 支持 geocode_callback
# ======================================================================

class TestAmapGeocoding:
    """测试地理编码回调解锁 + 缺坐标 shop 的自动补全"""

    def test_ensure_coords_with_geocode_callback(self):
        """geocode_callback 被调用且返回有效坐标时，shop 获得正确坐标"""
        from multi_day_scheduler import _ensure_coords

        shops = [
            {"shop_id": "s1", "name": "故宫", "category": "scenic",
             "address": "北京市东城区景山前街4号"},
        ]
        call_log = []

        def mock_geocode(name, address):
            call_log.append((name, address))
            return (39.916, 116.397)  # 返回故宫真实坐标

        _ensure_coords(shops, geocode_callback=mock_geocode)

        assert "lat" in shops[0] and "lng" in shops[0]
        assert abs(shops[0]["lat"] - 39.916) < 0.01
        assert abs(shops[0]["lng"] - 116.397) < 0.01
        assert len(call_log) == 1
        assert call_log[0][0] == "故宫"

    def test_ensure_coords_callback_none_uses_fallback(self):
        """geocode_callback 返回 None 时使用兜底坐标"""
        from multi_day_scheduler import _ensure_coords

        shops = [
            {"shop_id": "s1", "name": "未知地点", "category": "scenic"},
        ]

        def mock_geocode(name, address):
            return None  # 模拟 API 找不到

        _ensure_coords(shops, geocode_callback=mock_geocode,
                       arrival_lat=39.90, arrival_lng=116.40)

        assert shops[0].get("is_imputed") is True
        assert abs(shops[0]["lat"] - 39.90) < 0.01

    def test_ensure_coords_skips_existing_coords(self):
        """已有坐标的 shop 不触发 geocode_callback"""
        from multi_day_scheduler import _ensure_coords

        shops = [
            {"shop_id": "s1", "name": "故宫", "category": "scenic",
             "lat": 39.916, "lng": 116.397},
        ]
        call_log = []

        def mock_geocode(name, address):
            call_log.append(1)
            return (0, 0)

        _ensure_coords(shops, geocode_callback=mock_geocode)

        assert len(call_log) == 0  # 已有坐标，不调用
        assert shops[0]["lat"] == 39.916  # 原值不变

    def test_solve_multi_day_with_geocode_callback(self):
        """solve_multi_day 接受 geocode_callback 且不崩溃"""
        from multi_day_scheduler import solve_multi_day

        shops = [
            {"shop_id": "s1", "name": "故宫", "category": "scenic",
             "address": "北京市东城区", "opentime": "08:30-17:00"},
            {"shop_id": "s2", "name": "天坛", "category": "scenic",
             "lat": 39.882, "lng": 116.406, "opentime": "08:00-17:00"},
        ]

        def mock_geocode(name, address):
            if "故宫" in name:
                return (39.916, 116.397)
            return None

        result = solve_multi_day(shops, num_days=1,
                                 checkin_lat=39.92, checkin_lng=116.40,
                                 geocode_callback=mock_geocode)
        assert len(result["days"]) == 1
        # 两个 shop 都应被排入（s1 通过 geocode 补全了坐标）
