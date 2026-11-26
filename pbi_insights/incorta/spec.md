# Power BI to Incorta build spec (rules)

Condensed from the Claude Doc "Power BI to Incorta build spec" (revision 17, 25 Nov 2026). The rule engine in
`pbi_insights/incorta/plan.py` implements these rules.

## Goal and inputs
- Rebuild the report in Incorta with the simplest objects that give the same numbers. DAX, Power Query and
  relationships become tables, joins, formula columns, materialized views (MVs), business views and insights.
- Incorta reads the same sources as Power BI (same tables, same native SQL; files are uploaded as data files),
  through one Incorta data source per database or file folder.
- Input: the migration workbook / JSON of one .pbix. Output: the Incorta build plan (8 sheets), SQL files,
  a design document and a lineage diagram.

## Step 1: map the sources
- Every Power BI table gets an Incorta table, a dataset SQL (or file) and a key before any measure is placed.
- One Incorta data source per database; replace local names such as "." with the real server.
- Drop auto date tables, measure tables (no data) and tables nothing uses; record the reason.
- Dataset SQL starts from the decoded native SQL with Power Query renames as column aliases; keep business
  rules (WHERE, joins) unless a decision says otherwise. Power Query row filters on files become load filters.
- Load what insights, measures and joins use, plus keys; skip unused columns and images.
- Key = the one-side column(s) of each relationship; distinct count must equal row count.
- Full load by default; incremental only for large facts with a reliable update date (open question).

## Step 1b: Power Query lineage (queries built from other queries)
- Build the query graph first: a query that names another query (Table.Combine, Table.NestedJoin, Source =
  OtherQuery) reads it. Keep every upstream query of a table the report uses, loaded or connection-only.
- Each query's external source (Odbc.Query, Sql.Database with a native query, a file or folder) becomes a
  physical table loaded as-is: the native SQL is the dataset SQL, the file is a data file.
- Every query with steps becomes one MV named <query>_MV that reads the Incorta objects of the queries it
  references, so the Incorta chain is the Power Query chain (1:1, traceable). Never stand in a row count for
  a query's logic.
- Exception, the earliest layer: a single-source query whose steps are only types, renames, column selection,
  a simple row filter or a key stays a physical table (load filter, key), with no MV.
- Translation of the steps: one CTE per group of steps (named after the Power Query step); Table.NestedJoin +
  expand = LEFT JOIN on null-safe keys (<=>, Power Query matches nulls); Table.Combine = UNION ALL by column
  name with NULL for missing columns; Replacer.ReplaceValue = CASE (chains folded), Replacer.ReplaceText =
  REPLACE; Table.Distinct on keys keeps the first row (first union input, then the other columns: decision);
  try ... otherwise = the expression (Spark gives NULL) or COALESCE when the fallback is a value;
  Date.FromText follows its culture (en-GB day first); DateTime.LocalNow = CURRENT_DATE at load (reload daily).
- File steps (folder listing, newest file, sheet, skipped rows, header row, title rows) describe how to load
  the file; a newest-file-in-folder pattern or a wide file (unpivot of every other column) is a decision.
- A column that mixes numbers and text (e.g. '-') stays text, as in Power BI; recommend a number with NULL.
- Check every MV on the rows saved in the .pbix (DuckDB, local): inputs = the saved outputs of the queries it
  reads; a source query's raw table is rebuilt from its saved output columns. Record Match / Differs / Partly
  checked per layer.

## Step 2: schemas
- One physical schema per source subject area, named in capitals. Tables keep their Power BI names. MVs end
  in _MV. Derived objects (MVs, Analyzer tables) stay in the schema of the tables they read.
- One business schema per report audience, named after the report: one view per Power BI table or display
  folder, Power BI display names as labels, keys hidden, reusable measure formulas.
- No copies of a table across schemas; a table needed in a second role becomes an alias.

## Step 3: joins
- Each relationship becomes one join from the many side (child) to the one side (parent), on columns of the
  same type, with a unique key on the parent. An Incorta join is a left outer join from child to parent.
