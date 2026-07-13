"""
task_reminder_skill.py —— 独立任务提醒 Skill 核心引擎（沙盒健康生活专员）
===================================================================
核心修正:
  1. 催促判定改为基于时间差（last_action_time vs 当前虚拟时间），
     不再依赖 ticked_minutes 列表内是否存在某个时间点，避免快进/拖拽时同 tick 触发多级催促
  2. 45 分钟无响应 → 联系紧急联络人（非上医院）
  3. 延后 30 分钟从当前虚拟时间计时，直接从 time_master 读取当前时间
"""

from typing import Dict, Any, List, Optional


# ======================================================================
# 内部工具函数
# ======================================================================

def _t_to_m(t: str) -> int:
    parts = t.split(":")
    h, m = int(parts[0]), int(parts[1])
    return h * 60 + m


def _m_to_t(mins: int) -> str:
    mins = mins % 1440
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _format_dosage(dosage: str) -> str:
    """格式化剂量显示：纯数字自动追加「片」，已有单位的保持原样"""
    d = dosage.strip()
    if not d:
        return ""
    # 纯数字 → "每次服用X片"
    if d.isdigit():
        return f"每次服用{d}片"
    # 已有文字/单位 → "每次X"（用户自己填了"2粒""1包"等）
    return f"每次{d}"


# ======================================================================
# 任务状态机管理器
# ======================================================================

class ReminderStateManager:
    """维护单会话内部的健康提醒状态机（纯内存沙盒环境）"""
    def __init__(self):
        # 结构: { session_id: { med_id: { status, original_time, last_action_time, miss_count, med_name } } }
        self._states: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # 记录已成功吞服的历史，用于【防重复吃药】拦截
        self._history: Dict[str, Dict[str, List[str]]] = {}

    def init_session(self, session_id: str):
        if session_id not in self._states:
            self._states[session_id] = {}
        if session_id not in self._history:
            self._history[session_id] = {}

    def record_swallowed(self, session_id: str, med_name: str, current_time: str):
        self.init_session(session_id)
        if med_name not in self._history[session_id]:
            self._history[session_id][med_name] = []
        self._history[session_id][med_name].append(current_time)

    def check_duplicate_attempt(self, session_id: str, med_name: str) -> Optional[str]:
        """检查老人家今天是否已经吃过这个药，返回吃过的最后时间"""
        self.init_session(session_id)
        history_list = self._history[session_id].get(med_name, [])
        if history_list:
            return history_list[-1]
        return None

    def set_med_state(self, session_id: str, med_id: str, state_dict: dict):
        self.init_session(session_id)
        self._states[session_id][med_id] = state_dict

    def get_med_state(self, session_id: str, med_id: str) -> Optional[dict]:
        return self._states.get(session_id, {}).get(med_id)

    def get_all_active_meds(self, session_id: str) -> dict:
        return self._states.get(session_id, {})


# 全局状态单例
_REMINDER_MANAGER = ReminderStateManager()


# ======================================================================
# 核心业务执行逻辑
# ======================================================================

