"""Power Query (M) → Spark SQL, query by query, following the references between queries.

Each Power BI query becomes Incorta objects:
- the external source call (Odbc.Query / Sql.Database / a file or folder) → a physical table that loads it as-is
  (dataset SQL = the native query, or a data file);
- every step after the source, and every query built from other queries, → Spark SQL for an MV that reads the
  Incorta objects of its inputs (the raw table and/or the MVs of the queries it references).
The steps are evaluated in order on a relational value (Rel) and rendered as one CTE per materialized step.
Anything the translator cannot express is reported (step name, function) for a manual rewrite.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .mlang import MSyntaxError, parse_m, table_type_columns

log = logging.getLogger(__name__)

DIALECT = "spark"
SOURCE_FNS = {"Odbc.Query", "Odbc.DataSource", "Sql.Database", "Sql.Databases", "Oracle.Database",
              "PostgreSQL.Database", "MySQL.Database", "Snowflake.Databases", "GoogleBigQuery.Database",
              "Excel.Workbook", "Csv.Document", "Folder.Files", "Folder.Contents", "File.Contents",
              "Json.Document", "Xml.Tables", "SharePoint.Files", "SharePoint.Contents", "Web.Contents",
              "OData.Feed", "Salesforce.Data", "Dataflows.Contents"}
TYPE_SQL = {"text": "STRING", "number": "DOUBLE", "Int64.Type": "BIGINT", "Int32.Type": "INT", "Int16.Type": "INT",
            "Int8.Type": "INT", "Number.Type": "DOUBLE", "Decimal.Type": "DOUBLE", "Currency.Type": "DECIMAL(19,4)",
            "Percentage.Type": "DOUBLE", "Double.Type": "DOUBLE", "Single.Type": "DOUBLE", "date": "DATE",
            "datetime": "TIMESTAMP", "datetimezone": "TIMESTAMP", "logical": "BOOLEAN", "Text.Type": "STRING",
            "Date.Type": "DATE", "DateTime.Type": "TIMESTAMP", "Logical.Type": "BOOLEAN"}
TYPE_HINT = {"STRING": "text", "DOUBLE": "number", "BIGINT": "number", "INT": "number", "DECIMAL(19,4)": "number",
             "DATE": "date", "TIMESTAMP": "datetime", "BOOLEAN": "logical"}
MODEL_HINT = {"text": "text", "whole number": "number", "decimal number": "number", "fixed decimal number": "number",
              "int64": "number", "double": "number", "date": "date", "date/time": "datetime", "true/false": "logical"}


class Unsupported(Exception):
    pass


def q(name: str) -> exp.Identifier:
    simple = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))
    return exp.to_identifier(name, quoted=not simple)


def col(name: str, table: str | None = None) -> exp.Column:
    return exp.Column(this=q(name), table=exp.to_identifier(table) if table else None)


def lit(v) -> exp.Expression:
    if v is None:
        return exp.Null()
    if isinstance(v, bool):
        return exp.Boolean(this=v)
    if isinstance(v, (int, float)):
        return exp.Literal.number(v)
    return exp.Literal.string(str(v))


def sql_of(e: exp.Expression) -> str:
    return e.sql(dialect=DIALECT)


# ---------------------------------------------------------------------------------------------- values
@dataclass
class Source:
    kind: str                      # odbc | sql | file | folder
    connector: str
    target: str = ""               # DSN / server / file path
    database: str = ""
    native_sql: str = ""
    item: str = ""                 # sheet / table
    header: bool = False
    notes: list = field(default_factory=list)


@dataclass
class FileValue:
    """A file, folder listing or workbook before it becomes a table (headers / sheet chosen)."""
    source: Source
    nested: dict = field(default_factory=dict)      # listing column -> the table it holds (FileValue or Rel)
    meta: dict = field(default_factory=dict)        # other listing columns (file name, ...) -> constant or FILE_COL


@dataclass
class Nav:
    """A database before one of its tables is picked: Odbc.DataSource(...){[Name=..., Kind=...]}[Data]..."""
    connector: str
    target: str
    path: tuple = ()
    kind: str = ""
    rel: object = None

    def step(self, sel: dict) -> "Nav":
        names = [str(sel[k]) for k in ("Schema", "Item", "Name") if sel.get(k)]
        kind = str(sel.get("Kind") or ("Table" if sel.get("Item") or len(names) > 1 else "Database"))
        return Nav(self.connector, self.target, self.path + tuple(names), kind)


@dataclass
class ColList:
    """table[Column]: the list of a column's values."""
    rel: object
    name: str


class UnknownColumns:
    """Table.ColumnNames of a file: the names are only known when the file is read."""


FILE_COL = "__file_listing_column"
LISTING_COLUMNS = ("Content", "Name", "Extension", "Date accessed", "Date modified", "Date created", "Attributes",
                   "Folder Path")
WORKBOOK_COLUMNS = {"Name", "Data", "Item", "Kind", "Hidden"}
NAV_FNS = {"Odbc.DataSource", "Sql.Database", "Sql.Databases", "Oracle.Database", "PostgreSQL.Database",
           "MySQL.Database", "Snowflake.Databases"}
CLEANING_FNS = {"Text.Trim", "Text.TrimStart", "Text.TrimEnd", "Text.Upper", "Text.Lower", "Text.Proper", "Text.Clean",
                "Text.From", "Text.Replace", "Number.From", "Date.From", "DateTime.Date", "DateTime.From",
                "Character.FromNumber"}


@dataclass
class FuncRef:
    name: str


@dataclass
class Lambda:
    params: list
    body: object
    env: dict


@dataclass
class Enum:
    name: str


@dataclass
class TypeVal:
    text: str


@dataclass
class Rel:
    base: str                                   # FROM: CTE name or Incorta object
    cols: dict                                  # M column name -> expression over base
    types: dict = field(default_factory=dict)   # M column name -> text | number | date | datetime | logical
    where: list = field(default_factory=list)
    open: bool = False                          # file tables: unknown columns read from the base
    rename: dict = field(default_factory=dict)  # open rels: M name -> base column name
    join: dict | None = None                    # pending Table.NestedJoin
    distinct: bool = False
    grid: bool = False                          # file rows before the header row is promoted
    origin: dict = field(default_factory=dict)  # M column -> source column it was cleaned from (lineage)
    namer: object = None                        # open rels: source column -> Incorta column of the raw table
    seen: set | None = None                     # open rels: source columns read
    maps: dict = field(default_factory=dict)    # column -> chained whole-value replacements (base, {old: new}, expr)
    values: dict = field(default_factory=dict)  # column -> every value it can hold, when the steps fix them

    def column(self, name: str) -> exp.Expression:
        if name in self.cols:
            return self.cols[name].copy()
        if self.open:
            raw = self.rename.get(name, name)
            if self.seen is not None:
                self.seen[raw] = True
            return col(self.namer(raw) if self.namer else raw)
        raise Unsupported(f"column [{name}] does not exist at this step")

    def copy(self) -> "Rel":
        return Rel(self.base, dict(self.cols), dict(self.types), list(self.where), self.open, dict(self.rename),
                   dict(self.join) if self.join else None, self.distinct, self.grid, dict(self.origin), self.namer,
                   self.seen, dict(self.maps), dict(self.values))

    def src_of(self, name: str):
        """Source column a column comes from, when it is the same value cleaned (trim, type, rename)."""
        if name in self.origin:
            return self.origin[name]
        if self.open and name not in self.cols:
            return self.rename.get(name, name)
        return None


@dataclass
class QueryResult:
    query: str
    sources: list = field(default_factory=list)          # [Source]
    refs: list = field(default_factory=list)             # queries it reads
    sql: str = ""                                         # Spark SQL of the MV ("" = no MV needed)
    columns: list = field(default_factory=list)          # output M column names
    unsupported: list = field(default_factory=list)      # (step, reason)
    notes: list = field(default_factory=list)
    passthrough: bool = False                            # output = the raw source table, no MV
    steps: int = 0
    types: dict = field(default_factory=dict)            # output M column -> text | number | date | ...
    raw_columns: list = field(default_factory=list)      # columns of the raw source table (native SELECT / file header)
    lineage: dict = field(default_factory=dict)          # output M column -> raw source column (cleaned copy)
    file_steps: list = field(default_factory=list)       # file / sheet selection done before the data (folder, sheet)
    decisions: list = field(default_factory=list)        # (topic, question) for the decision log
    values: dict = field(default_factory=dict)           # output M column -> every value it can hold, when known
    helper: bool = False                                 # sample file / parameter query: no table, no MV


class QueryContext:
    """What the translator needs from the plan: Incorta names of other queries' outputs and raw tables."""

    def __init__(self, queries: dict, object_of, raw_of, column_of, out_columns, types_of=None, raw_column=None,
                 params=None, functions=None, ast_of=None, result_of=None, loaded=None):
        self.queries = queries                  # name -> M code
        self.functions = functions or {}        # function query -> M code
        self.ast_of = ast_of or (lambda qn: parse_m(queries[qn]))
        self.result_of = result_of or (lambda qn: None)
        self.loaded = loaded or (lambda qn: True)           # is the query loaded into the model?
        self.object_of = object_of              # query -> Incorta object with the query's output
        self.raw_of = raw_of                    # query -> Incorta physical table of its external source
        self.column_of = column_of              # (query, M column) -> Incorta column name
        self.out_columns = out_columns          # query -> output M columns (from the model), or None
        self.types_of = types_of or (lambda qn, c: None)
        self.raw_column = raw_column or (lambda qn, c: c)   # (query, source column) -> raw table column
        self.params = params or {}              # parameter query -> value


