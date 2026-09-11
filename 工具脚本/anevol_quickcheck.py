# -*- coding: utf-8 -*-
import configparser, time, requests
from urllib3 import disable_warnings, exceptions
disable_warnings(exceptions.InsecureRequestWarning)
import sys
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception: pass
cfg = configparser.ConfigParser(); cfg.read(r"E:/system/Desktop/config.ini", encoding="utf8")
tok = (cfg["tiku"].get("tokens") or "").strip().split(",")[0].strip()
URL = "https://tiku.anevol.cn/api/search"
p = {"title":"下列哪个数是质数？","options":"A. 4\nB. 5\nC. 6\nD. 8","type":"single"}
t0=time.time()
r = requests.post(URL, params={"token":tok}, json=p, timeout=(5,25), verify=False)
print("HTTP", r.status_code, "耗时 %.1fs" % (time.time()-t0))
print(r.text.strip()[:400])
