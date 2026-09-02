#!/usr/bin/env python3
"""Paper-only all-location Kalshi daily-high scanner. No auth or order code.\nPublic runner; contains no credentials."""
from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOSTS = [
    "https://external-api.kalshi.com/trade-api/v2",
    "https://api.elections.kalshi.com/trade-api/v2",
]
QTY = 1.0
FEE_RATE = 0.02
LEG_BUFFER = 0.005
MIN_PROFIT = 0.01
MODEL_RESERVE = 0.05
MAX_HOURS_TO_CLOSE = 12
WEATHER_SOURCE_TOLERANCE_F = 1
OUT = Path("automation/kalshi-weather/data")
OUT.mkdir(exist_ok=True)
SIGNALS_FILE = OUT / "signals.jsonl"
SNAPSHOTS_FILE = OUT / "observation_snapshots.jsonl"
RECONCILIATIONS_FILE = OUT / "reconciliations.jsonl"


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def external_json(url, attempts=4):
    """Read a public weather endpoint. This scanner never sends credentials."""
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "KalshiWeatherPaperMonitor/2.0 research@example.com", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except Exception as exc:
            last = exc
            time.sleep(min(1.0 * (2 ** attempt), 8))
    raise RuntimeError(f"Public weather endpoint unavailable: {last}")


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


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


def is_daily_high_series(meta):
    """Identify the recurring daily-high product from live series metadata, not a city allowlist."""
    ticker = str(meta.get("ticker") or "").upper()
    title = str(meta.get("title") or "").lower()
    frequency = str(meta.get("frequency") or "").lower()
    high_words = ("highest temperature", "high temperature", "daily high", "temperature high")
    not_hourly = "hour" not in frequency and "hourly" not in title
    return not_hourly and (any(word in title for word in high_words) or ticker.startswith(("KXHIGH", "KXHIGHT")))