def process_reminder_pipeline(
    session_id: str,
    ticked_minutes: List[str],
    triggered_events: List[dict],
    time_master,
    elder_name: str = "",
    emergency_contact: str = "",
) -> List[dict]:
    """
    核心时空过筛引擎（修正版）
    ==========================
    1. 处理刚刚由 Time Master 触发的原始事件（喝水/吃药初次响铃）
    2. 基于 **时间差** 盘点挂起事件是否引发了"超时未响应"
       （不再依赖 ticked_minutes 列表内是否包含某个催促时间点）
    """
    mgr = _REMINDER_MANAGER
    mgr.init_session(session_id)
    output_notifications = []

    # 占位符兜底
    _elder = elder_name or "【对您的称呼（需要在偏好设置里完善）】"
    _contact = emergency_contact or "【紧急联系人信息（需要在偏好设置里完善）】"

    # ----------- 1. 处理 frisch 触发的原始事件 -----------
    for event in triggered_events:
        ev_type = event.get("type")
        ev_id = event.get("id")
        ev_time = event.get("time")
        med_name = event.get("med_name") or event.get("name") or event.get("label") or "未名药"
        ev_label = event.get("label", "")
        ev_images = event.get("images", [])
        ev_ring_mode = event.get("ring_mode", "once")
        ev_note = event.get("note", "")
        ev_dosage = event.get("dosage", "")
        ev_meal = event.get("meal_timing", "")
        ev_pill_shape = event.get("pill_shape", "")
        ev_pill_color = event.get("pill_color", "")
        ev_pill_color2 = event.get("pill_color2", "")
        ev_med_name = event.get("med_name", "")

        if ev_type == "WATER":
            output_notifications.append({
                "type": "WATER_UI_ALERT",
                "time": ev_time,
                "label": ev_label or "喝水",
                "message": f"🥤【温馨喝水提示】({ev_time})：忙碌之余，记得喝杯温水润润嗓子哦，保持身体水分充足！",
                "ring_mode": ev_ring_mode,
            })

        elif ev_type == "CUSTOM":
            output_notifications.append({
                "type": "CUSTOM_RINGING_ALERT",
                "time": ev_time,
                "label": ev_label or "自定义提醒",
                "message": "⏰ " + (event.get("content") or ev_label or "自定义提醒"),
                "content": event.get("content", ""),
                "note": event.get("note", ""),
                "ring_mode": ev_ring_mode,
            })

        elif ev_type == "MED":
            # 防重复：今天是否已吃过
            last_taken = mgr.check_duplicate_attempt(session_id, med_name)
            if last_taken:
                output_notifications.append({
                    "type": "MED_DUPLICATE_BLOCK",
                    "time": ev_time,
                    "message": f"⚠️【🚨 安全拦截防御】检测到系统原本计划在 {ev_time} 提醒服用 [{med_name}]。"
                               f"但记录显示，{_elder}已在 {last_taken} 服用过该药物。"
                               f"系统已自动锁死并拦截本次提醒，防止重复服药！"
                })
                continue

            # 正常初次触发：进入 RINGING 响铃状态
            med_state = {
                "status": "RINGING",
                "original_time": ev_time,
                "last_action_time": ev_time,
                "miss_count": 0,
                "med_name": ev_med_name or med_name,
                "med_id": ev_id,
                "note": ev_note,
                "dosage": ev_dosage,
                "meal_timing": ev_meal,
                "pill_shape": ev_pill_shape,
                "pill_color": ev_pill_color,
                "pill_color2": ev_pill_color2,
                "images": ev_images,
                "ring_mode": ev_ring_mode,
                "label": ev_label,
            }
            mgr.set_med_state(session_id, ev_id, med_state)

            msg = f"{_elder}，请服用【{med_name}】"
            if ev_dosage or ev_meal:
                parts = []
                if ev_meal:
                    parts.append(f"{ev_meal}服用")
                if ev_dosage:
                    _dosage_text = _format_dosage(ev_dosage)
                    parts.append(_dosage_text)
                msg += "\n" + "，".join(parts)
            if ev_note:
                msg += f"\n📝 {ev_note}"

            output_notifications.append({
                "type": "MED_RINGING_ALERT",
                "time": ev_time,
                "med_id": ev_id,
                "label": ev_label or med_name,
                "images": ev_images,
                "ring_mode": ev_ring_mode,
                "pill_shape": ev_pill_shape,
                "pill_color": ev_pill_color,
                "pill_color2": ev_pill_color2,
                "meal_timing": ev_meal,
                "message": msg,
            })

    # ----------- 2. 基于时间差判定"超时未响应" -----------
    # 获取当前虚拟时间（从 time_master 读）
    cs = time_master.get_session(session_id)
    if not cs:
        return output_notifications
    now_minutes = _t_to_m(cs.virtual_time)

    active_meds = mgr.get_all_active_meds(session_id)
    for med_id, state in list(active_meds.items()):
        if state["status"] not in ["RINGING", "PENDING_SWALLOW"]:
            continue

        # 核心修正：计算从最后一次交互到现在经过了多少分钟
        last_m = _t_to_m(state["last_action_time"])
        elapsed = now_minutes - last_m
        if elapsed < 0:
            elapsed = 0  # 不跨天

        current_miss = state["miss_count"]

        # 三级催促判定（均基于 elapsed，不再依赖 ticked_minutes）
        # 催促间隔：+5min 初次催促 → +10min 二次催促 → +15min 紧急联络
        med_note = state.get("note", "")
        med_dosage = state.get("dosage", "")
        med_meal = state.get("meal_timing", "")

        def _build_med_msg(level_label, elder, med_name, dosage, meal, note):
            msg = f"{level_label}\n{elder}，请服用【{med_name}】"
            if dosage or meal:
                parts = []
                if meal: parts.append(f"{meal}服用")
                if dosage: parts.append(_format_dosage(dosage))
                msg += "\n" + "，".join(parts)
            if note:
                msg += f"\n📝 {note}"
            return msg

        if elapsed >= 15 and current_miss < 3:
            state["miss_count"] = 3
            state["status"] = "ESCALATED"
            output_notifications.append({
                "type": "MED_ESCALATION_CRITICAL",
                "med_id": med_id,
                "time": _m_to_t(last_m + 15),
                "message": f"🆘 {_elder}，系统检测到您已超过15分钟未响应服药提醒，已自动通知紧急联络人：{_contact}",
            })

        elif elapsed >= 10 and current_miss < 2:
            state["miss_count"] = 2
            output_notifications.append({
                "type": "MED_URGE_HEAVY",
                "med_id": med_id,
                "time": _m_to_t(last_m + 10),
                "message": _build_med_msg("第3次提醒", _elder, state['med_name'], med_dosage, med_meal, med_note),
            })

        elif elapsed >= 5 and current_miss < 1:
            state["miss_count"] = 1
            output_notifications.append({
                "type": "MED_URGE_LIGHT",
                "med_id": med_id,
                "time": _m_to_t(last_m + 5),
                "message": _build_med_msg("第2次提醒", _elder, state['med_name'], med_dosage, med_meal, med_note),
            })

    return output_notifications


