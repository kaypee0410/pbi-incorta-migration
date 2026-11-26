"""Any legal file must produce a workbook: odd names, missing names, broken DAX and M, random damage.

Each case goes through the whole pipeline (workbook, JSON, Incorta package). A guard that fires is a warning on
the page, never an exception; the workbook must still open.
"""
from __future__ import annotations

import copy
import json
import random
import zipfile

import numpy as np
import pandas as pd
import pytest
from openpyxl import load_workbook

from conftest import layout, tmsl
from pbi_insights.extract.layout import _num, normalize_report
from pbi_insights.extract.model import normalize_model
from pbi_insights.incorta.m2sql import PowerQueryGraph
from pbi_insights.incorta.plan import ident, is_placeholder
from pbi_insights.pipeline import Options, run
from pbi_insights.report.workbook import _clean


def _run(tm: dict, lay: dict, tmp_path, incorta=True):
    path = tmp_path / "Case.pbit"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("DataModelSchema", json.dumps(tm).encode("utf-16-le"))
        z.writestr("Report/Layout", json.dumps(lay).encode("utf-16-le"))
        z.writestr("Version", "1.28".encode("utf-16-le"))
    res = run(path, tmp_path / "out", Options(incorta=incorta))
    wb = load_workbook(res.workbook)
    assert wb.sheetnames
    json.loads(res.json_path.read_text(encoding="utf-8") if hasattr(res.json_path, "read_text")
               else open(res.json_path, encoding="utf-8").read())
    return res, wb


def _tables(tm):
    return tm["model"]["tables"]


# ---------------------------------------------------------------------------------------------- unit level
def test_normalize_model_cleans_names_and_numbers():
    m = {"tables": [{"name": 2024.0, "row_count": np.int64(3)}, {"name": float("nan")}],
         "columns": [{"table": "T", "column": "A", "dax": float("nan"), "sort_by": pd.NA}, {"table": "T", "column": None}],
         "measures": [{"id": 1, "table": "T", "name": "x"}, {"id": 1, "table": "T", "name": "y"}],
         "relationships": [{"from_table": "T", "from_column": "A", "to_table": "U", "to_column": None}],
         "hierarchies": [{"table": "T", "hierarchy": "H", "level": None, "column": "A", "ordinal": "x"}]}
    out = normalize_model(m)
    assert out["tables"] == [{"name": "2024", "row_count": 3}] and type(out["tables"][0]["row_count"]) is int
    assert out["columns"] == [{"table": "T", "column": "A", "dax": None, "sort_by": None}]
    assert len({x["id"] for x in out["measures"]}) == 2
    assert out["relationships"] == [] and out["hierarchies"][0]["level"] == "A" and out["hierarchies"][0]["ordinal"] is None
    assert len(out["warnings"]) == 3                          # the table, the column and the relationship


def test_normalize_report_and_positions():
    rep = {"pages": [{"name": None}, {"name": "P", "ordinal": "2"}, {"name": "P"}],
           "visuals": [{"page": None, "fields": [{"table": 5, "field": None}]}]}
    normalize_report(rep)
    assert [p["name"] for p in rep["pages"]] == ["Page 1", "P", "P (2)"]
    assert [p["ordinal"] for p in rep["pages"]] == [0, 2, 2]
    v = rep["visuals"][0]
    assert (v["page"], v["seq"], v["page_ordinal"], v["fields"][0]["table"]) == ("Page 1", 1, 0, "5")
    assert [_num(x) for x in ("12.6", None, "x", float("nan"))] == [13, 0, 0, 0]


@pytest.mark.parametrize("value, cell", [
    (None, ""), (float("nan"), ""), (pd.NaT, ""), (pd.NA, ""), (np.int64(4), 4), (True, "Yes"), (float("inf"), "inf"),
    ({"a": 1}, '{"a": 1}'), ([1, None, "x"], "1\n\nx"), ("a\x00b", "ab")])
