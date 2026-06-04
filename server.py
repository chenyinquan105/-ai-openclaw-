"""
server.py —— HTTP 桥梁，不修改 main.py 一行代码
====================================================

将 CLI 的 input()/print() 交互替换为 HTTP 请求/响应。
状态保存在 MeituanAgent 实例中，每次请求推进一个阶段。
"""

import json
import os
import re
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# ======================================================================
# import 后端（不修改 main.py）
# ======================================================================
base_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, base_dir)
import main as backend

from skills import time_master
import skills.task_reminder_skill as reminder_skill

app = Flask(__name__, static_folder=base_dir)
CORS(app)

# ======================================================================
# 虚拟时钟全局 session_id
# ======================================================================
_CLOCK_SESSION_ID = "sandbox_main"
# 全局状态 —— 每个 session 一个 agent 实例
# ======================================================================
# 当前只支持单会话（一个用户）
agent = None
session_state = {
    "phase": None,          # "init" | "choose_shop" | "ask_time" | "schedule" | "conflict" | "done"
    "searched_categories": [],
    "selected_pairs": [],   # [(category, shop_id, shop_name), ...]
    "fixed_time": None,
    "time_mode": "now",
    "conflict_task": None,
    "task_list": [],
    "spatial_matrix": {},
    "now_str": "",
    "confirmed_ids": [],
    "rejected_ids": [],
    "user_input": "",
    "time_desc": "",
    "has_time_from_input": False,
}


def _reset_session():
    global agent, session_state
    agent = backend.MeituanAgent(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com"
    )
    session_state = {
        "phase": "init",
        "searched_categories": [],
        "selected_pairs": [],
        "fixed_time": None,
        "time_mode": "now",
        "conflict_task": None,
        "task_list": [],
        "spatial_matrix": {},
        "now_str": "",
        "confirmed_ids": [],
        "rejected_ids": [],
        "user_input": "",
        "time_desc": "",
        "has_time_from_input": False,
    }


def _duration(cat: str) -> int:
    return {"hair": 60, "pet": 30, "cafe": 20,
            "restaurant": 60, "gym": 60, "cinema": 120, "laundry": 30}.get(cat, 45)


def _search_poi(agent_instance, user_text: str) -> dict:
    """执行 LLM 解析 + POI 搜索，返回 (category_list, poi_data) 或错误"""
    system_prompt_1 = {
        "role": "system",
        "content": "你是一个生活秘书。第一步必须调用 search_poi 搜索各品类商户。品类映射规则：理发/美发/沙宣→hair，宠物/狗/猫/洗澡/宠物店→pet，咖啡→cafe，健身→gym，餐饮/吃饭/餐厅→restaurant，电影/影院→cinema，洗衣/干洗→laundry，火锅/海底捞/吃火锅→hotpot。"
    }
    agent_instance.context_memory = [system_prompt_1, {"role": "user", "content": user_text}]

    tools_poi = [{
        "type": "function",
        "function": {
            "name": "search_poi",
            "parameters": {
                "type": "object",
                "properties": {
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "center_coord": {"type": "string"},
                    "radius_meters": {"type": "integer"},
                    "min_rating": {"type": "number"}
                },
                "required": ["categories"]
            }
        }
    }]

    msg = agent_instance._call_llm(agent_instance.context_memory, tools=tools_poi)

    retry_p1 = 0
    while not msg.tool_calls and retry_p1 < 5:
        retry_p1 += 1
        agent_instance.context_memory.append({"role": "assistant", "content": msg.content or ""})
        agent_instance.context_memory.append({"role": "user", "content": "请调用 search_poi 工具搜索对应品类商户，不要用文字回答。"})
        msg = agent_instance._call_llm(agent_instance.context_memory, tools=tools_poi)

    if not msg.tool_calls:
        return {"error": "LLM 未调用搜索工具。"}

    agent_instance.context_memory.append(msg)

    all_results = {}
    for tool_call in msg.tool_calls:
        args = json.loads(tool_call.function.arguments)
        raw_cats = args.get("categories", [])
        mapped_cats = list(set([backend.CATEGORY_MAP.get(c, c) for c in raw_cats]))

        search_res = backend.skill_poi.search_poi_matrix(
            center_coord=args.get("center_coord", "39.93,116.45"),
            categories=mapped_cats,
            radius_meters=args.get("radius_meters", 3000),
            min_rating=args.get("min_rating", 0)
        )

        if search_res.get("status") == "SUCCESS":
            for cat in search_res["search_results"]:
                if cat not in all_results:
                    all_results[cat] = []
                all_results[cat].extend(search_res["search_results"][cat])
                for shop in search_res["search_results"][cat]:
                    agent_instance.poi_cache[shop["shop_id"]] = shop

        agent_instance.context_memory.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(search_res)
        })

    # 按品类分组
    agent_instance.poi_cache_per_category = {}
    for sid, shop in agent_instance.poi_cache.items():
        cat = shop.get("category")
        agent_instance.poi_cache_per_category.setdefault(cat, []).append(shop)

    return {"categories": list(all_results.keys()), "results": all_results}


