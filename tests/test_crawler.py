import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import crawler


PRODUCT_PAYLOAD = {
    "title": "FRITZ!Box 5530 Fiber",
    "variants": [
        {
            "sku": "20002960",
            "price": 16999,
            "compare_at_price": 18599,
            "available": True,
        }
    ],
}

PRODUCT_HTML = """
<!doctype html><html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org/",
  "@type": "Product",
  "name": "FRITZ!Box 5530 Fiber",
  "offers": [{
    "@type": "Offer",
    "priceCurrency": "EUR",
    "price": "169.99",
    "availability": "https://schema.org/InStock",
    "sku": "20002960"
  }]
}
</script></head><body></body></html>
"""


class CrawlerTests(unittest.TestCase):
    def test_parse_product_converts_cents(self):
        product = crawler.parse_product(PRODUCT_PAYLOAD)
        self.assertEqual(product["price"], 169.99)
        self.assertEqual(product["compare_at_price"], 185.99)
        self.assertTrue(product["available"])

    def test_parse_product_rejects_wrong_sku(self):
        payload = json.loads(json.dumps(PRODUCT_PAYLOAD))
        payload["variants"][0]["sku"] = "wrong"
        with self.assertRaises(crawler.ProductDataError):
            crawler.parse_product(payload)

    def test_parse_product_html_uses_json_ld_fallback(self):
        product = crawler.parse_product_html(PRODUCT_HTML)
        self.assertEqual(product["price"], 169.99)
        self.assertEqual(product["sku"], "20002960")
        self.assertTrue(product["available"])

    def test_update_history_replaces_same_day(self):
        history = {"product": {}, "observations": [{"date": "2026-08-21", "price": 180}]}
        product = crawler.parse_product(PRODUCT_PAYLOAD)
        timestamp = datetime(2026, 8, 21, 18, 30, tzinfo=ZoneInfo("Europe/Berlin"))
        result = crawler.update_history(history, product, timestamp)
        self.assertEqual(len(result["observations"]), 1)
        self.assertEqual(result["observations"][0]["price"], 169.99)

    def test_dashboard_contains_current_price(self):
        product = crawler.parse_product(PRODUCT_PAYLOAD)
        timestamp = datetime(2026, 8, 21, 18, 30, tzinfo=ZoneInfo("Europe/Berlin"))
        history = crawler.update_history({"product": {}, "observations": []}, product, timestamp)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "index.html"
            crawler.generate_dashboard(history, target)
            document = target.read_text(encoding="utf-8")
        self.assertIn("169,99 €", document)
        self.assertIn("FRITZ!Box 5530 Fiber", document)


if __name__ == "__main__":
    unittest.main()
