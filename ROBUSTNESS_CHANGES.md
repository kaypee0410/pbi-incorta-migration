# Robustness changes: apply by hand

What changed and why, file by file. Every edit is **find → replace** or **add**; nothing overwrites a whole file
except the new test file. Do the steps in order, then run the checks at the end.

**Before you start:** steps 8 to 10 (`incorta/plan.py`, `incorta/m2sql.py`, `incorta/package.py`) assume the
Power Query lineage version (the one with `m2sql.py`, `mlang.py` and `verify.py`). If you don't have
`pbi_insights/incorta/m2sql.py`, apply that version first; otherwise skip the m2sql parts.

Commit your current state first (`git add -A && git commit -m "before robustness"`), so `git diff` shows exactly
what you changed.

**The idea in one line:** clean every model and layout once, where it is read (no NaN, every name is text),
make the DAX and Power Query translators unable to raise, and guard every output (sheet, visual, package file)
so one failure becomes a warning instead of a stopped run.

---


## 1. `pbi_insights/extract/model.py` (the root cause)


Power BI stores some column names only as `InferredName` (columns of DAX calculated tables). Reading only `ExplicitName` gave an empty value, which pandas turns into `NaN` (a float), and the workbook's name sort crashed. Four reads use `ExplicitName`; all four now fall back to `InferredName`, and every model goes through one clean-up function.


**1a.** In `from_vertipaq`, the columns query: replace `s.ExplicitName AS SortBy` so the query reads:

```python
    cdf = _q(db, """SELECT c.ID, t.Name AS TableName, c.ExplicitName, c.InferredName, c.Type, c.ExplicitDataType,
                      c.InferredDataType, c.DataCategory, c.IsHidden, c.IsKey, c.SummarizeBy, c.SourceColumn,
                      c.Expression, c.FormatString, COALESCE(s.ExplicitName, s.InferredName) AS SortBy, c.Description,
                      c.DisplayFolder
                    FROM [Column] c JOIN [Table] t ON t.ID = c.TableID
                    LEFT JOIN [Column] s ON s.ID = c.SortByColumnID""")
```

**1b.** The relationships block (replaces the version from my earlier message, including its `"?"`):

```python
    rdf = _q(db, """SELECT r.ID, r.IsActive, r.CrossFilteringBehavior, r.FromCardinality, r.ToCardinality,
                      ft.Name AS FromTable, COALESCE(fc.ExplicitName, fc.InferredName) AS FromColumn, tt.Name AS ToTable,
                      COALESCE(tc.ExplicitName, tc.InferredName) AS ToColumn
                    FROM Relationship r JOIN [Table] ft ON ft.ID = r.FromTableID JOIN [Column] fc ON fc.ID = r.FromColumnID
                    JOIN [Table] tt ON tt.ID = r.ToTableID LEFT JOIN [Column] tc ON tc.ID = r.ToColumnID""")
    for r in rdf.itertuples():
        out["relationships"].append({
            "from_table": r.FromTable, "from_column": _s(r.FromColumn), "to_table": r.ToTable,
            "to_column": _s(r.ToColumn) or ("Date" if is_auto_date(r.ToTable) else None), "from_cardinality": CARD.get(r.FromCardinality, "many"),
            "to_cardinality": CARD.get(r.ToCardinality, "one"),
            "cross_filter": CROSSFILTER.get(r.CrossFilteringBehavior, str(r.CrossFilteringBehavior)),
            "active": bool(r.IsActive), "auto_date": is_auto_date(r.ToTable) or is_auto_date(r.FromTable)})
```

**1c.** The hierarchies query and list: `ColumnName` falls back too, and `ordinal` is no longer forced with `int()` (the clean-up does it safely):

```python
    hdf = _q(db, """SELECT t.Name AS TableName, h.Name AS Hierarchy, l.Name AS Level, l.Ordinal,
                      COALESCE(c.ExplicitName, c.InferredName) AS ColumnName
                    FROM Level l JOIN Hierarchy h ON h.ID = l.HierarchyID JOIN [Table] t ON t.ID = h.TableID
                    JOIN [Column] c ON c.ID = l.ColumnID""")
    if hdf is not None:
        out["hierarchies"] = [{"table": r.TableName, "hierarchy": r.Hierarchy, "level": r.Level,
                               "ordinal": _s(r.Ordinal), "column": r.ColumnName}
                              for r in hdf.itertuples() if not is_auto_date(r.TableName)]
```

