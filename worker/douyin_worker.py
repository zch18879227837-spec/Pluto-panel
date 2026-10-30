# -*- coding: utf-8 -*-
"""抖音游客浏览 worker：独立子进程运行 Playwright，避免阻塞 AstrBot 事件循环。

只做三件事：打开抖音精选 → 滚动收集视频卡片 → 按停留时长模拟观看。
不登录、不点赞、不评论、不搜索（搜索页会触发滑块/图形验证码）。

v1.4.0 风控修复版：
- 命中安全验证立刻退出，交由插件侧进入 30 分钟冷却；
- 复用固定 profile 目录（不是每次全新无痕），并随机化指纹；
- 停留 8~20 秒、下滑间隔 2.5~6 秒，下滑前带鼠标微移。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import time

BROWSERS_PATH = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()

UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
)

KILL_MODAL = """() => {
    let n = 0;
    document.querySelectorAll('[class*="douyin_login"]').forEach(el => { el.remove(); n++; });
    document.querySelectorAll('div[class*="mask"],div[class*="Mask"]').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width > 800 && r.height > 600) { el.remove(); n++; }
    });
    return n;
}"""

# 反自动化：隐藏 webdriver、补 plugins/languages、随机 WebGL 厂商
STEALTH = """(() => {
    const define = (obj, key, value) => {
        try { Object.defineProperty(obj, key, {get: () => value, configurable: true}); } catch (e) {}
    };
    define(navigator, 'webdriver', undefined);
    define(navigator, 'languages', ['zh-CN', 'zh', 'en']);
    define(navigator, 'plugins', [1, 2, 3, 4, 5]);
    define(navigator, 'hardwareConcurrency', [4, 8, 12, 16][Math.floor(Math.random() * 4)]);
    define(navigator, 'deviceMemory', [4, 8][Math.floor(Math.random() * 2)]);
    if (window.chrome) define(window.chrome, 'runtime', {});
    const vendors = ['Google Inc. (Intel)', 'Google Inc. (NVIDIA)', 'Google Inc. (AMD)'];
    const renderers = ['ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)',
                       'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0)',
                       'ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0)'];
    const vi = Math.floor(Math.random() * vendors.length);
    const patch = (proto, name, value) => {
        try {
            const orig = proto[name];
            proto[name] = function () {
                if (arguments.length === 0) return value;
                return orig.apply(this, arguments);
            };
        } catch (e) {}
    };
    try {
        const origGetParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function (p) {
            if (p === 37445) return vendors[vi];
            if (p === 37446) return renderers[vi];
            return origGetParameter.apply(this, arguments);
        };
    } catch (e) {}
    try {
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) => (
            params && params.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : origQuery(params)
        );
    } catch (e) {}
})();"""

READ_CARDS = """() => [...document.querySelectorAll('[class*="discover-video-card-item"]')].map(el => {
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    const m = t.match(/^([\\d:]+)\\s+([\\d.]+万?)\\s+(.*)$/);
    const rawId = el.getAttribute('data-aweme-id') || el.getAttribute('id') || '';
    return {
        vid: /^\\d{8,}$/.test(rawId) ? rawId : '',
        raw: t,
        duration: m ? m[1] : '',
        title: (m ? m[3] : t).slice(0, 120),
    };
}).filter(c => c.title && c.title.length > 4)"""

CHECK_RISK = """() => {
    // 只认真正出现在屏幕上的验证框：页面常驻的隐藏 captcha 容器不算，登录弹窗里的“验证码登录”字样也不算
    const visible = el => {
        const r = el.getBoundingClientRect();
        return el.offsetParent !== null && r.width > 80 && r.height > 80;
    };
    const caps = [...document.querySelectorAll(
        '[class*="captcha"],[id*="captcha"],iframe[src*="captcha"],[class*="verify-bar"],[class*="slide-verify"]')];
    if (caps.some(visible)) return true;
    const txt = (document.body.innerText || '').replace(/\\s+/g, '');
    return /点击.{0,6}形状|形状相同|拖动.{0,4}滑块|滑动.{0,2}验证|请完成安全验证|安全验证失败/.test(txt);
}"""


def _card_key(card: dict) -> str:
    """去重键：优先视频唯一 ID；抓不到 ID 时退回整段文本的哈希（避免标题相似误合并）。"""
    vid = str(card.get("vid") or "").strip()
    if vid:
        return "vid:" + vid
    raw = str(card.get("raw") or "")
    return "raw:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _dur_seconds(text: str) -> int:
    parts = [int(p) for p in text.split(":") if p.isdigit()] if text else []
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


def _dwell_seconds(duration: int, lo: float = 8.0, hi: float = 20.0) -> int:
    """单视频停留 lo~hi 秒（由风控档位下发）；8 秒是硬性底线，任何档位都不破。"""
    lo = max(8.0, float(lo or 8.0))
    hi = max(lo, float(hi or 20.0))
    dwell = random.uniform(lo, hi)
    if duration > 0:
        # 视频本身很短时也不低于底线；长视频按区间上限收住
        dwell = min(dwell, max(8.0, float(duration)))
    return int(round(max(8.0, dwell)))


async def _human_move(page) -> None:
    """下滑前先做两三次小幅度鼠标移动，模拟真人手部动作。"""
    for _ in range(random.randint(2, 3)):
        await page.mouse.move(
            random.randint(240, 1300),
            random.randint(160, 760),
            steps=random.randint(6, 14),
        )
        await page.wait_for_timeout(random.randint(120, 380))


async def run_session(args) -> dict:
    if BROWSERS_PATH:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", BROWSERS_PATH)
    from playwright.async_api import async_playwright

    started = time.time()
    items: dict[str, dict] = {}
    minutes = 0.0
    risk = False
    error = ""
    profile_dir = os.path.abspath(args.profile_dir) if args.profile_dir else ""
    os.makedirs(profile_dir, exist_ok=True) if profile_dir else None

    async with async_playwright() as pw:
        ua = random.choice(UA_POOL)
        viewport = {"width": random.randint(1280, 1600), "height": random.randint(760, 1000)}
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage", "--no-sandbox", "--lang=zh-CN",
            "--mute-audio", "--disable-infobars", "--no-default-browser-check",
            "--disable-features=Translate,BackForwardCache",
        ]
        ctx = None
        try:
            ctx = await pw.chromium.launch_persistent_context(
                profile_dir or None,
                headless=True,
                args=launch_args,
                user_agent=ua,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport=viewport,
                device_scale_factor=random.choice([1, 1.25]),
                color_scheme=random.choice(["light", "light", "dark"]),
                java_script_enabled=True,
                ignore_https_errors=True,
            )
            await ctx.add_init_script(STEALTH)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(random.randint(2600, 4200))
            await page.evaluate(KILL_MODAL)
            await _human_move(page)

            idle_rounds = 0
            while True:
                if time.time() - started >= args.max_minutes * 60:
                    break
                if len(items) >= args.max_videos:
                    break
                if await page.evaluate(CHECK_RISK):
                    risk = True
                    break

                cards = await page.evaluate(READ_CARDS)
                fresh = [c for c in cards if _card_key(c) not in items]
                if not fresh:
                    idle_rounds += 1
                    if idle_rounds > 6:
                        break
                else:
                    idle_rounds = 0
                for card in fresh[:2]:
                    key = _card_key(card)
                    if key in items:
                        continue
                    if len(items) >= args.max_videos or time.time() - started >= args.max_minutes * 60:
                        break
                    await page.evaluate(KILL_MODAL)
                    dur = _dur_seconds(card["duration"])
                    dwell = _dwell_seconds(dur, args.dwell_min, args.dwell_max)
                    await page.wait_for_timeout(dwell * 1000)
                    if await page.evaluate(CHECK_RISK):
                        risk = True
                        break
                    items[key] = {
                        "video_id": str(card.get("vid") or ""),
                        "title": card["title"],
                        "duration": dur,
                        "watched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "dwell": dwell,
                    }
                if risk:
                    break

                await _human_move(page)
                await page.mouse.wheel(0, random.randint(520, 940))
                await page.wait_for_timeout(int(random.uniform(2.5, 6.0) * 1000))
            minutes = round((time.time() - started) / 60.0, 1)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            try:
                if ctx is not None:
                    await ctx.close()
            except Exception:  # noqa: BLE001
                pass

    return {
        "ok": not risk and not error,
        "error": error,
        "risk": risk,
        "minutes": minutes,
        "videos": len(items),
        "items": list(items.values()),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# CLI entry: run one guest browsing session, then emit the JSON report to --out or stdout.
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-videos", type=int, default=10)
    ap.add_argument("--max-minutes", type=float, default=8.0)
    ap.add_argument("--dwell-min", type=float, default=8.0)
    ap.add_argument("--dwell-max", type=float, default=20.0)
    ap.add_argument("--profile-dir", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    result = asyncio.run(run_session(a))
    payload = json.dumps(result, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
    else:
        print(payload)


if __name__ == "__main__":
    main()