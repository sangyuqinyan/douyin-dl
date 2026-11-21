# -*- coding: utf-8 -*-
"""抖音批量下载器 —— 桌面外壳（Windows 11 Fluent 风格）

打包说明：
  pyinstaller --noconfirm --clean --onedir --noconsole --name DouyinDownloader \
      --collect-all playwright --collect-all sv_ttk --paths . app.py

运行方式：
  双击 -> 图形界面
  命令行 -> 抖音下载器.exe --cli --url ... --mode post --max 20
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import sv_ttk
except Exception:
    sv_ttk = None

APP_TITLE = "抖音批量下载"
APP_SUB = "浏览器驱动 · 自动去重 · 增量更新"
FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
COOKIE = os.path.join(BASE, "cookie.txt")
DEFAULT_OUT = os.path.join(BASE, "Download")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
FONT = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
FONT_LOG = ("Consolas", 9)

MODES = [("主页作品", "post"), ("喜欢 / 点赞", "like"), ("我的收藏", "collection"),
         ("合集", "mix"), ("单条作品", "one")]


def system_prefers_dark():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return val == 0
    except Exception:
        return False


def cli_mode():
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--cli"]
    import douyin_batch
    sys.exit(douyin_batch.main())


def ensure_streams():
    """窗口/管道模式下统一 UTF-8，避免中文与 emoji 触发 GBK 编码异常。"""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass
            s = getattr(sys, name, None)
        if s is not None and hasattr(s, "reconfigure"):
            try:
                if not s.isatty():
                    s.reconfigure(encoding="utf-8", errors="replace")
                else:
                    s.reconfigure(errors="replace")
            except Exception:
                pass


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.q = queue.Queue()
        self.dark = system_prefers_dark()
        self.accent = "Accent.TButton" if sv_ttk else "TButton"

        root.title(APP_TITLE)
        root.geometry("880x660")
        root.minsize(780, 580)
        try:
            ico = os.path.join(BASE, "assets", "app.ico")
            if os.path.exists(ico):
                root.iconbitmap(default=ico)
        except Exception:
            pass

        if sv_ttk:
            try:
                sv_ttk.set_theme("dark" if self.dark else "light")
            except Exception:
                pass
        self._init_style()
        self._build()
        self.refresh_cookie_state()
        root.after(120, self.drain)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------ 外观
    def _init_style(self):
        st = ttk.Style()
        st.configure(".", font=FONT)
        st.configure("Title.TLabel", font=FONT_TITLE)
        st.configure("Sub.TLabel", font=("Microsoft YaHei UI", 9),
                     foreground=("#6a6a6a" if not self.dark else "#a0a0a0"))
        st.configure("Hint.TLabel", font=("Microsoft YaHei UI", 8),
                     foreground=("#8a8a8a" if not self.dark else "#8f8f8f"))
        st.configure("Card.TFrame", padding=16)
        st.configure("Status.TLabel", font=("Microsoft YaHei UI", 8),
                     foreground=("#666666" if not self.dark else "#9a9a9a"))
        self.log_bg = "#fbfbfb" if not self.dark else "#1b1b1b"
        self.log_fg = "#1b1b1b" if not self.dark else "#e6e6e6"

    def _build(self):
        outer = ttk.Frame(self.root, padding=(20, 16, 20, 12))
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x", pady=(0, 14))
        ttk.Label(head, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="    " + APP_SUB, style="Sub.TLabel").pack(side="left", pady=(7, 0))

        card = ttk.Frame(outer, style="Card.TFrame")
        card.pack(fill="x")
        card.columnconfigure(0, weight=1, uniform="col")
        card.columnconfigure(2, weight=1, uniform="col")

        ttk.Label(card, text="作品链接").grid(row=0, column=0, columnspan=3, sticky="w")
        self.url = tk.StringVar(value="https://www.douyin.com/user/self?from_tab_name=main&showTab=post")
        url_row = ttk.Frame(card)
        url_row.grid(row=1, column=0, columnspan=3, sticky="we", pady=(4, 14))
        ttk.Button(url_row, text="粘贴", width=7, command=self.paste).pack(side="right", padx=(8, 0))
        ttk.Entry(url_row, textvariable=self.url, font=FONT).pack(side="left", fill="x", expand=True)

        # 左列：模式 / 区间
        ttk.Label(card, text="下载模式").grid(row=2, column=0, sticky="w")
        self.mode = tk.StringVar(value=MODES[0][0])
        ttk.Combobox(card, textvariable=self.mode, values=[m[0] for m in MODES],
                     state="readonly", width=16, font=FONT).grid(row=3, column=0, sticky="we", pady=(4, 12))

        ttk.Label(card, text="时间区间（可留空）").grid(row=4, column=0, sticky="w")
        self.interval = tk.StringVar(value="")
        ttk.Entry(card, textvariable=self.interval, font=FONT).grid(row=5, column=0, sticky="we", pady=(4, 0))
        ttk.Label(card, text="格式 2025-01-01|2026-11-01", style="Hint.TLabel").grid(
            row=6, column=0, sticky="w", pady=(3, 0))

        # 右列：数量 / 目录
        ttk.Label(card, text="本次新增条数").grid(row=2, column=2, sticky="w")
        num_row = ttk.Frame(card)
        num_row.grid(row=3, column=2, sticky="w", pady=(4, 12))
        self.count = tk.StringVar(value="20")
        ttk.Spinbox(num_row, from_=0, to=9999, textvariable=self.count, width=8,
                    font=FONT).pack(side="left")
        ttk.Label(num_row, text="   0 = 不限", style="Hint.TLabel").pack(side="left")

        ttk.Label(card, text="保存到").grid(row=4, column=2, sticky="w")
        out_row = ttk.Frame(card)
        out_row.grid(row=5, column=2, sticky="we", pady=(4, 0))
        self.out = tk.StringVar(value=DEFAULT_OUT)
        ttk.Button(out_row, text="浏览", width=7, command=self.browse).pack(side="right", padx=(8, 0))
        ttk.Entry(out_row, textvariable=self.out, font=FONT).pack(side="left", fill="x", expand=True)

        # 操作栏
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(16, 10))
        self.btn_start = ttk.Button(bar, text="开始下载", style=self.accent, command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="检查去重", command=self.verify).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开文件夹", command=self.open_dir).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="更新 Cookie", command=self.edit_cookie).pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 12))

        # 日志
        ttk.Label(outer, text="运行日志", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
        self.log = scrolledtext.ScrolledText(outer, height=13, wrap="word", font=FONT_LOG,
                                             relief="solid", borderwidth=1, padx=10, pady=8)
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled", background=self.log_bg, foreground=self.log_fg,
                           insertbackground=self.log_fg, highlightthickness=0)

        # 状态栏
        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(8, 0))
        self.status = ttk.Label(status, text="就绪", style="Status.TLabel")
        self.status.pack(side="left")
        self.cookie_state = ttk.Label(status, text="", style="Status.TLabel")
        self.cookie_state.pack(side="right")

    # ------------------------------------------------ 交互
    def paste(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.out.get() or BASE)
        if d:
            self.out.set(d)

    def open_dir(self):
        p = self.out.get() or DEFAULT_OUT
        os.makedirs(p, exist_ok=True)
        if os.name == "nt":
            os.startfile(p)
        else:
            subprocess.Popen(["xdg-open", p])

    def refresh_cookie_state(self):
        ok = os.path.exists(COOKIE) and os.path.getsize(COOKIE) > 20
        self.cookie_state.config(text=("Cookie 已就绪" if ok else "Cookie 未设置"),
                                 foreground=("#1a7f37" if ok else "#c0392b"))

    def edit_cookie(self):
        win = tk.Toplevel(self.root)
        win.title("更新 Cookie")
        win.geometry("680x380")
        win.transient(self.root)
        ttk.Label(win, text="把浏览器请求头里的 Cookie 整段粘贴进来（不含 Cookie: 前缀，不要换行）：",
                  wraplength=640).pack(anchor="w", padx=16, pady=(16, 8))
        txt = tk.Text(win, wrap="char", font=("Consolas", 9), relief="solid", borderwidth=1,
                      padx=8, pady=8, background=self.log_bg, foreground=self.log_fg,
                      insertbackground=self.log_fg)
        txt.pack(fill="both", expand=True, padx=16)
        if os.path.exists(COOKIE):
            txt.insert("1.0", open(COOKIE, encoding="ascii", errors="ignore").read().strip())

        def save():
            val = txt.get("1.0", "end").strip().replace("\r", "").replace("\n", "")
            if len(val) < 20:
                messagebox.showwarning("提示", "内容过短，请确认复制完整。", parent=win)
                return
            open(COOKIE, "w", encoding="ascii", errors="ignore").write(val)
            self.refresh_cookie_state()
            win.destroy()
            self.write("Cookie 已更新")

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=16, pady=14)
        ttk.Button(bar, text="保存", style=self.accent, command=save).pack(side="right")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=(0, 8))

    # ------------------------------------------------ 运行
    def write(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def build_cmd(self):
        mode = dict(MODES)[self.mode.get()]
        iv = self.interval.get().strip() or "all"
        return [sys.executable, "--cli", "--url", self.url.get().strip(), "--mode", mode,
                "--max", self.count.get().strip() or "0", "--interval", iv,
                "--out", self.out.get().strip() or DEFAULT_OUT, "--cookie", COOKIE]

    def _busy(self, on):
        self.btn_start.config(state="disabled" if on else "normal")
        self.btn_stop.config(state="normal" if on else "disabled")
        if on:
            self.progress.start(12)
        else:
            self.progress.stop()

    def start(self):
        if self.proc:
            return
        if not os.path.exists(COOKIE) or os.path.getsize(COOKIE) < 20:
            messagebox.showwarning("缺少 Cookie", "请先点「更新 Cookie」把登录 Cookie 粘贴进来。")
            return
        url = self.url.get().strip()
        if "douyin.com" not in url:
            messagebox.showwarning("链接无效", "请填写抖音链接。")
            return
        self.write("")
        self.write("> " + self.mode.get() + "  " + url)
        self.status.config(text="正在打开页面…")
        self._busy(True)
        threading.Thread(target=self.run_proc, args=(self.build_cmd(),), daemon=True).start()

    def run_proc(self, cmd):
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace",
                                         creationflags=CREATE_NO_WINDOW, cwd=BASE, env=env)
            for line in self.proc.stdout:
                self.q.put(line.rstrip())
            code = self.proc.wait()
            self.q.put(("DONE", code))
        except Exception as e:
            self.q.put(f"启动失败：{e}")
            self.q.put(("DONE", -1))

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.q.put("已请求停止…")

    def verify(self):
        if self.proc:
            return
        self.write("")
        self.write("> 一致性检查")
        self.status.config(text="正在校验索引…")
        self._busy(True)
        cmd = [sys.executable, "--cli", "--verify", "--out", self.out.get().strip() or DEFAULT_OUT]
        threading.Thread(target=self.run_proc, args=(cmd,), daemon=True).start()

    def drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    self.finish(item[1])
                    continue
                self.write(item)
                if any(k in item for k in ("扫描", "待下载", "下载 ", "完成 ")):
                    self.status.config(text=item.strip()[:64])
        except queue.Empty:
            pass
        self.root.after(120, self.drain)

    def finish(self, code):
        self.proc = None
        self._busy(False)
        msg = {0: "完成", 3: "部分失败，可重跑补齐", 4: "校验发现不一致"}.get(code, f"退出码 {code}")
        self.status.config(text=msg)
        self.write(f"— {msg} —")

    def on_close(self):
        if self.proc:
            if not messagebox.askokcancel("退出", "任务仍在运行，确定退出？"):
                return
            self.proc.terminate()
        self.root.destroy()


def main():
    ensure_streams()
    if "--echo" in sys.argv:
        i = sys.argv.index("--echo")
        print(sys.argv[i + 1] if len(sys.argv) > i + 1 else "", flush=True)
        return
    if "--cli" in sys.argv:
        cli_mode()
        return
    if "--selftest" in sys.argv:
        root = tk.Tk()
        root.withdraw()
        App(root)
        root.update()
        root.destroy()
        sys.exit(0)
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.15)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        detail = traceback.format_exc()
        try:
            with open(os.path.join(BASE, "error.log"), "a", encoding="utf-8") as f:
                f.write(detail + "\n")
        except Exception:
            pass
        try:
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(APP_TITLE, "程序出错，详情已写入同目录 error.log\n\n"
                                 + detail.strip().splitlines()[-1][:180])
            r.destroy()
        except Exception:
            pass
        sys.exit(1)
