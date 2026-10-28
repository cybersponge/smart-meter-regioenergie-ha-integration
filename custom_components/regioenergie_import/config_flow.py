"""Config and options flow for the Regio Energie Smart Meter Import."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import slugify

from . import statistic_ids_of
from .api import (
    RegioEnergieAuthError,
    RegioEnergieClient,
    RegioEnergieConnectionError,
    RegioEnergieError,
)
from .const import (
    CONF_BASE_URL,
    CONF_COST_STATISTIC_ID,
    CONF_DAYS_TO_FETCH,
    CONF_EXPORT_TYPE,
    CONF_HISTORY_DAYS,
    CONF_LOGIN_URL,
    CONF_METER_NUMBER,
    CONF_METER_TYPE,
    CONF_METERS,
    CONF_OBIS_CODE,
    CONF_PASSWORD_FIELD,
    CONF_PRICE,
    CONF_PRICES,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_STATISTIC_ID,
    CONF_UNIT,
    CONF_USERNAME_FIELD,
    CONF_VALID_FROM,
    DEFAULT_BASE_URL,
    DEFAULT_DAYS_TO_FETCH,
    DEFAULT_EXPORT_TYPE,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_OBIS_CODE,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_UNIT_BY_METER_TYPE,
    DOMAIN,
    MAX_DAYS_TO_FETCH,
    MAX_HISTORY_DAYS,
    MAX_SCAN_INTERVAL_MINUTES,
    METER_TYPE_ELECTRICITY,
    METER_TYPES,
    MIN_DAYS_TO_FETCH,
    MIN_HISTORY_DAYS,
    MIN_SCAN_INTERVAL_MINUTES,
    PORTAL_TIMEZONE,
    UNITS_BY_METER_TYPE,
    entry_title,
)
from .coordinator import (
    cost_statistic_id_for,
    cost_statistic_id_of,
    month_key,
    sorted_schedule,
)

_LOGGER = logging.getLogger(__name__)

# Window used to check that a meter actually returns data during setup.
PROBE_DAYS = 14

CONF_DELETE_HISTORY = "delete_history"
CONF_ENTRIES = "entries"

_LOGIN_OPTION_KEYS = (CONF_LOGIN_URL, CONF_USERNAME_FIELD, CONF_PASSWORD_FIELD)


class InvalidMeter(Exception):
    """The portal answered, but not with usable readings for this meter."""


def _today() -> date:
    """Return today's date in Swiss local time."""
    return datetime.now(ZoneInfo(PORTAL_TIMEZONE)).date()


def build_statistic_id(name: str, taken: set[str]) -> str:
    """Build a unique, stable statistic id for a meter."""
    base = slugify(name) or "meter"
    candidate = f"{DOMAIN}:{base}_import"
    index = 2
    while candidate in taken:
        candidate = f"{DOMAIN}:{base}_{index}_import"
        index += 1
    return candidate


def assign_statistic_ids(meter: dict[str, Any], taken: set[str]) -> dict[str, Any]:
    """Give a new meter its permanent consumption and cost statistic ids."""
    statistic_id = build_statistic_id(meter[CONF_NAME], taken)
    return meter | {
        CONF_STATISTIC_ID: statistic_id,
        CONF_COST_STATISTIC_ID: cost_statistic_id_for(statistic_id),
    }


def new_meter(meter: dict[str, Any], today: date) -> dict[str, Any]:
    """Turn the price box of a new meter into the start of its schedule.

    The month of the first entry hardly matters: months before it use its
    price as well, so it also prices the history imported for the meter.
    """
    result = {key: value for key, value in meter.items() if key != CONF_PRICE}
    if meter.get(CONF_PRICE) is not None:
        result[CONF_PRICES] = [
            {CONF_VALID_FROM: month_key(today), CONF_PRICE: float(meter[CONF_PRICE])}
        ]
    return result


def set_price(
    schedule: list[dict[str, Any]], month: str, price: float
) -> list[dict[str, Any]]:
    """Add a price from a month on, replacing an entry of the same month."""
    kept = [entry for entry in schedule if entry[CONF_VALID_FROM] != month]
    return sorted_schedule([*kept, {CONF_VALID_FROM: month, CONF_PRICE: price}])


