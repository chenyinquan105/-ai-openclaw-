"""
main.py —— 美团 AI 智能助手中枢系统
======================================
双轨设计：
  模式 1 — 原有静态排程（搜索店铺 → 选店 → 排计划）
  模式 2 — 24H 时空沙盒仿真（虚拟时钟驱动排程计划实时推进）
"""

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
    "干洗": "laundry", "洗衣服": "laundry", "咖啡": "cafe", "健身": "gym",
    "火锅": "hotpot",
    "餐饮": "restaurant", "餐厅": "restaurant", "吃饭": "restaurant", "饭店": "restaurant", "中餐": "restaurant",
    "日料": "japanese", "日本料理": "japanese", "日式": "japanese", "居酒屋": "japanese",
    "电影": "cinema", "电影院": "cinema", "看电影": "cinema",
}

CATEGORY_NAME_CN = {
    "hair": "理发",
    "pet": "宠物",
    "cafe": "咖啡",
    "gym": "健身",
    "restaurant": "餐饮",
    "cinema": "电影",
    "laundry": "干洗",
    "hotpot": "火锅",
    "japanese": "日料",
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
if base_dir not in sys.path: sys.path.insert(0, base_dir)
if skills_path not in sys.path: sys.path.append(skills_path)

try:
    from skills.amap_poi import amap_poi as skill_poi
    from skills.concurrent_pipeline_scheduler import concurrent_pipeline_scheduler as skill_scheduler
except ImportError as e:
    print(f"❌ 导入 Skill 失败: {e}")
    sys.exit(1)

from openai import OpenAI

# ======================================================================
# 双轨通用核心（虚拟时钟 + 任务提醒）
# ======================================================================
from skills.time_master import time_master as skill_time
from skills.task_reminder_skill import task_reminder_skill as reminder_skill


def _t_to_m(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _m_to_t(mins: int) -> str:
    mins = mins % 1440
    return f"{mins // 60:02d}:{mins % 60:02d}"


class MeituanAgent:
    def __init__(self, api_key: str, base_url: str):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        # 通过 FAST_LLM_MODEL 环境变量切换更快模型（如 deepseek-chat 已内置加速）
        self.model = os.getenv("FAST_LLM_MODEL", "deepseek-chat")
        self.context_memory = []
        self.poi_cache = {}

    def _call_llm(self, messages: list, tools: list = None, max_tokens: int = 300000):
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice="auto" if tools else None,
            timeout=8.0
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
                matched = [s for s in top_n if choice in s.get("name", "")]
                if matched:
                    print(f"  → 已选: {matched[0].get('name')}")
                    return matched[0]["shop_id"]
                print(f"  ⚠️ 请输入 1-{len(top_n)}、0、或完整店名")

    # ======================================================================
    # 模式 1：原有静态排程（不动）
    # ======================================================================

    def run(self):
        print("=== 美团 AI 智能助手 ===")
        print("  [1] 原有排程模式（搜索选店 → 排计划）")
        print("  [2] 24H 时空沙盒仿真（虚拟时钟驱动计划实时推进）")
        mode = input("  请选择运行模式 (1/2): ").strip()
        if mode == "2":
            self._run_sandbox_timeline()
            return
        self._run_classic_scheduling()

    def _run_classic_scheduling(self):
        """模式 1：原有静态排程（代码不变）"""
        user_input = input("用户: ")

        # --- 阶段 1: 需求解析与 POI 搜索 ---
        system_prompt_1 = {
            "role": "system",
            "content": "你是一个生活秘书。第一步必须调用 search_poi 搜索各品类商户。刻画品类映射规则：理发/美发/沙宣→hair，宠物/狗/猫/洗澡/宠物店→pet，咖啡→cafe，健身→gym，餐饮/吃饭/餐厅→restaurant，电影/影院→cinema，洗衣/干洗→laundry，火锅/海底捞/吃火锅→hotpot。"
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

        self.poi_cache_per_category = {}
        for sid, shop in self.poi_cache.items():
            cat = shop.get("category")
            self.poi_cache_per_category.setdefault(cat, []).append(shop)

        # --- 阶段 2: top 3 → 用户选店 ---
        searched_categories = list(self.poi_cache_per_category.keys())
        selected_pairs = []
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
                self._format_output(schedule_res)
                break
            else:
                print(f"AI: 规划失败：{schedule_res.get('message')}")
                break

    def _format_output(self, res):
        print("\n" + "★"*25)
        print(f"🏆 时间最优解生成 (建议 {res['suggested_departure_time']} 出发)")
        for item in res["timeline"]:
            print(f"[{item['time']}] {item['memo']}")
        print("★"*25 + "\n")

    # ======================================================================
    # 模式 2：24H 时空沙盒仿真
    # ======================================================================

    def _run_sandbox_timeline(self):
        """
        24H 时空沙盒仿真系统。
        虚拟时钟驱动排程计划实时推进，PlanB 异常、任务提醒全部联动。
        """
        print("\n" + "═"*25 + " 迈入24H生活健康数字沙盒 " + "═"*25)
        print("💡 操作指南：")
        print("  +10 / +20 / +30    快进按钮")
        print("  GOTO HH:MM         拖拽时间轴到指定时间点")
        print("  SPEED 1.0/2.0/3.0  切换自动走时倍速")
        print("  (空回车)            自动走时模式下手动步进一秒")
        print("  STATUS              查看当前时钟与事件状态")
        print("  EXIT                退出沙盒")
        print("═"*70)

        session_id = "sandbox_main"
        tm = skill_time.get_master()

        # ----------------------------------------------------------------
        # 第 1 步：跑一次原有排程引擎，获取计划表
        # ----------------------------------------------------------------
        print("\n[沙盒] 正在调用排程引擎生成计划...")
        schedule_res = self._generate_schedule_for_sandbox()
        if not schedule_res:
            print("[沙盒] 计划生成失败，退回主菜单。")
            return

        timeline = schedule_res["timeline"]
        suggested_departure = schedule_res["suggested_departure_time"]

        print(f"\n★ 计划已生成（建议 {suggested_departure} 出发）★")
        for item in timeline:
            print(f"  [{item['time']}] {item['memo']}")

        # ----------------------------------------------------------------
        # 第 2 步：将 timeline 注册到虚拟时钟作为 schedule_nodes
        # ----------------------------------------------------------------
        schedule_nodes = []
        for item in timeline:
            schedule_nodes.append({
                "time": item["time"],
                "type": "SCHEDULE",
                "node_id": item.get("task_id", "") or item.get("action", "unknown"),
                "name": item["memo"],
                "action": item["action"],
                "target_location_id": item.get("target_location_id"),
            })
        # 额外注册一个水任务作为演示
        schedule_nodes.append({"time": "10:00", "type": "WATER", "id": "wat_1", "name": "喝水提醒"})
        schedule_nodes.append({"time": "15:00", "type": "WATER", "id": "wat_2", "name": "喝水提醒"})
        schedule_nodes.append({"time": "08:30", "type": "MED", "id": "med_hypertension", "name": "高血压阿司匹林"})

        tm.set_schedule(session_id, schedule_nodes)

        # 将时钟初始化到计划表最早时间前 15 分钟
        first_time = schedule_nodes[0]["time"]
        first_m = _t_to_m(first_time)
        init_m = max(0, first_m - 15)
        init_time = _m_to_t(init_m)
        clock = tm.get_or_create_session(session_id, initial_time=init_time)

        # 记录已经报告过到达的节点（避免重复弹）
        _reported_arrive = set()

        auto_tick_enabled = False

        # ----------------------------------------------------------------
        # 第 3 步：主循环 — 用户操作 → 时钟推进 → 事件判定 → 反馈
        # ----------------------------------------------------------------
        while True:
            clock_state = clock.to_dict()
            running_label = f"▶ {clock_state['speed']}x" if auto_tick_enabled else "⏸"
            print(f"\n[🕒 {clock_state['virtual_time']}] {running_label}", end="")

            user_cmd = input("  >>> ").strip().lower()

            if user_cmd in ("exit", "quit"):
                tm.stop_auto_tick(session_id)
                break

            if user_cmd == "status":
                cs = tm.get_session(session_id)
                print(f"  虚拟时间: {cs.virtual_time}")
                print(f"  自动走时: {'开' if cs.is_running else '关'}")
                print(f"  待触发节点: {len(cs.schedule_nodes)} 个")
                for n in cs.schedule_nodes:
                    t = n.get("type", "?")
                    print(f"    [{n['time']}] [{t}] {n.get('name','')}")
                pending = tm.pop_triggered_events(session_id)
                if pending:
                    print(f"  未消费事件: {len(pending)} 个")
                continue

            # --- 用户响应交互（吃药/喝水/到达确认） ---
            if user_cmd in ("1", "2", "3", "我已吞服药片", "吃了", "已到达", "到达"):
                action_res = reminder_skill.handle_user_action(
                    session_id, user_cmd, clock.virtual_time, tm
                )
                print(f"  {action_res['message']}")
                continue

            ticked_list = []
            triggered_events = []
            time_changed = False

            # --- A. 快进 ---
            if user_cmd.startswith("+"):
                try:
                    delta = int(user_cmd[1:])
                    res = tm.offset(session_id, delta)
                    if res["status"] == "SUCCESS":
                        ticked_list = res["ticked_minutes_list"]
                        triggered_events = res["triggered_nodes"]
                        print(f"  ⏩ 快进 {delta} 分钟 → {res['new_virtual_time']}")
                        time_changed = True
                    else:
                        print(f"  ⚠️ {res['error_message']}")
                except ValueError:
                    print("  ⚠️ 格式: +10 / +20 / +30")
                continue

            # --- B. 拖拽跳转 ---
            if user_cmd.startswith("goto "):
                target_t = user_cmd[5:].strip()
                res = tm.jump(session_id, target_t)
                if res["status"] == "SUCCESS":
                    ticked_list = res["ticked_minutes_list"]
                    triggered_events = res["triggered_nodes"]
                    print(f"  🎚️ 跳转 → {res['new_virtual_time']} (经过 {res['elapsed_minutes']} 分钟)")
                    time_changed = True
                else:
                    print(f"  ⚠️ {res['error_message']}")
                continue

            # --- C. 倍速切换 ---
            if user_cmd.startswith("speed "):
                try:
                    speed_val = float(user_cmd.split(" ")[1])
                    if speed_val not in (1.0, 2.0, 3.0):
                        print("  ⚠️ 允许倍速: 1.0 / 2.0 / 3.0")
                        continue
                    speed_min = speed_val * 60  # 1x=60, 2x=120, 3x=180
                    tm.stop_auto_tick(session_id)
                    res = tm.start_auto_tick(session_id, speed_min)
                    if res["status"] == "SUCCESS":
                        auto_tick_enabled = True
                        print(f"  🚀 自动走时 {speed_val}x 已启动（每秒推进 {speed_min} 虚拟分钟）")
                    else:
                        print(f"  ⚠️ {res['error_message']}")
                except ValueError:
                    print("  ⚠️ 格式: SPEED 1.0 / SPEED 2.0 / SPEED 3.0")
                continue

            # --- D. 停止倍速 ---
            if user_cmd == "stop":
                tm.stop_auto_tick(session_id)
                auto_tick_enabled = False
                print("  ⏹ 自动走时已停止")
                continue

            # --- E. 空回车：自动走时步进 / 消费事件 ---
            if user_cmd == "":
                if auto_tick_enabled:
                    # 消费事件队列
                    queued = tm.pop_triggered_events(session_id)
                    if queued:
                        triggered_events = queued
                        ticked_list = []
                        time_changed = True
                    else:
                        cs = tm.get_session(session_id)
                        print(f"  . 当前 {cs.virtual_time} 无新事件")
                else:
                    print("  ℹ️ 自动走时未开启（先输入 SPEED 2.0）")
                # 如果没事件，继续循环
                if not time_changed:
                    continue

            # --- F. 未知指令 ---
            if not time_changed:
                print("  ⚠️ 未知指令，请参考上方操作指南")
                continue

            # ----------------------------------------------------------------
            # 事件判定：处理 ticked_list + triggered_events
            # ----------------------------------------------------------------
            if time_changed:
                # 1) 处理 SCHEDULE 类型的触发事件（排程到达/离开）
                for ev in triggered_events:
                    if ev.get("type") == "SCHEDULE":
                        node_id = ev.get("node_id", "")
                        name = ev.get("name", "")
                        act = ev.get("action", "")
                        if act == "ARRIVE" and node_id not in _reported_arrive:
                            _reported_arrive.add(node_id)
                            msg = f"✅ [{ev['time']}] 已到达：{name}"
                            print(f"  📍 {msg}")
                        elif act == "DEPART":
                            print(f"  🚶 [{ev['time']}] {name}")
                        elif act == "LEAVE":
                            print(f"  🚶 [{ev['time']}] {name}")
                        else:
                            print(f"  📋 [{ev['time']}] {name}")

                # 2) 调用 task_reminder 处理 WATER/MED 事件 + 超时催促
                alerts = reminder_skill.process_reminder_pipeline(
                    session_id, ticked_list, triggered_events, tm
                )
                for alert in alerts:
                    print(f"  {alert['message']}")

    # ======================================================================
    # 沙盒辅助：生成排程计划
    # ======================================================================

    def _generate_schedule_for_sandbox(self) -> dict:
        """
        快速走一遍排程流程（硬编码品类用于演示），返回 schedule_res。
        与 _run_classic_scheduling 的阶段 1-3 相同，但自动选店 + 自动时间。
        """
        # 使用硬编码演示场景：理发 → 咖啡 → 宠物
        demo_categories = ["hair", "cafe", "pet"]
        print(f"[沙盒] 演示场景: 理发 → 咖啡 → 宠物")

        # 搜 POI
        for cat in demo_categories:
            search_res = skill_poi.search_poi_matrix(
                center_coord="39.93,116.45",
                categories=[cat],
                radius_meters=3000,
                min_rating=0,
            )
            if search_res.get("status") == "SUCCESS":
                for c in search_res["search_results"]:
                    for shop in search_res["search_results"][c]:
                        self.poi_cache[shop["shop_id"]] = shop

        self.poi_cache_per_category = {}
        for sid, shop in self.poi_cache.items():
            cat = shop.get("category")
            self.poi_cache_per_category.setdefault(cat, []).append(shop)

        # 自动选 top1
        selected_pairs = []
        for cat in demo_categories:
            shops = self.poi_cache_per_category.get(cat, [])
            if shops:
                sorted_shops = sorted(shops, key=lambda s: s.get("rating", 0), reverse=True)
                top = sorted_shops[0]
                selected_pairs.append((cat, top["shop_id"], top["name"]))

        if not selected_pairs:
            return None

        def _duration(cat):
            return {"hair": 60, "pet": 30, "cafe": 20}.get(cat, 45)

        task_list = []
        spatial_matrix = {
            "locations": {"loc_current": {"name": "当前起点", "coord": "39.93,116.45"}},
            "routes": {}
        }

        for cat, sid, sname in selected_pairs:
            info = self.poi_cache.get(sid, {})
            coord = f"{info.get('lat', 39.93)},{info.get('lng', 116.45)}"

            task_list.append({
                "task_id": sid,
                "name": sname,
                "location_id": sid,
                "duration_minutes": _duration(cat),
                "human_needed": info.get("human_needed", True),
            })
            spatial_matrix["locations"][sid] = {
                "name": sname,
                "coord": coord,
            }

        now_str = "08:00"
        confirmed_ids, rejected_ids = [], []

        while True:
            schedule_res = skill_scheduler.solve_concurrent_timeline(
                task_list, spatial_matrix, now_str, confirmed_ids, rejected_ids
            )
            if schedule_res.get("status") == "CONFIRM_REQUIRED":
                # 沙盒模式自动接受冲突
                confirmed_ids.append(schedule_res["conflict_task"]["task_id"])
                continue
            elif schedule_res.get("status") == "SUCCESS":
                return schedule_res
            else:
                return None


    # ======================================================================
    # 通用LLM聊天流 — chat_stream()
    # ======================================================================

    def chat_stream(self, messages: list, tools: list = None, max_tool_rounds: int = 5):
        """
        流式LLM聊天生成器，支持工具调用循环。

        参数:
            messages: 完整对话历史（含system prompt）
            tools: OpenAI格式工具定义列表
            max_tool_rounds: 最大工具调用轮次（防止死循环）

        Yields:
            dict: SSE事件 {event, data}
        """
        current_messages = list(messages)  # 不修改原始列表
        tool_round = 0

        while tool_round <= max_tool_rounds:
            tool_round += 1

            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=current_messages,
                    max_tokens=4096,
                    tools=tools,
                    tool_choice="auto" if tools else None,
                    stream=True,
                    timeout=120.0
                )
            except Exception as e:
                yield {"event": "error", "data": {"message": f"LLM调用失败: {str(e)}"}}
                return

            # 收集流式响应，同时检测tool_calls
            collected_content = ""
            collected_tool_calls = []
            # DeepSeek streaming中 tool_calls 分chunk返回
            tool_call_buffer = {}  # index → {id, name, arguments_str}

            try:
                for chunk in response:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue

                    # 文本内容
                    if delta.content:
                        collected_content += delta.content
                        yield {"event": "message", "data": {"role": "assistant", "content": delta.content}}

                    # 工具调用（流式：每个chunk可能只传一个function name片段或arguments片段）
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_call_buffer:
                                tool_call_buffer[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": "",
                                    "arguments_str": ""
                                }
                            buf = tool_call_buffer[idx]
                            if tc_delta.id:
                                buf["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    buf["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    buf["arguments_str"] += tc_delta.function.arguments

            except Exception as e:
                yield {"event": "error", "data": {"message": f"流式响应中断: {str(e)}"}}
                return

            # 处理收集到的内容
            if collected_content:
                collected_content = ""  # 已逐块yield，不需要再发

            # 处理工具调用
            if tool_call_buffer:
                # 构建完整的tool_calls
                for idx in sorted(tool_call_buffer.keys()):
                    buf = tool_call_buffer[idx]
                    try:
                        args = json.loads(buf["arguments_str"]) if buf["arguments_str"].strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                    tc = {
                        "id": buf["id"],
                        "name": buf["name"],
                        "arguments": args
                    }
                    collected_tool_calls.append(tc)

                    # 添加到消息历史
                    current_messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": buf["id"],
                            "type": "function",
                            "function": {"name": buf["name"], "arguments": buf["arguments_str"]}
                        }]
                    })

                    # yield工具调用事件
                    yield {
                        "event": "tool_call",
                        "data": {
                            "id": buf["id"],
                            "name": buf["name"],
                            "arguments": args
                        }
                    }

                # 工具调用已yield，等待外部执行后传入结果
                # 不在内部循环中执行（由server.py的_execute_chat_tool处理）
                # 返回工具调用信息，让调用方执行
                if collected_tool_calls:
                    yield {
                        "event": "tool_calls_complete",
                        "data": {
                            "tool_calls": [{
                                "id": tc["id"],
                                "name": tc["name"],
                                "arguments": tc["arguments"]
                            } for tc in collected_tool_calls]
                        }
                    }
                    # 工具调用结果由server.py追加到messages后重新调用chat_stream
                    return

            else:
                # 没有工具调用，纯文本回复完成
                yield {"event": "done", "data": {"status": "complete"}}
                return

        # 超过最大轮次
        yield {"event": "error", "data": {"message": "工具调用轮次超限，请简化请求"}}

    def chat_stream_continue(self, messages: list, tools: list = None, max_tool_rounds: int = 5):
        """
        工具调用完成后的继续流式生成。与chat_stream相同逻辑，
        但messages中已包含tool_call和tool结果。
        """
        return self.chat_stream(messages, tools, max_tool_rounds)


if __name__ == "__main__":
    agent = MeituanAgent(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
    agent.run()
