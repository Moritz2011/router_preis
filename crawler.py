#!/usr/bin/env python3
"""Track the FRITZ!Box 5630 and retain the retired 5530 Fiber's history."""

from __future__ import annotations

import html
import json
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


PRODUCT_NAME = "FRITZ!Box 5630"
PRODUCT_URL = "https://fritz.com/products/fritz-box-5630-20003150"
PRODUCT_JSON_URL = f"{PRODUCT_URL}.js"
EXPECTED_SKU = "20003150"
RETIRED_SKU = "20002960"
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


class ShopRateLimitError(ProductDataError):
    """A shop rate limit must not trigger immediate fallback requests."""


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
        "name": str(payload.get("title") or PRODUCT_NAME),
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
                "name": str(candidate.get("name") or PRODUCT_NAME),
                "sku": EXPECTED_SKU,
                "price": min(prices),
                "compare_at_price": None,
                "available": bool(available_offers),
            }

    raise ProductDataError("Keine passenden strukturierten Produktdaten im HTML gefunden.")


def get_shop_response(client: requests.Session, url: str) -> requests.Response:
    """Honor HTTP 429 once, waiting at most 60 seconds as requested by the shop."""
    for attempt in range(2):
        response = client.get(url, headers=HEADERS, timeout=(10, 30))
        if response.status_code != 429:
            return response
        retry_after = response.headers.get("Retry-After", "60")
        try:
            delay = max(1, int(retry_after))
        except ValueError:
            try:
                until = parsedate_to_datetime(retry_after)
                delay = max(1, int((until - datetime.now(timezone.utc)).total_seconds()) + 1)
            except (ValueError, TypeError, OverflowError):
                delay = 60
        if attempt == 1 or delay > 60:
            raise ShopRateLimitError(
                f"Der Shop begrenzt Anfragen (HTTP 429, Retry-After: {retry_after}). "
                "Vorhandene Messwerte bleiben erhalten."
            )
        print(f"Shop-Anfragelimit erreicht; warte {delay} Sekunden vor einem erneuten Versuch.", flush=True)
        time.sleep(delay)
    raise ShopRateLimitError("Der Shop begrenzt Anfragen weiterhin.")


def fetch_product(session: requests.Session | None = None) -> dict[str, Any]:
    """Fetch product JSON, falling back to schema.org data in the HTML page."""
    client = session or requests.Session()
    try:
        response = get_shop_response(client, PRODUCT_JSON_URL)
        response.raise_for_status()
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise ProductDataError("Der Shop hat kein gueltiges JSON geliefert.") from exc
        if not isinstance(payload, dict):
            raise ProductDataError("Unerwartetes Format der Produktantwort.")
        return parse_product(payload)
    except ShopRateLimitError:
        raise
    except (requests.RequestException, ProductDataError) as primary_error:
        print(
            f"Hinweis: Shopify-JSON nicht nutzbar ({primary_error}); nutze HTML-Fallback.",
            file=sys.stderr,
        )

    response = get_shop_response(client, PRODUCT_URL)
    response.raise_for_status()
    return parse_product_html(response.text)


def new_history() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "product": {
            "name": PRODUCT_NAME,
            "sku": EXPECTED_SKU,
            "url": PRODUCT_URL,
            "currency": CURRENCY,
        },
        "observations": [],
        "archives": [],
    }