**1d.** Replace `extract_model` so both readers go through the clean-up:

```python
def extract_model(pkg: Package) -> dict:
    if pkg.has_model_schema:
        return normalize_model(from_tmsl(pkg.model_schema()))
    if pkg.has_vertipaq_model:
        return normalize_model(from_vertipaq(pkg.path))
    return empty_model("No semantic model in this file: it is probably a thin report connected to a published "
                       "semantic model. Export a .pbit, or open the semantic model's own .pbix, to decode DAX and sources.")
```

**1e.** Add at the end of the file:

```python
# ------------------------------------------------------------------------------------- one shape for every model
NAME_FIELDS = {
    "tables": ("name",),
    "columns": ("table", "column"),
    "measures": ("table", "name"),
    "kpis": ("table", "measure"),
    "relationships": ("from_table", "from_column", "to_table", "to_column"),
    "hierarchies": ("table", "hierarchy", "level", "column"),
    "roles": ("role", "table"),
    "expressions": ("name",),
    "data_sources": ("name",),
}
INT_FIELDS = {"tables": ("row_count",), "measures": ("id",), "hierarchies": ("ordinal",)}
TEXT_FIELDS = {"tables": ("description", "m_expression", "calc_dax"),
               "columns": ("data_type", "data_category", "summarize_by", "source_column", "dax", "format_string",
                           "sort_by", "description", "display_folder", "column_type"),
               "measures": ("dax", "format_string", "display_folder", "description"),
               "expressions": ("kind", "expression"), "roles": ("filter",), "data_sources": ("connection",),
               "relationships": ("from_cardinality", "to_cardinality", "cross_filter")}


def _clean(v):
    """pandas / numpy values -> plain Python: NaN, NA and NaT -> None; numpy scalars -> int / float / bool."""
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, dict):
        return {k: (x if k == "_handle" else _clean(x)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    try:
        if v != v:                                   # NaN / NaT
            return None
    except Exception:  # noqa: BLE001 - pandas NA raises on comparison
        return None
    if type(v).__module__ == "numpy":
        try:
            return v.item()
        except Exception:  # noqa: BLE001
            return v
    return v


def _name(v) -> str | None:
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)                                   # a table or column named 2024 read back as 2024.0
    v = str(v)
    return v if v.strip() else None


def normalize_model(out: dict) -> dict:
    """Every model, whatever reader produced it, leaves here with one shape: names are non-empty str, optional
    text is str or None, counts are int or None, no NaN anywhere. Entries without a name are dropped with a
    warning instead of reaching the planner or the workbook as NaN or None (which break sorting and joins)."""
    handle = out.pop("_handle", None)
    out = _clean(out)
    warnings = out.setdefault("warnings", [])
    for key, fields in NAME_FIELDS.items():
        kept = []
        for i, item in enumerate(out.get(key) or []):
            if not isinstance(item, dict):
                continue
            missing = []
            for f in fields:
                if f in item:
                    item[f] = _name(item[f])
                    if item[f] is None:
                        missing.append(f)
            if missing and key == "hierarchies" and missing == ["level"]:
                item["level"] = item.get("column") or f"Level {i + 1}"
                missing = []
            if missing:
                label = ", ".join(f"{f}={item.get(f)!r}" for f in fields if f in item)
                warnings.append(f"Skipped a {key[:-1] if key.endswith('s') else key} without a "
                                f"{'/'.join(missing)} ({label}): the file stores no name for it.")
                continue
            for f in TEXT_FIELDS.get(key, ()):
                if f in item and item[f] is not None and not isinstance(item[f], str):
                    item[f] = str(item[f])
            for f in INT_FIELDS.get(key, ()):
                if f in item and item[f] is not None:
                    try:
                        item[f] = int(item[f])
                    except (TypeError, ValueError):
                        item[f] = None
            kept.append(item)
        out[key] = kept
    # measures keep unique ids (the workbook sorts by them)
    seen = set()
    for i, m in enumerate(out.get("measures") or [], 1):
        if m.get("id") is None or m["id"] in seen:
            m["id"] = 10 ** 6 + i
        seen.add(m["id"])
    if handle is not None:
        out["_handle"] = handle
    return out
```


