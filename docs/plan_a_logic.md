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

## Not supported yet (each is Needs review in the output, never a guessed SQL)

- Total CTE for non-additive aggregates at a coarser grain (next step).
- USERELATIONSHIP combined with a filter removal.
- ALLSELECTED when an outer CALCULATE of the measure chain filters the same columns.
- ALLCROSSFILTERED.
- A total at a coarser grain inside another aggregate's argument or filter.
