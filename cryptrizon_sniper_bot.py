import os, re, json, time, asyncio, requests
from typing import List, Dict, Tuple, Optional
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =========================
# Config & Constants
# =========================
BINANCE_PRODUCTS     = "https://www.binance.com/bapi/asset/v1/public/asset-service/product/get-products"
BINANCE_PRODUCTS_ALT = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-product-list"
CG = "https://api.coingecko.com/api/v3"
CONFIG_FILE = os.getenv("CONFIG_FILE", "cryptrizon_config.json")

BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

DEFAULT_CONFIG = {
  "symbols_cache": [], "symbols_cache_updated": 0, "symbols_cache_ttl_hours": 12,
  "fallback_enabled": True, "push_interval_hours": 6,
  "alpha_days_primary": 90, "alpha_days_fallback": 30, "bob_days": 30,
  "bottom_pct": 0.15, "top_pct": 0.85,
  "alpha_list": [
    "VINE","HOUSE","PYTHIA","FWOG","POPCAT","G","FARTCOIN","PUMP","JELLYJELLY",
    "ARC","ALON","PIPPIN","GRASS","AI16Z","ALCH","GRIFFAIN","RFC","ZEREBRO","GOAT",
    "CLANKER","ICNT","VVV","AERO","ZORA","RDAC","FLOCK","B3","DEGEN","SKI","ODOS","POKT","LUNAI",
    "XO","HIPPO","SCA","DMC","NS","BLUE","NAVX","FHSS","HSL","MEET","SINS","TAG","VELO","BOB"
  ]
}
CG_ALIASES = {"BOB":"build-on-bnb","FWOG":"fwog","GRASS":"grass","POPCAT":"popcat"}

# =========================
# Config I/O
# =========================
def load_cfg()->Dict:
    if os.path.exists(CONFIG_FILE):
        try:
            cfg=json.load(open(CONFIG_FILE,"r"))
            for k,v in DEFAULT_CONFIG.items(): cfg.setdefault(k,v)
            return cfg
        except Exception:
            pass
    json.dump(DEFAULT_CONFIG, open(CONFIG_FILE,"w"), indent=2)
    return DEFAULT_CONFIG.copy()

def save_cfg(cfg:Dict):
    json.dump(cfg, open(CONFIG_FILE,"w"), indent=2)

CFG = load_cfg()

# =========================
# Utilities
# =========================
def get_json(url: str, params: dict = None, headers: dict = None, timeout=25):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status(); return r.json()
    except Exception:
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

def fetch_all_binance_bases() -> List[str]:
    bases = set()
    data = get_json(BINANCE_PRODUCTS)
    if data and isinstance(data.get("data"), list):
        for it in data["data"]:
            s = it.get("s") or it.get("symbol")
            if s and s.upper().endswith(("USDT","FDUSD","USDC","BUSD","TUSD")):
                bases.add(_strip_quote(s))
    if not bases:
        data2 = get_json(BINANCE_PRODUCTS_ALT, params={"includeEtf":"false"})
        if data2 and isinstance(data2.get("data"), list):
            for it in data2["data"]:
                s = it.get("s") or it.get("symbol")
                if s and s.upper().endswith(("USDT","FDUSD","USDC","BUSD","TUSD")):
                    bases.add(_strip_quote(s))
    return sorted(bases)

def get_universe(force:bool=False)->List[str]:
    now=int(time.time()); ttl=int(CFG.get("symbols_cache_ttl_hours",12))*3600
    if CFG.get("symbols_cache") and not force and now-int(CFG.get("symbols_cache_updated",0))<ttl:
        return CFG["symbols_cache"]
    tokens=fetch_all_binance_bases()
    if tokens:
        CFG["symbols_cache"]=tokens; CFG["symbols_cache_updated"]=now; save_cfg(CFG)
    return tokens

# =========================
# Prices & Zones
# =========================
def resolve_cg_id(q:str)->Optional[str]:
    if q.upper() in CG_ALIASES: return CG_ALIASES[q.upper()]
    data=get_json(f"{CG}/search", params={"query":q})
    if not data: return None
    ql=q.lower(); best=None
    for c in data.get("coins",[]):
        if c.get("symbol","{}").lower()==ql or c.get("name","{}").lower()==ql:
            return c.get("id")
        if best is None: best=c.get("id")
    return best