def discover():
    # Enumerate the current Weather catalog first.  This avoids walking tens of
    # thousands of unrelated open markets and automatically catches new cities
    # and renamed daily-high series.
    catalog = get("/series?" + urllib.parse.urlencode({
        "category": "Climate and Weather",
        "include_product_metadata": "true",
    }))
    weather_series = catalog.get("series") or []
    selected = {s["ticker"]: s for s in weather_series if s.get("ticker") and is_daily_high_series(s)}
    if not selected:
        raise RuntimeError("Weather series catalog returned no daily-high series")

    markets, pages = [], 0
    series_pages = {}
    for ticker in sorted(selected):
        cursor = ""
        seen_cursors = set()
        count = 0
        while True:
            params = {
                "series_ticker": ticker,
                "status": "open",
                "limit": 1000,
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor
            payload = get("/markets?" + urllib.parse.urlencode(params))
            pages += 1
            count += 1
            markets.extend(
                {**m, "series_ticker": m.get("series_ticker") or ticker}
                for m in payload.get("markets", [])
                if is_daily_high({**m, "series_ticker": m.get("series_ticker") or ticker})
            )
            next_cursor = payload.get("cursor") or ""
            if not next_cursor:
                break
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise RuntimeError(f"Repeated pagination cursor for {ticker}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if count > 100:
                raise RuntimeError(f"Pagination safety limit exceeded for {ticker}")
        series_pages[ticker] = count

    active = {m.get("series_ticker") for m in markets}
    active_meta = {ticker: selected[ticker] for ticker in active if ticker in selected}
    return markets, pages, active_meta, len(weather_series), len(selected), series_pages


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


def bids(book, side):
    root = book.get("orderbook_fp") or book.get("orderbook") or book
    raw = root.get(side + "_dollars") or root.get(side) or []
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
    for no_price, available in bids(book, "no"):
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


def executable_no(book, quantity=QTY):
    remaining, cost, fills = quantity, 0.0, []
    for yes_price, available in bids(book, "yes"):
        take = min(remaining, available)
        if take <= 0:
            continue
        no_ask = 1 - yes_price
        cost += take * no_ask
        fills.append({"no_ask": round(no_ask, 4), "quantity": round(take, 2)})
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


def settlement_station(members):
    """Translate the settlement rule's dynamic CLI code to its US ICAO station."""
    for market in members:
        rules = " ".join(str(market.get(k) or "") for k in ("rules_primary", "rules_secondary"))
        match = re.search(r"\(CLI([A-Z0-9]{3})\)", rules.upper())
        if match:
            return "K" + match.group(1)
    return None


def settlement_location(members, meta, fallback):
    """Prefer the binding rule's location over occasionally stale series display metadata."""
    for market in members:
        rules = str(market.get("rules_primary") or "")
        match = re.search(r"recorded at (.+?) \(CLI[A-Z0-9]+\)", rules, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return location(meta, fallback)


def weather_observations(stations):
    if not stations:
        return {}
    params = urllib.parse.urlencode({"ids": ",".join(sorted(stations)), "format": "json", "hours": 24})
    rows = external_json("https://aviationweather.gov/api/data/metar?" + params)
    grouped = {station: [] for station in stations}
    for row in rows if isinstance(rows, list) else []:
        station = row.get("icaoId")
        if station in grouped:
            grouped[station].append(row)
    return grouped


def station_state(rows, close_time):
    """Build an observation state for the contract's exact 24-hour weather day."""
    start = close_time - timedelta(hours=24)
    usable = []
    for row in rows:
        observed = datetime.fromtimestamp(number(row.get("obsTime")) or 0, timezone.utc)
        if start <= observed <= datetime.now(timezone.utc) + timedelta(minutes=15):
            usable.append((observed, row))
    usable.sort(key=lambda item: item[0])
    if not usable:
        return None
    temperatures = []
    six_hour_maxima = []
    for _, row in usable:
        temp_c = number(row.get("temp"))
        max_c = number(row.get("maxT"))
        if temp_c is not None:
            temperatures.append(temp_c * 9 / 5 + 32)
        if max_c is not None:
            six_hour_maxima.append(max_c * 9 / 5 + 32)
    if not temperatures:
        return None
    current_f = temperatures[-1]
    earlier = temperatures[max(0, len(temperatures) - 4)]
    return {
        "observation_count": len(usable),
        "current_temp_f": round(current_f, 2),
        "observed_high_f": round(max(temperatures + six_hour_maxima), 2),
        "six_hour_max_f": round(max(six_hour_maxima), 2) if six_hour_maxima else None,
        "three_report_trend_f": round(current_f - earlier, 2),
        "last_observation_at": usable[-1][0].isoformat(),
        "latitude": number(usable[-1][1].get("lat")),
        "longitude": number(usable[-1][1].get("lon")),
        "raw_observation": usable[-1][1].get("rawOb"),
    }


def nws_remaining_max(state, close_time):
    lat, lon = state.get("latitude"), state.get("longitude")
    if lat is None or lon is None:
        return None
    point = external_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
    hourly_url = ((point.get("properties") or {}).get("forecastHourly"))
    if not hourly_url:
        return None
    source = "nws_hourly"
    try:
        forecast = external_json(hourly_url)
    except Exception:
        # Individual NWS hourly grid endpoints occasionally return persistent
        # 500s while the official standard-period endpoint remains healthy.
        forecast = external_json(hourly_url.removesuffix("/hourly"))
        source = "nws_standard_period_fallback"
    values = []
    current = datetime.now(timezone.utc)
    for period in ((forecast.get("properties") or {}).get("periods") or []):
        when = parse_time(period.get("startTime"))
        end = parse_time(period.get("endTime")) or when
        value = number(period.get("temperature"))
        unit = str(period.get("temperatureUnit") or "F").upper()
        if when and end and value is not None and when <= close_time and end >= current:
            if unit == "C":
                value = value * 9 / 5 + 32
            values.append(value)
    return (max(values), source) if values else (None, source)


def discrete_temperature_distribution(mean_f, sigma_f, observed_high_f):
    minimum = math.floor(observed_high_f - WEATHER_SOURCE_TOLERANCE_F)
    weights = {}
    for value in range(-40, 141):
        if value < minimum:
            continue
        weight = math.exp(-0.5 * ((value - mean_f) / sigma_f) ** 2)
        weights[value] = weight
    total = sum(weights.values())
    return {value: weight / total for value, weight in weights.items()} if total else {}


def bracket_probability(market, distribution):
    kind = market.get("strike_type")
    floor = number(market.get("floor_strike"))
    cap = number(market.get("cap_strike"))
    probability = 0.0
    for value, weight in distribution.items():
        wins = False
        if kind == "less" and cap is not None:
            wins = value < cap
        elif kind == "greater" and floor is not None:
            wins = value > floor
        elif kind == "between" and floor is not None and cap is not None:
            wins = floor <= value <= cap
        if wins:
            probability += weight
    return probability


def observation_signal(event_ticker, members, place, station, state, close_time, existing_keys):
    hours_to_close = (close_time - datetime.now(timezone.utc)).total_seconds() / 3600
    forecast_max, forecast_source = nws_remaining_max(state, close_time)
    if forecast_max is None:
        return [], {"status": "weather_forecast_unavailable", "hours_to_close": round(hours_to_close, 2)}
    mean = max(state["observed_high_f"], forecast_max)
    sigma = 1.35 if hours_to_close <= 6 and state.get("six_hour_max_f") is not None else 1.9
    distribution = discrete_temperature_distribution(mean, sigma, state["observed_high_f"])
    candidates = []
    for market in members:
        probability = bracket_probability(market, distribution)
        yes_ask = number(market.get("yes_ask_dollars"))
        no_ask = number(market.get("no_ask_dollars"))
        choices = []
        if yes_ask is not None and yes_ask > 0:
            choices.append(("yes", probability, yes_ask))
        if no_ask is not None and no_ask > 0:
            choices.append(("no", 1 - probability, no_ask))
        for side, win_probability, displayed_ask in choices:
            optimistic = win_probability - displayed_ask - displayed_ask * FEE_RATE - LEG_BUFFER - MODEL_RESERVE
            if optimistic < MIN_PROFIT or win_probability < 0.70:
                continue
            key = f"late_day_observation_model|{market['ticker']}|{side}"
            if key in existing_keys:
                continue
            book = get("/markets/" + urllib.parse.quote(market["ticker"], safe="") + "/orderbook")
            fill = executable_yes(book) if side == "yes" else executable_no(book)
            if not fill:
                continue
            cost = fill["cost"]
            fees = cost * FEE_RATE
            net = win_probability - cost - fees - LEG_BUFFER - MODEL_RESERVE
            if net < MIN_PROFIT:
                continue
            candidates.append({
                "signal_key": key,
                "strategy": "late_day_observation_model",
                "model_status": "experimental_forward_test",
                "observed_at": now(),
                "location": place,
                "station": station,
                "event_ticker": event_ticker,
                "ticker": market["ticker"],
                "bracket": market.get("title") or market.get("yes_sub_title"),
                "side": side,
                "quantity": QTY,
                "model_probability": round(win_probability, 4),
                "model_mean_f": round(mean, 2),
                "model_sigma_f": sigma,
                "observed_high_f": state["observed_high_f"],
                "six_hour_max_f": state.get("six_hour_max_f"),
                "remaining_nws_max_f": round(forecast_max, 2),
                "remaining_nws_source": forecast_source,
                "hours_to_close": round(hours_to_close, 2),
                "gross_cost": round(cost, 4),
                "estimated_fees": round(fees, 4),
                "slippage_reserve": LEG_BUFFER,
                "model_uncertainty_reserve": MODEL_RESERVE,
                "expected_net_profit": round(net, 4),
                "fills": fill["fills"],
                "close_time": close_time.isoformat(),
            })
    snapshot = {
        "observed_at": now(),
        "strategy": "late_day_observation_model",
        "model_status": "experimental_forward_test",
        "location": place,
        "station": station,
        "event_ticker": event_ticker,
        "hours_to_close": round(hours_to_close, 2),
        "model_mean_f": round(mean, 2),
        "model_sigma_f": sigma,
        "remaining_nws_max_f": round(forecast_max, 2),
        "remaining_nws_source": forecast_source,
        **state,
    }
    return candidates, snapshot


def reconcile_signals(signals, existing_reconciliations):
    reconciled_keys = {row.get("signal_key") for row in existing_reconciliations}
    new_rows = []
    for signal in signals:
        key = signal.get("signal_key")
        ticker = signal.get("ticker")
        close_time = parse_time(signal.get("close_time"))
        if not key or not ticker or key in reconciled_keys or not close_time:
            continue
        if datetime.now(timezone.utc) < close_time + timedelta(hours=6):
            continue
        payload = get("/markets/" + urllib.parse.quote(ticker, safe=""))
        market = payload.get("market") or payload
        result = str(market.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        won = result == signal.get("side")
        total_cost = signal["gross_cost"] + signal["estimated_fees"] + signal.get("slippage_reserve", 0)
        new_rows.append({
            "signal_key": key,
            "reconciled_at": now(),
            "strategy": signal.get("strategy"),
            "event_ticker": signal.get("event_ticker"),
            "ticker": ticker,
            "side": signal.get("side"),
            "result": result,
            "won": won,
            "paper_pnl": round((1.0 if won else 0.0) - total_cost, 4),
        })
    return new_rows


def scan():
    started = now()
    markets, pages, series, weather_series_count, daily_high_series_count, series_pages = discover()
    groups = {}
    for m in markets:
        groups.setdefault(m.get("event_ticker") or m["ticker"], []).append(m)

    historical_signals = read_jsonl(SIGNALS_FILE)
    existing_keys = {row.get("signal_key") for row in historical_signals if row.get("signal_key")}
    events, signals, failures, observation_failures, snapshots = [], [], [], [], []
    eligible = {}
    stations = set()
    current = datetime.now(timezone.utc)
    for event_ticker, members in groups.items():
        close_time = parse_time(members[0].get("close_time"))
        station = settlement_station(members)
        if not close_time or not station:
            continue
        hours_to_close = (close_time - current).total_seconds() / 3600
        if 0 < hours_to_close <= MAX_HOURS_TO_CLOSE:
            eligible[event_ticker] = (station, close_time)
            stations.add(station)
    observations = {}
    if stations:
        try:
            observations = weather_observations(stations)
        except Exception as exc:
            observation_failures.append({"scope": "all_eligible_stations", "error": str(exc)})

    for event_ticker, members in sorted(groups.items()):
        series_ticker = members[0].get("series_ticker") or ""
        meta = series.get(series_ticker, {})
        place = settlement_location(members, meta, series_ticker)
        legs = [{"ticker": m["ticker"], "bracket": m.get("title") or m.get("subtitle"), "top": top_ask(m)} for m in members]
        complete = all(leg["top"] for leg in legs)
        top_cost = sum(leg["top"]["ask"] for leg in legs) if complete else None
        optimistic = 1 - top_cost - top_cost * FEE_RATE - len(legs) * LEG_BUFFER if top_cost is not None else None
        status, execution = "ruled_out_at_top_of_book", None
        observation_monitoring = {"status": "outside_late_day_window"}

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
                        key = f"complete_bracket_basket|{event_ticker}"
                        if key not in existing_keys:
                            signals.append({
                                "signal_key": key,
                                "strategy": "complete_bracket_basket",
                                "observed_at": now(),
                                "location": place,
                                "event_ticker": event_ticker,
                                **execution,
                            })
                    else:
                        status = "rejected_after_depth_fees_buffer"
            except Exception as exc:
                status = "coverage_failure"
                failures.append({"event_ticker": event_ticker, "location": place, "error": str(exc)})
        elif top_cost is None:
            status = "no_complete_executable_top_book"

        if event_ticker in eligible:
            station, close_time = eligible[event_ticker]
            state = station_state(observations.get(station, []), close_time)
            if state is None:
                observation_monitoring = {"status": "observation_data_unavailable", "station": station}
                observation_failures.append({
                    "event_ticker": event_ticker,
                    "location": place,
                    "station": station,
                    "error": "No usable METAR observations for the contract weather day",
                })
            else:
                try:
                    candidates, snapshot = observation_signal(
                        event_ticker, members, place, station, state, close_time, existing_keys
                    )
                    signals.extend(candidates)
                    snapshots.append(snapshot)
                    snapshot_status = snapshot.get("status")
                    observation_monitoring = {
                        "status": snapshot_status or "evaluated",
                        "station": station,
                        "observed_high_f": state["observed_high_f"],
                        "six_hour_max_f": state.get("six_hour_max_f"),
                        "new_signals": len(candidates),
                    }
                    if snapshot_status:
                        observation_failures.append({
                            "event_ticker": event_ticker,
                            "location": place,
                            "station": station,
                            "error": snapshot_status,
                        })
                except Exception as exc:
                    observation_monitoring = {"status": "evaluation_failure", "station": station, "error": str(exc)}
                    observation_failures.append({
                        "event_ticker": event_ticker,
                        "location": place,
                        "station": station,
                        "error": str(exc),
                    })

        events.append({
            "location": place,
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "settlement_station": settlement_station(members),
            "bracket_count": len(members),
            "settlement_sources": meta.get("settlement_sources") or [],
            "rules_captured": all(m.get("rules_primary") or m.get("rules_secondary") for m in members),
            "top_basket_cost": round(top_cost, 4) if top_cost is not None else None,
            "optimistic_net_after_fees_buffer": round(optimistic, 4) if optimistic is not None else None,
            "status": status,
            "execution": execution,
            "observation_monitoring": observation_monitoring,
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
            "weather_series_in_catalog": weather_series_count,
            "daily_high_series_discovered": daily_high_series_count,
            "series_pages": series_pages,
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
            "model_uncertainty_reserve": MODEL_RESERVE,
            "observation_window_hours_to_close": MAX_HOURS_TO_CLOSE,
            "weather_source_tolerance_f": WEATHER_SOURCE_TOLERANCE_F,
            "note": "Basket arbitrage and the experimental late-day observation model are tracked separately. The observation model is a forward test, not a proven edge.",
        },
        "qualifying_signals": signals,
        "coverage_failures": failures,
        "observation_edge": {
            "status": "experimental_forward_test",
            "eligible_events": len(eligible),
            "stations_requested": sorted(stations),
            "events_evaluated": len(snapshots),
            "new_signals": len([s for s in signals if s.get("strategy") == "late_day_observation_model"]),
            "data_failures": observation_failures,
            "snapshots": snapshots,
        },
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

    existing_signals = read_jsonl(SIGNALS_FILE)
    existing_keys = {row.get("signal_key") for row in existing_signals if row.get("signal_key")}
    new_signals = [row for row in result["qualifying_signals"] if row.get("signal_key") not in existing_keys]
    if new_signals:
        with SIGNALS_FILE.open("a") as fh:
            for signal in new_signals:
                fh.write(json.dumps(signal, sort_keys=True) + "\n")
    snapshots = (result.get("observation_edge") or {}).pop("snapshots", [])
    if snapshots:
        with SNAPSHOTS_FILE.open("a") as fh:
            for snapshot in snapshots:
                fh.write(json.dumps(snapshot, sort_keys=True) + "\n")
    all_signals = existing_signals + new_signals
    existing_reconciliations = read_jsonl(RECONCILIATIONS_FILE)
    new_reconciliations = reconcile_signals(all_signals, existing_reconciliations)
    if new_reconciliations:
        with RECONCILIATIONS_FILE.open("a") as fh:
            for reconciliation in new_reconciliations:
                fh.write(json.dumps(reconciliation, sort_keys=True) + "\n")
    all_reconciliations = existing_reconciliations + new_reconciliations
    result["qualifying_signals"] = new_signals
    result["settled_reconciliations"] = new_reconciliations
    result["paper_tracking"] = {
        "cumulative_signals": len(all_signals),
        "cumulative_settled": len(all_reconciliations),
        "cumulative_paper_pnl": round(sum(number(row.get("paper_pnl")) or 0 for row in all_reconciliations), 4),
        "signals_by_strategy": {
            strategy: len([row for row in all_signals if row.get("strategy") == strategy])
            for strategy in ("complete_bracket_basket", "late_day_observation_model")
        },
    }
    (OUT / "latest.json").write_text(json.dumps(result, indent=2) + "\n")
    summary = {
        "completed_at": result["completed_at"],
        **result["discovery"],
        "signals": len(result["qualifying_signals"]),
        "observation_events_evaluated": result.get("observation_edge", {}).get("events_evaluated", 0),
        "observation_data_failures": len(result.get("observation_edge", {}).get("data_failures", [])),
        "settled_reconciliations": len(new_reconciliations),
        "coverage_failures": len(result["coverage_failures"]),
    }
    print(json.dumps(summary, indent=2))
    if result["coverage_failures"]:
        raise SystemExit(2)
    if result.get("observation_edge", {}).get("data_failures"):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
