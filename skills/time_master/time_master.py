"""
time_master.py —— 时间管家（沙盒虚拟时钟芯片）
=================================================
物理契约 (API Contract):
  虚拟时钟在服务端维护单一时钟状态，只做时间维度的纯数学计算。
  所有操作返回统一输出格式，包含 ticked_minutes_list + triggered_nodes。
  前端只传操作指令，不传当前时间。

核心原则:
  - 单一时钟源：服务端持有最终状态，客户端只发指令
  - 纯数学计算：不涉及任何系统真实时间
  - 倍速走时服务端定时器驱动，客户端只需发 start/stop
  - 走时范围：模拟多日虚拟时间，支持跨天（virtual_day 追踪天数偏移）
  - 1 倍速 = 1 秒走 1 虚拟分钟，speed 为每秒推进的虚拟分钟数
    speed 枚举: 1(1x正常) / 60(60x) / 300(300x)，内部存储为虚拟分钟/真实秒
  - 排程联动：推进后返回 triggered_nodes，由上层调用方处理节点完成

输出统一格式:
{
    "status": "SUCCESS | ERROR",
    "previous_virtual_time": "HH:MM",
    "new_virtual_time": "HH:MM",
    "elapsed_minutes": integer,
    "ticked_minutes_list": ["HH:MM", ...],   # 闭区间每-分钟
    "triggered_nodes": [...],                 # 命中的排程节点
    "error_message": ""
}
"""

import threading
from typing import Optional


# ======================================================================
# 时钟状态定义
# ======================================================================

class ClockState:
    """单会话虚拟时钟状态（支持多日模拟）"""
    def __init__(self, session_id: str, initial_time: str = "08:00", start_date: str = ""):
        self.session_id = session_id
        # 底层统一用绝对浮点数分钟维护时钟（仅当天 0-1439）
        self.virtual_minutes: float = _parse_minutes(initial_time)
        self.speed = 1.0 / 60                  # 每秒推进的虚拟分钟数，默认 1x 正常速度 = 1/60 分钟/秒
        self.is_running = False               # 自动走时是否开启
        self.schedule_nodes: list = []        # 排程节点列表 [{"time":"HH:MM","node_id":"...","name":"..."}]
        self.triggered_queue: list = []       # 事件消费队列，自动走时触发的事件推入
        self._timer: Optional[threading.Timer] = None
        # 多日支持
        self.virtual_day: int = 0             # 相对 start_date 的天数偏移
        self.start_date: str = start_date     # 参考起始日期 "YYYY-MM-DD"，空字符串表示未设置

    @property
    def virtual_time(self) -> str:
        """当天分钟 => HH:MM:SS（精确到秒）"""
        total_seconds = int(self.virtual_minutes * 60) % 86400
        h = total_seconds // 3600
        m = (total_seconds % 3600) // 60
        s = total_seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    @property
    def minutes_today(self) -> int:
        return int(self.virtual_minutes) % 1440

    @property
    def current_date_str(self) -> str:
        """返回当前虚拟日期字符串，如 '2026-07-13'；start_date 未设置时返回空字符串"""
        if not self.start_date:
            return ""
        try:
            from datetime import datetime, timedelta
            base = datetime.strptime(self.start_date, "%Y-%m-%d")
            current = base + timedelta(days=self.virtual_day)
            return current.strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            return self.start_date

    @property
    def current_date_display(self) -> str:
        """返回中文日期显示，如 '7月13日'；start_date 未设置时返回空字符串"""
        if not self.start_date:
            return ""
        try:
            from datetime import datetime, timedelta
            base = datetime.strptime(self.start_date, "%Y-%m-%d")
            current = base + timedelta(days=self.virtual_day)
            return f"{current.month}月{current.day}日"
        except (ValueError, OverflowError):
            return ""

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "virtual_time": self.virtual_time,
            "virtual_day": self.virtual_day,
            "start_date": self.start_date,
            "current_date": self.current_date_str,
            "current_date_display": self.current_date_display,
            "speed": self.speed,
            "is_running": self.is_running,
            "has_pending_events": len(self.triggered_queue) > 0,
        }


