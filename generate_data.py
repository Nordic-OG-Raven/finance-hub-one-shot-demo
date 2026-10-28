"""
generate_data.py — build the raw source data for the demo.

This stands in for what would normally come out of an ERP / Business Central:
a general-ledger export (actuals) and a budget export. We write them as CSVs
to data/raw/ so the ETL has a genuine "ingest raw files" starting point —
exactly like landing files in a Fabric Lakehouse bronze layer.

The data is deliberately a little messy (inconsistent department spellings,
amounts as strings) so the silver layer has real cleaning work to do, and it
contains three deliberate "stories" so the variance-explainer AI has something
real to talk about:

  1. Marketing overspend in H2 (a Webshop growth push).
  2. Revenue shortfall in Detail København in Q4.
  3. A COGS (Vareforbrug) spike from mid-year on a supplier price increase.

Run:  python generate_data.py
"""

import csv
import os
import random

random.seed(42)  # deterministic — the demo looks the same every run

RAW_DIR = os.path.join(os.path.dirname(__file__), "data", "raw")

YEAR = 2024
MONTHS = list(range(1, 13))

# P&L accounts for a small Danish furniture retailer, "Nordlys Møbler A/S".
# is_revenue drives the sign convention in reporting.
ACCOUNTS = [
    # account_no, account_name, account_group, is_revenue
    (1000, "Omsætning", "Omsætning", True),
    (2000, "Vareforbrug", "Vareforbrug", False),
    (3000, "Personaleomkostninger", "Personale", False),
    (4000, "Markedsføring", "Markedsføring", False),
    (5000, "Lokaleomkostninger", "Lokaler", False),
    (6000, "Øvrige omkostninger", "Øvrige", False),
]

# Departments as the business thinks of them (canonical form).
DEPARTMENTS = [
    # canonical name, region, channel
    ("Detail Aarhus", "Jylland", "Detail"),
    ("Detail København", "Sjælland", "Detail"),
    ("Webshop", "Online", "Webshop"),
]

# Messy spellings that show up in the raw GL export — the silver layer must
# normalise these back to the canonical department name.
DEPT_NOISE = {
    "Detail Aarhus": ["Detail Aarhus", "detail aarhus ", "  Detail Aarhus", "DETAIL AARHUS"],
    "Detail København": ["Detail København", "detail københavn", "Detail Koebenhavn ", "detail København "],
    "Webshop": ["Webshop", "webshop", " WEBSHOP", "web shop"],
}

# Baseline monthly budget per account per department (DKK).
# Revenue split roughly: Aarhus 35%, København 40%, Webshop 25%.
REVENUE_BUDGET = {"Detail Aarhus": 1_050_000, "Detail København": 1_200_000, "Webshop": 750_000}
# Costs as a share of that department's revenue budget.
COST_RATE = {
    2000: 0.42,  # Vareforbrug ~42% of revenue
    3000: 0.22,  # Personale
    4000: 0.05,  # Markedsføring
    5000: 0.08,  # Lokaler
    6000: 0.03,  # Øvrige
}


def budget_amount(account_no, dept):
    """Flat monthly budget — the plan, before any real-world variance."""
    rev = REVENUE_BUDGET[dept]
    if account_no == 1000:
        return rev
    return round(rev * COST_RATE[account_no])


def actual_amount(account_no, dept, month):
    """Actuals = budget plus normal noise plus the three deliberate stories."""
    base = budget_amount(account_no, dept)
    noise = random.uniform(-0.04, 0.04)  # ±4% ordinary month-to-month wobble
    amount = base * (1 + noise)

    # Story 1: marketing overspend in H2, heaviest on the Webshop push.
    if account_no == 4000 and month >= 7:
        ramp = 1.4 + (month - 7) * 0.12  # grows Jul -> Dec
        if dept == "Webshop":
            ramp += 0.5
        amount = base * ramp

    # Story 2: revenue miss in Detail København in Q4.
    if account_no == 1000 and dept == "Detail København" and month >= 10:
        amount = base * 0.82  # ~18% under plan

    # Story 3: COGS (Vareforbrug) price spike from June onward, all departments.
    if account_no == 2000 and month >= 6:
        amount = base * 1.13

    return round(amount, 2)


def main():
    os.makedirs(RAW_DIR, exist_ok=True)

    # --- raw_gl.csv : the "actuals" general-ledger export -------------------
    gl_path = os.path.join(RAW_DIR, "raw_gl.csv")
    with open(gl_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["txn_date", "account_no", "account_name", "department", "amount"])
        for month in MONTHS:
            day = random.randint(10, 20)
            txn_date = f"{YEAR}-{month:02d}-{day:02d}"
            for account_no, account_name, _, _ in ACCOUNTS:
                for dept, _, _ in DEPARTMENTS:
                    amount = actual_amount(account_no, dept, month)
                    messy_dept = random.choice(DEPT_NOISE[dept])
                    # amount written as a plain string, as ERP exports often do
                    w.writerow([txn_date, account_no, account_name, messy_dept, f"{amount}"])

    # --- raw_budget.csv : the budget export --------------------------------
    budget_path = os.path.join(RAW_DIR, "raw_budget.csv")
    with open(budget_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["period", "account_no", "department", "budget_amount"])
        for month in MONTHS:
            period = f"{YEAR}-{month:02d}"
            for account_no, _, _, _ in ACCOUNTS:
                for dept, _, _ in DEPARTMENTS:
                    w.writerow([period, account_no, dept, budget_amount(account_no, dept)])

    print(f"Wrote {gl_path}")
    print(f"Wrote {budget_path}")


if __name__ == "__main__":
    main()
