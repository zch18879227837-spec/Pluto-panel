"""PLUTO panel · 抖音 PC 客户端屏幕视觉驱动（v1.16.0）

只做三件事，全部限定在抖音窗口内部：
  1. 打开本地抖音 PC 客户端（如果没开）
  2. 在抖音窗口内下发滚轮/按键消息切下一个视频（不移动物理鼠标、不抢焦点）
  3. 截取抖音窗口画面回传，并用 OCR 识别画面元素（不写死任何像素坐标）

设计约束（与面板其余模块完全解耦）：
  - 不改配置、不自动修复：只读窗口状态 + 截图 + 单次动作
  - 窗口最小化 / 被别的窗口盖住 / 不在屏幕上 → 立即停手并上报
  - 只发滚动与翻页键，绝不点击画面，因此不可能误点赞、评论、关注
  - 任何异常都吞掉并返回结构化结果，不影响机器人的消息主线程
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
import pathlib
from typing import Any, Callable, Optional

try:
    import win32api
    import win32con
    import win32gui
    import win32process
    import win32ui
    from ctypes import wintypes
    HAS_WIN32 = True
except Exception:  # pragma: no cover - 非 Windows / 缺 pywin32
    HAS_WIN32 = False

try:
    from PIL import Image
    HAS_PIL = True
except Exception:  # pragma: no cover
    HAS_PIL = False

# --------------------------------------------------------------------------- #
# OCR：Windows 自带 OCR（离线，支持中文）。缺 winsdk 时降级为「只能看不能认」。
# --------------------------------------------------------------------------- #
HAS_OCR = False
try:
    from winsdk.windows.graphics.imaging import BitmapDecoder  # noqa: F401
    from winsdk.windows.media.ocr import OcrEngine  # noqa: F401
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream  # noqa: F401
    HAS_OCR = True
except Exception:
    pass

USER32 = ctypes.windll.user32 if sys.platform == "win32" else None
DOUYIN_PROMPT = """【抖音 PC 客户端 · 内置规则】
你能操作的是本机的抖音 PC 客户端，方式是「看屏幕 + 只发滚动消息」，不是网页版，也没有无头浏览器。
- 能做：打开客户端、看窗口状态、截一张画面并识别、在抖音窗口里往下滑一个视频。
- 不能做：点赞、评论、收藏、转发、关注、私信、搜索、改设置、点画面里任何按钮。
- 一次性：每次动作只做一下，做完立刻停；绝不为了"多刷几条"自己连续循环。
- 先复述再动手：管理员下指令后先用自己的语气复述一遍要做什么，等对方回「确认」才执行。
- 挡住就停：窗口被最小化、被别的窗口盖住、或识别不到界面元素时，立刻停下并说明原因，不要瞎点。
- 不打扰人：只往抖音窗口里发消息，不移动系统鼠标、不抢别的窗口焦点、不遮挡主人正在用的画面。
- 画布之外的窗口一律不碰；失败就说失败，不要编造"已经刷了 N 条"。"""

DWMWA_EXTENDED_FRAME_BOUNDS = 9
PW_RENDERFULLCONTENT = 0x00000002
_DPI_READY = False


def ensure_dpi_aware() -> None:
    """高 DPI 屏幕上，非 DPI-aware 进程拿到的坐标是「逻辑像素」而 DWM 返回物理像素，
    两者混用会让取点跑到屏幕外（WindowFromPoint 返回 0，被误判成遮挡）。
    这里把本进程切成 per-monitor DPI aware，保证坐标与截图同一套。
    """
    global _DPI_READY
    if _DPI_READY or sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _DPI_READY = True


@dataclass
class Hit:
    """OCR 命中的一个词：文本 + 在窗口内的相对位置（0~1）。"""

    text: str
    x: float
    y: float
    w: float
    h: float

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "x": round(self.x, 3), "y": round(self.y, 3),
                "w": round(self.w, 3), "h": round(self.h, 3)}


@dataclass
class WindowState:
    found: bool = False
    hwnd: int = 0
    elevated: bool = False        # 目标进程是不是以管理员权限运行（UIPI 会拒绝我们发消息）
    title: str = ""
    visible: bool = False
    minimized: bool = False
    foreground: bool = False
    covered: float = 0.0          # 被其它窗口盖住的比例 0~1
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found, "hwnd": self.hwnd, "title": self.title,
            "visible": self.visible, "minimized": self.minimized,
            "foreground": self.foreground, "covered": round(self.covered, 2),
            "rect": list(self.rect), "message": self.message,
            "elevated": self.elevated,
            "usable": (self.found and self.visible and not self.minimized
                       and self.covered < 0.35 and not self.elevated),
        }


class DouyinPC:
    """抖音 PC 客户端驱动。所有方法都不抛异常，只返回结构化结果。"""

    def __init__(self, data_dir: Path, settings_get: Callable[[], dict], dbg: Optional[Callable[[str], None]] = None):
        self.data_dir = Path(data_dir)
        self.capture_dir = self.data_dir / "douyin_captures"
        self._settings_get = settings_get
        self._dbg = dbg or (lambda *_: None)
        self._last_hash = ""

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def cfg(self) -> dict[str, Any]:
        try:
            dy = self._settings_get() or {}
        except Exception:
            dy = {}
        return {
            "app_path": str(dy.get("app_path") or r"D:\抖音\douyin\douyin.exe"),
            "window_keyword": str(dy.get("window_keyword") or "抖音"),
            "process_name": str(dy.get("process_name") or "douyin.exe"),
            "launch_timeout": int(dy.get("launch_timeout") or 25),
            "scroll_ticks": int(dy.get("scroll_ticks") or 3),
        }

    # ------------------------------------------------------------------ #
    # 窗口发现 / 状态
    # ------------------------------------------------------------------ #
    # 浏览器里的网页版也有「抖音」字样，绝不能被当成客户端
    BROWSER_PROCS = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
                     "opera.exe", "360se.exe", "qqbrowser.exe", "safari.exe")

    def _enum_top_windows(self) -> list[tuple[int, str, str]]:
        """枚举顶层窗口 → [(hwnd, 进程名小写, 标题)]。"""
        if not HAS_WIN32:
            return []
        rows: list[tuple[int, str, str]] = []

        def cb(hwnd, _):
            try:
                if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                    return True
                if win32gui.GetParent(hwnd) != 0:
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                name = ""
                try:
                    import psutil  # 可选依赖
                    name = (psutil.Process(pid).name() or "").lower()
                except Exception:
                    name = ""
                title = win32gui.GetWindowText(hwnd) or ""
                if title:
                    rows.append((hwnd, name, title))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, None)
        return rows

    def _windows(self) -> list[int]:
        """先按进程名精确找客户端；找不到才用标题兜底，且排除浏览器窗口。"""
        proc = self.cfg()["process_name"].lower()
        keyword = self.cfg()["window_keyword"]
        rows = self._enum_top_windows()
        by_proc = [hwnd for hwnd, name, _ in rows if proc and (name == proc or proc in name)]
        if by_proc:
            return by_proc
        out = []
        for hwnd, name, title in rows:
            if name in self.BROWSER_PROCS:
                continue
            if keyword and keyword in title:
                out.append(hwnd)
        return out

    def find_window(self) -> Optional[int]:
        wins = self._windows()
        if not wins:
            return None

        def score(hwnd: int) -> tuple[int, int]:
            """有标题的主窗口优先，其次比面积（抖音还有一个无标题的大壳窗口）。"""
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                area = max(0, r - l) * max(0, b - t)
            except Exception:
                area = 0
            try:
                titled = 1 if (win32gui.GetWindowText(hwnd) or "").strip() else 0
            except Exception:
                titled = 0
            return (titled, area)

        return max(wins, key=score)

    def _frame_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """DWM 扩展边框（不含阴影），失败退回 GetWindowRect。"""
        try:
            rect = wintypes.RECT()
            ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(rect), ctypes.sizeof(rect))
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
        return win32gui.GetWindowRect(hwnd)

    def _covered_ratio(self, hwnd: int) -> float:
        """在窗口内部取样，看有多少点被别的窗口挡住（不移动鼠标，纯查询）。"""
        try:
            l, t, r, b = self._frame_rect(hwnd)
            if r - l < 40 or b - t < 40:
                return 1.0
            pts = []
            for fx in (0.12, 0.3, 0.5, 0.7, 0.88):
                for fy in (0.12, 0.3, 0.5, 0.7, 0.88):
                    # 画面上方留出标题栏
                    px = int(l + (r - l) * fx)
                    py = int(t + (b - t) * (0.06 + 0.92 * fy))
                    pts.append((px, py))
            # 按「进程」判断遮挡：抖音是 Electron，自己有多个顶层窗口（面板/提示层），
            # 只比 hwnd 会把这些自家窗口误判成遮挡。
            _, target_pid = win32process.GetWindowThreadProcessId(hwnd)
            blocked = 0
            for px, py in pts:
                try:
                    hit = ctypes.windll.user32.WindowFromPoint(wintypes.POINT(px, py))
                    if not hit:
                        blocked += 1
                        continue
                    root = ctypes.windll.user32.GetAncestor(wintypes.HWND(hit), 2) or hit
                    _, pid = win32process.GetWindowThreadProcessId(int(root))
                    if int(pid) != int(target_pid):
                        blocked += 1
                except Exception:
                    blocked += 1
            return blocked / max(1, len(pts))
        except Exception:
            return 0.0

    def state(self) -> WindowState:
        ensure_dpi_aware()
        st = WindowState()
        if not HAS_WIN32:
            st.message = "缺少 pywin32，无法读取窗口"
            return st
        try:
            hwnd = self.find_window()
            if not hwnd:
                st.message = "抖音 PC 客户端没有运行（或没有可见窗口）"
                return st
            st.found = True
            st.hwnd = hwnd
            st.title = win32gui.GetWindowText(hwnd) or ""
            st.visible = bool(win32gui.IsWindowVisible(hwnd))
            st.minimized = bool(win32gui.IsIconic(hwnd))
            st.foreground = win32gui.GetForegroundWindow() == hwnd
            st.rect = self._frame_rect(hwnd)
            if st.visible and not st.minimized:
                st.covered = self._covered_ratio(hwnd)
            # 提权进程会拒绝我们的 PostMessage（UIPI 拦截，错误 5）。这不是"看不见窗口"，
            # 直接拿一条无害的 WM_NULL 试发一下：被拒就说明这个窗口我们根本操作不了。
            try:
                import win32api
                import win32con
                l, t, r, b = st.rect
                lp = (((t + b) // 2) << 16) | (((l + r) // 2) & 0xFFFF)
                # 位移为 0 的滚轮消息：不会真的滚动，但和真正翻页走同一条权限通道
                win32api.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, 0, lp)
                st.elevated = False
            except Exception:
                st.elevated = True
            if st.elevated:
                st.message = "抖音是管理员权限运行的，我没有权限往里操作"
            elif not st.visible:
                st.message = "抖音窗口被隐藏"
            elif st.minimized:
                st.message = "抖音窗口被最小化了"
            elif st.covered >= 0.35:
                st.message = "抖音窗口被其它窗口盖住了"
            else:
                st.message = "窗口可见"
            return st
        except Exception as exc:
            st.message = f"读取窗口状态失败：{type(exc).__name__}: {exc}"
            return st

    # ------------------------------------------------------------------ #
    # 打开客户端
    # ------------------------------------------------------------------ #
    def _restore(self, hwnd: int) -> bool:
        """把窗口从最小化还原并唤到前台（只在用户明确说「打开」时调用）。

        抖音是 Electron 客户端，对 ShowWindow(SW_RESTORE) 不响应（窗口还原了画面也不出来），
        必须发 WM_SYSCOMMAND/SC_RESTORE 才有效；两条路都试一遍，再确认确实不再最小化。
        """
        if not HAS_WIN32:
            return False
        try:
            import win32con
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)       # 常规窗口
                win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND,   # Electron：真正有效的这条
                                     win32con.SC_RESTORE, 0)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass                                              # 前台权限被拒也不影响可见性
            for _ in range(12):
                time.sleep(0.25)
                if not win32gui.IsIconic(hwnd) and win32gui.IsWindowVisible(hwnd):
                    return True
            return not win32gui.IsIconic(hwnd)
        except Exception as exc:
            self._dbg(f"还原抖音窗口失败: {exc}")
            return False

    def open_app(self) -> dict[str, Any]:
        st = self.state()
        if st.found and st.minimized:
            # 最小化时不能直接报「已经在运行」，先还原再判断
            if self._restore(st.hwnd):
                st = self.state()
                if st.found and st.visible and not st.minimized:
                    return {"ok": True, "opened": False, "restored": True,
                            "message": "抖音窗口已经还原到前台", "state": st.as_dict()}
            return {"ok": False, "opened": False,
                    "message": "抖音窗口被最小化了，我尝试还原但没成功，你手动点一下任务栏？",
                    "state": st.as_dict()}
        if st.found and st.visible:
            return {"ok": True, "opened": False, "message": "抖音已经在运行", "state": st.as_dict()}
        path = self.cfg()["app_path"]
        if not Path(path).exists():
            return {"ok": False, "opened": False,
                    "message": f"找不到抖音客户端：{path}（在面板设置里改 app_path）"}
        try:
            subprocess.Popen([path], close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            return {"ok": False, "opened": False, "message": f"启动失败：{type(exc).__name__}: {exc}"}
        deadline = time.time() + self.cfg()["launch_timeout"]
        while time.time() < deadline:
            time.sleep(1.0)
            cur = self.state()
            if cur.found and cur.visible and not cur.minimized:
                return {"ok": True, "opened": True, "message": "抖音已打开", "state": cur.as_dict()}
        cur = self.state()
        return {"ok": False, "opened": True,
                "message": "已经发起启动，但 %d 秒内没等到可见窗口" % self.cfg()["launch_timeout"],
                "state": cur.as_dict()}

    # ------------------------------------------------------------------ #
    # 截图
    # ------------------------------------------------------------------ #
    def capture(self, hwnd: int) -> tuple[Optional["Image.Image"], dict[str, Any]]:
        if not (HAS_WIN32 and HAS_PIL):
            return None, {"ok": False, "message": "缺少 pywin32 / Pillow，无法截图"}
        try:
            ensure_dpi_aware()
            l, t, r, b = self._frame_rect(hwnd)
            w, h = r - l, b - t
            if w <= 0 or h <= 0:
                return None, {"ok": False, "message": "窗口尺寸异常"}
            hwnd_dc = win32gui.GetWindowDC(hwnd)
            src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            mem_dc = src_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(src_dc, w, h)
            mem_dc.SelectObject(bmp)
            ok = ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
            info = bmp.GetInfo()
            data = bmp.GetBitmapBits(True)
            img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), data, "raw", "BGRX", 0, 1)
            win32gui.DeleteObject(bmp.GetHandle())
            mem_dc.DeleteDC()
            src_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            if not ok or img.getbbox() is None:
                # Electron（抖音 PC）对 PrintWindow 经常返回空白，退回按窗口矩形抓屏。
                # 抓屏拿的是"屏幕上真实可见的画面"，所以上面必须先确认窗口没被挡住。
                try:
                    from PIL import ImageGrab
                    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
                    if img.getbbox() is None:
                        return None, {"ok": False, "message": "截图失败：窗口没有可读画面"}
                    return img, {"ok": True, "size": [img.width, img.height], "via": "screen"}
                except Exception as exc:
                    return None, {"ok": False,
                                  "message": f"截图失败：窗口没有可读画面（{type(exc).__name__}）"}
            return img, {"ok": True, "size": [img.width, img.height], "via": "printwindow"}
        except Exception as exc:
            return None, {"ok": False, "message": f"截图异常：{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------ #
    # OCR
    # ------------------------------------------------------------------ #
    def ocr(self, img: "Image.Image") -> list[Hit]:
        """把窗口截图丢给 Windows 自带 OCR，返回相对坐标的文本块。"""
        if not (HAS_OCR and HAS_PIL):
            return []
        try:
            import asyncio
            return asyncio.run(self._ocr_async(img))
        except Exception as exc:
            self._dbg(f"OCR 失败: {exc}")
            return []

    async def _ocr_async(self, img: "Image.Image") -> list[Hit]:
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            return []
        # 放大小图，提高小字识别率（不改变比例，坐标仍按原图换算）
        scale = 1.0
        if img.width < 900:
            scale = 900 / img.width
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(buf.getvalue())
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bitmap)
        hits: list[Hit] = []
        W = max(1, img.width)
        H = max(1, img.height)
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            r = words[0].bounding_rect
            x0, y0 = r.x, r.y
            x1 = max(wr.bounding_rect.x + wr.bounding_rect.width for wr in words)
            y1 = max(wr.bounding_rect.y + wr.bounding_rect.height for wr in words)
            hits.append(Hit(text=line.text.replace(" ", ""),
                            x=x0 / W, y=y0 / H, w=(x1 - x0) / W, h=(y1 - y0) / H))
        return hits

    # ------------------------------------------------------------------ #
    # 画面理解：只靠 OCR 识别，绝不写死坐标
    # ------------------------------------------------------------------ #
    def analyze(self, img: "Image.Image") -> dict[str, Any]:
        hits = self.ocr(img)
        texts = [h.text for h in hits]
        joined = " ".join(texts)

        login_gate = any(k in joined for k in ("登录后", "扫码登录", "验证码登录", "一键登录"))
        risk_gate = any(k in joined for k in ("安全验证", "完成验证", "拖动滑块", "请完成下列验证"))
        tabs = [t for t in texts if t in ("推荐", "关注", "朋友", "精选", "首页")]
        # 右侧互动栏是「正在看视频」最稳的信号：竖排的 点赞/评论/收藏/分享
        rail = [h for h in hits if h.x > 0.62 and 0.25 < h.y < 0.95
                and any(k in h.text for k in ("赞", "评论", "收藏", "分享", "转发"))]
        # 标题区：画面下半部分、靠左的长文本
        title_hits = [h for h in hits
                      if h.y > 0.5 and h.x < 0.62 and len(h.text) >= 4
                      and not any(k in h.text for k in ("评论", "分享", "收藏", "赞"))]
        title_hits.sort(key=lambda h: (h.y, -len(h.text)))
        titles = [h.text for h in title_hits[:4]]

        kind = "unknown"
        if risk_gate:
            kind = "risk"
        elif login_gate:
            kind = "login"
        elif rail or tabs:
            kind = "video"
        return {
            "kind": kind,
            "texts": texts[:40],
            "tabs": tabs,
            "has_rail": bool(rail),
            "title_candidates": titles,
            "hit_count": len(hits),
            "size": [img.width, img.height],
        }

    # ------------------------------------------------------------------ #
    # 单步动作：只发滚动/翻页消息，不点击、不抢焦点
    # ------------------------------------------------------------------ #
    def scroll_next(self, hwnd: int) -> dict[str, Any]:
        if not HAS_WIN32:
            return {"ok": False, "message": "缺少 pywin32，无法下发滚动"}
        try:
            l, t, r, b = self._frame_rect(hwnd)
            cx = (l + r) // 2
            cy = int(t + (b - t) * 0.55)   # 画面中部偏下，避开顶部标签栏
            ticks = max(1, min(8, int(self.cfg()["scroll_ticks"])))
            delta = -120 * ticks            # 负值 = 向下
            lparam = (cy << 16) | (cx & 0xFFFF)
            wparam = (delta << 16) & 0xFFFFFFFF
            for _ in range(2):
                win32api.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)
                time.sleep(0.12)
                # 有些版本只吃按键，补一次 PageDown（不带修饰键）
                win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_NEXT, 0)
                win32api.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_NEXT, 0)
                time.sleep(0.35)
            return {"ok": True, "message": "已下发翻页（滚轮 + PageDown）", "ticks": ticks}
        except Exception as exc:
            return {"ok": False, "message": f"下发翻页失败：{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------ #
    # 组合动作：截一张图并判断当前画面
    # ------------------------------------------------------------------ #
    def snapshot(self, save: bool = True, tag: str = "shot") -> dict[str, Any]:
        st = self.state()
        if not st.found:
            return {"ok": False, "state": st.as_dict(), "message": st.message, "error": st.message}
        if not (st.visible and not st.minimized):
            return {"ok": False, "state": st.as_dict(), "message": st.message, "error": st.message}
        if st.covered >= 0.35:
            msg = "抖音窗口不可见：被其它窗口盖住了，先把它露出来"
            return {"ok": False, "state": st.as_dict(), "message": msg, "error": msg}
        img, meta = self.capture(st.hwnd)
        if img is None or not meta.get("ok"):
            msg = meta.get("message", "截图失败")
            return {"ok": False, "state": st.as_dict(), "message": msg, "error": msg}
        info = self.analyze(img)
        digest = hashlib.md5(img.tobytes()).hexdigest()
        changed = digest != self._last_hash
        self._last_hash = digest
        path = ""
        thumb = ""
        if save:
            try:
                self.capture_dir.mkdir(parents=True, exist_ok=True)
                path = str(self.capture_dir / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png")
                img.save(path)
                small = img.copy()
                if small.width > 900:
                    small = small.resize((900, int(small.height * 900 / small.width)))
                buf = io.BytesIO()
                small.save(buf, "JPEG", quality=80)
                import base64
                thumb = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            except Exception as exc:
                self._dbg(f"保存截图失败: {exc}")
        titles = list(info.get("title_candidates") or [])
        return {"ok": True, "state": st.as_dict(), "image_path": path,
                "thumb": thumb, "analysis": info, "changed": changed,
                "message": "已截图",
                # ---- 扁平镜像：调用方（main.py）不用再拆 analysis ----
                "kind": info.get("kind") or "unknown",
                "titles": titles,
                "title": titles[0] if titles else "",
                "width": img.width,
                "height": img.height,
                "file": pathlib.Path(path).name if path else ""}

    def next_video(self) -> dict[str, Any]:
        """切下一个视频：先确认窗口可见，再滚动，再截图确认画面确实变了。"""
        st = self.state()
        if not st.found or not st.visible or st.minimized:
            return {"ok": False, "state": st.as_dict(), "message": st.message, "error": st.message}
        if st.covered >= 0.35:
            msg = "抖音窗口不可见：被其它窗口盖住了，先把它露出来"
            return {"ok": False, "state": st.as_dict(), "message": msg, "error": msg}
        before = self.snapshot(save=False)
        if not before.get("ok"):
            return before
        res = self.scroll_next(st.hwnd)
        if not res.get("ok"):
            msg = res.get("message", "翻页失败")
            return {"ok": False, "state": st.as_dict(), "message": msg, "error": msg}
        time.sleep(1.2)
        after = self.snapshot(save=True, tag="next")
        if not after.get("ok"):
            return after
        if not after.get("changed"):
            msg = "画面没有任何变化，可能已经不是视频流界面，先停手"
            return {"ok": False, "state": after["state"], "analysis": after.get("analysis"),
                    "message": msg, "error": msg, "changed": False}
        return after

    # ------------------------------------------------------------------ #
    # 自检：只读
    # ------------------------------------------------------------------ #
    def selfcheck(self) -> dict[str, Any]:
        st = self.state()
        checks = [
            {"name": "pywin32", "ok": HAS_WIN32, "level": "正常" if HAS_WIN32 else "异常",
             "detail": "已安装" if HAS_WIN32 else "缺少 pywin32，窗口操作不可用"},
            {"name": "Pillow", "ok": HAS_PIL, "level": "正常" if HAS_PIL else "异常",
             "detail": "已安装" if HAS_PIL else "缺少 Pillow，截图不可用"},
            {"name": "Windows OCR", "ok": HAS_OCR, "level": "正常" if HAS_OCR else "警告",
             "detail": "可用（离线中文识别）" if HAS_OCR else "缺 winsdk，只能截图不能识图"},
            {"name": "客户端路径", "ok": Path(self.cfg()["app_path"]).exists(),
             "level": "正常" if Path(self.cfg()["app_path"]).exists() else "警告",
             "detail": self.cfg()["app_path"]},
            {"name": "抖音窗口", "ok": st.found and st.visible and not st.minimized,
             "level": "正常" if (st.found and st.visible and not st.minimized) else "警告",
             "detail": st.message},
        ]
        worst = "正常"
        for c in checks:
            if c["level"] == "异常":
                worst = "异常"
                break
            if c["level"] == "警告":
                worst = "警告"
        return {"level": worst, "state": st.as_dict(), "checks": checks}


# --------------------------------------------------------------------------- #
# 命令行：方便在装插件前单独验证（不影响面板）
# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent
    settings = {}
    try:
        sp = Path(os.environ.get("PLUTO_DATA_DIR", "")) / "settings.json"
        if sp.exists():
            settings = json.loads(sp.read_text(encoding="utf-8")).get("douyin", {})
    except Exception:
        pass
    pc = DouyinPC(here, lambda: settings, print)
    action = argv[1] if len(argv) > 1 else "state"
    if action == "state":
        print(json.dumps(pc.state().as_dict(), ensure_ascii=False, indent=1))
    elif action == "open":
        print(json.dumps(pc.open_app(), ensure_ascii=False, indent=1)[:800])
    elif action == "shot":
        r = pc.snapshot(save=True, tag="cli")
        r.pop("thumb", None)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif action == "next":
        r = pc.next_video()
        r.pop("thumb", None)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif action == "check":
        print(json.dumps(pc.selfcheck(), ensure_ascii=False, indent=1))
    else:
        print("用法: douyin_pc.py state|open|shot|next|check")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))