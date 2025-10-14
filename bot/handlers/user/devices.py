from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.device_management_service import DeviceManagementService

router = Router(name="devices")

def _title(d: dict) -> str:
    # Короткий, человеко-читаемый заголовок
    model = d.get("deviceModel") or d.get("model") or ""
    plat = d.get("platform") or ""
    parts = []
    if plat:
        parts.append(str(plat))
    if model:
        parts.append(str(model))
    title = " • ".join(parts) or "Устройство"
    # Телеграм ограничивает длину подписи кнопки ~64-70 символами
    return (title[:60] + "…") if len(title) > 61 else title

def _hwid(d: dict) -> str:
    # Для HWID-API идентификатором служит hwid (обычно 16..36 символов)
    return str(d.get("hwid") or "")

@router.message(Command("devices"))
async def devices_entry(msg: Message, session: AsyncSession):
    await _render_list(msg, session)

async def _render_list(message: Message, session: AsyncSession):
    svc = DeviceManagementService()
    devices = await svc.list_devices(message.from_user.id, session=session)

    if not devices:
        await message.answer(
            "У вас пока нет сохранённых устройств.\n\n"
            "Если вы подключались раньше — перезапустите приложение и обновите профиль."
        )
        return

    kb = InlineKeyboardBuilder()
    lines = ["Ваши устройства:\n", "Чтобы удалить устройство — нажмите на его название."]

    for d in devices:
        hw = _hwid(d)
        if not hw:
            continue
        # callback_data <= 64 байт: префикс 2 символа + hwid
        cb = f"d:{hw}"[:64]
        kb.button(text=_title(d), callback_data=cb)

    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())

# Шаг подтверждения удаления
@router.callback_query(F.data.startswith("d:"))
async def ask_delete(call: CallbackQuery, session: AsyncSession):
    hw = call.data[2:64]  # без префикса
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
        await call.message.edit_text("Готово. Устройство удалено ✅")
    else:
        await call.message.edit_text("Не удалось удалить устройство. Попробуйте позже.")
    # Показать обновлённый список следом
    await _render_list(call.message, session)

# Отмена
@router.callback_query(F.data == "n")
async def cancel_delete(call: CallbackQuery, session: AsyncSession):
    await call.message.edit_text("Удаление отменено.")
    await _render_list(call.message, session)
