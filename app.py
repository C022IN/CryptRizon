try:
    import ssl
except ModuleNotFoundError as e:
    raise RuntimeError(
        "Python was built without OpenSSL/ssl. Reinstall Python with SSL support or use a base image like python:3.11-slim and ensure ca-certificates are installed."
    ) from e

import os, json, time, asyncio, copy
from contextlib import asynccontextmanager, suppress
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone
from collections import OrderedDict
import re as _re

import httpx
from fastapi import FastAPI, HTTPException, Body, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BINANCE_PRODUCTS = "https://www.binance.com/bapi/asset/v1/public/asset-service/product/get-products"
BINANCE_PRODUCTS_ALT = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-product-list"
CG = "https://api.coingecko.com/api/v3"
CONFIG_FILE = os.getenv("CONFIG_FILE", "cryptrizon_config.json")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
ALLOW_ORIGINS_RAW = os.getenv("ALLOW_ORIGINS", "*")
PUSH_AUTH_TOKEN = os.getenv("PUSH_AUTH_TOKEN", "")
CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY", "12"))
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_CG_CACHE = int(os.getenv("MAX_CG_CACHE", "2000"))
MEM_CG_CACHE_CAP = int(os.getenv("MEM_CG_CACHE_CAP", "1000"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))
ENABLE_AUTOPUSH = os.getenv("ENABLE_AUTOPUSH", "0") == "1"
CFG_RELOAD_INTERVAL = float(os.getenv("CFG_RELOAD_INTERVAL", "2"))

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
    ],
    "cg_id_cache": {}
}
CG_ALIASES = {"BOB": "build-on-bnb", "FWOG": "fwog", "GRASS": "grass", "POPCAT": "popcat"}

class ConfigManager:
    def __init__(self, path: str, defaults: Dict):
        self.path = path
        self.defaults = defaults
        self._lock = asyncio.Lock()
        self._cfg = self._load()
        try:
            self._mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            self._mtime = 0.0
        self._last_check = 0.0

    def _load(self) -> Dict:
        if os.path.exists(self.path):
            try:
                cfg = json.load(open(self.path, "r"))
                for k, v in self.defaults.items():
                    cfg.setdefault(k, v)
                if not isinstance(cfg.get("cg_id_cache", {}), dict):
                    cfg["cg_id_cache"] = {}
                return cfg
            except Exception:
                pass
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.defaults, f, indent=2)
        os.replace(tmp, self.path)
        return copy.deepcopy(self.defaults)

    async def _reload_if_changed(self):
        now = time.time()
        if CFG_RELOAD_INTERVAL == 0 or (now - self._last_check) >= CFG_RELOAD_INTERVAL:
            self._last_check = now
            try:
                mtime = os.path.getmtime(self.path)
            except FileNotFoundError:
                mtime = 0.0
            if mtime > self._mtime:
                async with self._lock:
                    try:
                        fresh = json.load(open(self.path, "r"))
                        for k, v in self.defaults.items():
                            fresh.setdefault(k, v)
                        if not isinstance(fresh.get("cg_id_cache", {}), dict):
                            fresh["cg_id_cache"] = {}
                        self._cfg = fresh
                        self._mtime = mtime
                    except Exception:
                        pass

    async def read(self) -> Dict:
        await self._reload_if_changed()
        async with self._lock:
            return copy.deepcopy(self._cfg)

    async def write(self, updates: Dict) -> Dict:
        async with self._lock:
            self._cfg.update(updates)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._cfg, f, indent=2)
            os.replace(tmp, self.path)
            try:
                self._mtime = os.path.getmtime(self.path)
            except FileNotFoundError:
                self._mtime = time.time()
            return copy.deepcopy(self._cfg)

    async def get(self, key: str, default=None):
        return (await self.read()).get(key, default)

    async def cg_cache_get(self, key: str) -> Optional[str]:
        return (await self.read()).get("cg_id_cache", {}).get(key)

    async def cg_cache_set(self, key: str, value: str):
        async with self._lock:
            self._cfg.setdefault("cg_id_cache", {})
            cache: Dict[str, str] = self._cfg["cg_id_cache"]
            if key not in cache and len(cache) >= MAX_CG_CACHE:
                drop_n = max(1, MAX_CG_CACHE // 10)
                for k in list(cache.keys())[:drop_n]:
                    cache.pop(k, None)
            cache[key] = value
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._cfg, f, indent=2)
            os.replace(tmp, self.path)
            try:
                self._mtime = os.path.getmtime(self.path)
            except FileNotFoundError:
                self._mtime = time.time()

