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

# ---- 整卷模式（mooc2 网页版）----
# 【实测】有些考试（例如中华文化才艺）的客户端就是「整卷模式」：一页展示全部题目，
# 保存走 preview-save，且表单里必须有 paperId / examCreateUserId / examRelationId /
# answerMode / view / feedbackEnc。用手机单题模式的 reVersionSubmitTestNew 去打它，
# 服务端一律回 {"status":"error","msg":"提交失败：无效操作"}。
# 参考实现（CxKitty）只实现了单题模式，所以这套字段是从真实抓包里对出来的。
EXAM_PREVIEW_SAVE = "https://mooc1.chaoxing.com/exam-ans/exam/test/preview-save"
EXAM_MOOC2_PREVIEW = "https://mooc1.chaoxing.com/exam-ans/mooc2/exam/preview"
# 整卷模式的签名参数里还有一堆固定为 undefined 的占位
_PREVIEW_SIGN_PLACEHOLDERS = ("_signcode", "_signc", "_signe", "_signk",
                              "_cxcid", "_cxtime", "_signt")

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


# 移动端请求头。
# 【坑】考试走的是手机端接口（mooc1-api.chaoxing.com/exam-ans/exam/phone/*），
# 服务端会认客户端身份；用桌面 Chrome 的 UA 去 POST 提交，会被回
# 「无效操作」—— 实测踩到过（答案一条都存不进去）。参考实现的注释也写了
# "默认使用 APP 的 UA, 因为一些接口为 APP 独占"。
_ANDROID_VERSION = "Android 10"
_DEVICE_VENDOR = "MI11"
_APP_VERSION = "com.chaoxing.mobile/ChaoXingStudy_3_5.1.4_android_phone_614_74"


def exam_headers() -> dict:
    ua = " ".join((
        "Dalvik/2.1.0 (Linux; U; {}; {} Build/SKQ1.210216.001)".format(_ANDROID_VERSION, _DEVICE_VENDOR),
        "(device:{})".format(_DEVICE_VENDOR),
        "Language/zh_CN",
        _APP_VERSION,
        "(@Kalimdor)_{}".format(get_imei()),
    ))
    return {"User-Agent": ua, "X-Requested-With": "com.chaoxing.mobile"}


def web_headers(referer: str = "") -> dict:
    """整卷模式（mooc2 网页版）用的请求头：桌面 UA + XHR，实测抓包就是这样。"""
    h = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"),
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://mooc1.chaoxing.com",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    if referer:
        h["Referer"] = referer
    return h


