"""公共行为风控核心（抖音网页模块与其它模块共用一套参数与计数）。

设计约束（与《抖音风控参数完全由管理员自定义》需求对应）：
1. 所有阈值都来自面板配置，代码里不设「内置封顶」；只有一条底层保护：
   抖音私信轮询间隔不得低于 5 秒（`MIN_POLL_SECONDS`），防止高频轮询丢 cookie。
2. 计数按「平台 + 用户」分组，QQ 与抖音的限流数据互不干扰。
3. 每次动作前调用 `guard()`：返回是否放行、需要等待多久、是否处于冷却。
   冷却时长也由管理员配置。
4. 模块不 import AstrBot，也不做 IO，方便单测与复用。
"""

from __future__ import annotations

import random
import time
from collections import deque
from typing import Any, Callable

MIN_POLL_SECONDS = 5.0
"""底层保护：私信轮询不得低于 5 秒。"""

RISK_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # 行为配额（每分钟）
    "video_per_min": 20,
    "like_per_min": 10,
    "comment_per_min": 4,
    "dm_per_min": 6,
    # 动作之间的随机延时（秒）
    "delay_min": 1.5,
    "delay_max": 4.5,
    # 触到配额后的冷却时长（秒）
    "cooldown_seconds": 90,
    # 每次会话最多动作数，0 表示不限
    "actions_per_session": 0,
    "poll_seconds": 8.0,
}

ACTION_LIMIT_KEY = {
    "video": "video_per_min",
    "like": "like_per_min",
    "comment": "comment_per_min",
    "dm": "dm_per_min",
    "share": "dm_per_min",
    "scroll": "video_per_min",
}


