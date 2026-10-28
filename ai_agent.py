"""
ai_agent.py — the AI layer on top of the gold warehouse.

Two capabilities, both mirroring Finance Hub:

    1. nl2sql()          — a finance user asks a question in plain Danish; the
                           model writes SQL against the gold layer, we run it
                           read-only, and the model explains the result.
    2. explain_variance()— given an actual-vs-budget gap, the model drafts a
                           plain-Danish explanation of what likely drove it.

Design choices:
  * The model never touches the database directly; it returns read-only SQL,
    and we validate it is safe before running it on a read-only DuckDB connection.
    That is the "finance always approves" guardrail in code.
  * Claude Opus 4.8 by default (override with ASCENDIT_DEMO_MODEL). The API
    key is read from ANTHROPIC_API_KEY — e.g. a local .env (see .env.example).

"""

# Imports: `os` lets us read environment variables, `re` gives us regular
# expressions (text pattern matching), and `anthropic` is the official library
# for talking to Claude.
import os
import re
import anthropic
from dotenv import load_dotenv

# Load ANTHROPIC_API_KEY (and any ASCENDIT_DEMO_MODEL override) from a local
# .env file next to this script, so the app works on startup without needing
# the key exported in the shell first.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Which Claude model to use. We first check for an environment variable called
# ASCENDIT_DEMO_MODEL, so you can switch models without editing code. If it
# isn't set, we fall back to "claude-opus-4-8".
MODEL = os.environ.get("ASCENDIT_DEMO_MODEL", "claude-opus-4-8")

# A compact description of the gold layer, handed to the model so it can write
# correct SQL. Keeping this in one place (and in sync with sql/gold.sql) is the
# whole contract between the warehouse and the AI.
# (In short: the model can't see the database, so this text tells it which
# table exists, what each column means, and what the data covers. It's in
# Danish because the users ask their questions in Danish.)
SCHEMA_DOC = """\
Du skriver DuckDB-SQL mod et "gold"-lag i en finans-dataplatform.
Brug ALTID denne view til spørgsmål om tal, medmindre andet er umuligt:

gold.v_actual_vs_budget (én række pr. måned / konto / afdeling):
  date_key      INTEGER  -- YYYYMM, fx 202410
  year          INTEGER
  month         INTEGER  -- 1-12
  month_name    VARCHAR  -- dansk månedsnavn, fx 'Oktober'
  quarter       VARCHAR  -- 'Q1'..'Q4'
  account_no    INTEGER
  account_name  VARCHAR  -- fx 'Markedsføring'
  account_group VARCHAR  -- 'Omsætning','Vareforbrug','Personale','Markedsføring','Lokaler','Øvrige'
  is_revenue    BOOLEAN  -- TRUE for omsætning, ellers FALSE (omkostning)
  department    VARCHAR  -- 'Detail Aarhus','Detail København','Webshop'
  region        VARCHAR  -- 'Jylland','Sjælland','Online'
  channel       VARCHAR  -- 'Detail','Webshop'
  actual_dkk    DOUBLE   -- realiseret beløb
  budget_dkk    DOUBLE   -- budgetteret beløb
  variance_dkk  DOUBLE   -- actual - budget (positiv = merforbrug for omkostninger / bedre end budget for omsætning)
  variance_pct  DOUBLE   -- afvigelse i procent af budget

Data dækker regnskabsåret 2024, selskabet "Nordlys Møbler A/S" (møbelforhandler).
Alle beløb er i DKK.
"""

# A list of "dangerous" SQL words that could change or delete data, or reach
# outside the database (like loading files or extensions). If any of these
# words appear in the model's SQL, we refuse to run it.
# - `\b` means "word boundary", so we match whole words only: "update" is
#   caught, but a column like "updated_at" is not.
# - re.IGNORECASE means "DELETE", "delete" and "DeLeTe" are all caught.
# The leading underscore in the name is a Python convention meaning "this is
# only meant to be used inside this file".
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|copy|pragma|replace|"
    r"truncate|grant|install|load|export|call)\b",
    re.IGNORECASE,
)

# Creates a connection object for talking to Claude. It reads the API key from
# ANTHROPIC_API_KEY. If the key is a user-scoped key (not tied to one
# workspace), Anthropic requires an anthropic-workspace-id header, so we pass
# ANTHROPIC_WORKSPACE_ID along when it is set.
def _client():
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
    return anthropic.Anthropic(default_headers=headers)


# Claude's reply comes back as a list of "content blocks" (text, tool calls,
# etc.). This helper keeps only the text blocks, glues them together into a
# single string, and trims whitespace from the start and end.
def _text(response):
    return "".join(b.text for b in response.content if b.type == "text").strip()


def is_safe_select(sql: str) -> bool:
    """Allow only a single read-only SELECT/WITH query. This is the gate that
    lets us run model-written SQL without risking the warehouse."""
    # Clean up the query: trim spaces, drop one trailing semicolon, trim again.
    s = sql.strip().rstrip(";").strip()
    # If there's still a semicolon, the model tried to sneak in a second
    # statement (e.g. "SELECT ...; DROP TABLE ..."). Reject it.
    if ";" in s:  # no stacked statements
        return False
    # The query must start with SELECT or WITH, the two ways to write a
    # read-only query. Anything else gets rejected.
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        return False
    # Reject the query if any dangerous word from _FORBIDDEN appears anywhere.
    if _FORBIDDEN.search(s):
        return False
    # Passed every check, so the query is safe to run.
    return True


