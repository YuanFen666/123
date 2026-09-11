# -*- coding: utf-8 -*-
"""
考试作答模块（真正进入考场、答题、可选交卷）
==============================================
协议来源：https://github.com/jexjws/CxKitty （GPL-3.0，作者 SocialSisterYi）。
本项目按其协议自己实现，并做了三处安全改造（见下）。

流程
----
    GET  exam-ans/exam/phone/task-exam            考试封面（拿 examAnswerId / 是否要验证码 / 人脸）
    GET  captcha.chaoxing.com/captcha/...         滑块验证码（如果需要）
    GET  exam-ans/exam/phone/start                开考（302，Location 里带 enc）—— 从这一刻开始计时！
    GET  exam-ans/exam/phone/loadAnswerStatic     答题卡（拿到总题数与哪些题已答）
    GET  exam-ans/exam/test/reVersionTestStartNew 逐题取题（同时刷新 enc/计时参数）
    POST exam-ans/exam/test/reVersionSubmitTestNew 逐题保存（tempSave=true）/ 交卷（tempSave=false）

【安全改造（与参考实现的差异）】
1. **默认不自动交卷**：逐题作答并保存（tempSave=true）后停下，把「请核对后自己交卷」打出来。
   只有显式打开 auto_submit 且作答覆盖率达标时才交卷。
2. **不覆盖已有答案**：答题卡里显示"已答"的题直接跳过，避免把你自己答过的对题改成错的。
3. **失败要喊人**：一旦开考后出现任何异常，日志会反复提醒「考试已开始，请立刻到手机/网页手动完成」，
   并把剩余时间打出来 —— 因为开考即计时、到点自动交卷，代码挂了不能让你不知情。

⚠️ 现实约束：本模块**无法离线验证**（取题/提交都要真进考场）。第一次跑就是你的正式考试。
   建议第一次用默认的「只作答不交卷」，人工核对后再交。
"""
import json
import random
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from hashlib import md5
from math import floor
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from api.logger import logger

# ---------------- 接口 ----------------
EXAM_COVER = "https://mooc1-api.chaoxing.com/exam-ans/exam/phone/task-exam"
EXAM_START = "https://mooc1-api.chaoxing.com/exam-ans/exam/phone/start"
EXAM_SHEET = "https://mooc1-api.chaoxing.com/exam-ans/exam/phone/loadAnswerStatic"
EXAM_FETCH = "https://mooc1-api.chaoxing.com/exam-ans/exam/test/reVersionTestStartNew"
EXAM_SUBMIT = "https://mooc1.chaoxing.com/exam-ans/exam/test/reVersionSubmitTestNew"

CAPTCHA_CONF = "https://captcha.chaoxing.com/captcha/get/conf"
CAPTCHA_IMAGE = "https://captcha.chaoxing.com/captcha/get/verification/image"
CAPTCHA_CHECK = "https://captcha.chaoxing.com/captcha/check/verification/result"

# ---------------- 题型（考试页 input[name^=type] 的取值）----------------
QT_SINGLE, QT_MULTI, QT_BLANK, QT_JUDGE = 0, 1, 2, 3
QT_NAME = {0: "单选题", 1: "多选题", 2: "填空题", 3: "判断题", 4: "简答题"}

# 考试页给的时间文本
_REMAIN = re.compile(r"(\d+)\s*分")

# 一个进程一个虚拟设备号（参考实现也是随机的，服务端只当标识用）
_IMEI = secrets.token_hex(16)


def get_imei() -> str:
    return _IMEI


def get_ts() -> str:
    return "{}".format(round(time.time() * 1000))


