import builtins
import io
import ipaddress
import itertools
import json
import os
import pathlib
import plistlib
import re
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import time

import pytest



from caty_gateway import caty_gateway
from caty_gateway import pairing_store
from caty_gateway.doctor import Doctor
from functools import partial
from caty_gateway import setup_orchestrator


OS_VALUES = ("Linux", "macOS")
BACKENDS = ("openclaw", "hermes", "claude", "codex", "openai-compat")
DELIVERIES = ("local/tty", "remote-chat/url")
PRECONDITIONS = ("clean", "collision", "interrupted-resume")
FULL_MATRIX = tuple(itertools.product(OS_VALUES, BACKENDS, DELIVERIES, PRECONDITIONS))
EXECUTED_CELLS = {
    ("Linux", "hermes", "remote-chat/url", "clean"),
    ("macOS", "openclaw", "local/tty", "collision"),
    ("Linux", "claude", "local/tty", "interrupted-resume"),
}


def _cell_id(cell):
    operating_system, backend, delivery, precondition = cell
    return f"{operating_system}-{backend}-{delivery}-{precondition}"


def _degraded_reason(cell):
    operating_system, backend, delivery, precondition = cell
    return (
        f"representative {precondition} coverage executes on a different "
        f"OS/backend/delivery cell; {operating_system}/{backend}/{delivery} is inspection-only"
    )


def _free_ephemeral_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


DEGRADED_CELLS = {
    cell: _degraded_reason(cell)
    for cell in FULL_MATRIX
    if cell not in EXECUTED_CELLS
}
MATRIX_RECORDS = tuple(
    (cell, "EXECUTED", "")
    if cell in EXECUTED_CELLS
    else (cell, "DEGRADED", DEGRADED_CELLS.get(cell, ""))
    for cell in FULL_MATRIX
)


LOCK_HELPER = textwrap.dedent(
    """
    import fcntl
    import os
    import sys

    mode = sys.argv[1]
    lock_path = sys.argv[2]
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        if mode == "hold":
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("HELD", flush=True)
            sys.stdin.readline()
        elif mode == "probe":
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("BLOCKED", flush=True)
            else:
                print("ACQUIRED", flush=True)
                fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            raise SystemExit("unknown mode")
    finally:
        os.close(fd)
    """
)


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class _NonClosingBytesIO(io.BytesIO):
    def close(self):
        pass


class MemorySocket:
    def __init__(self, request_bytes):
        self.input = io.BytesIO(request_bytes)
        self.output = _NonClosingBytesIO()

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


class _FakeImage:
    def save(self, handle, format):
        assert format == "PNG"
        handle.write(b"\x89PNG\r\n\x1a\nfixture")


class _FakeQR:
    def __init__(self, border):
        assert border == 1

    def add_data(self, payload):
        self.payload = payload

    def make(self, fit):
        assert fit is True

    def make_image(self):
        return _FakeImage()


class _FakeQRCodeModule:
    QRCode = _FakeQR


class _FakeDeliveryServer:
    def __init__(self):
        self.server_address = ("127.0.0.1", 49152)
        self.RequestHandlerClass = None
        self.timeout = None
        self.closed = False
        self.requests = 0

    def handle_request(self):
        if self.requests:
            return
        self.requests += 1
        handler = self.RequestHandlerClass
        raw = (
            f"GET {handler.delivery_path} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
        ).encode("latin-1")
        handler(MemorySocket(raw), ("127.0.0.1", 12345), MemoryServer())

    def server_close(self):
        self.closed = True


@pytest.fixture
def fake_home(tmp_path):
    home = tmp_path / "home"
    (home / ".config/caty-gateway").mkdir(parents=True)
    (home / ".local/state").mkdir(parents=True)
    return home