class RiskCore:
    """按平台+用户分组的滑动窗口计数器 + 冷却。"""

    def __init__(
        self,
        settings_get: Callable[[], dict],
        dbg: Callable[[str], None] | None = None,
        evt: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings_get
        self._dbg = dbg or (lambda msg: None)
        self._evt = evt
        self._clock = clock
        self._hits: dict[tuple[str, str, str], deque[float]] = {}
        self._cool: dict[tuple[str, str], float] = {}
        self._session_actions: dict[tuple[str, str], int] = {}
        self._last_wait: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------ 配置
    def cfg(self) -> dict[str, Any]:
        raw = self._settings() or {}
        out = dict(RISK_DEFAULTS)
        for key, value in raw.items():
            if key in out and isinstance(value, (int, float, bool)):
                out[key] = value
        out["poll_seconds"] = max(MIN_POLL_SECONDS, float(out.get("poll_seconds") or 8.0))
        out["delay_min"] = max(0.0, float(out.get("delay_min") or 0))
        out["delay_max"] = max(out["delay_min"], float(out.get("delay_max") or out["delay_min"]))
        return out

    def poll_seconds(self) -> float:
        return max(MIN_POLL_SECONDS, float(self.cfg().get("poll_seconds") or 8.0))

    # ------------------------------------------------------------ 计数
    def _key(self, platform: str, uid: str, action: str) -> tuple[str, str, str]:
        return (str(platform), str(uid), str(action))

    def _trim(self, bucket: deque[float], now: float) -> None:
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()

    def used(self, platform: str, uid: str, action: str) -> int:
        now = self._clock()
        bucket = self._hits.get(self._key(platform, uid, action))
        if not bucket:
            return 0
        self._trim(bucket, now)
        return len(bucket)

    def cooldown_left(self, platform: str, uid: str) -> float:
        until = self._cool.get((str(platform), str(uid)), 0.0)
        return max(0.0, until - self._clock())

    def random_delay(self) -> float:
        cfg = self.cfg()
        lo, hi = cfg["delay_min"], cfg["delay_max"]
        return round(random.uniform(lo, hi), 2) if hi > lo else round(lo, 2)

    # ------------------------------------------------------------ 放行判断
    def guard(self, platform: str, uid: str, action: str) -> dict[str, Any]:
        """动作执行前调用。返回 {ok, wait, reason, cooldown, used, limit}。"""
        cfg = self.cfg()
        if not cfg.get("enabled", True):
            return {"ok": True, "wait": 0.0, "reason": "", "cooldown": 0.0, "used": 0, "limit": 0}

        # 冷却到期后顺手把这个用户的分钟窗口清干净：否则冷却一过又立刻撞上
        # 刚才那几次的旧计数，变成永远不放行。
        left = self.cooldown_left(platform, uid)
        if left <= 0 and self._cool.pop((str(platform), str(uid)), None):
            for key in [k for k in self._hits if k[0] == str(platform) and k[1] == str(uid)]:
                self._hits.pop(key, None)
            self._session_actions.pop((str(platform), str(uid)), None)
        if left > 0:
            return {
                "ok": False,
                "wait": round(left, 1),
                "reason": f"冷却中，还要等 {int(left) + 1} 秒",
                "cooldown": round(left, 1),
                "used": 0,
                "limit": 0,
            }

        cap = int(cfg.get("actions_per_session") or 0)
        sess_key = (str(platform), str(uid))
        if cap > 0 and self._session_actions.get(sess_key, 0) >= cap:
            return {
                "ok": False,
                "wait": 0.0,
                "reason": f"这一轮已经做了 {cap} 个动作，先停一停",
                "cooldown": 0.0,
                "used": cap,
                "limit": cap,
            }

        limit_key = ACTION_LIMIT_KEY.get(action)
        limit = int(cfg.get(limit_key) or 0) if limit_key else 0
        if limit <= 0:
            return {"ok": True, "wait": self.random_delay(), "reason": "", "cooldown": 0.0, "used": 0, "limit": 0}

        key = self._key(platform, uid, action)
        bucket = self._hits.setdefault(key, deque())
        now = self._clock()
        self._trim(bucket, now)
        if len(bucket) >= limit:
            cool = float(cfg.get("cooldown_seconds") or 90)
            self._cool[(str(platform), str(uid))] = now + cool
            reason = f"每分钟最多 {limit} 次，先冷却 {int(cool)} 秒"
            if self._evt:
                try:
                    self._evt("警告", "抖音", "限流", f"{platform}/{uid} {action}：{reason}")
                except Exception:
                    pass
            return {"ok": False, "wait": round(cool, 1), "reason": reason, "cooldown": round(cool, 1), "used": len(bucket), "limit": limit}

        bucket.append(now)
        self._session_actions[sess_key] = self._session_actions.get(sess_key, 0) + 1
        wait = self.random_delay()
        self._last_wait[(str(platform), str(uid))] = wait
        return {"ok": True, "wait": wait, "reason": "", "cooldown": 0.0, "used": len(bucket), "limit": limit}

    def reset_session(self) -> None:
        """开始新一轮时清零会话计数（配额仍按分钟）。"""
        self._session_actions.clear()

    def snapshot(self) -> dict[str, Any]:
        cfg = self.cfg()
        return {
            "cfg": {
                "enabled": cfg["enabled"],
                "video_per_min": cfg["video_per_min"],
                "like_per_min": cfg["like_per_min"],
                "comment_per_min": cfg["comment_per_min"],
                "dm_per_min": cfg["dm_per_min"],
                "delay_min": cfg["delay_min"],
                "delay_max": cfg["delay_max"],
                "cooldown_seconds": cfg["cooldown_seconds"],
                "actions_per_session": cfg["actions_per_session"],
                "poll_seconds": cfg["poll_seconds"],
            },
            "min_poll_seconds": MIN_POLL_SECONDS,
            "active": [
                {
                    "platform": p,
                    "uid": u,
                    "cool": round(self.cooldown_left(p, u), 1),
                    "video": self.used(p, u, "video"),
                    "like": self.used(p, u, "like"),
                    "comment": self.used(p, u, "comment"),
                    "dm": self.used(p, u, "dm"),
                }
                for (p, u) in sorted({(k[0], k[1]) for k in self._hits})
                if self.used(p, u, "video") or self.used(p, u, "like") or self.used(p, u, "comment") or self.used(p, u, "dm")
            ],
        }