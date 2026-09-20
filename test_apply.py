"""Guardrails whose failure could strand a display; no compositor mutations."""
import contextlib
import copy
import json
import os
import re
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import apply as arrangement


class LayoutSafetyTests(unittest.TestCase):
    def setUp(self):
        self.raw = [
            {"id": 0, "name": "A", "width": 2560, "height": 1600, "scale": 2,
             "transform": 0, "refreshRate": 60, "x": 0, "y": 0, "serial": "a",
             "availableModes": ["2560x1600@60Hz", "1920x1200@75Hz", "1920x1080@60Hz"]},
            {"id": 1, "name": "B", "width": 1920, "height": 1080, "scale": 1.5,
             "transform": 1, "refreshRate": 60, "x": 1280, "y": 0, "serial": "b",
             "availableModes": ["1920x1080@60Hz", "1600x900@75Hz"]},
        ]
        self.current = self.discover()
        self.plan = {"baseline": copy.deepcopy(self.current),
                     "positions": [{key: monitor[key] for key in arrangement.POSITION}
                                   for monitor in self.current["monitors"]],
                     "workspaces": []}

    def discover(self, workspaces=None):
        def query(kind, *args):
            return {"monitors": self.raw,
                    "workspaces": workspaces if workspaces is not None else [{"id": 1, "monitor": "A"}],
                    "activewindow": {}}[kind]
        with patch.object(arrangement, "query", side_effect=query):
            return arrangement.snapshot()

    @contextlib.contextmanager
    def compositor(self, fail_after=None):
        """Apply monitor IPC to isolated state; never invoke a real compositor."""
        batches = []
        failing = fail_after is not None

        def execute(code):
            nonlocal failing
            rules = re.findall(r"hl\.monitor\(\{([^}]+)\}\)", code)
            if not rules:
                return
            batches.append(len(rules))
            for index, rule in enumerate(rules):
                if failing and index == fail_after:
                    failing = False
                    raise ValueError("Synthetic monitor apply failure")
                fields = dict(re.findall(r'(\w+)=("[^"]*"|[^,]+)', rule))
                fields = {key: json.loads(value) for key, value in fields.items()}
                monitor = next(m for m in self.current["monitors"] if m["name"] == fields["output"])
                width, height, refresh = re.fullmatch(r"(\d+)x(\d+)@([\d.]+)", fields["mode"]).groups()
                x, y = fields["position"].split("x")
                monitor.update(width=int(width), height=int(height), refreshRate=float(refresh),
                               scale=fields["scale"], transform=fields["transform"], x=int(x), y=int(y))
                monitor["logicalWidth"], monitor["logicalHeight"] = arrangement.logical_size(monitor, monitor["transform"])

        with patch.object(arrangement, "snapshot", side_effect=lambda: copy.deepcopy(self.current)), \
                patch.object(arrangement, "lua_eval", side_effect=execute), \
                patch.object(arrangement, "check_monitor_rules", return_value=[]):
            yield batches

    def mode_plan(self):
        self.plan["positions"][0].update(width=1920, height=1200, refreshRate=75, scale=1.5)
        self.plan["positions"][1].update(width=1600, height=900, refreshRate=75, scale=1.25)
        return self.validate()


    def test_empty_nonpersistent_workspace_can_expire_during_preview(self):
        baseline = copy.deepcopy(self.current)
        baseline["workspaces"].append({"id": 8, "monitor": "A", "windows": 0, "ispersistent": False})
        self.current["workspaces"][0]["monitor"] = "B"
        with self.compositor():
            arrangement.verify_arrangement(baseline, self.plan["positions"],
                                           [{"id": 1, "source": "A", "target": "B"}])
        self.assertEqual(self.current["workspaces"], [{"id": 1, "monitor": "B"}])

    def test_missing_populated_persistent_or_unknown_workspace_still_fails(self):
        for metadata in ({"windows": 1, "ispersistent": False},
                         {"windows": 0, "ispersistent": True}, {"windows": 0}, {}):
            with self.subTest(metadata=metadata):
                baseline = copy.deepcopy(self.current)
                baseline["workspaces"].append({"id": 8, "monitor": "A", **metadata})
                with self.compositor():
                    with self.assertRaisesRegex(ValueError, "Workspace 8"):
                        arrangement.verify_arrangement(baseline, self.plan["positions"], [])
                    with self.assertRaisesRegex(ValueError, "Workspace 8 disappeared"):
                        arrangement.move_workspace(baseline, 8, "B")

    def test_surviving_empty_workspace_must_retain_its_display(self):
        baseline = copy.deepcopy(self.current)
        baseline["workspaces"].append({"id": 8, "monitor": "A", "windows": 0, "ispersistent": False})
        self.current["workspaces"].append({"id": 8, "monitor": "B", "windows": 0, "ispersistent": False})
        with self.compositor():
            with self.assertRaisesRegex(ValueError, "Workspace 8"):
                arrangement.verify_arrangement(baseline, self.plan["positions"], [])
            with self.assertRaisesRegex(ValueError, "moved externally"):
                arrangement.move_workspace(baseline, 8, "B", expected_source="A")

    def test_expired_empty_workspace_is_not_recreated_by_move_or_rollback(self):
        baseline = copy.deepcopy(self.current)
        baseline["workspaces"].append({"id": 8, "monitor": "A", "windows": 0, "ispersistent": False})
        before = copy.deepcopy(self.current)
        with self.compositor():
            arrangement.move_workspace(baseline, 8, "B", expected_source="A")
            errors = arrangement.rollback({"baseline": baseline,
                                           "plan": {"displayChanges": 0, "positions": self.plan["positions"]}})
        self.assertEqual(errors, [])
        self.assertEqual(self.current, before)

    def test_faulty_output_preserves_healthy_outputs_and_all_workspaces(self):
        self.raw[1]["width"] = 0
        self.raw.extend([{"name": "off", "disabled": True, "width": 0},
                         {"name": "mirror", "mirrorOf": "A", "width": 0}])
        workspaces = [{"id": 1, "monitor": "A"}, {"id": 2, "monitor": "B"}]
        result = json.loads(json.dumps(self.discover(workspaces), allow_nan=False))
        self.assertTrue(result["ok"])
        self.assertEqual(result["monitors"], self.current["monitors"][:1])
        self.assertEqual(result["workspaces"], workspaces)
        self.assertEqual(result["disabledMonitors"], self.raw[2:3])
        self.assertEqual(result["mirroredOutputs"], ["mirror"])
        self.assertEqual(result["unavailableMonitors"][0]["name"], "B")
        self.assertEqual(result["unavailableMonitors"][0]["serial"], "b")
        self.assertIn("width", result["unavailableMonitors"][0]["reason"])

    def test_all_outputs_unusable_still_returns_workspace_discovery(self):
        for monitor in self.raw:
            monitor["height"] = 0
        result = self.discover()
        self.assertTrue(result["ok"])
        self.assertEqual(result["monitors"], [])
        self.assertEqual({m["name"] for m in result["unavailableMonitors"]}, {"A", "B"})
        self.assertEqual(result["workspaces"], self.current["workspaces"])

    def test_missing_nonfinite_and_zero_logical_geometry_is_json_safe(self):
        original = copy.deepcopy(self.raw[1])
        cases = [
            ("width", None, "width"), ("height", float("inf"), "height"),
            ("scale", float("nan"), "scale"), ("transform", float("-inf"), "rotation"),
            ("x", None, "X coordinate"), ("y", float("nan"), "Y coordinate"),
            ("x", arrangement.COORD_LIMIT + 1, "X coordinate"), ("y", 0.5, "whole number"),
            ("width", 1, "logical display"),
        ]
        for field, value, reason in cases:
            with self.subTest(field=field, value=value):
                self.raw[1] = dict(original, id=float("nan"), serial=float("inf"),
                                   description={"nested": float("nan")})
                if value is None:
                    del self.raw[1][field]
                else:
                    self.raw[1][field] = value
                if field == "width" and value == 1:
                    self.raw[1]["scale"] = 16
                result = json.loads(json.dumps(self.discover(), allow_nan=False))
                self.assertEqual(result["monitors"], self.current["monitors"][:1])
                unavailable = result["unavailableMonitors"][0]
                self.assertEqual(unavailable["name"], "B")
                self.assertIn(reason, unavailable["reason"])
                self.assertNotIn("logicalWidth", unavailable)
                self.assertIsNone(unavailable["id"])
                self.assertIsNone(unavailable["serial"])
                self.assertIsNone(unavailable["description"])

    def test_ipc_failure_is_not_partial_discovery(self):
        self.raw[1]["width"] = 0
        def query(kind, *args):
            if kind == "monitors":
                return self.raw
            raise RuntimeError("IPC unavailable")
        with patch.object(arrangement, "query", side_effect=query):
            with self.assertRaisesRegex(RuntimeError, "IPC unavailable"):
                arrangement.snapshot()

    def test_degraded_baseline_requires_refresh_after_output_recovers(self):
        self.raw[1]["width"] = 0
        self.plan["baseline"] = self.discover()
        with self.assertRaisesRegex(ValueError, "B"):
            self.validate()
        self.raw[1]["width"] = 1920
        self.plan["baseline"] = self.discover()
        self.assertTrue(self.validate()["ok"])

    def test_omitting_faulty_output_cannot_begin_or_keep_trial(self):
        self.raw[1]["width"] = 0
        degraded = self.discover()
        self.plan["baseline"] = copy.deepcopy(degraded)
        self.plan["baseline"].pop("unavailableMonitors")
        self.plan["positions"] = self.plan["positions"][:1]
        token = "b" * 48
        state = {"token": token, "state": "pending", "phase": "waiting", "request": "",
                 "deadline": 0, "message": "Preview", "baseline": self.plan["baseline"],
                 "plan": self.plan}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(arrangement, "runtime_dir", return_value=root):
                with patch.object(arrangement, "snapshot", return_value=degraded):
                    with self.assertRaisesRegex(ValueError, "B"):
                        arrangement.begin(self.plan)
                    self.assertFalse((root / "active.json").exists())
                    arrangement.save(root, state)
                    with self.assertRaisesRegex(ValueError, "B"):
                        arrangement.control("keep", token)
                    self.assertEqual(arrangement.load(root, token)["request"], "")

    def test_output_failure_between_preflight_and_mutation_blocks_changes(self):
        self.raw[1]["width"] = 0
        degraded = self.discover()
        with patch.object(arrangement, "snapshot", return_value=degraded):
            with patch.object(arrangement, "lua_eval") as mutate:
                with self.assertRaisesRegex(ValueError, "B"):
                    arrangement.set_positions(self.current, [dict(self.plan["positions"][0], y=50)])
                with self.assertRaisesRegex(ValueError, "B"):
                    arrangement.move_workspace(self.current, 1, "A")
                with self.assertRaisesRegex(ValueError, "B"):
                    arrangement.restore_view(self.current)
                mutate.assert_not_called()

    def test_rollback_restores_healthy_output_while_reporting_faulty_output(self):
        self.plan["positions"][0]["y"] = 50
        self.current["monitors"][0]["y"] = 50
        self.current["monitors"].pop()
        self.current["unavailableMonitors"] = [{"name": "B", "reason": "Display geometry is unavailable."}]
        with self.compositor(), patch.object(arrangement.time, "monotonic", side_effect=[0, 4]):
            errors = arrangement.rollback({"baseline": self.plan["baseline"],
                                           "plan": {**self.plan, "displayChanges": 1}})
        self.assertEqual(self.current["monitors"][0]["y"], 0)
        self.assertTrue(any("B" in error and "unavailable" in error for error in errors))

    def validate(self):
        return arrangement.validate(self.plan, self.current)

    def test_scaled_rotated_edge_contact_is_valid(self):
        self.assertTrue(self.validate()["ok"])
        # Rotated B is 720 logical pixels wide, so C can touch its right edge.
        c = dict(self.current["monitors"][0], id=2, name="C", x=2000, y=0, serial="c")
        self.current["monitors"].append(c)
        self.plan["baseline"] = copy.deepcopy(self.current)
        self.plan["positions"].append({key: c[key] for key in arrangement.POSITION})
        self.assertTrue(self.validate()["ok"])
        self.plan["positions"][2]["x"] = 1999
        with self.assertRaises(ValueError):
            self.validate()

    def test_one_pixel_overlap_is_rejected(self):
        self.plan["positions"][1]["x"] = 1279
        with self.assertRaises(ValueError):
            self.validate()

    def test_corner_contact_is_not_a_connected_desktop(self):
        self.plan["positions"][1]["y"] = 800
        with self.assertRaises(ValueError):
            self.validate()

    def test_gap_is_rejected(self):
        self.plan["positions"][1]["x"] = 1281
        with self.assertRaises(ValueError):
            self.validate()

    def test_negative_origin_is_normalized_without_layout_change(self):
        for p in self.plan["positions"]:
            p["x"] -= 1280
            p["y"] -= 400
        result = self.validate()
        self.assertEqual(result["displayChanges"], 0)
        self.assertEqual(result["positions"], [{key: m[key] for key in arrangement.POSITION}
                                               for m in self.current["monitors"]])

    def test_changed_hardware_identity_invalidates_preview(self):
        self.current["monitors"][1]["serial"] = "replacement"
        with self.assertRaises(ValueError):
            self.validate()

    def test_stale_workspace_source_is_rejected(self):
        self.plan["workspaces"] = [{"id": 1, "source": "A", "target": "B"}]
        self.current["workspaces"][0]["monitor"] = "B"
        with self.assertRaises(ValueError):
            self.validate()

    def test_nonfinite_position_is_rejected(self):
        self.plan["positions"][1]["x"] = float("nan")
        with self.assertRaises(ValueError):
            self.validate()

    def test_unrequested_workspace_drift_invalidates_trial(self):
        self.current["workspaces"][0]["monitor"] = "B"
        with patch.object(arrangement, "snapshot", return_value=self.current):
            with self.assertRaises(ValueError):
                arrangement.verify_arrangement(self.plan["baseline"], self.plan["positions"], [])

    def test_rollback_does_not_expose_overlapping_intermediate_layout(self):
        baseline = copy.deepcopy(self.current)
        self.current["monitors"][0].update(x=0, y=1280)
        self.current["monitors"][1].update(x=0, y=0)
        positions = [{key: m[key] for key in arrangement.POSITION} for m in self.current["monitors"]]
        with self.compositor() as batches:
            errors = arrangement.rollback({"baseline": baseline,
                                           "plan": {"displayChanges": 2, "positions": positions}})
        self.assertEqual(errors, [])
        self.assertEqual(batches, [2])
        self.assertEqual(self.current["monitors"], baseline["monitors"])

    def test_discovery_filters_and_deduplicates_advertised_modes(self):
        self.raw[0]["availableModes"] = [
            "1920x1080@59.94Hz", "01920x01080@59.940000", "1920x1080@60.000001Hz",
            "0x1080@60Hz", "1920x1080@nanHz", "preferred", {}, "1920x1080@60Hz;disable",
        ]
        monitor = self.discover()["monitors"][0]
        options = monitor["modeOptions"]
        self.assertEqual({mode["value"] for mode in options},
                         {"1920x1080@59.94", "1920x1080@60.000001"})
        self.assertEqual([(mode["width"], mode["height"], mode["refreshRate"]) for mode in options],
                         [(1920, 1080, 60.000001), (1920, 1080, 59.94)])
        self.assertEqual((monitor["width"], monitor["height"], monitor["refreshRate"]), (2560, 1600, 60))

    def test_changed_mode_and_scale_use_draft_geometry_for_edge_checks(self):
        self.plan["positions"][0].update(width=1920, height=1080, scale=2)
        with self.assertRaisesRegex(ValueError, "share an edge"):
            self.validate()
        self.plan["positions"][1]["x"] = 960
        self.assertEqual(self.validate()["displayChanges"], 2)
        self.plan["positions"][1]["x"] = 959
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.validate()

    def test_profile_rotation_changes_geometry_and_rolls_back(self):
        self.plan["positions"][0]["transform"] = 1
        with self.assertRaisesRegex(ValueError, "share an edge"):
            self.validate()
        self.plan["positions"][1]["x"] = 800
        plan = self.validate()
        baseline = self.plan["baseline"]
        with self.compositor():
            arrangement.set_positions(baseline, plan["positions"])
            arrangement.verify_arrangement(baseline, plan["positions"], [])
            self.assertEqual(self.current["monitors"][0]["transform"], 1)
            self.assertEqual(self.current["monitors"][0]["logicalWidth"], 800)
            self.assertEqual(arrangement.rollback({"baseline": baseline, "plan": plan}), [])
        self.assertEqual(self.current["monitors"], baseline["monitors"])

    def test_invalid_profile_rotation_is_rejected_before_mutation(self):
        self.plan["positions"][0]["transform"] = 8
        with self.assertRaisesRegex(ValueError, "rotation"):
            self.validate()

    def test_unadvertised_mode_and_changed_catalog_are_rejected(self):
        self.plan["positions"][0]["refreshRate"] = 120
        with self.assertRaisesRegex(ValueError, "not advertised"):
            self.validate()
        self.plan["positions"][0]["refreshRate"] = 60
        self.current["monitors"][0]["modeOptions"].pop()
        with self.assertRaisesRegex(ValueError, "advertised modes changed"):
            self.validate()

    def test_current_custom_timing_is_preserved_and_rounded_rates_match(self):
        self.raw[0]["refreshRate"] = 59.95123
        self.current = self.discover()
        self.plan["baseline"] = copy.deepcopy(self.current)
        self.plan["positions"][0]["refreshRate"] = 59.95
        result = self.validate()
        self.assertEqual(result["positions"][0]["refreshRate"], 59.95123)
        self.assertEqual(result["displayChanges"], 0)
        plan = self.mode_plan()
        with self.compositor():
            arrangement.set_positions(self.plan["baseline"], plan["positions"])
            self.current["monitors"][0]["refreshRate"] = 75.004
            arrangement.verify_arrangement(self.plan["baseline"], plan["positions"], [])
            self.current["monitors"][0]["refreshRate"] = 75.02
            with self.assertRaises(ValueError):
                arrangement.verify_arrangement(self.plan["baseline"], plan["positions"], [])

    def test_scale_requires_quantization_and_integral_logical_pixels(self):
        for scale, reason in ((1.3333, "1/120"), (1.3, "integral"), ("auto", "Invalid")):
            with self.subTest(scale=scale):
                self.plan["positions"][0]["scale"] = scale
                with self.assertRaisesRegex(ValueError, reason):
                    self.validate()

    def test_mode_scale_rollback_preserves_foreign_settings(self):
        plan = self.mode_plan()
        baseline = self.plan["baseline"]
        with self.compositor():
            arrangement.set_positions(baseline, plan["positions"])
            self.current["monitors"][0]["sdrBrightness"] = 0.7
            with patch.object(arrangement.time, "monotonic", side_effect=[0, 4]):
                errors = arrangement.rollback({"baseline": baseline, "plan": plan})
        self.assertTrue(errors)
        for old, actual in zip(baseline["monitors"], self.current["monitors"]):
            self.assertEqual({key: actual[key] for key in arrangement.POSITION},
                             {key: old[key] for key in arrangement.POSITION})
        self.assertEqual(self.current["monitors"][0]["sdrBrightness"], 0.7)

    def test_rollback_preserves_external_geometry_and_replaced_identity(self):
        plan = self.mode_plan()
        baseline = self.plan["baseline"]
        for change in ({"x": 12}, {"refreshRate": 80}, {"scale": 2}, {"transform": 2}, {"serial": "replacement"}):
            with self.subTest(change=change):
                self.current = copy.deepcopy(baseline)
                with self.compositor():
                    arrangement.set_positions(baseline, plan["positions"])
                    self.current["monitors"][0].update(change)
                    external = copy.deepcopy(self.current["monitors"][0])
                    with patch.object(arrangement.time, "monotonic", side_effect=[0, 4]):
                        errors = arrangement.rollback({"baseline": baseline, "plan": plan})
                self.assertTrue(errors)
                self.assertEqual(self.current["monitors"][0], external)
                self.assertEqual(self.current["monitors"][1], baseline["monitors"][1])

    def test_worker_restores_modes_after_partial_apply_failure(self):
        plan = self.mode_plan()
        token = "c" * 48
        state = {"token": token, "state": "starting", "phase": "prepared", "request": "",
                 "message": "Starting", "mutationStarted": False, "baseline": self.plan["baseline"], "plan": plan}
        with tempfile.TemporaryDirectory() as directory, self.compositor(fail_after=1):
            root = Path(directory)
            arrangement.save(root, state)
            arrangement.worker(root, token)
            finished = arrangement.load(root, token)
        self.assertEqual(finished["state"], "failed")
        self.assertEqual(self.current["monitors"], self.plan["baseline"]["monitors"])

    def test_restarted_worker_restores_own_mode_changes_without_resuming_trial(self):
        plan = self.mode_plan()
        token = "d" * 48
        state = {"token": token, "state": "pending", "phase": "waiting", "request": "keep",
                 "message": "Preview", "deadline": 0, "mutationStarted": True,
                 "baseline": self.plan["baseline"], "plan": plan}
        with tempfile.TemporaryDirectory() as directory, self.compositor():
            root = Path(directory)
            arrangement.set_positions(state["baseline"], plan["positions"])
            arrangement.save(root, state)
            arrangement.worker(root, token)
            finished = arrangement.load(root, token)
        self.assertEqual(finished["state"], "failed")
        self.assertEqual(self.current["monitors"], state["baseline"]["monitors"])

    def test_recovery_preserves_countdown_and_does_not_revive_finished_preview(self):
        token = "a" * 48
        state = {"token": token, "state": "pending", "phase": "waiting",
                 "deadline": 112, "message": "Preview", "uiScreen": "A",
                 "baseline": self.current, "plan": self.plan}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arrangement.save(root, state)
            arrangement.atomic(root, "active.json", {"token": token})
            with patch.object(arrangement, "runtime_dir", return_value=root):
                with patch.object(arrangement.time, "monotonic", return_value=100):
                    recovered = arrangement.resume()
                self.assertEqual(recovered["token"], token)
                self.assertEqual(recovered["secondsRemaining"], 12)
                self.assertEqual(recovered["plan"]["positions"], self.plan["positions"])
                with patch.object(arrangement.time, "monotonic", return_value=108):
                    self.assertEqual(arrangement.resume()["secondsRemaining"], 4)
                state["state"] = "kept"
                arrangement.save(root, state)
                self.assertNotIn("token", arrangement.resume())

    def profile_request(self):
        self.current["workspaces"] = [
            {"id": 1, "monitor": "A", "windows": 0, "ispersistent": False},
            {"id": 2, "monitor": "B", "windows": 0, "ispersistent": True},
            {"id": 4, "monitor": "B", "windows": 2, "ispersistent": False},
            {"id": 5, "monitor": "A"},
            {"id": -99, "monitor": "A", "windows": 0, "ispersistent": True},
        ]
        self.plan["baseline"] = copy.deepcopy(self.current)
        self.plan["profileWorkspaces"] = [{"id": 1, "target": "A"}, {"id": 7, "target": "B"}]
        return self.plan

    def test_profile_validation_restores_missing_saved_ids_without_synthetic_moves(self):
        request = self.profile_request()
        plan = self.validate()
        self.assertEqual(plan["profileWorkspaces"], request["profileWorkspaces"])
        self.assertEqual(plan["workspaces"], [])
        self.assertGreater(plan["workspaceChanges"], 0)
        self.assertEqual(plan["removeWorkspaces"], [2])

    def test_profile_cleanup_is_derived_and_preserves_populated_unknown_and_special(self):
        request = self.profile_request()
        request["removeWorkspaces"] = [1, 4, 5, -99]
        plan = self.validate()
        self.assertEqual(plan["removeWorkspaces"], [2])
        self.current["workspaces"][1]["windows"] = 1
        self.assertEqual(self.validate()["removeWorkspaces"], [])

    def test_profile_retained_extra_can_move_but_retired_extra_cannot(self):
        self.profile_request()
        self.plan["workspaces"] = [{"id": 4, "source": "B", "target": "A"}]
        self.assertEqual(self.validate()["workspaces"], self.plan["workspaces"])
        self.plan["workspaces"] = [{"id": 2, "source": "B", "target": "A"}]
        with self.assertRaisesRegex(ValueError, "retired"):
            self.validate()

    def test_profile_rejects_conflicting_card_targets_and_empty_display(self):
        self.profile_request()
        self.plan["workspaces"] = [{"id": 1, "source": "A", "target": "B"}]
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.validate()
        self.plan["workspaces"] = []
        self.plan["profileWorkspaces"][1]["target"] = "A"
        self.current["workspaces"] = self.current["workspaces"][:2]
        self.plan["baseline"] = copy.deepcopy(self.current)
        with self.assertRaisesRegex(ValueError, "every enabled display"):
            self.validate()

    def test_profile_rejects_stale_saved_presence_or_placement(self):
        self.profile_request()
        self.current["workspaces"].append({"id": 7, "monitor": "B"})
        with self.assertRaisesRegex(ValueError, "Saved workspace 7 changed"):
            self.validate()
        self.current["workspaces"].pop()
        self.current["workspaces"][0]["monitor"] = "B"
        with self.assertRaisesRegex(ValueError, "Saved workspace 1 changed"):
            self.validate()

    def test_profile_rejects_stale_extra_placement_without_overwriting_populated_extra(self):
        self.profile_request()
        extra = self.current["workspaces"][2]
        extra["monitor"] = "A"
        with self.assertRaisesRegex(ValueError, "Extra workspace 4 moved externally"):
            self.validate()
        self.assertEqual(extra, {"id": 4, "monitor": "A", "windows": 2, "ispersistent": False})

    def test_profile_contract_rejects_empty_duplicate_invalid_and_unknown_targets(self):
        self.profile_request()
        for saved in ([], [{"id": 1, "target": "A"}, {"id": 1, "target": "B"}],
                      [{"id": -1, "target": "A"}], [{"id": True, "target": "A"}],
                      [{"id": 7, "target": "disconnected"}]):
            with self.subTest(saved=saved):
                self.plan["profileWorkspaces"] = saved
                with self.assertRaises(ValueError):
                    self.validate()

    def test_profile_missing_saved_empty_workspace_never_gets_expiry_exemption(self):
        self.profile_request()
        baseline = copy.deepcopy(self.current)
        self.current["workspaces"] = [w for w in self.current["workspaces"] if w["id"] != 1]
        self.current["workspaces"].append({"id": 7, "monitor": "B", "windows": 0, "ispersistent": True})
        with self.compositor():
            with self.assertRaisesRegex(ValueError, "Workspace 1"):
                arrangement.verify_arrangement(baseline, self.plan["positions"], [],
                                               self.plan["profileWorkspaces"])

    def test_profile_saved_workspace_requires_correct_target_and_session_retention(self):
        self.profile_request()
        baseline = copy.deepcopy(self.current)
        self.current["workspaces"][0]["ispersistent"] = True
        workspace = {"id": 7, "monitor": "B", "windows": 0, "ispersistent": True}
        self.current["workspaces"].append(workspace)
        with self.compositor():
            arrangement.verify_arrangement(baseline, self.plan["positions"], [],
                                           self.plan["profileWorkspaces"])
            workspace["ispersistent"] = False
            with self.assertRaisesRegex(ValueError, "retained for the session"):
                arrangement.verify_arrangement(baseline, self.plan["positions"], [],
                                               self.plan["profileWorkspaces"])
            workspace.update(ispersistent=True, monitor="A")
            with self.assertRaisesRegex(ValueError, "intended display B"):
                arrangement.verify_arrangement(baseline, self.plan["positions"], [],
                                               self.plan["profileWorkspaces"])

    def test_profile_cleanup_verification_requires_retirement_but_preserves_arriving_windows(self):
        self.profile_request()
        baseline = copy.deepcopy(self.current)
        self.current["workspaces"][0]["ispersistent"] = True
        self.current["workspaces"].append({"id": 7, "monitor": "B", "windows": 0, "ispersistent": True})
        extra = self.current["workspaces"][1]
        with self.compositor():
            with self.assertRaisesRegex(ValueError, "did not retire"):
                arrangement.verify_arrangement(baseline, self.plan["positions"], [],
                                               self.plan["profileWorkspaces"], {2})
            extra["windows"] = 1
            arrangement.verify_arrangement(baseline, self.plan["positions"], [],
                                           self.plan["profileWorkspaces"], {2})
            self.current["workspaces"].remove(extra)
            arrangement.verify_arrangement(baseline, self.plan["positions"], [],
                                           self.plan["profileWorkspaces"], {2})

    def test_profile_workspace_only_keep_and_resume_require_complete_restoration(self):
        self.profile_request()
        plan = self.validate()
        token = "e" * 48
        state = {"token": token, "state": "pending", "phase": "waiting", "request": "",
                 "deadline": 100, "message": "Preview", "baseline": self.plan["baseline"], "plan": plan}
        state["profileJournal"] = arrangement.prepare_profile(state, [])
        rules = arrangement.expected_profile_rules(state, {2})
        self.current["workspaces"] = [w for w in self.current["workspaces"] if w["id"] != 2]
        self.current["workspaces"][0]["ispersistent"] = True
        with tempfile.TemporaryDirectory() as directory, self.compositor():
            root = Path(directory)
            arrangement.save(root, state)
            arrangement.atomic(root, "active.json", {"token": token})
            with patch.object(arrangement, "runtime_dir", return_value=root), \
                    patch.object(arrangement, "profile_status", return_value={2}), \
                    patch.object(arrangement, "workspace_rules", return_value=rules):
                self.assertEqual(arrangement.resume()["plan"]["profileWorkspaces"], plan["profileWorkspaces"])
                with self.assertRaisesRegex(ValueError, "Workspace 7"):
                    arrangement.control("keep", token)
                self.assertEqual(arrangement.load(root, token)["request"], "")
                self.current["workspaces"].append({"id": 7, "monitor": "B", "windows": 0, "ispersistent": True})
                arrangement.control("keep", token)
                self.assertEqual(arrangement.load(root, token)["request"], "keep")

    def test_profile_worker_rejects_rule_drift_before_any_mutation(self):
        self.profile_request()
        plan = self.validate()
        token = "f" * 48
        state = {"token": token, "state": "starting", "phase": "prepared", "request": "",
                 "message": "Starting", "mutationStarted": False, "baseline": self.plan["baseline"], "plan": plan}
        state["profileJournal"] = arrangement.prepare_profile(state, [])
        before = copy.deepcopy(self.current)
        with tempfile.TemporaryDirectory() as directory, self.compositor(), \
                patch.object(arrangement, "workspace_rules",
                             return_value=[{"workspaceString": "1", "enabled": True, "persistent": True}]):
            root = Path(directory)
            arrangement.save(root, state)
            arrangement.worker(root, token)
            finished = arrangement.load(root, token)
        self.assertEqual(finished["state"], "failed")
        self.assertFalse(finished["mutationStarted"])
        self.assertEqual(self.current, before)

    def test_profile_verification_rejects_rule_edits_even_when_positions_match(self):
        self.profile_request()
        state = {"token": "a" * 48, "baseline": self.plan["baseline"], "plan": self.validate()}
        state["profileJournal"] = arrangement.prepare_profile(state, [])
        rules = arrangement.expected_profile_rules(state, set())
        rules[0]["monitor"] = "B"
        with self.compositor(), patch.object(arrangement, "profile_status", return_value=set()), \
                patch.object(arrangement, "workspace_rules", return_value=rules):
            with self.assertRaisesRegex(ValueError, "rules changed externally"):
                arrangement.verify_trial(state)

    def test_profile_rollback_preserves_external_workspace_move(self):
        self.profile_request()
        state = {"token": "b" * 48, "baseline": self.plan["baseline"], "plan": self.validate()}
        state["profileJournal"] = arrangement.prepare_profile(state, [])
        self.current["workspaces"][0]["monitor"] = "B"
        with self.compositor(), patch.object(arrangement, "workspace_rules", return_value=[]), \
                patch.object(arrangement.time, "monotonic", side_effect=[0, 4]):
            errors = arrangement.rollback(state)
        self.assertTrue(any("moved externally" in error for error in errors))
        self.assertEqual(self.current["workspaces"][0]["monitor"], "B")


class AutomaticRestorationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": directory, "XDG_RUNTIME_DIR": directory,
            "HYPRLAND_INSTANCE_SIGNATURE": "isolated-automatic-tests"}))
        raw = [{"id": index, "name": name, "make": "Synthetic", "model": name, "serial": name,
                "width": 1920, "height": 1080, "scale": 1, "transform": 0, "refreshRate": 60,
                "x": index * 1920, "y": 0, "availableModes": ["1920x1080@60Hz"]}
               for index, name in enumerate(("A", "B"))]
        with patch.object(arrangement, "query", side_effect=lambda kind, *args: {
                "monitors": raw, "activewindow": {},
                "workspaces": [{"id": 1, "monitor": "A", "windows": 1, "ispersistent": False},
                               {"id": 2, "monitor": "B", "windows": 1, "ispersistent": False}]}[kind]):
            self.current = arrangement.snapshot()
        self.stack.enter_context(patch.object(arrangement, "snapshot", side_effect=lambda: copy.deepcopy(self.current)))
        self.stack.enter_context(patch.object(arrangement, "run", return_value=""))
        self.stack.enter_context(patch.object(arrangement.shutil, "which", return_value="/synthetic/tool"))
        self.stack.enter_context(patch.object(arrangement, "check_monitor_rules", return_value=[]))
        self.rules = []
        self.stack.enter_context(patch.object(arrangement, "workspace_rules", side_effect=lambda: copy.deepcopy(self.rules)))
        self.stack.enter_context(patch.object(arrangement, "profile_status", return_value=set()))
        self.stack.enter_context(patch.object(arrangement, "lua_eval"))
        self.stack.enter_context(patch.object(arrangement, "announce_automatic"))
        self.now = 100.0
        self.stack.enter_context(patch.object(arrangement.time, "monotonic", side_effect=lambda: self.now))
        self.profile = {"id": "a" * 32, "name": "Desk", "automatic": True,
                        "monitors": [{"connector": m["name"],
                                      **{key: m[key] for key in (*arrangement.display_profiles.IDENTITY,
                                                                 *arrangement.display_profiles.GEOMETRY)}}
                                     for m in self.current["monitors"]],
                        "workspaces": [{"id": 1, "monitor": 0}, {"id": 2, "monitor": 1}]}
        self.store = arrangement.display_profiles.store_path(create=True)
        self.write_profiles([self.profile])
        self.runtime = arrangement.runtime_dir()

    def write_profiles(self, profiles):
        self.store.write_text(json.dumps({"version": 2, "profiles": profiles}))
        self.store.chmod(0o600)

    def check(self, event=False, blocked=False):
        return arrangement.display_automation.check(arrangement, {"event": event, "blocked": blocked})

    def start(self):
        self.assertEqual(self.check()["status"], "settling")
        self.now += 2
        result = self.check()
        self.assertEqual(result["status"], "starting", result)
        return result["token"]

    def finish(self, token):
        state = arrangement.load(self.runtime, token)
        state.update(state="kept", phase="finished")
        arrangement.save(self.runtime, state)

    def run_worker(self, token, on_tick=None):
        def apply_profile(state):
            for workspace in self.current["workspaces"]:
                workspace["ispersistent"] = True
            self.rules = arrangement.expected_profile_rules(state, set())

        def rollback(state):
            self.current = copy.deepcopy(state["baseline"])
            self.rules = copy.deepcopy(state["profileJournal"]["beforeRules"])
            return []

        def sleep(seconds):
            self.now = round(self.now + seconds, 6)
            if on_tick:
                on_tick()

        with patch.object(arrangement, "apply_profile", side_effect=apply_profile), \
                patch.object(arrangement, "rollback", side_effect=rollback), \
                patch.object(arrangement.time, "sleep", side_effect=sleep):
            arrangement.worker(self.runtime, token)
        return arrangement.load(self.runtime, token)

    def test_bursts_settle_handoff_deduplicates_and_same_combination_can_reconnect(self):
        self.assertEqual(self.check()["retryAfterMs"], 2000)
        self.now = 101.5
        self.assertEqual(self.check(event=True)["status"], "settling")
        # Paired monitoradded/monitoraddedv2 delivery extends the same burst.
        self.now = 101.6
        self.assertEqual(self.check(event=True)["status"], "settling")
        self.now = 103.59
        self.assertEqual(self.check()["status"], "settling")
        self.now = 103.6
        first = self.check()
        self.assertEqual(first["status"], "starting")
        self.assertEqual(self.check()["token"], first["token"])
        self.assertEqual(self.check(event=True)["token"], first["token"])
        self.finish(first["token"])
        # Geometry, workspace focus/order, and refreshes do not enforce the profile.
        self.current["monitors"].reverse()
        for monitor in self.current["monitors"]:
            monitor["x"] += 30
            monitor["focused"] = True
        self.current["workspaces"][0]["monitor"] = "B"
        self.now = 120
        self.assertEqual(self.check()["status"], "kept")
        self.assertEqual(self.check(event=True)["status"], "settling")
        self.now += 2
        second = self.check()
        self.assertEqual(second["status"], "starting", second)
        self.assertNotEqual(first["token"], second["token"])

    def test_topology_availability_changes_extend_pending_settling(self):
        self.check(event=True)
        self.now += 1
        self.current["unavailableMonitors"] = [{"name": "C", "make": "Synthetic", "model": "C", "serial": "C"}]
        self.assertEqual(self.check()["retryAfterMs"], 2000)
        self.now += 2
        result = self.check()
        self.assertEqual(result["status"], "skipped")
        self.assertIn("unavailable", result["message"])
        self.current["unavailableMonitors"] = []
        self.now += 10
        self.assertEqual(self.check()["status"], "skipped")

    def test_open_panel_consumes_episode_and_closing_does_not_restore(self):
        self.check()
        self.now += 1
        self.assertEqual(self.check(blocked=True)["status"], "skipped")
        self.now += 10
        self.assertEqual(self.check()["status"], "skipped")
        self.assertEqual(self.check(event=True)["status"], "settling")
        self.now += 2
        self.assertEqual(self.check()["status"], "starting")

    def test_manual_begin_cannot_grant_automatic_and_suppresses_connection(self):
        self.check()
        draft = arrangement.display_automation.request_for(arrangement, self.profile, self.current)
        draft.update(automatic=True, automaticProfile={"id": self.profile["id"]})
        trial = arrangement.begin(draft)
        self.assertFalse(trial["automatic"])
        self.now += 5
        self.assertEqual(self.check(event=True)["status"], "skipped")
        self.finish(trial["token"])
        self.now += 10
        self.assertEqual(self.check()["status"], "skipped")

    def test_profile_load_and_forget_suppress_pending_episode(self):
        self.check()
        request = {"id": self.profile["id"], "revision": arrangement.display_config.digest(self.store.read_bytes())}
        with patch.object(arrangement.sys, "argv", ["apply.py", "profile-load", json.dumps(request)]):
            self.assertTrue(arrangement.main()["ok"])
        self.now += 5
        self.assertEqual(self.check()["status"], "skipped")
        self.check(event=True)
        with patch.object(arrangement.display_config, "prepare", side_effect=ValueError("stale selection")):
            with self.assertRaisesRegex(ValueError, "stale selection"):
                arrangement.forget({})
        self.now += 5
        self.assertEqual(self.check()["status"], "skipped")

    def test_active_auto_remains_observable_while_blocked_and_discovery_fails(self):
        token = self.start()
        with patch.object(arrangement, "snapshot", side_effect=ValueError("IPC unavailable")):
            result = self.check(event=True, blocked=True)
        self.assertTrue(result["automatic"])
        self.assertEqual(result["token"], token)
        self.assertEqual(result["profileName"], "Desk")
        self.assertGreater(result["retryAfterMs"], 0)
        self.finish(token)
        self.now += 10
        self.assertEqual(self.check()["status"], "kept")

    def test_missing_weak_conflicting_and_unsupported_profiles_never_launch(self):
        cases = []
        disabled = copy.deepcopy(self.profile)
        disabled["automatic"] = False
        cases.append([disabled])
        weak = copy.deepcopy(self.profile)
        weak["monitors"][0]["serial"] = ""
        cases.append([weak])
        conflict = copy.deepcopy(self.profile)
        conflict.update(id="b" * 32, name="Other")
        cases.append([self.profile, conflict])
        unavailable = copy.deepcopy(self.profile)
        unavailable["monitors"][0].update(width=1600, height=900)
        cases.append([unavailable])
        for profiles in cases:
            with self.subTest(profiles=profiles):
                self.write_profiles(profiles)
                self.now += 10
                self.assertEqual(self.check(event=True)["status"], "settling")
                self.now += 2
                self.assertEqual(self.check()["status"], "skipped")
                self.assertFalse((self.runtime / "active.json").exists())
                self.now += 10
                self.assertEqual(self.check()["status"], "skipped")

    def test_renamed_connector_does_not_bypass_exact_rule_preflight(self):
        self.profile["monitors"][0]["x"], self.profile["monitors"][1]["x"] = 1920, 0
        self.write_profiles([self.profile])
        self.current["monitors"][0]["name"] = "renamed-A"
        self.current["workspaces"][0]["monitor"] = "renamed-A"
        self.check()
        self.now += 2
        with patch.object(arrangement, "check_monitor_rules", side_effect=ValueError("No safe exact monitor rule")):
            result = self.check()
        self.assertEqual(result["status"], "skipped")
        self.assertFalse((self.runtime / "active.json").exists())

    def test_worker_rechecks_revision_and_disable_before_mutation(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.write_profiles([self.profile])
                self.now += 10
                self.check(event=True)
                self.now += 2
                token = self.check()["token"]
                modified = copy.deepcopy(self.profile)
                modified.update(automatic=enabled, name="Changed")
                self.write_profiles([modified])
                before = copy.deepcopy(self.current)
                arrangement.worker(self.runtime, token)
                result = arrangement.load(self.runtime, token)
                self.assertEqual(result["state"], "failed")
                self.assertFalse(result["mutationStarted"])
                self.assertEqual(self.current, before)

    def test_automatic_revert_preserves_workspace_move_during_worker_startup(self):
        token = self.start()
        # The launcher saw A; the user moved this workspace before the worker ran.
        self.current["workspaces"][0]["monitor"] = "B"
        observed = []

        def execute(code):
            # Emulate only compositor side effects; use the real worker and rollback.
            for wid, target in re.findall(
                    r'hl\.workspace_rule\(\{workspace="(\d+)",monitor="([^"]+)",persistent=true\}\)', code):
                self.rules.append({"workspaceString": wid, "enabled": True,
                                   "persistent": True, "monitor": target})
                workspace = next(w for w in self.current["workspaces"] if w["id"] == int(wid))
                workspace.update(monitor=target, ispersistent=True)
            for wid in re.findall(r'j\.pins\["(\d+)"\]:set_enabled\(false\)', code):
                for rule in self.rules:
                    if rule["workspaceString"] == wid:
                        rule["enabled"] = False
                next(w for w in self.current["workspaces"] if w["id"] == int(wid))["ispersistent"] = False
            for wid, target in re.findall(
                    r'hl\.dsp\.workspace\.move\(\{workspace=(\d+),monitor="([^"]+)"\}\)', code):
                next(w for w in self.current["workspaces"] if w["id"] == int(wid))["monitor"] = target

        def revert(seconds):
            self.now += seconds
            if arrangement.load(self.runtime, token)["state"] == "pending":
                observed.append(self.current["workspaces"][0]["monitor"])
                arrangement.control("revert", token)

        with patch.object(arrangement, "lua_eval", side_effect=execute), \
                patch.object(arrangement.time, "sleep", side_effect=revert):
            arrangement.worker(self.runtime, token)
        self.assertEqual(observed, ["A"])
        self.assertEqual(arrangement.load(self.runtime, token)["state"], "reverted")
        self.assertEqual(self.current["workspaces"][0]["monitor"], "B")
        self.assertEqual(self.current["workspaces"][1]["monitor"], "B")

    def test_auto_observes_full_deadline_and_keep_cannot_shorten_it(self):
        token = self.start()
        started = self.now
        attempts = []

        def keep_early():
            if not attempts:
                resumed = arrangement.resume()
                self.assertTrue(resumed["automatic"])
                self.assertEqual(resumed["profileName"], "Desk")
                self.assertEqual(resumed["secondsRemaining"], 20)
                attempts.append(arrangement.control("keep", token))

        result = self.run_worker(token, keep_early)
        self.assertFalse(attempts[0]["ok"])
        self.assertEqual(result["state"], "kept")
        self.assertGreaterEqual(self.now - started, 20)
        self.assertTrue(all(w["ispersistent"] for w in self.current["workspaces"]))
        self.assertNotIn("retryAfterMs", self.check())

    def test_manual_trial_still_requires_apply_and_expires(self):
        draft = arrangement.display_automation.request_for(arrangement, self.profile, self.current)
        token = arrangement.begin(draft)["token"]
        before = copy.deepcopy(self.current)
        result = self.run_worker(token)
        self.assertEqual(result["state"], "reverted")
        self.assertEqual(self.current, before)
        token = arrangement.begin(draft)["token"]

        def keep():
            arrangement.control("keep", token)

        started = self.now
        result = self.run_worker(token, keep)
        self.assertEqual(result["state"], "kept")
        self.assertLess(self.now - started, 20)

    def test_auto_verification_failure_reverts_and_does_not_retry(self):
        token = self.start()
        before = copy.deepcopy(self.current)
        started = self.now

        def external_change():
            if self.now - started >= 5:
                self.current["workspaces"][0]["ispersistent"] = False

        result = self.run_worker(token, external_change)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.current, before)
        self.now += 10
        self.assertEqual(self.check()["token"], token)
        self.assertEqual(self.check()["status"], "failed")

    def test_profile_disabled_externally_during_countdown_rolls_back_at_commit(self):
        token = self.start()
        before = copy.deepcopy(self.current)
        started = self.now
        changed = False

        def disable():
            nonlocal changed
            if not changed and self.now - started >= 5:
                changed = True
                profile = copy.deepcopy(self.profile)
                profile["automatic"] = False
                self.write_profiles([profile])

        result = self.run_worker(token, disable)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.current, before)
        self.assertGreaterEqual(self.now - started, 20)
        self.assertFalse(json.loads(self.store.read_text())["profiles"][0]["automatic"])

    def test_automatic_revert_and_worker_restart_never_keep_or_reapply(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.now += 10
                self.check(event=True)
                self.now += 2
                token = self.check()["token"]
                baseline = copy.deepcopy(self.current)
                if restart:
                    state = arrangement.load(self.runtime, token)
                    state.update(state="pending", phase="waiting", mutationStarted=True,
                                 request="keep", deadline=self.now + 20)
                    arrangement.save(self.runtime, state)
                    self.current["workspaces"][0]["ispersistent"] = True
                    result = self.run_worker(token)
                    self.assertEqual(result["state"], "failed")
                else:
                    result = self.run_worker(token, lambda: arrangement.control("revert", token))
                    self.assertEqual(result["state"], "reverted")
                self.assertEqual(self.current, baseline)


if __name__ == "__main__":
    unittest.main()
