# -*- coding: utf-8 -*-
"""
离线验证 PyInstaller onefile exe 里到底装了什么（不运行 exe、不联网、不碰超星账号）。
做法：在 exe 里定位内嵌的 PYZ 归档 -> 解出指定模块的字节码 -> 递归搜索符号名。

用法：
    python verify_exe_modules.py <exe路径> [模块名]
"""
import io
import marshal
import struct
import sys
import zlib

MAGIC = b"PYZ\0"


def collect_names(code, out):
    out.update(code.co_names)
    for c in code.co_consts:
        if hasattr(c, "co_names"):
            collect_names(c, out)
        elif isinstance(c, str):
            out.add(c)
    return out


def find_pyz(data):
    """返回 (pyz起始偏移, TOC列表)。"""
    pos = data.find(MAGIC)
    while pos != -1:
        try:
            toc_offset = struct.unpack("!i", data[pos + 8:pos + 12])[0]
            toc = marshal.loads(data[pos + toc_offset:])
        except Exception:
            pos = data.find(MAGIC, pos + 1)
            continue
        if isinstance(toc, list) and toc and isinstance(toc[0], tuple):
            return pos, toc
        pos = data.find(MAGIC, pos + 1)
    return None, None


def main(exe_path, module="api.answer", needles=()):
    data = open(exe_path, "rb").read()
    print("exe: {}".format(exe_path))
    print("     大小 {:,} 字节, 修改时间 {}".format(
        len(data), __import__("time").strftime(
            "%Y-%m-%d %H:%M:%S", __import__("time").localtime(__import__("os").path.getmtime(exe_path)))))

    pos, toc = find_pyz(data)
    if pos is None:
        print("!! 没找到内嵌 PYZ")
        return 3
    print("     内嵌 PYZ 位于偏移 {}，共 {} 个模块".format(pos, len(toc)))

    entry = None
    for name, info in toc:
        if name == module:
            entry = info
            break
    if entry is None:
        print("!! PYZ 里没有模块 {}".format(module))
        return 4

    typecode, offset, length = entry
    raw = data[pos + offset: pos + offset + length]
    code = marshal.loads(zlib.decompress(raw))
    names = collect_names(code, set())
    print("     模块 {}: 顶层符号 {} 个".format(module, len(names)))

    # f-string 会被拆成「带前缀/后缀的片段常量」，所以除了精确匹配再做一次子串匹配
    blob = "\n".join(names)
    bad = 0
    for n in needles:
        hit = (n in names) or (n in blob)
        bad += (not hit)
        print("     [{}] {}".format("OK  " if hit else "缺失", n))
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    exe = sys.argv[1]
    mod = sys.argv[2] if len(sys.argv) > 2 else "api.answer"
    extra = sys.argv[3:]
    sys.exit(main(
        exe, module=mod,
        needles=extra or ["TikuIcodef", "TikuChain", "TikuAnevol", "_AnswerNormalizeMixin",
                          "_normalize_text_answer", "_split_options",
                          "_AI_CB_THRESHOLD", "_AI_FATAL_WORDS"],
    ))
