"""Rule engine: the analysis of one .pbix → an Incorta build plan, following spec.md in this folder.

Every object of the Power BI model gets one Incorta home: a physical schema table (data file or SQL),
a physical formula column, a materialized view, a join, a business view column or formula, or an insight
formula. Each row carries a status (Proposed / Needs design / Not needed) and its reason, so a developer can
approve the plan row by row.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re

from ..analyze.dax_translate import SQL_GAP_PREFIXES, ModelIndex, Translator
from ..analyze.formulas import ToolFormula, _Unsupported
from ..analyze.ir import Agg, Col, CorrAgg, Fn, Lit, Lookup, Rank, Review, TimeShift, Total, aggs_in, walk
from ..extract.mquery import split_top_level
from . import sparksql

MANUAL = "[MANUAL CHECK REQUIRED]"
PROPOSED, NEEDS_DESIGN, NOT_NEEDED = "Proposed", "Needs design", "Not needed"
FILE_CONNECTORS = {"Csv.Document": "CSV file", "Excel.Workbook": "Excel workbook", "Json.Document": "JSON file",
                   "Xml.Tables": "XML file", "Parquet.Document": "Parquet file", "Folder.Files": "Folder of files"}
TYPE_MAP = {"text": "string", "whole number": "long", "int64": "long", "decimal number": "double",
            "fixed decimal number": "double", "double": "double", "date/time": "timestamp", "datetime": "timestamp",
            "date": "date", "time": "string", "true/false": "boolean", "boolean": "boolean", "binary": "binary"}
M_TYPE = {"type date": "date", "type datetime": "timestamp", "type datetimezone": "timestamp", "type text": "string",
          "type number": "double", "int64.type": "long", "type logical": "boolean", "currency.type": "double",
          "percentage.type": "double", "type time": "string"}
VISUAL_MAP = {
    "card": "KPI", "cardVisual": "KPI", "multiRowCard": "KPI (one per measure)", "kpi": "KPI", "gauge": "Gauge",
    "clusteredBarChart": "Bar chart", "stackedBarChart": "Stacked bar chart",
    "hundredPercentStackedBarChart": "100% stacked bar chart", "clusteredColumnChart": "Column chart",
    "stackedColumnChart": "Stacked column chart", "hundredPercentStackedColumnChart": "100% stacked column chart",
    "lineChart": "Line chart", "areaChart": "Area chart", "stackedAreaChart": "Stacked area chart",
    "lineClusteredColumnComboChart": "Combo chart (column + line)",
    "lineStackedColumnComboChart": "Combo chart (stacked column + line)", "pieChart": "Pie chart",
    "donutChart": "Donut chart", "scatterChart": "Scatter chart", "treemap": "Treemap", "map": "Map",
    "filledMap": "Map (filled)", "shapeMap": "Map (filled)", "tableEx": "Aggregated table", "table": "Aggregated table",
    "pivotTable": "Pivot table", "matrix": "Pivot table", "slicer": "Dashboard filter (prompt)",
    "funnel": "Funnel chart", "waterfallChart": "Waterfall chart", "ribbonChart": "Stacked column chart",
    "textbox": "Text (recreate by hand)", "image": "Image (recreate by hand)", "shape": "Shape (recreate by hand)",
    "basicShape": "Shape (recreate by hand)", "actionButton": "Button (not migrated)",
}
_REF = re.compile(r"^(.*)\[(.*)\]$")


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


def schema_name(stem: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(stem))
    return ident(s).upper()


def parse_ref(text: str):
    m = _REF.match(str(text or "").strip())
    return (m.group(1).strip("'"), m.group(2)) if m else None


def one_line(text, limit: int = 400) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


class IncortaNames:
    """Power BI table / column → Incorta schema object / column names."""

    def __init__(self, schema: str, business_schema: str):
        self.schema = schema
        self.bs = business_schema
        self.objects: dict[str, str] = {}      # PBI table -> Incorta object insights and joins read (table or MV)
        self.raw: dict[str, str] = {}          # PBI table -> loaded Incorta table (file / SQL)
        self.mv_only: dict[str, set] = {}      # PBI table -> columns that exist only in its MV
        self.colmap: dict[tuple, str] = {}
        self.views: dict[str, str] = {}        # PBI table or display folder -> business view

    def column(self, table: str, col: str) -> str:
        key = (table, col)
        if key not in self.colmap:
            base, n = ident(col), 2
            taken = {v.lower() for (t, _), v in self.colmap.items() if t == table}
            name = base
            while name.lower() in taken:
                name, n = f"{base}_{n}", n + 1
            self.colmap[key] = name
        return self.colmap[key]

    def object(self, table: str) -> str:
        return f"{self.schema}.{self.objects.get(table, ident(table))}"

    def object_raw(self, table: str) -> str:
        return f"{self.schema}.{self.raw.get(table, self.objects.get(table, ident(table)))}"

    def object_for(self, table: str, cols) -> str:
        return self.object(table) if set(cols) & self.mv_only.get(table, set()) else self.object_raw(table)

    def full(self, table: str, col: str) -> str:
        return f"{self.object(table)}.{self.column(table, col)}"

    def bv(self, view: str, label: str) -> str:
        return f"{self.bs}.{ident(view)}.{ident(label)}"


class IncortaPlanner:
    def __init__(self, data: dict, schema: str | None = None, business_schema: str | None = None,
                 report_name: str | None = None):
        self.data = data
        self.model = data["model"]
        self.report = data["report"]
        self.sources = data.get("sources") or {}
        self.idx = ModelIndex(self.model)
        self.translator = Translator(self.idx, data.get("graph"))
        stem = report_name or re.sub(r"\.(pbix|pbit)$", "", str(data["summary"].get("file") or "Report"), flags=re.I)
        self.report_name = stem
        self.names = IncortaNames(schema or schema_name(stem), business_schema or ident(stem))
        self.measures = [m for m in data.get("measures", []) if m.get("scope") != "Not used"]
        self.mindex = {m["name"]: m for m in data.get("measures", [])}
        plans = [_plain(p) for p in data.get("plans", [])]
        self.plans = [p for p in plans if p.get("approach") != "Static"]
        self.static = [p for p in plans if p.get("approach") == "Static"]
        self.tables = {t["name"]: t for t in self.model.get("tables", []) if not t.get("auto_date")}
        self.cols = {(c["table"], c["column"]): c for c in self.model.get("columns", []) if not c.get("auto_date")}
        self.used: dict[tuple, set] = {}
        self.decisions: list[dict] = []
        self.checks: list[dict] = []
        self.today = dt.date.today().isoformat()

    # ------------------------------------------------------------------------------------------ helpers
    def is_calc(self, t: str, c: str) -> bool:
        return bool((self.cols.get((t, c)) or {}).get("dax"))

    def ctype(self, t: str, c: str) -> str:
        col = self.cols.get((t, c)) or {}
        src = self.sources.get(t, {}).get("m") or {}
        for name, mt in src.get("types") or []:
            if str(name).lower() in (c.lower(), str(col.get("source_column") or "").lower()):
                return M_TYPE.get(str(mt).lower().strip(), TYPE_MAP.get(str(col.get("data_type", "")).lower(), "string"))
        return TYPE_MAP.get(str(col.get("data_type", "")).lower(), "string")

    def add_used(self, t: str, c: str, why: str):
        if (t, c) in self.cols:
            self.used.setdefault((t, c), set()).add(why)

    def decision(self, topic, decision, options, recommendation, reason, source="Rule"):
        d = {"id": f"D{len(self.decisions) + 1}", "round": 1, "topic": topic, "decision": decision,
             "options": options, "recommendation": recommendation, "reason": reason, "owner": "",
             "date": self.today, "status": PROPOSED, "source": source}
        self.decisions.append(d)
        return d["id"]

    def check(self, obj, issue, impact, action, risk="Medium"):
        c = {"id": f"MC{len(self.checks) + 1}", "object": obj, "issue": issue, "impact": impact, "risk": risk,
             "action": action}
        self.checks.append(c)
        return c["id"]

    def formula(self, ir, level_dims=None, rank_dims=None, inline=False):
        f = ToolFormula("incorta", namer=self.names.full, key_of=self.key_of, level_dims=level_dims,
                        rank_dims=rank_dims, typer=self.ctype)
        try:
            return f.r(ir), f.used
        except _Unsupported:
            return None, f.used

    def key_of(self, table: str) -> str | None:
        t = self.table_rows.get(table) if hasattr(self, "table_rows") else None
        if t and t.get("key"):
            return self.names.full(table, t["key_cols"][0])
        return None

    # ------------------------------------------------------------------------------------------ build
    def build(self) -> dict:
        self._used_by_report()
        self._calc_closure()
        self.joins = self._joins()
        self._calc_closure()
        self._power_query_graph()
        self.table_rows = self._tables()
        self.formula_cols, self.mvs = self._calc_columns()
        self.mvs = self._pq_mvs() + self.mvs
        self._link_joins()
        self.measure_rows = self._measures()
        self._insight_mvs()
        self.insight_rows, self.filters = self._insights()
        self.bs_rows = self._business_schema()
        self._model_decisions()
        return self._result()

    # ---- step 1: what the report uses -------------------------------------------------------------------
    def _used_by_report(self):
        for p in self.plans:
            for d in p.get("dims") or []:
                self.add_used(d["table"], d["column"], p["id"])
                if d.get("sort_by"):
                    self.add_used(d["table"], d["sort_by"], f"{p['id']} (sort)")
            for ln in p.get("lineage") or []:
                if ln.get("column"):
                    self.add_used(ln["column"][0], ln["column"][1], p["id"])
        for v in self.report.get("visuals", []):
            for f in v.get("visual_filters") or []:
                if f.get("field_kind") == "Column":
                    self.add_used(f.get("table"), f.get("field"), "visual filter")
        for pg in self.report.get("pages", []):
            for f in pg.get("filters") or []:
                if f.get("field_kind") == "Column":
                    self.add_used(f.get("table"), f.get("field"), f"page filter {pg.get('name')}")
        for f in self.report.get("report_filters") or []:
            if f.get("field_kind") == "Column":
                self.add_used(f.get("table"), f.get("field"), "report filter")
        for m in self.measures:
            for bc in m.get("base_columns") or []:
                ref = parse_ref(bc)
                if ref:
                    self.add_used(ref[0], ref[1], f"measure {m['name']}")

    def _calc_closure(self):
        changed = True
        while changed:
            changed = False
            for (t, c) in list(self.used):
                col = self.cols.get((t, c)) or {}
                if col.get("sort_by") and (t, col["sort_by"]) not in self.used:
                    self.add_used(t, col["sort_by"], f"sort of {c}")
                    changed = True
                if not col.get("dax"):
                    continue
                for ref in self.translator.calc_column(t, c).column_refs:
                    r = parse_ref(ref)
                    if r and r not in self.used and r in self.cols:
                        self.add_used(r[0], r[1], f"{t}[{c}]")
                        changed = True

    # ---- step 3: joins ----------------------------------------------------------------------------------
    def _concat_shape(self, t, c):
        if not self.is_calc(t, c):
            return None
        ir = self.translator.calc_column(t, c).ir
        if isinstance(ir, Fn) and ir.name == "CONCAT" and all(isinstance(a, (Col, Lit)) for a in ir.args):
            cols = [a for a in ir.args if isinstance(a, Col)]
            if cols and all(x.table == t and not self.is_calc(t, x.column) for x in cols):
                shape = tuple("C" if isinstance(a, Col) else ("L", a.value) for a in ir.args)
                return shape, [x.column for x in cols]
        return None

    def _uses_userelationship(self, r) -> bool:
        for m in self.measures:
            dax = m.get("dax") or ""
            if "USERELATIONSHIP" in dax.upper() and r["from_column"].lower() in dax.lower():
                return True
        return False

    def _joins(self) -> list[dict]:
        used_tables = {t for t, _ in self.used}
        out = []
        for r in self.model.get("relationships", []):
            if r.get("auto_date"):
                continue
            child, parent = r["from_table"], r["to_table"]
            card = f"{r.get('from_cardinality')}-to-{r.get('to_cardinality')}"
            if r.get("from_cardinality") == "one" and r.get("to_cardinality") == "many":
                child, parent = parent, child
            ccol, pcol = (r["from_column"], r["to_column"]) if child == r["from_table"] else (r["to_column"], r["from_column"])
            desc = (f"{r['from_table']}[{r['from_column']}] → {r['to_table']}[{r['to_column']}] "
                    f"({card}, {'both directions' if str(r.get('cross_filter')).lower().startswith('both') else 'single'}"
                    f", {'active' if r.get('active') else 'inactive'})")
            j = {"pbi_relationship": desc, "child_table": child, "parent_table": parent, "child_pbi": [ccol],
                 "parent_pbi": [pcol], "type": "Left outer (child → parent)", "alias": "", "notes": [],
                 "status": PROPOSED}
            if child not in used_tables or parent not in used_tables:
                j["status"], j["notes"] = NOT_NEEDED, ["one side is not used by any insight or measure"]
                out.append(j)
                continue
            if not r.get("active"):
                if self._uses_userelationship(r):
                    alias = f"{ident(parent)}_{ident(ccol)}"
                    j["alias"] = alias
                    j["notes"].append(f"inactive in Power BI and used with USERELATIONSHIP: join to alias {alias}")
                else:
                    j["status"] = NOT_NEEDED
                    j["notes"].append("inactive in Power BI and no measure activates it (USERELATIONSHIP): no join "
                                      "or alias needed")
                    out.append(j)
                    continue
            if r.get("from_cardinality") == "many" and r.get("to_cardinality") == "many":
                j["status"] = NEEDS_DESIGN
                j["notes"].append("many-to-many: use a bridge table as the common parent and set the base table")
            if str(r.get("cross_filter")).lower().startswith("both"):
                j["notes"].append("both directions in Power BI: Incorta joins filter child → parent only; set the base "
                                  "table on insights that filter the other way")
            cs, ps = self._concat_shape(child, ccol), self._concat_shape(parent, pcol)
            if cs and ps and cs[0] == ps[0]:
                j["child_pbi"], j["parent_pbi"] = cs[1], ps[1]
                j["composite_from"] = (ccol, pcol)
                j["notes"].append(f"{child}[{ccol}] and {parent}[{pcol}] concatenate the same columns: composite join "
                                  f"on {', '.join(cs[1])} = {', '.join(ps[1])} (no key column to build)")
            for c in j["child_pbi"]:
                self.add_used(child, c, "join")
            for c in j["parent_pbi"]:
                self.add_used(parent, c, "join (key)")
            out.append(j)
        return out

    # ---- step 1 + 2: tables ------------------------------------------------------------------------------
    def _pq_steps(self, t: str) -> list[tuple]:
        expr = (self.tables.get(t) or {}).get("m_expression") or ""
        m = re.match(r"\s*let\s+(.*)\s+in\s+(.*?)\s*$", expr, re.S)
        if not m:
            return []
        steps = []
        for part in split_top_level(m.group(1)):
            name, _, val = part.partition("=")
            name = name.strip()
            name = name[2:-1] if name.startswith('#"') else name
            fn = re.match(r"([A-Za-z_][\w\.]*)\s*\(", val.strip())
            steps.append((name, fn.group(1) if fn else "Expression", val.strip()))
        return steps

    def _m_condition(self, t: str, text: str):
        """Power Query `each` row condition → Incorta load-filter formula (None when not simple)."""
        cond = re.sub(r"^\s*each\s+", "", text.strip())

        def col(name):
            return f"{self.names.object_raw(t)}.{self.names.column(t, name)}"

        def atom(a):
            a = a.strip()
            while a.startswith("(") and a.endswith(")") and _balanced(a[1:-1]):
                a = a[1:-1].strip()
            for words, fn in ((" or ", "or"), (" and ", "and")):
                parts = _split_words(a, words)
                if len(parts) > 1:
                    inner = [atom(p) for p in parts]
                    return None if None in inner else f"{fn}({', '.join(inner)})"
            if a.lower().startswith("not "):
                inner = atom(a[4:])
                return None if inner is None else f"not({inner})"
            m = re.match(r'^\[([^\]]+)\]\s*(<>|=|<=|>=|<|>)\s*(null|"(?:[^"]|"")*"|-?\d+(?:\.\d+)?|true|false)$', a, re.I)
            if not m:
                return None
            c, op, v = m.group(1), m.group(2), m.group(3)
            if (t, c) not in self.cols:
                return None
            if v.lower() == "null":
                return f"not(isNull({col(c)}))" if op == "<>" else (f"isNull({col(c)})" if op == "=" else None)
            if v.startswith('"'):
                inner = v[1:-1].replace('""', '"')
                v = '"' + inner.replace("\\", "\\\\").replace('"', '\\"') + '"'
            return f"{col(c)} {'!=' if op == '<>' else op} {v}"

        return atom(cond)

    def _power_query_graph(self):
        """Every Power Query query (loaded or connection-only), translated to Spark SQL in dependency order."""
        from .m2sql import MODEL_HINT, PowerQueryGraph

        queries, outputs, params, functions = {}, {}, {}, {}
        for name, t in self.tables.items():
            if not t.get("calculated") and t.get("m_expression"):
                queries[name] = t["m_expression"]
        for e in self.model.get("expressions") or []:
            name, code = e.get("name"), e.get("expression") or ""
            if not name or name in queries:
                continue
            pv = _param_value(code)
            if pv is not None:
                params[name] = pv
            elif not re.match(r"\s*(\(|let\s+\w+\s*=\s*\()[^)]*\)\s*(as\s+\w+\s*)?=>", code):
                queries[name] = code                     # connection-only query
            else:
                functions[name] = code                   # function query: used where a step calls it
        for (t, c), col in self.cols.items():
            if t in queries and not col.get("dax") and col.get("column_type") != "rowNumber" \
                    and not str(c).startswith("RowNumber"):
                outputs.setdefault(t, []).append((str(col.get("source_column") or c), c,
                                                  MODEL_HINT.get(str(col.get("data_type", "")).lower())))
        self.pq = PowerQueryGraph(queries, outputs, self.names.schema, namer=self.names.column, params=params,
                                  functions=functions)
        self.pq.translate_all()
        light = [q for q in self.pq.results if q in self.tables and self._light_query(q)]
        if light:                                  # these stay physical tables: re-translate the queries that read them
            for q in light:
                self.pq.objects[q] = self.pq.raw[q]
                self.pq.force_object[q] = self.pq.raw[q]
            self.pq.translate_all()

    LIGHT_STEPS = {"Table.TransformColumnTypes", "Table.RenameColumns", "Table.SelectColumns", "Table.RemoveColumns",
                   "Table.ReorderColumns", "Table.PromoteHeaders", "Table.Sort", "Table.Buffer", "Table.SelectRows",
                   "Table.Distinct", "Odbc.Query", "Sql.Database", "Sql.Databases", "Excel.Workbook", "Csv.Document",
                   "File.Contents", "Json.Document", "Expression", "Table.Skip", "Oracle.Database",
                   "PostgreSQL.Database", "MySQL.Database", "Snowflake.Databases", "GoogleBigQuery.Database",
                   "Value.NativeQuery"}

    def _light_query(self, name: str) -> bool:
        """One source and only steps the physical table itself can carry (types, names, columns, a simple row filter,
        a key): no MV, the table is the query (spec step 4: logic in the earliest layer)."""
        r = self.pq.results[name]
        if r.passthrough:
            return True
        if r.refs or r.unsupported or len(r.sources) != 1 or r.sources[0].kind == "folder" or name not in self.tables:
            return False
        if any(x.startswith(("unpivots", "skips a title")) for x in r.file_steps):
            return False
        for step, fn, val in self._pq_steps(name):
            if fn not in self.LIGHT_STEPS:
                return False
            if fn == "Table.SelectRows":
                cond = split_top_level(val[val.index("(") + 1: val.rindex(")")])[-1] if "(" in val else ""
                if self._m_condition(name, cond) is None:
                    return False
            if fn == "Table.Distinct" and "," not in val:
                return False
        return True

    def _pq_row(self, name: str, row: dict):
        """A Power Query query → its physical table (the external source as-is) and its MV (the steps)."""
        r = self.pq.results[name]
        src = r.sources[0] if r.sources else None
        obj = self.pq.object_name(name)
        row["pq"] = True
        row["physical"] = self.pq.raw[name] if src else ""
        row["pq_mv"] = "" if r.passthrough else obj
        row["refs"] = list(r.refs)
        row["file_steps"] = list(r.file_steps)
        row["raw_columns"] = list(r.raw_columns)
        if src is None:
            row.update(kind="Materialized view (Power Query)", source_type="Power Query (built from other queries)",
                       source_detail=", ".join(r.refs))
        elif src.kind in ("odbc", "sql"):
            label = f"ODBC DSN {src.target}" if src.kind == "odbc" else f"{src.connector} ({src.target}" + (
                f" / {src.database})" if src.database else ")")
            row.update(kind="Physical table (SQL)", source_type=label, dataset_sql=src.native_sql.strip())
            if src.kind == "odbc":
                row["notes"].append(f"Power BI reads DSN {src.target} over ODBC: create an Incorta data source for the "
                                    f"same database (the native query names it) and paste the dataset SQL")
        else:
            path = src.target or ""
            leaf = re.split(r"[\\/]", path.rstrip("\\/"))[-1] if path else ""
            row["file"] = leaf + (" (newest file in the folder)" if src.kind == "folder" else "")
            kind_label = FILE_CONNECTORS.get(src.connector, src.connector)
            row.update(kind="Physical table (data file)", source_type=f"Data files ({kind_label})",
                       source_detail=path)
            if r.file_steps:
                row["notes"].append("file selection in Power Query: " + "; ".join(r.file_steps))
            for n in src.notes:
                if n not in row["notes"]:
                    row["notes"].append(n)
            if src.kind == "folder":
                row["notes"].append(f"{MANUAL} Power BI picks the file in folder {path} at every refresh; in Incorta "
                                    f"upload the current file (same name every time) or point a folder data source at "
                                    f"it - see the decision")
        self.names.objects[name] = obj
        self.names.raw[name] = obj
        if r.unsupported:
            row["status"] = NEEDS_DESIGN
        if not r.passthrough:
            row["mv"] = obj
            row["notes"].append(f"the Power Query steps ({r.steps}) are rebuilt in the MV {obj}"
                                + (f", which reads {', '.join(self.pq.object_name(x) for x in r.refs)}" if r.refs else ""))
        for c in r.raw_columns:
            self.pq.raw_column(name, c)

    def _tables(self) -> dict:
        used_tables = {t for t, _ in self.used}
        feeds = {}
        for q in self.pq.upstream([t for t in used_tables if t in self.pq.queries]):
            feeds[q] = [d for d in self.pq.downstream(q)]
        rows = {}
        names = list(self.tables) + [q for q in self.pq.queries if q not in self.tables and q in feeds]
        for name in names:
            t = self.tables.get(name) or {"name": name, "row_count": None, "connection_only": True}
            src = (self.sources.get(name) or {}).get("m") or {}
            sql = (self.sources.get(name) or {}).get("sql") or {}
            cols = [c for (tt, _), c in self.cols.items() if tt == name]
            measures_here = [m for m in self.model.get("measures", []) if m.get("table") == name]
            row = {"pbi_table": name, "rows": t.get("row_count"), "include": name in used_tables or name in feeds,
                   "notes": [],
                   "decisions": [], "status": PROPOSED, "key_cols": [], "key": "", "load_filter": "",
                   "load_filter_m": [], "dataset_sql": "", "load_type": "Full", "kind": "", "source_type": "",
                   "source_detail": "", "file": "", "mv": "", "calc_table": bool(t.get("calculated"))}
            data_cols = [c for c in cols if c.get("column_type") != "rowNumber" and not str(c["column"]).startswith("RowNumber")]
            if name in feeds and name not in used_tables:
                row["notes"].append("not read by the report directly: feeds " + ", ".join(feeds[name]))
            if t.get("connection_only"):
                row["notes"].append("connection-only query in Power BI (not loaded): rebuilt because other queries read it")
            if measures_here and not t.get("calculated") and not any((name, c["column"]) in self.used for c in data_cols) \
                    and (t.get("row_count") or 0) <= 1 and name not in feeds:
                row.update(include=False, kind="Excluded", status=NOT_NEEDED,
                           reason=f"measure table ({len(measures_here)} measures, no data): its measures become "
                                  f"formula columns of business view {ident(name)}")
                rows[name] = row
                continue
            if not row["include"]:
                why = "not used by any insight, measure or join"
                if t.get("calculated") and "GENERATESERIES" in (t.get("calc_dax") or "").upper():
                    why += " (a what-if parameter: if a slicer needs it later, use an Incorta presentation variable)"
                row.update(kind="Excluded", status=NOT_NEEDED, reason=why)
                rows[name] = row
                continue
            if t.get("calculated"):
                row.update(kind="Materialized view (calculated table)", source_type="DAX calculated table",
                           source_detail=one_line(t.get("calc_dax"), 160))
                self.names.objects[name] = ident(name) + "_MV"
            elif name in self.pq.results and not self._light_query(name):
                self._pq_row(name, row)
            else:
                kind = src.get("source_kind")
                connector = src.get("connector") or ""
                if kind == "file":
                    path = src.get("server") or ""
                    row["file"] = re.split(r"[\\/]", path)[-1] if path else ""
                    row.update(kind="Physical table (data file)",
                               source_type=f"Data files ({FILE_CONNECTORS.get(connector, connector)})",
                               source_detail=row["file"])
                elif kind == "database":
                    row.update(kind="Physical table (SQL)", source_type=f"{connector} ({src.get('server') or '?'}"
                               f"{' / ' + src['database'] if src.get('database') else ''})")
                    native = src.get("native_query")
                    if native:
                        row["dataset_sql"] = native.strip()
                    elif src.get("source_objects"):
                        picked = [c for c in data_cols if (name, c["column"]) in self.used and not c.get("dax")]
                        back = (self.sources.get(name) or {}).get("back") or {}
                        sel = ",\n  ".join(
                            (f"{back.get(c.get('source_column') or c['column'], c.get('source_column') or c['column'])}"
                             f" AS {ident(c['column'])}") for c in picked) or "*"
                        row["dataset_sql"] = f"SELECT\n  {sel}\nFROM {src['source_objects'][-1]}"
                    if "TABLESAMPLE (random sample)" in (sql.get("flags") or []):
                        row["notes"].append("Power BI reads a random sample (TABLESAMPLE): decide whether Incorta loads "
                                            "the full table")
                elif kind == "embedded":
                    row.update(kind="Physical table (data file)", source_type="Data typed into Power BI (Enter data)",
                               source_detail="export it to a CSV and upload it as a data file")
                else:
                    row.update(kind="Physical table", source_type=connector or "unknown source")
                    row["notes"].append(f"{MANUAL} source {connector or 'not recognised'}: pick the matching Incorta "
                                        f"connector")
                tname = self.pq.raw.get(name, ident(name)) if hasattr(self, "pq") else ident(name)
                self.names.objects[name] = tname
                self.names.raw[name] = tname
                self._power_query(name, row)
                r = (self.pq.results.get(name) if hasattr(self, "pq") else None)
                if r and r.sources and r.sources[0].kind in ("odbc", "sql"):
                    src0 = r.sources[0]
                    row["source_type"] = (f"ODBC DSN {src0.target}" if src0.kind == "odbc" else
                                          f"{src0.connector} ({src0.target}" + (f" / {src0.database})" if src0.database else ")"))
                    row["dataset_sql"] = row["dataset_sql"] or src0.native_sql.strip()
            rows[name] = row
        # keys: the one side of each join (or the composite key)
        for j in self.joins:
            if j["status"] == NOT_NEEDED:
                continue
            p = rows.get(j["parent_table"])
            if p and p["include"] and not p["key_cols"]:
                p["key_cols"] = list(j["parent_pbi"])
        for name, row in rows.items():
            for c in [c for (tt, c) in self.cols if tt == name]:
                self.names.column(name, c)
            if row["key_cols"]:
                row["key"] = ", ".join(self.names.column(name, c) for c in row["key_cols"])
        return rows

    def _power_query(self, name: str, row: dict):
        for step, fn, val in self._pq_steps(name):
            if fn == "Table.SelectRows":
                cond = split_top_level(val[val.index("(") + 1: val.rindex(")")])[-1] if "(" in val else ""
                f = self._m_condition(name, cond)
                row["load_filter_m"].append(f"{step}: {one_line(cond, 200)}")
                if f is None:
                    row["notes"].append(f"{MANUAL} Power Query filter '{step}' ({one_line(cond, 120)}) is not a simple "
                                        f"condition: write it as the load filter or move it to an MV")
                elif f not in row["load_filter"]:
                    row["load_filter"] = f if not row["load_filter"] else f"and({row['load_filter']}, {f})"
            elif fn == "Table.Distinct":
                keys = re.findall(r'"((?:[^"]|"")*)"', val.split(",", 1)[1]) if "," in val else []
                if keys:
                    row["key_cols"] = [k for k in keys if (name, k) in self.cols]
                    row["notes"].append(f"Power Query removes duplicate {', '.join(keys)} rows ('{step}'): the Incorta "
                                        f"key on these columns keeps one row per key")
                else:
                    row["notes"].append(f"{MANUAL} Power Query removes duplicate rows ('{step}'): add a key or "
                                        f"deduplicate in an MV")
            elif fn in ("Table.Group", "Table.NestedJoin", "Table.Join", "Table.Pivot", "Table.Unpivot",
                        "Table.UnpivotOtherColumns", "Table.Combine", "Table.ExpandTableColumn", "Table.AddColumn",
                        "Table.ReplaceValue", "Table.FillDown", "Table.FillUp", "Table.SplitColumn"):
                row["notes"].append(f"{MANUAL} Power Query step '{step}' ({fn}) changes rows or columns: rebuild it in "
                                    f"the dataset SQL or an MV")

    # ---- step 4: calculated columns ------------------------------------------------------------------------
    def _calc_columns(self):
        fcols, mvs = [], []
        for name, row in self.table_rows.items():
            if not row["include"]:
                continue
            if row["calc_table"]:
                mvs.append(self._calc_table_mv(name, row))
                continue
            calc = [c for (t, c) in self.cols if t == name and self.is_calc(t, c) and (t, c) in self.used]
            if not calc:
                continue
            deps = {}
            needs_mv, reasons = False, []
            trs = {c: self.translator.calc_column(name, c) for c in calc}
            for c, tr in trs.items():
                deps[c] = [r[1] for r in map(parse_ref, tr.column_refs) if r and r[0] == name and self.is_calc(name, r[1])
                           and r[1] != c]
                ir = tr.ir
                if any(isinstance(n, CorrAgg) for n in walk(ir)):
                    needs_mv = True
                    reasons.append(f"{c} aggregates the table for each row ({_corr_text(ir)})")
                elif [r for r in tr.reviews if not r.startswith(SQL_GAP_PREFIXES)]:
                    needs_mv = True
                    reasons.append(f"{c}: {tr.reviews[0]}")
                elif self.formula(ir)[0] is None:
                    needs_mv = True
                    reasons.append(f"{c} uses a function with no Incorta formula equivalent")
            if needs_mv:
                mvs.append(self._table_mv(name, row, calc, deps, reasons))
                for c in calc:
                    fcols.append({"table": name, "column": c, "incorta_table": self.names.objects[name],
                                  "incorta_column": self.names.column(name, c), "home": f"MV {self.names.objects[name]}",
                                  "formula": f"(computed in {self.names.objects[name]}, see its SQL)",
                                  "dax": (self.cols[(name, c)].get("dax") or ""), "purpose": "",
                                  "status": PROPOSED, "notes": []})
                continue
            for c in calc:
                tr = trs[c]
                inline = bool(deps.get(c))
                ir = self.translator.calc_column(name, c, inline_calc=True).ir if inline else tr.ir
                text, used = self.formula(ir)
                notes = list(tr.notes)
                if inline:
                    notes.append(f"repeats the logic of {', '.join(deps[c])}: an Incorta formula column cannot use another "
                                 f"formula column of the same table")
                if "lookup" in used:
                    for lk in [n for n in walk(ir) if isinstance(n, Lookup)]:
                        lt = self.table_rows.get(lk.result.table)
                        keys = [k.column for k, _ in lk.keys]
                        if lt is not None:
                            if not lt["key_cols"]:
                                lt["key_cols"] = keys
                                lt["key"] = ", ".join(self.names.column(lk.result.table, k) for k in keys)
                                lt["notes"].append(f"key {', '.join(keys)}: lookup() from {name}[{c}] needs it")
                            elif lt["key_cols"] != keys:
                                notes.append(f"{MANUAL} lookup() needs the key of {lk.result.table} to be "
                                             f"{', '.join(keys)}")
                status = PROPOSED
                if text and len(text) > 1500:
                    notes.append(f"{MANUAL} long formula after repeating the columns it uses: consider an MV")
                    status = NEEDS_DESIGN
                fcols.append({"table": name, "column": c, "incorta_table": self.names.objects[name],
                              "incorta_column": self.names.column(name, c), "home": "Physical formula column",
                              "formula": text or "", "dax": self.cols[(name, c)].get("dax") or "",
                              "purpose": _purpose(ir, tr), "status": status, "notes": notes})
        return fcols, mvs

    def _pq_mvs(self) -> list[dict]:
        """One MV per Power Query query with steps, in load order (inputs first)."""
        out = []
        schema = self.names.schema
        for name in self.pq.order():
            row = self.table_rows.get(name)
            if not row or not row["include"] or not row.get("pq") or not row.get("pq_mv"):
                continue
            r = self.pq.results[name]
            code = self.pq.queries[name]
            obj = row["pq_mv"]
            reads = [f"{schema}.{self.pq.object_name(x)}" for x in r.refs] + (
                [f"{schema}.{row['physical']}"] if row["physical"] else [])
            fns = _step_functions(code)
            if "Table.Combine" in fns:
                grain = f"union of {len(r.refs) or 'its'} inputs (Table.Combine)"
            elif "Table.Group" in fns:
                grain = "one row per group (Table.Group)"
            else:
                grain = f"one row per row of {row['physical'] or (r.refs[:1] or [name])[0]}" + (
                    " (after its filters)" if "Table.SelectRows" in fns else "")
            reviews = [f"step '{a}': {b}" for a, b in r.unsupported]
            if r.unsupported:
                sql = _untranslated_sql(name, r, code)
                self.check(f"{schema}.{obj}", f"Power Query step not translated: {reviews[0]}",
                           f"{name} and everything built from it", "write the remaining steps in the MV SQL (the M code is "
                           "in the .sql file)", "High")
            else:
                sql = r.sql
            did = ""
            for topic, question in r.decisions:
                did = self.decision(f"{name}: {topic}", question, "", "see the decision text",
                                    "Power Query and Spark SQL differ here; the MV follows Power BI until you decide")
            src = row.get("file_steps") and any("newest" in x for x in row["file_steps"])
            if src:
                did = self.decision(f"{name}: newest file in a folder", "Power BI reads the newest file of the folder "
                                    f"{row['source_detail']} at every refresh.",
                                    "(a) upload the current file under one fixed name each period; (b) an Incorta folder "
                                    "data source that loads the latest file; (c) load every file and keep the latest in "
                                    "the MV", "(a)", "simplest to operate and to reconcile")
            out.append({
                "name": obj, "pbi_table": name, "kind": "Power Query MV", "grain": grain, "language": "Spark SQL",
                "sql": sql, "reads": reads, "columns": [self.pq.column_of(name, c) for c in r.columns],
                "key": row.get("key", ""), "serves": self._insights_using(name),
                "reason": f"Power Query '{name}': {r.steps} steps ({', '.join(fns[:6])}"
                          + (", ..." if len(fns) > 6 else "") + ")" + (f"; reads {', '.join(r.refs)}" if r.refs else ""),
                "rule": "Power Query steps: one MV per query, reading the Incorta objects of its inputs",
                "notes": list(r.notes) + (["file: " + "; ".join(r.file_steps)] if r.file_steps else []),
                "reviews": reviews, "decision": did, "status": NEEDS_DESIGN if r.unsupported else PROPOSED,
                "depth": self.pq.depth(name), "steps": r.steps, "m_query": name,
                "feeds": [d for d in self.pq.downstream(name) if (self.table_rows.get(d) or {}).get("include")]})
        return out

    def _table_mv(self, name, row, calc, deps, reasons) -> dict:
        mv_name = ident(name) + ("_CALC_MV" if row.get("pq_mv") else "_MV")
        self.names.objects[name] = mv_name
        self.names.mv_only[name] = set(calc)
        source_cols = [c for (t, c) in self.cols if t == name and not self.is_calc(t, c) and (t, c) in self.used]
        label = (f"Incorta table {self.names.object_raw(name)} loaded from {row['file']}" if row.get("file")
                 else f"Incorta table {self.names.object_raw(name)}")
        sp = sparksql.table_mv_sql(name, source_cols, calc, deps, self.translator, self.names, label,
                                   types={c: self.ctype(name, c) for c in source_cols})
        row["mv"] = mv_name
        row["notes"].append(f"insights and joins read {mv_name}, which adds the {len(calc)} calculated columns")
        did = self.decision(
            f"{name} calculated columns",
            f"Compute the {len(calc)} calculated columns of {name} in the MV {mv_name}",
            f"(a) MV {mv_name} over {ident(name)} (one MV at the table's grain); (b) a small MV for the per-row "
            f"aggregate joined back as a parent, plus physical formula columns that repeat the logic they use",
            "(a)", "; ".join(reasons) + ". Physical formula columns cannot aggregate or use another formula "
            "column of the same table, and the MV keeps each column readable.")
        return {"name": mv_name, "pbi_table": name, "kind": "Table MV (calculated columns)",
                "grain": f"one row per {name} row", "language": "Spark SQL", "sql": sp["sql"],
                "reads": [self.names.object_raw(name)], "columns": sp["columns"], "key": row.get("key", ""),
                "serves": self._insights_using(name), "reason": "; ".join(reasons), "rule": "MV rule 2 (window / "
                "multi-step logic)", "notes": sp["notes"], "reviews": sp["reviews"], "decision": did,
                "status": PROPOSED if not sp["reviews"] else NEEDS_DESIGN}

    def _calc_table_mv(self, name, row) -> dict:
        mv_name = self.names.objects[name]
        sp = sparksql.calc_table_sql(name, self.tables[name].get("calc_dax") or "", self.translator, self.names)
        for c in [c for (t, c) in self.cols if t == name]:
            self.names.column(name, c)
        reads = sorted({self.names.object_raw(t) for t in _tables_in_dax(self.tables[name].get("calc_dax") or "",
                                                                          self.tables) if t != name})
        if row["key_cols"] == [] and any(c == "Date" for (t, c) in self.cols if t == name):
            row["key_cols"], row["key"] = ["Date"], self.names.column(name, "Date")
        return {"name": mv_name, "pbi_table": name, "kind": "Calculated table MV", "grain": _grain_of_calc(name, self),
                "language": "Spark SQL", "sql": sp["sql"], "reads": reads, "columns": sp["columns"],
                "key": row.get("key", ""), "serves": self._insights_using(name),
                "reason": "DAX calculated table: Incorta builds the same rows with Spark SQL", "rule": "calculated table",
                "notes": sp["notes"], "reviews": sp["reviews"], "decision": "",
                "status": PROPOSED if not sp["reviews"] else NEEDS_DESIGN}

    def _insights_using(self, table: str) -> list[str]:
        out = []
        for p in self.plans:
            tables = {d["table"] for d in p.get("dims") or []}
            for mref in p.get("measures") or []:
                m = self.mindex.get(mref["name"] if isinstance(mref, dict) else mref)
                if m and m.get("translation") is not None and m["translation"].ir is not None:
                    tables |= {a.table for a in aggs_in(m["translation"].ir, into_scalar=True)}
                    tables |= {n.date.table for n in walk(m["translation"].ir) if isinstance(n, TimeShift)}
            if table in tables:
                out.append(p["id"])
        return out

    def _link_joins(self):
        for j in self.joins:
            j["child"] = self.names.object(j["child_table"])
            j["parent"] = self.names.object(j["parent_table"])
            j["child_columns"] = [self.names.column(j["child_table"], c) for c in j["child_pbi"]]
            j["parent_columns"] = [self.names.column(j["parent_table"], c) for c in j["parent_pbi"]]
            if j["status"] == NOT_NEEDED:
                j["key_unique"] = j["types_match"] = "-"
                continue
            ct = [self.ctype(j["child_table"], c) for c in j["child_pbi"]]
            pt = [self.ctype(j["parent_table"], c) for c in j["parent_pbi"]]
            norm = lambda x: {"timestamp": "date", "long": "number", "double": "number"}.get(x, x)  # noqa: E731
            fixed = False
            if [norm(x) for x in ct] != [norm(x) for x in pt] and len(ct) == 1 and ct[0] == "string" \
                    and pt[0] in ("long", "double"):
                f = next((f for f in self.formula_cols if f["table"] == j["child_table"]
                          and f["column"] == j["child_pbi"][0] and f["home"] == "Physical formula column"), None)
                if f and f["formula"]:
                    f["formula"] = f"{'int' if pt[0] == 'long' else 'double'}({f['formula']})"
                    f["notes"].append(f"cast to a number so join {j.get('id') or ''} compares the same type as "
                                      f"{j['parent_table']}[{j['parent_pbi'][0]}]".replace("  ", " "))
                    fixed = True
            if fixed:
                j["types_match"] = f"Yes after the cast ({pt[0]}): the child formula column is converted"
            elif [norm(x) for x in ct] == [norm(x) for x in pt]:
                j["types_match"] = "Yes (" + ", ".join(ct) + ")"
            else:
                j["types_match"] = "No: " + ", ".join(f"{a} vs {b}" for a, b in zip(ct, pt))
                j["notes"].append(f"{MANUAL} cast one side so both columns have the same type")
            p = self.table_rows.get(j["parent_table"]) or {}
            if p.get("key_cols") == j["parent_pbi"]:
                j["key_unique"] = f"Key on {j['parent']} ({p['key']})"
            else:
                j["key_unique"] = f"{MANUAL} make {', '.join(j['parent_columns'])} unique on {j['parent']}"
            for side in ("child", "parent"):
                tname = j[f"{side}_table"]
                for c in j[f"{side}_pbi"]:
                    if self.is_calc(tname, c) and tname not in self.names.mv_only:
                        j["notes"].append(f"{c} is a formula column on {tname}: Incorta can join on a child formula "
                                          f"column" + ("" if side == "child" else f"; {MANUAL} a parent key that is a "
                                                       f"formula column may need to move into the dataset SQL or an MV"))
        for k, j in enumerate([j for j in self.joins if j["status"] != NOT_NEEDED], 1):
            j["id"] = f"J{k}"
        for j in self.joins:
            j.setdefault("id", "-")

    # ---- step 4: measures -----------------------------------------------------------------------------------
    def _measures(self) -> list[dict]:
        rows = []
        for m in self.measures:
            tr = m.get("translation")
            ir = tr.ir if tr is not None else None
            hard = [r for r in (tr.reviews if tr else []) if not r.startswith(SQL_GAP_PREFIXES)]
            view = ident(m.get("display_folder") or m["table"])
            self.names.views[m["name"]] = view
            base_name, n = ident(m["name"]), 2
            mname = base_name
            while any(r["view"] == view and r["incorta_name"].lower() == mname.lower() for r in rows):
                mname, n = f"{base_name}_{n}", n + 1
            direct = list(m.get("used_directly_in") or [])
            via = list(m.get("used_via") or [])
            row = {"measure": m["name"], "pbi_table": m["table"], "view": view, "incorta_name": mname,
                   "dax": m.get("dax") or "", "pattern": m.get("pattern"), "used_by": direct, "used_via": via,
                   "format": m.get("format_string") or "", "notes": [], "status": PROPOSED, "formula": "",
                   "formula_expanded": "", "features": [], "decision": ""}
            if hard or ir is None:
                row.update(home="Decision needed", status=NEEDS_DESIGN)
                row["notes"] += hard or ["no translation"]
                rows.append(row)
                continue
            text, used = self.formula(ir, level_dims=[], rank_dims=[])
            if text is None:
                row.update(home="Decision needed", status=NEEDS_DESIGN)
                row["notes"].append("no Incorta formula equivalent for this logic")
                rows.append(row)
                continue
            row["formula_expanded"], row["features"] = text, sorted(used)
            per_insight = bool({"groupBy", "rank"} & used)
            pattern = m.get("pattern") or ""
            if pattern.startswith("Mixed grain"):
                row["home"] = "MV at the insight grain"
                row["notes"].append("mixes a fact aggregate with a dimension aggregate: a formula over the fact join "
                                    "would repeat the dimension values per fact row, so each insight that shows it gets "
                                    "an MV at its grain (spec: mixed grain)")
                rows.append(row)
                continue
            if pattern.startswith("Dimension-grain"):
                tables = sorted({a.table for a in aggs_in(ir)})
                row["home"] = f"Insight formula (base table {', '.join(self.names.object(t) for t in tables)})"
                row["base_tables"] = tables
                row["notes"].append(f"aggregates {', '.join(tables)}: set it as the insight's base table so Incorta "
                                    f"counts {', '.join(tables)} rows, not fact rows")
                rows.append(row)
                continue
            if per_insight:
                row["home"] = "Insight formula (groupBy uses the insight's columns)"
            elif len(direct) >= 2 or via:
                row["home"] = f"Business view formula ({self.names.bs}.{view})"
            else:
                row["home"] = f"Insight formula ({direct[0]})" if direct else f"Business view formula ({self.names.bs}.{view})"
            if "ago" in used or "toDate" in used:
                row["notes"].append("time series: add it to the Measure tray of an insight grouped by "
                                    "Date.Date (Date Part = Month)")
            if "groupBy" in used:
                row["notes"].append(f"{MANUAL} level-based measure: groupBy() lists the insight's grouping columns "
                                    f"that Power BI keeps (same order). Dashboard filters on the removed columns still "
                                    f"apply in Incorta, while REMOVEFILTERS ignores them in Power BI")
            if "rank" in used:
                row["notes"].append("rank: groupBy() lists the insight's other grouping columns (rank within them); "
                                    "hide the total row (Power BI shows no rank there)")
            if any(isinstance(n, Agg) and n.distinct and n.pred is not None for n in walk(ir)):
                row["notes"].append(f"{MANUAL} distinct(if(..., x, null)): check that distinct() does not count null")
            rows.append(row)
        self._bv_measures = sorted([r for r in rows if r.get("formula_expanded")
                                    and r.get("home", "").startswith("Business view")],
                                   key=lambda r: -len(r["formula_expanded"]))
        for r in rows:
            r["formula"] = self.compose(r["formula_expanded"], r)
        return rows

    def _insight_mvs(self):
        """Insights that show a mixed-grain measure get an MV at their grain (one per grain), built from the
        insight's two-step reference SQL re-generated in Spark SQL with the Incorta names."""
        mixed = {m["measure"] for m in self.measure_rows if m.get("home") == "MV at the insight grain"}
        if not mixed:
            return
        from ..analyze.planner import Planner
        from ..analyze.querygen import Naming
        import sqlglot
        from sqlglot import exp as sx

        names = self.names

        class IncortaNaming(Naming):
            def view(self, table):
                return names.object(table)

        pl = Planner(self.report, self.model, dialect="spark")
        pl.naming = IncortaNaming()
        pl.qb.naming = pl.naming
        spark_plans = {p["id"]: p for p in pl.plan_report()}
        rev = {names.object(t).lower(): t for t in self.tables}
        by_grain: dict[tuple, dict] = {}
        for p in self.plans:
            ms = [x["name"] if isinstance(x, dict) else x for x in p.get("measures") or []]
            hit = [m for m in ms if m in mixed or set(self.mindex.get(m, {}).get("depends_on") or []) & mixed]
            sp = spark_plans.get(p["id"])
            if not hit or not sp or not sp.get("sql"):
                continue
            grain = tuple((d["table"], d["column"]) for d in p.get("dims") or [])
            try:
                tree = sqlglot.parse_one(sp["sql"], read="spark")
                alias = {}
                for t in tree.find_all(sx.Table):
                    full = (f"{t.db}.{t.name}" if t.db else t.name).lower()
                    if full in rev:
                        alias[t.alias_or_name] = rev[full]
                formula_cols = set()
                for c in tree.find_all(sx.Column):
                    if c.table in alias:
                        t = alias[c.table]
                        if self.is_calc(t, c.name) and t not in names.mv_only:
                            formula_cols.add(f"{names.object(t)}.{names.column(t, c.name)}")
                        c.set("this", sx.to_identifier(names.column(t, c.name)))
                sql = tree.sql(dialect="spark", pretty=True)
            except Exception as ex:  # noqa: BLE001
                sql = f"-- [MANUAL CHECK REQUIRED] could not convert the reference SQL ({ex})\n" + sp["sql"]
            mv = by_grain.get(grain)
            if mv is None:
                dims = [d["column"] for d in p.get("dims") or []] or ["total"]
                name = ("_".join([ident(hit[0])] + [ident(x) for x in dims[:2]]) + "_MV").upper()[:60]
                mv = {"name": name, "pbi_table": "", "kind": "Insight-grain MV (mixed grain)",
                      "grain": " × ".join(d["column"] for d in p.get("dims") or []) or "one row (total)",
                      "language": "Spark SQL", "sql": sql, "reads": sorted({names.object(t) for t in sp.get("tables", [])
                                                                            if t in self.tables} or
                                                                           {names.object(a) for a in alias.values()}),
                      "columns": [d["column"] for d in p.get("dims") or []] + ms, "key": "", "serves": [],
                      "reason": f"{', '.join(hit)} mixes a fact-level and a dimension-level aggregate; the MV computes "
                                f"each part at its own grain and joins them at the insight grain",
                      "rule": "MV rule 2 (mixed grain)", "notes": [
                          "an MV only filters on the columns it carries: add the dashboard filter columns users need "
                          "to its grain", "reference SQL: the insight's two-step query, regenerated in Spark SQL"],
                      "reviews": [], "decision": "", "status": PROPOSED, "measures": hit}
                self.mvs.append(mv)
                by_grain[grain] = mv
            if formula_cols:
                why = (f"reads physical formula column(s) {', '.join(sorted(formula_cols))}: check that your Incorta "
                       f"version lets MVs read formula columns, otherwise repeat their logic in the MV")
                if why not in mv["reviews"]:
                    mv["reviews"].append(why)
            mv["serves"].append(p["id"])
            p["_mv"] = mv["name"]
        if by_grain:
            self.decision("Mixed-grain measures", f"Build {len(by_grain)} MV(s) at the insight grain for "
                          f"{', '.join(sorted(mixed))}", "(a) one MV per insight grain from the reference SQL; "
                          "(b) level-based measures when the levels are exactly the insight's grouping columns",
                          "(a)", "a formula over the fact join would repeat the dimension values per fact row")

    def compose(self, text: str, own=None) -> str:
        """Replace the expanded text of business-view measures by a reference to them (readable formulas).
        Formulas with rank() or groupBy() stay expanded: those functions take the aggregation itself."""
        if not text or "rank(" in text.lower() or "groupBy(" in text:
            return text
        for other in getattr(self, "_bv_measures", []):
            if other is own or len(other["formula_expanded"]) < 24 or other["formula_expanded"] == text:
                continue
            if other["formula_expanded"] in text:
                text = text.replace(other["formula_expanded"], f"{self.names.bs}.{other['view']}.{other['incorta_name']}")
        return text

    # ---- step 5: insights ---------------------------------------------------------------------------------
    def _insights(self):
        rows, filters = [], []
        self._kpi_ts = []
        for p in self.static:
            rows.append({"id": p["id"], "dashboard": self.report_name, "tab": p.get("page"), "title": p.get("title") or "",
                         "pbi_visual": p.get("visual_type"), "visualization": VISUAL_MAP.get(p.get("visual_type"),
                         "Recreate by hand"), "base_table": "", "dimensions": "", "measures": "", "filters": "",
                         "sort": "", "drill": "", "objects": "", "formulas": [], "notes": [], "status": PROPOSED,
                         "expected_rows": "", "reconciled": "-"})
        runs = ((self.data.get("checks") or {}).get("test_run") or {})
        hier = {}
        for h in self.model.get("hierarchies") or []:
            hier.setdefault((h.get("table"), h.get("level")), h.get("hierarchy"))
        for p in self.plans:
            vt = p.get("visual_type")
            dims = p.get("dims") or []
            mnames = [x["name"] if isinstance(x, dict) else x for x in p.get("measures") or []]
            dim_refs = [self._bv_col(d["table"], d["column"]) for d in dims]
            viz = VISUAL_MAP.get(vt, p.get("generic_type") or vt)
            if vt in ("tableEx", "table") and not mnames:
                viz = "Listing table"
            if vt == "slicer":
                for d in dims:
                    filters.append({"tab": p.get("page"), "field": self._bv_col(d["table"], d["column"]),
                                    "pbi_field": f"{d['table']}[{d['column']}]", "visual": p["id"],
                                    "type": "Dashboard filter (prompt)", "applies_to": "all insights of the tab"})
            formulas, notes, status, objects = [], [], PROPOSED, set()
            for mn in mnames:
                mrow = next((r for r in self.measure_rows if r["measure"] == mn), None)
                if not mrow:
                    continue
                if mrow["status"] == NEEDS_DESIGN:
                    status = NEEDS_DESIGN
                    notes.append(f"{mn}: waits for decision {mrow.get('decision') or '(see Decision Log)'}")
                    continue
                if mrow["home"].startswith("Insight formula (groupBy"):
                    m = self.mindex[mn]
                    text, _ = self.formula(m["translation"].ir,
                                           level_dims=lambda n, p=p: self._dims_except(p, n.tables, n.columns),
                                           rank_dims=lambda n, p=p: self._dims_except(
                                               p, [x for x in n.over if isinstance(x, str)],
                                               [x for x in n.over if isinstance(x, Col)]))
                    formulas.append({"measure": mn, "formula": text})
            ts = [mn for mn in mnames if {"ago", "toDate"} & set(
                (next((r for r in self.measure_rows if r["measure"] == mn), {}) or {}).get("features", []))]
            date_tables = {n.date.table for mn in ts for n in walk(self.mindex[mn]["translation"].ir)
                           if isinstance(n, TimeShift)}
            if ts and not any(d["table"] in date_tables for d in dims):
                notes.append(f"{MANUAL} {', '.join(ts)} use ago(): Incorta compares with the previous period of the "
                             f"date grouping. With no date column in the insight, add Date.Date (Date Part = Month) "
                             f"as a grouping or filter the tab to one month")
                self._kpi_ts.append(p["id"])
            for d in dims:
                objects.add(self.names.object(d["table"]))
            for mn in mnames:
                m = self.mindex.get(mn)
                if m and m.get("translation") is not None and m["translation"].ir is not None:
                    for a in aggs_in(m["translation"].ir, into_scalar=True):
                        objects.add(self.names.object(a.table))
            fact_tables = [o for o in objects if any(self.names.object(t) == o and self._is_fact(t) for t in self.tables)]
            base = ""
            dim_grain = [t for mn in mnames for r in self.measure_rows if r["measure"] == mn
                         for t in r.get("base_tables", [])]
            if p.get("_mv"):
                base = f"{self.names.schema}.{p['_mv']}"
                objects.add(base)
                notes.append(f"built on the MV {p['_mv']} (mixed-grain measure)")
            elif mnames and dim_grain and not fact_tables:
                base = self.names.object(dim_grain[0])
                notes.append(f"base table {base}: the measures count {dim_grain[0]} rows")
            elif mnames and fact_tables:
                base = sorted(fact_tables)[0]
            elif dims and not mnames and vt != "slicer":
                base = self.names.object(dims[0]["table"])
                notes.append("dimension-only insight: set the base table so Incorta does not count fact rows")
            levels = [hier.get((d["table"], d["column"])) for d in dims]
            drill = ""
            if len([x for x in levels if x]) >= 2 and len(set(x for x in levels if x)) == 1:
                drill = f"drill down {' → '.join(d['column'] for d in dims)} (hierarchy {levels[0]})"
            run = runs.get(p["id"]) or {}
            rows.append({"id": p["id"], "dashboard": self.report_name, "tab": p.get("page"),
                         "title": p.get("title") or _auto_title(mnames, dims), "pbi_visual": vt, "visualization": viz,
                         "base_table": base, "dimensions": ", ".join(dim_refs),
                         "measures": ", ".join(self._measure_ref(mn) for mn in mnames),
                         "filters": "; ".join(_filter_text(f) for f in p.get("filters") or []),
                         "sort": _sort_text(p.get("sort_text")), "drill": drill, "objects": ", ".join(sorted(objects)),
                         "formulas": formulas, "notes": notes + list(p.get("notes") or [])[:3], "status": status,
                         "expected_rows": run.get("rows", "") if run.get("status") == "Runs" else "",
                         "reconciled": "Not yet"})
        return rows, filters

    def _is_fact(self, table: str) -> bool:
        return any(r.get("from_table") == table and r.get("from_cardinality") == "many"
                   for r in self.model.get("relationships", []))

    def _dims_except(self, plan, tables, columns) -> list[str]:
        out = []
        for d in plan.get("dims") or []:
            if "*" in tables or d["table"] in tables or Col(d["table"], d["column"]) in columns:
                continue
            out.append(self.names.full(d["table"], d["column"]))
        return out

    def _bv_col(self, table: str, col: str) -> str:
        return f"{self.names.bs}.{ident(table)}.{ident(col)}"

    def _measure_ref(self, mn: str) -> str:
        r = next((x for x in self.measure_rows if x["measure"] == mn), None)
        if r and r.get("home", "").startswith("Business view"):
            return f"{self.names.bs}.{r['view']}.{r['incorta_name']}"
        if r and r.get("home") == "MV at the insight grain":
            return f"{mn} (MV column)"
        return f"{mn} (insight formula)" if r and r["status"] != NEEDS_DESIGN else f"{mn} (decision needed)"

    # ---- step 2: business schema ---------------------------------------------------------------------------
    def _business_schema(self) -> list[dict]:
        rows = []
        for name, t in self.table_rows.items():
            if not t["include"]:
                continue
            view = ident(name)
            direct = {c for (tt, c), why in self.used.items() if tt == name and any(
                not (w.startswith(("join", "sort of")) or "[" in w) for w in why)}
            if not direct:
                continue
            for (tt, c), col in self.cols.items():
                if tt != name or (tt, c) not in self.used:
                    continue
                is_key = c in t["key_cols"] or any(c in j["child_pbi"] for j in self.joins if j["child_table"] == name)
                if c not in direct and not is_key:
                    continue
                hidden = bool(col.get("hidden")) or (is_key and c not in direct)
                rows.append({"business_schema": self.names.bs, "view": view, "column": ident(c), "label": c,
                             "kind": "Column", "source": self.names.full(name, c), "formula": "",
                             "hidden": "Yes" if hidden else "No", "sort_by": ident(col["sort_by"]) if col.get("sort_by") else "",
                             "status": PROPOSED})
        for m in self.measure_rows:
            if m["status"] == NEEDS_DESIGN or not m.get("home", "").startswith("Business view"):
                continue
            rows.append({"business_schema": self.names.bs, "view": m["view"], "column": m["incorta_name"],
                         "label": m["measure"], "kind": "Formula (measure)", "source": f"DAX measure {m['measure']}",
                         "formula": m["formula"], "hidden": "No", "sort_by": "", "status": m["status"]})
        return rows

    # ---- decisions and manual checks --------------------------------------------------------------------------
    def _model_decisions(self):
        for d in self.data.get("decisions") or []:
            did, text, options, rec = (list(d) + ["", "", "", ""])[:4]
            if str(text).startswith(("First target tool", "Where the logic lives")):
                continue                    # answered by the spec: Incorta, logic placed layer by layer
            self.decision("Workbook finding", text, options, rec, f"from the migration workbook ({did})",
                          source="Workbook")
        composite = [j for j in self.joins if j.get("composite_from")]
        if composite:
            self.decision("Concatenated keys",
                          "Join on the underlying columns instead of building the concatenated key columns",
                          "(a) composite joins on the source columns; (b) build the key columns (formula column on the "
                          "child, dataset SQL / MV on the parent) and join on them",
                          "(a)", "; ".join(f"{j['child_table']}[{j['composite_from'][0]}] → {j['parent_table']}"
                                           f"[{j['composite_from'][1]}]" for j in composite)
                          + ": same result without new key columns. Rows with a blank part do not match in Incorta "
                            "(Power BI matched blank to blank).")
        for name, row in self.table_rows.items():
            if row.get("reason", "").startswith("measure table"):
                continue
            if not row["include"] and self.tables[name].get("calculated"):
                self.decision(f"{name} (calculated table)", f"Do not build {name}",
                              "(a) leave it out; (b) build it as an MV or a presentation variable",
                              "(a)", row.get("reason", ""))
        inactive = [j for j in self.joins if j["status"] == NOT_NEEDED and "inactive" in " ".join(j["notes"])]
        if inactive:
            names = ", ".join(f"{j['child_table']}[{j['child_pbi'][0]}]" for j in inactive)
            self.decision("Inactive relationships", f"No aliases for the {len(inactive)} inactive relationship(s): "
                          f"{names}", "(a) leave them out; (b) an alias of the parent per relationship, with its own join",
                          "(a)", "no measure activates them with USERELATIONSHIP")
        for m in self.measure_rows:
            if m["status"] == NEEDS_DESIGN:
                m["decision"] = self.decision(
                    f"Measure {m['measure']}", f"Design {m['measure']} in Incorta",
                    _design_options(m), "(a)" if "TOPN" in m["dax"].upper() else "",
                    "; ".join(m["notes"]) or "the rule engine has no automatic home for it", source="Rule")
        lbm = [m["measure"] for m in self.measure_rows if "groupBy" in m.get("features", [])]
        if lbm:
            self.check(f"Measures {', '.join(lbm)}", "REMOVEFILTERS / ALL ignores slicers on the removed table in Power "
                       "BI; an Incorta level-based measure still applies dashboard filters on those columns",
                       "the share / benchmark differs when a dashboard filter is set on the removed table's columns",
                       "exclude those dashboard filters from these insights (or keep the filter off), or build an MV",
                       "Medium")
        if self._kpi_ts:
            self.check(f"Insights {', '.join(self._kpi_ts)}", "time-series measures (ago) in an insight without a date "
                       "grouping", "the previous-month value is computed for the wrong period or is empty",
                       "add Date.Date with Date Part = Month as a grouping, or show one month only", "Medium")
        for f in self.formula_cols:
            for n in f["notes"]:
                if n.startswith(MANUAL):
                    self.check(f"{f['table']}[{f['column']}]", n.replace(MANUAL, "").strip(), "formula may not "
                               "validate", "check it in the Formula Builder")
        for mv in self.mvs:
            for r in mv.get("reviews") or []:
                self.check(f"MV {mv['name']}", r, "SQL may not match Power BI", "review the marked lines")
        if any(t.get("file") for t in self.table_rows.values() if t["include"]):
            self.check("Data files", "column names: the plan uses Incorta-safe names (spaces become _). Incorta may "
                       "keep the CSV header names", "formulas and MV SQL fail if the names differ",
                       "after uploading the files, compare the column names with the Source Map and adjust", "Low")
        for name, row in self.table_rows.items():
            if row["include"] and row["load_filter"]:
                self.check(f"{name} load filter", "Power Query keeps rows where the filter is true; Incorta's load "
                           "filter must do the same", "row counts differ", f"check the {name} row count after load "
                           f"({row.get('rows') or '?'} rows in Power BI)", "Low")
            if any("duplicate" in n for n in row["notes"]):
                self.check(f"{name} key", "Power Query keeps the first duplicate row; an Incorta key keeps one row per "
                           "key (check which)", "attribute values of duplicate keys may differ",
                           "compare row counts and a few duplicate keys after load", "Low")
        dc = [m["measure"] for m in self.measure_rows if any(MANUAL in n and "distinct" in n for n in m["notes"])]
        if dc:
            self.check(f"Measures {', '.join(dc)}", "distinct(if(..., x, null)) must not count the null",
                       "the count is one higher than Power BI", "compare with the Power BI value", "Low")

    # ---- result ------------------------------------------------------------------------------------------------
    def _lineage(self) -> list[dict]:
        """One row per Power Query query in the build: its physical table, MV, inputs and readers."""
        out = []
        used = {t for t, _ in self.used}
        for q in self.pq.order():
            row = self.table_rows.get(q)
            if not row or not row["include"]:
                continue
            r = self.pq.results[q]
            if not row.get("pq"):                  # a plain table (light query) that a Power Query MV reads
                if not any(q in self.pq.refs.get(x, []) for x in self.pq.results):
                    continue
                out.append({
                    "query": q, "role": "report table" if q in used else "upstream", "depth": 0,
                    "physical": self.names.objects.get(q, self.pq.raw[q]), "mv": "",
                    "object": self.names.objects.get(q, self.pq.raw[q]),
                    "source": row["source_type"] + (f": {row['file']}" if row["file"] else ""), "reads": [],
                    "reads_queries": [], "feeds": [d for d in self.pq.downstream(q)
                                                   if (self.table_rows.get(d) or {}).get("include")],
                    "steps": r.steps, "translation": "Plain table (types, names and filters only)", "untranslated": [],
                    "notes": [], "file_steps": list(r.file_steps), "rows": row.get("rows"), "check": "",
                    "check_detail": ""})
                continue
            out.append({
                "query": q, "role": "report table" if q in used else "upstream", "depth": self.pq.depth(q),
                "physical": row.get("physical", ""), "mv": row.get("pq_mv", ""),
                "object": self.pq.object_name(q), "source": row["source_type"] + (f": {row['file']}" if row["file"] else ""),
                "reads": [self.pq.object_name(x) for x in r.refs], "reads_queries": list(r.refs),
                "feeds": [d for d in self.pq.downstream(q) if (self.table_rows.get(d) or {}).get("include")],
                "steps": r.steps, "translation": "Not translated" if r.unsupported else (
                    "No MV (loads as-is)" if r.passthrough else "Translated"),
                "untranslated": [f"{a}: {b}" for a, b in r.unsupported], "notes": list(r.notes),
                "file_steps": list(r.file_steps), "rows": row.get("rows"), "check": "", "check_detail": ""})
        return out

    def check_power_query(self, plan: dict) -> list[dict]:
        """Run every Power Query MV on the rows Power BI saved (DuckDB) and record Match / Differs per layer."""
        try:
            import duckdb  # noqa: F401
        except ImportError:
            plan["pq_checks_note"] = "not run: pip install duckdb to check every MV against the rows saved in the .pbix"
            return []
        from ..analyze.profile import load_frames
        from .verify import check_graph

        wanted = {x["query"] for x in plan.get("pq_lineage") or [] if x["mv"]}
        inputs = wanted | {r for q in wanted for r in self.pq.refs.get(q, [])}
        raw, _ = load_frames(self.model, inputs & set(self.tables))
        frames = {}
        for q, df in raw.items():
            if df is None:
                continue
            ren = {mc: sc for sc, mc, _ in self.pq.outputs.get(q, [])}
            frames[q] = df.rename(columns=ren)
        rows = check_graph(self.pq, frames, only=wanted)
        by = {r["query"]: r for r in rows}
        for x in plan["pq_lineage"]:
            c = by.get(x["query"])
            if c:
                x["check"] = c["status"]
                x["check_detail"] = c.get("detail", "")
                x["check_rows"] = f"{c['rows']:,} / {c['pbi_rows']:,}" if c.get("rows") is not None else ""
        for m in plan["mvs"]:
            c = by.get(m.get("m_query") or "")
            if c and m["kind"] == "Power Query MV":
                m["check"] = c["status"]
                m["check_detail"] = c.get("detail", "")
        plan["pq_checks"] = rows
        return rows

    def _result(self) -> dict:
        included = [t for t in self.table_rows.values() if t["include"]]
        source_map = []
        for name, t in self.table_rows.items():
            loaded = [c for (tt, c) in self.cols if tt == name and (tt, c) in self.used and not self.is_calc(tt, c)]
            skipped = [c for (tt, c) in self.cols if tt == name and (tt, c) not in self.used and not self.is_calc(tt, c)]
            if t.get("pq") and t["include"]:
                raw_types = {}
                for rc in t.get("raw_columns") or []:
                    mc = next((c for (tt, c), col in self.cols.items() if tt == name and
                               str(col.get("source_column") or c) == rc and not col.get("dax")), None)
                    raw_types[self.pq.raw_column(name, rc)] = self.ctype(name, mc) if mc else "string"
                source_map.append({
                    "pbi_table": name, "schema": self.names.schema, "incorta_table": t["physical"],
                    "reads_as": self.names.objects.get(name, ""), "kind": t["kind"], "data_source": t["source_type"],
                    "source": t["file"] or t["source_detail"], "dataset_sql": t["dataset_sql"], "load_filter": "",
                    "power_query_filters": "", "key": t["key"] if not t.get("pq_mv") else "",
                    "columns_to_load": ", ".join(raw_types), "columns_not_loaded": "",
                    "load_type": t["load_type"], "rows": t.get("rows"), "status": t["status"],
                    "notes": t["notes"] + ([t["reason"]] if t.get("reason") else []), "types": raw_types,
                    "pq_mv": t.get("pq_mv", ""), "refs": t.get("refs", []), "file_steps": t.get("file_steps", []),
                    "feeds": [d for d in self.pq.downstream(name) if (self.table_rows.get(d) or {}).get("include")]})
                continue
            source_map.append({
                "pbi_table": name, "schema": self.names.schema if t["include"] else "",
                "incorta_table": (self.names.raw.get(name) or self.names.objects.get(name, "")) if t["include"] else "",
                "reads_as": self.names.objects.get(name, "") if t["include"] else "", "kind": t["kind"],
                "data_source": t["source_type"], "source": t["file"] or t["source_detail"],
                "dataset_sql": t["dataset_sql"], "load_filter": t["load_filter"],
                "power_query_filters": "; ".join(t["load_filter_m"]), "key": t["key"],
                "columns_to_load": ", ".join(self.names.column(name, c) for c in loaded),
                "columns_not_loaded": ", ".join(skipped), "load_type": t["load_type"] if t["include"] else "",
                "rows": t.get("rows"), "status": t["status"], "notes": t["notes"] + ([t["reason"]] if t.get("reason") else []),
                "types": {self.names.column(name, c): self.ctype(name, c) for c in loaded}})
        needs = [m for m in self.measure_rows if m["status"] == NEEDS_DESIGN]
        summary = {
            "report": self.report_name, "schema": self.names.schema, "business_schema": self.names.bs,
            "dashboard": self.report_name, "tabs": [p.get("name") for p in self.report.get("pages", [])],
            "tables_loaded": len([t for t in included if not t["calc_table"] and (t.get("physical") or not t.get("pq"))]),
            "power_query_mvs": len([m for m in self.mvs if m["kind"] == "Power Query MV"]),
            "mvs": len(self.mvs), "joins": len([j for j in self.joins if j["status"] != NOT_NEEDED]),
            "formula_columns": len([f for f in self.formula_cols if f["home"] == "Physical formula column"]),
            "business_view_formulas": len([b for b in self.bs_rows if b["kind"].startswith("Formula")]),
            "insight_formulas": len([m for m in self.measure_rows if m.get("home", "").startswith("Insight formula")]),
            "measures": len(self.measure_rows), "measures_needing_design": len(needs),
            "insights": len(self.plans), "dashboard_filters": len(self.filters), "decisions": len(self.decisions),
            "manual_checks": len(self.checks),
            "excluded_tables": [f"{t['pbi_table']} ({t.get('reason', '')})" for t in self.table_rows.values() if not t["include"]],
            "fact_rows": max([t.get("rows") or 0 for t in included] or [0]),
        }
        summary["complexity"] = ("High" if needs or any(m["kind"].startswith("Table MV") for m in self.mvs)
                                 else "Medium" if self.mvs else "Low")
        return {"version": 1, "spec": "Power BI to Incorta build spec, rev 17 (pbi_insights/incorta/spec.md)",
                "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "summary": summary,
                "source_map": source_map, "joins": self.joins, "formula_columns": self.formula_cols, "mvs": self.mvs,
                "business_schema": self.bs_rows, "measures": self.measure_rows, "insights": self.insight_rows,
                "dashboard_filters": self.filters, "decisions": self.decisions, "manual_checks": self.checks,
                "pq_lineage": self._lineage(),
                "names": {"schema": self.names.schema, "business_schema": self.names.bs,
                          "objects": self.names.objects, "raw": self.names.raw}}


