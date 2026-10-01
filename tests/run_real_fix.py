# -*- coding: utf-8 -*-
"""真实全流程实跑驱动（无 GUI 外壳，执行内核与双击运行完全一致）

用途：验证 删除着色器 → 写待恢复清单 → 触发 Steam 验证 → 等待补回 全链路。
      （v1.0.0 的崩溃点在第 2 步，导致第 3 步从未执行。）

用法：
  python tests/run_real_fix.py precheck   只探测，不动任何文件
  python tests/run_real_fix.py run        真实执行（会删文件并触发 Steam 验证）
"""
import importlib.util
import os
import queue
import sys
import threading
import time

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cs2_fixer.py")


def load_mod():
    spec = importlib.util.spec_from_file_location("cs2_fixer_real", SRC)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [SRC]                     # 清掉外部参数，确保 DRY_RUN / CHECK_MODE 均为 False
    spec.loader.exec_module(m)
    return m


def probe(m):
    steam = m.find_steam_path()
    libs = m.parse_libraries(steam) if steam else []
    lib, cs2 = m.find_cs2(libs) if libs else (None, None)
    files = m.list_shader_files(cs2) if cs2 else []
    caches = m.get_cache_targets(lib) if lib else (m.get_cache_targets(None) if cs2 else [])
    return steam, lib, cs2, files, caches


def main():
    m = load_mod()
    steam, lib, cs2, files, caches = probe(m)

    print("=" * 66)
    print("Steam      : %s" % steam)
    print("库(CS2所在): %s" % lib)
    print("CS2 目录   : %s" % cs2)
    print("CS2 运行中 : %s" % m.is_cs2_running())
    print("-" * 66)
    print("待删着色器文件（%d 个）:" % len(files))
    total = 0
    for p, s in files:
        total += s
        print("   %10d  %s" % (s, os.path.basename(p)))
    print("   合计 %s" % m.fmt_size(total))
    print("-" * 66)
    print("缓存清理项（%d 个）:" % len(caches))
    for name, path, _ in caches:
        print("   %-26s %8s  %s" % (name, m.fmt_size(m.dir_size(path)), path))
    print("=" * 66)

    if len(sys.argv) > 1 and sys.argv[1] == "precheck":
        print("[precheck] 仅探测，未改动任何文件。")
        return 0

    if not files and not caches:
        print("没有可执行项，退出。")
        return 1
    if m.is_cs2_running():
        print("!! CS2 正在运行，中止（请先退出游戏）。")
        return 1

    print("\n[3 秒后开始真实执行，期间可 Ctrl+C 中止]")
    time.sleep(3)

    app = m.App.__new__(m.App)           # 绕过 Tk 窗口，其余全用真实实现
    app.steam = steam
    app.cs2 = cs2
    app.cache_targets = caches
    app.stop_event = threading.Event()
    app.ui_q = queue.Queue()
    app._setup_logfile()
    print("本次日志文件: %s\n" % app.logf.name)

    def pump():
        while True:
            kind, payload = app.ui_q.get()
            if kind == "log":
                print(payload, flush=True)
            elif kind == "done":
                print(">>> [UI 事件] 完成弹窗（真实 GUI 下会弹「修复完成」）", flush=True)
            elif kind == "timeout":
                print(">>> [UI 事件] 超时提示", flush=True)
            elif kind == "error":
                print(">>> [UI 事件] 错误弹窗：%s" % payload, flush=True)

    threading.Thread(target=pump, daemon=True).start()

    t0 = time.time()
    app._run_fix(True, files, True, [(n, p) for n, p, _ in caches])
    elapsed = time.time() - t0

    time.sleep(0.5)
    try:
        app.logf.close()
    except OSError:
        pass
    print("\n[实跑结束] 耗时 %.1f 秒" % elapsed)
    print("[待恢复清单] %s" % ("仍存在（说明尚未补回）" if os.path.isfile(m.STATE_FILE) else "已清除（文件已补回）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
