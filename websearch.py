"""PLUTO panel · 联网搜索模块（v1.9.0 从独立插件 astrbot-websearch 内聚进来）。

三层分工，与独立插件完全一致：
- 配置层：面板「联网搜索」页下发到 settings.json 的 websearch 段；
- 工具层：只接收模型给的关键词 → 请求服务商 → 返回标准化 JSON，不判断「要不要搜」；
- 日志层：每次搜索落本模块自己的 SQLite，供面板列表读取。

模块独立性：
- 自身持有一个 enabled 开关，关掉后工具直接返回「已被关闭」，模型会改用自身知识；
- 数据库落在 data/plugin_data/astrbot_plugin_token_panel/websearch/search_log.db，
  不与其他模块共用文件，清空日志只影响本模块；
- 不推送消息、不拦截对话、不改上下文，搜索失败不影响会话主流程。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from astrbot.api import logger

MODULE_NAME = "websearch"
LOG_LIMIT = 50  # 日志最多保留条数（面板按 50 条展示）

PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek（api.deepseek.com/v1/tools/search）",
        "url": "https://api.deepseek.com/v1/tools/search",
        "key_hint": "sk- 开头",
    },
    "tavily": {
        "label": "Tavily（api.tavily.com/search）",
        "url": "https://api.tavily.com/search",
        "key_hint": "tvly- 开头",
    },
    "bocha": {
        "label": "博查 Bocha（api.bochaai.com/v1/web-search）",
        "url": "https://api.bochaai.com/v1/web-search",
        "key_hint": "博查控制台申请",
    },
}

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "provider": "deepseek",
    "api_key": "",
    "deepseek_api_key": "",
    "tavily_api_key": "",
    "bocha_api_key": "",
    "web_search_link": False,
    "max_steps": 12,
    "timeout": 20,
    "max_results": 5,
}

LIMITS: dict[str, tuple[int, int]] = {
    "max_steps": (1, 50),
    "timeout": (3, 120),
    "max_results": (1, 10),
}

# 模型看到的工具说明：与独立插件逐字一致，避免换壳后模型行为漂移
TOOL_DESC = """联网搜索。把关键词交给搜索服务，拿回网页标题、链接与摘要。

什么时候用：新闻时事、价格行情、具体参数、最新版本、教程步骤、报错排查、你不确定又想弄准的资料。
什么时候不用：闲聊、你自己的设定、常识和梗。搜之前先想想你是不是本来就知道。
一次问题最多搜两轮（必要时可用中英文各搜一次），拿到结果就自己嚼碎了回答，别把链接整段甩给用户。

Args:
    query(string): 搜索关键词，像人一样简短，不要写成整句问题
    count(number): 需要几条结果，1-10，默认 5
