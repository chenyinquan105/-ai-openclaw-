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

app = Flask(__name__, static_folder=os.path.join(base_dir, "static"))
CORS(app)

# ======================================================================
# 管家长期记忆 —— 偏好谱读写引擎
# ======================================================================
_MEMORY_PATH = os.path.join(base_dir, "管家记忆.md")


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


def _append_chat_message(role: str, content=None, tool_calls=None, tool_call_id=None, name=None):
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
}


def _reset_session():
    global agent, session_state
    # 复用已有 agent 避免重复初始化 OpenClaw Bridge（并发时可能阻塞）
    if agent is None:
        agent = backend.MeituanAgent(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com"
        )
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
    }
    # 清空虚拟时钟 session — gunicorn 模式下跳过（time_master 线程锁不兼容）
    # 原代码: tm = time_master.get_master(); tm.remove_session(_CLOCK_SESSION_ID)


def _duration(cat: str) -> int:
    return {"hair": 60, "pet": 30, "cafe": 20,
            "restaurant": 60, "gym": 60, "cinema": 120, "laundry": 30}.get(cat, 45)


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
        # ── 其他 ──
        "干洗": "laundry", "洗衣服": "laundry", "洗衣": "laundry",
        "健身": "gym", "瑜伽": "gym", "游泳": "gym", "锻炼": "gym",
        "电影": "cinema", "影院": "cinema", "电影院": "cinema", "看电影": "cinema",
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


def _search_poi(agent_instance, user_text: str, profile: dict = None) -> dict:
    """执行 LLM 解析 + POI 搜索，返回 (category_list, poi_data) 或错误。
    profile: 管家记忆偏好谱，用于注入口味/预算参数。

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
        print(f"[fast-path] 命中品类关键词: {_fast_categories}，跳过 LLM", flush=True)
        all_results = {}
        for cat in _fast_categories:
            search_res = backend.skill_poi.search_poi_matrix(
                center_coord="39.93,116.45",
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

        # 坐标兜底：LLM 可能传中文地名，直接 fallback 到三里屯默认坐标
        raw_coord = args.get("center_coord", "")
        coord = raw_coord
        if raw_coord:
            parts = raw_coord.strip().split(",")
            if len(parts) != 2:
                coord = "39.93,116.45"
            else:
                try:
                    float(parts[0].strip())
                    float(parts[1].strip())
                except ValueError:
                    coord = "39.93,116.45"
        if not coord or not coord.strip():
            coord = "39.93,116.45"

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
                center_coord="39.93,116.45",
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
@app.route("/api/edit_trip", methods=["POST"])
def api_edit_trip():
    """行程编辑端点：用户用自然语言微调已有行程。
    支持：改路线/换交通/增删目的地/调整时间/换偏好。
    """
    global agent, session_state
    data = request.get_json(silent=True) or {}
    edit_text = (data.get("text") or "").strip()
    if not edit_text:
        return jsonify({"error": "请输入编辑指令"}), 400

    if session_state.get("phase") != "done" and not session_state.get("selected_pairs"):
        return jsonify({"error": "没有活跃行程，请先发起一个行程"}), 400

    # 用 LLM 解析编辑意图
    current_plan_desc = ""
    for cat, sid, sname in session_state.get("selected_pairs", []):
        current_plan_desc += f"- {sname} ({cat})\n"

    edit_prompt = f"""当前行程：
{current_plan_desc}
用户编辑指令：{edit_text}

请判断用户意图，返回 JSON（只返回 JSON，不要其他文字）：
{{
  "action": "add_stop" | "remove_stop" | "reroute" | "change_time" | "change_transport",
  "params": {{}}
}}

