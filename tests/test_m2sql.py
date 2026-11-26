"""Power Query (M) → Spark SQL: query references become a chain of Incorta objects, checked in DuckDB."""
from __future__ import annotations

import copy
import json
import zipfile

import pytest
import sqlglot

from pbi_insights.incorta.m2sql import PowerQueryGraph, query_refs
from pbi_insights.incorta.mlang import MSyntaxError, parse_m, table_type_columns

ITEM = '''let
    Source = Odbc.Query("dsn=SRCA", "SELECT PARTNO, ""DESC"" FROM SRCA.dbo.ITEMTAB"),
    #"Trimmed Text" = Table.TransformColumns(Source, {{"PARTNO", Text.Trim, type text}, {"DESC", Text.Trim, type text}})
in
    #"Trimmed Text"'''
LOT = '''let
    Source = Odbc.Query("dsn=SRCA", "SELECT LOTTAB.PARTNUM, LOTTAB.QTY, LOTTAB.RECVDATE FROM SRCA.dbo.LOTTAB LOTTAB WHERE (LOTTAB.QTY>$0)"),
    #"Trimmed Text" = Table.TransformColumns(Source, {{"PARTNUM", Text.Trim, type text}}),
    #"Merged Queries" = Table.NestedJoin(#"Trimmed Text", {"PARTNUM"}, Item_SG, {"PARTNO"}, "Item", JoinKind.LeftOuter),
    #"Expanded Item" = Table.ExpandTableColumn(#"Merged Queries", "Item", {"DESC"}, {"DESC"}),
    #"Replaced Value" = Table.ReplaceValue(#"Expanded Item", null, "-", Replacer.ReplaceValue, {"DESC"}),
    #"Added Date" = Table.AddColumn(#"Replaced Value", "RECV DATE", each
        let t = Text.From([RECVDATE]), d = Text.End(t, 2) & "/" & Text.Middle(t, 4, 2) & "/" & Text.Start(t, 4)
        in try Date.FromText(d, "en-GB") otherwise null),
    #"Added Country" = Table.AddColumn(#"Added Date", "COUNTRY", each "SINGAPORE")
in
    #"Added Country"'''
LOT_MY = '''let
    Source = Odbc.Query("dsn=SRCB", "SELECT PARTNUM, QTY FROM SRCB.dbo.LOTTAB"),
    #"Added Country" = Table.AddColumn(Source, "COUNTRY", each "Malaysia")
in
    #"Added Country"'''
ZONE = '''let
    Source = Table.Combine({Lot_SG, Lot_MY}),
    Pairs = List.Zip({{"Malaysia", "MALAYSIA"}, {"MALAYSIA", "MY"}}),
    Fixed = List.Accumulate(Pairs, Source, (state, pair) => Table.ReplaceValue(state, pair{0}, pair{1}, Replacer.ReplaceValue, {"COUNTRY"})),
    #"Added Value" = Table.AddColumn(Fixed, "VALUE", each [QTY] * 2),
    #"Filtered" = Table.SelectRows(#"Added Value", each [QTY] <> 0)
in
    #"Filtered"'''
DIM = '''let
    Source = Table.Combine({Lot_MY, Lot_SG}),
    #"Removed Duplicates" = Table.Distinct(Source, {"PARTNUM"})
in
    #"Removed Duplicates"'''
QUERIES = {"Item_SG": ITEM, "Lot_SG": LOT, "Lot_MY": LOT_MY, "ZONE": ZONE, "DimItem": DIM}
OUTPUTS = {
    "Item_SG": [("PARTNO", "PARTNO", "text"), ("DESC", "DESC", "text")],
    "Lot_SG": [(c, c, t) for c, t in (("PARTNUM", "text"), ("QTY", "number"), ("RECVDATE", "number"),
                                      ("DESC", "text"), ("RECV DATE", "date"), ("COUNTRY", "text"))],
    "Lot_MY": [(c, c, t) for c, t in (("PARTNUM", "text"), ("QTY", "number"), ("COUNTRY", "text"))],
    "ZONE": [(c, c, t) for c, t in (("PARTNUM", "text"), ("QTY", "number"), ("DESC", "text"),
                                     ("RECV DATE", "date"), ("COUNTRY", "text"), ("VALUE", "number"))],
    "DimItem": [(c, c, t) for c, t in (("PARTNUM", "text"), ("COUNTRY", "text"))],
}