"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class WebSearchModule:
    """联网搜索模块：检索 + 日志，挂在 PLUTO panel 内部独立运行。"""

    def __init__(
        self,
        data_dir: Path,
        settings_getter: Callable[[], dict[str, Any]],
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self._settings_getter = settings_getter
        self._debug = debug or (lambda _m: None)
        self.dir: Path = Path(data_dir) / MODULE_NAME
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path: Path = self.dir / "search_log.db"
        self._recent: dict[str, list[float]] = {}
        self._init_db()

    # ------------------------------------------------------------------ #
    # 配置读取（每次都从 settings 实时取，面板改完立即生效，无需重载）
    # ------------------------------------------------------------------ #
    def conf(self) -> dict[str, Any]:
        merged = dict(DEFAULTS)
        try:
            settings = self._settings_getter() or {}
            raw = settings.get(MODULE_NAME)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in merged and value is not None:
                        merged[key] = value
        except Exception as exc:  # 配置坏了也不能让模块崩
            self._debug(f"读取联网搜索配置失败: {exc}")
        return merged

    @property
    def enabled(self) -> bool:
        return bool(self.conf().get("enabled", True))

    @property
    def provider(self) -> str:
        name = str(self.conf().get("provider") or "deepseek").strip().lower()
        return name if name in PROVIDERS else "deepseek"

    def _api_key(self) -> str:
        cfg = self.conf()
        direct = str(cfg.get("api_key") or "").strip()
        if direct:
            return direct
        return str(cfg.get(f"{self.provider}_api_key") or "").strip()

    @property
    def timeout(self) -> float:
        try:
            return float(max(3, min(120, int(self.conf().get("timeout") or 20))))
        except Exception:
            return 20.0

    @property
    def max_results(self) -> int:
        try:
            return int(max(1, min(10, int(self.conf().get("max_results") or 5))))
        except Exception:
            return 5

    @property
    def max_steps(self) -> int:
        try:
            return int(max(1, min(50, int(self.conf().get("max_steps") or 12))))
        except Exception:
            return 12

    def masked(self) -> dict[str, Any]:
        """给面板看的配置：Key 只回显是否已配置 + 尾 4 位，绝不回传明文。"""
        cfg = self.conf()
        out: dict[str, Any] = {
            "enabled": bool(cfg.get("enabled", True)),
            "provider": self.provider,
            "providers": {k: {"label": v["label"], "key_hint": v["key_hint"]} for k, v in PROVIDERS.items()},
            "web_search_link": bool(cfg.get("web_search_link", False)),
            "max_steps": self.max_steps,
            "timeout": int(self.timeout),
            "max_results": self.max_results,
            "limits": LIMITS,
            "log_limit": LOG_LIMIT,
            "db_path": str(self.db_path),
        }
        for field in ("api_key", "deepseek_api_key", "tavily_api_key", "bocha_api_key"):
            value = str(cfg.get(field) or "").strip()
            out[f"{field}_set"] = bool(value)
            out[f"{field}_tail"] = value[-4:] if len(value) >= 4 else ""
        out["key_ready"] = bool(self._api_key())
        out["key_tail"] = self._api_key()[-4:] if self._api_key() else ""
        return out

    # ------------------------------------------------------------------ #
    # 日志库
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        query TEXT NOT NULL,
                        status TEXT NOT NULL,
                        count INTEGER DEFAULT 0,
                        took_ms INTEGER DEFAULT 0,
                        results TEXT DEFAULT '[]',
                        error TEXT DEFAULT ''
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"[token_panel] 初始化搜索日志库失败: {exc}")

    def _write_log(
        self,
        *,
        provider: str,
        query: str,
        status: str,
        results: list[dict[str, Any]],
        took_ms: int,
        error: str = "",
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO search_log (ts, provider, query, status, count, took_ms, results, error)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        _now_iso(),
                        provider,
                        query[:500],
                        status,
                        len(results),
                        int(took_ms),
                        json.dumps(results, ensure_ascii=False)[:20000],
                        (error or "")[:400],
                    ),
                )
                conn.execute(
                    "DELETE FROM search_log WHERE id NOT IN "
                    "(SELECT id FROM search_log ORDER BY id DESC LIMIT ?)",
                    (LOG_LIMIT,),
                )
                conn.commit()
        except Exception as exc:  # 日志失败绝不影响搜索本身
            logger.warning(f"[token_panel] 写搜索日志失败: {exc}")
        try:
            from .eventlog import log_event
            log_event(
                "信息" if status == "ok" else "警告",
                "模型",
                "搜索",
                f"{provider} 搜索「{query[:40]}」返回 {len(results)} 条（{int(took_ms)} ms）"
                + (f"：{error[:60]}" if error else ""),
                query=query[:100],
            )
        except Exception:
            pass

    def rows(self, keyword: str = "", limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            if not self.db_path.exists():
                return out
            with self._connect() as conn:
                sql = "SELECT id, ts, provider, query, status, count, took_ms FROM search_log"
                args: list[Any] = []
                if keyword:
                    sql += " WHERE query LIKE ?"
                    args.append(f"%{keyword}%")
                sql += " ORDER BY id DESC LIMIT ?"
                args.append(int(limit))
                for row in conn.execute(sql, args):
                    out.append(
                        {
                            "id": row["id"], "ts": row["ts"], "provider": row["provider"],
                            "query": row["query"], "status": row["status"],
                            "count": row["count"], "took_ms": row["took_ms"],
                        }
                    )
        except Exception as exc:
            self._debug(f"读取搜索日志失败: {exc}")
        return out

    def stats(self) -> dict[str, Any]:
        data = {"total": 0, "today": 0, "ok": 0, "fail": 0, "avg_ms": 0, "last": None}
        try:
            if not self.db_path.exists():
                return data
            with self._connect() as conn:
                data["total"] = conn.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
                today = time.strftime("%Y-%m-%d")
                data["today"] = conn.execute(
                    "SELECT COUNT(*) FROM search_log WHERE ts LIKE ?", (today + "%",)
                ).fetchone()[0]
                data["ok"] = conn.execute(
                    "SELECT COUNT(*) FROM search_log WHERE status='ok'"
                ).fetchone()[0]
                data["fail"] = data["total"] - data["ok"]
                avg = conn.execute(
                    "SELECT AVG(took_ms) FROM search_log WHERE status='ok'"
                ).fetchone()[0]
                data["avg_ms"] = int(avg or 0)
                last = conn.execute(
                    "SELECT ts, query, status FROM search_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if last:
                    data["last"] = {"ts": last["ts"], "query": last["query"], "status": last["status"]}
        except Exception as exc:
            self._debug(f"统计搜索日志失败: {exc}")
        return data

    def clear(self) -> int:
        try:
            if not self.db_path.exists():
                return 0
            with self._connect() as conn:
                n = conn.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
                conn.execute("DELETE FROM search_log")
                conn.commit()
                return int(n)
        except Exception as exc:
            logger.warning(f"[token_panel] 清空搜索日志失败: {exc}")
            return 0

    # ------------------------------------------------------------------ #
    # 工具层：模型调用入口
    # ------------------------------------------------------------------ #
    # Run one query through the configured provider, trim results, and record it in search_log.db.
    async def search(self, umo: str, query: str, count: int = 0) -> str:
        """执行一次搜索，返回给模型的文本（与独立插件行为一致）。"""
        query = (query or "").strip()
        if not query:
            return "搜索关键词是空的，没搜。"
        if not self.enabled:
            return "联网搜索已被管理员关闭。请直接凭自己的知识回答，并说明这一点。"

        # 同一会话 5 分钟内最多搜 max_steps 次，防止模型来回搜个没完
        now = time.time()
        window = [t for t in (self._recent.get(umo) or []) if now - t < 300]
        cap = self.max_steps
        if cap > 0 and len(window) >= cap:
            return f"本次对话已经搜了 {len(window)} 次（上限 {cap}），别再搜了，用现有结果回答。"
        window.append(now)
        self._recent[umo] = window

        try:
            want = int(count or 0) or self.max_results
        except Exception:
            want = self.max_results
        want = max(1, min(10, want))

        key = self._api_key()
        if not key:
            self._write_log(provider=self.provider, query=query, status="fail",
                            results=[], took_ms=0, error="未配置 API Key")
            return "搜索服务没有配置 API Key，搜不了。请直接回答并说明没联网。"

        started = time.time()
        try:
            results = await asyncio.wait_for(
                self._call_provider(self.provider, key, query, want),
                timeout=self.timeout + 5,
            )
            took = int((time.time() - started) * 1000)
            self._write_log(provider=self.provider, query=query, status="ok",
                            results=results, took_ms=took)
            payload: dict[str, Any] = {
                "provider": self.provider,
                "query": query,
                "took_ms": took,
                "results": results,
            }
            if not bool(self.conf().get("web_search_link", False)):
                payload["hint"] = (
                    "回复里不要贴裸链接，用自己的话把结论讲清楚；"
                    "需要说明出处时自然提一句从哪看到的。"
                )
            return json.dumps(payload, ensure_ascii=False)
        except asyncio.TimeoutError:
            took = int((time.time() - started) * 1000)
            self._write_log(provider=self.provider, query=query, status="fail",
                            results=[], took_ms=took, error="请求超时")
            return "搜索超时了，没拿到结果。可以先凭自己的知识回答，或者换个关键词再试。"
        except Exception as exc:
            took = int((time.time() - started) * 1000)
            msg = str(exc)[:300]
            self._write_log(provider=self.provider, query=query, status="fail",
                            results=[], took_ms=took, error=msg)
            logger.warning(f"[token_panel] 联网搜索失败: {msg}")
            return f"搜索失败了（{msg}）。先凭自己的知识回答，并说明没联网查到。"

    async def _call_provider(
        self, provider: str, key: str, query: str, count: int
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if provider == "tavily":
                resp = await client.post(
                    PROVIDERS["tavily"]["url"],
                    json={"api_key": key, "query": query, "max_results": count, "include_answer": False},
                )
                resp.raise_for_status()
                data = resp.json()
                return [
                    {
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("url") or ""),
                        "summary": str(item.get("content") or "")[:600],
                    }
                    for item in (data.get("results") or [])
                ][:count]

            if provider == "bocha":
                resp = await client.post(
                    PROVIDERS["bocha"]["url"],
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"query": query, "count": count, "summary": True},
                )
                resp.raise_for_status()
                data = resp.json()
                items = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
                return [
                    {
                        "title": str(item.get("name") or ""),
                        "url": str(item.get("url") or ""),
                        "summary": str(item.get("summary") or item.get("snippet") or "")[:600],
                    }
                    for item in items
                ][:count]

            resp = await client.post(
                PROVIDERS["deepseek"]["url"],
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"query": query, "count": count},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("results") or []
            return [
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                    "summary": str(item.get("summary") or item.get("content") or "")[:600],
                }
                for item in items
            ][:count]