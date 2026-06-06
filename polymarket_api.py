"""
Polymarket public API client — no auth required.

Data chain:
  1. polymarket.com/leaderboard  → scrape real top traders by P&L + volume
  2. data-api /positions          → each whale's current open positions
  3. data-api /activity           → each whale's recent trades
"""
import re
import json
import time
import requests
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_API = "https://data-api.polymarket.com"
HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def _get(url: str, params: dict = None):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[API ERROR] {url} → {e}")
        return None


# ── Step 1: scrape the real leaderboard ───────────────────────────────────────

def fetch_leaderboard() -> dict:
    """
    Returns {'profit': [...], 'volume': [...], 'biggest_wins': [...]}
    Each entry has: rank, proxyWallet, name, pseudonym, pnl, volume, realized
    Source: polymarket.com/leaderboard page __NEXT_DATA__
    """
    try:
        r = requests.get("https://polymarket.com/leaderboard", headers=HEADERS, timeout=20)
        r.raise_for_status()
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL
        )
        if not m:
            raise ValueError("__NEXT_DATA__ not found in page")

        page_data = json.loads(m.group(1))
        queries   = (
            page_data["props"]["pageProps"]
            .get("dehydratedState", {})
            .get("queries", [])
        )

        result = {"profit": [], "volume": [], "biggest_wins": []}
        for q in queries:
            key  = q.get("queryKey", [])
            data = q.get("state", {}).get("data", [])
            if not isinstance(data, list) or not data:
                continue
            if not isinstance(data[0], dict) or "proxyWallet" not in data[0]:
                continue
            sort_key = key[1] if len(key) > 1 else ""
            if sort_key == "profit":
                result["profit"] = data
            elif sort_key == "volume":
                result["volume"] = data
            elif sort_key == "biggestWins":
                result["biggest_wins"] = data

        return result

    except Exception as e:
        print(f"[LEADERBOARD ERROR] {e}")
        return {"profit": [], "volume": [], "biggest_wins": []}


def _clean_name(raw: str) -> str:
    if not raw:
        return ""
    # Strip auto-generated "0xABC…-timestamp" format
    if raw.startswith("0x") and "-" in raw:
        raw = raw.split("-")[0]
    return (raw[:12] + "…") if len(raw) > 15 else raw


def build_ranked_whales(leaderboard: dict, top_n: int = 20) -> list[dict]:
    """
    Merge profit and volume leaderboards.
    Rank primarily by 30d P&L (profit list), add volume-only traders after.
    Returns up to top_n unique entries with leaderboard metadata.
    """
    seen: OrderedDict[str, dict] = OrderedDict()

    # Profit list first — primary ranking
    for t in leaderboard.get("profit", []):
        addr = t.get("proxyWallet", "")
        if not addr:
            continue
        seen[addr] = {
            "address":      addr,
            "name":         _clean_name(t.get("name") or t.get("pseudonym") or ""),
            "pnl_30d":      float(t.get("pnl", 0) or 0),
            "volume_30d":   float(t.get("volume", 0) or 0),
            "realized_30d": float(t.get("realized", 0) or 0),
            "profit_rank":  t.get("rank"),
            "volume_rank":  None,
        }

    # Volume list — fill in volume_rank and add any not already seen
    for t in leaderboard.get("volume", []):
        addr = t.get("proxyWallet", "")
        if not addr:
            continue
        if addr in seen:
            seen[addr]["volume_rank"] = t.get("rank")
        else:
            seen[addr] = {
                "address":      addr,
                "name":         _clean_name(t.get("name") or t.get("pseudonym") or ""),
                "pnl_30d":      float(t.get("pnl", 0) or 0),
                "volume_30d":   float(t.get("volume", 0) or 0),
                "realized_30d": float(t.get("realized", 0) or 0),
                "profit_rank":  None,
                "volume_rank":  t.get("rank"),
            }

    # Biggest wins list — adds more unique high-value traders
    for t in leaderboard.get("biggest_wins", []):
        addr = t.get("proxyWallet", "")
        if not addr or addr in seen:
            continue
        seen[addr] = {
            "address":      addr,
            "name":         _clean_name(t.get("name") or t.get("pseudonym") or ""),
            "pnl_30d":      float(t.get("pnl", 0) or 0),
            "volume_30d":   float(t.get("volume", 0) or 0),
            "realized_30d": float(t.get("realized", 0) or 0),
            "profit_rank":  None,
            "volume_rank":  None,
        }

    return list(seen.values())[:top_n]


