#!/usr/bin/env python3
"""Paper-only all-location Kalshi daily-high scanner. No auth or order code.\nPublic runner; contains no credentials."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOSTS = [
    "https://external-api.kalshi.com/trade-api/v2",
    "https://api.elections.kalshi.com/trade-api/v2",
]
QTY = 1.0
FEE_RATE = 0.02
LEG_BUFFER = 0.005
MIN_PROFIT = 0.01
OUT = Path("automation/kalshi-weather/data")
OUT.mkdir(exist_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def get(path):
    last = None
    for attempt in range(8):
        host = HOSTS[attempt % len(HOSTS)]
        req = urllib.request.Request(
            host + path,
            headers={"User-Agent": "KalshiWeatherPaperMonitor/1.0", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except Exception as exc:
            last = exc
            status = getattr(exc, "code", 0)
            if status and status != 429 and status < 500:
                raise
            time.sleep(min(0.75 * (2 ** (attempt // 2)), 8))
    raise RuntimeError(f"Kalshi unavailable after retries: {last}")


def is_daily_high(m):
    ticker = str(m.get("series_ticker") or m.get("ticker") or "").upper()
    text = " ".join(str(m.get(k) or "") for k in ("title", "subtitle", "yes_sub_title")).lower()
    return (ticker.startswith("KXHIGH") or ticker.startswith("KXHIGHT")) and any(
        marker in text for marker in ("temperature", "°", " degrees", "or below", "or above", " to ")
    )


def discover():
    markets, cursor, pages = [], "", 0
    while True:
        params = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        payload = get("/markets?" + urllib.parse.urlencode(params))
        pages += 1
        markets.extend(m for m in payload.get("markets", []) if is_daily_high(m))
        cursor = payload.get("cursor") or ""
        if not cursor:
            return markets, pages
        if pages > 100:
            raise RuntimeError("Pagination safety limit exceeded")


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def top_ask(m):
    ask, size = number(m.get("yes_ask_dollars")), number(m.get("yes_ask_size_fp"))
    if ask is None or ask <= 0:
        return None
    return {"ask": ask, "size": size or 0.0}


def no_bids(book):
    root = book.get("orderbook_fp") or book.get("orderbook") or book
    raw = root.get("no_dollars") or root.get("no") or []
    levels = []
    for level in raw:
        if isinstance(level, dict):
            price = number(level.get("price_dollars", level.get("price")))
            qty = number(level.get("quantity_fp", level.get("quantity")))
        else:
            price, qty = number(level[0]), number(level[1])
        if price is not None and price > 1:
            price /= 100
        if price is not None and qty is not None:
            levels.append((price, qty))
    return sorted(levels, reverse=True)


def executable_yes(book, quantity=QTY):
    remaining, cost, fills = quantity, 0.0, []
    for no_price, available in no_bids(book):
        take = min(remaining, available)
        if take <= 0:
            continue
        yes_ask = 1 - no_price
        cost += take * yes_ask
        fills.append({"yes_ask": round(yes_ask, 4), "quantity": round(take, 2)})
        remaining -= take
        if remaining <= 1e-9:
            return {"cost": cost, "fills": fills}
    return None


def series_meta(ticker):
    try:
        payload = get("/series/" + urllib.parse.quote(ticker, safe=""))
        return payload.get("series") or payload
    except Exception as exc:
        return {"metadata_error": str(exc)}


def location(meta, fallback):
    title = str(meta.get("title") or "").strip()
    if title:
        lowered = title.lower()
        for marker in (" in ", " at "):
            if marker in lowered:
                value = title[lowered.index(marker) + len(marker):].rstrip("?")
                return value.replace(" today", "").strip()
        return title
    return fallback


def scan():
    started = now()
    markets, pages = discover()
    groups = {}
    for m in markets:
        groups.setdefault(m.get("event_ticker") or m["ticker"], []).append(m)

    series = {t: series_meta(t) for t in sorted({m.get("series_ticker") for m in markets if m.get("series_ticker")})}
    events, signals, failures = [], [], []

    for event_ticker, members in sorted(groups.items()):
        series_ticker = members[0].get("series_ticker") or ""
        meta = series.get(series_ticker, {})
        place = location(meta, series_ticker)
        legs = [{"ticker": m["ticker"], "bracket": m.get("title") or m.get("subtitle"), "top": top_ask(m)} for m in members]
        complete = all(leg["top"] for leg in legs)
        top_cost = sum(leg["top"]["ask"] for leg in legs) if complete else None
        optimistic = 1 - top_cost - top_cost * FEE_RATE - len(legs) * LEG_BUFFER if top_cost is not None else None
        status, execution = "ruled_out_at_top_of_book", None

        if optimistic is not None and optimistic >= MIN_PROFIT:
            try:
                books = [get("/markets/" + urllib.parse.quote(m["ticker"], safe="") + "/orderbook") for m in members]
                filled = [executable_yes(book) for book in books]
                if not all(filled):
                    status = "rejected_insufficient_visible_depth"
                else:
                    gross = sum(fill["cost"] for fill in filled)
                    fees = gross * FEE_RATE
                    buffer = len(members) * LEG_BUFFER
                    net = 1 - gross - fees - buffer
                    execution = {
                        "quantity": QTY,
                        "gross_cost": round(gross, 4),
                        "estimated_fees": round(fees, 4),
                        "safety_buffer": round(buffer, 4),
                        "expected_net_profit": round(net, 4),
                        "legs": [
                            {
                                "ticker": m["ticker"],
                                "bracket": m.get("title") or m.get("subtitle"),
                                "fills": fill["fills"],
                                "cost": round(fill["cost"], 4),
                            }
                            for m, fill in zip(members, filled)
                        ],
                    }
                    if net >= MIN_PROFIT:
                        status = "qualifying_paper_signal"
                        signals.append({"observed_at": now(), "location": place, "event_ticker": event_ticker, **execution})
                    else:
                        status = "rejected_after_depth_fees_buffer"
            except Exception as exc:
                status = "coverage_failure"
                failures.append({"event_ticker": event_ticker, "location": place, "error": str(exc)})
        elif top_cost is None:
            status = "no_complete_executable_top_book"

        events.append({
            "location": place,
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "bracket_count": len(members),
            "settlement_sources": meta.get("settlement_sources") or [],
            "rules_captured": all(m.get("rules_primary") or m.get("rules_secondary") for m in members),
            "top_basket_cost": round(top_cost, 4) if top_cost is not None else None,
            "optimistic_net_after_fees_buffer": round(optimistic, 4) if optimistic is not None else None,
            "status": status,
            "execution": execution,
        })

    failures.extend(
        {"series_ticker": ticker, "error": meta["metadata_error"]}
        for ticker, meta in series.items() if meta.get("metadata_error")
    )
    locations = sorted({e["location"] for e in events})
    return {
        "mode": "paper_only",
        "live_trading": False,
        "scanned_at": started,
        "completed_at": now(),
        "discovery": {
            "pagination_complete": True,
            "market_pages": pages,
            "open_temperature_markets": len(markets),
            "events": len(events),
            "locations_count": len(locations),
            "locations": locations,
        },
        "methodology": {
            "basket_quantity": QTY,
            "conservative_fee_rate": FEE_RATE,
            "per_leg_slippage_buffer": LEG_BUFFER,
            "minimum_net_profit": MIN_PROFIT,
            "note": "Full depth is fetched for candidates; baskets already too expensive at top-of-book cannot improve at deeper prices.",
        },
        "qualifying_signals": signals,
        "coverage_failures": failures,
        "events": events,
    }


def main():
    try:
        result = scan()
    except Exception as exc:
        result = {
            "mode": "paper_only",
            "live_trading": False,
            "completed_at": now(),
            "fatal_error": str(exc),
            "discovery": {"pagination_complete": False},
        }
        (OUT / "latest.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        raise SystemExit(1)

    (OUT / "latest.json").write_text(json.dumps(result, indent=2) + "\n")
    if result["qualifying_signals"]:
        with (OUT / "signals.jsonl").open("a") as fh:
            for signal in result["qualifying_signals"]:
                fh.write(json.dumps(signal, sort_keys=True) + "\n")
    summary = {
        "completed_at": result["completed_at"],
        **result["discovery"],
        "signals": len(result["qualifying_signals"]),
        "coverage_failures": len(result["coverage_failures"]),
    }
    print(json.dumps(summary, indent=2))
    if result["coverage_failures"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