def fetch_prices(cg_id:str, days:int)->Optional[List[float]]:
    data=get_json(f"{CG}/coins/{cg_id}/market_chart",
                  params={"vs_currency":"usd","days":str(days),"interval":"hourly"})
    if not data: return None
    arr=[p[1] for p in data.get("prices",[])]
    return arr or None

def zone_metrics(prices:List[float]):
    low,high=min(prices),max(prices); cur=prices[-1]; rng=max(high-low,1e-18)
    pct=(cur-low)/rng
    return cur,low,high,pct

def zone_label(pct:float)->str:
    if pct<=CFG["bottom_pct"]: return "Bottom"
    if pct>=CFG["top_pct"]:    return "High"
    return "Mid"

def momentum_bias(prices:List[float], hrs:int=12)->str:
    if len(prices)<2: return "flat"
    sl=prices[-hrs:] if len(prices)>=hrs else prices
    d=sl[-1]-sl[0]; return "up" if d>0 else ("down" if d<0 else "flat")

# =========================
# Alpha helpers
# =========================
def scan_subset(symbols:List[str], days:int):
    out=[]
    for s in symbols:
        cid=resolve_cg_id(s) or resolve_cg_id(s+" solana")
        if not cid: out.append({"name":s,"err":"no_cg_match"}); continue
        prices=fetch_prices(cid, days)
        if not prices: out.append({"name":s,"err":"no_price_data"}); continue
        cur,low,high,pct=zone_metrics(prices)
        out.append({"name":s,"cur":cur,"low":low,"high":high,"pct":pct,
                    "zone":zone_label(pct),"bias12h":momentum_bias(prices)})
    return out

