"""Forget contracts, exercised only against private temporary configuration files."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apply as arrangement
import configuration as config


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = [self.root / "monitors.lua", self.root / "shell.json"]
        self.lua = '-- keep header\nlocal scale = 1\nhl.env("GDK_SCALE", tostring(scale))\n'
        self.rule_a = 'hl.monitor({ output = "A", mode = "1920x1080@60", scale = 1 })'
        self.rule_b = 'hl.monitor({ output = "B", mode = "preferred", position = "auto" })'
        self.document = {"idle": {"lock": 300}, "bar": {"layout": {"left": [
            {"id": "display.workspaces", "iconOnly": True, "displays": []},
            {"id": "other.widget", "displays": [{"connector": "A", "name": "Unrelated"}]}]}},
            "plugins": []}
        self.live = [{"name": "A", "make": "Vendor", "model": "Panel", "serial": "one", "width": 0}]
        self.write_config(self.lua + self.rule_a + "\n" + self.rule_b + "\n")
        self.addCleanup(patch.stopall)
        patch.object(config, "config_paths", return_value=self.paths).start()
        patch.object(arrangement, "runtime_dir", return_value=self.root).start()
        patch.object(arrangement, "query", side_effect=lambda *args: copy.deepcopy(self.live)).start()
        self.commands = []
        patch.object(arrangement, "run", side_effect=self.run_command).start()
        self.token_number = 0

    def run_command(self, command, **kwargs):
        self.commands.append(command)
        return "ok" if command == ["hyprctl", "reload"] else ""

    def write_config(self, source, displays=None):
        if displays is not None:
            self.document["bar"]["layout"]["left"][0]["displays"] = displays
        self.paths[0].write_text(source)
        self.paths[1].write_text(json.dumps(self.document))

    def listed(self):
        return config.configuration(arrangement)

    def request(self, connector):
        catalog = self.listed()
        selected = next(item for item in catalog["entries"] if item["connector"] == connector)
        return {"key": selected["key"], "revision": catalog["revision"]}

    def worker(self, request):
        self.token_number += 1
        token = f"{self.token_number:048x}"
        state = {"token": token, "phase": "prepared", "created": 0, "request": request}
        arrangement.save(self.root, state)
        arrangement.forget_worker(self.root, token)
        return arrangement.load(self.root, token)["result"]

    def test_config_only_disconnected_and_faulty_displays_need_no_preferences(self):
        entries = {entry["connector"]: entry for entry in self.listed()["entries"]}
        self.assertEqual(set(entries), {"A", "B"})
        self.assertTrue(entries["A"]["connected"])
        self.assertFalse(entries["B"]["connected"])
        before = [path.read_bytes() for path in self.paths]
        result = self.worker(self.request("B"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.paths[0].read_text(), self.lua + self.rule_a + "\n\n")
        self.assertEqual(self.paths[1].read_bytes(), before[1])
        self.assertEqual([Path(path).read_bytes() for path in result["backupPaths"]], [before[0]])
        self.assertEqual([entry["connector"] for entry in result["entries"]], ["A"])

    def test_preference_only_removal_does_not_reload_or_touch_monitor_file(self):
        self.write_config(self.lua, [{"connector": "old", "name": "Retired"}])
        original = self.paths[0].read_bytes()
        result = self.worker(self.request("old"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.paths[0].read_bytes(), original)
        self.assertEqual(json.loads(self.paths[1].read_bytes())["bar"]["layout"]["left"][0]["displays"], [])
        self.assertFalse(any(command[0] == "hyprctl" for command in self.commands))
        self.assertEqual(result["entries"], [])

    def test_merged_removal_preserves_other_rules_comments_and_preferences(self):
        comment = '-- ' + self.rule_a + '\n--[=[\n' + self.rule_b + '\n]=]\n'
        self.write_config(self.lua + comment + self.rule_a + ' -- trailing note\n' + self.rule_b + '\n',
                          [{"connector": "A", "name": "Desk"}, {"connector": "B", "name": "Keep", "custom": 4}])
        originals = [path.read_bytes() for path in self.paths]
        result = self.worker(self.request("A"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.paths[0].read_text(), self.lua + comment + ' -- trailing note\n' + self.rule_b + '\n')
        expected = copy.deepcopy(self.document)
        del expected["bar"]["layout"]["left"][0]["displays"][0]
        self.assertEqual(json.loads(self.paths[1].read_bytes()), expected)
        self.assertEqual([Path(path).read_bytes() for path in result["backupPaths"]], originals)

    def test_multiline_literal_and_string_comment_boundaries_are_not_reinterpreted(self):
        rule = 'hl.monitor( {\n output = [=[A]=],\n mode = "preferred", -- } )\n position = "auto",\n transform = 0,\n} );'
        unrelated = 'local text = "hl.monitor({ output = \\\"ghost\\\" }) -- not code"\n'
        self.write_config(self.lua + unrelated + rule + '\n' + self.rule_b + '\n')
        result = self.worker(self.request("A"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.paths[0].read_text(), self.lua + unrelated + '\n' + self.rule_b + '\n')

    def test_repeated_exact_rules_are_removed_together(self):
        self.write_config(self.rule_a + "\n" + self.rule_b + "\n" + self.rule_a + "\n")
        result = self.worker(self.request("A"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.paths[0].read_text(), "\n" + self.rule_b + "\n\n")

    def test_unique_hardware_identity_matches_preferences_after_connector_move(self):
        self.write_config(self.rule_a + "\n", [{"connector": "old", "make": "Vendor", "model": "Panel", "serial": "one", "name": "Desk"}])
        result = self.worker(self.request("A"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["entries"], [])
        self.assertEqual(json.loads(self.paths[1].read_bytes())["bar"]["layout"]["left"][0]["displays"], [])

    def test_moved_strong_identity_keeps_old_and_current_rules_independent(self):
        for selected in ("B", "A"):
            with self.subTest(selected=selected):
                preferences = [{"connector": "B", "make": "Vendor", "model": "Panel",
                                "serial": "one", "name": "Desk"}]
                self.write_config(self.rule_a + "\n" + self.rule_b + "\n", preferences)
                originals = [path.read_bytes() for path in self.paths]
                result = self.worker(self.request(selected))
                self.assertTrue(result["ok"], result)
                if selected == "B":
                    self.assertEqual(self.paths[0].read_text(), self.rule_a + "\n\n")
                    self.assertEqual(self.paths[1].read_bytes(), originals[1])
                    self.assertEqual([Path(path).read_bytes() for path in result["backupPaths"]], [originals[0]])
                else:
                    self.assertEqual(self.paths[0].read_text(), "\n" + self.rule_b + "\n")
                    self.assertEqual(json.loads(self.paths[1].read_bytes())["bar"]["layout"]["left"][0]["displays"], [])
                    self.assertEqual([Path(path).read_bytes() for path in result["backupPaths"]], originals)

    def test_moved_weak_identity_cannot_choose_between_configured_connectors(self):
        self.write_config(self.rule_a + "\n" + self.rule_b + "\n",
                          [{"connector": "B", "make": "Vendor", "model": "Panel", "name": "Desk"}])
        originals = [path.read_bytes() for path in self.paths]
        for connector in ("A", "B"):
            result = self.worker(self.request(connector))
            self.assertFalse(result["ok"])
            self.assertEqual([path.read_bytes() for path in self.paths], originals)

    def test_duplicate_live_serials_still_block_both_connectors(self):
        self.live.append(dict(self.live[0], name="B"))
        self.write_config(self.rule_a + "\n" + self.rule_b + "\n",
                          [{"connector": "B", "make": "Vendor", "model": "Panel",
                            "serial": "one", "name": "Desk"}])
        originals = [path.read_bytes() for path in self.paths]
        for connector in ("A", "B"):
            result = self.worker(self.request(connector))
            self.assertFalse(result["ok"])
            self.assertEqual([path.read_bytes() for path in self.paths], originals)

    def test_ambiguous_hardware_and_conflicting_saved_identities_are_refused(self):
        self.live.append(dict(self.live[0], name="B"))
        self.write_config(self.rule_a + "\n" + self.rule_b + "\n",
                          [{"connector": "A", "make": "Vendor", "model": "Panel", "name": "Which one?"}])
        before = [path.read_bytes() for path in self.paths]
        entries = self.listed()["entries"]
        self.assertTrue(all(not entry["removable"] and entry["reason"] for entry in entries))
        result = self.worker(self.request("B"))
        self.assertFalse(result["ok"])
        self.assertEqual([path.read_bytes() for path in self.paths], before)
        self.write_config(self.rule_a + "\n", [{"connector": "A", "serial": "one"}, {"connector": "A", "serial": "two"}])
        self.assertFalse(self.listed()["entries"][0]["removable"])

    def test_dynamic_conditional_and_broad_rules_are_visible_but_not_removable(self):
        for rule in ('hl.monitor({ output = pick_output(), mode = "preferred" })',
                     'if enabled then\n' + self.rule_a + '\nend',
                     'hl.monitor({ output = "", mode = "preferred" })',
                     'hl.monitor({ output = "desc:Vendor Panel", mode = "preferred" })',
                     'hl["monitor"]({ output = "A", mode = "preferred" })'):
            with self.subTest(rule=rule):
                self.write_config(rule + '\n')
                listed = self.listed()
                self.assertEqual(len(listed["entries"]), 1)
                selected = listed["entries"][0]
                self.assertFalse(selected["removable"])
                result = self.worker({"key": selected["key"], "revision": listed["revision"]})
                self.assertFalse(result["ok"])
                self.assertEqual(self.paths[0].read_text(), rule + '\n')

    def test_stale_either_file_or_hardware_is_refused_without_backups(self):
        for change in ("lua", "json", "hardware"):
            with self.subTest(change=change):
                request = self.request("A")
                if change == "lua":
                    self.paths[0].write_text(self.paths[0].read_text() + "-- external edit\n")
                elif change == "json":
                    document = json.loads(self.paths[1].read_bytes())
                    document["idle"]["lock"] = 900
                    self.paths[1].write_text(json.dumps(document))
                else:
                    self.live[0]["serial"] = "replacement"
                before = [path.read_bytes() for path in self.paths]
                result = self.worker(request)
                self.assertFalse(result["ok"])
                self.assertEqual([path.read_bytes() for path in self.paths], before)
                self.assertEqual(list(self.root.glob("*.bak")), [])

    def test_reload_failure_restores_exact_originals_and_reports_failure(self):
        self.write_config(self.rule_a + '\n', [{"connector": "A", "name": "Desk"}])
        originals = [path.read_bytes() for path in self.paths]
        reloads = []
        def run(command, **kwargs):
            if command == ["hyprctl", "reload"]:
                reloads.append(self.paths[0].read_bytes())
                return "ok"
            if command == ["hyprctl", "configerrors"] and len(reloads) == 1:
                return "Bad configuration"
            return ""
        with patch.object(arrangement, "run", side_effect=run):
            result = self.worker(self.request("A"))
        self.assertFalse(result["ok"])
        self.assertIn("Bad configuration", result["message"])
        self.assertEqual([path.read_bytes() for path in self.paths], originals)
        self.assertEqual(reloads, [b"\n", originals[0]])

    def test_concurrent_edit_during_reload_is_preserved_instead_of_blind_rollback(self):
        self.write_config(self.rule_a + '\n', [{"connector": "A", "name": "Desk"}])
        original_lua = self.paths[0].read_bytes()
        changed = copy.deepcopy(self.document)
        changed["bar"]["layout"]["left"][0]["displays"] = []
        changed["idle"]["lock"] = 900
        external = json.dumps(changed).encode()
        def run(command, **kwargs):
            if command == ["hyprctl", "reload"]:
                self.paths[1].write_bytes(external)
                return "ok"
            return ""
        with patch.object(arrangement, "run", side_effect=run):
            result = self.worker(self.request("A"))
        self.assertFalse(result["ok"])
        self.assertIn("Concurrent edit preserved", result["message"])
        self.assertEqual(self.paths[1].read_bytes(), external)
        self.assertEqual(self.paths[0].read_bytes(), original_lua)

    def test_worker_can_finish_from_persisted_request_without_initiating_caller(self):
        request = self.request("A")
        token = "d" * 48
        arrangement.save(self.root, {"token": token, "phase": "prepared", "created": 0, "request": request})
        del request
        arrangement.forget_worker(self.root, token)
        result = arrangement.load(self.root, token)["result"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.paths[0].read_text(), self.lua + '\n' + self.rule_b + '\n')
        notifications = [command for command in self.commands if command[0] == "notify-send"]
        self.assertTrue(any(result["backupPaths"][0] in command[-1] for command in notifications))

    def test_restarted_worker_recovers_interrupted_write_instead_of_reapplying(self):
        request = self.request("A")
        paths, originals, replacements = config.prepare(arrangement, request)
        backup = self.root / "original.bak"
        backup.write_bytes(originals[0])
        paths[0].write_bytes(replacements[0])
        token = "e" * 48
        arrangement.save(self.root, {"token": token, "phase": "writing", "request": request,
            "journal": [{"path": str(paths[0]), "backup": str(backup), "before": config.digest(originals[0]),
                         "after": config.digest(replacements[0]), "lua": True}]})
        arrangement.forget_worker(self.root, token)
        self.assertFalse(arrangement.load(self.root, token)["result"]["ok"])
        self.assertEqual(paths[0].read_bytes(), originals[0])

    def test_active_preview_blocks_worker_and_forget_reservation_blocks_begin(self):
        token = "f" * 48
        arrangement.save(self.root, {"token": token, "state": "pending", "phase": "waiting"})
        arrangement.atomic(self.root, "active.json", {"token": token})
        before = [path.read_bytes() for path in self.paths]
        result = self.worker(self.request("A"))
        self.assertFalse(result["ok"])
        self.assertIn("preview", result["message"])
        self.assertEqual([path.read_bytes() for path in self.paths], before)
        arrangement.save(self.root, {"token": token, "phase": "writing"})
        arrangement.atomic(self.root, "forget-active.json", {"token": token})
        with self.assertRaisesRegex(ValueError, "configuration change"):
            arrangement.begin({})

    def test_duplicate_json_keys_and_symlink_files_are_never_rewritten(self):
        self.paths[1].write_text('{"idle": 1, "idle": 2}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            self.listed()
        self.paths[1].unlink()
        target = self.root / "elsewhere.json"
        target.write_text('{}')
        self.paths[1].symlink_to(target)
        with self.assertRaises(OSError):
            self.listed()
        self.assertEqual(target.read_text(), '{}')

    def test_ipc_unavailability_does_not_hide_configured_displays(self):
        with patch.object(arrangement, "query", side_effect=RuntimeError("IPC unavailable")):
            listed = self.listed()
        self.assertEqual({entry["connector"] for entry in listed["entries"]}, {"A", "B"})
        self.assertTrue(all(entry["connected"] is None for entry in listed["entries"]))


if __name__ == "__main__":
    unittest.main()
