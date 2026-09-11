# -*- coding: utf-8 -*-
"""
ANEVOL 题库 -> tikuAdapter 协议 本地桥接服务
=================================================
用途：让 chaoxing (Samueli924/chaoxing) v3.1.4 的 TikuAdapter 题库
      能够调用 ANEVOL 题库 API。

原理：
  chaoxing 的 TikuAdapter 会向配置里的 url 发送：
      POST  {"question": "...", "options": ["选项1","选项2",...], "type": 0|1|2|3|4}
  并期望收到：
      {"answer": {"bestAnswer": ["答案1", "答案2", ...]}}

  本服务把上面的请求翻译成 ANEVOL 的 /api/search 请求，
  再把 ANEVOL 的返回转换成 bestAnswer 列表。

只用 Python 标准库，无第三方依赖。

配置：只改下面 ANEVOL_TOKEN（和可选端口）即可。
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 用户配置区
# ---------------------------------------------------------------------------
ANEVOL_TOKEN = ""
ANEVOL_URL = "https://tiku.anevol.cn/api/search"


def _app_dir():
    """exe/py 所在目录（兼容 PyInstaller 打包）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _load_token():
    """token 解析顺序：环境变量 > 同目录 anevol_token.txt > 内置默认值。"""
    env = os.environ.get("ANEVOL_TOKEN")
    if env and env.strip():
        return env.strip()
    try:
        p = os.path.join(_app_dir(), "anevol_token.txt")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                t = f.read().strip()
            if t:
                return t
    except Exception:
        pass
    return ANEVOL_TOKEN


ANEVOL_TOKEN = _load_token()

# 备用题库：网课小工具（GO题 cx.icodef.com）。ANEVOL 失败时才会用到。
# 该免费接口返回质量不稳定，默认关闭；需要时改成 True。
GO_FALLBACK_ENABLE = False
GO_URL = "https://cx.icodef.com/wyn-nb?v=4"

HOST = "127.0.0.1"
PORT = 8787

TIMEOUT = 45          # 单次请求超时（秒）
MAX_RETRY = 2         # 传输类错误（网络/超时）的重试次数
MIN_INTERVAL = 0.3    # 全局最小请求间隔（秒），防止触发限流

# “AI服务暂时不可用”这类服务端故障，重试一次就够了：ANEVOL 每次要 20~25 秒才返回错误，
# 傻等三轮会把整章拖到几十分钟。连续失败到 CB_THRESHOLD 次就熔断 CB_PAUSE 秒。
CB_THRESHOLD = 8      # 连续失败多少次后熔断
CB_PAUSE = 90         # 熔断暂停秒数
NON_RETRY_WORDS = ("密钥", "无效", "次数不足", "余额", "额度", "不存在")
# ---------------------------------------------------------------------------

TYPE_MAP = {0: "single", 1: "multiple", 2: "fill", 3: "judgement", 4: "short_answer"}
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

TRUE_WORDS = {"正确", "对", "√", "是", "T", "True", "true"}
FALSE_WORDS = {"错误", "错", "×", "否", "不对", "不正确", "F", "False", "false"}

_rate_lock = threading.Lock()
_last_call = [0.0]

_LOG_LOCK = threading.Lock()
LOG_PATH = os.path.join(_app_dir(), "anevol_bridge.log")

# 熔断器状态
_cb_lock = threading.Lock()
_cb_consec_fail = [0]
_cb_pause_until = [0.0]

_QUERY_STATS = {"ok": 0, "fail": 0}


def _cb_pause_remaining():
    with _cb_lock:
        return max(0.0, _cb_pause_until[0] - time.time())


def _cb_report(ok):
    with _cb_lock:
        if ok:
            _cb_consec_fail[0] = 0
            return
        _cb_consec_fail[0] += 1
        if _cb_consec_fail[0] >= CB_THRESHOLD and _cb_pause_until[0] < time.time():
            _cb_pause_until[0] = time.time() + CB_PAUSE
            _cb_consec_fail[0] = 0
            log("!! ANEVOL 服务端连续 %d 次不可用，暂停 %d 秒不再请求（本章这部分题会走随机作答，"
                "章节因为覆盖率不达标不会提交，等会儿重跑本章即可）" % (CB_THRESHOLD, CB_PAUSE))


def _is_transient(msg):
    """密钥无效/额度用完这种错误重试没有意义。"""
    return not any(w in (msg or "") for w in NON_RETRY_WORDS)


def log(*args):
    msg = " ".join(str(a) for a in args)
    stamped = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(stamped, flush=True)
    except Exception:
        pass
    # 同时落盘，方便出问题时把日志发给别人看
    try:
        with _LOG_LOCK:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(stamped + "\n")
    except Exception:
        pass


