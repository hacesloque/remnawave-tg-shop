import aiohttp
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from config.settings import get_settings


class DeviceManagementService:
    """
    Управление HWID-устройствами Remnawave.
      user_uuid:
        1) берём из нашей БД: SELECT panel_user_uuid FROM users WHERE user_id = :tg_id
        2) если нет — по API:
           - /users?username=tg_{id} (строгое совпадение username)
           - /users?telegram_id={id} (строгая фильтрация по полю telegram_id/telegramId)
      список:   GET  /hwid/devices/{user_uuid}
      удаление: POST /hwid/devices/delete  body={"user_uuid","hwid"}
    """

    def __init__(self) -> None:
        s = get_settings()
        self.base = str(s.PANEL_API_URL).rstrip("/")
        self.key: str = s.PANEL_API_KEY

        # Пути поиска пользователя
        self.path_find_user: str = getattr(
            s, "PANEL_FIND_USER_BY_TG_PATH", "/users?telegram_id={tg_id}"
        )
        self.path_find_user_by_username: str = getattr(
            s, "PANEL_FIND_USER_BY_USERNAME_PATH", "/users?username={username}"
        )

        # Пути HWID API
        self.path_hwid_list: str = getattr(
            s, "PANEL_DEVICES_LIST_PATH", "/hwid/devices/{user_uuid}"
        )
        self.path_hwid_delete: str = getattr(
            s, "PANEL_DEVICE_DELETE_PATH", "/hwid/devices/delete"
        )

    # ----------------- helpers -----------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _get_json(self, session: aiohttp.ClientSession, url: str) -> Optional[Any]:
        async with session.get(url, timeout=15) as r:
            if r.status != 200:
                try:
                    body = await r.text()
                except Exception:
                    body = "<non-text>"
                logging.warning("GET %s -> %s %s", url, r.status, body)
                return None
            try:
                return await r.json()
            except Exception:
                logging.exception("Failed to decode JSON from %s", url)
                return None

    async def _post_json(self, session: aiohttp.ClientSession, url: str, body: Dict[str, Any]) -> tuple[int, str]:
        async with session.post(url, json=body, timeout=15) as r:
            txt = await r.text()
            return r.status, txt

    # ----------------- user uuid resolve -----------------

    async def _resolve_user_uuid_from_db(self, db: AsyncSession, tg_user_id: int) -> Optional[str]:
        """Берём panel_user_uuid из нашей БД (users.user_id)."""
        try:
            res = await db.execute(
                text("SELECT panel_user_uuid FROM users WHERE user_id = :uid LIMIT 1"),
                {"uid": tg_user_id},
            )
            row = res.first()
            if row and row[0]:
                return str(row[0])
        except Exception as e:
            logging.warning("DB resolve user_uuid failed: %s", e)
        return None

    async def _resolve_user_uuid_http(self, http: aiohttp.ClientSession, tg_user_id: int) -> Optional[str]:
        """Фолбэк через панель: сначала username=tg_{id}, потом telegram_id={id} с ручной фильтрацией."""
        username = f"tg_{tg_user_id}"

        def pick_uuid_strict(data: Any, *, username: Optional[str] = None, tg_id: Optional[int] = None) -> Optional[str]:
            if not isinstance(data, dict):
                return None
            resp = data.get("response")
            users = resp.get("users") if isinstance(resp, dict) else None
            if not isinstance(users, list):
                return None
            if username is not None:
                for u in users:
                    if str(u.get("username") or "") == username:
                        return u.get("uuid")
            if tg_id is not None:
                for u in users:
                    val = u.get("telegram_id", u.get("telegramId"))
                    if val is not None and str(val) == str(tg_id):
                        return u.get("uuid")
            return None

        # 1) поиск по username
        url_un = f"{self.base}{self.path_find_user_by_username.format(username=username)}"
        data_un = await self._get_json(http, url_un)
        uuid = pick_uuid_strict(data_un, username=username)
        if uuid:
            return uuid

        # 2) поиск по telegram_id с фильтрацией
        url_tg = f"{self.base}{self.path_find_user.format(tg_id=tg_user_id)}"
        data_tg = await self._get_json(http, url_tg)
        uuid = pick_uuid_strict(data_tg, tg_id=tg_user_id)
        return uuid

    # ----------------- public API -----------------

    async def list_devices(self, tg_user_id: int, session: Optional[AsyncSession] = None) -> List[Dict[str, Any]]:
        # 1) Пробуем UUID из нашей БД
        db_uuid: Optional[str] = None
        if session is not None:
            db_uuid = await self._resolve_user_uuid_from_db(session, tg_user_id)

        async with aiohttp.ClientSession(headers=self._headers()) as http:
            user_uuid = db_uuid or await self._resolve_user_uuid_http(http, tg_user_id)
            if not user_uuid:
                return []
            url = f"{self.base}{self.path_hwid_list.format(user_uuid=user_uuid)}"
            data = await self._get_json(http, url)
            if not data or not isinstance(data, dict):
                return []
            resp = data.get("response")
            devices = resp.get("devices") if isinstance(resp, dict) else None
            return devices or []

    async def delete_device(self, tg_user_id: int, device_hwid: str, session: Optional[AsyncSession] = None) -> bool:
        # 1) Пробуем UUID из нашей БД
        db_uuid: Optional[str] = None
        if session is not None:
            db_uuid = await self._resolve_user_uuid_from_db(session, tg_user_id)

        async with aiohttp.ClientSession(headers=self._headers()) as http:
            user_uuid = db_uuid or await self._resolve_user_uuid_http(http, tg_user_id)
            if not user_uuid:
                return False
            url = f"{self.base}{self.path_hwid_delete}"
            status, txt = await self._post_json(http, url, {"userUuid": user_uuid, "hwid": device_hwid})
            if status in (200, 204):
                return True
            logging.warning("HWID delete %s -> %s %s", url, status, txt)
            return False
