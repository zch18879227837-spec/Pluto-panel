# -*- coding: utf-8 -*-
"""事件日志：统一记录「谁、在什么时间、做了什么」。

落盘三件套（全部在插件的 data 目录下）：
    event_log.db      SQLite 主库（面板的「事件日志维护」页读写它）
    event_log.jsonl   本地 JSON 行日志（双写备份，方便 grep / 外部工具）
    event_log.db-wal   SQLite 运行时产物（WAL 模式）

设计约束：
1. 分表维护：只建 `events` 单表 + 三个索引，结构稳定，方便面板「表维护」展示。
2. 带时间戳与级别：ts 存 `YYYY-MM-DD HH:MM:SS`（本地时区），ts_ms 存毫秒，便于排序与统计。
3. 操作者与类别：actor / category 两列做维度，面板按这两个维度筛选。
4. 自动清理：默认保留 30 天，另外限制最大 20000 行，超限从最旧删起。
5. 任何异常都不向调用方抛：日志模块自身坏掉，不能拖垮机器人主流程。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

RETENTION_DAYS = 30
MAX_ROWS = 20000
LEVELS = ("信息", "警告", "错误", "调试")
CATEGORIES = (
    "上下文", "压缩", "承接", "清空", "搜索", "语音", "通知",
    "主动消息", "抖音", "日记", "配置", "系统", "错误",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT    NOT NULL,
    ts_ms    INTEGER NOT NULL,
    level    TEXT    NOT NULL,
    actor    TEXT    NOT NULL,
    category TEXT    NOT NULL,
    detail   TEXT    NOT NULL,
    extra    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts       ON events(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_events_level    ON events(level);
"""

_sink: "EventLog | None" = None


def set_event_sink(log: "EventLog | None") -> None:
    """由 main.py 在初始化时注入实例，其它模块用 log_event 直接写。"""
    global _sink
    _sink = log


def log_event(level: str, actor: str, category: str, detail: str, **extra: Any) -> bool:
    """模块级快捷入口；没注入实例时静默丢弃，不抛异常。"""
    log = _sink
    if log is None:
        return False
    try:
        return log.write(level, actor, category, detail, extra or None)
    except Exception:
        return False


def _now_parts() -> tuple[str, int]:
    now = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), int(now * 1000)


