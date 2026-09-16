from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from typing import List, Dict, Optional, Tuple
import requests
import time
import os
import json
import threading

app = FastAPI(title="Hybrid Early Gem Scanner – DexScreener PRO")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CHAINS = {"ethereum", "base", "solana", "bsc", "arbitrum", "avalanche", "robinhood"}

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
WATCH_MAX_AGE_HOURS = 14 * 24
WATCH_LOCK = threading.Lock()
CART_LOCK = threading.Lock()
WALLET_LOCK = threading.Lock()


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
        "price_change_m5": round(num(p, "priceChange", "m5"), 2),
        "price_change_h1": round(num(p, "priceChange", "h1"), 2),
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
    return {
        "provider": "goplus",
        "honeypot": honeypot,
        "risk": risk,
        "flags": flags,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "owner": info.get("owner_address") or "",
        "lp_holders": info.get("lp_holder_count"),
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


def _holder_snapshot(data: Dict) -> Dict:
    holders = data.get("topHolders") or []
    creator = data.get("creator") or ""
    top = []
    insider_pct = 0.0
    top10 = 0.0
    for h in holders[:12]:
        if not isinstance(h, dict):
            continue
        pct = float(h.get("pct") or h.get("percentage") or 0)
        addr = h.get("address") or h.get("owner") or h.get("wallet") or ""
        insider = bool(h.get("insider") or h.get("isInsider"))
        label = str(h.get("label") or "")
        top.append({"address": addr, "pct": round(pct, 2), "insider": insider, "label": label})
        low = label.lower()
        if any(x in low for x in ("pool", "raydium", "lp", "pump")):
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
    top1 = top[0]["pct"] if top else 0
    non_lp = [h for h in top if "pool" not in str(h.get("label") or "").lower() and "lp" not in str(h.get("label") or "").lower()]
    top1_nonlp = non_lp[0]["pct"] if non_lp else 0
    lp_locked = data.get("lpLocked")
    if lp_locked is None:
        markets = data.get("markets") or []
        if markets and isinstance(markets[0], dict):
            lp_locked = markets[0].get("lpLocked")
    lp_pct = data.get("lpLockedPct")
    if "CREATOR_RUGGED" in flags_h:
        note = "creator punya jejak rug"
    elif "INSIDER_CLUSTER" in flags_h or "INSIDER_NETWORK" in flags_h:
        note = "cluster insider/bundler"
    elif top10 >= 50:
        note = "top holder non-LP kuasai supply"
    else:
        note = "sebaran holder biasa"
    return {
        "creator": creator or "",
        "top_holders": top[:8],
        "top1_pct": round(float(top1_nonlp or top1), 2),
        "top10_pct": round(top10, 2),
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
        row["holder_count"] = sec.get("holder_count") or 0
        row["lp_locked"] = sec.get("lp_locked")
        row["lp_locked_pct"] = sec.get("lp_locked_pct")
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


@app.get("/scan/top")
def scan_top(
    limit: int = 10,
    mode: str = Query("balanced", enum=["strict", "balanced", "aggressive"]),
):
    try:
        best_pairs = group_best_by_token(fetch_pairs())
        results = []
        for p in best_pairs.values():
            if not is_pair_young(p):
                continue
            liq = num(p, "liquidity", "usd")
            vol = num(p, "volume", "h24")
            if mode == "strict" and (is_suspicious(p) or liq < 10_000 or vol < 10_000):
                continue
            if mode == "balanced" and (liq < 1_000 or vol < 1_000):
                continue
            if mode == "aggressive" and liq < 300:
                continue
            results.append(enrich(p, False))
        ranked = sorted(results, key=lambda x: x["confidence"], reverse=True)[:limit]
        secured = attach_security(ranked)
        remember_tokens(secured)
        return secured
    except Exception as e:
        print("DISCOVERY ERROR:", e)
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
    if flags:
        safety = min(safety, 50)
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
    if addr.startswith("0x") and len(addr) == 42:
        addr = addr
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
        if not pairs:
            return {"error": "pair tidak ketemu di DexScreener", "address": addr}
        # satu mint: kalau user tempel CA token, kunci ke base=CA
        base_hits = [p for p in pairs if ((p.get("baseToken") or {}).get("address") or "").lower() == want_addr]
        if base_hits:
            pairs = base_hits
        pair_hits = [p for p in pairs if (p.get("pairAddress") or "").lower() == want_addr]
        if pair_hits:
            pairs = pair_hits
        best = group_best_by_token(pairs)
        rows = [enrich(p, False) for p in best.values()]
        rows = attach_security(rows)
        remember_tokens(rows)
        for row in rows:
            chain_l = str(row.get("chain") or "").lower()
            pair = row.get("pair_address") or ""
            if chain_l and pair:
                row["traders_url"] = f"https://dexscreener.com/{chain_l}/{pair}"
            row["fomo_url"] = fomo_url(row)
            row["green_alert"] = is_green_signal(row)
            row["ca_report"] = ca_analysis(row)
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
    return True


def tg_buttons(row: Dict) -> dict:
    buttons = []
    fomo = fomo_url(row)
    dex = dex_url(row)
    row_btns = []
    if fomo:
        row_btns.append({"text": "Trade FOMO", "url": fomo})
    if dex:
        row_btns.append({"text": "DexScreener", "url": dex})
    if row_btns:
        buttons.append(row_btns)
    return {"inline_keyboard": buttons} if buttons else {}


def send_telegram(text: str, parse_mode: str = "HTML", buttons: Optional[dict] = None) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        print("telegram skip: token/chat_id kosong")
        return False
    payload = {
        "chat_id": TG_CHAT,
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


def send_telegram_photo(photo_url: str, caption: str, buttons: Optional[dict] = None) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        return False
    payload = {
        "chat_id": TG_CHAT,
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
        f"🟢 <b>GREEN GEM</b>\n"
        f"{header}\n"
        f"{chain} · {dex}\n"
        f"━━━━━━━━━━━━━━\n"
        f"Score        <b>{row.get('score')}</b>\n"
        f"Confidence   <b>{row.get('confidence')}</b>\n"
        f"Risk         <b>{_esc(row.get('risk'))}</b>\n"
        f"Upside       <b>{_esc(row.get('upside'))}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"Liq     {_usd(row.get('liquidity_usd'))}\n"
        f"MCap    {_usd(row.get('market_cap'))}\n"
        f"Vol     {_usd(row.get('volume_24h'))}\n"
        f"24h     {ch}\n"
        f"━━━━━━━━━━━━━━\n"
        f"CA\n<code>{addr}</code>\n"
    )
    creator = _esc(row.get("creator") or "")
    if creator:
        body += f"Dev  <code>{creator}</code>\n"
        body += f"Dev tx  https://solscan.io/account/{creator}\n"
    note = _esc(row.get("holder_note") or "")
    if note:
        body += (
            f"Holders  {note}\n"
            f"Top1 {row.get('top1_pct') or 0}% · Top10 {row.get('top10_pct') or 0}%"
            f" · n={row.get('holder_count') or '-'}\n"
        )
        if row.get("lp_locked") is not None:
            body += f"LP lock  {row.get('lp_locked')} {row.get('lp_locked_pct') or ''}%\n"
    fomo = fomo_url(row)
    body += f"Social  {social}\n"
    body += f"Chart   {chart}\n"
    if fomo:
        body += f'Trade   <a href="{_esc(fomo)}">Open FOMO</a>'
    return body


def notify_token(row: Dict) -> bool:
    caption = format_alert(row)
    buttons = tg_buttons(row)
    icon = row.get("icon") or ""
    if icon and send_telegram_photo(icon, caption, buttons):
        return True
    return send_telegram(caption, buttons=buttons)


def run_alert_pass() -> Dict:
    WORKER["running"] = True
    WORKER["last_start"] = time.time()
    WORKER["last_error"] = ""
    try:
        rows = scan_top(limit=20, mode="balanced")
        sent = 0
        now = time.time()
        for row in rows:
            if not is_green_signal(row):
                continue
            key = f"{row.get('chain')}:{(row.get('token_address') or '').lower()}"
            last = SENT_ALERTS.get(key, 0)
            if now - last < 6 * 3600:
                continue
            if notify_token(row):
                SENT_ALERTS[key] = now
                sent += 1
        result = {"checked": len(rows), "sent": sent}
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
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True
