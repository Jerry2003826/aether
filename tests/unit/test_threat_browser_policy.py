import base64
import importlib.util
import ipaddress
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from importlib.metadata import version as package_version
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from aether_worker import threat_browser, threat_browser_entry
from aether_worker.ingest import FIXTURE_ENV
from aether_worker.threat_browser import (
    OwnerApproval,
    PageCapture,
    Switches,
    ThreatBrowserError,
    VerifierPermit,
    VisitRequest,
    _Fetched,
    _pinned_get,
    _Gate,
    authorize_target,
    bundled_chromium_revision,
    default_switches,
    intent_digest,
)
from aether_worker import runner_process
from aether_worker.threat_launch import ThreatLaunchError, browser_command, docker_runner, link_binding, setup_commands
from aether_worker.threat_visit import visit_threat_page
from aether_worker.threat_browser_entry import run_entry
from aether_worker import threat_visit
from aether_worker.threat_pins import (
    APPROVED_TOR_PROXY,
    CHROMIUM_BROWSER_VERSION,
    CHROMIUM_REVISION,
    DEBIAN_IMAGE,
    ENTRYPOINT,
    PLAYWRIGHT_IMAGE,
    PLAYWRIGHT_VERSION,
    TOR_ALIAS,
    TOR_KEY_FINGERPRINT,
    TOR_VERSION,
)

BROWSER = Switches(browser_enabled=True, tor_enabled=False, onion_enabled=False, owner_confirmed=False)
TOR = Switches(browser_enabled=True, tor_enabled=True, onion_enabled=True, owner_confirmed=True)
PUBLIC = "https://example.com/note"


def _resolve(_host: str):
    return [ipaddress.ip_address("93.184.216.34")]


def _grant() -> object:
    state = {"used": False}

    def authorize(_request: VisitRequest, _digest: str) -> str:
        if state["used"]:
            raise ThreatBrowserError("approval_consumed")
        state["used"] = True
        return "owner"

    return authorize


def _deploy(monkeypatch: pytest.MonkeyPatch, switches: Switches) -> None:
    monkeypatch.setattr(threat_browser, "default_switches", lambda path=None: switches)


def _request(**kwargs: object) -> VisitRequest:
    payload: dict[str, object] = {"url": PUBLIC, "switches": BROWSER, "deployed": BROWSER, "runtime": "playwright"}
    payload.update(kwargs)
    return VisitRequest(**payload)  # type: ignore[arg-type]


@pytest.fixture
def no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("browser opened")

    monkeypatch.setattr(threat_browser, "read_page", boom)


def test_checked_in_switches_are_off() -> None:
    text = Path("config/threat_browser.yaml").read_text(encoding="utf-8")
    assert "enabled: false" in text
    assert "onion_enabled: false" in text
    assert "owner_confirmed: false" in text
    switches = default_switches()
    assert switches == Switches()


BUILT_BROWSER = "sha256:" + ("ab" * 32)
BUILT_TOR = "sha256:" + ("cd" * 32)


def test_browser_image_digest_matches_the_dockerfile() -> None:
    text = Path("infra/threat-browser.Dockerfile").read_text(encoding="utf-8")
    digest = PLAYWRIGHT_IMAGE.split("@", 1)[1]
    assert text.startswith(f"FROM mcr.microsoft.com/playwright@{digest}\n")
    assert "playwright==1.64.0" in text
    assert ":latest" not in text
    assert "pyyaml==6.0.3" in text
    assert "osminogin" not in text
    assert "COPY packages/domain/aether_domain /opt/aether/aether_domain" in text
    assert "COPY services/worker/aether_worker/threat_browser_entry.py" in text
    assert "COPY config/threat_browser.yaml /opt/aether/config/threat_browser.yaml" in text
    assert bundled_chromium_revision() == CHROMIUM_REVISION
    assert package_version("playwright") == PLAYWRIGHT_VERSION


