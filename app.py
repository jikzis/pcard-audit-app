"""OSU P-card audit app: ask questions in natural language, and search for prohibited purchases."""
import gzip
import hmac
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st

DB_GZ = Path(__file__).parent / "data" / "pcards.db.gz"
MODEL = "claude-opus-5-5"
MAX_ROWS = 1000
QUERY_TIMEOUT_S = 20

st.set_page_config(page_title="P-card Audit", page_icon="🔎", layout="wide")


# ---------------------------------------------------------------- access

def secret(name: str) -> str | None:
    try:
        value = st.secrets.get(name)
    except Exception:  # no secrets file locally
        value = None
    return value or os.environ.get(name)


def require_password():
    """Stop the script until the viewer has entered the app password (fails closed if none is set)."""
    if st.session_state.get("authenticated"):
        return
    expected = secret("APP_PASSWORD")
    st.title("🔎 OSU P-card Audit")
    if not expected:
        st.error("This app is locked: no `APP_PASSWORD` is configured in the app's secrets.")
        st.stop()
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Open", type="primary")
    if submitted:
        if hmac.compare_digest(entered.encode(), expected.encode()):
            st.session_state["authenticated"] = True
            st.rerun()
        time.sleep(2)  # slow down guessing
        st.error("Wrong password.")
    st.stop()


require_password()


# ---------------------------------------------------------------- database

