"""
Pure-Python whale analysis — no API key required.
Detects consensus, contradictions, and generates a structured report dict.
Claude enhancement is optional (used only if ANTHROPIC_API_KEY is set).
"""
import os
import json
from collections import defaultdict, Counter
from datetime import datetime


# ── Market category classification ────────────────────────────────────────────

# Rules checked in order — first match wins
_CATEGORY_RULES = [
    ("Crypto", [
        "bitcoin","btc","ethereum","eth ","crypto","defi","nft","solana","sol ",
        "xrp","dogecoin","doge","binance","coinbase","blockchain","altcoin",
        "stablecoin","web3","polygon","chainlink","avalanche","cardano","ripple",
    ]),
    ("Politics", [
        "election","president","senate","congress","republican","democrat",
        "trump","biden","harris","governor","vote","ballot","legislation",
        "supreme court","white house","parliament","minister","cabinet",
        "administration","tariff","sanction","attorney general","secretary of",
    ]),
    ("Sports", [
        "nba","nfl","mlb","nhl","mls","ufc","fifa","champions league",
        "premier league","la liga","bundesliga","serie a","ligue 1",
        "super bowl","world cup","euro 2","copa america","tennis","golf",
        "boxing","wrestling","olympic","championship","esports",
        "counter-strike","cs2","dota","valorant","league of legends",
        " vs "," vs. ",
        "spread:","over/under","o/u ","point spread",
        " fc ","fc win"," fc?", "win the league","win the cup",
        "win the nba","win the nfl","win the mlb","win the nhl",
    ]),
    ("Finance", [
        "stock","nasdaq","s&p 500","dow jones","fed rate","interest rate",
        "inflation","gdp","recession","ipo","earnings","commodity","oil price",
        "gold price","silver","forex","treasury","bond yield","wall street",
    ]),
    ("Entertainment", [
        "oscar","emmy","grammy","golden globe","academy award","box office",
        "movie","film","tv show","celebrity","album","netflix","streaming",
        "taylor swift","billboard","spotify","grammy",
    ]),
    ("Science / Tech", [
        "openai","chatgpt","gpt-","artificial intelligence","spacex","nasa",
        "rocket launch","satellite","climate","earthquake","hurricane",
        "pandemic","vaccine","fda approval","drug approval","microsoft",
        "google","apple stock","nvidia","meta ","anthropic",
    ]),
    ("World Events", [
        "war","ceasefire","peace deal","russia","ukraine","china","taiwan",
        "israel","gaza","iran","north korea","nato","united nations",
        "nuclear","missile","invasion","conflict","geopolit","refugee",
    ]),
]

CATEGORY_EMOJI = {
    "Crypto":        "₿",
    "Politics":      "🏛",
    "Sports":        "⚽",
    "Finance":       "📈",
    "Entertainment": "🎬",
    "Science / Tech":"🔬",
    "World Events":  "🌍",
    "Other":         "📌",
}


def classify_market(title: str) -> str:
    t = title.lower()
    for category, keywords in _CATEGORY_RULES:
        if any(kw in t for kw in keywords):
            return category
    return "Other"


