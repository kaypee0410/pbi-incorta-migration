"""Write the Incorta build package: one SQL file per Incorta object, a design document, a lineage diagram, the build-plan workbook and the plan JSON, zipped like the Dataprep → Incorta packages.

<slug>__<object>.sql          header with the Incorta configuration, then the dataset / MV Spark SQL
<slug>_doc.md / .pdf          A. Summary, B. Key transformations, C. Incorta optimization, D. Manual intervention,
                              E. Build steps, F. Decision log, appendices (measures, insights, SQL)
<slug>_dag.png                sources → Incorta tables → MVs → business schema → dashboard tabs
<slug>_pq_lineage.png         every Power Query query by level: physical tables and MVs, arrows = reads
<slug>_incorta_build_plan.xlsx  Overview + the build-plan sheets of the spec
<slug>_incorta_plan.json      the whole plan, for re-runs and tooling
"""
from __future__ import annotations

import json
import logging
import re
import textwrap
import zipfile
from pathlib import Path

from .. import __version__
from .plan import MANUAL, NEEDS_DESIGN, NOT_NEEDED, ident, one_line, schema_name
from .sheets import write_build_plan

log = logging.getLogger(__name__)

def slug_of(name: str) -> str:
    return schema_name(name).lower()


# ------------------------------------------------------------------------------------------ SQL files
def _header(title: str, lines: list[str], sections: dict[str, list[str]]) -> str:
    out = [title] + list(lines)
    for name, items in sections.items():
        if items:
            out.append(f"{name}:")
            out += [f"- {x}" for x in items]
    return "/*\n" + "\n".join(_safe_comment(x) for x in out) + "\n*/\n"


def _safe_comment(s: str) -> str:
    return str(s).replace("*/", "* /")


def table_sql(plan: dict, s: dict, joins: list[dict], fcols: list[dict]) -> str:
    schema = plan["summary"]["schema"]
    name = s["incorta_table"]
    obj = f"{schema}.{name}"
    cfg = [f"1. Physical schema table - {obj}", "Incorta Configuration:", f"- Schema: {schema}",
           f"- Table Name: {name}", f"- Data Source: {s['data_source'] or '-'}"]
    if s["source"]:
        cfg.append(f"- Source: {s['source']}" + (" (upload it as a data file)" if "file" in s["kind"] else ""))
    cfg += [f"- Load Type: {s['load_type'] or 'Full'}", f"- Key Columns: {s['key'] or '(none: fact table)'}"]
    if s["load_filter"]:
        cfg.append(f"- Load Filter: {s['load_filter']}")
        cfg.append(f"  (Power Query: {s['power_query_filters']})")
    if s.get("pq_mv"):
        cfg.append(f"- Read by: {schema}.{s['pq_mv']} (the Power Query steps of {s['pbi_table']})")
    elif s["reads_as"] and s["reads_as"] != name:
        cfg.append(f"- Read by: {schema}.{s['reads_as']} (insights and joins use the MV)")
    if s.get("pq_mv"):
        cfg.append(f"- Rows in Power BI: {s['rows']:,} after the Power Query steps (compare with {s['pq_mv']})"
                   if isinstance(s.get("rows"), int) else "- Rows in Power BI: ?")
    else:
        cfg.append(f"- Rows in Power BI: {s['rows']:,}" if isinstance(s.get("rows"), int) else "- Rows in Power BI: ?")
    if s.get("file_steps"):
        cfg.append("- File selection in Power Query: " + "; ".join(s["file_steps"]))
    cfg.append(f"- Status: {s['status']} (rule engine)")
    types = s.get("types") or {}
    sections = {
        f"Columns to load ({len(types)})": [f"{c} ({t})" for c, t in types.items()],
        "Feeds (Power Query)": [", ".join(s.get("feeds") or [])] if s.get("feeds") else [],
        "Columns not loaded (not used by any insight, measure or join)": [s["columns_not_loaded"]] if s["columns_not_loaded"] else [],
        "Formula Columns": [f"{f['incorta_column']} = {f['formula']}\n  DAX: {one_line(f['dax'], 300)}" for f in fcols
                            if f["home"] == "Physical formula column"],
        "Joins (this table is the child)": [f"{j['id']}: {', '.join(j['child_columns'])} -> {j['parent']} "
                                            f"({', '.join(j['parent_columns'])})" for j in joins if j.get("child") == obj],
        "Joins (this table is the parent)": [f"{j['id']}: {j['child']} ({', '.join(j['child_columns'])})" for j in joins
                                             if j.get("parent") == obj],
        "Notes": [n.replace(MANUAL, "[MANUAL CHECK REQUIRED]") for n in s["notes"]],
    }
    head = _header(f"Incorta Build Mappings for {name}", cfg, sections)
    if s["dataset_sql"]:
        body = ("-- Dataset SQL for the Incorta table (from the Power BI native query / source object)\n"
                + s["dataset_sql"].rstrip().rstrip(";") + ";\n")
    elif s.get("pq_mv"):
        rows = f"{s['rows']:,}" if isinstance(s.get("rows"), int) else "?"
        body = (f"-- No dataset SQL: Incorta loads this table from the data file as it is ({len(types)} columns).\n"
                f"-- The Power Query steps run in {schema}.{s['pq_mv']}; Power BI has {rows} rows after them.\n"
                f"-- Check after the first load (rows in the file):\n"
                f"SELECT COUNT(*) AS row_count FROM {obj};\n")
    else:
        keys = [k.strip() for k in (s["key"] or "").split(",") if k.strip()]
        key_expr = (f"COUNT(DISTINCT {keys[0]})" if len(keys) == 1 else
                    f"COUNT(DISTINCT CONCAT_WS('|', {', '.join(keys)}))" if keys else "NULL")
        rows = f"{s['rows']:,}" if isinstance(s.get("rows"), int) else "?"
        body = ("-- No dataset SQL: this table loads straight from its data file.\n"
                f"-- Reconciliation after the first load: Power BI has {rows} rows"
                + (" (one per key)" if keys else "") + ".\n"
                f"SELECT\n  COUNT(*) AS row_count,\n  {key_expr} AS distinct_keys\nFROM {obj};\n")
    return head + body


def mv_sql(plan: dict, mv: dict, joins: list[dict]) -> str:
    schema = plan["summary"]["schema"]
    obj = f"{schema}.{mv['name']}"
    cfg = [f"1. Materialized view - {obj}", "Incorta Configuration:", f"- Schema: {schema}",
           f"- Table Name: {mv['name']}", f"- Type: Materialized view ({mv['language']})",
           f"- Reads: {', '.join(mv['reads']) or '-'}", f"- Grain: {mv['grain']}",
           f"- Key: {mv['key'] or '(none: one row per source row)'}",
           f"- Load: Full, after {', '.join(mv['reads']) or 'its sources'}",
           f"- Serves: {', '.join(mv['serves'])} ({len(mv['serves'])} insights)" if mv["serves"] else "- Serves: -",
           f"- Why an MV: {mv['reason']}", f"- Rule: {mv['rule']}",
           f"- Status: {mv['status']} (rule engine)"]
    if mv.get("check"):
        cfg.append(f"- Check on the rows saved in the .pbix: {mv['check']}"
                   + (f" ({mv['check_detail']})" if mv.get("check_detail") else ""))
    replaced = [c for c in mv.get("columns") or []]
    sections = {
        "Power BI objects replaced": ([f"Calculated table {mv['pbi_table']}"] if mv["kind"].startswith("Calc") else
                                      [f"Power Query query {mv['pbi_table']} ({mv.get('steps', '?')} steps)"]
                                      if mv["kind"] == "Power Query MV" else
                                      [f"Calculated columns of {mv['pbi_table']}: "
                                       + ", ".join(c for c in replaced if c in _calc_names(plan, mv['pbi_table']))]),
        "Feeds": [", ".join(mv.get("feeds") or [])] if mv.get("feeds") else [],
        "Joins (this MV is the child)": [f"{j['id']}: {', '.join(j['child_columns'])} -> {j['parent']} "
                                         f"({', '.join(j['parent_columns'])})" for j in joins if j.get("child") == obj],
        "Joins (this MV is the parent)": [f"{j['id']}: {j['child']} ({', '.join(j['child_columns'])})" for j in joins
                                          if j.get("parent") == obj],
        "Notes": list(mv.get("notes") or []) + [f"[MANUAL CHECK REQUIRED] {r}" for r in mv.get("reviews") or []],
    }
    body = mv["sql"].rstrip().rstrip(";") + ";\n"
    return _header(f"Incorta Build Mappings for {mv['name']}", cfg, sections) + body