## 2. `pbi_insights/extract/layout.py`


Same idea for the report layout: a page without an `ordinal` stopped the Tabs sheet (`None + 1`), and a position stored as text would stop `round()`.


**2a.** Add this function just above `def _src_table(`:

```python
def _num(v) -> int:
    """A position or size from the layout JSON as a whole number (missing or odd values -> 0)."""
    try:
        f = float(v)
        return round(f) if f == f else 0
    except (TypeError, ValueError):
        return 0
```

**2b.** In `parse_legacy`, the sections loop:

```python
    for s in sorted(lay.get("sections", []), key=lambda x: x.get("ordinal") if isinstance(x.get("ordinal"), (int, float)) else 0):
```

**2c.** Every `round(pos.get(...) or 0)` becomes `_num(pos.get(...) or 0)`: one line in `parse_legacy` (the `containers.append(...)`) and two lines each in `parse_legacy` and `parse_pbir` (the `"x"/"y"` and `"w"/"h"` lines). After the change they read:

```python
            containers.append((_num(pos.get("y", vc.get("y", 0)) or 0), _num(pos.get("x", vc.get("x", 0)) or 0), vc, c, pos))
"x": _num(pos.get("x", 0) or 0), "y": _num(pos.get("y", 0) or 0),
"w": _num(pos.get("width", 0) or 0), "h": _num(pos.get("height", 0) or 0),
```

Tip: in VS Code, Find `round(pos.get` (5 hits) and replace `round(` with `_num(` on those lines only.


**2d.** Replace `parse_report` and add `normalize_report` after it:

```python
def parse_report(pkg: Package) -> dict:
    if pkg.has_legacy_layout:
        rep = parse_legacy(pkg.read_json("Report/Layout"))
    elif pkg.has_pbir:
        rep = parse_pbir(pkg)
    else:
        return {"format": None, "pages": [], "visuals": [], "report_filters": [],
                "warnings": ["No report pages found: this file holds a model only."]}
    normalize_report(rep)
    prefixes = page_prefixes(rep["pages"])
    for p in rep["pages"]:
        p["prefix"] = prefixes[p["name"]]
    for v in rep["visuals"]:
        v["id"] = f"{prefixes[v['page']]}-{v['seq']:02d}"
    return rep


def normalize_report(rep: dict) -> dict:
    """One shape for every layout: pages have a unique text name and a whole-number ordinal; visual fields have
    text table / field names or None (a field without a name is kept as 'Other')."""
    renamed = {}
    seen = set()
    for i, p in enumerate(rep.get("pages") or []):
        if not isinstance(p.get("ordinal"), int):
            try:
                p["ordinal"] = int(p.get("ordinal"))
            except (TypeError, ValueError):
                p["ordinal"] = i
        old = p.get("name")
        name = str(old).strip() if old is not None and str(old).strip() else f"Page {i + 1}"
        base, n = name, 2
        while name in seen:
            name, n = f"{base} ({n})", n + 1
        seen.add(name)
        renamed[old] = name
        p["name"] = name
    for j, v in enumerate(rep.get("visuals") or []):
        if v.get("page") in renamed:
            v["page"] = renamed[v["page"]]
        if not isinstance(v.get("page_ordinal"), int):
            v["page_ordinal"] = next((p["ordinal"] for p in rep["pages"] if p["name"] == v.get("page")), 0)
        if not isinstance(v.get("seq"), int):
            v["seq"] = j + 1
        for f in v.get("fields") or []:
            for k in ("table", "field"):
                if f.get(k) is not None and not isinstance(f[k], str):
                    f[k] = str(f[k])
    return rep
```


## 3. `pbi_insights/report/workbook.py`


**3a.** Imports: add `import logging` after `from __future__ import annotations`, and after the constants (just above `def _clean`) add:

```python
log = logging.getLogger(__name__)
```

