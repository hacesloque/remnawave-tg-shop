import logging
import re
from datetime import datetime
from typing import Optional

from aiogram import Router, F, types, Bot
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.utils.markdown import hcode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from db.dal import promo_code_dal
from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.services.promo_code_service import PromoCodeService
from bot.services.subscription_service import SubscriptionService
from bot.states.user_states import UserPromoStates
from bot.keyboards.inline.user_keyboards import get_back_to_main_menu_markup
from .start import send_main_menu

router = Router(name="user_promo_router")

SUSPICIOUS_SQL_KEYWORDS_REGEX = re.compile(
    r"\b(DROP\s*TABLE|DELETE\s*FROM|ALTER\s*TABLE|TRUNCATE\s*TABLE|UNION\s*SELECT|"
    r";\s*SELECT|;\s*INSERT|;\s*UPDATE|;\s*DELETE|xp_cmdshell|sysdatabases|sysobjects|INFORMATION_SCHEMA)\b",
    re.IGNORECASE,
)
SUSPICIOUS_CHARS_REGEX = re.compile(r"(--|#\s|;|\*\/|\/\*)")
MAX_PROMO_CODE_INPUT_LENGTH = 100

# promo codes can be typed directly in chat
PROMO_CODE_CHAT_REGEX = re.compile(r"^[A-Za-z0-9_\-]{3,30}$")


