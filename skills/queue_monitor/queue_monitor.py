"""
queue_monitor — 餐厅排队监控 Skill

模拟美团餐厅实时排队状态，支持排号/查询/轮询/消化。
"""
import time
import random
from typing import Dict, List, Optional


# 全局排队状态（简易内存存储）
_QUEUES: Dict[str, dict] = {}
_COUNTER = int(time.time() * 1000)


def _new_queue_id() -> str:
    global _COUNTER
    _COUNTER += 1
    return f"Q_{_COUNTER}"


def _reset_all():
    """测试用：清空所有排队"""
    global _QUEUES
    _QUEUES = {}


def _get_or_create_shop_state(shop_id: str, shop_name: str, hour: int) -> dict:
    """获取或初始化店铺排队状态"""
    key = shop_id
    if key in _QUEUES:
        return _QUEUES[key]

    # 初始排队数：午餐(11-14)少、晚餐(17-20)多
    if 11 <= hour <= 14:
        initial = random.randint(3, 8)
    elif 17 <= hour <= 20:
        initial = random.randint(8, 20)
    else:
        initial = random.randint(2, 6)

    # 消化速度（分钟/桌）：午餐快、晚餐慢
    digest = random.randint(3, 5) if 11 <= hour <= 14 else random.randint(4, 6)

    _QUEUES[key] = {
        "shop_id": shop_id,
        "shop_name": shop_name,
        "tables_ahead": initial,
        "digest_rate_minutes": digest,
        "last_updated": time.time(),
        "active_queues": [],
    }
    return _QUEUES[key]


def enqueue(
    shop_id: str,
    shop_name: str = "",
    party_size: int = 2,
    current_hour: int = 12,
) -> dict:
    """
    排号入队。

    参数:
        shop_id: 店铺 ID
        shop_name: 店铺名称
        party_size: 用餐人数
        current_hour: 当前小时（用于模拟）

    返回:
        dict: 排号结果
    """
    if not shop_id:
        return {"status": "ERROR", "message": "缺少 shop_id"}

    state = _get_or_create_shop_state(shop_id, shop_name, current_hour)
    qid = _new_queue_id()

    wait_min = state["tables_ahead"] * state["digest_rate_minutes"]
    entry = {
        "queue_id": qid,
        "shop_id": shop_id,
        "shop_name": shop_name or state["shop_name"],
        "party_size": party_size,
        "position": len(state["active_queues"]) + 1,
        "enqueued_at": time.time(),
    }
    state["active_queues"].append(entry)

    return {
        "status": "SUCCESS",
        "queue_id": qid,
        "shop_name": state["shop_name"],
        "tables_ahead": state["tables_ahead"],
        "estimated_wait_minutes": wait_min,
        "party_size": party_size,
    }


def query(queue_id: str) -> dict:
    """
    查询单个排号状态。

    参数:
        queue_id: 排号 ID

    返回:
        dict: 排队状态
    """
    for shop_id, state in _QUEUES.items():
        for q in state["active_queues"]:
            if q["queue_id"] == queue_id:
                # 更新时间消化
                _digest(state)
                wait_min = state["tables_ahead"] * state["digest_rate_minutes"]
                alert = None
                if state["tables_ahead"] <= 1:
                    alert = "⚠️ 只剩 1 桌！请立即出发！"
                elif state["tables_ahead"] <= 3:
                    alert = f"还剩 {state['tables_ahead']} 桌（约{wait_min}分钟），建议叫车出发"
                elif state["tables_ahead"] <= 5:
                    alert = f"前方还有 {state['tables_ahead']} 桌，可以准备出门了"

                return {
                    "status": "SUCCESS",
                    "queue_id": queue_id,
                    "shop_name": state["shop_name"],
                    "tables_ahead": state["tables_ahead"],
                    "estimated_wait_minutes": wait_min,
                    "alert": alert,
                }
    return {"status": "ERROR", "message": "排号不存在"}


def poll_all(current_hour: int = None) -> dict:
    """
    批量轮询所有排队状态，返回提醒列表。

    参数:
        current_hour: 当前小时

    返回:
        dict: 所有排队状态 + 提醒
    """
    queues = []
    alerts = []

    for shop_id, state in list(_QUEUES.items()):
        _digest(state, current_hour)
        wait_min = state["tables_ahead"] * state["digest_rate_minutes"]

        name = state["shop_name"]
        item = {
            "shop_id": shop_id,
            "shop_name": name,
            "tables_ahead": state["tables_ahead"],
            "estimated_wait_minutes": wait_min,
            "active_queues": len(state["active_queues"]),
        }

        if state["tables_ahead"] <= 1:
            item["alert"] = "立即出发"
            alerts.append(f"🚨 {name}: 只剩 1 桌！请立即出发！")
            for q in state["active_queues"]:
                alerts.append(f"  排号 {q['queue_id']}: 你的桌位即将就绪")
        elif state["tables_ahead"] <= 3:
            item["alert"] = "即将轮到"
            alerts.append(f"📢 {name}: 还剩 {state['tables_ahead']} 桌（约{wait_min}分钟），建议叫车出发")
        elif state["tables_ahead"] <= 5:
            item["alert"] = "准备出门"
            alerts.append(f"⏰ {name}: 前方 {state['tables_ahead']} 桌（约{wait_min}分钟），可以准备出门")

        queues.append(item)

    # 清理已完成排队的店铺（0桌且无活跃排号）
    # 保留以便查询

    return {
        "status": "SUCCESS",
        "queues": queues,
        "alerts": alerts,
    }


def _digest(state: dict, current_hour: int = None):
    """消化排队：根据时间流逝减少前方桌数"""
    now = time.time()
    elapsed_min = (now - state["last_updated"]) / 60.0

    if elapsed_min <= 0:
        return

    # 每 digest_rate_minutes 消化 1 桌
    tables_done = int(elapsed_min / state["digest_rate_minutes"])
    if tables_done > 0:
        state["tables_ahead"] = max(0, state["tables_ahead"] - tables_done)
        state["last_updated"] += tables_done * state["digest_rate_minutes"] * 60

    # 如果当前是餐饮高峰，动态调整消化速度
    if current_hour and (12 <= current_hour <= 13 or 18 <= current_hour <= 19):
        state["digest_rate_minutes"] = max(3, state["digest_rate_minutes"] - 1)


def handle(action: str, **kwargs) -> dict:
    """
    统一入口：根据 action 分发。

    支持: enqueue, query, poll_all, reset
    """
    if action == "enqueue":
        return enqueue(**kwargs)
    elif action == "query":
        return query(kwargs.get("queue_id", ""))
    elif action == "poll_all":
        return poll_all(kwargs.get("current_hour"))
    elif action == "reset":
        _reset_all()
        return {"status": "SUCCESS", "message": "已重置"}
    else:
        return {"status": "ERROR", "message": f"未知 action: {action}"}
