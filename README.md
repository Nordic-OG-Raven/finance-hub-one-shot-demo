# Nordlys Møbler — AI-afvigelsesforklaring (demo)

A small, end-to-end finance analytics pipeline built to show at an Ascendit
conversation. It mirrors the pattern Ascendit sells — **finansdata → ren
datamodel (medallion / stjerneskema) → rapportering → et AI-lag der forklarer
tallene** — but is entirely my own build, runs locally, and I can walk through
every line.

It is deliberately the same shape as Ascendit's **Finance Hub** product
(actual-vs-budget, afvigelsesforklaring med AI, "økonomifunktionen godkender"),
and the same shape as the **Senior Data Engineer** role's stack (SQL + Python
ETL, medallion/Fabric-style layers, a reporting layer on top).

```
  data/raw/*.csv        ← ERP-style exports (general ledger + budget)
        │  generate_data.py
        ▼
  ┌─────────────┐  ┌─────────────┐  ┌───────────────────────────┐
  │   BRONZE    │→ │   SILVER    │→ │           GOLD            │
  │ raw, as-is  │  │ cleaned,    │  │ star schema + view        │
  │ (strings)   │  │ conformed,  │  │ dim_date / dim_account /  │
  │             │  │ monthly     │  │ dim_department / fact +   │
  │             │  │             │  │ v_actual_vs_budget        │
  └─────────────┘  └─────────────┘  └───────────────────────────┘
        etl.py · sql/silver.sql · sql/gold.sql   (DuckDB)
        │
        ▼
  app.py (Streamlit)            ai_agent.py (Claude)
  ├─ Overblik: actual vs budget ├─ NL2SQL: spørg data på dansk
  ├─ Spørg data (AI) ───────────┤   (model skriver SQL, vi kører read-only)
  └─ Forklar afvigelse (AI) ────┘   variance-forklaring som udkast
```

## How this maps to Microsoft Fabric

The demo runs locally on **DuckDB** so it is self-contained and explainable,
but each piece has a direct Fabric equivalent:

| Here (local)                 | Fabric equivalent                          |
| ---------------------------- | ------------------------------------------ |
| `data/raw/*.csv`             | Files landed in **OneLake**                |
| bronze schema (raw, as-is)   | **Lakehouse** bronze                       |
| silver schema (cleaned)      | **Lakehouse** silver (notebook / dataflow) |
| gold star schema + view      | **Warehouse** gold (serving layer)         |
| `sql/*.sql` transformations  | Fabric SQL / notebook (Spark/SQL)          |
| Streamlit dashboard          | **Power BI** report on the gold model      |
| `ai_agent.py`                | Azure OpenAI / Copilot on top of the model |

## Run it

```bash
pip install -r requirements.txt

python generate_data.py     # write the raw CSV exports
python etl.py               # build the DuckDB warehouse (bronze→silver→gold)

cp .env.example .env        # then add your own Anthropic API key to .env
streamlit run app.py        # open the dashboard
```

The dashboard works without a key (Overblik tab); the two AI tabs read
`ANTHROPIC_API_KEY` from `.env`. Set `ASCENDIT_DEMO_MODEL=claude-haiku-4-5`
in `.env` to run the AI cheaply.

## The data (and what the AI should find)

Synthetic FY2024 for a fictional furniture retailer, with three deliberate
"stories" baked into the numbers so the variance explainer has something real
to say:

1. **Markedsføring** overspent in H2 — a Webshop growth push.
2. **Detail København** missed its revenue budget in Q4.
3. **Vareforbrug** (COGS) spiked from June on a supplier price increase.

## Files

| File                | What it does                                              |
| ------------------- | -------------------------------------------------------- |
| `generate_data.py`  | Writes the raw GL + budget CSVs                           |
| `sql/silver.sql`    | Clean / type / conform / monthly aggregate               |
| `sql/gold.sql`      | Star schema (dims + fact) + `v_actual_vs_budget` view     |
| `etl.py`            | Orchestrates bronze → silver → gold in DuckDB            |
| `ai_agent.py`       | NL2SQL + variance explainer (Anthropic SDK)              |
| `app.py`            | Streamlit dashboard + AI tabs                            |
| `.env.example`      | Copy to `.env` and add your Anthropic API key            |