# ---------------------------------------------------------------------------------------------- translator
class QueryTranslator:
    def __init__(self, name: str, ctx: QueryContext, carry: dict | None = None):
        self.name = name
        self.ctx = ctx
        self.ctes: list[tuple[str, str, str]] = []        # (cte, comment, sql)
        self.res = QueryResult(name)
        self.step = ""
        self._n = 0
        self.carry = carry or {}                           # what the first pass learnt about a file source
        self.part_added = self.part_used = False          # union input number (__part) for Table.Distinct
        self.pending: list[str] = []                       # Power Query steps not yet written to a CTE
        self._sn: dict[str, str] = {}                      # Power Query column -> SQL name inside the MV
        self._taken: set[str] = set()
        for c in ctx.out_columns(name) or []:
            self.sn(c)
        self.promotes = 0                                  # header promotions of the raw file
        self.reshape: dict = {}                            # step -> (kept columns, attribute, value) of a file unpivot
        self.skipped_after_header = False                  # Table.Skip after a first header promotion

    # ---- entry
    def translate(self) -> QueryResult:
        res = self._run(None)
        known = None
        if self.seen and not res.unsupported:
            # file / SELECT * sources: the columns are only known from the steps that read them; run again with them
            known = list(self.seen)
            carry = {"promotes": self.promotes, "reshape": dict(self.reshape)}
            self.__init__(self.name, self.ctx, carry)
            res = self._run(known)
        if self.part_added and not self.part_used and not res.unsupported:
            # the union input number is only needed when a later Table.Distinct keeps the first row: drop it
            carry = dict(self.carry, promotes=self.promotes, reshape=dict(self.reshape), no_part=True)
            self.__init__(self.name, self.ctx, carry)
            res = self._run(known)
        return res

    def _run(self, known_raw):
        self.known_raw = known_raw
        self.seen: dict = {}
        self.reads_helper = False
        try:
            ast = self.ctx.ast_of(self.name)
        except MSyntaxError as ex:
            self.res.unsupported.append(("(whole query)", f"the M reader does not know this syntax yet: {ex}"))
            return self.res
        binds, body = (ast[1], ast[2]) if ast[0] == "let" else ([], ast)
        self.res.steps = len(binds)
        env: dict = {}
        try:
            for name, e in binds:
                self.step = name
                env[name] = self.ev(e, env)
            self.step = "(result)"
            out = self.ev(body, env)
            if isinstance(out, FileValue):
                if not self.ctx.loaded(self.name):
                    self.res.helper = True                 # a file's content (sample file, parameter): not a table
                    return self.res
                out = self.file_rel(out)
            if not isinstance(out, Rel):
                raise Unsupported("the query does not end in a table")
            if out.join:
                out = self.apply_join(out, expand=[])
            if not self.ctx.loaded(self.name) and self.reads_helper and not self.seen:
                self.res.helper = True                     # Power BI's "Transform Sample File": a preview only
                return self.res
            if self.skipped_after_header and self.promotes < 2:
                raise Unsupported("Table.Skip on loaded rows (row order)")
            if known_raw is None and out.open:             # the loaded columns no step names come from the source
                for c in self.ctx.out_columns(self.name) or []:
                    out.column(c)
            if known_raw is not None or not self.seen:
                self.finish(out)
        except Unsupported as ex:
            self.res.unsupported.append((self.step, str(ex)))
        except RecursionError:
            self.res.unsupported.append((self.step, "expression too deep"))
        except Exception as ex:  # noqa: BLE001 - an M shape the translator does not expect: flag, never crash
            log.exception("Power Query translation of %s failed at step %s", self.name, self.step)
            self.res.unsupported.append((self.step, f"not translated ({type(ex).__name__}: {ex})"))
        return self.res

    def finish(self, rel: Rel):
        wanted = self.ctx.out_columns(self.name) or list(rel.cols)
        flat = lambda x: re.sub(r"\s+", " ", x)  # noqa: E731
        for c in [c for c in wanted if c not in rel.cols]:       # the model stores "A #(lf)%" as "A %"
            same = [x for x in rel.cols if flat(x) == flat(c) and x not in wanted]
            if len(same) == 1:
                rel = rel.copy()
                for d in (rel.cols, rel.types, rel.origin, rel.values):
                    if same[0] in d:
                        d[c] = d[same[0]]
                rel.cols[c] = rel.cols.pop(same[0])
                self._sn[c] = self.sn(same[0])
        if not wanted:
            raise Unsupported("the columns are only known when the file is read, and no step names them")
        db = [x for x in self.res.sources if x.kind in ("odbc", "sql")]
        if len({(x.target, x.database, x.item or x.native_sql) for x in db}) > 1:
            raise Unsupported("the query reads " + str(len(db)) + " database tables: load each one in its own query "
                              "(name its columns with Table.SelectColumns) and join them here")
        for c in wanted:
            if c in rel.cols and _has_file_col(rel.cols[c]):
                raise Unsupported(f"[{c}] comes from the file name or folder: add it when the files are loaded")
        self.res.values = {c: v for c, v in rel.values.items() if c in wanted}
        self.res.columns = wanted
        self.res.lineage = {c: rel.src_of(c) for c in wanted if rel.src_of(c)}
        self.res.types = {c: rel.types.get(c) for c in wanted}
        raw = self._raw_name()
        bare = (not self.ctes and not rel.where and not rel.distinct and rel.base == raw and wanted
                and all(c in rel.cols and isinstance(rel.cols[c], exp.Column) and not rel.cols[c].table
                        and rel.cols[c].name == self.ctx.raw_column(self.name, c)
                        and self.ctx.column_of(self.name, c) == self.ctx.raw_column(self.name, c) for c in wanted))
        if bare:
            self.res.passthrough = True                          # the source table is the output: no MV
            return
        rel = self.materialize(rel, "final") if (rel.where or rel.distinct) else rel
        items = []
        for c in wanted:
            if c not in rel.cols:
                raise Unsupported(f"no step produces the output column [{c}] that the model loads")
            e = rel.column(c)
            name = self.ctx.column_of(self.name, c)
            items.append(e if isinstance(e, exp.Column) and e.name == name and not e.table else exp.alias_(e, q(name)))
        final = exp.select(*items).from_(rel.base)
        sql = final.sql(dialect=DIALECT, pretty=True)
        if self.ctes:
            sql = "WITH\n" + ",\n".join(
                f"{n} AS (\n  -- {comment}\n  " + body.replace("\n", "\n  ") + "\n)" for n, comment, body in self.ctes) \
                + "\n" + sql
        self.res.sql = sql

    def _raw_name(self) -> str:
        return self.ctx.raw_of(self.name)

    def sn(self, m: str) -> str:
        """SQL name of a Power Query column inside the MV: the Incorta name for output columns, else Incorta-safe."""
        if m.startswith("__"):
            return m
        if m not in self._sn:
            if m in (self.ctx.out_columns(self.name) or []):
                base = self.ctx.column_of(self.name, m)
            else:
                base = _ident(m)
            name, n = base, 2
            while name.lower() in self._taken:
                name, n = f"{base}_{n}", n + 1
            self._sn[m] = name
            self._taken.add(name.lower())
        return self._sn[m]

    def materialize(self, rel: Rel, label: str) -> Rel:
        if rel.open:                                   # first pass over a file: columns not known yet, no SQL kept
            return rel
        rel = self.apply_join(rel) if rel.join else rel
        if any(_has_file_col(e) for e in rel.cols.values()):       # listing columns nobody used so far
            rel = rel.copy()
            rel.cols = {n: e for n, e in rel.cols.items() if not _has_file_col(e)}
        self._n += 1
        sel = [e if (isinstance(e, exp.Column) and e.name == self.sn(n) and not e.table) else exp.alias_(e, q(self.sn(n)))
               for n, e in rel.cols.items()]
        s = exp.select(*sel).from_(rel.base)
        for w in rel.where:
            s = s.where(w)
        if rel.distinct:
            s = s.distinct()
        before = [x for x in self.pending if x != self.step]      # steps applied since the last CTE
        if before:
            steps = before
            self.pending = [self.step] if self.step in self.pending else []
        else:
            steps, self.pending = [self.step if label == "final" else label], []
        cte = cte_name(self._n, steps[0])
        self.ctes.append((cte, "Power Query step" + ("s: " if len(steps) > 1 else ": ") + ", ".join(steps),
                          s.sql(dialect=DIALECT, pretty=True)))
        return Rel(cte, {n: col(self.sn(n)) for n in rel.cols}, dict(rel.types), origin=dict(rel.origin),
                   values=dict(rel.values))

    def needs_materialize(self, rel: Rel, names) -> bool:
        if rel.join or rel.distinct:
            return True
        for n in names:
            e = rel.cols.get(n)
            if e is not None and not isinstance(e, (exp.Column, exp.Literal)):
                return True
        return False

    # ---- evaluation of M values
    def ev(self, n, env):
        k = n[0]
        if k == "str":
            return n[1]
        if k == "num":
            return n[1]
        if k == "null":
            return None
        if k == "bool":
            return n[1]
        if k == "list":
            out = []
            for x in n[1]:
                v = self.ev(x, env)
                out += v if x[0] == "range" else [v]
            return out
        if k == "record":
            return {name: self.ev(e, env) for name, e in n[1]}
        if k == "type":
            return TypeVal(n[1])
        if k == "each":
            return Lambda(["_"], n[1], dict(env))
        if k == "fn":
            return Lambda(n[1], n[2], dict(env))
        if k == "let":
            local = dict(env)
            for name, e in n[1]:
                local[name] = self.ev(e, local)
            return self.ev(n[2], local)
        if k == "id":
            name = n[1]
            if name in env:
                return env[name]
            if name in self.ctx.params:
                return self.ctx.params[name]
            if name in self.ctx.queries and name != self.name:
                ref = self.ctx.result_of(name)
                if ref is not None and ref.helper:
                    self.reads_helper = True
                    return FileValue(Source("file", "Power BI sample file", name))
                return self.query_ref(name)
            if name in self.ctx.functions:
                return self.function(name)
            if name.split(".")[0] in ("JoinKind", "Replacer", "Order", "QuoteStyle", "MissingField", "Comparer",
                                      "ExtraValues", "JoinAlgorithm", "RoundingMode", "Splitter", "Combiner",
                                      "Occurrence", "Culture", "GroupKind"):
                return Enum(name)
            if name.endswith(".Type"):
                return TypeVal(name)
            if "." not in name and not name.startswith("#"):
                raise Unsupported(f"'{name}' is not a step or query in this file (missing or renamed query)")
            return FuncRef(name)
        if k == "call":
            return self.call(n, env)
        if k == "item":
            target = self.ev(n[1], env)
            if isinstance(target, Nav):
                sel = self.ev(n[2], env)
                if not isinstance(sel, dict) or not (sel.get("Name") or sel.get("Item")):
                    raise Unsupported("database navigation that does not name the table")
                return target.step(sel)
            if isinstance(target, FileValue):
                sel = self.ev(n[2], env)
                if isinstance(sel, dict) and (sel.get("Item") or sel.get("Name")):
                    target.source.item = str(sel.get("Item") or sel.get("Name"))
                    self.file_step(f"{sel.get('Kind') or 'item'} {target.source.item}")
                elif isinstance(sel, (int, float)) and target.source.kind == "folder":
                    if not any("newest" in x for x in self.res.file_steps):
                        self.file_step("takes the first file of the list")
                return target
            if isinstance(target, Rel) and (target.grid or target.open) and self.known_raw is None:
                return target
            if isinstance(target, list) and isinstance(n[2], tuple) and n[2][0] == "num":
                return target[int(n[2][1])]
            raise Unsupported("row or item access {...} on a table")
        if k == "field":
            target = self.ev(n[1], env) if n[1] is not None else env.get("_")
            if isinstance(target, Nav):
                return self.nav_table(target) if target.kind in ("Table", "View") else target
            if isinstance(target, FileValue):
                if n[2] in target.nested:
                    return target.nested[n[2]]
                if n[2] == "Data":
                    return self.file_rel(target)
                return target
            if isinstance(target, dict):
                return target.get(n[2])
            if isinstance(target, Rel) and n[1] is not None:
                return ColList(target, n[2])
            raise Unsupported(f"field access [{n[2]}] outside a row")
        if k == "proj":
            target = self.ev(n[1], env) if n[1] is not None else env.get("_")
            if not isinstance(target, (FileValue, Rel)):
                raise Unsupported("column selection [[...]] outside a table")
            return self.select_columns(self.to_rel(target), n[2])
        if k == "coalesce":
            left = self.ev(n[1], env)
            return self.ev(n[2], env) if left is None else left
        if k == "if":
            if n[2][0] == "error" or n[3][0] == "error":       # a check that stops the refresh: the other branch is the data
                return self.ev(n[3] if n[2][0] == "error" else n[2], env)
            c = self.ev(n[1], env)
            if isinstance(c, (FileValue, Rel)) or c is None or isinstance(c, exp.Expression):
                return self.ev(n[2], env)          # file branching: take the first (e.g. .xlsx) branch
            return self.ev(n[2] if c else n[3], env)
        if k == "bin":
            l, r = self.ev(n[2], env), self.ev(n[3], env)
            if isinstance(l, FileValue) or isinstance(r, FileValue):
                return FileValue(l.source if isinstance(l, FileValue) else r.source)
            if isinstance(l, (int, float, str)) and isinstance(r, (int, float, str)):
                return _py_bin(n[1], l, r)
            raise Unsupported(f"operator {n[1]} on tables")
        if k == "try":
            try:
                return self.ev(n[1], env)
            except Unsupported:
                if n[2] is not None:
                    return self.ev(n[2], env)
                raise
        if k == "error":
            return None
        if k == "un":
            v = self.ev(n[2], env)
            if n[1] == "-" and isinstance(v, (int, float)):
                return -v
            if n[1] == "not" and isinstance(v, bool):
                return not v
            return v
        if k == "range":
            a, b = self.ev(n[1], env), self.ev(n[2], env)
            if isinstance(a, int) and isinstance(b, int):
                return list(range(a, b + 1))
            if isinstance(a, str) and isinstance(b, str) and len(a) == len(b) == 1:
                return [chr(x) for x in range(ord(a), ord(b) + 1)]
            raise Unsupported("list range {a..b} of computed values")
        if k == "trycatch":
            raise Unsupported("try ... catch that uses the error record")
        raise Unsupported(f"{k} expression")

    def function(self, name: str) -> Lambda:
        """A function query (e.g. Power BI's "Transform File")."""
        try:
            f = self.ev(parse_m(self.ctx.functions[name]), {})
        except MSyntaxError as ex:
            raise Unsupported(f"function {name}: the M reader does not know this syntax yet: {ex}") from None
        if not isinstance(f, Lambda):
            raise Unsupported(f"{name} is not a function")
        return f

    def nav_table(self, nav: Nav) -> Rel:
        """The table or view a navigation ends in: a physical table that loads it."""
        if nav.rel is None:
            kind = "odbc" if nav.connector == "Odbc.DataSource" else "sql"
            o, c = ("`", "`") if nav.connector == "MySQL.Database" else ('"', '"')
            parts = [x if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x) else o + x.replace(c, c * 2) + c for x in nav.path]
            src = Source(kind, nav.connector, nav.target, nav.path[0] if len(nav.path) > 1 else "", item=".".join(parts))
            self.res.sources.append(src)
            self.step and self.file_step(f"database {nav.kind.lower()} {src.item}")
            nav.rel = self.raw_rel(None)
            cols = [f"{o}{x.replace(c, c * 2)}{c} AS {self.ctx.raw_column(self.name, x)}" for x in self.known_raw or []]
            src.native_sql = "SELECT " + (", ".join(cols) or "*") + f" FROM {src.item}"
        return nav.rel.copy()

    def query_ref(self, name: str) -> Rel:
        if name not in self.res.refs:
            self.res.refs.append(name)
        cols = self.ctx.out_columns(name) or []
        if not cols:
            raise Unsupported(f"columns of query {name} are not known")
        rel = Rel(self.ctx.object_of(name), {c: col(self.ctx.column_of(name, c)) for c in cols})
        for c in cols:
            t = self.ctx.types_of(name, c)
            if t:
                rel.types[c] = t
        ref = self.ctx.result_of(name)
        rel.values = dict(ref.values) if ref is not None else {}
        return rel

    def raw_rel(self, columns, grid=False) -> Rel:
        """The query's raw source table (native query or file) as a relation."""
        namer = lambda c: self.ctx.raw_column(self.name, c)  # noqa: E731
        if grid:
            return Rel(self._raw_name(), {}, open=True, grid=True, namer=namer, seen=self.seen)
        if columns is None:
            if self.known_raw is None:
                return Rel(self._raw_name(), {}, open=True, grid=grid, namer=namer, seen=self.seen)
            columns = self.known_raw
        self.res.raw_columns = list(columns)
        rel = Rel(self._raw_name(), {c: col(namer(c)) for c in columns}, origin={c: c for c in columns})
        for c in columns:
            t = self.ctx.types_of(self.name, c)
            if t:
                rel.types[c] = t
        return rel

    def file_rel(self, fv: FileValue) -> Rel:
        if fv.source not in self.res.sources:
            self.res.sources.append(fv.source)
        return self.raw_rel(None, grid=not fv.source.header)

    def file_step(self, text: str, fv=None):
        if text not in self.res.file_steps:
            self.res.file_steps.append(text)

    # ---- calls
    def call(self, n, env):
        fn = n[1]
        name = fn[1] if fn[0] == "id" else None
        if name and name in env and isinstance(env[name], Lambda):
            lam = env[name]
            args = [self.ev(a, env) for a in n[2]]
            local = dict(lam.env)
            local.update(zip(lam.params, args))
            return self.ev(lam.body, local)
        if name is None:
            raise Unsupported("call of a computed function")
        if name in self.ctx.functions:
            lam = self.function(name)
            return self.ev(lam.body, dict(lam.env, **dict(zip(lam.params, [self.ev(a, env) for a in n[2]]))))
        if name == "#table":
            return self.table_literal(n[2], env)
        if name in SOURCE_FNS:
            return self.source_call(name, n[2], env)
        handler = getattr(self, "t_" + name.replace(".", "_"), None)
        if handler is not None:
            return handler(n[2], env)
        args = [self.ev(a, env) for a in n[2]]
        if any(isinstance(a, FileValue) for a in args):
            return next(a for a in args if isinstance(a, FileValue))
        if any(isinstance(a, Rel) for a in args):
            raise Unsupported(f"{name} is not translated")
        if name.startswith(("Text.", "Number.", "Date.", "List.", "DateTime.", "Duration.")):
            return _py_scalar(name, args)
        if name in ("#date", "#datetime", "#duration", "#time"):
            return args
        raise Unsupported(f"{name} is not translated")

    def source_call(self, name, args_ast, env):
        def arg(i, default=None):
            return self.ev(args_ast[i], env) if len(args_ast) > i else default

        if name == "Odbc.Query":
            dsn = re.sub(r"(?i)^dsn=", "", str(arg(0)))
            src = Source("odbc", "Odbc.Query", dsn, native_sql=str(arg(1)))
            return self.sql_source(src)
        if name in ("Sql.Database", "Oracle.Database", "PostgreSQL.Database", "MySQL.Database"):
            opts = arg(2) if len(args_ast) > 2 else {}
            opts = opts if isinstance(opts, dict) else {}
            src = Source("sql", name, str(arg(0)), str(arg(1) or ""), native_sql=str(opts.get("Query") or ""))
            if src.native_sql:
                return self.sql_source(src)
            return Nav(name, src.target, (src.database,) if src.database and name == "Sql.Database" else ())
        if name in NAV_FNS:
            return Nav(name, re.sub(r"(?i)^dsn=", "", str(arg(0) or "")))
        if name == "File.Contents":
            return Source("file", "File.Contents", str(arg(0)))
        if name in ("Excel.Workbook", "Csv.Document", "Json.Document", "Xml.Tables"):
            inner = self.ev(args_ast[0], env)
            if isinstance(inner, FileValue):
                src = inner.source
                src.connector = name
            elif isinstance(inner, Source):
                src = Source("file", name, inner.target)
            else:
                src = Source("file", name, str(inner or ""))
            if name == "Excel.Workbook":
                src.header = bool(arg(1, False))
                self.file_step("Excel workbook" + (" (first row as headers)" if src.header else ""))
            if name == "Csv.Document":
                opts = arg(1, {})
                if isinstance(opts, dict) and opts.get("Delimiter"):
                    self.file_step(f"CSV, delimiter {opts.get('Delimiter')!r}")
            if src not in self.res.sources:
                self.res.sources.append(src)
            return FileValue(src)
        if name in ("Folder.Files", "Folder.Contents", "SharePoint.Files", "SharePoint.Contents"):
            src = Source("folder", name, str(arg(0)))
            self.res.sources.append(src)
            self.file_step(f"files in folder {src.target}")
            return FileValue(src, meta={c: FILE_COL for c in LISTING_COLUMNS})
        raise Unsupported(f"source {name}")

    def sql_source(self, src: Source) -> Rel:
        self.res.sources.append(src)
        cols = _select_columns(src.native_sql)
        return self.raw_rel(cols or None)
    # ---- row expressions
    def row(self, node, rel: Rel, env: dict) -> exp.Expression:
        k = node[0]
        if k == "field":
            if node[1] is None or node[1] == ("id", "_"):
                return rel.column(node[2])
            target = node[1]
            if target[0] == "id" and isinstance(env.get(target[1]), dict):
                return lit(env[target[1]].get(node[2]))
            raise Unsupported(f"field access [{node[2]}] on another row")
        if k == "id":
            name = node[1]
            if name == "_" and "_" in env and isinstance(env["_"], exp.Expression):
                return env["_"].copy()
            if name in env:
                v = env[name]
                if isinstance(v, exp.Expression):
                    return v.copy()
                if isinstance(v, (str, int, float, bool)) or v is None:
                    return lit(v)
                raise Unsupported(f"{name} used as a value")
            raise Unsupported(f"{name} used as a value")
        if k in ("str", "num", "null", "bool"):
            return lit(self.ev(node, env))
        if k == "let":
            local = dict(env)
            for name, e in node[1]:
                local[name] = self.row(e, rel, local)
            return self.row(node[2], rel, local)
        if k == "if":
            c = self.cond(node[1], rel, env)
            t, e = self.row(node[2], rel, env), self.row(node[3], rel, env)
            kt, ke = self.kind_of(node[2], t, rel, env), self.kind_of(node[3], e, rel, env)
            if {kt, ke} == {"text", "number"}:              # Power Query keeps both types; Incorta needs one: text
                t = num_to_text(t) if kt == "number" else t
                e = num_to_text(e) if ke == "number" else e
                self.note_once("a column mixes numbers and text in Power Query (Power BI stores it as text): it is text "
                               "in the MV, numbers written without trailing zeros")
                if not any(d[0] == "number or text column" for d in self.res.decisions):
                    self.res.decisions.append(("number or text column", f"Step '{self.step}' returns a number or a text "
                                               f"marker (e.g. '-') in one column. The MV keeps Power BI's behaviour (text). "
                                               f"Recommended for Incorta: a DOUBLE column with NULL for the marker, "
                                               f"formatted in the insight, so it can be summed and filtered as a number."))
            if not _maybe_null(c) or isinstance(e, exp.Null):
                return exp.Case(ifs=[exp.If(this=c, true=t)], default=e)
            # Power Query raises an error when the condition is null (null < 0): Power BI loads the cell as blank
            self.note_once("if ... then ... else: when the condition cannot be decided (a null value is compared), Power "
                           "Query raises an error that Power BI loads as blank; the MV gives NULL")
            return exp.Case(ifs=[exp.If(this=c, true=t), exp.If(this=exp.Not(this=exp.Paren(this=c.copy())), true=e)])
        if k == "try":
            inner = self.row(node[1], rel, env)
            other = self.row(node[2], rel, env) if node[2] is not None else exp.Null()
            if isinstance(other, exp.Null) or not _can_error(inner):
                return inner                               # Spark gives NULL where Power Query raises an error
            self.note_once("try ... otherwise: the fallback is used where the value is NULL (Spark returns NULL "
                           "instead of a conversion error)")
            if _infer(inner, rel) == "number" and isinstance(other, exp.Literal) and other.is_string:
                inner = num_to_text(inner)
            return exp.Coalesce(this=inner, expressions=[other])
        if k == "un":
            x = self.row(node[2], rel, env)
            if node[1] == "not":
                return exp.Not(this=exp.Paren(this=x))
            return exp.Neg(this=x) if node[1] == "-" else x
        if k == "bin":
            return self.binop(node, rel, env)
        if k == "call":
            return self.row_call(node, rel, env)
        if k == "coalesce":
            return exp.Coalesce(this=self.row(node[1], rel, env), expressions=[self.row(node[2], rel, env)])
        if k == "error":
            self.note_once("error ...: Power Query raises an error (Power BI loads the cell as blank); the MV gives NULL")
            return exp.Null()
        if k == "trycatch":
            raise Unsupported("try ... catch that uses the error record")
        if k == "list":
            raise Unsupported("list value in a row expression")
        raise Unsupported(f"{k} in a row expression")

    def cond(self, node, rel, env) -> exp.Expression:
        return self.row(node, rel, env)

    def binop(self, node, rel, env):
        op, a, b = node[1], node[2], node[3]
        if op in ("is", "as"):
            x = self.row(a, rel, env)
            t = b[1] if b[0] == "type" else ""
            if op == "is" and t.startswith("number"):
                self.note_once("Value.Is / is number: approximated as 'not null'")
                return exp.Not(this=exp.Is(this=x, expression=exp.Null()))
            return x
        if op in ("=", "<>") and (a[0] == "null" or b[0] == "null"):
            x = self.row(b if a[0] == "null" else a, rel, env)
            e = exp.Is(this=x, expression=exp.Null())
            return e if op == "=" else exp.Not(this=e)
        l, r = self.row(a, rel, env), self.row(b, rel, env)
        if op in ("=", "<>"):                               # Power Query: null = x is false, null <> x is true
            ln, rn = self.kind_of(a, l, rel, env), self.kind_of(b, r, rel, env)
            if {ln, rn} == {"text", "number"}:
                l = exp.TryCast(this=l, to=exp.DataType.build("DOUBLE")) if ln == "text" else l
                r = exp.TryCast(this=r, to=exp.DataType.build("DOUBLE")) if rn == "text" else r
            if _nullable(l) or _nullable(r):
                e = exp.NullSafeEQ(this=l, expression=r)
                return e if op == "=" else exp.Not(this=exp.Paren(this=e))
        if op in ("+", "-", "*", "/"):
            l = exp.TryCast(this=l, to=exp.DataType.build("DOUBLE")) if self.kind_of(a, l, rel, env) in ("text", "any") else l
            r = exp.TryCast(this=r, to=exp.DataType.build("DOUBLE")) if self.kind_of(b, r, rel, env) in ("text", "any") else r
        cls = {"and": exp.And, "or": exp.Or, "=": exp.EQ, "<>": exp.NEQ, "<": exp.LT, ">": exp.GT, "<=": exp.LTE,
               ">=": exp.GTE, "+": exp.Add, "-": exp.Sub, "*": exp.Mul, "/": exp.Div}.get(op)
        if op == "&":
            return exp.Concat(expressions=[l, r], safe=True, coalesce=False)
        if cls is None:
            raise Unsupported(f"operator {op}")
        if op in ("and", "or"):
            return cls(this=exp.Paren(this=l), expression=exp.Paren(this=r))
        if op in ("+", "-", "*", "/"):
            return exp.Paren(this=cls(this=l, expression=r))
        return cls(this=l, expression=r)

    def row_call(self, node, rel, env):
        fn = node[1]
        if fn[0] != "id":
            raise Unsupported("computed function call")
        name, args = fn[1], node[2]
        if name in env and isinstance(env[name], Lambda):
            lam = env[name]
            local = dict(lam.env)
            local.update({p: self.row(a, rel, env) for p, a in zip(lam.params, args)})
            return self.row(lam.body, rel, local)
        A = lambda i: self.row(args[i], rel, env)  # noqa: E731
        if name in ("DateTime.LocalNow", "DateTime.FixedLocalNow", "DateTimeZone.LocalNow", "DateTimeZone.UtcNow",
                    "DateTime.UtcNow"):
            self.note_once("current date/time: the MV takes it at load time, so reload it daily")
            return exp.CurrentTimestamp()
        if name in ("DateTime.Date", "Date.From"):
            if args and args[0][0] == "call" and args[0][1][1] in ("DateTime.LocalNow", "DateTime.FixedLocalNow"):
                self.note_once("current date/time: the MV takes it at load time, so reload it daily")
                return exp.CurrentDate()
            return exp.Cast(this=A(0), to=exp.DataType.build("DATE"))
        if name == "Text.Clean":
            return exp.Anonymous(this="regexp_replace", expressions=[
                exp.Cast(this=A(0), to=exp.DataType.build("STRING")), lit(r"[\x00-\x1F\x7F-\x9F]"), lit("")])
        if name == "Character.FromNumber":
            return lit(self.t_Character_FromNumber(args, env))
        if name == "Text.From":
            x = A(0)
            if (self._hint(args[0], rel) == "number" or _infer(x, rel) == "number"):
                return num_to_text(x)
            return exp.Cast(this=x, to=exp.DataType.build("STRING"))
        if name == "Text.Trim":
            return exp.Trim(this=A(0))
        if name == "Text.TrimStart":
            return exp.Trim(this=A(0), position="LEADING")
        if name == "Text.TrimEnd":
            return exp.Trim(this=A(0), position="TRAILING")
        if name == "Text.Upper":
            return exp.Upper(this=A(0))
        if name == "Text.Lower":
            return exp.Lower(this=A(0))
        if name == "Text.Proper":
            return exp.Initcap(this=A(0))
        if name == "Text.Length":
            return exp.Length(this=A(0))
        if name == "Text.Start":
            return exp.Anonymous(this="LEFT", expressions=[A(0), A(1)])
        if name == "Text.End":
            return exp.Anonymous(this="RIGHT", expressions=[A(0), A(1)])
        if name in ("Text.Middle", "Text.Range"):
            start = exp.Paren(this=exp.Add(this=A(1), expression=lit(1)))
            ex = [A(0), start] + ([A(2)] if len(args) > 2 else [])
            return exp.Substring(this=ex[0], start=ex[1], length=ex[2] if len(ex) > 2 else None)
        if name == "Text.Contains":
            return exp.GT(this=exp.StrPosition(this=A(0), substr=A(1)), expression=lit(0))
        if name == "Text.StartsWith":
            return exp.EQ(this=exp.Anonymous(this="LEFT", expressions=[A(0), exp.Length(this=A(1))]), expression=A(1))
        if name == "Text.EndsWith":
            return exp.EQ(this=exp.Anonymous(this="RIGHT", expressions=[A(0), exp.Length(this=A(1))]), expression=A(1))
        if name == "Text.Replace":
            return exp.Anonymous(this="REPLACE", expressions=[A(0), A(1), A(2)])
        if name == "Text.PadStart":
            return exp.Pad(this=A(0), expression=A(1), fill_pattern=A(2) if len(args) > 2 else lit(" "), is_left=True)
        if name == "Text.PadEnd":
            return exp.Pad(this=A(0), expression=A(1), fill_pattern=A(2) if len(args) > 2 else lit(" "), is_left=False)
        if name == "Text.BeforeDelimiter":
            return exp.Anonymous(this="SUBSTRING_INDEX", expressions=[A(0), A(1), lit(1)])
        if name == "Text.Combine":
            if args[0][0] != "list":
                raise Unsupported("Text.Combine of a computed list")
            parts = [self.row(x, rel, env) for x in args[0][1]]
            sep = A(1) if len(args) > 1 else lit("")
            return exp.ConcatWs(expressions=[sep] + parts)
        if name in ("Number.From", "Number.FromText", "Decimal.From", "Double.From", "Currency.From"):
            return exp.Cast(this=A(0), to=exp.DataType.build("DOUBLE"))
        if name in ("Int64.From", "Int32.From"):
            return exp.Cast(this=A(0), to=exp.DataType.build("BIGINT"))
        if name == "Number.Round":
            self.note_once("Number.Round rounds half to even: BROUND")
            return exp.Anonymous(this="BROUND", expressions=[A(0)] + ([A(1)] if len(args) > 1 else []))
        if name == "Number.RoundDown":
            return exp.Floor(this=A(0))
        if name == "Number.RoundUp":
            return exp.Ceil(this=A(0))
        if name == "Number.Abs":
            return exp.Abs(this=A(0))
        if name == "Number.Mod":
            return exp.Mod(this=A(0), expression=A(1))
        if name == "Number.IsNaN":
            return exp.Anonymous(this="ISNAN", expressions=[A(0)])
        if name == "Value.Is":
            t = args[1][1] if args[1][0] == "type" else (args[1][1] if args[1][0] == "id" else "")
            if "number" in str(t).lower() or str(t) in ("Int64.Type", "Int32.Type", "Int16.Type", "Int8.Type",
                                                         "Currency.Type", "Decimal.Type", "Double.Type", "Percentage.Type"):
                self.note_once("Value.Is / is number: approximated as 'not null'")
                return exp.Not(this=exp.Is(this=A(0), expression=exp.Null()))
            raise Unsupported(f"Value.Is(..., {t})")
        if name in ("Date.FromText", "DateTime.FromText"):
            opts = self.ev(args[1], env) if len(args) > 1 else None
            fmts = _date_formats(opts, name == "DateTime.FromText")
            fn = "TO_DATE" if name == "Date.FromText" else "TO_TIMESTAMP"
            parts = [exp.Anonymous(this=fn, expressions=[A(0), lit(f)]) for f in fmts]
            self.note_once("Date.FromText: the text is parsed with " + " or ".join(repr(f) for f in fmts)
                           + " (Spark returns NULL where Power Query raises an error)")
            return parts[0] if len(parts) == 1 else exp.Coalesce(this=parts[0], expressions=parts[1:])
        if name in ("Date.Year", "Date.Month", "Date.Day", "Date.QuarterOfYear"):
            cls = {"Date.Year": exp.Year, "Date.Month": exp.Month, "Date.Day": exp.Day,
                   "Date.QuarterOfYear": exp.Quarter}[name]
            return cls(this=A(0))
        if name == "Date.StartOfMonth":
            return exp.Cast(this=exp.DateTrunc(this=A(0), unit=exp.var("MONTH")), to=exp.DataType.build("DATE"))
        if name == "Date.EndOfMonth":
            return exp.LastDay(this=A(0))
        if name == "Date.AddDays":
            return exp.DateAdd(this=A(0), expression=A(1), unit=exp.var("DAY"))
        if name == "Date.AddMonths":
            return exp.Anonymous(this="ADD_MONTHS", expressions=[A(0), A(1)])
        if name == "Date.ToText":
            fmt = self.ev(args[1], env) if len(args) > 1 else "yyyy-MM-dd"
            if isinstance(fmt, dict):
                fmt = fmt.get("Format", "yyyy-MM-dd")
            return exp.Anonymous(this="DATE_FORMAT", expressions=[A(0), lit(str(fmt))])
        if name == "Duration.Days":
            a = args[0]
            if a[0] == "bin" and a[1] == "-":
                return exp.DateDiff(this=self.row(a[2], rel, env), expression=self.row(a[3], rel, env),
                                    unit=exp.var("DAY"))
            raise Unsupported("Duration.Days of a duration value")
        if name == "List.Contains":
            if args[0][0] != "list":
                raise Unsupported("List.Contains of a computed list")
            x, vals = A(1), []
            for item in args[0][1]:
                if item[0] == "call" and item[1] == ("id", "#date") and all(y[0] == "num" for y in item[2]):
                    vals.append(self.row(item, rel, env))
                    continue
                v = self.ev(item, env)
                vals += v if item[0] == "range" else [v]
            if not vals or not all(v is None or isinstance(v, (str, int, float, bool, exp.Expression)) for v in vals):
                raise Unsupported("List.Contains of a computed list")
            inside = [v if isinstance(v, exp.Expression) else lit(v) for v in vals if v is not None]
            e = exp.In(this=x, expressions=inside) if inside else exp.Boolean(this=False)
            if inside and not isinstance(x, exp.Literal):     # List.Contains(list, null) is false, not null
                e = exp.Coalesce(this=e, expressions=[exp.Boolean(this=False)])
            if None in vals:
                e = exp.Paren(this=exp.Or(this=exp.Is(this=x.copy(), expression=exp.Null()), expression=e))
            return e
        if name in ("List.Max", "List.Min", "List.Sum") and args[0][0] == "list":
            items = [self.row(x, rel, env) for x in args[0][1]]
            if name == "List.Sum":
                out = items[0]
                for x in items[1:]:
                    out = exp.Add(this=out, expression=x)
                return exp.Paren(this=out)
            return (exp.Greatest if name == "List.Max" else exp.Least)(this=items[0], expressions=items[1:])
        if name == "List.IsEmpty" and args and args[0][0] == "call" and args[0][1] == ("id", "List.RemoveMatchingItems"):
            inner = args[0][2]
            if inner[0][0] == "call" and inner[0][1] == ("id", "Record.FieldValues") and inner[1][0] == "list":
                if rel.open:
                    raise Unsupported("blank-row filter on a file whose columns are not known")
                vals = [self.ev(x, env) for x in inner[1][1]]
                texts = [v for v in vals if v is not None]
                conds = []
                for c in [c for c in rel.cols if not c.startswith("__")]:
                    x = rel.column(c)
                    parts = []
                    if None in vals:
                        parts.append(exp.Is(this=x.copy(), expression=exp.Null()))
                    if texts:
                        parts.append(exp.In(this=exp.Cast(this=x.copy(), to=exp.DataType.build("STRING")),
                                            expressions=[lit(t) for t in texts]))
                    one = parts[0]
                    for p_ in parts[1:]:
                        one = exp.Or(this=one, expression=p_)
                    conds.append(exp.Paren(this=one))
                out = conds[0]
                for c_ in conds[1:]:
                    out = exp.And(this=out, expression=c_)
                return exp.Paren(this=out)
            raise Unsupported("List.IsEmpty of a computed list")
        if name == "List.NonNullCount" and args and args[0][0] == "call" and args[0][1] == ("id", "Record.FieldValues"):
            if rel.open:
                return lit(1)                                # first pass over a file: columns not known yet
            parts = [exp.Case(ifs=[exp.If(this=exp.Not(this=exp.Is(this=rel.column(c), expression=exp.Null())),
                                          true=lit(1))], default=lit(0)) for c in rel.cols if not c.startswith("__")]
            out = parts[0]
            for p_ in parts[1:]:
                out = exp.Add(this=out, expression=p_)
            return exp.Paren(this=out)
        if name == "#date":
            parts = [A(0), A(1), A(2)]
            return exp.Anonymous(this="MAKE_DATE", expressions=[
                x if isinstance(x, (exp.Year, exp.Month, exp.Day, exp.Literal))
                else exp.Cast(this=x, to=exp.DataType.build("INT")) for x in parts])
        if name == "List.Count":
            days = _weekday_count(node)
            if days is None:
                raise Unsupported("List.Count of a computed list")
            return self.weekdays(self.row(days[0], rel, env), self.row(days[1], rel, env))
        if name == "Record.Field":
            rec = env.get(args[0][1]) if args[0][0] == "id" else (self.ev(args[0], env) if args[0][0] == "record" else None)
            if isinstance(rec, dict):                         # a lookup record: [Jan = 1, Feb = 2, ...]
                if not all(v is None or isinstance(v, (str, int, float, bool)) for v in rec.values()):
                    raise Unsupported("Record.Field of a record with computed values")
                x = A(1)
                return exp.Case(ifs=[exp.If(this=exp.EQ(this=x.copy(), expression=lit(k_)), true=lit(v))
                                     for k_, v in rec.items()])
            fname = self.ev(args[1], env)
            return rel.column(str(fname))
        raise Unsupported(f"{name} in a row expression")

    def _hint(self, node, rel: Rel):
        if node[0] == "field" and (node[1] is None or node[1] == ("id", "_")):
            return rel.types.get(node[2])
        if node[0] == "num":
            return "number"
        return None

    def kind_of(self, node, e, rel: Rel, env) -> str | None:
        if node[0] == "str":
            return "text"
        if node[0] == "num":
            return "number"
        if node[0] == "field" and (node[1] is None or node[1] == ("id", "_")):
            return rel.types.get(node[2])
        if node[0] == "id" and isinstance(env.get(node[1]), exp.Expression):
            return _infer(env[node[1]], rel)
        return _infer(e, rel)

    def weekdays(self, start: exp.Expression, end: exp.Expression) -> exp.Expression:
        """Monday-to-Friday days from start to end, both included (day 0 = Monday 1900-01-01)."""
        def before(e, plus):
            k = exp.DateDiff(this=e, expression=exp.Cast(this=lit("1900-01-01"), to=exp.DataType.build("DATE")),
                             unit=exp.var("DAY"))
            k = exp.Paren(this=exp.Add(this=k, expression=lit(1))) if plus else k
            return exp.Add(this=exp.Mul(this=lit(5), expression=exp.Floor(this=exp.Div(this=k, expression=lit(7)))),
                           expression=exp.Least(this=exp.Mod(this=k.copy(), expression=lit(7)), expressions=[lit(5)]))
        self.note_once("weekdays between two dates: counted Monday to Friday, both dates included, as List.Dates does")
        n = exp.Sub(this=exp.Paren(this=before(end.copy(), True)), expression=exp.Paren(this=before(start.copy(), False)))
        late = exp.LT(this=exp.DateDiff(this=end.copy(), expression=start.copy(), unit=exp.var("DAY")), expression=lit(-1))
        return exp.Case(ifs=[exp.If(this=late, true=exp.Null())],
                        default=exp.Cast(this=n, to=exp.DataType.build("BIGINT")))

    def t_Character_FromNumber(self, a, env):
        n = self.ev(a[0], env)
        if not isinstance(n, int):
            raise Unsupported("Character.FromNumber of a computed number")
        return chr(n)

    def note_once(self, text: str):
        if text not in self.res.notes:
            self.res.notes.append(text)

    # ---- function arguments that are functions
    def apply_fn(self, f, x: exp.Expression, rel: Rel, env) -> exp.Expression:
        if isinstance(f, FuncRef):
            return self.row_call(("call", ("id", f.name), [("id", "__x")]), rel, dict(env, __x=x))
        if isinstance(f, Lambda):
            local = dict(f.env)
            local[f.params[0] if f.params else "_"] = x
            return self.row(f.body, rel, local)
        raise Unsupported("unsupported column function")

    def as_lambda(self, node, env) -> Lambda:
        v = self.ev(node, env)
        if isinstance(v, Lambda):
            return v
        if isinstance(v, FuncRef):
            return Lambda(["_"], ("call", ("id", v.name), [("id", "_")]), dict(env))
        raise Unsupported("expected a function")

    def rel_arg(self, node, env) -> Rel:
        return self.to_rel(self.ev(node, env))

    def to_rel(self, v) -> Rel:
        if isinstance(v, FileValue):
            v = self.file_rel(v)
        if not isinstance(v, Rel):
            raise Unsupported("expected a table")
        if self.step and self.step not in self.pending:
            self.pending.append(self.step)
        if v.grid:
            self.file_step("read without a header row (columns Column1, Column2, ...)")
            return self.raw_rel(None)
        return v.copy()

    def select_columns(self, rel: Rel, names: list) -> Rel:
        for n in names:
            if rel.open and n not in rel.cols:
                rel.origin.setdefault(n, rel.rename.get(n, n))
        rel.cols = {n: rel.column(n) for n in names}
        rel.origin = {n: o for n, o in rel.origin.items() if n in rel.cols}
        rel.open = False
        return rel

    def table_literal(self, a, env) -> Rel:
        """#table(type table [A = text, ...] | {"A", ...}, {{value, ...}, ...})."""
        t = self.ev(a[0], env)
        if isinstance(t, TypeVal):
            cols = table_type_columns(t.text)
            if not cols:
                raise Unsupported("#table with a computed type")
        elif isinstance(t, list) and all(isinstance(x, str) for x in t):
            cols = [(x, "any") for x in t]
        else:
            raise Unsupported("#table with computed columns")
        if a[1][0] != "list" or any(r[0] != "list" or len(r[1]) != len(cols) for r in a[1][1]) or not a[1][1]:
            raise Unsupported("#table with computed rows")
        none, parts, types = Rel("", {}), [], {}
        for r in a[1][1]:
            items = []
            for (c, tt), v in zip(cols, r[1]):
                e = self.row(v, none, dict(env))
                st = TYPE_SQL.get(tt.replace("nullable ", ""))
                if st:
                    e = exp.Cast(this=e, to=exp.DataType.build(st))
                    types[c] = TYPE_HINT.get(st)
                items.append(exp.alias_(e, q(self.sn(c))))
            parts.append(exp.select(*items).sql(dialect=DIALECT, pretty=True))
        self._n += 1
        cte = cte_name(self._n, self.step)
        self.ctes.append((cte, f"Power Query step: {self.step} (#table)", "\nUNION ALL\n".join(parts)))
        self.pending = [x for x in self.pending if x != self.step]
        return Rel(cte, {c: col(self.sn(c)) for c, _ in cols}, types)

    # ---- steps on a file listing (one row per file or sheet)
    def file_add_column(self, v: FileValue, a, env) -> FileValue:
        cname, lam = self.ev(a[1], env), self.as_lambda(a[2], env)
        body = lam.body
        opens = body[0] == "call" and body[1][0] == "id" and (
            body[1][1] in SOURCE_FNS or body[1][1] in self.ctx.functions or body[1][1] == "Table.PromoteHeaders"
            or isinstance(env.get(body[1][1]), Lambda))
        if not opens:
            raise Unsupported(f"column [{cname}] is computed from the file name or properties: add it when the files "
                              f"are loaded")
        table = self.ev(body, dict(lam.env, _=v))
        if not isinstance(table, (FileValue, Rel)):
            raise Unsupported(f"column [{cname}] of the file list does not hold a table")
        self.file_step(f"opens every file of the list (step '{self.step}')")
        return FileValue(v.source, dict(v.nested, **{cname: table}), dict(v.meta))

    def file_expand(self, v: FileValue, newcol, names, alias):
        """Expands a table column of a file listing: workbook sheets (still a listing) or the data itself."""
        if not isinstance(names, UnknownColumns) and names and set(names) <= WORKBOOK_COLUMNS and not v.source.header:
            out = FileValue(v.source, {k: t for k, t in v.nested.items() if k != newcol}, dict(v.meta))
            for n, al in zip(names, alias):
                if n == "Data":
                    out.nested[al] = FileValue(v.source)
                else:
                    out.meta[al] = FILE_COL
            return out
        t = v.nested.get(newcol)
        self.file_step("expands the data of every file" + (
            "" if isinstance(names, UnknownColumns) else f" ({len(names)} columns)"))
        r = self.to_file_table(t if t is not None else v)
        if isinstance(names, UnknownColumns):
            names = alias = []
        if r.grid or r.open:
            r.rename.update({al: nm for nm, al in zip(names, alias)})
        for c, val in v.meta.items():                        # listing columns stay on every row of the data
            if c not in alias and c not in r.cols and (val != FILE_COL or names or c not in LISTING_COLUMNS):
                r.cols[c] = col(FILE_COL) if val == FILE_COL else lit(val)
        return r

    def to_file_table(self, t) -> Rel:
        return self.file_rel(t) if isinstance(t, FileValue) else t.copy()

    def raw_stage(self, v) -> bool:
        """Is v still the raw file (nothing but header / row selection so far)?"""
        return isinstance(v, Rel) and v.base == self._raw_name() and not self.ctes and (v.grid or v.open) \
            and any(x.kind in ("file", "folder") for x in self.res.sources)

    @staticmethod
    def is_file(v) -> bool:
        return isinstance(v, FileValue) or (isinstance(v, Rel) and v.grid)

    # ---- Table.* steps
    def t_Table_TransformColumns(self, a, env):
        v = self.ev(a[0], env)
        specs = a[1]
        items = specs[1] if specs[0] == "list" else []
        if items and items[0][0] != "list":
            items = [specs]
        if isinstance(v, FileValue) and items and all(self.ev(it[1][0], env) in v.nested for it in items):
            nested = dict(v.nested)
            for it in items:                               # a table column of the file list
                cname, lam = self.ev(it[1][0], env), self.as_lambda(it[1][1], env)
                nested[cname] = self.ev(lam.body, dict(lam.env, **{lam.params[0] if lam.params else "_": nested[cname]}))
                if not isinstance(nested[cname], (FileValue, Rel)):
                    raise Unsupported(f"column [{cname}] of the file list does not hold a table")
            return FileValue(v.source, nested, dict(v.meta))
        rel = self.to_rel(v)
        names = [self.ev(it[1][0], env) for it in items]
        if self.needs_materialize(rel, names):
            rel = self.materialize(rel, self.step)
        for it, cname in zip(items, names):
            parts = it[1]
            f = self.ev(parts[1], env)
            if rel.open and cname not in rel.cols:
                rel.origin.setdefault(cname, rel.rename.get(cname, cname))
            x = rel.column(cname)
            e = self.apply_fn(f, x, rel, env)
            rel.values.pop(cname, None)
            if isinstance(f, Lambda) and not _only_cleans(f.body):
                rel.origin.pop(cname, None)                # a new value, not the source value cleaned
            if len(parts) > 2:
                tv = self.ev(parts[2], env)
                t = TYPE_SQL.get(tv.text.replace("nullable ", "")) if isinstance(tv, TypeVal) else None
                if t and t != "STRING":
                    e = exp.Cast(this=e, to=exp.DataType.build(t))
                if t:
                    rel.types[cname] = TYPE_HINT.get(t)
            rel.cols[cname] = e
        return rel

    def t_Table_TransformColumnTypes(self, a, env):
        v = self.ev(a[0], env)
        if self.is_file(v):
            return v                                       # types of the raw grid, before the header row: no effect
        rel = self.to_rel(v)
        specs = a[1][1] if a[1][0] == "list" else []
        if specs and specs[0][0] != "list":
            specs = [a[1]]
        for sp in specs:
            cname = self.ev(sp[1][0], env)
            tv = self.ev(sp[1][1], env)
            t = TYPE_SQL.get(tv.text.replace("nullable ", "")) if isinstance(tv, TypeVal) else None
            if not t or tv.text == "any":
                continue
            if rel.open and cname not in rel.cols:
                rel.origin.setdefault(cname, rel.rename.get(cname, cname))
            x = rel.column(cname)
            cur = rel.types.get(cname)
            if t == "STRING" and cur == "number":
                x = num_to_text(x)
            elif TYPE_HINT.get(t) != cur or t not in ("STRING",):
                x = exp.Cast(this=x, to=exp.DataType.build(t))
            if t != "STRING" or not all(isinstance(y, str) for y in rel.values.get(cname, ())):
                rel.values.pop(cname, None)
            rel.cols[cname] = x
            rel.types[cname] = TYPE_HINT.get(t)
            if rel.open and cname not in rel.cols:
                pass
        return rel

    def t_Table_AddColumn(self, a, env):
        v = self.ev(a[0], env)
        if isinstance(v, FileValue):
            return self.file_add_column(v, a, env)
        rel = self.to_rel(v)
        cname = self.ev(a[1], env)
        f = self.as_lambda(a[2], env)
        used = _fields(f.body)
        if self.needs_materialize(rel, used):
            rel = self.materialize(rel, self.step)
        local = dict(f.env)
        e = self.row(f.body, rel, local)
        if len(a) > 3:
            tv = self.ev(a[3], env)
            t = TYPE_SQL.get(tv.text.replace("nullable ", "")) if isinstance(tv, TypeVal) else None
            if t:
                rel.types[cname] = TYPE_HINT.get(t)
                if t not in ("STRING",):
                    e = exp.Cast(this=e, to=exp.DataType.build(t))
        else:
            rel.types[cname] = _infer(e, rel)
        rel.cols[cname] = e
        rel.values.pop(cname, None)
        if f.body[0] == "str":
            rel.values[cname] = (f.body[1],)
        return rel

    def t_Table_DuplicateColumn(self, a, env):
        rel = self.rel_arg(a[0], env)
        src, dst = self.ev(a[1], env), self.ev(a[2], env)
        rel.cols[dst] = rel.column(src)
        if rel.src_of(src):
            rel.origin[dst] = rel.src_of(src)
        if src in rel.types:
            rel.types[dst] = rel.types[src]
        return rel

    def t_Table_RenameColumns(self, a, env):
        v = self.ev(a[0], env)
        pairs = self.ev(a[1], env)
        if pairs and not isinstance(pairs[0], list):
            pairs = [pairs]
        if isinstance(v, FileValue) and v.meta:
            m = {old: new for old, new in pairs}
            return FileValue(v.source, {m.get(k, k): t for k, t in v.nested.items()},
                             {m.get(k, k): t for k, t in v.meta.items()})
        rel = self.to_rel(v)
        new_cols = {}
        mapping = {old: new for old, new in pairs}
        missing_ok = len(a) > 2 and "Ignore" in repr(a[2])
        for old, new in pairs:
            if old not in rel.cols and rel.open:
                rel.origin.setdefault(old, rel.rename.get(old, old))
                rel.cols[old] = rel.column(old)
            elif old not in rel.cols and not missing_ok:
                raise Unsupported(f"rename of column [{old}], which does not exist at this step")
        for name, e in rel.cols.items():
            new_cols[mapping.get(name, name)] = e
        types, origin = {}, {}
        rel.values = {mapping.get(n, n): v for n, v in rel.values.items()}
        for name in rel.cols:
            if name in rel.types:
                types[mapping.get(name, name)] = rel.types[name]
            if name in rel.origin:
                origin[mapping.get(name, name)] = rel.origin[name]
        rel.cols, rel.types, rel.origin = new_cols, types, origin
        return rel

    def t_Table_SelectColumns(self, a, env):
        v = self.ev(a[0], env)
        names = self.ev(a[1], env)
        names = [names] if isinstance(names, str) else names
        if isinstance(v, FileValue) and v.meta:
            return FileValue(v.source, {k: t for k, t in v.nested.items() if k in names},
                             {k: t for k, t in v.meta.items() if k in names})
        return self.select_columns(self.to_rel(v), names)

    def t_Table_RemoveColumns(self, a, env):
        v = self.ev(a[0], env)
        names = self.ev(a[1], env)
        names = [names] if isinstance(names, str) else names
        if isinstance(v, FileValue) and v.meta:
            return FileValue(v.source, {k: t for k, t in v.nested.items() if k not in names},
                             {k: t for k, t in v.meta.items() if k not in names})
        if isinstance(v, Rel) and v.grid:
            return v                                       # columns of the raw grid, before the header row
        rel = self.to_rel(v)
        if rel.join and rel.join["newcol"] in names:
            rel = self.apply_join(rel, expand=[])
        for n in names:
            rel.cols.pop(n, None)
        return rel

    def t_Table_ReorderColumns(self, a, env):
        rel = self.rel_arg(a[0], env)
        names = [n for n in self.ev(a[1], env) if n in rel.cols]
        rel.cols = {**{n: rel.cols[n] for n in names}, **{n: e for n, e in rel.cols.items() if n not in names}}
        return rel

    def t_Table_ReplaceValue(self, a, env):
        rel = self.rel_arg(a[0], env)
        old, new, replacer = self.ev(a[1], env), self.ev(a[2], env), self.ev(a[3], env)
        names = self.ev(a[4], env)
        names = [names] if isinstance(names, str) else names
        rname = getattr(replacer, "name", "")
        text_mode = rname.endswith("ReplaceText")
        if not text_mode and not rname.endswith("ReplaceValue"):
            raise Unsupported(f"Table.ReplaceValue with replacer {rname or 'function'}")
        if isinstance(old, Rel) or isinstance(new, Rel) or (text_mode and (isinstance(old, Lambda) or isinstance(new, Lambda))):
            raise Unsupported("Table.ReplaceValue with computed values")
        if isinstance(old, Lambda) or isinstance(new, Lambda):
            return self.replace_per_row(rel, old, new, names)
        for n in names:
            if rel.open and n not in rel.cols:
                rel.origin.setdefault(n, rel.rename.get(n, n))
            cur = rel.column(n)
            rel.values.pop(n, None)
            if text_mode:
                if old in (None, ""):
                    continue
                if _size(cur) > 1500:
                    rel = self.materialize(rel, self.step)
                    cur = rel.column(n)
                rel.cols[n] = exp.Anonymous(this="REPLACE", expressions=[cur, lit(old), lit(new)])
                rel.maps.pop(n, None)
                self.note_once("Replacer.ReplaceText replaces the text inside values (REPLACE), as Power Query does")
                continue
            typed = lambda v: str(v) if (rel.types.get(n) == "text" and isinstance(v, (int, float))  # noqa: E731
                                         and not isinstance(v, bool)) else v
            o, w = typed(old), typed(new)                  # a text column keeps text: 0 -> '0'
            entry = rel.maps.get(n)
            if entry and n in rel.cols and rel.cols[n] is entry[2]:
                base, m = entry[0], dict(entry[1])
            else:
                if not isinstance(cur, (exp.Column, exp.Literal)):
                    rel = self.materialize(rel, self.step)
                    cur = rel.column(n)
                base, m = cur, {}
            m2 = {k: (w if _same(v, o) else v) for k, v in m.items()}
            if not any(_same(k, o) for k in m):
                m2[o] = w
            ifs = []
            for k, v in m2.items():
                cond = exp.Is(this=base.copy(), expression=exp.Null()) if k is None else \
                    exp.EQ(this=base.copy(), expression=lit(k))
                ifs.append(exp.If(this=cond, true=lit(v)))
            e = exp.Case(ifs=ifs, default=base.copy())
            rel.cols[n] = e
            rel.maps[n] = (base, m2, e)
        return rel

    def replace_per_row(self, rel: Rel, old, new, names) -> Rel:
        """Table.ReplaceValue(t, each old, each new, Replacer.ReplaceValue, names): per row, value = old -> new."""
        used = names + [c for v in (old, new) if isinstance(v, Lambda) for c in _fields(v.body)]
        for n in names:
            if rel.open and n not in rel.cols:
                rel.origin.setdefault(n, rel.rename.get(n, n))
        if self.needs_materialize(rel, used):
            rel = self.materialize(rel, self.step)
        before = rel.copy()                                # every column is replaced from the row as it was
        for n in names:
            x = before.column(n)
            o = self.row(old.body, before, dict(old.env)) if isinstance(old, Lambda) else lit(old)
            v = self.row(new.body, before, dict(new.env)) if isinstance(new, Lambda) else lit(new)
            rel.cols[n] = v if o == x else exp.Case(ifs=[exp.If(this=exp.NullSafeEQ(this=x.copy(), expression=o), true=v)],
                                                    default=x)
            rel.values.pop(n, None)
            rel.maps.pop(n, None)
            rel.origin.pop(n, None)
        return rel

    def t_Table_SelectRows(self, a, env):
        v = self.ev(a[0], env)
        if self.is_file(v):
            body = a[1][1] if a[1][0] == "each" else None
            m = _equals_field(body)
            if isinstance(v, FileValue) and m and m[0] in ("Name", "Item"):
                v.source.item = m[1]
                self.file_step(f"sheet / table {m[1]}")
            elif isinstance(v, FileValue) and body is not None and "Hidden" in repr(body):
                self.file_step("skips hidden files")
            elif isinstance(v, FileValue) and body is not None and "Extension" in repr(body):
                self.file_step("keeps files by extension")
            elif isinstance(v, FileValue):
                self.file_step(f"keeps the files / sheets that pass step '{self.step}'")
                fixed = {c: x for c, x in _equal_conditions(body) if v.meta.get(c) == FILE_COL}
                if fixed:
                    return FileValue(v.source, dict(v.nested), dict(v.meta, **fixed))
            else:
                self.file_step(f"drops file rows in step '{self.step}' before the header row")
                self.rows_above_header()
            return v
        rel = self.to_rel(v)
        f = self.as_lambda(a[1], env)
        if self.needs_materialize(rel, _fields(f.body)):
            rel = self.materialize(rel, self.step)
        rel.where.append(self.row(f.body, rel, dict(f.env)))
        only = _allowed_values(f.body)
        if only:
            rel.values[only[0]] = only[1]
        return rel

    def t_Table_Distinct(self, a, env):
        rel = self.rel_arg(a[0], env)
        if len(a) > 1:
            keys = self.ev(a[1], env)
            keys = [keys] if isinstance(keys, str) else keys
            if set(keys) != set(rel.cols):
                rel = self.materialize(rel, self.step)
                return self._first_per(rel, keys, "key")
        visible = [c for c in rel.cols if not c.startswith("__")]
        if "__part" in rel.cols:
            return self._first_per(rel, visible, "row")
        rel = self.materialize(rel, self.step) if rel.where or rel.join else rel
        rel.distinct = True
        return rel

    def _first_per(self, rel: Rel, keys, what: str) -> Rel:
        rel = self.materialize(rel, self.step)
        order = [exp.Ordered(this=col("__part"))] if "__part" in rel.cols else []
        self.part_used = self.part_used or bool(order)
        order += [exp.Ordered(this=rel.column(c)) for c in rel.cols if c not in keys and not c.startswith("__")]
        rn = exp.Window(this=exp.RowNumber(), partition_by=[rel.column(k) for k in keys],
                        order=exp.Order(expressions=order or [exp.Ordered(this=col(keys[0]))]))
        inner = rel.copy()
        inner.cols["__rn"] = rn
        inner = self.materialize(inner, self.step + f" (first row per {what})")
        inner.where.append(exp.EQ(this=col("__rn"), expression=lit(1)))
        inner.cols.pop("__rn")
        if what == "key":
            self.note_once("Table.Distinct on " + ", ".join(keys) + " keeps the first row Power Query loads"
                           + ("; the MV keeps the row of the first Table.Combine input, as Power BI does, and within "
                              "an input the first by the other columns (Spark has no load order)" if "__part" in rel.cols
                              else "; Spark has no load order, so the MV keeps the first by the other columns"))
            self.res.decisions.append(("one row per " + ", ".join(keys),
                                       f"Power Query keeps the first {', '.join(keys)} row it loads; rows with the same "
                                       f"key but different values are resolved by load order, which Incorta does not "
                                       f"have. Confirm the rule (first input of the union, then alphabetical) or pick one "
                                       f"(latest, most frequent)."))
        return inner

    def t_List_Zip(self, a, env):
        lists = self.ev(a[0], env)
        if not isinstance(lists, list) or not all(isinstance(x, list) for x in lists):
            raise Unsupported("List.Zip of computed lists")
        return [list(x) for x in zip(*lists)]

    def t_List_Accumulate(self, a, env):
        items, state, f = self.ev(a[0], env), self.ev(a[1], env), self.ev(a[2], env)
        if not isinstance(items, list) or not isinstance(f, Lambda) or len(f.params) != 2:
            raise Unsupported("List.Accumulate over a computed list")
        for it in items:
            local = dict(f.env)
            local[f.params[0]], local[f.params[1]] = state, it
            state = self.ev(f.body, local)
        return state

    def t_Table_Sort(self, a, env):
        v = self.ev(a[0], env)
        if isinstance(v, FileValue):
            spec = repr(a[1])
            if "Date modified" in spec or "Date created" in spec:
                self.file_step("takes the newest file (sorted by date modified, newest first)")
                v.source.notes.append("Power BI reads the newest file in the folder at each refresh")
            else:
                self.file_step(f"sorts the file list in step '{self.step}'")
            return v
        return self.to_rel(v)

    def t_Table_Buffer(self, a, env):
        return self.rel_arg(a[0], env)

    def t_Table_RemoveRowsWithErrors(self, a, env):
        self.note_once("Table.RemoveRowsWithErrors: rows that fail a conversion are kept with NULL in Spark")
        return self.rel_arg(a[0], env)

    def t_Table_ReplaceErrorValues(self, a, env):
        rel = self.rel_arg(a[0], env)
        for n, v in self.ev(a[1], env):
            rel.cols[n] = exp.Coalesce(this=rel.column(n), expressions=[lit(v)])
            rel.values.pop(n, None)
        return rel

    def rows_above_header(self):
        topic = "rows above the header"
        if not any(d[0] == topic for d in self.res.decisions):
            self.res.decisions.append((topic, f"Step '{self.step}' drops rows of the file before the header row is taken. "
                                       "Incorta reads the file with one header row: save or skip the file so its first "
                                       "row is the real header, and apply the same row filter (see the Power Query code) "
                                       "to the rows below it."))
        for src in self.res.sources:
            note = "remove the title and blank rows so the first row of the file is the real header row"
            if note not in src.notes:
                src.notes.append(note)

    def t_Table_PromoteHeaders(self, a, env):
        v = self.ev(a[0], env)
        if isinstance(v, FileValue):
            v = self.file_rel(v)
            if not v.grid:
                v.grid = True
        first_pass = self.known_raw is None
        raw_now = isinstance(v, Rel) and v.base == self._raw_name() and not self.ctes and not v.where
        if first_pass and self.raw_stage(v) and v.where:        # rows dropped between two header promotions
            raw_now = True
        if isinstance(v, Rel) and (v.grid or (raw_now and (v.open or not first_pass))):
            self.promotes += 1
            if not v.grid:                                 # promoted again: the first header row was a title row
                if first_pass:
                    self.seen.clear()
                self.file_step("skips a title row (headers promoted twice)")
            if not first_pass and self.promotes < self.carry.get("promotes", 1):
                return self.raw_rel(None, grid=True)       # still above the real header row
            for src in self.res.sources:
                src.header = True
            self.file_step("first row as headers")
            return self.raw_rel(None)
        raise Unsupported("PromoteHeaders on a computed table")

    def t_Table_Skip(self, a, env):
        v = self.ev(a[0], env)
        if self.is_file(v):
            n = self.ev(a[1], env)
            self.file_step(f"skips the first {n} row(s) of the sheet")
            src = v.source if isinstance(v, FileValue) else (self.res.sources[-1] if self.res.sources else None)
            if src is not None:
                src.notes.append(f"skip the first {n} row(s) of the file before the header row")
            return v
        if self.known_raw is None and self.raw_stage(v) and not v.where and not v.cols:
            self.skipped_after_header = True               # only right when a later step promotes the real header row
            return v
        raise Unsupported("Table.Skip on loaded rows (row order)")

    def t_Table_NestedJoin(self, a, env):
        left = self.rel_arg(a[0], env)
        lkeys = self.ev(a[1], env)
        right = self.rel_arg(a[2], env)
        rkeys = self.ev(a[3], env)
        newcol = self.ev(a[4], env)
        kind = self.ev(a[5], env) if len(a) > 5 else Enum("JoinKind.LeftOuter")
        lkeys = [lkeys] if isinstance(lkeys, str) else lkeys
        rkeys = [rkeys] if isinstance(rkeys, str) else rkeys
        if left.join:
            left = self.apply_join(left)
        left = self.materialize(left, self.step) if (self.needs_materialize(left, list(left.cols)) or left.where) else left
        if right.join or right.where or right.distinct or any(not isinstance(e, exp.Column) for e in right.cols.values()):
            right = self.materialize(right, self.step + " (right side)")
        left.join = {"right": right, "lkeys": lkeys, "rkeys": rkeys, "newcol": newcol,
                     "kind": kind.name.split(".")[-1] if isinstance(kind, Enum) else "LeftOuter", "step": self.step}
        return left

    def t_Table_ExpandTableColumn(self, a, env):
        v = self.ev(a[0], env)
        if isinstance(v, FileValue):
            names = self.ev(a[2], env)
            alias = self.ev(a[3], env) if len(a) > 3 and a[3][0] != "null" else names
            if v.meta:
                return self.file_expand(v, self.ev(a[1], env), names, alias)
            self.file_step(f"expands the sheet data ({len(names)} columns)")
            r = self.file_rel(v)
            if not r.open and not r.grid and alias != names:
                pass
            if r.grid or r.open:
                r.rename.update({al: nm for nm, al in zip(names, alias)})
            return r
        rel = v.copy()
        newcol = self.ev(a[1], env)
        names = self.ev(a[2], env)
        alias = self.ev(a[3], env) if len(a) > 3 and a[3][0] != "null" else names
        if not rel.join or rel.join["newcol"] != newcol:
            raise Unsupported(f"expand of [{newcol}] without a matching Table.NestedJoin")
        return self.apply_join(rel, expand=list(zip(names, alias)))

    def apply_join(self, rel: Rel, expand=None) -> Rel:
        j = rel.join
        rel = rel.copy()
        rel.join = None
        kind = j["kind"]
        right: Rel = j["right"]
        if rel.open:                                   # first pass over a file: note the key columns, keep reading
            for lk in j["lkeys"]:
                rel.column(lk)
            for src, dst in expand or []:
                right.column(src)
                rel.cols[dst] = col(self.sn(dst))
                if right.types.get(src):
                    rel.types[dst] = right.types[src]
            return rel
        # the left side is read directly when its columns are plain (renamed) columns; else as a CTE
        if not all(isinstance(e, exp.Column) and not e.table for e in rel.cols.values()) or rel.where or rel.distinct:
            rel = self.materialize(rel, j["step"] + " (left side)")
        left = {n: _qualify(e, "l") for n, e in rel.cols.items()}
        on = None
        for lk, rk in zip(j["lkeys"], j["rkeys"]):
            c = exp.NullSafeEQ(this=left[lk].copy(), expression=_qualify(right.column(rk), "r"))
            on = c if on is None else exp.And(this=on, expression=c)
        self._n += 1
        cte = f"s{self._n:02d}_" + re.sub(r"[^0-9a-z]+", "_", j["step"].lower()).strip("_")[:40]
        if expand is None or kind in ("LeftAnti", "LeftSemi") or (kind == "Inner" and not expand):
            cond = exp.Exists(this=exp.select("1").from_(exp.alias_(exp.to_table(right.base) if "." in right.base
                                                                     else exp.to_identifier(right.base), "r")).where(on))
            if kind == "LeftAnti":
                cond = exp.Not(this=cond)
            elif kind not in ("Inner", "LeftSemi"):
                cond = None
            s = exp.select(*[_as(e, self.sn(n)) for n, e in left.items()]).from_(
                exp.alias_(exp.to_table(rel.base) if "." in rel.base else exp.to_identifier(rel.base), "l"))
            if cond is not None:
                s = s.where(cond)
            self.ctes.append((cte, f"Power Query step: {j['step']} (Table.NestedJoin {kind})",
                              s.sql(dialect=DIALECT, pretty=True)))
            self.pending = [x for x in self.pending if x not in (j["step"], self.step)]
            return Rel(cte, {n: col(self.sn(n)) for n in rel.cols}, dict(rel.types), origin=dict(rel.origin),
                       values=dict(rel.values))
        join_sql = {"LeftOuter": "LEFT JOIN", "Inner": "JOIN", "RightOuter": "RIGHT JOIN", "FullOuter": "FULL JOIN"}.get(kind)
        if not join_sql:
            raise Unsupported(f"JoinKind.{kind}")
        items = [_as(e, self.sn(n)) for n, e in left.items()] + [exp.alias_(_qualify(right.column(src), "r"), q(self.sn(dst)))
                                                                 for src, dst in expand]
        rtable = right.base
        sel = ",\n  ".join(sql_of(i) for i in items)
        rsrc = rtable if "." in rtable else sql_of(q(rtable)) if False else rtable
        sql = (f"SELECT\n  {sel}\nFROM {rel.base} AS l\n{join_sql} {rsrc} AS r\n  ON {sql_of(on)}")
        self.ctes.append((cte, f"Power Query steps: {j['step']}, {self.step} (Table.NestedJoin {kind} + expand)"
                          if self.step != j["step"] else f"Power Query step: {j['step']} (Table.NestedJoin {kind})", sql))
        self.pending = [x for x in self.pending if x not in (j["step"], self.step)]
        out = Rel(cte, {n: col(self.sn(n)) for n in rel.cols}, dict(rel.types), origin=dict(rel.origin),
                  values=dict(rel.values) if kind in ("LeftOuter", "Inner") else {})
        for src, dst in expand:
            out.cols[dst] = col(self.sn(dst))
            if right.types.get(src):
                out.types[dst] = right.types[src]
        return out

    def t_Table_Join(self, a, env):
        left = self.rel_arg(a[0], env)
        right = self.rel_arg(a[2], env)
        lkeys, rkeys = self.ev(a[1], env), self.ev(a[3], env)
        kind = self.ev(a[4], env) if len(a) > 4 else Enum("JoinKind.Inner")
        left.join = {"right": right, "lkeys": [lkeys] if isinstance(lkeys, str) else lkeys,
                     "rkeys": [rkeys] if isinstance(rkeys, str) else rkeys, "newcol": "__join",
                     "kind": kind.name.split(".")[-1] if isinstance(kind, Enum) else "Inner", "step": self.step}
        return self.apply_join(left, expand=[(c, c) for c in right.cols if c not in left.cols])

    def t_Table_Combine(self, a, env):
        tables = self.ev(a[0], env)
        if isinstance(tables, FileValue):
            return self.file_rel(tables)
        if isinstance(tables, Rel) and (tables.grid or tables.base == self._raw_name()):
            return tables                                  # Table.Combine(list[Data]): the sheets of the file list
        if not isinstance(tables, list) or not all(isinstance(t, Rel) for t in tables):
            raise Unsupported("Table.Combine of computed tables")
        rels = []
        for t in tables:
            t = t.copy()
            if self.needs_materialize(t, list(t.cols)) or t.where or t.open:
                if t.open:
                    raise Unsupported("Table.Combine of a file table with unknown columns")
                t = self.materialize(t, self.step + " (input)")
            rels.append(t)
        names: list[str] = []
        for t in rels:
            names += [n for n in t.cols if n not in names and n != "__part"]
        if not self.carry.get("no_part"):
            names.append("__part")
            self.part_added = True
            for i, t in enumerate(rels):
                t.cols["__part"] = lit(i + 1)
        parts = []
        for t in rels:
            sel = [exp.alias_(t.cols[n], q(self.sn(n))) if n in t.cols and not (isinstance(t.cols[n], exp.Column) and t.cols[n].name == self.sn(n) and not t.cols[n].table)
                   else (col(self.sn(n)) if n in t.cols else exp.alias_(exp.Null(), q(self.sn(n)))) for n in names]
            parts.append(exp.select(*sel).from_(t.base).sql(dialect=DIALECT, pretty=True))
        self._n += 1
        cte = f"s{self._n:02d}_" + re.sub(r"[^0-9a-z]+", "_", self.step.lower()).strip("_")[:40]
        self.ctes.append((cte, f"Power Query step: {self.step} (Table.Combine: UNION ALL by column name)",
                          "\nUNION ALL\n".join(parts)))
        self.pending = [x for x in self.pending if x != self.step]
        types = {}
        for t in rels:
            for k, v in t.types.items():
                types.setdefault(k, v)
        values = {n: tuple(dict.fromkeys(x for t in rels for x in t.values[n])) for n in names
                  if all(n in t.cols and n in t.values for t in rels)}
        return Rel(cte, {n: col(self.sn(n)) for n in names}, types, values=values)

    def t_Table_ColumnNames(self, a, env):
        if a[0][0] == "call" and a[0][1][0] == "id" and a[0][1][1] in self.ctx.functions:
            return UnknownColumns()                        # columns of the sample file
        v = self.ev(a[0], env)
        if isinstance(v, FileValue) or (isinstance(v, Rel) and (v.grid or v.open)):
            return UnknownColumns()
        if isinstance(v, Rel):
            return [c for c in v.cols if not c.startswith("__")]
        raise Unsupported("Table.ColumnNames of a computed table")

    def t_List_Distinct(self, a, env):
        v = self.ev(a[0], env)
        if isinstance(v, list):
            return list(dict.fromkeys(v)) if all(x is None or isinstance(x, (str, int, float)) for x in v) else v
        if isinstance(v, ColList):
            return v
        raise Unsupported("List.Distinct of a computed list")

    def t_Table_Pivot(self, a, env):
        rel = self.rel_arg(a[0], env)
        vals, attr, value = self.ev(a[1], env), self.ev(a[2], env), self.ev(a[3], env)
        fn = self.ev(a[4], env) if len(a) > 4 else None
        known = rel.values.get(attr)
        if isinstance(vals, ColList):
            vals = list(vals.rel.values.get(vals.name) or ())
        if not isinstance(vals, list) or not vals or not all(isinstance(x, str) for x in vals):
            raise Unsupported(f"Table.Pivot on [{attr}]: its values come from the data, so the output columns cannot be "
                              f"known; filter [{attr}] to a fixed list of values first")
        if not known or not set(known) <= set(vals):
            raise Unsupported(f"Table.Pivot on [{attr}]: rows with other values than the listed ones may exist")
        fname = fn.name if isinstance(fn, FuncRef) else ("" if fn is None else "?")
        cls = {"": exp.Max, "List.Sum": exp.Sum, "List.Max": exp.Max, "List.Min": exp.Min,
               "List.Average": exp.Avg}.get(fname)
        if cls is None:
            raise Unsupported("Table.Pivot with an aggregation that is not List.Sum / Max / Min / Average")
        if not fname:
            self.note_once(f"pivot of [{attr}] without aggregation: Power Query raises an error when a key has two values; "
                           f"the MV keeps the largest")
        rel = self.materialize(rel, self.step) if (self.needs_materialize(rel, list(rel.cols)) or rel.where) else rel
        if rel.open:
            raise Unsupported("Table.Pivot of a table whose columns are not known")
        keys = [c for c in rel.cols if c not in (attr, value) and not c.startswith("__")]
        items = [_as(rel.column(k), self.sn(k)) for k in keys]
        for v in vals:
            pick = exp.Case(ifs=[exp.If(this=exp.EQ(this=rel.column(attr), expression=lit(v)), true=rel.column(value))])
            items.append(exp.alias_(cls(this=pick), q(self.sn(v))))
        self._n += 1
        cte = cte_name(self._n, self.step)
        s = exp.select(*items).from_(rel.base).group_by(*[rel.column(k) for k in keys])
        self.ctes.append((cte, f"Power Query step: {self.step} (Table.Pivot)", s.sql(dialect=DIALECT, pretty=True)))
        self.pending = [x for x in self.pending if x != self.step]
        types = {k: t for k, t in rel.types.items() if k in keys}
        types.update({v: ("number" if fname in ("List.Sum", "List.Average") else rel.types.get(value)) for v in vals})
        return Rel(cte, {c: col(self.sn(c)) for c in keys + vals}, types,
                   values={k: v for k, v in rel.values.items() if k in keys})

    def t_Table_SplitColumn(self, a, env):
        if not _digit_to_other(a[2]):
            raise Unsupported("Table.SplitColumn with this splitter")
        rel = self.rel_arg(a[0], env)
        cname, names = self.ev(a[1], env), self.ev(a[3], env) if len(a) > 3 else None
        if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
            raise Unsupported("Table.SplitColumn without a list of new names")
        if self.needs_materialize(rel, [cname]):
            rel = self.materialize(rel, self.step)
        x, new = rel.column(cname), {}
        for i, n in enumerate(names):                      # a part: non-digits then the digits that end it
            skip = "" if not i else r"(?:[^0-9]*[0-9]+)" + ("" if i == 1 else r"(?:[^0-9]+[0-9]+){%d}" % (i - 1))
            part = r"([^0-9]*[0-9]*)" if not i else r"([^0-9]+[0-9]*)"
            e = exp.Anonymous(this="regexp_extract", expressions=[x.copy(), lit("^" + skip + part), lit(1)])
            new[n] = exp.Nullif(this=e, expression=lit(""))
        out = {}
        for c, e in rel.cols.items():
            out.update(new if c == cname else {c: e})
        rel.cols = out
        rel.types.update({n: "text" for n in names})
        rel.origin.pop(cname, None)
        return rel

    def t_Table_Group(self, a, env):
        rel = self.rel_arg(a[0], env)
        keys = self.ev(a[1], env)
        keys = [keys] if isinstance(keys, str) else keys
        specs = a[2][1] if a[2][0] == "list" else []
        if specs and specs[0][0] != "list":
            specs = [a[2]]
        rel = self.materialize(rel, self.step) if (self.needs_materialize(rel, list(rel.cols)) or rel.where) else rel
        items = [_as(rel.column(k), self.sn(k)) for k in keys]
        out = {k: col(self.sn(k)) for k in keys}
        types = {k: rel.types[k] for k in keys if k in rel.types}
        for sp in specs:
            parts = sp[1]
            name = self.ev(parts[0], env)
            f = parts[1]
            body = f[1] if f[0] == "each" else None
            if body and body[0] == "try" and (body[2] is None or body[2][0] == "null"):
                body = body[1]
            agg = None
            if body and body[0] == "call" and body[1][0] == "id":
                fn = body[1][1]
                inner = body[2][0] if body[2] else None
                cast = nonnull = False
                while inner is not None and inner[0] == "call" and inner[1] in (("id", "List.RemoveNulls"),
                                                                                 ("id", "List.Transform")):
                    if inner[1][1] == "List.RemoveNulls":
                        nonnull = True
                    elif inner[2][1] in (("id", "Number.From"), ("each", ("call", ("id", "Number.From"), [("id", "_")]))):
                        cast = True
                    else:
                        break
                    inner = inner[2][0]
                if fn == "Table.RowCount":
                    agg = exp.Count(this=exp.Star())
                elif inner is not None and inner[0] == "field":
                    c = rel.column(inner[2])
                    c = exp.Cast(this=c, to=exp.DataType.build("DOUBLE")) if cast else c
                    if fn == "List.Count":                  # counts nulls too
                        agg = exp.Count(this=c) if nonnull else exp.Count(this=exp.Star())
                    elif fn == "List.First" and nonnull:
                        agg = exp.Min(this=c)
                        self.note_once(f"List.First of [{inner[2]}] per group: Power Query takes the first row it loads; Spark "
                                       f"has no load order, so the MV takes the first alphabetically")
                        self.res.decisions.append((f"one {inner[2]} per " + ", ".join(keys),
                                                   f"Power Query keeps the first non-blank {inner[2]} it loads per "
                                                   f"{', '.join(keys)}; Incorta has no load order. Confirm the rule (first "
                                                   f"alphabetically) or pick one (latest, most frequent)."))
                    else:
                        agg = {"List.Sum": exp.Sum, "List.Max": exp.Max, "List.Min": exp.Min, "List.Average": exp.Avg,
                               "List.NonNullCount": exp.Count}.get(fn, lambda this: None)(this=c)
                elif fn == "List.Count" and inner and inner[0] == "call" and inner[1] == ("id", "List.Distinct"):
                    agg = exp.Count(this=exp.Distinct(expressions=[rel.column(inner[2][0][2])]))
            if agg is None:
                raise Unsupported("Table.Group aggregation that is not a simple List.Sum / Max / Min / Average / Count")
            items.append(exp.alias_(agg, q(self.sn(name))))
            out[name] = col(self.sn(name))
            if len(parts) > 2:
                tv = self.ev(parts[2], env)
                types[name] = TYPE_HINT.get(TYPE_SQL.get(tv.text.replace("nullable ", ""))) if isinstance(tv, TypeVal) else None
        self._n += 1
        cte = f"s{self._n:02d}_" + re.sub(r"[^0-9a-z]+", "_", self.step.lower()).strip("_")[:40]
        s = exp.select(*items).from_(rel.base).group_by(*[rel.column(k) for k in keys])
        self.ctes.append((cte, f"Power Query step: {self.step} (Table.Group)", s.sql(dialect=DIALECT, pretty=True)))
        self.pending = [x for x in self.pending if x != self.step]
        return Rel(cte, out, types, values={k: v for k, v in rel.values.items() if k in keys})

    def t_Table_UnpivotOtherColumns(self, a, env):
        v = self.ev(a[0], env)
        keep = self.ev(a[1], env)
        attr, value = self.ev(a[2], env), self.ev(a[3], env)
        keep = [keep] if isinstance(keep, str) else keep
        rel = self.to_rel(v)
        if rel.open or self.step in self.carry.get("reshape", {}):
            # wide file (one column per month / item): columns are only known when the file is read
            self.reshape[self.step] = (keep, attr, value)
            self.file_step(f"unpivots every column except {', '.join(keep)} into {attr} / {value}: load the file in "
                           f"long format ({', '.join(keep)}, {attr}, {value}) - see the decision")
            if not any(d[0].startswith("wide file") for d in self.res.decisions):
                self.res.decisions.append(("wide file layout", f"The file has one column per {attr} (columns change "
                                           f"when new ones are added). Load it in long format ({', '.join(keep + [attr, value])}): "
                                           f"reshape the file at the source (recommended), or keep it wide and list "
                                           f"today's columns in a stack() MV that must change with every new column."))
            for src in self.res.sources:
                src.notes.append(f"reshape to long format before loading: {', '.join(keep + [attr, value])}")
            cols = keep + [attr, value]
            self.res.raw_columns = cols
            namer = lambda c: self.ctx.raw_column(self.name, c)  # noqa: E731
            out = Rel(self._raw_name(), {c: col(namer(c)) for c in cols}, origin={c: c for c in cols})
            out.types[value] = None
            return out
        return self._unpivot(rel, keep, [c for c in rel.cols if c not in keep and not c.startswith("__")], attr, value)

    def t_Table_Unpivot(self, a, env):
        rel = self.rel_arg(a[0], env)
        cols_ = self.ev(a[1], env)
        attr, value = self.ev(a[2], env), self.ev(a[3], env)
        return self._unpivot(rel, [c for c in rel.cols if c not in cols_], cols_, attr, value)

    def _unpivot(self, rel, keep, cols_, attr, value):
        if rel.open:
            raise Unsupported("unpivot of a file table with unknown columns")
        rel = self.materialize(rel, self.step) if (self.needs_materialize(rel, list(rel.cols)) or rel.where) else rel
        parts = []
        for c in cols_:
            s = exp.select(*[_as(rel.column(k), self.sn(k)) for k in keep], exp.alias_(lit(c), q(self.sn(attr))), exp.alias_(rel.column(c), q(self.sn(value)))) \
                .from_(rel.base).where(exp.Not(this=exp.Is(this=rel.column(c), expression=exp.Null())))
            parts.append(s.sql(dialect=DIALECT, pretty=True))
        self._n += 1
        cte = f"s{self._n:02d}_" + re.sub(r"[^0-9a-z]+", "_", self.step.lower()).strip("_")[:40]
        self.ctes.append((cte, f"Power Query step: {self.step} (unpivot; Power Query drops null values)",
                          "\nUNION ALL\n".join(parts)))
        self.pending = [x for x in self.pending if x != self.step]
        return Rel(cte, {**{k: col(self.sn(k)) for k in keep}, attr: col(self.sn(attr)), value: col(self.sn(value))})

    def t_Table_AddIndexColumn(self, a, env):
        rel = self.rel_arg(a[0], env)
        name = self.ev(a[1], env)
        start = self.ev(a[2], env) if len(a) > 2 else 0
        self.note_once("index column: Power Query numbers rows in load order; Spark has no load order")
        rel = self.materialize(rel, self.step) if (self.needs_materialize(rel, list(rel.cols)) or rel.where) else rel
        rn = exp.Window(this=exp.RowNumber(), order=exp.Order(expressions=[exp.Ordered(this=rel.column(c)) for c in rel.cols][:3]))
        rel.cols[name] = exp.Add(this=exp.Sub(this=rn, expression=lit(1)), expression=lit(start or 0))
        return rel


