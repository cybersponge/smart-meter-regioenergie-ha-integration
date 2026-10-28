"""Tests for the statistic id generation and the meter form validation."""

from datetime import date

import pytest

from custom_components.regioenergie_import.config_flow import (
    _meter_details_schema,
    _validate_meter_input,
    assign_statistic_ids,
    build_statistic_id,
    format_schedule,
    new_meter,
    set_price,
)
from custom_components.regioenergie_import.const import (
    CONF_COST_STATISTIC_ID,
    CONF_METER_NUMBER,
    CONF_METER_TYPE,
    CONF_OBIS_CODE,
    CONF_PRICE,
    CONF_PRICES,
    CONF_STATISTIC_ID,
    CONF_UNIT,
    CONF_VALID_FROM,
    DEFAULT_OBIS_CODE,
    DEFAULT_UNIT_BY_METER_TYPE,
    entry_title,
)
from homeassistant.components.recorder.statistics import valid_statistic_id

VALID_METER = {
    "name": "Water",
    CONF_METER_TYPE: "water",
    CONF_METER_NUMBER: "CH1000000000000000000000000012345",
    CONF_OBIS_CODE: DEFAULT_OBIS_CODE,
    CONF_UNIT: "m³",
}


def test_entry_title_identifies_the_integration_and_account() -> None:
    """A reauthentication issue names its integration and account."""
    assert entry_title("person@example.com") == (
        "Regio Energie Smart Meter Import: person@example.com"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Water", "regioenergie_import:water_import"),
        ("Wasser (Garten)", "regioenergie_import:wasser_garten_import"),
        ("Fernwärme", "regioenergie_import:fernwarme_import"),
        ("   ", "regioenergie_import:unknown_import"),
    ],
)
def test_statistic_ids_are_valid(name: str, expected: str) -> None:
    """Names turn into ids the recorder accepts."""
    statistic_id = build_statistic_id(name, set())

    assert statistic_id == expected
    assert valid_statistic_id(statistic_id)


def test_statistic_ids_do_not_collide() -> None:
    """A second meter with the same name gets its own id."""
    taken = {"regioenergie_import:water_import"}

    second = build_statistic_id("Water", taken)
    third = build_statistic_id("Water", taken | {second})

    assert second == "regioenergie_import:water_2_import"
    assert third == "regioenergie_import:water_3_import"
    assert valid_statistic_id(second)


def test_meter_input_accepts_a_complete_form() -> None:
    """A filled in form reports no errors."""
    assert _validate_meter_input(VALID_METER) == {}


def test_meter_input_rejects_a_unit_that_does_not_fit() -> None:
    """A water meter cannot be counted in kWh."""
    errors = _validate_meter_input({**VALID_METER, CONF_UNIT: "kWh"})

    assert errors == {CONF_UNIT: "invalid_unit"}


def test_meter_input_reports_missing_fields() -> None:
    """Empty required fields are reported per field."""
    errors = _validate_meter_input(
        {**VALID_METER, "name": "", CONF_METER_NUMBER: "", CONF_OBIS_CODE: ""}
    )

    assert errors == {
        "name": "required",
        CONF_METER_NUMBER: "required",
        CONF_OBIS_CODE: "required",
    }


@pytest.mark.parametrize(
    ("meter_type", "unit"),
    [("electricity", "kWh"), ("gas", "m³"), ("water", "m³"), ("heat", "kWh")],
)
def test_every_meter_type_has_a_working_default(meter_type: str, unit: str) -> None:
    """All four meter types can be configured with their default unit."""
    meter = {**VALID_METER, CONF_METER_TYPE: meter_type, CONF_UNIT: unit}

    assert _validate_meter_input(meter) == {}


@pytest.mark.parametrize("meter_type", ["electricity", "gas", "water", "heat"])
def test_the_unit_is_preset_for_the_chosen_type(meter_type: str) -> None:
    """Picking the type fills the unit box with what that meter is billed in."""
    schema = _meter_details_schema(meter_type, {})

    filled = schema(
        {
            "name": "Meter",
            CONF_METER_NUMBER: "CH1",
            CONF_OBIS_CODE: DEFAULT_OBIS_CODE,
            "export_type": "WirkBezug",
        }
    )

    assert filled[CONF_UNIT] == DEFAULT_UNIT_BY_METER_TYPE[meter_type]
    assert _validate_meter_input({**filled, CONF_METER_TYPE: meter_type}) == {}


def test_only_units_of_the_chosen_type_are_offered() -> None:
    """A water meter cannot even be offered kWh."""
    schema = _meter_details_schema("water", {})

    unit_key = next(key for key in schema.schema if str(key) == CONF_UNIT)

    assert schema.schema[unit_key].config["options"] == ["m³", "L"]


def test_a_price_can_be_cleared_again() -> None:
    """An emptied price box must not be refilled with the old value."""
    schema = _meter_details_schema("water", {**VALID_METER, CONF_PRICE: 4.25})

    submitted = schema(
        {
            "name": "Water",
            CONF_METER_NUMBER: "CH1",
            CONF_UNIT: "m³",
            CONF_OBIS_CODE: DEFAULT_OBIS_CODE,
            "export_type": "WirkBezug",
        }
    )

    assert CONF_PRICE not in submitted


def test_the_unit_of_an_existing_meter_is_not_offered() -> None:
    """Changing the unit would mix two units in one history."""
    schema = _meter_details_schema("water", VALID_METER, editing=True)

    keys = {str(key) for key in schema.schema}
    assert CONF_UNIT not in keys
    assert CONF_PRICE not in keys  # prices are managed on their own


def test_the_price_of_a_new_meter_starts_its_schedule() -> None:
    """The price box becomes the first entry of the schedule."""
    meter = new_meter({**VALID_METER, CONF_PRICE: 2.5}, date(2026, 10, 20))

    assert CONF_PRICE not in meter
    assert meter[CONF_PRICES] == [{CONF_VALID_FROM: "2026-10", CONF_PRICE: 2.5}]


def test_a_new_meter_without_price_has_no_schedule() -> None:
    """No price, no cost statistic."""
    assert CONF_PRICES not in new_meter(dict(VALID_METER), date(2026, 10, 20))


def test_a_price_for_the_same_month_replaces_the_old_one() -> None:
    """One entry per month, kept in order."""
    prices = [
        {CONF_VALID_FROM: "2026-05", CONF_PRICE: 0.30},
        {CONF_VALID_FROM: "2026-01", CONF_PRICE: 0.25},
    ]

    prices = set_price(prices, "2026-05", 0.31)
    prices = set_price(prices, "2025-10", 0.20)

    assert prices == [
        {CONF_VALID_FROM: "2025-10", CONF_PRICE: 0.20},
        {CONF_VALID_FROM: "2026-01", CONF_PRICE: 0.25},
        {CONF_VALID_FROM: "2026-05", CONF_PRICE: 0.31},
    ]
    assert format_schedule(prices, "CHF/kWh").splitlines()[0] == (
        "- 2025-10: 0.2 CHF/kWh"
    )


def test_a_new_meter_gets_both_statistic_ids() -> None:
    """Consumption and cost ids are fixed when the meter is created."""
    meter = assign_statistic_ids({**VALID_METER, "name": "Strom"}, set())

    assert meter[CONF_STATISTIC_ID] == "regioenergie_import:strom_import"
    assert meter[CONF_COST_STATISTIC_ID] == "regioenergie_import:strom_cost"
    assert valid_statistic_id(meter[CONF_COST_STATISTIC_ID])
