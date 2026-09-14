# -*- coding: utf-8 -*-
"""
课程进度 / 分数
================
给启动脚本的选课界面用：显示每门课的完成度和当前分数。

取数链路（2026-09-14 实测确定性，三段缺一不可）
------------------------------------------------
    ① GET /visit/stucoursemiddle?courseid=&clazzid=&vc=1&cpi=      ← 不跟随跳转
         302 的 Location 里带 **enc**；enc 按课程固定、不随会话变
    ② GET {host}/mooc-ans/mycourse/studentcourse?courseId=&clazzid=&cpi=&enc=&fromMiddle=1&vc=1
         返回 ~120KB 的课程页，里面有导航链接，能抠出 **openc**
    ③ GET {host}/mooc-ans/studyprogress?courseId=&classId=&ut=s&enc=&cpi=&openc=
         200，title='学习进度'，里面有「我的进度 N%」和「当前分数」（如果有）

【两个坑，都是实测踩出来的】
  · **host 必须跟 302 给的保持一致**（一般是 `mooc1-2`）。用 `mooc1` 会被拒，
    只回 800 多字节的「温馨提示」。
  · **enc 和 openc 都要带**。只带 enc 同样被拒。

【分数的前提】有些课开了「自定义权重模式」，老平台上看不到分数 ——
页面里那个 `customModeLabel` 提示块会显示「请切换至新版平台查看成绩」。
这种情况我们拿不到分数，但**完成度照样能拿**。
"""
import re

from api.logger import logger

COURSE_MIDDLE = "https://mooc1-2.chaoxing.com/visit/stucoursemiddle"

_RE_ENC = re.compile(r"[?&]enc=([0-9a-fA-F]{20,})")
_RE_OPEN = re.compile(r"[?&]openc=([0-9a-fA-F]{20,})")
_RE_PERCENT = re.compile(r"我的进度\s*([0-9]+)\s*%")
# 「当前分数 （<span style="color:#f00;">92.03</span>）」
_RE_SCORE = re.compile(r"当前分数[^0-9]{0,120}<span[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*</span>")
# 三个分项：视频 / 章节测验 / 考试，是紧跟其后的 <td>
_RE_TDS = re.compile(r"<td[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*</td>")
# 自定义权重提示块。**不能假设 style 在 id 后面** —— 真实页面是
#   <div style="height:40px;display: none;" id="customModeLabel">
# style 在前！所以先匹配整个标签再取 style，跟顺序无关（自测抓到的 bug）。
_RE_CUSTOM_TAG = re.compile(r'<div[^>]*id="customModeLabel"[^>]*>')
_RE_STYLE_ATTR = re.compile(r'style="([^"]*)"')


def parse_progress_html(html: str) -> dict:
    """
    纯函数：从进度页 HTML 里解析出进度与分数。**不联网，可离线自测。**

    返回：
        {"percent": int|None,          # 我的进度 N%
         "score":   float|None,        # 当前分数
         "video":   float|None,        # 视频分项
         "quiz":    float|None,        # 章节测验分项
         "exam":    float|None,        # 考试分项
         "custom_weight": bool}        # 是否「自定义权重模式」（这种课拿不到分数）
    """
    html = html or ""
    out = {"percent": None, "score": None, "video": None,
           "quiz": None, "exam": None, "custom_weight": False}

    m = _RE_PERCENT.search(html)
    if m:
        try:
            out["percent"] = int(m.group(1))
        except ValueError:
            pass

    m = _RE_SCORE.search(html)
    if m:
        try:
            out["score"] = float(m.group(1))
        except ValueError:
            pass

    # 分项：只在「当前分数」那一行的表格里找，避免误抓别的数字
    if out["score"] is not None:
        tail = html[m.end(): m.end() + 800]
        tds = _RE_TDS.findall(tail)
        vals = []
        for t in tds[:3]:
            try:
                vals.append(float(t))
            except ValueError:
                pass
        if len(vals) >= 1:
            out["video"] = vals[0]
        if len(vals) >= 2:
            out["quiz"] = vals[1]
        if len(vals) >= 3:
            out["exam"] = vals[2]

    # 自定义权重模式：那块提示的 style 里没有 display:none 就是在显示。
    # 没有 style 属性时按「显示」算（div 默认就是可见的）。
    tag = _RE_CUSTOM_TAG.search(html)
    if tag:
        st = _RE_STYLE_ATTR.search(tag.group(0))
        if st is None or "none" not in st.group(1).replace(" ", "").lower():
            out["custom_weight"] = True
    return out


