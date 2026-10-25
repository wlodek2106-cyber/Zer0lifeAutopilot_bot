import json
import unittest

from support_inbox import SupportInbox


class FakePart:
    def __init__(self, name, value, filename=None):
        self.name = name
        self.value = value if isinstance(value, bytes) else value.encode("utf-8")
        self.filename = filename

    async def read(self, decode=False):
        return self.value


class FakeReader:
    def __init__(self, parts):
        self.parts = iter(parts)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.parts)
        except StopIteration:
            raise StopAsyncIteration


class FakeRequest(dict):
    content_type = "multipart/form-data"
    content_length = 1024

    def __init__(self, parts, user=None):
        super().__init__(telegram_user=user or {"id": 123456, "username": "tester"})
        self.parts = parts

    async def multipart(self):
        return FakeReader(self.parts)


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, content_type=None):
        return {"ok": True, "result": {"message_id": 99}}


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse()


class FakeSessionFactory:
    def __init__(self):
        self.calls = []

    def __call__(self, **_):
        return FakeSession(self.calls)


class SupportInboxTests(unittest.IsolatedAsyncioTestCase):
    def make_inbox(self, *, clock=None):
        factory = FakeSessionFactory()
        options = {"session_factory": factory}
        if clock:
            options["clock"] = clock
        return SupportInbox("test-bot-token", 987654, **options), factory

    async def test_text_request_is_sent_only_to_configured_admin(self):
        inbox, factory = self.make_inbox()
        response = await inbox.handle(FakeRequest([
            FakePart("message", "The wallet screen is stuck.")
        ]))

        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.text)["success"])
        self.assertEqual(len(factory.calls), 1)
        call = factory.calls[0]
        self.assertTrue(call["url"].endswith("/sendMessage"))
        self.assertEqual(call["json"]["chat_id"], 987654)
        self.assertIn("Telegram user ID: 123456", call["json"]["text"])
        self.assertIn("@tester", call["json"]["text"])
        self.assertIn("The wallet screen is stuck.", call["json"]["text"])
        self.assertNotIn("parse_mode", call["json"])

    async def test_png_request_sends_caption_and_image_to_admin(self):
        inbox, factory = self.make_inbox()
        png = b"\x89PNG\r\n\x1a\nsmall-image-data"
        response = await inbox.handle(FakeRequest([
            FakePart("message", "The button does not respond."),
            FakePart("photo", png, filename="../../private.png"),
        ]))

        self.assertEqual(response.status, 200)
        call = factory.calls[0]
        self.assertTrue(call["url"].endswith("/sendPhoto"))
        self.assertNotIn("json", call)
        fields = call["data"]._fields
        names = [field[0].get("name") for field in fields]
        self.assertEqual(names, ["chat_id", "caption", "photo"])
        self.assertEqual(fields[0][2], "987654")
        self.assertIn("The button does not respond.", fields[1][2])
        self.assertEqual(fields[2][0].get("filename"), "support.png")
        self.assertEqual(fields[2][2], png)

    async def test_empty_message_and_non_image_are_rejected_without_sending(self):
        inbox, factory = self.make_inbox()
        empty = await inbox.handle(FakeRequest([FakePart("message", " \n ")]))
        invalid_photo = await inbox.handle(FakeRequest([
            FakePart("message", "Please inspect this."),
            FakePart("photo", b"not an image", filename="photo.png"),
        ]))

        self.assertEqual(empty.status, 400)
        self.assertEqual(invalid_photo.status, 415)
        self.assertEqual(factory.calls, [])

    async def test_support_requests_are_rate_limited_per_telegram_user(self):
        inbox, factory = self.make_inbox(clock=lambda: 1000.0)
        results = []
        for _ in range(6):
            results.append(await inbox.handle(FakeRequest([
                FakePart("message", "Please help.")
            ])))

        self.assertEqual([response.status for response in results],
                         [200, 200, 200, 200, 200, 429])
        self.assertEqual(len(factory.calls), 5)


if __name__ == "__main__":
    unittest.main()