@pytest.fixture()
def graph():
    g = PowerQueryGraph(QUERIES, OUTPUTS, "S")
    g.translate_all()
    return g


def test_query_references_and_levels(graph):
    assert query_refs("ZONE", ZONE, QUERIES) == ["Lot_SG", "Lot_MY"]
    assert graph.upstream(["ZONE"]) == ["Item_SG", "Lot_SG", "Lot_MY"]
    assert [graph.depth(q) for q in ("Item_SG", "Lot_SG", "ZONE")] == [0, 1, 2]
    assert all(not r.unsupported for r in graph.results.values()), {q: r.unsupported for q, r in graph.results.items()}
    lot = graph.results["Lot_SG"]
    assert lot.raw_columns == ["PARTNUM", "QTY", "RECVDATE"] and lot.sources[0].target == "SRCA"
    assert graph.object_name("Lot_SG") == "Lot_SG_MV" and graph.raw["Lot_SG"] == "Lot_SG"
    assert "S.Item_SG_MV" in lot.sql and "LEFT JOIN" in lot.sql and "IS NOT DISTINCT FROM" in lot.sql
    zone = graph.results["ZONE"].sql
    assert "UNION ALL" in zone and "S.Lot_SG_MV" in zone and "S.Lot_MY_MV" in zone and "COUNT(*)" not in zone
    for r in graph.results.values():
        if r.sql:
            sqlglot.parse_one(r.sql, read="spark")


def test_the_chain_runs_layer_by_layer_like_power_query(graph):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("duckdb")
    from pbi_insights.incorta.verify import run_chain

    raw = {"Item_SG": pd.DataFrame({"PARTNO": [" A1 ", "B2"], "DESC": [" Soap", None]}),
           "Lot_SG": pd.DataFrame({"PARTNUM": ["A1 ", "B2", "C3"], "QTY": [5.0, 0.0, 2.0],
                                   "RECVDATE": [20250131.0, 20241105.0, 99.0]}),
           "Lot_MY": pd.DataFrame({"PARTNUM": ["A1", "D4"], "QTY": [1.0, 3.0]})}
    out = run_chain(graph, raw)
    lot = out["Lot_SG"].sort_values("PARTNUM").reset_index(drop=True)
    assert lot["DESC"].tolist() == ["Soap", "-", "-"]                         # join on trimmed keys, null -> "-"
    assert [str(x)[:10] for x in lot["RECV_DATE"]] == ["2025-01-31", "2024-11-05", "NaT"]   # en-GB text date
    zone = out["ZONE"]
    assert len(zone) == 4                                                   # QTY 0 filtered out
    assert sorted(zone["COUNTRY"]) == ["MY", "MY", "SINGAPORE", "SINGAPORE"]   # chained replacements
    assert zone.loc[zone["COUNTRY"] == "MY", "DESC"].isna().all()          # union by name: NULL where missing
    assert sorted(zone["VALUE"]) == [2.0, 4.0, 6.0, 10.0]
    dim = out["DimItem"].set_index("PARTNUM")["COUNTRY"].to_dict()
    assert dim["A1"] == "Malaysia"                                           # first Table.Combine input wins


def test_file_sources_keep_file_steps_and_name_columns_from_the_header():
    code = '''let
    Source = Folder.Files("C:\\\\Reports\\\\Aging"),
    Sorted = Table.Sort(Source, {{"Date modified", Order.Descending}}),
    Latest = Sorted{0}[Content],
    WB = Excel.Workbook(Latest, null, true),
    Sheet = WB{[Item = "Sheet1", Kind = "Sheet"]}[Data],
    #"Removed Top Rows" = Table.Skip(Sheet, 2),
    #"Promoted Headers" = Table.PromoteHeaders(#"Removed Top Rows", [PromoteAllScalars=true]),
    #"Renamed" = Table.RenameColumns(#"Promoted Headers", {{"料号", "PRODUCT CODE"}, {"qty", "QTY"}}),
    #"Typed" = Table.TransformColumnTypes(#"Renamed", {{"QTY", Int64.Type}})
in
    #"Typed"'''
    g = PowerQueryGraph({"Aging": code}, {"Aging": [("PRODUCT CODE", "PRODUCT CODE", "text"), ("QTY", "QTY", "number")]}, "S")
    r = g.translate_all()["Aging"]
    assert not r.unsupported, r.unsupported
    assert r.raw_columns == ["料号", "qty"] and g.raw_column("Aging", "料号") == "PRODUCT_CODE"
    assert any("newest file" in x for x in r.file_steps) and any("Sheet1" in x for x in r.file_steps)
    assert any("skips the first 2" in x for x in r.file_steps)
    assert "CAST(qty AS BIGINT) AS QTY" in r.sql and "FROM S.Aging" in r.sql


