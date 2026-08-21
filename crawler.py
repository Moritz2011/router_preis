#!/usr/bin/env python3
"""Track the daily price of the FRITZ!Box 5530 Fiber."""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


PRODUCT_URL = "https://fritz.com/products/fritz-box-5530-fiber-20002960"
PRODUCT_JSON_URL = f"{PRODUCT_URL}.js"
EXPECTED_SKU = "20002960"
CURRENCY = "EUR"
TIMEZONE = ZoneInfo("Europe/Berlin")

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "price_data.json"
DASHBOARD_FILE = ROOT / "index.html"

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


class ProductDataError(RuntimeError):
    """Raised when the shop response does not contain the expected product."""


class JsonLdParser(HTMLParser):
    """Collect JSON-LD script contents without third-party HTML dependencies."""

    def __init__(self) -> None:
        super().__init__()
        self.documents: list[str] = []
        self._buffer: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag.lower() == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._buffer is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._buffer is not None:
            self.documents.append("".join(self._buffer))
            self._buffer = None


def cents_to_euros(value: Any) -> float | None:
    """Convert Shopify's integer cent value to euros."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductDataError(f"Ungueltiger Preiswert: {value!r}")
    return round(float(value) / 100, 2)


def parse_product(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the Shopify product response."""
    variants = payload.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ProductDataError("Die Produktantwort enthaelt keine Varianten.")

    matching_variants = [v for v in variants if str(v.get("sku")) == EXPECTED_SKU]
    if not matching_variants:
        raise ProductDataError(
            f"SKU {EXPECTED_SKU} wurde in der Produktantwort nicht gefunden."
        )

    available_variants = [v for v in matching_variants if v.get("available") is True]
    price_source = available_variants or matching_variants
    prices = [cents_to_euros(v.get("price")) for v in price_source]
    numeric_prices = [price for price in prices if price is not None]
    if not numeric_prices:
        raise ProductDataError("Die Produktantwort enthaelt keinen Preis.")

    compare_prices = [
        cents_to_euros(v.get("compare_at_price")) for v in price_source
    ]
    numeric_compare_prices = [price for price in compare_prices if price is not None]

    return {
        "name": str(payload.get("title") or "FRITZ!Box 5530 Fiber"),
        "sku": EXPECTED_SKU,
        "price": min(numeric_prices),
        "compare_at_price": (
            min(numeric_compare_prices) if numeric_compare_prices else None
        ),
        "available": bool(available_variants),
    }


def walk_json(value: Any):
    """Yield all dictionaries from an arbitrarily nested JSON value."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def parse_product_html(document: str) -> dict[str, Any]:
    """Extract the product from schema.org JSON-LD embedded in the HTML page."""
    parser = JsonLdParser()
    parser.feed(document)

    for raw_document in parser.documents:
        try:
            structured_data = json.loads(raw_document)
        except json.JSONDecodeError:
            continue

        for candidate in walk_json(structured_data):
            schema_type = candidate.get("@type")
            types = schema_type if isinstance(schema_type, list) else [schema_type]
            if "Product" not in types:
                continue

            offers = candidate.get("offers", [])
            if isinstance(offers, dict):
                offers = [offers]
            if not isinstance(offers, list):
                continue

            matching_offers = [
                offer
                for offer in offers
                if isinstance(offer, dict) and str(offer.get("sku")) == EXPECTED_SKU
            ]
            if not matching_offers:
                continue

            available_offers = [
                offer
                for offer in matching_offers
                if str(offer.get("availability", "")).lower().endswith("instock")
            ]
            price_source = available_offers or matching_offers
            try:
                prices = [round(float(offer["price"]), 2) for offer in price_source]
            except (KeyError, TypeError, ValueError) as exc:
                raise ProductDataError("Ungueltiger Preis im HTML-Fallback.") from exc

            currencies = {
                str(offer.get("priceCurrency", CURRENCY)).upper()
                for offer in price_source
            }
            if currencies != {CURRENCY}:
                raise ProductDataError(f"Unerwartete Waehrung im HTML: {currencies}")

            return {
                "name": str(candidate.get("name") or "FRITZ!Box 5530 Fiber"),
                "sku": EXPECTED_SKU,
                "price": min(prices),
                "compare_at_price": None,
                "available": bool(available_offers),
            }

    raise ProductDataError("Keine passenden strukturierten Produktdaten im HTML gefunden.")


def fetch_product(session: requests.Session | None = None) -> dict[str, Any]:
    """Fetch product JSON, falling back to schema.org data in the HTML page."""
    client = session or requests.Session()
    try:
        response = client.get(
            PRODUCT_JSON_URL,
            headers=HEADERS,
            timeout=(10, 30),
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise ProductDataError("Der Shop hat kein gueltiges JSON geliefert.") from exc
        if not isinstance(payload, dict):
            raise ProductDataError("Unerwartetes Format der Produktantwort.")
        return parse_product(payload)
    except (requests.RequestException, ProductDataError) as primary_error:
        print(
            f"Hinweis: Shopify-JSON nicht nutzbar ({primary_error}); nutze HTML-Fallback.",
            file=sys.stderr,
        )

    response = client.get(PRODUCT_URL, headers=HEADERS, timeout=(10, 30))
    response.raise_for_status()
    return parse_product_html(response.text)


def load_history(path: Path = DATA_FILE) -> dict[str, Any]:
    """Load existing history or initialize a new data structure."""
    if not path.exists():
        return {
            "product": {
                "name": "FRITZ!Box 5530 Fiber",
                "sku": EXPECTED_SKU,
                "url": PRODUCT_URL,
                "currency": CURRENCY,
            },
            "observations": [],
        }

    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict) or not isinstance(data.get("observations"), list):
        raise ProductDataError(f"{path.name} hat ein ungueltiges Format.")
    return data


def update_history(
    history: dict[str, Any], product: dict[str, Any], timestamp: datetime
) -> dict[str, Any]:
    """Add or replace today's observation and keep entries sorted."""
    day = timestamp.date().isoformat()
    observation = {
        "date": day,
        "timestamp": timestamp.isoformat(timespec="seconds"),
        "price": product["price"],
        "compare_at_price": product["compare_at_price"],
        "available": product["available"],
    }

    observations = [
        item for item in history.get("observations", []) if item.get("date") != day
    ]
    observations.append(observation)
    observations.sort(key=lambda item: item["date"])

    history["product"] = {
        "name": product["name"],
        "sku": product["sku"],
        "url": PRODUCT_URL,
        "currency": CURRENCY,
    }
    history["observations"] = observations
    return history


