import json
import logging
import hmac
import hashlib
from typing import Optional

from aiohttp import web
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from .panel_api_service import PanelApiService
from bot.middlewares.i18n import JsonI18n
from bot.keyboards.inline.user_keyboards import get_subscribe_only_markup, get_autorenew_cancel_keyboard
from db.dal import user_dal
from bot.utils.date_utils import add_months


EVENT_MAP = {
    "user.expires_in_72_hours": (3, "subscription_72h_notification"),
    "user.expires_in_48_hours": (2, "subscription_48h_notification"),
    "user.expires_in_24_hours": (1, "subscription_24h_notification"),
}


class PanelWebhookService:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        i18n: JsonI18n,
        async_session_factory: sessionmaker,
        panel_service: PanelApiService,
    ):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.panel_service = panel_service

    async def _send_message(
        self,
        user_id: int,
        lang: str,
        message_key: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        **kwargs,
    ):
        _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw)
        try:
            await self.bot.send_message(
                user_id, _(message_key, **kwargs), reply_markup=reply_markup
            )
        except Exception as e:
            logging.error(f"Failed to send notification to {user_id}: {e}")

    async def _handle_expired_subscription(
        self,
        session,
        user_id: int,
        user_payload: dict,
        lang: str,
        markup,
        first_name: str,
    ) -> bool:
        """
        Handle expired subscription — auto-renew Tribute users if no cancellation was received.

        Returns True if an auto-renewal was performed (and renewal message sent), False otherwise.
        """
        from db.dal import subscription_dal, payment_dal  # local imports to avoid cycles
        from datetime import datetime, timezone

        try:
            auto_renewed = False

            # Anti-duplication: if user already has active sub (end_date > now), skip
            now_utc = datetime.now(timezone.utc)
            existing_active = await subscription_dal.get_active_subscriptions_for_user(session, user_id)
            if any(getattr(s, "end_date", None) and s.end_date > now_utc for s in existing_active):
                latest_end = max(s.end_date for s in existing_active if s.end_date)
                logging.info(
                    f"Expired event dedup: user {user_id} already has active subscription until {latest_end} — skipping auto-renewal"
                )
                return True

            # Active (or most-recent) subs for user
            user_subs = await subscription_dal.get_active_subscriptions_for_user(session, user_id)

            for sub in user_subs:
                # If panel marked as cancelled (handled by Tribute 'cancelled_subscription'), skip auto-renew
                if sub.status_from_panel == "CANCELLED":
                    logging.info(
                        f"Subscription {sub.subscription_id} for user {user_id} was cancelled, skipping auto-renewal"
                    )
                    continue

                # Determine last Tribute duration (months); None if never paid via Tribute
                last_tribute_duration = await payment_dal.get_last_tribute_payment_duration(session, user_id)

                if last_tribute_duration is None:
                    continue

                logging.info(
                    f"Auto-renewing tribute subscription for user {user_id} for {last_tribute_duration} months"
                )

                # Base extension from the later of current end_date (may include promos/manual ext) or now
                now_utc = datetime.now(timezone.utc)
                current_end = getattr(sub, "end_date", None) or now_utc
                base = current_end if current_end > now_utc else now_utc
                new_end_date = add_months(base, last_tribute_duration)

                # Update local DB subscription
                await subscription_dal.update_subscription(
                    session,
                    sub.subscription_id,
                    {
                        "end_date": new_end_date,
                        "status_from_panel": "ACTIVE",
                        "is_active": True,
                    },
                )

                # Try to extend expiry on the panel as well (best-effort)
                try:
                    panel_payload = {
                        "uuid": sub.panel_user_uuid,
                        "expireAt": new_end_date.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        "status": "ACTIVE",
                    }
                    _resp = await self.panel_service.update_user_details_on_panel(
                        sub.panel_user_uuid, panel_payload, log_response=True
                    )
                    if _resp:
                        logging.info(
                            f"Panel expiry updated for user {user_id} (panel_uuid {sub.panel_user_uuid}) to {new_end_date}"
                        )
                except Exception as e_panel:
                    logging.error(
                        f"Failed to update panel expiry for user {user_id} (panel_uuid {sub.panel_user_uuid}): {e_panel}"
                    )

                # Record synthetic succeeded payment mirroring last tribute amount/currency (for history/analytics)
                try:
                    last_payment = await payment_dal.get_last_tribute_payment(session, user_id)
                    if last_payment and last_payment.amount and last_payment.currency:
                        provider_payment_id = f"tribute_auto_{user_id}_{sub.subscription_id}_{new_end_date.strftime('%Y%m%d')}"
                        await payment_dal.ensure_payment_with_provider_id(
                            session,
                            user_id=user_id,
                            amount=float(last_payment.amount),
                            currency=last_payment.currency,
                            months=last_tribute_duration,
                            description="Tribute auto-renewal",
                            provider="tribute",
                            provider_payment_id=provider_payment_id,
                        )
                except Exception as e_pay:
                    logging.error(
                        f"Failed to record auto-renewal payment for user {user_id}, sub {sub.subscription_id}: {e_pay}"
                    )

                auto_renewed = True
                break

            await session.commit()
            return auto_renewed

        except Exception as e:
            logging.error(f"Error handling expired subscription for user {user_id}: {e}", exc_info=True)
            await session.rollback()
            return False

    async def handle_event(self, event_name: str, user_payload: dict):
        telegram_id = user_payload.get("telegramId")
        if not telegram_id:
            logging.warning("Panel webhook without telegramId received")
            return
        user_id = int(telegram_id)

        if not self.settings.SUBSCRIPTION_NOTIFICATIONS_ENABLED:
            return

        async with self.async_session_factory() as session:
            db_user = await user_dal.get_user_by_id(session, user_id)
            lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
            first_name = db_user.first_name or f"User {user_id}" if db_user else f"User {user_id}"

        markup = get_subscribe_only_markup(lang, self.i18n)

        if event_name in EVENT_MAP:
            days_left, msg_key = EVENT_MAP[event_name]
            if days_left == 1:
                # Trigger auto-renew via SubscriptionService (wired in at factory)
                try:
                    subscription_service = getattr(self, "subscription_service", None)
                    if subscription_service:
                        async with self.async_session_factory() as session:
                            from db.dal import subscription_dal
                            sub = await subscription_dal.get_active_subscription_by_user_id(session, user_id)
                            if sub and sub.auto_renew_enabled and sub.provider != 'tribute':
                                try:
                                    ok = await subscription_service.charge_subscription_renewal(session, sub)
                                    # If initiation succeeded, suppress the 24h reminder by returning early
                                    if ok:
                                        await session.commit()
                                        return
                                    else:
                                        await session.rollback()
                                except Exception:
                                    await session.rollback()
                                    logging.exception("Auto-renew attempt (24h) failed")
                except Exception:
                    logging.exception("Auto-renew trigger (24h) failed pre-check")
            if days_left <= self.settings.SUBSCRIPTION_NOTIFY_DAYS_BEFORE:
                # For 48h event, if auto-renew is enabled and not tribute, show special notice with cancel button
                if days_left == 2:
                    async with self.async_session_factory() as session:
                        from db.dal import subscription_dal
                        sub = await subscription_dal.get_active_subscription_by_user_id(session, user_id)
                        logging.info(
                            "48h webhook check: user_id=%s sub_found=%s auto_renew=%s provider=%s",
                            user_id,
                            bool(sub),
                            getattr(sub, 'auto_renew_enabled', None) if sub else None,
                            getattr(sub, 'provider', None) if sub else None,
                        )
                        if sub and sub.auto_renew_enabled and sub.provider != 'tribute':
                            cancel_kb = get_autorenew_cancel_keyboard(lang, self.i18n)
                            await self._send_message(
                                user_id,
                                lang,
                                "autorenew_48h_charge_tomorrow_notice",
                                reply_markup=cancel_kb,
                                user_name=first_name,
                            )
                            return
                await self._send_message(
                    user_id,
                    lang,
                    msg_key,
                    reply_markup=markup,
                    user_name=first_name,
                    end_date=user_payload.get("expireAt", "")[:10],
                )

        elif event_name == "user.expired":
            async with self.async_session_factory() as session:
                auto_renewed = await self._handle_expired_subscription(
                    session, user_id, user_payload, lang, markup, first_name
                )

            # If auto-renewed via Tribute, suppress expiration notification. Otherwise, send it if enabled.
            if not auto_renewed and self.settings.SUBSCRIPTION_NOTIFY_ON_EXPIRE:
                await self._send_message(
                    user_id,
                    lang,
                    "subscription_expired_notification",
                    reply_markup=markup,
                    user_name=first_name,
                    end_date=user_payload.get("expireAt", "")[:10],
                )

        elif event_name == "user.expired_24_hours_ago" and self.settings.SUBSCRIPTION_NOTIFY_AFTER_EXPIRE:
            await self._send_message(
                user_id,
                lang,
                "subscription_expired_yesterday_notification",
                reply_markup=markup,
                user_name=first_name,
                end_date=user_payload.get("expireAt", "")[:10],
            )

    async def handle_webhook(self, raw_body: bytes, signature_header: Optional[str]) -> web.Response:
        # Optional HMAC verification (enabled if secret set)
        if self.settings.PANEL_WEBHOOK_SECRET:
            if not signature_header:
                return web.Response(status=403, text="no_signature")
            expected_sig = hmac.new(
                self.settings.PANEL_WEBHOOK_SECRET.encode(),
                raw_body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected_sig, signature_header):
                return web.Response(status=403, text="invalid_signature")

        # Parse JSON
        try:
            payload = json.loads(raw_body.decode())
        except Exception:
            return web.Response(status=400, text="bad_request")

        event_name = payload.get("name") or payload.get("event")
        user_data = payload.get("payload") or payload.get("data", {})
        if isinstance(user_data, dict) and "user" in user_data:
            user_data = user_data.get("user") or user_data

        telegram_id = user_data.get("telegramId") if isinstance(user_data, dict) else None

        if not event_name:
            return web.Response(status=200, text="ok_no_event")

        logging.info(
            "Panel webhook event received: %s; telegramId=%s",
            event_name,
            telegram_id if telegram_id is not None else "N/A",
        )

        await self.handle_event(event_name, user_data)
        return web.Response(status=200, text="ok")


async def panel_webhook_route(request: web.Request):
    service: PanelWebhookService = request.app["panel_webhook_service"]
    raw = await request.read()
    signature_header = request.headers.get("X-Remnawave-Signature")
    return await service.handle_webhook(raw, signature_header)