def tune_tiku_for_exam(tiku) -> None:
    """
    考试是按时间走的，题库链那套「防限流慢间隔」在这里反而害事
    （每题 icodef 1.5s + ANEVOL 重试 + AI 3s，一道题能磨 5 秒以上）。
    进考场前把这些间隔压到最小，交卷速度优先。
    """
    if tiku is None:
        return
    targets = list(getattr(tiku, "providers", []) or [])
    if not targets and not hasattr(tiku, "providers"):
        targets = [tiku]
    for p in targets:
        name = type(p).__name__
        if name == "TikuIcodef":
            p.min_interval = 0.3
        elif name == "AI":
            p.min_interval_seconds = 0.3
        elif name == "TikuAnevol":
            p.min_interval = 0.2
        logger.debug("考试模式：已把 {} 的请求间隔调小".format(name))


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

    def _get_images(self) -> "Tuple[bytes, bytes]":
        """
        获取滑块图。

        【坑】这个接口返回的 shadeImage / cutoutImage 是图片 **URL**，不是图片本身，
        必须再 GET 一次把字节下下来 —— 早先直接把 URL 字符串丢给 PIL，
        报的是 "a bytes-like object is required, not 'str'"。
        """
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
        shade_url, cutout_url = vo["shadeImage"], vo["cutoutImage"]
        logger.debug("验证码图片地址: {} | {}".format(str(shade_url)[:90], str(cutout_url)[:90]))
        shade = self.session.get(shade_url, headers={"Referer": self.referer}, timeout=20).content
        cutout = self.session.get(cutout_url, headers={"Referer": self.referer}, timeout=20).content
        if not shade or not cutout:
            raise RuntimeError("验证码图片下载为空")
        return shade, cutout

    def _match(self, shade: bytes, cutout: bytes) -> int:
        """
        算缺口 x 坐标。

        【为什么只留 PIL 这条】
        参考实现用 cv2 时假设「滑块在 cutout 图里的 y 就等于它在背景里的 y」、
        且 x 固定取 8:48 —— 这只在「cutout 是整张图、滑块贴在固定位置」时成立。
        实测换成合成图后它直接返回 -5（明显跑飞），说明前提不可靠。
        所以这里改成**不做任何位置假设的二维搜索 + 边缘匹配**：
          - 二维搜索：y 也一起找，不假设滑块在 cutout 里的位置
          - 边缘匹配：用 FIND_EDGES 后的灰度做差，对"缺口处被调亮/压暗"不敏感
        这条路径只用 PIL，可以离线用合成图验证（自测里就是这么测的）。
        """
        return self._match_pil(shade, cutout)

    @staticmethod
    def _match_pil(shade: bytes, cutout: bytes, step: int = 1) -> int:
        import io
        from PIL import Image, ImageChops, ImageFilter, ImageStat

        bg = Image.open(io.BytesIO(shade)).convert("L")
        piece_img = Image.open(io.BytesIO(cutout))

        # 1) 取滑块的形状：优先用 alpha 通道，没有就按「非接近白色」算
        if piece_img.mode in ("RGBA", "LA"):
            alpha = piece_img.convert("RGBA").split()[-1]
            bbox = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
        else:
            bbox = None
        if not bbox:
            bbox = piece_img.convert("L").point(lambda v: 255 if v < 235 else 0).getbbox()
        if not bbox:
            raise RuntimeError("滑块图是空的，识别不了")

        piece = piece_img.convert("L").crop(bbox).filter(ImageFilter.FIND_EDGES)
        pw, ph = piece.size
        bg_edge = bg.filter(ImageFilter.FIND_EDGES)
        bw, bh = bg_edge.size

        best_score, best_xy = None, (0, 0)
        ys = range(0, max(1, bh - ph), step)
        xs = range(0, max(1, bw - pw), step)
        for y in ys:
            for x in xs:
                win = bg_edge.crop((x, y, x + pw, y + ph))
                score = sum(ImageStat.Stat(ImageChops.difference(win, piece)).mean)
                if best_score is None or score < best_score:
                    best_score, best_xy = score, (x, y)
        return int(best_xy[0])

    def _check(self, x: int) -> str:
        r = self.session.get(CAPTCHA_CHECK, params={
            "callback": "cx_captcha_function", "captchaId": self.captcha_id, "type": "slide",
            "token": self.token, "textClickArr": json.dumps([{"x": x}], separators=(",", ":")),
            "coordinate": "[]", "runEnv": 10, "version": self.version, "t": "a",
            "iv": self.iv, "_": get_ts(),
        }, headers={"Referer": self.referer}, timeout=20)
        data = self._parse_callback(r.text)
        if data.get("result") is True:
            # 【坑】验证码通过后的 validate **不在顶层**，而是塞在 extraData 里，
            # 而且 extraData 本身是个 JSON 字符串。取错字段就会拿到空串，
            # 开考时会被服务端判「验证码错误！」—— 实测踩到过。
            extra = data.get("extraData")
            if extra:
                try:
                    val = json.loads(extra).get("validate")
                    if val:
                        return str(val)
                except (TypeError, ValueError) as e:
                    logger.warning("解析验证码 extraData 失败 -> {}".format(e))
            if data.get("validate"):
                return str(data["validate"])
            logger.warning("验证码通过了但没解析出 validate，返回空值: {}".format(str(data)[:160]))
            return ""
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

    def match(part: str, allow_reverse: bool = False) -> str:
        t = _clean(part)
        if not t:
            return ""
        for k, v in options.items():
            cv = _clean(v)
            if not cv:
                continue
            if _subseq(t, cv) or t in v:
                return k
        if allow_reverse:
            # 最后兜底才用：选项被包在答案里（大模型常回 "答案为北京" 这种）
            for k, v in options.items():
                cv = _clean(v)
                if cv and cv in t:
                    return k
        return ""

    # 【顺序很重要】先拿整串做**严格**匹配：选项原文里本身就可能带顿号/逗号
    # （比如选项是 "北京、上海"），先拆开反而谁都匹配不上。
    # 注意这里不能用宽松匹配 —— 否则 "苹果#香蕉" 会被整串喂给选项 "苹果" 命中，
    # 多选题就只剩 A 了（自测里就是这么抓到的）。
    whole = match(text)
    if whole:
        return whole

    # 整串匹配不上，再按常见分隔符拆开逐段匹配（多选答案常见形式）
    picked = []
    for part in re.split(r"[#\n、,，;；|]+", text):
        k = match(part)
        if k and k not in picked:
            picked.append(k)
    if picked:
        return "".join(k for k in keys if k in picked)

    # 都不行，最后才用宽松匹配兜底
    return match(text, allow_reverse=True)


