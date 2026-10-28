-- silver.sql : clean and conform the raw bronze data.
--
-- Bronze is the raw landing zone (strings, messy department names, one row per
-- posting). Silver is where we type-cast, normalise departments, and aggregate
-- to the monthly grain the business actually reports on. This is the same job
-- a Fabric notebook / dataflow does between the Lakehouse bronze and silver
-- layers — done here in plain SQL so every step is visible.
--
-- Quick vocabulary:
--   * "Type-cast" (CAST) = convert a value from one type to another, e.g. the
--     text '1250.50' into the number 1250.50. Bronze stores everything as text.
--   * "Grain" = what one row represents. Bronze has one row per posting;
--     silver has one row per month + account + department.

-- Create the "silver" schema (a folder for tables) if it doesn't exist yet.
CREATE SCHEMA IF NOT EXISTS silver;

-- Actuals: cast amount to a number, derive a YYYYMM month key, and map the
-- messy free-text department onto a canonical name.
-- Line by line:
--   * date_key: turn the text date into a real DATE, format it as 'YYYYMM'
--     with strftime (e.g. '2024-10-15' -> '202410'), then make it a number.
--   * account_no: text -> whole number.
--   * department: people typed department names in different ways ('web shop',
--     ' Webshop ', 'Detail Koebenhavn'...). lower() makes it lowercase and
--     trim() removes spaces at the start and end, then the CASE maps each
--     known spelling to ONE official name. Unknown names are kept (trimmed),
--     so they still show up and can be spotted.
--   * amount_dkk: text -> decimal number, then SUM adds up all postings.
-- WHERE skips rows with no date, since we can't place them in a month.
-- GROUP BY 1, 2, 3 groups by the first three columns (month, account,
-- department), so every posting in the same month/account/department is
-- added together into one row.
CREATE OR REPLACE TABLE silver.actuals_monthly AS
SELECT
    CAST(strftime(CAST(txn_date AS DATE), '%Y%m') AS INTEGER) AS date_key,
    CAST(account_no AS INTEGER)                               AS account_no,
    CASE lower(trim(department))
        WHEN 'detail aarhus'     THEN 'Detail Aarhus'
        WHEN 'detail københavn'  THEN 'Detail København'
        WHEN 'detail koebenhavn' THEN 'Detail København'
        WHEN 'webshop'           THEN 'Webshop'
        WHEN 'web shop'          THEN 'Webshop'
        ELSE trim(department)
    END                                                      AS department,
    SUM(CAST(trim(amount) AS DOUBLE))                        AS amount_dkk
FROM bronze.raw_gl
WHERE txn_date IS NOT NULL
GROUP BY 1, 2, 3;

-- Budget is already at the monthly grain; just type it and key it.
-- Line by line:
--   * date_key: the period looks like '2024-10'. replace() removes the dash
--     ('202410'), then CAST makes it a number, matching the actuals key.
--   * account_no: text -> whole number.
--   * department: just trim spaces (the budget names are already clean).
--   * amount_dkk: text -> decimal number, summed in case a month/account/
--     department appears more than once.
-- GROUP BY 1, 2, 3 works the same way as in the actuals table above.
CREATE OR REPLACE TABLE silver.budget_monthly AS
SELECT
    CAST(replace(period, '-', '') AS INTEGER) AS date_key,
    CAST(account_no AS INTEGER)               AS account_no,
    trim(department)                          AS department,
    SUM(CAST(budget_amount AS DOUBLE))        AS amount_dkk
FROM bronze.raw_budget
GROUP BY 1, 2, 3;
