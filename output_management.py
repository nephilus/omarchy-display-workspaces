"""Small, geometry-only wlr-output-management client for Hyprland 0.56.2.

The override belongs to the connected manager, not to a configuration object.
Keep this connection alive for as long as its session arrangement is wanted.
No generic Wayland bindings, monitor rules, or color/adaptive-sync requests are
used here. Binding version 2 deliberately excludes newer policy interfaces.
"""

import math
import os
import select
import socket
import struct
import time


_INTERFACE = "zwlr_output_manager_v1"
_SERVER_IDS = 0xFF000000
_MAX_OBJECTS = 16384
_MAX_DISPATCH_BYTES = 1024 * 1024
_REFRESH_TOLERANCE = 0.010000001
_SCALE_TOLERANCE = 0.000001
_MODE_FIELDS = ("width", "height", "refreshRate")
_IDENTITY_FIELDS = ("name", "make", "model", "serial")


def _numbers(payload, fmt):
    if len(payload) != struct.calcsize("=" + fmt):
        raise ValueError("Malformed output-management event.")
    return struct.unpack("=" + fmt, payload)


def _string(payload, offset=0):
    if len(payload) < offset + 4:
        raise ValueError("Malformed output-management string.")
    length = struct.unpack_from("=I", payload, offset)[0]
    start, end = offset + 4, offset + 4 + length
    padded = (end + 3) & ~3
    if not length or padded > len(payload) or payload[end - 1] != 0 or b"\0" in payload[start:end - 1]:
        raise ValueError("Malformed output-management string.")
    try:
        return payload[start:end - 1].decode("utf-8"), padded
    except UnicodeDecodeError:
        raise ValueError("Invalid output-management text.") from None


def _wire_string(value):
    encoded = value.encode("utf-8") + b"\0"
    return struct.pack("=I", len(encoded)) + encoded + bytes((-len(encoded)) % 4)


def _number(value, label, minimum, maximum, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not minimum <= value <= maximum or not math.isfinite(value)
            or (integer and value != int(value))):
        raise ValueError(f"Invalid native display {label}.")
    return int(value) if integer else value


def _indexed(records, label):
    if not isinstance(records, list):
        raise ValueError(f"Invalid {label} list.")
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"Invalid {label}.")
        name = record.get("name")
        if not isinstance(name, str) or not name or name in result:
            raise ValueError(f"Invalid or duplicate {label} name.")
        result[name] = record
    return result


def _identity(record):
    values = tuple("" if record.get(key) is None else record[key] for key in _IDENTITY_FIELDS)
    if not all(isinstance(value, str) for value in values):
        raise ValueError("Invalid native display identity.")
    return values


def _same_mode(left, right):
    return (left.get("width") == right.get("width") and left.get("height") == right.get("height")
            and isinstance(left.get("refreshRate"), (int, float))
            and isinstance(right.get("refreshRate"), (int, float))
            and abs(left["refreshRate"] - right["refreshRate"]) <= _REFRESH_TOLERANCE)


def _geometry(record):
    return {
        "x": _number(record.get("x"), "X coordinate", -32768, 32768, True),
        "y": _number(record.get("y"), "Y coordinate", -32768, 32768, True),
        "width": _number(record.get("width"), "width", 1, 65536, True),
        "height": _number(record.get("height"), "height", 1, 65536, True),
        "refreshRate": _number(record.get("refreshRate"), "refresh rate", 0.001, 1000),
        "scale": _number(record.get("scale"), "scale", 0.1, 16),
        "transform": _number(record.get("transform"), "transform", 0, 7, True),
    }


def _scale_fixed(geometry, scale_checks):
    scale = geometry["scale"]
    if scale > 10:
        raise ValueError("Native output management supports scales only from 0.1 to 10, including rollback.")
    fixed = round(scale * 256)
    wire_scale = fixed / 256
    if not 0.1 <= wire_scale <= 10:
        raise ValueError("Display scale is outside the native fixed-point range.")
    logical = (geometry["width"] / wire_scale, geometry["height"] / wire_scale)
    wire_integral = all(value == round(value) for value in logical)
    if abs(wire_scale - scale) <= _SCALE_TOLERANCE:
        if scale_checks and not wire_integral:
            raise ValueError("Native scale checks would change the requested fractional logical size.")
        return fixed
    # Hyprland corrects fractional logical pixels to the nearest 1/120 scale.
    # If the wire value already has integral pixels, correction never runs.
    corrected = round(wire_scale * 120) / 120
    logical = (geometry["width"] / corrected, geometry["height"] / corrected)
    if (not scale_checks or abs(corrected - scale) > _SCALE_TOLERANCE
            or not all(value == round(value) for value in logical) or wire_integral):
        raise ValueError("Display scale cannot be represented safely by native output management; "
                         "use a 1/256 scale or enable compatible Hyprland scale checks.")
    return fixed