# ---------------------------------------------------------------------------------------------- helpers
def _has_file_col(e) -> bool:
    return isinstance(e, exp.Expression) and any(c.name == FILE_COL for c in e.find_all(exp.Column))


def _maybe_null(c: exp.Expression) -> bool:
    """Can a condition be NULL (Power Query: an error)?"""
    if isinstance(c, (exp.Paren, exp.Not)):
        return _maybe_null(c.this)
    if isinstance(c, (exp.And, exp.Or)):
        return _maybe_null(c.this) or _maybe_null(c.expression)
    if isinstance(c, (exp.Is, exp.NullSafeEQ, exp.Boolean, exp.Exists)):
        return False
    if isinstance(c, exp.Coalesce):
        return not (c.expressions and isinstance(c.expressions[-1], (exp.Literal, exp.Boolean)))
    if isinstance(c, (exp.EQ, exp.NEQ)):
        return not (isinstance(c.this, exp.Literal) and isinstance(c.expression, exp.Literal))
    return True


def _only_cleans(node) -> bool:
    """Does a column function only clean the value (trim, case, conversion)? Then it is still the source value."""
    if isinstance(node, tuple) and node and node[0] == "call":
        return node[1][0] == "id" and node[1][1] in CLEANING_FNS and all(_only_cleans(x) for x in node[2])
    if isinstance(node, tuple) and node and node[0] == "if":          # if _ = null then "NA" else _
        return all(_only_cleans(x) for x in node[1:])
    if isinstance(node, tuple) and node and node[0] == "bin" and node[1] in ("=", "<>", "and", "or"):
        return _only_cleans(node[2]) and _only_cleans(node[3])
    return isinstance(node, tuple) and bool(node) and node[0] in ("id", "str", "num", "null")