# =========================
# Report builder (/status)
# =========================
def build_status_text()->str:
    bases=get_universe(False); symbols_total=len(bases)
    # 90d scan (ALL)
    scanned=0; results=[]; skip={"no_cg_match":0,"no_price_data":0}
    for sym in bases:
        cid=resolve_cg_id(sym)
        if not cid: skip["no_cg_match"]+=1; continue
        prices=fetch_prices(cid, CFG["alpha_days_primary"])
        if not prices: skip["no_price_data"]+=1; continue
        scanned+=1; cur,low,high,pct=zone_metrics(prices)
        results.append({"name":sym,"cur":cur,"low":low,"high":high,"pct":pct,"zone":zone_label(pct)})

    def blocks_from_results(res3):
        bottoms=[r for r in res3 if r["pct"]<=CFG["bottom_pct"]]
        tops=[r for r in res3 if r["pct"]>=CFG["top_pct"]]
        closest_bottom=sorted(res3,key=lambda r:r["pct"])[:3]
        closest_top=sorted(res3,key=lambda r:(1-r["pct"]))[:3]
        parts=[]
        if bottoms:
            best=min(bottoms,key=lambda r:r["pct"])
            parts.append(
                "🎯 Bottom‑Zone Sniper (3‑month)\n"
                f"{best['name']} — ${fmt(best['cur'])}\n"
                f"90d L/H: ${fmt(best['low'])} – ${fmt(best['high'])}\n"
                f"Zone: **{best['zone']}** (pos {best['pct']*100:.1f}%)"
            )
        else:
            parts.append("🎯 Bottom‑Zone Sniper (3‑month): **None**")
        if closest_bottom:
            parts.append("**3 closest to bottom (3‑month):**\n"+"\n".join(
                f"• {r['name']}: ${fmt(r['cur'])} | 90d {fmt(r['low'])}/{fmt(r['high'])} | Zone {r['zone']} | Pos {r['pct']*100:.1f}%"
                for r in closest_bottom))
        if tops:
            t=max(tops,key=lambda r:r["pct"])
            parts.append(
                "\n📈 Top‑Zone Watch (3‑month)\n"
                f"{t['name']} — ${fmt(t['cur'])}\n"
                f"90d L/H: ${fmt(t['low'])} – ${fmt(t['high'])}\n"
                f"Zone: **{t['zone']}** (pos {t['pct']*100:.1f}%)"
            )
        else:
            parts.append("\n📈 Top‑Zone Watch (3‑month): **None**")
        if closest_top:
            parts.append("**3 closest to top (3‑month):**\n"+"\n".join(
                f"• {r['name']}: ${fmt(r['cur'])} | 90d {fmt(r['low'])}/{fmt(r['high'])} | Zone {r['zone']} | Pos {r['pct']*100:.1f}%"
                for r in closest_top))
        return "\n\n".join(parts), bool(bottoms), bool(tops), scanned, symbols_total, skip

    blocks, has_bottom, has_top, scanned, symbols_total, skip = blocks_from_results(results)

    # Fallbacks 30d if needed
    if CFG["fallback_enabled"]:
        if not has_bottom:
            res1=[]
            for sym in bases:
                cid=resolve_cg_id(sym); 
                if not cid: continue
                prices=fetch_prices(cid, CFG["alpha_days_fallback"]); 
                if not prices: continue
                cur,low,high,pct=zone_metrics(prices)
                res1.append({"name":sym,"cur":cur,"low":low,"high":high,"pct":pct,"zone":zone_label(pct)})
            bottoms1=[r for r in res1 if r["pct"]<=CFG["bottom_pct"]]
            if bottoms1:
                b=min(bottoms1,key=lambda r:r["pct"]) 
                blocks += (
                    "\n\n🔁 Fallback: Short‑term bottom (1‑month)\n"
                    f"{b['name']} — ${fmt(b['cur'])}\n"
                    f"30d L/H: ${fmt(b['low'])} – ${fmt(b['high'])}\n"
                    f"Zone: **{b['zone']}** (pos {b['pct']*100:.1f}%)"
                )
            else:
                blocks += "\n\n🔁 Fallback (1‑month): **No coin in bottom zone either.**"
        if not has_top:
            res1=[]
            for sym in bases:
                cid=resolve_cg_id(sym); 
                if not cid: continue
                prices=fetch_prices(cid, CFG["alpha_days_fallback"]); 
                if not prices: continue
                cur,low,high,pct=zone_metrics(prices)
                res1.append({"name":sym,"cur":cur,"low":low,"high":high,"pct":pct,"zone":zone_label(pct)})
            tops1=[r for r in res1 if r["pct"]>=CFG["top_pct"]]
            if tops1:
                t=max(tops1,key=lambda r:r["pct"]) 
                blocks += (
                    "\n\n🔁 Fallback: Short‑term top (1‑month)\n"
                    f"{t['name']} — ${fmt(t['cur'])}\n"
                    f"30d L/H: ${fmt(t['low'])} – ${fmt(t['high'])}\n"
                    f"Zone: **{t['zone']}** (pos {t['pct']*100:.1f}%)"
                )
            else:
                blocks += "\n\n🔁 Fallback (1‑month Top): **No coin in top zone either.**"

    # Alpha Focus
    bases_set=set(bases)
    alpha=[s for s in CFG.get("alpha_list",[]) if s in bases_set or s=="BOB"]
    alpha_rows=scan_subset(alpha, CFG["alpha_days_primary"]) if alpha else []
    alpha_ok=sorted([r for r in alpha_rows if "err" not in r], key=lambda r:r["pct"])
    lines=["**⭐ Alpha Focus (90d)**"]
    if alpha_ok:
        for r in alpha_ok[:10]:
            pos=r["pct"]*100.0
            lines.append(
                f"• {r['name']}: ${fmt(r['cur'])} | 90d {fmt(r['low'])}/{fmt(r['high'])} | {r['zone']} | {pos:.1f}% | 12h {r['bias12h']}"
            )
    else:
        if alpha_rows:
            errs=[f"• {r['name']}: ({r['err']})" for r in alpha_rows if "err" in r]
            lines += (errs or ["• None"])
        else:
            lines.append("• None (use /alphaadd to manage list)")

    # BOB block
    bob_prices = fetch_prices("build-on-bnb", CFG["bob_days"])
    if bob_prices:
        cur,low,high,pct=zone_metrics(bob_prices); zone=zone_label(pct); bias=momentum_bias(bob_prices)
        bob = (
            f"🪙 BOB — {CFG['bob_days']}‑Day View\n"
            f"Price: ${fmt(cur)}\nLow / High: ${fmt(low)} – ${fmt(high)}\n"
            f"Zone: **{zone}**  (≤{int(CFG['bottom_pct']*100)}% = Bottom, ≥{int(CFG['top_pct']*100)}% = High)\n"
            f"12h Momentum: {bias}\nRule: Buy only in Bottom; Trim in High; Mid = hold/observe."
        )
    else:
        bob = "🪙 BOB — data unavailable."

    coverage = fmt_pct((scanned/max(len(bases),1))*100)
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
app=FastAPI(title="CryptRizon Actions")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

