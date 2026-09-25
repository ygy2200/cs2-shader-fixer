# -*- coding: utf-8 -*-
"""
CS2 掉帧修复工具 (CS2Fixer) v1.0.0
把小黑盒帖《CS2更新后爆卡,掉帧严重的罪魁祸首找到了》里的手动操作做成一键修复。

流程：检查游戏进程 → 删 game\\core\\shaders_* → 清显卡/系统着色器缓存
      → 触发 Steam 验证完整性 → 自动等待文件补回。

仅使用 Python 标准库。命令行 --dry-run 可无副作用测试。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import queue
import ctypes
import datetime
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import winreg

APP_ID = 730
APP_TITLE = "CS2 掉帧修复工具"
APP_VERSION = "v1.0.0"
CS2_DIR_NAME = "Counter-Strike Global Offensive"
CS2_EXE = "cs2.exe"
WATCH_INTERVAL = 3          # 完成检测轮询间隔（秒）
WATCH_TIMEOUT = 30 * 60     # 完成检测超时（秒）
SEEK_RUNNING_GRACE = 180    # 等待"看到验证开始"的宽限（秒）

APP_DATA = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CS2Fixer")
LOG_DIR = os.path.join(APP_DATA, "logs")
STATE_FILE = os.path.join(APP_DATA, "pending_restore.json")
REMINDER_FILE = os.path.join(APP_DATA, "reminder.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "CS2Fixer"

DRY_RUN = "--dry-run" in sys.argv
CHECK_MODE = "--check" in sys.argv

# CS2 官方 UI 配色（深灰 + 橙黄强调）
C_BG = "#1b1e23"      # 窗口底色
C_BG2 = "#121417"     # 日志区更深
C_FG = "#d8d9db"      # 正文浅灰
C_FG_DIM = "#8a8f96"  # 次要文字
C_ACCENT = "#de9b35"  # CS2 橙黄
C_LINE = "#3a3f46"    # 边框

# ---------------------------------------------------------------- 基础工具

def fmt_size(n):
    if n is None or n < 0:
        return "?"
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


def is_cs2_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % CS2_EXE, "/NH"],
            capture_output=True, timeout=10).stdout.decode("gbk", "replace")
        return CS2_EXE.lower() in out.lower()
    except Exception:
        return False


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_steam_path():
    """注册表 SteamPath → 默认路径 → None。返回形如 D:\\y 的原生路径。"""
    for hive, view in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(hive, r"Software\Valve\Steam", 0, winreg.KEY_READ | view) as k:
                val, _ = winreg.QueryValueEx(k, "SteamPath")
                p = os.path.normpath(val)
                if os.path.isdir(p):
                    return p
        except OSError:
            pass
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(p):
            return p
    return None


def parse_libraries(steam_path):
    """libraryfolders.vdf 里的所有库路径（含 Steam 根库）。"""
    libs = [steam_path]
    vdf = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    try:
        text = open(vdf, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        text = ""
    for m in re.finditer(r'"path"\s+"([^"]+)"', text):
        p = os.path.normpath(m.group(1).replace("\\\\", "\\"))
        if p not in libs:
            libs.append(p)
    return libs


def find_cs2(libs):
    """返回 (所在库, CS2 目录) 或 (None, None)。"""
    for lib in libs:
        cs2 = os.path.join(lib, "steamapps", "common", CS2_DIR_NAME)
        if os.path.isdir(os.path.join(cs2, "game", "core")):
            return lib, cs2
    return None, None


def read_buildid():
    """读 appmanifest_730.acf 的 buildid，用于更新检测。失败返回 None。"""
    steam = find_steam_path()
    libs = parse_libraries(steam) if steam else []
    for lib in libs:
        acf = os.path.join(lib, "steamapps", "appmanifest_%d.acf" % APP_ID)
        try:
            m = re.search(r'"buildid"\s+"(\d+)"', open(acf, "r", encoding="utf-8", errors="replace").read())
            if m:
                return m.group(1)
        except OSError:
            continue
    return None

# ---------------------------------------------------------------- 提醒状态与计划任务

def load_reminder():
    try:
        with open(REMINDER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_reminder(data):
    os.makedirs(APP_DATA, exist_ok=True)
    with open(REMINDER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def task_exists():
    """更新提醒是否开启 = HKCU Run 键里有没有我们的自启动值。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def task_create():
    """开启更新提醒：写一条 HKCU 登录自启动（用户级，无需管理员，登录后静默检测）。"""
    try:
        me = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, '"%s" --check' % me)
        return True, ""
    except OSError as e:
        return False, str(e)


def task_delete():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
        return True
    except OSError:
        return False

# ---------------------------------------------------------------- 提醒窗（--check 模式）

