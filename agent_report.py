# -*- coding: utf-8 -*-
"""agent_report.py —— 外部 agent（DeepSeek Harness）推理报送。

DSH 那边一轮结束时，桥脚本把这一轮的
  用时 / 模型耗时 / 输入输出 token / 缓存命中 / 花费 / 回答开头
POST 到 /page/agent-report，这里再转成一条通知发到指定会话（默认主人私聊）。

设计原则：只陈述事实，不替 agent 表态、不装人设。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

AGENT_REPORT_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "umo": "napcat:FriendMessage:1703346440",   # 默认发给主人私聊
    "title": "🧠 DSH 这轮完事",
    "show_tokens": True,      # 输入/输出/缓存
    "show_cost": True,        # 花费
    "show_summary": True,     # 回答开头
    "summary_chars": 60,      # 回答摘取长度
    "max_log": 200,
    "min_seconds": 0,         # 小于这个秒数就不发（0=都发）
}


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return "—"


def _human_elapsed(value: Any) -> str:
    try:
        sec = int(round(float(value)))
    except (TypeError, ValueError):
        return ""
    if sec < 1:
        return "不到 1 秒"
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒" if sec % 60 else f"{sec // 60} 分"
    return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"


def _fmt_cost(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{amount:.2f} 块" if amount >= 0.005 else "不到 1 分钱"


def _shorten(text: Any, limit: int) -> str:
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    body = re.sub(r"^(回答|答)[:：]\s*", "", body)
    if not body:
        return ""
    return body if len(body) <= limit else body[:limit].rstrip() + "…"


class AgentReportService:
    """DSH → AstrBot → QQ 的报送通道；由主插件实例化，内部无全局状态。"""

    def __init__(
        self,
        context: Any,
        settings_getter: Callable[[], dict[str, Any]],
        debug_logger: Callable[[str], None],
        data_dir: Path,
    ) -> None:
        self.context = context
        self._settings = settings_getter
        self._dbg = debug_logger
        self.data_dir = Path(data_dir)
        self.log_path = self.data_dir / "agent_report_log.json"

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def cfg(self) -> dict[str, Any]:
        cfg = dict(AGENT_REPORT_DEFAULTS)
        try:
            raw = self._settings().get("agent_report") or {}
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in AGENT_REPORT_DEFAULTS})
        except Exception as exc:  # noqa: BLE001
            self._dbg(f"读取外部报送配置失败: {exc}")
        return cfg

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        cfg = self.cfg()
        for key, value in (patch or {}).items():
            if key not in AGENT_REPORT_DEFAULTS:
                continue
            if key in ("enabled", "show_tokens", "show_cost", "show_summary"):
                cfg[key] = bool(value)
            elif key in ("max_log", "min_seconds", "summary_chars"):
                try:
                    cfg[key] = max(0, min(1000, int(value)))
                except (TypeError, ValueError):
                    pass
            else:
                text = str(value or "").strip()
                if text:
                    cfg[key] = text[:120]
        return cfg

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def render(self, payload: dict[str, Any]) -> str:
        cfg = self.cfg()
        lines: list[str] = [str(cfg.get("title") or AGENT_REPORT_DEFAULTS["title"])]

        elapsed = _human_elapsed(payload.get("elapsed"))
        llm = _human_elapsed(payload.get("llm_elapsed"))
        if elapsed and llm and llm != elapsed:
            lines.append(f"⏱ 用时 {elapsed}（模型 {llm}）")
        elif elapsed:
            lines.append(f"⏱ 用时 {elapsed}")
        elif llm:
            lines.append(f"⏱ 模型 {llm}")

        if cfg.get("show_tokens", True):
            tin = _fmt_int(payload.get("tokens_in"))
            tout = _fmt_int(payload.get("tokens_out"))
            cache = _fmt_int(payload.get("cache_read"))
            bits = []
            if tin != "—":
                bits.append(f"输入 {tin}")
            if tout != "—":
                bits.append(f"输出 {tout}")
            if cache not in ("—", "0"):
                bits.append(f"缓存读 {cache}")
            if bits:
                lines.append("🔢 " + " · ".join(bits))

        if cfg.get("show_cost", True):
            cost = _fmt_cost(payload.get("cost"))
            if cost:
                lines.append(f"💸 {cost}")

        if payload.get("reason"):
            lines.append(f"⚠️ 这轮没跑通：{payload['reason']}")

        if cfg.get("show_summary", True):
            try:
                limit = int(cfg.get("summary_chars") or 60)
            except (TypeError, ValueError):
                limit = 60
            summary = _shorten(payload.get("summary") or payload.get("response"), limit)
            if summary:
                lines.append(f"💬 {summary}")

        return "\n".join(lines).strip()

    # ------------------------------------------------------------------ #
    # 投递
    # ------------------------------------------------------------------ #
    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self.cfg()
        if not cfg.get("enabled", True):
            return {"sent": False, "reason": "disabled"}
        try:
            if payload.get("elapsed") is not None and float(payload["elapsed"]) < float(cfg.get("min_seconds") or 0):
                return {"sent": False, "reason": "too_short"}
        except (TypeError, ValueError):
            pass
        umo = str(payload.get("umo") or cfg.get("umo") or "").strip()
        if not umo:
            return {"sent": False, "reason": "no_umo"}
        text = self.render(payload)
        if not text:
            return {"sent": False, "reason": "empty"}
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[token_panel] 外部报送发送失败: {exc}")
            return {"sent": False, "reason": f"send_failed: {exc}"}
        self._log(umo, text, payload)
        return {"sent": True, "text": text, "umo": umo}

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def _log(self, umo: str, text: str, payload: dict[str, Any]) -> None:
        try:
            rows = self.log_rows(10**6)
            rows.insert(0, {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "umo": umo,
                "text": text,
                "turn": payload.get("turn"),
                "tokens_in": payload.get("tokens_in"),
                "tokens_out": payload.get("tokens_out"),
                "cost": payload.get("cost"),
                "elapsed": payload.get("elapsed"),
            })
            keep = int(self.cfg().get("max_log") or 200)
            self.log_path.write_text(
                json.dumps(rows[:keep], ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self._dbg(f"写入外部报送日志失败: {exc}")

    def log_rows(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            if not self.log_path.exists():
                return []
            rows = json.loads(self.log_path.read_text(encoding="utf-8"))
            return rows if isinstance(rows, list) else []
        except Exception:  # noqa: BLE001
            return []

    def clear_log(self) -> None:
        try:
            self.log_path.write_text("[]", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self._dbg(f"清空外部报送日志失败: {exc}")