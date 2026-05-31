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

    def run(self):
        print("=== 美团 AI 智能助手：全功能完整版 ===")
        user_input = input("用户: ")
        
        # --- 阶段 1: 需求解析与 POI 搜索 ---
        system_prompt_1 = {
            "role": "system",
            "content": "你是一个生活秘书。第一步必须调用 search_poi 搜索各品类商户。品类映射规则：理发/美发/沙宣→hair，宠物/狗/猫/洗澡→pet，咖啡/饮品/库迪→cafe，健身→gym，餐饮/吃饭/餐厅→restaurant，电影/影院→cinema，洗衣/干洗→laundry。注意：'库迪'可能是宠物品牌'酷迪宠物'，也可能是咖啡品牌，请同时搜索 pet 和 cafe 品类。"
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

        # 阶段 1 重试：最多 5 次，强制 LLM 调用 search_poi
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
            # 语义映射转换
            raw_cats = args.get("categories", [])
            mapped_cats = list(set([CATEGORY_MAP.get(c, c) for c in raw_cats]))

            print(f"[*] 检索 {mapped_cats} 品类中...")
            search_res = skill_poi.search_poi_matrix(
                center_coord=args.get("center_coord", "39.93,116.45"),
                categories=mapped_cats,
                radius_meters=args.get("radius_meters", 3000),
                min_rating=args.get("min_rating", 0)  # 容错：允许搜到所有分数的店
            )

            if search_res.get("status") == "SUCCESS":
                for cat in search_res["search_results"]:
                    for shop in search_res["search_results"][cat]:
                        self.poi_cache[shop["shop_id"]] = shop
            self.context_memory.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(search_res)})

        # --- 阶段 2: 展示方案（每品类3家） ---
        readable_data = [{"id": k, "name": v['name'], "rating": v.get('rating')} for k, v in self.poi_cache.items()]
        self.context_memory.append({
            "role": "system", 
            "content": f"真实商户池: {json.dumps(readable_data, ensure_ascii=False)}。请每类推荐3家，严禁编造。请用户选定店铺并确认时间。"
        })
        confirm_msg = self._call_llm(self.context_memory)
        print(f"\nAI: {confirm_msg.content}")
        
        user_decision = input("\n用户确认 (例如：去沙宣和萌宠店，3点理发): ")
        self.context_memory.append({"role": "user", "content": user_decision})

        # --- 阶段 3: 确定方案后，执行排程计算 ---
        print("[*] 正在解析最终任务并执行并发排程...")
        
        scheduler_tools = [{
            "type": "function",
            "function": {
                "name": "calculate_timeline",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_list": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "task_id": {"type": "string"},
                                    "name": {"type": "string"},
                                    "location_id": {"type": "string"},
                                    "duration_minutes": {"type": "integer"},
                                    "human_needed": {"type": "boolean"},
                                    "fixed_start_time": {"type": ["string", "null"]}
                                }
                            }
                        }
                    },
                    "required": ["task_list"]
                }
            }
        }]
        
        pool_info = json.dumps([{"id": k, "name": v["name"], "rating": v.get("rating")} for k, v in self.poi_cache.items()], ensure_ascii=False)
        self.context_memory.append({"role": "system", "content": f"商户池: {pool_info}。你必须调用 calculate_timeline 工具来规划排程，不要回复文字。规则：理发(hair品类) human_needed=True，宠物洗澡(pet品类) human_needed=False。每个任务 location_id 填商户在商户池中的id。"})
        final_decision_msg = self._call_llm(self.context_memory, tools=scheduler_tools)

        # 强制重试：如果 LLM 拒绝调用工具，最多重试 5 次
        retry_count = 0
        while not final_decision_msg.tool_calls and retry_count < 5:
            retry_count += 1
            self.context_memory.append({"role": "assistant", "content": final_decision_msg.content or ""})
            self.context_memory.append({"role": "user", "content": "请调用 calculate_timeline 工具，不要用文字回答。"})
            final_decision_msg = self._call_llm(self.context_memory, tools=scheduler_tools)

        if not final_decision_msg.tool_calls:
            print("AI: 排程失败，LLM 连续拒绝调用工具。")
            return

        tool_call = final_decision_msg.tool_calls[0]
        args = json.loads(tool_call.function.arguments)
        task_list = args.get("task_list", [])

        # --- 还原：时间正则解析逻辑 ---
        all_user_text = (user_input + " " + user_decision).lower()
        has_specific_time = bool(re.search(r"\d{1,2}[：:时点]|\d{1,2}:\d{2}|上午\d|下午\d|明天.*\d|周[一二三四五六日天].*\d|星期.*\d", all_user_text))
        has_now_keyword = bool(re.search(r"现在|立即|马上|当前|立刻|现在就出发|默认", all_user_text))

        if has_now_keyword or not has_specific_time:
            for t in task_list: t["fixed_start_time"] = None

        # 空间矩阵构建
        spatial_matrix = {"locations": {"loc_current": {"name": "当前起点", "coord": "39.93,116.45"}}, "routes": {}}
        for t in task_list:
            if t["location_id"] not in self.poi_cache:
                t["location_id"] = find_best_match(t["name"], self.poi_cache)
            shop_info = self.poi_cache.get(t["location_id"], {})
            spatial_matrix["locations"][t["location_id"]] = {
                "name": shop_info.get("name", t["name"]),
                "coord": shop_info.get("coord", "39.93,116.45")
            }

        confirmed_ids, rejected_ids = [], []
        now_str = datetime.now().strftime("%H:%M")

        while True:
            schedule_res = skill_scheduler.solve_concurrent_timeline(
                task_list, spatial_matrix, now_str, confirmed_ids, rejected_ids
            )

            if schedule_res.get("status") == "CONFIRM_REQUIRED":
                print(f"\n⚠️ 冲突预案激活: {schedule_res['message']}")
                choice = input("AI 请示：1.接受延误 2.任务延后: ")
                if choice == "1": confirmed_ids.append(schedule_res["conflict_task"]["task_id"])
                else: rejected_ids.append(schedule_res["conflict_task"]["task_id"])
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
