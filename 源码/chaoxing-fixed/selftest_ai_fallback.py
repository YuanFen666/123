# -*- coding: utf-8 -*-
"""
AI 兜底自测（离线，假大模型响应）
==================================
场景：provider = TikuIcodef,TikuAnevol,AI
语义：前两个题库都没给出答案的题目（没收录 / 超时 / 服务故障 / 熔断中），
      交给 AI 来答，而不是像原来那样随机瞎填。

验证点：
  1. 题库命中时**不会**调用 AI（省钱省时间）；
  2. 两个题库都没搜到时，AI 接管并把答案映射成正确的提交字母；
  3. AI 返回的选项原文/字母/判断题写法都能归一化；
  4. AI 输出没法解析（乱码）→ 返回 None，不崩，交给上层随机作答；
  5. AI 没配 key → 初始化即明确报错并停用（不会每题发一次注定失败的请求）；
  6. AI 调用报鉴权/余额错误 → 熔断，后续题目不再调用；
  7. AI 的 Answer 字段写成字符串而不是列表时，不会被拆成单个字。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api.answer as A
from api.answer import AI, Tiku, TikuChain
from api.answer_check import check_answer

ICODEF_URL = "https://cx.icodef.com/wyn-nb?v=4"
OPTIONS = "A. 实事求是, 群众路线\nB. 中国共产党的领导\nC. 北京、上海"

BASE_CONF = {
    "provider": "TikuIcodef,TikuAnevol,AI",
    "submit": "true",
    "cover_rate": "0.8",
    "true_list": "正确,对,√,是",
    "false_list": "错误,错,×,否,不对,不正确",
    "icodef_url": ICODEF_URL,
    "icodef_min_interval": "0",
    "url": "",
    "tokens": "TEST_TOKEN",
    "anevol_min_interval": "0",
    "endpoint": "https://api.deepseek.com/v1",
    "key": "sk-test-key",
    "model": "deepseek-chat",
    "http_proxy": "",
    "min_interval_seconds": "0",
}

CALLS = []


class FakeResp:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class NoCache:
    def __init__(self, *a, **kw):
        pass

    def get_cache(self, q):
        return None

    def add_cache(self, q, a):
        pass


# ---------------------------------------------------------------- 假 OpenAI
class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeCompletion:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeAPIError(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


class FakeCompletions:
    def __init__(self, content=None, exc=None):
        self._content = content
        self._exc = exc
        self.seen_prompts = []

    def create(self, model=None, messages=None):
        CALLS.append("ai")
        self.seen_prompts.append(messages)
        if self._exc:
            raise self._exc
        return FakeCompletion(self._content)


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeOpenAI:
    """替掉 openai.OpenAI：记录收到的 prompt，按需返回内容或抛错。"""

    completions = None

    def __init__(self, **kw):
        self.kwargs = kw
        self.chat = FakeChat(FakeOpenAI.completions)


def install(icodef_body, anevol_body, ai_content=None, ai_exc=None):
    FakeOpenAI.completions = FakeCompletions(ai_content, ai_exc)
    A.OpenAI = FakeOpenAI

    def fake_post(url, **kw):
        if "icodef" in url:
            CALLS.append("icodef")
            return FakeResp(icodef_body)
        CALLS.append("anevol")
        return FakeResp(anevol_body)

    A.requests.post = fake_post


def build_chain(conf=None):
    A.CacheDAO = NoCache
    chain = TikuChain()
    chain.config_set(conf or dict(BASE_CONF))
    chain.init_tiku()
    return chain


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


def main():
    results = []
    print("=" * 96)
    print(" AI 兜底自测（离线，假大模型响应）")
    print("=" * 96)

    MISS = {"code": -1, "data": "未搜索到答案", "msg": "未搜索到答案"}
    ANEVOL_MISS = {"code": 0, "msg": "没有找到相关答案"}

    # 1) 题库命中 -> 不该调用 AI
    CALLS.clear()
    install({"code": 1, "data": "中国共产党的领导", "msg": ""}, ANEVOL_MISS, ai_content='{"Answer": ["应改用A"]}')
    chain = build_chain()
    ans = chain.query({"title": "题1", "options": OPTIONS, "type": "single"})
    ok = (ans == "中国共产党的领导" and "ai" not in CALLS and CALLS == ["icodef"])
    results.append(report("题库命中 → 完全不调用 AI", ok, "  调用顺序={} 答案={!r}".format(CALLS, ans)))

    # 2) 两个题库都没搜到 -> AI 接管
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_content='{"Answer": ["中国共产党的领导"]}')
    chain = build_chain()
    ans = chain.query({"title": "题2", "options": OPTIONS, "type": "single"})
    ok = (ans == "中国共产党的领导" and CALLS == ["icodef", "anevol", "ai"])
    results.append(report("两个题库都没搜到 → AI 接管并给出答案", ok,
                          "  调用顺序={} 答案={!r}".format(CALLS, ans)))

    # 3) AI 返回字母 -> 还原成选项原文
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_content='{"Answer": ["B"]}')
    chain = build_chain()
    ans = chain.query({"title": "题3", "options": OPTIONS, "type": "single"})
    ok = (ans == "中国共产党的领导")
    results.append(report("AI 返回字母 'B' → 还原成选项原文", ok, "  答案={!r}".format(ans)))

    # 4) AI 返回多选题多个选项原文（列表）
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_content='{"Answer": ["实事求是", "中国共产党的领导"]}')
    chain = build_chain()
    ans = chain.query({"title": "题4", "options": OPTIONS, "type": "multiple"})
    ok = (ans is not None and set(ans.split("\n")) == {"实事求是", "中国共产党的领导"})
    results.append(report("AI 多选返回多个答案 → 换行拼接", ok, "  答案={!r}".format(ans)))

    # 5) AI 判断题
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_content='{"Answer": ["错误"]}')
    chain = build_chain()
    ans = chain.query({"title": "题5", "options": "A. 正确\nB. 错误", "type": "judgement"})
    ok = (ans == "错误" and check_answer(ans, "judgement", chain))
    results.append(report("AI 判断题 → '错误' 且通过类型校验", ok, "  答案={!r}".format(ans)))

    # 6) Answer 写成字符串（不是列表）时不能被拆成单字
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_content='{"Answer": "中国共产党的领导"}')
    chain = build_chain()
    ans = chain.query({"title": "题6", "options": OPTIONS, "type": "single"})
    ok = (ans == "中国共产党的领导")
    results.append(report("AI 把 Answer 写成字符串 → 不会被拆成单字", ok, "  答案={!r}".format(ans)))

    # 7) AI 输出乱码 -> None（不崩，交给上层随机作答）
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_content="我觉得应该是C吧，不确定")
    chain = build_chain()
    ans = chain.query({"title": "题7", "options": OPTIONS, "type": "single"})
    ok = (ans is None)
    results.append(report("AI 输出无法解析 → 返回 None（不崩）", ok, "  答案={!r}".format(ans)))

    # 8) 没配 key -> 初始化就停用，且不进入题库链
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_content='{"Answer": ["x"]}')
    conf = dict(BASE_CONF)
    conf["key"] = ""
    chain = build_chain(conf)
    ok = ("AI" not in chain.provider_names and chain.provider_names == ["TikuIcodef", "TikuAnevol"])
    results.append(report("AI 没配 key → 初始化即停用并被移出题库链", ok,
                          "  已加载={}".format(chain.provider_names)))

    # 9) 鉴权/余额错误 -> 熔断，后续题目不再调用 AI
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_exc=FakeAPIError("Error code: 401 - invalid api key", status_code=401))
    chain = build_chain()
    chain.query({"title": "题9-a", "options": OPTIONS, "type": "single"})
    n1 = len([c for c in CALLS if c == "ai"])
    chain.query({"title": "题9-b", "options": OPTIONS, "type": "single"})
    n2 = len([c for c in CALLS if c == "ai"])
    ai_inst = [p for p in chain.providers if isinstance(p, AI)][0]
    ok = (n1 == 1 and n2 == 1 and ai_inst._pause_remaining() > 500)
    results.append(report("AI 报 401 → 只试 1 次并长时间熔断", ok,
                          "  AI 调用次数={}→{}  熔断剩余={:.0f}s".format(n1, n2, ai_inst._pause_remaining())))

    # 10) 普通失败连续 5 次 -> 熔断 300s
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_exc=FakeAPIError("connection reset"))
    chain = build_chain()
    for i in range(5):
        chain.query({"title": "题10-%d" % i, "options": OPTIONS, "type": "single"})
    n5 = len([c for c in CALLS if c == "ai"])
    chain.query({"title": "题10-x", "options": OPTIONS, "type": "single"})
    n6 = len([c for c in CALLS if c == "ai"])
    ok = (n5 == 5 and n6 == 5)
    results.append(report("AI 连续 5 次失败 → 熔断并跳过", ok,
                          "  AI 调用次数={}→{}".format(n5, n6)))

    # 11) 喂给模型的选项已经去掉 "A." 标号
    CALLS.clear()
    install(MISS, ANEVOL_MISS, ai_content='{"Answer": ["中国共产党的领导"]}')
    chain = build_chain()
    chain.query({"title": "题11", "options": OPTIONS, "type": "single"})
    prompt = FakeOpenAI.completions.seen_prompts[-1]
    user_msg = [m for m in prompt if m["role"] == "user"][0]["content"]
    ok = ("选项：实事求是, 群众路线" in user_msg and "A. 实事求是" not in user_msg)
    results.append(report("喂给模型的选项已去掉 'A.' 标号", ok,
                          "  user 内容={!r}".format(user_msg)))

    # 12) AI 启用时会打印明确日志（便于排查到底有没有接上）
    ok = True
    ai = AI()
    ai.config_set(dict(BASE_CONF))
    ai.init_tiku()
    ok = (ai.client is not None and not getattr(ai, "DISABLE", False))
    results.append(report("AI 配置齐全 → 正常初始化（client 已就绪）", ok))

    print("-" * 96)
    print(" 结果: {} passed, {} failed".format(sum(results), len(results) - sum(results)))
    print("=" * 96)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