# ======================================================================
# 纯数学工具
# ======================================================================

def _parse_minutes(t: str) -> float:
    """HH:MM 或 HH:MM:SS => 当天分钟数（浮点，精确到秒）"""
    parts = t.split(":")
    h = int(parts[0])
    m = int(parts[1])
    s = int(parts[2]) if len(parts) > 2 else 0
    return h * 60 + m + s / 60.0


def _minutes_to_time(mins: float) -> str:
    """分钟数 => HH:MM:SS（不跨天，精确到秒）"""
    total_seconds = int(mins * 60) % 86400
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ======================================================================
# 统一输出构建器
# ======================================================================

def _build_output(
    previous_virtual_time: str,
    new_virtual_time: str,
    elapsed_minutes: int,
    triggered_nodes: list = None,
    previous_minutes: float = None,
    new_minutes: float = None,
    error_message: str = "",
    virtual_day: int = 0,
    start_date: str = "",
    current_date: str = "",
) -> dict:
    """
    构建统一输出格式。
    当 previous_minutes / new_minutes 提供时，自动生成 ticked_minutes_list。
    """
    if previous_minutes is not None and new_minutes is not None:
        start_m = int(previous_minutes) % 1440
        end_m = int(new_minutes) % 1440
        elapsed = end_m - start_m
        if elapsed < 0:
            elapsed = 0  # 跨天由调用方传入 actual elapsed
        ticked = []
        for m in range(start_m, end_m + 1):
            ticked.append(_minutes_to_time(float(m)))
    else:
        ticked = []
        elapsed = elapsed_minutes

    # 计算 current_date 和 current_date_display（如果提供了 start_date 和 virtual_day）
    current_date_display = ""
    if not current_date and start_date:
        try:
            from datetime import datetime, timedelta
            base = datetime.strptime(start_date, "%Y-%m-%d")
            cur = base + timedelta(days=virtual_day)
            current_date = cur.strftime("%Y-%m-%d")
            current_date_display = f"{cur.month}月{cur.day}日"
        except (ValueError, OverflowError):
            current_date = ""
    elif current_date and not current_date_display:
        # 如果传入了 current_date，也尝试生成 display 格式
        try:
            from datetime import datetime
            d = datetime.strptime(current_date, "%Y-%m-%d")
            current_date_display = f"{d.month}月{d.day}日"
        except (ValueError, OverflowError):
            pass

    return {
        "status": "SUCCESS" if not error_message else "ERROR",
        "previous_virtual_time": previous_virtual_time,
        "new_virtual_time": new_virtual_time,
        "elapsed_minutes": elapsed,
        "ticked_minutes_list": ticked,
        "triggered_nodes": triggered_nodes or [],
        "error_message": error_message,
        "virtual_day": virtual_day,
        "start_date": start_date,
        "current_date": current_date,
        "current_date_display": current_date_display,
    }


# ======================================================================
# 控制器
# ======================================================================

