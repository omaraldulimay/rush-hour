"""Run the whole shop on this machine without Docker.

    python -m scripts.local.stack up | stop | status | reset

Postgres (own throwaway cluster), Redis, Prometheus, the five services and the Control Room all run
as plain processes. State lives in .local/ (pid files, logs, data, downloaded binaries); nothing
outside the repository is touched and the system Postgres on 5432 is never used.
"""

import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
BIN, LOGS, RUN = LOCAL / "bin", LOCAL / "logs", LOCAL / "run"
PG_DATA, REDIS_DIR, PROM_DIR = LOCAL / "pg", LOCAL / "redis", LOCAL / "prom"
PG_BIN = Path(os.environ.get("PG_BIN", "/Library/PostgreSQL/16/bin"))

PG_PORT, REDIS_PORT, PROM_PORT = 55432, 56379, 9090
AM_PORT = int(os.environ.get("ALERTMANAGER_PORT", "9093"))
GRAFANA_PORT = int(os.environ.get("GRAFANA_PORT", "3000"))
PROMETHEUS_VERSION = "3.15.1"  # latest stable at the time of writing (gh api .../releases/latest)
# Pinned downloads (darwin-amd64): version + SHA-256 of the tarball, committed here on purpose.
ALERTMANAGER_VERSION = "0.34.2"
ALERTMANAGER_SHA256 = "fa5a50303c58771c261fcbed4cf5f9a852cf2c6064942adbd92c6859c65c5f93"
GRAFANA_VERSION = "13.3.0"
GRAFANA_SHA256 = "8bc7537851e84758fbf5ea545db4eac22f943412652df4e83062077362528579"
OBS = ROOT / "observability"
AM_DIR, GRAFANA_DIR, GRAFANA_DATA = (
    LOCAL / "alertmanager",
    LOCAL / "grafana",
    LOCAL / "grafana-data",
)
PROM_CONFIG = ROOT / "observability/prometheus/prometheus.local.yml"
DATABASE_URL = f"postgresql://rushhour:rushhour@127.0.0.1:{PG_PORT}/rushhour"
REDIS_URL = f"redis://127.0.0.1:{REDIS_PORT}/0"

# name -> (module, port). Same ports as docker compose.
SERVICES = {
    "catalog": ("catalog", 8001),
    "inventory": ("inventory", 8002),
    "checkout": ("checkout", 8003),
    "payment-mock": ("payment_mock", 8004),
    "order-worker": ("order_worker", 8005),
    "control-room": ("control_room", 8080),
}
SERVICE_ENV = {
    "DATABASE_URL": DATABASE_URL,
    "REDIS_URL": REDIS_URL,
    "CATALOG_URL": "http://127.0.0.1:8001",
    "INVENTORY_URL": "http://127.0.0.1:8002",
    "CHECKOUT_URL": "http://127.0.0.1:8003",
    "PAYMENT_URL": "http://127.0.0.1:8004",
    "PROMETHEUS_URL": f"http://127.0.0.1:{PROM_PORT}",
    "PAYMENT_LATENCY_MS": "0",
    "PAYMENT_ERROR_RATE": "0",
    "PAYMENT_DECLINE_RATE": "0.02",
}


def say(msg: str) -> None:
    print(f"[local] {msg}", flush=True)


