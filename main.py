import json
import os
import sys
import re
import difflib
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

base_dir = os.path.dirname(os.path.abspath(__file__))
skills_path = os.path.join(base_dir, "skills")
if skills_path not in sys.path:
    sys.path.append(skills_path)

try:
    from skills import generic_poi_searcher as skill_poi
    from skills import concurrent_pipeline_scheduler as skill_scheduler
except ImportError as e:
    print(f"导入 Skill 失败: {e}")
    sys.exit(1)

from openai import OpenAI


# ======================================================================
# CATEGORY_MAP —— 品类映射硬编码
# ======================================================================
CATEGORY_MAP = {
    "理发": "hair", "剪头": "hair", "美发": "hair", "沙宣": "hair",
    "狗洗澡": "pet", "宠物洗澡": "pet", "宠物": "pet", "宠物店": "pet",
    "库迪": "pet", "酷迪": "pet",
    "咖啡": "cafe",
    "健身": "gym",
    "吃饭": "restaurant", "餐饮": "restaurant", "餐厅": "restaurant",
    "电影": "cinema", "影院": "cinema",
    "洗衣": "laundry", "干洗": "laundry",
}

# default location
DEFAULT_COORD = "39.93,116.45"


# ======================================================================
# LLM 工具定义 —— 唯一一个工具，只做意图解析
# ======================================================================
INTENT_PARSE_TOOL = [{
    "type": "function",
    "function": {
        "name": "parse_user_intent",
        "description": "解析用户的行程意图，输出结构化任务列表",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_type": {
                                "type": "string",
                                "description": "任务品类。映射规则：理发/剪头/美发/沙宣→hair, 库迪/酷迪/宠物洗澡/狗洗澡/宠物/宠物店→pet, 咖啡→cafe, 健身→gym, 吃饭/餐厅/餐饮→restaurant, 电影/影院→cinema, 洗衣/干洗→laundry"
                            },
                            "shop_name_hint": {
                                "type": "string",
                                "description": "用户提到的店铺名关键词（如'沙宣'），没有则填空字符串"
                            }
                        },
                        "required": ["task_type"]
                    }
                },
                "time_desc": {
                    "type": "string",
                    "description": "用户说的原始时间描述原文，如'现在''明天下午3点''周五上午'，没有则填'现在'"
                }
            },
            "required": ["tasks", "time_desc"]
        }
    }
}]


# ======================================================================
# 辅助函数
# ======================================================================

def find_best_match(query_name: str, poi_cache: dict) -> str:
    """ID 语义纠偏引擎"""
    if not poi_cache:
        return "loc_current"
    name_to_id = {info.get("name", ""): sid for sid, info in poi_cache.items()
                  if info.get("name")}
    if query_name in name_to_id:
        return name_to_id[query_name]
    matches = difflib.get_close_matches(query_name, name_to_id.keys(), n=1, cutoff=0.5)
    if matches:
        return name_to_id[matches[0]]
    for name, sid in name_to_id.items():
        if query_name in name or name in query_name:
            return sid
    best = max(poi_cache.values(), key=lambda v: v.get("rating", 0))
    return best["shop_id"]


def match_shop_to_category(hint: str, poi_cache: dict, category: str) -> tuple:
    """
    在 poi_cache 的指定品类中匹配店铺名
    返回 (shop_id, shop_name) 或 (None, None)
    """
    candidates = {sid: info for sid, info in poi_cache.items()
                  if info.get("category") == category}
    if not candidates:
        return None, None

    name_to_id = {info.get("name", ""): sid for sid, info in candidates.items()
                  if info.get("name")}

    # 1) 精确匹配
    if hint in name_to_id:
        sid = name_to_id[hint]
        return sid, candidates[sid]["name"]

    # 2) 部分匹配
    if hint:
        for name, sid in name_to_id.items():
            if hint in name or name in hint:
                return sid, candidates[sid]["name"]

    # 3) 评分最高
    best = max(candidates.values(), key=lambda v: v.get("rating", 0))
    return best["shop_id"], best["name"]


def search_poi_for_categories(categories: list) -> dict:
    """直接调 Skill 1 搜索 POI"""
    cats = list(set(categories))
    print(f"[*] 检索 {cats} 品类中...")
    res = skill_poi.search_poi_matrix(
        center_coord=DEFAULT_COORD,
        categories=cats,
        radius_meters=3000,
        min_rating=0
    )
    return res


def resolve_time(user_text: str, llm_time_desc: str) -> tuple:
    """
    解析时间，返回 (time_mode, fixed_time_str)
    time_mode: "now" | "fixed"
    """
    full = (user_text + " " + llm_time_desc).lower()
    has_now = bool(re.search(r"现在|立即|马上|当前|立刻|现在就出发|默认|立即出发", full))
    has_specific = bool(re.search(
        r"\d{1,2}[：:时点]|\d{1,2}:\d{2}|上午\d|下午\d|明天.*\d|"
        r"周[一二三四五六日天].*\d|星期.*\d",
        full
    ))
    if has_now or not has_specific:
        return "now", None

    m = re.search(r"(\d{1,2})[：:时点](\d{0,2})", full)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        return "fixed", f"{h:02d}:{mi:02d}"
    return "now", None


def build_default_duration(category: str) -> int:
    return {
        "hair": 60,
        "pet": 30,
        "cafe": 20,
        "restaurant": 60,
        "gym": 60,
        "cinema": 120,
        "laundry": 30,
    }.get(category, 45)


