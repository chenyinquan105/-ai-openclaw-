"""
TDD: 确认并执行 → 起床前1分钟暂停 → 防坑指南
测试文件 — Seam 1, 2, 3
"""
import requests
import json
import sys

BASE = "http://localhost:5000"
PASS = 0
FAIL = 0

def test(name, fn):
    global PASS, FAIL
    try:
        result = fn()
        PASS += 1
        extra = f"      {result}" if result else ""
        print(f"  ✅ {name}{extra}")
    except AssertionError as e:
        FAIL += 1
        print(f"  ❌ {name}: {e}")
    except Exception as e:
        FAIL += 1
        print(f"  💥 {name}: {type(e).__name__}: {e}")

def assert_eq(actual, expected, msg=""):
    assert actual == expected, f"{msg} 期望={expected!r}, 实际={actual!r}"

def assert_in(substring, container, msg=""):
    assert substring in str(container), f"{msg} '{substring}' not in {container!r}"

def assert_true(condition, msg=""):
    assert condition, msg


# ============================================================
# Seam 1: clock_init 动态时间
# ============================================================
def run_seam_1():
    print("\n" + "=" * 60)
    print("Seam 1: POST /api/clock/init — 动态 WAKE_UP 时间")
    print("=" * 60)

    def t11():
        r = requests.post(f"{BASE}/api/clock/init", json={
            "initial_time": "05:42:00", "start_date": "2026-07-20",
            "schedule_nodes": [
                {"time": "05:43", "action": "WAKE_UP", "type": "WAKE_UP",
                 "label": "起床", "repeat": "once", "date": "2026-07-20", "day_index": 0},
            ]
        })
        d = r.json()
        assert_eq(r.status_code, 200, "HTTP status")
        assert_eq(d["is_running"], False, "is_running")
        assert_eq(d["current_date"], "2026-07-20", "current_date")
        assert_in("05:42", d["virtual_time"], "virtual_time")
        return f"time={d['virtual_time']}, date={d['current_date_display']}, paused={d['is_running']}"
    test("1.1 init 05:42:00 (WAKE_UP=05:43), 暂停", t11)

    def t12():
        r = requests.post(f"{BASE}/api/clock/init", json={
            "initial_time": "06:29", "start_date": "2026-07-21",
            "schedule_nodes": [
                {"time": "06:30", "action": "WAKE_UP", "type": "WAKE_UP",
                 "label": "起床", "repeat": "once", "date": "2026-07-21", "day_index": 0},
            ]
        })
        d = r.json()
        assert_eq(r.status_code, 200, "HTTP status")
        assert_eq(d["is_running"], False, "is_running")
        assert_in("06:29", d["virtual_time"], "virtual_time")
    test("1.2 init 06:29 (WAKE_UP=06:30), HH:MM 格式", t12)

    def t13():
        r = requests.post(f"{BASE}/api/clock/init", json={
            "initial_time": "07:29:00", "start_date": "2026-07-16",
            "schedule_nodes": [
                {"time": "07:30", "action": "WAKE_UP", "type": "WAKE_UP",
                 "label": "起床", "repeat": "once", "date": "2026-07-16", "day_index": 0},
            ]
        })
        d = r.json()
        assert_eq(r.status_code, 200, "HTTP status")
        assert_eq(d["is_running"], False, "is_running")
        assert_in("07:29", d["virtual_time"], "virtual_time")
    test("1.3 init 07:29:00 (WAKE_UP=07:30), HH:MM:SS", t13)

    def t14():
        r = requests.post(f"{BASE}/api/clock/init", json={
            "initial_time": "23:59:00", "start_date": "2026-07-22",
            "schedule_nodes": [
                {"time": "00:00", "action": "WAKE_UP", "type": "WAKE_UP",
                 "label": "凌晨出发", "repeat": "once", "date": "2026-07-22", "day_index": 0},
            ]
        })
        d = r.json()
        assert_eq(r.status_code, 200, "HTTP status")
        assert_eq(d["is_running"], False, "is_running")
        assert_in("23:59", d["virtual_time"], "virtual_time")
    test("1.4 init 23:59:00 (WAKE_UP=00:00), 跨天", t14)

    def t15():
        r = requests.get(f"{BASE}/api/clock/status")
        d = r.json()
        assert_true(d["schedule_count"] >= 1, f"schedule_count={d['schedule_count']}")
    test("1.5 schedule_nodes 已存储", t15)


