"""Session-only ownership of field-selective Wayland output overrides."""
import contextlib
import json
import os
from pathlib import Path
import selectors
import socket
import stat
import time

import output_management


STATE = "output-owner.json"
SOCKET = "output-owner.sock"
LIMIT = 1048576


def load(api, root):
    data = api.display_config.read_file(root / STATE)
    if data is None:
        return None
    state = json.loads(data, object_pairs_hook=api.display_config.strict_object)
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("generation"), str)
            or not api.TOKEN_RE.fullmatch(state["generation"])):
        raise ValueError("Invalid session layout ownership state; manual recovery is required.")
    return state


def active(api, root):
    state = load(api, root)
    return state is not None and not state.get("stopped", False)


def receive(stream):
    data = bytearray()
    while b"\n" not in data:
        chunk = stream.recv(min(65536, LIMIT + 1 - len(data)))
        if not chunk:
            raise ValueError("Session layout owner disconnected before replying.")
        data.extend(chunk)
        if len(data) > LIMIT:
            raise ValueError("Session layout request is too large.")
    line, remainder = data.split(b"\n", 1)
    if remainder:
        raise ValueError("Unexpected data after session layout request.")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Invalid session layout request.")
    return value


def call(root, request):
    path = root / SOCKET
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Unsafe session layout socket.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(8)
        stream.connect(str(path))
        stream.sendall(json.dumps(request, allow_nan=False).encode() + b"\n")
        result = receive(stream)
    if result.get("ok") is not True:
        raise ValueError(result.get("message", "Session layout operation failed."))
    return result


def scale_checks(api):
    option = api.query("getoption", "debug:disable_scale_checks")
    if not isinstance(option, dict) or type(option.get("bool")) is not bool:
        raise ValueError("Cannot establish Hyprland's scale-check policy safely.")
    return not option["bool"]


def preflight(api, current, positions):
    checks = scale_checks(api)
    with output_management.Connection() as connection:
        # A draft must have a representable recovery baseline as well as target.
        connection.plan(current, [{key: m[key] for key in api.POSITION}
                                  for m in current["monitors"]], scale_checks=checks)
        connection.plan(current, positions, scale_checks=checks)


def ensure(api, root, generation=None):
    with api.locked(root, "output-start.lock"):
        try:
            result = call(root, {"command": "status"})
        except OSError:
            previous = load(api, root)
            if generation is not None and (previous is None or previous.get("stopped")
                                           or previous["generation"] != generation):
                raise ValueError("Session layout ownership was released; preserving external changes.")
            unit = "display-workspaces-output-" + root.name.rsplit("-", 1)[-1]
            status = api.run(["systemctl", "--user", "show", "--property=ActiveState", "--value", unit + ".service"])
            if status not in {"active", "activating", "reloading"}:
                args = ["systemd-run", "--user", "--quiet", "--collect", "--service-type=exec",
                        "--unit=" + unit, "--property=Restart=on-failure", "--property=RestartSec=1",
                        "--property=StartLimitIntervalSec=30", "--property=StartLimitBurst=3",
                        "--property=TimeoutStopSec=5", "--property=UMask=0077",
                        "--property=ConditionPathExists=" + str(Path(api.__file__).resolve())]
                for name in ("HOME", "XDG_CONFIG_HOME", "HYPRLAND_INSTANCE_SIGNATURE", "XDG_RUNTIME_DIR",
                             "WAYLAND_DISPLAY", "PATH", "DBUS_SESSION_BUS_ADDRESS"):
                    if name in os.environ:
                        args.append(f"--setenv={name}={os.environ[name]}")
                args.extend([api.sys.executable, str(Path(api.__file__).resolve()), "output-owner"])
                api.run(args, timeout=10)
            deadline = time.monotonic() + 8
            while True:
                try:
                    result = call(root, {"command": "status"})
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ValueError("Session layout owner did not become ready.")
                    time.sleep(0.05)
        if generation is not None and result.get("generation") != generation:
            raise ValueError("Session layout ownership changed; preserving external changes.")
        return result["generation"]


def verify(api, root, generation):
    ensure(api, root, generation)


def apply(api, root, expected, positions, generation):
    ensure(api, root, generation)
    call(root, {"command": "apply", "generation": generation,
                "expected": expected, "positions": positions})


def finish(root, generation, *, keep, retain=False):
    call(root, {"command": "finish", "generation": generation, "keep": keep, "retain": retain})


def release(api, root):
    state = load(api, root)
    if state is None or state.get("stopped"):
        return
    ensure(api, root, state["generation"])
    call(root, {"command": "release", "generation": state["generation"]})


def config_stamp(api):
    monitor_path = api.display_config.config_paths()[0]
    return [api.display_config.digest(api.display_config.read_file(path))
            for path in (monitor_path, monitor_path.parent / "hyprland.lua")]


def geometry_matches(api, expected, current, alternative=None):
    before = {m["name"]: m for m in expected["monitors"]}
    after = {m["name"]: m for m in current["monitors"]}
    alternatives = {m["name"]: m for m in (alternative or expected)["monitors"]}
    disabled_before = {m["name"]: m for m in expected.get("disabledMonitors", [])}
    disabled_after = {m["name"]: m for m in current.get("disabledMonitors", [])}
    return (before.keys() == after.keys() == alternatives.keys()
            and disabled_before.keys() == disabled_after.keys()
            and all(api.same_fields(m, disabled_after[name], api.IDENTITY)
                    for name, m in disabled_before.items())
            and not current.get("unavailableMonitors") and not current.get("mirroredOutputs")
            and expected.get("session") == current.get("session")
            and all(any(api.same_fields(candidate, after[name], api.IDENTITY + api.GEOMETRY + ("x", "y"))
                        for candidate in (m, alternatives[name]))
                    for name, m in before.items()))


