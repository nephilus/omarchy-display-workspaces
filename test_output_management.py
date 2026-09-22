"""Native output-management regressions using a private, in-process compositor."""
import copy
import os
import queue
import select
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from output_management import Connection


POSITION = ("name", "x", "y", "width", "height", "refreshRate", "scale", "transform")


def uint(*values):
    return struct.pack("=" + "I" * len(values), *values)


def integer(*values):
    return struct.pack("=" + "i" * len(values), *values)


def string(value):
    encoded = value.encode() + b"\0"
    return uint(len(encoded)) + encoded + b"\0" * (-len(encoded) % 4)


def take_string(payload, offset):
    size, = struct.unpack_from("=I", payload, offset)
    start = offset + 4
    return payload[start:start + size - 1].decode(), start + (size + 3) // 4 * 4


def monitor(name, x=0, disabled=False):
    return {"name": name, "make": "Fixture", "model": "Panel", "serial": "synthetic-" + name,
            "width": 1920, "height": 1080, "refreshRate": 60.0, "scale": 1.0,
            "transform": 0, "x": x, "y": 0, "disabled": disabled,
            "modeOptions": [{"width": 1920, "height": 1080, "refreshRate": 60.0},
                            {"width": 1600, "height": 900, "refreshRate": 75.0}]}


class OutputServer:
    """Enough real wire protocol to observe geometry, hidden policy, and lifetime.

    Like Hyprland, omitted heads are untouched, test is only a success stub,
    and manager serials do not provide a stale-state guard.
    """

    def __init__(self, heads=None, *, fragmented=False, outcome="success", version=2,
                 scale_checks=True, absolute_display=False):
        self.heads = copy.deepcopy(heads if heads is not None else [
            monitor("DP-7"), monitor("HDMI-A-3", 1920), monitor("DP-9", disabled=True)])
        for head in self.heads:
            head["policy"] = {"icc": "fixture.icc", "hdr": True, "vrr": 2}
            head["timing"] = "original-timing-" + head["name"]
        self.fallback_modes = {
            head["name"]: {key: head[key] for key in ("width", "height", "refreshRate", "timing")}
            for head in self.heads}
        self.fragmented = fragmented
        self.outcome = outcome
        self.version = version
        self.scale_checks = scale_checks
        self.absolute_display = absolute_display
        self.peers = []
        self.applies = 0
        self.overrides = set()
        self.pending = queue.Queue()
        self.errors = queue.Queue()
        self.stopping = threading.Event()
        self.disconnected = threading.Event()
        self.lock = threading.RLock()

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="output-wire-")
        root = Path(self.directory.name)
        (root / "home").mkdir()
        self.path = root / "wayland-test"
        self.environment = patch.dict(os.environ, {
            "HOME": str(root / "home"), "XDG_RUNTIME_DIR": str(root),
            "XDG_CONFIG_HOME": str(root / "home" / ".config"),
            "WAYLAND_DISPLAY": str(self.path) if self.absolute_display else self.path.name,
            "WAYLAND_SOCKET": ""})
        self.environment.start()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(8)
        self.listener.settimeout(0.1)
        self.thread = threading.Thread(target=self._serve, name="private-output-fixture", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, kind, value, traceback):
        self.stopping.set()
        self.listener.close()
        self.thread.join(3)
        for peer in self.peers:
            try:
                peer.client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.client.close()
        for peer in self.peers:
            peer.thread.join(3)
        self.environment.stop()
        self.directory.cleanup()
        if self.thread.is_alive() or any(peer.thread.is_alive() for peer in self.peers):
            raise AssertionError("Private compositor thread did not stop")
        if kind is None and not self.errors.empty():
            raise self.errors.get_nowait()

    def expected(self):
        def public(head):
            return {key: copy.deepcopy(value) for key, value in head.items()
                    if key not in ("policy", "timing", "omitCurrentMode")}
        return {"monitors": [public(h) for h in self.heads if not h["disabled"]],
                "disabledMonitors": [public(h) for h in self.heads if h["disabled"]]}

    def on_sync(self, action):
        """Schedule a server-side external change before the next roundtrip reply."""
        self.pending.put(action)

    def _serve(self):
        try:
            while not self.stopping.is_set():
                try:
                    client, _ = self.listener.accept()
                except socket.timeout:
                    continue
                peer = OutputPeer(self, client, owner=not self.peers)
                self.peers.append(peer)
                peer.thread.start()
        except OSError as error:
            if not self.stopping.is_set():
                self.errors.put(error)


