# -*- coding: utf-8 -*-
"""对比两轮运行开局阶段的视频并发与章节节奏，判断"答题是否拖住了线程"。"""
import io
import re
import sys
import datetime

LOG = r"C:\Users\11977\chaoxing.log"
WINDOW_MIN = 4          # 只看开局前 N 分钟

ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
login_re = re.compile(r"api\.base:login:\d+ - 登录成功")

CHAP = re.compile(r"__main__:process_chapter:\d+ - 当前章节: (.+)$")
DONE = re.compile(r"__main__:process_chapter:\d+ - 章节：(.+?) 已完成所有任务点")
VID = re.compile(r"api\.base:study_video:(\d+) - (开始任务|任务完成): (.+?)(?:\.mp4)?(?:,|$)")
JOB = re.compile(r"__main__:process_job:\d+ - 识别到(视频|章节检测|文档|阅读)任务")
QUIZ = re.compile(r"api\.base:study_work:\d+ - (.+?) 填写答案为")
ANSOK = re.compile(r"(提交|保存)答题成功")
TIKU = re.compile(r"api\.answer:_ask:\d+ - ANEVOL")

runs, cur = [], None
for line in io.open(LOG, encoding="utf-8", errors="replace"):
    m = ts_re.match(line)
    if not m:
        continue
    t = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    if login_re.search(line):
        cur = {"start": t, "ev": []}
        runs.append(cur)
        continue
    if cur is None:
        continue
    for tag, rx in (("章节", CHAP), ("完成", DONE), ("视频", VID), ("任务", JOB),
                    ("答题", QUIZ), ("提交", ANSOK), ("题库", TIKU)):
        mm = rx.search(line)
        if mm:
            cur["ev"].append((t, tag, mm.groups()))
            break


def show(r, idx):
    start = r["start"]
    end = start + datetime.timedelta(minutes=WINDOW_MIN)
    ev = [e for e in r["ev"] if e[0] <= end]
    if not ev:
        return
    vers = [g[0] for t, tag, g in ev if tag == "视频"]
    ver = "修复版" if any(v in ("488", "547") for v in vers) else "官方原版"
    print("=" * 78)
    print(" 第%d轮 %s（%s）  开局前 %d 分钟" % (idx, start, ver, WINDOW_MIN))
    print("-" * 78)
    playing = set()
    for t, tag, g in ev:
        ts = (t - start).total_seconds()
        mmss = "%02d:%02d" % (ts // 60, ts % 60)
        if tag == "视频" and g[1] == "开始任务":
            playing.add(g[2])
            print("  +%s  开始播放 %-42s  当前并发=%d" % (mmss, g[2][:42], len(playing)))
        elif tag == "视频" and g[1] == "任务完成":
            playing.discard(g[2])
            print("  +%s  播完 %-44s  当前并发=%d" % (mmss, g[2][:44], len(playing)))
        elif tag == "章节":
            print("  +%s  领走章节 %s" % (mmss, g[0][:40]))
        elif tag == "答题":
            print("  +%s      答题: %s" % (mmss, g[0][:36]))
        elif tag == "提交":
            print("  +%s      答题提交/保存成功" % mmss)
    n_chap = sum(1 for e in ev if e[1] == "章节")
    n_job = sum(1 for e in ev if e[1] == "任务")
    n_vid = sum(1 for e in ev if e[1] == "视频" and e[2][1] == "开始任务")
    n_q = sum(1 for e in ev if e[1] == "答题")
    n_ok = sum(1 for e in ev if e[1] == "提交")
    n_tiku = sum(1 for e in ev if e[1] == "题库")
    print("-" * 78)
    print("  开局 %d 分钟汇总: 领走章节 %d 个 / 识别任务点 %d 个 / 开始视频 %d 个 / "
          "答题 %d 题 / 答题成功提交 %d 次 / 题库调用 %d 次" % (WINDOW_MIN, n_chap, n_job, n_vid, n_q, n_ok, n_tiku))
    print()


for i, r in enumerate(runs, 1):
    if i >= 10:
        show(r, i)