CFG_MGR = ConfigManager(CONFIG_FILE, DEFAULT_CONFIG)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": "CryptRizon/1.0"})
    app.state.sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
    app.state.cg_mem_cache = OrderedDict()
    async def cfg_hot_reload_loop():
        await asyncio.sleep(0.5)
        while True:
            await asyncio.sleep(max(CFG_RELOAD_INTERVAL, 0.5))
            with suppress(Exception):
                await CFG_MGR._reload_if_changed()
    app.state.cfg_reload_task = asyncio.create_task(cfg_hot_reload_loop())
    app.state.autopush_task = asyncio.create_task(autopush_loop(app)) if ENABLE_AUTOPUSH else None
    try:
        yield
    finally:
        with suppress(Exception):
            if app.state.autopush_task:
                app.state.autopush_task.cancel()
                await asyncio.sleep(0)
        with suppress(Exception):
            if app.state.cfg_reload_task:
                app.state.cfg_reload_task.cancel()
                await asyncio.sleep(0)
        with suppress(Exception):
            await app.state.client.aclose()

app = FastAPI(title="CryptRizon Actions", lifespan=lifespan)

allow_origins = [o.strip() for o in ALLOW_ORIGINS_RAW.split(",")] if ALLOW_ORIGINS_RAW else ["*"]
allow_credentials = False if (len(allow_origins) == 1 and allow_origins[0] == "*") else True

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def get_json(app: FastAPI, url: str, params: Dict = None) -> Optional[Dict]:
    client: httpx.AsyncClient = app.state.client
    sem: asyncio.Semaphore = app.state.sem
    for attempt in range(3):
        try:
            async with sem:
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

def _strip_quote(sym: str) -> str:
    return _re.sub(r"(USDT|FDUSD|USDC|BUSD|TUSD|TRY|EUR|BRL|NGN|RUB|GBP|DAI|USTC)$", "", sym, flags=_re.I)

async def fetch_all_binance_bases(app: FastAPI) -> List[str]:
    bases = set()
    data = await get_json(app, BINANCE_PRODUCTS)
    if data and isinstance(data.get("data"), list):
        for it in data["data"]:
            s = it.get("s") or it.get("symbol")
            if s and s.upper().endswith(("USDT","FDUSD","USDC","BUSD","TUSD")):
                bases.add(_strip_quote(s))
    if not bases:
        data2 = await get_json(app, BINANCE_PRODUCTS_ALT, params={"includeEtf": "false"})
        if data2 and isinstance(data2.get("data"), list):
            for it in data2["data"]:
                s = it.get("s") or it.get("symbol")
                if s and s.upper().endswith(("USDT","FDUSD","USDC","BUSD","TUSD")):
                    bases.add(_strip_quote(s))
    return sorted(bases)

async def get_universe(app: FastAPI, force: bool = False) -> List[str]:
    cfg = await CFG_MGR.read()
    now = int(time.time())
    ttl = int(cfg.get("symbols_cache_ttl_hours", 12)) * 3600
    if cfg.get("symbols_cache") and not force and now - int(cfg.get("symbols_cache_updated", 0)) < ttl:
        return cfg["symbols_cache"]
    tokens = await fetch_all_binance_bases(app)
    if tokens:
        await CFG_MGR.write({"symbols_cache": tokens, "symbols_cache_updated": now})
    return tokens

async def _normalize_cg_key(q: str) -> str:
    return (q or "").strip().lower()

async def _mem_cache_get(app: FastAPI, key: str) -> Optional[str]:
    mem: OrderedDict = app.state.cg_mem_cache
    if key in mem:
        mem.move_to_end(key)
        return mem[key]
    return None

async def _mem_cache_set(app: FastAPI, key: str, value: str):
    mem: OrderedDict = app.state.cg_mem_cache
    mem[key] = value
    mem.move_to_end(key)
    while len(mem) > MEM_CG_CACHE_CAP:
        mem.popitem(last=False)

