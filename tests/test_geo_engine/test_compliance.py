"""Traceability of each planting to the act and clause behind it.

The brief makes this pass/fail: «объяснение обязательно содержит ссылку на
нормативный акт и конкретный пункт / таблицу / норму отступа», and «"чёрный
ящик" без привязки к нормам не принимается». These tests pin the parts that
would silently degrade — a citation that looks confident but is unverified, a
fallback setback presented as if a norm demanded it, a binding constraint that
is not actually the tightest one.
"""

import csv
import json

from shapely.geometry import LineString, Point, Polygon

from geo_engine.compliance import (
    UNMAPPED_SOURCE_ID,
    explain_items,
    report_payload,
    unverified_sources,
    write_trace_csv,
)
from geo_engine.model import PlantingItem, Utility, Zone
from geo_engine.norms import load_norms


def _tree(x, y):
    return PlantingItem(geometry=Point(x, y), planting_type="tree", species="Липа", score=0.7, rationale="ранжирование")


def _gas(y=0.0):
    return Utility(geometry=LineString([(-100, y), (100, y)]), object_type="gas_pipe")


def test_a_compliant_planting_cites_the_act_and_the_clause():
    norms = load_norms()

    records = explain_items([_tree(0, 20)], [_gas()], [], norms)
    record = records[0]

    assert record.compliant
    assert "СП 42.13330.2016" in record.summary or "743-ПП" in record.summary
    gas_check = next(c for c in record.checks if c.object_type == "gas_pipe")
    assert gas_check.clause
    assert gas_check.required_m == norms.setback_for("gas_pipe", "tree")
    assert gas_check.actual_m == 20.0


def test_a_violation_is_reported_with_the_norm_it_breaks():
    """The brief asks for this explicitly: «если посадка запрещена или отклонена
    алгоритмом вблизи коммуникаций — также фиксируется причина со ссылкой на норму».
    """
    norms = load_norms()
    too_close = norms.setback_for("gas_pipe", "tree") / 2

    records = explain_items([_tree(0, too_close)], [_gas()], [], norms)
    record = records[0]

    assert not record.compliant
    assert "НЕ допустим" in record.summary
    assert record.binding_constraint == "gas_pipe"
    assert not next(c for c in record.checks if c.object_type == "gas_pipe").satisfied


def test_the_binding_constraint_is_the_one_with_least_slack():
    """It is the reason the planting could not move, so it is the clause an
    expert checks first — and it is what goes into the one-line summary.
    """
    norms = load_norms()
    # Building setback is 5.0 m and the wall is 6 m away (slack 1.0);
    # gas is 2.0 m and the main is 20 m away (slack 18.0).
    building = Zone(geometry=Polygon([(-50, 26), (50, 26), (50, 40), (-50, 40)]), zone_type="building")

    records = explain_items([_tree(0, 20)], [_gas()], [building], norms)

    assert records[0].binding_constraint == "building"
    assert "стена здания" in records[0].summary


def test_an_object_type_with_no_citation_says_so_instead_of_inventing_one():
    """setback_for falls back to the largest known setback for unknown types.
    That fallback is ours, not a norm's, and the explanation must not imply
    otherwise — this is the exact failure mode the brief rejects.
    """
    norms = load_norms()
    mystery = Utility(geometry=LineString([(-100, 0), (100, 0)]), object_type="нечто_неизвестное")

    records = explain_items([_tree(0, 50)], [mystery], [], norms)
    check = next(c for c in records[0].checks if c.object_type == "нечто_неизвестное")

    assert check.source_id == UNMAPPED_SOURCE_ID
    assert not check.verified
    assert "не определён" in check.citation


def test_unverified_citations_are_counted_not_hidden():
    """A traceability feature fails quietly when a citation nobody checked looks
    exactly like one that was checked. Power lines are the honest remaining
    gap: СП 42.13330.2016's table 9.1 note 2 defers to ПУЭ, which has not been
    read, so the value applied here is ours and must say so.
    """
    norms = load_norms()
    power_line = Utility(
        geometry=LineString([(-100, 0), (100, 0)]), object_type="power_line_corridor"
    )

    records = explain_items([_tree(0, 3.2)], [power_line], [], norms)
    counts = unverified_sources(records)

    assert records[0].binding_constraint == "power_line_corridor"
    assert any("ПУЭ" in citation for citation in counts)


def test_report_payload_normalizes_sources_instead_of_repeating_them():
    """Act, clause and table row are identical for every planting hitting the
    same rule; repeating them per item cost 3.3 KB per planting on real data.
    """
    norms = load_norms()
    records = explain_items([_tree(0, 20), _tree(5, 25)], [_gas()], [], norms)

    payload = report_payload(records, norms, input="test")

    assert payload["sources"], "источники должны быть вынесены в отдельную таблицу"
    for item in payload["items"]:
        for check in item["checks"]:
            assert check["source"] in payload["sources"], "проверка ссылается на источник по id"
            assert not {"act", "clause", "citation", "table_row"} & set(check), (
                "текст акта/пункта/строки таблицы не должен дублироваться в каждой проверке"
            )

    naive = json.dumps([r.to_dict() for r in records], ensure_ascii=False)
    normalized = json.dumps(payload["items"], ensure_ascii=False)
    assert len(normalized) < len(naive)


def test_trace_csv_has_one_row_per_planting_and_constraint(tmp_path):
    norms = load_norms()
    records = explain_items([_tree(0, 20), _tree(5, 25)], [_gas()], [], norms)

    path = tmp_path / "trace.csv"
    written = write_trace_csv(records, path)

    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert written == len(rows) == sum(len(r.checks) for r in records)
    assert {"act", "clause", "required_m", "actual_m", "satisfied"} <= set(rows[0])
    assert sum(int(r["is_binding"]) for r in rows) == len(records)


def test_no_items_yields_no_records():
    assert explain_items([], [_gas()], [], load_norms()) == []
