# Power BI Extraction Insights

A local web page (and CLI) that takes a Power BI file and produces the migration workbook: every report tab
and insight, the semantic model, Power Query sources, relationships, and each DAX measure decoded into
Spark SQL, with a build plan per insight and an optional build package for Incorta.

It runs on your own computer. Files never leave it. Every generated query is Spark SQL, the language Incorta
MVs run.

## Quick start (Windows, PowerShell, Python 3.11+)

```powershell
cd C:\path\to\pbi-extraction-insights
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5001, drop a `.pbix` on the page, choose **Create workbook**, then
**Download workbook (.xlsx)**. Next time, double-click `run.bat` (it does all of the above).

If PowerShell blocks `activate`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

## Command line

```powershell
python -m pbi_insights samples\Retail.pbix --out outputs
python -m pbi_insights samples\Retail.pbix --out outputs --data --incorta
```

| Option | What it does |
|---|---|
| `--data` | Reads the data saved in the .pbix: profiles columns and test-runs every insight query (SQLite) |
| `--view-prefix` | Prefix for target view names (default `vw_`) |
| `--incorta` | Also writes the Incorta build package (below) and adds its sheets to the workbook |
| `--incorta-schema` | Incorta physical schema name (default: the report name in capitals) |
| `--no-incorta-check` | Skip running the Power Query MVs on the rows saved in the .pbix |

Each run writes `<name>_migration_inventory.xlsx` and `<name>_inventory.json` (everything, machine-readable).

## Incorta build package

With `--incorta` (or **Build the Incorta package** on the page) the app applies the Incorta build spec
(`pbi_insights/incorta/spec.md`) to the decoded report and writes `<name>_incorta_outputs.zip`:

| File | Contents |
|---|---|
| `<name>__<table>.sql` | One per Incorta table: data source, key, load filter, columns to load, formula columns, joins; the dataset SQL (database sources) or the file to load and a row-count check (data files) |
| `<name>__<mv>.sql` | One per materialized view, Spark SQL in steps: one per Power Query query with steps (reading the MVs of the queries it references), calculated tables, calculated columns that aggregate per row, mixed-grain insights |
| `<name>_doc.pdf` / `.md` | Design: A. Summary, B. Key transformations, C. Incorta optimization, D. Manual intervention, E. Build steps, F. Decision log, appendices |
| `<name>_dag.png` | Lineage: sources, Incorta tables, MVs, business views, dashboard tabs |
| `<name>_pq_lineage.png` | Every Power Query query by level: physical tables and MVs, arrows = which object reads which |
| `<name>_incorta_build_plan.xlsx` | Overview with the build order, then Incorta Source Map, Power Query Lineage, Joins, Formula Columns, MVs, Business Schema, Measures, Insights, Decision Log, Manual Checks |
| `<name>_incorta_plan.json` | The whole plan, machine-readable |

The rules decide every row (status **Proposed**); anything they cannot place is **Needs design** with options
in the Decision Log. Items a person must confirm carry `[MANUAL CHECK REQUIRED]`.

**Power Query lineage.** Power Query often builds a report table from other queries (a union of country
queries, each joined to item, cost and optional-field queries). The package follows that chain: each query's
source (ODBC native query, database query, file) is a physical table loaded as-is, and each query with steps is
an MV `<query>_MV` that reads the MVs of the queries it references, so every Power Query step has its Incorta
place. The M code is parsed (`incorta/mlang.py`) and translated step by step to Spark SQL (`incorta/m2sql.py`);
steps it cannot translate are listed for a manual rewrite. Simple single-source queries (types,
renames, a filter) stay plain tables.

**Layer checks.** With duckdb installed, every Power Query MV is run on the rows Power BI saved for the queries
it reads and compared column by column with the rows Power BI saved for it (Match / Differs / Partly checked
in the Power Query Lineage sheet, the MV's .sql header and the document). Runs locally; switch it off with
`--no-incorta-check`.

Check the calculated-column and calculated-table MVs against the numbers Power BI saved in the file (runs
locally, needs `pip install duckdb`):

```powershell
python tools\check_incorta_mvs.py samples\MyReport.pbix
```

It runs each MV's Spark SQL (transpiled to DuckDB) on the tables stored in the .pbix and compares every
generated column row by row with the column Power BI computed.

What the rules handle: data-file and SQL sources (Power Query row filters become load filters, remove-duplicates
becomes the key), joins child to parent (concatenated keys become composite joins, inactive relationships get
an alias only when USERELATIONSHIP uses them, type mismatches are cast), row-level calculated columns as physical
formula columns (LOOKUPVALUE as `lookup()`, repeating any formula column they use), per-row aggregates (a
median per currency and month, EARLIER) and DAX calculated tables (CALENDAR, GENERATESERIES) as Spark SQL MVs,
measures as business-view or insight formulas (`ago()` / `toDate()` for time intelligence, `denseRank()` for
RANKX, `groupBy()` for REMOVEFILTERS totals), mixed-grain measures as MVs at the insight grain, visuals as
insights and slicers as dashboard filters.

## What the workbook contains

| Sheet | Contents |
|---|---|
| Plan | File summary, how each insight gets built, migration steps, findings that change the build, open decisions |
| Tabs | Each report tab: purpose, insight counts, slicers, filter-pane fields, applied page filters |
| Insights | Each visual with an ID (`OV-02`): chart type, dimensions, measures, filters, sort, business question, approach |
| Insight Lineage | For every field on every insight: DAX or column, model base columns, database source, target column / SQL |
| Sources | Each table: role, rows, connector, server, database, source objects, business rules in the native SQL, Power Query steps, M code |
| Columns | Every column: where it is used, type, database source, target column, DAX and SQL for calculated columns |
| Relationships | Cardinality, filter direction, the target `LEFT JOIN`, notes (type mismatches, bidirectional, inactive) |
| Measures | DAX, plain-English logic, generic SQL, pattern, complexity, dependencies, draft Incorta formula |
| Build Plan | Per insight: approach, grain, dataset, measure logic, filters, sort, how to build it in each tool |
| Target Views | One `CREATE VIEW` per table: native SQL + Power Query renames/types + calculated columns as SQL |
| Insight SQL | One reference query per insight (Spark SQL) and, with `--data`, whether it ran and how many rows it returned |
| Data Profile | With `--data`: distinct values, nulls, min and max per column |

## How the SQL stays faithful to Power BI

The generated SQL follows DAX semantics rather than a literal rewrite:

- CALCULATE filters are pushed into the aggregate (`SUM(CASE WHEN ... THEN ... END)`) and replace visual
  filters on the same column; `FILTER(ALL(...))` becomes a fixed subquery that ignores the visual's filters.
- Filters flow one way (from dimension to fact), joins are `LEFT JOIN`s from the many side.
- Measures that mix grains (for example sales per square foot) are computed per table in CTEs and joined on
  the shared dimensions, so nothing is double counted.
- BLANK arithmetic, safe division, drill-down levels, sort-by columns and implicit measures are handled.
- Anything it cannot reproduce exactly (time intelligence, context transition, TopN filters) is marked
  **Needs review** with the reason. It never guesses.

On the Retail Analysis Sample all 16 data insights match an independent re-implementation of the DAX logic.

## Limits

- Thin reports (live connection to a published model) carry no model: export a `.pbit`, or use the model's own `.pbix`.
- DirectQuery files have no saved data, so `--data` profiles nothing.
- Time intelligence, RANKX and REMOVEFILTERS get Incorta formulas (ago, denseRank, groupBy); the generic SQL
  for them is still flagged for review.
- Custom visuals are listed; you pick the closest native chart.

## Project layout

```
app.py                  Flask server (upload, background jobs, downloads)
templates/index.html    The page
static/css, static/js   Page style and behaviour
pbi_insights/
  extract/              PBIX package, report layout, semantic model, Power Query, native SQL lineage
  analyze/              DAX parser and translator, SQL rendering, query builder, planner, findings, views
  incorta/              Incorta build package: spec.md (rules), plan.py (rule engine), sparksql.py (MV SQL),
                        mlang.py (Power Query parser), m2sql.py (Power Query -> Spark SQL, query graph),
                        verify.py (layer checks in DuckDB), sheets.py,
                        package.py (SQL, PDF, diagrams, zip)
  report/workbook.py    The Excel writer
  pipeline.py, cli.py   Orchestration and command line