_TRUE = ("正确", "对", "√", "✓", "是", "true", "t", "a", "1")


def judgement_value(answer: str, options: Dict[str, str]) -> str:
    """判断题答案归一化成 'true' / 'false'。"""
    t = str(answer or "").strip().lower()
    if t in ("true", "false"):
        return t
    # 【顺序】必须先按词判断，而且**先查错误词** —— "不正确" 里含 "正确"，
    # 先查正确词会把 "不正确" 判成 true。也不能先走选项字母映射：
    # 选项 A 是 "正确" 时，"不正确" 会被宽松匹配到 A，同样判反（自测抓到的）。
    for w in ("错误", "不正确", "不对", "错", "×", "✗", "否", "false", "f"):
        if w in t:
            return "false"
    for w in ("正确", "对", "√", "✓", "是", "true", "t"):
        if w in t:
            return "true"
    # 词判断不行，再按选项字母（A/T/1 -> true，其余 -> false）
    keys = to_option_keys(answer, options)
    if keys:
        return "true" if keys[0].upper() in ("A", "T", "1") else "false"
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
        # 整卷模式需要的东西（从开考重定向和整卷页里取）
        self.openc = ""
        self.paper_id = ""
        self.exam_create_user_id = ""
        self.statistics = {}   # 占位，避免旧代码引用
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
        }, headers=exam_headers(), timeout=25, allow_redirects=False)
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
        }, headers=exam_headers(), timeout=25, allow_redirects=False)

        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            tip = soup.select_one("p.blankTips,li.msg")
            raise ExamAborted("开考被拒：{}".format(tip.get_text(strip=True) if tip else r.text[:120]))
        if r.status_code not in (301, 302, 303, 307, 308):
            raise ExamAborted("开考返回意外状态 HTTP {}".format(r.status_code))

        loc = r.headers.get("Location", "")
        logger.info("开考响应: HTTP {} -> {}".format(r.status_code, loc[:160]))
        m = re.search(r"[?&]enc=([^&]+)", loc)
        if not m:
            raise ExamAborted("开考重定向里没有 enc 参数: {}".format(loc[:160]))
        self.enc = m.group(1)
        # 把重定向 URL 的全部查询参数留下来 —— 整卷模式要用其中的 openc
        try:
            from urllib.parse import urlparse, parse_qsl
            self.start_query = dict(parse_qsl(urlparse(loc).query))
            self.openc = self.start_query.get("openc", "") or self.openc
            logger.debug("开考重定向参数: {}".format(
                {k: v for k, v in self.start_query.items() if k != "enc"}))
        except Exception:  # noqa: BLE001
            self.start_query = {}
        first = self.fetch(0)   # 第一题会把 enc/剩余时间刷新出来

        # 【保险】真开考的考试一定带计时。如果取完第一题计时参数还是 0，
        # 说明这场考试其实没被真正开始（取题接口不开考也能渲染题目），
        # 这时候再提交只会被服务端秒拒「无效操作」，纯属白跑还刷一堆错误日志。
        if not self.remain_time and not self.enc_remain_time:
            raise ExamInProgressError(
                "开考后服务端没有返回考试计时（remainTime/encRemainTime 都是 0），"
                "说明考试没有真正开始。已停止，避免继续发无效提交。")
        logger.info("考试计时: 剩余 {} 秒（encRemainTime={}）, encLastUpdateTime={}".format(
            self.remain_time, self.enc_remain_time, self.last_update_time))

        # 【优先走整卷模式】实测目标考试的客户端就是整卷模式（preview-save + paperId），
        # 用手机单题模式的 reVersionSubmitTestNew 打它会一律回「无效操作」。
        try:
            questions = self.open_preview()
        except Exception as e:  # noqa: BLE001
            logger.warning("整卷模式拉取失败（{}: {}），回退单题模式".format(type(e).__name__, e))
            questions = []
        if questions and self.paper_id:
            return self._run_preview(questions)
        if questions:
            logger.warning("整卷模式没解析出 paperId —— 若接下来保存仍报「无效操作」，"
                           "就是缺这个字段，请把日志发我")
        else:
            logger.info("改用单题模式作答")
        return first

    # ---------------- 4. 取题 ----------------
    def fetch(self, index: int) -> ExamQuestion:
        r = self.session.get(EXAM_FETCH, params={
            "courseId": self.course_id, "classId": self.class_id, "tId": self.exam_id,
            "id": self.exam_answer_id, "source": 0, "p": 1, "isphone": "true",
            "tag": int(self.enc_remain_time == 0), "cpi": self.cpi, "imei": get_imei(),
            "start": index, "enc": self.enc, "keyboardDisplayRequiresUserAction": 1,
            "monitorStatus": 0, "monitorOp": -1, "remainTimeParam": self.enc_remain_time,
            "relationAnswerLastUpdateTime": self.last_update_time,
        }, headers=exam_headers(), timeout=25)
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
        # 【诊断】第一次取题时把表单里的字段名全打出来 —— 这些字段名（enc / encRemainTime /
        # remainTime / encLastUpdateTime / testUserRelationId…）如果和预期不一致，
        # 计时参数就会一直是 0，提交必然被判「无效操作」。
        if index == 0:
            fields = []
            for inp in form.select("input,textarea"):
                nm = inp.get("id") or inp.get("name") or "?"
                fields.append("{}={}".format(nm, str(inp.get("value") or "")[:24]))
            logger.debug("考试表单字段({} 个): {}".format(len(fields), " | ".join(fields[:40])))
        for key, attr in (("enc", "enc"), ("enc_remain_time", "encRemainTime"),
                          ("remain_time", "remainTime"), ("last_update_time", "encLastUpdateTime")):
            node = form.select_one("input#{}".format(attr))
            if node is not None:
                try:
                    setattr(self, key, int(node["value"]) if key != "enc" else node["value"])
                except (TypeError, ValueError):
                    pass
        # 开考后真正生效的考试会话 id 以取题页为准（封面页拿到的可能不是最终值）
        rid = form.select_one("input#testUserRelationId")
        if rid is not None and (rid.get("value") or "").strip():
            if self.exam_answer_id and rid["value"].strip() != str(self.exam_answer_id):
                logger.debug("考试会话 id 更新: {} -> {}".format(
                    self.exam_answer_id, rid["value"].strip()))
            self.exam_answer_id = rid["value"].strip()
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
        }, headers=exam_headers(), timeout=25)
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
        r = self.session.post(EXAM_SUBMIT, params=params, data=data,
                              headers=exam_headers(), timeout=25)
        r.raise_for_status()
        try:
            js = r.json()
        except Exception:  # noqa: BLE001
            raise RuntimeError("提交返回的不是 JSON（HTTP {}）：{}".format(
                r.status_code, r.text[:200]))
        if js.get("status") != "success":
            # 【诊断】把请求和服务端原话完整落盘，方便和浏览器里抓到的真实请求逐字段对比。
            # 文件：程序运行目录下的 exam_submit_debug.json
            try:
                import os as _os
                with open("exam_submit_debug.json", "w", encoding="utf8") as _fp:
                    json.dump({
                        "url": EXAM_SUBMIT,
                        "headers": exam_headers(),
                        "params": {k: str(v) for k, v in params.items()},
                        "data": {k: str(v) for k, v in data.items()},
                        "response": js,
                        "my_state": {
                            "exam_id": self.exam_id, "exam_answer_id": self.exam_answer_id,
                            "class_id": self.class_id, "course_id": self.course_id,
                            "cpi": self.cpi, "uid": self._uid(), "index": index, "qid": qid,
                            "remain_time": self.remain_time,
                            "enc_remain_time": self.enc_remain_time,
                            "last_update_time": self.last_update_time,
                            "enc": self.enc,
                        },
                    }, _fp, ensure_ascii=False, indent=2)
                logger.error("已把本次提交请求写进 exam_submit_debug.json（发给我就能对比）")
            except Exception as _e:  # noqa: BLE001
                logger.debug("写诊断文件失败 -> {}".format(_e))
            logger.debug("提交失败响应: {}".format(str(js)[:400]))
            raise RuntimeError("提交失败：{}（第 {} 题 qid={}；服务端计时 remainTime={} "
                               "encRemainTime={}）".format(
                                   js.get("msg"), index, qid,
                                   self.remain_time, self.enc_remain_time))
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
        """
        把题库答案写回 q.old_answer（交给 _answer_form 组装）；成功返回 True。

        失败时把原因打清楚 —— 是「三个来源都没答出来」还是「答了但对不上选项」，
        这两种情况看起来都像"没调用 AI"，日志必须能区分开。
        """
        ans = self.ask(q)
        if not ans:
            logger.warning("  题库链三个来源（网课小工具 / ANEVOL / AI）都没给出答案")
            return False
        if q.type == QT_SINGLE:
            keys = to_option_keys(ans, q.options)
            if not keys:
                logger.warning("  题库答了 {!r}，但映射不到选项上，跳过".format(str(ans)[:50]))
                return False
            q.old_answer = keys[0]
        elif q.type == QT_MULTI:
            keys = to_option_keys(ans, q.options)
            if not keys:
                logger.warning("  题库答了 {!r}，但映射不到选项上，跳过".format(str(ans)[:50]))
                return False
            q.old_answer = "".join(sorted(set(keys)))
        elif q.type == QT_JUDGE:
            v = judgement_value(ans, q.options)
            if not v:
                logger.warning("  题库答了 {!r}，但判断不出对错，跳过".format(str(ans)[:50]))
                return False
            q.old_answer = v
        else:
            q.old_answer = str(ans).strip()
        return True

    # ---------------- 8. 主流程 ----------------
    # ---------------- 整卷模式（mooc2 网页版）----------------
    def preview_url(self) -> str:
        """整卷页 URL（也用来当提交时的 Referer）。"""
        from urllib.parse import urlencode
        return EXAM_MOOC2_PREVIEW + "?" + urlencode({
            "courseId": self.course_id, "classId": self.class_id, "start": 0, "cpi": self.cpi,
            "examRelationId": self.exam_id, "examRelationAnswerId": self.exam_answer_id,
            "newMooc": "true", "openc": self.openc, "monitorStatus": 0, "monitorOp": -1,
            "remainTimeParam": self.enc_remain_time,
            "relationAnswerLastUpdateTime": self.last_update_time, "enc": self.enc,
        })

    def open_preview(self) -> List[ExamQuestion]:
        """拉整卷页面，解析 paperId / examCreateUserId / 全部题目。"""
        r = self.session.get(self.preview_url(), headers=web_headers(self.preview_url()), timeout=25)
        soup = BeautifulSoup(r.text, "lxml")

        def pick(*names):
            for nm in names:
                for sel in ("input#{}".format(nm), "input[name='{}']".format(nm),
                            "input[name=\"{}\"]".format(nm)):
                    node = soup.select_one(sel)
                    if node is not None and (node.get("value") or "").strip():
                        return node["value"].strip()
            # 实在找不到就从整页 HTML 里正则捞（页面里常写在 JS 变量里）
            for nm in names:
                m = re.search(nm + r"['\"]?\s*[:=]\s*['\"]?(\d+)", r.text)
                if m:
                    return m.group(1)
            return ""

        self.paper_id = pick("paperId", "paperid", "testPaperId2")
        self.exam_create_user_id = pick("examCreateUserId", "createUserId", "examCreateUserid")
        rid = pick("testUserRelationId", "examRelationAnswerId")
        if rid:
            self.exam_answer_id = rid
        for key, attr in (("enc", "enc"), ("enc_remain_time", "encRemainTime"),
                          ("remain_time", "remainTime"), ("last_update_time", "encLastUpdateTime")):
            node = soup.select_one("input#{}".format(attr))
            if node is not None:
                try:
                    setattr(self, key, int(node["value"]) if key != "enc" else node["value"])
                except (TypeError, ValueError):
                    pass
        nodes = soup.select("div.questionWrap.singleQuesId.ans-cc-exam")
        if not nodes:
            nodes = soup.select("div.ans-cc-exam")
        questions = [parse_question(n, i) for i, n in enumerate(nodes)]
        logger.info("整卷模式：解析到 {} 题；paperId={} examCreateUserId={} openc={}".format(
            len(questions), self.paper_id or "(没找到)", self.exam_create_user_id or "(没找到)",
            self.openc or "(空)"))
        return questions

    def save_preview(self, index: int, q: Optional[ExamQuestion], final: bool = False) -> dict:
        qid = q.id if q else 0
        params = {
            "classId": self.class_id, "courseId": self.course_id, "cpi": self.cpi,
            "testPaperId": self.exam_id, "testUserRelationId": self.exam_answer_id,
            "tempSave": "false" if final else "true",
            **get_exam_signature(self._uid(), qid, random.randint(100, 1000), random.randint(100, 1000)),
            "qid": qid, "version": 1, "view": "json", "_csign": 0,
        }
        for k in _PREVIEW_SIGN_PLACEHOLDERS:
            params[k] = "undefined"
        data = {
            "answerMode": 1, "courseId": self.course_id, "paperId": self.paper_id,
            "testPaperId": self.exam_id, "examCreateUserId": self.exam_create_user_id,
            "feedbackEnc": "", "testUserRelationId": self.exam_answer_id,
            "classId": self.class_id, "type": 0, "remainTime": self.remain_time,
            "tempSave": "false" if final else "true", "timeOver": "false",
            "encRemainTime": self.enc_remain_time, "encLastUpdateTime": self.last_update_time,
            "enc": self.enc, "userId": self._uid(), "cpi": self.cpi,
            "examRelationId": self.exam_id, "enterPageTime": self.last_update_time,
            "exitdtime": 0, "monitorforcesubmit": 0,
        }
        if q is not None:
            data.update(self._answer_form(q))
            data["start"] = index
        r = self.session.post(EXAM_PREVIEW_SAVE, params=params, data=data,
                              headers=web_headers(self.preview_url()), timeout=25)
        r.raise_for_status()
        try:
            js = r.json()
        except Exception:  # noqa: BLE001
            raise RuntimeError("整卷保存返回非 JSON：{}".format(r.text[:200]))
        if str(js.get("status", "")).lower() != "success" and js.get("status") is not True:
            raise RuntimeError("整卷保存失败：{}".format(str(js)[:200]))
        return js

    def _run_preview(self, questions: List[ExamQuestion]) -> dict:
        """整卷模式下的作答主循环（客户端就是这么存的）。"""
        answered = skipped = failed = 0
        for i, q in enumerate(questions):
            if self.overwrite is False and (q.old_answer or "").strip():
                logger.info("第 {} 题已有答案（{}），跳过不覆盖".format(i, q.old_answer))
                skipped += 1
                continue
            logger.info("第 {} 题 [{}] {}".format(i, q.type_name, q.title[:60]))
            if not self.fill(q):
                failed += 1
                continue
            try:
                self.save_preview(i, q, final=False)
                answered += 1
                logger.info("  已作答并保存 -> {}".format(q.old_answer))
            except Exception as e:  # noqa: BLE001
                failed += 1
                logger.error("  第 {} 题保存失败 -> {}: {}".format(i, type(e).__name__, e))
                if "无效操作" in str(e):
                    logger.error("服务端仍回「无效操作」，已停止（请把日志发我）")
                    break
        self.stats.update(total=len(questions), answered=answered, skipped=skipped, failed=failed)
        cover = (answered + skipped) / len(questions) if questions else 0.0
        logger.info("整卷模式作答：保存 {}，跳过 {}，未答 {}；覆盖率 {:.0%}".format(
            answered, skipped, failed, cover))
        if not self.auto_submit:
            logger.warning("【未交卷】答案已逐题保存。请到手机/网页核对后自己点交卷。")
            return self.stats
        if cover < self.min_cover:
            logger.error("覆盖率 {:.0%} 低于门槛 {:.0%}，不自动交卷。".format(cover, self.min_cover))
            return self.stats
        try:
            self.save_preview(0, None, final=True)
            logger.warning("【已自动交卷】{}".format(self.title))
        except Exception as e:  # noqa: BLE001
            logger.error("自动交卷失败 -> {}（请手动交卷）".format(e))
        return self.stats

    # ---------------- 单题模式回退 ----------------
    def run(self) -> dict:
        logger.info("=" * 90)
        logger.info("考试作答：{}（{}）".format(self.title, self.exam_id))
        logger.info("模式：{}".format("自动答题 + 自动交卷" if self.auto_submit
                                      else "自动答题但不交卷（答完你自己核对交卷）"))
        logger.info("=" * 90)

        self.load_cover()
        self.solve_captcha()
        # 考试按时间走，进考场前把题库链的慢间隔压下去（每题能省好几秒）
        tune_tiku_for_exam(self.tiku)
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
                if "无效操作" in str(e):
                    # 实测确认：考试确实开考了（服务端计时正常），是提交请求本身被拒。
                    # 继续把每一题都发一遍只是刷错误日志，立刻停手并留下诊断文件。
                    logger.error("=" * 90)
                    logger.error("服务端对所有提交都回「无效操作」——考试是开着的（计时正常），")
                    logger.error("说明是提交请求的字段和真实客户端对不上。已停止提交。")
                    logger.error("请把程序目录下的 exam_submit_debug.json 发给我，")
                    logger.error("我会和浏览器 F12 抓到的真实请求逐字段比对后修正。")
                    logger.error("=" * 90)
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