def _analyze_categories(profiles: list) -> dict:
    """
    For each market category:
      - total capital deployed (open positions, rank 1-30 only)
      - recent trade volume
      - which whales are most active
      - dominant exit style
    Also builds per-whale primary category and portfolio breakdown.
    """
    cat_capital    = defaultdict(float)   # cat → USDC in open positions
    cat_trade_vol  = defaultdict(float)   # cat → USDC from recent trades
    cat_whales     = defaultdict(dict)    # cat → {name: {capital, rank, pnl}}
    cat_exit_styles= defaultdict(list)    # cat → [exit_style, ...]
    whale_cat_caps = {}                   # name → {cat: capital}

    for p in profiles:
        if p.get("rank", 99) > 30:
            continue
        name       = p["name"]
        rank       = p["rank"]
        pnl        = p.get("pnl_30d", 0)
        exit_style = p.get("exit_style", "")

        per_cat = defaultdict(float)
        for pos in p.get("open_positions", []):
            cat = classify_market(pos["market"])
            cap = pos.get("initial_usdc", 0)
            per_cat[cat]       += cap
            cat_capital[cat]   += cap
            if exit_style and exit_style != "Insufficient data":
                cat_exit_styles[cat].append(exit_style)
            if name not in cat_whales[cat]:
                cat_whales[cat][name] = {"capital": 0.0, "rank": rank, "pnl": pnl}
            cat_whales[cat][name]["capital"] += cap

        for t in p.get("recent_trades", []):
            cat = classify_market(t.get("market", ""))
            cat_trade_vol[cat] += t.get("amount", 0)

        whale_cat_caps[name] = dict(per_cat)

    # Build category list
    all_cats = set(cat_capital) | set(cat_trade_vol)
    categories = []
    for cat_name in all_cats:
        total_cap  = cat_capital.get(cat_name, 0)
        trade_vol  = cat_trade_vol.get(cat_name, 0)
        if total_cap < 200 and trade_vol < 200:
            continue

        whales_here = cat_whales.get(cat_name, {})
        top_whales  = sorted(
            [{"name": k, "rank": v["rank"], "capital": round(v["capital"]),
              "pnl": v["pnl"]}
             for k, v in whales_here.items()],
            key=lambda x: (-x["capital"], -x["pnl"])
        )[:4]

        # Dominant exit style in this category
        styles = cat_exit_styles.get(cat_name, [])
        dom_style = Counter(styles).most_common(1)[0][0] if styles else "—"

        categories.append({
            "name":          cat_name,
            "emoji":         CATEGORY_EMOJI.get(cat_name, "📌"),
            "total_capital": round(total_cap),
            "trade_volume":  round(trade_vol),
            "whale_count":   len(whales_here),
            "top_whales":    top_whales,
            "dominant_exit": dom_style,
        })

    categories.sort(key=lambda x: -x["total_capital"])

    # Per-whale primary category
    whale_focus = {}
    for p in profiles:
        if p.get("rank", 99) > 30:
            continue
        name    = p["name"]
        caps    = whale_cat_caps.get(name, {})
        total   = sum(caps.values())
        if caps and total > 0:
            primary = max(caps, key=caps.get)
            pct     = round(caps[primary] / total * 100)
            breakdown = {k: round(v / total * 100)
                         for k, v in sorted(caps.items(), key=lambda x: -x[1])}
        else:
            primary, pct, breakdown = "—", 0, {}
        whale_focus[name] = {"primary": primary, "pct": pct, "breakdown": breakdown}

    return {"categories": categories, "whale_focus": whale_focus}


# ── Copy-trade / herd detection ──────────────────────────────────────────────

_FOLLOW_WINDOW = 12 * 3600   # 12 h — entries within this window flagged as potential follows
_MIN_FOLLOW_INSTANCES = 3    # need at least 3 shared timed entries to flag a pair


def _detect_copy_traders(profiles: list[dict]) -> dict:
    """
    Detect whales that appear to copy each other's entry timing.

    For every (market, outcome) pair collect all BUY trades with timestamps.
    Per whale, take the EARLIEST buy timestamp so multiple fills in one market
    count as one entry.  If whale B's first entry is within _FOLLOW_WINDOW of
    whale A's first entry, and A has a better leaderboard rank, count one
    "follow event" for the (A→B) pair.

    Pairs with >= _MIN_FOLLOW_INSTANCES follow events are flagged.

    Returns:
      pairs       — [{leader, leader_rank, follower, follower_rank, markets, instances}]
      whale_types — [{name, rank, classification, led_count, followed_count}]
    """
    # Build per-(market, outcome) first-entry timestamp per whale
    # first_entries[(market, outcome)][whale] = (rank, min_ts)
    first_entries: dict = defaultdict(dict)

    for p in profiles:
        if p.get("rank", 99) > 30:
            continue
        name = p["name"]
        rank = p["rank"]
        for trade in p.get("recent_trades", []):
            if trade.get("side", "").upper() != "BUY":
                continue
            market  = trade.get("market", "")
            outcome = trade.get("outcome", "").upper()
            ts      = int(trade.get("timestamp", 0) or 0)
            amount  = float(trade.get("amount", 0) or 0)
            if not market or ts == 0 or amount < 50:
                continue
            key = (market, outcome)
            if name not in first_entries[key] or ts < first_entries[key][name][1]:
                first_entries[key][name] = (rank, ts)

    # Count follow events per (leader, follower) pair
    follow_counts:  dict = defaultdict(int)   # (leader, follower) → market count
    follow_markets: dict = defaultdict(set)   # (leader, follower) → {market names}

    for (market, outcome), whale_map in first_entries.items():
        if len(whale_map) < 2:
            continue
        # Sort by entry timestamp
        sorted_entries = sorted(whale_map.items(), key=lambda x: x[1][1])  # (name, (rank, ts))
        for i, (later_name, (later_rank, later_ts)) in enumerate(sorted_entries):
            for earlier_name, (earlier_rank, earlier_ts) in sorted_entries[:i]:
                diff = later_ts - earlier_ts
                if diff < 0 or diff > _FOLLOW_WINDOW:
                    continue
                # Only flag when the earlier whale has a better (lower) rank
                if earlier_rank >= later_rank:
                    continue
                pair_key = (earlier_name, later_name)
                follow_counts[pair_key]  += 1
                follow_markets[pair_key].add(market)

    # Build flagged pairs
    pairs = []
    for (leader, follower), count in follow_counts.items():
        if count < _MIN_FOLLOW_INSTANCES:
            continue
        leader_rank   = next((p["rank"] for p in profiles if p["name"] == leader),   99)
        follower_rank = next((p["rank"] for p in profiles if p["name"] == follower), 99)
        markets       = sorted(follow_markets[(leader, follower)])
        pairs.append({
            "leader":        leader,
            "leader_rank":   leader_rank,
            "follower":      follower,
            "follower_rank": follower_rank,
            "instances":     count,
            "markets":       markets[:3],
        })

    pairs.sort(key=lambda x: -x["instances"])

    # Classify each whale
    led_count:      dict = defaultdict(int)
    followed_count: dict = defaultdict(int)
    for pair in pairs:
        led_count[pair["leader"]]       += pair["instances"]
        followed_count[pair["follower"]] += pair["instances"]

    whale_types = []
    for p in profiles:
        if p.get("rank", 99) > 30:
            continue
        name = p["name"]
        rank = p["rank"]
        led  = led_count.get(name, 0)
        fol  = followed_count.get(name, 0)
        if led == 0 and fol == 0:
            classification = "independent"
        elif fol == 0:
            classification = "original"    # others follow them, they don't follow anyone
        elif led == 0:
            classification = "follower"    # copies others, no one copies them
        else:
            classification = "mixed"       # both leads and follows
        whale_types.append({
            "name":           name,
            "rank":           rank,
            "classification": classification,
            "led_count":      led,
            "followed_count": fol,
        })

    whale_types.sort(key=lambda x: x["rank"])

    return {"pairs": pairs[:10], "whale_types": whale_types}