def format_eur(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def generate_dashboard(history: dict[str, Any], path: Path = DASHBOARD_FILE) -> None:
    """Generate a static dashboard suitable for GitHub Pages."""
    observations = history["observations"]
    if not observations:
        raise ProductDataError("Ohne Preisdaten kann kein Dashboard erzeugt werden.")

    current = observations[-1]
    previous = observations[-2] if len(observations) > 1 else None
    prices = [float(item["price"]) for item in observations]
    lowest = min(prices)
    highest = max(prices)
    difference = (
        round(current["price"] - previous["price"], 2) if previous else None
    )

    if difference is None:
        trend_text = "Erste Messung"
        trend_class = "neutral"
    elif difference < 0:
        trend_text = f"↓ {format_eur(abs(difference))} seit der letzten Messung"
        trend_class = "down"
    elif difference > 0:
        trend_text = f"↑ {format_eur(difference)} seit der letzten Messung"
        trend_class = "up"
    else:
        trend_text = "→ Unverändert seit der letzten Messung"
        trend_class = "neutral"

    product = history["product"]
    title = html.escape(product["name"])
    labels = json.dumps([item["date"] for item in observations], ensure_ascii=False)
    values = json.dumps(prices)
    status = "Verfügbar" if current["available"] else "Derzeit nicht verfügbar"
    status_class = "available" if current["available"] else "unavailable"
    updated = datetime.fromisoformat(current["timestamp"]).strftime("%d.%m.%Y um %H:%M Uhr")
    regular_price = current.get("compare_at_price")
    regular_price_html = (
        f'<span class="compare">statt {format_eur(regular_price)}</span>'
        if regular_price and regular_price > current["price"]
        else ""
    )

    document = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Täglicher Preisverlauf für die {title}">
  <title>{title} – Preisverlauf</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
      color: #07142d; background: #f1f6fa; }}
    header {{ padding: 42px 22px 84px; color: white;
      background: linear-gradient(135deg, #001e6e 0%, #0089d1 75%, #00beff 100%); }}
    header div, main {{ width: min(920px, 100%); margin: auto; }}
    h1 {{ margin: 0; font-size: clamp(1.8rem, 5vw, 3rem); }}
    header p {{ margin: 10px 0 0; opacity: .8; }}
    main {{ padding: 0 20px 40px; margin-top: -48px; }}
    .card {{ background: white; border-radius: 18px; padding: 26px;
      box-shadow: 0 15px 40px rgba(0, 30, 110, .12); }}
    .summary {{ display: grid; grid-template-columns: 1.4fr 1fr 1fr; gap: 24px; }}
    .label {{ color: #5f6d76; font-size: .83rem; font-weight: 650; letter-spacing: .02em; }}
    .price {{ margin-top: 5px; font-size: clamp(2.4rem, 7vw, 4rem); font-weight: 800; line-height: 1; }}
    .compare {{ display: block; margin-top: 8px; color: #5f6d76; text-decoration: line-through; }}
    .metric {{ margin-top: 8px; font-size: 1.4rem; font-weight: 750; }}
    .trend {{ display: inline-block; margin-top: 14px; padding: 7px 10px; border-radius: 8px;
      font-size: .85rem; font-weight: 700; }}
    .down {{ color: #08783e; background: #e4f8ed; }}
    .up {{ color: #b42318; background: #ffebe9; }}
    .neutral {{ color: #40516c; background: #edf2f7; }}
    .status {{ display: inline-flex; align-items: center; gap: 7px; margin-top: 16px; font-weight: 700; }}
    .status::before {{ content: ''; width: 9px; height: 9px; border-radius: 50%; background: currentColor; }}
    .available {{ color: #08783e; }} .unavailable {{ color: #b42318; }}
    .chart-card {{ margin-top: 20px; }}
    .chart-card h2 {{ margin: 0 0 22px; font-size: 1.1rem; }}
    .chart-wrap {{ height: 310px; }}
    .actions {{ display: flex; justify-content: space-between; align-items: center; gap: 16px;
      margin-top: 20px; color: #5f6d76; font-size: .82rem; }}
    a.button {{ color: #001e6e; background: #00beff; padding: 11px 16px; border-radius: 9px;
      text-decoration: none; font-weight: 750; white-space: nowrap; }}
    @media (max-width: 680px) {{ .summary {{ grid-template-columns: 1fr 1fr; }}
      .current {{ grid-column: 1 / -1; }} .actions {{ align-items: flex-start; flex-direction: column; }} }}
  </style>
</head>
<body>
  <header><div><h1>{title}</h1><p>Automatisch erfasster Preisverlauf · SKU {EXPECTED_SKU}</p></div></header>
  <main>
    <section class="card summary">
      <div class="current">
        <div class="label">AKTUELLER PREIS</div>
        <div class="price">{format_eur(current['price'])}</div>
        {regular_price_html}
        <div class="trend {trend_class}">{trend_text}</div>
        <div class="status {status_class}">{status}</div>
      </div>
      <div><div class="label">TIEFSTPREIS</div><div class="metric">{format_eur(lowest)}</div></div>
      <div><div class="label">HÖCHSTPREIS</div><div class="metric">{format_eur(highest)}</div></div>
    </section>
    <section class="card chart-card">
      <h2>Preisverlauf</h2>
      <div class="chart-wrap"><canvas id="price-chart"></canvas></div>
    </section>
    <div class="actions">
      <span>Zuletzt geprüft: {updated} · Preise ohne Gewähr</span>
      <a class="button" href="{PRODUCT_URL}" target="_blank" rel="noopener">Bei FRITZ! ansehen</a>
    </div>
  </main>
  <script>
    new Chart(document.getElementById('price-chart'), {{
      type: 'line',
      data: {{ labels: {labels}, datasets: [{{ data: {values}, borderColor: '#0089d1',
        backgroundColor: 'rgba(0, 190, 255, .12)', fill: true, tension: .25,
        pointRadius: 4, pointBackgroundColor: '#001e6e' }}] }},
      options: {{ maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }},
        interaction: {{ intersect: false, mode: 'index' }},
        scales: {{ y: {{ ticks: {{ callback: value => value.toLocaleString('de-DE',
          {{ style: 'currency', currency: 'EUR' }}) }} }} }} }}
    }});
  </script>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def save_history(history: dict[str, Any], path: Path = DATA_FILE) -> None:
    path.write_text(
        json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def run() -> None:
    now = datetime.now(TIMEZONE)
    print(f"FRITZ!-Preisabruf am {now:%d.%m.%Y um %H:%M Uhr}")
    product = fetch_product()
    history = update_history(load_history(), product, now)
    save_history(history)
    generate_dashboard(history)
    print(f"Aktueller Preis: {format_eur(product['price'])}")
    print(f"Verfuegbar: {'ja' if product['available'] else 'nein'}")
    print(f"Gespeichert: {DATA_FILE.name}, {DASHBOARD_FILE.name}")


if __name__ == "__main__":
    try:
        run()
    except (requests.RequestException, ProductDataError, OSError, json.JSONDecodeError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        sys.exit(1)
