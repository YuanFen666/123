# -*- coding: utf-8 -*-
"""
TikuChain（多题库顺序回退）离线自测
====================================
配置              provider = TikuIcodef,TikuAnevol
语义              按顺序查询，第一个给出「类型校验通过」的答案的题库胜出，后面的不再请求。

验证点：
  1. 先问 icodef（快、免费），命中后 ANEVOL 一次都不该被请求（省钱省时间）；
  2. icodef 搜不到（code=-1）时自动回退到 ANEVOL；
  3. 两个都搜不到 → 返回 None（上层走随机作答 + 不提交）；
  4. 链里某个题库抛异常，不能拖垮整条链，要继续问下一个；
  5. 配置里写错类名只跳过它，不影响其它题库；
  6. get_tiku_from_config() 能按逗号自动识别成题库链，单个类名时行为和原版一致。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api.answer as A
from api.answer import Tiku, TikuChain, TikuIcodef, TikuAnevol
from api.answer_check import check_answer

ICODEF_URL = "https://cx.icodef.com/wyn-nb?v=4"

CONF = {
    "provider": "TikuIcodef,TikuAnevol",
    "submit": "true",
    "cover_rate": "0.8",
    "true_list": "正确,对,√,是",
    "false_list": "错误,错,×,否,不对,不正确",
    "icodef_url": ICODEF_URL,
    "icodef_min_interval": "0",
    "url": "",
    "tokens": "TEST_TOKEN",
    "anevol_min_interval": "0",
}

OPTIONS = "A. 实事求是, 群众路线\nB. 中国共产党的领导\nC. 北京、上海"


class FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class NoCache:
    """禁用缓存，避免自测往 cache.json 里写脏数据。"""

    def __init__(self, *a, **kw):
        pass

    def get_cache(self, q):
        return None

    def add_cache(self, q, a):
        pass


CALLS = []


def install(icodef_body, anevol_body, icodef_exc=None):
    """icodef / anevol 各自的假响应；anovel_body 为 None 表示不该被调用。"""

    def fake_post(url, **kw):
        if "icodef" in url:
            CALLS.append("icodef")
            if icodef_exc:
                raise icodef_exc
            return FakeResp(200, icodef_body)
        CALLS.append("anevol")
        if anevol_body is None:
            raise AssertionError("不该请求 ANEVOL，但被请求了！")
        return FakeResp(200, anevol_body)

    A.requests.post = fake_post


def build_chain(conf=None):
    A.CacheDAO = NoCache
    chain = TikuChain()
    chain.config_set(conf or CONF)
    chain.init_tiku()
    return chain


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


def main():
    results = []
    q = {"title": "测试题目", "options": OPTIONS, "type": "single"}

    print("=" * 96)
    print(" TikuChain 多题库顺序回退自测（离线，假响应）")
    print("=" * 96)

    # 1) icodef 命中 → ANEVOL 不该被请求
    CALLS.clear()
    install({"code": 1, "data": "中国共产党的领导", "msg": ""}, None)
    chain = build_chain()
    ans = chain.query(dict(q))
    ok = (ans == "中国共产党的领导" and CALLS == ["icodef"] and check_answer(ans, "single", chain))
    results.append(report("icodef 命中 → 不再请求 ANEVOL（省钱）", ok,
                          "  调用顺序={} 答案={!r}".format(CALLS, ans)))

    # 2) icodef 搜不到 → 自动回退 ANEVOL
    CALLS.clear()
    install({"code": -1, "data": "未搜索到答案", "msg": "未搜索到答案"},
            {"code": 1, "answer": "B"})
    chain = build_chain()
    ans = chain.query({"title": "回退题", "options": OPTIONS, "type": "single"})
    ok = (ans == "中国共产党的领导" and CALLS == ["icodef", "anevol"])
    results.append(report("icodef 搜不到 → 回退 ANEVOL 并拿到答案", ok,
                          "  调用顺序={} 答案={!r}".format(CALLS, ans)))

    # 3) A 顺序反过来：ANEVOL 命中 → icodef 不该被请求
    CALLS.clear()
    install({"code": 1, "data": "中国共产党的领导", "msg": ""}, {"code": 1, "answer": "B"})
    conf = dict(CONF)
    conf["provider"] = "TikuAnevol,TikuIcodef"
    chain = build_chain(conf)
    ans = chain.query({"title": "反序题", "options": OPTIONS, "type": "single"})
    ok = (ans == "中国共产党的领导" and CALLS == ["anevol"] and
          chain.provider_names == ["TikuAnevol", "TikuIcodef"])
    results.append(report("provider 顺序生效（反过来问也一样）", ok,
                          "  加载顺序={} 调用顺序={}".format(chain.provider_names, CALLS)))

    # 4) 两个都搜不到 → None（上层随机作答且不提交）
    CALLS.clear()
    install({"code": -1, "data": "未搜索到答案"}, {"code": 0, "msg": "没有找到相关答案"})
    chain = build_chain()
    ans = chain.query({"title": "都没有的题", "options": OPTIONS, "type": "single"})
    ok = (ans is None and CALLS == ["icodef", "anevol"])
    results.append(report("两个题库都搜不到 → 返回 None", ok,
                          "  调用顺序={} 答案={!r}".format(CALLS, ans)))

    # 5) 链里某个题库内部抛异常（网络层）→ 重试后仍失败，必须继续问下一个
    CALLS.clear()
    install({"code": 1, "data": "x"}, {"code": 1, "answer": "C"},
            icodef_exc=RuntimeError("模拟题库内部崩了"))
    chain = build_chain()
    ans = chain.query({"title": "异常回退题", "options": OPTIONS, "type": "single"})
    # icodef 自己是重试 3 次后才放弃的；顿号被 _safe_single 净化掉是预期的（否则 check_single 会失败）
    ok = (ans == "北京上海" and CALLS == ["icodef", "icodef", "icodef", "anevol"])
    results.append(report("icodef 网络层抛异常 → 重试后回退 ANEVOL", ok,
                          "  调用顺序={} 答案={!r}".format(CALLS, ans)))

    # 5b) 子题库的 query() 本身抛异常 → 链必须兜住并继续（这一层是链自己的 try/except）
    CALLS.clear()
    install({"code": 1, "data": "x"}, {"code": 1, "answer": "C"})
    chain = build_chain()
    def boom(q):
        CALLS.append("icodef-crash")
        raise RuntimeError("模拟题库 query 炸了")
    chain.providers[0].query = boom
    ans = chain.query({"title": "链级异常题", "options": OPTIONS, "type": "single"})
    ok = (ans == "北京上海" and CALLS == ["icodef-crash", "anevol"])
    results.append(report("子题库 query() 直接抛异常 → 链兜住并继续", ok,
                          "  调用顺序={} 答案={!r}".format(CALLS, ans)))

    # 6) 配置里写错类名 → 只跳过它
    CALLS.clear()
    install({"code": 1, "data": "中国共产党的领导"}, None)
    conf = dict(CONF)
    conf["provider"] = "TikuNotExist,TikuIcodef"
    chain = build_chain(conf)
    ans = chain.query({"title": "错名字题", "options": OPTIONS, "type": "single"})
    ok = (chain.provider_names == ["TikuIcodef"] and ans == "中国共产党的领导")
    results.append(report("配置里写错类名 → 跳过但其它题库照常工作", ok,
                          "  已加载={}".format(chain.provider_names)))

    # 7) 链里全是无效类名 → 整链停用（等于关闭题库）
    conf = dict(CONF)
    conf["provider"] = "AAA,BBB"
    chain = build_chain(conf)
    ok = (chain.DISABLE is True and chain.providers == [])
    results.append(report("链里没有可用题库 → 整链停用", ok))

    # 8) get_tiku_from_config 分发：逗号 -> 链；单个 -> 原版行为
    #    注意：get_tiku_from_config() 只负责「选择并构造」，真正的加载在随后的 init_tiku()
    #    （main.py 里就是 tiku = tiku.get_tiku_from_config(); tiku.init_tiku()）
    base = Tiku()
    base.config_set(dict(CONF))
    got = base.get_tiku_from_config()
    is_chain = isinstance(got, TikuChain)
    got.init_tiku()
    ok1 = is_chain and got.provider_names == ["TikuIcodef", "TikuAnevol"]

    conf_single = dict(CONF)
    conf_single["provider"] = "TikuIcodef"
    got_single = Tiku()
    got_single.config_set(conf_single)
    got_single = got_single.get_tiku_from_config()
    got_single.init_tiku()
    ok2 = isinstance(got_single, TikuIcodef)

    conf_empty = dict(CONF)
    conf_empty["provider"] = ""
    got_empty = Tiku()
    got_empty.config_set(conf_empty)
    got_empty = got_empty.get_tiku_from_config()
    ok3 = (got_empty.DISABLE is True)

    results.append(report("get_tiku_from_config 按逗号识别题库链 / 单个类名不变 / 留空=关闭", ok1 and ok2 and ok3,
                          "  链={} 单个={} 留空停用={}".format(
                              type(got).__name__, type(got_single).__name__, got_empty.DISABLE)))

    # 9) 题干预处理：去题号，但不能吃掉题干开头的年份/算式
    #    原版 ^\d+ 会把 "1+1等于几？" 削成 "+1等于几？"、把 "1921年..." 削成 "年..."，
    #    题干变形 = 直接搜不到（实测 icodef 因此漏题）。
    class EchoTiku(Tiku):
        def __init__(self):
            super().__init__()
            self.name = "echo"
            self.seen = []

        def _init_tiku(self):
            pass

        def _query(self, q_info):
            self.seen.append(q_info["title"])
            return None

    echo = EchoTiku()
    echo.config_set(dict(CONF))
    echo.init_tiku()

    TITLE_CASES = [
        ("1+1等于几？", "1+1等于几？"),
        ("1921年中国共产党成立", "1921年中国共产党成立"),
        ("1949. 中华人民共和国成立", "1949. 中华人民共和国成立"),
        ("1. 下列说法正确的是", "下列说法正确的是"),
        ("12、关于改革开放的说法", "关于改革开放的说法"),
        ("3) 判断下列说法", "判断下列说法"),
    ]
    bad = []
    for raw, expect in TITLE_CASES:
        echo.query({"title": raw, "options": "A 甲\nB 乙", "type": "single"})
        got_title = echo.seen[-1]
        if got_title != expect:
            bad.append("{!r} -> {!r} (期望 {!r})".format(raw, got_title, expect))
    results.append(report("题干预处理：去题号但保留年份/算式", not bad,
                          "  全部符合" if not bad else "  不符合: " + "; ".join(bad)))

    # 10) 日志等级：链路中间环节没查到，不能再报 ERROR
    #     起因：用户看到「从网课小工具题库获取答案失败」这种 ERROR，以为程序坏了，
    #     其实那只是中间环节没查到，后面 ANEVOL/AI 早就把答案答出来了。
    from api.logger import logger as _loguru

    def capture(fn):
        recs = []

        def _sink(message):
            recs.append((message.record["level"].name, message.record["message"]))

        sid = _loguru.add(_sink, level="DEBUG")
        try:
            fn()
        finally:
            _loguru.remove(sid)
        return recs

    # 10a) 中间环节没查到、后一环命中 -> 一条 ERROR 都不该有
    install({"code": -1, "data": "未搜索到答案"}, {"code": 1, "answer": "B"})
    conf = dict(CONF)
    conf["provider"] = "TikuIcodef,TikuAnevol"
    chain = build_chain(conf)
    box = {}
    recs = capture(lambda: box.update(
        ans=chain.query({"title": "日志题A", "options": OPTIONS, "type": "single"})))
    errs = [m for lv, m in recs if lv == "ERROR"]
    hits = [m for lv, m in recs if "题库链命中" in m]
    ok = (box.get("ans") == "中国共产党的领导" and not errs and len(hits) == 1)
    results.append(report("中间题库未收录、后一环命中 → 0 条 ERROR，且只有 1 条命中日志", ok,
                          "  ERROR={} 命中={!r}".format(errs, hits[0] if hits else "")))
    print("        本次全部日志: {}".format(
        " | ".join("{}:{}".format(lv, m[:40]) for lv, m in recs)))

    # 10b) 全链都没答案 -> 恰好 1 条 ERROR，且措辞明确指出会走随机作答
    install({"code": -1, "data": "未搜索到答案"}, {"code": 0, "msg": "没有找到相关答案"})
    chain = build_chain(conf)
    recs = capture(lambda: chain.query({"title": "日志题B", "options": OPTIONS, "type": "single"}))
    errs = [m for lv, m in recs if lv == "ERROR"]
    ok = (len(errs) == 1 and "题库链全部未命中" in errs[0])
    results.append(report("全链未命中 → 恰好 1 条 ERROR 且写明「随机作答」", ok,
                          "  ERROR 条数={} 内容={!r}".format(len(errs), errs[0] if errs else "")))

    # 10c) 措辞里不该再出现「获取答案失败」这种吓人的字眼
    install({"code": -1, "data": "未搜索到答案"}, {"code": 0, "msg": "没有找到相关答案"})
    chain = build_chain(conf)
    recs = capture(lambda: chain.query({"title": "日志题C", "options": OPTIONS, "type": "single"}))
    msgs = " ".join(m for _, m in recs)
    ok = ("未收录" in msgs and "获取答案失败" not in msgs)
    results.append(report("「题不在库里」记 INFO「未收录」，不再出现「获取答案失败」", ok))

    print("-" * 96)
    print(" 结果: {} passed, {} failed".format(sum(results), len(results) - sum(results)))
    print("=" * 96)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