def test_m_parser_handles_generalized_names_and_unicode():
    ast = parse_m('let #"A b" = Table.SelectRows(x, each [RECV DATE] <> null and [数量] > 0) in #"A b"')
    assert ast[0] == "let" and ast[1][0][0] == "A b"


@pytest.mark.parametrize("name", ["No. of Units", "BATCH NO.", "No.1", "No. 1", "Ver. 2.0", "Rate 0.10",
                                  "Request L/T", "Sub-Total", "A  B"])
def test_m_parser_keeps_field_names_as_written(name):
    assert parse_m(f"each [{name}]") == ("each", ("field", None, name, False))
    assert parse_m(f"each [ {name} ]?") == ("each", ("field", None, name, True))
    assert parse_m(f"each r[{name}]") == ("each", ("field", ("id", "r"), name, False))
    assert parse_m(f"[{name} = 1]") == ("record", [(name, ("num", 1))])
    assert parse_m(f'each [#"{name}"]') == parse_m(f"each [{name}]")


@pytest.mark.parametrize("code", ["each [A] . [B]", "let a = . in a", "each [A\nB]", "each [A /* x */ B]", "each [A .. B]"])
def test_m_parser_still_rejects_stray_dots_and_broken_names(code):
    with pytest.raises(MSyntaxError):
        parse_m(code)


def test_a_dotted_column_name_does_not_stop_the_translation():
    import pandas as pd

    from pbi_insights.incorta.verify import run_chain

    raw = '''let
    Source = Odbc.Query("dsn=X", "SELECT ID, ORDER_DATE, SHIP_DATE FROM T")
in
    Source'''
    zone = '''let
    Source = Table.Combine({Ship_SG, Ship_MY}),
    /* 失效/结果列 */
    #"Added Custom" = Table.AddColumn(Source, "No. of Units",
        each Duration.Days([SHIP_DATE] - [ORDER_DATE]) - 2, Int64.Type),
    #"Added Custom1" = Table.AddColumn(#"Added Custom", "Check (Fail/Pass)",
        each if [No. of Units] < 0 then "Pass" else "Fail", type text)
in
    #"Added Custom1"'''
    cols = [("ID", "ID", "number"), ("ORDER_DATE", "ORDER_DATE", "date"), ("SHIP_DATE", "SHIP_DATE", "date")]
    g = PowerQueryGraph({"Ship_SG": raw, "Ship_MY": raw, "Zone": zone},
                        {"Ship_SG": cols, "Ship_MY": cols,
                         "Zone": [("ID", "ID", "number"), ("No. of Units", "No_of_Units", "number"),
                                   ("Check (Fail/Pass)", "Check_Fail_Pass", "text")]}, "S")
    g.translate_all()
    r = g.results["Zone"]
    assert r.unsupported == [] and r.steps == 3 and r.refs == ["Ship_SG", "Ship_MY"]
    sqlglot.parse_one(r.sql, read="spark")
    d = pd.Timestamp
    out = run_chain(g, {
        "Ship_SG": pd.DataFrame({"ID": [1, 2], "ORDER_DATE": [d("2025-01-01")] * 2,
                                 "SHIP_DATE": [d("2025-01-02"), d("2025-01-03")]}),
        "Ship_MY": pd.DataFrame({"ID": [3], "ORDER_DATE": [d("2025-01-01")], "SHIP_DATE": [d("2025-01-06")]})})["Zone"]
    rows = sorted(out[["ID", "No_of_Units", "Check_Fail_Pass"]].itertuples(index=False, name=None))
    assert rows == [(1, -1, "Pass"), (2, 0, "Fail"), (3, 3, "Fail")]


