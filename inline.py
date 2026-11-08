from __future__ import annotations

from maxapi.types import (
    ButtonsPayload,
    CallbackButton,
    LinkButton,
    OpenAppButton,
)
from maxapi.types.attachments.buttons.callback_button import CallbackButton as MaxCallbackButton

ButtonRows = list[list[MaxCallbackButton]]

MINI_APP_BUTTON_TEXT = "🕹 Открыть мини-приложение"


def button(text: str, payload: str) -> MaxCallbackButton:
    return CallbackButton(text=text, payload=payload)


def link_button(text: str, url: str) -> LinkButton:
    """Кнопка-ссылка: запасной способ открыть мини-приложение."""
    return LinkButton(text=text, url=url)


def open_app_button(
    text: str, app_name: str, payload: str | None = None
) -> OpenAppButton:
    """Нативная кнопка мини-приложения MAX.

    MAX запускает мини-приложение внутри мессенджера (не во внешнем браузере).
    ``payload`` — необязательный параметр запуска: MAX ограничивает его длину
    (~64 символа), поэтому подписанный токен сюда не кладём — он уже в URL
    кнопки-ссылки, а mini.js читает токен из ``?token=``.
    """
    return OpenAppButton(text=text, web_app=app_name.lstrip("@"), payload=payload)


def mini_app_button(
    text: str,
    url: str | None = None,
    app_name: str | None = None,
    payload: str | None = None,
) -> OpenAppButton | LinkButton:
    """Нативная кнопка мини-приложения, а без username бота — обычная ссылка."""
    if app_name:
        return open_app_button(text, app_name, payload)
    return link_button(text, url or "")


def keyboard(*rows: tuple[tuple[str, str], ...]) -> ButtonsPayload:
    return ButtonsPayload(
        buttons=[
            [button(text, payload) for text, payload in row]
            for row in rows
        ]
    )


def menu_keyboard(
    role: str,
    web_url: str | None = None,
    app_name: str | None = None,
    payload: str | None = None,
) -> ButtonsPayload:
    """Главное меню кабинета: только функции выбранной роли + мини-приложение."""
    menus = {
        "student": (
            (("🏫 Войти в класс", "student:join_class"),),
            (("📚 Расписание", "student:schedule"), ("📝 Домашка", "student:homework")),
            (("⏰ Дедлайны", "student:deadlines"), ("🧘 Антистресс", "student:stress")),
            (("🎮 Профиль и очки", "student:profile"), ("⚙️ Настройки", "student:settings")),
            (("📢 Объявления класса", "student:announcements"),),
        ),
        "teacher": (
            (("➕ Создать класс", "teacher:create_class"), ("🏫 Мои классы", "teacher:classes")),
            (("📝 Домашка", "teacher:homework"), ("🗓 Расписание", "teacher:schedule")),
            (("⏰ Контрольные", "teacher:deadline"), ("📊 Статистика", "teacher:stats")),
            (("📢 Объявление", "teacher:announce"), ("✅ Сдача домашки", "teacher:hw_mark")),
            (("⚙️ Настройки", "teacher:settings"),),
        ),
        "parent": (
            (("👨‍👩‍👧 Синхронизироваться с ребёнком", "parent:sync"),),
            (("📊 Учебная сводка", "parent:summary"), ("🔔 Уведомления", "parent:notifications")),
            (("💡 Советы", "parent:tips"), ("⚙️ Настройки", "parent:settings")),
        ),
        "psychologist": (
            (("📥 Сигналы", "psychologist:signals"),),
            (("📊 Статистика", "psychologist:stats"), ("⚙️ Настройки", "psychologist:settings")),
        ),
    }
    payload_buttons = keyboard(*menus.get(role, menus["student"]))
    buttons: list[list] = list(payload_buttons.buttons or [])
    # Мини-приложение открывается внутри MAX (кнопка open_app) — токен в payload.
    if app_name or web_url:
        buttons.insert(
            0,
            [
                mini_app_button(
                    MINI_APP_BUTTON_TEXT,
                    url=web_url,
                    app_name=app_name,
                    payload=payload,
                )
            ],
        )
    else:
        # Без персональных данных — через колбэк бот пришлёт кнопку следующим шагом.
        buttons.append([button("🕹 Мини-приложение", "ui:miniapp")])
    return ButtonsPayload(buttons=buttons)


def mini_app_keyboard(
    role: str,
    web_url: str | None = None,
    app_name: str | None = None,
    payload: str | None = None,
) -> ButtonsPayload:
    """Карточка мини-приложения: открыть в MAX или вернуться в меню.

    Имена параметров совпадают с ``menu_keyboard``, потому что main.py передаёт
    сюда один и тот же набор ``_mini_app_args(user)``.
    """
    return ButtonsPayload(
        buttons=[
            [
                mini_app_button(
                    MINI_APP_BUTTON_TEXT,
                    url=web_url,
                    app_name=app_name,
                    payload=payload,
                )
            ],
            [button("⬅️ В меню", f"menu:{role}")],
        ]
    )


def registration_roles_keyboard() -> ButtonsPayload:
    """Первый шаг стартового окна: ученик / сотрудник школы / родитель."""
    return keyboard(
        (("🎓 Я ученик", "onboard:role:student"),),
        (("👩‍🏫 Я сотрудник школы", "onboard:role:staff"),),
        (("👨‍👩‍👧 Я родитель", "onboard:role:parent"),),
    )


def staff_roles_keyboard() -> ButtonsPayload:
    """Уточнение роли после выбора «Я сотрудник школы»."""
    return keyboard(
        (("👩‍🏫 Учитель", "onboard:role:teacher"),),
        (("🧠 Психолог", "onboard:role:psychologist"),),
        (("⬅️ Назад", "onboard:role_back"),),
    )


def grade_keyboard() -> ButtonsPayload:
    buttons = tuple((f"{grade} класс", f"onboard:grade:{grade}") for grade in range(1, 12))
    chunk_size = 4
    return keyboard(
        *(
            tuple(buttons[i : i + chunk_size])
            for i in range(0, len(buttons), chunk_size)
        )
    )


def back_menu(role: str) -> ButtonsPayload:
    return keyboard((("⬅️ В меню", f"menu:{role}"),))


def with_menu_row(buttons: ButtonsPayload | None, role: str) -> ButtonsPayload:
    """Гарантировать кнопку «⬅️ В меню» на любом экране кабинета.

    Пользователь не должен оказываться в тупике без выхода в меню: если у
    исходящей клавиатуры ещё нет кнопки возврата — дописываем её отдельной
    строкой. Повторно не добавляем, если кнопка уже есть.
    """
    payload = f"menu:{role}"
    rows = [list(row) for row in (buttons.buttons if buttons else [])]
    if any(
        getattr(item, "payload", None) == payload for row in rows for item in row
    ):
        return buttons
    rows.append([button("⬅️ В меню", payload)])
    return ButtonsPayload(buttons=rows)
