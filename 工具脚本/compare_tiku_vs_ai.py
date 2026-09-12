# -*- coding: utf-8 -*-
"""
题库 vs 直接用 AI —— 同一批题的答案对比
=========================================
背景：题库作者说"题库也是直接交给 AI 答题的"。如果属实，那就是花两遍钱、走两遍网络。
      这个脚本用日志里的真题做 A/B，让 题库 和 自己的 DeepSeek 各答一遍，看差多少。

用法：
    python compare_tiku_vs_ai.py [题目数]

数据来源：chaoxing.log 里的
    api.answer:query:259 - 从ANEVOL题库获取答案：<题干> -> <答案>
"""
import configparser
import re
import sys
import time

sys.path.insert(0, r"E:\system\Desktop\网课\源码\chaoxing-fixed")
from api.answer import Tiku  # noqa: E402

LOG = r"E:\system\Desktop\网课\chaoxing.log"
CONF = r"E:\system\Desktop\网课\config.ini"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 15


def norm(s: str) -> str:
    """归一化：去掉题号/空格/标点，转小写，便于粗比。"""
    s = str(s or "")
    s = re.sub(r"^\s*\d+\s*[.、]\s*", "", s)
    s = re.sub(r"[【】\[\]（）()\s，。、.,;；:：?？!！\"'`]", "", s)
    return s.lower()


def pick_letters(s: str) -> str:
    """抽出答案里的选项字母（如 'C无人机播种' -> 'C'，'AKmeans' -> 'A'）。"""
    s = str(s or "").strip()
    m = re.match(r"^([A-Ha-h]{1,6})\b", s)
    if m:
        return m.group(1).upper()
    letters = "".join(ch for ch in s if ch.isascii() and ch.isalpha()).upper()
    return letters if letters and len(letters) <= 6 and set(letters) <= set("ABCDEFGH") else ""


# ---- 1) 从日志里抽题 ----
pat = re.compile(r"从ANEVOL题库获取答案[:：]\s*(.*?)\s*->\s*(.*)$")
qa = []
seen = set()
for line in open(LOG, encoding="utf8", errors="replace"):
    m = pat.search(line)
    if not m:
        continue
    q, a = m.group(1).strip(), m.group(2).strip()
    key = norm(q)
    if len(q) < 8 or not a or key in seen:
        continue
    seen.add(key)
    qa.append((q, a))
print("日志里 ANEVOL 答题记录: {} 条（去重后 {} 条），取前 {} 条做对比\n".format(len(seen), len(qa), N))

# ---- 2) 建题库链，单独取出 ANEVOL 与 AI ----
cfg = configparser.ConfigParser(); cfg.read(CONF, encoding="utf-8-sig")
tiku_cfg = dict(cfg.items("tiku"))
t = Tiku(); t.config_set(tiku_cfg); chain = t.get_tiku_from_config(); chain.init_tiku()
anevol = ai = None
for p in getattr(chain, "providers", []):
    if type(p).__name__ == "TikuAnevol":
        anevol = p
    elif type(p).__name__ == "AI":
        ai = p
if ai is None or anevol is None:
    print("!! 题库链里没有 AI 或 ANEVOL，检查 config.ini 的 provider 设置")
    sys.exit(1)
print("对比对象: ANEVOL题库(线上) vs AI大模型({})\n".format(
    getattr(ai, "model", getattr(ai, "model_name", "?"))))
print("=" * 100)

same_letter = same_text = diff = 0
rows = []
for i, (q, anevol_ans) in enumerate(qa[:N], 1):
    qi = {"title": q, "options": "", "type": "single"}
    t0 = time.time()
    try:
        ai_ans = ai.query(qi)
    except Exception as e:  # noqa: BLE001
        ai_ans = "<AI 异常: {}>".format(e)
    cost = time.time() - t0

    la, lb = pick_letters(anevol_ans), pick_letters(ai_ans)
    if norm(anevol_ans) == norm(ai_ans):
        verdict, same_text = "完全一致", same_text + 1
    elif la and lb and la == lb:
        verdict, same_letter = "选项字母一致", same_letter + 1
    else:
        verdict, diff = "不一致", diff + 1
    rows.append((i, q, anevol_ans, ai_ans, verdict, cost))
    print("\n[{}] {} ({:.1f}s)".format(i, q[:66], cost))
    print("    题库(ANEVOL): {}".format(str(anevol_ans)[:70]))
    print("    自己 AI     : {}".format(str(ai_ans)[:70]))
    print("    判定        : {}".format(verdict))

print("\n" + "=" * 100)
tot = len(rows)
print("统计（{} 题）:".format(tot))
print("  完全一致     {:>3}  {:.0%}".format(same_text, same_text / tot if tot else 0))
print("  选项字母一致 {:>3}  {:.0%}".format(same_letter, same_letter / tot if tot else 0))
print("  不一致       {:>3}  {:.0%}".format(diff, diff / tot if tot else 0))
print("  平均耗时     {:.1f}s （自己 AI 直连）".format(sum(r[5] for r in rows) / tot if tot else 0))
print("\n结论参考：")
print("  · 一致率高 → 题库很可能就是同一套 AI 转手，直连更省钱更快")
print("  · 不一致多 → 题库有自己的语料/校对，¥0.0015/题买的是准确性，别急着砍")