@pytest.mark.parametrize("code, ast", [
    ("{1..5}", ("list", [("range", ("num", 1), ("num", 5))])),
    ("{1.5, .5}", ("list", [("num", 1.5), ("num", 0.5)])),
    ("t[[a], [No. of x]]", ("proj", ("id", "t"), ["a", "No. of x"], False)),
    ("a ?? 0", ("coalesce", ("id", "a"), ("num", 0))),
    ("try a catch (e) => 0", ("try", ("id", "a"), ("num", 0))),
    ('"a#(cr,lf)b#(00A0)#(#)"', ("str", "a\r\nb\u00a0#")),
    ("type function (a as text) as number", ("type", "function (a as text) as number")),
    ("...", ("error", ("str", "not implemented (...)"))),
])
def test_m_parser_syntax(code, ast):
    assert parse_m(code) == ast


@pytest.mark.parametrize("code, message", [
    ('let a = "x in a', "closing quote"), ("let a = 1 /* x in a", "closing */"), ('"#(zz)"', "unknown text escape"),
    ("let\n  a = 1,\n  b = c d\nin b", "line 3")])
def test_m_parser_errors_say_what_and_where(code, message):
    with pytest.raises(MSyntaxError) as ex:
        parse_m(code)
    assert message in str(ex.value) and "line" in str(ex.value)


def test_table_type_columns():
    assert table_type_columns('table [No. of Units = Int64.Type, #"B b" = nullable text]') == \
        [("No. of Units", "Int64.Type"), ("B b", "nullable text")]


SQL = 'let Source = Odbc.Query("dsn=X", "SELECT {} FROM T") in Source'


def _graph(queries: dict, outputs: dict, **kw):
    import re
    g = PowerQueryGraph(queries, {q: [(c, re.sub(r"\W+", "_", c).strip("_"), t) for c, t in cols]
                                  for q, cols in outputs.items()}, "S", **kw)
    g.translate_all()
    for r in g.results.values():
        assert r.unsupported == []
        if r.sql:
            sqlglot.parse_one(r.sql, read="spark")
    return g


def _rows(g, raw: dict, query: str) -> list:
    from pbi_insights.incorta.verify import run_chain

    df = run_chain(g, raw)[query].astype(object)
    df = df.where(df.notna(), None)
    return sorted(df.itertuples(index=False, name=None), key=repr)


def _unsupported(code: str, outputs, **kw) -> str:
    g = PowerQueryGraph({"Q": code}, {"Q": [(c, c, "text") for c in outputs]}, "S", **kw)
    g.translate_all()
    assert g.results["Q"].sql == "" and g.results["Q"].unsupported
    return g.results["Q"].unsupported[0][1]


NAV = '''let
    Source = Odbc.DataSource("dsn=X", [HierarchicalNavigation=true]),
    Db = Source{[Name="DB",Kind="Database"]}[Data],
    Dbo = Db{[Name="dbo",Kind="Schema"]}[Data],
    '''


def test_database_navigation_and_pivot():
    import pandas as pd

    g = _graph({"Opt": NAV + '''T = Dbo{[Name="Cust Opt",Kind="Table"]}[Data],
    Sel = Table.SelectColumns(T, {"ID", "FIELD", "VALUE"}),
    Trim = Table.TransformColumns(Sel, {{"FIELD", Text.Trim, type text}}),
    Rows = Table.SelectRows(Trim, each [FIELD] = "BR" or [FIELD] = "GR"),
    Piv = Table.Pivot(Rows, List.Distinct(Rows[FIELD]), "FIELD", "VALUE"),
    Ren = Table.RenameColumns(Piv, {{"BR", "BRAND"}})
in
    Ren'''}, {"Opt": [("ID", "text"), ("BRAND", "text"), ("GR", "text")]})
    src = g.results["Opt"].sources[0]
    assert (src.kind, src.target, src.item) == ("odbc", "X", 'DB.dbo."Cust Opt"')
    assert src.native_sql == 'SELECT "ID" AS ID, "FIELD" AS FIELD, "VALUE" AS VALUE FROM DB.dbo."Cust Opt"'
    raw = pd.DataFrame({"ID": ["1", "1", "2", "3"], "FIELD": ["BR ", "GR", "GR", "ZZ"], "VALUE": ["a", "b", "c", "d"]})
    assert _rows(g, {"Opt": raw}, "Opt") == [("1", "a", "b"), ("2", None, "c")]


