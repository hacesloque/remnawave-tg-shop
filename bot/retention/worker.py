import os, asyncio, logging
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from bot.retention.templates import TEMPLATES
from bot.retention.sender import send_template

def env_bool(name:str, default:bool=False) -> bool:
    val = os.getenv(name, str(default).lower())
    return str(val).lower() == "true"

def env_int(name:str, default:int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def env_str(name:str, default:str) -> str:
    return os.getenv(name, default)

def in_send_window():
    try:
        window = env_str("RETENTION_SEND_WINDOW", "09:00-22:00")
        start, end = window.split("-")
        now = datetime.now()
        h = now.hour * 60 + now.minute
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        return sh * 60 + sm <= h <= eh * 60 + em
    except Exception:
        return True

async def tick(bot, async_session_factory):
    max_per_tick = env_int("RETENTION_MAX_PER_TICK", 20)
    async with async_session_factory() as session:  # type: AsyncSession
        rows = (await session.execute(text("""
            SELECT o.id, o.user_id, o.template_key
            FROM retention.outbox o
            LEFT JOIN retention.dnd d ON d.user_id = o.user_id
            WHERE o.sent_at IS NULL
              AND o.scheduled_at <= now()
              AND d.user_id IS NULL
            ORDER BY o.scheduled_at
            LIMIT :limit
        """), {"limit": max_per_tick})).mappings().all()

        for r in rows:
            oid, uid, tpl_key = r["id"], r["user_id"], r["template_key"]
            tpl = TEMPLATES.get(tpl_key)
            if not tpl:
                await session.execute(text("UPDATE retention.outbox SET error=:err WHERE id=:id"),
                                      {"err": "unknown template", "id": oid})
                await session.commit()
                continue
            try:
                mid = await send_template(bot, uid, tpl)
                await session.execute(text("UPDATE retention.outbox SET sent_at=now() WHERE id=:id"), {"id": oid})
                await session.execute(text("""
                    INSERT INTO retention.sent_log(user_id, template_key, message_id)
                    VALUES (:uid, :tpl, :mid)
                """), {"uid": uid, "tpl": tpl_key, "mid": mid})
                await session.commit()
                await asyncio.sleep(1.2)
            except Exception as e:
                err = str(e)
                # если пользователь заблокировал бота — кладём в DND и помечаем задачу ошибкой
                if "403" in err and "blocked by the user" in err:
                    await session.execute(text("""
                        INSERT INTO retention.dnd(user_id, reason)
                        VALUES (:uid, 'tg_blocked_403')
                        ON CONFLICT (user_id) DO NOTHING
                    """), {"uid": uid})
                await session.execute(text("UPDATE retention.outbox SET error=:err WHERE id=:id"),
                                      {"err": err[:500], "id": oid})
                await session.commit()
                logging.warning(f"[retention] send_template error to {uid}: {err}")
                await asyncio.sleep(1.2)

async def retention_loop(bot, async_session_factory):
    if not env_bool("RETENTION_ENABLED", False):
        logging.info("[retention] disabled")
        return
    logging.info("[retention] started")
    sleep_sec = env_int("RETENTION_SLEEP_SEC", 30)
    while True:
        try:
            if in_send_window():
                await tick(bot, async_session_factory)
        except Exception as e:
            logging.warning(f"[retention] loop error: {e}")
        await asyncio.sleep(sleep_sec)
