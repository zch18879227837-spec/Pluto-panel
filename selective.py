# -*- coding: utf-8 -*-
"""PLUTO panel · 选择性上下文剪枝（v1.17.0）。

为什么要它
---------
AstrBot 核心的 `max_turns` 是按「整轮」删的：一旦超过上限，最老的整轮直接消失，
轮里的约定、数字、偏好跟着一起没。本模块把「丢什么」变成逐片段判断：

- 规则层：约定/承诺/数字日期/偏好禁忌/进行中的事/强情绪 → 保留原句；
          寒暄、重复确认、纯语气词 → 丢弃；
- 记忆层：与 livingmemory 里高重要度事实（importance ≥ 0.65）重合的片段 → 加分；
- 模型层：规则判不准的「灰色片段」交给对话模型打 0–3 分（同一批次一次请求）。

产出三堆：keep（原句保留，进「记忆卡」）、fold（折叠成摘要）、drop（只进备份）。
所有原始历史仍然整份备份到 carry_backups，任何一次剪枝都能回填。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

PRUNE_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "trigger_rounds": 26,      # 超过这么多轮就自动剪一次
    "trigger_tokens": 55000,   # 或估算超过这么多 token
    "keep_recent": 10,         # 最近 N 轮永远原样保留
    "model_scoring": True,     # 灰色片段是否交给模型打分
    "max_drop_ratio": 0.4,     # 单次最多丢掉这么多比例的片段（防误伤）
    "memory_boost": True,      # 是否接入 livingmemory 的重要度
    "min_fragment_len": 4,     # 太短的片段直接按规则处理，不占用模型
}

# --------------------------------------------------------------------------- #
# 规则层
# --------------------------------------------------------------------------- #
KEEP_PATTERNS: list[tuple[str, str]] = [
    (r"说好|约定|答应|讲好|定好|决定|计划|安排|改到|推迟", "约定/计划"),
    (r"提醒|别忘|记得|务必|一定要|千万|到时候", "提醒/必须"),
    (r"下次|以后|明天|后天|周末|下周|月底|生日|纪念日", "时间点"),
    (r"\d{1,2}\s*[点:：]\s*\d{0,2}|\d{1,2}\s*(月\s*\d{1,2}|号)", "具体时间"),
    (r"\d+\.\d+|\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}|\d+\s*(元|块|块钱|米|G|MB|GB)", "数字/金额"),
    (r"喜欢|讨厌|不爱|爱吃|不吃|过敏|口味|忌口|习惯|作息", "偏好/禁忌"),
    (r"正在|在做|还没|进度|下载到|装好|弄好|修好|搞完|卡在", "进行中"),
    (r"主人|对象|朋友|同学|室友|学校|专业|宿舍|家里|公司|工作单位", "关系/身份"),
    (r"委屈|难受|生气|害怕|紧张|emo|睡不着|哭", "强情绪"),
    (r"不行|不要|不能|禁止|别碰|别说|别发|不许", "边界/禁令"),
]

DROP_PATTERNS: list[str] = [
    r"^(在吗|在不在|人呢|你好|嗨|hi|hello)[。！？!?~]*$",
    r"^(嗯+|哦+|噢+|啊+|哈+|嘿+|好+|行|收到|好的|好吧|可以|没事)[。！？!?~，,]*$",
    r"^(晚安|早安|早上好|午安|睡吧|吃饭了吗|吃了没)[。！？!?~]*$",
    r"^(哈哈+|嘿嘿+|笑死|草|牛|绝了)[。！？!?~]*$",
    r"^\[(图片|语音|表情)\]$",
]

KEEP_RE = [(re.compile(p), label) for p, label in KEEP_PATTERNS]
DROP_RE = [re.compile(p) for p in DROP_PATTERNS]
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|\n+")


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for raw in SENT_SPLIT.split(text or ""):
        s = (raw or "").strip()
        if s:
            out.append(s)
    return out


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^\u4e00-\u9fff0-9A-Za-z]+", "", text or "")
    if len(clean) < 2:
        return set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


NOISE_RE = (
    re.compile(r"Current datetime", re.I),
    re.compile(r"system_reminder", re.I),
    re.compile(r"^\[Image \d+:"),
    re.compile(r"original path", re.I),
    re.compile(r"^\[Voice", re.I),
    re.compile(r"^\[File", re.I),
)


def rule_score(text: str) -> tuple[int, str]:
    """返回 (分数, 理由)。5=强保留，0=可丢。"""
    s = (text or "").strip()
    if not s:
        return 0, "空"
    for rx in NOISE_RE:
        if rx.search(s):
            return 0, "系统/媒体占位"
    for rx in DROP_RE:
        if rx.match(s):
            return 0, "寒暄/语气词"
    for rx, label in KEEP_RE:
        if rx.search(s):
            return 5, label
    if len(s) <= 6:
        return 1, "过短"
    return 3, "普通内容"


# --------------------------------------------------------------------------- #
# 记忆层
# --------------------------------------------------------------------------- #
def load_memory_facts(db_path: Path, limit: int = 400) -> list[tuple[set[str], float, str]]:
    """读 livingmemory 的高重要度原子事实，返回 [(bigrams, importance, content)]。"""
    facts: list[tuple[set[str], float, str]] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return facts
    try:
        rows = con.execute(
            "SELECT content, importance FROM memory_atoms WHERE importance >= 0.65 ORDER BY importance DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for content, imp in rows:
            bg = _bigrams(str(content or ""))
            if bg:
                facts.append((bg, float(imp or 0.0), str(content)))
    except Exception:
        pass
    finally:
        con.close()
    return facts


def memory_boost(text: str, facts: list[tuple[set[str], float, str]], min_len: int = 12) -> tuple[int, str]:
    """与记忆事实重合得越多，越值得留原句（短句、重合少的不算，避免把「在吗」抬成事实）。"""
    if len(text or "") < min_len:
        return 0, ""
    bg = _bigrams(text)
    if not bg:
        return 0, ""
    best, hit = 0.0, ""
    for fbg, imp, content in facts:
        inter = len(bg & fbg)
        if not inter:
            continue
        score = inter / max(1, len(bg))
        if score > best:
            best, hit = score, content
    if best >= 0.5:
        return 2, f"对上长期记忆：{hit[:40]}"
    if best >= 0.22:
        return 1, "部分相关记忆"
    return 0, ""


# --------------------------------------------------------------------------- #
# 模型层
# --------------------------------------------------------------------------- #
SCORE_PROMPT = (
    "你要给一段聊天回忆做取舍。每条用小序号列出一句对话片段，判断它对「以后还能记得住当时的事」有多重要。\n"
    "打 0–3 分：\n"
    "3 = 必须原句记住：约定与承诺、具体时间数字、称呼与关系、明确偏好与禁忌、正在做的事与进度、强烈情绪。\n"
    "2 = 值得留：有信息量的事，但缺了以后能靠上下文补回。\n"
    "1 = 只剩语气：应答、重复确认、空话。\n"
    "0 = 可以丢：寒暄、纯表情、无信息量的打闹。\n"
    "只输出 JSON，不要解释：{\"scores\":[{\"i\":0,\"s\":3,\"why\":\"约定了下雨提醒\"}]}"
)


def parse_scores(raw: str, n: int) -> dict[int, tuple[int, str]]:
    out: dict[int, tuple[int, str]] = {}
    if not raw:
        return out
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return out
    try:
        data = json.loads(m.group(0))
    except Exception:
        return out
    if isinstance(data, list):
        data = {"scores": data}
    if not isinstance(data, dict):
        return out
    for item in data.get("scores") or []:
        try:
            i = int(item.get("i"))
            s = int(item.get("s"))
        except Exception:
            continue
        if 0 <= i < n:
            out[i] = (max(0, min(3, s)), str(item.get("why") or "")[:60])
    return out


# --------------------------------------------------------------------------- #
# 主体
# --------------------------------------------------------------------------- #
class SelectivePruner:
    """把一段历史切成片段、打分、给出 keep/fold/drop 三堆。"""

    def __init__(self, data_dir: Path, settings_get: Callable[[], dict], dbg: Callable[[str], None]):
        self.data_dir = Path(data_dir)
        self._settings_get = settings_get
        self._dbg = dbg

    # ---- 配置 ----
    def cfg(self) -> dict[str, Any]:
        raw = {}
        try:
            raw = (self._settings_get() or {}).get("prune") or {}
        except Exception:
            raw = {}
        out = dict(PRUNE_DEFAULTS)
        out.update({k: v for k, v in raw.items() if k in PRUNE_DEFAULTS})
        return out

    # ---- 片段化 ----
    def fragments(self, head: list[dict]) -> list[dict[str, Any]]:
        cfg = self.cfg()
        min_len = int(cfg.get("min_fragment_len") or 4)
        items: list[dict[str, Any]] = []
        rnd = 0
        for msg in head or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "user":
                rnd += 1
            text = self._text_of(msg)
            if not text:
                continue
            who = "主人" if role == "user" else ("肥鱼" if role == "assistant" else None)
            if who is None:
                continue
            for sent in split_sentences(text):
                if len(sent) < min_len:
                    score, why = rule_score(sent)
                else:
                    score, why = rule_score(sent)
                items.append(
                    {"i": len(items), "round": rnd, "who": who, "text": sent, "score": score, "why": why}
                )
        return items

    @staticmethod
    def _text_of(msg: dict) -> str:
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
            return "\n".join(parts).strip()
        return ""

    # ---- 打分 ----
    async def plan(self, umo: str, head: list[dict], provider=None) -> dict[str, Any]:
        cfg = self.cfg()
        items = self.fragments(head)
        facts: list[tuple[set[str], float, str]] = []
        if cfg.get("memory_boost"):
            facts = load_memory_facts(self._memory_db())

        for it in items:
            if facts and it["score"] < 5:
                add, why2 = memory_boost(it["text"], facts)
                if add:
                    it["score"] = min(5, it["score"] + add)
                    it["why"] = f"{it['why']}；{why2}"

        # 灰色片段（分数 2–3）交给模型复核
        gray = [it for it in items if it["score"] in (2, 3) and len(it["text"]) >= 8]
        model_used = 0
        if cfg.get("model_scoring") and provider is not None and gray:
            picked = gray[:60]
            scores = await self._score_with_model(provider, picked)
            model_used = len(scores)
            for it in picked:
                if it["i"] in scores:
                    s, why = scores[it["i"]]
                    it["model_score"] = s
                    it["score"] = max(0, min(5, s * 2)) if s >= 2 else s
                    it["why"] = (why or it["why"])[:60]

        # 分堆
        keep = [it for it in items if it["score"] >= 4]
        fold = [it for it in items if 2 <= it["score"] <= 3]
        drop = [it for it in items if it["score"] <= 1]

        # 防误伤：单次丢弃比例设上限，超出的按顺序救回并并入 fold（不能凭空丢掉）
        ratio = float(cfg.get("max_drop_ratio") or 0.4)
        if drop and items and len(drop) / len(items) > ratio:
            allowed = max(0, int(len(items) * ratio))
            rescued = drop[: len(drop) - allowed]
            drop = drop[len(drop) - allowed :]
            fold = rescued + fold
            if rescued:
                self._dbg(f"剪枝：丢弃比例超限，{len(rescued)} 段改走摘要")

        return {
            "ok": True,
            "umo": umo,
            "fragments": items,
            "keep": keep,
            "fold": fold,
            "drop": drop,
            "model_used": model_used,
            "cfg": cfg,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _memory_db(self) -> Path:
        cands = [
            self.data_dir.parent / "astrbot_plugin_livingmemory" / "livingmemory.db",
            Path(r"D:\dafeiyu\AstrBot\data\plugin_data\astrbot_plugin_livingmemory\livingmemory.db"),
        ]
        for p in cands:
            if p.exists():
                return p
        return cands[-1]

    async def _score_with_model(self, provider, items: list[dict]) -> dict[int, tuple[int, str]]:
        lines = []
        for n, it in enumerate(items):
            lines.append(f'{it["i"]}. [{it["who"]}] {it["text"][:100]}')
        prompt = SCORE_PROMPT + "\n\n---\n" + "\n".join(lines)
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="你只输出 JSON，不给解释。",
            )
            raw = (getattr(resp, "completion_text", None) or "").strip()
            mapped = parse_scores(raw, max(it["i"] for it in items) + 1)
            return mapped
        except Exception as exc:
            self._dbg(f"剪枝：模型打分失败（按规则继续）：{exc}")
            return {}

    # ---- 组装 ----
    @staticmethod
    def fact_card(keep: list[dict], total_rounds: int, limit: int = 14) -> str:
        if not keep:
            return ""
        ordered = sorted(keep, key=lambda it: -int(it.get("score") or 0))
        picked = ordered[:limit]
        lines = []
        for it in picked:
            text = str(it["text"] or "").strip()
            if len(text) > 90:
                cut = text[:90]
                pos = max(cut.rfind("。"), cut.rfind("，"), cut.rfind("！"), cut.rfind("？"))
                text = (cut[: pos + 1].rstrip("，,、；;：: ") + "…" if pos >= 45 else cut + "…")
            lines.append(f"- [{it['who']}] {text}")
        if len(keep) > limit:
            lines.append(f"（另有 {len(keep) - limit} 句同类内容已并入下面的摘要）")
        return (
            f"【记忆卡 · 前 {total_rounds} 轮里必须原句记住的 {len(picked)} 句】\n"
            + "\n".join(lines)
            + "\n（这些是原话摘录，涉及方向的主语也照抄，别改。）"
        )

    @staticmethod
    def clean_summary(text: str, limit: "int | None" = None) -> str:
        """清掉「配置项：未提及」这类空项、本机路径与情绪标记；默认不截字数。"""
        keep_lines: list[str] = []
        for raw in str(text or "").splitlines():
            line = re.sub(r"&&[a-zA-Z_]+&&", "", raw).rstrip()
            body = line.strip().lstrip("-*• ").replace(" ", "").rstrip("。．.，,；;")
            if not body:
                continue
            if re.search(r"(未提及|未提到|暂无|无相关|没有提到|不涉及)$", body):
                continue
            if re.search(r"(media_image_|\bD:\\|https?://) ", line) or re.search(r"[A-Za-z]:\\", line):
                continue
            if re.fullmatch(r"\*{0,2}[^：:]{1,12}\*{0,2}[：:]\s*", body):
                continue
            if len(re.sub(r"^[-*•\s]*[^：:]{1,12}[：:]", "", body)) < 6:
                continue  # 「情绪：发和」这种清掉标记后的残句不值得留
            keep_lines.append(line)
        out = "\n".join(keep_lines).strip()
        if limit and len(out) > limit:
            cut = out[:limit]
            pos = max(cut.rfind("。"), cut.rfind("\n"))
            out = cut[: pos + 1] if pos > limit * 0.6 else cut
        return out

    @staticmethod
    def folded_messages(fold: list[dict], drop: list[dict]) -> list[dict]:
        """折叠内容 + 丢弃内容一起交给摘要模型，生成叙事摘要。"""
        out: list[dict] = []
        for it in fold + drop:
            out.append({"role": "user" if it["who"] == "主人" else "assistant", "content": it["text"]})
        return out

    def stats(self, plan: dict) -> dict[str, Any]:
        items = plan.get("fragments") or []
        return {
            "fragments": len(items),
            "keep": len(plan.get("keep") or []),
            "fold": len(plan.get("fold") or []),
            "drop": len(plan.get("drop") or []),
            "model_used": plan.get("model_used", 0),
        }