**3b.** Replace `_clean` (any value becomes something Excel can store: NaN, NaT, numpy numbers, dicts, time zones):

```python
def _clean(x):
    """Any value -> something Excel stores: text, a number, a date or blank (never raises)."""
    import datetime as _dt
    import math

    if x is None:
        return ""
    try:
        if x != x:                                   # NaN, NaT
            return ""
    except Exception:  # noqa: BLE001 - pandas NA cannot be compared
        return ""
    if type(x).__module__ == "numpy":
        try:
            x = x.item()
        except Exception:  # noqa: BLE001
            x = str(x)
    if isinstance(x, (list, tuple, set)):
        x = "\n".join("" if i is None else str(i) for i in x)
    elif isinstance(x, dict):
        import json as _json
        x = _json.dumps(x, ensure_ascii=False, default=str)
    if isinstance(x, bool):
        return "Yes" if x else ""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return "" if math.isnan(x) else str(x)
    if isinstance(x, _dt.datetime) and x.tzinfo is not None:
        x = x.replace(tzinfo=None)
    if not isinstance(x, (str, int, float, _dt.date, _dt.time)):
        try:
            import pandas as _pd
            if _pd.isna(x):
                return ""
        except Exception:  # noqa: BLE001
            pass
        x = str(x)
    if isinstance(x, str):
        x = ILLEGAL_CHARACTERS_RE.sub("", x)
        if len(x) > MAX_CELL:
            x = x[:MAX_CELL] + "\n... (truncated: see the JSON export)"
    return x
```

**3c.** In `_sources_sheet`, the key sort gets `key=str` (from my earlier message):

```python
        keys = sorted({r["to_column"] for r in x.model.get("relationships", []) if r["to_table"] == t["name"]} |
                      {r["from_column"] for r in x.model.get("relationships", []) if r["from_table"] == t["name"]},
                      key=str)
```

**3d.** Replace `write_workbook` (each sheet is guarded; a failure becomes a "Not written" sheet and a warning on the page):

```python
def write_workbook(data: dict, path: str | Path) -> Path:
    """Write every sheet. A sheet that fails is dropped and named in a 'Not written' sheet and in the page's
    warnings; the other sheets are still written."""
    x = _Ctx(data)
    wb = Workbook()
    wb.remove(wb.active)
    failed = []

    def guard(label, fn, *args):
        before = set(wb.sheetnames)
        try:
            fn(*args)
        except Exception as ex:  # noqa: BLE001 - one sheet must not stop the workbook
            log.exception("workbook: %s failed", label)
            for name in set(wb.sheetnames) - before:
                del wb[name]
            failed.append((label, f"{type(ex).__name__}: {ex}"))

    builders = [("Plan", _plan_sheet), ("Tabs", _tabs_sheet), ("Insights", _insights_sheet),
                ("Lineage", _lineage_sheet), ("Sources", _sources_sheet), ("Columns", _columns_sheet),
                ("Relationships", _relationships_sheet), ("Measures", _measures_sheet),
                ("Build plan", _build_plan_sheet), ("Views", _views_sheet), ("Insight SQL", _insight_sql_sheet)]
    if data.get("checks"):
        builders.append(("Data profile", _profile_sheet))
    if data.get("ai"):
        builders.append(("AI notes", _ai_sheet))
    for label, fn in builders:
        guard(label, fn, wb, x)
    if data.get("incorta"):
        from ..incorta.sheets import add_incorta_sheets
        guard("Incorta sheets", add_incorta_sheets, wb, data["incorta"], data["incorta"].get("files"))
    if failed:
        ws = wb.create_sheet("Not written")
        _set(ws, 1, 1, "These sheets could not be written; the rest of the workbook is complete.", font=Font(bold=True))
        _set(ws, 2, 1, "Sheet", font=Font(bold=True))
        _set(ws, 2, 2, "Error (send this to the tool's maintainer)", font=Font(bold=True))
        for i, (label, err) in enumerate(failed, 3):
            _set(ws, i, 1, label)
            _set(ws, i, 2, err)
        ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 24, 120
        data["summary"].setdefault("warnings", []).append(
            "Workbook sheets not written: " + "; ".join(f"{a} ({b})" for a, b in failed))
    if not wb.sheetnames:
        wb.create_sheet("Empty")
    wb.properties.title = f"{data['summary']['file']} migration inventory"
    wb.properties.creator = "PBI Extraction Insights"
    wb.save(path)
    return Path(path)
```


