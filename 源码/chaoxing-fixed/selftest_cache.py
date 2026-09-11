# -*- coding: utf-8 -*-
"""
CacheDAO 并发写缓存自测
========================
背景（线上真实报错，日志里刷了 10 次）：

    ERROR | api.answer:_write_cache:122 - Failed to write cache atomically:
            [WinError 5] 拒绝访问。: 'E:\\...\\cache.json_ljmpmpvu' -> 'cache.json'

根因：Tiku.query 每次查询都 new 一个 CacheDAO()，而原版把锁挂在实例上
（self._lock = threading.RLock()），于是每个实例一把锁 = 等于没锁。
8 个 worker 并发答题时多个线程同时 os.replace() 抢写 cache.json，
Windows 上就报 WinError 5；后果是答案写不进缓存、被重复查询。

本测试：
  1. 断言两个 CacheDAO 实例共享同一把锁（原版会失败）；
  2. 8 线程 × 25 次并发写 → 一条不丢、一次不报错；
  3. 模拟文件被短暂占用（os.replace 先失败几次）→ 退避重试后仍能写入；
  4. 不留临时文件、缓存 JSON 始终可解析。
"""
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api.answer as A
from api.answer import CacheDAO, _CACHE_LOCK


def capture_errors():
    """开始捕获 ERROR 级日志，返回 (get_records, stop)。"""
    from api.logger import logger as L
    recs = []

    def _sink(message):
        if message.record["level"].name == "ERROR":
            recs.append(message.record["message"])

    sid = L.add(_sink, level="ERROR")
    return recs, (lambda: L.remove(sid))


def main():
    results = []
    tmpdir = tempfile.mkdtemp(prefix="cache_selftest_")
    cache_path = os.path.join(tmpdir, "cache.json")

    print("=" * 96)
    print(" CacheDAO 并发写缓存自测")
    print("=" * 96)

    # ---- 1) 锁必须是全进程共享的 ----
    a, b = CacheDAO(cache_path), CacheDAO(cache_path)
    ok = (a._lock is b._lock) and (a._lock is _CACHE_LOCK)
    print(" [{}] 两个 CacheDAO 实例共享同一把锁（原版是各自一把 = 等于没锁）".format("PASS" if ok else "FAIL"))
    results.append(ok)

    # ---- 2) 8 线程 × 25 次并发写 ----
    recs, stop = capture_errors()
    N_THREADS, PER = 8, 25
    errs = []

    def worker(tid):
        try:
            for i in range(PER):
                CacheDAO(cache_path).add_cache("题-%d-%d" % (tid, i), "答案%d" % i)
        except Exception as e:  # noqa: BLE001
            errs.append("%s: %s" % (type(e).__name__, e))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop()

    data = json.loads(open(cache_path, encoding="utf8").read())
    expect = N_THREADS * PER
    leftovers = [f for f in os.listdir(tmpdir) if f != "cache.json"]
    ok = (len(data) == expect and not errs and not recs and not leftovers)
    print(" [{}] {} 线程 × {} 次并发写 → 缓存条目 {}/{}，异常 {}，ERROR 日志 {}，残留临时文件 {}".format(
        "PASS" if ok else "FAIL", N_THREADS, PER, len(data), expect, len(errs), len(recs), len(leftovers)))
    if errs:
        print("        异常: {}".format(errs[:3]))
    if recs:
        print("        日志: {}".format(recs[:3]))
    results.append(ok)

    # ---- 3) 模拟目标文件被短暂占用 → 退避重试 ----
    real_replace = A.os.replace
    state = {"n": 0}

    def flaky_replace(src, dst):
        state["n"] += 1
        if state["n"] <= 3:
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst)

    recs, stop = capture_errors()
    A.os.replace = flaky_replace
    try:
        CacheDAO(cache_path).add_cache("被占用后仍要写进去", "OK")
    finally:
        A.os.replace = real_replace
        stop()
    got = json.loads(open(cache_path, encoding="utf8").read()).get("被占用后仍要写进去")
    ok = (got == "OK" and not recs and state["n"] == 4)
    print(" [{}] os.replace 先失败 3 次 → 退避重试后写入成功（重试次数={}，ERROR={}）".format(
        "PASS" if ok else "FAIL", state["n"], len(recs)))
    results.append(ok)

    # ---- 4) 缓存文件始终是合法 JSON ----
    try:
        json.loads(open(cache_path, encoding="utf8").read())
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    print(" [{}] 全程结束后 cache.json 仍是合法 JSON".format("PASS" if ok else "FAIL"))
    results.append(ok)

    # 清理
    for f in os.listdir(tmpdir):
        try:
            os.remove(os.path.join(tmpdir, f))
        except OSError:
            pass
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass

    print("-" * 96)
    print(" 结果: {} passed, {} failed".format(sum(results), len(results) - sum(results)))
    print("=" * 96)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
