# OSU P-card Audit

A Streamlit website that internal auditors can use to examine Oklahoma State University purchasing-card
(P-card) transactions (2010–2014, about 489,000 rows).

It has two tabs:

1. **Ask in natural language** – type a question in plain English. Claude (Anthropic API) turns it into a
   SQLite query, and the app runs the query on a read-only copy of the database and shows the SQL and the
   result table.
2. **Prohibited purchases dashboard** – select a year and search the **description** field or the
   **vendor** field with a keyword (for example `beer` or `post office`) to find possible internal control
   deviations. Suggested keywords are given for each prohibited category, and results can be summarised
   by cardholder and downloaded as CSV.

## Run locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then add your Anthropic API key
streamlit run app.py
```

The database ships compressed as `data/pcards.db.gz` and is unpacked automatically on first start.

## Deploy (Streamlit Community Cloud)

1. Push this repository to GitHub.
2. On [share.streamlit.io](https://share.streamlit.io), create an app from the repository with `app.py`
   as the main file.
3. Under **Settings → Secrets**, add `ANTHROPIC_API_KEY = "..."`.

## Security and privacy

- Only the auditor's question and a fixed description of the table's columns are sent to the Anthropic
  API (plus the failed SQL and SQLite error message if a generated query has to be corrected).
  Transaction data is never sent; queries run only on the app's own server.
- The dashboard tab makes no external calls.
- Streamlit usage statistics are turned off in `.streamlit/config.toml`.
- Deploy the app as a private app on Streamlit Community Cloud and invite only the people who need access.
- The API key is read from Streamlit secrets or the `ANTHROPIC_API_KEY` environment variable. It is never
  stored in the source code, and `.streamlit/secrets.toml` is git-ignored.
- Generated SQL is restricted to a single `SELECT`/`WITH` statement and runs on a read-only connection
  (`mode=ro`, `PRAGMA query_only`) with a time limit and a 1,000-row cap.
