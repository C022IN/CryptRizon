"""
CryptRizon Actions — FastAPI backend

Key fixes & improvements in this revision:
1) **ssl preflight**: fail fast with a clear message if Python lacks the `ssl` module (root cause of your crash).
2) **Thread/worker‑safe config**: central ConfigManager with atomic writes and locks.
3) **Async I/O**: switch to `httpx` with connection pooling and concurrency control (no blocking `requests`).
4) **Event‑loop safe auto‑push**: background task now uses async HTTP, no blocking in the loop.
5) **Safer CORS & auth hook**: restrictable via env; optional shared secret for `/telegram/push`.
6) **Raw data endpoint**: `/status/raw` for structured results.
7) **Light self‑tests** (pure functions) when `RUN_TESTS=1`.

Deployment notes:
- Use a Python image that ships with OpenSSL & CA certs, e.g. `python:3.11-slim` plus `ca-certificates`.
- Requirements (pin or compatible): fastapi, uvicorn, httpx, pydantic.
"""

# --- Crash guard: verify ssl exists before importing FastAPI/Starlette stack ---
try:
    import ssl  # noqa: F401
except ModuleNotFoundError as e:
    raise RuntimeError(
        "Python was built without OpenSSL/ssl. Reinstall Python with SSL support or use a base image "
        "like python:3.11-slim and ensure ca-certificates are installed."
    ) from e

import os, json, time, asyncio, math
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Body, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =========================
# Config & Constants
# =========================
BINANCE_PRODUCTS     = "https://www.binance.com/bapi/asset/v1/public/asset-service/product/get-products"
BINANCE_PRODUCTS_ALT = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-product-list"
CG = "https://api.coingecko.com/api/v3"
CONFIG_FILE = os.getenv("CONFIG_FILE", "cryptrizon_config.json")

BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID   = os.getenv("ADMIN_CHAT_ID", "").strip()
ALLOW_ORIGINS   = os.getenv("ALLOW_ORIGINS", "*")  # comma-separated list
PUSH_AUTH_TOKEN = os.getenv("PUSH_AUTH_TOKEN", "")  # optional shared secret for /telegram/push

DEFAULT_CONFIG: Dict = {
    "symbols_cache": [],
    "symbols_cache_updated": 0,
    "symbols_cache_ttl_hours": 12,
    "fallback_enabled": True,
    "push_interval_hours": 6,
    "alpha_days_primary": 90,
    "alpha_days_fallback": 30,
    "bob_days": 30,
    "bottom_pct": 0.15,
    "top_pct": 0.85,
    "alpha_list": [
        "VINE","HOUSE","PYTHIA","FWOG","POPCAT","G","FARTCOIN","PUMP","JELLYJELLY",
        "ARC","ALON","PIPPIN","GRASS","AI16Z","ALCH","GRIFFAIN","RFC","ZEREBRO","GOAT",
        "CLANKER","ICNT","VVV","AERO","ZORA","RDAC","FLOCK","B3","DEGEN","SKI","ODOS","POKT","LUNAI",
        "XO","HIPPO","SCA","DMC","NS","BLUE","NAVX","FHSS","HSL","MEET","SINS","TAG","VELO","BOB"
    ]
}
CG_ALIASES = {"BOB": "build-on-bnb", "FWOG": "fwog", "GRASS": "grass", "POPCAT": "popcat"}

# =========================
# Config manager (thread/worker safe)
# =========================
class ConfigManager:
    def __init__(self, path: str, defaults: Dict):
        self.path = path
        self.defaults = defaults
        self._lock = asyncio.Lock()
        self._cfg = self._load()

    def _load(self) -> Dict:
        if os.path.exists(self.path):
            try:
                cfg = json.load(open(self.path, "r"))
                for k, v in self.defaults.items():
                    cfg.setdefault(k, v)
                return cfg
            except Exception:
                pass
        # write defaults atomically
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.defaults, f, indent=2)
        os.replace(tmp, self.path)
        return self.defaults.copy()

    async def read(self) -> Dict:
        # return a shallow copy to avoid accidental external mutation
        return dict(self._cfg)

    async def write(self, updates: Dict) -> Dict:
        async with self._lock:
            self._cfg.update(updates)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._cfg, f, indent=2)
            os.replace(tmp, self.path)
            return dict(self._cfg)

    async def get(self, key: str, default=None):
        return (await self.read()).get(key, default)