@app.get("/binance/bases")
def api_get_bases(force: bool=False):
    return {"bases": get_universe(force)}

@app.get("/coingecko/search")
def api_cg_search(q:str):
    cid=resolve_cg_id(q)
    if not cid: raise HTTPException(404,"no_cg_match")
    return {"id":cid}

@app.get("/coingecko/prices")
def api_cg_prices(id:str, days:int):
    arr=fetch_prices(id, days)
    if not arr: raise HTTPException(404,"no_price_data")
    return {"prices":arr}

@app.get("/config")
def api_get_config():
    return CFG

class Zones(BaseModel):
    bottom:int; top:int

@app.post("/config/setzones")
def api_set_zones(z:Zones):
    if not (0<=z.bottom<z.top<=100):
        raise HTTPException(400,"bad range")
    CFG["bottom_pct"]=z.bottom/100; CFG["top_pct"]=z.top/100; save_cfg(CFG)
    return {"ok":True}

class Interval(BaseModel):
    hours:int

@app.post("/config/setinterval")
def api_set_interval(body: Interval):
    h=max(0,int(body.hours))
    CFG["push_interval_hours"]=h; save_cfg(CFG)
    return {"ok": True, "push_interval_hours": h}

@app.post("/refreshalpha")
def api_refresh_alpha():
    bases=get_universe(True)
    return {"ok": True, "bases": len(bases)}

@app.post("/config/alphaadd")
def api_alpha_add(items: List[str]=Body(...)):
    cur=set(CFG.get("alpha_list",[])); add=[s.upper() for s in items if s.strip()]
    added=[s for s in add if s not in cur]
    if added:
        CFG["alpha_list"]=sorted(cur.union(added)); save_cfg(CFG)
    return {"added":added}

@app.post("/config/alpharm")
def api_alpha_rm(items: List[str]=Body(...)):
    cur=set(CFG.get("alpha_list",[])); rm=[s.upper() for s in items if s.strip()]
    removed=[s for s in rm if s in cur]
    if removed:
        CFG["alpha_list"]=sorted([s for s in cur if s not in removed]); save_cfg(CFG)
    return {"removed":removed}

@app.get("/status")
def api_status():
    try:
        text = build_status_text()
        return {"text": text}
    except Exception as e:
        raise HTTPException(500, f"status_failed: {e}")

# Telegram push helper
@app.post("/telegram/push")
def api_telegram_push(payload: Dict=Body(...)):
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        raise HTTPException(400,"telegram not configured")
    text=payload.get("text","")
    r=requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id":ADMIN_CHAT_ID,"text":text,"parse_mode":"Markdown"})
    if r.status_code!=200:
        raise HTTPException(500, r.text)
    return {"ok":True}

# =========================
# Background Auto‑Push Loop
# =========================
autopush_task: Optional[asyncio.Task] = None

async def autopush_loop():
    global autopush_task
    while True:
        hours=int(CFG.get("push_interval_hours",0))
        if hours<=0:
            await asyncio.sleep(5)
            continue
        await asyncio.sleep(hours*3600)
        try:
            text=build_status_text()
            if BOT_TOKEN and ADMIN_CHAT_ID:
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id":ADMIN_CHAT_ID,"text":text,"parse_mode":"Markdown"}, timeout=20
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
        autopush_task.cancel(); autopush_task=None