def format_schedule(schedule: list[dict[str, Any]], unit_label: str) -> str:
    """Render a price schedule as lines for a form description."""
    if not schedule:
        return "-"
    return "\n".join(
        f"- {entry[CONF_VALID_FROM]}: {entry[CONF_PRICE]:g} {unit_label}"
        for entry in sorted_schedule(schedule)
    )


def _meter_placeholders(hass: HomeAssistant, meter_type: str) -> dict[str, str]:
    """Return the currency and unit the price fields are asked in."""
    return {
        "currency": hass.config.currency or "",
        "unit": DEFAULT_UNIT_BY_METER_TYPE[meter_type],
    }


def _login_options(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Return the advanced login fields that were filled in."""
    return {
        key: str(user_input.get(key, "")).strip()
        for key in _LOGIN_OPTION_KEYS
        if str(user_input.get(key, "")).strip()
    }


def _credentials_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Return the schema of the account step."""
    return vol.Schema(
        {
            vol.Required(
                CONF_BASE_URL, default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")
            ): TextSelector(TextSelectorConfig(autocomplete="username")),
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD, autocomplete="current-password"
                )
            ),
            vol.Optional(
                CONF_LOGIN_URL, default=defaults.get(CONF_LOGIN_URL, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Optional(
                CONF_USERNAME_FIELD, default=defaults.get(CONF_USERNAME_FIELD, "")
            ): TextSelector(),
            vol.Optional(
                CONF_PASSWORD_FIELD, default=defaults.get(CONF_PASSWORD_FIELD, "")
            ): TextSelector(),
        }
    )


def _meter_type_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Return the schema of the step that asks what is being metered."""
    return vol.Schema(
        {
            vol.Required(
                CONF_METER_TYPE,
                default=defaults.get(CONF_METER_TYPE, METER_TYPE_ELECTRICITY),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(METER_TYPES),
                    translation_key="meter_type",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _price_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=0, step="any", mode=NumberSelectorMode.BOX)
    )


def _meter_details_schema(
    meter_type: str, defaults: Mapping[str, Any], *, editing: bool = False
) -> vol.Schema:
    """Return the schema of the meter step for an already chosen type.

    When editing, the unit is left out: the statistic already holds values in
    it, and changing it would mix two units in one history. Prices of an
    existing meter are managed under "Prices".
    """
    schema: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): TextSelector(),
        vol.Required(
            CONF_METER_NUMBER, default=defaults.get(CONF_METER_NUMBER, "")
        ): TextSelector(),
    }
    if not editing:
        schema[
            vol.Required(
                CONF_UNIT,
                # Preset with the unit this kind of meter is billed in, and
                # only offer the units that fit it.
                default=defaults.get(CONF_UNIT, DEFAULT_UNIT_BY_METER_TYPE[meter_type]),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=list(UNITS_BY_METER_TYPE[meter_type]),
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
    schema[
        vol.Required(
            CONF_OBIS_CODE, default=defaults.get(CONF_OBIS_CODE, DEFAULT_OBIS_CODE)
        )
    ] = TextSelector()
    schema[
        vol.Required(
            CONF_EXPORT_TYPE,
            default=defaults.get(CONF_EXPORT_TYPE, DEFAULT_EXPORT_TYPE),
        )
    ] = TextSelector()
    if not editing:
        # `suggested_value` prefills the box without voluptuous putting the
        # old value back when it is submitted empty - which a plain `default`
        # would do, making a price impossible to remove.
        suggested = defaults.get(CONF_PRICE)
        description = {"suggested_value": suggested} if suggested is not None else None
        schema[vol.Optional(CONF_PRICE, description=description)] = _price_selector()
    return vol.Schema(schema)


def _clean_meter_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Trim the free text fields of the meter form."""
    return {
        key: value.strip() if isinstance(value, str) else value
        for key, value in user_input.items()
    }


def _validate_meter_input(meter: Mapping[str, Any]) -> dict[str, str]:
    """Return per-field errors for a meter form submission."""
    errors: dict[str, str] = {}
    if not meter.get(CONF_NAME):
        errors[CONF_NAME] = "required"
    if not meter.get(CONF_METER_NUMBER):
        errors[CONF_METER_NUMBER] = "required"
    if not meter.get(CONF_OBIS_CODE):
        errors[CONF_OBIS_CODE] = "required"
    if meter.get(CONF_UNIT) not in UNITS_BY_METER_TYPE[meter[CONF_METER_TYPE]]:
        errors[CONF_UNIT] = "invalid_unit"
    return errors


async def async_probe_portal(
    hass: HomeAssistant,
    credentials: Mapping[str, Any],
    options: Mapping[str, Any],
    meter: Mapping[str, Any] | None = None,
) -> None:
    """Log in to the portal and, if given a meter, fetch a sample of its data.

    Raises the api errors, or InvalidMeter when the export stays empty.
    """
    # A throwaway session: not tied to an entry, so it is detached here.
    session = async_create_clientsession(hass, auto_cleanup=False)
    client = RegioEnergieClient(
        session,
        credentials.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        credentials[CONF_USERNAME],
        credentials[CONF_PASSWORD],
        login_url=options.get(CONF_LOGIN_URL),
        username_field=options.get(CONF_USERNAME_FIELD),
        password_field=options.get(CONF_PASSWORD_FIELD),
    )
    try:
        await client.async_login()
        if meter is None:
            return

        end = _today()
        readings = await client.async_fetch_readings(
            meter[CONF_METER_NUMBER],
            meter.get(CONF_OBIS_CODE, DEFAULT_OBIS_CODE),
            meter.get(CONF_EXPORT_TYPE, DEFAULT_EXPORT_TYPE),
            end - timedelta(days=PROBE_DAYS),
            end,
        )
        if not readings:
            raise InvalidMeter(f"No readings for meter {meter[CONF_METER_NUMBER]}")
    finally:
        session.detach()


async def async_probe_with_errors(
    hass: HomeAssistant,
    credentials: Mapping[str, Any],
    options: Mapping[str, Any],
    meter: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Run async_probe_portal and translate its errors into form errors."""
    try:
        await async_probe_portal(hass, credentials, options, meter)
    except RegioEnergieAuthError as err:
        _LOGGER.debug("Login failed: %s", err)
        return {"base": "invalid_auth"}
    except RegioEnergieConnectionError as err:
        _LOGGER.debug("Portal unreachable: %s", err)
        return {"base": "cannot_connect"}
    except InvalidMeter as err:
        _LOGGER.debug("Meter probe empty: %s", err)
        return {CONF_METER_NUMBER: "no_data"}
    except RegioEnergieError as err:
        _LOGGER.debug("Portal error: %s", err)
        return {"base": "unknown"}
    except Exception:
        _LOGGER.exception("Unexpected error while validating the portal account")
        return {"base": "unknown"}
    return {}


def _unique_id(credentials: Mapping[str, Any]) -> str:
    """Return the unique id of an account: portal host and username."""
    host = urlparse(credentials[CONF_BASE_URL]).netloc
    return f"{host}::{credentials[CONF_USERNAME].lower()}"


def _credentials_from(user_input: Mapping[str, Any]) -> dict[str, Any]:
    return {
        CONF_BASE_URL: user_input[CONF_BASE_URL].strip().rstrip("/"),
        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
        CONF_PASSWORD: user_input[CONF_PASSWORD],
    }


class RegioEnergieConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of a portal account."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialise the flow."""
        self._credentials: dict[str, Any] = {}
        self._login_options: dict[str, Any] = {}
        self._meter_type: str = METER_TYPE_ELECTRICITY

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the portal account and verify it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            credentials = _credentials_from(user_input)
            login_options = _login_options(user_input)

            await self.async_set_unique_id(_unique_id(credentials))
            self._abort_if_unique_id_configured()

            errors = await async_probe_with_errors(
                self.hass, credentials, login_options
            )
            if not errors:
                self._credentials = credentials
                self._login_options = login_options
                return await self.async_step_meter_type()

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(user_input or {}),
            errors=errors,
            description_placeholders={"default_url": DEFAULT_BASE_URL},
        )

    async def async_step_meter_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask what the first meter measures."""
        if user_input is not None:
            self._meter_type = user_input[CONF_METER_TYPE]
            return await self.async_step_meter_details()

        return self.async_show_form(
            step_id="meter_type",
            data_schema=_meter_type_schema({CONF_METER_TYPE: self._meter_type}),
        )

    async def async_step_meter_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the first meter and create the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            meter = _clean_meter_input(user_input) | {CONF_METER_TYPE: self._meter_type}
            errors = _validate_meter_input(meter)
            if not errors:
                errors = await async_probe_with_errors(
                    self.hass, self._credentials, self._login_options, meter
                )
            if not errors:
                return self.async_create_entry(
                    title=entry_title(self._credentials[CONF_USERNAME]),
                    data=self._credentials,
                    options={
                        **self._login_options,
                        CONF_DAYS_TO_FETCH: DEFAULT_DAYS_TO_FETCH,
                        CONF_SCAN_INTERVAL_MINUTES: DEFAULT_SCAN_INTERVAL_MINUTES,
                        CONF_HISTORY_DAYS: DEFAULT_HISTORY_DAYS,
                        CONF_METERS: [
                            assign_statistic_ids(new_meter(meter, _today()), set())
                        ],
                    },
                )

        return self.async_show_form(
            step_id="meter_details",
            data_schema=_meter_details_schema(self._meter_type, user_input or {}),
            errors=errors,
            description_placeholders=_meter_placeholders(self.hass, self._meter_type),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle credentials that stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            credentials = {
                **entry.data,
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            await self.async_set_unique_id(_unique_id(credentials))
            self._abort_if_unique_id_mismatch()
            errors = await async_probe_with_errors(
                self.hass, credentials, entry.options
            )
            if not errors:
                return self.async_update_reload_and_abort(entry, data=credentials)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")
                    ): TextSelector(TextSelectorConfig(autocomplete="username")),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"username": entry.data.get(CONF_USERNAME, "")},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the portal address, the account or the login details."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            credentials = _credentials_from(user_input)
            login_options = _login_options(user_input)
            await self.async_set_unique_id(_unique_id(credentials))
            self._abort_if_unique_id_mismatch()
            errors = await async_probe_with_errors(
                self.hass, credentials, login_options
            )
            if not errors:
                options = {
                    key: value
                    for key, value in entry.options.items()
                    if key not in _LOGIN_OPTION_KEYS
                }
                return self.async_update_reload_and_abort(
                    entry, data=credentials, options=options | login_options
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(
                user_input or {**entry.data, **entry.options}
            ),
            errors=errors,
            description_placeholders={"default_url": DEFAULT_BASE_URL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return RegioEnergieOptionsFlow()


class RegioEnergieOptionsFlow(OptionsFlow):
    """Manage meters, prices and polling settings of an existing entry."""

    def __init__(self) -> None:
        """Initialise the flow; the entry is read on the first step."""
        self._options: dict[str, Any] = {}
        self._meters: list[dict[str, Any]] = []
        self._selected: str | None = None
        self._draft: dict[str, Any] = {}
        self._schedule: list[dict[str, Any]] = []
        self._loaded = False

    def _load(self) -> None:
        """Copy the options of the entry once, so steps can change them."""
        if self._loaded:
            return
        self._options = dict(self.config_entry.options)
        self._meters = [dict(meter) for meter in self._options.get(CONF_METERS, [])]
        self._loaded = True

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what can be changed."""
        self._load()
        menu = ["add_meter", "settings"]
        if self._meters:
            menu = ["add_meter", "edit_meter", "prices", "remove_meter", "settings"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    # --- meters ----------------------------------------------------------------

    async def async_step_add_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add another meter to this account."""
        self._selected = None
        self._draft = {}
        return await self.async_step_meter_type()

    async def async_step_edit_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the meter to edit."""
        if user_input is not None:
            self._selected = user_input[CONF_STATISTIC_ID]
            index = self._selected_index()
            if index is None:
                return await self.async_step_init()
            self._draft = dict(self._meters[index])
            # Type and unit stay as they are, see _meter_details_schema.
            return await self.async_step_meter_details()

        return self._show_meter_picker("edit_meter")

    async def async_step_meter_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask what the meter measures, so its unit can be preset."""
        if user_input is not None:
            self._draft[CONF_METER_TYPE] = user_input[CONF_METER_TYPE]
            return await self.async_step_meter_details()

        return self.async_show_form(
            step_id="meter_type", data_schema=_meter_type_schema(self._draft)
        )

    async def async_step_meter_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fill in the meter, either a new one or the one picked for editing."""
        meter_type = self._draft.get(CONF_METER_TYPE, METER_TYPE_ELECTRICITY)
        index = self._selected_index()
        editing = index is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            meter = _clean_meter_input(user_input) | {CONF_METER_TYPE: meter_type}
            if index is not None:
                meter[CONF_UNIT] = self._meters[index][CONF_UNIT]
            errors = _validate_meter_input(meter)
            if not errors:
                errors = await async_probe_with_errors(
                    self.hass, self.config_entry.data, self._options, meter
                )
            if not errors:
                if index is None:
                    self._meters.append(
                        assign_statistic_ids(
                            new_meter(meter, _today()),
                            {existing[CONF_STATISTIC_ID] for existing in self._meters},
                        )
                    )
                else:
                    # The statistic ids and the prices stay as they are,
                    # otherwise the history collected so far would be orphaned.
                    current = self._meters[index]
                    self._meters[index] = meter | {
                        CONF_STATISTIC_ID: current[CONF_STATISTIC_ID],
                        CONF_COST_STATISTIC_ID: cost_statistic_id_of(current),
                        CONF_PRICES: current.get(CONF_PRICES, []),
                    }
                return self._save()

        return self.async_show_form(
            step_id="meter_details",
            data_schema=_meter_details_schema(
                meter_type,
                user_input if user_input is not None else self._draft,
                editing=editing,
            ),
            errors=errors,
            description_placeholders=_meter_placeholders(self.hass, meter_type),
        )

    async def async_step_remove_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove meters from this account, with or without their history."""
        if user_input is not None:
            removed = set(user_input[CONF_METERS])
            if user_input.get(CONF_DELETE_HISTORY, True):
                statistic_ids = statistic_ids_of(
                    [m for m in self._meters if m[CONF_STATISTIC_ID] in removed]
                )
                if statistic_ids:
                    get_instance(self.hass).async_clear_statistics(statistic_ids)
            self._meters = [
                meter
                for meter in self._meters
                if meter[CONF_STATISTIC_ID] not in removed
            ]
            return self._save()

        return self.async_show_form(
            step_id="remove_meter",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_METERS, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._meter_options(),
                            mode=SelectSelectorMode.LIST,
                            multiple=True,
                        )
                    ),
                    vol.Required(CONF_DELETE_HISTORY, default=True): BooleanSelector(),
                }
            ),
        )

    # --- prices ----------------------------------------------------------------

    async def async_step_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the meter whose prices are edited."""
        if user_input is None and len(self._meters) == 1:
            user_input = {CONF_STATISTIC_ID: self._meters[0][CONF_STATISTIC_ID]}
        if user_input is not None:
            self._selected = user_input[CONF_STATISTIC_ID]
            index = self._selected_index()
            if index is None:
                return await self.async_step_init()
            self._schedule = sorted_schedule(self._meters[index].get(CONF_PRICES))
            return await self.async_step_price_menu()

        return self._show_meter_picker("prices")

    async def async_step_price_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the schedule and what can be done with it."""
        menu = ["price_add", "price_save"]
        if self._schedule:
            menu = ["price_add", "price_remove", "price_save"]
        return self.async_show_menu(
            step_id="price_menu",
            menu_options=menu,
            description_placeholders=self._price_placeholders(),
        )

    async def async_step_price_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a price that applies from a month on."""
        if user_input is not None:
            month = month_key(date.fromisoformat(user_input[CONF_VALID_FROM]))
            self._schedule = set_price(
                self._schedule, month, float(user_input[CONF_PRICE])
            )
            return await self.async_step_price_menu()

        return self.async_show_form(
            step_id="price_add",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_VALID_FROM, default=_today().replace(day=1).isoformat()
                    ): DateSelector(),
                    vol.Required(CONF_PRICE): _price_selector(),
                }
            ),
            description_placeholders=self._price_placeholders(),
        )

    async def async_step_price_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove entries from the schedule."""
        if user_input is not None:
            removed = set(user_input[CONF_ENTRIES])
            self._schedule = [
                entry
                for entry in self._schedule
                if entry[CONF_VALID_FROM] not in removed
            ]
            return await self.async_step_price_menu()

        unit_label = self._price_placeholders()["unit_label"]
        return self.async_show_form(
            step_id="price_remove",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENTRIES, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {
                                    "value": entry[CONF_VALID_FROM],
                                    "label": f"{entry[CONF_VALID_FROM]}: "
                                    f"{entry[CONF_PRICE]:g} {unit_label}",
                                }
                                for entry in self._schedule
                            ],
                            mode=SelectSelectorMode.LIST,
                            multiple=True,
                        )
                    )
                }
            ),
        )

    async def async_step_price_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the schedule; the next poll re-prices the affected months."""
        index = self._selected_index()
        if index is not None:
            self._meters[index] = self._meters[index] | {
                CONF_PRICES: sorted_schedule(self._schedule)
            }
        return self._save()

    def _price_placeholders(self) -> dict[str, str]:
        index = self._selected_index()
        meter = self._meters[index] if index is not None else {}
        unit_label = f"{self.hass.config.currency or ''}/{meter.get(CONF_UNIT, '')}"
        return {
            "meter": meter.get(CONF_NAME, ""),
            "unit_label": unit_label,
            "schedule": format_schedule(self._schedule, unit_label),
        }

    # --- settings --------------------------------------------------------------

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change polling, history and login details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            login_options = _login_options(user_input)
            errors = await async_probe_with_errors(
                self.hass, self.config_entry.data, login_options
            )
            if not errors:
                self._options = {
                    key: value
                    for key, value in self._options.items()
                    if key not in _LOGIN_OPTION_KEYS
                } | {
                    **login_options,
                    CONF_DAYS_TO_FETCH: int(user_input[CONF_DAYS_TO_FETCH]),
                    CONF_SCAN_INTERVAL_MINUTES: int(
                        user_input[CONF_SCAN_INTERVAL_MINUTES]
                    ),
                    CONF_HISTORY_DAYS: int(user_input[CONF_HISTORY_DAYS]),
                }
                return self._save()

        options = self._options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL_MINUTES,
                        default=options.get(
                            CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
                        ),
                    ): _number_box(
                        MIN_SCAN_INTERVAL_MINUTES, MAX_SCAN_INTERVAL_MINUTES
                    ),
                    vol.Required(
                        CONF_DAYS_TO_FETCH,
                        default=options.get(CONF_DAYS_TO_FETCH, DEFAULT_DAYS_TO_FETCH),
                    ): _number_box(MIN_DAYS_TO_FETCH, MAX_DAYS_TO_FETCH),
                    vol.Required(
                        CONF_HISTORY_DAYS,
                        default=options.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
                    ): _number_box(MIN_HISTORY_DAYS, MAX_HISTORY_DAYS),
                    vol.Optional(
                        CONF_LOGIN_URL, default=options.get(CONF_LOGIN_URL, "")
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                    vol.Optional(
                        CONF_USERNAME_FIELD,
                        default=options.get(CONF_USERNAME_FIELD, ""),
                    ): TextSelector(),
                    vol.Optional(
                        CONF_PASSWORD_FIELD,
                        default=options.get(CONF_PASSWORD_FIELD, ""),
                    ): TextSelector(),
                }
            ),
            errors=errors,
        )

    # --- helpers ---------------------------------------------------------------

    def _show_meter_picker(self, step_id: str) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATISTIC_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=self._meter_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    def _meter_options(self) -> list[dict[str, str]]:
        """Return the configured meters as selector options."""
        return [
            {
                "value": meter[CONF_STATISTIC_ID],
                "label": f"{meter.get(CONF_NAME, '?')} ({meter[CONF_METER_NUMBER]})",
            }
            for meter in self._meters
        ]

    def _selected_index(self) -> int | None:
        """Return the list index of the meter picked before."""
        for index, meter in enumerate(self._meters):
            if meter[CONF_STATISTIC_ID] == self._selected:
                return index
        return None

    def _save(self) -> ConfigFlowResult:
        """Write the options back; the entry reloads through its listener."""
        return self.async_create_entry(
            title="", data={**self._options, CONF_METERS: self._meters}
        )


def _number_box(minimum: int, maximum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX
        )
    )
