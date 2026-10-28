"""
app.py — the dashboard + AI front end (the Power BI stand-in).

Three tabs:
  * Overblik        — actual vs budget KPIs, chart, and a variance table.
  * Spørg data (AI) — ask a question in Danish; the NL2SQL agent answers.
  * Forklar afvigelse (AI) — pick a cost/revenue line; the AI drafts a
                             plain-Danish explanation of the variance.

All reads go through a READ-ONLY DuckDB connection, so nothing in the UI — or
anything the AI generates — can modify the warehouse.

Run:  streamlit run app.py
"""

import os
import altair as alt
import duckdb
import pandas as pd
import streamlit as st

import ai_agent

DB_PATH = os.path.join(os.path.dirname(__file__), "warehouse.duckdb")

st.set_page_config(page_title="Nordlys Møbler — Finansoverblik", page_icon="📊", layout="wide")


@st.cache_resource
def get_con():
    """One shared read-only connection to the gold warehouse."""
    return duckdb.connect(DB_PATH, read_only=True)


def kr(x):
    return f"{x:,.0f} kr.".replace(",", ".")


if not os.path.exists(DB_PATH):
    st.error("Warehouse mangler. Kør `python generate_data.py` og derefter `python etl.py` først.")
    st.stop()

con = get_con()

st.title("📊 Nordlys Møbler A/S — Finansoverblik 2024")
st.caption(
    "Demo: medallion-pipeline (bronze → silver → gold) i DuckDB, dashboard i Streamlit, "
    "og et AI-lag der forklarer afvigelser — samme mønster som en Fabric + Power BI-løsning."
)

tab_overblik, tab_spoerg, tab_forklar = st.tabs(
    ["Overblik", "Spørg data (AI)", "Forklar afvigelse (AI)"]
)

# ---------------------------------------------------------------------------
# TAB 1 — Overblik
# ---------------------------------------------------------------------------
with tab_overblik:
    full = con.execute(
        """
        SELECT account_group, is_revenue,
               SUM(actual_dkk) AS actual_dkk,
               SUM(budget_dkk) AS budget_dkk,
               SUM(variance_dkk) AS variance_dkk
        FROM gold.v_actual_vs_budget
        GROUP BY account_group, is_revenue
        ORDER BY is_revenue DESC, actual_dkk DESC
        """
    ).fetchdf()

    omsaetning = full.loc[full.is_revenue, "actual_dkk"].sum()
    oms_budget = full.loc[full.is_revenue, "budget_dkk"].sum()
    omkost = full.loc[~full.is_revenue, "actual_dkk"].sum()
    omkost_budget = full.loc[~full.is_revenue, "budget_dkk"].sum()
    resultat = omsaetning - omkost
    resultat_budget = oms_budget - omkost_budget

    c1, c2, c3 = st.columns(3)
    c1.metric("Omsætning", kr(omsaetning), kr(omsaetning - oms_budget) + " vs. budget")
    c2.metric("Omkostninger", kr(omkost), kr(omkost - omkost_budget) + " vs. budget",
              delta_color="inverse")
    c3.metric("Resultat", kr(resultat), kr(resultat - resultat_budget) + " vs. budget")

    st.subheader("Realiseret vs. budget pr. kontogruppe")
    chart_df = full.melt(
        id_vars=["account_group"],
        value_vars=["actual_dkk", "budget_dkk"],
        var_name="scenarie",
        value_name="beloeb",
    ).replace({"actual_dkk": "Realiseret", "budget_dkk": "Budget"})
    chart = (
        alt.Chart(chart_df)
        .mark_bar()
        .encode(
            x=alt.X("account_group:N", title="Kontogruppe", sort="-y"),
            y=alt.Y("beloeb:Q", title="DKK"),
            color=alt.Color("scenarie:N", title=""),
            xOffset="scenarie:N",
            tooltip=["account_group", "scenarie", alt.Tooltip("beloeb:Q", format=",.0f")],
        )
        .properties(height=340)
    )
    st.altair_chart(chart, use_container_width=True)

    st.subheader("Største afvigelser (hele året)")
    var_df = full.copy()
    var_df["afvigelse %"] = (100 * var_df.variance_dkk / var_df.budget_dkk).round(1)
    var_df = var_df.reindex(var_df.variance_dkk.abs().sort_values(ascending=False).index)
    show = var_df[["account_group", "actual_dkk", "budget_dkk", "variance_dkk", "afvigelse %"]]
    show = show.rename(
        columns={
            "account_group": "Kontogruppe",
            "actual_dkk": "Realiseret",
            "budget_dkk": "Budget",
            "variance_dkk": "Afvigelse",
        }
    )
    st.dataframe(
        show.style.format(
            {"Realiseret": "{:,.0f}", "Budget": "{:,.0f}", "Afvigelse": "{:,.0f}", "afvigelse %": "{:+.1f}%"}
        ),
        use_container_width=True,
        hide_index=True,
    )

