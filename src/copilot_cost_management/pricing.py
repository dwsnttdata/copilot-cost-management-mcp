from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
import re
from typing import Any

import httpx


PRICING_SOURCE_URL = (
    "https://learn.microsoft.com/en-us/azure/cost-management-billing/"
    "reservations/copilot-credit-p3"
)
_PAYG_RATE_PATTERN = re.compile(
    r"assuming\s+the\s+pay-as-you-go\s+rate\s+for\s+copilot\s+credit\s+to\s+be"
    r"\s+\$\s*(?P<rate>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_CENT = Decimal("0.01")


class _LearnPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.metadata: dict[str, str] = {}
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "meta":
            values = {key.lower(): value for key, value in attrs if value is not None}
            name = (values.get("name") or values.get("property") or "").lower()
            content = values.get("content")
            if name and content:
                self.metadata[name] = content
        elif tag in {"p", "li", "h1", "h2", "h3", "td", "th", "br"}:
            self.text.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in {
            "p",
            "li",
            "h1",
            "h2",
            "h3",
            "td",
            "th",
            "br",
        }:
            self.text.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.text.append(data)


def parse_microsoft_pricing_page(html: str) -> dict[str, Any]:
    parser = _LearnPageParser()
    parser.feed(html)
    page_text = " ".join(" ".join(parser.text).split())
    match = _PAYG_RATE_PATTERN.search(page_text)
    if not match:
        raise RuntimeError(
            "Microsoft Learn did not expose its Copilot Credit pay-as-you-go "
            "reference rate in the expected form; no monetary estimate was returned."
        )

    try:
        rate = Decimal(match.group("rate"))
    except InvalidOperation as error:
        raise RuntimeError("Microsoft Learn returned an invalid Copilot Credit rate.") from error
    if not rate.is_finite() or rate <= 0:
        raise RuntimeError("Microsoft Learn returned a non-positive Copilot Credit rate.")

    return {
        "rate_usd_per_credit": rate,
        "source_title": parser.metadata.get("title", "Copilot Credit P3 - Microsoft Learn"),
        "source_last_updated": parser.metadata.get("ms.date"),
        "source_url": PRICING_SOURCE_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "pricing_basis": (
            "Illustrative pay-as-you-go rate assumption published by Microsoft Learn; "
            "not a guaranteed customer-specific or pre-purchase price."
        ),
    }


async def fetch_live_copilot_credit_pricing() -> dict[str, Any]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=True,
        headers={"User-Agent": "copilot-cost-management-mcp"},
    ) as client:
        response = await client.get(PRICING_SOURCE_URL)
        response.raise_for_status()
    return parse_microsoft_pricing_page(response.text)


def _validated_credits(credits: float | int | str) -> Decimal:
    try:
        value = Decimal(str(credits))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("credits must be a finite, non-negative number.") from error
    if not value.is_finite() or value < 0:
        raise ValueError("credits must be a finite, non-negative number.")
    return value


def _rounded_usd(credits: Decimal, rate: Decimal) -> Decimal:
    return (credits * rate).quantize(_CENT, rounding=ROUND_HALF_UP)


def convert_credits_to_usd(
    credits: float | int | str, pricing: dict[str, Any]
) -> dict[str, Any]:
    quantity = _validated_credits(credits)
    rate = pricing["rate_usd_per_credit"]
    amount = _rounded_usd(quantity, rate)
    return {
        "credits": float(quantity),
        "units": "copilotCredit",
        "currency": "USD",
        "estimated_amount_usd": float(amount),
        "estimated_amount_usd_display": f"${amount:,.2f}",
        **pricing,
        "rate_usd_per_credit": float(rate),
        "caveats": [
            "Estimate uses the illustrative pay-as-you-go rate on the live Microsoft Learn page.",
            "Actual charges can differ due to pre-purchase discounts, billing method, "
            "service, region, contract terms, taxes, and other discounts.",
            "This is not invoice reconciliation; use the billing export or invoice for "
            "actual charged amounts.",
        ],
    }


def attach_usd_estimate(
    row: dict[str, Any], credits_field: str, rate: Decimal
) -> None:
    credits = row.get(credits_field)
    if credits is None:
        return
    quantity = _validated_credits(credits)
    amount = _rounded_usd(quantity, rate)
    row["estimated_cost_usd"] = float(amount)
    row["estimated_cost_usd_display"] = f"${amount:,.2f}"
