import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


from caty_gateway import avatar_engine
from caty_gateway import caty_config
from caty_gateway import caty_gateway
from caty_gateway import setup_redaction


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            data, self.data = self.data, b""
            return data
        data, self.data = self.data[:size], self.data[size:]
        return data


class AvatarCloudSessionContractTests(unittest.TestCase):
    def test_c3_c9_cloud_session_wires_exact_facade_urls_payload_and_bearer_headers(self):
        # Dotted fixture value: not a real credential; the dot keeps the
        # pre-commit secret guard's 16-char run heuristic from matching.
        token = "cloud.session-token-abcdefghijklmnop.0123456789"
        clients = caty_gateway._avatar_pass_clients(
            "stylize",
            {"base_url": caty_config.CATY_CLOUD_ORIGIN, "token": token},
        )
        requests = []

        def poyo_urlopen(request, timeout=60):
            requests.append(request)
            return FakeResponse(json.dumps({"data": {"task_id": "task-1"}}).encode())

        clients.poyo.urlopen = poyo_urlopen
        task_id = clients.poyo.submit(
            "prompt text", ["https://assets.renoise.ai/identity.png", "https://assets.renoise.ai/style.png"]
        )

        def renoise_urlopen(request, timeout=120):
            requests.append(request)
            return FakeResponse(json.dumps({"downloadUrl": "https://assets.renoise.ai/upload.png"}).encode())

        clients.renoise.urlopen = renoise_urlopen
        clients.renoise.upload_bytes(b"\x89PNGdata", "avatar.png")

        self.assertEqual(task_id, "task-1")
        self.assertEqual(clients.kind, "stylize")
        self.assertEqual(clients.source, "cloud")
        self.assertEqual(
            requests[0].full_url,
            "https://api.caty.talk/v1/avatar/api/generate/submit",
        )
        self.assertEqual(dict(requests[0].header_items())["Authorization"], f"Bearer {token}")
        expected_payload = {
            "model": "nano-banana-pro-edit",
            "input": {
                "prompt": "prompt text",
                "image_urls": [
                    "https://assets.renoise.ai/identity.png",
                    "https://assets.renoise.ai/style.png",
                ],
                "size": "1:1",
                "resolution": "2K",
            },
        }
        self.assertEqual(requests[0].data, json.dumps(expected_payload).encode("utf-8"))
        self.assertEqual(
            requests[1].full_url,
            "https://api.caty.talk/v1/avatar/materials/upload",
        )
        renoise_headers = dict(requests[1].header_items())
        self.assertEqual(renoise_headers["Authorization"], f"Bearer {token}")
        self.assertNotIn("X-api-key", renoise_headers)

    def test_c9_byok_request_shape_is_unchanged_without_cloud_session(self):
        env = {
            "POYO_API_KEY": "byok-poyo",
            "POYO_BASE": "https://api.poyo.ai",
            "RENOISE_API_KEY": "byok-renoise",
            "RENOISE_AUTH_TOKEN": "",
            "RENOISE_BASE_URL": "https://www.renoise.ai/api/public/v1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            clients = caty_gateway._avatar_pass_clients("stylize")

        captured = []
        clients.poyo.urlopen = lambda request, timeout=60: (
            captured.append(request)
            or FakeResponse(json.dumps({"data": {"task_id": "byok-task"}}).encode())
        )
        clients.poyo.submit("same prompt", ["https://assets.renoise.ai/input.png"])
        clients.renoise.urlopen = lambda request, timeout=120: (
            captured.append(request)
            or FakeResponse(json.dumps({"downloadUrl": "https://assets.renoise.ai/file.png"}).encode())
        )
        clients.renoise.upload_bytes(b"\x89PNGdata", "input.png")

        self.assertEqual(clients.source, "byok")
        self.assertEqual(captured[0].full_url, "https://api.poyo.ai/api/generate/submit")
        self.assertEqual(dict(captured[0].header_items())["Authorization"], "Bearer byok-poyo")
        self.assertEqual(
            captured[0].data,
            json.dumps(
                {
                    "model": "nano-banana-pro-edit",
                    "input": {
                        "prompt": "same prompt",
                        "image_urls": ["https://assets.renoise.ai/input.png"],
                        "size": "1:1",
                        "resolution": "2K",
                    },
                }
            ).encode("utf-8"),
        )
        self.assertEqual(
            captured[1].full_url,
            "https://www.renoise.ai/api/public/v1/materials/upload",
        )
        byok_headers = dict(captured[1].header_items())
        self.assertEqual(byok_headers["X-api-key"], "byok-renoise")
        self.assertNotIn("Authorization", byok_headers)

    def test_c9_cloud_precedence_over_env_and_neither_returns_disabled(self):
        with mock.patch.dict(
            os.environ,
            {"POYO_API_KEY": "env-poyo", "RENOISE_API_KEY": "env-renoise"},
            clear=False,
        ):
            clients = caty_gateway._avatar_pass_clients(
                "set",
                {"base_url": caty_config.CATY_CLOUD_ORIGIN, "token": "cloud-wins"},
            )
        self.assertEqual(clients.source, "cloud")
        self.assertEqual(clients.poyo.api_key, "cloud-wins")
        self.assertEqual(clients.renoise.auth_token, "cloud-wins")

        with mock.patch.dict(
            os.environ,
            {"POYO_API_KEY": "", "RENOISE_API_KEY": "", "RENOISE_AUTH_TOKEN": ""},
            clear=False,
        ):
            with self.assertRaises(avatar_engine.AvatarEngineDisabled):
                caty_gateway._avatar_pass_clients("stylize")

    def test_c14_origin_pin_matrix(self):
        for accepted in ("https://api.caty.talk", "https://api.caty.talk/", "HTTPS://API.CATY.TALK"):
            with self.subTest(accepted=accepted):
                self.assertEqual(
                    caty_config.normalize_caty_cloud_origin(accepted),
                    caty_config.CATY_CLOUD_ORIGIN,
                )

        rejected = (
            "http://api.caty.talk",
            "https://api.example.invalid",
            "https://api.caty.talk?token=x",
            "https://api.caty.talk#fragment",
            "https://api.caty.talk.evil.example",
            "https://api.caty.talk:443",
            "https://api.caty.talk:8443",
            "https://api.caty.talk.",
            "https://api.caty.talk/v1/avatar",
            " https://api.caty.talk",
            "https://api.caty.talk\n",
        )
        for value in rejected:
            with self.subTest(rejected=value), self.assertRaises(caty_config.InvalidConfig):
                caty_config.normalize_caty_cloud_origin(value)

    def test_f5_download_rejects_non_https_and_non_vendor_hosts_before_transport(self):
        client = avatar_engine.PoyoClient(
            api_key="secret",
            urlopen=lambda request, timeout=120: self.fail("transport must not be called"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.png"
            for value in (
                "file:///etc/passwd",
                "http://files.poyo.ai/result.png",
                "https://evilpoyo.ai/result.png",
                "https://poyo.ai.evil.example/result.png",
                "https://files.example.invalid/result.png",
                "https://files.poyo.ai:8443/result.png",
            ):
                with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "allow-listed"):
                    client.download(value, output)
                self.assertFalse(output.exists())

    def test_residual_vendor_allowlist_is_runtime_configurable_and_label_bounded(self):
        seen = []
        client = avatar_engine.PoyoClient(
            api_key="secret",
            urlopen=lambda request, timeout=120: (
                seen.append(request.full_url) or FakeResponse(b"png")
            ),
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"CATY_AVATAR_VENDOR_HOST_ALLOWLIST": "vendor-cdn.example"},
            clear=False,
        ):
            output = Path(tmp) / "result.png"
            client.download("https://images.vendor-cdn.example/result.png", output)
            self.assertEqual(output.read_bytes(), b"png")
            with self.assertRaisesRegex(RuntimeError, "allow-listed"):
                client.download("https://evilvendor-cdn.example/result.png", output)

        self.assertEqual(seen, ["https://images.vendor-cdn.example/result.png"])

    def test_f6_http_424_with_retry_after_raises_without_retry_spin(self):
        calls = []

        def urlopen(request, timeout=60):
            calls.append(request)
            raise urllib.error.HTTPError(
                request.full_url,
                424,
                "vendor cooldown",
                hdrs={"Retry-After": "30"},
                fp=io.BytesIO(b"cooldown"),
            )

        client = avatar_engine.PoyoClient(
            api_key="secret",
            base_url="https://api.caty.talk/v1/avatar",
            urlopen=urlopen,
            sleep=lambda seconds: self.fail("424 must not sleep"),
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            client.submit("prompt", ["https://assets.renoise.ai/input.png"])
        self.assertEqual(raised.exception.code, 424)
        self.assertEqual(len(calls), 1)

    def test_c9_cloud_session_token_is_redacted_from_setup_and_gateway_logs(self):
        # Dotted fixture value: not a real credential (see note above).
        token = "cloud.token-canary-abcdefghijklmnop.0123456789"
        line = repr(
            {
                "cloud_session": {
                    "base_url": caty_config.CATY_CLOUD_ORIGIN,
                    "token": token,
                }
            }
        )
        self.assertNotIn(token, setup_redaction.redact(line))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            caty_gateway.log(line)
        self.assertNotIn(token, output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