def run_check_mode():
    """计划任务调用的静默检测：CS2 更新了才弹提醒窗，否则瞬间退出。"""
    cur = read_buildid()
    if not cur:
        return
    st = load_reminder()
    if "last_buildid" not in st:                 # 首次运行：只建基线，不弹窗
        st["last_buildid"] = cur
        save_reminder(st)
        return
    if st.get("last_buildid") == cur or st.get("muted") == cur:
        return
    last_fix = st.get("last_fix_time", "从未")
    root = tk.Tk()
    root.title(APP_TITLE)
    root.configure(bg=C_BG)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    f = tk.Frame(root, bg=C_BG, padx=22, pady=18)
    f.pack()
    tk.Label(f, text="CS2 更新了！", bg=C_BG, fg=C_ACCENT,
             font=("Microsoft YaHei", 15, "bold")).pack(anchor="w")
    tk.Label(f, text="大版本更新后容易爆卡掉帧。上次运行修复：%s。\n"
                     "如果进游戏觉得卡，就跑一次清理（全程只点两次鼠标）。" % last_fix,
             bg=C_BG, fg=C_FG, font=("Microsoft YaHei", 10), justify="left").pack(anchor="w", pady=(8, 14))
    btns = tk.Frame(f, bg=C_BG)
    btns.pack(fill="x")
    def open_main():
        if getattr(sys, "frozen", False):
            os.startfile(sys.executable)
        else:
            tk.messagebox.showinfo(APP_TITLE, "开发模式：请直接运行 cs2_fixer.py（不带 --check）")
        root.destroy()
    tk.Button(btns, text="打开修复工具", bg=C_ACCENT, fg="#14161a", relief="flat",
              font=("Microsoft YaHei", 10, "bold"), padx=14, pady=4, command=open_main).pack(side="left", padx=(0, 8))
    def know():
        st = load_reminder(); st["muted"] = cur; save_reminder(st)
        root.destroy()
    tk.Button(btns, text="知道了", bg="#2a2f36", fg=C_FG, relief="flat",
              font=("Microsoft YaHei", 10), padx=12, pady=4, command=know).pack(side="left", padx=(0, 8))
    def never():
        task_delete()
        st = load_reminder(); st["last_buildid"] = cur; st["muted"] = None; save_reminder(st)
        root.destroy()
    tk.Button(btns, text="不再提醒（关闭检测）", bg="#2a2f36", fg=C_FG_DIM, relief="flat",
              font=("Microsoft YaHei", 9), padx=8, pady=4, command=never).pack(side="right")
    root.mainloop()

# ---------------------------------------------------------------- 缓存清单

def get_cache_targets(cs2_lib):
    """动态检测所有候选缓存目录。返回 [(标签, 路径, 说明)]，仅保留存在的。"""
    la = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
    cands = [
        ("DirectX 着色器缓存（系统）", os.path.join(la, "D3DSCache"),
         "Windows 系统级 DirectX 着色器缓存。帖子让你用「磁盘清理」勾选删的就是它，这里直接删，等效。"),
        ("NVIDIA DXCache", os.path.join(la, "NVIDIA", "DXCache"),
         "NVIDIA 驱动的 DirectX 着色器缓存，CS2 掉帧时最值得清的一项。"),
        ("NVIDIA GLCache", os.path.join(la, "NVIDIA", "GLCache"),
         "NVIDIA 驱动的 OpenGL/Vulkan 着色器缓存。"),
        ("NVIDIA DXCache（新驱动）", os.path.join(la, "NVIDIA", "PerDriverVersion", "DXCache"),
         "新版 NVIDIA 驱动改用的缓存路径，与上面 DXCache 二选一存在。"),
        ("AMD DxCache", os.path.join(la, "AMD", "DxCache"),
         "AMD 显卡驱动的 DirectX 着色器缓存。"),
        ("AMD GLCache", os.path.join(la, "AMD", "GLCache"),
         "AMD 显卡驱动的 OpenGL 缓存。"),
        ("AMD VkCache", os.path.join(la, "AMD", "VkCache"),
         "AMD 显卡驱动的 Vulkan 缓存。"),
    ]
    if cs2_lib:
        cands.append(("Steam shadercache（CS2）", os.path.join(cs2_lib, "steamapps", "shadercache", str(APP_ID)),
                      "Steam 自己为 CS2 存的着色器缓存（Steam Deck/Linux 体系用的，Windows 上通常是空的，有就清掉）。"))
    return [(name, path, desc) for name, path, desc in cands if os.path.isdir(path)]

