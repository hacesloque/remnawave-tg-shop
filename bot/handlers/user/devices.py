from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config.settings import get_settings
from bot.services.device_management_service import DeviceManagementService
from sqlalchemy.ext.asyncio import AsyncSession

router = Router(name="devices")

def _title(d: dict) -> str:
    # Отображаемое имя устройства
    return d.get("deviceModel") or d.get("model") or d.get("platform") or d.get("userAgent") or "Устройство"

def _id(d: dict) -> str:
    # Для HWID-API идентификатором служит hwid
    return str(d.get("hwid") or d.get("id") or d.get("uuid") or "")

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
    lines = ["Ваши устройства:\n"]
    for d in devices:
        did = _id(d)
        if not did:
            continue
        name = _title(d)
        platform = d.get("platform")
        caption = f"{name}" + (f" · {platform}" if platform else "")
        lines.append("• " + caption)
        kb.button(text=caption[:40], callback_data=f"dev:ask:{did}:{(name or 'device')[:40]}")
    kb.adjust(1)
    lines.append("\nЧтобы удалить устройство — нажмите на него.")
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())

@router.message(Command("devices"))
async def devices_entry(msg: Message, session: AsyncSession):
    s = get_settings()
    if not getattr(s, "DEVICES_MANAGEMENT_ENABLED", False):
        await msg.answer("Управление устройствами временно недоступно.")
        return
    await _render_list(msg, session)

@router.callback_query(F.data.startswith("dev:ask:"))
async def ask_delete(call: CallbackQuery, session: AsyncSession):
    s = get_settings()
    if not getattr(s, "DEVICES_MANAGEMENT_ENABLED", False):
        await call.answer("Сейчас недоступно", show_alert=True)
        return
    _, _, dev_id, name = call.data.split(":", 3)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"dev:confirm:{dev_id}")
    kb.button(text="❌ Нет", callback_data="dev:cancel")
    kb.adjust(2)
    await call.message.answer(f"Удалить устройство «{name}»?", reply_markup=kb.as_markup())
    await call.answer()

@router.callback_query(F.data == "dev:cancel")
async def cancel(call: CallbackQuery):
    await call.answer("Отменено")

@router.callback_query(F.data.startswith("dev:confirm:"))
async def do_delete(call: CallbackQuery, session: AsyncSession):
    s = get_settings()
    if not getattr(s, "DEVICES_MANAGEMENT_ENABLED", False):
        await call.answer("Сейчас недоступно", show_alert=True)
        return
    dev_id = call.data.split(":", 2)[2]
    svc = DeviceManagementService()
    ok = await svc.delete_device(call.from_user.id, dev_id, session=session)
    if ok:
        await call.answer("Удалено")
        await call.message.answer("Устройство удалено. Обновляю список…")
        await _render_list(call.message, session)
    else:
        await call.answer("Ошибка", show_alert=True)
        await call.message.answer("Не удалось удалить устройство. Попробуйте позже или напишите в поддержку.")