# ── Step 2 & 3: enrich each whale ─────────────────────────────────────────────

def get_positions(address: str) -> list[dict]:
    """Paginate through ALL positions (active + redeemable). Lost/redeemed positions
    are pruned by Polymarket's API and will NOT appear regardless of pagination."""
    all_positions = []
    offset = 0
    limit  = 500
    while True:
        batch = _get(f"{DATA_API}/positions", {"user": address, "limit": limit, "offset": offset})
        if not isinstance(batch, list) or not batch:
            break
        all_positions.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.15)   # gentle throttle for pagination
    return all_positions


def get_activity(address: str, limit: int = 30) -> list[dict]:
    data = _get(f"{DATA_API}/activity", {"user": address, "limit": limit})
    return data if isinstance(data, list) else []


def _is_active_position(p: dict) -> bool:
    cur = float(p.get("curPrice", 0) or 0)
    return not p.get("redeemable", False) and cur not in (0.0, 1.0)


def _is_resolved_position(p: dict) -> bool:
    cur = float(p.get("curPrice", 0) or 0)
    return cur in (0.0, 1.0) or p.get("redeemable", False)


def _calc_exit_behavior(activity: list[dict], positions: list[dict]) -> dict:
    """
    Detect early exits using realizedPnl + totalBought on resolved positions.

    Key fields (from Polymarket positions API):
      totalBought  : total tokens ever purchased for this market
      size         : remaining token balance (totalBought - sold)
      realizedPnl  : USDC received from selling tokens (not from redeeming won bets)
      endDate      : when the market resolved

    If totalBought > size  →  tokens were sold before resolution = early exit
    curPrice = 1 after early exit  →  took profit early (left gains on table)
    curPrice = 0 after early exit  →  cut loss early (saved some capital)
    """
    early_profit: list[dict] = []
    early_stop:   list[dict] = []
    held_win:     list[dict] = []
    held_loss:    list[dict] = []

    for p in positions:
        if not _is_resolved_position(p):
            continue

        cur_price    = float(p.get("curPrice", 0) or 0)
        realized_pnl = float(p.get("realizedPnl", 0) or 0)
        total_bought = float(p.get("totalBought", 0) or 0)
        size         = float(p.get("size", 0) or 0)
        avg_price    = float(p.get("avgPrice", 0) or 0)
        initial_val  = float(p.get("initialValue", 0) or 0)
        title        = p.get("title", "Unknown")
        end_date     = (p.get("endDate") or "")[:10]   # YYYY-MM-DD
        won          = cur_price >= 0.99 or p.get("redeemable", False)

        tokens_sold  = max(0.0, total_bought - size)
        sold_early   = tokens_sold > 1.0 or abs(realized_pnl) > 0.01

        base = {"market": title, "end_date": end_date}

        if sold_early and tokens_sold > 0:
            # Estimate avg sell price: proceeds = realizedPnl + cost_of_sold
            cost_of_sold = avg_price * tokens_sold
            proceeds     = realized_pnl + cost_of_sold
            sell_price   = round(proceeds / tokens_sold, 3) if tokens_sold > 0 else None

            if won:
                left = round(tokens_sold * (1.0 - (sell_price or avg_price)), 2)
                early_profit.append({
                    **base,
                    "sell_price":    sell_price,
                    "left_on_table": max(0, left),
                    "tokens_sold":   round(tokens_sold, 0),
                    "verdict":       "Early Profit",
                })
            else:
                saved = max(0.0, round(proceeds, 2))
                early_stop.append({
                    **base,
                    "sell_price":  sell_price,
                    "saved":       saved,
                    "tokens_sold": round(tokens_sold, 0),
                    "verdict":     "Stop Loss",
                })
        else:
            entry = {**base, "initial_usdc": round(initial_val, 0)}
            (held_win if won else held_loss).append(entry)

    total_early = len(early_profit) + len(early_stop)
    total_hold  = len(held_win) + len(held_loss)
    total       = total_early + total_hold
    early_pct   = round(total_early / total * 100) if total > 0 else 0

    if total < 3:
        style   = "Insufficient data"
        summary = "Too few resolved positions"
    elif early_pct >= 60:
        if len(early_profit) >= len(early_stop):
            style   = "Early Profit Taker"
            summary = f"Exits before resolution to lock gains — {total_early} early exits vs {total_hold} holds"
        else:
            style   = "Stop Loss Setter"
            summary = f"Cuts losses before they hit 0 — disciplined risk control"
    elif early_pct <= 25:
        wr = round(len(held_win) / total_hold * 100) if total_hold else 0
        style   = "Diamond Hands"
        summary = f"Holds to resolution — {wr}% win rate on {total_hold} resolved bets"
    else:
        style   = "Mixed Exit"
        summary = f"Early exits {early_pct}% of time, holds rest to resolution"

    return {
        "exit_style":       style,
        "exit_summary":     summary,
        "early_exit_pct":   early_pct,
        "early_exit_count": total_early,
        "hold_count":       total_hold,
        "early_profit":     sorted(early_profit, key=lambda x: x.get("left_on_table",0), reverse=True)[:8],
        "early_stop":       sorted(early_stop,   key=lambda x: x.get("saved",0),         reverse=True)[:8],
        "held_win":         held_win[:5],
        "held_loss":        held_loss[:5],
    }