# ---------------------------------------------------------------------------------------------- utilities
def _param_value(code: str):
    """Value of a Power BI parameter query ("abc" meta [IsParameterQuery=true, ...]), else None."""
    m = re.match(r'\s*("(?:[^"]|"")*"|-?\d+(?:\.\d+)?|true|false)\s+meta\s*\[[^\]]*IsParameterQuery\s*=\s*true',
                 code or "", re.I | re.S)
    if not m:
        return None
    v = m.group(1)
    if v.startswith('"'):
        return v[1:-1].replace('""', '"')
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return float(v) if "." in v else int(v)


def _step_functions(code: str) -> list[str]:
    seen = []
    for fn in re.findall(r"\b(Table\.[A-Za-z]+|List\.Accumulate|Odbc\.Query|Sql\.Database|Excel\.Workbook|"
                         r"Csv\.Document|Folder\.Files)\s*\(", code or ""):
        if fn not in seen:
            seen.append(fn)
    return seen


def _untranslated_sql(name: str, r, code: str) -> str:
    lines = [f"-- [MANUAL CHECK REQUIRED] Power Query '{name}' could not be fully translated:"]
    lines += [f"--   step '{a}': {b}" for a, b in r.unsupported]
    lines += ["-- Write the MV SQL from the Power Query code below.", "-- Power Query (M):"]
    lines += ["--   " + ln for ln in (code or "").splitlines()]
    lines.append("SELECT 1 AS placeholder")
    return "\n".join(lines)