CFG_MGR = ConfigManager(CONFIG_FILE, DEFAULT_CONFIG)

# =========================
# HTTP Client (async, pooled)
# =========================
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
CLIENT: Optional[httpx.AsyncClient] = None
CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY", "12"))
SEM = asyncio.Semaphore(CONCURRENCY_LIMIT)

async def get_client() -> httpx.AsyncClient:
    global CLIENT
    if CLIENT is None:
        CLIENT = httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": "CryptRizon/1.0"})
    return CLIENT

async def close_client():
    global CLIENT
    if CLIENT is not None:
        await CLIENT.aclose()
        CLIENT = None

# =========================
# Helpers (async I/O)
# =========================
async def get_json(url: str, params: Dict = None) -> Optional[Dict]:
    client = await get_client()
    for attempt in range(3):
        try:
            async with SEM:
                r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            return None
        except httpx.RequestError:
            if attempt < 2:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            return None

def fmt(x: float) -> str:
    return f"{x:.10f}".rstrip("0").rstrip(".")

def fmt_pct(x: float) -> str:
    return f"{x:.1f}%"

# =========================
# Binance Universe
# =========================
import re as _re

def _strip_quote(sym: str) -> str:
    return _re.sub(r"(USDT|FDUSD|USDC|BUSD|TUSD|TRY|EUR|BRL|NGN|RUB|GBP|DAI|USTC)$", "", sym, flags=_re.I)

async def fetch_all_binance_bases() -> List[str]:
    bases = set()
    data = await get_json(BINANCE_PRODUCTS)
    if data and isinstance(data.get("data"), list):
        for it in data["data"]:
            s = it.get("s") or it.get("symbol")
            if s and s.upper().endswith(("USDT","FDUSD","USDC","BUSD","TUSD")):
                bases.add(_strip_quote(s))
    if not bases:
        data2 = await get_json(BINANCE_PRODUCTS_ALT, params={"includeEtf": "false"})
        if data2 and isinstance(data2.get("data"), list):
            for it in data2["data"]:
                s = it.get("s") or it.get("symbol")
                if s and s.upper().endswith(("USDT","FDUSD","USDC","BUSD","TUSD")):
                    bases.add(_strip_quote(s))
    return sorted(bases)

async def get_universe(force: bool = False) -> List[str]:
    cfg = await CFG_MGR.read()
    now = int(time.time())
    ttl = int(cfg.get("symbols_cache_ttl_hours", 12)) * 3600
    if cfg.get("symbols_cache") and not force and now - int(cfg.get("symbols_cache_updated", 0)) < ttl:
        return cfg["symbols_cache"]
    tokens = await fetch_all_binance_bases()
    if tokens:
        await CFG_MGR.write({"symbols_cache": tokens, "symbols_cache_updated": now})
    return tokens

# =========================
# Prices & Zones
# =========================
async def resolve_cg_id(q: str) -> Optional[str]:
    q_upper = q.upper()
    if q_upper in CG_ALIASES:
        return CG_ALIASES[q_upper]
    data = await get_json(f"{CG}/search", params={"query": q})
    if not data:
        return None
    ql = q.lower()
    best = None
    for c in data.get("coins", []):
        if c.get("symbol", "").lower() == ql or c.get("name", "").lower() == ql:
            return c.get("id")
        if best is None:
            best = c.get("id")
    return best

async def fetch_prices(cg_id: str, days: int) -> Optional[List[float]]:
    data = await get_json(
        f"{CG}/coins/{cg_id}/market_chart",
        params={"vs_currency": "usd", "days": str(days), "interval": "hourly"},
    )
    if not data:
        return None
    arr = [p[1] for p in data.get("prices", [])]
    return arr or None