def _ts_to_str(ts: int) -> str:
    """Unix timestamp → human-readable relative string."""
    if not ts:
        return ""
    import time
    diff = int(time.time()) - ts
    if diff < 60:        return f"{diff}s ago"
    if diff < 3600:      return f"{diff // 60}m ago"
    if diff < 86400:     return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def _calc_stake_and_pnl_profile(activity: list[dict], positions: list[dict],
                                 pnl_30d: float, realized_30d: float) -> dict:
    """
    Analyse 注碼 (bet sizing) and P&L growth pattern.

    Returns:
      stake_avg, stake_max, stake_min, stake_cv (coefficient of variation),
      stake_trend  : "growing" | "shrinking" | "stable" | "erratic"
      pnl_composition : {"realized": %, "unrealized": %}
      avg_entry_price  : where they tend to enter (low=longshot, high=favourite)
      trader_type  : classification string
      trader_desc  : one-line explanation
    """
    import math

    trades = [t for t in activity if t.get("type") == "TRADE"]
    amounts = [float(t.get("usdcSize", 0) or 0) for t in trades if float(t.get("usdcSize", 0) or 0) > 0]

    # ── 注碼 stats ─────────────────────────────────────────────────────────────
    if len(amounts) < 2:
        stake_stats = {
            "stake_avg": amounts[0] if amounts else 0,
            "stake_max": amounts[0] if amounts else 0,
            "stake_min": amounts[0] if amounts else 0,
            "stake_cv":  None,
            "stake_trend": "unknown",
        }
    else:
        avg   = sum(amounts) / len(amounts)
        variance = sum((x - avg) ** 2 for x in amounts) / len(amounts)
        std   = math.sqrt(variance)
        cv    = round(std / avg, 2) if avg > 0 else None  # coefficient of variation

        # Trend: compare first half vs second half of recent trades
        half = len(amounts) // 2
        early_avg = sum(amounts[:half]) / half
        late_avg  = sum(amounts[half:]) / (len(amounts) - half)
        if late_avg > early_avg * 1.3:
            trend = "growing"    # staking more recently — pressing/confidence
        elif late_avg < early_avg * 0.7:
            trend = "shrinking"  # pulling back — cautious or losing edge
        elif cv and cv > 0.8:
            trend = "erratic"    # huge variance between bets
        else:
            trend = "stable"     # consistent sizing

        stake_stats = {
            "stake_avg":   round(avg, 0),
            "stake_max":   round(max(amounts), 0),
            "stake_min":   round(min(amounts), 0),
            "stake_cv":    cv,
            "stake_trend": trend,
        }

    # ── P&L composition ────────────────────────────────────────────────────────
    unrealized = sum(float(p.get("cashPnl", 0) or 0)
                     for p in positions if _is_active_position(p))
    total_abs = abs(realized_30d) + abs(unrealized)
    if total_abs > 0:
        pnl_composition = {
            "realized_pct":   round(abs(realized_30d) / total_abs * 100, 1),
            "unrealized_pct": round(abs(unrealized)   / total_abs * 100, 1),
            "realized_sign":  "profit" if realized_30d >= 0 else "loss",
        }
    else:
        pnl_composition = {"realized_pct": 0, "unrealized_pct": 0, "realized_sign": "—"}

    # ── Average entry price across open positions ──────────────────────────────
    entry_prices = [float(p.get("avgPrice", 0) or 0)
                    for p in positions if _is_active_position(p)
                    and float(p.get("avgPrice", 0) or 0) > 0]
    avg_entry = round(sum(entry_prices) / len(entry_prices), 3) if entry_prices else None

    # ── Classify trader type ───────────────────────────────────────────────────
    trend     = stake_stats["stake_trend"]
    cv        = stake_stats["stake_cv"]
    stake_avg = stake_stats["stake_avg"]
    is_profitable = pnl_30d > 0

    if stake_avg >= 50_000 and trend == "stable" and is_profitable:
        trader_type = "High-Conviction Whale"
        trader_desc = f"Consistently bets large (avg ${stake_avg:,.0f}), growing P&L steadily"
    elif trend == "growing" and is_profitable:
        trader_type = "Pressing Winner"
        trader_desc = f"Increasing stake size as P&L grows — riding hot streak"
    elif trend == "growing" and not is_profitable:
        trader_type = "Martingale Risk"
        trader_desc = f"Staking more while losing — classic revenge-betting pattern"
    elif trend == "stable" and is_profitable and (cv is None or cv < 0.5):
        trader_type = "Disciplined Trader"
        trader_desc = f"Consistent bet sizing (avg ${stake_avg:,.0f}), steady profit growth"
    elif trend == "erratic" or (cv and cv > 0.8):
        trader_type = "Erratic / Emotional"
        trader_desc = f"Bet sizes vary wildly (CV={cv}) — driven by emotion not system"
    elif trend == "shrinking" and is_profitable:
        trader_type = "Taking Profits"
        trader_desc = f"Reducing exposure while ahead — disciplined risk management"
    elif trend == "shrinking" and not is_profitable:
        trader_type = "Losing Edge"
        trader_desc = f"Shrinking bets as P&L deteriorates — losing confidence"
    elif avg_entry and avg_entry < 0.2:
        trader_type = "Longshot Hunter"
        trader_desc = f"Targets low-probability events (avg entry {avg_entry:.2f})"
    elif avg_entry and avg_entry > 0.8:
        trader_type = "Favourite Backer"
        trader_desc = f"Bets near-certain outcomes (avg entry {avg_entry:.2f}) — low risk, low reward"
    else:
        trader_type = "Balanced"
        trader_desc = f"Mixed strategy, avg stake ${stake_avg:,.0f}"

    return {
        **stake_stats,
        "pnl_composition": pnl_composition,
        "avg_entry_price": avg_entry,
        "trader_type":     trader_type,
        "trader_desc":     trader_desc,
    }