def get_exam_signature(uid, qid, x, y) -> dict:
    """
    计算提交接口的签名参数组（pos / rd / value / _edt）。

    这是**请求完整性签名**（防篡改/防重放），x、y 只是参与哈希的随机数
    （参考实现同样传 random，服务端并不校验它是不是真实点击位置）。
    """
    ts = get_ts()
    r1 = random.randrange(0, 9)
    r2 = random.randrange(0, 9)
    a = "{}{}{}{}{}".format(secrets.token_hex(16), ts[4:], r1, r2, qid or "")
    temp = 0
    for ch in a:
        temp = (temp << 5) - temp + ord(ch)
    salt = "{}{}{}".format(r1, r2, (0x7FFFFFFF & temp) % 10)
    enc_val = "{}".format(uid)
    if qid:
        enc_val += "_{}".format(qid)
    enc_val += "|{}".format(salt)
    enc_val2 = "".join(str(ord(c)) for c in enc_val)
    b = len(enc_val2) // 5
    c = int(enc_val2[b] + enc_val2[2 * b] + enc_val2[3 * b] + enc_val2[4 * b])
    d = len(enc_val) // 2 + 1
    e = (c * int(enc_val2[:10]) + d) % 0x7FFFFFFF
    pos = "({}|{})".format(x, y)
    result = ""
    for ch in pos:
        temp = ord(ch) ^ floor(e / 0x7FFFFFFF * 0xFF)
        result += "{:02x}".format(temp)
        e = (c * e + d) % 0x7FFFFFFF
    return {
        "pos": "{}{}".format(result, secrets.token_hex(4)),
        "rd": random.random(),
        "value": pos,
        "_edt": "{}{}".format(ts, salt),
    }


