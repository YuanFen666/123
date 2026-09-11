# -*- coding: utf-8 -*-
"""
考试看板：只读地列出每门课的考试，并在控制台/日志显著提示 + 可选推送到手机。

============================================================================
为什么只做「发现 + 提醒」，不做自动答题
============================================================================
考试的接口和学习通章节测验完全是两套：

    章节测验：POST work/addStudentWorkNew，一次请求，可重做
    考试    ：手机端 SSR 多步流程
              GET  exam-ans/exam/phone/task-exam        考试封面（拿 examAnswerId）
              POST exam-ans/exam/phone/start            开始考试
              GET  exam-ans/exam/test/reVersionTestStartNew  取题
              POST exam-ans/exam/test/reVersionSubmitTestNew 提交答案

其中提交接口需要 get_exam_signature(uid, qid, x, y) —— 签名里要塞**屏幕点击坐标 x/y**，
本质是把请求伪装成「真机、真人点了一下屏幕」；若考试要求人脸识别
（封面页 faceRecognitionCompare 字段），参考实现还会上传预存的人脸照片去过 face-compare 比对。

**这两件事分别是绕过反作弊和绕过身份核验，本项目不做。**

另外有两个现实约束常被忽略：
  1. 一旦进入考场，**计时立刻开始**，到点系统自动交卷 ——
     不存在「先自动答题、我不提交、人工检查后再交」这个安全中间态；
  2. 考试通常**只有一次机会**，还可能有「仅限电脑客户端」「指定 IP」等限制。

所以本模块只做零风险的那部分：把考试和截止时间告诉你，你亲自去考。

接口来源：https://github.com/jexjws/CxKitty （GPL-3.0，作者 SocialSisterYi）
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from bs4 import BeautifulSoup

from api.logger import logger

# 课程考试列表（手机端 SSR 页面，纯只读）
EXAM_LIST_URL = "https://mooc1-api.chaoxing.com/exam/phone/task-list"

# 去重状态文件：避免同一场考试每次运行都推送
EXAM_STATE_FILE = "exam_state.json"

# 状态文本归类（不同账号/课程用的词不完全一样，这里按关键字宽松匹配）
_DONE_WORDS = ("已完成", "已结束", "已批阅", "已提交", "已过期")
_TODO_WORDS = ("待做", "未交", "未开始", "进行中", "待批阅", "未完成")

# 默认：剩余时间少于这么多小时就每次运行都提醒
DEFAULT_WARN_HOURS = 48.0


@dataclass
class ExamInfo:
    """一场考试。"""
    course_title: str
    course_id: str
    clazz_id: str
    cpi: str
    name: str
    status: str
    exam_id: str = ""
    enc_task: str = ""
    remain_text: str = ""
    remain_hours: Optional[float] = None

    @property
    def done(self) -> bool:
        return any(w in self.status for w in _DONE_WORDS)

    @property
    def todo(self) -> bool:
        if self.done:
            return False
        return any(w in self.status for w in _TODO_WORDS) or not self.status

    @property
    def remain_human(self) -> str:
        h = self.remain_hours
        if h is None:
            return self.remain_text or "未知"
        if h >= 24:
            return "{:.0f} 天 {:.0f} 小时".format(h // 24, h % 24)
        return "{:.1f} 小时".format(h)

    def line(self) -> str:
        return "  [{}] {:<24} 状态: {:<8} 截止: {}".format(
            self.course_title[:14], self.name[:24], self.status, self.remain_human)


def parse_remain_hours(text: str) -> Optional[float]:
    """
    把页面上的剩余时间文本解析成小时数。
    支持：'剩余2522小时3分钟' / '剩余2天3小时' / '剩余45分钟' / '2026-12-01 23:59'
    """
    if not text:
        return None
    t = str(text).strip()

    # 绝对时间
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})", t)
    if m:
        try:
            dt = datetime(*[int(x) for x in m.groups()])
            return (dt - datetime.now()).total_seconds() / 3600.0
        except ValueError:
            pass

    # 相对时间：逐段累加
    units = {"天": 24.0, "小时": 1.0, "时": 1.0, "分钟": 1.0 / 60.0, "分": 1.0 / 60.0}
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+)\s*(天|小时|时|分钟|分)", t):
        found = True
        total += int(num) * units[unit]
    return total if found else None


class ExamWatch:
    """
    考试看板。

    用法（main.py 里已接好）：
        ExamWatch(notification).run(courses)
    整个过程只发 GET，绝不进入考场、绝不提交任何东西。
    """

    def __init__(self, notification=None, warn_hours: float = DEFAULT_WARN_HOURS,
                 state_file: str = EXAM_STATE_FILE):
        self.notification = notification
        self.warn_hours = float(warn_hours or DEFAULT_WARN_HOURS)
        self.state_file = state_file

    # ---------------- 读取 ----------------
    def fetch(self, courses: List[dict]) -> List[ExamInfo]:
        from api.base import SessionManager          # 延迟导入，避免循环依赖
        session = SessionManager.get_session()
        exams: List[ExamInfo] = []

        for c in courses or []:
            params = {"courseId": c.get("courseId"), "classId": c.get("clazzId"), "cpi": c.get("cpi")}
            try:
                resp = session.get(EXAM_LIST_URL, params=params, timeout=20)
            except Exception as e:  # noqa: BLE001
                logger.warning("考试看板：读取课程 {} 的考试列表失败 -> {}: {}".format(
                    c.get("title"), type(e).__name__, e))
                continue
            if resp.status_code != 200:
                logger.warning("考试看板：课程 {} 考试列表返回 HTTP {}".format(c.get("title"), resp.status_code))
                continue
            try:
                exams.extend(self._parse(c, resp.text))
            except Exception as e:  # noqa: BLE001
                logger.warning("考试看板：解析课程 {} 的考试列表失败 -> {}: {}".format(
                    c.get("title"), type(e).__name__, e))
        return exams

    @staticmethod
    def _parse(course: dict, html: str) -> List[ExamInfo]:
        """从考试列表页 HTML 里解析出考试。结构（实测）：ul.nav > li[data] > p(名称) + span(状态) + span.fr(截止)"""
        soup = BeautifulSoup(html, "lxml")
        nav = soup.find("ul", {"class": "nav"})
        if not nav:
            return []
        out = []
        for li in nav.find_all("li"):
            data = li.get("data") or ""
            m = re.search(r"[?&]taskrefId=(\d+)", data)
            m2 = re.search(r"[?&]enc_task=([0-9a-zA-Z]+)", data)
            name = li.find("p").get_text(strip=True) if li.find("p") else "未命名考试"
            span = li.find("span")
            status = span.get_text(strip=True) if span else ""
            fr = li.find("span", {"class": "fr"})
            remain_text = fr.get_text(strip=True) if fr else ""
            out.append(ExamInfo(
                course_title=str(course.get("title") or ""),
                course_id=str(course.get("courseId") or ""),
                clazz_id=str(course.get("clazzId") or ""),
                cpi=str(course.get("cpi") or ""),
                name=name,
                status=status,
                exam_id=m.group(1) if m else "",
                enc_task=m2.group(1) if m2 else "",
                remain_text=remain_text,
                remain_hours=parse_remain_hours(remain_text),
            ))
        return out

    # ---------------- 输出 ----------------
    def render(self, exams: List[ExamInfo]) -> str:
        todo = [e for e in exams if e.todo]
        done = [e for e in exams if e.done]
        lines = ["", "=" * 92,
                 " 考试看板（只读；本程序不会替你进考场，请自己安排时间应考）",
                 "=" * 92]
        if not exams:
            lines.append("  没有查到任何考试。")
        else:
            if todo:
                lines.append(" 【待完成 {} 场】".format(len(todo)))
                for e in sorted(todo, key=lambda x: (x.remain_hours is None, x.remain_hours or 0)):
                    lines.append(e.line())
                    if e.remain_hours is not None and e.remain_hours <= self.warn_hours:
                        lines.append("      ⚠ 距截止不足 {:.0f} 小时，抓紧".format(self.warn_hours))
            if done:
                lines.append(" 【已完成 {} 场】".format(len(done)))
                for e in done:
                    lines.append(e.line())
        lines.append("=" * 92)
        return "\n".join(lines)

    # ---------------- 去重 ----------------
    def _load_state(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8-sig") as fp:
                return json.load(fp)
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            with open(self.state_file, "w", encoding="utf8") as fp:
                json.dump(state, fp, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.debug("考试看板：写入状态文件失败 -> {}".format(e))

    def _need_notify(self, exams: List[ExamInfo]) -> bool:
        """
        只在「有新的待做考试 / 状态变化 / 临近截止」时推送，避免每次运行都刷屏。
        """
        state = self._load_state()
        changed = False
        for e in exams:
            if not e.todo:
                continue
            key = e.exam_id or e.name
            old = state.get(key, {}).get("status")
            if old != e.status:
                changed = True
            if e.remain_hours is not None and e.remain_hours <= self.warn_hours:
                changed = True
        new_state = {e.exam_id or e.name: {"status": e.status, "name": e.name,
                                           "remain": e.remain_text}
                     for e in exams}
        if new_state != state:
            self._save_state(new_state)
        return changed

    # ---------------- 入口 ----------------
    def run(self, courses: List[dict]) -> List[ExamInfo]:
        exams = self.fetch(courses)
        report = self.render(exams)

        # 控制台 + 日志都要显眼
        for ln in report.splitlines():
            logger.info(ln)

        todo = [e for e in exams if e.todo]
        if todo and self.notification is not None and self._need_notify(exams):
            try:
                self.notification.send("chaoxing 考试提醒\n" + "\n".join(
                    "{}({}): {} {}".format(e.course_title, e.name, e.status, e.remain_text)
                    for e in todo))
            except Exception as e:  # noqa: BLE001
                logger.debug("考试看板：通知推送失败 -> {}".format(e))
        return exams
