# -*- coding: utf-8 -*-
"""
把批改页里的「正确答案」导入本地正确答案库
============================================
用途：你考完/做完测验后，批改页上会显示每道题的**正确答案**。
      把它导出成 HTML 喂给这个脚本，就能一次性把答案存进 answer_key.json。
      以后遇到同样的题，程序先查这个库 —— 不花 API 次数、不走网络，而且答案可信。

用法：
    python harvest_answer_key.py <批改页.html> [更多.html ...]
    python harvest_answer_key.py --list          # 看看库里现在有多少条
    python harvest_answer_key.py --export 文本   # 导出成「题干<TAB>答案」的纯文本，方便人工核对

怎么拿到 HTML（和你上次给我的一样）：
    在批改页按 F12 → Elements → 右键最上面的 <html> → Copy → Copy outerHTML
    → 粘进记事本存成 .html 文件
"""
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "源码", "chaoxing-fixed"))
from bs4 import BeautifulSoup  # noqa: E402
from api.answer import answer_key, normalize_title  # noqa: E402


def clean(s: str) -> str:
    """
    归一化题干 —— 直接复用程序查询时用的那个函数（单一事实来源）。

    第一版我自己写了套正则，结果剥不干净 "(判断题, 2.0分)"，
    导入进去的键和查询用的键对不上，答案库看着有数据却永远命中不了。
    现在改成共用 api.answer.normalize_title，两边不可能再不一致。
    """
    return normalize_title(re.sub(r"\s+", " ", str(s or "")).strip())


def harvest(html_text: str):
    """
    从批改页 HTML 里抠出 (题干, 正确答案)。

    超星的批改页结构有好几代，这里做「多模式匹配」而不是死认一套选择器：
    先按题目容器切块，再在每块里分别找题干和正确答案。
    找不到就报出来 —— 别静默当成 0 条，那样你根本不知道是结构变了还是真没有。
    """
    soup = BeautifulSoup(html_text, "lxml")
    found = []

    # 题目容器候选（不同版本的批改页都用过这些类名）
    containers = []
    for sel in ("div.questionLi", "div.mark_item", "div.TiMu", "div.questionWrap",
                "div[id^='sigleQuestionDiv_']", "li.questionLi", "div.ans-cc-exam"):
        containers = soup.select(sel)
        if containers:
            break
    if not containers:
        # 退一步：整页当成一块，试试能不能至少抠出几组
        containers = [soup]

    for node in containers:
        text = node.get_text("\n", strip=True)
        # 题干：优先取公认的题干节点
        title = ""
        for sel in ("h3.mark_name", "div.stem_answer", "div.mark_name", "div.tit",
                    "div.questionStem", "div.stem"):
            t = node.select_one(sel)
            if t is not None:
                title = t.get_text(" ", strip=True)
                break
        if not title:
            # 兜底：取最长的那一行当题干
            lines = [l for l in text.split("\n") if len(l) > 6]
            title = max(lines, key=len) if lines else ""

        # 正确答案：几种常见写法都试
        ans = ""
        m = re.search(r"正确答案[:：]?\s*([^\n]{1,60})", text)
        if m:
            ans = m.group(1)
        else:
            node_ans = node.select_one("span.rightAnswer, div.rightAnswer, em.rightAnswer, "
                                       "span.answerRight, i.rightAnswer")
            if node_ans is not None:
                ans = node_ans.get_text(" ", strip=True)
        if not ans:
            continue

        # 去掉题干里混进来的「正确答案/我的答案」尾巴和分数说明
        title = re.split(r"正确答案|我的答案|答案解析|\(\s*[单多判填简]\s*选题", clean(title))[0]
        title = clean(title)
        # 答案解析那段常被一起抓进来，砍掉
        ans = clean(re.split(r"我的答案|答案解析|得分|\(|（", ans)[0])
        if len(title) >= 6 and ans:
            found.append((title, ans))
    return found


def main():
    args = sys.argv[1:]
    dao = answer_key()

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    if args[0] == "--list":
        data = dao._read_cache()
        print("正确答案库：{} 条  （文件：{}）".format(len(data), dao.cache_file))
        for i, (q, a) in enumerate(list(data.items())[:15], 1):
            print("  {:>3}. {:<52} -> {}".format(i, q[:52], a))
        if len(data) > 15:
            print("  ... 还有 {} 条".format(len(data) - 15))
        return 0

    if args[0] == "--export":
        data = dao._read_cache()
        out = args[1] if len(args) > 1 else "answer_key_export.txt"
        with open(out, "w", encoding="utf8") as fp:
            for q, a in sorted(data.items()):
                fp.write("{}\t{}\n".format(q, a))
        print("已导出 {} 条到 {}".format(len(data), out))
        return 0

    total_new = 0
    for path in args:
        if not os.path.isfile(path):
            print("跳过（文件不存在）：{}".format(path))
            continue
        with open(path, encoding="utf8", errors="replace") as fp:
            html = fp.read()
        items = harvest(html)
        print("\n{}: 抠出 {} 组（题干, 正确答案）".format(os.path.basename(path), len(items)))
        if not items:
            print("  ⚠ 一条都没抠出来 —— 多半是页面结构和我预期的不同。")
            print("    把这份 HTML 发我，我按真实结构调整解析（就像上次修整卷页那样）。")
            continue
        for q, a in items[:5]:
            print("   {}  ->  {}".format(q[:56], a))
        if len(items) > 5:
            print("   ... 还有 {} 组".format(len(items) - 5))
        for q, a in items:
            before = dao.get_cache(q)
            dao.add_cache(q, a, source="批改页导入")
            if before != a:
                total_new += 1

    print("\n本次新写入正确答案库：{} 条；库内共 {} 条".format(total_new, len(dao._read_cache())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