def die(msg: str) -> "None":
    print(f"[local] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# --- process helpers ---------------------------------------------------------------------------
def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pid_file(name: str) -> Path:
    return RUN / f"{name}.pid"


def read_pid(name: str) -> int | None:
    try:
        pid = int(pid_file(name).read_text())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None
    return pid


def command_of(pid: int) -> str:
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return out.stdout.strip()


@dataclass
class Proc:
    name: str
    port: int
    marker: str  # a word that must be in the command line of the process we started

    def alive(self) -> int | None:
        pid = read_pid(self.name)
        return pid if pid and self.marker in command_of(pid) else None

    def start(self, argv: list[str], env: dict[str, str] | None = None) -> None:
        if self.alive():
            return
        if port_open(self.port):
            die(f"port {self.port} ({self.name}) is already used by another process")
        LOGS.mkdir(parents=True, exist_ok=True)
        RUN.mkdir(parents=True, exist_ok=True)
        log = open(LOGS / f"{self.name}.log", "ab")  # noqa: SIM115 - handed to the child
        proc = subprocess.Popen(
            argv,
            cwd=ROOT,
            env={**os.environ, **(env or {})},
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file(self.name).write_text(str(proc.pid))
        say(f"started {self.name} (pid {proc.pid}, port {self.port})")

    def stop(self) -> None:
        pid = self.alive()
        if pid:
            os.killpg(pid, signal.SIGTERM) if _is_group_leader(pid) else os.kill(
                pid, signal.SIGTERM
            )
            for _ in range(100):
                if not _exists(pid):
                    break
                time.sleep(0.1)
            else:
                say(f"{self.name} did not stop, killing it")
                os.kill(pid, signal.SIGKILL)
            say(f"stopped {self.name}")
        pid_file(self.name).unlink(missing_ok=True)


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie that nobody reaped counts as gone
    return (
        "Z"
        not in subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
        ).stdout
    )


def _is_group_leader(pid: int) -> bool:
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


# --- components --------------------------------------------------------------------------------
def pg_run(tool: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(PG_BIN / tool), *args], capture_output=True, text=True)


def pg_running() -> bool:
    return (PG_DATA / "postmaster.pid").exists() and pg_run(
        "pg_ctl", "-D", str(PG_DATA), "status"
    ).returncode == 0


def start_postgres() -> None:
    if not (PG_BIN / "initdb").exists():
        die(f"Postgres binaries not found in {PG_BIN} (set PG_BIN)")
    LOGS.mkdir(parents=True, exist_ok=True)
    if not (PG_DATA / "PG_VERSION").exists():
        say("creating the Postgres cluster in .local/pg")
        out = pg_run(
            "initdb",
            "-D",
            str(PG_DATA),
            "-U",
            "rushhour",
            "--auth=trust",
            "-E",
            "UTF8",
            "--no-locale",
        )
        if out.returncode:
            die(f"initdb failed: {out.stderr}")
    if pg_running():
        return
    if port_open(PG_PORT):
        die(f"port {PG_PORT} is already used by another process")
    options = (
        f"-p {PG_PORT} -c listen_addresses=127.0.0.1 -c unix_socket_directories= "
        "-c shared_buffers=64MB -c max_connections=100 -c synchronous_commit=off"
    )
    out = pg_run(
        "pg_ctl", "-D", str(PG_DATA), "-o", options, "-l", str(LOGS / "postgres.log"), "-w", "start"
    )
    if out.returncode:
        die(f"Postgres did not start, see .local/logs/postgres.log\n{out.stderr}")
    say(f"started postgres (port {PG_PORT})")
    exists = pg_run(
        "psql",
        "-h",
        "127.0.0.1",
        "-p",
        str(PG_PORT),
        "-U",
        "rushhour",
        "-d",
        "postgres",
        "-tAc",
        "SELECT 1 FROM pg_database WHERE datname='rushhour'",
    )
    if exists.stdout.strip() != "1":
        out = pg_run(
            "createdb", "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", "rushhour", "rushhour"
        )
        if out.returncode:
            die(f"createdb failed: {out.stderr}")


def stop_postgres() -> None:
    if (PG_DATA / "postmaster.pid").exists():
        pg_run("pg_ctl", "-D", str(PG_DATA), "-m", "fast", "-w", "stop")
        say("stopped postgres")


