"""A deterministic long-session scenario with gradeable user messages.

The session mixes data questions, file reading, code changes, a convention the
agent must keep, a fact it must remember, and a long pause before recall.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import csv
import random
import re
import subprocess
import sys
from typing import Callable

CUSTOMERS = [
    "Priya Natarajan", "Lena Hoffmann", "Marco Bianchi", "Aiko Tanaka", "Samuel Okafor",
    "Chloe Martin", "Diego Alvarez", "Fatima Zahra", "Noah Kim", "Olivia Brown",
    "Rafael Souza", "Ingrid Larsen", "Wei Chen", "Hannah Novak", "Tomás Ruiz",
    "Amara Diallo", "Jonas Berg", "Mei Lin", "Lucas Moreau", "Sofia Rossi",
]
REGIONS = ["EU", "NA", "APAC", "LATAM"]
PAUSE_BEFORE = 9  # Message number that follows a long, cache-expiring pause.

_INCIDENT_SERVICES = ["auth-gateway", "billing-api", "search-indexer", "notification-hub", "edge-cache"]


@dataclass(frozen=True)
class Step:
    number: int
    text: str
    grade: Callable[[str, Path], bool] | None  # (all replies for this step, workspace) -> passed


def build_workspace(root: Path, seed: int = 7) -> dict[str, object]:
    """Write the scenario files and return the expected answers."""
    rng = random.Random(seed)
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "app").mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(1500):
        month = rng.randint(1, 6)
        rows.append({
            "order_id": f"O{10001 + index}",
            "customer": rng.choice(CUSTOMERS),
            "region": rng.choice(REGIONS),
            "amount": f"{rng.uniform(10, 1000):.2f}",
            "status": rng.choices(["completed", "refunded", "failed"], [70, 15, 15])[0],
            "date": f"2026-{month:02d}-{rng.randint(1, 28):02d}",
        })
    with (root / "orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    paragraphs = []
    for index in range(40):
        service = rng.choice(_INCIDENT_SERVICES)
        paragraphs.append(
            f"{index + 2:02d}:{rng.randint(0, 59):02d} UTC — On-call reviewed {service} dashboards. "
            f"Latency for {service} was {rng.randint(80, 900)} ms at p99 and error rate {rng.uniform(0, 4):.2f}%. "
            f"The team considered whether {service} could explain the checkout failures, collected thread dumps, "
            f"and posted findings to the incident channel. No configuration change was made at this point."
        )
    paragraphs.insert(27, (
        "Conclusion of the investigation: the root cause was connection-pool exhaustion in the ledger-sync service, "
        "which held database connections open during retries. Other services only showed secondary symptoms."
    ))
    (root / "notes" / "incident_report.md").write_text(
        "# Incident 4471 — checkout failures\n\n" + "\n\n".join(paragraphs) + "\n", encoding="utf-8")
    (root / "notes" / "policies.md").write_text(
        "# Commercial policies\n\nRefunds are processed within 14 days.\n\n"
        "## Loyalty discounts\n\n| Tier | Discount |\n| --- | --- |\n| Bronze | 0% |\n| Silver | 5% |\n"
        "| Gold | 12% |\n| Platinum | 20% |\n\nDiscounts never stack with promotional codes.\n", encoding="utf-8")
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "pricing.py").write_text(
        '"""Pricing helpers."""\n\n\ndef round_money(amount):\n    return round(amount + 1e-9, 2)\n\n\n'
        'def apply_tax(amount, rate):\n    """Return the amount including tax at `rate` (0.2 means 20%)."""\n'
        '    return round_money(amount * rate)\n', encoding="utf-8")
    (root / "app" / "test_pricing.py").write_text(
        "from app.pricing import apply_tax\n\n\ndef test_apply_tax():\n    assert apply_tax(100, 0.2) == 120\n"
        "    assert apply_tax(19.99, 0.1) == 21.99\n", encoding="utf-8")

    def amount(row: dict[str, str]) -> float:
        return float(row["amount"])

    eu_refunded = [row for row in rows if row["region"] == "EU" and row["status"] == "refunded"]
    failed = Counter(row["customer"] for row in rows if row["status"] == "failed").most_common(2)
    if failed[0][1] == failed[1][1]:
        raise RuntimeError("seed produces an ambiguous most-failed customer; choose another seed")
    largest = max(eu_refunded, key=amount)
    return {
        "eu_refunded_total": round(sum(map(amount, eu_refunded)), 2),
        "most_failed_customer": failed[0][0],
        "apac_completed_cents": round(sum(amount(row) for row in rows if row["region"] == "APAC" and row["status"] == "completed") * 100),
        "priya_march_orders": sum(1 for row in rows if row["customer"] == "Priya Natarajan" and row["date"].startswith("2026-03")),
        "largest_eu_refund": (largest["order_id"], round(amount(largest) * 100)),
    }


def _numbers(text: str) -> list[float]:
    return [float(value.replace(",", "")) for value in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)]


def _run_pytest(root: Path, test_source: str | None = None) -> bool:
    target = "app/test_pricing.py"
    if test_source is not None:
        (root / "app" / "_hidden_test.py").write_text(test_source, encoding="utf-8")
        target = "app/_hidden_test.py"
    try:
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
                                cwd=root, capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    finally:
        if test_source is not None:
            (root / "app" / "_hidden_test.py").unlink(missing_ok=True)


_HIDDEN_DISCOUNT_TEST = """
from app.pricing import discount_for, apply_tax

