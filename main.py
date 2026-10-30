"""Token 面板 —— AstrBot 会话上下文用量可视化插件。

设计要点（对应需求）：
1. Token 数字全部来自 AstrBot 官方数据：会话表里的 ``token_usage``（AstrBot 保存的
   最近一次 LLM 请求总 token 数）与 Provider 的 ``max_context_tokens``（官方按
   models.dev 元数据解析）。本插件不做任何 token 估算。
2. 所有读取都包在 try/except 里，失败时只返回友好提示，不抛异常、不阻塞消息主线程。
3. 自动播报只在"跨过阈值"或"超过阈值且距上次播报超过冷却时间"时发一次，避免刷屏。
4. QQ 指令统一使用 token_ 前缀，绝不占用 /ctx 等原生指令。
5. 面板设置持久化到 data/plugin_data/astrbot_plugin_token_panel/settings.json。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlmodel import col

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.db.po import ProviderStat
from astrbot.core.message.message_event_result import MessageChain

from .proactive import PROACTIVE_DEFAULTS, ProactiveEngine, start_loops, stop_all_loops
from .notify import NOTIFY_DEFAULTS, NotifyService
from .risk_tuning import RiskTuner
from .tts_module import (
    TTS_DEFAULTS,
    TtsService,
    clean_for_tts,
    has_trigger,
    strip_trigger,
)

PLUGIN_NAME = "astrbot_plugin_token_panel"
PLUGIN_VERSION = "1.7.2"
PAGE_PREFIX = f"/{PLUGIN_NAME}/page"

# 高危线固定 95%（需求写死），预警线可在面板/指令里调整
HIGH_PERCENT = 95
# 同一会话自动播报的最小间隔（秒），避免连续刷屏
NOTICE_COOLDOWN = 1800
# 清空上下文二次确认有效期（秒）
CLEAR_CONFIRM_TTL = 60
# 网页聊天里给当前会话设备注的前缀
WEB_NOTE_PREFIX = "#标题"
# 生成 AI 摘要的超时时间（秒）与整体最长等待
SUMMARY_TIMEOUT = 12.0
SUMMARY_WAIT = 15.0
# 摘要失败后的冷却时间（秒），冷却期内不再调用模型，避免反复失败拖慢面板
SUMMARY_FAIL_COOLDOWN = 600
# 闲置网页会话判定（天）
IDLE_DAYS = 7

# --------------------------------------------------------------------------- #
# 上下文压缩 / 承接（集成自 astrbot_plugin_ctx_carry，逻辑、摘要模板、话术均原样保留）
# --------------------------------------------------------------------------- #
CARRY_DEFAULTS: dict[str, Any] = {
    "token_limit": 128000,       # 估算基准窗口（仅用于估算与触发线）
    "auto_enabled": False,       # 请求内自动压缩开关
    "auto_threshold": 0.8,       # 请求内自动压缩触发线
    "keep_rounds": 8,            # /压缩 默认保留轮数
    "summary_max_chars": 500,    # 摘要字数上限
    "enhanced_summary": False,   # 增强摘要模式（默认关闭，关闭时与原版逐字一致）
}
# 压缩日志最多保留条数（只记录，不参与逻辑）
CARRY_LOG_LIMIT = 200
# 单会话历史备份最多保留份数（只读常量，面板展示用）
CARRY_BACKUP_LIMIT = 20
# 参数合法区间（面板前端实时校验，后端仍夹取一次兜底）
CARRY_LIMITS: dict[str, list[int]] = {
    "token_limit": [1000, 10000000],
    "auto_threshold": [30, 95],       # 百分比
    "summary_max_chars": [100, 2000],
    "keep_rounds": [1, 50],
}

SUMMARY_PROMPT = (
    "请把下面这段较早的聊天记录压缩成一段不超过 {max_chars} 字的中文摘要，供你自己之后回忆。\n"
    "要求：\n"
    "1. 保留：约定与承诺、称呼与关系、主人的偏好与作息、正在做的事及进度、明显的情绪事件、反复出现的梗；\n"
    "2. 丢掉：寒暄、重复表达、与事实无关的语气词；\n"
    "3. 用要点式短句，不要编造没出现过的内容，不要输出开场白或客套话。"
)

# 增强摘要模式：只在原模板之后追加要求，原模板本身一字不改。
ENHANCED_SUMMARY_EXTRA = (
    "\n4. 另外单独提取对话里出现的关键数据（数字、版本号、价格、日期）、配置项（参数名与取值）、"
    "代码要点（函数名、路径、命令）——只摘录原文出现过的事实，不确定就写「未提及」，严禁编造；\n"
    "5. 仍用要点式短句，总字数不超过 {max_chars} 字，不要输出表格或代码块。"
)

# --------------------------------------------------------------------------- #
# 会话日记本（v1.6.0）
# 与压缩 / 承接完全解耦的独立记忆层：自己存盘、自己检索、自己的开关。
# 全程不经过任何摘要与承接函数，也不参与上下文合并。
# --------------------------------------------------------------------------- #
DIARY_DEFAULTS: dict[str, Any] = {
    "enable": False,              # 总开关（关闭时读写与检索注入全部休眠）
    "max_per_day": 3,             # 单会话单日最多几篇（模型按权重自己决定 1~3 篇）
    "max_single_length": 260,     # 单篇正文长度上限
    "inject_relevant_count": 3,   # 每次请求最多注入几条相关日记
}
# 参数合法区间（面板前端实时校验，后端仍夹取一次兜底）
DIARY_LIMITS: dict[str, list[int]] = {
    "max_per_day": [1, 3],
    "max_single_length": [60, 1000],
    "inject_relevant_count": [1, 10],
}
# 相似度低于这个值就不注入，宁可少给也不灌无关内容
DIARY_RELEVANCE_FLOOR = 0.10
# 日记权重：high > medium > low（与摘要模板保留项一一对应）
DIARY_WEIGHTS = ("high", "medium", "low")
DIARY_WEIGHT_RANK = {"high": 3, "medium": 2, "low": 1}
DIARY_WEIGHT_CN = {"high": "高", "medium": "中", "low": "低"}
# 日记标签：<diary weight="high">正文</diary>
DIARY_TAG_RE = re.compile(r"<diary\b([^>]*)>(.*?)</diary\s*>", re.S | re.I)
# 流式输出时漏写的未闭合残留，发送前一并清掉
DIARY_TAIL_RE = re.compile(r"<diary\b[^>]*>.*$", re.S | re.I)
DIARY_WEIGHT_ATTR_RE = re.compile(r"weight\s*=\s*[\"']?([A-Za-z]+)", re.I)

# 注入给模型的写日记规则（只在开关打开时出现在系统提示里）
DIARY_WRITE_RULE = (
    "【日记本】值得长期记住的事，在回复末尾附：<diary weight=\"high\">一句话</diary>，weight 取 high / medium / low。\n"
    "高=长期偏好与习惯、重要约定与承诺、关系与关键事实；中=阶段性进展与结论、明确的计划；低=只对当下有用的小事；"
    "寒暄、重复、无信息量的闲聊不写。一天最多 {max_per_day} 篇，没有就不写；"
    "第三人称、一句话讲清「发生了什么、意味着什么」，不超过 {max_len} 字，不重复原话，不要 Markdown 与编号；"
    "标签原样输出，不解释日记本，也不在标签之外复述正文。"
)

REMINDER_RE = re.compile(r"<system_remind[^>]*>.*?</system_remind[^>]*>", re.S)
REMINDER_TAIL_RE = re.compile(r"<system_remind.*$", re.S)


def _strip_reminders(text: str) -> str:
    text = REMINDER_RE.sub("", text)
    text = REMINDER_TAIL_RE.sub("", text)
    return text.strip()


def msg_text(message: dict) -> str:
    """从一条 OpenAI 格式消息里取出纯文本。"""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                parts.append(str(part.get("text") or ""))
            elif ptype == "image_url":
                parts.append("[图片]")
            elif ptype == "audio_url":
                parts.append("[语音]")
        raw = "\n".join(parts)
    else:
        raw = ""
    return _strip_reminders(raw)


def est_tokens(text: str) -> int:
    """与 AstrBot 自带估算器同口径：中文*0.6 + 其他*0.3。"""
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - chinese
    return int(chinese * 0.6 + other * 0.3)


def est_history_tokens(history: list[dict]) -> int:
    return sum(est_tokens(msg_text(m)) + 4 for m in history)


def split_history(history: list[dict], keep_rounds: int) -> tuple[list[dict], list[dict]]:
    """按「轮」切开：一轮从 user 消息开始。返回 (旧, 保留)。"""
    if keep_rounds <= 0:
        return list(history), []
    user_positions = [i for i, m in enumerate(history) if m.get("role") == "user"]
    if len(user_positions) <= keep_rounds:
        return [], list(history)
    cut = user_positions[-keep_rounds]
    return history[:cut], history[cut:]


def build_transcript(history: list[dict], per_msg_limit: int = 400, total_limit: int = 24000) -> str:
    lines: list[str] = []
    total = 0
    for m in history:
        role = "主人" if m.get("role") == "user" else ("肥鱼" if m.get("role") == "assistant" else str(m.get("role")))
        text = msg_text(m)
        if not text:
            continue
        line = f"{role}：{text[:per_msg_limit]}"
        total += len(line)
        if total > total_limit:
            lines.append("……（更早的内容省略）")
            break
        lines.append(line)
    return "\n".join(lines)


def rounds_of(history: list[dict]) -> int:
    return sum(1 for m in history if isinstance(m, dict) and m.get("role") == "user")


# --------------------------------------------------------------------------- #
# 上下文统计（集成自 ctx_stats，解析顺序与输出格式原样保留）
# --------------------------------------------------------------------------- #
def _ctx_tip(pct: float) -> str:
    """按占用率给一句人话提示，沿用原有分档。"""
    if pct < 50:
        return "空间充足，放心聊"
    if pct < 75:
        return "中等，继续对话会慢慢上涨"
    if pct < 90:
        return "偏高，有丢失早期记忆风险"
    return "危险，建议清空会话或者压缩上下文"


def _provider_model_name(provider) -> str | None:
    """兼容不同版本：优先 get_model()，退回 provider.model / provider_config。"""
    try:
        model = provider.get_model()
        if model:
            return str(model)
    except Exception:
        pass
    try:
        model = getattr(provider, "model", None)
        if model:
            return str(model)
    except Exception:
        pass
    try:
        cfg = getattr(provider, "provider_config", None) or {}
        model = cfg.get("model")
        if model:
            return str(model)
    except Exception:
        pass
    return None


DEFAULT_SETTINGS: dict[str, Any] = {
    "style": "claude",       # claude=Claude Code 风格  deepseek=DeepSeek 蓝调
    "theme": "ivory",        # ivory=象牙白  ink=纯黑
    "zoom": 100,             # 页面缩放 80~140
    "tooltip": True,         # 悬浮提示
    "warn_percent": 80,      # 预警阈值 1~99
    "auto_broadcast": True,  # 自动进度播报
    "debug_log": False,      # 调试日志
    "carry": dict(CARRY_DEFAULTS),  # 压缩承接设置（迁移自 ctx_carry 配置）
    "proactive": dict(PROACTIVE_DEFAULTS),  # 精细化主动消息（v1.4.1 内置）
    "diary": dict(DIARY_DEFAULTS),  # 会话日记本（v1.6.0，独立于压缩/承接）
    "notify": dict(NOTIFY_DEFAULTS),  # 详细推理完成通知（v1.7.0，独立模块）
    "tts": dict(TTS_DEFAULTS),  # 语音合成（v1.7.1，独立模块）
    "net_page_size": 50,     # 搜索监控日志展示条数
    "net_highlight": True,   # 搜索监控关键词高亮
    "limit_override": 0,     # 上下文上限手动兜底：0=自动读取（v1.4.1 修复）
    "risk_strictness": 1.0,  # 抖音风控严格系数 0.1~2.0（v1.5.0）
    "net_log_db": "",        # 搜索日志库路径，空=自动找（v1.6.3）
    "playwright_browsers_path": "",  # Playwright 浏览器目录，空=用环境变量（v1.6.3）
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _fmt_int(value: Any) -> str:
    """1234567 -> 1,234,567；异常值返回 '—'。"""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _render_bar(percent: float, width: int = 12) -> str:
    """纯文本进度条，用于 QQ 内的 /token_ctx 卡片。"""
    try:
        ratio = max(0.0, min(1.0, float(percent) / 100.0))
    except (TypeError, ValueError):
        ratio = 0.0
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def _status_of(percent: float | None, warn_percent: int) -> tuple[str, str]:
    """返回 (状态文案, 状态级别 normal|warn|high)。"""
    if percent is None:
        return "未知", "unknown"
    if percent >= HIGH_PERCENT:
        return "高危", "high"
    if percent >= warn_percent:
        return "预警", "warn"
    return "正常", "normal"


def _split_umo(umo: str) -> tuple[str, str, str]:
    """platform:message_type:session_id -> (platform, message_type, session_id)。

    WebChat 等平台的 umo 可能没有冒号（如 webchat!<user>!<uuid>），此时整串当作
    session_id，platform 依据前缀判断，保证面板里不会出现空白会话名。
    """
    text = str(umo or "").strip()
    parts = text.split(":", 2)
    while len(parts) < 3:
        parts.append("")
    platform, message_type, session_id = parts[0], parts[1], parts[2]
    if not session_id and not message_type:
        # 无冒号形式：整串是会话标识
        platform = platform.split("!", 1)[0] if platform else ""
        session_id = text
        message_type = ""
    return platform, message_type, session_id


def _kind_of(message_type: str, platform: str = "") -> str:
    """把 AstrBot 的 message_type 归一成 friend / group。"""
    mt = (message_type or "").lower()
    if "group" in mt:
        return "group"
    return "friend"


def _label_of(kind: str, platform: str, session: str, title: str) -> str:
    """给面板生成一个人类可读的会话名。"""
    title = (title or "").strip()
    if kind == "group":
        return f"群 {session}" + (f" · {title}" if title else "")
    if (platform or "").lower().startswith("webchat"):
        short = session.split("!")[-1][:8] if session else ""
        return f"网页面板 {short}".strip()
    return f"私聊 {session}" if session else "未知会话"


def _is_web(platform: str) -> bool:
    """是否网页聊天平台（webchat，兼容带后缀的适配器名）。"""
    return (platform or "").lower().startswith("webchat")


def _web_short(session: str) -> str:
    """网页会话随机 ID 的短码（uuid 前 8 位）。"""
    return (session or "").split("!")[-1][:8]


def _history_size(history_json: Any) -> int:
    """history 里的消息条数（无法解析时按 0 处理）。"""
    try:
        messages = json.loads(history_json) if isinstance(history_json, str) else history_json
    except Exception:
        return 0
    return len(messages) if isinstance(messages, list) else 0


def _history_snippet(history_json: Any, limit: int = 14) -> str:
    """把 history 尾部若干条消息压成纯文本，供 AI 摘要使用（失败返回空串）。"""
    try:
        messages = json.loads(history_json) if isinstance(history_json, str) else history_json
    except Exception:
        return ""
    if not isinstance(messages, list):
        return ""
    lines: list[str] = []
    for item in messages[-limit:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = item.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            text = " ".join(chunks)
        text = " ".join(str(text).split())
        if not text:
            continue
        text = text[:160]
        if role == "user":
            lines.append(f"用户：{text}")
        elif role == "assistant":
            lines.append(f"助手：{text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def _idle_days(raw: Any) -> int:
    """距今天数（数据库时间是 UTC 朴素时间）。"""
    if not raw:
        return 0
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip().replace("T", " ")
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 86400))
    except Exception:
        return 0


def _load_local_time(raw: Any) -> str:
    """把数据库里的 UTC 时间戳转成本地时区的简短展示。"""
    if not raw:
        return ""
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip().replace("T", " ")
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, num))


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class TokenPanel(Star):
    """Token 面板插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg = config

        # 数据目录与设置文件
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.data_dir / "backups"
        self.backup_dir.mkdir(exist_ok=True)
        self.settings_path = self.data_dir / "settings.json"
        self.meta_path = self.data_dir / "session_meta.json"

        # 运行期状态（不落盘）
        self._pending_clear: dict[str, float] = {}
        self._pending_diary_clear: dict[str, float] = {}
        self._last_notice: dict[str, float] = {}
        self._last_percent: dict[str, float] = {}
        # provider 窗口上限缓存：umo -> (model, limit, source, expire_ts)
        self._limit_cache: dict[str, tuple[str | None, int | None, str, float]] = {}

        self._settings = self._load_settings()
        # 会话备注/摘要缓存：cid -> {title, title_at, summary, msg_count, summary_at, failed_at}
        self._meta: dict[str, Any] | None = None

        # ---- 压缩承接（集成模块）运行期状态 ----
        self.carry_backup_dir = self.data_dir / "carry_backups"
        self.carry_backup_dir.mkdir(exist_ok=True)
        self.carry_log_path = self.data_dir / "compress_log.json"
        self._auto_cache: dict[str, tuple[int, str]] = {}

        # ---- 精细化主动消息引擎（v1.4.1） ----
        self.proactive = ProactiveEngine(self)

        # ---- 详细推理完成通知（v1.7.0）：独立模块，自己管自己的状态与日志 ----
        self.notify = NotifyService(
            self.context,
            settings_getter=lambda: self._settings,
            debug_logger=self._dbg,
            data_dir=self.data_dir,
        )

        # ---- 语音合成（v1.7.1）：只调用 AstrBot 原生 TTSProvider，配置在 settings.json 的 tts 段 ----
        self.tts = TtsService(
            self.context,
            settings_getter=lambda: self._settings,
            settings_saver=self._save_settings,
            debug_logger=self._dbg,
            data_dir=self.data_dir,
        )

        # ---- 抖音风控调速（v1.5.0）：分数/计时/回升进度只在内存里，重启即清空 ----
        self.risk = RiskTuner()
        self._dy_manual_at = 0.0

        # ---- 抖音"最近浏览"内存缓冲（v1.5.1）：只给模型拼上下文用，不落盘，重启即清空 ----
        self._dy_recent: list[dict[str, Any]] = []

        logger.info(f"[token_panel] {PLUGIN_VERSION} 初始化完成，数据目录 {self.data_dir}")

    # ------------------------------------------------------------------ #
    # 设置读写
    # ------------------------------------------------------------------ #
    def _cfg_default(self, key: str, fallback: Any) -> Any:
        """优先读 _conf_schema 注入的上级配置，缺失时用 fallback。"""
        try:
            if self.cfg is not None and key in self.cfg:
                return self.cfg[key]
        except Exception:
            pass
        return fallback

    def _load_settings(self) -> dict[str, Any]:
        """合并 默认值 → 插件配置 → 本地 settings.json。"""
        merged = dict(DEFAULT_SETTINGS)
        for key in DEFAULT_SETTINGS:
            merged[key] = self._cfg_default(key, DEFAULT_SETTINGS[key])

        try:
            if self.settings_path.exists():
                raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key in DEFAULT_SETTINGS:
                        if key in raw:
                            merged[key] = raw[key]
        except Exception as exc:
            logger.warning(f"[token_panel] 读取本地设置失败，使用默认值: {exc}")

        # 兜底校验，防止手改 json 写坏
        merged["style"] = "deepseek" if str(merged.get("style")) == "deepseek" else "claude"
        merged["theme"] = "ink" if str(merged.get("theme")) == "ink" else "ivory"
        merged["zoom"] = _clamp_int(merged.get("zoom"), 80, 140, 100)
        merged["warn_percent"] = _clamp_int(merged.get("warn_percent"), 1, 99, 80)
        merged["tooltip"] = bool(merged.get("tooltip", True))
        merged["auto_broadcast"] = bool(merged.get("auto_broadcast", True))
        merged["debug_log"] = bool(merged.get("debug_log", False))

        # 压缩承接设置：逐项兜底，手改坏了也不会炸
        carry = merged.get("carry")
        carry = dict(carry) if isinstance(carry, dict) else {}
        base = dict(CARRY_DEFAULTS)
        base.update(carry)
        try:
            base["token_limit"] = max(1000, int(base.get("token_limit") or 128000))
        except (TypeError, ValueError):
            base["token_limit"] = 128000
        try:
            base["keep_rounds"] = max(1, min(50, int(base.get("keep_rounds") or 8)))
        except (TypeError, ValueError):
            base["keep_rounds"] = 8
        try:
            base["auto_threshold"] = min(0.95, max(0.3, float(base.get("auto_threshold") or 0.8)))
        except (TypeError, ValueError):
            base["auto_threshold"] = 0.8
        try:
            base["summary_max_chars"] = max(100, min(2000, int(base.get("summary_max_chars") or 500)))
        except (TypeError, ValueError):
            base["summary_max_chars"] = 500
        base["auto_enabled"] = bool(base.get("auto_enabled", False))
        base["enhanced_summary"] = bool(base.get("enhanced_summary", False))
        merged["carry"] = base

        # 会话日记本：逐项兜底（v1.6.0）
        diary = merged.get("diary")
        diary = dict(diary) if isinstance(diary, dict) else {}
        dbase = dict(DIARY_DEFAULTS)
        dbase.update(diary)
        dbase["enable"] = bool(dbase.get("enable", False))
        dbase["max_per_day"] = _clamp_int(dbase.get("max_per_day"), 1, 3, 3)
        dbase["max_single_length"] = _clamp_int(dbase.get("max_single_length"), 60, 1000, 260)
        dbase["inject_relevant_count"] = _clamp_int(dbase.get("inject_relevant_count"), 1, 10, 3)
        merged["diary"] = dbase
        return merged

    def _save_settings(self) -> bool:
        """原子写入 settings.json。"""
        try:
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.settings_path)
            return True
        except Exception as exc:
            logger.warning(f"[token_panel] 保存设置失败: {exc}")
            return False

    def _dbg(self, message: str) -> None:
        """调试日志开关打开时才输出。"""
        if self._settings.get("debug_log"):
            logger.info(f"[token_panel][debug] {message}")

    def _admins(self) -> set[str]:
        """AstrBot 管理员 QQ 集合（用于会话可见性过滤）。"""
        try:
            raw = self.context.get_config().get("admins_id", []) or []
            return {str(item).strip() for item in raw if str(item).strip()}
        except Exception as exc:
            self._dbg(f"读取管理员列表失败: {exc}")
            return set()

    # ------------------------------------------------------------------ #
    # 官方数据读取
    # ------------------------------------------------------------------ #
    def _db(self):
        """拿到 AstrBot 的会话数据库句柄（官方 ORM）。"""
        db = getattr(self.context, "_db", None)
        if db is None:
            cm = getattr(self.context, "conversation_manager", None)
            db = getattr(cm, "db", None)
        if db is None:
            raise RuntimeError("无法访问会话数据库")
        return db

    async def _fetch_rows(self, limit: int = 200) -> list[Any]:
        """读取最近的会话行（不带 history，避免把几 MB 对话读进内存）。"""
        db = self._db()
        rows: list[Any] = []
        try:
            # 官方实现返回 (rows, total)，这里做一次兼容解包
            res = await db.get_filtered_conversations(page=1, page_size=limit, include_history=False)
            rows = res[0] if isinstance(res, tuple) else res
        except TypeError:
            # 不同版本签名差异时退回分页接口
            page = 1
            while len(rows) < limit:
                chunk = await db.get_all_conversations(page, 50)
                if isinstance(chunk, tuple):
                    chunk = chunk[0]
                if not chunk:
                    break
                rows.extend(chunk)
                if len(chunk) < 50:
                    break
                page += 1
        return list(rows or [])[:limit]

    def _fallback_limit(self) -> int:
        """AstrBot 自己的兜底窗口（agent_runner.config.compression.fallback_max_tokens）。

        AstrBot 在 ``astr_main_agent`` 里会把读不到的值直接写进
        ``provider.provider_config["max_context_tokens"]``，所以这个数字可能已经被
        “污染”成兜底值，必须认识它、才能区分「真实配置」与「兜底值」。
        """
        try:
            conf = self.context.get_config()
            value = (
                conf.get("agent_runner", {})
                .get("config", {})
                .get("compression", {})
                .get("fallback_max_tokens", 128000)
            )
            return int(value or 0)
        except Exception:
            return 128000

    async def _model_limit(
        self, umo: str, force: bool = False
    ) -> tuple[str | None, int | None, str]:
        """返回 (模型名, 上下文窗口上限, 来源)。

        取值顺序（从可信到兜底）：
        1. ``manual``   面板里手动填的上限（用户显式覆盖，优先级最高）
        2. ``provider`` Provider 配置里显式填写的 ``max_context_tokens``
        3. ``metadata`` AstrBot 自带的 models.dev 元数据（模型的真实窗口）
        4. ``fallback`` AstrBot 兜底值（仅用于让进度条有个参照，会标注出来）
        5. ``unset``    都没有 → 面板显示“未设置”，并提示手动填写
        """
        now = time.time()
        cached = self._limit_cache.get(umo)
        if not force and cached and cached[3] > now:
            return cached[0], cached[1], cached[2]

        model: str | None = None
        limit: int | None = None
        source = "unset"
        fallback = self._fallback_limit()
        override = _clamp_int(self._settings.get("limit_override"), 0, 10_000_000, 0)

        provider = await self._provider_for(umo)
        if provider is not None:
            model = _provider_model_name(provider) or None

        if override > 0:
            limit, source = override, "manual"
        else:
            provider_value = 0
            if provider is not None:
                try:
                    provider_config = getattr(provider, "provider_config", {}) or {}
                    provider_value = int(provider_config.get("max_context_tokens", 0) or 0)
                except Exception as exc:
                    self._dbg(f"读取 provider 上限失败: {exc}")

            metadata_value = 0
            if model:
                try:
                    from astrbot.core.utils.llm_metadata import LLM_METADATAS

                    info = LLM_METADATAS.get(model)
                    if isinstance(info, dict):
                        metadata_value = int((info.get("limit") or {}).get("context") or 0)
                except Exception as exc:
                    self._dbg(f"读取模型元数据失败: {exc}")

            if provider_value > 0 and provider_value != fallback:
                limit, source = provider_value, "provider"
            elif metadata_value > 0:
                limit, source = metadata_value, "metadata"
            elif provider_value > 0:
                limit, source = provider_value, "fallback"
            elif fallback > 0:
                # 连 Provider 都没写时要区分“AstrBot 兜底”与“完全未知”，
                # 兜底值只作参照，不当作真实上限。
                limit, source = None, "unset"

        self._limit_cache[umo] = (model, limit, source, now + 120)
        return model, limit, source

    # ------------------------------------------------------------------ #
    # 网页会话备注（#标题）与 AI 摘要
    # ------------------------------------------------------------------ #
    def _load_meta(self) -> dict[str, Any]:
        """懒加载 session_meta.json；任何异常都只记日志并返回空表。"""
        if self._meta is not None:
            return self._meta
        data: dict[str, Any] = {}
        try:
            if self.meta_path.exists():
                raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
        except Exception as exc:
            self._dbg(f"读取会话备注失败: {exc}")
        self._meta = data
        return data

    def _save_meta(self) -> bool:
        """原子写 session_meta.json。"""
        try:
            data = self._load_meta()
            tmp = self.meta_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.meta_path)
            return True
        except Exception as exc:
            self._dbg(f"保存会话备注失败: {exc}")
            return False

    def _meta_entry(self, cid: str) -> dict[str, Any]:
        item = self._load_meta().get(cid)
        return dict(item) if isinstance(item, dict) else {}

    def _set_title(self, cid: str, title: str) -> None:
        data = self._load_meta()
        entry = dict(data.get(cid) or {})
        entry["title"] = title[:40]
        entry["title_at"] = time.time()
        data[cid] = entry
        self._save_meta()

    def _clear_title(self, cid: str) -> None:
        data = self._load_meta()
        entry = dict(data.get(cid) or {})
        if entry.pop("title", None) is not None:
            entry["title_at"] = time.time()
            data[cid] = entry
            self._save_meta()

    def _set_parent(self, cid: str, parent_cid: str, parent_label: str = "") -> None:
        """记录承接派生关系（只挂在新会话上，父会话条目一字不动）。"""
        data = self._load_meta()
        entry = dict(data.get(cid) or {})
        entry["parent_cid"] = parent_cid
        entry["parent_label"] = parent_label[:60]
        entry["parent_at"] = time.time()
        data[cid] = entry
        self._save_meta()

    def _drop_meta(self, cid: str) -> None:
        """会话被清空/删除时一并丢弃备注与摘要。"""
        data = self._load_meta()
        if cid in data:
            data.pop(cid, None)
            self._save_meta()

    def _summary_usable(self, entry: dict[str, Any], msg_count: int) -> bool:
        """缓存是否还能用：消息数没变、且（有摘要或仍在失败冷却中）。"""
        if not entry or entry.get("msg_count") != msg_count:
            return False
        if entry.get("summary"):
            return True
        failed_at = float(entry.get("failed_at") or 0)
        return bool(failed_at) and (time.time() - failed_at) < SUMMARY_FAIL_COOLDOWN

    def _remember_summary(self, cid: str, summary: str, msg_count: int) -> None:
        data = self._load_meta()
        entry = dict(data.get(cid) or {})
        entry["summary"] = summary
        entry["msg_count"] = msg_count
        entry["summary_at"] = time.time()
        entry.pop("failed_at", None)
        data[cid] = entry
        self._save_meta()

    def _remember_failure(self, cid: str, msg_count: int) -> None:
        data = self._load_meta()
        entry = dict(data.get(cid) or {})
        entry["failed_at"] = time.time()
        entry["msg_count"] = msg_count
        data[cid] = entry
        self._save_meta()

    async def _web_extra(self, umo: str, cid: str) -> dict[str, Any]:
        """取网页会话的消息条数与对话片段（只读，失败返回空）。"""
        info: dict[str, Any] = {"msg_count": 0, "snippet": ""}
        try:
            conv = await self.context.conversation_manager.get_conversation(umo, cid)
        except Exception as exc:
            self._dbg(f"读取网页会话失败: {exc}")
            return info
        if conv is None:
            return info
        history = getattr(conv, "history", None)
        try:
            messages = json.loads(history) if isinstance(history, str) else (history or [])
        except Exception:
            messages = []
        if isinstance(messages, list):
            info["msg_count"] = len(messages)
        info["snippet"] = _history_snippet(history)
        return info

    async def _make_summary(self, umo: str, cid: str, msg_count: int, snippet: str) -> str:
        """调用当前会话模型生成不超过 20 字的摘要；任何异常都降级为「摘要获取失败」。"""
        try:
            if not snippet.strip():
                summary = "无对话内容"
                self._remember_summary(cid, summary, msg_count)
                return summary
            provider = await self.context.get_using_provider_async(umo=umo)
            if provider is None:
                raise RuntimeError("没有可用模型")
            prompt = (
                "把下面这段网页聊天记录概括成一句中文短语，不超过 20 个字，"
                "只输出概括本身，不要标点、引号或解释。\n\n" + snippet
            )
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt="你是极简摘要器，只输出不超过 20 个字的中文短语，不要标点。",
                ),
                timeout=SUMMARY_TIMEOUT,
            )
            text = " ".join(str(getattr(resp, "completion_text", "") or "").split())
            text = text.strip(" \"'“”‘’。，,、!！?？")
            if len(text) > 20:
                text = text[:20]
                # 截断落在英文/数字中间时，回退到上一个空格，避免出现 “V4.1 Fla” 这种半截词
                if re.search(r"[A-Za-z0-9]", text[-1:]):
                    pos = text.rfind(" ")
                    if pos > 6:
                        text = text[:pos]
            text = text[:20]
            if not text:
                raise RuntimeError("模型返回空摘要")
            self._remember_summary(cid, text, msg_count)
            return text
        except Exception as exc:
            self._dbg(f"生成摘要失败 {cid[:8]}: {exc}")
            self._remember_failure(cid, msg_count)
            return "摘要获取失败"

    async def _conversation_usage(self, umo: str) -> dict[str, Any]:
        """读取某个会话的官方 token 用量。"""
        data: dict[str, Any] = {
            "umo": umo,
            "cid": None,
            "used": 0,
            "msg_count": 0,
            "updated_at": "",
            "title": "",
        }
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return data
            conv = await cm.get_conversation(umo, cid)
            if conv is None:
                return data
            data["cid"] = cid
            data["used"] = int(getattr(conv, "token_usage", 0) or 0)
            data["title"] = getattr(conv, "title", "") or ""
            history = getattr(conv, "history", "") or ""
            try:
                parsed = json.loads(history) if history else []
                if isinstance(parsed, list):
                    data["msg_count"] = len(parsed)
            except Exception:
                pass
        except Exception as exc:
            self._dbg(f"读取会话用量失败 {umo}: {exc}")
        return data

    async def _session_info(self, umo: str) -> dict[str, Any]:
        """汇总一个会话的：模型、上限、已用、剩余、百分比。"""
        usage = await self._conversation_usage(umo)
        model, limit, source = await self._model_limit(umo)
        used = usage["used"]
        percent = None
        remaining = None
        if limit and limit > 0:
            percent = round(used / limit * 100.0, 1)
            remaining = max(0, limit - used)
        info = {
            **usage,
            "model": model,
            "limit": limit,
            "limit_source": source,
            "remaining": remaining,
            "percent": percent,
        }
        return info

    # ------------------------------------------------------------------ #
    # 卡片文案
    # ------------------------------------------------------------------ #
    def _card_text(self, info: dict[str, Any]) -> str:
        """构造 /token_ctx 的文本卡片（不依赖图片，任何平台都能看）。"""
        warn = int(self._settings.get("warn_percent", 80))
        lines = ["📊 当前会话上下文用量"]

        model = info.get("model") or "未知模型"
        lines.append(f"模型：{model}")

        used = info.get("used") or 0
        limit = info.get("limit")
        if not limit:
            lines.append(f"已用：{_fmt_int(used)} tokens")
            lines.append("该模型未设置窗口上限，无法计算占比")
            return "\n".join(lines)

        percent = info.get("percent") or 0.0
        label, level = _status_of(percent, warn)
        icon = {"normal": "✅", "warn": "⚠️", "high": "🔴", "unknown": "❔"}[level]

        lines.append(f"窗口上限：{_fmt_int(limit)} tokens")
        lines.append(f"已用：{_fmt_int(used)} tokens（{percent:.1f}%）")
        lines.append(f"剩余：{_fmt_int(info.get('remaining'))} tokens")
        lines.append(f"{_render_bar(percent)} {percent:.1f}%")
        lines.append(f"{icon} 状态：{label}（预警线 {warn}%）")
        if info.get("msg_count"):
            lines.append(f"当前会话消息数：{info['msg_count']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 上下文压缩 / 承接 / 统计（集成模块；逻辑、摘要模板、话术均原样保留）
    # ------------------------------------------------------------------ #
    @property
    def carry(self) -> dict[str, Any]:
        """压缩承接设置（缺项自动回落到原版默认值）。"""
        carry = self._settings.get("carry")
        if not isinstance(carry, dict):
            carry = dict(CARRY_DEFAULTS)
            self._settings["carry"] = carry
        return carry

    @property
    def token_limit(self) -> int:
        try:
            return max(1000, int(self.carry.get("token_limit") or 128000))
        except (TypeError, ValueError):
            return 128000

    @property
    def keep_rounds(self) -> int:
        try:
            return max(1, int(self.carry.get("keep_rounds") or 8))
        except (TypeError, ValueError):
            return 8

    @property
    def auto_threshold(self) -> float:
        try:
            return min(0.95, max(0.3, float(self.carry.get("auto_threshold") or 0.8)))
        except (TypeError, ValueError):
            return 0.8

    @property
    def summary_max_chars(self) -> int:
        try:
            return max(100, int(self.carry.get("summary_max_chars") or 500))
        except (TypeError, ValueError):
            return 500

    @property
    def enhanced_summary(self) -> bool:
        """增强摘要开关，默认关闭；关闭时摘要逻辑与原版完全一致。"""
        return bool(self.carry.get("enhanced_summary", False))

    def _auto_enabled(self) -> bool:
        return bool(self.carry.get("auto_enabled", False))

    def _set_auto_enabled(self, value: bool) -> bool:
        self.carry["auto_enabled"] = bool(value)
        return self._save_settings()

    async def _history_of(self, umo: str, cid: str | None = None) -> tuple[str | None, list[dict]]:
        """读取会话历史。cid 为空时取当前会话（与原 /压缩 的取法一致）。"""
        cm = self.context.conversation_manager
        if not cid:
            cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            return None, []
        conv = await cm.get_conversation(umo, cid)
        raw = getattr(conv, "history", None) if conv else None
        if not raw:
            # 会话管理器里取不到时回落到数据库行本身：某些网页会话只在库里，
            # 只看内存会把「有内容」误判成空会话（1.3.3 修复）。
            try:
                row = await self._db().get_conversation_by_id(cid)
                raw = getattr(row, "content", None) if row else None
            except Exception as exc:  # 读库失败一律视为「内容不明」，宁可不动
                logger.warning(f"[token_panel] 读库取历史失败 cid={cid[:8]}: {exc}")
                raise
        if not raw:
            return cid, []
        try:
            data = json.loads(raw)
            return cid, data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning(f"[token_panel] 解析历史失败: {exc}")
            return cid, []

    def _backup_history(self, umo: str, cid: str, history: list[dict]) -> tuple[str, str]:
        """压缩/承接前把完整旧历史备份到插件数据目录（每会话最多 20 份）。

        返回 (备份文件名, 失败原因)。成功时失败原因为空串；失败时只有原因，不影响本次压缩。
        """
        try:
            safe = re.sub(r"[^0-9A-Za-z._-]+", "_", umo)[:60]
            folder = self.carry_backup_dir / safe
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{cid[:8]}-{time.strftime('%Y%m%d-%H%M%S')}.json"
            path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
            # 写入后立刻校验，避免磁盘满/权限问题留下 0 字节文件却报成功
            if not path.exists() or path.stat().st_size <= 2:
                raise OSError("备份文件写入后为空")
        except Exception as exc:
            logger.warning(f"[token_panel] 备份失败: {exc}")
            return "", f"{type(exc).__name__}: {exc}"
        self._prune_backups(folder)
        return path.name, ""

    def _prune_backups(self, folder: Path) -> int:
        """严格把单会话备份控制在 CARRY_BACKUP_LIMIT 份以内，返回删除份数。"""
        removed = 0
        try:
            files = sorted(
                (p for p in folder.glob("*.json") if p.is_file()),
                key=lambda p: (p.stat().st_mtime, p.name),
                reverse=True,
            )
            for old_file in files[CARRY_BACKUP_LIMIT:]:
                try:
                    old_file.unlink()
                    removed += 1
                except OSError as exc:
                    self._dbg(f"删除旧备份失败 {old_file.name}: {exc}")
        except Exception as exc:
            self._dbg(f"清理旧备份失败: {exc}")
        if removed:
            self._dbg(f"备份老化清理：删除 {removed} 份最旧备份")
        return removed

    async def _provider_for(self, umo: str):
        try:
            prov = await self.context.get_using_provider_async(umo=umo)
        except Exception:
            prov = None
        if prov is None:
            try:
                prov = self.context.get_using_provider(umo=umo)
            except Exception:
                prov = None
        return prov

    async def _summarize(self, umo: str, head: list[dict]) -> str:
        """用对话模型把较早的历史压成摘要（摘要模板与原版逐字一致）。

        增强摘要模式（默认关闭）只在原模板之后追加额外要求，原模板不改一个字；
        关闭时走的分支与原版完全相同。
        """
        prov = await self._provider_for(umo)
        if prov is None:
            raise RuntimeError("没有可用的对话模型，先检查模型配置")
        transcript = build_transcript(head)
        if not transcript:
            return ""
        prompt = SUMMARY_PROMPT.format(max_chars=self.summary_max_chars)
        if self.enhanced_summary:
            prompt += ENHANCED_SUMMARY_EXTRA.format(max_chars=self.summary_max_chars)
        resp = await prov.text_chat(
            prompt=prompt + "\n\n---\n" + transcript,
            system_prompt="你是聊天记录摘要助手，只做压缩，不参与对话，不输出多余解释。",
        )
        return (getattr(resp, "completion_text", None) or "").strip()

    @staticmethod
    def _summary_message(text: str, meta: str) -> dict:
        return {
            "role": "assistant",
            "content": [{"type": "text", "text": f"【前情摘要 · {meta}】\n{text}"}],
        }

    @staticmethod
    def _parse_keep(event: AstrMessageEvent) -> int | None:
        parts = (event.message_str or "").split()
        if len(parts) > 1 and parts[1].isdigit():
            return max(0, min(50, int(parts[1])))
        return None

    # ---------- 压缩日志（只记录，不参与逻辑） ----------
    def _log_label(self, umo: str, cid: str | None = None) -> str:
        """给压缩日志生成人类可读的会话名（复用面板的 umo 解析器）。"""
        try:
            platform, message_type, session = _split_umo(umo)
            label = _label_of(_kind_of(message_type, platform), platform, session, "")
        except Exception:
            label = ""
        short = str(cid or "")[:8]
        if label and short:
            return f"{label} · {short}"
        return label or short or "会话"

    def _carry_log_read(self) -> list[dict[str, Any]]:
        try:
            if not self.carry_log_path.exists():
                return []
            data = json.loads(self.carry_log_path.read_text(encoding="utf-8"))
            return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        except Exception as exc:
            self._dbg(f"读取压缩日志失败: {exc}")
            return []

    def _carry_log_add(self, entry: dict[str, Any]) -> None:
        try:
            items = self._carry_log_read()
            items.insert(0, entry)
            tmp = self.carry_log_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(items[:CARRY_LOG_LIMIT], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.carry_log_path)
        except Exception as exc:
            self._dbg(f"写入压缩日志失败: {exc}")

    # ---------- /ctx 用到的官方 token 记录 ----------
    async def _last_input_tokens(self, umo: str, cid: str | None = None) -> tuple[int, bool]:
        """返回 (最近一次内部模型调用的输入 token 合计, 是否有记录)。"""
        try:
            cm = self.context.conversation_manager
            if not cid:
                cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return 0, False
            db = self._db()
            async with db.get_db() as session:
                result = await session.execute(
                    select(
                        ProviderStat.token_input_cached,
                        ProviderStat.token_input_other,
                    )
                    .where(
                        col(ProviderStat.agent_type) == "internal",
                        col(ProviderStat.conversation_id) == cid,
                    )
                    .order_by(col(ProviderStat.id).desc())
                    .limit(1)
                )
                row = result.first()
        except Exception as exc:
            logger.warning(f"[token_panel] 读取会话 token 记录失败: {exc}")
            return 0, False
        if row is None:
            return 0, False
        return int(row[0] or 0) + int(row[1] or 0), True

    async def _resolve_ctx_limit(self, umo: str, force: bool = True) -> tuple[int | None, str]:
        """模型窗口上限（与面板同一套解析逻辑，force 会跳过缓存重新读取）。"""
        _model, limit, source = await self._model_limit(umo, force=force)
        return limit, source

    # ---------- 压缩 / 承接 核心（返回值含给面板展示的日志字段） ----------
    async def _compress_core(self, umo: str, cid: str | None, keep: int) -> dict[str, Any]:
        keep = max(1, keep)
        cid, history = await self._history_of(umo, cid)
        if not cid:
            return {"ok": False, "text": "当前还没有会话，先聊两句吧。", "before": 0, "after": 0}
        head, tail = split_history(history, keep)
        if len(head) < 2:
            return {
                "ok": False,
                "text": f"现在只有 {rounds_of(history)} 轮，不够压（要保留最近 {keep} 轮）。先多聊几句。",
                "before": 0,
                "after": 0,
            }
        before = est_history_tokens(history)
        try:
            summary = await self._summarize(umo, head)
        except Exception as exc:
            logger.error(f"[token_panel] 压缩失败: {exc}")
            return {"ok": False, "text": f"压缩失败：{exc}", "before": before, "after": before}
        if not summary:
            return {
                "ok": False,
                "text": "模型没给出摘要，这次先不压，原对话没动。",
                "before": before,
                "after": before,
            }

        backup_name, backup_error = self._backup_history(umo, cid, history)
        new_history = [self._summary_message(summary, f"压缩自前 {rounds_of(head)} 轮")] + tail
        await self.context.conversation_manager.update_conversation(
            umo, conversation_id=cid, history=new_history, token_usage=0
        )
        after = est_history_tokens(new_history)
        logger.info(
            f"[token_panel] 压缩完成 cid={cid[:8]} 轮数 {rounds_of(history)}->{rounds_of(new_history)} "
            f"tokens {before}->{after} 备份={backup_name or '失败'}"
        )
        backup_line = backup_name or f"（备份失败：{backup_error or '未知原因'}，不影响本次压缩）"
        text = (
            "【压缩完成】\n"
            "操作类型：手动压缩\n"
            f"会话 ID：{cid}\n"
            f"压缩前估算：{before} tokens（{rounds_of(history)} 轮）\n"
            f"压缩后估算：{after} tokens（{rounds_of(new_history)} 轮，保留最近 {keep} 轮原文）\n"
            f"备份文件名：{backup_line}\n"
            f"— 摘要 —\n{summary}"
        )
        self._carry_log_add(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": "compress",
                "type": "手动压缩",
                "cid": cid,
                "umo": umo,
                "label": self._log_label(umo, cid),
                "before": before,
                "after": after,
                "keep": keep,
                "rounds_before": rounds_of(history),
                "rounds_after": rounds_of(new_history),
                "backup": backup_name,
                "backup_ok": bool(backup_name),
                "backup_error": backup_error,
                "new_cid": "",
            }
        )
        return {
            "ok": True,
            "text": text,
            "before": before,
            "after": after,
            "cid": cid,
            "backup": backup_name,
            "backup_error": backup_error,
        }

    async def _continue_core(self, umo: str, cid: str | None, keep: int) -> dict[str, Any]:
        cid, history = await self._history_of(umo, cid)
        if not cid or not history:
            return {"ok": False, "text": "当前没有可承接的内容，先聊两句吧。"}
        try:
            summary = await self._summarize(umo, history)
        except Exception as exc:
            logger.error(f"[token_panel] 承接摘要失败: {exc}")
            return {"ok": False, "text": f"承接失败：{exc}"}
        if not summary:
            return {"ok": False, "text": "模型没给出摘要，这次先不换会话。"}

        _, tail = split_history(history, keep) if keep > 0 else (history, [])
        new_content = [self._summary_message(summary, f"承接自 {cid[:8]}")] + tail
        backup_name, backup_error = self._backup_history(umo, cid, history)

        cm = self.context.conversation_manager
        old_conv = await cm.get_conversation(umo, cid)
        platform_id = umo.split(":", 1)[0] if ":" in umo else ""
        try:
            new_cid = await cm.new_conversation(
                umo,
                platform_id=platform_id,
                content=new_content,
                title=f"承接自 {cid[:8]}",
                persona_id=getattr(old_conv, "persona_id", None) or None,
            )
        except Exception as exc:
            logger.error(f"[token_panel] 新建会话失败: {exc}")
            return {"ok": False, "text": f"新建会话失败：{exc}（原会话没动）"}

        # 继承父会话的自定义备注与 AI 摘要；父会话自身数据不动
        parent_entry = self._meta_entry(cid)
        inherited_note = str(parent_entry.get("title") or "")
        inherited_summary = str(parent_entry.get("summary") or "")
        try:
            if inherited_note:
                self._set_title(new_cid, inherited_note)
            if inherited_summary:
                self._remember_summary(new_cid, inherited_summary, 0)
            self._set_parent(new_cid, cid, self._log_label(umo, cid))
        except Exception as exc:
            self._dbg(f"继承父会话备注失败: {exc}")

        # 日记本跟随会话完整继承（独立于压缩/承接逻辑，只做整本复制）
        try:
            inherited_diary = self._diary_inherit(umo, cid, new_cid)
            if inherited_diary:
                self._dbg(f"继承日记 {inherited_diary} 篇")
        except Exception as exc:
            logger.warning(f"[token_panel] 继承日记失败: {exc}")

        before = est_history_tokens(history)
        after = est_history_tokens(new_content)
        backup_line = backup_name or f"（备份失败：{backup_error or '未知原因'}，不影响本次承接）"
        text = (
            "【承接完成】\n"
            "操作类型：手动承接\n"
            f"会话 ID：{cid}\n"
            f"新会话 ID：{new_cid}\n"
            f"压缩前估算：{before} tokens\n"
            f"压缩后估算：{after} tokens（摘要 {len(summary)} 字 + 最近 {keep} 轮原文）\n"
            f"备份文件名：{backup_line}\n"
            f"已切到新会话；想回旧会话发：/ctx_switch {cid[:8]}\n"
            f"— 摘要 —\n{summary}"
        )
        self._carry_log_add(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": "continue",
                "type": "手动承接",
                "cid": cid,
                "new_cid": new_cid,
                "umo": umo,
                "label": f"{self._log_label(umo, cid)} → {new_cid[:8]}",
                "before": before,
                "after": after,
                "keep": keep,
                "rounds_before": rounds_of(history),
                "rounds_after": rounds_of(new_content),
                "backup": backup_name,
                "backup_ok": bool(backup_name),
                "backup_error": backup_error,
            }
        )
        return {
            "ok": True,
            "text": text,
            "cid": cid,
            "new_cid": new_cid,
            "backup": backup_name,
            "backup_error": backup_error,
        }

    # ------------------------------------------------------------------ #
    # 会话日记本（v1.6.0）
    # 独立存储、独立检索、独立开关：全程不经过任何压缩 / 承接函数，
    # 也不参与上下文摘要合并，只做「写过什么」的长期备忘。
    # ------------------------------------------------------------------ #
    def _diary_cfg(self) -> dict[str, Any]:
        """读取日记本配置（永远返回完整字段，手改坏了也不会炸）。"""
        raw = self._settings.get("diary")
        cfg = dict(DIARY_DEFAULTS)
        if isinstance(raw, dict):
            cfg.update(raw)
        cfg["enable"] = bool(cfg.get("enable", False))
        cfg["max_per_day"] = _clamp_int(cfg.get("max_per_day"), 1, 3, 3)
        cfg["max_single_length"] = _clamp_int(cfg.get("max_single_length"), 60, 1000, 260)
        cfg["inject_relevant_count"] = _clamp_int(cfg.get("inject_relevant_count"), 1, 10, 3)
        return cfg

    def _diary_enabled(self) -> bool:
        """总开关：关闭时读写、检索注入、标签识别全部休眠。"""
        return bool(self._diary_cfg().get("enable"))

    @staticmethod
    def _diary_key(raw: str, fallback: str = "unknown") -> str:
        """把 umo / cid 洗成能当目录名的安全串（只保留字母数字与 ._@-）。"""
        text = re.sub(r"[^0-9A-Za-z_.@-]+", "_", str(raw or "")).strip("._- ")
        return text[:80] or fallback

    def _diary_root(self) -> Path:
        return self.data_dir / "diary_log"

    def _diary_path(self, umo: str, cid: str = "") -> Path:
        """diary_log/{platform}_{person}/diary.json —— 按人存，跨会话共享。

        同一个人换了会话（或做了「承接」）仍然翻得到自己的旧日记；
        网页面板的每个聊天会话是独立伪用户，统一归到同一个面板主人账下。
        """
        platform, _mtype, sid = _split_umo(umo)
        person = str(sid or umo)
        if str(platform).lower().startswith("webchat"):
            parts = person.split("!")
            person = parts[1] if len(parts) > 1 and parts[1] else "astrbot"
        platform_key = self._diary_key(platform, "webchat")
        person_key = self._diary_key(person)
        return self._diary_root() / f"{platform_key}_{person_key}" / "diary.json"

    def _diary_read(self, umo: str, cid: str) -> list[dict[str, Any]]:
        """读某个会话的全部日记（最新在前）；任何异常都返回空表，不向上抛。"""
        if not umo:
            return []
        try:
            path = self._diary_path(umo, cid)
            if not path.exists():
                return []
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[token_panel] 读取日记失败: {exc}")
            return []
        entries = raw.get("entries") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return []
        out: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            weight = str(item.get("weight") or "medium").strip().lower()
            if weight not in DIARY_WEIGHT_RANK:
                weight = "medium"
            stamp = str(item.get("time") or "").strip()
            day = str(item.get("date") or "").strip() or stamp[:10]
            out.append(
                {
                    "time": stamp,
                    "date": day,
                    "content": content,
                    "weight": weight,
                    "cid": str(item.get("cid") or ""),
                    "umo": str(item.get("umo") or ""),
                }
            )
        out.sort(key=lambda e: str(e.get("time") or ""), reverse=True)
        return out

    def _diary_write(self, umo: str, cid: str, entries: list[dict[str, Any]]) -> bool:
        """整表落盘（原子替换）；写失败只记 warning，不影响聊天。"""
        try:
            path = self._diary_path(umo, cid)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "umo": str(umo or ""),
                "cid": str(cid or ""),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "entries": entries,
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception as exc:
            logger.warning(f"[token_panel] 写入日记失败: {exc}")
            return False

    def _diary_add(self, umo: str, cid: str, content: str, weight: str = "medium") -> dict[str, Any] | None:
        """追加一篇日记；超过当日上限、重复内容、未达标一律安静跳过。"""
        try:
            if not self._diary_enabled():
                return None
            text = re.sub(r"\s+", " ", str(content or "")).strip()
            if not text:
                return None
            cfg = self._diary_cfg()
            text = text[: int(cfg["max_single_length"])]
            weight = str(weight or "medium").strip().lower()
            if weight not in DIARY_WEIGHT_RANK:
                weight = "medium"
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            entries = self._diary_read(umo, cid)
            if any(str(e.get("content") or "") == text for e in entries):
                return None
            today_count = sum(1 for e in entries if str(e.get("date") or "") == today)
            if today_count >= int(cfg["max_per_day"]):
                return None
            entry = {
                "time": time.strftime("%Y-%m-%d %H:%M", now),
                "date": today,
                "content": text,
                "weight": weight,
                "cid": str(cid or ""),
                "umo": str(umo or ""),
            }
            entries.insert(0, entry)
            if not self._diary_write(umo, cid, entries):
                return None
            return entry
        except Exception as exc:
            logger.warning(f"[token_panel] 记录日记失败: {exc}")
            return None

    def _diary_clear(self, umo: str, cid: str) -> int:
        """清空某个会话的日记本，返回清掉的条数。"""
        count = len(self._diary_read(umo, cid))
        try:
            path = self._diary_path(umo, cid)
            if path.exists():
                path.unlink()
            parent = path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception as exc:
            logger.warning(f"[token_panel] 清空日记失败: {exc}")
        return count

    def _diary_inherit(self, umo: str, old_cid: str, new_cid: str) -> int:
        """（已废弃）日记改成按人存后，承接无需复制，保留函数只为兼容旧调用。"""
        try:
            old = self._diary_read(umo, old_cid)
            if not old:
                return 0
            merged = self._diary_read(umo, new_cid)
            seen = {(str(e.get("time") or ""), str(e.get("content") or "")) for e in merged}
            added = 0
            for entry in old:
                key = (str(entry.get("time") or ""), str(entry.get("content") or ""))
                if key in seen:
                    continue
                merged.append(dict(entry))
                seen.add(key)
                added += 1
            if added <= 0:
                return 0
            merged.sort(key=lambda e: str(e.get("time") or ""), reverse=True)
            return added if self._diary_write(umo, new_cid, merged) else 0
        except Exception as exc:
            logger.warning(f"[token_panel] 继承日记失败: {exc}")
            return 0

    # ---- 相似度检索：只召回与当前提问相关的少量日记 ---------------- #
    _DIARY_STOP_BIGRAMS = frozenset(
        {
            "的了", "了吗", "呢吧", "是的", "我在", "你在", "这个", "那个", "什么",
            "怎么", "我们", "你们", "他们", "就是", "还是", "可以", "没有", "不是",
            "因为", "所以", "但是", "如果", "已经", "一个", "一下", "现在", "今天",
        }
    )

    @classmethod
    def _diary_tokens(cls, text: str) -> set[str]:
        """粗粒度分词：英文/数字取词，中文取二字组（去掉高频虚词）。"""
        low = str(text or "").lower()
        tokens = set(re.findall(r"[a-z0-9]{2,}", low))
        for chunk in re.findall(r"[\u4e00-\u9fff]+", low):
            if len(chunk) == 1:
                tokens.add(chunk)
                continue
            for i in range(len(chunk) - 1):
                bigram = chunk[i : i + 2]
                if bigram not in cls._DIARY_STOP_BIGRAMS:
                    tokens.add(bigram)
        return tokens

    @staticmethod
    def _diary_score(query_tokens: set[str], entry_tokens: set[str]) -> float:
        """0~1 的相似度：以「提问被覆盖多少」为主，日记自身的覆盖为辅。"""
        if not query_tokens or not entry_tokens:
            return 0.0
        hit = query_tokens & entry_tokens
        if not hit:
            return 0.0
        q_cover = len(hit) / len(query_tokens)
        e_cover = len(hit) / len(entry_tokens)
        return q_cover * 0.7 + e_cover * 0.3

    def _diary_retrieve(self, umo: str, cid: str, query: str, limit: int) -> list[dict[str, Any]]:
        """取相关度最高的若干篇：先按权重 high>medium>low，再按相似度。"""
        entries = self._diary_read(umo, cid)
        if not entries or limit <= 0:
            return []
        query_tokens = self._diary_tokens(query)
        if not query_tokens:
            return []
        scored: list[tuple[int, float, dict[str, Any]]] = []
        for entry in entries:
            score = self._diary_score(query_tokens, self._diary_tokens(str(entry.get("content") or "")))
            if score < DIARY_RELEVANCE_FLOOR:
                continue
            scored.append((DIARY_WEIGHT_RANK.get(str(entry.get("weight")), 2), score, entry))
        scored.sort(key=lambda item: (-item[0], -item[1], str(item[2].get("time") or "")), reverse=False)
        return [item[2] for item in scored[:limit]]

    def _diary_context_block(self, umo: str, cid: str, query: str) -> tuple[str, int]:
        """拼出要追加进系统提示的那一段（写日记规则 + 相关日记）。"""
        cfg = self._diary_cfg()
        rule = DIARY_WRITE_RULE.format(
            max_per_day=int(cfg["max_per_day"]),
            max_len=int(cfg["max_single_length"]),
        )
        related = self._diary_retrieve(umo, cid, query, int(cfg["inject_relevant_count"]))
        if not related:
            return rule, 0
        lines = [rule, "", "【相关日记】（只作参考，不要在回复里复述）"]
        for item in related:
            stamp = str(item.get("time") or "")[5:16]
            lines.append(f"- {stamp}｜{DIARY_WEIGHT_CN.get(str(item.get('weight')), '中')}权重：{item.get('content')}")
        return "\n".join(lines), len(related)

    def _diary_rule_block(self) -> tuple[str, int]:
        """只要「写日记」的规则文本（常量部分，不影响前缀缓存）。"""
        cfg = self._diary_cfg()
        rule = DIARY_WRITE_RULE.format(
            max_per_day=int(cfg["max_per_day"]),
            max_len=int(cfg["max_single_length"]),
        )
        return rule, 0

    def _inject_extra(self, req: Any, text: str) -> bool:
        """把一段临时内容挂到本轮的 user 侧（不落库、不改系统提示）。

        ProviderRequest.extra_user_content_parts 会跟在用户消息后面发给模型，
        标记 mark_as_temp() 后不会写进会话历史，因此不会让系统提示前缀每轮变化。
        """
        text = str(text or "").strip()
        if not text:
            return False
        try:
            from astrbot.core.agent.message import TextPart

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                # 老版本 AstrBot 没有临时内容通道：退回拼到系统提示末尾（功能不丢）
                base = str(getattr(req, "system_prompt", "") or "")
                req.system_prompt = (base + "\n\n" + text).strip()
                return True
            for part in parts:
                if str(getattr(part, "text", "") or "") == text:
                    return False
            parts.append(TextPart(text=text).mark_as_temp())
            return True
        except Exception as exc:
            self._dbg(f"注入临时内容失败: {exc}")
            return False

    @staticmethod
    def _diary_response_text(resp: Any) -> str:
        """从 LLMResponse 里拼出正文（流式与非流式都走 result_chain）。"""
        try:
            chain = getattr(resp, "result_chain", None)
            parts = getattr(chain, "chain", None) or []
            pieces: list[str] = []
            for comp in parts:
                text = getattr(comp, "text", None)
                if isinstance(text, str) and text:
                    pieces.append(text)
            return "".join(pieces)
        except Exception:
            return ""

    @staticmethod
    def _diary_extract(text: str) -> list[tuple[str, str]]:
        """抠出 <diary weight=...>正文</diary>，返回 [(权重, 正文)]。"""
        out: list[tuple[str, str]] = []
        for attrs, body in DIARY_TAG_RE.findall(str(text or "")):
            content = re.sub(r"\s+", " ", str(body or "")).strip()
            if not content:
                continue
            match = DIARY_WEIGHT_ATTR_RE.search(str(attrs or ""))
            weight = match.group(1).strip().lower() if match else "medium"
            if weight not in DIARY_WEIGHT_RANK:
                weight = "medium"
            out.append((weight, content))
        return out

    def _diary_groups(self, umo: str, cid: str) -> list[dict[str, Any]]:
        """按自然日分组（最新日期在前，同日内最新在前），面板与指令共用。"""
        groups: list[dict[str, Any]] = []
        current = None
        for entry in self._diary_read(umo, cid):
            day = str(entry.get("date") or "")
            if current is None or current.get("date") != day:
                current = {"date": day, "items": []}
                groups.append(current)
            current["items"].append(dict(entry))
        return groups

    def _diary_text(self, umo: str, cid: str) -> str:
        """给 /ctx_diary 用的纯文本视图。"""
        cfg = self._diary_cfg()
        entries = self._diary_read(umo, cid)
        if not entries:
            return (
                "日记本还是空的。\n"
                + ("日记本当前是关闭的，打开后才会开始记录。" if not cfg["enable"] else "等出现值得记的事，我会自己写进来。")
            )
        today = time.strftime("%Y-%m-%d")
        today_count = sum(1 for e in entries if str(e.get("date") or "") == today)
        lines = [
            f"【日记本】共 {len(entries)} 篇 · 今日 {today_count}/{int(cfg['max_per_day'])} 篇",
            f"（独立记忆：不参与上下文压缩与承接）",
        ]
        shown = 0
        for group in self._diary_groups(umo, cid):
            lines.append("")
            lines.append(str(group.get("date") or ""))
            for item in group.get("items") or []:
                if shown >= 12:
                    lines.append("（只显示最近 12 篇，完整内容看面板）")
                    return "\n".join(lines)
                stamp = str(item.get("time") or "")[11:16]
                mark = DIARY_WEIGHT_CN.get(str(item.get("weight")), "中")
                lines.append(f"  {stamp} · {mark}｜{item.get('content')}")
                shown += 1
        return "\n".join(lines)

    # ---- 钩子：注入 / 记录 / 剥离 ---------------------------------- #
    @filter.on_llm_request()
    async def diary_inject(self, event: AstrMessageEvent, req: Any):
        """发往模型前：把与本轮提问最相关的少量日记追加进系统提示。"""
        try:
            if not self._diary_enabled():
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not umo:
                return
            cm = self.context.conversation_manager
            cid = str(await cm.get_curr_conversation_id(umo) or umo)
            query = str(getattr(event, "message_str", "") or "")
            block, count = self._diary_context_block(umo, cid, query)
            if not block:
                return
            base = str(getattr(req, "system_prompt", "") or "")
            if "【日记本】" in base:  # 同一请求里已有，不重复叠加
                return
            rule, _cnt = self._diary_rule_block()
            # 写日记的规则是常量，放系统提示末尾不会让前缀每轮都变
            req.system_prompt = (base + "\n\n" + rule).strip()
            # 「相关日记」每轮可能不同，改挂临时内容块，避免打断前缀缓存
            related = block[len(rule):].strip()
            if related and self._inject_extra(req, related):
                self._dbg(f"日记注入 {count} 条（{cid[:8]}）")
        except Exception as exc:
            logger.warning(f"[token_panel] 日记检索注入失败: {exc}")

    @filter.on_llm_response()
    async def diary_capture(self, event: AstrMessageEvent, resp: Any):
        """模型回复后：识别 <diary> 标签并按权重限度写入日记本。"""
        try:
            if not self._diary_enabled():
                return
            text = self._diary_response_text(resp)
            if "<diary" not in text.lower():
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not umo:
                return
            cm = self.context.conversation_manager
            cid = str(await cm.get_curr_conversation_id(umo) or umo)
            for weight, content in self._diary_extract(text):
                entry = self._diary_add(umo, cid, content, weight)
                if entry:
                    self._dbg(f"写入日记 1 篇（{weight}）")
        except Exception as exc:
            logger.warning(f"[token_panel] 识别日记标签失败: {exc}")

    @filter.on_decorating_result()
    async def diary_strip(self, event: AstrMessageEvent):
        """发送前把日记标签从正文里剥掉；用户永远看不到标记。"""
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None) if result else None
            if not chain:
                return
            for comp in chain:
                text = getattr(comp, "text", None)
                if not isinstance(text, str) or "<diary" not in text.lower():
                    continue
                cleaned = DIARY_TAG_RE.sub("", text)
                cleaned = DIARY_TAIL_RE.sub("", cleaned)
                cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
                comp.text = cleaned
        except Exception as exc:
            logger.warning(f"[token_panel] 剥离日记标签失败: {exc}")

    # ---- Web API --------------------------------------------------- #
    @staticmethod
    def _query_text(name: str) -> str:
        try:
            from astrbot.api.web import request

            return str(request.query.get(name) or "").strip()
        except Exception:
            return ""

    async def _diary_default_target(self) -> tuple[str, str]:
        """没带 umo 时，退到最近的会话上（面板首次打开用）。"""
        try:
            rows = await self._fetch_rows(50)
        except Exception as exc:
            self._dbg(f"日记默认会话失败: {exc}")
            return "", ""
        if not rows:
            return "", ""
        row = rows[0]
        umo = str(getattr(row, "user_id", "") or "")
        cid = str(getattr(row, "conversation_id", "") or "")
        return umo, cid

    async def api_diary_overview(self) -> dict[str, Any]:
        """日记本总览：会话标识 + 分组列表 + 配置。"""
        try:
            umo = self._query_text("umo")
            cid = self._query_text("cid")
            if not umo:
                umo, cid = await self._diary_default_target()
            if not umo:
                return {"status": "error", "message": "还没有可读取的会话"}
            cfg = self._diary_cfg()
            entries = self._diary_read(umo, cid)
            today = time.strftime("%Y-%m-%d")
            today_count = sum(1 for e in entries if str(e.get("date") or "") == today)
            groups = self._diary_groups(umo, cid)
            for group in groups:
                for item in group["items"]:
                    item["clock"] = str(item.get("time") or "")[11:16]
                    item["weight_cn"] = DIARY_WEIGHT_CN.get(str(item.get("weight")), "中")
            return {
                "status": "ok",
                "data": {
                    "umo": umo,
                    "cid": cid,
                    "label": self._log_label(umo, cid),
                    "sessions": await self._diary_sessions(),
                    "settings": cfg,
                    "defaults": dict(DIARY_DEFAULTS),
                    "limits": {k: list(v) for k, v in DIARY_LIMITS.items()},
                    "total": len(entries),
                    "today": today_count,
                    "groups": groups,
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 读取日记本失败: {exc}")
            return {"status": "error", "message": "读取日记本失败，请稍后再试"}

    async def _diary_sessions(self) -> list[dict[str, Any]]:
        """给面板的会话下拉：最近 30 个会话 + 各自日记条数。"""
        out: list[dict[str, Any]] = []
        try:
            rows = await self._fetch_rows(60)
        except Exception:
            return out
        for row in rows:
            umo = str(getattr(row, "user_id", "") or "")
            cid = str(getattr(row, "conversation_id", "") or "")
            if not umo:
                continue
            try:
                count = len(self._diary_read(umo, cid))
            except Exception:
                count = 0
            out.append(
                {
                    "umo": umo,
                    "cid": cid,
                    "label": self._log_label(umo, cid),
                    "count": count,
                }
            )
            if len(out) >= 30:
                break
        return out

    async def api_diary_settings(self) -> dict[str, Any]:
        """读取 / 保存日记本设置（GET 读、POST 部分更新）。"""
        try:
            from astrbot.api.web import request as plugin_request

            method = str(getattr(plugin_request, "method", "GET") or "GET").upper()
            if method == "GET":
                return {
                    "status": "ok",
                    "data": {
                        "diary": self._diary_cfg(),
                        "defaults": dict(DIARY_DEFAULTS),
                        "limits": {k: list(v) for k, v in DIARY_LIMITS.items()},
                    },
                }

            payload = await self._json_body()
            cfg = self._diary_cfg()
            if "enable" in payload:
                cfg["enable"] = bool(payload.get("enable"))
            for key in ("max_per_day", "max_single_length", "inject_relevant_count"):
                if key in payload:
                    low, high = DIARY_LIMITS[key]
                    cfg[key] = _clamp_int(payload.get(key), low, high, int(DIARY_DEFAULTS[key]))
            self._settings["diary"] = cfg
            self._save_settings()
            return {"status": "ok", "data": {"diary": cfg}}
        except Exception as exc:
            logger.warning(f"[token_panel] 保存日记设置失败: {exc}")
            return {"status": "error", "message": "保存失败，请稍后再试"}

    async def api_diary_clear(self) -> dict[str, Any]:
        """清空指定会话的日记本。"""
        try:
            payload = await self._json_body()
            umo = str(payload.get("umo") or "").strip()
            cid = str(payload.get("cid") or "").strip()
            if not umo:
                return {"status": "error", "message": "缺少会话信息"}
            removed = self._diary_clear(umo, cid)
            return {"status": "ok", "data": {"removed": removed, "label": self._log_label(umo, cid)}}
        except Exception as exc:
            logger.warning(f"[token_panel] 清空日记本失败: {exc}")
            return {"status": "error", "message": "清空失败，请稍后再试"}

    # ------------------------------------------------------------------ #
    # QQ 指令
    # ------------------------------------------------------------------ #
    @filter.command("token_ctx")
    async def cmd_token_ctx(self, event: AstrMessageEvent):
        """/token_ctx 查看当前会话的 token 用量进度条（所有人可用）。"""
        try:
            info = await self._session_info(event.unified_msg_origin)
            text = self._card_text(info)
        except Exception as exc:
            logger.warning(f"[token_panel] /token_ctx 读取失败: {exc}")
            text = "读取 Token 数据失败，请稍后再试（机器人仍可正常聊天）。"
        yield event.plain_result(text)

    @filter.command("token_ctxon")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ctx_on(self, event: AstrMessageEvent):
        """/token_ctxon 开启自动进度播报（仅管理员）。"""
        self._settings["auto_broadcast"] = True
        ok = self._save_settings()
        tip = "已开启" if ok else "已开启（但写入配置文件失败，重启后可能恢复旧值）"
        yield event.plain_result(f"🔔 自动进度播报{tip}。达到预警阈值时会提醒一次。")

    @filter.command("token_ctxoff")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ctx_off(self, event: AstrMessageEvent):
        """/token_ctxoff 关闭自动进度播报（仅管理员）。"""
        self._settings["auto_broadcast"] = False
        ok = self._save_settings()
        tip = "已关闭" if ok else "已关闭（但写入配置文件失败）"
        yield event.plain_result(f"🔕 自动进度播报{tip}。")

    @filter.command("token_ctxset")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ctx_set(self, event: AstrMessageEvent):
        """/token_ctxset 80 修改预警百分比阈值（仅管理员）。"""
        raw = (event.message_str or "").strip()
        parts = raw.split()
        arg = parts[1] if len(parts) > 1 else ""
        arg = arg.replace("%", "").replace("％", "").strip()
        if not arg.isdigit():
            yield event.plain_result(
                f"用法：/token_ctxset 80\n当前预警阈值：{self._settings.get('warn_percent')}%"
            )
            return
        value = _clamp_int(arg, 1, 99, int(self._settings.get("warn_percent", 80)))
        self._settings["warn_percent"] = value
        ok = self._save_settings()
        suffix = "" if ok else "（写入配置失败）"
        yield event.plain_result(
            f"已把预警阈值设为 {value}%{suffix}；高危线固定为 {HIGH_PERCENT}%。"
        )

    @filter.command("token_clearctx")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ctx_clear(self, event: AstrMessageEvent):
        """/token_clearctx 清空当前会话上下文（二次确认，仅管理员）。"""
        umo = event.unified_msg_origin
        now = time.time()
        deadline = self._pending_clear.get(umo, 0)
        text = (event.message_str or "").strip()

        # 第二次输入才真正执行（60 秒内有效）
        if deadline > now and "确认" in text:
            try:
                cm = self.context.conversation_manager
                cid = await cm.get_curr_conversation_id(umo)
                if not cid:
                    yield event.plain_result("当前会话还没有上下文，无需清空。")
                    return
                backup_name = await self._backup_before_clear(umo, cid)
                await cm.update_conversation(
                    umo, conversation_id=cid, history=[], token_usage=0
                )
                # 会话清空：日记本同步清空
                try:
                    self._diary_clear(umo, cid)
                except Exception as exc:
                    logger.warning(f"[token_panel] 清空日记失败: {exc}")
                self._pending_clear.pop(umo, None)
                note = f"（旧记录已备份：{backup_name}）" if backup_name else ""
                yield event.plain_result(f"✅ 已清空当前会话上下文{note}。")
            except Exception as exc:
                logger.warning(f"[token_panel] 清空上下文失败: {exc}")
                yield event.plain_result("清空失败，读取会话时出错，请稍后再试。")
            return

        self._pending_clear[umo] = now + CLEAR_CONFIRM_TTL
        yield event.plain_result(
            "⚠️ 即将清空当前会话的全部上下文（不可撤销）。\n"
            f"请在 {CLEAR_CONFIRM_TTL} 秒内回复「/token_clearctx 确认」执行；"
            "回复其他内容即取消。"
        )

    # ------------------------------------------------------------------ #
    # 集成进来的上下文指令（/ctx、/ctx_compress、/ctx_continue）
    # ------------------------------------------------------------------ #
    @filter.command("ctx")
    async def cmd_ctx_stats(self, event: AstrMessageEvent):
        """/ctx 查看当前会话上下文用量（所有人可用，输出与原 ctx_stats 一致）。"""
        umo = event.unified_msg_origin
        try:
            total, has_record = await self._last_input_tokens(umo)
            limit, _source = await self._resolve_ctx_limit(umo)
            lines = ["【上下文统计】"]
            if not has_record:
                lines.append("（这个会话还没记录到模型调用，先聊一句再来）")
            lines.append(f"当前上下文Token：{total}")
            if limit is None:
                lines.append("获取模型上限失败")
                lines.append("占用率：无法计算")
            else:
                pct = total / limit * 100
                lines += [
                    f"模型上限：{limit}",
                    f"占用率：{pct:.1f}%",
                    f"提示：{_ctx_tip(pct)}",
                ]
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.warning(f"[token_panel] /ctx 读取失败: {exc}")
            yield event.plain_result("【上下文统计】\n读取 Token 数据失败，请稍后再试。")

    @filter.command("ctx_compress")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ctx_compress(self, event: AstrMessageEvent):
        """/ctx_compress [保留轮数] 压缩当前会话上下文（仅管理员）。"""
        umo = event.unified_msg_origin
        keep = self._parse_keep(event)
        keep = max(1, self.keep_rounds if keep is None else keep)
        try:
            result = await self._compress_core(umo, None, keep)
            text = str(result.get("text") or "压缩失败：未知错误")
        except Exception as exc:
            logger.warning(f"[token_panel] /ctx_compress 失败: {exc}")
            text = f"压缩失败：{exc}"
        yield event.plain_result(text)

    @filter.command("ctx_continue")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ctx_continue(self, event: AstrMessageEvent):
        """/ctx_continue [保留轮数] 压成摘要并换到新会话（仅管理员）。"""
        umo = event.unified_msg_origin
        keep = self._parse_keep(event)
        keep = 4 if keep is None else keep
        try:
            result = await self._continue_core(umo, None, keep)
            text = str(result.get("text") or "承接失败：未知错误")
        except Exception as exc:
            logger.warning(f"[token_panel] /ctx_continue 失败: {exc}")
            text = f"承接失败：{exc}"
        yield event.plain_result(text)

    @filter.command("ctx_sessions")
    async def cmd_ctx_sessions(self, event: AstrMessageEvent):
        """/ctx_sessions 列出当前会话最近的几个会话（所有人可用）。"""
        umo = event.unified_msg_origin
        cm = self.context.conversation_manager
        cur = await cm.get_curr_conversation_id(umo)
        try:
            convs = await cm.get_conversations(umo)
        except Exception as exc:
            yield event.plain_result(f"读取会话失败：{exc}")
            return
        convs = sorted(convs or [], key=lambda c: getattr(c, "updated_at", 0), reverse=True)[:8]
        if not convs:
            yield event.plain_result("还没有会话记录。")
            return
        rows = []
        carried = 0
        for conv in convs:
            cid = str(getattr(conv, "cid", ""))
            flag = "▶" if cid == cur else "　"
            title = getattr(conv, "title", None) or "（无标题）"
            entry = self._meta_entry(cid)
            parent = str(entry.get("parent_cid") or "")
            if parent:
                carried += 1
                rows.append(f"{flag} {cid[:8]}　{title}\n　　　└ 📌 承接自 {parent[:8]}")
            else:
                rows.append(f"{flag} {cid[:8]}　{title}")
        yield event.plain_result(
            "【最近会话】\n"
            "（▶ 当前会话；普通会话只显示 ID 与标题；📌 承接自 xxxxxxxx 表示由该父会话派生）\n"
            + "\n".join(rows)
            + f"\n\n共 {len(convs)} 个，其中承接派生 {carried} 个。"
            + "\n切换：/ctx_switch <前8位>　承接：/ctx_continue [保留轮数]"
        )

    @filter.command("ctx_switch")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ctx_switch(self, event: AstrMessageEvent):
        """/ctx_switch <会话前8位> 切到指定会话（仅管理员）。"""
        umo = event.unified_msg_origin
        parts = (event.message_str or "").split()
        key = parts[1].lower() if len(parts) > 1 else ""
        if not key:
            yield event.plain_result("用法：/ctx_switch <会话前8位>（先发 /ctx_sessions 看列表）")
            return
        cm = self.context.conversation_manager
        try:
            convs = await cm.get_conversations(umo)
        except Exception as exc:
            yield event.plain_result(f"读取会话失败：{exc}")
            return
        match = None
        for conv in convs or []:
            cid = str(getattr(conv, "cid", ""))
            if cid.lower().startswith(key):
                match = cid
                break
        if not match:
            yield event.plain_result(f"没找到以 {key} 开头的会话，发 /ctx_sessions 看看有哪些。")
            return
        await cm.switch_conversation(umo, match)
        self._auto_cache.pop(match, None)
        yield event.plain_result(f"已切换到会话 {match[:8]}。")

    @filter.command("ctx_auto")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ctx_auto(self, event: AstrMessageEvent):
        """/ctx_auto [on|off] 查看或切换自动压缩（仅管理员）。"""
        parts = (event.message_str or "").split()
        mode = parts[1].lower() if len(parts) > 1 else "状态"
        if mode in {"on", "开", "开启", "打开"}:
            self._set_auto_enabled(True)
        elif mode in {"off", "关", "关闭", "关掉"}:
            self._set_auto_enabled(False)
        on = self._auto_enabled()
        yield event.plain_result(
            f"自动压缩：{'已开启' if on else '已关闭'}\n"
            f"触发阈值：{self.auto_threshold:.0%}（约 {int(self.token_limit * self.auto_threshold)} tokens）\n"
            f"保留轮数：{self.keep_rounds} 轮\n"
            f"估算窗口：{self.token_limit} tokens\n"
            f"增强摘要：{'开启' if self.enhanced_summary else '关闭'}\n"
            "说明：只在单次请求内把老对话换成摘要，不改数据库；真要瘦身请用 /ctx_compress。"
        )

    @filter.command("ctx_diary")
    async def cmd_ctx_diary(self, event: AstrMessageEvent):
        """/ctx_diary 查看当前会话的日记本（所有人可用）。"""
        try:
            umo = event.unified_msg_origin
            cm = self.context.conversation_manager
            cid = str(await cm.get_curr_conversation_id(umo) or umo)
            yield event.plain_result(self._diary_text(umo, cid))
        except Exception as exc:
            logger.warning(f"[token_panel] 读取日记本失败: {exc}")
            yield event.plain_result("日记本读取失败，稍后再试。")

    @filter.command("ctx_diary_clear")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ctx_diary_clear(self, event: AstrMessageEvent):
        """/ctx_diary_clear 清空当前会话的日记本（二次确认，仅管理员）。"""
        umo = event.unified_msg_origin
        now = time.time()
        deadline = self._pending_diary_clear.get(umo, 0)
        text = (event.message_str or "").strip()

        if deadline > now and "确认" in text:
            self._pending_diary_clear.pop(umo, None)
            try:
                cm = self.context.conversation_manager
                cid = str(await cm.get_curr_conversation_id(umo) or umo)
                removed = self._diary_clear(umo, cid)
                if removed:
                    yield event.plain_result(f"已清空当前会话的日记本，共删除 {removed} 篇。")
                else:
                    yield event.plain_result("当前会话的日记本本来就是空的。")
            except Exception as exc:
                logger.warning(f"[token_panel] 清空日记本失败: {exc}")
                yield event.plain_result("清空失败，稍后再试。")
            return

        self._pending_diary_clear[umo] = now + 60
        yield event.plain_result(
            "这会删掉当前会话的全部日记，且不可恢复（不影响聊天上下文）。\n"
            "60 秒内再发一次 /ctx_diary_clear 确认 即可执行。"
        )

    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def auto_compress(self, event: AstrMessageEvent, req):
        try:
            if not self._auto_enabled():
                return
            contexts = getattr(req, "contexts", None)
            if not isinstance(contexts, list) or len(contexts) < 8:
                return
            est = sum(est_tokens(msg_text(m)) + 4 for m in contexts if isinstance(m, dict))
            if est < self.token_limit * self.auto_threshold:
                return
            umo = event.unified_msg_origin
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo) or umo
            head, tail = split_history(contexts, self.keep_rounds)
            if not head:
                return
            cached = self._auto_cache.get(cid)
            if cached and cached[0] == len(head):
                summary = cached[1]
            else:
                summary = await self._summarize(umo, head)
                if not summary:
                    return
                self._auto_cache[cid] = (len(head), summary)
                # 只在真正生成新摘要时记一条日志，避免每次请求都刷屏
                try:
                    new_contexts = [self._summary_message(summary, "本次请求内自动压缩")] + tail
                    self._carry_log_add(
                        {
                            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "kind": "auto",
                            "type": "请求内自动压缩",
                            "cid": str(cid),
                            "umo": umo,
                            "label": self._log_label(umo, str(cid)),
                            "before": est,
                            "after": est_history_tokens(new_contexts),
                            "keep": self.keep_rounds,
                            "rounds_before": rounds_of(contexts),
                            "rounds_after": rounds_of(new_contexts),
                            "backup": "",
                            "backup_ok": None,
                            "backup_error": "",
                            "new_cid": "",
                            "request_only": True,
                        }
                    )
                except Exception as exc:
                    self._dbg(f"写自动压缩日志失败: {exc}")
            req.contexts = [self._summary_message(summary, "本次请求内自动压缩")] + tail
            logger.info(
                f"[token_panel] 请求内自动压缩 cid={str(cid)[:8]} {est} tokens -> "
                f"{est_history_tokens(req.contexts)} tokens"
            )
        except Exception as exc:  # 自动压缩失败绝不影响正常回复
            logger.warning(f"[token_panel] 自动压缩跳过: {exc}")

    # ------------------------------------------------------------------ #
    # 网页聊天里的手动备注：`#标题 名字`
    # ------------------------------------------------------------------ #
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_web_note(self, event: AstrMessageEvent):
        """网页聊天发送 `#标题 内容` 给当前会话设备注（只对 webchat 生效）。"""
        try:
            text = (event.message_str or "").strip()
            if not text.startswith(WEB_NOTE_PREFIX):
                return
            platform, _mtype, _session = _split_umo(event.unified_msg_origin)
            if not _is_web(platform):
                return

            title = text[len(WEB_NOTE_PREFIX):].strip()
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(event.unified_msg_origin)
            if not cid and title:
                # 新开的网页会话还没有对话记录：先建一个空对话，再挂备注，避免“必须先聊一句”
                try:
                    cid = await cm.new_conversation(
                        event.unified_msg_origin,
                        platform_id=platform,
                        content=[],
                        title=title[:40],
                    )
                except Exception as exc:
                    self._dbg(f"新建网页会话失败: {exc}")
                    cid = None
            if not cid:
                event.stop_event()
                yield event.plain_result("当前网页会话还没有标题。发「#标题 名字」即可命名。")
                return

            if not title:
                self._clear_title(cid)
                event.stop_event()
                yield event.plain_result("已清除当前网页会话的标题。")
                return

            self._set_title(cid, title)
            logger.info(f"[token_panel] 网页会话备注 {cid[:8]} -> {title[:40]}")
            event.stop_event()
            yield event.plain_result(
                f"已把当前网页会话命名为「{title[:40]}」，面板里会显示这个名字。"
            )
        except Exception as exc:
            self._dbg(f"设置网页备注失败: {exc}")

    async def _backup_before_clear(self, umo: str, cid: str) -> str:
        """清空前把旧历史备份到插件数据目录，返回文件名。"""
        try:
            cm = self.context.conversation_manager
            conv = await cm.get_conversation(umo, cid)
            history = getattr(conv, "history", "") or "[]"
            folder = self.backup_dir / "cleared"
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{cid[:8]}-{time.strftime('%Y%m%d-%H%M%S')}.json"
            (folder / name).write_text(history, encoding="utf-8")
            return name
        except Exception as exc:
            self._dbg(f"清空备份失败: {exc}")
            return ""

    # ------------------------------------------------------------------ #
    # 自动播报
    # ------------------------------------------------------------------ #
    @filter.after_message_sent(priority=100000)
    async def on_after_sent(self, event: AstrMessageEvent):
        """每条消息发出后，视情况提醒一次（绝不打断正常对话）。"""
        if not self._settings.get("auto_broadcast"):
            return
        try:
            umo = event.unified_msg_origin
            platform, _mt, _session = _split_umo(umo)
            if platform == "webchat":
                return
            info = await self._session_info(umo)
            percent = info.get("percent")
            if percent is None:
                return

            warn = int(self._settings.get("warn_percent", 80))
            prev = self._last_percent.get(umo)
            self._last_percent[umo] = float(percent)

            # 只在「刚跨过预警线」或「已经超过预警线且冷却已过」时提醒一次，
            # 正常聊天不会收到任何插件提示。
            crossed = prev is None or prev < warn <= percent
            last_ts = self._last_notice.get(umo, 0.0)
            cooled = (time.time() - last_ts) >= NOTICE_COOLDOWN
            if not (percent >= warn and crossed and cooled):
                return

            self._last_notice[umo] = time.time()
            label, level = _status_of(float(percent), warn)
            icon = "🔴" if level == "high" else "⚠️"
            hint = "建议 /ctx_compress 一下，或清空上下文" if level == "warn" else "建议尽快处理，避免超窗"
            text = (
                f"{icon} 上下文用量提醒：{percent:.1f}%（{label}）\n"
                f"{_render_bar(float(percent))}\n"
                f"{hint}。发送 /token_ctx 查看详情。"
            )
            await self.context.send_message(
                umo, MessageChain([Plain(text)])
            )
        except Exception as exc:
            # 播报失败绝不能影响聊天，只记调试日志
            self._dbg(f"自动播报失败: {exc}")

    # ------------------------------------------------------------------ #
    # 面板 Web API（注册在扩展功能页面下）
    # ------------------------------------------------------------------ #
    async def initialize(self):
        """插件加载后注册面板接口，并自动禁用会造成指令冲突的旧插件。"""
        try:
            register = self.context.register_web_api
            register(f"{PAGE_PREFIX}/overview", self.api_overview, ["GET"], "Token 面板总览")
            register(f"{PAGE_PREFIX}/settings", self.api_settings, ["POST"], "Token 面板设置")
            register(f"{PAGE_PREFIX}/clear", self.api_clear, ["POST"], "Token 面板清空会话")
            register(f"{PAGE_PREFIX}/health", self.api_health, ["GET"], "Token 面板健康检查")
            register(f"{PAGE_PREFIX}/clear-idle", self.api_clear_idle, ["POST"], "清理闲置网页会话")
            register(f"{PAGE_PREFIX}/clear-empty", self.api_clear_empty, ["POST"], "删除空会话")
            register(f"{PAGE_PREFIX}/stats", self.api_stats, ["POST"], "会话上下文统计")
            register(f"{PAGE_PREFIX}/compress", self.api_compress, ["POST"], "压缩会话上下文")
            register(f"{PAGE_PREFIX}/continue", self.api_continue, ["POST"], "生成承接摘要并换会话")
            register(f"{PAGE_PREFIX}/carry", self.api_carry, ["GET", "POST"], "压缩承接设置")
            register(f"{PAGE_PREFIX}/diary-overview", self.api_diary_overview, ["GET"], "会话日记本总览")
            register(f"{PAGE_PREFIX}/diary-settings", self.api_diary_settings, ["GET", "POST"], "会话日记本设置")
            register(f"{PAGE_PREFIX}/diary-clear", self.api_diary_clear, ["POST"], "清空会话日记本")
            register(f"{PAGE_PREFIX}/compress-log", self.api_compress_log, ["GET"], "压缩日志")
            register(f"{PAGE_PREFIX}/backup-preview", self.api_backup_preview, ["POST"], "备份预览")
            register(f"{PAGE_PREFIX}/dy-overview", self.api_dy_overview, ["GET"], "抖音浏览总览")
            register(f"{PAGE_PREFIX}/dy-settings", self.api_dy_settings, ["POST"], "抖音浏览设置")
            register(f"{PAGE_PREFIX}/dy-run", self.api_dy_run, ["POST"], "抖音浏览立即执行")
            register(f"{PAGE_PREFIX}/dy-log", self.api_dy_log, ["GET"], "抖音浏览日志")
            register(f"{PAGE_PREFIX}/net-overview", self.api_net_overview, ["POST"], "联网搜索监控")
            register(f"{PAGE_PREFIX}/net-clear", self.api_net_clear, ["POST"], "清空搜索日志")
            # ---- 精细化主动消息（v1.4.1） ----
            register(f"{PAGE_PREFIX}/proactive-overview", self.api_proactive_overview, ["GET"], "主动消息总览")
            register(f"{PAGE_PREFIX}/proactive-settings", self.api_proactive_settings, ["POST"], "主动消息全局配置")
            register(f"{PAGE_PREFIX}/proactive-session", self.api_proactive_session, ["POST"], "主动消息会话配置")
            register(f"{PAGE_PREFIX}/proactive-log", self.api_proactive_log, ["POST"], "主动消息日志")
            register(f"{PAGE_PREFIX}/proactive-clear", self.api_proactive_clear, ["POST"], "清空主动消息日志")
            register(f"{PAGE_PREFIX}/proactive-reset", self.api_proactive_reset, ["POST"], "重置会话冷却惩罚")
            register(f"{PAGE_PREFIX}/limit-override", self.api_limit_override, ["POST"], "手动兜底上限")
            # ---- 抖音风控调速（v1.5.0） ----
            register(f"{PAGE_PREFIX}/risk-status", self.api_risk_status, ["GET"], "抖音风控实时状态")
            register(f"{PAGE_PREFIX}/risk-settings", self.api_risk_settings, ["POST"], "抖音风控严格系数")
            # ---- 语音合成（v1.7.1） ----
            register(f"{PAGE_PREFIX}/tts-overview", self.api_tts_overview, ["GET"], "语音合成总览")
            register(f"{PAGE_PREFIX}/tts-settings", self.api_tts_settings, ["POST"], "语音合成设置")
            # ---- 详细推理完成通知（v1.7.0） ----
            register(f"{PAGE_PREFIX}/notify-overview", self.api_notify_overview, ["GET"], "推理通知总览")
            register(f"{PAGE_PREFIX}/notify-settings", self.api_notify_settings, ["POST"], "推理通知设置")
            register(f"{PAGE_PREFIX}/notify-log", self.api_notify_log, ["POST"], "推理通知日志")
            register(f"{PAGE_PREFIX}/notify-clear", self.api_notify_clear, ["POST"], "清空推理通知日志")
            logger.info("[token_panel] 面板接口注册完成")
        except Exception as exc:
            logger.error(f"[token_panel] 面板接口注册失败: {exc}")
        # 不能在这里 await 插件管理锁：重载本插件时锁还在自己手里，会死锁。
        # 交给后台任务，等加载/重载释放锁后再处理旧插件。
        try:
            asyncio.create_task(self._disable_legacy_plugins())
        except Exception as exc:
            logger.warning(f"[token_panel] 启动旧插件禁用任务失败: {exc}")
            asyncio.create_task(self._dy_loop())
        # 主动消息调度：登记表在 proactive 模块级，热重载时能取消上一个实例的循环。
        # （原来用实例属性 _proactive_tasks，新实例拿到空列表，旧循环永远不停，
        #   重载几次就有几个定时器同时发消息。）
        try:
            start_loops(self.proactive)
        except Exception as exc:
            logger.warning(f"[token_panel] 启动主动消息调度失败: {exc}")

    # ================================================================== #
    # 语音合成（v1.7.1，增量模块）
    # 与通知/主动消息/日记/抖音/风控完全隔离；只读写 settings.json 的 tts 段。
    # 全部合成走 AstrBot 原生 TTSProvider（context.get_using_tts_provider_async），
    # 不自行封装任何 TTS 接口。自然语言触发优先于自动开关，且只对当前轮生效。
    # ================================================================== #
    TTS_TRIGGER_WORDS = ("发语音", "语音说", "用语音回答")

    def _tts_save(self, patch: dict[str, Any]) -> dict[str, Any]:
        """写回 tts 配置并落盘（失败只记日志，不影响指令返回）。"""
        cfg = self.tts.update(patch)
        try:
            self._settings["tts"] = cfg
            self._save_settings()
        except Exception as exc:
            logger.warning(f"[token_panel] 保存语音配置失败: {exc}")
        return cfg

    @filter.on_llm_request()
    async def tts_capture_trigger(self, event: AstrMessageEvent, req: Any):
        """识别自然语言触发词：本轮强制语音，并把触发词从 prompt 里剥掉。"""
        try:
            raw = event.message_str or ""
            if not has_trigger(raw):
                return
            event.set_extra("_tts_force_voice", True)
            cleaned = strip_trigger(raw)
            # 去掉触发词后的提问：空了就补一句，避免模型收到空问题
            if len(cleaned) < 2:
                cleaned = "请直接回答我上面这句话。"
            try:
                req.prompt = cleaned
            except Exception:
                pass
            # 历史里的最后一条也可能是这句原文，顺手清理，避免污染上下文
            try:
                contexts = getattr(req, "contexts", None)
                if contexts:
                    last = contexts[-1]
                    if isinstance(last, dict):
                        content = last.get("content")
                        if isinstance(content, str) and has_trigger(content):
                            last["content"] = strip_trigger(content) or cleaned
                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    text = str(part.get("text") or "")
                                    if has_trigger(text):
                                        part["text"] = strip_trigger(text) or cleaned
            except Exception as exc:
                logger.warning(f"[token_panel] 清理触发词失败: {exc}")
            self._dbg("命中语音触发词，本轮强制附带语音")
        except Exception as exc:
            logger.warning(f"[token_panel] 语音触发识别失败: {exc}")

    @filter.on_decorating_result()
    async def tts_attach_voice(self, event: AstrMessageEvent):
        """发送前给本轮回复追加语音（指令反馈保持纯文字）。"""
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None) if result else None
            if not chain:
                return
            if self.tts.is_command(event):
                return
            # 已经带语音（别的插件或原生 TTS 处理过）就不重复追加
            try:
                from astrbot.core.message.components import Record as _Record

                if any(isinstance(comp, _Record) for comp in chain):
                    return
            except Exception:
                _Record = None

            forced = bool(event.get_extra("_tts_force_voice"))
            if not self.tts.should_voice(event, forced):
                return
            cfg = self.tts.cfg()
            texts = [
                comp.text
                for comp in chain
                if isinstance(getattr(comp, "text", None), str) and comp.text.strip()
            ]
            if not texts:
                return
            spoken = clean_for_tts("".join(texts))
            if len(spoken) < 2:
                return
            paths = await self.tts.synthesize(event, spoken)
            if not paths:
                return

            from astrbot.core.message.components import Record

            if not cfg.get("send_both"):
                # 只发语音：先摘掉纯文本，再追加语音，避免文字里带指令正文
                for comp in list(chain):
                    if isinstance(getattr(comp, "text", None), str):
                        chain.remove(comp)
            for path in paths:
                chain.append(Record(file=path, url=path))
            self._dbg(f"已附加语音 {len(paths)} 段（{'强制' if forced else '自动'}）")
        except Exception as exc:
            logger.warning(f"[token_panel] 追加语音失败: {exc}")

    # ---------------------------------------------------------------- #
    # 指令：/tts_toggle /tts_private /tts_group /tts_voice /tts_speed
    # ---------------------------------------------------------------- #
    @filter.command("tts_toggle")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_tts_toggle(self, event: AstrMessageEvent):
        """/tts_toggle 开关全局语音（仅管理员）。"""
        cfg = self.tts.cfg()
        new_state = not bool(cfg.get("enabled"))
        self._tts_save({"enabled": new_state})
        yield event.plain_result("语音合成：" + ("已开启" if new_state else "已关闭"))

    @filter.command("tts_private")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_tts_private(self, event: AstrMessageEvent):
        """/tts_private 切换私聊自动语音（仅管理员）。"""
        cfg = self.tts.cfg()
        new_state = not bool(cfg.get("private_enabled"))
        self._tts_save({"private_enabled": new_state})
        yield event.plain_result("私聊自动语音：" + ("开" if new_state else "关"))

    @filter.command("tts_group")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_tts_group(self, event: AstrMessageEvent):
        """/tts_group 切换群聊自动语音（仅管理员）。"""
        cfg = self.tts.cfg()
        new_state = not bool(cfg.get("group_enabled"))
        self._tts_save({"group_enabled": new_state})
        yield event.plain_result("群聊自动语音：" + ("开" if new_state else "关"))

    @filter.command("tts_voice")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_tts_voice(self, event: AstrMessageEvent):
        """/tts_voice <音色ID> 修改音色（仅管理员），留空则恢复框架默认。"""
        arg = (event.message_str or "").strip()
        for head in ("/tts_voice", "tts_voice"):
            if arg.lower().startswith(head):
                arg = arg[len(head):]
                break
        voice = arg.strip()
        self._tts_save({"voice_id": voice})
        yield event.plain_result(
            "音色已设置为：" + (voice if voice else "框架当前默认音色")
        )

    @filter.command("tts_speed")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_tts_speed(self, event: AstrMessageEvent):
        """/tts_speed <0.5~2.0> 调整语速（仅管理员）。"""
        arg = (event.message_str or "").strip()
        for head in ("/tts_speed", "tts_speed"):
            if arg.lower().startswith(head):
                arg = arg[len(head):]
                break
        try:
            speed = float(arg.strip())
        except (TypeError, ValueError):
            yield event.plain_result(
                "用法：/tts_speed 1.2（范围 0.5 ~ 2.0）" + NL
                + "当前语速：" + str(self.tts.cfg().get("speed"))
            )
            return
        if speed < 0.5 or speed > 2.0:
            yield event.plain_result("语速范围是 0.5 ~ 2.0，请重新输入。")
            return
        self._tts_save({"speed": speed})
        yield event.plain_result("语速已设置为：" + str(speed))

    # ---------------------------------------------------------------- #
    # 面板接口
    # ---------------------------------------------------------------- #
    async def _tts_payload(self) -> dict:
        """语音状态载荷：概览与保存接口共用，保证两边的统计/提供商信息一致。"""
        cfg = self.tts.cfg()
        provider_name = ""
        provider_ok = False
        try:
            provider = await self.context.get_using_tts_provider_async()
            if provider is not None:
                provider_ok = True
                pcfg = getattr(provider, "provider_config", {}) or {}
                provider_name = str(
                    pcfg.get("id") or pcfg.get("model") or provider.__class__.__name__
                )
        except Exception as exc:
            self._dbg(f"读取 TTS 提供商失败: {exc}")
        return {
            "settings": cfg,
            "stats": self.tts.stats(),
            "provider_available": provider_ok,
            "provider_name": provider_name,
            "trigger_words": list(self.TTS_TRIGGER_WORDS),
        }

    async def api_tts_overview(self):
        """面板：语音合成当前状态。"""
        return {"status": "ok", "data": await self._tts_payload()}

    async def api_tts_settings(self):
        """面板：保存语音设置。"""
        body = await self._json_body()
        patch = body.get("settings") if isinstance(body.get("settings"), dict) else body
        self._tts_save(patch or {})
        return {"status": "ok", "data": await self._tts_payload()}

    # ================================================================== #
    # 详细推理完成通知（v1.7.0，增量模块）
    # 与 Token 统计 / 压缩承接 / 日记 / 主动消息 / 抖音 / 风控 完全隔离：
    # 配置在 settings.json 的 notify 段，日志在 notify_log.json，互不占用字段。
    # ================================================================== #
    @filter.on_llm_request()
    async def notify_turn_begin(self, event: AstrMessageEvent, req: Any):
        """一轮请求开始：只记时间戳，不发任何消息。"""
        try:
            self.notify.begin(event)
        except Exception as exc:
            logger.warning(f"[token_panel] 推理通知开始失败: {exc}")

    @filter.on_llm_response()
    async def notify_turn_note(self, event: AstrMessageEvent, resp: Any):
        """拿到模型响应：暂存用量/错误信息，仍然不发消息。"""
        try:
            await self.notify.note_response(event, resp)
        except Exception as exc:
            logger.warning(f"[token_panel] 推理通知记录失败: {exc}")

    @filter.on_agent_done()
    async def notify_turn_done(self, event: AstrMessageEvent, run_context: Any, response: Any):
        """整轮推理彻底结束：这时才发唯一一条汇总通知。"""
        try:
            await self.notify.finish(event, run_context, response)
        except Exception as exc:
            logger.warning(f"[token_panel] 推理通知发送失败: {exc}")

    def _notify_save(self, patch: dict[str, Any]) -> dict[str, Any]:
        """写回 notify 配置并落盘（失败只记日志，不影响指令返回）。"""
        cfg = self.notify.update(patch)
        try:
            self._settings["notify"] = cfg
            self._save_settings()
        except Exception as exc:
            logger.warning(f"[token_panel] 保存推理通知配置失败: {exc}")
        return cfg

    @filter.command("notify_on")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_notify_on(self, event: AstrMessageEvent):
        """/notify_on 开启推理完成通知（仅管理员）。"""
        self._notify_save({"enabled": True})
        yield event.plain_result("推理完成通知：已开启（投递模式 " + str(self.notify.cfg().get("mode")) + "）")

    @filter.command("notify_off")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_notify_off(self, event: AstrMessageEvent):
        """/notify_off 关闭推理完成通知（仅管理员）。"""
        self._notify_save({"enabled": False})
        yield event.plain_result("推理完成通知：已关闭")

    @filter.command("notify_status")
    async def cmd_notify_status(self, event: AstrMessageEvent):
        """/notify_status 查看通知配置与本会话状态（所有人）。"""
        cfg = self.notify.cfg()
        mode_text = {"session": "仅原会话", "admin": "仅管理员私聊", "both": "双推送"}.get(
            str(cfg.get("mode")), "仅原会话"
        )
        on_text = "开启" if cfg.get("enabled") else "关闭"
        pending = 0
        try:
            pending = len(getattr(self.notify, "_turns", {}) or {})
        except Exception:
            pending = 0
        last = ""
        try:
            rows = self.notify.log_rows(1)
            if rows:
                last = str(rows[0].get("time") or "")
        except Exception:
            last = ""
        text = (
            "【推理完成通知】" + on_text + NL
            + "投递模式：" + mode_text + NL
            + "群聊投递：" + ("允许" if cfg.get("group_enabled") else "不投递") + NL
            + "内容开关：Token " + ("开" if cfg.get("show_tokens") else "关")
            + " / 耗时 " + ("开" if cfg.get("show_elapsed") else "关")
            + " / 记忆 " + ("开" if cfg.get("show_memory") else "关") + NL
            + "语音播报：" + ("开" if cfg.get("tts") else "关") + NL
            + "本轮进行中：" + str(pending) + " 个会话待汇总" + NL
            + "最近一条：" + (last or "暂无")
        )
        yield event.plain_result(text)

    @filter.command("notify_toggle")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_notify_toggle(self, event: AstrMessageEvent):
        """/notify_toggle 一键开关整套推理通知（仅管理员）。"""
        cfg = self.notify.cfg()
        new_state = not bool(cfg.get("enabled"))
        self._notify_save({"enabled": new_state})
        yield event.plain_result(f"推理通知已{'开启' if new_state else '关闭'}。")

    @filter.command("notify_mode")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_notify_mode(self, event: AstrMessageEvent):
        """/notify_mode session|admin|both 切换投递模式（仅管理员）。"""
        raw = str(getattr(event, "message_str", "") or "").strip()
        arg = raw.split(maxsplit=1)[1].strip().lower() if len(raw.split(maxsplit=1)) > 1 else ""
        alias = {"会话": "session", "原会话": "session", "管理员": "admin", "双": "both",
                 "双推送": "both", "both": "both", "admin": "admin", "session": "session"}
        mode = alias.get(arg, "")
        if not mode:
            cfg = self.notify.cfg()
            yield event.plain_result(
                "用法：/notify_mode session|admin|both\n当前：" + str(cfg.get("mode") or "session")
            )
            return
        self._notify_save({"mode": mode})
        yield event.plain_result(f"推理通知投递模式已改为：{mode}")

    @filter.command("notify_text")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_notify_text(self, event: AstrMessageEvent):
        """/notify_text <文案> 自定义成功通知前缀（仅管理员）。"""
        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""
        if not text:
            yield event.plain_result("用法：/notify_text ✅ 推理完成（自定义成功前缀）")
            return
        self._notify_save({"success_prefix": text[:60]})
        yield event.plain_result(f"成功通知前缀已改为：{text[:60]}")

    @filter.command("notify_errtext")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_notify_errtext(self, event: AstrMessageEvent):
        """/notify_errtext <文案> 自定义失败通知前缀（仅管理员）。"""
        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""
        if not text:
            yield event.plain_result("用法：/notify_errtext ❌ 推理失败（自定义失败前缀）")
            return
        self._notify_save({"fail_prefix": text[:60]})
        yield event.plain_result(f"失败通知前缀已改为：{text[:60]}")

    async def api_notify_overview(self):
        try:
            cfg = self.notify.cfg()
            tts_ok = False
            try:
                tts_ok = bool(self.context.get_all_tts_providers())
            except Exception:
                tts_ok = False
            admins = []
            try:
                admins = [str(a) for a in (self.context.get_config().get("admins_id") or [])]
            except Exception:
                admins = []
            return {
                "status": "ok",
                "data": {
                    "settings": cfg,
                    "rows": self.notify.log_rows(50),
                    "tts_available": tts_ok,
                    "admins": admins,
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 读取推理通知总览失败: {exc}")
            return {"status": "error", "message": "读取推理通知数据失败"}

    async def api_notify_settings(self):
        try:
            body = await self._json_body()
            patch = body.get("settings") if isinstance(body.get("settings"), dict) else body
            cfg = self._notify_save(patch or {})
            return {"status": "ok", "data": {"settings": cfg}}
        except Exception as exc:
            logger.warning(f"[token_panel] 保存推理通知设置失败: {exc}")
            return {"status": "error", "message": "保存设置失败"}

    async def api_notify_log(self):
        try:
            body = await self._json_body()
            limit = int(body.get("limit") or 50)
            return {"status": "ok", "data": {"rows": self.notify.log_rows(limit)}}
        except Exception as exc:
            logger.warning(f"[token_panel] 读取推理通知日志失败: {exc}")
            return {"status": "error", "message": "读取日志失败"}

    async def api_notify_clear(self):
        try:
            self.notify.clear_log()
            return {"status": "ok", "data": {"rows": []}}
        except Exception as exc:
            logger.warning(f"[token_panel] 清空推理通知日志失败: {exc}")
            return {"status": "error", "message": "清空日志失败"}

    async def terminate(self) -> None:
        """卸载/重载时停掉调度循环（模块级登记表，能取消到上个实例的任务）。"""
        try:
            stop_all_loops()
        except Exception as exc:
            logger.warning(f"[token_panel] 停止主动消息调度失败: {exc}")

    # ------------------------------------------------------------------ #
    # 防冲突：自动禁用「上下文统计」「上下文压缩与承接」旧插件
    # ------------------------------------------------------------------ #
    LEGACY_PLUGIN_NAMES = {"ctx_stats", "astrbot_plugin_ctx_carry"}
    LEGACY_DISPLAY_NAMES = {"上下文统计", "上下文压缩与承接", "上下文压缩和承接"}

    async def _disable_legacy_plugins(self) -> None:
        """只禁用、不删文件；禁用记录进 shared_preferences，可手动重新启用。"""
        try:
            manager = getattr(self.context, "_star_manager", None)
            if manager is None:
                self._dbg("未找到 star manager，跳过旧插件禁用")
                return
            targets: list[str] = []
            for meta in self.context.get_all_stars():
                name = str(getattr(meta, "name", "") or "")
                display = str(getattr(meta, "display_name", "") or "")
                if not name or name == PLUGIN_NAME:
                    continue
                if name in self.LEGACY_PLUGIN_NAMES or display in self.LEGACY_DISPLAY_NAMES:
                    targets.append(name)
            for name in targets:
                meta = self.context.get_registered_star(name)
                if meta is None or not getattr(meta, "activated", False):
                    continue
                try:
                    await manager.turn_off_plugin(name)
                    logger.info(
                        f"[token_panel] 已自动禁用同名旧插件，避免指令重复注册、冲突报错："
                        f"{name}（文件保留，需要时可在插件管理里手动重新启用）"
                    )
                except Exception as exc:
                    logger.warning(f"[token_panel] 禁用旧插件 {name} 失败: {exc}")
        except Exception as exc:
            logger.warning(f"[token_panel] 检查旧插件冲突失败: {exc}")

    async def _json_body(self) -> dict[str, Any]:
        """读取面板请求的 JSON body（AstrBot 官方 PluginRequest 代理）。"""
        try:
            from astrbot.api.web import request  # 插件 Web 处理函数内可用

            payload = await request.json(default={})
            if isinstance(payload, dict):
                return payload
        except Exception as exc:
            self._dbg(f"读取请求体失败: {exc}")
        return {}

    def _query_flag(self, name: str) -> bool:
        """读取查询参数里的布尔开关（force=1 等）；取不到就当 False。"""
        try:
            from astrbot.api.web import request

            raw = request.query.get(name)
            return str(raw or "").strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            return False

    async def api_health(self) -> dict[str, Any]:
        """健康检查：只读，不依赖数据库。"""
        return {
            "status": "ok",
            "data": {
                "plugin": PLUGIN_NAME,
                "version": PLUGIN_VERSION,
                "error": None,
            },
        }

    async def api_overview(self) -> dict[str, Any]:
        """面板总览：会话列表 + 设置 + 预警线。``?force=1`` 会跳过上限缓存。"""
        try:
            force = self._query_flag("force")
            rows = await self._fetch_rows()
            admins = self._admins()
            sessions: list[dict[str, Any]] = []
            limit_hints: dict[str, dict[str, Any]] = {}
            for row in rows:
                try:
                    umo = str(getattr(row, "user_id", "") or "")
                    if not umo:
                        continue
                    platform, mtype, session_id = _split_umo(umo)
                    kind = _kind_of(mtype, platform)
                    title = str(getattr(row, "title", "") or "")

                    # 权限隔离：QQ 私聊只展示管理员自己的；网页会话本身就在管理员后台里，全部展示
                    if kind == "friend" and not _is_web(platform) and admins:
                        haystack = session_id or umo
                        if not any(admin in haystack for admin in admins):
                            continue

                    used = int(getattr(row, "token_usage", 0) or 0)
                    model, limit, source = await self._model_limit(umo, force=force)
                    percent = round(used / limit * 100.0, 1) if limit else None
                    if model and model not in limit_hints:
                        limit_hints[model] = {"limit": limit, "source": source}
                    sessions.append(
                        {
                            "cid": str(getattr(row, "conversation_id", "") or ""),
                            "umo": umo,
                            "platform": platform,
                            "kind": kind,
                            "session": session_id,
                            "label": _label_of(kind, platform, session_id, title),
                            "title": title,
                            "model": model,
                            "limit": limit,
                            "limit_source": source,
                            "used": used,
                            "remaining": (max(0, limit - used) if limit else None),
                            "percent": percent,
                            "updated_at": _load_local_time(getattr(row, "updated_at", None)),
                            "web": _is_web(platform),
                            "msg_count": 0,
                            "idle_days": _idle_days(getattr(row, "updated_at", None)),
                            "parent_cid": "",
                            "parent_label": "",
                        }
                    )
                except Exception as exc:  # 单条坏数据不影响整体
                    self._dbg(f"跳过异常会话: {exc}")
                    continue

            # ---- 承接派生关系：只读 session_meta.json，父会话数据不动 ----
            for item in sessions:
                try:
                    entry = self._meta_entry(item["cid"])
                    parent = str(entry.get("parent_cid") or "")
                    if parent:
                        item["parent_cid"] = parent
                        item["parent_label"] = str(entry.get("parent_label") or "") or parent[:8]
                except Exception as exc:
                    self._dbg(f"读取承接关系失败: {exc}")

            # ---- 网页会话：手动备注优先，没有备注才生成 AI 摘要 ----
            web_items = [item for item in sessions if item.get("web")]
            pending: list[Any] = []
            for item in web_items:
                try:
                    extra = await self._web_extra(item["umo"], item["cid"])
                    item["msg_count"] = int(extra.get("msg_count") or 0)
                    item["snippet"] = str(extra.get("snippet") or "")
                except Exception as exc:
                    self._dbg(f"读取网页会话附加信息失败: {exc}")
                entry = self._meta_entry(item["cid"])
                item["note"] = str(entry.get("title") or "")
                item["short_id"] = _web_short(item["session"])
                if item["note"]:
                    item["display"] = item["note"]
                    item["display_kind"] = "title"
                    continue
                item["display_kind"] = "summary"
                item["display"] = "摘要获取中…"
                msg_count = int(item.get("msg_count") or 0)
                if not self._summary_usable(entry, msg_count) and item.get("snippet"):
                    pending.append(
                        asyncio.create_task(
                            self._make_summary(item["umo"], item["cid"], msg_count, item["snippet"])
                        )
                    )
            if pending:
                try:
                    await asyncio.wait(pending, timeout=SUMMARY_WAIT)
                except Exception as exc:
                    self._dbg(f"摘要任务等待失败: {exc}")
            for item in web_items:
                if not item.get("note"):
                    entry = self._meta_entry(item["cid"])
                    if entry.get("summary"):
                        item["display"] = str(entry["summary"])
                    elif not int(item.get("msg_count") or 0):
                        item["display"] = "尚无对话"
                    else:
                        item["display"] = "摘要获取失败"
                item.pop("snippet", None)
            idle_count = sum(
                1 for item in web_items if (item.get("idle_days") or 0) >= IDLE_DAYS
            )
            empty_count = sum(
                1
                for item in sessions
                if not int(item.get("used") or 0) and not int(item.get("msg_count") or 0)
            )

            sessions.sort(key=lambda item: (item["percent"] is None, -(item["percent"] or 0)))
            return {
                "status": "ok",
                "data": {
                    "settings": dict(self._settings),
                    "high_percent": HIGH_PERCENT,
                    "sessions": sessions,
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "idle_days_limit": IDLE_DAYS,
                    "idle_count": idle_count,
                    "empty_count": empty_count,
                    "carry": dict(self.carry),
                    "limits": {k: list(v) for k, v in CARRY_LIMITS.items()},
                    "backup_limit": CARRY_BACKUP_LIMIT,
                    "log_limit": CARRY_LOG_LIMIT,
                    "limit_override": _clamp_int(
                        self._settings.get("limit_override"), 0, 10_000_000, 0
                    ),
                    "limit_fallback": self._fallback_limit(),
                    "limit_hints": limit_hints,
                    "error": None,
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 面板读取失败: {exc}")
            return {
                "status": "error",
                "message": "读取Token数据失败",
                "data": {"settings": dict(self._settings), "sessions": [], "error": str(exc)},
            }

    async def api_settings(self) -> dict[str, Any]:
        """保存面板设置（部分更新）。"""
        try:
            payload = await self._json_body()
            if "style" in payload:
                self._settings["style"] = (
                    "deepseek" if str(payload["style"]) == "deepseek" else "claude"
                )
            if "theme" in payload:
                self._settings["theme"] = "ink" if str(payload["theme"]) == "ink" else "ivory"
            if "zoom" in payload:
                self._settings["zoom"] = _clamp_int(payload["zoom"], 80, 140, 100)
            if "tooltip" in payload:
                self._settings["tooltip"] = bool(payload["tooltip"])
            if "warn_percent" in payload:
                self._settings["warn_percent"] = _clamp_int(payload["warn_percent"], 1, 99, 80)
            if "auto_broadcast" in payload:
                self._settings["auto_broadcast"] = bool(payload["auto_broadcast"])
            if "debug_log" in payload:
                self._settings["debug_log"] = bool(payload["debug_log"])
            ok = self._save_settings()
            return {
                "status": "ok" if ok else "error",
                "message": None if ok else "设置未能写入磁盘",
                "data": dict(self._settings),
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 保存设置失败: {exc}")
            return {"status": "error", "message": "保存设置失败", "data": dict(self._settings)}

    async def api_limit_override(self) -> dict[str, Any]:
        """手动兜底上限：0 = 自动读取；保存后清掉上限缓存，下次读取立即生效。"""
        try:
            payload = await self._json_body()
            raw = payload.get("value", payload.get("limit_override"))
            if raw is None:
                return {"status": "error", "message": "缺少上限数值"}
            try:
                value = int(float(str(raw).strip()))
            except (TypeError, ValueError):
                return {"status": "error", "message": "请填写数字（0 表示自动读取）"}
            if value != 0 and not (1000 <= value <= 10_000_000):
                return {"status": "error", "message": "手动上限需在 1,000 ~ 10,000,000 之间，或填 0 自动读取"}
            self._settings["limit_override"] = value
            self._limit_cache.clear()
            ok = self._save_settings()
            logger.info(f"[token_panel] 手动上限设为 {value or '自动'}")
            return {
                "status": "ok" if ok else "error",
                "message": None if ok else "设置未能写入磁盘",
                "data": {
                    "limit_override": value,
                    "ambiguous": bool(payload.get("ambiguous")),
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 保存手动上限失败: {exc}")
            return {"status": "error", "message": "保存失败，请稍后再试"}

    async def api_clear(self) -> dict[str, Any]:
        """清空指定会话的上下文（面板按钮）。"""
        try:
            payload = await self._json_body()
            cid = str(payload.get("cid") or "").strip()
            if not cid:
                return {"status": "error", "message": "缺少会话 ID"}

            db = self._db()
            row = await db.get_conversation_by_id(cid)
            if row is None:
                return {"status": "error", "message": "会话不存在或已被删除"}

            umo = str(getattr(row, "user_id", "") or "")
            platform, mtype, session_id = _split_umo(umo)
            admins = self._admins()
            if _kind_of(mtype, platform) == "friend" and not _is_web(platform) and admins:
                haystack = session_id or umo
                if not any(admin in haystack for admin in admins):
                    return {"status": "error", "message": "无权操作该会话"}

            await self._backup_before_clear(umo, cid)
            cm = self.context.conversation_manager
            await cm.update_conversation(umo, conversation_id=cid, history=[], token_usage=0)
            self._drop_meta(cid)
            # 会话清空：日记本同步清空（日记不参与压缩，但跟着会话走）
            try:
                self._diary_clear(umo, cid)
            except Exception as exc:
                logger.warning(f"[token_panel] 清空日记失败: {exc}")
            logger.info(f"[token_panel] 面板清空会话 {cid[:8]}")
            return {"status": "ok", "data": {"cid": cid}}
        except Exception as exc:
            logger.warning(f"[token_panel] 面板清空失败: {exc}")
            return {"status": "error", "message": "清空失败，请稍后再试"}

    async def api_clear_empty(self) -> dict[str, Any]:
        """一键删除空会话（0 tokens 且确认没有任何历史内容），删除前逐个备份。"""
        try:
            rows = await self._fetch_rows()
            db = self._db()
            admins = self._admins()
            cm = self.context.conversation_manager
            removed = 0
            skipped = 0
            for row in rows:
                cid = str(getattr(row, "conversation_id", "") or "")
                if not cid:
                    continue
                try:
                    umo = str(getattr(row, "user_id", "") or "")
                    platform, mtype, session_id = _split_umo(umo)
                    if (
                        _kind_of(mtype, platform) == "friend"
                        and not _is_web(platform)
                        and admins
                    ):
                        haystack = session_id or umo
                        if not any(admin in haystack for admin in admins):
                            continue
                    if int(getattr(row, "token_usage", 0) or 0) > 0:
                        continue
                    # 双源确认：会话管理器 + 数据库行，任意一处有内容都不删
                    in_mem = ""
                    try:
                        conv = await cm.get_conversation(umo, cid)
                        in_mem = str(getattr(conv, "history", "") or "")
                    except Exception as exc:
                        logger.warning(f"[token_panel] 读会话管理器失败 cid={cid[:8]}: {exc}")
                        skipped += 1
                        continue
                    if in_mem.strip() not in ("", "[]", "null"):
                        skipped += 1
                        continue
                    try:
                        db_row = await db.get_conversation_by_id(cid)
                        in_db = str(getattr(db_row, "content", "") or "")
                    except Exception as exc:
                        logger.warning(f"[token_panel] 读数据库行失败 cid={cid[:8]}: {exc}")
                        skipped += 1
                        continue
                    if in_db.strip() not in ("", "[]", "null"):
                        skipped += 1
                        continue
                    # 兜底：真到删除这一步先留备份，备份失败就不删
                    if not await self._backup_before_clear(umo, cid):
                        skipped += 1
                        continue
                    await db.delete_conversation(cid)
                    self._drop_meta(cid)
                    try:
                        self._diary_clear(umo, cid)
                    except Exception as exc:
                        logger.warning(f"[token_panel] 清空日记失败: {exc}")
                    removed += 1
                    logger.info(f"[token_panel] 删除空会话 {cid[:8]}")
                except Exception as exc:
                    logger.warning(f"[token_panel] 删除空会话失败 cid={cid[:8]}: {exc}")
                    skipped += 1
            logger.info(f"[token_panel] 删除空会话 {removed} 个，跳过 {skipped} 个")
            return {"status": "ok", "data": {"removed": removed, "skipped": skipped}}
        except Exception as exc:
            logger.warning(f"[token_panel] 删除空会话失败: {exc}")
            return {"status": "error", "message": "删除失败，请稍后再试"}

    async def api_clear_idle(self) -> dict[str, Any]:
        """一键清理闲置的网页会话（默认 7 天没有新消息），连同备注一起删除。"""
        try:
            rows = await self._fetch_rows()
            db = self._db()
            removed = 0
            for row in rows:
                try:
                    umo = str(getattr(row, "user_id", "") or "")
                    platform, _mtype, _session = _split_umo(umo)
                    if not _is_web(platform):
                        continue
                    if _idle_days(getattr(row, "updated_at", None)) < IDLE_DAYS:
                        continue
                    cid = str(getattr(row, "conversation_id", "") or "")
                    if not cid:
                        continue
                    await self._backup_before_clear(umo, cid)
                    await db.delete_conversation(cid)
                    self._drop_meta(cid)
                    try:
                        self._diary_clear(umo, cid)
                    except Exception as exc:
                        logger.warning(f"[token_panel] 清空日记失败: {exc}")
                    removed += 1
                except Exception as exc:
                    self._dbg(f"清理闲置会话失败: {exc}")
            logger.info(f"[token_panel] 清理闲置网页会话 {removed} 个")
            return {"status": "ok", "data": {"removed": removed}}
        except Exception as exc:
            logger.warning(f"[token_panel] 清理闲置会话失败: {exc}")
            return {"status": "error", "message": "清理失败，请稍后再试"}

# ------------------------------------------------------------------ #
    # 压缩承接 / 统计 的面板接口（集成模块）
    # ------------------------------------------------------------------ #
    async def api_stats(self) -> dict[str, Any]:
        """单会话统计：模型、已用、上限、占比（数据全部来自官方接口）。"""
        try:
            payload = await self._json_body()
            umo = str(payload.get("umo") or "").strip()
            cid = str(payload.get("cid") or "").strip() or None
            if not umo:
                return {"status": "error", "message": "缺少会话信息"}

            total, has_record = await self._last_input_tokens(umo, cid)
            # 每次打开统计都重新解析一次上限：换模型 / 改模型配置后立刻生效
            limit, source = await self._resolve_ctx_limit(umo, force=True)
            _cid, history = await self._history_of(umo, cid)
            provider = await self._provider_for(umo)
            model = _provider_model_name(provider) if provider is not None else None
            percent = round(total / limit * 100.0, 1) if limit else None
            return {
                "status": "ok",
                "data": {
                    "umo": umo,
                    "cid": _cid or cid,
                    "model": model,
                    "used": total,
                    "has_record": has_record,
                    "limit": limit,
                    "limit_source": source,
                    "percent": percent,
                    "remaining": (max(0, limit - total) if limit else None),
                    "tip": (_ctx_tip(percent) if percent is not None else "获取模型上限失败"),
                    "rounds": rounds_of(history),
                    "messages": len(history),
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 面板统计失败: {exc}")
            return {"status": "error", "message": "读取上下文统计数据失败"}

    async def api_compress(self) -> dict[str, Any]:
        """压缩指定会话（等价于 /ctx_compress）。"""
        try:
            payload = await self._json_body()
            umo = str(payload.get("umo") or "").strip()
            cid = str(payload.get("cid") or "").strip() or None
            try:
                keep = int(payload.get("keep") or self.keep_rounds)
            except (TypeError, ValueError):
                keep = self.keep_rounds
            if not umo:
                return {"status": "error", "message": "缺少会话信息"}
            result = await self._compress_core(umo, cid, max(1, keep))
            return {
                "status": "ok",
                "data": {
                    "ok": bool(result.get("ok")),
                    "type": "手动压缩",
                    "message": str(result.get("text") or ""),
                    "cid": result.get("cid") or cid,
                    "before": int(result.get("before") or 0),
                    "after": int(result.get("after") or 0),
                    "backup": result.get("backup") or "",
                    "backup_ok": bool(result.get("backup")),
                    "backup_error": result.get("backup_error") or "",
                    "new_cid": "",
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 面板压缩失败: {exc}")
            return {"status": "error", "message": "压缩失败，请稍后再试"}

    async def api_continue(self) -> dict[str, Any]:
        """生成承接摘要并切到新会话（等价于 /ctx_continue）。"""
        try:
            payload = await self._json_body()
            umo = str(payload.get("umo") or "").strip()
            cid = str(payload.get("cid") or "").strip() or None
            try:
                keep = int(payload.get("keep") or 4)
            except (TypeError, ValueError):
                keep = 4
            if not umo:
                return {"status": "error", "message": "缺少会话信息"}
            result = await self._continue_core(umo, cid, max(0, keep))
            return {
                "status": "ok",
                "data": {
                    "ok": bool(result.get("ok")),
                    "type": "手动承接",
                    "message": str(result.get("text") or ""),
                    "cid": result.get("cid") or cid,
                    "new_cid": result.get("new_cid"),
                    "backup": result.get("backup") or "",
                    "backup_ok": bool(result.get("backup")),
                    "backup_error": result.get("backup_error") or "",
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 面板承接失败: {exc}")
            return {"status": "error", "message": "承接失败，请稍后再试"}

    async def api_backup_preview(self) -> dict[str, Any]:
        """预览压缩/承接前的历史备份（只读，严格限制在插件备份目录内）。"""
        try:
            payload = await self._json_body()
            name = str(payload.get("name") or "").strip()
            cid = str(payload.get("cid") or "").strip()
            if not name or not re.fullmatch(r"[0-9A-Za-z._-]{1,90}\.json", name):
                return {"status": "error", "message": "备份文件名不合法"}
            root = self.carry_backup_dir.resolve()
            target = None
            for path in root.rglob(name):
                try:
                    path.resolve().relative_to(root)
                except Exception:
                    continue  # 越界路径直接跳过
                if cid and not path.name.startswith(cid[:8]):
                    continue
                target = path
                break
            if target is None:
                return {"status": "error", "message": "备份文件不存在或已被清理"}
            raw = json.loads(target.read_text(encoding="utf-8"))
            messages = raw if isinstance(raw, list) else []
            items: list[dict[str, str]] = []
            for msg in messages[:10]:
                try:
                    items.append(
                        {
                            "role": str(msg.get("role") or "unknown"),
                            "text": msg_text(msg)[:140],
                        }
                    )
                except Exception:
                    continue
            stat = target.stat()
            return {
                "status": "ok",
                "data": {
                    "name": target.name,
                    "size": stat.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                    "count": len(messages),
                    "items": items,
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 读取备份失败: {exc}")
            return {"status": "error", "message": "读取备份失败，请稍后再试"}

    async def api_carry(self) -> dict[str, Any]:
        """读取 / 保存压缩承接设置（GET 读、POST 部分更新）。"""
        try:
            from astrbot.api.web import request as plugin_request

            method = str(getattr(plugin_request, "method", "GET") or "GET").upper()
            if method == "GET":
                return {
                    "status": "ok",
                    "data": {
                        "carry": dict(self.carry),
                        "defaults": dict(CARRY_DEFAULTS),
                        "limits": {k: list(v) for k, v in CARRY_LIMITS.items()},
                        "backup_limit": CARRY_BACKUP_LIMIT,
                        "log_limit": CARRY_LOG_LIMIT,
                    },
                }

            payload = await self._json_body()
            carry = dict(self.carry)
            if "token_limit" in payload:
                carry["token_limit"] = _clamp_int(payload.get("token_limit"), 1000, 10000000, 128000)
            if "keep_rounds" in payload:
                carry["keep_rounds"] = _clamp_int(payload.get("keep_rounds"), 1, 50, 8)
            if "summary_max_chars" in payload:
                carry["summary_max_chars"] = _clamp_int(payload.get("summary_max_chars"), 100, 2000, 500)
            if "auto_threshold" in payload:
                try:
                    ratio = float(payload.get("auto_threshold"))
                except (TypeError, ValueError):
                    ratio = 0.8
                carry["auto_threshold"] = round(min(0.95, max(0.3, ratio)), 2)
            if "auto_enabled" in payload:
                carry["auto_enabled"] = bool(payload.get("auto_enabled"))
            if "enhanced_summary" in payload:
                carry["enhanced_summary"] = bool(payload.get("enhanced_summary"))
            self._settings["carry"] = carry
            ok = self._save_settings()
            return {
                "status": "ok",
                "data": {
                    "carry": dict(carry),
                    "saved": ok,
                    "limits": {k: list(v) for k, v in CARRY_LIMITS.items()},
                    "backup_limit": CARRY_BACKUP_LIMIT,
                    "log_limit": CARRY_LOG_LIMIT,
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 压缩承接设置保存失败: {exc}")
            return {"status": "error", "message": "保存设置失败"}

    async def api_compress_log(self) -> dict[str, Any]:
        """压缩日志（只读，最近在前）。"""
        try:
            items = self._carry_log_read()
            return {"status": "ok", "data": {"items": items[:50], "total": len(items)}}
        except Exception as exc:
            logger.warning(f"[token_panel] 读取压缩日志失败: {exc}")
            return {"status": "ok", "data": {"items": [], "total": 0}}


    # ------------------------------------------------------------------ #
    # 联网搜索监控：只读 websearch 插件的 SQLite 日志，不改它任何状态
    # ------------------------------------------------------------------ #
    NET_LOG_DB_NAME = "search_log.db"
    NET_PLUGIN_HINT = "astrbot_plugin_websearch"

    def _net_db_path(self) -> Path:
        """搜索日志库位置：优先 settings.net_log_db，其次自动在同级插件目录里找。

        不再硬编码作者本机路径；换机器或换盘符，只要 websearch 插件还在就能定位。
        """
        try:
            custom = str(self._settings.get("net_log_db") or "").strip()
            if custom:
                return Path(custom)
        except Exception:
            pass
        base = self.data_dir.parent  # .../data/plugin_data
        preferred = base / self.NET_PLUGIN_HINT / self.NET_LOG_DB_NAME
        if preferred.exists():
            return preferred
        try:
            for cand in sorted(base.glob("*/" + self.NET_LOG_DB_NAME)):
                return cand
        except Exception:
            pass
        return preferred

    def _net_rows(self, keyword: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """读搜索日志（只读连接，任何异常都返回空列表）。"""
        rows: list[dict[str, Any]] = []
        try:
            db_path = self._net_db_path()
            if not db_path.exists():
                return rows
            import sqlite3

            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                cur = conn.cursor()
                sql = ("SELECT id, ts, provider, query, status, count, took_ms FROM search_log")
                args: list[Any] = []
                if keyword:
                    sql += " WHERE query LIKE ?"
                    args.append(f"%{keyword}%")
                sql += " ORDER BY id DESC LIMIT ?"
                args.append(int(limit))
                for row in cur.execute(sql, args):
                    rows.append({
                        "id": row[0], "ts": row[1], "provider": row[2], "query": row[3],
                        "status": row[4], "count": row[5], "took_ms": row[6],
                    })
            finally:
                conn.close()
        except Exception as exc:
            self._dbg(f"读取搜索日志失败: {exc}")
        return rows

    def _net_stats(self) -> dict[str, Any]:
        stats = {"total": 0, "today": 0, "ok": 0, "fail": 0, "avg_ms": 0}
        try:
            db_path = self._net_db_path()
            if not db_path.exists():
                return stats
            import sqlite3

            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                cur = conn.cursor()
                stats["total"] = cur.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
                today = datetime.now().strftime("%Y-%m-%d")
                stats["today"] = cur.execute(
                    "SELECT COUNT(*) FROM search_log WHERE ts LIKE ?", (today + "%",)).fetchone()[0]
                stats["ok"] = cur.execute(
                    "SELECT COUNT(*) FROM search_log WHERE status='ok'").fetchone()[0]
                fail = cur.execute(
                    "SELECT COUNT(*) FROM search_log WHERE status!='ok'").fetchone()[0]
                stats["fail"] = fail
                avg = cur.execute("SELECT AVG(took_ms) FROM search_log WHERE status='ok'").fetchone()[0]
                stats["avg_ms"] = int(avg or 0)
            finally:
                conn.close()
        except Exception as exc:
            self._dbg(f"统计搜索日志失败: {exc}")
        return stats

    async def cmd_net_log(self, event: AstrMessageEvent):
        """查看最近联网搜索记录"""
        rows = self._net_rows(limit=5)
        if not rows:
            yield event.plain_result("还没有联网搜索记录。")
            return
        lines = ["【最近联网搜索】"]
        for row in rows:
            mark = "✅" if row["status"] == "ok" else "⚠️"
            lines.append(f"{mark} {row['ts'][11:16]} {str(row['query'])[:30]} → {row['count']} 条 / {row['took_ms']}ms")
        yield event.plain_result("\n".join(lines))

    async def api_net_overview(self) -> dict[str, Any]:
        """联网搜索监控总览。"""
        try:
            payload = await self._json_body()
            keyword = str(payload.get("keyword") or "").strip()[:60]
            return {"status": "ok", "data": {
                "rows": self._net_rows(keyword=keyword, limit=50),
                "stats": self._net_stats(),
                "db_found": self._net_db_path().exists(),
            }}
        except Exception as exc:
            logger.warning(f"[token_panel] 联网监控失败: {exc}")
            return {"status": "ok", "data": {"rows": [], "stats": {}, "db_found": False}}

    async def api_net_clear(self) -> dict[str, Any]:
        """清空搜索日志（只清日志表，不动插件配置）。"""
        try:
            db_path = self._net_db_path()
            if not db_path.exists():
                return {"status": "ok", "data": {"removed": 0}}
            import sqlite3

            conn = sqlite3.connect(str(db_path), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM search_log")
                removed = cur.rowcount
                conn.commit()
            finally:
                conn.close()
            return {"status": "ok", "data": {"removed": removed}}
        except Exception as exc:
            logger.warning(f"[token_panel] 清空搜索日志失败: {exc}")
            return {"status": "error", "message": "清空失败"}

    # ------------------------------------------------------------------ #
    # 抖音游客浏览：只打开抖音精选页滚动看视频，不登录/点赞/评论/搜索
    # 真实浏览器跑在独立子进程里，任何异常都不会阻塞机器人的消息主线程
    # ------------------------------------------------------------------ #
    DY_DEFAULTS: dict[str, Any] = {
        "enabled": False,
        "interval_minutes": 180,
        "max_minutes": 8,
        "max_videos": 8,
        "daily_sessions": 3,
        "quiet_start": 1,
        "quiet_end": 7,
        "keywords": "",
        "notify": True,
    }
    DY_LOG_LIMIT = 100
    DY_WORKER_TIMEOUT_PAD = 180
    # 内存里保留的"最近浏览"条数（只用于给模型拼上下文，服务重启即清空）
    DY_RECENT_LIMIT = 20
    # 主人口头问起浏览记录的问法（自然语言自动触发）。要求"刷/看/浏览"类词 + 疑问/记录类词同时出现，
    # 避免聊天里随便提到"抖音"就把日志灌进提示词。
    DY_ASK_RES = (
        re.compile(r"(浏览|观看|刷片|刷视频|刷抖音|刷到)的?(记录|历史|清单)"),
        re.compile(r"(刷了|刷到|看了|看到|推了|推到)的?(什么|啥|哪些|多少|什么视频|哪些视频)"),
        re.compile(r"(什么|啥|哪些)视频.{0,6}(刷|看|推)"),
        re.compile(r"(刚才|刚刚|最近|今天|昨天).{0,10}(刷了|刷到|看了|推到).{0,6}(什么|啥|哪些|多少)"),
        re.compile(r"(汇报|说说|讲讲|聊聊|整理).{0,8}(刷|看|浏览)"),
        re.compile(r"(刷|看)的(都是|全是)(什么|啥)"),
    )
    # 命中安全验证后的惩罚冷却（分钟）：冷却期内自动/手动都不许再开浏览器
    DY_RISK_COOLDOWN_MIN = 30
    DY_PROFILE_DIRNAME = "douyin_profile"
    # 主人口头指令优先窗口（秒）：/dy_set 之后这段时间内算法不压缩他给的参数
    DY_OVERRIDE_WINDOW = 7200

    def _dy_settings(self) -> dict[str, Any]:
        """抖音配置（挂在 settings.json 的 douyin 段，与其它设置同一份文件）。"""
        dy = self._settings.get("douyin")
        if not isinstance(dy, dict):
            dy = {}
            self._settings["douyin"] = dy
        for key, value in self.DY_DEFAULTS.items():
            dy.setdefault(key, value)
        return dy

    def _risk_strictness(self) -> float:
        """读取严格系数：0.1~2.0，坏值回落到默认 1.0。"""
        try:
            value = float(self._settings.get("risk_strictness", 1.0))
        except Exception:
            value = 1.0
        return max(0.1, min(2.0, value))

    def _dy_effective(self) -> dict[str, Any]:
        """算这一轮真正生效的浏览参数：风控档位 × （主人口头指令优先）。"""
        dy = self._dy_settings()
        strict = self._risk_strictness()
        try:
            params = self.risk.effective_params(dy, strict)
        except Exception as exc:
            self._dbg(f"风控调速计算失败: {exc}")
            params = {
                "videos": max(1, int(dy.get("max_videos", 8))),
                "minutes": float(dy.get("max_minutes", 8)),
                "interval": float(dy.get("interval_minutes", 180)),
                "dwell_min": 8.0,
                "dwell_max": 20.0,
                "snapshot": {},
            }
        manual = (time.time() - float(self._dy_manual_at or 0.0)) < float(self.DY_OVERRIDE_WINDOW)
        if manual:
            # 主人口头指令优先：条数/时长/间隔按他自己设的走；8 秒停留底线不破
            params.update({
                "videos": max(1, int(dy.get("max_videos", 8))),
                "minutes": float(dy.get("max_minutes", 8)),
                "interval": float(dy.get("interval_minutes", 180)),
                "dwell_min": 8.0,
                "dwell_max": 20.0,
            })
        params["manual"] = manual
        params["strictness"] = strict
        if not params.get("snapshot"):
            try:
                params["snapshot"] = self.risk.snapshot(strict)
            except Exception:
                params["snapshot"] = {}
        return params

    def _dy_state_path(self) -> Path:
        return self.data_dir / "douyin_state.json"

    def _dy_log_path(self) -> Path:
        return self.data_dir / "douyin_log.json"

    def _dy_state(self) -> dict[str, Any]:
        today = datetime.now().date().isoformat()
        state: dict[str, Any] = {
            "date": today,
            "sessions": 0,
            "minutes": 0.0,
            "videos": 0,
            "last_run": "",
            "last_error": "",
            "risk_hits": 0,
            "risk_until": "",
            "running": False,
        }
        try:
            raw = json.loads(self._dy_state_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update(raw)
        except Exception:
            pass
        if state.get("date") != today:
            state.update({"date": today, "sessions": 0, "minutes": 0.0, "videos": 0})
        # 计数永远以日志为准（状态文件里的旧数字只做兜底，读出来立刻被覆盖）
        state.update(self._dy_stats_from_log())
        state["date"] = today
        return state

    def _dy_save_state(self, state: dict[str, Any]) -> None:
        try:
            self._dy_state_path().write_text(
                json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[token_panel] 保存抖音状态失败: {exc}")

    def _dy_log_read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._dy_log_path().read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _dy_log_add(self, entry: dict[str, Any]) -> None:
        items = self._dy_log_read()
        items.insert(0, entry)
        try:
            self._dy_log_path().write_text(
                json.dumps(items[: self.DY_LOG_LIMIT], ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[token_panel] 写抖音日志失败: {exc}")

    # ---- 最近浏览（内存缓冲）：给模型拼上下文用，不写盘 --------------------- #
    def _dy_recent_list(self) -> list[dict[str, Any]]:
        buf = getattr(self, "_dy_recent", None)
        if not isinstance(buf, list):
            buf = []
            self._dy_recent = buf
        return buf

    def _dy_recent_append(
        self, items: list[dict[str, Any]], session_ts: str, session_id: str = ""
    ) -> None:
        """把一次浏览的视频写进内存缓冲（最新的在最前，超过 20 条自动丢最旧）。"""
        buf = self._dy_recent_list()
        for item in reversed(list(items or [])):
            if not isinstance(item, dict):
                continue
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
            if not title:
                continue
            buf.insert(0, {
                "ts": str(item.get("watched_at") or session_ts or ""),
                "title": title[:80],
                "dwell": item.get("dwell"),
                "duration": item.get("duration"),
                "session": str(session_id or "")[:15],
            })
        del buf[self.DY_RECENT_LIMIT:]

    def _dy_recent_rows(self, limit: int = 0) -> list[dict[str, Any]]:
        buf = self._dy_recent_list()
        rows = buf[: (limit or self.DY_RECENT_LIMIT)]
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _dy_short_time(raw: Any) -> str:
        """2026-10-29 21:03:12 -> 10-29 21:03；解析失败就原样返回。"""
        text = str(raw or "").strip()
        if not text:
            return "时间未知"
        try:
            return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").strftime("%m-%d %H:%M")
        except Exception:
            return text[:16]

    def _dy_recent_lines(self, limit: int = 0) -> list[str]:
        """一行一条的人工可读记录，既用于兜底纯文本，也用于拼进提示词。"""
        lines: list[str] = []
        for idx, row in enumerate(self._dy_recent_rows(limit), 1):
            bits = [f"{idx}. {self._dy_short_time(row.get('ts'))}"]
            dwell = row.get("dwell")
            try:
                if float(dwell or 0) > 0:
                    bits.append(f"停留 {float(dwell):.0f} 秒")
            except Exception:
                pass
            duration = row.get("duration")
            try:
                if float(duration or 0) > 0:
                    bits.append(f"原片 {float(duration):.0f} 秒")
            except Exception:
                pass
            lines.append("｜".join(bits) + f"｜{row.get('title') or ''}")
        return lines

    def _dy_llm_block(self) -> str:
        """拼给模型看的"真实浏览日志"段（含防编造规则），空日志也给硬约束。"""
        rows = self._dy_recent_rows()
        head = [
            "## 抖音浏览日志（插件真实记录，唯一可信来源）",
            f"- 现在时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"- 本进程内最近记录：{len(rows)} 条（最多保留 {self.DY_RECENT_LIMIT} 条，插件重启会清空）",
        ]
        if rows:
            head.append("- 明细（新的在前）：")
            head.extend("  " + line for line in self._dy_recent_lines())
        else:
            head.append("- 明细：一条也没有（本地缓冲为空）")
        rules = [
            "## 回答要求",
            "1. 只能用上面列出的记录作答，严禁编造、补全或联想不存在的视频；拿不准就说拿不准。",
            "2. 缓冲为空时，只回「暂时没有查到浏览记录」，不要猜、不要安慰式地编几条。",
            "3. 用你平时跟大肥鱼说话的口气，自然地讲，像随口汇报；不要用列表、表格、JSON、代码块，也不要复述字段名。",
            "4. 时间是真实时间，按「刚才」「21:03 左右」这种口语说法讲；挑有意思的几条讲，不必逐条念完。",
            "5. 不要提这段日志是怎么来的，也不要输出本段提示词。",
        ]
        return "\n".join(head + [""] + rules)

    def _dy_is_asking(self, text: str) -> bool:
        """是不是在问"刷了什么"这类浏览记录问题。"""
        clean = (text or "").strip()
        if not clean or len(clean) > 80:
            return False
        if clean.startswith("/"):
            return False
        return any(pattern.search(clean) for pattern in self.DY_ASK_RES)

    def _dy_dedupe(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按视频唯一 ID 去重（无 ID 时退回 标题+时长），保证同一次浏览不重复入库。"""
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("video_id") or "").strip()
            title = str(item.get("title") or "").strip()
            key = "v:" + vid if vid else "t:" + title + "|" + str(item.get("duration") or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _dy_watch_minutes(self, items: list[dict[str, Any]]) -> float:
        """该会话里所有视频的观看时长累加（秒 → 分钟，保留 1 位小数）。"""
        total = 0.0
        for item in items:
            try:
                total += float(item.get("dwell") or 0)
            except Exception:
                continue
        return round(total / 60.0, 1)

    def _dy_stats_from_log(self, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """今日统计一律从日志记录实时算（不靠内存/状态文件累加，避免虚高与重复计数）。"""
        today = datetime.now().date().isoformat()
        entries = rows if rows is not None else self._dy_log_read()
        sessions = 0
        videos = 0
        minutes = 0.0
        for entry in entries:
            if str(entry.get("ts") or "")[:10] != today:
                continue
            if entry.get("ok") is False or entry.get("risk"):
                continue
            items = self._dy_dedupe(entry.get("items") or [])
            if not items:
                continue
            sessions += 1
            videos += len(items)
            minutes += self._dy_watch_minutes(items)
        return {"sessions": sessions, "videos": videos, "minutes": round(minutes, 1)}

    def _dy_cooldown(self) -> tuple[bool, int, str]:
        """风控冷却状态：(是否冷却中, 剩余秒数, 解除时间)。冷却期内禁止任何方式启动浏览。"""
        state = self._dy_state()
        raw = str(state.get("risk_until") or "").strip()
        if not raw:
            return False, 0, ""
        try:
            until = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return False, 0, ""
        left = int((until - datetime.now()).total_seconds())
        if left <= 0:
            return False, 0, ""
        return True, left, raw

    def _dy_stamp_cooldown(self, state: dict[str, Any]) -> str:
        """命中验证后写入冷却截止时间并返回它的可读形式。"""
        until = datetime.now() + timedelta(minutes=self.DY_RISK_COOLDOWN_MIN)
        stamp = until.strftime("%Y-%m-%d %H:%M:%S")
        state["risk_until"] = stamp
        return stamp

    def _dy_in_quiet_hours(self, dy: dict[str, Any]) -> bool:
        try:
            hour = datetime.now().hour
            start = int(dy.get("quiet_start", 1)) % 24
            end = int(dy.get("quiet_end", 7)) % 24
        except Exception:
            return False
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    async def _dy_run_once(self, manual: bool = False) -> dict[str, Any]:
        """跑一次游客浏览；manual=True 时忽略间隔与每日上限（仍受时长/条数约束）。"""
        state = self._dy_state()
        if state.get("running"):
            return {"ok": False, "message": "上一次浏览还没结束"}
        cooling, left, until = self._dy_cooldown()
        if cooling:
            return {
                "ok": False,
                "cooldown": True,
                "remaining": left,
                "message": f"风控冷却中：还需 {left // 60} 分 {left % 60} 秒（{until} 之后才能再刷）",
            }
        dy = self._dy_settings()
        eff = self._dy_effective()
        worker = Path(__file__).parent / "worker" / "douyin_worker.py"
        if not worker.exists():
            return {"ok": False, "message": "缺少 worker/douyin_worker.py"}
        out_path = self.data_dir / "douyin_last.json"
        state["running"] = True
        self._dy_save_state(state)
        started = datetime.now()
        proc = None
        try:
            env = os.environ.copy()
            browsers_path = str(self._settings.get("playwright_browsers_path") or "").strip()
            if browsers_path:
                env.setdefault("PLAYWRIGHT_BROWSERS_PATH", browsers_path)
            env.setdefault("PYTHONUTF8", "1")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-X", "utf8", str(worker),
                "--max-videos", str(int(eff.get("videos", 8))),
                "--max-minutes", str(float(eff.get("minutes", 8))),
                "--dwell-min", str(float(eff.get("dwell_min", 8.0))),
                "--dwell-max", str(float(eff.get("dwell_max", 20.0))),
                "--profile-dir", str(self.data_dir / self.DY_PROFILE_DIRNAME),
                "--out", str(out_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            timeout = float(eff.get("minutes", 8)) * 60 + self.DY_WORKER_TIMEOUT_PAD
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            state["running"] = False
            state["last_error"] = "浏览超时"
            self._dy_save_state(state)
            return {"ok": False, "message": "浏览超时，已终止子进程"}
        except Exception as exc:
            state["running"] = False
            state["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            self._dy_save_state(state)
            return {"ok": False, "message": f"启动浏览器失败：{exc}"}

        result: dict[str, Any] = {}
        try:
            if out_path.exists():
                result = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as exc:
            result = {"ok": False, "error": f"读取结果失败: {exc}"}

        session_id = started.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
        raw_items = result.get("items") or []
        if not isinstance(raw_items, list):
            raw_items = []
        items = self._dy_dedupe(raw_items)
        # 先进内存缓冲（给模型拼上下文用），再走原有落盘日志
        try:
            self._dy_recent_append(items, started.strftime("%Y-%m-%d %H:%M:%S"), session_id)
        except Exception as exc:
            self._dbg(f"写最近浏览缓冲失败: {exc}")
        videos = len(items)
        minutes = round(float(result.get("minutes") or 0), 1)
        ok = bool(result.get("ok"))
        risk = bool(result.get("risk"))
        state.update({
            "running": False,
            "last_run": started.strftime("%Y-%m-%d %H:%M:%S"),
            "last_error": str(result.get("error") or "")[:200],
        })
        if risk:
            state["risk_hits"] = int(state.get("risk_hits", 0)) + 1
            cooldown_until = self._dy_stamp_cooldown(state)
            try:
                self.risk.record_verification()
            except Exception as exc:
                self._dbg(f"记录风控事件失败: {exc}")
        else:
            cooldown_until = str(state.get("risk_until") or "")

        entry = {
            "session_id": session_id,
            "ts": started.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "手动" if manual else "自动",
            "ok": ok,
            "risk": risk,
            "videos": videos,
            "minutes": minutes,
            "watch_minutes": self._dy_watch_minutes(items),
            "error": state.get("last_error", ""),
            "items": items[:30],
            "risk_until": cooldown_until,
            "tier": str(eff.get("snapshot", {}).get("tier") or ""),
            "dwell": [eff.get("dwell_min"), eff.get("dwell_max")],
        }
        self._dy_log_add(entry)
        # 写库之后再统计，保证顶部数字与列表条数来自同一份不重复记录
        state.update(self._dy_stats_from_log())
        self._dy_save_state(state)
        logger.info(
            f"[token_panel] 抖音浏览结束：{videos} 个视频 / {minutes} 分钟 / "
            f"ok={ok} risk={risk}"
        )
        if risk:
            try:
                tier_now = self.risk.snapshot(self._risk_strictness()).get("tier", "正常")
            except Exception:
                tier_now = "正常"
            await self._dy_notify(
                f"⚠️ 抖音出现安全验证，本次浏览终止。\n"
                f"已进入 {self.DY_RISK_COOLDOWN_MIN} 分钟冷却（至 {cooldown_until}），期间不会自动重试，"
                f"手动 /dy_test 也会被拒绝。\n当前风控档位：{tier_now}。"
            )
        elif ok and videos and dy.get("notify", True):
            await self._dy_notify(self._dy_digest(items))
        return {"ok": ok, "message": state.get("last_error") or "", "videos": videos,
                "minutes": minutes, "risk": risk}

    def _dy_digest(self, items: list[dict[str, Any]]) -> str:
        """把一次浏览结果整理成一条简短通知（不刷屏，一条概括）。"""
        lines = ["🛒 大肥鱼刚刷了会儿抖音（游客模式，没登录）"]
        for item in items[:6]:
            title = str(item.get("title", "")).strip()
            title = re.sub(r"\s*@.+$", "", title)[:42]
            dwell = item.get("dwell")
            suffix = f"（看了 {dwell} 秒）" if dwell else ""
            lines.append(f"· {title}{suffix}")
        if len(items) > 6:
            lines.append(f"… 还有 {len(items) - 6} 条")
        lines.append("要我细说哪条，直接问我。")
        return "\n".join(lines)

    async def _dy_notify(self, text: str) -> None:
        """通知只发给管理员的大号（固定 QQ 私聊），失败仅记日志。"""
        admins = sorted(self._admins())
        if not admins:
            return
        platform = "napcat"
        if self.context.platform_manager:
            for meta in self.context.platform_manager.platform_insts:
                platform = getattr(meta, "id", platform) or platform
                break
        try:
            from astrbot.core.message.message_event_result import MessageChain

            for admin in admins:
                umo = f"{platform}:FriendMessage:{admin}"
                await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:
            self._dbg(f"抖音通知发送失败: {exc}")

    async def _dy_loop(self) -> None:
        """后台调度：每分钟看一眼该不该刷，不轮询数据库、不占请求线程。"""
        await asyncio.sleep(45)
        while True:
            try:
                dy = self._dy_settings()
                state = self._dy_state()
                if dy.get("enabled") and not state.get("running"):
                    eff = self._dy_effective()
                    if not self._dy_in_quiet_hours(dy):
                        due = True
                        last = str(state.get("last_run") or "")
                        if last:
                            try:
                                delta = datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                                due = delta.total_seconds() >= float(eff.get("interval", 180)) * 60
                            except Exception:
                                due = True
                        if int(state.get("sessions", 0)) >= int(dy.get("daily_sessions", 3)):
                            due = False
                        if due:
                            await self._dy_run_once(manual=False)
            except Exception as exc:
                self._dbg(f"抖音调度异常: {exc}")
            await asyncio.sleep(60)

    @filter.command("dy_on")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_dy_on(self, event: AstrMessageEvent):
        """开启抖音游客浏览（仅管理员）"""
        self._dy_settings()["enabled"] = True
        self._save_settings()
        yield event.plain_result("已开启抖音游客浏览，会在允许时段内自动刷。")

    @filter.command("dy_off")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_dy_off(self, event: AstrMessageEvent):
        """关闭抖音游客浏览（仅管理员）"""
        self._dy_settings()["enabled"] = False
        self._save_settings()
        yield event.plain_result("已关闭抖音游客浏览。")

    @filter.command("dy_test")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_dy_test(self, event: AstrMessageEvent):
        """立刻浏览一次抖音（仅管理员）"""
        cooling, left, until = self._dy_cooldown()
        if cooling:
            yield event.plain_result(
                f"风控冷却中，暂时不能刷：还需 {left // 60} 分 {left % 60} 秒（{until} 之后解禁）。")
            return
        yield event.plain_result("开始浏览，大约 1-3 分钟，结束后把看到的内容发你。")
        asyncio.create_task(self._dy_run_once(manual=True))

    @filter.command("net_log")
    @filter.command("dy_status")
    async def cmd_dy_status(self, event: AstrMessageEvent):
        """查看抖音浏览状态"""
        dy = self._dy_settings()
        state = self._dy_state()
        lines = [
            "【抖音浏览】",
            f"状态：{'已开启' if dy.get('enabled') else '已关闭'}",
        ]
        cooling, left, until = self._dy_cooldown()
        if cooling:
            lines.append(f"风控冷却中：还需 {left // 60} 分 {left % 60} 秒（{until} 解禁，期间禁止启动）")
        lines += [
            f"今天已刷：{state.get('sessions', 0)} 次 / {state.get('videos', 0)} 个视频 / {state.get('minutes', 0)} 分钟",
            f"间隔：{dy.get('interval_minutes')} 分钟　单次上限：{dy.get('max_videos')} 个视频 / {dy.get('max_minutes')} 分钟",
            f"每日上限：{dy.get('daily_sessions')} 次　免打扰：{dy.get('quiet_start')}:00-{dy.get('quiet_end')}:00",
            f"上次浏览：{state.get('last_run') or '还没刷过'}",
        ]
        if state.get("last_error"):
            lines.append(f"上次异常：{state['last_error']}")
        yield event.plain_result("\n".join(lines))

    @filter.command("dy_log")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_dy_log(self, event: AstrMessageEvent):
        """/dy_log：把真实浏览记录交给模型，用平时的语气汇报（仅管理员）。"""
        if not self._dy_recent_rows():
            # 缓冲空着就别花一次模型调用了，直接说实话
            yield event.plain_result(
                "暂时没有查到浏览记录。\n"
                "（最近浏览只记在本进程内存里，插件重启后要再刷一次才会出现）",
            )
            return
        req = await self._dy_llm_request(event)
        if req is None:
            # 模型链路不可用时退回纯文本，保证这条指令永远有回应
            yield event.plain_result("【最近浏览】\n" + "\n".join(self._dy_recent_lines()))
            return
        yield req

    async def _dy_llm_request(self, event: AstrMessageEvent):
        """为 /dy_log 构造一次正常的模型请求：带人格、带历史，系统提示词里塞真实日志。"""
        try:
            from astrbot.core.provider.entities import ProviderRequest
        except Exception as exc:
            self._dbg(f"取 ProviderRequest 失败: {exc}")
            return None
        try:
            ask = (event.message_str or "").strip()
            ask = re.sub(r"^/dy_log\s*", "", ask).strip()
            req = ProviderRequest()
            req.prompt = ask or "看一下最近刷到的抖音记录，挑有意思的跟我说说。"
            req.system_prompt = self._dy_llm_block()
            req.session_id = getattr(event, "unified_msg_origin", "") or ""
            # 挂上当前会话：人格与历史才会生效（不挂的话 persona 注入会被整段跳过）
            try:
                umo = event.unified_msg_origin
                cm = self.context.conversation_manager
                cid = await cm.get_curr_conversation_id(umo)
                if cid:
                    req.conversation = await cm.get_conversation(umo, cid)
            except Exception as exc:
                self._dbg(f"挂载会话失败: {exc}")
            return req
        except Exception as exc:
            self._dbg(f"构造模型请求失败: {exc}")
            return None

    @filter.on_llm_request()
    async def dy_recent_inject(self, event: AstrMessageEvent, req):
        """主人用自然语言问起"刚刷了什么"时，才把真实日志拼进系统提示词（不做无差别注入）。"""
        try:
            if not event.is_admin():
                return
            if not self._dy_is_asking(event.message_str or ""):
                return
            block = self._dy_llm_block()
            if self._inject_extra(req, block):
                self._dbg("已按口头提问注入最近浏览记录")
        except Exception as exc:
            self._dbg(f"注入最近浏览记录失败: {exc}")

    @filter.command("dy_set")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_dy_set(self, event: AstrMessageEvent):
        """设置抖音浏览参数（仅管理员）"""
        raw = (event.message_str or "").replace("/dy_set", "", 1).strip()
        parts = raw.replace("=", " ").split()
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/dy_set 参数 值\n参数：interval_minutes / max_minutes / max_videos / "
                "daily_sessions / quiet_start / quiet_end / keywords / notify")
            return
        key, value = parts[0], " ".join(parts[1:])
        dy = self._dy_settings()
        if key not in self.DY_DEFAULTS:
            yield event.plain_result(f"没有这个参数：{key}")
            return
        try:
            if key in {"enabled", "notify"}:
                dy[key] = value.lower() in {"1", "on", "true", "yes", "开"}
            elif key in {"keywords"}:
                dy[key] = value[:120]
            elif key in {"max_minutes"}:
                dy[key] = min(60, max(1, float(value)))
            else:
                dy[key] = int(float(value))
        except Exception:
            yield event.plain_result("值不合法，请填数字")
            return
        self._save_settings()
        self._dy_manual_at = time.time()
        yield event.plain_result(f"已设置 {key} = {dy[key]}")

    async def api_dy_overview(self) -> dict[str, Any]:
        """抖音页总览：状态 + 配置 + 最近日志。"""
        try:
            dy = self._dy_settings()
            state = self._dy_state()
            rows = self._dy_log_read()
            sessions: list[dict[str, Any]] = []
            for entry in rows[:20]:
                items = self._dy_dedupe(entry.get("items") or [])
                sessions.append({
                    "session_id": str(entry.get("session_id") or ""),
                    "ts": entry.get("ts") or "",
                    "mode": entry.get("mode") or "自动",
                    "ok": entry.get("ok") is not False,
                    "risk": bool(entry.get("risk")),
                    "error": entry.get("error") or "",
                    "count": len(items),
                    "minutes": round(float(entry.get("minutes") or 0), 1),
                    "watch_minutes": self._dy_watch_minutes(items),
                    "items": items,
                })
            return {
                "status": "ok",
                "data": {
                    "settings": {k: dy.get(k) for k in self.DY_DEFAULTS},
                    "state": state,
                    "sessions": sessions,
                    "log": rows[:20],
                    "defaults": dict(self.DY_DEFAULTS),
                },
            }
        except Exception as exc:
            logger.warning(f"[token_panel] 读取抖音总览失败: {exc}")
            return {"status": "ok", "data": {"settings": {}, "state": {}, "sessions": [], "log": []}}

    async def api_dy_settings(self) -> dict[str, Any]:
        """保存抖音页设置。"""
        try:
            payload = await self._json_body()
            dy = self._dy_settings()
            for key in self.DY_DEFAULTS:
                if key not in payload:
                    continue
                value = payload.get(key)
                if key in {"enabled", "notify"}:
                    dy[key] = bool(value)
                elif key == "keywords":
                    dy[key] = str(value or "")[:120]
                elif key == "max_minutes":
                    dy[key] = round(min(60, max(1, float(value))), 1)
                else:
                    dy[key] = int(value)
            ok = self._save_settings()
            return {"status": "ok", "data": {"settings": {k: dy.get(k) for k in self.DY_DEFAULTS}, "saved": ok}}
        except Exception as exc:
            logger.warning(f"[token_panel] 保存抖音设置失败: {exc}")
            return {"status": "error", "message": "保存失败"}

    async def api_dy_run(self) -> dict[str, Any]:
        """手动触发一次浏览（异步，不等待结果）。"""
        try:
            state = self._dy_state()
            if state.get("running"):
                return {"status": "ok", "data": {"started": False, "message": "上一次还没结束"}}
            cooling, left, until = self._dy_cooldown()
            if cooling:
                return {"status": "ok", "data": {
                    "started": False, "cooldown": True, "remaining": left,
                    "message": f"风控冷却中：还需 {left // 60} 分 {left % 60} 秒（{until} 之后才能再刷）"}}
            asyncio.create_task(self._dy_run_once(manual=True))
            return {"status": "ok", "data": {"started": True}}
        except Exception as exc:
            logger.warning(f"[token_panel] 手动抖音浏览失败: {exc}")
            return {"status": "error", "message": "启动失败"}

    async def api_dy_log(self) -> dict[str, Any]:
        """抖音浏览日志（最近在前）。"""
        try:
            items = self._dy_log_read()
            return {"status": "ok", "data": {"items": items[:50], "total": len(items)}}
        except Exception as exc:
            logger.warning(f"[token_panel] 读取抖音日志失败: {exc}")
            return {"status": "ok", "data": {"items": [], "total": 0}}

    # ------------------------------------------------------------------ #
    # 抖音风控调速（v1.5.0）
    # ------------------------------------------------------------------ #
    async def api_risk_status(self) -> dict[str, Any]:
        """风控调速实时状态：档位 / 分数 / 阶段 / 回升进度 / 生效参数。"""
        try:
            strict = self._risk_strictness()
            snap = self.risk.snapshot(strict)
            eff = self._dy_effective()
            state = self._dy_state()
            left_min = 0
            if eff.get("manual"):
                passed = time.time() - float(self._dy_manual_at or 0.0)
                left_min = max(0, int((float(self.DY_OVERRIDE_WINDOW) - passed) // 60))
            snap.update({
                "override": bool(eff.get("manual")),
                "override_minutes_left": left_min,
                "effective": {
                    "videos": eff.get("videos"),
                    "minutes": eff.get("minutes"),
                    "interval": eff.get("interval"),
                    "dwell_min": eff.get("dwell_min"),
                    "dwell_max": eff.get("dwell_max"),
                },
                "cooldown_until": str(state.get("risk_until") or ""),
                "risk_hits": int(state.get("risk_hits", 0) or 0),
            })
            return {"status": "ok", "data": snap}
        except Exception as exc:
            self._dbg(f"风控状态读取失败: {exc}")
            return {"status": "ok", "data": {"error": "读取风控状态失败", "detail": str(exc)[:160]}}

    async def api_risk_settings(self) -> dict[str, Any]:
        """保存「风控严格系数」（0.1~2.0，落 PLUTO panel 自己的 settings.json）。"""
        try:
            payload = await self._json_body()
        except Exception:
            payload = {}
        raw = payload.get("strictness") if isinstance(payload, dict) else None
        try:
            raw_value = float(str(raw).strip())
        except Exception:
            return {"status": "error", "message": "严格系数必须是 0.1 ~ 2.0 的数字"}
        if not (0.1 <= raw_value <= 2.0):
            return {"status": "error", "message": "严格系数必须在 0.1 ~ 2.0 之间"}
        value = round(raw_value, 1)
        self._settings["risk_strictness"] = value
        ok = self._save_settings()
        return {"status": "ok", "data": {"strictness": value, "saved": bool(ok)}}

    # ------------------------------------------------------------------ #
    # 精细化主动消息（v1.4.1）
    # ------------------------------------------------------------------ #
    def _proactive_cfg(self) -> dict[str, Any]:
        return self.proactive.settings()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """记录主人最近发言，供主动消息判断“对话优先”。

        只做一次轻量写入，不拦截消息；异常一律吞掉，绝不影响正常对话。
        """
        try:
            sender = str(event.get_sender_id() or "")
            admins = self._admins()
            if sender and admins and sender not in admins:
                return
            umo = event.unified_msg_origin or ""
            if not umo:
                return
            text = (event.message_str or "").strip()
            await self.proactive.on_inbound(umo, text)
        except Exception as exc:
            self._dbg(f"主动消息入站跟踪失败: {exc}")

    @filter.command("proactive")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_proactive(self, event: AstrMessageEvent):
        """开启/关闭精细化主动消息（仅管理员）"""
        cfg = self._proactive_cfg()
        now = not bool(cfg.get("enabled"))
        cfg["enabled"] = now
        self.proactive.save_settings()
        logger.info(f"[token_panel] 主动消息总开关 -> {now}")
        yield event.plain_result(
            f"主动消息已{'开启' if now else '关闭'}。\n"
            + ("它会按情绪等级与静默窗口，在你安静一段时间后偶尔主动找你；随时可以 /proactive 关掉。"
               if now else "已停止主动发送，不会再有主动消息。")
        )

    @filter.command("proactive-status")
    async def cmd_proactive_status(self, event: AstrMessageEvent):
        """查看主动消息状态"""
        try:
            yield event.plain_result(self.proactive.status_text())
        except Exception as exc:
            logger.warning(f"[token_panel] 读取主动消息状态失败: {exc}")
            yield event.plain_result("读取主动消息状态失败，稍后再试。")

    @filter.command("proactive-reset")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_proactive_reset(self, event: AstrMessageEvent):
        """重置当前会话的情绪等级、冷却与未回复惩罚（仅管理员）"""
        try:
            umo = event.unified_msg_origin or ""
            self.proactive.reset_session(umo)
            yield event.plain_result("已重置本会话的主动消息冷却与情绪惩罚，接下来按全局默认重新开始。")
        except Exception as exc:
            logger.warning(f"[token_panel] 重置主动消息失败: {exc}")
            yield event.plain_result("重置失败，详情见后台日志。")

    async def api_proactive_overview(self) -> dict[str, Any]:
        """主动消息总览：全局配置、会话参数、最近日志。"""
        try:
            data = self.proactive.overview()
            return {"status": "ok", "data": data}
        except Exception as exc:
            logger.warning(f"[token_panel] 读取主动消息总览失败: {exc}")
            return {"status": "ok", "data": {"settings": {}, "sessions": [], "log": [], "summary": {}}}

    async def api_proactive_settings(self) -> dict[str, Any]:
        """保存主动消息全局配置（只接受白名单字段）。"""
        try:
            body = await self._json_body()
            cfg = self._proactive_cfg()
            allowed = {
                "enabled", "private_enabled", "group_enabled", "adaptive", "soft_mode",
                "min_interval", "jitter", "daily_max", "burst_max", "penalty_mult",
                "idle_guard_seconds", "ignore_after_min", "quiet_windows", "active_windows",
                "weights",
            }
            bool_keys = {"enabled", "private_enabled", "group_enabled", "adaptive", "soft_mode"}
            num_ranges = {
                "min_interval": (0.5, 1440.0),
                "jitter": (0.0, 120.0),
                "penalty_mult": (1.0, 10.0),
            }
            int_ranges = {
                "daily_max": (0, 50),
                "burst_max": (1, 5),
                "idle_guard_seconds": (30, 3600),
                "ignore_after_min": (5, 1440),
            }
            for key, value in (body or {}).items():
                if key not in allowed:
                    continue
                if key in bool_keys:
                    cfg[key] = bool(value)
                elif key in {"quiet_windows", "active_windows"}:
                    spans = []
                    if isinstance(value, list):
                        for pair in value:
                            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                                spans.append([str(pair[0])[:5], str(pair[1])[:5]])
                    cfg[key] = spans
                elif key == "weights":
                    if isinstance(value, dict):
                        cfg[key] = {
                            str(k): max(0, min(100, int(v)))
                            for k, v in value.items()
                            if k in {"douyin", "search", "chat"}
                        }
                elif key in num_ranges:
                    low, high = num_ranges[key]
                    cfg[key] = round(max(low, min(high, float(value or 0))), 1)
                elif key in int_ranges:
                    low, high = int_ranges[key]
                    cfg[key] = int(max(low, min(high, int(value or 0))))
            ok = self.proactive.save_settings()
            return {"status": "ok", "data": {"settings": cfg, "saved": True if ok is None else bool(ok)}}
        except Exception as exc:
            logger.warning(f"[token_panel] 保存主动消息设置失败: {exc}")
            return {"status": "error", "message": "保存失败"}

    async def api_proactive_session(self) -> dict[str, Any]:
        """保存单会话参数；patch 里 None 表示继承全局。"""
        try:
            body = await self._json_body()
            umo = str((body or {}).get("umo") or "")
            patch = (body or {}).get("patch") or {}
            if not umo or not isinstance(patch, dict):
                return {"status": "error", "message": "参数不完整"}
            clean = {}
            for key in ("enabled", "min_interval", "jitter", "daily_max", "adaptive"):
                if key in patch:
                    value = patch[key]
                    clean[key] = None if value is None else (
                        bool(value) if key == "enabled" else float(value)
                    )
            row = self.proactive.upsert_session(umo, clean)
            return {"status": "ok", "data": {"session": row}}
        except Exception as exc:
            logger.warning(f"[token_panel] 保存会话主动消息参数失败: {exc}")
            return {"status": "error", "message": "保存失败"}

    async def api_proactive_log(self) -> dict[str, Any]:
        """主动消息发送日志（支持按 umo 过滤）。"""
        try:
            body = await self._json_body()
            umo = str((body or {}).get("umo") or "")
            limit = int((body or {}).get("limit") or 80)
            items = self.proactive.logs(umo=umo, limit=max(1, min(200, limit)))
            return {"status": "ok", "data": {"items": items, "total": len(items)}}
        except Exception as exc:
            logger.warning(f"[token_panel] 读取主动消息日志失败: {exc}")
            return {"status": "ok", "data": {"items": [], "total": 0}}

    async def api_proactive_clear(self) -> dict[str, Any]:
        """清空主动消息日志（可只清某个会话）。"""
        try:
            body = await self._json_body()
            umo = str((body or {}).get("umo") or "")
            removed = self.proactive.clear_logs(umo=umo)
            return {"status": "ok", "data": {"removed": removed}}
        except Exception as exc:
            logger.warning(f"[token_panel] 清空主动消息日志失败: {exc}")
            return {"status": "error", "message": "清空失败"}

    async def api_proactive_reset(self) -> dict[str, Any]:
        """重置指定会话的冷却与惩罚。"""
        try:
            body = await self._json_body()
            umo = str((body or {}).get("umo") or "")
            if not umo:
                return {"status": "error", "message": "缺少会话标识"}
            self.proactive.reset_session(umo)
            return {"status": "ok", "data": {"reset": True}}
        except Exception as exc:
            logger.warning(f"[token_panel] 重置主动消息失败: {exc}")
            return {"status": "error", "message": "重置失败"}
