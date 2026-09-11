# -*- coding: utf-8 -*-
"""按"每次运行"统计 chaoxing.log 里同时在播的视频数。"""
import io
import re
import sys
import datetime
from collections import defaultdict

LOG = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\11977\chaoxing.log"
ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
vid_re = re.compile(r"api\.base:study_video:(\d+) - (开始任务|任务完成): (.+?)(?:\.mp4)?(?:,|$)")
login_re = re.compile(r"api\.base:login:\d+ - 登录成功")

runs, cur = [], None
for line in io.open(LOG, encoding="utf-8", errors="replace"):
    m = ts_re.match(line)
    if not m:
        continue
    t = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    if login_re.search(line):
        cur = {"start": t, "events": [], "ver": set()}
        runs.append(cur)
        continue
    if cur is None:
        continue
    vm = vid_re.search(line)
    if vm:
        ln, kind, name = int(vm.group(1)), vm.group(2), vm.group(3).strip()
        cur["ver"].add("修复版" if ln in (488, 547) else "官方原版")
        cur["events"].append((t, kind, name))

print("共识别到 %d 次运行（按「登录成功」切分）\n" % len(runs))
for i, r in enumerate(runs, 1):
    if not r["events"]:
        continue
    active, samples, peak = set(), [], 0
    for t, kind, name in r["events"]:
        if kind == "开始任务":
            active.add(name)
            peak = max(peak, len(active))
        else:
            active.discard(name)
        samples.append((t, len(active)))
    dist = defaultdict(float)
    for k in range(1, len(samples)):
        dt = (samples[k][0] - samples[k - 1][0]).total_seconds()
        if 0 <= dt < 3600:
            dist[samples[k - 1][1]] += dt
    tot = sum(dist.values()) or 1
    end = samples[-1][0]
    print("=" * 74)
    print(" 第%d次运行  %s → %s   时长 %.0f 分钟" % (i, r["start"], end, (end - r["start"]).total_seconds() / 60))
    print(" exe: %s   视频事件 %d 条   峰值并发 %d" % ("/".join(sorted(r["ver"])) or "?", len(r["events"]), peak))
    print(" 并发分布: " + "   ".join("%d个=%.0f分(%.0f%%)" % (k, dist[k] / 60, dist[k] / tot * 100) for k in sorted(dist)))
