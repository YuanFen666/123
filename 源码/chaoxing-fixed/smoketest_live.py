# -*- coding: utf-8 -*-
"""
真实网络冒烟测试（会消耗 ANEVOL 额度，题目很少）
================================================
用真实的 config.ini 配好题库，直接问两个题库，验证：
  1. TikuChain 的完整链路（含缓存、类型校验、答案归一化）能走通；
  2. 两个题库各自的真实响应、耗时、返回内容；
  3. 题库返回的答案最终能不能落到正确的选项字母上（这一步才是真正决定对错的关键）。

用法：
    python smoketest_live.py                    # 默认读 ..\\..\\config.ini
    python smoketest_live.py -c D:\\path\\config.ini
"""
import argparse
import configparser
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.answer import Tiku, TikuChain, TikuIcodef, TikuAnevol
from api.answer_check import check_answer, cut


# ---------------- chaoxing 侧答案 -> 选项字母（复刻 base.py::study_work） ----------------
def clean_res(res):
    if isinstance(res, str):
        res = [res]
    return [re.sub(r"^[A-Za-z]|[.,!?;:，。！？；：]", "", c).strip() for c in res]


def is_subsequence(a, o):
    it = iter(o)
    return all(c in it for c in a)


def resolve_single(options_str, res):
    options_list = cut(options_str)
    if options_list is None:
        return ""
    for o in options_list:
        if is_subsequence(clean_res(res)[0], o):
            return o[:1]
    return ""


def resolve_multiple(options_str, res):
    options_list, res_list = cut(options_str), cut(res)
    if res_list is None or options_list is None:
        return ""
    answer = ""
    for _a in clean_res(res_list):
        for o in options_list:
            if is_subsequence(_a, o):
                answer += o[:1]
    return "".join(sorted(answer))


def final_answer(qtype, options, res):
    if not res:
        return "(无 -> 随机作答)"
    if qtype == "single":
        return resolve_single(options, res) or "(匹配失败 -> 随机作答)"
    if qtype == "multiple":
        return resolve_multiple(options, res) or "(匹配失败 -> 随机作答)"
    return res


QUESTIONS = [
    ("单选", {"title": "中国最早的一部诗歌总集是（）",
              "options": "A. 《诗经》\nB. 《易经》\nC. 《论语》\nD. 《楚辞》",
              "type": "single"}, "A"),
    ("多选", {"title": "马克思主义的三个基本组成部分是（）",
              "options": "A. 科学社会主义理论\nB. 马克思主义哲学\nC. 马克思主义政治经济学\nD. 空想社会主义",
              "type": "multiple"}, "ABC"),
    ("填空", {"title": "1+1等于几？", "options": "", "type": "completion"}, "2"),
]