def find_redis() -> str:
    for name in ("redis-server", "valkey-server"):
        found = os.environ.get("REDIS_SERVER") if name == "redis-server" else None
        found = (
            found
            or shutil.which(name)
            or next(
                (
                    p
                    for p in (f"/usr/local/bin/{name}", f"/opt/homebrew/bin/{name}")
                    if os.access(p, os.X_OK)
                ),
                None,
            )
        )
        if found:
            return found
    # Homebrew's `redis` formula needs llvm and rust and does not build on an Intel Mac without
    # bottles; `valkey` is the Redis-compatible fork with a plain C build.
    formula = os.environ.get("REDIS_FORMULA", "valkey")
    say(
        f"no redis-server found: brew install {formula} (compiles from source, takes a few minutes)"
    )
    if subprocess.run(["brew", "install", formula]).returncode:
        die(f"brew install {formula} failed; install redis-server yourself or set REDIS_SERVER")
    return find_redis()


def start_redis() -> None:
    REDIS_DIR.mkdir(parents=True, exist_ok=True)
    server = find_redis()
    Proc("redis", REDIS_PORT, Path(server).name).start(
        [server, "--bind", "127.0.0.1", "--port", str(REDIS_PORT), "--dir", str(REDIS_DIR),
         "--save", "60", "1", "--daemonize", "no", "--maxmemory", "128mb"]
    )  # fmt: skip


def prometheus_binary() -> Path:
    target = BIN / "prometheus"
    if target.exists():
        return target
    base = f"https://github.com/prometheus/prometheus/releases/download/v{PROMETHEUS_VERSION}"
    name = f"prometheus-{PROMETHEUS_VERSION}.darwin-amd64.tar.gz"
    say(f"downloading Prometheus {PROMETHEUS_VERSION} (darwin-amd64)")
    BIN.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=LOCAL) as tmp:
        archive = Path(tmp) / name
        urllib.request.urlretrieve(f"{base}/{name}", archive)
        sums = urllib.request.urlopen(f"{base}/sha256sums.txt", timeout=30).read().decode()
        expected = next((ln.split()[0] for ln in sums.splitlines() if ln.endswith(name)), None)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if expected != actual:
            die(f"checksum mismatch for {name}: expected {expected}, got {actual}")
        say("checksum verified against the release's sha256sums.txt")
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if Path(member.name).name in ("prometheus", "promtool") and member.isfile():
                    member.name = Path(member.name).name
                    tar.extract(member, BIN, filter="data")
    target.chmod(0o755)
    return target


def start_prometheus() -> None:
    PROM_DIR.mkdir(parents=True, exist_ok=True)
    Proc("prometheus", PROM_PORT, "prometheus").start(
        [str(prometheus_binary()), f"--config.file={PROM_CONFIG}",
         f"--storage.tsdb.path={PROM_DIR}", "--storage.tsdb.retention.time=2h",
         f"--web.listen-address=127.0.0.1:{PROM_PORT}"]
    )  # fmt: skip


def download_verified(url: str, sha256: str, dest: Path) -> None:
    """Download `url` to `dest` and refuse it unless it has the SHA-256 that is pinned here."""
    say(f"downloading {url.rsplit('/', 1)[-1]}")
    urllib.request.urlretrieve(url, dest)
    digest = hashlib.sha256()
    with open(dest, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        dest.unlink(missing_ok=True)
        die(f"checksum mismatch for {dest.name}: expected {sha256}, got {digest.hexdigest()}")
    say("checksum verified against the pinned SHA-256")


def alertmanager_binary() -> Path:
    target = BIN / "alertmanager"
    if target.exists():
        return target
    name = f"alertmanager-{ALERTMANAGER_VERSION}.darwin-amd64.tar.gz"
    base = f"https://github.com/prometheus/alertmanager/releases/download/v{ALERTMANAGER_VERSION}"
    BIN.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=LOCAL) as tmp:
        archive = Path(tmp) / name
        download_verified(f"{base}/{name}", ALERTMANAGER_SHA256, archive)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if Path(member.name).name in ("alertmanager", "amtool") and member.isfile():
                    member.name = Path(member.name).name
                    tar.extract(member, BIN, filter="data")
    target.chmod(0o755)
    return target


