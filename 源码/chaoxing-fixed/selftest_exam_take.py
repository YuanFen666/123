# -*- coding: utf-8 -*-
"""
考试作答模块离线自测
====================
考证的是「不联网也验证得了」的那部分：
  - 提交签名 get_exam_signature 的形状与确定性（同样输入不崩、字段齐全）
  - 答案映射 to_option_keys / judgement_value（单选题、多选、判断题）
  - 考试页解析 parse_question（用真实页面结构做夹具）
  - 答案表单 _answer_form（每种题型的字段名）
  - 安全策略：已答的题不覆盖、覆盖率不足不交卷、未开考失败不算消耗机会

⚠️ 注意：进考场之后的真实请求（start/fetch/submit）**无法离线验证**，那部分只能真跑。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bs4 import BeautifulSoup

from api.exam_take import (ExamQuestion, ExamAborted, ExamTaker, get_exam_signature,
                           judgement_value, parse_question, to_option_keys, QT_SINGLE,
                           QT_MULTI, QT_JUDGE, QT_BLANK,
                           SlideCaptcha, CAPTCHA_CONF, CAPTCHA_IMAGE, CAPTCHA_CHECK)


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


# ---- 试题页面夹具（结构来自参考实现的解析选择器 + 真实考试页字段名）----
Q_SINGLE = """
<div class="answerMain questionWrap singleQuesId ans-cc-exam">
  <input type="hidden" name="questionId" value="5001"/>
  <input type="hidden" name="type5001" value="0"/>
  <div class="tit"><h3>单选题（共1题，5.0分）</h3>1.<span>（5.0分）</span>中华人民共和国的首都是？</div>
  <div class="answerList radioList" name="A"><cc>北京</cc></div>
  <div class="answerList radioList" name="B"><cc>上海</cc></div>
  <div class="answerList radioList" name="C"><cc>广州</cc></div>
  <input type="hidden" id="answer5001" value=""/>
</div>
"""

Q_MULTI = """
<div class="answerMain questionWrap singleQuesId ans-cc-exam">
  <input type="hidden" name="questionId" value="5002"/>
  <input type="hidden" name="type5002" value="1"/>
  <div class="tit"><h3>多选题</h3>2.下列属于水果的有？</div>
  <div class="answerList radioList" name="A"><cc>苹果</cc></div>
  <div class="answerList radioList" name="B"><cc>白菜</cc></div>
  <div class="answerList radioList" name="C"><cc>香蕉</cc></div>
  <input type="hidden" id="answers5002" value=""/>
</div>
"""

Q_JUDGE = """
<div class="answerMain questionWrap singleQuesId ans-cc-exam">
  <input type="hidden" name="questionId" value="5003"/>
  <input type="hidden" name="type5003" value="3"/>
  <div class="tit"><h3>判断题</h3>3.地球是圆的。</div>
  <div class="answerList radioList" name="A"><cc>正确</cc></div>
  <div class="answerList radioList" name="B"><cc>错误</cc></div>
  <input type="hidden" id="answer5003" value=""/>
</div>
"""

Q_BLANK = """
<div class="answerMain questionWrap singleQuesId ans-cc-exam">
  <input type="hidden" name="questionId" value="5004"/>
  <input type="hidden" name="type5004" value="2"/>
  <div class="tit"><h3>填空题</h3>4.中国的首都是____。</div>
  <div class="completionList objectAuswerList"><span class="grayTit">第1空</span>
     <textarea class="blanktextarea"></textarea></div>
  <input type="hidden" id="answer5004" value=""/>
