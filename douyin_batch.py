# -*- coding: utf-8 -*-
"""抖音批量下载（浏览器驱动版）

原理：用 Playwright 启动本机 Edge，注入 cookie.txt 中的登录态，打开目标页面，
由页面自身完成平台签名，脚本只截获接口返回的 JSON，再下载无水印直链。

去重：以 aweme_id 为唯一标识，落在 Download/.state.db（SQLite）。
判定发生在“应用 --max 之前”，因此已下载的作品不占配额。

用法示例：
  python douyin_batch.py --url <链接> --mode post --max 20
  python douyin_batch.py --verify          # 校验索引与磁盘是否一致
  python douyin_batch.py --rebuild         # 扫描已有文件、重建索引（迁移用）
"""

import argparse
import os
import re
import sqlite3
import sys
import time

import httpx
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0")

MODE_ENDPOINTS = {
    "post": ("/aweme/v1/web/aweme/post/",),
    "like": ("/aweme/v1/web/aweme/favorite/",),
    "collection": ("/aweme/v1/web/aweme/listcollection/", "/aweme/v1/web/aweme/collection/"),
    "mix": ("/aweme/v1/web/mix/aweme/",),
    "one": ("/aweme/v1/web/aweme/detail/",),
}

DB_NAME = ".state.db"
LOCK_NAME = ".lock"
MIN_SIZE = 1024

# 程序所在目录：打包成 exe 后取 exe 目录，脚本运行时取脚本目录。
# 默认的 cookie.txt / Download 都锚定到这里，避免从别的目录启动时找不到。
BASE_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))


def _fix_streams():
    """Windows 下管道/重定向默认用 GBK，遇到 emoji 会抛 UnicodeEncodeError。
    非控制台（管道、文件、GUI 子进程）统一切 UTF-8；控制台保留原编码但不容错。"""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None or not hasattr(s, "reconfigure"):
            continue
        try:
            if not s.isatty():
                s.reconfigure(encoding="utf-8", errors="replace")
            else:
                s.reconfigure(errors="replace")
        except Exception:
            pass


_fix_streams()


