# -*- coding: utf-8 -*-
"""精细化主动消息引擎（v1.4.1 内置，替代插件市场的独立主动消息插件）。

职责边界：只负责「什么时候发、发多少、发给谁、发了什么记录在案」，
内容一律由当前会话的模型依据人设即时生成，本模块不干预创作。

数据落在 data_dir/proactive.db，仅两张表：
  proactive_sessions —— 每会话独立参数（不设则继承全局）
  proactive_log      —— 主动消息日志（含主人后续回复摘要）
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

PROACTIVE_DEFAULTS: dict[str, Any] = {
    "enabled": False,                 # 总控
    "private_enabled": True,          # 私聊
    "group_enabled": False,           # 群聊
    "min_interval": 45.0,             # 分钟，最低 0.5
    "jitter": 10.0,                   # ± 分钟
    "quiet_windows": [["23:00", "07:00"]],
    "active_windows": [["09:00", "22:30"]],
    "weights": {"douyin": 40, "search": 30, "chat": 30},
    "daily_max": 6,                   # 单会话单日上限
    "burst_max": 1,                   # 单次调度最多连发（上限 1）
    "penalty_mult": 2.0,              # 被无视/敷衍后的冷却倍率
    "adaptive": True,                 # 反馈自适应
    "soft_mode": True,                # 柔和模式
    "idle_guard_seconds": 120,        # 对话优先：最近有人说话就不打扰
    "ignore_after_min": 30,           # 多久没回视为被无视
}

SOURCE_LABELS = {"douyin": "抖音视频摘要", "search": "搜索资讯", "chat": "自由闲聊"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_sessions(
    umo TEXT PRIMARY KEY,
    enabled INTEGER,
    min_interval REAL,
    jitter REAL,
    daily_max INTEGER,
    weights TEXT,
    cooldown_until TEXT,
    adaptive REAL DEFAULT 1.0,
    last_sent TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS proactive_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    ts TEXT NOT NULL,
    content TEXT,
    source TEXT,
    status TEXT,
    reply TEXT,
    reply_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_plog_umo_ts ON proactive_log(umo, ts);
"""


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if 0 <= hour <= 24 and 0 <= minute < 60:
        return hour % 24, minute
    return None