def _equal_conditions(body) -> list:
    """[A] = "x" and ... -> [(A, x), ...] (top-level conditions only)."""
    if isinstance(body, tuple) and body and body[0] == "bin" and body[1] == "and":
        return _equal_conditions(body[2]) + _equal_conditions(body[3])
    m = _equals_field(body)
    return [m] if m else []


def _allowed_values(body):
    """[A] = "x" or [A] = "y" -> (A, (x, y)); also List.Contains({...}, [A]); None for anything else."""
    if isinstance(body, tuple) and body and body[0] == "bin" and body[1] == "or":
        l, r = _allowed_values(body[2]), _allowed_values(body[3])
        return (l[0], tuple(dict.fromkeys(l[1] + r[1]))) if l and r and l[0] == r[0] else None
    if isinstance(body, tuple) and body and body[0] == "call" and body[1] == ("id", "List.Contains") \
            and len(body[2]) == 2 and body[2][0][0] == "list" and all(x[0] == "str" for x in body[2][0][1]) \
            and body[2][1][0] == "field" and body[2][1][1] is None:
        return body[2][1][2], tuple(x[1] for x in body[2][0][1])
    m = _equals_field(body)
    return (m[0], (m[1],)) if m else None


def _weekday_count(node):
    """List.Count(List.Select(List.Dates(start, Duration.Days(end - start) + 1, 1 day), each Monday-to-Friday)):
    (start, end) expressions, None for anything else."""
    try:
        sel = node[2][0]
        dates, test = sel[2]
        start, count, step = dates[2]
        days, one = count[2], count[3]
        cond = test[1]
        dow = cond[2]
        ok = (sel[1] == ("id", "List.Select") and dates[1] == ("id", "List.Dates") and count[:2] == ("bin", "+")
              and one == ("num", 1) and days[1] == ("id", "Duration.Days") and days[2][0][:2] == ("bin", "-")
              and days[2][0][3] == start and step[1] == ("id", "#duration")
              and [x[1] for x in step[2]] == [1, 0, 0, 0] and cond[:2] == ("bin", "<") and cond[3] == ("num", 5)
              and dow[1] == ("id", "Date.DayOfWeek") and dow[2] == [("id", "_"), ("id", "Day.Monday")])
        return (start, days[2][0][2]) if ok else None
    except (TypeError, ValueError, IndexError):
        return None


