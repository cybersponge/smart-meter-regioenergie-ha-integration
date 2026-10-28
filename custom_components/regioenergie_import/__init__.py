"""The Regio Energie Smart Meter Import integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .api import RegioEnergieClient
from .const import (
    CONF_BASE_URL,
    CONF_HISTORY_DAYS,
    CONF_LOGIN_URL,
    CONF_METERS,
    CONF_PASSWORD_FIELD,
    CONF_PRICE,
    CONF_PRICE_WINTER,
    CONF_PRICES,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_STATISTIC_ID,
    CONF_USERNAME_FIELD,
    CONF_VALID_FROM,
    DEFAULT_BASE_URL,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    PORTAL_TIMEZONE,
    SUMMER_MONTHS,
    entry_title,
)
from .coordinator import (
    RegioEnergieCoordinator,
    cost_statistic_id_of,
    month_key,
    store_key,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_IMPORT_HISTORY = "import_history"
ATTR_START = "start"

SERVICE_IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_STATISTIC_ID): cv.string,
        vol.Required(ATTR_START): cv.date,
    }
)

# The poll interval of version 1 defaulted to 6 hours. The portal publishes
# every 15 minutes, so that default moves to the new one instead of to 360.
_V1_DEFAULT_SCAN_INTERVAL_HOURS = 6

type RegioEnergieConfigEntry = ConfigEntry[RegioEnergieCoordinator]


def build_client(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> RegioEnergieClient:
    """Create a portal client with its own cookie jar for this entry.

    Created during the entry setup, the session is detached by Home Assistant
    itself when the entry unloads; closing it here is not allowed.
    """
    session = async_create_clientsession(hass)
    return RegioEnergieClient(
        session,
        entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        login_url=entry.options.get(CONF_LOGIN_URL),
        username_field=entry.options.get(CONF_USERNAME_FIELD),
        password_field=entry.options.get(CONF_PASSWORD_FIELD),
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the history import action."""

    async def async_import_history(call: ServiceCall) -> None:
        statistic_id: str = call.data[CONF_STATISTIC_ID]
        start: date = call.data[ATTR_START]
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            coordinator: RegioEnergieCoordinator = entry.runtime_data
            for meter in coordinator.meters:
                if meter[CONF_STATISTIC_ID] == statistic_id:
                    if not coordinator.async_start_backfill(meter, start):
                        raise ServiceValidationError(
                            translation_domain=DOMAIN,
                            translation_key="backfill_running",
                            translation_placeholders={"statistic_id": statistic_id},
                        )
                    return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_meter",
            translation_placeholders={"statistic_id": statistic_id},
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        async_import_history,
        schema=SERVICE_IMPORT_HISTORY_SCHEMA,
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> bool:
    """Set up a portal account from a config entry."""
    # Older entries were named only after the username, which makes the
    # authentication repair impossible to attribute to this integration.
    if entry.title == entry.data[CONF_USERNAME]:
        hass.config_entries.async_update_entry(
            entry, title=entry_title(entry.data[CONF_USERNAME])
        )

    coordinator = RegioEnergieCoordinator(hass, entry, build_client(hass, entry))
    entry.runtime_data = coordinator

    # The statistics are written by the coordinator itself, and only priced
    # meters get a sensor - an account without one has no entity listening to
    # the coordinator, and a coordinator without listeners never schedules its
    # next refresh. The no-op listener keeps the polling timer running.
    entry.async_on_unload(coordinator.async_add_listener(_noop))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> None:
    """Reload the entry after its options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> None:
    """Delete the statistics of a removed account along with it."""
    statistic_ids = statistic_ids_of(entry.options.get(CONF_METERS, []))
    if statistic_ids:
        get_instance(hass).async_clear_statistics(statistic_ids)
    await Store(hass, 1, store_key(entry.entry_id)).async_remove()


def statistic_ids_of(meters: list[Mapping[str, Any]]) -> list[str]:
    """Return the consumption and cost statistic ids of these meters."""
    return [
        statistic_id
        for meter in meters
        for statistic_id in (
            meter[CONF_STATISTIC_ID],
            cost_statistic_id_of(dict(meter)),
        )
    ]


async def async_migrate_entry(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> bool:
    """Migrate an entry of an older version."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        today = datetime.now(ZoneInfo(PORTAL_TIMEZONE)).date()
        hass.config_entries.async_update_entry(
            entry, options=migrate_options_v1(entry.options, today), version=2
        )
        _LOGGER.info("Migrated %s to version 2", entry.title)
    return True


def migrate_options_v1(options: Mapping[str, Any], today: date) -> dict[str, Any]:
    """Turn the options of version 1 into those of version 2.

    The poll interval moves from hours to minutes, and the base and winter
    price of a meter become a monthly price schedule. A winter price turns into
    entries every April and October, from the start of the history up to now.
    """
    migrated = {
        key: value
        for key, value in options.items()
        if key not in (CONF_SCAN_INTERVAL_HOURS, CONF_METERS)
    }
    hours = int(options.get(CONF_SCAN_INTERVAL_HOURS, _V1_DEFAULT_SCAN_INTERVAL_HOURS))
    migrated[CONF_SCAN_INTERVAL_MINUTES] = (
        DEFAULT_SCAN_INTERVAL_MINUTES
        if hours == _V1_DEFAULT_SCAN_INTERVAL_HOURS
        else hours * 60
    )
    migrated.setdefault(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)

    first = today.replace(year=today.year - 3, month=1, day=1)
    migrated[CONF_METERS] = []
    for meter in options.get(CONF_METERS, []):
        new_meter = {
            key: value
            for key, value in meter.items()
            if key not in (CONF_PRICE, CONF_PRICE_WINTER)
        }
        base = meter.get(CONF_PRICE)
        winter = meter.get(CONF_PRICE_WINTER)
        if base is not None and winter is None:
            new_meter[CONF_PRICES] = [
                {CONF_VALID_FROM: month_key(today), CONF_PRICE: float(base)}
            ]
        elif base is not None:
            new_meter[CONF_PRICES] = _seasonal_schedule(
                float(base), float(winter), first, today
            )
        migrated[CONF_METERS].append(new_meter)
    return migrated


def _seasonal_schedule(
    summer: float, winter: float, first: date, today: date
) -> list[dict[str, Any]]:
    """Spell a summer/winter tariff out as monthly entries."""
    schedule: list[dict[str, Any]] = []
    year, month = first.year, first.month
    current: float | None = None
    while (year, month) <= (today.year, today.month):
        price = summer if month in SUMMER_MONTHS else winter
        if price != current:
            schedule.append(
                {CONF_VALID_FROM: f"{year:04d}-{month:02d}", CONF_PRICE: price}
            )
            current = price
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return schedule


@callback
def _noop() -> None:
    """Do nothing; see async_setup_entry."""