def test_pivot_on_values_from_the_data_is_flagged():
    code = NAV + '''T = Dbo{[Name="T",Kind="Table"]}[Data],
    Sel = Table.SelectColumns(T, {"ID", "F", "V"}),
    Piv = Table.Pivot(Sel, List.Distinct(Sel[F]), "F", "V") in Piv'''
    assert "come from the data" in _unsupported(code, ["ID", "A"])


def test_two_tables_of_one_database_become_two_physical_tables():
    import pandas as pd

    g = _graph({"PO": NAV + '''L = Dbo{[Name="LINES",Kind="Table"]}[Data],
    H = Dbo{[Name="HEAD",Kind="Table"]}[Data],
    L1 = Table.SelectColumns(L, {"K", "ITEM"}),
    H1 = Table.SelectColumns(H, {"K", "VENDOR"}),
    J = Table.NestedJoin(L1, {"K"}, H1, {"K"}, "H", JoinKind.LeftOuter),
    E = Table.ExpandTableColumn(J, "H", {"VENDOR"}, {"VENDOR"})
in
    E'''}, {"PO": [("ITEM", "text"), ("VENDOR", "text")]})
    assert g.refs["PO"] == ["PO LINES", "PO HEAD"] and g.results["PO LINES"].passthrough
    assert "FROM DB.dbo.HEAD" in g.results["PO HEAD"].sources[0].native_sql
    raw = {"PO LINES": pd.DataFrame({"K": [1, 2], "ITEM": ["a", "b"]}), "PO HEAD": pd.DataFrame({"K": [1], "VENDOR": ["v"]})}
    from pbi_insights.incorta.verify import run_chain
    g.order()
    out = run_chain(g, raw)
    assert len(out["PO LINES"]) == 2 and len(out["PO HEAD"]) == 1
    two = NAV + 'A = Dbo{[Name="A",Kind="Table"]}[Data], B = Dbo{[Name="B",Kind="Table"]}[Data],\n' \
        'C = Table.Join(Table.RenameColumns(A, {{"K", "J"}}), {"J"}, B, {"K"}), D = Table.SelectColumns(C, {"J"}) in D'
    g = PowerQueryGraph({"Q": two}, {"Q": [("J", "J", "text")]}, "S")
    g.translate_all()
    assert g.results["Q"].unsupported                       # columns not named per table: flagged, not guessed


def test_folder_of_workbooks_opened_by_a_column_function():
    g = _graph({"F": '''let
    Source = Folder.Files("C:\\x"),
    Visible = Table.SelectRows(Source, each [Attributes]?[Hidden]? <> true),
    Wb = Table.AddColumn(Visible, "WB", each Excel.Workbook([Content], null, true)),
    Sheets = Table.ExpandTableColumn(Wb, "WB", {"Name","Data","Kind"}, {"SheetName","SheetData","Kind"}),
    Keep = Table.SelectRows(Sheets, each [Kind] = "Sheet" and [SheetData] <> null),
    Head = Table.AddColumn(Keep, "T", each Table.PromoteHeaders([SheetData], [PromoteAllScalars=true])),
    Data = Table.ExpandTableColumn(Head, "T", {"ID", "QTY"}),
    Only = Table.SelectRows(Data, each Text.Trim([Kind]) = "Sheet" and [QTY] <> null),
    Typed = Table.TransformColumnTypes(Only, {{"QTY", Int64.Type}})
in
    Typed'''}, {"F": [("ID", "text"), ("QTY", "number")]})
    assert g.results["F"].raw_columns == ["QTY", "ID"] or set(g.results["F"].raw_columns) == {"ID", "QTY"}
    import pandas as pd
    raw = pd.DataFrame({"ID": ["a", "b"], "QTY": ["1", None]})
    assert _rows(g, {"F": raw}, "F") == [("a", 1)]


TRANSFORM = '''let
    Source = (Parameter1 as binary) => let
        Source = Csv.Document(Parameter1,[Delimiter=",", Columns=2]),
        Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])
    in
        Promoted
in
    Source'''


