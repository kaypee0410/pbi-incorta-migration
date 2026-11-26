"""ALL / REMOVEFILTERS / ALLSELECTED as SQL, and measure dependencies (synthetic model from conftest: Sales -> Store)."""
from __future__ import annotations

from conftest import tmsl
from pbi_insights.analyze.dax_translate import ModelIndex
from pbi_insights.analyze.planner import measure_dependencies
from pbi_insights.extract.model import from_tmsl


def _model(extra: dict, table: str = "Sales") -> dict:
    """The conftest model with extra measures on `table`."""
    tm = tmsl()
    t = next(x for x in tm["model"]["tables"] if x["name"] == table)
    t["measures"] += [{"name": k, "expression": v} for k, v in extra.items()]
    return from_tmsl(tm)


# ------------------------------------------------------------------------------------------- 7. dependencies
def test_dependencies_table_qualified_measure_and_created_columns():
    model = _model({
        "Qualified": "'Sales'[Total] + Sales[LY]",                       # measures written with a table name
        "Column": "SUM(Sales[Amount])",                                  # a real column is not a measure
        "Made": 'SUMX(ADDCOLUMNS(VALUES(Store[Region]), "TY", Sales[Total]), [TY])',
        "Made2": 'VAR t = SELECTCOLUMNS(Store, "LY", [Stores]) RETURN SUMX(t, [LY])',
    })
    deps = measure_dependencies(model, ModelIndex(model))
    assert deps["Qualified"] == ["Total", "LY"]
    assert deps["Column"] == []
    # [TY] / [LY] name the column the measure creates, not the measures TY / LY
    assert deps["Made"] == ["Total"]
    assert deps["Made2"] == ["Stores"]
