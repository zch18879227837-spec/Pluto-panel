"""抖音网页版平台适配器：把 Playwright 私信接进 AstrBot 事件流。

要点：
- 平台 id 固定 `douyin_web`，会话 id = 抖音 openid（找不到 openid 时用昵称/哈希兜底）。
- 事件走 AstrBot 常规管线，因此人设、指令、权限、TTS、记忆、日记、图片处理全部与 QQ 一致；
  回复由本事件的 `send()` 直接发回抖音私信，不会串到 QQ。
- 管理员身份按插件里单独配置的抖音 openid 白名单判定（与 QQ 管理员互不影响）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.core.platform import AstrBotMessage, MessageMember, MessageType, Platform, PlatformMetadata
from astrbot.core.platform.message_session import MessageSession

from .douyin_web import clean_text

PLATFORM_NAME = "douyin_web"
PLATFORM_ID = "douyin_web"


class DouyinMessageEvent(AstrMessageEvent):
    def __init__(self, message_str, message_obj, platform_meta, session_id, web) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._web = web

    async def send(self, message: MessageChain) -> None:
        text_parts: list[str] = []
        for comp in message.chain:
            if isinstance(comp, Plain):
                text_parts.append(comp.text)
            elif isinstance(comp, Image):
                text_parts.append("[图片]")
        text = clean_text("\n".join(p for p in text_parts if p))
        if not text:
            return
        logger.info(f"[douyin] 事件回包 -> {self.get_session_id()} text={text[:40]!r}")
        res = await self._web.send_dm(self.get_session_id(), text)
        logger.info(f"[douyin] 发送结果 {res}")
        self._has_send_oper = True

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        buf: list[str] = []
        async for chain in generator:
            for comp in chain.chain:
                if isinstance(comp, Plain):
                    buf.append(comp.text)
                elif isinstance(comp, Image):
                    buf.append("[图片]")
        text = clean_text("".join(buf))
        if text:
            await self._web.send_dm(self.get_session_id(), text)
        self._has_send_oper = True


class DouyinPlatform(Platform):
    """轻量平台实例：只负责 run 循环与 send_by_session 回包。"""

    def __init__(self, config: dict, event_queue, web, *, admins_get: Callable[[], list[str]], log=None) -> None:
        super().__init__(config, event_queue)
        self._web = web
        self._admins = admins_get
        self._log = log or (lambda *a, **k: None)
        self._task: asyncio.Task | None = None
        self._meta = PlatformMetadata(
            name=PLATFORM_NAME,
            description="抖音网页版（Playwright）：私信、刷视频、点赞、评论、分享",
            id=PLATFORM_ID,
            adapter_display_name="抖音网页版",
            support_streaming_message=False,
        )
        self._seen: set[str] = set()

    def meta(self) -> PlatformMetadata:
        return self._meta

    def run(self):
        return self._loop()

    async def _loop(self) -> None:
        while True:
            try:
                if not self._web.cfg().get("enabled"):
                    await asyncio.sleep(3)
                    continue
                # 插件重载后浏览器是停的：先把它拉起来，否则 status 永远说没登录
                try:
                    await self._web.ensure()
                except Exception as exc:
                    logger.warning(f"[douyin] 启动浏览器失败: {exc}")
                    await asyncio.sleep(10)
                    continue
                interval = self._web_poll_interval()
                st = await self._web.status()
                if not st.get("logged_in"):
                    # 没登录 / 正在扫码时不要翻页，否则会把登录页拽走
                    await asyncio.sleep(max(3.0, interval))
                    continue
                if st.get("gate") == "captcha":
                    # 卡在滑块验证：把窗口亮出来让人工过，过完再继续
                    try:
                        await self._web.make_headful()
                    except Exception as exc:
                        logger.warning(f"[douyin] 拉起验证窗口失败: {exc}")
                    await asyncio.sleep(max(5.0, interval))
                    continue
                msgs = await self._web.poll_messages()
                for item in msgs:
                    await self._emit(item)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[douyin] 轮询异常: {exc}")
                await asyncio.sleep(5)

    def _web_poll_interval(self) -> float:
        try:
            from .risk_core import MIN_POLL_SECONDS

            raw = (self.config or {}).get("poll_seconds")
            return max(MIN_POLL_SECONDS, float(raw or 8.0))
        except Exception:
            return 8.0

    async def _emit(self, item: dict[str, Any]) -> None:
        mid = str(item.get("mid") or "")
        if mid and mid in self._seen:
            return
        if mid:
            self._seen.add(mid)
            if len(self._seen) > 500:
                self._seen.clear()

        text = clean_text(item.get("text") or "")
        if not text:
            return
        openid = str(item.get("openid") or item.get("nickname") or "unknown")
        nickname = str(item.get("nickname") or "抖音用户")

        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = PLATFORM_ID
        message.session_id = openid
        message.message_id = mid or f"douyin-{int(asyncio.get_event_loop().time())}"
        message.sender = MessageMember(user_id=openid, nickname=nickname)
        message.message = [Plain(text)]
        message.message_str = text
        message.raw_message = item

        event = DouyinMessageEvent(text, message, self._meta, openid, self._web)
        if openid in (self._admins() or []):
            event.role = "admin"
        logger.info(f"[douyin] 入队事件 openid={openid} text={text[:40]!r}")
        await self._event_queue.put(event)
        self._log("私信", f"收到抖音私信：{text[:40]}")

    async def send_by_session(self, session, message_chain: MessageChain) -> None:
        parts: list[str] = []
        for comp in message_chain.chain:
            if isinstance(comp, Plain):
                parts.append(comp.text)
            elif isinstance(comp, Image):
                parts.append("[图片]")
        text = clean_text("\n".join(p for p in parts if p))
        logger.info(f"[douyin] send_by_session -> {session.session_id} text={text[:50]!r}")
        if text:
            res = await self._web.send_dm(session.session_id, text)
            logger.info(f"[douyin] send_by_session 结果 {res}")
            self._log("私信", f"回复抖音私信：{text[:40]}")

    async def debug_emit(self, text: str, openid: str = "debug-user") -> dict:
        """调试：直接往事件队列塞一条假私信，用来验证管线与回包。"""
        await self._emit({"mid": f"debug-{text[:8]}-{asyncio.get_event_loop().time():.0f}",
                          "openid": openid, "nickname": openid, "text": text})
        return {"ok": True, "openid": openid, "text": text}

    async def start_polling(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop_polling(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None