def log(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------- 索引

class Index:
    """作品/文件级去重索引。key = aweme_id:idx（视频 idx=0，图集从 1 起）"""

    def __init__(self, out_dir):
        self.path = os.path.join(out_dir, DB_NAME)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                key        TEXT PRIMARY KEY,
                aweme_id   TEXT NOT NULL,
                idx        INTEGER NOT NULL,
                kind       TEXT NOT NULL,
                path       TEXT NOT NULL,
                size       INTEGER NOT NULL DEFAULT 0,
                status     TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_aweme ON files(aweme_id)")
        self.conn.commit()

    def get(self, key):
        cur = self.conn.execute("SELECT path, size, status FROM files WHERE key=?", (key,))
        return cur.fetchone()

    def is_done(self, key):
        row = self.get(key)
        if not row:
            return False
        path, size, status = row
        if status != "done":
            return False
        return os.path.exists(path) and os.path.getsize(path) >= MIN_SIZE

    def mark(self, key, aweme_id, idx, kind, path, size, status):
        self.conn.execute(
            "INSERT INTO files(key, aweme_id, idx, kind, path, size, status, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "path=excluded.path, size=excluded.size, status=excluded.status, updated_at=excluded.updated_at",
            (key, aweme_id, idx, kind, path, size, status, int(time.time())))
        self.conn.commit()

    def rows(self):
        return self.conn.execute("SELECT key, aweme_id, idx, kind, path, size, status FROM files").fetchall()

    def close(self):
        self.conn.close()


def pid_alive(pid):
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def acquire_lock(out_dir):
    path = os.path.join(out_dir, LOCK_NAME)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return path
    except FileExistsError:
        try:
            pid = int(open(path, encoding="ascii", errors="ignore").read().strip() or 0)
        except Exception:
            pid = 0
        if pid and pid_alive(pid):
            stale = False
        elif pid:
            stale = True          # 记录了 PID 但进程已不存在 → 崩溃残留
        else:
            try:
                stale = time.time() - os.path.getmtime(path) > 3600
            except OSError:
                stale = True
        if stale:
            try:
                os.remove(path)
            except OSError:
                pass
            return acquire_lock(out_dir)
        raise SystemExit("已有另一个下载任务在运行（.lock 存在）。关闭那个窗口，或删除 Download\\.lock 后重试。")


def release_lock(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- 工具

def load_cookies(path):
    raw = open(path, encoding="ascii", errors="ignore").read().replace("\r", "").replace("\n", "").strip()
    if len(raw) < 20:
        raise SystemExit("cookie.txt 为空或过短，请先双击「设置Cookie.cmd」粘贴 Cookie。")
    out = []
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k:
                out.append({"name": k, "value": v, "domain": ".douyin.com", "path": "/"})
    return out


RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def sanitize(text, limit=48):
    """清洗为合法的 Windows 文件/目录名。

    要点：结尾的空格与点在 Windows 上不合法，必须一起剥掉；
    仅 strip() 或仅 strip('.') 都会留下"末尾空格"这种坑（如昵称 "wwy ."）。
    """
    text = re.sub(r"[\r\n\t]", " ", text or "")
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = "".join(ch for ch in text if ord(ch) >= 32)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" .")[:limit].strip(" .")
    if not text:
        text = "未命名"
    if text.split(".")[0].upper() in RESERVED_NAMES:
        text = "_" + text
    return text


def best_video_url(video):
    best = None
    for b in video.get("bit_rate") or []:
        url = ((b.get("play_addr") or {}).get("url_list") or [None])[0]
        if url and (best is None or b.get("bit_rate", 0) > best[0]):
            best = (b.get("bit_rate", 0), url)
    if best:
        return best[1]
    urls = (video.get("play_addr") or {}).get("url_list") or []
    return urls[0] if urls else None


def build_jobs(item, out_dir):
    """返回 {id, ts, jobs:[(key, idx, kind, dest, url)]}，key 为去重主键。"""
    aweme_id = str(item.get("aweme_id") or "")
    if not aweme_id:
        return None
    ts = int(item.get("create_time") or 0)
    date = time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(ts)) if ts else "0000-00-00"
    desc = sanitize(item.get("desc") or "", 40)
    author = sanitize((item.get("author") or {}).get("nickname") or "未知作者", 24)

    folder = os.path.join(out_dir, author)
    base = f"{date}_{desc}_{aweme_id}"

    jobs = []
    images = item.get("images") or []
    if images:
        for pos, img in enumerate(images, 1):
            urls = img.get("url_list") or []
            if urls:
                jobs.append((f"{aweme_id}:{pos}", pos, "img",
                             os.path.join(folder, f"{base}_{pos}.jpeg"), urls[-1]))
    else:
        url = best_video_url(item.get("video") or {})
        if url:
            jobs.append((f"{aweme_id}:0", 0, "video", os.path.join(folder, base + ".mp4"), url))
    return {"id": aweme_id, "ts": ts, "jobs": jobs}


def download(client, url, dest, referer):
    """下载到 .part，校验后原子改名。返回 (状态, 字节数)。"""
    folder = os.path.dirname(dest)
    os.makedirs(folder, exist_ok=True)
    tmp = dest + ".part"
    last = ""
    for attempt in range(3):
        try:
            os.makedirs(folder, exist_ok=True)   # 目录可能被外部清理
            with client.stream("GET", url, headers={"Referer": referer, "User-Agent": UA}) as r:
                r.raise_for_status()
                done = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(262144):
                        f.write(chunk)
                        done += len(chunk)
            if done < MIN_SIZE:
                raise IOError("文件过小")
            os.replace(tmp, dest)
            return "ok", done
        except Exception as e:
            last = f"{type(e).__name__}:{e}"[:90]
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if attempt == 2:
                return f"fail:{last}", 0
            time.sleep(1.5)
    return f"fail:{last}", 0


