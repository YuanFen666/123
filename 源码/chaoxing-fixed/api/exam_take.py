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

# 滑块验证码自动识别的最多尝试次数。
# 【为什么是 8】chaoxing 的背景图里有**干扰项**（真实缺口和干扰项形状一样、
# 只有明暗/清晰度不同），纯算法匹配会偶尔跑偏；而**同一张验证码只能校验一次**
# （实测第二次就返回 error:1 verification error），所以每失败一次必须换新图。
# 单次成功率哪怕只有 0.4，8 次也有 98% 以上。
# 全部失败后还有人工兜底（见 SlideCaptcha._manual_fallback）。
# 【注意】必须在 SlideCaptcha 类**之前**定义 —— 它被用作 solve() 的默认参数值，
# 类定义时就会求值，放后面会 NameError（这个坑刚踩过）。
CAPTCHA_MAX_TRY = 8
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
    考试是按时间走的，题库链那套「防限流」的慢间隔在这里是浪费；
    但**超时不能一味收紧** —— 收紧超时会逼着题库提前认输，
    把题丢给准确率更低的来源。

    两轮实测（详见 config.ini 的[tiku]注释）：
      速度：  ANEVOL 20~42 秒/题      AI 0.5~2 秒/题
      准确率：中国商贸文化 85 题（拿批改页每题得分对日志来源）
              ANEVOL 27 题 100%     AI 53 题 70%
              全部 16 道失分题都是 AI 答的。

    考试预算其实很宽（100 分钟 / 85 题 = 每题 70 秒），所以这里给题库**留足时间**：
        ANEVOL 50 秒（实测最长 42 秒 + 余量）      AI 20 秒
    只有「两次请求之间的间隔」继续压小 —— 它不影响答案质量，只影响等待。
    """
    if tiku is None:
        return
    targets = list(getattr(tiku, "providers", []) or [])
    if not targets and not hasattr(tiku, "providers"):
        targets = [tiku]
    for p in targets:
        name = type(p).__name__
        if name == "TikuAnevol":
            p.min_interval = 0.2
            try:
                # ANEVOL 的读超时是模块级常量（请求处直接读），只能改全局。
                # 给到 50 秒：实测 20~42 秒，之前压到 6 秒会让它白超时、把题推给 AI。
                import api.answer as _ans
                _ans._ANEVOL_READ_TIMEOUT = 50
            except Exception:  # noqa: BLE001
                pass
        elif name == "AI":
            p.min_interval_seconds = 0.3
            p.read_timeout = 20
        elif name == "TikuIcodef":
            # 已不在链路里；留着兼容，真加回来时给它短超时（命中率只有 3.5%）
            p.min_interval = 0.3
            p.read_timeout = 4
        logger.debug("考试模式：已调整 {} 的间隔与超时".format(name))


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
        self._last_bg_width = 0     # 最近一张背景图的宽度（人工兜底时提示用）

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
        try:
            import io
            from PIL import Image
            self._last_bg_width = Image.open(io.BytesIO(shade)).size[0]
        except Exception:  # noqa: BLE001
            self._last_bg_width = 0
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

    def _save_fail_image(self, shade: bytes, tag: str = "") -> str:
        """
        验证码失败时把背景图存下来 —— 失败原因里"匹配跑偏"占大头，
        留着图才能复盘；也是人工兜底时要给人看的那张图。
        """
        try:
            import io
            import os
            from PIL import Image
            d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
            d = os.path.abspath(d)
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "captcha_fail_{}{}.png".format(
                time.strftime("%Y%m%d_%H%M%S"), tag))
            Image.open(io.BytesIO(shade)).save(p)
            return p
        except Exception:  # noqa: BLE001
            return ""

    def _manual_fallback(self, last_error, max_try: int) -> str:
        """
        自动识别全部失败后的人工兜底。

        【为什么值得做】chaoxing 的背景图里有**干扰项**：真实缺口和干扰项形状一样、
        只有明暗/清晰度不同。纯算法匹配会偶尔跑偏，而**同一张验证码只能校验一次**
        （实测第二次就返回 error:1 verification error），所以只能换新图重试。
        重试若干次仍不过时，与其直接报错退出，不如把图交给用户看一眼 ——
        人眼认这个缺口几乎不会错。

        交互式终端里：取一张新图 → 存盘 → 提示路径 → 让用户输入 x → 校验。
        非交互（无人值守）：只存图 + 明确报错，绝不静默卡在等输入上。
        """
        import sys
        shade, _cutout = None, None
        try:
            shade, _cutout = self._get_images()
        except Exception:  # noqa: BLE001
            pass
        path = self._save_fail_image(shade, "_manual") if shade else ""
        tip = ("滑块验证码自动识别 {} 次都没过（最后一次：{}）".format(max_try, last_error))
        if path:
            tip += "\n     失败时的背景图已存到：{}".format(path)
        if not (getattr(sys.stdin, "isatty", lambda: False)() and getattr(sys.stdout, "isatty", lambda: False)()):
            raise RuntimeError(tip + "\n     （非交互环境，无法人工兜底；重跑一次通常能碰上好认的图）")
        if not shade:
            raise RuntimeError(tip + "\n     （也没取到新验证码图，无法人工兜底）")
        logger.warning(tip)
        logger.warning("     现在换成人工识别：请打开上面那张图，看缺口离左边多少像素。")
        logger.warning("     图片宽度 {} 像素，输入 0~{} 之间的整数即可（直接回车=放弃）。".format(
            self._last_bg_width or 320, max(0, (self._last_bg_width or 320) - 1)))
        for _ in range(3):
            try:
                raw = input("     请输入缺口 x 坐标: ").strip()
            except (EOFError, KeyboardInterrupt):
                raise RuntimeError("人工兜底被中断")
            if not raw:
                raise RuntimeError("人工兜底放弃（回车）")
            try:
                x = int(raw)
            except ValueError:
                logger.warning("     请输入整数。")
                continue
            try:
                v = self._check(x)
                logger.info("人工输入的 x={} 通过了验证码".format(x))
                return v
            except Exception as e:  # noqa: BLE001
                logger.warning("     x={} 没通过（{}）；本张图已作废，再取一张重来。".format(x, e))
                try:
                    shade, _cutout = self._get_images()
                    path = self._save_fail_image(shade, "_manual2")
                    if path:
                        logger.warning("     新图已存到：{}".format(path))
                except Exception:  # noqa: BLE001
                    pass
        raise RuntimeError("人工兜底也失败了")

    def solve(self, max_try: int = CAPTCHA_MAX_TRY) -> str:
        self._get_server_time()
        last = None
        shade = None
        for i in range(max_try):
            try:
                shade, cutout = self._get_images()
                x = self._match(shade, cutout)
                logger.info("滑块验证码：第 {} 次尝试，识别缺口 x={}".format(i + 1, x))
                return self._check(x)
            except Exception as e:  # noqa: BLE001
                last = e
                logger.warning("滑块验证码第 {} 次未通过 -> {}".format(i + 1, e))
                if shade:
                    self._save_fail_image(shade, "_try{}".format(i + 1))
                time.sleep(1.0)
        return self._manual_fallback(last, max_try)


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


# ============================================================
#  考试门槛：等「服务端统计」追上来
# ============================================================
# 【为什么需要这个】实测（2026-09-15，账号 #3）：
#   刷完一门课的全部任务点后，进度页**立刻**显示 100%，
#   但紧接着进考场却被拒：「该考试教师已设置章节任务点未完成90%，不能参加考试」。
#   过一段时间再做「就绪体检」，同一场考试变成 √ 可以考试。
#   → 进度页和考试门槛读的**不是同一个值**：进度页实时，门槛用的是服务端的
#     任务点聚合缓存，汇总有延迟（几分钟到几十分钟）。
#
# 【所以做法】门槛被拒时不要直接放弃，而是**隔一会儿重新探测门槛本身**
#   （用门槛做探测比等固定时间准 —— 因为它才是权威判断），追上了就自动开考。
#
# 【必须区分】「任务点没到」是可恢复的；「已交卷 / 已过期 / 要人脸」是永久性的，
#   等多久都没用，绝不能傻等（会白白占掉几十分钟）。

# 可恢复：等一等服务端统计就会好
_GATE_TRANSIENT_WORDS = (
    "任务点", "章节", "完成度", "未完成", "未达标", "不足", "未满足",
)
# 永久性：怎么等都不会变（**优先级高于上面**）
_GATE_PERMANENT_WORDS = (
    "已交卷", "已交过卷", "已过期", "已结束", "已批阅", "待批阅",
    "人脸", "已经完成", "已完成", "不允许", "没有权限", "不存在",
)


def is_transient_gate_reason(reason: str) -> bool:
    """
    这个「不能考」的原因，是不是「等一等就会好」的那类？

    只认「任务点/章节完成度没到」——那是服务端统计延迟造成的，可恢复。
    已交卷 / 已过期 / 要人脸 等一律返回 False，避免无谓的长等待。
    """
    r = _remove_escape(reason or "")
    if not r:
        return False
    for w in _GATE_PERMANENT_WORDS:
        if w in r:
            return False
    for w in _GATE_TRANSIENT_WORDS:
        if w in r:
            return True
    return False


def wait_for_gate(probe, exam, max_wait: float, poll: float, log=None):
    """
    反复探测考试门槛，直到可以考或超出预算。

    probe : 无参可调用，返回带 .can_start / .reason 的对象（就是 ExamBoard.probe 的偏函数）
    exam  : 用于日志的考试对象（取 .name）
    max_wait : 最长等待秒数；<=0 表示不等待（立刻返回最后一次结果）
    poll  : 每次探测的间隔秒数
    返回最后一次的 probe() 结果；一次都没探到就返回 None。

    **任何异常都吃掉**：这只是"多等一会儿"，绝不能因为它把整个流程搞挂。
    """
    import time as _time

    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:  # noqa: BLE001
                pass

    name = getattr(exam, "name", "?")
    last = None
    if max_wait <= 0 or poll <= 0:
        try:
            return probe()
        except Exception:  # noqa: BLE001
            return None

    _time_start = _time.time()
    attempt = 0
    while True:
        attempt += 1
        try:
            last = probe()
        except Exception as ex:  # noqa: BLE001
            _log("《{}》第 {} 次探测门槛失败（忽略，继续等）：{}: {}".format(
                name, attempt, type(ex).__name__, ex))
            last = None
        if last is not None and getattr(last, "can_start", False):
            waited = _time.time() - _time_start
            _log("《{}》门槛已通过（等了 {:.0f} 秒，第 {} 次探测）—— 服务端统计追上来了，继续开考".format(
                name, waited, attempt))
            return last
        elapsed = _time.time() - _time_start
        if elapsed + poll > max_wait:
            _log("《{}》等待预算用尽（已等 {:.0f} 秒 / 上限 {:.0f} 秒），本次放弃这场考试".format(
                name, elapsed, max_wait))
            return last
        reason = getattr(last, "reason", "") if last is not None else ""
        _log("《{}》还不能考（{}）；已等 {:.0f} 秒，{} 秒后再探测一次（上限 {:.0f} 秒）".format(
            name, reason or "未通过", elapsed, int(poll), max_wait))
        _time.sleep(poll)


def parse_preview_question(node, index: int = 0) -> ExamQuestion:
    """
    解析「整卷预览」页(标题=整卷预览)的题目。

    结构来自真实页面抓取（2026-09-12 用户导出的考试页 HTML）。它和我们最早
    照参考实现写的**单题页结构完全不是一套** —— 这才是当初"解析到 0 题"的真因，
    跟 openc 毫无关系。

        <div id="sigleQuestionDiv_890718804" class="questionLi ..." data="890718804">
          <h3 class="mark_name colorDeep">1. <span>(单选题, 1.0 分)</span>
              <div>题干文字</div></h3>
          <form>
            <input name="type890718804"  value="0">
            <input name="questionId"     value="890718804">
            <input name="typeName890718804" value="单选题">
            <input name="start"          value="0">
            <input id="answer890718804"  value="">
            <div class="stem_answer">
              <div class="answerBg" onclick="saveSingleSelect(this,'890718804')">
                <span data="B" qid="890718804" class="saveSingleSelect ... num_option">A</span>
                <div class="answer_p">自然条件</div>
              </div>
              ...

    【最关键的一点】选项是乱序的（页面里 randomOptions=true）：
        span 的 **data** 才是原始选项键 —— 提交必须用它；
        显示出来的字母（num_option 的 A/B/C/D）只是乱序后的展示位置。
    页面 JS addChoice() 里就是这么做的：
        choiceContent = choiceContent + $(this).attr("data");   // 拼 data，不是显示字母
    取错一个字母，整份答案就全错，所以这里只认 data。
    """
    qid_in = node.select_one("input[name='questionId']")
    qid = 0
    if qid_in is not None and (qid_in.get("value") or "").strip():
        try:
            qid = int(qid_in["value"].strip())
        except ValueError:
            qid = 0
    if not qid:
        try:
            qid = int((node.get("data") or "0").strip())
        except ValueError:
            qid = 0

    type_in = node.select_one("input[name^='type']")
    qtype = QT_SINGLE
    if type_in is not None:
        raw = (type_in.get("value") or "").strip()
        if raw.lstrip("-").isdigit():
            qtype = int(raw)

    title = ""
    h3 = node.select_one("h3.mark_name")
    if h3 is not None:
        parts = []
        for tag in h3.children:
            if getattr(tag, "name", None) == "span":   # 跳过「(单选题, 1.0 分)」
                continue
            parts.append(tag.get_text() if hasattr(tag, "get_text") else str(tag))
        title = re.sub(r"^\s*\d+\s*[.、]\s*", "", "".join(parts))
    title = _remove_escape(title)

    q = ExamQuestion(id=qid, type=qtype, title=title, index=index)

    ans_in = node.select_one("input[id^='answer']")
    q.old_answer = (ans_in.get("value") if ans_in is not None else "") or ""

    if qtype in (QT_SINGLE, QT_MULTI, QT_JUDGE):
        for opt in node.select("div.answerBg"):
            sp = opt.select_one("span[data]")
            if sp is None:
                continue
            key = (sp.get("data") or "").strip()
            if not key:
                continue
            tx = opt.select_one("div.answer_p") or opt
            q.options[key] = _remove_escape(tx.get_text())
    else:
        for blank in node.select("div.completionList.objectAuswerList"):
            span = blank.select_one("span.grayTit")
            q.blanks.append(_remove_escape(span.get_text() if span else ""))
    return q


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
                 max_questions: int = 200, openc: str = ""):
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
        # openc 优先用配置里给的：它从开考重定向里不一定拿得到，
        # 但就明明白白写在考试页 URL 里（?openc=xxxxxxxx），用户复制一下即可。
        self.openc = (openc or "").strip()
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
        # 【诊断】把整卷页原样落盘 + 逐个数候选节点。
        # 之前只看到"解析到 0 题"就下结论说缺 openc，其实没有证据 ——
        # 得先看清服务端到底返回了什么（拒绝页？外壳页？还是结构不同）。
        try:
            with open("exam_preview_debug.html", "w", encoding="utf8") as _fp:
                _fp.write(r.text)
        except Exception as _e:  # noqa: BLE001
            logger.debug("落盘整卷页失败 -> {}".format(_e))
        _title = soup.find("title")
        logger.info("整卷页 HTTP {}  {} 字节  title={!r}  已存 exam_preview_debug.html".format(
            r.status_code, len(r.text), (_title.get_text(strip=True) if _title else "")[:40]))
        for _sel in ("div.questionWrap.singleQuesId.ans-cc-exam", "div.ans-cc-exam",
                     "div.questionWrap", "div.allAnswerList", "form#submitTest",
                     "input#paperId", "input#examCreateUserId"):
            logger.info("    候选节点 {:<42} -> {} 个".format(_sel, len(soup.select(_sel))))
        _blank = soup.select_one("p.blankTips,li.msg,h2")
        if _blank:
            logger.info("    页面提示: {}".format(_blank.get_text(strip=True)[:80]))

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
        # openc 就在整卷页里（<input type="hidden" id="openc">）—— 不用你手工粘。
        # 页面能加载说明会话有效；openc 只是给后续请求（保存）带的凭证。
        oc = soup.select_one("input#openc")
        if oc is not None and (oc.get("value") or "").strip():
            self.openc = oc["value"].strip()

        # 【真实选择器】整卷页用 div.questionLi / div[id^=sigleQuestionDiv_]，
        # 不是单题页的 questionWrap/ans-cc-exam —— 之前就是这里错了才解析到 0 题。
        nodes = soup.select("div.questionLi")
        if not nodes:
            nodes = soup.select("div[id^='sigleQuestionDiv_']")
        if not nodes:
            nodes = soup.select("div.questionWrap.singleQuesId.ans-cc-exam")
        questions = [parse_preview_question(n, i) for i, n in enumerate(nodes)]
        logger.info("整卷模式：解析到 {} 题；paperId={} examCreateUserId={} openc={}".format(
            len(questions), self.paper_id or "(没找到)", self.exam_create_user_id or "(没找到)",
            (self.openc[:12] + "…") if self.openc else "(空)"))
        if questions and questions[0].options:
            _q0 = questions[0]
            logger.info("  第 1 题抽样：[{}] {} / 选项(键=原始键, 乱序后展示) {}".format(
                _q0.type_name, _q0.title[:40],
                {k: v[:12] for k, v in list(_q0.options.items())[:4]}))
        if not questions:
            logger.warning("整卷页没解析到题目 —— 页面已存成 exam_preview_debug.html，"
                           "请把它发给我（结构可能又变了）")
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

    # ---------------- 答案清单（保存接口不可用时的兜底产物）----------------
    def _dump_answers(self, collected: List[tuple]) -> None:
        """
        把算出来的答案写成 exam_answers.md：
           题号 | 题型 | 题干 | 选项 | 答案
        保存接口的模式/字段一时对不上时，你可以照这份清单在浏览器里快速填。
        """
        if not collected:
            return
        lines = ["# 考试答案清单：{}".format(self.title), "",
                 "> 由程序用题库链算出，**仅供人工核对/手动填写**。",
                 "> 生成时间：{}".format(time.strftime("%Y-%m-%d %H:%M:%S")), ""]
        for idx, q, ans, ok in collected:
            lines.append("## 第 {} 题（{}）{}".format(idx + 1, q.type_name, q.title))
            if q.options:
                for k, v in q.options.items():
                    mark = " ✅" if ok and k in str(ans).upper() else ""
                    lines.append("- {} {}{}".format(k, v, mark))
            lines.append("")
            lines.append("**答案：{}**{}".format(ans or "(没答出来)",
                                                "" if ok else "  ← 需要你自己判断"))
            lines.append("")
        path = "exam_answers.md"
        with open(path, "w", encoding="utf8") as fp:
            fp.write("\n".join(lines))
        logger.warning("已把 {} 题的答案清单写成 {}（可照着在浏览器里填）".format(len(collected), path))

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

        # 【关键】start() 只在**整卷模式**下返回非 None —— 那种模式下它已经把整张
        # 卷子答完并（在 auto_submit 时）交卷了。如果这里不接返回值继续往下走，
        # 程序会退回「单题模式」再答一遍，然后被服务端一句
        # 「提交失败：考试已经提交」顶回来，并写出 exam_answers.md /
        # exam_submit_debug.json 一堆无用文件 —— 实测踩到过（2026-09-15），
        # 用户看到的就是「明明交卷成功了，后面却弹出一大段报错」。
        preview_result = self.start()
        if preview_result is not None:
            logger.info("=" * 90)
            logger.info("整卷模式已完成本次考试{}，不再进入单题模式。".format(
                "并自动交卷" if self.auto_submit else "（未交卷，请你自己核对后提交）"))
            logger.info("=" * 90)
            return preview_result

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
        collected = []          # (题号, 题目, 算出的答案, 是否映射成功) —— 最后写成答案清单
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
                collected.append((index, q, "", False))
                failed += 1
                index += 1
                continue
            collected.append((index, q, q.old_answer, True))
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
        # 【有用产物】不管保存成没成，把「算出来的答案」落一份清单。
        # 保存接口的字段/模式一时对不上时，你可以照着这份清单在浏览器里快速填。
        try:
            self._dump_answers(collected)
        except Exception as _e:  # noqa: BLE001
            logger.debug("写答案清单失败 -> {}".format(_e))
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