def _calc_win_stats(positions: list[dict]) -> dict:
    """
    From ALL positions (active + resolved), compute:
      - win_rate       : resolved wins / total resolved (0-100)
      - avg_win_usdc   : average profit on winning resolved bets
      - avg_loss_usdc  : average loss on losing resolved bets (positive number)
      - profit_factor  : avg_win / avg_loss  (>1 = good, <1 = gambler)
      - resolved_count : how many resolved bets used for stats
      - label          : "Skilled" / "Value Bettor" / "High Risk" / "Gambler" / "Insufficient data"
    """
    wins, losses = [], []

    for p in positions:
        if not _is_resolved_position(p):
            continue
        initial    = float(p.get("initialValue", 0) or 0)
        if initial <= 0:
            continue
        cur_price  = float(p.get("curPrice",     0) or 0)
        redeemable = p.get("redeemable", False)
        size       = float(p.get("size",         0) or 0)
        total_bought = float(p.get("totalBought", 0) or 0)

        if redeemable or cur_price == 1.0:
            # Won. Use size*1 if still held; use totalBought*1 as proxy if already redeemed (size=0)
            redeemable_tokens = size if size > 0 else total_bought
            profit = redeemable_tokens - initial
            wins.append(profit)
        else:
            # Lost (curPrice=0). Token worthless — lost full initial stake.
            losses.append(initial)

    total = len(wins) + len(losses)
    if total < 3:
        return {
            "win_rate": None, "avg_win_usdc": None, "avg_loss_usdc": None,
            "profit_factor": None, "resolved_count": total, "label": "Insufficient data"
        }

    win_rate      = round(len(wins) / total * 100, 1)
    avg_win       = round(sum(wins)   / len(wins),   2) if wins   else 0
    avg_loss      = round(sum(losses) / len(losses), 2) if losses else 0
    profit_factor = round(avg_win / avg_loss, 2) if avg_loss > 0 else None

    # Classify
    pnl_positive = (sum(wins) - sum(losses)) > 0
    if win_rate >= 55 and pnl_positive:
        label = "Skilled"
    elif win_rate < 55 and pnl_positive and profit_factor and profit_factor >= 1.5:
        label = "Value Bettor"        # wins less often but wins big
    elif win_rate >= 50 and not pnl_positive:
        label = "High Risk"           # wins often but over-sizes losers
    elif win_rate < 40:
        label = "Gambler"
    else:
        label = "Balanced"

    return {
        "win_rate":       win_rate,
        "avg_win_usdc":   avg_win,
        "avg_loss_usdc":  avg_loss,
        "profit_factor":  profit_factor,
        "resolved_count": total,
        "label":          label,
    }