## 4. `pbi_insights/analyze/dax_translate.py`


Broken DAX in a calculated column that another formula uses escaped as an exception; any unexpected DAX shape could too. Both now become review items.


**4a.** Add `import logging` with the imports and `log = logging.getLogger(__name__)` above the first class.


**4b.** In `column_ref`, there are two `try: return self.tr(self._parse(self.idx.columns[(t, c)]["dax"]), ...)` blocks. Add the same `except` to both, between the `return` line and `finally:`:

```python
                try:
                    return self.tr(self._parse(self.idx.columns[(t, c)]["dax"]), replace(ctx, host=t))
                except DaxSyntaxError as ex:
                    return self.review(f"calculated column {t}[{c}] could not be parsed: {ex}")
                finally:
                    self._stack.pop()
```

**4c.** In `_run`, after `except RecursionError:` add the generic catch:

```python
        try:
            res.ir = self.tr(ast, ctx)
        except RecursionError:
            res.ir = Review("expression too deeply nested")
        except Exception as ex:  # noqa: BLE001 - an unexpected DAX shape is a review item, never a crash
            log.exception("DAX translation failed")
            res.ir = Review(f"translator could not handle this DAX ({type(ex).__name__}: {ex})")
```


## 5. `pbi_insights/analyze/querygen.py`


A measure whose DAX did not parse has no expression (`None`) and the SQL renderer stopped on it. At the top of `QueryBuilder.build`:

```python
        plan = QueryPlan()
        self._name_outputs(dims, measures)
        for m in measures:
            if m.ir is None:                              # DAX that did not parse: render NULL, flag for review
                m.ir = Review(f"measure [{m.name}] could not be translated")
```


## 6. `pbi_insights/analyze/planner.py`


**6a.** Imports: add `import logging`, add `QueryPlan` to the `from .querygen import ...` line, and `log = logging.getLogger(__name__)` above `class Planner`.


**6b.** In `plan_visual`, replace `plan = self.qb.build(dims, measures, filters, sorts)` with:

```python
        try:
            plan = self.qb.build(dims, measures, filters, sorts)
        except Exception as ex:  # noqa: BLE001 - one visual must not stop the report
            log.exception("reference SQL for %s failed", v.get("id"))
            plan = QueryPlan(reviews=[f"reference SQL could not be generated ({type(ex).__name__}: {ex})"])
```

**6c.** Replace `plan_report` (one visual that fails is planned without its fields and marked Needs review):

```python
    def plan_report(self) -> list[dict]:
        pages = {p["name"]: p for p in self.report.get("pages", [])}
        slicer_by_page = {}
        for v in self.report.get("visuals", []):
            if v.get("is_slicer") and v.get("slicer_selection"):
                slicer_by_page.setdefault(v["page"], []).append(v["slicer_selection"])
        plans = []
        for v in self.report.get("visuals", []):
            args = (pages.get(v["page"], {}).get("filters", []), self.report.get("report_filters", []),
                    [] if v.get("is_slicer") else slicer_by_page.get(v["page"], []))
            try:
                plans.append(self.plan_visual(v, *args))
            except Exception as ex:  # noqa: BLE001 - plan the visual without its fields rather than stop
                log.exception("planning %s failed", v.get("id"))
                bare = dict(v, fields=[], visual_filters=[], sort_spec=[])
                p = self.plan_visual(bare, [], [], [])
                p["reviews"] = list(p.get("reviews") or []) + [
                    f"this visual could not be planned ({type(ex).__name__}: {ex}): rebuild it by hand"]
                p["approach"], p["complexity"] = "Needs review", "High"
                plans.append(p)
        return plans
```


## 7. `pbi_insights/pipeline.py`


If the Incorta package fails, the workbook is still written. Make sure `import logging` and `log = logging.getLogger(__name__)` are at the top, then wrap the `if options.incorta:` block: indent its body one level inside `try:` and add the `except`. The block becomes:

