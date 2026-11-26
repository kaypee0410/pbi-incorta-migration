"""The Incorta build-plan sheets (spec: Outputs). Written into the migration workbook and into the
stand-alone build plan of the Incorta package."""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Font

from ..report.workbook import WRAP, _set, _sheet

SHEETS = ["Incorta Source Map", "Power Query Lineage", "Incorta Joins", "Incorta Formula Columns", "Incorta MVs",
          "Incorta Business Schema", "Incorta Measures", "Incorta Insights", "Decision Log", "Manual Checks"]


def _notes(x) -> str:
    return "\n".join(x) if isinstance(x, (list, tuple)) else (x or "")


def add_incorta_sheets(wb, plan: dict, files: dict | None = None) -> None:
    files = files or {}
    _sheet(wb, "Incorta Source Map",
           ["Power BI table", "Schema", "Incorta table", "Insights read", "Kind", "Data source", "Source (file / object)",
            "Dataset SQL", "Load filter", "Power Query filters", "Key", "Columns to load", "Columns not loaded",
            "Load type", "Rows in .pbix", "Status", "Notes"],
           [[s["pbi_table"], s["schema"], s["incorta_table"], s["reads_as"], s["kind"], s["data_source"], s["source"],
             s["dataset_sql"], s["load_filter"], s["power_query_filters"], s["key"], s["columns_to_load"],
             s["columns_not_loaded"], s["load_type"], s["rows"], s["status"], _notes(s["notes"])]
            for s in plan["source_map"]],
           [150, 120, 130, 130, 170, 140, 160, 260, 260, 220, 160, 300, 220, 70, 80, 90, 320],
           wrap=(5, 6, 7, 10, 12, 13, 17), code=(8, 9), nums={15: "#,##0"})
    if plan.get("pq_lineage"):
        _sheet(wb, "Power Query Lineage",
               ["Level", "Power BI query", "Role", "Source", "Physical table", "MV", "Reads (Incorta)", "Read by (queries)",
                "Steps", "Translation", "SQL file", "Check on saved rows", "Rows (MV / Power BI)", "Check detail",
                "File selection", "Notes"],
               [[x["depth"], x["query"], x["role"], x["source"], x["physical"], x["mv"], ", ".join(x["reads"]),
                 ", ".join(x["feeds"]), x["steps"], x["translation"], files.get(x["mv"] or x["physical"], ""),
                 x.get("check", ""), x.get("check_rows", ""), x.get("check_detail", ""), "\n".join(x["file_steps"]),
                 _notes(x["notes"] + x["untranslated"])] for x in plan["pq_lineage"]],
               [45, 200, 90, 220, 170, 190, 260, 220, 50, 100, 230, 100, 110, 420, 300, 360],
               wrap=(4, 7, 8, 14, 15, 16))
    _sheet(wb, "Incorta Joins",
           ["ID", "Child table", "Child columns", "Parent table", "Parent columns", "Join type", "Parent key unique",
            "Types match", "Alias", "Power BI relationship", "Status", "Notes"],
           [[j.get("id"), j.get("child"), ", ".join(j.get("child_columns") or []), j.get("parent"),
             ", ".join(j.get("parent_columns") or []), j.get("type"), j.get("key_unique"), j.get("types_match"),
             j.get("alias"), j.get("pbi_relationship"), j.get("status"), _notes(j.get("notes"))]
            for j in plan["joins"]],
           [40, 190, 170, 190, 170, 140, 220, 140, 120, 300, 90, 380], wrap=(7, 10, 12))
    _sheet(wb, "Incorta Formula Columns",
           ["Power BI table", "Incorta table", "Column", "Home", "Incorta formula", "Power BI DAX", "Purpose",
            "Status", "Notes"],
           [[f["table"], f["incorta_table"], f["incorta_column"], f["home"], f["formula"], f["dax"], f["purpose"],
             f["status"], _notes(f["notes"])] for f in plan["formula_columns"]],
           [130, 140, 160, 170, 420, 380, 260, 90, 320], wrap=(7, 9), code=(5, 6))
    _sheet(wb, "Incorta MVs",
           ["Schema", "MV", "Kind", "Grain", "Language", "Reads", "Key", "Insights served", "Reason", "Rule",
            "SQL file", "SQL", "Status", "Notes"],
           [[plan["summary"]["schema"], m["name"], m["kind"], m["grain"], m["language"], ", ".join(m["reads"]),
             m["key"], ", ".join(m["serves"]), m["reason"], m["rule"], files.get(m["name"], ""), m["sql"],
             m["status"], _notes(list(m.get("notes") or []) + list(m.get("reviews") or []))]
            for m in plan["mvs"]],
           [120, 150, 170, 160, 80, 200, 100, 220, 320, 160, 220, 520, 90, 320], wrap=(6, 8, 9, 14), code=(12,))
    _sheet(wb, "Incorta Business Schema",
           ["Business schema", "View", "Column", "Label", "Kind", "Source", "Formula", "Hidden", "Sort by", "Status"],
           [[b["business_schema"], b["view"], b["column"], b["label"], b["kind"], b["source"], b["formula"],
             b["hidden"], b["sort_by"], b["status"]] for b in plan["business_schema"]],
           [150, 140, 170, 170, 130, 300, 420, 60, 120, 90], code=(7,))
    _sheet(wb, "Incorta Measures",
           ["Measure", "Power BI table", "Business view", "Incorta name", "Home", "Incorta formula", "Expanded formula",
            "Power BI DAX", "Pattern", "Used by", "Used via", "Format", "Status", "Decision", "Notes"],
           [[m["measure"], m["pbi_table"], m["view"], m["incorta_name"], m.get("home"), m["formula"],
             m["formula_expanded"] if m["formula_expanded"] != m["formula"] else "", m["dax"], m["pattern"],
             ", ".join(m["used_by"]), ", ".join(m["used_via"]), m["format"], m["status"], m.get("decision", ""),
             _notes(m["notes"])] for m in plan["measures"]],
           [170, 130, 120, 160, 220, 420, 360, 380, 170, 160, 160, 110, 90, 70, 360],
           wrap=(5, 10, 11, 15), code=(6, 7, 8))
    _sheet(wb, "Incorta Insights",
           ["ID", "Dashboard", "Tab", "Title", "Power BI visual", "Incorta visualization", "Base table", "Dimensions",
            "Measures", "Insight formulas", "Filters", "Sort", "Drill", "Objects used", "Expected rows", "Reconciled",
            "Status", "Notes"],
           [[i["id"], i["dashboard"], i["tab"], i["title"], i["pbi_visual"], i["visualization"], i["base_table"],
             i["dimensions"], i["measures"], "\n".join(f"{f['measure']} = {f['formula']}" for f in i["formulas"]),
             i["filters"], i["sort"], i["drill"], i["objects"], i["expected_rows"], i["reconciled"], i["status"],
             _notes(i["notes"])] for i in plan["insights"]],
           [60, 140, 150, 220, 150, 170, 170, 260, 300, 420, 200, 160, 200, 260, 70, 80, 90, 360],
           wrap=(4, 8, 9, 11, 14, 18), code=(10,), nums={15: "#,##0"})
    _sheet(wb, "Decision Log",
           ["ID", "Round", "Topic", "Decision", "Options", "Recommendation", "Reason", "Owner", "Date", "Status",
            "Source"],
           [[d["id"], d["round"], d["topic"], d["decision"], d["options"], d["recommendation"], d["reason"],
             d["owner"], d["date"], d["status"], d["source"]] for d in plan["decisions"]],
           [40, 50, 170, 300, 380, 120, 420, 90, 80, 90, 90], wrap=(3, 4, 5, 7))
    _sheet(wb, "Manual Checks",
           ["ID", "Object", "Issue", "Impact", "Risk", "Required action"],
           [[c["id"], c["object"], c["issue"], c["impact"], c["risk"], c["action"]] for c in plan["manual_checks"]],
           [50, 240, 420, 300, 60, 380], wrap=(2, 3, 4, 6))


