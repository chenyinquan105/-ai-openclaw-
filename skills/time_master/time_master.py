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
  - 走时范围：只模拟 24h 虚拟时间，不跨天
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
    """单会话虚拟时钟状态"""
    def __init__(self, session_id: str, initial_time: str = "08:00"):
        self.session_id = session_id
        # 底层统一用绝对浮点数分钟维护时钟
        h, m = initial_time.split(":")
        self.virtual_minutes: float = float(int(h) * 60 + int(m))
        self.speed = 1.0 / 60                  # 每秒推进的虚拟分钟数，默认 1x 正常速度 = 1/60 分钟/秒
        self.is_running = False               # 自动走时是否开启
        self.schedule_nodes: list = []        # 排程节点列表 [{"time":"HH:MM","node_id":"...","name":"..."}]
        self.triggered_queue: list = []       # 事件消费队列，自动走时触发的事件推入
        self._timer: Optional[threading.Timer] = None

    @property
    def virtual_time(self) -> str:
        """绝对分钟 => HH:MM（不跨天，限制在 00:00-23:59）"""
        mins = int(self.virtual_minutes) % 1440
        return f"{mins // 60:02d}:{mins % 60:02d}"

    @property
    def minutes_today(self) -> int:
        return int(self.virtual_minutes) % 1440

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "virtual_time": self.virtual_time,
            "speed": self.speed,
            "is_running": self.is_running,
            "has_pending_events": len(self.triggered_queue) > 0,
        }


# ======================================================================
# 纯数学工具
# ======================================================================

def _parse_minutes(t: str) -> int:
    """HH:MM => 当天分钟数"""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _minutes_to_time(mins: int) -> str:
    """分钟数 => HH:MM（不跨天）"""
    mins = mins % 1440
    return f"{mins // 60:02d}:{mins % 60:02d}"


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
            elapsed = 0  # 不跨天，负值表示无推进
        ticked = []
        for m in range(start_m, end_m + 1):
            ticked.append(_minutes_to_time(m))
    else:
        ticked = []
        elapsed = elapsed_minutes

    return {
        "status": "SUCCESS" if not error_message else "ERROR",
        "previous_virtual_time": previous_virtual_time,
        "new_virtual_time": new_virtual_time,
        "elapsed_minutes": elapsed,
        "ticked_minutes_list": ticked,
        "triggered_nodes": triggered_nodes or [],
        "error_message": error_message,
    }


# ======================================================================
# 控制器
# ======================================================================

class TimeMaster:
    def __init__(self):
        self._sessions: dict[str, ClockState] = {}
        self._lock = threading.Lock()

    # ---------- 会话管理 ----------

    def get_or_create_session(self, session_id: str, initial_time: str = "08:00") -> ClockState:
        """外部调用（带锁）"""
        with self._lock:
            return self._get_or_create_session_nolock(session_id, initial_time)

    def _get_or_create_session_nolock(self, session_id: str, initial_time: str = "08:00") -> ClockState:
        """内部调用（调用者已持有锁）"""
        if session_id not in self._sessions:
            self._sessions[session_id] = ClockState(session_id, initial_time)
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
        相对偏移 delta 分钟，返回统一输出。
        """
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            old_minutes = cs.virtual_minutes
            old_time = cs.virtual_time

            cs.virtual_minutes += delta_minutes

            triggered = self._slice_triggered(cs, old_minutes, cs.virtual_minutes)

            return _build_output(
                previous_virtual_time=old_time,
                new_virtual_time=cs.virtual_time,
                elapsed_minutes=delta_minutes,
                triggered_nodes=triggered,
                previous_minutes=old_minutes,
                new_minutes=cs.virtual_minutes,
            )

    def jump(self, session_id: str, target_time: str) -> dict:
        """
        模式 2：SLIDER_DRAG（拖拽时间轴到指定时间点）
        绝对跳转，不可跨天。
        """
        with self._lock:
            cs = self._get_or_create_session_nolock(session_id)
            old_minutes = cs.virtual_minutes
            old_time = cs.virtual_time
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
                )

            cs.virtual_minutes = target_m
            triggered = self._slice_triggered(cs, old_minutes, cs.virtual_minutes)
            elapsed = int(cs.virtual_minutes - old_minutes)

            return _build_output(
                previous_virtual_time=old_time,
                new_virtual_time=cs.virtual_time,
                elapsed_minutes=max(0, elapsed),
                triggered_nodes=triggered,
                previous_minutes=old_minutes,
                new_minutes=cs.virtual_minutes,
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

    def _slice_triggered(self, cs: ClockState, start_m: float, end_m: float) -> list:
        """
        时间切片扫描器（不跨天版本）。
        从 start_m 推进到 end_m，找出在此区间内触发的节点。
        - repeat=daily 的节点触发生成事件后保留原节点（次日继续提醒）
        - 非 daily 节点触发后移除

        ticked_minutes_list 由 _build_output 自动从 start_m/end_m 生成，
        此方法只负责区间命中判定。
        """
        triggered = []
        remaining = []

        s_today = int(start_m) % 1440
        e_today = int(end_m) % 1440

        for nt in cs.schedule_nodes:
            nm = _parse_minutes(nt["time"])
            if s_today < nm <= e_today:
                triggered.append(nt)
                # daily 节点保留，次日继续提醒
                # WATER/MED 类型默认 daily（除非显式设为 once）；CUSTOM 按显式字段
                is_daily = nt.get("repeat") == "daily"
                if not is_daily and nt.get("type") in ("WATER", "MED") and nt.get("repeat") != "once":
                    is_daily = True  # 兜底：WATER/MED 无 repeat 字段时默认每天
                if is_daily:
                    remaining.append(nt)
            else:
                remaining.append(nt)

        cs.schedule_nodes = remaining
        return triggered

    def _start_timer(self, cs: ClockState):
        """每秒触发一次，步进 cs.speed 虚拟分钟"""
        if not cs.is_running:
            return

        old_m = cs.virtual_minutes
        cs.virtual_minutes += cs.speed  # 每秒推进 speed 虚拟分钟

        triggered = self._slice_triggered(cs, old_m, cs.virtual_minutes)
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
