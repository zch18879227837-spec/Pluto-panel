"""大肥鱼等距小屋（Iso House）

PLUTO panel 的内嵌轻量等距房间模块。设计约束：

* 角色素材只来自管理员上传的参考图，模块本身不生成任何角色图片；
  上传的图片按原样缩放成场景精灵（可选透明 PNG 以获得最佳效果），
  左右移动靠水平镜像，坐下/躺下靠缩放与阴影微调。
* 房间布局、点位、背景、漫游范围全部存在面板既有的 ``settings.json``
  的 ``isohouse`` 段里，不新建记忆库；偏好与关键行为复用会话日记本。
* 本模块只提供状态与读写，A* 寻路与绘制放在前端（views/dashboard）。
"""

from __future__ import annotations

import base64
import binascii
import copy
import io
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

MAX_ASSET_BYTES = 6 * 1024 * 1024
MAX_LOG = 240
BG_PRESETS = ("night-sea", "dusk", "dawn", "custom")

# 场景网格：12 x 9（菱形地板），家具占格不参与寻路
GRID_W = 12
GRID_H = 9

# 室内可站立区域（贴墙留一格）
WALK_BOX = (1, 2, GRID_W - 2, GRID_H - 1)

FURNITURE_DEFAULT: list[dict[str, Any]] = [
    {"id": "bed", "kind": "bed", "x": 0, "y": 0, "w": 2, "h": 2, "label": "床", "block": True, "interactive": True},
    {"id": "wardrobe", "kind": "wardrobe", "x": 0, "y": 3, "w": 1, "h": 2, "label": "衣柜", "block": True, "interactive": True},
    {"id": "desk", "kind": "desk", "x": 2, "y": 2, "w": 2, "h": 1, "label": "书桌电脑", "block": True, "interactive": True},
    {"id": "shelf", "kind": "shelf", "x": 6, "y": 0, "w": 1, "h": 1, "label": "展示架", "block": True, "interactive": True},
    {"id": "sofa", "kind": "sofa", "x": 1, "y": 6, "w": 3, "h": 1, "label": "沙发", "block": True, "interactive": True},
    {"id": "table", "kind": "table", "x": 5, "y": 6, "w": 2, "h": 1, "label": "茶几", "block": True, "interactive": False},
    {"id": "rug", "kind": "rug", "x": 4, "y": 4, "w": 4, "h": 2, "label": "地毯", "block": False, "interactive": False},
    {"id": "plant", "kind": "plant", "x": 11, "y": 7, "w": 1, "h": 1, "label": "绿植", "block": True, "interactive": False},
]

POINTS_DEFAULT: list[dict[str, Any]] = [
    {"id": "p-bed", "pose": "sit", "name": "床边", "x": 2, "y": 3, "note": "床沿，能靠着床头坐", "interactive": True,
     "lines": ["这里最软了，你要不要一起瘫一会儿？", "被子还有太阳晒过的味道。"]},
    {"id": "p-wardrobe", "pose": "stand", "name": "衣柜", "x": 1, "y": 5, "note": "左边木衣柜", "interactive": True,
     "lines": ["今天穿哪件好呢……你帮我挑？", "衣柜里塞满了，可我还是觉得没衣服穿。"]},
    {"id": "p-desk", "pose": "desk", "name": "书桌", "x": 3, "y": 3, "note": "书桌和亮着的电脑", "interactive": True,
     "lines": ["屏幕的光有点凉，正好醒醒神。", "要不要坐过来看我敲两行？"]},
    {"id": "p-sofa", "pose": "sofa", "name": "沙发", "x": 2, "y": 8, "note": "沙发靠窗那侧", "interactive": True,
     "lines": ["沙发陷进去就起不来了。", "今晚有月亮，坐这儿正好。"]},
    {"id": "p-window", "pose": "gaze", "name": "窗边", "x": 10, "y": 2, "note": "海景窗正对海面", "interactive": True,
     "lines": ["外面那片海在动，像有人在慢慢呼吸。", "月亮掉进海里了，你看。"]},
    {"id": "p-corner", "pose": "stand", "name": "懒人角落", "x": 11, "y": 5, "note": "最靠里的角落", "interactive": True,
     "lines": ["角落最安心，谁都看不见我。", "来这儿找我，我一直都在。"]},
    {"id": "p-showcase", "pose": "stand", "name": "展示架", "x": 6, "y": 2, "note": "摆了些小物件的架子", "interactive": True,
     "lines": ["这层摆的都是舍不得扔的东西。", "要不要我把那本拿下来给你看？"]},
]

