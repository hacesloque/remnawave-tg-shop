-- ══ Отчёт за сегодня (MSK) ══════════════════
\set QUIET 1
\pset border 2
\pset linestyle unicode
\pset pager off
SET TIME ZONE 'Europe/Moscow';
\unset QUIET

-- ===== Таблица 1: сводка по шаблонам с учётом 403 =====
WITH errors_403_by_tpl AS (
    SELECT template_key AS шаблон, COUNT(*) AS с_ошибкой
    FROM retention.outbox
    WHERE created_at::date = CURRENT_DATE
      AND error ILIKE '%403%'
    GROUP BY template_key
),
sent_summary AS (
    SELECT
        template_key                                  AS шаблон,
        MIN(sent_at)                                  AS первый_отправлен,
        MAX(sent_at)                                  AS последний_отправлен,
        COUNT(*)                                      AS успешно_отправлено,
        LEFT(template_key, 1)                         AS seg_letter,
        COALESCE(NULLIF(SUBSTRING(template_key FROM '\d+'), ''), '0')::int AS seg_idx
    FROM retention.sent_log
    WHERE sent_at::date = CURRENT_DATE
    GROUP BY template_key
)
SELECT
    s.шаблон,
    s.успешно_отправлено,
    COALESCE(e.с_ошибкой, 0)                         AS с_ошибкой,
    TO_CHAR(s.первый_отправлен,   'HH24:MI:SS')      AS первый_отправлен,
    TO_CHAR(s.последний_отправлен,'HH24:MI:SS')      AS последний_отправлен
FROM sent_summary s
LEFT JOIN errors_403_by_tpl e ON e.шаблон = s.шаблон
ORDER BY s.seg_letter, s.seg_idx;

\echo
\echo '── Подробности за сегодня (MSK) ──'

-- ===== Таблица 2: детали (user_id + username) + статус =====
WITH sent_rows AS (
    SELECT template_key, user_id, sent_at AS ts, FALSE AS is_block
    FROM retention.sent_log
    WHERE sent_at::date = CURRENT_DATE
),
error_rows AS (
    SELECT template_key, user_id, created_at AS ts, TRUE AS is_block
    FROM retention.outbox
    WHERE created_at::date = CURRENT_DATE
      AND error ILIKE '%403%'
),
events AS (
    SELECT * FROM sent_rows
    UNION ALL
    SELECT * FROM error_rows
),
ranked AS (
    SELECT
        e.template_key                                AS шаблон,
        e.user_id,
        e.ts,
        CASE WHEN e.is_block THEN 0 ELSE 1 END        AS prio,
        CASE WHEN e.is_block THEN 'Заблокирован 🚫' ELSE 'Отправлено ✅' END AS статус,
        LEFT(e.template_key, 1)                       AS seg_letter,
        COALESCE(NULLIF(SUBSTRING(e.template_key FROM '\d+'), ''), '0')::int AS seg_idx
    FROM events e
),
final AS (
    SELECT DISTINCT ON (r.шаблон, r.user_id)
        r.шаблон,
        r.user_id,
        CASE WHEN u.username IS NOT NULL AND u.username <> ''
             THEN '@' || u.username ELSE '' END       AS username,
        TO_CHAR(r.ts, 'HH24:MI:SS')                   AS время,
        r.статус,
        r.seg_letter,
        r.seg_idx,
        r.ts,
        r.prio
    FROM ranked r
    LEFT JOIN public.users u ON u.user_id = r.user_id
    ORDER BY r.шаблон, r.user_id, r.prio, r.ts DESC
)
SELECT
    шаблон,
    user_id,
    username,
    время,
    статус
FROM final
ORDER BY seg_letter, seg_idx, ts ASC
LIMIT 1000;
