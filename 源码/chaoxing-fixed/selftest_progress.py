# -*- coding: utf-8 -*-
"""
课程进度/分数 解析自测（离线）
==============================
夹具结构抄自真实进度页（用户 2026-09-14 导出的《人工智能与现代农林业》），
不联网就能验证解析逻辑。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.progress import parse_progress_html, format_progress

# ---- 真实结构：有分数（92.03 / 30.0 / 26.03 / 36.0），进度 100% ----
NORMAL = """
<div class="MainJd">
  <div style="height:40px;display: none;" id="customModeLabel">
    <h4>教师采用自定义权重模式评分，请切换至新版平台查看成绩</h4></div>
  <div style="height:40px" id="jdLabel"><h3 class="jdTitle"><i></i>考核标准</h3></div>
  <table cellpadding="0" cellspacing="0" id="weightTable"><tbody>
    <tr><td width="150" align="right">章节任务点（30%）：</td><td align="left">课程视频/音频全部完成得满分</td></tr>
    <tr><td width="130" align="right">考试（40%）：</td><td align="left">所有考试的平均分</td></tr>
  </tbody></table>
  <table cellpadding="0" cellspacing="0" width="100%" class="commonTable"><thead>
    <tr><th width="15%">考核内容</th><th width="7%">视频（30%）</th>
        <th width="10%">章节测验（30%）</th><th width="7%">考试（40%）</th></tr>
  </thead><tbody><tr>
    <th>当前分数 （
        <span style="color:#f00;">92.03</span>
    ）</th>
    <td>30.0</td>
    <td width="15%">26.03</td>
    <td>36.0</td>
  </tr></tbody></table>
  <div id="myprocess" style="width: 650px;">
    <div class="myposition" style="...">
      <span style="...;color: #f00;...">我的进度100%</span>
    </div>
  </div>
</div>
"""

# ---- 自定义权重模式：提示块显示（没有 display:none），且拿不到分数 ----
CUSTOM = """
<div class="MainJd">
  <div style="height:40px" id="customModeLabel">
    <h4 style="font-size: 14px;padding: 13px 0;color: #666;">教师采用自定义权重模式评分，请切换至新版平台查看成绩</h4></div>
  <div id="myprocess"><span style="color:#f00;">我的进度63%</span></div>
</div>
"""

# ---- 没做完的普通课 ----
PARTIAL = NORMAL.replace("92.03", "71.50").replace("30.0</td>", "21.0</td>") \
                .replace("26.03", "27.00").replace(">36.0<", ">23.5<") \
                .replace("我的进度100%", "我的进度 63 %")


def report(name, ok, extra=""):
    print(" [{}] {}{}".format("PASS" if ok else "FAIL", name, extra))
    return ok


def main():
    results = []
    print("=" * 92)
    print(" 课程进度/分数 解析自测（离线）")
    print("=" * 92)

    p = parse_progress_html(NORMAL)
    ok = (p["percent"] == 100 and p["score"] == 92.03 and p["video"] == 30.0
          and p["quiz"] == 26.03 and p["exam"] == 36.0 and p["custom_weight"] is False)
    results.append(report("解析完整进度页（进度+总分+三项分）", ok,
                          "  {}".format({k: p[k] for k in ("percent", "score", "video", "quiz", "exam")})))

    ok = format_progress(p) == "已完成100% 92.03分"
    results.append(report("格式化输出（全部完成）", ok, "  -> {!r}".format(format_progress(p))))

    p2 = parse_progress_html(PARTIAL)
    ok = (p2["percent"] == 63 and p2["score"] == 71.50 and p2["video"] == 21.0
          and p2["quiz"] == 27.00 and p2["exam"] == 23.5)
    results.append(report("解析未完成课程（进度 63%）", ok,
                          "  -> {}".format(format_progress(p2))))
    ok = format_progress(p2) == "进行中63% 71.50分"
    results.append(report("格式化输出（未完成 → 显示「进行中」）", ok,
                          "  -> {!r}".format(format_progress(p2))))

    p3 = parse_progress_html(CUSTOM)
    ok = (p3["percent"] == 63 and p3["score"] is None and p3["custom_weight"] is True)
    results.append(report("自定义权重模式：识别出提示块、不误报分数", ok,
                          "  custom_weight={} score={} -> {!r}".format(
                              p3["custom_weight"], p3["score"], format_progress(p3))))

    # 空页 / 垃圾输入不能崩
    for junk in ("", "<html></html>", None):
        try:
            r = parse_progress_html(junk)
            assert r["percent"] is None and r["score"] is None
        except Exception as e:  # noqa: BLE001
            results.append(report("空输入不崩", False, "  {}: {}".format(type(e).__name__, e)))
            break
    else:
        results.append(report("空输入/垃圾输入不崩，返回空结果", True))

    # 不能把别的数字误当分数（页面上有 30%、40% 这类权重数字）
    p4 = parse_progress_html(NORMAL.replace("当前分数", "考核内容"))
    ok = p4["score"] is None
    results.append(report("没有「当前分数」时不误抓页面上的其他数字", ok,
                          "  score={}".format(p4["score"])))

    print("-" * 92)
    print(" 结果: {} passed, {} failed".format(sum(results), len(results) - sum(results)))
    print("=" * 92)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