def _digit_to_other(node) -> bool:
    """Splitter.SplitTextByCharacterTransition({"0".."9"}, (c) => not List.Contains({"0".."9"}, c))."""
    digits = ("list", [("range", ("str", "0"), ("str", "9"))])
    return (node[0] == "call" and node[1] == ("id", "Splitter.SplitTextByCharacterTransition") and len(node[2]) == 2
            and node[2][0] == digits and node[2][1][0] == "fn" and len(node[2][1][1]) == 1
            and node[2][1][2] == ("un", "not", ("call", ("id", "List.Contains"), [digits, ("id", node[2][1][1][0])])))


def cte_name(n: int, step: str) -> str:
    return f"s{n:02d}_" + (re.sub(r"[^0-9a-z]+", "_", str(step).lower()).strip("_")[:40] or "step")


def _as(e: exp.Expression, name: str) -> exp.Expression:
    return e if isinstance(e, exp.Column) and e.name == name else exp.alias_(e, q(name))


def _qualify(e: exp.Expression, alias: str) -> exp.Expression:
    e = e.copy()
    for c in e.find_all(exp.Column):
        c.set("table", exp.to_identifier(alias))
    if isinstance(e, exp.Column):
        e.set("table", exp.to_identifier(alias))
    return e


def _fields(node) -> list:
    out = []

    def walk(n):
        if isinstance(n, tuple):
            if n and n[0] == "field" and (n[1] is None or n[1] == ("id", "_")):
                out.append(n[2])
            for x in n[1:]:
                walk(x)
        elif isinstance(n, list):
            for x in n:
                walk(x)
    walk(node)
    return out


