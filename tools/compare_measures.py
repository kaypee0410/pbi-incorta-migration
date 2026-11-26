"""Compare the Measures sheet of two migration workbooks by measure name (not by row).

    python tools/compare_measures.py <ours.xlsx> <reference.xlsx> [--sql] [--only NAME ...]

Prints the Plan sheet's "measures translated" line of both, then every measure whose Scope, Pattern, Translation
status or Depends on measures differ. With --sql it also compares the Generic SQL after removing pure dialect
differences (quoting, LEN/LENGTH, DATEFROMPARTS/MAKE_DATE, EDATE/ADD_MONTHS, ISNULL/COALESCE, CAST(... AS FLOAT),
whitespace, letter case) and prints both versions when they still differ, for a person to judge.
Only reads the workbooks' text (measure names, DAX, SQL); no data rows.
"""
from __future__ import annotations

import argparse
import re
import sys

import openpyxl

FIELDS = ("Scope", "Pattern", "Translation status", "Depends on measures")
SQL_COL = "Generic SQL (on target views)"


def read(path: str) -> tuple[str, dict]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    plan_line = ""
    if "Plan" in wb.sheetnames:
        for row in wb["Plan"].iter_rows(values_only=True):
            for v in row:
                if isinstance(v, str) and "measures translated" in v:
                    plan_line = v.split("·")[0].strip()
    rows = wb["Measures"].iter_rows(values_only=True)
    head = [str(h or "").strip() for h in next(rows)]
    out = {}
    for r in rows:
        rec = {h: ("" if v is None else str(v)) for h, v in zip(head, r)}
        if rec.get("Measure"):
            out[rec["Measure"]] = rec
    wb.close()
    return plan_line, out


def deps(text: str) -> list[str]:
    return sorted(x.strip() for x in text.split(", ") if x.strip() and x.strip() != "-")


def norm_sql(sql: str) -> str:
    """The SQL without pure dialect differences, for a shape comparison."""
    s = sql or ""
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)                       # comments
    s = re.sub(r"CAST\(\s*(.*?)\s+AS\s+(FLOAT|REAL|DOUBLE)\s*\)", r"\1", s, flags=re.I)
    s = s.replace("[", "").replace("]", "").replace("`", "").replace('"', "")
    for a, b in ((r"\bLEN\(", "LENGTH("), (r"\bDATEFROMPARTS\(", "MAKE_DATE("), (r"\bISNULL\(", "COALESCE("),
                 (r"\bDATE_FROM_PARTS\(", "MAKE_DATE(")):
        s = re.sub(a, b, s, flags=re.I)
    s = re.sub(r"\bDATEADD\(\s*MONTH\s*,\s*([^,]+?)\s*,\s*([^()]+?)\)", r"ADD_MONTHS(\2, \1)", s, flags=re.I)
    s = re.sub(r"\bEDATE\(", "ADD_MONTHS(", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip().lower()
    s = re.sub(r"\s*([(),=<>+\-*/])\s*", r"\1", s)
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ours")
    ap.add_argument("reference")
    ap.add_argument("--sql", action="store_true", help="also compare the Generic SQL shape")
    ap.add_argument("--only", nargs="*", help="limit to these measure names")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p1, ours = read(args.ours)
    p2, ref = read(args.reference)
    print(f"Plan (ours):      {p1}\nPlan (reference): {p2}\n")
    names = args.only or sorted(set(ours) | set(ref), key=str.lower)
    diffs = 0
    for n in names:
        a, b = ours.get(n), ref.get(n)
        if a is None or b is None:
            print(f"[{n}] only in {'reference' if a is None else 'ours'}")
            diffs += 1
            continue
        lines = []
        for f in FIELDS:
            va, vb = a.get(f, ""), b.get(f, "")
            same = deps(va) == deps(vb) if f == "Depends on measures" else va.strip() == vb.strip()
            if not same:
                lines.append(f"  {f}: ours = {va!r}\n  {' ' * len(f)}  ref  = {vb!r}")
        if args.sql and norm_sql(a.get(SQL_COL, "")) != norm_sql(b.get(SQL_COL, "")):
            lines.append(f"  SQL (ours): {a.get(SQL_COL, '')}\n  SQL (ref):  {b.get(SQL_COL, '')}")
        if lines:
            diffs += 1
            print(f"[{n}]\n" + "\n".join(lines))
    print(f"\n{len(names)} measures compared, {diffs} with differences.")
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
