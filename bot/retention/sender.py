import json, logging, aiohttp

async def send_template(bot, user_id: int, tpl: dict):
    """
    Отправка сообщения по шаблону из templates.py
    """
    text = tpl["text"]
    kb = tpl.get("reply_markup")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{bot.token}/sendMessage",
                data={
                    "chat_id": user_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": json.dumps(kb)
                },
                timeout=10
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Telegram error {resp.status}: {body}")
                data = await resp.json()
                return data.get("result", {}).get("message_id")
    except Exception as e:
        logging.warning(f"[retention] send_template error to {user_id}: {e}")
        raise
