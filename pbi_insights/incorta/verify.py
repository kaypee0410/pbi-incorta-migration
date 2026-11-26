"""Check generated Power Query MVs against the rows Power BI saved in the .pbix, one query at a time.

For each query with MV SQL: its input objects are filled with the rows Power BI stored for the queries it reads;
its raw source table is rebuilt from the query's own stored rows (the source columns usually survive to the
output, trimmed or retyped); the Spark SQL runs in DuckDB (transpiled with sqlglot) and every output column is
compared, as a sorted list of values, with Power BI's stored column. Needs `pip install duckdb`; runs locally.
"""
from __future__ import annotations

import datetime as dt
import math
import re


def _norm(v):
    if v is None:
        return None
    try:
        import pandas as pd
        if v is pd.NA or v is pd.NaT or (not isinstance(v, (str, bytes, list, tuple)) and pd.isna(v)):
            return None
    except (TypeError, ValueError, ImportError):
        pass
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None if math.isnan(v) else ("inf" if v > 0 else "-inf")
    if hasattr(v, "isoformat"):
        try:
            return v.date().isoformat() if isinstance(v, dt.datetime) or hasattr(v, "to_pydatetime") else v.isoformat()
        except Exception:  # noqa: BLE001
            return str(v)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return _num(float(v))
    s = str(v)
    if s in ("NaN", "nan"):
        return None                                      # 0/0: Power BI keeps NaN, Spark returns NULL
    if s in ("Infinity", "-Infinity", "inf", "-inf"):
        return "inf" if not s.startswith("-") else "-inf"
    if re.fullmatch(r"\s*-?\d+(\.\d*)?([eE][-+]?\d+)?\s*", s):
        return _num(float(s))
    return s


def _num(f: float) -> float:
    """Twelve significant digits (floating-point noise below that); _close() allows for Power BI's shorter text."""
    return float(f"{f:.12g}") if f else 0.0


def _key(v):
    if v is None:
        return (0, 0.0, "")
    if isinstance(v, (int, float)):
        return (1, float(v), "")
    return (2, 0.0, str(v))


def _close(a: list, b: list, rtol: float = 5e-8, atol: float = 1e-9) -> bool:
    """Same values up to floating-point noise (both lists sorted). Power BI keeps untyped (any) numbers as text
    rounded to about nine digits, so the tolerance is 5e-8 relative plus 1e-9 absolute."""
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        if isinstance(x, float) and isinstance(y, float) and abs(x - y) <= atol + rtol * max(abs(x), abs(y)):
            continue
        return False
    return True


def _div_zero(a: list, b: list) -> str:
    """Only difference: Power BI Infinity where the MV has NULL (x / 0)."""
    inf_b = sum(1 for y in b if y in ("inf", "-inf"))
    if not inf_b or len(a) != len(b):
        return ""
    a2 = sorted((x for x in a if x is not None), key=_key)
    b2 = sorted((y for y in b if y is not None and y not in ("inf", "-inf")), key=_key)
    na, nb = sum(1 for x in a if x is None), sum(1 for y in b if y is None)
    if na - nb == inf_b and _close(a2, b2):
        return f"{inf_b} rows"
    return ""


def find_today(frames) -> str | None:
    for df in frames.values():
        if df is not None and "TODAY" in df.columns and len(df):
            v = df["TODAY"].dropna()
            if len(v):
                return str(v.iloc[0])[:10]
    return None