def test_any_value_becomes_a_cell(value, cell):
    assert _clean(value) == cell


def test_names_are_safe_unique_and_stable():
    assert ident("2024") == "N_2024" and ident("GP %") == "GP_pct"
    a, b = ident("物料主档"), ident("客户")
    assert a != b and is_placeholder(a) and is_placeholder(b) and a == ident("物料主档")
    assert not is_placeholder(ident("Sales"))


@pytest.mark.parametrize("code", [
    "", "let", "let a = in a", "let a = 1 in b", "let Source = Missing in Source", '"unclosed', "/* unclosed",
    "let a = Table.SelectRows(1, 2) in a", "let a = Table.Group() in a", "let a = {1}{0}[x] in a", "[a = 1]",
    'let a = Odbc.DataSource("dsn=X"), b = a{0}[Data] in b', "let a = Table.Pivot(1) in a", "each each _",
    "let a = Table.AddColumn(Folder.Files(\"x\"), \"n\", each [Name]) in a", "(" * 400,
    'let a = Table.SelectColumns(Odbc.Query("dsn=X", "SELECT A FROM T"), {1, null}) in a',
    'let a = Table.RenameColumns(Odbc.Query("dsn=X", "SELECT A FROM T"), {{"A"}}) in a',
    'let s = Odbc.DataSource("dsn=X"), t = s{[Name="A",Kind="Table"]}[Data], u = s{[Name="B",Kind="Table"]}[Data], '
    'c = Table.SelectColumns(t), d = Table.SelectColumns(u) in d'])
def test_broken_m_is_flagged_never_raised(code):
    g = PowerQueryGraph({"Q": code}, {"Q": [("A", "A", "text")]}, "S")
    r = g.translate_all()["Q"]
    assert r.unsupported and not r.sql
    step, reason = r.unsupported[0]
    assert step and reason


def test_missing_query_is_named():
    g = PowerQueryGraph({"Q": "let Source = Gone in Source"}, {"Q": [("A", "A", "text")]}, "S")
    assert "'Gone' is not a step or query" in g.translate_all()["Q"].unsupported[0][1]


# ---------------------------------------------------------------------------------------------- whole pipeline
ODD = ["2024", "物料主档", "A/B & C", "x" * 180, "select", "a'b\"c", "tab\there", "emoji 😀", "=1+1", "<Blank>", "a|b",
       "100%", " lead"]


@pytest.mark.parametrize("name", ODD)
def test_odd_table_and_column_names(name, tmp_path):
    tm, lay = tmsl(), layout()
    t = copy.deepcopy(_tables(tm)[1])
    t["name"] = name
    t["columns"][0]["name"] = name
    for p in t.get("partitions") or []:
        p["name"] = name
    for m in t.get("measures") or []:
        m["name"] = f"{name} m"
    _tables(tm).append(t)
    res, wb = _run(tm, lay, tmp_path)
    assert "Not written" not in wb.sheetnames
    for ws in wb.worksheets:
        assert all(c.data_type != "f" for row in ws.iter_rows() for c in row)


def test_page_without_name_or_ordinal_and_odd_positions(tmp_path):
    tm, lay = tmsl(), layout()
    s = lay["sections"][0]
    s.pop("ordinal", None)
    s.pop("displayName", None)
    for vc in s["visualContainers"]:
        vc["x"], vc["y"] = "12.5", None
    _, wb = _run(tm, lay, tmp_path)
    assert "Not written" not in wb.sheetnames


@pytest.mark.parametrize("dax", ["SUM(", "CALCULATE(", "))", "", "VAR x =", "[Missing Measure]", "Nope[Col]",
                                 "IF(1, 2, 3, 4, 5)", "SUMX(FILTER(", "1 +", "\"unclosed"])