def _plain(o):
    """Dataclasses (Dim, ...) → dicts, so in-memory and JSON results read the same way."""
    import dataclasses
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        if type(o).__module__.endswith(".ir"):
            return o
        return {k: _plain(v) for k, v in dataclasses.asdict(o).items() if k != "ir"}
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(x) for x in o]
    return o


def _balanced(s: str) -> bool:
    depth, q = 0, False
    for ch in s:
        if ch == '"':
            q = not q
        elif not q:
            depth += ch == "("
            depth -= ch == ")"
            if depth < 0:
                return False
    return depth == 0


def _split_words(s: str, word: str) -> list[str]:
    parts, depth, q, start, i, low = [], 0, False, 0, 0, s.lower()
    while i < len(s):
        ch = s[i]
        if ch == '"':
            q = not q
        elif not q:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and low.startswith(word, i):
                parts.append(s[start:i])
                i += len(word)
                start = i
                continue
        i += 1
    parts.append(s[start:])
    return [p for p in parts if p.strip()]


def _tables_in_dax(dax: str, tables: dict) -> list[str]:
    found = set()
    for m in re.finditer(r"'([^']+)'\s*\[|\b([A-Za-z_][\w ]*?)\s*\[", dax or ""):
        name = (m.group(1) or m.group(2) or "").strip()
        if name in tables:
            found.add(name)
    return sorted(found)