def serve(api, root):
    """One event-driven owner per compositor; only explicit requests change geometry."""
    with api.locked(root, "output-owner.lock", blocking=False), contextlib.ExitStack() as resources:
        previous = load(api, root)
        stamp = config_stamp(api)
        state = previous if previous and not previous.get("stopped") else {
            "version": 1, "generation": api.secrets.token_hex(24), "stopped": False,
            "before": None, "after": None, "config": stamp}
        connection = resources.enter_context(output_management.Connection())
        events = resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        events.settimeout(4)
        events.connect(str(Path(os.environ["XDG_RUNTIME_DIR"]) / "hypr"
                           / os.environ["HYPRLAND_INSTANCE_SIGNATURE"] / ".socket2.sock"))
        events.setblocking(False)
        reason = ""
        if state.get("after"):
            current = api.snapshot()
            before, after = state["before"], state["after"]
            # Restart adopts only a before/after mixture left by our own mutation.
            if state["config"] != stamp or not geometry_matches(api, before, current, after):
                reason = "Displays or configuration changed while the layout owner was unavailable."
            else:
                positions = [{key: m[key] for key in api.POSITION} for m in current["monitors"]]
                connection.apply(current, positions, scale_checks=scale_checks(api))
                state.update(before=current, after=current)
        if reason:
            state.update(stopped=True, reason=reason)
            api.atomic(root, STATE, state)
            return
        api.atomic(root, STATE, state)
        listener = resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        path = root / SOCKET
        if path.exists():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Unsafe stale session layout socket.")
            path.unlink()
        listener.bind(str(path))
        os.chmod(path, 0o600)
        resources.callback(path.unlink, missing_ok=True)
        listener.listen(4)
        selector = resources.enter_context(selectors.DefaultSelector())
        selector.register(events, selectors.EVENT_READ, 0)
        selector.register(connection.fileno(), selectors.EVENT_READ, 1)
        selector.register(listener, selectors.EVENT_READ, 2)
        pending_events = b""
        print("Session layout owner ready", flush=True)
        while not reason:
            for key, _ in sorted(selector.select(), key=lambda event: event[0].data):
                if key.data == 0:
                    chunk = events.recv(65536)
                    if not chunk:
                        reason = "Compositor event connection closed."
                        break
                    pending_events += chunk
                    if len(pending_events) > LIMIT:
                        raise ValueError("Compositor event frame is too large.")
                    lines = pending_events.split(b"\n")
                    pending_events = lines.pop()
                    if any(line.partition(b">>")[0] in {
                            b"configreloaded", b"monitoradded", b"monitoraddedv2", b"monitorremoved"}
                           for line in lines):
                        reason = "Display configuration or connections changed; layout ownership released."
                        break
                elif key.data == 1:
                    connection.dispatch()
                    if state.get("after") and not geometry_matches(api, state["after"], api.snapshot(), state["before"]):
                        reason = "Display geometry changed externally; layout ownership released."
                        break
                else:
                    stream, _ = listener.accept()
                    with stream:
                        stream.settimeout(8)
                        try:
                            request = receive(stream)
                            command = request.get("command")
                            if command != "status" and request.get("generation") != state["generation"]:
                                raise ValueError("Session layout ownership changed.")
                            if command == "status":
                                result = {"ok": True, "generation": state["generation"], "holding": state.get("after") is not None}
                            elif command == "release":
                                reason = "Session layout ownership explicitly released."
                                connection.close()
                                state.update(stopped=True, reason=reason)
                                api.atomic(root, STATE, state)
                                result = {"ok": True}
                            elif command == "finish":
                                current = api.snapshot()
                                matched = state.get("after") and geometry_matches(api, state["after"], current)
                                if request.get("keep") and not matched:
                                    raise ValueError("Displays changed before retaining the session layout.")
                                if matched and (request.get("keep") or request.get("retain")):
                                    state.update(before=current, after=current)
                                else:
                                    reason = "Preview finished; new session layout ownership released."
                                    connection.close()
                                    state.update(stopped=True, reason=reason)
                                api.atomic(root, STATE, state)
                                result = {"ok": True}
                            elif command == "apply":
                                if config_stamp(api) != state["config"]:
                                    reason = "Monitor configuration changed; preserving external edits."
                                    raise ValueError(reason)
                                expected, positions = request["expected"], request["positions"]
                                current = api.snapshot()
                                api.compare_outputs(expected, current, catalog=True)
                                selected = api.indexed(positions, "name", "display position")
                                if not selected.keys() <= {m["name"] for m in current["monitors"]}:
                                    raise ValueError("A requested display is no longer connected.")
                                # Pin untouched live geometry too, without restoring a foreign edit.
                                complete = [selected.get(m["name"], {k: m[k] for k in api.POSITION})
                                            for m in current["monitors"]]
                                checks = scale_checks(api)
                                connection.plan(current, complete, scale_checks=checks)
                                state.update(before=current, after=api.expected_outputs(current, complete))
                                api.atomic(root, STATE, state)
                                connection.apply(current, complete, scale_checks=checks)
                                result = {"ok": True}
                            else:
                                raise ValueError("Unknown session layout operation.")
                        except Exception as error:
                            result = {"ok": False, "message": str(error)}
                        with contextlib.suppress(OSError):
                            stream.sendall(json.dumps(result, allow_nan=False).encode() + b"\n")
                    if reason:
                        break
        state.update(stopped=True, reason=reason)
        api.atomic(root, STATE, state)