ROAM_DEFAULT = {
    "enabled": False,
    "regions": [[1, 2, GRID_W - 2, GRID_H - 2]],
    "stay_min": 3,
    "stay_max": 10,
    "speed": 2.4,
    "random_weight": 0.55,     # 随机格子权重，其余权重给交互点位
    "prefer_interactive": True,  # 优先去可交互点位
}

# 画布渲染参数（v1.12.0）：只影响绘制，不改寻路与存储
RENDER_DEFAULT = {
    "window_open": True,
    "grid_alpha": 0.06,
    "point_color": "#5B8BE0",
    "point_color_alt": "#E5B45A",
    "light_dir": "right",     # left / right
    "moonlight": 0.55,        # 月光强度 0–1
}

# 气泡参数（v1.12.0）
BUBBLE_DEFAULT = {
    "ttl": 8,                 # 秒
    "bg": "#101620",
    "color": "#EAF0FA",
    "max_width": 232,
    "alpha": 0.86,
    "offset": 0,              # 相对默认高度的额外偏移
}

ISO_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "bg": "night-sea",
    "grid": {"w": GRID_W, "h": GRID_H},
    "furniture": FURNITURE_DEFAULT,
    "points": POINTS_DEFAULT,
    "roam": ROAM_DEFAULT,
    "char": {
        "name": "大肥鱼",
        "asset": "",          # 当前场景精灵（上传后的文件名）
        "reference": "",      # 参考原图（仅作留存，不参与绘制）
        "scale": 1.0,
        "offset_y": 0,
        "tint": "auto",
        "shadow_alpha": 0.26,   # 脚底接触阴影不透明度
        "tint_strength": 0.34,  # 环境色融合强度（0–1）
        "bubble_offset": 6,     # 气泡相对头顶的偏移（px）
        "pixel_strength": 0.55,  # 像素化强度（0.15 细腻 – 1 粗糙），只做画风适配
    },
    "render": dict(RENDER_DEFAULT),
    "bubble": dict(BUBBLE_DEFAULT),
    "talk": {"on_arrive": True, "cooldown": 240},
    "bubbles": [],
    "last_umo": "",
    "pos": {"x": 6, "y": 6, "dir": "down"},
    "pending": {"target": "", "x": None, "y": None, "ts": 0, "pose": "auto"},
    "prefs": {"nickname": "", "fav_point": ""},
    "log": [],
}

KINDS = ("bed", "wardrobe", "desk", "sofa", "table", "shelf", "plant", "rug", "lamp", "custom")

HOUSE_PROMPT = (
    "【小屋】主人屋里有一间等距小屋，里面住着「大肥鱼」（就是你自己）。"
    "你可以用 house_control 工具让她回屋、去某个点位、开/关自动走动或停下；"
    "点位名由主人自定义（例如「懒人角落」「展示架」），不确定有哪些点位时先传 action=list。"
    "不要在回复里输出坐标或内部状态，只说人话（例如「已经在懒人角落窝好了」）。"
)

