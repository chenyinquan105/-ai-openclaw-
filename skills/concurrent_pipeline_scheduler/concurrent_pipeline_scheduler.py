"""
concurrent_pipeline_scheduler.py —— 工业级全通用排程引擎 V5.3 (最终集成版)
================================================================
物理契约:
  solve_concurrent_timeline(task_list, spatial_matrix, current_time_str, ...) -> dict

核心逻辑:
  1. 空间重心策略: 过滤掉严重绕路的任务，将其推迟到固定行程(锚点)之后。
  2. 冲突预案模拟: 
     - 延误 > 15min 或 用户已拒绝: 自动延后。
     - 0 < 延误 <= 15min 且 未确认: 返回 CONFIRM_REQUIRED 触发 main.py 询问。
     - 无延误 或 用户已确认: 准许执行。
"""

import math
from typing import List, Dict, Any, Tuple

# ======================================================================
# 全局常量
# ======================================================================
WALK_SPEED = 80                    # 步行速度 m/min
TAXI_SPEED = 400                   # 出租车速度 m/min
TAXI_WAIT = 5                      # 打车等候 + 启动耗时 (分钟)
CAR_SPEED = 500                    # 驾车速度 m/min
CAR_WARMUP = 5                     # 启动汽车+停车耗时 (分钟)
METRO_ACCESS = 5                   # 步行到地铁站+等车+出站 (分钟)
METRO_SPEED = 500                  # 地铁行驶速度 m/min
DROP_PICK_DURATION = 5
MAX_TOLERABLE_DELAY = 15  # 软冲突容忍上限 (分钟)

# ======================================================================
# 工具函数
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

def _get_route(loc_a: str, loc_b: str, sm: dict) -> Tuple[str, int]:
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
    """
    根据交通模式和距离(m)计算总耗时(分钟)，包含启动/接驳/等待时间。
    已知 mode 值: WALK, TAXI, DRIVE, METRO, BUS
    """
    if mode == "TAXI":
        return TAXI_WAIT + math.ceil(dist / TAXI_SPEED)
    if mode == "DRIVE":
        return CAR_WARMUP + math.ceil(dist / CAR_SPEED)
    if mode == "METRO":
        return METRO_ACCESS + math.ceil(dist / METRO_SPEED)
    if mode == "BUS":
        return 5 + math.ceil(dist / 250)  # 5min 步行到站+等车
    # WALK 及未知模式
    return math.ceil(dist / WALK_SPEED)

def _parse(t: str) -> int:
    p = t.strip().split(":")
    return int(p[0]) * 60 + int(p[1])

