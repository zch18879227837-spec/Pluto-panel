"""抖音网页版一体化模块（Playwright）。

职责边界：
- 只负责「浏览器里的事」：打开/登录/保持 cookie、拉私信、发私信、刷视频、点赞、评论、取分享链接；
- 是否放行、等多久、要不要冷却，全部交给 `risk_core.RiskCore`（这里只把动作名/平台/用户传过去）；
- 不做消息分发、不写记忆、不进 AstrBot 事件流——那些由 `douyin_adapter.py` 与 main.py 负责。

所有定位都走 DOM 选择器或键盘操作（不写死坐标）；找不到控件时返回明确错误，
调用方据此停手并提示管理员，绝不猜测着点。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

DOUYIN_WEB_PROMPT = """抖音网页版规则（管理员视角）：
- 只有管理员能让我刷视频、点赞、评论、分享，而且每次动手前我会把要做的事复述一遍，等他确认。
- 我不会自己循环刷，也不会自动点赞、评论、关注、私信陌生人。
- 所有动作都先问公共风控：它说等我就等，它说冷却我就停。
- 窗口找不到、页面变了、要求登录时，我立刻停手并说清楚原因。
"""

HOME = "https://www.douyin.com/"
FEED = "https://www.douyin.com/?recommend=1"
MESSAGES = "https://www.douyin.com/messages"

CLEAN_RE = re.compile(r"(\s*@\S+)|(\[\S+?表情\])|(\[[\w\u4e00-\u9fff]{1,6}\])")


_TIME_RE = re.compile(
    r"^(刚刚|\d+\s*秒前|\d+\s*分钟前|\d+\s*小时前|\d+\s*天前|昨天|前天|星期[一二三四五六日]|"
    r"\d{1,2}:\d{2}|\d{4}[-/]\d{1,2}[-/]\d{1,2}.*)$"
)
_STATE_WORDS = {"在线", "离线", "已读", "未读", "对方正在输入", "置顶", ""}

# 会话列表里那行「10分钟内在线 / 昨天在线 / 6小时内在线」是**在线状态**，不是消息正文。
# 之前没排除掉，结果每次轮询都把状态行当成用户发言，模型就一直在回「两小时了」「六小时了」。
_PRESENCE_RE = re.compile(
    r"^(刚刚|\d+\s*(秒|分钟|小时|天|周|个月)\s*(内|前)?\s*在?线?|"
    r"昨天\s*在?线?|前天\s*在?线?|\d{1,2}:\d{2}\s*在?线?|"
    r"最近\s*在?线?|当前在线|离线|忙碌|离开)$"
)


def _split_conversation(raw: str) -> tuple[str, str] | None:
    """会话项整块文本 → (会话名, 最后一条正文)。取不到正文就返回 None。"""
    lines = [x.strip() for x in (raw or "").split("\n") if x.strip()]
    if not lines:
        return None
    name = lines[0]
    rest = [
        ln
        for ln in lines[1:]
        if not _TIME_RE.match(ln) and not _PRESENCE_RE.match(ln) and ln not in _STATE_WORDS
    ]
    body = ""
    for ln in reversed(rest):
        body = ln
        break
    return name, body


def clean_text(text: str) -> str:
    """过滤 @标签、表情占位、多余换行——抖音侧消息专用。"""
    if not text:
        return ""
    out = CLEAN_RE.sub(" ", str(text))
    out = out.replace("\u200b", "").replace("\xa0", " ")
    out = re.sub(r"\n{2,}", "\n", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def video_key(url: str, title: str) -> str:
    match = re.search(r"/video/(\d+)", url or "")
    if match:
        return match.group(1)
    return (title or "")[:40]


class DouyinWeb:
    def __init__(
        self,
        data_dir: Path,
        settings_get: Callable[[], dict],
        dbg: Callable[[str], None] | None = None,
        log: Callable[..., None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self._settings = settings_get
        self._dbg = dbg or (lambda msg: None)
        self._log = log
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()
        self._last_mid: str = ""
        self._seen_msg: dict[str, float] = {}
        self._sent_texts: list[str] = []  # 自己刚发出去的话，用来过滤回声
        self._current: dict[str, Any] = {}
        self._profile = self.data_dir / "douyin_profile"
        self._cookies = self.data_dir / "douyin_cookies.json"

    # ------------------------------------------------------------ 基础
    def cfg(self) -> dict[str, Any]:
        raw = self._settings() or {}
        return {
            "enabled": bool(raw.get("enabled", False)),
            "headless": bool(raw.get("headless", True)),
            "profile_dir": str(self._profile),
            "login_timeout": int(raw.get("login_timeout") or 180),
            "wait_video": float(raw.get("wait_video") or 4.0),
        }

    def _kill_profile_procs(self) -> int:
        """杀掉命令行里带本 profile 目录的 chrome 进程（重载插件后旧浏览器会占着锁）。"""
        killed = 0
        try:
            import psutil  # 可选依赖：没装就只清锁文件
        except Exception:
            return 0
        key = str(self._profile).lower()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "chrome" not in name and "chromium" not in name:
                    continue
                cmd = " ".join(proc.info.get("cmdline") or []).lower()
                if key in cmd:
                    proc.kill()
                    killed += 1
            except Exception:
                continue
        if killed:
            time.sleep(1.0)
            self._dbg(f"清理了 {killed} 个残留浏览器进程")
        return killed

    def _clear_singleton(self) -> None:
        """删掉 Chromium 留下的 Singleton* 锁文件（进程被杀后它们会残留）。"""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = self._profile / name
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except Exception:
                pass


    def _login_ok_hint(self) -> bool:
        """粗判：cookie 文件里出现过登录态标记，就不再强制有头。"""
        try:
            p = self._cookies_path
            if not p.exists():
                return False
            raw = p.read_text(encoding="utf-8", errors="ignore")
            return "sessionid" in raw or "sid_tt" in raw
        except Exception:
            return False

    async def _ensure(self) -> None:
        if self._ctx is not None:
            return
        from playwright.async_api import async_playwright  # 延迟导入：没装 Playwright 也不影响插件启动

        self._pw = await async_playwright().start()
        self._profile.mkdir(parents=True, exist_ok=True)
        headless = bool(self.cfg()["headless"])
        if getattr(self, "_force_headless", None) is not None:
            headless = bool(self._force_headless)
        # 没登录成功过就别用无头：验证码中间页在无头里看不见也过不去，
        # 一旦卡在 captcha，整个私信/刷视频链路就静默失效。
        if headless and not self._login_ok_hint():
            headless = False
        # 上一次进程被强杀时 profile 里会留下 Singleton 锁，直接启动会报
        # "Target page, context or browser has been closed"，先清掉再启。
        self._kill_profile_procs()
        self._clear_singleton()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled",
                "--window-size=1340,880",
                "--window-position=80,60", "--no-first-run"],
            viewport=None if not headless else {"width": 1440, "height": 900},
        )
        pages = [p for p in self._ctx.pages if not p.is_closed()]
        self._page = pages[0] if pages else await self._ctx.new_page()
        # 新启动的上下文里 pages[0] 往往就是 about:blank：先导航到首页，
        # 否则后续所有 DOM 查询都落在空白页上（表现为「找不到消息入口」）。
        try:
            if not (self._page.url or "").startswith("http"):
                await self._page.goto(HOME, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(1.2)
        except Exception as exc:
            self._dbg(f"首页导航失败：{exc}")
        self._dbg("抖音网页浏览器已启动")

    async def start(self, headless: bool | None = None) -> dict[str, Any]:
        async with self._lock:
            try:
                await self._ensure()
                if headless is not None:
                    self._dbg(f"headless 参数 {headless} 需重启浏览器才生效")
                await self._goto(HOME)
                return {"ok": True, **await self._status_locked()}
            except Exception as exc:
                self._dbg(f"启动浏览器失败：{exc}")
                return {"ok": False, "message": f"启动浏览器失败：{exc}"}

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            await self._close_locked()
            return {"ok": True}

    async def _close_locked(self) -> None:
        self._force_headless = None
        self._login_state = ""
        try:
            if self._ctx is not None:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None
        self._page = None


    def _pick_page(self):
        """在持久化上下文里挑「真正挂着抖音」的页面。

        插件重载/浏览器重启后 pages[0] 经常是 about:blank，
        继续用它会得到空 DOM（表现为「找不到消息入口」）。
        """
        ctx = self._ctx
        if ctx is None:
            return None
        try:
            pages = [p for p in ctx.pages if not p.is_closed()]
        except Exception:
            pages = []
        for p in pages:
            try:
                if "douyin.com" in (p.url or ""):
                    self._page = p
                    return p
            except Exception:
                continue
        if pages:
            self._page = pages[0]
            return self._page
        return None

    async def _goto(self, url: str) -> None:
        page = self._pick_page()
        if page is None:
            raise RuntimeError("浏览器还没起来")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(1.2)

    # ------------------------------------------------------------ 状态/登录
    async def _gate_reason(self) -> str:
        """判断当前页面是不是「过不去」的状态：验证码、登录墙。返回 ''/captcha/login。"""
        page = self._pick_page()
        if page is None:
            return ""
        try:
            if await self._playing():
                return ""
            title = (await page.title()) or ""
            url = page.url or ""
        except Exception:
            return ""
        if "验证码" in title or "安全验证" in title:
            return "captcha"
        if "login" in url or "passport" in url:
            return "login"
        try:
            box = await page.query_selector(
                '[data-e2e="login-modal"], [class*="login-container"], [class*="login-panel"]'
            )
            if box:
                return "login"
        except Exception:
            pass
        return ""

    async def _logged_in_locked(self) -> bool:
        if await self._gate_reason():
            return False
        page = self._pick_page()
        if page is None:
            return False
        # 已经在播放某条视频（弹窗全屏）时页头是隐藏的，按按钮判断必然误判
        if await self._playing():
            return True
        try:
            # 刚重载插件时页面还停在 about:blank，先回首页再判断
            if "douyin.com" not in (page.url or ""):
                if not (self.data_dir / "douyin_cookies.json").exists():
                    return False
                await self._goto(HOME)
                await asyncio.sleep(2.0)
            info = await page.evaluate(
                """() => {
                  const hits = [...document.querySelectorAll('button,div,span,a')]
                    .filter((el) => {
                      const t = (el.innerText || '').trim();
                      if (t !== '登录' && t !== '立即登录') return false;
                      const r = el.getBoundingClientRect();
                      return r.y < 140 && r.width > 8 && r.width < 120;
                    });
                  const avatar = !!document.querySelector(
                    '[data-e2e="user-avatar"], img[class*="avatar"], [class*="avatar"] img');
                  const txt = document.body.innerText || '';
                  return {hasLogin: hits.length > 0, avatar,
                          hasMsg: txt.indexOf('消息') >= 0};
                }"""
            )
            if info.get("hasLogin"):
                return False
            return bool(info.get("avatar") or info.get("hasMsg"))
        except Exception:
            return False

    async def _status_locked(self) -> dict[str, Any]:
        page = self._pick_page()
        gate = await self._gate_reason()
        title = ""
        try:
            title = (await page.title()) if page else ""
        except Exception:
            title = ""
        logged = False
        if page and not gate:
            logged = await self._logged_in_locked()
        msg = ""
        login_state = getattr(self, "_login_state", "")
        if self._ctx is None:
            msg = "浏览器未启动"
        elif login_state == "waiting" and not logged:
            msg = "登录窗口已打开，等着扫码"
        elif login_state == "timeout" and not logged:
            msg = "等登录超时了，可以再点一次扫码登录"
        elif gate == "captcha":
            msg = "卡在验证码页，去有头窗口手动过一下"
        elif gate == "login":
            msg = "未登录，点「扫码登录」"
        elif not logged:
            msg = "没认出登录态，可能还没登录"
        return {
            "running": self._ctx is not None,
            "logged_in": bool(logged),
            "gate": gate,
            "title": title[:60],
            "message": msg,
            "url": (page.url if page else ""),
            "cookies_at": str(self._cookies) if self._cookies.exists() else "",
            "profile": str(self._profile),
            "headless": self.cfg()["headless"] if getattr(self, "_force_headless", None) is None else bool(self._force_headless),
            "login_state": login_state,
        }

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            if self._ctx is None:
                return {"running": False, "logged_in": False, "message": "浏览器未启动"}
            return await self._status_locked()

    async def open_login(self) -> dict[str, Any]:
        """拉起有头窗口让管理员扫码：立即返回，登录状态由后台任务盯着。"""
        async with self._lock:
            try:
                cfg = self.cfg()
                need_relaunch = self._ctx is not None and bool(cfg["headless"])
                if need_relaunch:
                    await self._close_locked()
                self._force_headless = False  # 登录窗口必须看得见
                await self._ensure()
                await self._goto(HOME)
                await self._open_login_modal()
                if await self._logged_in_locked():
                    await self._save_cookies()
                    self._login_state = "ok"
                    return {"ok": True, "logged_in": True, "message": "已经是登录状态"}
                self._login_state = "waiting"
                asyncio.create_task(self._watch_login(float(cfg["login_timeout"])))
                return {
                    "ok": True,
                    "logged_in": False,
                    "opened": True,
                    "message": "登录窗口已打开，请在弹出的浏览器里扫码；扫完这里会自动变成已登录",
                }
            except Exception as exc:
                self._login_state = "error"
                return {"ok": False, "message": f"打开登录失败：{exc}"}

    async def make_headful(self) -> None:
        """需要人肉过验证码时，把有头窗口拉起来（无头状态则重启一次）。"""
        async with self._lock:
            if self._ctx is not None and not self.cfg().get("headless", True):
                return
            if self._ctx is not None:
                await self._close_locked()
            self._force_headless = False
            await self._ensure()

    async def ensure(self) -> None:
        """对外：需要时把浏览器拉起来（幂等）。"""
        async with self._lock:
            await self._ensure()

    async def debug_home(self) -> dict[str, Any]:
        """临时：回首页看登录态与私信入口。"""
        try:
            async with self._lock:
                await self._ensure()
        except Exception as exc:
            return {"ok": False, "error": f"启动浏览器失败：{exc}"}
        page = self._pick_page()
        if page is None:
            return {"ok": False, "error": "没有页面"}
        try:
            await self._goto(HOME)
            await asyncio.sleep(3.0)
            info = await page.evaluate(
                """() => {
                  const txt = document.body.innerText || '';
                  const links = [...document.querySelectorAll('a')]
                    .map((a) => ({t: (a.innerText || '').trim().slice(0, 12),
                                  h: a.getAttribute('href') || ''}))
                    .filter((x) => x.h && /消息|私信|chat|message/i.test(x.t + x.h));
                  return {url: location.href, title: document.title,
                          has_login_btn: txt.indexOf('登录') >= 0 && txt.indexOf('发布视频') < 0,
                          avatar: !!document.querySelector('img[class*="avatar"], [class*="avatar"] img'),
                          links: links.slice(0, 12)};
                }"""
            )
            shot = await self.debug_shot("home-probe")
            return {"ok": True, "info": info, "shot": shot}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def debug_feed(self) -> dict[str, Any]:
        """临时：先点开首页第一张卡片，看进入的是不是竖向播放器。"""
        try:
            async with self._lock:
                await self._ensure()
                page = self._pick_page()
                await self._goto(FEED)
                await asyncio.sleep(3.0)
                before = await page.evaluate("() => location.href")
                hit = await page.evaluate(
                    """() => {
                      const cards = [...document.querySelectorAll('img')]
                        .filter((el) => {
                          const r = el.getBoundingClientRect();
                          return r.width > 150 && r.height > 120;
                        })
                        .map((el) => el.closest('div,li,a') || el.parentElement);
                      if (!cards.length) return null;
                      const r = cards[0].getBoundingClientRect();
                      return {x: r.x + r.width / 2, y: r.y + r.height / 2,
                              w: Math.round(r.width), h: Math.round(r.height), n: cards.length};
                    }"""
                )
                clicked = False
                if hit:
                    await page.mouse.click(hit["x"], hit["y"])
                    clicked = True
                    await asyncio.sleep(4.0)
                after = await page.evaluate(
                    """() => {
                      const v = [...document.querySelectorAll('video')].map((el) => {
                        const r = el.getBoundingClientRect();
                        return {w: Math.round(r.width), h: Math.round(r.height),
                                src: (el.currentSrc || '').slice(0, 70)};
                      });
                      return {url: location.href,
                              vids: v,
                              hasLike: !!document.querySelector('[data-e2e="video-like"], [aria-label*="赞"]')};
                    }"""
                )
                shot = await self.debug_shot("feed-click")
                return {"ok": True, "before": before, "hit": hit, "clicked": clicked,
                        "after": after, "shot": shot}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def debug_dm_entry(self) -> dict[str, Any]:
        """临时：回首页，找「消息/私信」入口并真点一次，看跳到哪。"""
        try:
            async with self._lock:
                await self._ensure()
                page = self._pick_page()
                await self._goto(HOME)
                await asyncio.sleep(3.0)
                cands = await page.evaluate(
                    """() => {
                      const want = ['消息','私信','聊天'];
                      const out = [];
                      document.querySelectorAll('a,button,div,span,li').forEach((el) => {
                        const t = (el.innerText || '').trim();
                        if (!t || t.length > 6) return;
                        if (!want.some((w) => t.indexOf(w) >= 0)) return;
                        const r = el.getBoundingClientRect();
                        if (r.width < 8 || r.height < 8) return;
                        out.push({t, x: r.x + r.width / 2, y: r.y + r.height / 2,
                                  tag: el.tagName, href: el.getAttribute('href') || ''});
                      });
                      return out.slice(0, 10);
                    }"""
                )
                shot1 = await self.debug_shot("dm-home")
                clicked = None
                if cands:
                    c = cands[0]
                    await page.mouse.click(c["x"], c["y"])
                    await asyncio.sleep(3.5)
                    clicked = c
                url = page.url
                shot2 = await self.debug_shot("dm-after")
                return {"ok": True, "cands": cands, "clicked": clicked,
                        "url": url, "shots": [shot1, shot2],
                        "title": await page.title()}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def debug_shot(self, tag: str = "shot") -> str:
        """临时：把当前页面截一张图，返回路径。"""
        page = self._pick_page()
        if page is None:
            return ""
        try:
            out = self.data_dir / "douyin_captures" / f"{tag}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out))
            return str(out)
        except Exception as exc:
            return f"失败：{exc}"


    async def debug_header(self) -> dict[str, Any]:
        """调试：列出页头右上角所有可能点得动的元素，便于定位「消息」入口。"""
        async with self._lock:
            page = self._pick_page()
            if page is None:
                return {"ok": False, "message": "浏览器没启动"}
            try:
                data = await page.evaluate(
                    """() => {
                      const vw = window.innerWidth, vh = window.innerHeight;
                      const out = [];
                      document.querySelectorAll('*').forEach((el) => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 12 || r.height < 12) return;
                        if (r.x < vw * 0.72 || r.y > vh * 0.22) return;
                        const cls = (el.className && el.className.toString()) || '';
                        const txt = (el.innerText || '').trim().slice(0, 20);
                        const aria = el.getAttribute('aria-label') || '';
                        const title = el.getAttribute('title') || '';
                        if (!txt && !aria && !title && !cls) return;
                        out.push({tag: el.tagName, cls: cls.slice(0, 90), txt, aria, title,
                                  x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
                                  w: Math.round(r.width), h: Math.round(r.height)});
                      });
                      return {vw, vh, items: out.slice(0, 200)};
                    }"""
                )
                return {"ok": True, **data}
            except Exception as exc:
                return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


    async def debug_drawer(self) -> dict[str, Any]:
        """调试：尝试打开私信抽屉，返回点击前后的判定结果与截图路径。"""
        async with self._lock:
            page = self._pick_page()
            if page is None:
                return {"ok": False, "message": "浏览器没启动"}
            before = await self._msg_drawer_open()
            await self._click_text(["消息", "私信"])
            await asyncio.sleep(2.2)
            after = await self._msg_drawer_open()
            shot = await self.debug_shot("drawer-after")
            return {"ok": True, "before": before, "after": after, "shot": shot,
                    "url": page.url}




    async def debug_composer(self) -> dict[str, Any]:
        """调试：点开第一段会话，列出输入框/发送按钮候选。"""
        async with self._lock:
            page = self._pick_page()
            if page is None:
                return {"ok": False, "message": "浏览器没启动"}
            try:
                if not await self._open_msg_drawer():
                    return {"ok": False, "message": "抽屉没打开"}
                pos = await page.evaluate(
                    """() => {
                      const vw = window.innerWidth, out = [];
                      document.querySelectorAll('[class*="conversation"], [class*="session"]').forEach((el) => {
                        const r = el.getBoundingClientRect();
                        const t = (el.innerText || '').trim();
                        if (!t || r.width < 80 || r.height < 24 || r.x < vw * 0.55) return;
                        out.push({x: r.x + r.width / 2, y: r.y + r.height / 2, t: t.slice(0, 24)});
                      });
                      return out.slice(0, 5);
                    }"""
                )
                if pos:
                    await page.mouse.dblclick(pos[0]["x"], pos[0]["y"])
                    await asyncio.sleep(2.5)
                data = await page.evaluate(
                    """() => {
                      const out = {url: location.href, editors: [], texts: [], areas: []};
                      document.querySelectorAll('[contenteditable], textarea, input').forEach((el) => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 40 || r.height < 16) return;
                        out.editors.push({tag: el.tagName, ce: el.getAttribute('contenteditable'),
                                          ph: el.getAttribute('placeholder') || '',
                                          cls: ((el.className && el.className.toString()) || '').slice(0, 60),
                                          x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
                                          w: Math.round(r.width), h: Math.round(r.height)});
                      });
                      document.querySelectorAll('*').forEach((el) => {
                        const t = (el.innerText || '').trim();
                        if (t !== '发送' && t !== '发送消息' && t !== '发消息') return;
                        const r = el.getBoundingClientRect();
                        if (r.width < 10) return;
                        out.texts.push({t, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
                      });
                      out.areas = [...document.querySelectorAll('[class*="chat" i], [class*="message" i]')]
                        .slice(0, 8).map((el) => {
                          const r = el.getBoundingClientRect();
                          return {cls: ((el.className && el.className.toString()) || '').slice(0, 60),
                                  x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
                        });
                      return out;
                    }"""
                )
                shot = await self.debug_shot("composer")
                return {"ok": True, "shot": shot, **data}
            except Exception as exc:
                return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


    async def debug_click(self, text: str, wait: float = 2.0) -> dict[str, Any]:
        """调试：按文字点击一次并截图，返回点击前 URL 与点击后 URL。"""
        async with self._lock:
            page = self._pick_page()
            if page is None:
                return {"ok": False, "message": "浏览器没启动"}
            before = page.url
            hit = await self._click_text([text])
            await asyncio.sleep(wait)
            shot = await self.debug_shot("click-" + text[:6])
            body = ""
            try:
                body = (await page.evaluate("() => (document.body.innerText || '').slice(0, 600)")) or ""
            except Exception:
                pass
            return {"ok": True, "hit": hit, "before": before, "after": page.url,
                    "shot": shot, "body": body}

    async def debug_reply(self, text: str) -> dict[str, Any]:
        """调试：打开私信抽屉里的第一段会话，发一条消息，并回报会话 URL。"""
        async with self._lock:
            page = self._pick_page()
            if page is None:
                return {"ok": False, "message": "浏览器没启动"}
            try:
                if not await self._open_msg_drawer():
                    return {"ok": False, "message": "抽屉没打开"}
                items = await page.evaluate(
                    """() => {
                      const vw = window.innerWidth, out = [];
                      document.querySelectorAll('[class*="conversation"], [class*="session"]').forEach((el) => {
                        const r = el.getBoundingClientRect();
                        const t = (el.innerText || '').trim();
                        if (!t || r.width < 80 || r.height < 24 || r.x < vw * 0.55) return;
                        out.push({x: r.x + r.width / 2, y: r.y + r.height / 2, t: t.slice(0, 30)});
                      });
                      return out.slice(0, 5);
                    }"""
                )
                if not items:
                    return {"ok": False, "message": "没有会话"}
                first = items[0]
                await page.mouse.click(first["x"], first["y"])
                await asyncio.sleep(2.5)
                url = page.url
                box = await page.evaluate(
                    """() => {
                      const eds = [...document.querySelectorAll('div[contenteditable="true"], textarea')];
                      const vw = window.innerWidth;
                      for (const el of eds) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 120 || r.x < vw * 0.3) continue;
                        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                      }
                      return null;
                    }"""
                )
                if not box:
                    return {"ok": False, "message": "找不到输入框", "url": url}
                await page.mouse.click(box["x"], box["y"])
                await asyncio.sleep(0.4)
                await page.keyboard.type(text)
                await asyncio.sleep(0.3)
                await page.keyboard.press("Enter")
                await asyncio.sleep(1.6)
                shot = await self.debug_shot("reply-after")
                return {"ok": True, "url": url, "shot": shot, "peer": first["t"]}
            except Exception as exc:
                return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    async def debug_dom(self, needles: str = "消息,私信,message,chat") -> dict[str, Any]:
        """调试：按关键词在整页找元素（不过滤坐标），返回坐标与属性。"""
        async with self._lock:
            page = self._pick_page()
            if page is None:
                return {"ok": False, "message": "浏览器没启动"}
            try:
                data = await page.evaluate(
                    """(needles) => {
                      const words = needles.split(',').filter(Boolean);
                      const hit = [];
                      const all = document.querySelectorAll('*');
                      for (const el of all) {
                        const cls = (el.className && el.className.toString()) || '';
                        const aria = el.getAttribute('aria-label') || '';
                        const title = el.getAttribute('title') || '';
                        const href = el.getAttribute('href') || '';
                        const txt = (el.innerText || '').trim();
                        const blob = (cls + ' ' + aria + ' ' + title + ' ' + href + ' ' + txt).toLowerCase();
                        if (!words.some((w) => blob.indexOf(w.toLowerCase()) >= 0)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 8 || r.height < 8) continue;
                        hit.push({tag: el.tagName, cls: cls.slice(0, 100), aria, title,
                                  href: href.slice(0, 80), txt: txt.slice(0, 40),
                                  x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
                                  w: Math.round(r.width), h: Math.round(r.height)});
                        if (hit.length >= 40) break;
                      }
                      return {url: location.href, vw: innerWidth, vh: innerHeight,
                              bodyHead: (document.body.innerText || '').slice(0, 300), items: hit};
                    }""",
                    needles,
                )
                return {"ok": True, **data}
            except Exception as exc:
                return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    async def debug_login(self) -> dict[str, Any]:
        """调试：列出页面里含「登录」的元素，并尝试点第一个可点的。"""
        page = self._pick_page()
        if page is None:
            return {"ok": False, "error": "浏览器没启动"}
        try:
            info = await page.evaluate(
                """() => {
                  const out = [];
                  document.querySelectorAll('button,[role="button"],a,div,span').forEach((el) => {
                    const t = (el.innerText || '').trim();
                    if (!t || t.length > 16 || t.indexOf('登录') < 0) return;
                    const r = el.getBoundingClientRect();
                    out.push({ tag: el.tagName, text: t, cls: String(el.className).slice(0, 50),
                               x: Math.round(r.x), y: Math.round(r.y),
                               w: Math.round(r.width), h: Math.round(r.height) });
                  });
                  return out.slice(0, 15);
                }"""
            )
            box = await page.evaluate(
                """() => {
                  const pick = [...document.querySelectorAll('button,[role="button"]')]
                    .filter((el) => {
                      const t = (el.innerText || '').trim();
                      return (t === '登录' || t === '立即登录') &&
                             el.getBoundingClientRect().width > 0;
                    });
                  if (!pick.length) return null;
                  const r = pick[0].getBoundingClientRect();
                  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }"""
            )
            clicked = 0
            if box:
                await page.mouse.click(box["x"], box["y"])
                clicked = 1
            await asyncio.sleep(2.0)
            vp = await page.evaluate("() => ({w: innerWidth, h: innerHeight, dpr: devicePixelRatio})")
            pages = []
            for p in self._ctx.pages:
                try:
                    pages.append({"url": p.url, "title": await p.title()})
                except Exception:
                    pages.append({"url": "?", "title": "?"})
            shot = ""
            try:
                target = self._ctx.pages[-1]
                out = self.data_dir / "douyin_captures" / "login-debug.png"
                out.parent.mkdir(parents=True, exist_ok=True)
                await target.screenshot(path=str(out))
                shot = str(out)
            except Exception as exc:
                shot = f"截图失败：{exc}"
            return {"ok": True, "candidates": info, "clicked": clicked, "viewport": vp,
                    "url": page.url, "title": await page.title(),
                    "pages": pages, "shot": shot}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def _open_login_modal(self) -> bool:
        """把扫码登录框点出来。

        抖音对 JS 的 el.click() 不响应（isTrusted=false），必须用真实鼠标事件；
        按钮可能在视口外，所以先拿到坐标再 mouse.click。
        """
        page = self._pick_page()
        if page is None:
            return False
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                box = await page.evaluate(
                    """() => {
                      const pick = [...document.querySelectorAll('button,[role="button"],a')]
                        .filter((el) => {
                          const t = (el.innerText || '').trim();
                          if (t !== '登录' && t !== '立即登录') return false;
                          const r = el.getBoundingClientRect();
                          return r.width > 8 && r.height > 8;
                        });
                      if (!pick.length) return null;
                      const r = pick[0].getBoundingClientRect();
                      return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                    }"""
                )
                if box:
                    await page.mouse.click(box["x"], box["y"])
                    await asyncio.sleep(2.0)
                    shown = await page.evaluate(
                        """() => document.body.innerText.indexOf('扫码登录') >= 0"""
                    )
                    self._dbg(f"点击登录按钮 → 二维码{'已出现' if shown else '未出现'}")
                    if shown:
                        return True
                else:
                    await asyncio.sleep(1.5)
            except Exception as exc:
                self._dbg(f"点登录失败：{exc}")
                await asyncio.sleep(1.5)
        return False

    async def _watch_login(self, timeout: float) -> None:
        """后台盯登录态：扫完保存 cookie，超时就收手。"""
        try:
            waited = 0.0
            while waited < timeout:
                await asyncio.sleep(2.0)
                waited += 2.0
                if await self._logged_in_locked():
                    await self._save_cookies()
                    self._login_state = "ok"
                    self._dbg("抖音登录成功，cookie 已保存")
                    return
            self._login_state = "timeout"
            self._dbg("抖音登录等待超时")
        except Exception as exc:
            self._login_state = "error"
            self._dbg(f"登录监听失败：{exc}")

    async def _save_cookies(self) -> None:
        try:
            if self._ctx is not None:
                state = await self._ctx.storage_state()
                self._cookies.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self._dbg(f"cookie 已保存 {self._cookies}")
        except Exception as exc:
            self._dbg(f"保存 cookie 失败：{exc}")

    # ------------------------------------------------------------ 私信
    async def _click_text(self, texts: list[str], timeout: float = 8.0) -> bool:
        """在页面上按文字找可点元素，用真实鼠标点它（JS click 抖音不认）。"""
        page = self._pick_page()
        if page is None:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                box = await page.evaluate(
                    """(wants) => {
                      const hits = [];
                      document.querySelectorAll('a,button,div,span,li').forEach((el) => {
                        const t = (el.innerText || '').trim();
                        if (!t || t.length > 6) return;
                        if (!wants.some((w) => t === w)) return;
                        const r = el.getBoundingClientRect();
                        if (r.width < 6 || r.height < 6) return;
                        hits.push({x: r.x + r.width / 2, y: r.y + r.height / 2});
                      });
                      return hits[0] || null;
                    }""",
                    texts,
                )
                if box:
                    await page.mouse.click(box["x"], box["y"])
                    return True
            except Exception as exc:
                self._dbg(f"点文字失败 {texts}: {exc}")
            await asyncio.sleep(0.8)
        return False

    async def _msg_drawer_open(self) -> bool:
        """右侧私信抽屉是否已经打开（只认右半屏里的会话/空态）。"""
        page = self._pick_page()
        if page is None:
            return False
        try:
            return bool(await page.evaluate(
                """() => {
                  const vw = window.innerWidth;
                  const nodes = [...document.querySelectorAll('div,li,ul')];
                  const right = nodes.filter((el) => {
                    const r = el.getBoundingClientRect();
                    return r.x > vw * 0.55 && r.width > 180 && r.width < vw * 0.6
                           && r.height > 200;
                  });
                  if (!right.length) return false;
                  return right.some((el) => {
                    const t = el.innerText || '';
                    return t.indexOf('暂无会话') >= 0
                        || !!el.querySelector('[class*="conversation"], [class*="session"]');
                  });
                }"""
            ))
        except Exception:
            return False

    async def _click_message_entry(self) -> bool:
        """点右上角「消息」入口（它是 <UL>，不是 button，只能按坐标点）。"""
        page = self._pick_page()
        if page is None:
            return False
        try:
            pos = await page.evaluate(
                """() => {
                  const vw = window.innerWidth;
                  const cands = [...document.querySelectorAll('ul,div,a,li')];
                  for (const el of cands) {
                    const t = (el.innerText || '').trim();
                    if (t !== '消息' && t !== '私信' && t !== '消息 私信') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 16 || r.height < 12) continue;
                    if (r.x < vw * 0.6) continue;
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                  }
                  return null;
                }"""
            )
        except Exception:
            return False
        if not pos:
            return False
        await page.mouse.click(pos["x"], pos["y"])
        return True

    async def _open_msg_drawer(self) -> bool:
        """把右上角「消息」抽屉打开；已开直接返回，点不开再补一次。"""
        if await self._msg_drawer_open():
            return True
        for _ in range(3):
            if not await self._click_message_entry():
                await self._click_text(["消息", "私信"])
            await asyncio.sleep(1.8)
            if await self._msg_drawer_open():
                return True
        return False

    async def poll_messages(self) -> list[dict[str, Any]]:
        """拉一次私信（从首页「消息」抽屉），返回新消息，已去重、已清洗。"""
        async with self._lock:
            page = self._pick_page()
            if self._ctx is None or page is None:
                return []
            try:
                if not await self._logged_in_locked():
                    return []
                if "douyin.com" not in (page.url or ""):
                    await self._goto(HOME)
                    await asyncio.sleep(2.0)
                if not await self._open_msg_drawer():
                    self._dbg("没找到「消息」入口")
                    return []
                items = await page.evaluate(
                    """() => {
                      const out = [];
                      const vw = window.innerWidth;
                      document.querySelectorAll('[class*="conversation"], [class*="session"]').forEach((el) => {
                        const t = (el.innerText || '').trim();
                        if (!t || t.length < 2) return;
                        const r = el.getBoundingClientRect();
                        if (r.width < 80 || r.height < 24) return;
                        if (r.x < vw * 0.55) return;
                        const unread = !!el.querySelector('[class*="badge"], [class*="unread"]');
                        let uid = el.getAttribute('data-uid') || el.getAttribute('data-conversation-id')
                               || el.getAttribute('data-id') || '';
                        if (!uid) {
                          const a = el.querySelector('a[href*="user/"], a[href*="uid="]');
                          if (a) {
                            const m = (a.getAttribute('href') || '').match(/(?:user\/|uid=)([A-Za-z0-9_\-]{6,})/);
                            if (m) uid = m[1];
                          }
                        }
                        if (!uid) {
                          const img = el.querySelector('img[src*="user/"]');
                          if (img) {
                            const m = (img.getAttribute('src') || '').match(/user\/([A-Za-z0-9_\-]{6,})/);
                            if (m) uid = m[1];
                          }
                        }
                        out.push({text: t, unread, uid: uid, x: r.x + r.width / 2, y: r.y + r.height / 2});
                      });
                      return out.slice(0, 20);
                    }"""
                )
                fresh: list[dict[str, Any]] = []
                now = time.time()
                for it in items:
                    raw = clean_text(it.get("text") or "")
                    if not raw:
                        continue
                    parsed = _split_conversation(raw)
                    if parsed is None:
                        continue
                    name, body = parsed
                    if not body:
                        continue
                    # 自己刚发出去的话不算“收到”
                    if body in self._sent_texts:
                        continue
                    mid = str(abs(hash(name + "|" + body)) % 10**12)
                    if mid in self._seen_msg:
                        continue
                    self._seen_msg[mid] = now
                    uid = str(it.get("uid") or "").strip()
                    fresh.append({"openid": uid or name, "uid": uid, "name": name[:24],
                                  "text": body, "unread": bool(it.get("unread")), "mid": mid,
                                  "x": it.get("x"), "y": it.get("y")})
                return fresh
            except Exception as exc:
                self._dbg(f"拉私信失败：{exc}")
                return []

    async def send_dm(self, openid: str, text: str) -> dict[str, Any]:
        """给某个会话发私信：打开抽屉 → 点会话 → 输入 → 回车。"""
        async with self._lock:
            page = self._pick_page()
            if self._ctx is None or page is None:
                return {"ok": False, "message": "浏览器未启动"}
            body = clean_text(text)
            if not body:
                return {"ok": False, "message": "内容为空"}
            try:
                if not await self._open_msg_drawer():
                    return {"ok": False, "message": "没找到「消息」入口"}
                if openid:
                    hit = await page.evaluate(
                        """(name) => {
                          const nodes = [...document.querySelectorAll('[class*="conversation"], [class*="session"]')];
                          for (const el of nodes) {
                            const t = (el.innerText || '').trim();
                            if (!t.includes(name)) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 60 || r.height < 20) continue;
                            return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                          }
                          return null;
                        }""",
                        openid,
                    )
                    if hit is None:
                        return {"ok": False, "message": f"会话列表里没找到「{openid}」"}
                    await page.mouse.click(hit["x"], hit["y"])
                    await asyncio.sleep(1.5)
                box = await page.query_selector(
                    'div[contenteditable="true"], textarea[data-e2e="chat-input"], textarea'
                )
                if box is None:
                    return {"ok": False, "message": "没找到私信输入框（可能还没点开会话）"}
                await box.click()
                await page.keyboard.type(body)
                await page.keyboard.press("Enter")
                await asyncio.sleep(0.8)
                try:
                    self._sent_texts.append(body)
                    self._sent_texts = self._sent_texts[-20:]
                except Exception:
                    pass

                return {"ok": True, "message": "已发送"}
            except Exception as exc:
                return {"ok": False, "message": f"发私信失败：{exc}"}

    async def _playing(self) -> bool:
        """当前是否已经在播放某条视频（modal_id / video 页 / 有主播放器）。"""
        page = self._pick_page()
        if page is None:
            return False
        try:
            return bool(await page.evaluate(
                """() => {
                  const u = location.href;
                  if (u.indexOf('modal_id=') >= 0 || u.indexOf('/video/') >= 0) return true;
                  return [...document.querySelectorAll('video')].some((v) => {
                    const r = v.getBoundingClientRect();
                    return r.width > 400 && r.height > 300;
                  });
                }"""
            ))
        except Exception:
            return False

    async def _enter_video(self) -> bool:
        """从首页信息流点开一条视频（点缩略图，抖音会开 modal_id 弹窗）。"""
        page = self._pick_page()
        if page is None:
            return False
        if await self._playing():
            return True
        try:
            # 推荐流会自己播一条，比在精选网格里点缩略图稳得多
            await self._goto(FEED)
            await asyncio.sleep(self.cfg()["wait_video"])
            if await self._playing():
                return True
            for _ in range(3):
                hit = await page.evaluate(
                    """() => {
                      const imgs = [...document.querySelectorAll('img')]
                        .filter((el) => {
                          const r = el.getBoundingClientRect();
                          return r.width > 150 && r.height > 120 && r.y > 60;
                        });
                      if (!imgs.length) return null;
                      const r = imgs[0].getBoundingClientRect();
                      return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                    }"""
                )
                if hit is None:
                    await asyncio.sleep(1.5)
                    continue
                await page.mouse.click(hit["x"], hit["y"])
                await asyncio.sleep(self.cfg()["wait_video"])
                if await self._playing():
                    return True
            return False
        except Exception as exc:
            self._dbg(f"进视频失败：{exc}")
            return False

    async def _aweme_id(self) -> str:
        page = self._pick_page()
        if page is None:
            return ""
        try:
            url = page.url or ""
        except Exception:
            return ""
        for pat in ("modal_id=", "/video/"):
            if pat in url:
                tail = url.split(pat, 1)[1]
                return tail.split("&")[0].split("?")[0]
        return ""

    async def next_video(self) -> dict[str, Any]:
        async with self._lock:
            if self._ctx is None:
                return {"ok": False, "message": "浏览器未启动"}
            try:
                page = self._pick_page()
                if not await self._enter_video():
                    return {"ok": False, "message": "没能进入视频播放页"}
                before = await self._aweme_id()
                await page.keyboard.press("ArrowDown")
                await asyncio.sleep(self.cfg()["wait_video"])
                after = await self._aweme_id()
                info = await self._read_current()
                if after and before and after == before:
                    # 弹窗模式下方向键有时不生效，用滚动兜底一次
                    await page.mouse.wheel(0, 900)
                    await asyncio.sleep(self.cfg()["wait_video"])
                    after = await self._aweme_id()
                    info = await self._read_current()
                info["moved"] = bool(after and after != before)
                info["aweme_id"] = after
                return {"ok": True, **info}
            except Exception as exc:
                return {"ok": False, "message": f"切换失败：{exc}"}

    async def like(self) -> dict[str, Any]:
        async with self._lock:
            if self._ctx is None:
                return {"ok": False, "message": "浏览器未启动"}
            try:
                page = self._pick_page()
                if "/video/" not in (page.url or "") and not await self._enter_video():
                    return {"ok": False, "message": "没能进入视频播放页"}
                btn = await page.query_selector(
                    '[data-e2e="video-like"], [data-e2e="like-icon"], [aria-label*="赞"]'
                )
                if btn is not None:
                    box = await btn.bounding_box()
                    if box:
                        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    else:
                        await btn.click()
                else:
                    await page.keyboard.press("z")
                await asyncio.sleep(0.9)
                info = await self._read_current()
                info["liked"] = await self.liked_state()
                return {"ok": True, **info}
            except Exception as exc:
                return {"ok": False, "message": f"点赞失败：{exc}"}

    async def debug_rail(self) -> dict[str, Any]:
        """临时：鼠标移到画面中间，再列右侧竖排按钮。"""
        page = self._pick_page()
        if page is None:
            return {"ok": False, "error": "浏览器未启动"}
        try:
            if not await self._enter_video():
                return {"ok": False, "error": "没能进入视频播放页"}
            vp = page.viewport_size or {"width": 1280, "height": 720}
            await page.mouse.move(vp["width"] * 0.5, vp["height"] * 0.5)
            await asyncio.sleep(1.2)
            info = await page.evaluate(
                """() => {
                  const vw = window.innerWidth;
                  const out = [];
                  document.querySelectorAll('div,span,button').forEach((el) => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 20 || r.width > 120) return;
                    if (r.x < vw * 0.7) return;
                    const t = (el.innerText || '').trim();
                    out.push({t: t.slice(0, 10), x: Math.round(r.x), y: Math.round(r.y),
                              w: Math.round(r.width), h: Math.round(r.height),
                              cls: (el.className || '').toString().slice(0, 30)});
                  });
                  return {vw, out: out.slice(0, 30)};
                }"""
            )
            return {"ok": True, **info}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def like_probe(self) -> dict[str, Any]:
        """临时：点画面取焦点后按 z，看点赞状态与 class 是否变化。"""
        page = self._pick_page()
        if page is None:
            return {"ok": False, "error": "浏览器未启动"}
        try:
            if not await self._enter_video():
                return {"ok": False, "error": "没能进入视频播放页"}
            vp = page.viewport_size or {"width": 1280, "height": 720}
            snap = """() => {
              const els = [...document.querySelectorAll('[class*="like" i], [class*="digg" i]')];
              return els.map((el) => ({
                cls: (el.className || '').toString().slice(0, 60),
                t: (el.innerText || '').trim().slice(0, 12),
                r: (() => { const b = el.getBoundingClientRect();
                            return {x: Math.round(b.x), y: Math.round(b.y),
                                    w: Math.round(b.width), h: Math.round(b.height)}; })(),
              })).slice(0, 12);
            }"""
            before = await page.evaluate(snap)
            await page.mouse.click(vp["width"] * 0.5, vp["height"] * 0.5)
            await asyncio.sleep(0.6)
            await page.keyboard.press("z")
            await asyncio.sleep(1.5)
            after = await page.evaluate(snap)
            return {"ok": True, "url": page.url, "before": before, "after": after,
                    "changed": before != after}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def liked_state(self) -> bool:
        """当前视频是否已点赞（按按钮的 aria-pressed / 高亮类判断）。"""
        page = self._pick_page()
        if page is None:
            return False
        try:
            return bool(await page.evaluate(
                """() => {
                  const el = document.querySelector(
                    '[data-e2e="video-like"], [data-e2e="like-icon"], [aria-label*="赞"]');
                  if (!el) return false;
                  const cls = (el.className || '').toString();
                  const pressed = el.getAttribute('aria-pressed');
                  return pressed === 'true' || /active|checked|liked|selected/i.test(cls);
                }"""
            ))
        except Exception:
            return False

    async def probe_actions(self) -> dict[str, Any]:
        """只看不点：把视频页右侧操作栏的可点元素列出来。"""
        page = self._pick_page()
        if page is None:
            return {"ok": False, "error": "浏览器未启动"}
        try:
            if not await self._enter_video():
                return {"ok": False, "error": "没能进入视频播放页"}
            info = await page.evaluate(
                """() => {
                  const w = window.innerWidth;
                  const out = [];
                  document.querySelectorAll('button,[role="button"],div,span,a').forEach((el) => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 24 || r.height < 24 || r.width > 160) return;
                    if (r.x < w * 0.62) return;
                    const t = (el.innerText || '').trim();
                    const aria = el.getAttribute('aria-label') || '';
                    const title = el.getAttribute('title') || '';
                    const cls = (el.className || '').toString().slice(0, 60);
                    const e2e = el.getAttribute('data-e2e') || '';
                    if (!t && !aria && !title && !cls) return;
                    out.push({t: t.slice(0, 8), aria: aria.slice(0, 12), title: title.slice(0, 12),
                              e2e, cls});
                  });
                  return {w, items: out.slice(0, 25), url: location.href};
                }"""
            )
            info["liked"] = await self.liked_state()
            return {"ok": True, **info}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def comment(self, text: str) -> dict[str, Any]:
        async with self._lock:
            if self._ctx is None:
                return {"ok": False, "message": "浏览器未启动"}
            body = clean_text(text)
            if not body:
                return {"ok": False, "message": "评论内容是空的"}
            try:
                page = self._pick_page()
                if "/video/" not in (page.url or "") and not await self._enter_video():
                    return {"ok": False, "message": "没能进入视频播放页"}
                box = await page.query_selector(
                    '[data-e2e="comment-input"], div[contenteditable="true"], textarea'
                )
                if box is None:
                    return {"ok": False, "message": "没找到评论框，可能没登录或页面变了"}
                await box.click()
                await page.keyboard.type(body)
                await page.keyboard.press("Enter")
                await asyncio.sleep(1.0)
                info = await self._read_current()
                return {"ok": True, "text": body, **info}
            except Exception as exc:
                return {"ok": False, "message": f"评论失败：{exc}"}

    # ------------------------------------------------------------ 刷视频 / 互动
    async def _read_current(self) -> dict[str, Any]:
        page = self._pick_page()
        title = ""
        desc = ""
        url = page.url if page else ""
        try:
            node = await page.query_selector(
                '[data-e2e="video-desc"], [class*="video-info"] h1, [class*="desc"]'
            )
            if node:
                title = clean_text(await node.inner_text() or "")
            meta = await page.query_selector('meta[property="og:title"]')
            if not title and meta:
                title = clean_text(await meta.get_attribute("content") or "")
            desc_node = await page.query_selector('[class*="video-desc"], [class*="caption"]')
            if desc_node:
                desc = clean_text(await desc_node.inner_text() or "")
        except Exception:
            pass
        self._current = {"title": title, "desc": desc, "url": url, "key": video_key(url, title)}
        return self._current

    async def current(self) -> dict[str, Any]:
        async with self._lock:
            if self._ctx is None:
                return {}
            return await self._read_current()

    async def selfcheck(self) -> list[dict[str, Any]]:
        """只读检查：依赖、浏览器、登录、页面元素。"""
        out: list[dict[str, Any]] = []
        try:
            import playwright  # noqa: F401

            out.append({"name": "Playwright", "state": "正常", "detail": "已安装"})
        except Exception:
            out.append({"name": "Playwright", "state": "异常", "detail": "未安装 playwright"})
        status = await self.status()
        out.append(
            {
                "name": "浏览器",
                "state": "正常" if status.get("running") else "警告",
                "detail": status.get("url") or "未启动",
            }
        )
        out.append(
            {
                "name": "登录状态",
                "state": "正常" if status.get("logged_in") else "警告",
                "detail": "已登录" if status.get("logged_in") else "未登录，需要扫码",
            }
        )
        return out