_IMG_RE = re.compile(r"^data:image/(png|jpe?g|webp);base64,", re.I)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _int(value: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return fallback


def _float(value: Any, fallback: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return fallback


def _color(value: Any, fallback: str) -> str:
    """只接受 #RGB / #RRGGBB / rgba(...)，避免把任意字符串塞进画布样式。"""
    raw = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", raw):
        return raw
    if re.fullmatch(r"rgba?\([0-9.,\s%\)]+\)", raw):
        return raw
    return fallback


POSES = ("auto", "stand", "sit", "sofa", "desk", "gaze")


def _pose(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in POSES else "auto"


def _clean_id(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(text or ""))[:32]
    return cleaned or fallback


class IsoHouse:
    """小屋状态读写。settings 由主插件持有，这里只做 section 的合并与校验。"""

    def __init__(
        self,
        data_dir: Path,
        settings: Callable[[], dict[str, Any]],
        save: Callable[[], bool],
        dbg: Callable[[str], None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.asset_dir = self.data_dir / "isohouse"
        self._settings = settings
        self._save = save
        self._dbg = dbg or (lambda _m: None)
        self.asset_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 基础读写 ---------------- #
    def cfg(self) -> dict[str, Any]:
        raw = (self._settings() or {}).get("isohouse")
        return self.sanitize(raw)

    def sanitize(self, raw: Any) -> dict[str, Any]:
        data = copy.deepcopy(ISO_DEFAULTS)
        if not isinstance(raw, dict):
            return data

        data["enabled"] = bool(raw.get("enabled", True))
        bg = str(raw.get("bg") or "night-sea")
        data["bg"] = bg if bg in BG_PRESETS else "night-sea"

        furniture: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw.get("furniture") or []:
            if not isinstance(item, dict):
                continue
            fid = _clean_id(item.get("id"), f"f{len(furniture) + 1}")
            if fid in seen:
                continue
            seen.add(fid)
            kind = str(item.get("kind") or "custom")
            furniture.append(
                {
                    "id": fid,
                    "kind": kind if kind in KINDS else "custom",
                    "x": _int(item.get("x"), 1, 0, GRID_W - 1),
                    "y": _int(item.get("y"), 1, 0, GRID_H - 1),
                    "w": _int(item.get("w"), 1, 1, 4),
                    "h": _int(item.get("h"), 1, 1, 3),
                    "label": str(item.get("label") or "")[:24],
                    "block": bool(item.get("block", kind != "rug")),
                    "interactive": bool(item.get("interactive", False)),
                }
            )
            if len(furniture) >= 40:
                break
        data["furniture"] = furniture or copy.deepcopy(FURNITURE_DEFAULT)

        points: list[dict[str, Any]] = []
        names: set[str] = set()
        for item in raw.get("points") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()[:16]
            if not name or name in names:
                continue
            names.add(name)
            lines = [str(x)[:60] for x in (item.get("lines") or []) if str(x).strip()][:8]
            points.append(
                {
                    "id": _clean_id(item.get("id"), f"p{len(points) + 1}"),
                    "name": name,
                    "x": _int(item.get("x"), 6, 0, GRID_W - 1),
                    "y": _int(item.get("y"), 6, 0, GRID_H - 1),
                    "note": str(item.get("note") or "")[:40],
                    "interactive": bool(item.get("interactive", False)),
                    "lines": lines,
                    "pose": _pose(item.get("pose")),
                }
            )
            if len(points) >= 30:
                break
        data["points"] = points or copy.deepcopy(POINTS_DEFAULT)

        roam = raw.get("roam") if isinstance(raw.get("roam"), dict) else {}
        base = dict(ROAM_DEFAULT)
        base.update(roam)
        regions: list[list[int]] = []
        for r in base.get("regions") or []:
            if isinstance(r, (list, tuple)) and len(r) == 4:
                regions.append(
                    [
                        _int(r[0], 1, 0, GRID_W - 1),
                        _int(r[1], 2, 0, GRID_H - 1),
                        _int(r[2], 4, 1, GRID_W),
                        _int(r[3], 4, 1, GRID_H),
                    ]
                )
        base["regions"] = regions or copy.deepcopy(ROAM_DEFAULT["regions"])
        base["enabled"] = bool(base.get("enabled"))
        base["stay_min"] = _int(base.get("stay_min"), 3, 1, 120)
        base["stay_max"] = max(base["stay_min"], _int(base.get("stay_max"), 10, 1, 600))
        base["speed"] = round(_float(base.get("speed"), 2.4, 0.6, 8.0), 2)
        base["random_weight"] = round(_float(base.get("random_weight"), 0.55, 0.0, 1.0), 2)
        base["prefer_interactive"] = bool(base.get("prefer_interactive", True))
        data["roam"] = base

        ch = raw.get("char") if isinstance(raw.get("char"), dict) else {}
        data["char"] = {
            "name": str(ch.get("name") or "大肥鱼")[:16],
            "asset": self._safe_asset(ch.get("asset")),
            "reference": self._safe_asset(ch.get("reference")),
            "scale": round(_float(ch.get("scale"), 1.0, 0.5, 2.0), 2),
            "offset_y": _int(ch.get("offset_y"), 0, -60, 60),
            "tint": str(ch.get("tint") or "auto")[:12],
            "shadow_alpha": round(_float(ch.get("shadow_alpha"), 0.26, 0.0, 0.8), 2),
            "tint_strength": round(_float(ch.get("tint_strength"), 0.34, 0.0, 1.0), 2),
            "bubble_offset": _int(ch.get("bubble_offset"), 6, -40, 120),
            "pixel_strength": round(_float(ch.get("pixel_strength"), 0.55, 0.15, 1.0), 2),
        }

        shade_keys = raw.get("render") if isinstance(raw.get("render"), dict) else {}
        render = dict(RENDER_DEFAULT)
        render["grid_alpha"] = round(_float(shade_keys.get("grid_alpha"), 0.06, 0.0, 0.5), 3)
        render["point_color"] = _color(shade_keys.get("point_color"), RENDER_DEFAULT["point_color"])
        render["point_color_alt"] = _color(shade_keys.get("point_color_alt"), RENDER_DEFAULT["point_color_alt"])
        render["light_dir"] = "left" if str(shade_keys.get("light_dir")) == "left" else "right"
        render["moonlight"] = round(_float(shade_keys.get("moonlight"), 0.55, 0.0, 1.0), 2)
        render["window_open"] = bool(shade_keys.get("window_open", True))
        data["render"] = render

        bub = raw.get("bubble") if isinstance(raw.get("bubble"), dict) else {}
        bubble = dict(BUBBLE_DEFAULT)
        bubble["ttl"] = _int(bub.get("ttl"), 8, 1, 60)
        bubble["bg"] = _color(bub.get("bg"), BUBBLE_DEFAULT["bg"])
        bubble["color"] = _color(bub.get("color"), BUBBLE_DEFAULT["color"])
        bubble["max_width"] = _int(bub.get("max_width"), 232, 120, 420)
        bubble["alpha"] = round(_float(bub.get("alpha"), 0.86, 0.2, 1.0), 2)
        bubble["offset"] = _int(bub.get("offset"), 0, -80, 160)
        data["bubble"] = bubble

        tk = raw.get("talk") if isinstance(raw.get("talk"), dict) else {}
        data["talk"] = {
            "on_arrive": bool(tk.get("on_arrive", True)),
            "cooldown": _int(tk.get("cooldown"), 240, 30, 3600),
        }

        bubbles = []
        for item in raw.get("bubbles") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            bubbles.append(
                {
                    "id": str(item.get("id") or "")[:24] or f"b{len(bubbles) + 1}",
                    "text": text[:180],
                    "ts": _float(item.get("ts"), time.time(), 0, 2**31),
                    "origin": str(item.get("origin") or "panel")[:12],
                }
            )
        data["bubbles"] = bubbles[-8:]
        data["last_umo"] = str(raw.get("last_umo") or "")[:120]

        pos = raw.get("pos") if isinstance(raw.get("pos"), dict) else {}
        data["pos"] = {
            "x": _int(pos.get("x"), 6, 0, GRID_W - 1),
            "y": _int(pos.get("y"), 6, 0, GRID_H - 1),
            "dir": str(pos.get("dir") or "down")[:8],
        }

        pending = raw.get("pending") if isinstance(raw.get("pending"), dict) else {}
        data["pending"] = {
            "target": str(pending.get("target") or "")[:16],
            "pose": _pose(pending.get("pose")),
            "x": None if pending.get("x") is None else _int(pending.get("x"), 0, 0, GRID_W - 1),
            "y": None if pending.get("y") is None else _int(pending.get("y"), 0, 0, GRID_H - 1),
            "ts": _int(pending.get("ts"), 0, 0, 2**31),
        }

        prefs = raw.get("prefs") if isinstance(raw.get("prefs"), dict) else {}
        data["prefs"] = {
            "nickname": str(prefs.get("nickname") or "")[:16],
            "fav_point": str(prefs.get("fav_point") or "")[:16],
        }

        logs = []
        for item in raw.get("log") or []:
            if not isinstance(item, dict):
                continue
            logs.append(
                {
                    "ts": str(item.get("ts") or _now())[:19],
                    "kind": str(item.get("kind") or "info")[:12],
                    "text": str(item.get("text") or "")[:160],
                    "actor": str(item.get("actor") or "大肥鱼")[:16],
                }
            )
        data["log"] = logs[-MAX_LOG:]
        return data

    def _safe_asset(self, name: Any) -> str:
        raw = str(name or "").strip()
        if not raw:
            return ""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", raw):
            return ""
        return raw if (self.asset_dir / raw).exists() else ""

    def write(self, data: dict[str, Any]) -> bool:
        settings = self._settings()
        settings["isohouse"] = self.sanitize(data)
        return bool(self._save())

    # ---------------- 日志 ---------------- #
    def add_log(self, kind: str, text: str, actor: str = "大肥鱼") -> dict[str, Any]:
        data = self.cfg()
        entry = {"ts": _now(), "kind": str(kind)[:12], "text": str(text)[:160], "actor": str(actor)[:16]}
        data["log"] = (data.get("log") or [])[-(MAX_LOG - 1):] + [entry]
        self.write(data)
        return entry

    def logs(self, limit: int = 60) -> list[dict[str, Any]]:
        return (self.cfg().get("log") or [])[-max(1, min(MAX_LOG, limit)):]

    # ---------------- 素材 ---------------- #
    def upload(self, data_url: str, role: str = "char") -> dict[str, Any]:
        if not isinstance(data_url, str) or not _IMG_RE.match(data_url):
            raise ValueError("只支持 PNG / JPEG / WebP 图片")
        payload = _IMG_RE.sub("", data_url)
        try:
            raw = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("图片数据损坏") from exc
        if not raw:
            raise ValueError("图片为空")
        if len(raw) > MAX_ASSET_BYTES:
            raise ValueError("图片超过 6 MB，请先压缩")

        ext = ".png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            ext = ".webp"
        name = f"{role}-{int(time.time())}{ext}"
        path = self.asset_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)

        data = self.cfg()
        key = "reference" if role == "reference" else "asset"
        data["char"][key] = name
        self.write(data)
        self.add_log("素材", f"更新了{'参考图' if role == 'reference' else '角色精灵'}")
        return {"asset": name, "bytes": len(raw)}

    def asset_data_url(self, name: str) -> str:
        safe = self._safe_asset(name)
        if not safe:
            return ""
        path = self.asset_dir / safe
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        suffix = path.suffix.lower().lstrip(".") or "png"
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "webp": "webp"}.get(suffix, "png")
        return f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii")

    # ---------------- 行为 ---------------- #
    def point_by_name(self, name: str) -> dict[str, Any] | None:
        query = str(name or "").strip()
        if not query:
            return None
        points = self.cfg().get("points") or []
        for item in points:
            if item["name"] == query:
                return item
        for item in points:  # 宽松匹配：包含即命中（「去角落」→ 懒人角落）
            if query in item["name"] or item["name"] in query:
                return item
        return None

    def legal_spot(self, x: int, y: int) -> tuple[int, int]:
        """站位校验：落在挡路家具里时，向外找最近的可站格。

        与原前端 nearestWalkable 的规则一致（四周扩散、不看对角线顺序），
        这样服务端下发的目标与前端 A* 的起点/终点永远可达。
        """
        data = self.cfg()
        blocked: set[tuple[int, int]] = set()
        for f in data["furniture"]:
            if not f.get("block", f.get("kind") != "rug"):
                continue
            for dx in range(int(f.get("w") or 1)):
                for dy in range(int(f.get("h") or 1)):
                    blocked.add((f["x"] + dx, f["y"] + dy))
        x = _int(x, 6, 0, GRID_W - 1)
        y = _int(y, 6, 0, GRID_H - 1)
        if (x, y) not in blocked:
            return x, y
        for r in range(1, 8):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < GRID_W and 0 <= ny < GRID_H and (nx, ny) not in blocked:
                        return nx, ny
        return x, y

    def go(self, name: str = "", x: int | None = None, y: int | None = None, actor: str = "主人") -> dict[str, Any]:
        data = self.cfg()
        target_name = ""
        if name:
            point = self.point_by_name(name)
            if not point:
                names = "、".join(p["name"] for p in data.get("points") or []) or "（还没设点位）"
                return {"ok": False, "message": f"屋里没有「{name}」这个点位。现在有的：{names}"}
            x, y, target_name = point["x"], point["y"], point["name"]
        if x is None or y is None:
            return {"ok": False, "message": "缺少目标位置"}
        x, y = self.legal_spot(int(x), int(y))
        point = self.point_by_name(target_name) if target_name else None
        pose = (point or {}).get("pose") or "auto"
        data["pending"] = {"target": target_name, "x": x, "y": y, "ts": int(time.time()), "pose": pose}
        self.write(data)
        self.add_log("指令", f"{actor}让她去{target_name or f'({x},{y})'}", actor=actor)
        return {"ok": True, "message": target_name or f"({x},{y})", "x": x, "y": y, "target": target_name}

    def roam(self, on: bool, actor: str = "主人") -> dict[str, Any]:
        data = self.cfg()
        data["roam"]["enabled"] = bool(on)
        if not on:
            data["pending"] = {"target": "", "x": None, "y": None, "ts": 0}
        self.write(data)
        self.add_log("指令", f"{actor}{'开启' if on else '关闭'}了自动走动", actor=actor)
        return {"ok": True, "enabled": bool(on)}

    def arrive(self, x: int, y: int, target: str = "") -> None:
        data = self.cfg()
        data["pos"] = {"x": _int(x, 6, 0, GRID_W - 1), "y": _int(y, 6, 0, GRID_H - 1), "dir": data["pos"].get("dir", "down")}
        data["pending"] = {"target": "", "x": None, "y": None, "ts": 0, "pose": "auto"}
        if target and target in [p["name"] for p in data.get("points") or []]:
            data["prefs"]["fav_point"] = target
        self.write(data)
        self.add_log("移动", f"到达{target or f'({x},{y})'}")

    # ---------------- 气泡 ---------------- #
    def say(self, text: str, origin: str = "panel", actor: str = "大肥鱼") -> dict[str, Any]:
        """往画布上推一条气泡（面板对话与 QQ 对话共用同一个队列）。"""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "message": "没有内容"}
        data = self.cfg()
        self._bubble_seq = int(getattr(self, "_bubble_seq", 0)) + 1
        entry = {
            "id": "b%d-%d" % (int(time.time() * 1000), self._bubble_seq),
            "text": text[:180],
            "ts": time.time(),
            "origin": str(origin)[:12],
        }
        data["bubbles"] = (data.get("bubbles") or [])[-7:] + [entry]
        self.write(data)
        self.add_log("对话", text[:120], actor=actor)
        return {"ok": True, "bubble": entry, "log": self.logs(1)}

    def bubbles(self, since: float = 0) -> list[dict[str, Any]]:
        data = self.cfg()
        out = []
        for item in data.get("bubbles") or []:
            if float(item.get("ts") or 0) > float(since or 0):
                out.append(item)
        return out

    # ---------------- 记忆联动 ---------------- #
    def memory_facts(self) -> list[str]:
        """给会话日记本/上下文用的短事实，不复述坐标噪声。"""
        data = self.cfg()
        facts = [f"小屋里住着{data['char']['name']}，房间是等距小屋（{data['bg']} 背景）"]
        if data["prefs"].get("fav_point"):
            facts.append(f"她最常待的点位是「{data['prefs']['fav_point']}」")
        if data["roam"].get("enabled"):
            facts.append("小屋的自动走动现在开着")
        if data["points"]:
            facts.append("小屋点位：" + "、".join(p["name"] for p in data["points"]))
        labels = [f.get("label") for f in data.get("furniture") or [] if f.get("label")]
        if labels:
            facts.append("家具：" + "、".join(labels[:10]))
        return facts

    def context_block(self) -> str:
        data = self.cfg()
        if not data.get("enabled"):
            return ""
        pos = data["pos"]
        where = next((p["name"] for p in data["points"] if p["x"] == pos["x"] and p["y"] == pos["y"]), "")
        here = where or "({},{})".format(pos["x"], pos["y"])
        bits = [f"位置：{here}", f"自动走动：{'开' if data['roam']['enabled'] else '关'}"]
        cur = next((p for p in data["points"] if p["x"] == pos["x"] and p["y"] == pos["y"]), None)
        if cur and cur.get("note"):
            bits.append(f"这里：{cur['note']}")
        act = [p["name"] for p in data["points"] if p.get("interactive")]
        if act:
            bits.append("能互动的点位：" + "、".join(act))
        if data["prefs"].get("fav_point"):
            bits.append(f"偏爱：{data['prefs']['fav_point']}")
        recent = [e["text"] for e in (data.get("log") or [])[-3:]]
        if recent:
            bits.append("最近：" + "；".join(recent))
        return "【小屋现状】" + "；".join(bits)

    # ---------------- 面板载荷 ---------------- #
    def overview(self) -> dict[str, Any]:
        data = self.cfg()
        return {
            "state": {
                "enabled": data["enabled"],
                "bg": data["bg"],
                "grid": data["grid"],
                "furniture": data["furniture"],
                "points": data["points"],
                "roam": data["roam"],
                "char": data["char"],
                "pos": data["pos"],
                "pending": data["pending"],
                "prefs": data["prefs"],
                "render": data["render"],
                "bubble": data["bubble"],
                "bubbles": data["bubbles"],
                "last_umo": data["last_umo"],
            },
            "char_url": self.asset_data_url(data["char"].get("asset") or ""),
            "reference_url": self.asset_data_url(data["char"].get("reference") or ""),
            "log": data.get("log") or [],
            "prompt": HOUSE_PROMPT,
            "presets": list(BG_PRESETS),
        }

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        data = self.cfg()
        for key in ("bg", "furniture", "points", "roam", "char", "prefs", "enabled", "pos", "render", "bubble", "last_umo"):
            if key in body:
                data[key] = body[key]
        if "char" in body and isinstance(body["char"], dict):
            merged = dict(self.cfg()["char"])
            merged.update({k: body["char"][k] for k in body["char"] if k in merged})
            data["char"] = merged
        for key in ("render", "bubble"):
            if isinstance(body.get(key), dict):
                merged2 = dict(self.cfg()[key])
                merged2.update({k: body[key][k] for k in body[key] if k in merged2})
                data[key] = merged2
        if not self.write(data):
            return {"ok": False, "message": "写入设置失败"}
        self.add_log("配置", "更新了小屋设置", actor="主人")
        return {"ok": True, "state": self.overview()["state"]}

    def reset(self) -> dict[str, Any]:
        data = self.cfg()
        data["furniture"] = copy.deepcopy(FURNITURE_DEFAULT)
        data["points"] = copy.deepcopy(POINTS_DEFAULT)
        data["roam"] = copy.deepcopy(ROAM_DEFAULT)
        data["bg"] = "night-sea"
        data["pos"] = {"x": 6, "y": 6, "dir": "down"}
        data["pending"] = {"target": "", "x": None, "y": None, "ts": 0}
        # 渲染与气泡参数一起回到默认（这是「一键恢复默认」的语义），素材与日志保留
        data["render"] = copy.deepcopy(RENDER_DEFAULT)
        data["bubble"] = copy.deepcopy(BUBBLE_DEFAULT)
        data["bubbles"] = []
        self.write(data)
        self.add_log("配置", "恢复默认布局", actor="主人")
        return {"ok": True, "state": self.overview()["state"]}