def _grain_of_calc(name: str, planner: IncortaPlanner) -> str:
    dax = (planner.tables[name].get("calc_dax") or "").upper()
    if "CALENDAR" in dax:
        return "one row per day"
    if "GENERATESERIES" in dax:
        return "one row per series value"
    return "as the DAX table"


def _purpose(ir, tr) -> str:
    """Plain-English purpose of a formula column (lookups described as mappings)."""
    lks = [n for n in walk(ir) if isinstance(n, Lookup)]
    if not lks:
        return tr.explanation
    lk = lks[0]
    keys = ", ".join(f"{k.column} = {v.column if isinstance(v, Col) else 'a computed value'}" for k, v in lk.keys)
    text = f"maps to {lk.result.table}[{lk.result.column}] where {keys}"
    from ..analyze.ir import Case
    if isinstance(ir, Case) and len(ir.whens) == 1 and isinstance(ir.whens[0][1], Lit):
        text += f"; \"{ir.whens[0][1].value}\" when there is no match"
    return text


def _corr_text(ir) -> str:
    n = next(x for x in walk(ir) if isinstance(x, CorrAgg))
    keys = ", ".join(sorted({c.column for i, _ in n.keys for c in walk(i) if isinstance(c, Col)}))
    word = {"MEDIAN": "median", "SUM": "sum", "AVG": "average", "MIN": "minimum", "MAX": "maximum", "COUNT": "count"}
    return f"{word.get(n.func, n.func.lower())} over the rows of {n.table}" + (f" with the same {keys}" if keys else "")