@pytest.fixture
def gateway_state(tmp_path, monkeypatch):
    saved = {
        "token": caty_gateway.CATY_TOKEN,
        "admin": caty_gateway.CATY_ADMIN_TOKEN,
        "store": caty_gateway._pairing_store,
        "config": caty_gateway._pairing_config,
        "limiter": caty_gateway._pair_claim_rate_limiter,
    }
    clock = MutableClock()
    config = pairing_store.PairingConfig()
    store = pairing_store.PairingStore(tmp_path / "pairing", "caty", config=config, clock=clock)
    token = "acceptance-" + "client-token"  # deliberate test canary
    monkeypatch.setenv("CATY_TOKEN", token)
    monkeypatch.setenv("CATY_PUBLIC_URL", "http://100.64.0.10:8788")
    caty_gateway.CATY_TOKEN = token
    caty_gateway.CATY_ADMIN_TOKEN = ""
    caty_gateway._pairing_store = store
    caty_gateway._pairing_config = config
    caty_gateway._pair_claim_rate_limiter = caty_gateway._PairClaimRateLimiter(clock=clock)
    yield store, clock, token
    store.close()
    caty_gateway.CATY_TOKEN = saved["token"]
    caty_gateway.CATY_ADMIN_TOKEN = saved["admin"]
    caty_gateway._pairing_store = saved["store"]
    caty_gateway._pairing_config = saved["config"]
    caty_gateway._pair_claim_rate_limiter = saved["limiter"]


def _write_exec(path, body):
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_fake_commands(workdir):
    bin_dir = workdir / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    _write_exec(
        bin_dir / "tailscale",
        """
        #!/usr/bin/env sh
        if [ "$1" = "status" ]; then exit 0; fi
        if [ "$1" = "ip" ]; then echo "100.64.0.1"; exit 0; fi
        exit 1
        """,
    )
    for command in ("systemctl", "loginctl", "ffmpeg", "ffprobe", "openclaw", "hermes", "claude", "codex", "launchctl"):
        _write_exec(bin_dir / command, "#!/usr/bin/env sh\necho main\n")
    _write_exec(bin_dir / "journalctl", "#!/usr/bin/env sh\necho journal line\n")
    return bin_dir


def _prepare_fake_gateway_files(workdir):
    # Child commands exercise module dispatch with a deliberately fake package.
    package_root = workdir / "fake_pyqrcode"
    for package in ("qrcode", "caty_gateway"):
        (package_root / package).mkdir(parents=True, exist_ok=True)
        (package_root / package / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "caty_gateway" / "__main__.py").write_text(
        "import sys\nif sys.argv[1:2] == ['qr']: print('QR URL: http://100.64.0.1:49152/qr/test')\n",
        encoding="utf-8",
    )


def _make_orchestrator(fake_home_dir, workdir, monkeypatch, *argv, system="Linux", extra_env=None):
    workdir.mkdir(parents=True, exist_ok=True)
    bin_dir = _prepare_fake_commands(workdir)
    _prepare_fake_gateway_files(workdir)
    env = {
        "HOME": str(fake_home_dir),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": str(workdir / "fake_pyqrcode"),
        "XDG_RUNTIME_DIR": str(fake_home_dir / "xdg-runtime"),
        "CATY_BACKEND": "openclaw",
    }
    env.update({"CATY_GATEWAY_TOKEN": "fake-gateway-token", "CATY_OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
                "CATY_OPENAI_MODEL": "fake-model"})
    monkeypatch.setattr(setup_orchestrator, "Doctor", partial(Doctor, port_available=lambda port: True,
        get_json=lambda url, headers, timeout: (200, {"data": [{"id": "fake-model"}]})))
    if extra_env:
        env.update(extra_env)
    monkeypatch.setattr(setup_orchestrator.platform, "system", lambda: system)
    orchestrator = setup_orchestrator.SetupOrchestrator(
        ["--member", "alice", *argv], env=env, workdir=workdir
    )
    orchestrator._probe_backend = lambda: True
    orchestrator._voice = lambda: None
    return orchestrator