async def prompt_promo_code_input(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not i18n:
        await callback.answer("Language service error.", show_alert=True)
        return

    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    if not callback.message:
        logging.error("CallbackQuery has no message in prompt_promo_code_input")
        await callback.answer(_("error_occurred_processing_request"), show_alert=True)
        return

    try:
        await callback.message.edit_text(
            text=_(key="promo_code_prompt"),
            reply_markup=get_back_to_main_menu_markup(current_lang, i18n),
        )
    except Exception as e_edit:
        logging.warning(f"Failed to edit message for promo prompt: {e_edit}. Sending new one.")
        await callback.message.answer(
            text=_(key="promo_code_prompt"),
            reply_markup=get_back_to_main_menu_markup(current_lang, i18n),
        )

    await callback.answer()
    await state.set_state(UserPromoStates.waiting_for_promo_code)


@router.message(UserPromoStates.waiting_for_promo_code, F.text)
async def process_promo_code_input(
    message: types.Message,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
    promo_code_service: PromoCodeService,
    subscription_service: SubscriptionService,
    bot: Bot,
    session: AsyncSession,
):
    logging.info(
        f"Processing promo code input from user {message.from_user.id} in state {await state.get_state()}: '{message.text}'"
    )

    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not i18n or not promo_code_service:
        logging.error("Dependencies (i18n or PromoCodeService) missing in process_promo_code_input")
        await message.reply("Service error. Please try again later.")
        await state.clear()
        return

    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    code_input = message.text.strip() if message.text else ""
    user = message.from_user

    is_suspicious = False
    if not code_input:
        is_suspicious = True
        logging.warning(f"Empty promo code input by user {user.id}.")
    elif (
        len(code_input) > MAX_PROMO_CODE_INPUT_LENGTH
        or SUSPICIOUS_SQL_KEYWORDS_REGEX.search(code_input)
        or SUSPICIOUS_CHARS_REGEX.search(code_input)
    ):
        is_suspicious = True
        logging.warning(
            f"Suspicious input for promo code by user {user.id} (len: {len(code_input)}): '{code_input}'"
        )

    if is_suspicious:
        if settings.LOG_SUSPICIOUS_ACTIVITY:
            try:
                from bot.services.notification_service import NotificationService

                notification_service = NotificationService(bot, settings, i18n)
                await notification_service.notify_suspicious_promo_attempt(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    suspicious_input=code_input,
                )
            except Exception as e:
                logging.error(f"Failed to send suspicious promo notification: {e}")

        response_text = _("promo_code_not_found", code=hcode(code_input.upper()))
        await message.answer(response_text, parse_mode="HTML")
        await state.clear()
        return

    success, result = await promo_code_service.apply_promo_code(session, user.id, code_input, current_lang)

    if success:
        await session.commit()
        new_end_date = result if isinstance(result, datetime) else None

        # Кастомный короткий текст (2 строки), без кнопок
        response_text = _(
            "promo_code_applied_success_short",
            end_date=(new_end_date.strftime("%d.%m.%Y %H:%M:%S") if new_end_date else "N/A"),
        )
        await message.answer(response_text, parse_mode="HTML")
    else:
        await session.rollback()
        # 'result' уже локализованная строка (например: "Вы уже активировали промокод ...")
        await message.answer(result, parse_mode="HTML")

    await state.clear()
    logging.info(f"Promo code input '{code_input}' finished for user {user.id}. State cleared.")
# Пользователь вводит промокод прямо в чат (вне меню/состояния)
@router.message(
    StateFilter(None),
    F.text,
    ~F.text.startswith("/"),
)
async def maybe_process_promo_or_support_from_chat(
    message: types.Message,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
    promo_code_service: PromoCodeService,
    subscription_service: SubscriptionService,
    bot: Bot,
    session: AsyncSession,
):
    """
    Chat input routing:
    - ignore /commands
    - if looks like promo (3-30 chars [A-Za-z0-9_-]):
        - if promo DOES NOT exist in DB -> treat as support message
        - if exists -> apply promo and respond with promo texts
    - otherwise -> support message
    """
    if not message.text:
        return

    text = message.text.strip()
    if not text:
        return

    # commands already excluded by decorator, but keep safe
    if text.startswith("/"):
        return

    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    # promo-like?
    promo_like = bool(re.fullmatch(r"[A-Za-z0-9_\-]{3,30}", text))

    if promo_like:
        promo = await promo_code_dal.get_promo_code_by_code(session, text)
        if promo:
            # promo exists -> apply
            success, result = await promo_code_service.apply_promo_code(
                session, message.from_user.id, text, current_lang
            )
            if success:
                await session.commit()
                new_end_date = result if isinstance(result, datetime) else None
                response_text = _(
                    "promo_code_applied_success_short",
                    end_date=(new_end_date.strftime("%d.%m.%Y %H:%M:%S") if new_end_date else "N/A"),
                )
                await message.answer(response_text, parse_mode="HTML")
                return

            await session.rollback()
            # result already localized promo text (already used / expired / limit / etc.)
            await message.answer(result, parse_mode="HTML")
            return

        # promo-like but NOT existing -> treat as support message (do not show "not found")
        # fallthrough to support

    # SUPPORT FLOW (any other text)
    support_admin_id = 7816794857
    user = message.from_user
    user_display = f"@{user.username}" if user.username else (user.full_name if user else "Unknown")

    # to admin
    
    # SUPPORT: admin reply button
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Ответить", callback_data=f"support_reply:{message.from_user.id}")
    kb.button(text="👤 Карточка", callback_data=f"support_user_card:{message.from_user.id}")
    kb.adjust(2)
    await bot.send_message(
    support_admin_id,
    f"💬 <b>Сообщение в поддержку</b>\n\n"
    f"👤 Пользователь: {user_display}\n"
    f"🆔 ID: <code>{user.id}</code>\n\n"
    f"📝 Сообщение:\n<pre>{text}</pre>",

        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )

    # to user
    ack_msg = await message.answer("✅ Сообщение отправлено в поддержку. Мы скоро ответим.", parse_mode="HTML")
    # store ack message_id so we can delete it after admin reply
    try:
        key = StorageKey(bot_id=bot.id, chat_id=user.id, user_id=user.id)
        await state.storage.set_data(key, {"support_last_ack_message_id": ack_msg.message_id})
    except Exception as e:
        logging.warning(f"Failed to store support ack message_id: {e}")

@router.callback_query(F.data == "main_action:back_to_main", UserPromoStates.waiting_for_promo_code)
async def cancel_promo_input_via_button(
    callback: types.CallbackQuery,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
    subscription_service: SubscriptionService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        logging.error("i18n missing in cancel_promo_input_via_button")
        await callback.answer("Language error", show_alert=True)
        return

    await state.clear()

    if callback.message:
        await send_main_menu(
            callback,
            settings,
            i18n_data,
            subscription_service,
            session,
            is_edit=True,
        )
    else:
        _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
        await callback.answer(_("promo_input_cancelled_short"), show_alert=False)