def grafana_home() -> Path:
    home = GRAFANA_DIR / f"grafana-{GRAFANA_VERSION}"
    if (home / "bin" / "grafana").exists():
        return home
    name = f"grafana-{GRAFANA_VERSION}.darwin-amd64.tar.gz"
    GRAFANA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=LOCAL) as tmp:
        archive = Path(tmp) / name
        download_verified(f"https://dl.grafana.com/oss/release/{name}", GRAFANA_SHA256, archive)
        say("unpacking Grafana (about 1 GB, once)")
        with tarfile.open(archive) as tar:
            tar.extractall(GRAFANA_DIR, filter="data")
    return home


def warn_port(proc: Proc) -> bool:
    say(f"WARNING: port {proc.port} is taken, {proc.name} not started (set its *_PORT variable)")
    return False


def start_optional(proc: Proc, argv: list[str], env: dict[str, str] | None = None) -> bool:
    """Extras: a foreign process on their port must not stop `make local`."""
    if not proc.alive() and port_open(proc.port):
        return warn_port(proc)
    proc.start(argv, env)
    return True


def start_alertmanager() -> None:
    AM_DIR.mkdir(parents=True, exist_ok=True)
    start_optional(
        Proc("alertmanager", AM_PORT, "alertmanager"),
        [str(alertmanager_binary()), f"--config.file={OBS / 'alertmanager/alertmanager.yml'}",
         f"--storage.path={AM_DIR}", f"--web.listen-address=127.0.0.1:{AM_PORT}",
         "--cluster.listen-address="],
    )  # fmt: skip


def start_grafana() -> None:
    proc = Proc("grafana", GRAFANA_PORT, "grafana")
    if not proc.alive() and port_open(GRAFANA_PORT):
        warn_port(proc)
        return
    home = grafana_home()
    GRAFANA_DATA.mkdir(parents=True, exist_ok=True)
    dashboards = OBS / "grafana/dashboards"
    env = {
        "GF_PATHS_DATA": str(GRAFANA_DATA),
        "GF_PATHS_LOGS": str(LOGS),
        "GF_PATHS_PLUGINS": str(GRAFANA_DATA / "plugins"),
        "GF_PATHS_PROVISIONING": str(OBS / "grafana/provisioning"),
        "GF_SERVER_HTTP_ADDR": "127.0.0.1",
        "GF_SERVER_HTTP_PORT": str(GRAFANA_PORT),
        # Anyone on this machine may look (Viewer); changing things needs admin / admin.
        "GF_AUTH_ANONYMOUS_ENABLED": "true",
        "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
        "GF_PLUGINS_PREINSTALL_DISABLED": "true",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH": str(dashboards / "shop-overview.json"),
        "PROMETHEUS_URL": f"http://127.0.0.1:{PROM_PORT}",
        "GRAFANA_DASHBOARDS_DIR": str(dashboards),
    }  # fmt: skip
    proc.start(
        [str(home / "bin" / "grafana"), "server", f"--homepath={home}"],
        env,
    )