def _snapshot_tree(root):
    return {
        str(path.relative_to(root)): (
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in (root, *sorted(root.rglob("*")))
    }


def _request(method, path, *, body=b"", headers=None, peer="127.0.0.1"):
    request_headers = dict(headers or {})
    request_headers.setdefault("Host", "127.0.0.1")
    request_headers.setdefault("Connection", "close")
    request_headers.setdefault("Content-Length", str(len(body)))
    lines = [f"{method} {path} HTTP/1.1"]
    lines.extend(f"{key}: {value}" for key, value in request_headers.items())
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    sock = MemorySocket(raw)
    caty_gateway.Handler(sock, (peer, 12345), MemoryServer())
    response = sock.output.getvalue()
    head, _, rest = response.partition(b"\r\n\r\n")
    response_lines = head.decode("iso-8859-1").split("\r\n")
    status = int(response_lines[0].split()[1])
    response_headers = {}
    for line in response_lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            response_headers[key.lower()] = value.strip()
    length = int(response_headers.get("content-length", "0"))
    payload = json.loads(rest[:length]) if length else None
    return status, payload


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _identity_is_remote_acceptance_signal(peer, status, authorized):
    try:
        address = ipaddress.ip_address(peer)
    except (TypeError, ValueError):
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        status == 200
        and authorized
        and not address.is_loopback
        and caty_gateway._tailnet_or_loopback_peer(str(address))
    )


@pytest.mark.parametrize(
    "cell,status,reason", MATRIX_RECORDS, ids=[_cell_id(record[0]) for record in MATRIX_RECORDS]
)
def test_every_nominal_matrix_cell_is_explicitly_classified(cell, status, reason):
    assert status in {"EXECUTED", "DEGRADED"}
    assert bool(reason) is (status == "DEGRADED")


def test_degraded_cell_list_exactly_equals_full_matrix_minus_executed_cells():
    assert set(DEGRADED_CELLS) == set(FULL_MATRIX) - EXECUTED_CELLS
    assert set(DEGRADED_CELLS).isdisjoint(EXECUTED_CELLS)
    assert len(FULL_MATRIX) == 60
    assert len(EXECUTED_CELLS) == 3
    assert len(DEGRADED_CELLS) == 57


def test_acceptance_matrix_is_complete_and_code_owned():
    assert len(FULL_MATRIX) == 60
    assert {cell[1] for cell in FULL_MATRIX} == {"openclaw", "hermes", "claude", "codex", "openai-compat"}
    assert set(DEGRADED_CELLS) == set(FULL_MATRIX) - EXECUTED_CELLS


def test_clean_linux_hermes_remote_chat_completes_all_phases_with_one_visible_qr_url(
    fake_home, tmp_path, monkeypatch, capfd
):
    port = _free_ephemeral_port()
    prompt_reads = []
    token_generations = []
    handoffs = []
    generated_token = "a1" * 24  # deliberate test canary, not a credential
    monkeypatch.setattr(
        builtins,
        "input",
        lambda *args, **kwargs: prompt_reads.append(args) or pytest.fail("--yes read stdin"),
    )
    def token_hex(size):
        token_generations.append(size)
        assert size == 24
        return generated_token

    monkeypatch.setattr(setup_orchestrator.secrets, "token_hex", token_hex)
    orchestrator = _make_orchestrator(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--backend",
        "hermes",
        "--port",
        str(port),
        "--public-url",
        f"http://100.64.0.1:{port}",
        "--qr-delivery",
        "url",
        system="Linux",
        extra_env={"CATY_BACKEND": "hermes", "CATY_HERMES_API_KEY": "fake-hermes-key"},
    )
    http_calls = []

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        url = request.full_url if hasattr(request, "full_url") else request
        headers = dict(request.header_items()) if hasattr(request, "header_items") else {}
        http_calls.append((url, headers, timeout))
        if url.endswith("/health"):
            return Response({"ok": True})
        assert url.endswith("/identity")
        assert headers.get("Authorization", "").startswith("Bearer ")
        return Response({"id": "alice"})

    monkeypatch.setattr(setup_orchestrator.urllib.request, "urlopen", fake_urlopen)
    orchestrator._supervised_handoff = lambda *args: handoffs.append(args) or True
    start = time.monotonic()
    assert orchestrator.run() == 0
    elapsed = time.monotonic() - start
    captured = capfd.readouterr().out
    assert tuple(orchestrator.state.completed_phases) == setup_orchestrator.PHASES
    assert "QR URL: http://100.64.0.1:49152/qr/test" in captured
    assert elapsed < 600
    assert prompt_reads == []
    assert len(handoffs) <= 1
    assert token_generations == [24]
    managed_surfaces = captured + orchestrator.status_path.read_text(encoding="utf-8")
    assert generated_token not in managed_surfaces
    assert [call[0] for call in http_calls] == [
        f"http://127.0.0.1:{port}/health",
        f"http://127.0.0.1:{port}/identity",
    ]
    assert not orchestrator.state_path.exists()
    print(f"MEASURED first-visible-QR seconds={elapsed:.2f} (fake externals; approval-wait excluded)")


