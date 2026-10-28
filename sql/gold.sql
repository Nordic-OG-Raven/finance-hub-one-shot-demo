-- gold.sql : the business-facing star schema + reporting view.
--
-- Gold is what reporting tools (Power BI; Streamlit here)
-- connect to. Conformed dimensions + a single fact table, plus one view that
-- joins everything and computes the actual-vs-budget variance the finance
-- function cares about. This is the layer a Fabric Warehouse would expose.
--
-- Quick vocabulary:
--   * A "fact" table holds the numbers (amounts), one row per event/period.
--   * A "dimension" table holds descriptions (names, groups, regions) that
--     give those numbers context.
--   * A "star schema" is one fact table in the middle with dimension tables
--     around it, joined by keys. Drawn out, it looks like a star.

-- Create a "gold" schema (a folder for tables) if it doesn't exist yet.
-- IF NOT EXISTS means re-running this script won't fail.
CREATE SCHEMA IF NOT EXISTS gold;

-- ---- Dimensions ---------------------------------------------------------

-- Date dimension: one row per month found in the data, with handy labels.
-- date_key is a number like 202410 (YYYYMM), so:
--   * date_key // 100 is whole-number division -> 202410 // 100 = 2024 (year)
--   * date_key % 100  is the remainder         -> 202410 % 100  = 10   (month)
-- The CASE turns the month number into its Danish name (10 -> 'Oktober').
-- The quarter formula maps months 1-3 -> Q1, 4-6 -> Q2, and so on:
--   (month - 1) // 3 + 1, then 'Q' || ... glues the letter Q in front.
-- The FROM part collects every month that appears in EITHER the actuals or
-- the budget. UNION (without ALL) removes duplicates, and DISTINCT makes sure
-- each month ends up only once.
-- CREATE OR REPLACE means: if the table already exists, rebuild it from scratch.
CREATE OR REPLACE TABLE gold.dim_date AS
SELECT DISTINCT
    date_key,
    date_key // 100                     AS year,
    date_key % 100                      AS month,
    CASE date_key % 100
        WHEN 1 THEN 'Januar'   WHEN 2 THEN 'Februar'  WHEN 3 THEN 'Marts'
        WHEN 4 THEN 'April'    WHEN 5 THEN 'Maj'      WHEN 6 THEN 'Juni'
        WHEN 7 THEN 'Juli'     WHEN 8 THEN 'August'   WHEN 9 THEN 'September'
        WHEN 10 THEN 'Oktober' WHEN 11 THEN 'November' ELSE 'December'
    END                                 AS month_name,
    'Q' || CAST(((date_key % 100) - 1) // 3 + 1 AS INTEGER) AS quarter
FROM (SELECT date_key FROM silver.actuals_monthly
      UNION SELECT date_key FROM silver.budget_monthly);

-- Account dimension is a small fixed list — define it inline so the P&L
-- grouping and the revenue/cost sign convention live in one place.
-- (VALUES (...), (...) lets us type rows straight into the query instead of
-- reading them from another table. "AS t(...)" names the columns.
-- Columns: account number, account name, the group it belongs to in the P&L,
-- and whether it's revenue (TRUE) or a cost (FALSE).
-- Translations: Omsætning = Revenue, Vareforbrug = Cost of goods,
-- Personaleomkostninger = Staff costs, Markedsføring = Marketing,
-- Lokaleomkostninger = Premises costs, Øvrige omkostninger = Other costs.)
CREATE OR REPLACE TABLE gold.dim_account AS
SELECT * FROM (VALUES
    (1000, 'Omsætning',             'Omsætning',     TRUE),
    (2000, 'Vareforbrug',           'Vareforbrug',   FALSE),
    (3000, 'Personaleomkostninger', 'Personale',     FALSE),
    (4000, 'Markedsføring',         'Markedsføring', FALSE),
    (5000, 'Lokaleomkostninger',    'Lokaler',       FALSE),
    (6000, 'Øvrige omkostninger',   'Øvrige',        FALSE)
) AS t(account_no, account_name, account_group, is_revenue);

-- Department dimension: the three departments, typed in by hand like above.
-- Each department gets a region (Jylland = Jutland, Sjælland = Zealand,
-- Online) and a sales channel (Detail = Retail store, Webshop = online shop).
CREATE OR REPLACE TABLE gold.dim_department AS
SELECT * FROM (VALUES
    ('Detail Aarhus',    'Jylland',  'Detail'),
    ('Detail København', 'Sjælland', 'Detail'),
    ('Webshop',          'Online',   'Webshop')
) AS t(department, region, channel);

-- ---- Fact ---------------------------------------------------------------
-- One row per month / account / department / scenario. Scenario is a tiny
-- dimension ('Actual' vs 'Budget') that lets the view pivot the two together.

-- Stack the actuals and the budget into ONE table. Both sources have the same
-- columns, so we add a "scenario" label to each row to remember where it came
-- from, then put them on top of each other with UNION ALL.
-- (UNION ALL keeps every row. Plain UNION would drop duplicates, and we never
-- want to lose real amounts.)
CREATE OR REPLACE TABLE gold.fact_finance AS
SELECT date_key, account_no, department, 'Actual' AS scenario, amount_dkk
FROM silver.actuals_monthly
UNION ALL
SELECT date_key, account_no, department, 'Budget' AS scenario, amount_dkk
FROM silver.budget_monthly;

-- ---- Reporting view -----------------------------------------------------
-- The one object most questions should hit. Actual and budget side by side
-- with the variance pre-computed. For costs a positive variance means we
-- OVERSPENT; for revenue a positive variance means we beat the plan.

-- A VIEW is a saved query, not a stored table: every time someone selects
-- from it, the query runs again on the latest data.
CREATE OR REPLACE VIEW gold.v_actual_vs_budget AS
SELECT
    -- Descriptive columns, pulled from the fact table and the three
    -- dimensions. The letters before the dot (f, d, a, dep) are short
    -- nicknames ("aliases") for the tables, set up in the FROM/JOIN part below.
    f.date_key,
    d.year,
    d.month,
    d.month_name,
    d.quarter,
    f.account_no,
    a.account_name,
    a.account_group,
    a.is_revenue,
    f.department,
    dep.region,
    dep.channel,
    -- "Pivot" trick: turn the Actual and Budget rows into two side-by-side
    -- columns. For actual_dkk we add up amounts only from 'Actual' rows
    -- (Budget rows count as 0), and the reverse for budget_dkk.
    SUM(CASE WHEN f.scenario = 'Actual' THEN f.amount_dkk ELSE 0 END) AS actual_dkk,
    SUM(CASE WHEN f.scenario = 'Budget' THEN f.amount_dkk ELSE 0 END) AS budget_dkk,
    -- Variance in kroner: actual minus budget.
    SUM(CASE WHEN f.scenario = 'Actual' THEN f.amount_dkk ELSE 0 END)
      - SUM(CASE WHEN f.scenario = 'Budget' THEN f.amount_dkk ELSE 0 END) AS variance_dkk,
    -- Variance in percent of budget, rounded to 1 decimal.
    -- We write 100.0 (not 100) so the division gives decimals instead of
    -- rounding down to a whole number.
    -- NULLIF(x, 0) turns a budget of 0 into NULL. Dividing by NULL gives NULL
    -- instead of a "division by zero" error, so a zero budget just shows an
    -- empty percentage.
    ROUND(
        100.0 * (
            SUM(CASE WHEN f.scenario = 'Actual' THEN f.amount_dkk ELSE 0 END)
          - SUM(CASE WHEN f.scenario = 'Budget' THEN f.amount_dkk ELSE 0 END)
        ) / NULLIF(SUM(CASE WHEN f.scenario = 'Budget' THEN f.amount_dkk ELSE 0 END), 0),
        1
    ) AS variance_pct
-- Start from the fact table and attach the matching description from each
-- dimension. A JOIN ... ON matches rows where the key columns are equal, e.g.
-- every fact row with date_key 202410 gets the year/month/quarter for 202410.
FROM gold.fact_finance f
JOIN gold.dim_date d        ON f.date_key    = d.date_key
JOIN gold.dim_account a     ON f.account_no  = a.account_no
JOIN gold.dim_department dep ON f.department = dep.department
-- Group by the 12 descriptive columns (the numbers refer to their position in
-- the SELECT list: 1 = date_key, 2 = year, ... 12 = channel). That squashes
-- the Actual row and the Budget row for the same month/account/department
-- into one row, which is what the SUMs above need.
GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12;