# ---------------------------------------------------------------- 状态持久化（中断保险）

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(files):
    os.makedirs(APP_DATA, exist_ok=True)
    data = {
        "version": 1,
        "stage": "pending_validate",
        "files": [{"path": p, "size": s} for p, s in files],
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clear_state():
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def check_pending():
    """返回 (缺失文件清单, 状态dict)。文件都已回来则顺手清掉状态。"""
    st = load_state()
    if not st:
        return [], None
    missing = [f for f in st.get("files", []) if not (os.path.isfile(f["path"]) and os.path.getsize(f["path"]) == f["size"])]
    if not missing:
        clear_state()
    return missing, st

# ---------------------------------------------------------------- 操作

def list_shader_files(cs2_dir):
    pat = os.path.join(cs2_dir, "game", "core", "shaders_*")
    return [(p, os.path.getsize(p)) for p in sorted(glob.glob(pat)) if os.path.isfile(p)]


def delete_files(files, log):
    ok, fail = [], []
    for i, (p, _) in enumerate(files, 1):
        log("  删除 (%d/%d) %s" % (i, len(files), os.path.basename(p)))
        if DRY_RUN:
            ok.append(p)
            continue
        try:
            os.remove(p)
            ok.append(p)
        except OSError as e:
            fail.append((p, str(e)))
            log("  !! 删除失败：%s（%s）" % (os.path.basename(p), e))
    return ok, fail


def dir_size(path):
    total = 0
    for root, _dirs, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def clean_dir(path, log):
    if DRY_RUN:
        log("  [dry-run] 跳过清空 %s" % path)
        return
    n = 0
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            n += 1
        except OSError as e:
            log("  跳过 %s（被占用：%s）" % (entry, e))
    log("  已清空 %s（%d 项）" % (path, n))


def trigger_validate():
    try:
        os.startfile("steam://validate/%d" % APP_ID)
        return True
    except OSError:
        return False

# ---------------------------------------------------------------- 验证完成检测

def read_log_state(steam_path):
    """读 Steam content_log.txt 尾部，判断 730 是否处于更新/验证活动。
    返回 (running: bool, last_line: str)。日志缺失视为不在活动。"""
    logf = os.path.join(steam_path, "logs", "content_log.txt")
    try:
        with open(logf, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return False, ""
    lines = [ln for ln in text.splitlines() if "AppID %d" % APP_ID in ln]
    if not lines:
        return False, ""
    # 只看最后一条 730 相关行：验证结束后最后一条是完成态，历史 Queued 行不再干扰
    running = bool(re.search(r"Running Update|Verifying|Queued|Validating", lines[-1]))
    return running, lines[-1].strip()


def watch_validation(steam_path, expect_files, log, ui, stop_event):
    """轮询直到删除的文件被 Steam 补回且验证活动结束。expect_files: [(path,size)]"""
    t0 = time.time()
    seen_running = False
    while not stop_event.is_set():
        if time.time() - t0 > WATCH_TIMEOUT:
            log("!! 等待超过 30 分钟，Steam 可能仍在验证。可到 Steam 里查看进度；")
            log("   文件稍后补回也不影响本工具——下次启动会继续检查。")
            ui("timeout")
            return
        back = all(os.path.isfile(p) and os.path.getsize(p) == s for p, s in expect_files) if expect_files else None
        running, last = read_log_state(steam_path)
        if running:
            if not seen_running:
                seen_running = True
                log("√ 已确认 Steam 开始验证游戏文件…")
            if back is False:
                log("  验证进行中…（已等待 %s）" % fmt_secs(time.time() - t0))
        if (seen_running and not running) and back is not False:
            # 补回完成（expect_files 为空时只看日志静默）
            clear_state()
            log("√ 修复完成！删除的着色器文件已由 Steam 补回，缓存会在下次进游戏时自动重建。")
            ui("done")
            return
        if not seen_running and time.time() - t0 > SEEK_RUNNING_GRACE:
            if back:
                clear_state()
                log("√ 文件已全部补回（日志未记录到验证活动，Steam 可能早已静默完成）。")
                ui("done")
                return
            log("… Steam 启动/排队中（已等待 %s），继续等…" % fmt_secs(time.time() - t0))
        stop_event.wait(WATCH_INTERVAL)


def fmt_secs(s):
    return "%d分%02d秒" % (s // 60, s % 60)

# ---------------------------------------------------------------- 教学文案

HELP_MAIN = """【这个软件是干嘛的】

CS2 每次大版本更新后爆卡、掉帧，多半是着色器（显卡的"翻译文件"）
在更新后留下了坏版本。修复办法是：删掉坏文件 → 让 Steam 重新下载
干净版本 → 顺手清掉显卡缓存。

这等于把小黑盒帖子《CS2更新后爆卡,掉帧严重的罪魁祸首找到了》里
那一长串手动操作，压缩成点一次按钮。

【三步分别做什么】
1. 删着色器文件：删 game\\core 里 4 个官方着色器包（约12MB）。
   不用怕，Steam 验证时会原样补回，游戏本体一个字节都没少。
2. 清缓存：清掉 Windows / 显卡驱动 / Steam 三层的着色器缓存，
   等于帖子里的"磁盘清理勾选 DirectX 着色器缓存"。
3. Steam 验证：等于右键 CS2 → 属性 → 已安装文件 → 验证完整性，
   我们帮你点，并自动盯着它做完。

【安全保证：中途断了怎么办】
· 删完文件后任何一步断掉（关机/崩溃/手滑关窗口），Steam 的验证
  都不受影响——它在 Steam 里独立运行，会自己把文件补回来。
· 软件在删完文件的那一刻会留一份"待恢复清单"，下次打开软件会
  检查并主动提示你补回，不会留烂摊子。
· 就算永远不用本软件，手动在 Steam 里点一次"验证完整性"也能
  完全恢复。本软件不写注册表、不改 Steam 设置、不留常驻进程。

【为什么不做帖子里的 shader_build 730】
那条命令属于 Steam Deck / Linux 的 Vulkan 着色器体系（Fossilize），
在 Windows 上不会产生任何实际效果——你机器上它该写的缓存目录
建了两年都是空的。删文件+验证+清缓存才是真正起作用的三件事。"""

HELP_SHADERS = """【清理 CS2 着色器文件】

帖子原手动操作：Steam 库 → 右键 CS2 → 管理 → 浏览本地文件
→ 进入 game 文件夹 → 再进 core 文件夹 → 把 shaders 开头的
文件全删掉。

本质：这 4 个文件（约12MB）是 Valve 打包分发的官方着色器包，
大版本更新后它可能与你的驱动不匹配，就是"爆卡"的元凶之一。
删掉它，下一步的 Steam 验证会自动下载当前版本的正确包回来。

会丢东西吗：不会。验证补回后和原来一模一样，这正是回滚方式。
游戏本体、存档、设置完全不受影响。"""

HELP_CACHES = """【清理着色器缓存】

帖子原手动操作：开始菜单搜"磁盘清理" → 勾选 DirectX 着色器
缓存 → 删除。

本质：磁盘清理删的就是下面这些文件夹，本软件直接删，效果一样
而且更精准——
· DirectX 着色器缓存（系统）：Windows 层的 D3D 缓存
· NVIDIA DXCache / GLCache：显卡驱动层缓存（清完下次进游戏
  头一两分钟稍慢，属于正常重建，之后恢复流畅）
· Steam shadercache：Steam 自己的缓存（多数人机器上是空的）

会丢东西吗：不会。缓存类文件系统/驱动会自动重建，没有"恢复"
一说。清错也只是白清一次。"""

HELP_VALIDATE = """【触发 Steam 验证并自动等待】

帖子原手动操作：Steam 库 → 右键 CS2 → 属性 → 已安装文件 →
验证游戏文件的完整性 → 干等它跑完。

本质：让 Steam 对照官方清单把删掉的文件原样下载回来。
本软件帮你触发，并每 3 秒检查一次：文件是否都回来了、Steam
是否还在忙。全部就绪会弹"修复完成"，你不用守着。

注意：验证一般需要几分钟（校验全部游戏文件），期间请不要启动
CS2，否则验证会被打断重来。"""

HELP_REMIND = """【CS2 更新提醒】

勾选后，软件会在 Windows 的「当前用户登录自启动」里登记一条
（位置：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，
值名 CS2Fixer）：每次登录桌面后静默运行约 2 秒，只做一件事
——比对 CS2 的版本号。没更新就瞬间退出：无窗口、无后台常驻、
不占内存。

只有检测到 CS2 更新了，才会弹一个小窗提醒：更新后容易爆卡，
卡了就打开工具跑一次清理。

怎么关闭：取消这个勾选即可，这条登记立即删除，系统恢复原样。
提醒窗里也有"不再提醒"按钮，效果相同。"""

FAQ = """
常见问题
━━━━━━━━━━━━━━━━━━━━
Q：点了开始修复，中途我把软件关了/断电了，会不会坏？
A：不会。删文件后的恢复由 Steam 独立完成，与软件无关。
   下次打开软件它会主动检查并提示补回。

Q：删掉的文件怎么手动恢复？
A：Steam → 右键 CS2 → 属性 → 已安装文件 → 验证游戏文件的完整性。

Q：多久跑一次？
A：CS2 每次大版本更新后卡顿了就跑一次，平时不用。

Q：为什么没有帖子里"控制台输 shader_build 730"那步？
A：见帮助首页底部。

Q：更新提醒会不会常驻后台？
A：不会。每次登录桌面静默跑 2 秒比对版本号，没更新瞬间退出。
   取消勾选即删除登记，系统恢复原样。

Q：会改系统设置吗？
A：核心功能不写注册表、不改 Steam 设置。唯一可选项是"更新提醒"：
   它会在「当前用户登录自启动」里登记一条（可用 msconfig 查看），
   界面上取消勾选即彻底移除。
"""

# ---------------------------------------------------------------- GUI

def make_gradient(width, height, top=(32, 36, 44), bottom=(17, 19, 23)):
    """程序化竖直渐变图（启动时一次性生成，静态零开销）。"""
    img = tk.PhotoImage(width=width, height=height)
    for y in range(height):
        t = y / max(1, height - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        img.put("{#%02x%02x%02x}" % c, (0, y, width, y + 1))
    return img


def shorten(s, n=52):
    return s if len(s) <= n else s[:n - 1] + "…"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("%s %s" % (APP_TITLE, APP_VERSION))
        self.resizable(False, False)
        self.configure(bg=C_BG)
        self.ui_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.log_file = None

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            self.call("tk", "scaling", dpi / 96.0 * 1.3333)
        except Exception:
            pass
        self._apply_theme()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.steam = find_steam_path()
        self.libs = parse_libraries(self.steam) if self.steam else []
        self.lib, self.cs2 = find_cs2(self.libs) if self.libs else (None, None)
        self.cache_targets = get_cache_targets(self.lib) if self.lib else []

        self._build_ui()
        self._setup_logfile()
        self.after(200, self._poll_queue)
        self.after(400, self._startup_checks)

    # ---------- UI 构建 ----------
    def _apply_theme(self):
        """CS2 官方 UI 配色：深灰底 + 橙黄强调（clam 主题全量自绘）。"""
        self.configure(bg=C_BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=C_BG, foreground=C_FG, bordercolor=C_LINE,
                        font=("Microsoft YaHei", 9))
        style.configure("TFrame", background=C_BG)
        style.configure("TLabel", background=C_BG, foreground=C_FG)
        style.configure("TLabelframe", background=C_BG, foreground=C_FG, bordercolor=C_LINE)
        style.configure("TLabelframe.Label", background=C_BG, foreground=C_ACCENT,
                        font=("Microsoft YaHei", 9, "bold"))
        style.configure("TCheckbutton", background=C_BG, foreground=C_FG, focuscolor=C_BG,
                        indicatorbackground="#2a2f36", indicatorcolor="#2a2f36",
                        indicatorforeground=C_ACCENT)
        style.map("TCheckbutton",
                  background=[("active", C_BG), ("pressed", C_BG)],
                  foreground=[("active", "#ffffff")],
                  indicatorcolor=[("selected", C_ACCENT), ("pressed", C_BG)])
        style.configure("TButton", background="#2a2f36", foreground=C_FG, bordercolor=C_LINE)
        style.map("TButton", background=[("active", "#3a4048"), ("pressed", "#23272d")])
        style.configure("Accent.TButton", background=C_ACCENT, foreground="#14161a",
                        font=("Microsoft YaHei", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", "#f0b254"), ("pressed", "#c8872c")],
                  foreground=[("active", "#14161a")])

    def _build_ui(self):
        pad = dict(padx=12, pady=5)
        self._banner_img = make_gradient(1200, 100)
        WIN_W = 940

        banner = tk.Canvas(self, height=100, highlightthickness=0, bg=C_BG2)
        banner.pack(fill="x")
        banner.create_image(0, 0, image=self._banner_img, anchor="nw")
        cx, cy = 52, 44
        banner.create_oval(cx - 16, cy - 16, cx + 16, cy + 16, outline=C_ACCENT, width=2)
        banner.create_line(cx - 27, cy, cx - 7, cy, fill=C_ACCENT, width=2)
        banner.create_line(cx + 7, cy, cx + 27, cy, fill=C_ACCENT, width=2)
        banner.create_line(cx, cy - 27, cx, cy - 7, fill=C_ACCENT, width=2)
        banner.create_line(cx, cy + 7, cx, cy + 27, fill=C_ACCENT, width=2)
        banner.create_oval(cx - 2.5, cy - 2.5, cx + 2.5, cy + 2.5, fill=C_ACCENT, outline="")
        banner.create_text(96, 28, text="CS2 掉帧修复工具", anchor="w",
                           fill="#ffffff", font=("Microsoft YaHei", 16, "bold"))
        banner.create_text(96, 55, text="着色器清理  ·  缓存重建  ·  一键修复",
                           anchor="w", fill=C_FG_DIM, font=("Microsoft YaHei", 9))
        steam_txt = self.steam or "未检测到"
        cs2_txt = shorten(self.cs2, 46) if self.cs2 else "未检测到！可在下方手动指定目录"
        self.txt_path = banner.create_text(
            96, 79, text="Steam: %s      CS2: %s" % (steam_txt, cs2_txt),
            anchor="w", fill="#7d838c", font=("Microsoft YaHei", 9))
        banner.create_text(WIN_W - 18, 20, text=APP_VERSION, anchor="ne",
                           fill="#5a606a", font=("Microsoft YaHei", 8, "bold"))
        for i in range(7):
            x0 = WIN_W - 30 - i * 16
            banner.create_line(x0, 100, x0 + 46, 30, fill=C_ACCENT, stipple="gray25", width=2)
        banner.create_line(0, 99, WIN_W, 99, fill=C_ACCENT, width=1)
        if not self.cs2:
            banner.create_window(WIN_W - 18, 62, anchor="ne", window=ttk.Button(
                banner, text="手动选择 CS2 目录", command=self._pick_cs2))

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True)

        m1 = ttk.LabelFrame(frm, text=" 1. 清理 CS2 着色器文件 ")
        m1.pack(fill="x", **pad)
        row = ttk.Frame(m1); row.pack(fill="x", padx=8, pady=3)
        self.var_shaders = tk.BooleanVar(value=True)
        n, size = self._shader_stats()
        txt = "（%s）" % fmt_size(size) if n else "（未发现，可能已修复过）"
        self.lbl_shaders = ttk.Label(row, text="删除 game\\core\\shaders_*  %s" % txt)
        self.lbl_shaders.pack(side="left")
        ttk.Button(row, text="这是什么？", width=10, command=lambda: self._show_help("着色器文件", HELP_SHADERS)).pack(side="right")

        self.m2 = ttk.LabelFrame(frm, text=" 2. 清理着色器缓存 ")
        self.m2.pack(fill="x", **pad)
        self._fill_cache_area(self.m2)
        ttk.Button(self.m2, text="这是什么？", width=10, command=lambda: self._show_help("着色器缓存", HELP_CACHES)).pack(anchor="e", padx=8, pady=2)

        m3 = ttk.LabelFrame(frm, text=" 3. Steam 验证 + 自动等待完成 ")
        m3.pack(fill="x", **pad)
        r3 = ttk.Frame(m3); r3.pack(fill="x", padx=8, pady=3)
        self.var_validate = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="触发 Steam 验证完整性，软件自动盯着到完成（约几分钟）",
                        variable=self.var_validate).pack(side="left")
        ttk.Button(r3, text="这是什么？", width=10, command=lambda: self._show_help("Steam 验证", HELP_VALIDATE)).pack(side="right")

        m4 = ttk.LabelFrame(frm, text=" 4. CS2 更新提醒（可选） ")
        m4.pack(fill="x", **pad)
        r4 = ttk.Frame(m4); r4.pack(fill="x", padx=8, pady=3)
        self.var_remind = tk.BooleanVar(value=task_exists())
        ttk.Checkbutton(r4, text="登录后静默检测 CS2 是否更新，更新了才弹提醒（无后台常驻）",
                        variable=self.var_remind, command=self._toggle_task).pack(side="left")
        ttk.Button(r4, text="这是什么？", width=10, command=lambda: self._show_help("更新提醒", HELP_REMIND)).pack(side="right")

        btns = ttk.Frame(frm)
        btns.pack(fill="x", **pad)
        self.btn_start = ttk.Button(btns, text="开始修复", style="Accent.TButton", command=self._on_start)
        self.btn_start.pack(side="left", padx=(10, 4), pady=4, ipadx=12)
        ttk.Button(btns, text="打开日志文件夹", command=lambda: os.startfile(LOG_DIR) if os.path.isdir(LOG_DIR) else messagebox.showinfo(APP_TITLE, "还没有日志。")).pack(side="left", padx=4)
        ttk.Button(btns, text="帮助", command=lambda: self._show_help("帮助", HELP_MAIN + "\n" + FAQ)).pack(side="right", padx=10)

        lg = ttk.LabelFrame(frm, text=" 执行日志 ")
        lg.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self.txt_log = scrolledtext.ScrolledText(lg, height=12, width=88, state="disabled",
                                                 font=("Microsoft YaHei", 9), bg=C_BG2, fg="#cfd2d6",
                                                 insertbackground=C_FG, relief="flat")
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)

    def _shader_stats(self):
        if not self.cs2:
            return 0, 0
        files = list_shader_files(self.cs2)
        return len(files), sum(s for _, s in files)

    def _pick_cs2(self):
        d = filedialog.askdirectory(title="选择 CS2 目录（Counter-Strike Global Offensive）")
        if not d:
            return
        if not os.path.isdir(os.path.join(d, "game", "core")):
            messagebox.showwarning(APP_TITLE, "该目录下没有 game\\core，不像 CS2 的安装目录。")
            return
        self.cs2 = d
        self.cache_targets = get_cache_targets(None)
        for w in self.m2.winfo_children():
            w.destroy()
        self._fill_cache_area(self.m2)
        n, size = self._shader_stats()
        self.lbl_shaders.config(text="删除 game\\core\\shaders_*  %s" % ("（%s）" % fmt_size(size) if n else "（未发现，可能已修复过）"))
        for w in self.winfo_children():
            if isinstance(w, tk.Canvas):
                w.itemconfig(self.txt_path, text="Steam: %s      CS2: %s（手动指定）" % (self.steam or "未检测到", shorten(d, 46)))

    def _fill_cache_area(self, parent):
        """填充/重建缓存勾选区。"""
        self.cache_vars = []
        if self.cache_targets:
            for name, path, desc in self.cache_targets:
                if not os.path.isdir(path):
                    continue
                v = tk.BooleanVar(value=True)
                r = ttk.Frame(parent); r.pack(fill="x", padx=8, pady=1)
                ttk.Checkbutton(r, text="%s  （%s）" % (name, fmt_size(dir_size(path))), variable=v).pack(side="left")
                self.cache_vars.append((v, name, path))
        else:
            ttk.Label(parent, text="未发现可清理的缓存目录").pack(anchor="w", padx=8, pady=3)

    # ---------- 日志 ----------
    def _setup_logfile(self):
        if DRY_RUN:
            self.logf = None
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        name = "fixer_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.logf = open(os.path.join(LOG_DIR, name), "a", encoding="utf-8")

    def log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = "[%s] %s" % (ts, msg)
        self.ui_q.put(("log", line))
        if self.logf:
            try:
                self.logf.write(line + "\n")
                self.logf.flush()
            except OSError:
                pass

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                if kind == "log":
                    self.txt_log.config(state="normal")
                    self.txt_log.insert("end", payload + "\n")
                    self.txt_log.see("end")
                    self.txt_log.config(state="disabled")
                elif kind == "done":
                    self.btn_start.config(state="normal")
                    messagebox.showinfo(APP_TITLE, "修复完成！可以启动 CS2 了。\n首次进游戏加载稍慢是正常现象（缓存重建）。")
                elif kind == "timeout":
                    self.btn_start.config(state="normal")
                elif kind == "error":
                    self.btn_start.config(state="normal")
                    messagebox.showerror(APP_TITLE, payload)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _show_help(self, title, text):
        w = tk.Toplevel(self)
        w.title("%s - %s" % (APP_TITLE, title))
        w.geometry("620x460")
        w.transient(self)
        w.configure(bg=C_BG)
        t = scrolledtext.ScrolledText(w, wrap="word", font=("Microsoft YaHei", 10),
                                      bg=C_BG2, fg=C_FG, insertbackground=C_FG, relief="flat")
        t.pack(fill="both", expand=True, padx=8, pady=8)
        t.insert("1.0", text.strip())
        t.config(state="disabled")

    def _toggle_task(self):
        if DRY_RUN:
            self.log("[dry-run] 计划任务注册/删除已跳过")
            return
        if self.var_remind.get():
            ok, msg = task_create()
            if ok:
                self.log("√ 已开启更新提醒：每次登录后静默检测 CS2 版本号（登录自启动项：%s）" % RUN_VALUE)
            else:
                self.var_remind.set(False)
                self.log("!! 开启失败：%s" % msg)
                messagebox.showerror(APP_TITLE, "开启失败：%s" % msg)
        else:
            task_delete()
            self.log("已关闭更新提醒，登录自启动登记已删除，系统恢复原样。")

    # ---------- 启动检查 ----------
    def _startup_checks(self):
        if DRY_RUN:
            self.log("== dry-run 模式：只扫描和演练，不会真删任何文件 ==")
        marker = os.path.join(APP_DATA, "seen_welcome.txt")
        first_run = not os.path.exists(marker)
        if first_run and not DRY_RUN:
            messagebox.showinfo(
                APP_TITLE + " · 使用前须知",
                "开始前只需要记住三件事：\n\n"
                "1. 先完全退出 CS2 游戏（本软件会再检查一遍）\n"
                "2. 删掉的东西 Steam 会原样补回，不用怕\n"
                "3. 最后的验证要几分钟，期间别开游戏，也不用守着\n\n"
                "详细说明和常见问题在右下角「帮助」里。")
            try:
                open(marker, "w").close()
            except OSError:
                pass

        missing, st = check_pending()
        if st and not missing and not DRY_RUN:
            self.log("上次中断的修复已由 Steam 自行完成（文件都在），残留状态已清理。")
        if missing and not DRY_RUN:
            self.log("检测到上次修复未完成：%d 个文件尚未被 Steam 补回。" % len(missing))
            if messagebox.askyesno(
                    APP_TITLE + " · 上次修复中断",
                    "上次修复执行到一半中断了，有 %d 个着色器文件\n"
                    "还没被 Steam 补回来（不影响游戏设置，但建议尽快恢复）。\n\n"
                    "要现在就触发 Steam 验证把它们补回吗？" % len(missing)):
                self.log("按用户要求，立即触发验证补回缺失文件…")
                self.var_shaders.set(False)
                threading.Thread(target=self._run_fix, args=(False, [], True), daemon=True).start()
            else:
                self.log("用户选择暂不恢复。下次打开软件会再次提醒。")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                    APP_TITLE,
                    "修复还在执行中（Steam 验证由 Steam 自己完成，不受影响）。\n"
                    "确定要退出吗？\n\n"
                    "退出后下次打开本软件，会自动检查删掉的文件是否已补回。"):
                return
            self.stop_event.set()
        self.destroy()

    # ---------- 执行 ----------
    def _on_start(self):
        if is_cs2_running():
            messagebox.showwarning(APP_TITLE, "检测到 CS2 正在运行。\n请先完全退出游戏（包括托盘），再点开始修复。")
            return
        if not self.cs2 and not (self.var_validate.get() or any(v.get() for v, _, _ in self.cache_vars)):
            messagebox.showwarning(APP_TITLE, "没有检测到 CS2，且没有勾选任何可执行的操作。")
            return

        shader_files = list_shader_files(self.cs2) if (self.cs2 and self.var_shaders.get()) else []
        picked_caches = [(n, p) for v, n, p in self.cache_vars if v.get()]
        do_validate = self.var_validate.get()

        if shader_files:
            total = sum(s for _, s in shader_files)
            names = "\n".join("  · %s（%s）" % (os.path.basename(p), fmt_size(s)) for p, s in shader_files)
            if not messagebox.askyesno(
                    APP_TITLE + " · 确认删除",
                    "将删除以下 %d 个着色器文件，共 %s：\n\n%s\n\n"
                    "Steam 验证时会自动原样补回，确定继续吗？" % (len(shader_files), fmt_size(total), names)):
                self.log("用户在确认弹窗选择了取消。")
                return

        if not (shader_files or picked_caches or do_validate):
            messagebox.showinfo(APP_TITLE, "没有勾选任何操作。")
            return

        self.btn_start.config(state="disabled")
        self.stop_event.clear()
        self.worker = threading.Thread(target=self._run_fix, args=(bool(shader_files), shader_files, do_validate, picked_caches), daemon=True)
        self.worker.start()

    def _run_fix(self, do_shaders, shader_files, do_validate, picked_caches=()):
        try:
            self.log("== 开始 ==")
            if DRY_RUN:
                self.log("[dry-run] 以下全部操作只做演练，不动真实文件")

            if is_cs2_running():
                self.ui_q.put(("error", "执行前检查：CS2 正在运行，请先退出游戏。"))
                self.log("!! CS2 正在运行，中止。")
                return

            deleted = []
            if do_shaders and shader_files:
                self.log("① 删除着色器文件（共 %d 个，%s）" % (len(shader_files), fmt_size(sum(s for _, s in shader_files))))
                ok, fail = delete_files(shader_files, self.log)
                deleted = ok
                if fail:
                    self.log("   有 %d 个删除失败（多为文件被占用），已记录。" % len(fail))
                elif not DRY_RUN:
                    self.log("√ 删除完成。")
            elif do_shaders:
                self.log("① 未发现需要删除的着色器文件，跳过。")

            if picked_caches:
                self.log("② 清理着色器缓存")
                for name, path in picked_caches:
                    before = dir_size(path)
                    self.log("  清理 %s（%s）" % (name, fmt_size(before)))
                    clean_dir(path, self.log)
                self.log("√ 缓存清理完成。首次进游戏稍慢属正常重建。")

            if do_validate and (deleted or not do_shaders) and not DRY_RUN:
                if deleted:
                    save_state(deleted)
                    self.log("（已写待恢复清单，万一中途断电/退出，下次打开本软件会自动提醒补回）")
                self.log("③ 触发 Steam 验证完整性…")
                if trigger_validate():
                    self.log("已发送验证请求。Steam 若没开会自动启动；启动后可能先登录再排队，请耐心等。")
                    self.log("   期间请【不要启动 CS2】。可以最小化本窗口去干别的，完成会弹窗提醒。")
                    expect = [(p, s) for p, s in shader_files] if deleted else []
                    watch_validation(self.steam, expect, self.log,
                                     lambda kind: self.ui_q.put((kind, "")), self.stop_event)
                else:
                    self.log("!! 无法触发 Steam 验证（steam:// 协议失败）。")
                    self.log("   请手动：Steam → 右键 CS2 → 属性 → 已安装文件 → 验证游戏文件的完整性。")
                    self.ui_q.put(("error", "无法自动触发 Steam 验证。\n请按日志里的手动方法操作一次。"))
            elif do_validate and DRY_RUN:
                self.log("③ [dry-run] 跳过触发验证（真实运行时才会调起 Steam）")
            elif do_validate and not deleted:
                self.log("③ 没有删除任何文件，无需验证，跳过。")

            self.log("== 结束 ==")
            if not DRY_RUN:
                cur = read_buildid()
                st = load_reminder()
                if cur:
                    st["last_buildid"] = cur
                st["last_fix_time"] = datetime.date.today().isoformat()
                st["muted"] = None
                save_reminder(st)
        except Exception as e:  # 兜底：任何未预期异常都不静默
            self.log("!! 发生未预期的错误：%r" % (e,))
            self.ui_q.put(("error", "发生错误：%s\n\n详情见日志。删掉的文件可在 Steam 里验证补回。" % e))

# ---------------------------------------------------------------- 入口

def main():
    os.makedirs(APP_DATA, exist_ok=True)
    if CHECK_MODE:
        run_check_mode()
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
