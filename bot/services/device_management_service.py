import aiohttp
import logging
from typing import Any, Dict, List, Optional
from config.settings import get_settings

class DeviceManagementService:
    """
    HWID-устройства Remnawave:
      1) user_uuid: сначала /users?username=tg_{id}, затем /users?telegram_id={id} с ручной фильтрацией
      2) список:    GET  /hwid/devices/{user_uuid}
      3) удаление:  POST /hwid/devices/delete  body={user_uuid, hwid}
    """

    def __init__(self):
        s = get_settings()
        self.base = str(s.PANEL_API_URL).rstrip("/")           # https://admin.netaway.top/api
        self.key  = s.PANEL_API_KEY

        # Пути поиска пользователя
        self.path_find_user = getattr(
            s, "PANEL_FIND_USER_BY_TG_PATH",
            "/users?telegram_id={tg_id}"
        )
        self.path_find_user_by_username = getattr(
            s, "PANEL_FIND_USER_BY_USERNAME_PATH",
            "/users?username={username}"
        )

        # Пути HWID API
        self.path_hwid_list = getattr(
            s, "PANEL_DEVICES_LIST_PATH",
            "/hwid/devices/{user_uuid}"
        )
        self.path_hwid_delete = getattr(
            s, "PANEL_DEVICE_DELETE_PATH",
            "/hwid/devices/delete"
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _get_json(self, session: aiohttp.ClientSession, url: str) -> Optional[Any]:
        async with session.get(url, timeout=15) as r:
            if r.status != 200:
                logging.warning("GET %s -> %s %s", url, r.status, await r.text())
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

    async def _resolve_user_uuid(self, session: aiohttp.ClientSession, tg_user_id: int) -> Optional[str]:
        """
        Универсально для ЛЮБОГО пользователя:
        1) точный поиск по username=tg_{id}
        2) поиск по telegram_id с ручной фильтрацией (ни в коем случае не берём "первого")
        """
        username = f"tg_{tg_user_id}"

        def pick_uuid_strict(data, *, username=None, tg_id=None):
            if not isinstance(data, dict):
                return None
            resp = data.get("response")
            users = resp.get("users") if isinstance(resp, dict) else None
            if not isinstance(users, list):
                return None
            if username:
                for u in users:
                    if str(u.get("username") or "") == username:
                        return u.get("uuid")
            if tg_id is not None:
                for u in users:
                    val = u.get("telegram_id", u.get("telegramId"))
                    if val is not None and str(val) == str(tg_id):
                        return u.get("uuid")
            return None

        # 1) username
        url_un = f"{self.base}{self.path_find_user_by_username.format(username=username)}"
        data_un = await self._get_json(session, url_un)
        uuid = pick_uuid_strict(data_un, username=username)
        if uuid:
            return uuid

        # 2) telegram_id
        url_tg = f"{self.base}{self.path_find_user.format(tg_id=tg_user_id)}"
        data_tg = await self._get_json(session, url_tg)
        uuid = pick_uuid_strict(data_tg, tg_id=tg_user_id)
        return uuid

    async def list_devices(self, tg_user_id: int) -> List[Dict[str, Any]]:
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            user_uuid = await self._resolve_user_uuid(session, tg_user_id)
            if not user_uuid:
                return []
            url = f"{self.base}{self.path_hwid_list.format(user_uuid=user_uuid)}"
            data = await self._get_json(session, url)
            if not data:
                return []
            resp = data.get("response") if isinstance(data, dict) else None
            devs = resp.get("devices") if isinstance(resp, dict) else None
            return devs or []

    async def delete_device(self, tg_user_id: int, device_hwid: str) -> bool:
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            user_uuid = await self._resolve_user_uuid(session, tg_user_id)
            if not user_uuid:
                return False
            url = f"{self.base}{self.path_hwid_delete}"
            status, txt = await self._post_json(session, url, {"user_uuid": user_uuid, "hwid": device_hwid})
            if status in (200, 204):
                return True
            logging.warning("DELETE(HWID) %s -> %s %s", url, status, txt)
            return False
