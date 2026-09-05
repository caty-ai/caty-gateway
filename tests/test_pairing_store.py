import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock



from caty_gateway import pairing_store as ps


LOCK_HELPER = textwrap.dedent(
    """
    import fcntl
    import json
    import os
    import sys
    import time

    mode = sys.argv[1]
    lock_path = sys.argv[2]
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW

    if mode == "hold":
        fd = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("HELD", flush=True)
            sys.stdin.readline()
            print("RELEASED", flush=True)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
    elif mode == "probe":
        fd = os.open(lock_path, flags, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("BLOCKED", flush=True)
            else:
                print("ACQUIRED", flush=True)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    elif mode == "critical":
        root_dir, member_id, counter_path, log_path, iterations = sys.argv[3:8]
        from caty_gateway import pairing_store as ps

        store = ps.PairingStore(root_dir, member_id)
        try:
            for index in range(int(iterations)):
                with store._file_lock():
                    start = time.monotonic_ns()
                    try:
                        with open(counter_path, "r", encoding="utf-8") as stream:
                            before = int(stream.read() or "0")
                    except FileNotFoundError:
                        before = 0
                    with open(counter_path, "w", encoding="utf-8") as stream:
                        stream.write(str(before + 1))
                    after = before + 1
                    end = time.monotonic_ns()
                    with open(log_path, "a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "pid": os.getpid(),
                                    "index": index,
                                    "start": start,
                                    "end": end,
                                    "before": before,
                                    "after": after,
                                },
                                sort_keys=True,
                            )
                            + "\\n"
                        )
            print("DONE", flush=True)
        finally:
            store.close()
    else:
        raise SystemExit(f"unknown mode: {mode}")
    """
)


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class PairingStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="caty-pairing-store-")
        self.root = os.path.join(self.tmp.name, "pairing")
        self.clock = MutableClock()
        self.warnings = []
        self.config = ps.PairingConfig()
        self.store = ps.PairingStore(
            self.root,
            "caty",
            config=self.config,
            warn=self.warnings.append,
            clock=self.clock,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _lock_path(self):
        return os.path.join(self.root, ".lockf")

    def _spawn_lock_helper(self, mode, *extra_args):
        env = os.environ.copy()
        python_path = os.path.dirname(os.path.dirname(__file__))
        env["PYTHONPATH"] = python_path + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                LOCK_HELPER,
                mode,
                self._lock_path(),
                *extra_args,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    def _read_line(self, proc, timeout=2.0):
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            if proc.poll() is None:
                proc.kill()
            stdout, stderr = proc.communicate()
            self.fail(
                "timed out waiting for child output; "
                f"rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"
            )
        line = proc.stdout.readline().strip()
        if not line:
            if proc.poll() is None:
                proc.kill()
            stdout, stderr = proc.communicate()
            self.fail(
                "child exited without output; "
                f"rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"
            )
        return line

    def _assert_process_ok(self, proc, timeout=2.0):
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            self.fail(f"child timeout stdout={stdout!r} stderr={stderr!r}")
        if proc.returncode != 0:
            self.fail(f"child rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}")
        return stdout, stderr

    @staticmethod
    def _fd_dir():
        for candidate in ("/dev/fd", "/proc/self/fd"):
            if os.path.isdir(candidate):
                return candidate
        raise unittest.SkipTest("no fd listing directory available")

    def test_member_namespace_matches_store_directory_component(self):
        # A record whose member_id disagrees with the directory it lives in is
        # deleted as foreign, so both must come from one derivation (§7-3).
        for env, expected in (
            ({}, "default"),
            ({"CATY_ID": ""}, "default"),
            ({"CATY_ID": "  "}, "default"),
            ({"CATY_ID": ".."}, "default"),
            ({"CATY_ID": "caty"}, "caty"),
            ({"CATY_ID": "myagent"}, "myagent"),
        ):
            with self.subTest(env=env):
                patched = dict(env)
                with mock.patch.dict(os.environ, patched, clear=False):
                    if "CATY_ID" not in patched:
                        os.environ.pop("CATY_ID", None)
                    os.environ.pop("CATY_PAIRING_DIR", None)
                    member = ps.default_pairing_member()
                    root = ps.default_pairing_root()
                self.assertEqual(member, expected)
                self.assertEqual(os.path.basename(root), expected)

    def test_sweep_removes_orphan_part_and_aged_a2_stale_files_without_touching_lock_names(self):
        issued = self.store.issue()
        orphan = Path(self.root, issued["pid"] + ".json.part")
        orphan.write_text("{}")
        stray = Path(self.root, "deadbeef.json.part")
        stray.write_text("{}")
        stale_lock = Path(self.root, ".lock.stale-1234-deadbeef")
        stale_lock.write_text("stale")
        os.utime(stale_lock, (0, 0))
        fresh_lock = Path(self.root, ".lock.stale-1234-feedface")
        fresh_lock.write_text("fresh")
        fresh_mtime = self.clock.value - ps.LOCK_STALE_SECONDS + 1
        os.utime(fresh_lock, (fresh_mtime, fresh_mtime))
        boundary_lock = Path(self.root, ".lock.stale-1234-cafebabe")
        boundary_lock.write_text("just stale")
        boundary_mtime = self.clock.value - ps.LOCK_STALE_SECONDS - 0.001
        os.utime(boundary_lock, (boundary_mtime, boundary_mtime))
        live_lockf = Path(self.root, ".lockf")
        before = os.stat(live_lockf, follow_symlinks=False)
        # An aged A2-era `.lock` body must survive: only `.lock.stale-*`
        # remnants are sweepable, never either lock body (§7-9, §7-2 rule 4).
        legacy_lock = Path(self.root, ".lock")
        legacy_lock.write_text("12345")
        os.utime(legacy_lock, (0, 0))
        legacy_before = os.stat(legacy_lock, follow_symlinks=False)
        foreign = Path(self.root, "notes.part")
        foreign.write_text("keep me")

        self.store.sweep()

        after = os.stat(live_lockf, follow_symlinks=False)
        self.assertFalse(orphan.exists())
        self.assertFalse(stray.exists())
        self.assertFalse(stale_lock.exists())
        self.assertTrue(fresh_lock.exists())
        self.assertFalse(boundary_lock.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        legacy_after = os.stat(legacy_lock, follow_symlinks=False)
        self.assertEqual(
            (legacy_before.st_dev, legacy_before.st_ino),
            (legacy_after.st_dev, legacy_after.st_ino),
        )
        self.assertEqual(self.store.live_count(), 1)

    def test_flock_is_exclusive_across_processes_and_unlock_allows_next_holder(self):
        holder = self._spawn_lock_helper("hold")
        contender = None
        try:
            self.assertEqual(self._read_line(holder), "HELD")
            contender = self._spawn_lock_helper("probe")
            self.assertEqual(self._read_line(contender), "BLOCKED")
            self._assert_process_ok(contender)
            holder.stdin.write("\n")
            holder.stdin.flush()
            self.assertEqual(self._read_line(holder), "RELEASED")
            self._assert_process_ok(holder)
            reacquire = self._spawn_lock_helper("probe")
            try:
                self.assertEqual(self._read_line(reacquire), "ACQUIRED")
                self._assert_process_ok(reacquire)
            finally:
                if reacquire.poll() is None:
                    reacquire.kill()
                    reacquire.communicate()
        finally:
            for proc in (contender, holder):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.communicate()

    def test_sigkill_releases_lock_immediately_for_next_process(self):
        holder = self._spawn_lock_helper("hold")
        try:
            self.assertEqual(self._read_line(holder), "HELD")
            os.kill(holder.pid, signal.SIGKILL)
            holder.wait(timeout=2)
            contender = self._spawn_lock_helper("probe")
            try:
                self.assertEqual(self._read_line(contender), "ACQUIRED")
                self._assert_process_ok(contender)
            finally:
                if contender.poll() is None:
                    contender.kill()
                    contender.communicate()
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.communicate()

    def test_unlinking_lock_path_while_held_allows_second_holder_on_replacement_inode(self):
        holder = self._spawn_lock_helper("hold")
        try:
            self.assertEqual(self._read_line(holder), "HELD")
            os.unlink(self._lock_path())
            contender = self._spawn_lock_helper("probe")
            try:
                self.assertEqual(self._read_line(contender), "ACQUIRED")
                self._assert_process_ok(contender)
            finally:
                if contender.poll() is None:
                    contender.kill()
                    contender.communicate()
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.communicate()

    def test_same_process_threads_still_exclude_each_other_with_distinct_fds(self):
        holder_ready = threading.Event()
        release_holder = threading.Event()
        outcomes = []
        errors = []

        def holder():
            try:
                with self.store._file_lock():
                    holder_ready.set()
                    release_holder.wait(timeout=2)
            except Exception as error:
                errors.append(error)
                holder_ready.set()
                release_holder.set()

        def contender():
            if not holder_ready.wait(timeout=2):
                errors.append(RuntimeError("holder did not acquire lock"))
                release_holder.set()
                return
            try:
                with self.store._file_lock():
                    outcomes.append("acquired")
            except ps.PairingUnavailable:
                outcomes.append("blocked")
            except Exception as error:
                errors.append(error)
            finally:
                release_holder.set()

        threads = [threading.Thread(target=holder), threading.Thread(target=contender)]
        with mock.patch.object(ps, "LOCK_WAIT_SECONDS", 0.02):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, ["blocked"])

    def test_file_lock_is_non_reentrant_and_warns_on_timeout(self):
        with self.store._file_lock(), mock.patch.object(ps, "LOCK_WAIT_SECONDS", 0.01), mock.patch.object(
            ps.time,
            "monotonic",
            side_effect=[0.0, 0.02],
        ), mock.patch.object(ps.time, "sleep", return_value=None):
            with self.assertRaisesRegex(ps.PairingUnavailable, "pairing store lock busy"):
                with self.store._file_lock():
                    pass
        self.assertIn("lock_wait_timeout", self.warnings)

    def test_failed_lock_acquire_does_not_leak_fds(self):
        second = ps.PairingStore(
            self.root,
            "caty",
            config=self.config,
            warn=self.warnings.append,
            clock=self.clock,
        )
        try:
            fd_dir = self._fd_dir()
            with self.store._file_lock(), mock.patch.object(ps, "LOCK_WAIT_SECONDS", 0.001), mock.patch.object(
                ps.time, "sleep", return_value=None
            ):
                before = len(os.listdir(fd_dir))
                for _ in range(25):
                    with self.assertRaises(ps.PairingUnavailable):
                        second.issue()
                after = len(os.listdir(fd_dir))
            self.assertEqual(after, before)
            self.assertEqual(self.warnings.count("lock_wait_timeout"), 1)
        finally:
            second.close()

    def test_unexpected_flock_error_is_wrapped_and_closes_fd(self):
        lock = self.store._file_lock()
        fd = os.open(
            self._lock_path(),
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with mock.patch.object(lock, "_open_fd", return_value=fd), \
                mock.patch.object(ps.fcntl, "flock", side_effect=OSError("boom")):
            with self.assertRaisesRegex(
                ps.PairingUnavailable, "cannot acquire pairing store lock"
            ):
                lock.__enter__()
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_parallel_critical_sections_never_overlap_and_never_lose_counter_updates(self):
        counter_path = os.path.join(self.root, "counter.txt")
        log_path = os.path.join(self.root, "critical.jsonl")
        processes = [
            self._spawn_lock_helper(
                "critical",
                self.root,
                "caty",
                counter_path,
                log_path,
                "20",
            )
            for _ in range(4)
        ]
        try:
            for proc in processes:
                self.assertEqual(self._read_line(proc, timeout=5.0), "DONE")
            for proc in processes:
                self._assert_process_ok(proc, timeout=5.0)
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()

        with open(counter_path, "r", encoding="utf-8") as stream:
            self.assertEqual(int(stream.read()), 80)
        with open(log_path, "r", encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream if line.strip()]
        self.assertEqual(len(records), 80)
        self.assertEqual(sorted(record["after"] for record in records), list(range(1, 81)))
        ordered = sorted(records, key=lambda item: item["start"])
        for previous, current in zip(ordered, ordered[1:]):
            self.assertLessEqual(previous["end"], current["start"])

    def test_lock_path_symlink_is_rejected(self):
        other_root = os.path.join(self.tmp.name, "symlinked")
        os.makedirs(other_root, mode=0o700)
        os.symlink(os.path.join(self.tmp.name, "target"), os.path.join(other_root, ".lockf"))
        with self.assertRaises(ps.PairingUnavailable):
            ps.PairingStore(
                other_root,
                "caty",
                config=self.config,
                warn=self.warnings.append,
                clock=self.clock,
            )

    def test_lock_open_uses_cloexec_nofollow_and_does_not_truncate(self):
        lock = self.store._file_lock()
        with mock.patch.object(ps.os, "open", wraps=os.open) as opened:
            fd = lock._open_fd()
        os.close(fd)
        _, flags, mode = opened.call_args.args
        self.assertEqual(flags & os.O_CLOEXEC, os.O_CLOEXEC)
        self.assertEqual(flags & os.O_NOFOLLOW, os.O_NOFOLLOW)
        self.assertEqual(flags & os.O_TRUNC, 0)
        self.assertEqual(mode, 0o600)

    def test_pair_regex_rejects_before_any_file_io(self):
        uninitialized = object.__new__(ps.PairingStore)
        with mock.patch.object(ps.os, "open") as opened, mock.patch.object(ps.os, "listdir") as listed:
            with self.assertRaises(ps.PairingInvalid):
                uninitialized.claim("../../x." + "a" * 32, lambda: "token")
        opened.assert_not_called()
        listed.assert_not_called()
        self.assertIsNone(ps.PAIR_RE.fullmatch("A1B2C3D4." + "a" * 32))

    def test_issue_persists_only_hash_with_private_permissions(self):
        issued = self.store.issue()
        pid, secret = issued["pair"].split(".")
        record_path = Path(self.root, pid + ".json")
        record = json.loads(record_path.read_text())
        self.assertNotIn(secret, record_path.read_text())
        self.assertEqual(record["secret_sha256"], hashlib.sha256(secret.encode()).hexdigest())
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
        lock_path = Path(self._lock_path())
        self.assertEqual(lock_path.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_claim_once_then_second_store_observes_consumed_tombstone(self):
        issued = self.store.issue()
        self.assertEqual(self.store.claim(issued["pair"], lambda: "long-lived"), "long-lived")
        second = ps.PairingStore(self.root, "caty", config=self.config, clock=self.clock)
        try:
            with self.assertRaises(ps.PairingConsumed):
                second.claim(issued["pair"], lambda: "long-lived")
            tombstone = json.loads(Path(self.root, issued["pid"] + ".tombstone").read_text())
            self.assertEqual(set(tombstone), {"pid", "state", "at", "expires_at"})
            self.assertEqual(tombstone["state"], "consumed")
        finally:
            second.close()

    def test_repeated_gateway_store_restarts_do_not_revoke_live_credential(self):
        issued = self.store.issue()
        expected_pid = issued["pid"]
        for _ in range(8):
            self.store.close()
            self.store = ps.PairingStore(
                self.root,
                "caty",
                config=self.config,
                warn=self.warnings.append,
                clock=self.clock,
            )
            self.assertEqual(self.store.live_count(), 1)
            self.assertTrue(Path(self.root, expected_pid + ".json").is_file())

    def test_claim_and_tombstone_outcomes_survive_fresh_store_instances(self):
        issued = self.store.issue()
        self.store.close()  # drop all process-local objects before claim
        self.store = ps.PairingStore(self.root, "caty", config=self.config, clock=self.clock)
        self.assertEqual(self.store.claim(issued["pair"], lambda: "long-lived"), "long-lived")
        self.store.close()
        self.store = ps.PairingStore(self.root, "caty", config=self.config, clock=self.clock)
        with self.assertRaises(ps.PairingConsumed):
            self.store.claim(issued["pair"], lambda: "long-lived")

        expired = self.store.issue()
        self.clock.value += self.config.ttl_seconds
        self.store.close()
        self.store = ps.PairingStore(self.root, "caty", config=self.config, clock=self.clock)
        with self.assertRaises(ps.PairingExpired):
            self.store.claim(expired["pair"], lambda: "long-lived")
        self.store.close()
        self.store = ps.PairingStore(self.root, "caty", config=self.config, clock=self.clock)
        with self.assertRaises(ps.PairingExpired):
            self.store.claim(expired["pair"], lambda: "long-lived")

    def test_concurrent_cross_process_claim_has_one_unlink_winner(self):
        issued = self.store.issue()
        second = ps.PairingStore(self.root, "caty", config=self.config, clock=self.clock)
        barrier = threading.Barrier(2)
        results = []

        def synchronize_first_read(store):
            original = store._read_record
            first = True

            def wrapped(pid):
                nonlocal first
                result = original(pid)
                if first:
                    first = False
                    barrier.wait(timeout=1)
                return result

            store._read_record = wrapped

        synchronize_first_read(self.store)
        synchronize_first_read(second)

        def claim(store):
            try:
                results.append(store.claim(issued["pair"], lambda: "token"))
            except Exception as error:
                results.append(type(error))

        threads = [threading.Thread(target=claim, args=(store,)) for store in (self.store, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        second.close()
        self.assertCountEqual(results, ["token", ps.PairingConsumed])

    def test_expired_unknown_wrong_secret_and_revoked_are_distinct(self):
        issued = self.store.issue()
        self.clock.value += self.config.ttl_seconds
        with self.assertRaises(ps.PairingExpired):
            self.store.claim(issued["pair"], lambda: "token")

        unknown = "01234567." + "a" * 32
        with self.assertRaises(ps.PairingInvalid):
            self.store.claim(unknown, lambda: "token")

        self.clock.value += 1
        wrong_target = self.store.issue()
        wrong = wrong_target["pid"] + "." + "0" * 32
        if wrong == wrong_target["pair"]:
            wrong = wrong_target["pid"] + "." + "1" * 32
        with self.assertRaises(ps.PairingInvalid):
            self.store.claim(wrong, lambda: "token")
        self.store.revoke()
        with self.assertRaises(ps.PairingInvalid):
            self.store.claim(wrong_target["pair"], lambda: "token")

    def test_reissue_revokes_old_without_tombstone(self):
        old = self.store.issue()
        self.clock.value += 1
        new = self.store.issue()
        self.assertEqual(self.store.live_count(), 1)
        with self.assertRaises(ps.PairingInvalid):
            self.store.claim(old["pair"], lambda: "token")
        self.assertFalse(Path(self.root, old["pid"] + ".tombstone").exists())
        self.assertEqual(self.store.claim(new["pair"], lambda: "token"), "token")

    def test_issue_rolls_back_new_record_when_old_revocation_fails(self):
        with mock.patch.object(self.store, "_revoke_records_except", side_effect=OSError("boom")):
            with self.assertRaises(ps.PairingUnavailable):
                self.store.issue()
        self.assertEqual(self.store.live_count(), 0)

    def test_lockout_resets_consecutive_window(self):
        issued = self.store.issue()
        wrong = issued["pid"] + "." + "f" * 32
        if wrong == issued["pair"]:
            wrong = issued["pid"] + "." + "e" * 32
        for _ in range(self.config.max_failures):
            with self.assertRaises(ps.PairingInvalid):
                self.store.claim(wrong, lambda: "token")
        with self.assertRaises(ps.PairingRateLimited):
            self.store.claim(wrong, lambda: "token")
        self.clock.value += self.config.lockout_seconds
        with self.assertRaises(ps.PairingInvalid):
            self.store.claim(wrong, lambda: "token")
        record = json.loads(Path(self.root, issued["pid"] + ".json").read_text())
        self.assertEqual(record["fail_count"], 1)
        self.assertEqual(record["fail_total"], self.config.max_failures + 1)

    def test_cumulative_failure_revokes_but_can_be_disabled_for_loopback(self):
        config = ps.PairingConfig(max_failures=2, lockout_seconds=1, max_fail_total=3)
        store = ps.PairingStore(
            os.path.join(self.tmp.name, "cumulative"),
            "caty",
            config=config,
            clock=self.clock,
        )
        try:
            issued = store.issue()
            wrong = issued["pid"] + "." + "0" * 32
            for _ in range(3):
                with self.assertRaises(ps.PairingInvalid):
                    store.claim(wrong, lambda: "token")
                self.clock.value += 1
            self.assertEqual(store.live_count(), 0)

            issued = store.issue()
            wrong = issued["pid"] + "." + "0" * 32
            for _ in range(4):
                for _ in range(config.max_failures):
                    with self.assertRaises(ps.PairingInvalid):
                        store.claim(wrong, lambda: "token", enforce_cumulative_revoke=False)
                with self.assertRaises(ps.PairingRateLimited):
                    store.claim(wrong, lambda: "token", enforce_cumulative_revoke=False)
                self.clock.value += config.lockout_seconds
            self.assertEqual(store.live_count(), 1)
        finally:
            store.close()

    def test_default_fifty_cumulative_failures_revoke(self):
        issued = self.store.issue()
        wrong = issued["pid"] + "." + "0" * 32
        for _ in range(self.config.max_fail_total // self.config.max_failures):
            for _ in range(self.config.max_failures):
                with self.assertRaises(ps.PairingInvalid):
                    self.store.claim(wrong, lambda: "token")
            self.clock.value += self.config.lockout_seconds
        self.assertEqual(self.config.max_fail_total, 50)
        self.assertEqual(self.store.live_count(), 0)
        with self.assertRaises(ps.PairingInvalid):
            self.store.claim(issued["pair"], lambda: "token")

    def test_postcondition_empty_credential_does_not_consume(self):
        issued = self.store.issue()
        with self.assertRaises(ps.PairingUnavailable):
            self.store.claim(issued["pair"], lambda: "")
        self.assertEqual(self.store.live_count(), 1)
        self.assertEqual(self.store.claim(issued["pair"], lambda: "token"), "token")

    def test_corrupt_secret_hash_is_invalid_and_deleted(self):
        issued = self.store.issue()
        path = Path(self.root, issued["pid"] + ".json")
        record = json.loads(path.read_text())
        record["secret_sha256"] = "not-a-hash"
        path.write_text(json.dumps(record))
        with self.assertRaises(ps.PairingInvalid):
            self.store.claim(issued["pair"], lambda: "token")
        self.assertFalse(path.exists())

    def test_failure_counter_persist_error_is_invalid_and_warned(self):
        issued = self.store.issue()
        wrong = issued["pid"] + "." + "0" * 32
        with mock.patch.object(self.store, "_write_private_file", side_effect=OSError("disk full")):
            with self.assertRaises(ps.PairingInvalid):
                self.store.claim(wrong, lambda: "token")
        self.assertTrue(any("failure_counter_persist_failed" in item for item in self.warnings))
        record = json.loads(Path(self.root, issued["pid"] + ".json").read_text())
        self.assertEqual(record["fail_total"], 0)

    def test_sweep_uses_record_content_not_mtime_and_retains_unparseable(self):
        issued = self.store.issue()
        record_path = Path(self.root, issued["pid"] + ".json")
        os.utime(record_path, (0, 0))
        self.clock.value += self.config.ttl_seconds - 1
        self.store.sweep()
        self.assertTrue(record_path.exists())
        broken = Path(self.root, "abcdef99.json")
        broken.write_text("not-json")
        self.clock.value += 1
        self.store.sweep()
        self.assertFalse(record_path.exists())
        self.assertTrue(broken.exists())
        self.assertTrue(any("sweep_unparseable_record" in item for item in self.warnings))

    def test_startup_keeps_newest_duplicate_warns_and_deletes_foreign(self):
        older = self.store.issue()
        old_path = Path(self.root, older["pid"] + ".json")
        old_record = json.loads(old_path.read_text())
        newer_pid = "abcdef12"
        newer_record = dict(old_record, pid=newer_pid, created_at=1001.0)
        Path(self.root, newer_pid + ".json").write_text(json.dumps(newer_record))
        foreign_pid = "abcdef34"
        foreign_record = dict(old_record, pid=foreign_pid, member_id="other")
        Path(self.root, foreign_pid + ".json").write_text(json.dumps(foreign_record))
        warnings = []
        restarted = ps.PairingStore(self.root, "caty", config=self.config, warn=warnings.append, clock=self.clock)
        try:
            self.assertFalse(old_path.exists())
            self.assertTrue(Path(self.root, newer_pid + ".json").exists())
            self.assertFalse(Path(self.root, foreign_pid + ".json").exists())
            self.assertTrue(any("duplicate_live_records" in item for item in warnings))
            self.assertTrue(any("foreign_member_record" in item for item in warnings))
        finally:
            restarted.close()

    def test_cross_process_revoke_is_observed_without_cache(self):
        issued = self.store.issue()
        second = ps.PairingStore(self.root, "caty", config=self.config, clock=self.clock)
        try:
            self.store.revoke()
            with self.assertRaises(ps.PairingInvalid):
                second.claim(issued["pair"], lambda: "token")
        finally:
            second.close()

    def test_config_rejects_bad_numbers_and_clamps_ttl_with_warning(self):
        for value in ("bad", "0", "-1"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"CATY_PAIRING_RATE_PER_MIN": value}, clear=False
            ):
                with self.assertRaisesRegex(ValueError, "CATY_PAIRING_RATE_PER_MIN"):
                    ps.load_config()
        warnings = []
        with mock.patch.dict(os.environ, {"CATY_PAIRING_TTL_SECONDS": "9999"}, clear=False):
            config = ps.load_config(warnings.append)
        self.assertEqual(config.ttl_seconds, 3600)
        self.assertTrue(any("clamped" in item for item in warnings))

    def test_busy_lock_is_unavailable_and_warned(self):
        holder = self._spawn_lock_helper("hold")
        try:
            self.assertEqual(self._read_line(holder), "HELD")
            with mock.patch.object(ps, "LOCK_WAIT_SECONDS", 0.01), mock.patch.object(
                ps.time,
                "monotonic",
                side_effect=[0.0, 0.02],
            ), mock.patch.object(ps.time, "sleep", return_value=None):
                with self.assertRaises(ps.PairingUnavailable):
                    self.store.issue()
            self.assertIn("lock_wait_timeout", self.warnings)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.communicate()


if __name__ == "__main__":
    unittest.main()