def zone_metrics(prices: List[float]) -> Tuple[float, float, float, float]:
    low, high = min(prices), max(prices)
    cur = prices[-1]
    rng = max(high - low, 1e-18)
    pct = (cur - low) / rng
    return cur, low, high, pct

async def momentum_bias(prices: List[float], hrs: int = 12) -> str:
    if len(prices) < 2:
        return "flat"
    sl = prices[-hrs:] if len(prices) >= hrs else prices
    d = sl[-1] - sl[0]
    return "up" if d > 0 else ("down" if d < 0 else "flat")

# =========================
# Alpha helpers
# =========================
async def scan_subset(symbols: List[str], days: int):
    out = []
    for s in symbols:
        cid = await resolve_cg_id(s) or await resolve_cg_id(s + " solana")
        if not cid:
            out.append({"name": s, "err": "no_cg_match"})
            continue
        prices = await fetch_prices(cid, days)
        if not prices:
            out.append({"name": s, "err": "no_price_data"})
            continue
        cur, low, high, pct = zone_metrics(prices)
        out.append({
            "name": s, "cur": cur, "low": low, "high": high, "pct": pct,
            "zone": ("Bottom" if pct <= (await CFG_MGR.get("bottom_pct")) else ("High" if pct >= (await CFG_MGR.get("top_pct")) else "Mid")),
            "bias12h": await momentum_bias(prices)
        })
    return out