class TimeMaster:
    def __init__(self):
        self._sessions: dict[str, ClockState] = {}
        self._lock = threading.Lock()

    # ---------- 会话管理 ----------

    def get_or_create_session(self, session_id: str, initial_time: str = "08:00", start_date: str = "") -> ClockState:
        """外部调用（带锁）"""
        with self._lock:
            return self._get_or_create_session_nolock(session_id, initial_time, start_date)

    def _get_or_create_session_nolock(self, session_id: str, initial_time: str = "08:00", start_date: str = "") -> ClockState:
        """内部调用（调用者已持有锁）"""
        if session_id not in self._sessions:
            self._sessions[session_id] = ClockState(session_id, initial_time, start_date=start_date)
        return self._sessions[session_id]

    def get_session(self, session_id: str) -> Optional[ClockState]:
        """获取会话，返回 None 如果不存在"""
        with self._lock:
            return self._sessions.get(session_id)

    def remove_session(self, session_id: str):
        with self._lock:
            if session_id in self._sessions:
                self.stop_auto_tick(session_id)
                del self._sessions[session_id]

    # ---------- 排程节点 ----------

    def set_schedule(self, session_id: str, nodes: list, initial_time: str = "08:00"):
        """注册排程节点，按时间升序存储。session 不存在时拒绝写入（防止意外创建）"""
        with self._lock:
            if session_id not in self._sessions:
                return False
            cs = self._sessions[session_id]
            cs.schedule_nodes = sorted(nodes, key=lambda n: _parse_minutes(n["time"]))
            return True

    # ---------- 三种控制模式 ----------

    def offset(self, session_id: str, delta_minutes: int) -> dict:
        """
        模式 1：QUICK_FORWARD（快进按钮 +10/+20/+30）
        相对偏移 delta 分钟，支持跨天，返回统一输出。
        """
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            old_minutes = cs.virtual_minutes
            old_time = cs.virtual_time
            old_day = cs.virtual_day

            new_raw = cs.virtual_minutes + delta_minutes
            # 检查是否跨天
            days_passed = int(new_raw // 1440)
            cs.virtual_minutes = new_raw % 1440
            cs.virtual_day += days_passed

            triggered = self._slice_triggered(cs, old_minutes, old_day, cs.virtual_minutes, cs.virtual_day)

            return _build_output(
                previous_virtual_time=old_time,
                new_virtual_time=cs.virtual_time,
                elapsed_minutes=delta_minutes,
                triggered_nodes=triggered,
                previous_minutes=old_minutes,
                new_minutes=cs.virtual_minutes,
                virtual_day=cs.virtual_day,
                start_date=cs.start_date,
            )

    def jump(self, session_id: str, target_time: str) -> dict:
        """
        模式 2：SLIDER_DRAG（拖拽时间轴到指定时间点）
        绝对跳转到当天指定时间，不可跨天。日期间跳转请用 jump_day()。
        """
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            old_minutes = cs.virtual_minutes
            old_time = cs.virtual_time
            old_day = cs.virtual_day
            target_m = float(_parse_minutes(target_time))

            # 校验：只允许当天 00:00-23:59
            if target_m < 0 or target_m > 1439:
                return _build_output(
                    previous_virtual_time=old_time,
                    new_virtual_time=old_time,
                    elapsed_minutes=0,
                    previous_minutes=old_minutes,
                    new_minutes=old_minutes,
                    error_message=f"目标时间越界: {target_time}，仅允许 00:00-23:59",
                    virtual_day=cs.virtual_day,
                    start_date=cs.start_date,
                )

            cs.virtual_minutes = target_m
            triggered = self._slice_triggered(cs, old_minutes, old_day, cs.virtual_minutes, cs.virtual_day)
            elapsed = int(cs.virtual_minutes - old_minutes)

            return _build_output(
                previous_virtual_time=old_time,
                new_virtual_time=cs.virtual_time,
                elapsed_minutes=max(0, elapsed),
                triggered_nodes=triggered,
                previous_minutes=old_minutes,
                new_minutes=cs.virtual_minutes,
                virtual_day=cs.virtual_day,
                start_date=cs.start_date,
            )

    def jump_day(self, session_id: str, delta_days: int) -> dict:
        """
        模式 2b：DAY_NAV（日期导航箭头）
        切换 virtual_day，保持当天时间不变。不扫描触发节点（纯查看）。
        """
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            old_day = cs.virtual_day
            old_time = cs.virtual_time

            new_day = old_day + delta_days
            if new_day < 0:
                new_day = 0  # 不允许负天数

            cs.virtual_day = new_day

            return _build_output(
                previous_virtual_time=old_time,
                new_virtual_time=cs.virtual_time,
                elapsed_minutes=0,
                triggered_nodes=[],
                previous_minutes=cs.virtual_minutes,
                new_minutes=cs.virtual_minutes,
                virtual_day=cs.virtual_day,
                start_date=cs.start_date,
            )

    def start_auto_tick(self, session_id: str, speed: float = 1.0) -> dict:
        """
        模式 3：AUTO_TICK — 启动自动走时。
        speed: 每秒推进的虚拟分钟数，默认 1/60 (1x 正常速度)
        """
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            if cs.is_running:
                return _build_output(
                    previous_virtual_time=cs.virtual_time,
                    new_virtual_time=cs.virtual_time,
                    elapsed_minutes=0,
                    previous_minutes=cs.virtual_minutes,
                    new_minutes=cs.virtual_minutes,
                    error_message="自动走时已在运行中",
                    virtual_day=cs.virtual_day,
                    start_date=cs.start_date,
                )

            speed = float(speed)
            if speed <= 0 or speed > 1440:
                return _build_output(
                    previous_virtual_time=cs.virtual_time,
                    new_virtual_time=cs.virtual_time,
                    elapsed_minutes=0,
                    previous_minutes=cs.virtual_minutes,
                    new_minutes=cs.virtual_minutes,
                    error_message=f"无效倍速: {speed}，允许范围 (0, 1440]",
                    virtual_day=cs.virtual_day,
                    start_date=cs.start_date,
                )

            cs.speed = speed
            cs.is_running = True
            self._start_timer(cs)

            return _build_output(
                previous_virtual_time=cs.virtual_time,
                new_virtual_time=cs.virtual_time,
                elapsed_minutes=0,
                previous_minutes=cs.virtual_minutes,
                new_minutes=cs.virtual_minutes,
                virtual_day=cs.virtual_day,
                start_date=cs.start_date,
            )

    def stop_auto_tick(self, session_id: str) -> dict:
        """
        模式 3：AUTO_TICK — 停止自动走时。
        """
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            old_time = cs.virtual_time
            old_minutes = cs.virtual_minutes
            cs.is_running = False
            if cs._timer:
                cs._timer.cancel()
                cs._timer = None

            return _build_output(
                previous_virtual_time=old_time,
                new_virtual_time=cs.virtual_time,
                elapsed_minutes=0,
                previous_minutes=old_minutes,
                new_minutes=cs.virtual_minutes,
                virtual_day=cs.virtual_day,
                start_date=cs.start_date,
            )

    def set_speed(self, session_id: str, speed: float) -> dict:
        """只设倍速，不启动自动走时"""
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            speed = float(speed)
            if speed <= 0 or speed > 1440:
                return _build_output(
                    previous_virtual_time=cs.virtual_time,
                    new_virtual_time=cs.virtual_time,
                    elapsed_minutes=0,
                    previous_minutes=cs.virtual_minutes,
                    new_minutes=cs.virtual_minutes,
                    error_message=f"无效倍速: {speed}，允许范围 (0, 1440]",
                    virtual_day=cs.virtual_day,
                    start_date=cs.start_date,
                )
            cs.speed = speed
            return {
                "status": "SUCCESS",
                "previous_virtual_time": cs.virtual_time,
                "new_virtual_time": cs.virtual_time,
                "elapsed_minutes": 0,
                "ticked_minutes_list": [],
                "triggered_nodes": [],
                "error_message": "",
                "speed": speed,
                "virtual_day": cs.virtual_day,
                "start_date": cs.start_date,
                "current_date": cs.current_date_str,
            }

    def pop_triggered_events(self, session_id: str) -> list:
        """消费事件队列（供外部 poll 使用）"""
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            events = list(cs.triggered_queue)
            cs.triggered_queue.clear()
            return events

    def push_triggered_event(self, session_id: str, event: dict):
        """向事件队列注入一条事件（供提醒引擎使用）"""
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            cs.triggered_queue.append(event)

    # ---------- 内部方法 ----------

    def _slice_triggered(self, cs: ClockState, start_m: float, start_day: int, end_m: float, end_day: int) -> list:
        """
        时间切片扫描器（多日版本）。
        从 (start_day, start_m) 推进到 (end_day, end_m)，找出在此区间内触发的节点。
        - repeat=daily 的节点：每天都触发生成事件后保留原节点
        - repeat=once 且有 date 字段：仅当 cs.current_date_str == node.date 时触发，触发后移除
        - repeat=once 无 date 字段（legacy）：当作当天一次性
        - WATER 多时间点：逐个检查 sub_times，每个命中子时间生成独立触发事件
        """
        triggered = []
        remaining = []

        s_today = int(start_m) % 1440
        e_today = int(end_m) % 1440
        current_date = cs.current_date_str  # 用于 date-aware 匹配

        for nt in cs.schedule_nodes:
            sub_times = nt.get("sub_times", []) if nt.get("type") == "WATER" else []
            node_date = nt.get("date", "")
            node_repeat = nt.get("repeat", "")

            # ── 日期感知过滤 ──
            # 如果节点指定了 date 且 current_date 已设置，仅当匹配时才考虑触发
            if node_date and current_date:
                if node_date != current_date:
                    # 日期不匹配：检查是否已过期（date < current_date 且 repeat=once → 丢弃）
                    if node_repeat == "once" and node_date < current_date:
                        continue  # 过期的一次性节点，不保留
                    # 未来的日期或 daily/weekly 节点，保留
                    remaining.append(nt)
                    continue

            # ── repeat=once 且无 date 字段（legacy 行为）：仅当天触发 ──
            if node_repeat == "once" and not node_date and start_day != end_day:
                # 跨天推进中，legacy once 节点不匹配（无法确定属于哪一天）
                remaining.append(nt)
                continue

            if sub_times:
                # WATER 多时间点：逐个检查 sub_times
                any_triggered = False
                for st in sub_times:
                    sm = _parse_minutes(st)
                    if s_today < sm <= e_today:
                        triggered_node = dict(nt)
                        triggered_node["time"] = st
                        # 注入触发日期信息
                        if current_date:
                            triggered_node["trigger_date"] = current_date
                        triggered.append(triggered_node)
                        any_triggered = True
                # WATER 默认 daily，保留原节点（含完整 sub_times）
                is_daily = node_repeat != "once"
                if is_daily:
                    remaining.append(nt)
                elif not any_triggered:
                    remaining.append(nt)
            else:
                nm = _parse_minutes(nt["time"])
                if s_today < nm <= e_today:
                    triggered_node = dict(nt)
                    # 注入触发日期信息
                    if current_date:
                        triggered_node["trigger_date"] = current_date
                    triggered.append(triggered_node)
                    # daily 节点保留，次日继续提醒
                    # WATER/MED 类型默认 daily（除非显式设为 once）；CUSTOM 按显式字段
                    is_daily = node_repeat == "daily"
                    if not is_daily and nt.get("type") in ("WATER", "MED") and node_repeat != "once":
                        is_daily = True  # 兜底：WATER/MED 无 repeat 字段时默认每天
                    if is_daily:
                        remaining.append(nt)
                    # once 节点不保留（已触发，移除）
                else:
                    remaining.append(nt)

        cs.schedule_nodes = remaining
        return triggered

    def _start_timer(self, cs: ClockState):
        """每秒触发一次，步进 cs.speed 虚拟分钟，支持跨天"""
        if not cs.is_running:
            return

        old_m = cs.virtual_minutes
        old_day = cs.virtual_day
        new_raw = cs.virtual_minutes + cs.speed

        # 跨天检测
        days_passed = int(new_raw // 1440)
        cs.virtual_minutes = new_raw % 1440
        cs.virtual_day += days_passed

        triggered = self._slice_triggered(cs, old_m, old_day, cs.virtual_minutes, cs.virtual_day)
        if triggered:
            cs.triggered_queue.extend(triggered)

        cs._timer = threading.Timer(1.0, self._tick, args=[cs.session_id])
        cs._timer.daemon = True
        cs._timer.start()

    def _tick(self, session_id: str):
        with self._lock:
            cs = self._sessions.get(session_id)
            if cs and cs.is_running:
                self._start_timer(cs)


# ======================================================================
# 全局单例
# ======================================================================

_MASTER: Optional[TimeMaster] = None


def get_master() -> TimeMaster:
    global _MASTER
    if _MASTER is None:
        _MASTER = TimeMaster()
    return _MASTER