```python
    if options.incorta:
        try:
            from .incorta.package import write_package
            from .incorta.plan import build_plan

            say(7, "Planning the Incorta build (rules from the build spec)")
            plan = build_plan(data, schema=options.incorta_schema or None, report_name=stem,
                              check_data=options.incorta_check)
            if options.use_ai:
                from .incorta.ai import enrich_plan

                say(7, "Asking Claude to review the Incorta build plan")
                ai = enrich_plan(plan, progress=lambda msg: say(7, msg))
                for e in ai.get("errors") or []:
                    data["summary"].setdefault("warnings", []).append(f"Incorta AI: {e}")
            say(8, "Writing the Incorta build package")
            pkg = write_package(plan, data, out, pbix_path=path, stem=stem)
            plan["files"], plan["build_steps"] = pkg["sql_files"], pkg["build_steps"]
            data["incorta"] = plan
            s = plan["summary"]
            data["summary"]["incorta"] = {"package": pkg["zip"].name, "schema": s["schema"], "mvs": s["mvs"],
                                          "joins": s["joins"], "formula_columns": s["formula_columns"],
                                          "measures_needing_design": s["measures_needing_design"],
                                          "ai_used": bool((plan.get("ai") or {}).get("used")),
                                          "power_query_mvs": s.get("power_query_mvs", 0),
                                          "checks": dict(collections.Counter(c["status"] for c in plan.get("pq_checks") or []))}
            data["summary"].setdefault("warnings", []).extend(pkg["warnings"])
        except Exception as ex:  # noqa: BLE001 - the workbook is still written
            log.exception("Incorta package failed")
            pkg = None
            data.pop("incorta", None)
            data["summary"].pop("incorta", None)
            data["summary"].setdefault("warnings", []).append(
                f"Incorta package not built ({type(ex).__name__}: {ex}). The workbook is complete; "
                f"send this message to the tool's maintainer.")
```


## 8. `pbi_insights/incorta/plan.py`


**8a.** Names: `ident("2024")` gave `_2024` and a Chinese name gave `_` (several tables could collide). Add `import hashlib` to the imports and replace `ident` (add `is_placeholder` after it):

```python
def ident(name: str) -> str:
    """Incorta-safe object / column name: letters, digits and underscores ("GP %" → GP_pct)."""
    s = str(name).replace("%", " pct ").replace("#", " num ").replace("$", " usd ").replace("&", " and ") \
        .replace("/", " per ").replace("+", " plus ")
    s = re.sub(r"[^0-9A-Za-z_]+", "_", s).strip("_")
    s = re.sub(r"_+", "_", s)
    if not s:                     # nothing Latin left (e.g. a Chinese name): stable placeholder, see is_placeholder
        return "X_" + hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:6]
    return s if s[0].isalpha() else f"N_{s}"


def is_placeholder(name: str) -> bool:
    """ident() of a name with no Latin letters or digits: callers may find a better name (file, renamed column)."""
    return bool(re.fullmatch(r"X_[0-9a-f]{6}", str(name)))
```

**8b.** In `IncortaNames.column`, delete the two lines that set `base = "COL"` (the placeholder name is stable and unique), so it reads:

```python
        key = (table, col)
        if key not in self.colmap:
            base, n = ident(col), 2
```

**8c.** `build_plan`: the layer checks can never stop the plan:

```python
def build_plan(data: dict, schema: str | None = None, business_schema: str | None = None,
               report_name: str | None = None, check_data: bool = False) -> dict:
    planner = IncortaPlanner(data, schema, business_schema, report_name)
    plan = planner.build()
    if check_data:
        try:
            planner.check_power_query(plan)
        except Exception as ex:  # noqa: BLE001 - the checks are a bonus, never a blocker
            import logging
            logging.getLogger(__name__).exception("layer checks failed")
            plan["pq_checks_note"] = f"not run: {type(ex).__name__}: {ex}"
    return plan
```

**8d.** In `_tables`, the branch for plain (light) tables uses the same name as the Power Query graph. Replace the two `ident(name)` lines just before `self._power_query(name, row)`:

