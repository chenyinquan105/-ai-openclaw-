import json
import os
import sys
import re
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
import difflib

# ======================================================================
# 核心组件：语义映射与纠偏
# ======================================================================
CATEGORY_MAP = {
    "理发": "hair", "剪头": "hair", "理发店": "hair", "美发": "hair",
    "狗洗澡": "pet", "宠物店": "pet", "给宠物洗澡": "pet",
    "干洗": "laundry", "洗衣服": "laundry", "咖啡": "cafe", "健身": "gym"
}

CATEGORY_NAME_CN = {
    "hair": "理发",
    "pet": "宠物洗澡",
    "cafe": "咖啡",
    "gym": "健身",
    "restaurant": "餐饮",
    "cinema": "电影",
    "laundry": "干洗",
}

def find_best_match(query_name: str, poi_cache: dict) -> str:
    """【ID 语义纠偏引擎】保证模型提到的店名能对准数据库 ID"""
    if not poi_cache: return "loc_current"
    name_to_id = {info.get("name", ""): sid for sid, info in poi_cache.items() if info.get("name")}
    if query_name in name_to_id: return name_to_id[query_name]
    matches = difflib.get_close_matches(query_name, name_to_id.keys(), n=1, cutoff=0.5)
    if matches: return name_to_id[matches[0]]
    for name, sid in name_to_id.items():
        if query_name in name or name in query_name: return sid
    return list(poi_cache.keys())[0] if poi_cache else "loc_current"

# 确保能找到 skills 文件夹下的模块
base_dir = os.path.dirname(os.path.abspath(__file__))
skills_path = os.path.join(base_dir, "skills")
if skills_path not in sys.path: sys.path.append(skills_path)

try:
    from skills import generic_poi_searcher as skill_poi
    from skills import concurrent_pipeline_scheduler as skill_scheduler
except ImportError as e:
    print(f"❌ 导入 Skill 失败: {e}")
    sys.exit(1)

from openai import OpenAI