@st.cache_resource
def db_path() -> str:
    """Decompress the bundled database once per server process."""
    path = Path(tempfile.gettempdir()) / "pcards_audit.db"
    if not path.exists() or path.stat().st_size == 0:
        with gzip.open(DB_GZ, "rb") as src, open(path, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return str(path)


def connect() -> sqlite3.Connection:
    # read-only: nothing the app (or a generated query) runs can change the data
    con = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only = ON")
    return con


def run_query(sql: str, params=()) -> pd.DataFrame:
    con = connect()
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame(cur.fetchmany(MAX_ROWS), columns=cols)
    finally:
        con.close()


MONEY_NAME = re.compile(r"amount|total|spent|spend|sum|cost|price|value|avg|average|charge|fee|paid|payment|refund",
                        re.I)
NOT_MONEY_NAME = re.compile(r"count|transactions|number|month|year|^id$|pct|percent|ratio|times", re.I)


def money_config(df: pd.DataFrame) -> dict:
    """Show numeric columns that hold dollar amounts as $1,234.56."""
    return {c: st.column_config.NumberColumn(format="dollar") for c in df.columns
            if pd.api.types.is_float_dtype(df[c]) and MONEY_NAME.search(c) and not NOT_MONEY_NAME.search(c)}


@st.cache_data
def years() -> list[int]:
    return run_query("SELECT DISTINCT Year FROM pcards ORDER BY Year DESC")["Year"].tolist()


# ---------------------------------------------------------------- dashboard

# Prohibited purchase categories with keyword suggestions for each search field
PROHIBITED = {
    "Alcohol": {"description": ["beer", "wine", "liquor", "vodka", "whiskey"],
                "vendor": ["liquor", "wine", "bottle shop", "brewing", "bar"]},
    "Cash, cash advances and ATM": {"description": ["cash", "atm", "advance"],
                                    "vendor": ["atm", "cash", "western union"]},
    "Decorations": {"description": ["decoration", "balloon", "ornament", "christmas"],
                    "vendor": ["party", "decor", "balloon", "hobby lobby"]},
    "Donations and sponsorships": {"description": ["donation", "sponsor", "contribution"],
                                   "vendor": ["foundation", "charity", "giving", "united way"]},
    "Gasoline": {"description": ["fuel", "gasoline", "unleaded", "diesel"],
                 "vendor": ["oil", "fuel", "quiktrip", "shell", "phillips 66", "conoco"]},
    "Gifts, gift cards and gift certificates": {"description": ["gift card", "gift certificate", "gift"],
                                                "vendor": ["gift", "florist", "flowers", "card"]},
    "Insurance": {"description": ["insurance", "premium", "policy"],
                  "vendor": ["insurance", "geico", "state farm", "allstate"]},
    "Late fees": {"description": ["late fee", "late charge", "finance charge", "penalty"],
                  "vendor": ["late fee"]},
    "Mail and postage": {"description": ["postage", "stamps", "shipping"],
                         "vendor": ["usps", "post office", "postal", "stamps.com", "pitney"]},
    "Moving expenses": {"description": ["moving", "relocation"],
                        "vendor": ["moving", "u-haul", "movers", "pods"]},
    "Personal purchases": {"description": ["personal", "netflix", "spotify", "itunes"],
                           "vendor": ["netflix", "spotify", "itunes", "jewelry", "salon"]},
    "Personal and individual memberships / dues": {"description": ["membership", "dues", "renewal"],
                                                  "vendor": ["gym", "fitness", "yoga", "country club", "ymca"]},
    "Salaries, wages and benefits": {"description": ["salary", "wages", "payroll", "stipend"],
                                     "vendor": ["payroll", "adp", "paychex"]},
    "Service and incentive awards": {"description": ["award", "plaque", "trophy", "incentive"],
                                     "vendor": ["trophy", "awards", "plaque", "engraving"]},
}

RESULT_COLUMNS = ("FullName, TransactionDate, PostedDate, Amount, Vendor, Description, MCC, "
                  "CardholderLastName, CardholderFirstInitial, ID")


def search(field: str, keyword: str, year: int) -> pd.DataFrame:
    sql = (f"SELECT {RESULT_COLUMNS} FROM pcards WHERE Year = ? AND {field} LIKE ? "
           "ORDER BY Amount DESC")
    return run_query(sql, (year, f"%{keyword.strip()}%"))


def show_results(df: pd.DataFrame, keyword: str, field: str, year: int, key: str):
    if df.empty:
        st.info(f"No {year} transactions where the {field.lower()} contains “{keyword}”.")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Transactions", f"{len(df):,}" + ("+" if len(df) == MAX_ROWS else ""))
    c2.metric("Total amount", f"${df['Amount'].sum():,.2f}")
    c3.metric("Cardholders", f"{df['FullName'].nunique():,}")
    c4.metric("Vendors", f"{df['Vendor'].nunique():,}")
    if len(df) == MAX_ROWS:
        st.caption(f"Showing the {MAX_ROWS:,} largest transactions. Use a more specific keyword to narrow the search.")

    st.dataframe(df, use_container_width=True, hide_index=True,
                 column_config=money_config(df))

    with st.expander("Follow-up: totals by cardholder"):
        by_holder = (df.groupby("FullName")
                     .agg(Transactions=("Amount", "size"), Total=("Amount", "sum"),
                          Vendors=("Vendor", "nunique"))
                     .sort_values("Total", ascending=False).reset_index())
        st.dataframe(by_holder, use_container_width=True, hide_index=True,
                     column_config=money_config(by_holder))

    st.download_button("Download results (CSV)", df.to_csv(index=False).encode("utf-8"),
                       file_name=f"pcard_{field.lower()}_{keyword.strip().replace(' ', '_')}_{year}.csv",
                       mime="text/csv", key=f"dl_{key}")


def search_panel(field: str, label: str, example: str, year: int):
    key = field.lower()
    category = st.selectbox(f"Prohibited category (optional)", ["—"] + list(PROHIBITED),
                            key=f"cat_{key}",
                            help="Pick a category to get keyword suggestions for this field.")
    if category != "—":
        st.caption("Suggested keywords: " + ", ".join(f"`{k}`" for k in PROHIBITED[category][key]))

    with st.form(f"form_{key}"):
        keyword = st.text_input(f"Keyword to search in the {label.lower()} field",
                                placeholder=f"e.g. {example}", key=f"kw_{key}")
        submitted = st.form_submit_button(f"Search {label.lower()}", type="primary")

    if submitted:
        if not keyword.strip():
            st.warning("Enter a keyword first.")
        else:
            st.session_state[f"last_{key}"] = (keyword, year)
    if f"last_{key}" in st.session_state:
        keyword, searched_year = st.session_state[f"last_{key}"]
        df = search(field, keyword, searched_year)
        st.markdown(f"**{label} contains “{keyword}” in {searched_year}**")
        show_results(df, keyword, field, searched_year, key)


def dashboard_tab():
    st.subheader("Prohibited purchases dashboard")
    with st.expander("How to use this dashboard", expanded=True):
        st.markdown(
            """
1. **Select the year** you want to audit.
2. Choose a search option:
   - **Description search** looks for the keyword in the transaction description the cardholder
     entered (for example `beer` or `gift card`).
   - **Vendor search** looks for the keyword in the merchant name (for example `usps` or `liquor`).
3. Optionally pick a **prohibited category** to see suggested keywords for that field.
4. Enter a keyword and press **Search**. The search is not case-sensitive and matches any part of the text.
5. Review the results: the cardholder, dates, amount, vendor, description and merchant category code (MCC)
   are the details needed for follow-up. Open **totals by cardholder** to see who to contact first,
   and **download the CSV** to document the work.

A match is only a *potential* deviation. Many descriptions say only "GENERAL PURCHASE", so a vendor search
often finds more than a description search. Always confirm with the receipt and business purpose before
concluding that a purchase violates policy.

**Prohibited on the P-card:** alcohol; cash, cash advances and ATM transactions; decorations; donations
and sponsorships; gasoline; gifts, gift cards and gift certificates; insurance; late fees; mail and
postage; moving expenses; personal purchases of any type; personal and individual memberships and dues;
salaries, wages and benefits; service and incentive awards.
"""
        )

    year = st.selectbox("Year to search", years(), index=years().index(2014) if 2014 in years() else 0)

    desc_tab, vendor_tab = st.tabs(["🔤 Description search", "🏪 Vendor search"])
    with desc_tab:
        st.markdown("Search the **transaction description** field for possible internal control deviations.")
        search_panel("Description", "Description", "beer", year)
    with vendor_tab:
        st.markdown("Search the **vendor** field for possible internal control deviations.")
        search_panel("Vendor", "Vendor", "post office", year)


# ---------------------------------------------------------------- natural language

SCHEMA_PROMPT = """You translate an internal auditor's question into one SQLite query against a database of
Oklahoma State University (OSU) purchasing-card (P-card) transactions.

There is a single table:

CREATE TABLE pcards (
  Year INTEGER,                -- calendar year of the transaction (2010-2014; 2014 is the audit year)
  Month INTEGER,               -- month of the transaction, 1-12
  FullName TEXT,               -- cardholder, e.g. 'Employee 51914657' (anonymised)
  ID INTEGER,                  -- row id
  AgencyNumber INTEGER,        -- always 1000
  AgencyName TEXT,             -- always 'OKLAHOMA STATE UNIVERSITY'
  CardholderLastName TEXT,     -- e.g. 'Employee51914657'
  CardholderFirstInitial TEXT,
  Description TEXT,            -- free-text description; very often 'GENERAL PURCHASE'
  Amount REAL,                 -- US dollars; negative values are returns/credits
  Vendor TEXT,                 -- merchant name, upper case, e.g. 'WAL-MART #0137'
  TransactionDate TEXT,        -- text 'M/D/YYYY 0:00:00', e.g. '7/26/2014 0:00:00' (NOT ISO, do not sort it as text)
  PostedDate TEXT,             -- same format as TransactionDate
  MCC TEXT                     -- merchant category description, upper case, e.g. 'HARDWARE STORES'
);

Rules:
- Write exactly one read-only SELECT statement (a WITH ... SELECT is fine). Never modify data.
- Filter with Year and Month instead of parsing TransactionDate. To sort by date use
  Year, Month, CAST(SUBSTR(TransactionDate, INSTR(TransactionDate, '/') + 1) AS INTEGER).
- Use LIKE with % wildcards for text matching; LIKE is case-insensitive here.
- If the question does not name a year, assume 2014 and say so in the explanation.
- Round money to 2 decimals and give columns readable aliases.
- Return at most 1000 rows; add ORDER BY so the most relevant rows come first.
- If the question cannot be answered from this table, set sql to an empty string and explain why.

P-card policy context: single-transaction limit $5,000; monthly limit $10,000 without justification;
annual limit $50,000. Prohibited: alcohol, cash/ATM, decorations, donations/sponsorships, gasoline, gifts and
gift cards, insurance, late fees, mail/postage, moving expenses, personal purchases, personal memberships/dues,
salaries/wages/benefits, service and incentive awards. Split purchases to avoid the $5,000 limit are prohibited.
Meals during travel (restaurant charges on the same day as hotel charges) should be claimed as per diem."""

SQL_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "One SQLite SELECT statement, or empty if unanswerable"},
        "explanation": {"type": "string", "description": "One or two sentences on how the query answers the question"},
    },
    "required": ["sql", "explanation"],
    "additionalProperties": False,
}