```python
                tname = self.pq.raw.get(name, ident(name)) if hasattr(self, "pq") else ident(name)
                self.names.objects[name] = tname
                self.names.raw[name] = tname
                self._power_query(name, row)
```

**8e.** In `_pq_row`, just before `if not r.passthrough:` add:

```python
        if r.unsupported:
            row["status"] = NEEDS_DESIGN
```


## 9. `pbi_insights/incorta/m2sql.py`


**9a.** Next to `_ident` (near `class PowerQueryGraph`) add `_placeholder`:

```python
def _ident(name: str) -> str:
    from .plan import ident
    return ident(name)


def _placeholder(name: str) -> bool:
    from .plan import is_placeholder
    return is_placeholder(name)
```

**9b.** In `QueryTranslator.sn`, delete the two lines `if not re.search(r"[A-Za-z]", base): base = "COL"` so the `else` branch is just `base = _ident(m)`.


**9c.** In `_run`, after `except RecursionError:` add:

```python
        except Exception as ex:  # noqa: BLE001 - an M shape the translator does not expect: flag, never crash
            self.res.unsupported.append((self.step, f"not translated ({type(ex).__name__}: {ex})"))
```

**9d.** In `ev`, for identifiers: an unknown name (a missing or renamed query) is now a clear message instead of a confusing one later. Just before `return FuncRef(name)` in the `if k == "id":` branch add:

```python
            if "." not in name and not name.startswith("#"):
                raise Unsupported(f"'{name}' is not a step or query in this file (missing or renamed query)")
```

**9e.** Replace `_readable_source_name`:

```python
def _readable_source_name(code: str) -> str:
    """A Latin name for a query from its source: the file name, else the table in the native query."""
    m = re.search(r'File\.Contents\("([^"]+)"\)', code or "") or re.search(r'Folder\.Files\("([^"]+)"\)', code or "")
    if m:
        leaf = re.split(r"[\\/]", m.group(1).rstrip("\\/"))[-1]
        leaf = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", leaf)
        name = _ident(leaf)
        if not _placeholder(name):
            return name
    m = re.search(r"\bFROM\s+([\w.\[\]]+)", code or "", re.I)
    if m:
        name = _ident(m.group(1).split(".")[-1].strip("[]"))
        if not _placeholder(name) and len(name) > 2:
            return name
    return ""
```

**9f.** In `PowerQueryGraph.__init__`, the naming loop: `if not re.search(r"[A-Za-z]", first):` becomes `if _placeholder(first):`.


**9g.** In `raw_column`, delete the two lines `if not re.search(r"[A-Za-z]", base): base = "COL"`.


**9h.** Replace `translate_all` (both translations guarded, `_placeholder` used):

```python
    def translate_all(self, only=None) -> dict:
        ctx = self.context()
        for q in self.order():
            if only is not None and q not in only:
                continue
            try:
                r = QueryTranslator(q, ctx).translate()
            except Exception as ex:  # noqa: BLE001
                r = QueryResult(q, unsupported=[("(whole query)", f"not translated ({type(ex).__name__}: {ex})")])
            bad = [c for c in r.raw_columns if _placeholder(_ident(c)) and (q, c) not in self._raw_override]
            if bad:                                # non-Latin source columns: name them after the column they become
                back = {}
                for out, rc in r.lineage.items():
                    back.setdefault(rc, out)
                for c in bad:
                    alt = _ident(back[c]) if c in back else ""
                    self._raw_override[(q, c)] = alt if alt and not _placeholder(alt) else _ident(c)
                for k in [k for k in self._raw_cols if k[0] == q]:
                    del self._raw_cols[k]
                try:
                    r = QueryTranslator(q, ctx).translate()
                except Exception as ex:  # noqa: BLE001
                    r = QueryResult(q, unsupported=[("(whole query)", f"not translated ({type(ex).__name__}: {ex})")])
            self.results[q] = r
            self.objects[q] = self.force_object.get(q) or (self.raw[q] if r.passthrough else self.raw[q] + "_MV")
        return self.results
```


## 10. `pbi_insights/incorta/package.py`