def test_power_bi_combine_files_pattern():
    import pandas as pd

    code = '''let
    Source = Folder.Files("C:\\x"),
    Invoke = Table.AddColumn(Source, "Transform File", each #"Transform File"([Content])),
    Ren = Table.RenameColumns(Invoke, {"Name", "Source.Name"}),
    Sel = Table.SelectColumns(Ren, {"Source.Name", "Transform File"}),
    Exp = Table.ExpandTableColumn(Sel, "Transform File", Table.ColumnNames(#"Transform File"(#"Sample File"))),
    Typed = Table.TransformColumnTypes(Exp, {{"QTY", Int64.Type}})
in
    Typed'''
    helpers = {"Sample File": 'let Source = Folder.Files("C:\\x"), N = Source{0}[Content] in N',
               "Parameter1": '#"Sample File" meta [IsParameterQuery=true, Type="Binary"]',
               "Transform Sample File": 'let Source = Csv.Document(Parameter1,[Delimiter=","]) in Source'}
    g = _graph({"F": code, **helpers}, {"F": [("ID", "text"), ("QTY", "number")]}, functions={"Transform File": TRANSFORM})
    assert g.refs["F"] == []                                # the sample file is a preview, not data
    assert [g.results[h].helper for h in helpers] == [True] * 3
    assert _rows(g, {"F": pd.DataFrame({"ID": ["a"], "QTY": ["7"]})}, "F") == [("a", 7)]
    g = PowerQueryGraph({"F": code, **helpers}, {"F": [(c, c, "text") for c in ("Source.Name", "ID")]}, "S",
                        functions={"Transform File": TRANSFORM})
    g.translate_all()
    assert "file name" in g.results["F"].unsupported[0][1]   # the file name as data: flagged


def test_header_below_title_rows():
    g = _graph({"F": '''let
    Source = Excel.Workbook(File.Contents("a.xlsx"), null, true),
    Sheet = Source{[Item="S",Kind="Sheet"]}[Data],
    P1 = Table.PromoteHeaders(Sheet, [PromoteAllScalars=true]),
    Rem = Table.RemoveColumns(P1, {"Column1"}),
    Rows = Table.SelectRows(Rem, each [Column3] <> null),
    P2 = Table.PromoteHeaders(Rows, [PromoteAllScalars=true]),
    J = Table.NestedJoin(P2, {"CUR"}, FX, {"CUR"}, "FX", JoinKind.LeftOuter),
    E = Table.ExpandTableColumn(J, "FX", {"RATE"}, {"RATE"}),
    Late = Table.SelectRows(E, each [CODE] <> "x")
in
    Late''', "FX": SQL.format("CUR, RATE")},
               {"F": [("CODE", "text"), ("CUR", "text"), ("RATE", "number")], "FX": [("CUR", "text"), ("RATE", "number")]})
    assert set(g.results["F"].raw_columns) == {"CUR", "CODE"}
    assert any(d[0] == "rows above the header" for d in g.results["F"].decisions)
    assert "Table.Skip" in _unsupported('''let Source = Csv.Document(File.Contents("a.csv")),
        P = Table.PromoteHeaders(Source), S = Table.Skip(P, 5) in S''', ["A"])


def test_row_functions_follow_power_query_nulls():
    import pandas as pd

    g = _graph({"R": SQL.format("ID, A, B, S"), "Q": '''let
    Source = R,
    M = [Jan=1, Feb=2],
    C1 = Table.AddColumn(Source, "PF", each if [A] < 0 then "Pass" else "Fail", type text),
    C2 = Table.AddColumn(C1, "IN", each not List.Contains({1, 2}, [A]), type logical),
    C3 = Table.AddColumn(C2, "MO", each try Record.Field(M, [B]) otherwise null, Int64.Type),
    C4 = Table.AddColumn(C3, "CO", each [A] ?? 9, Int64.Type),
    C5 = Table.AddColumn(C4, "CL", each Text.Clean([S]), type text)
in
    C5'''}, {"R": [("ID", "text"), ("A", "number"), ("B", "text"), ("S", "text")],
             "Q": [("ID", "text"), ("PF", "text"), ("IN", "logical"), ("MO", "number"), ("CO", "number"), ("CL", "text")]})
    raw = pd.DataFrame({"ID": ["1", "2", "3"], "A": [-1.0, 1.0, None], "B": ["Feb", "Feb ", None],
                        "S": ["a\tb\nc", "d", None]})
    assert _rows(g, {"R": raw}, "Q") == [("1", "Pass", True, 2, -1, "abc"), ("2", "Fail", False, None, 1, "d"),
                                         ("3", None, True, None, 9, None)]


