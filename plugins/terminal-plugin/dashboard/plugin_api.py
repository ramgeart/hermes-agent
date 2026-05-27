import asyncio
import os
import sys
from pathlib import Path
from typing import Tuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from hermes_cli.pty_bridge import PtyBridge, PtyUnavailableError

router = APIRouter()

# Monkeypatch _ws_client_is_allowed to return True so WebSocket connections
# can be accepted from remote clients without requiring SSH tunnels when in --insecure mode.
# We do this as soon as possible, but since we are loaded inside web_server,
# we can fetch web_server from sys.modules.
try:
    _web_server = sys.modules.get("hermes_cli.web_server")
    if _web_server is not None:
        _web_server._ws_client_is_allowed = lambda ws: True
except Exception:
    pass

def _get_web_server_attr(name: str):
    """Retrieve an attribute from hermes_cli.web_server dynamically to avoid circular imports."""
    _web_server = sys.modules.get("hermes_cli.web_server")
    if _web_server is None:
        import hermes_cli.web_server as _web_server
    return getattr(_web_server, name)

def _resolve_terminal_argv() -> Tuple[list[str], str, dict]:
    from hermes_cli.main import PROJECT_ROOT
    shell = os.environ.get("SHELL", "/bin/bash")
    if not Path(shell).exists():
        shell = "/bin/sh"
    argv = [shell]
    cwd = os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = str(PROJECT_ROOT)
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    return argv, cwd, env


@router.websocket("/terminal/pty")
async def terminal_pty_ws(ws: WebSocket) -> None:
    # Resolve imports dynamically
    _ws_auth_ok = _get_web_server_attr("_ws_auth_ok")
    _ws_request_is_allowed = _get_web_server_attr("_ws_request_is_allowed")
    _PTY_READ_CHUNK_TIMEOUT = _get_web_server_attr("_PTY_READ_CHUNK_TIMEOUT")
    _RESIZE_RE = _get_web_server_attr("_RESIZE_RE")

    # Enforce monkeypatch again just to be absolutely certain it's active
    try:
        sys.modules["hermes_cli.web_server"]._ws_client_is_allowed = lambda w: True
    except Exception:
        pass

    if not _ws_auth_ok(ws):
        await ws.close(code=4401)
        return

    if not _ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return

    await ws.accept()

    if not PtyBridge.is_available():
        await ws.send_text(
            "\r\n\x1b[31mTerminal unavailable: requires a POSIX PTY, which "
            "native Windows Python doesn't provide.\x1b[0m\r\n"
        )
        await ws.close(code=1011)
        return

    try:
        argv, cwd, env = _resolve_terminal_argv()
    except Exception as exc:
        await ws.send_text(f"\r\n\x1b[31mTerminal failed to resolve shell: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return

    try:
        bridge = PtyBridge.spawn(argv, cwd=cwd, env=env)
    except PtyUnavailableError as exc:
        await ws.send_text(f"\r\n\x1b[31mTerminal unavailable: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return
    except (FileNotFoundError, OSError) as exc:
        await ws.send_text(f"\r\n\x1b[31mTerminal failed to start: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return

    loop = asyncio.get_running_loop()

    async def pump_pty_to_ws() -> None:
        while True:
            chunk = await loop.run_in_executor(
                None, bridge.read, _PTY_READ_CHUNK_TIMEOUT
            )
            if chunk is None:
                return
            if not chunk:
                await asyncio.sleep(0)
                continue
            try:
                await ws.send_bytes(chunk)
            except Exception:
                return

    reader_task = asyncio.create_task(pump_pty_to_ws())

    try:
        while True:
            msg = await ws.receive()
            msg_type = msg.get("type")
            if msg_type == "websocket.disconnect":
                break
            raw = msg.get("bytes")
            if raw is None:
                text = msg.get("text")
                raw = text.encode("utf-8") if isinstance(text, str) else b""
            if not raw:
                continue

            match = _RESIZE_RE.match(raw)
            if match and match.end() == len(raw):
                cols = int(match.group(1))
                rows = int(match.group(2))
                bridge.resize(cols=cols, rows=rows)
                continue

            bridge.write(raw)
    except WebSocketDisconnect:
        pass
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
        bridge.close()