def num_to_text(x: exp.Expression) -> exp.Expression:
    """Power Query Text.From(number): 12 -> '12', 1.5 -> '1.5' (no trailing zeros, no exponent)."""
    return exp.Anonymous(this="regexp_replace", expressions=[
        exp.Cast(this=exp.Cast(this=x, to=exp.DataType.build("DECIMAL(38,10)")), to=exp.DataType.build("STRING")),
        lit(r"\.?0+$"), lit("")])


def _can_error(e: exp.Expression) -> bool:
    return any(isinstance(n, (exp.Cast, exp.TryCast, exp.StrToDate, exp.Div, exp.DateDiff)) for n in e.walk())


def _nullable(e: exp.Expression) -> bool:
    return not isinstance(e, exp.Literal)


DAY_FIRST = ("en-GB", "en-AU", "en-NZ", "en-IN", "en-SG", "en-IE", "en-ZA", "en-MY", "fr", "es", "it", "pt", "id",
             "ms", "th", "vi", "nl", "de", "ru", "pl", "tr")


def _date_formats(opts, with_time=False) -> list[str]:
    """Spark patterns for Power Query Date.FromText(text, culture | [Format, Culture])."""
    fmt = culture = None
    if isinstance(opts, str):
        culture = opts
    elif isinstance(opts, dict):
        fmt, culture = opts.get("Format"), opts.get("Culture")
    if fmt:
        return [str(fmt).replace("tt", "a")]
    out = ["yyyy-M-d"]
    c = str(culture or "en-US")
    if c.startswith(("zh", "ja", "ko")):
        out.append("yyyy/M/d")
    elif c.startswith(DAY_FIRST):
        out.append("d.M.yyyy" if c.startswith(("de", "ru", "pl", "tr")) else "d/M/yyyy")
    else:
        out.append("M/d/yyyy")
    if with_time:
        out = [f + " H:m:s" for f in out] + out
    return out


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return type(a) is type(b) and a == b or (isinstance(a, (int, float)) and isinstance(b, (int, float)) and a == b)


