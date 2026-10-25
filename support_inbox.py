import asyncio
import time
import unicodedata

import aiohttp
from aiohttp import web


MAX_SUPPORT_PHOTO_BYTES = 5 * 1024 * 1024
MAX_SUPPORT_REQUEST_BYTES = MAX_SUPPORT_PHOTO_BYTES + 64 * 1024
MAX_SUPPORT_MESSAGE_CHARS = 700
SUPPORT_RATE_LIMIT = 5
SUPPORT_RATE_WINDOW_SECONDS = 60 * 60


class SupportInbox:
    """Forward authenticated Mini App support requests privately to the admin."""

    def __init__(self, bot_token, admin_telegram_id, *, session_factory=None,
                 clock=time.monotonic):
        self.bot_token = str(bot_token or "").strip()
        try:
            self.admin_telegram_id = int(admin_telegram_id)
        except (TypeError, ValueError):
            self.admin_telegram_id = 0
        self.session_factory = session_factory
        self.clock = clock
        self._recent_requests = {}
        self._rate_lock = asyncio.Lock()

    @staticmethod
    def _error(message, status):
        return web.json_response({"success": False, "message": message}, status=status)

    @staticmethod
    def _utf16_length(value):
        return len(value.encode("utf-16-le")) // 2

    @staticmethod
    def _normalize_message(value):
        value = unicodedata.normalize("NFC", str(value or ""))
        return "".join(
            char for char in value
            if char in "\n\t" or unicodedata.category(char)[0] != "C"
        ).strip()

    @staticmethod
    def _photo_type(data):
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", "support.png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", "support.jpg"
        return None, None

    async def _parse_request(self, request):
        if request.content_type != "multipart/form-data":
            return None, None, None, 415, "Send the request using the support form."
        if request.content_length and request.content_length > MAX_SUPPORT_REQUEST_BYTES:
            return None, None, None, 413, "The request is too large. Choose an image under 5 MB."

        message = None
        photo = None
        photo_type = None
        try:
            reader = await request.multipart()
            async for part in reader:
                if part.name not in ("message", "photo"):
                    return None, None, None, 400, "The support form contains an unexpected field."
                if part.name == "message":
                    if message is not None or part.filename:
                        return None, None, None, 400, "The support form is invalid."
                    raw_message = await part.read(decode=False)
                    if len(raw_message) > MAX_SUPPORT_MESSAGE_CHARS * 8:
                        return None, None, None, 400, "Keep your message under 700 characters."
                    try:
                        message = raw_message.decode("utf-8")
                    except UnicodeDecodeError:
                        return None, None, None, 400, "The support message must be valid text."
                else:
                    if photo is not None:
                        return None, None, None, 400, "Attach only one image."
                    if not part.filename:
                        await part.read(decode=False)
                        continue
                    photo = await part.read(decode=False)
                    if len(photo) > MAX_SUPPORT_PHOTO_BYTES:
                        return None, None, None, 413, "Choose an image under 5 MB."
                    if not photo:
                        return None, None, None, 400, "The selected image is empty."
                    photo_type, _ = self._photo_type(photo)
                    if not photo_type:
                        return None, None, None, 415, "Attach a JPG or PNG image."
        except web.HTTPRequestEntityTooLarge:
            return None, None, None, 413, "The request is too large. Choose an image under 5 MB."
        except web.HTTPException:
            return None, None, None, 400, "The support form could not be read. Please try again."
        except (ValueError, UnicodeDecodeError):
            return None, None, None, 400, "The support form could not be read. Please try again."

        message = self._normalize_message(message)
        if not message:
            return None, None, None, 400, "Describe the issue before sending."
        if len(message) > MAX_SUPPORT_MESSAGE_CHARS:
            return None, None, None, 400, "Keep your message under 700 characters."
        return message, photo, photo_type, None, None

    async def _reserve_rate_slot(self, uid):
        now = self.clock()
        async with self._rate_lock:
            recent = [stamp for stamp in self._recent_requests.get(uid, [])
                      if now - stamp < SUPPORT_RATE_WINDOW_SECONDS]
            if len(recent) >= SUPPORT_RATE_LIMIT:
                self._recent_requests[uid] = recent
                return False
            recent.append(now)
            self._recent_requests[uid] = recent
            return True

    async def _deliver(self, uid, username, message, photo, photo_type):
        username = "".join(
            char for char in str(username or "")
            if char.isascii() and (char.isalnum() or char == "_")
        )[:32] or "not set"
        header = (
            "Technical support request\n"
            f"Telegram user ID: {uid}\n"
            f"Telegram username: @{username}\n\n"
        )
        text = header + message
        timeout = aiohttp.ClientTimeout(total=20)
        session_factory = self.session_factory or aiohttp.ClientSession
        async with session_factory(timeout=timeout) as session:
            if photo is None:
                if self._utf16_length(text) > 4096:
                    raise ValueError("Support text exceeds Telegram's message limit.")
                async with session.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.admin_telegram_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                    timeout=timeout,
                ) as response:
                    result = await response.json(content_type=None)
            else:
                if self._utf16_length(text) > 1024:
                    raise ValueError("Support caption exceeds Telegram's caption limit.")
                form = aiohttp.FormData()
                form.add_field("chat_id", str(self.admin_telegram_id))
                form.add_field("caption", text)
                _, filename = self._photo_type(photo)
                form.add_field(
                    "photo", photo, filename=filename, content_type=photo_type)
                async with session.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendPhoto",
                    data=form,
                    timeout=timeout,
                ) as response:
                    result = await response.json(content_type=None)
        return response.status == 200 and isinstance(result, dict) and result.get("ok") is True

    async def handle(self, request):
        if not self.bot_token or self.admin_telegram_id <= 0:
            return self._error("Technical support is temporarily unavailable.", 503)

        message, photo, photo_type, error_status, error_message = await self._parse_request(request)
        if error_status:
            return self._error(error_message, error_status)

        uid = int(request["telegram_user"]["id"])
        if not await self._reserve_rate_slot(uid):
            return self._error("You have sent several requests recently. Please try again later.", 429)

        try:
            delivered = await self._deliver(
                uid, request["telegram_user"].get("username"), message, photo, photo_type)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return self._error(
                "Delivery could not be confirmed. Check your Telegram chat before retrying.", 502)
        except Exception:
            return self._error("Could not deliver the support request. Please try again later.", 502)
        if not delivered:
            return self._error("Could not deliver the support request. Please try again later.", 502)
        return web.json_response({
            "success": True,
            "message": "Your support request was sent privately to the administrator.",
        })
