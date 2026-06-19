"""
Shared pytest fixtures and lightweight Playwright fakes.

The real scraper talks to a Playwright ``Page``.  Rather than spinning up
a browser, these fakes implement just the async surface the scraper uses
(``locator``, ``get_by_role``, ``evaluate``, ``mouse``, ``keyboard`` …)
with configurable return values.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest


class FakeLocator:
    """
    Minimal async stand-in for a Playwright ``Locator``.

    Parameters
    ----------
    items : list, optional
        Backing items; ``count`` reports ``len(items)`` and ``nth`` indexes
        into them.  Each item may carry ``text`` / ``visible`` / ``box``.
    on_click : callable, optional
        Invoked (no args) whenever ``click`` is awaited.
    """

    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        self._items = items if items is not None else [{}]
        self._on_click = on_click
        self._idx = 0

    # navigation --------------------------------------------------------------
    def nth(self, i: int) -> FakeLocator:
        loc = FakeLocator([self._items[i]], on_click=self._on_click)
        return loc

    @property
    def first(self) -> FakeLocator:
        return self.nth(0) if self._items else FakeLocator([{}], self._on_click)

    def filter(self, **_: Any) -> FakeLocator:
        return self

    def locator(self, *_: Any, **__: Any) -> FakeLocator:
        return self

    # async queries -----------------------------------------------------------
    async def count(self) -> int:
        return len(self._items)

    async def all_text_contents(self) -> list[str]:
        return [it.get("text", "") for it in self._items]

    async def text_content(self) -> str:
        return self._items[0].get("text", "") if self._items else ""

    async def is_visible(self) -> bool:
        return self._items[0].get("visible", True) if self._items else False

    async def bounding_box(self) -> dict[str, float] | None:
        return self._items[0].get("box") if self._items else None

    async def evaluate(self, _script: str) -> str:
        return self._items[0].get("text", "") if self._items else ""

    async def wait_for(self, **_: Any) -> None:
        return None

    async def click(self, **_: Any) -> None:
        if self._on_click is not None:
            self._on_click()


class FakeMouse:
    """Records mouse clicks."""

    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []

    async def click(self, x: float, y: float, **_: Any) -> None:
        self.clicks.append((x, y))


class FakeKeyboard:
    """Records key presses."""

    def __init__(self) -> None:
        self.presses: list[str] = []

    async def press(self, key: str) -> None:
        self.presses.append(key)


class FakePage:
    """
    Configurable async fake for a Playwright ``Page``.

    Parameters
    ----------
    locators : dict, optional
        Maps a selector string to a :class:`FakeLocator` (or factory).
    radios : list of str, optional
        Radio-button labels returned by ``input[type='radio']`` locators.
    menu_items : list of dict, optional
        Items returned for the vessel-class menu selector.
    evaluate_result : Any, optional
        Value returned by ``page.evaluate`` (used by ``extract_sections``).
    """

    def __init__(
        self,
        locators: dict[str, FakeLocator] | None = None,
        radios: list[str] | None = None,
        menu_items: list[dict[str, Any]] | None = None,
        evaluate_result: Any = None,
    ) -> None:
        self._locators = locators or {}
        self._radios = radios or []
        self._menu_items = menu_items or []
        self._evaluate_result = evaluate_result if evaluate_result is not None else {}
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.goto_url: str | None = None
        self.selected_vessel: str | None = None

    # navigation --------------------------------------------------------------
    async def goto(self, url: str, **_: Any) -> None:
        self.goto_url = url

    async def wait_for_load_state(self, *_: Any, **__: Any) -> None:
        return None

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    def get_by_role(self, _role: str, *, name: str = "") -> FakeLocator:
        return FakeLocator([{"text": name}])

    def get_by_text(self, text: str, **_: Any) -> FakeLocator:
        return FakeLocator([{"text": text}])

    def locator(self, selector: str, **kwargs: Any) -> FakeLocator:
        if "radio" in selector:
            return FakeLocator([{"text": r} for r in self._radios])
        if "menuItem" in selector:
            return FakeLocator(self._menu_items)
        if selector in self._locators:
            return self._locators[selector]
        # default: a single clickable element
        return FakeLocator([{"text": kwargs.get("has_text", "")}])

    async def evaluate(self, _script: str, *_: Any) -> Any:
        return self._evaluate_result

    async def content(self) -> str:
        return "<html></html>"

    async def screenshot(self, **_: Any) -> None:
        return None

    def on(self, *_: Any, **__: Any) -> None:
        return None


@pytest.fixture()
def fake_page_factory() -> Callable[..., FakePage]:
    """Return the :class:`FakePage` constructor for ad-hoc configuration."""
    return FakePage
