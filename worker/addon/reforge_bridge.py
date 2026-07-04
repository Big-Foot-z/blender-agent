"""Reforge Blender bridge addon (bridge plan §4.1).

A localhost-only TCP server inside an interactive Blender session so the Reforge
app can refresh the scene after a low-poly approval without a manual re-import.

Threading model: the socket threads ONLY read/write bytes; every command runs on
Blender's main thread via ``bpy.app.timers`` (bpy is main-thread-only). A
connection thread enqueues the request and blocks (with a timeout) until the
timer has executed it, then writes the JSON-line response.

Discovery/auth: on register the addon writes ``~/.reforge/bridge.json`` with
``{port, token, blender_version, pid}``; the app reads it and must echo the
token on every request (prevents local port hijacking, plan §4.1/§6).

Protocol: one JSON object per line, response is one JSON object per line.
Commands: ``ping``, ``open_file {path}``, ``refresh {blend_path, fbx_path}``.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import threading

import bpy

bl_info = {
    "name": "Reforge Bridge",
    "author": "Reforge",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "Background TCP server (localhost)",
    "description": "Lets the Reforge app refresh/open working models in this session",
    "category": "System",
}

BASE_PORT = 43717
PORT_SCAN_RANGE = 10
HANDSHAKE_PATH = os.path.join(os.path.expanduser("~"), ".reforge", "bridge.json")
_TIMER_INTERVAL = 0.2
_EXEC_TIMEOUT = 30.0

# One pending request at a time is plenty for a local single-user bridge.
_requests: "queue.Queue[_Pending]" = queue.Queue()
_server: "_BridgeServer | None" = None


class _Pending:
    def __init__(self, request: dict):
        self.request = request
        self.done = threading.Event()
        self.response: dict = {"ok": False, "reason": "not_executed"}


def _norm(path: str | None) -> str:
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


# ---------------------------------------------------------------------------
# Main-thread command handlers (called from the bpy.app.timers callback)
# ---------------------------------------------------------------------------

def _cmd_ping(_req: dict) -> dict:
    return {"ok": True, "file": bpy.data.filepath, "dirty": bpy.data.is_dirty}


def _import_tagged(path: str) -> dict:
    """Import an fbx/glb into the current scene and tag the objects."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    else:
        return {"ok": False, "reason": f"unsupported_extension:{ext}"}
    imported = [o.name for o in bpy.context.selected_objects]
    for obj in bpy.context.selected_objects:
        obj["reforge_source"] = path
    return {"ok": True, "imported": imported}


def _cmd_open_file(req: dict) -> dict:
    path = req.get("path") or ""
    if not os.path.exists(path):
        return {"ok": False, "reason": "not_found"}
    if path.lower().endswith(".blend"):
        bpy.ops.wm.open_mainfile(filepath=path)
        return {"ok": True, "file": bpy.data.filepath}
    bpy.ops.wm.read_homefile(use_empty=True)
    return _import_tagged(path)


def _cmd_refresh(req: dict) -> dict:
    blend_path = req.get("blend_path") or ""
    fbx_path = req.get("fbx_path") or ""

    # 1) The working .blend itself is open -> revert (unless there are unsaved
    #    edits: never destroy user data, plan §4.1 unsaved protection).
    if blend_path and _norm(bpy.data.filepath) == _norm(blend_path):
        if bpy.data.is_dirty:
            return {"ok": False, "reason": "dirty"}
        bpy.ops.wm.revert_mainfile()
        return {"ok": True, "mode": "revert", "file": bpy.data.filepath}

    # 2) A tagged import of the working fbx is in the scene -> replace it.
    if fbx_path:
        tagged = [
            o for o in bpy.data.objects
            if _norm(o.get("reforge_source", "")) == _norm(fbx_path)
        ]
        if tagged:
            for obj in tagged:
                mesh = obj.data if obj.type == "MESH" else None
                bpy.data.objects.remove(obj, do_unlink=True)
                if mesh is not None and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            if not os.path.exists(fbx_path):
                return {"ok": False, "reason": "not_found"}
            res = _import_tagged(fbx_path)
            if res.get("ok"):
                res["mode"] = "reimport"
            return res

    return {"ok": False, "reason": "not_loaded"}


_COMMANDS = {
    "ping": _cmd_ping,
    "open_file": _cmd_open_file,
    "refresh": _cmd_refresh,
}


def _drain_requests() -> float:
    """bpy.app.timers callback: execute queued requests on the main thread."""
    while True:
        try:
            pending = _requests.get_nowait()
        except queue.Empty:
            return _TIMER_INTERVAL
        try:
            cmd = pending.request.get("cmd")
            handler = _COMMANDS.get(cmd)
            if handler is None:
                pending.response = {"ok": False, "reason": f"unknown_cmd:{cmd}"}
            else:
                pending.response = handler(pending.request)
        except Exception as exc:  # noqa: BLE001 - report, never kill the timer
            pending.response = {"ok": False, "reason": f"error:{exc}"}
        finally:
            pending.done.set()


# ---------------------------------------------------------------------------
# Socket server (background threads: bytes only, no bpy)
# ---------------------------------------------------------------------------

class _BridgeServer:
    def __init__(self):
        self.token = secrets.token_hex(16)
        self.sock: socket.socket | None = None
        self.port = 0
        self.stopping = False
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        for port in range(BASE_PORT, BASE_PORT + PORT_SCAN_RANGE):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                s.listen(4)
                self.sock = s
                self.port = port
                break
            except OSError:
                continue
        if self.sock is None:
            raise OSError("reforge_bridge: no free port in scan range")
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _accept_loop(self) -> None:
        while not self.stopping and self.sock is not None:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return  # socket closed on unregister
            t = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            t.start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(_EXEC_TIMEOUT + 5)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 1_000_000:
                    return
            line = buf.split(b"\n", 1)[0]
            try:
                request = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(conn, {"ok": False, "reason": "bad_json"})
                return
            if request.get("token") != self.token:
                self._send(conn, {"ok": False, "reason": "bad_token"})
                return
            pending = _Pending(request)
            _requests.put(pending)
            if not pending.done.wait(_EXEC_TIMEOUT):
                self._send(conn, {"ok": False, "reason": "timeout"})
                return
            self._send(conn, pending.response)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send(conn: socket.socket, obj: dict) -> None:
        conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _write_handshake(server: _BridgeServer) -> None:
    os.makedirs(os.path.dirname(HANDSHAKE_PATH), exist_ok=True)
    doc = {
        "port": server.port,
        "token": server.token,
        "blender_version": bpy.app.version_string,
        "pid": os.getpid(),
    }
    with open(HANDSHAKE_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)


def _remove_handshake() -> None:
    try:
        os.remove(HANDSHAKE_PATH)
    except OSError:
        pass


def register() -> None:
    global _server
    if _server is not None:
        return
    # Never start the server (or overwrite a live session's handshake file) from
    # a --background Blender — e.g. the headless install_bridge.py run itself.
    if bpy.app.background:
        print("reforge_bridge: background mode, bridge server not started")
        return
    server = _BridgeServer()
    server.start()
    _server = server
    _write_handshake(server)
    if not bpy.app.timers.is_registered(_drain_requests):
        bpy.app.timers.register(_drain_requests, persistent=True)
    print(f"reforge_bridge: listening on 127.0.0.1:{server.port}")


def unregister() -> None:
    global _server
    if bpy.app.timers.is_registered(_drain_requests):
        bpy.app.timers.unregister(_drain_requests)
    if _server is not None:
        _server.stop()
        _server = None
    _remove_handshake()


if __name__ == "__main__":
    register()