def _build_categories_for_frontend(agent_instance) -> list:
    """将 poi_cache_per_category 转为前端需要的格式（top3 + 评分）"""
    result = []
    for cat, shops in agent_instance.poi_cache_per_category.items():
        sorted_shops = sorted(shops, key=lambda s: s.get("rating", 0), reverse=True)
        top_n = sorted_shops[:3]
        shops_data = []
        for s in top_n:
            # 计算到起点的距离
            dist_m = 0
            raw_coord = s.get("coord", "")
            if raw_coord and "," in raw_coord:
                try:
                    slat, slng = float(raw_coord.split(",")[0].strip()), float(raw_coord.split(",")[1].strip())
                    from math import radians, cos, sin, asin, sqrt
                    R = 6371000
                    dlat = radians(slat - 39.93)
                    dlng = radians(slng - 116.45)
                    a = sin(dlat/2)**2 + cos(radians(39.93))*cos(radians(slat))*sin(dlng/2)**2
                    c = 2 * asin(sqrt(a))
                    dist_m = int(R * c)
                except:
                    pass
            dist_str = f"{dist_m}m" if dist_m < 1000 else f"{dist_m/1000:.1f}km"
            shops_data.append({
                "shop_id": s["shop_id"],
                "name": s["name"],
                "rating": s.get("rating", 0),
                "distance": dist_str,
                "human_needed": s.get("human_needed", True),
                "phone": s.get("phone", ""),
                "address": s.get("address", ""),
                "signature_dishes": s.get("signature_dishes", []),
                "top_comments": s.get("top_comments", []),
            })
        result.append({
            "category": cat,
            "label": backend.CATEGORY_NAME_CN.get(cat, cat),
            "shops": shops_data
        })
    return result


# ======================================================================
# API 路由
# ======================================================================