def _summarise_positions(raw: list[dict]) -> list[dict]:
    active = [p for p in raw if _is_active_position(p)]
    ranked = sorted(
        active, key=lambda x: abs(float(x.get("currentValue", 0) or 0)), reverse=True
    )[:12]
    return [
        {
            "market":       p.get("title", "Unknown"),
            "outcome":      p.get("outcome", "?"),
            "size":         round(float(p.get("size", 0) or 0), 2),
            "avg_price":    round(float(p.get("avgPrice", 0) or 0), 3),
            "cur_price":    round(float(p.get("curPrice", 0) or 0), 3),
            "initial_usdc": round(float(p.get("initialValue", 0) or 0), 2),
            "current_usdc": round(float(p.get("currentValue", 0) or 0), 2),
            "cash_pnl":     round(float(p.get("cashPnl", 0) or 0), 2),
            "realized_pnl": round(float(p.get("realizedPnl", 0) or 0), 2),
            "pct_pnl":      round(float(p.get("percentPnl", 0) or 0), 1),
        }
        for p in ranked
    ]


def _summarise_activity(raw: list[dict]) -> list[dict]:
    return [
        {
            "market":    t.get("title", ""),
            "side":      t.get("side", "?"),
            "outcome":   t.get("outcome", "?"),
            "amount":    round(float(t.get("usdcSize", 0) or 0), 2),
            "price":     round(float(t.get("price", 0) or 0), 3),
            "timestamp": int(t.get("timestamp", 0) or 0),
            "tx_hash":   t.get("transactionHash", ""),
        }
        for t in raw if t.get("type") == "TRADE"
    ][:200]


# ── Main entrypoint ────────────────────────────────────────────────────────────