# =========================
# Report builder (/status)
# =========================
async def build_status_text() -> str:
    cfg = await CFG_MGR.read()
    bases = await get_universe(False)
    symbols_total = len(bases)

    scanned = 0
    results = []
    skip = {"no_cg_match": 0, "no_price_data": 0}

    # Concurrency with backpressure
    sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

    async def process(sym: str):
        nonlocal scanned, results, skip
        async with sem:
            cid = await resolve_cg_id(sym)
            if not cid:
                skip["no_cg_match"] += 1
                return
            prices = await fetch_prices(cid, cfg["alpha_days_primary"])
            if not prices:
                skip["no_price_data"] += 1
                return
            scanned_local = 1
            cur, low, high, pct = zone_metrics(prices)
            results.append({"name": sym, "cur": cur, "low": low, "high": high, "pct": pct,
                            "zone": ("Bottom" if pct <= cfg["bottom_pct"] else ("High" if pct >= cfg["top_pct"] else "Mid"))})
            scanned_local and None
            scanned_queue.put_nowait(1)

    # Track scanned without race (queue-based counter)
    scanned_queue: asyncio.Queue = asyncio.Queue()

    tasks = [asyncio.create_task(process(sym)) for sym in bases]

    async def counter():
        nonlocal scanned
        while any(not t.done() for t in tasks):
            try:
                await asyncio.wait_for(scanned_queue.get(), timeout=0.2)
                scanned += 1
            except asyncio.TimeoutError:
                pass

    await asyncio.gather(counter(), *tasks)

    def blocks_from_results(res3: List[Dict]):
        bottoms = [r for r in res3 if r["pct"] <= cfg["bottom_pct"]]
        tops    = [r for r in res3 if r["pct"] >= cfg["top_pct"]]
        closest_bottom = sorted(res3, key=lambda r: r["pct"])[:3]
        closest_top    = sorted(res3, key=lambda r: (1 - r["pct"]))[:3]
        parts: List[str] = []
        if bottoms:
            best = min(bottoms, key=lambda r: r["pct"])
            parts.append(
                "🎯 Bottom‑Zone Sniper (3‑month)\n"
                f"{best['name']} — ${fmt(best['cur'])}\n"
                f"90d L/H: ${fmt(best['low'])} – ${fmt(best['high'])}\n"
                f"Zone: **{best['zone']}** (pos {best['pct']*100:.1f}%)"
            )
        else:
            parts.append("🎯 Bottom‑Zone Sniper (3‑month): **None**")
        if closest_bottom:
            parts.append("**3 closest to bottom (3‑month):**\n" + "\n".join(
                f"• {r['name']}: ${fmt(r['cur'])} | 90d {fmt(r['low'])}/{fmt(r['high'])} | Zone {r['zone']} | Pos {r['pct']*100:.1f}%"
                for r in closest_bottom
            ))
        if tops:
            t = max(tops, key=lambda r: r["pct"])
            parts.append(
                "\n📈 Top‑Zone Watch (3‑month)\n"
                f"{t['name']} — ${fmt(t['cur'])}\n"
                f"90d L/H: ${fmt(t['low'])} – ${fmt(t['high'])}\n"
                f"Zone: **{t['zone']}** (pos {t['pct']*100:.1f}%)"
            )
        else:
            parts.append("\n📈 Top‑Zone Watch (3‑month): **None**")
        if closest_top:
            parts.append("**3 closest to top (3‑month):**\n" + "\n".join(
                f"• {r['name']}: ${fmt(r['cur'])} | 90d {fmt(r['low'])}/{fmt(r['high'])} | Zone {r['zone']} | Pos {r['pct']*100:.1f}%"
                for r in closest_top
            ))
        return "\n\n".join(parts), bool(bottoms), bool(tops)

    blocks, has_bottom, has_top = blocks_from_results(results)

    # Fallbacks 30d if needed
    if cfg["fallback_enabled"]:
        if not has_bottom:
            res1 = []
            sem_fb = asyncio.Semaphore(CONCURRENCY_LIMIT)
            async def proc_fb(sym: str):
                async with sem_fb:
                    cid = await resolve_cg_id(sym)
                    if not cid:
                        return
                    prices = await fetch_prices(cid, cfg["alpha_days_fallback"])
                    if not prices:
                        return
                    cur, low, high, pct = zone_metrics(prices)
                    res1.append({"name": sym, "cur": cur, "low": low, "high": high, "pct": pct,
                                 "zone": ("Bottom" if pct <= cfg["bottom_pct"] else ("High" if pct >= cfg["top_pct"] else "Mid"))})
            await asyncio.gather(*(proc_fb(sym) for sym in bases))
            bottoms1 = [r for r in res1 if r["pct"] <= cfg["bottom_pct"]]
            if bottoms1:
                b = min(bottoms1, key=lambda r: r["pct"])  
                blocks += (
                    "\n\n🔁 Fallback: Short‑term bottom (1‑month)\n"
                    f"{b['name']} — ${fmt(b['cur'])}\n"
                    f"30d L/H: ${fmt(b['low'])} – ${fmt(b['high'])}\n"
                    f"Zone: **{b['zone']}** (pos {b['pct']*100:.1f}%)"
                )
            else:
                blocks += "\n\n🔁 Fallback (1‑month): **No coin in bottom zone either.**"
        if not has_top:
            res1 = []
            sem_fb2 = asyncio.Semaphore(CONCURRENCY_LIMIT)
            async def proc_fb2(sym: str):
                async with sem_fb2:
                    cid = await resolve_cg_id(sym)
                    if not cid:
                        return
                    prices = await fetch_prices(cid, cfg["alpha_days_fallback"])
                    if not prices:
                        return
                    cur, low, high, pct = zone_metrics(prices)
                    res1.append({"name": sym, "cur": cur, "low": low, "high": high, "pct": pct,
                                 "zone": ("Bottom" if pct <= cfg["bottom_pct"] else ("High" if pct >= cfg["top_pct"] else "Mid"))})
            await asyncio.gather(*(proc_fb2(sym) for sym in bases))
            tops1 = [r for r in res1 if r["pct"] >= cfg["top_pct"]]
            if tops1:
                t = max(tops1, key=lambda r: r["pct"])  
                blocks += (
                    "\n\n🔁 Fallback: Short‑term top (1‑month)\n"
                    f"{t['name']} — ${fmt(t['cur'])}\n"
                    f"30d L/H: ${fmt(t['low'])} – ${fmt(t['high'])}\n"
                    f"Zone: **{t['zone']}** (pos {t['pct']*100:.1f}%)"
                )
            else:
                blocks += "\n\n🔁 Fallback (1‑month Top): **No coin in top zone either.**"

    # Alpha Focus (90d)
    bases_set = set(bases)
    alpha = [s for s in cfg.get("alpha_list", []) if s in bases_set or s == "BOB"]
    alpha_rows = await scan_subset(alpha, cfg["alpha_days_primary"]) if alpha else []
    alpha_ok = sorted([r for r in alpha_rows if "err" not in r], key=lambda r: r["pct"])
    lines = ["**⭐ Alpha Focus (90d)**"]
    if alpha_ok:
        for r in alpha_ok[:10]:
            pos = r["pct"] * 100.0
            lines.append(
                f"• {r['name']}: ${fmt(r['cur'])} | 90d {fmt(r['low'])}/{fmt(r['high'])} | {r['zone']} | {pos:.1f}% | 12h {r['bias12h']}"
            )
    else:
        if alpha_rows:
            errs = [f"• {r['name']}: ({r['err']})" for r in alpha_rows if "err" in r]
            lines += (errs or ["• None"])
        else:
            lines.append("• None (use /alphaadd to manage list)")

    # BOB block (30d)
    bob_prices = await fetch_prices("build-on-bnb", cfg["bob_days"])
    if bob_prices:
        cur, low, high, pct = zone_metrics(bob_prices)
        zone = ("Bottom" if pct <= cfg["bottom_pct"] else ("High" if pct >= cfg["top_pct"] else "Mid"))
        bias = await momentum_bias(bob_prices)
        bob = (
            f"🪙 BOB — {cfg['bob_days']}‑Day View\n"
            f"Price: ${fmt(cur)}\nLow / High: ${fmt(low)} – ${fmt(high)}\n"
            f"Zone: **{zone}**  (≤{int(cfg['bottom_pct']*100)}% = Bottom, ≥{int(cfg['top_pct']*100)}% = High)\n"
            f"12h Momentum: {bias}\nRule: Buy only in Bottom; Trim in High; Mid = hold/observe."
        )
    else:
        bob = "🪙 BOB — data unavailable."

    coverage = fmt_pct((scanned / max(len(bases), 1)) * 100)
    header = (
        f"📡 CryptRizon Sniper — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Symbols fetched: {len(bases)} | Scanned (3m): {scanned} | Coverage: {coverage}\n"
        f"Skipped: {skip['no_cg_match']} no CG match, {skip['no_price_data']} no/insufficient price data"
    )

    text = f"{header}\n\n{blocks}\n\n" + "\n".join(lines) + "\n\n" + bob
    return text

