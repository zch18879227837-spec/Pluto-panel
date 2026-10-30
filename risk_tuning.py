"""抖音风控调速：近 3 天加权风控分数 + 降档 / 回升状态机。

设计要点（与《抖音风控调速》需求一一对应）：
1. 分数 = 最近 72 小时内每次验证的加权和，权重按「距今小时数」指数衰减（半衰期 24h），
   再乘以面板上的「严格系数」（0.1~2.0）。越新的验证权重越高，超过 3 天的记录自动失效。
2. 触发验证后立即降档，并且必须连续 90 分钟没有新的验证，才开始回升；回升用 90 分钟
   匀速把参数放回正常档位。回升途中只要再触发一次验证，立刻打断、重置计时。
3. 档位只有三档：正常 / 轻度降档 / 深度降档。视频停留时长硬性底线 8 秒，任何档位都不破。
4. 分数、计时、回升进度全部放在内存里（属于临时运行数据），重启 AstrBot 自动清空；
   只有严格系数由外层存进 PLUTO panel 的 settings.json。

这个模块不 import 任何 AstrBot 组件，也不做 IO，方便单独测试。
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

# ---- 分数模型 ----
WINDOW_HOURS = 72.0          # 只看最近 3 天
HALF_LIFE_HOURS = 24.0       # 每 24 小时权重减半
LIGHT_SCORE = 0.8            # 达到该分数 → 轻度降档
DEEP_SCORE = 2.0             # 达到该分数 → 深度降档

# ---- 回升节奏 ----
QUIET_MINUTES = 90.0         # 连续无验证多久才启动回升
RAMP_MINUTES = 90.0          # 回升周期（匀速放回正常档位）

# ---- 参数收放幅度 ----
MIN_DWELL = 8.0              # 硬性底线：任何档位单视频停留 ≥ 8 秒
DWELL_NORMAL = (8.0, 20.0)   # 正常档位停留区间（秒）
DWELL_DEEP = (16.0, 32.0)    # 深度档位停留区间（秒）
VIDEO_KEEP_DEEP = 0.4        # 深度档位单次视频数最多保留 40%
MINUTES_KEEP_DEEP = 0.5      # 深度档位单次时长最多保留 50%
INTERVAL_MAX_MULT = 2.2      # 深度档位浏览间隔最多拉长到 2.2 倍

TIER_NORMAL = "正常"
TIER_LIGHT = "轻度降档"
TIER_DEEP = "深度降档"
TIER_KEYS = {TIER_NORMAL: "normal", TIER_LIGHT: "light", TIER_DEEP: "deep"}

STAGE_RUNNING = "正常运行"
STAGE_WAITING = "回升观察等待中"
STAGE_RAMPING = "回升进行中"
STAGE_KEYS = {STAGE_RUNNING: "running", STAGE_WAITING: "waiting", STAGE_RAMPING: "ramping"}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fmt(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class RiskTuner:
    """风控调速状态机（纯内存，重启清空）。"""

    def __init__(self) -> None:
        self.events: list[float] = []          # 每次验证的触发时间戳
        self.last_verified: float | None = None

    # ---------- 写入 ----------
    def record_verification(self, ts: float | None = None) -> None:
        """记录一次风控验证：立刻降档，并重置回升计时。"""
        now = float(ts if ts is not None else time.time())
        self.events.append(now)
        self.last_verified = now
        self._prune(now)

    def reset(self) -> None:
        self.events.clear()
        self.last_verified = None

    # ---------- 计算 ----------
    def _prune(self, now: float) -> None:
        horizon = WINDOW_HOURS * 3600.0
        self.events = [t for t in self.events if now - t <= horizon]
        if self.last_verified is not None and now - self.last_verified > horizon:
            self.last_verified = None

    # Weighted verification score over the last 72 hours (half-life 24h), multiplied by the
    # operator-facing strictness factor.
    def score(self, strictness: float = 1.0, now: float | None = None) -> float:
        """近 3 天加权风控分数（已乘严格系数）。"""
        now = float(now if now is not None else time.time())
        self._prune(now)
        total = 0.0
        for ts in self.events:
            age_h = max(0.0, now - ts) / 3600.0
            total += math.pow(0.5, age_h / HALF_LIFE_HOURS)
        return round(total * _clamp(float(strictness), 0.1, 2.0), 3)

    def recovered_at(self, now: float | None = None) -> tuple[float | None, float | None]:
        """返回 (预计开始回升时间, 预计完全恢复时间)。没有验证记录时都是 None。"""
        if self.last_verified is None:
            return None, None
        start = self.last_verified + QUIET_MINUTES * 60.0
        return start, start + RAMP_MINUTES * 60.0

    def snapshot(self, strictness: float = 1.0, now: float | None = None) -> dict[str, Any]:
        """当前状态快照：档位、分数、阶段、回升进度、收放系数。"""
        now = float(now if now is not None else time.time())
        score = self.score(strictness, now)
        stage, progress, severity = STAGE_RUNNING, 100.0, 0.0
        start, finish = self.recovered_at(now)

        if self.last_verified is not None:
            elapsed = now - self.last_verified
            quiet = QUIET_MINUTES * 60.0
            ramp = RAMP_MINUTES * 60.0
            if elapsed < quiet:
                stage = STAGE_WAITING
                progress = _clamp(elapsed / quiet * 100.0, 0.0, 100.0)
            elif elapsed < quiet + ramp:
                stage = STAGE_RAMPING
                progress = _clamp((elapsed - quiet) / ramp * 100.0, 0.0, 100.0)
            else:
                stage = STAGE_RUNNING
                progress = 100.0

        # 回升过程中，档位随进度匀速回落；回升结束后参数回到正常档位
        # （raw score 仍会保留到 3 天窗口自然衰减完，只作为参考值展示）
        effective_score = score
        if stage == STAGE_RAMPING:
            effective_score = score * (1.0 - progress / 100.0)
        elif stage == STAGE_RUNNING:
            effective_score = 0.0

        if effective_score >= DEEP_SCORE:
            tier = TIER_DEEP
        elif effective_score >= LIGHT_SCORE:
            tier = TIER_LIGHT
        else:
            tier = TIER_NORMAL

        severity = _clamp(effective_score / DEEP_SCORE, 0.0, 1.0)
        # 停留时长底线：severity 再高也不少于 MIN_DWELL
        dwell_min = MIN_DWELL + (DWELL_DEEP[0] - MIN_DWELL) * severity
        dwell_max = DWELL_NORMAL[1] + (DWELL_DEEP[1] - DWELL_NORMAL[1]) * severity

        return {
            "strictness": round(_clamp(float(strictness), 0.1, 2.0), 1),
            "score": round(score, 2),
            "score_raw": score,
            "score_effective": round(effective_score, 2),
            "tier": tier,
            "tier_key": TIER_KEYS[tier],
            "stage": stage,
            "stage_key": STAGE_KEYS[stage],
            "progress": round(progress, 1),
            "severity": round(severity, 3),
            "events": len(self.events),
            "last_verified": _fmt(self.last_verified),
            "recover_at": _fmt(start),
            "normal_at": _fmt(finish),
            "window_hours": int(WINDOW_HOURS),
            "half_life_hours": int(HALF_LIFE_HOURS),
            "quiet_minutes": int(QUIET_MINUTES),
            "ramp_minutes": int(RAMP_MINUTES),
            "min_dwell": MIN_DWELL,
            "light_score": LIGHT_SCORE,
            "deep_score": DEEP_SCORE,
            "ratio_videos": round(1.0 - (1.0 - VIDEO_KEEP_DEEP) * severity, 3),
            "ratio_minutes": round(1.0 - (1.0 - MINUTES_KEEP_DEEP) * severity, 3),
            "interval_mult": round(1.0 + (INTERVAL_MAX_MULT - 1.0) * severity, 3),
            "dwell_min": round(max(MIN_DWELL, dwell_min), 1),
            "dwell_max": round(max(MIN_DWELL, dwell_max), 1),
        }

    # Map the score to a three-tier parameter set; the 8s dwell floor is never crossed.
    def effective_params(
        self, dy: dict[str, Any], strictness: float = 1.0, now: float | None = None
    ) -> dict[str, Any]:
        """把档位映射成这一轮真正生效的浏览参数（最小停留时长仍按硬底线走）。"""
        snap = self.snapshot(strictness, now)
        videos = max(1, int(round(float(dy.get("max_videos", 8)) * snap["ratio_videos"])))
        minutes = max(1.0, round(float(dy.get("max_minutes", 8)) * snap["ratio_minutes"], 1))
        interval = float(dy.get("interval_minutes", 180)) * float(snap["interval_mult"])
        return {
            "videos": videos,
            "minutes": minutes,
            "interval": round(interval, 1),
            "dwell_min": snap["dwell_min"],
            "dwell_max": snap["dwell_max"],
            "snapshot": snap,
        }