def format_output(res: dict):
    print("\n" + "★" * 25)
    print(f"🏆 时间最优解生成 (建议 {res['suggested_departure_time']} 出发)")
    for item in res["timeline"]:
        print(f"[{item['time']}] {item['memo']}")
    print("★" * 25 + "\n")


# ======================================================================
# 主流程
# ======================================================================

def main():
    print("=== 美团 AI 智能助手 ===")

    client = OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com"
    )
    model = "deepseek-chat"

    # ----------------------------------------------------------------
    # 1. 用户输入
    # ----------------------------------------------------------------
    user_input = input("用户: ")

    # ----------------------------------------------------------------
    # 2. LLM 意图解析 —— 仅此一步依赖 LLM
    # ----------------------------------------------------------------
    print("[*] 正在解析意图...")
    messages = [
        {"role": "system",
         "content": "你是一个意图解析器。请调用 parse_user_intent 将用户需求转为结构化数据，不要回复文字。"},
        {"role": "user", "content": user_input}
    ]

    msg = None
    for attempt in range(5):
        msg = client.chat.completions.create(
            model=model, messages=messages,
            tools=INTENT_PARSE_TOOL, tool_choice="auto"
        ).choices[0].message
        if msg.tool_calls:
            break
        messages.append({"role": "assistant", "content": msg.content or ""})
        messages.append({"role": "user",
                         "content": "请调用 parse_user_intent 工具，不要用文字回答。"})

    if not msg or not msg.tool_calls:
        print("AI: 意图解析失败。")
        return

    args = json.loads(msg.tool_calls[0].function.arguments)
    tasks = args.get("tasks", [])
    time_desc = args.get("time_desc", "")

    if not tasks:
        print("AI: 未能识别出有效任务。")
        return

    # ----------------------------------------------------------------
    # 3. 品类映射 → 搜索 POI
    # ----------------------------------------------------------------
    categories = [CATEGORY_MAP.get(t["task_type"], t["task_type"]) for t in tasks]
    poi_res = search_poi_for_categories(categories)

    poi_cache = {}
    if poi_res.get("status") == "SUCCESS":
        for cat in poi_res["search_results"]:
            for shop in poi_res["search_results"][cat]:
                poi_cache[shop["shop_id"]] = shop
    else:
        print(f"AI: 搜索失败: {poi_res.get('message')}")
        return

    # ----------------------------------------------------------------
    # 4. 店铺匹配（规则驱动）
    # ----------------------------------------------------------------
    selected_pairs = []
    for t in tasks:
        raw_type = t["task_type"]
        cat = CATEGORY_MAP.get(raw_type, raw_type)
        hint = t.get("shop_name_hint", "")
        sid, sname = match_shop_to_category(hint, poi_cache, cat)
        if not sid:
            print(f"[!] 品类 {cat} 未找到匹配店铺")
            continue
        selected_pairs.append((cat, sid, sname))

    if not selected_pairs:
        print("AI: 未能匹配到合适的店铺。")
        return

    # ----------------------------------------------------------------
    # 5. 展示确认
    # ----------------------------------------------------------------
    print("\n--- 已匹配到以下商户 ---")
    for cat, sid, sname in selected_pairs:
        info = poi_cache[sid]
        print(f"  • {sname} (★{info.get('rating', '-')})")

    time_mode, fixed_time = resolve_time(user_input, time_desc)
    if time_mode == "now":
        print(f"  ⏰ 立即出发")
    elif fixed_time:
        print(f"  ⏰ 预定时间: {fixed_time}")

    confirm = input("\n以上方案确认？(回车确认 / n 重输): ")
    if confirm.strip().lower() == "n":
        print("AI: 请重新输入。")
        return

    # ----------------------------------------------------------------
    # 6. 构建排程输入并执行
    # ----------------------------------------------------------------
    print("[*] 正在执行并发排程...")
    task_list = []
    spatial_matrix = {
        "locations": {
            "loc_current": {"name": "当前起点", "coord": DEFAULT_COORD}
        },
        "routes": {}
    }

    for cat, sid, sname in selected_pairs:
        info = poi_cache[sid]
        coord = f"{info.get('lat', 39.93)},{info.get('lng', 116.45)}"
        task_list.append({
            "task_id": sid,
            "name": sname,
            "location_id": sid,
            "duration_minutes": build_default_duration(cat),
            "human_needed": info.get("human_needed", True),
            "fixed_start_time": fixed_time if time_mode == "fixed" else None,
        })
        spatial_matrix["locations"][sid] = {"name": sname, "coord": coord}

    now_str = datetime.now().strftime("%H:%M")
    confirmed_ids, rejected_ids = [], []

    while True:
        schedule_res = skill_scheduler.solve_concurrent_timeline(
            task_list, spatial_matrix, now_str, confirmed_ids, rejected_ids
        )
        if schedule_res.get("status") == "CONFIRM_REQUIRED":
            print(f"\n⚠️ 冲突预案激活: {schedule_res['message']}")
            choice = input("AI 请示：1.接受延误 2.任务延后: ")
            if choice == "1":
                confirmed_ids.append(schedule_res["conflict_task"]["task_id"])
            else:
                rejected_ids.append(schedule_res["conflict_task"]["task_id"])
            continue
        elif schedule_res.get("status") == "SUCCESS":
            format_output(schedule_res)
            break
        else:
            print(f"AI: 规划失败：{schedule_res.get('message')}")
            break


if __name__ == "__main__":
    main()
