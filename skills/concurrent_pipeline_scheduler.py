"""
concurrent_pipeline_scheduler.py —— 工业级全通用排程引擎 V5 (生产版)
================================================================
物理契约:
  solve_concurrent_timeline(task_list: list, spatial_matrix: dict, current_time_str: str) -> dict

架构准则:
  - 全程 int 分钟级运算 (math.ceil 路程取整), 无 timedelta 秒级精度污染
  - routes.get(key, {}).get('distance_meters', 0) 防崩溃
  - Timeline 每个动作字典严格包含 target_location_id, next_location_id, task_id 结构化字段
  - 步骤一: 钉钉子 (fixed_start_time 锚点)
  - 步骤二: 反向推算推迟出发
  - 步骤三: 顺向狂飙 DROP -> EXECUTION -> 固定行程 -> PICK
  - WALK: ceil(dist/80) | TAXI: 5 + ceil(dist/400)
  - DROP/PICK: 5min
"""

import math, json
from typing import List, Dict, Any, Tuple


# ======================================================================
# 全局常量
# ======================================================================
WALK_SPEED = 80
TAXI_SPEED = 400
TAXI_WAIT = 5
DROP_PICK_DURATION = 5


# ======================================================================
# Haversine 回退估算
# ======================================================================
def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * \
        math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _parse_coord(coord_str: str) -> Tuple[float, float]:
    parts = coord_str.strip().split(",")
    return float(parts[0].strip()), float(parts[1].strip())


# ======================================================================
# 路由防御性查询 (KeyError 永不当机)
# ======================================================================
def _get_route(loc_a: str, loc_b: str, sm: dict) -> Tuple[str, int]:
    """
    返回 (transport_mode, distance_meters)
    1. routes 字典中查找 loc_a->loc_b 或 loc_b->loc_a
    2. 缺失则回退 Haversine 物理估算
    3. 纯防御,永不抛 KeyError
    """
    routes = sm.get("routes", {})
    for key in (f"{loc_a}->{loc_b}", f"{loc_b}->{loc_a}"):
        entry = routes.get(key)
        if entry is not None:
            return entry.get("transport_mode", "WALK"), int(entry.get("distance_meters", 0))
    locs = sm.get("locations", {})
    ca = locs.get(loc_a, {}).get("coord", "")
    cb = locs.get(loc_b, {}).get("coord", "")
    if ca and cb:
        lat1, lng1 = _parse_coord(ca)
        lat2, lng2 = _parse_coord(cb)
        return "WALK", int(_haversine_km(lat1, lng1, lat2, lng2) * 1000.0)
    return "WALK", 0


def _travel_min(mode: str, dist: int) -> int:
    if mode == "TAXI":
        return TAXI_WAIT + math.ceil(dist / TAXI_SPEED)
    return math.ceil(dist / WALK_SPEED)


# ======================================================================
# 时间工具 (纯 int 分钟级)
# ======================================================================
def _parse(t: str) -> int:
    p = t.strip().split(":")
    return int(p[0]) * 60 + int(p[1])


