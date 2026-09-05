import os
import sys
import unittest
import urllib.parse
from io import BytesIO
from unittest import mock


from caty_gateway import caty_gateway as cg


class NonClosingBytesIO(BytesIO):
    def close(self):
        pass


class MemorySocket:
    def __init__(self, request_bytes):
        self.input = BytesIO(request_bytes)
        self.output = NonClosingBytesIO()

    def makefile(self, mode, *args, **kwargs):
        return self.input if "r" in mode else self.output

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, timeout):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


class MemoryServer:
    server_name = "127.0.0.1"
    server_port = 0


def raw_request(path, handler_class=cg.Handler):
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    sock = MemorySocket(request)
    handler_class(sock, ("127.0.0.1", 0), MemoryServer())
    return sock.output.getvalue()


def split_response(raw):
    head, marker, body = raw.partition(b"\r\n\r\n")
    if not marker:
        raise AssertionError(f"response has no header terminator: {raw!r}")
    return head, body


def header_values(head, name):
    prefix = name.lower().encode("ascii") + b":"
    return [
        line.split(b":", 1)[1].lstrip()
        for line in head.split(b"\r\n")[1:]
        if line.lower().startswith(prefix)
    ]


class ResponseHeaderSanitizationTest(unittest.TestCase):
    def setUp(self):
        self.original_jobs = dict(cg.JOBS)
        cg.JOBS.clear()
        self.auth_patch = mock.patch.object(cg, "CATY_TOKEN", "")
        self.env_patch = mock.patch.dict(os.environ, {"CATY_REQUIRE_AUTH": "0"})
        self.auth_patch.start()
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.auth_patch.stop()
        cg.JOBS.clear()
        cg.JOBS.update(self.original_jobs)

    def assert_safe_header_value(self, head, name, expected):
        values = header_values(head, name)
        self.assertEqual(len(values), 1)
        self.assertNotIn(b"\r", values[0])
        self.assertNotIn(b"\n", values[0])
        self.assertEqual(values[0], expected)
        return values[0]

    def test_reply_cr_lf_cannot_split_headers_or_leak_into_audio_body(self):
        audio = b"\x00\x01audio-body"
        replies = {
            "cr": "100%: 前\rX-Injected: cr後",
            "lf": "100%: 前\nX-Injected: lf後",
            "crlf": "100%: 前\r\nX-Injected: crlf後\r\n\r\nfake-body",
        }

        for label, reply in replies.items():
            with self.subTest(label=label):
                job = cg.Job("transcript")
                job.reply = reply
                job.chunks = [audio]
                job.done = True
                cg.JOBS["header-test"] = job

                head, body = split_response(raw_request("/reply/header-test"))

                self.assertEqual(body, audio)
                self.assertNotIn(b"\r\nX-Injected:", head)
                self.assertEqual(header_values(head, "X-Injected"), [])
                self.assert_safe_header_value(
                    head,
                    "X-Reply",
                    reply.replace("\r", " ").replace("\n", " ").encode("utf-8"),
                )
                self.assertEqual(
                    header_values(head, "Content-Length"),
                    [str(len(audio)).encode("ascii")],
                )

                encoded_value = self.assert_safe_header_value(
                    head,
                    "X-Reply-Enc",
                    urllib.parse.quote(reply, safe="").encode("ascii"),
                )
                self.assertEqual(
                    urllib.parse.unquote(encoded_value.decode("ascii")),
                    reply,
                )

    def test_shared_send_path_sanitizes_transcript_and_preserves_japanese(self):
        transcript = "こんにちは\r\nX-Injected: yes\n続き"
        response_body = b'{"ok":true}'

        class TranscriptHandler(cg.Handler):
            def do_GET(self):
                self._send(
                    200,
                    response_body,
                    extra={"X-Transcript": transcript},
                )

        head, body = split_response(raw_request("/", TranscriptHandler))

        self.assertEqual(body, response_body)
        self.assertNotIn(b"\r\nX-Injected:", head)
        self.assertEqual(header_values(head, "X-Injected"), [])
        self.assert_safe_header_value(
            head,
            "X-Transcript",
            transcript.replace("\r", " ").replace("\n", " ").encode("utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
