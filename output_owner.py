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

def restoration(api, root, current):
    state = load(api, root)
    if not state or state.get("stopped") or not state.get("before") or not state.get("after"):
        return None
    try:
        if state.get("config") != config_stamp(api) or current.get("session") != state["before"].get("session"):
            return None
    except (OSError, ValueError):
        return None
    source = state.get("restore") or state["before"]
    connected = {m["name"]: m for m in current["monitors"] + current.get("disabledMonitors", [])}
    saved = {m["name"]: m for m in source.get("monitors", [])}
    if (not saved or not saved.keys() <= connected.keys()
            or any(not api.same_fields(monitor, connected[name], api.IDENTITY)
                   for name, monitor in saved.items())
            or not any(name in {m["name"] for m in current.get("disabledMonitors", [])}
                       for name in saved)):
        return None
    return source


def restorable(api, root, current):
    """Return complete geometry only for outputs disabled by the live owner."""
    source = restoration(api, root, current)
    if source is None:
        return []
    disabled = {m["name"] for m in current.get("disabledMonitors", [])}
    return [{**monitor, "disabled": True} for monitor in source["monitors"]
            if monitor["name"] in disabled]


def redock_layout(api, root, current):
    """Return the owner's complete pre-disable geometry for all connected outputs."""
    source = restoration(api, root, current)
    return [dict(monitor) for monitor in source["monitors"]] if source is not None else []


def redock_workspaces(api, root, current):
    """Return the pre-disable workspace placement retained by the live owner."""
    source = restoration(api, root, current)
    return [dict(workspace) for workspace in source.get("workspaces", [])
            if workspace.get("id", 0) > 0] if source is not None else []


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
    rollback = [{**{key: m[key] for key in api.POSITION}, "enabled": True,
                 "modeOptions": m.get("modeOptions", [])} for m in current["monitors"]]
    rollback.extend({**{key: m[key] for key in api.POSITION}, "enabled": False,
                     "modeOptions": m.get("modeOptions", [])}
                    for m in current.get("restorableMonitors", []))
    with output_management.Connection() as connection:
        connection.plan(current, rollback, scale_checks=checks)
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
    alternatives = alternative or expected
    expected_enabled = {m["name"]: m for m in expected["monitors"]}
    alternative_enabled = {m["name"]: m for m in alternatives["monitors"]}
    expected_disabled = {m["name"]: m for m in expected.get("disabledMonitors", [])}
    alternative_disabled = {m["name"]: m for m in alternatives.get("disabledMonitors", [])}
    current_enabled = {m["name"]: m for m in current["monitors"]}
    current_disabled = {m["name"]: m for m in current.get("disabledMonitors", [])}
    connected = set(expected_enabled) | set(expected_disabled)
    if (connected != set(alternative_enabled) | set(alternative_disabled)
            or connected != set(current_enabled) | set(current_disabled)
            or current.get("unavailableMonitors") or current.get("mirroredOutputs")
            or expected.get("session") != current.get("session")):
        return False
    for name in connected:
        if name in current_enabled:
            candidates = [state[name] for state in (expected_enabled, alternative_enabled) if name in state]
            if not any(api.same_fields(candidate, current_enabled[name],
                                       api.IDENTITY + api.GEOMETRY + ("x", "y"))
                       for candidate in candidates):
                return False
        else:
            candidates = [state[name] for state in (expected_disabled, alternative_disabled) if name in state]
            if not any(api.same_fields(candidate, current_disabled[name], api.IDENTITY)
                       for candidate in candidates):
                return False
    return True


def serve(api, root):
    """One event-driven owner per compositor; only explicit requests change geometry."""
    with api.locked(root, "output-owner.lock", blocking=False), contextlib.ExitStack() as resources:
        previous = load(api, root)
        stamp = config_stamp(api)
        state = previous if previous and not previous.get("stopped") else {
            "version": 1, "generation": api.secrets.token_hex(24), "stopped": False,
            "before": None, "after": None, "restore": None, "config": stamp}
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
                positions = [{**{key: m[key] for key in api.POSITION}, "enabled": True,
                              "modeOptions": m.get("modeOptions", [])}
                             for m in current["monitors"]]
                positions.extend({**{key: m[key] for key in api.POSITION}, "enabled": False,
                                  "modeOptions": m.get("modeOptions", [])}
                                 for m in current.get("restorableMonitors", []))
                topology_owned = ({m["name"] for m in before["monitors"]}
                                  != {m["name"] for m in after["monitors"]})
                if not topology_owned:
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
                    kinds = {line.partition(b">>")[0] for line in lines}
                    if b"configreloaded" in kinds:
                        reason = "Display configuration or connections changed; layout ownership released."
                        break
                    if kinds & {b"monitoradded", b"monitoraddedv2", b"monitorremoved"}:
                        current = api.snapshot()
                        if not state.get("after") or not geometry_matches(api, state["after"], current,
                                                                        state["before"]):
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
                                if not geometry_matches(api, expected, current):
                                    raise ValueError("Displays changed before session layout mutation.")
                                selected = api.indexed(positions, "name", "display target")
                                connected = ({m["name"] for m in current["monitors"]}
                                             | {m["name"] for m in current.get("disabledMonitors", [])})
                                if not selected.keys() <= connected:
                                    raise ValueError("A requested display is no longer connected.")
                                complete = list(positions)
                                complete.extend({"name": m["name"], "enabled": False}
                                                for m in current.get("disabledMonitors", [])
                                                if m["name"] not in selected)
                                checks = scale_checks(api)
                                after = api.expected_outputs(current, complete)
                                topology_change = ({m["name"] for m in current["monitors"]}
                                                   != {m["name"] for m in after["monitors"]})
                                if topology_change:
                                    with output_management.Connection() as probe:
                                        probe.plan(current, complete, scale_checks=checks)
                                else:
                                    connection.plan(current, complete, scale_checks=checks)
                                restore = state.get("restore")
                                if topology_change and restore is None:
                                    restore = current
                                if restore and not ({m["name"] for m in restore["monitors"]}
                                                    & {m["name"] for m in after.get("disabledMonitors", [])}):
                                    restore = None
                                state.update(before=current, after=after, restore=restore)
                                api.atomic(root, STATE, state)
                                if topology_change:
                                    api.apply_topology(current, complete)
                                else:
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
