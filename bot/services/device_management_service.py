import aiohttp
import logging
from typing import Any, Dict, List, Optional
from config.settings import get_settings

class DeviceManagementService:
    """
    Работа с HWID-устройствами Remnawave:
      - По tg_user_id получаем user_uuid через /users?telegram_id=
      - Список:  GET  /hwid/devices/{user_uuid}
      - Удаление: POST /hwid/devices/delete  body={user_uuid, hwid}
    """

    def __init__(self):
        s = get_settings()
        self.base = str(s.PANEL_API_URL).rstrip("/")
        self.key  = s.PANEL_API_KEY
        self.mode = getattr(s, "DEVICES_API_MODE", "HWID").upper()
        self.path_find_user = "/users?telegram_id={tg_id}"
        self.path_hwid_list = "/hwid/devices/{user_uuid}"
        self.path_hwid_delete = "/hwid/devices/delete"

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.key}", "Accept": "application/json", "Content-Type": "application/json"}

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
        # 1) основной запрос: /users?telegram_id={tg_id}
        url = f"{self.base}{self.path_find_user.format(tg_id=tg_user_id)}"
        data = await self._get_json(session, url)
        def pick_uuid(data):
            if not isinstance(data, dict): 
                return None
            resp = data.get("response") if isinstance(data, dict) else None
            users = resp.get("users") if isinstance(resp, dict) else None
            if not isinstance(users, list): 
                return None
            # Ищем точное совпадение по telegram_id / telegramId
            for u in users:
                if str(u.get("telegram_id") or u.get("telegramId") or "") == str(tg_user_id):
                    return u.get("uuid")
            # Фолбэк: username == f"tg_{tg_user_id}"
            cand = f"tg_{tg_user_id}"
            for u in users:
                if str(u.get("username") or "") == cand:
                    return u.get("uuid")
            # Если единственная запись — берём её, иначе None
            if len(users) == 1:
                return users[0].get("uuid")
            return None

        uuid = pick_uuid(data) if data else None
        if uuid:
            return uuid

        # 2) фолбэк: /users?username=tg_{tg_id}
        try_user = f"tg_{tg_user_id}"
        url2 = f"{self.base}/users?username={try_user}"
        data2 = await self._get_json(session, url2)
        uuid2 = pick_uuid(data2) if data2 else None
        return uuid2


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

    async def delete_device(self, tg_user_id: int, device_id: str) -> bool:
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            user_uuid = await self._resolve_user_uuid(session, tg_user_id)
            if not user_uuid:
                return False
            url = f"{self.base}{self.path_hwid_delete}"
            status, txt = await self._post_json(session, url, {"user_uuid": user_uuid, "hwid": device_id})
            if status in (200, 204):
                return True
            logging.warning("DELETE(HWID) %s -> %s %s", url, status, txt)
            return False