def _auto_title(mnames, dims) -> str:
    if mnames and not dims:
        return ", ".join(mnames)
    if mnames:
        return f"{', '.join(mnames)} by {', '.join(d['column'] for d in dims)}"
    return ", ".join(d["column"] for d in dims)


def _filter_text(f) -> str:
    if isinstance(f, dict):
        return one_line(f.get("text") or f.get("condition") or f"{f.get('table')}[{f.get('field')}]", 160)
    return one_line(f, 160)


def _sort_text(s) -> str:
    if not s or s in ("Default", "-"):
        return s or ""
    out = []
    for part in str(s).split(","):
        part = part.strip()
        m = re.match(r"^(.*?)\s+(ASC|DESC)$", part, re.I)
        ref = parse_ref(m.group(1) if m else part)
        name = ref[1] if ref else (m.group(1) if m else part)
        out.append(f"{name} ({'descending' if m and m.group(2).upper() == 'DESC' else 'ascending'})")
    return ", ".join(out)


def _design_options(m: dict) -> str:
    dax = m["dax"].upper()
    if "TOPN" in dax:
        return ("(a) insight-level design: an aggregated table grouped by the ranked column with a Top N filter on the "
                "measure and a level-based total, so every dashboard filter still applies; (b) MV with a rank per month "
                "(fast, but ignores the other slicers); (c) Incorta SQL table with presentation variables")
    if "ALLSELECTED" in dax or "ALLEXCEPT" in dax:
        return "(a) level-based measure on the kept columns; (b) MV at the insight grain"
    if "USERELATIONSHIP" in dax:
        return "(a) alias of the date table with its own join; (b) MV"
    return "(a) hand-written formula reviewed by a developer; (b) MV at the insight grain; (c) leave out"


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
