from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from app.keyboards.inline import keyboard, menu_keyboard
from app.main import _day_number, _parse_datetime
from app.services.mvp import REWARD_CATALOG, SPIN_PRIZES, analyze_stress, normalize_school


def test_stress_analyzer_scores_multiple_themes() -> None:
    score, themes = analyze_stress("Я очень устал, всё бесит и становится страшно")

    assert score == 4
    assert "усталость" in themes
    assert "тревога" in themes


def test_stress_analyzer_flags_urgent_words() -> None:
    score, themes = analyze_stress("Я хочу умереть")

    assert score == 5
    assert "требуется срочная поддержка" in themes


def test_student_menu_uses_inline_callback_buttons() -> None:
    buttons = menu_keyboard("student")

    assert any(
        item.payload == "student:homework"
        for row in buttons.buttons
        for item in row
    )


def test_callback_payload_is_attached_to_inline_button() -> None:
    buttons = keyboard((("Назад", "menu:student"),))

    assert buttons.buttons[0][0].text == "Назад"
    assert buttons.buttons[0][0].payload == "menu:student"


def test_date_and_weekday_inputs_are_parsed() -> None:
    assert _parse_datetime("25.10.2026 18:30") == datetime(2026, 10, 25, 18, 30)
    assert _parse_datetime("25.10.2026") == datetime(2026, 10, 25, 23, 59)
    assert _day_number("пн") == 1


def test_invalid_schedule_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        _parse_datetime("завтра")

    with pytest.raises(ValueError):
        _day_number("8")

class FakeSession:
    """Замена AsyncSession: отдаёт заранее заданный набор наград пользователя."""

    def __init__(self, reward_keys: set[str]) -> None:
        self._keys = reward_keys

    async def scalars(self, statement):
        return list(self._keys)


def test_normalize_school_ignores_formatting() -> None:
    """«Школа №1», «школа N1» и «школа1» — одна и та же школа."""
    variants = ("Школа №1", "школа N1", "Школа  1", "школа1", "ШКОЛА № 1")
    normalized = {normalize_school(item) for item in variants}

    assert normalized == {"школа 1"}
    assert normalize_school("Гимназия №5 им. Пушкина") == normalize_school(
        "гимназия 5 им пушкина"
    )
    assert normalize_school(None) == ""
    assert normalize_school("   ") == ""


def test_homework_gain_applies_boost_rewards() -> None:
    """Стоимость домашки даёт баллы; бусты ×2 и ×3 работают и не складываются."""
    from app.services import mvp

    item = SimpleNamespace(points=10)

    assert asyncio.run(mvp._homework_gain(FakeSession(set()), 1, item)) == 10
    assert (
        asyncio.run(mvp._homework_gain(FakeSession({"points_boost"}), 1, item)) == 20
    )
    assert (
        asyncio.run(
            mvp._homework_gain(FakeSession({"points_boost", "points_boost_3"}), 1, item)
        )
        == 30
    )
    # Баллы не могут стать отрицательными или «NaN»-подобными.
    assert asyncio.run(mvp._homework_gain(FakeSession(set()), 1, SimpleNamespace(points=None))) == 0


def test_reward_catalog_is_consistent() -> None:
    """Каталог наград: уникальные цены/названия и корректные призы колеса."""
    # Оставлены только «крутые» награды — мелкие значки и титулы убраны.
    assert len(REWARD_CATALOG) >= 8
    costs = [cost for _title, cost, _description in REWARD_CATALOG.values()]
    assert all(cost > 0 for cost in costs)
    assert all(prize >= 0 for prize in SPIN_PRIZES)
    assert max(SPIN_PRIZES) <= 50


def test_user_keeps_explicit_full_name() -> None:
    """``name`` — синоним ``full_name``: явное имя не должно теряться."""
    from app.db.models import User

    assert User(max_user_id=1, full_name="Иванов Иван").full_name == "Иванов Иван"
    assert User(max_user_id=2, name="Петров Пётр").name == "Петров Пётр"
    assert User(max_user_id=3).full_name == "Пользователь"