# ---------------------------------------------------------------------------
# TAB 2 — Spørg data (NL2SQL)
# ---------------------------------------------------------------------------
with tab_spoerg:
    st.subheader("Stil et spørgsmål til tallene — på dansk")
    st.caption(
        "AI'en skriver SQL mod gold-laget, vi kører den read-only, og AI'en forklarer svaret. "
        "SQL'en vises, så en bruger altid kan se og godkende, hvad der blev spurgt om."
    )
    examples = [
        "Hvilken kontogruppe har den største afvigelse fra budgettet?",
        "Hvordan udviklede markedsføringsomkostningerne sig måned for måned?",
        "Hvilken afdeling missede sit omsætningsbudget mest i Q4?",
    ]
    question = st.text_input("Spørgsmål", value=examples[0])
    with st.expander("Eksempler"):
        for e in examples:
            st.write("• " + e)

    if st.button("Spørg", type="primary"):
        try:
            with st.spinner("AI skriver SQL og analyserer…"):
                sql, df, answer = ai_agent.nl2sql(question, con)
            st.markdown("**Svar**")
            st.info(answer)
            st.markdown("**Resultat**")
            st.dataframe(df, use_container_width=True, hide_index=True)
            with st.expander("Vis genereret SQL (til godkendelse)"):
                st.code(sql, language="sql")
        except Exception as exc:  # noqa: BLE001 — surface any AI/SQL error to the user
            st.error(f"Kunne ikke besvare spørgsmålet: {exc}")

# ---------------------------------------------------------------------------
# TAB 3 — Forklar afvigelse (variance explainer)
# ---------------------------------------------------------------------------
with tab_forklar:
    st.subheader("Lad AI'en udarbejde et udkast til afvigelsesforklaring")
    st.caption(
        "Vælg en kontogruppe. AI'en forklarer, hvad der sandsynligvis driver afvigelsen, "
        "ud fra den månedlige og afdelingsvise fordeling. Det er et UDKAST — "
        "økonomifunktionen godkender."
    )

    groups = [r[0] for r in con.execute(
        "SELECT DISTINCT account_group FROM gold.v_actual_vs_budget ORDER BY 1"
    ).fetchall()]
    group = st.selectbox("Kontogruppe", groups, index=groups.index("Markedsføring")
                         if "Markedsføring" in groups else 0)

    totals = con.execute(
        """
        SELECT SUM(actual_dkk) AS actual_dkk, SUM(budget_dkk) AS budget_dkk,
               SUM(variance_dkk) AS variance_dkk
        FROM gold.v_actual_vs_budget WHERE account_group = ?
        """,
        [group],
    ).fetchdf().iloc[0]
    var_pct = 100 * totals.variance_dkk / totals.budget_dkk if totals.budget_dkk else 0

    m1, m2, m3 = st.columns(3)
    m1.metric("Budget", kr(totals.budget_dkk))
    m2.metric("Realiseret", kr(totals.actual_dkk))
    m3.metric("Afvigelse", kr(totals.variance_dkk), f"{var_pct:+.1f}%", delta_color="inverse")

    by_month = con.execute(
        """
        SELECT month, month_name,
               SUM(actual_dkk) AS realiseret, SUM(budget_dkk) AS budget,
               SUM(variance_dkk) AS afvigelse
        FROM gold.v_actual_vs_budget WHERE account_group = ?
        GROUP BY month, month_name ORDER BY month
        """,
        [group],
    ).fetchdf()
    by_dept = con.execute(
        """
        SELECT department AS afdeling,
               SUM(actual_dkk) AS realiseret, SUM(budget_dkk) AS budget,
               SUM(variance_dkk) AS afvigelse
        FROM gold.v_actual_vs_budget WHERE account_group = ?
        GROUP BY department ORDER BY afvigelse DESC
        """,
        [group],
    ).fetchdf()

    cc1, cc2 = st.columns(2)
    with cc1:
        st.caption("Månedlig udvikling")
        st.dataframe(by_month[["month_name", "realiseret", "budget", "afvigelse"]]
                     .rename(columns={"month_name": "måned"}),
                     use_container_width=True, hide_index=True)
    with cc2:
        st.caption("Pr. afdeling")
        st.dataframe(by_dept, use_container_width=True, hide_index=True)

    if st.button("Generér forklaring (udkast)", type="primary"):
        try:
            with st.spinner("AI udarbejder udkast…"):
                explanation = ai_agent.explain_variance(
                    {
                        "account_group": group,
                        "period": "Regnskabsåret 2024",
                        "budget_dkk": float(totals.budget_dkk),
                        "actual_dkk": float(totals.actual_dkk),
                        "variance_dkk": float(totals.variance_dkk),
                        "variance_pct": float(var_pct),
                        "by_month_md": by_month[["month_name", "realiseret", "budget", "afvigelse"]]
                        .to_markdown(index=False),
                        "by_dept_md": by_dept.to_markdown(index=False),
                    }
                )
            st.success("Udkast til afvigelsesforklaring (til godkendelse):")
            st.write(explanation)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Kunne ikke generere forklaring: {exc}")