async def resolve_cg_id(app: FastAPI, q: str) -> Optional[str]:
    if not q:
        return None
    q_upper = q.upper()
    if q_upper in CG_ALIASES:
        return CG_ALIASES[q_upper]
    key = await _normalize_cg_key(q)
    cached = await _mem_cache_get(app, key)
    if cached:
        return cached
    disk = await CFG_MGR.cg_cache_get(key)
    if disk:
        await _mem_cache_set(app, key, disk)
        return disk
    data = await get_json(app, f"{CG}/search", params={"query": q})
    if not data:
        return None
    ql = key
    best = None
    for c in data.get("coins", []):
        sym = (c.get("symbol") or "").lower()
        name = (c.get("name") or "").lower()
        if sym == ql or name == ql:
            best = c.get("id")
            break
        if best is None:
            best = c.get("id")
    if best:
        await _mem_cache_set(app, key, best)
        await CFG_MGR.cg_cache_set(key, best)
    return best

async def fetch_prices(app: FastAPI, cg_id: str, days: int) -> Optional[List[float]]:
    data = await get_json(app, f"{CG}/coins/{cg_id}/market_chart", params={"vs_currency": "usd", "days": str(days), "interval": "hourly"})
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

async def scan_subset(app: FastAPI, symbols: List[str], days: int):
    cfg = await CFG_MGR.read()
    out = []
    for s in symbols:
        cid = await resolve_cg_id(app, s)
        if not cid:
            cid = await resolve_cg_id(app, s + " solana")
            if cid:
                await CFG_MGR.cg_cache_set(await _normalize_cg_key(s + " solana"), cid)
        if not cid:
            out.append({"name": s, "err": "no_cg_match"})
            continue
        prices = await fetch_prices(app, cid, days)
        if not prices:
            out.append({"name": s, "err": "no_price_data"})
            continue
        cur, low, high, pct = zone_metrics(prices)
        zone = ("Bottom" if pct <= cfg["bottom_pct"] else ("High" if pct >= cfg["top_pct"] else "Mid"))
        out.append({"name": s, "cur": cur, "low": low, "high": high, "pct": pct, "zone": zone, "bias12h": await momentum_bias(prices)})
    return out

async def _run_in_batches(items: List[str], fn, batch_size: int = BATCH_SIZE):
    for i in range(0, len(items), batch_size):
        batch = items[i:i+batch_size]
        tasks = [asyncio.create_task(fn(x)) for x in batch]
        await asyncio.gather(*tasks)

async def build_status_text(app: FastAPI) -> str:
    cfg = await CFG_MGR.read()
    bases = await get_universe(app, False)
    results: List[Dict] = []
    skip = {"no_cg_match": 0, "no_price_data": 0}

    async def process(sym: str):
        cid = await resolve_cg_id(app, sym)
        if not cid:
            cid2 = await resolve_cg_id(app, sym + " solana")
            if cid2:
                await CFG_MGR.cg_cache_set(await _normalize_cg_key(sym + " solana"), cid2)
                cid = cid2
        if not cid:
            skip["no_cg_match"] += 1
            return
        prices = await fetch_prices(app, cid, cfg["alpha_days_primary"])
        if not prices:
            skip["no_price_data"] += 1
            return
        cur, low, high, pct = zone_metrics(prices)
        zone = ("Bottom" if pct <= cfg["bottom_pct"] else ("High" if pct >= cfg["top_pct"] else "Mid"))
        results.append({"name": sym, "cur": cur, "low": low, "high": high, "pct": pct, "zone": zone})

    await _run_in_batches(bases, process, BATCH_SIZE)

    def blocks_from_results(res3: List[Dict]):
        bottoms = [r for r in res3 if r["pct"] <= cfg["bottom_pct"]]
        tops = [r for r in res3 if r["pct"] >= cfg["top_pct"]]
        closest_bottom = sorted(res3, key=lambda r: r["pct"])[:3]
        closest_top = sorted(res3, key=lambda r: (1 - r["pct"]))[:3]
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

    if cfg["fallback_enabled"]:
        if not has_bottom:
            res1: List[Dict] = []
            async def proc_fb(sym: str):
                cid = await resolve_cg_id(app, sym) or await resolve_cg_id(app, sym + " solana")
                if not cid:
                    return
                prices = await fetch_prices(app, cid, cfg["alpha_days_fallback"])
                if not prices:
                    return
                cur, low, high, pct = zone_metrics(prices)
                zone = ("Bottom" if pct <= cfg["bottom_pct"] else ("High" if pct >= cfg["top_pct"] else "Mid"))
                res1.append({"name": sym, "cur": cur, "low": low, "high": high, "pct": pct, "zone": zone})
            await _run_in_batches(bases, proc_fb, BATCH_SIZE)
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
            res1: List[Dict] = []
            async def proc_fb2(sym: str):
                cid = await resolve_cg_id(app, sym) or await resolve_cg_id(app, sym + " solana")
                if not cid:
                    return
                prices = await fetch_prices(app, cid, cfg["alpha_days_fallback"])
                if not prices:
                    return
                cur, low, high, pct = zone_metrics(prices)
                zone = ("Bottom" if pct <= cfg["bottom_pct"] else ("High" if pct >= cfg["top_pct"] else "Mid"))
                res1.append({"name": sym, "cur": cur, "low": low, "high": high, "pct": pct, "zone": zone})
            await _run_in_batches(bases, proc_fb2, BATCH_SIZE)
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

    bases_set = set(bases)
    alpha = [s for s in cfg.get("alpha_list", []) if s in bases_set or s == "BOB"]
    alpha_rows = await scan_subset(app, alpha, cfg["alpha_days_primary"]) if alpha else []
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

    bob_prices = await fetch_prices(app, "build-on-bnb", cfg["bob_days"])
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

    coverage = fmt_pct((len(results) / max(len(bases), 1)) * 100)
    header = (
        f"📡 CryptRizon Sniper — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Symbols fetched: {len(bases)} | Scanned (3m): {len(results)} | Coverage: {coverage}\n"
        f"Skipped: {skip['no_cg_match']} no CG match, {skip['no_price_data']} no/insufficient price data"
    )

    text = f"{header}\n\n{blocks}\n\n" + "\n".join(lines) + "\n\n" + bob
    return text

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
async def api_get_bases(request: Request, force: bool = False):
    bases = await get_universe(request.app, force)
    return {"bases": bases}

