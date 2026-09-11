# -*- coding: utf-8 -*-
"""
TikuAnevol 离线自测
====================
用假响应走通「ANEVOL 返回 -> Tiku.query 校验 -> chaoxing 最终提交的选项字母」整条链路，
不依赖网络，也不依赖超星接口。

为什么必须测到"最终字母"这一步：
  ANEVOL 对选择题返回的是字母("B")，但 chaoxing 的 clean_res() 会把答案开头的字母删掉，
  删成空串后 is_subsequence("", 选项) 恒为真 -> 永远匹配到第一个选项。
  这个 bug 只有把整个链路跑一遍才看得出来。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api.answer as answer_mod
from api.answer import TikuAnevol
from api.answer_check import check_answer, cut


# ---------------------------------------------------------------- chaoxing 侧逻辑复刻
def clean_res(res):
    if isinstance(res, str):
        res = [res]
    return [re.sub(r"^[A-Za-z]|[.,!?;:，。！？；：]", "", c).strip() for c in res]


def is_subsequence(a, o):
    it = iter(o)
    return all(c in it for c in a)


def resolve_single(options_str, res):
    """复刻 base.py study_work 里单选答案 -> 提交字母 的逻辑。"""
    options_list = cut(options_str)
    if options_list is None:
        return ""
    t_res = clean_res(res)
    for o in options_list:
        if is_subsequence(t_res[0], o):
            return o[:1]
    return ""


def resolve_multiple(options_str, res):
    """复刻 base.py study_work 里多选答案 -> 提交字母 的逻辑。"""
    options_list = cut(options_str)
    res_list = cut(res)
    if res_list is None or options_list is None:
        return ""
    answer = ""
    for _a in clean_res(res_list):
        for o in options_list:
            if is_subsequence(_a, o):
                answer += o[:1]
    return "".join(sorted(answer))


# ---------------------------------------------------------------- 假 ANEVOL 响应
class FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def make_tiku(anevol_answer, status=200):
    """构造一个 TikuAnevol 实例，并把 requests.post 换成本地假响应。"""
    conf = {
        "provider": "TikuAnevol",
        "submit": "true",
        "cover_rate": "0.8",
        "true_list": "正确,对,√,是",
        "false_list": "错误,错,×,否,不对,不正确",
        "url": "",
        "tokens": "TEST_TOKEN_1234567890",
    }
    t = TikuAnevol()
    t.config_set(conf)
    t.init_tiku()  # 必须走完整初始化：它会填 true_list / false_list / SUBMIT / COVER_RATE

    body = (
        {"code": 1, "answer": anevol_answer, "using_backup": False}
        if status == 200
        else {"code": 0, "msg": "AI服务暂时不可用，请稍后重试"}
    )

    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return FakeResp(status, body)

    answer_mod.requests.post = fake_post
    return t, captured


# ---------------------------------------------------------------- 用例
OPTIONS_SINGLE = "A. 实事求是, 群众路线\nB. 中国共产党的领导\nC. 北京、上海"
OPTIONS_MULTI = "A. 实事求是\nB. 群众路线\nC. 独立自主"

CASES = [
    # (说明, 题目类型, 选项, ANEVOL返回, 期望最终提交字母/内容)
    ("单选-字母B, 且选项无分隔符", "single", OPTIONS_SINGLE, "B", "B"),
    ("单选-字母C, 选项含顿号(净化后仍需匹配正确项)", "single", OPTIONS_SINGLE, "C", "C"),
    ("单选-ANEVOL直接返回选项原文", "single", OPTIONS_SINGLE, "中国共产党的领导", "B"),
    ("多选-字母AC", "multiple", OPTIONS_MULTI, "AC", "AC"),
    ("多选-A,C 带分隔", "multiple", OPTIONS_MULTI, "A, C", "AC"),
    ("判断-符号√", "judgement", "A. 正确\nB. 错误", "√", "正确"),
    ("判断-文字错误", "judgement", "A. 正确\nB. 错误", "错误", "错误"),
    ("填空-文本", "completion", "", "社会主义初级阶段", "社会主义初级阶段"),
]


def main():
    print("=" * 78)
    print(" TikuAnevol 自测（离线，假 ANEVOL 响应）")
    print("=" * 78)

    passed = failed = 0
    for desc, qtype, options, anevol_ans, expect in CASES:
        tiku, captured = make_tiku(anevol_ans)
        q_info = {"title": "测试题目", "options": options, "type": qtype}

        raw = tiku._query(q_info)
        gate = check_answer(raw, qtype, tiku) if raw else False

        if qtype == "single":
            final = resolve_single(options, raw) if raw else ""
        elif qtype == "multiple":
            final = resolve_multiple(options, raw) if raw else ""
        else:
            final = raw or ""

        ok = (final == expect) and gate
        passed += ok
        failed += (not ok)

        print("\n[{}] {}".format("PASS" if ok else "FAIL", desc))
        print("   ANEVOL 返回 : {}".format(repr(anevol_ans)))
        print("   _query 输出 : {}".format(repr(raw)))
        print("   类型校验    : {}".format("通过" if gate else "不通过(check_answer)"))
        print("   最终提交    : {}  (期望 {})".format(repr(final), repr(expect)))
        if captured.get("json"):
            print("   发给ANEVOL  : {}".format(
                json.dumps({k: v for k, v in captured["json"].items() if k != "options"},
                           ensure_ascii=False)))

    # ---- 反例：证明"直接返回字母"为什么不行（原版行为） ----
    print("\n" + "-" * 78)
    print(" 反例：如果像其它题库那样直接把字母当作答案返回")
    print("-" * 78)
    buggy = resolve_single(OPTIONS_SINGLE, "B")
    print("   ANEVOL 返回 'B' -> clean_res 后变成 '' : {}".format(repr(clean_res("B"))))
    print("   is_subsequence('', 选项A) = True  -> 命中第一个选项")
    print("   最终提交 = {}  (期望 B)  ############ 这就是必须还原成选项原文的原因".format(repr(buggy)))

    # ---- 反例：证明净化掉分隔符是必要的 ----
    print("\n" + "-" * 78)
    print(" 反例：如果单选直接返回带顿号的选项原文")
    print("-" * 78)
    raw_with_punct = "北京、上海"
    print("   cut({!r}) = {}  -> len != 1 -> check_single 失败".format(
        raw_with_punct, cut(raw_with_punct)))
    print("   check_answer({!r}, 'single') = {}".format(
        raw_with_punct, check_answer(raw_with_punct, "single", make_tiku("x")[0])))
    print("   结果是答案被丢弃 -> 回退随机答题")

    print("\n" + "=" * 78)
    print(" 结果: {} passed, {} failed".format(passed, failed))
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
