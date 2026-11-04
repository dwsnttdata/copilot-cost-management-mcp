import unittest
from decimal import Decimal
from unittest.mock import patch

import httpx

from copilot_cost_management import pricing


MICROSOFT_PRICING_PAGE = """
<html>
  <head>
    <meta name="title" content="Copilot Credit P3 - Microsoft Cost Management">
    <meta name="ms.date" content="2026-07-17">
    <script>const cached = "assuming the pay-as-you-go rate for Copilot Credit to be $9.99";</script>
  </head>
  <body>
    <p>Assuming the pay-as-you-go rate for Copilot Credit to be $0.01,
    then at the pay-as-you-go rate, this will cost $15,000.</p>
  </body>
</html>
"""


class CopilotCreditPricingTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_reads_the_visible_assumption_not_script_content(self):
        result = pricing.parse_microsoft_pricing_page(MICROSOFT_PRICING_PAGE)

        self.assertEqual(result["rate_usd_per_credit"], Decimal("0.01"))
        self.assertEqual(result["source_last_updated"], "2026-07-17")
        self.assertEqual(result["source_url"], pricing.PRICING_SOURCE_URL)

    def test_parser_fails_if_rate_is_no_longer_published(self):
        with self.assertRaisesRegex(RuntimeError, "no monetary estimate"):
            pricing.parse_microsoft_pricing_page("<html><body>No price listed.</body></html>")

    async def test_fetch_reads_the_microsoft_source_each_time(self):
        response = httpx.Response(
            200,
            text=MICROSOFT_PRICING_PAGE,
            request=httpx.Request("GET", pricing.PRICING_SOURCE_URL),
        )

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url):
                self.url = url
                return response

        fake_client = FakeAsyncClient()
        with patch.object(pricing.httpx, "AsyncClient", return_value=fake_client) as client:
            result = await pricing.fetch_live_copilot_credit_pricing()

        client.assert_called_once()
        self.assertEqual(fake_client.url, pricing.PRICING_SOURCE_URL)
        self.assertEqual(result["rate_usd_per_credit"], Decimal("0.01"))

    def test_conversion_returns_usd_cents_and_discloses_basis(self):
        result = pricing.convert_credits_to_usd(
            "1146.227227",
            {
                "rate_usd_per_credit": Decimal("0.01"),
                "source_url": pricing.PRICING_SOURCE_URL,
                "pricing_basis": (
                    "Illustrative pay-as-you-go rate assumption published by Microsoft Learn; "
                    "not a guaranteed customer-specific or pre-purchase price."
                ),
            },
        )

        self.assertEqual(result["estimated_amount_usd"], 11.46)
        self.assertEqual(result["estimated_amount_usd_display"], "$11.46")
        self.assertEqual(result["currency"], "USD")
        self.assertIn("not a guaranteed", result["pricing_basis"])
        self.assertTrue(result["caveats"])

    def test_conversion_rejects_invalid_credit_amounts(self):
        for amount in (-1, float("nan"), "not-a-number"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                pricing.convert_credits_to_usd(amount, {"rate_usd_per_credit": Decimal("0.01")})


if __name__ == "__main__":
    unittest.main()
