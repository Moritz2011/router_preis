# FRITZ!Box 5630 – Preis-Crawler mit 5530-Archiv

Der Crawler liest täglich den aktuellen Preis der
[FRITZ!Box 5630](https://fritz.com/products/fritz-box-5630-20003150)
aus dem öffentlichen Shopify-Produktendpunkt aus. Falls dieser nicht nutzbar
ist, dienen die strukturierten Produktdaten der HTML-Seite als Fallback. Der
Crawler speichert pro Tag einen Messwert in `price_data.json` und erzeugt daraus
das statische Dashboard `index.html`.

Die FRITZ!Box 5530 Fiber wird nicht mehr abgefragt. Ihre bisherigen Messwerte
bleiben unverändert als abgeschlossenes Archiv auf derselben Seite sichtbar:
letzter Preis und Lieferstatus, Tiefst-/Höchstpreis, Diagramm und alle
Tagesmessungen. Der Verlauf der 5630 startet separat mit der ersten neuen Messung.

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

Auch bei wiederholten Läufen wird der aktuelle Stand von `main` ausgecheckt.
Beim Speichern führt `publish_prices.py` die neue Messung mit dem neuesten
Remote-Preisverlauf zusammen. Pro Tag bleibt die zeitlich neueste Messung
erhalten; das Dashboard wird daraus neu erzeugt. Bei konkurrierenden Commits
wird der normale Push bis zu dreimal versucht. Andere Änderungen und ältere
Tagesmesswerte bleiben dabei erhalten. Es wird kein Force-Push verwendet.

## Dashboard über GitHub Pages

`.github/workflows/pages.yml` veröffentlicht `index.html` automatisch über
GitHub Pages. Nach jedem erfolgreichen täglichen Crawler-Lauf wird auch die
Website mit den neuen Daten aktualisiert.

## Datenformat

`price_data.json` enthält unter `product` und `observations` das aktive Modell
(5630, SKU `20003150`). Unter `archives` liegen abgeschlossene Modelle mit
ihren eigenen Produktinformationen und Messwerten. Das bisherige Datenformat
der 5530 (SKU `20002960`) wird beim ersten erfolgreichen neuen Lauf automatisch
und ohne weitere Shop-Abfrage ins Archiv übernommen.

Mehrere Läufe am selben Tag ersetzen nur den Messwert des aktiven Modells für
diesen Tag. Schlägt der Abruf fehl oder liefert der Shop nicht die erwartete
SKU `20003150`, bleiben die vorhandenen Dateien unverändert. Die Archivdaten
werden bei der Veröffentlichung auch nach konkurrierenden Pushes beibehalten.
Bei einem Shop-Anfragelimit (HTTP 429) wartet der Crawler einmal entsprechend
`Retry-After`, höchstens 60 Sekunden. Längere oder anhaltende Limits beenden den
Lauf ohne weitere Fallback-Anfrage und ohne Änderungen an den gespeicherten Daten.
