import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import crawler


PRODUCT_PAYLOAD = {
    "title": "FRITZ!Box 5630",
    "variants": [
        {
            "sku": "20003150",
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
  "name": "FRITZ!Box 5630",
  "offers": [{
    "@type": "Offer",
    "priceCurrency": "EUR",
    "price": "169.99",
    "availability": "https://schema.org/InStock",
    "sku": "20003150"
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
        self.assertEqual(product["sku"], "20003150")
        self.assertTrue(product["available"])

    def test_update_history_replaces_same_day(self):
        history = crawler.new_history()
        history["observations"] = [{"date": "2026-08-21", "price": 180}]
        product = crawler.parse_product(PRODUCT_PAYLOAD)
        timestamp = datetime(2026, 8, 21, 18, 30, tzinfo=ZoneInfo("Europe/Berlin"))
        result = crawler.update_history(history, product, timestamp)
        self.assertEqual(len(result["observations"]), 1)
        self.assertEqual(result["observations"][0]["price"], 169.99)

    def test_dashboard_contains_current_price(self):
        product = crawler.parse_product(PRODUCT_PAYLOAD)
        timestamp = datetime(2026, 8, 21, 18, 30, tzinfo=ZoneInfo("Europe/Berlin"))
        history = crawler.update_history(crawler.new_history(), product, timestamp)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "index.html"
            crawler.generate_dashboard(history, target)
            document = target.read_text(encoding="utf-8")
        self.assertIn("169,99 €", document)
        self.assertIn("FRITZ!Box 5630", document)

    def legacy_history(self):
        return {
            "product": {
                "name": "FRITZ!Box 5530 Fiber",
                "sku": "20002960",
                "url": "https://fritz.com/products/fritz-box-5530-fiber-20002960",
                "currency": "EUR",
            },
            "observations": [{
                "date": "2026-11-05", "timestamp": "2026-11-05T16:31:29+01:00",
                "price": 199.99, "available": False, "compare_at_price": None,
            }],
        }

    def test_migration_keeps_all_legacy_records_and_is_idempotent(self):
        legacy = self.legacy_history()
        original = json.loads(json.dumps(legacy))
        migrated = crawler.prepare_history(legacy)
        self.assertEqual(migrated["product"]["sku"], "20003150")
        self.assertEqual(migrated["observations"], [])
        self.assertEqual(migrated["archives"][0]["product"], original["product"])
        self.assertEqual(migrated["archives"][0]["observations"], original["observations"])
        self.assertEqual(legacy, original)
        self.assertEqual(crawler.prepare_history(migrated), migrated)

    def test_new_measurement_on_same_day_does_not_replace_legacy_price(self):
        legacy = self.legacy_history()
        product = crawler.parse_product(PRODUCT_PAYLOAD)
        timestamp = datetime(2026, 11, 5, 18, tzinfo=ZoneInfo("Europe/Berlin"))
        result = crawler.update_history(legacy, product, timestamp)
        self.assertEqual(result["observations"][0]["price"], 169.99)
        self.assertEqual(result["archives"][0]["observations"][0]["price"], 199.99)

    def test_fetch_only_queries_the_new_model(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = PRODUCT_PAYLOAD
        crawler.fetch_product(session)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.get.call_args.args[0],
                         "https://fritz.com/products/fritz-box-5630-20003150.js")

    def test_rate_limit_waits_before_retrying_the_same_endpoint(self):
        limited = Mock(status_code=429, headers={"Retry-After": "60"})
        success = Mock(status_code=200)
        success.json.return_value = PRODUCT_PAYLOAD
        session = Mock()
        session.get.side_effect = [limited, success]
        with patch("crawler.time.sleep") as sleep:
            result = crawler.fetch_product(session)
        sleep.assert_called_once_with(60)
        self.assertEqual(result["sku"], "20003150")
        self.assertEqual([call.args[0] for call in session.get.call_args_list],
                         [crawler.PRODUCT_JSON_URL, crawler.PRODUCT_JSON_URL])

    def test_persistent_rate_limit_does_not_request_html_fallback(self):
        session = Mock()
        session.get.return_value = Mock(status_code=429, headers={"Retry-After": "1"})
        with patch("crawler.time.sleep"):
            with self.assertRaises(crawler.ShopRateLimitError):
                crawler.fetch_product(session)
        self.assertEqual(session.get.call_count, 2)
        self.assertTrue(all(call.args[0] == crawler.PRODUCT_JSON_URL
                            for call in session.get.call_args_list))

    def test_long_rate_limit_is_not_retried_early(self):
        session = Mock()
        session.get.return_value = Mock(status_code=429, headers={"Retry-After": "120"})
        with patch("crawler.time.sleep") as sleep:
            with self.assertRaises(crawler.ShopRateLimitError):
                crawler.fetch_product(session)
        sleep.assert_not_called()
        self.assertEqual(session.get.call_count, 1)

    def test_dashboard_separates_current_model_and_archive(self):
        product = crawler.parse_product(PRODUCT_PAYLOAD)
        timestamp = datetime(2026, 11, 6, 15, tzinfo=ZoneInfo("Europe/Berlin"))
        history = crawler.update_history(self.legacy_history(), product, timestamp)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "index.html"
            crawler.generate_dashboard(history, target)
            document = target.read_text(encoding="utf-8")
        active, archived = document.split('<section id="history"', 1)
        self.assertIn("FRITZ!Box 5630", active)
        self.assertNotIn("199,99 €", active)
        self.assertIn("FRITZ!Box 5530 Fiber", archived)
        self.assertIn("199,99 €", archived)
        self.assertIn("Tracking beendet", archived)
        self.assertIn("ZULETZT ERFASSTER PREIS", archived)
        self.assertIn('id="archive-chart-0"', archived)
        self.assertIn("Alle 1 Tagesmessungen ansehen", archived)

    def test_unexpected_history_sku_is_rejected(self):
        history = self.legacy_history()
        history["product"]["sku"] = "another-router"
        with self.assertRaises(crawler.ProductDataError):
            crawler.prepare_history(history)


if __name__ == "__main__":
    unittest.main()