class Connection:
    """One retained native override owner; construction itself does not connect."""

    def __init__(self, timeout=4.0):
        self.timeout = _number(timeout, "connection timeout", 0.001, 60)
        self._socket = None
        self._buffer = bytearray()
        self._objects = {}
        self._next_id = 2
        self._globals = {}
        self._global = None
        self._manager = None
        self._serial = None
        self._path = None
        self._mode_overrides = set()

    def __enter__(self):
        if self._socket is not None:
            raise ValueError("Output-management connection is already open.")
        display = os.environ.get("WAYLAND_DISPLAY", "")
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        if not display or "\0" in display:
            raise ValueError("WAYLAND_DISPLAY is unavailable for native display changes.")
        if not os.path.isabs(display) and (not runtime or not os.path.isabs(runtime)):
            raise ValueError("XDG_RUNTIME_DIR is unavailable for native display changes.")
        return self._connect(display if os.path.isabs(display) else os.path.join(runtime, display))

    def _connect(self, path):
        self._path = path
        self._objects = {1: {"kind": "display"}}
        self._buffer.clear()
        self._globals.clear()
        self._mode_overrides.clear()
        self._next_id = 2
        self._global = self._manager = self._serial = None
        try:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.settimeout(self.timeout)
            self._socket.connect(path)
            self._socket.setblocking(False)
            registry, _ = self._new("registry")
            self._send(1, 1, struct.pack("=I", registry))
            self._roundtrip()
            managers = [(name, version) for name, version in self._globals.items() if version >= 2]
            if len(managers) != 1:
                raise ValueError("A unique wlr-output-management version 2 manager is required.")
            self._global = managers[0][0]
            self._manager, _ = self._new("manager")
            self._send(registry, 0, struct.pack("=I", self._global) + _wire_string(_INTERFACE)
                       + struct.pack("=II", 2, self._manager))
            self._roundtrip()
            if self._serial is None:
                raise ValueError("Native output discovery did not complete.")
            return self
        except (OSError, OverflowError):
            self.close()
            raise ValueError("Cannot connect to the native Wayland output manager.") from None
        except ValueError:
            self.close()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._buffer.clear()
        self._objects.clear()

    def fileno(self):
        if self._socket is None:
            raise ValueError("Native output-management connection is closed.")
        return self._socket.fileno()

    def _new(self, kind, **properties):
        if self._next_id >= _SERVER_IDS or len(self._objects) >= _MAX_OBJECTS:
            raise ValueError("Native output-management resource limit reached.")
        object_id = self._next_id
        self._next_id += 1
        state = {"kind": kind, **properties}
        self._objects[object_id] = state
        return object_id, state

    def _server_object(self, object_id, kind, **properties):
        previous = self._objects.get(object_id)
        if (object_id < _SERVER_IDS or (previous is not None and not previous.get("finished"))
                or len(self._objects) >= _MAX_OBJECTS):
            raise ValueError("Invalid native output-management resource.")
        self._objects[object_id] = {"kind": kind, "finished": False, **properties}

    def _send(self, object_id, opcode, payload=b"", deadline=None):
        self.fileno()
        deadline = time.monotonic() + self.timeout if deadline is None else deadline
        packet = memoryview(struct.pack("=II", object_id, ((len(payload) + 8) << 16) | opcode) + payload)
        while packet:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise ValueError("Native output-management request timed out.")
            try:
                if not select.select([], [self._socket], [], remaining)[1]:
                    self.close()
                    raise ValueError("Native output-management request timed out.")
                sent = self._socket.send(packet)
                if not sent:
                    raise OSError("closed")
                packet = packet[sent:]
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                self.close()
                raise ValueError("Native output-management connection was lost.") from None

    def dispatch(self):
        """Consume buffered frames and currently readable data without waiting."""
        self.fileno()
        received = 0
        try:
            while True:
                offset = 0
                while len(self._buffer) - offset >= 8:
                    object_id, header = struct.unpack_from("=II", self._buffer, offset)
                    size, opcode = header >> 16, header & 0xFFFF
                    if size < 8 or size % 4:
                        raise ValueError("Malformed native output-management frame.")
                    if len(self._buffer) - offset < size:
                        break
                    self._event(object_id, opcode, bytes(self._buffer[offset + 8:offset + size]))
                    offset += size
                if offset:
                    del self._buffer[:offset]
                try:
                    chunk = self._socket.recv(65536)
                except BlockingIOError:
                    return
                except InterruptedError:
                    continue
                if not chunk:
                    raise ValueError("Native output-management connection was closed by the compositor.")
                received += len(chunk)
                if received > _MAX_DISPATCH_BYTES:
                    raise ValueError("Native output-management event limit exceeded.")
                self._buffer.extend(chunk)
        except OSError:
            self.close()
            raise ValueError("Native output-management connection was lost.") from None
        except ValueError:
            self.close()
            raise

    def _wait(self, complete, deadline):
        while True:
            self.dispatch()
            if complete():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Native output-management response timed out.")
            try:
                select.select([self._socket], [], [], remaining)
            except InterruptedError:
                continue
            except OSError:
                self.close()
                raise ValueError("Native output-management connection was lost.") from None

    def _roundtrip(self):
        deadline = time.monotonic() + self.timeout
        callback, state = self._new("callback", done=False)
        self._send(1, 0, struct.pack("=I", callback), deadline)
        self._wait(lambda: state["done"], deadline)

    def _event(self, object_id, opcode, payload):
        state = self._objects.get(object_id)
        if state is None or state.get("finished"):
            raise ValueError("Event for an unavailable native output resource.")
        kind = state["kind"]
        if kind == "display":
            if opcode == 0:
                if len(payload) < 12:
                    raise ValueError("Malformed Wayland protocol error.")
                _, code = struct.unpack_from("=II", payload)
                _, end = _string(payload, 8)
                if end != len(payload):
                    raise ValueError("Malformed Wayland protocol error.")
                # Compositor error text can contain private hardware identities.
                raise ValueError(f"Wayland output-management protocol error (code {code}).")
            if opcode == 1:
                deleted, = _numbers(payload, "I")
                old = self._objects.get(deleted)
                if old is None or deleted >= _SERVER_IDS or old["kind"] not in ("callback", "configuration", "configuration_head"):
                    raise ValueError("Invalid Wayland resource deletion.")
                del self._objects[deleted]
                return
        elif kind == "registry":
            if opcode == 0:
                if len(payload) < 12:
                    raise ValueError("Malformed Wayland global.")
                name = struct.unpack_from("=I", payload)[0]
                interface, end = _string(payload, 4)
                version, = _numbers(payload[end:], "I")
                if interface == _INTERFACE:
                    self._globals[name] = version
                return
            if opcode == 1:
                removed, = _numbers(payload, "I")
                self._globals.pop(removed, None)
                if removed == self._global:
                    raise ValueError("Native output manager was removed.")
                return
        elif kind == "callback" and opcode == 0:
            _numbers(payload, "I")
            state["done"] = True
            return
        elif kind == "manager":
            if opcode == 0:
                head, = _numbers(payload, "I")
                self._server_object(head, "head", modes=[], make="", model="", serial="")
                return
            if opcode == 1:
                self._serial, = _numbers(payload, "I")
                return
            if opcode == 2:
                _numbers(payload, "")
                raise ValueError("Native output manager finished; refresh the display session.")
        elif kind == "head":
            if opcode in (0, 1, 10, 11, 12):
                value, end = _string(payload)
                if end != len(payload):
                    raise ValueError("Malformed native display identity.")
                state[{0: "name", 1: "description", 10: "make", 11: "model", 12: "serial"}[opcode]] = value
                return
            if opcode == 2:
                _numbers(payload, "ii")
                return
            if opcode == 3:
                mode, = _numbers(payload, "I")
                self._server_object(mode, "mode", head=object_id)
                state["modes"].append(mode)
                return
            if opcode == 4:
                enabled, = _numbers(payload, "i")
                state["enabled"] = bool(enabled)
                return
            if opcode == 5:
                mode, = _numbers(payload, "I")
                resource = self._objects.get(mode, {})
                if resource.get("kind") != "mode" or resource.get("head") != object_id or resource.get("finished"):
                    raise ValueError("Native display refers to an unavailable current mode.")
                state["current_mode"] = mode
                return
            if opcode == 6:
                state["x"], state["y"] = _numbers(payload, "ii")
                return
            if opcode in (7, 8):
                value, = _numbers(payload, "i")
                state["transform" if opcode == 7 else "scale"] = value if opcode == 7 else value / 256
                return
            if opcode == 9:
                _numbers(payload, "")
                raise ValueError("A native display head finished; refresh the display session.")
        elif kind == "mode":
            if opcode == 0:
                state["width"], state["height"] = _numbers(payload, "ii")
                return
            if opcode == 1:
                refresh, = _numbers(payload, "i")
                state["refreshRate"] = refresh / 1000
                return
            if opcode in (2, 3):
                _numbers(payload, "")
                state["preferred" if opcode == 2 else "finished"] = True
                return
        elif kind == "configuration" and opcode in (0, 1, 2):
            _numbers(payload, "")
            if state["result"] is not None:
                raise ValueError("Duplicate native display configuration result.")
            state["result"] = opcode
            return
        raise ValueError("Unsupported native output-management event.")

    def _heads(self):
        result = {}
        for object_id, head in self._objects.items():
            if head["kind"] != "head" or head.get("finished"):
                continue
            name = head.get("name")
            if not isinstance(name, str) or not name or name in result or "enabled" not in head:
                raise ValueError("Incomplete or ambiguous native display discovery.")
            result[name] = (object_id, head)
        return result

    def _snapshot(self):
        active, disabled = [], []
        for object_id, head in self._heads().values():
            record = {key: head.get(key, "") for key in _IDENTITY_FIELDS}
            record["disabled"] = not head["enabled"]
            if head["enabled"]:
                mode = self._objects.get(head.get("current_mode"), {})
                if mode.get("kind") != "mode" or mode.get("finished") or mode.get("head") != object_id:
                    raise ValueError("Native output management does not expose the active display timing.")
                record.update({key: mode.get(key) for key in _MODE_FIELDS})
                record.update({key: head.get(key) for key in ("x", "y", "scale", "transform")})
                _geometry(record)
                record["modeOptions"] = [
                    {key: self._objects[candidate].get(key) for key in _MODE_FIELDS}
                    for candidate in head["modes"]
                    if candidate in self._objects and not self._objects[candidate].get("finished")
                    and self._objects[candidate].get("head") == object_id
                ]
                active.append(record)
            else:
                disabled.append(record)
        return {"monitors": active, "disabledMonitors": disabled}

    def _compare(self, expected, actual):
        if not isinstance(expected, dict) or expected.get("mirroredOutputs") or expected.get("unavailableMonitors"):
            raise ValueError("Mirrored or unavailable displays cannot use native output management.")
        enabled = _indexed(expected.get("monitors"), "enabled display")
        disabled = _indexed(expected.get("disabledMonitors", []), "disabled display")
        live = _indexed(actual["monitors"], "native enabled display")
        inactive = _indexed(actual["disabledMonitors"], "native disabled display")
        if not enabled or enabled.keys() != live.keys() or disabled.keys() != inactive.keys() or enabled.keys() & disabled.keys():
            raise ValueError("Connected or enabled native displays changed. Refresh the panel.")
        for name, record in enabled.items():
            if record.get("disabled") or record.get("mirrorOf", "none") not in (None, "", "none"):
                raise ValueError("Disabled or mirrored displays cannot be arranged natively.")
            before = _geometry(record)
            after = live[name]
            if (_identity(record) != _identity(after) or not _same_mode(before, after)
                    or any(before[key] != after[key] for key in ("x", "y", "transform"))
                    or abs(round(before["scale"] * 256) / 256 - after["scale"]) > _SCALE_TOLERANCE):
                raise ValueError("Native display identity or geometry changed. Refresh the panel.")
        if any(_identity(record) != _identity(inactive[name]) for name, record in disabled.items()):
            raise ValueError("Disabled native displays changed. Refresh the panel.")
        return enabled

    def _refresh(self, expected):
        self.fileno()
        # Hyprland 0.56.2 does not advertise every soft geometry change to an
        # existing manager. A fresh, read-only manager observes the actual state.
        # A separate socket releases all v2 resources without losing our overrides.
        probe = Connection(self.timeout)
        try:
            probe._connect(self._path)
            actual = probe._snapshot()
            enabled = self._compare(expected, actual)
        finally:
            probe.close()
        self._roundtrip()
        heads = self._heads()
        discovered = actual["monitors"] + actual["disabledMonitors"]
        if heads.keys() != {record["name"] for record in discovered}:
            raise ValueError("Native output resources changed during discovery.")
        for record in discovered:
            _, head = heads[record["name"]]
            if _identity(head) != _identity(record) or head["enabled"] == record["disabled"]:
                raise ValueError("Native output resources changed during discovery.")
        return enabled, heads, _indexed(actual["monitors"], "native display")

    def check(self, expected):
        """Read fresh native state and reject a stale IPC baseline, without edits."""
        self._refresh(expected)

    def _prepare(self, expected, positions, scale_checks):
        if not isinstance(scale_checks, bool):
            raise ValueError("Native scale-check policy must be explicit.")
        targets = _indexed(positions, "native display position")
        enabled, heads, current = self._refresh(expected)
        if not targets.keys() <= enabled.keys():
            raise ValueError("Native changes may target only expected enabled displays.")
        prepared = []
        for name, position in targets.items():
            original = enabled[name]
            before, target = _geometry(original), _geometry(position)
            _scale_fixed(before, scale_checks)
            fixed = _scale_fixed(target, scale_checks)
            head_id, head = heads[name]
            unchanged = _same_mode(before, target)
            options = original.get("modeOptions")
            if not isinstance(options, list) or not all(isinstance(mode, dict) for mode in options):
                raise ValueError("Native mode changes require the current advertised mode catalog.")
            advertised = any(_same_mode(mode, before) for mode in options)
            mode_id = None
            if not advertised:
                if not unchanged:
                    raise ValueError("Cannot switch away from an unadvertised custom timing with native rollback.")
                if head_id in self._mode_overrides:
                    raise ValueError("A custom timing cannot replace this connection's existing mode override; "
                                     "a fresh native output owner is required.")
            else:
                if not any(_same_mode(mode, target) for mode in options):
                    raise ValueError("Requested native display mode is not advertised.")
                if not any(_same_mode(mode, target) for mode in current[name]["modeOptions"]):
                    raise ValueError("Requested native display mode is no longer advertised.")
                matches = []
                for candidate in head["modes"]:
                    mode = self._objects.get(candidate, {})
                    if not mode.get("finished") and mode.get("head") == head_id and _same_mode(mode, target):
                        matches.append(candidate)
                if not matches:
                    raise ValueError("Requested native display mode is not advertised.")
                # A manager retains committed MODE across configurations, even
                # failed ones. Pin advertised timing on every apply so takeover
                # and rollback cannot inherit an old override or fallback mode.
                if unchanged and head.get("current_mode") in matches:
                    mode_id = head["current_mode"]
                else:
                    closest = min(abs(self._objects[candidate]["refreshRate"] - target["refreshRate"])
                                  for candidate in matches)
                    nearest = [candidate for candidate in matches
                               if abs(abs(self._objects[candidate]["refreshRate"] - target["refreshRate"])
                                      - closest) <= 0.000000001]
                    if len(nearest) != 1:
                        raise ValueError("Requested native display mode is ambiguous.")
                    mode_id = nearest[0]
            prepared.append((head_id, mode_id, target, fixed))
        return prepared

    def plan(self, expected, positions, *, scale_checks=True):
        """Preflight every target and rollback scale without creating a config."""
        self._prepare(expected, positions, scale_checks)

    def apply(self, expected, positions, *, scale_checks=True):
        """Commit only selected enabled heads, preserving all nongeometry policy."""
        prepared = self._prepare(expected, positions, scale_checks)
        if not prepared:
            return
        # Allocate and serialize everything locally before the first config request.
        count = len(prepared) + 1
        if len(self._objects) + count >= _MAX_OBJECTS or self._next_id + count >= _SERVER_IDS:
            raise ValueError("Native output-management resource limit reached.")
        configuration, state = self._new("configuration", result=None)
        requests = [(self._manager, 0, struct.pack("=II", configuration, self._serial))]
        for head_id, mode_id, target, fixed in prepared:
            config_head, _ = self._new("configuration_head")
            requests.append((configuration, 0, struct.pack("=II", config_head, head_id)))
            if mode_id is not None:
                requests.append((config_head, 0, struct.pack("=I", mode_id)))
            requests.extend(((config_head, 2, struct.pack("=ii", target["x"], target["y"])),
                             (config_head, 3, struct.pack("=i", target["transform"])),
                             (config_head, 4, struct.pack("=i", fixed))))
        requests.append((configuration, 2, b""))
        deadline = time.monotonic() + self.timeout
        try:
            for object_id, opcode, payload in requests:
                self._send(object_id, opcode, payload, deadline)
            self._mode_overrides.update(head_id for head_id, mode_id, _, _ in prepared if mode_id is not None)
            self._wait(lambda: state["result"] is not None, deadline)
            if state["result"] == 1:
                raise ValueError("Native display configuration failed.")
            if state["result"] == 2:
                raise ValueError("Native display configuration was cancelled; refresh the panel.")
        finally:
            if self._socket is not None:
                self._send(configuration, 4)
        self._roundtrip()
        targets = _indexed(positions, "native display position")
        intended = {**expected, "monitors": [{**monitor, **targets.get(monitor["name"], {})}
                                             for monitor in expected["monitors"]]}
        self._refresh(intended)
