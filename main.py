from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from typing import List, Dict, Optional, Tuple, Any
import requests
import time
import os
import json
import threading
import re

app = FastAPI(title="Hybrid Early Gem Scanner – DexScreener PRO")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CHAINS = {"ethereum", "base", "solana", "bsc", "arbitrum", "avalanche", "robinhood", "arc"}

JUNK_SYMBOLS = {
    "ETH", "WETH", "BTC", "WBTC", "SOL", "WSOL", "BNB", "WBNB", "BSC",
    "USDC", "USDT", "DAI", "USD1", "USDBC", "USDB", "FDUSD", "TUSD",
    "AVAX", "WAVAX", "ARB", "WARB", "UNKNOWN", "CAKE", "BUSD",
}

CACHE = {"data": [], "ts": 0}
CACHE_TTL = 45
SEC_CACHE = {}
SEC_TTL = 180

GOPLUS_CHAIN = {
    "ethereum": "1",
    "bsc": "56",
    "arbitrum": "42161",
    "base": "8453",
    "avalanche": "43114",
    "robinhood": "4663",
}

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "gem-scanner/2.0"})

DATA_DIR = os.getenv("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
WATCH_FILE = os.path.join(DATA_DIR, "watchlist.json")
CART_FILE = os.path.join(DATA_DIR, "cart.json")
WALLET_FILE = os.path.join(DATA_DIR, "wallets.json")
SIGNAL_FILE = os.path.join(DATA_DIR, "signal_stats.json")
WATCH_MAX_AGE_HOURS = 14 * 24
WATCH_LOCK = threading.Lock()
CART_LOCK = threading.Lock()
WALLET_LOCK = threading.Lock()
SIGNAL_LOCK = threading.Lock()


def load_watch() -> Dict[str, Dict]:
    if not os.path.exists(WATCH_FILE):
        return {}
    try:
        with open(WATCH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("watch load error:", e)
        return {}


def save_watch(data: Dict[str, Dict]) -> None:
    tmp = WATCH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WATCH_FILE)


def load_cart() -> Dict[str, Dict]:
    if not os.path.exists(CART_FILE):
        return {}
    try:
        with open(CART_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("cart load error:", e)
        return {}


def save_cart(data: Dict[str, Dict]) -> None:
    tmp = CART_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CART_FILE)


def load_wallets() -> Dict[str, Dict]:
    if not os.path.exists(WALLET_FILE):
        return {}
    try:
        with open(WALLET_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("wallet load error:", e)
        return {}


def save_wallets(data: Dict[str, Dict]) -> None:
    tmp = WALLET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WALLET_FILE)


def wallet_links(addr: str, chain: str) -> Dict[str, str]:
    a = (addr or "").strip()
    c = (chain or "solana").lower()
    links = {
        "dex": f"https://dexscreener.com/{c}/{a}",
        "gmgn": "",
        "explorer": "",
    }
    if c == "solana":
        links["gmgn"] = f"https://gmgn.ai/sol/address/{a}"
        links["explorer"] = f"https://solscan.io/account/{a}"
    elif c == "robinhood":
        links["explorer"] = f"https://explorer.robinhood.com/address/{a}"
        links["gmgn"] = f"https://gmgn.ai/base/address/{a}"
    elif c == "arc":
        links["explorer"] = f"https://explorer.arc.io/address/{a}"
        links["gmgn"] = ""
    else:
        links["gmgn"] = f"https://gmgn.ai/{c}/address/{a}"
        links["explorer"] = f"https://blockscan.com/address/{a}"
    return links


def _price_float(v) -> Optional[float]:
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def _chg_from_snaps(snaps: List[Dict], now: float, minutes: int, now_price: Optional[float]) -> Optional[float]:
    if not now_price:
        return None
    target = now - minutes * 60
    best = None
    best_dt = 10**18
    for s in snaps:
        ts = float(s.get("ts") or 0)
        px = _price_float(s.get("price"))
        if not ts or not px:
            continue
        dt = abs(ts - target)
        if dt < best_dt:
            best_dt = dt
            best = px
    if best is None or best_dt > minutes * 60 * 0.7:
        return None
    return round((now_price / best - 1) * 100, 2)


def remembered_addresses() -> List[str]:
    now = time.time()
    out = []
    for rec in load_watch().values():
        first = float(rec.get("first_seen") or 0)
        if first and (now - first) / 3600 > WATCH_MAX_AGE_HOURS:
            continue
        addr = rec.get("token_address")
        if addr:
            out.append(addr)
    return out


def remember_tokens(rows: List[Dict]) -> None:
    if not rows:
        return
    now = time.time()
    with WATCH_LOCK:
        data = load_watch()
        for row in rows:
            addr = (row.get("token_address") or "").strip()
            chain = (row.get("chain") or "").lower()
            if not addr:
                continue
            key = f"{chain}:{addr.lower()}"
            prev = data.get(key) or {}
            data[key] = {
                "token_address": addr,
                "chain": chain,
                "symbol": row.get("symbol") or prev.get("symbol") or "",
                "name": row.get("name") or prev.get("name") or "",
                "pair_address": row.get("pair_address") or prev.get("pair_address") or "",
                "first_seen": prev.get("first_seen") or now,
                "last_seen": now,
                "first_mcap": prev.get("first_mcap") or row.get("market_cap") or 0,
                "last_mcap": row.get("market_cap") or 0,
                "last_liq": row.get("liquidity_usd") or 0,
                "age_hours": row.get("age_hours"),
                "url": row.get("url") or prev.get("url") or "",
                "creator": row.get("creator") or prev.get("creator") or "",
                "top1_pct": row.get("top1_pct") or prev.get("top1_pct") or 0,
                "top10_pct": row.get("top10_pct") or prev.get("top10_pct") or 0,
                "holder_count": row.get("holder_count") or prev.get("holder_count") or 0,
                "lp_locked": row.get("lp_locked") if row.get("lp_locked") is not None else prev.get("lp_locked"),
                "risk": row.get("risk") or prev.get("risk") or "",
            }
        # drop too old
        keep = {}
        for k, rec in data.items():
            first = float(rec.get("first_seen") or now)
            if (now - first) / 3600 <= WATCH_MAX_AGE_HOURS:
                keep[k] = rec
        save_watch(keep)


def _get_json(url: str, timeout: int = 12):
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _chunk(items: List[str], n: int = 30) -> List[List[str]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def discover_token_addresses() -> List[Tuple[str, str]]:
    """Latest profiles + boosts. Returns (chainId, tokenAddress)."""
    urls = [
        "https://api.dexscreener.com/token-profiles/latest/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1",
    ]
    seen = set()
    out: List[Tuple[str, str]] = []

    for url in urls:
        try:
            data = _get_json(url)
            if isinstance(data, dict):
                data = data.get("data") or data.get("tokens") or []
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                chain = (item.get("chainId") or "").lower()
                addr = item.get("tokenAddress") or item.get("address")
                if not addr:
                    continue
                if chain and chain not in CHAINS:
                    continue
                key = f"{chain}:{addr.lower()}"
                if key in seen:
                    continue
                seen.add(key)
                out.append((chain, addr))
            time.sleep(0.15)
        except Exception as e:
            print("discover error:", url, e)
    return out


def fetch_pairs_for_tokens(addresses: List[str]) -> List[Dict]:
    pairs: List[Dict] = []
    for group in _chunk(addresses, 30):
        try:
            url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(group)
            data = _get_json(url)
            pairs.extend(data.get("pairs") or [])
            time.sleep(0.2)
        except Exception as e:
            print("token pairs error:", e)
    return pairs


# ---------- Yodao Pump.fun API (https://dev.yodao.io) ----------
YODAO_API = os.getenv("YODAO_API_BASE", "https://dev.yodao.io").rstrip("/")
YODAO_CACHE = {"ts": 0, "rows": []}
YODAO_TTL = 8


def _yodao_pct(v) -> float:
    """Yodao kadang 0–1 fraction, kadang sudah 0–100."""
    try:
        x = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    if 0 < x <= 1.5:
        return round(x * 100.0, 2)
    return round(x, 2)


def fetch_yodao_meme(filters: Optional[Dict] = None) -> Dict:
    """POST /api/v2/meme — base hidup: https://dev.yodao.io (api2 sering DNS mati)."""
    # body kosong = semua pool (filter ketat bikin 0 hasil)
    body = filters if filters is not None else {}
    try:
        r = SESSION.post(
            f"{YODAO_API}/api/v2/meme",
            json=body,
            headers={"Content-Type": "application/json", "x-socket-id": "hybrid-gem-scanner"},
            timeout=18,
        )
        if r.status_code not in (200, 201):
            print("yodao meme status:", r.status_code, r.text[:200])
            return {}
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("yodao meme error:", e)
        return {}


def fetch_yodao_market(mint: str) -> Dict:
    """GET /api/v2/search/tokens/:mint/market-data (response kadang list)."""
    if not mint:
        return {}
    try:
        r = SESSION.get(
            f"{YODAO_API}/api/v2/search/tokens/{mint}/market-data",
            timeout=12,
        )
        if not r.ok:
            return {}
        data = r.json()
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("yodao market error:", e)
        return {}


def yodao_pool_to_row(p: Dict, stage: str = "new") -> Dict:
    """Map Yodao pool → internal row shape (compatible with filters / Telegram)."""
    mint = p.get("mint") or ""
    liq = float(p.get("liqudity") or p.get("liquidity") or 0)
    mcap = float(p.get("market_cap") or 0)
    vol = float(p.get("vol_24h") or 0)
    buys = float(p.get("tx_24h_buy") or 0)
    sells = float(p.get("tx_24h_sell") or 0)
    top10 = _yodao_pct(p.get("top_10_percent"))
    holders = int(p.get("holders") or 0)
    creator_pct = _yodao_pct(p.get("creator_holding") or p.get("creator_holding_pct"))
    snipers = _yodao_pct(p.get("snipers_holding"))
    insiders = _yodao_pct(p.get("insiders_holding"))
    bundle = _yodao_pct(p.get("bundle_holding"))
    fresh = _yodao_pct(p.get("fresh_wallets_holding") or p.get("fresh_wallet_holding"))
    pro = float(p.get("pro_traders_holding") or p.get("pro_traders") or 0)
    pct_done = float(p.get("pct_completion") or 0)
    ts = p.get("timestamp") or ""
    age_h = None
    try:
        # timestamp often "YYYY-MM-DD HH:MM:SS"
        from datetime import datetime
        dt = datetime.strptime(str(ts)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        age_h = max(0.0, (time.time() - dt.timestamp()) / 3600)
    except Exception:
        age_h = None
    socials = []
    if p.get("twitter"):
        socials.append({"type": "twitter", "url": p["twitter"]})
    if p.get("telegram"):
        socials.append({"type": "telegram", "url": p["telegram"]})
    websites = [p["website"]] if p.get("website") else []
    flags = []
    if creator_pct >= 10:
        flags.append("DEV_HOLDING_HIGH")
    if snipers >= 15:
        flags.append("SNIPERS_HIGH")
    if insiders >= 10:
        flags.append("INSIDERS_HIGH")
    if bundle >= 15:
        flags.append("BUNDLE_HIGH")
    if fresh >= 20:
        flags.append("FRESH_WALLETS_HIGH")
    if top10 >= 40:
        flags.append("CONCENTRATED_HOLDERS")
    pressure = "BUY PRESSURE" if buys > sells * 1.15 else ("SELL PRESSURE" if sells > buys * 1.15 else "NEUTRAL")
    score = 40
    if holders >= 50:
        score += 10
    if 15_000 <= mcap <= 400_000:
        score += 15
    if liq >= 8_000:
        score += 10
    if creator_pct < 5:
        score += 8
    if top10 < 25:
        score += 8
    if stage == "completing":
        score += 5
    risk = "HIGH" if (creator_pct >= 20 or snipers >= 30 or insiders >= 20) else "LOW"
    if risk == "HIGH":
        score = min(score, 55)
    return {
        "symbol": p.get("symbol") or "?",
        "name": p.get("name") or "",
        "token_address": mint,
        "chain": "solana",
        "sector": "SOLANA",
        "dex": "pumpfun" if stage != "graduated" else "pumpswap",
        "score": min(score, 100),
        "confidence": min(50 + (10 if holders > 40 else 0) + (10 if vol > 10_000 else 0), 95),
        "pressure": pressure,
        "verdict": "YODAO " + stage.upper(),
        "whale": "WHALE BUYING" if pro >= 5 or buys > sells * 1.3 else "-",
        "flow": "ACCUMULATION" if buys > sells else ("DISTRIBUTION" if sells > buys else "NEUTRAL"),
        "liquidity_usd": liq,
        "market_cap": mcap,
        "volume_24h": vol,
        "price_change_24h": 0,
        "price_usd": None,
        "age_hours": round(age_h, 2) if age_h is not None else None,
        "pair_address": p.get("pool") or "",
        "url": f"https://dexscreener.com/solana/{mint}" if mint else "",
        "upside": "HIGH ROOM" if mcap and mcap < 250_000 else "SPECULATIVE",
        "upside_note": f"yodao {stage} · complete {pct_done:.0f}%",
        "icon": p.get("image") or "",
        "socials": socials,
        "websites": websites,
        "honeypot": False,
        "risk": risk,
        "flags": flags,
        "buy_tax": 0,
        "sell_tax": 0,
        "sec_provider": "yodao",
        "creator": p.get("creator") or "",
        "top_holders": [],
        "top10_pct": top10,
        "top1_pct": max(creator_pct, top10 / 4 if top10 else 0),
        "holder_count": holders,
        "holder_note": (
            f"yodao · dev {creator_pct:.1f}% · sniper {snipers:.1f}% · "
            f"insider {insiders:.1f}% · bundle {bundle:.1f}% · fresh {fresh:.1f}%"
        ),
        "tx_buys_h1": buys,
        "tx_sells_h1": sells,
        "tx_buys_m5": 0,
        "tx_sells_m5": 0,
        "yodao_stage": stage,
        "yodao_pct_completion": pct_done,
        "yodao_dev_holding": creator_pct,
        "yodao_snipers": snipers,
        "yodao_insiders": insiders,
        "yodao_bundle": bundle,
        "yodao_fresh": fresh,
        "yodao_pro_traders": pro,
        "source": "yodao",
    }


def fetch_yodao_rows(force: bool = False) -> List[Dict]:
    now = time.time()
    if not force and YODAO_CACHE["rows"] and now - YODAO_CACHE["ts"] < YODAO_TTL:
        return list(YODAO_CACHE["rows"])
    data = fetch_yodao_meme({})
    rows = []
    for stage in ("new", "completing", "graduated"):
        for p in data.get(stage) or []:
            if not isinstance(p, dict) or not p.get("mint"):
                continue
            row = yodao_pool_to_row(p, stage)
            # soft filter client-side (jangan terlalu kaku)
            mcap = float(row.get("market_cap") or 0)
            holders = int(row.get("holder_count") or 0)
            if mcap > 0 and mcap < 500:
                continue
            if holders == 0 and mcap < 1000:
                continue
            rows.append(row)
    YODAO_CACHE["rows"] = rows
    YODAO_CACHE["ts"] = now
    return rows


def apply_yodao_enrich(row: Dict) -> Dict:
    """Isi holder/dev/sniper dari Yodao market-data kalau mint Solana."""
    chain = str(row.get("chain") or "").lower()
    mint = row.get("token_address") or ""
    if chain not in ("solana",) or not mint:
        return row
    md = fetch_yodao_market(mint)
    if not md:
        return row
    top10 = _yodao_pct(md.get("top_10_percent") or row.get("top10_pct"))
    holders = int(md.get("holders") or row.get("holder_count") or 0)
    dev = _yodao_pct(md.get("creator_holding_pct") or md.get("creator_holding"))
    snipers = _yodao_pct(md.get("snipers_holding"))
    insiders = _yodao_pct(md.get("insiders_holding"))
    bundle = _yodao_pct(md.get("bundle_holding"))
    fresh = _yodao_pct(md.get("fresh_wallet_holding") or md.get("fresh_wallets_holding"))
    liq = float(md.get("liquidity") or row.get("liquidity_usd") or 0)
    mcap = float(md.get("market_cap") or row.get("market_cap") or 0)
    vol = float(md.get("vol_24h") or row.get("volume_24h") or 0)
    px = md.get("token_price_usd")
    row["top10_pct"] = top10
    row["top1_pct"] = max(float(row.get("top1_pct") or 0), dev)
    row["holder_count"] = holders or row.get("holder_count")
    row["liquidity_usd"] = liq or row.get("liquidity_usd")
    row["market_cap"] = mcap or row.get("market_cap")
    row["volume_24h"] = vol or row.get("volume_24h")
    if px:
        row["price_usd"] = px
    row["yodao_dev_holding"] = dev
    row["yodao_snipers"] = snipers
    row["yodao_insiders"] = insiders
    row["yodao_bundle"] = bundle
    row["yodao_fresh"] = fresh
    row["yodao_pct_completion"] = float(md.get("pct_completion") or row.get("yodao_pct_completion") or 0)
    row["yodao_tx_buy"] = float(md.get("tx_24h_buy") or 0)
    row["yodao_tx_sell"] = float(md.get("tx_24h_sell") or 0)
    row["yodao_pro"] = float(md.get("pro_traders") or md.get("pro_traders_holding") or 0)
    row["yodao_ok"] = True
    note = (
        f"yodao · dev {dev:.1f}% · sniper {snipers:.1f}% · "
        f"insider {insiders:.1f}% · bundle {bundle:.1f}% · fresh {fresh:.1f}%"
    )
    prev_note = row.get("holder_note") or ""
    if "yodao" not in prev_note:
        row["holder_note"] = (prev_note + " · " + note).strip(" ·") if prev_note else note
    else:
        row["holder_note"] = note
    flags = list(row.get("flags") or [])
    if dev >= 10 and "DEV_HOLDING_HIGH" not in flags:
        flags.append("DEV_HOLDING_HIGH")
    if snipers >= 15 and "SNIPERS_HIGH" not in flags:
        flags.append("SNIPERS_HIGH")
    if insiders >= 10 and "INSIDERS_HIGH" not in flags:
        flags.append("INSIDERS_HIGH")
    if bundle >= 15 and "BUNDLE_HIGH" not in flags:
        flags.append("BUNDLE_HIGH")
    if fresh >= 25 and "FRESH_WALLETS_HIGH" not in flags:
        flags.append("FRESH_WALLETS_HIGH")
    if top10 >= 40 and "CONCENTRATED_HOLDERS" not in flags:
        flags.append("CONCENTRATED_HOLDERS")
    if dev >= 10:
        row["top1_pct"] = max(float(row.get("top1_pct") or 0), dev)
    row["flags"] = flags
    if not row.get("creator") and md.get("creator"):
        row["creator"] = md["creator"]
    if not row.get("icon") and md.get("image"):
        row["icon"] = md["image"]
    if md.get("twitter") and not any(s.get("type") == "twitter" for s in (row.get("socials") or [])):
        row.setdefault("socials", []).append({"type": "twitter", "url": md["twitter"]})
    if md.get("telegram") and not any(s.get("type") == "telegram" for s in (row.get("socials") or [])):
        row.setdefault("socials", []).append({"type": "telegram", "url": md["telegram"]})
    if md.get("website") and md["website"] not in (row.get("websites") or []):
        row.setdefault("websites", []).append(md["website"])
    row["sec_provider"] = (row.get("sec_provider") or "") + "+yodao"
    return row


# ---------- FOMO API (https://fomoapi.io/docs) ----------
FOMO_API = os.getenv("FOMO_API_BASE", "https://api.fomoapi.io").rstrip("/")
FOMO_API_KEY = os.getenv("FOMO_API_KEY", "").strip()
FOMO_CACHE = {"ts": 0, "rows": []}
FOMO_TTL = 60


def _fomo_headers() -> Dict:
    h = {"Accept": "application/json"}
    if FOMO_API_KEY:
        h["authorization"] = f"Bearer {FOMO_API_KEY}"
    return h


def fomo_get(path: str, params: Optional[Dict] = None) -> Any:
    if not FOMO_API_KEY:
        return {"error": "FOMO_API_KEY kosong — ambil gratis di https://fomoapi.io/dashboard"}
    try:
        r = SESSION.get(
            f"{FOMO_API}{path}",
            headers=_fomo_headers(),
            params=params or {},
            timeout=15,
        )
        if r.status_code == 401:
            return {"error": "FOMO key invalid / missing"}
        if r.status_code == 402:
            return {"error": "FOMO credits exhausted"}
        if not r.ok:
            return {"error": f"FOMO HTTP {r.status_code}", "body": r.text[:200]}
        return r.json()
    except Exception as e:
        print("fomo_get error:", e)
        return {"error": str(e)}


def fomo_token_to_row(t: Dict, board: str) -> Dict:
    tok = t.get("token") if isinstance(t.get("token"), dict) else {}
    addr = (tok.get("address") or t.get("address") or "").strip()
    symbol = tok.get("symbol") or t.get("symbol") or "?"
    name = tok.get("name") or t.get("name") or ""
    network = str(t.get("network") or t.get("chain") or "solana").lower()
    if "robinhood" in network or network in ("4663", "rh"):
        chain = "robinhood"
    elif network in ("ethereum", "eth", "1"):
        chain = "ethereum"
    elif network in ("base", "8453"):
        chain = "base"
    elif network in ("bsc", "56"):
        chain = "bsc"
    else:
        chain = "solana"
    mcap = float(t.get("marketCapUsd") or t.get("marketCap") or 0)
    px = t.get("priceUsd") or t.get("price")
    chg = float(t.get("change24h") or t.get("priceChange24h") or 0)
    holders = t.get("holders")
    return {
        "symbol": symbol,
        "name": name,
        "token_address": addr,
        "chain": chain,
        "sector": chain.upper(),
        "dex": "fomo",
        "score": 60,
        "confidence": 70,
        "pressure": "BUY PRESSURE" if chg > 0 else "NEUTRAL",
        "verdict": f"FOMO {board.upper()}",
        "whale": "FOMO SMART MONEY",
        "flow": "NEUTRAL",
        "liquidity_usd": 0,
        "market_cap": mcap,
        "volume_24h": 0,
        "price_change_24h": chg,
        "price_usd": px,
        "age_hours": None,
        "pair_address": "",
        "url": f"https://dexscreener.com/{chain}/{addr}" if addr else "",
        "upside": "HIGH ROOM" if mcap and mcap < 500_000 else "SPECULATIVE",
        "upside_note": f"fomo board {board}",
        "icon": t.get("image") or "",
        "socials": [],
        "websites": [],
        "honeypot": False,
        "risk": "UNKNOWN",
        "flags": [f"FOMO_{board.upper()}"],
        "buy_tax": 0,
        "sell_tax": 0,
        "sec_provider": "fomoapi",
        "creator": "",
        "top_holders": [],
        "top10_pct": 0,
        "top1_pct": 0,
        "holder_count": holders if holders is not None else 0,
        "holder_note": f"fomo {board} rank {t.get('rank') or '-'}",
        "fomo_board": board,
        "fomo_rank": t.get("rank"),
        "source": "fomo",
        "fomo_url": f"https://fomo.family/tokens/{chain}/{addr}" if addr else "",
    }


def fetch_fomo_boards(limit: int = 25) -> List[Dict]:
    """Token boards: trending / most-held / graduated dari FOMO social traders."""
    now = time.time()
    if FOMO_CACHE["rows"] and now - FOMO_CACHE["ts"] < FOMO_TTL:
        return list(FOMO_CACHE["rows"])[:limit]
    if not FOMO_API_KEY:
        return []
    rows = []
    seen = set()
    for board in ("trending", "graduated", "most-held"):
        data = fomo_get(f"/v2/leaderboard/tokens/{board}", {"limit": min(limit, 40)})
        if not isinstance(data, dict) or data.get("error"):
            print("fomo board", board, data.get("error") if isinstance(data, dict) else data)
            continue
        if data.get("available") is False:
            continue
        for t in data.get("tokens") or []:
            if not isinstance(t, dict):
                continue
            row = fomo_token_to_row(t, board)
            key = (row.get("token_address") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(row)
    FOMO_CACHE["rows"] = rows
    FOMO_CACHE["ts"] = now
    return rows[:limit]


def fetch_fomo_smart_holders(address: str, limit: int = 20) -> Dict:
    """Smart-money holders FOMO yang pegang token ini."""
    if not address or not FOMO_API_KEY:
        return {}
    data = fomo_get(f"/token/{address}/holders", {"limit": limit})
    if not isinstance(data, dict) or data.get("error"):
        return data if isinstance(data, dict) else {}
    if data.get("available") is False:
        return {"available": False, "holders": []}
    holders = data.get("holders") or data.get("data") or []
    if not isinstance(holders, list):
        holders = []
    return {"available": True, "holders": holders, "count": len(holders)}


def apply_fomo_enrich(row: Dict) -> Dict:
    addr = row.get("token_address") or ""
    if not addr or not FOMO_API_KEY:
        row["fomo_ok"] = False
        return row
    sm = fetch_fomo_smart_holders(addr, 15)
    row["fomo_ok"] = bool(sm) and not sm.get("error")
    if not sm or sm.get("error") or sm.get("available") is False:
        if isinstance(sm, dict) and sm.get("error"):
            row["fomo_error"] = sm.get("error")
        return row
    holders = sm.get("holders") or []
    row["fomo_smart_holders"] = holders[:10]
    row["fomo_smart_count"] = len(holders)
    total_val = 0.0
    for h in holders:
        try:
            total_val += float(h.get("valueUsd") or 0)
        except (TypeError, ValueError):
            pass
    row["fomo_smart_value_usd"] = round(total_val, 2)
    if holders:
        names = []
        for h in holders[:5]:
            handle = h.get("handle") or h.get("user") or "?"
            names.append(f"@{handle}")
        row["holder_note"] = (
            (row.get("holder_note") or "") + f" · FOMO smart: {', '.join(names)}"
        ).strip(" ·")
        flags = list(row.get("flags") or [])
        if "FOMO_SMART_HOLDERS" not in flags:
            flags.append("FOMO_SMART_HOLDERS")
        if len(holders) >= 3 and "FOMO_CROWD_IN" not in flags:
            flags.append("FOMO_CROWD_IN")
        row["flags"] = flags
    # cek apakah token ada di board FOMO
    try:
        for board in ("trending", "graduated"):
            data = fomo_get(f"/v2/leaderboard/tokens/{board}", {"limit": 40})
            if not isinstance(data, dict) or data.get("error"):
                continue
            for t in data.get("tokens") or []:
                tok = t.get("token") if isinstance(t.get("token"), dict) else {}
                a = (tok.get("address") or t.get("address") or "").lower()
                if a == addr.lower():
                    row["fomo_board"] = board
                    row["fomo_rank"] = t.get("rank")
                    break
            if row.get("fomo_board"):
                break
    except Exception as e:
        print("fomo board check error:", e)
    return row


def fetch_pairs() -> List[Dict]:
    now = time.time()
    if CACHE["data"] and now - CACHE["ts"] < CACHE_TTL:
        return CACHE["data"]

    discovered = discover_token_addresses()
    addrs = [addr for _, addr in discovered]
    for old in remembered_addresses():
        if old not in addrs:
            addrs.append(old)
    pairs = fetch_pairs_for_tokens(addrs) if addrs else []

    # fallback ringan: search pair yang emang pair, bukan nama chain
    if len(pairs) < 8:
        for q in ["PEPE", "BONK", "WIF", "MOG", "BRETT", "TOSHI"]:
            try:
                data = _get_json(f"https://api.dexscreener.com/latest/dex/search?q={q}")
                pairs.extend(data.get("pairs") or [])
                time.sleep(0.15)
            except Exception as e:
                print("search fallback error:", q, e)

    CACHE["data"] = pairs
    CACHE["ts"] = now
    return pairs


def num(d: Optional[dict], *keys, default=0.0) -> float:
    cur = d or {}
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    try:
        return float(cur or 0)
    except (TypeError, ValueError):
        return default


def pair_age_hours(p: Dict) -> Optional[float]:
    created = p.get("pairCreatedAt")
    if not created:
        return None
    try:
        return ((time.time() * 1000 - float(created)) / 1000) / 3600
    except (TypeError, ValueError):
        return None


def is_pair_young(p: Dict, max_hours: int = 72) -> bool:
    age = pair_age_hours(p)
    return age is not None and 0 <= age <= max_hours


def base_symbol(p: Dict) -> str:
    return ((p.get("baseToken") or {}).get("symbol") or "").strip().upper()


def is_junk_base(p: Dict) -> bool:
    sym = base_symbol(p)
    if not sym or sym in JUNK_SYMBOLS:
        return True
    if len(sym) > 24:
        return True
    chain = (p.get("chainId") or "").upper()
    if sym == chain:
        return True
    return False


def pick_best_pair(pairs: List[Dict]) -> Optional[Dict]:
    if not pairs:
        return None

    def key(p):
        liq = num(p, "liquidity", "usd")
        vol = num(p, "volume", "h24")
        return (liq, vol)

    return sorted(pairs, key=key, reverse=True)[0]


def is_suspicious(p: Dict) -> bool:
    liq = num(p, "liquidity", "usd")
    vol = num(p, "volume", "h24")
    change = num(p, "priceChange", "h24")
    if liq < 300:
        return True
    if liq > 0 and vol / liq > 8:
        return True
    if change > 1500:
        return True
    return False


def score_pair(p: Dict) -> int:
    score = 0
    liq = num(p, "liquidity", "usd")
    vol = num(p, "volume", "h24")
    change = num(p, "priceChange", "h24")
    chain = p.get("chainId") or ""

    if 1_000 <= liq <= 300_000:
        score += 30
    elif liq <= 1_000_000:
        score += 20

    if liq > 0 and vol / liq > 0.2:
        score += 20

    if change > 20:
        score += 20
    elif change > 5:
        score += 10

    if chain in ["ethereum", "base", "solana"]:
        score += 10

    return min(score, 100)


def verdict(score: int) -> str:
    if score >= 70:
        return "STRONG EARLY GEM"
    if score >= 50:
        return "EARLY CANDIDATE"
    return "WATCHLIST"


def whale_badge(p: Dict) -> Optional[str]:
    liq = num(p, "liquidity", "usd")
    vol = num(p, "volume", "h24")
    if liq > 0 and vol >= 100_000 and vol / liq >= 2:
        return "WHALE BUYING"
    if liq >= 50_000 and vol / liq >= 1:
        return "SMART FLOW"
    return None


def buy_sell_pressure(p: Dict) -> Tuple[str, int]:
    h24 = ((p.get("txns") or {}).get("h24")) or {}
    buys = float(h24.get("buys") or 0)
    sells = float(h24.get("sells") or 0)
    if buys + sells == 0:
        return "NO DATA", 0
    ratio = buys / (sells + 1)
    if ratio >= 1.2:
        return "BUY PRESSURE", 15
    if ratio >= 1.05:
        return "BUY BIAS", 8
    if ratio < 0.9:
        return "SELL PRESSURE", -10
    return "NEUTRAL", 0


def accumulation_distribution(p: Dict) -> str:
    h24 = ((p.get("txns") or {}).get("h24")) or {}
    buys = float(h24.get("buys") or 0)
    sells = float(h24.get("sells") or 0)
    liq = num(p, "liquidity", "usd")
    vol = num(p, "volume", "h24")
    change = num(p, "priceChange", "h24")
    if buys + sells == 0:
        return "NO DATA"
    buy_ratio = buys / (sells + 1)
    if buy_ratio >= 1.1 and change < 80 and vol > liq * 0.5:
        return "ACCUMULATION"
    if buy_ratio < 0.9 and vol > liq and change > 30:
        return "DISTRIBUTION"
    return "NEUTRAL"


def confidence_score(base_score: int, whale: Optional[str], pressure_bonus: int, is_breakout: bool) -> int:
    bonus = pressure_bonus
    if whale == "WHALE BUYING":
        bonus += 30
    elif whale == "SMART FLOW":
        bonus += 15
    if is_breakout:
        bonus += 10
    confidence = base_score + bonus
    if pressure_bonus < 0 and whale is None:
        confidence = min(confidence, 40)
    return max(0, min(confidence, 100))


NARRATIVE_RULES = [
    ("MEME", ("meme", "pepe", "doge", "dog", "cat", "kitten", "inu", "frog", "ape", "monkey", "wojak", "bonk", "wif", "popcat", "squirrel", "banger", "stunk", "pumpdog")),
    ("DEFI", ("defi", "yield farm", "lending", "borrow", "vault", "aave", "morpho")),
    ("RWA", ("rwa", "real-world", "treasury", "tokenized stock", "nasdaq", "commodity")),
    ("DEPIN", ("depin", "helium", "render network")),
    ("AI / AGENT", (" ai", "ai ", "agent", "gpt", "llm", "agi", "flyai", "finch")),
    ("PREDICTION", ("polymarket", "kalshi", "prediction")),
    ("PERPS / DERIV", ("perp", "perps", "hyperliquid")),
    ("PRIVACY / ZK", ("privacy", " zk", "zk ", "zkats", "anon")),
    ("GAMING", ("gaming", "gamefi", "play2earn")),
    ("STABLECOIN", ("stablecoin", "usdc", "usdt", "dai ")),
    ("BTCFI", ("btcfi", "ordinals", "runes")),
    ("LAUNCHPAD", ("pump.fun", "pumpfun", "launchpad", "fairlaunch")),
    ("POLITICS", ("trump", "maga", "potus", "election", "vote")),
    ("ROBINHOOD CHAIN", ("robinhood",)),
    ("ARC CHAIN", ("circle arc",)),
]


def detect_narrative(row_or_pair: Dict) -> str:
    if "baseToken" in row_or_pair:
        base = row_or_pair.get("baseToken") or {}
        symbol = base.get("symbol") or ""
        name = base.get("name") or ""
        chain = row_or_pair.get("chainId") or ""
        dex = row_or_pair.get("dexId") or ""
    else:
        symbol = row_or_pair.get("symbol") or ""
        name = row_or_pair.get("name") or ""
        chain = row_or_pair.get("chain") or ""
        dex = row_or_pair.get("dex") or ""
    blob = f" {symbol} {name} {chain} ".lower()
    hits = []
    for label, keys in NARRATIVE_RULES:
        if any(k.lower() in blob for k in keys):
            hits.append(label)
    dex_l = str(dex).lower()
    chain_l = str(chain).lower()
    if chain_l == "robinhood" and "ROBINHOOD CHAIN" not in hits:
        hits.append("ROBINHOOD CHAIN")
    if chain_l == "arc" and "ARC CHAIN" not in hits:
        hits.append("ARC CHAIN")
    if dex_l in ("pumpswap", "pumpfun", "raydium") and "LAUNCHPAD" not in hits and "MEME" in hits:
        hits.append("LAUNCHPAD")
    return " · ".join(hits[:2]) if hits else "UNLABELED"


def enrich(p: Dict, is_breakout: bool = False) -> Dict:
    score = score_pair(p)
    whale = whale_badge(p)
    pressure_label, pressure_bonus = buy_sell_pressure(p)
    confidence = confidence_score(score, whale, pressure_bonus, is_breakout)
    base = p.get("baseToken") or {}
    liq = num(p, "liquidity", "usd")
    vol = num(p, "volume", "h24")
    mcap = p.get("marketCap") or p.get("fdv") or 0
    try:
        mcap = round(float(mcap or 0), 2)
    except (TypeError, ValueError):
        mcap = 0
    age = pair_age_hours(p)
    info = p.get("info") or {}
    icon = info.get("imageUrl") or info.get("icon") or ""
    socials = []
    for s in (info.get("socials") or []):
        if isinstance(s, dict) and s.get("url"):
            socials.append({
                "type": (s.get("type") or s.get("platform") or "social"),
                "url": s.get("url"),
            })
    websites = []
    for w in (info.get("websites") or []):
        if isinstance(w, dict) and w.get("url"):
            websites.append(w.get("url"))
        elif isinstance(w, str):
            websites.append(w)
    return {
        "symbol": base.get("symbol") or "UNKNOWN",
        "name": base.get("name") or "",
        "token_address": base.get("address") or "",
        "chain": (p.get("chainId") or "").upper(),
        "sector": (p.get("chainId") or "").upper(),
        "dex": p.get("dexId") or "",
        "score": score,
        "confidence": confidence,
        "pressure": pressure_label,
        "verdict": "BREAKOUT" if is_breakout else verdict(score),
        "whale": whale,
        "flow": accumulation_distribution(p),
        "liquidity_usd": round(liq, 2),
        "market_cap": mcap,
        "volume_24h": round(vol, 2),
        "price_usd": p.get("priceUsd"),
        "tx_buys_m5": int(((p.get("txns") or {}).get("m5") or {}).get("buys") or 0),
        "tx_sells_m5": int(((p.get("txns") or {}).get("m5") or {}).get("sells") or 0),
        "tx_buys_h1": int(((p.get("txns") or {}).get("h1") or {}).get("buys") or 0),
        "tx_sells_h1": int(((p.get("txns") or {}).get("h1") or {}).get("sells") or 0),
        "volume_m5": float(((p.get("volume") or {}).get("m5") or 0) or 0),
        "volume_h1": float(((p.get("volume") or {}).get("h1") or 0) or 0),
        "price_change_m5": round(num(p, "priceChange", "m5"), 2),
        "price_change_h1": round(num(p, "priceChange", "h1"), 2),
        "price_change_h6": round(num(p, "priceChange", "h6"), 2),
        "price_change_24h": round(num(p, "priceChange", "h24"), 2),
        "age_hours": round(age, 2) if age is not None else None,
        "pair_address": p.get("pairAddress"),
        "url": p.get("url") or "",
        "narrative": detect_narrative(p),
        "upside": "UNRATED",
        "upside_note": "",
        "icon": icon,
        "socials": socials,
        "websites": websites,
    }


def _truthy(v) -> bool:
    return str(v).lower() in {"1", "true", "yes"}


def _pct(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def check_goplus_evm(chain: str, address: str) -> Dict:
    cid = GOPLUS_CHAIN.get(chain.lower())
    if not cid:
        return {}
    url = f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={address}"
    data = _get_json(url, timeout=15)
    result = (data.get("result") or {})
    info = result.get(address.lower()) or result.get(address) or {}
    if not info:
        return {}
    flags = []
    if _truthy(info.get("is_honeypot")):
        flags.append("HONEYPOT")
    if _truthy(info.get("cannot_sell_all")):
        flags.append("CANNOT_SELL_ALL")
    if _truthy(info.get("cannot_buy")):
        flags.append("CANNOT_BUY")
    if _truthy(info.get("is_blacklisted")):
        flags.append("BLACKLIST")
    if _truthy(info.get("hidden_owner")):
        flags.append("HIDDEN_OWNER")
    if _truthy(info.get("is_proxy")):
        flags.append("PROXY")
    buy_tax = _pct(info.get("buy_tax"))
    sell_tax = _pct(info.get("sell_tax"))
    if sell_tax >= 15 or buy_tax >= 15:
        flags.append("HIGH_TAX")
    honeypot = "HONEYPOT" in flags or "CANNOT_SELL_ALL" in flags
    risk = "HONEYPOT" if honeypot else ("HIGH" if flags else "LOW")

    # Burn dari holder dead/blackhole (penting untuk Robinhood & EVM lain)
    dead = {
        "0x000000000000000000000000000000000000dead",
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000001",
        "0x000000000000000000000000000000000000dEaD".lower(),
    }
    burn_pct = 0.0
    for h in info.get("holders") or []:
        if not isinstance(h, dict):
            continue
        ha = str(h.get("address") or "").lower()
        try:
            pct = float(h.get("percent") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct <= 1.0:
            pct *= 100.0
        if ha in dead or "dead" in ha or ha.endswith("dead"):
            burn_pct += pct
        # locked holder sering LP lock contract
        if _truthy(h.get("is_locked")) and ha not in dead:
            pass
    # LP holders locked
    lp_locked_pct = 0.0
    for h in info.get("lp_holders") or []:
        if not isinstance(h, dict):
            continue
        try:
            pct = float(h.get("percent") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct <= 1.0:
            pct *= 100.0
        if _truthy(h.get("is_locked")) or str(h.get("tag") or "").lower() in ("blackhole", "burn", "dead"):
            lp_locked_pct += pct

    try:
        creator_pct = float(info.get("creator_percent") or 0)
        if creator_pct <= 1:
            creator_pct *= 100
    except (TypeError, ValueError):
        creator_pct = 0.0

    return {
        "provider": "goplus" + (f"-{chain.lower()}" if chain else ""),
        "honeypot": honeypot,
        "risk": risk,
        "flags": flags,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "owner": info.get("owner_address") or "",
        "creator": info.get("creator_address") or "",
        "creator_percent": round(creator_pct, 2),
        "lp_holders": info.get("lp_holder_count"),
        "burn_pct": round(burn_pct, 2),
        "lp_locked_pct": round(lp_locked_pct, 2),
        "holder_count": int(info.get("holder_count") or 0) if str(info.get("holder_count") or "").isdigit() or isinstance(info.get("holder_count"), int) else 0,
        "is_open_source": info.get("is_open_source"),
    }


def check_goplus_solana(address: str) -> Dict:
    url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={address}"
    data = _get_json(url, timeout=15)
    result = data.get("result") or {}
    info = result.get(address) or result.get(address.lower()) or {}
    if not isinstance(info, dict):
        info = {}
    flags = []
    if _truthy(info.get("honeypot")) or _truthy(info.get("is_honeypot")):
        flags.append("HONEYPOT")
    if info.get("mintable") or info.get("mint_authority"):
        flags.append("MINT_AUTH")
    if info.get("freezable") or info.get("freeze_authority"):
        flags.append("FREEZE_AUTH")
    metadata = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
    if metadata.get("mutable"):
        flags.append("MUTABLE_META")
    honeypot = "HONEYPOT" in flags
    risk = "HONEYPOT" if honeypot else ("HIGH" if flags else "LOW")
    return {
        "provider": "goplus-solana",
        "honeypot": honeypot,
        "risk": risk,
        "flags": flags,
        "buy_tax": 0,
        "sell_tax": 0,
    }


def _is_lp_holder(label: str, addr: str = "") -> bool:
    low = f"{label} {addr}".lower()
    keys = (
        "pool", "raydium", "lp", "pump", "pair", "liquidity", "univ2", "univ3",
        "pancake", "aerodrome", "camelot", "sushiswap", "curve", "balancer",
        "vault", "router",
    )
    return any(k in low for k in keys)


def _holder_snapshot(data: Dict) -> Dict:
    holders = data.get("topHolders") or []
    creator = data.get("creator") or ""
    top = []
    insider_pct = 0.0
    top10 = 0.0
    lp_pct_sum = 0.0
    for h in holders[:15]:
        if not isinstance(h, dict):
            continue
        pct = float(h.get("pct") or h.get("percentage") or 0)
        addr = h.get("address") or h.get("owner") or h.get("wallet") or ""
        insider = bool(h.get("insider") or h.get("isInsider"))
        label = str(h.get("label") or "")
        is_lp = _is_lp_holder(label, addr)
        top.append({
            "address": addr,
            "pct": round(pct, 2),
            "insider": insider,
            "label": label,
            "is_lp": is_lp,
        })
        if is_lp:
            lp_pct_sum += pct
            continue
        top10 += pct
        if insider:
            insider_pct += pct
    flags_h = []
    if top10 >= 50:
        flags_h.append("CONCENTRATED_HOLDERS")
    if insider_pct >= 15:
        flags_h.append("INSIDER_CLUSTER")
    if data.get("graphInsidersDetected") or data.get("insiderNetworks"):
        flags_h.append("INSIDER_NETWORK")
    if data.get("rugged"):
        flags_h.append("CREATOR_RUGGED")
    top1_raw = float(top[0]["pct"]) if top else 0.0
    non_lp = [h for h in top if not h.get("is_lp")]
    top1_whale = float(non_lp[0]["pct"]) if non_lp else 0.0
    # top1_pct = whale terbesar non-LP (untuk peringatan dump)
    top1_pct = top1_whale
    lp_locked = data.get("lpLocked")
    if lp_locked is None:
        markets = data.get("markets") or []
        if markets and isinstance(markets[0], dict):
            lp_locked = markets[0].get("lpLocked")
    lp_pct = data.get("lpLockedPct")
    if top1_whale >= 10:
        flags_h.append("DOMINANT_HOLDER")
        note = f"holder dominan non-LP {top1_whale:.1f}% (≥10%)"
    elif "CREATOR_RUGGED" in flags_h:
        note = "creator punya jejak rug"
    elif "INSIDER_CLUSTER" in flags_h or "INSIDER_NETWORK" in flags_h:
        note = "cluster insider/bundler"
    elif top10 >= 40:
        note = "top holder non-LP cukup terkonsentrasi"
    elif top1_raw >= 15 and top and top[0].get("is_lp"):
        note = f"top1 adalah LP pool ~{top1_raw:.1f}% (bukan whale)"
    elif top1_whale <= 0 and top1_raw <= 0:
        note = "data holder kosong"
    else:
        note = "sebaran holder non-LP relatif biasa"
    return {
        "creator": creator or "",
        "top_holders": top[:8],
        "top1_pct": round(top1_pct, 2),
        "top1_raw_pct": round(top1_raw, 2),
        "top10_pct": round(top10, 2),
        "lp_holder_pct": round(lp_pct_sum, 2),
        "insider_pct": round(insider_pct, 2),
        "holder_count": data.get("totalHolders") or data.get("holderCount") or len(holders),
        "lp_locked": bool(lp_locked) if lp_locked is not None else None,
        "lp_locked_pct": lp_pct,
        "rugged": bool(data.get("rugged")),
        "holder_note": note,
        "holder_flags": flags_h,
    }


def check_rugcheck(address: str) -> Dict:
    url = f"https://api.rugcheck.xyz/v1/tokens/{address}/report"
    data = _get_json(url, timeout=15)
    score = data.get("score")
    risks = data.get("risks") or []
    names = []
    for r in risks:
        if isinstance(r, dict):
            names.append(str(r.get("name") or r.get("level") or "risk"))
        else:
            names.append(str(r))
    snap = _holder_snapshot(data if isinstance(data, dict) else {})
    names.extend(snap["holder_flags"])
    level = str(data.get("score_normalised") or "")
    honeypot = any("honeypot" in n.lower() or "rugged" in n.lower() for n in names) or bool(data.get("rugged"))
    high = (score is not None and float(score) >= 1000) or bool(snap["holder_flags"])
    risk = "HONEYPOT" if honeypot else ("HIGH" if high or names else "LOW")
    out = {
        "provider": "rugcheck",
        "honeypot": honeypot,
        "risk": risk,
        "flags": names[:10],
        "buy_tax": 0,
        "sell_tax": 0,
        "rugcheck_score": score,
        "level": level,
    }
    out.update(snap)
    return out


def security_check(chain: str, address: str) -> Dict:
    if not address:
        return {"honeypot": None, "risk": "UNKNOWN", "flags": ["NO_ADDRESS"], "buy_tax": 0, "sell_tax": 0}
    key = f"{chain.lower()}:{address.lower()}"
    now = time.time()
    hit = SEC_CACHE.get(key)
    if hit and now - hit["ts"] < SEC_TTL:
        return hit["data"]

    chain_l = chain.lower()
    out = {
        "honeypot": None,
        "risk": "UNKNOWN",
        "flags": [],
        "buy_tax": 0,
        "sell_tax": 0,
        "provider": "",
    }
    try:
        if chain_l == "solana":
            try:
                out = check_rugcheck(address)
            except Exception as e:
                print("rugcheck error:", e)
                try:
                    out = check_goplus_solana(address)
                except Exception as e2:
                    print("goplus sol error:", e2)
                    out["flags"] = ["CHECK_FAILED"]
        elif chain_l in GOPLUS_CHAIN:
            out = check_goplus_evm(chain_l, address)
            if not out:
                out = {"honeypot": None, "risk": "UNKNOWN", "flags": ["NO_DATA"], "buy_tax": 0, "sell_tax": 0, "provider": "goplus"}
        else:
            out["flags"] = ["CHAIN_UNSUPPORTED"]
    except Exception as e:
        print("security error:", e)
        out["flags"] = ["CHECK_FAILED"]

    SEC_CACHE[key] = {"ts": now, "data": out}
    time.sleep(0.12)
    return out


ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "").strip()
SCAN_CHAIN = {
    "ethereum": 1,
    "base": 8453,
    "bsc": 56,
    "arbitrum": 42161,
    "avalanche": 43114,
}
DEAD_WALLETS = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}


def check_evm_burn(chain: str, token: str) -> Dict:
    if not ETHERSCAN_KEY:
        return {"ok": False, "reason": "ETHERSCAN_API_KEY kosong"}
    cid = SCAN_CHAIN.get((chain or "").lower())
    if not cid:
        return {"ok": False, "reason": "chain bukan EVM Etherscan"}
    token = (token or "").lower()
    burned = 0.0
    try:
        for sink in DEAD_WALLETS:
            url = (
                "https://api.etherscan.io/v2/api"
                f"?chainid={cid}&module=account&action=tokentx"
                f"&contractaddress={token}&address={sink}"
                f"&page=1&offset=100&sort=desc&apikey={ETHERSCAN_KEY}"
            )
            data = _get_json(url, timeout=12) or {}
            rows = data.get("result") or []
            if not isinstance(rows, list):
                continue
            for tx in rows:
                to = str(tx.get("to") or "").lower()
                if to != sink:
                    continue
                raw = float(tx.get("value") or 0)
                dec = int(tx.get("tokenDecimal") or 18)
                burned += raw / (10 ** dec)
            time.sleep(0.2)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "burned_tokens": burned}


# ---------- CoinMarketCap (burn / LP / buyback signals) ----------
CMC_API_KEY = os.getenv("CMC_API_KEY", "").strip()
CMC_BASE = os.getenv("CMC_API_BASE", "https://pro-api.coinmarketcap.com").rstrip("/")
CMC_PLATFORM = {
    "solana": "Solana",
    "ethereum": "Ethereum",
    "eth": "Ethereum",
    "bsc": "BSC",
    "bnb": "BSC",
    "base": "Base",
    "arbitrum": "Arbitrum",
    "avalanche": "Avalanche",
    "polygon": "Polygon",
    "optimism": "Optimism",
    "robinhood": "Robinhood",
    "arc": "Arc",
}


def cmc_headers() -> Dict:
    h = {"Accept": "application/json"}
    if CMC_API_KEY:
        h["X-CMC_PRO_API_KEY"] = CMC_API_KEY
    return h


def fetch_cmc_security(chain: str, address: str) -> Dict:
    """GET /v1/dex/security/detail — keyless via /public-api atau dengan CMC_API_KEY."""
    if not address:
        return {}
    platform = CMC_PLATFORM.get((chain or "").lower())
    if not platform:
        return {}
    try:
        if CMC_API_KEY:
            url = f"{CMC_BASE}/v1/dex/security/detail"
        else:
            url = f"{CMC_BASE}/public-api/v1/dex/security/detail"
        r = SESSION.get(
            url,
            params={"platformName": platform, "address": address},
            headers=cmc_headers(),
            timeout=14,
        )
        if not r.ok:
            print("cmc security", r.status_code, r.text[:160])
            return {}
        data = r.json().get("data")
        if isinstance(data, list) and data:
            return data[0] if isinstance(data[0], dict) else {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("cmc security error:", e)
        return {}


def fetch_cmc_token(chain: str, address: str) -> Dict:
    """GET /v1/dex/token — detail token CMC (tags, mcap, links)."""
    if not address:
        return {}
    platform = CMC_PLATFORM.get((chain or "").lower())
    if not platform:
        return {}
    try:
        if CMC_API_KEY:
            url = f"{CMC_BASE}/v1/dex/token"
        else:
            url = f"{CMC_BASE}/public-api/v1/dex/token"
        r = SESSION.get(
            url,
            params={"platform": platform, "address": address},
            headers=cmc_headers(),
            timeout=14,
        )
        if not r.ok:
            return {}
        data = r.json().get("data")
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("cmc token error:", e)
        return {}


def parse_cmc_mechanics(sec: Dict, tok: Dict) -> Dict:
    """Map CMC securityItems + token meta → LP / burn / buyback status."""
    items = sec.get("securityItems") or []
    codes = []
    hits = []
    for it in items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or it.get("name") or "").strip()
        desc = str(it.get("description") or "").strip()
        blob = f"{code} {desc}".lower()
        codes.append(blob)
        # isHit true = risk found; for positive LP lock wording varies
        if it.get("isHit") is True or "found" in code.lower() and "not found" not in code.lower():
            hits.append(blob)
    all_txt = " | ".join(codes)
    tags = tok.get("tags") or sec.get("tags") or []
    if isinstance(tags, list):
        tag_txt = " ".join(str(t).lower() for t in tags)
    else:
        tag_txt = str(tags).lower()
    meta_blob = " ".join([
        all_txt,
        tag_txt,
        str(tok.get("n") or tok.get("name") or ""),
        str(tok.get("desc") or tok.get("description") or ""),
    ]).lower()

    lp_status = "LP UNKNOWN"
    if any(k in meta_blob for k in (
        "liquidity burned", "lp burned", "lp burn", "liquidity locked",
        "lp locked", "lock liquidity", "liquidity lock",
    )):
        # distinguish hit vs not found
        if any("not found" in c and any(x in c for x in ("liquidity", "lp lock", "lp burn")) for c in codes):
            lp_status = "LP UNLOCKED"
        else:
            lp_status = "LP BURN / LOCK"
    if any("liquidity risk" in c and "not found" not in c for c in hits):
        lp_status = "LP RISK"
    # explicit not found liquidity risk → unlocked-ish
    if "liquidity risk not found" in all_txt and lp_status == "LP UNKNOWN":
        lp_status = "LP OK (CMC)"

    burn = "BURN UNKNOWN"
    if any(k in meta_blob for k in ("token burn", "supply burn", "burned", "deflation", "dead wallet", "burn mechanism")):
        if "not found" in all_txt and "burn" in all_txt and "burned" not in " ".join(hits):
            burn = "NO BURN (CMC)"
        else:
            burn = "TOKEN BURN (CMC)"
    if any("self-destruct" in c and "not found" not in c for c in hits):
        burn = "SELF-DESTRUCT RISK"

    buyback = "BUYBACK UNKNOWN"
    if any(k in meta_blob for k in ("buyback", "buy back", "buy-back", "repurchase", "revenue share")):
        buyback = "BUYBACK CLAIM (CMC)"

    level = str(sec.get("securityLevel") or "").lower()
    note = f"CMC security={level or '-'} · platform={sec.get('platformName') or '-'}"
    if not sec and not tok:
        note = "CMC: tidak ada data security/token"
    return {
        "lp_status": lp_status,
        "burn_status": burn,
        "buyback_status": buyback,
        "note": note,
        "cmc_level": level,
        "cmc_ok": bool(sec) or bool(tok),
    }


def apply_cmc_mechanics(row: Dict) -> Dict:
    """Isi LP/burn/buyback dari CoinMarketCap API."""
    chain = str(row.get("chain") or "").lower()
    addr = row.get("token_address") or ""
    if not addr or chain not in CMC_PLATFORM:
        return row
    sec = fetch_cmc_security(chain, addr)
    tok = fetch_cmc_token(chain, addr)
    parsed = parse_cmc_mechanics(sec, tok)
    row["cmc_security"] = {
        "level": sec.get("securityLevel"),
        "category": sec.get("categoryLevel"),
        "items": len(sec.get("securityItems") or []),
    }
    row["cmc_ok"] = parsed.get("cmc_ok")
    # CMC hanya fallback kalau GMGN belum isi
    if not row.get("gmgn_ok"):
        if parsed.get("lp_status") and parsed["lp_status"] != "LP UNKNOWN":
            row["lp_status"] = parsed["lp_status"]
        if parsed.get("burn_status") and parsed["burn_status"] not in ("BURN UNKNOWN",):
            row["burn_status"] = parsed["burn_status"]
        if parsed.get("buyback_status") and parsed["buyback_status"] not in ("BUYBACK UNKNOWN",):
            row["buyback_status"] = parsed["buyback_status"]
    row["mechanics_note"] = (row.get("mechanics_note") or "") + " | " + parsed.get("note", "")
    row["mechanics_note"] = row["mechanics_note"].strip(" |")
    # honeypot hint dari CMC
    for it in sec.get("securityItems") or []:
        code = str(it.get("code") or "").lower()
        if "honeypot" in code and "not found" not in code and it.get("isHit") is not False:
            if "not found" not in code:
                row["honeypot"] = True
                row["risk"] = "HONEYPOT"
    evm = sec.get("evmDisplay") or {}
    if str(evm.get("honeypotStatus") or "").lower() in ("yes", "true", "honeypot"):
        row["honeypot"] = True
        row["risk"] = "HONEYPOT"
    return row



# ---------- GMGN OpenAPI (burn / LP / dev rug) https://openapi.gmgn.ai ----------
GMGN_DEFAULT_KEY = "gmgn_basesolbscethmonadtron"
# Render sering set GMGN_API_KEY="" → override default jadi kosong → 401
_raw_gmgn = os.getenv("GMGN_API_KEY")
GMGN_API_KEY = (_raw_gmgn if _raw_gmgn is not None else GMGN_DEFAULT_KEY).strip() or GMGN_DEFAULT_KEY
GMGN_HOST = os.getenv("GMGN_API_BASE", "https://openapi.gmgn.ai").rstrip("/")
GMGN_CHAIN = {
    "solana": "sol",
    "sol": "sol",
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "bnb": "bsc",
    "base": "base",
}


def gmgn_get(path: str, params: Dict) -> Dict:
    """GET openapi.gmgn.ai — 401 = key kosong/salah (AUTH_INVALID / AUTH_KEY_INVALID)."""
    key = GMGN_API_KEY or GMGN_DEFAULT_KEY
    try:
        import uuid
        q = dict(params or {})
        q["timestamp"] = int(time.time())
        q["client_id"] = str(uuid.uuid4())
        r = SESSION.get(
            f"{GMGN_HOST}{path}",
            params=q,
            headers={"X-APIKEY": key, "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 401:
            # fallback key publik read-only
            if key != GMGN_DEFAULT_KEY:
                q["timestamp"] = int(time.time())
                q["client_id"] = str(uuid.uuid4())
                r = SESSION.get(
                    f"{GMGN_HOST}{path}",
                    params=q,
                    headers={"X-APIKEY": GMGN_DEFAULT_KEY, "Accept": "application/json"},
                    timeout=15,
                )
            if r.status_code == 401:
                try:
                    err = r.json()
                except Exception:
                    err = {}
                return {
                    "error": f"401 {err.get('error') or err.get('message') or 'AUTH'} — set GMGN_API_KEY di Render (jangan kosong)",
                }
        if not r.ok:
            return {"error": f"HTTP {r.status_code}", "body": r.text[:180]}
        data = r.json()
        if isinstance(data, dict) and data.get("code") not in (0, None, "0"):
            code = data.get("code")
            if code in (401, "401"):
                return {"error": f"401 {data.get('message') or data.get('error') or 'auth'}"}
            return {"error": data.get("message") or data.get("reason") or "gmgn error", "raw": data}
        if isinstance(data, dict) and "data" in data:
            return data.get("data") or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("gmgn_get error:", e)
        return {"error": str(e)}


def apply_gmgn_enrich(row: Dict) -> Dict:
    """Burn / LP / token dev dari GMGN. Dev serial/rug → avoid."""
    chain = str(row.get("chain") or "").lower()
    addr = (row.get("token_address") or "").strip()
    gchain = GMGN_CHAIN.get(chain)
    if not addr or not gchain:
        return row
    sec = gmgn_get("/v1/token/security", {"chain": gchain, "address": addr})
    info = gmgn_get("/v1/token/info", {"chain": gchain, "address": addr})
    if isinstance(sec, dict) and sec.get("error") and isinstance(info, dict) and info.get("error"):
        row["gmgn_error"] = sec.get("error") or info.get("error")
        return row
    if not isinstance(sec, dict):
        sec = {}
    if not isinstance(info, dict):
        info = {}
    row["gmgn_ok"] = True
    flags = list(row.get("flags") or [])

    burn_status = str(sec.get("burn_status") or "").lower()
    try:
        burn_ratio = float(sec.get("burn_ratio") or 0)
    except (TypeError, ValueError):
        burn_ratio = 0.0
    lock = sec.get("lock_summary") if isinstance(sec.get("lock_summary"), dict) else {}
    is_locked = bool(lock.get("is_locked"))
    try:
        lock_pct = float(lock.get("lock_percent") or 0)
    except (TypeError, ValueError):
        lock_pct = 0.0
    lock_detail = lock.get("lock_detail") or []
    blackhole = any(isinstance(x, dict) and x.get("is_blackhole") for x in lock_detail)
    if burn_status in ("burn", "burned", "blackhole") or burn_ratio >= 0.9 or blackhole:
        row["burn_status"] = f"LP/TOKEN BURN (GMGN {burn_ratio:.0%})" if burn_ratio else "LP/TOKEN BURN (GMGN)"
        row["lp_status"] = "LP BURN / LOCK"
    elif is_locked or lock_pct >= 0.5:
        row["lp_status"] = f"LP LOCKED (GMGN {lock_pct:.0%})" if lock_pct else "LP LOCKED (GMGN)"
        if row.get("burn_status") in (None, "", "BURN UNKNOWN"):
            row["burn_status"] = "NO BURN · LP LOCKED"
    else:
        if row.get("lp_status") in (None, "", "LP UNKNOWN"):
            row["lp_status"] = "LP UNLOCKED (GMGN)"
        if row.get("burn_status") in (None, "", "BURN UNKNOWN"):
            row["burn_status"] = "NO BURN (GMGN)" if burn_status in ("", "none", "unburn") else f"BURN {burn_status or 'UNKNOWN'}"

    fee = info.get("fee_distribution") or {}
    fee_blob = json.dumps(fee).lower() if fee else ""
    if any(k in fee_blob for k in ("buyback", "burn", "treasury")):
        row["buyback_status"] = "BUYBACK/FEE ROUTE (GMGN)"
    elif row.get("buyback_status") in (None, "", "BUYBACK UNKNOWN"):
        row["buyback_status"] = "BUYBACK UNKNOWN (GMGN)"

    honeypot = sec.get("is_honeypot") if sec.get("is_honeypot") is not None else sec.get("honeypot")
    if honeypot in (True, 1, "1", "yes", "true"):
        row["honeypot"] = True
        row["risk"] = "HONEYPOT"
        if "HONEYPOT" not in flags:
            flags.append("HONEYPOT")
    if sec.get("renounced_mint") is False:
        flags.append("MINT_NOT_RENOUNCED")
    if sec.get("renounced_freeze_account") is False:
        flags.append("FREEZE_NOT_RENOUNCED")
    try:
        top10 = float(sec.get("top_10_holder_rate") or 0)
        if 0 < top10 <= 1:
            top10 *= 100
        if top10 > 0:
            row["top10_pct"] = round(top10, 2)
    except (TypeError, ValueError):
        pass
    try:
        bt = float(sec.get("buy_tax") or 0)
        st = float(sec.get("sell_tax") or 0)
        if bt <= 1:
            bt *= 100
        if st <= 1:
            st *= 100
        if bt or st:
            row["buy_tax"] = round(bt, 2)
            row["sell_tax"] = round(st, 2)
    except (TypeError, ValueError):
        pass

    dev = info.get("dev") if isinstance(info.get("dev"), dict) else {}
    stat = info.get("stat") if isinstance(info.get("stat"), dict) else {}
    creator = dev.get("creator_address") or row.get("creator") or ""
    if creator:
        row["creator"] = creator
    creator_status = str(dev.get("creator_token_status") or "").lower()
    row["gmgn_creator_status"] = creator_status
    row["gmgn_creator"] = creator
    try:
        created_count = int(stat.get("creator_created_count") or dev.get("creator_open_count") or 0)
    except (TypeError, ValueError):
        created_count = 0
    try:
        creator_hold = float(stat.get("creator_hold_rate") or 0)
        if creator_hold <= 1:
            creator_hold *= 100
    except (TypeError, ValueError):
        creator_hold = 0.0
    row["gmgn_creator_hold_pct"] = round(creator_hold, 2)
    row["gmgn_creator_created_count"] = created_count
    ath = dev.get("ath_token_info") if isinstance(dev.get("ath_token_info"), dict) else {}
    try:
        ath_mc = float(ath.get("ath_mc") or 0) if ath else 0
    except (TypeError, ValueError):
        ath_mc = 0
    row["gmgn_dev_ath_mc"] = ath_mc
    row["gmgn_dev_ath_symbol"] = ath.get("symbol") or ""

    avoid = False
    avoid_reasons = []
    if created_count >= 8:
        avoid = True
        avoid_reasons.append(f"dev buat {created_count} token (serial)")
        flags.append("DEV_SERIAL_DEPLOYER")
    if creator_status == "creator_hold" and creator_hold >= 8:
        avoid = True
        avoid_reasons.append(f"dev masih hold {creator_hold:.1f}%")
        flags.append("DEV_STILL_HOLDING")
    if creator_status == "creator_hold" and created_count >= 3:
        avoid = True
        avoid_reasons.append("dev hold + multi token")
        flags.append("DEV_HOLD_MULTI")
    rug = sec.get("rug_ratio")
    if rug is None:
        rug = info.get("rug_ratio")
    try:
        rug_f = float(rug) if rug is not None else None
    except (TypeError, ValueError):
        rug_f = None
    if rug_f is not None:
        if rug_f > 1:
            rug_f = rug_f / 100.0
        row["gmgn_rug_ratio"] = rug_f
        if rug_f >= 0.3:
            avoid = True
            avoid_reasons.append(f"rug_ratio {rug_f:.2f}")
            flags.append("GMGN_HIGH_RUG_RATIO")
    if avoid:
        row["risk"] = "HIGH"
        row["gmgn_avoid"] = True
        row["gmgn_avoid_reason"] = "; ".join(avoid_reasons)
        if "DEV_RUG_RISK" not in flags:
            flags.append("DEV_RUG_RISK")
    else:
        row["gmgn_avoid"] = False
        if creator_status == "creator_close":
            flags.append("DEV_CLOSED")

    # --- analisa token lengkap GMGN ---
    tags = info.get("wallet_tags_stat") if isinstance(info.get("wallet_tags_stat"), dict) else {}
    price = info.get("price") if isinstance(info.get("price"), dict) else {}
    pool = info.get("pool") if isinstance(info.get("pool"), dict) else {}
    link = info.get("link") if isinstance(info.get("link"), dict) else {}

    def _n(v, default=0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    smart_n = int(tags.get("smart_wallets") or 0)
    sniper_n = int(tags.get("sniper_wallets") or 0)
    bundler_n = int(tags.get("bundler_wallets") or 0)
    fresh_n = int(tags.get("fresh_wallets") or 0)
    renowned_n = int(tags.get("renowned_wallets") or 0)
    whale_n = int(tags.get("whale_wallets") or 0)
    rat_n = int(tags.get("rat_trader_wallets") or 0)

    bundler_rate = _n(stat.get("top_bundler_trader_percentage") or stat.get("bot_degen_rate"))
    if bundler_rate <= 1:
        bundler_rate *= 100
    rat_rate = _n(stat.get("top_rat_trader_percentage"))
    if rat_rate <= 1:
        rat_rate *= 100
    fresh_rate = _n(stat.get("fresh_wallet_rate"))
    if fresh_rate <= 1:
        fresh_rate *= 100

    liq_g = _n(info.get("liquidity") or pool.get("liquidity"))
    holders_g = int(info.get("holder_count") or stat.get("holder_count") or 0)
    if holders_g and not row.get("holder_count"):
        row["holder_count"] = holders_g
    if liq_g and (not row.get("liquidity_usd") or float(row.get("liquidity_usd") or 0) <= 0):
        row["liquidity_usd"] = round(liq_g, 2)

    # price changes GMGN windows
    def _pct_chg(cur, old):
        try:
            c, o = float(cur), float(old)
            if o == 0:
                return None
            return round((c / o - 1) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    p_now = price.get("price")
    gmgn_chg = {
        "m1": _pct_chg(p_now, price.get("price_1m")),
        "m5": _pct_chg(p_now, price.get("price_5m")),
        "h1": _pct_chg(p_now, price.get("price_1h")),
        "h6": _pct_chg(p_now, price.get("price_6h")),
        "h24": _pct_chg(p_now, price.get("price_24h")),
    }
    buys_24 = int(_n(price.get("buys_24h")))
    sells_24 = int(_n(price.get("sells_24h")))

    renounced_mint = sec.get("renounced_mint")
    renounced_freeze = sec.get("renounced_freeze_account")
    launchpad = info.get("launchpad") or info.get("launchpad_platform") or ""
    launch_progress = info.get("launchpad_progress")

    # skor analisa GMGN 0-100
    score = 50
    notes = []
    if burn_status in ("burn", "burned") or burn_ratio >= 0.9 or blackhole:
        score += 12
        notes.append("LP/token burned")
    elif is_locked:
        score += 6
        notes.append("LP locked")
    else:
        score -= 8
        notes.append("LP unlocked")
    if creator_status == "creator_close":
        score += 10
        notes.append("dev closed")
    elif creator_status == "creator_hold":
        score -= 10
        notes.append("dev holding")
    if created_count >= 8:
        score -= 20
        notes.append("serial deployer")
    elif created_count >= 3:
        score -= 8
        notes.append("multi deploy")
    if smart_n >= 10:
        score += 10
        notes.append(f"{smart_n} smart")
    elif smart_n >= 3:
        score += 5
    if renowned_n >= 3:
        score += 5
        notes.append(f"{renowned_n} KOL")
    if sniper_n >= 30:
        score -= 10
        notes.append("banyak sniper")
    if bundler_rate >= 25 or bundler_n >= 50:
        score -= 12
        notes.append("bundler tinggi")
    if rat_rate >= 15 or rat_n >= 20:
        score -= 10
        notes.append("rat trader")
    if fresh_rate >= 40:
        score -= 6
        notes.append("fresh wallet tinggi")
    if renounced_mint is True:
        score += 4
    elif renounced_mint is False:
        score -= 8
        notes.append("mint belum renounce")
    if renounced_freeze is False:
        score -= 6
        notes.append("freeze aktif")
    if row.get("honeypot"):
        score = min(score, 10)
        notes.append("honeypot")
    if avoid:
        score = min(score, 25)
    score = max(0, min(100, score))

    if score >= 70 and not avoid and not row.get("honeypot"):
        gmgn_verdict = "GMGN CLEAN"
    elif score >= 45 and not avoid:
        gmgn_verdict = "GMGN MIXED"
    else:
        gmgn_verdict = "GMGN RISKY"

    row["gmgn_smart"] = smart_n
    row["gmgn_sniper"] = sniper_n
    row["gmgn_bundler"] = bundler_n
    row["gmgn_fresh"] = fresh_n
    row["gmgn_renowned"] = renowned_n
    row["gmgn_whale"] = whale_n
    row["gmgn_rat"] = rat_n
    row["gmgn_bundler_rate"] = round(bundler_rate, 1)
    row["gmgn_rat_rate"] = round(rat_rate, 1)
    row["gmgn_fresh_rate"] = round(fresh_rate, 1)
    row["gmgn_holders"] = holders_g
    row["gmgn_liq"] = round(liq_g, 2) if liq_g else None
    row["gmgn_launchpad"] = launchpad
    row["gmgn_launch_progress"] = launch_progress
    row["gmgn_renounced_mint"] = renounced_mint
    row["gmgn_renounced_freeze"] = renounced_freeze
    row["gmgn_chg"] = gmgn_chg
    row["gmgn_buys_24h"] = buys_24
    row["gmgn_sells_24h"] = sells_24
    row["gmgn_score"] = score
    row["gmgn_verdict"] = gmgn_verdict
    row["gmgn_notes"] = notes
    row["gmgn_twitter"] = link.get("twitter_username") or ""
    row["gmgn_website"] = link.get("website") or ""
    row["gmgn_telegram"] = link.get("telegram") or ""
    row["gmgn_report"] = {
        "score": score,
        "verdict": gmgn_verdict,
        "notes": notes,
        "smart": smart_n,
        "sniper": sniper_n,
        "bundler": bundler_n,
        "fresh": fresh_n,
        "renowned": renowned_n,
        "whale": whale_n,
        "rat": rat_n,
        "bundler_rate": round(bundler_rate, 1),
        "rat_rate": round(rat_rate, 1),
        "fresh_rate": round(fresh_rate, 1),
        "holders": holders_g,
        "liquidity": round(liq_g, 2) if liq_g else None,
        "launchpad": launchpad,
        "renounced_mint": renounced_mint,
        "renounced_freeze": renounced_freeze,
        "creator_status": creator_status,
        "creator_hold_pct": round(creator_hold, 2),
        "creator_created": created_count,
        "burn_ratio": burn_ratio,
        "lp_locked": is_locked,
        "chg": gmgn_chg,
        "buys_24h": buys_24,
        "sells_24h": sells_24,
        "avoid": avoid,
        "avoid_reason": row.get("gmgn_avoid_reason") or "",
    }

    row["flags"] = flags
    row["sec_provider"] = (row.get("sec_provider") or "") + "+gmgn"
    row["mechanics_note"] = (
        f"GMGN {gmgn_verdict} skor {score} · burn={sec.get('burn_status')} · "
        f"smart={smart_n} sniper={sniper_n} · dev={creator_status or '-'} created={created_count}"
    )
    return row


def is_dev_rug_risk(row: Dict) -> bool:
    if row.get("gmgn_avoid"):
        return True
    flags = " ".join(str(f).upper() for f in (row.get("flags") or []))
    return any(x in flags for x in (
        "DEV_RUG_RISK", "DEV_SERIAL_DEPLOYER", "GMGN_HIGH_RUG_RATIO", "CREATOR_RUGGED",
    ))


def detect_mechanics(row: Dict) -> Dict:
    flags = " ".join(str(f).lower() for f in (row.get("flags") or []))
    blob = " ".join([
        str(row.get("name") or ""),
        str(row.get("symbol") or ""),
        " ".join(str(w) for w in (row.get("websites") or [])),
        " ".join(str((s or {}).get("url") or "") for s in (row.get("socials") or [])),
        flags,
    ]).lower()
    chain_l = str(row.get("chain") or "").lower()
    lp = row.get("lp_locked")
    lp_pct = float(row.get("lp_locked_pct") or row.get("goplus_lp_locked_pct") or 0)
    if lp is True or lp_pct >= 50 or "lp burned" in flags or "liquidity burned" in flags:
        lp_status = "LP BURN / LOCK"
    elif lp is False and lp_pct <= 0:
        lp_status = "LP UNLOCKED"
    else:
        lp_status = "LP UNKNOWN"
    burn = "TOKEN BURN CLAIM" if any(k in blob for k in ("burn", "deflation", "dead wallet")) else "BURN UNKNOWN"
    if "cannot burn" in flags or "no burn" in blob:
        burn = "NO BURN"
    # GoPlus dead-wallet burn (Robinhood + EVM)
    try:
        gp_burn = float(row.get("goplus_burn_pct") or 0)
    except (TypeError, ValueError):
        gp_burn = 0.0
    if gp_burn >= 1:
        burn = f"TOKEN BURN {gp_burn:.1f}% (GoPlus dead)"
        if chain_l == "robinhood":
            burn = f"TOKEN BURN {gp_burn:.1f}% (Robinhood/GoPlus)"
    onchain = row.get("burn_onchain")
    if isinstance(onchain, dict) and onchain.get("ok"):
        amt = float(onchain.get("burned_tokens") or 0)
        if amt > 0:
            burn = f"BURNED {amt:.2f}"
        elif gp_burn < 1:
            burn = "NO BURN ONCHAIN"
    buyback = "BUYBACK CLAIM" if any(k in blob for k in ("buyback", "buy back", "buy-back", "repurchase")) else "BUYBACK UNKNOWN"
    # Robinhood: sering klaim di web/twitter
    if chain_l == "robinhood" and buyback == "BUYBACK UNKNOWN":
        if any(k in blob for k in ("buyback", "buy back", "revenue", "treasury")):
            buyback = "BUYBACK CLAIM (Robinhood meta)"
    note = "LP/burn: RugCheck·GoPlus·GMGN·CMC · Robinhood via GoPlus chain 4663"
    return {"lp_status": lp_status, "burn_status": burn, "buyback_status": buyback, "note": note}


def attach_security(rows: List[Dict]) -> List[Dict]:
    for row in rows:
        sec = security_check(row.get("chain") or row.get("sector") or "", row.get("token_address") or "")
        row["honeypot"] = sec.get("honeypot")
        row["risk"] = sec.get("risk") or "UNKNOWN"
        row["flags"] = sec.get("flags") or []
        row["buy_tax"] = sec.get("buy_tax") or 0
        row["sell_tax"] = sec.get("sell_tax") or 0
        row["sec_provider"] = sec.get("provider") or ""
        row["creator"] = sec.get("creator") or ""
        row["top_holders"] = sec.get("top_holders") or []
        row["top10_pct"] = sec.get("top10_pct") or 0
        row["insider_pct"] = sec.get("insider_pct") or 0
        row["holder_note"] = sec.get("holder_note") or ""
        row["top1_pct"] = sec.get("top1_pct") or 0
        row["top1_raw_pct"] = sec.get("top1_raw_pct") or 0
        row["lp_holder_pct"] = sec.get("lp_holder_pct") or 0
        row["holder_count"] = sec.get("holder_count") or 0
        row["lp_locked"] = sec.get("lp_locked")
        row["lp_locked_pct"] = sec.get("lp_locked_pct")
        # GoPlus burn/LP (termasuk Robinhood chain 4663)
        if sec.get("burn_pct") is not None:
            row["goplus_burn_pct"] = sec.get("burn_pct")
        if sec.get("lp_locked_pct") is not None:
            row["goplus_lp_locked_pct"] = sec.get("lp_locked_pct")
            if float(sec.get("lp_locked_pct") or 0) >= 50:
                row["lp_locked"] = True
                row["lp_locked_pct"] = sec.get("lp_locked_pct")
        if sec.get("creator") and not row.get("creator"):
            row["creator"] = sec.get("creator")
        if "DOMINANT_HOLDER" in (sec.get("holder_flags") or []):
            row["flags"] = list(row.get("flags") or []) + ["DOMINANT_HOLDER"]
        mech = detect_mechanics(row)
        row["lp_status"] = mech["lp_status"]
        row["burn_status"] = mech["burn_status"]
        row["buyback_status"] = mech["buyback_status"]
        row["mechanics_note"] = mech["note"]
        row["rugged"] = bool(sec.get("rugged"))
        if sec.get("honeypot"):
            row["confidence"] = min(int(row.get("confidence") or 0), 25)
            row["verdict"] = "HONEYPOT RISK"
        elif row.get("risk") == "HIGH":
            row["confidence"] = min(int(row.get("confidence") or 0), 55)
        label, note = upside_label(row)
        row["upside"] = label
        row["upside_note"] = note
    return sorted(rows, key=row_rank)


def row_rank(row: Dict):
    flags = [str(f).upper() for f in (row.get("flags") or [])]
    blob = " ".join(flags)
    honeypot = bool(row.get("honeypot") or row.get("risk") == "HONEYPOT")
    hard = honeypot or any(x in blob for x in ("HONEYPOT", "CANNOT_SELL", "CANNOT_BUY", "BLACKLIST"))
    caution = row.get("risk") == "HIGH" or any(x in flags for x in ("HIGH_TAX", "HIDDEN_OWNER", "MINT_AUTH", "FREEZE_AUTH"))
    unknown = row.get("risk") in ("UNKNOWN", "", None) or "CHECK_FAILED" in flags or "NO_DATA" in flags
    if hard:
        tier = 3
    elif caution:
        tier = 2
    elif unknown:
        tier = 1
    else:
        tier = 0
    upside_order = {
        "HIGH ROOM": 0,
        "SPECULATIVE": 1,
        "MODERATE": 2,
        "LIMITED": 3,
        "CAUTION SETUP": 4,
        "THIN": 5,
        "WASHY": 6,
        "UNRATED": 7,
        "LOW ROOM": 8,
        "NO UPSIDE": 9,
    }
    return (tier, upside_order.get(str(row.get("upside") or ""), 8), -int(row.get("confidence") or 0))


def upside_label(row: Dict) -> Tuple[str, str]:
    """Spekulatif: ruang gerak, bukan prediksi 100x."""
    if row.get("honeypot") or row.get("risk") == "HONEYPOT":
        return "NO UPSIDE", "honeypot/red flag"
    liq = float(row.get("liquidity_usd") or 0)
    mcap = float(row.get("market_cap") or 0)
    vol = float(row.get("volume_24h") or 0)
    tax = max(float(row.get("sell_tax") or 0), float(row.get("buy_tax") or 0))
    flags = [str(f).upper() for f in (row.get("flags") or [])]
    if tax >= 15 or any(x in flags for x in ("HIGH_TAX", "CANNOT_SELL_ALL", "BLACKLIST")):
        return "NO UPSIDE", "tax/flag berbahaya"
    if mcap <= 0 or liq <= 0:
        return "UNRATED", "data mcap/liq kosong"

    vol_liq = vol / liq if liq else 0
    liq_mcap = liq / mcap if mcap else 0

    if vol_liq > 6:
        return "WASHY", "vol jauh di atas liq"
    if liq < 8000:
        return "THIN", "liq terlalu tipis"
    if mcap > 5_000_000:
        return "LIMITED", "mcap sudah besar"
    if row.get("risk") == "HIGH":
        return "CAUTION SETUP", "ada flag keamanan"

    small = mcap <= 400_000
    mid = mcap <= 1_500_000
    liq_ok = 15_000 <= liq <= 400_000
    vol_ok = 0.25 <= vol_liq <= 6
    depth_ok = 0.05 <= liq_mcap <= 0.8

    if small and liq_ok and vol_ok and depth_ok and row.get("risk") in ("LOW", None, ""):
        return "HIGH ROOM", "mcap kecil, liq/vol masih masuk akal"
    if (small or mid) and vol_ok and liq >= 10_000:
        return "SPECULATIVE", "ada ruang, belum rapi"
    if mid and liq_ok:
        return "MODERATE", "setup biasa"
    return "LOW ROOM", "sedikit ruang atau struktur jelek"


def group_best_by_token(pairs: List[Dict]) -> Dict[str, Dict]:
    buckets: Dict[str, List[Dict]] = {}
    for p in pairs:
        if is_junk_base(p):
            continue
        chain = (p.get("chainId") or "").lower()
        if chain and chain not in CHAINS:
            continue
        base = p.get("baseToken") or {}
        addr = (base.get("address") or "").lower()
        if not addr:
            continue
        buckets.setdefault(f"{chain}:{addr}", []).append(p)

    best: Dict[str, Dict] = {}
    for key, group in buckets.items():
        picked = pick_best_pair(group)
        if picked:
            best[key] = picked
    return best


FOMO_REQUIRE_SMART = os.getenv("FOMO_REQUIRE_SMART", "1") == "1"
FOMO_MIN_SMART = int(os.getenv("FOMO_MIN_SMART", "1"))


def has_fomo_smart_wallet(row: Dict) -> bool:
    """Lolos hanya jika ada smart wallet FOMO yang pegang (atau di board FOMO)."""
    if not FOMO_REQUIRE_SMART:
        return True
    n = int(row.get("fomo_smart_count") or 0)
    if n >= FOMO_MIN_SMART:
        return True
    flags = " ".join(str(f).upper() for f in (row.get("flags") or []))
    if "FOMO_SMART_HOLDERS" in flags or "FOMO_CROWD_IN" in flags:
        return True
    # board trending/graduated = sudah di radar FOMO traders
    if row.get("fomo_board") in ("trending", "graduated"):
        return True
    if row.get("source") == "fomo" and row.get("token_address"):
        return True
    return False


def is_early_setup(row: Dict, mode: str = "balanced") -> bool:
    """Hidden gem: masih early, bukan honeypot. Longgar di tape, kaku di rug."""
    if row.get("honeypot") or row.get("risk") in ("HONEYPOT",):
        return False
    if mode == "strict" and row.get("risk") == "HIGH":
        return False
    top10 = float(row.get("top10_pct") or 0)
    holders = int(row.get("holder_count") or 0)
    flags = " ".join(str(f).upper() for f in (row.get("flags") or []))
    if any(x in flags for x in ("HONEYPOT", "CANNOT_SELL", "CANNOT_BUY", "BLACKLIST", "RUGGED")):
        return False
    tax = max(float(row.get("sell_tax") or 0), float(row.get("buy_tax") or 0))
    if tax >= 12:
        return False
    liq = float(row.get("liquidity_usd") or 0)
    mcap = float(row.get("market_cap") or 0)
    vol = float(row.get("volume_24h") or 0)
    chg = float(row.get("price_change_24h") or 0)
    age = row.get("age_hours")
    if liq <= 0 or vol <= 0:
        return False
    ratio = vol / liq
    if mode == "aggressive":
        age_ok = age is None or (0.8 <= age <= 72)
        band = 6_000 <= liq <= 350_000 and 10_000 <= mcap <= 1_200_000
        tape = 0.2 <= ratio <= 6.5 and -8 <= chg <= 90
    elif mode == "strict":
        age_ok = age is not None and 2 <= age <= 30
        band = 12_000 <= liq <= 180_000 and 20_000 <= mcap <= 500_000
        tape = 0.35 <= ratio <= 4.2 and 0 <= chg <= 55
    else:
        age_ok = age is None or (1 <= age <= 48)
        band = 8_000 <= liq <= 250_000 and 15_000 <= mcap <= 800_000
        tape = 0.25 <= ratio <= 5.5 and -3 <= chg <= 70
    if not age_ok or not band or not tape:
        return False
    if holders > 0 and top10 >= 42:
        return False
    if is_dev_rug_risk(row):
        return False
    return True


@app.get("/scan/top")
def scan_top(
    limit: int = 10,
    mode: str = Query("balanced", enum=["strict", "balanced", "aggressive"]),
):
    try:
        best_pairs = group_best_by_token(fetch_pairs())
        results = []
        for p in best_pairs.values():
            if not is_pair_young(p, 72 if mode == "aggressive" else (30 if mode == "strict" else 48)):
                continue
            liq = num(p, "liquidity", "usd")
            vol = num(p, "volume", "h24")
            chg = num(p, "priceChange", "h24")
            if liq < 6_000 or vol < 3_000:
                continue
            if liq > 400_000:
                continue
            if vol / max(liq, 1) > 7:
                continue
            if chg < -10 or chg > 95:
                continue
            results.append(enrich(p, False))
        results.sort(key=lambda x: x["confidence"], reverse=True)
        secured = attach_security(results[:40])
        # Yodao pump.fun early pools (Solana) — sumber tambahan
        try:
            seen_mints = {(r.get("token_address") or "").lower() for r in secured}
            for yr in fetch_yodao_rows()[:30]:
                mint = (yr.get("token_address") or "").lower()
                if not mint or mint in seen_mints:
                    continue
                if is_early_setup(yr, mode) or (mode == "aggressive" and yr.get("risk") != "HIGH"):
                    secured.append(yr)
                    seen_mints.add(mint)
        except Exception as ye:
            print("yodao merge error:", ye)
        picked = [r for r in secured if is_early_setup(r, mode) or (
            mode == "aggressive" and r.get("source") == "yodao" and r.get("risk") != "HIGH"
        )]
        # merge FOMO board tokens (sudah punya smart money sosial)
        try:
            seen_mints = {(r.get("token_address") or "").lower() for r in picked}
            for fr in fetch_fomo_boards(limit=25):
                mint = (fr.get("token_address") or "").lower()
                if not mint or mint in seen_mints:
                    continue
                if is_early_setup(fr, mode) or mode == "aggressive":
                    fr["fomo_smart_count"] = max(int(fr.get("fomo_smart_count") or 0), 1)
                    picked.append(fr)
                    seen_mints.add(mint)
        except Exception as fe:
            print("fomo merge discovery error:", fe)
        # cek smart wallet FOMO (mahal credits → hanya top kandidat)
        if FOMO_REQUIRE_SMART and FOMO_API_KEY and mode != "aggressive":
            enriched = []
            for r in picked[:25]:
                if r.get("source") == "fomo" or int(r.get("fomo_smart_count") or 0) >= FOMO_MIN_SMART:
                    enriched.append(r)
                    continue
                try:
                    apply_fomo_enrich(r)
                except Exception as e:
                    print("fomo filter enrich:", e)
                if has_fomo_smart_wallet(r):
                    enriched.append(r)
            picked = enriched
        elif FOMO_REQUIRE_SMART and not FOMO_API_KEY and mode != "aggressive":
            # tanpa key: hanya yang source fomo (kosong) → discovery kosong, lebih aman
            picked = [r for r in picked if has_fomo_smart_wallet(r)]
        picked.sort(key=lambda x: (
            int(x.get("fomo_smart_count") or 0),
            x.get("confidence") or 0,
            x.get("score") or 0,
        ), reverse=True)
        remember_tokens(picked[:limit])
        return picked[:limit]
    except Exception as e:
        print("DISCOVERY ERROR:", e)
        return []


@app.get("/scan/yodao")
def scan_yodao(limit: int = 20, stage: str = Query("all", enum=["all", "new", "completing", "graduated"])):
    """Kandidat Pump.fun dari Yodao API (dev.yodao.io)."""
    try:
        rows = fetch_yodao_rows(force=True)
        if stage != "all":
            rows = [r for r in rows if r.get("yodao_stage") == stage]
        rows.sort(key=lambda x: (x.get("score") or 0, x.get("volume_24h") or 0), reverse=True)
        return rows[:limit]
    except Exception as e:
        print("YODAO SCAN ERROR:", e)
        return []


@app.get("/scan/fomo")
def scan_fomo(limit: int = 20, board: str = Query("all", enum=["all", "trending", "graduated", "most-held"])):
    """Token board dari FOMO social traders (fomoapi.io). Butuh FOMO_API_KEY."""
    if not FOMO_API_KEY:
        return {"error": "Set FOMO_API_KEY di Render. Gratis: https://fomoapi.io/dashboard"}
    try:
        rows = fetch_fomo_boards(limit=40)
        if board != "all":
            rows = [r for r in rows if r.get("fomo_board") == board]
        return rows[:limit]
    except Exception as e:
        print("FOMO SCAN ERROR:", e)
        return {"error": str(e)}


@app.get("/scan/quality")
def scan_quality(limit: int = 20):
    """Token dengan tape sehat, window POSSIBLE, keamanan hijau."""
    try:
        best_pairs = group_best_by_token(fetch_pairs())
        rows = []
        for p in best_pairs.values():
            liq = num(p, "liquidity", "usd")
            vol = num(p, "volume", "h24")
            if liq < 8_000 or vol < 5_000:
                continue
            rows.append(enrich(p, False))
        rows = attach_security(rows)
        picked = []
        for row in rows:
            if row.get("honeypot") or row.get("risk") in ("HONEYPOT", "HIGH"):
                continue
            rpt = ca_analysis(row)
            row["ca_report"] = rpt
            row["health_score"] = rpt.get("health_score")
            row["safety_score"] = rpt.get("safety_score")
            row["trend_score"] = rpt.get("trend_score")
            row["trend_window"] = rpt.get("trend_window")
            row["green_alert"] = is_green_signal(row)
            row["fomo_url"] = fomo_url(row)
            if (
                float(rpt.get("health_score") or 0) >= 75
                and str(rpt.get("trend_window") or "") == "POSSIBLE"
                and float(rpt.get("safety_score") or 0) >= 70
            ):
                picked.append(row)
        picked.sort(key=lambda x: (x.get("safety_score") or 0, x.get("confidence") or 0), reverse=True)
        return picked[:limit]
    except Exception as e:
        print("QUALITY ERROR:", e)
        return []


@app.get("/scan/breakout")
def scan_breakout(limit: int = 10):
    try:
        results = []
        for p in group_best_by_token(fetch_pairs()).values():
            liq = num(p, "liquidity", "usd")
            vol = num(p, "volume", "h24")
            change = num(p, "priceChange", "h24")
            age = pair_age_hours(p)
            if age is None or age < 168 or liq < 50_000 or vol < 200_000 or change < 30:
                continue
            results.append(enrich(p, True))
        ranked = sorted(results, key=lambda x: x["confidence"], reverse=True)[:limit]
        return attach_security(ranked)
    except Exception as e:
        print("BREAKOUT ERROR:", e)
        return []


def ca_analysis(row: Dict) -> Dict:
    """Saringan CA: verifikasi profil + bukan honeypot + peluang tape beberapa jam.
    Bukan prediksi harga."""
    flags = [str(f).upper() for f in (row.get("flags") or [])]
    socials = row.get("socials") or []
    sites = row.get("websites") or []
    liq = float(row.get("liquidity_usd") or 0)
    vol = float(row.get("volume_24h") or 0)
    mcap = float(row.get("market_cap") or 0)
    chg = float(row.get("price_change_24h") or 0)
    age = row.get("age_hours")
    try:
        age = float(age) if age is not None else None
    except (TypeError, ValueError):
        age = None
    vl = (vol / liq) if liq else 0
    top10 = float(row.get("top10_pct") or 0)
    honeypot = bool(row.get("honeypot") or row.get("risk") == "HONEYPOT")
    checks = []

    verified = bool(row.get("icon")) and (len(socials) + len(sites) >= 1)
    if verified:
        checks.append("profil Dex ada (icon + social/web)")
    else:
        checks.append("profil lemah / belum lengkap")

    rh_empty = str(row.get("chain") or "").upper() in ("ROBINHOOD",) and top10 <= 0
    if honeypot or any("HONEYPOT" in f or "RUGGED" in f or "CANNOT_SELL" in f for f in flags):
        checks.append("honeypot / rugged / cannot sell")
        trend = "AVOID"
        note = "jangan dipegang"
    elif chg <= -40:
        checks.append(f"24h {chg:.0f}% — dump dalam")
        trend = "UNLIKELY"
        note = "harga sudah pecah, bukan window naik"
    elif chg <= -15:
        checks.append(f"24h {chg:.0f}% — retrace")
        trend = "WATCH"
        note = "masih turun, jangan baca sebagai trending"
    elif row.get("risk") == "HIGH":
        checks.append("risk HIGH")
        trend = "UNLIKELY"
        note = "flag keamanan"
    elif vl > 6 or str(row.get("upside") or "") in ("WASHY", "THIN", "NO UPSIDE"):
        checks.append(f"tape panas vol/liq {vl:.1f}x")
        trend = "UNLIKELY"
        note = "sudah rame / washy — trending sudah terjadi"
    elif chg >= 80:
        checks.append(f"24h sudah +{chg:.0f}%")
        trend = "UNLIKELY"
        note = "momentum sudah jalan, risiko exit"
    elif age is not None and age < 1:
        checks.append(f"umur {age:.1f} jam — terlalu baru")
        trend = "WATCH"
        note = "launch candle, pantau dulu"
    elif age is not None and age > 96:
        checks.append(f"umur {age:.0f} jam")
        trend = "WATCH"
        note = "bukan early window"
    elif liq < 15000 or mcap < 20000:
        checks.append("liq/mcap tipis")
        trend = "WATCH"
        note = "mudah disapu"
    elif (
        row.get("risk") in ("LOW", None, "")
        and not honeypot
        and not rh_empty
        and 0.3 <= vl <= 4
        and 8 <= chg <= 40
        and age is not None and 3 <= age <= 48
        and top10 < 40
    ):
        checks.append("tape waras, 24h plus kecil, umur masih window")
        trend = "POSSIBLE"
        note = "spekulatif — bukan jaminan naik"
    else:
        checks.append("belum rapi untuk window beberapa jam")
        trend = "WATCH"
        note = "pantau, jangan anggap trending"

    if top10 >= 50:
        checks.append(f"top10 {top10}% terkonsentrasi")
    if rh_empty:
        checks.append("holder Robinhood kosong — verifikasi dangkal")

    # Gauge: arah harga masuk hitungan
    health = 55
    if 0.4 <= vl <= 3.5:
        health += 20
    elif vl > 6:
        health -= 25
    if chg <= -40:
        health -= 35
    elif chg <= -15:
        health -= 20
    elif 8 <= chg <= 40:
        health += 15
    elif chg >= 80:
        health -= 15
    health = max(5, min(92, health))

    safety = 70 if not honeypot else 8
    if row.get("risk") == "HIGH":
        safety = 25
    if rh_empty:
        safety = min(safety, 42)
    hard_flag = any(
        any(k in f for k in ("HONEYPOT", "RUGGED", "MINT", "FREEZE", "HIDDEN", "CANNOT", "BLACKLIST"))
        for f in flags
    )
    if hard_flag:
        safety = min(safety, 48)
    if chg <= -40:
        safety = min(safety, 40)

    trend_score = {"POSSIBLE": 72, "WATCH": 38, "UNLIKELY": 16, "AVOID": 6}.get(trend, 30)
    if chg < 0:
        trend_score = min(trend_score, 28)

    return {
        "verified_profile": verified,
        "not_honeypot": not honeypot,
        "trend_window": trend,
        "trend_note": note,
        "checks": checks,
        "vol_liq": round(vl, 2),
        "health_score": health,
        "safety_score": safety,
        "trend_score": trend_score,
    }


@app.get("/scan/ca")
def scan_ca(address: str = Query(..., min_length=8), chain: str = Query("")):
    """Analisa satu CA / mint. Tidak lewat filter umur."""
    addr = address.strip()
    if addr.startswith("0x"):
        if len(addr) != 42:
            return {"error": "CA EVM harus 42 karakter (0x + 40 hex). Yang ditempel kepotong.", "address": addr}
    elif len(addr) < 32:
        return {"error": "CA / mint terlalu pendek. Tempel address penuh.", "address": addr}
    try:
        pairs = fetch_pairs_for_tokens([addr])
        want_addr = addr.lower()
        matched = []
        for p in pairs:
            if chain and (p.get("chainId") or "").lower() != chain.lower():
                continue
            base = ((p.get("baseToken") or {}).get("address") or "").lower()
            quote = ((p.get("quoteToken") or {}).get("address") or "").lower()
            pair = (p.get("pairAddress") or "").lower()
            if want_addr in (base, quote, pair):
                matched.append(p)
        pairs = matched
        rows: List[Dict] = []
        if not pairs:
            # fallback Solana: Yodao market-data saja
            md = fetch_yodao_market(addr)
            if md and (md.get("mint") or md.get("symbol")):
                stub = {
                    "symbol": md.get("symbol") or "?",
                    "name": md.get("name") or "",
                    "token_address": md.get("mint") or addr,
                    "chain": "solana",
                    "sector": "SOLANA",
                    "dex": "pumpfun",
                    "score": 45,
                    "confidence": 50,
                    "pressure": "NEUTRAL",
                    "verdict": "YODAO ONLY",
                    "whale": "-",
                    "flow": "NEUTRAL",
                    "liquidity_usd": float(md.get("liquidity") or 0),
                    "market_cap": float(md.get("market_cap") or 0),
                    "volume_24h": float(md.get("vol_24h") or 0),
                    "price_change_24h": 0,
                    "price_usd": md.get("token_price_usd"),
                    "age_hours": None,
                    "pair_address": md.get("pool_amm") or md.get("pool") or "",
                    "url": f"https://dexscreener.com/solana/{addr}",
                    "upside": "SPECULATIVE",
                    "upside_note": "hanya dari Yodao (Dex pair belum ketemu)",
                    "icon": md.get("image") or "",
                    "socials": [],
                    "websites": [],
                    "honeypot": False,
                    "risk": "UNKNOWN",
                    "flags": ["YODAO_ONLY"],
                    "buy_tax": 0,
                    "sell_tax": 0,
                    "sec_provider": "yodao",
                    "creator": md.get("creator") or "",
                    "top_holders": [],
                    "top10_pct": 0,
                    "top1_pct": 0,
                    "holder_count": int(md.get("holders") or 0),
                    "holder_note": "",
                    "source": "yodao",
                }
                rows = [stub]
            else:
                return {"error": "pair tidak ketemu di DexScreener / Yodao", "address": addr}
        else:
            # satu mint: kalau user tempel CA token, kunci ke base=CA
            base_hits = [p for p in pairs if ((p.get("baseToken") or {}).get("address") or "").lower() == want_addr]
            if base_hits:
                pairs = base_hits
            pair_hits = [p for p in pairs if (p.get("pairAddress") or "").lower() == want_addr]
            if pair_hits:
                pairs = pair_hits
            best = group_best_by_token(pairs)
            picked = list(best.values())
            if len(picked) > 1:
                picked = [sorted(picked, key=lambda p: num(p, "liquidity", "usd"), reverse=True)[0]]
            rows = [enrich(p, False) for p in picked]
            rows = attach_security(rows)
            remember_tokens(rows)
        for row in rows:
            chain_l = str(row.get("chain") or "").lower()
            pair = row.get("pair_address") or ""
            if chain_l and pair:
                row["traders_url"] = f"https://dexscreener.com/{chain_l}/{pair}"
            row["fomo_url"] = fomo_url(row)
            row["green_alert"] = is_green_signal(row)
            row["burn_onchain"] = check_evm_burn(row.get("chain") or "", row.get("token_address") or "")
            mech = detect_mechanics(row)
            row["lp_status"] = mech["lp_status"]
            row["burn_status"] = mech["burn_status"]
            row["buyback_status"] = mech.get("buyback_status") or row.get("buyback_status")
            row["mechanics_note"] = mech.get("note") or ""
            try:
                apply_gmgn_enrich(row)
            except Exception as ge:
                print("gmgn enrich error:", ge)
            try:
                apply_cmc_mechanics(row)
            except Exception as ce:
                print("cmc mechanics error:", ce)
            row["ca_report"] = ca_analysis(row)
            try:
                apply_yodao_enrich(row)
            except Exception as ye:
                print("yodao enrich ca error:", ye)
            try:
                apply_fomo_enrich(row)
            except Exception as fe:
                print("fomo enrich ca error:", fe)
        return rows
    except Exception as e:
        print("CA SCAN ERROR:", e)
        return {"error": str(e), "address": addr}


@app.get("/cart/add")
def cart_add(address: str = Query(..., min_length=8), chain: str = Query("")):
    rows = scan_ca(address=address, chain=chain)
    if isinstance(rows, dict) and rows.get("error"):
        return rows
    now = time.time()
    with CART_LOCK:
        cart = load_cart()
        for row in rows:
            addr = row.get("token_address") or address
            ch = (row.get("chain") or chain or "").lower()
            key = f"{ch}:{addr.lower()}"
            prev = cart.get(key) or {}
            px = _price_float(row.get("price_usd"))
            snaps = list(prev.get("snapshots") or [])
            if px:
                snaps.append({"ts": now, "price": px})
            snaps = snaps[-400:]
            cart[key] = {
                "token_address": addr,
                "chain": ch,
                "symbol": row.get("symbol"),
                "name": row.get("name"),
                "pair_address": row.get("pair_address"),
                "url": row.get("url"),
                "icon": row.get("icon"),
                "added_at": prev.get("added_at") or now,
                "first_price": prev.get("first_price") or px,
                "snapshots": snaps,
            }
        save_cart(cart)
    return {"ok": True, "count": len(load_cart()), "added": [r.get("symbol") for r in rows]}


@app.get("/cart/remove")
def cart_remove(address: str = Query(...), chain: str = Query("")):
    key = f"{chain.lower()}:{(address or '').lower()}"
    with CART_LOCK:
        cart = load_cart()
        cart.pop(key, None)
        # also try without chain match
        if key not in cart:
            for k in list(cart.keys()):
                if k.endswith(":" + address.lower()):
                    cart.pop(k, None)
        save_cart(cart)
    return {"ok": True, "count": len(cart)}


@app.get("/cart")
def cart_list():
    cart = load_cart()
    if not cart:
        return []
    addrs = []
    for rec in cart.values():
        if rec.get("token_address"):
            addrs.append(rec["token_address"])
    pairs = fetch_pairs_for_tokens(addrs) if addrs else []
    best = group_best_by_token(pairs)
    now = time.time()
    out = []
    with CART_LOCK:
        cart = load_cart()
        for key, rec in cart.items():
            p = best.get(key)
            if not p:
                # try any pair for this mint
                mint = (rec.get("token_address") or "").lower()
                for k2, pv in best.items():
                    if k2.endswith(":" + mint):
                        p = pv
                        break
            row = enrich(p, False) if p else {
                "symbol": rec.get("symbol"),
                "name": rec.get("name"),
                "token_address": rec.get("token_address"),
                "chain": (rec.get("chain") or "").upper(),
                "url": rec.get("url"),
                "icon": rec.get("icon"),
                "price_usd": None,
            }
            px = _price_float(row.get("price_usd"))
            snaps = list(rec.get("snapshots") or [])
            if px:
                snaps.append({"ts": now, "price": px})
                snaps = snaps[-400:]
                rec["snapshots"] = snaps
                rec["last_price"] = px
            first = _price_float(rec.get("first_price")) or (snaps[0].get("price") if snaps else None)
            since_add = None
            if first and px:
                since_add = round((px / float(first) - 1) * 100, 2)
            row["added_at"] = rec.get("added_at")
            row["watch_min"] = round((now - float(rec.get("added_at") or now)) / 60, 1)
            row["chg_since_add"] = since_add
            row["chg_5m"] = _chg_from_snaps(snaps, now, 5, px)
            if row.get("chg_5m") is None:
                row["chg_5m"] = row.get("price_change_m5")
            row["chg_15m"] = _chg_from_snaps(snaps, now, 15, px)
            row["chg_30m"] = _chg_from_snaps(snaps, now, 30, px)
            row["chg_1h"] = _chg_from_snaps(snaps, now, 60, px)
            if row.get("chg_1h") is None:
                row["chg_1h"] = row.get("price_change_h1")
            row["chg_24h"] = row.get("price_change_24h")
            out.append(row)
        save_cart(cart)
    return out


@app.get("/scan/watch")
def scan_watch(limit: int = 20):
    """Token yang pernah disimpan, termasuk yang sudah lewat 72 jam."""
    try:
        watch = load_watch()
        if not watch:
            return []
        pairs = group_best_by_token(fetch_pairs())
        results = []
        for key, rec in watch.items():
            p = pairs.get(key)
            if not p:
                results.append({
                    "symbol": rec.get("symbol"),
                    "chain": (rec.get("chain") or "").upper(),
                    "token_address": rec.get("token_address"),
                    "first_seen": rec.get("first_seen"),
                    "first_mcap": rec.get("first_mcap"),
                    "last_mcap": rec.get("last_mcap"),
                    "age_hours": rec.get("age_hours"),
                    "status": "MISSING_PAIR",
                    "url": rec.get("url") or "",
                })
                continue
            row = enrich(p, False)
            row["first_seen"] = rec.get("first_seen")
            row["first_mcap"] = rec.get("first_mcap")
            row["tracked_hours"] = round((time.time() - float(rec.get("first_seen") or time.time())) / 3600, 2)
            try:
                fm = float(rec.get("first_mcap") or 0)
                lm = float(row.get("market_cap") or 0)
                row["mcap_multiple"] = round(lm / fm, 2) if fm > 0 else None
            except (TypeError, ValueError, ZeroDivisionError):
                row["mcap_multiple"] = None
            results.append(row)
        live = [r for r in results if r.get("token_address") and r.get("pair_address")]
        live = attach_security(live[:limit])
        remember_tokens(live)
        missing = [r for r in results if r.get("status") == "MISSING_PAIR"]
        out = live + missing
        return sorted(out, key=lambda x: float(x.get("age_hours") or 0), reverse=True)[:limit]
    except Exception as e:
        print("WATCH ERROR:", e)
        return []


@app.get("/scan/narratives")
def scan_narratives():
    rows = scan_top(limit=20, mode="balanced")
    extra = []
    try:
        extra = [r for r in scan_watch(limit=20) if r.get("symbol")]
    except Exception:
        extra = []
    seen = set()
    merged = []
    for r in rows + extra:
        key = f"{r.get('chain')}:{(r.get('token_address') or '').lower()}"
        if key in seen:
            continue
        seen.add(key)
        if not r.get("narrative"):
            r["narrative"] = detect_narrative(r)
        merged.append(r)
    buckets: Dict[str, Dict] = {}
    for r in merged:
        label = r.get("narrative") or "UNLABELED"
        for part in [x.strip() for x in label.split("·")]:
            part = part.strip() or "UNLABELED"
            b = buckets.setdefault(part, {
                "narrative": part,
                "tokens": 0,
                "volume_24h": 0.0,
                "liquidity_usd": 0.0,
                "market_cap": 0.0,
                "symbols": [],
            })
            b["tokens"] += 1
            b["volume_24h"] += float(r.get("volume_24h") or 0)
            b["liquidity_usd"] += float(r.get("liquidity_usd") or 0)
            b["market_cap"] += float(r.get("market_cap") or 0)
            if r.get("symbol"):
                liq_r = float(r.get("liquidity_usd") or 0)
                vol_r = float(r.get("volume_24h") or 0)
                b["symbols"].append({
                    "symbol": r.get("symbol"),
                    "chain": r.get("chain"),
                    "volume_24h": vol_r,
                    "liquidity_usd": liq_r,
                    "vol_liq": round((vol_r / liq_r), 2) if liq_r else 0,
                    "market_cap": r.get("market_cap"),
                    "price_change_24h": r.get("price_change_24h"),
                    "upside": r.get("upside"),
                    "risk": r.get("risk"),
                    "url": r.get("url"),
                })
    out = sorted(buckets.values(), key=lambda x: x["volume_24h"], reverse=True)
    for b in out:
        vol = float(b["volume_24h"] or 0)
        liq = float(b["liquidity_usd"] or 0)
        vls = sorted(float(s.get("vol_liq") or 0) for s in b["symbols"])
        vl = vls[len(vls)//2] if vls else ((vol / liq) if liq else 0)
        chgs = sorted(float(s.get("price_change_24h") or 0) for s in b["symbols"])
        avg_chg = chgs[len(chgs)//2] if chgs else 0
        heat = min(100, int(min(vol / 80_000, 35) + min(vl * 6, 30) + min(max(avg_chg, 0) / 5, 20) + min(b["tokens"] * 3, 15)))
        if vl > 8 and avg_chg > 80:
            regime = "BLOW-OFF"
        elif vl >= 3 and avg_chg >= 20:
            regime = "HEATING"
        elif 0.4 <= vl <= 4 and 5 <= avg_chg <= 45:
            regime = "BUILDING"
        elif avg_chg < -10:
            regime = "COOLING"
        else:
            regime = "QUIET"
        b["volume_24h"] = round(vol, 2)
        b["liquidity_usd"] = round(liq, 2)
        b["market_cap"] = round(float(b["market_cap"] or 0), 2)
        b["vol_liq"] = round(vl, 2)
        b["avg_change_24h"] = round(avg_chg, 2)
        b["heat"] = heat
        b["regime"] = regime
        b["symbols"] = sorted(b["symbols"], key=lambda x: float(x.get("volume_24h") or 0), reverse=True)[:8]
    return sorted(out, key=lambda x: (-x.get("heat", 0), -x.get("volume_24h", 0)))


@app.get("/whales/wallets")
def whales_wallets():
    return list(load_wallets().values())


@app.get("/whales/add")
def whales_add(address: str = Query(..., min_length=8), chain: str = Query("solana"), note: str = Query("")):
    addr = address.strip()
    ch = (chain or "solana").lower()
    key = f"{ch}:{addr.lower()}"
    with WALLET_LOCK:
        data = load_wallets()
        prev = data.get(key) or {}
        data[key] = {
            "address": addr,
            "chain": ch,
            "note": note or prev.get("note") or "",
            "added_at": prev.get("added_at") or time.time(),
            "links": wallet_links(addr, ch),
        }
        save_wallets(data)
    return {"ok": True, "wallet": data[key], "count": len(data)}


@app.get("/whales/remove")
def whales_remove(address: str = Query(...), chain: str = Query("solana")):
    key = f"{chain.lower()}:{address.lower()}"
    with WALLET_LOCK:
        data = load_wallets()
        data.pop(key, None)
        save_wallets(data)
    return {"ok": True, "count": len(data)}


@app.get("/scan/whales")
def scan_whales():
    clusters = scan_narratives()
    leaders = []
    for b in clusters:
        for s in b.get("symbols") or []:
            chain = str(s.get("chain") or "").lower()
            url = s.get("url") or ""
            leaders.append({
                "symbol": s.get("symbol"),
                "chain": s.get("chain"),
                "narrative": b.get("narrative"),
                "regime": b.get("regime"),
                "heat": b.get("heat"),
                "volume_24h": s.get("volume_24h"),
                "market_cap": s.get("market_cap"),
                "price_change_24h": s.get("price_change_24h"),
                "url": url,
                "traders_url": url,
            })
    leaders = sorted(leaders, key=lambda x: float(x.get("volume_24h") or 0), reverse=True)[:12]
    return {
        "clusters": clusters,
        "leaders": leaders,
        "wallets": list(load_wallets().values()),
    }


@app.get("/ui")
def ui():
    path = os.path.join(BASE_DIR, "dashboard.html")
    if not os.path.exists(path):
        return {"error": "dashboard.html missing", "put_file_next_to": "main.py"}
    return FileResponse(path)


ALERT_MIN_SCORE = int(os.getenv("ALERT_MIN_SCORE", "70"))
ALERT_MIN_CONF = int(os.getenv("ALERT_MIN_CONF", "70"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL_SEC", "180"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DONATE_USDT = os.getenv("DONATE_USDT_ADDRESS", "").strip()
DONATE_NET = os.getenv("DONATE_USDT_NETWORK", "TON").strip() or "TON"
DONATE_WALLET_URL = os.getenv("DONATE_WALLET_URL", "https://t.me/wallet").strip()
SENT_ALERTS = {}
WORKER = {
    "running": False,
    "last_start": 0,
    "last_end": 0,
    "last_result": {},
    "last_error": "",
    "loop_alive": False,
}


def is_green_signal(row: Dict) -> bool:
    if row.get("honeypot") or row.get("risk") in ("HONEYPOT", "HIGH"):
        return False
    flags = [str(f).upper() for f in (row.get("flags") or [])]
    if any(x in " ".join(flags) for x in (
        "HONEYPOT", "CANNOT_SELL", "CANNOT_BUY", "BLACKLIST",
        "INSIDER_CLUSTER", "INSIDER_NETWORK", "CREATOR_RUGGED", "CONCENTRATED_HOLDERS",
    )):
        return False
    if row.get("risk") not in ("LOW", None, ""):
        return False
    if int(row.get("score") or 0) < ALERT_MIN_SCORE:
        return False
    if int(row.get("confidence") or 0) < ALERT_MIN_CONF:
        return False
    upside = str(row.get("upside") or "").upper()
    if upside not in {"HIGH ROOM", "SPECULATIVE"}:
        return False

    liq = float(row.get("liquidity_usd") or 0)
    mcap = float(row.get("market_cap") or 0)
    vol = float(row.get("volume_24h") or 0)
    change = float(row.get("price_change_24h") or 0)
    age = row.get("age_hours")
    tax = max(float(row.get("sell_tax") or 0), float(row.get("buy_tax") or 0))
    top10 = float(row.get("top10_pct") or 0)

    if tax >= 10:
        return False
    if liq < 20_000 or liq > 400_000:
        return False
    if mcap < 25_000 or mcap > 800_000:
        return False
    if vol <= 0 or liq <= 0:
        return False
    ratio = vol / liq
    if ratio < 0.3 or ratio > 4:
        return False
    if change < 5 or change > 45:
        return False
    if age is not None and (age < 2 or age > 72):
        return False
    if top10 >= 40:
        return False
    # WAJIB: ada smart wallet FOMO
    if FOMO_REQUIRE_SMART and not has_fomo_smart_wallet(row):
        return False
    # hindari token yang dev-nya punya jejak rug / serial deployer (GMGN)
    if is_dev_rug_risk(row):
        return False
    return True


def donate_text() -> str:
    addr = DONATE_USDT or "(set DONATE_USDT_ADDRESS di Render)"
    return (
        "☕ <b>Donasi USDT</b>\n"
        f"Jaringan: <b>{DONATE_NET}</b>\n"
        "Kirim ke dompet Telegram / address ini:\n"
        f"<code>{addr}</code>\n"
        "Salin address, buka Wallet Telegram, kirim USDT."
    )


def menu_buttons() -> dict:
    return {"inline_keyboard": [
        [{"text": "🔍 Scan kandidat", "callback_data": "scan"}],
        [{"text": "🚀 Yodao Pump", "callback_data": "yodao"}, {"text": "🔥 FOMO Board", "callback_data": "fomo"}],
        [{"text": "📊 Statistik signal", "callback_data": "stats"}],
        [{"text": "☕ Donasi USDT", "callback_data": "donasi"}],
    ]}


def send_fomo_candidates(chat_id: str) -> None:
    if not FOMO_API_KEY:
        send_telegram(
            "🔥 <b>FOMO API</b>\nSet env <code>FOMO_API_KEY</code> di Render.\n"
            "Ambil gratis: https://fomoapi.io/dashboard",
            buttons=menu_buttons(),
            chat_id=chat_id,
        )
        return
    send_telegram("🔥 Scan FOMO board...", chat_id=chat_id)
    try:
        rows = fetch_fomo_boards(limit=10)
    except Exception as e:
        send_telegram(f"FOMO gagal: {e}", buttons=menu_buttons(), chat_id=chat_id)
        return
    if not rows:
        send_telegram(
            "🔥 Board kosong / key credits habis / API error.",
            buttons=menu_buttons(),
            chat_id=chat_id,
        )
        return
    lines = [f"🔥 <b>FOMO Board</b> · {len(rows)} token", "Sumber: api.fomoapi.io · smart money social"]
    kb = []
    for i, row in enumerate(rows, 1):
        addr = row.get("token_address") or ""
        chg = float(row.get("price_change_24h") or 0)
        lines.append("")
        lines.append(
            f"<b>{i}. ${row.get('symbol')}</b> · {row.get('fomo_board')} #{row.get('fomo_rank') or '-'} · "
            f"{str(row.get('chain') or '').upper()}"
        )
        lines.append(f"MCap {_usd(row.get('market_cap'))} · 24h {chg:+.1f}%")
        lines.append(f"<code>{addr}</code>")
        if addr:
            kb.append([{"text": f"🔬 Analisa ${row.get('symbol') or i}", "callback_data": "ca:" + addr[:60]}])
    kb.append([{"text": "🔥 FOMO lagi", "callback_data": "fomo"}])
    send_telegram("\n".join(lines), buttons={"inline_keyboard": kb}, chat_id=chat_id)


def send_yodao_candidates(chat_id: str) -> None:
    send_telegram("🚀 Scan Yodao Pump.fun...", chat_id=chat_id)
    try:
        rows = scan_yodao(limit=8, stage="all")
    except Exception as e:
        send_telegram(f"Yodao gagal: {e}", buttons=menu_buttons(), chat_id=chat_id)
        return
    if not rows:
        send_telegram(
            "🚀 <b>Yodao kosong</b>\nTidak ada pool lolos filter, atau API tidak merespon.",
            buttons=menu_buttons(),
            chat_id=chat_id,
        )
        return
    lines = [f"🚀 <b>Yodao Pump</b> · {len(rows)} kandidat", "Sumber: api2.yodao.io · Solana only"]
    kb = []
    for i, row in enumerate(rows, 1):
        addr = row.get("token_address") or ""
        stage = row.get("yodao_stage") or "-"
        lines.append("")
        lines.append(
            f"<b>{i}. ${row.get('symbol')}</b> · {stage} · complete {row.get('yodao_pct_completion') or 0:.0f}%"
        )
        lines.append(
            f"MCap {_usd(row.get('market_cap'))} · Liq {_usd(row.get('liquidity_usd'))} · "
            f"n={row.get('holder_count') or '-'}"
        )
        lines.append(
            f"Dev {row.get('yodao_dev_holding') or 0:.0f}% · Sniper {row.get('yodao_snipers') or 0:.0f}% · "
            f"Top10 {row.get('top10_pct') or 0:.0f}%"
        )
        lines.append(f"<code>{addr}</code>")
        if addr:
            kb.append([{"text": f"🔬 Analisa ${row.get('symbol') or i}", "callback_data": "ca:" + addr[:60]}])
    kb.append([{"text": "🚀 Yodao lagi", "callback_data": "yodao"}])
    kb.append([{"text": "🔍 Scan Dex", "callback_data": "scan"}])
    send_telegram("\n".join(lines), buttons={"inline_keyboard": kb}, chat_id=chat_id)


def donate_buttons() -> dict:
    return {"inline_keyboard": [
        [{"text": "👛 Buka Wallet Telegram", "url": DONATE_WALLET_URL or "https://t.me/wallet"}],
        [{"text": "🔍 Scan kandidat", "callback_data": "scan"}],
    ]}


def send_scan_candidates(chat_id: str) -> None:
    send_telegram("🔍 Scan filter early...", chat_id=chat_id)
    try:
        rows = scan_top(limit=8, mode="aggressive")
    except Exception as e:
        send_telegram(f"Scan gagal: {e}", buttons=menu_buttons(), chat_id=chat_id)
        return
    if not rows:
        send_telegram(
            "🔍 <b>Scan kosong</b>\nTidak ada kandidat lolos filter early sekarang.\nCoba lagi beberapa menit.",
            buttons=menu_buttons(),
            chat_id=chat_id,
        )
        return
    lines = [f"🔍 <b>Kandidat early</b> · {len(rows)} token", "Lolos umur/mcap/liq/vol. Tap analisa."]
    kb = []
    for i, row in enumerate(rows, 1):
        addr = row.get("token_address") or ""
        chg = float(row.get("price_change_24h") or 0)
        lines.append("")
        lines.append(f"<b>{i}. ${row.get('symbol') or '-'}</b> · {str(row.get('chain') or '').upper()}")
        lines.append(f"Liq {_usd(row.get('liquidity_usd'))} · MCap {_usd(row.get('market_cap'))} · 24h {chg:+.1f}%")
        lines.append(f"<code>{addr}</code>")
        if addr:
            kb.append([{"text": f"🔬 Analisa ${row.get('symbol') or i}", "callback_data": "ca:" + addr[:60]}])
    kb.append([{"text": "🔍 Scan lagi", "callback_data": "scan"}])
    kb.append([{"text": "☕ Donasi USDT", "callback_data": "donasi"}])
    send_telegram("\n".join(lines), buttons={"inline_keyboard": kb}, chat_id=chat_id)


def tg_buttons(row: Dict) -> dict:
    buttons = []
    fomo = fomo_url(row)
    dex = dex_url(row)
    row_btns = []
    if fomo:
        row_btns.append({"text": "⚡️ Trade FOMO", "url": fomo})
    if dex:
        row_btns.append({"text": "📉 DexScreener", "url": dex})
    if row_btns:
        buttons.append(row_btns)
    buttons.append([
        {"text": "🔍 Scan", "callback_data": "scan"},
        {"text": "☕ Donasi USDT", "callback_data": "donasi"},
    ])
    return {"inline_keyboard": buttons}


def send_telegram(text: str, parse_mode: str = "HTML", buttons: Optional[dict] = None, chat_id: Optional[str] = None) -> bool:
    dest = str(chat_id or TG_CHAT or "").strip()
    if not TG_TOKEN or not dest:
        print("telegram skip: token/chat_id kosong")
        return False
    payload = {
        "chat_id": dest,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = buttons
    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print("telegram error:", e)
        return False


def send_telegram_photo(photo_url: str, caption: str, buttons: Optional[dict] = None, chat_id: Optional[str] = None) -> bool:
    dest = str(chat_id or TG_CHAT or "").strip()
    if not TG_TOKEN or not dest:
        return False
    payload = {
        "chat_id": dest,
        "photo": photo_url,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }
    if buttons:
        payload["reply_markup"] = buttons
    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            json=payload,
            timeout=25,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print("telegram photo error:", e)
        return False


CA_EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
CA_SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TG_OFFSET_FILE = os.path.join(DATA_DIR, "tg_offset.json")
TG_OFFSET = {"id": 0}


def extract_ca(text: str) -> str:
    raw = (text or "").strip()
    m = CA_EVM_RE.search(raw)
    if m:
        return m.group(0)
    for m in CA_SOL_RE.finditer(raw):
        tok = m.group(0)
        if tok.lower() in ("http", "https"):
            continue
        if any(x in raw.lower() for x in ("dexscreener", "t.me", "http")) and tok.startswith("http"):
            continue
        if len(tok) >= 32:
            return tok
    return ""


def _tape_label(row: Dict) -> Tuple[str, str]:
    b5 = float(row.get("tx_buys_m5") or 0)
    s5 = float(row.get("tx_sells_m5") or 0)
    t = b5 + s5
    if t <= 0:
        return "NEUTRAL", "➖"
    p = b5 / t
    chg5 = float(row.get("price_change_m5") or 0)
    if p >= 0.68 and chg5 >= 0:
        return "BUY STRENGTH", "🟢"
    if p >= 0.58:
        return "BUY BIAS", "🟢"
    if p <= 0.32 and chg5 <= 0:
        return "SELL STRENGTH", "🔴"
    if p <= 0.42:
        return "SELL BIAS", "🔴"
    return "NEUTRAL", "⚪"


def holder_block(row: Dict) -> Tuple[list, bool, float]:
    """Blok holder untuk Telegram. Return (lines, dominan, top1_whale)."""
    top1 = float(row.get("top1_pct") or 0)
    top1_raw = float(row.get("top1_raw_pct") or 0)
    top10 = float(row.get("top10_pct") or 0)
    lp_h = float(row.get("lp_holder_pct") or 0)
    n = row.get("holder_count") or "-"
    note = row.get("holder_note") or ""
    holders = row.get("top_holders") or []
    dominan = top1 >= 10
    lines = ["──────────────", "👥 <b>HOLDERS</b>"]
    if top1 <= 0 and top10 <= 0 and top1_raw <= 0 and not holders:
        lines.append("⚪ Data holder kosong")
        return lines, False, 0.0
    if dominan:
        lines.append(f"🚨 Whale Top1 <b>{top1:.1f}%</b> ≥10% — risiko dump")
    else:
        lines.append(f"✅ Whale Top1 {top1:.1f}% · Top10 {top10:.1f}% · n={n}")
    if lp_h > 0 or (top1_raw >= 10 and top1_raw != top1):
        lines.append(f"🏦 LP di list ~{lp_h or top1_raw:.1f}% (bukan whale)")
    if note and len(str(note)) < 120:
        lines.append(f"<i>{note}</i>")
    elif note:
        lines.append(f"<i>{str(note)[:100]}…</i>")
    shown = 0
    for h in holders[:8]:
        if not isinstance(h, dict):
            continue
        pct = float(h.get("pct") or 0)
        is_lp = bool(h.get("is_lp")) or _is_lp_holder(str(h.get("label") or ""), str(h.get("address") or ""))
        addr = str(h.get("address") or "")
        short = (addr[:4] + "…" + addr[-4:]) if len(addr) > 10 else addr
        if is_lp:
            lines.append(f"• {pct:.1f}% <code>{short}</code> 🏦 LP")
        else:
            flag = " 🚨" if pct >= 10 else ""
            lines.append(f"• {pct:.1f}% <code>{short}</code>{flag}")
            shown += 1
        if shown >= 4:
            break
    return lines, dominan, top1


def analisa_id(row: Dict) -> str:
    """Pesan Telegram analisa CA — format rapi, padat."""
    rpt = row.get("ca_report") or {}
    tw = str(rpt.get("trend_window") or "WATCH")
    health = rpt.get("health_score")
    aman = rpt.get("safety_score")
    early = is_early_setup(row, "balanced")
    tape, tape_ico = _tape_label(row)
    honey = bool(row.get("honeypot"))
    risk = str(row.get("risk") or "-")
    upside = str(row.get("upside") or "-")
    h_lines, dominan, top1 = holder_block(row)

    if honey or risk in ("HONEYPOT",) or row.get("gmgn_avoid"):
        sig, sig_ico, putusan = "JANGAN BELI", "🛑", row.get("gmgn_avoid_reason") or "Honeypot / dev rug risk"
        stars_n = 1
    elif risk == "HIGH" or tw == "AVOID":
        sig, sig_ico, putusan = "JANGAN BELI", "🛑", "Risiko tinggi / window mati"
        stars_n = 1
    elif dominan and top1 >= 20:
        sig, sig_ico, putusan = "JANGAN BELI", "🚨", f"Holder dominan {top1:.1f}%"
        stars_n = 1
    elif not early or tw == "UNLIKELY" or upside in ("WASHY", "THIN", "NO UPSIDE"):
        sig, sig_ico, putusan = "JANGAN BELI", "🚫", "Bukan setup early"
        stars_n = 2
    elif dominan:
        sig, sig_ico, putusan = "JANGAN KEJAR", "🚨", f"Top1 {top1:.1f}% ≥10%"
        stars_n = 2
    elif tw == "POSSIBLE" and early and tape.startswith("BUY"):
        sig, sig_ico, putusan = "BELI SPEKULATIF", "🟢", "Early + tape beli · size kecil"
        stars_n = 5 if (aman or 0) >= 70 and (health or 0) >= 75 else 4
    elif tw == "POSSIBLE" and early:
        sig, sig_ico, putusan = "PANTAU DULU", "🟡", "Early oke, tape belum jelas"
        stars_n = 3
    else:
        sig, sig_ico, putusan = "JANGAN KEJAR", "⚠️", "Belum cukup untuk masuk"
        stars_n = 2

    stars = "★" * stars_n + "☆" * (5 - stars_n)
    chg = float(row.get("price_change_24h") or 0)
    chg_ico = "📈" if chg >= 0 else "📉"
    risk_ico = "🟢" if risk == "LOW" else ("🔴" if risk in ("HIGH", "HONEYPOT") else "⚪")
    win_ico = {"POSSIBLE": "🟢", "WATCH": "🟡", "UNLIKELY": "🔴", "AVOID": "🛑"}.get(tw, "⚪")
    name = row.get("name") or ""
    age = row.get("age_hours")
    age_s = f"{float(age):.1f}j" if age is not None else "-"
    vl = rpt.get("vol_liq")
    try:
        vl_s = f"{float(vl):.1f}x" if vl not in (None, "") else "-"
    except (TypeError, ValueError):
        vl_s = "-"

    # LP / burn satu baris tanpa dobel
    lp_s = str(row.get("lp_status") or "")
    burn_s = str(row.get("burn_status") or "")
    buy_s = str(row.get("buyback_status") or "")
    mech_bits = []
    if lp_s and lp_s not in ("LP UNKNOWN", "LP ?"):
        mech_bits.append(f"🔒 {lp_s}")
    if burn_s and burn_s not in ("BURN UNKNOWN", "Burn ?") and "LP/TOKEN BURN" not in lp_s:
        # jangan ulangi kalau LP sudah bilang BURN
        if "BURN" in burn_s.upper() and "BURN" in lp_s.upper():
            pass
        else:
            mech_bits.append(f"🔥 {burn_s}")
    elif burn_s and "BURN" in burn_s.upper() and "BURN" not in lp_s.upper():
        mech_bits.append(f"🔥 {burn_s}")
    if buy_s and "UNKNOWN" not in buy_s.upper():
        mech_bits.append(f"♻️ {buy_s}")
    mech_line = " · ".join(mech_bits) if mech_bits else "🔒 LP/Burn belum jelas"

    lines = [
        f"{sig_ico} <b>{sig}</b>  {stars} <b>{stars_n}/5</b>",
        f"<i>{putusan}</i>",
        "",
        f"💎 <b>${row.get('symbol') or '-'}</b>" + (f" · {name}" if name else ""),
        f"⛓️ {str(row.get('chain') or '').upper()} · {row.get('dex') or '-'}",
        "──────────────",
        f"{win_ico} <b>{tw}</b>  ·  {tape_ico} <b>{tape}</b>  ·  {risk_ico} <b>{risk}</b>  ·  🎯 <b>{upside}</b>",
        f"{'🍯 HONEYPOT' if honey else '✅ Bukan honeypot'} · Skor tape {health if health is not None else '-'} · aman {aman if aman is not None else '-'}",
        "──────────────",
        f"💧 {_usd(row.get('liquidity_usd'))} · 🏦 {_usd(row.get('market_cap'))} · 📦 {_usd(row.get('volume_24h'))}",
        f"{chg_ico} 24h {chg:+.1f}% · ⏱ {age_s} · vol/liq {vl_s}",
        mech_line,
    ]
    lines.extend(h_lines)

    # sumber ringkas
    lines.append("──────────────")
    if row.get("gmgn_ok"):
        cstat = row.get("gmgn_creator_status") or "-"
        gv = row.get("gmgn_verdict") or "GMGN"
        gs = row.get("gmgn_score")
        lines.append(f"🦞 <b>GMGN</b>  {gv}" + (f" · skor <b>{gs}</b>/100" if gs is not None else ""))
        lines.append(
            f"   Smart {row.get('gmgn_smart') or 0} · KOL {row.get('gmgn_renowned') or 0} · "
            f"Sniper {row.get('gmgn_sniper') or 0} · Bundler {row.get('gmgn_bundler') or 0} · "
            f"Fresh {row.get('gmgn_fresh') or 0}"
        )
        chg = row.get("gmgn_chg") or {}
        chg_bits = []
        for k, lab in (("m5", "5m"), ("h1", "1h"), ("h6", "6h"), ("h24", "24h")):
            v = chg.get(k)
            if v is not None:
                chg_bits.append(f"{lab} {v:+.1f}%")
        if chg_bits:
            lines.append("   Δ " + " · ".join(chg_bits))
        buys, sells = row.get("gmgn_buys_24h"), row.get("gmgn_sells_24h")
        if buys or sells:
            lines.append(f"   Tx 24h buy {buys or 0} / sell {sells or 0}")
        lines.append(
            f"   Dev <b>{cstat}</b> · hold {row.get('gmgn_creator_hold_pct') or 0}% · "
            f"created {row.get('gmgn_creator_created_count') or 0}"
            + (f" · {row.get('gmgn_launchpad')}" if row.get("gmgn_launchpad") else "")
        )
        ren_m, ren_f = row.get("gmgn_renounced_mint"), row.get("gmgn_renounced_freeze")
        ren_s = []
        if ren_m is True:
            ren_s.append("mint✅")
        elif ren_m is False:
            ren_s.append("mint❌")
        if ren_f is True:
            ren_s.append("freeze✅")
        elif ren_f is False:
            ren_s.append("freeze❌")
        if ren_s:
            lines.append("   " + " · ".join(ren_s))
        if row.get("gmgn_notes"):
            lines.append("   " + " · ".join(str(x) for x in (row.get("gmgn_notes") or [])[:5]))
        if row.get("gmgn_avoid"):
            lines.append(f"🚨 <b>HINDARI</b> {row.get('gmgn_avoid_reason') or 'dev rug'}")
        elif cstat == "creator_close":
            lines.append("✅ Dev sudah close alokasi")
        if row.get("gmgn_creator"):
            lines.append(f"   <code>{row.get('gmgn_creator')}</code>")
    elif row.get("gmgn_error"):
        lines.append(f"🦞 GMGN  ⚠️ {_esc(str(row.get('gmgn_error'))[:80])}")
    else:
        lines.append("🦞 GMGN  — (chain tidak support / kosong)")

    if row.get("yodao_ok") or row.get("yodao_dev_holding") is not None:
        lines.append(
            f"🚀 Yodao  bond {float(row.get('yodao_pct_completion') or 0):.0f}% · "
            f"dev {float(row.get('yodao_dev_holding') or 0):.1f}% · "
            f"sniper {float(row.get('yodao_snipers') or 0):.1f}% · "
            f"fresh {float(row.get('yodao_fresh') or 0):.1f}%"
        )
    sm = row.get("fomo_smart_holders") or []
    if sm or row.get("fomo_board"):
        board = f" · {row.get('fomo_board')}#{row.get('fomo_rank')}" if row.get("fomo_board") else ""
        lines.append(f"🔥 FOMO  {row.get('fomo_smart_count') or len(sm)} smart{board}")
        for h in sm[:3]:
            handle = h.get("handle") or "?"
            v = h.get("valueUsd")
            lines.append(f"   @{handle}" + (f" · {_usd(v)}" if v not in (None, "") else ""))

    lines.append("──────────────")
    lines.append(f"<code>{row.get('token_address') or '-'}</code>")
    foot = []
    if row.get("url"):
        foot.append(f'<a href="{row["url"]}">DexScreener</a>')
    if row.get("fomo_url"):
        foot.append(f'<a href="{row["fomo_url"]}">FOMO</a>')
    if foot:
        lines.append(" · ".join(foot))
    lines.append("<i>⚠️ Bukan saran finansial · DYOR</i>")
    return "\n".join(lines)


# anti-double kirim analisa CA (webhook + poll / photo+text)
_CA_SENT_LOCK = threading.Lock()
_CA_SENT: Dict[str, float] = {}


def handle_ca_message(text: str, chat_id: str) -> Dict[str, Any]:
    addr = extract_ca(text)
    if not addr:
        send_telegram(
            "Kirim <b>CA / mint penuh</b>.\nContoh EVM 0x + 40 hex, atau mint Solana 32–44 karakter.",
            chat_id=chat_id,
        )
        return {"ok": False, "reason": "no_ca"}
    dedupe_key = f"{chat_id}:{addr.lower()}"
    now = time.time()
    with _CA_SENT_LOCK:
        last = _CA_SENT.get(dedupe_key, 0)
        if now - last < 45:
            # sudah dikirim baru saja — skip double
            return {"ok": True, "symbol": "", "address": addr, "deduped": True}
        _CA_SENT[dedupe_key] = now
    send_telegram(f"⏳ Cek CA <code>{addr}</code> ...", chat_id=chat_id)
    rows = scan_ca(address=addr, chain="")
    if isinstance(rows, dict) and rows.get("error"):
        send_telegram(f"Gagal: {rows.get('error')}\n<code>{addr}</code>", chat_id=chat_id)
        return rows
    if not rows:
        send_telegram("Pair tidak ketemu di DexScreener.", chat_id=chat_id)
        return {"ok": False}
    row = rows[0]
    caption = analisa_id(row)
    icon = row.get("icon") or ""
    # Hanya KIRIM SEKALI: caption panjang → teks saja (bukan photo + teks)
    # Photo Telegram max caption 1024; analisa kita biasanya >1024
    if icon and len(caption) <= 1000:
        ok = send_telegram_photo(icon, caption, buttons=tg_buttons(row), chat_id=chat_id)
        if not ok:
            send_telegram(caption, buttons=tg_buttons(row), chat_id=chat_id)
    else:
        send_telegram(caption, buttons=tg_buttons(row), chat_id=chat_id)
    return {"ok": True, "symbol": row.get("symbol"), "address": addr}


def process_tg_update(upd: Dict) -> None:
    cb = upd.get("callback_query") or {}
    if cb:
        chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
        data = str(cb.get("data") or "")
        if not chat:
            return
        if data == "donasi":
            send_telegram(donate_text(), buttons=donate_buttons(), chat_id=str(chat))
        elif data == "scan":
            send_scan_candidates(str(chat))
        elif data == "yodao":
            send_yodao_candidates(str(chat))
        elif data == "fomo":
            send_fomo_candidates(str(chat))
        elif data == "stats":
            stats = refresh_signal_stats()
            send_telegram(format_stats_msg(stats), buttons=menu_buttons(), chat_id=str(chat))
        elif data.startswith("ca:"):
            handle_ca_message(data[3:], str(chat))
        return
    msg = upd.get("message") or upd.get("edited_message") or {}
    text = msg.get("text") or msg.get("caption") or ""
    chat = (msg.get("chat") or {}).get("id")
    if not text or not chat:
        return
    if text.startswith("/start"):
        send_telegram(
            "Tempel <b>CA</b> atau tekan <b>Scan</b>.\n/scan — kandidat lolos filter\n/donasi — USDT",
            buttons=menu_buttons(),
            chat_id=str(chat),
        )
        return
    if text.startswith("/scan") or text.lower() == "scan":
        send_scan_candidates(str(chat))
        return
    if text.startswith("/yodao") or text.lower() in ("yodao", "pump"):
        send_yodao_candidates(str(chat))
        return
    if text.startswith("/fomo") or text.lower() == "fomo":
        send_fomo_candidates(str(chat))
        return
    if text.startswith("/stats") or text.lower() in ("stats", "statistik", "winrate"):
        stats = refresh_signal_stats()
        send_telegram(format_stats_msg(stats), buttons=menu_buttons(), chat_id=str(chat))
        return
    if text.startswith("/donasi") or text.lower() in ("donasi", "donate"):
        send_telegram(donate_text(), buttons=donate_buttons(), chat_id=str(chat))
        return
    if extract_ca(text) or text.startswith("/ca") or text.startswith("/analisa"):
        handle_ca_message(text.replace("/ca", "").replace("/analisa", ""), str(chat))


@app.post("/telegram/hook")
async def telegram_hook(request: Request):
    secret = os.getenv("TELEGRAM_HOOK_SECRET", "")
    if secret and request.query_params.get("secret") != secret:
        return {"ok": False, "error": "forbidden"}
    try:
        upd = await request.json()
    except Exception:
        return {"ok": False}
    try:
        process_tg_update(upd)
    except Exception as e:
        print("tg hook error:", e)
    return {"ok": True}


def poll_telegram():
    if not TG_TOKEN:
        return
    try:
        if os.path.exists(TG_OFFSET_FILE):
            TG_OFFSET["id"] = int(json.load(open(TG_OFFSET_FILE)).get("id") or 0)
    except Exception:
        pass
    while True:
        try:
            r = SESSION.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"timeout": 25, "offset": TG_OFFSET["id"] + 1},
                timeout=35,
            )
            data = r.json() if r.ok else {}
            for upd in data.get("result") or []:
                TG_OFFSET["id"] = max(TG_OFFSET["id"], int(upd.get("update_id") or 0))
                process_tg_update(upd)
            try:
                with open(TG_OFFSET_FILE, "w") as f:
                    json.dump({"id": TG_OFFSET["id"]}, f)
            except Exception:
                pass
        except Exception as e:
            print("tg poll error:", e)
            time.sleep(5)


def _usd(n) -> str:
    try:
        x = float(n or 0)
    except (TypeError, ValueError):
        return "-"
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.1f}K"
    return f"${x:,.0f}"


FOMO_CHAIN = {
    "solana": "solana",
    "ethereum": "ethereum",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "avalanche": "avalanche",
    "robinhood": "robinhood",
    "arc": "arc",
}


def fomo_url(row: Dict) -> str:
    addr = (row.get("token_address") or "").strip()
    if not addr:
        return ""
    slug = FOMO_CHAIN.get(str(row.get("chain") or "").lower(), "solana")
    return f"https://fomo.family/tokens/{slug}/{addr}"


def tg_buttons(row: Dict) -> dict:
    buttons = []
    row_btns = []
    fomo = fomo_url(row)
    dex = dex_url(row)
    if fomo:
        row_btns.append({"text": "Trade FOMO", "url": fomo})
    if dex:
        row_btns.append({"text": "DexScreener", "url": dex})
    if row_btns:
        buttons.append(row_btns)
    return {"inline_keyboard": buttons} if buttons else {}


def dex_url(row: Dict) -> str:
    if row.get("url"):
        return row["url"]
    chain = str(row.get("chain") or "").lower()
    pair = row.get("pair_address") or ""
    if chain and pair:
        return f"https://dexscreener.com/{chain}/{pair}"
    return ""


def _esc(s) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _social_label(kind: str) -> str:
    k = (kind or "").lower()
    mapping = {
        "twitter": "X",
        "x": "X",
        "telegram": "Telegram",
        "discord": "Discord",
        "website": "Web",
        "web": "Web",
    }
    return mapping.get(k, kind[:12].capitalize() if kind else "Link")


def format_alert(row: Dict) -> str:
    symbol = _esc(row.get("symbol") or "?")
    name = _esc(row.get("name") or "")
    chain = _esc(row.get("chain") or "-")
    dex = _esc(row.get("dex") or "-")
    addr = _esc(row.get("token_address") or "-")
    link = dex_url(row)
    change = row.get("price_change_24h")
    try:
        ch = f"{float(change):+.1f}%"
    except (TypeError, ValueError):
        ch = "-"

    links = []
    seen = set()
    for s in (row.get("socials") or []):
        url = (s.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(f'<a href="{_esc(url)}">{_esc(_social_label(s.get("type")))}</a>')
    for w in (row.get("websites") or []):
        url = str(w or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(f'<a href="{_esc(url)}">Web</a>')
    social = " · ".join(links[:5]) if links else "—"

    header = f"<b>${symbol}</b>"
    if name:
        header += f"  <i>{name}</i>"

    chart = f'<a href="{_esc(link)}">DexScreener</a>' if link else "—"
    body = (
        f"🟢 <b>GREEN GEM</b> · BELI SPEKULATIF  ⭐⭐⭐⭐☆\n"
        f"{header}\n"
        f"{chain} · {dex}\n"
        f"──────────────\n"
        f"Score <b>{row.get('score')}</b> · Conf <b>{row.get('confidence')}</b>\n"
        f"Risk <b>{_esc(row.get('risk'))}</b> · Upside <b>{_esc(row.get('upside'))}</b>\n"
        f"🔒 {_esc(row.get('lp_status') or '-')} · 🔥 {_esc(row.get('burn_status') or '-')}\n"
        f"──────────────\n"
        f"💧 {_usd(row.get('liquidity_usd'))} · 🏦 {_usd(row.get('market_cap'))} · 📦 {_usd(row.get('volume_24h'))}\n"
        f"📈 24h {ch}\n"
        f"──────────────\n"
        f"<code>{addr}</code>\n"
    )
    creator = _esc(row.get("creator") or "")
    if creator:
        body += f"Dev  <code>{creator}</code>\n"
        body += f"Dev tx  https://solscan.io/account/{creator}\n"
    top1 = float(row.get("top1_pct") or 0)
    top10 = float(row.get("top10_pct") or 0)
    note = _esc(row.get("holder_note") or "")
    body += "━━━━━━━━━━━━━━\n👥 HOLDERS\n"
    if top1 >= 10:
        body += f"🚨 PERINGATAN holder dominan Top1 <b>{top1:.1f}%</b> (≥10%)\n"
    elif top1 > 0:
        body += f"✅ Top1 {top1:.1f}% · tidak dominan\n"
    else:
        body += "⚪ Data holder kosong\n"
    body += f"Top10 {top10:.1f}% · n={row.get('holder_count') or '-'}\n"
    if note:
        body += f"{note}\n"
    for h in (row.get("top_holders") or [])[:4]:
        if not isinstance(h, dict):
            continue
        pct = float(h.get("pct") or 0)
        label = str(h.get("label") or "").lower()
        if any(x in label for x in ("pool", "raydium", "lp", "pump", "pair")):
            continue
        addr = str(h.get("address") or "")
        short = (addr[:4] + "…" + addr[-4:]) if len(addr) > 10 else addr
        mark = " 🚨" if pct >= 10 else ""
        body += f"• {pct:.1f}% <code>{_esc(short)}</code>{mark}\n"
    if row.get("lp_locked") is not None:
        body += f"LP lock  {row.get('lp_locked')} {row.get('lp_locked_pct') or ''}%\n"
    fomo = fomo_url(row)
    body += f"Social  {social}\n"
    body += f"Chart   {chart}\n"
    if fomo:
        body += f'Trade   <a href="{_esc(fomo)}">Open FOMO</a>'
    return body


def load_signals() -> List[Dict]:
    if not os.path.exists(SIGNAL_FILE):
        return []
    try:
        with open(SIGNAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print("signal load error:", e)
        return []


def save_signals(rows: List[Dict]) -> None:
    tmp = SIGNAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows[-500:], f, ensure_ascii=False)
    os.replace(tmp, SIGNAL_FILE)


def record_signal(row: Dict, source: str = "alert") -> None:
    px = _price_float(row.get("price_usd"))
    addr = (row.get("token_address") or "").strip()
    if not addr:
        return
    key = f"{str(row.get('chain') or '').lower()}:{addr.lower()}"
    with SIGNAL_LOCK:
        rows = load_signals()
        for r in rows:
            if r.get("key") == key and (time.time() - float(r.get("ts") or 0)) < 6 * 3600:
                return
        rows.append({
            "key": key,
            "ts": time.time(),
            "source": source,
            "symbol": row.get("symbol") or "?",
            "name": row.get("name") or "",
            "chain": str(row.get("chain") or "").lower(),
            "token_address": addr,
            "pair_address": row.get("pair_address") or "",
            "entry_price": px,
            "entry_mcap": float(row.get("market_cap") or 0),
            "last_price": px,
            "last_mcap": float(row.get("market_cap") or 0),
            "pnl_pct": 0.0,
            "status": "OPEN",
            "url": row.get("url") or "",
        })
        save_signals(rows)


def refresh_signal_stats() -> Dict:
    with SIGNAL_LOCK:
        rows = load_signals()
    if not rows:
        return {"total": 0, "open": 0, "win": 0, "loss": 0, "flat": 0, "winrate": 0, "avg_pnl": 0, "items": []}
    addrs = list({r.get("token_address") for r in rows if r.get("token_address")})
    price_map: Dict[str, float] = {}
    mcap_map: Dict[str, float] = {}
    try:
        pairs = fetch_pairs_for_tokens(addrs[:40])
        for p in pairs:
            base = ((p.get("baseToken") or {}).get("address") or "").lower()
            if not base:
                continue
            px = _price_float((p.get("priceUsd")))
            mc = num(p, "marketCap") or num(p, "fdv")
            if px and base not in price_map:
                price_map[base] = px
            if mc and base not in mcap_map:
                mcap_map[base] = float(mc)
    except Exception as e:
        print("signal refresh price error:", e)
    win = loss = flat = open_n = 0
    pnls = []
    now = time.time()
    updated = []
    for r in rows:
        addr = (r.get("token_address") or "").lower()
        entry = float(r.get("entry_price") or 0)
        last = price_map.get(addr) or float(r.get("last_price") or 0)
        if last and entry > 0:
            pnl = (last / entry - 1.0) * 100.0
            r["last_price"] = last
            r["pnl_pct"] = round(pnl, 2)
            if mcap_map.get(addr):
                r["last_mcap"] = mcap_map[addr]
            age_h = (now - float(r.get("ts") or now)) / 3600
            # closed-ish after 24h for stats; still track OPEN under 24h
            if age_h >= 24 or abs(pnl) >= 3:
                if pnl >= 5:
                    r["status"] = "WIN"
                    win += 1
                elif pnl <= -5:
                    r["status"] = "LOSS"
                    loss += 1
                else:
                    r["status"] = "FLAT"
                    flat += 1
            else:
                r["status"] = "OPEN"
                open_n += 1
            pnls.append(pnl)
        else:
            r["status"] = r.get("status") or "OPEN"
            open_n += 1
        updated.append(r)
    with SIGNAL_LOCK:
        save_signals(updated)
    decided = win + loss
    wr = round(win / decided * 100, 1) if decided else 0.0
    avg = round(sum(pnls) / len(pnls), 2) if pnls else 0.0
    return {
        "total": len(updated),
        "open": open_n,
        "win": win,
        "loss": loss,
        "flat": flat,
        "winrate": wr,
        "avg_pnl": avg,
        "items": sorted(updated, key=lambda x: float(x.get("ts") or 0), reverse=True)[:20],
    }


def format_stats_msg(stats: Dict) -> str:
    total = stats.get("total") or 0
    win = stats.get("win") or 0
    loss = stats.get("loss") or 0
    flat = stats.get("flat") or 0
    open_n = stats.get("open") or 0
    wr = stats.get("winrate") or 0
    avg = stats.get("avg_pnl") or 0
    decided = win + loss
    lines = [
        "📊 <b>STATISTIK SIGNAL TELEGRAM</b>",
        "━━━━━━━━━━━━━━",
        f"Total sinyal   <b>{total}</b>",
        f"🟢 WIN         <b>{win}</b>",
        f"🔴 LOSS        <b>{loss}</b>",
        f"⚪ FLAT        <b>{flat}</b>",
        f"⏳ OPEN        <b>{open_n}</b>",
        "━━━━━━━━━━━━━━",
        f"Winrate        <b>{wr}%</b>  ({win}/{decided or 0} decided)",
        f"Avg PnL        <b>{avg:+.2f}%</b>",
        "",
        "Rules: WIN ≥+5% · LOSS ≤−5% · dihitung dari harga saat alert.",
        "OPEN = belum 24 jam / gerak &lt;3%.",
    ]
    for it in (stats.get("items") or [])[:8]:
        pnl = float(it.get("pnl_pct") or 0)
        st = it.get("status") or "OPEN"
        ico = {"WIN": "🟢", "LOSS": "🔴", "FLAT": "⚪", "OPEN": "⏳"}.get(st, "•")
        age_h = (time.time() - float(it.get("ts") or time.time())) / 3600
        lines.append(
            f"{ico} <b>${it.get('symbol')}</b> {pnl:+.1f}% · {st} · {age_h:.1f}j"
        )
    return "\n".join(lines)


def notify_token(row: Dict) -> bool:
    caption = format_alert(row)
    buttons = tg_buttons(row)
    icon = row.get("icon") or ""
    ok = False
    if icon and send_telegram_photo(icon, caption, buttons):
        ok = True
    else:
        ok = send_telegram(caption, buttons=buttons)
    if ok:
        try:
            record_signal(row, source="alert")
        except Exception as e:
            print("record_signal error:", e)
    return ok


def run_alert_pass() -> Dict:
    WORKER["running"] = True
    WORKER["last_start"] = time.time()
    WORKER["last_error"] = ""
    try:
        rows = scan_top(limit=20, mode="balanced")
        sent = 0
        now = time.time()
        for row in rows:
            # pastikan FOMO smart sudah di-enrich sebelum green check
            if FOMO_REQUIRE_SMART and FOMO_API_KEY and not has_fomo_smart_wallet(row):
                try:
                    apply_fomo_enrich(row)
                except Exception as e:
                    print("alert fomo enrich:", e)
            if not is_green_signal(row):
                continue
            key = f"{row.get('chain')}:{(row.get('token_address') or '').lower()}"
            last = SENT_ALERTS.get(key, 0)
            if now - last < 6 * 3600:
                continue
            if notify_token(row):
                SENT_ALERTS[key] = now
                sent += 1
        result = {
            "checked": len(rows),
            "sent": sent,
            "fomo_require": FOMO_REQUIRE_SMART,
            "fomo_key": bool(FOMO_API_KEY),
        }
        WORKER["last_result"] = result
        return result
    except Exception as e:
        WORKER["last_error"] = str(e)
        raise
    finally:
        WORKER["running"] = False
        WORKER["last_end"] = time.time()


def alert_loop():
    WORKER["loop_alive"] = True
    time.sleep(8)
    while True:
        try:
            print("alert pass:", run_alert_pass())
        except Exception as e:
            print("alert loop error:", e)
            WORKER["last_error"] = str(e)
        time.sleep(max(60, ALERT_INTERVAL))


@app.on_event("startup")
def _start_alerts():
    if os.getenv("ENABLE_TELEGRAM", "1") != "1":
        return
    t = threading.Thread(target=alert_loop, daemon=True)
    t.start()
    if os.getenv("ENABLE_TG_POLL", "1") == "1":
        threading.Thread(target=poll_telegram, daemon=True).start()


@app.get("/alerts/test")
def alerts_test():
    ok = send_telegram("Scanner LIVE. Test Telegram OK.")
    return {"ok": ok, "chat_configured": bool(TG_TOKEN and TG_CHAT)}


@app.get("/alerts/run")
def alerts_run():
    return run_alert_pass()


@app.get("/alerts/status")
def alerts_status():
    return {
        "loop_alive": WORKER.get("loop_alive"),
        "scanning": WORKER.get("running"),
        "telegram": bool(TG_TOKEN and TG_CHAT),
        "interval_sec": ALERT_INTERVAL,
        "last_start": WORKER.get("last_start") or 0,
        "last_end": WORKER.get("last_end") or 0,
        "last_result": WORKER.get("last_result") or {},
        "last_error": WORKER.get("last_error") or "",
        "sent_cache": len(SENT_ALERTS),
    }


@app.get("/alerts/stats")
def alerts_stats():
    return refresh_signal_stats()


@app.get("/")
def root():
    return {
        "status": "OK",
        "message": "Hybrid Early Gem Scanner LIVE",
        "endpoints": ["/ui", "/scan/top", "/scan/breakout", "/scan/watch", "/alerts/test", "/alerts/run"],
        "telegram": bool(TG_TOKEN and TG_CHAT),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