def test_collision_macos_openclaw_local_aborts_before_side_effect_and_preserves_foreign_home(
    fake_home, tmp_path, monkeypatch, capsys
):
    port = _free_ephemeral_port()
    launch_agents = fake_home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    foreign = launch_agents / "ai.caty.gateway.bob.plist"
    foreign.write_bytes(
        plistlib.dumps(
            {"EnvironmentVariables": {"CATY_GATEWAY_PORT": str(port), "CATY_ID": "bob"}}
        )
    )
    foreign_before = (foreign.read_bytes(), foreign.stat().st_mtime_ns)
    home_before = _snapshot_tree(fake_home)
    orchestrator = _make_orchestrator(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--backend",
        "openclaw",
        "--port",
        str(port),
        "--public-url",
        f"http://100.64.0.1:{port}",
        "--qr-delivery",
        "tty",
        system="Darwin",
    )
    with pytest.raises(setup_orchestrator.SetupError, match="preflight failed"):
        orchestrator.run()
    evidence = capsys.readouterr().err
    assert "ai.caty.gateway.bob.plist" in evidence
    assert "interrupted job" not in evidence
    assert (foreign.read_bytes(), foreign.stat().st_mtime_ns) == foreign_before
    assert _snapshot_tree(fake_home) == home_before
    assert not orchestrator.state_path.exists()


def test_interrupted_resume_linux_claude_local_reuses_token_and_artifact_then_finishes(
    fake_home, tmp_path, monkeypatch
):
    port = _free_ephemeral_port()
    generated = []
    original_token_hex = setup_orchestrator.secrets.token_hex

    def token_hex(size):
        generated.append(size)
        return original_token_hex(size)

    monkeypatch.setattr(setup_orchestrator.secrets, "token_hex", token_hex)
    first = _make_orchestrator(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--backend",
        "claude",
        "--port",
        str(port),
        "--public-url",
        f"http://100.64.0.1:{port}",
        "--qr-delivery",
        "tty",
        extra_env={"CATY_CLAUDE_BIN": str(tmp_path / "fakebin" / "claude")},
    )
    first._health = lambda: (_ for _ in ()).throw(setup_orchestrator.SetupError("self-interruption"))
    first._identity = lambda: None
    first._qr = lambda: None
    with pytest.raises(setup_orchestrator.SetupError, match="self-interruption"):
        first.run()
    artifact = first.artifact_path
    artifact_before = (artifact.read_bytes(), artifact.stat().st_mtime_ns)
    assert first.state_path.exists()
    assert "install" in first.state.completed_phases
    second = _make_orchestrator(
        fake_home,
        tmp_path,
        monkeypatch,
        "--yes",
        "--backend",
        "claude",
        "--port",
        str(port),
        "--public-url",
        f"http://100.64.0.1:{port}",
        "--qr-delivery",
        "tty",
        extra_env={"CATY_CLAUDE_BIN": str(tmp_path / "fakebin" / "claude")},
    )
    second._health = lambda: None
    second._identity = lambda: None
    second._qr = lambda: None
    assert second.run() == 0
    assert tuple(second.state.completed_phases) == setup_orchestrator.PHASES
    assert (artifact.read_bytes(), artifact.stat().st_mtime_ns) == artifact_before
    assert generated == [24]
    assert (fake_home / ".config/systemd/user" / second.service_name).read_bytes() == second._expected_systemd_unit()
    assert not second.state_path.exists()