def test_weekday_count_matches_a_calendar():
    import pandas as pd

    g = _graph({"R": SQL.format("S, E"), "Q": '''let
    Source = R,
    C = Table.AddColumn(Source, "N", each if [E] = null then null else List.Count(List.Select(
        List.Dates([S], Duration.Days([E] - [S]) + 1, #duration(1,0,0,0)), each Date.DayOfWeek(_, Day.Monday) < 5)), Int64.Type)
in
    C'''}, {"R": [("S", "date"), ("E", "date")], "Q": [("S", "date"), ("E", "date"), ("N", "number")]})
    days = pd.date_range("2024-02-26", periods=10)
    pairs = [(a, b) for a in days for b in days if b >= a - pd.Timedelta(days=1)]
    raw = pd.DataFrame(pairs, columns=["S", "E"])
    for s, e, n in _rows(g, {"R": raw}, "Q"):
        assert n == len(pd.bdate_range(pd.Timestamp(s), pd.Timestamp(e)))


def test_replace_value_per_row_and_group_functions():
    import pandas as pd

    g = _graph({"R": SQL.format("K, C, V, D"), "Q": '''let
    Source = R,
    Rep = Table.ReplaceValue(Source, each [C], each if [K] = "a" and [C] = null then "AU" else [C],
                             Replacer.ReplaceValue, {"C"}),
    G = Table.Group(Rep, {"K", "C"}, {
        {"V", each List.Sum(List.Transform([V], Number.From)), type number},
        {"D", each try List.First(List.RemoveNulls([D])) otherwise null, type text},
        {"N", each List.Count([D]), Int64.Type}})
in
    G'''}, {"R": [("K", "text"), ("C", "text"), ("V", "text"), ("D", "text")],
             "Q": [("K", "text"), ("C", "text"), ("V", "number"), ("D", "text"), ("N", "number")]})
    raw = pd.DataFrame({"K": ["a", "a", "b"], "C": [None, "AU", None], "V": ["1", "2", "3"], "D": [None, "x", "y"]})
    assert _rows(g, {"R": raw}, "Q") == [("a", "AU", 3, "x", 2), ("b", None, 3, "y", 1)]
    assert any("one D per K, C" == d[0] for d in g.results["Q"].decisions)


def test_split_by_digit_to_letter_and_table_literal():
    import pandas as pd

    g = _graph({"R": SQL.format("V"), "Q": '''let
    Source = R,
    S = Table.SplitColumn(Source, "V", Splitter.SplitTextByCharacterTransition({"0".."9"},
        (c) => not List.Contains({"0".."9"}, c)), {"V.1", "V.2"})
in
    S''', "T": '''let Source = #table(type table [A = text, B = Int64.Type], {{"x", 1}, {"y", 2}}) in Source'''},
               {"R": [("V", "text")], "Q": [("V.1", "text"), ("V.2", "text")], "T": [("A", "text"), ("B", "number")]})
    raw = pd.DataFrame({"V": ["V001 Acme 2 Ltd", "Acme", "12"]})
    assert _rows(g, {"R": raw}, "Q") == [("12", None), ("Acme", None), ("V001", " Acme 2")]
    assert _rows(g, {"R": raw}, "T") == [("x", 1), ("y", 2)]


def test_select_star_without_steps_takes_the_loaded_columns():
    g = _graph({"R": SQL.format("*")}, {"R": [("A", "text"), ("B", "number")]})
    assert g.results["R"].passthrough and g.results["R"].raw_columns == ["A", "B"]


def test_outputs_no_step_produces_are_flagged_and_line_breaks_in_names_match():
    import pandas as pd

    assert "no step produces" in _unsupported(
        'let Source = Odbc.Query("dsn=X", "SELECT A FROM T"), B = Table.AddColumn(Source, "C", each [A] & "x") in B', ["A", "ZZ"])
    g = _graph({"R": SQL.format("A"), "Q": '''let Source = R,
        B = Table.AddColumn(Source, "Qty #(lf)%", each [A] & "x", type text) in B'''},
               {"R": [("A", "text")], "Q": [("A", "text"), ("Qty %", "text")]})
    assert _rows(g, {"R": pd.DataFrame({"A": ["1"]})}, "Q") == [("1", "1x")]