def harvest(url, cookies, headless, mode="post", enough=None, scan_cap=1500):
    """打开页面截获列表。

    enough(items) 返回 True 时停止滚动——由调用方按"新增作品数"判断，
    而不是按"已采集总数"判断，否则已下载越多越翻不动。
    """
    items = {}
    state = {"has_more": True, "sec": ""}
    allowed = MODE_ENDPOINTS.get(mode, MODE_ENDPOINTS["post"])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="msedge", headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--mute-audio"],
        )
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        ctx.add_cookies(cookies)
        page = ctx.new_page()

        def on_req(req):
            m = re.search(r"sec_user_id=(MS4wLjABAAAA[\w\-]+)", req.url)
            if m:
                state["sec"] = m.group(1)

        def on_resp(resp):
            if resp.status != 200 or not any(p in resp.url for p in allowed):
                return
            try:
                data = resp.json()
            except Exception:
                return
            lst = data.get("aweme_list") or []
            if data.get("aweme_detail"):
                lst = [data["aweme_detail"]]
            for it in lst:
                if it.get("aweme_id"):
                    items[str(it["aweme_id"])] = it
            if "has_more" in data:
                state["has_more"] = bool(data.get("has_more"))

        page.on("request", on_req)
        page.on("response", on_resp)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # 关键：抖音用的是虚拟列表，滚轮必须落在列表区域才会触发下一页；
        # 指针默认在 (0,0)，不移动的话最多只加载两三页就停住。
        def center_mouse():
            try:
                vp = page.viewport_size or {"width": 1920, "height": 1080}
                page.mouse.move(vp["width"] // 2, int(vp["height"] * 0.6))
            except Exception:
                pass

        center_mouse()

        idle = 0
        last = 0
        for _ in range(400):
            if enough and enough(items):
                break
            if len(items) >= scan_cap:
                log(f"已达单次扫描上限 {scan_cap} 个，如仍有未下载内容请再跑一次")
                break
            if idle >= 4:
                break
            page.mouse.wheel(0, 2600)
            page.wait_for_timeout(1400)
            if len(items) == last:
                idle += 1
                if idle == 2:
                    center_mouse()      # 指针可能被页面重绘挤开，重新定位
            else:
                idle, last = 0, len(items)
            if not state["has_more"] and idle >= 2:
                break

        browser.close()
    return items, state["sec"]


# ---------------------------------------------------------------- 校验 / 重建

FILE_RE = re.compile(r"_(\d{15,20})(?:_(\d+))?\.(mp4|jpeg|jpg|png|webp)$", re.I)
LEGACY_RE = re.compile(r"_(\d{8})(?:_(\d+))?\.(mp4|jpeg|jpg|png|webp)$", re.I)


def legacy_map(out_dir):
    """旧命名（aweme_id 后 8 位）-> 路径映射，用于把历史文件纳入索引而不重下。"""
    m = {}
    for path in scan_disk(out_dir):
        mm = LEGACY_RE.search(os.path.basename(path))
        if mm:
            last8 = mm.group(1)
            pos = int(mm.group(2) or 0)
            m.setdefault((last8, pos), []).append(path)
    return m


def adopt_legacy(out_dir, index, items):
    """把磁盘上旧命名的文件按 (id 后 8 位, 序号) 归入索引，避免同一作品被重复下载。"""
    lm = legacy_map(out_dir)
    if not lm:
        return 0
    adopted = 0
    for it in items.values():
        aid = str(it.get("aweme_id") or "")
        if not aid:
            continue
        last8 = aid[-8:]
        images = it.get("images") or []
        if images:
            for pos in range(1, len(images) + 1):
                key = f"{aid}:{pos}"
                if index.get(key):
                    continue
                for path in lm.get((last8, pos), []):
                    if os.path.exists(path):
                        index.mark(key, aid, pos, "img", path, os.path.getsize(path), "done")
                        adopted += 1
                        break
        else:
            key = f"{aid}:0"
            if not index.get(key):
                for path in lm.get((last8, 0), []):
                    if path.lower().endswith(".mp4") and os.path.exists(path):
                        index.mark(key, aid, 0, "video", path, os.path.getsize(path), "done")
                        adopted += 1
                        break
    return adopted


def scan_disk(out_dir):
    found = []
    for root, _, files in os.walk(out_dir):
        for name in files:
            if name.endswith(".part"):
                continue
            if not re.search(r"\.(mp4|jpeg|jpg|png|webp)$", name, re.I):
                continue
            found.append(os.path.join(root, name))
    return found


def rebuild(out_dir, index):
    adopted = 0
    for path in scan_disk(out_dir):
        m = FILE_RE.search(os.path.basename(path))
        if not m:
            continue
        aweme_id, pos, ext = m.group(1), int(m.group(2) or 0), m.group(3).lower()
        key = f"{aweme_id}:{pos}"
        if index.get(key):
            continue
        kind = "video" if ext == "mp4" else "img"
        index.mark(key, aweme_id, pos, kind, path, os.path.getsize(path), "done")
        adopted += 1
    return adopted


def prune(out_dir, index):
    """删除索引中文件已不存在的记录，用于"我主动删了，不要自动补回"。"""
    n = 0
    for key, aweme_id, idx, kind, path, size, status in index.rows():
        if status == "done" and not os.path.exists(path):
            index.conn.execute("DELETE FROM files WHERE key=?", (key,))
            n += 1
    index.conn.commit()
    return n


def verify(out_dir, index):
    rows = index.rows()
    problems = {"missing": [], "orphan": [], "partial": [], "duplicate": []}
    seen = {}

    for key, aweme_id, idx, kind, path, size, status in rows:
        if status != "done":
            continue
        if not os.path.exists(path):
            problems["missing"].append(path)
        elif os.path.getsize(path) < MIN_SIZE:
            problems["partial"].append(path)
        seen.setdefault(key, []).append(path)

    for key, paths in seen.items():
        if len(paths) > 1:
            problems["duplicate"].append((key, paths))

    known = {os.path.abspath(r[4]) for r in rows if r[6] == "done"}
    legacy_unmanaged = []
    for path in scan_disk(out_dir):
        if os.path.abspath(path) in known:
            continue
        name = os.path.basename(path)
        if LEGACY_RE.search(name) and not FILE_RE.search(name):
            legacy_unmanaged.append(path)
        else:
            problems["orphan"].append(path)

    for root, _, files in os.walk(out_dir):
        for name in files:
            if name.endswith(".part"):
                problems["partial"].append(os.path.join(root, name))

    log(f"索引记录：{len([r for r in rows if r[6] == 'done'])} 条")
    log(f"磁盘文件：{len(scan_disk(out_dir))} 个")
    log(f"缺失（有记录无文件）：{len(problems['missing'])}")
    for p in problems["missing"][:10]:
        log(f"  - {p}")
    log(f"孤儿（有文件无记录）：{len(problems['orphan'])}")
    for p in problems["orphan"][:10]:
        log(f"  - {p}")
    log(f"重复键：{len(problems['duplicate'])}")
    for k, ps in problems["duplicate"][:10]:
        log(f"  - {k} -> {ps}")
    log(f"残缺/临时文件：{len(problems['partial'])}")
    log(f"旧命名未纳管：{len(legacy_unmanaged)}（下次按对应账号抓取时会自动纳入索引）")

    clean = not any(problems.values())
    log("结论：" + ("一致，无重复风险" if clean else "存在不一致，建议按提示处理"))
    return 0 if clean else 4


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--mode", default="post")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--interval", default="all")
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "Download"))
    ap.add_argument("--cookie", default=os.path.join(BASE_DIR, "cookie.txt"))
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--prune", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    index = Index(args.out)

    if args.verify:
        code = verify(args.out, index)
        index.close()
        return code

    if args.rebuild:
        n = rebuild(args.out, index)
        log(f"已从磁盘回填 {n} 条记录")
        index.close()
        return 0

    if args.prune:
        n = prune(args.out, index)
        log(f"已遗忘 {n} 条文件已不存在的记录")
        index.close()
        return 0

    if not args.url:
        raise SystemExit("缺少 --url（或用 --verify / --rebuild）")

    cookies = load_cookies(args.cookie)

    url = args.url.strip()
    if args.mode in ("like", "collection"):
        if "showTab=" in url:
            url = re.sub(r"showTab=\w+", f"showTab={args.mode}", url)
        else:
            url += ("&" if "?" in url else "?") + f"showTab={args.mode}"

    start_ts = end_ts = None
    if args.interval != "all" and "|" in args.interval:
        a, b = args.interval.split("|", 1)
        start_ts = int(time.mktime(time.strptime(a.strip(), "%Y-%m-%d")))
        end_ts = int(time.mktime(time.strptime(b.strip(), "%Y-%m-%d"))) + 86399

    lock = acquire_lock(args.out)
    try:
        # 先建立"已完成"视图：索引记录 + 旧命名文件（后者不必等 harvest 就能判定）
        done_keys = {r[0] for r in index.rows() if r[6] == "done" and os.path.exists(r[4])}
        legacy = legacy_map(args.out)

        def item_done(it):
            aid = str(it.get("aweme_id") or "")
            if not aid:
                return True
            n = len(it.get("images") or [])
            pairs = [(f"{aid}:{i}", i) for i in range(1, n + 1)] if n else [(f"{aid}:0", 0)]
            for key, pos in pairs:
                if key in done_keys:
                    continue
                if any(os.path.exists(p) for p in legacy.get((aid[-8:], pos), [])):
                    continue
                return False
            return True

        def enough(cur):
            # 关键：按"新增作品数"决定是否继续翻页，而不是按"已采集总数"。
            # max=0 表示全量，一直翻到底。
            if not args.max:
                return False
            return sum(1 for x in cur.values() if not item_done(x)) >= args.max

        log(f"打开页面：{url}")
        items, sec = harvest(url, cookies, headless=not args.headful, mode=args.mode, enough=enough)
        if not items and not args.headful:
            log("无头模式未取到数据，改用可见窗口重试…")
            items, sec = harvest(url, cookies, headless=False, mode=args.mode, enough=enough)

        if sec:
            log(f"账号 ID：{sec}")

        adopted = adopt_legacy(args.out, index, items)
        if adopted:
            log(f"已把 {adopted} 个历史文件纳入索引（旧命名），不会重复下载")

        done_keys = {r[0] for r in index.rows() if r[6] == "done" and os.path.exists(r[4])}

        records = []
        for it in sorted(items.values(), key=lambda x: int(x.get("create_time") or 0), reverse=True):
            ts = int(it.get("create_time") or 0)
            if start_ts and ts and (ts < start_ts or ts > end_ts):
                continue
            rec = build_jobs(it, args.out)
            if rec and rec["jobs"]:
                records.append(rec)

        new_records = [r for r in records if not all(j[0] in done_keys for j in r["jobs"])]
        skipped = len(records) - len(new_records)
        picked = new_records[:args.max] if args.max else new_records

        log(f"共扫描 {len(records)} 个作品：已下载 {skipped} 个，新增 {len(new_records)} 个")
        log(f"本次下载 {len(picked)} 个")
        if not picked:
            log("没有新作品需要下载。")
            return 0

        ok = skip = fail = 0
        referer = "https://www.douyin.com/"
        ck = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        with httpx.Client(timeout=120, follow_redirects=True, headers={"Cookie": ck}) as client:
            for i, rec in enumerate(picked, 1):
                for key, pos, kind, dest, u in rec["jobs"]:
                    if index.is_done(key):
                        skip += 1
                        continue
                    res, size = download(client, u, dest, referer)
                    name = os.path.basename(dest)
                    if res == "ok":
                        index.mark(key, rec["id"], pos, kind, dest, size, "done")
                        ok += 1
                        log(f"[{i}/{len(picked)}] 完成 {name}")
                    else:
                        fail += 1
                        log(f"[{i}/{len(picked)}] 失败 {name} {res}")

        log()
        log(f"下载完成：成功 {ok}，已存在跳过 {skip}，作品级跳过 {skipped}，失败 {fail}")
        log(f"保存位置：{os.path.abspath(args.out)}")
        return 0 if fail == 0 else 3
    finally:
        release_lock(lock)
        index.close()


if __name__ == "__main__":
    sys.exit(main())