def _calc_names(plan, table) -> set:
    return {f["column"] for f in plan["formula_columns"] if f["table"] == table}


# ------------------------------------------------------------------------------------------ document
def _md_table(headers: list[str], rows: list[list]) -> str:
    def cell(x):
        t = str(x if x is not None else "")
        if len(t) > 700:                       # a table row must fit on a page: the workbook has the full text
            t = t[:700] + " ... (full text in the build plan workbook)"
        return t.replace("|", "\\|").replace("<", "&lt;").replace("\n", "<br>")

    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    out += ["| " + " | ".join(cell(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def _wrap_code(text: str, width: int = 112) -> str:
    out = []
    for line in str(text).splitlines():
        if len(line) <= width:
            out.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        out += textwrap.wrap(line, width=width, subsequent_indent=" " * (indent + 4), break_long_words=True,
                             break_on_hyphens=False, drop_whitespace=False) or [line]
    return "\n".join(out)


def _break_formula(text: str, width: int = 96) -> str:
    """Break a long one-line formula after commas so it reads (and prints) in lines of ~width characters."""
    if len(text) <= width:
        return text
    out, line = [], ""
    for part in re.split(r"(?<=,) ", text):
        if line and len(line) + len(part) + 1 > width:
            out.append(line)
            line = "    " + part
        else:
            line = f"{line} {part}" if line else part
    out.append(line)
    return "\n".join(out)


def build_steps(plan: dict, files: dict) -> list[str]:
    s = plan["summary"]
    schema = s["schema"]
    src = [x for x in plan["source_map"] if x["schema"]]
    file_tables = [x for x in src if "data file" in x["kind"]]
    sql_tables = [x for x in src if "(SQL)" in x["kind"]]
    steps = []
    if file_tables:
        steps.append("Data source: create (or reuse) a Data Files data source and upload "
                     + ", ".join(x["source"] or x["pbi_table"] for x in file_tables) + ".")
    if sql_tables:
        steps.append("Data source: create (or reuse) one Incorta data source per database: "
                     + ", ".join(sorted({x["data_source"] for x in sql_tables})) + ".")
    phys = [x["incorta_table"] for x in src if x["incorta_table"] and "Materialized" not in x["kind"]]
    steps.append(f"Physical schema {schema}: create the {len(phys)} table(s) "
                 + ", ".join(phys) + "; dataset SQL, key columns and load filters are in each table's .sql file.")
    fc = [f for f in plan["formula_columns"] if f["home"] == "Physical formula column"]
    if fc:
        steps.append("Formula columns: add " + ", ".join(f"{f['incorta_table']}.{f['incorta_column']}" for f in fc)
                     + " (Incorta Formula Columns sheet).")
    pq = [m for m in plan["mvs"] if m["kind"] == "Power Query MV"]
    if pq:
        by_depth: dict[int, list[str]] = {}
        for m in pq:
            by_depth.setdefault(m.get("depth", 0), []).append(m["name"])
        for d in sorted(by_depth):
            steps.append(f"Power Query MVs, level {d} ({'read the physical tables' if d == 0 else 'read the level-' + str(d - 1) + ' objects and below'}): "
                         + ", ".join(by_depth[d]) + " (paste each .sql file; same schema, so one load runs them in order).")
    for m in plan["mvs"]:
        if m["kind"] != "Power Query MV":
            steps.append(f"Materialized view {schema}.{m['name']}: new MV ({m['language']}), paste {files.get(m['name'], '')}.")
    js = [j for j in plan["joins"] if j["status"] != NOT_NEEDED]
    if js:
        steps.append("Joins: " + "; ".join(f"{j['id']} {j['child']}({', '.join(j['child_columns'])}) -> "
                                           f"{j['parent']}({', '.join(j['parent_columns'])})" for j in js) + ".")
    steps.append(f"Load {schema} (full load) and check row counts against the Source Map and each table's "
                 f"reconciliation query.")
    steps.append(f"Business schema {s['business_schema']}: one view per table with the Power BI display names, keys "
                 f"hidden, and the measure formulas (Incorta Business Schema sheet).")
    steps.append(f"Dashboard {s['dashboard']}: tabs {', '.join(s['tabs'])}; dashboard filters from the slicers, then "
                 f"the insights in the Incorta Insights sheet (base table, formulas, sort).")
    steps.append("Reconcile every insight with Power BI under the same filters; record the result in the Insights sheet.")
    return steps


def doc_markdown(plan: dict, data: dict, files: dict) -> str:
    s = plan["summary"]
    src = [x for x in plan["source_map"] if x["schema"]]
    sources = [x["source"] or x["pbi_table"] for x in src if "data file" in x["kind"] or "(SQL)" in x["kind"]]
    fact = max(src, key=lambda x: x.get("rows") or 0) if src else None
    total_measures = len(data.get("measures") or [])
    reasons = []
    if any(m["kind"].startswith("Table MV") for m in plan["mvs"]):
        reasons.append("per-row aggregate in calculated columns (table MV)")
    feats = {f for m in plan["measures"] for f in m.get("features", [])}
    if {"ago", "toDate"} & feats:
        reasons.append("time intelligence")
    if "rank" in feats:
        reasons.append("ranking")
    if "groupBy" in feats:
        reasons.append("share-of-total / benchmark measures")
    if s["measures_needing_design"]:
        reasons.append(f"{s['measures_needing_design']} measure(s) needing a decision")
    tabs = s["tabs"]
    measures_top = [m["measure"] for m in sorted(plan["measures"], key=lambda m: -len(m["used_by"]))[:5]]
    dims_top = []
    for i in plan["insights"]:
        for d in (i["dimensions"] or "").split(", "):
            lab = d.split(".")[-1].replace("_", " ") if d else ""
            if lab and lab not in dims_top:
                dims_top.append(lab)
    purpose = (
        f"Rebuild the Power BI report {s['report']} in Incorta: {len(tabs)} tab(s) ({', '.join(tabs)}) with "
        f"{s['insights']} data insights showing {', '.join(measures_top)} by {', '.join(dims_top[:6])}.")
    grain = (
        f"One row per {fact['pbi_table']} row ({fact['rows']:,} rows in the .pbix)" if fact and fact.get("rows") else "-")
    pq_mvs = [m for m in plan["mvs"] if m["kind"] == "Power Query MV"]
    lin = plan.get("pq_lineage") or []
    report_objs = [x["object"] for x in lin if x["role"] == "report table"]
    if pq_mvs:
        dsrc = sorted({x["data_source"] for x in src if x["incorta_table"] and "Materialized" not in x["kind"]})
        flow_default = (f"{len(dsrc)} data source(s) → {s['tables_loaded']} physical table(s) loaded as-is → "
                        f"{len(pq_mvs)} Power Query MV(s) in {max(m.get('depth', 0) for m in pq_mvs) + 1} level(s), one per "
                        f"query, reading each other as the queries do → {len(report_objs)} report object(s) "
                        f"({', '.join(report_objs[:8])}{', ...' if len(report_objs) > 8 else ''})"
                        + (f" + {len(plan['mvs']) - len(pq_mvs)} other MV(s)" if len(plan["mvs"]) > len(pq_mvs) else "")
                        + f" → business schema {s['business_schema']} → dashboard {s['dashboard']} ({len(tabs)} tabs, "
                          f"{s['insights']} insights)")
    else:
        flow_default = (f"{len(sources)} source(s) → {s['tables_loaded']} physical table(s) → {s['mvs']} MV(s) "
                        f"({', '.join(m['name'] for m in plan['mvs'])}) → business schema {s['business_schema']} → "
                        f"dashboard {s['dashboard']} ({len(tabs)} tabs, {s['insights']} insights)")
    flow = flow_default
    L = [f"# Incorta build design: {s['report']}", "",
         f"Generated by PBI Extraction Insights {__version__} on {plan['generated_at']} from "
         f"{data['summary'].get('file')}. Rules: {plan['spec']}.", "",
         "## A. Summary", "",
         _md_table(["Item", "Value"], [
             ["Report", s["report"]],
             ["Complexity", f"{s['complexity']}" + (f": {', '.join(reasons)}" if reasons else "")],
             ["Size", f"{len(plan['source_map'])} Power BI tables ({s['tables_loaded']} loaded, {s['mvs']} MVs), "
                      f"{s['fact_rows']:,} fact rows, {len(tabs)} tabs, {s['insights']} data insights, "
                      f"{s['measures']} measures in scope ({total_measures} in the model)"],
             ["Purpose", purpose],
             ["Source names", (", ".join(sorted({x["data_source"] for x in src if x["incorta_table"] and "Materialized" not in x["kind"]}))
                               if pq_mvs else ", ".join(sources)) or "-"],
             ["Destination", f"Physical schema {s['schema']}, business schema {s['business_schema']}, dashboard "
                             f"{s['dashboard']}"],
             ["Business grain", grain],
             ["High-level flow pattern", flow],
         ]), ""]
    # ---- B
    L += ["## B. Key transformations and business logic", ""]
    items: list[str] = []
    if not items and pq_mvs:
        by_ds: dict[str, list[str]] = {}
        for x in src:
            if x["incorta_table"] and "Materialized" not in x["kind"]:
                by_ds.setdefault(x["data_source"], []).append(x["incorta_table"])
        for ds, tabs_ in sorted(by_ds.items()):
            items.append(f"**Load from {ds}**: {len(tabs_)} table(s) as Power BI reads them ({', '.join(tabs_[:12])}"
                         f"{', ...' if len(tabs_) > 12 else ''}); no logic in the dataset SQL beyond the native query.")
        for x in lin:
            if x["role"] == "report table":
                items.append(f"**{x['query']}** is built by {x['object']}"
                             + (f" from {', '.join(x['reads_queries'])}" if x["reads_queries"] else
                                f" from its source ({x['source']})") + f" ({x['steps']} Power Query steps). "
                             "See Power Query lineage below.")
    if not items:
        for x in src:
            if "data file" in x["kind"] or "(SQL)" in x["kind"]:
                t = f"**Load {x['pbi_table']}** from {x['source'] or x['data_source']}"
                if x["load_filter"]:
                    t += f"; keep rows where {x['power_query_filters'] or x['load_filter']} (load filter)"
                if x["key"]:
                    t += f"; key {x['key']}"
                items.append(t + ".")
        for m in plan["mvs"]:
            cols = [f for f in plan["formula_columns"] if f["table"] == m["pbi_table"]]
            if m["kind"] == "Power Query MV":
                continue
            if m["kind"].startswith("Calc"):
                items.append(f"**{m['pbi_table']}** (DAX calculated table) becomes {m['name']}: {m['grain']}, "
                             f"columns {', '.join(m['columns'])}.")
            else:
                items.append(f"**{m['pbi_table']} calculated columns** ({', '.join(c['column'] for c in cols)}) are "
                             f"computed in {m['name']}: {m['reason']}.")
        for f in plan["formula_columns"]:
            if f["home"] == "Physical formula column":
                items.append(f"**{f['table']}[{f['column']}]**: {one_line(f['purpose'] or f['dax'], 220)} "
                             f"(formula column).")
        groups: dict[str, list[str]] = {}
        for m in plan["measures"]:
            groups.setdefault(m["pattern"] or "Other", []).append(m["measure"])
        for pat, names in groups.items():
            items.append(f"**Measures, {pat.lower()}**: {', '.join(names)}.")
    if pq_mvs:
        for m in plan["mvs"]:
            if m["kind"] != "Power Query MV":
                cols = [f for f in plan["formula_columns"] if f["table"] == m["pbi_table"]]
                items.append(f"**{m['pbi_table']}** ({m['kind']}) becomes {m['name']}: {m['reason']}.")
        for f in plan["formula_columns"]:
            if f["home"] == "Physical formula column":
                items.append(f"**{f['table']}[{f['column']}]**: {one_line(f['purpose'] or f['dax'], 220)} "
                             f"(formula column).")
        groups2: dict[str, list[str]] = {}
        for m in plan["measures"]:
            groups2.setdefault(m["pattern"] or "Other", []).append(m["measure"])
        for pat, names in groups2.items():
            items.append(f"**Measures, {pat.lower()}**: {', '.join(names)}.")
    L += [f"{i}. {t}" for i, t in enumerate(items, 1)] + [""]
    if lin:
        L += _lineage_markdown(plan, lin)
    # ---- C
    L += ["## C. Incorta optimization", "", "### Recommended approach", ""]
    L += [f"One physical schema {s['schema']} with {s['tables_loaded']} table(s), {s['mvs']} MV(s) and {s['joins']} "
          f"join(s); business schema {s['business_schema']} with one view per table and {s['business_view_formulas']} "
          f"reusable measure formula(s); {s['insight_formulas']} measure(s) written in the insights that need the "
          f"insight's own grouping (rank, level-based totals). Each piece of logic sits in the earliest layer that keeps "
          f"it correct (spec step 4).", ""]
    L += ["### What the Incorta data model replaces (no MV needed)", ""]
    bypass: list[str] = []
    for j in plan["joins"]:
        if j.get("composite_from"):
            bypass.append(f"{j['child_table']}[{j['composite_from'][0]}] / {j['parent_table']}"
                          f"[{j['composite_from'][1]}]: composite join {j['id']} on {', '.join(j['child_columns'])}"
                          f" instead of concatenated key columns.")
    for f in plan["formula_columns"]:
        if "lookup(" in (f["formula"] or ""):
            bypass.append(f"{f['table']}[{f['column']}]: LOOKUPVALUE becomes an Incorta lookup() formula column.")
    for x in src:
        if x["load_filter"]:
            bypass.append(f"{x['pbi_table']}: the Power Query row filter becomes the table's load filter.")
        if any("duplicate" in n for n in x["notes"]):
            bypass.append(f"{x['pbi_table']}: Power Query 'remove duplicates' becomes the key {x['key']}.")
    excl = [x["pbi_table"] for x in plan["source_map"] if not x["schema"]]
    if excl:
        bypass.append("Not built: " + ", ".join(excl) + " (measure tables become business views; unused tables "
                      "are left out).")
    if any(j["status"] == NOT_NEEDED and "inactive" in " ".join(j["notes"]) for j in plan["joins"]):
        bypass.append("Inactive relationships: no aliases needed (no USERELATIONSHIP).")
    L += [f"- {b}" for b in bypass] + [""]
    L += ["### What should be a materialized view", ""]
    if pq_mvs:
        L.append(f"- **{len(pq_mvs)} Power Query MVs**, one per Power BI query with steps (names end in _MV, the physical "
                 f"table of the same query has the name without _MV). Each reads the Incorta objects of the queries it "
                 f"references, so the Incorta chain is the Power Query chain: unions, merges and cleaning happen in the "
                 f"same order and can be compared layer by layer.")
    L += [f"- **{m['name']}** ({m['grain']}): {m['reason']}. Serves {len(m['serves'])} insight(s)." for m in plan["mvs"]
          if m["kind"] != "Power Query MV" or m["serves"]]
    L += ["", "### Suggested MV design", ""]
    if pq_mvs:
        L.append("- Power Query MVs: one CTE per materialized step (named after the Power Query step), then the final "
                 "SELECT with the model's column names; joins are LEFT JOINs with null-safe keys (<=>) as Table.NestedJoin "
                 "matches nulls; Table.Combine is UNION ALL by column name; replacements become CASE / REPLACE.")
    for m in plan["mvs"]:
        if m["kind"] == "Power Query MV" and not m["serves"]:
            continue
        steps = re.findall(r"-- Step (\d+): ([^\n]+)", m["sql"])
        L.append(f"- **{m['name']}** reads {', '.join(m['reads']) or '-'}; key {m['key'] or 'none'}; "
                 f"{len(m['columns'])} columns" + (f"; {len(steps)} steps (one per dependency level of the calculated "
                                                    f"columns)" if steps else "") + f". SQL: {files.get(m['name'], '')}.")
    L += ["", "### Why this is optimal", ""]
    L += ["Row-level logic stays in load-time formula columns or one MV per grain, so insights read stored columns; "
          "joins replace Power BI relationships one to one (child to parent, left outer); measures reused by several "
          "insights are defined once in the business schema; only logic that depends on the insight's own grouping "
          "(rank, share of total) is written in the insight.", ""]
    L += ["### Data model considerations", ""]
    cons: list[str] = []
    for j in plan["joins"]:
        if j["status"] == NOT_NEEDED:
            continue
        cons.append(f"{j['id']}: {j['child']} → {j['parent']} on {', '.join(j['child_columns'])} = "
                    f"{', '.join(j['parent_columns'])}; parent key: {j['key_unique']}; types: {j['types_match']}.")
    for m in plan["mvs"]:
        if m["kind"] == "Power Query MV" and not m["serves"]:
            continue
        cons.append(f"{m['name']} loads after {', '.join(m['reads']) or 'its sources'} (same schema, one load job).")
    if pq_mvs:
        cons.append(f"All {len(pq_mvs)} Power Query MVs sit in schema {s['schema']}: Incorta loads them after the "
                    f"physical tables, each after the objects it reads (levels 0 to "
                    f"{max(m.get('depth', 0) for m in pq_mvs)} in the lineage).")
    if any("CAST(" in m["sql"] and "AS DATE" in m["sql"] for m in plan["mvs"]):
        cons.append("Date columns are cast to DATE in the MVs so joins to the date MV compare the same type.")
    for i in plan["insights"]:
        for n in i["notes"]:
            if "base table" in n:
                cons.append(f"{i['id']}: {n}")
    L += [f"- {c}" for c in cons] + [""]
    L += ["### Incorta objects to build", ""]
    objs = []
    for x in src:
        if "Materialized" not in x["kind"]:
            objs.append([f"{s['schema']}.{x['incorta_table']}", x["kind"], files.get(x["incorta_table"], "")])
    for m in plan["mvs"]:
        objs.append([f"{s['schema']}.{m['name']}", m["kind"], files.get(m["name"], "")])
    for j in plan["joins"]:
        if j["status"] != NOT_NEEDED:
            objs.append([j["id"], "Join", f"{j['child']} → {j['parent']}"])
    objs.append([s["business_schema"], "Business schema",
                 f"{len({b['view'] for b in plan['business_schema']})} views, {s['business_view_formulas']} formulas"])
    objs.append([s["dashboard"], "Dashboard", f"{len(tabs)} tabs, {s['insights']} insights, {s['dashboard_filters']} filters"])
    L += [_md_table(["Object", "Type", "File / detail"], objs), ""]
    # ---- D
    L += ["## D. Manual intervention", ""]
    k = 0
    for c in plan["manual_checks"]:
        k += 1
        L += [f"{k}. **{c.get('object', '')}**: {c.get('issue', '')}",
              f"    - Impact: {c.get('impact', '')}", f"    - Risk: {c.get('risk', '')}",
              f"    - Required action: {c.get('action', '')}"]
    for d in plan["decisions"]:
        if d["status"] in ("Proposed",) and d["topic"].startswith("Measure"):
            k += 1
            L += [f"{k}. **{d['topic']}** ({d['id']}): {d['decision']}", f"    - Options: {d['options']}",
                  f"    - Recommendation: {d['recommendation'] or 'developer decision'}"]
    if k == 0:
        L.append("None.")
    L += ["", f"**Complexity assessment:** {s['complexity']}" + (f" ({', '.join(reasons)})." if reasons else "."), ""]
    # ---- E
    L += ["## E. Build steps", ""]
    L += [f"{i}. {t}" for i, t in enumerate(build_steps(plan, files), 1)] + [""]
    # ---- F
    L += ["## F. Decision log (settle before building)", "",
          _md_table(["ID", "Topic", "Decision", "Recommendation", "Status"],
                    [[d["id"], d["topic"], d["decision"], d["recommendation"], d["status"]] for d in plan["decisions"]]),
          ""]
    # ---- appendices
    L += ["## Appendix 1: measures", ""]
    for m in plan["measures"]:
        L += [f"### {m['measure']}", "", f"Home: {m.get('home', '-')}. Status: {m['status']}."
              + (f" Used by {', '.join(m['used_by'])}." if m["used_by"] else "")
              + (f" Decision {m['decision']}." if m.get("decision") else ""), ""]
        if m["formula"]:
            L += ["```text", _wrap_code(_break_formula(m["formula"]), 100), "```", ""]
    L += ["## Appendix 2: insights by tab", ""]
    for tab in tabs:
        rows = [[i["id"], i["visualization"], i["title"], i["dimensions"].replace(f"{s['business_schema']}.", ""),
                 i["measures"].replace(f"{s['business_schema']}.", "")]
                for i in plan["insights"] if i["tab"] == tab]
        L += [f"### {tab}", "", _md_table(["ID", "Incorta", "Title", "Dimensions", "Measures"], rows), ""]
    if plan.get("dashboard_filters"):
        L += ["### Dashboard filters", "", _md_table(
            ["Tab", "Filter", "From"], [[f["tab"], f["field"], f["visual"]] for f in plan["dashboard_filters"]]), ""]
    L += ["## Appendix 3: SQL", ""]
    shown = [m for m in plan["mvs"] if m["kind"] != "Power Query MV" or m["serves"]]
    if len(shown) < len(plan["mvs"]):
        L += [f"The SQL of the {len(plan['mvs']) - len(shown)} upstream Power Query MVs is in their .sql files "
              f"(listed in Incorta objects to build). Below: the MVs the insights read.", ""]
    for m in shown:
        L += [f"### {m['name']}", "", "```sql", _wrap_code(m["sql"]), "```", ""]
    return "\n".join(L)


def _lineage_markdown(plan: dict, lin: list[dict]) -> list[str]:
    """Power Query lineage: each report table as a tree down to its physical tables, then the check per layer."""
    by = {x["query"]: x for x in lin}
    L = ["### Power Query lineage", "",
         "How each table the report reads is built in Power BI, query by query, and the Incorta object that rebuilds "
         "each step. Physical tables load the source as-is; every query with steps is an MV.", ""]

    def tree(q, depth, seen):
        x = by.get(q)
        if not x:
            return
        pad = "    " * depth
        obj = x["mv"] or x["physical"] or x["object"]
        extra = f" + table {x['physical']}" if x["mv"] and x["physical"] else ""
        chk = f", check: {x['check']}" if x.get("check") else ""
        src = "" if x["reads_queries"] else f", source {x['source']}"
        if q in seen:
            L.append(f"{pad}- {q} → {obj} (see above)")
            return
        seen.add(q)
        L.append(f"{pad}- **{q}** → {obj}{extra} ({x['steps']} steps{src}{chk})")
        for r in x["reads_queries"]:
            tree(r, depth + 1, seen)

    for x in lin:
        if x["role"] == "report table":
            tree(x["query"], 0, set())
    L.append("")
    checks = [x for x in lin if x.get("check")]
    if checks:
        from collections import Counter
        cnt = Counter(x["check"] for x in checks)
        L += ["### Layer checks (MV SQL run on the rows saved in the .pbix)", "",
              "Each MV was run in DuckDB over the rows Power BI saved for its inputs (and, for a source query, over its "
              "own source columns rebuilt from the saved output), then every output column was compared with Power "
              "BI's saved column. " + ", ".join(f"{n} {k}" for k, n in cnt.most_common()) + ".", "",
              _md_table(["Level", "Query", "Incorta object", "Check", "Rows (MV / Power BI)", "Detail"],
                        [[x["depth"], x["query"], x["object"], x["check"], x.get("check_rows", ""),
                          one_line(x.get("check_detail", ""), 260)] for x in sorted(checks, key=lambda x: (x["depth"], x["query"]))
                         if x["check"] != "No MV"]), ""]
    elif plan.get("pq_checks_note"):
        L += [f"Layer checks {plan['pq_checks_note']}.", ""]
    return L


# ------------------------------------------------------------------------------------------ PDF + PNG
PDF_CSS = """
body { font-family: sans-serif; font-size: 9pt; line-height: 1.35; color: #1d2b25; }
h1 { font-size: 17pt; color: #1e6b52; margin-bottom: 6pt; }
h2 { font-size: 13pt; color: #1e6b52; margin-top: 14pt; border-bottom: 1px solid #1e6b52; }
h3 { font-size: 10.5pt; margin-top: 10pt; }
h4 { font-size: 9.5pt; margin-top: 8pt; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0; }
th { background-color: #e7f0ec; font-weight: bold; text-align: left; border: 1px solid #b9cbc3; padding: 2pt; }
td { border: 1px solid #c9d6d0; padding: 2pt; vertical-align: top; }
code { font-family: monospace; font-size: 7.5pt; }
pre { font-family: monospace; font-size: 6.8pt; background-color: #f4f7f5; padding: 4pt; }
li { margin-bottom: 2pt; }
"""


def _escape_html(md_text: str) -> str:
    """Text from the report (names, DAX, M) may contain '<' (e.g. <Blank>, < 1 month): outside code it would be
    read as an HTML tag and swallow the rest of the document. Escape it, keeping the <br> line breaks we add."""
    out, fence = [], False
    for line in md_text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        parts = line.split("`")
        for i in range(0, len(parts), 2):                # even parts are outside inline code
            parts[i] = parts[i].replace("<br>", "\x00").replace("<", "&lt;").replace("\x00", "<br>")
        out.append("`".join(parts))
    return "\n".join(out)


def _plain_html(md_text: str) -> str:
    """Last-resort layout: every line its own paragraph (always paginates)."""
    import html as _html

    out, fence = [], False
    for line in md_text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        esc = _html.escape(line)
        if fence:
            out.append(f'<p style="font-family: monospace; font-size: 7pt; margin: 0">{esc or "&nbsp;"}</p>')
        elif line.startswith("#"):
            out.append(f"<h3>{_html.escape(line.lstrip('#').strip())}</h3>")
        elif line.strip():
            out.append(f"<p>{esc}</p>")
    return "\n".join(out)


def _layout(html: str, max_pages: int):
    import io

    import pymupdf

    story = pymupdf.Story(html=html, user_css=PDF_CSS)
    media = pymupdf.paper_rect("a4")
    where = media + (42, 48, -42, -48)
    buf = io.BytesIO()                      # in memory: no temporary file for Windows to keep locked
    writer = pymupdf.DocumentWriter(buf)
    more, pages = True, 0
    while more and pages < max_pages:
        dev = writer.begin_page(media)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        pages += 1
    writer.close()
    return (None if more else buf.getvalue()), media


def markdown_to_pdf(md_text: str, pdf_path, title: str) -> Path:
    """Markdown -> A4 PDF. PyMuPDF can loop forever on some page breaks (a table row taller than what is left);
    the page count is capped by the text size and, past the cap, the document is laid out again line by line."""
    import markdown
    import pymupdf

    md_text = _escape_html(md_text)
    cap = 80 + len(md_text) // 600
    data, media = _layout(markdown.markdown(md_text, extensions=["tables", "fenced_code"]), cap)
    simplified = data is None
    if simplified:
        log.warning("PDF layout did not finish for %s: simplified layout", title)
        data, media = _layout(_plain_html(md_text), cap * 3)
        if data is None:
            raise RuntimeError("the PDF layout did not finish (the .md file has the same content)")
    doc = pymupdf.open("pdf", data)
    n = len(doc)
    for i, page in enumerate(doc, 1):
        page.insert_text((42, media.height - 24), f"{title}  ·  page {i} of {n}"
                         + ("  ·  simplified layout (see the .md file)" if simplified else ""),
                         fontsize=7, color=(0.4, 0.45, 0.43))
    doc.set_metadata({"title": title, "creator": "PBI Extraction Insights", "producer": "PyMuPDF"})
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    Path(pdf_path).write_bytes(out)
    return Path(pdf_path)


def dag_png(plan: dict, path) -> Path:
    """Lineage: sources → Incorta tables → MVs → business views → dashboard tabs."""
    if any(m["kind"] == "Power Query MV" for m in plan["mvs"]):
        return _dag_with_lineage(plan, path)
    import pymupdf

    s = plan["summary"]
    src = [x for x in plan["source_map"] if x["schema"]]
    sources = [(x["pbi_table"], x["source"] or x["data_source"]) for x in src if "Materialized" not in x["kind"]]
    tables = [(x["pbi_table"], x["incorta_table"]) for x in src if "Materialized" not in x["kind"]]
    mvs = [(m["pbi_table"], m["name"], m) for m in plan["mvs"]]
    views = sorted({b["view"] for b in plan["business_schema"]})
    tabs = s["tabs"]
    cols = [sources, tables, mvs, views, tabs]
    n = max(len(c) for c in cols) or 1
    W, H = 1240, max(380, 150 + n * 66)
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    xs, bw, bh = [30, 270, 510, 760, 1010], 200, 40
    heads = ["Sources", f"Physical schema {s['schema']}", "Materialized views", f"Business schema {s['business_schema']}",
             f"Dashboard {s['dashboard']}"]
    for x, h in zip(xs, heads):
        page.insert_textbox(pymupdf.Rect(x, 18, x + bw, 50), h, fontsize=10, fontname="helv", align=1,
                            color=(0.12, 0.42, 0.32))

    def ys(count):
        top = 70 + (n - count) * 33
        return [top + i * 66 for i in range(count)]

    pos = {}
    BLUE, LBLUE, ORANGE, PURPLE, GREEN = (0.30, 0.52, 0.80), (0.86, 0.92, 0.98), (0.96, 0.62, 0.25), (0.62, 0.52, 0.80), (0.27, 0.62, 0.42)

    def box(rect, label, fill, stroke, shape="rect", sub=""):
        sh = page.new_shape()
        if shape == "cyl":
            e = 8
            sh.draw_rect(pymupdf.Rect(rect.x0, rect.y0 + e / 2, rect.x1, rect.y1 - e / 2))
            sh.finish(color=None, fill=fill)
            sh.draw_oval(pymupdf.Rect(rect.x0, rect.y1 - e, rect.x1, rect.y1))
            sh.finish(color=stroke, fill=fill, width=1)
            sh.draw_line((rect.x0, rect.y0 + e / 2), (rect.x0, rect.y1 - e / 2))
            sh.draw_line((rect.x1, rect.y0 + e / 2), (rect.x1, rect.y1 - e / 2))
            sh.finish(color=stroke, width=1)
            sh.draw_oval(pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + e))
            sh.finish(color=stroke, fill=fill, width=1)
        elif shape == "hex":
            d = 12
            pts = [(rect.x0 + d, rect.y0), (rect.x1 - d, rect.y0), (rect.x1, (rect.y0 + rect.y1) / 2),
                   (rect.x1 - d, rect.y1), (rect.x0 + d, rect.y1), (rect.x0, (rect.y0 + rect.y1) / 2),
                   (rect.x0 + d, rect.y0)]
            sh.draw_polyline(pts)
            sh.finish(color=stroke, fill=fill, width=1, closePath=True)
        else:
            sh.draw_rect(rect, radius=0.15 if shape == "round" else None)
            sh.finish(color=stroke, fill=fill, width=1)
        sh.commit()
        page.insert_textbox(pymupdf.Rect(rect.x0 + 4, rect.y0 + (6 if sub else 12), rect.x1 - 4, rect.y1), label,
                            fontsize=8.5, fontname="helv", align=1, color=(0.1, 0.12, 0.12))
        if sub:
            page.insert_textbox(pymupdf.Rect(rect.x0 + 4, rect.y0 + 19, rect.x1 - 4, rect.y1), sub, fontsize=6.5,
                                fontname="helv", align=1, color=(0.3, 0.33, 0.33))

    def arrow(a, b, color=(0.45, 0.5, 0.5), dashed=False, label=""):
        (x0, y0), (x1, y1) = a, b
        sh = page.new_shape()
        sh.draw_bezier((x0, y0), ((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1), (x1 - 6, y1))
        sh.finish(color=color, width=0.9, dashes="[3 2] 0" if dashed else None)
        sh.draw_polyline([(x1 - 6, y1 - 3.5), (x1, y1), (x1 - 6, y1 + 3.5)])
        sh.finish(color=color, fill=color, closePath=True)
        sh.commit()
        if label:
            page.insert_text(((x0 + x1) / 2 - 8, (y0 + y1) / 2 - 3), label, fontsize=6.5, color=color)

    for (t, f), y in zip(sources, ys(len(sources))):
        r = pymupdf.Rect(xs[0], y, xs[0] + bw, y + bh)
        box(r, f[:34], LBLUE, BLUE, "cyl")
        pos[("src", t)] = r
    for (t, name), y in zip(tables, ys(len(tables))):
        r = pymupdf.Rect(xs[1], y, xs[1] + bw, y + bh)
        row = next(x for x in src if x["pbi_table"] == t)
        box(r, name, (0.93, 0.96, 1.0), BLUE, "rect", f"key {row['key']}" if row["key"] else "fact / no key")
        pos[("tbl", t)] = r
        if ("src", t) in pos:
            a = pos[("src", t)]
            arrow((a.x1, (a.y0 + a.y1) / 2), (r.x0, (r.y0 + r.y1) / 2))
    for (t, name, m), y in zip(mvs, ys(len(mvs))):
        r = pymupdf.Rect(xs[2], y, xs[2] + bw, y + bh)
        box(r, name, (1.0, 0.92, 0.82), ORANGE, "hex", m["grain"][:40])
        pos[("mv", t)] = r
        for rd in m["reads"]:
            tt = next((x for x, n2 in tables if f"{s['schema']}.{n2}" == rd), None)
            if tt and ("tbl", tt) in pos:
                a = pos[("tbl", tt)]
                arrow((a.x1, (a.y0 + a.y1) / 2), (r.x0, (r.y0 + r.y1) / 2), ORANGE)
    for v, y in zip(views, ys(len(views))):
        r = pymupdf.Rect(xs[3], y, xs[3] + bw, y + bh)
        nf = len([b for b in plan["business_schema"] if b["view"] == v and b["kind"].startswith("Formula")])
        nc = len([b for b in plan["business_schema"] if b["view"] == v and b["kind"] == "Column"])
        box(r, v, (0.94, 0.91, 0.99), PURPLE, "round", f"{nc} columns, {nf} formulas")
        pos[("view", v)] = r
        for b in plan["business_schema"]:
            if b["view"] != v or b["kind"] != "Column":
                continue
            obj = b["source"].rsplit(".", 1)[0]
            key = next((("mv", t) for t, n2, _ in mvs if f"{s['schema']}.{n2}" == obj), None) or \
                next((("tbl", t) for t, n2 in tables if f"{s['schema']}.{n2}" == obj), None)
            if key and key in pos and not pos.get(("edge", key, v)):
                a = pos[key]
                arrow((a.x1, (a.y0 + a.y1) / 2), (r.x0, (r.y0 + r.y1) / 2), PURPLE)
                pos[("edge", key, v)] = True
        if nf and not nc:
            facts = {f"{s['schema']}." + m2["name"] for _, _, m2 in mvs} | {f"{s['schema']}.{n2}" for _, n2 in tables}
            for t, n2, _ in mvs:
                if any(f"{s['schema']}.{n2}" in (b["formula"] or "") for b in plan["business_schema"] if b["view"] == v):
                    a = pos[("mv", t)]
                    arrow((a.x1, (a.y0 + a.y1) / 2), (r.x0, (r.y0 + r.y1) / 2), PURPLE)
            del facts
    for tab, y in zip(tabs, ys(len(tabs))):
        r = pymupdf.Rect(xs[4], y, xs[4] + bw, y + bh)
        ins = [i for i in plan["insights"] if i["tab"] == tab and i["visualization"] not in ("",)]
        box(r, tab, (0.88, 0.96, 0.90), GREEN, "round", f"{len(ins)} items")
        used_views = set()
        for i in ins:
            for part in (i["dimensions"] + ", " + i["measures"]).split(", "):
                bits = part.split(".")
                if len(bits) == 3 and bits[0] == s["business_schema"]:
                    used_views.add(bits[1])
        for v in sorted(used_views):
            if ("view", v) in pos:
                a = pos[("view", v)]
                arrow((a.x1, (a.y0 + a.y1) / 2), (r.x0, (r.y0 + r.y1) / 2), GREEN)
    joins = [j for j in plan["joins"] if j["status"] != NOT_NEEDED]
    legend = "Joins (child -> parent): " + ("; ".join(
        f"{j['id']} {j['child'].split('.')[-1]}({', '.join(j['child_columns'])}) -> "
        f"{j['parent'].split('.')[-1]}({', '.join(j['parent_columns'])})" for j in joins) or "none")
    page.insert_textbox(pymupdf.Rect(30, H - 46, W - 30, H - 18), legend, fontsize=7.5, fontname="helv",
                        color=(0.3, 0.33, 0.33))
    page.insert_text((30, H - 10), "Arrows show the data flow. Generated by PBI Extraction Insights.", fontsize=7,
                     color=(0.4, 0.45, 0.43))
    pix = page.get_pixmap(dpi=150)
    pix.save(str(path))
    doc.close()
    return Path(path)


_BLUE, _LBLUE, _ORANGE, _PURPLE, _GREEN = (0.30, 0.52, 0.80), (0.86, 0.92, 0.98), (0.96, 0.62, 0.25), \
    (0.62, 0.52, 0.80), (0.27, 0.62, 0.42)


def _box(page, rect, label, fill, stroke, sub="", size=8.0, radius=None):
    import pymupdf

    sh = page.new_shape()
    sh.draw_rect(rect, radius=radius)
    sh.finish(color=stroke, fill=fill, width=0.9)
    sh.commit()
    top = rect.y0 + (2 if sub else max(2, (rect.height - size * 1.3) / 2))
    page.insert_textbox(pymupdf.Rect(rect.x0 + 3, top, rect.x1 - 3, rect.y1), label, fontsize=size, fontname="helv",
                        align=1, color=(0.1, 0.12, 0.12))
    if sub:
        page.insert_textbox(pymupdf.Rect(rect.x0 + 3, rect.y0 + size + 4, rect.x1 - 3, rect.y1), sub,
                            fontsize=size - 1.8, fontname="helv", align=1, color=(0.3, 0.33, 0.33))


def _arrow(page, a, b, color=(0.45, 0.5, 0.5), width=0.7):
    (x0, y0), (x1, y1) = a, b
    sh = page.new_shape()
    sh.draw_bezier((x0, y0), ((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1), (x1 - 5, y1))
    sh.finish(color=color, width=width)
    sh.draw_polyline([(x1 - 5, y1 - 3), (x1, y1), (x1 - 5, y1 + 3)])
    sh.finish(color=color, fill=color, closePath=True)
    sh.commit()


def _dag_with_lineage(plan: dict, path) -> Path:
    """Data sources → Power Query lineage (summary) → objects the report reads → business views → tabs."""
    import pymupdf

    s = plan["summary"]
    lin = plan.get("pq_lineage") or []
    src = [x for x in plan["source_map"] if x["schema"] and x["incorta_table"] and "Materialized" not in x["kind"]]
    sources = []
    for x in src:
        label = x["data_source"] if "(SQL)" in x["kind"] else (x["source"] or x["data_source"]).split("\\")[-1]
        if label not in sources:
            sources.append(label)
    report = []
    for b in plan["business_schema"]:
        if b["kind"] == "Column" and b.get("source"):
            obj = b["source"].rsplit(".", 1)[0].split(".")[-1]
            if obj not in report:
                report.append(obj)
    report += [m["name"] for m in plan["mvs"] if m["kind"] != "Power Query MV" and m["name"] not in report]
    views = sorted({b["view"] for b in plan["business_schema"]})
    tabs = s["tabs"]
    n = max(len(sources), len(report), len(views), len(tabs), 6)
    step = 34
    W, H = 1300, 120 + n * step + 60
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    xs, bw, bh = [24, 280, 540, 800, 1060], 220, 26
    pq = [m for m in plan["mvs"] if m["kind"] == "Power Query MV"]
    heads = ["Data sources", f"Power Query lineage ({s['schema']})", "Objects the report reads",
             f"Business schema {s['business_schema']}", f"Dashboard {s['dashboard']}"]
    for x, h in zip(xs, heads):
        page.insert_textbox(pymupdf.Rect(x, 18, x + bw, 50), h, fontsize=10, fontname="helv", align=1,
                            color=(0.12, 0.42, 0.32))

    def ys(count):
        top = 64 + (n - count) * step / 2
        return [top + i * step for i in range(count)]

    pos = {}
    for lab, y in zip(sources, ys(len(sources))):
        r = pymupdf.Rect(xs[0], y, xs[0] + bw, y + bh)
        cnt = len([x for x in src if (x["data_source"] if "(SQL)" in x["kind"] else
                                        (x["source"] or x["data_source"]).split("\\")[-1]) == lab])
        _box(page, r, lab[:44], _LBLUE, _BLUE, f"{cnt} table(s)", 7.5)
        pos[("src", lab)] = r
    levels = max([m.get("depth", 0) for m in pq] or [0]) + 1
    mid = pymupdf.Rect(xs[1], 64, xs[1] + bw, H - 70)
    sh = page.new_shape()
    sh.draw_rect(mid, radius=0.04)
    sh.finish(color=_ORANGE, fill=(1.0, 0.95, 0.88), width=0.9)
    sh.commit()
    page.insert_textbox(pymupdf.Rect(mid.x0 + 8, mid.y0 + 10, mid.x1 - 8, mid.y0 + 90),
                        f"{len(src)} physical tables (sources as-is)\nthen {len(pq)} Power Query MVs in {levels} level(s)",
                        fontsize=9.5, fontname="helv", align=1, color=(0.1, 0.12, 0.12))
    page.insert_textbox(pymupdf.Rect(mid.x0 + 8, mid.y0 + 46, mid.x1 - 8, mid.y0 + 140),
                        "One MV per Power BI query with steps; each reads the objects of the queries it references, "
                        "so the chain is the Power Query chain. Full chain: the _pq_lineage.png diagram and the "
                        "design document (Power Query lineage).", fontsize=7.2, fontname="helv", align=1,
                        color=(0.3, 0.33, 0.33))
    for lab in sources:
        a = pos[("src", lab)]
        _arrow(page, (a.x1, (a.y0 + a.y1) / 2), (mid.x0, (a.y0 + a.y1) / 2), _BLUE)
    for name, y in zip(report, ys(len(report))):
        r = pymupdf.Rect(xs[2], y, xs[2] + bw, y + bh)
        m = next((m for m in plan["mvs"] if m["name"] == name), None)
        chk = (m or {}).get("check", "")
        if m:
            _box(page, r, name[:40], (1.0, 0.92, 0.82), _ORANGE, m["grain"][:46] + (f" | check: {chk}" if chk else ""), 7.5)
        else:
            _box(page, r, name[:40], (0.93, 0.96, 1.0), _BLUE, "physical table", 7.5)
        pos[("obj", name)] = r
        _arrow(page, (mid.x1, (r.y0 + r.y1) / 2), (r.x0, (r.y0 + r.y1) / 2), _ORANGE)
    for v, y in zip(views, ys(len(views))):
        r = pymupdf.Rect(xs[3], y, xs[3] + bw, y + bh)
        nf = len([b for b in plan["business_schema"] if b["view"] == v and b["kind"].startswith("Formula")])
        nc = len([b for b in plan["business_schema"] if b["view"] == v and b["kind"] == "Column"])
        _box(page, r, v[:40], (0.94, 0.91, 0.99), _PURPLE, f"{nc} columns, {nf} formulas", 7.5, radius=0.15)
        pos[("view", v)] = r
        done = set()
        for b in plan["business_schema"]:
            if b["view"] != v or b["kind"] != "Column":
                continue
            obj = b["source"].rsplit(".", 1)[0].split(".")[-1]
            if ("obj", obj) in pos and obj not in done:
                a = pos[("obj", obj)]
                _arrow(page, (a.x1, (a.y0 + a.y1) / 2), (r.x0, (r.y0 + r.y1) / 2), _PURPLE)
                done.add(obj)
    for tab, y in zip(tabs, ys(len(tabs))):
        r = pymupdf.Rect(xs[4], y, xs[4] + bw, y + bh)
        ins = [i for i in plan["insights"] if i["tab"] == tab]
        _box(page, r, tab[:40], (0.88, 0.96, 0.90), _GREEN, f"{len(ins)} items", 7.5, radius=0.15)
        used_views = set()
        for i in ins:
            for part in (i["dimensions"] + ", " + i["measures"]).split(", "):
                bits = part.split(".")
                if len(bits) == 3 and bits[0] == s["business_schema"]:
                    used_views.add(bits[1])
        for v in sorted(used_views):
            if ("view", v) in pos:
                a = pos[("view", v)]
                _arrow(page, (a.x1, (a.y0 + a.y1) / 2), (r.x0, (r.y0 + r.y1) / 2), _GREEN)
    page.insert_text((24, H - 14), "Arrows show the data flow. Generated by PBI Extraction Insights.", fontsize=7,
                     color=(0.4, 0.45, 0.43))
    page.get_pixmap(dpi=150).save(str(path))
    doc.close()
    return Path(path)


def pq_lineage_png(plan: dict, path) -> Path | None:
    """Every Power Query query in the build, by level: physical tables (blue) and MVs (orange), arrows = reads."""
    import pymupdf

    lin = plan.get("pq_lineage") or []
    if not lin:
        return None
    levels: dict[int, list[dict]] = {}
    for x in lin:
        levels.setdefault(x["depth"], []).append(x)
    n = max(len(v) for v in levels.values())
    bw, bh, gap, colw = 200, 20, 6, 250
    W = 40 + colw * len(levels)
    H = 80 + n * (bh + gap) + 40
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    pos = {}
    for d in sorted(levels):
        x0 = 20 + d * colw
        page.insert_textbox(pymupdf.Rect(x0, 16, x0 + bw, 44), "Level 0: source queries" if d == 0 else f"Level {d}",
                            fontsize=10, fontname="helv", align=1, color=(0.12, 0.42, 0.32))
        items = sorted(levels[d], key=lambda x: x["query"])
        top = 56 + (n - len(items)) * (bh + gap) / 2
        for i, x in enumerate(items):
            r = pymupdf.Rect(x0, top + i * (bh + gap), x0 + bw, top + i * (bh + gap) + bh)
            is_mv = bool(x["mv"])
            fill = (1.0, 0.92, 0.82) if is_mv else (0.93, 0.96, 1.0)
            stroke = _ORANGE if is_mv else _BLUE
            label = x["object"] + (" (+table)" if x["mv"] and x["physical"] else "")
            _box(page, r, label[:44], fill, stroke, "", 6.6)
            if x["role"] == "report table":
                sh = page.new_shape()
                sh.draw_rect(r + (-1.5, -1.5, 1.5, 1.5))
                sh.finish(color=(0.12, 0.42, 0.32), width=1.4)
                sh.commit()
            pos[x["query"]] = r
    for x in lin:
        b = pos.get(x["query"])
        for q in x["reads_queries"]:
            a = pos.get(q)
            if a and b:
                _arrow(page, (a.x1, (a.y0 + a.y1) / 2), (b.x0, (b.y0 + b.y1) / 2), (0.6, 0.62, 0.62), 0.5)
    page.insert_text((20, H - 14), "Blue: physical table (source as-is). Orange: MV with the query's steps (+table: the "
                     "query also has its own physical table). Green frame: a table the report reads.", fontsize=7,
                     color=(0.4, 0.45, 0.43))
    page.get_pixmap(dpi=130).save(str(path))
    doc.close()
    return Path(path)


def _pos_of(pos, plan, obj, tables, mvs):
    schema = plan["summary"]["schema"]
    for t, n2, _ in mvs:
        if f"{schema}.{n2}" == obj:
            return pos.get(("mv", t))
    for t, n2 in tables:
        if f"{schema}.{n2}" == obj:
            return pos.get(("tbl", t))
    return None


# ------------------------------------------------------------------------------------------ package
def write_package(plan: dict, data: dict, out_dir, stem: str | None = None) -> dict:
    """Write every file of the package. One object or one document that fails does not stop the others: it is
    replaced by a note (SQL files) or left out, and the reason goes to the warnings shown on the page."""
    out_dir = Path(out_dir)
    s = plan["summary"]
    slug = slug_of(stem or s["report"])
    folder = out_dir / f"{slug}_incorta"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*"):
        if old.is_file():
            old.unlink()
    written, files, warnings = [], {}, []

    def attempt(label, fn, *args):
        try:
            return fn(*args)
        except ImportError as ex:
            warnings.append(f"{label} not written ({ex}): pip install -r requirements.txt")
        except Exception as ex:  # noqa: BLE001 - one artefact must not stop the package
            log.exception("Incorta package: %s failed", label)
            warnings.append(f"{label} not written: {type(ex).__name__}: {ex}")
        return None

    def sql_file(name, obj, make):
        try:
            text = make()
        except Exception as ex:  # noqa: BLE001
            log.exception("Incorta package: SQL for %s failed", obj)
            warnings.append(f"SQL file for {obj} could not be generated ({type(ex).__name__}: {ex}); "
                            f"the file says so and the object needs a manual check")
            text = (f"-- [MANUAL CHECK REQUIRED] The SQL for {obj} could not be generated: "
                    f"{type(ex).__name__}: {ex}\n-- See the build plan workbook for this object.\n")
        (folder / name).write_text(text, encoding="utf-8")
        files[obj] = name
        written.append(name)

    joins = [j for j in plan["joins"] if j["status"] != NOT_NEEDED]
    for x in plan["source_map"]:
        if not x["schema"] or "Materialized" in x["kind"] or not x.get("incorta_table"):
            continue
        fcols = [f for f in plan["formula_columns"] if f["table"] == x["pbi_table"]]
        sql_file(f"{slug}__{x['incorta_table'].lower()}.sql", x["incorta_table"],
                 lambda x=x, fcols=fcols: table_sql(plan, x, joins, fcols))
    for m in plan["mvs"]:
        sql_file(f"{slug}__{m['name'].lower()}.sql", m["name"],
                 lambda m=m: mv_sql(plan, m, joins))
    steps = attempt("build steps", build_steps, plan, files) or []
    doc_md = attempt("design document", doc_markdown, plan, data, files)
    if doc_md is not None:
        (folder / f"{slug}_doc.md").write_text(doc_md, encoding="utf-8")
        written.append(f"{slug}_doc.md")
        if attempt("design PDF", markdown_to_pdf, doc_md, folder / f"{slug}_doc.pdf",
                   f"{s['report']} - Incorta build design"):
            written.append(f"{slug}_doc.pdf")
    if attempt("lineage diagram", dag_png, plan, folder / f"{slug}_dag.png"):
        written.append(f"{slug}_dag.png")
    if attempt("Power Query lineage diagram", pq_lineage_png, plan, folder / f"{slug}_pq_lineage.png"):
        written.append(f"{slug}_pq_lineage.png")
    if attempt("build plan workbook", write_build_plan, plan, folder / f"{slug}_incorta_build_plan.xlsx", files, steps):
        written.append(f"{slug}_incorta_build_plan.xlsx")
    plan_out = dict(plan, files=files, build_steps=steps)
    (folder / f"{slug}_incorta_plan.json").write_text(json.dumps(plan_out, indent=2, default=str), encoding="utf-8")
    written.append(f"{slug}_incorta_plan.json")
    written = list(dict.fromkeys(written))
    zip_path = out_dir / f"{slug}_incorta_outputs.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in written:
            z.write(folder / name, arcname=name)
    return {"zip": zip_path, "folder": folder, "files": written, "sql_files": files, "warnings": warnings,
            "build_steps": steps}
