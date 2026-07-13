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
import queue
import threading
import time as _time
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS

# ======================================================================
# import 后端（不修改 main.py）
# ======================================================================
base_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, base_dir)
import main as backend

from skills.time_master import time_master as time_master
from skills.task_reminder_skill import task_reminder_skill as reminder_skill
from skills.route_planner.route_planner import plan_route as _skill_route_planner
from skills.queue_monitor.queue_monitor import handle as _skill_queue_monitor
from skills.weather_extractor.weather_extractor import extract_weather as _skill_weather_extractor
from skills.multi_day_scheduler.multi_day_scheduler import solve_multi_day as _skill_multi_day_schedule, CATEGORY_DURATIONS, _l3_loosest_day_backfill
from skills.multi_day_scheduler.hotel_decision import should_switch_hotel, determine_strategy
from skills.multi_day_scheduler.scheduling_penalty import dynamic_fatigue_cost, time_of_day_fatigue_multiplier

app = Flask(__name__, static_folder=os.path.join(base_dir, "static"))
CORS(app)

# ======================================================================
# 全局配置常量
# ======================================================================
_MEMORY_PATH = os.path.join(base_dir, "管家记忆.md")
DEFAULT_CENTER_COORD = "39.93,116.45"  # 北京三里屯，坐标回退时的唯一默认值


def _read_profile() -> dict:
    """从 管家记忆.md 解析四维度偏好，返回结构化字典"""
    defaults = {
        "personal": {
            "elder_name": "",
            "emergency_contact_name": "",
            "emergency_contact_phone": "",
        },
        "taste": {
            "taste_tolerance": "无辣",
            "dietary_restrictions": [],
            "cuisine_preference": [],
        },
        "commute": {
            "walking_tolerance_meters": 800,
            "transport_priority": "步行优先",
            "travel_preference": "公共交通",
        },
        "budget": {
            "price_level": "中端",
            "custom_budget_per_person": "",
            "rating_cutoff": 4.0,
        },
        "lifestyle": {
            "hydration_interval_minutes": 90,
            "medication_schedule": [],
        },
        "custom_reminders": [],
        "itinerary_templates": [],
    }
    if not os.path.exists(_MEMORY_PATH):
        return defaults
    try:
        with open(_MEMORY_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return defaults

    def _prune(v: str) -> str:
        return v.strip().lstrip("-").strip()

    # 逐节解析 Markdown 表格
    for section_name, section_key, field_map in [
        ("个人身份", "personal", {
            "elder_name": "elder_name",
            "emergency_contact_name": "emergency_contact_name",
            "emergency_contact_phone": "emergency_contact_phone",
        }),
        ("口味", "taste", {
            "taste_tolerance": "taste_tolerance",
            "dietary_restrictions": "dietary_restrictions",
            "cuisine_preference": "cuisine_preference",
        }),
        ("通勤", "commute", {
            "walking_tolerance_meters": "walking_tolerance_meters",
            "transport_priority": "transport_priority",
            "travel_preference": "travel_preference",
        }),
        ("预算", "budget", {
            "price_level": "price_level",
            "custom_budget_per_person": "custom_budget_per_person",
            "rating_cutoff": "rating_cutoff",
        }),
        ("健康作息", "lifestyle", {
            "hydration_interval_minutes": "hydration_interval_minutes",
            "medication_schedule": "medication_schedule",
        }),
    ]:
        # 定位到 ## section_name 并以 --- 或文件尾为界
        import re as _re
        pat = rf"## {section_name}\s*\n(.*?)(?=\n## |\Z)"
        m = _re.search(pat, text, _re.DOTALL)
        if not m:
            continue
        section_text = m.group(1)
        for line in section_text.strip().split("\n"):
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) < 2:
                continue
            key, val = cells[0], cells[1]
            if key == "字段":
                continue  # header row
            if key in field_map:
                target = field_map[key]
                if target in ("dietary_restrictions", "cuisine_preference"):
                    defaults[section_key][target] = [
                        x.strip() for x in val.split(",") if x.strip()
                    ]
                elif target == "medication_schedule":
                    items = []
                    for chunk in [x.strip() for x in val.split(",") if x.strip()]:
                        # "08:00:降压药" → time="08:00", name="降压药"
                        parts = chunk.split(":")
                        if len(parts) >= 3:
                            items.append({"time": f"{parts[0]}:{parts[1]}", "name": parts[2]})
                    defaults[section_key][target] = items
                elif target == "walking_tolerance_meters":
                    try:
                        defaults[section_key][target] = int(val)
                    except ValueError:
                        pass
                elif target == "rating_cutoff":
                    try:
                        defaults[section_key][target] = float(val)
                    except ValueError:
                        pass
                elif target == "hydration_interval_minutes":
                    try:
                        defaults[section_key][target] = int(val)
                    except ValueError:
                        pass
                else:
                    defaults[section_key][target] = val

    # 解析自定义提醒段
    import re as _re2
    custom_pat = r"## 自定义提醒\s*\n.*?\n(.*?)(?=\n## |\Z)"
    cm = _re2.search(custom_pat, text, _re2.DOTALL)
    if cm:
        custom_reminders = []
        lines = cm.group(1).strip().split("\n")
        for line in lines:
            line = line.strip()
            if not line.startswith("|") or "---|---" in line or line.startswith("| id"):
                continue
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) >= 5:
                cr = {
                    "id": parts[0], "label": parts[1], "time": parts[2],
                    "repeat": parts[3] if parts[3] else "daily",
                    "date": parts[4] if len(parts) > 4 else "",
                    "note": parts[5] if len(parts) > 5 else "",
                    "images": parts[6].split("|") if len(parts) > 6 and parts[6] else [],
                }
                if cr["id"] and cr["time"]:
                    custom_reminders.append(cr)
        defaults["custom_reminders"] = custom_reminders

    # 解析景点排程模板节
    import re as _re3

    def _strip_tail(v: str) -> str:
        """移除 Markdown 表格行末尾的 |"""
        return v.strip().rstrip("|").strip()

    template_section_pat = r"## 景点排程模板\s*\n(.*?)(?=\n## |\Z)"
    tm_match = _re3.search(template_section_pat, text, _re3.DOTALL)
    if tm_match:
        templates = []
        section_text = tm_match.group(1)
        template_blocks = _re3.split(r"\n### ", section_text)
        for block in template_blocks:
            if not block.strip():
                continue
            template = {}
            tid_match = _re3.search(r"template_id\s*\|\s*(.+)", block)
            if tid_match:
                template["template_id"] = _strip_tail(tid_match.group(1))
            ms_match = _re3.search(r"match_spots\s*\|\s*(.+)", block)
            if ms_match:
                template["match_spots"] = [s.strip() for s in _strip_tail(ms_match.group(1)).split(",") if s.strip()]
            day_pattern = _re3.findall(r"(day_\d+_spots|last_day_spots)\s*\|\s*(.+)", block)
            for key, val in day_pattern:
                template[key] = [s.strip() for s in _strip_tail(val).split(",") if s.strip()]
            td_match = _re3.search(r"trip_days\s*\|\s*(.+)", block)
            if td_match:
                try:
                    template["trip_days"] = int(_strip_tail(td_match.group(1)))
                except ValueError:
                    template["trip_days"] = 0
            sr_match = _re3.search(r"schedule_rationale\s*\|\s*(.+)", block)
            if sr_match:
                template["schedule_rationale"] = _strip_tail(sr_match.group(1))
            if template.get("template_id") and template.get("match_spots"):
                templates.append(template)
        defaults["itinerary_templates"] = templates

    return defaults


def _persist_custom_reminders(schedule_nodes):
    """将 CUSTOM 类型提醒写入管家记忆"""
    custom_nodes = [n for n in schedule_nodes if n.get("type") == "CUSTOM"]
    _write_profile({"custom_reminders": custom_nodes})


def _write_profile(updates: dict) -> dict:
    """增量更新 管家记忆.md 中的字段，返回最终 profile"""
    current = _read_profile()
    # 合并 updates 到 current
    for section_key, fields in updates.items():
        if section_key in current:
            if isinstance(fields, dict):
                current[section_key].update(fields)

    # 序列化为 Markdown
    def _list_or_val(v, section):
        if isinstance(v, list):
            if section == "lifestyle" and all(isinstance(i, dict) for i in v):
                return ", ".join([f"{i['time']}:{i['name']}" for i in v])
            return ", ".join(v)
        return str(v)

    # 自定义提醒序列化
    custom_lines = ""
    for cr in current.get("custom_reminders", []):
        imgs = "|".join(cr.get("images", [])[:3]) if cr.get("images") else ""
        custom_lines += f"| {cr.get('id','')} | {cr.get('label','')} | {cr.get('time','')} | {cr.get('repeat','daily')} | {cr.get('date','')} | {cr.get('note','')} | {imgs} |\n"

    md = f"""# 管家记忆 — 用户长期偏好谱

> 本文件由系统自动维护，人类可读 + LLM 可解析。每次交互结束后由管家语义提取并写入。

## 个人身份
| 字段 | 值 |
|---|---|
| elder_name | {current['personal']['elder_name']} |
| emergency_contact_name | {current['personal']['emergency_contact_name']} |
| emergency_contact_phone | {current['personal']['emergency_contact_phone']} |

## 口味
| 字段 | 值 |
|---|---|
| taste_tolerance | {current['taste']['taste_tolerance']} |
| dietary_restrictions | {_list_or_val(current['taste']['dietary_restrictions'], 'taste')} |
| cuisine_preference | {_list_or_val(current['taste']['cuisine_preference'], 'taste')} |

## 通勤
| 字段 | 值 |
|---|---|
| walking_tolerance_meters | {current['commute']['walking_tolerance_meters']} |
| transport_priority | {current['commute']['transport_priority']} |
| travel_preference | {current['commute']['travel_preference']} |

## 预算
| 字段 | 值 |
|---|---|
| price_level | {current['budget']['price_level']} |
| custom_budget_per_person | {current['budget']['custom_budget_per_person']} |
| rating_cutoff | {current['budget']['rating_cutoff']} |

## 健康作息
| 字段 | 值 |
|---|---|
| hydration_interval_minutes | {current['lifestyle']['hydration_interval_minutes']} |
| medication_schedule | {_list_or_val(current['lifestyle']['medication_schedule'], 'lifestyle')} |
"""

    # 序列化景点排程模板
    itinerary_template_md = ""
    for tpl in current.get("itinerary_templates", []):
        tpl_name = tpl.get("template_id", "未命名模板")
        tpl_md = f"\n### {tpl_name}\n"
        tpl_md += "| 字段 | 值 |\n|---|---|\n"
        tpl_md += f"| template_id | {tpl.get('template_id', '')} |\n"
        tpl_md += f"| match_spots | {', '.join(tpl.get('match_spots', []))} |\n"
        for key in sorted(tpl.keys()):
            if key in ("template_id", "match_spots", "trip_days"):
                continue
            if key.startswith("day_") or key == "last_day_spots":
                val = tpl[key]
                if isinstance(val, list):
                    tpl_md += f"| {key} | {', '.join(val)} |\n"
        tpl_md += f"| trip_days | {tpl.get('trip_days', '')} |\n"
        sr = tpl.get("schedule_rationale", "")
        if sr:
            tpl_md += f"| schedule_rationale | {sr} |\n"
        itinerary_template_md += tpl_md

    if itinerary_template_md:
        md += f"\n## 景点排程模板{itinerary_template_md}\n"

    if custom_lines:
        md += f"""
## 自定义提醒
| id | label | time | repeat | date | note | images |
|---|---|---|---|---|---|---|
{custom_lines}"""
    with open(_MEMORY_PATH, "w", encoding="utf-8") as f:
        f.write(md)
    return current


# ======================================================================
# 通用LLM聊天 — 对话历史持久化
# ======================================================================
_CHAT_HISTORY_PATH = os.path.join(base_dir, "chat_history.json")


def _load_chat_history() -> dict:
    """加载聊天历史"""
    if not os.path.exists(_CHAT_HISTORY_PATH):
        return {"sessions": {}, "active_session": None}
    try:
        with open(_CHAT_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"sessions": {}, "active_session": None}


def _save_chat_history(history: dict):
    """保存聊天历史"""
    with open(_CHAT_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _get_active_session() -> dict:
    """获取或创建活跃的聊天会话"""
    history = _load_chat_history()
    sid = history.get("active_session")
    if sid and sid in history["sessions"]:
        return history, history["sessions"][sid]
    # 创建新会话
    now = datetime.now()
    sid = f"chat_{now.strftime('%Y%m%d_%H%M%S')}"
    session = {
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "messages": []
    }
    history["active_session"] = sid
    history["sessions"][sid] = session
    _save_chat_history(history)
    return history, session


def _append_chat_message(role: str, content=None, tool_calls=None, tool_call_id=None, name=None, reasoning_content=None):
    """向活跃会话追加一条消息"""
    history, session = _get_active_session()
    msg = {"role": role}
    if content is not None:
        msg["content"] = content
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if tool_call_id:
        msg["tool_call_id"] = tool_call_id
    if name:
        msg["name"] = name
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    msg["timestamp"] = datetime.now().isoformat()
    session["messages"].append(msg)
    session["updated_at"] = datetime.now().isoformat()
    _save_chat_history(history)


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
    "clock_enabled": False,
    # === 多日行程扩展字段 ===
    "trip_mode": "single",           # "single" | "multi"
    "trip_days": 1,                  # 计划天数 (1-7)
    "trip_destination": "北京",      # 目的地城市
    "trip_transport": "步行优先",    # 全局交通偏好
    "trip_checkin_lat": None,        # 酒店纬度
    "trip_checkin_lng": None,        # 酒店经度
    "active_day_index": 0,           # 当前查看/编辑的天
    "days": [],                      # 每天独立数据 [{day_index, label, selected_pairs, task_list, spatial_matrix, schedule_result, chat_history, transport_override}, ...]
    "candidate_pool": [],            # 多日模式下未分配的POI池 [shop_info_dict, ...]
}


def _ensure_agent():
    """确保 agent 已初始化（不重置 session_state）。
    用于 api_edit_trip 等不经过 _reset_session 的端点。
    """
    global agent
    if agent is None:
        agent = backend.MeituanAgent(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com"
        )


def _reset_session():
    global agent, session_state
    # 复用已有 agent 避免重复初始化 OpenClaw Bridge（并发时可能阻塞）
    _ensure_agent()
    # 只重置 agent 的内存状态，不重建实例
    agent.context_memory = []
    agent.poi_cache = {}
    agent.poi_cache_per_category = {}
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
        "clock_enabled": False,
        "pending_anomalies": [],
        "anomaly_intent_triggers": [],
        "pitfall_intent_triggers": [],
        "pitfall_global_reminders": [],
        # === 多日行程扩展字段 ===
        "trip_mode": "single",
        "trip_days": 1,
        "trip_destination": "北京",
        "trip_transport": "步行优先",
        "trip_checkin_lat": None,
        "trip_checkin_lng": None,
        "active_day_index": 0,
        "days": [],
        "candidate_pool": [],
    }
    # 清空虚拟时钟 session — gunicorn 模式下跳过（time_master 线程锁不兼容）
    # 原代码: tm = time_master.get_master(); tm.remove_session(_CLOCK_SESSION_ID)


def _apairs():
    """返回当前活跃的 selected_pairs，自动适配单日/多日模式。
    单日模式 → 返回顶层 selected_pairs（向后兼容）
    多日模式 → 返回当前活跃天的 selected_pairs
    """
    if session_state.get("trip_mode") == "multi":
        idx = session_state.get("active_day_index", 0)
        days = session_state.get("days", [])
        if 0 <= idx < len(days):
            return days[idx].get("selected_pairs", [])
        return []
    return session_state.get("selected_pairs", [])


def _auto_search_restaurants_for_day(day_centroid_lat, day_centroid_lng,
                                      cuisine_prefs, rating_cutoff, destination,
                                      meal_type="restaurant"):
    """自动搜索当天活动中心附近的餐厅，考虑偏好和评分。
    返回最多 3 个符合条件的餐厅 shop dict 列表。
    """
    try:
        keywords = "|".join(cuisine_prefs[:3]) if cuisine_prefs else ""
        min_rating = float(rating_cutoff) if rating_cutoff else 3.5
        city = destination or "北京"

        # 高德搜索附近餐厅
        result = _amap_client.search_nearby(
            lng=day_centroid_lng, lat=day_centroid_lat,
            keywords=keywords if keywords else meal_type,
            category=meal_type if not keywords else "",
            offset=10,
        )
        shops = result.get("shops", []) if isinstance(result, dict) else []
        # 按评分筛选并排序
        filtered = [s for s in shops if s.get("rating", 0) >= min_rating]
        filtered.sort(key=lambda s: s.get("rating", 0), reverse=True)
        return filtered[:3]
    except Exception as e:
        print(f"[auto_search_restaurants] 失败: {e}", flush=True)
        return []


def _auto_search_alternatives(shop_name, category, lat, lng, destination):
    """为闭店 POI 搜索附近同类别替代品。"""
    try:
        city = destination or "北京"
        result = _amap_client.search_nearby(
            lng=lng, lat=lat,
            keywords=shop_name,
            category=category,
            offset=5,
        )
        shops = result.get("shops", []) if isinstance(result, dict) else []
        # 排除原店
        alternatives = [s for s in shops if s.get("name", "") != shop_name]
        alternatives.sort(key=lambda s: s.get("rating", 0), reverse=True)
        return alternatives[:3]
    except Exception as e:
        print(f"[auto_search_alternatives] 失败: {e}", flush=True)
        return []


def _auto_search_hotels(lat, lng, destination, radius=5000, limit=5):
    """使用高德 API 搜索指定坐标附近的酒店。
    返回按评分降序排列的酒店列表。
    """
    try:
        city = destination or "北京"
        result = _amap_client.search_nearby(
            lng=lng, lat=lat,
            keywords="酒店",
            category="hotel",
            offset=min(limit * 3, 25),
            radius=radius,
        )
        shops = result.get("shops", []) if isinstance(result, dict) else []
        # 过滤：只保留名称中包含酒店/宾馆/民宿/旅馆/客栈的
        hotel_keywords = ["酒店", "宾馆", "民宿", "旅馆", "客栈", "青旅", "如家", "汉庭", "全季"]
        hotels = [
            s for s in shops
            if any(kw in (s.get("name", "") or "") for kw in hotel_keywords)
        ]
        # 去重（按名称）
        seen = set()
        unique = []
        for h in hotels:
            name = h.get("name", "")
            if name and name not in seen:
                seen.add(name)
                unique.append(h)
        unique.sort(key=lambda s: s.get("rating", 0) or 0, reverse=True)
        return unique[:limit]
    except Exception as e:
        print(f"[auto_search_hotels] 失败: {e}", flush=True)
        return []


def _auto_search_hotels_for_day(day_data, checkin_lat, checkin_lng, destination):
    """为某天的活动重心搜索附近酒店。
    优先用当天 task_list 的几何中心，回退到 checkin 坐标。
    """
    lats, lngs = [], []
    for t in day_data.get("task_list", []):
        try:
            lats.append(float(t.get("lat", 0)))
            lngs.append(float(t.get("lng", 0)))
        except (ValueError, TypeError):
            pass
    if lats and lngs:
        centroid_lat = sum(lats) / len(lats)
        centroid_lng = sum(lngs) / len(lngs)
    else:
        centroid_lat = float(checkin_lat)
        centroid_lng = float(checkin_lng)
    return _auto_search_hotels(centroid_lat, centroid_lng, destination)


def _compute_day_fatigue_detail(timeline, day_index=0, prev_day_fatigue=0.0):
    """计算当天行程的详细体力消耗分析。

    基于实际 itinerary（景点数、类型、时长、时间段）计算疲劳度，
    返回包含消耗明细、等级标签、活动统计的丰富数据结构。

    参数:
        timeline: 精修层格式的节点列表（action/category/duration_minutes/time）
        day_index: 天数索引（0=第一天）
        prev_day_fatigue: 前一天累积的 raw fatigue 值（用于多日累积）

    返回:
        {
            "fatigue_level": float,       # 最终体力值 0-100
            "fatigue_pct": float,         # 消耗百分比 0-1
            "fatigue_label": str,         # 轻松/适中/较累/很累/极度疲劳
            "fatigue_raw": float,         # 原始 penalty（保持向后兼容）
            "breakdown": [...],           # 每项活动消耗明细
            "activity_summary": {...},    # 各类活动计数
            "multi_day_multiplier": float,# 多日累积系数
            "noon_activities": int,       # 正午(13-15时)活动数
        }
    """
    from skills.multi_day_scheduler.scheduling_penalty import (
        time_of_day_fatigue_multiplier,
        multi_day_fatigue_multiplier, LAMBDA_FATIGUE,
        _classify_travel_category,
    )

    # ── 展示用体力系数（独立于优化器的 FATIGUE_COEFFICIENT）──
    # 设计目标：1h 景点 ≈ 7% 全天体力，让数字符合真人体验
    DISPLAY_FATIGUE_COEFFICIENT = {
        # 活动类
        "scenic": 7.0,       # 景点最累，走路+站立
        "gym": 6.0,          # 健身高强度
        "shopping": 4.0,     # 逛街走路但不紧张
        "default": 3.5,      # 未分类活动
        "cinema": 1.5,       # 坐着看
        "hair": 1.5,         # 理发
        "pet": 1.5,          # 宠物
        "cafe": 1.0,         # 咖啡厅=放松
        "restaurant": 1.0,   # 吃饭接近休息
        "hotpot": 1.0,
        "japanese": 1.0,
        "laundry": 1.0,
        "breakfast": 0.5,    # 早餐最轻松
        # 交通类（%全天体力/小时）
        "travel_walk": 4.0,    # 步行赶路
        "travel_plane": 18.0,  # 飞机：3%/10min → 18%/h
        "travel_car": 12.0,    # 汽车/打车/公交/地铁：2%/10min → 12%/h
        "travel_train": 18.0,  # 高铁/火车：3%/10min → 18%/h（归入公共交通）
    }

    # 将 server timeline 转为 refined 格式（与 dynamic_fatigue_cost 一致）
    refined = []
    TRAVEL_ACTIONS = {"LEAVE_HOME", "TO_STATION", "OUTBOUND_JOURNEY", "ARRIVAL",
                      "ARRIVAL_TRANSIT", "HOTEL_PENDING", "RETURN_JOURNEY",
                      "DEPARTURE", "ARRIVE_HOME"}
    for node in timeline:
        action = node.get("action", "")
        time_str = node.get("time", "00:00")
        try:
            h, m = map(int, time_str.split(":"))
            start_minutes = h * 60 + m
        except (ValueError, TypeError):
            start_minutes = 540  # 默认 9:00

        if action == "VISIT":
            refined.append({
                "type": "VISIT",
                "category": node.get("category", "default"),
                "duration_minutes": node.get("duration_minutes", 60),
                "start_minutes": start_minutes,
                "shop_id": node.get("shop_id", ""),
                "memo": node.get("memo", ""),
            })
        elif action in ("LUNCH", "DINNER", "REST", "BREAKFAST",
                        "LUNCH_NEEDED", "DINNER_NEEDED", "BREAKFAST_NEEDED"):
            refined.append({
                "type": action,
                "start_minutes": start_minutes,
            })
        elif action in TRAVEL_ACTIONS:
            refined.append({
                "type": "TRAVEL",
                "action": action,
                "duration_minutes": node.get("duration_minutes", 30),
                "start_minutes": start_minutes,
                "memo": node.get("memo", ""),
            })

    # ── 计算体力消耗 ──
    fatigue_level = 100.0
    cumulative_drain = 0.0
    total_penalty = 0.0
    delta = multi_day_fatigue_multiplier(day_index, prev_day_fatigue)
    breakdown = []
    activity_summary = {}
    noon_count = 0
    MEAL_RECOVERY = 2  # 每餐恢复量（吃顿饭能缓缓，但不能重置上午的疲劳）

    for node in refined:
        if node.get("type") == "VISIT":
            cat = node.get("category", "default")
            coef = DISPLAY_FATIGUE_COEFFICIENT.get(cat, DISPLAY_FATIGUE_COEFFICIENT.get("default", 3.5))
            dur_hours = node.get("duration_minutes", 60) / 60.0
            start_min = node.get("start_minutes", 540)
            gamma = time_of_day_fatigue_multiplier(start_min)

            drain = coef * dur_hours * gamma * delta
            fatigue_level -= drain
            cumulative_drain += drain

            if fatigue_level < 30:
                total_penalty += (30 - fatigue_level) ** 2

            # 活动名提取（去掉 emoji 前缀）
            memo = node.get("memo", "")
            name = memo or node.get("shop_id", "未知")
            # 提取中文名（memo 格式如 "📍 故宫"）
            for prefix in ["📍 ", "⚠️ ", "🚫 "]:
                if name.startswith(prefix):
                    name = name[len(prefix):]

            # 统计类别
            cat_label = cat
            activity_summary[cat_label] = activity_summary.get(cat_label, 0) + 1

            # 正午活动
            if 780 <= start_min <= 900:
                noon_count += 1

            breakdown.append({
                "name": name[:20],
                "category": cat,
                "drain": round(drain, 1),
                "start_time": f"{start_min // 60:02d}:{start_min % 60:02d}",
                "duration_hours": round(dur_hours, 1),
                "is_noon": 780 <= start_min <= 900,
            })
        elif node.get("type") in ("LUNCH", "DINNER", "REST", "BREAKFAST",
                                   "LUNCH_NEEDED", "DINNER_NEEDED", "BREAKFAST_NEEDED"):
            fatigue_level = min(100.0, fatigue_level + MEAL_RECOVERY)
        elif node.get("type") == "TRAVEL":
            action = node.get("action", "")
            memo = node.get("memo", "")
            travel_cat = _classify_travel_category(action, memo)
            coef = DISPLAY_FATIGUE_COEFFICIENT.get(travel_cat, DISPLAY_FATIGUE_COEFFICIENT.get("default", 3.5))
            dur_hours = node.get("duration_minutes", 30) / 60.0
            start_min = node.get("start_minutes", 540)
            gamma = time_of_day_fatigue_multiplier(start_min)

            drain = coef * dur_hours * gamma * delta
            fatigue_level -= drain
            cumulative_drain += drain

            if fatigue_level < 30:
                total_penalty += (30 - fatigue_level) ** 2

            # 交通名提取
            travel_name = memo or action
            for prefix in ["✈️ ", "🚄 ", "🚗 ", "🏠 ", "🛬 ", "🔙 ", "🏨 "]:
                if travel_name.startswith(prefix):
                    travel_name = travel_name[len(prefix):]
            travel_label = {"travel_walk": "步行", "travel_plane": "飞机", "travel_car": "汽车", "travel_train": "高铁"}.get(travel_cat, "交通")

            activity_summary[travel_label] = activity_summary.get(travel_label, 0) + 1

            if 780 <= start_min <= 900:
                noon_count += 1

            breakdown.append({
                "name": travel_name[:20],
                "category": travel_label,
                "drain": round(drain, 1),
                "start_time": f"{start_min // 60:02d}:{start_min % 60:02d}",
                "duration_hours": round(dur_hours, 1),
                "is_noon": 780 <= start_min <= 900,
            })

    # ── 最终值计算 ──
    # 疲劳百分比基于累积消耗（MAX_DAILY_DRAIN=100 → 1:1 映射，100 点 = 100%）
    MAX_DAILY_DRAIN = 100.0
    fatigue_pct = round(min(1.0, cumulative_drain / MAX_DAILY_DRAIN), 3)
    fatigue_level = max(0.0, min(100.0, fatigue_level))
    fatigue_raw = LAMBDA_FATIGUE * total_penalty

    # 等级标签（基于累积消耗，与用餐恢复无关）
    if cumulative_drain < 15:
        label = "轻松"
    elif cumulative_drain < 30:
        label = "适中"
    elif cumulative_drain < 50:
        label = "较累"
    elif cumulative_drain < 70:
        label = "很累"
    else:
        label = "极度疲劳"

    # breakdown 按消耗降序排列，只保留有消耗的
    breakdown.sort(key=lambda x: x["drain"], reverse=True)

    return {
        "fatigue_level": round(fatigue_level, 1),
        "fatigue_pct": fatigue_pct,
        "fatigue_label": label,
        "fatigue_raw": round(fatigue_raw, 1),
        "cumulative_drain": round(cumulative_drain, 1),
        "breakdown": breakdown,
        "activity_summary": activity_summary,
        "multi_day_multiplier": round(delta, 2),
        "noon_activities": noon_count,
    }


def _make_strategy_reasoning(strategy: str, fatigue_detail: dict,
                              end_time_minutes: int, reason: str) -> str:
    """根据疲劳分析和策略生成人类可读的策略推理文案。"""
    fatigue_pct = int(fatigue_detail.get("fatigue_pct", 0) * 100)
    fatigue_label = fatigue_detail.get("fatigue_label", "")
    summary = fatigue_detail.get("activity_summary", {})
    scenic_count = summary.get("scenic", 0)
    shopping_count = summary.get("shopping", 0)
    total_activities = sum(summary.values())
    multi_day = fatigue_detail.get("multi_day_multiplier", 1.0)

    # 基础描述
    parts = []
    if scenic_count > 0:
        parts.append(f"{scenic_count}个景点")
    if shopping_count > 0:
        parts.append(f"{shopping_count}处购物")
    other_count = total_activities - scenic_count - shopping_count
    if other_count > 0:
        parts.append(f"{other_count}项其他活动")

    activity_desc = "、".join(parts) if parts else f"{total_activities}项活动"

    base = f"今日{activity_desc}，预计消耗{fatigue_pct}%体力（{fatigue_label}）"

    if multi_day > 1.05:
        base += f"，多日累积系数×{multi_day}"

    # 策略特定文案
    if strategy == "switch":
        end_h = end_time_minutes // 60
        end_m = end_time_minutes % 60
        return f"{base}，行程在{end_h:02d}:{end_m:02d}前结束，体力充足，建议换房以减少通勤时间"
    else:  # sustained
        end_h = end_time_minutes // 60
        end_m = end_time_minutes % 60
        if fatigue_pct >= 30:
            return f"{base}，体力消耗较大，建议坚守原酒店休息"
        elif end_time_minutes >= 1200:
            return f"{base}，行程结束较晚（{end_h:02d}:{end_m:02d}），建议坚守原酒店，避免深夜搬行李"
        else:
            return f"{base}，建议坚守原酒店，减少换房麻烦"


def _compute_hotel_decisions(result_days, checkin_lat, checkin_lng, destination):
    """根据每日排程结果计算酒店决策。

    对每天 timeline 计算动态体力消耗，然后调用 hotel_decision 模块
    判断是否需要换酒店、采用什么策略。

    返回:
        [{"day_index": 0, "strategy": "sustained", "should_switch": False,
          "reason": "", "fatigue": 0.0, "end_time_minutes": 1200,
          "hotel_options": [...]}, ...]
    """
    decisions = []
    prev_day_fatigue = 0.0
    cumulative_time_saved = 0.0

    for i, day in enumerate(result_days):
        timeline = day.get("timeline", [])

        # ── 使用 _compute_day_fatigue_detail 替代旧 dynamic_fatigue_cost ──
        fatigue_detail = _compute_day_fatigue_detail(timeline, i, prev_day_fatigue)
        fatigue_normalized = fatigue_detail["fatigue_pct"]  # 0-1，基于实际消耗

        # 计算当日结束时间
        end_time_minutes = 1200  # 默认 20:00
        for node in reversed(timeline):
            t = node.get("time", "")
            try:
                h, m = map(int, t.split(":"))
                end_time_minutes = h * 60 + m
                break
            except (ValueError, TypeError):
                pass

        # 计算新酒店 vs 原酒店的时间节省
        time_saved_single = 0.0
        hotel_options = _auto_search_hotels_for_day(day, checkin_lat, checkin_lng, destination)
        if hotel_options:
            import math
            def _haversine_km(lat1, lng1, lat2, lng2):
                R = 6371
                phi1, phi2 = math.radians(lat1), math.radians(lat2)
                dphi = math.radians(lat2 - lat1)
                dlambda = math.radians(lng2 - lng1)
                a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
                return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

            lats, lngs = [], []
            for t in day.get("task_list", []):
                try:
                    lats.append(float(t.get("lat", 0)))
                    lngs.append(float(t.get("lng", 0)))
                except (ValueError, TypeError):
                    pass
            if lats and lngs:
                centroid_lat = sum(lats) / len(lats)
                centroid_lng = sum(lngs) / len(lngs)
            else:
                centroid_lat = float(checkin_lat)
                centroid_lng = float(checkin_lng)

            dist_to_original = _haversine_km(centroid_lat, centroid_lng, float(checkin_lat), float(checkin_lng))
            best_hotel = hotel_options[0]
            best_lat = float(best_hotel.get("lat", checkin_lat))
            best_lng = float(best_hotel.get("lng", checkin_lng))
            dist_to_new = _haversine_km(centroid_lat, centroid_lng, best_lat, best_lng)
            time_saved_single = max(0.0, (dist_to_original - dist_to_new) / 20.0 * 60.0)
            cumulative_time_saved += time_saved_single

        # 调用 hotel_decision
        should_switch, reason = should_switch_hotel(
            fatigue_normalized, time_saved_single, cumulative_time_saved
        )
        strategy = determine_strategy(
            fatigue_normalized, end_time_minutes
        )

        # 如果决策是换房，确保有酒店选项
        if strategy == "switch" and not hotel_options:
            strategy = "sustained"
            should_switch = False
            reason = "no_hotel_available"

        # 动态生成策略推理文案
        strategy_reasoning = _make_strategy_reasoning(
            strategy, fatigue_detail, end_time_minutes, reason
        )

        decisions.append({
            "day_index": i,
            "strategy": strategy,
            "should_switch": should_switch,
            "reason": reason,
            "fatigue": fatigue_normalized,
            "fatigue_raw": fatigue_detail["fatigue_raw"],
            "fatigue_level": fatigue_detail["fatigue_level"],
            "fatigue_label": fatigue_detail["fatigue_label"],
            "fatigue_breakdown": fatigue_detail["breakdown"][:5],  # 前5项主要消耗
            "activity_summary": fatigue_detail["activity_summary"],
            "multi_day_multiplier": fatigue_detail["multi_day_multiplier"],
            "strategy_reasoning": strategy_reasoning,
            "end_time_minutes": end_time_minutes,
            "time_saved_single": round(time_saved_single, 1),
            "time_saved_cumulative": round(cumulative_time_saved, 1),
            "hotel_options": hotel_options[:3] if should_switch else [],
        })

        prev_day_fatigue = fatigue_detail["cumulative_drain"]

    return decisions


def _l3_capacity_scan_and_dump(unassigned, days, checkin_lat, checkin_lng, transport="步行优先"):
    """Phase 6: L3 容量余量扫描 + 极简打卡倒灌。

    将所有未分配店铺以 is_backup=True 强制插入每天时间线的空隙中。
    这是排程的最后一道防线 —— 宁可挤一点，也不能丢店铺。

    算法：
    1. 扫描每天 timeline，找出所有可用时间空隙（gap = 前节点结束到后节点开始）
    2. 对每个 unassigned shop，按容量降序扫描每天，找到能容纳它的 gap
    3. 插入为 VISIT 节点，标记 is_backup=True
    4. 为 backup 店铺估算旅行时间（haversine 到相邻节点的距离 / 步行速度）

    Returns: (days, still_unassigned, backup_count)
    """
    import math

    # ── 辅助：时间字符串 ↔ 分钟 ──
    def _t2m(t_str):
        try:
            h, m = map(int, str(t_str).split(":"))
            return h * 60 + m
        except (ValueError, TypeError):
            return 12 * 60

    def _m2t(m):
        m = max(0, min(23 * 60 + 59, m))
        return f"{m // 60:02d}:{m % 60:02d}"

    # ── 辅助：Haversine 距离（米）──
    def _dist_m(lat1, lng1, lat2, lng2):
        try:
            R = 6371000
            phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
            dphi = math.radians(float(lat2) - float(lat1))
            dlambda = math.radians(float(lng2) - float(lng1))
            a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
            return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        except (ValueError, TypeError):
            return 5000  # 坐标缺失时保守估计 5km

    if not unassigned:
        return days, [], 0

    # ── 品类默认时长（与 scheduler 保持一致）──
    CAT_DUR = {
        "scenic": 180, "restaurant": 60, "hotpot": 90, "cafe": 30,
        "shopping": 90, "cinema": 120, "hotel": 480,
        "lunch_needed": 60, "dinner_needed": 60, "breakfast_needed": 30,
        "museum": 120, "park": 90, "default": 60,
    }

    # ── 构建每天的时间线节点索引，用于快速查找相邻节点坐标 ──
    day_nodes = []  # [(node, day_index), ...] for all non-special actions
    for di, day in enumerate(days):
        nodes = []
        for n in day.get("timeline", []):
            nodes.append(n)
        day_nodes.append(nodes)

    # ── 对每个 unassigned shop，找最优插入位置 ──
    still_unassigned = []
    backup_count = 0

    for us in unassigned:
        shop_id = us.get("shop_id", "")
        shop_name = us.get("name", us.get("shop_name", shop_id))
        shop_cat = us.get("category", "default")
        shop_lat = us.get("lat", checkin_lat)
        shop_lng = us.get("lng", checkin_lng)
        shop_dur = us.get("duration_minutes", CAT_DUR.get(shop_cat, 60))

        # 尝试插入到每天
        best_insert = None  # (day_idx, position, gap_minutes, insert_time)

        for di, day in enumerate(days):
            timeline = day.get("timeline", [])
            if not timeline:
                continue

            # 找 TO_STATION 时间作为硬截止（离开日不能超过此时间）
            cutoff_min = 24 * 60  # 默认无限制
            for n in timeline:
                if n.get("action") == "TO_STATION":
                    cutoff_min = _t2m(n.get("time", "22:00"))
                    break

            for j in range(len(timeline) - 1):
                prev_node = timeline[j]
                next_node = timeline[j + 1]

                # 跳过特殊动作：不插入到 WAKE_UP 前、TO_STATION/RETURN_JOURNEY 节点之间
                if next_node.get("action") in ("WAKE_UP",):
                    continue
                if prev_node.get("action") in ("BEDTIME",):
                    continue
                # 不在 travel 节点之间插入
                if prev_node.get("action") in ("TO_STATION", "RETURN_JOURNEY", "ARRIVE_HOME", "DEPARTURE"):
                    continue
                if next_node.get("action") in ("TO_STATION", "RETURN_JOURNEY", "ARRIVE_HOME", "DEPARTURE"):
                    continue

                # 计算 gap
                prev_time = _t2m(prev_node.get("time", "09:00"))
                prev_dur = prev_node.get("duration_minutes", 0)
                prev_end = prev_time + prev_dur

                next_time = _t2m(next_node.get("time", "22:00"))

                gap = next_time - prev_end
                if gap <= 0:
                    continue

                # 估算通勤时间
                prev_lat = prev_node.get("lat", checkin_lat)
                prev_lng = prev_node.get("lng", checkin_lng)
                next_lat = next_node.get("lat", checkin_lat)
                next_lng = next_node.get("lng", checkin_lng)

                travel_to = _dist_m(prev_lat, prev_lng, shop_lat, shop_lng) / 80  # 80m/min ≈ 步行
                travel_from = _dist_m(shop_lat, shop_lng, next_lat, next_lng) / 80
                travel_buffer = int(travel_to + travel_from)
                # 极简模式：通勤缓冲不超过 gap 的 1/3，保证至少能塞入
                travel_buffer = min(travel_buffer, max(0, gap // 3))

                needed = shop_dur + travel_buffer
                if gap >= needed:
                    # 选择剩余容量最大的 gap（gap - needed 最大）
                    slack = gap - needed
                    if best_insert is None or slack > best_insert[3]:
                        insert_time = prev_end + int(travel_to * 0.6)  # 偏向前节点
                        # 插入时间 + 持续时间不能超过当天硬截止
                        if insert_time + shop_dur > cutoff_min:
                            continue
                        best_insert = (di, j + 1, slack, insert_time)

        if best_insert is not None:
            di, pos, slack, insert_time = best_insert
            # 构建 backup 节点
            backup_node = {
                "time": _m2t(insert_time),
                "action": "VISIT",
                "memo": f"📋 备选：{shop_name}（{us.get('unassigned_type', 'backup')}恢复）",
                "category": shop_cat,
                "shop_id": shop_id,
                "duration_minutes": shop_dur,
                "opentime": us.get("opentime", "未知"),
                "is_backup": True,  # Phase 6: 标记为备选打卡
                "lat": shop_lat,
                "lng": shop_lng,
            }
            days[di]["timeline"].insert(pos, backup_node)
            backup_count += 1
            print(f"[L3倒灌] ✅ '{shop_name}' ({shop_id}) → 第{di+1}天 {_m2t(insert_time)} "
                  f"(gap剩余{slack}min, dur={shop_dur}min)", flush=True)
        else:
            still_unassigned.append(us)
            print(f"[L3倒灌] ❌ '{shop_name}' ({shop_id}) 无法塞入任何一天的时间空隙", flush=True)

    # ── 所有天重新按时间排序（保持 WAKE_UP 最前、BEDTIME 最后）──
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


def _llm_estimate_shop_durations(candidate_pool: list) -> tuple:
    """排程前：让 LLM 搜索/估算每个店铺的实际耗时、体力消耗和适配前往时间。

    对每个候选店铺，LLM 基于自身知识（知名景点有训练数据）估算：
    - duration_minutes: 实际游玩耗时（分钟）
    - fatigue_weight: 体力消耗权重（1-10，1=轻松散步，10=极度消耗体力）
    - suitable_time: 适配前往时间（"day"=适合白天, "night"=适合夜间, "both"=白天夜间均可）

    优先使用 shop 估算缓存（_shop_estimates_cache），miss 的才调 LLM。
    搜不到的店铺回退到 CATEGORY_DURATIONS 品类默认值。

    Returns:
        (dynamic_durations: dict, fatigue_weights: dict, suitable_times: dict)
        - dynamic_durations: {shop_id: minutes}
        - fatigue_weights: {shop_id: weight}
        - suitable_times: {shop_id: "day"|"night"|"both"}
    """
    if not candidate_pool:
        return {}, {}, {}

    # ── 先从缓存中获取已知 shop 的估算 ──
    dynamic_durations = {}
    fatigue_weights = {}
    suitable_times = {}
    uncached_shops = []
    cache_hits = 0

    for s in candidate_pool:
        sid = s.get("shop_id", "")
        if sid and sid in _shop_estimates_cache:
            cached = _shop_estimates_cache[sid]
            dur = cached.get("duration_minutes")
            fw = cached.get("fatigue_weight")
            st = cached.get("suitable_time")
            if dur is not None and isinstance(dur, (int, float)) and dur > 0:
                dynamic_durations[sid] = int(dur)
            if fw is not None and isinstance(fw, (int, float)) and 1 <= fw <= 10:
                fatigue_weights[sid] = float(fw)
            if st in ("day", "night", "both"):
                suitable_times[sid] = st
            cache_hits += 1
        else:
            uncached_shops.append(s)

    if not uncached_shops:
        print(f"[LLM耗时估算] ✅ 全部来自缓存（{cache_hits}/{len(candidate_pool)}），跳过LLM调用", flush=True)
        return dynamic_durations, fatigue_weights, suitable_times

    print(f"[LLM耗时估算] 缓存命中 {cache_hits}/{len(candidate_pool)}，需LLM估算 {len(uncached_shops)} 个", flush=True)

    _ensure_agent()

    # 构建店铺列表文本（仅未缓存的）
    shops_text = ""
    for i, s in enumerate(uncached_shops):
        sid = s.get("shop_id", f"unknown_{i}")
        name = s.get("name", "未知")
        cat = s.get("category", "unknown")
        addr = s.get("address", "")
        coord = s.get("coord", "")
        shops_text += f"{i+1}. id={sid} | 名称={name} | 品类={cat}"
        if addr:
            shops_text += f" | 地址={addr}"
        if coord:
            shops_text += f" | 坐标={coord}"
        shops_text += "\n"

    system_prompt = f"""你是一个专业的旅行规划数据助手。你的任务是为以下每个目的地估算**实际游玩耗时**、**体力消耗权重**和**适配前往时间段**。

请基于你对这些地点的了解（包括知名度、规模、实际游览所需时间、适合白天还是晚上）进行估算：

**耗时估算规则：**
- 大型景区（如故宫、颐和园、八达岭长城等）：通常 180-360 分钟
- 中型景区/公园（如天坛、圆明园等）：通常 120-240 分钟
- 小型景点/步行街/商圈（如王府井、南锣鼓巷等）：通常 60-120 分钟
- 博物馆：通常 90-180 分钟
- 购物中心/商圈：通常 60-120 分钟
- 餐厅：通常 60-90 分钟
- 咖啡馆/饮品店：通常 30-45 分钟

**体力消耗权重（1-10）：**
- 1-3：轻松（逛街、咖啡馆、平路公园）
- 4-6：适中（中型景区、博物馆、有少量台阶）
- 7-8：较累（大型景区、需要较多步行）
- 9-10：很累（爬山、长城、大型户外徒步）

**适配前往时间（suitable_time）：**
- "day"：只适合白天前往（如故宫、天坛、爬山类景点、博物馆等白天开放的场所）
- "night"：只适合/更适合夜间前往（如夜市、酒吧街、灯光秀、夜景观景点）
- "both"：白天夜间均可（如商圈步行街、餐厅等全天候场所）

只输出JSON，格式如下，不要任何其他文字：
{{
  "estimates": [
    {{"shop_id": "xxx", "duration_minutes": 180, "fatigue_weight": 5, "suitable_time": "day", "reason": "一句话理由"}},
    ...
  ]
}}

以下是需要估算的目的地列表：
{shops_text}"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "请逐一估算以上所有目的地的耗时和体力消耗，只输出JSON。"},
        ]
        msg = agent._call_llm(messages, max_tokens=8000, response_format={"type": "json_object"})
        content = msg.content or ""

        # 提取 JSON
        import re
        json_match = re.search(r'\{[\s\S]*\}', content)
        if not json_match:
            print(f"[LLM耗时估算] ⚠️ 响应中未找到JSON，回退品类默认值", flush=True)
            return dynamic_durations, fatigue_weights, suitable_times

        data = json.loads(json_match.group(0))
        estimates = data.get("estimates", [])

        new_count = 0
        for est in estimates:
            sid = est.get("shop_id", "")
            dur = est.get("duration_minutes")
            fw = est.get("fatigue_weight")
            st = est.get("suitable_time")
            reason = est.get("reason", "")
            if sid:
                if dur is not None and isinstance(dur, (int, float)) and dur > 0:
                    dynamic_durations[sid] = int(dur)
                if fw is not None and isinstance(fw, (int, float)) and 1 <= fw <= 10:
                    fatigue_weights[sid] = float(fw)
                if st in ("day", "night", "both"):
                    suitable_times[sid] = st
                # 写入缓存
                _shop_estimates_cache[sid] = {
                    "duration_minutes": int(dur) if (dur is not None and isinstance(dur, (int, float)) and dur > 0) else None,
                    "fatigue_weight": float(fw) if (fw is not None and isinstance(fw, (int, float)) and 1 <= fw <= 10) else None,
                    "suitable_time": st if st in ("day", "night", "both") else None,
                }
                new_count += 1
                print(f"[LLM耗时估算] {sid}: dur={dur}min, fatigue={fw}, suitable={st} — {reason}", flush=True)

        if new_count > 0:
            _save_shop_estimates_cache()
        print(f"[LLM耗时估算] ✅ 完成：{len(dynamic_durations)} 个耗时 + {len(fatigue_weights)} 个体力 + {len(suitable_times)} 个时间段（新增LLM估算 {new_count} 个）", flush=True)
        return dynamic_durations, fatigue_weights, suitable_times

    except Exception as e:
        print(f"[LLM耗时估算] ❌ 失败: {e}，回退品类默认值", flush=True)
        import traceback
        traceback.print_exc()
        return dynamic_durations, fatigue_weights, suitable_times


def _run_multi_day_schedule(candidate_pool, trip_days, checkin_lat, checkin_lng,
                             transport, start_time,
                             dynamic_durations=None, fatigue_weights=None,
                             suitable_times=None):
    """调用多日排程引擎，将候选池中的店铺分配到每天。并自动补全三餐+处理闭店冲突。"""
    # 转换候选池格式：确保有 lat/lng，保留 opentime
    shops = []
    for item in candidate_pool:
        shop = dict(item)
        coord = shop.get("coord", "")
        if "," in str(coord):
            parts = str(coord).split(",")
            try:
                shop["lat"] = float(parts[0].strip())
                shop["lng"] = float(parts[1].strip())
            except (ValueError, TypeError):
                shop["lat"] = float(checkin_lat)
                shop["lng"] = float(checkin_lng)
        else:
            shop["lat"] = float(shop.get("lat", checkin_lat))
            shop["lng"] = float(shop.get("lng", checkin_lng))
        # 保留 opentime
        if "opentime" not in shop:
            shop["opentime"] = "未知"
        shops.append(shop)

    # 获取天气数据和用户偏好
    weather_data = session_state.get("trip_weather", {})
    try:
        preferences = _read_profile()
    except Exception:
        preferences = {}

    # 构建旅行信息（去程/返程）
    departure_city = session_state.get("trip_departure_city", "")
    travel_info = {
        "outbound_type": session_state.get("trip_outbound_type", ""),
        "outbound_departure_time": session_state.get("trip_outbound_departure_time", ""),
        "outbound_arrival_time": session_state.get("trip_outbound_arrival_time", ""),
        "arrival_station": session_state.get("trip_arrival_station", ""),
        "return_type": session_state.get("trip_return_type", ""),
        "return_departure_time": session_state.get("trip_return_departure_time", ""),
        "return_station": session_state.get("trip_return_station", ""),
        "departure_city": departure_city,
    }
    # 补充站点坐标（从内置字典查找）
    try:
        from skills.multi_day_scheduler.multi_day_scheduler import _lookup_station_coord
        for key in ("arrival_station", "return_station"):
            st_name = travel_info.get(key, "")
            if st_name:
                coord = _lookup_station_coord(st_name)
                if coord:
                    travel_info[f"{key}_lat"] = coord[0]
                    travel_info[f"{key}_lng"] = coord[1]
    except ImportError:
        pass
    travel_preference = session_state.get("trip_travel_preference",
        preferences.get("commute", {}).get("travel_preference", "公共交通"))

    # ── Amap 地理编码回调：为缺坐标的 shop 调用高德 API 补全 ──
    def _geocode_shop(name, address):
        """通过高德 API 根据名称+地址查询坐标"""
        try:
            # 优先后端已加载的 Amap client
            from skills.amap_poi.amap_poi import AmapPOIClient
            _gc_client = AmapPOIClient()
            geo_result = _gc_client.geocode(address=address or name, city="")
            if geo_result and isinstance(geo_result, dict):
                loc = geo_result.get("location", "")
                if "," in loc:
                    parts = loc.split(",")
                    return (float(parts[1]), float(parts[0]))  # (lat, lng)
            # 回退：用 search_poi 搜索
            search_result = _gc_client.search_poi(keywords=name, city="")
            if search_result and isinstance(search_result, list) and len(search_result) > 0:
                first = search_result[0]
                loc = first.get("location", "")
                if "," in loc:
                    parts = loc.split(",")
                    return (float(parts[1]), float(parts[0]))
        except Exception:
            pass
        return None

    result = _skill_multi_day_schedule(
        shops, trip_days,
        float(checkin_lat), float(checkin_lng),
        transport, start_time,
        weather_data=weather_data,
        preferences=preferences,
        travel_info=travel_info,
        travel_preference=travel_preference,
        dynamic_durations=dynamic_durations,
        fatigue_weights=fatigue_weights,
        suitable_times=suitable_times,
        geocode_callback=_geocode_shop,
    )

    # ── 后处理：跨天餐厅去重 + 保留待排程占位 ──
    # 不再通过 Amap API 自动搜索补全餐厅，只用预选池中的餐厅。
    # 预选餐厅不足时保留 LUNCH_NEEDED / DINNER_NEEDED 占位节点。
    global_used_meal_ids = set()  # 跨天追踪已用餐厅，防止同店复排
    auto_meals_added = []  # 不再自动添加，保留字段兼容前端
    destination = session_state.get("trip_destination", "北京")

    for day in result.get("days", []):
        day_idx = day.get("day_index", 0)
        timeline = day.get("timeline", [])

        # 跨天去重：移除当天 timeline 中已被前些天用过的餐厅
        for node in timeline:
            sid = node.get("shop_id", "")
            action = node.get("action", "")
            if sid and action in ("LUNCH", "DINNER"):
                if sid in global_used_meal_ids:
                    # 该餐厅已在之前的天使用过 → 替换为待排程占位
                    node["action"] = "LUNCH_NEEDED" if action == "LUNCH" else "DINNER_NEEDED"
                    node["memo"] = "⚠️ 午餐（待补充）" if action == "LUNCH" else "⚠️ 晚餐（待补充）"
                    node["category"] = "lunch_needed" if action == "LUNCH" else "dinner_needed"
                    node["shop_id"] = ""
                    node["opentime"] = "未知"
                else:
                    global_used_meal_ids.add(sid)

        # 重新排序（去重替换可能改变节点，保持 WAKE_UP 最前、BEDTIME 最后）
        _sort_timeline_keep_wake_bedtime(timeline)

        # ── 插入 "返回酒店" 节点（紧跟最后一项活动之后，BEDTIME 之前）──
        # 最后一天有返程交通时跳过：用户坐飞机/火车回家，不需要酒店
        has_return_journey = any(
            n.get("action") in ("TO_STATION", "DEPARTURE", "RETURN_JOURNEY", "ARRIVE_HOME")
            for n in timeline
        )
        if not has_return_journey:
            bedtime_idx = -1
            for j, node in enumerate(timeline):
                if node.get("action") == "BEDTIME":
                    bedtime_idx = j
                    break
            if bedtime_idx >= 0:
                # 找到 BEDTIME 之前最晚的活动节点，计算酒店时间 = 最后活动结束时间 + 通勤缓冲
                last_activity_end = 19 * 60  # 兜底：最早 19:00
                for j in range(bedtime_idx):
                    node = timeline[j]
                    action = node.get("action", "")
                    if action in ("VISIT", "LUNCH", "DINNER", "REST", "BREAKFAST",
                                  "LUNCH_NEEDED", "DINNER_NEEDED", "BREAKFAST_NEEDED"):
                        t = _time_str_to_minutes(node.get("time", "00:00"))
                        dur = node.get("duration_minutes", 30)
                        end_t = t + dur + 15  # +15min 通勤缓冲
                        if end_t > last_activity_end:
                            last_activity_end = end_t
                # 不晚于 BEDTIME 前 15 分钟
                bt = _time_str_to_minutes(timeline[bedtime_idx].get("time", "22:00"))
                hotel_time = _safe_time_str(min(last_activity_end, bt - 15))
                hotel_node = {
                    "time": hotel_time,
                    "action": "HOTEL_PENDING",
                    "memo": "🏨 返回酒店（待安排）",
                    "category": "hotel",
                    "shop_id": "",
                    "duration_minutes": 30,
                    "opentime": "未知",
                }
                timeline.insert(bedtime_idx, hotel_node)
        # 重新排序确保时间正确（保持 WAKE_UP 最前、BEDTIME 最后）
        _sort_timeline_keep_wake_bedtime(timeline)

    # ── 后处理：闭店冲突 → 搜索替代品 ──
    closed_conflicts_resolved = []
    for cc in result.get("closed_conflicts", []):
        day_idx = cc.get("day_index", 0)
        shop_name = cc.get("shop_name", "")
        cat = cc.get("category", "")
        # 从当天 task_list 中找坐标
        day_data = result["days"][day_idx] if day_idx < len(result["days"]) else None
        ref_lat, ref_lng = float(checkin_lat), float(checkin_lng)
        if day_data:
            for t in day_data.get("task_list", []):
                if t.get("name") == shop_name:
                    ref_lat = float(t.get("lat", ref_lat))
                    ref_lng = float(t.get("lng", ref_lng))
                    break

        alternatives = _auto_search_alternatives(shop_name, cat, ref_lat, ref_lng, destination)
        cc["alternatives"] = [{"name": a.get("name", ""), "rating": a.get("rating", 0),
                                "opentime": a.get("opentime", "未知")} for a in alternatives]
        closed_conflicts_resolved.append(cc)

    result["closed_conflicts"] = closed_conflicts_resolved
    result["auto_meals_added"] = auto_meals_added

    # ── 酒店决策：计算换房策略 + 搜索附近酒店 ──
    try:
        hotel_decisions = _compute_hotel_decisions(
            result.get("days", []), checkin_lat, checkin_lng, destination
        )
        # 注入到 algorithm_metadata
        result.setdefault("algorithm_metadata", {})["hotel_decisions"] = hotel_decisions
        # 为每天注入酒店信息（包含完整疲劳分析）
        for dec in hotel_decisions:
            di = dec["day_index"]
            if di < len(result["days"]):
                result["days"][di]["hotel_info"] = {
                    "strategy": dec["strategy"],
                    "should_switch": dec["should_switch"],
                    "reason": dec["reason"],
                    "fatigue": dec["fatigue"],
                    "fatigue_level": dec.get("fatigue_level", 0),
                    "fatigue_label": dec.get("fatigue_label", ""),
                    "fatigue_breakdown": dec.get("fatigue_breakdown", []),
                    "activity_summary": dec.get("activity_summary", {}),
                    "multi_day_multiplier": dec.get("multi_day_multiplier", 1.0),
                    "strategy_reasoning": dec.get("strategy_reasoning", ""),
                    "hotel_options": dec["hotel_options"],
                }
        print(f"[multi_day] 酒店决策完成, 共{len(hotel_decisions)}天, "
              f"换房建议: {sum(1 for d in hotel_decisions if d['should_switch'])}天", flush=True)
    except Exception as e:
        print(f"[multi_day] 酒店决策失败: {e}", flush=True)
        import traceback
        traceback.print_exc()
        # ── 兜底：即使酒店搜索失败，也要为每天注入疲劳预测 ──
        try:
            prev_fatigue = 0.0
            for di, day in enumerate(result.get("days", [])):
                fd = _compute_day_fatigue_detail(day.get("timeline", []), di, prev_fatigue)
                prev_fatigue = fd["cumulative_drain"]
                result["days"][di]["hotel_info"] = {
                    "strategy": "sustained",
                    "should_switch": False,
                    "reason": "fallback",
                    "fatigue": fd["fatigue_pct"],
                    "fatigue_level": fd["fatigue_level"],
                    "fatigue_label": fd["fatigue_label"],
                    "fatigue_breakdown": fd["breakdown"][:5],
                    "activity_summary": fd["activity_summary"],
                    "multi_day_multiplier": fd["multi_day_multiplier"],
                    "strategy_reasoning": f"今日{fatigue_label}（{int(fd['fatigue_pct']*100)}%），酒店信息暂时无法获取",
                    "hotel_options": [],
                }
            print(f"[multi_day] 疲劳预测兜底完成, 共{len(result.get('days', []))}天", flush=True)
        except Exception as e2:
            print(f"[multi_day] 疲劳兜底也失败: {e2}", flush=True)

    return result


def _time_str_to_minutes(time_str: str) -> int:
    """ "HH:MM" → 分钟数 """
    try:
        h, m = map(int, time_str.split(":"))
        return h * 60 + m
    except (ValueError, TypeError):
        return 0


def _safe_time_str(minutes: int) -> str:
    """分钟数 → "HH:MM"，钳制在 00:00-23:59"""
    m = max(0, min(minutes, 23 * 60 + 59))
    return f"{m // 60:02d}:{m % 60:02d}"


def _sort_timeline_keep_wake_bedtime(timeline: list) -> list:
    """原地排序 timeline：WAKE_UP 最前、BEDTIME 最后、其余按时间。

    与 multi_day_scheduler.py 中的排序逻辑保持一致，
    防止纯时间排序破坏门到门出行链路的逻辑顺序。
    """
    wake_node = None
    bedtime_node = None
    rest = []
    for n in timeline:
        action = n.get("action", "")
        if action == "WAKE_UP":
            wake_node = n
        elif action == "BEDTIME":
            bedtime_node = n
        else:
            rest.append(n)
    rest.sort(key=lambda x: _time_str_to_minutes(x.get("time", "00:00")))
    timeline.clear()
    if wake_node:
        timeline.append(wake_node)
    timeline.extend(rest)
    if bedtime_node:
        timeline.append(bedtime_node)
    return timeline


def _inject_multi_day_reminders(schedule_result: dict) -> dict:
    """将多日行程中的 WAKE_UP 和 BEDTIME 节点注册为持续响铃提醒。
    每个节点关联具体日期（而非全部同一天）。"""
    try:
        _tm = time_master.get_master()
        _cs = _tm.get_session(_CLOCK_SESSION_ID)
        if not _cs:
            # 时钟未运行，不注入（用户未开启虚拟时钟）
            return {"status": "SKIPPED", "reason": "clock_disabled", "count": 0}

        # 保留现有 WATER/MED 节点
        existing = list(_cs.schedule_nodes) if _cs.schedule_nodes else []
        preserved = [n for n in existing if n.get("type") in ("WATER", "MED")]

        # 保留现有 SCHEDULE 节点（不与多日提醒冲突）
        schedule_nodes = [n for n in existing if n.get("type") == "SCHEDULE"]

        # 收集多日行程提醒
        trip_start_date = session_state.get("trip_start_date", datetime.now().strftime("%Y-%m-%d"))
        trip_days = session_state.get("trip_days", 1)

        # 计算每天的实际日期
        from datetime import timedelta as _td
        sd = datetime.strptime(trip_start_date, "%Y-%m-%d")

        reminder_nodes = []
        for day in schedule_result.get("days", []):
            day_idx = day.get("day_index", 0)
            day_label = day.get("label", f"第{day_idx+1}天")
            # 计算这一天的实际日期
            day_date = (sd + _td(days=day_idx)).strftime("%Y-%m-%d")

            for node in day.get("timeline", []):
                action = node.get("action", "")
                if action in ("WAKE_UP", "BEDTIME"):
                    is_wake = action == "WAKE_UP"
                    node_id = f"multi_{action}_{day_idx}"
                    label = f"{day_label} {'起床' if is_wake else '就寝'}"
                    reminder_nodes.append({
                        "id": node_id,
                        "time": node.get("time", "07:30" if is_wake else "22:00"),
                        "type": "CUSTOM",
                        "action": action,  # 保留 action 用于触发防坑推送/疲劳调研
                        "label": label,
                        "repeat": "once",  # 每天只在具体日期触发一次
                        "date": day_date,  # 每-天使用独立的日期
                        "note": node.get("memo", ""),
                        "alarm_type": "persistent_ring",
                        "day_index": day_idx,
                    })

        # 合并：保留 + 行程节点 + 新提醒
        merged = preserved + schedule_nodes + reminder_nodes
        _tm.set_schedule(_CLOCK_SESSION_ID, merged)

        return {"status": "SUCCESS", "count": len(reminder_nodes)}

    except Exception as e:
        print(f"[multi_day_reminders] 注入失败: {e}", flush=True)
        return {"status": "ERROR", "error": str(e), "count": 0}


def _validate_weather_questions(questions: list, day_weather_map: dict) -> list:
    """校验天气相关的问题：如果建议改期的目标天天气同样不好，替换为室内/带雨具建议。"""
    import re as _vre
    validated = []
    for q in questions:
        text = q.get("question_text", "")
        # 匹配"改到第X天"模式
        move_match = _vre.search(r'改到第(\d+)天', text)
        if move_match:
            target_day_num = int(move_match.group(1))  # 1-indexed
            target_di = target_day_num - 1
            target_w = day_weather_map.get(target_di, {})
            if target_w and not target_w.get("outdoor_suitable", True):
                # 目标天也不适宜户外！重写这个问题
                print(f"[天气校验] ⚠️ 问题建议改到第{target_day_num}天，但该天户外不宜，替换为室内建议", flush=True)
                # 尝试提取活动名称
                activity_match = _vre.search(r'(\S+)(?:是户外|为户外|在户外)', text)
                activity = activity_match.group(1) if activity_match else "该户外活动"
                q = dict(q)
                q["question_text"] = f"第{target_day_num}天同样天气不好（户外不宜），{activity}建议换成室内活动或带雨具前往"
                q["options"] = ["换成室内活动", "坚持前往，带雨具", "取消该行程"]
        validated.append(q)
    return validated


def _llm_review_schedule(schedule_result: dict, weather_data: dict, overcrowded_warning: dict = None) -> dict:
    """LLM 审查排程结果，返回 {phase, auto_fixes, questions, risk_flags}。
    如果 LLM 调用失败，返回空审查（不阻塞用户）。
    """
    _ensure_agent()
    try:
        # ── 构建每天天气映射（sorted 保证与 day_index 对应）──
        sorted_weather = sorted((weather_data or {}).items())
        day_weather_map = {}  # day_index → weather_dict
        for di in range(len(schedule_result.get("days", []))):
            if di < len(sorted_weather):
                day_weather_map[di] = sorted_weather[di][1]

        # 构建审查 prompt —— 含每天 POI 统计 + 天气标注
        days_text = ""
        for di, day in enumerate(schedule_result.get("days", [])):
            tl = day.get("timeline", [])
            visit_count = sum(1 for n in tl if n.get("action") == "VISIT")
            total_min = sum(n.get("duration_minutes", 0) for n in tl)

            # ── 提取当天所有 POI 坐标，计算最大间距 ──
            sm = day.get("spatial_matrix", {})
            task_list = day.get("task_list", [])
            poi_coords = []  # [(name, lat, lng), ...]
            for t in task_list:
                tlat = t.get("lat")
                tlng = t.get("lng")
                if tlat and tlng:
                    poi_coords.append((t.get("name", "?"), float(tlat), float(tlng)))
            # 从 spatial_matrix.distances 提取最大距离
            max_dist_km = 0
            max_dist_pair = ""
            distances = sm.get("distances", {})
            for key, val in distances.items():
                d = val.get("distance_m", 0) if isinstance(val, dict) else 0
                if d > max_dist_km:
                    max_dist_km = d
                    max_dist_pair = key
            max_dist_km = round(max_dist_km / 1000, 1)

            # 天气标注：直接在标题行展示，LLM 一眼看到
            w = day_weather_map.get(di, {})
            weather_tag = ""
            if w:
                if w.get("outdoor_suitable"):
                    weather_tag = f" | ☀️{w.get('day_weather', '')} 户外适宜"
                else:
                    weather_tag = f" | 🌧️{w.get('day_weather', '')} 户外不宜"

            dist_warning = ""
            if max_dist_km > 15:
                dist_warning = f" ⚠️最大POI间距{max_dist_km}km（跨城级别！）"
            elif max_dist_km > 5:
                dist_warning = f" ⚠️最大POI间距{max_dist_km}km"
            days_text += f"\n### {day.get('label', '')} （景点{visit_count}个，总时长约{total_min//60}h{total_min%60}min，POI间最大距离{max_dist_km}km）{weather_tag}{dist_warning}\n"
            for node in tl[:10]:
                # 尝试匹配坐标
                coord_str = ""
                memo = node.get('memo', '')
                for pname, plat, plng in poi_coords:
                    if pname and (pname in memo or memo in pname):
                        coord_str = f" [{plat:.4f},{plng:.4f}]"
                        break
                days_text += f"  {node.get('time', '')} {node.get('action', '')} {node.get('memo', '')} ({node.get('category', '')}){coord_str}\n"
            # 追加距离矩阵摘要
            if distances:
                far_pairs = []
                for key, val in distances.items():
                    d = val.get("distance_m", 0) if isinstance(val, dict) else 0
                    if d > 3000:  # >3km
                        far_pairs.append(f"{key}={round(d/1000,1)}km")
                if far_pairs:
                    days_text += f"  远距离POI对: {'; '.join(far_pairs[:5])}\n"

        # 增加一个清晰的天气摘要
        weather_summary_parts = []
        for di in range(len(schedule_result.get("days", []))):
            w = day_weather_map.get(di, {})
            if w:
                label = schedule_result["days"][di].get("label", f"第{di+1}天")
                cond = "户外适宜" if w.get("outdoor_suitable") else "户外不宜"
                weather_summary_parts.append(f"{label}: {w.get('day_weather', '?')} {cond}")
        weather_summary = " | ".join(weather_summary_parts)

        weather_text = ""
        for date_key, w in sorted_weather:
            weather_text += f"  {date_key}: {w.get('day_weather', '?')} {w.get('day_temp', '?')}°C 户外={'适宜' if w.get('outdoor_suitable') else '不宜'}\n"

        system_prompt = """你是一个专业旅行规划审查专家。审查用户的多日行程排程，只输出 JSON。

检查维度：
1. 路线合理：同一天 POI 距离是否合理？有没有跨城的情况？
   - **每个节点旁边的 [lat,lng] 是坐标，标题行注明了当天 POI 间最大距离**
   - 任意两个 POI 间距 > 15km 且交通 > 30min → 必须在 risk_flags 中标出
   - 任意两个 POI 间距 > 30km → 属于跨城级别，在 risk_flags 中标出即可，不要自动 move_to_day
   - 「远距离POI对」列出了当天所有间距 > 3km 的 POI 组合
2. 用餐安排：午餐(11:30-13:30)/晚餐(17:30-19:30)是否在合理位置？是否就近？
3. 天气影响：雨天户外景点是否需要提醒或调整？
   - **⚠️ 关键规则：如果建议改期，目标天的天气标注必须是"户外适宜"，否则禁止建议改期！**
   - 每行标题末尾已标明了天气状态（☀️户外适宜 或 🌧️户外不宜），直接看即可。
   - 如果多天都是"户外不宜"，不要建议改期——改为建议换成室内活动或提醒带雨具。
4. 体力消耗：
   - 每天景点数是否超过 4-5 个？（超过则体验差，应建议分散到其他天）
   - 每天总活动时长是否超过 10 小时？
   - 是否有连续高强度活动（如连续爬山/户外暴走）？
   - 如果某天太满而某天太空，建议重新分配，让每天节奏均匀
5. 偏好匹配：餐厅类型是否符合用户口味？
6. 时间冲突：POI 时间是否合理？

对于可以确定的问题，直接给出 auto_fixes。
对于需要用户决定的问题，给出 questions（每个问题带 options 选项数组）。
没有问题时返回空数组。

**auto_fixes 中每个 fix 必须包含 type 字段**，可选值：
- "move_to_day": 将 POI 移至另一天（需 from_day_index, to_day_index, poi_name）
- "swap": 交换两天的 POI（需 day_a_index, day_b_index, poi_a_name, poi_b_name）
- "reorder": 调整当天顺序（需 day_index, poi_name, new_position）
- "general": 通用建议（仅 detail，无需程序化应用）
**注意：绝不要使用 remove_poi——所有用户选择的目的地都必须保留。所有非餐类目的地必须在 timeline 中有对应的 VISIT 节点。不要通过 move_to_day 以外的方式移动或删除目的地。**

严格按以下 JSON 格式输出（不要包含其他文字）：
{"auto_fixes":[{"type":"move_to_day","from_day_index":0,"to_day_index":1,"poi_name":"八达岭长城","detail":"故宫到八达岭长城60km，建议移至第二天单独游览"}],"questions":[{"question_id":"q1","question_text":"第1天有雷阵雨，长城是户外景点，但第2天天气晴朗，是否把长城改到第2天？","options":["改到第2天","坚持第1天去，带雨具","换成室内活动"]}],"risk_flags":["第2天体力消耗较大","⚠️ 第1天: 故宫→八达岭长城 相距60km，严重跨城！"]}
注意：如果目标日天气标注为"户外不宜"，严禁建议改期到那天，改为建议带雨具或换室内活动。"""

        user_prompt = f"""请审查以下多日行程：

## 天气汇总
{weather_summary}

## 详细天气
{weather_text}

## 排程结果
{days_text}

## 用户偏好
目的地: {session_state.get('trip_destination', '')}
出行方式: {session_state.get('trip_transport', '')}
"""
        # 如果有事前超载警告，注入到 prompt 让 LLM 重点关注
        if overcrowded_warning and overcrowded_warning.get("overcrowded"):
            user_prompt = f"⚠️ **前置警告**：{overcrowded_warning.get('message', '')}\n\n" + user_prompt

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        review_resp = agent._call_llm(messages, max_tokens=800, response_format={"type": "json_object"})
        content = ""
        if hasattr(review_resp, "content"):
            content = review_resp.content or ""
        elif hasattr(review_resp, "choices") and review_resp.choices:
            content = review_resp.choices[0].message.content or ""
        elif isinstance(review_resp, str):
            content = review_resp

        # JSON mode 优先直接解析；fallback 正则
        try:
            parsed = json.loads(content.strip())
        except (json.JSONDecodeError, ValueError):
            import re as _re
            json_match = _re.search(r'\{[\s\S]*\}', content)
            if json_match:
                try:
                    parsed = json.loads(json_match.group())
                except (json.JSONDecodeError, ValueError):
                    print(f"[json_parse] _llm_review_schedule 解析失败，回退空审查: {content[:100]}", flush=True)
                    return {"phase": "done", "auto_fixes": [], "questions": [], "risk_flags": []}
            else:
                print(f"[json_parse] _llm_review_schedule 未找到 JSON，回退空审查: {content[:100]}", flush=True)
                return {"phase": "done", "auto_fixes": [], "questions": [], "risk_flags": []}

        if parsed:
            auto_fixes = parsed.get("auto_fixes", [])
            questions = parsed.get("questions", [])
            risk_flags = parsed.get("risk_flags", [])

            # ── 代码层面校验天气相关问题 ──
            if questions:
                questions = _validate_weather_questions(questions, day_weather_map)

            # 如果有问题，标记为审查交互模式
            phase = "schedule_review" if questions else "done"

            # 缓存审查状态用于后续问答（深拷贝避免后续代码修改 schedule_result 污染快照）
            import copy as _copy
            session_state["_review_state"] = {
                "auto_fixes": auto_fixes,
                "questions": questions,
                "risk_flags": risk_flags,
                "schedule_snapshot": _copy.deepcopy(schedule_result),
            }

            return {
                "phase": phase,
                "auto_fixes": auto_fixes,
                "questions": questions,
                "risk_flags": risk_flags,
            }

        return {"phase": "done", "auto_fixes": [], "questions": [], "risk_flags": []}

    except Exception as e:
        print(f"[LLM审查] 失败，退回纯算法结果: {e}", flush=True)
        return {"phase": "done", "auto_fixes": [], "questions": [], "risk_flags": []}


def _duration(cat: str) -> int:
    return {"hair": 60, "pet": 30, "cafe": 20,
            "restaurant": 60, "gym": 60, "cinema": 120, "laundry": 30,
            "hotel": 480, "scenic": 180, "breakfast": 45, "shopping": 90}.get(cat, 45)


def _try_fast_category_match(user_text: str) -> list:
    """如果用户输入包含明确的品类关键词，直接返回品类列表，跳过 LLM。
    返回空列表表示没有明确匹配，需要走 LLM 慢路径。
    """
    # 品类关键词 → 品类编码（四大demo品类重点加固）
    KEYWORD_TO_CAT = {
        # ── 理发 (hair) ──
        "理发": "hair", "剪头": "hair", "美发": "hair", "造型": "hair",
        "烫头": "hair", "染发": "hair", "剪头发": "hair", "做头发": "hair",
        "洗剪吹": "hair", "剪发": "hair", "理头": "hair", "修刘海": "hair",
        "接发": "hair", "发廊": "hair", "理发店": "hair", "沙龙": "hair",
        # ── 宠物 (pet) ──
        "宠物": "pet", "狗": "pet", "猫": "pet", "洗澡": "pet",
        "洗狗": "pet", "给狗": "pet", "洗猫": "pet", "给猫": "pet",
        "猫咪": "pet", "狗狗": "pet", "犬": "pet", "喵": "pet",
        "宠物美容": "pet", "寄养": "pet", "宠物店": "pet",
        # ── 火锅 (hotpot) ──
        "火锅": "hotpot", "海底捞": "hotpot", "涮肉": "hotpot", "涮羊肉": "hotpot",
        "吃火锅": "hotpot", "涮锅": "hotpot", "铜锅": "hotpot",
        "重庆火锅": "hotpot", "四川火锅": "hotpot", "麻辣烫": "hotpot",
        "涮": "hotpot",
        # ── 日料 (japanese) ──
        "日料": "japanese", "寿司": "japanese", "居酒屋": "japanese",
        "日本料理": "japanese", "拉面": "japanese", "吃日料": "japanese",
        "日式": "japanese", "刺身": "japanese", "生鱼片": "japanese",
        "鳗鱼饭": "japanese", "天妇罗": "japanese", "烧鸟": "japanese",
        "日料店": "japanese", "和牛": "japanese",
        # ── 餐饮 (restaurant) ──
        "川菜": "restaurant", "湘菜": "restaurant", "粤菜": "restaurant",
        "鲁菜": "restaurant", "东北菜": "restaurant", "西北菜": "restaurant",
        "烤肉": "restaurant", "烧烤": "restaurant", "烤鸭": "restaurant",
        "北京菜": "restaurant", "本帮菜": "restaurant", "吃辣": "restaurant",
        "辣的": "restaurant", "餐厅": "restaurant", "吃饭": "restaurant",
        "饭店": "restaurant", "中餐": "restaurant", "馆子": "restaurant",
        "炒菜": "restaurant", "家常菜": "restaurant", "淮扬菜": "restaurant",
        # ── 饮品 (cafe) ──
        "咖啡": "cafe", "奶茶": "cafe", "茶饮": "cafe", "水吧": "cafe",
        "星巴克": "cafe", "瑞幸": "cafe", "喜茶": "cafe", "饮品": "cafe",
        "喝东西": "cafe", "下午茶": "cafe", "喝咖啡": "cafe",
        # ── 酒店/住宿 (hotel) ──
        "酒店": "hotel", "住宿": "hotel", "宾馆": "hotel", "旅馆": "hotel",
        "如家": "hotel", "汉庭": "hotel", "全季": "hotel", "民宿": "hotel",
        "住哪": "hotel", "住": "hotel", "过夜": "hotel", "入住": "hotel",
        "青旅": "hotel", "客栈": "hotel",
        # ── 景点/旅游 (scenic) ──
        "景点": "scenic", "旅游": "scenic", "景区": "scenic", "公园": "scenic",
        "博物院": "scenic", "博物馆": "scenic", "故宫": "scenic", "颐和园": "scenic",
        "天坛": "scenic", "长城": "scenic", "动物园": "scenic", "植物园": "scenic",
        "寺庙": "scenic", "教堂": "scenic", "古镇": "scenic", "游乐园": "scenic",
        "爬山": "scenic", "登山": "scenic", "名胜": "scenic", "古迹": "scenic",
        "西湖": "scenic", "外滩": "scenic", "观光": "scenic", "游玩": "scenic",
        "风景": "scenic", "园林": "scenic", "广场": "scenic", "步行街": "scenic",
        # ── 早餐 (breakfast) ──
        "早餐": "breakfast", "早点": "breakfast", "早饭": "breakfast",
        "吃早饭": "breakfast", "吃早餐": "breakfast", "早茶": "breakfast",
        "豆浆": "breakfast", "油条": "breakfast", "包子铺": "breakfast",
        # ── 购物 (shopping) ──
        "购物": "shopping", "逛街": "shopping", "商场": "shopping",
        "百货": "shopping", "购物中心": "shopping", "买衣服": "shopping",
        "逛街": "shopping", "奥特莱斯": "shopping", "免税店": "shopping",
        "特产": "shopping", "纪念品": "shopping", "伴手礼": "shopping",
        # ── 其他 ──
        "干洗": "laundry", "洗衣服": "laundry", "洗衣": "laundry",
        "健身": "gym", "瑜伽": "gym", "游泳": "gym", "锻炼": "gym",
        "电影": "cinema", "影院": "cinema", "电影院": "cinema", "看电影": "cinema",
        # ── 菜市场 (market) ──
        "菜市场": "market", "买菜": "market", "菜场": "market",
        "农贸市场": "market", "生鲜": "market", "菜市": "market",
        "赶集": "market", "农贸": "market",
    }

    # 虚词过滤：去掉「不想洗澡」「不要辣的」这类否定+无意义词
    _negations = ["不想", "不要", "不吃", "不洗", "不去", "别", "除了"]
    _clean = user_text
    for neg in _negations:
        # 否定词后的品类词不匹配：直接删掉否定短语
        if neg in _clean:
            idx = _clean.index(neg)
            # 删掉否定词及后面紧跟的相关词（如"不想洗澡" → 把"洗澡"也从匹配候选里移除）
            _clean = _clean[:idx] + _clean[idx+len(neg):]

    matched_cats = set()
    for keyword, cat in KEYWORD_TO_CAT.items():
        if keyword in _clean:
            matched_cats.add(cat)

    if not matched_cats:
        return []

    cats = list(matched_cats)
    print(f"[fast-path] 「{user_text[:50]}」→ {cats}", flush=True)
    return cats


def _detect_location_search(user_text: str) -> dict | None:
    """
    检测用户消息是否包含「具体地名 + 搜索意图」。
    返回 {"place": str, "keywords": str} 或 None。

    正例（拦截 — 纯位置搜索，无调度意图）:
    - "故宫附近有什么好玩的" → {"place": "故宫", "keywords": "景点"}
    - "三里屯周边好吃的推荐" → {"place": "三里屯", "keywords": "餐厅"}
    - "望京旁边有咖啡店吗" → {"place": "望京", "keywords": "咖啡"}

    反例（不拦截 — 含调度/修改意图，透传给 LLM 理解）:
    - "故宫好玩吗" → None (问意见)
    - "把故宫加到行程" → None (编辑行程)
    - "帮我在7号上午，在故宫旁边找个好玩的" → None (含日期+调度意图)
    - "你好" → None (寒暄)
    """
    if not user_text or len(user_text.strip()) < 3:
        return None

    text = user_text.strip()

    # 1. 排除行程编辑/调度意图（让 LLM 处理复杂语义）
    # 1a. 显式行程编辑关键词
    trip_edit_keywords = ["加到行程", "添加行程", "修改行程", "删除行程", "取消行程",
                          "加进行程", "加到计划", "加入行程", "加到我的行程",
                          "加进计划", "帮我安排", "给我安排", "安排到", "排到"]
    for kw in trip_edit_keywords:
        if kw in text:
            return None

    # 1b. 日期+时段模式 → 涉及行程调度的可能性高，不拦截
    date_time_patterns = [
        r'\d+号\s*(?:上午|下午|晚上|早上|中午|凌晨)',   # "7号上午", "3号下午"
        r'第\s*\d+\s*天',                              # "第3天", "第 3 天"
        r'(?:今天|明天|后天)\s*(?:上午|下午|晚上|早上)?',  # "明天上午"
    ]
    for p in date_time_patterns:
        if re.search(p, text):
            return None

    # 1c. 跨模式检测：同时包含日期数字 + 调度动作词 → 调度意图
    has_date_ref = bool(re.search(r'\d+号|第\s*\d+\s*天|今天|明天|后天', text))
    schedule_verbs = [r'帮我', r'给我', r'加到', r'安排', r'添加', r'放在', r'加进', r'排到']
    has_schedule_verb = any(re.search(v, text) for v in schedule_verbs)
    if has_date_ref and has_schedule_verb:
        return None

    # 2. 提取「地名 + 位置词」组合
    location_pattern = r'([\w一-龥]{2,20})(?:附近|周边|旁边|一带|那边|这块|跟前|左右)'
    match = re.search(location_pattern, text)
    if not match:
        return None
    place = match.group(1).strip()

    # 3. 检查搜索意图（必须同时满足位置信号 + 搜索意图）
    search_patterns = [
        r'有什么', r'有没有', r'哪有', r'哪里', r'哪儿',
        r'帮我搜', r'帮我找', r'搜一下', r'找一下', r'搜搜',
        r'推荐', r'介绍',
        r'好吃的', r'好玩的', r'好逛的', r'吃的', r'玩的', r'好去处',
        r'逛逛看', r'逛逛', r'逛一逛',
        r'有\S{1,6}吗',  # "有咖啡店吗", "有火锅吗"
        r'有\S{1,6}的',  # "有好吃的", "有玩的"
    ]
    has_search_intent = any(re.search(p, text) for p in search_patterns)
    if not has_search_intent:
        return None

    # 4. 排除纯意见询问（如"XX好玩吗""XX怎么样"）
    opinion_patterns = [r'好玩吗', r'怎么样\s*$', r'值得去吗', r'好不好']
    if any(re.search(p, text) for p in opinion_patterns):
        # 但如果同时有明确搜索词（如"有什么好玩的"），仍然拦截
        if not re.search(r'有什么|帮我找|搜一下|推荐', text):
            return None

    # 5. 提取关键词：优先用用户原话中的品类词
    keywords = "景点"  # 默认（同时用于显示和 API）
    display_keyword = "景点"  # 用户友好的显示名
    # 尝试复用 _try_fast_category_match 的品类映射
    fast_match = _try_fast_category_match(text)
    if fast_match:
        # 取第一个匹配的品类
        from skills.amap_poi.amap_poi import CATEGORY_KEYWORD_MAP
        import main as _main
        for cat in fast_match:
            if cat in CATEGORY_KEYWORD_MAP:
                keywords = CATEGORY_KEYWORD_MAP[cat]  # API 用（如 "景点|公园|博物馆"）
                display_keyword = _main.CATEGORY_NAME_CN.get(cat, cat)  # 显示用（如 "景点"）
                break
    else:
        # 尝试直接从文本提取品类词
        category_words = ["火锅", "咖啡", "奶茶", "理发", "景点", "公园", "商场",
                          "电影", "酒店", "日料", "川菜", "烧烤", "健身", "游泳",
                          "早餐", "超市", "药店", "加油站", "停车"]
        for w in category_words:
            if w in text:
                keywords = w
                display_keyword = w
                break
        # 模糊品类词映射
        if keywords == "景点" and display_keyword == "景点":
            if re.search(r'好吃的|吃的|美食|吃饭|餐厅|饭', text):
                keywords = "餐厅|中餐|快餐|美食"
                display_keyword = "美食"
            elif re.search(r'好逛的|逛街|购物|商场|买东西', text):
                keywords = "购物|商场|商圈"
                display_keyword = "购物"
            elif re.search(r'好玩的|玩的|逛逛|逛一逛', text):
                keywords = "景点|公园|博物馆|景区|名胜|古迹|寺庙"
                display_keyword = "景点"

    return {"place": place, "keywords": keywords, "display_keyword": display_keyword}


def _infer_center_coord() -> str | None:
    """从会话上下文推断用户当前位置坐标。
    优先级: trip_checkin (酒店坐标) > None (回退到 DEFAULT_CENTER_COORD)
    返回 "lng,lat" 字符串或 None。
    """
    lat = session_state.get("trip_checkin_lat")
    lng = session_state.get("trip_checkin_lng")
    if lat is not None and lng is not None:
        return f"{lng},{lat}"
    return None


# 预编译正则：过滤 LLM 文本输出中可能泄漏的 Anthropic-format XML 工具调用标签
_XML_TOOL_TAG_RE = re.compile(r'</?invoke[^>]*>|</?parameter[^>]*>')

def _sanitize_llm_text(text: str) -> str:
    """过滤 LLM 输出中可能泄漏的 XML 工具调用标签"""
    if not text:
        return text
    return _XML_TOOL_TAG_RE.sub('', text).strip()


def _search_poi(agent_instance, user_text: str, profile: dict = None, center_coord: str = None) -> dict:
    """执行 LLM 解析 + POI 搜索，返回 (category_list, poi_data) 或错误。
    profile: 管家记忆偏好谱，用于注入口味/预算参数。
    center_coord: 调用方可显式传入坐标（如从会话上下文推断），用于 LLM 未返回坐标时的默认值；为 None 时回退到 DEFAULT_CENTER_COORD。

    优化：当用户输入包含明确品类关键词时，跳过 LLM 直接搜索（快路径）。
    """
    # —— 每轮搜索清空 cache，避免跨请求污染 ——
    agent_instance.poi_cache = {}
    agent_instance.poi_cache_per_category = {}

    if profile is None:
        profile = {
            "taste": {}, "commute": {}, "budget": {}, "lifestyle": {},
        }

    # 组装偏好注入语段
    taste = profile.get("taste", {})
    budget = profile.get("budget", {})
    cuisine_pref = taste.get("cuisine_preference", [])
    taste_tol = taste.get("taste_tolerance", "")
    diet_res = taste.get("dietary_restrictions", [])
    rating_cutoff = budget.get("rating_cutoff", 4.0)
    price_level = budget.get("price_level", "中端")

    pref_lines = ["用户的长期偏好如下，请在搜索时酌情使用："]
    if taste_tol:
        pref_lines.append(f"- 辣度偏好: {taste_tol}")
    if diet_res:
        pref_lines.append(f"- 忌口/过敏: {', '.join(diet_res)}")
    if cuisine_pref:
        pref_lines.append(f"- 偏好菜系: {', '.join(cuisine_pref)}")
    if rating_cutoff:
        pref_lines.append(f"- 评分底线: {rating_cutoff} 分以上")
    if price_level:
        pref_lines.append(f"- 消费预算: {price_level}")
    pref_text = "\n".join(pref_lines)

    # ═══════════════════════════════════════════════════════════════
    # 快路径：用户输入含明确品类关键词 → 跳过 LLM，直接搜高德
    # ═══════════════════════════════════════════════════════════════
    _fast_categories = _try_fast_category_match(user_text)
    if _fast_categories:
        _used_coord = center_coord or DEFAULT_CENTER_COORD
        if not center_coord:
            print(f"[coord] 快路径无调用方坐标，使用默认 {DEFAULT_CENTER_COORD}，用户输入: {user_text[:50]}", flush=True)
        print(f"[fast-path] 命中品类关键词: {_fast_categories}，跳过 LLM", flush=True)
        all_results = {}
        for cat in _fast_categories:
            search_res = backend.skill_poi.search_poi_matrix(
                center_coord=_used_coord,
                categories=[cat],
                radius_meters=3000,
                min_rating=rating_cutoff,
                price_level=price_level,
                dietary_restrictions=diet_res if diet_res else None,
            )
            if search_res.get("status") == "SUCCESS":
                for c, shops in search_res["search_results"].items():
                    if c not in all_results:
                        all_results[c] = []
                    all_results[c].extend(shops)
                    for shop in shops:
                        agent_instance.poi_cache[shop["shop_id"]] = shop

        agent_instance.poi_cache_per_category = {}
        for sid, shop in agent_instance.poi_cache.items():
            cat = shop.get("category")
            agent_instance.poi_cache_per_category.setdefault(cat, []).append(shop)

        return {"categories": list(all_results.keys()), "results": all_results}

    # ═══════════════════════════════════════════════════════════════
    # 慢路径：走 LLM 解析（模糊语义/多目的地/抽象描述）
    # ═══════════════════════════════════════════════════════════════

    system_prompt_1 = {
        "role": "system",
        "content": f"""{pref_text}

你是一个生活秘书。第一步必须调用 search_poi 搜索各品类商户。

## 品类映射规则
理发/美发/造型/沙宣->hair，宠物/狗/猫/洗澡/宠物店->pet，咖啡/奶茶/茶饮/水吧->cafe，
健身/瑜伽/游泳->gym，餐饮/吃饭/餐厅/中餐->restaurant，电影/影院->cinema，
洗衣/干洗->laundry，火锅/海底捞/吃火锅->hotpot，日料/寿司/居酒屋->japanese。

## 模糊语义处理（核心能力）
当用户使用抽象/口语描述时，你必须推理出「用户真正想找的场所类型」，转为具体可搜索的关键词。**严禁把形容词、感受词直接放进 keywords。**

翻译规则：
- "安静的地方看书" → 推导：可能是图书馆、书店、书吧 → keywords: "图书馆|书店|书吧"
- "适合小孩玩" → 推导：游乐园、儿童乐园、亲子 → keywords: "游乐园|儿童乐园|亲子"
- "有变形金刚的游乐园" → keywords: "变形金刚|主题乐园"
- "圆的湖能玩帆船" → 推导：公园、湖泊景区 → keywords: "公园|湖|帆船"
- "浪漫的约会餐厅" → keywords: "西餐|日料|观景餐厅"
- "便宜又好吃的" → 同时设 min_rating 为 3.5，categories 按品类填

**关键原则：keywords 里必须是高德地图能搜到的场所名称/类型词，不能是"安静""浪漫""舒服""好玩"这类形容词。**

## 多目的地串联
用户一句话含多个目的地时，每次 tool call 代表一个目的地，可分多次调用 search_poi：
- "去环球影城吃冰淇淋然后去世贸天阶买蛋糕"：
  第1次: keywords: "环球影城|冰淇淋", categories: []
  第2次: keywords: "世贸天阶|蛋糕|烘焙", categories: []
- 每个目的地独立搜索，便于后续分别排程。

min_rating 参数请设为 {rating_cutoff}。"""
    }
    agent_instance.context_memory = [system_prompt_1, {"role": "user", "content": user_text}]

    tools_poi = [{
        "type": "function",
        "function": {
            "name": "search_poi",
            "parameters": {
                "type": "object",
                "properties": {
                    "categories": {"type": "array", "items": {"type": "string"}, "description": "品类编码数组，如 ['hair','cafe']。模糊描述时可传空数组"},
                    "keywords": {"type": "string", "description": "模糊语义关键词，如'变形金刚 游乐园''安静的咖啡厅'。品类明确时可不填"},
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
    while not msg.tool_calls and retry_p1 < 2:
        retry_p1 += 1
        agent_instance.context_memory.append({"role": "assistant", "content": msg.content or ""})
        agent_instance.context_memory.append({"role": "user", "content": "请调用 search_poi 工具搜索对应品类商户，不要用文字回答。"})
        msg = agent_instance._call_llm(agent_instance.context_memory, tools=tools_poi)

    if not msg.tool_calls:
        return {"error": "LLM 未调用搜索工具。"}

    agent_instance.context_memory.append(msg)

    all_results = {}
    llm_keywords = ""  # P0-1: 模糊关键词，在循环和重试中复用
    for tool_call in msg.tool_calls:
        args = json.loads(tool_call.function.arguments)
        raw_cats = args.get("categories", [])
        mapped_cats = list(set([backend.CATEGORY_MAP.get(c, c) for c in raw_cats]))

        # 坐标兜底：LLM 可能传中文地名，直接 fallback 到默认坐标
        raw_coord = args.get("center_coord", "")
        coord = raw_coord
        if raw_coord:
            parts = raw_coord.strip().split(",")
            if len(parts) != 2:
                print(f"[coord] 坐标格式无效 '{raw_coord[:50]}'，回退到默认 {DEFAULT_CENTER_COORD}", flush=True)
                coord = DEFAULT_CENTER_COORD
            else:
                try:
                    float(parts[0].strip())
                    float(parts[1].strip())
                except ValueError:
                    print(f"[coord] 坐标解析失败 '{raw_coord[:50]}'，回退到默认 {DEFAULT_CENTER_COORD}", flush=True)
                    coord = DEFAULT_CENTER_COORD
        if not coord or not coord.strip():
            coord = center_coord or DEFAULT_CENTER_COORD
            source = "调用方传入" if center_coord else "默认"
            print(f"[coord] 坐标为空，回退到{source} {coord}，用户输入: {user_text[:50]}", flush=True)

        fallback_min_rating = budget.get("rating_cutoff", 0)
        fallback_price_level = budget.get("price_level", None)
        # price_level 仅对餐饮品类生效（hair/pet/cafe/gym/cinema/laundry 不过滤）
        food_cats = {"restaurant", "hotpot"}
        effective_price_level = fallback_price_level if any(c in food_cats for c in mapped_cats) else None
        llm_keywords = args.get("keywords", "").strip() if isinstance(args.get("keywords"), str) else ""
        # 强制过滤：把 LLM 可能误传的抽象形容词从关键词中剔除
        _abstract_words = ['安静', '舒服', '好玩', '便宜', '浪漫', '好吃', '好看', '方便', '近', '快',
                           '看书', '学习', '工作', '约会', '聊天', '休息', '发呆', '放松', '拍照',
                           '热闹', '人少', '小众', '网红', '高级', '温馨', '干净', '大', '小', '新']
        for _aw in _abstract_words:
            llm_keywords = llm_keywords.replace(_aw, '')
        llm_keywords = ' '.join(llm_keywords.split())
        # 过滤后关键词太短或只是品类名 → 清空，退回品类自动关键词
        if llm_keywords and len(llm_keywords) <= 3:
            llm_keywords = ''
        # 兜底：LLM 没给关键词时，用清洗后的用户输入
        if not llm_keywords:
            cleaned = user_text
            # 去掉虚词
            for filler in ['有', '的', '一个', '那个', '哪个', '帮我', '我想', '我要', '找', '一下',
                           '附近', '周边', '有没有', '哪里', '什么地方', '怎么', '和', '想', '个']:
                cleaned = cleaned.replace(filler, ' ')
            # 去掉抽象形容词（API 不认）
            for adj in ['安静', '舒服', '好玩', '便宜', '浪漫', '好吃', '好看', '方便', '近', '快']:
                cleaned = cleaned.replace(adj, ' ')
            cleaned = ' '.join(cleaned.split())
            llm_keywords = cleaned if cleaned else user_text
        print(f"[DEBUG search_poi] LLM args: {json.dumps(args, ensure_ascii=False)}, keywords={llm_keywords!r}", flush=True)
        # 模糊搜索时放宽评分+扩大半径（关键词优先于精确匹配）
        effective_min_rating = args.get("min_rating", fallback_min_rating)
        effective_radius = args.get("radius_meters", 3000)
        if llm_keywords:
            effective_min_rating = min(effective_min_rating, 3.0)
            effective_radius = max(effective_radius, 10000)  # 模糊搜索扩大范围
        search_res = backend.skill_poi.search_poi_matrix(
            center_coord=coord,
            categories=mapped_cats if mapped_cats else (["restaurant"] if not llm_keywords else ["restaurant"]),
            radius_meters=effective_radius,
            min_rating=effective_min_rating,
            price_level=effective_price_level,
            dietary_restrictions=diet_res if diet_res else None,
            keywords=llm_keywords if llm_keywords else None,
        )

        if search_res.get("status") == "SUCCESS":
            for cat in search_res["search_results"]:
                print(f"[DEBUG search_poi] cat={cat} got {len(search_res['search_results'][cat])} shops", flush=True)
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

    # 如果所有 tool_call 都失败了，用默认坐标重试一次
    if not all_results:
        fallback_cats = []
        for tool_call in msg.tool_calls:
            args = json.loads(tool_call.function.arguments)
            raw_cats = args.get("categories", [])
            fallback_cats.extend([backend.CATEGORY_MAP.get(c, c) for c in raw_cats])
        if fallback_cats:
            fallback_cats = list(set(fallback_cats))
            # 重试时同样只对餐饮品类应用 price_level
            retry_price_level = fallback_price_level if any(c in food_cats for c in fallback_cats) else None
            retry_res = backend.skill_poi.search_poi_matrix(
                center_coord=DEFAULT_CENTER_COORD,
                categories=fallback_cats if fallback_cats else ["restaurant"],
                radius_meters=10000 if llm_keywords else 3000,
                min_rating=min(fallback_min_rating, 3.0) if llm_keywords else fallback_min_rating,
                price_level=retry_price_level,
                keywords=llm_keywords if llm_keywords else None,
            )
            if retry_res.get("status") == "SUCCESS":
                for cat, shoplist in retry_res["search_results"].items():
                    all_results[cat] = shoplist
                    for shop in shoplist:
                        agent_instance.poi_cache[shop["shop_id"]] = shop

    # 按品类分组
    agent_instance.poi_cache_per_category = {}
    for sid, shop in agent_instance.poi_cache.items():
        cat = shop.get("category")
        agent_instance.poi_cache_per_category.setdefault(cat, []).append(shop)

    return {"categories": list(all_results.keys()), "results": all_results}


def _build_categories_for_frontend(agent_instance, profile: dict = None) -> list:
    """将 poi_cache_per_category 转为前端需要的格式（top3 + 评分），偏好品类排前"""
    # 偏好菜系优先排序
    cuisine_pref = []
    if profile:
        cuisine_pref = profile.get("taste", {}).get("cuisine_preference", [])
    # cuisine_preference 是中文偏好（如"日料", "轻食"），需映射到 category 编码
    pref_cats = set()
    for cn in cuisine_pref:
        for cat_val, cn_val in backend.CATEGORY_NAME_CN.items():
            if cn_val == cn or cn_val in cn or cn in cn_val:
                pref_cats.add(cat_val)
        # 也尝直接匹配 category 编码
        if cn in backend.CATEGORY_NAME_CN:
            pref_cats.add(cn)

    def _sort_key(item):
        cat = item[0]
        # 偏好品类排前
        return (0 if cat in pref_cats else 1, cat)

    sorted_cats = sorted(agent_instance.poi_cache_per_category.items(), key=_sort_key)

    result = []
    for cat, shops in sorted_cats:
        sorted_shops = sorted(shops, key=lambda s: s.get("rating", 0), reverse=True)
        top_n = sorted_shops[:3]
        shops_data = []
        for s in top_n:
            # ── 距离计算：Haversine from 参考点 (39.93, 116.45) ──
            dist_m = 0
            raw_coord = s.get("coord", "")
            # 兜底：从 lat + lng 构造 coord
            if (not raw_coord or "," not in raw_coord) and s.get("lat") and s.get("lng"):
                raw_coord = f"{s['lat']},{s['lng']}"
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
            dist_str = f"{dist_m}m" if dist_m < 1000 else f"{dist_m/1000:.1f}km" if dist_m > 0 else "?m"
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
                "coord": raw_coord if (raw_coord and "," in raw_coord) else "",
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

@app.after_request
def _add_no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/")
def index():
    resp = send_from_directory(base_dir, "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/start", methods=["POST"])
def api_start():
    """阶段 1: 接收用户文字 → 读偏好谱 → LLM解析 → POI搜索 → 返回品类+店铺列表给前端选择"""
    global agent, session_state
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "请输入需求"}), 400

    _reset_session()
    agent.context_memory = []
    session_state["user_input"] = text

    # 读取长期偏好谱并暂存
    profile = _read_profile()
    session_state["_profile"] = profile

    result = _search_poi(agent, text, profile)
    if "error" in result:
        return jsonify({"error": result["error"]}), 500

    categories = _build_categories_for_frontend(agent, profile)
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

    # P0-2: auto 模式 — 自动选店+排程，一步到位
    auto_mode = data.get("auto", False)
    if auto_mode:
        # 每个品类自动选评分最高的店
        auto_pairs = []
        for cat, shops in agent.poi_cache_per_category.items():
            if shops:
                best = max(shops, key=lambda s: s.get("rating", 0))
                auto_pairs.append((cat, best["shop_id"], best["name"]))
        if auto_pairs:
            session_state["selected_pairs"] = auto_pairs
            session_state["transport"] = data.get("transport", "步行")
            session_state["phase"] = "running"
            return _run_schedule_from_session()

    # 交互模式：返回品类列表让用户选
    return jsonify({
        "phase": "choose_shop",
        "categories": categories,
        "auto_available": len(session_state.get("searched_categories", [])) > 0,
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


# ======================================================================
# P0-3 + P0-4: 多轮对话路线编辑 + 行中动态修改目的地
# ======================================================================

def _shop_name_matches(candidate_name, keyword):
    """精确 > 前缀 > 子串（仅当关键词 >= 3 字时启用子串匹配，防止短词误匹配）"""
    cn = candidate_name.lower()
    kw = keyword.lower()
    if cn == kw:
        return True
    if cn.startswith(kw):
        return True
    if len(kw) >= 3 and kw in cn:
        return True
    return False


def _looks_like_swap_intent(text):
    """检测文本是否包含换店意图（用于 clarify 阶段判断是否应返回 clarify_swap_target）"""
    return bool(
        re.search(r'换.{0,2}(?:店|家|个|一家)', text) or
        '有没有其他' in text or '别的' in text or '其他店' in text or
        re.search(r'(?:这家|这个|这家店|不要这个|不喜欢这个|不想要这个)', text)
    )


def _resolve_swap_target(selected_pairs, target_name=None, target_category=None, target_shop_id=None):
    """解析换店目标：按 shop_id > name > category 优先级匹配。
    返回 (matched_stop, unresolved_pairs) 或 (None, selected_pairs)。
    matched_stop 是 (category, shop_id, shop_name) 元组。
    """
    if target_shop_id:
        for p in selected_pairs:
            if p[1] == target_shop_id:
                remaining = [x for x in selected_pairs if x[1] != target_shop_id]
                return p, remaining
        return None, selected_pairs

    if target_name:
        for p in selected_pairs:
            if _shop_name_matches(p[2], target_name):
                remaining = [x for x in selected_pairs if x[1] != p[1]]
                return p, remaining
        return None, selected_pairs

    if target_category:
        for p in selected_pairs:
            if p[0] == target_category:
                remaining = [x for x in selected_pairs if x[1] != p[1]]
                return p, remaining
        return None, selected_pairs

    # 无目标 + 仅1个 stop → 直接用
    if len(selected_pairs) == 1:
        return selected_pairs[0], []

    # 无目标 + 多个 stop → 需要追问
    return None, selected_pairs


def _fallback_parse_edit(edit_text, selected_pairs=None):
    """规则兜底：当 LLM 解析失败时，用关键词匹配推断用户意图。
    返回 {"action": ..., "params": {...}}，无法判定时返回 clarify。
    """
    text = edit_text.strip()

    # ── 品类关键词映射（置顶，供 swap + add_stop 共用） ──
    _CAT_KEYWORDS = {
        '理发': 'hair', '剪头': 'hair', '美发': 'hair',
        '咖啡': 'cafe', '奶茶': 'cafe', '茶': 'cafe', '喝杯': 'cafe',
        '宠物': 'pet', '狗': 'pet', '猫': 'pet',
        '健身': 'gym', '运动': 'gym',
        '火锅': 'hotpot',
        '日料': 'japanese', '日式': 'japanese', '寿司': 'japanese',
        '电影': 'cinema', '看片': 'cinema',
        '干洗': 'laundry', '洗衣': 'laundry',
        '餐饮': 'restaurant', '吃饭': 'restaurant', '美食': 'restaurant',
        '菜市场': 'market', '买菜': 'market', '菜场': 'market',
        '农贸市场': 'market', '生鲜': 'market',
    }

    # ── 跨品类替换（"把火锅改成理发"）──
    # 优先级最高：在换店检测之前，因为"改成"语义更强
    m_replace = re.search(r'(?:把|将)\s*([^\s，。！？、…]{1,8}?)\s*(?:改成|换成|换成去|改为|换为|变成)\s*([^\s，。！？、…]{1,8})', text)
    if not m_replace:
        # 无"把/将"前缀："火锅换成理发"（要求 old 部分是品类关键词，降低误匹配）
        m_replace = re.search(r'(?:把|将)?\s*([^\s，。！？、…]{1,8}?)\s*(?:改成|换成|换成去|改为|换为|变成)\s*([^\s，。！？、…]{1,8})', text)
    if not m_replace:
        # 反向模式："不去X了(改为)去Y"
        m_replace = re.search(r'(?:不去|不想去|不要)\s*([^\s，。！？、…]{1,8}?)(?:了|啦)?\s*(?:改成|换成|去|换)\s*([^\s，。！？、…]{1,8})', text)
    if m_replace:
        old_part = m_replace.group(1).strip()
        new_part = m_replace.group(2).strip()
        # 识别 old_part 的品类（用于 remove）
        old_cat = _CAT_KEYWORDS.get(old_part, "")
        # 识别 new_part 的品类和关键词
        new_cat = _CAT_KEYWORDS.get(new_part, "")
        new_kw = new_part
        return {"action": "replace_stop", "params": {
            "remove_name": old_part,
            "remove_category": old_cat,
            "add_keywords": new_kw,
            "add_category": new_cat
        }}

    # ── 换店 ──
    # 先尝试从文本中提取目标（品类或店名），裸"换一家"不在此返回，留给 LLM 解析
    swap_target_category = None
    swap_target_name = None

    # 品类提取："换一家咖啡" / "换个理发"
    for kw, cat in _CAT_KEYWORDS.items():
        if kw in text:
            swap_target_category = cat
            break

    # 店名提取："把海底捞换了" / "换掉XX" / "不想去XX了" / "不要XX" / "别去XX"
    m_name = re.search(r'(?:换掉|换了|不想去|不要|别去|去掉)\s*([^\s，。！？、…]{2,10})', text)
    if m_name:
        swap_target_name = m_name.group(1).strip()
    # "换一家XX" / "换个XX" → XX 可能是店名
    m_name2 = re.search(r'换.{0,2}(?:店|家|个|一家)\s*([^\s，。！？、…]{2,10})', text)
    if m_name2 and not swap_target_name:
        candidate = m_name2.group(1).strip()
        # 排除掉本身就是品类词的情况（如"换一家咖啡"→"咖啡"是品类不是店名）
        if candidate not in _CAT_KEYWORDS:
            swap_target_name = candidate

    # 换店模式分类：
    # - explicit: "换一家"、"换个别的"、"有没有其他" → L1 可处理，handler 负责解析目标
    # - pronoun: "这家不要"、"不喜欢这个" → 需要 LLM 对话上下文来消解指代
    is_explicit_swap = bool(
        re.search(r'换.{0,2}(?:店|家|个|一家)', text) or
        '有没有其他' in text or '别的' in text or '其他店' in text
    )
    is_pronoun_swap = bool(
        re.search(r'(?:这家|这个|这家店|不要这个|不喜欢这个|不想要这个)', text)
    )

    if is_explicit_swap:
        if swap_target_category or swap_target_name:
            # 有明确目标 → 直接返回带 target 的 swap_current，跳过 LLM
            params = {}
            if swap_target_category:
                params["target_category"] = swap_target_category
            if swap_target_name:
                params["target_name"] = swap_target_name
            return {"action": "swap_current", "params": params}
        # 裸"换一家"无目标 → 仍返回 swap_current，但 params 为空
        # handler 层 _resolve_swap_target 会处理：
        #   1个stop → 直接用 → 搜候选 → swap_selection
        #   多个stop → 返回 clarify_swap_target 追问
        return {"action": "swap_current", "params": {}}

    if is_pronoun_swap:
        # 代词指代 → 不在此返回（pass），让后续逻辑落入 clarify
        # L2 LLM 利用对话上下文解析"这家"指的是哪个 stop
        # 如果 LLM 也无法确定 → L3 clarify → handler 判断后返回 clarify_swap_target
        pass

    # ── 放弃修改 ──
    if re.search(r'还是|算了|不换|就这个|就这样|不改|不换|不用了|就它吧|就这家|听你的|不折腾了', text):
        return {"action": "no_change", "params": {}}

    # ── 删除 ──
    m = re.search(r'(?:不去|删掉?|取消|去掉|移除)\s*(.{1,10}?)\s*(?:了|吧|吗|$)', text)
    if m:
        name = m.group(1).strip()
        # 拒绝空 name、单字 name、纯标点/空白 name
        if name and len(name) >= 2 and not re.match(r'^[\s，。！？、…]+$', name):
            return {"action": "remove_stop", "params": {"name": name}}

    # ── 交通方式 ──
    if '打车' in text or '出租车' in text:
        return {"action": "change_transport", "params": {"mode": "TAXI"}}
    if '地铁' in text or '公交' in text or '坐公交' in text or '搭公交' in text or '坐地铁' in text or '搭地铁' in text:
        return {"action": "change_transport", "params": {"mode": "METRO"}}
    if '走路' in text or '步行' in text:
        return {"action": "change_transport", "params": {"mode": "WALK"}}
    if '开车' in text or '驾车' in text:
        return {"action": "change_transport", "params": {"mode": "DRIVE"}}
    if '骑单车' in text or '骑行' in text or '骑车' in text:
        return {"action": "change_transport", "params": {"mode": "WALK"}}  # 非机动车最近似步行模式

    # ── 时间 ──
    m = re.search(r'(?:推迟|延迟|推后).{0,3}?(\d+)\s*(?:分钟|分)', text)
    if m:
        return {"action": "change_time", "params": {"time": f"+{m.group(1)}"}}
    m = re.search(r'(?:提前|提早).{0,3}?(\d+)\s*(?:分钟|分)', text)
    if m:
        return {"action": "change_time", "params": {"time": f"-{m.group(1)}"}}
    m = re.search(r'(?:改成|改到|调到|换到).{0,3}?(\d{1,2}:\d{2})', text)
    if m:
        t = m.group(1)
        parts = t.split(':')
        if 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59:
            return {"action": "change_time", "params": {"time": t}}

    # ── 提醒（优先级高于新增/换店，因为"提醒我去XX"是提醒意图而非搜索意图）──
    _reminder_time = None
    m1 = re.search(r'(?:上午|下午|晚上|早上|中午)?\s*(\d{1,2})\s*[:：点]\s*(?:(\d{0,2})\s*)?(?:分\s*)?(?:提醒|记得|别忘了)', text)
    if m1:
        hour = int(m1.group(1))
        minute = int(m1.group(2)) if m1.group(2) else 0
        _reminder_time = f"{hour:02d}:{minute:02d}"
    m2 = re.search(r'(?:提醒|记得|别忘了)\s*(?:我\s*)?(?:在\s*)?(?:上午|下午|晚上|早上|中午)?\s*(\d{1,2})\s*[:：点]\s*(?:(\d{0,2})\s*)?(?:分\s*)?', text)
    if m2 and not _reminder_time:
        hour = int(m2.group(1))
        minute = int(m2.group(2)) if m2.group(2) else 0
        _reminder_time = f"{hour:02d}:{minute:02d}"
    if _reminder_time or (('提醒' in text or '记得' in text or '别忘了' in text) and not any(kw in text for kw in ['换', '改', '删', '不去', '交通', '打车', '地铁', '走路', '步行', '开车', '路线', '高速'])):
        _reminder_label = "提醒"
        _label_match = re.search(r'(?:提醒|记得|别忘了)(?:我\s*)?(?:在?\s*\d{1,2}[:：点]\s*(?:\d{0,2})?\s*(?:分\s*)?)?(?:去\s*)?(.{1,6}?)(?:$|[，。！？、…\s])', text)
        if _label_match:
            candidate = _label_match.group(1).strip()
            if candidate and len(candidate) >= 1 and candidate not in ('了', '吧', '吗', '啊', '呢', '呀'):
                _reminder_label = candidate
        return {"action": "add_reminder", "params": {
            "type": "CUSTOM",
            "time": _reminder_time or "12:00",
            "label": _reminder_label,
            "repeat": "once",
            "note": edit_text
        }}

    # ── 新增（品类关键词，复用函数顶部的 _CAT_KEYWORDS） ──
    for kw, cat in _CAT_KEYWORDS.items():
        if kw in text:
            return {"action": "add_stop", "params": {"keywords": kw, "category": cat, "shop_name": ""}}

    # ── 路线 ──
    if '高速' in text or '走近路' in text or '换路线' in text or '换条路' in text:
        pref = 'avoid_highway' if '不走高速' in text or '避免高速' in text else 'fast'
        return {"action": "reroute", "params": {"preference": pref}}

    # ── 包含"去"字可能是新增（但排除代词停用词） ──
    _ADD_STOP_STOPWORDS = {'这家', '那家', '那里', '这里', '一下', '一个', '几个', '一些'}
    m = re.search(r'(?:去|加|添加|加入|再来一个)\s*([^\s，。！？、…]{1,8})', text)
    if m:
        kw = m.group(1).strip()
        if kw and kw not in _ADD_STOP_STOPWORDS and len(kw) >= 2:
            return {"action": "add_stop", "params": {"keywords": kw, "category": "", "shop_name": ""}}

    # ── 默认：追问 ──
    return {"action": "clarify", "params": {"message": "抱歉，我没太理解您的意思，能再说一遍吗？"}}

@app.route("/api/edit_trip", methods=["POST"])
def api_edit_trip():
    """行程编辑端点：用户用自然语言微调已有行程。
    支持：改路线/换交通/增删目的地/调整时间/换偏好。
    """
    global agent, session_state
    _ensure_agent()
    data = request.get_json(silent=True) or {}
    edit_text = (data.get("text") or "").strip()
    # 前端直接传的目标店铺 ID（用户点击了 clarify_swap_target 选择器）
    swap_target_shop_id = (data.get("swap_target_shop_id") or "").strip()
    if not edit_text and not swap_target_shop_id:
        return jsonify({"error": "请输入编辑指令"}), 400

    if session_state.get("phase") != "done" and not session_state.get("selected_pairs"):
        return jsonify({"error": "没有活跃行程，请先发起一个行程"}), 400

    # ═══════════════════════════════════════════════════════════════
    # 多品类快路径：用户一句话含多个品类（如"理发顺便带狗洗澡"）
    # 跳过 LLM，直接搜索全部品类并返回 multi_shop_selection
    # ═══════════════════════════════════════════════════════════════
    _multi_cats = _try_fast_category_match(edit_text)
    _looks_like_add = any(kw in edit_text for kw in [
        "想", "要", "帮我", "推荐", "找", "搜", "加", "去", "安排",
        "顺便", "还有", "以及", "另外", "再", "也", "带", "顺便带",
    ])
    _has_swap_signal = any(kw in edit_text for kw in [
        "换", "不要", "取消", "删", "改", "代替", "替换", "换成",
    ])
    if len(_multi_cats) >= 2 and _looks_like_add and not _has_swap_signal:
        print(f"[edit_trip] 多品类快路径: {_multi_cats}，跳过 LLM", flush=True)
        rating_cutoff = 3.5
        profile = _load_profile_memory()
        if profile:
            rating_cutoff = profile.get("budget", {}).get("rating_cutoff", 3.5)
        all_category_results = []
        for cat in _multi_cats:
            try:
                search_res = backend.skill_poi.search_poi_matrix(
                    center_coord=DEFAULT_CENTER_COORD,
                    categories=[cat],
                    radius_meters=5000,
                    min_rating=rating_cutoff,
                )
                if search_res.get("status") == "SUCCESS":
                    for c, shops in search_res.get("search_results", {}).items():
                        if shops:
                            shop_list = []
                            for s in shops[:3]:
                                s["coord"] = f"{s.get('lat','')},{s.get('lng','')}"
                                agent.poi_cache[s["shop_id"]] = s
                                shop_list.append(s)
                            all_category_results.append({
                                "category": c,
                                "label": backend.CATEGORY_NAME_CN.get(c, c),
                                "shops": shop_list,
                            })
            except Exception as e:
                print(f"[edit_trip] 多品类快路径搜索 {cat} 失败: {e}", flush=True)

        if all_category_results:
            return jsonify({
                "phase": "multi_shop_selection",
                "categories": all_category_results,
                "message": "已为您找到以下店铺：",
            })
        else:
            return jsonify({"error": "附近未找到相关店铺，请换个关键词试试"}), 404

    # 用 LLM 解析编辑意图
    # ── 确认回调：用户已确认之前缓存的待确认操作 ──
    confirm_action = data.get("confirm_action")
    skip_llm = False
    if confirm_action and session_state.get("_pending_confirmation"):
        if confirm_action == "execute":
            pending = session_state.pop("_pending_confirmation")
            action = pending.get("action", "clarify")
            params = pending.get("params", {})
            llm_success = True
            skip_llm = True
            print(f"[edit_trip] 用户确认执行: action={action}", flush=True)
        elif confirm_action == "revise":
            pending = session_state.pop("_pending_confirmation")
            revise_note = (data.get("revise_note") or "").strip()
            print(f"[edit_trip] 用户修正: {revise_note}", flush=True)
            edit_text = f"（修正）上次我说「{edit_text}」，但你的理解不对。{revise_note}。请重新理解。"
            # 继续走下面的 LLM 解析流程
    current_plan_desc = ""
    for cat, sid, sname in session_state.get("selected_pairs", []):
        current_plan_desc += f"- {sname} ({cat})\n"

    # 对话历史（前端传入，用于指代消解）
    context = (data.get("context") or "").strip()

    # 用户 prompt：仅包含当前上下文，规则和示例已在 system prompt 中
    edit_prompt = f"""## 对话历史（用于理解指代）
{context if context else '（无历史）'}

## 当前行程
{current_plan_desc if current_plan_desc else '（行程为空，只能执行 add_stop 操作）'}

## 用户编辑指令
{edit_text}

请返回 JSON（只返回 JSON，不要其他文字）："""

    edit_messages = [
        {"role": "system", "content": """你是行程编辑助手，负责将用户的自然语言编辑指令解析为精确的 JSON 操作。

## 规则总览（10条）
1. swap_current: 用户想替换当前行程中的某个目的地（同品类换店）。必须从用户文本和对话上下文中解析出具体目标：
   - 用户说"换一家咖啡"→ target_category: "cafe"
   - 用户说"把海底捞换了"→ target_name: "海底捞"
   - 用户说"这家不要"且上文AI推荐了某店→ target_name: 那家店名
   - 用户说"换一家"且行程只有1个目的地→ target_category: 那个目的地的品类
   - 用户说"换一家"且行程有多个目的地→ 必须返回 clarify！追问具体换哪个
   params: {target_category: 品类代码或空, target_name: 店名或空}
   ⚠️ 无法确定目标且有多个目的地时，必须返回 clarify！
2. no_change: 用户放弃修改（"还是去这家吧"、"算了不换了"、"就这个吧"）
3. clarify: 用户意图模糊，无法确定具体操作——返回追问消息。模糊输入如"嗯..."、"那个..."、"emmm"都应返回 clarify
4. replace_stop: 用户想把某个目的地换成不同品类（跨品类替换）。⚠️ 与 swap_current 的区别：replace_stop 是新老品类不同！如"把火锅改成理发"=跨品类替换，不是同品类换店。
   params: {remove_name: 要去掉的目的地名称或品类, remove_category: 要去掉的品类代码或空, add_keywords: 新品类搜索关键词, add_category: 新品类代码或空}
   示例："把火锅改成理发"→ remove_name="火锅", add_keywords="理发", add_category="hair"
5. add_stop: 新增目的地。⚠️ 注意：如果用户输入以"提醒"/"记得"/"别忘了"开头或为主意图，应识别为 add_reminder 而非 add_stop。
   params: {keywords: 搜索关键词, category: 品类代码或空字符串, shop_name: 用户明确提到的店名，没提则为空字符串}
   品类代码: hair=理发, pet=宠物, cafe=咖啡, gym=健身, restaurant=餐饮, cinema=电影, laundry=干洗, hotpot=火锅, japanese=日料, market=菜市场
6. remove_stop: 删除目的地。params: {name: 要删的店名关键词，至少2个字}
7. change_time: 调整时间。params: {time: HH:MM 或 "now" 或 "+30"（推迟30分钟）或 "-15"（提前15分钟）}
8. change_transport: 切换交通方式。params: {mode: WALK|TAXI|METRO|DRIVE}
9. reroute: 修改路线偏好。params: {preference: fast|short|scenic|avoid_highway}
10. 格式要求：只返回 JSON，不要任何解释文字、markdown 代码块标记或前后缀
11. add_reminder: 用户要求设置提醒（不是修改行程）。⚠️ 识别要点：含"提醒"、"记得"、"别忘了"等关键词的输入，即使其中包含"去"字，也优先识别为提醒而非新增目的地。
    params: {type: "CUSTOM"或"MED"或"WATER", time: "HH:MM"格式的时间, label: 提醒内容标签, repeat: "once"或"daily"}
    示例："下午三点提醒我去"→ type="CUSTOM", time="15:00", label="去"
    示例："记得8点提醒我买菜"→ type="CUSTOM", time="08:00", label="买菜"
    示例："提醒我吃药"→ type="MED", time="当前时间", label="吃药"

## 指代消解规则
- 利用对话历史理解"这家"、"那个"、"它"等代词
- 如果对话历史中 AI 刚推荐了某家店，用户说"这家不要"或"换一家" → swap_current
- 如果对话历史中 AI 刚推荐了某家店，用户说"还是这家吧"或"就它了" → no_change
- 用户说"那还是去这家吧"是对之前换店意图的撤销 → no_change

## 示例（10-shot）

用户: "换一家吧不想去这家"
→ {"action": "swap_current", "params": {}}

// 说明：上方示例中用户未指定具体目标，但若行程仅1个目的地则可直接 swap；
// 若行程有多个目的地，LLM 应返回 clarify 追问"您想换哪一个？"

用户: "换一家咖啡"
→ {"action": "swap_current", "params": {"target_category": "cafe"}}

用户: "把海底捞换了"
→ {"action": "swap_current", "params": {"target_name": "海底捞"}}

用户: "那还是去这家吧"
→ {"action": "no_change", "params": {}}

用户: "帮我加一个理发"
→ {"action": "add_stop", "params": {"keywords": "理发", "category": "hair", "shop_name": ""}}

用户: "去海底捞吃饭"
→ {"action": "add_stop", "params": {"keywords": "海底捞", "category": "restaurant", "shop_name": "海底捞"}}

用户: "不去一楼一饭店了"
→ {"action": "remove_stop", "params": {"name": "一楼一饭店"}}

用户: "打车去吧"
→ {"action": "change_transport", "params": {"mode": "TAXI"}}

用户: "推迟30分钟出发"
→ {"action": "change_time", "params": {"time": "+30"}}

用户: "换一条不走高速的路"
→ {"action": "reroute", "params": {"preference": "avoid_highway"}}

对话历史: AI: "为您找到以下可替换的餐饮店铺" / 用户: "算了还是原来的吧"
→ {"action": "no_change", "params": {}}

对话历史: AI: "已为您推荐星巴克" / 用户: "这家不要，换一个"
→ {"action": "swap_current", "params": {"target_name": "星巴克"}}

当前行程有[理发:东田造型, 咖啡:星巴克, 餐饮:海底捞] / 用户: "换一家吧"
→ {"action": "clarify", "params": {"message": "您想换哪一个目的地？目前行程中有理发（东田造型）、咖啡（星巴克）和餐饮（海底捞）。"}}

当前行程有[咖啡:星巴克] / 用户: "换一家吧"
→ {"action": "swap_current", "params": {"target_category": "cafe"}}

当前行程有[火锅:海底捞] / 用户: "把火锅改成理发"
→ {"action": "replace_stop", "params": {"remove_name": "火锅", "remove_category": "hotpot", "add_keywords": "理发", "add_category": "hair"}}

当前行程有[咖啡:星巴克] / 用户: "不想喝咖啡了改成奶茶"
→ {"action": "replace_stop", "params": {"remove_name": "咖啡", "remove_category": "cafe", "add_keywords": "奶茶", "add_category": "cafe"}}

用户: "下午三点提醒我去"
→ {"action": "add_reminder", "params": {"type": "CUSTOM", "time": "15:00", "label": "去"}}

用户: "提醒我8点买菜"
→ {"action": "add_reminder", "params": {"type": "CUSTOM", "time": "08:00", "label": "买菜"}}

用户: "记得下午两点提醒我接孩子"
→ {"action": "add_reminder", "params": {"type": "CUSTOM", "time": "14:00", "label": "接孩子"}}

只返回 JSON，不要其他文字。"""},
        {"role": "user", "content": edit_prompt}
    ]

    # ── LLM 解析（LLM-first，不设规则拦截）──
    if not skip_llm:
        ALLOWED_ACTIONS = {
            "add_stop", "remove_stop", "reroute", "change_time",
            "change_transport", "swap_current", "no_change", "clarify", "replace_stop",
            "add_reminder"
        }

        intent = {"action": "clarify", "params": {}}
        llm_success = False
        try:
            edit_resp = agent._call_llm(edit_messages, max_tokens=500, response_format={"type": "json_object"})
            raw = (edit_resp.content or "").strip()
            # JSON mode 优先直接解析；fallback 仍用正则提取
            try:
                llm_intent = json.loads(raw)
            except json.JSONDecodeError:
                json_match = re.search(r'\{[^{}]*"action"\s*:\s*"[a-z_]+"[^{}]*\}', raw, re.DOTALL)
                if not json_match:
                    json_match = re.search(r'\{.*\}', raw, re.DOTALL)
                if json_match:
                    llm_intent = json.loads(json_match.group(0))
                else:
                    llm_intent = None
            if llm_intent:
                action_val = llm_intent.get("action", "")
                if action_val in ALLOWED_ACTIONS:
                    intent = llm_intent
                    llm_success = True
                    print(f"[edit_trip] LLM 解析成功: action={action_val}")
                else:
                    print(f"[edit_trip] 拒绝无效 action: '{action_val}', 回退为 clarify")
            else:
                print(f"[edit_trip] LLM 返回无法解析的 JSON, 回退为 clarify")
        except json.JSONDecodeError as e:
            print(f"[edit_trip] LLM JSON 解析失败: {e}, 回退为 clarify")
        except Exception as e:
            print(f"[edit_trip] LLM 调用异常: {e}, 回退为 clarify")
        if not llm_success:
            print(f"[edit_trip] 最终回退为 clarify")

        action = intent.get("action", "clarify")
        params = intent.get("params", {})

        # ── 确认步骤：修改型操作需要用户确认 ──
        confirm_needed = intent.get("confirm_needed", False)
        interpretation = intent.get("interpretation", "")
        # 默认修改型 action 需要确认（no_change/clarify 除外）
        if action not in ("no_change", "clarify") and not confirm_needed:
            confirm_needed = True
        if confirm_needed and action not in ("no_change", "clarify"):
            session_state["_pending_confirmation"] = {
                "action": action,
                "params": params,
                "interpretation": interpretation or f"执行操作: {action}",
                "created_at": _time.time(),
            }
            return jsonify({
                "phase": "confirm",
                "action": action,
                "interpretation": interpretation or f"我准备执行「{action}」操作",
                "message": interpretation or f"我准备执行「{action}」操作，确认吗？",
                "pending_params": params,
            })

    # 如果前端传了 swap_target_shop_id（用户点击了目标选择器），注入到 params
    if swap_target_shop_id and action == "swap_current":
        params["target_shop_id"] = swap_target_shop_id

    # ── 执行编辑 ──

    # 无需重跑排程的 action
    if action == "no_change":
        return jsonify({"phase": "no_change", "message": "好的，行程保持不变 ✅"})

    if action == "add_reminder":
        rtype = params.get("type", "CUSTOM")
        rtime = params.get("time", "12:00")
        label = params.get("label", "提醒")
        repeat = params.get("repeat", "once")
        note = params.get("note", "")
        task_id = f"{rtype.lower()}_{int(_time.time())}"
        task = {
            "task_id": task_id,
            "type": rtype,
            "time": rtime,
            "label": label,
            "repeat": repeat,
            "note": note,
            "status": "active"
        }
        existing = session_state.get("_reminder_tasks", [])
        existing.append(task)
        session_state["_reminder_tasks"] = existing

        # 智能追问：检测提醒标签是否暗示了目的地品类
        follow_up = ""
        combined_text = label + edit_text
        # 买菜/菜市场 → 追问菜市场
        if any(kw in combined_text for kw in ["买菜", "菜市场", "菜场", "菜", "农贸", "生鲜"]):
            follow_up = "你有没有常去的菜市场？要不要我推荐几个？"
        # 吃饭/饭店 → 追问餐饮
        elif any(kw in combined_text for kw in ["吃饭", "饭店", "餐厅", "下馆子", "美食"]):
            follow_up = "你有没有常去的饭店？要我推荐几家吗？"
        # 喝咖啡/奶茶 → 追问咖啡
        elif any(kw in combined_text for kw in ["咖啡", "奶茶", "喝杯", "茶饮"]):
            follow_up = "你平时喜欢去哪家咖啡店？要我推荐几家吗？"

        return jsonify({
            "phase": "reminder_added",
            "message": f"已为你设置{rtime}的提醒「{label}」✅",
            "follow_up": follow_up,
            "reminder": task
        })

    if action == "clarify":
        # 如果用户输入看起来像换店意图但无法确定目标，返回 clarify_swap_target
        if _looks_like_swap_intent(edit_text):
            pairs = session_state.get("selected_pairs", [])
            if len(pairs) == 1:
                # 只有1个 stop → 不追问，直接搜索同品类候选
                cat, sid, sname = pairs[0]
                try:
                    swap_res = backend.skill_poi.search_poi_matrix(
                        center_coord=DEFAULT_CENTER_COORD,
                        categories=[cat] if cat else ["restaurant"],
                        radius_meters=5000,
                        min_rating=3.5,
                        keywords=sname,
                    )
                    candidates = []
                    for c, shops in swap_res.get("search_results", {}).items():
                        for s in shops:
                            if s.get("shop_id") != sid:
                                s["coord"] = f"{s.get('lat','')},{s.get('lng','')}"
                                agent.poi_cache[s["shop_id"]] = s
                                candidates.append(s)
                    if not candidates:
                        return jsonify({"error": f"附近未找到其他{cat}品类店铺"}), 404
                    return jsonify({
                        "phase": "swap_selection",
                        "current_shop": {"name": sname, "shop_id": sid},
                        "category": cat,
                        "label": backend.CATEGORY_NAME_CN.get(cat, cat),
                        "candidates": candidates[:5],
                        "message": f"为您找到以下可替换的{backend.CATEGORY_NAME_CN.get(cat, cat)}店铺："
                    })
                except Exception as e:
                    return jsonify({"error": f"搜索替换店铺失败: {str(e)}"}), 500
            elif len(pairs) > 1:
                stop_list = []
                for c, sid, sname in pairs:
                    stop_list.append({
                        "category": c, "shop_id": sid, "name": sname,
                        "label": backend.CATEGORY_NAME_CN.get(c, c)
                    })
                return jsonify({
                    "phase": "clarify_swap_target",
                    "stops": stop_list,
                    "message": params.get("message", "您想换哪一个目的地？")
                })
        msg = params.get("message", "抱歉，我没太理解您的意思，能再说一遍吗？")
        return jsonify({"phase": "clarify", "message": msg})

    if action == "swap_current":
        pairs = session_state.get("selected_pairs", [])
        if not pairs:
            return jsonify({"error": "没有可替换的目的地"}), 400

        # 解析目标：按 shop_id > name > category 优先级
        target_name = params.get("target_name", "")
        target_category = params.get("target_category", "")
        target_shop_id = params.get("target_shop_id", "")
        matched, _ = _resolve_swap_target(pairs, target_name, target_category, target_shop_id)

        if not matched:
            # 无法确定目标 → 返回目的地列表让用户选
            stop_list = []
            for c, sid, sname in pairs:
                stop_list.append({
                    "category": c,
                    "shop_id": sid,
                    "name": sname,
                    "label": backend.CATEGORY_NAME_CN.get(c, c)
                })
            return jsonify({
                "phase": "clarify_swap_target",
                "stops": stop_list,
                "message": "您想换哪一个目的地？"
            })

        cat, sid, sname = matched
        # 搜索同品类候选店铺
        try:
            swap_res = backend.skill_poi.search_poi_matrix(
                center_coord=DEFAULT_CENTER_COORD,
                categories=[cat] if cat else ["restaurant"],
                radius_meters=5000,
                min_rating=3.5,
                keywords=sname,
            )
            candidates = []
            for c, shops in swap_res.get("search_results", {}).items():
                for s in shops:
                    if s.get("shop_id") != sid:
                        s["coord"] = f"{s.get('lat','')},{s.get('lng','')}"
                        agent.poi_cache[s["shop_id"]] = s
                        candidates.append(s)
            if not candidates:
                return jsonify({"error": f"附近未找到其他{cat}品类店铺"}), 404
            return jsonify({
                "phase": "swap_selection",
                "current_shop": {"name": sname, "shop_id": sid},
                "category": cat,
                "label": backend.CATEGORY_NAME_CN.get(cat, cat),
                "candidates": candidates[:5],
                "message": f"为您找到以下可替换的{backend.CATEGORY_NAME_CN.get(cat, cat)}店铺："
            })
        except Exception as e:
            return jsonify({"error": f"搜索替换店铺失败: {str(e)}"}), 500

    if action == "replace_stop":
        # 跨品类替换：先移除旧 stop，再搜索新品类
        remove_name = params.get("remove_name", "").strip()
        add_keywords = params.get("add_keywords", edit_text)
        add_category = params.get("add_category", "")
        remove_category = params.get("remove_category", "")

        if not remove_name or len(remove_name) < 1:
            return jsonify({"error": "请指定要替换的目的地"}), 400

        old_pairs = session_state.get("selected_pairs", [])
        # 用 _shop_name_matches 匹配要移除的 stop，品类匹配也参与（优先品类）
        new_pairs = []
        removed_stop = None
        for cat, sid, sname in old_pairs:
            if removed_stop is None and (
                (remove_category and cat == remove_category) or
                _shop_name_matches(sname, remove_name) or
                (remove_name in _CAT_KEYWORDS and cat == _CAT_KEYWORDS[remove_name])
            ):
                removed_stop = (cat, sid, sname)
                continue
            new_pairs.append((cat, sid, sname))

        if removed_stop is None:
            return jsonify({"error": f"未找到可替换的目的地'{remove_name}'"}), 404
        if not new_pairs and not add_keywords:
            return jsonify({"error": "该操作会清空所有行程，已拒绝。请指定新目的地"}), 400

        # 先更新 selected_pairs（移除旧 stop）
        session_state["selected_pairs"] = new_pairs

        # 搜索新品类店铺
        try:
            new_res = backend.skill_poi.search_poi_matrix(
                center_coord=DEFAULT_CENTER_COORD,
                categories=[add_category] if add_category else ["restaurant"],
                radius_meters=5000,
                min_rating=3.5,
                keywords=add_keywords if add_keywords else None,
            )
            added = False
            for c, shops in new_res.get("search_results", {}).items():
                if shops:
                    shop_list = []
                    for s in shops[:3]:
                        s["coord"] = f"{s.get('lat','')},{s.get('lng','')}"
                        agent.poi_cache[s["shop_id"]] = s
                        shop_list.append(s)
                    return jsonify({
                        "phase": "need_shop_selection",
                        "category": c,
                        "label": backend.CATEGORY_NAME_CN.get(c, add_keywords),
                        "shops": shop_list,
                        "message": f"已移除{backend.CATEGORY_NAME_CN.get(removed_stop[0], removed_stop[2])}，为您搜索{backend.CATEGORY_NAME_CN.get(c, '')}店铺："
                    })
            if not added:
                return jsonify({"error": f"未找到匹配'{add_keywords}'的新目的地"}), 404
        except Exception as e:
            # 恢复旧 pairs
            session_state["selected_pairs"] = old_pairs
            return jsonify({"error": f"搜索新目的地失败: {str(e)}"}), 500

    if action == "add_stop":
        # 搜索新目的地
        kw = params.get("keywords", edit_text)
        cat = params.get("category")
        shop_name = params.get("shop_name", "")
        try:
            new_res = backend.skill_poi.search_poi_matrix(
                center_coord=DEFAULT_CENTER_COORD,
                categories=[cat] if cat else ["restaurant"],
                radius_meters=5000,
                min_rating=3.5,
                keywords=kw if kw else None,
            )
            # 取第一个有结果的品类
            added = False
            for c, shops in new_res.get("search_results", {}).items():
                if shops:
                    # 用户没指定具体店名 → 返回推荐面板数据
                    if not shop_name:
                        shop_list = []
                        for s in shops[:3]:
                            s["coord"] = f"{s.get('lat','')},{s.get('lng','')}"
                            shop_list.append(s)
                        return jsonify({
                            "phase": "need_shop_selection",
                            "category": c,
                            "label": backend.CATEGORY_NAME_CN.get(c, kw),
                            "shops": shop_list,
                            "message": f"为您找到以下{backend.CATEGORY_NAME_CN.get(c, '')}店铺："
                        })
                    # 有具体店名 → 匹配并自动添加
                    best = max(shops, key=lambda s: s.get("rating", 0))
                    agent.poi_cache[best["shop_id"]] = best
                    session_state["selected_pairs"].append((c, best["shop_id"], best["name"]))
                    added = True
                    break
            if not added:
                return jsonify({"error": f"未找到匹配' {kw} '的目的地"}), 404
        except Exception as e:
            return jsonify({"error": f"搜索失败: {str(e)}"}), 500

    elif action == "remove_stop":
        name_kw = params.get("name", "").strip()
        if not name_kw or len(name_kw) < 2:
            return jsonify({"error": "请指定要删除的目的地名称"}), 400
        old_pairs = session_state.get("selected_pairs", [])
        # 先计算，不直接赋值（防止误清空）
        # 使用 _shop_name_matches 逐级匹配，防止短关键词误匹配
        new_pairs = [
            (cat, sid, sname)
            for cat, sid, sname in old_pairs
            if not _shop_name_matches(sname, name_kw)
        ]
        if len(new_pairs) == len(old_pairs):
            return jsonify({"error": f"未找到含'{name_kw}'的目的地"}), 404
        if not new_pairs:
            return jsonify({"error": "该操作会清空所有行程，已拒绝。请指定更具体的目的地名称"}), 400
        session_state["selected_pairs"] = new_pairs

    elif action == "change_time":
        t = params.get("time", "now")
        if t == "now":
            session_state["fixed_time"] = None
            session_state["time_mode"] = "now"
        elif t.startswith("+") or t.startswith("-"):
            # 相对时间偏移
            try:
                delta = int(t)
                from datetime import datetime, timedelta
                new_dt = datetime.now() + timedelta(minutes=delta)
                session_state["fixed_time"] = new_dt.strftime("%H:%M")
                session_state["time_mode"] = "fixed"
            except ValueError:
                pass
        else:
            session_state["fixed_time"] = t
            session_state["time_mode"] = "fixed"

    elif action == "change_transport":
        mode = params.get("mode", "WALK")
        session_state["transport"] = {"WALK": "步行", "TAXI": "打车", "METRO": "公共交通", "DRIVE": "驾车"}.get(mode, "步行")

    # reroute / 其他: 直接重跑排程（可能用新偏好）
    session_state["phase"] = "running"
    return _run_schedule_from_session()


@app.route("/api/add_stop_to_trip", methods=["POST"])
def api_add_stop_to_trip():
    """行程中用户选店后，将店铺加入 selected_pairs 并重跑排程"""
    data = request.get_json()
    category = data.get("category", "")
    shop_id = data.get("shop_id", "")
    info = agent.poi_cache.get(shop_id, {})
    if not info:
        return jsonify({"error": "店铺不存在"}), 404
    session_state["selected_pairs"].append((category, shop_id, info.get("name", shop_id)))
    return _run_schedule_from_session()


# ── 排程辅助函数 ──

def _run_schedule_from_session():
    """从 session_state 构建排程输入并执行"""
    global session_state
    fixed_time = session_state.get("fixed_time")
    time_mode = session_state.get("time_mode", "now")

    task_list = []
    spatial_matrix = {
        "locations": {"loc_current": {"name": "当前起点", "coord": DEFAULT_CENTER_COORD}},
        "routes": {}
    }

    for cat, sid, sname in session_state["selected_pairs"]:
        info = agent.poi_cache.get(sid, {})
        raw = info.get('coord', '')
        if raw and ',' in raw:
            coord = raw
        else:
            coord = DEFAULT_CENTER_COORD
        human_needed = info.get("human_needed", True)
        # 兜底：品类强制修正（防止预置缓存等非 _normalize_poi 来源的数据错误）
        if cat in ("pet", "laundry"):
            human_needed = False
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

    # —— 诊断日志：打印 task_list 的 human_needed 分类 ——
    print(f"[调度] task_list 共 {len(task_list)} 个任务:", flush=True)
    for t in task_list:
        print(f"  - {t['name']} | human_needed={t['human_needed']} | fixed={t['fixed_start_time']} | cat={t.get('category','')}", flush=True)

    # —— 交通模式自动计算（Phase 4.4）——
    _transport_priority = _read_profile().get("commute", {}).get("transport_priority", "步行优先")
    _transport_map = {"步行优先": "WALK", "打车优先": "TAXI", "地铁优先": "METRO", "驾车优先": "DRIVE"}
    _default_mode = _transport_map.get(_transport_priority, "WALK")
    # 为用户指定的 transport 覆盖
    _user_transport = session_state.get("transport", "")
    if _user_transport and _user_transport in ("打车", "公共交通", "驾车"):
        _override_map = {"打车": "TAXI", "公共交通": "METRO", "驾车": "DRIVE"}
        _default_mode = _override_map.get(_user_transport, _default_mode)

    # 自动计算所有位置对之间的路线
    import math as _math
    _all_loc_ids = list(spatial_matrix["locations"].keys())
    for i, loc_a in enumerate(_all_loc_ids):
        for loc_b in _all_loc_ids[i + 1:]:
            ca = spatial_matrix["locations"][loc_a].get("coord", "")
            cb = spatial_matrix["locations"][loc_b].get("coord", "")
            if ca and cb:
                lat1, lng1 = [float(x) for x in ca.split(",")]
                lat2, lng2 = [float(x) for x in cb.split(",")]
                R = 6371.0
                dlat = _math.radians(lat2 - lat1)
                dlng = _math.radians(lng2 - lng1)
                a = _math.sin(dlat / 2) ** 2 + _math.cos(_math.radians(lat1)) * _math.cos(_math.radians(lat2)) * _math.sin(dlng / 2) ** 2
                c = 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1 - a))
                dist = int(R * c * 1000)
                spatial_matrix["routes"][f"{loc_a}->{loc_b}"] = {"transport_mode": _default_mode, "distance_meters": dist}
                spatial_matrix["routes"][f"{loc_b}->{loc_a}"] = {"transport_mode": _default_mode, "distance_meters": dist}

    session_state["task_list"] = task_list
    session_state["spatial_matrix"] = spatial_matrix
    # 若用户说几点出发，now_str 设为该时间（引擎从该时间开始行走）
    if time_mode == "fixed":
        session_state["now_str"] = fixed_time
    else:
        # 虚拟时间控制台开着 → 用虚拟时间；否则用系统真实时间
        if session_state.get("clock_enabled"):
            _tm = time_master.get_master()
            _cs = _tm.get_session(_CLOCK_SESSION_ID)
            session_state["now_str"] = _cs.virtual_time if (_cs and _cs.virtual_time) else datetime.now().strftime("%H:%M")
        else:
            session_state["now_str"] = datetime.now().strftime("%H:%M")
    session_state["confirmed_ids"] = []
    session_state["rejected_ids"] = []

    return _run_schedule()


def _run_schedule():
    """执行一次排程，处理 CONFIRM_REQUIRED / SUCCESS / 其他"""
    global session_state
    # ★ 修复：从虚拟时钟重新获取当前时间，确保虚拟时间推进后重排使用最新时间
    if session_state.get("clock_enabled"):
        _tm = time_master.get_master()
        _cs = _tm.get_session(_CLOCK_SESSION_ID)
        if _cs and _cs.virtual_time:
            session_state["now_str"] = _cs.virtual_time
    schedule_res = backend.skill_scheduler.solve_concurrent_timeline(
        session_state["task_list"],
        session_state["spatial_matrix"],
        session_state["now_str"],
        session_state["confirmed_ids"],
        session_state["rejected_ids"],
    )

    # —— 诊断日志：调度器返回结果 ——
    if schedule_res.get("status") == "SUCCESS":
        tl = schedule_res.get("timeline", [])
        drop_ct = sum(1 for x in tl if x.get("action") == "DROP_TASK")
        exec_ct = sum(1 for x in tl if x.get("action") == "START_TASK")
        pick_ct = sum(1 for x in tl if x.get("action") == "PICK_TASK")
        print(f"[调度] ✅ SUCCESS | DROP={drop_ct} EXEC={exec_ct} PICK={pick_ct} 总耗时={schedule_res.get('total_duration_minutes')}min", flush=True)

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
        raw = schedule_res["timeline"]

        # ── 品类 → 具体事项描述 ──
        _SPECIFIC_ACTIONS = {
            "hair": "理发", "pet": "给宠物洗澡美容", "laundry": "洗衣",
            "cafe": "喝杯饮品", "gym": "健身锻炼", "restaurant": "用餐",
            "hotpot": "吃火锅", "japanese": "吃日料", "cinema": "看电影",
        }

        # ── 时间计算 ──
        def _calc_end(time_str: str, add_min: int) -> str:
            try:
                parts = time_str.split(":")
                h, m = int(parts[0]), int(parts[1])
                m += add_min
                h += m // 60
                m = m % 60
                return f"{h:02d}:{m:02d}"
            except:
                return ""

        # task_id → {name, duration, human_needed, action_name, category}
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
                    "category": cat,
                }

        # 截短店名
        def _short(name: str) -> str:
            s = re.split(r'[\(（·]', name)[0].strip()
            s = re.sub(r'[—–-].*$', '', s).strip()
            return s

        # location id → {full, short}
        loc_map = {}
        for lid, ldata in session_state.get("spatial_matrix", {}).get("locations", {}).items():
            full = ldata.get("name", lid)
            loc_map[lid] = {"full": full, "short": _short(full)}

        cleaned = []
        skip = set()
        for i in range(len(raw)):
            if i in skip:
                continue
            item = raw[i]

            # ── DEPART ──
            if item["action"] == "DEPART":
                first_dest = ""
                for j in range(i + 1, len(raw)):
                    if raw[j]["action"] == "MOVE":
                        nid = raw[j].get("next_location_id", "")
                        first_dest = loc_map.get(nid, {}).get("short", "")
                        break
                item["header"] = f"出发，前往{first_dest}" if first_dest else "出发"

            # ── MOVE + 后续动作合并 ──
            elif item["action"] == "MOVE":
                nxt_idx = i + 1
                # 跳过中间的 WAIT（如 MOVE → WAIT → PICK_TASK）
                while nxt_idx < len(raw) and raw[nxt_idx]["action"] == "WAIT":
                    nxt_idx += 1
                if nxt_idx < len(raw):
                    nxt = raw[nxt_idx]
                    if nxt["action"] in ("DROP_TASK", "START_TASK", "PICK_TASK"):
                        tgt_short = loc_map.get(item.get("next_location_id", ""), {}).get("short", "")
                        tid = nxt.get("task_id", "")
                        tinfo = task_map.get(tid, {})
                        act_name = tinfo.get("action_name", "")
                        cat = tinfo.get("category", "")
                        specific = _SPECIFIC_ACTIONS.get(cat, act_name)
                        duration = tinfo.get("duration_minutes", 45)

                        # 找下一个 MOVE 的目的地
                        next_dest = ""
                        for j in range(nxt_idx + 1, len(raw)):
                            if raw[j]["action"] == "MOVE":
                                nid = raw[j].get("next_location_id", "")
                                next_dest = loc_map.get(nid, {}).get("short", "")
                                break

                        item["time"] = nxt["time"]
                        item["action"] = "MOVE_AND_EXEC"
                        item["task_id"] = nxt.get("task_id", item.get("task_id", ""))

                        if nxt["action"] == "DROP_TASK":
                            item["header"] = f"去 {tgt_short}"
                            item["detail"] = f"放下{act_name}（后台处理约 {duration} 分钟）"
                            item["end_time"] = _calc_end(nxt["time"], duration + 5)  # DROP + DROP_PICK
                        elif nxt["action"] == "START_TASK":
                            item["header"] = f"去 {tgt_short}"
                            item["detail"] = f"{specific}（预计 {duration} 分钟）"
                            item["end_time"] = _calc_end(nxt["time"], duration)
                        elif nxt["action"] == "PICK_TASK":
                            item["header"] = f"去 {tgt_short}"
                            item["detail"] = f"取回{act_name}"
                            item["end_time"] = _calc_end(nxt["time"], 5)  # PICK 约5分钟

                        # 标记跳过 MOVE 和合并的动作（以及中间的 WAIT）
                        skip.add(nxt_idx)
                        for w in range(i + 1, nxt_idx):
                            skip.add(w)

            # ── 未被合并的独立 action（WAIT / 未匹配的 PICK 等）──
            if item["action"] not in ("DEPART", "MOVE_AND_EXEC"):
                # 保留原样但用新字段
                item["header"] = item.get("memo", "")
                # 去掉旧字段
                item.pop("memo", None)

            # 清理内部标记
            item.pop("memo", None)
            item.pop("sub_task", None)
            item.pop("_pick_merge", None)
            item.pop("target_location_id", None)
            item.pop("next_location_id", None)

            cleaned.append({
                "time": item.get("time", ""),
                "action": item.get("action", ""),
                "header": item.get("header", ""),
                "detail": item.get("detail", ""),
                "end_time": item.get("end_time", ""),
                "task_id": item.get("task_id", ""),
            })

        # 注册到虚拟时钟（如果已开启）
        _tm = time_master.get_master()
        _cs = _tm.get_session(_CLOCK_SESSION_ID)
        if _cs:
            # 保留 WATER/MED 提醒节点，只替换 SCHEDULE 节点
            _old_nodes = list(_cs.schedule_nodes) if _cs.schedule_nodes else []
            _reminder_nodes = [n for n in _old_nodes if n.get("type") in ("WATER", "MED")]
            _schedule_nodes = _reminder_nodes[:]  # 先放提醒节点
            # 获取行程日期（如果有）
            _trip_date = session_state.get("trip_start_date", _cs.current_date_str or datetime.now().strftime("%Y-%m-%d"))
            for item in schedule_res["timeline"]:
                _schedule_nodes.append({
                    "time": item["time"],
                    "type": "SCHEDULE",
                    "node_id": item.get("task_id", ""),
                    "name": item.get("memo", ""),
                    "action": item.get("action", ""),
                    "target_location_id": item.get("target_location_id"),
                    "date": _trip_date,  # 添加日期绑定
                })

            _tm.set_schedule(_CLOCK_SESSION_ID, _schedule_nodes)

        # 读取管家偏好，注入通勤参数
        _commute = _read_profile().get("commute", {})
        _walk_tolerance = _commute.get("walking_tolerance_meters", 800)
        _transport_priority = _commute.get("transport_priority", "步行优先")
        # 若用户在前端选了交通方式 → 用前端的；否则用偏好默认
        _user_transport = session_state.get("transport", "")
        if not _user_transport or _user_transport == "步行":
            _user_transport = _transport_priority

        # 映射 transport_priority 到前端四个值
        _transport_map = {
            "步行优先": "步行",
            "打车优先": "打车",
            "地铁优先": "公共交通",
        }
        _user_transport = _transport_map.get(_user_transport, _user_transport)

        # 调用防踩坑 Skill
        from skills.destination_anti_pitfall import destination_anti_pitfall as skill_pitfall
        pitfall_input = {
            "trip_id": f"trip_{int(datetime.now().timestamp())}",
            "current_node_index": 0,
            "pipeline_nodes": [],
            "transport": _user_transport,
            "walking_tolerance_meters": _walk_tolerance,
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
                "coordinate": info.get("coord", DEFAULT_CENTER_COORD)
            })
        pitfall_output = skill_pitfall.execute_anti_pitfall_skill(
            input_payload=pitfall_input
        )
        pending_triggers = skill_pitfall.get_pending_triggers(pitfall_output)
        # 保存到 session_state 供反射 API 使用
        session_state["pitfall_global_reminders"] = pitfall_output.get("global_reminders", [])
        session_state["pitfall_localized_insights"] = pitfall_output.get("localized_insights", [])
        session_state["pitfall_intent_triggers"] = pending_triggers

        # —— 异常传感器：检测当前环境上下文中是否有活跃异常，产 Plan B trigger ——
        from skills.anomaly_sensor_skill import anomaly_sensor_skill as anomaly_sensor
        anomaly_triggers = []
        anomaly_insights = []
        # 检查是否有未处理的异常注入
        _pending_anomalies = session_state.get("pending_anomalies", [])
        if _pending_anomalies:
            sensor_input = {
                "pipeline_nodes": pitfall_input["pipeline_nodes"],
                "environmental_context": {
                    "timestamp": int(datetime.now().timestamp()),
                    "weather_summary": "多云",
                    "active_anomalies": _pending_anomalies,
                },
            }
            sensor_output = anomaly_sensor.execute_anomaly_sensor_skill(input_payload=sensor_input)
            anomaly_triggers = sensor_output.get("intent_triggers", [])
            anomaly_insights = sensor_output.get("localized_insights", [])
            # 清空已处理的异常（避免重复弹窗）
            session_state["pending_anomalies"] = []
        session_state["anomaly_intent_triggers"] = anomaly_triggers
        session_state["anomaly_insights"] = anomaly_insights

        return jsonify({
            "phase": "done",
            "departure_time": schedule_res["suggested_departure_time"],
            "total_minutes": schedule_res["total_duration_minutes"],
            "timeline": cleaned,
            "pitfall_reminders": pitfall_output.get("global_reminders", []),
            "pitfall_insights": pitfall_output.get("localized_insights", []),
            "pitfall_triggers": pending_triggers,
            "anomaly_triggers": anomaly_triggers,
            "anomaly_insights": anomaly_insights,
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
    """前端用户点击 intent_trigger 按钮后，执行反射动作。
    支持两种来源：
    1. destination_anti_pitfall 产的 trigger（virtual_call_taxi/virtual_queue 等）
    2. anomaly_sensor 产的 trigger（virtual_pipeline_mutate → 真正的管线变异）
    """
    from skills.destination_anti_pitfall import destination_anti_pitfall as skill_pitfall
    data = request.get_json(silent=True) or {}
    trigger_id = data.get("trigger_id")
    if not trigger_id:
        return jsonify({"error": "缺少 trigger_id"}), 400

    # 搜索所有 trigger 来源
    target = None
    # 1) pitfall_intent_triggers（防踩坑产的）
    for t in session_state.get("pitfall_intent_triggers", []):
        if t.get("trigger_id") == trigger_id:
            target = t
            break
    # 2) anomaly_intent_triggers（异常传感器产的）
    if not target:
        for t in session_state.get("anomaly_intent_triggers", []):
            if t.get("trigger_id") == trigger_id:
                target = t
                break

    if not target:
        return jsonify({"error": "未找到对应 trigger"}), 404

    # 判断是不是虚拟管线变异器 trigger
    reflection = target.get("action_reflection", {})
    target_tools = reflection.get("target_tools", [])

    if "virtual_pipeline_mutate" in target_tools:
        # —— 真正的管线变异：调用 /api/pipeline/mutate 的逻辑 ——
        params = reflection.get("parameter_mapping", {})
        if not params.get("execute_intercept_hook"):
            result = skill_pitfall.dispatch_reflection(target)
            return jsonify(result)

        action = params.get("mutation_directive", "SWAP_NODE")
        corrupted_node_id = params.get("corrupted_node_id", "")
        delta_minutes = params.get("delta_delay_minutes", 30)

        task_list = session_state.get("task_list", [])
        spatial_matrix = session_state.get("spatial_matrix", {})

        if not task_list:
            return jsonify({"status": "ERROR", "message": "无行程数据"}), 400

        # 找到受灾任务
        corrupted_task = None
        corrupted_idx = -1
        for i, t in enumerate(task_list):
            if t["task_id"] == corrupted_node_id:
                corrupted_task = t
                corrupted_idx = i
                break

        if not corrupted_task:
            return jsonify({"status": "ERROR", "message": f"未找到节点 {corrupted_node_id}，可能已完成或不在本次行程"}), 404

        if action == "SWAP_NODE":
            corrupted_cat = corrupted_task.get("category", "")
            candidates = []
            for sid, shop in agent.poi_cache.items():
                if shop.get("category") == corrupted_cat and sid != corrupted_node_id:
                    candidates.append((sid, shop))
            if not candidates:
                return jsonify({"status": "ERROR", "message": f"品类 {corrupted_cat} 无替选店铺，建议改为跳过"}), 404

            new_sid, new_shop = candidates[0]
            task_list[corrupted_idx] = {
                "task_id": new_sid,
                "name": new_shop["name"],
                "location_id": new_sid,
                "duration_minutes": corrupted_task["duration_minutes"],
                "human_needed": corrupted_task.get("human_needed", True),
                "fixed_start_time": corrupted_task.get("fixed_start_time"),
                "category": corrupted_cat,
            }
            spatial_matrix["locations"][new_sid] = {
                "name": new_shop["name"],
                "coord": f"{new_shop.get('lat', 39.93)},{new_shop.get('lng', 116.45)}",
            }
            session_state["task_list"] = task_list
            session_state["spatial_matrix"] = spatial_matrix
            session_state["confirmed_ids"] = []
            session_state["rejected_ids"] = []
            return _run_schedule()

        elif action == "BYPASS_NODE":
            task_list.pop(corrupted_idx)
            session_state["task_list"] = task_list
            session_state["confirmed_ids"] = []
            session_state["rejected_ids"] = []
            return _run_schedule()

        elif action == "POSTPONE_NODE":
            task_list.pop(corrupted_idx)
            task_list.append(corrupted_task)
            session_state["task_list"] = task_list
            session_state["confirmed_ids"] = []
            session_state["rejected_ids"] = []
            return _run_schedule()

        return jsonify({"status": "ERROR", "message": f"未知变异动作: {action}"}), 400

    # 其他 trigger 走原有防踩坑反射逻辑
    result = skill_pitfall.dispatch_reflection(target)
    return jsonify(result)


# ======================================================================
# Plan B 二级弹窗相关 API
# ======================================================================

@app.route("/api/insert_shelter", methods=["POST"])
def api_insert_shelter():
    """
    下暴雨避雨：由前端传入 shop_id 指定饮品店，插入行程第一个目的地后重算排程。
    输入: { shop_id: "..." }  可选，不传则自动找最近的 cafe
    """
    if not session_state.get("task_list"):
        return jsonify({"error": "无行程数据"}), 400

    data = request.get_json(silent=True) or {}
    forced_shop_id = data.get("shop_id", "")

    cafe_shop_id = None
    cafe_name = None

    # 前端指定了 shop_id 优先用
    if forced_shop_id and forced_shop_id in agent.poi_cache:
        cafe_shop_id = forced_shop_id
        cafe_name = agent.poi_cache[forced_shop_id].get("name", "附近饮品店")

    # 否则自动找 cafe 品类最近店铺
    if not cafe_shop_id:
        for sid, shop in agent.poi_cache.items():
            if shop.get("category") == "cafe":
                cafe_shop_id = sid
                cafe_name = shop.get("name", "附近饮品店")
                break

    if not cafe_shop_id:
        # 兜底：尝试触发一次 cafe 品类独立搜索
        try:
            extra = backend.skill_poi.search_poi_matrix(
                center_coord=DEFAULT_CENTER_COORD,
                categories=["cafe"],
                radius_meters=5000,
                min_rating=3.5
            )
            if extra.get("status") == "SUCCESS":
                cafes = extra.get("search_results", {}).get("cafe", [])
                for s in cafes:
                    sid = s.get("shop_id")
                    if sid and sid not in agent.poi_cache:
                        agent.poi_cache[sid] = s
                # 重试取第一个
                for sid, shop in agent.poi_cache.items():
                    if shop.get("category") == "cafe":
                        cafe_shop_id = sid
                        cafe_name = shop.get("name", "附近饮品店")
                        break
        except Exception:
            pass

    if not cafe_shop_id:
        return jsonify({"error": "未找到附近的避雨店铺"}), 404

    # 构造避雨节点，插入 selected_pairs 第0位
    selected_pairs = session_state.get("selected_pairs", [])
    selected_pairs.insert(0, ("cafe", cafe_shop_id, cafe_name))
    session_state["selected_pairs"] = selected_pairs

    # 重跑完整排程链路（含时间重算 + 交通模式 + 防踩坑 + 异常传感器）
    result = _run_schedule_from_session()
    if not result:
        return jsonify({"error": "排程失败"}), 500

    result_json = result.get_json()
    result_json["shelter_name"] = cafe_name
    result_json["shelter_id"] = cafe_shop_id
    return jsonify(result_json)


@app.route("/api/get_swap_candidates", methods=["POST"])
def api_get_swap_candidates():
    """
    获取可替换的同品类店铺列表（排除异常店）。
    输入: { anomaly_type: "排号异常" | "餐厅停电", category: "cafe" 可选 }
    """
    data = request.get_json(silent=True) or {}
    anomaly_type = data.get("anomaly_type", "")
    forced_category = data.get("category", "")  # 如果前端指定了品类，直接用

    selected_pairs = session_state.get("selected_pairs", [])

    target_category = None
    excluded_shop_id = None

    if forced_category:
        target_category = forced_category
        excluded_shop_id = ""
        if selected_pairs:
            for cat, sid, sname in selected_pairs:
                if cat == forced_category:
                    excluded_shop_id = sid
                    break
    elif not selected_pairs:
        return jsonify({"error": "无已选店铺"}), 400
    else:
        for cat, sid, sname in selected_pairs:
            if cat in ("restaurant",):
                target_category = cat
                excluded_shop_id = sid
                break
        if not target_category:
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


@app.route("/api/get_nearby_cafes", methods=["POST"])
def api_get_nearby_cafes():
    """
    实时高德API查询最近饮品店（咖啡/奶茶）。
    接受可选 lat/lng 坐标；优先请求body → session_state → 北京中心兜底。
    直调 AmapPOIClient.search_nearby() 绕过 poi_cache 预缓存；
    若API不可用或无结果，降级使用 poi_cache 并标记 source。
    """
    import math

    data = request.get_json(silent=True) or {}
    lat = data.get("lat")
    lng = data.get("lng")

    # 确定查询中心坐标
    center_lat = 39.93
    center_lng = 116.45

    if lat is not None and lng is not None:
        center_lat = float(lat)
        center_lng = float(lng)
    else:
        sm = session_state.get("spatial_matrix", {})
        locs = sm.get("locations", {})
        if locs:
            first_loc = next(iter(locs.values()))
            coord_str = first_loc.get("coord", "")
            if coord_str and "," in coord_str:
                try:
                    parts = coord_str.split(",")
                    center_lat = float(parts[0].strip())
                    center_lng = float(parts[1].strip())
                except (ValueError, IndexError):
                    pass

    shops = []
    source = "cache_fallback"

    # 优先直调高德 API 实时搜索，绕过预缓存
    try:
        result = search_nearby(
            lng=center_lng,
            lat=center_lat,
            radius=3000,
            keywords="咖啡|奶茶|茶饮",
            category="cafe",
        )
        if result and result.get("shops"):
            shops = result["shops"]
            source = "amap_realtime"
    except Exception:
        pass

    # 降级：使用 poi_cache
    if not shops and agent and agent.poi_cache:
        for sid, shop in agent.poi_cache.items():
            if shop.get("category") == "cafe":
                shops.append(shop)

    # 按评分降序
    shops.sort(key=lambda s: s.get("rating", 0), reverse=True)

    # 计算距离并格式化
    shops_data = []
    all_out_of_1km = True
    for shop in shops:
        dist_m = 0
        coord = shop.get("coord", "")
        if coord and "," in coord:
            try:
                slat = float(coord.split(",")[0].strip())
                slng = float(coord.split(",")[1].strip())
                R = 6371000
                dlat = math.radians(slat - center_lat)
                dlng = math.radians(slng - center_lng)
                a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(center_lat)) * math.cos(math.radians(slat)) * math.sin(dlng / 2) ** 2
                c = 2 * math.asin(math.sqrt(a))
                dist_m = int(R * c)
            except Exception:
                pass
        if dist_m <= 1000:
            all_out_of_1km = False
        dist_str = f"{dist_m}m" if dist_m < 1000 else f"{dist_m / 1000:.1f}km"
        dist_km = round(dist_m / 1000, 1)
        shops_data.append({
            "shop_id": shop.get("shop_id", shop.get("id", "")),
            "name": shop.get("name", ""),
            "rating": shop.get("rating", 0),
            "distance": dist_str,
            "distance_meters": dist_m,
            "distance_km": dist_km,
            "coord": coord,
        })

    return jsonify({
        "shops": shops_data[:5],
        "all_out_of_1km": all_out_of_1km and len(shops_data) > 0,
        "total_found": len(shops_data),
        "source": source,
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
    replaced = False
    for i, (cat, sid, sname) in enumerate(selected_pairs):
        # 只按品类匹配替换，防止误替换不同品类的 stop
        if cat == new_category:
            selected_pairs[i] = (new_category, new_shop_id, new_name)
            replaced = True
            break
    if not replaced:
        return jsonify({"error": f"行程中未找到品类为 {new_category} 的目的地，无法替换"}), 400
    session_state["selected_pairs"] = selected_pairs

    # 更新 task_list
    raw = new_shop.get('coord', '')
    coord = raw if raw and ',' in raw else DEFAULT_CENTER_COORD

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
                "action": item["action"],
                "header": memo,
                "detail": sub["action"] + "（预计 " + str(sub["duration_minutes"]) + " 分钟）" if sub else "",
                "end_time": "",
                "task_id": item.get("task_id", ""),
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
            _tm.set_schedule(_CLOCK_SESSION_ID, _sn)
    else:
        return jsonify({
            "phase": "swapped",
            "message": schedule_res.get("message", "店铺已替换，但排程需要进一步确认")
        })


@app.route("/api/replan", methods=["POST"])
def api_replan():
    """
    重新排程：修改 now_str（延后出发）并重新调排程引擎。
    输入: { delay_minutes: int, transport_mode: str, reroute: bool }
    - transport_mode: 'taxi'|'walk'|'metro'|'walk_bus'|'drive' 切换交通方式
    - reroute: true 时对 spatial_matrix 中的所有距离应用 1.5x 绕行乘数
    当虚拟时钟开启时，优先使用虚拟时间作为基础。
    """
    data = request.get_json(silent=True) or {}
    delay = int(data.get("delay_minutes", 0))
    transport_mode = (data.get("transport_mode") or "").strip()
    is_reroute = data.get("reroute", False)

    # —— 处理 transport_mode 变更 ——
    if transport_mode:
        _mode_map = {
            'taxi': 'TAXI', 'walk': 'WALK', 'metro': 'METRO',
            'walk_bus': 'BUS', 'drive': 'DRIVE', 'bus': 'BUS',
        }
        _mode = _mode_map.get(transport_mode, 'TAXI')
        sm = session_state.get("spatial_matrix", {})
        for route_key in sm.get("routes", {}):
            sm["routes"][route_key]["transport_mode"] = _mode
        # 同步更新 session_state.transport 供后续使用
        _transport_label = {'TAXI': '打车', 'WALK': '步行', 'METRO': '地铁', 'BUS': '步行+公交', 'DRIVE': '驾车'}.get(_mode, '打车')
        session_state["transport"] = _transport_label

    # —— 处理 reroute（绕行）——
    if is_reroute:
        sm = session_state.get("spatial_matrix", {})
        for route_key in sm.get("routes", {}):
            orig = sm["routes"][route_key].get("distance_meters", 0)
            if orig > 0:
                sm["routes"][route_key]["distance_meters"] = int(orig * 1.5)
                sm["routes"][route_key]["_rerouted"] = True

    # 虚拟时钟开启时优先用虚拟时间
    if session_state.get("clock_enabled"):
        _tm = time_master.get_master()
        _cs = _tm.get_session(_CLOCK_SESSION_ID)
        if _cs and _cs.virtual_time:
            now_str = _cs.virtual_time
        else:
            now_str = session_state.get("now_str", "10:00")
    else:
        now_str = session_state.get("now_str", "10:00")
    if delay > 0 and now_str:
        parts = now_str.split(":")
        if len(parts) == 2:
            try:
                h, m = int(parts[0]), int(parts[1])
                total = h * 60 + m + delay
                new_h = total // 60
                new_m = total % 60
                if new_h >= 24:
                    new_h = 23
                    new_m = 59
                now_str = f"{new_h:02d}:{new_m:02d}"
            except:
                pass
    session_state["now_str"] = now_str
    session_state["confirmed_ids"] = []
    session_state["rejected_ids"] = []

    schedule_res = backend.skill_scheduler.solve_concurrent_timeline(
        session_state["task_list"],
        session_state["spatial_matrix"],
        now_str,
        session_state["confirmed_ids"],
        session_state["rejected_ids"],
    )

    if schedule_res.get("status") == "SUCCESS":
        cleaned = []
        for item in schedule_res["timeline"]:
            memo = item.get("memo", "")
            task_list = session_state.get("task_list", [])
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
                "task_id": item.get("task_id", ""),
            })
        main_plan = {
            "departure_time": schedule_res["suggested_departure_time"],
            "total_minutes": schedule_res["total_duration_minutes"],
            "timeline": cleaned,
        }
        session_state["main_plan"] = main_plan

        return jsonify({
            "status": "SUCCESS",
            "departure_time": schedule_res["suggested_departure_time"],
            "total_minutes": schedule_res["total_duration_minutes"],
            "timeline": cleaned,
        })
    elif schedule_res.get("status") == "CONFIRM_REQUIRED":
        session_state["phase"] = "conflict"
        session_state["conflict_task"] = schedule_res["conflict_task"]
        return jsonify({
            "status": "CONFIRM_REQUIRED",
            "conflict_task": {
                "task_id": schedule_res["conflict_task"]["task_id"],
                "name": session_state["conflict_task"]["name"],
            }
        })
    else:
        return jsonify({"status": "ERROR", "message": schedule_res.get("message", "重新排程失败")})


@app.route("/api/cancel_trip", methods=["POST"])
def api_cancel_trip():
    """
    取消整个行程：清理 session_state 中所有行程相关数据，取消虚拟时钟注册。
    """
    global session_state
    session_state["selected_pairs"] = []
    session_state["task_list"] = []
    session_state["spatial_matrix"] = {}
    session_state["phase"] = None
    session_state["main_plan"] = None
    session_state["now_str"] = None
    session_state["confirmed_ids"] = []
    session_state["rejected_ids"] = []
    session_state["pending_anomalies"] = []
    # 清理虚拟时钟调度节点
    _tm = time_master.get_master()
    _cs = _tm.get_session(_CLOCK_SESSION_ID)
    if _cs:
        _tm.set_schedule(_CLOCK_SESSION_ID, [])
    return jsonify({"status": "cancelled"})


@app.route("/api/planb_default", methods=["POST"])
def api_planb_default():
    """
    Plan B 默认兜底：用 LLM 分析当前异常 + 行程状态，自动选择最优 action 并执行。
    输入: { anomaly_type: str, corrupted_node_id: str }
    """
    global session_state
    data = request.get_json(silent=True) or {}
    anomaly_type = data.get("anomaly_type", "")
    corrupted_node_id = data.get("corrupted_node_id", "")

    task_list = session_state.get("task_list", [])
    if not task_list:
        return jsonify({"error": "无活跃行程"}), 400

    # 找受灾节点
    corrupted_task = None
    corrupted_idx = -1
    for i, t in enumerate(task_list):
        if t["task_id"] == corrupted_node_id:
            corrupted_task = t
            corrupted_idx = i
            break
    if not corrupted_task:
        corrupted_task = task_list[0] if task_list else None
        corrupted_idx = 0

    # 检查是否有同品类替选店铺
    corrupted_cat = corrupted_task.get("category", "") if corrupted_task else ""
    swap_available = False
    if corrupted_cat and agent and agent.poi_cache:
        for sid, shop in agent.poi_cache.items():
            if shop.get("category") == corrupted_cat and sid != corrupted_node_id:
                swap_available = True
                break

    # 构建 LLM 决策 prompt
    task_summary = "\n".join([
        f"- {t['name']} ({t.get('category', '')}) {'⚠️受灾节点' if t.get('task_id') == corrupted_node_id else ''}"
        for t in task_list
    ])

    decision_prompt = f"""异常类型: {anomaly_type}
受灾节点: {corrupted_task['name'] if corrupted_task else '未知'}
同品类替选可用: {'是' if swap_available else '否'}
当前行程:
{task_summary}

请选择最优 Plan B action，只返回一个词:
- SWAP (如有替选店铺，优先换店)
- BYPASS (如该节点非核心，可跳过)
- POSTPONE (如延后不影响整体)
- TRANSPORT (如天气/交通问题，改出行方式)"""

    # 默认 fallback
    chosen_action = "POSTPONE"
    action_desc = "已延后受灾节点"

    try:
        decision_msgs = [
            {"role": "system", "content": "你是行程应急决策助手。只返回一个词: SWAP/BYPASS/POSTPONE/TRANSPORT"},
            {"role": "user", "content": decision_prompt}
        ]
        decision_resp = agent._call_llm(decision_msgs, max_tokens=50)
        raw = (decision_resp.content or "").strip().upper()
        for act in ["SWAP", "BYPASS", "POSTPONE", "TRANSPORT"]:
            if act in raw:
                chosen_action = act
                break
    except Exception as e:
        print(f"[planb_default] LLM 决策失败: {e}，fallback=POSTPONE")

    # —— 执行决策 ——
    spatial_matrix = session_state.get("spatial_matrix", {})
    now_str = session_state.get("now_str", "10:00")

    if chosen_action == "SWAP" and swap_available and corrupted_cat:
        # 找替选店铺并替换
        candidates = []
        for sid, shop in agent.poi_cache.items():
            if shop.get("category") == corrupted_cat and sid != corrupted_node_id:
                candidates.append((sid, shop))
        if candidates:
            candidates.sort(key=lambda x: x[1].get("rating", 0), reverse=True)
            new_sid, new_shop = candidates[0]
            task_list[corrupted_idx] = {
                "task_id": new_sid, "name": new_shop["name"],
                "location_id": new_sid,
                "duration_minutes": corrupted_task["duration_minutes"] if corrupted_task else 60,
                "human_needed": corrupted_task.get("human_needed", True) if corrupted_task else True, "fixed_start_time": None, "category": corrupted_cat,
            }
            spatial_matrix["locations"][new_sid] = {
                "name": new_shop["name"],
                "coord": f"{new_shop.get('lat', 39.93)},{new_shop.get('lng', 116.45)}",
            }
            action_desc = f"已自动替换为 {new_shop['name']}"

    elif chosen_action == "BYPASS" and corrupted_idx >= 0:
        task_list.pop(corrupted_idx)
        action_desc = "已移除受灾节点"

    elif chosen_action == "TRANSPORT":
        # 改交通方式为 TAXI
        for route_key in spatial_matrix.get("routes", {}):
            spatial_matrix["routes"][route_key]["transport_mode"] = "TAXI"
        session_state["transport"] = "打车"
        action_desc = "已切换为打车出行"

    else:  # POSTPONE
        if corrupted_idx >= 0:
            t = task_list.pop(corrupted_idx)
            task_list.append(t)
            t["delay_because_anomaly"] = 30
        action_desc = "已延后受灾节点"

    session_state["task_list"] = task_list
    session_state["spatial_matrix"] = spatial_matrix
    session_state["confirmed_ids"] = []
    session_state["rejected_ids"] = []

    # 重跑排程
    schedule_res = backend.skill_scheduler.solve_concurrent_timeline(
        task_list, spatial_matrix, now_str,
        session_state["confirmed_ids"], session_state["rejected_ids"],
    )

    if schedule_res.get("status") == "SUCCESS":
        cleaned = []
        for item in schedule_res["timeline"]:
            sub = None
            if item["task_id"]:
                for t in task_list:
                    if t["task_id"] == item["task_id"]:
                        sub = {"action": t["name"], "duration_minutes": t["duration_minutes"]}
                        break
            cleaned.append({
                "time": item["time"], "memo": item.get("memo", ""),
                "action": item["action"], "sub_task": sub,
                "task_id": item.get("task_id", ""),
            })
        return jsonify({
            "status": "SUCCESS", "action_taken": chosen_action,
            "action_desc": action_desc,
            "departure_time": schedule_res["suggested_departure_time"],
            "total_minutes": schedule_res["total_duration_minutes"],
            "timeline": cleaned,
        })
    else:
        return jsonify({"status": "ERROR", "message": schedule_res.get("message", "AI 决策执行失败")})


# ======================================================================
# 虚拟时钟 API
# ======================================================================

@app.route("/api/clock/init", methods=["POST"])
def clock_init():
    """初始化或重置虚拟时钟，保留现有 WATER/MED 提醒节点"""
    data = request.get_json() or {}
    tm = time_master.get_master()
    initial_time = data.get("initial_time", "08:00")
    start_date = data.get("start_date", datetime.now().strftime("%Y-%m-%d"))
    nodes = data.get("schedule_nodes", [])
    clock = tm.get_or_create_session(_CLOCK_SESSION_ID, initial_time=initial_time, start_date=start_date)
    # 始终更新 start_date（每次 init 都重置为传入日期，防止 session 残留旧日期）
    clock.start_date = start_date
    # 保留现有 WATER/MED 提醒节点，合并前端传的非提醒节点
    existing = list(clock.schedule_nodes) if clock.schedule_nodes else []
    reminder_nodes = [n for n in existing if n.get("type") in ("WATER", "MED")]
    merged = reminder_nodes + [n for n in nodes if n.get("type") not in ("WATER", "MED")]
    tm.set_schedule(_CLOCK_SESSION_ID, merged, initial_time=initial_time)
    h, m = initial_time.split(":")[:2]
    clock.virtual_minutes = float(int(h) * 60 + int(m))
    clock.virtual_day = 0
    clock.is_running = False
    session_state["clock_enabled"] = True
    # 存储 trip_start_date 供多日行程使用
    session_state["trip_start_date"] = start_date
    return jsonify(clock.to_dict())


@app.route("/api/clock/status", methods=["GET"])
def clock_status():
    """获取当前时钟状态"""
    tm = time_master.get_master()
    cs = tm.get_session(_CLOCK_SESSION_ID)
    if not cs:
        return jsonify({"virtual_time": None, "speed": 0, "is_running": False, "schedule_count": 0})
    d = cs.to_dict()
    d["speed"] = round(d["speed"] * 60)  # 内部速度 → multiplier (1/60/300)
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
    cs = tm.get_session(_CLOCK_SESSION_ID)
    res["is_running"] = cs.is_running if cs else False
    return jsonify(res)


@app.route("/api/clock/jump", methods=["POST"])
def clock_jump():
    """跳转到指定时间"""
    data = request.get_json() or {}
    target = data.get("target", "14:00")
    tm = time_master.get_master()
    res = tm.jump(_CLOCK_SESSION_ID, target)
    _process_clock_triggers(res)
    cs = tm.get_session(_CLOCK_SESSION_ID)
    res["is_running"] = cs.is_running if cs else False
    return jsonify(res)


@app.route("/api/clock/jump_day", methods=["POST"])
def clock_jump_day():
    """日期导航：切换 virtual_day（+1 下一天，-1 上一天），保持当天时间不变"""
    data = request.get_json() or {}
    delta = int(data.get("delta", 0))
    tm = time_master.get_master()
    res = tm.jump_day(_CLOCK_SESSION_ID, delta)
    cs = tm.get_session(_CLOCK_SESSION_ID)
    res["is_running"] = cs.is_running if cs else False
    return jsonify(res)


@app.route("/api/clock/speed", methods=["POST"])
def clock_set_speed():
    """设置倍速（只记倍速，不启动走时）: 前端直接传虚拟分钟/秒 (1/60=1x, 1=60x, 5=300x)"""
    data = request.get_json() or {}
    speed = float(data.get("speed", 1.0/60))
    tm = time_master.get_master()
    res = tm.set_speed(_CLOCK_SESSION_ID, speed)
    cs = tm.get_session(_CLOCK_SESSION_ID)
    return jsonify({
        "status": res.get("status", "SUCCESS"),
        "speed": speed,
        "virtual_time": res.get("new_virtual_time", cs.virtual_time if cs else "12:00:00"),
        "is_running": cs.is_running if cs else False,
    })


@app.route("/api/clock/start", methods=["POST"])
def clock_start():
    """启动/继续自动走时（以当前记录的速度启动）"""
    tm = time_master.get_master()
    cs = tm.get_session(_CLOCK_SESSION_ID)
    speed = cs.speed if cs else (1.0/60)
    tm.stop_auto_tick(_CLOCK_SESSION_ID)
    res = tm.start_auto_tick(_CLOCK_SESSION_ID, speed)
    return jsonify({
        "status": res.get("status", "SUCCESS"),
        "speed": speed,
        "virtual_time": res.get("new_virtual_time", cs.virtual_time if cs else "12:00:00"),
        "is_running": True,
    })


@app.route("/api/clock/stop", methods=["POST"])
def clock_stop():
    """停止自动走时。power_off=true 时同步关闭虚拟时钟（关机），否则仅暂停走时。"""
    data = request.get_json() or {}
    power_off = data.get("power_off", False)
    if power_off:
        session_state["clock_enabled"] = False
    tm = time_master.get_master()
    tm.stop_auto_tick(_CLOCK_SESSION_ID)
    cs = tm.get_session(_CLOCK_SESSION_ID)
    return jsonify({"status": "STOPPED", "virtual_time": cs.virtual_time if cs else "08:00"})


@app.route("/api/clock/events", methods=["GET"])
def clock_pop_events():
    """消费未读的触发事件，自动走时期间也走提醒管线"""
    tm = time_master.get_master()
    cs = tm.get_session(_CLOCK_SESSION_ID)
    raw_events = tm.pop_triggered_events(_CLOCK_SESSION_ID)
    # 筛选提醒类节点 + 行程类节点（WAKE_UP→防坑指南, BEDTIME→疲劳调研）走管线处理
    reminder_nodes = [e for e in raw_events if isinstance(e, dict) and e.get("type") in ("WATER", "MED", "CUSTOM")]
    trip_nodes = [e for e in raw_events if isinstance(e, dict) and e.get("action") in ("WAKE_UP", "BEDTIME")]
    other_events = [e for e in raw_events if e not in reminder_nodes and e not in trip_nodes]
    if reminder_nodes or trip_nodes:
        fake_res = {
            "ticked_minutes_list": [],
            "triggered_nodes": reminder_nodes + trip_nodes,
            "current_date": cs.current_date_str if cs else "",
            "virtual_day": cs.virtual_day if cs else 0,
        }
        _process_clock_triggers(fake_res)
        # 管线处理后的事件已在 SSE 广播 + 推回队列，取出来
        processed = tm.pop_triggered_events(_CLOCK_SESSION_ID)
        all_events = other_events + processed
    else:
        all_events = raw_events
    return jsonify({
        "events": all_events,
        "virtual_time": cs.virtual_time if cs else "08:00",
    })


@app.route("/api/clock/set_schedule", methods=["POST"])
def clock_set_schedule():
    """设置排程节点（仅虚拟时间开启时有效）"""
    if not session_state.get("clock_enabled"):
        return jsonify({"status": "SKIPPED", "count": 0, "reason": "clock_disabled"})
    data = request.get_json() or {}
    nodes = data.get("nodes", [])
    tm = time_master.get_master()
    _cs = tm.get_session(_CLOCK_SESSION_ID)
    if not _cs:
        return jsonify({"status": "SKIPPED", "count": 0, "reason": "no_clock_session"})
    tm.set_schedule(_CLOCK_SESSION_ID, nodes)
    return jsonify({"status": "SUCCESS", "count": len(nodes)})


def _process_clock_triggers(res: dict):
    """时钟事件产生后，调用 reminder_skill 处理并注入事件队列。
    同时处理 WAKE_UP（防坑指南推送）和 BEDTIME（疲劳调研）事件。"""
    ticked = res.get("ticked_minutes_list", [])
    events = res.get("triggered_nodes", [])
    current_date = res.get("current_date", "")
    virtual_day = res.get("virtual_day", 0)

    # 读取个人信息，注入到提醒管线
    profile = _read_profile()
    personal = profile.get("personal", {})
    elder_name = personal.get("elder_name", "")
    emergency_contact_name = personal.get("emergency_contact_name", "")
    emergency_contact_phone = personal.get("emergency_contact_phone", "")
    if emergency_contact_name or emergency_contact_phone:
        emergency_contact = f"{emergency_contact_name}：{emergency_contact_phone}"
    else:
        emergency_contact = ""

    # 分离提醒类事件和行程类事件
    reminder_events = [e for e in events if isinstance(e, dict) and e.get("type") in ("WATER", "MED", "CUSTOM")]
    trip_events = [e for e in events if isinstance(e, dict) and e.get("action") in ("WAKE_UP", "BEDTIME")]

    # 始终调用 process_reminder_pipeline：
    # - 有新事件时：处理它们（响铃 + 状态初始化）
    # - 无新事件时：检查挂起事件是否超时（催促链）
    alerts = reminder_skill.process_reminder_pipeline(
        _CLOCK_SESSION_ID, ticked, reminder_events, time_master.get_master(),
        elder_name=elder_name,
        emergency_contact=emergency_contact,
    )

    # 将 reminder alerts 注入到 time_master 的事件队列
    _tm = time_master.get_master()
    for alert in alerts:
        _tm.push_triggered_event(_CLOCK_SESSION_ID, alert)

    # ——— 处理行程事件：WAKE_UP → 防坑指南推送，BEDTIME → 疲劳调研 ———
    for ev in trip_events:
        action = ev.get("action", "")
        if action == "WAKE_UP":
            # 推送防坑指南
            pitfall_event = _build_pitfall_guide_event(ev, current_date, virtual_day)
            if pitfall_event:
                _tm.push_triggered_event(_CLOCK_SESSION_ID, pitfall_event)
                alerts.append(pitfall_event)
        elif action == "BEDTIME":
            # 推送疲劳调研
            fatigue_event = _build_fatigue_survey_event(ev, current_date, virtual_day)
            if fatigue_event:
                _tm.push_triggered_event(_CLOCK_SESSION_ID, fatigue_event)
                alerts.append(fatigue_event)

    # ——— 诊断日志 ———
    if events:
        app.logger.info(f'[ClockTrigger] raw_nodes={len(events)} types={[e.get("type") for e in events]}')
    if alerts:
        app.logger.info(f'[ClockTrigger] alerts={len(alerts)} types={[a.get("type") for a in alerts]} sse_clients={len(_sse_clients)}')

    # ——— SSE 广播：通知所有连接的客户端 ———
    _broadcast_sse_events(alerts if alerts else events)


def _load_pitfall_cache() -> dict:
    """加载北京景点防坑缓存数据库"""
    import os as _os
    _cache_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "cache", "pitfall")
    _cache_file = _os.path.join(_cache_dir, "beijing_pitfall_cache.json")
    if _os.path.exists(_cache_file):
        try:
            with open(_cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _lookup_pitfall_cache(shops: list) -> dict:
    """在缓存中查找店铺的防坑建议，返回 {shop_name: {tips, transport, ...}}"""
    cache = _load_pitfall_cache()
    attractions = cache.get("attractions", [])
    matched = {}
    for cat, sid, sname in shops:
        sname_clean = sname.strip()
        for att in attractions:
            keywords = att.get("keywords", [])
            # 模糊匹配：店铺名含有关键词，或关键词含有店铺名
            for kw in keywords:
                if kw in sname_clean or sname_clean in kw:
                    matched[sname_clean] = {
                        "name": att.get("name", sname_clean),
                        "tips": att.get("pitfall_tips", []),
                        "transport": att.get("transport_tips", ""),
                        "best_time": att.get("best_time", ""),
                    }
                    break
            if sname_clean in matched:
                break
    return matched


def _generate_pitfall_insights_for_day(day_index: int, virtual_day: int) -> dict:
    """为指定天生成防坑指南内容，缓存数据优先，补充品类级提示"""
    trip_days = session_state.get("days", [])
    result = {
        "global_reminders": [],
        "destination_tips": [],
        "day_shops": [],
    }

    # 加载全局提醒
    cache = _load_pitfall_cache()
    for r in cache.get("global_reminders", []):
        result["global_reminders"].append(r.get("text", ""))

    if day_index < len(trip_days):
        day_data = trip_days[day_index]
        shops = day_data.get("selected_pairs", [])  # [(cat, sid, sname), ...]

        # 1. 查缓存
        cached = _lookup_pitfall_cache(shops)

        # 2. 对未命中缓存的店铺，尝试使用 anti-pitfall skill 的品类规则
        try:
            from skills.destination_anti_pitfall import destination_anti_pitfall as skill_pitfall
            pipeline_nodes = []
            missed_shops = [(c, s, n) for c, s, n in shops if n.strip() not in cached]
            for cat, sid, sname in (missed_shops or shops):
                info = agent.poi_cache.get(sid, {}) if hasattr(agent, 'poi_cache') else {}
                pipeline_nodes.append({
                    "node_id": sid,
                    "node_name": sname,
                    "category": cat,
                    "coordinate": info.get("coord", "39.93,116.45"),
                })
            if pipeline_nodes:
                skill_input = {
                    "trip_id": f"pitfall_day{day_index}",
                    "current_node_index": 0,
                    "pipeline_nodes": pipeline_nodes,
                    "transport": "步行",
                    "walking_tolerance_meters": 800,
                    "environmental_context": {
                        "timestamp": int(time.time()),
                        "weather_summary": "今日多云",
                        "client_platform": "WECHAT",
                    },
                }
                skill_output = skill_pitfall.execute_anti_pitfall_skill(input_payload=skill_input)
                # 品类级 global_reminders 追加（去重）
                for r in skill_output.get("global_reminders", []):
                    text = r.get("display_text", "")
                    if text and text not in result["global_reminders"]:
                        result["global_reminders"].append(text)
                # 未命中缓存的店铺使用品类级提示
                for insight in skill_output.get("localized_insights", []):
                    for cat, sid, sname in shops:
                        if sname.strip() not in cached:
                            cached[sname.strip()] = {
                                "name": sname,
                                "tips": [insight.get("content", "")],
                                "transport": "",
                                "best_time": "",
                            }
                            break
        except Exception:
            pass

        # 3. 组装结果
        for cat, sid, sname in shops:
            sname_clean = sname.strip()
            result["day_shops"].append(sname_clean)
            if sname_clean in cached:
                result["destination_tips"].append(cached[sname_clean])
            else:
                result["destination_tips"].append({
                    "name": sname_clean,
                    "tips": ["预计游玩1-2小时，请注意保管随身物品。"],
                    "transport": "",
                    "best_time": "",
                })

    return result


def _build_pitfall_guide_event(ev: dict, current_date: str, virtual_day: int) -> dict:
    """根据 WAKE_UP 事件构建防坑指南推送事件（含缓存防坑数据）"""
    trip_days = session_state.get("days", [])
    day_index = ev.get("day_index", virtual_day)
    memo = ev.get("memo", "")

    # 生成防坑内容（优先缓存）
    insights = _generate_pitfall_insights_for_day(day_index, virtual_day)

    # 构建当天目的地列表（用于兼容旧前端）
    day_shops = []
    if day_index < len(trip_days):
        day_data = trip_days[day_index]
        task_list = day_data.get("task_list", [])
        day_shops = [t.get("name", "") for t in task_list if t.get("name")]

    return {
        "type": "PITFALL_GUIDE",
        "id": f"pitfall_{current_date}",
        "time": ev.get("time", ""),
        "date": current_date,
        "label": "今日防坑指南",
        "memo": memo,
        "day_index": virtual_day,
        "destinations": day_shops,
        "message": f"早上好！今天是{current_date}，出发前查看今日防坑指南，避开常见陷阱。",
        "global_reminders": insights.get("global_reminders", []),
        "destination_tips": insights.get("destination_tips", []),
    }


def _build_fatigue_survey_event(ev: dict, current_date: str, virtual_day: int) -> dict:
    """根据 BEDTIME 事件构建疲劳程度调研事件"""
    trip_days = session_state.get("days", [])
    day_index = ev.get("day_index", virtual_day)
    day_label = f"第{virtual_day + 1}天"
    # 获取当天疲劳分析数据（从 hotel_info 中读取智能排程的预测）
    fatigue_info = {}
    if day_index < len(trip_days):
        day_data = trip_days[day_index]
        hotel_info = day_data.get("hotel_info", {})
        if hotel_info:
            fatigue_pct = hotel_info.get("fatigue", 0)
            if isinstance(fatigue_pct, (int, float)) and fatigue_pct <= 1.0:
                fatigue_pct = round(fatigue_pct * 100)
            fatigue_info = {
                "fatigue_level": hotel_info.get("fatigue_level", 100),
                "fatigue_label": hotel_info.get("fatigue_label", ""),
                "fatigue_pct": fatigue_pct,
            }
    return {
        "type": "FATIGUE_SURVEY",
        "id": f"fatigue_{current_date}",
        "time": ev.get("time", ""),
        "date": current_date,
        "label": "每日疲劳调研",
        "memo": ev.get("memo", ""),
        "day_index": virtual_day,
        "day_label": day_label,
        "fatigue_info": fatigue_info,
        "message": f"一天的行程结束啦！今天的疲劳程度符合预期吗？",
        "survey_options": [
            {"value": 1, "label": "与预期相符"},
            {"value": 2, "label": "比预期更累"},
            {"value": 3, "label": "比预期更轻松"},
        ],
    }


# ======================================================================
# SSE 实时推送引擎
# ======================================================================

_sse_clients: list = []  # 存放所有活动 SSE 客户端的 queue


def _broadcast_sse_events(events: list):
    """向所有 SSE 客户端广播事件"""
    if not events:
        return
    dead = []
    payload = f"data: {json.dumps({'events': events}, ensure_ascii=False)}\n\n"
    for q in _sse_clients:
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        if q in _sse_clients:
            _sse_clients.remove(q)


@app.route("/api/sse/events")
def sse_events():
    """SSE 端点：前端通过 EventSource 连接，实时接收时钟推进事件"""
    q: queue.Queue = queue.Queue()
    _sse_clients.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield data
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ======================================================================
# 独立于虚拟时钟的后台提醒轮询线程
# 无论虚拟时钟开或关，用系统真实时间独立检测提醒节点到期
# ======================================================================

# 记录每个提醒节点今天是否已触发过（key: task_id），每天清零
_realtime_reminder_fired_today: dict = {}
_realtime_reminder_date: str = ""
_realtime_reminder_lock = threading.Lock()


def _realtime_reminder_poller():
    """
    后台线程：每 30 秒用系统真实时间轮询一次。
    - 不依赖虚拟时钟
    - 不依赖 clock_enabled
    - 到时间的 WATER/MED 节点直接 SSE 广播弹窗事件
    - repeat=daily 的节点每天自动重新就绪
    """
    global _realtime_reminder_fired_today, _realtime_reminder_date
    while True:
        try:
            # 虚拟时钟开启时，提醒由虚拟时间驱动，真实时间轮询暂停
            if session_state.get("clock_enabled"):
                _time.sleep(30)
                continue

            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            now_time = now.strftime("%H:%M")

            # 日期变了，清空已触发标记（允许次日重新提醒）
            with _realtime_reminder_lock:
                if _realtime_reminder_date != today_str:
                    _realtime_reminder_fired_today = {}
                    _realtime_reminder_date = today_str

            # 从虚拟时钟 session 读取所有 WATER/MED 节点
            tm = time_master.get_master()
            cs = tm.get_session(_CLOCK_SESSION_ID)
            if cs and cs.schedule_nodes:
                alerts = []
                for n in cs.schedule_nodes:
                    ntype = n.get("type", "")
                    if ntype not in ("WATER", "MED", "CUSTOM"):
                        continue
                    # 每周重复: 检查今天是否在选中的星期几内
                    if n.get("repeat") == "weekly":
                        weekdays = n.get("weekdays", [])
                        if weekdays:
                            today_wd = now.strftime("%a").lower()  # mon, tue, ...
                            if today_wd not in weekdays:
                                continue
                    tid = n.get("id", "")
                    node_time = n.get("time", "")  # "HH:MM"
                    if not tid or not node_time:
                        continue

                    # WATER: 检查 sub_times 数组（如不存在则 fallback 到 time 字段）
                    if ntype == "WATER":
                        sub_times = n.get("sub_times", [node_time])
                        if now_time not in sub_times:
                            continue
                        # 用子时间唯一标识防重复
                        sub_key = tid + "_" + now_time.replace(":", "")
                    else:
                        # MED: 只有到达提醒时间才触发（误差 1 分钟内）
                        if node_time != now_time:
                            continue
                        sub_key = tid

                    # 今天已触发过则跳过（防重复）
                    with _realtime_reminder_lock:
                        if _realtime_reminder_fired_today.get(sub_key):
                            continue
                        _realtime_reminder_fired_today[sub_key] = True

                    label = n.get("label", "喝水" if ntype == "WATER" else "吃药")
                    if ntype == "WATER":
                        alerts.append({
                            "type": "WATER_RINGING_ALERT",
                            "med_id": tid,
                            "task_id": tid,
                            "message": f"⏰ {label}时间到了！该喝水了 💧",
                            "label": label,
                            "time": now_time,
                            "ring_mode": n.get("ring_mode", "once"),
                        })
                    elif ntype == "CUSTOM":
                        alerts.append({
                            "type": "CUSTOM_RINGING_ALERT",
                            "med_id": tid,
                            "task_id": tid,
                            "message": "⏰ " + n.get("content", n.get("label", "自定义提醒")),
                            "label": label,
                            "time": now_time,
                            "content": n.get("content", ""),
                            "note": n.get("note", ""),
                            "ring_mode": n.get("ring_mode", "once"),
                        })
                    else:
                        alerts.append({
                            "type": "MED_RINGING_ALERT",
                            "med_id": tid,
                            "task_id": tid,
                            "message": f"⏰ {label}时间到了！请按时服药 💊",
                            "label": label,
                            "time": node_time,
                            "ring_mode": n.get("ring_mode", "once"),
                            "meal_timing": n.get("meal_timing", ""),
                            "images": n.get("images", []),
                            "pill_shape": n.get("pill_shape", ""),
                            "pill_color": n.get("pill_color", ""),
                            "pill_color2": n.get("pill_color2", ""),
                            "dosage": n.get("dosage", ""),
                            "med_name": n.get("med_name", ""),
                        })

                if alerts:
                    _broadcast_sse_events(alerts)

        except Exception:
            pass  # 静默，不因一次异常终止

        _time.sleep(30)


_realtime_poller_started = False
_realtime_poller_thread = None


def _ensure_realtime_poller():
    """确保独立轮询线程已启动（幂等）"""
    global _realtime_poller_started, _realtime_poller_thread
    if _realtime_poller_started:
        return
    _realtime_poller_started = True
    _realtime_poller_thread = threading.Thread(
        target=_realtime_reminder_poller,
        daemon=True,
        name="realtime-reminder-poller"
    )
    _realtime_poller_thread.start()
    print("⏰ 独立提醒轮询线程已启动（不依赖虚拟时钟）")


# ======================================================================
# 提醒任务管理 API
# ======================================================================

@app.route("/api/reminder/restore", methods=["POST"])
def reminder_restore_from_profile():
    """从管家记忆恢复所有提醒到虚拟时钟"""
    profile = _read_profile()
    custom_nodes = profile.get("custom_reminders", [])
    tm = time_master.get_master()
    cs = tm.get_or_create_session(_CLOCK_SESSION_ID)
    existing_ids = {n.get("id") for n in cs.schedule_nodes} if cs else set()
    current = list(cs.schedule_nodes) if cs else []
    restored = 0
    for cr in custom_nodes:
        if not cr.get("id") or not cr.get("time"):
            continue
        if cr["id"] in existing_ids:
            continue
        node = {
            "id": cr["id"], "type": "CUSTOM", "time": cr["time"],
            "state": "pending", "label": cr.get("label", ""),
            "repeat": cr.get("repeat", "daily"), "date": cr.get("date", ""),
            "images": cr.get("images", []), "note": cr.get("note", ""),
            "created_at": cr.get("created_at", ""),
            "ring_mode": cr.get("ring_mode", "once"),
        }
        current.append(node)
        existing_ids.add(cr["id"])
        restored += 1
    tm.set_schedule(_CLOCK_SESSION_ID, current)
    return jsonify({"status": "SUCCESS", "restored": restored})


@app.route("/api/reminder/tasks", methods=["GET"])
def reminder_get_tasks():
    """获取当前虚拟时钟中的所有 WATER/MED/CUSTOM 排程节点"""
    tm = time_master.get_master()
    cs = tm.get_session(_CLOCK_SESSION_ID)
    if not cs:
        return jsonify({"tasks": []})
    tasks = []
    for n in cs.schedule_nodes:
        if n.get("_postponed"):
            continue
        if n.get("type") in ("WATER", "MED", "CUSTOM"):
            tasks.append(n)
    return jsonify({"tasks": tasks})


@app.route("/api/reminder/add_task", methods=["POST"])
def reminder_add_task():
    """添加一个提醒节点到虚拟时钟（自动初始化时钟会话）。
    支持扩展字段: date, images, note, repeat, ring_mode"""
    data = request.get_json(silent=True) or {}
    node = data.get("node", {})
    if not node or not node.get("id") or not node.get("time") or not node.get("type"):
        return jsonify({"status": "ERROR", "message": "缺少必填字段"}), 400
    # 标准化节点结构
    normalized = {
        "id": node["id"],
        "type": node["type"],
        "time": node["time"],
        "state": node.get("state", "pending"),
        "label": node.get("label", ""),
        "repeat": node.get("repeat", "daily"),
        "date": node.get("date", ""),
        "images": node.get("images", []),
        "note": node.get("note", ""),
        "created_at": node.get("created_at", ""),
        "ring_mode": node.get("ring_mode", "once"),
        "dosage": node.get("dosage", ""),
        "meal_timing": node.get("meal_timing", ""),
        "pill_shape": node.get("pill_shape", ""),
        "pill_color": node.get("pill_color", ""),
        "pill_color2": node.get("pill_color2", ""),
        "med_name": node.get("med_name", ""),
        "content": node.get("content", ""),
        "sub_times": node.get("sub_times", []),
        "interval_minutes": node.get("interval_minutes", 90),
        "start_time": node.get("start_time", ""),
        "end_time": node.get("end_time", ""),
        "weekdays": node.get("weekdays", []),
    }
    tm = time_master.get_master()
    cs = tm.get_or_create_session(_CLOCK_SESSION_ID)
    current = list(cs.schedule_nodes) if cs else []
    current.append(normalized)
    tm.set_schedule(_CLOCK_SESSION_ID, current)
    # CUSTOM 类型自动持久化到管家记忆
    if normalized["type"] == "CUSTOM":
        _persist_custom_reminders(current)
    return jsonify({"status": "SUCCESS"})


@app.route("/api/reminder/remove_task", methods=["POST"])
def reminder_remove_task():
    """删除指定 id 的提醒节点，同步清除已触发但未消费的事件"""
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id", "")
    if not task_id:
        return jsonify({"status": "ERROR", "message": "缺少 task_id"}), 400
    tm = time_master.get_master()
    cs = tm.get_session(_CLOCK_SESSION_ID)
    if not cs:
        return jsonify({"status": "SUCCESS"})
    # 1. 从排程节点中移除
    current = [n for n in cs.schedule_nodes if n.get("id") != task_id]
    tm.set_schedule(_CLOCK_SESSION_ID, current)
    # 2. 清除已触发但未消费的事件队列中属于该提醒的事件
    cs.triggered_queue = [
        e for e in cs.triggered_queue
        if not (e.get("med_id") == task_id or e.get("task_id") == task_id or e.get("id") == task_id)
    ]
    # 如果删除的是自定义提醒，同步持久化
    _persist_custom_reminders(current)
    return jsonify({"status": "SUCCESS"})


@app.route("/api/reminder/update_task", methods=["POST"])
def reminder_update_task():
    """编辑指定 id 的提醒节点，用新数据覆盖旧节点（保留原 id）"""
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id", "")
    node = data.get("node", {})
    if not task_id or not node:
        return jsonify({"status": "ERROR", "message": "缺少 task_id 或 node"}), 400
    tm = time_master.get_master()
    cs = tm.get_session(_CLOCK_SESSION_ID)
    if not cs:
        return jsonify({"status": "ERROR", "message": "时钟会话不存在"}), 400
    # 找到并替换旧节点
    updated = False
    new_nodes = []
    for n in cs.schedule_nodes:
        if n.get("id") == task_id:
            normalized = {
                "id": task_id,  # 保留原 id
                "type": node.get("type", n.get("type", "MED")),
                "time": node.get("time", n.get("time", "")),
                "state": node.get("state", n.get("state", "pending")),
                "label": node.get("label", n.get("label", "")),
                "repeat": node.get("repeat", n.get("repeat", "daily")),
                "date": node.get("date", n.get("date", "")),
                "images": node.get("images", n.get("images", [])),
                "note": node.get("note", n.get("note", "")),
                "created_at": node.get("created_at", n.get("created_at", "")),
                "ring_mode": node.get("ring_mode", n.get("ring_mode", "once")),
                "dosage": node.get("dosage", n.get("dosage", "")),
                "meal_timing": node.get("meal_timing", n.get("meal_timing", "")),
                "pill_shape": node.get("pill_shape", n.get("pill_shape", "")),
                "pill_color": node.get("pill_color", n.get("pill_color", "")),
                "pill_color2": node.get("pill_color2", n.get("pill_color2", "")),
                "med_name": node.get("med_name", n.get("med_name", "")),
                "content": node.get("content", n.get("content", "")),
                "sub_times": node.get("sub_times", n.get("sub_times", [])),
                "interval_minutes": node.get("interval_minutes", n.get("interval_minutes", 90)),
                "start_time": node.get("start_time", n.get("start_time", "")),
                "end_time": node.get("end_time", n.get("end_time", "")),
                "weekdays": node.get("weekdays", n.get("weekdays", [])),
            }
            new_nodes.append(normalized)
            updated = True
        else:
            new_nodes.append(n)
    if not updated:
        return jsonify({"status": "ERROR", "message": "未找到对应任务"}), 404
    tm.set_schedule(_CLOCK_SESSION_ID, new_nodes)
    # CUSTOM 类型同步持久化
    _persist_custom_reminders(new_nodes)
    return jsonify({"status": "SUCCESS"})


@app.route("/api/reminder/action", methods=["POST"])
def reminder_user_action():
    """
    接收用户对服药提醒的交互响应（五步强闭环状态机）。
    输入：{"med_id": "med_001", "action": "1"|"2"|"swallow"}
    其中 "1" = 确认去拿药, "2" = 延后30分钟, "swallow" = 我已吞服药片
    额外：{"action": "fatigue_survey", "level": 1-5, "note": "...", "date": "...", "day_index": 0}
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")

    # —— 疲劳调研 action（独立处理，不走药物状态机）——
    if action == "fatigue_survey":
        level = data.get("level", 0)
        note = data.get("note", "")
        date_str = data.get("date", "")
        day_index = data.get("day_index", 0)
        # 存储到 session_state
        if "fatigue_surveys" not in session_state:
            session_state["fatigue_surveys"] = []
        session_state["fatigue_surveys"].append({
            "level": level,
            "note": note,
            "date": date_str,
            "day_index": day_index,
            "timestamp": datetime.now().isoformat(),
        })
        app.logger.info(f'[FatigueSurvey] level={level} note={note} date={date_str} day={day_index}')
        return jsonify({
            "status": "SUCCESS",
            "message": "疲劳调研已记录",
            "stored": len(session_state["fatigue_surveys"]),
        })

    med_id = data.get("med_id", "")
    if not med_id or not action:
        return jsonify({"status": "ERROR", "message": "缺少 med_id 或 action"}), 400

    # 映射 action → user_input
    action_map = {"1": "1", "2": "2", "swallow": "我已吞服药片"}
    user_input = action_map.get(action, action)

    result = reminder_skill.handle_user_action(
        _CLOCK_SESSION_ID,
        user_input,
        datetime.now().strftime("%H:%M"),
        time_master.get_master(),
    )

    # SSE 广播结果通知前端更新
    _broadcast_sse_events([{
        "type": "REMINDER_ACTION_RESULT",
        "med_id": med_id,
        "status": result.get("status"),
        "message": result.get("message"),
    }])

    return jsonify({
        "status": "SUCCESS",
        "result": result,
    })


# ======================================================================
# 药品图片上传 API
# ======================================================================

import os as _os
import uuid as _uuid

_UPLOAD_DIR = _os.path.join(base_dir, "static", "uploads", "medicines")
_os.makedirs(_UPLOAD_DIR, exist_ok=True)

@app.route("/api/reminder/upload_image", methods=["POST"])
def reminder_upload_image():
    """上传药品图片，返回可访问URL"""
    if 'image' not in request.files:
        return jsonify({"status": "ERROR", "message": "未选择文件"}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({"status": "ERROR", "message": "文件名为空"}), 400
    # 限制5MB
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 5 * 1024 * 1024:
        return jsonify({"status": "ERROR", "message": "文件超过5MB"}), 400
    # 生成唯一文件名
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
    if ext not in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
        ext = 'jpg'
    filename = f"{_uuid.uuid4().hex}.{ext}"
    filepath = _os.path.join(_UPLOAD_DIR, filename)
    file.save(filepath)
    url = f"/static/uploads/medicines/{filename}"
    return jsonify({"status": "SUCCESS", "url": url, "id": filename})


# ======================================================================
# 管家长期记忆 API — 偏好谱读写
# ======================================================================

@app.route("/api/profile/get", methods=["GET"])
def profile_get():
    """读取用户长期偏好谱"""
    return jsonify({"status": "SUCCESS", "profile": _read_profile()})


@app.route("/api/profile/set", methods=["POST"])
def profile_set():
    """增量更新用户长期偏好谱"""
    data = request.get_json(silent=True) or {}
    updates = data.get("updates", {})
    if not updates:
        return jsonify({"status": "ERROR", "message": "缺少 updates 字段"}), 400
    profile = _write_profile(updates)
    return jsonify({"status": "SUCCESS", "profile": profile})


@app.route("/api/parse-note", methods=["POST"])
def parse_note():
    """LLM 将用户自然语言中的杂项信息整理为提醒备注"""
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"note": ""})
    try:
        import requests as _requests
        resp = _requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}"},
            json={
                "model": os.getenv("FAST_LLM_MODEL", "deepseek-v4-pro"),
                "messages": [{"role": "user", "content": f"把以下文字整理成一句简洁的服药备注（不超过20字），直接输出结果不要解释：{text}"}],
                "max_tokens": 40, "temperature": 0.3
            },
            timeout=3
        )
        note = resp.json()["choices"][0]["message"]["content"].strip()
        return jsonify({"note": note})
    except Exception:
        return jsonify({"note": text})


# ======================================================================
# Phase 3.1: 管线变异器 — 防踩坑 + 异常注入
# ======================================================================

_ANOMALY_EVENT_POOL = [
    {"class": "STORE_CLOSURE", "template": "{shop}因突发电力故障暂停营业", "duration": 240},
    {"class": "QUEUE_FULL", "template": "{shop}当前排队已满，预计等待90分钟", "duration": 90},
    {"class": "WEATHER_EVENT", "template": "雷暴预警，建议减少步行出行", "duration": 120},
    {"class": "TRAFFIC_CONTROL", "template": "{shop}周边交通管制，建议绕行", "duration": 60},
]

import random as _random


@app.route("/api/pitfall/check", methods=["POST"])
def pitfall_check():
    """
    防踩坑检查 API：对传入的 pipeline_nodes 做步行距离/交通/身份一致性校验。
    复用 destination_anti_pitfall 的核心逻辑。
    """
    data = request.get_json(silent=True) or {}
    pipeline_nodes = data.get("pipeline_nodes", [])
    walking_tolerance = data.get("walking_tolerance_meters", 800)
    transport = data.get("transport", "步行")

    if not pipeline_nodes:
        return jsonify({"status": "ERROR", "message": "缺少 pipeline_nodes"}), 400

    from skills.destination_anti_pitfall import destination_anti_pitfall as skill_pitfall

    input_payload = {
        "trip_id": f"trip_{int(datetime.now().timestamp())}",
        "current_node_index": 0,
        "pipeline_nodes": pipeline_nodes,
        "transport": transport,
        "walking_tolerance_meters": walking_tolerance,
        "environmental_context": {
            "timestamp": int(datetime.now().timestamp()),
            "weather_summary": "今日多云",
            "client_platform": "WECHAT",
        },
    }

    output = skill_pitfall.execute_anti_pitfall_skill(input_payload=input_payload)
    pending = skill_pitfall.get_pending_triggers(output)

    return jsonify({
        "status": "SUCCESS",
        "global_reminders": output.get("global_reminders", []),
        "localized_insights": output.get("localized_insights", []),
        "intent_triggers": pending,
    })


@app.route("/api/anomaly/inject", methods=["POST"])
def anomaly_inject():
    """
    动态异常事件注入：从全局店铺池中随机选目标，注入异常。
    将异常挂到 pending_anomalies，然后跑一次排程让 Plan B trigger 自动出现在结果里。
    如果当前没有行程，则只返回异常信息（等下次排程时自动带上）。
    """
    data = request.get_json(silent=True) or {}
    event_class = data.get("event_class", "")
    shop_name = data.get("shop_name")

    # 选事件模板
    event_tmpl = None
    for ev in _ANOMALY_EVENT_POOL:
        if ev["class"] == event_class:
            event_tmpl = ev
            break
    if not event_tmpl:
        event_tmpl = _random.choice(_ANOMALY_EVENT_POOL)

    # 选目标店铺（优先命中当前行程中的店铺）
    target_shop = None
    all_shops = list(agent.poi_cache.values()) if agent.poi_cache else []
    # 先看当前行程里有没有匹配的
    selected_pairs = session_state.get("selected_pairs", [])
    if shop_name:
        for cat, sid, sname in selected_pairs:
            if sname == shop_name or sid == shop_name:
                info = agent.poi_cache.get(sid, {})
                target_shop = {"shop_id": sid, "name": sname, "category": cat, **info}
                break
        if not target_shop:
            for s in all_shops:
                if s.get("name") == shop_name:
                    target_shop = s
                    break
    if not target_shop and selected_pairs:
        # 从当前行程随机选一个
        cat, sid, sname = _random.choice(selected_pairs)
        info = agent.poi_cache.get(sid, {})
        target_shop = {"shop_id": sid, "name": sname, "category": cat, **info}
    if not target_shop and all_shops:
        target_shop = _random.choice(all_shops)
    if not target_shop:
        target_shop = {"shop_id": "shop_rest_01", "name": "海底捞三里屯店", "category": "hotpot", "lat": 39.936, "lng": 116.449}

    # 构建描述
    desc = event_tmpl["template"].format(shop=target_shop.get("name", "未知店铺"))

    anomaly_payload = {
        "anomaly_id": f"anom_{int(datetime.now().timestamp())}",
        "anomaly_class": event_tmpl["class"],
        "target_node_id": target_shop["shop_id"],
        "description": desc,
        "impact_duration_minutes": event_tmpl["duration"],
        "fallback_directives": {
            "action_required": "SWAP_NODE",
            "attribute_filter": {"category": target_shop.get("category", "restaurant")},
        },
    }

    # 挂到 pending_anomalies，让下次 _run_schedule 自动处理
    session_state.setdefault("pending_anomalies", []).append(anomaly_payload)

    # 如果当前有行程，立即重排以产 Plan B trigger
    if session_state.get("task_list"):
        return _run_schedule()

    return jsonify({
        "status": "SUCCESS",
        "message": f"异常已注入: {desc}，将在下次排程时触发 Plan B",
        "injected_anomaly": anomaly_payload,
    })


@app.route("/api/memory/detect", methods=["POST"])
def memory_detect():
    """
    语义偏好检测 API：用 LLM 分析用户最新消息，检测偏好变化并自动写入。
    输入: { user_message: str, context_messages: [...] }
    """
    _ensure_agent()
    data = request.get_json(silent=True) or {}
    user_message = data.get("user_message", "")
    context = data.get("context_messages", [])
    if not user_message:
        return jsonify({"status": "ERROR", "message": "缺少 user_message"}), 400

    current_profile = _read_profile()

    detect_prompt = {
        "role": "system",
        "content": (
            "你是偏好检测器。分析用户消息，提取四维度的偏好变化：\n"
            "1. 口味(taste): taste_tolerance(无辣/微辣/中辣/重辣), dietary_restrictions(忌口列表), cuisine_preference(菜系列表)\n"
            "2. 通勤(commute): walking_tolerance_meters(米), transport_priority(步行优先/打车优先/地铁优先)\n"
            "3. 预算(budget): price_level(经济/中端/高端), rating_cutoff(评分)\n"
            "4. 健康作息(lifestyle): hydration_interval_minutes(分钟), medication_schedule\n"
            "5. 景点排程模板(itinerary_templates): 当用户手动调整景点排程并明确表示「以后都按这个安排」「以后这几个点都按这个排」时记录模板。格式: [{\"template_id\": \"beijing_classic_7\", \"match_spots\": [\"八达岭长城\", ...], \"day_2_spots\": [\"圆明园\", \"颐和园\"], ...}]\n\n"
            f"当前偏好: {json.dumps(current_profile, ensure_ascii=False)}\n\n"
            "只返回 JSON，格式: {\"detected_updates\": {...}}。如果无变化返回空对象。"
        ),
    }

    messages = [detect_prompt]
    if context:
        messages.extend(context)
    messages.append({"role": "user", "content": user_message})

    try:
        llm_msg = agent._call_llm(messages, response_format={"type": "json_object"})
        content = ""
        if hasattr(llm_msg, "content"):
            content = llm_msg.content or ""
        elif hasattr(llm_msg, "choices") and llm_msg.choices:
            content = llm_msg.choices[0].message.content or ""
        elif isinstance(llm_msg, str):
            content = llm_msg
        try:
            result = json.loads(content.strip())
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[json_parse] memory_detect 解析失败，回退无变更: {content[:100]}", flush=True)
            return jsonify({"status": "SUCCESS", "detected_updates": {}, "applied": False, "message": "无偏好变化"})
        updates = result.get("detected_updates", {}) if isinstance(result, dict) else {}
        if updates:
            _write_profile(updates)
            return jsonify({"status": "SUCCESS", "detected_updates": updates, "applied": True})
        return jsonify({"status": "SUCCESS", "detected_updates": {}, "applied": False, "message": "无偏好变化"})
    except Exception as e:
        return jsonify({"status": "ERROR", "message": str(e)}), 500


@app.route("/api/pipeline/mutate", methods=["POST"])
def pipeline_mutate():
    """
    管线变异器：对当前行程执行 swap/bypass/postpone。
    输入: { action: "SWAP_NODE"|"BYPASS_NODE"|"POSTPONE_NODE", corrupted_node_id, ... }
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action", "POSTPONE_NODE")
    corrupted_node_id = data.get("corrupted_node_id", "")
    delta_minutes = data.get("delta_delay_minutes", 30)

    task_list = session_state.get("task_list", [])
    spatial_matrix = session_state.get("spatial_matrix", {})

    if not task_list:
        return jsonify({"status": "ERROR", "message": "无行程数据"}), 400

    # 找到受灾任务
    corrupted_task = None
    corrupted_idx = -1
    for i, t in enumerate(task_list):
        if t["task_id"] == corrupted_node_id:
            corrupted_task = t
            corrupted_idx = i
            break

    if not corrupted_task:
        return jsonify({"status": "ERROR", "message": f"未找到节点 {corrupted_node_id}"}), 404

    if action == "SWAP_NODE":
        # 从全局 POI 缓存中找同品类替选店铺
        corrupted_cat = corrupted_task.get("category", "")
        candidates = []
        for sid, shop in agent.poi_cache.items():
            if shop.get("category") == corrupted_cat and sid != corrupted_node_id:
                candidates.append((sid, shop))
        if not candidates:
            return jsonify({"status": "ERROR", "message": f"品类 {corrupted_cat} 无替选店铺"}), 404

        # 直接启用同品类替选店铺
        new_sid, new_shop = candidates[0]
        task_list[corrupted_idx] = {
            "task_id": new_sid,
            "name": new_shop["name"],
            "location_id": new_sid,
            "duration_minutes": corrupted_task["duration_minutes"],
            "human_needed": corrupted_task.get("human_needed", True),
            "fixed_start_time": corrupted_task.get("fixed_start_time"),
            "category": corrupted_cat,
        }
        spatial_matrix["locations"][new_sid] = {
            "name": new_shop["name"],
            "coord": f"{new_shop.get('lat', 39.93)},{new_shop.get('lng', 116.45)}",
        }
        session_state["task_list"] = task_list
        session_state["spatial_matrix"] = spatial_matrix

        # 重排
        session_state["confirmed_ids"] = []
        session_state["rejected_ids"] = []
        return _run_schedule()

    elif action == "BYPASS_NODE":
        # 直接移除受灾节点
        task_list.pop(corrupted_idx)
        session_state["task_list"] = task_list
        session_state["confirmed_ids"] = []
        session_state["rejected_ids"] = []
        return _run_schedule()

    elif action == "POSTPONE_NODE":
        # 延后受灾节点，先跑其他任务再回来
        task_list.pop(corrupted_idx)
        task_list.append(corrupted_task)
        # 增加缓冲时间标记
        corrupted_task["delay_because_anomaly"] = delta_minutes
        session_state["task_list"] = task_list
        session_state["confirmed_ids"] = []
        session_state["rejected_ids"] = []
        return _run_schedule()

    return jsonify({"status": "ERROR", "message": f"未知变异动作: {action}"}), 400


# ======================================================================
# 新 Skill API: 路径规划 / 排队监控 / 天气抽取
# ======================================================================

@app.route("/api/route/plan", methods=["POST"])
def api_route_plan():
    """
    路径规划 API：给定起点+途经点+交通偏好，返回最优路径。
    """
    data = request.get_json(silent=True) or {}
    start_coord = data.get("start_coord", DEFAULT_CENTER_COORD)
    waypoints = data.get("waypoints", [])
    transport_pref = data.get("transport_preference", "步行优先")
    walking_tol = data.get("walking_tolerance_meters", 800)
    weather_cond = data.get("weather_condition")

    result = _skill_route_planner(
        start_coord=start_coord,
        waypoints=waypoints,
        transport_preference=transport_pref,
        walking_tolerance_meters=walking_tol,
        weather_condition=weather_cond,
    )
    return jsonify(result)


@app.route("/api/queue/<action>", methods=["POST"])
def api_queue(action):
    """
    排队监控 API：enqueue / query / poll_all。
    """
    data = request.get_json(silent=True) or {}
    result = _skill_queue_monitor(action, **data)
    return jsonify(result)


@app.route("/api/weather", methods=["POST"])
def api_weather():
    """
    天气查询 API：返回天气+活动建议+交通影响。
    """
    data = request.get_json(silent=True) or {}
    coord = data.get("coord", DEFAULT_CENTER_COORD)
    date = data.get("date", "2026-06-06")
    result = _skill_weather_extractor(coord=coord, date=date)
    return jsonify(result)


@app.route("/api/weather/realtime", methods=["POST"])
def api_weather_realtime():
    """
    实时天气 API：调用高德天气 API 获取真实天气数据。
    接受 adcode 或 coord 参数；若提供 coord 则逆地理编码获取 adcode。
    """
    data = request.get_json(silent=True) or {}
    adcode = data.get("adcode", "")
    coord = data.get("coord", "")

    # 若未提供 adcode 但提供了坐标，逆地理编码获取 adcode
    if not adcode and coord:
        try:
            parts = coord.strip().split(",")
            lng = float(parts[1].strip())
            lat = float(parts[0].strip())
            rev_geo = _amap_client.reverse_geocode(lng=lng, lat=lat)
            if rev_geo and isinstance(rev_geo, dict):
                adcode = rev_geo.get("adcode", "")
        except Exception:
            pass

    # 兜底：默认北京
    if not adcode:
        adcode = "110000"

    try:
        result = _amap_weather_client.get_real_time_weather(adcode=adcode)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "ERROR", "message": f"天气查询失败: {str(e)}"})


@app.route("/api/weather/forecast", methods=["POST"])
def api_weather_forecast():
    """
    多日天气预报 API：调用高德预报 API 获取未来4天天气。
    请求: {city: "北京"} 或 {adcode: "110000"}
    响应: {forecasts: [{date, day_weather, day_temp, walking_penalty, outdoor_suitable, confidence}, ...]}
    """
    data = request.get_json(silent=True) or {}
    city = data.get("city", "").strip()
    adcode = data.get("adcode", "").strip()

    if not adcode and city:
        adcode = _CITY_ADCODE.get(city, "")
    if not adcode:
        # 尝试逆地理编码或兜底
        adcode = "110000"

    try:
        result = _amap_weather_client.get_weather_forecast(adcode=adcode)
        return jsonify({"status": "SUCCESS", **result})
    except Exception as e:
        return jsonify({"status": "ERROR", "message": f"天气预报查询失败: {str(e)}"})


# ======================================================================
# 高德 POI API（真实数据检索 + 地理编码）
# ======================================================================
from skills.amap_poi.amap_poi import AmapPOIClient
from skills.amap_poi.amap_poi import search_nearby
_amap_client = AmapPOIClient()

from skills.amap_weather.amap_weather import AmapWeatherClient
_amap_weather_client = AmapWeatherClient()


@app.route("/api/poi/search", methods=["POST"])
def poi_search():
    """关键字搜索 POI"""
    data = request.get_json(silent=True) or {}
    result = _amap_client.search_poi(
        keywords=data.get("keywords", ""),
        city=data.get("city", "北京"),
        category=data.get("category"),
        offset=data.get("offset", 10),
    )
    return jsonify(result)


@app.route("/api/poi/nearby", methods=["POST"])
def poi_nearby():
    """周边搜索 POI"""
    data = request.get_json(silent=True) or {}
    result = _amap_client.search_nearby(
        lng=float(data.get("lng", 116.455)),
        lat=float(data.get("lat", 39.932)),
        radius=int(data.get("radius", 3000)),
        keywords=data.get("keywords", ""),
        category=data.get("category"),
        min_rating=float(data.get("min_rating", 0)),
    )
    return jsonify(result)


@app.route("/api/poi/fuzzy", methods=["POST"])
def poi_fuzzy():
    """模糊搜索/输入提示"""
    data = request.get_json(silent=True) or {}
    result = _amap_client.fuzzy_search(
        keywords=data.get("keywords", ""),
        city=data.get("city", "北京"),
    )
    return jsonify(result)


@app.route("/api/poi/detail", methods=["POST"])
def poi_detail():
    """POI 详情查询"""
    data = request.get_json(silent=True) or {}
    result = _amap_client.get_poi_detail(
        poi_id=data.get("poi_id", ""),
    )
    return jsonify(result)


@app.route("/api/poi/geocode", methods=["POST"])
def poi_geocode():
    """地理编码：地址→坐标 / 坐标→地址"""
    data = request.get_json(silent=True) or {}
    action = data.get("action", "geocode")

    if action == "reverse":
        result = _amap_client.reverse_geocode(
            lng=float(data.get("lng", 116.455)),
            lat=float(data.get("lat", 39.932)),
        )
        return jsonify({"status": "SUCCESS", "data": result})

    result = _amap_client.geocode(
        address=data.get("address", ""),
        city=data.get("city", "北京"),
    )
    if result is None:
        return jsonify({"status": "ERROR", "message": f"未找到地址: {data.get('address', '')}"})
    return jsonify({"status": "SUCCESS", "data": result})


# ======================================================================
# 通用LLM置顶聊天框 — 聊天 API 端点
# ======================================================================

# ── 工具定义（15个 tools schema） ──
CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_poi",
            "description": "搜索周边商户。根据关键词、品类、评分等条件查找餐厅、咖啡店、理发店、宠物店等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "description": "搜索关键词"},
                    "category": {"type": "string", "description": "品类: restaurant/hair/pet/cafe/gym/cinema/laundry/hotpot/japanese"},
                    "radius_meters": {"type": "integer", "description": "搜索半径（米），默认3000"},
                    "min_rating": {"type": "number", "description": "最低评分，默认3.5"}
                },
                "required": ["keywords"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "plan_route",
            "description": "规划多节点路线。给定途经点列表和交通偏好，计算最优访问顺序与接驳方式（步行/打车/地铁/公交）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "waypoints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "地点名称"},
                                "coord": {"type": "string", "description": "坐标 lat,lng"},
                                "duration_minutes": {"type": "number", "description": "预计停留时间（分钟）"}
                            },
                            "required": ["name", "coord"]
                        },
                        "description": "途经点列表"
                    },
                    "transport_preference": {
                        "type": "string",
                        "enum": ["步行优先", "打车优先", "地铁优先", "公交优先"],
                        "description": "交通偏好，默认步行优先"
                    },
                    "start_coord": {"type": "string", "description": "出发坐标 lat,lng，默认三里屯 39.93,116.45"}
                },
                "required": ["waypoints"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hail_taxi",
            "description": "虚拟打车。模拟叫车流程，返回预估价格、等待时间和车型信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_address": {"type": "string", "description": "出发地址或坐标"},
                    "to_address": {"type": "string", "description": "目的地地址或坐标"},
                    "car_type": {"type": "string", "enum": ["快车", "优享", "专车"], "description": "车型，默认快车"}
                },
                "required": ["from_address", "to_address"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "plan_transit",
            "description": "公交+地铁+步行组合路线规划。自动计算最优公共交通方案（步行到站→乘车→换乘→步行到达）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_address": {"type": "string", "description": "出发地址"},
                    "to_address": {"type": "string", "description": "目的地地址"},
                    "prefer_mode": {"type": "string", "enum": ["最快", "最少换乘", "最少步行"], "description": "偏好模式，默认最快"}
                },
                "required": ["from_address", "to_address"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_reminder",
            "description": "添加生活提醒。支持喝水提醒(WATER)、吃药提醒(MED)、自定义提醒(CUSTOM)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["WATER", "MED", "CUSTOM"], "description": "提醒类型"},
                    "time": {"type": "string", "description": "提醒时间，格式 HH:MM，如 15:00"},
                    "label": {"type": "string", "description": "提醒标签，如'喝水'、'吃降压药'、'买菜'"},
                    "repeat": {"type": "string", "enum": ["once", "daily"], "description": "重复模式，默认once"},
                    "note": {"type": "string", "description": "备注说明"}
                },
                "required": ["type", "time", "label"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_reminder",
            "description": "删除一个提醒。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "提醒任务ID"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "列出当前所有活跃的提醒任务。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_weather",
            "description": "查询指定地点的天气信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "地点名称，如'三里屯'、'国贸'"},
                    "date": {"type": "string", "description": "日期，如'今天'、'明天'、'2026-06-29'"}
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_profile",
            "description": "读取用户偏好设置：口味、预算、通勤方式、健康作息等。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_profile",
            "description": "更新用户偏好设置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "偏好字段路径，如 taste.spicy_level, commute.preferred_transport"},
                    "value": {"type": "string", "description": "新值"}
                },
                "required": ["field", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_queue",
            "description": "查询餐厅排队状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "restaurant_name": {"type": "string", "description": "餐厅名称"}
                },
                "required": ["restaurant_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "start_trip",
            "description": "发起新的行程计划。根据用户需求搜索POI、选店、排程一步到位。",
            "parameters": {
                "type": "object",
                "properties": {
                    "requirements": {"type": "string", "description": "行程需求描述，如'理发+喝咖啡'"},
                    "time": {"type": "string", "description": "出发时间或到达时间，如'15:00'、'now'"},
                    "transport": {"type": "string", "enum": ["步行", "打车", "地铁", "公交"], "description": "交通方式，默认步行"}
                },
                "required": ["requirements"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_trip",
            "description": "修改进行中的行程（单日/多日均支持）：增加/删除目的地、调整时间、更换交通方式、换店。多日行程需指定 day_index（从0开始，0=第1天）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add_stop", "remove_stop", "change_time", "change_transport", "reroute", "swap_current", "replace_stop", "move_to_day", "no_change", "clarify"], "description": "操作类型。add_stop=新增目的地；remove_stop=删除；change_time=调整时间；change_transport=换交通方式；swap_current=换店；replace_stop=跨品类替换；move_to_day=多日行程中将活动移到另一天；no_change=放弃修改；clarify=意图不明确需追问"},
                    "params": {
                        "type": "object",
                        "properties": {
                            "keywords": {"type": "string", "description": "新增目的地关键词（如'咖啡'、'火锅'、'景点'）"},
                            "category": {"type": "string", "description": "品类代码: hair/pet/cafe/gym/restaurant/cinema/laundry/hotpot/japanese/market/scenic"},
                            "shop_name": {"type": "string", "description": "用户明确提到的店名，没提则为空字符串"},
                            "name": {"type": "string", "description": "要删除/替换的店名"},
                            "target_name": {"type": "string", "description": "swap_current/move_to_day/replace_stop/change_time 时指定要操作的活动名称"},
                            "target_category": {"type": "string", "description": "swap_current 时指定要换掉的品类"},
                            "time": {"type": "string", "description": "新时间 HH:MM 格式，如'14:00'；或相对偏移如'+30'（推迟30分钟）"},
                            "day_index": {"type": "integer", "description": "多日行程中要操作的天索引（从0开始，0=第1天）。不传则操作当前活跃天"},
                            "to_day": {"type": "integer", "description": "move_to_day 时目标天的索引（从0开始）"},
                            "to_time": {"type": "string", "description": "move_to_day 时在目标天的插入时间 HH:MM 格式"},
                            "new_start": {"type": "string", "description": "多日行程中修改当天整体出发时间 HH:MM 格式"},
                            "mode": {"type": "string", "enum": ["WALK", "TAXI", "METRO", "BUS"], "description": "交通方式"},
                            "preference": {"type": "string", "enum": ["fast", "short", "scenic", "avoid_highway"]},
                            "message": {"type": "string", "description": "clarify 时的追问内容"}
                        }
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_trip",
            "description": "取消当前进行中的行程。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_trip_status",
            "description": "查看当前行程状态：目的地列表、时间安排、交通方式等。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "geocode",
            "description": "将地名或地址解析为经纬度坐标。用于在搜索周边商户前先确定位置。例如：'东来顺饭店'→{lng:116.xxx, lat:39.xxx}。获取坐标后应继续调用 search_nearby 搜索周边。",
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "地名或详细地址，如'东来顺饭店'、'故宫'、'三里屯太古里'"},
                    "city": {"type": "string", "description": "所在城市，默认北京"}
                },
                "required": ["address"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_nearby",
            "description": "在指定坐标周边搜索商户（餐厅、景点、咖啡店等）。使用前应先通过 geocode 获取目标位置的坐标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "lng": {"type": "number", "description": "中心点经度"},
                    "lat": {"type": "number", "description": "中心点纬度"},
                    "keywords": {"type": "string", "description": "搜索关键词，如'火锅'、'咖啡'、'景点'、'好玩'"},
                    "category": {"type": "string", "description": "高德地图 POI 分类代码。常用值: scenic(景点)、restaurant(餐饮)、cafe(咖啡)、shopping(购物)、hotel(酒店)。指定后可精准过滤，推荐使用"},
                    "radius": {"type": "number", "description": "搜索半径（米），默认3000"}
                },
                "required": ["lng", "lat"]
            }
        }
    },
]


def _handle_multi_day_chat_edit(action: str, params: dict) -> dict:
    """在 CHAT_TOOL edit_trip 中处理多日模式编辑。
    将单日 edit_trip action 映射到多日 days[] 的操作。
    支持 day_index 参数让 LLM 指定操作任意天。
    """
    days = session_state.get("days", [])
    if not days:
        return {"status": "ERROR", "message": "没有活跃的多日行程"}

    active_idx = session_state.get("active_day_index", 0)
    if active_idx >= len(days):
        active_idx = 0

    # ── 支持 LLM 通过 day_index 参数指定要操作的天 ──
    day_index = params.get("day_index")
    if day_index is not None:
        try:
            di = int(day_index)
            if 0 <= di < len(days):
                active_idx = di
        except (ValueError, TypeError):
            pass  # 无效的 day_index，fallback 到 active_idx

    target_day = days[active_idx]
    target_tl = target_day.setdefault("timeline", [])

    # ── 使用行程入住坐标（酒店）作为搜索中心 ──
    center = _infer_center_coord() or DEFAULT_CENTER_COORD

    if action in ("no_change", "clarify"):
        return {"status": "SUCCESS", "message": "好的" if action == "no_change" else "请再描述一下你想怎么调整～"}

    if action == "remove_stop":
        name = params.get("name", "").strip()
        if not name or len(name) < 2:
            return {"status": "ERROR", "message": "请指定要删除的目的地名称（至少2个字）"}
        # 从 timeline 中移除匹配的节点
        new_tl = [n for n in target_tl if name not in (n.get("memo", "") or "")]
        if len(new_tl) == len(target_tl):
            return {"status": "ERROR", "message": f"在第{active_idx+1}天未找到「{name}」"}
        if not new_tl:
            return {"status": "ERROR", "message": "该操作会清空当天所有安排，已拒绝"}
        target_day["timeline"] = new_tl
        # 同步 task_list
        target_day["task_list"] = [t for t in target_day.get("task_list", [])
                                   if name not in (t.get("name", "") or "")]
        # 同步 selected_pairs
        target_day["selected_pairs"] = [(c, s, n) for c, s, n in target_day.get("selected_pairs", [])
                                         if name not in n]
        session_state["days"] = days
        return {"status": "SUCCESS",
                "message": f"已从第{active_idx+1}天移除「{name}」",
                "data": {"days": days}}

    if action == "add_stop":
        kw = params.get("keywords", "") or params.get("shop_name", "")
        cat = params.get("category", "")
        shop_name = params.get("shop_name", "")
        shop_id = params.get("shop_id", "")
        time_str = params.get("time", "12:00")
        if not kw:
            return {"status": "ERROR", "message": "请提供搜索关键词（如「咖啡」「景点」）"}
        # 用户未指定具体店铺 → 返回候选列表，不自动添加
        if not shop_name and not shop_id:
            try:
                search_cat = [cat] if cat else ["restaurant"]
                new_res = backend.skill_poi.search_poi_matrix(
                    center_coord=center,
                    categories=search_cat,
                    radius_meters=5000,
                    min_rating=3.5,
                    keywords=kw
                )
                candidates = []
                for c, shops in new_res.get("search_results", {}).items():
                    for s in shops[:5]:
                        candidates.append({
                            "shop_id": str(s.get("shop_id", "")),
                            "name": s.get("name", ""),
                            "category": c,
                            "rating": s.get("rating", 0),
                        })
                return {"status": "SUCCESS", "phase": "need_shop_selection",
                        "message": f"找到 {len(candidates)} 个「{kw}」相关商户，请选择",
                        "candidates": candidates}
            except Exception as e:
                return {"status": "ERROR", "message": f"搜索失败: {str(e)}"}
        # 用户指定了具体店铺 → 自动添加
        try:
            # 搜索 POI（使用 _infer_center_coord 优先酒店坐标）
            search_cat = [cat] if cat else ["restaurant"]
            new_res = backend.skill_poi.search_poi_matrix(
                center_coord=center,
                categories=search_cat,
                radius_meters=5000,
                min_rating=3.5,
                keywords=kw
            )
            found = False
            for c, shops in new_res.get("search_results", {}).items():
                if shops:
                    best = max(shops, key=lambda s: s.get("rating", 0))
                    new_node = {
                        "time": time_str,
                        "action": "VISIT",
                        "memo": best["name"],
                        "category": c,
                        "shop_id": str(best.get("shop_id", "")),
                        "lat": best.get("lat", ""),
                        "lng": best.get("lng", ""),
                        "duration_minutes": _duration(c) if c else 60,
                        "rating": best.get("rating", 0),
                    }
                    # 按时间插入
                    new_min = _time_str_to_minutes(time_str)
                    insert_idx = len(target_tl)
                    for i, n in enumerate(target_tl):
                        nt = n.get("time", "")
                        n_min = _time_str_to_minutes(nt) if nt else 9999
                        if new_min < n_min:
                            insert_idx = i
                            break
                    target_tl.insert(insert_idx, new_node)
                    target_day.setdefault("task_list", []).append({
                        "name": best["name"], "category": c,
                        "shop_id": str(best.get("shop_id", "")),
                        "lat": best.get("lat", ""), "lng": best.get("lng", ""),
                        "duration_minutes": new_node["duration_minutes"],
                    })
                    target_day.setdefault("selected_pairs", []).append((c, str(best.get("shop_id", "")), best["name"]))
                    found = True
                    break
            if found:
                session_state["days"] = days
                return {"status": "SUCCESS",
                        "message": f"已添加「{kw}」到第{active_idx+1}天",
                        "data": {"days": days}}
            return {"status": "ERROR", "message": f"未找到「{kw}」相关商户"}
        except Exception as e:
            return {"status": "ERROR", "message": f"搜索失败: {str(e)}"}

    if action == "change_time":
        new_time = params.get("time", "")
        target_name = params.get("target_name", "")
        new_start = params.get("new_start", "")
        if not new_time and not new_start:
            return {"status": "ERROR", "message": "请提供新时间（如 14:00）"}

        time_to_set = new_time or new_start

        if target_name:
            # ── 修改指定活动的具体时间 ──
            for node in target_tl:
                if target_name in (node.get("memo", "") or ""):
                    node["time"] = time_to_set
                    # 按时间重排
                    target_tl.sort(key=lambda n: _time_str_to_minutes(n.get("time", "")) if n.get("time", "") else 9999)
                    session_state["days"] = days
                    return {"status": "SUCCESS",
                            "message": f"已将第{active_idx+1}天的「{target_name}」时间调整为 {time_to_set}",
                            "data": {"days": days}}
            return {"status": "ERROR", "message": f"在第{active_idx+1}天未找到「{target_name}」"}
        elif new_start:
            # ── 修改当天出发时间（调整第一个 WAKE_UP 节点）──
            for node in target_tl:
                if node.get("action") == "WAKE_UP":
                    node["time"] = new_start
                    break
            target_tl.sort(key=lambda n: _time_str_to_minutes(n.get("time", "")) if n.get("time", "") else 9999)
            session_state["days"] = days
            return {"status": "SUCCESS", "message": f"已将第{active_idx+1}天出发时间调整为 {new_start}",
                    "data": {"days": days}}
        else:
            # ── 修改当天整体出发时间（兼容旧行为）──
            session_state["fixed_time"] = time_to_set
            session_state["time_mode"] = "fixed"
            return {"status": "SUCCESS", "message": f"已将第{active_idx+1}天出发时间调整为 {time_to_set}",
                    "data": {"days": days}}

    if action == "change_transport":
        mode = params.get("mode", "WALK")
        transport_name = {"WALK": "步行", "TAXI": "打车", "METRO": "地铁", "BUS": "公交"}.get(mode, "步行")
        session_state["transport"] = transport_name
        return {"status": "SUCCESS", "message": f"已将第{active_idx+1}天交通方式切换为 {transport_name}",
                "data": {"days": days}}

    if action == "swap_current":
        target_name = params.get("target_name", "") or params.get("shop_name", "")
        target_cat = params.get("target_category", "")
        if not target_name and not target_cat:
            return {"status": "ERROR", "message": "请指定要替换的目的地名称或品类"}
        # 查找匹配的活动并替换
        removed = False
        old_memo = ""
        for i, n in enumerate(target_tl):
            memo = n.get("memo", "") or ""
            cat_n = n.get("category", "")
            if (target_name and target_name in memo) or (target_cat and target_cat == cat_n):
                old_memo = memo
                target_tl.pop(i)
                removed = True
                # 尝试搜索替换店铺（使用 _infer_center_coord 优先酒店坐标）
                try:
                    search_cat = [cat_n] if cat_n else ["restaurant"]
                    new_res = backend.skill_poi.search_poi_matrix(
                        center_coord=center, categories=search_cat,
                        radius_meters=5000, min_rating=4.0, keywords=""
                    )
                    for c, shops in new_res.get("search_results", {}).items():
                        if shops:
                            best = max(shops, key=lambda s: s.get("rating", 0))
                            new_node = {
                                "time": n.get("time", "12:00"),
                                "action": "VISIT",
                                "memo": best["name"],
                                "category": c,
                                "shop_id": str(best.get("shop_id", "")),
                                "lat": best.get("lat", ""),
                                "lng": best.get("lng", ""),
                                "duration_minutes": _duration(c) if c else 60,
                                "rating": best.get("rating", 0),
                            }
                            target_tl.insert(i, new_node)
                            break
                except Exception:
                    pass
                break
        if removed:
            session_state["days"] = days
            return {"status": "SUCCESS", "message": f"已替换「{old_memo}」",
                    "data": {"days": days}}
        return {"status": "ERROR", "message": f"在第{active_idx+1}天未找到匹配的活动"}

    if action == "replace_stop":
        target_name = params.get("target_name", "") or params.get("name", "")
        new_keywords = params.get("keywords", "")
        new_cat = params.get("category", "")
        if not target_name:
            return {"status": "ERROR", "message": "请指定要替换的目的地名称"}
        if not new_keywords:
            return {"status": "ERROR", "message": "请提供新的搜索关键词"}
        # 找到并移除旧节点
        removed_node = None
        remove_idx = -1
        for i, n in enumerate(target_tl):
            if target_name in (n.get("memo", "") or ""):
                removed_node = target_tl.pop(i)
                remove_idx = i
                break
        if removed_node is None:
            return {"status": "ERROR", "message": f"在第{active_idx+1}天未找到「{target_name}」"}
        old_cat = removed_node.get("category", "")
        # 同步 task_list / selected_pairs
        target_day["task_list"] = [t for t in target_day.get("task_list", [])
                                   if target_name not in (t.get("name", "") or "")]
        target_day["selected_pairs"] = [(c, s, n) for c, s, n in target_day.get("selected_pairs", [])
                                         if target_name not in n]
        # 搜索替代店铺
        try:
            search_cat = [new_cat] if new_cat else ([old_cat] if old_cat else ["restaurant"])
            new_res = backend.skill_poi.search_poi_matrix(
                center_coord=center, categories=search_cat,
                radius_meters=5000, min_rating=3.5, keywords=new_keywords
            )
            for c, shops in new_res.get("search_results", {}).items():
                if shops:
                    best = max(shops, key=lambda s: s.get("rating", 0))
                    new_node = {
                        "time": removed_node.get("time", "12:00"),
                        "action": "VISIT",
                        "memo": best["name"],
                        "category": c,
                        "shop_id": str(best.get("shop_id", "")),
                        "lat": best.get("lat", ""),
                        "lng": best.get("lng", ""),
                        "duration_minutes": _duration(c) if c else 60,
                        "rating": best.get("rating", 0),
                    }
                    target_tl.insert(remove_idx, new_node)
                    target_day.setdefault("task_list", []).append({
                        "name": best["name"], "category": c,
                        "shop_id": str(best.get("shop_id", "")),
                        "lat": best.get("lat", ""), "lng": best.get("lng", ""),
                        "duration_minutes": new_node["duration_minutes"],
                    })
                    target_day.setdefault("selected_pairs", []).append((c, str(best.get("shop_id", "")), best["name"]))
                    session_state["days"] = days
                    return {"status": "SUCCESS",
                            "message": f"已将第{active_idx+1}天的「{target_name}」替换为「{best['name']}」",
                            "data": {"days": days}}
        except Exception as e:
            pass
        # 搜索失败，保留移除状态
        session_state["days"] = days
        return {"status": "SUCCESS",
                "message": f"已移除「{target_name}」，但未找到「{new_keywords}」的替代商户",
                "data": {"days": days}}

    if action == "move_to_day":
        target_name = params.get("target_name", "")
        to_day = params.get("to_day")
        to_time = params.get("to_time", "")
        if not target_name:
            return {"status": "ERROR", "message": "请指定要移动的活动名称"}
        if to_day is None:
            return {"status": "ERROR", "message": "请指定目标天（to_day，从0开始）"}
        try:
            to_di = int(to_day)
            if to_di < 0 or to_di >= len(days):
                return {"status": "ERROR", "message": f"目标天索引 {to_di} 超出范围（共{len(days)}天）"}
        except (ValueError, TypeError):
            return {"status": "ERROR", "message": f"to_day 参数无效: {to_day}"}

        # ── 在所有天中查找匹配的活动 ──
        moved_node = None
        source_day_idx = -1
        for di, d in enumerate(days):
            tl = d.get("timeline", [])
            for node in list(tl):
                if target_name in (node.get("memo", "") or ""):
                    moved_node = dict(node)
                    tl.remove(node)
                    source_day_idx = di
                    break
            if moved_node:
                break

        if moved_node is None:
            return {"status": "ERROR", "message": f"在所有天的行程中未找到「{target_name}」"}

        # ── 同步源天的 task_list / selected_pairs ──
        if source_day_idx >= 0:
            src_day = days[source_day_idx]
            src_day["task_list"] = [t for t in src_day.get("task_list", [])
                                    if target_name not in (t.get("name", "") or "")]
            src_day["selected_pairs"] = [(c, s, n) for c, s, n in src_day.get("selected_pairs", [])
                                          if target_name not in n]

        # ── 插入到目标天 ──
        if to_time:
            moved_node["time"] = to_time
        target_day2 = days[to_di]
        target_tl2 = target_day2.setdefault("timeline", [])
        # 按时间排序插入
        raw_time = moved_node.get("time", "")
        node_min = _time_str_to_minutes(raw_time) if raw_time else 9999
        insert_idx = len(target_tl2)
        for i, n in enumerate(target_tl2):
            nt = n.get("time", "")
            n_min = _time_str_to_minutes(nt) if nt else 9999
            if node_min < n_min:
                insert_idx = i
                break
        target_tl2.insert(insert_idx, moved_node)
        # 同步目标天的 task_list / selected_pairs
        target_day2.setdefault("task_list", []).append({
            "name": moved_node.get("memo", ""),
            "category": moved_node.get("category", ""),
            "shop_id": str(moved_node.get("shop_id", "")),
            "lat": moved_node.get("lat", ""),
            "lng": moved_node.get("lng", ""),
            "duration_minutes": moved_node.get("duration_minutes", 60),
        })
        target_day2.setdefault("selected_pairs", []).append(
            (moved_node.get("category", ""),
             str(moved_node.get("shop_id", "")),
             moved_node.get("memo", ""))
        )

        session_state["days"] = days
        time_info = f" {to_time}" if to_time else ""
        return {"status": "SUCCESS",
                "message": f"已将「{target_name}」从第{source_day_idx+1}天移动到第{to_di+1}天{time_info}",
                "data": {"days": days}}

    # 其他 action 不支持多日模式
    return {"status": "ERROR",
            "message": f"多日模式下暂不支持「{action}」操作，请尝试在行程面板中手动调整"}


def _execute_chat_tool(tool_name: str, arguments: dict) -> dict:
    """执行聊天工具调用，返回结果字典"""
    global agent, session_state

    try:
        # ── POI搜索 ──
        if tool_name == "search_poi":
            # 复用按钮流程的 _search_poi()：快路径关键词 + 偏好注入 + LLM慢路径
            user_text = session_state.get("_last_user_message", arguments.get("keywords", ""))
            profile = _read_profile()
            try:
                result = _search_poi(agent, user_text, profile, center_coord=_infer_center_coord())
                if "error" in result:
                    return {"status": "ERROR", "message": result["error"]}
                categories = _build_categories_for_frontend(agent, profile)
                if not categories:
                    return {"status": "ERROR", "message": "未搜索到相关商户，换个关键词试试？"}
                # 同步 session_state（后续 confirmShopSelection / start_trip 需要）
                session_state["searched_categories"] = result.get("categories", [])
                session_state["_profile"] = profile
                session_state["phase"] = "choose_shop"
                total_shops = sum(len(c.get("shops", [])) for c in categories)
                return {
                    "status": "SUCCESS",
                    "type": "search_result",
                    "data": {"categories": categories},
                    "message": f"为你找到{len(categories)}个品类共{total_shops}家店铺，请在面板中选择～"
                }
            except Exception as e:
                return {"status": "ERROR", "message": f"POI搜索失败: {str(e)}"}

        # ── 路线规划 ──
        elif tool_name == "plan_route":
            waypoints = arguments.get("waypoints", [])
            transport = arguments.get("transport_preference", "步行优先")
            start = arguments.get("start_coord", DEFAULT_CENTER_COORD)
            if not waypoints:
                return {"status": "ERROR", "message": "请提供至少一个途经点"}
            result = _skill_route_planner(
                start_coord=start,
                waypoints=waypoints,
                transport_preference=transport
            )
            return {"status": "SUCCESS", "data": result,
                    "message": f"路线规划完成: {len(result.get('route',[]))}段, 总行程{result.get('total_travel_minutes',0)}分钟"}

        # ── 虚拟打车 ──
        elif tool_name == "hail_taxi":
            import random
            from_addr = arguments.get("from_address", "当前位置")
            to_addr = arguments.get("to_address", "目的地")
            car_type = arguments.get("car_type", "快车")
            wait_time = random.randint(2, 10)
            price_base = {"快车": 15, "优享": 25, "专车": 40}.get(car_type, 15)
            price = price_base + random.randint(5, 25)
            taxi_data = {
                "from": from_addr,
                "to": to_addr,
                "car_type": car_type,
                "estimated_wait_minutes": wait_time,
                "estimated_price": price,
                "driver_name": random.choice(["张师傅", "李师傅", "王师傅", "赵师傅"]),
                "car_plate": f"京B{random.randint(10000,99999)}",
                "car_model": random.choice(["丰田卡罗拉", "大众帕萨特", "比亚迪汉", "特斯拉Model3"]),
                "status": "searching"
            }
            return {"status": "SUCCESS", "data": {"taxi": taxi_data},
                    "message": f"已为你呼叫{car_type}，预计{wait_time}分钟后到达，预估¥{price}"}

        # ── 公交+地铁+步行组合规划 ──
        elif tool_name == "plan_transit":
            from_addr = arguments.get("from_address", "当前位置")
            to_addr = arguments.get("to_address", "目的地")
            prefer = arguments.get("prefer_mode", "最快")

            # 模拟智能组合规划
            result_data = {
                "from": from_addr,
                "to": to_addr,
                "prefer_mode": prefer,
                "route": [],
                "total_time_minutes": 0,
                "total_cost": 0,
                "total_walking_meters": 0
            }

            # 模拟: 距离决定组合方式
            # 近距离：步行为主
            # 中距离：步行+公交
            # 远距离：步行+地铁+步行
            import random
            scenario = random.randint(1, 3)

            if scenario == 1:
                # 步行+地铁组合
                result_data["route"] = [
                    {"step": 1, "mode": "🚶步行", "detail": f"步行{random.randint(200,600)}m至地铁站", "time_minutes": random.randint(3, 8), "cost": 0},
                    {"step": 2, "mode": "🚇地铁", "detail": f"乘坐地铁{random.randint(3,8)}站", "time_minutes": random.randint(5, 15), "cost": random.randint(3, 6)},
                    {"step": 3, "mode": "🚶步行", "detail": f"出站后步行{random.randint(100,400)}m到达", "time_minutes": random.randint(2, 5), "cost": 0},
                ]
            elif scenario == 2:
                # 步行+公交组合
                result_data["route"] = [
                    {"step": 1, "mode": "🚶步行", "detail": f"步行{random.randint(150,400)}m至公交站", "time_minutes": random.randint(2, 5), "cost": 0},
                    {"step": 2, "mode": "🚌公交", "detail": f"乘坐公交{random.randint(3,6)}站", "time_minutes": random.randint(8, 20), "cost": random.randint(1, 3)},
                    {"step": 3, "mode": "🚶步行", "detail": f"下车步行{random.randint(100,300)}m到达", "time_minutes": random.randint(2, 5), "cost": 0},
                ]
            else:
                # 纯步行
                result_data["route"] = [
                    {"step": 1, "mode": "🚶步行", "detail": f"全程步行约{random.randint(800,1500)}m", "time_minutes": random.randint(10, 20), "cost": 0},
                ]

            for r in result_data["route"]:
                result_data["total_time_minutes"] += r["time_minutes"]
                result_data["total_cost"] += r["cost"]
                result_data["total_walking_meters"] += int(r.get("detail", "0").replace("步行","").split("m")[0]) if "步行" in r.get("mode","") else 0

            return {"status": "SUCCESS", "data": result_data,
                    "message": f"从{from_addr}到{to_addr}推荐: {len(result_data['route'])}段, 约{result_data['total_time_minutes']}分钟, ¥{result_data['total_cost']}"}

        # ── 添加提醒 ──
        elif tool_name == "add_reminder":
            rtype = arguments.get("type", "CUSTOM")
            rtime = arguments.get("time", "12:00")
            label = arguments.get("label", "提醒")
            repeat = arguments.get("repeat", "once")
            note = arguments.get("note", "")
            task_id = f"{rtype.lower()}_{int(_time.time())}"
            task = {
                "task_id": task_id,
                "type": rtype,
                "time": rtime,
                "label": label,
                "repeat": repeat,
                "note": note,
                "status": "active"
            }
            # 加入全局提醒
            existing = session_state.get("_reminder_tasks", [])
            existing.append(task)
            session_state["_reminder_tasks"] = existing
            return {"status": "SUCCESS", "data": task,
                    "message": f"已添加提醒: {label} @ {rtime}"}

        # ── 删除提醒 ──
        elif tool_name == "remove_reminder":
            tid = arguments.get("task_id", "")
            existing = session_state.get("_reminder_tasks", [])
            before = len(existing)
            session_state["_reminder_tasks"] = [t for t in existing if t.get("task_id") != tid]
            if len(session_state["_reminder_tasks"]) < before:
                return {"status": "SUCCESS", "message": f"已删除提醒 {tid}"}
            return {"status": "ERROR", "message": f"未找到提醒 {tid}"}

        # ── 列出提醒 ──
        elif tool_name == "list_reminders":
            tasks = session_state.get("_reminder_tasks", [])
            return {"status": "SUCCESS", "data": {"tasks": tasks},
                    "message": f"当前共有{len(tasks)}个提醒" if tasks else "当前没有提醒"}

        # ── 天气 ──
        elif tool_name == "check_weather":
            loc = arguments.get("location", "北京")
            try:
                # 优先使用高德实时天气：先逆地理编码获取 adcode
                adcode = "110000"  # 默认北京
                try:
                    geo = _amap_client.geocode(loc)
                    if geo and geo.get("lng") and geo.get("lat"):
                        rev = _amap_client.reverse_geocode(lng=geo["lng"], lat=geo["lat"])
                        if rev and isinstance(rev, dict):
                            adcode = rev.get("adcode", "110000")
                except Exception:
                    pass
                wx = _amap_weather_client.get_real_time_weather(adcode=adcode)
            except Exception:
                # 兜底：mock 天气
                try:
                    wx = _skill_weather_extractor(DEFAULT_CENTER_COORD)
                except Exception:
                    wx = {"status": "ERROR", "message": "天气查询失败"}
            return {"status": "SUCCESS", "data": wx,
                    "message": f"{loc}当前天气: {wx.get('weather',{}).get('condition','?')}, {wx.get('weather',{}).get('temperature_c','?')}°C"}

        # ── 读偏好 ──
        elif tool_name == "read_profile":
            profile = _read_profile()
            return {"status": "SUCCESS", "data": profile,
                    "message": "已读取偏好设置"}

        # ── 更新偏好 ──
        elif tool_name == "update_profile":
            field = arguments.get("field", "")
            value = arguments.get("value", "")
            profile = _read_profile()
            try:
                # 简单字段更新
                keys = field.split(".")
                target = profile
                for k in keys[:-1]:
                    target = target.setdefault(k, {})
                target[keys[-1]] = value
                _write_profile(profile)
                return {"status": "SUCCESS", "message": f"已更新 {field} = {value}"}
            except Exception as e:
                return {"status": "ERROR", "message": f"偏好更新失败: {str(e)}"}

        # ── 排队 ──
        elif tool_name == "check_queue":
            name = arguments.get("restaurant_name", "")
            try:
                qr = _skill_queue_monitor("query", {"restaurant_name": name})
                return {"status": "SUCCESS", "data": qr,
                        "message": f"{name}排队状态: {qr.get('queue_length',0)}桌在等"}
            except Exception as e:
                return {"status": "ERROR", "message": f"排队查询失败: {str(e)}"}

        # ── 发起行程 ──
        elif tool_name == "start_trip":
            reqs = arguments.get("requirements", "")
            time_str = arguments.get("time", "now")
            transport = arguments.get("transport", "步行")

            # ⚠️ 保护：检查是否已有活跃行程
            existing_days = session_state.get("days", [])
            has_active_trip = bool(existing_days) or session_state.get("phase") == "done"
            if has_active_trip:
                day_count = len(existing_days) if existing_days else 1
                print(f"[start_trip] ⚠️ 已有{day_count}天行程，返回确认请求而非直接覆盖", flush=True)
                return {
                    "status": "CONFIRM_REQUIRED",
                    "message": f"你已有一个{day_count}天行程计划，确定要用新行程覆盖吗？回复「确定」或「是」来确认，回复「取消」来保留现有行程。",
                    "data": {"existing_days": day_count}
                }

            # 无活跃行程时才正常执行
            _reset_session()
            agent.context_memory = []
            session_state["user_input"] = reqs
            session_state["transport"] = transport
            profile = _read_profile()
            session_state["_profile"] = profile
            result = _search_poi(agent, reqs, profile, center_coord=_infer_center_coord())
            if "error" in result:
                return {"status": "ERROR", "message": result["error"]}
            # 构建前端品类选择面板数据（不再自动选店排程）
            categories_data = _build_categories_for_frontend(agent, profile)
            if not categories_data:
                return {"status": "ERROR", "message": "未找到匹配目的地"}
            _fast_cats = _try_fast_category_match(reqs)
            is_multi = len(_fast_cats) >= 2
            # 暂存到 session_state，供后续 confirmShopSelection 使用
            session_state["_pending_start_trip_categories"] = categories_data
            session_state["phase"] = "shop_selection"
            session_state["transport"] = transport
            return {
                "status": "SUCCESS",
                "phase": "multi_shop_selection" if is_multi else "need_shop_selection",
                "data": {"categories": categories_data},
                "message": "已为您找到以下店铺：" if is_multi else f"为您找到以下{backend.CATEGORY_NAME_CN.get(categories_data[0]['category'], '')}店铺：",
            }

        # ── 编辑行程 ──
        elif tool_name == "edit_trip":
            action = arguments.get("action", "reroute")
            params = arguments.get("params", {})

            # ── 多日模式：路由到多日编辑逻辑 ──
            if session_state.get("trip_mode") == "multi":
                return _handle_multi_day_chat_edit(action, params)

            if session_state.get("phase") != "done" and not session_state.get("selected_pairs"):
                return {"status": "ERROR", "message": "没有活跃行程"}

            # ── 验证 action 是否合法 ──
            _CHAT_ALLOWED_ACTIONS = {
                "add_stop", "remove_stop", "change_time", "change_transport",
                "reroute", "swap_current", "no_change", "clarify", "replace_stop",
                "add_reminder"
            }
            if action not in _CHAT_ALLOWED_ACTIONS:
                print(f"[chat-tool edit_trip] 拒绝无效 action: {action}")
                return {"status": "ERROR", "message": f"不支持的操作: {action}"}

            # 注入前端传的 swap_target_shop_id（用户点击了 clarify_swap_target 选择器）
            swap_target_shop_id = (arguments.get("swap_target_shop_id") or "").strip()
            if swap_target_shop_id and action == "swap_current":
                params["target_shop_id"] = swap_target_shop_id

            if action == "change_time":
                new_time = params.get("time", "")
                if new_time:
                    session_state["fixed_time"] = new_time
                    session_state["time_mode"] = "fixed"
                    return _run_schedule_from_session()
                return {"status": "ERROR", "message": "请提供新时间"}

            elif action == "change_transport":
                mode = params.get("mode", "WALK")
                session_state["transport"] = {"WALK": "步行", "TAXI": "打车", "METRO": "地铁", "BUS": "公交"}.get(mode, "步行")
                return _run_schedule_from_session()

            elif action == "add_stop":
                kw = params.get("keywords", "") or params.get("shop_name", "")
                cat = params.get("category")
                shop_name = params.get("shop_name", "")
                if not kw:
                    return {"status": "ERROR", "message": "请提供搜索关键词"}
                try:
                    new_res = backend.skill_poi.search_poi_matrix(
                        center_coord=DEFAULT_CENTER_COORD,
                        categories=[cat] if cat else ["restaurant"],
                        radius_meters=5000,
                        min_rating=3.5,
                        keywords=kw
                    )
                    for c, shops in new_res.get("search_results", {}).items():
                        if shops:
                            # 用户没指定具体店名 → 返回推荐面板
                            if not shop_name:
                                shop_list = []
                                for s in shops[:3]:
                                    s["coord"] = f"{s.get('lat','')},{s.get('lng','')}"
                                    agent.poi_cache[s["shop_id"]] = s
                                    shop_list.append(s)
                                return {"status": "SUCCESS", "phase": "need_shop_selection",
                                        "category": c,
                                        "label": backend.CATEGORY_NAME_CN.get(c, kw),
                                        "shops": shop_list,
                                        "message": f"为您找到以下{backend.CATEGORY_NAME_CN.get(c, '')}店铺："}
                            # 有具体店名 → 匹配并自动添加
                            best = max(shops, key=lambda s: s.get("rating", 0))
                            agent.poi_cache[best["shop_id"]] = best
                            pairs = list(session_state.get("selected_pairs", []))
                            pairs.append((c, best["shop_id"], best["name"]))
                            session_state["selected_pairs"] = pairs
                            result = _run_schedule_from_session()
                            return {"status": "SUCCESS", "data": (result.get_json() if hasattr(result, 'get_json') else result),
                                    "message": f"已添加 {best['name']}"}
                    return {"status": "ERROR", "message": f"未找到 '{kw}'"}
                except Exception as e:
                    return {"status": "ERROR", "message": f"搜索失败: {str(e)}"}

            elif action == "remove_stop":
                name = params.get("name", "").strip()
                if not name or len(name) < 2:
                    return {"status": "ERROR", "message": "请指定要删除的目的地名称"}
                old_pairs = session_state.get("selected_pairs", [])
                # 使用 _shop_name_matches 逐级匹配，防止短关键词误匹配
                new_pairs = [(c, sid, sn) for c, sid, sn in old_pairs if not _shop_name_matches(sn, name)]
                if len(new_pairs) == len(old_pairs):
                    return {"status": "ERROR", "message": f"未找到 '{name}'"}
                if not new_pairs:
                    return {"status": "ERROR", "message": "该操作会清空所有行程，已拒绝"}
                session_state["selected_pairs"] = new_pairs
                result = _run_schedule_from_session()
                return {"status": "SUCCESS", "data": (result.get_json() if hasattr(result, 'get_json') else result),
                        "message": f"已移除 {name}"}

            elif action == "replace_stop":
                remove_name = params.get("remove_name", "").strip()
                add_keywords = params.get("add_keywords", "")
                add_category = params.get("add_category", "")
                remove_category = params.get("remove_category", "")

                if not remove_name:
                    return {"status": "ERROR", "message": "请指定要替换的目的地"}
                old_pairs = session_state.get("selected_pairs", [])
                new_pairs = []
                removed_stop = None
                for cat, sid, sname in old_pairs:
                    if removed_stop is None and (
                        (remove_category and cat == remove_category) or
                        _shop_name_matches(sname, remove_name) or
                        (remove_name in _CAT_KEYWORDS and cat == _CAT_KEYWORDS[remove_name])
                    ):
                        removed_stop = (cat, sid, sname)
                        continue
                    new_pairs.append((cat, sid, sname))
                if removed_stop is None:
                    return {"status": "ERROR", "message": f"未找到可替换的目的地'{remove_name}'"}
                session_state["selected_pairs"] = new_pairs
                try:
                    new_res = backend.skill_poi.search_poi_matrix(
                        center_coord=DEFAULT_CENTER_COORD,
                        categories=[add_category] if add_category else ["restaurant"],
                        radius_meters=5000,
                        min_rating=3.5,
                        keywords=add_keywords if add_keywords else None,
                    )
                    for c, shops in new_res.get("search_results", {}).items():
                        if shops:
                            shop_list = []
                            for s in shops[:3]:
                                s["coord"] = f"{s.get('lat','')},{s.get('lng','')}"
                                agent.poi_cache[s["shop_id"]] = s
                                shop_list.append(s)
                            return {"status": "SUCCESS", "phase": "need_shop_selection",
                                    "category": c,
                                    "label": backend.CATEGORY_NAME_CN.get(c, add_keywords),
                                    "shops": shop_list,
                                    "message": f"已移除{backend.CATEGORY_NAME_CN.get(removed_stop[0], removed_stop[2])}，为您推荐{backend.CATEGORY_NAME_CN.get(c, '')}店铺："}
                    return {"status": "ERROR", "message": f"未找到'{add_keywords}'相关店铺"}
                except Exception as e:
                    session_state["selected_pairs"] = old_pairs
                    return {"status": "ERROR", "message": f"搜索新目的地失败: {str(e)}"}

            elif action == "no_change":
                return {"status": "SUCCESS", "message": "好的，行程保持不变 ✅"}

            elif action == "clarify":
                msg = params.get("message", "抱歉，我没太理解您的意思，能再说一遍吗？")
                return {"status": "SUCCESS", "message": msg, "phase": "clarify"}

            elif action == "swap_current":
                pairs = session_state.get("selected_pairs", [])
                if not pairs:
                    return {"status": "ERROR", "message": "没有可替换的目的地"}

                # 解析目标：按 shop_id > name > category 优先级
                target_name = params.get("target_name", "")
                target_category = params.get("target_category", "")
                target_shop_id = params.get("target_shop_id", "")
                matched, _ = _resolve_swap_target(pairs, target_name, target_category, target_shop_id)

                if not matched:
                    # 无法确定目标 → 返回目的地列表让用户选（CHAT_TOOLS 返回 clarify）
                    stop_list = []
                    for c, sid, sname in pairs:
                        stop_list.append({
                            "category": c,
                            "shop_id": sid,
                            "name": sname,
                            "label": backend.CATEGORY_NAME_CN.get(c, c)
                        })
                    return {"status": "SUCCESS", "phase": "clarify_swap_target",
                            "stops": stop_list,
                            "message": "您想换哪一个目的地？"}

                cat, sid, sname = matched
                try:
                    swap_res = backend.skill_poi.search_poi_matrix(
                        center_coord=DEFAULT_CENTER_COORD,
                        categories=[cat] if cat else ["restaurant"],
                        radius_meters=5000,
                        min_rating=3.5,
                        keywords=sname,
                    )
                    candidates = []
                    for c, shops in swap_res.get("search_results", {}).items():
                        for s in shops:
                            if s.get("shop_id") != sid:
                                s["coord"] = f"{s.get('lat','')},{s.get('lng','')}"
                                agent.poi_cache[s["shop_id"]] = s
                                candidates.append(s)
                    if not candidates:
                        return {"status": "ERROR", "message": f"附近未找到其他可替换店铺"}
                    return {"status": "SUCCESS", "phase": "swap_selection",
                            "current_shop": {"name": sname, "shop_id": sid},
                            "category": cat,
                            "label": backend.CATEGORY_NAME_CN.get(cat, cat),
                            "candidates": candidates[:5],
                            "message": f"为您找到以下可替换的{backend.CATEGORY_NAME_CN.get(cat, cat)}店铺："}
                except Exception as e:
                    return {"status": "ERROR", "message": f"搜索替换店铺失败: {str(e)}"}

            return {"status": "ERROR", "message": f"不支持的操作: {action}"}

        # ── 取消行程 ──
        elif tool_name == "cancel_trip":
            _reset_session()
            session_state["phase"] = "init"
            return {"status": "SUCCESS", "message": "行程已取消"}

        # ── 获取行程状态 ──
        elif tool_name == "get_trip_status":
            trip_mode = session_state.get("trip_mode", "single")
            # ── 多日模式：返回每日摘要 + 当前日时间线 ──
            if trip_mode == "multi":
                days = session_state.get("days", [])
                if not days:
                    return {"status": "SUCCESS", "data": {"active": False, "trip_mode": "multi"},
                            "message": "当前没有活跃的多日行程"}
                active_idx = session_state.get("active_day_index", 0)
                # 如果 LLM 传了 day_index，优先使用
                req_day = arguments.get("day_index")
                if req_day is not None:
                    active_idx = int(req_day)
                # 所有天的摘要
                all_days_summary = []
                for d in days:
                    tl = d.get("timeline", [])
                    if not tl:
                        sched = d.get("schedule_result", {})
                        tl = sched.get("timeline", []) if isinstance(sched, dict) else []
                    # 提取关键活动名称
                    key_activities = []
                    for node in tl[:8]:
                        memo = node.get("memo", "")
                        if memo:
                            key_activities.append(memo)
                    all_days_summary.append({
                        "day_index": d.get("day_index", 0),
                        "label": d.get("label", f"第{d.get('day_index', 0)+1}天"),
                        "activity_count": len(tl),
                        "key_activities": key_activities[:5],
                        "timeline_summary": [f"{n.get('time', '')} {n.get('memo', '')}" for n in tl[:8]],
                    })
                # 当前活跃日的详情
                current_day = None
                if 0 <= active_idx < len(days):
                    d = days[active_idx]
                    pairs = d.get("selected_pairs", [])
                    # 优先使用活 timeline（编辑后的），fallback 到 schedule_result.timeline
                    timeline = d.get("timeline", [])
                    if not timeline:
                        sched = d.get("schedule_result", {})
                        timeline = sched.get("timeline", []) if isinstance(sched, dict) else []
                    sched = d.get("schedule_result", {})
                    free_slots = sched.get("free_slots", []) if isinstance(sched, dict) else []
                    current_day = {
                        "day_index": d.get("day_index", active_idx),
                        "label": d.get("label", f"第{active_idx+1}天"),
                        "destinations": [{"category": c, "name": n, "shop_id": sid} for c, sid, n in pairs],
                        "timeline": timeline,
                        "free_slots": free_slots,
                    }
                elif req_day is not None:
                    return {"status": "SUCCESS", "data": {
                        "active": True, "trip_mode": "multi",
                        "total_days": len(days),
                        "message": f"第{req_day+1}天暂无安排"
                    }, "message": f"第{req_day+1}天暂无安排"}
                return {"status": "SUCCESS", "data": {
                    "active": True,
                    "trip_mode": "multi",
                    "total_days": len(days),
                    "current_day": current_day,
                    "all_days_summary": all_days_summary,
                    "transport": session_state.get("trip_transport", "步行优先"),
                }, "message": f"多日行程({len(days)}天)，当前第{active_idx+1}天"}
            # ── 单日模式：保持原有逻辑不变 ──
            pairs = session_state.get("selected_pairs", [])
            if not pairs:
                return {"status": "SUCCESS", "data": {"active": False, "trip_mode": "single"},
                        "message": "当前没有活跃行程"}
            dests = [{"category": c, "name": n, "shop_id": sid} for c, sid, n in pairs]
            return {"status": "SUCCESS", "data": {
                "active": True,
                "trip_mode": "single",
                "destinations": dests,
                "transport": session_state.get("transport", "步行"),
                "time": session_state.get("fixed_time", "现在"),
                "phase": session_state.get("phase", "init")
            }, "message": f"当前行程: {len(dests)}个目的地"}

        # ── 地理编码：地名→坐标 ──
        elif tool_name == "geocode":
            address = arguments.get("address", "").strip()
            if not address:
                return {"status": "ERROR", "message": "请提供要查询的地名或地址"}
            city = arguments.get("city", "北京")
            try:
                geo_result = _amap_client.geocode(address=address, city=city)
                if geo_result is None:
                    return {"status": "ERROR", "message": f"未找到地址'{address}'，请检查地名是否正确或尝试更具体的位置描述"}
                lng = geo_result.get("lng") or geo_result.get("longitude")
                lat = geo_result.get("lat") or geo_result.get("latitude")
                formatted_address = geo_result.get("address", address)
                session_state["_last_geocode_address"] = address
                return {
                    "status": "SUCCESS",
                    "data": {"lng": lng, "lat": lat, "address": formatted_address, "city": city},
                    "message": f"已定位: {formatted_address} (经度{lng}, 纬度{lat})"
                }
            except Exception as e:
                return {"status": "ERROR", "message": f"地理编码失败: {str(e)}"}

        # ── 周边搜索：指定坐标搜索商户 ──
        elif tool_name == "search_nearby":
            lng = arguments.get("lng")
            lat = arguments.get("lat")
            if lng is None or lat is None:
                return {"status": "ERROR", "message": "请提供搜索中心点的经纬度坐标(lng, lat)"}
            keywords = arguments.get("keywords", "")
            category = arguments.get("category", "")
            radius = int(arguments.get("radius", 3000))
            try:
                nearby_result = _amap_client.search_nearby(
                    lng=float(lng), lat=float(lat),
                    radius=radius, keywords=keywords, category=category
                )
                if nearby_result is None:
                    return {"status": "ERROR", "message": f"周边搜索失败，请尝试调整关键词或扩大搜索范围"}
                shops = nearby_result.get("shops") or nearby_result.get("pois") or nearby_result.get("results") or []
                if isinstance(nearby_result, list):
                    shops = nearby_result
                # 过滤掉与搜索地名相同的 POI（如搜"故宫"周边应排除"故宫博物院"）
                _searched_place = session_state.get("_last_geocode_address", "")
                if _searched_place and shops:
                    shops = [s for s in shops if _searched_place not in (s.get("name") or "") and (s.get("name") or "") not in _searched_place]
                total = len(shops) if isinstance(shops, list) else 0
                return {
                    "status": "SUCCESS",
                    "data": {"shops": shops[:15], "total": total, "center": {"lng": lng, "lat": lat}},
                    "message": f"在坐标({lng},{lat})周边找到{total}家商户" + (f"，关键词: {keywords}" if keywords else "")
                }
            except Exception as e:
                return {"status": "ERROR", "message": f"周边搜索失败: {str(e)}"}

        else:
            return {"status": "ERROR", "message": f"未知工具: {tool_name}"}

    except Exception as e:
        return {"status": "ERROR", "message": f"工具执行异常: {str(e)}"}


@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    """通用LLM聊天流式端点（SSE）"""
    global agent
    if agent is None:
        _reset_session()

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "请输入消息"}), 400

    # 存储用户原始消息，供 _execute_chat_tool 中复用 _search_poi() 的快路径匹配
    session_state["_last_user_message"] = message

    context = data.get("context", {})
    sid = data.get("session_id")

    # ── 构建系统提示 ──
    profile = _read_profile()

    # 用户身份（核心！用于自然称呼和个性化对话）
    personal = profile.get("personal", {})
    user_name = personal.get("elder_name", "").strip()
    user_display = f"「{user_name}」" if user_name else "用户（尚未告知姓名）"
    emergency_name = personal.get("emergency_contact_name", "").strip()
    emergency_phone = personal.get("emergency_contact_phone", "").strip()

    # 口味偏好
    taste = profile.get("taste", {})
    spicy = taste.get("taste_tolerance", "中等")
    diet = taste.get("dietary_restrictions", [])
    cuisine = taste.get("cuisine_preference", [])

    # 通勤偏好
    commute = profile.get("commute", {})
    walk_meters = commute.get("walking_tolerance_meters", 800)
    transport_pref = commute.get("transport_priority", "步行优先")

    # 预算偏好
    budget = profile.get("budget", {})
    price = budget.get("price_level", "中端")
    custom_budget = budget.get("custom_budget_per_person", "")
    rating_min = budget.get("rating_cutoff", 4.0)

    # 健康作息
    lifestyle = profile.get("lifestyle", {})
    hydration_min = lifestyle.get("hydration_interval_minutes", 90)
    meds = lifestyle.get("medication_schedule", [])

    # 组装成自然语言的「管家备忘录」
    profile_lines = []
    profile_lines.append(f"用户的名字是{user_display}，用这个名字自然称呼他即可，不要用笼统的「你」；但不要在每条回复开头都喊名字，只在关键确认时用。")
    if emergency_name:
        profile_lines.append(f"紧急联系人：{emergency_name}（{emergency_phone}）。")
    profile_lines.append(f"口味：辣度偏好「{spicy}」")
    if diet:
        profile_lines.append(f"饮食禁忌：{'、'.join(diet)}")
    if cuisine:
        profile_lines.append(f"偏好菜系：{'、'.join(cuisine)}")
    profile_lines.append(f"预算：{price}档次" + (f"，每餐约{custom_budget}元" if custom_budget else ""))
    profile_lines.append(f"评分底线：{rating_min}分以上")
    profile_lines.append(f"出行：{transport_pref}，步行容忍{walk_meters}米以内")
    profile_lines.append(f"作息：每{hydration_min}分钟提醒喝水" + (f"，用药：{'、'.join(m['name'] + '@' + m['time'] for m in meds)}" if meds else ""))
    profile_summary = "\n".join(profile_lines)

    # 上下文信息
    context_info = ""
    if context.get("active_trip"):
        dests = context.get("trip_destinations", [])
        ctx_transport = context.get("trip_transport", "步行")
        ctx_time = context.get("virtual_time", "未知")
        context_info = f"活跃行程: {', '.join(dests) if dests else '有'}, 交通: {ctx_transport}, 虚拟时间: {ctx_time}"
    else:
        context_info = "无活跃行程"

    # ── 多日行程上下文注入 ──
    multi_day_context = ""
    if session_state.get("trip_mode") == "multi":
        days = session_state.get("days", [])
        if days:
            dest = session_state.get("trip_destination", "北京")
            active_idx = session_state.get("active_day_index", 0)
            multi_day_context = (
                f"\n# 多日行程（重要！当前用户正在规划多日行程）\n"
                f"目的地: {dest}，共 {len(days)} 天，当前聚焦第{active_idx+1}天\n"
                f"用户可能通过自然语言修改任意天的行程——注意识别他说的「第X天」「明天」「后天」。\n"
            )
            for d in days:
                day_label = d.get("label", f"第{d.get('day_index', 0)+1}天")
                tl = d.get("timeline", [])
                items = []
                for node in tl[:10]:
                    t = node.get("time", "")
                    memo = node.get("memo", "")
                    items.append(f"  {t} {memo}")
                if items:
                    multi_day_context += f"{day_label}:\n" + "\n".join(items) + "\n"
                else:
                    multi_day_context += f"{day_label}:（暂无安排）\n"
            context_info = f"多日行程({len(days)}天，目的地{dest})"

    system_prompt = (
        "# 🧠 工作方式：分解思维\n\n"
        "你不是关键词匹配机器人。面对用户的每条消息，请你按以下步骤思考：\n\n"
        "**第1步：理解问题结构**\n"
        "用户想做什么？消息里包含哪些关键维度？\n"
        "- 时间维度：有没有提到具体日期/时段（如「6号上午」「下午」「今天」）？\n"
        "- 地点维度：有没有提到具体地名/位置（如「故宫附近」「东来顺饭店旁边」）？\n"
        "- 活动维度：想找什么类型的店铺或活动（吃喝/玩乐/出行）？\n"
        "- 行程维度：是否涉及已有行程的查询或修改？\n\n"
        "**第2步：确定工具和调用顺序**\n"
        "根据问题结构，列出需要的工具，并确定正确的调用顺序。\n"
        "- 如果涉及具体地名 + 搜索商户 → 先 geocode(地名) 获取坐标，再 search_nearby(坐标,关键词)\n"
        "- 如果涉及日期 + 行程 → 先 get_trip_status 查看当天安排\n"
        "- 如果是要找店铺但没提具体位置 → 用 search_poi 在你已知的默认位置搜索\n"
        "- 简单信息查询（天气/排队）→ 直接调对应工具\n\n"
        "**第3步：逐步执行**\n"
        "一次调用一个工具，根据返回结果决定下一步。不要试图一次调用完成所有事情。\n\n"
        "# 📍 位置感知搜索（重要！）\n\n"
        "当用户提到具体地名时（如「XX附近」「XX旁边」「XX周边」），你必须：\n"
        "1. 先调用 geocode(address=\"用户提到的地名\") 获取经纬度\n"
        "2. 再用 search_nearby(lng=坐标.lng, lat=坐标.lat, keywords=\"用户想找的内容\") 搜索周边\n"
        "**严禁**在用户明确提到地名时直接用 search_poi 搜默认位置！那样会搜错地方！\n"
        "如果 geocode 返回失败（地名不存在），告诉用户你无法识别该位置，请他确认地名。\n"
        "📍 搜索结果过滤\n"
        "当搜索「XX附近/周边」时，如果搜索结果中包含 XX 自身（如搜「故宫」结果里有「故宫博物院」），\n"
        "必须在总结时排除——用户问的是「附近」，不是问这个地方本身有什么。\n"
        "只推荐与搜索地名不同的周边商户/景点。\n\n"
        "# ⚠️ 核心铁律\n\n"
        "你有工具可以搜索真实商户数据。当用户想找/吃/喝/玩/去某类店铺时，**必须调用搜索工具**。\n"
        "**绝对禁止**凭你的训练数据直接推荐具体店铺名称——你记忆中的店可能已关门、评分不准、距离未知。\n"
        "**绝对禁止**用文字回复代替工具调用。用户说「想吃火锅」→ 调 search_poi(keywords=\"火锅\")，而不是直接说「xx火锅店不错」。\n"
        "同样的，用户表达过敏/忌口/偏好 → 调 update_profile；用户要提醒/路线/叫车/天气 → 调对应工具。\n"
        "**绝对禁止**在用户只是问问题时修改行程——「附近有啥」≠「加到行程」。\n"
        "如果用户的问题存在歧义（地名不明确、时间模糊），主动询问澄清，不要猜测。\n\n"
        "# 身份定义\n\n"
        f"你是「小美」，{user_display}的私人生活助理。你通过调用后台工具来帮他找到真实可靠的本地生活信息。\n"
        "你的温暖体现在语气上，你的可靠体现在「只推荐工具搜出来的真实数据」上。\n\n"
        f"# {user_display}的专属档案（搜索时会自动应用，你不用手动处理）\n\n"
        f"{profile_summary}\n\n"
        f"系统状态：{context_info}\n"
        f"默认搜索位置坐标：{DEFAULT_CENTER_COORD}（当用户没有指定具体位置时使用此坐标搜索）\n\n"
        "# 工具速查\n\n"
        "搜索店铺（无具体位置）→ search_poi\n"
        "地名→坐标 → geocode\n"
        "指定坐标周边搜索 → search_nearby（需先 geocode）\n"
        "出行规划 → start_trip / plan_route / hail_taxi / plan_transit\n"
        "行程管理 → edit_trip / cancel_trip / get_trip_status\n"
        "多日行程专用:\n"
        "  - 添加活动: edit_trip(action=\"add_stop\", params={keywords:\"...\", day_index:N, time:\"HH:MM\"})\n"
        "  - 删除活动: edit_trip(action=\"remove_stop\", params={name:\"...\", day_index:N})\n"
        "  - 跨天移动: edit_trip(action=\"move_to_day\", params={target_name:\"...\", to_day:N, to_time:\"HH:MM\"})\n"
        "  - 调整时间: edit_trip(action=\"change_time\", params={target_name:\"...\", time:\"HH:MM\", day_index:N})\n"
        "  - day_index 从 0 开始（0=第1天），修改前先口头确认再调用工具\n"
        "提醒管理 → add_reminder / remove_reminder / list_reminders\n"
        "信息查询 → check_weather / check_queue / read_profile\n"
        "偏好更新 → update_profile\n"
        "纯问候/闲聊（仅限「你好」「谢谢」「晚安」等寒暄，无任何实质需求）→ 不调工具，温暖回应 1-3 句\n"
        "⚠️ 涉及地名+搜索意图（如「XX附近有什么」「帮我找XX周边的店」）不是闲聊！必须先调 geocode→search_nearby 实际搜索！\n\n"
        "# 语气规则\n\n"
        f"- 自然称呼{user_display}，不用「您」\n"
        "- 适量用「呀」「嘛」「哈」「～」和 emoji（1-3个/条），不要刷屏\n"
        "- 当搜索结果以面板展示时，只需简短一句（≤15字）引导用户查看面板即可，如「帮你找到了，在下面挑挑看～」。严禁在文字中逐一列出店铺、排时间、生成行程预览——这些信息都在面板里\n"
        "- 不要说自己是「AI」「助手」「机器人」「管家」——你是「小美」\n"
        "- 当需要用户确认操作时，用温暖但清晰的方式说明将要做什么，等用户确认后再执行"
        f"# 多日行程专用操作\n\n"
        f"- 添加活动 → edit_trip(action=\"add_stop\", params={{keywords: \"...\", day_index: N, time: \"HH:MM\"}})\n"
        f"- 删除活动 → edit_trip(action=\"remove_stop\", params={{name: \"...\", day_index: N}})\n"
        f"- 移动活动跨天 → edit_trip(action=\"move_to_day\", params={{target_name: \"...\", to_day: N, to_time: \"HH:MM\"}})\n"
        f"- 调整某活动时间 → edit_trip(action=\"change_time\", params={{target_name: \"...\", time: \"HH:MM\", day_index: N}})\n"
        f"- 替换活动 → edit_trip(action=\"replace_stop\", params={{target_name: \"...\", keywords: \"...\", day_index: N}})\n"
        f"- 修改型操作前先口头确认，用户同意后再调用 edit_trip\n"
        f"- day_index 从0开始（0=第1天）；用户说「第2天」→ day_index=1\n\n"
    )

    # ── 将多日上下文追加到 system prompt ──
    if multi_day_context:
        system_prompt += "\n" + multi_day_context

    # ── 加载或创建会话 ──
    history_db, chat_session = _get_active_session()
    if sid and sid in history_db["sessions"]:
        # 使用指定会话
        session_messages = history_db["sessions"][sid]["messages"]
        history_db["active_session"] = sid
        _save_chat_history(history_db)
    else:
        session_messages = chat_session["messages"]

    # ── 构建消息列表 ──
    messages = [{"role": "system", "content": system_prompt}]
    # 添加历史消息（最近40条，防止过长）
    for msg in session_messages[-40:]:
        m = {"role": msg["role"]}
        if "content" in msg and msg["content"] is not None:
            m["content"] = msg["content"]
        if "tool_calls" in msg:
            m["tool_calls"] = msg["tool_calls"]
        if "tool_call_id" in msg:
            m["tool_call_id"] = msg["tool_call_id"]
        if "name" in msg:
            m["name"] = msg["name"]
        if "reasoning_content" in msg and msg["reasoning_content"] is not None:
            m["reasoning_content"] = msg["reasoning_content"]
        messages.append(m)
    # 添加当前用户消息
    messages.append({"role": "user", "content": message})

    # 保存用户消息
    _append_chat_message("user", content=message)

    def generate():
        nonlocal messages
        chat_session_id = history_db.get("active_session", "chat_000")
        try:
            # 发送用户消息事件
            yield f"event: message\ndata: {json.dumps({'role': 'user', 'content': message}, ensure_ascii=False)}\n\n"

            # ═══════════════════════════════════════════════════════════════
            # 预拦截：检测位置搜索意图，直接调高德 API（绕过 LLM function calling）
            # ═══════════════════════════════════════════════════════════════
            _location_intent = _detect_location_search(message)
            if _location_intent:
                place = _location_intent["place"]
                keywords = _location_intent["keywords"]
                display_keyword = _location_intent.get("display_keyword", keywords)
                print(f"[LocationIntercept] 检测到位置搜索意图: place={place}, keywords={keywords}", flush=True)

                # 1. 发射"正在搜索"文本（用友好的显示名）
                yield f"event: message\ndata: {json.dumps({'role': 'assistant', 'content': f'帮你搜搜{place}周边的{display_keyword}～'}, ensure_ascii=False)}\n\n"

                # 2. geocode
                geo_call_id = f"call_direct_geo_{int(_time.time())}"
                yield f"event: tool_call\ndata: {json.dumps({'id': geo_call_id, 'name': 'geocode', 'arguments': {'address': place}, 'status': 'started'}, ensure_ascii=False)}\n\n"

                geo_result = _amap_client.geocode(address=place, city="北京")
                if geo_result:
                    yield f"event: tool_result\ndata: {json.dumps({'id': geo_call_id, 'name': 'geocode', 'status': 'completed', 'result': {'status': 'SUCCESS', 'data': geo_result, 'message': f"已定位: {geo_result.get('formatted_address', place)}"}}, ensure_ascii=False)}\n\n"

                    # 3. search_nearby
                    sn_call_id = f"call_direct_sn_{int(_time.time())}"
                    yield f"event: tool_call\ndata: {json.dumps({'id': sn_call_id, 'name': 'search_nearby', 'arguments': {'lng': geo_result['lng'], 'lat': geo_result['lat'], 'keywords': keywords}, 'status': 'started'}, ensure_ascii=False)}\n\n"

                    search_result = _amap_client.search_nearby(
                        lng=geo_result["lng"], lat=geo_result["lat"],
                        radius=3000, keywords=keywords
                    )
                    shops = search_result.get("shops", [])[:15]
                    # 过滤掉与搜索地名相同的 POI（如搜"故宫"周边应排除"故宫博物院"等内部POI）
                    _filtered_shops = []
                    for s in shops:
                        s_name = s.get("name", "")
                        if place in s_name or s_name in place:
                            continue
                        _filtered_shops.append(s)
                    if _filtered_shops:
                        shops = _filtered_shops
                    total = len(shops)
                    sn_result = {
                        "status": "SUCCESS",
                        "data": {
                            "shops": shops, "total": total,
                            "center": {"lng": geo_result["lng"], "lat": geo_result["lat"]}
                        },
                        "message": f"在{place}周边找到{total}家商户"
                    }
                    yield f"event: tool_result\ndata: {json.dumps({'id': sn_call_id, 'name': 'search_nearby', 'status': 'completed', 'result': sn_result}, ensure_ascii=False)}\n\n"

                    # 4. 保存搜索结果到会话历史（仅文本，不保存 tool 消息避免后续 LLM 调用混乱）
                    _append_chat_message("assistant", content=f'帮你搜搜{place}周边的{display_keyword}～')

                    # 5. 将搜索结果注入为系统消息，让 LLM 直接总结（不模拟 tool_calls，避免 v4-pro reasoning_content 兼容问题）
                    shop_names = [s.get("name", "?") for s in shops[:5]]
                    shop_list_str = "、".join(shop_names) if shop_names else "无结果"
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[系统已自动搜索] 用户想知道「{place}」附近有什么。"
                            f"已在{place}周边（坐标 {geo_result['lng']},{geo_result['lat']}）"
                            f"搜索，共找到 {total} 家商户。"
                            f"排名靠前的有：{shop_list_str}等。"
                            f"你现在是纯文本模式，请按以下格式用温暖自然的语气回复（1-3句话即可）："
                            f"1. 先确认搜索动作（如「好的，帮你搜搜{place}周边的好去处～」），自然地称呼用户"
                            f"2. 然后挑1-2家评分高的简单提一下"
                            f"3. 最后引导用户查看面板"
                            f"⚠️ 你现在没有工具可用。只输出普通文本，禁止输出任何工具调用语法"
                            f"（如 geocode()、<invoke>、<parameter>、function_call 等）。"
                            f"绝对不要逐条列出所有结果。"
                        )
                    })

                    # 6. 调用 LLM 做自然语言总结（不带工具，纯文本回复）
                    assistant_full_response = ""
                    generator2 = agent.chat_stream(messages, tools=None, max_tool_rounds=1)
                    for event_dict in generator2:
                        evt = event_dict["event"]
                        payload = event_dict["data"]
                        if evt == "message":
                            assistant_full_response += payload["content"]
                            yield f"event: message\ndata: {json.dumps({'role': 'assistant', 'content': payload['content']}, ensure_ascii=False)}\n\n"
                        elif evt == "done":
                            _sanitized = _sanitize_llm_text(assistant_full_response)
                            if _sanitized:
                                _append_chat_message("assistant", content=_sanitized)
                        elif evt == "error":
                            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

                    yield f"event: done\ndata: {json.dumps({'session_id': chat_session_id, 'status': 'complete'}, ensure_ascii=False)}\n\n"
                    return  # 拦截完成，不走原来的 LLM 路径
                else:
                    # geocode 失败 → 提示后结束
                    print(f"[LocationIntercept] geocode 失败: {place}", flush=True)
                    yield f"event: message\ndata: {json.dumps({'role': 'assistant', 'content': f'抱歉，没找到「{place}」的位置，换个说法试试～'}, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'session_id': chat_session_id, 'status': 'complete'}, ensure_ascii=False)}\n\n"
                    return

            # ═══════════════════════════════════════════════════════════════
            # LLM 路径：所有消息统一由 LLM 决定是聊天还是调工具
            # ═══════════════════════════════════════════════════════════════

            # 第一轮：调用LLM流式
            generator = agent.chat_stream(messages, tools=CHAT_TOOLS, max_tool_rounds=5)

            current_tool_calls = None
            assistant_full_response = ""  # 累积最终assistant回复

            for event_dict in generator:
                evt = event_dict["event"]
                payload = event_dict["data"]

                if evt == "message":
                    # 流式文本块 — 累积到最终回复
                    assistant_full_response += payload["content"]
                    yield f"event: message\ndata: {json.dumps({'role': 'assistant', 'content': payload['content']}, ensure_ascii=False)}\n\n"

                elif evt == "tool_call":
                    # 工具调用开始
                    yield f"event: tool_call\ndata: {json.dumps({'id': payload['id'], 'name': payload['name'], 'arguments': payload['arguments'], 'status': 'started'}, ensure_ascii=False)}\n\n"

                elif evt == "tool_calls_complete":
                    # 工具调用完成，执行工具
                    current_tool_calls = payload["tool_calls"]
                    _reasoning = payload.get("reasoning_content")

                    # ★ 保存assistant的tool_calls消息到历史和messages（必须在tool结果之前）
                    _tool_calls_for_history = []
                    for tc in current_tool_calls:
                        _tool_calls_for_history.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}
                        })
                    _append_chat_message("assistant", content=None, tool_calls=_tool_calls_for_history, reasoning_content=_reasoning)
                    # ⚠️ 关键：也必须加到 messages 列表中，否则后续 API 调用会报错
                    # "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'"
                    _assistant_msg = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": _tool_calls_for_history
                    }
                    if _reasoning:
                        _assistant_msg["reasoning_content"] = _reasoning
                    messages.append(_assistant_msg)

                    for tc in current_tool_calls:
                        tool_name = tc["name"]
                        tool_args = tc["arguments"]

                        # 执行工具
                        result = _execute_chat_tool(tool_name, tool_args)

                        # 发送工具结果
                        result_status = result.get('status', '')
                        if result_status == 'SUCCESS':
                            sse_status = 'completed'
                        elif result_status == 'CONFIRM_REQUIRED':
                            sse_status = 'confirm_required'
                        else:
                            sse_status = 'failed'
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc['id'], 'name': tool_name, 'status': sse_status, 'result': result}, ensure_ascii=False)}\n\n"

                        # 将工具结果加入消息
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(result, ensure_ascii=False)
                        })

                        # 保存到历史
                        _append_chat_message("tool", tool_call_id=tc["id"], name=tool_name, content=json.dumps(result, ensure_ascii=False))

                    # 继续流式生成（工具结果后的自然语言回复）
                    assistant_full_response = ""  # 重置以收集tool后的回复
                    generator2 = agent.chat_stream_continue(messages, tools=CHAT_TOOLS, max_tool_rounds=5)
                    for event_dict2 in generator2:
                        evt2 = event_dict2["event"]
                        payload2 = event_dict2["data"]
                        if evt2 == "message":
                            assistant_full_response += payload2["content"]
                            yield f"event: message\ndata: {json.dumps({'role': 'assistant', 'content': payload2['content']}, ensure_ascii=False)}\n\n"
                        elif evt2 == "tool_call":
                            # 嵌套工具调用（第二轮）
                            yield f"event: tool_call\ndata: {json.dumps({'id': payload2['id'], 'name': payload2['name'], 'arguments': payload2['arguments'], 'status': 'started'}, ensure_ascii=False)}\n\n"
                            # ★ 保存assistant的tool_calls到历史和messages
                            _tc_msg = [{
                                "id": payload2["id"],
                                "type": "function",
                                "function": {"name": payload2["name"], "arguments": json.dumps(payload2["arguments"], ensure_ascii=False)}
                            }]
                            _append_chat_message("assistant", content=None, tool_calls=_tc_msg)
                            messages.append({"role": "assistant", "content": None, "tool_calls": _tc_msg})
                            # 执行并返回
                            tc_result = _execute_chat_tool(payload2["name"], payload2["arguments"])
                            tc_result_status = tc_result.get('status', '')
                            if tc_result_status == 'SUCCESS':
                                tc_sse_status = 'completed'
                            elif tc_result_status == 'CONFIRM_REQUIRED':
                                tc_sse_status = 'confirm_required'
                            else:
                                tc_sse_status = 'failed'
                            yield f"event: tool_result\ndata: {json.dumps({'id': payload2['id'], 'name': payload2['name'], 'status': tc_sse_status, 'result': tc_result}, ensure_ascii=False)}\n\n"
                            messages.append({
                                "role": "tool",
                                "tool_call_id": payload2["id"],
                                "content": json.dumps(tc_result, ensure_ascii=False)
                            })
                            _append_chat_message("tool", tool_call_id=payload2["id"], name=payload2["name"], content=json.dumps(tc_result, ensure_ascii=False))
                            # 再继续
                            generator3 = agent.chat_stream_continue(messages, tools=CHAT_TOOLS, max_tool_rounds=3)
                            for event_dict3 in generator3:
                                evt3 = event_dict3["event"]
                                payload3 = event_dict3["data"]
                                if evt3 == "message":
                                    assistant_full_response += payload3["content"]
                                    yield f"event: message\ndata: {json.dumps({'role': 'assistant', 'content': payload3['content']}, ensure_ascii=False)}\n\n"
                                elif evt3 == "done":
                                    # 保存最终的assistant回复（三层tool调用后）
                                    if assistant_full_response.strip():
                                        _sanitized = _sanitize_llm_text(assistant_full_response)
                                        if _sanitized:
                                            _append_chat_message("assistant", content=_sanitized)
                                elif evt3 == "error":
                                    yield f"event: error\ndata: {json.dumps(payload3, ensure_ascii=False)}\n\n"
                        elif evt2 == "done":
                            # 保存最终的assistant回复（两层tool调用后）
                            if assistant_full_response.strip():
                                _sanitized = _sanitize_llm_text(assistant_full_response)
                                if _sanitized:
                                    _append_chat_message("assistant", content=_sanitized)
                        elif evt2 == "error":
                            yield f"event: error\ndata: {json.dumps(payload2, ensure_ascii=False)}\n\n"

                elif evt == "error":
                    yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

                elif evt == "done":
                    # 保存最终assistant回复（如果没有工具调用）
                    if assistant_full_response.strip() and not current_tool_calls:
                        _sanitized = _sanitize_llm_text(assistant_full_response)
                        if _sanitized:
                            _append_chat_message("assistant", content=_sanitized)

            # 发送完成事件
            yield f"event: done\ndata: {json.dumps({'session_id': chat_session_id, 'status': 'complete'}, ensure_ascii=False)}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'message': f'服务器错误: {str(e)}'}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/chat/history", methods=["GET"])
def chat_history():
    """获取当前会话的聊天历史"""
    history_db = _load_chat_history()
    sid = history_db.get("active_session")
    if not sid or sid not in history_db["sessions"]:
        return jsonify({"session_id": None, "messages": []})
    session = history_db["sessions"][sid]
    return jsonify({
        "session_id": sid,
        "messages": [
            {"role": m.get("role"), "content": m.get("content")}
            for m in session.get("messages", [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ],
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
    })


@app.route("/api/chat/clear", methods=["POST"])
def chat_clear():
    """清除聊天历史，开启新会话"""
    history_db = _load_chat_history()
    now = datetime.now()
    new_sid = f"chat_{now.strftime('%Y%m%d_%H%M%S')}"
    history_db["active_session"] = new_sid
    history_db["sessions"][new_sid] = {
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "messages": []
    }
    _save_chat_history(history_db)
    return jsonify({"status": "SUCCESS", "new_session_id": new_sid})


@app.route("/api/chat/sessions", methods=["GET"])
def list_chat_sessions():
    """列出所有会话摘要（按时间倒序）"""
    history_db = _load_chat_history()
    sessions_list = []
    for sid, sess in history_db.get("sessions", {}).items():
        msgs = sess.get("messages", [])
        # 取第一条用户消息作为摘要
        summary = ""
        for m in msgs:
            if m.get("role") == "user" and m.get("content"):
                summary = m["content"][:50]
                break
        msg_count = sum(1 for m in msgs if m.get("role") in ("user", "assistant") and m.get("content"))
        sessions_list.append({
            "session_id": sid,
            "created_at": sess.get("created_at"),
            "updated_at": sess.get("updated_at"),
            "message_count": msg_count,
            "summary": summary or "(空对话)"
        })
    # 按创建时间倒序
    sessions_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify({
        "sessions": sessions_list,
        "active_session": history_db.get("active_session")
    })


@app.route("/api/chat/session/<session_id>", methods=["GET"])
def get_session_detail(session_id):
    """获取指定会话的完整消息"""
    history_db = _load_chat_history()
    if session_id not in history_db.get("sessions", {}):
        return jsonify({"error": "Session not found"}), 404
    session = history_db["sessions"][session_id]
    return jsonify({
        "session_id": session_id,
        "messages": [
            {"role": m.get("role"), "content": m.get("content")}
            for m in session.get("messages", [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ],
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
    })


# ======================================================================
# 多日行程 API
# ======================================================================

# 热门城市坐标映射
_CITY_COORDS = {
    "北京": (39.93, 116.45), "上海": (31.23, 121.47), "广州": (23.13, 113.26),
    "深圳": (22.54, 114.06), "成都": (30.57, 104.07), "杭州": (30.25, 120.16),
    "西安": (34.26, 108.94), "重庆": (29.56, 106.55), "南京": (32.06, 118.79),
    "武汉": (30.59, 114.30), "长沙": (28.23, 112.94), "厦门": (24.48, 118.09),
    "三亚": (18.25, 109.51), "丽江": (26.87, 100.23), "大理": (25.61, 100.27),
    "桂林": (25.27, 110.29), "苏州": (31.30, 120.63), "青岛": (36.07, 120.38),
    "大连": (38.91, 121.61), "昆明": (25.04, 102.71),
}

# 城市名 -> 高德 adcode（用于天气 API）
_CITY_ADCODE = {
    "北京": "110000", "上海": "310000", "广州": "440100", "深圳": "440300",
    "成都": "510100", "杭州": "330100", "西安": "610100", "重庆": "500000",
    "南京": "320100", "武汉": "420100", "长沙": "430100", "厦门": "350200",
    "三亚": "460200", "丽江": "530700", "大理": "532900", "桂林": "450300",
    "苏州": "320500", "青岛": "370200", "大连": "210200", "昆明": "530100",
}

# 加载预缓存
import json as _json
_top20_cache = {}
_top20_cache_path = os.path.join(base_dir, "skills", "multi_day_scheduler", "top20_cache.json")
try:
    with open(_top20_cache_path, "r", encoding="utf-8") as _f:
        _top20_cache = _json.loads(_f.read())
except Exception:
    print(f"[多日行程] 未能加载 top20_cache.json，将全量使用API实时搜索", flush=True)

# ── Shop 估算缓存（按 shop_id 缓存 LLM 耗时/体力/时间段估算结果）──
_shop_estimates_cache = {}
_shop_estimates_cache_path = os.path.join(base_dir, "cache", "shop_estimates_cache.json")
try:
    with open(_shop_estimates_cache_path, "r", encoding="utf-8") as _f:
        _shop_estimates_cache = _json.loads(_f.read())
    print(f"[Shop估算缓存] 已加载 {len(_shop_estimates_cache)} 个店铺的估算数据", flush=True)
except Exception:
    print(f"[Shop估算缓存] 未找到缓存文件，将全量使用LLM估算", flush=True)


def _save_shop_estimates_cache():
    """持久化 shop 估算缓存到磁盘"""
    try:
        os.makedirs(os.path.dirname(_shop_estimates_cache_path), exist_ok=True)
        with open(_shop_estimates_cache_path, "w", encoding="utf-8") as _f:
            _f.write(_json.dumps(_shop_estimates_cache, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[Shop估算缓存] 写入失败: {e}", flush=True)


# ── 排程结果缓存（按输入 hash 缓存完整排程结果，秒级返回）──
_schedule_result_cache = {}
_schedule_result_cache_path = os.path.join(base_dir, "cache", "schedule_result_cache.json")
try:
    with open(_schedule_result_cache_path, "r", encoding="utf-8") as _f:
        _schedule_result_cache = _json.loads(_f.read())
    print(f"[排程缓存] 已加载 {len(_schedule_result_cache)} 个排程结果", flush=True)
except Exception:
    print(f"[排程缓存] 未找到缓存文件，将全量实时排程", flush=True)


def _save_schedule_result_cache():
    """持久化排程结果缓存到磁盘"""
    try:
        os.makedirs(os.path.dirname(_schedule_result_cache_path), exist_ok=True)
        with open(_schedule_result_cache_path, "w", encoding="utf-8") as _f:
            _f.write(_json.dumps(_schedule_result_cache, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[排程缓存] 写入失败: {e}", flush=True)


def _compute_schedule_cache_key(candidate_pool, trip_days, transport, start_time, checkin_lat, checkin_lng):
    """根据排程输入计算缓存 key。只依赖 shop_id 列表（排序后）和核心参数。"""
    shop_ids = sorted([s.get("shop_id", "") for s in candidate_pool if s.get("shop_id")])
    key_parts = [
        ",".join(shop_ids),
        str(trip_days),
        str(transport),
        str(start_time),
        str(round(float(checkin_lat or 0), 4)),
        str(round(float(checkin_lng or 0), 4)),
    ]
    import hashlib
    raw = "|".join(key_parts)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _replace_nan_in_result(obj):
    """递归替换结果中的 NaN/Infinity 为 None（JSON 不支持 NaN）。"""
    import math
    if isinstance(obj, dict):
        return {k: _replace_nan_in_result(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_replace_nan_in_result(v) for v in obj]
    elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _get_top20_attractions(city: str, ref_lat: float = None, ref_lng: float = None) -> dict:
    """获取城市 Top20 热门景点/餐厅。先查预缓存，miss 则调高德 API 实时搜索。"""
    # 城市名模糊匹配缓存
    cached = None
    for cached_city, shops in _top20_cache.items():
        if cached_city == city or city in cached_city or cached_city in city:
            cached = shops
            break

    if cached:
        # 用缓存数据
        if ref_lat is None:
            coords = _CITY_COORDS.get(city, (39.93, 116.45))
            ref_lat, ref_lng = coords
        formatted = []
        for s in cached:
            s_lat = float(s.get("lat", ref_lat))
            s_lng = float(s.get("lng", ref_lng))
            formatted.append({
                "shop_id": s.get("shop_id", ""),
                "name": s.get("name", ""),
                "category": s.get("category", ""),
                "category_cn": backend.CATEGORY_NAME_CN.get(s.get("category", ""), s.get("category", "")),
                "rating": s.get("rating", 0),
                "coord": f"{s_lat},{s_lng}",
                "lat": s_lat,
                "lng": s_lng,
                "address": s.get("address", ""),
                "source": "cache",
            })
        return {"city": city, "attractions": formatted, "total": len(formatted), "source": "cache"}

    # 缓存 miss → 实时 API 搜索
    coords = _CITY_COORDS.get(city, (39.93, 116.45))
    if ref_lat is None:
        ref_lat, ref_lng = coords
    else:
        ref_lat = float(ref_lat)
        ref_lng = float(ref_lng)

    travel_categories = ["scenic", "restaurant", "hotpot", "shopping", "cafe"]
    all_shops = []
    seen_ids = set()

    for cat in travel_categories:
        try:
            resp = _amap_client.search_nearby(
                lng=ref_lng, lat=ref_lat,
                radius=30000,
                keywords="",
                category=cat,
                min_rating=3.5,
            )
            for shop in resp.get("shops", []):
                sid = shop.get("shop_id", "")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    shop["_category"] = cat
                    all_shops.append(shop)
        except Exception as e:
            print(f"[get_top20] 品类 '{cat}' API搜索失败: {e}", flush=True)
            continue

    all_shops.sort(key=lambda s: float(s.get("rating", 0) or 0), reverse=True)
    top20 = all_shops[:20]

    formatted = []
    for s in top20:
        formatted.append({
            "shop_id": s.get("shop_id", ""),
            "name": s.get("name", ""),
            "category": s.get("_category", ""),
            "category_cn": backend.CATEGORY_NAME_CN.get(s.get("_category", ""), s.get("_category", "")),
            "rating": s.get("rating", 0),
            "coord": s.get("coord", f"{s.get('lat', ref_lat)},{s.get('lng', ref_lng)}"),
            "lat": s.get("lat", ref_lat),
            "lng": s.get("lng", ref_lng),
            "address": s.get("address", ""),
            "source": "api",
        })

    return {"city": city, "attractions": formatted, "total": len(formatted), "source": "api"}


@app.route("/api/popular_attractions", methods=["GET"])
def popular_attractions():
    """获取目的地城市 Top20 热门景点/餐厅。
    先查预缓存（北京/上海/广州/深圳/成都/杭州），miss 则实时调用高德 API。
    查询参数: ?city=北京
    """
    city = request.args.get("city", session_state.get("trip_destination", "北京"))
    result = _get_top20_attractions(city)
    return jsonify(dict(status="SUCCESS", **result))


@app.route("/api/search_attraction", methods=["POST"])
def search_attraction():
    """在弹窗内搜索POI加入候选池。优先用 search_poi（完整数据），fallback 到 fuzzy_search。
    请求: {keywords: "长城", city: "北京"}
    """
    data = request.get_json(silent=True) or {}
    keywords = data.get("keywords", "").strip()
    city = data.get("city", session_state.get("trip_destination", "北京"))

    if not keywords:
        return jsonify({"status": "ERROR", "message": "请输入搜索关键词"}), 400

    formatted = []

    # 方法1: search_poi（返回完整 shop 数据：shop_id/rating/category/lat/lng）
    try:
        result = _amap_client.search_poi(keywords=keywords, city=city, offset=10)
        shops = result.get("shops", []) if isinstance(result, dict) else []
        for s in shops[:10]:
            formatted.append({
                "shop_id": s.get("shop_id", ""),
                "name": s.get("name", ""),
                "category": s.get("category", s.get("_category", "")),
                "category_cn": backend.CATEGORY_NAME_CN.get(s.get("category", s.get("_category", "")), ""),
                "rating": s.get("rating", 0),
                "coord": s.get("coord", f"{s.get('lat', 0)},{s.get('lng', 0)}"),
                "lat": s.get("lat", 0),
                "lng": s.get("lng", 0),
                "address": s.get("address", ""),
                "opentime": s.get("opentime", "未知"),
            })
    except Exception as e:
        print(f"[search_attraction] search_poi 失败: {e}", flush=True)

    # 方法2: fuzzy_search 兜底（返回 name/address/location，无 rating/category）
    if not formatted:
        try:
            tips = _amap_client.fuzzy_search(keywords=keywords, city=city)
            if isinstance(tips, list):
                for i, t in enumerate(tips[:10]):
                    loc = t.get("location", "")
                    lat, lng = 0, 0
                    if "," in loc:
                        parts = loc.split(",")
                        try:
                            lng = float(parts[0])
                            lat = float(parts[1])
                        except (ValueError, TypeError):
                            pass
                    formatted.append({
                        "shop_id": f"search_{city}_{i}_{hash(t.get('name', ''))}",
                        "name": t.get("name", ""),
                        "category": "",
                        "category_cn": "",
                        "rating": 0,
                        "coord": f"{lat},{lng}",
                        "lat": lat,
                        "lng": lng,
                        "address": t.get("address", t.get("district", "")),
                        "opentime": "未知",
                    })
        except Exception as e2:
            print(f"[search_attraction] fuzzy_search 也失败: {e2}", flush=True)

    if not formatted:
        return jsonify({"status": "ERROR", "message": f"未找到'{keywords}'相关地点", "results": [], "total": 0}), 200

    return jsonify({"status": "SUCCESS", "results": formatted, "total": len(formatted)})


@app.route("/api/set_trip_config", methods=["POST"])
def set_trip_config():
    """设置多日行程配置：天数 + 目的地 + 交通方式 + 旅行信息，返回 Top20 热门 + 天气预调研。
    请求: {days: 2, destination: "北京", transport: "地铁优先", start_date: "2026-07-15",
           checkin_lat: 39.93, checkin_lng: 116.45,
           departure_city: "上海", outbound_type: "飞机", outbound_departure_time: "08:00",
           outbound_arrival_time: "10:30", arrival_station: "北京大兴国际机场",
           return_type: "高铁", return_departure_time: "16:00", return_station: "北京南站",
           travel_preference: "公共交通"}
    """
    data = request.get_json(silent=True) or {}

    days = int(data.get("days", 2))
    if days < 1:
        days = 1
    elif days > 7:
        days = 7

    destination = data.get("destination", "北京")
    transport = data.get("transport", "步行优先")
    start_date = data.get("start_date", "")
    checkin_lat = data.get("checkin_lat")
    checkin_lng = data.get("checkin_lng")

    # ── 旅行信息（去程/返程）──
    # 重要：仅当请求中实际包含该字段时才更新，防止后续调用（如 fetchPopularAttractions）
    # 用空值覆盖用户已填写的航班/高铁信息
    for _key, _session_key in [
        ("departure_city", "trip_departure_city"),
        ("outbound_type", "trip_outbound_type"),
        ("outbound_departure_time", "trip_outbound_departure_time"),
        ("outbound_arrival_time", "trip_outbound_arrival_time"),
        ("arrival_station", "trip_arrival_station"),
        ("return_type", "trip_return_type"),
        ("return_departure_time", "trip_return_departure_time"),
        ("return_station", "trip_return_station"),
        ("travel_preference", "trip_travel_preference"),
    ]:
        if _key in data:
            session_state[_session_key] = data[_key]

    # 存储基础字段到 session_state
    session_state["trip_mode"] = "multi"
    session_state["trip_days"] = days
    session_state["trip_destination"] = destination
    session_state["trip_transport"] = transport
    session_state["trip_start_date"] = start_date
    if "checkin_lat" in data:
        session_state["trip_checkin_lat"] = checkin_lat
    if "checkin_lng" in data:
        session_state["trip_checkin_lng"] = checkin_lng

    # 生成带日期的 day label
    from datetime import datetime as _dt, timedelta as _td
    _weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    day_labels = []
    if start_date:
        try:
            sd = _dt.strptime(start_date, "%Y-%m-%d")
            for i in range(days):
                d = sd + _td(days=i)
                label = f"{d.month}/{d.day} {_weekdays[d.weekday()]} · 第{i+1}天"
                day_labels.append(label)
        except (ValueError, TypeError):
            day_labels = [f"第{i+1}天" for i in range(days)]
    else:
        day_labels = [f"第{i+1}天" for i in range(days)]

    # 初始化每天的容器
    session_state["days"] = []
    for i in range(days):
        session_state["days"].append({
            "day_index": i,
            "label": day_labels[i],
            "selected_pairs": [],
            "task_list": [],
            "spatial_matrix": {},
            "schedule_result": None,
            "chat_history": [],
            "transport_override": None,
        })
    session_state["active_day_index"] = 0
    session_state["candidate_pool"] = []

    # 获取热门推荐（缓存优先）
    result = _get_top20_attractions(destination, checkin_lat, checkin_lng)

    # 天气预调研（带重试）
    trip_weather = {}
    try:
        adcode = _CITY_ADCODE.get(destination, "110000")
        fc_res = None
        for attempt in range(3):
            try:
                fc_res = _amap_weather_client.get_weather_forecast(adcode=adcode)
                if fc_res.get("forecasts"):
                    break
            except Exception as retry_err:
                print(f"[set_trip_config] 天气API重试 {attempt+1}/3: {retry_err}", flush=True)
                import time as _time
                _time.sleep(1)
        if fc_res is None:
            fc_res = {"forecasts": [], "confidence": "low"}
        forecasts = fc_res.get("forecasts", [])
        # 匹配到每一天
        for i in range(days):
            if start_date:
                try:
                    sd = _dt.strptime(start_date, "%Y-%m-%d")
                    target_date = (sd + _td(days=i)).strftime("%Y-%m-%d")
                    matched = None
                    for fc in forecasts:
                        if fc.get("date") == target_date:
                            matched = fc
                            break
                    if matched:
                        trip_weather[target_date] = matched
                    else:
                        # 超出高德4天预报范围 → 用该城市当月气候均值兜底
                        try:
                            target_month = sd.month if sd else 7
                            climate = _amap_weather_client.get_climate_average(adcode, target_month)
                            if climate:
                                climate["date"] = target_date
                                trip_weather[target_date] = climate
                            else:
                                trip_weather[target_date] = {"confidence": "low", "note": "暂无该日预报数据"}
                        except Exception:
                            trip_weather[target_date] = {"confidence": "low", "note": "暂无该日预报数据"}
                except (ValueError, TypeError):
                    pass
        # 如果没有日期，取前N天的预报
        if not trip_weather and forecasts:
            for i in range(min(days, len(forecasts))):
                fc = forecasts[i]
                trip_weather[fc.get("date", f"day{i}")] = fc
    except Exception as e:
        print(f"[set_trip_config] 天气预调研失败: {e}", flush=True)
    session_state["trip_weather"] = trip_weather

    return jsonify({
        "status": "SUCCESS",
        "trip_mode": "multi",
        "trip_days": days,
        "trip_destination": destination,
        "trip_transport": transport,
        "start_date": start_date,
        "attractions": result["attractions"],
        "attractions_source": result.get("source", "unknown"),
        "candidate_pool_size": len(session_state.get("candidate_pool", [])),
        "weather": trip_weather,
    })


@app.route("/api/smart_schedule", methods=["POST"])
def smart_schedule():
    """核心排程：对候选池执行智能分天+排程。
    请求: {start_time: "09:00"}
    """
    data = request.get_json(silent=True) or {}
    schedule_start = datetime.now()
    start_time = data.get("start_time", "09:00")

    # 优先使用请求中携带的候选列表（前端直接传），回退到 session 缓存
    candidates_from_client = data.get("candidates", None)
    if candidates_from_client:
        candidate_pool = candidates_from_client
        session_state["candidate_pool"] = candidate_pool
    else:
        candidate_pool = session_state.get("candidate_pool", [])
    trip_days = session_state.get("trip_days", 2)
    checkin_lat = session_state.get("trip_checkin_lat")
    checkin_lng = session_state.get("trip_checkin_lng")
    transport = session_state.get("trip_transport", "步行优先")

    if not candidate_pool:
        return jsonify({"status": "ERROR", "message": "候选池为空，请先选择想去的地方"}), 400

    if trip_days < 1:
        return jsonify({"status": "ERROR", "message": "天数必须 >= 1"}), 400

    # ── 事前检查：POI 数量 vs 天数是否合理 ──
    import math
    MAX_SCENIC_PER_DAY = 5  # 每天景点上限（只算 scenic，不含饭店/购物中心）
    scenic_count = sum(1 for s in candidate_pool if s.get("category", "") == "scenic")
    overcrowded_warning = None
    overcrowded_action = data.get("overcrowded_action", "")
    if scenic_count > trip_days * MAX_SCENIC_PER_DAY and overcrowded_action != "continue_anyway":
        recommended = max(trip_days + 1, int(math.ceil(scenic_count / MAX_SCENIC_PER_DAY)))
        return jsonify({
            "status": "OVERCROWDED",
            "scenic_count": scenic_count,
            "current_days": trip_days,
            "suggested_days": recommended,
            "message": f"您选了{scenic_count}个景点，{trip_days}天每天最多{MAX_SCENIC_PER_DAY}个，建议增至{recommended}天体验更好",
            "options": [
                {"action": "extend_days", "label": f"延长至{recommended}天", "days": recommended},
                {"action": "reduce_pois", "label": "返回减少景点"},
                {"action": "continue_anyway", "label": f"保持{trip_days}天，继续排程"},
            ]
        }), 200

    # 如果没有酒店坐标，用第一个POI的坐标作为参考
    if not checkin_lat or not checkin_lng:
        first = candidate_pool[0]
        checkin_lat = float(first.get("lat", 39.93))
        checkin_lng = float(first.get("lng", 116.45))
        session_state["trip_checkin_lat"] = checkin_lat
        session_state["trip_checkin_lng"] = checkin_lng

    # ── 排程结果缓存检查：相同输入秒级返回 ──
    cache_key = _compute_schedule_cache_key(candidate_pool, trip_days, transport, start_time, checkin_lat, checkin_lng)
    if cache_key in _schedule_result_cache:
        cached = _schedule_result_cache[cache_key]
        _time.sleep(2)  # 人工延迟，让用户感知排程过程
        cache_elapsed = (datetime.now() - schedule_start).total_seconds()
        print(f"[排程缓存] ✅ 命中！{len(candidate_pool)}个POI {trip_days}天 → {cache_elapsed:.1f}s返回（含2s延迟）", flush=True)
        result = cached["result"]
        review_result = cached.get("review_result", {})
        trip_weather = session_state.get("trip_weather", {})

        # 恢复 session_state
        days = session_state.get("days", [])
        for i, day_result in enumerate(result.get("days", [])):
            if i < len(days):
                days[i]["selected_pairs"] = day_result.get("pairs", [])
                days[i]["task_list"] = day_result.get("task_list", [])
                days[i]["spatial_matrix"] = day_result.get("spatial_matrix", {})
                days[i]["schedule_result"] = day_result
                days[i]["hotel_info"] = day_result.get("hotel_info", {})
                day_result["label"] = days[i].get("label", day_result.get("label", f"第{i+1}天"))
        session_state["days"] = days
        session_state["phase"] = "done"
        session_state["active_day_index"] = 0

        # 注入多日提醒（虚拟时钟提醒事件）
        reminders_injected = _inject_multi_day_reminders(result)

        # 清理 NaN 值
        result = _replace_nan_in_result(result)

        # 组装与正常响应一致的格式（前端期望 data.days 等字段）
        return jsonify({
            "status": "SUCCESS",
            "days": result.get("days", []),
            "unassigned": result.get("unassigned", []),
            "algorithm_metadata": result.get("algorithm_metadata", {}),
            "hotel_plan": result.get("hotel_plan", []),
            "weather": trip_weather,
            "review": review_result,
            "auto_meals_added": result.get("auto_meals_added", []),
            "closed_conflicts": result.get("closed_conflicts", []),
            "unknown_hours_shops": result.get("unknown_hours_shops", []),
            "overflow_notifications": result.get("overflow_notifications", []),
            "reminders_injected": reminders_injected,
            "overcrowded_warning": None,
            "_debug_candidate_count": len(candidate_pool),
            "_debug_from_cache": True,
        })

    # ── Phase 0: LLM 实时搜索/估算每个店铺的实际耗时和体力消耗 ──
    dynamic_durations, fatigue_weights, suitable_times = _llm_estimate_shop_durations(candidate_pool)

    # ── 应用用户从问题反馈面板选择的 suitable_time 覆盖 ──
    suitable_time_overrides = session_state.pop("suitable_time_overrides", {})
    if suitable_time_overrides:
        for sid, override_st in suitable_time_overrides.items():
            if sid in suitable_times:
                print(f"[suitable_time覆盖] {sid}: {suitable_times[sid]} → {override_st}", flush=True)
            suitable_times[sid] = override_st
        print(f"[suitable_time覆盖] 已应用 {len(suitable_time_overrides)} 个覆盖", flush=True)

    # 调用排程引擎
    try:
        result = _run_multi_day_schedule(
            candidate_pool, trip_days,
            checkin_lat, checkin_lng,
            transport, start_time,
            dynamic_durations=dynamic_durations,
            fatigue_weights=fatigue_weights,
            suitable_times=suitable_times,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "ERROR", "message": f"排程失败: {str(e)}"}), 500

    # 将结果写入 session_state.days，保留已配置的 label
    days = session_state.get("days", [])
    for i, day_result in enumerate(result.get("days", [])):
        if i < len(days):
            days[i]["selected_pairs"] = day_result.get("pairs", [])
            days[i]["task_list"] = day_result.get("task_list", [])
            days[i]["spatial_matrix"] = day_result.get("spatial_matrix", {})
            days[i]["schedule_result"] = day_result
            days[i]["hotel_info"] = day_result.get("hotel_info", {})
            # 用 session 中的 label（含日期）覆盖 scheduler 生成的
            day_result["label"] = days[i].get("label", day_result.get("label", f"第{i+1}天"))

    session_state["days"] = days
    session_state["phase"] = "done"
    session_state["active_day_index"] = 0

    # 附带天气数据
    trip_weather = session_state.get("trip_weather", {})

    # ── LLM 审查（阶段 2）──
    fast_mode = data.get("fast", False)
    review_start = datetime.now()
    pre_review_visits = []
    if fast_mode:
        print(f"[排程] ⚡ 快速模式，跳过LLM审查", flush=True)
        review_result = {}
    else:
        # 审查前完整时间线日志（含时间）
        print(f"[排程审查前] === 完整时间线 ===", flush=True)
        for di, day in enumerate(result.get("days", [])):
            for n in day.get("timeline", []):
                print(f"  D{di+1} {n.get('time',''):>6} {n.get('action',''):<12} {n.get('memo','')[:40]} (shop_id={n.get('shop_id','')}, cat={n.get('category','')})", flush=True)
        pre_review_visits = []
        for di, day in enumerate(result.get("days", [])):
            for n in day.get("timeline", []):
                if n.get("action") == "VISIT":
                    pre_review_visits.append(f"D{di+1}:{n.get('shop_id','')}:{n.get('memo','')[:20]}")
        print(f"[排程审查前] VISIT节点({len(pre_review_visits)}): {pre_review_visits}", flush=True)

        review_result = _llm_review_schedule(result, trip_weather, overcrowded_warning)

    # ── 应用 auto_fixes（LLM 确定的优化，无需用户确认）──
    auto_fixes_applied = []
    for fix in review_result.get("auto_fixes", []):
        fix_type = fix.get("type", "general")
        try:
            if fix_type == "move_to_day":
                from_day = fix.get("from_day_index", -1)
                to_day = fix.get("to_day_index", -1)
                poi_name = fix.get("poi_name", "")
                if 0 <= to_day < len(result["days"]) and poi_name:
                    moved = None
                    actual_from_day = -1
                    for di, day in enumerate(result["days"]):
                        for t in list(day.get("task_list", [])):
                            if t.get("name", "") == poi_name:
                                moved = t
                                actual_from_day = di
                                day["task_list"].remove(t)
                                break
                        if moved:
                            break
                    if moved and actual_from_day >= 0:
                        # ⚠️ 使用 actual_from_day（POI 实际所在天）而非 LLM 指定的 from_day
                        # 防止 LLM 错误指定 from_day 导致 timeline/pairs 误删
                        result["days"][to_day].setdefault("task_list", []).append(moved)
                        # 同步更新 selected_pairs（pairs 列表）——从实际所在天移除
                        actual_src_pairs = result["days"][actual_from_day].get("pairs", [])
                        moved_pair = None
                        for p in actual_src_pairs:
                            if len(p) >= 3 and p[2] == poi_name:
                                moved_pair = p
                                break
                        if moved_pair:
                            result["days"][actual_from_day]["pairs"] = [p for p in actual_src_pairs if not (len(p) >= 3 and p[2] == poi_name)]
                            result["days"][to_day].setdefault("pairs", []).append(moved_pair)
                        # 同步更新 timeline：从实际所在天移除（精确 shop_id 匹配）
                        actual_src_timeline = result["days"][actual_from_day].get("timeline", [])
                        moved_shop_id = moved.get("shop_id", "") if isinstance(moved, dict) else ""
                        moved_task_id = moved.get("task_id", "") if isinstance(moved, dict) else ""
                        match_id = moved_shop_id or moved_task_id
                        if match_id:
                            result["days"][actual_from_day]["timeline"] = [
                                n for n in actual_src_timeline
                                if n.get("shop_id") != match_id
                            ]
                        # 目标天 timeline 插入占位节点（按时间排序）
                        dst_timeline = result["days"][to_day].get("timeline", [])
                        placeholder_time = "10:00"
                        dst_timeline.append({
                            "time": placeholder_time,
                            "action": "VISIT",
                            "memo": f"📌 {poi_name}（已移入）",
                            "category": moved.get("category", ""),
                            "shop_id": moved_shop_id,
                            "duration_minutes": moved.get("duration_minutes", 60),
                            "opentime": "未知",
                        })
                        dst_timeline.sort(key=lambda n: (lambda t: int(t.split(":")[0])*60 + int(t.split(":")[1]))(n.get("time", "00:00")))
                        result["days"][to_day]["timeline"] = dst_timeline
                        auto_fixes_applied.append(f"已将「{poi_name}」从第{actual_from_day+1}天移至第{to_day+1}天")
                        print(f"[auto_fix] move_to_day: {poi_name} D{actual_from_day+1}→D{to_day+1} (LLM指定的from_day={from_day+1})", flush=True)
                    else:
                        print(f"[auto_fix] move_to_day 跳过: 未在任何天找到 '{poi_name}'", flush=True)
            elif fix_type == "remove_poi":
                # 【设计原则】预选池目的地 100% 保留，不允许 LLM 删除。
                # 若 LLM 认为某 POI 不合理，应转为 question 让用户决定，
                # 或在 risk_flags 中标注，绝不自动删除。
                poi_name = fix.get("poi_name", "")
                print(f"[auto_fix] remove_poi 已禁用，忽略删除「{poi_name}」的请求", flush=True)
                # 转为 risk_flag 提醒用户
                if "risk_flags" not in review_result:
                    review_result["risk_flags"] = []
                review_result["risk_flags"].append(f"LLM 建议移除「{poi_name}」，已自动保留（所有预选目的地均须排入）")
            elif fix_type == "swap":
                day_a = fix.get("day_a_index", -1)
                day_b = fix.get("day_b_index", -1)
                poi_a = fix.get("poi_a_name", "")
                poi_b = fix.get("poi_b_name", "")
                if 0 <= day_a < len(result["days"]) and 0 <= day_b < len(result["days"]) and poi_a and poi_b:
                    tl_a = result["days"][day_a].get("task_list", [])
                    tl_b = result["days"][day_b].get("task_list", [])
                    item_a = next((t for t in tl_a if t.get("name", "") == poi_a), None)
                    item_b = next((t for t in tl_b if t.get("name", "") == poi_b), None)
                    if item_a and item_b:
                        tl_a.remove(item_a)
                        tl_b.remove(item_b)
                        tl_a.append(item_b)
                        tl_b.append(item_a)
                        auto_fixes_applied.append(f"已交换「{poi_a}」与「{poi_b}」")
            # reorder / general 型仅作建议，不自动执行
        except Exception as fix_err:
            print(f"[auto_fix] 应用失败 ({fix_type}): {fix_err}", flush=True)

    if auto_fixes_applied:
        print(f"[auto_fix] 共应用 {len(auto_fixes_applied)} 个自动修正: {auto_fixes_applied}", flush=True)
        # 更新 applied_fixes 到 review_result
        review_result["applied_fixes"] = auto_fixes_applied

    # 审查后排程快照日志（仅正常模式）
    if not fast_mode:
        post_review_visits = []
        for di, day in enumerate(result.get("days", [])):
            for n in day.get("timeline", []):
                if n.get("action") == "VISIT":
                    post_review_visits.append(f"D{di+1}:{n.get('shop_id','')}:{n.get('memo','')[:20]}")
        print(f"[排程审查后] VISIT节点({len(post_review_visits)}): {post_review_visits}", flush=True)
        print(f"[排程审查后] === 完整时间线 ===", flush=True)
        for di, day in enumerate(result.get("days", [])):
            for n in day.get("timeline", []):
                print(f"  D{di+1} {n.get('time',''):>6} {n.get('action',''):<12} {n.get('memo','')[:40]} (shop_id={n.get('shop_id','')}, cat={n.get('category','')})", flush=True)
        if pre_review_visits != post_review_visits:
            lost = [v for v in pre_review_visits if v not in post_review_visits]
            gained = [v for v in post_review_visits if v not in pre_review_visits]
            print(f"[排程审查] ⚠️ VISIT节点变化! 丢失:{lost} 新增:{gained}", flush=True)

    # ── ⚠️ 目的地完整性验证：确保所有非餐类目的地都在 timeline 中有 VISIT 节点 ──
    MEAL_CATS_CHECK = {"restaurant", "hotpot", "japanese", "food", "dining", "buffet", "barbecue"}
    all_non_meal_shops = {}  # shop_id → {name, category}
    for s in candidate_pool:
        cat = s.get("category", "")
        sid = s.get("shop_id", "")
        if cat not in MEAL_CATS_CHECK and sid:
            all_non_meal_shops[sid] = {"name": s.get("name", ""), "category": cat}

    timeline_visit_ids = set()
    for day in result.get("days", []):
        for n in day.get("timeline", []):
            if n.get("action") == "VISIT" and n.get("shop_id"):
                timeline_visit_ids.add(n.get("shop_id"))

    missing_from_timeline = []
    for sid, info in all_non_meal_shops.items():
        if sid not in timeline_visit_ids:
            missing_from_timeline.append((sid, info["name"], info["category"]))

    if missing_from_timeline:
        print(f"[完整性验证] ❌ {len(missing_from_timeline)} 个非餐目的地缺失于 timeline: {[(sid, name) for sid, name, _ in missing_from_timeline]}", flush=True)
        # ── 跨天分配恢复：按每天剩余容量合理分布，而非全堆到最后一天 ──
        def _time_to_min(t_str):
            try:
                parts = t_str.split(":")
                return int(parts[0]) * 60 + int(parts[1])
            except (ValueError, IndexError, AttributeError):
                return 600  # 默认10:00

        def _min_to_time(m):
            return f"{max(0, min(23, m // 60)):02d}:{max(0, min(59, m % 60)):02d}"

        # 计算每天剩余容量（bedtime - 最后一个非BEDTIME活动的结束时间）
        # 同时扫描日间所有 gap，选最大可用容量（不只是就寝前）
        day_capacities = []
        for d in result.get("days", []):
            timeline = d.get("timeline", [])
            # 找 BEDTIME
            bedtime = 22 * 60
            for n in timeline:
                if n.get("action") == "BEDTIME":
                    bedtime = _time_to_min(n.get("time", "22:00"))
                    break

            # 计算 last_end（排除 BEDTIME 节点）
            last_end = 10 * 60
            for n in timeline:
                if n.get("action") == "BEDTIME":
                    continue  # 不纳入活动结束时间计算
                t = _time_to_min(n.get("time", "10:00"))
                dur = n.get("duration_minutes", 0)
                end = t + dur
                if end > last_end:
                    last_end = end

            # 扫描日间所有相邻节点间的 gap（不只是就寝前）
            max_gap = 0
            # 添加就寝前 gap
            pre_bedtime_gap = max(0, bedtime - last_end)
            max_gap = max(max_gap, pre_bedtime_gap)

            # 扫描节点间 gap
            sorted_nodes = sorted(
                [n for n in timeline if n.get("action") != "BEDTIME"],
                key=lambda n: _time_to_min(n.get("time", "10:00"))
            )
            for j in range(len(sorted_nodes) - 1):
                prev_t = _time_to_min(sorted_nodes[j].get("time", "10:00"))
                prev_dur = sorted_nodes[j].get("duration_minutes", 0)
                prev_end = prev_t + prev_dur
                next_t = _time_to_min(sorted_nodes[j + 1].get("time", "10:00"))
                gap = next_t - prev_end
                if gap > max_gap:
                    max_gap = gap

            day_capacities.append(max_gap)

        # 按容量降序排列天的索引
        sorted_day_indices = sorted(range(len(day_capacities)),
                                    key=lambda i: day_capacities[i], reverse=True)

        for sid, name, cat in missing_from_timeline:
            found_in_task = False
            for d in result["days"]:
                for t in d.get("task_list", []):
                    if t.get("shop_id") == sid or t.get("task_id") == sid:
                        found_in_task = True
                        break
            if not found_in_task:
                print(f"[完整性验证] ⚠️ '{name}' ({sid}) 既不在 timeline 也不在 task_list 中！", flush=True)
                continue

            # 选剩余容量最大的天
            best_day_idx = sorted_day_indices[0]
            best_day = result["days"][best_day_idx]
            best_timeline = best_day.get("timeline", [])

            # 计算该天最后一个非BEDTIME活动的结束时间（排除BEDTIME节点）
            last_visit_end = 10 * 60
            for n in best_timeline:
                if n.get("action") == "BEDTIME":
                    continue  # 排除BEDTIME节点
                t = _time_to_min(n.get("time", "10:00"))
                ndur = n.get("duration_minutes", 0)
                if t + ndur > last_visit_end:
                    last_visit_end = t + ndur

            # 使用正确的品类时长（优先用 LLM 估算值）
            dur = dynamic_durations.get(sid, CATEGORY_DURATIONS.get(cat, 60))

            # 找 bedtime 时间，确保插入不在就寝后
            best_bedtime = 22 * 60
            for n in best_timeline:
                if n.get("action") == "BEDTIME":
                    best_bedtime = _time_to_min(n.get("time", "22:00"))
                    break

            # 插入时间 = 最后活动结束 + 15min 缓冲，但必须在就寝前
            insert_time = last_visit_end + 15
            if insert_time + dur > best_bedtime:
                # 如果插入后会超过就寝时间，尝试往前放到就寝前
                insert_time = max(last_visit_end + 5, best_bedtime - dur - 5)
            if insert_time + dur > best_bedtime:
                # 仍然超了就寝 → 极简打卡模式（10min）
                dur = 10
                insert_time = best_bedtime - 15

            best_timeline.append({
                "time": _min_to_time(insert_time),
                "action": "VISIT",
                "memo": f"⚠️ 补入：{name}（系统自动分配至第{best_day_idx+1}天）",
                "category": cat,
                "shop_id": sid,
                "duration_minutes": dur,
                "opentime": "未知",
            })
            # 更新该天容量
            day_capacities[best_day_idx] = max(0, day_capacities[best_day_idx] - dur - 15)
            # 重新排序容量（简单重排即可）
            sorted_day_indices.sort(key=lambda i: day_capacities[i], reverse=True)
            print(f"[完整性验证] 已恢复 '{name}' ({sid}) → 第{best_day_idx+1}天 {_min_to_time(insert_time)} (dur={dur}min)", flush=True)

        # 所有天的 timeline 重新按时间排序（保护 WAKE_UP 最前、BEDTIME 最后）
        for d in result["days"]:
            timeline = d.get("timeline", [])
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
            rest.sort(key=lambda n: (lambda t: int(t.split(":")[0])*60 + int(t.split(":")[1]))(n.get("time", "00:00")) if ":" in str(n.get("time", "")) else 600)
            d["timeline"] = []
            if wake_node:
                d["timeline"].append(wake_node)
            d["timeline"].extend(rest)
            if bedtime_node:
                d["timeline"].append(bedtime_node)

        # 重新验证
        timeline_visit_ids2 = set()
        for day in result.get("days", []):
            for n in day.get("timeline", []):
                if n.get("action") == "VISIT" and n.get("shop_id"):
                    timeline_visit_ids2.add(n.get("shop_id"))
        still_missing = [sid for sid in all_non_meal_shops if sid not in timeline_visit_ids2]
        if still_missing:
            print(f"[完整性验证] ⚠️ 恢复后仍有 {len(still_missing)} 个缺失: {still_missing}", flush=True)
        else:
            print(f"[完整性验证] ✅ 恢复后所有非餐目的地均已存在于 timeline（跨天分配）", flush=True)
    else:
        print(f"[完整性验证] ✅ 所有 {len(all_non_meal_shops)} 个非餐目的地均存在于 timeline", flush=True)

    # ── Phase 6: L3 逆向倒灌 + 极简打卡恢复 ──
    # 策略 1: 逆向扫描最宽松天，BEDTIME 前插入 10min 极简打卡
    # 策略 2（兜底）: 间隙扫描，将剩余未分配塞入时间空隙
    l3_unassigned = result.get("unassigned", [])
    if l3_unassigned:
        print(f"[L3倒灌] 开始处理 {len(l3_unassigned)} 个未分配店铺...", flush=True)
        # 优先：逆向扫描最宽松天策略
        result["days"], l3_still_unassigned, l3_backup_count = _l3_loosest_day_backfill(
            l3_unassigned, result["days"], transport
        )
        # 兜底：剩余未分配用原有间隙扫描
        if l3_still_unassigned:
            gap_count = 0
            result["days"], l3_still_unassigned, gap_count = _l3_capacity_scan_and_dump(
                l3_still_unassigned, result["days"],
                float(checkin_lat), float(checkin_lng), transport
            )
            l3_backup_count += gap_count
        result["unassigned"] = l3_still_unassigned
        print(f"[L3倒灌] 完成: {l3_backup_count} 个已恢复为备份打卡, "
              f"{len(l3_still_unassigned)} 个仍无法安排", flush=True)

    # ── L3 回填后重新排序所有天的时间线（确保 WAKE_UP 最前、BEDTIME 最后）──
    for day in result.get("days", []):
        _sort_timeline_keep_wake_bedtime(day.get("timeline", []))

    # ── 提醒注入（阶段 3）：起床/就寝 → 持续响铃 ──
    reminders_injected = _inject_multi_day_reminders(result)

    # ── 组装最终 VISIT 快照用于前端诊断 ──
    final_visit_snapshot = []
    for di, day in enumerate(result.get("days", [])):
        for n in day.get("timeline", []):
            if n.get("action") == "VISIT":
                final_visit_snapshot.append({"day": di+1, "shop_id": n.get("shop_id",""), "memo": (n.get("memo","") or "")[:40]})

    # ── 写入排程结果缓存 ──
    try:
        _schedule_result_cache[cache_key] = {
            "result": result,
            "review_result": review_result,
            "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "num_shops": len(candidate_pool),
            "trip_days": trip_days,
        }
        _save_schedule_result_cache()
        elapsed = (datetime.now() - schedule_start).total_seconds()
        mode_tag = "⚡fast" if fast_mode else "normal"
        print(f"[排程缓存] 💾 已缓存排程结果 key={cache_key} ({len(candidate_pool)}POI {trip_days}天) 总耗时={elapsed:.1f}s [{mode_tag}]", flush=True)
    except Exception as cache_err:
        print(f"[排程缓存] ⚠️ 写入失败: {cache_err}", flush=True)

    return jsonify({
        "status": "SUCCESS",
        "days": result.get("days", []),
        "unassigned": result.get("unassigned", []),
        "algorithm_metadata": result.get("algorithm_metadata", {}),
        "hotel_plan": result.get("hotel_plan", []),  # Phase 6: 动态换住决策
        "weather": trip_weather,
        "review": review_result,  # {phase, auto_fixes, questions, risk_flags}
        "auto_meals_added": result.get("auto_meals_added", []),
        "closed_conflicts": result.get("closed_conflicts", []),
        "unknown_hours_shops": result.get("unknown_hours_shops", []),
        "overflow_notifications": result.get("overflow_notifications", []),
        "reminders_injected": reminders_injected,
        "overcrowded_warning": overcrowded_warning,  # 事前检查：POI过多警告
        "_debug_visit_snapshot": final_visit_snapshot,  # 诊断用：最终 VISIT 节点列表
        "_debug_candidate_count": len(candidate_pool),
        "_debug_auto_fixes_count": len(auto_fixes_applied),
    })


@app.route("/api/schedule_overcrowded_answer", methods=["POST"])
def schedule_overcrowded_answer():
    """处理超量 POI 的用户选择。
    请求: {action: "extend_days"|"reduce_pois"|"continue_anyway", days: int}
    """
    _ensure_agent()
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")

    if action == "reduce_pois":
        # 返回选择阶段，让用户减少 POI
        session_state["phase"] = "poi_selection"
        return jsonify({
            "status": "REDUCE_POIS",
            "message": "请减少选择的景点数量，或返回重新选择。",
        })

    if action == "extend_days":
        new_days = data.get("days", 0)
        if new_days < 1:
            return jsonify({"status": "ERROR", "message": "无效的天数"}), 400
        session_state["trip_days"] = new_days
        # 重建 days 数组
        session_state["days"] = [
            {"day_index": i, "label": f"第{i+1}天",
             "selected_pairs": [], "task_list": [], "spatial_matrix": {}}
            for i in range(new_days)
        ]

    if action == "continue_anyway":
        # 不改变天数，继续排程（前端以 overcrowded_action=continue_anyway 重调 smart_schedule）
        pass

    return jsonify({
        "status": "OK",
        "action": action,
        "trip_days": session_state.get("trip_days", 2),
        "message": "已更新，请重新触发排程" if action in ("extend_days", "continue_anyway") else "请减少景点",
    })


@app.route("/api/schedule_review_answer", methods=["POST"])
def schedule_review_answer():
    """LLM 审查问答交互：用户回答审查问题后，LLM 应用到排程。
    请求: {question_id: "q1", answer: "改到后天", answers: {"q1":"...","q2":"..."}}
    """
    _ensure_agent()
    data = request.get_json(silent=True) or {}
    question_id = data.get("question_id", "")
    answer = data.get("answer", "")
    all_answers = data.get("answers", {})

    if not all_answers and question_id:
        all_answers = {question_id: answer}

    review_state = session_state.get("_review_state", {})
    questions = review_state.get("questions", [])
    schedule = review_state.get("schedule_snapshot", {})

    if not all_answers:
        sched = review_state.get("schedule_snapshot", {})
        return jsonify({"phase": "done", "days": sched.get("days", []), "message": "无需回答"})

    # ── ⚠️ 快照完整性校验 + 回退逻辑 ──
    sched_days = schedule.get("days", [])
    snapshot_total_nodes = sum(len(d.get("timeline", [])) for d in sched_days)
    print(f"[审查问答] 快照校验: days={len(sched_days)}, 总节点={snapshot_total_nodes}", flush=True)

    if not sched_days or snapshot_total_nodes == 0:
        print("[审查问答] ⚠️ 快照异常（空或无timeline），尝试回退到 session_state.days", flush=True)
        fallback_days = session_state.get("days", [])
        if fallback_days:
            schedule = {"days": []}
            for d in fallback_days:
                sr = d.get("schedule_result", {})
                if sr and sr.get("timeline"):
                    schedule["days"].append(sr)
            sched_days = schedule.get("days", [])
            fb_total = sum(len(d.get("timeline", [])) for d in sched_days)
            print(f"[审查问答] 回退后: days={len(sched_days)}, 总节点={fb_total}", flush=True)
        if not sched_days or sum(len(d.get("timeline", [])) for d in sched_days) == 0:
            print("[审查问答] ❌ 快照和 session_state.days 均为空！放弃操作", flush=True)
            return jsonify({"phase": "done", "days": [], "fallback": True, "error": "snapshot_empty"})

    # ── 获取天气数据（用于 LLM 上下文）──
    trip_weather = session_state.get("trip_weather", {})
    sorted_weather = sorted(trip_weather.items())
    weather_context = ""
    for di_idx in range(len(sched_days)):
        if di_idx < len(sorted_weather):
            _, w = sorted_weather[di_idx]
            cond = "户外适宜" if w.get("outdoor_suitable") else "户外不宜"
            weather_context += f"第{di_idx+1}天(day_index={di_idx}): {w.get('day_weather','?')} {cond}\n"

    # 让 LLM 基于用户回答调整排程
    try:
        days_text = ""
        for di_idx, day in enumerate(sched_days):
            days_text += f"\n### {day.get('label', '')} (day_index={di_idx})\n"
            for node in day.get("timeline", []):
                days_text += f"  {node.get('time', '')} {node.get('memo', '')}\n"

        qa_text = "\n".join([f"Q: {qid} A: {ans}" for qid, ans in all_answers.items()])

        system_prompt = """你是旅行规划专家。用户回答了你的问题，请根据回答输出对排程的**具体操作**（而非完整时间线）。只输出 JSON。

支持的操作类型：
- add_node: 添加新节点 → {"type":"add_node","day_index":0,"node":{"time":"15:00","action":"VISIT","memo":"颐和园","category":"scenic"}}
- move_node: 移动节点到另一天（用于重新分配，不可用于删除） → {"type":"move_node","target_name":"长城","to_day":2,"to_time":"09:00"}
- change_time: 修改节点时间 → {"type":"change_time","day_index":0,"target_name":"天坛","new_time":"10:00"}
- swap_node: 替换节点 → {"type":"swap_node","day_index":0,"target_name":"故宫","new_node":{"time":"09:00","action":"VISIT","memo":"颐和园","category":"scenic"}}
- update_node: 更新节点字段 → {"type":"update_node","day_index":0,"target_name":"早餐","field":"time","value":"08:00"}

输出格式：
{"operations":[{"type":"move_node","target_name":"长城","to_day":2,"to_time":"09:00"}],"follow_up_questions":[]}

重要规则：
- day_index / to_day 从 0 开始计数：第1天=0, 第2天=1, 第3天=2, 以此类推
- 不需要改动的节点不要出现在 operations 中
- target_name 必须从当前排程 memo 中**精确**匹配（完全相等），不要模糊匹配
- ⚠️ 严禁删除任何 VISIT 节点！所有用户选择的目的地都必须保留在时间线中！
- 如需减少某天负担，用 move_node 将 VISIT 移到另一天，不要删除
- 如果无需进一步问题，follow_up_questions 为空数组；最多再提 1 个跟进问题
- follow_up_questions 格式: [{"question_id":"q_f1","question_text":"问题文本","options":["选项1","选项2"]}]
- 注意天气上下文：如果涉及户外活动改期，检查目标天是否户外适宜"""

        user_prompt = f"""## 天气上下文
{weather_context}
## 当前排程
{days_text}

## 用户回答
{qa_text}

请根据用户回答调整排程。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        resp = agent._call_llm(messages, max_tokens=800, response_format={"type": "json_object"})
        content = ""
        if hasattr(resp, "content"):
            content = resp.content or ""
        elif hasattr(resp, "choices") and resp.choices:
            content = resp.choices[0].message.content or ""
        elif isinstance(resp, str):
            content = resp

        print(f"[审查问答] LLM 原始返回 ({len(content)} chars): {content[:300]}...", flush=True)

        # JSON mode 优先直接解析；fallback 正则
        try:
            parsed = json.loads(content.strip())
        except (json.JSONDecodeError, ValueError):
            import re as _re
            json_match = _re.search(r'\{[\s\S]*\}', content)
            if json_match:
                try:
                    parsed = json.loads(json_match.group())
                except (json.JSONDecodeError, ValueError):
                    print(f"[json_parse] schedule_review_answer 解析失败，回退空操作: {content[:100]}", flush=True)
                    return jsonify({"phase": "done", "days": schedule.get("days", []), "fallback": True})
            else:
                print(f"[json_parse] schedule_review_answer 未找到 JSON，回退空操作: {content[:100]}", flush=True)
                return jsonify({"phase": "done", "days": schedule.get("days", []), "fallback": True})

        if isinstance(parsed, dict):
            operations = parsed.get("operations", [])
            follow_up = parsed.get("follow_up_questions", [])

            # 应用 LLM 的操作到 schedule（精确操作，不动未改节点）
            sched_days = schedule.get("days", [])
            num_days = len(sched_days)
            changes_log = []

            # ── 辅助函数 ──
            def _norm_day_index(di):
                """规范化 day_index：LLM 看到的是"第1天/第2天"，可能传 1-indexed。
                自动兼容 0-indexed 和 1-indexed。返回 (normalized_index, ok)"""
                if di is None or di < 0:
                    return di, False
                if 0 <= di < num_days:
                    return di, True   # 0-indexed，在范围内
                if 1 <= di <= num_days:
                    return di - 1, True  # 1-indexed → 0-indexed
                return di, False

            def _match_node(node, target):
                """精确匹配节点：memo 完全相等 或 shop_id 完全相等"""
                if not target:
                    return False
                return node.get("memo", "") == target or node.get("shop_id", "") == target

            def _sort_timeline(tl):
                tl.sort(key=lambda n: _time_str_to_minutes(n.get("time", "")) if n.get("time", "") else 9999)

            # 操作前日志
            before_counts = [len(sd.get("timeline", [])) for sd in sched_days]
            print(f"[审查问答] 操作前各天节点数: {before_counts}", flush=True)

            for op in operations:
                op_type = op.get("type", "")
                try:
                    if op_type == "add_node":
                        di, ok = _norm_day_index(op.get("day_index", -1))
                        node = op.get("node", {})
                        if ok and di < num_days and node:
                            tl = sched_days[di].setdefault("timeline", [])
                            tl.append(node)
                            _sort_timeline(tl)
                            changes_log.append(f"第{di+1}天新增{node.get('memo', '')}")
                        else:
                            print(f"[审查问答] add_node 跳过: day_index={op.get('day_index')}, ok={ok}, node={'有' if node else '无'}", flush=True)

                    elif op_type == "remove_node":
                        # ⚠️ 禁止删除 VISIT 节点——所有目的地都必须保留在时间线中
                        di, ok = _norm_day_index(op.get("day_index", -1))
                        target = op.get("target_name", "")
                        if not ok:
                            print(f"[审查问答] remove_node 跳过: day_index={op.get('day_index')} 无效", flush=True)
                            continue
                        tl = sched_days[di].get("timeline", [])
                        found = False
                        target_node = None
                        for node in list(tl):
                            if _match_node(node, target):
                                target_node = node
                                found = True
                                break
                        if not found:
                            print(f"[审查问答] remove_node: 第{di+1}天未找到 '{target}'", flush=True)
                            continue
                        if target_node and target_node.get("action") == "VISIT":
                            print(f"[审查问答] remove_node 已阻止: 不允许删除 VISIT 节点 '{target}'（所有目的地必须保留）", flush=True)
                            changes_log.append(f"⚠️ 已阻止删除「{target}」（所有目的地均须保留）")
                            continue
                        tl.remove(target_node)
                        changes_log.append(f"第{di+1}天删除{target}")

                    elif op_type == "move_node":
                        target = op.get("target_name", "")
                        to_di, to_ok = _norm_day_index(op.get("to_day", -1))
                        to_time = op.get("to_time", "")
                        # ⚠️ 先验证目标天有效，再从来源删除（防止静默丢失节点）
                        if not to_ok:
                            print(f"[审查问答] move_node 跳过: to_day={op.get('to_day')} 无效 (num_days={num_days})", flush=True)
                            continue
                        # 在来源天中查找并移除
                        moved = None
                        from_day_idx = -1
                        for di, sd in enumerate(sched_days):
                            tl = sd.get("timeline", [])
                            for node in list(tl):
                                if _match_node(node, target):
                                    moved = dict(node)
                                    tl.remove(node)
                                    from_day_idx = di
                                    break
                            if moved:
                                break
                        if moved is None:
                            print(f"[审查问答] move_node: 未找到 '{target}'", flush=True)
                            continue
                        # 现在安全：已从来源删除，插入目标天
                        if to_time:
                            moved["time"] = to_time
                        target_tl = sched_days[to_di].setdefault("timeline", [])
                        target_tl.append(moved)
                        _sort_timeline(target_tl)
                        changes_log.append(f"移动{target}从第{from_day_idx+1}天到第{to_di+1}天")
                        print(f"[审查问答] move_node: '{target}' day{from_day_idx+1}→day{to_di+1} @{to_time or moved.get('time','')}", flush=True)

                    elif op_type == "change_time":
                        di, ok = _norm_day_index(op.get("day_index", -1))
                        target = op.get("target_name", "")
                        new_time = op.get("new_time", "")
                        if not ok:
                            print(f"[审查问答] change_time 跳过: day_index={op.get('day_index')} 无效", flush=True)
                            continue
                        if not new_time:
                            continue
                        tl = sched_days[di].get("timeline", [])
                        for node in tl:
                            if _match_node(node, target):
                                node["time"] = new_time
                                changes_log.append(f"第{di+1}天{target}时间改为{new_time}")
                                break
                        _sort_timeline(tl)

                    elif op_type == "swap_node":
                        di, ok = _norm_day_index(op.get("day_index", -1))
                        target = op.get("target_name", "")
                        new_node = op.get("new_node", {})
                        if not ok:
                            print(f"[审查问答] swap_node 跳过: day_index={op.get('day_index')} 无效", flush=True)
                            continue
                        if not new_node:
                            continue
                        tl = sched_days[di].get("timeline", [])
                        for i, node in enumerate(tl):
                            if _match_node(node, target):
                                tl[i] = new_node
                                changes_log.append(f"第{di+1}天替换{target}为{new_node.get('memo', '')}")
                                break
                        _sort_timeline(tl)

                    elif op_type == "update_node":
                        di, ok = _norm_day_index(op.get("day_index", -1))
                        target = op.get("target_name", "")
                        field = op.get("field", "")
                        value = op.get("value", "")
                        if not ok:
                            print(f"[审查问答] update_node 跳过: day_index={op.get('day_index')} 无效", flush=True)
                            continue
                        if not field:
                            continue
                        tl = sched_days[di].get("timeline", [])
                        for node in tl:
                            if _match_node(node, target):
                                node[field] = value
                                changes_log.append(f"第{di+1}天更新{target}的{field}")
                                break
                except Exception as _op_exc:
                    print(f"[审查问答] 操作异常 {op_type}: {_op_exc}", flush=True)
                    pass  # 单个操作失败不影响其他操作

            # 操作后日志
            after_counts = [len(sd.get("timeline", [])) for sd in sched_days]
            print(f"[审查问答] 操作后各天节点数: {after_counts}, 变更: {changes_log}", flush=True)

            # ── ⚠️ 目的地完整性验证：确保 task_list 中所有非餐目的地都在 timeline 中有 VISIT 节点 ──
            MEAL_CATS_CHECK2 = {"restaurant", "hotpot", "japanese", "food", "dining", "buffet", "barbecue"}
            review_missing = []
            for di, day in enumerate(sched_days):
                task_shop_ids = {}
                for t in day.get("task_list", []):
                    sid = t.get("shop_id") or t.get("task_id", "")
                    cat = t.get("category", "")
                    if sid and cat not in MEAL_CATS_CHECK2:
                        task_shop_ids[sid] = t.get("name", "")
                tl_shop_ids = {n.get("shop_id", "") for n in day.get("timeline", []) if n.get("shop_id", "")}
                for sid, name in task_shop_ids.items():
                    if sid not in tl_shop_ids:
                        review_missing.append((di, sid, name))
                        print(f"[审查问答] ❌ 完整性验证: 第{di+1}天 task_list 有 '{name}' ({sid}) 但 timeline 中无 VISIT 节点", flush=True)
            if review_missing:
                for di, sid, name in review_missing:
                    if di < len(sched_days):
                        day = sched_days[di]
                        tl = day.get("timeline", [])
                        # 找该天最后一个VISIT的结束时间
                        last_end = 10 * 60
                        for n in tl:
                            t_str = n.get("time", "10:00")
                            try:
                                parts = t_str.split(":")
                                t = int(parts[0]) * 60 + int(parts[1])
                            except (ValueError, IndexError):
                                t = 10 * 60
                            end = t + n.get("duration_minutes", 0)
                            if end > last_end:
                                last_end = end
                        # 找原始 category 以使用正确的时长
                        orig_cat = "scenic"
                        for t in day.get("task_list", []):
                            if (t.get("shop_id") or t.get("task_id", "")) == sid:
                                orig_cat = t.get("category", "scenic")
                                break
                        dur = CATEGORY_DURATIONS.get(orig_cat, 60)
                        insert_time = last_end + 15
                        h, m = insert_time // 60, insert_time % 60
                        tl.append({
                            "time": f"{h:02d}:{m:02d}", "action": "VISIT",
                            "memo": f"⚠️ 恢复：{name}（审查操作后缺失，已自动补回至第{di+1}天）",
                            "category": orig_cat, "shop_id": sid,
                            "duration_minutes": dur, "opentime": "未知",
                        })
                        _sort_timeline(tl)
                print(f"[审查问答] ✅ 已恢复 {len(review_missing)} 个缺失的目的地（使用正确时长+合理时间）", flush=True)
            else:
                print(f"[审查问答] ✅ 所有非餐目的地均在 timeline 中", flush=True)

            # 更新缓存
            session_state["_review_state"]["schedule_snapshot"] = schedule

            phase = "schedule_review" if follow_up else "done"
            return jsonify({
                "phase": phase,
                "days": schedule.get("days", []),
                "questions": follow_up,
                "changes": changes_log,
            })

        else:
            print(f"[json_parse] schedule_review_answer parsed 非 dict，回退空操作: type={type(parsed).__name__}", flush=True)

        return jsonify({"phase": "done", "days": schedule.get("days", []), "fallback": True})

    except Exception as e:
        print(f"[审查问答] LLM 调用失败: {e}", flush=True)
        return jsonify({"phase": "done", "days": schedule.get("days", []), "fallback": True})


@app.route("/api/schedule_resolve_issues", methods=["POST"])
def schedule_resolve_issues():
    """处理排程问题反馈：用户从底部选择框选择解决方案后回传。
    请求: {actions: [{shop_id, action: "cancel"|"force_day"|"force_night"|"ignore"}]}
    修改 session_state 后返回 OK，前端重新调用 /api/smart_schedule。
    """
    data = request.get_json(silent=True) or {}
    actions = data.get("actions", [])

    if not actions:
        return jsonify({"status": "ERROR", "message": "缺少 actions 参数"}), 400

    candidate_pool = session_state.get("candidate_pool", [])
    if not candidate_pool:
        return jsonify({"status": "ERROR", "message": "无候选池"}), 400

    # 初始化 suitable_time 覆盖存储
    if "suitable_time_overrides" not in session_state:
        session_state["suitable_time_overrides"] = {}

    cancelled_ids = set()
    for a in actions:
        shop_id = a.get("shop_id", "")
        action = a.get("action", "")
        if action == "cancel":
            cancelled_ids.add(shop_id)
            # 也从覆盖中清除
            session_state["suitable_time_overrides"].pop(shop_id, None)
        elif action == "force_day":
            session_state["suitable_time_overrides"][shop_id] = "day"
        elif action == "force_night":
            session_state["suitable_time_overrides"][shop_id] = "night"
        elif action == "force_both":
            session_state["suitable_time_overrides"][shop_id] = "both"
        # "ignore": 不做任何修改

    # 从候选池中移除取消的 POI
    if cancelled_ids:
        session_state["candidate_pool"] = [
            s for s in candidate_pool
            if s.get("shop_id", "") not in cancelled_ids
        ]
        print(f"[问题解决] 已取消 {len(cancelled_ids)} 个POI: {cancelled_ids}", flush=True)

    print(f"[问题解决] 收到 {len(actions)} 个操作, 取消={len(cancelled_ids)}, "
          f"覆盖={list(session_state['suitable_time_overrides'].keys())}", flush=True)

    return jsonify({
        "status": "OK",
        "cancelled": list(cancelled_ids),
        "overrides": session_state["suitable_time_overrides"],
        "message": "已应用用户选择，请重新触发排程",
    })


# ======================================================================
# 酒店搜索 + 选择 API
# ======================================================================

@app.route("/api/search_hotels_for_day", methods=["POST"])
def api_search_hotels_for_day():
    """搜索某天的策略感知酒店推荐。
    请求: {day_index: 0}
    返回: {strategy, strategy_label, search_center, hotels: [{shop_id, name, rating,
           price_level, price_range, address, phone, distance, opentime, lat, lng}]}
    """
    data = request.get_json(silent=True) or {}
    day_idx = int(data.get("day_index", 0))
    days = session_state.get("days", [])
    if day_idx < 0 or day_idx >= len(days):
        return jsonify({"error": f"无效的天索引: {day_idx}"}), 400

    day = days[day_idx]
    checkin_lat = float(session_state.get("trip_checkin_lat", 39.93))
    checkin_lng = float(session_state.get("trip_checkin_lng", 116.45))
    destination = session_state.get("trip_destination", "北京")

    # ── 确定策略 ──
    result_days = [{
        "day_index": d.get("day_index", i),
        "timeline": d.get("schedule_result", {}).get("timeline", d.get("timeline", [])),
        "task_list": d.get("task_list", d.get("schedule_result", {}).get("task_list", [])),
    } for i, d in enumerate(days)]

    decisions = _compute_hotel_decisions(result_days, checkin_lat, checkin_lng, destination)
    dec = decisions[day_idx] if day_idx < len(decisions) else None

    if not dec:
        return jsonify({"error": "无法计算酒店决策"}), 500

    strategy = dec["strategy"]

    # ── 策略感知搜索中心 ──
    search_lat, search_lng = checkin_lat, checkin_lng
    search_radius = 3000

    if strategy == "switch":
        # 当天 centroid
        lats, lngs = [], []
        for t in day.get("task_list", []):
            try:
                lats.append(float(t.get("lat", 0)))
                lngs.append(float(t.get("lng", 0)))
            except (ValueError, TypeError):
                pass
        if lats and lngs:
            search_lat = sum(lats) / len(lats)
            search_lng = sum(lngs) / len(lngs)

    # sustained: 使用 checkin 坐标，已默认

    # ── 搜索酒店 ──
    raw_hotels = _auto_search_hotels(search_lat, search_lng, destination,
                                      radius=search_radius, limit=15)

    # ── 按价格分层，每层选评分最高的 1 家 ──
    hotels_with_price = []
    for h in raw_hotels:
        cost = _extract_hotel_price(h)
        hotels_with_price.append((h, cost))

    # 价格分层: 高档(>500), 中档(200-500), 经济(100-200), 民宿/青旅(<100)
    tiers = [
        ("luxury", "高档", 500, 99999),
        ("mid", "中档", 200, 500),
        ("economy", "经济", 100, 200),
        ("budget", "民宿/青旅", 0, 100),
    ]

    selected = []
    seen_ids = set()
    for tier_key, tier_label, lo, hi in tiers:
        candidates = [(h, c) for h, c in hotels_with_price if lo <= c < hi and h.get("shop_id", "") not in seen_ids]
        candidates.sort(key=lambda x: x[0].get("rating", 0) or 0, reverse=True)
        if candidates:
            h, cost = candidates[0]
            seen_ids.add(h.get("shop_id", ""))
            selected.append(_format_hotel_response(h, cost, tier_key, tier_label, search_lat, search_lng))

    # 如果某些层级没有，用未用的酒店补足到 5 家
    remaining = [(h, c) for h, c in hotels_with_price if h.get("shop_id", "") not in seen_ids]
    remaining.sort(key=lambda x: x[0].get("rating", 0) or 0, reverse=True)
    for h, cost in remaining:
        if len(selected) >= 5:
            break
        if h.get("shop_id", "") in seen_ids:
            continue
        seen_ids.add(h.get("shop_id", ""))
        tier = _guess_tier(cost)
        selected.append(_format_hotel_response(h, cost, tier[0], tier[1], search_lat, search_lng))

    # 策略名映射
    strategy_labels = {
        "sustained": "🟢 不换房",
        "switch": "🔵 推荐换房",
    }

    return jsonify({
        "strategy": strategy,
        "strategy_label": strategy_labels.get(strategy, strategy),
        "strategy_reason": dec.get("strategy_reasoning",
                          f"疲劳度 {int(dec['fatigue']*100)}%, "
                          f"单日省时 {dec.get('time_saved_single', 0):.0f}min, "
                          f"累计省时 {dec.get('time_saved_cumulative', 0):.0f}min"),
        "fatigue_pct": int(dec["fatigue"] * 100),
        "fatigue_label": dec.get("fatigue_label", ""),
        "search_center": {"lat": search_lat, "lng": search_lng},
        "search_radius": search_radius,
        "hotels": selected,
        "day_index": day_idx,
    })


def _extract_hotel_price(hotel: dict) -> float:
    """从高德返回的酒店数据中提取价格（人均/起价）。
    优先 biz_ext.cost，其次 signature_dishes 中的 price。
    """
    # 尝试从 signature_dishes 提取 cost
    dishes = hotel.get("signature_dishes", []) or []
    costs = []
    for d in dishes:
        c = d.get("price", 0)
        if isinstance(c, (int, float)) and c > 0:
            costs.append(float(c))
    if costs:
        return min(costs)  # 取最低价格作为起价
    # 酒店类默认价格的合理估计（无法从API获取时按评分估算）
    rating = hotel.get("rating", 0) or 0
    if rating >= 4.5:
        return 500
    elif rating >= 4.0:
        return 300
    elif rating >= 3.0:
        return 150
    else:
        return 80


def _guess_tier(cost: float) -> tuple:
    """根据价格猜测档次"""
    if cost >= 500:
        return ("luxury", "高档")
    elif cost >= 200:
        return ("mid", "中档")
    elif cost >= 100:
        return ("economy", "经济")
    else:
        return ("budget", "民宿/青旅")


def _format_hotel_response(hotel: dict, cost: float, tier_key: str,
                            tier_label: str, center_lat: float, center_lng: float) -> dict:
    """格式化酒店响应数据"""
    import math
    def _haversine_m(lat1, lng1, lat2, lng2):
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lng2 - lng1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    dist = hotel.get("distance") or _haversine_m(
        center_lat, center_lng,
        float(hotel.get("lat", center_lat)), float(hotel.get("lng", center_lng))
    )
    return {
        "shop_id": hotel.get("shop_id", ""),
        "name": hotel.get("name", "未知酒店"),
        "rating": hotel.get("rating", 0) or 0,
        "price_level": tier_key,
        "price_label": tier_label,
        "price_range": f"¥{int(cost)}起" if cost > 0 else "价格未知",
        "address": hotel.get("address", "") or hotel.get("business_area", "") or "",
        "phone": hotel.get("phone", "") or "",
        "distance": round(dist, 0) if dist else 0,
        "opentime": hotel.get("opentime", "未知"),
        "lat": hotel.get("lat", center_lat),
        "lng": hotel.get("lng", center_lng),
    }


@app.route("/api/select_hotel", methods=["POST"])
def api_select_hotel():
    """用户为某天选择酒店。
    请求: {day_index: 0, hotel: {shop_id, name, address, ...}}
    返回: {status: "OK", day_index: 0, hotel_name: "..."}
    """
    data = request.get_json(silent=True) or {}
    day_idx = int(data.get("day_index", 0))
    hotel = data.get("hotel", {}) or {}

    days = session_state.get("days", [])
    if day_idx < 0 or day_idx >= len(days):
        return jsonify({"error": f"无效的天索引: {day_idx}"}), 400

    hotel_name = hotel.get("name", "")
    hotel_address = hotel.get("address", "")

    # ── 更新 session_state 中的 schedule_result ──
    day = days[day_idx]
    schedule = day.get("schedule_result", {})
    timeline = schedule.get("timeline", day.get("timeline", []))

    updated = False
    for node in timeline:
        if node.get("action") in ("HOTEL_PENDING", "HOTEL_CHECKIN"):
            node["action"] = "HOTEL_CHECKIN"
            node["memo"] = f"🏨 {hotel_name}"
            node["shop_id"] = hotel.get("shop_id", "")
            node["category"] = "hotel"
            node["opentime"] = hotel.get("opentime", "未知")
            if hotel_address:
                node["detail"] = hotel_address
            updated = True
            break

    if updated:
        schedule["timeline"] = timeline
        day["schedule_result"] = schedule
        # 同时更新 day 级别的 hotel_info
        if "hotel_info" not in day:
            day["hotel_info"] = {}
        day["hotel_info"]["selected_hotel"] = {
            "shop_id": hotel.get("shop_id", ""),
            "name": hotel_name,
            "address": hotel_address,
            "rating": hotel.get("rating", 0),
            "price_label": hotel.get("price_label", ""),
            "phone": hotel.get("phone", ""),
        }
        session_state["days"] = days
        print(f"[select_hotel] Day {day_idx}: 选择酒店 '{hotel_name}'", flush=True)

    return jsonify({
        "status": "OK",
        "day_index": day_idx,
        "hotel_name": hotel_name,
        "timeline": timeline,  # 返回更新后的时间线供前端刷新
    })


@app.route("/api/switch_day", methods=["POST"])
def switch_day():
    """切换活跃天视图。
    请求: {day_index: 1}
    """
    data = request.get_json(silent=True) or {}
    day_index = int(data.get("day_index", 0))

    days = session_state.get("days", [])
    if day_index < 0 or day_index >= len(days):
        return jsonify({"status": "ERROR", "message": f"无效的天索引: {day_index}"}), 400

    session_state["active_day_index"] = day_index
    day_data = days[day_index]

    return jsonify({
        "status": "SUCCESS",
        "active_day_index": day_index,
        "day_data": {
            "day_index": day_data.get("day_index"),
            "label": day_data.get("label", f"第{day_index+1}天"),
            "selected_pairs": day_data.get("selected_pairs", []),
            "schedule_result": day_data.get("schedule_result"),
            "chat_history": day_data.get("chat_history", []),
        },
        "days_count": len(days),
    })


@app.route("/api/move_to_day", methods=["POST"])
def move_to_day():
    """跨天移动 POI，触发两天重排。
    请求: {shop_id: "B0001", from_day: 0, to_day: 1}
    """
    data = request.get_json(silent=True) or {}
    shop_id = data.get("shop_id", "")
    from_day = int(data.get("from_day", 0))
    to_day = int(data.get("to_day", 0))

    days = session_state.get("days", [])

    if from_day >= len(days) or to_day >= len(days):
        return jsonify({"status": "ERROR", "message": "无效的天索引"}), 400

    # 从源天移除
    from_pairs = days[from_day].get("selected_pairs", [])
    moved_pair = None
    new_from_pairs = []
    for pair in from_pairs:
        if pair[1] == shop_id:
            moved_pair = pair
        else:
            new_from_pairs.append(pair)

    if moved_pair is None:
        return jsonify({"status": "ERROR", "message": f"在第{from_day+1}天中找不到店铺: {shop_id}"}), 404

    days[from_day]["selected_pairs"] = new_from_pairs

    # 添加到目标天
    days[to_day].setdefault("selected_pairs", []).append(moved_pair)

    # 重排两天（简单版：只更新数据，实际重排由前端触发）
    session_state["days"] = days

    return jsonify({
        "status": "SUCCESS",
        "moved_shop": {"shop_id": shop_id, "name": moved_pair[2] if len(moved_pair) > 2 else shop_id},
        "from_day": from_day,
        "to_day": to_day,
        "message": f"已将{moved_pair[2] if len(moved_pair) > 2 else shop_id}移至第{to_day+1}天",
    })


@app.route("/api/set_trip_days", methods=["POST"])
def set_trip_days():
    """修改天数、切换单日/多日模式。
    请求: {mode: "multi", days: 3}
    """
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", session_state.get("trip_mode", "single"))
    days_count = int(data.get("days", session_state.get("trip_days", 2)))

    if mode not in ("single", "multi"):
        return jsonify({"status": "ERROR", "message": "mode 必须是 'single' 或 'multi'"}), 400

    old_mode = session_state.get("trip_mode", "single")

    if mode == "multi":
        # 切换到多日模式
        if old_mode == "single":
            # 迁移现有 selected_pairs 到候选池
            existing_pairs = session_state.get("selected_pairs", [])
            session_state["candidate_pool"] = []
            for cat, sid, sname in existing_pairs:
                session_state["candidate_pool"].append({
                    "category": cat,
                    "shop_id": sid,
                    "name": sname,
                    "coord": "",
                })
            # 清理单日数据
            session_state["selected_pairs"] = []
            session_state["task_list"] = []
            session_state["spatial_matrix"] = {}

        session_state["trip_mode"] = "multi"
        session_state["trip_days"] = min(max(days_count, 1), 7)

        # 重建 days 数组
        new_days = []
        old_days = session_state.get("days", [])
        for i in range(session_state["trip_days"]):
            if i < len(old_days):
                new_days.append(old_days[i])
                new_days[i]["day_index"] = i
                new_days[i]["label"] = f"第{i+1}天"
            else:
                new_days.append({
                    "day_index": i,
                    "label": f"第{i+1}天",
                    "selected_pairs": [],
                    "task_list": [],
                    "spatial_matrix": {},
                    "schedule_result": None,
                    "chat_history": [],
                    "transport_override": None,
                })
        session_state["days"] = new_days
        session_state["active_day_index"] = 0

        return jsonify({
            "status": "SUCCESS",
            "trip_mode": "multi",
            "trip_days": session_state["trip_days"],
            "candidate_pool_size": len(session_state.get("candidate_pool", [])),
            "days_count": len(session_state.get("days", [])),
        })

    else:
        # 切换回单日模式
        # 合并所有天的 selected_pairs
        all_pairs = []
        for d in session_state.get("days", []):
            all_pairs.extend(d.get("selected_pairs", []))
        # 也合并候选池
        for item in session_state.get("candidate_pool", []):
            all_pairs.append((item.get("category", ""), item.get("shop_id", ""), item.get("name", "")))

        session_state["trip_mode"] = "single"
        session_state["trip_days"] = 1
        session_state["days"] = []
        session_state["candidate_pool"] = []
        session_state["selected_pairs"] = all_pairs
        session_state["active_day_index"] = 0

        return jsonify({
            "status": "SUCCESS",
            "trip_mode": "single",
            "merged_pairs_count": len(all_pairs),
            "message": f"已合并所有店铺到单日行程，共 {len(all_pairs)} 家",
        })


# ======================================================================
# 启动
# ======================================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"🚀 美团 AI 助手服务启动: http://localhost:{port}")
    _reset_session()
    _ensure_realtime_poller()
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
