import io
import unittest
from unittest.mock import patch

from app.api import TelegramAPI, TelegramAPIError
from app.ocr import OCRWorker, extract_tesseract, matched_words
from app.service import GroupBot
from app.store import Store
from tests.test_service import FakeTelegramAPI


class FakeTesseract:
    def __init__(self, text, code=0):
        self.stdout = text.encode("utf-8")
        self.returncode = code


class OCRFunctionsTests(unittest.TestCase):
    def test_confident_chinese_and_english_keywords(self):
        self.assertEqual(matched_words("加 微 信 联 系 | hello", 87,
                                       ["加微信联系", "无关内容"], 65), ["加微信联系"])
        self.assertEqual(matched_words("Telegram 联系", 50,
                                       ["telegram联系"], 65), [])
        self.assertEqual(matched_words("加微信", 90, ["加微信"], 65), [])
        self.assertEqual(matched_words("", 90, ["加微信联系"], 65), [])

    def test_tesseract_tsv_confidence(self):
        tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
               "left\ttop\twidth\theight\tconf\ttext\n"
               "5\t1\t1\t1\t1\t1\t0\t0\t50\t12\t90\t加微信联系\n"
               "5\t1\t1\t1\t1\t2\t60\t0\t50\t12\t70\tQQ1234\n")
        with patch("app.ocr.subprocess.run",
                   return_value=FakeTesseract(tsv)) as run:
            text, confidence = extract_tesseract(b"\xff\xd8pretend-jpg")
        self.assertEqual(confidence, 80)
        self.assertIn("加微信联系", text)
        self.assertEqual(run.call_args.kwargs["timeout"], 8)

    def test_ocr_size_limit_without_running_binary(self):
        with patch("app.ocr.subprocess.run") as run:
            text, confidence = extract_tesseract(b"X" * 3_000_001)
        self.assertEqual((text, confidence), ("", 0))
        run.assert_not_called()

    def test_telegram_file_path_safety(self):
        api = TelegramAPI("TEST_TOKEN")
        api.call = lambda method, **kwargs: {"file_path": "../../secret",
                                              "file_size": 2}
        with self.assertRaises(TelegramAPIError):
            api.fetch_photo("test-id")
        api.call = lambda method, **kwargs: {
            "file_path": "photos/safe_file.jpg", "file_size": 4_000_000}
        with self.assertRaises(TelegramAPIError):
            api.fetch_photo("test-id")

    def test_download_file_bytes_bound(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b"test_jpg_bytes"

        api = TelegramAPI("TEST_TOKEN")
        api.call = lambda method, **kwargs: {
            "file_path": "photos/safe.jpg", "file_size": 14}
        with patch("app.api.urllib.request.urlopen", return_value=Response()) as fetch:
            value = api.fetch_photo("file-ref")
        self.assertEqual(value, b"test_jpg_bytes")
        req = fetch.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/file/botTEST_TOKEN/photos/safe.jpg"))

    def test_disabled_worker_never_downloads(self):
        class API:
            def fetch_photo(self, *args, **kwargs):
                raise AssertionError("OCR disabled")

        worker = OCRWorker(API(), enabled=False)
        self.assertFalse(worker.submit("file_id", {"message_id": 1}))
        self.assertEqual(list(worker.results()), [])


class OCRServiceTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegramAPI()
        self.store = Store(":memory:")
        self.store.ensure_chat(-1001, "测试群")
        self.store.add_filter(-1001, "word", "加微信联系", "加微信联系")
        self.bot = GroupBot(self.api, self.store, bot_id=999, username="MyBot")
        self.bot.ocr = OCRWorker(self.api, enabled=True,
                                 extractor=lambda data: ("加微信联系", 92.0))
        self.api.fetch_photo = lambda file_id, max_bytes=3_000_000: b"jpeg"

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def msg(self, uid, msg_id):
        return {"chat": {"id": -1001, "title": "测试群", "type": "supergroup"},
                "from": {"id": uid, "first_name": "测试人"},
                "message_id": msg_id,
                "photo": [{"file_id": "photo-id", "file_unique_id": str(msg_id),
                           "file_size": 1024}]}

    def drain(self):
        self.bot.ocr._executor.shutdown(wait=True)
        self.bot.drain_ocr()

    def test_review_mode_never_deletes(self):
        self.store.set(-1001, "ocr_mode", "review")
        self.store.set(-1001, "log_chat_id", -200)
        self.bot.handle_update({"message": self.msg(2, 101)})
        self.drain()
        self.assertFalse(any(method == "deleteMessage" for method, _ in self.api.calls))
        self.assertEqual(self.store.stats(-1001).get("ocr_hits"), 1)
        self.assertTrue(any(method == "sendMessage" and p.get("chat_id") == -200
                            for method, p in self.api.calls))
        self.assertFalse(any(method == "sendMessage" and
                            "加微信联系" in p.get("text", "") and p.get("chat_id") == -1001
                            for method, p in self.api.calls))

    def test_explicit_delete_mode_and_admin_exemption(self):
        self.store.set(-1001, "ocr_mode", "delete")
        self.bot.handle_update({"message": self.msg(1, 110)})
        self.bot.handle_update({"message": self.msg(2, 111)})
        self.drain()
        targets = [p["message_id"] for method, p in self.api.calls
                   if method == "deleteMessage"]
        self.assertEqual(targets, [111])
        self.assertEqual(self.store.stats(-1001).get("ocr_deleted"), 1)

    def test_stale_mode_change_skips_delete(self):
        self.store.set(-1001, "ocr_mode", "delete")
        self.bot.handle_update({"message": self.msg(2, 120)})
        self.store.set(-1001, "ocr_mode", "off")
        self.drain()
        self.assertFalse(any(method == "deleteMessage" for method, _ in self.api.calls))

    def test_photo_edit_invalidates_pending_ocr(self):
        self.store.set(-1001, "ocr_mode", "delete")
        original = self.msg(2, 125)
        self.bot.handle_update({"message": original})
        edited = dict(original)
        edited["photo"] = [{"file_id": "changed", "file_unique_id": "new"}]
        self.bot.handle_update({"edited_message": edited})
        self.drain()
        self.assertFalse(any(
            method == "deleteMessage" for method, _ in self.api.calls))

    def test_ocr_off_never_enqueues_photo(self):
        self.bot.handle_update({"message": self.msg(2, 130)})
        self.drain()
        self.assertEqual(self.store.stats(-1001).get("ocr_hits"), None)
        self.assertFalse(any(method == "deleteMessage" for method, _ in self.api.calls))

    def test_ocr_mode_command_checks_engine(self):
        disabled = OCRWorker(self.api, enabled=False)
        self.bot.ocr.shutdown()
        self.bot.ocr = disabled
        group = {"message_id": 140, "chat": {"id": -1001, "type": "supergroup",
                                               "title": "测试群"},
                 "from": {"id": 1}, "text": "/ocr delete"}
        self.bot.handle_update({"message": group})
        self.assertEqual(self.store.settings(-1001)["ocr_mode"], "off")


if __name__ == "__main__":
    unittest.main()