def build_whale_profiles(top_n: int = 20) -> list[dict]:
    print("  Fetching real leaderboard from polymarket.com…")
    leaderboard = fetch_leaderboard()

    profit_count = len(leaderboard.get("profit", []))
    volume_count = len(leaderboard.get("volume", []))
    print(f"  Got {profit_count} profit leaders, {volume_count} volume leaders")

    if profit_count == 0 and volume_count == 0:
        return []

    whales = build_ranked_whales(leaderboard, top_n=top_n)
    print(f"  Enriching {len(whales)} whale profiles…")

    def _fetch_whale(rank_w):
        rank, w = rank_w
        address   = w["address"]
        positions = get_positions(address)
        activity  = get_activity(address, limit=500)  # extended history for better analysis

        active_pos     = [p for p in positions if _is_active_position(p)]
        redeemable_pos = [p for p in positions if p.get("redeemable", False)]
        unrealized_pnl = sum(float(p.get("cashPnl", 0) or 0) for p in active_pos)
        total_invested = sum(float(p.get("initialValue", 0) or 0) for p in active_pos)

        # ── Fund-flow analysis (from all fetched positions) ──────────────────
        # NOTE: Polymarket prunes lost/redeemed-zero positions from the API,
        # so these figures cover visible positions only (active + pending wins).
        # Lost capital is not returned by the API regardless of pagination.
        all_invested       = sum(float(p.get("initialValue", 0) or 0) for p in positions)
        all_early_exits    = sum(float(p.get("realizedPnl",  0) or 0) for p in positions)
        # Pending wins USDC = sum of token balances in won positions (1 token = 1 USDC when redeemed)
        pending_wins_usdc  = sum(float(p.get("size", 0) or 0) for p in redeemable_pos)
        # Visible net: what we can prove they've made (without counting losses we can't see)
        visible_net        = pending_wins_usdc + all_early_exits - all_invested
        # ROI multiplier on visible capital: how many times their invested capital returned
        roi_multiplier     = round(pending_wins_usdc / all_invested, 2) if all_invested > 0 else None
        win_stats      = _calc_win_stats(positions)
        stake_profile  = _calc_stake_and_pnl_profile(
            activity, positions, w["pnl_30d"], w["realized_30d"]
        )
        exit_profile   = _calc_exit_behavior(activity, positions)
        return rank, {
            "rank":           rank,
            "name":           w["name"] or address[:10] + "…",
            "address":        address,
            "pnl_30d":        round(w["pnl_30d"], 2),
            "volume_30d":     round(w["volume_30d"], 2),
            "realized_30d":   round(w["realized_30d"], 2),
            "profit_rank":    w["profit_rank"],
            "volume_rank":    w["volume_rank"],
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_invested": round(total_invested, 2),
            "total_value":    round(
                sum(float(p.get("currentValue", 0) or 0) for p in active_pos), 2
            ),
            "open_positions":    _summarise_positions(positions),
            "recent_trades":    _summarise_activity(activity),
            "latest_trade_ts":  max(
                (int(t.get("timestamp", 0) or 0) for t in activity if t.get("type") == "TRADE"),
                default=0
            ),
            "win_rate":          win_stats["win_rate"],
            "avg_win_usdc":      win_stats["avg_win_usdc"],
            "avg_loss_usdc":     win_stats["avg_loss_usdc"],
            "profit_factor":     win_stats["profit_factor"],
            "resolved_count":    win_stats["resolved_count"],
            "pending_wins_usdc":  round(pending_wins_usdc, 0),
            # Fund-flow (visible positions only — lost bets pruned by Polymarket API)
            "all_invested":       round(all_invested, 0),
            "all_early_exits":    round(all_early_exits, 0),
            "visible_net":        round(visible_net, 0),
            "roi_multiplier":     roi_multiplier,
            "stake_avg":       stake_profile["stake_avg"],
            "stake_max":       stake_profile["stake_max"],
            "stake_min":       stake_profile["stake_min"],
            "stake_cv":        stake_profile["stake_cv"],
            "stake_trend":     stake_profile["stake_trend"],
            "avg_entry_price": stake_profile["avg_entry_price"],
            "pnl_composition": stake_profile["pnl_composition"],
            "trader_type":     stake_profile["trader_type"],
            "trader_desc":     stake_profile["trader_desc"],
            # Exit behaviour
            "exit_style":      exit_profile["exit_style"],
            "exit_summary":    exit_profile["exit_summary"],
            "early_exit_pct":  exit_profile["early_exit_pct"],
            "early_exit_count":exit_profile["early_exit_count"],
            "hold_count":      exit_profile["hold_count"],
            "early_profit":    exit_profile["early_profit"],
            "early_stop":      exit_profile["early_stop"],
            "held_win":        exit_profile["held_win"],
            "held_loss":       exit_profile["held_loss"],
        }

    results = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_whale, (rank, w)): rank
                   for rank, w in enumerate(whales, start=1)}
        for fut in as_completed(futures):
            try:
                rank, profile = fut.result()
                results[rank] = profile
            except Exception as e:
                print(f"[WHALE FETCH ERROR] rank {futures[fut]}: {e}")

    return [results[r] for r in sorted(results)]
