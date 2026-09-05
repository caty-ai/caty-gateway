import base64
import io
import json
import os
import sys
import unittest
import urllib.error


from caty_gateway import vision_describer


class FakeResponse:
    def __init__(self, payload=b""):
        self.payload = payload

    def read(self, size=None):
        if size is None:
            return self.payload
        return self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class VisionDescriberTests(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CATY_VISION_MODEL")}
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("CATY_VISION_MODEL", None)

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_request_body_and_headers(self):
        captured = {}

        def fake_urlopen(request, timeout=60):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(json.dumps({"content": [{"type": "text", "text": "blue hair, glasses"}]}).encode("utf-8"))

        client = vision_describer.VisionDescriber(api_key="secret-key", model="claude-test", urlopen=fake_urlopen)
        result = client.describe(b"\x89PNG\r\n\x1a\navatar")

        self.assertEqual(result, "blue hair, glasses")
        self.assertEqual(captured["url"], vision_describer.ANTHROPIC_MESSAGES_URL)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["headers"]["x-api-key"], "secret-key")
        self.assertEqual(captured["headers"]["anthropic-version"], vision_describer.ANTHROPIC_VERSION)
        self.assertEqual(captured["headers"]["content-type"], "application/json")
        body = captured["body"]
        self.assertEqual(body["model"], "claude-test")
        self.assertEqual(body["max_tokens"], 300)
        content = body["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertEqual(content[0]["source"]["data"], base64.b64encode(b"\x89PNG\r\n\x1a\navatar").decode("ascii"))
        self.assertEqual(content[1]["type"], "text")
        self.assertIn("compact, comma-separated English identity description", content[1]["text"])

    def test_post_processing_and_truncation(self):
        long_text = "x" * (vision_describer.IDENTITY_DESCRIPTION_LIMIT + 50)

        def fake_urlopen(request, timeout=60):
            payload = {"content": [{"type": "text", "text": f"  “black hair,\n\n  red scarf”  {long_text}"}]}
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        client = vision_describer.VisionDescriber(api_key="secret-key", urlopen=fake_urlopen)
        result = client.describe(b"image")

        self.assertTrue(result.startswith("black hair, red scarf"))
        self.assertNotIn("\n", result)
        self.assertEqual(len(result), vision_describer.IDENTITY_DESCRIPTION_LIMIT)

    def test_braces_are_neutralized_for_downstream_format(self):
        def fake_urlopen(request, timeout=60):
            payload = {"content": [{"type": "text", "text": "black hair, {weird} scarf}"}]}
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        client = vision_describer.VisionDescriber(api_key="secret-key", urlopen=fake_urlopen)
        result = client.describe(b"image")

        self.assertEqual(result, "black hair, (weird) scarf)")
        # Must survive the slot-prompt templating used in face_core.
        self.assertEqual(
            "prompt {c}".format(c=result),
            "prompt black hair, (weird) scarf)",
        )

    def test_non_utf8_body_raises_describer_error(self):
        def fake_urlopen(request, timeout=60):
            return FakeResponse(b"\xff\xfe\x00 broken bytes \x80")

        client = vision_describer.VisionDescriber(api_key="secret-key", urlopen=fake_urlopen, retry_attempts=1)
        with self.assertRaises(vision_describer.VisionDescriberError) as cm:
            client.describe(b"image")
        self.assertIn("could not be read", str(cm.exception))

    def test_oversized_response_raises_describer_error(self):
        def fake_urlopen(request, timeout=60):
            return FakeResponse(b"x" * (vision_describer.MAX_RESPONSE_BYTES + 10))

        client = vision_describer.VisionDescriber(api_key="secret-key", urlopen=fake_urlopen, retry_attempts=1)
        with self.assertRaises(vision_describer.VisionDescriberError) as cm:
            client.describe(b"image")
        self.assertIn("size limit", str(cm.exception))

    def test_missing_api_key_raises_disabled(self):
        with self.assertRaisesRegex(
            vision_describer.VisionDescriberDisabled,
            "ANTHROPIC_API_KEY is required for avatar vision description",
        ):
            vision_describer.VisionDescriber(api_key=None)

    def test_http_error_raises_without_api_key(self):
        def fake_urlopen(request, timeout=60):
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "bad request",
                hdrs={},
                fp=io.BytesIO(b"fake-secret-key"),
            )

        client = vision_describer.VisionDescriber(api_key="fake-secret-key", urlopen=fake_urlopen, retry_attempts=1)
        with self.assertRaises(vision_describer.VisionDescriberError) as cm:
            client.describe(b"image")

        self.assertIn("HTTP 400", str(cm.exception))
        self.assertNotIn("fake-secret-key", str(cm.exception))

    def test_retries_429_once_then_succeeds(self):
        calls = []
        sleeps = []

        def fake_urlopen(request, timeout=60):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    hdrs={},
                    fp=io.BytesIO(b"rate limited"),
                )
            return FakeResponse(json.dumps({"content": [{"type": "text", "text": "green bob, hoodie"}]}).encode("utf-8"))

        client = vision_describer.VisionDescriber(
            api_key="secret-key",
            urlopen=fake_urlopen,
            sleep=lambda seconds: sleeps.append(seconds),
        )

        self.assertEqual(client.describe(b"image"), "green bob, hoodie")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [1.0])


if __name__ == "__main__":
    unittest.main()