def test_collision_and_self_interrupt_have_distinguishable_acceptance_outcomes(
    fake_home, tmp_path, monkeypatch
):
    collision_port = _free_ephemeral_port()
    collision_home = tmp_path / "collision-home"
    (collision_home / ".config/caty-gateway").mkdir(parents=True)
    (collision_home / ".local/state").mkdir(parents=True)
    launch_agents = collision_home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    foreign = launch_agents / "ai.caty.gateway.bob.plist"
    foreign.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "CATY_GATEWAY_PORT": str(collision_port),
                    "CATY_ID": "bob",
                }
            }
        )
    )
    collision_before = _snapshot_tree(collision_home)
    collision_workdir = tmp_path / "collision-work"
    collision_workdir.mkdir()
    collision_orchestrator = _make_orchestrator(
        collision_home,
        collision_workdir,
        monkeypatch,
        "--yes",
        "--port",
        str(collision_port),
        "--public-url",
        f"http://100.64.0.1:{collision_port}",
        system="Darwin",
    )
    collision_result = None
    with pytest.raises(setup_orchestrator.SetupError):
        collision_result = collision_orchestrator.run()
    collision_outcome = {
        "completed": collision_result == 0,
        "home_mutated": _snapshot_tree(collision_home) != collision_before,
        "resume_state_left": collision_orchestrator.state_path.exists(),
    }

    resume_home = tmp_path / "resume-home"
    (resume_home / ".config/caty-gateway").mkdir(parents=True)
    (resume_home / ".local/state").mkdir(parents=True)
    resume_before = _snapshot_tree(resume_home)
    resume_workdir = tmp_path / "resume-work"
    resume_workdir.mkdir()
    resume_port = _free_ephemeral_port()
    interrupted = _make_orchestrator(
        resume_home,
        resume_workdir,
        monkeypatch,
        "--yes",
        "--port",
        str(resume_port),
        "--public-url",
        f"http://100.64.0.1:{resume_port}",
    )
    interrupted._health = lambda: (_ for _ in ()).throw(setup_orchestrator.SetupError("interrupted"))
    interrupted._identity = lambda: None
    interrupted._qr = lambda: None
    with pytest.raises(setup_orchestrator.SetupError, match="interrupted"):
        interrupted.run()
    assert interrupted.state_path.exists()
    resumed = _make_orchestrator(
        resume_home,
        resume_workdir,
        monkeypatch,
        "--yes",
        "--port",
        str(resume_port),
        "--public-url",
        f"http://100.64.0.1:{resume_port}",
    )
    resumed._health = lambda: None
    resumed._identity = lambda: None
    resumed._qr = lambda: None
    resume_result = resumed.run()
    resume_outcome = {
        "completed": resume_result == 0,
        "home_mutated": _snapshot_tree(resume_home) != resume_before,
        "resume_state_left": resumed.state_path.exists(),
    }
    assert collision_outcome == {
        "completed": False,
        "home_mutated": False,
        "resume_state_left": False,
    }
    assert resume_outcome == {
        "completed": True,
        "home_mutated": True,
        "resume_state_left": False,
    }
    for key in ("completed", "home_mutated"):
        assert collision_outcome[key] != resume_outcome[key]