def _wait_rate_limit():
    with _rate_lock:
        now = time.time()
        wait = MIN_INTERVAL - (now - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def clean_option(text):
    """去掉选项前面的 A. / A、 / A． 之类的标号（TikuAdapter 已去一次，这里兜底）。"""
    if text is None:
        return ""
    s = str(text).strip()
    s = re.sub(r"^[A-Za-z]\s*[\.、．，,：:）\)]\s*", "", s)
    return s.strip()


def parse_letters(answer, option_count):
    """从 ANEVOL 返回里解析出选项序号列表。返回 None 表示不是字母形式。"""
    if not answer:
        return None
    s = answer.strip()
    # 全角字母转半角
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF21 <= ord(c) <= 0xFF3A else c for c in s)
    # 去掉常见分隔符和无关词
    s = re.sub(r"[正、确错误答案选项,，。;；|/\\\s\[\]\"'`]+", "", s)

    # 形式一："A" / "ABD" / "AB"（纯字母串）
    if s and re.fullmatch(r"[A-Za-z]+", s):
        cand = [LETTERS.index(c.upper()) for c in s]
        if all(0 <= i < option_count for i in cand):
            return sorted(set(cand))
        return None

    # 形式二："A和C" / "1、3" 之类
    tokens = re.findall(r"[A-Za-z]|\d+", answer)
    cand = []
    for t in tokens:
        if t.isdigit():
            i = int(t) - 1
        else:
            i = LETTERS.index(t.upper())
        if 0 <= i < option_count:
            cand.append(i)
    return sorted(set(cand)) if cand else None


def normalize_judgement(answer, options):
    """把任意形式的判断答案归一化成 '正确' 或 '错误'（必须落在配置的 true_list/false_list 里）。"""
    t = (answer or "").strip()
    if t in TRUE_WORDS:
        return "正确"
    if t in FALSE_WORDS:
        return "错误"
    # 先判否，再判是（"不正确" 里含有 "正确"）
    for w in ("错误", "不正确", "不对", "错", "×", "✗"):
        if w in t:
            return "错误"
    if t in ("x", "X"):
        return "错误"
    for w in ("正确", "√", "✓"):
        if w in t:
            return "正确"
    if t in ("对", "是") or t.startswith("对"):
        return "正确"
    # 可能是选项字母，用选项文本反推
    if options:
        idx = parse_letters(t, len(options))
        if idx:
            content = options[idx[0]]
            for w in ("错误", "不正确", "不对", "错", "否"):
                if w in content:
                    return "错误"
            for w in ("正确", "对", "√", "是"):
                if w in content:
                    return "正确"
    return t