@app.route("/")
def index():
    return send_from_directory(base_dir, "index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    """阶段 1: 接收用户文字 → LLM解析 → POI搜索 → 返回品类+店铺列表给前端选择"""
    global agent, session_state
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "请输入需求"}), 400

    _reset_session()
    agent.context_memory = []
    session_state["user_input"] = text

    result = _search_poi(agent, text)
    if "error" in result:
        return jsonify({"error": result["error"]}), 500

    categories = _build_categories_for_frontend(agent)
    if not categories:
        return jsonify({"error": "未搜索到任何商户"}), 404

    session_state["searched_categories"] = result["categories"]
    session_state["phase"] = "choose_shop"

    # 判断用户是否已提时间，若已提则语义解析出发/到达
    # 先把中文数字归一化：“两点”→“2点”，“二点半”→“2点30”
    text_norm = text.replace('两', '2').replace('二', '2').replace('点半', '点30').replace('半', '30')
    has_time = bool(re.search(
        r"\d{1,2}[：:时点]|\d{1,2}:\d{2}|上午\d|下午\d|明天.*\d|周[一二三四五六日天].*\d|星期.*\d",
        text_norm
    ))
    session_state["has_time_from_input"] = has_time
    fixed_time = None
    time_mode = "now"
    if has_time:
        m = re.search(r"(\d{1,2})[：:时点](\d{0,2})", text_norm)
        if m:
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            # 没有明确上午/下午标记，且 h<=5 → 视为下午
            has_am = bool(re.search(r'早上|早晨|上午|早[上晨]', text))
            has_pm = bool(re.search(r'下午|晚上|傍晚|今晚|午[后饭]|下[午晚]', text))
            if has_am and h == 12:
                h = 0
            elif not has_am and not has_pm and h <= 5:
                h += 12
            elif has_pm and h < 12:
                h += 12
            fixed_time = f"{h:02d}:{mi:02d}"
        # 判断是"几点出发"还是"几点到达"
        # 含"出发/开始走/启程/走"等 → 出发时间
        # 否则默认为到达时间（要去做什么/到什么地方）
        if re.search(r"出发|开始走|启程|开始|就走|就走|再走|从.*走", text):
            time_mode = "fixed"
        else:
            time_mode = "arrive_by"
    session_state["fixed_time"] = fixed_time
    session_state["time_mode"] = time_mode

    return jsonify({
        "phase": "choose_shop",
        "categories": categories
    })


@app.route("/api/choose_shop", methods=["POST"])
def api_choose_shop():
    """阶段 2: 用户选好店 → 存状态"""
    global session_state
    data = request.get_json(silent=True) or {}
    selections = data.get("selections", [])  # [{category, shop_id}]

    if not selections:
        return jsonify({"error": "请至少选择一个店铺"}), 400

    selected_pairs = []
    for sel in selections:
        cat = sel.get("category")
        sid = sel.get("shop_id")
        if cat and sid and sid in agent.poi_cache:
            shop_info = agent.poi_cache[sid]
            selected_pairs.append((cat, sid, shop_info["name"]))

    if not selected_pairs:
        return jsonify({"error": "选中的店铺无效"}), 400

    session_state["selected_pairs"] = selected_pairs
    session_state["transport"] = data.get("transport", "步行")

    # 若用户最开始已提时间，直接跑排程，跳过时间输入
    if session_state.get("fixed_time"):
        session_state["phase"] = "running"
        return _run_schedule_from_session()

    session_state["phase"] = "ask_time"
    return jsonify({
        "phase": "ask_time",
        "has_time_in_input": session_state["has_time_from_input"]
    })


@app.route("/api/set_time", methods=["POST"])
def api_set_time():
    """阶段 3: 用户提供时间 → 解析 → 执行排程"""
    global session_state
    data = request.get_json(silent=True) or {}
    time_text = (data.get("time") or "").strip()

    user_input = session_state["user_input"]
    time_desc_full = user_input + " " + time_text
    # 中文数字归一化
    td_norm = time_desc_full.replace('两', '2').replace('二', '2').replace('点半', '点30').replace('半', '30')

    has_now = bool(re.search(r"现在|立即|马上|当前|立刻|现在就出发|默认", time_desc_full.lower()))
    has_specific = bool(re.search(
        r"\d{1,2}[：:时点]|\d{1,2}:\d{2}|上午\d|下午\d|明天.*\d|周[一二三四五六日天].*\d|星期.*\d",
        td_norm
    ))

    fixed_time = None
    time_mode = "now"
    if has_specific and not has_now:
        m = re.search(r"(\d{1,2})[：:时点](\d{0,2})", td_norm)
        if m:
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            # 没有明确上午/下午标记，且 h<=5 → 视为下午
            has_am = bool(re.search(r'早上|早晨|上午|早[上晨]', time_desc_full))
            has_pm = bool(re.search(r'下午|晚上|傍晚|今晚|午[后饭]|下[午晚]', time_desc_full))
            if has_am and h == 12:
                h = 0
            elif not has_am and not has_pm and h <= 5:
                h += 12
            elif has_pm and h < 12:
                h += 12
            fixed_time = f"{h:02d}:{mi:02d}"
            time_mode = "fixed"

    session_state["fixed_time"] = fixed_time
    session_state["time_mode"] = time_mode

    # 构建排程输入
    return _run_schedule_from_session()


def _run_schedule_from_session():
    """从 session_state 构建排程输入并执行"""
    global session_state
    fixed_time = session_state.get("fixed_time")
    time_mode = session_state.get("time_mode", "now")

    task_list = []
    spatial_matrix = {
        "locations": {"loc_current": {"name": "当前起点", "coord": "39.93,116.45"}},
        "routes": {}
    }

    for cat, sid, sname in session_state["selected_pairs"]:
        info = agent.poi_cache.get(sid, {})
        raw = info.get('coord', '')
        if raw and ',' in raw:
            coord = raw
        else:
            coord = "39.93,116.45"
        human_needed = info.get("human_needed", True)
        # time_mode:
        #   "fixed" → 用户说几点出发，该时间就是出发时间，不设 fixed_start_time
        #   "arrive_by" → 用户说几点到店，该时间就是到店时间，设 fixed_start_time
        #   "now" → 没提时间，即出发
        task_list.append({
            "task_id": sid,
            "name": sname,
            "location_id": sid,
            "duration_minutes": _duration(cat),
            "human_needed": human_needed,
            "fixed_start_time": fixed_time if time_mode == "arrive_by" else None,
            "category": cat,
        })
        spatial_matrix["locations"][sid] = {"name": sname, "coord": coord}

    session_state["task_list"] = task_list
    session_state["spatial_matrix"] = spatial_matrix
    # 若用户说几点出发，now_str 设为该时间（引擎从该时间开始行走）
    if time_mode == "fixed":
        session_state["now_str"] = fixed_time
    else:
        # 虚拟时间开启时优先使用虚拟时钟当前时间
        _tm = time_master.get_master()
        _cs = _tm.get_session(_CLOCK_SESSION_ID)
        if _cs and _cs.virtual_time:
            session_state["now_str"] = _cs.virtual_time
        else:
            session_state["now_str"] = datetime.now().strftime("%H:%M")
    session_state["confirmed_ids"] = []
    session_state["rejected_ids"] = []

    return _run_schedule()


def _run_schedule():
    """执行一次排程，处理 CONFIRM_REQUIRED / SUCCESS / 其他"""
    global session_state
    schedule_res = backend.skill_scheduler.solve_concurrent_timeline(
        session_state["task_list"],
        session_state["spatial_matrix"],
        session_state["now_str"],
        session_state["confirmed_ids"],
        session_state["rejected_ids"],
    )

    if schedule_res.get("status") == "CONFIRM_REQUIRED":
        session_state["phase"] = "conflict"
        session_state["conflict_task"] = schedule_res["conflict_task"]
        return jsonify({
            "phase": "conflict",
            "message": schedule_res["message"],
            "delay_minutes": schedule_res.get("delay_minutes"),
            "conflict_task": {
                "task_id": schedule_res["conflict_task"]["task_id"],
                "name": session_state["conflict_task"]["name"],
            }
        })

    elif schedule_res.get("status") == "SUCCESS":
        session_state["phase"] = "done"
        # 修正时间线：MOVE 条目的时间改为后续 DROP/START 的时间，删除后续重复条目
        raw = schedule_res["timeline"]
        # 构建 task_id → {name, duration_minutes, human_needed} 映射
        task_map = {}
        for t in session_state["task_list"]:
            tid = t.get("task_id")
            if tid:
                cat = t.get("category", "")
                task_map[tid] = {
                    "name": t.get("name", ""),
                    "duration_minutes": t.get("duration_minutes", 45),
                    "human_needed": t.get("human_needed", True),
                    "action_name": backend.CATEGORY_NAME_CN.get(cat, t.get("name", "")),
                }
        cleaned = []
        skip = set()
        for i in range(len(raw)):
            if i in skip:
                continue
            item = raw[i]
            if item["action"] == "MOVE" and i + 1 < len(raw):
                nxt = raw[i + 1]
                if nxt["action"] in ("DROP_TASK", "START_TASK", "PICK_TASK"):
                    # 保留 MOVE 条目，时间改为到达时间，memo 添加执行内容
                    act_label = {"DROP_TASK": "放下", "START_TASK": "开始", "PICK_TASK": "回收"}
                    tag = act_label.get(nxt["action"], "")
                    item["time"] = nxt["time"]
                    item["memo"] = f"{item['memo']} — {nxt['memo']}"
                    item["action"] = "MOVE_AND_EXEC"
                    skip.add(i + 1)
            # 提取 task_id 对应的子任务信息
            sub_task_info = None
            tid = item.get("task_id")
            if tid and tid in task_map:
                info = task_map[tid]
                if info["human_needed"]:
                    # 只在 MOVE 或 MOVE_AND_EXEC 条目上附加子任务行
                    if item["action"] in ("MOVE", "MOVE_AND_EXEC"):
                        sub_task_info = {
                            "action": info["action_name"],
                            "duration_minutes": info["duration_minutes"],
                        }
            # 清洗 memo：将 "前往锚点: xxx" 统一转为 "前往 xxx"
            memo = item["memo"]
            memo = re.sub(r'^前往锚点:\s*', '前往 ', memo)
            cleaned.append({
                "time": item["time"],
                "memo": memo,
                "action": item["action"],
                "sub_task": sub_task_info,
            })

        # 注册到虚拟时钟（如果已开启）
        _tm = time_master.get_master()
        _cs = _tm.get_session(_CLOCK_SESSION_ID)
        if _cs:
            _schedule_nodes = []
            for item in schedule_res["timeline"]:
                _schedule_nodes.append({
                    "time": item["time"],
                    "type": "SCHEDULE",
                    "node_id": item.get("task_id", ""),
                    "name": item.get("memo", ""),
                    "action": item.get("action", ""),
                    "target_location_id": item.get("target_location_id"),
                })
            # 日常提醒
            _schedule_nodes.append({"time": "10:00", "type": "WATER", "id": "wat_1", "name": "喝水提醒"})
            _schedule_nodes.append({"time": "15:00", "type": "WATER", "id": "wat_2", "name": "喝水提醒"})
            _schedule_nodes.append({"time": "08:30", "type": "MED", "id": "med_hypertension", "name": "高血压阿司匹林"})
            _tm.set_schedule(_CLOCK_SESSION_ID, _schedule_nodes)

        # 调用防踩坑 Skill
        from skills import destination_anti_pitfall as skill_pitfall
        pitfall_input = {
            "trip_id": f"trip_{int(datetime.now().timestamp())}",
            "current_node_index": 0,
            "pipeline_nodes": [],
            "transport": session_state.get("transport", "步行"),
            "environmental_context": {
                "timestamp": int(datetime.now().timestamp()),
                "weather_summary": "今日多云，傍晚空气湿度较大，体感闷热",
                "client_platform": "WECHAT"
            }
        }
        for cat, sid, sname in session_state["selected_pairs"]:
            info = agent.poi_cache.get(sid, {})
            pitfall_input["pipeline_nodes"].append({
                "node_id": sid,
                "node_name": sname,
                "category": cat,
                "coordinate": info.get("coord", "39.93,116.45")
            })
        pitfall_output = skill_pitfall.execute_anti_pitfall_skill(
            input_payload=pitfall_input
        )
        pending_triggers = skill_pitfall.get_pending_triggers(pitfall_output)
        # 保存到 session_state 供反射 API 使用
        session_state["pitfall_global_reminders"] = pitfall_output.get("global_reminders", [])
        session_state["pitfall_localized_insights"] = pitfall_output.get("localized_insights", [])
        session_state["pitfall_intent_triggers"] = pending_triggers

        return jsonify({
            "phase": "done",
            "departure_time": schedule_res["suggested_departure_time"],
            "total_minutes": schedule_res["total_duration_minutes"],
            "timeline": cleaned,
            "pitfall_reminders": pitfall_output.get("global_reminders", []),
            "pitfall_insights": pitfall_output.get("localized_insights", []),
            "pitfall_triggers": pending_triggers,
        })

    else:
        session_state["phase"] = "error"
        return jsonify({
            "phase": "error",
            "message": schedule_res.get("message", "排程失败")
        })


@app.route("/api/conflict_choice", methods=["POST"])
def api_conflict_choice():
    """处理冲突确认：接受或延后"""
    global session_state
    data = request.get_json(silent=True) or {}
    choice = data.get("choice")  # "accept" | "postpone"

    if not session_state.get("conflict_task"):
        return jsonify({"error": "无待处理的冲突"}), 400

    if choice == "accept":
        session_state["confirmed_ids"].append(session_state["conflict_task"]["task_id"])
    else:
        session_state["rejected_ids"].append(session_state["conflict_task"]["task_id"])

    session_state["conflict_task"] = None
    return _run_schedule()


@app.route("/api/reset", methods=["POST"])
def api_reset():
    _reset_session()
    return jsonify({"phase": "init"})


@app.route("/api/reflect_trigger", methods=["POST"])
def api_reflect_trigger():
    """前端用户点击 intent_trigger 按钮后，执行反射动作"""
    from skills import destination_anti_pitfall as skill_pitfall
    data = request.get_json(silent=True) or {}
    trigger_id = data.get("trigger_id")
    if not trigger_id:
        return jsonify({"error": "缺少 trigger_id"}), 400

    triggers = session_state.get("pitfall_intent_triggers", [])
    target = None
    for t in triggers:
        if t.get("trigger_id") == trigger_id:
            target = t
            break

    if not target:
        return jsonify({"error": "未找到对应 trigger"}), 404

    result = skill_pitfall.dispatch_reflection(target)
    return jsonify(result)


# ======================================================================
# Plan B 二级弹窗相关 API
# ======================================================================

@app.route("/api/insert_shelter", methods=["POST"])
def api_insert_shelter():
    """
    下暴雨避雨：检索附近 cafe 品类店铺，插入行程第一个目的地后重算排程。
    """
    if not session_state.get("task_list"):
        return jsonify({"error": "无行程数据"}), 400

    # 从 poi_cache 中找 cafe 品类最近店铺
    cafe_shop_id = None
    cafe_name = None
    for sid, shop in agent.poi_cache.items():
        if shop.get("category") == "cafe":
            cafe_shop_id = sid
            cafe_name = shop.get("name", "附近饮品店")
            break

    if not cafe_shop_id:
        # 找不到 cafe，尝试其他饮品类兜底
        for sid, shop in agent.poi_cache.items():
            cat = shop.get("category", "")
            if cat in ("cafe",):
                cafe_shop_id = sid
                cafe_name = shop.get("name", "附近店铺")
                break

    if not cafe_shop_id:
        return jsonify({"error": "未找到附近的避雨店铺"}), 404

    # 构造避雨节点
    shelter_info = agent.poi_cache[cafe_shop_id]
    raw_coord = shelter_info.get('coord', '')
    if raw_coord and ',' in raw_coord:
        coord = raw_coord
    else:
        coord = "39.93,116.45"

    shelter_task = {
        "task_id": cafe_shop_id,
        "name": cafe_name,
        "location_id": cafe_shop_id,
        "duration_minutes": 20,  # 歇脚20分钟
        "human_needed": True,
        "fixed_start_time": None,
        "category": "cafe",
    }

    # 插入 task_list 第0位
    task_list = session_state["task_list"]
    task_list.insert(0, shelter_task)
    session_state["task_list"] = task_list

    # 更新 spatial_matrix
    session_state["spatial_matrix"]["locations"][cafe_shop_id] = {
        "name": cafe_name,
        "coord": coord
    }

    # 清除之前的排程缓存，重算
    session_state["confirmed_ids"] = []
    session_state["rejected_ids"] = []

    schedule_res = backend.skill_scheduler.solve_concurrent_timeline(
        task_list,
        session_state["spatial_matrix"],
        session_state["now_str"],
        session_state["confirmed_ids"],
        session_state["rejected_ids"],
    )

    if schedule_res.get("status") == "SUCCESS":
        # 防踩坑走一遍（简化）
        cleaned_timeline = []
        for item in schedule_res["timeline"]:
            memo = item.get("memo", "")
            sub = None
            if item["task_id"]:
                for t in task_list:
                    if t["task_id"] == item["task_id"]:
                        sub = {"action": t["name"], "duration_minutes": t["duration_minutes"]}
                        break
            cleaned_timeline.append({
                "time": item["time"],
                "memo": memo,
                "action": item["action"],
                "sub_task": sub,
            })
        return jsonify({
            "phase": "done",
            "shelter_name": cafe_name,
            "shelter_id": cafe_shop_id,
            "departure_time": schedule_res["suggested_departure_time"],
            "total_minutes": schedule_res["total_duration_minutes"],
            "timeline": cleaned_timeline,
            "pitfall_reminders": [],
            "pitfall_insights": [],
            "pitfall_triggers": [],
        })
        # 注册到虚拟时钟（如果已开启）
        _tm = time_master.get_master()
        _cs = _tm.get_session(_CLOCK_SESSION_ID)
        if _cs:
            _sn = []
            for item in schedule_res["timeline"]:
                _sn.append({"time": item["time"], "type": "SCHEDULE", "node_id": item.get("task_id",""), "name": item.get("memo",""), "action": item.get("action","")})
            _sn.append({"time": "10:00", "type": "WATER", "id": "wat_1", "name": "喝水提醒"})
            _sn.append({"time": "15:00", "type": "WATER", "id": "wat_2", "name": "喝水提醒"})
            _sn.append({"time": "08:30", "type": "MED", "id": "med_hypertension", "name": "高血压阿司匹林"})
            _tm.set_schedule(_CLOCK_SESSION_ID, _sn)
    else:
        return jsonify({
            "phase": "inserted",
            "shelter_name": cafe_name,
            "shelter_id": cafe_shop_id,
            "message": schedule_res.get("message", "避雨点已加入，但排程需要进一步确认")
        })


@app.route("/api/get_swap_candidates", methods=["POST"])
def api_get_swap_candidates():
    """
    获取可替换的同品类店铺列表（排除异常店）。
    输入: { anomaly_type: "排号异常" | "餐厅停电" }
    """
    data = request.get_json(silent=True) or {}
    anomaly_type = data.get("anomaly_type", "")

    selected_pairs = session_state.get("selected_pairs", [])
    if not selected_pairs:
        return jsonify({"error": "无已选店铺"}), 400

    # 找到需要被替换的节点（第一个与异常匹配的品类）
    # 排号异常/停电通常对应 restaurant 品类
    target_category = None
    excluded_shop_id = None
    for cat, sid, sname in selected_pairs:
        if cat in ("restaurant",):
            target_category = cat
            excluded_shop_id = sid
            break

    if not target_category:
        # 兜底：尝试用第一个品类
        cat, sid, sname = selected_pairs[0]
        target_category = cat
        excluded_shop_id = sid

    # 从 poi_cache_per_category 获取同品类所有店铺并过滤
    shops_data = []
    shops = agent.poi_cache_per_category.get(target_category, [])
    for shop in shops:
        if shop["shop_id"] == excluded_shop_id:
            continue
        # 计算距离
        dist_m = 0
        raw_coord = shop.get("coord", "")
        if raw_coord and "," in raw_coord:
            try:
                slat, slng = float(raw_coord.split(",")[0].strip()), float(raw_coord.split(",")[1].strip())
                from math import radians, cos, sin, asin, sqrt
                R = 6371000
                dlat = radians(slat - 39.93)
                dlng = radians(slng - 116.45)
                a = sin(dlat/2)**2 + cos(radians(39.93))*cos(radians(slat))*sin(dlng/2)**2
                c = 2 * asin(sqrt(a))
                dist_m = int(R * c)
            except:
                pass
        dist_str = f"{dist_m}m" if dist_m < 1000 else f"{dist_m/1000:.1f}km"
        shops_data.append({
            "shop_id": shop["shop_id"],
            "name": shop["name"],
            "rating": shop.get("rating", 0),
            "distance": dist_str,
        })

    # 按评分降序
    shops_data.sort(key=lambda s: s["rating"], reverse=True)

    return jsonify({
        "category": target_category,
        "shops": shops_data[:5]
    })


@app.route("/api/shop_detail", methods=["POST"])
def api_shop_detail():
    """店铺详情：返回 phone/address/signature_dishes/top_comments"""
    global session_state
    data = request.get_json(silent=True) or {}
    shop_id = data.get("shop_id", "")
    if not shop_id:
        return jsonify({"error": "缺少 shop_id"}), 400
    info = agent.poi_cache.get(shop_id, {})
    if not info:
        return jsonify({"error": "未找到该店铺"}), 404
    return jsonify({
        "shop_id": shop_id,
        "name": info.get("name", ""),
        "rating": info.get("rating", 0),
        "phone": info.get("phone", ""),
        "address": info.get("address", ""),
        "signature_dishes": info.get("signature_dishes", []),
        "top_comments": info.get("top_comments", []),
    })


@app.route("/api/swap_shop", methods=["POST"])
def api_swap_shop():
    """
    替换店铺后重算排程。
    输入: { new_shop_id: "...", is_queue: bool }
    """
    data = request.get_json(silent=True) or {}
    new_shop_id = data.get("new_shop_id")
    is_queue = data.get("is_queue", False)

    if not new_shop_id or new_shop_id not in agent.poi_cache:
        return jsonify({"error": "无效的店铺 ID"}), 400

    # 更新 selected_pairs 中对应条目
    new_shop = agent.poi_cache[new_shop_id]
    new_category = new_shop.get("category", "")
    new_name = new_shop.get("name", "")

    selected_pairs = session_state.get("selected_pairs", [])
    updated = False
    for i, (cat, sid, sname) in enumerate(selected_pairs):
        # 匹配品类后替换
        if cat == new_category or (not updated):
            selected_pairs[i] = (new_category, new_shop_id, new_name)
            updated = True
            break
    if not updated:
        selected_pairs.append((new_category, new_shop_id, new_name))
    session_state["selected_pairs"] = selected_pairs

    # 更新 task_list
    raw = new_shop.get('coord', '')
    coord = raw if raw and ',' in raw else "39.93,116.45"

    def _duration(cat):
        return {"hair": 60, "pet": 30, "cafe": 20,
                "restaurant": 60, "gym": 60, "cinema": 120, "laundry": 30}.get(cat, 45)

    new_task = {
        "task_id": new_shop_id,
        "name": new_name,
        "location_id": new_shop_id,
        "duration_minutes": _duration(new_category),
        "human_needed": new_shop.get("human_needed", True),
        "fixed_start_time": session_state.get("fixed_time"),
        "category": new_category,
    }
    session_state["spatial_matrix"]["locations"][new_shop_id] = {
        "name": new_name,
        "coord": coord
    }
    task_list = session_state["task_list"]
    # 替换掉品类相同的旧 task
    replaced = False
    for i, t in enumerate(task_list):
        if t.get("category") == new_category or (not replaced):
            task_list[i] = new_task
            replaced = True
            break
    if not replaced:
        task_list.append(new_task)
    session_state["task_list"] = task_list

    # 重算排程
    session_state["confirmed_ids"] = []
    session_state["rejected_ids"] = []
    schedule_res = backend.skill_scheduler.solve_concurrent_timeline(
        task_list,
        session_state["spatial_matrix"],
        session_state["now_str"],
        session_state["confirmed_ids"],
        session_state["rejected_ids"],
    )

    if schedule_res.get("status") == "SUCCESS":
        cleaned = []
        for item in schedule_res["timeline"]:
            memo = item.get("memo", "")
            sub = None
            if item["task_id"]:
                for t in task_list:
                    if t["task_id"] == item["task_id"]:
                        sub = {"action": t["name"], "duration_minutes": t["duration_minutes"]}
                        break
            cleaned.append({
                "time": item["time"],
                "memo": memo,
                "action": item["action"],
                "sub_task": sub,
            })
        return jsonify({
            "phase": "done",
            "departure_time": schedule_res["suggested_departure_time"],
            "total_minutes": schedule_res["total_duration_minutes"],
            "timeline": cleaned,
            "pitfall_reminders": [],
            "pitfall_insights": [],
            "pitfall_triggers": [],
        })
        # 注册到虚拟时钟（如果已开启）
        _tm = time_master.get_master()
        _cs = _tm.get_session(_CLOCK_SESSION_ID)
        if _cs:
            _sn = []
            for item in schedule_res["timeline"]:
                _sn.append({"time": item["time"], "type": "SCHEDULE", "node_id": item.get("task_id",""), "name": item.get("memo",""), "action": item.get("action","")})
            _sn.append({"time": "10:00", "type": "WATER", "id": "wat_1", "name": "喝水提醒"})
            _sn.append({"time": "15:00", "type": "WATER", "id": "wat_2", "name": "喝水提醒"})
            _sn.append({"time": "08:30", "type": "MED", "id": "med_hypertension", "name": "高血压阿司匹林"})
            _tm.set_schedule(_CLOCK_SESSION_ID, _sn)
    else:
        return jsonify({
            "phase": "swapped",
            "message": schedule_res.get("message", "店铺已替换，但排程需要进一步确认")
        })


# ======================================================================
# 虚拟时钟 API
# ======================================================================

@app.route("/api/clock/init", methods=["POST"])
def clock_init():
    """初始化或重置虚拟时钟，接收 schedule_nodes JSON"""
    data = request.get_json() or {}
    tm = time_master.get_master()
    initial_time = data.get("initial_time", "08:00")
    nodes = data.get("schedule_nodes", [])
    tm.set_schedule(_CLOCK_SESSION_ID, nodes, initial_time=initial_time)
    clock = tm.get_or_create_session(_CLOCK_SESSION_ID, initial_time=initial_time)
    return jsonify(clock.to_dict())


@app.route("/api/clock/status", methods=["GET"])
def clock_status():
    """获取当前时钟状态"""
    tm = time_master.get_master()
    cs = tm.get_session(_CLOCK_SESSION_ID)
    if not cs:
        return jsonify({"virtual_time": None, "speed": 0, "is_running": False, "schedule_count": 0})
    d = cs.to_dict()
    d["schedule_count"] = len(cs.schedule_nodes)
    return jsonify(d)


@app.route("/api/clock/offset", methods=["POST"])
def clock_offset():
    """快进 N 分钟"""
    data = request.get_json() or {}
    delta = data.get("delta", 10)
    tm = time_master.get_master()
    res = tm.offset(_CLOCK_SESSION_ID, int(delta))
    _process_clock_triggers(res)
    return jsonify(res)


@app.route("/api/clock/jump", methods=["POST"])
def clock_jump():
    """跳转到指定时间"""
    data = request.get_json() or {}
    target = data.get("target", "14:00")
    tm = time_master.get_master()
    res = tm.jump(_CLOCK_SESSION_ID, target)
    _process_clock_triggers(res)
    return jsonify(res)


@app.route("/api/clock/speed", methods=["POST"])
def clock_set_speed():
    """设置倍速（只记倍速，不启动走时）: speed=30|60|120"""
    data = request.get_json() or {}
    speed_val = float(data.get("speed", 30))
    tm = time_master.get_master()
    res = tm.set_speed(_CLOCK_SESSION_ID, speed_val)
    cs = tm.get_session(_CLOCK_SESSION_ID)
    return jsonify({
        "status": res.get("status", "SUCCESS"),
        "speed": speed_val,
        "virtual_time": res.get("new_virtual_time", cs.virtual_time if cs else "12:00"),
        "is_running": cs.is_running if cs else False,
    })


@app.route("/api/clock/start", methods=["POST"])
def clock_start():
    """启动/继续自动走时（以当前记录的速度启动）"""
    tm = time_master.get_master()
    cs = tm.get_session(_CLOCK_SESSION_ID)
    speed = cs.speed if cs else 1.0
    tm.stop_auto_tick(_CLOCK_SESSION_ID)
    res = tm.start_auto_tick(_CLOCK_SESSION_ID, speed)
    return jsonify({
        "status": res.get("status", "SUCCESS"),
        "speed": speed,
        "virtual_time": res.get("new_virtual_time", cs.virtual_time if cs else "12:00"),
        "is_running": True,
    })


@app.route("/api/clock/stop", methods=["POST"])
def clock_stop():
    """停止自动走时"""
    tm = time_master.get_master()
    tm.stop_auto_tick(_CLOCK_SESSION_ID)
    cs = tm.get_session(_CLOCK_SESSION_ID)
    return jsonify({"status": "STOPPED", "virtual_time": cs.virtual_time if cs else "08:00"})


@app.route("/api/clock/events", methods=["GET"])
def clock_pop_events():
    """消费未读的触发事件"""
    tm = time_master.get_master()
    events = tm.pop_triggered_events(_CLOCK_SESSION_ID)
    cs = tm.get_session(_CLOCK_SESSION_ID)
    return jsonify({
        "events": events,
        "virtual_time": cs.virtual_time if cs else "08:00",
    })


@app.route("/api/clock/set_schedule", methods=["POST"])
def clock_set_schedule():
    """设置排程节点"""
    data = request.get_json() or {}
    nodes = data.get("nodes", [])
    tm = time_master.get_master()
    tm.set_schedule(_CLOCK_SESSION_ID, nodes)
    return jsonify({"status": "SUCCESS", "count": len(nodes)})


def _process_clock_triggers(res: dict):
    """时钟事件产生后，调用 reminder_skill 处理并记录到 session_state"""
    from flask import current_app as app
    ticked = res.get("ticked_minutes_list", [])
    events = res.get("triggered_nodes", [])
    if not ticked and not events:
        return
    alerts = reminder_skill.process_reminder_pipeline(
        _CLOCK_SESSION_ID, ticked, events, time_master.get_master()
    )
    # 将 alert 挂到全局以便前端 /api/clock/events 也能拉到
    # alerts 直接返回给前端（offset/jump 的响应中已带 triggered_nodes）
    # 更深的提醒通知通过 pop_triggered_events 拉取


# ======================================================================
# 启动
# ======================================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"🚀 美团 AI 助手服务启动: http://localhost:{port}")
    _reset_session()
    app.run(host="0.0.0.0", port=port, debug=False)