# ---------------------------------------------------------------------------
# 滑块验证码
# ---------------------------------------------------------------------------
class SlideCaptcha:
    """超星图形验证码（滑块）。识别用模板匹配，失败回退 ddddocr。"""

    def __init__(self, session, captcha_id: str, referer: str, version: str = "1.1.20"):
        self.session = session
        self.captcha_id = captcha_id
        self.referer = referer
        self.version = version
        self.server_time = 0
        self.iv = ""
        self.token = ""

    @staticmethod
    def _parse_callback(text: str) -> dict:
        m = re.search(r"cx_captcha_function\((\{.*\})\)", text, re.S)
        if not m:
            raise ValueError("验证码接口返回格式不认识: {}".format(text[:120]))
        return json.loads(m.group(1))

    def _get_server_time(self):
        r = self.session.get(CAPTCHA_CONF, params={
            "callback": "cx_captcha_function", "captchaId": self.captcha_id, "_": get_ts(),
        }, headers={"Referer": self.referer}, timeout=20)
        self.server_time = self._parse_callback(r.text)["t"]

    def _get_images(self):
        captcha_key = md5("{}{}".format(self.server_time, uuid.uuid4()).encode()).hexdigest()
        self.iv = md5("{}{}{}{}".format(
            self.captcha_id, "slide", get_ts(), uuid.uuid4()).encode()).hexdigest()
        token = md5("{}{}{}{}".format(
            self.server_time, self.captcha_id, "slide", captcha_key).encode()).hexdigest()
        r = self.session.get(CAPTCHA_IMAGE, params={
            "callback": "cx_captcha_function", "captchaId": self.captcha_id, "type": "slide",
            "version": self.version, "captchaKey": captcha_key,
            "token": "{}:{}".format(token, self.server_time + 300000),
            "referer": self.referer, "iv": self.iv, "_": get_ts(),
        }, headers={"Referer": self.referer}, timeout=20)
        data = self._parse_callback(r.text)
        self.token = data["token"]
        vo = data["imageVerificationVo"]
        return vo["shadeImage"], vo["cutoutImage"]

    def _match(self, shade: bytes, cutout: bytes) -> int:
        """算缺口 x 坐标。三级降级：cv2 -> ddddocr -> 纯 PIL。"""
        # 主方案：OpenCV 模板匹配（与参考实现一致）
        try:
            import cv2
            import numpy as np
            s = cv2.imdecode(np.frombuffer(shade, np.uint8), cv2.IMREAD_COLOR)
            c = cv2.imdecode(np.frombuffer(cutout, np.uint8), cv2.IMREAD_COLOR)
            gray = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
            contours, _ = cv2.findContours(gray, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                _, cy, _, _ = cv2.boundingRect(contours[0])
                c_teil = c[cy + 2: cy + 44, 8:48]
                s_teil = s[max(0, cy - 2): cy + 50]
                res = cv2.matchTemplate(s_teil, c_teil, cv2.TM_CCOEFF_NORMED)
                _, _, _, max_loc = cv2.minMaxLoc(res)
                return int(max_loc[0]) - 5
        except Exception as e:  # noqa: BLE001
            logger.debug("cv2 识别滑块失败 -> {}: {}".format(type(e).__name__, e))

        # 备用一：ddddocr 自带滑块
        try:
            import ddddocr
            det = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
            return int(det.slide_match(cutout, shade, simple_target=True)["target"][0])
        except Exception as e:  # noqa: BLE001
            logger.debug("ddddocr 识别滑块失败 -> {}: {}".format(type(e).__name__, e))

        # 备用二：只用 PIL（打包进 exe 的就是这条，体积代价最小）
        return self._match_pil(shade, cutout)

    @staticmethod
    def _match_pil(shade: bytes, cutout: bytes) -> int:
        """
        纯 PIL 的滑块匹配：把滑块图裁出来，在背景图上从左往右滑，
        用 ImageChops.difference 算平均差，差最小的位置就是缺口。
        用 PIL 的 C 实现做像素运算，比纯 Python 循环快得多，也不需要 numpy/cv2。
        """
        import io
        from PIL import Image, ImageChops, ImageStat
        bg = Image.open(io.BytesIO(shade)).convert("L")
        piece_img = Image.open(io.BytesIO(cutout))
        # 滑块图通常是带透明通道的，用 alpha 求真实形状的包围盒
        if piece_img.mode in ("RGBA", "LA"):
            alpha = piece_img.convert("RGBA").split()[-1]
            bbox = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
        else:
            bbox = None
        if not bbox:
            # 没有透明通道：把近白像素当背景
            gray = piece_img.convert("L")
            bbox = gray.point(lambda v: 255 if v < 235 else 0).getbbox()
        if not bbox:
            raise RuntimeError("滑块图是空的，识别不了")
        piece = piece_img.convert("L").crop(bbox)
        pw, ph = piece.size

        best_score, best_x = None, 0
        for x in range(0, max(1, bg.size[0] - pw)):
            win = bg.crop((x, bbox[1], x + pw, bbox[1] + ph))
            score = sum(ImageStat.Stat(ImageChops.difference(win, piece)).mean)
            if best_score is None or score < best_score:
                best_score, best_x = score, x
        return int(best_x)

    def _check(self, x: int) -> str:
        r = self.session.get(CAPTCHA_CHECK, params={
            "callback": "cx_captcha_function", "captchaId": self.captcha_id, "type": "slide",
            "token": self.token, "textClickArr": json.dumps([{"x": x}], separators=(",", ":")),
            "coordinate": "[]", "runEnv": 10, "version": self.version, "t": "a",
            "iv": self.iv, "_": get_ts(),
        }, headers={"Referer": self.referer}, timeout=20)
        data = self._parse_callback(r.text)
        if data.get("result") is True:
            return data.get("validate") or ""
        raise RuntimeError("验证码校验未通过: {}".format(str(data)[:120]))

    def solve(self, max_try: int = 3) -> str:
        self._get_server_time()
        last = None
        for i in range(max_try):
            try:
                shade, cutout = self._get_images()
                x = self._match(shade, cutout)
                logger.info("滑块验证码：第 {} 次尝试，识别缺口 x={}".format(i + 1, x))
                return self._check(x)
            except Exception as e:  # noqa: BLE001
                last = e
                logger.warning("滑块验证码第 {} 次未通过 -> {}".format(i + 1, e))
                time.sleep(1.0)
        raise RuntimeError("滑块验证码连续 {} 次失败: {}".format(max_try, last))


# ---------------------------------------------------------------------------
# 题目
# ---------------------------------------------------------------------------
@dataclass
class ExamQuestion:
    id: int
    type: int
    title: str
    options: Dict[str, str] = field(default_factory=dict)   # 选项 key -> 文本
    blanks: List[str] = field(default_factory=list)         # 填空题各空的提示
    old_answer: str = ""                                    # 服务器上已有的答案
    index: int = 0

    @property
    def type_name(self) -> str:
        return QT_NAME.get(self.type, "未知题型({})".format(self.type))

    def options_text(self) -> str:
        return "\n".join("{}. {}".format(k, v) for k, v in self.options.items())

    def to_qinfo(self) -> dict:
        """转成本项目题库链认识的题目格式。"""
        t = {QT_SINGLE: "single", QT_MULTI: "multiple",
             QT_JUDGE: "judgement", QT_BLANK: "completion"}.get(self.type, "single")
        return {"title": self.title, "options": self.options_text(), "type": t}


def _remove_escape(text: str) -> str:
    return (text or "").replace("\xa0", " ").replace("\u2002", "").replace("\u200b", "").replace("\u3000", "").strip()


def parse_question(node, index: int = 0) -> ExamQuestion:
    """解析单题（结构来自真实考试页，见模块头注释）。"""
    qid = int(node.select_one("input[name='questionId']")["value"])
    qtype = int(node.select_one("input[name^='type']")["value"])

    # 题干：div.tit 里除了 <h3>（题型标题）和 <span>（分值/题号样式）之外的都是题干。
    # 【注意】不要按固定下标切 children —— 不同页面/题型的层级不一样，
    # 早先按 children[4:] 切会把题干整段切掉（自测里就是这么发现的）。
    tit = node.select_one("div.tit")
    title = ""
    if tit:
        parts = []
        for tag in tit.children:
            name = getattr(tag, "name", None)
            if name in ("h3", "span"):
                continue
            parts.append(tag.get_text() if hasattr(tag, "get_text") else str(tag))
        title = "".join(parts)
        title = re.sub(r"^\s*\d+\s*[.、]\s*", "", title)     # 去掉题号 "1."
    title = _remove_escape(title)

    q = ExamQuestion(id=qid, type=qtype, title=title, index=index)

    inp = node.select_one("input[id^='answer']")
    q.old_answer = (inp.get("value") if inp else "") or ""

    if qtype in (QT_SINGLE, QT_MULTI, QT_JUDGE):
        # 判断题也有选项（A 正确 / B 错误），解析出来才能按字母映射
        for opt in node.select("div.answerList.radioList"):
            key = opt.get("name") or ""
            cc = opt.select_one("cc")
            q.options[key] = _remove_escape("".join(cc.strings) if cc else "")
    elif qtype == QT_BLANK:
        for blank in node.select("div.completionList.objectAuswerList"):
            span = blank.select_one("span.grayTit")
            q.blanks.append(_remove_escape(span.get_text() if span else ""))
    return q


# ---------------------------------------------------------------------------
# 答案 -> 考试表单值
# ---------------------------------------------------------------------------
def _clean(s) -> str:
    return re.sub(r"^[A-Za-z]|[.,!?;:，。！？；：\s、]", "", str(s or ""))


def _subseq(a: str, o: str) -> bool:
    it = iter(o)
    return all(c in it for c in a)


def to_option_keys(answer: str, options: Dict[str, str]) -> str:
    """把题库给的答案（选项原文，或字母）映射成考试选项 key，如 'A' / 'AC'。"""
    text = str(answer or "").strip()
    if not text or not options:
        return ""
    keys = list(options.keys())
    # 纯字母形式（ASCII 字母且都在选项 key 里）
    letters = "".join(ch for ch in text if ch.isascii() and ch.isalpha()).upper()
    if letters and set(letters) <= set(k.upper() for k in keys):
        return "".join(k for k in keys if k.upper() in set(letters))

    def match(part: str) -> str:
        t = _clean(part)
        if not t:
            return ""
        for k, v in options.items():
            if _subseq(t, _clean(v)) or t in v:
                return k
        return ""

    # 【顺序很重要】先拿整串去匹配：选项原文里本身就可能带顿号/逗号
    # （比如选项是 "北京、上海"），先拆开反而谁都匹配不上。
    whole = match(text)
    if whole:
        return whole

    # 整串匹配不上，再按常见分隔符拆开逐段匹配（多选答案常见形式）
    picked = []
    for part in re.split(r"[#\n、,，;；|]+", text):
        k = match(part)
        if k and k not in picked:
            picked.append(k)
    return "".join(k for k in keys if k in picked)


_TRUE = ("正确", "对", "√", "✓", "是", "true", "t", "a", "1")


def judgement_value(answer: str, options: Dict[str, str]) -> str:
    t = str(answer or "").strip().lower()
    if t in ("true", "false"):
        return t
    keys = to_option_keys(answer, options)
    if keys:
        k = keys[0]
        return "true" if k.upper() in ("A", "T", "1") else "false"
    for w in ("错误", "不正确", "不对", "错", "×", "x", "否", "false", "f"):
        if w in t:
            return "false"
    for w in _TRUE:
        if w in t:
            return "true"
    return ""


# ---------------------------------------------------------------------------
# 考试会话
# ---------------------------------------------------------------------------
class ExamAborted(Exception):
    """开考之前就失败 —— 没有消耗考试机会，安全。"""


class ExamInProgressError(Exception):
    """已经开考之后失败 —— 计时在跑，必须让用户知道去手动完成。"""


class ExamTaker:
    def __init__(self, course: dict, exam, tiku=None, *, auto_submit: bool = False,
                 min_cover: float = 0.9, min_remain_min: int = 5, overwrite: bool = False,
                 max_questions: int = 200):
        self.course = course
        self.exam = exam                      # api.exam.ExamInfo
        self.tiku = tiku
        self.auto_submit = bool(auto_submit)
        self.min_cover = float(min_cover)
        self.min_remain_min = int(min_remain_min)
        self.overwrite = bool(overwrite)
        self.max_questions = int(max_questions)

        self.exam_id = str(exam.exam_id)
        self.class_id = str(course.get("clazzId", ""))
        self.course_id = str(course.get("courseId", ""))
        self.cpi = str(course.get("cpi", ""))
        self.uid = ""

        self.title = getattr(exam, "name", "") or ""
        self.exam_answer_id = ""
        self.need_captcha = False
        self.need_face = False
        self.captcha_id = ""
        self.captcha_validate = ""

        self.enc = ""
        self.enc_remain_time = 0
        self.remain_time = 0
        self.last_update_time = 0
        self.started = False
        self.stats = {"total": 0, "answered": 0, "skipped": 0, "failed": 0}

    # ---------------- 基础 ----------------
    @property
    def session(self):
        from api.base import SessionManager
        return SessionManager.get_session()

    def _uid(self) -> str:
        if not self.uid:
            self.uid = self.session.cookies.get("_uid") or ""
        return self.uid

    def remain_min(self) -> Optional[float]:
        if self.enc_remain_time:
            return self.enc_remain_time / 60.0
        if self.remain_time:
            return self.remain_time / 60.0
        return None

    def _warn_in_progress(self, why: str):
        r = self.remain_min()
        logger.error("=" * 90)
        logger.error("考试已经开始，但程序在「{}」这一步出错了！".format(why))
        logger.error("【请立刻打开手机 App 或网页版继续这场考试，别等程序】")
        if r is not None:
            logger.error("服务端显示剩余时间约 {:.0f} 分钟，到点会自动交卷。".format(r))
        logger.error("=" * 90)

    # ---------------- 1. 封面 ----------------
    def load_cover(self) -> None:
        r = self.session.get(EXAM_COVER, params={
            "taskrefId": self.exam_id, "courseId": self.course_id, "classId": self.class_id,
            "userId": self._uid(), "role": "", "source": 0,
            "enc_task": getattr(self.exam, "enc_task", ""), "cpi": self.cpi, "vx": 0,
        }, timeout=25, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            raise ExamAborted("这场考试已经交过卷了（服务端跳转 {}）".format(r.headers.get("Location", "")[:80]))
        if r.status_code != 200:
            raise ExamAborted("封面页 HTTP {}".format(r.status_code))
        soup = BeautifulSoup(r.text, "lxml")
        if (h2 := soup.find("h2")):
            raise ExamAborted("不能参加考试：{}".format(h2.get_text(strip=True)))

        def val(sel):
            n = soup.select_one(sel)
            return (n.get("value") or "").strip() if n and n.has_attr("value") else ""

        self.exam_answer_id = val("input#testUserRelationId")
        self.need_face = val("input#needFaceRecognition") not in ("", "0") or \
            val("input#faceRecognitionCompare") not in ("", "0")
        self.need_captcha = val("input#captchaCheck") not in ("", "0")
        self.captcha_id = val("input#captchaCaptchaId")
        t = soup.select_one("span.overHidden2")
        if t:
            self.title = t.get_text(strip=True)
        if not self.exam_answer_id:
            raise ExamAborted("封面页没解析出 examAnswerId（页面结构可能变了）")
        if self.need_face:
            raise ExamAborted("这场考试要求人脸识别，本项目不处理")
        logger.info("考试封面解析成功：{}（是否需要验证码: {}）".format(
            self.title, "是" if self.need_captcha else "否"))

    # ---------------- 2. 验证码 ----------------
    def solve_captcha(self) -> None:
        if not self.need_captcha:
            return
        logger.info("该考试需要图形验证码，开始识别（最多 3 次）...")
        self.captcha_validate = SlideCaptcha(
            self.session, self.captcha_id, EXAM_COVER).solve()
        logger.info("验证码已通过")

    # ---------------- 3. 开考 ----------------
    def start(self) -> None:
        r = self.session.get(EXAM_START, params={
            "courseId": self.course_id, "classId": self.class_id, "examId": self.exam_id,
            "source": 0, "examAnswerId": self.exam_answer_id, "cpi": self.cpi,
            "keyboardDisplayRequiresUserAction": 1, "imei": get_imei(),
            "faceDetection": 0, "facekey": "", "faceDetectionResult": "",
            "captchavalidate": self.captcha_validate, "jt": 0, "code": "",
        }, timeout=25, allow_redirects=False)

        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            tip = soup.select_one("p.blankTips,li.msg")
            raise ExamAborted("开考被拒：{}".format(tip.get_text(strip=True) if tip else r.text[:120]))
        if r.status_code not in (301, 302, 303, 307, 308):
            raise ExamAborted("开考返回意外状态 HTTP {}".format(r.status_code))

        loc = r.headers.get("Location", "")
        m = re.search(r"[?&]enc=([^&]+)", loc)
        if not m:
            raise ExamAborted("开考重定向里没有 enc 参数: {}".format(loc[:120]))
        self.enc = m.group(1)
        self.started = True
        logger.warning("已进入考场，计时开始！")
        self.fetch(0)   # 第一题会把 enc/剩余时间刷新出来

    # ---------------- 4. 取题 ----------------
    def fetch(self, index: int) -> ExamQuestion:
        r = self.session.get(EXAM_FETCH, params={
            "courseId": self.course_id, "classId": self.class_id, "tId": self.exam_id,
            "id": self.exam_answer_id, "source": 0, "p": 1, "isphone": "true",
            "tag": int(self.enc_remain_time == 0), "cpi": self.cpi, "imei": get_imei(),
            "start": index, "enc": self.enc, "keyboardDisplayRequiresUserAction": 1,
            "monitorStatus": 0, "monitorOp": -1, "remainTimeParam": self.enc_remain_time,
            "relationAnswerLastUpdateTime": self.last_update_time,
        }, timeout=25)
        soup = BeautifulSoup(r.text, "lxml")
        tip = soup.body.select_one("p.blankTips") if soup.body else None
        if tip:
            msg = tip.get_text(strip=True)
            if msg in ("考试已经提交",):
                raise StopIteration("考试已提交")
            raise RuntimeError("取题失败：{}".format(msg))

        form = soup.select_one("form#submitTest")
        if form is None:
            raise RuntimeError("页面里没有 form#submitTest（可能被风控或页面结构变了）")
        for key, attr in (("enc", "enc"), ("enc_remain_time", "encRemainTime"),
                          ("remain_time", "remainTime"), ("last_update_time", "encLastUpdateTime")):
            node = form.select_one("input#{}".format(attr))
            if node is not None:
                try:
                    setattr(self, key, int(node["value"]) if key != "enc" else node["value"])
                except (TypeError, ValueError):
                    pass
        node = form.select_one("div.questionWrap.singleQuesId.ans-cc-exam")
        if node is None:
            raise RuntimeError("页面里没有题目节点")
        return parse_question(node, index)

    # ---------------- 5. 答题卡 ----------------
    def answer_sheet(self) -> Dict[str, Dict[int, bool]]:
        r = self.session.get(EXAM_SHEET, params={
            "courseId": self.course_id, "classId": self.class_id, "source": 0, "start": 0,
            "cpi": self.cpi, "examRelationId": self.exam_id, "imei": get_imei(),
            "examRelationAnswerId": self.exam_answer_id,
            "remainTimeParam": self.enc_remain_time,
            "relationAnswerLastUpdateTime": self.last_update_time, "enc": self.enc,
        }, timeout=25)
        soup = BeautifulSoup(r.text, "lxml")
        sheet: Dict[str, Dict[int, bool]] = {}
        for father in soup.select("ul"):
            h4 = father.select_one("h4.cardTit")
            if not h4:
                continue
            m = re.search(r"[一二三四五六七八九].*?、\s*(\S+)", h4.get_text())
            name = m.group(1) if m else "其它"
            sheet[name] = {int(li["data"]): ("complated" in (li.get("class") or []))
                           for li in father.select("li") if li.has_attr("data")}
        return sheet

    # ---------------- 6. 提交 ----------------
    def submit(self, index: int, question: Optional[ExamQuestion], final: bool = False) -> dict:
        qid = question.id if question else 0
        params = {
            "classId": self.class_id, "courseId": self.course_id, "cpi": self.cpi,
            "testPaperId": self.exam_id, "testUserRelationId": self.exam_answer_id,
            "tempSave": "false" if final else "true",
            **get_exam_signature(self._uid(), qid, random.randint(100, 1000), random.randint(100, 1000)),
            "qid": question.id if question else "", "version": 1,
        }
        data = {
            "courseId": self.course_id, "testPaperId": self.exam_id,
            "testUserRelationId": self.exam_answer_id, "classId": self.class_id,
            "type": 0, "isphone": "true", "imei": get_imei(), "subCount": "",
            "remainTime": self.remain_time, "tempSave": "false" if final else "true",
            "timeOver": "false", "encRemainTime": self.enc_remain_time,
            "encLastUpdateTime": self.last_update_time, "enc": self.enc,
            "userId": self._uid(), "source": 0, "start": index,
            "enterPageTime": self.last_update_time, "monitorforcesubmit": 0,
            "answeredView": 0, "exitdtime": 0,
        }
        if question is not None:
            data.update(self._answer_form(question))
        r = self.session.post(EXAM_SUBMIT, params=params, data=data, timeout=25)
        r.raise_for_status()
        js = r.json()
        if js.get("status") != "success":
            raise RuntimeError("提交失败：{}".format(js.get("msg")))
        if not final and js.get("data"):
            parts = str(js["data"]).split("|")
            if len(parts) >= 3:
                self.last_update_time, self.enc_remain_time, self.enc = int(parts[0]), int(parts[1]), parts[2]
        return js

    @staticmethod
    def _answer_form(q: ExamQuestion) -> dict:
        form = {
            "type{}".format(q.id): q.type, "questionId": q.id,
            "typeName{}".format(q.id): QT_NAME.get(q.type, ""), "hidetext": "",
        }
        if q.type == QT_SINGLE:
            form["answer{}".format(q.id)] = q.old_answer
        elif q.type == QT_MULTI:
            form["answers{}".format(q.id)] = q.old_answer
        elif q.type == QT_JUDGE:
            form["answer{}".format(q.id)] = q.old_answer
        elif q.type == QT_BLANK:
            parts = [p for p in re.split(r"[#\n]+", q.old_answer) if p.strip()] or [q.old_answer]
            num = ""
            for i, v in enumerate(parts, 1):
                form["answer{}{}".format(q.id, i)] = v
                num += "{}, ".format(i)
            form["blankNum{}".format(q.id)] = num
        return form

    # ---------------- 7. 用题库链作答 ----------------
    def ask(self, q: ExamQuestion) -> Optional[str]:
        if self.tiku is None:
            return None
        try:
            return self.tiku.query(q.to_qinfo())
        except Exception as e:  # noqa: BLE001
            logger.warning("题库查询异常 -> {}: {}".format(type(e).__name__, e))
            return None

    def fill(self, q: ExamQuestion) -> bool:
        """把题库答案写回 q.old_answer（交给 _answer_form 组装）；成功返回 True。"""
        ans = self.ask(q)
        if not ans:
            return False
        if q.type == QT_SINGLE:
            keys = to_option_keys(ans, q.options)
            if not keys:
                return False
            q.old_answer = keys[0]
        elif q.type == QT_MULTI:
            keys = to_option_keys(ans, q.options)
            if not keys:
                return False
            q.old_answer = "".join(sorted(set(keys)))
        elif q.type == QT_JUDGE:
            v = judgement_value(ans, q.options)
            if not v:
                return False
            q.old_answer = v
        else:
            q.old_answer = str(ans).strip()
        return True

    # ---------------- 8. 主流程 ----------------
    def run(self) -> dict:
        logger.info("=" * 90)
        logger.info("考试作答：{}（{}）".format(self.title, self.exam_id))
        logger.info("模式：{}".format("自动答题 + 自动交卷" if self.auto_submit
                                      else "自动答题但不交卷（答完你自己核对交卷）"))
        logger.info("=" * 90)

        self.load_cover()
        self.solve_captcha()
        self.start()

        try:
            sheet = self.answer_sheet()
            all_idx = sorted({i for v in sheet.values() for i in v})
            self.stats["total"] = len(all_idx)
            done = {i for v in sheet.values() for i, ok in v.items() if ok}
            logger.info("答题卡：共 {} 题，服务器上已答 {} 题".format(len(all_idx), len(done)))
        except Exception as e:  # noqa: BLE001
            logger.warning("拿答题卡失败（{}），改为逐题试到拉不动为止".format(e))
            all_idx, done = None, set()

        if all_idx is not None and self.min_remain_min > 0:
            r = self.remain_min()
            if r is not None and r < self.min_remain_min:
                logger.error("剩余时间只有 {:.0f} 分钟，低于安全线 {} 分钟，已停止作答。".format(
                    r, self.min_remain_min))
                self._warn_in_progress("剩余时间不足")
                return self.stats

        answered = skipped = failed = 0
        index = 0
        limit = len(all_idx) if all_idx else self.max_questions
        while index < limit:
            try:
                q = self.fetch(index)
            except StopIteration:
                logger.info("服务端表示没有更多题目了，取题结束")
                break
            except Exception as e:  # noqa: BLE001
                logger.error("取第 {} 题失败 -> {}: {}".format(index, type(e).__name__, e))
                self._warn_in_progress("取第 {} 题".format(index))
                break

            real_index = all_idx[index] if all_idx else index
            if real_index in done and not self.overwrite:
                logger.info("第 {} 题（{}{}）服务器上已答，跳过不覆盖".format(
                    index, q.type_name, "，你答过" if real_index in done else ""))
                skipped += 1
                index += 1
                continue

            logger.info("第 {} 题 [{}] {}".format(index, q.type_name, q.title[:60]))
            if q.options:
                logger.debug("  选项: {}".format(q.options_text().replace("\n", " | ")))
            if not self.fill(q):
                logger.warning("  题库没给出可用答案，跳过（该题留空）")
                failed += 1
                index += 1
                continue
            try:
                self.submit(index, q, final=False)
                answered += 1
                logger.info("  已作答并保存 -> {}".format(q.old_answer))
            except Exception as e:  # noqa: BLE001
                failed += 1
                logger.error("  第 {} 题提交失败 -> {}: {}".format(index, type(e).__name__, e))
                if "时间已用完" in str(e):
                    self._warn_in_progress("提交时提示时间已用完")
                    break
            index += 1

        self.stats.update(answered=answered, skipped=skipped, failed=failed)
        total = self.stats["total"] or (answered + failed)
        cover = (answered + skipped) / total if total else 0.0
        logger.info("=" * 90)
        logger.info("本次作答：保存 {} 题，跳过（原已答）{} 题，未答 {} 题；覆盖率 {:.0%}".format(
            answered, skipped, failed, cover))
        if r := self.remain_min():
            logger.info("服务端剩余时间约 {:.0f} 分钟".format(r))

        if not self.auto_submit:
            logger.warning("=" * 90)
            logger.warning("【未交卷】答案已逐题保存到服务器。请你打开手机/网页核对一遍，")
            logger.warning("          确认无误后自己点「交卷」。到时间系统也会自动交卷。")
            logger.warning("=" * 90)
            return self.stats

        if cover < self.min_cover:
            logger.error("覆盖率 {:.0%} 低于设定门槛 {:.0%}，为安全起见不自动交卷，请自己核对后交。".format(
                cover, self.min_cover))
            return self.stats
        try:
            self.submit(0, None, final=True)
            logger.warning("【已自动交卷】{}".format(self.title))
        except Exception as e:  # noqa: BLE001
            logger.error("自动交卷失败 -> {}: {}（请自己手动交卷）".format(type(e).__name__, e))
            self._warn_in_progress("自动交卷")
        return self.stats
