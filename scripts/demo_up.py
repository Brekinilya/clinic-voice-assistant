"""Start the local offline demo stack with Docker Compose."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DEMO_VALUES = {
    "APP_MODE": "offline_test",
    "BACKEND_DEMO_ENABLED": "true",
    "DIALOGUE_ENABLED": "false",
    "DIALOGUE_STORAGE": "memory",
    "DIALOGUE_MODE": "natural",
    "WEB_ACCESS_ENABLED": "true",
    "TELEPHONY_ENABLED": "false",
}


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("\\'", "'")
        values[key.strip()] = value
    return values


def run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=180, help="readiness timeout in seconds")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if shutil.which("docker") is None:
        parser.error("Docker Desktop / Docker Compose is required; start Docker and retry")

    env_path = ROOT / ".env"
    if not env_path.exists():
        run(sys.executable, "deploy/vm/configure_env.py", "--mode", "offline_test")
    env_values = read_env(env_path)
    for key, expected in REQUIRED_DEMO_VALUES.items():
        if env_values.get(key, "").lower() != expected:
            parser.error(
                f"existing .env is not an offline demo configuration ({key}); it was left "
                "unchanged. Back it up and create a demo .env with: "
                "python deploy/vm/configure_env.py --mode offline_test"
            )

    try:
        run("docker", "compose", "version")
        run("docker", "compose", "build", "app")
        run("docker", "compose", "up", "-d", "database")
        run("docker", "compose", "run", "--rm", "app", "python", "-m", "app.backend", "migrate")
        run("docker", "compose", "up", "-d", "app")
    except subprocess.CalledProcessError as error:
        print(f"Startup step failed with exit code {error.returncode}.", file=sys.stderr)
        return error.returncode or 1

    deadline = time.monotonic() + args.timeout
    health_url = "http://127.0.0.1:8000/health/ready"
    while time.monotonic() < deadline:
        try:
            with urlopen(health_url, timeout=3) as response:
                if response.status == 200:
                    print("Demo is ready: http://127.0.0.1:8000/chat")
                    print(
                        f"Login: {env_values.get('WEB_ACCESS_USER', 'clinic')}; "
                        "password is stored in .env."
                    )
                    return 0
        except (HTTPError, URLError, TimeoutError, OSError):
            time.sleep(2)

    print(
        f"App did not become ready within {args.timeout} seconds. "
        "Inspect with: docker compose logs app database",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
