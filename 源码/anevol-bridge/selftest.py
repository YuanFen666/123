# -*- coding: utf-8 -*-
"""
自测脚本：验证 anevol_bridge 的输出能被 chaoxing v3.1.4 的解析逻辑正确还原成答案。
解析逻辑从 api/answer_check.py 和 api/base.py 里原样抄来（连同 TikuAdapter._query 的解析）。
"""
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "anevol_bridge.py"
PORT = 8787

# ---------------------------------------------------------------------------
# ===== 以下从 chaoxing v3.1.4 源码原样抄写，勿改 =====
def cut(answer):
    cut_char = ["\n", ",", "，", "|", "\r", "\t", "#", "*", "-", "_", "+", "@", "~", "/", "\\", ".", "&", " ", "、"]
    if answer is None:
        return None
    answer = str(answer)
    for char in cut_char:
        if char not in answer:
            continue
        res = [opt.strip() for opt in answer.split(char) if opt.strip()]
        if res:
            return res
    stripped = answer.strip()
    return [stripped] if stripped else None


def check_judgement(answer, true_list, false_list):
    if answer in true_list:
        return 1
    elif answer in false_list:
        return 0
    return -1


def check_answer(answer, type, true_list, false_list):
    if type == "single":
        _t = cut(answer)
        if _t is not None and len(_t) == 1 and check_judgement(answer, true_list, false_list) == -1:
            return True
    elif type == "multiple":
        _t = cut(answer)
        if _t is not None and len(_t) > 0 and check_judgement(answer, true_list, false_list) == -1:
            return True
    elif type == "completion":
        if len(answer) > 0:
            return True
    elif type == "judgement":
        if check_judgement(answer, true_list, false_list) != -1:
            return True
    else:
        return True
    return False


def multi_cut(answer):
    return cut(answer)


def clean_res(res):
    cleaned_res = []
    if isinstance(res, str):
        res = [res]
    for c in res:
        cleaned = re.sub(r"^[A-Za-z]|[.,!?;:，。！？；：]", "", c)
        cleaned_res.append(cleaned.strip())
    return cleaned_res


def is_subsequence(a, o):
    iter_o = iter(o)
    return all(c in iter_o for c in a)


def adapter_parse(res_json):
    """TikuAdapter._query 的返回解析"""
    if not len(res_json["answer"]["bestAnswer"]):
        return None
    sep = "\n"
    return sep.join(res_json["answer"]["bestAnswer"]).strip()


def project_map(res, q, true_list, false_list):
    """base.py 764-824 行：把题库答案映射成最终提交的答案"""
    answer = ""
    if not res:
        return "RANDOM"
    if check_answer(res, q["type"], true_list, false_list) is not True:
        return "REJECTED"
    if q["type"] == "multiple":
        options_list = multi_cut(q["options"])
        res_list = multi_cut(res)
        if res_list is not None and options_list is not None:
            for _a in clean_res(res_list):
                for o in options_list:
                    if is_subsequence(_a, o):
                        answer += o[:1]
            answer = "".join(sorted(answer))
    elif q["type"] == "single":
        options_list = multi_cut(q["options"])
        if options_list is not None:
            t_res = clean_res(res)
            for o in options_list:
                if is_subsequence(t_res[0], o):
                    answer = o[:1]
                    break
    elif q["type"] == "judgement":
        answer = "true" if check_judgement(res, true_list, false_list) == 1 else "false"
    elif q["type"] == "completion":
        answer = res
    else:
        answer = res
    return answer or "RANDOM"
# ===== 抄写结束 =================================================================


TRUE_LIST = ["正确", "对", "√", "是"]
FALSE_LIST = ["错误", "错", "×", "否", "不对", "不正确"]


def adapter_style_request(question, options_str, type_int):
    """按 TikuAdapter 的请求风格调用桥接（options 是去掉字母后的列表）"""
    opts = [re.sub(r"^[A-Za-z]\.?、?\s?", "", o) for o in options_str.split("\n")]
    payload = {"question": question, "options": opts, "type": type_int}
    req = urllib.request.Request(
        "http://127.0.0.1:%d/" % PORT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


CASES = [
    # (题型, 题干, 超星里的选项, TikuAdapter type, 期望最终答案)
    ("single", "中国的首都是", "A.北京\nB.上海\nC.广州\nD.深圳", 0, "A"),
    ("single", "2+2等于几", "A.3\nB.4\nC.5\nD.6", 0, "B"),
    ("multiple", "以下哪些是中国的直辖市", "A.北京\nB.上海\nC.广州\nD.天津", 1, "ABD"),
    ("judgement", "地球是圆的", "A.正确\nB.错误", 3, "true"),
    ("judgement", "地球是太阳系中最大的行星", "A.正确\nB.错误", 3, "false"),
    ("completion", "中国的首都是____", "", 2, "北京"),
]


def main():
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    # 等端口就绪
    ok = False
    for _ in range(40):
        time.sleep(0.3)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=2) as r:
                if r.status == 200:
                    ok = True
                    break
        except Exception:
            continue
    if not ok:
        proc.kill()
        print("桥接服务启动失败")
        return 1

    print("\n%-11s %-26s %-8s %-8s %s" % ("题型", "题干", "期望", "实际", "结果"))
    print("-" * 72)
    all_pass = True
    for qtype, title, options, type_int, expect in CASES:
        res_json = adapter_style_request(title, options, type_int)
        res = adapter_parse(res_json)
        q = {"type": qtype, "options": options, "title": title}
        if qtype == "completion":
            # completion 走 base.py 的 else 分支直接用 res
            got = res if res else "RANDOM"
        else:
            got = project_map(res, q, TRUE_LIST, FALSE_LIST)
        flag = "PASS" if got == expect else "FAIL"
        if flag == "FAIL":
            all_pass = False
        print("%-11s %-26s %-8s %-8s %s  (bestAnswer=%s)" % (qtype, title[:24], expect, got, flag, res_json["answer"]["bestAnswer"]))

    print("-" * 72)
    print("全部通过" if all_pass else "存在失败用例")
    proc.kill()
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