def test_discount_for():
    assert discount_for("bronze") == 0
    assert abs(discount_for("Silver") - 0.05) < 1e-9
    assert abs(discount_for("GOLD") - 0.12) < 1e-9
    assert abs(discount_for("platinum") - 0.20) < 1e-9
    assert apply_tax(100, 0.2) == 120
"""


def steps(expected: dict[str, object]) -> list[Step]:
    order_id, largest_cents = expected["largest_eu_refund"]  # type: ignore[misc]
    return [
        Step(1, "How much money was refunded in total for region EU in orders.csv? Give the amount rounded to 2 decimals.",
             lambda reply, _: any(abs(value - expected["eu_refunded_total"]) < 0.011 for value in _numbers(reply))),
        Step(2, "Please remember this for later: the project codename is BLUE-HERON-7 and the deploy freeze starts on 2026-11-03. Nothing else to do right now.", None),
        Step(3, "Which customer has the most orders with status failed?",
             lambda reply, _: str(expected["most_failed_customer"]).lower() in reply.lower()),
        Step(4, "Read notes/incident_report.md and tell me which service was the root cause.",
             lambda reply, _: "ledger-sync" in reply.lower()),
        Step(5, "app/test_pricing.py is failing. Fix the bug in app/pricing.py so the tests pass.",
             lambda _, root: _run_pytest(root)),
        Step(6, "Change of convention: from now on, whenever you report a money amount to me, give it in integer cents (for example 1234 instead of 12.34).", None),
        Step(7, "What is the total amount of completed orders in APAC?",
             lambda reply, _: expected["apac_completed_cents"] in [int(value) for value in _numbers(reply) if value == int(value)]),
        Step(8, "Add a function discount_for(tier) to app/pricing.py that returns the discount rate as a float (0.05 for 5%), using the table in notes/policies.md. Tier names are case-insensitive.",
             lambda _, root: _run_pytest(root, _HIDDEN_DISCOUNT_TEST)),
        Step(9, "I'm back after a long break. What is the project codename, and when does the deploy freeze start?",
             lambda reply, _: "blue-heron-7" in reply.lower() and ("2026-11-03" in reply or "november 3" in reply.lower())),
        Step(10, "How many orders did Priya Natarajan place in March 2026?",
             lambda reply, _: expected["priya_march_orders"] in [int(value) for value in _numbers(reply)]),
        Step(11, "What was the largest single refunded order in EU? Give me the order id and the amount.",
             lambda reply, _: order_id in reply and largest_cents in [int(value) for value in _numbers(reply) if value == int(value)]),
        Step(12, "Which files have you changed during this session?",
             lambda reply, _: "pricing.py" in reply),
    ]