def prepare_history(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate the old single-model file once, preserving its original records."""
    if not isinstance(data, dict) or not isinstance(data.get("observations"), list):
        raise ProductDataError("Die Preishistorie hat ein ungueltiges Format.")
    sku = data.get("product", {}).get("sku")
    archives = data.get("archives", [])
    if not isinstance(archives, list):
        raise ProductDataError("Das Preisarchiv hat ein ungueltiges Format.")
    if sku == EXPECTED_SKU:
        return {**data, "schema_version": 2, "archives": archives}
    if sku != RETIRED_SKU:
        raise ProductDataError(f"Unerwartete Produkt-SKU in der Historie: {sku}")
    if any(item.get("product", {}).get("sku") == RETIRED_SKU for item in archives):
        raise ProductDataError("Die 5530-Historie ist mehrfach vorhanden.")

    archived = {key: value for key, value in data.items() if key != "archives"}
    archived["archive_reason"] = "Dieses Modell wird nicht mehr angeboten. Das Tracking ist beendet."
    result = new_history()
    result["archives"] = [*archives, archived]
    return result


def load_history(path: Path = DATA_FILE) -> dict[str, Any]:
    """Load history and migrate the retired model in memory; never fetch it."""
    if not path.exists():
        return new_history()

    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    return prepare_history(data)


def update_history(
    history: dict[str, Any], product: dict[str, Any], timestamp: datetime
) -> dict[str, Any]:
    """Add or replace today's observation and keep entries sorted."""
    history = prepare_history(history)
    if product["sku"] != EXPECTED_SKU:
        raise ProductDataError("Die Messung gehoert nicht zum aktiven Produkt.")
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


def render_archive(history: dict[str, Any], index: int) -> tuple[str, dict[str, Any] | None]:
    """Render an archived model using only its frozen observations."""
    product = history["product"]
    title = html.escape(product["name"])
    reason = html.escape(history.get("archive_reason", "Das Tracking dieses Modells ist beendet."))
    observations = history["observations"]
    heading = f'<h3>{title}</h3><span class="archive-badge">Historie · Tracking beendet</span>'
    if not observations:
        return f'<article class="card archived-card">{heading}<p>{reason}</p></article>', None

    current = observations[-1]
    prices = [float(item["price"]) for item in observations]
    first_date = datetime.fromisoformat(observations[0]["timestamp"]).strftime("%d.%m.%Y")
    updated = datetime.fromisoformat(current["timestamp"]).strftime("%d.%m.%Y um %H:%M Uhr")
    last_status = "Verfügbar" if current["available"] else "Nicht verfügbar"
    chart_id = f"archive-chart-{index}"
    rows = "".join(
        f'<tr><th scope="row">{datetime.fromisoformat(item["timestamp"]).strftime("%d.%m.%Y")}</th>'
        f'<td>{format_eur(item["price"])}</td>'
        f'<td>{"Verfügbar" if item["available"] else "Nicht verfügbar"}</td></tr>'
        for item in reversed(observations)
    )
    markup = f"""
      <article class="card archived-card">
        <div class="model-heading">{heading}</div>
        <p class="archive-note">{reason} Die bisherigen Daten bleiben hier erhalten.</p>
        <div class="summary">
          <div class="current">
            <div class="label">ZULETZT ERFASSTER PREIS</div>
            <div class="price archive-price">{format_eur(current['price'])}</div>
            <p class="last-status">Letzter Lieferstatus: {last_status}</p>
          </div>
          <div><div class="label">TIEFSTPREIS IM VERLAUF</div><div class="metric">{format_eur(min(prices))}</div></div>
          <div><div class="label">HÖCHSTPREIS IM VERLAUF</div><div class="metric">{format_eur(max(prices))}</div></div>
        </div>
        <h4>Abgeschlossener Preisverlauf</h4>
        <div class="chart-wrap"><canvas id="{chart_id}" role="img" aria-label="Historischer Preisverlauf {title}"></canvas></div>
        <details class="history-details">
          <summary>Alle {len(observations)} Tagesmessungen ansehen</summary>
          <div class="table-scroll"><table>
            <thead><tr><th scope="col">Datum</th><th scope="col">Preis</th><th scope="col">Lieferstatus damals</th></tr></thead>
            <tbody>{rows}</tbody>
          </table></div>
        </details>
        <div class="actions">
          <span>Erfasst seit {first_date} · Letzte Messung: {updated}</span>
          <a href="{html.escape(product['url'], quote=True)}" target="_blank" rel="noopener">Ehemalige Produktseite</a>
        </div>
      </article>"""
    chart = {
        "id": chart_id,
        "labels": [item["date"] for item in observations],
        "values": prices,
        "archived": True,
    }
    return markup, chart


def generate_dashboard(history: dict[str, Any], path: Path = DASHBOARD_FILE) -> None:
    """Show the active model followed by the preserved, separate model archives."""
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
    charts = [{
        "id": "price-chart",
        "labels": [item["date"] for item in observations],
        "values": prices,
        "archived": False,
    }]
    archive_markup = []
    for index, archive in enumerate(history.get("archives", [])):
        markup, chart = render_archive(archive, index)
        archive_markup.append(markup)
        if chart is not None:
            charts.append(chart)
    archive_html = (
        '<section id="history" class="archive-area"><h2>Abgeschlossene Preisverläufe</h2>'
        + "".join(archive_markup) + '</section>' if archive_markup else ""
    )
    navigation = '<nav><a href="#active-model">Aktuelles Modell</a><a href="#history">Modell-Historie</a></nav>' if archive_markup else ""
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
    nav {{ display: flex; gap: 20px; margin-top: 22px; }}
    nav a {{ color: white; text-underline-offset: 5px; font-size: .9rem; }}
    a:focus-visible, summary:focus-visible {{ outline: 3px solid #0089d1; outline-offset: 5px; }}
    nav a:focus-visible {{ outline-color: white; }}
    .archive-area {{ margin-top: 52px; padding-top: 26px; border-top: 1px solid #cbd5e1; }}
    .archive-area > h2 {{ margin: 0 0 20px; font-size: 1.4rem; }}
    .archived-card {{ margin-top: 20px; box-shadow: 0 5px 20px rgba(0, 30, 110, .06); }}
    .model-heading {{ display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }}
    .model-heading h3 {{ margin: 0; font-size: 1.4rem; }}
    .archive-badge {{ color: #40516c; background: #edf2f7; padding: 6px 10px; border-radius: 7px; font-size: .8rem; }}
    .archive-note, .last-status {{ color: #5f6d76; font-size: .9rem; line-height: 1.6; }}
    .archive-note {{ margin-bottom: 24px; }}
    .archive-price {{ font-size: 2.4rem; }}
    .archived-card h4 {{ margin: 28px 0 20px; font-size: 1rem; }}
    .history-details {{ margin-top: 20px; }}
    summary {{ cursor: pointer; color: #001e6e; font-weight: 650; padding: 10px 0; }}
    .table-scroll {{ overflow-x: auto; margin-top: 12px; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 380px; font-size: .85rem; }}
    th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
    thead {{ background: #f1f6fa; }}
    tbody th {{ font-weight: 500; }}
    .actions a:not(.button) {{ color: #40516c; text-underline-offset: 3px; }}
    @media (max-width: 680px) {{ .summary {{ grid-template-columns: 1fr 1fr; }}
      .current {{ grid-column: 1 / -1; }} .actions {{ align-items: flex-start; flex-direction: column; }} }}
  </style>
</head>
<body>
  <header><div><h1>{title}</h1><p>Tägliches Tracking aktiv · Artikelnummer {html.escape(product['sku'])}</p>{navigation}</div></header>
  <main>
    <section id="active-model" class="card summary">
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
      <div class="chart-wrap"><canvas id="price-chart" role="img" aria-label="Preisverlauf {title}"></canvas></div>
    </section>
    <div class="actions">
      <span>Zuletzt geprüft: {updated} · Preise ohne Gewähr</span>
      <a class="button" href="{html.escape(product['url'], quote=True)}" target="_blank" rel="noopener">Bei FRITZ! ansehen</a>
    </div>
    {archive_html}
  </main>
  <script>
    for (const chart of {json.dumps(charts, ensure_ascii=False)}) {{
      new Chart(document.getElementById(chart.id), {{
      type: 'line',
      data: {{ labels: chart.labels, datasets: [{{ data: chart.values,
        borderColor: chart.archived ? '#64748b' : '#0089d1',
        backgroundColor: chart.archived ? 'rgba(100, 116, 139, .08)' : 'rgba(0, 190, 255, .12)',
        fill: true, tension: .25, pointRadius: chart.values.length > 40 ? 2 : 4,
        pointBackgroundColor: chart.archived ? '#64748b' : '#001e6e' }}] }},
      options: {{ maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }},
        interaction: {{ intersect: false, mode: 'index' }},
        scales: {{ y: {{ ticks: {{ callback: value => value.toLocaleString('de-DE',
          {{ style: 'currency', currency: 'EUR' }}) }} }} }} }}
      }});
    }}
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