@app.get("/coingecko/search")
async def api_cg_search(request: Request, q: str):
    cid = await resolve_cg_id(request.app, q)
    if not cid:
        raise HTTPException(404, "no_cg_match")
    return {"id": cid}

@app.get("/coingecko/prices")
async def api_cg_prices(request: Request, id: str, days: int):
    arr = await fetch_prices(request.app, id, days)
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
async def api_refresh_alpha(request: Request):
    bases = await get_universe(request.app, True)
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
async def api_status(request: Request):
    try:
        text = await build_status_text(request.app)
        return {"text": text}
    except Exception as e:
        raise HTTPException(500, f"status_failed: {e}")

@app.get("/status/raw")
async def api_status_raw(request: Request):
    text = await build_status_text(request.app)
    return {"text": text, "generated_at": datetime.now(timezone.utc).isoformat()}

class PushPayload(BaseModel):
    text: str

@app.post("/telegram/push")
async def api_telegram_push(request: Request, _: bool = Depends(require_push_auth), payload: PushPayload = Body(...)):
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        raise HTTPException(400, "telegram not configured")
    client: httpx.AsyncClient = request.app.state.client
    r = await client.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": ADMIN_CHAT_ID, "text": payload.text},
    )
    if r.status_code != 200:
        raise HTTPException(500, r.text)
    return {"ok": True}

async def autopush_loop(app: FastAPI):
    await asyncio.sleep(1 + (time.time() % 3))
    while True:
        cfg = await CFG_MGR.read()
        hours = int(cfg.get("push_interval_hours", 0))
        if hours <= 0:
            await asyncio.sleep(5)
            continue
        await asyncio.sleep(hours * 3600)
        try:
            text = await build_status_text(app)
            if BOT_TOKEN and ADMIN_CHAT_ID:
                client: httpx.AsyncClient = app.state.client
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": ADMIN_CHAT_ID, "text": text},
                )
        except Exception as e:
            print("Auto-push error:", e)

if os.getenv("RUN_TESTS") == "1":
    def _assert(cond, msg):
        if not cond:
            raise AssertionError(msg)
    cur, low, high, pct = zone_metrics([1, 2, 3])
    _assert(low == 1 and high == 3 and cur == 3, "zone_metrics values wrong")
    _assert(0.0 <= pct <= 1.0, "pct out of range")
    _, _, _, pct_low = zone_metrics([5, 5, 5])
    _assert(0.0 <= pct_low <= 1.0, "flat range pct")
    _assert(fmt(1.2300000) == "1.23", "fmt trimming failed")
    print("Self-tests passed ✅")
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