def check_graph(graph, frames: dict, only=None, examples: bool = False) -> list[dict]:
    """graph: PowerQueryGraph (translated); frames: query -> DataFrame of Power BI's saved rows, M column names.

    One row per query with an MV: Match / Differs / SQL error / No data / Not checked, rows and column details.
    examples=True adds one differing value per column (data values: for the developer's console only, never for
    the package documents).
    """
    import logging

    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    today = find_today(frames)
    out = []
    for q, r in graph.results.items():
        if only is not None and q not in only:
            continue
        base = {"query": q, "object": graph.object_name(q), "depth": graph.depth(q), "reads": ", ".join(r.refs)}
        if r.unsupported:
            out.append(dict(base, status="Not translated", detail="; ".join(f"{a}: {b}" for a, b in r.unsupported)))
            continue
        if r.passthrough:
            out.append(dict(base, status="No MV", detail="loads straight from the source (no Power Query steps)"))
            continue
        stored = frames.get(q)
        if stored is None:
            out.append(dict(base, status="No data", detail="no rows saved in the .pbix for this query"))
            continue
        missing_refs = [x for x in r.refs if frames.get(x) is None]
        if missing_refs:
            out.append(dict(base, status="No data", detail="inputs without saved rows: " + ", ".join(missing_refs)))
            continue
        try:
            res, lost = run_query(graph, frames, q, today)
        except Exception as ex:  # noqa: BLE001
            out.append(dict(base, status="SQL error", detail=str(ex).splitlines()[0][:300]))
            continue
        diffs, compared, case_only, same, divzero = [], 0, [], [], []
        for c in r.columns:
            ic = graph.column_of(q, c)
            if c not in stored.columns or ic not in res.columns:
                continue
            compared += 1
            a = sorted((_norm(v) for v in res[ic].tolist()), key=_key)
            b = sorted((_norm(v) for v in stored[c].tolist()), key=_key)
            if a == b:
                same.append(c)
                continue
            al = sorted((x.lower() if isinstance(x, str) else x for x in a), key=_key)
            bl = sorted((x.lower() if isinstance(x, str) else x for x in b), key=_key)
            if al == bl:
                case_only.append(c)
                continue
            if _close(a, b):
                same.append(c)
                continue
            dz = _div_zero(a, b)
            if dz:
                divzero.append(f"{c} ({dz})")
                continue
            from collections import Counter
            ca, cb = Counter(a), Counter(b)
            extra, missing = ca - cb, cb - ca
            bad = sum(missing.values())
            ex_mv = next(iter(extra), None), next(iter(missing), None)
            diffs.append(f"{c} ({bad:,} of {len(b):,} values differ" + (
                f", e.g. MV {ex_mv[0]!r} vs Power BI {ex_mv[1]!r})" if examples else ")"))
        status = "Match" if not diffs and len(res) == len(stored) else "Differs"
        detail = "; ".join(diffs[:8]) + (f" (+{len(diffs) - 8} more)" if len(diffs) > 8 else "")
        if case_only:
            detail = (detail + "; " if detail else "") + (
                "upper/lower case only in " + ", ".join(case_only) + " (Power BI keeps one spelling per distinct value)")
        if divzero:
            detail = (detail + "; " if detail else "") + ("division by zero gives NULL in the MV where Power BI keeps "
                                                          "Infinity: " + ", ".join(divzero))
        if lost:
            detail = (detail + "; " if detail else "") + (
                "source columns not saved in Power BI (rebuilt as NULL, columns built from them cannot be checked): "
                + ", ".join(lost[:10]) + (" ..." if len(lost) > 10 else ""))
            if diffs:
                status = "Partly checked"
        out.append(dict(base, status=status, rows=len(res), pbi_rows=len(stored), columns=compared,
                        matching=len(same) + len(case_only), detail=detail, today=today,
                        method="source rebuilt from the saved output" if r.sources else "inputs = saved outputs"))
    return out


def to_duckdb(sql: str) -> str:
    """An MV's Spark SQL for DuckDB (only used to check it)."""
    import sqlglot

    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="spark").transform(           # Spark casts bad text to NULL, DuckDB raises
        lambda n: exp.TryCast(this=n.this, to=n.args["to"]) if type(n) is exp.Cast else n)
    duck = tree.sql(dialect="duckdb")
    duck = re.sub(r"(?<!TRY_)STRPTIME\(", "TRY_STRPTIME(", duck)
    return re.sub(r"(?i)\bSUBSTRING_INDEX\(", "SPLIT_PART(", duck)     # the translator only takes the first part