- Key types differ: fix the type in the dataset SQL or a formula column on one side, then join.
- Join on a DAX calculated column: create that column first (dataset SQL, MV or formula column), then join.
  A key built by concatenating columns (A & "|" & B) on both sides is a composite join on A and B.
- Inactive relationship used with USERELATIONSHIP: an alias of the parent with its own join.
- Bidirectional or many-to-many: no direct equivalent; set a base table or pre-join in an MV; flag for review.
- Two paths between the same tables: break the cycle with an alias.

## Step 4: place each piece of logic (earliest layer that keeps it correct)
| Layer | When it runs | Best for |
| Dataset SQL | extraction | renames, type casts, filters, joins already in the source SQL |
| Physical formula column | load, stored | row-level calculated columns; can read parent tables through joins; cannot aggregate; cannot reference another formula column of the same table or of a child table |
| MV (Spark SQL / PySpark) | load, after its sources | aggregation, mixed grain, window functions, multi-step logic, logic shared by several insights |
| Business view formula | query time | measures reused by several insights |
| Insight formula | query time | logic used by one chart |

Rules by pattern:
- Row-level calculated column (IF, YEAR, FORMAT, &, LOOKUPVALUE) → physical formula column (lookup() needs a key
  on the lookup table).
- Calculated column with aggregation, EARLIER or a per-row filter over the same table → MV (or Analyzer table).
- Simple aggregate → insight measure; business view formula when 2+ insights use it (sum, count, average, distinct).
- Filtered aggregate (CALCULATE with a fixed filter) → sum(if(...)) or a helper column at load.
- Ratio / arithmetic of measures → business view or insight formula; guard division by zero with if()
  (Power BI shows blank, not 0).
- Dimension-grain aggregate → insight with the dimension as base table.
- Mixed grain → MV at the insight's grain; a level-based measure is an option when its levels are the insight's
  grouping dimensions (same order; cannot be used in filters).
- Ignores filters (FILTER(ALL), REMOVEFILTERS) → level-based measure, an insight with its own filter kept out of
  dashboard filters, or an MV.
- Time intelligence → Incorta time-series functions in the measure tray (ago(), toDate()), or a date table plus
  an MV.
- Ranking (RANKX) → rank() / denseRank(groupBy(...), orderBy(measure, false)) in the insight.
- Hard-coded values → keep for parity, or a variable (decision log).
- Needs review → a developer decision; never built before approval.
- MV rule: create one only when 2+ insights need the same non-trivial logic at the same grain, the logic mixes
  grains / needs windows / takes several steps, or the insight is too slow on joins. One MV per grain; merge
  MVs that share a grain.

## Step 5: insights and dashboard
- Report → dashboard; page → dashboard tab (same order and names); card → KPI; bar / column / line / pie /
  scatter → same chart; table → aggregated or listing table; matrix → pivot table; slicer → dashboard filter or
  prompt; page filter → filter on every insight of the tab; visual filter → insight filter; hierarchy → drill
  down; sort by column → sort on that column; text / image → recreated by hand.
- Set the base table for dimension-only insights and wherever two join paths exist.

## Step 6: review loop and exit criteria
- Can an MV be replaced by a join plus a formula column? Do two MVs share a grain? Is a formula repeated in
  2+ insights (move it to the business view)? Does an insight aggregate a parent over the fact join?
- Exit: every table mapped or excluded with a reason; every join has a unique parent key, matching types and no
  second path; every in-scope measure has one home; every insight has a pattern and base table where needed;
  no Needs review item without an owner and a decision; every MV names the insights it serves and its rule;
  decision log settled; expected numbers exist for reconciliation.

## Guardrails
- Status gate Proposed → Approved → Built → Reconciled.
- Anything that needs a person is tagged `-- [MANUAL CHECK REQUIRED]`.
- A row is done only when Incorta matches Power BI under the same filters.

## Output sheets
Incorta Source Map, Power Query Lineage, Incorta Joins, Incorta Formula Columns, Incorta MVs, Incorta Business Schema,
Incorta Measures, Incorta Insights, Decision Log. Status values: Proposed, Approved, Built,
Reconciled.