# ── Core analysis (always runs) ───────────────────────────────────────────────

def _stance_map(profiles: list[dict]) -> dict:
    """market → {YES: [...], NO: [...], total_usdc: float}"""
    stances = defaultdict(lambda: {"YES": [], "NO": [], "total_usdc": 0.0})
    for p in profiles:
        for pos in p["open_positions"]:
            m    = pos["market"]
            side = pos["outcome"].upper()
            if side not in ("YES", "NO"):
                continue
            stances[m][side].append({
                "whale":     p["name"],
                "rank":      p["rank"],
                "pnl_30d":   p.get("pnl_30d", 0),
                "invested":  pos["initial_usdc"],
                "cur_price": pos["cur_price"],
                "cash_pnl":  pos["cash_pnl"],
            })
            stances[m]["total_usdc"] += pos["initial_usdc"]
    return stances


def build_report(profiles: list[dict]) -> dict:
    """
    Returns a structured report dict — no external calls.
    {
      consensus: [{market, side, whales, total_usdc}],
      contradictions: [{market, yes_whales, no_whales, total_usdc}],
      top_markets: [{market, total_usdc, whale_count}],
      whale_styles: [{name, rank, style, notes}],
      signals: [{market, side, confidence, rationale}],
    }
    """
    stances = _stance_map(profiles)

    # Consensus: ≥2 whales same side
    consensus = []
    for market, data in sorted(stances.items(), key=lambda x: x[1]["total_usdc"], reverse=True):
        for side in ("YES", "NO"):
            if len(data[side]) >= 2:
                consensus.append({
                    "market":     market,
                    "side":       side,
                    "whales":     data[side],
                    "total_usdc": round(data["total_usdc"], 0),
                    "avg_price":  round(
                        sum(w["cur_price"] for w in data[side]) / len(data[side]), 3
                    ) if data[side] else 0,
                })

    # Contradictions: both sides have at least 1 whale
    contradictions = []
    for market, data in sorted(stances.items(), key=lambda x: x[1]["total_usdc"], reverse=True):
        if data["YES"] and data["NO"]:
            contradictions.append({
                "market":     market,
                "yes_whales": data["YES"],
                "no_whales":  data["NO"],
                "total_usdc": round(data["total_usdc"], 0),
            })

    # Top markets by whale capital
    top_markets = sorted(
        [
            {
                "market":      m,
                "total_usdc":  round(d["total_usdc"], 0),
                "whale_count": len(d["YES"]) + len(d["NO"]),
                "yes_count":   len(d["YES"]),
                "no_count":    len(d["NO"]),
            }
            for m, d in stances.items()
        ],
        key=lambda x: x["total_usdc"],
        reverse=True,
    )[:15]

    # Simple style heuristics per whale
    whale_styles = []
    for p in profiles:
        pos = p["open_positions"]
        if not pos:
            whale_styles.append({"name": p["name"], "rank": p["rank"], "style": "Unknown", "notes": "No open positions"})
            continue

        yes_count    = sum(1 for x in pos if x["outcome"].upper() == "YES")
        no_count     = sum(1 for x in pos if x["outcome"].upper() == "NO")
        avg_price    = sum(x["cur_price"] for x in pos) / len(pos) if pos else 0
        trader_label = p.get("trader_label", "")
        win_rate     = p.get("win_rate")
        pf           = p.get("profit_factor")
        resolved     = p.get("resolved_count", 0)

        # Prefer data-driven label, fall back to position heuristics
        if trader_label and trader_label not in ("Insufficient data", ""):
            style = trader_label
        elif no_count > yes_count * 2:
            style = "Contrarian / Skeptic"
        elif yes_count > no_count * 2:
            style = "Optimist / Momentum"
        elif avg_price < 0.25:
            style = "Long-shot Hunter"
        elif avg_price > 0.75:
            style = "Favourite Player"
        elif len(pos) >= 8:
            style = "Diversifier"
        else:
            style = "Balanced"

        if resolved > 0:
            note = f"{resolved} pending winning bets (unredeemed)"
        else:
            note = f"{yes_count} YES / {no_count} NO across {len(pos)} open markets"

        pnl_30d = p.get("pnl_30d", 0)
        pnl_tag = f" | 30d P&L ${pnl_30d:+,.0f}"
        whale_styles.append({
            "name":           p["name"],
            "rank":           p["rank"],
            "style":          style,
            "notes":          note + pnl_tag,
            "win_rate":       win_rate,
            "profit_factor":  pf,
            "resolved_count": resolved,
            "exit_style":     p.get("exit_style", ""),
        })

    # Signals: consensus markets with meaningful capital
    signals = []
    for c in consensus[:8]:
        invested = sum(w["invested"] for w in c["whales"])
        n        = len(c["whales"])
        price    = c["avg_price"]

        if n >= 4 and invested > 50_000:
            confidence = "High"
        elif n >= 3 or invested > 20_000:
            confidence = "Medium"
        else:
            confidence = "Low"

        rationale = (
            f"{n} whale{'s' if n>1 else ''} on {c['side']} "
            f"(${invested:,.0f} total, avg price {price:.2f})"
        )
        signals.append({
            "market":     c["market"],
            "side":       c["side"],
            "confidence": confidence,
            "rationale":  rationale,
        })

    cat_analysis  = _analyze_categories(profiles)
    copy_analysis = _detect_copy_traders(profiles)

    # Enrich whale_styles with primary category + copy-trade classification
    whale_focus = cat_analysis["whale_focus"]
    wtype_map   = {wt["name"]: wt for wt in copy_analysis["whale_types"]}
    for ws in whale_styles:
        focus = whale_focus.get(ws["name"], {})
        ws["primary_category"]    = focus.get("primary", "—")
        ws["category_pct"]        = focus.get("pct", 0)
        ws["category_breakdown"]  = focus.get("breakdown", {})
        wt = wtype_map.get(ws["name"], {})
        ws["trader_independence"] = wt.get("classification", "independent")
        ws["led_count"]           = wt.get("led_count", 0)
        ws["followed_count"]      = wt.get("followed_count", 0)

    return {
        "generated_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        "whale_count":      len(profiles),
        "consensus":        consensus[:10],
        "contradictions":   contradictions[:8],
        "top_markets":      top_markets,
        "whale_styles":     whale_styles,
        "signals":          signals,
        "category_analysis": cat_analysis,
        "copy_analysis":     copy_analysis,
    }