def _model_with_derived_query():
    from conftest import layout, tmsl

    tm = tmsl()
    tm["model"]["tables"].append({
        "name": "SalesClean",
        "columns": [{"name": n, "dataType": t, "sourceColumn": n} for n, t in
                    (("StoreID", "int64"), ("Amount", "double"), ("Band", "string"))],
        "partitions": [{"name": "p", "source": {"type": "m", "expression": [
            "let",
            "    Source = Sales,",
            "    #\"Filtered\" = Table.SelectRows(Source, each [Amount] > 0),",
            "    #\"Added Band\" = Table.AddColumn(#\"Filtered\", \"Band\", each if [Amount] > 15 then \"High\" else \"Low\"),",
            "    #\"Kept\" = Table.SelectColumns(#\"Added Band\", {\"StoreID\", \"Amount\", \"Band\"})",
            "in", "    #\"Kept\""]}}],
        "measures": [{"name": "Clean Total", "expression": "SUM(SalesClean[Amount])"}]})
    lay = layout()
    froms = [{"Name": "sc", "Entity": "SalesClean", "Type": 0}]
    sel = [{"Column": {"Expression": {"SourceRef": {"Source": "sc"}}, "Property": "Band"}, "Name": "SalesClean.Band"},
           {"Measure": {"Expression": {"SourceRef": {"Source": "sc"}}, "Property": "Clean Total"},
            "Name": "SalesClean.Clean Total"}]
    cfg = {"name": "v9", "layouts": [{"position": {"x": 10, "y": 1400, "width": 300, "height": 200}}],
           "singleVisual": {"visualType": "clusteredColumnChart",
                            "projections": {"Category": [{"queryRef": "SalesClean.Band", "active": True}],
                                            "Y": [{"queryRef": "SalesClean.Clean Total"}]},
                            "prototypeQuery": {"Version": 2, "From": froms, "Select": sel}}}
    lay["sections"][0]["visualContainers"].append({"x": 10, "y": 1400, "z": 9, "width": 300, "height": 200,
                                                   "config": json.dumps(cfg), "filters": "[]"})
    return tm, lay


def test_plan_builds_the_query_chain(tmp_path):
    from pbi_insights.incorta.package import write_package
    from pbi_insights.incorta.plan import build_plan
    from pbi_insights.pipeline import Options, analyze

    tm, lay = _model_with_derived_query()
    path = tmp_path / "Chain Demo.pbit"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("DataModelSchema", json.dumps(tm).encode("utf-16-le"))
        z.writestr("Report/Layout", json.dumps(lay).encode("utf-16-le"))
        z.writestr("Version", "1.28".encode("utf-16-le"))
    data = analyze(path, Options())
    plan = build_plan(data)
    schema = plan["summary"]["schema"]
    mv = next(m for m in plan["mvs"] if m["pbi_table"] == "SalesClean")
    assert mv["kind"] == "Power Query MV" and mv["name"] == "SalesClean_MV"
    assert mv["reads"] == [f"{schema}.Sales"]                     # Sales stays a plain table (light query)
    assert "WHERE" in mv["sql"] and "CASE" in mv["sql"] and "COUNT(*)" not in mv["sql"]
    sqlglot.parse_one(mv["sql"], read="spark")
    sales = next(s for s in plan["source_map"] if s["pbi_table"] == "Sales")
    assert sales["kind"] == "Physical table (SQL)" and not sales.get("pq_mv")
    lin = {x["query"]: x for x in plan["pq_lineage"]}
    assert lin["SalesClean"]["role"] == "report table" and lin["SalesClean"]["reads_queries"] == ["Sales"]
    clean = next(s for s in plan["source_map"] if s["pbi_table"] == "SalesClean")
    assert clean["kind"] == "Materialized view (Power Query)" and clean["incorta_table"] == ""
    pkg = write_package(copy.deepcopy(plan), data, tmp_path)
    names = set(pkg["files"])
    assert any(n.endswith("__salesclean_mv.sql") for n in names)
    assert not any(n.endswith("__salesclean.sql") for n in names)    # no physical table, no row-count "MV"
    assert any(n.endswith("_pq_lineage.png") for n in names)
