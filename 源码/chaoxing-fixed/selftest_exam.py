# -*- coding: utf-8 -*-
"""
考试看板自测（离线，用真实页面结构做夹具）
==========================================
夹具 HTML 来自实测（2026-09-11，账号 130****66 的「中国商贸文化」考试页）：

    <ul class="nav">
      <li data="https://mooc1-api.chaoxing.com/exam-ans/android/mtaskmsgspecial
                   ?taskrefId=10807686&...&type=exam&enc_task=e8e629869aee1fc63744e0499873fad9">
        <div aria-label="..."><p>中国商贸文化</p><span>待做</span>
             <span class="fr">剩余2522小时2分钟</span></div>
      </li>
    </ul>

验证：解析、剩余时间换算、待做/已完成归类、异常页面不崩、去重推送策略。
全程离线，不联网。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api.base as base_mod
from api.exam import ExamInfo, ExamWatch, parse_remain_hours

REAL_LI = """<ul class="nav">
<li data="https://mooc1-api.chaoxing.com/exam-ans/android/mtaskmsgspecial?taskrefId=10807686&amp;msgId=0&amp;courseId=266218484&amp;userId=416780389&amp;clazzId=152328696&amp;type=exam&amp;enc_task=e8e629869aee1fc63744e0499873fad9" data-stopexam="0" onclick="goTask(this);">
<img aria-hidden="true" src="//mooc1-api.chaoxing.com/exam-ans/images/exam/phone/task-exam.png" tabindex="-1"/>
<div aria-label="考试名称：中国商贸文化; 考试状态：待做;" role="text" tabindex="0">
<p>中国商贸文化</p>
<span>待做</span>
<span class="fr">剩余2522小时2分钟</span>
</div>
</li>
</ul>"""

DONE_LI = """<ul class="nav">
<li data="https://mooc1-api.chaoxing.com/exam-ans/android/mtaskmsgspecial?taskrefId=10755325&amp;courseId=266218735&amp;clazzId=152328990&amp;type=exam&amp;enc_task=e78f50c683063f5c16aaafbbc24c8ff5">
<div><p>中华文化才艺</p><span>已完成</span><span class="fr">剩余2522小时3分钟</span></div>
</li>
</ul>"""

# ---- 以下是「考试体检」用的封面页夹具，字段与标签来自实测页面（2026-09-11）----
# 正常可考封面页：不需要人脸、无屏幕监控，但需要验证码
COVER_OK = """<html><body>
<span class="overHidden2">中华文化才艺</span>
<input type="hidden" id="needFaceRecognition" value="0"/>
<input type="hidden" id="faceRecognitionCompare" name="faceRecognitionCompare" value=""/>
<input type="hidden" id="captchaCheck" value="1"/>
<input type="hidden" id="captchaCaptchaId" value="TEST_CAPTCHA_ID"/>
<input type="hidden" id="monitorLock" value="0"/>
<input type="hidden" id="screenMonitor" value=""/>
<input type="hidden" id="screenshotNumberLimit" value="0"/>
<input type="hidden" id="testUserRelationId" name="testUserRelationId" value="171393570"/>
<input type="hidden" id="isStartPage" value="1"/>
<script>var needcode = 0; var limitmin = 60;</script>
</body></html>"""

# 被门槛拦住的封面页（实测原话）
COVER_BLOCKED = """<html><body>
<h2 class="color6 fs36 textCenter marBom60 line64">该考试教师已设置章节任务点未完成90%，不能参加考试</h2>
</body></html>"""

# 构造一个「人脸识别 + 屏幕监控 + 邀请码全开」的页面，验证检出能力
COVER_STRICT = """<html><body>
<span class="overHidden2">严格监考示例</span>
<input type="hidden" id="needFaceRecognition" value="1"/>
<input type="hidden" id="captchaCheck" value="0"/>
<input type="hidden" id="monitorLock" value="1"/>
<input type="hidden" id="testUserRelationId" value="999"/>
<script>var needcode = 1;</script>
</body></html>"""


class FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, url, params=None, timeout=None):
        key = (params or {}).get("courseId")
        v = self.mapping.get(key, "")
        if isinstance(v, Exception):
            raise v
        return FakeResp(v)


class FakeNotify:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


def main():
    results = []
    print("=" * 100)
    print(" 考试看板自测（离线）")
    print("=" * 100)

    # ---- 1) 解析真实结构 ----
    exams = ExamWatch._parse({"title": "中国商贸文化", "courseId": "266218484",
                              "clazzId": "152328696", "cpi": "500640783"}, REAL_LI)
    e = exams[0] if exams else None
    ok = (e is not None and e.name == "中国商贸文化" and e.status == "待做"
          and e.exam_id == "10807686"
          and e.enc_task == "e8e629869aee1fc63744e0499873fad9"
          and e.remain_hours is not None and abs(e.remain_hours - (2522 + 2 / 60.0)) < 0.1)
    results.append(report("解析真实考试列表 HTML（名称/状态/examId/enc_task/剩余时间）", ok,
                          "  name={!r} status={!r} examId={!r} remain={:.1f}h".format(
                              e.name if e else None, e.status if e else None,
                              e.exam_id if e else None, e.remain_hours if e else -1)))

    # ---- 2) 剩余时间解析的多种写法 ----
    cases = [("剩余2522小时2分钟", 2522 + 2 / 60.0),
             ("剩余2天3小时", 51.0),
             ("剩余45分钟", 0.75),
             ("剩余1天", 24.0),
             ("", None),
             ("未知", None)]
    bad = []
    for text, want in cases:
        got = parse_remain_hours(text)
        if want is None:
            if got is not None:
                bad.append("{!r}->{!r}".format(text, got))
        elif got is None or abs(got - want) > 0.01:
            bad.append("{!r}->{!r}(期望{})".format(text, got, want))
    results.append(report("剩余时间文本解析（小时/分钟/天/空值）", not bad,
                          "  全部通过" if not bad else "  不符: " + "; ".join(bad)))

    # ---- 3) 待做 / 已完成 归类 ----
    done = ExamWatch._parse({"title": "中华文化才艺", "courseId": "266218735",
                             "clazzId": "152328990", "cpi": "500640783"}, DONE_LI)
    ok = (len(done) == 1 and done[0].done and not done[0].todo
          and exams[0].todo and not exams[0].done)
    results.append(report("状态归类：待做=todo、已完成=done", ok,
                          "  待做.todo={} 已完成.done={}".format(exams[0].todo, done[0].done)))

    # ---- 4) 异常页面不能崩 ----
    ok = True
    for bad_html in ("", "<html></html>", "<ul class='other'></ul>", "not html at all"):
        try:
            r = ExamWatch._parse({"title": "x"}, bad_html)
            ok = ok and (r == [])
        except Exception as ex:  # noqa: BLE001
            ok = False
            print("        异常: {}: {}".format(type(ex).__name__, ex))
    results.append(report("空/异常页面 → 返回空列表且不抛异常", ok))

    # ---- 5) fetch：一门课报错不影响另一门 ----
    fake = FakeSession({"266218484": REAL_LI,
                        "266218735": RuntimeError("模拟网络错误"),
                        "266218999": REAL_LI})
    real_get = base_mod.SessionManager.get_session
    base_mod.SessionManager.get_session = staticmethod(lambda: fake)
    try:
        got = ExamWatch().fetch([
            {"title": "中国商贸文化", "courseId": "266218484", "clazzId": "1", "cpi": "1"},
            {"title": "坏课程", "courseId": "266218735", "clazzId": "2", "cpi": "1"},
            {"title": "第三门", "courseId": "266218999", "clazzId": "3", "cpi": "1"},
        ])
    finally:
        base_mod.SessionManager.get_session = real_get
    ok = (len(got) == 2)
    results.append(report("某门课读取失败 → 跳过它，其余课程照常", ok, "  解析到 {} 场考试".format(len(got))))

    # ---- 6) 去重推送策略 ----
    tmpdir = tempfile.mkdtemp(prefix="exam_selftest_")
    state = os.path.join(tmpdir, "exam_state.json")
    notify = FakeNotify()
    w = ExamWatch(notification=notify, warn_hours=48, state_file=state)
    todo_exam = [ExamInfo("中国商贸文化", "1", "1", "1", "期末考试", "待做",
                          exam_id="10807686", remain_text="剩余2522小时2分钟",
                          remain_hours=2522.0)]

    # 第一次：应推送
    n0 = len(notify.sent)
    w._need_notify(todo_exam) and notify.send(w.render(todo_exam))
    first = len(notify.sent) > n0
    # 第二次：状态没变且不临近截止 → 不推送
    n1 = len(notify.sent)
    if w._need_notify(todo_exam):
        notify.send(w.render(todo_exam))
    second = len(notify.sent) > n1
    # 第三次：临近截止 → 每次都推
    urgent = [ExamInfo("中国商贸文化", "1", "1", "1", "期末考试", "待做",
                       exam_id="10807686", remain_text="剩余3小时", remain_hours=3.0)]
    n2 = len(notify.sent)
    if w._need_notify(urgent):
        notify.send(w.render(urgent))
    third = len(notify.sent) > n2
    ok = (first and not second and third)
    results.append(report("推送去重：首次推 / 状态未变压掉 / 临近截止每次都推", ok,
                          "  首次={} 重复={} 紧急={}".format(first, second, third)))

    # ---- 7) 报告渲染不崩且含关键信息 ----
    text = ExamWatch(notification=None).render(todo_exam + done)
    ok = ("考试看板" in text and "待完成 1 场" in text and "已完成 1 场" in text
          and "不会替你进考场" in text)
    results.append(report("看板文本渲染（含待完成/已完成/免责说明）", ok))

    # ---- 8) 就绪体检：正常可考封面页 ----
    from api.exam import ExamWatch as EW
    r = EW._parse_cover(todo_exam[0], COVER_OK)
    ok = (r.can_start and not r.need_face and r.need_captcha and not r.monitor
          and not r.need_code and r.relation_id == "171393570" and r.title == "中华文化才艺"
          and r.duration == "60 分钟")
    results.append(report("体检-正常封面：可考 / 无人脸 / 需验证码 / 无监控", ok,
                          "  can_start={} face={} captcha={} monitor={} code={} rel={}".format(
                              r.can_start, r.need_face, r.need_captcha, r.monitor, r.need_code, r.relation_id)))

    # ---- 9) 就绪体检：被章节任务点门槛拦住 ----
    r = EW._parse_cover(todo_exam[0], COVER_BLOCKED)
    ok = ((not r.can_start) and "章节任务点未完成90%" in r.reason)
    results.append(report("体检-被门槛拦住：不可考，且原因用服务端原话", ok,
                          "  reason={!r}".format(r.reason)))

    # ---- 10) 就绪体检：严格监考页面的检出能力 ----
    r = EW._parse_cover(todo_exam[0], COVER_STRICT)
    ok = (r.need_face and r.monitor and r.need_code and r.can_start)
    results.append(report("体检-严格监考：能检出人脸/监控/邀请码", ok,
                          "  face={} monitor={} code={}".format(r.need_face, r.monitor, r.need_code)))

    # ---- 11) 就绪体检：空页面不崩 ----
    r = EW._parse_cover(todo_exam[0], "")
    ok = bool((not r.can_start) and r.reason)
    results.append(report("体检-空封面页：不可考且给出原因（不崩）", ok, "  reason={!r}".format(r.reason[:40])))

    # ---- 12) 就绪行渲染 ----
    line = EW._parse_cover(todo_exam[0], COVER_OK).line()
    ok = ("✓ 可以考试" in line and "无需人脸" in line and "需要验证码" in line)
    results.append(report("体检-就绪行文本渲染", ok))
    print("        " + line.replace("\n", "\n        "))

    print("\n".join("        " + l for l in text.splitlines()[:8]))

    for f in os.listdir(tmpdir):
        try:
            os.remove(os.path.join(tmpdir, f))
        except OSError:
            pass
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass

    print("-" * 100)
    print(" 结果: {} passed, {} failed".format(sum(results), len(results) - sum(results)))
    print("=" * 100)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
