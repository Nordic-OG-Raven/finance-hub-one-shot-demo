"""
etl.py — the medallion pipeline.

Loads the raw CSVs into a DuckDB warehouse and transforms them through three
layers:

    bronze  — raw files landed as-is (strings, messy)   [ingest]
    silver  — cleaned, typed, conformed, monthly grain   [sql/silver.sql]
    gold    — star schema + actual-vs-budget view        [sql/gold.sql]

This is the local stand-in for a Microsoft Fabric medallion architecture:
bronze/silver = Lakehouse, gold = Warehouse, and the SQL files are what a
Fabric notebook or dataflow would run. DuckDB is an embedded SQL engine, so
the whole thing runs in one file with no server to manage.

Run:  python etl.py          (expects data/raw/*.csv — run generate_data.py first)
"""

# Imports: `os` helps us build file paths and check whether files exist,
# and `duckdb` is the database engine we load the data into.
import os
import duckdb

# File locations, all relative to the folder this script lives in, so the
# script works no matter which folder you run it from.
# - HERE:    the folder containing etl.py
# - RAW_DIR: where the input CSV files are (data/raw)
# - DB_PATH: the database file we create (warehouse.duckdb)
# - SQL_DIR: where silver.sql and gold.sql live
# os.path.join glues folder names together with the right slash for your OS.
HERE = os.path.dirname(__file__)
RAW_DIR = os.path.join(HERE, "data", "raw")
DB_PATH = os.path.join(HERE, "warehouse.duckdb")
SQL_DIR = os.path.join(HERE, "sql")


def run_sql_file(con, path):
    """Execute a .sql file one statement at a time.

    DuckDB's Python driver runs a single statement per execute() call, so we
    strip line comments (so a ';' inside a comment can't fool us) and split the
    file on ';'. Keeps the SQL files readable as standalone, commented scripts.
    """
    # Open the .sql file and read the whole thing into one string.
    # The `with` block closes the file automatically when we're done.
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    # Drop `-- ...` line comments before splitting. None of our SQL uses `--`
    # inside a string literal, so this is safe here.
    # (For each line, split at the first "--" and keep only the part before it,
    # then join the lines back together.)
    script = "\n".join(line.split("--", 1)[0] for line in raw.splitlines())
    # Every SQL statement ends with ";", so splitting on ";" gives one piece per
    # statement. Pieces that are empty or only whitespace (e.g. after the last
    # ";") are skipped, and the rest are run one by one.
    for statement in script.split(";"):
        if statement.strip():
            con.execute(statement)


# The main function: builds the whole warehouse from the raw CSVs,
# layer by layer (bronze -> silver -> gold).
def build():
    # Paths to the two input files: the general ledger (raw_gl.csv, the actual
    # bookkeeping entries) and the budget (raw_budget.csv).
    # If either is missing, stop with a helpful message. SystemExit ends the
    # program and prints the text, without a long error traceback.
    raw_gl = os.path.join(RAW_DIR, "raw_gl.csv")
    raw_budget = os.path.join(RAW_DIR, "raw_budget.csv")
    if not (os.path.exists(raw_gl) and os.path.exists(raw_budget)):
        raise SystemExit("Raw data missing. Run `python generate_data.py` first.")

    # Fresh build every run so the demo is reproducible.
    # (Delete the old database file if there is one, so we always start empty.)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    # Open (and create) the database file. `con` is our connection, used for
    # every query below.
    con = duckdb.connect(DB_PATH)

    # ---- BRONZE: land the raw files exactly as they are --------------------
    # all_varchar keeps everything as text, so the "cleaning" genuinely happens
    # in silver rather than being hidden by the CSV reader's type inference.
    # (read_csv_auto reads a CSV file straight into a table. The f"..." strings
    # insert the file path into the SQL text.)
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    con.execute(
        f"CREATE OR REPLACE TABLE bronze.raw_gl AS "
        f"SELECT * FROM read_csv_auto('{raw_gl}', all_varchar=true)"
    )
    con.execute(
        f"CREATE OR REPLACE TABLE bronze.raw_budget AS "
        f"SELECT * FROM read_csv_auto('{raw_budget}', all_varchar=true)"
    )
    print("bronze : raw_gl, raw_budget loaded")

    # ---- SILVER + GOLD: transform via the SQL scripts ----------------------
    # Run silver.sql first, because gold.sql reads from the silver tables.
    # The print lines are progress messages for whoever runs the script.
    run_sql_file(con, os.path.join(SQL_DIR, "silver.sql"))
    print("silver : actuals_monthly, budget_monthly built")
    run_sql_file(con, os.path.join(SQL_DIR, "gold.sql"))
    print("gold   : star schema + v_actual_vs_budget built")

    # ---- Quick sanity check ------------------------------------------------
    # Two small queries to prove the build worked:
    # 1. Count the rows in the final view. fetchone() returns the first row as a
    #    tuple, and [0] takes its first (and only) value.
    rows = con.execute("SELECT COUNT(*) FROM gold.v_actual_vs_budget").fetchone()[0]
    # 2. Total actual revenue (omsaetning) and total actual costs (omkostninger)
    #    for the whole year. The CASE picks which rows go into which total.
    full_year = con.execute(
        """
        SELECT
            SUM(CASE WHEN is_revenue THEN actual_dkk ELSE 0 END)  AS omsaetning,
            SUM(CASE WHEN NOT is_revenue THEN actual_dkk ELSE 0 END) AS omkostninger
        FROM gold.v_actual_vs_budget
        """
    ).fetchone()
    # We're done with the database, so close the connection.
    con.close()

    # Print a summary in Danish: "Done. N rows in ...", then revenue, costs and
    # profit (Resultat = revenue minus costs).
    # In the f-strings, `:>14,.0f` means: right-align in 14 characters, add
    # thousands separators, and show no decimals, so the numbers line up.
    print(f"\nFærdig. {rows} rækker i gold.v_actual_vs_budget")
    print(f"  Omsætning (actual):   {full_year[0]:>14,.0f} kr.")
    print(f"  Omkostninger (actual):{full_year[1]:>14,.0f} kr.")
    print(f"  Resultat (actual):    {full_year[0] - full_year[1]:>14,.0f} kr.")
    print(f"\nWarehouse: {DB_PATH}")


# This block only runs when you start the file directly (`python etl.py`),
# not when another file imports it. That way other code can import
# run_sql_file or build without kicking off a full rebuild.
if __name__ == "__main__":
    build()