意图说明：
- add_stop: 新增目的地。params: {{keywords: 搜索关键词, category: 品类或空}}
- remove_stop: 删除目的地。params: {{name: 要删的店名关键词}}
- reroute: 换路线。params: {{preference: fast|short|scenic|avoid_highway}}
- change_time: 调整时间。params: {{time: HH:MM 或 now 或 +30}}
- change_transport: 换交通方式。params: {{mode: WALK|TAXI|METRO|DRIVE}}"""

    edit_messages = [
        {"role": "system", "content": "你是行程编辑助手，解析用户编辑意图。只返回 JSON。"},
        {"role": "user", "content": edit_prompt}
    ]
    try:
        edit_resp = agent._call_llm(edit_messages, max_tokens=500)
        raw = (edit_resp.content or "").strip()
        # 提取 JSON（可能被 markdown 包裹）
        json_match = re.search(r'\{[^{}]*"action"[^{}]*\}', raw, re.DOTALL)
        if not json_match:
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            intent = json.loads(json_match.group(0))
        else:
            intent = {"action": "reroute", "params": {"preference": "fast"}}
    except Exception as e:
        print(f"[edit_trip] LLM 解析失败: {e}, raw={raw if 'raw' in dir() else 'N/A'}")
        return jsonify({"error": f"无法理解编辑指令: {str(e)}"}), 400

    action = intent.get("action", "reroute")
    params = intent.get("params", {})

    # ── 执行编辑 ──
    if action == "add_stop":
        # 搜索新目的地
        kw = params.get("keywords", edit_text)
        cat = params.get("category")
        try:
            new_res = backend.skill_poi.search_poi_matrix(
                center_coord="39.93,116.45",
                categories=[cat] if cat else ["restaurant"],
                radius_meters=5000,
                min_rating=3.5,
                keywords=kw if kw else None,
            )
            # 取第一个有结果的品类
            added = False
            for c, shops in new_res.get("search_results", {}).items():
                if shops:
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
        name_kw = params.get("name", "")
        before = len(session_state["selected_pairs"])
        session_state["selected_pairs"] = [
            (cat, sid, sname)
            for cat, sid, sname in session_state["selected_pairs"]
            if name_kw not in sname
        ]
        if len(session_state["selected_pairs"]) == before:
            return jsonify({"error": f"未找到含'{name_kw}'的目的地"}), 404
        if not session_state["selected_pairs"]:
            return jsonify({"error": "行程已清空，请重新发起"}), 400

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


# ── 排程辅助函数 ──

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
            for item in schedule_res["timeline"]:
                _schedule_nodes.append({
                    "time": item["time"],
                    "type": "SCHEDULE",
                    "node_id": item.get("task_id", ""),
                    "name": item.get("memo", ""),
                    "action": item.get("action", ""),
                    "target_location_id": item.get("target_location_id"),
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
                center_coord="39.93,116.45",
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
    返回 poi_cache 中所有 category 为 "cafe" 的店铺列表，按评分降序，最多5个。
    若 cache 中没有 cafe，则自动触发一次独立搜索补充到 cache。
    """
    if not agent or not agent.poi_cache:
        return jsonify({"error": "无店铺缓存"}), 400

    import math

    # 先检查 cache 里有没有 cafe
    has_cafe_in_cache = any(s.get("category") == "cafe" for s in agent.poi_cache.values())

    if not has_cafe_in_cache:
        # 自动触发 cafe 品类搜索，补充到 poi_cache
        try:
            extra = backend.skill_poi.search_poi_matrix(
                center_coord="39.93,116.45",
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
        except Exception as e:
            pass  # 搜索失败则继续用 cache 中已有的（可能为空）

    shops_data = []
    all_out_of_1km = True
    for sid, shop in agent.poi_cache.items():
        if shop.get("category") != "cafe":
            continue
        # 计算距离（haversine，中心坐标 39.93, 116.45）
        dist_m = 0
        raw_coord = shop.get("coord", "")
        if raw_coord and "," in raw_coord:
            try:
                slat, slng = float(raw_coord.split(",")[0].strip()), float(raw_coord.split(",")[1].strip())
                R = 6371000
                dlat = math.radians(slat - 39.93)
                dlng = math.radians(slng - 116.45)
                a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(39.93)) * math.cos(math.radians(slat)) * math.sin(dlng / 2) ** 2
                c = 2 * math.asin(math.sqrt(a))
                dist_m = int(R * c)
            except:
                pass
        if dist_m <= 1000:
            all_out_of_1km = False
        dist_str = f"{dist_m}m" if dist_m < 1000 else f"{dist_m / 1000:.1f}km"
        dist_km = round(dist_m / 1000, 1)
        shops_data.append({
            "shop_id": shop["shop_id"],
            "name": shop["name"],
            "rating": shop.get("rating", 0),
            "distance": dist_str,
            "distance_meters": dist_m,
            "distance_km": dist_km,
        })

    # 按评分降序
    shops_data.sort(key=lambda s: s["rating"], reverse=True)

    return jsonify({
        "shops": shops_data[:5],
        "all_out_of_1km": all_out_of_1km and len(shops_data) > 0,
        "total_found": len(shops_data),
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
    nodes = data.get("schedule_nodes", [])
    clock = tm.get_or_create_session(_CLOCK_SESSION_ID, initial_time=initial_time)
    # 保留现有 WATER/MED 提醒节点，合并前端传的非提醒节点
    existing = list(clock.schedule_nodes) if clock.schedule_nodes else []
    reminder_nodes = [n for n in existing if n.get("type") in ("WATER", "MED")]
    merged = reminder_nodes + [n for n in nodes if n.get("type") not in ("WATER", "MED")]
    tm.set_schedule(_CLOCK_SESSION_ID, merged, initial_time=initial_time)
    h, m = initial_time.split(":")
    clock.virtual_minutes = float(int(h) * 60 + int(m))
    clock.is_running = False
    session_state["clock_enabled"] = True
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
    # 筛选提醒类节点走管线处理（格式化 + SSE广播弹窗）
    reminder_nodes = [e for e in raw_events if isinstance(e, dict) and e.get("type") in ("WATER", "MED", "CUSTOM")]
    other_events = [e for e in raw_events if e not in reminder_nodes]
    if reminder_nodes:
        fake_res = {"ticked_minutes_list": [], "triggered_nodes": reminder_nodes}
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
    """时钟事件产生后，调用 reminder_skill 处理并注入事件队列"""
    ticked = res.get("ticked_minutes_list", [])
    events = res.get("triggered_nodes", [])
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
    # 始终调用 process_reminder_pipeline：
    # - 有新事件时：处理它们（响铃 + 状态初始化）
    # - 无新事件时：检查挂起事件是否超时（催促链）
    alerts = reminder_skill.process_reminder_pipeline(
        _CLOCK_SESSION_ID, ticked, events, time_master.get_master(),
        elder_name=elder_name,
        emergency_contact=emergency_contact,
    )
    # 将 reminder alerts 注入到 time_master 的事件队列
    # 前端通过 /api/clock/events 拉取后会渲染为交互式 Dialog
    _tm = time_master.get_master()
    for alert in alerts:
        _tm.push_triggered_event(_CLOCK_SESSION_ID, alert)

    # ——— 诊断日志 ———
    if events:
        app.logger.info(f'[ClockTrigger] raw_nodes={len(events)} types={[e.get("type") for e in events]}')
    if alerts:
        app.logger.info(f'[ClockTrigger] alerts={len(alerts)} types={[a.get("type") for a in alerts]} sse_clients={len(_sse_clients)}')

    # ——— SSE 广播：通知所有连接的客户端 ———
    _broadcast_sse_events(alerts if alerts else events)


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
    """
    data = request.get_json(silent=True) or {}
    med_id = data.get("med_id", "")
    action = data.get("action", "")
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
                "model": "deepseek-chat",
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
            "4. 健康作息(lifestyle): hydration_interval_minutes(分钟), medication_schedule\n\n"
            f"当前偏好: {json.dumps(current_profile, ensure_ascii=False)}\n\n"
            "只返回 JSON，格式: {\"detected_updates\": {...}}。如果无变化返回空对象。"
        ),
    }

    messages = [detect_prompt]
    if context:
        messages.extend(context)
    messages.append({"role": "user", "content": user_message})

    try:
        llm_msg = agent._call_llm(messages)
        content = llm_msg.content or "{}"
        result = json.loads(content)
        updates = result.get("detected_updates", {})
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
    start_coord = data.get("start_coord", "39.93,116.45")
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
    coord = data.get("coord", "39.93,116.45")
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


# ======================================================================
# 高德 POI API（真实数据检索 + 地理编码）
# ======================================================================
from skills.amap_poi.amap_poi import AmapPOIClient
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
            "description": "修改进行中的行程：增加/删除目的地、调整时间、更换交通方式、重新排序。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add_stop", "remove_stop", "change_time", "change_transport", "reroute"], "description": "操作类型"},
                    "params": {
                        "type": "object",
                        "properties": {
                            "keywords": {"type": "string", "description": "新增目的地关键词"},
                            "name": {"type": "string", "description": "要删除的店名"},
                            "time": {"type": "string", "description": "新时间 HH:MM"},
                            "mode": {"type": "string", "enum": ["WALK", "TAXI", "METRO", "BUS"], "description": "交通方式"},
                            "preference": {"type": "string", "enum": ["fast", "short", "scenic"]}
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
]


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
                result = _search_poi(agent, user_text, profile)
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
                    "data": {"categories": categories},
                    "message": f"为你找到{len(categories)}个品类共{total_shops}家店铺，请在面板中选择～"
                }
            except Exception as e:
                return {"status": "ERROR", "message": f"POI搜索失败: {str(e)}"}

        # ── 路线规划 ──
        elif tool_name == "plan_route":
            waypoints = arguments.get("waypoints", [])
            transport = arguments.get("transport_preference", "步行优先")
            start = arguments.get("start_coord", "39.93,116.45")
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
                wx = _skill_weather_extractor(loc)
                return {"status": "SUCCESS", "data": wx,
                        "message": f"{loc}当前天气: {wx.get('weather','?')}, {wx.get('temp','?')}°C"}
            except Exception as e:
                return {"status": "ERROR", "message": f"天气查询失败: {str(e)}"}

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
            # 复用 api/start 逻辑但简化
            _reset_session()
            agent.context_memory = []
            session_state["user_input"] = reqs
            session_state["transport"] = transport
            profile = _read_profile()
            session_state["_profile"] = profile
            result = _search_poi(agent, reqs, profile)
            if "error" in result:
                return {"status": "ERROR", "message": result["error"]}
            # 自动选top1
            auto_pairs = []
            for cat, shops in agent.poi_cache_per_category.items():
                if shops:
                    best = max(shops, key=lambda s: s.get("rating", 0))
                    auto_pairs.append((cat, best["shop_id"], best["name"]))
            if auto_pairs:
                session_state["selected_pairs"] = auto_pairs
                session_state["phase"] = "done"
                schedule = _run_schedule_from_session()
                if hasattr(schedule, 'get_json'):
                    schedule = schedule.get_json()
                return {"status": "SUCCESS", "data": schedule,
                        "message": f"行程已生成: {len(auto_pairs)}个目的地"}
            return {"status": "ERROR", "message": "未找到匹配目的地"}

        # ── 编辑行程 ──
        elif tool_name == "edit_trip":
            action = arguments.get("action", "reroute")
            params = arguments.get("params", {})
            # 复用现有的编辑逻辑
            if session_state.get("phase") != "done" and not session_state.get("selected_pairs"):
                return {"status": "ERROR", "message": "没有活跃行程"}

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
                kw = params.get("keywords", "")
                cat = params.get("category")
                if not kw:
                    return {"status": "ERROR", "message": "请提供搜索关键词"}
                try:
                    new_res = backend.skill_poi.search_poi_matrix(
                        center_coord="39.93,116.45",
                        categories=[cat] if cat else ["restaurant"],
                        radius_meters=5000,
                        min_rating=3.5,
                        keywords=kw
                    )
                    for c, shops in new_res.get("search_results", {}).items():
                        if shops:
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
                name = params.get("name", "")
                pairs = [(c, sid, sn) for c, sid, sn in session_state.get("selected_pairs", []) if name not in sn]
                if len(pairs) == len(session_state.get("selected_pairs", [])):
                    return {"status": "ERROR", "message": f"未找到 '{name}'"}
                session_state["selected_pairs"] = pairs
                result = _run_schedule_from_session()
                return {"status": "SUCCESS", "data": (result.get_json() if hasattr(result, 'get_json') else result),
                        "message": f"已移除 {name}"}

            return {"status": "ERROR", "message": f"不支持的操作: {action}"}

        # ── 取消行程 ──
        elif tool_name == "cancel_trip":
            _reset_session()
            session_state["phase"] = "init"
            return {"status": "SUCCESS", "message": "行程已取消"}

        # ── 获取行程状态 ──
        elif tool_name == "get_trip_status":
            pairs = session_state.get("selected_pairs", [])
            if not pairs:
                return {"status": "SUCCESS", "data": {"active": False},
                        "message": "当前没有活跃行程"}
            dests = [{"category": c, "name": n, "shop_id": sid} for c, sid, n in pairs]
            return {"status": "SUCCESS", "data": {
                "active": True,
                "destinations": dests,
                "transport": session_state.get("transport", "步行"),
                "time": session_state.get("fixed_time", "现在"),
                "phase": session_state.get("phase", "init")
            }, "message": f"当前行程: {len(dests)}个目的地"}

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
    profile_lines.append(f"用户的名字是{user_display}，你必须用这个名字自然称呼他，不要用笼统的「你」。")
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

    system_prompt = (
        "# ⚠️ 最重要规则（最高优先级）\n\n"
        "你有工具可以搜索真实商户数据。当用户想找/吃/喝/玩/去某类店铺时，**必须调用 search_poi 工具**。\n"
        "**绝对禁止**凭你的训练数据直接推荐具体店铺名称——你记忆中的店可能已关门、评分不准、距离未知。\n"
        "**绝对禁止**用文字回复代替工具调用。用户说「想吃火锅」→ 调 search_poi(keywords=\"火锅\")，而不是直接说「xx火锅店不错」。\n"
        "同样的，用户表达过敏/忌口/偏好 → 调 update_profile；用户要提醒/路线/叫车/天气 → 调对应工具。\n\n"
        "# 身份定义\n\n"
        f"你是「小美」，{user_display}的私人生活助理。你通过调用后台工具来帮他找到真实可靠的本地生活信息。\n"
        "你的温暖体现在语气上，你的可靠体现在「只推荐工具搜出来的真实数据」上。\n\n"
        f"# {user_display}的专属档案（搜索时会自动应用，你不用手动处理）\n\n"
        f"{profile_summary}\n\n"
        f"系统状态：{context_info}\n\n"
        "# 意图 → 工具映射\n\n"
        "用户想找店铺（吃/喝/玩/理发/宠物/电影/健身等）→ **必须调 search_poi**，keywords 从用户消息中提取品类关键词\n"
        "用户表达个人信息（过敏/忌口/口味/预算/出行偏好）→ 调 update_profile，同时给一句确认\n"
        "用户要出行（路线/叫车/多目的地）→ 调 start_trip / plan_route / hail_taxi / plan_transit\n"
        "用户要提醒（喝水/吃药/自定义）→ 调 add_reminder / remove_reminder / list_reminders\n"
        "用户查信息（天气/排队/行程）→ 调 check_weather / check_queue / get_trip_status\n"
        "纯问候/闲聊/情绪分享（无实质需求）→ 不调工具，温暖回应 1-3 句\n\n"
        "# 语气规则\n\n"
        f"- 自然称呼{user_display}，不用「您」\n"
        "- 适量用「呀」「嘛」「哈」「～」和 emoji（1-3个/条），不要刷屏\n"
        "- 工具搜索结果会以面板展示，你只需说一句简短引导（如「帮你找到了几家，看看～」），**不要**在文字里逐条列出店铺\n"
        "- 不要说自己是「AI」「助手」「机器人」「管家」——你是「小美」"
    )

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
        messages.append(m)
    # 添加当前用户消息
    messages.append({"role": "user", "content": message})

    # 保存用户消息
    _append_chat_message("user", content=message)

    def generate():
        nonlocal messages
        try:
            # 发送用户消息事件
            yield f"event: message\ndata: {json.dumps({'role': 'user', 'content': message}, ensure_ascii=False)}\n\n"

            # ═══════════════════════════════════════════════════════════════
            # ★ 快路径：用户消息含明确品类关键词 → 跳过 LLM 工具调用，直接搜索
            # ═══════════════════════════════════════════════════════════════
            _fast_cats = _try_fast_category_match(message)
            if _fast_cats:
                print(f"[chat-fast-path] 命中品类: {_fast_cats}，直接搜索", flush=True)
                try:
                    search_result = _search_poi(agent, message, profile)
                    if "error" not in search_result:
                        categories = _build_categories_for_frontend(agent, profile)
                        if categories:
                            session_state["searched_categories"] = search_result.get("categories", [])
                            session_state["_profile"] = profile
                            session_state["phase"] = "choose_shop"
                            total_shops = sum(len(c.get("shops", [])) for c in categories)

                            # 模拟 tool_call 事件（前端需要）
                            fake_tc_id = f"fast_{int(_time.time()*1000)}"
                            yield f"event: tool_call\ndata: {json.dumps({'id': fake_tc_id, 'name': 'search_poi', 'arguments': {'keywords': message, 'category': _fast_cats[0]}, 'status': 'started'}, ensure_ascii=False)}\n\n"

                            # 模拟搜索等待，让用户看到"正在检索..."过程
                            _time.sleep(1.0)

                            # 保存 assistant tool_calls 到历史
                            _append_chat_message("assistant", content=None, tool_calls=[{
                                "id": fake_tc_id, "type": "function",
                                "function": {"name": "search_poi", "arguments": json.dumps({"keywords": message, "category": _fast_cats[0]}, ensure_ascii=False)}
                            }])

                            # 发送 tool_result 事件（含品类数据，前端渲染面板）
                            result_data = {
                                "status": "SUCCESS",
                                "data": {"categories": categories},
                                "message": f"为你找到{len(categories)}个品类共{total_shops}家店铺，请在面板中选择～"
                            }
                            yield f"event: tool_result\ndata: {json.dumps({'id': fake_tc_id, 'name': 'search_poi', 'status': 'completed', 'result': result_data}, ensure_ascii=False)}\n\n"

                            # 将工具结果加入 messages
                            messages.append({
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [{
                                    "id": fake_tc_id, "type": "function",
                                    "function": {"name": "search_poi", "arguments": json.dumps({"keywords": message, "category": _fast_cats[0]}, ensure_ascii=False)}
                                }]
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": fake_tc_id,
                                "name": "search_poi",
                                "content": json.dumps(result_data, ensure_ascii=False)
                            })
                            _append_chat_message("tool", tool_call_id=fake_tc_id, name="search_poi", content=json.dumps(result_data, ensure_ascii=False))

                            # 固定简短引导语，不再调 LLM（面板已展示全部信息）
                            guide_msg = "帮你找到了几家，在下方面板里挑挑看～"
                            yield f"event: message\ndata: {json.dumps({'role': 'assistant', 'content': guide_msg}, ensure_ascii=False)}\n\n"
                            _save_chat_history(history_db)
                            _append_chat_message("assistant", content=guide_msg)
                            yield f"event: done\ndata: {json.dumps({'status': 'complete'}, ensure_ascii=False)}\n\n"
                            return
                except Exception as e:
                    print(f"[chat-fast-path] 搜索失败: {e}，回退到LLM路径", flush=True)
                    # 快路径失败 → 回退到正常 LLM 路径

            # ═══════════════════════════════════════════════════════════════
            # 搜索意图检测：快路径未命中，但消息仍像搜索请求 → 走高德API
            # ═══════════════════════════════════════════════════════════════
            _search_intent_kw = ["找", "搜", "附近", "有没有", "哪里有", "帮我", "推荐",
                                 "想去", "想吃", "想喝", "想买", "求", "查一下", "看看"]
            _non_search_patterns = ["你好", "嗨", "早", "在吗", "再见", "谢谢", "你能",
                                    "你是谁", "叫什么", "干嘛", "功能", "能力"]
            _is_search = any(kw in message for kw in _search_intent_kw)
            _is_chat = any(kw in message for kw in _non_search_patterns)

            if _is_search and not _is_chat:
                print(f"[chat-search-intent] 检测到搜索意图: {message[:50]}，走高德API", flush=True)
                try:
                    search_result = _search_poi(agent, message, profile)
                    if "error" not in search_result:
                        categories = _build_categories_for_frontend(agent, profile)
                        if categories:
                            session_state["searched_categories"] = search_result.get("categories", [])
                            session_state["_profile"] = profile
                            session_state["phase"] = "choose_shop"
                            total_shops = sum(len(c.get("shops", [])) for c in categories)

                            fake_tc_id = f"search_{int(_time.time()*1000)}"
                            yield f"event: tool_call\ndata: {json.dumps({'id': fake_tc_id, 'name': 'search_poi', 'arguments': {'keywords': message}, 'status': 'started'}, ensure_ascii=False)}\n\n"

                            # 模拟搜索等待，让用户看到"正在检索..."过程
                            _time.sleep(1.0)

                            _append_chat_message("assistant", content=None, tool_calls=[{
                                "id": fake_tc_id, "type": "function",
                                "function": {"name": "search_poi", "arguments": json.dumps({"keywords": message}, ensure_ascii=False)}
                            }])

                            result_data = {
                                "status": "SUCCESS",
                                "data": {"categories": categories},
                                "message": f"为你找到{len(categories)}个品类共{total_shops}家店铺，请在面板中选择～"
                            }
                            yield f"event: tool_result\ndata: {json.dumps({'id': fake_tc_id, 'name': 'search_poi', 'status': 'completed', 'result': result_data}, ensure_ascii=False)}\n\n"

                            messages.append({
                                "role": "assistant", "content": None,
                                "tool_calls": [{"id": fake_tc_id, "type": "function",
                                    "function": {"name": "search_poi", "arguments": json.dumps({"keywords": message}, ensure_ascii=False)}}]
                            })
                            messages.append({"role": "tool", "tool_call_id": fake_tc_id, "name": "search_poi",
                                "content": json.dumps(result_data, ensure_ascii=False)})
                            _append_chat_message("tool", tool_call_id=fake_tc_id, name="search_poi",
                                content=json.dumps(result_data, ensure_ascii=False))

                            guide_msg = "帮你找到了几家，在下方面板里挑挑看～"
                            yield f"event: message\ndata: {json.dumps({'role': 'assistant', 'content': guide_msg}, ensure_ascii=False)}\n\n"
                            _save_chat_history(history_db)
                            _append_chat_message("assistant", content=guide_msg)
                            yield f"event: done\ndata: {json.dumps({'status': 'complete'}, ensure_ascii=False)}\n\n"
                            return
                except Exception as e:
                    print(f"[chat-search-intent] 搜索失败: {e}，回退到LLM路径", flush=True)

            # ═══════════════════════════════════════════════════════════════
            # 正常路径：LLM 处理（非搜索意图的闲聊/问候/情绪等）
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

                    # ★ 保存assistant的tool_calls消息到历史（必须在tool结果之前）
                    _tool_calls_for_history = []
                    for tc in current_tool_calls:
                        _tool_calls_for_history.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}
                        })
                    _append_chat_message("assistant", content=None, tool_calls=_tool_calls_for_history)

                    for tc in current_tool_calls:
                        tool_name = tc["name"]
                        tool_args = tc["arguments"]

                        # 执行工具
                        result = _execute_chat_tool(tool_name, tool_args)

                        # 发送工具结果
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc['id'], 'name': tool_name, 'status': 'completed' if result.get('status') == 'SUCCESS' else 'failed', 'result': result}, ensure_ascii=False)}\n\n"

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
                            # ★ 保存assistant的tool_calls到历史
                            _append_chat_message("assistant", content=None, tool_calls=[{
                                "id": payload2["id"],
                                "type": "function",
                                "function": {"name": payload2["name"], "arguments": json.dumps(payload2["arguments"], ensure_ascii=False)}
                            }])
                            # 执行并返回
                            tc_result = _execute_chat_tool(payload2["name"], payload2["arguments"])
                            yield f"event: tool_result\ndata: {json.dumps({'id': payload2['id'], 'name': payload2['name'], 'status': 'completed' if tc_result.get('status') == 'SUCCESS' else 'failed', 'result': tc_result}, ensure_ascii=False)}\n\n"
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
                                        _append_chat_message("assistant", content=assistant_full_response.strip())
                        elif evt2 == "done":
                            # 保存最终的assistant回复（两层tool调用后）
                            if assistant_full_response.strip():
                                _append_chat_message("assistant", content=assistant_full_response.strip())

                elif evt == "error":
                    yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

                elif evt == "done":
                    # 保存最终assistant回复（如果没有工具调用）
                    if assistant_full_response.strip() and not current_tool_calls:
                        _append_chat_message("assistant", content=assistant_full_response.strip())

            # 发送完成事件
            chat_session_id = history_db.get("active_session", "chat_000")
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
# 启动
# ======================================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"🚀 美团 AI 助手服务启动: http://localhost:{port}")
    _reset_session()
    _ensure_realtime_poller()
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
