# -*- coding: utf-8 -*-
"""
worker 线程自愈自测
====================
模拟原版最容易触发的问题：超星接口读超时(ReadTimeout) 让 worker 线程被彻底打死。

原版行为：worker_thread 是 while True，异常被 log_error 包住后 raise 出去，
          线程直接死亡且永不重建 -> 并发数只减不增，最后"一次只能看两条视频"。
修复后：  异常在循环内被兜住，转成 ChapterResult.ERROR 走重试，线程继续活着。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import main as M
from main import ChapterTask, ChapterResult, JobProcessor

JOBS = 4
TASK_N = 12
FAILS = 6          # 故意制造 6 次读超时
RESULT = {"calls": 0}
ALIVE_SAMPLES = []


class FakeChaoxing:
    """process_chapter 被替换掉了，这里用不到真实接口。"""
    pass


def fake_process_chapter(chaoxing, course, point, speed):
    RESULT["calls"] += 1
    if RESULT["calls"] <= FAILS:
        # 这就是线上真实发生的异常类型
        raise requests.exceptions.ReadTimeout("HTTPSConnectionPool: Read timed out. (read timeout=5)")
    return ChapterResult.SUCCESS


def sampler(proc, stop_evt):
    # 只统计“正式工作期间”的存活数：
    #  - 线程还没创建出来（run() 之前）不采
    #  - 收尾时队列 shutdown、worker 正常退出不算衰减
    while not stop_evt.is_set() and not proc._stopping:
        if proc.threads:
            ALIVE_SAMPLES.append(sum(1 for t in proc.threads if t.is_alive()))
        time.sleep(0.02)


def make_proc():
    tasks = [
        ChapterTask(point={"title": "章节%d" % i, "has_finished": False}, index=i)
        for i in range(TASK_N)
    ]
    config = {"speed": 1.0, "jobs": JOBS, "notopen_action": "retry"}
    return JobProcessor(FakeChaoxing(), {"title": "测试课程"}, tasks, config), tasks


def main():
    M.process_chapter = fake_process_chapter  # 注入故障

    proc, tasks = make_proc()
    stop_evt = threading.Event()
    threading.Thread(target=sampler, args=(proc, stop_evt), daemon=True).start()

    t0 = time.time()
    proc.run()
    elapsed = time.time() - t0
    stop_evt.set()
    time.sleep(0.2)

    ok_results = sum(1 for t in tasks if t.result == ChapterResult.SUCCESS)
    failed = len(proc.failed_tasks)
    min_alive = min(ALIVE_SAMPLES) if ALIVE_SAMPLES else -1

    print("=" * 74)
    print(" worker 线程自愈自测")
    print("=" * 74)
    print(" 并发数 (jobs)             : %d" % JOBS)
    print(" 章节任务数                : %d" % TASK_N)
    print(" 注入的 ReadTimeout 次数   : %d" % FAILS)
    print(" process_chapter 实际调用  : %d  (期望 %d = 任务数 + 注入失败数)"
          % (RESULT["calls"], TASK_N + FAILS))
    print(" 成功完成的章节            : %d / %d" % (ok_results, TASK_N))
    print(" 最终失败进 failed_tasks   : %d" % failed)
    print(" 运行期间最小存活 worker   : %d / %d" % (min_alive, JOBS))
    print(" 总耗时                    : %.2fs" % elapsed)
    print("-" * 74)

    checks = [
        ("所有章节最终都成功", ok_results == TASK_N),
        ("没有任务被丢进失败列表", failed == 0),
        ("每次失败都被重试了", RESULT["calls"] == TASK_N + FAILS),
        ("worker 并发全程未衰减（修复生效）", min_alive >= JOBS),
        ("没有卡死（正常返回）", elapsed < 30),
    ]
    allok = True
    for name, res in checks:
        print(" [%s] %s" % ("PASS" if res else "FAIL", name))
        allok &= res

    print("-" * 74)
    if min_alive >= JOBS:
        print(" 对比：原版在此处会有 %d 个 worker 被 ReadTimeout 打死，"
              % min(FAILS, JOBS))
        print("       并发从 %d 掉到 %d，剩下 %d 条线程继续跑，表现就是“越跑越慢”。"
              % (JOBS, max(0, JOBS - FAILS), max(0, JOBS - FAILS)))
    print("=" * 74)
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