@st.cache_resource
def client():
    key = secret("ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=key) if key else None


def ask_claude(messages, system, effort, output_config_extra=None):
    output_config = {"effort": effort, **(output_config_extra or {})}
    response = client().beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=messages,
        output_config=output_config,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined this request. Try rephrasing the question.")
    return next((b.text for b in response.content if b.type == "text"), "")


def generate_sql(question: str, previous_error: tuple[str, str] | None = None) -> dict:
    messages = [{"role": "user", "content": question}]
    if previous_error:
        bad_sql, error = previous_error
        messages = [{"role": "user", "content":
                     f"{question}\n\nA previous attempt failed.\nSQL:\n{bad_sql}\nError: {error}\n"
                     "Write a corrected query."}]
    text = ask_claude(messages, SCHEMA_PROMPT, "medium",
                      {"format": {"type": "json_schema", "schema": SQL_SCHEMA}})
    return json.loads(text)


def check_read_only(sql: str):
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        raise ValueError("Only a single statement is allowed.")
    if not re.match(r"^(SELECT|WITH)\b", stripped, re.I):
        raise ValueError("Only SELECT queries are allowed.")
    return stripped


def describe_result(df: pd.DataFrame) -> str:
    """Summarise the result locally - no transaction data is ever sent to the API."""
    if df.empty:
        return "The query returned no rows."
    rows = f"{len(df):,} rows" + (" (only the first 1,000 are shown)" if len(df) == MAX_ROWS else "")
    if len(df) == 1 and len(df.columns) == 1:
        return f"**Answer:** {df.iat[0, 0]}"
    return f"The query returned {rows}."


def answer(question: str) -> dict:
    result = {"question": question}
    plan = generate_sql(question)
    for attempt in range(2):
        result["sql"], result["explanation"] = plan["sql"], plan["explanation"]
        if not plan["sql"].strip():
            result["answer"] = plan["explanation"]
            return result
        try:
            sql = check_read_only(plan["sql"])
            df = run_query(sql)
            break
        except (sqlite3.Error, ValueError) as e:
            if attempt == 1:
                result["error"] = str(e)
                return result
            plan = generate_sql(question, (plan["sql"], str(e)))
    result["data"] = df
    result["answer"] = f"{result['explanation']}\n\n{describe_result(df)}"
    return result


EXAMPLES = [
    "Which 10 employees spent the most in 2014?",
    "How many transactions over $5,000 were there in 2014, and by whom?",
    "Show purchases at liquor stores in 2014.",
    "Which vendors received the most money in December 2014?",
    "What was the total spend per month in 2014?",
]


def ask_tab():
    st.subheader("Ask the database")
    st.markdown("Ask a question in plain English. Claude writes a SQL query, and the app runs it on the "
                "P-card database (read-only) and shows the result. The query is shown so you can check "
                "how the answer was produced.")
    st.caption("Privacy: only your question and a description of the table's columns are sent to Claude. "
               "Transaction data never leaves this server.")
    if client() is None:
        st.error("No Anthropic API key is configured. Add `ANTHROPIC_API_KEY` to the app's secrets "
                 "(Streamlit Cloud: Settings → Secrets) or to `.streamlit/secrets.toml` when running locally.")
        return

    st.caption("Examples:")
    cols = st.columns(len(EXAMPLES))
    for col, ex in zip(cols, EXAMPLES):
        if col.button(ex, use_container_width=True):
            st.session_state["nl_question"] = ex

    with st.form("nl_form"):
        question = st.text_area("Your question", key="nl_question", height=80,
                                placeholder="e.g. Which cardholders bought gasoline in 2014 and how much?")
        submitted = st.form_submit_button("Ask", type="primary")

    history = st.session_state.setdefault("nl_history", [])
    if submitted and question.strip():
        with st.spinner("Writing and running the query…"):
            try:
                history.insert(0, answer(question.strip()))
            except anthropic.AuthenticationError:
                st.error("The Anthropic API key was rejected. Check the key in the app's secrets.")
            except anthropic.RateLimitError:
                st.error("Rate limited by the Anthropic API. Wait a moment and try again.")
            except (anthropic.APIStatusError, anthropic.APIConnectionError, RuntimeError) as e:
                st.error(f"Could not get an answer: {e}")

    for i, item in enumerate(history):
        with st.container(border=True):
            st.markdown(f"**Q: {item['question']}**")
            if "error" in item:
                st.error(f"The generated query failed: {item['error']}")
            if item.get("answer"):
                st.markdown(item["answer"])
            if item.get("sql"):
                with st.expander("SQL query"):
                    st.code(item["sql"], language="sql")
            if "data" in item and not item["data"].empty:
                st.dataframe(item["data"], use_container_width=True, hide_index=True,
                             column_config=money_config(item["data"]))
                st.download_button("Download (CSV)", item["data"].to_csv(index=False).encode("utf-8"),
                                   file_name="pcard_query.csv", mime="text/csv", key=f"nl_dl_{i}")


# ---------------------------------------------------------------- layout

st.title("🔎 OSU P-card Audit")
st.caption("Internal audit tool for Oklahoma State University purchasing-card transactions (2010–2014).")

tab_ask, tab_dash = st.tabs(["💬 Ask in natural language", "🚫 Prohibited purchases dashboard"])
with tab_ask:
    ask_tab()
with tab_dash:
    dashboard_tab()