def run_query(graph, frames: dict, q: str, today: str | None = None):
    """Run one query's MV; the rebuilt source is loaded as text when the steps compare a saved number with text."""
    import duckdb

    try:
        return _run_query(graph, frames, q, today)
    except duckdb.ConversionException:
        if not graph.results[q].sources:
            raise
        return _run_query(graph, frames, q, today, as_text=True)


def _run_query(graph, frames: dict, q: str, today: str | None = None, as_text: bool = False):
    """Run one query's MV SQL in DuckDB over Power BI's saved rows. Returns (result DataFrame, lost source columns)."""
    import duckdb
    import pandas as pd
    import sqlglot

    r = graph.results[q]
    stored = frames[q]
    con = duckdb.connect()
    try:
        for ref in r.refs:
            _load(con, f"{graph.schema}.{graph.object_name(ref)}", frames[ref], lambda c, ref=ref: graph.column_of(ref, c))
        lost = []
        if r.sources:
            back = {}
            for c, rc in r.lineage.items():
                if c in stored.columns:
                    back.setdefault(rc, c)
            raw = pd.DataFrame(index=stored.index)
            for rc in r.raw_columns:
                if rc in back:
                    raw[graph.raw_column(q, rc)] = stored[back[rc]]
                else:
                    raw[graph.raw_column(q, rc)] = pd.Series([None] * len(stored), index=stored.index, dtype="object")
                    lost.append(rc)
            if as_text:
                for c in raw.columns:
                    if pd.api.types.is_numeric_dtype(raw[c]) and not pd.api.types.is_bool_dtype(raw[c]):
                        raw[c] = raw[c].map(lambda v: None if pd.isna(v) else str(v)).astype("object")
            _load(con, f"{graph.schema}.{graph.raw[q]}", raw, lambda c: c)
        duck = to_duckdb(r.sql)
        if today:
            duck = re.sub(r"CURRENT_DATE(\(\))?", f"CAST('{today}' AS DATE)", duck)
            duck = re.sub(r"CURRENT_TIMESTAMP(\(\))?", f"CAST('{today}' AS TIMESTAMP)", duck)
        return con.execute(duck).df(), lost
    finally:
        con.close()


def run_chain(graph, raw_frames: dict) -> dict:
    """Run the whole Power Query chain in DuckDB: raw_frames = query -> DataFrame of its source (source column
    names). Every MV runs after the objects it reads and is stored under its Incorta name. Returns query -> result."""
    import logging

    import duckdb
    import sqlglot

    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    con = duckdb.connect()
    out = {}
    try:
        for q in graph.order():
            r = graph.results.get(q)
            if r is None or r.unsupported or r.helper:
                continue
            if r.sources:
                df = raw_frames[q]
                _load(con, f"{graph.schema}.{graph.raw[q]}", df, lambda c, q=q: graph.raw_column(q, c))
            if r.passthrough:
                out[q] = con.execute(f'SELECT * FROM {graph.schema}."{graph.raw[q]}"').df()
                continue
            duck = to_duckdb(r.sql)
            con.execute(f'CREATE TABLE {graph.schema}."{graph.object_name(q)}" AS {duck}')
            out[q] = con.execute(f'SELECT * FROM {graph.schema}."{graph.object_name(q)}"').df()
    finally:
        con.close()
    return out


def _load(con, full_name: str, df, rename):
    import pandas as pd

    schema, table = full_name.split(".", 1)
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    d = df.rename(columns={c: rename(c) for c in df.columns})
    d = d.loc[:, ~d.columns.duplicated()].copy()
    for c in d.columns:
        if str(d[c].dtype).startswith("datetime64"):
            d[c] = pd.Series([x.to_pydatetime() if pd.notna(x) else None for x in d[c]], index=d.index, dtype="object")
    con.register("tmp_df", d)
    con.execute(f'CREATE OR REPLACE TABLE {schema}."{table}" AS SELECT * FROM tmp_df')
    con.unregister("tmp_df")
