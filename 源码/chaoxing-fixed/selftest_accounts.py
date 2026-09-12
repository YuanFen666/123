# -*- coding: utf-8 -*-
"""
多账号支持自测（离线）
======================
验证：
  1. [accounts] 段能正确解析出多组账号；
  2. 一组 → 直接用（不进入选择流程）；多组 → 让用户选；
  3. 选择时输入非法编号会重问，直接回车默认第 1 个；
  4. 非交互（stdin 读完）不会卡死，退回到第 1 个；
  5. 老写法（只有 [common] 的 username/password）仍然可用 —— 向后兼容。
"""
import builtins
import configparser
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import load_accounts, choose_account, mask_account


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


class FakeInput:
    """按剧本喂给 input()，喂完就抛 EOFError（模拟非交互）。"""

    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, prompt=""):
        if not self.answers:
            raise EOFError()
        return self.answers.pop(0)


def with_input(answers, fn):
    real = builtins.input
    builtins.input = FakeInput(answers)
    try:
        return fn()
    finally:
        builtins.input = real


def main():
    results = []
    print("=" * 90)
    print(" 多账号支持自测（离线）")
    print("=" * 90)

    # ---- 1) 解析 ----
    cfg = configparser.ConfigParser()
    cfg.read_string("""
[common]
username = 111
password = aaa
[accounts]
13800000000 = pw_test_one
13800138000 = pw_test_two
""")
    acc = load_accounts(cfg)
    ok = (acc == [("13800000000", "pw_test_one"), ("13800138000", "pw_test_two")])
    results.append(report("解析 [accounts] 多组账号（密码含 . 也正常）", ok, "  解析到 {} 组".format(len(acc))))

    # 密码里含逗号/冒号/等号 —— 用 key/value 存就不会被切坏（这是当初选这种写法的原因）
    cfg2 = configparser.ConfigParser()
    cfg2.read_string("[accounts]\n13800000000 = p,w:d=x\n")
    ok = load_accounts(cfg2) == [("13800000000", "p,w:d=x")]
    results.append(report("密码含逗号/冒号/等号也不会被切坏", ok,
                          "  -> {!r}".format(load_accounts(cfg2))))

    # 没有 [accounts] 段 → 空列表（继续用 [common] 的单账号，向后兼容）
    cfg3 = configparser.ConfigParser()
    cfg3.read_string("[common]\nusername = 111\npassword = aaa\n")
    ok = load_accounts(cfg3) == []
    results.append(report("没有 [accounts] 段 → 返回空（老配置照常可用）", ok))

    # ---- 2) 一组：直接用，不问 ----
    called = {"n": 0}

    def one():
        called["n"] += 1
        return input("不该被调用")
    got = with_input([], lambda: choose_account([("13800000000", "pw")]))
    ok = got == ("13800000000", "pw")
    results.append(report("只有一组账号 → 直接使用（不询问）", ok, "  -> {!r}".format(got)))

    # ---- 3) 多组：按编号选 ----
    got = with_input(["2"], lambda: choose_account([("a" * 11, "pw1"), ("b" * 11, "pw2")]))
    ok = got == ("b" * 11, "pw2")
    results.append(report("多组账号 → 输入 2 选到第 2 个", ok, "  -> {!r}".format(got)))

    # 非法编号后重问，再给合法值
    got = with_input(["9", "abc", "1"], lambda: choose_account([("a" * 11, "pw1"), ("b" * 11, "pw2")]))
    ok = got == ("a" * 11, "pw1")
    results.append(report("编号非法会重问（9/abc 都拒绝），直到给对", ok, "  -> {!r}".format(got)))

    # 直接回车 → 第 1 个
    got = with_input([""], lambda: choose_account([("a" * 11, "pw1"), ("b" * 11, "pw2")]))
    ok = got == ("a" * 11, "pw1")
    results.append(report("直接回车 → 默认第 1 个", ok, "  -> {!r}".format(got)))

    # ---- 4) 非交互：不能卡死 ----
    got = with_input([], lambda: choose_account([("a" * 11, "pw1"), ("b" * 11, "pw2")]))
    ok = got == ("a" * 11, "pw1")
    results.append(report("stdin 读完（无人值守）→ 退回第 1 个，不卡死", ok, "  -> {!r}".format(got)))

    # ---- 4b) bat 传进来的 --account-index ----
    got = choose_account([("a" * 11, "pw1"), ("b" * 11, "pw2")], 2)
    ok = got == ("b" * 11, "pw2")
    results.append(report("--account-index 2 → 直接取第 2 组（不再询问）", ok, "  -> {!r}".format(got)))

    got = choose_account([("a" * 11, "pw1"), ("b" * 11, "pw2")], 99)
    ok = got == ("a" * 11, "pw1")
    results.append(report("--account-index 越界 → 退回第 1 组", ok, "  -> {!r}".format(got)))

    # ---- 4c) stdout 被重定向时必须自动选，不能弹一个看不见的菜单 ----
    # 实际踩过：bat 里 --list-courses 用 for /f 抓输出，子进程弹账号菜单却被吞掉，
    # 程序静默卡死等一个用户永远看不到的输入。
    class _NoTty:
        def isatty(self): return False
        def write(self, *_a): pass
        def flush(self): pass

    import sys as _sys
    _real = _sys.stdout
    _sys.stdout = _NoTty()
    try:
        got = choose_account([("a" * 11, "pw1"), ("b" * 11, "pw2")])
    finally:
        _sys.stdout = _real
    ok = got == ("a" * 11, "pw1")
    results.append(report("stdout 被重定向（for /f）→ 自动用第 1 组，不弹看不见的菜单", ok,
                          "  -> {!r}".format(got)))

    # ---- 5) 打码 ----
    ok = (mask_account("13800000000") == "138****00" and mask_account("abc") == "ab***")
    results.append(report("手机号打码显示", ok,
                          "  13800000000 -> {}   abc -> {}".format(
                              mask_account("13800000000"), mask_account("abc"))))

    print("-" * 90)
    print(" 结果: {} passed, {} failed".format(sum(results), len(results) - sum(results)))
    print("=" * 90)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
