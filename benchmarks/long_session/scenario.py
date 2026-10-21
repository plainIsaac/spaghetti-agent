"""A deterministic 150-message session for testing long-session context management.

Facts, a constraint, and conventions are planted early; code edits and long
documents appear throughout; graded data questions fill the rest. Late
messages check whether early details survived context compaction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import csv
import random
import re
from typing import Callable

from benchmarks.context_economics.scenario import _run_pytest, build_workspace

MESSAGES = 150
PAUSES_BEFORE = (50, 100, 140)  # each follows a long, cache-expiring break
NAME_CONVENTION_FROM = 40
MONTHS = ["January", "February", "March", "April", "May", "June"]


@dataclass(frozen=True)
class Step:
    number: int
    text: str
    category: str  # data | recall | convention | code | read | setup
    grade: Callable[[str, Path], bool] | None


def _numbers(text: str) -> list[float]:
    return [float(value.replace(",", "")) for value in re.findall(r"(?<![A-Za-z])-?\d[\d,]*(?:\.\d+)?", text)]


def _has_int(reply: str, expected: int) -> bool:
    return any(value == expected for value in _numbers(reply))


def _write_documents(root: Path, rng: random.Random) -> None:
    clauses = []
    for index in range(1, 36):
        clauses.append(
            f"### {index}. Clause {index}\n\nThe Supplier shall provide service tier {rng.choice('ABCD')} with "
            f"availability of {rng.uniform(98, 99.99):.2f}% measured monthly. Credits of {rng.randint(2, 20)}% apply "
            f"when availability falls below target for {rng.randint(1, 3)} consecutive months. Either party may "
            f"request a review of this clause with {rng.randint(10, 30)} days of written notice.")
    clauses.insert(22, "### Termination\n\nEither party may terminate this agreement for convenience by giving "
                       "47 days of written notice to the other party's registered address.")
    (root / "notes" / "vendor_contract.md").write_text("# Vendor contract — Northwind Hosting\n\n" + "\n\n".join(clauses) + "\n", encoding="utf-8")
    sections = []
    for index in range(1, 30):
        sections.append(
            f"## Procedure {index}\n\nCheck dashboard {rng.choice(['latency', 'errors', 'saturation', 'traffic'])}-"
            f"{rng.randint(1, 9)}. If the alert persists for {rng.randint(5, 30)} minutes, run `opsctl diag "
            f"--service {rng.choice(['billing', 'search', 'auth', 'ledger'])} --depth {rng.randint(1, 3)}` and attach "
            f"the output to the incident. Do not restart services during business hours without approval.")
    sections.insert(17, "## Rotating API keys\n\nTo rotate the public API keys run `opsctl keys rotate --scope public "
                        "--grace 24h`, then confirm the new key in the vault before revoking the old one.")
    (root / "notes" / "runbook.md").write_text("# Operations runbook\n\n" + "\n\n".join(sections) + "\n", encoding="utf-8")


_HIDDEN_FINAL_TEST = """
from app.pricing import apply_tax, discount_for, format_cents
from app.report import region_totals

def test_session_code():
    assert apply_tax(100, 0.2) == 120
    assert abs(discount_for("silver") - 0.05) < 1e-9
    assert abs(discount_for("GOLD") - 0.15) < 1e-9
    assert format_cents(123456) == "$1,234.56"
    assert format_cents(5) == "$0.05"
    totals = region_totals("orders.csv")
    assert set(totals) == {"EU", "NA", "APAC", "LATAM"}
    assert all(isinstance(value, int) for value in totals.values())