</div>
"""


def parse(html):
    return parse_question(BeautifulSoup(html, "lxml").select_one("div.ans-cc-exam"), 0)


def main():
    results = []
    print("=" * 100)
    print(" 考试作答模块离线自测")
    print("=" * 100)

    # ---- 1) 解析题目 ----
    q = parse(Q_SINGLE)
    ok = (q.id == 5001 and q.type == QT_SINGLE and "首都" in q.title
          and q.options == {"A": "北京", "B": "上海", "C": "广州"})
    results.append(report("解析单选题（题号/题型/题干/选项）", ok,
                          "  id={} type={} options={}".format(q.id, q.type, q.options)))

    qm, qj, qb = parse(Q_MULTI), parse(Q_JUDGE), parse(Q_BLANK)
    ok = (qm.type == QT_MULTI and len(qm.options) == 3
          and qj.type == QT_JUDGE and qj.options.get("A") == "正确"
          and qb.type == QT_BLANK and len(qb.blanks) == 1)
    results.append(report("解析多选/判断/填空", ok,
                          "  多选{}项 判断A={} 填空{}空".format(
                              len(qm.options), qj.options.get("A"), len(qb.blanks))))

    # ---- 2) 答案 -> 选项 key ----
    cases = [
        ("北京", q.options, "A", "单选：原文"),
        ("A", q.options, "A", "单选：字母"),
        ("广州", q.options, "C", "单选：第三个"),
        ("苹果#香蕉", qm.options, "AC", "多选：# 连接"),
        ("苹果、香蕉", qm.options, "AC", "多选：顿号连接"),
        ("A,C", qm.options, "AC", "多选：字母带逗号"),
    ]
    bad = []
    for ans, opts, want, desc in cases:
        got = to_option_keys(ans, opts)
        if got != want:
            bad.append("{}: {!r}->{!r}(期望{!r})".format(desc, ans, got, want))
    results.append(report("答案映射 to_option_keys（文本/字母/多选分隔）", not bad,
                          "  全部正确" if not bad else "  不符: " + "; ".join(bad)))

    # ---- 3) 判断题 ----
    jc = [("正确", "true"), ("错误", "false"), ("√", "true"), ("×", "false"),
          ("对", "true"), ("不正确", "false"), ("A", "true"), ("B", "false")]
    bad = []
    for ans, want in jc:
        got = judgement_value(ans, qj.options)
        if got != want:
            bad.append("{!r}->{!r}(期望{!r})".format(ans, got, want))
    results.append(report("判断题归一化 judgement_value", not bad,
                          "  全部正确" if not bad else "  不符: " + "; ".join(bad)))

    # ---- 4) 答案表单字段名 ----
    t = ExamTaker.__new__(ExamTaker)          # 不跑 __init__，只测静态方法
    f1 = ExamTaker._answer_form(ExamQuestion(1, QT_SINGLE, "x", {"A": "a"}, old_answer="A"))
    f2 = ExamTaker._answer_form(ExamQuestion(2, QT_MULTI, "x", {"A": "a", "B": "b"}, old_answer="AB"))
    f3 = ExamTaker._answer_form(ExamQuestion(3, QT_JUDGE, "x", {}, old_answer="true"))
    f4 = ExamTaker._answer_form(ExamQuestion(4, QT_BLANK, "x", {}, old_answer="北京#上海"))
    ok = (f1.get("answer1") == "A" and f1.get("type1") == QT_SINGLE and "questionId" in f1
          and f2.get("answers2") == "AB"
          and f3.get("answer3") == "true"
          and f4.get("answer41") == "北京" and f4.get("answer42") == "上海"
          and "blankNum4" in f4)
    results.append(report("组装提交表单（单选 answer / 多选 answers / 判断 true|false / 填空分空）", ok,
                          "  单选={} 多选={} 判断={} 填空={}".format(
                              f1.get("answer1"), f2.get("answers2"), f3.get("answer3"),
                              [f4.get("answer41"), f4.get("answer42")])))

    # ---- 5) 签名 ----
    s1 = get_exam_signature(416780389, 5001, 500, 600)
    s2 = get_exam_signature(416780389, 5001, 500, 600)
    ok = (set(s1) == {"pos", "rd", "value", "_edt"}
          and s1["value"] == "(500|600)"
          and isinstance(s1["rd"], float)
          and len(s1["_edt"]) > 10
          and s1["pos"] != s2["pos"])       # 含随机数，两次不同才对
    results.append(report("提交签名 get_exam_signature（字段齐全、每次随机）", ok,
                          "  value={} edt={}…".format(s1["value"], s1["_edt"][:16])))
    ok = get_exam_signature(1, 0, 1, 1)["value"] == "(1|1)"   # qid 为 0（纯交卷）也不能崩
    results.append(report("签名在 qid=0（纯交卷）时也能算出来", ok))

    # ---- 6) 安全策略：未开考就失败 = 不消耗机会 ----
    import api.base as base_mod

    class FakeResp:
        status_code = 200
        text = '<html><body><h2 class="color6">该考试教师已设置章节任务点未完成90%，不能参加考试</h2></body></html>'
        headers = {}

    class FakeSession:
        cookies = {"_uid": "1"}

        def get(self, *a, **kw):
            return FakeResp()

    real_get = base_mod.SessionManager.get_session
    base_mod.SessionManager.get_session = staticmethod(lambda: FakeSession())
    from types import SimpleNamespace
    try:
        t = ExamTaker.__new__(ExamTaker)
        t.exam_id, t.course_id, t.class_id, t.cpi = "1", "2", "3", "4"
        t.uid, t.title, t.started = "1", "", False
        t.exam = SimpleNamespace(exam_id="1", enc_task="x", name="示例考试")
        try:
            t.load_cover()
            ok, why = False, "竟然没抛异常"
        except ExamAborted as e:
            ok, why = ("章节任务点未完成90%" in str(e)), str(e)
        except Exception as e:  # noqa: BLE001
            ok, why = False, "抛了别的异常: {}: {}".format(type(e).__name__, e)
    finally:
        base_mod.SessionManager.get_session = real_get
    results.append(report("开考前被门槛拦住 → 抛 ExamAborted（不消耗考试机会）", ok, "  " + why))

    # ---- 7) 安全策略：默认不自动交卷 ----
    ok = (ExamTaker.__init__.__defaults__ is not None)  # 占位，真正断言在下面
    t2 = ExamTaker.__new__(ExamTaker)
    import inspect
    sig = inspect.signature(ExamTaker.__init__)
    ok = (sig.parameters["auto_submit"].default is False
          and sig.parameters["overwrite"].default is False
          and sig.parameters["min_cover"].default >= 0.8)
    results.append(report("默认安全：auto_submit=False、不覆盖已答题、覆盖率门槛≥0.8", ok,
                          "  auto_submit={} overwrite={} min_cover={}".format(
                              sig.parameters["auto_submit"].default,
                              sig.parameters["overwrite"].default,
                              sig.parameters["min_cover"].default)))

    # ---- 8) 滑块识别（纯 PIL 路径，打包进 exe 用的就是这条）----
    # 造一张背景图，在已知位置"挖"出滑块，看能不能把 x 找回来。
    import io
    import random as _rnd
    from PIL import Image, ImageDraw
    from api.exam_take import SlideCaptcha

    W, H, PW, PH = 320, 160, 48, 44
    TARGET_X, TARGET_Y = 137, 60
    _rnd.seed(7)
    bg = Image.new("RGB", (W, H), (238, 238, 238))
    dr = ImageDraw.Draw(bg)
    for _ in range(90):
        x1, y1 = _rnd.randint(0, W), _rnd.randint(0, H)
        dr.line([x1, y1, x1 + 25, y1 + 18],
                fill=(_rnd.randint(0, 255), _rnd.randint(0, 255), _rnd.randint(0, 255)), width=3)
    # 滑块图与背景同尺寸，把缺口那块原样贴到对应位置（真实的超星滑块图就是这样）
    cut = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cut.paste(bg.crop((TARGET_X, TARGET_Y, TARGET_X + PW, TARGET_Y + PH)), (TARGET_X, TARGET_Y))
    b1, b2 = io.BytesIO(), io.BytesIO()
    bg.save(b1, "PNG")
    cut.save(b2, "PNG")
    try:
        got = SlideCaptcha._match_pil(b1.getvalue(), b2.getvalue())
        ok = abs(got - TARGET_X) <= 3
        detail = "  实际缺口 x={} 识别出的 x={}".format(TARGET_X, got)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, "  异常: {}: {}".format(type(e).__name__, e)
    results.append(report("滑块识别（纯 PIL，无 cv2/numpy 也能用）", ok, detail))

    # ---- 8b) 滑块识别：滑块图是「小图」的情况（真实接口很可能是这种）----
    # 上面 8 用的是「与背景同尺寸的滑块图」；这里用小图，且缺口处故意调亮，
    # 检验二维搜索 + 边缘匹配是否还找得准。
    small_piece = bg.crop((TARGET_X, TARGET_Y, TARGET_X + PW, TARGET_Y + PH))
    b3 = io.BytesIO()
    small_piece.save(b3, "PNG")
    bg_gap = bg.copy()
    # 把缺口区域整体调亮（模拟真实的"挖洞"效果）
    gap = bg.crop((TARGET_X, TARGET_Y, TARGET_X + PW, TARGET_Y + PH)).point(lambda v: min(255, v + 55))
    bg_gap.paste(gap, (TARGET_X, TARGET_Y))
    b4 = io.BytesIO()
    bg_gap.save(b4, "PNG")
    try:
        got = SlideCaptcha._match_pil(b4.getvalue(), b3.getvalue())
        ok = abs(got - TARGET_X) <= 4
        detail = "  真实缺口 x={} 识别出的 x={}（滑块为小图 + 缺口调亮）".format(TARGET_X, got)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, "  异常: {}: {}".format(type(e).__name__, e)
    results.append(report("滑块识别-小图模式 + 缺口调亮（不假设位置、对亮度不敏感）", ok, detail))

    # ---- 9) 整个滑块验证码流程（离线：伪造三个接口 + 图片下载）----
    # 这次上线第一次真跑就栽在「接口给的是图片 URL，不是图片字节」上，
    # 所以必须把整条链路（取时间->取图->下载->识别->校验）都测一遍。
    class CapResp:
        def __init__(self, text=None, content=b""):
            self.text = text or ""
            self.content = content

    class CapSession:
        """按 URL 分派：配置接口 / 取图接口 / 图片本身 / 校验接口"""
        def __init__(self, shade, cutout, result=True, validate="VALID-123", wrong_first=0):
            self.shade, self.cutout = shade, cutout
            self.result, self.validate = result, validate
            self.wrong_first = wrong_first
            self.calls = []
            self.check_x = []

        def get(self, url, **kw):
            self.calls.append(url)
            if url == CAPTCHA_CONF:
                return CapResp('cx_captcha_function({"t": 1700000000000})')
            if url == CAPTCHA_IMAGE:
                return CapResp('cx_captcha_function(' + json.dumps({
                    "token": "TK-1",
                    "imageVerificationVo": {
                        "shadeImage": "https://captcha.example/shade.png",
                        "cutoutImage": "https://captcha.example/cutout.png",
                    }}) + ')')
            if url.endswith("shade.png"):
                return CapResp(content=self.shade)
            if url.endswith("cutout.png"):
                return CapResp(content=self.cutout)
            if url == CAPTCHA_CHECK:
                arr = json.loads(kw["params"]["textClickArr"])
                self.check_x.append(arr[0]["x"])
                if len(self.check_x) <= self.wrong_first:
                    return CapResp('cx_captcha_function({"result": false})')
                # 【还原真实结构】validate 藏在 extraData（一个 JSON 字符串）里，
                # 不是顶层字段 —— 线上就是取错字段导致拿到空 validate。
                extra = json.dumps({"validate": self.validate})
                return CapResp('cx_captcha_function(' + json.dumps(
                    {"result": True, "extraData": extra}) + ')')
            raise AssertionError("测试没预料到的请求: {}".format(url))

    from api.exam_take import SlideCaptcha as SC

    # 造一张带已知缺口的滑块图
    W2, H2, PW2, PH2, TX2, TY2 = 300, 150, 46, 42, 121, 55
    _rnd.seed(11)
    bg2 = Image.new("RGB", (W2, H2), (240, 240, 240))
    d2 = ImageDraw.Draw(bg2)
    for _ in range(80):
        x1, y1 = _rnd.randint(0, W2), _rnd.randint(0, H2)
        d2.line([x1, y1, x1 + 20, y1 + 15],
                fill=(_rnd.randint(0, 255), _rnd.randint(0, 255), _rnd.randint(0, 255)), width=3)
    cut2 = Image.new("RGBA", (W2, H2), (0, 0, 0, 0))
    cut2.paste(bg2.crop((TX2, TY2, TX2 + PW2, TY2 + PH2)), (TX2, TY2))
    bb1, bb2 = io.BytesIO(), io.BytesIO()
    bg2.save(bb1, "PNG")
    cut2.save(bb2, "PNG")

    fs = CapSession(bb1.getvalue(), bb2.getvalue())
    try:
        val = SC(fs, "CAPTCHA-ID", "https://mooc1-api.chaoxing.com/").solve()
        ok = (val == "VALID-123" and len(fs.check_x) == 1 and abs(fs.check_x[0] - TX2) <= 3)
        detail = "  拿到 validate={!r}，提交的 x={}（真实缺口 {}）".format(val, fs.check_x, TX2)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, "  异常: {}: {}".format(type(e).__name__, e)
    results.append(report("滑块验证码全流程（配置->取图->下载->识别->校验）", ok, detail))

    # ---- 10) 校验第一次失败要自动重试 ----
    fs2 = CapSession(bb1.getvalue(), bb2.getvalue(), wrong_first=1)
    try:
        val = SC(fs2, "CAPTCHA-ID", "https://mooc1-api.chaoxing.com/").solve()
        ok = (val == "VALID-123" and len(fs2.check_x) == 2)
        detail = "  尝试 {} 次后通过".format(len(fs2.check_x))
    except Exception as e:  # noqa: BLE001
        ok, detail = False, "  异常: {}: {}".format(type(e).__name__, e)
    results.append(report("验证码校验失败会自动换图重试", ok, detail))

    # ---- 11) 移动端请求头（线上就是这条缺失导致「提交失败: 无效操作」）----
    from api.exam_take import exam_headers, tune_tiku_for_exam
    h = exam_headers()
    ok = ("com.chaoxing.mobile" in h.get("User-Agent", "")
          and h.get("X-Requested-With") == "com.chaoxing.mobile"
          and "Dalvik" in h.get("User-Agent", ""))
    results.append(report("考试请求带移动端 UA（否则提交被判「无效操作」）", ok,
                          "  UA={}…".format(h.get("User-Agent", "")[:52])))

    # ---- 12) 考试模式会压小题库链间隔（否则每题磨好几秒）----
    from api.answer import TikuIcodef, TikuAnevol, AI

    class FakeChain:
        def __init__(self):
            self.providers = [TikuIcodef(), TikuAnevol(), AI()]

    fc = FakeChain()
    fc.providers[0].min_interval = 1.5
    fc.providers[1].min_interval = 0.3
    fc.providers[2].min_interval_seconds = 3.0
    tune_tiku_for_exam(fc)
    ok = (fc.providers[0].min_interval <= 0.3 and fc.providers[2].min_interval_seconds <= 0.3)
    results.append(report("进考场前把题库链间隔压小（提速）", ok,
                          "  icodef={} ANEVOL={} AI={}".format(
                              fc.providers[0].min_interval, fc.providers[1].min_interval,
                              fc.providers[2].min_interval_seconds)))

    # ---- 13) 整卷预览页解析（结构来自 2026-09-12 用户导出的真实考试页）----
    # 当初"解析到 0 题"的真因就是选择器照错了页面结构，跟 openc 无关。
    # 这里用真实结构做夹具，并锁死最关键的一点：选项 key 必须取 span 的 data
    # （原始键），而不是显示出来的字母（乱序后的展示位置）。
    Q_PREVIEW = """
    <div id="sigleQuestionDiv_890718804" class="questionLi scroll_890718804 singleQuesId" data="890718804">
      <h3 class="mark_name colorDeep">1. <span class="colorShallow"
          aria-label="1. (单选题, 1.0分)">(单选题, 1.0 分)</span>
        <div style="overflow:hidden;"> 现代农林业的核心特征之一是运用现代科学技术和（）来实现高效可持续发展。 </div>
      </h3>
      <form>
        <input type="hidden" name="type890718804" value="0">
        <input type="hidden" name="questionId" value="890718804">
        <input type="hidden" name="typeName890718804" value="单选题">
        <input type="hidden" name="start" value="7">
        <input type="hidden" id="answer890718804" name="answer890718804" value="">
        <div class="stem_answer">
          <div class="clearfix answerBg" onclick="saveSingleSelect(this,'890718804');">
            <span data="B" qid="890718804" class="saveSingleSelect choice890718804 num_option fl">A</span>
            <div class="fl answer_p">自然条件</div>
          </div>
          <div class="clearfix answerBg" onclick="saveSingleSelect(this,'890718804');">
            <span data="A" qid="890718804" class="saveSingleSelect choice890718804 num_option fl">B</span>
            <div class="fl answer_p">传统经验</div>
          </div>
          <div class="clearfix answerBg" onclick="saveSingleSelect(this,'890718804');">
            <span data="D" qid="890718804" class="saveSingleSelect choice890718804 num_option fl">C</span>
            <div class="fl answer_p">手工工具</div>
          </div>
          <div class="clearfix answerBg" onclick="saveSingleSelect(this,'890718804');">
            <span data="C" qid="890718804" class="saveSingleSelect choice890718804 num_option fl">D</span>
            <div class="fl answer_p">现代工业装备和管理方法</div>
          </div>
        </div>
      </form>
    </div>
    """
    from api.exam_take import parse_preview_question
    pq = parse_preview_question(BeautifulSoup(Q_PREVIEW, "lxml").select_one("div.questionLi"), 0)
    ok = (pq.id == 890718804 and pq.type == QT_SINGLE
          and "现代农林业" in pq.title and "单选题" not in pq.title and "1.0 分" not in pq.title)
    results.append(report("整卷页解析：题号/题型/题干（题干已剔除题型分值）", ok,
                          "  id={} type={} 题干={}".format(pq.id, pq.type_name, pq.title[:26])))

    # ★ 最关键：选项 key 必须是 data（原始键），不是显示字母。用错就全盘皆错。
    ok = (pq.options == {"B": "自然条件", "A": "传统经验",
                         "D": "手工工具", "C": "现代工业装备和管理方法"})
    results.append(report("整卷页解析：选项 key 取 data（原始键）而非显示字母", ok,
                          "  -> {}".format(pq.options)))
    ok = to_option_keys("自然条件", pq.options) == "B"
    results.append(report("乱序下答案映射正确（'自然条件' -> B，不是显示位 A）", ok,
                          "  to_option_keys('自然条件') = {}".format(
                              to_option_keys("自然条件", pq.options))))

    print("-" * 100)
    print(" 结果: {} passed, {} failed".format(sum(results), len(results) - sum(results)))
    print("=" * 100)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