def _extract_sql(raw: str) -> str:
    """Pull SQL out of the model reply, tolerating a ```sql fence if present."""
    # Models sometimes wrap code in markdown fences like ```sql ... ```.
    # This pattern looks for that and captures just the part in between.
    # re.DOTALL lets "." also match newlines, so multi-line SQL works.
    m = re.search(r"```(?:sql)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    # If we found a fence, use what was inside it. Otherwise assume the whole
    # reply is SQL. Either way, trim whitespace and a trailing semicolon.
    return (m.group(1) if m else raw).strip().rstrip(";").strip()


def nl2sql(question: str, con):
    """Answer a Danish finance question against the gold layer.

    Returns (sql, dataframe, answer_da). Raises ValueError if the generated SQL
    is not a safe read-only SELECT. `con` must be a read-only DuckDB connection.
    """
    # Get a Claude client. We use it twice below: once to write SQL and once
    # to explain the result.
    client = _client()

    # Step 1: model writes the SQL.
    # We send the schema description plus instructions as the "system" prompt
    # (the rules the model should follow), and the user's question as the
    # message. The Danish instructions say: "Write ONE read-only SELECT query
    # in DuckDB dialect that answers the user's question. Return ONLY SQL,
    # no explanation, no markdown."
    gen = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=(
            SCHEMA_DOC
            + "\nSkriv ÉN read-only SELECT-forespørgsel i DuckDB-dialekt der besvarer "
            "brugerens spørgsmål. Returnér KUN SQL — ingen forklaring, ingen markdown."
        ),
        messages=[{"role": "user", "content": question}],
    )
    # Pull the text out of the reply, strip any ``` fences, then run the safety
    # check. If the SQL fails the check we stop here with an error, so unsafe
    # SQL never reaches the database. (The Danish message means "Generated SQL
    # was rejected by the safety check".)
    sql = _extract_sql(_text(gen))
    if not is_safe_select(sql):
        raise ValueError(f"Genereret SQL blev afvist af sikkerhedstjek:\n{sql}")

    # Step 2: run it ourselves (read-only), so the model never touches the DB.
    # .fetchdf() returns the result as a pandas DataFrame (a table in Python).
    df = con.execute(sql).fetchdf()

    # Step 3: model explains the result in plain Danish.
    # We only send the first 50 rows, formatted as a markdown table, so we
    # don't send the model a huge amount of data.
    preview = df.head(50).to_markdown(index=False)
    # Second call to Claude: give it the question and the result table, and
    # ask for a short answer. The Danish instructions say: "You are a financial
    # analyst. Answer briefly and precisely in Danish based on the result.
    # Mention concrete numbers (rounded to the nearest thousand kr.). Don't
    # oversell; if the result is empty, say so."
    ans = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=(
            "Du er en finansanalytiker. Svar kort og præcist på dansk ud fra "
            "resultatet. Nævn konkrete tal (afrund til hele tusinde kr.). "
            "Oversælg ikke; hvis resultatet er tomt, sig det."
        ),
        messages=[
            {
                "role": "user",
                "content": f"Spørgsmål: {question}\n\nResultat:\n{preview}",
            }
        ],
    )
    # Return three things: the SQL that was run, the full result table, and
    # the model's Danish explanation.
    return sql, df, _text(ans)


def explain_variance(context: dict) -> str:
    """Draft a plain-Danish explanation of a single actual-vs-budget variance.

    `context` carries the headline numbers plus a monthly and per-department
    breakdown (as markdown tables) so the model can reason about *what drove*
    the gap, not just restate it. The output is explicitly a draft for the
    finance function to review — the Finance Hub "økonomifunktionen godkender"
    pattern.
    """
    client = _client()
    # Build the message for the model from the values in `context`.
    # In the f-string:
    # - `:,.0f` formats a number with thousands separators and no decimals
    #   (1234567.8 -> "1,234,568").
    # - `:+.1f` shows one decimal and always includes the sign (+4.2 / -3.1).
    # The labels are Danish: account/group, period, budget, actual, variance,
    # monthly trend, and per department.
    prompt = f"""\
Konto / gruppe: {context['account_group']}
Periode: {context['period']}
Budget: {context['budget_dkk']:,.0f} kr.
Realiseret: {context['actual_dkk']:,.0f} kr.
Afvigelse: {context['variance_dkk']:,.0f} kr. ({context['variance_pct']:+.1f}%)

Månedlig udvikling:
{context['by_month_md']}

Pr. afdeling:
{context['by_dept_md']}
"""
    # Ask Claude to write the explanation. The Danish system prompt says:
    # "You are a financial analyst at a furniture company. Write a short draft
    # (3-5 sentences) in Danish explaining what is likely driving the variance,
    # based ON THE NUMBERS you are given, and point to the months/departments
    # that contribute most. Suggest one concrete follow-up question. Be sober:
    # this is a draft for the finance team to approve, not a final conclusion.
    # Don't make up numbers that aren't in the data."
    resp = client.messages.create(
        model=MODEL,
        max_tokens=700,
        system=(
            "Du er finansanalytiker i en møbelvirksomhed. Skriv et kort udkast "
            "(3-5 sætninger) på dansk der forklarer, hvad der sandsynligvis "
            "driver afvigelsen, baseret PÅ TALLENE du får — peg på de måneder/"
            "afdelinger der bidrager mest. Foreslå ét konkret opfølgningsspørgsmål. "
            "Vær nøgtern: det er et udkast, som økonomifunktionen skal godkende, "
            "ikke en endelig konklusion. Find ikke på tal der ikke står i data."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    # Return only the text of the model's reply.
    return _text(resp)