# ============================================================
# Seam 2: clock/offset 经过 WAKE_UP 触发 PITFALL_GUIDE
# ============================================================
def run_seam_2():
    print("\n" + "=" * 60)
    print("Seam 2: POST /api/clock/offset — 经过 WAKE_UP 触发 PITFALL_GUIDE")
    print("=" * 60)

    # Setup: init → consume stale → offset to trigger WAKE_UP → fetch events ONCE
    requests.post(f"{BASE}/api/clock/init", json={
        "initial_time": "07:29:00", "start_date": "2026-07-16",
        "schedule_nodes": [
            {"time": "07:30", "action": "WAKE_UP", "type": "WAKE_UP",
             "label": "起床", "repeat": "once", "date": "2026-07-16", "day_index": 0},
        ]
    })
    requests.get(f"{BASE}/api/clock/events")

    # Trigger once
    _off_res = requests.post(f"{BASE}/api/clock/offset", json={"offset_minutes": 2})
    # Fetch events once — shared across seam 2 assertions (events are pop-on-read)
    _ev_res = requests.get(f"{BASE}/api/clock/events")
    _events = _ev_res.json().get("events", [])
    _pitfall = next((e for e in _events if isinstance(e, dict) and e.get("type") == "PITFALL_GUIDE"), None)

    def t21():
        actions = [n.get("action") for n in _off_res.json().get("triggered_nodes", [])]
        assert_in("WAKE_UP", actions, "triggered_nodes")
        return f"triggered: {actions}"
    test("2.1 offset +2min → WAKE_UP 触发", t21)

    def t22():
        types = [e.get("type") for e in _events if isinstance(e, dict)]
        assert_in("PITFALL_GUIDE", types, "event types")
        return f"types: {types}"
    test("2.2 events 包含 PITFALL_GUIDE", t22)

    def t23():
        assert_true(_pitfall is not None, "找不到 PITFALL_GUIDE")
        gr = _pitfall.get("global_reminders", [])
        assert_true(len(gr) > 0, f"global_reminders 为空")
        return f"global_reminders: {len(gr)} 条"
    test("2.3 PITFALL_GUIDE 有 global_reminders", t23)

    def t24():
        assert_true(_pitfall is not None, "找不到 PITFALL_GUIDE")
        assert_eq(_pitfall.get("label", ""), "今日防坑指南", "label")
        assert_in("早上好", _pitfall.get("message", ""), "message")
        assert_true(_pitfall.get("id", "").startswith("pitfall_"), f"id={_pitfall.get('id')}")
    test("2.4 PITFALL_GUIDE 字段完整", t24)


# ============================================================
# Seam 3: GET /api/clock/events 返回结构正确
# ============================================================
def run_seam_3():
    print("\n" + "=" * 60)
    print("Seam 3: GET /api/clock/events — 结构正确")
    print("=" * 60)

    # Setup: re-init + trigger, fetch once
    requests.post(f"{BASE}/api/clock/init", json={
        "initial_time": "07:59:00", "start_date": "2026-07-16",
        "schedule_nodes": [
            {"time": "08:00", "action": "WAKE_UP", "type": "WAKE_UP",
             "label": "起床", "repeat": "once", "date": "2026-07-16", "day_index": 0},
        ]
    })
    requests.get(f"{BASE}/api/clock/events")
    requests.post(f"{BASE}/api/clock/offset", json={"offset_minutes": 2})
    _ev3 = requests.get(f"{BASE}/api/clock/events").json()
    _events3 = _ev3.get("events", [])
    _pitfall3 = next((e for e in _events3 if isinstance(e, dict) and e.get("type") == "PITFALL_GUIDE"), None)

    def t31():
        assert_in("events", list(_ev3.keys()), "response keys")
        assert_in("virtual_time", list(_ev3.keys()), "response keys")
    test("3.1 response 包含 events + virtual_time", t31)

    def t32():
        assert_true(isinstance(_events3, list), "events is list")
    test("3.2 events 是列表", t32)

    def t33():
        assert_true(_pitfall3 is not None, "找不到 PITFALL_GUIDE")
        required = ["type", "label", "message", "date", "global_reminders", "destination_tips"]
        for key in required:
            assert_in(key, list(_pitfall3.keys()), f"缺少字段: {key}")
        return (f"destinations={_pitfall3.get('destinations', [])}, "
                f"tips={len(_pitfall3.get('destination_tips', []))}目的地, "
                f"reminders={len(_pitfall3.get('global_reminders', []))}条")
    test("3.3 PITFALL_GUIDE 所有字段齐全", t33)


# ============================================================
# Run all
# ============================================================
if __name__ == "__main__":
    try:
        r = requests.get(f"{BASE}/api/clock/status", timeout=3)
        assert r.status_code == 200
    except Exception:
        print("❌ 服务器未运行！请先启动: python3 server.py")
        sys.exit(1)

    run_seam_1()
    run_seam_2()
    run_seam_3()

    print("\n" + "=" * 60)
    print(f"  结果: {PASS} 通过, {FAIL} 失败, 共 {PASS+FAIL} 项")
    print("=" * 60)

    if FAIL > 0:
        sys.exit(1)