class OutputPeer:
    """Per-connection resource IDs; geometry is shared with discovery probes."""

    def __init__(self, server, client, owner):
        self.server = server
        self.client = client
        self.owner = owner
        self.heads = server.heads
        self.fragmented = server.fragmented
        self.version = server.version
        self.scale_checks = server.scale_checks
        self.stopping = server.stopping
        self.errors = server.errors
        self.objects = {1: ("display", None)}
        self.next_server_id = 0xFF000010
        self.manager = None
        self.serial = 10
        self.wires = {}
        self.overrides = set()
        self.mode_overrides = {}
        self.disconnected = threading.Event()
        self.thread = threading.Thread(target=self._serve, name="private-output-client", daemon=True)

    def wire(self, head):
        return self.wires[id(head)]

    def event(self, object_id, opcode, payload=b""):
        frame = uint(object_id, ((len(payload) + 8) << 16) | opcode) + payload
        if self.fragmented:
            self.client.sendall(frame[:3])
            time.sleep(0.001)
            self.client.sendall(frame[3:7])
            time.sleep(0.001)
            self.client.sendall(frame[7:])
        else:
            self.client.sendall(frame)

    def done(self):
        self.serial += 1
        self.event(self.manager, 1, uint(self.serial))

    def new_id(self):
        result = self.next_server_id
        self.next_server_id += 1
        return result

    def announce(self, head):
        wire = self.wires[id(head)] = {"object": self.new_id()}
        self.event(self.manager, 0, uint(wire["object"]))
        self.event(wire["object"], 0, string(head["name"]))
        self.event(wire["object"], 1, string("Synthetic test panel"))
        self.event(wire["object"], 2, integer(530, 300))
        for opcode, field in ((10, "make"), (11, "model"), (12, "serial")):
            if field in head:
                self.event(wire["object"], opcode, string(head[field]))
        modes = copy.deepcopy(head["modeOptions"])
        active = {field: head[field] for field in ("width", "height", "refreshRate")}
        if active not in modes:
            modes.append(active)
        wire["modes"] = {}
        for mode in modes:
            mode_id = self.new_id()
            wire["modes"][mode_id] = mode
            self.event(wire["object"], 3, uint(mode_id))
            self.event(mode_id, 0, integer(mode["width"], mode["height"]))
            self.event(mode_id, 1, integer(round(mode["refreshRate"] * 1000)))
            if mode == active:
                wire["current_mode"] = mode_id
        self.publish_geometry(head)

    def publish_geometry(self, head):
        wire = self.wire(head)
        self.event(wire["object"], 4, integer(not head["disabled"]))
        if not head["disabled"]:
            if not head.get("omitCurrentMode"):
                self.event(wire["object"], 5, uint(wire["current_mode"]))
            self.event(wire["object"], 6, integer(head["x"], head["y"]))
            self.event(wire["object"], 7, integer(head["transform"]))
            self.event(wire["object"], 8, integer(round(head["scale"] * 256)))

    def replace(self, head, **changes):
        self.event(self.wire(head)["object"], 9)
        head.update(changes)
        self.announce(head)
        self.done()

    def remove(self, head):
        self.event(self.wire(head)["object"], 9)
        self.heads.remove(head)
        self.done()

    def _serve(self):
        try:
            self.client.settimeout(0.1)
            data = bytearray()
            while not self.stopping.is_set():
                try:
                    chunk = self.client.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                data.extend(chunk)
                while len(data) >= 8:
                    object_id, word = struct.unpack_from("=II", data)
                    size, opcode = word >> 16, word & 0xFFFF
                    if size < 8 or size % 4:
                        raise AssertionError("Client sent invalid Wayland framing")
                    if len(data) < size:
                        break
                    payload = bytes(data[8:size])
                    del data[:size]
                    with self.server.lock:
                        self.request(object_id, opcode, payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The client may reject an event while the remaining batch is in flight.
        except OSError as error:
            if not self.stopping.is_set() and not self.disconnected.is_set():
                self.errors.put(error)
        except Exception as error:
            self.errors.put(error)
        finally:
            self.server.overrides.difference_update(self.overrides)
            self.client.close()
            self.disconnected.set()
            if self.owner:
                self.server.disconnected.set()

    def request(self, object_id, opcode, payload):
        kind, value = self.objects[object_id]
        if kind == "display":
            new_id, = struct.unpack("=I", payload)
            if opcode == 1:
                self.objects[new_id] = ("registry", None)
                self.event(new_id, 0, uint(7) + string("zwlr_output_manager_v1") + uint(self.version))
            elif opcode == 0:
                # Probe registry syncs precede manager binding. Apply changes to
                # an already-bound peer so events always target valid resources.
                if self.manager is not None:
                    while not self.server.pending.empty():
                        self.server.pending.get_nowait()(self)
                self.event(new_id, 0, uint(self.serial))
                self.event(1, 1, uint(new_id))
            else:
                raise AssertionError("Unsupported display request")
        elif kind == "registry":
            if opcode != 0:
                raise AssertionError("Unsupported registry request")
            interface, offset = take_string(payload, 4)
            version, new_id = struct.unpack_from("=II", payload, offset)
            if interface != "zwlr_output_manager_v1" or version != 2:
                raise AssertionError("Client must bind output manager version 2")
            self.manager = new_id
            self.objects[new_id] = ("manager", None)
            for head in self.heads:
                self.announce(head)
            self.done()
        elif kind == "manager":
            if opcode == 0:
                new_id, serial = struct.unpack("=II", payload)
                self.objects[new_id] = ("configuration", {})
            elif opcode == 1:
                self.event(object_id, 2)
            else:
                raise AssertionError("Unsupported manager request")
        elif kind == "configuration":
            if opcode == 0:
                new_id, head_id = struct.unpack("=II", payload)
                head = next(h for h in self.heads if self.wire(h)["object"] == head_id)
                if head_id in value:
                    raise AssertionError("Head configured twice")
                settings = {"disabled": False}
                value[head_id] = (head, settings)
                self.objects[new_id] = ("configuration_head", (head, settings))
            elif opcode == 1:
                head_id, = struct.unpack("=I", payload)
                head = next(h for h in self.heads if self.wire(h)["object"] == head_id)
                value[head_id] = (head, {"disabled": True})
            elif opcode == 2:
                self._apply(object_id, value)
            elif opcode == 3:
                self.event(object_id, 0)  # Hyprland's test handler is a stub.
            elif opcode == 4:
                del self.objects[object_id]
                self.event(1, 1, uint(object_id))
            else:
                raise AssertionError("Unsupported configuration request")
        elif kind == "configuration_head":
            head, settings = value
            if opcode == 0:
                mode_id, = struct.unpack("=I", payload)
                settings.update(self.wire(head)["modes"][mode_id])
                settings.update(timing="advertised-mode-timing", _mode=mode_id)
            elif opcode == 1:
                width, height, refresh = struct.unpack("=iii", payload)
                settings.update(width=width, height=height, refreshRate=refresh / 1000,
                                timing="synthesized-custom-timing")
            elif opcode == 2:
                settings["x"], settings["y"] = struct.unpack("=ii", payload)
            elif opcode == 3:
                settings["transform"], = struct.unpack("=i", payload)
            elif opcode == 4:
                fixed, = struct.unpack("=i", payload)
                settings["scale"] = fixed / 256
            elif opcode == 5:
                enabled, = struct.unpack("=I", payload)
                settings["policy"] = dict(head["policy"], vrr=enabled)
            else:
                raise AssertionError("Unsupported geometry request")
        else:
            raise AssertionError("Request targets unknown resource")

    def _apply(self, object_id, configuration):
        self.server.applies += 1
        # Native configuration objects merge flags into the manager-owned override.
        # A failed reload may retain those requested flags without changing pixels.
        for head_id, (_, settings) in configuration.items():
            if "width" in settings:
                self.mode_overrides[head_id] = {
                    key: settings[key] for key in ("width", "height", "refreshRate", "timing")}
        outcome = self.server.outcome
        if outcome in ("failed", "cancelled"):
            self.event(object_id, 1 if outcome == "failed" else 2)
            return
        if outcome == "disconnect":
            self.disconnected.set()
            self.client.shutdown(socket.SHUT_RDWR)
            return
        if outcome == "silent":
            return
        for head_id, (head, settings) in configuration.items():
            mode = self.mode_overrides.get(head_id, self.server.fallback_modes[head["name"]])
            settings = {**mode, **settings}
            settings.pop("_mode", None)
            self.wire(head)["current_mode"] = next(
                mode_id for mode_id, advertised in self.wire(head)["modes"].items()
                if all(advertised[key] == mode[key] for key in ("width", "height", "refreshRate")))
            scale = settings.get("scale", head["scale"])
            width = settings.get("width", head["width"])
            height = settings.get("height", head["height"])
            if self.scale_checks and any(abs(size / scale - round(size / scale)) > 1e-6
                                         for size in (width, height)):
                settings["scale"] = round(scale * 120) / 120
            head.update(settings)
            self.overrides.add(head_id)
            self.server.overrides.add(head_id)
            self.publish_geometry(head)
        self.done()
        self.event(object_id, 0)


class OutputManagementTests(unittest.TestCase):
    def positions(self, expected):
        return [{field: head[field] for field in POSITION} for head in expected["monitors"]]

    def test_geometry_apply_preserves_hidden_policy_and_omitted_heads(self):
        with OutputServer(absolute_display=True) as server:
            baseline = server.expected()
            untouched = copy.deepcopy(server.heads[1:])
            policy = copy.deepcopy(server.heads[0]["policy"])
            target = dict(self.positions(baseline)[0], x=-1200, y=60, width=1600,
                          height=900, refreshRate=75, scale=1.25, transform=2)
            with Connection(timeout=2) as connection:
                connection.check(baseline)
                connection.plan(baseline, [target])
                self.assertEqual(server.applies, 0)
                self.assertEqual(server.expected(), baseline)
                connection.apply(baseline, [target])
                self.assertEqual({key: server.heads[0][key] for key in POSITION}, target)
                self.assertEqual(server.heads[0]["policy"], policy)
                self.assertEqual(server.expected()["monitors"][1:], baseline["monitors"][1:])
                for before, after in zip(untouched, server.heads[1:]):
                    self.assertEqual(after["policy"], before["policy"])
                    self.assertEqual(after["disabled"], before["disabled"])
                self.assertTrue(server.overrides)
                current = server.expected()
                connection.apply(current, [self.positions(baseline)[0]])
                self.assertEqual(server.expected(), baseline)
                self.assertEqual(server.heads[0]["policy"], policy)
            self.assertTrue(server.disconnected.wait(2))
            self.assertEqual(server.overrides, set())


    def test_disconnect_releases_override_without_reverting_kept_geometry(self):
        with OutputServer() as server:
            baseline = server.expected()
            target = dict(self.positions(baseline)[0], x=-120)
            with Connection(timeout=2) as connection:
                connection.apply(baseline, [target])
                self.assertTrue(server.overrides)
            self.assertTrue(server.disconnected.wait(2))
            self.assertEqual(server.overrides, set())
            self.assertEqual(server.heads[0]["x"], -120)
            self.assertEqual(server.heads[0]["policy"],
                             {"icc": "fixture.icc", "hdr": True, "vrr": 2})

    def test_new_owner_preserves_kept_advertised_mode_in_position_only_edit(self):
        with OutputServer() as server:
            baseline = server.expected()
            target = dict(self.positions(baseline)[0], width=1600, height=900, refreshRate=75)
            with Connection(timeout=2) as first:
                first.apply(baseline, [target])
            self.assertTrue(server.disconnected.wait(2))
            kept = server.expected()
            moved = dict(self.positions(kept)[0], x=-200)
            with Connection(timeout=2) as second:
                second.apply(kept, [moved])
                self.assertEqual({key: server.heads[0][key] for key in POSITION}, moved)
                self.assertEqual(server.heads[0]["policy"],
                                 {"icc": "fixture.icc", "hdr": True, "vrr": 2})

    def test_rollback_clears_failed_mode_override_when_live_mode_never_changed(self):
        with OutputServer(outcome="failed") as server:
            baseline = server.expected()
            target = dict(self.positions(baseline)[0], width=1600, height=900, refreshRate=75)
            with Connection(timeout=2) as connection:
                with self.assertRaises(ValueError):
                    connection.apply(baseline, [target])
                self.assertEqual(server.expected(), baseline)
                server.outcome = "success"
                connection.apply(baseline, [self.positions(baseline)[0]])
                self.assertEqual(server.expected(), baseline)
                self.assertEqual(server.heads[0]["policy"],
                                 {"icc": "fixture.icc", "hdr": True, "vrr": 2})

    def test_fragmented_events_and_optional_identity_support_fresh_check(self):
        head = monitor("DP-7")
        for field in ("make", "model", "serial"):
            del head[field]
        with OutputServer([head], fragmented=True) as server:
            baseline = server.expected()
            target = dict(self.positions(baseline)[0], x=120)
            with Connection(timeout=2) as connection:
                connection.check(baseline)
                connection.apply(baseline, [target])
                connection.check(server.expected())
                self.assertEqual(server.heads[0]["x"], 120)

    def test_apply_rechecks_identity_geometry_and_disappearance_after_preflight(self):
        for change in ("identity", "position", "lost"):
            with self.subTest(change=change), OutputServer() as server:
                baseline = server.expected()
                target = dict(self.positions(baseline)[0], x=120)
                with Connection(timeout=2) as connection:
                    connection.plan(baseline, [target])
                    if change == "identity":
                        server.on_sync(lambda s: s.replace(s.heads[0], serial="replacement-private-serial"))
                    elif change == "position":
                        # Native soft reconfigurations need not emit head events.
                        with server.lock:
                            server.heads[0]["x"] = 30
                    else:
                        server.on_sync(lambda s: s.remove(s.heads[0]))
                    with self.assertRaises(ValueError) as failure:
                        connection.apply(baseline, [target])
                    self.assertNotIn("replacement-private-serial", str(failure.exception))
                    self.assertNotIn("synthetic-DP-7", str(failure.exception))
                    self.assertEqual(server.applies, 0)

    def test_unexpected_enabled_head_cannot_be_ignored(self):
        with OutputServer() as server:
            baseline = server.expected()
            with Connection(timeout=2) as connection:
                def enable(s):
                    s.heads[2]["disabled"] = False
                    s.publish_geometry(s.heads[2])
                    s.done()
                server.on_sync(enable)
                with self.assertRaises(ValueError):
                    connection.apply(baseline, self.positions(baseline))
                self.assertEqual(server.applies, 0)
                self.assertFalse(server.heads[2]["disabled"])

    def test_failure_cancellation_disconnect_and_timeout_raise_without_success(self):
        for outcome in ("failed", "cancelled", "disconnect", "silent"):
            with self.subTest(outcome=outcome), OutputServer(outcome=outcome) as server:
                baseline = server.expected()
                target = dict(self.positions(baseline)[0], x=120)
                with Connection(timeout=0.25) as connection:
                    with self.assertRaises(ValueError):
                        connection.apply(baseline, [target])
                    self.assertEqual(server.expected(), baseline)

    def test_finished_manager_is_not_used_for_configuration(self):
        with OutputServer() as server:
            baseline = server.expected()
            with Connection(timeout=2) as connection:
                server.on_sync(lambda s: s.event(s.manager, 2))
                with self.assertRaises(ValueError):
                    connection.apply(baseline, self.positions(baseline))
                self.assertEqual(server.applies, 0)

    def test_owner_dispatch_reports_finished_resources_and_protocol_errors(self):
        for failure in ("finished", "protocol"):
            with self.subTest(failure=failure), OutputServer() as server:
                with Connection(timeout=2) as connection:
                    peer = server.peers[0]
                    with server.lock:
                        if failure == "finished":
                            peer.event(peer.manager, 2)
                        else:
                            peer.event(1, 0, uint(peer.manager, 1) + string("private-serial-detail"))
                    ready, _, _ = select.select([connection.fileno()], [], [], 1)
                    self.assertTrue(ready, "Owner socket must become readable")
                    with self.assertRaises(ValueError) as error:
                        connection.dispatch()
                    self.assertNotIn("private-serial-detail", str(error.exception))
                    self.assertEqual(server.applies, 0)

    def test_manager_without_identity_events_is_unsupported(self):
        with OutputServer(version=1):
            with self.assertRaises(ValueError):
                with Connection(timeout=0.25):
                    pass

    def test_custom_active_timing_survives_position_edit_but_new_custom_mode_is_rejected(self):
        head = monitor("DP-7")
        head.update(width=2048, height=1152, refreshRate=59.987)
        with OutputServer([head]) as server:
            baseline = server.expected()
            target = dict(self.positions(baseline)[0], x=120)
            timing = server.heads[0]["timing"]
            with Connection(timeout=2) as connection:
                connection.apply(baseline, [target])
                self.assertEqual(server.heads[0]["timing"], timing)
                self.assertEqual(server.heads[0]["refreshRate"], 59.987)
                current = server.expected()
                new_custom = dict(target, width=2000, height=1000)
                with self.assertRaises(ValueError):
                    connection.plan(current, [new_custom])
                with self.assertRaises(ValueError):
                    connection.apply(current, [new_custom])
                self.assertEqual(server.applies, 1)
                self.assertEqual(server.heads[0]["timing"], timing)

    def test_missing_active_mode_and_irrecoverable_custom_mode_switch_fail_closed(self):
        for missing in (False, True):
            with self.subTest(missing_active_mode=missing):
                head = monitor("DP-7")
                head.update(width=2048, height=1152, refreshRate=59.987, omitCurrentMode=missing)
                with OutputServer([head]) as server:
                    baseline = server.expected()
                    target = dict(self.positions(baseline)[0], width=1600, height=900,
                                  refreshRate=75)
                    with Connection(timeout=2) as connection:
                        with self.assertRaises(ValueError):
                            connection.apply(baseline, [target])
                        self.assertEqual(server.applies, 0)
                        self.assertEqual(server.expected(), baseline)

    def test_native_scale_correction_recovers_fraction_and_preserves_rollback(self):
        head = monitor("DP-7")
        head.update(width=1920, height=1080, scale=4 / 3)
        with OutputServer([head]) as server:
            baseline = server.expected()
            target = dict(self.positions(baseline)[0], x=120)
            with Connection(timeout=2) as connection:
                connection.check(baseline)
                connection.apply(baseline, [target], scale_checks=True)
                self.assertEqual(server.heads[0]["scale"], 4 / 3)
                connection.check(server.expected())
                connection.apply(server.expected(), self.positions(baseline), scale_checks=True)
                self.assertEqual(server.expected(), baseline)

    def test_unsafe_target_or_rollback_scale_is_rejected_before_mutation(self):
        cases = [(1, 4 / 3, False), (4 / 3, 1, False), (1, 12, True),
                 (12, 1, True), (1, 1.333, True)]
        for original, target_scale, scale_checks in cases:
            with self.subTest(original=original, target=target_scale, checks=scale_checks):
                head = monitor("DP-7")
                head["scale"] = original
                with OutputServer([head], scale_checks=scale_checks) as server:
                    baseline = server.expected()
                    target = dict(self.positions(baseline)[0], x=120, scale=target_scale)
                    with Connection(timeout=2) as connection:
                        with self.assertRaises(ValueError):
                            connection.plan(baseline, [target], scale_checks=scale_checks)
                        with self.assertRaises(ValueError):
                            connection.apply(baseline, [target], scale_checks=scale_checks)
                        self.assertEqual(server.applies, 0)
                        self.assertEqual(server.expected(), baseline)


if __name__ == "__main__":
    unittest.main()
