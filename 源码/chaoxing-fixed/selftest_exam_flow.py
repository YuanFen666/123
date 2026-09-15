# -*- coding: utf-8 -*-
"""
考试作答流程自测（离线，不联网）
================================
锁住一个实际踩到的 bug（2026-09-15）：

    整卷模式答完 85 题并【已自动交卷】之后，程序**又退回去走单题模式**，
    再答一遍 → 被服务端一句「提交失败：考试已经提交」顶回来 →
    还写出 exam_answers.md / exam_submit_debug.json 一堆无用文件。
    用户看到的是「明明交卷成功了，后面却弹出一大段报错」。

根因：ExamTaker.start() 在整卷模式下会 **return** 结果，而 run() 没接这个返回值。
修法：run() 接住返回值，非 None 就说明整卷模式已经做完，直接返回。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.exam_take import ExamTaker


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


class Stat(dict):
    """单题模式会读写各种统计键，缺键时按 0 算，免得自测被无关的 KeyError 打断"""
    def __missing__(self, k):
        return 0


class FakeTaker(ExamTaker):
    """只替换网络动作，专门用来验证 run() 的分支走向"""

    def __init__(self, start_returns):
        self.title = "人工智能与法律"
        self.exam_id = "10846513"
        self.auto_submit = True
        self.tiku = None            # run() 会把它交给 tune_tiku_for_exam（已打桩）
        self.stats = Stat()         # 单题模式分支会往里写统计
        self._start_returns = start_returns
        self.calls = []

    def load_cover(self):
        self.calls.append("load_cover")

    def solve_captcha(self):
        self.calls.append("solve_captcha")

    def start(self):
        self.calls.append("start")
        return self._start_returns

    def answer_sheet(self):
        # 走到这里就说明「整卷做完后又进了单题模式」= 就是那个 bug
        self.calls.append("answer_sheet")
        raise AssertionError("不该进入单题模式！")

    def remain_min(self):
        self.calls.append("remain_min")
        return None

    def fetch(self, i):
        self.calls.append("fetch")
        raise StopIteration

    def _dump_answers(self, collected):
        self.calls.append("_dump_answers")

    @property
    def min_remain_min(self):
        return 0

    @property
    def max_questions(self):
        return 3

    @property
    def overwrite(self):
        return False


def main():
    res = []
    print("=" * 92)
    print(" 考试作答流程 自测（整卷模式后不得回退单题模式）")
    print("=" * 92)

    # 1) 整卷模式返回了结果 -> 必须立刻收工，绝不碰单题模式
    import api.exam_take as E
    orig_tune = E.tune_tiku_for_exam
    E.tune_tiku_for_exam = lambda tiku: None       # 别去动真题库
    try:
        t = FakeTaker({"total": 85, "saved": 85})
        out = t.run()
        ok = ("answer_sheet" not in t.calls and out == {"total": 85, "saved": 85})
        res.append(report("整卷模式做完 → 直接返回，不进入单题模式", ok,
                          "  调用序列 = {}".format(" → ".join(t.calls))))

        # 2) start() 返回 None（不是整卷模式）-> 应该继续走单题模式
        #    这里只断言「有没有走到 answer_sheet」——那才是要锁住的行为；
        #    假对象没必要把整条单题流程都撑起来，所以吞掉中途的无关异常。
        t2 = FakeTaker(None)
        try:
            t2.run()
        except Exception:  # noqa: BLE001
            pass
        entered = "answer_sheet" in t2.calls
        res.append(report("非整卷模式 → 仍然正常进入单题模式（没误伤）", entered,
                          "  调用序列 = {}".format(" → ".join(t2.calls))))

        # 3) 两种情况下都要先把前置步骤做完
        res.append(report("前置步骤（封面/验证码/开考）在任何分支前都已执行",
                          t.calls[:3] == ["load_cover", "solve_captcha", "start"],
                          "  {}".format(t.calls[:3])))
    finally:
        E.tune_tiku_for_exam = orig_tune

    print("-" * 92)
    print(" 结果: {} passed, {} failed".format(sum(res), len(res) - sum(res)))
    print("=" * 92)
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