def write_build_plan(plan: dict, path, files: dict | None = None, overview: list | None = None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Overview"
    s = plan["summary"]
    rows = [("Incorta build plan", f"{s['report']}"), ("Generated", plan["generated_at"]), ("Rules", plan["spec"]),
            ("Physical schema", s["schema"]), ("Business schema", s["business_schema"]),
            ("Dashboard", f"{s['dashboard']} ({len(s['tabs'])} tabs: {', '.join(s['tabs'])})"),
            ("Tables loaded", s["tables_loaded"]), ("Materialized views", s["mvs"]), ("Joins", s["joins"]),
            ("Physical formula columns", s["formula_columns"]), ("Business view formulas", s["business_view_formulas"]),
            ("Insight formulas", s["insight_formulas"]), ("Measures in scope", s["measures"]),
            ("Measures needing a decision", s["measures_needing_design"]), ("Insights", s["insights"]),
            ("Dashboard filters", s["dashboard_filters"]), ("Decisions to settle", s["decisions"]),
            ("Manual checks", s["manual_checks"]), ("Complexity", s["complexity"])]
    _set(ws, 1, 1, "Incorta build plan", font=Font(bold=True, size=12))
    for r, (k, v) in enumerate(rows[1:], start=3):
        _set(ws, r, 1, k, font=Font(bold=True))
        _set(ws, r, 2, v, WRAP)
    r = len(rows) + 4
    _set(ws, r, 1, "Build order", font=Font(bold=True, size=12))
    for i, step in enumerate(overview or [], start=1):
        _set(ws, r + i, 1, f"Step {i}", font=Font(bold=True))
        _set(ws, r + i, 2, step, WRAP)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 120
    add_incorta_sheets(wb, plan, files)
    wb.properties.title = f"{s['report']} Incorta build plan"
    wb.properties.creator = "PBI Extraction Insights"
    wb.save(path)
    return path
