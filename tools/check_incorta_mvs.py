"""Check the Incorta MV SQL against the values Power BI stored in the .pbix (no Incorta needed).

For every MV that replaces a DAX calculated table or the calculated columns of a table, the Spark SQL is
transpiled to DuckDB with sqlglot, run on the tables saved in the .pbix (renamed to the Incorta column names),
and every generated column is compared row by row with the column Power BI computed.

    pip install duckdb
    python tools\\check_incorta_mvs.py samples\\MyReport.pbix

Runs locally; the data never leaves the machine.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    import duckdb
    import pandas as pd
    import sqlglot

    from pbi_insights.analyze.profile import load_frames
    from pbi_insights.incorta.plan import IncortaPlanner
    from pbi_insights.pipeline import Options, analyze

    argv = argv or sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    data = analyze(argv[0], Options())
    pl = IncortaPlanner(data)
    plan = pl.build()
    names = pl.names
    mvs = [m for m in plan["mvs"] if m["pbi_table"]]
    if not mvs:
        print("No MV replaces a calculated table or calculated columns: nothing to check.")
        return 0
    needed = {t for t in names.raw} | {m["pbi_table"] for m in mvs}
    frames, _ = load_frames(data["model"], needed)
    con = duckdb.connect()
    con.execute(f"CREATE SCHEMA {names.schema}")
    for t, df in frames.items():
        if t not in names.raw:
            continue
        cols = [c for c in df.columns if (t, c) in pl.cols and not pl.is_calc(t, c)]
        raw = df[cols].rename(columns={c: names.column(t, c) for c in cols})
        raw.insert(0, "__rid", range(len(raw)))
        con.register("tmp_df", raw)
        con.execute(f'CREATE TABLE {names.schema}."{names.raw[t]}" AS SELECT * FROM tmp_df')
        con.unregister("tmp_df")
    failures = 0
    for mv in mvs:
        t, pbi = mv["pbi_table"], frames.get(mv["pbi_table"])
        tree = sqlglot.parse_one(mv["sql"], read="spark")
        calc_table = mv["kind"].startswith("Calc")
        if not calc_table:
            ctes = tree.args.get("with") or tree.args.get("with_")
            ctes.expressions[0].this.select("__rid", copy=False)
            tree = tree.select("__rid", copy=False).order_by("__rid")
        sql = tree.sql(dialect="duckdb").replace("DAYOFWEEK(", "1 + DAYOFWEEK(")   # Spark: Sunday = 1
        try:
            res = con.execute(sql).df()
        except Exception as ex:  # noqa: BLE001
            print(f"FAIL {mv['name']}: {ex}")
            failures += 1
            continue
        if pbi is None:
            print(f"{mv['name']}: {len(res)} rows (no saved Power BI data to compare)")
            continue
        print(f"{mv['name']}: {len(res)} rows (Power BI {len(pbi)})")
        if calc_table:
            key = names.column(t, pbi.columns[0])
            a = res.sort_values(key).reset_index(drop=True)
            b = pbi.sort_values(pbi.columns[0]).reset_index(drop=True)
            cols = [c for c in pbi.columns if names.column(t, c) in res.columns]
        else:
            a, b = res.reset_index(drop=True), pbi.reset_index(drop=True)
            cols = [c for c in pbi.columns if pl.is_calc(t, c) and names.column(t, c) in res.columns]
        for c in cols:
            x, y = a[names.column(t, c)], b[c]
            if pd.api.types.is_bool_dtype(y) or pd.api.types.is_bool_dtype(x):
                bad = x.astype("boolean").fillna(False) != y.astype("boolean").fillna(False)
            elif pd.api.types.is_numeric_dtype(y) and pd.api.types.is_numeric_dtype(x):
                xx, yy = pd.to_numeric(x, errors="coerce"), pd.to_numeric(y, errors="coerce")
                bad = ~(((xx - yy).abs() <= 1e-6 * (1 + yy.abs())) | (xx.isna() & yy.isna()))
            elif "date" in str(y.dtype) or "date" in str(x.dtype):
                bad = pd.to_datetime(x).dt.date.astype(str) != pd.to_datetime(y).dt.date.astype(str)
            else:
                bad = x.astype(str).where(x.notna(), "") != y.astype(str).where(y.notna(), "")
            n = int(bad.sum())
            failures += n > 0
            print(f"  {'OK  ' if n == 0 else 'DIFF'} {c}: {n} of {len(bad)} rows differ")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
