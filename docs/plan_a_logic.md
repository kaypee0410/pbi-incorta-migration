# Filter removal (ALL / REMOVEFILTERS / ALLSELECTED / ALLEXCEPT): general rules

Names here are neutral (Fact, Dim, Lookup ...). Notes about a client report (its tables, measures, results) live in
`docs/private/`, which git ignores: code, tests and committed docs never contain a name from a client report
(`python tools/check_client_names.py` checks the tracked files against the models in `samples/` and `uploads/`).

Every rule below is decided only from the DAX AST and the model metadata (tables, columns, relationships,
cardinality, cross-filter direction, data types), never from a table, column, measure or visual name.

## Decisions (agreed)

- **Expanded table.** `expanded(T)` = T plus every table reachable from T along active relationships from the many
  side to the one side, recursively, whatever the cross-filter direction. A measure is flagged ("check in Power BI")
  only when its removal, or a filter its aggregate depends on, crosses a bidirectional or many-to-many relationship;
  not because such a relationship exists somewhere in the model.
- **KEEPFILTERS(ALL(T[c]))** removes nothing (ALL returns every value, KEEPFILTERS intersects it with the current
  filter): no total, status Translated with notes, note "KEEPFILTERS(ALL(...)) keeps the current filters (no total is
  computed); confirm in Power BI if the result looks off".
- **Removal on another table.** `ALL(OtherFact)` clears the filters on every table in expanded(OtherFact), so it does
  affect a Fact measure when the two facts share dimensions. A removal whose expanded table does not meet the
  measure's expanded table has no effect.
- **Non-additive totals** (AVERAGE, AVERAGEX, DISTINCTCOUNT, MEDIAN): Needs review, "needs a total CTE at the
  coarser grain".
- **USERELATIONSHIP** together with a removal: Needs review.
- **ALLSELECTED under an outer CALCULATE that filters the same columns**: Needs review (ALLSELECTED uses the filters of
  the visual's query, not those of an outer CALCULATE).
- **ALLEXCEPT**: see the rules (step 2).

## The one function: `RelGraph.removal(tables, columns, keep)`

`expanded(T)` = T + every table reachable from T along active relationships from the many to the one side
(`RelGraph.expanded`). `removal(...)` answers "does this removal clear the filters on column X[c]?":

| Argument | Columns cleared |
|---|---|
| `ALL(T)` / `REMOVEFILTERS(T)` / `ALLNOBLANKROW(T)` | every column of expanded(T) |
| `ALL(T[c1], T[c2])` (same for REMOVEFILTERS, ALLNOBLANKROW) | those columns only |
| `ALL()` / `REMOVEFILTERS()` | every column of the model |
| `ALLEXCEPT(T, X[k], ...)` | every column of expanded(T) except the kept ones |
| `ALLSELECTED(...)` | same columns as ALL; slicer, page, report and visual filters stay (only the visual's own grouping goes) |
| `FILTER(ALL(T), cond)` | every column of expanded(T), then `cond` is added as a filter |
| `KEEPFILTERS(ALL(...))` | nothing |

The same function decides the outer CALCULATE filters a removal clears (translator), the window's
`PARTITION BY` and the slicer flag (insight SQL), and the `groupBy()` columns (Incorta).

## Rules (input shape → SQL → pattern / status → Incorta)

For `CALCULATE(e, <removal>)`, with H = each table e aggregates (its top aggregates):

| # | When | SQL | Pattern / status | Incorta |
|---|---|---|---|---|
| F0 | A filter into expanded(H) or into a removed table's expanded table arrives through a bidirectional, many-to-many or one-to-one relationship (`RelGraph.reverse_entry`) | REVIEW NULL | Needs review "filter removal through a ... relationship: check in Power BI" | decision |
| F1 | The removal clears no column of any expanded(H) | e unchanged | e's own pattern, note "no effect" | e's formula |
| F2 (Case 1) | Kind ALL, every column of every expanded(H) cleared, e's columns inside expanded(H), no aggregate of the visual inside an aggregate's filter, no time filter / FILTER(T) in the same CALCULATE | scalar subquery per table: `(SELECT e FROM H [joins])`; inside an aggregate: the inline subquery (per-measure SQL), a one-row CTE CROSS JOINed (insight SQL) | Filter-independent (ALL) / Translated with notes | `groupBy(insight dims − cleared)`, i.e. `groupBy()` when the insight only groups by cleared columns |
| F3 (Case 2) | Otherwise, and every aggregate of e additive (SUM, SUMX, COUNT, COUNTROWS, MIN, MAX) | each aggregate: `SUM/MIN/MAX(<per group>) OVER (PARTITION BY insight dims − cleared)`; insight SQL = grouped query, then the window, then the blank-row filter | Total at a coarser grain (ALL) / Translated with notes | `groupBy(insight dims − cleared)` |
| F4 | As F3 with AVERAGE(X), DISTINCTCOUNT or MEDIAN | REVIEW NULL | Share of total (needs review) / Needs review "needs a total CTE at the coarser grain" | groupBy formula |
| F5 | KEEPFILTERS(ALL(...)) | e unchanged | Translated with notes, note "KEEPFILTERS(ALL(...)) keeps the current filters ..." | e's formula |
| F6 | ALLSELECTED clears an outer CALCULATE filter of the measure chain; ALL and ALLSELECTED in one CALCULATE; ALLEXCEPT with another removal; FILTER(ALL(T), cond) around an aggregate T's expanded table does not reach | REVIEW NULL | Needs review | decision |
| F7 | An F2 measure referenced inside a filter argument, a VAR, DATE() / EDATE(), in any fact | the F2 subquery in place | Filter-independent (ALL) | as F2 |
| F8 | An F3 total inside another aggregate's argument or filter | REVIEW NULL | Needs review | decision |

Insight-level flags: an ALL / REMOVEFILTERS (not ALLSELECTED) measure with a slicer / page / visual filter on a cleared
column → "Power BI's total ignores the slicer on X[c]; compute the total in a CTE without that filter.". A measure
whose window drops one of the insight's dimensions (or a filter-independent value) and is not BLANK without rows →
"has a value for groups without rows". A visual-level Top N filter is always flagged by the planner.

Dependencies (scope): `[Name]` is a measure unless the same DAX creates a column Name (ADDCOLUMNS, SELECTCOLUMNS,
SUMMARIZE, SUMMARIZECOLUMNS, GROUPBY, ROW, ADDMISSINGITEMS, NATURALINNERJOIN); `'T'[Name]` is a measure when T has no
column Name.

## Not supported yet (each is Needs review in the output, never a guessed SQL)

- Total CTE for non-additive aggregates at a coarser grain (next step).
- USERELATIONSHIP combined with a filter removal.
- ALLSELECTED when an outer CALCULATE of the measure chain filters the same columns.
- ALLCROSSFILTERED.
- A total at a coarser grain inside another aggregate's argument or filter.