# ── Optional Claude enhancement ───────────────────────────────────────────────

def enhance_with_claude(report: dict, profiles: list[dict]) -> str | None:
    """
    If ANTHROPIC_API_KEY is set, returns a Claude-written markdown commentary.
    Otherwise returns None.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "your_anthropic_key_here":
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        payload = {
            "consensus_markets":      report["consensus"][:6],
            "contradiction_markets":  report["contradictions"][:6],
            "top_signals":            report["signals"][:5],
            "whale_styles":           report["whale_styles"][:10],
        }

        msg = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1500,
            system=(
                "You are a Polymarket intelligence analyst. "
                "Given structured whale data, write a sharp 3-paragraph commentary: "
                "(1) what the smart money consensus means for these markets, "
                "(2) what the contradictions reveal about uncertainty, "
                "(3) your overall read on investment implications. "
                "Be concise and analytical. No headers needed."
            ),
            messages=[{
                "role": "user",
                "content": f"```json\n{json.dumps(payload, indent=2)}\n```"
            }],
        )
        return msg.content[0].text
    except Exception as e:
        return f"(Claude commentary unavailable: {e})"


def analyze_whales(profiles: list[dict]) -> dict:
    """Main entry point. Returns report dict + optional ai_commentary string."""
    report = build_report(profiles)
    report["ai_commentary"] = enhance_with_claude(report, profiles)
    return report