class MeituanAgent:
    def __init__(self, api_key: str, base_url: str):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = "deepseek-chat"
        self.context_memory = []
        self.poi_cache = {}

    def _call_llm(self, messages: list, tools: list = None):
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None
        )
        return response.choices[0].message

    def _show_category_top3_and_choose(self, category: str, top3_text: str) -> str:
        """
        展示该品类 top 3 店铺，让用户键盘选择。
        返回用户选中的 shop_id。
        如果用户直接回车，返回 None 表示跳过/不选。
        """
        shops = self.poi_cache_per_category.get(category, [])
        if not shops:
            return None
        # 按评分降序
        sorted_shops = sorted(shops, key=lambda s: s.get("rating", 0), reverse=True)
        top_n = sorted_shops[:3]

        cn = CATEGORY_NAME_CN.get(category, category)
        print(f"\n  📋 {cn} 推荐（前3家）：")
        print(f"  {' 店名':<20} {'评分':<5} {'序号'}")
        print(f"  {'-'*30}")
        for i, shop in enumerate(top_n, 1):
            print(f"  {i}. {shop.get('name',''):<18} ★{shop.get('rating','-')}")
        print(f"  0. 跳过（不选此品类）")

        while True:
            choice = input(f"  请选择（1/{len(top_n)}/0，直接回车默认第1家）: ").strip()
            if choice == "":
                selected = top_n[0]
                print(f"  → 已选: {selected.get('name')}")
                return selected["shop_id"]
            if choice == "0":
                return None
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(top_n):
                    selected = top_n[idx]
                    print(f"  → 已选: {selected.get('name')}")
                    return selected["shop_id"]
                else:
                    print(f"  ⚠️ 请输入 1-{len(top_n)} 或 0")
            except ValueError:
                # 允许直接输入店名
                matched = [s for s in top_n if choice in s.get("name", "")]
                if matched:
                    print(f"  → 已选: {matched[0].get('name')}")
                    return matched[0]["shop_id"]
                print(f"  ⚠️ 请输入 1-{len(top_n)}、0、或完整店名")

    def run(self):
        print("=== 美团 AI 智能助手 ===")
        user_input = input("用户: ")

        # --- 阶段 1: 需求解析与 POI 搜索 ---
        system_prompt_1 = {
            "role": "system",
            "content": "你是一个生活秘书。第一步必须调用 search_poi 搜索各品类商户。刻画品类映射规则：理发/美发/沙宣→hair，宠物/狗/猫/洗澡/宠物店→pet，咖啡→cafe，健身→gym，餐饮/吃饭/餐厅→restaurant，电影/影院→cinema，洗衣/干洗→laundry。"
        }
        self.context_memory.append(system_prompt_1)
        self.context_memory.append({"role": "user", "content": user_input})

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

        msg = self._call_llm(self.context_memory, tools=tools_poi)

        # 阶段 1 重试
        retry_p1 = 0
        while not msg.tool_calls and retry_p1 < 5:
            retry_p1 += 1
            self.context_memory.append({"role": "assistant", "content": msg.content or ""})
            self.context_memory.append({"role": "user", "content": "请调用 search_poi 工具搜索对应品类商户，不要用文字回答。"})
            msg = self._call_llm(self.context_memory, tools=tools_poi)

        if not msg.tool_calls:
            print("AI: 搜索失败，LLM 未调用搜索工具。")
            return

        self.context_memory.append(msg)
        for tool_call in msg.tool_calls:
            args = json.loads(tool_call.function.arguments)
            raw_cats = args.get("categories", [])
            mapped_cats = list(set([CATEGORY_MAP.get(c, c) for c in raw_cats]))

            print(f"[*] 检索 {mapped_cats} 品类中...")
            search_res = skill_poi.search_poi_matrix(
                center_coord=args.get("center_coord", "39.93,116.45"),
                categories=mapped_cats,
                radius_meters=args.get("radius_meters", 3000),
                min_rating=args.get("min_rating", 0)
            )

            if search_res.get("status") == "SUCCESS":
                for cat in search_res["search_results"]:
                    for shop in search_res["search_results"][cat]:
                        self.poi_cache[shop["shop_id"]] = shop
            self.context_memory.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(search_res)})

        # ---------- 按品类分组，供后面交互选择使用 ----------
        self.poi_cache_per_category = {}
        for sid, shop in self.poi_cache.items():
            cat = shop.get("category")
            self.poi_cache_per_category.setdefault(cat, []).append(shop)

        # --- 阶段 2: 代码展示 top 3 → 用户选店（替换原来的 LLM 推荐环节） ---
        # 找出这次实际搜索的品类
        searched_categories = list(self.poi_cache_per_category.keys())

        selected_pairs = []  # [(category, shop_id, shop_name), ...]
        print("\n--- 请为每个品类选择店铺 ---")

        for cat in searched_categories:
            chosen_id = self._show_category_top3_and_choose(cat, "")
            if chosen_id:
                shop_info = self.poi_cache[chosen_id]
                selected_pairs.append((cat, chosen_id, shop_info["name"]))
                self.context_memory.append({
                    "role": "system",
                    "content": f"用户选择了 {cat} 品类店铺: {shop_info['name']}"
                })

        if not selected_pairs:
            print("AI: 未选择任何店铺。")
            return

        # --- 询问时间 ---
        # 先看看用户之前有没有提时间
        print("\n--- 时间确认 ---")
        has_time = bool(re.search(
            r"\d{1,2}[：:时点]|\d{1,2}:\d{2}|上午\d|下午\d|明天.*\d|周[一二三四五六日天].*\d|星期.*\d",
            user_input
        ))

        time_desc = ""
        if not has_time:
            time_desc = input("请问预计什么时间去？（直接回车默认现在出发）: ").strip()
        else:
            print("已从您之前的输入中识别到时间信息。")

        time_desc_full = user_input + " " + time_desc
        # 正则解析
        has_now = bool(re.search(r"现在|立即|马上|当前|立刻|现在就出发|默认", time_desc_full.lower()))
        has_specific = bool(re.search(
            r"\d{1,2}[：:时点]|\d{1,2}:\d{2}|上午\d|下午\d|明天.*\d|周[一二三四五六日天].*\d|星期.*\d",
            time_desc_full
        ))

        fixed_time = None
        if has_specific and not has_now:
            m = re.search(r"(\d{1,2})[：:时点](\d{0,2})", time_desc_full)
            if m:
                h, mi = int(m.group(1)), int(m.group(2) or 0)
                fixed_time = f"{h:02d}:{mi:02d}"

        # --- 阶段 3: 执行并发排程 ---
        print("[*] 正在执行并发排程...")

        # 按品类定默认时长
        def _duration(cat):
            return {"hair": 60, "pet": 30, "cafe": 20,
                    "restaurant": 60, "gym": 60, "cinema": 120, "laundry": 30}.get(cat, 45)

        task_list = []
        spatial_matrix = {
            "locations": {"loc_current": {"name": "当前起点", "coord": "39.93,116.45"}},
            "routes": {}
        }

        for cat, sid, sname in selected_pairs:
            info = self.poi_cache.get(sid, {})
            coord = f"{info.get('lat', 39.93)},{info.get('lng', 116.45)}"
            human_needed = info.get("human_needed", True)

            task_list.append({
                "task_id": sid,
                "name": sname,
                "location_id": sid,
                "duration_minutes": _duration(cat),
                "human_needed": human_needed,
                "fixed_start_time": fixed_time,
            })
            spatial_matrix["locations"][sid] = {
                "name": sname,
                "coord": coord
            }

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
                self.format_final_output(schedule_res)
                break
            else:
                print(f"AI: 规划失败：{schedule_res.get('message')}")
                break

    def format_final_output(self, res):
        print("\n" + "★"*25)
        print(f"🏆 时间最优解生成 (建议 {res['suggested_departure_time']} 出发)")
        for item in res["timeline"]:
            print(f"[{item['time']}] {item['memo']}")
        print("★"*25 + "\n")

if __name__ == "__main__":
    agent = MeituanAgent(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
    agent.run()
