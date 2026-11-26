# PBI Extraction Insights

Local web app + CLI that decodes a Power BI file (.pbix / .pbit / .bim) and writes a migration workbook
(Excel) and, optionally, a build package for rebuilding the report in Incorta. Incorta only: generated SQL is
always Spark SQL (`pipeline.SQL_DIALECT`), there is no dialect option and no QuickSight/Tableau output.
This file is a starter: grow it as the project grows (decisions, conventions, gotchas).

## Commands (Windows, from the project folder with .venv active)
- Set up: `python -m venv .venv`, `.venv\Scripts\activate`, `pip install -r requirements.txt -r requirements-dev.txt`
- Web app: `python app.py` then open http://127.0.0.1:5001 (or double-click `run.bat`)
- CLI: `python -m pbi_insights samples\MyReport.pbix --out outputs [--data] [--incorta]`
- Tests: `python -m pytest -q` (put real files in `samples/` to also run the sample test)

## How it works
`pbi_insights/pipeline.py` runs these steps; each lives in its own module:
1. `extract/package.py` opens the zip. `extract/layout.py` reads pages, visuals, fields, filters, sorts
   (legacy `Report/Layout` and PBIR). Visual IDs are `<page prefix>-<nn>`, e.g. `OV-02`.
2. `extract/model.py` reads the semantic model: VertiPaq via pbixray for .pbix, TMSL JSON for .pbit/.bim.
3. `extract/mquery.py` decodes Power Query M (connector, server, database, native SQL, renames, types);
   `extract/sqllineage.py` parses the native SQL with sqlglot (objects, column lineage, risky patterns).
4. `analyze/dax_parser.py` -> `analyze/dax_translate.py` -> IR (`analyze/ir.py`) -> `analyze/sqlrender.py`.
5. `analyze/planner.py` + `analyze/querygen.py`: one build plan and one reference SQL query per visual.
6. `analyze/sources.py` (target views), `analyze/findings.py`, `analyze/formulas.py` (tool drafts),
   `analyze/profile.py` (optional: profile saved data, test-run every query on SQLite).
7. `report/workbook.py` (the Excel file). `report/webview.py` turns a run's JSON export into the web page's
   data (counts, review measures grouped by blocking pattern with «» marks, MV build plan, findings); `app.py`
   caches it in `outputs/<run>/meta.json`, keeps "Mark as done" flags in `done.json`, and serves
   `/api/history` (runs grouped by file, with a diff). Page: `templates/components/*.html`,
   `static/css/tokens.css` (all colours and fonts, fonts bundled in `static/fonts/`), `static/js/app.js`.
8. `incorta/` (with `--incorta`): `plan.py` applies `incorta/spec.md` to the analysis (source map, joins,
   formula columns, MVs, business schema, measures, insights, decisions, manual checks); `sparksql.py` writes
   MV Spark SQL; `package.py` writes the SQL files, design PDF, DAG PNG, build-plan workbook and the zip.
   Power Query: `mlang.py` parses M into a tuple AST; `m2sql.py` translates each query step by step to Spark
   SQL (Rel = the table so far, one CTE per group of steps) and `PowerQueryGraph` orders the queries by their
   references (one physical table per external source, one `<query>_MV` per query with steps); `verify.py`
   runs each MV in DuckDB on the rows saved in the .pbix (`check_graph`, `run_query`, `run_chain`).

## Conventions
- Robustness (any legal .pbix must produce a workbook): every model leaves the readers through
  `extract.model.normalize_model` and every layout through `extract.layout.normalize_report` (names are
  non-empty str, no pandas NaN / numpy values, unnamed items dropped with a warning). The DAX and M
  translators never raise: unexpected shapes become `Review` / `Unsupported`. Each workbook sheet, each
  visual plan and each package file is guarded: a failure becomes a warning on the page ("Not written" sheet,
  a SQL file that says so), never a stopped run. When a guard fires, fix the root cause and add the case to
  `tests/test_robustness.py` (odd names, missing names, broken DAX/M, randomized mutations).
- Correct or flagged, never guessed: anything the translator cannot reproduce exactly becomes a
  `Review(reason)`, and the insight's approach becomes "Needs review".
- Generated SQL targets one view per Power BI table (`vw_<table in snake_case>`), LEFT JOIN from the many side.
- Keep DAX semantics (filter context, BLANK, one-way filters, grain). See the dax-to-sql skill.
- Workbook text is data: write cells through `_set`/`_sheet` (strips illegal characters, blocks formulas).
- Python 3.10+, type hints, no new heavy dependencies without a reason.
- Power Query semantics to keep: joins match nulls (`<=>`), Table.Combine unions by column name, Table.Distinct
  keeps the first row, `null = x` is false, try/otherwise catches errors not nulls, untyped (any) columns are
  text in Power BI. A step the translator cannot express raises `Unsupported(reason)`: the MV is Needs design,
  never a guessed SQL. Test new M patterns in tests/test_m2sql.py with `run_chain` on small frames.
- Incorta rules: a physical formula column cannot aggregate or use another formula column of the same table
  (repeat the logic or move the table's calculated columns to an MV); joins go child to parent with a unique
  parent key; one MV per grain.
- Per-row aggregates in calculated columns are `CorrAgg` in the IR (window or grouped subquery); LOOKUPVALUE is
  `Lookup`, time intelligence `TimeShift`, RANKX `Rank`, REMOVEFILTERS / ALL `Total`. Plain SQL still flags the
  last three for review; the Incorta formulas cover them.

## Data handling
- `uploads/` and `samples/` hold client files: never commit them, never paste their data into chats or docs.

## Claude Code helpers in .claude/
- Skills: `extract-pbix` (run and summarise a file), `dax-to-sql` (extend the translator),
  `add-workbook-sheet` (change the Excel output), `build-incorta-package` (generate and review the package).
- Agent: `dax-sql-reviewer` (checks generated SQL against DAX semantics).

## Roadmap / open items
- Fine-grain the workbook layout with the team (it follows the Retail Analysis Sample Phase 1 plan).
- Time intelligence (TOTALYTD, SAMEPERIODLASTYEAR, DATEADD) needs a date-table SQL pattern in the generic SQL;
  Incorta uses ago()/toDate().
- Incorta plan round trip: read an edited build-plan workbook back (approved rows) and regenerate the package.
- TOPN-style measures (Top N share) have no automatic Incorta home: Decision Log + manual design.
- Thin reports (live connection) have no model inside: export a .pbit or use the model's own .pbix.
- Compare test-run results with Power BI's rendered visuals for sign-off.
