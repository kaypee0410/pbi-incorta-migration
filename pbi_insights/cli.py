"""Command line: python -m pbi_insights.cli <file.pbix> [--out outputs] [--data] [--incorta]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import STEPS, Options, run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pbi_insights", description="Inventory a Power BI file for migration and "
                                 "write an Excel workbook (plus a JSON export).")
    ap.add_argument("file", help=".pbix, .pbit or .bim file")
    ap.add_argument("--out", default="outputs", help="output folder (default: outputs)")
    ap.add_argument("--data", action="store_true", help="profile the saved data and test-run every insight query")
    ap.add_argument("--view-prefix", default="vw_", help="prefix for target view names (default: vw_)")
    ap.add_argument("--incorta", action="store_true",
                    help="also write the Incorta build package (SQL, design PDF, diagram, build plan)")
    ap.add_argument("--incorta-schema", default="", help="Incorta physical schema name (default: report name)")
    ap.add_argument("--no-incorta-check", action="store_true",
                    help="skip running the Power Query MVs on the rows saved in the .pbix (needs duckdb)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 2

    def progress(i, msg):
        if not args.quiet:
            print(f"[{i + 1}/{len(STEPS)}] {msg}", flush=True)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    res = run(path, args.out, Options(read_data=args.data,
                                      view_prefix=args.view_prefix, incorta=args.incorta,
                                      incorta_schema=args.incorta_schema,
                                      incorta_check=not args.no_incorta_check), progress)
    s = res.summary
    print(f"\n{s['tabs']} tabs, {s['data_insights']} data insights, {s['tables']} tables, {s['measures']} measures "
          f"({s['measures_translated']} translated, {s['measures_review']} to review), {s['findings']} findings "
          f"({s['findings_high']} high) in {s['seconds']} s")
    for w in res.warnings:
        print(f"WARNING: {w}")
    print(f"Workbook: {res.workbook}\nJSON:     {res.json_path}")
    if res.incorta_zip:
        inc = s.get("incorta") or {}
        print(f"Incorta:  {res.incorta_zip}  (schema {inc.get('schema')}, {inc.get('mvs')} MVs, {inc.get('joins')} "
              f"joins, {inc.get('measures_needing_design')} measure(s) needing a decision)")
        if inc.get("checks"):
            print("          Power Query MVs run on the saved rows: "
                  + ", ".join(f"{n} {k.lower()}" for k, n in sorted(inc["checks"].items(), key=lambda kv: -kv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