def test_a3_busy_lock_times_out_once_per_window_and_sigkill_releases_same_lock_inode(
    tmp_path, monkeypatch
):
    warnings = []
    clock = MutableClock()
    store = pairing_store.PairingStore(tmp_path / "pairing", "caty", warn=warnings.append, clock=clock)
    monkeypatch.setattr(caty_gateway, "CATY_TOKEN", "lock-http-token")
    monkeypatch.setattr(caty_gateway, "_pairing_store", store)
    monkeypatch.setattr(caty_gateway, "_pairing_config", store.config)
    lock_path = pathlib.Path(store.root_dir) / ".lockf"
    inode_before = lock_path.stat().st_ino

    def spawn(mode):
        return subprocess.Popen(
            [sys.executable, "-B", "-c", LOCK_HELPER, mode, str(lock_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    holder = spawn("hold")
    try:
        assert holder.stdout.readline().strip() == "HELD"
        for times in ((0.0, 1.01), (2.0, 3.01)):
            values = iter(times)
            monkeypatch.setattr(pairing_store, "LOCK_WAIT_SECONDS", 1.0)
            monkeypatch.setattr(pairing_store.time, "monotonic", lambda: next(values))
            monkeypatch.setattr(pairing_store.time, "sleep", lambda _seconds: None)
            with pytest.raises(pairing_store.PairingUnavailable, match="lock busy"):
                store.issue()
        values = iter((4.0, 5.01))
        monkeypatch.setattr(pairing_store.time, "monotonic", lambda: next(values))
        status, payload = _request("POST", "/pair/new", headers=_auth(caty_gateway.CATY_TOKEN))
        assert (status, payload["error"]) == (503, "pairing_disabled")
        assert warnings.count("lock_wait_timeout") == 1
        holder.send_signal(signal.SIGKILL)
        holder.communicate(timeout=5)
        probe = spawn("probe")
        stdout, stderr = probe.communicate(timeout=5)
        assert probe.returncode == 0, stderr
        assert stdout.strip() == "ACQUIRED"
        assert lock_path.is_file()
        assert lock_path.stat().st_ino == inode_before
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=5)
        store.close()


def test_matrix_harness_preserves_persistent_lock_file_inode(tmp_path):
    store = pairing_store.PairingStore(tmp_path / "pairing", "caty")
    lock_path = pathlib.Path(store.root_dir) / ".lockf"
    inode = lock_path.stat().st_ino
    store.issue()
    store.revoke()
    store.sweep()
    assert lock_path.is_file()
    assert lock_path.stat().st_ino == inode
    store.close()


def test_revoke_is_immediate_within_one_simulated_second_at_http_layer(gateway_state):
    store, clock, token = gateway_state
    issued = store.issue()
    status, _ = _request("POST", "/pair/revoke", headers=_auth(token))
    assert status == 200
    clock.value += 0.5
    body = json.dumps({"pair": issued["pair"]}).encode()
    status, payload = _request(
        "POST", "/pair/claim", body=body, headers={**_auth(token), "Content-Type": "application/json"}
    )
    assert (status, payload["error"]) == (401, "pairing_invalid")


def test_consumed_409_and_expired_410_tombstones_survive_gateway_restart(gateway_state):
    store, clock, token = gateway_state
    consumed = store.issue()
    body = json.dumps({"pair": consumed["pair"]}).encode()
    assert _request("POST", "/pair/claim", body=body, headers={"Content-Type": "application/json"})[0] == 200
    expired = store.issue()
    clock.value += store.config.ttl_seconds
    store.close()
    fresh = pairing_store.PairingStore(store.root_dir, "caty", config=store.config, clock=clock)
    caty_gateway._pairing_store = fresh
    try:
        consumed_status, consumed_payload = _request(
            "POST", "/pair/claim", body=body, headers={"Content-Type": "application/json"}
        )
        fresh.close()
        fresh = pairing_store.PairingStore(store.root_dir, "caty", config=store.config, clock=clock)
        caty_gateway._pairing_store = fresh
        expired_body = json.dumps({"pair": expired["pair"]}).encode()
        expired_status, expired_payload = _request(
            "POST", "/pair/claim", body=expired_body, headers={"Content-Type": "application/json"}
        )
        assert (consumed_status, consumed_payload["error"]) == (409, "pairing_consumed")
        assert (expired_status, expired_payload["error"]) == (410, "pairing_expired")
    finally:
        fresh.close()


def test_identity_acceptance_separates_local_health_from_tailnet_authenticated_identity(
    gateway_state, capsys
):
    _store, _clock, token = gateway_state
    caty_gateway.log("acceptance_reach=local-health peer=127.0.0.1")
    health_status, _ = _request("GET", "/health", headers=_auth(token), peer="127.0.0.1")
    remote_signal = _identity_is_remote_acceptance_signal("100.64.0.5", 200, True)
    caty_gateway.log(
        f"acceptance_reach=tailnet-identity peer=100.64.0.5 acceptance_signal={str(remote_signal).lower()}"
    )
    identity_status, payload = _request("GET", "/identity", headers=_auth(token), peer="100.64.0.5")
    unauthorized_status, _ = _request("GET", "/identity", peer="100.64.0.5")
    evidence = capsys.readouterr().out
    assert health_status == 200
    assert identity_status == 200
    assert payload["id"]
    assert unauthorized_status == 401
    assert "acceptance_reach=local-health" in evidence
    assert "acceptance_reach=tailnet-identity" in evidence
    assert "acceptance_signal=true" in evidence


def test_localhost_identity_success_is_not_the_remote_acceptance_signal(gateway_state):
    _store, _clock, token = gateway_state
    peer = "127.0.0.1"
    status, _ = _request("GET", "/identity", headers=_auth(token), peer=peer)
    acceptance_signal = _identity_is_remote_acceptance_signal(peer, status, authorized=True)
    assert status == 200
    assert acceptance_signal is False


def test_two_way_secret_redaction_canary_covers_both_redactors_and_all_managed_surfaces(
    gateway_state, fake_home, tmp_path, monkeypatch, capsys
):
    store, _clock, token = gateway_state
    issued_records = []

    def issue_for_qr():
        issued = store.issue()
        issued["expires_at"] = time.time() + 60
        issued_records.append(issued)
        return issued

    delivery_server = _FakeDeliveryServer()
    monkeypatch.setattr(caty_gateway, "_load_qr_png_dependencies", lambda: _FakeQRCodeModule)
    monkeypatch.setattr(caty_gateway, "_issue_pairing_for_qr", issue_for_qr)
    monkeypatch.setattr(caty_gateway, "_bind_qr_delivery_server", lambda *_args: delivery_server)
    assert caty_gateway.print_qr_url(3) is True
    issued = issued_records[0]
    pair = issued["pair"]  # deliberate test canary: actually issued, never a source literal
    pid = issued["pid"]
    evidence_line = (
        f"{pair} CATY_TOKEN={token} pairing_invalid pairing_disabled "
        f"stage=pairing status=pairing_invalid pid={pid}"
    )
    orchestrator_surface = setup_orchestrator.redact(evidence_line)
    gateway_surface = caty_gateway._redact_log_text(evidence_line)
    orchestrator = _make_orchestrator(
        fake_home,
        tmp_path / "orchestrator-surface",
        monkeypatch,
        "--yes",
        "--public-url",
        "http://100.64.0.1:8788",
    )
    orchestrator._update_status(state="running", timeline_entry=evidence_line)
    status_surface = orchestrator.status_path.read_text(encoding="utf-8")
    print(orchestrator_surface)
    caty_gateway.log(evidence_line)
    captured = capsys.readouterr().out
    all_surfaces = "\n".join(
        (orchestrator_surface, gateway_surface, status_surface, captured)
    )
    assert pair not in all_surfaces
    assert token not in all_surfaces
    assert "pairing_invalid" in all_surfaces
    assert "pairing_disabled" in all_surfaces
    assert f"stage=pairing status=pairing_invalid pid={pid}" in all_surfaces
    assert "QR URL: http://127.0.0.1:49152/qr/" in captured
    assert delivery_server.closed is True


@pytest.mark.parametrize("backend", BACKENDS)
def test_all_release_backends_pass_passive_setup_plan(fake_home, tmp_path, monkeypatch, backend):
    orchestrator = _make_orchestrator(fake_home, tmp_path, monkeypatch,
        "--backend", backend, "--yes", "--plan-only", "--port", str(_free_ephemeral_port()),
        "--public-url", "http://127.0.0.1:8788",
        extra_env={"CATY_HERMES_API_KEY": "fake-hermes-key"})
    assert orchestrator.run() == 0
    assert orchestrator.config["backend"] == backend
    assert not orchestrator.state_path.exists()
    assert not orchestrator.artifact_path.exists()
