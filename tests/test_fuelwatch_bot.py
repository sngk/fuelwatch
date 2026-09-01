import unittest
from datetime import datetime

import fuelwatch_bot as bot


SAMPLE = b'''<?xml version="1.0"?><rss><channel><item>
<title>171.9: Example Fuel Bayswater</title><price>171.9</price>
<trading-name>Example Fuel Bayswater</trading-name><brand>Example</brand>
<location>BAYSWATER</location><address>1 Test Road</address><link>https://example.test</link>
</item></channel></rss>'''


class BotTests(unittest.TestCase):
    def test_parse_feed(self):
        station = bot.parse_feed(SAMPLE)[0]
        self.assertEqual(station.price, "171.9")
        self.assertEqual(station.name, "Example Fuel Bayswater")
        self.assertEqual(station.suburb, "BAYSWATER")

    def test_parse_title_fallback(self):
        station = bot.parse_feed(b"<rss><channel><item><title>155.7: Test Station</title></item></channel></rss>")[0]
        self.assertEqual((station.price, station.name), ("155.7", "Test Station"))

    def test_next_run_rolls_to_tomorrow(self):
        now = datetime(2026, 9, 1, 14, 41, tzinfo=bot.AWST)
        self.assertEqual(bot.next_run(now, 14, 40).day, 2)

    def test_comparison_summary_cheaper(self):
        today = [bot.Station("180.0", "Today Fuel", "", "BAYSWATER", "1 Road", "")]
        tomorrow = [bot.Station("175.0", "Tomorrow Fuel", "", "BAYSWATER", "2 Road", "")]
        summary = bot.comparison_summary(today, tomorrow)
        self.assertIn("5.0 c/L cheaper", summary)
        self.assertIn("$2.50", summary)

    def test_matching_station_change(self):
        station = bot.Station("181.5", "Same Fuel", "Brand", "PERTH", "1 Road", "")
        line = bot._price_line(station, 1, {bot._station_key(station): 180.0})
        self.assertIn("1.5 c/L dearer", line)


if __name__ == "__main__":
    unittest.main()