tests/                  Unit and end-to-end tests (synthetic .pbit, no client files needed)
samples/, uploads/, outputs/   Your files (ignored by git)
.claude/                Claude Code settings, skills and a reviewer agent
CLAUDE.md               Project notes for Claude Code
```

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

Drop a real `.pbix` into `samples/` and the sample test also runs it end to end with `--data`.

## Working with Claude Code

Open the folder in VS Code and start `claude` in the terminal. It reads `CLAUDE.md`, and you can use:

- `/extract-pbix samples\MyReport.pbix --data`: run a file and get a summary of what needs attention
- `/dax-to-sql <function or measure>`: extend the translator for a new DAX pattern
- `/add-workbook-sheet <sheet> <contents>`: change the Excel output
- `/build-incorta-package samples\MyReport.pbix`: generate and review the Incorta build package
- the `dax-sql-reviewer` agent: checks generated SQL against DAX semantics

## Put it in your own GitHub repo

```powershell
git init
git add .
git commit -m "Initial commit: Power BI Extraction Insights"
git branch -M main
git remote add origin https://github.com/<your-account>/pbi-extraction-insights.git
git push -u origin main
```

Create the empty repository on GitHub first (no README, so the first push is clean). `.gitignore` already
keeps `.env`, `.venv`, `uploads/`, `outputs/` and `samples/` out of the repository.
