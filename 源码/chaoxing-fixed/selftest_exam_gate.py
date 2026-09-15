# -*- coding: utf-8 -*-
"""
考试门槛等待逻辑自测（离线，不联网）
====================================
背景（2026-09-15 实测，账号 #3）：刷完课进度页立刻 100%，但进考场被拒
「该考试教师已设置章节任务点未完成90%，不能参加考试」；过一会再体检就变 √。
→ 进度页实时、门槛用服务端聚合缓存，汇总有延迟。

所以门槛被拒时要**自己轮询门槛**（拿门槛当探测最准），追上了自动开考；
但「已交卷/已过期/要人脸」是永久性的，绝不能傻等。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.exam_take import is_transient_gate_reason, wait_for_gate


class R:
    """假的 ExamReady"""
    def __init__(self, can_start, reason=""):
        self.can_start = can_start
        self.reason = reason


class E:
    name = "人工智能与法律"


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


def main():
    res = []
    print("=" * 92)
    print(" 考试门槛等待逻辑 自测")
    print("=" * 92)

    # ---- 1. 分类：哪些原因值得等 ----
    transient = [
        "该考试教师已设置章节任务点未完成90%，不能参加考试",   # ← 用户实际遇到的原话
        "章节任务点未达到要求",
        "任务点完成度不足",
        "该考试需要完成全部章节",
    ]
    permanent = [
        "这场考试已经交过卷了（服务端跳转到成绩页）",
        "已过期",
        "这场考试要求人脸识别，本项目不处理",
        "待批阅",
        "",
        "莫名其妙的原因",
    ]
    ok = all(is_transient_gate_reason(r) for r in transient)
    res.append(report("可恢复原因（任务点/章节没到）判为「可以等」", ok,
                      "  {} 条".format(len(transient))))
    ok = all(not is_transient_gate_reason(r) for r in permanent)
    res.append(report("永久原因（已交卷/过期/人脸/不认识）判为「不能等」", ok,
                      "  {} 条".format(len(permanent))))

    # ---- 2. 一探就通过：不该有多余等待 ----
    calls = {"n": 0}
    def p_ready():
        calls["n"] += 1
        return R(True)
    t0 = time.time()
    r = wait_for_gate(p_ready, E(), max_wait=5, poll=0.05)
    dt = time.time() - t0
    res.append(report("一开始就能考 → 立即通过、不等待", r.can_start and calls["n"] == 1 and dt < 0.5,
                      "  探测 {} 次，耗时 {:.2f}s".format(calls["n"], dt)))

    # ---- 3. 第 3 次才通过：应该一直轮询到通过 ----
    calls = {"n": 0}
    def p_late():
        calls["n"] += 1
        return R(calls["n"] >= 3, "" if calls["n"] >= 3 else "章节任务点未完成90%")
    logs = []
    t0 = time.time()
    r = wait_for_gate(p_late, E(), max_wait=5, poll=0.05, log=logs.append)
    dt = time.time() - t0
    res.append(report("延迟恢复 → 轮询到通过为止，自动开考", r.can_start and calls["n"] == 3,
                      "  探测 {} 次，耗时 {:.2f}s".format(calls["n"], dt)))
    res.append(report("等待期间有进度日志（不是静默卡住）", len(logs) >= 2,
                      "  {} 条日志".format(len(logs))))

    # ---- 4. 一直不通过：应该在预算内放弃，不能无限等 ----
    calls = {"n": 0}
    def p_never():
        calls["n"] += 1
        return R(False, "章节任务点未完成90%")
    t0 = time.time()
    r = wait_for_gate(p_never, E(), max_wait=0.35, poll=0.1, log=lambda m: None)
    dt = time.time() - t0
    res.append(report("始终不通过 → 按预算放弃（不无限等）",
                      (r is None or not r.can_start) and dt < 1.5,
                      "  探测 {} 次，耗时 {:.2f}s（上限 0.35s）".format(calls["n"], dt)))

    # ---- 5. 探测本身抛异常：不能把流程搞挂 ----
    calls = {"n": 0}
    def p_boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("网络抖了一下")
        return R(True)
    try:
        r = wait_for_gate(p_boom, E(), max_wait=5, poll=0.05, log=lambda m: None)
        res.append(report("探测抛异常 → 吃掉异常并继续重试", r is not None and r.can_start,
                          "  探测 {} 次".format(calls["n"])))
    except Exception as e:  # noqa: BLE001
        res.append(report("探测抛异常 → 吃掉异常并继续重试", False, "  抛出来了: " + repr(e)))

    # ---- 6. max_wait = 0：表示不等待，只探一次 ----
    calls = {"n": 0}
    def p_once():
        calls["n"] += 1
        return R(False, "章节任务点未完成90%")
    r = wait_for_gate(p_once, E(), max_wait=0, poll=120, log=lambda m: None)
    res.append(report("exam_gate_wait=0 → 只探一次、不等待（尊重用户配置）",
                      calls["n"] == 1 and not r.can_start, "  探测 {} 次".format(calls["n"])))

    print("-" * 92)
    print(" 结果: {} passed, {} failed".format(sum(res), len(res) - sum(res)))
    print("=" * 92)
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
