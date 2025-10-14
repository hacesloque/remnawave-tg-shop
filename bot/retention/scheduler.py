import os, asyncio, logging
from sqlalchemy import text

def env_bool(name:str, default:bool=False) -> bool:
    val = os.getenv(name, str(default).lower())
    return str(val).lower() == "true"

def env_int(name:str, default:int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

# A1: "/start и ушёл" — через 2 часа после регистрации
A1_SQL = """
WITH cand AS (
  SELECT u.user_id AS user_id, (u.registration_date + interval '2 hours') AS sch
  FROM public.users u
  LEFT JOIN public.subscriptions s
         ON s.user_id = u.user_id
        AND s.provider IS NULL
        AND s.is_active = true
  WHERE u.registration_date >= now() - interval '7 days'
    AND u.registration_date <= now() - interval '2 hours'
    AND s.user_id IS NULL
    AND NOT EXISTS (
      SELECT 1 FROM retention.outbox o
      WHERE o.user_id = u.user_id AND o.template_key = 'A1'
    )
    AND NOT EXISTS (
      SELECT 1 FROM retention.dnd d
      WHERE d.user_id = u.user_id
    )
  LIMIT 50
)
INSERT INTO retention.outbox(user_id, segment, template_key, scheduled_at)
SELECT user_id, 'A', 'A1', sch FROM cand;
"""

# B1: триал активирован, но не подключился — через 1 час после активации (трафика нет)
B1_SQL = """
WITH cand AS (
  SELECT s.user_id AS user_id, (s.start_date + interval '1 hour') AS sch
  FROM public.subscriptions s
  WHERE s.provider IS NULL
    AND s.is_active = true
    AND s.start_date >= now() - interval '7 days'
    AND s.start_date <= now() - interval '1 hour'
    AND COALESCE(s.traffic_used_bytes, 0) = 0
    AND NOT EXISTS (
      SELECT 1 FROM retention.outbox o
      WHERE o.user_id = s.user_id AND o.template_key = 'B1'
    )
    AND NOT EXISTS (
      SELECT 1 FROM retention.dnd d
      WHERE d.user_id = s.user_id
    )
  LIMIT 50
)
INSERT INTO retention.outbox(user_id, segment, template_key, scheduled_at)
SELECT user_id, 'B', 'B1', sch FROM cand;
"""

# A2: "/start и ушёл" — напоминание через 24 часа после регистрации (если триал не активирован)
A2_SQL = """
WITH cand AS (
  SELECT u.user_id AS user_id, (u.registration_date + interval '24 hours') AS sch
  FROM public.users u
  LEFT JOIN public.subscriptions s
         ON s.user_id = u.user_id
        AND s.provider IS NULL
        AND s.is_active = true
  WHERE u.registration_date >= now() - interval '10 days'
    AND u.registration_date <= now() - interval '24 hours'
    AND s.user_id IS NULL
    AND NOT EXISTS (
      SELECT 1 FROM retention.outbox o
      WHERE o.user_id = u.user_id AND o.template_key = 'A2'
    )
    AND NOT EXISTS (
      SELECT 1 FROM retention.dnd d
      WHERE d.user_id = u.user_id
    )
  LIMIT 50
)
INSERT INTO retention.outbox(user_id, segment, template_key, scheduled_at)
SELECT user_id, 'A', 'A2', sch FROM cand;
"""

# B2: триал активирован 24+ ч назад, трафика всё ещё нет
B2_SQL = """
WITH cand AS (
  SELECT s.user_id AS user_id, (s.start_date + interval '24 hours') AS sch
  FROM public.subscriptions s
  WHERE s.provider IS NULL
    AND s.is_active = true
    AND s.start_date >= now() - interval '10 days'
    AND s.start_date <= now() - interval '24 hours'
    AND COALESCE(s.traffic_used_bytes, 0) = 0
    AND NOT EXISTS (
      SELECT 1 FROM retention.outbox o
      WHERE o.user_id = s.user_id AND o.template_key = 'B2'
    )
    AND NOT EXISTS (
      SELECT 1 FROM retention.dnd d
      WHERE d.user_id = s.user_id
    )
  LIMIT 50
)
INSERT INTO retention.outbox(user_id, segment, template_key, scheduled_at)
SELECT user_id, 'B', 'B2', sch FROM cand;
"""

async def schedule_tick(async_session_factory):
    async with async_session_factory() as session:
        await session.execute(text(A1_SQL))
        await session.execute(text(B1_SQL))
        await session.execute(text(A2_SQL))
        await session.execute(text(B2_SQL))
        await session.commit()
        logging.info("[retention][scheduler] queued A1/B1/A2/B2 (if any)")

async def scheduler_loop(async_session_factory):
    if not env_bool("RETENTION_SCHEDULER_ENABLED", False):
        logging.info("[retention][scheduler] disabled")
        return
    interval = env_int("RETENTION_SCHEDULE_INTERVAL_SEC", 900)
    logging.info(f"[retention][scheduler] started (A1+B1+A2+B2), interval={interval}s")
    while True:
        try:
            await schedule_tick(async_session_factory)
        except Exception as e:
            logging.warning(f"[retention][scheduler] tick error: {e}")
        await asyncio.sleep(interval)
