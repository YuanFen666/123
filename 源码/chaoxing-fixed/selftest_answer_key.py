# -*- coding: utf-8 -*-
"""
正确答案库（answer_key.json）自测
==================================
验证三条：
  1. 它和 cache.json 是两个独立的文件，互不干扰；
  2. 查询时**正确答案库优先于 cache.json**（相同题目以核验过的为准）；
  3. 来源分流正确：AI/题库的猜测只进 cache.json，绝不污染正确答案库。

另外验证批改页导入器能抠出 (题干, 正确答案)。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api.answer as A  # noqa: E402


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


def main():
    results = []
    print("=" * 96)
    print(" 正确答案库自测（离线）")
    print("=" * 96)

    tmp = tempfile.mkdtemp(prefix="answer_key_test_")
    cwd = os.getcwd()
    try:
        os.chdir(tmp)

        # ---- 1) 两个库是两个文件 ----
        cache = A.CacheDAO()
        key = A.AnswerKeyDAO()
        ok = (os.path.basename(str(cache.cache_file)) == "cache.json"
              and os.path.basename(str(key.cache_file)) == "answer_key.json")
        results.append(report("正确答案库与缓存是独立文件", ok,
                              "  {} vs {}".format(cache.cache_file.name, key.cache_file.name)))

        # ---- 2) 写入互不影响 ----
        cache.add_cache("题目甲", "AI猜的A")
        key.add_cache("题目甲", "核验过的B", source="测试")
        ok = (cache.get_cache("题目甲") == "AI猜的A" and key.get_cache("题目甲") == "核验过的B")
        results.append(report("同一道题在两个库里互不覆盖", ok,
                              "  cache={!r} key={!r}".format(cache.get_cache("题目甲"),
                                                            key.get_cache("题目甲"))))

        # ---- 3) 真实查询时：正确答案库优先 ----
        # 用一个假的 Tiku 子类，记录 _query 有没有被调用（命中正确答案库就不该再查题库/AI）
        class Probe(A.Tiku):
            name = "探针"

            def __init__(self):
                super().__init__()
                self.called = 0

            def _query(self, q_info):
                self.called += 1
                return "题库瞎猜的C"

        p = Probe()
        ans = p.query({"title": "题目甲", "options": "", "type": "single"})
        ok = (ans == "核验过的B" and p.called == 0)
        results.append(report("命中正确答案库 -> 直接返回，不再问题库/AI（省调用）", ok,
                              "  返回={!r}  题库被调用 {} 次".format(ans, p.called)))

        # ---- 4) 库里没有的题：正常走题库，且猜测只落到 cache.json ----
        p2 = Probe()
        ans2 = p2.query({"title": "题目乙", "options": "", "type": "single"})
        ok = (ans2 == "题库瞎猜的C" and p2.called == 1
              and cache.get_cache("题目乙") == "题库瞎猜的C"
              and key.get_cache("题目乙") is None)
        results.append(report("未命中 -> 走题库，且猜测只写 cache.json，不污染答案库", ok,
                              "  cache={!r} key={!r}".format(cache.get_cache("题目乙"),
                                                            key.get_cache("题目乙"))))

        # ---- 5) 空值不写入 ----
        key.add_cache("题目丙", "")
        key.add_cache("", "空题干")
        ok = (key.get_cache("题目丙") is None and "题目丙" not in key._read_cache())
        results.append(report("空答案/空题干不会写进库", ok))

        # ---- 6) 批改页导入器能抠出题干+正确答案 ----
        from importlib import util as _iu
        hp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "工具脚本", "harvest_answer_key.py")
        spec = _iu.spec_from_file_location("harvest", os.path.abspath(hp))
        hv = _iu.module_from_spec(spec)
        spec.loader.exec_module(hv)

        HTML = """
        <div class="questionLi">
          <h3 class="mark_name">1. （单选题, 2.0分）中华人民共和国的首都是哪里？</h3>
          <div>正确答案：A</div>
          <div>我的答案：B</div>
          <div>答案解析：北京是首都。</div>
        </div>
        <div class="questionLi">
          <h3 class="mark_name">2. （判断题, 2.0分）地球是圆的。</h3>
          <div>正确答案：正确</div>
          <div>我的答案：正确</div>
        </div>
        """
        items = hv.harvest(HTML)
        ok = (len(items) == 2 and "首都" in items[0][0] and items[0][1] == "A"
              and items[1][1] == "正确")
        results.append(report("批改页导入器：抠出 (题干, 正确答案)，并剥掉「我的答案/解析」", ok,
                              "  {}".format([(q[:22], a) for q, a in items])))
        ok = all("我的答案" not in q and "答案解析" not in q for q, _ in items)
        results.append(report("导入的题干里不含「我的答案/答案解析」等尾巴", ok))

        # ★ 题干必须和「查询时归一化后的形式」一致，否则答案库永远命中不了
        ok = all("单选题" not in q and "判断题" not in q and "分）" not in q and "分)" not in q
                 for q, _ in items)
        results.append(report("导入的题干已剥掉题型/分值标记（与查询时的归一化一致）", ok,
                              "  {}".format([q[:24] for q, _ in items])))
        # 直接拿导入结果去查，模拟真实命中
        key.add_cache(items[0][0], items[0][1], source="测试导入")
        class P2(A.Tiku):
            name = "探针2"

            def __init__(self):
                super().__init__()
                self.called = 0

            def _query(self, q_info):
                self.called += 1
                return "不该被调用"

        p3 = P2()
        got = p3.query({"title": "（单选题, 2.0分）中华人民共和国的首都是哪里？",
                        "options": "", "type": "single"})
        ok = (got == items[0][1] and p3.called == 0)
        results.append(report("导入后的题干能被真实查询命中（端到端）", ok,
                              "  返回={!r} 题库被调用 {} 次".format(got, p3.called)))

        print("-" * 96)
        print(" 结果: {} passed, {} failed".format(sum(results), len(results) - sum(results)))
        print("=" * 96)
        return 0 if all(results) else 1
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