def main():
    ap = argparse.ArgumentParser()
    default_conf = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config.ini"))
    ap.add_argument("-c", "--config", default=default_conf)
    args = ap.parse_args()

    print("=" * 96)
    print(" 真实网络冒烟测试")
    print(" 配置: {}".format(args.config))
    print("=" * 96)

    cfg = configparser.ConfigParser()
    if not cfg.read(args.config, encoding="utf8"):
        print("!! 读不到配置文件: {}".format(args.config))
        return 1
    tiku_conf = dict(cfg.items("tiku"))
    print(" provider = {}".format(tiku_conf.get("provider")))

    # ---------------- 1. 题库链完整链路 ----------------
    print("\n" + "-" * 96)
    print(" [1] 题库链完整链路（Tiku.query：含缓存 / 类型校验 / 答案归一化）")
    print("-" * 96)
    base = Tiku()
    base.config_set(tiku_conf)
    chain = base.get_tiku_from_config()
    chain.init_tiku()
    if isinstance(chain, TikuChain):
        print(" 查询顺序: {}".format(" -> ".join(chain.provider_names)))

    chain_ok = 0
    for label, q, expect in QUESTIONS:
        t0 = time.time()
        ans = chain.query(dict(q))
        dt = time.time() - t0
        fin = final_answer(q["type"], q["options"], ans)
        ok = (fin == expect)
        chain_ok += ok
        print(" [{}] {:<4} 耗时{:>5.1f}s  题库返回={!r:<28} 最终提交={!r} (期望 {!r})".format(
            "PASS" if ok else "FAIL", label, dt, ans, fin, expect))

    # ---------------- 2. 各题库单独实测（绕开缓存，强制真实请求） ----------------
    print("\n" + "-" * 96)
    print(" [2] 各题库单独实测（走 _query，绕开缓存，强制真实请求）")
    print("-" * 96)

    for cls in (TikuIcodef, TikuAnevol):
        t = cls()
        t.config_set(tiku_conf)
        t.init_tiku()
        t.DISABLE = False
        print("\n >> {}（{}）".format(cls.__name__, t.name))
        for label, q, expect in QUESTIONS:
            t0 = time.time()
            raw = t._query(dict(q))
            dt = time.time() - t0
            gate = bool(raw) and check_answer(raw, q["type"], t)
            fin = final_answer(q["type"], q["options"], raw)
            print("    [{}] {:<4} 耗时{:>5.1f}s  返回={!r:<30} 校验={} 最终提交={!r} (期望 {!r})".format(
                "PASS" if fin == expect else "MISS", label, dt, raw, "通过" if gate else "不通过",
                fin, expect))

    # ---------------- 3. AI 兜底专项 ----------------
    # 题库基本不可能收录的题，专门用来逼出第三道防线
    print("\n" + "-" * 96)
    print(" [3] AI 兜底专项（前两个题库搜不到时，看 AI 是否接管）")
    print("-" * 96)

    cold_q = {"title": "在 Python 语言中，用于定义函数的关键字是下列哪一个？",
              "options": "A. func\nB. def\nC. function\nD. define", "type": "single"}

    # 3.1 先确认两个题库确实搜不到（绕开缓存，强制真实请求）
    for cls in (TikuIcodef, TikuAnevol):
        t = cls()
        t.config_set(tiku_conf)
        t.init_tiku()
        t0 = time.time()
        raw = t._query(dict(cold_q))
        print("    {}: 返回={!r}  耗时{:.1f}s".format(cls.__name__, raw, time.time() - t0))

    # 3.2 只用 AI 组一条链，直接验证 AI 兜底这条通路（含缓存/校验/归一化）
    from api.answer import AI
    ai_conf = dict(tiku_conf)
    ai_conf["provider"] = "AI"
    ai_base = Tiku()
    ai_base.config_set(ai_conf)
    ai_chain = ai_base.get_tiku_from_config()
    ai_chain.init_tiku()
    if getattr(ai_chain, "DISABLE", False):
        print("    !! AI 未启用（config.ini 里的 key/endpoint/model 没配全）")
    else:
        t0 = time.time()
        raw = ai_chain.query(dict(cold_q))
        dt = time.time() - t0
        fin = final_answer("single", cold_q["options"], raw)
        print("    [{}] 纯 AI 链: 返回={!r} 耗时{:.1f}s 最终提交={!r} (期望 'B')".format(
            "PASS" if fin == "B" else "FAIL", raw, dt, fin))

    # 3.3 完整三道防线：这一题正常情况下应该由 AI 给出答案
    t0 = time.time()
    ans = chain.query(dict(cold_q))
    dt = time.time() - t0
    fin = final_answer("single", cold_q["options"], ans)
    print("    [{}] 完整题库链: 返回={!r} 耗时{:.1f}s 最终提交={!r} (期望 'B')".format(
        "PASS" if fin == "B" else "FAIL", ans, dt, fin))

    print("\n" + "=" * 96)
    print(" 题库链结果: {} / {} 命中".format(chain_ok, len(QUESTIONS)))
    print("=" * 96)
    return 0 if chain_ok else 1


if __name__ == "__main__":
    sys.exit(main())
