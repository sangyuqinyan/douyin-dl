# 抖音批量下载器

按账号批量下载抖音**无水印**视频与图文，自动去重、增量更新。Windows 桌面图形界面 + 命令行。

> 仅用于个人备份与已授权内容的整理，请遵守平台协议与著作权法。

## 为什么用「浏览器驱动」而不是直连接口

抖音网页端的接口现在要求 `X-Gnarly` 一类**请求头签名**，纯 HTTP 方案（如 f2 等基于 `a_bogus` 的实现）
实测会被判定为 `Blocked by ArgusSecurityPlugin`，返回 403。

本项目的做法：用 Playwright 启动本机 Edge，注入你已登录的 Cookie，打开目标页面，
**由页面自己发请求完成签名**，脚本只截获返回的 JSON，再下载播放直链。
不逆向签名算法，因此平台改签名也不会失效；代价是每次运行要多花几秒启动浏览器。

## 功能

- 模式：主页作品 / 喜欢 / 我的收藏 / 合集 / 单条
- 视频取最高码率的无水印流；图文取原图
- SQLite 索引按 `aweme_id` 去重，重复运行只补新增
- 断点补漏：文件被删后重跑只补那一个
- 时间区间过滤、并发下载、失败重试
- Win11 Fluent 风格界面，自动跟随系统深浅色
- 可选命令行模式，便于写批处理

## 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 x64 |
| 浏览器 | Microsoft Edge（系统自带，无需另装 Chromium） |
| 源码运行 | Python 3.10+，且带 tkinter（官方安装包默认包含） |
| 打包 | PyInstaller 6.x |

## 快速开始（源码）

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

首次使用需要填入登录 Cookie：

1. Edge 打开 <https://www.douyin.com> 并登录
2. `F12` → 网络 → 刷新 → 点任意 `www.douyin.com` 的请求
3. 复制请求头里 `Cookie:` 冒号后的全部内容
4. 程序里点「更新 Cookie」粘贴保存

然后粘贴链接（`https://www.douyin.com/user/self?...` 也可）、选模式、填条数，点「开始下载」。

## 命令行

```bat
python app.py --cli --url "https://www.douyin.com/user/MS4wLjABAAAA..." --mode like --max 20
```

| 参数 | 说明 |
|---|---|
| `--mode` | `post` / `like` / `collection` / `mix` / `one` |
| `--max` | 本次最多**新增**多少条，`0` 为不限（已下载的不占配额） |
| `--interval` | `2025-01-01|2026-11-01`，默认 `all` |
| `--out` | 保存目录，默认 `./Download` |
| `--verify` | 校验索引与磁盘一致性 |
| `--prune` | 清掉「文件已删除」的索引记录 |
| `--rebuild` | 按磁盘文件重建索引 |

## 打包为 exe

```bat
python -m pip install pyinstaller
pyinstaller --noconfirm --onedir --noconsole --name DouyinDownloader ^
  --icon assets/app.ico --add-data "assets;assets" ^
  --collect-all playwright --collect-all sv_ttk --paths . app.py
```

产物在 `dist\DouyinDownloader\`，约 140 MB（其中 120 MB 是 Playwright 驱动）。
`--collect-all playwright` 与 `--collect-all sv_ttk` 都是必须的，否则运行时会缺文件。

## 去重机制

- 主键：`{aweme_id}:{序号}`，视频序号为 0，图集从 1 起
- 索引：`Download/.state.db`（SQLite，WAL），随目录移动仍有效
- 判定时机：在应用 `--max` **之前**过滤已完成作品，因此 max 只计新增
- 写入顺序：`.part` → 校验 ≥1KB → 原子改名 → 事务登记
- 并发：`.lock` 单实例锁，按 PID 判断是否残留

## 已知限制

- 需要登录 Cookie，通常几天到几周失效
- 私密、付费、好友可见内容不支持
- 列表靠滚动加载，账号作品很多时首次扫描较慢（约 1.4 秒/屏，单次上限 1500 条）
- 平台改版可能导致失效，届时需要更新选择器或接口路径

## 许可证

未指定许可证。如需商用或再分发，请先联系作者。
