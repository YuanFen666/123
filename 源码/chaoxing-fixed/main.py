# -*- coding: utf-8 -*-
import argparse
import configparser
import enum
import sys
import threading
import time
import traceback
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass
from queue import PriorityQueue, ShutDown
from threading import RLock
from typing import Any

from tqdm import tqdm
import requests

from api.answer import Tiku
from api.base import Chaoxing, Account, StudyResult
from api.exceptions import LoginError, InputFormatError
from api.logger import logger
from api.notification import Notification


class ChapterResult(enum.Enum):
    SUCCESS=0,
    ERROR=1,
    NOT_OPEN=2,
    PENDING=3


def log_error(func):
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except BaseException as e:
            logger.error(f"Error in thread {threading.current_thread().name}: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)
            raise

    return wrapper


# 以下异常属于“临时性网络问题”，不应该让 worker 线程退出。
# 原版 worker_thread 是 while True，异常被 log_error 抛出后线程直接死亡，
# 线程数只减不增，表现就是“越跑越慢，最后只能同时看两条视频”。
TRANSIENT_ERRORS = (
    requests.exceptions.Timeout,          # 含 ReadTimeout / ConnectTimeout
    requests.exceptions.ConnectionError,  # ConnectionReset / ChunkedEncodingError 等
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)


def is_transient_error(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_ERRORS)


def str_to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Samueli924/chaoxing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--use-cookies", action="store_true", help="使用cookies登录")

    parser.add_argument(
        "-c", "--config", type=str, default=None, help="使用配置文件运行程序"
    )
    parser.add_argument("-u", "--username", type=str, default=None, help="手机号账号")
    parser.add_argument("-p", "--password", type=str, default=None, help="登录密码")
    parser.add_argument(
        "-l", "--list", type=str, default=None, help="要学习的课程ID列表, 以 , 分隔"
    )
    parser.add_argument(
        "-s", "--speed", type=float, default=None, help="视频播放倍速 (默认1, 最大2)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=None, help="同时进行的章节数 (默认4, 如果一个章节有多个任务点，不会限制同时处理任务点的数量)"
    )

    parser.add_argument(
        "-v",
        "--verbose",
        "--debug",
        action="store_true",
        help="启用调试模式, 输出DEBUG级别日志",
    )
    parser.add_argument(
        "-a", "--notopen-action", type=str, default=None, 
        choices=["retry", "ask", "continue"],
        help="遇到关闭任务点时的行为: retry-重试, ask-询问, continue-继续"
    )
    parser.add_argument(
        "--all-courses",
        action="store_true",
        help="忽略 course_list，直接处理全部课程（并且不再弹出「请手动输入课程ID」的交互提示）",
    )
    parser.add_argument(
        "--list-courses",
        action="store_true",
        help="只登录并打印课程列表（每行 `序号|课程名|courseId`，纯文本，供启动脚本生成菜单）然后退出",
    )
    parser.add_argument(
        "--exam-only",
        action="store_true",
        help="只运行考试看板与就绪体检（只读），不进行刷课",
    )
    parser.add_argument(
        "--exam-take",
        action="store_true",
        help="【危险】进入考场自动答题（默认逐题保存但不交卷；配合 --exam-submit 才自动交卷）",
    )
    parser.add_argument(
        "--exam-submit",
        action="store_true",
        help="【危险】自动交卷（需与 --exam-take 同用；覆盖率达标才会交）",
    )
    parser.add_argument(
        "--exam-openc",
        type=str, default=None,
        help="整卷模式入口参数：从浏览器考试页 URL 里 ?openc= 后面那串复制过来",
    )
    parser.add_argument(
        "--account-index",
        type=int, default=None,
        help="多账号时直接用第 N 组（1 起），不再交互询问（供启动脚本调用）",
    )
    parser.add_argument(
        "--list-accounts",
        action="store_true",
        help="只打印 config.ini 里的账号列表（序号|打码手机号）到 stdout，不登录",
    )

    # 在解析之前捕获 -h 的行为
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        parser.print_help()
        sys.exit(0)

    return parser.parse_args()


def load_accounts(config) -> list:
    """
    读取 [accounts] 段里的多组账号。

    写法（一行一个账号，key 是手机号，value 是密码）：
        [accounts]
        13800000000 = 密码A
        13800138000 = 密码B

    用 key/value 而不是 "账号:密码" 拼接，是为了不受密码里含逗号/冒号/等号的影响。
    返回 [(用户名, 密码), ...]；没有这个段就返回空列表（继续用 [common] 里的单账号）。
    """
    accounts = []
    if config.has_section("accounts"):
        for user, pwd in config.items("accounts"):
            user, pwd = (user or "").strip(), (pwd or "").strip()
            if user and pwd:
                accounts.append((user, pwd))
    return accounts


def mask_account(user: str) -> str:
    """手机号打码，用于在菜单/日志里显示。"""
    user = str(user or "")
    if len(user) >= 7:
        return "{}****{}".format(user[:3], user[-2:])
    return (user[:2] + "***") if user else "?"


def choose_account(accounts: list, forced_index=None):
    """
    多账号时让用户选一个；只有一组就直接用。

    三种"不能问"的场景都会自动退回第 1 个账号（绝不卡住）：
      · forced_index 指定了编号（bat 里已经问过，用 --account-index 传进来）
      · stdout 被重定向（bat 用 for /f 抓输出时，菜单根本显示不出来）
      · stdin 读完 / Ctrl+C
    """
    if not accounts:
        return None, None
    if forced_index:
        try:
            idx = int(forced_index)
            if 1 <= idx <= len(accounts):
                user, pwd = accounts[idx - 1]
                logger.info("使用指定的账号 #{}: {}".format(idx, mask_account(user)))
                return user, pwd
            logger.warning("--account-index {} 超出范围（共 {} 组），改用第 1 个".format(
                idx, len(accounts)))
        except (TypeError, ValueError):
            logger.warning("--account-index 不是数字，改用第 1 个")
        return accounts[0]
    if len(accounts) == 1:
        logger.info("使用 config.ini 里的账号: {}".format(mask_account(accounts[0][0])))
        return accounts[0]
    # 【关键】bat 里 for /f 会把 stdout 抓走，这时菜单是"看不见"的 ——
    # 如果照样 input()，程序就静默卡死，用户只看到"读到 2 组账号"然后不动了。
    if not sys.stdout.isatty():
        logger.warning("输出被重定向、无法显示选择菜单，自动使用第 1 个账号: {}".format(
            mask_account(accounts[0][0])))
        logger.warning("（想让程序问你，请在 cmd 里直接跑 exe，或用 --account-index 指定）")
        return accounts[0]

    print("")
    print("=" * 60)
    print(" config.ini 里配置了 {} 组账号，请选择本次要登录的：".format(len(accounts)))
    for i, (user, _) in enumerate(accounts, 1):
        print("   [{}] {}".format(i, mask_account(user)))
    print("=" * 60)
    while True:
        try:
            raw = input("请输入编号后回车（直接回车=第 1 个）: ").strip()
        except (EOFError, KeyboardInterrupt):
            logger.warning("无法交互选择账号，默认使用第 1 个: {}".format(mask_account(accounts[0][0])))
            return accounts[0]
        if not raw:
            return accounts[0]
        if raw.isdigit() and 1 <= int(raw) <= len(accounts):
            user, pwd = accounts[int(raw) - 1]
            logger.info("已选择账号: {}".format(mask_account(user)))
            return user, pwd
        print("  编号不对，请重新输入（1~{}）".format(len(accounts)))


def load_config_from_file(config_path):
    """从配置文件加载设置"""
    config = configparser.ConfigParser()
    # 用 utf-8-sig：Windows 记事本另存为 UTF-8 会带 BOM，而带 BOM 的文件用 "utf8" 读
    # 会让 configparser 直接抛 MissingSectionHeaderError（第一段变成 "\ufeff[common]"），
    # 现象就是「程序一启动就崩」。utf-8-sig 带不带 BOM 都能读。
    config.read(config_path, encoding="utf-8-sig")
    
    common_config: dict[str, Any] = {}
    tiku_config: dict[str, Any] = {}
    notification_config: dict[str, Any] = {}
    
    # 检查并读取common节
    if config.has_section("common"):
        common_config = dict(config.items("common"))
        # 处理course_list，将字符串转换为列表
        if "course_list" in common_config and common_config["course_list"]:
            common_config["course_list"] = [item.strip() for item in common_config["course_list"].split(",") if item.strip()]
        # 处理speed，将字符串转换为浮点数
        if "speed" in common_config:
            common_config["speed"] = float(common_config["speed"])
        if "jobs" in common_config:
            common_config["jobs"] = int(common_config["jobs"])
        # 处理notopen_action，设置默认值为retry
        if "notopen_action" not in common_config:
            common_config["notopen_action"] = "retry"
        if "use_cookies" in common_config:
            common_config["use_cookies"] = str_to_bool(common_config["use_cookies"])
        if "username" in common_config and common_config["username"] is not None:
            common_config["username"] = common_config["username"].strip()
        if "password" in common_config and common_config["password"] is not None:
            common_config["password"] = common_config["password"].strip()

    # 检查并读取tiku节
    if config.has_section("tiku"):
        tiku_config = dict(config.items("tiku"))
        # 处理数值类型转换
        for key in ["delay", "cover_rate"]:
            if key in tiku_config:
                tiku_config[key] = float(tiku_config[key])

    # 检查并读取notification节
    if config.has_section("notification"):
        notification_config = dict(config.items("notification"))

    # 多账号：[accounts] 段（可选）。没有就用 [common] 里的单账号。
    accounts = load_accounts(config)
    if accounts:
        common_config["accounts"] = accounts
        # 用 debug 级别：--list-accounts 会被 bat 反复拉起，
        # 用 info 会在选择菜单上方留下一行突兀的日志。
        logger.debug("config.ini 里读到了 {} 组账号".format(len(accounts)))

    return common_config, tiku_config, notification_config


def build_config_from_args(args):
    """从命令行参数构建配置"""
    common_config = {
        "use_cookies": args.use_cookies,
        "username": args.username,
        "password": args.password,
        "course_list": [item.strip() for item in args.list.split(",") if item.strip()] if args.list else None,
        "speed": args.speed if args.speed else 1.0,
        "jobs": args.jobs if args.jobs else 4,
        "notopen_action": args.notopen_action if args.notopen_action else "retry"
    }
    return common_config, {}, {}


def init_config():
    """初始化配置"""
    args = parse_args()

    if args.config:
        common_config, tiku_config, notification_config = load_config_from_file(args.config)

        # 【重要】命令行参数覆盖配置文件。
        # 原版走 -c 时会把命令行参数**全部忽略**（比如 -l 指定的课程列表根本不生效），
        # 启动脚本正是靠这个把「菜单里选的课程」传进来的，所以这里必须做覆盖。
        # 只在参数「确实传了」时才覆盖（因此上面把这些参数的 default 都改成了 None）。
        if args.list:
            common_config["course_list"] = [x.strip() for x in args.list.split(",") if x.strip()]
        if args.username:
            common_config["username"] = args.username.strip()
        if args.password:
            common_config["password"] = args.password.strip()
        if args.speed:
            common_config["speed"] = float(args.speed)
        if args.jobs:
            common_config["jobs"] = int(args.jobs)
        if args.notopen_action:
            common_config["notopen_action"] = args.notopen_action
        if args.use_cookies:
            common_config["use_cookies"] = True
    else:
        common_config, tiku_config, notification_config = build_config_from_args(args)

    # 把「只影响本次运行」的开关带上（它们不写在配置文件里）。
    # 用下划线前缀，避免和配置文件里的键冲突。
    common_config["_list_courses"] = bool(getattr(args, "list_courses", False))
    common_config["_exam_only"] = bool(getattr(args, "exam_only", False))
    common_config["_all_courses"] = bool(getattr(args, "all_courses", False))
    common_config["_exam_take"] = bool(getattr(args, "exam_take", False))
    common_config["_exam_submit"] = bool(getattr(args, "exam_submit", False))
    if getattr(args, "exam_openc", None):
        common_config["exam_openc"] = args.exam_openc.strip()
    if getattr(args, "account_index", None):
        common_config["_account_index"] = args.account_index
    common_config["_list_accounts"] = bool(getattr(args, "list_accounts", False))
    return common_config, tiku_config, notification_config


def take_exams(watch, exams, courses, tiku, auto_submit: bool, openc: str = "",
               max_exams: int = 1) -> None:
    """
    对「体检通过、现在能考」的考试执行自动作答。

    安全设计（见 api/exam_take.py）：
      - 默认逐题保存但**不交卷**，由你自己核对后手动交；
      - 服务器上已有答案的题默认跳过，不覆盖；
      - 开考后任何异常都会反复提示「请立即手动完成考试」。
    """
    from api.exam_take import ExamTaker, ExamAborted

    # ---- 使用前的说明（只提示，不阻断）----
    logger.info("=" * 90)
    logger.info("考试作答 · 使用说明")
    logger.info("  这是什么：替你进考场，用题库链逐题作答（第 2 档只保存，第 3 档才交卷）")
    logger.info("  前提一：先用「只体检」确认这场考试现在能考（别卡在章节任务点门槛上）")
    logger.info("  前提二：进考场就开始计时、通常只有一次机会 —— 所以第一次请用第 2 档，")
    logger.info("          让程序答题但由你自己核对后交卷")
    logger.info("  整卷模式：整卷页的 openc 会从页面里自动读取，不需要手工配置")
    logger.info("  成本提示：命中本地 cache.json 的题目不消耗任何题库额度")
    logger.info("=" * 90)
    if not (openc or "").strip():
        logger.debug("（未从配置提供 openc —— 整卷页打开后会自动从页面 input#openc 读取）")

    by_course = {c.get("courseId"): c for c in (courses or [])}
    todo = [e for e in (exams or []) if e.todo and e.exam_id]
    if not todo:
        logger.info("没有需要作答的考试。")
        return

    # 每场考试都会消耗一次机会、进考场就开始计时，所以默认只处理一场。
    # max_exams <= 0 表示**不限制**（把选中课程的考试全都做掉）—— 合并模式就是这么调的。
    try:
        _max = int(max_exams)
    except (TypeError, ValueError):
        _max = 1
    if _max > 0 and len(todo) > _max:
        logger.warning("待做考试有 {} 场，本次只处理前 {} 场（每场都会消耗一次机会，"
                       "不自动接着考下一门）；需要继续请再运行一次。".format(len(todo), _max))
        todo = todo[:_max]
    elif _max <= 0:
        logger.info("待做考试 {} 场，按「不限制」全部处理".format(len(todo)))

    # 二次确认：这是不可逆操作（考试通常只有一次机会）
    logger.warning("=" * 90)
    logger.warning("即将进入考场自动作答：{} 场；交卷方式：{}".format(
        len(todo), "程序自动交卷" if auto_submit else "只保存答案，由你自己交卷"))
    logger.warning("=" * 90)

    ready = getattr(watch, "last_ready", None) or {}
    for e in todo:
        r = ready.get(e.exam_id)
        if r is not None and not r.can_start:
            logger.warning("跳过《{}》：{}".format(e.name, r.reason or "就绪体检未通过"))
            continue
        course = by_course.get(e.course_id)
        if not course:
            logger.warning("跳过《{}》：找不到对应课程信息".format(e.name))
            continue
        taker = ExamTaker(course, e, tiku, auto_submit=auto_submit, openc=openc)
        try:
            taker.run()
        except ExamAborted as ex:
            # 还没开考就失败 —— 没有消耗考试机会
            logger.warning("《{}》未进入考场（未消耗机会）：{}".format(e.name, ex))
        except Exception as ex:  # noqa: BLE001
            logger.error("《{}》作答过程出错：{}: {}".format(e.name, type(ex).__name__, ex))


def init_chaoxing(common_config, tiku_config):
    """初始化超星实例"""
    username = common_config.get("username", "")
    password = common_config.get("password", "")
    use_cookies = common_config.get("use_cookies", False)

    # ---- 多账号支持 ----
    # [accounts] 段里配了多组就让你选；只配了一组（或只有 [common] 的单账号）直接登录。
    accounts = list(common_config.get("accounts") or [])
    if not accounts and username and password:
        accounts = [(username, password)]
    if accounts and not use_cookies:
        username, password = choose_account(accounts, common_config.get("_account_index"))

    # 如果没有提供用户名密码，从命令行获取
    if (not username or not password) and not use_cookies:
        username = input("请输入你的手机号, 按回车确认\n手机号:")
        password = input("请输入你的密码, 按回车确认\n密码:")
    
    account = Account(username, password)
    
    # 设置题库
    tiku = Tiku()
    tiku.config_set(tiku_config)  # 载入配置
    tiku = tiku.get_tiku_from_config()  # 载入题库
    tiku.init_tiku()  # 初始化题库
    
    # 获取查询延迟设置
    query_delay = tiku_config.get("delay", 0)
    
    # 实例化超星API
    chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=query_delay)
    
    return chaoxing


