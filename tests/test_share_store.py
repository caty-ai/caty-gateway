import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock


from caty_gateway import share_store


class ShareStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-share-store-")
        self.root = os.path.join(self.tmp.name, "spool")
        self.store = share_store.ShareStore(
            self.root,
            ttl_seconds=900,
            sweep_interval_seconds=0,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def put(self, **changes):
        values = {
            "session_id": "session-a",
            "kind": "file",
            "filename": "notes.txt",
            "mime": "text/plain",
            "data": b"hello share",
        }
        values.update(changes)
        return self.store.put(**values)

    def spool_entries(self):
        return [name for name in os.listdir(self.root) if name != "claimed"]

    def test_put_consume_roundtrip_and_single_use(self):
        result = self.put()
        share_id = result["share_id"]

        consumed = self.store.consume(share_id, "session-a")

        self.assertEqual(consumed["data"], b"hello share")
        self.assertEqual(consumed["filename"], "notes.txt")
        self.assertEqual(consumed["kind"], "file")
        self.assertFalse(os.path.exists(os.path.join(self.root, share_id)))
        self.assertFalse(os.path.exists(os.path.join(self.root, share_id + ".json")))
        with self.assertRaises(share_store.ShareNotFound):
            self.store.consume(share_id, "session-a")

    def test_expired_share_is_deleted_on_access(self):
        result = self.put()
        share_id = result["share_id"]
        self.store._metadata[share_id]["created_at"] = time.time() - 901

        with self.assertRaises(share_store.ShareExpired):
            self.store.consume(share_id, "session-a")

        self.assertFalse(os.path.exists(os.path.join(self.root, share_id)))
        self.assertFalse(os.path.exists(os.path.join(self.root, share_id + ".json")))

    def test_sweep_removes_expired_stale_parts_and_hard_cap_files(self):
        expired = self.put(data=b"expired")["share_id"]
        self.store._metadata[expired]["created_at"] = time.time() - 901

        stale_part = os.path.join(self.root, "orphan.part")
        with open(stale_part, "wb") as stream:
            stream.write(b"partial")

        old_id = "a" * 32
        old_data = os.path.join(self.root, old_id)
        old_sidecar = old_data + ".json"
        with open(old_data, "wb") as stream:
            stream.write(b"orphan")
        with open(old_sidecar, "w", encoding="utf-8") as stream:
            json.dump({}, stream)
        old_unknown = os.path.join(self.root, "unknown-orphan")
        with open(old_unknown, "wb") as stream:
            stream.write(b"orphan")
        old = time.time() - 86401
        os.utime(old_data, (old, old))
        os.utime(old_sidecar, (old, old))
        os.utime(old_unknown, (old, old))

        self.store.sweep()

        with self.assertRaises(share_store.ShareExpired):
            self.store.consume(expired, "session-a")
        for path in (
            os.path.join(self.root, expired),
            os.path.join(self.root, expired + ".json"),
            stale_part,
            old_data,
            old_sidecar,
            old_unknown,
        ):
            self.assertFalse(os.path.exists(path), path)

    def test_idempotency_reuses_same_content_and_conflicts_on_change(self):
        first = self.put(idempotency_key="request-1")
        second = self.put(idempotency_key="request-1")
        self.assertEqual(first, second)

        with self.assertRaises(share_store.IdempotencyConflict):
            self.put(data=b"different", idempotency_key="request-1")

        published = [
            name for name in self.spool_entries()
            if not name.endswith(".json")
        ]
        self.assertEqual(published, [first["share_id"]])

    def test_idempotency_survives_store_restart(self):
        first = self.put(idempotency_key="restart-key")
        restarted = share_store.ShareStore(
            self.root,
            ttl_seconds=900,
            sweep_interval_seconds=0,
        )
        self.addCleanup(restarted.close)

        second = restarted.put(
            "session-a", "file", "notes.txt", "text/plain", b"hello share",
            idempotency_key="restart-key",
        )

        self.assertEqual(first, second)

    def test_session_mismatch_does_not_consume(self):
        share_id = self.put()["share_id"]

        with self.assertRaises(share_store.SessionMismatch):
            self.store.consume(share_id, "session-b")

        self.assertTrue(os.path.exists(os.path.join(self.root, share_id)))
        self.assertEqual(
            self.store.consume(share_id, "session-a")["data"], b"hello share"
        )

    def test_restart_after_consume_does_not_resurrect_share(self):
        share_id = self.put()["share_id"]

        self.store.consume(share_id, "session-a")

        restarted = share_store.ShareStore(
            self.root,
            ttl_seconds=900,
            sweep_interval_seconds=0,
        )
        self.addCleanup(restarted.close)
        with self.assertRaises(share_store.ShareNotFound):
            restarted.consume(share_id, "session-a")

    def test_idempotency_replay_after_consume_mints_new_share_id(self):
        first = self.put(idempotency_key="consumed-key")
        self.store.consume(first["share_id"], "session-a")

        # Consumed shares are intentionally single-use; replaying the same key
        # stages a fresh share instead of resurrecting the consumed one.
        second = self.put(idempotency_key="consumed-key")

        self.assertNotEqual(first["share_id"], second["share_id"])

    def test_live_share_quota_releases_slots_after_consume_and_expiry(self):
        share_ids = [
            self.put(filename=f"notes-{index}.txt")["share_id"]
            for index in range(4)
        ]

        with self.assertRaises(share_store.ShareQuotaExceeded):
            self.put(filename="notes-4.txt")

        self.store.consume(share_ids[0], "session-a")
        after_consume = self.put(filename="after-consume.txt")
        self.assertRegex(after_consume["share_id"], r"^[0-9a-f]{32}$")

        self.store._metadata[share_ids[1]]["created_at"] = time.time() - 901
        after_expiry = self.put(filename="after-expiry.txt")
        self.assertRegex(after_expiry["share_id"], r"^[0-9a-f]{32}$")

    def test_share_id_is_validated_before_filesystem_lookup(self):
        for invalid in ("", "../secret", "/tmp/secret", "A" * 32, "a" * 31, "a" * 33, "a" * 31 + "\x00"):
            with self.subTest(invalid=invalid), self.assertRaises(
                share_store.InvalidShareId
            ):
                self.store.consume(invalid, "session-a")

    def test_root_and_published_files_have_private_modes(self):
        share_id = self.put()["share_id"]

        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.root, share_id)).st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.root, share_id + ".json")).st_mode),
            0o600,
        )

    def test_staging_validation_failure_never_publishes_part_or_final(self):
        with mock.patch.object(
            self.store,
            "_validate_staged",
            side_effect=share_store.ShareStagingError("invalid"),
        ):
            with self.assertRaises(share_store.ShareStagingError):
                self.put()

        self.assertEqual(self.spool_entries(), [])

    def test_rejects_unsafe_metadata_before_writing(self):
        invalid_changes = (
            {"filename": "../secret.txt"},
            {"filename": "folder/secret.txt"},
            {"filename": "folder\\secret.txt"},
            {"filename": "bad\x00name.txt"},
            {"session_id": "../session"},
            {"session_id": "/absolute"},
            {"mime": "bad\x00mime"},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.put(**changes)
        self.assertEqual(self.spool_entries(), [])

    def test_take_binary_claims_private_file_and_is_single_use(self):
        png = b"\x89PNG\r\n\x1a\nprivate-image"
        share_id = self.put(
            kind="file", filename="photo.bin", mime="text/plain", data=png
        )["share_id"]

        claimed = self.store.take(share_id, "session-a")

        self.assertIsInstance(claimed, share_store.ClaimedFile)
        self.assertEqual(claimed.sniffed_mime, "image/png")
        self.assertEqual(claimed.declared_kind, "file")
        self.assertEqual(claimed.size, len(png))
        self.assertEqual(claimed.filename, "photo.bin")
        self.assertEqual(os.path.dirname(claimed.path), self.store.claimed_dir)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.claimed_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(claimed.path).st_mode), 0o600)
        with open(claimed.path, "rb") as stream:
            self.assertEqual(stream.read(), png)
        with self.assertRaises(share_store.ShareNotFound):
            self.store.take(share_id, "session-a")

    def test_stage_claimed_bytes_png_writes_private_claim(self):
        png = b"\x89PNG\r\n\x1a\nscreen-png"

        claimed = self.store.stage_claimed_bytes(
            png, "screen.png", "image"
        )

        self.assertIsInstance(claimed, share_store.ClaimedFile)
        self.assertEqual(claimed.sniffed_mime, "image/png")
        self.assertEqual(claimed.filename, "screen.png")
        self.assertEqual(claimed.declared_kind, "image")
        self.assertEqual(stat.S_IMODE(os.stat(claimed.path).st_mode), 0o600)
        with open(claimed.path, "rb") as stream:
            self.assertEqual(stream.read(), png)

    def test_stage_claimed_bytes_jpeg_uses_sniffed_mime(self):
        jpeg = b"\xff\xd8\xffscreen-jpeg"

        claimed = self.store.stage_claimed_bytes(
            jpeg, "screen.jpg", "image"
        )

        self.assertIsInstance(claimed, share_store.ClaimedFile)
        self.assertEqual(claimed.sniffed_mime, "image/jpeg")
        self.assertEqual(stat.S_IMODE(os.stat(claimed.path).st_mode), 0o600)

    def test_stage_claimed_bytes_rejects_garbage_without_writing(self):
        rejected = self.store.stage_claimed_bytes(
            b"not an image", "screen.jpg", "image"
        )

        self.assertEqual(
            rejected,
            share_store.Rejected(
                reason="mime-rejected",
                filename="screen.jpg",
                size=len(b"not an image"),
                declared_kind="image",
            ),
        )
        self.assertEqual(os.listdir(self.store.claimed_dir), [])

    def test_stage_claimed_bytes_sweeps_expired_spool_entries(self):
        expired = self.put(data=b"expired")["share_id"]
        self.store._metadata[expired]["created_at"] = time.time() - 901

        claimed = self.store.stage_claimed_bytes(
            b"\x89PNG\r\n\x1a\nscreen", "screen.png", "image"
        )

        self.assertIsInstance(claimed, share_store.ClaimedFile)
        self.assertFalse(os.path.exists(os.path.join(self.root, expired)))
        self.assertFalse(os.path.exists(os.path.join(self.root, expired + ".json")))

    def test_take_text_uses_one_lock_acquisition_and_is_single_use(self):
        share_id = self.put(data=b"plain text")["share_id"]
        original_lock = self.store._lock

        class CountingLock:
            count = 0

            def __enter__(inner_self):
                inner_self.count += 1
                return original_lock.__enter__()

            def __exit__(inner_self, *args):
                return original_lock.__exit__(*args)

        counting_lock = CountingLock()
        self.store._lock = counting_lock
        taken = self.store.take(share_id, "session-a")

        self.assertIsInstance(taken, share_store.TextBytes)
        self.assertEqual(taken.data, b"plain text")
        self.assertEqual(counting_lock.count, 1)
        with self.assertRaises(share_store.ShareNotFound):
            self.store.take(share_id, "session-a")

    def test_take_rejected_image_is_single_use(self):
        share_id = self.put(
            kind="image", filename="fake.png", mime="image/png",
            data=b"not really an image",
        )["share_id"]

        rejected = self.store.take(share_id, "session-a")

        self.assertEqual(
            rejected,
            share_store.Rejected(
                reason="mime-rejected", filename="fake.png",
                size=len(b"not really an image"), declared_kind="image",
            ),
        )
        with self.assertRaises(share_store.ShareNotFound):
            self.store.take(share_id, "session-a")

    def test_take_rename_then_removal_failure_compensates_claim(self):
        share_id = self.put(
            kind="image", filename="photo.png", mime="image/png",
            data=b"\x89PNG\r\n\x1a\nbytes",
        )["share_id"]

        with mock.patch.object(
            self.store, "_remove_locked", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(PermissionError):
                self.store.take(share_id, "session-a")

        self.assertEqual(os.listdir(self.store.claimed_dir), [])
        with self.assertRaises(share_store.ShareNotFound):
            self.store.take(share_id, "session-a")

    def test_take_text_and_rejected_commit_logical_removal_before_unlink_error(self):
        cases = (
            ("file", b"plain text"),
            ("image", b"not really an image"),
        )
        for kind, data in cases:
            with self.subTest(kind=kind):
                share_id = self.put(
                    kind=kind, filename="upload.bin",
                    mime="application/octet-stream", data=data,
                    idempotency_key=f"failure-{kind}",
                )["share_id"]
                data_path = os.path.join(self.root, share_id)
                original_unlink = os.unlink

                def fail_data_unlink(path):
                    if path == data_path:
                        raise PermissionError("denied")
                    return original_unlink(path)

                with mock.patch.object(os, "unlink", side_effect=fail_data_unlink):
                    with self.assertRaises(PermissionError):
                        self.store.take(share_id, "session-a")

                self.assertNotIn(share_id, self.store._metadata)
                self.assertNotIn(
                    ("session-a", f"failure-{kind}"), self.store._idempotency
                )
                with self.assertRaises(share_store.ShareNotFound):
                    self.store.take(share_id, "session-a")

    def test_cleanup_claimed_orphans_removes_regular_files_without_following_symlinks(self):
        regular = os.path.join(self.store.claimed_dir, "orphan")
        target = os.path.join(self.tmp.name, "outside")
        link = os.path.join(self.store.claimed_dir, "link")
        with open(regular, "wb") as stream:
            stream.write(b"orphan")
        with open(target, "wb") as stream:
            stream.write(b"outside")
        os.symlink(target, link)

        self.store.cleanup_claimed_orphans()

        self.assertFalse(os.path.exists(regular))
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.exists(target))

    def test_default_root_honors_env_and_xdg_state_convention(self):
        with mock.patch.dict(
            os.environ,
            {"CATY_SHARE_DIR": self.tmp.name + "/custom"},
            clear=False,
        ):
            self.assertEqual(
                share_store.default_share_root(),
                os.path.abspath(self.tmp.name + "/custom"),
            )
        with mock.patch.dict(
            os.environ,
            {
                "CATY_SHARE_DIR": "",
                "XDG_STATE_HOME": self.tmp.name + "/state",
                "CATY_ID": "member-a-1",
            },
            clear=False,
        ):
            self.assertEqual(
                share_store.default_share_root(),
                self.tmp.name + "/state/caty-gateway/share-spool/member-a-1",
            )
        for invalid_member_id in ("", ".", "..", "../bad", "bad!member"):
            with self.subTest(member_id=invalid_member_id), mock.patch.dict(
                os.environ,
                {
                    "CATY_SHARE_DIR": "",
                    "XDG_STATE_HOME": self.tmp.name + "/state",
                    "CATY_ID": invalid_member_id,
                },
                clear=False,
            ):
                self.assertEqual(
                    share_store.default_share_root(),
                    self.tmp.name + "/state/caty-gateway/share-spool/default",
                )

    def test_periodic_sweeper_removes_expired_share_without_access(self):
        store = share_store.ShareStore(
            os.path.join(self.tmp.name, "sweeper-spool"),
            ttl_seconds=900,
            sweep_interval_seconds=0.01,
        )
        try:
            share_id = store.put(
                "session-a",
                "file",
                "notes.txt",
                "text/plain",
                b"hello share",
            )["share_id"]
            store._metadata[share_id]["created_at"] = time.time() - 901

            deadline = time.time() + 1
            data_path = os.path.join(store.root_dir, share_id)
            while os.path.exists(data_path) and time.time() < deadline:
                time.sleep(0.02)

            self.assertFalse(os.path.exists(data_path))
            self.assertFalse(os.path.exists(data_path + ".json"))
        finally:
            store.close()

    def test_rejected_put_on_restart_still_starts_periodic_sweeper(self):
        seeded = share_store.ShareStore(
            os.path.join(self.tmp.name, "restart-sweeper-spool"),
            ttl_seconds=900,
            sweep_interval_seconds=0,
        )
        try:
            share_ids = [
                seeded.put(
                    "session-a",
                    "file",
                    f"seed-{index}.txt",
                    "text/plain",
                    b"hello share",
                )["share_id"]
                for index in range(4)
            ]
        finally:
            seeded.close()

        restarted = share_store.ShareStore(
            os.path.join(self.tmp.name, "restart-sweeper-spool"),
            ttl_seconds=900,
            sweep_interval_seconds=0.01,
        )
        try:
            with self.assertRaises(share_store.ShareQuotaExceeded):
                restarted.put(
                    "session-a",
                    "file",
                    "overflow.txt",
                    "text/plain",
                    b"hello share",
                )

            restarted._metadata[share_ids[0]]["created_at"] = time.time() - 901
            deadline = time.time() + 1
            data_path = os.path.join(restarted.root_dir, share_ids[0])
            while os.path.exists(data_path) and time.time() < deadline:
                time.sleep(0.02)

            self.assertFalse(os.path.exists(data_path))
            result = restarted.put(
                "session-a",
                "file",
                "after-sweep.txt",
                "text/plain",
                b"hello share",
            )
            self.assertRegex(result["share_id"], r"^[0-9a-f]{32}$")
        finally:
            restarted.close()


if __name__ == "__main__":
    unittest.main()
