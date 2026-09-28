from __future__ import annotations

import pytest

from amadeus_bot.services.campus_auth import CampusAuthenticator


@pytest.mark.parametrize(
    "method_name, variable",
    [
        ("_headless", "AMADEUS_CAMPUS_BROWSER_HEADLESS"),
        ("_portal_headless", "AMADEUS_PORTAL_BROWSER_HEADLESS"),
        ("_activity_headless", "AMADEUS_ACTIVITY_BROWSER_HEADLESS"),
    ],
)
def test_browser_login_is_headless_by_default(monkeypatch, method_name: str, variable: str) -> None:
    monkeypatch.delenv(variable, raising=False)

    assert getattr(CampusAuthenticator, method_name)() is True


@pytest.mark.parametrize(
    "method_name, variable",
    [
        ("_headless", "AMADEUS_CAMPUS_BROWSER_HEADLESS"),
        ("_portal_headless", "AMADEUS_PORTAL_BROWSER_HEADLESS"),
        ("_activity_headless", "AMADEUS_ACTIVITY_BROWSER_HEADLESS"),
    ],
)
def test_browser_login_can_be_made_visible_for_debugging(
    monkeypatch,
    method_name: str,
    variable: str,
) -> None:
    monkeypatch.setenv(variable, "false")

    assert getattr(CampusAuthenticator, method_name)() is False