def test_tor_image_is_built_from_debian_and_the_official_archive() -> None:
    dockerfile = Path("infra/tor.Dockerfile").read_text(encoding="utf-8")
    script = Path("infra/tor/install_tor.py").read_text(encoding="utf-8")
    entry = Path("infra/tor/entrypoint.sh").read_text(encoding="utf-8")
    assert dockerfile.startswith(f"FROM {DEBIAN_IMAGE}\n")
    assert "USER debian-tor" in dockerfile
    assert "policy-rc.d" in dockerfile
    assert "osminogin" not in dockerfile and "osminogin" not in script
    assert "https://deb.torproject.org/torproject.org" in script
    assert TOR_KEY_FINGERPRINT in script
    assert TOR_VERSION in script
    assert "/tmp" not in script
    fixture = Path("infra/tor/fixture_server.py").read_text(encoding="utf-8")
    assert "ssl.SSLContext" in fixture
    assert "serve_forever" not in fixture
    assert "keyout" not in dockerfile
    assert "/tmp" not in entry
    assert "DisableNetwork 1" in entry
    assert "AETHER_TOR_FIXTURE" in entry
    assert "keyout" in entry


@pytest.mark.parametrize(
    ("action", "code"),
    [
        ("trade", "trade_rejected"),
        ("illegal_download", "illegal_download_rejected"),
        ("contact_seller", "contact_seller_rejected"),
    ],
)
def test_trade_illegal_download_and_seller_contact_are_rejected(no_browser: None, action: str, code: str) -> None:
    audit: list[dict[str, object]] = []
    with pytest.raises(ThreatBrowserError) as rejected:
        visit_threat_page(_request(action=action), audit=audit, resolve=_resolve)
    assert rejected.value.code == code
    assert audit[-1]["decision"] == code
    assert audit[-1]["admissible_evidence"] is False


@pytest.mark.parametrize(
    ("goal", "code"),
    [
        ("请购买这份资料", "trade_rejected"),
        ("checkout now", "trade_rejected"),
        ("非法下载完整包", "illegal_download_rejected"),
        ("联系卖家并索要样品", "contact_seller_rejected"),
        ("contact the seller", "contact_seller_rejected"),
    ],
)
def test_goal_text_cannot_ask_for_a_forbidden_action(no_browser: None, goal: str, code: str) -> None:
    audit: list[dict[str, object]] = []
    with pytest.raises(ThreatBrowserError) as rejected:
        visit_threat_page(_request(goal=goal), audit=audit, resolve=_resolve)
    assert rejected.value.code == code