PyMuPDF can loop forever on some page breaks (the fuzz test hit it). The PDF now has a page cap and a simple fallback layout; text with `<` (e.g. `<Blank>` in DAX) is escaped; every file of the package is guarded.


**10a.** Add `import logging` to the imports and `log = logging.getLogger(__name__)` after them.


**10b.** In `_md_table`, replace the inner `cell` function:

```python
    def cell(x):
        t = str(x if x is not None else "")
        if len(t) > 700:                       # a table row must fit on a page: the workbook has the full text
            t = t[:700] + " ... (full text in the build plan workbook)"
        return t.replace("|", "\\|").replace("<", "&lt;").replace("\n", "<br>")
```

**10c.** Replace `markdown_to_pdf` with these four functions:

```python
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
```

**10d.** Replace `write_package`:

```python
def write_package(plan: dict, data: dict, out_dir, pbix_path=None, stem: str | None = None) -> dict:
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
                 lambda m=m: mv_sql(plan, m, joins, ai_marked=m.get("status") == "AI suggestion"))
    for d in (plan.get("ai") or {}).get("drafts") or []:
        if d.get("mv_sql") and d.get("mv_name"):
            mvn = ident(d["mv_name"])
            head = _header(f"Incorta Build Mappings for {mvn} (AI draft)", [
                f"1. Materialized view - {s['schema']}.{mvn}", f"- For: {d.get('object')}",
                f"- Approach: {d.get('approach', '')}", "- Status: AI suggestion (not approved)"],
                {"Notes": list(d.get("notes") or [])})
            body = "-- START OF AI GENERATED CODE\n" + d["mv_sql"].rstrip().rstrip(";") + ";\n-- END OF AI GENERATED CODE\n"
            sql_file(f"{slug}__{mvn.lower()}_ai_draft.sql", mvn, lambda head=head, body=body: head + body)
    spec_hash = hashlib.sha256(SPEC_FILE.read_bytes()).hexdigest() if SPEC_FILE.exists() else "-"
    steps = attempt("build steps", build_steps, plan, files) or []
    doc_md = attempt("design document", doc_markdown, plan, data, files)
    if doc_md is not None:
        (folder / f"{slug}_doc.md").write_text(doc_md, encoding="utf-8")
        written.append(f"{slug}_doc.md")
        if attempt("design PDF", markdown_to_pdf, doc_md, folder / f"{slug}_doc.pdf",
                   f"{s['report']} - Incorta build design"):
            written.append(f"{slug}_doc.pdf")
    hist_md = attempt("AI history", ai_history_markdown, plan, data, pbix_path, spec_hash)
    if hist_md is not None:
        (folder / f"{slug}_ai_history.md").write_text(hist_md, encoding="utf-8")
        written.append(f"{slug}_ai_history.md")
        if attempt("AI history PDF", markdown_to_pdf, hist_md, folder / f"{slug}_ai_history.pdf",
                   f"{s['report']} - AI audit trail"):
            written.append(f"{slug}_ai_history.pdf")
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
```


## 11. Tests and notes


- Add `tests/test_robustness.py` (attached as its own file): odd but legal names, missing names, broken DAX and M, and 12 randomized mutations of the synthetic report, each through the whole pipeline.
- `CLAUDE.md`, under `## Conventions`, add as the first bullet:

```markdown
- Robustness (any legal .pbix must produce a workbook): every model leaves the readers through
  `extract.model.normalize_model` and every layout through `extract.layout.normalize_report` (names are
  non-empty str, no pandas NaN / numpy values, unnamed items dropped with a warning). The DAX and M
  translators never raise: unexpected shapes become `Review` / `Unsupported`. Each workbook sheet, each
  visual plan and each package file is guarded: a failure becomes a warning on the page ("Not written" sheet,
  a SQL file that says so), never a stopped run. When a guard fires, fix the root cause and add the case to
  `tests/test_robustness.py` (odd names, missing names, broken DAX/M, randomized mutations).
```


## Check your work

```powershell
python -m pytest -q
```

Expect all tests to pass (57 with the new file; 1 skipped). Then re-run APAC Forecast Report from the page.
If something still goes wrong, the run now finishes and the page lists what was skipped; send me that text.