def handle_user_action(
    session_id: str,
    user_input: str,
    current_time: str,
    time_master,
) -> dict:
    """
    接收老人的动作输入（1 / 2 / 我已吞服药片），驱动内部状态机。
    current_time 参数保留用于部分场景兼容，核心时间读取从 time_master 获取。
    """
    mgr = _REMINDER_MANAGER
    mgr.init_session(session_id)
    active_meds = mgr.get_all_active_meds(session_id)
    # 称呼兜底（process_reminder_pipeline 通过参数传入，handle_user_action 无此参数，加 fallback）
    _elder = "老人家"

    # 寻找当前正在挂起等待交互的药物节点
    pending_med = None
    for mid, state in active_meds.items():
        if state["status"] in ["RINGING", "PENDING_SWALLOW"]:
            pending_med = state
            break

    # 兜底：如果用户说"吃了"但状态已经是 COMPLETED，防重复拦截
    if user_input in ["我已吞服药片", "吃了", "1"] and not pending_med:
        for mid, state in active_meds.items():
            if state["status"] == "COMPLETED":
                return {
                    "status": "INTERCEPTED",
                    "message": (
                        f"⚠️【🚨 拒绝执行】{_elder}，系统记录显示您今天已经在 {state['last_action_time']} "
                        f"服用过 [{state['med_name']}] 了！请不要重复吃药，药吃多了会不舒服的！"
                    ),
                }

    if not pending_med:
        return {"status": "NO_ACTIVE_PERIOD", "message": "ℹ️ 当前没有正在等待确认的服药流程。"}

    med_id = pending_med["med_id"]
    med_name = pending_med["med_name"]

    # ---- 分支 1: 响铃唤醒阶段 ----
    if pending_med["status"] == "RINGING":
        if user_input == "1":
            pending_med["status"] = "PENDING_SWALLOW"
            # 从 time_master 读取当前虚拟时间
            cs = time_master.get_session(session_id)
            now_time = cs.virtual_time if cs else current_time
            pending_med["last_action_time"] = now_time
            pending_med["miss_count"] = 0
            return {
                "status": "PROCEED",
                "message": (
                    f"✅【系统进入安全监视程序】{_elder}去拿药了。"
                    f"请您在【真正把药片吞服下去】之后，点击或回复【我已吞服药片】，"
                    f"这样系统才能彻底放心哦！"
                ),
            }

        elif user_input == "2":
            # 延后 30 分钟：从当前虚拟时间起计时
            cs = time_master.get_session(session_id)
            now_time = cs.virtual_time if cs else current_time
            now_m = _t_to_m(now_time)
            new_trigger_m = now_m + 30
            new_trigger_time = _m_to_t(new_trigger_m)

            # 回调 time_master 注入一个新的动态排程节点
            current_schedule = list(cs.schedule_nodes) if cs else []
            # 去重：移除同一 med_id 的旧延后节点，仅保留最新一次
            current_schedule = [n for n in current_schedule if not (n.get("_postponed") and n.get("id") == med_id)]
            current_schedule.append({
                "time": new_trigger_time,
                "type": "MED",
                "id": med_id,
                "name": med_name,
                "label": pending_med.get("label", med_name),
                "ring_mode": pending_med.get("ring_mode", "once"),
                "note": pending_med.get("note", ""),
                "dosage": pending_med.get("dosage", ""),
                "meal_timing": pending_med.get("meal_timing", ""),
                "images": pending_med.get("images", []),
                "pill_shape": pending_med.get("pill_shape", ""),
                "pill_color": pending_med.get("pill_color", ""),
                "pill_color2": pending_med.get("pill_color2", ""),
                "med_name": pending_med.get("med_name", ""),
                "_postponed": True,
            })
            time_master.set_schedule(session_id, current_schedule)

            # 重置状态机为 IDLE + 清空催促计数，等待 30 分钟后由 Time Master 二次唤醒
            pending_med["status"] = "IDLE"
            pending_med["miss_count"] = 0
            pending_med["last_action_time"] = now_time
            return {
                "status": "POSTPONED",
                "message": (
                    f"🌾【顺延成功】收到，知道{_elder}还没吃饭。"
                    f"已自动将 [{med_name}] 服药提醒往后顺延 30 分钟。"
                    f"将在虚拟时间 {new_trigger_time} 再次唤醒响铃！"
                ),
            }

        else:
            return {"status": "INVALID_INPUT", "message": "⚠️ 选型无效，请输入 '1' (现在去吃药) 或 '2' (还没吃饭延后)。"}

    # ---- 分支 2: 等待真正吞服确认阶段 ----
    elif pending_med["status"] == "PENDING_SWALLOW":
        if user_input in ["我已吞服药片", "吃了"]:
            pending_med["status"] = "COMPLETED"
            cs = time_master.get_session(session_id)
            now_time = cs.virtual_time if cs else current_time
            pending_med["last_action_time"] = now_time
            # 永固锁定至防重复服用历史库
            mgr.record_swallowed(session_id, med_name, now_time)
            return {
                "status": "SUCCESS_CLOSED",
                "message": (
                    f"🎉【服药闭环成功】记录成功！{_elder}已于 {now_time} 顺利服用 [{med_name}]。"
                    f"今日该服药事件安全锁死，防重复覆盖机制已激活！"
                ),
            }
        else:
            return {
                "status": "WAITING_SWALLOW",
                "message": "ℹ️ 系统仍在安全挂起中。请在确认真正吞下药片后回复【我已吞服药片】。",
            }

    return {"status": "UNKNOWN_ERROR", "message": "未处理的状态机分支。"}