def _in_windows(now: datetime, windows: Any) -> bool:
    """now 是否落在 windows（[[\"23:00\",\"07:00\"], …]）内，支持跨零点。"""
    if not isinstance(windows, list):
        return False
    cur = now.hour * 60 + now.minute
    for pair in windows:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, end = _parse_hhmm(pair[0]), _parse_hhmm(pair[1])
        if not start or not end:
            continue
        s = start[0] * 60 + start[1]
        e = end[0] * 60 + end[1]
        if s == e:
            return True
        if s < e:
            if s <= cur < e:
                return True
        elif cur >= s or cur < e:   # 跨零点
            return True
    return False


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class ProactiveEngine:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.data_dir = plugin.data_dir
        self.db_path = self.data_dir / "proactive.db"
        self._inflight: set[str] = set()
        self._last_in: dict[str, datetime] = {}
        self._task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._init_db()

    # ------------------------------------------------------------------ #
    # 基础：数据库 / 设置
    # ------------------------------------------------------------------ #
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=8)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._conn() as conn:
                conn.executescript(SCHEMA)
        except Exception as exc:
            logger.warning(f"[token_panel] 主动消息数据库初始化失败: {exc}")

    def settings(self) -> dict[str, Any]:
        cfg = self.plugin._settings.get("proactive")
        if not isinstance(cfg, dict):
            cfg = {}
            self.plugin._settings["proactive"] = cfg
        for key, value in PROACTIVE_DEFAULTS.items():
            cfg.setdefault(key, value)
        return cfg

    def save_settings(self) -> None:
        try:
            self.plugin._save_settings()
        except Exception as exc:
            logger.warning(f"[token_panel] 保存主动消息配置失败: {exc}")

    # ------------------------------------------------------------------ #
    # 会话参数
    # ------------------------------------------------------------------ #
    def session_row(self, umo: str) -> dict[str, Any]:
        row: dict[str, Any] = {
            "umo": umo, "enabled": None, "min_interval": None, "jitter": None,
            "daily_max": None, "weights": None, "cooldown_until": "",
            "adaptive": 1.0, "last_sent": "",
        }
        try:
            with self._conn() as conn:
                cur = conn.execute("SELECT * FROM proactive_sessions WHERE umo=?", (umo,))
                got = cur.fetchone()
            if got:
                row.update({k: got[k] for k in got.keys() if k != "weights"})
                raw = got["weights"]
                row["weights"] = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
        except Exception as exc:
            logger.warning(f"[token_panel] 读取主动消息会话参数失败: {exc}")
        return row

    def all_sessions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            with self._conn() as conn:
                cur = conn.execute("SELECT * FROM proactive_sessions ORDER BY updated_at DESC")
                for got in cur.fetchall():
                    item = {k: got[k] for k in got.keys() if k != "weights"}
                    raw = got["weights"]
                    item["weights"] = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
                    rows.append(item)
        except Exception as exc:
            logger.warning(f"[token_panel] 列取主动消息会话失败: {exc}")
        return rows

    def upsert_session(self, umo: str, patch: dict[str, Any]) -> dict[str, Any]:
        """只写显式提供的字段；None 表示继承全局。"""
        row = self.session_row(umo)
        row.update({k: v for k, v in patch.items() if k in row or k == "weights"})
        weights = row.get("weights")
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO proactive_sessions
                       (umo, enabled, min_interval, jitter, daily_max, weights,
                        cooldown_until, adaptive, last_sent, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(umo) DO UPDATE SET
                         enabled=excluded.enabled, min_interval=excluded.min_interval,
                         jitter=excluded.jitter, daily_max=excluded.daily_max,
                         weights=excluded.weights, cooldown_until=excluded.cooldown_until,
                         adaptive=excluded.adaptive, last_sent=excluded.last_sent,
                         updated_at=excluded.updated_at""",
                    (
                        umo,
                        None if row.get("enabled") is None else int(bool(row["enabled"])),
                        row.get("min_interval"),
                        row.get("jitter"),
                        row.get("daily_max"),
                        json.dumps(weights, ensure_ascii=False) if weights else None,
                        row.get("cooldown_until") or "",
                        float(row.get("adaptive") or 1.0),
                        row.get("last_sent") or "",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
        except Exception as exc:
            logger.warning(f"[token_panel] 写入主动消息会话失败: {exc}")
        return self.session_row(umo)

    def delete_session(self, umo: str) -> None:
        try:
            with self._conn() as conn:
                conn.execute("DELETE FROM proactive_sessions WHERE umo=?", (umo,))
        except Exception as exc:
            logger.warning(f"[token_panel] 删除主动消息会话失败: {exc}")

    def reset_session(self, umo: str) -> dict[str, Any]:
        """清冷却惩罚 + 自适应倍率 + 权重偏移，回到继承全局的干净状态。"""
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE proactive_sessions SET cooldown_until='', adaptive=1.0, "
                    "weights=NULL, updated_at=? WHERE umo=?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), umo),
                )
        except Exception as exc:
            logger.warning(f"[token_panel] 重置主动消息会话失败: {exc}")
        return self.session_row(umo)

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def add_log(self, umo: str, content: str, source: str, status: str = "sent") -> int:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO proactive_log(umo, ts, content, source, status, reply, reply_ts)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (umo, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     (content or "")[:400], source, status, "", ""),
                )
                return int(cur.lastrowid or 0)
        except Exception as exc:
            logger.warning(f"[token_panel] 写主动消息日志失败: {exc}")
            return 0

    def logs(self, umo: str = "", limit: int = 80) -> list[dict[str, Any]]:
        try:
            with self._conn() as conn:
                if umo:
                    cur = conn.execute(
                        "SELECT * FROM proactive_log WHERE umo=? ORDER BY id DESC LIMIT ?",
                        (umo, limit))
                else:
                    cur = conn.execute(
                        "SELECT * FROM proactive_log ORDER BY id DESC LIMIT ?", (limit,))
                return [dict(row) for row in cur.fetchall()]
        except Exception as exc:
            logger.warning(f"[token_panel] 读取主动消息日志失败: {exc}")
            return []

    def clear_logs(self, umo: str = "") -> int:
        try:
            with self._conn() as conn:
                if umo:
                    cur = conn.execute("DELETE FROM proactive_log WHERE umo=?", (umo,))
                else:
                    cur = conn.execute("DELETE FROM proactive_log")
                return int(cur.rowcount or 0)
        except Exception as exc:
            logger.warning(f"[token_panel] 清空主动消息日志失败: {exc}")
            return 0

    def count_today(self, umo: str) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM proactive_log WHERE umo=? AND status='sent' AND ts LIKE ?",
                    (umo, f"{today}%"))
                return int(cur.fetchone()[0])
        except Exception:
            return 0

    def _last_sent_at(self, umo: str) -> datetime | None:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT ts FROM proactive_log WHERE umo=? AND status='sent' ORDER BY id DESC LIMIT 1",
                    (umo,))
                got = cur.fetchone()
            if got:
                return datetime.strptime(got["ts"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ #
    # 目标会话
    # ------------------------------------------------------------------ #
    async def _targets(self) -> list[str]:
        cfg = self.settings()
        found: list[str] = []
        try:
            cm = self.plugin.context.conversation_manager
            rows, _total = await cm.get_filtered_conversations(page=1, page_size=200)
        except Exception:
            rows = []
        for row in rows or []:
            umo = str(getattr(row, "user_id", "") or "")
            if not umo or ":" not in umo:
                continue
            kind = "group" if "GroupMessage" in umo else "friend"
            if kind == "group" and not cfg.get("group_enabled"):
                continue
            if kind == "friend" and not cfg.get("private_enabled"):
                continue
            if umo not in found:
                found.append(umo)
        for item in self.all_sessions():
            umo = str(item.get("umo") or "")
            if umo and item.get("enabled") and umo not in found:
                found.append(umo)
        # 显式 enabled=0 的会话剔除
        enabled_map = {str(i.get("umo")): i.get("enabled") for i in self.all_sessions()}
        return [u for u in found if enabled_map.get(u) is not False]

    # ------------------------------------------------------------------ #
    # 素材
    # ------------------------------------------------------------------ #
    def _douyin_material(self) -> str:
        try:
            entries = self.plugin._dy_log_read()[:5]
        except Exception:
            entries = []
        titles: list[str] = []
        for entry in entries:
            for item in (entry.get("items") or [])[:4]:
                title = re.sub(r"\s*@.+$", "", str(item.get("title") or "")).strip()
                if title:
                    titles.append(title[:60])
        return "；".join(titles[:6])

    def _search_material(self) -> str:
        path = self.plugin.NET_LOG_DB
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT query FROM search_log ORDER BY id DESC LIMIT 20").fetchall()
            finally:
                conn.close()
        except Exception:
            return ""
        queries = [str(r[0]).strip() for r in rows if r and str(r[0]).strip()]
        return "；".join(dict.fromkeys(queries[:8]))

    def _material(self, source: str) -> str:
        if source == "douyin":
            return self._douyin_material()
        if source == "search":
            return self._search_material()
        return ""

    # ------------------------------------------------------------------ #
    # 生成与发送
    # ------------------------------------------------------------------ #
    async def _persona_prompt(self, umo: str) -> str:
        try:
            pm = getattr(self.plugin.context, "persona_manager", None)
            if pm is None:
                return ""
            persona = await pm.get_default_persona_v3(umo)
            if isinstance(persona, dict):
                return str(persona.get("prompt") or "")
            return str(getattr(persona, "prompt", "") or "")
        except Exception:
            return ""

    async def _compose(self, umo: str, source: str, soft: bool) -> str:
        material = self._material(source)
        label = SOURCE_LABELS.get(source, "自由闲聊")
        persona = await self._persona_prompt(umo)
        rules = [
            "你现在要主动给主人发一条消息（不是回复，是主动搭话）。",
            f"素材类型：{label}。",
        ]
        if material:
            rules.append(f"可用素材（只能基于它说，不许编造）：{material}")
        else:
            rules.append("没有外部素材，就聊聊日常、关心一下主人最近的状态。")
        rules.append("要求：一句话到两句话，口语化，不超过60字，不要表情包，不要列表，不要复述素材全文，不要提到“素材”“搜索”“抖音”这些词。")
        if soft:
            rules.append("语气要柔和、自然，像熟人随口一提，避免突兀和过度热情。")
        prompt = "\n".join(rules)
        try:
            provider = await self.plugin.context.get_using_provider_async(umo=umo)
            if provider is None:
                return ""
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=persona, contexts=[]),
                timeout=40,
            )
            text = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning(f"[token_panel] 主动消息生成失败: {exc}")
            return ""
        text = re.sub(r"^[\"“'‘]+|[\"”'’]+$", "", text).strip()
        text = text.replace("\n", " ").strip()
        return text[:120]

    async def _send(self, umo: str, text: str) -> bool:
        try:
            await self.plugin.context.send_message(umo, MessageChain().message(text))
            return True
        except Exception as exc:
            logger.warning(f"[token_panel] 主动消息发送失败 {umo}: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #
    def _effective(self, umo: str) -> dict[str, Any]:
        cfg = self.settings()
        row = self.session_row(umo)
        adaptive = float(row.get("adaptive") or 1.0)
        base = float(row.get("min_interval") or cfg.get("min_interval") or 45.0)
        interval = _clamp(base * adaptive, 0.5, 24 * 60)
        weights = row.get("weights") or cfg.get("weights") or {"chat": 100}
        if not isinstance(weights, dict):
            weights = {"chat": 100}
        total = sum(max(0.0, float(weights.get(k) or 0)) for k in ("douyin", "search", "chat"))
        if total <= 0:
            weights, total = {"chat": 100}, 100.0
        return {
            "umo": umo,
            "interval": interval,
            "jitter": float(row.get("jitter") if row.get("jitter") is not None else cfg.get("jitter") or 0),
            "daily_max": int(row.get("daily_max") or cfg.get("daily_max") or 6),
            "weights": weights,
            "total": total,
            "adaptive": adaptive,
            "cooldown_until": str(row.get("cooldown_until") or ""),
        }

    def _cooling(self, eff: dict[str, Any]) -> tuple[bool, int]:
        raw = eff.get("cooldown_until") or ""
        if not raw:
            return False, 0
        try:
            until = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return False, 0
        left = int((until - datetime.now()).total_seconds())
        return (True, left) if left > 0 else (False, 0)

    async def _maybe_send(self, umo: str, cfg: dict[str, Any], now: datetime) -> None:
        eff = self._effective(umo)
        cooling, _left = self._cooling(eff)
        if cooling:
            return
        last_in = self._last_in.get(umo)
        guard = int(cfg.get("idle_guard_seconds") or 120)
        if last_in and (now - last_in).total_seconds() < guard:
            return
        if self.count_today(umo) >= eff["daily_max"]:
            return
        last_sent = self._last_sent_at(umo)
        wait = eff["interval"]
        jitter = eff["jitter"]
        if jitter:
            wait += random.uniform(-abs(jitter), abs(jitter))
        wait = max(0.5, wait)
        if last_sent and (now - last_sent).total_seconds() < wait * 60:
            return
        if umo in self._inflight:
            return
        self._inflight.add(umo)
        try:
            burst = max(1, min(1, int(cfg.get("burst_max") or 1)))
            for index in range(burst):
                if index:
                    await asyncio.sleep(12)
                src = random.choices(
                    ["douyin", "search", "chat"],
                    weights=[max(0.0, float(eff["weights"].get(k) or 0)) for k in ("douyin", "search", "chat")],
                    k=1,
                )[0]
                text = await self._compose(umo, src, bool(cfg.get("soft_mode", True)))
                if not text:
                    continue
                if await self._send(umo, text):
                    self.add_log(umo, text, SOURCE_LABELS[src], "sent")
                    logger.info(f"[token_panel] 主动消息已发送 {umo} 素材={src} 长度={len(text)}")
        finally:
            self._inflight.discard(umo)

    async def tick(self) -> None:
        cfg = self.settings()
        if not cfg.get("enabled"):
            return
        now = datetime.now()
        if _in_windows(now, cfg.get("quiet_windows")):
            return
        active = cfg.get("active_windows")
        if isinstance(active, list) and active and not _in_windows(now, active):
            return
        try:
            targets = await self._targets()
        except Exception as exc:
            logger.warning(f"[token_panel] 主动消息目标解析失败: {exc}")
            return
        for umo in targets[:30]:
            try:
                await self._maybe_send(umo, cfg, now)
            except Exception as exc:
                logger.warning(f"[token_panel] 主动消息调度异常 {umo}: {exc}")

    async def loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning(f"[token_panel] 主动消息循环异常: {exc}")
            await asyncio.sleep(45)

    async def watchdog_loop(self) -> None:
        """每 5 分钟：超时未回的主动消息标记为“被无视”，并按倍率加冷却。"""
        await asyncio.sleep(300)
        while True:
            try:
                await self._sweep_ignored()
            except Exception as exc:
                logger.warning(f"[token_panel] 主动消息巡检异常: {exc}")
            await asyncio.sleep(300)

    async def _sweep_ignored(self) -> None:
        cfg = self.settings()
        limit_min = int(cfg.get("ignore_after_min") or 30)
        cutoff = datetime.now() - timedelta(minutes=limit_min)
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, umo, ts, source FROM proactive_log WHERE status='sent'").fetchall()
        except Exception:
            return
        for row in rows:
            try:
                sent = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if sent > cutoff:
                continue
            try:
                with self._conn() as conn:
                    conn.execute("UPDATE proactive_log SET status='ignored' WHERE id=?", (row["id"],))
            except Exception:
                continue
            if cfg.get("adaptive"):
                self._apply_penalty(str(row["umo"]), str(row["source"] or ""), cold=True, penalty_only=True)

    def _apply_penalty(self, umo: str, source_label: str, cold: bool, penalty_only: bool = False) -> None:
        cfg = self.settings()
        row = self.session_row(umo)
        adaptive = float(row.get("adaptive") or 1.0)
        weights = dict(row.get("weights") or cfg.get("weights") or {"douyin": 40, "search": 30, "chat": 30})
        key = next((k for k, v in SOURCE_LABELS.items() if v == source_label), "")
        if not penalty_only and cold:
            adaptive = _clamp(adaptive * 1.15, 0.4, 3.0)
            if key:
                weights[key] = _clamp(float(weights.get(key) or 0) - 5, 5, 70)
        elif not penalty_only:
            adaptive = _clamp(adaptive * 0.9, 0.4, 3.0)
            if key:
                weights[key] = _clamp(float(weights.get(key) or 0) + 5, 5, 70)
        eff_base = float(row.get("min_interval") or cfg.get("min_interval") or 45.0)
        mult = float(cfg.get("penalty_mult") or 2.0)
        until = (datetime.now() + timedelta(minutes=max(1.0, eff_base * mult))).strftime("%Y-%m-%d %H:%M:%S")
        self.upsert_session(umo, {
            "adaptive": adaptive,
            "weights": weights,
            "cooldown_until": until,
        })

    # ------------------------------------------------------------------ #
    # 主人回复联动
    # ------------------------------------------------------------------ #
    async def on_inbound(self, umo: str, text: str) -> None:
        self._last_in[umo] = datetime.now()
        try:
            with self._conn() as conn:
                got = conn.execute(
                    "SELECT * FROM proactive_log WHERE umo=? AND status='sent'"
                    " ORDER BY id DESC LIMIT 1", (umo,)).fetchone()
        except Exception:
            return
        if not got:
            return
        try:
            sent = datetime.strptime(got["ts"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            sent = datetime.now()
        if (datetime.now() - sent).total_seconds() > 6 * 3600:
            return
        reply = re.sub(r"\s+", " ", (text or "").strip())[:60]
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE proactive_log SET status='replied', reply=?, reply_ts=? WHERE id=?",
                    (reply, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), got["id"]))
        except Exception as exc:
            logger.warning(f"[token_panel] 更新主动消息回复失败: {exc}")
        if self.settings().get("adaptive") and reply:
            asyncio.create_task(self._adapt(umo, reply, str(got["source"] or "")))

    async def _adapt(self, umo: str, reply: str, source_label: str) -> None:
        attitude = self._heuristic_attitude(reply)
        try:
            provider = await self.plugin.context.get_using_provider_async(umo=umo)
            if provider is not None:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=(
                            "判断主人对这条主动消息的反应属于哪类，只回一个词：\n"
                            "感兴趣（认真回应、接话、追问）／敷衍（嗯、哦、哈哈、随便）／冷淡（不接话、转移话题、明显不想聊）。\n"
                            f"主人的回复：{reply}"
                        ),
                        system_prompt="你是极简分类器，只输出一个词。",
                        contexts=[],
                    ),
                    timeout=20,
                )
                raw = str(getattr(resp, "completion_text", "") or "")
                if "感" in raw:
                    attitude = "interested"
                elif "敷衍" in raw:
                    attitude = "cold"
                elif "冷" in raw:
                    attitude = "cold"
        except Exception:
            pass
        if attitude == "interested":
            self._apply_penalty(umo, source_label, cold=False)
        elif attitude == "cold":
            self._apply_penalty(umo, source_label, cold=True)

    @staticmethod
    def _heuristic_attitude(reply: str) -> str:
        text = (reply or "").strip()
        if len(text) >= 12 or any(k in text for k in ("为什么", "怎么", "真的", "然后", "？", "?")):
            return "interested"
        if len(text) <= 6:
            return "cold"
        return "neutral"

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def overview(self) -> dict[str, Any]:
        cfg = self.settings()
        sessions = []
        for item in self.all_sessions():
            umo = str(item.get("umo") or "")
            eff = self._effective(umo)
            cooling, left = self._cooling(eff)
            sessions.append({
                "umo": umo,
                "label": self._label(umo),
                "enabled": item.get("enabled"),
                "min_interval": item.get("min_interval"),
                "jitter": item.get("jitter"),
                "daily_max": item.get("daily_max"),
                "weights": item.get("weights"),
                "adaptive": round(float(item.get("adaptive") or 1.0), 2),
                "cooldown_until": item.get("cooldown_until") or "",
                "cooling": cooling,
                "cooling_left": left,
                "today": self.count_today(umo),
                "effective_interval": round(eff["interval"], 1),
            })
        logs = self.logs("", 60)
        for entry in logs:
            entry["label"] = self._label(str(entry.get("umo") or ""))
        return {
            "settings": cfg,
            "sessions": sessions,
            "logs": logs,
            "today_total": sum(s["today"] for s in sessions) if sessions else 0,
        }

    @staticmethod
    def _label(umo: str) -> str:
        parts = [p for p in (umo or "").split(":") if p]
        kind = "群" if "GroupMessage" in umo else "私聊"
        who = parts[-1] if parts else umo
        return f"{kind} {who}"

    def status_text(self) -> str:
        cfg = self.settings()
        lines = [
            "【主动消息状态】",
            f"总开关：{'已开启' if cfg.get('enabled') else '已关闭'}"
            f"（私聊{'开' if cfg.get('private_enabled') else '关'} / 群聊{'开' if cfg.get('group_enabled') else '关'}）",
            f"基础间隔：{cfg.get('min_interval')} 分钟（±{cfg.get('jitter')}）"
            f"｜单会话每日上限：{cfg.get('daily_max')} 条",
            f"允许发送时段：{self._fmt_windows(cfg.get('active_windows'))}",
            f"免打扰：{self._fmt_windows(cfg.get('quiet_windows'))}",
            f"反馈自适应：{'开' if cfg.get('adaptive') else '关'}｜柔和模式：{'开' if cfg.get('soft_mode') else '关'}",
        ]
        sessions = self.all_sessions()
        if not sessions:
            lines.append("会话独立配置：暂无（全部继承全局）")
        else:
            lines.append("会话独立配置：")
            for item in sessions[:10]:
                umo = str(item.get("umo") or "")
                eff = self._effective(umo)
                cooling, left = self._cooling(eff)
                cool_text = f"冷却剩余 {left // 60} 分 {left % 60} 秒" if cooling else "冷却 —"
                lines.append(
                    f"· {self._label(umo)}：间隔 {round(eff['interval'], 1)} 分钟"
                    f"｜今日 {self.count_today(umo)}/{eff['daily_max']}｜{cool_text}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_windows(windows: Any) -> str:
        if not isinstance(windows, list) or not windows:
            return "不限"
        spans = []
        for pair in windows:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                spans.append(f"{pair[0]}-{pair[1]}")
        return "、".join(spans) if spans else "不限"