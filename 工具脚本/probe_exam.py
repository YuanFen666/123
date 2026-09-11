# -*- coding: utf-8 -*-
"""
考试接口探测器（只读）
======================
只做三件事：登录 -> 读课程列表 -> 用候选接口**只发 GET** 问一遍考试相关地址，
把真实的 URL / 状态码 / 返回结构打出来。不答题、不提交、不改任何数据。

目的：考试和学习通章节测验是两套完全不同的接口（exam-ans vs work），
网上没有权威文档，必须先拿真实账号探明端点和字段名，才能实现自动考试。

用法：
    python probe_exam.py                 # 默认读 ../../config.ini
    python probe_exam.py -c <config路径>
"""
import argparse
import configparser
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "源码", "chaoxing-fixed"))

from api.answer import Tiku
from api.base import Chaoxing, Account, SessionManager

CANDIDATES = [
    ("考试列表(exam-list, 带cpi/mooc2)",
     "https://mooc1.chaoxing.com/mooc-ans/exam-ans/exam/test/exam-list",
     {"classId": "{clazzId}", "courseId": "{courseId}", "cpi": "{cpi}", "ut": "s", "mooc2": 1}),
    ("考试列表(exam-list, 最简)",
     "https://mooc1.chaoxing.com/mooc-ans/exam-ans/exam/test/exam-list",
     {"classId": "{clazzId}", "courseId": "{courseId}", "ut": "s"}),
    ("作业/测验列表(getAllWork, 对照组)",
     "https://mooc1.chaoxing.com/mooc-ans/work/getAllWork",
     {"classId": "{clazzId}", "courseId": "{courseId}", "isdisplaytable": 2, "mooc": 1, "ut": "s"}),
    ("课程页(mooc2 studentcourse, 用于从 HTML 里找考试入口)",
     "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse",
     {"courseid": "{courseId}", "clazzid": "{clazzId}", "cpi": "{cpi}", "ut": "s"}),
]


def brief(text, limit=400):
    t = re.sub(r"\s+", " ", text or "")
    return t[:limit]


def show(title, resp):
    ct = resp.headers.get("Content-Type", "")
    print("\n  >>> {}".format(title))
    print("      {} {}  [{}]  {} 字节".format(
        resp.status_code, resp.reason, ct.split(";")[0], len(resp.content)))
    if resp.status_code != 200:
        print("      body: {}".format(brief(resp.text, 200)))
        return
    if "json" in ct or resp.text.lstrip().startswith(("{", "[")):
        try:
            data = resp.json()
            print("      JSON 顶层键: {}".format(
                list(data.keys())[:20] if isinstance(data, dict) else "list(len=%d)" % len(data)))
            print("      内容: {}".format(brief(json.dumps(data, ensure_ascii=False), 500)))
            return
        except Exception:
            pass
    # HTML：找考试相关线索
    hits = re.findall(r"[^\"'\s]*exam[^\"'\s]{0,120}", resp.text, re.I)
    uniq = []
    for h in hits:
        if h not in uniq:
            uniq.append(h)
    print("      命中 exam 的片段 {} 条:".format(len(uniq)))
    for h in uniq[:12]:
        print("        {}".format(h[:150]))
    if not uniq:
        print("      未命中 exam；正文片段: {}".format(brief(resp.text, 200)))


def main():
    ap = argparse.ArgumentParser()
    default_conf = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config.ini"))
    ap.add_argument("-c", "--config", default=default_conf)
    ap.add_argument("-n", "--courses", type=int, default=2, help="最多探测几门课")
    args = ap.parse_args()

    cfg = configparser.ConfigParser()
    cfg.read(args.config, encoding="utf-8-sig")
    common = dict(cfg.items("common"))
    username, password = common.get("username", "").strip(), common.get("password", "").strip()
    use_cookies = str(common.get("use_cookies", "false")).lower() == "true"

    print("=" * 100)
    print(" 考试接口探测器（只读）")
    print(" 账号: {}   登录方式: {}".format(username[:3] + "****" + username[-2:],
                                            "cookies" if use_cookies else "账号密码"))
    print("=" * 100)

    tiku = Tiku()
    tiku.DISABLE = True          # 探测过程不查题
    cx = Chaoxing(account=Account(username, password), tiku=tiku)
    st = cx.login(login_with_cookies=use_cookies)
    print("登录结果: {}".format(st))
    if not st.get("status"):
        return 1

    courses = cx.get_course_list()
    print("\n课程数: {}".format(len(courses)))
    for c in courses[:args.courses]:
        print("\n" + "=" * 100)
        print("课程: {}  (courseId={} clazzId={} cpi={})".format(
            c.get("title"), c.get("courseId"), c.get("clazzId"), c.get("cpi")))
        print("=" * 100)
        for title, url, params in CANDIDATES:
            p = {k: str(v).format(**c) for k, v in params.items()}
            try:
                r = SessionManager.get_session().get(url, params=p, timeout=20)
                show(title, r)
            except Exception as e:  # noqa: BLE001
                print("\n  >>> {}\n      请求异常: {}: {}".format(title, type(e).__name__, e))
            time.sleep(1.0)

    print("\n" + "=" * 100)
    print(" 探测结束（未做任何提交）")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
