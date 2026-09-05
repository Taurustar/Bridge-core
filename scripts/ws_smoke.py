#!/usr/bin/env python3
"""WebSocket smoke script (plan section 31, milestone 1.0.0).

Exercises the minimal live protocol surface of a running Bridge Core
Engine and exits nonzero on any failure. Intended for deployment checks
and post-upgrade verification; it never sends a companion turn, so no
LLM provider is needed:

    python3 scripts/ws_smoke.py --host 100.x.y.z --port 8766 --user owner

Checks, in order:
  1. GET  /health answers 200 with redis ok.
  2. WS   connect as the owner -> `connected` frame with server_version
     and capabilities.
  3. WS   heartbeat (valid sequence + fresh last_input_at) ->
     `heartbeat_ack` with counted=true.
  4. WS   raw non-JSON frame -> `error` frame with code bad_json.
  5. WS   non-owner connect -> terminal error forbidden_user + close
     (only when a second user id is probed with --probe-foreign-user).

All frames are validated by small pure functions so the checks stay
unit-testable (see tests/test_release.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.constants import VERSION  # noqa: E402


# -- pure validators (unit-tested) --------------------------------------------


def verify_connected(frame: dict, expected_version: str = VERSION) -> list[str]:
    """Return a list of problems; empty means the frame is a valid connect."""
    problems: list[str] = []
    if frame.get("type") != "connected":
        problems.append(f"expected type=connected, got {frame.get('type')!r}")
    if not frame.get("connection_id", "").startswith("conn_"):
        problems.append("missing connection_id")
    if frame.get("server_version") != expected_version:
        problems.append(
            f"server_version {frame.get('server_version')!r} != {expected_version!r}"
        )
    capabilities = frame.get("capabilities")
    if not isinstance(capabilities, list) or "text" not in capabilities:
        problems.append("capabilities must be a list containing 'text'")
    if "heartbeat" not in (capabilities or []):
        problems.append("capabilities must contain 'heartbeat'")
    return problems


def verify_heartbeat_ack(frame: dict) -> list[str]:
    problems: list[str] = []
    if frame.get("type") != "heartbeat_ack":
        problems.append(f"expected type=heartbeat_ack, got {frame.get('type')!r}")
    if not frame.get("server_time"):
        problems.append("missing server_time")
    if "initiative_counter" not in frame:
        problems.append("missing initiative_counter")
    if frame.get("counted") is not True:
        problems.append("counted must be true for a fresh sequence")
    return problems


def verify_error(frame: dict, expected_code: str) -> list[str]:
    problems: list[str] = []
    if frame.get("type") != "error":
        problems.append(f"expected type=error, got {frame.get('type')!r}")
    error = frame.get("error") or {}
    if error.get("code") != expected_code:
        problems.append(
            f"expected error.code={expected_code!r}, got {error.get('code')!r}"
        )
    if not error.get("message"):
        problems.append("error.message must not be empty")
    return problems


# -- driver ---------------------------------------------------------------------


async def smoke(
    ws_url: str,
    http_url: str,
    owner: str,
    timeout: float,
    probe_foreign_user: bool,
) -> list[str]:
    """Run all checks; returns the list of problems (empty = pass)."""
    problems: list[str] = []

    def record(step: str, found: list[str]) -> None:
        if found:
            problems.extend(f"{step}: {problem}" for problem in found)
            print(f"FAIL {step}")
        else:
            print(f"ok   {step}")

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(f"{http_url}/health")
            health = response.json() if response.status_code == 200 else {}
            record(
                "GET /health",
                []
                if response.status_code == 200 and health.get("redis") is True
                else [f"status={response.status_code} body={health!r}"],
            )
        except Exception as exc:  # noqa: BLE001 - smoke reports, never raises
            record("GET /health", [type(exc).__name__])
            return problems

    try:
        async with websockets.connect(ws_url, open_timeout=timeout) as ws:
            connected = json.loads(
                await asyncio.wait_for(ws.recv(), timeout=timeout)
            )
            record("WS connected frame", verify_connected(connected))

            heartbeat = {
                "type": "heartbeat",
                "sequence": 1,
                "last_input_at": time.time(),
            }
            await ws.send(json.dumps(heartbeat))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            record("WS heartbeat_ack", verify_heartbeat_ack(ack))

            await ws.send("this is not json")
            error = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            record("WS bad_json error", verify_error(error, "bad_json"))
    except Exception as exc:  # noqa: BLE001
        record("WS session", [f"{type(exc).__name__}: {exc}"])
        return problems

    if probe_foreign_user:
        foreign_url = ws_url.replace(f"/ws/{owner}", "/ws/not-the-owner")
        try:
            async with websockets.connect(foreign_url, open_timeout=timeout) as ws:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                found = verify_error(frame, "forbidden_user")
                if frame.get("terminal") is not True:
                    found.append("forbidden_user error must be terminal")
                record("WS foreign user rejected", found)
        except Exception as exc:  # noqa: BLE001 - close 4003 surfaces here too
            # The server sends the error frame then closes with 4003; some
            # clients surface the close instead of the frame. Both prove the
            # rejection, but only when the failure is a clean WS close.
            record(
                "WS foreign user rejected",
                [f"raised {type(exc).__name__} (treat as pass only if the "
                 f"server closed the socket: {exc})"],
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-check a running Bridge Core Engine over HTTP+WS."
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Server host (default 127.0.0.1; use the "
                             "Tailscale address for remote checks)")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--user", default="owner",
                        help="Configured OWNER_USER_ID (default owner)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="Per-step timeout in seconds")
    parser.add_argument("--probe-foreign-user", action="store_true",
                        help="Also verify a non-owner id is rejected")
    parser.add_argument("--json", action="store_true",
                        help="Print findings as JSON instead of text")
    args = parser.parse_args(argv)

    ws_url = f"ws://{args.host}:{args.port}/ws/{args.user}"
    http_url = f"http://{args.host}:{args.port}"
    problems = asyncio.run(
        smoke(ws_url, http_url, args.user, args.timeout, args.probe_foreign_user)
    )
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    if problems:
        print(f"SMOKE FAILED ({len(problems)} problem(s))", file=sys.stderr)
        return 1
    print("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