def process_job(chaoxing: Chaoxing, course:dict, job:dict, job_info:dict, speed:float) -> StudyResult:
    """处理单个任务点"""
    # 视频任务
    if job["type"] == "video":
        logger.trace(f"识别到视频任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        # 超星的接口没有返回当前任务是否为Audio音频任务
        video_result = chaoxing.study_video(
            course, job, job_info, _speed=speed, _type="Video"
        )
        if video_result.is_failure():
            logger.warning("当前任务非视频任务, 正在尝试音频任务解码")
            video_result = chaoxing.study_video(
                course, job, job_info, _speed=speed, _type="Audio")
        if video_result.is_failure():
            logger.warning(
                f"出现异常任务 -> 任务章节: {course['title']} 任务ID: {job['jobid']}, 已跳过"
            )
        return video_result
    # 文档任务
    elif job["type"] == "document":
        logger.trace(f"识别到文档任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        return chaoxing.study_document(course, job)
    # 测验任务
    elif job["type"] == "workid":
        logger.trace(f"识别到章节检测任务, 任务章节: {course['title']}")
        return chaoxing.study_work(course, job, job_info)
    # 阅读任务
    elif job["type"] == "read":
        logger.trace(f"识别到阅读任务, 任务章节: {course['title']}")
        return chaoxing.study_read(course, job, job_info)

    logger.error("Unknown job type: %s", job["type"])
    return StudyResult.ERROR


@dataclass(order=True)
class ChapterTask:
    index: int
    point: dict[str, Any]
    result: ChapterResult = ChapterResult.PENDING
    tries: int = 0

class JobProcessor:
    def __init__(self, chaoxing: Chaoxing, course: dict[str, Any], tasks: list[ChapterTask], config: dict[str, Any]):
        self.chaoxing = chaoxing
        self.course = course
        self.speed = config["speed"]
        self.max_tries = 5
        self.tasks = tasks
        self.failed_tasks: list[ChapterTask] = []
        self.task_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.retry_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.wait_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.threads: list[threading.Thread] = []
        self.worker_num = config["jobs"]
        self.config = config
        self._stopping = False

    def _spawn_worker(self):
        thread = threading.Thread(target=self.worker_thread, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def watchdog_thread(self):
        """
        兜底守护线程：周期性检查 worker 是否还在。
        正常情况下 worker 永远不会退出（worker_thread 内部已做异常兜底），
        但万一还有别的诡异情况把它掀掉，这里会立刻补一个新的，
        避免出现“并发数只减不增，最后只能同时看两条视频”。
        """
        max_threads = self.worker_num * 4
        while not self._stopping:
            time.sleep(3)
            if self._stopping:
                return
            alive = sum(1 for t in self.threads if t.is_alive())
            if alive >= self.worker_num:
                continue
            if len(self.threads) >= max_threads:
                logger.error("worker 线程反复退出且已达补位上限({}), 不再补位", max_threads)
                continue
            logger.warning("检测到 worker 线程退出, 自动补位使并发恢复: 存活 {}/{}", alive + 1, self.worker_num)
            self._spawn_worker()

    def run(self):
        for task in self.tasks:
            self.task_queue.put(task)

        for i in range(self.worker_num):
            self._spawn_worker()

        threading.Thread(target=self.retry_thread, daemon=True).start()
        threading.Thread(target=self.watchdog_thread, daemon=True).start()

        self.task_queue.join()
        self._stopping = True
        time.sleep(0.5)
        self.task_queue.shutdown()


    @log_error
    def worker_thread(self):
        tqdm.set_lock(tqdm.get_lock())
        while True:
            try:
                task = self.task_queue.get()
            except ShutDown:
                logger.info("Queue shut down")
                return

            # 关键修复：单章处理过程中的异常不再让线程退出。
            # process_chapter 里任何一次网络抖动（读超时/连接被重置）原来都会
            # 顺着 log_error 抛出 while True，线程随即死亡且永不重建，
            # 于是并发数从 4 掉到 3、2、1…… 现在统一转成 ERROR 走重试。
            try:
                task.result = process_chapter(self.chaoxing, self.course, task.point, self.speed)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:  # noqa: BLE001
                if is_transient_error(e):
                    logger.warning(
                        "网络异常({}) -> 章节: {} , 已转重试 ({}/{} attempts)",
                        type(e).__name__, task.point.get("title"), task.tries + 1, self.max_tries,
                    )
                else:
                    logger.error(
                        "章节处理异常({}: {}) -> {}, 已转重试 ({}/{} attempts)",
                        type(e).__name__, e, task.point.get("title"), task.tries + 1, self.max_tries,
                    )
                    logger.debug(traceback.format_exc())
                task.result = ChapterResult.ERROR

            match task.result:
                case ChapterResult.SUCCESS:
                    logger.debug("Task success: {}", task.point["title"])
                    self.task_queue.task_done()
                    logger.debug(f"unfinished task: {self.task_queue.unfinished_tasks}")

                case ChapterResult.NOT_OPEN:
                    # task.tries += 1
                    if self.config["notopen_action"] == "continue":
                        logger.warning("章节未开启: {}, 正在跳过", task.point["title"])
                        self.task_queue.task_done()
                        continue

                    if task.tries >= self.max_tries:
                        logger.error(
                            "章节未开启: {} 可能由于上一章节的章节检测未完成, 也可能由于该章节因为时效已关闭，"
                            "请手动检查完成并提交再重试。或者在配置中配置(自动跳过关闭章节/开启题库并启用提交)"
                        , task.point["title"])
                        self.task_queue.task_done()
                        continue

                    # self.wait_queue.put(task)
                    self.retry_queue.put(task)

                case ChapterResult.ERROR:
                    task.tries += 1
                    logger.warning("Retrying task {} ({}/{} attempts)", task.point["title"], task.tries,
                                   self.max_tries)
                    if task.tries >= self.max_tries:
                        logger.error("Max retries reached for task: {}", task.point["title"])
                        self.failed_tasks.append(task)
                        self.task_queue.task_done()
                        continue
                    self.retry_queue.put(task)

                case _:
                    logger.error("Invalid task state {} for task {}", task.result, task.point["title"])
                    self.failed_tasks.append(task)
                    self.task_queue.task_done()

    @log_error
    def retry_thread(self):
        # 这个线程一旦死亡，失败任务就永远回不到主队列，task_queue.join() 会永久挂起。
        # 所以内部同样做兜底，不允许异常把它掀掉。
        while True:
            try:
                task = self.retry_queue.get()
                self.task_queue.put(task)
                self.task_queue.task_done() # task_done is not called when a task failed and needs to be retried, so if is reput into the queue, the task num will increase by one and become more than the real task number
                time.sleep(1)
            except ShutDown:
                return
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:  # noqa: BLE001
                logger.warning("retry_thread 出现异常({}: {}), 已忽略并继续", type(e).__name__, e)
                time.sleep(1)


def process_chapter(chaoxing: Chaoxing, course:dict[str, Any], point:dict[str, Any], speed:float) -> ChapterResult:
    """处理单个章节"""
    logger.info(f'当前章节: {point["title"]}')
    if point["has_finished"]:
        logger.info(f'章节：{point["title"]} 已完成所有任务点')
        return ChapterResult.SUCCESS
    
    # 随机等待，避免请求过快
    chaoxing.rate_limiter.limit_rate(random_time=True,random_min=0, random_max=0.2)
    
    # 获取当前章节的所有任务点
    job_info = None
    jobs, job_info = chaoxing.get_job_list(course, point)

    # 发现未开放章节, 根据配置处理
    if job_info.get("notOpen", False):
        return ChapterResult.NOT_OPEN

    # 已经默认处理空任务，此处不需要判断
    if not jobs:
        pass

    # TODO: 个别章节很恶心，多到5个点，可以并行处理，将来会让不同课程不同章节的所有任务点共享一个队列，从而实现全局并行
    job_results:list[StudyResult]=[]
    with ThreadPoolExecutor(max_workers=5) as executor:
        for result in executor.map(lambda job: process_job(chaoxing, course, job, job_info, speed), jobs):
            job_results.append(result)
    
    for result in job_results:
        if result.is_failure():
            return ChapterResult.ERROR

    return ChapterResult.SUCCESS



def process_course(chaoxing: Chaoxing, course:dict[str, Any], config: dict):
    """处理单个课程"""
    logger.info(f"开始学习课程: {course['title']}")
    
    # 获取当前课程的所有章节
    point_list = chaoxing.get_course_point(
        course["courseId"], course["clazzId"], course["cpi"]
    )

    # 为了支持课程任务回滚, 采用下标方式遍历任务点

    _old_format_sizeof = tqdm.format_sizeof
    tqdm.format_sizeof = format_time
    tqdm.set_lock(RLock())

    tasks=[]

    for i, point in enumerate(point_list["points"]):
        task = ChapterTask(point=point, index=i)
        tasks.append(task)
    p = JobProcessor(chaoxing, course, tasks, config)
    p.run()


    tqdm.format_sizeof = _old_format_sizeof

    """
    while __point_index < len(point_list["points"]):
        point = point_list["points"][__point_index]
        logger.debug(f"当前章节 __point_index: {__point_index}")
        
        result, auto_skip_notopen = process_chapter(
            chaoxing, course, point, RB, notopen_action, speed, auto_skip_notopen
        )
        
        if result == -1:  # 退出当前课程
            break
        elif result == 0:  # 重试前一章节
            __point_index -= 1  # 默认第一个任务总是开放的
        else:  # 继续下一章节
            __point_index += 1
    """



def filter_courses(all_course, course_list):
    """过滤要学习的课程"""
    if not course_list:
        # 手动输入要学习的课程ID列表
        print("*" * 10 + "课程列表" + "*" * 10)
        for course in all_course:
            print(f"ID: {course['courseId']} 课程名: {course['title']}")
        print("*" * 28)
        try:
            course_list = input(
                "请输入想要学习的课程列表,以逗号分隔,例: 2151141,189191,198198\n"
            ).split(",")
        except Exception as e:
            raise InputFormatError("输入格式错误") from e

    # 筛选需要学习的课程
    course_task = []
    course_ids = []
    for course in all_course:
        if course["courseId"] in course_list and course["courseId"] not in course_ids:
            course_task.append(course)
            course_ids.append(course["courseId"])
    
    # 如果没有指定课程，则学习所有课程
    if not course_task:
        course_task = all_course
    
    return course_task


def format_time(num, suffix='', divisor=''):
    total_time = round(num)
    sec = total_time % 60
    mins = (total_time % 3600) // 60
    hrs = total_time // 3600

    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{sec:02d}"

    return f"{mins:02d}:{sec:02d}"


def main():
    """主程序入口"""
    try:
        # 初始化配置
        common_config, tiku_config, notification_config = init_config()

        # --list-accounts：只打印账号列表就退出（不登录、不初始化题库），
        # 供启动脚本拉起「选账号」菜单用。日志走 stderr，这里只往 stdout 打结果。
        if common_config.get("_list_accounts"):
            _accs = list(common_config.get("accounts") or [])
            if not _accs and common_config.get("username"):
                _accs = [(common_config["username"], common_config.get("password", ""))]
            try:
                sys.stdout.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass
            for _i, (_u, _p) in enumerate(_accs, 1):
                print("{}|{}".format(_i, mask_account(_u)))
            return

        # 强制播放按照配置文件调节
        common_config["speed"] = min(2.0, max(1.0, common_config.get("speed", 1.0)))
        common_config["notopen_action"] = common_config.get("notopen_action", "retry")
        
        # 初始化超星实例
        chaoxing = init_chaoxing(common_config, tiku_config)
        
        # 设置外部通知
        notification = Notification()
        notification.config_set(notification_config)
        notification = notification.get_notification_from_config()
        notification.init_notification()
        
        # 检查当前登录状态
        _login_state = chaoxing.login(login_with_cookies=common_config.get("use_cookies", False))
        if not _login_state["status"]:
            raise LoginError(_login_state["msg"])
        
        # 获取所有的课程列表
        all_course = chaoxing.get_course_list()

        # --- 只列课程：给启动脚本生成菜单用 ---
        # 注意用 print() 走 stdout；程序日志由 loguru 写到 stderr，
        # 所以 stdout 是干净的纯文本，脚本可以放心按行解析。
        if common_config.get("_list_courses"):
            # Windows 控制台可能是 GBK(cp936)，遇到编不进去的字符（比如课程名里的 emoji）
            # print 会抛 UnicodeEncodeError；这里降级成替换字符，绝不因为一个字符让菜单挂掉。
            try:
                sys.stdout.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass
            for _i, _c in enumerate(all_course, 1):
                # 分隔符是 |，课程名里若含 | 或换行会破坏脚本的解析，先净化
                _title = str(_c.get("title", "")).replace("|", "/").replace("\r", " ").replace("\n", " ")
                print("{}|{}|{}".format(_i, _title, _c.get("courseId", "")), flush=True)
            return

        # 过滤要学习的课程（-l 指定的课程ID）
        # 注意：原版在 course_list 为空时会「弹交互提示」让你手输课程ID。
        # --all-courses / --exam-only 下不需要这个提示（无人值守和启动脚本会卡住），直接全选。
        if common_config.get("_all_courses") or (common_config.get("_exam_only")
                                                 and not common_config.get("course_list")):
            course_task = all_course
        else:
            course_task = filter_courses(all_course, common_config.get("course_list"))

        # 考试看板（只读：只列出考试和截止时间，绝不进考场、绝不提交）
        # 失败也只记一条警告，绝不影响刷课主流程
        _exams = []
        _watch = None
        if str_to_bool(common_config.get("exam_watch", True)):
            try:
                from api.exam import ExamWatch
                try:
                    warn_hours = float(common_config.get("exam_warn_hours") or 48)
                except (TypeError, ValueError):
                    warn_hours = 48.0
                _watch = ExamWatch(notification, warn_hours=warn_hours)
                _exams = _watch.run(course_task or all_course)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"考试看板执行失败（不影响刷课）: {type(e).__name__}: {e}")

        # --- 考试模式 ---
        if common_config.get("_exam_only"):
            if common_config.get("_exam_take"):
                logger.info("考试模式：进入考场自动作答。")
                take_exams(_watch, _exams, course_task or all_course, chaoxing.tiku,
                           auto_submit=bool(common_config.get("_exam_submit")),
                           openc=str(common_config.get("exam_openc") or ""),
                           max_exams=int(common_config.get("exam_take_max", 1) or 1))
            else:
                logger.info("考试模式：只做考试看板与就绪体检（只读），不进入考场。")
            return

        # 开始学习
        logger.info(f"课程列表过滤完毕, 当前课程任务数量: {len(course_task)}")
        for course in course_task:
            process_course(chaoxing, course, common_config)
        
        logger.info("所有课程学习任务已完成")
        notification.send("chaoxing : 所有课程学习任务已完成")

        # --- 合并模式：刷课全部跑完后，接着把考试也做掉 ---
        # 触发条件就是「给了 --exam-take 但没给 --exam-only」——
        # 不需要新参数：考试模式是"只考试"，合并模式是"刷课+考试一起"。
        # 注意两档的考试场数上限不同：
        #   考试模式   用 exam_take_max（默认 1，一场就跑，避免误考下一门）
        #   合并模式   默认**全部**（用户明确选了"一并进行"，就是要把选中的课都考完）
        if common_config.get("_exam_take"):
            logger.info("=" * 90)
            logger.info("刷课全部完成，接着处理考试（合并模式）")
            logger.info("=" * 90)
            take_exams(_watch, _exams, course_task or all_course, chaoxing.tiku,
                       auto_submit=bool(common_config.get("_exam_submit")),
                       openc=str(common_config.get("exam_openc") or ""),
                       max_exams=0)   # 0 = 不限制，选中课程的考试全做
        
    except SystemExit as e:
        if e.code != 0:
            logger.error(f"错误: 程序异常退出, 返回码: {e.code}")
        sys.exit(e.code)
    except KeyboardInterrupt as e:
        logger.error(f"错误: 程序被用户手动中断, {e}")
    except BaseException as e:
        logger.error(f"错误: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        try:
            notification.send(f"chaoxing : 出现错误 {type(e).__name__}: {e}\n{traceback.format_exc()}")
        except Exception:
            pass  # 如果通知发送失败，忽略异常
        raise e


if __name__ == "__main__":
    main()