def http_post_json(url, payload, headers=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def http_post_form(url, form):
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def ask_anevol(question, options, qtype):
    """调用 ANEVOL。返回答案字符串，失败返回 None。"""
    remaining = _cb_pause_remaining()
    if remaining > 0:
        log("ANEVOL 熔断中，直接跳过（还剩 %.0f 秒）" % remaining)
        return None

    payload = {"title": question}
    if options:
        payload["options"] = "\n".join(
            "{}. {}".format(LETTERS[i], c) for i, c in enumerate(options) if i < len(LETTERS)
        )
    if qtype:
        payload["type"] = qtype

    url = ANEVOL_URL + "?token=" + ANEVOL_TOKEN
    last_err = None
    for attempt in range(MAX_RETRY + 1):
        service_fail = False  # 服务端明确给出的失败（不是网线断了），重试大概率还是失败
        try:
            _wait_rate_limit()
            status, text = http_post_json(url, payload)
            body = json.loads(text)
            if body.get("code") == 1:
                _cb_report(True)
                _QUERY_STATS["ok"] += 1
                return str(body.get("answer", "")).strip()
            msg = str(body.get("msg") or "").strip()
            last_err = "code=%s msg=%s" % (body.get("code"), msg or "(无消息)")
            service_fail = True
            if not _is_transient(msg):
                log("ANEVOL 拒绝请求 -> %s（重试无意义，请检查套餐/密钥）" % last_err)
                break
            log("ANEVOL 返回失败 -> %s" % last_err)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = (json.loads(e.read().decode("utf-8", "replace")).get("msg") or "").strip()
            except Exception:
                pass
            # 能解析出 msg 说明响应体是 ANEVOL 自己给的，属于服务端故障而非网络问题
            service_fail = bool(detail)
            last_err = "HTTP %s%s" % (e.code, (" | " + detail) if detail else " " + str(e.reason))
            log("ANEVOL 请求失败 -> %s" % last_err)
        except Exception as e:  # noqa: BLE001
            last_err = "{}: {}".format(type(e).__name__, e)
            log("ANEVOL 请求异常 -> %s" % last_err)
        # “AI服务暂时不可用”这类故障每次都要 20 秒以上才返回，只试两次就收手，别把整章拖死
        if service_fail and attempt >= 1:
            break
        if attempt < MAX_RETRY:
            time.sleep(1.5 * (attempt + 1))

    _cb_report(False)
    _QUERY_STATS["fail"] += 1
    if _QUERY_STATS["fail"] % 10 == 0:
        log("统计：成功 %d 次 / 失败 %d 次" % (_QUERY_STATS["ok"], _QUERY_STATS["fail"]))
    return None


def ask_go(question):
    """备用题库（网课小工具 GO题）。"""
    if not GO_FALLBACK_ENABLE:
        return None
    try:
        _wait_rate_limit()
        status, text = http_post_form(GO_URL, {"question": question})
        body = json.loads(text)
        if body.get("code") == 1 and body.get("data"):
            return str(body["data"]).strip()
    except Exception as e:  # noqa: BLE001
        log("GO题 查询失败 ->", e)
    return None


def handle_query(question, option_list, type_int):
    """返回 bestAnswer 列表（给 chaoxing 的 TikuAdapter 用）。"""
    qtype = TYPE_MAP.get(type_int, "short_answer")
    options = [clean_option(o) for o in (option_list or [])]
    options = [o for o in options if o]

    ans = ask_anevol(question, options, qtype)
    if ans is None:
        ans = ask_go(question)
    if not ans:
        return []

    if qtype in ("single", "multiple") and options:
        idx = parse_letters(ans, len(options))
        if idx is not None:
            picked = [options[i] for i in idx]
            if qtype == "single":
                picked = picked[:1]
            if picked:
                log("{} | {} -> {}".format(qtype, question[:20], " / ".join(picked)))
                return picked
        # 不是字母形式（比如直接返回了选项内容），原样返回
        log("{} | {} -> {}".format(qtype, question[:20], ans[:40]))
        return [ans]

    if qtype == "judgement":
        j = normalize_judgement(ans, options)
        log("judgement | {} -> {}".format(question[:20], j))
        return [j]

    log("{} | {} -> {}".format(qtype, question[:20], ans[:40]))
    return [ans]


class Handler(BaseHTTPRequestHandler):
    server_version = "AnevolBridge/1.0"

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        # 浏览器打开 http://127.0.0.1:8787 可以看服务是否活着
        self._send(
            {
                "service": "anevol-bridge",
                "status": "running",
                "usage": "把 config.ini 的 [tiku] provider 设为 TikuAdapter，url 设为 http://127.0.0.1:%d/" % PORT,
            }
        )

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception as e:  # noqa: BLE001
            log("请求解析失败 ->", e)
            self._send({"answer": {"bestAnswer": []}})
            return

        question = str(req.get("question") or req.get("title") or "").strip()
        options = req.get("options") or []
        if isinstance(options, str):
            options = [o for o in options.split("\n")]
        type_int = req.get("type")
        if isinstance(type_int, str):
            rev = {v: k for k, v in TYPE_MAP.items()}
            type_int = rev.get(type_int, 4)
        if not isinstance(type_int, int):
            type_int = 4

        if not question:
            self._send({"answer": {"bestAnswer": []}})
            return

        try:
            best = handle_query(question, options, type_int)
        except Exception as e:  # noqa: BLE001
            log("处理异常 ->", e)
            best = []
        self._send({"answer": {"bestAnswer": best}, "plat": 0})

    def log_message(self, fmt, *args):  # 关掉默认访问日志
        return


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

    print("=" * 62)
    print(" ANEVOL 题库桥接服务  已启动")
    print(" 监听地址 : http://%s:%d/" % (HOST, PORT))
    print(" 题库     : ANEVOL (tiku.anevol.cn)  token=%s..." % ANEVOL_TOKEN[:8])
    print(" 备用题库 : %s" % ("网课小工具(GO题)" if GO_FALLBACK_ENABLE else "未启用"))
    print(" 日志文件 : %s" % LOG_PATH)
    print("-" * 62)
    print(" config.ini 需要这样写：")
    print("   [tiku]")
    print("   provider = TikuAdapter")
    print("   url = http://%s:%d/" % (HOST, PORT))
    print("-" * 62)
    print(" 怎么看失败原因：")
    print("   “ANEVOL 返回失败 -> code=0 msg=AI服务暂时不可用”  = 服务端故障，不是题库没答案")
    print("   “ANEVOL 熔断中”                                  = 服务端连续挂，暂停 90 秒不再白等")
    print("   “拒绝请求 -> ...密钥/次数不足...”                  = 套餐或密钥的问题，去控制台看")
    print("-" * 62)
    print(" 这个窗口请不要关闭，关闭后 chaoxing 就搜不到题了。")
    print(" 按 Ctrl+C 可停止服务。")
    print("=" * 62)

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.server_close()


if __name__ == "__main__":
    main()