def _fmt(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"

def _calculate_detour_score(start_loc: str, anchor_loc: str, task_loc: str, sm: dict) -> int:
    _, d1 = _get_route(start_loc, task_loc, sm)
    _, d2 = _get_route(task_loc, anchor_loc, sm)
    return d1 + d2

# ======================================================================
# 核心排程引擎
# ======================================================================

def solve_concurrent_timeline(
    task_list: list,
    spatial_matrix: dict,
    current_time_str: str,
    user_confirmed_tasks: list = None,
    user_rejected_tasks: list = None
) -> dict:
    user_confirmed_tasks = user_confirmed_tasks or []
    user_rejected_tasks = user_rejected_tasks or []

    # ---------- 1. 入参校验 ----------
    if not task_list: return {"status": "CONFLICT", "message": "task_list 不能为空"}
    try:
        current_min = _parse(current_time_str)
    except:
        return {"status": "CONFLICT", "message": "current_time_str 格式无效"}

    # ---------- 2. 任务分类与锚点锁定 ----------
    fixed_tasks = sorted(
        [t for t in task_list if t.get("fixed_start_time")],
        key=lambda t: _parse(t["fixed_start_time"])
    )
    # 初始分类
    nonfixed_drop = [t for t in task_list if not t.get("fixed_start_time") and not t["human_needed"]]
    nonfixed_exec = [t for t in task_list if not t.get("fixed_start_time") and t["human_needed"]]

    # —— 诊断日志 ——
    print(f"[调度器] 分类: FIXED={len(fixed_tasks)} DROP={len(nonfixed_drop)} EXEC={len(nonfixed_exec)}", flush=True)
    for t in nonfixed_drop:
        print(f"  DROP: {t['name']} (human_needed={t['human_needed']})", flush=True)
    for t in nonfixed_exec:
        print(f"  EXEC: {t['name']} (human_needed={t['human_needed']})", flush=True)
    
    occupied_slots = {}
    for ft in fixed_tasks:
        fs_min = _parse(ft["fixed_start_time"])
        for m in range(fs_min, fs_min + ft["duration_minutes"]):
            occupied_slots[m] = "FIXED"

    start_loc = spatial_matrix.get("start_location_id", "loc_current")
    anchor_loc = fixed_tasks[0]["location_id"] if fixed_tasks else None
    post_anchor_tasks = []

    # ---------- 3. 空间重心过滤 (Spatial Filter) ----------
    if anchor_loc:
        _, base_dist = _get_route(start_loc, anchor_loc, spatial_matrix)
        
        def spatial_split(tasks):
            on_way, way_far = [], []
            for t in tasks:
                d_score = _calculate_detour_score(start_loc, anchor_loc, t["location_id"], spatial_matrix)
                # 绕路比 > 2.0 且 绝对绕路 > 3km
                if d_score > (base_dist * 2) and (d_score - base_dist) > 3000:
                    way_far.append(t)
                else:
                    on_way.append((d_score, t))
            on_way.sort(key=lambda x: x[0])
            return [x[1] for x in on_way], way_far

        nonfixed_drop, far_drop = spatial_split(nonfixed_drop)
        nonfixed_exec, far_exec = spatial_split(nonfixed_exec)
        post_anchor_tasks.extend(far_drop + far_exec)

    # ---------- 4. 冲突预案模拟 (Conflict Simulation) ----------
    final_on_way_drop = []
    if anchor_loc:
        anchor_start_min = _parse(fixed_tasks[0]["fixed_start_time"])
        sim_min = current_min
        sim_loc = start_loc
        
        for task in nonfixed_drop:
            # 模拟：当前 -> 任务点 -> 锚点
            mode1, dist1 = _get_route(sim_loc, task["location_id"], spatial_matrix)
            arr_task = sim_min + _travel_min(mode1, dist1)
            
            mode2, dist2 = _get_route(task["location_id"], anchor_loc, spatial_matrix)
            arr_anchor = arr_task + DROP_PICK_DURATION + _travel_min(mode2, dist2)
            
            delay = arr_anchor - anchor_start_min
            
            # 判断逻辑
            if delay <= 0:
                final_on_way_drop.append(task)
                sim_min = arr_task + DROP_PICK_DURATION
                sim_loc = task["location_id"]
            elif delay > MAX_TOLERABLE_DELAY or task["task_id"] in user_rejected_tasks:
                # 延误太久或用户已拒绝：直接延后
                post_anchor_tasks.append(task)
            else:
                # 软冲突 (0-15min)：检查确认状态
                if task["task_id"] in user_confirmed_tasks:
                    final_on_way_drop.append(task)
                    sim_min = arr_task + DROP_PICK_DURATION
                    sim_loc = task["location_id"]
                else:
                    return {
                        "status": "CONFIRM_REQUIRED",
                        "conflict_task": task,
                        "delay_minutes": delay,
                        "fixed_task_name": fixed_tasks[0]["name"],
                        "message": f"执行[{task['name']}]将使[{fixed_tasks[0]['name']}]延误{delay}分钟，是否继续？"
                    }
        nonfixed_drop = final_on_way_drop

    # ---------- 5. 顺向狂飙执行 (Execution) ----------
    cur_min = current_min
    cur_loc = start_loc
    bg_finish_map = {} # location_id -> finish_time
    timeline = []

    def push(action, time_m, target_loc=None, next_loc=None, task_id=None, memo=""):
        timeline.append({
            "time": _fmt(time_m), "action": action,
            "target_location_id": target_loc, "next_location_id": next_loc,
            "task_id": task_id, "memo": memo
        })

    push("DEPART", cur_min, target_loc=cur_loc, memo="准备出发")

    # 阶段 A: 顺路 DROP
    if nonfixed_drop:
        print(f"[调度器] Phase A DROP: {[t['name'] for t in nonfixed_drop]}", flush=True)
    for task in nonfixed_drop:
        mode, dist = _get_route(cur_loc, task["location_id"], spatial_matrix)
        t_min = _travel_min(mode, dist)
        cur_min += t_min
        bg_finish_map[task["location_id"]] = cur_min + task["duration_minutes"]
        push("MOVE", cur_min - t_min, target_loc=cur_loc, next_loc=task["location_id"],
             task_id=task["task_id"], memo=f"前往 {task['name']}")
        push("DROP_TASK", cur_min, target_loc=task["location_id"], task_id=task["task_id"], memo=f"放下{task['name']}")
        cur_min += DROP_PICK_DURATION
        cur_loc = task["location_id"]

    # 阶段 B: 顺路 EXEC (必须人在场)
    if nonfixed_exec:
        print(f"[调度器] Phase B EXEC: {[t['name'] for t in nonfixed_exec]}", flush=True)
    for task in nonfixed_exec:
        mode, dist = _get_route(cur_loc, task["location_id"], spatial_matrix)
        t_min = _travel_min(mode, dist)
        arr_min = cur_min + t_min
        # 二次检查：是否由于前面的延误导致现在与锚点冲突
        if any(m in occupied_slots for m in range(arr_min, arr_min + task["duration_minutes"])):
            post_anchor_tasks.append(task)
            continue
        push("MOVE", cur_min, target_loc=cur_loc, next_loc=task["location_id"],
             task_id=task["task_id"], memo=f"前往 {task['name']}")
        cur_min = arr_min
        push("START_TASK", cur_min, target_loc=task["location_id"], task_id=task["task_id"], memo=f"开始{task['name']}")
        cur_min += task["duration_minutes"]
        cur_loc = task["location_id"]

    # 阶段 C: 固定行程 (锚点)
    for task in fixed_tasks:
        mode, dist = _get_route(cur_loc, task["location_id"], spatial_matrix)
        t_min = _travel_min(mode, dist)
        arr_min = cur_min + t_min
        fs_min = _parse(task["fixed_start_time"])
        
        push("MOVE", cur_min, target_loc=cur_loc, next_loc=task["location_id"],
             task_id=task["task_id"], memo=f"前往 {task['name']}")
        if arr_min < fs_min:
            push("WAIT", arr_min, target_loc=task["location_id"], memo="提前到达，等待开始")
            arr_min = fs_min
        
        cur_min = arr_min
        push("START_TASK", cur_min, target_loc=task["location_id"], task_id=task["task_id"], memo=f"开始{task['name']}")
        cur_min += task["duration_minutes"]
        cur_loc = task["location_id"]

    # 阶段 C.5: 延后任务处理 (Post Anchor) — MOVE 也有路径但无 depush 直接累加，不需要记忆
    for task in post_anchor_tasks:
        mode, dist = _get_route(cur_loc, task["location_id"], spatial_matrix)
        t_min = _travel_min(mode, dist)
        cur_min += t_min
        if not task["human_needed"]:
            bg_finish_map[task["location_id"]] = cur_min + task["duration_minutes"]
            push("MOVE", cur_min - t_min, target_loc=cur_loc, next_loc=task["location_id"],
                 task_id=task["task_id"], memo=f"前往 {task['name']}")
            push("DROP_TASK", cur_min, target_loc=task["location_id"], task_id=task["task_id"], memo=f"放下{task['name']}")
            cur_min += DROP_PICK_DURATION
        else:
            push("MOVE", cur_min - t_min, target_loc=cur_loc, next_loc=task["location_id"],
                 task_id=task["task_id"], memo=f"前往 {task['name']}")
            push("START_TASK", cur_min, target_loc=task["location_id"], task_id=task["task_id"], memo=f"开始{task['name']}")
            cur_min += task["duration_minutes"]
        cur_loc = task["location_id"]

    # 阶段 D: PICK 收尾 — 去回收地的 MOVE
    pick_tasks = [t for t in task_list if not t["human_needed"] and not t.get("fixed_start_time") and t["location_id"] in bg_finish_map]
    if pick_tasks:
        print(f"[调度器] Phase D PICK: {[t['name'] for t in pick_tasks]}", flush=True)
    for task in task_list:
        if task["human_needed"] or task.get("fixed_start_time"): continue
        dest = task["location_id"]
        if dest not in bg_finish_map: continue

        mode, dist = _get_route(cur_loc, dest, spatial_matrix)
        t_min = _travel_min(mode, dist)
        push("MOVE", cur_min, target_loc=cur_loc, next_loc=dest,
             task_id=task["task_id"], memo=f"前往取回 {task['name']}")
        cur_min += t_min
        if cur_min < bg_finish_map[dest]:
            push("WAIT", cur_min, target_loc=dest, memo=f"等待{task['name']}后台处理完成")
            cur_min = bg_finish_map[dest]
        push("PICK_TASK", cur_min, target_loc=dest, task_id=task["task_id"], memo=f"取回{task['name']}")
        cur_min += DROP_PICK_DURATION
        cur_loc = dest

    return {
        "status": "SUCCESS",
        "suggested_departure_time": _fmt(current_min),
        "total_duration_minutes": cur_min - current_min,
        "timeline": timeline
    }