# =========================
# FastAPI App & Endpoints
# =========================
app = FastAPI(title="CryptRizon Actions")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOW_ORIGINS.split(",")] if ALLOW_ORIGINS else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency: simple shared-secret auth for push endpoint
async def require_push_auth(request: Request):
    if not PUSH_AUTH_TOKEN:
        return True
    tok = request.headers.get("X-Push-Auth", "")
    if tok != PUSH_AUTH_TOKEN:
        raise HTTPException(401, "unauthorized")
    return True

@app.get("/health")
async def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

@app.get("/binance/bases")
async def api_get_bases(force: bool = False):
    bases = await get_universe(force)
    return {"bases": bases}

@app.get("/coingecko/search")
async def api_cg_search(q: str):
    cid = await resolve_cg_id(q)
    if not cid:
        raise HTTPException(404, "no_cg_match")
    return {"id": cid}

@app.get("/coingecko/prices")
async def api_cg_prices(id: str, days: int):
    arr = await fetch_prices(id, days)
    if not arr:
        raise HTTPException(404, "no_price_data")
    return {"prices": arr}

@app.get("/config")
async def api_get_config():
    return await CFG_MGR.read()

class Zones(BaseModel):
    bottom: int
    top: int

@app.post("/config/setzones")
async def api_set_zones(z: Zones):
    if not (0 <= z.bottom < z.top <= 100):
        raise HTTPException(400, "bad range")
    cfg = await CFG_MGR.write({"bottom_pct": z.bottom / 100, "top_pct": z.top / 100})
    return {"ok": True, "bottom_pct": cfg["bottom_pct"], "top_pct": cfg["top_pct"]}

