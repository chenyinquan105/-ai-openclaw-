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

app = Flask(__name__, static_folder=base_dir)
CORS(app)

# ======================================================================
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
        "content": "你是一个生活秘书。第一步必须调用 search_poi 搜索各品类商户。品类映射规则：理发/美发/沙宣→hair，宠物/狗/猫/洗澡/宠物店→pet，咖啡→cafe，健身→gym，餐饮/吃饭/餐厅→restaurant，电影/影院→cinema，洗衣/干洗→laundry。"
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

    # 判断用户是否已提时间
    has_time = bool(re.search(
        r"\d{1,2}[：:时点]|\d{1,2}:\d{2}|上午\d|下午\d|明天.*\d|周[一二三四五六日天].*\d|星期.*\d",
        text
    ))
    session_state["has_time_from_input"] = has_time

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

    has_now = bool(re.search(r"现在|立即|马上|当前|立刻|现在就出发|默认", time_desc_full.lower()))
    has_specific = bool(re.search(
        r"\d{1,2}[：:时点]|\d{1,2}:\d{2}|上午\d|下午\d|明天.*\d|周[一二三四五六日天].*\d|星期.*\d",
        time_desc_full
    ))

    fixed_time = None
    time_mode = "now"
    if has_specific and not has_now:
        m = re.search(r"(\d{1,2})[：:时点](\d{0,2})", time_desc_full)
        if m:
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            fixed_time = f"{h:02d}:{mi:02d}"
            time_mode = "fixed"

    session_state["fixed_time"] = fixed_time
    session_state["time_mode"] = time_mode

    # 构建排程输入
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
        task_list.append({
            "task_id": sid,
            "name": sname,
            "location_id": sid,
            "duration_minutes": _duration(cat),
            "human_needed": human_needed,
            "fixed_start_time": fixed_time if time_mode == "fixed" else None,
            "category": cat,
        })
        spatial_matrix["locations"][sid] = {"name": sname, "coord": coord}

    session_state["task_list"] = task_list
    session_state["spatial_matrix"] = spatial_matrix
    session_state["now_str"] = datetime.now().strftime("%H:%M")
    session_state["confirmed_ids"] = []
    session_state["rejected_ids"] = []

    # 立即执行排程
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

        # 调用防踩坑 Skill
        from skills import destination_anti_pitfall as skill_pitfall
        pitfall_input = {
            "trip_id": f"trip_{int(datetime.now().timestamp())}",
            "current_node_index": 0,
            "pipeline_nodes": [],
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
# 启动
# ======================================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"🚀 美团 AI 助手服务启动: http://localhost:{port}")
    _reset_session()
    app.run(host="0.0.0.0", port=port, debug=True)