class EventLog:
    def __init__(self, data_dir: Path, retention_days: int = RETENTION_DAYS, max_rows: int = MAX_ROWS) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "event_log.db"
        self.jsonl_path = self.data_dir / "event_log.jsonl"
        self.retention_days = int(retention_days)
        self.max_rows = int(max_rows)
        self._last_prune = 0.0
        self.last_error = ""
        try:
            with self._conn() as conn:
                conn.executescript(_SCHEMA)
        except Exception as exc:  # 建库失败也不能影响插件
            self.last_error = str(exc)

    # ---------- 基础 ----------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=8)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # Append one event to SQLite and mirror it to the JSONL file; returns False on failure
    # instead of raising, because logging must never break the caller.
    def write(self, level: str, actor: str, category: str, detail: str, extra: dict | None = None) -> bool:
        level = level if level in LEVELS else "信息"
        actor = (actor or "系统").strip()[:24]
        category = (category or "系统").strip()[:12]
        detail = (detail or "").strip()[:1000]
        ts, ts_ms = _now_parts()
        payload = ""
        if extra:
            try:
                payload = json.dumps(extra, ensure_ascii=False, default=str)[:2000]
            except Exception:
                payload = ""
        ok = False
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO events(ts, ts_ms, level, actor, category, detail, extra) VALUES(?,?,?,?,?,?,?)",
                    (ts, ts_ms, level, actor, category, detail, payload),
                )
            ok = True
        except Exception as exc:
            self.last_error = str(exc)
        # 双写 JSON 行日志（失败不影响 SQLite 已写入的结果）
        try:
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"ts": ts, "ts_ms": ts_ms, "level": level, "actor": actor,
                     "category": category, "detail": detail,
                     "extra": extra or {}},
                    ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass
        # 每 6 小时最多跑一次自动清理
        if ok and time.time() - self._last_prune > 6 * 3600:
            self._last_prune = time.time()
            try:
                self.prune()
            except Exception:
                pass
        return ok

    # ---------- 清理 ----------
    # Drop rows older than `days`, then enforce the row cap from oldest to newest.
    def prune(self, days: int | None = None) -> dict[str, int]:
        days = self.retention_days if days is None else max(1, int(days))
        cutoff_ms = int((time.time() - days * 86400) * 1000)
        deleted = 0
        try:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM events WHERE ts_ms < ?", (cutoff_ms,))
                deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                if total > self.max_rows:
                    over = total - self.max_rows
                    cur = conn.execute(
                        "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id ASC LIMIT ?)", (over,)
                    )
                    deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception as exc:
            self.last_error = str(exc)
        else:
            # 检查点必须放在写事务之外，否则会报 database table is locked
            try:
                with self._conn() as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
        rows = self.stats()
        return {"deleted": int(deleted), "remaining": int(rows.get("records", 0)), "days": days}

    # ---------- 查询 ----------
    def stats(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "path": str(self.db_path),
            "jsonl": str(self.jsonl_path),
            "size": 0, "records": 0, "tables": 0, "today": 0,
            "last_ts": "", "last_error": self.last_error, "retention_days": self.retention_days,
            "max_rows": self.max_rows,
        }
        try:
            if self.db_path.exists():
                info["size"] = self.db_path.stat().st_size
        except Exception:
            pass
        try:
            with self._conn() as conn:
                info["tables"] = int(conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0])
                info["records"] = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
                row = conn.execute("SELECT ts FROM events ORDER BY ts_ms DESC LIMIT 1").fetchone()
                info["last_ts"] = row[0] if row else ""
                today = time.strftime("%Y-%m-%d", time.localtime())
                info["today"] = int(conn.execute("SELECT COUNT(*) FROM events WHERE ts LIKE ?", (today + "%",)).fetchone()[0])
        except Exception as exc:
            info["last_error"] = str(exc)
        try:
            if self.jsonl_path.exists():
                info["jsonl_size"] = self.jsonl_path.stat().st_size
        except Exception:
            pass
        return info

    # Filtered, paginated read behind the panel list page (level / category / actor / days).
    def query(self, level: str = "", category: str = "", actor: str = "", keyword: str = "",
              days: int = 0, limit: int = 80, offset: int = 0) -> dict[str, Any]:
        where: list[str] = []
        args: list[Any] = []
        if level:
            where.append("level = ?")
            args.append(level)
        if category:
            where.append("category = ?")
            args.append(category)
        if actor:
            where.append("actor = ?")
            args.append(actor)
        if keyword:
            where.append("(detail LIKE ? OR extra LIKE ?)")
            args.extend([f"%{keyword}%", f"%{keyword}%"])
        if days and int(days) > 0:
            where.append("ts_ms >= ?")
            args.append(int((time.time() - int(days) * 86400) * 1000))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(500, int(limit or 80)))
        offset = max(0, int(offset or 0))
        items: list[dict[str, Any]] = []
        total = 0
        try:
            with self._conn() as conn:
                total = int(conn.execute("SELECT COUNT(*) FROM events" + clause, args).fetchone()[0])
                rows = conn.execute(
                    "SELECT id, ts, level, actor, category, detail, extra FROM events" + clause +
                    " ORDER BY ts_ms DESC, id DESC LIMIT ? OFFSET ?", (*args, limit, offset),
                ).fetchall()
                for row in rows:
                    items.append({
                        "id": row[0], "ts": row[1], "level": row[2], "actor": row[3],
                        "category": row[4], "detail": row[5], "extra": row[6],
                    })
        except Exception as exc:
            return {"ok": False, "error": str(exc), "items": [], "total": 0}
        return {"ok": True, "items": items, "total": total, "limit": limit, "offset": offset}

    def actors(self) -> list[str]:
        try:
            with self._conn() as conn:
                return [r[0] for r in conn.execute("SELECT actor, COUNT(*) c FROM events GROUP BY actor ORDER BY c DESC LIMIT 20")]
        except Exception:
            return []

    # ---------- 导出 ----------
    def dump_json(self, limit: int = 5000) -> tuple[str, str]:
        """返回 (文件名, JSON 文本)。"""
        data = self.query(limit=limit)
        payload = {
            "plugin": "astrbot_plugin_token_panel",
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "db": str(self.db_path),
            "total": data.get("total", 0),
            "items": data.get("items", []),
        }
        name = time.strftime("event-log-%Y%m%d-%H%M%S.json")
        return name, json.dumps(payload, ensure_ascii=False, indent=2)

    # Snapshot the SQLite file for download; WAL is checkpointed so the copy is consistent.
    def copy_db(self) -> tuple[str, Path]:
        name = time.strftime("event-log-%Y%m%d-%H%M%S.db")
        target = self.data_dir / "exports" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._conn() as conn:
                conn.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass
        src = sqlite3.connect(str(self.db_path))
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return name, target