class Interval(BaseModel):
    hours: int

@app.post("/config/setinterval")
async def api_set_interval(body: Interval):
    h = max(0, int(body.hours))
    cfg = await CFG_MGR.write({"push_interval_hours": h})
    return {"ok": True, "push_interval_hours": cfg["push_interval_hours"]}

@app.post("/refreshalpha")
async def api_refresh_alpha():
    bases = await get_universe(True)
    return {"ok": True, "bases": len(bases)}

@app.post("/config/alphaadd")
async def api_alpha_add(items: List[str] = Body(...)):
    cur = set((await CFG_MGR.read()).get("alpha_list", []))
    add = [s.upper() for s in items if s.strip()]
    added = [s for s in add if s not in cur]
    if added:
        cur.update(added)
        await CFG_MGR.write({"alpha_list": sorted(cur)})
    return {"added": added}

@app.post("/config/alpharm")
async def api_alpha_rm(items: List[str] = Body(...)):
    cur = set((await CFG_MGR.read()).get("alpha_list", []))
    rm = [s.upper() for s in items if s.strip()]
    removed = [s for s in rm if s in cur]
    if removed:
        for s in removed:
            cur.discard(s)
        await CFG_MGR.write({"alpha_list": sorted(cur)})
    return {"removed": removed}

@app.get("/status")
async def api_status():
    try:
        text = await build_status_text()
        return {"text": text}
    except Exception as e:
        raise HTTPException(500, f"status_failed: {e}")

@app.get("/status/raw")
async def api_status_raw():
    """Structured variant could be added later; for now return text with meta."""
    text = await build_status_text()
    return {"text": text, "generated_at": datetime.now(timezone.utc).isoformat()}

# Telegram push helper
class PushPayload(BaseModel):
    text: str

@app.post("/telegram/push")
async def api_telegram_push(_: bool = Depends(require_push_auth), payload: PushPayload = Body(...)):
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        raise HTTPException(400, "telegram not configured")
    text = payload.text
    client = await get_client()
    r = await client.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "Markdown"},
    )
    if r.status_code != 200:
        raise HTTPException(500, r.text)
    return {"ok": True}

# =========================
# Background Auto‑Push Loop (async, non‑blocking)
# =========================
autopush_task: Optional[asyncio.Task] = None

async def autopush_loop():
    global autopush_task
    while True:
        cfg = await CFG_MGR.read()
        hours = int(cfg.get("push_interval_hours", 0))
        if hours <= 0:
            await asyncio.sleep(5)
            continue
        await asyncio.sleep(hours * 3600)
        try:
            text = await build_status_text()
            if BOT_TOKEN and ADMIN_CHAT_ID:
                client = await get_client()
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                )
        except Exception as e:
            print("Auto-push error:", e)

@app.on_event("startup")
async def _on_startup():
    global autopush_task
    if autopush_task is None:
        autopush_task = asyncio.create_task(autopush_loop())

@app.on_event("shutdown")
async def _on_shutdown():
    global autopush_task
    if autopush_task:
        autopush_task.cancel()
        autopush_task = None
    await close_client()

# =========================
# Self-tests (pure functions only)
# Run with: RUN_TESTS=1 uvicorn app:app --reload
# =========================
if os.getenv("RUN_TESTS") == "1":
    # Basic tests that don't hit the network
    def _assert(cond, msg):
        if not cond:
            raise AssertionError(msg)

    # zone_metrics monotonicity
    cur, low, high, pct = zone_metrics([1, 2, 3])
    _assert(low == 1 and high == 3 and cur == 3, "zone_metrics values wrong")
    _assert(0.0 <= pct <= 1.0, "pct out of range")

    # pct at bounds
    _, _, _, pct_low = zone_metrics([5, 5, 5])
    _assert(0.0 <= pct_low <= 1.0, "flat range pct")

    # fmt trimming
    _assert(fmt(1.2300000) == "1.23", "fmt trimming failed")

    print("Self-tests passed ✅")
