"""Tests for config entry setup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.regioenergie_import import async_setup_entry
from homeassistant.const import CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_title", "expected_title"),
    [
        ("person@example.com", "Regio Energie Smart Meter Import: person@example.com"),
        ("My meter", None),
    ],
)
async def test_setup_identifies_legacy_entries_even_if_authentication_fails(
    current_title: str, expected_title: str | None
) -> None:
    """A login repair is attributable without changing custom entry titles."""
    hass = MagicMock()
    entry = MagicMock()
    entry.title = current_title
    entry.data = {CONF_USERNAME: "person@example.com"}
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=ConfigEntryAuthFailed("Login failed")
    )

    with (
        patch("custom_components.regioenergie_import.build_client"),
        patch(
            "custom_components.regioenergie_import.RegioEnergieCoordinator",
            return_value=coordinator,
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, entry)

    if expected_title is None:
        hass.config_entries.async_update_entry.assert_not_called()
    else:
        hass.config_entries.async_update_entry.assert_called_once_with(
            entry, title=expected_title
        )