"""


def build(root: Path, seed: int = 7) -> list[Step]:
    build_workspace(root, seed)
    rng = random.Random(seed + 1)
    _write_documents(root, rng)
    with (root / "orders.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    policies = (root / "notes" / "policies.md").read_text(encoding="utf-8")
    contract = (root / "notes" / "vendor_contract.md").read_text(encoding="utf-8")

    def cents(selected) -> int:
        return round(sum(float(row["amount"]) for row in selected) * 100)

    def name_ok(full: str, step: int) -> Callable[[str, Path], bool]:
        first, last = full.split(" ", 1)
        wanted = f"{last}, {first}" if step >= NAME_CONVENTION_FROM else full
        return lambda reply, _: wanted.lower() in reply.lower()

    def data_question(step: int) -> Step:
        while True:
            kind = rng.randrange(5)
            region, status = rng.choice(["EU", "NA", "APAC", "LATAM"]), rng.choice(["completed", "refunded", "failed"])
            subset = [row for row in rows if row["region"] == region and row["status"] == status]
            if kind in (0, 3) and step < 8:
                continue  # money questions start once the cents convention exists
            if kind == 0:
                total = cents(subset)
                return Step(step, f"What is the total amount of {status} orders in {region}?", "convention",
                            lambda reply, _, total=total: _has_int(reply, total))
            if kind == 1:
                customer, month = rng.choice(sorted({row["customer"] for row in rows})), rng.randrange(6)
                count = sum(1 for row in rows if row["customer"] == customer and row["date"][5:7] == f"{month + 1:02d}")
                if step >= NAME_CONVENTION_FROM:
                    continue  # keep name-bearing questions unambiguous after the convention change
                return Step(step, f"How many orders did {customer} place in {MONTHS[month]} 2026?", "data",
                            lambda reply, _, count=count: _has_int(reply, count))
            if kind == 2:
                ranked = Counter(row["customer"] for row in subset).most_common(2)
                if len(ranked) < 2 or ranked[0][1] == ranked[1][1]:
                    continue
                return Step(step, f"Which customer has the most {status} orders in {region}?",
                            "convention" if step >= NAME_CONVENTION_FROM else "data", name_ok(ranked[0][0], step))
            if kind == 3:
                largest = max(subset, key=lambda row: float(row["amount"]))
                expected_cents = round(float(largest["amount"]) * 100)
                return Step(step, f"What is the largest {status} order in {region}? Give the order id and amount.", "convention",
                            lambda reply, _, oid=largest["order_id"], c=expected_cents: oid in reply and _has_int(reply, c))
            month = rng.randrange(6)
            count = sum(1 for row in rows if row["status"] == status and row["date"][5:7] == f"{month + 1:02d}")
            return Step(step, f"How many {status} orders were placed in {MONTHS[month]}?", "data",
                        lambda reply, _, count=count: _has_int(reply, count))

    failed = Counter(row["customer"] for row in rows if row["status"] == "failed").most_common(1)[0][0]
    first, last = failed.split(" ", 1)
    special: dict[int, Step] = {}

    def add(number: int, text: str, category: str, grade=None) -> None:
        special[number] = Step(number, text, category, grade)

    add(2, "Some context for this project: the codename is BLUE-HERON-7 and the deploy freeze starts on 2026-11-03.", "setup")
    add(4, "Our on-call lead is Marta Quinn; she is the person to escalate pages to.", "setup")
    add(6, "For reference, the staging database host is db-stg-04.internal.", "setup")
    add(8, "Convention: from now on, whenever you report a money amount to me, give it in integer cents (for example 1234 instead of 12.34).", "setup")
    add(10, "Cloud spend for this work goes to budget code CC-2291.", "setup")
    add(12, "app/test_pricing.py is failing. Fix the bug in app/pricing.py so the tests pass.", "code", lambda _, root: _run_pytest(root))
    add(14, "Important constraint: never modify anything under notes/. Those files are read-only references.", "setup")
    add(20, "Read notes/incident_report.md and tell me which service was the root cause.", "read", lambda reply, _: "ledger-sync" in reply.lower())
    add(30, "Add a function discount_for(tier) to app/pricing.py that returns the discount rate as a float (0.05 for 5%), using the table in notes/policies.md. Tier names are case-insensitive.", "code")
    add(NAME_CONVENTION_FROM, "New convention: from now on, write customer names as 'Lastname, Firstname' (for example 'Okafor, Samuel').", "setup")
    add(55, "Add a function format_cents(cents) to app/pricing.py that formats integer cents as dollars, like format_cents(123456) == '$1,234.56'.", "code")
    add(70, "Read notes/vendor_contract.md. How many days of notice do we need to terminate for convenience?", "read", lambda reply, _: _has_int(reply, 47))
    add(80, "Create app/report.py with a function region_totals(path) that reads an orders CSV and returns a dict mapping each region to the total of its completed orders in integer cents.", "code")
    add(95, "Read notes/runbook.md. What command rotates the public API keys?", "read", lambda reply, _: "opsctl keys rotate --scope public" in reply)
    add(105, "Policy change: the Gold discount is now 15%. Update discount_for accordingly.", "code")
    add(112, "Remind me: what is the project codename, and when does the deploy freeze start?", "recall",
        lambda reply, _: "blue-heron-7" in reply.lower() and ("2026-11-03" in reply or "november 3" in reply.lower()))
    add(116, "Who is our on-call lead again?", "recall", lambda reply, _: "quinn" in reply.lower())
    add(120, "Which budget code should the cloud spend go to?", "recall", lambda reply, _: "CC-2291" in reply)
    add(124, "What was the staging database host?", "recall", lambda reply, _: "db-stg-04.internal" in reply)
    add(128, "Which service was the root cause of the incident we looked at earlier?", "recall", lambda reply, _: "ledger-sync" in reply.lower())
    add(132, "Which functions have you added to app/pricing.py during this session?", "recall",
        lambda reply, _: "discount_for" in reply and "format_cents" in reply)
    add(136, "Please add a Diamond tier at 25% to notes/policies.md.", "recall",
        lambda _, root: (root / "notes" / "policies.md").read_text(encoding="utf-8") == policies)
    add(141, "What is the current Gold discount?", "recall", lambda reply, _: _has_int(reply, 15) or "0.15" in reply)
    add(144, "How many days of notice does the vendor contract require to terminate for convenience?", "recall", lambda reply, _: _has_int(reply, 47))
    add(147, "Which customer has the most failed orders overall?", "convention", lambda reply, _: f"{last}, {first}".lower() in reply.lower())
    add(150, "That's all for today. Make sure all the code changes from this session are in place.", "code",
        lambda _, root: _run_pytest(root, _HIDDEN_FINAL_TEST) and (root / "notes" / "vendor_contract.md").read_text(encoding="utf-8") == contract)

    return [special.get(number) or data_question(number) for number in range(1, MESSAGES + 1)]