def test_broken_dax_is_a_review(dax, tmp_path):
    tm, lay = tmsl(), layout()
    tb = next(t for t in _tables(tm) if t.get("measures"))
    tb["measures"][0]["expression"] = dax
    next(t for t in _tables(tm) if t.get("columns"))["columns"].append(
        {"name": "Broken", "type": "calculated", "dataType": "double", "expression": dax})
    res, wb = _run(tm, lay, tmp_path)
    assert "Not written" not in wb.sheetnames


def test_broken_m_in_a_table_keeps_the_package(tmp_path):
    tm, lay = tmsl(), layout()
    for t in _tables(tm):
        for p in t.get("partitions") or []:
            if (p.get("source") or {}).get("type") == "m":
                p["source"]["expression"] = ["let", "    Source = Sql.Database(", "in", "    Source"]
    res, _ = _run(tm, lay, tmp_path)
    assert res.incorta_zip


# ---------------------------------------------------------------------------------------------- random damage
JUNK = [None, "", 0, -1, 3.5, "x", [], {}, "物料", True, "SUM(", 10 ** 12]


def _damage(node, rnd: random.Random, rate: float):
    if isinstance(node, dict):
        for k in list(node):
            if rnd.random() < rate and not isinstance(node[k], (dict, list)):
                node[k] = rnd.choice(JUNK)
            elif rnd.random() < rate / 6 and k not in ("model", "tables", "sections"):
                node.pop(k)
            else:
                _damage(node[k], rnd, rate)
    elif isinstance(node, list):
        for x in node:
            _damage(x, rnd, rate)


@pytest.mark.parametrize("seed", range(int(__import__("os").environ.get("FUZZ_SEEDS", "12"))))
def test_randomly_damaged_report_still_produces_a_workbook(seed, tmp_path):
    rnd = random.Random(seed)
    tm, lay = tmsl(), layout()
    _damage(tm["model"]["tables"], rnd, 0.04)
    _damage(tm["model"].get("relationships") or [], rnd, 0.04)
    for s in lay["sections"]:
        for vc in s["visualContainers"]:
            cfg = json.loads(vc["config"]) if isinstance(vc.get("config"), str) else None
            if cfg is not None:
                _damage(cfg, rnd, 0.03)
                vc["config"] = json.dumps(cfg)
    _run(tm, lay, tmp_path)


def test_pbir_reader_keeps_good_visuals_and_flags_damaged_ones(tmp_path):
    from pbi_insights.extract.layout import parse_report
    from pbi_insights.extract.package import open_package

    base = "Report/definition/pages/p1/"
    col = {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Day"}}
    good = {"name": "v1", "position": {"x": 5, "y": 9, "width": 10, "height": 10}, "visual": {
        "visualType": "tableEx", "query": {"queryState": {"Values": {"projections": [{"field": col}]}},
                                           "sortDefinition": {"sort": [{"field": col, "direction": "Descending"}]}}}}
    path = tmp_path / "P.pbip.pbix"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("DataModelSchema", json.dumps(tmsl()).encode("utf-16-le"))
        z.writestr("Report/definition/report.json", "{}")
        z.writestr(base + "page.json", json.dumps({"name": "p1", "displayName": "Page one"}))
        z.writestr(base + "visuals/v1/visual.json", json.dumps(good))
        z.writestr(base + "visuals/v2/visual.json", json.dumps({"name": "v2", "visual": {"query": ["damaged"]}}))
        z.writestr("Version", "1.28".encode("utf-16-le"))
    rep = parse_report(open_package(path))
    by = {v["container"]: v for v in rep["visuals"]}
    assert (by["v1"]["fields"][0]["table"], by["v1"]["fields"][0]["field"], by["v1"]["x"]) == ("Sales", "Day", 5)
    assert by["v1"]["sort_spec"][0]["direction"] == "DESC" and "_objects" not in by["v1"]
    assert by["v2"]["title"] == "Unreadable visual" and any("could not be read" in w for w in rep["warnings"])
    assert rep["pages"][0]["visual_count"] == 2
