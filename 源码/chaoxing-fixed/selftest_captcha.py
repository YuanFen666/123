# -*- coding: utf-8 -*-
"""
滑块验证码兜底逻辑自测（离线，不联网）
======================================
背景（2026-09-15 实测）：
  · chaoxing 背景图里有**干扰项** —— 真实缺口和干扰项形状一样，只有明暗/清晰度不同，
    纯模板匹配会偶尔跑偏（用户日志里连续 3 次全挂）。
  · **同一张验证码只能校验一次**：第二次提交返回
    {'error': 1, 'msg': 'verification error...'} —— 所以每失败一次必须换新图。
  · 因此把 CAPTCHA_MAX_TRY 从 3 提到 8；实测连续 4 轮全部通过（4/4）。

本自测验证「兜底路径」本身不会帮倒忙：无人值守时必须明确报错，
**绝不能静默卡在等输入上**（那会让整个刷课/考试流程挂死）。
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api.exam_take as E


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


class FakeCaptcha(E.SlideCaptcha):
    """不打网络的替身"""
    def __init__(self, images=(None, None), check_raises=True, width=320):
        self._last_bg_width = width
        self._images = images
        self._check_raises = check_raises
        self.checked = []

    def _get_images(self):
        return self._images

    def _check(self, x):
        self.checked.append(x)
        if self._check_raises:
            raise RuntimeError("验证码校验未通过")
        return "validate_FAKE"


# 一张能存成 png 的最小图
def _tiny_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (320, 160), (200, 100, 100)).save(buf, format="PNG")
    return buf.getvalue()


def main():
    res = []
    print("=" * 92)
    print(" 滑块验证码兜底 自测")
    print("=" * 92)

    # 1) 常量就在合理区间，且定义在类之前（否则类定义时就 NameError）
    res.append(report("CAPTCHA_MAX_TRY >= 5（单次成功率不高，靠重试兜）",
                      E.CAPTCHA_MAX_TRY >= 5, "  = {}".format(E.CAPTCHA_MAX_TRY)))
    import inspect
    sig = inspect.signature(E.SlideCaptcha.solve)
    res.append(report("solve() 默认用的就是 CAPTCHA_MAX_TRY",
                      sig.parameters["max_try"].default == E.CAPTCHA_MAX_TRY,
                      "  默认 = {}".format(sig.parameters["max_try"].default)))

    # 2) 失败存图：能落盘、路径可用
    c = FakeCaptcha(images=(_tiny_png(), b"x"))
    p = c._save_fail_image(_tiny_png(), "_selftest")
    ok = bool(p) and os.path.exists(p) and p.lower().endswith(".png")
    res.append(report("失败时能把背景图存下来（复盘/人工兜底用）", ok, "  -> {}".format(p)))
    if ok:
        try:
            os.remove(p)
        except OSError:
            pass
    # 图是坏的也不能抛异常炸掉流程
    bad = c._save_fail_image(b"not-an-image", "_selftest_bad")
    res.append(report("存图失败时静默返回空串，不抛异常", bad == "", "  -> {!r}".format(bad)))

    # 3) 非交互（无人值守）→ 必须明确报错，不能卡住
    real_stdin, real_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO("")     # 没有 isatty → 走非交互分支
        sys.stdout = io.StringIO()
        c2 = FakeCaptcha(images=(None, None))
        t0 = time.time()
        try:
            c2._manual_fallback(RuntimeError("8 次全挂"), 8)
            ok, msg = False, "居然没报错"
        except RuntimeError as e:
            ok, msg = True, str(e)
        dt = time.time() - t0
        res.append(report("无人值守时不等待输入、明确报错", ok and dt < 3,
                          "  {:.2f}s  {}".format(dt, msg.replace("\n", " ")[:70])))
        res.append(report("报错信息里说清了「重跑通常能碰上好认的图」",
                          "重跑" in msg or "非交互" in msg))
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout

    # 4) 交互式：模拟用户输入正确坐标 → 应当通过
    class TTY(io.StringIO):
        def isatty(self):
            return True
    try:
        sys.stdin, sys.stdout = TTY("137\n"), TTY()
        c3 = FakeCaptcha(images=(_tiny_png(), b"x"), check_raises=False)
        v = c3._manual_fallback(RuntimeError("8 次全挂"), 8)
        res.append(report("交互式下人工输入 x 能走通并返回 validate",
                          v == "validate_FAKE" and c3.checked == [137],
                          "  输入被校验的位置 = {}".format(c3.checked)))
    except Exception as e:  # noqa: BLE001
        res.append(report("交互式下人工输入 x 能走通并返回 validate", False, "  " + repr(e)))
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout

    # 5) 交互式下用户直接回车 = 放弃，也必须干净报错
    try:
        sys.stdin, sys.stdout = TTY("\n"), TTY()
        c4 = FakeCaptcha(images=(_tiny_png(), b"x"))
        try:
            c4._manual_fallback(RuntimeError("x"), 8)
            res.append(report("交互式下回车=放弃，并干净报错", False, "  没报错"))
        except RuntimeError as e:
            res.append(report("交互式下回车=放弃，并干净报错",
                              "放弃" in str(e) and c4.checked == []))
    except Exception as e:  # noqa: BLE001
        res.append(report("交互式下回车=放弃，并干净报错", False, "  " + repr(e)))
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout

    print("-" * 92)
    print(" 结果: {} passed, {} failed".format(sum(res), len(res) - sum(res)))
    print("=" * 92)
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