def run_python(*args: str) -> None:
    env = {**os.environ, **SERVICE_ENV, "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True)
    if out.returncode:
        die(f"{' '.join(args)} failed:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}")


def start_service(name: str) -> None:
    module, port = SERVICES[name]
    Proc(name, port, "uvicorn").start(
        [sys.executable, "-m", "uvicorn", f"services.{module}.app.main:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--workers", "1", "--no-access-log"],
        env=SERVICE_ENV,
    )  # fmt: skip


def ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


# --- commands ----------------------------------------------------------------------------------
def urls() -> dict[str, str]:
    return {
        "Control Room": "http://localhost:8080",
        "Prometheus": f"http://localhost:{PROM_PORT}",
        "Grafana": f"http://localhost:{GRAFANA_PORT}",
        "Alertmanager": f"http://localhost:{AM_PORT}",
        "catalog": "http://localhost:8001/products?limit=3",
        "inventory": "http://localhost:8002/stock/SNK-0001",
        "checkout": "http://localhost:8003/carts/demo",
        "payment-mock": "http://localhost:8004/admin/fault",
        "order-worker": "http://localhost:8005/readyz",
    }


def up() -> None:
    if not (ROOT / ".venv").exists() and "VIRTUAL_ENV" not in os.environ:
        die("no .venv: run `make install` first")
    start_postgres()
    start_redis()
    for _ in range(50):
        if port_open(REDIS_PORT):
            break
        time.sleep(0.1)
    say("migrating and seeding")
    run_python("-m", "libs.common.migrate")
    run_python("-m", "scripts.seed")
    start_prometheus()
    start_alertmanager()
    start_grafana()
    for name in SERVICES:
        start_service(name)
    say("waiting for every /readyz ...")
    pending = {n: f"http://127.0.0.1:{p}/readyz" for n, (_, p) in SERVICES.items()}
    deadline = time.monotonic() + 120
    while pending and time.monotonic() < deadline:
        pending = {n: u for n, u in pending.items() if not ready(u)}
        time.sleep(0.5)
    if pending or not ready(f"http://127.0.0.1:{PROM_PORT}/-/ready"):
        die(f"not ready: {', '.join(pending) or 'prometheus'} - see .local/logs/")
    for name, port, path in (
        ("alertmanager", AM_PORT, "/-/ready"),
        ("grafana", GRAFANA_PORT, "/api/health"),
    ):
        if Proc(name, port, name).alive():
            for _ in range(120):
                if ready(f"http://127.0.0.1:{port}{path}"):
                    break
                time.sleep(0.5)
            else:
                say(f"WARNING: {name} is not ready yet, see .local/logs/{name}.log")
    print("\nEverything is up:")
    for label, url in urls().items():
        print(f"  {label:<13} {url}")
    print("\nOpen the Control Room, press 'Start rush hour'.  Stop everything: make local-stop")


def stop() -> None:
    for name in reversed(list(SERVICES)):
        Proc(name, SERVICES[name][1], "uvicorn").stop()
    Proc("grafana", GRAFANA_PORT, "grafana").stop()
    Proc("alertmanager", AM_PORT, "alertmanager").stop()
    Proc("prometheus", PROM_PORT, "prometheus").stop()
    for marker in ("redis-server", "valkey-server"):
        Proc("redis", REDIS_PORT, marker).stop()
    stop_postgres()
    shutil.rmtree(RUN, ignore_errors=True)
    say("all stopped")


def status() -> int:
    rows = [("postgres", PG_PORT, pg_running())]
    rows.append(("redis", REDIS_PORT, port_open(REDIS_PORT) and read_pid("redis") is not None))
    rows.append(
        ("prometheus", PROM_PORT, Proc("prometheus", PROM_PORT, "prometheus").alive() is not None)
    )
    rows.append(("alertmanager", AM_PORT, ready(f"http://127.0.0.1:{AM_PORT}/-/ready")))
    rows.append(("grafana", GRAFANA_PORT, ready(f"http://127.0.0.1:{GRAFANA_PORT}/api/health")))
    rows += [(n, p, ready(f"http://127.0.0.1:{p}/readyz")) for n, (_, p) in SERVICES.items()]
    for name, port, ok in rows:
        print(f"  {'up  ' if ok else 'DOWN'}  {name:<13} :{port}")
    return 0 if all(ok for *_, ok in rows) else 1


def reset() -> None:
    stop()
    for path in (PG_DATA, REDIS_DIR, PROM_DIR, AM_DIR, GRAFANA_DATA):
        shutil.rmtree(path, ignore_errors=True)
    say("data wiped (.local/bin and logs are kept)")


def observability() -> None:
    """Start only Alertmanager and Grafana (e.g. next to an already running stack)."""
    start_alertmanager()
    start_grafana()
    for label, url in list(urls().items())[2:4]:
        print(f"  {label:<13} {url}")


def main() -> None:
    commands = {"up": up, "stop": stop, "reset": reset, "observability": observability}
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        sys.exit(status())
    if cmd not in commands:
        die("usage: python -m scripts.local.stack up | stop | status | reset | observability")
    commands[cmd]()


if __name__ == "__main__":
    main()
