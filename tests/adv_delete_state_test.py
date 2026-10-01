# -*- coding: utf-8 -*-
"""对抗性回归测试：delete_files -> save_state -> check_pending 全链路

回归对象：2026-10-01 17:22 实跑崩溃
    ValueError('too many values to unpack (expected 2)')
    根因：delete_files 返回路径字符串列表，save_state 按 (path,size) 元组解包。

安全约定：APP_DATA / STATE_FILE 一律劫持到临时目录，
         绝不写真实 %LOCALAPPDATA%\\CS2Fixer\\pending_restore.json。
运行： python adv_delete_state_test.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

SRC = os.environ.get("CS2FIXER_SRC") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cs2_fixer.py")

fails = []
total = [0]


def check(name, cond, detail=""):
    total[0] += 1
    if cond:
        print("  [PASS] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        fails.append(name)


def load_mod():
    spec = importlib.util.spec_from_file_location("cs2_fixer_under_test", SRC)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [SRC]                      # 防止测试脚本自身参数误触发 --check/--dry-run
    spec.loader.exec_module(m)
    return m


def make_files(base, sizes):
    os.makedirs(base, exist_ok=True)
    out = []
    for name, size in sizes.items():
        p = os.path.join(base, name)
        with open(p, "wb") as f:
            f.write(b"\0" * size)
        out.append((p, size))
    return out


def main():
    if not os.path.isfile(SRC):
        print("源码不存在: %s" % SRC)
        return 2

    m = load_mod()
    real_state = m.STATE_FILE

    tmp = tempfile.mkdtemp(prefix="cs2fixer_adv_")
    # —— 劫持路径：测试期间绝无真实文件系统副作用 ——
    m.APP_DATA = tmp
    m.STATE_FILE = os.path.join(tmp, "pending_restore.json")
    assert m.STATE_FILE != real_state, "状态文件劫持失败，中止"
    assert "CS2Fixer" not in m.STATE_FILE or tmp in m.STATE_FILE

    print("[环境] 临时根 = %s" % tmp)
    print("[环境] 真实状态文件未被触碰 = %s" % (not os.path.isfile(real_state)))
    check("真实 pending_restore.json 不存在（起点干净）", not os.path.isfile(real_state))

    silent = lambda s: None

    try:
        # T1 主回归：真实删除 -> save_state 不得抛异常
        print("\nT1 主回归：delete_files 返回值形态 + save_state 可写")
        base = os.path.join(tmp, "game", "core")
        files = make_files(base, {
            "shaders_pc_000.vpk": 3185298,
            "shaders_pc_dir.vpk": 25563,
            "shaders_vulkan_000.vpk": 8684498,
            "shaders_vulkan_dir.vpk": 28185,
        })
        ok, fail = m.delete_files(files, silent)
        check("删除成功 4 个", len(ok) == 4, "got %d" % len(ok))
        check("删除失败 0 个", not fail, str(fail))
        check("ok 元素为 (path,size) 二元组",
              all(isinstance(x, tuple) and len(x) == 2 and isinstance(x[0], str) and isinstance(x[1], int) for x in ok),
              repr(ok[:1]))
        check("文件确实被删掉", not any(os.path.isfile(p) for p, _ in files))
        try:
            m.save_state(ok)                       # 原 bug 在此抛 ValueError
            check("save_state(ok) 未抛异常（核心回归）", True)
        except Exception as e:
            check("save_state(ok) 未抛异常（核心回归）", False, repr(e))

        # T2 落盘结构可被 check_pending 原生读回
        print("\nT2 落盘 JSON 结构")
        st = json.load(open(m.STATE_FILE, encoding="utf-8"))
        check("stage=pending_validate", st.get("stage") == "pending_validate", str(st.get("stage")))
        check("files 为 4 条 dict{path,size}",
              len(st.get("files", [])) == 4 and all(set(f) >= {"path", "size"} for f in st["files"]))
        missing, _ = m.check_pending()
        check("文件全缺 -> missing=4", len(missing) == 4, "got %d" % len(missing))

        # T3 补回后自动清状态
        print("\nT3 补回检测")
        make_files(base, {"shaders_pc_000.vpk": 3185298, "shaders_pc_dir.vpk": 25563})
        missing, _ = m.check_pending()
        check("部分补回 -> missing=2", len(missing) == 2, "got %d" % len(missing))
        make_files(base, {"shaders_vulkan_000.vpk": 8684498, "shaders_vulkan_dir.vpk": 28185})
        missing, _ = m.check_pending()
        check("全部补回 -> missing=0", len(missing) == 0, "got %d" % len(missing))
        check("全部补回后状态文件被清除", not os.path.isfile(m.STATE_FILE))

        # T4 大小不一致不得误判为“已回来”
        print("\nT4 声明与实际不符（大小不同）")
        files = make_files(base, {"shaders_pc_000.vpk": 999})
        ok, _ = m.delete_files(files, silent)
        m.save_state(ok)
        make_files(base, {"shaders_pc_000.vpk": 111})          # 大小对不上
        missing, _ = m.check_pending()
        check("大小不符 -> 仍判缺失", len(missing) == 1, "got %d" % len(missing))
        m.clear_state()

        # T5 删除失败分支：目录冒充文件
        print("\nT5 删除失败分支")
        d = os.path.join(base, "shaders_fake.vpk")
        os.makedirs(d, exist_ok=True)                          # 目录，os.remove 必失败
        bad = [(d, 0)]
        ok2, fail2 = m.delete_files(bad, silent)
        check("失败项进 fail", len(fail2) == 1, str(fail2))
        check("失败项不进 ok", not ok2, str(ok2))
        check("失败项仍存在", os.path.isdir(d))
        try:
            m.save_state(ok2)                                  # 空列表也必须能写
            check("save_state([]) 可写", True)
        except Exception as e:
            check("save_state([]) 可写", False, repr(e))
        m.clear_state()

        # T6 DRY_RUN：返回同样形态且不真删
        print("\nT6 DRY_RUN 形态一致")
        old = m.DRY_RUN
        try:
            m.DRY_RUN = True
            f2 = make_files(os.path.join(tmp, "dry"), {"a.vpk": 10, "b.vpk": 20})
            okd, faild = m.delete_files(f2, silent)
            check("dry-run 返回 2 个二元组", len(okd) == 2 and all(isinstance(x, tuple) and len(x) == 2 for x in okd))
            check("dry-run 不真删", all(os.path.isfile(p) for p, _ in f2))
            m.save_state(okd)
            check("dry-run 结果可被 save_state 接受", True)
            m.clear_state()
        finally:
            m.DRY_RUN = old

        # T7 进程级兜底：真实入口函数签名一致（防回归时改动漏改调用方）
        print("\nT7 调用方一致性")
        import inspect
        src_all = open(SRC, encoding="utf-8").read()
        check("源码无遗留 for p, s in shader_files ... if deleted 混用",
              "for p, s in shader_files] if deleted" not in src_all)
        check("_run_fix 仍以 deleted 作为 expect 源", "expect = list(deleted)" in src_all)
        check("delete_files 文档串声明返回值形态", "返回 (成功删除的" in src_all)

        # T8 端到端（沙盒）：复刻用户 10-01 实际走的那条链路
        #    删文件 -> 写待恢复清单 -> ③ 触发 Steam 验证
        print("\nT8 端到端 _run_fix（沙盒：真删临时文件、不碰真实 Steam）")
        import queue as _q
        import threading as _th
        m.APP_DATA = tmp
        m.STATE_FILE = os.path.join(tmp, "pending_restore.json")
        m.REMINDER_FILE = os.path.join(tmp, "reminder.json")
        m.LOG_DIR = os.path.join(tmp, "logs")
        base2 = os.path.join(tmp, "e2e", "game", "core")
        files2 = make_files(base2, {"shaders_pc_000.vpk": 3185298, "shaders_pc_dir.vpk": 25563})

        logs = []
        app = m.App.__new__(m.App)                  # 绕过 Tk 窗口
        app.log = lambda s: logs.append(s)
        app.ui_q = _q.Queue()
        app.steam = os.path.join(tmp, "steam")
        app.stop_event = _th.Event()
        m.is_cs2_running = lambda: False
        m.trigger_validate = lambda: True
        m.watch_validation = lambda *a, **k: None
        m.read_buildid = lambda: "25515854"

        app._run_fix(True, files2, True, [])
        joined = "\n".join(logs)
        check("日志走到 '③ 触发 Steam 验证'", "③ 触发 Steam 验证" in joined, joined)
        check("日志无 '未预期的错误'（原崩溃点已跨过）", "未预期的错误" not in joined, joined)
        check("2 个文件确已删除", not any(os.path.isfile(p) for p, _ in files2))
        check("待恢复清单已落盘", os.path.isfile(m.STATE_FILE))
        missing, _ = m.check_pending()
        check("清单内 2 个文件被识别为缺失", len(missing) == 2, "got %d" % len(missing))
        m.clear_state()

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        check("临时目录已清理", not os.path.isdir(tmp))
        check("全程未写真实状态文件", not os.path.isfile(real_state))

    print("\n==== 结果：%d 项断言，失败 %d 项 ====" % (total[0], len(fails)))
    if fails:
        for f in fails:
            print("  !! %s" % f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
