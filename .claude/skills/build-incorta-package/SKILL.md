---
name: build-incorta-package
description: Generate the Incorta build package for a Power BI file and review it against the Incorta build spec. Use when asked to plan or build a report in Incorta, or when a package row is wrong or Needs design.
argument-hint: <path to .pbix / .pbit / .bim>
---

# Build and review the Incorta package

Target: `$ARGUMENTS`

1. Run `python -m pbi_insights $ARGUMENTS --out outputs --incorta` (add `--data` to profile the saved data and
   get expected row counts per insight). The zip lands in `outputs/<name>_incorta_outputs.zip`; the unzipped
   files are in `outputs/<name>_incorta/`.
2. Read `<name>_incorta_build_plan.xlsx` (Overview first) and `<name>_doc.md`. Report, in this order:
   - Decision Log rows to settle, with the recommendation;
   - Measures and MVs with status Needs design, and why;
   - Manual Checks;
   - the Power Query Lineage sheet: queries Not translated, and MVs whose check is Differs or Partly checked
     (read the check detail: Partly checked means source columns Power BI did not save);
   - joins whose parent key is not proven unique or whose types needed a cast.
3. For a wrong row, fix the rule, not the output:
   - table / join / load filter / key: `pbi_insights/incorta/plan.py` (`_tables`, `_power_query`, `_joins`);
   - formula text: `pbi_insights/analyze/formulas.py` (`ToolFormula`, Incorta branch) or the DAX translator
     (dax-to-sql skill) when the IR itself is wrong;
   - MV SQL: `pbi_insights/incorta/sparksql.py` (DAX tables / columns) or `pbi_insights/incorta/m2sql.py`
     (Power Query steps; add a `t_Table_<Function>` handler or a row-expression case in `row_call`).
   Keep spec.md and the code in step: when a rule changes, update `pbi_insights/incorta/spec.md`.
4. Verify: `python -m pytest -q tests/test_incorta.py`, then rerun step 1. Check the MVs against the values
   Power BI stored in the file: `python tools\check_incorta_mvs.py $ARGUMENTS` (needs `pip install duckdb`).

Rules to keep: physical formula columns are row level, can read parents through joins, cannot aggregate or use
another formula column of the same table; one MV per grain; unsure items carry [MANUAL CHECK REQUIRED]. Never paste client
data rows into chats, prompts or docs.