def test_tor_and_onion_stay_off_until_the_owner_confirms(monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    audit: list[dict[str, object]] = []
    with pytest.raises(ThreatBrowserError) as disabled:
        visit_threat_page(_request(switches=Switches(), deployed=Switches()), audit=audit, resolve=_resolve)
    assert disabled.value.code == "browser_disabled"
    unconfirmed = Switches(browser_enabled=True, tor_enabled=True, onion_enabled=False, owner_confirmed=False)
    _deploy(monkeypatch, unconfirmed)
    with pytest.raises(ThreatBrowserError) as tor_off:
        visit_threat_page(_request(switches=unconfirmed, deployed=unconfirmed), audit=audit, resolve=_resolve)
    assert tor_off.value.code == "tor_unconfirmed"
    _deploy(monkeypatch, BROWSER)
    asked = Switches(browser_enabled=True, tor_enabled=True, onion_enabled=False, owner_confirmed=True)
    lied = Switches(browser_enabled=True, tor_enabled=True, onion_enabled=True, owner_confirmed=True)
    with pytest.raises(ThreatBrowserError) as not_deployed:
        visit_threat_page(_request(switches=asked, deployed=lied), audit=audit, resolve=_resolve)
    assert not_deployed.value.code == "tor_disabled"


def test_onion_is_rejected_without_clearnet_dns(no_browser: None) -> None:
    calls: list[str] = []

    def resolve(host: str):
        calls.append(host)
        return [ipaddress.ip_address("93.184.216.34")]

    request = _request(url="http://example.onion/page")
    with pytest.raises(ThreatBrowserError) as rejected:
        authorize_target(request.url, switches=request.switches, resolve=resolve, allow_hosts=None)
    assert rejected.value.code == "tor_disabled"
    confirmed = Switches(browser_enabled=True, tor_enabled=True, onion_enabled=False, owner_confirmed=True)
    hidden = _request(url="http://example.onion/page", switches=confirmed, deployed=confirmed)
    with pytest.raises(ThreatBrowserError) as onion:
        authorize_target(hidden.url, switches=hidden.switches, resolve=resolve, allow_hosts=None)
    assert onion.value.code == "onion_disabled"
    assert calls == []


def test_metadata_proxy_is_rejected(monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    _deploy(monkeypatch, TOR)
    audit: list[dict[str, object]] = []
    request = _request(switches=TOR, deployed=TOR, fixture_proxy="socks5://169.254.169.254:9050")
    with pytest.raises(ThreatBrowserError) as rejected:
        visit_threat_page(request, audit=audit, resolve=_resolve)
    assert rejected.value.code == "proxy_rejected"


def test_approval_is_required_and_bound_to_the_url(monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    _deploy(monkeypatch, BROWSER)
    audit: list[dict[str, object]] = []
    with pytest.raises(ThreatBrowserError) as missing:
        visit_threat_page(_request(), audit=audit, resolve=_resolve)
    assert missing.value.code == "approval_required"
    stamped = _request()
    stamped = stamped.__class__(**{**stamped.__dict__, "approval": OwnerApproval("owner", "approved", intent_digest(stamped))})
    with pytest.raises(ThreatBrowserError) as unverified:
        visit_threat_page(stamped, audit=audit, resolve=_resolve)
    assert unverified.value.code == "verifier_required"
    original = _request()
    expected = intent_digest(original)

    def mismatch(_request: VisitRequest, digest: str) -> str:
        if digest != expected:
            raise ThreatBrowserError("approval_mismatch")
        return "owner"

    swapped = original.__class__(**{**original.__dict__, "url": "https://example.com/other"})
    with pytest.raises(ThreatBrowserError) as rejected:
        visit_threat_page(swapped, audit=audit, resolve=_resolve, authorize=mismatch)
    assert rejected.value.code == "approval_mismatch"
    with pytest.raises(ThreatBrowserError) as forged:
        VerifierPermit("a" * 64, object())
    assert forged.value.code == "verifier_required"
    assert audit[0]["previous_hash"] is None
    assert audit[1]["previous_hash"] == audit[0]["event_hash"]


def _allow(_request: VisitRequest, _digest: str) -> str:
    return "owner"


def test_container_plan_is_digest_pinned_and_does_not_fall_back(monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    _deploy(monkeypatch, BROWSER)
    recorded: list[list[str]] = []

    def runner(args: list[str], _stdin: bytes) -> bytes:
        recorded.append(args)
        if ENTRYPOINT in args:
            body = {
                "text": "可见正文",
                "browser_version": CHROMIUM_BROWSER_VERSION,
                "playwright_version": PLAYWRIGHT_VERSION,
                "user_agent": "HeadlessChrome",
            }
            return json.dumps(body).encode()
        return b""

    request = _request(runtime="container", browser_image=BUILT_BROWSER, tor_image=BUILT_TOR)
    audit: list[dict[str, object]] = []
    document = visit_threat_page(request, audit=audit, resolve=_resolve, runner=runner, cleanup=runner, authorize=_allow)
    assert document.text == "可见正文"
    assert document.admissible_evidence is False
    assert document.playwright_version == PLAYWRIGHT_VERSION
    browser = next(command for command in recorded if ENTRYPOINT in command)
    assert all(BUILT_TOR not in command for command in recorded)
    assert "--rm" in browser and "--read-only" in browser and BUILT_BROWSER in browser
    assert PLAYWRIGHT_IMAGE not in browser
    assert browser[browser.index("--cap-drop") + 1] == "ALL"
    assert "AETHER_BROWSER_NO_SANDBOX=1" in browser
    assert f"ALL_PROXY={APPROVED_TOR_PROXY}" not in browser
    assert browser[browser.index("--network") + 1].endswith("-egress")
    assert not any(part.endswith("-link") for part in browser)
    assert not any(command[:2] == ["network", "connect"] for command in recorded)
    assert not any(command[:1] == ["exec"] for command in recorded)
    assert "-p" not in browser and "--publish" not in browser
    assert "docker.sock" not in " ".join(browser)
    assert "--privileged" not in browser
    proxied = browser_command("job", image=BUILT_BROWSER, use_tor_proxy=True)
    assert f"ALL_PROXY={APPROVED_TOR_PROXY}" in proxied
    clear = setup_commands("job", tor_image=BUILT_TOR, fixture=False, tor_enabled=False)
    assert clear == [["network", "create", "job-egress"]]
    fixture = next(command for command in setup_commands("job", tor_image=BUILT_TOR, fixture=True) if BUILT_TOR in command)
    assert "AETHER_TOR_DISABLE_NETWORK=1" in fixture and "AETHER_TOR_FIXTURE=1" in fixture
    assert audit[-1]["decision"] == "completed"
    bad = dict(text="x", browser_version="0", playwright_version=PLAYWRIGHT_VERSION, user_agent="x")

    def wrong(_args: list[str], _stdin: bytes) -> bytes:
        if ENTRYPOINT in _args:
            return json.dumps(bad).encode()
        return b""

    with pytest.raises(ThreatBrowserError) as mismatch:
        visit_threat_page(request, audit=audit, resolve=_resolve, runner=wrong, cleanup=wrong, authorize=_allow)
    assert mismatch.value.code == "browser_pin_mismatch"
    _deploy(monkeypatch, TOR)
    recorded.clear()
    tor_request = _request(runtime="container", browser_image=BUILT_BROWSER, tor_image=BUILT_TOR, switches=TOR, deployed=TOR)
    visit_threat_page(tor_request, audit=audit, resolve=_resolve, runner=runner, cleanup=runner, authorize=_allow)
    tor = next(command for command in recorded if BUILT_TOR in command)
    tor_browser = next(command for command in recorded if ENTRYPOINT in command)
    assert "AETHER_TOR_DISABLE_NETWORK=1" not in tor
    assert tor_browser[tor_browser.index("--network") + 1].endswith("-link")
    assert not any(part.endswith("-egress") for part in tor_browser)
    assert TOR_ALIAS in tor
    assert "-p" not in tor and "--publish" not in tor
    probes = [command for command in recorded if command[:1] == ["exec"]]
    browser_at = next(index for index, command in enumerate(recorded) if ENTRYPOINT in command)
    assert probes and recorded.index(probes[0]) < browser_at
    assert "9050" in probes[-1][-1]
    assert "AETHER_TOR_SOCKS_ADDRESS" in probes[-1][-1]
    assert "127.0.0.1" not in probes[-1][-1]
    monkeypatch.setenv(FIXTURE_ENV, "1")
    _deploy(monkeypatch, BROWSER)
    fixture_request = _request(runtime="container", browser_image=BUILT_BROWSER, tor_image=BUILT_TOR, fixture=True)
    recorded.clear()
    visit_threat_page(fixture_request, audit=audit, resolve=_resolve, runner=runner, cleanup=runner, authorize=_allow)
    fixture_tor = next(command for command in recorded if BUILT_TOR in command)
    fixture_browser = next(command for command in recorded if ENTRYPOINT in command)
    assert "AETHER_TOR_DISABLE_NETWORK=1" in fixture_tor
    assert "AETHER_INGEST_ALLOW_FIXTURES=1" in fixture_browser
    fixture_probe = next(command for command in recorded if command[:1] == ["exec"])
    assert "8080" in fixture_probe[-1]
    assert "9050" not in fixture_probe[-1]
    assert "AETHER_TOR_SOCKS_ADDRESS" in fixture_probe[-1]


def test_socks_binds_only_on_the_internal_address() -> None:
    subnet, gateway, address = link_binding("aether-tb-abcdef01")
    network = ipaddress.ip_network(subnet)
    assert network.prefixlen == 28
    assert network.subnet_of(ipaddress.ip_network("10.240.0.0/16"))
    assert ipaddress.ip_address(address) in network
    assert ipaddress.ip_address(gateway) in network
    assert address != gateway
    assert link_binding("aether-tb-abcdef01") == (subnet, gateway, address)
    commands = setup_commands("aether-tb-abcdef01", tor_image=BUILT_TOR, tor_enabled=True)
    created = commands[0]
    assert created[:4] == ["network", "create", "--internal", "--subnet"]
    assert subnet in created and gateway in created and created[-1].endswith("-link")
    tor = next(command for command in commands if BUILT_TOR in command)
    assert tor[tor.index("--ip") + 1] == address
    assert f"AETHER_TOR_SOCKS_ADDRESS={address}" in tor
    assert f"AETHER_TOR_SOCKS_POLICY={subnet}" in tor
    assert "0.0.0.0" not in " ".join(" ".join(command) for command in commands)
    root = Path(__file__).resolve().parents[2]
    torrc = (root / "infra/tor/torrc").read_text(encoding="utf-8")
    entry = (root / "infra/tor/entrypoint.sh").read_text(encoding="utf-8")
    assert "SocksPort" not in torrc
    assert "accept *" not in torrc
    assert "0.0.0.0" not in torrc
    assert "SocksPolicy reject *" in entry
    assert "10.240.0.0/16" in entry
    with pytest.raises(ThreatLaunchError) as rejected:
        link_binding("../escape")
    assert rejected.value.code == "container_name_rejected"


def test_browser_waits_until_the_tor_proxy_accepts(monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    _deploy(monkeypatch, TOR)
    attempts = {"exec": 0}

    def runner(args: list[str], _stdin: bytes) -> bytes:
        if args[:1] == ["exec"]:
            attempts["exec"] += 1
            if attempts["exec"] < 3:
                raise ThreatLaunchError("container_unavailable")
            return b""
        if ENTRYPOINT in args:
            body = {
                "text": "可见正文",
                "browser_version": CHROMIUM_BROWSER_VERSION,
                "playwright_version": PLAYWRIGHT_VERSION,
                "user_agent": "HeadlessChrome",
            }
            return json.dumps(body).encode()
        return b""

    request = _request(runtime="container", browser_image=BUILT_BROWSER, tor_image=BUILT_TOR, switches=TOR, deployed=TOR)
    visit_threat_page(request, audit=[], resolve=_resolve, runner=runner, cleanup=runner, authorize=_allow)
    assert attempts["exec"] == 3


def test_child_frame_documents_are_not_fetched() -> None:
    gate = _Gate(switches=BROWSER, allow_hosts=None, resolve=_resolve, proxy=None, first_address="93.184.216.34")

    class _Frame:
        parent_frame = object()

    class _Request:
        resource_type = "document"
        method = "GET"
        url = "https://example.com/frame"
        frame = _Frame()

    class _Route:
        def __init__(self) -> None:
            self.aborted = False
            self.request = _Request()

        def abort(self) -> None:
            self.aborted = True

        def fulfill(self, **_kwargs: object) -> None:
            raise AssertionError("child frame was fetched")

    route = _Route()
    gate.intercept(route)
    assert route.aborted is True
    assert gate.hops == 0


def test_entrypoint_delegates_to_the_playwright_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_read_page(url: str, **kwargs: object) -> PageCapture:
        seen["url"] = url
        seen["proxy"] = kwargs["proxy"]
        return PageCapture("可见正文", CHROMIUM_BROWSER_VERSION, PLAYWRIGHT_VERSION, "HeadlessChrome")

    monkeypatch.setattr(threat_browser_entry, "read_page", fake_read_page)
    _deploy(monkeypatch, BROWSER)
    result = run_entry({"url": PUBLIC, "browser_enabled": True, "tor_enabled": False, "proxy": None})
    assert result["text"] == "可见正文"
    assert seen == {"url": PUBLIC, "proxy": None}
    source = Path(threat_browser.__file__).read_text(encoding="utf-8")
    entry_source = Path(threat_browser_entry.__file__).read_text(encoding="utf-8")
    assert "html.parser" not in source and "html.parser" not in entry_source
    assert "aether_worker.ingest" not in source and "aether_worker.ingest" not in entry_source
    _deploy(monkeypatch, TOR)
    with pytest.raises(ThreatBrowserError) as rejected:
        run_entry(
            {
                "url": PUBLIC,
                "browser_enabled": True,
                "tor_enabled": True,
                "onion_enabled": True,
                "owner_confirmed": True,
                "proxy": "socks5://169.254.169.254:9050",
            }
        )
    assert rejected.value.code == "proxy_rejected"


def test_entrypoint_ignores_allow_hosts_without_the_fixture_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FIXTURE_ENV, raising=False)
    seen: dict[str, object] = {}

    def fake_read_page(_url: str, **kwargs: object) -> PageCapture:
        seen["allow_hosts"] = kwargs["allow_hosts"]
        return PageCapture("可见正文", CHROMIUM_BROWSER_VERSION, PLAYWRIGHT_VERSION, "HeadlessChrome")

    monkeypatch.setattr(threat_browser_entry, "read_page", fake_read_page)
    _deploy(monkeypatch, BROWSER)
    payload = {"url": PUBLIC, "browser_enabled": True, "tor_enabled": False, "proxy": None, "allow_hosts": ["169.254.169.254"]}
    run_entry(payload)
    assert seen["allow_hosts"] is None
    monkeypatch.setenv(FIXTURE_ENV, "1")
    run_entry({**payload, "allow_hosts": ["aether-tor"]})
    assert seen["allow_hosts"] == frozenset({"aether-tor"})


def test_container_rejects_an_unpinned_image(monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    _deploy(monkeypatch, BROWSER)

    def runner(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("docker called")

    request = _request(runtime="container", browser_image="debian:trixie", tor_image=BUILT_TOR)
    with pytest.raises(ThreatBrowserError) as rejected:
        visit_threat_page(request, audit=[], resolve=_resolve, runner=runner, cleanup=runner, authorize=_allow)
    assert rejected.value.code == "image_unpinned"


def test_manual_approval_cannot_skip_the_verifier(monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    _deploy(monkeypatch, BROWSER)
    request = _request()
    digest = intent_digest(request)
    stamped = OwnerApproval(owner_id="owner", decision="approved", intent_digest=digest)
    audit: list[dict[str, object]] = []
    with pytest.raises(ThreatBrowserError) as rejected:
        visit_threat_page(request.__class__(**{**request.__dict__, "approval": stamped}), audit=audit, resolve=_resolve)
    assert rejected.value.code == "verifier_required"


def test_playwright_runtime_is_refused_without_the_fixture_switch(monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    _deploy(monkeypatch, BROWSER)
    monkeypatch.delenv(FIXTURE_ENV, raising=False)
    with pytest.raises(ThreatBrowserError) as rejected:
        visit_threat_page(_request(runtime="playwright"), audit=[], resolve=_resolve, authorize=_allow)
    assert rejected.value.code == "runtime_rejected"


def _load_fixture_server() -> ModuleType:
    path = Path("infra/tor/fixture_server.py")
    spec = importlib.util.spec_from_file_location("aether_tor_fixture", path)
    if spec is None or spec.loader is None:
        raise AssertionError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_ca_is_loaded_as_pem_text(monkeypatch: pytest.MonkeyPatch) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aether-tor")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("aether-tor")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    with TemporaryDirectory() as directory:
        root = Path(directory)
        cert_path = root / "cert.pem"
        key_path = root / "key.pem"
        cert_path.write_bytes(pem)
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        fixture = _load_fixture_server()
        fixture._HITS_PATH = root / "hits.json"
        context, listener = fixture._listener("127.0.0.1", 0, cert_path, key_path)
        assert listener.getsockname()[0] == "127.0.0.1"
        port = int(listener.getsockname()[1])

        def serve() -> None:
            while True:
                connection, _address = listener.accept()
                fixture._respond(context, connection)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with socket.create_connection(("127.0.0.1", port), 2):
            pass
        monkeypatch.setenv("AETHER_FIXTURE_CA", base64.b64encode(pem).decode("ascii"))
        try:
            fetched = _fetch_until_ready(port)
        finally:
            thread.join(timeout=0.2)
            listener.close()
    assert b"visible-text" in fetched.body()
    assert b"noscript-text" in fetched.body()


def _fetch_until_ready(port: int) -> _Fetched:
    deadline = time.monotonic() + 2
    last: ThreatBrowserError | None = None
    while time.monotonic() < deadline:
        try:
            return _pinned_get(f"https://aether-tor:{port}/page", "127.0.0.1", None)
        except ThreatBrowserError as exc:
            if not isinstance(exc.__cause__, ConnectionRefusedError):
                raise
            last = exc
            time.sleep(0.02)
    raise AssertionError(last)


def test_worker_does_not_open_docker(monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    _deploy(monkeypatch, BROWSER)
    monkeypatch.setenv("AETHER_PROCESS_ROLE", "worker")
    monkeypatch.setenv("AETHER_VERIFIER_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("AETHER_LOOPBACK_TOKEN", "token")
    monkeypatch.setenv("AETHER_RUNNER_BASE", "http://127.0.0.1:9")
    monkeypatch.setattr(threat_visit, "worker_authorize", lambda *_args: "owner")
    seen: dict[str, object] = {}

    def remote(payload: dict[str, object], *, browser_image: str, tor_image: str, fixture: bool) -> dict[str, object]:
        seen["image"] = browser_image
        seen["tor"] = tor_image
        seen["fixture"] = fixture
        seen["proxy"] = payload["proxy"]
        return {
            "text": "可见正文",
            "browser_version": CHROMIUM_BROWSER_VERSION,
            "playwright_version": PLAYWRIGHT_VERSION,
            "user_agent": "HeadlessChrome",
        }

    monkeypatch.setattr(threat_visit, "remote_execute", remote)

    def docker(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("docker called")

    def injected(_request: VisitRequest, _digest: str) -> str:
        raise AssertionError("worker used the injected authorize")

    request = _request(runtime="container", browser_image=BUILT_BROWSER, tor_image=BUILT_TOR)
    document = visit_threat_page(request, audit=[], resolve=_resolve, runner=docker, cleanup=docker, authorize=injected)
    assert document.text == "可见正文"
    assert seen == {"image": BUILT_BROWSER, "tor": BUILT_TOR, "fixture": False, "proxy": None}
    with pytest.raises(ThreatLaunchError) as rejected:
        docker_runner(["info"], b"")
    assert rejected.value.code == "docker_forbidden"


def test_runner_only_runs_its_configured_images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AETHER_THREAT_BROWSER_IMAGE", raising=False)
    monkeypatch.delenv("AETHER_TOR_IMAGE", raising=False)
    monkeypatch.delenv(FIXTURE_ENV, raising=False)
    body = {
        "payload": {"url": "https://example.com/note", "browser_enabled": True, "proxy": None},
        "browser_image": BUILT_BROWSER,
        "tor_image": BUILT_TOR,
    }
    status, rejected = runner_process._threat_browser(body)
    assert status == 200
    assert rejected == {"error": "image_unpinned"}
    monkeypatch.setenv("AETHER_THREAT_BROWSER_IMAGE", BUILT_BROWSER)
    monkeypatch.setenv("AETHER_TOR_IMAGE", BUILT_TOR)
    seen: dict[str, object] = {}

    def execute(payload: dict[str, object], **kwargs: object) -> dict[str, object]:
        seen["payload"] = payload
        seen["fixture"] = kwargs["fixture"]
        seen["browser"] = kwargs["browser_image"]
        return {"text": "ok"}

    monkeypatch.setattr(runner_process, "execute_container", execute)
    status, accepted = runner_process._threat_browser(body)
    assert status == 200
    assert accepted == {"text": "ok"}
    assert seen["browser"] == BUILT_BROWSER
    assert seen["fixture"] is False
    refused, fixture_body = runner_process._threat_browser({**body, "fixture": True})
    assert refused == 200
    assert fixture_body == {"error": "fixture_hosts_rejected"}
    monkeypatch.setenv(FIXTURE_ENV, "1")
    runner_process._threat_browser({**body, "fixture": True})
    assert seen["fixture"] is True