def _host_of(url: str, default: str = "https://mooc1-2.chaoxing.com") -> str:
    m = re.match(r"(https?://[^/]+)", url or "")
    return m.group(1) if m else default


def get_enc(session, course: dict) -> tuple:
    """
    ① 取 enc，并连带把 host 带回来。
    返回 (enc, host)；拿不到就是 (None, host)。
    """
    r = session.get(COURSE_MIDDLE, params={
        "courseid": course.get("courseId"), "clazzid": course.get("clazzId"),
        "vc": 1, "cpi": course.get("cpi"),
    }, timeout=25, allow_redirects=False)
    loc = r.headers.get("Location", "") or ""
    host = _host_of(loc)
    m = _RE_ENC.search(loc)
    if not m:
        # 有的情况会直接 200 返回（不再跳转），正文里也可能有
        m = _RE_ENC.search(r.text or "")
    return (m.group(1) if m else None), host


def get_openc(session, course: dict, enc: str, host: str) -> str:
    """② 从课程页里抠 openc（该页约 120KB，导航链接里带 enc+openc）。"""
    r = session.get(host + "/mooc-ans/mycourse/studentcourse", params={
        "courseId": course.get("courseId"), "clazzid": course.get("clazzId"),
        "cpi": course.get("cpi"), "enc": enc, "fromMiddle": 1, "vc": 1,
    }, timeout=25)
    body = r.text or ""
    m = _RE_OPEN.search(body)
    return m.group(1) if m else ""


def get_course_progress(session, course: dict) -> dict:
    """
    取一门课的进度与分数。**任何一步失败都返回带 error 的空结果，绝不抛异常**
    —— 它只服务于选课界面，不能因为它把整个刷课流程搞挂。
    """
    empty = {"percent": None, "score": None, "video": None, "quiz": None,
             "exam": None, "custom_weight": False, "error": None}
    try:
        enc, host = get_enc(session, course)
        if not enc:
            empty["error"] = "拿不到 enc"
            return empty
        openc = get_openc(session, course, enc, host)
        params = {
            "courseId": course.get("courseId"), "classId": course.get("clazzId"),
            "ut": "s", "enc": enc, "cpi": course.get("cpi"),
        }
        if openc:
            params["openc"] = openc
        r = session.get(host + "/mooc-ans/studyprogress", params=params, timeout=25)
        body = r.text or ""
        if "学习进度" not in body and "我的进度" not in body:
            empty["error"] = "进度页没返回内容（HTTP {}，{} 字节）".format(
                r.status_code, len(body))
            return empty
        res = parse_progress_html(body)
        res["error"] = None
        if res["custom_weight"] and res["score"] is None:
            res["error"] = "该课为自定义权重模式，老平台不显示分数"
        return res
    except Exception as e:  # noqa: BLE001
        logger.debug("取课程进度失败 {} -> {}: {}".format(
            course.get("title"), type(e).__name__, e))
        empty["error"] = "{}: {}".format(type(e).__name__, str(e)[:60])
        return empty


def format_progress(p: dict) -> str:
    """把进度结果拼成给选课界面看的短字符串，例如 '已完成100% 92.03分'。"""
    if not p:
        return "?"
    if p.get("percent") is None and p.get("score") is None:
        return p.get("error") or "?"
    parts = []
    if p.get("percent") is not None:
        pct = p["percent"]
        parts.append("已完成{}%".format(pct) if pct >= 100 else "进行中{}%".format(pct))
    if p.get("score") is not None:
        parts.append("{:.2f}分".format(p["score"]))
    elif p.get("custom_weight"):
        parts.append("自定义权重")
    return " ".join(parts) if parts else "?"
