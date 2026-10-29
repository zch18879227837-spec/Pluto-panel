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

PLUGIN_NAME = "astrbot_plugin_token_panel"
PLUGIN_VERSION = "1.3.1"
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

    async def _model_limit(self, umo: str) -> tuple[str | None, int | None, str]:
        """返回 (模型名, 上下文窗口上限, 来源)。读不到就给 (None, None, 'unknown')。"""
        now = time.time()
        cached = self._limit_cache.get(umo)
        if cached and cached[3] > now:
            return cached[0], cached[1], cached[2]

        model: str | None = None
        limit: int | None = None
        source = "unknown"
        try:
            provider = None
            try:
                provider = await self.context.get_using_provider_async(umo=umo)
            except Exception as exc:
                self._dbg(f"异步取 provider 失败: {exc}")
            if provider is None:
                try:
                    provider = self.context.get_using_provider(umo=umo)
                except Exception as exc:
                    self._dbg(f"同步取 provider 失败: {exc}")
            if provider is not None:
                try:
                    model = provider.get_model()
                except Exception:
                    model = getattr(provider, "model", None)
                provider_config = getattr(provider, "provider_config", {}) or {}
                try:
                    limit = int(provider_config.get("max_context_tokens", 0) or 0)
                except (TypeError, ValueError):
                    limit = 0
                if limit and limit > 0:
                    source = "provider"
                else:
                    limit = None
                    if model:
                        try:
                            from astrbot.core.utils.llm_metadata import LLM_METADATAS

                            info = LLM_METADATAS.get(model)
                            if info and isinstance(info.get("limit"), dict):
                                limit = int(info["limit"].get("context") or 0) or None
                                if limit:
                                    source = "metadata"
                        except Exception as exc:
                            self._dbg(f"读取模型元数据失败: {exc}")
        except Exception as exc:
            self._dbg(f"解析模型窗口失败: {exc}")

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
        if not conv or not getattr(conv, "history", None):
            return cid, []
        try:
            data = json.loads(conv.history)
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

    async def _resolve_ctx_limit(self, umo: str) -> tuple[int | None, str]:
        """模型窗口上限：Provider 运行时 > models.dev 元数据 > 失败（与 /ctx 原版一致）。"""
        provider = await self._provider_for(umo)
        if provider is None:
            return None, "unknown"
        try:
            provider_config = getattr(provider, "provider_config", None) or {}
            value = int(provider_config.get("max_context_tokens", 0) or 0)
            if value > 0:
                return value, "provider"
        except Exception as exc:
            self._dbg(f"读取 provider 上限失败: {exc}")
        model = _provider_model_name(provider)
        if model:
            try:
                from astrbot.core.utils.llm_metadata import LLM_METADATAS

                info = LLM_METADATAS.get(model)
                if isinstance(info, dict):
                    limit_info = info.get("limit") or {}
                    value = int(limit_info.get("context") or 0)
                    if value > 0:
                        return value, "metadata"
            except Exception as exc:
                self._dbg(f"读取模型元数据失败: {exc}")
        return None, "unknown"

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
            register(f"{PAGE_PREFIX}/stats", self.api_stats, ["POST"], "会话上下文统计")
            register(f"{PAGE_PREFIX}/compress", self.api_compress, ["POST"], "压缩会话上下文")
            register(f"{PAGE_PREFIX}/continue", self.api_continue, ["POST"], "生成承接摘要并换会话")
            register(f"{PAGE_PREFIX}/carry", self.api_carry, ["GET", "POST"], "压缩承接设置")
            register(f"{PAGE_PREFIX}/compress-log", self.api_compress_log, ["GET"], "压缩日志")
            register(f"{PAGE_PREFIX}/backup-preview", self.api_backup_preview, ["POST"], "备份预览")
            logger.info("[token_panel] 面板接口注册完成")
        except Exception as exc:
            logger.error(f"[token_panel] 面板接口注册失败: {exc}")
        # 不能在这里 await 插件管理锁：重载本插件时锁还在自己手里，会死锁。
        # 交给后台任务，等加载/重载释放锁后再处理旧插件。
        try:
            asyncio.create_task(self._disable_legacy_plugins())
        except Exception as exc:
            logger.warning(f"[token_panel] 启动旧插件禁用任务失败: {exc}")

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
        """面板总览：会话列表 + 设置 + 预警线。"""
        try:
            rows = await self._fetch_rows()
            admins = self._admins()
            sessions: list[dict[str, Any]] = []
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
                    model, limit, source = await self._model_limit(umo)
                    percent = round(used / limit * 100.0, 1) if limit else None
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
                    "carry": dict(self.carry),
                    "limits": {k: list(v) for k, v in CARRY_LIMITS.items()},
                    "backup_limit": CARRY_BACKUP_LIMIT,
                    "log_limit": CARRY_LOG_LIMIT,
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
            logger.info(f"[token_panel] 面板清空会话 {cid[:8]}")
            return {"status": "ok", "data": {"cid": cid}}
        except Exception as exc:
            logger.warning(f"[token_panel] 面板清空失败: {exc}")
            return {"status": "error", "message": "清空失败，请稍后再试"}

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
            limit, source = await self._resolve_ctx_limit(umo)
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
