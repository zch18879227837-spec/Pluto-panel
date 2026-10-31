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
import uuid
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
    "idle_guard_seconds": 600,        # 对话优先：最近有人说话就不打扰（最短 10 分钟）
    "ignore_after_min": 30,           # 多久没回视为被无视
    "review": True,                   # 发送前自检（先过脑子，不合格就不发）
    "dialogue_lines": 10,             # 生成时带上最近几条真实对话做上下文
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
CREATE TABLE IF NOT EXISTS proactive_used(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    topic TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pused_umo_ts ON proactive_used(umo, ts);
"""

# 调度任务登记表必须放在模块级：插件热重载会新建 ProactiveEngine 实例，
# 实例属性上的 _proactive_tasks 在新实例里永远是空列表，旧实例的循环
# 于是没人取消，重载几次就会有几个循环同时发消息。
_LOOP_TASKS: list[asyncio.Task] = []


def stop_all_loops() -> None:
    """取消所有旧实例遗留的调度循环（热重载 / 卸载时调用）。"""
    while _LOOP_TASKS:
        task = _LOOP_TASKS.pop()
        try:
            task.cancel()
        except Exception:
            pass


def start_loops(engine: "ProactiveEngine") -> None:
    """先清掉旧循环，再启动本实例的 loop 与 watchdog。

    除了模块级登记表，还给本次启动打一个 runner_token 写进 settings.json：
    旧实例的循环每次 tick 都会核对 token，发现已被接管就自行退出。
    这样即使插件模块被重新导入（登记表变成新对象），旧循环也不会活着。
    """
    stop_all_loops()
    token = uuid.uuid4().hex
    engine._runner_token = token
    try:
        cfg = engine.settings()
        cfg["runner_token"] = token
        engine.save_settings()
    except Exception as exc:
        logger.warning(f"[token_panel] 写入调度标记失败: {exc}")
    _LOOP_TASKS.append(asyncio.create_task(engine.loop()))
    _LOOP_TASKS.append(asyncio.create_task(engine.watchdog_loop()))


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
        self._last_topics: list[str] = []   # 本轮取用的素材标题，发送成功后入库去重
        self._last_material: str = ""       # 本轮素材原文，供发送前自检比对
        self._last_dialogue: str = ""       # 本轮注入的最近对话，供发送前自检比对
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
        """当日已发条数。status 只表示「主人有没有回」，不能用来数发送量：
        主人一回复 status 就变成 replied，按 status='sent' 统计会让计数清零，
        于是每回一句都会立刻再发一条。"""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM proactive_log WHERE umo=? AND ts LIKE ?",
                    (umo, f"{today}%"))
                return int(cur.fetchone()[0])
        except Exception:
            return 0

    def _last_sent_at(self, umo: str) -> datetime | None:
        """上次发送时间。优先读会话表的 last_sent（发送瞬间写入，不受回复影响），
        没有再回落日志最后一行（同样不看 status）。"""
        try:
            raw = str((self.session_row(umo) or {}).get("last_sent") or "")
            if raw:
                return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT ts FROM proactive_log WHERE umo=? ORDER BY id DESC LIMIT 1",
                    (umo,))
                got = cur.fetchone()
            if got:
                return datetime.strptime(got["ts"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        return None

    # Claim the slot atomically (conditional UPDATE + commit) before sending, so two
    # schedulers can never deliver the same proactive message twice.
    def _claim_slot(self, umo: str, now: datetime, wait_minutes: float) -> bool:
        """发送许可的原子抢占：多个循环（含热重载遗留的旧实例）同时判定
        「该发了」时，只有一个能写进去，其余直接放弃。"""
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        cutoff = (now - timedelta(minutes=max(0.5, wait_minutes))).strftime("%Y-%m-%d %H:%M:%S")
        conn = None
        try:
            conn = self._conn()
            conn.execute(
                "INSERT OR IGNORE INTO proactive_sessions(umo, last_sent, updated_at)"
                " VALUES(?,?,?)", (umo, "", stamp))
            cur = conn.execute(
                "UPDATE proactive_sessions SET last_sent=?, updated_at=?"
                " WHERE umo=? AND (last_sent IS NULL OR last_sent='' OR last_sent<=?)",
                (stamp, stamp, umo, cutoff))
            conn.commit()
            return cur.rowcount == 1
        except Exception as exc:
            logger.warning(f"[token_panel] 抢占发送许可失败 {umo}: {exc}")
            return False

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
            if umo.lower().startswith("webchat"):
                continue  # 网页面板会话不参与主动消息，避免插进对话
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
    def _recent_contents(self, umo: str, limit: int = 6) -> list[str]:
        """最近几条已发内容，用于过滤重复素材（素材源是固定的最近记录，
        不去重的话同一件事会被反复拿出来说）。"""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT content FROM proactive_log WHERE umo=?"
                    " ORDER BY id DESC LIMIT ?", (umo, limit)).fetchall()
            return [str(r["content"] or "") for r in rows]
        except Exception:
            return []

    def _used_topics(self, umo: str, hours: float = 72.0) -> list[str]:
        """最近 N 小时已经讲过的素材标题（跨消息、跨重启都记得）。"""
        cutoff = (datetime.now() - timedelta(hours=max(1.0, hours))).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT topic FROM proactive_used WHERE umo=? AND ts>=?"
                    " ORDER BY id DESC LIMIT 120", (umo, cutoff)).fetchall()
            return [str(r["topic"] or "") for r in rows]
        except Exception:
            return []

    def _mark_used(self, umo: str, topics: list[str]) -> None:
        """发送成功后登记素材，避免同一条素材在几小时内被反复讲。"""
        picked = [(umo, str(t).strip()[:120], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                  for t in (topics or []) if str(t).strip()]
        if not picked:
            return
        try:
            with self._conn() as conn:
                conn.executemany(
                    "INSERT INTO proactive_used(umo, topic, ts) VALUES(?,?,?)", picked)
        except Exception as exc:
            logger.warning(f"[token_panel] 记录已用素材失败: {exc}")

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", text or "")
        if len(clean) < 2:
            return set()
        return {clean[i:i + 2] for i in range(len(clean) - 1)}

    @staticmethod
    def _overlap(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        hit = len(a & b)
        return max(hit / len(a), hit / len(b))

    def _is_repeat(self, item: str, others: list[str], threshold: float = 0.26) -> bool:
        """素材去重：双向重叠率取大者。

        单向比值（交集 / 标题二元组）会被长标题里的 tag、话题词稀释：标题能有
        五六十个二元组，生成出来的文案只有二十来个，同一件事的比值也能压到阈值
        以下。双向取大之后，短文案命中同样算命中。
        """
        grams = self._bigrams(item)
        if not grams:
            return False
        for text in others:
            if self._overlap(grams, self._bigrams(text)) >= threshold:
                return True
        return False

    def _douyin_material(self, umo: str = "") -> str:
        """抖音素材。三条硬条件：浏览开关开着、最后一次浏览足够新、没讲过。

        浏览关掉之后 douyin_log.json 里仍留着上次的标题，原来每次调度都会从里面
        挑一条，等于替用户「补刷」旧视频——2026-10-31 中午连讲两遍同一件事就是
        这么来的。默认只认 24 小时内的记录，可用 douyin.material_ttl_hours 调。
        """
        self._last_topics = []
        cfg = self.plugin._settings.get("douyin")
        cfg = cfg if isinstance(cfg, dict) else {}
        if cfg.get("enabled") is False:
            return ""
        try:
            ttl = float(cfg.get("material_ttl_hours") or 24)
        except Exception:
            ttl = 24.0
        cutoff = datetime.now() - timedelta(hours=max(1.0, ttl))
        entries: list[dict] = []
        try:
            raw_entries = self.plugin._dy_log_read()[:8]
        except Exception:
            raw_entries = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            try:
                taken = datetime.strptime(str(entry.get("ts") or ""), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if taken >= cutoff:
                entries.append(entry)
        if not entries:
            return ""
        recent = self._recent_contents(umo) + self._used_topics(umo)
        titles: list[str] = []
        for entry in entries:
            for item in (entry.get("items") or [])[:4]:
                title = re.sub(r"\s*@.+$", "", str(item.get("title") or "")).strip()
                title = re.sub(r"^\d{1,2}:\d{2}\s*", "", title).strip()   # 去掉“04:38”时长戳
                if not title:
                    continue
                title = title[:80]
                if self._is_repeat(title, recent) or self._is_repeat(title, titles):
                    continue
                titles.append(title)
        self._last_topics = titles[:6]
        return "；".join(titles[:6])

    def _search_material(self, umo: str = "") -> str:
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
        recent = self._recent_contents(umo) + self._used_topics(umo)
        picked: list[str] = []
        for row in rows:
            query = str(row[0]).strip() if row and str(row[0]).strip() else ""
            if not query or self._is_repeat(query, recent) or self._is_repeat(query, picked):
                continue
            picked.append(query)
        self._last_topics = picked[:8]
        return "；".join(picked[:8])

    def _material(self, source: str, umo: str = "") -> str:
        self._last_topics = []
        if source == "douyin":
            return self._douyin_material(umo)
        if source == "search":
            return self._search_material(umo)
        return ""

    # ------------------------------------------------------------------ #
    # 上下文：让主动消息接得住上一句
    # ------------------------------------------------------------------ #
    def _astrbot_db_path(self):
        """AstrBot 主库路径（会话历史在里面），拿不到就返回 None。"""
        try:
            path = self.data_dir.parent.parent / "data_v4.db"
            return path if path.exists() else None
        except Exception:
            return None

    def _render_dialogue_message(self, message: Any) -> str:
        """把一条会话消息压成一行「谁：说了什么」。"""
        if not isinstance(message, dict):
            return ""
        role = str(message.get("role") or "")
        content = message.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    kind = str(part.get("type") or "")
                    if kind == "text":
                        parts.append(str(part.get("text") or ""))
                    elif kind in ("image_url", "image"):
                        parts.append("[图片]")
                elif part:
                    parts.append(str(part))
            text = " ".join(parts)
        # 去掉系统注入的时间戳、图片占位、情绪标记，只留人话
        text = re.sub(r"<system_reminder>.*?</system_reminder>", "", text, flags=re.S)
        text = re.sub(r"\[Image[^\]]*\]", "", text)
        text = re.sub(r"&&\w+&&", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""
        who = "主人" if role == "user" else "我"
        return f"{who}：{text[:90]}"

    def _recent_dialogue(self, umo: str, limit: int = 10) -> str:
        """读本会话最近的真实对话。读不到就返回空串，绝不编造。"""
        try:
            limit = max(0, min(int(limit or 0), 20))
        except Exception:
            limit = 10
        if limit <= 0:
            return ""
        db = self._astrbot_db_path()
        if db is None:
            return ""
        messages: list[Any] = []
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                row = conn.execute(
                    "SELECT content FROM conversations WHERE user_id=?"
                    " ORDER BY updated_at DESC LIMIT 1", (umo,)).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                parsed = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                if isinstance(parsed, list):
                    messages = parsed
        except Exception as exc:
            logger.debug(f"[token_panel] 读取最近对话失败: {exc}")
        lines = [
            line for line in (self._render_dialogue_message(m) for m in messages[-limit:])
            if line
        ]
        return "\n".join(lines)

    def _build_compose_prompt(self, material: str, dialogue: str, soft: bool) -> str:
        """拼主动消息的写作指令（纯函数，方便单测）。"""
        now = datetime.now()
        weekday = "一二三四五六日"[now.weekday()]
        rules = [
            "你现在要主动给主人发一条消息（不是回复，是主动搭话）。",
            f"现在是 {now.strftime('%Y-%m-%d %H:%M')}（周{weekday}）。这句话必须符合这个时间点："
            "白天不许说“还不睡”“半夜”，也不要凭空猜他刚在干什么。",
        ]
        if dialogue:
            rules.append("【最近的真实对话（从旧到新）】\n" + dialogue)
            rules.append(
                "上面最后一句是我说的、主人还没回。要接着这个话头说，或者自然岔开，不能装作没发生过。"
            )
        else:
            rules.append("【最近对话】没读到记录，那就只说一句轻量的日常话，不要编细节。")
        if material:
            rules.append(f"【可以顺口提一句的素材】{material}")
        else:
            rules.append("【素材】没有。不要假装刚刷到、刚看到什么。")
        rules += [
            "硬性要求：",
            "1. 只说你确定的事：只允许引用【最近对话】和【素材】里出现过的内容，其余一律不许编。",
            "2. 不编造自己的动作（“我刚吃了”“我刚刷到”“我看到一个”），素材里没有就是没有。",
            "3. 不复述素材标题，不出现“素材/搜索/抖音/视频”这些词，不用 # 和 @。",
            "4. 一句话，最多 40 个字，口语，像随手发的；不要表情、不要列表、不要旁白。",
            "5. 如果此刻真没什么值得说的，就只输出 SKIP 四个字母，别的什么都不要写。",
        ]
        if soft:
            rules.append("6. 语气柔和克制，别热情过头，一次最多一个问句。")
        return "\n".join(rules)

    async def _review_message(
        self, umo: str, text: str, material: str, dialogue: str
    ) -> tuple[bool, str]:
        """发送前自检：这句话发出去会不会显得莫名其妙。"""
        prompt = (
            "你是严格的审稿人。下面是角色准备主动发给主人的一句话，以及它当时能看到的上下文。"
            "判断这句话能不能原样发出去。\n\n"
            f"【准备发的话】{text}\n\n"
            f"【最近对话】\n{dialogue or '（无）'}\n\n"
            f"【素材】{material or '（无）'}\n\n"
            "命中任一条就驳回：\n"
            "1. 和最近对话完全无关，像自说自话；\n"
            "2. 编造了上下文里没有的事实（自己刚做了什么、看到什么、主人刚做了什么）；\n"
            "3. 时间点明显不对（比如下午说“你怎么还不睡”）；\n"
            "4. 空话套话，或复读上下文里的原句；\n"
            "5. 语义混乱、指代不清、读起来像病句。\n"
            '只输出 JSON：{"verdict":"send"} 或 {"verdict":"drop","why":"不超过15字"}'
        )
        reason = self._precheck_message(text, material, dialogue)
        if reason:
            return False, reason
        try:
            provider = await self.plugin.context.get_using_provider_async(umo=umo)
            if provider is None:
                return True, ""
            raw = ""
            for attempt in range(2):
                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=prompt,
                            system_prompt="你是极简审稿器，只输出 JSON，不要解释。",
                            contexts=[],
                        ),
                        timeout=25,
                    )
                    raw = str(getattr(resp, "completion_text", "") or "")
                except Exception as exc:
                    logger.warning(f"[token_panel] 主动消息自检调用失败: {exc}")
                    if attempt == 1:
                        return True, ""
                    continue
                verdict = self._parse_verdict(raw)
                if verdict is not None:
                    why_match = re.search(r'"why"\s*:\s*"?([^",}]{0,40})', raw)
                    return verdict, (why_match.group(1) if why_match else "")
                logger.debug(f"[token_panel] 审稿输出无法解析，重试: {raw[:60]!r}")
            logger.info(f"[token_panel] 审稿连续无有效结论，按通过处理: {text[:30]}")
            return True, ""
        except Exception as exc:
            logger.warning(f"[token_panel] 主动消息自检异常，按通过处理: {exc}")
            return True, ""

    def _parse_verdict(self, raw: str):
        """从审稿输出里取结论。返回 True=放行 False=驳回 None=读不出。"""
        text = (raw or "").strip()
        if not text:
            return None
        match = re.search(r'"verdict"\s*:\s*"(send|drop)"', text, re.I)
        if not match:
            # 兼容被截断/带代码块的输出
            match = re.search(r'verdict\s*[:=]\s*"?(send|drop)', text, re.I)
        if match:
            return match.group(1).lower() == "send"
        lowered = text.lower()
        if re.search(r'\bdrop\b|驳回|不发', lowered):
            return False
        if re.search(r'\bsend\b|放行|可以发', lowered):
            return True
        return None

    def _precheck_message(self, text: str, material: str, dialogue: str) -> str:
        """零成本硬规则：明显编造/时间错位直接驳回，不浪费一次模型调用。"""
        body = (text or "").strip()
        if not body:
            return "空消息"
        context = f"{material}\n{dialogue}"
        # 1) 假装刚看了外部内容，但上下文里没有这条素材
        source_hit = re.search(r"(刚|刚刚|方才)?(刷到|刷到了|看到一个|刷了个|视频里说|热搜上|抖音上|网上看到)", body)
        if source_hit:
            if not material.strip():
                return "编造外部素材"
            claim = re.split(r"[，。！？,.!?\s]", body[source_hit.end():])[0][:12]
            grams = {claim[i:i + 2] for i in range(len(claim) - 1)}
            if len(claim) >= 4 and len([g for g in grams if g in material]) < 2:
                return "素材对不上"
        # 2) 时间点错位：白天说困/说睡
        hour = datetime.now().hour
        if 9 <= hour < 20 and re.search(r"(还不睡|还没睡|该睡了|去睡吧|犯困|好困|困了|晚安|半夜|凌晨)", body):
            return "时间点不对"
        # 3) 编造自己的动作，且上下文里没有
        for act in re.findall(r"我刚(吃了|喝了|看了|刷了|玩了|出门|回来|睡醒)", body):
            if act not in context:
                return "编造自身动作"
        # 4) 原样复读对方上一句
        if dialogue:
            last = ""
            for line in reversed(dialogue.splitlines()):
                line = line.strip()
                if line:
                    last = re.sub(r"^(主人|我)：", "", line)
                    break
            if last and len(last) >= 6 and last[:8] in body:
                return "复读上一句"
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
        material = self._material(source, umo)
        self._last_material = material
        try:
            lines = int(self.settings().get("dialogue_lines") or 10)
        except Exception:
            lines = 10
        dialogue = ""
        try:
            dialogue = self._recent_dialogue(umo, lines)
        except Exception as exc:
            logger.debug(f"[token_panel] 组装对话上下文失败: {exc}")
        self._last_dialogue = dialogue
        persona = await self._persona_prompt(umo)
        prompt = self._build_compose_prompt(material, dialogue, soft)
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
        if text.strip("[]【】 ").upper() in ("", "SKIP"):
            logger.info("[token_panel] 模型判断此刻无话可说（SKIP），本轮不发送")
            return ""
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

    # Gate one session through silence window / daily cap / cooldown, then generate the
    # message from the current persona and deliver it.
    async def _maybe_send(self, umo: str, cfg: dict[str, Any], now: datetime) -> None:
        eff = self._effective(umo)
        cooling, _left = self._cooling(eff)
        if cooling:
            return
        last_in = self._last_in.get(umo)
        guard = max(int(cfg.get("idle_guard_seconds") or 600), 30)  # 面板可调，下限 30 秒
        if last_in and (now - last_in).total_seconds() < guard:
            return
        # 硬闸：库里记录的“最近一次人类发言”同样生效，重启后也不会失忆
        try:
            with self._conn() as conn:
                got = conn.execute(
                    "SELECT updated_at FROM proactive_sessions WHERE umo=?", (umo,)).fetchone()
            if got and got["updated_at"]:
                spoke = datetime.strptime(str(got["updated_at"]), "%Y-%m-%d %H:%M:%S")
                if (now - spoke).total_seconds() < guard:
                    return
        except Exception:
            pass
        if self.count_today(umo) >= eff["daily_max"]:
            return
        last_sent = self._last_sent_at(umo)
        wait = eff["interval"]
        jitter = eff["jitter"]
        if jitter:
            wait += random.uniform(-abs(jitter), abs(jitter))
        wait = max(0.5, wait)
        if last_sent and (now - last_sent).total_seconds() < max(wait * 60, 60):
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
                # 发送前自检：宁可这一条不发，也别发出去让人一脸问号
                if cfg.get("review", True):
                    ok, why = await self._review_message(
                        umo, text, self._last_material, self._last_dialogue
                    )
                    if not ok:
                        logger.info(
                            f"[token_panel] 主动消息自检未通过，已丢弃：{text[:40]}"
                            f"（{why or '未说明'}）"
                        )
                        continue
                if not self._claim_slot(umo, datetime.now(), wait):
                    logger.info(f"[token_panel] 主动消息被其它调度抢占，跳过 {umo}")
                    break
                if await self._send(umo, text):
                    self.add_log(umo, text, SOURCE_LABELS[src], "sent")
                    self._mark_used(umo, self._last_topics)
                    logger.info(f"[token_panel] 主动消息已发送 {umo} 素材={src} 长度={len(text)}")
                    try:
                        from .eventlog import log_event
                        log_event("信息", "系统", "主动消息", f"主动消息已发送（{src}，{len(text)} 字）", umo=umo)
                    except Exception:
                        pass
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
            if not self._is_current_runner():
                logger.info("[token_panel] 主动消息循环已被新实例接管，退出本循环")
                return
            try:
                await self.tick()
            except Exception as exc:
                logger.warning(f"[token_panel] 主动消息循环异常: {exc}")
            await asyncio.sleep(45)

    async def watchdog_loop(self) -> None:
        """每 5 分钟：超时未回的主动消息标记为“被无视”，并按倍率加冷却。"""
        await asyncio.sleep(300)
        while True:
            if not self._is_current_runner():
                return
            try:
                await self._sweep_ignored()
            except Exception as exc:
                logger.warning(f"[token_panel] 主动消息巡检异常: {exc}")
            await asyncio.sleep(300)

    def _is_current_runner(self) -> bool:
        """本实例是否仍是当前有效的调度者。

        必须读磁盘上的 settings.json：热重载后的新实例写的是它自己的内存副本，
        旧实例内存里的 token 永远等于自己，用内存比较判断不出来。
        """
        token = getattr(self, "_runner_token", "")
        if not token:
            return True
        try:
            raw = json.loads(self.plugin.settings_path.read_text(encoding="utf-8"))
            current = str(((raw.get("proactive") or {}).get("runner_token")) or "")
        except Exception:
            return True
        return current in ("", token)

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
        now = datetime.now()
        self._last_in[umo] = now
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO proactive_sessions(umo, last_sent, updated_at) VALUES(?,?,?)",
                    (umo, "", stamp))
                conn.execute(
                    "UPDATE proactive_sessions SET updated_at=? WHERE umo=?", (stamp, umo))
        except Exception as exc:
            logger.warning(f"[token_panel] 记录活跃时间失败 {umo}: {exc}")
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