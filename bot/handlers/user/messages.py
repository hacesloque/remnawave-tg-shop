import logging
from aiogram import Router, F, types
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import (
    SUPPORT_FORWARD_ENABLED,
    SUPPORT_INBOX_CHAT_ID,
    PROMOCODE_TEXT_INPUT_ENABLED,
    Settings,
)
from bot.services.promo_code_service import PromoCodeService

router = Router(name="user_messages")


def _is_command(message: Message) -> bool:
    if not message.text:
        return False
    text = message.text.lstrip()
    if text.startswith("/"):
        return True
    if message.entities:
        for e in message.entities:
            if getattr(e, "type", None) == "bot_command" and getattr(e, "offset", 0) == 0:
                return True
    return False


def _maybe_promocode(text: str) -> bool:
    """Простая эвристика: алфавитно-цифровая строка 4..32 символа."""
    if not text:
        return False
    s = text.strip()
    return 4 <= len(s) <= 32 and s.replace("-", "").isalnum()


@router.message(F.chat.type == "private", F.text & ~F.via_bot)
async def handle_user_message(
    message: types.Message,
    settings: Settings,
    session: AsyncSession,
    promo_code_service: PromoCodeService,
    i18n_data: dict,
):
    # 1) не трогаем команды (/start, /admin, etc.)
    if _is_command(message):
        return

    text = (message.text or "").strip()

    # 2) попытка активировать промокод по свободному вводу
    if PROMOCODE_TEXT_INPUT_ENABLED and _maybe_promocode(text):
        try:
            current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
            success, result = await promo_code_service.apply_promo_code(
                session, message.from_user.id, text, current_lang
            )
            if success:
                await message.answer("🎁 Промокод активирован! Спасибо 🙌")
                return
        except Exception as e:
            logging.warning(f"[promocode-input] Failed to apply '{text}': {e}")

    # 3) обычное сообщение — пересылаем в поддержку
    if not SUPPORT_FORWARD_ENABLED or not SUPPORT_INBOX_CHAT_ID:
        return

    try:
        await message.forward(SUPPORT_INBOX_CHAT_ID)
        await message.answer("✅ Сообщение передано в поддержку. Мы свяжемся с Вами в ближайшее время.")
    except Exception as e:
        logging.error(f"[support-forward] Error: {e}")
        await message.answer("❗️Не удалось отправить сообщение в поддержку. Попробуйте позже.")
