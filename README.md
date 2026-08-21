# FRITZ!Box 5530 Fiber – Preis-Crawler

Der Crawler liest täglich den aktuellen Preis der
[FRITZ!Box 5530 Fiber](https://fritz.com/products/fritz-box-5530-fiber-20002960)
aus dem öffentlichen Shopify-Produktendpunkt aus. Falls dieser nicht nutzbar
ist, dienen die strukturierten Produktdaten der HTML-Seite als Fallback. Der
Crawler speichert pro Tag einen Messwert in `price_data.json` und erzeugt daraus
das statische Dashboard `index.html`.

Das automatisch aktualisierte Dashboard ist unter
[moritz2011.github.io/router_preis](https://moritz2011.github.io/router_preis/)
erreichbar.

## Lokal ausführen

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python crawler.py
```

Tests:

```bash
python -m unittest discover -s tests -v
```

## GitHub-Automatisierung einrichten

1. Auf GitHub ein leeres Repository anlegen.
2. Dieses Projekt committen und auf den Standard-Branch pushen.
3. Unter **Settings → Actions → General → Workflow permissions** die Option
   **Read and write permissions** aktivieren, falls die Organisation dies nicht
   bereits erlaubt. Der Workflow benötigt Schreibzugriff, um `price_data.json`
   und `index.html` zu committen.
4. Unter **Actions → FRITZ!Box Preis-Crawler → Run workflow** einen ersten
   manuellen Lauf starten.

Danach läuft `.github/workflows/price-crawler.yml` täglich um 08:15 Uhr in der
Zeitzone `Europe/Berlin`. GitHub kann geplante Workflows bei hoher Auslastung
etwas verzögert starten.

## Dashboard über GitHub Pages

`.github/workflows/pages.yml` veröffentlicht `index.html` automatisch über
GitHub Pages. Nach jedem erfolgreichen täglichen Crawler-Lauf wird auch die
Website mit den neuen Daten aktualisiert.

## Datenformat

`price_data.json` behält die täglichen Messwerte. Mehrere Läufe am selben Tag
ersetzen nur den Messwert dieses Tages. Schlägt der Abruf fehl oder liefert der
Shop nicht mehr die erwartete SKU `20002960`, beendet sich der Crawler mit einem
Fehler und vorhandene Daten bleiben unverändert.