def _equals_field(body):
    """each [Field] = "text"  ->  (Field, text)."""
    if isinstance(body, tuple) and body and body[0] == "bin" and body[1] == "=":
        l, r = body[2], body[3]
        if l[0] == "field" and l[1] is None and r[0] == "str":
            return l[2], r[1]
    return None


def _size(e) -> int:
    return len(sql_of(e)) if isinstance(e, exp.Expression) else 0


def _infer(e: exp.Expression, rel: Rel | None = None):
    """Rough Power Query type of a translated expression: text | number | date | datetime | logical | any | None."""
    while isinstance(e, exp.Paren):
        e = e.this
    if isinstance(e, (exp.Cast, exp.TryCast)):
        return TYPE_HINT.get(e.to.sql(dialect=DIALECT).upper(), None)
    if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Abs, exp.Floor, exp.Ceil,
                      exp.DateDiff, exp.Year, exp.Month, exp.Day, exp.Quarter, exp.Length, exp.Count, exp.Sum, exp.Avg,
                      exp.StrPosition)):
        return "number"
    if isinstance(e, (exp.StrToDate, exp.CurrentDate, exp.LastDay, exp.DateAdd)):
        return "date"
    if isinstance(e, exp.CurrentTimestamp):
        return "datetime"
    if isinstance(e, (exp.Concat, exp.ConcatWs, exp.Upper, exp.Lower, exp.Trim, exp.Initcap, exp.Substring, exp.Pad)):
        return "text"
    if isinstance(e, (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.LT, exp.GT, exp.LTE, exp.GTE, exp.And, exp.Or, exp.Not,
                      exp.In, exp.Is, exp.Boolean)):
        return "logical"
    if isinstance(e, exp.Anonymous):
        n = str(e.this).upper()
        if n in ("REGEXP_REPLACE", "REPLACE", "LEFT", "RIGHT", "DATE_FORMAT", "SUBSTRING_INDEX"):
            return "text"
        if n in ("ADD_MONTHS", "MAKE_DATE"):
            return "date"
        if n in ("BROUND", "ROUND"):
            return "number"
        if n == "ISNAN":
            return "logical"
    if isinstance(e, exp.Coalesce):
        return _infer(e.this, rel)
    if isinstance(e, exp.Case):
        kinds = {_infer(i.args.get("true"), rel) for i in e.args.get("ifs") or []}
        if e.args.get("default") is not None and not isinstance(e.args["default"], exp.Null):
            kinds.add(_infer(e.args["default"], rel))
        kinds.discard(None)
        return kinds.pop() if len(kinds) == 1 else ("any" if kinds else None)
    if isinstance(e, exp.Literal):
        return "text" if e.is_string else "number"
    if isinstance(e, exp.Column) and rel is not None:
        return rel.types.get(e.name)
    return None


