"""Session-owner/watchdog contracts over isolated Unix and Wayland sockets."""
import contextlib
import copy
import os
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch

import apply as arrangement
import output_owner
from test_output_management import OutputServer


class OutputOwnerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.server = self.stack.enter_context(OutputServer())
        self.stack.enter_context(patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "isolated-output-owner"}))
        self.root = arrangement.runtime_dir()
        self.config = Path(os.environ["XDG_CONFIG_HOME"]) / "hypr"
        self.config.mkdir(parents=True)
        (self.config.parent / "omarchy").mkdir()
        self.monitor_file = self.config / "monitors.lua"
        self.monitor_file.write_text('hl.monitor({ output = "DP-7", mode = "1920x1080@60", scale = 1 })\n')
        self.original = self.monitor_file.read_bytes()
        self.stack.enter_context(patch.object(arrangement, "query", side_effect=self.query))
        self.stack.enter_context(patch.object(arrangement, "run", side_effect=self.run_command))
        self.stack.enter_context(patch.object(arrangement, "lua_eval", side_effect=AssertionError("No Lua monitor mutations")))
        self.event_path = Path(os.environ["XDG_RUNTIME_DIR"]) / "hypr" / os.environ["HYPRLAND_INSTANCE_SIGNATURE"] / ".socket2.sock"
        self.event_path.parent.mkdir(parents=True)
        self.event_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.event_listener.bind(str(self.event_path))
        self.event_listener.listen(4)
        self.event_listener.settimeout(3)
        self.events = []
        self.owners = []
        self.failures = []
        self.token = 0
        self.addCleanup(self.close_owners)
        self.start_owner()

    def query(self, kind, *args):
        if kind == "getoption":
            return {"bool": False}
        if kind == "monitors":
            with self.server.lock:
                monitors = copy.deepcopy(self.server.heads)
            for index, monitor in enumerate(monitors):
                monitor["id"] = index
                monitor["availableModes"] = [f"{m['width']}x{m['height']}@{m['refreshRate']}Hz"
                                             for m in monitor["modeOptions"]]
                monitor.pop("policy", None)
                monitor.pop("timing", None)
            return monitors
        if kind == "workspaces":
            return [{"id": 1, "monitor": "DP-7", "windows": 1, "ispersistent": False},
                    {"id": 2, "monitor": "HDMI-A-3", "windows": 1, "ispersistent": False}]
        if kind == "activewindow":
            return {}
        raise AssertionError(kind)

    def run_command(self, args, **kwargs):
        if args[:2] == ["hyprctl", "repl"]:
            return "true\t" + str(self.monitor_file)
        raise AssertionError("Unexpected external command: " + str(args))


    def start_owner(self):
        def serve():
            try:
                output_owner.serve(arrangement, self.root)
            except Exception as error:
                self.failures.append(error)
        owner = threading.Thread(target=serve, name="isolated-session-owner", daemon=True)
        self.owners.append(owner)
        owner.start()
        try:
            stream, _ = self.event_listener.accept()
        except TimeoutError:
            self.fail("Isolated owner did not connect: " + str(self.failures))
        self.events.append(stream)
        deadline = time.monotonic() + 3
        while True:
            try:
                self.generation = output_owner.call(self.root, {"command": "status"})["generation"]
                return
            except OSError:
                if not owner.is_alive() or time.monotonic() >= deadline:
                    self.fail("Isolated owner failed to start: " + str(self.failures))
                time.sleep(0.01)

    def close_owners(self):
        if any(owner.is_alive() for owner in self.owners):
            with contextlib.suppress(OSError, ValueError):
                output_owner.call(self.root, {"command": "release", "generation": self.generation})
        for stream in self.events:
            stream.close()
        self.event_listener.close()
        for owner in self.owners:
            owner.join(3)
        self.assertFalse(any(owner.is_alive() for owner in self.owners))

    def trial(self, action, y=120):
        baseline = arrangement.snapshot()
        positions = [{key: monitor[key] for key in arrangement.POSITION} for monitor in baseline["monitors"]]
        positions[1]["y"] = y
        request = {"baseline": baseline, "positions": positions, "workspaces": []}
        plan = arrangement.validate(request, baseline)
        self.token += 1
        token = f"{self.token:048x}"
        state = {"token": token, "state": "starting", "phase": "prepared", "request": "",
                 "message": "Starting", "mutationStarted": False, "baseline": baseline,
                 "plan": plan, "monitorBackend": "output-management", "automatic": False}
        arrangement.save(self.root, state)
        clock = [100.0]
        acted = False
        def tick(seconds):
            nonlocal acted
            current = arrangement.load(self.root, token)
            if current["state"] == "pending" and not acted:
                acted = True
                if action == "external":
                    with self.server.lock:
                        self.server.heads[0]["x"] = 17
                else:
                    arrangement.control(action, token)
            clock[0] += 4
        with patch.object(arrangement.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(arrangement.time, "sleep", side_effect=tick):
            arrangement.worker(self.root, token)
        return arrangement.load(self.root, token), baseline

    def test_manual_new_connector_validation_is_read_only_and_automatic_remains_blocked(self):
        baseline = arrangement.snapshot()
        positions = [{key: m[key] for key in arrangement.POSITION} for m in baseline["monitors"]]
        positions[1]["y"] = 120
        request = {"baseline": baseline, "positions": positions, "workspaces": []}
        result = arrangement.validate(request)
        self.assertTrue(result["ok"])
        with self.assertRaises(ValueError):
            arrangement.validate(request, allow_new=False)
        self.assertEqual(arrangement.snapshot(), baseline)
        self.assertEqual(self.server.overrides, set())
        self.assertEqual(self.monitor_file.read_bytes(), self.original)

    def test_cancel_restores_geometry_and_releases_new_owner(self):
        state, baseline = self.trial("revert")
        self.assertEqual(state["state"], "reverted", state["message"])
        self.assertEqual(arrangement.snapshot(), baseline)
        self.owners[-1].join(3)
        self.assertFalse(self.owners[-1].is_alive())
        self.assertTrue(self.server.disconnected.wait(3))
        self.assertEqual(self.server.overrides, set())
        self.assertEqual(self.monitor_file.read_bytes(), self.original)
        self.assertFalse(self.failures)

    def test_keep_retains_owner_and_later_cancel_restores_previous_kept_layout(self):
        state, _ = self.trial("keep")
        self.assertEqual(state["state"], "kept", state["message"])
        kept = arrangement.snapshot()
        self.assertEqual(kept["monitors"][1]["y"], 120)
        self.assertTrue(self.owners[-1].is_alive())
        self.assertTrue(self.server.overrides)
        reverted, _ = self.trial("revert", y=240)
        self.assertEqual(reverted["state"], "reverted", reverted["message"])
        self.assertEqual(arrangement.snapshot(), kept)
        self.assertTrue(self.owners[-1].is_alive())
        self.assertTrue(self.server.overrides)
        self.assertEqual(self.monitor_file.read_bytes(), self.original)
        self.assertFalse(self.failures)

    def test_external_geometry_is_not_overwritten_during_recovery(self):
        state, baseline = self.trial("external")
        self.assertEqual(state["state"], "failed")
        current = arrangement.snapshot()
        self.assertEqual(current["monitors"][0]["x"], 17)
        self.assertEqual(current["monitors"][1], baseline["monitors"][1])
        self.assertEqual(self.monitor_file.read_bytes(), self.original)
        self.assertEqual(self.server.heads[0]["policy"], {"icc": "fixture.icc", "hdr": True, "vrr": 2})

    def test_configuration_reload_releases_owner_and_rejects_stale_worker(self):
        state, baseline = self.trial("keep")
        self.assertEqual(state["state"], "kept", state["message"])
        self.events[-1].sendall(b"configreloaded>>\n")
        self.owners[-1].join(3)
        self.assertFalse(self.owners[-1].is_alive())
        current = arrangement.snapshot()
        with self.assertRaises(ValueError):
            output_owner.apply(arrangement, self.root, current,
                               [{key: m[key] for key in arrangement.POSITION} for m in baseline["monitors"]],
                               self.generation)
        self.assertEqual(arrangement.snapshot(), current)
        self.assertEqual(self.monitor_file.read_bytes(), self.original)

    def test_owner_restart_adopts_kept_geometry_without_replaying_an_old_layout(self):
        state, _ = self.trial("keep")
        self.assertEqual(state["state"], "kept", state["message"])
        before = arrangement.snapshot()
        generation = self.generation
        peer = next(peer for peer in self.server.peers if peer.overrides)
        peer.disconnected.set()
        peer.client.shutdown(socket.SHUT_RDWR)
        self.owners[-1].join(3)
        self.assertFalse(self.owners[-1].is_alive())
        self.assertEqual(len(self.failures), 1)
        self.start_owner()
        self.assertEqual(self.generation, generation)
        self.assertEqual(arrangement.snapshot(), before)
        self.assertTrue(self.server.overrides)
        self.assertEqual(self.monitor_file.read_bytes(), self.original)



    def test_resume_release_stops_unreachable_owner_without_restart(self):
        state = {"version": 1, "generation": "a" * 48, "stopped": False}
        with patch.object(output_owner, "load", side_effect=[state, state]), \
                patch.object(output_owner, "call", side_effect=OSError("missing socket")), \
                patch.object(arrangement, "run") as run, \
                patch.object(arrangement, "atomic") as atomic:
            output_owner.release_after_resume(arrangement, self.root)
        run.assert_called_once_with(
            ["systemctl", "--user", "stop",
             "display-workspaces-output-" + self.root.name.rsplit("-", 1)[-1] + ".service"],
            timeout=10)
        saved = atomic.call_args.args[2]
        self.assertTrue(saved["stopped"])
        self.assertIn("resumed", saved["reason"])

if __name__ == "__main__":
    unittest.main()
