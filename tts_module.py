# -*- coding: utf-8 -*-
"""PLUTO panel · TTS 语音合成模块（v1.7.1 新增，独立文件）

设计原则
--------
1. 只做「语音合成 + 语音追加」，不封装任何 TTS 接口：全部调用 AstrBot 原生
   `context.get_using_tts_provider_async()` 取到的 TTSProvider，由框架决定
   用哪家（dashscope / edge / azure / openai …）。
2. 与原有模块完全隔离：不读 notify 的 tts 字段、不碰 proactive / diary /
   token 统计，只读写自己的 `tts` 配置段与自己的统计文件。
3. 自然语言触发优先于自动开关：命中关键词 → 本轮强制带语音，仅本轮生效。
4. 触发词在送模型前从 prompt 里剥掉，避免污染上下文。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 配置默认值

TTS_DEFAULTS: dict[str, Any] = {
    "enabled": False,          # 全局语音总开关
    "private_enabled": True,   # 私聊自动带语音
    "group_enabled": False,    # 群聊自动带语音
    "send_both": True,         # True=文字+语音，False=只发语音
    "voice_id": "",            # 空=用框架当前音色
    "speed": 1.0,              # 0.5 ~ 2.0（提供商支持时才生效）
    "max_chars": 180,          # 单段语音最大字数（超长自动分段）
    "max_segments": 6,         # 单条回复最多合成几段，防刷屏
}

# 自然语言触发词（子串匹配，命中任意一个即强制语音）
TRIGGER_WORDS: tuple[str, ...] = ("发语音", "语音说", "用语音回答")

# 单次最多合成字符数，超过则截断（防止极端长文合成失败）
HARD_CHAR_CAP = 1200

# 表情 / 符号清洗：保留中英文、数字与常用标点
_KEEP_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9，。！？、；：,.!?;:\s（）()\-—]")
_WS_RE = re.compile(r"\s+")
_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")


def clean_for_tts(text: str) -> str:
    """过滤 emoji、Markdown 符号与不可读字符，避免 TTS 合成失败。"""
    if not text:
        return ""
    text = _KEEP_RE.sub("", str(text))
    text = _WS_RE.sub(" ", text)
    return text.strip()


# Split long replies into TTS-sized chunks on sentence boundaries (no mid-word cuts).
def split_for_tts(text: str, max_chars: int, max_segments: int) -> list[str]:
    """按句读切分并合并成若干段，每段不超过 max_chars。"""
    text = (text or "").strip()
    if not text:
        return []
    max_chars = max(40, min(600, int(max_chars or 180)))
    pieces: list[str] = []
    for sent in _SPLIT_RE.split(text):
        sent = sent.strip()
        if not sent:
            continue
        while len(sent) > max_chars:
            pieces.append(sent[:max_chars])
            sent = sent[max_chars:]
        if sent:
            pieces.append(sent)

    merged: list[str] = []
    buf = ""
    for piece in pieces:
        if not buf:
            buf = piece
        elif len(buf) + len(piece) <= max_chars:
            buf += piece
        else:
            merged.append(buf)
            buf = piece
    if buf:
        merged.append(buf)
    return merged[: max(1, min(12, int(max_segments or 6)))]


def has_trigger(text: str) -> bool:
    """用户这句话里是否含自然语言语音触发词。"""
    raw = str(text or "")
    return any(word in raw for word in TRIGGER_WORDS)


def strip_trigger(text: str) -> str:
    """把触发词及其常见搭配从文本里去掉，保持语句可读。"""
    out = str(text or "")
    for word in ("用语音回答", "发语音", "语音说"):
        out = out.replace(word, "")
    out = re.sub(r"[，,]\s*[，,]", "，", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" ，,。.！!？?\n\t")


class TtsService:
    """TTS 语音服务：配置、触发判断、合成与追加语音段。"""

    def __init__(
        self,
        context: Any,
        settings_getter,
        settings_saver,
        debug_logger,
        data_dir: Path,
    ) -> None:
        self.context = context
        self._settings = settings_getter
        self._save_settings = settings_saver
        self._dbg = debug_logger
        self.data_dir = Path(data_dir)
        self.stats_path = self.data_dir / "tts_stats.json"
        self._lock = asyncio.Lock()  # 同一时刻只改一个 provider 的音色/语速

    # ------------------------------------------------------------ 配置读写

    def cfg(self) -> dict[str, Any]:
        cfg = dict(TTS_DEFAULTS)
        try:
            raw = self._settings().get("tts") or {}
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in TTS_DEFAULTS})
        except Exception as exc:
            self._dbg(f"读取 TTS 配置失败: {exc}")
        return cfg

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """合并写入 tts 段，做类型与范围校验。"""
        cfg = self.cfg()
        for key, value in (patch or {}).items():
            if key not in TTS_DEFAULTS:
                continue
            if key in ("enabled", "private_enabled", "group_enabled", "send_both"):
                cfg[key] = bool(value)
            elif key in ("max_chars", "max_segments"):
                try:
                    cfg[key] = max(40, min(600, int(value))) if key == "max_chars" else max(1, min(12, int(value)))
                except (TypeError, ValueError):
                    pass
            elif key == "speed":
                try:
                    cfg[key] = round(max(0.5, min(2.0, float(value))), 2)
                except (TypeError, ValueError):
                    pass
            elif key == "voice_id":
                cfg[key] = str(value or "").strip()[:80]
        return cfg

    def save(self, patch: dict[str, Any]) -> dict[str, Any]:
        cfg = self.update(patch)
        try:
            self._save_settings({"tts": cfg})
        except Exception as exc:
            self._dbg(f"保存 TTS 配置失败: {exc}")
        return cfg

    # ------------------------------------------------------------ 运行时判断

    @staticmethod
    def _scope(event: Any) -> str:
        """private / group / other。"""
        try:
            if event.get_group_id():
                return "group"
            return "private"
        except Exception:
            return "other"

    def should_voice(self, event: Any, forced: bool) -> bool:
        """本轮是否要附带语音：自然语言触发优先，其次看自动开关。"""
        if forced:
            return True
        cfg = self.cfg()
        if not cfg.get("enabled"):
            return False
        scope = self._scope(event)
        if scope == "private":
            return bool(cfg.get("private_enabled"))
        if scope == "group":
            return bool(cfg.get("group_enabled"))
        return False

    @staticmethod
    def is_command(event: Any) -> bool:
        """指令消息（/xxx）不合成语音：指令反馈保持纯文字。"""
        try:
            text = (event.message_str or "").strip()
        except Exception:
            return False
        return text.startswith("/") or text.startswith("／") or text.startswith("#")

    # ------------------------------------------------------------ 合成

    async def _provider(self, event: Any):
        try:
            return await self.context.get_using_tts_provider_async(
                umo=getattr(event, "unified_msg_origin", None)
            )
        except Exception as exc:
            self._dbg(f"获取 TTS 提供商失败: {exc}")
            return None

    async def _synth_one(self, provider: Any, text: str, cfg: dict[str, Any]) -> str:
        """调原生 provider 合成一段，返回音频路径（失败返回空串）。"""
        voice = str(cfg.get("voice_id") or "").strip()
        speed = float(cfg.get("speed") or 1.0)
        async with self._lock:
            # 临时覆盖音色/语速：不同 provider 字段名不一样，存在才改，用完还原
            backup: dict[str, Any] = {}
            for attr, value in (("voice", voice), ("rate", f"+{round((speed - 1) * 100)}%")):
                if value and hasattr(provider, attr):
                    backup[attr] = getattr(provider, attr)
                    try:
                        setattr(provider, attr, value)
                    except Exception:
                        backup.pop(attr, None)
            if speed != 1.0 and hasattr(provider, "speed_ratio"):
                backup["speed_ratio"] = getattr(provider, "speed_ratio")
                try:
                    setattr(provider, "speed_ratio", speed)
                except Exception:
                    backup.pop("speed_ratio", None)
            try:
                return await provider.get_audio(text) or ""
            finally:
                for attr, old in backup.items():
                    try:
                        setattr(provider, attr, old)
                    except Exception:
                        pass

    async def synthesize(self, event: Any, text: str) -> list[str]:
        """把一段回复合成为若干音频文件路径。"""
        cfg = self.cfg()
        clean = clean_for_tts(text)[:HARD_CHAR_CAP]
        if not clean:
            return []
        provider = await self._provider(event)
        if provider is None:
            self._dbg("没有可用的 TTS 提供商，跳过语音合成")
            return []
        paths: list[str] = []
        for seg in split_for_tts(clean, cfg.get("max_chars", 180), cfg.get("max_segments", 6)):
            try:
                path = await self._synth_one(provider, seg, cfg)
            except Exception as exc:
                self._dbg(f"语音合成失败: {exc}")
                continue
            if path:
                paths.append(path)
        self._bump_stats(len(paths), len(clean))
        if paths:
            try:
                from .eventlog import log_event
                log_event("信息", "系统", "语音", f"合成语音 {len(paths)} 段 / {len(clean)} 字")
            except Exception:
                pass
        return paths

    # ------------------------------------------------------------ 统计

    def _bump_stats(self, segments: int, chars: int) -> None:
        try:
            data = {"total": 0, "segments": 0, "chars": 0, "last_at": ""}
            if self.stats_path.exists():
                data.update(json.loads(self.stats_path.read_text(encoding="utf-8")))
            data["total"] = int(data.get("total") or 0) + 1
            data["segments"] = int(data.get("segments") or 0) + segments
            data["chars"] = int(data.get("chars") or 0) + chars
            data["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.stats_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self._dbg(f"写语音统计失败: {exc}")

    def stats(self) -> dict[str, Any]:
        try:
            if self.stats_path.exists():
                return json.loads(self.stats_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"total": 0, "segments": 0, "chars": 0, "last_at": ""}