def _py_bin(op, l, r):
    if op == "&":
        return f"{l}{r}"
    import operator as o
    return {"+": o.add, "-": o.sub, "*": o.mul, "/": o.truediv, "=": o.eq, "<>": o.ne, "<": o.lt, ">": o.gt,
            "<=": o.le, ">=": o.ge, "and": lambda a, b: a and b, "or": lambda a, b: a or b}[op](l, r)


def _py_scalar(name, args):
    if name == "Text.Lower" and args and isinstance(args[0], str):
        return args[0].lower()
    if name == "Text.Upper" and args and isinstance(args[0], str):
        return args[0].upper()
    return None


def _select_columns(native_sql: str) -> list[str]:
    """Output column names of a native SELECT (T-SQL style, as the ODBC driver returns them)."""
    try:
        tree = sqlglot.parse_one(native_sql, read="tsql")
    except Exception:  # noqa: BLE001
        return []
    while isinstance(tree, exp.Union):
        tree = tree.this
    if not isinstance(tree, exp.Select):
        sel = tree.find(exp.Select)
        if sel is None:
            return []
        tree = sel
    out = []
    for e in tree.expressions:
        if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)):
            return []
        name, n = e.alias_or_name, 2
        while name in out:                                # Power Query names a repeated column NAME2, NAME3, ...
            name, n = f"{e.alias_or_name}{n}", n + 1
        out.append(name)
    return out


def query_refs(name: str, code: str, all_queries, ast=None) -> list[str]:
    """Queries a query reads (by identifier), excluding its own step names."""
    try:
        ast = ast or parse_m(code)
    except MSyntaxError:
        return []
    local = {b[0] for b in ast[1]} if ast[0] == "let" else set()
    found = []

    def walk(n):
        if isinstance(n, tuple) and n:
            if n[0] == "call" and n[1] == ("id", "Table.ColumnNames") and n[2] and n[2][0][0] == "call":
                return                                 # column names of a sample file: not data
            if n[0] == "id" and n[1] in all_queries and n[1] != name and n[1] not in local and n[1] not in found:
                found.append(n[1])
            for x in n[1:]:
                walk(x)
        elif isinstance(n, list):
            for x in n:
                walk(x)
    walk(ast)
    return found


# ---------------------------------------------------------------------------------------------- the query graph
def split_navigation(name: str, code: str, queries) -> list:
    """A query that picks several tables of a database becomes one query per table plus the rest:
    [(new query, its M code, the rewritten AST of `name`)]; [] when there is at most one table."""
    try:
        ast = parse_m(code)
    except MSyntaxError:
        return []
    if ast[0] != "let":
        return []
    binds = dict(ast[1])
    text = lambda v: '"' + v.replace('"', '""') + '"'  # noqa: E731

    def nav(e, depth=0):
        """(M text of a navigation, its last selector) or None."""
        if depth > 12 or not isinstance(e, tuple):
            return None
        if e[0] == "id" and e[1] in binds:
            return nav(binds[e[1]], depth + 1)
        if e[0] == "call" and e[1][0] == "id" and e[1][1] in NAV_FNS and e[2] and all(
                a[0] in ("str", "record") for a in e[2]) and not any(
                a[0] == "record" and any(k == "Query" for k, _ in a[1]) for a in e[2]):
            return e[1][1] + "(" + ", ".join(text(a[1]) for a in e[2] if a[0] == "str") + ")", {}
        if e[0] == "field" and e[2] == "Data" and e[1] and e[1][0] == "item" and e[1][2][0] == "record":
            base = nav(e[1][1], depth + 1)
            sel = e[1][2][1]
            if base and all(v[0] == "str" for _, v in sel):
                rec = "[" + ", ".join(f'#"{k}"={text(v[1])}' if not k.isalnum() else f"{k}={text(v[1])}" for k, v in sel) + "]"
                return f"{base[0]}{{{rec}}}[Data]", {k: v[1] for k, v in sel}
        return None

    tables = {}
    for b, e in ast[1]:
        found = nav(e)
        if found and found[1] and (found[1].get("Kind") in ("Table", "View") or (found[1].get("Item") and "Kind" not in found[1])):
            tables[b] = found
    if len({t[0] for t in tables.values()}) < 2:
        return []

    def count(n, b):
        if isinstance(n, (tuple, list)):
            return (1 if tuple(n[:2]) == ("id", b) else 0) + sum(count(x, b) for x in n)
        return 0

    new, drop, replace = [], set(), {}
    for b, (path, sel) in tables.items():
        pick = [(s, e) for s, e in ast[1] if e[0] == "call" and e[1] == ("id", "Table.SelectColumns") and e[2][0] == ("id", b)
                and e[2][1][0] == "list" and all(x[0] == "str" for x in e[2][1][1])]
        if len(pick) != 1 or count(ast[1], b) + count(ast[2], b) != 1:
            return []                                  # the columns are not named: reported by the translator
        qn, n = f"{name} {sel.get('Item') or sel.get('Name')}", 2
        while qn in queries or any(qn == x[0] for x in new):
            qn, n = f"{name} {sel.get('Item') or sel.get('Name')} {n}", n + 1
        cols = "{" + ", ".join(text(x[1]) for x in pick[0][1][2][1][1]) + "}"
        new.append((qn, f"let\n    Source = {path},\n    Columns = Table.SelectColumns(Source, {cols})\nin\n    Columns"))
        drop.add(b)
        replace[pick[0][0]] = ("id", qn)
    out = ("let", [(b, replace.get(b, e)) for b, e in ast[1] if b not in drop], ast[2])
    return [(qn, part, out) for qn, part in new]


def _ident(name: str) -> str:
    from .plan import ident
    return ident(name)


def _placeholder(name: str) -> bool:
    from .plan import is_placeholder
    return is_placeholder(name)


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


class PowerQueryGraph:
    """All Power Query queries of a model, translated in dependency order.

    queries: name -> M code (loaded tables and connection-only queries)
    outputs: name -> [(M column, model column, type hint)] for the queries loaded into the model
    namer:   (query, model column) -> Incorta column name (the plan's IncortaNames.column); default ident()
    """

    def __init__(self, queries: dict, outputs: dict, schema: str, namer=None, params=None, functions=None):
        self.queries = dict(queries)
        self.functions = dict(functions or {})   # function query -> M code
        self.asts: dict[str, tuple] = {}         # queries rewritten because they read several database tables
        for q, code in list(queries.items()):
            try:
                split = split_navigation(q, code, self.queries)
            except Exception:  # noqa: BLE001 - an unexpected shape stays one query: the translator reports it
                log.exception("splitting the database tables of %s failed", q)
                split = []
            for name, part, ast in split:
                self.queries[name] = part
                self.asts[q] = ast
        self.outputs = {q: list(v) for q, v in outputs.items()}
        self.schema = schema
        self.namer = namer
        self.params = dict(params or {})
        self.renamed: dict[str, str] = {}        # query -> readable Incorta name used instead of ident(query)
        self.refs = {q: query_refs(q, code, self.queries, self.asts.get(q)) for q, code in self.queries.items()}
        self.results: dict[str, QueryResult] = {}
        self.objects: dict[str, str] = {}
        self.raw: dict[str, str] = {}
        taken: set[str] = set()
        for i, q in enumerate(self.queries, 1):
            first = _ident(q)
            if _placeholder(first):      # e.g. a Chinese query name: name it after its file / table
                first = _readable_source_name(self.queries[q]) or f"Query_{i}"
            base, n = first, 2
            while base.lower() in taken:
                base, n = f"{first}_{n}", n + 1
            taken.add(base.lower())
            self.raw[q] = base
            if first != _ident(q):
                self.renamed[q] = base
        self._cols: dict[tuple, str] = {}
        self._raw_cols: dict[tuple, str] = {}
        self.force_object: dict[str, str] = {}   # queries kept as physical tables by the planner
        self._raw_override: dict[tuple, str] = {}

    # ---- names
    def object_name(self, q: str) -> str:
        return self.objects.get(q) or (self.raw[q] + "_MV")

    def column_of(self, q: str, mcol: str) -> str:
        key = (q, mcol)
        if key not in self._cols:
            model = next((m for c, m, _ in self.outputs.get(q, []) if c == mcol), None)
            if model is not None and self.namer:
                self._cols[key] = self.namer(q, model)
            else:
                base, n = _ident(model or mcol), 2
                taken = {v.lower() for (t, _), v in self._cols.items() if t == q}
                name = base
                while name.lower() in taken:
                    name, n = f"{base}_{n}", n + 1
                self._cols[key] = name
        return self._cols[key]

    def raw_column(self, q: str, c: str) -> str:
        key = (q, c)
        if key not in self._raw_cols:
            base, n = self._raw_override.get(key) or _ident(c), 2
            taken = {v.lower() for (t, _), v in self._raw_cols.items() if t == q}
            name = base
            while name.lower() in taken:
                name, n = f"{base}_{n}", n + 1
            self._raw_cols[key] = name
        return self._raw_cols[key]

    def out_columns(self, q: str):
        if self.outputs.get(q):
            return [c for c, _, _ in self.outputs[q]]
        r = self.results.get(q)
        return list(r.columns) if r and r.columns else None

    def type_of(self, q: str, c: str):
        for mc, _, t in self.outputs.get(q, []):
            if mc == c:
                return t
        r = self.results.get(q)
        return (r.types or {}).get(c) if r else None

    def context(self) -> QueryContext:
        return QueryContext(self.queries, object_of=lambda q: f"{self.schema}.{self.object_name(q)}",
                            raw_of=lambda q: f"{self.schema}.{self.raw[q]}", column_of=self.column_of,
                            out_columns=self.out_columns, types_of=self.type_of, raw_column=self.raw_column,
                            params=self.params, functions=self.functions,
                            ast_of=lambda q: self.asts.get(q) or parse_m(self.queries[q]),
                            result_of=self.results.get, loaded=lambda q: bool(self.outputs.get(q)))

    # ---- graph
    def order(self) -> list[str]:
        seen, out = set(), []

        def visit(q, stack=()):
            if q in seen or q in stack:
                return
            for r in self.refs.get(q, []):
                visit(r, stack + (q,))
            seen.add(q)
            out.append(q)
        for q in self.queries:
            visit(q)
        return out

    def upstream(self, names) -> list[str]:
        """The queries the given ones read, directly or through other queries (not including them)."""
        out, todo = [], list(names)
        while todo:
            q = todo.pop()
            for r in self.refs.get(q, []):
                if r not in out and r not in names:
                    out.append(r)
                    todo.append(r)
        return [q for q in self.order() if q in out]

    def downstream(self, q: str) -> list[str]:
        return [x for x, rs in self.refs.items() if q in rs]

    def depth(self, q: str, _memo=None) -> int:
        _memo = {} if _memo is None else _memo
        if q not in _memo:
            _memo[q] = 0 if not self.refs.get(q) else 1 + max(self.depth(r, _memo) for r in self.refs[q])
        return _memo[q]

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
