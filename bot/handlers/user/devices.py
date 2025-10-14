from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.device_management_service import DeviceManagementService

router = Router(name="devices")

def _title(d: dict) -> str:
    model = d.get("deviceModel") or d.get("model") or ""
    plat = d.get("platform") or ""
    parts = []
    if plat:
        parts.append(str(plat))
    if model:
        parts.append(str(model))
    title = " • ".join(parts) or "Устройство"
    return (title[:60] + "…") if len(title) > 61 else title

def _hwid(d: dict) -> str:
    return str(d.get("hwid") or "")

# 1) Команда /devices
@router.message(Command("devices"))
async def devices_entry(msg: Message, session: AsyncSession):
    await _render_list(msg, session, user_id=msg.from_user.id)

# 2) Текстовая кнопка главного меню (ReplyKeyboard): "Управление устройствами"
@router.message(F.text.casefold() == "управление устройствами")
async def devices_text(msg: Message, session: AsyncSession):
    await _render_list(msg, session)

# 3) Callback из инлайн-кнопок главного меню (если используется InlineKeyboard)
@router.callback_query(F.data == "devices_open")
async def open_devices_from_menu(call: CallbackQuery, session: AsyncSession):
    await _render_list(call.message, session, user_id=call.from_user.id)
    await call.answer()

async def _render_list(message: Message, session: AsyncSession, user_id: int | None = None):
    svc = DeviceManagementService()
    uid = user_id or message.from_user.id
    devices = await svc.list_devices(uid, session=session)

    if not devices:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="main_action:back_to_main")
        await message.answer(
            "У вас пока нет сохранённых устройств.\n\n"
            "Если вы подключались раньше — перезапустите приложение и обновите профиль.",
            reply_markup=kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    text = "Ваши устройства:\n\nЧтобы удалить устройство — нажмите на его название."

    for d in devices:
        hw = _hwid(d)
        if not hw:
            continue
        cb = f"d:{hw}"[:64]
        kb.button(text=_title(d), callback_data=cb)

    kb.button(text="⬅️ Назад", callback_data="main_action:back_to_main")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup())

# Шаг подтверждения удаления
@router.callback_query(F.data.startswith("d:"))
async def ask_delete(call: CallbackQuery, session: AsyncSession):
    hw = call.data[2:64]
    if not hw:
        await call.answer("Некорректное устройство", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"y:{hw}"[:64])
    kb.button(text="❌ Отмена", callback_data="n")
    kb.adjust(2)
    await call.message.edit_text(
        f"Удалить устройство?\n\n`{hw}`",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown",
    )

# Выполнить удаление
@router.callback_query(F.data.startswith("y:"))
async def do_delete(call: CallbackQuery, session: AsyncSession):
    hw = call.data[2:64]
    svc = DeviceManagementService()
    ok = await svc.delete_device(call.from_user.id, hw, session=session)
    if ok:
        left = await svc.list_devices(call.from_user.id, session=session)
        if left:
            await call.message.edit_text("Готово. Устройство удалено ✅")
            await _render_list(call.message, session, user_id=call.from_user.id)
        else:
            await call.message.edit_text("Готово. Устройство удалено ✅")
        return
    await call.message.edit_text("Не удалось удалить устройство. Попробуйте позже.")

# Отмена
@router.callback_query(F.data == "n")
async def cancel_delete(call: CallbackQuery, session: AsyncSession):
    await call.message.edit_text("Удаление отменено.")
    await _render_list(call.message, session, user_id=call.from_user.id)
