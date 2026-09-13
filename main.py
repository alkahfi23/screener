from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from typing import List, Dict, Optional, Tuple
import requests
import time
import os
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
        "price_change_24h": round(num(p, "priceChange", "h24"), 2),
        "age_hours": round(age, 2) if age is not None else None,
        "pair_address": p.get("pairAddress"),
        "url": p.get("url") or "",
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
        "top10_pct": round(top10, 2),
        "insider_pct": round(insider_pct, 2),
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

    if vol_liq > 10:
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
        return attach_security(ranked)
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
    if upside in {"WASHY", "THIN", "NO UPSIDE", "LOW ROOM", "UNRATED", "CAUTION SETUP"}:
        return False
    return True


def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    if not TG_TOKEN or not TG_CHAT:
        print("telegram skip: token/chat_id kosong")
        return False
    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print("telegram error:", e)
        return False


def send_telegram_photo(photo_url: str, caption: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        return False
    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            json={
                "chat_id": TG_CHAT,
                "photo": photo_url,
                "caption": caption[:1024],
                "parse_mode": "HTML",
            },
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
        body += f"Holders  {note} (top {row.get('top10_pct') or 0}%)\n"
    body += f"Social  {social}\nChart   {chart}"
    return body


def notify_token(row: Dict) -> bool:
    caption = format_alert(row)
    icon = row.get("icon") or ""
    if icon and send_telegram_photo(icon, caption):
        return True
    return send_telegram(caption)


def run_alert_pass() -> Dict:
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
    return {"checked": len(rows), "sent": sent}


def alert_loop():
    time.sleep(8)
    while True:
        try:
            print("alert pass:", run_alert_pass())
        except Exception as e:
            print("alert loop error:", e)
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


@app.get("/")
def root():
    return {
        "status": "OK",
        "message": "Hybrid Early Gem Scanner LIVE",
        "endpoints": ["/ui", "/scan/top", "/scan/breakout", "/alerts/test", "/alerts/run"],
        "telegram": bool(TG_TOKEN and TG_CHAT),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