def _fmt(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


# ======================================================================
# 核心排程引擎
# ======================================================================

def solve_concurrent_timeline(
    task_list: list,
    spatial_matrix: dict,
    current_time_str: str,
) -> dict:
    """
    【工业级通用排程引擎 I/O 契约桩 - 严禁私自修改任何字段类型】

    [EXPECTED INPUT JSON SCHEMA]:
    {
        "current_time_str": "14:00",
        "start_location_id": "loc_current",
        "task_list": [
            {
                "task_id": "str",
                "name": "str",
                "location_id": "str",
                "duration_minutes": int,
                "human_needed": bool,
                "fixed_start_time": "str_or_null"
            }
        ],
        "spatial_matrix": {
            "locations": {"loc_id": {"name": "str", "coord": "str"}},
            "routes": {"loc_A->loc_B": {"transport_mode": "WALK_or_TAXI", "distance_meters": int}}
        }
    }

    [EXPECTED OUTPUT JSON SCHEMA - SUCCESS]:
    {
        "status": "SUCCESS",
        "suggested_departure_time": "str",
        "total_duration_minutes": int,
        "timeline": [
            {
                "time": "str",
                "action": "DEPART | MOVE | DROP_TASK | START_TASK | WAIT | PICK_TASK",
                "target_location_id": "str",
                "next_location_id": "str_or_null",
                "task_id": "str_or_null",
                "memo": "str"
            }
        ]
    }

    [EXPECTED OUTPUT JSON SCHEMA - CONFLICT]:
    {
        "status": "CONFLICT",
        "conflict_task_id": "str",
        "message": "str"
    }
    """
    # ---------- 入参校验 ----------
    if not isinstance(task_list, list) or not task_list:
        return {"status": "CONFLICT", "message": "task_list 必须是非空数组"}
    if not isinstance(spatial_matrix, dict) or "locations" not in spatial_matrix:
        return {"status": "CONFLICT", "message": "spatial_matrix 必须包含 locations"}
    if not isinstance(current_time_str, str) or not current_time_str:
        return {"status": "CONFLICT", "message": "current_time_str 必须是非空字符串"}
    try:
        current_min = _parse(current_time_str)
    except Exception:
        return {"status": "CONFLICT", "message": f"current_time_str 格式无效: {current_time_str}"}
    for t in task_list:
        for k in ("task_id", "name", "location_id", "duration_minutes", "human_needed"):
            if k not in t:
                return {"status": "CONFLICT", "message": f"任务缺少字段: {k}"}
        if not isinstance(t["duration_minutes"], int) or t["duration_minutes"] <= 0:
            return {"status": "CONFLICT", "message": f"任务 {t.get('task_id')} duration_minutes 必须为正整数"}
        if not isinstance(t["human_needed"], bool):
            return {"status": "CONFLICT", "message": f"任务 {t.get('task_id')} human_needed 必须为布尔值"}
        if t.get("fixed_start_time") is not None:
            try:
                _parse(t["fixed_start_time"])
            except Exception:
                return {"status": "CONFLICT", "message": f"任务 {t.get('task_id')} fixed_start_time 格式无效"}

    # ---------- 任务分类 ----------
    fixed_tasks = sorted(
        [t for t in task_list if t.get("fixed_start_time") is not None],
        key=lambda t: _parse(t["fixed_start_time"]),
    )
    nonfixed_drop = [t for t in task_list if t.get("fixed_start_time") is None and not t["human_needed"]]
    nonfixed_exec = [t for t in task_list if t.get("fixed_start_time") is None and t["human_needed"]]

    # ==================================================================
    # 步骤一：钉钉子 — 标记固定行程占用分钟段
    # ==================================================================
    OCCUPIED = "FIXED"
    occupied_slots: Dict[int, str] = {}
    for ft in fixed_tasks:
        fs_min = _parse(ft["fixed_start_time"])
        for m in range(fs_min, fs_min + ft["duration_minutes"]):
            occupied_slots[m] = OCCUPIED

    # ==================================================================
    # 步骤二：反向推算 suggested_departure_time
    # ==================================================================
    suggested_departure = current_min
    if fixed_tasks:
        ft = fixed_tasks[0]
        fs_min = _parse(ft["fixed_start_time"])
        if fs_min > current_min:
            mode0, dist0 = _get_route(
                spatial_matrix.get("start_location_id", "loc_current"),
                ft["location_id"], spatial_matrix,
            )
            base_travel = _travel_min(mode0, dist0)
            work_total = sum(DROP_PICK_DURATION for _ in nonfixed_drop) + \
                         sum(t["duration_minutes"] for t in nonfixed_exec)
            segs = len(nonfixed_drop) + len(nonfixed_exec)
            if segs > 0:
                work_total += (segs - 1) * math.ceil(200 / WALK_SPEED)
            required = work_total + base_travel * 2
            latest = fs_min - required
            if latest > current_min:
                suggested_departure = latest

    # ==================================================================
    # 步骤三：顺向狂飙
    # ==================================================================
    cur_min = suggested_departure
    cur_loc = spatial_matrix.get("start_location_id", "loc_current")
    bg: Dict[str, int] = {}
    timeline: List[Dict[str, Any]] = []

    def push(action: str, time_str: str, target_loc: str = "",
             next_loc: str = "", task_id: str = "", memo: str = ""):
        entry: Dict[str, Any] = {
            "time": time_str,
            "action": action,
            "target_location_id": target_loc if target_loc else None,
            "next_location_id": next_loc if next_loc else None,
            "task_id": task_id if task_id else None,
            "memo": memo,
        }
        timeline.append(entry)

    # ----- 0. DEPART -----
    push("DEPART", _fmt(cur_min),
         target_loc=cur_loc, next_loc=cur_loc, memo="准备出发")

    # ==================================================================
    # 阶段 A: DROP 所有 human_needed == False 的任务
    # ==================================================================
    for task in nonfixed_drop:
        dest = task["location_id"]
        mode, dist = _get_route(cur_loc, dest, spatial_matrix)
        t_min = _travel_min(mode, dist)
        dest_name = spatial_matrix.get("locations", {}).get(dest, {}).get("name", dest)
        arr_min = cur_min + t_min

        move_memo = f"前往 {dest_name}"
        if mode == "TAXI":
            move_memo += f"，打车前往，等车5分钟+车程{dist}米"
        else:
            move_memo += f"，步行前往，物理距离{dist}米"

        push("MOVE", _fmt(cur_min),
             target_loc=cur_loc, next_loc=dest,
             task_id=task["task_id"], memo=move_memo)

        cur_min = arr_min
        bg_finish = cur_min + task["duration_minutes"]
        bg[dest] = bg_finish

        push("DROP_TASK", _fmt(cur_min),
             target_loc=dest, task_id=task["task_id"],
             memo=f"抵达。放下非在场任务[{task['name']}]，耗时{DROP_PICK_DURATION}分钟。"
                  f"后台倒计时{task['duration_minutes']}分钟开始（预计{_fmt(bg_finish)}完工）")

        cur_min += DROP_PICK_DURATION
        cur_loc = dest

    # ==================================================================
    # 阶段 B: 执行 human_needed == True 的自由任务
    # ==================================================================
    for task in nonfixed_exec:
        dest = task["location_id"]
        mode, dist = _get_route(cur_loc, dest, spatial_matrix)
        t_min = _travel_min(mode, dist)
        dest_name = spatial_matrix.get("locations", {}).get(dest, {}).get("name", dest)
        arr_min = cur_min + t_min
        end_min = arr_min + task["duration_minutes"]

        # --- 冲突检测：检查与固定行程是否有重叠 ---
        for m in range(arr_min, end_min):
            if m in occupied_slots:
                return {
                    "status": "CONFLICT",
                    "conflict_task_id": task["task_id"],
                    "message": f"无法完成排程规划！[{task['name']}] 预计执行时间段"
                               f"({_fmt(arr_min)}-{_fmt(end_min)})"
                               f"与您的固定硬行程存在不可调和的时空物理重叠！",
                }

        move_memo = f"前往 {dest_name}"
        if mode == "TAXI":
            move_memo += f"，打车前往，等车5分钟+车程{dist}米"
        else:
            move_memo += f"，步行前往，物理距离{dist}米"

        push("MOVE", _fmt(cur_min),
             target_loc=cur_loc, next_loc=dest,
             task_id=task["task_id"], memo=move_memo)

        cur_min = arr_min
        push("START_TASK", _fmt(cur_min),
             target_loc=dest, task_id=task["task_id"],
             memo=f"开始执行在场任务[{task['name']}]，人类必须全程在场，耗时{task['duration_minutes']}分钟")

        cur_min = end_min
        cur_loc = dest

    # ==================================================================
    # 阶段 C: 固定行程钉钉子
    # ==================================================================
    for task in fixed_tasks:
        dest = task["location_id"]
        dest_name = spatial_matrix.get("locations", {}).get(dest, {}).get("name", dest)
        mode, dist = _get_route(cur_loc, dest, spatial_matrix)
        t_min = _travel_min(mode, dist)
        arr_min = cur_min + t_min
        fs_min = _parse(task["fixed_start_time"])

        if arr_min > fs_min:
            return {
                "status": "CONFLICT",
                "conflict_task_id": task["task_id"],
                "message": f"无法在固定时间到达 [{task['name']}]！"
                           f"预计到达{_fmt(arr_min)}，但行程要求在{task['fixed_start_time']}开始",
            }

        move_memo = f"前往固定行程地点 {dest_name}"
        if mode == "TAXI":
            move_memo += f"，打车前往，等车5分钟+车程{dist}米"
        else:
            move_memo += f"，步行前往，物理距离{dist}米"

        push("MOVE", _fmt(cur_min),
             target_loc=cur_loc, next_loc=dest,
             task_id=task["task_id"], memo=move_memo)

        if arr_min < fs_min:
            wait_m = fs_min - arr_min
            push("WAIT", _fmt(arr_min),
                 target_loc=dest, task_id=task["task_id"],
                 memo=f"提前{wait_m}分钟到达固定行程。等待至{task['fixed_start_time']}开始执行（用户可休息）")
            arr_min = fs_min

        cur_min = arr_min
        push("START_TASK", _fmt(cur_min),
             target_loc=dest, task_id=task["task_id"],
             memo=f"硬约束锚点卡位：开始执行固定行程[{task['name']}]，耗时{task['duration_minutes']}分钟")
        cur_min += task["duration_minutes"]
        cur_loc = dest

    # ==================================================================
    # 阶段 D: PICK 收尾所有 DROP 的任务
    # ==================================================================
    for task in nonfixed_drop:
        dest = task["location_id"]
        bg_finish = bg.get(dest)
        if bg_finish is None:
            continue
        mode, dist = _get_route(cur_loc, dest, spatial_matrix)
        t_min = _travel_min(mode, dist)
        arr_min = cur_min + t_min
        dest_name = spatial_matrix.get("locations", {}).get(dest, {}).get("name", dest)

        move_memo = f"前往执行收尾阶段，返回 {dest_name}"
        if mode == "TAXI":
            move_memo += f"，打车前往，等车5分钟+车程{dist}米"
        else:
            move_memo += f"，步行前往，物理距离{dist}米"

        push("MOVE", _fmt(cur_min),
             target_loc=cur_loc, next_loc=dest,
             task_id=task["task_id"], memo=move_memo)

        cur_min = arr_min

        if cur_min < bg_finish:
            wait_m = bg_finish - cur_min
            push("WAIT", _fmt(cur_min),
                 target_loc=dest, task_id=task["task_id"],
                 memo=f"算法校验：当前时间{_fmt(cur_min)}未达到完工时间{_fmt(bg_finish)}。"
                      f"强行插入{wait_m}分钟空闲等待（用户可喝咖啡）")
            cur_min = bg_finish

        pick_end = cur_min + DROP_PICK_DURATION
        push("PICK_TASK", _fmt(cur_min),
             target_loc=dest, task_id=task["task_id"],
             memo=f"当前时间{_fmt(cur_min)}已远超任务完工时间{_fmt(bg_finish)}（或刚好到达）。"
                  f"执行PICK，耗时{DROP_PICK_DURATION}分钟，预计{_fmt(pick_end)}完成，全部行程结束")
        cur_min = pick_end
        cur_loc = dest

    # ---- 总耗时 ----
    total = cur_min - current_min

    return {
        "status": "SUCCESS",
        "suggested_departure_time": _fmt(suggested_departure),
        "total_duration_minutes": total,
        "timeline": timeline,
    }
