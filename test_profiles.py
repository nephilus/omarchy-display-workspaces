"""Profile safety contracts against isolated files and synthetic compositor data."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apply as arrangement
import configuration as config
import profiles


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "display-workspaces/profiles.json"
        self.raw = [
            {"id": 1, "name": "DP-1", "make": "Vendor", "model": "Panel", "serial": "one",
             "width": 1920, "height": 1080, "refreshRate": 60, "scale": 1, "transform": 0,
             "x": 0, "y": 0, "availableModes": ["1920x1080@60Hz", "1280x720@60Hz"]},
            {"id": 2, "name": "HDMI-A-1", "make": "Vendor", "model": "Panel", "serial": "two",
             "width": 1920, "height": 1080, "refreshRate": 60, "scale": 1, "transform": 0,
             "x": 1920, "y": 0, "availableModes": ["1920x1080@60Hz", "1280x720@60Hz"]},
        ]
        self.workspaces = [{"id": 1, "monitor": "DP-1", "windows": 1, "ispersistent": False},
                           {"id": 2, "monitor": "HDMI-A-1", "windows": 0, "ispersistent": False},
                           {"id": -99, "monitor": "DP-1", "windows": 0, "ispersistent": False}]
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}).start()
        patch.object(arrangement, "query", side_effect=self.query).start()
        patch.object(arrangement, "run", side_effect=AssertionError("No live commands allowed")).start()
        patch.object(arrangement, "lua_eval", side_effect=AssertionError("No live mutations allowed")).start()
        patch.object(arrangement, "check_monitor_rules", side_effect=AssertionError("Profiles do not need Lua rules")).start()

    def query(self, kind, *args):
        return copy.deepcopy({"monitors": self.raw, "workspaces": self.workspaces, "activewindow": {}}[kind])

    def save_request(self, name="Desk", **extra):
        return {"name": name, "baseline": arrangement.snapshot(),
                "revision": profiles.catalog(arrangement)["revision"], **extra}

    def save(self, name="Desk", **extra):
        return profiles.save(arrangement, self.save_request(name, **extra))

    def selection(self, result):
        return {"id": result["savedId"], "revision": result["revision"]}

    def test_catalog_without_store_is_read_only(self):
        result = profiles.catalog(arrangement)
        self.assertEqual(result["profiles"], [])
        self.assertFalse(self.path.parent.exists())

    def test_save_captures_live_geometry_and_load_is_draft_only(self):
        self.raw[0].update(x=-1920, transform=2)
        self.raw[1]["x"] = 0
        result = self.save("  Desk  ")
        document = json.loads(self.path.read_bytes())
        profile = document["profiles"][0]
        self.assertEqual(profile["name"], "Desk")
        self.assertEqual(profile["monitors"][0]["x"], -1920)
        self.assertEqual(profile["monitors"][0]["transform"], 2)
        self.assertEqual(profile["workspaces"], [{"id": 1, "monitor": 0}, {"id": 2, "monitor": 1}])
        self.raw[0].update(x=0, transform=0)
        self.raw[1]["x"] = 1920
        self.workspaces[0]["monitor"] = "HDMI-A-1"
        live_before = arrangement.snapshot()
        bytes_before = self.path.read_bytes()
        loaded = profiles.load(arrangement, self.selection(result))
        self.assertEqual(loaded["baseline"], live_before)
        self.assertEqual(loaded["positions"][0]["x"], 0)
        self.assertEqual(loaded["positions"][0]["transform"], 2)
        self.assertEqual(loaded["workspaces"], [{"id": 1, "source": "HDMI-A-1", "target": "DP-1"}])
        self.assertEqual(loaded["profileWorkspaces"], [{"id": 1, "target": "DP-1"},
                                                      {"id": 2, "target": "HDMI-A-1"}])
        self.assertEqual(arrangement.snapshot(), live_before)
        self.assertEqual(self.path.read_bytes(), bytes_before)

    def test_serial_identity_survives_changed_ports_and_runtime_ids(self):
        saved = self.save()
        self.raw[0].update(name="USB-C-9", id=100)
        self.raw[1].update(name="USB-C-8", id=101)
        self.raw.reverse()
        self.workspaces = [{"id": 1, "monitor": "USB-C-8", "windows": 1, "ispersistent": False},
                           {"id": 2, "monitor": "USB-C-9", "windows": 0, "ispersistent": False}]
        catalog = profiles.catalog(arrangement)
        self.assertEqual(catalog["profiles"][0]["match"], "hardware")
        loaded = profiles.load(arrangement, self.selection(saved))
        self.assertEqual([p["name"] for p in loaded["positions"]], ["USB-C-9", "USB-C-8"])
        self.assertEqual(loaded["workspaces"], [{"id": 1, "source": "USB-C-8", "target": "USB-C-9"},
                                                 {"id": 2, "source": "USB-C-9", "target": "USB-C-8"}])
        self.assertEqual(loaded["profileWorkspaces"], [{"id": 1, "target": "USB-C-9"},
                                                      {"id": 2, "target": "USB-C-8"}])

    def test_different_serial_on_same_connector_never_falls_back(self):
        saved = self.save()
        self.raw[1]["serial"] = "different"
        listed = profiles.catalog(arrangement)["profiles"][0]
        self.assertFalse(listed["canLoad"])
        self.assertEqual(listed["match"], "unavailable")
        with self.assertRaisesRegex(ValueError, "hardware.*changed"):
            profiles.load(arrangement, self.selection(saved))

    def test_unique_make_model_without_serial_is_explicitly_weak(self):
        self.raw[0].update(serial="", model="Unique")
        saved = self.save()
        self.raw[0]["name"] = "USB-C-9"
        self.workspaces[0]["monitor"] = "USB-C-9"
        listed = profiles.catalog(arrangement)["profiles"][0]
        self.assertTrue(listed["canLoad"])
        self.assertEqual(listed["match"], "port-dependent")
        self.assertIn("weak", profiles.load(arrangement, self.selection(saved))["message"])

    def test_duplicate_weak_identities_use_ports_not_enumeration_order(self):
        for monitor in self.raw:
            monitor["serial"] = ""
        saved = self.save()
        self.raw.reverse()
        loaded = profiles.load(arrangement, self.selection(saved))
        self.assertEqual([p["name"] for p in loaded["positions"]], ["DP-1", "HDMI-A-1"])
        self.assertEqual(profiles.catalog(arrangement)["profiles"][0]["match"], "port-dependent")
        self.raw[0]["name"] = "USB-C-9"
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            profiles.load(arrangement, self.selection(saved))

    def test_duplicate_serials_still_require_remembered_ports(self):
        self.raw[1]["serial"] = self.raw[0]["serial"]
        saved = self.save()
        self.assertEqual(saved["profiles"][0]["match"], "port-dependent")
        self.raw[0]["name"] = "USB-C-9"
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            profiles.load(arrangement, self.selection(saved))

    def test_unknown_identity_does_not_override_nonempty_contradiction(self):
        self.raw[0].update(serial="", model="", make="Vendor")
        saved = self.save()
        self.raw[0]["make"] = "Other"
        with self.assertRaisesRegex(ValueError, "changed"):
            profiles.load(arrangement, self.selection(saved))

    def test_missing_saved_workspaces_and_empty_extras_reconcile_only_in_draft(self):
        self.workspaces = [{"id": wid, "monitor": "DP-1" if wid < 5 else "HDMI-A-1",
                            "windows": 0, "ispersistent": False} for wid in range(1, 8)]
        saved = self.save()
        saved_bytes = self.path.read_bytes()
        self.workspaces = self.workspaces[:6] + [
            {"id": 8, "monitor": "DP-1", "windows": 0, "ispersistent": True},
            {"id": 9, "monitor": "HDMI-A-1", "windows": 2, "ispersistent": False},
            {"id": 10, "monitor": "DP-1", "ispersistent": False},
            {"id": -99, "monitor": "DP-1", "windows": 0, "ispersistent": False},
        ]
        live_before = arrangement.snapshot()
        loaded = profiles.load(arrangement, self.selection(saved))
        desired = [{"id": wid, "target": "DP-1" if wid < 5 else "HDMI-A-1"} for wid in range(1, 8)]
        self.assertEqual(loaded["profileWorkspaces"], desired)
        self.assertEqual(loaded["workspaces"], [])
        self.assertEqual(loaded["removeWorkspaces"], [8])
        validated = arrangement.validate({"baseline": loaded["baseline"], "positions": loaded["positions"],
                                          "workspaces": [], "profileWorkspaces": desired}, live_before)
        self.assertGreater(validated["workspaceChanges"], 0)
        self.assertTrue(profiles.catalog(arrangement)["profiles"][0]["canLoad"])
        self.assertEqual(arrangement.snapshot(), live_before)
        self.assertEqual(self.path.read_bytes(), saved_bytes)

    def test_profile_without_saved_positive_workspaces_does_not_authorize_cleanup(self):
        self.workspaces = [self.workspaces[-1]]
        saved = self.save()
        self.workspaces.append({"id": 8, "monitor": "DP-1", "windows": 0, "ispersistent": False})
        loaded = profiles.load(arrangement, self.selection(saved))
        self.assertIsNone(loaded["profileWorkspaces"])
        self.assertEqual(loaded["removeWorkspaces"], [])
        self.assertEqual(loaded["workspaces"], [])

    def test_stale_live_geometry_mode_catalog_or_workspace_baseline_cannot_save(self):
        for change in (lambda: self.raw[0].update(x=1),
                       lambda: self.raw[0]["availableModes"].append("800x600@60Hz"),
                       lambda: self.workspaces[0].update(monitor="HDMI-A-1")):
            original_raw, original_ws = copy.deepcopy(self.raw), copy.deepcopy(self.workspaces)
            request = self.save_request()
            change()
            with self.assertRaisesRegex(ValueError, "changed"):
                profiles.save(arrangement, request)
            self.assertFalse(self.path.exists())
            self.raw, self.workspaces = original_raw, original_ws

    def test_stale_revision_rejects_save_replace_delete_load_and_automatic(self):
        stale = self.save_request("Other")
        saved = self.save()
        original = self.path.read_bytes()
        for operation, request in ((profiles.save, stale),
                                   (profiles.save, {**stale, "id": saved["savedId"]}),
                                   (profiles.delete, {"id": saved["savedId"], "revision": stale["revision"]}),
                                   (profiles.load, {"id": saved["savedId"], "revision": stale["revision"]}),
                                   (profiles.set_automatic, {"id": saved["savedId"], "revision": stale["revision"],
                                                             "enabled": True})):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "profiles changed"):
                    operation(arrangement, request)
                self.assertEqual(self.path.read_bytes(), original)

    def test_explicit_replace_retains_id_and_exact_byte_backup(self):
        saved = self.save()
        self.path.write_bytes(json.dumps(json.loads(self.path.read_bytes()), separators=(",", ":")).encode() + b" \n")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.save("Desk")
        replaced = self.save("Renamed", id=saved["savedId"])
        self.assertEqual(replaced["savedId"], saved["savedId"])
        self.assertEqual([Path(p).read_bytes() for p in replaced["backupPaths"]], [original])
        self.assertEqual([p["name"] for p in replaced["profiles"]], ["Renamed"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_delete_works_offline_and_backs_up_exact_bytes(self):
        saved = self.save()
        original = self.path.read_bytes()
        with patch.object(arrangement, "snapshot", side_effect=RuntimeError("IPC unavailable")):
            listed = profiles.catalog(arrangement)
            self.assertEqual(listed["profiles"][0]["name"], "Desk")
            self.assertFalse(listed["profiles"][0]["canLoad"])
            deleted = profiles.delete(arrangement, self.selection(saved))
        self.assertEqual(deleted["profiles"], [])
        self.assertEqual([Path(p).read_bytes() for p in deleted["backupPaths"]], [original])

    def test_unsupported_topology_keeps_catalog_but_blocks_save_load(self):
        saved = self.save()
        healthy = copy.deepcopy(self.raw)
        cases = [healthy[:1], healthy + [dict(healthy[0], id=3, name="DP-9", serial="three", x=3840)],
                 healthy + [{"name": "off", "disabled": True}],
                 healthy + [{"name": "mirror", "mirrorOf": "DP-1"}],
                 [healthy[0], dict(healthy[1], width=0)]]
        for topology in cases:
            with self.subTest(topology=topology):
                self.raw = topology
                listed = profiles.catalog(arrangement)["profiles"][0]
                self.assertFalse(listed["canLoad"])
                self.assertEqual(listed["name"], "Desk")
                with self.assertRaises(ValueError):
                    profiles.load(arrangement, self.selection(saved))
        for topology in cases[2:]:
            self.raw = topology
            with self.assertRaises(ValueError):
                self.save("Other")

    def test_unadvertised_mode_only_loads_when_already_active(self):
        self.raw[0]["availableModes"] = []
        saved = self.save()
        self.assertTrue(saved["profiles"][0]["canLoad"])
        self.raw[0].update(width=1280, height=720)
        self.raw[1]["x"] = 1280
        self.assertFalse(profiles.catalog(arrangement)["profiles"][0]["canLoad"])
        with self.assertRaisesRegex(ValueError, "not advertised"):
            profiles.load(arrangement, self.selection(saved))

    def test_bad_scale_and_overlap_are_not_saved_or_loaded(self):
        saved = self.save()
        original = self.path.read_bytes()
        self.raw[0]["scale"] = 1.3
        with self.assertRaisesRegex(ValueError, "logical pixel"):
            self.save("Bad")
        self.assertEqual(self.path.read_bytes(), original)
        self.raw[0]["scale"] = 1
        document = json.loads(original)
        document["profiles"][0]["monitors"][1]["x"] = 1
        self.path.write_text(json.dumps(document))
        request = {"id": saved["savedId"], "revision": config.digest(self.path.read_bytes())}
        with self.assertRaisesRegex(ValueError, "overlap"):
            profiles.load(arrangement, request)

    def test_malformed_unknown_and_oversized_stores_are_never_overwritten(self):
        self.path.parent.mkdir(mode=0o700)
        for content in (b'{"version":1,"version":1,"profiles":[]}', b'{"version":99,"profiles":[]}',
                        b'{broken', b'{"version":1,"profiles":[],"session":"x"}',
                        b"[" * 1500 + b"]" * 1500, b" " * (profiles.MAX_BYTES + 1)):
            with self.subTest(content=content[:80]):
                self.path.write_bytes(content)
                request = {"name": "Desk", "baseline": arrangement.snapshot(), "revision": config.digest(content)}
                with self.assertRaises(ValueError):
                    profiles.save(arrangement, request)
                self.assertEqual(self.path.read_bytes(), content)

    def test_invalid_schema_references_and_geometry_are_refused(self):
        saved = self.save()
        original = json.loads(self.path.read_bytes())
        variants = []
        for field, value in (("transform", True), ("transform", 8), ("scale", float("nan")), ("width", 0)):
            document = copy.deepcopy(original)
            document["profiles"][0]["monitors"][0][field] = value
            variants.append(document)
        document = copy.deepcopy(original)
        document["profiles"][0]["workspaces"][0]["monitor"] = 5
        variants.append(document)
        document = copy.deepcopy(original)
        document["profiles"][0]["workspaces"].append(document["profiles"][0]["workspaces"][0])
        variants.append(document)
        document = copy.deepcopy(original)
        document["profiles"][0]["monitors"][0]["session"] = "runtime-only"
        variants.append(document)
        for document in variants:
            content = json.dumps(document).encode()
            self.path.write_bytes(content)
            with self.assertRaises(ValueError):
                profiles.delete(arrangement, {"id": saved["savedId"], "revision": config.digest(content)})
            self.assertEqual(self.path.read_bytes(), content)

    def test_v1_read_does_not_write_and_opt_in_migrates_with_exact_backup(self):
        saved = self.save()
        legacy = json.loads(self.path.read_bytes())
        legacy["version"] = 1
        del legacy["profiles"][0]["automatic"]
        original = (json.dumps(legacy, separators=(",", ":")) + " \n").encode()
        self.path.write_bytes(original)
        listed = profiles.catalog(arrangement)
        self.assertFalse(listed["profiles"][0]["automatic"])
        self.assertTrue(listed["profiles"][0]["canAutomate"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.bak")), [])
        enabled = profiles.set_automatic(arrangement, {"id": saved["savedId"], "revision": listed["revision"],
                                                       "enabled": True})
        self.assertTrue(enabled["profiles"][0]["automatic"])
        self.assertEqual([Path(p).read_bytes() for p in enabled["backupPaths"]], [original])
        self.assertTrue(profiles.catalog(arrangement)["profiles"][0]["automatic"])

    def test_automatic_enable_requires_live_saved_geometry_and_placements(self):
        saved = self.save()
        original = self.path.read_bytes()
        initial_monitors, initial_workspaces = copy.deepcopy(self.raw), copy.deepcopy(self.workspaces)
        for change in (lambda: self.raw[0].update(transform=2),
                       lambda: self.workspaces[0].update(monitor="HDMI-A-1"),
                       lambda: self.workspaces.pop(1),
                       lambda: self.raw.pop()):
            with self.subTest(change=change):
                self.raw, self.workspaces = copy.deepcopy(initial_monitors), copy.deepcopy(initial_workspaces)
                change()
                listed = profiles.catalog(arrangement)["profiles"][0]
                self.assertFalse(listed["canAutomate"])
                with self.assertRaises(ValueError):
                    profiles.set_automatic(arrangement, {**self.selection(saved), "enabled": True})
                self.assertEqual(self.path.read_bytes(), original)

    def test_automatic_enable_rechecks_live_state_before_write(self):
        saved = self.save()
        original = self.path.read_bytes()
        first = arrangement.snapshot()
        self.workspaces[0]["monitor"] = "HDMI-A-1"
        fresh = arrangement.snapshot()
        with patch.object(arrangement, "snapshot", side_effect=[first, fresh]):
            with self.assertRaisesRegex(ValueError, "placements are not live"):
                profiles.set_automatic(arrangement, {**self.selection(saved), "enabled": True})
        self.assertEqual(self.path.read_bytes(), original)

    def test_automatic_disable_is_offline_and_replacement_disarms(self):
        saved = self.save()
        enabled = profiles.set_automatic(arrangement, {**self.selection(saved), "enabled": True})
        original = self.path.read_bytes()
        with patch.object(arrangement, "snapshot", side_effect=RuntimeError("offline")):
            disabled = profiles.set_automatic(arrangement, {"id": saved["savedId"], "revision": enabled["revision"],
                                                            "enabled": False})
        self.assertFalse(disabled["profiles"][0]["automatic"])
        self.assertEqual([Path(p).read_bytes() for p in disabled["backupPaths"]], [original])
        profiles.set_automatic(arrangement, {"id": saved["savedId"], "revision": disabled["revision"], "enabled": True})
        original = self.path.read_bytes()
        replaced = self.save(id=saved["savedId"])
        self.assertFalse(replaced["profiles"][0]["automatic"])
        self.assertEqual([Path(p).read_bytes() for p in replaced["backupPaths"]], [original])

    def test_automatic_hardware_set_is_unordered_and_never_silently_reassigned(self):
        first = self.save()
        profiles.set_automatic(arrangement, {**self.selection(first), "enabled": True})
        first_profile = json.loads(self.path.read_bytes())["profiles"][0]
        self.raw.reverse()
        self.raw[0]["name"] = "USB-C-8"
        self.workspaces[1]["monitor"] = "USB-C-8"
        second = self.save("Alternative")
        document = json.loads(self.path.read_bytes())
        self.assertEqual(profiles.automation_key(first_profile), profiles.automation_key(document["profiles"][1]))
        self.assertFalse(second["profiles"][1]["canAutomate"])
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "already owns"):
            profiles.set_automatic(arrangement, {**self.selection(second), "enabled": True})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(profiles.catalog(arrangement)["profiles"][0]["automatic"])
        document["profiles"][1]["automatic"] = True
        self.path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "Only one automatic"):
            profiles.catalog(arrangement)

    def test_automatic_refuses_weak_or_duplicated_identities_but_manual_load_remains(self):
        for field, value in (("serial", ""), ("make", ""), ("model", ""), ("serial", "one")):
            with self.subTest(field=field, value=value):
                self.raw[1][field] = value
                saved = self.save("Weak", **({"id": saved["savedId"]} if self.path.exists() else {}))
                self.assertTrue(saved["profiles"][0]["canLoad"])
                self.assertFalse(saved["profiles"][0]["canAutomate"])
                original = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "unique, nonempty"):
                    profiles.set_automatic(arrangement, {**self.selection(saved), "enabled": True})
                self.assertEqual(self.path.read_bytes(), original)
                self.raw[1].update(make="Vendor", model="Panel", serial="two")

    def test_automatic_requires_saved_workspace_coverage_not_populated_extras(self):
        self.workspaces = [self.workspaces[0]]
        saved = self.save()
        self.workspaces.append({"id": 7, "monitor": "HDMI-A-1", "windows": 1, "ispersistent": False})
        listed = profiles.catalog(arrangement)["profiles"][0]
        self.assertTrue(listed["canLoad"])
        self.assertFalse(listed["canAutomate"])
        with self.assertRaisesRegex(ValueError, "saved positive workspace"):
            profiles.set_automatic(arrangement, {**self.selection(saved), "enabled": True})

    def test_new_schema_cannot_implicitly_or_weakly_authorize_automation(self):
        saved = self.save()
        original = json.loads(self.path.read_bytes())
        variants = []
        for value in (None, 0, 1, "true"):
            document = copy.deepcopy(original)
            document["profiles"][0]["automatic"] = value
            variants.append(document)
        document = copy.deepcopy(original)
        del document["profiles"][0]["automatic"]
        variants.append(document)
        document = copy.deepcopy(original)
        document["version"] = 1
        variants.append(document)
        document = copy.deepcopy(original)
        document["profiles"][0]["automatic"] = True
        document["profiles"][0]["monitors"][0]["serial"] = ""
        variants.append(document)
        for document in variants:
            content = json.dumps(document).encode()
            self.path.write_bytes(content)
            with self.assertRaises(ValueError):
                profiles.set_automatic(arrangement, {"id": saved["savedId"], "revision": config.digest(content),
                                                    "enabled": False})
            self.assertEqual(self.path.read_bytes(), content)

    def test_symlink_hardlink_and_writable_store_are_refused(self):
        saved = self.save()
        original = self.path.read_bytes()
        other = self.root / "other.json"
        other.write_bytes(original)
        self.path.unlink()
        self.path.symlink_to(other)
        with self.assertRaises((ValueError, OSError)):
            profiles.delete(arrangement, self.selection(saved))
        self.assertEqual(other.read_bytes(), original)
        self.path.unlink()
        os.link(other, self.path)
        with self.assertRaisesRegex(ValueError, "Unsafe profile file"):
            profiles.catalog(arrangement)
        self.path.unlink()
        self.path.write_bytes(original)
        self.path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, "Unsafe profile file"):
            profiles.catalog(arrangement)

    def test_symlink_directory_and_lock_are_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.path.parent.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Unsafe profile directory"):
            self.save()
        self.path.parent.unlink()
        self.path.parent.mkdir(mode=0o700)
        lock_target = self.root / "lock-target"
        lock_target.write_text("unchanged")
        (self.path.parent / ".profiles.lock").symlink_to(lock_target)
        with self.assertRaises((ValueError, OSError)):
            self.save()
        self.assertEqual(lock_target.read_text(), "unchanged")

    def test_concurrent_external_edit_preserved_after_backup(self):
        saved = self.save()
        original = self.path.read_bytes()
        external = original + b" \n"
        write = config.write_bytes
        def write_with_edit(path, data, mode=0o600):
            write(path, data, mode)
            if path.name.startswith(".profiles-"):
                self.path.write_bytes(external)
        with patch.object(config, "write_bytes", side_effect=write_with_edit):
            with self.assertRaisesRegex(ValueError, "Concurrent profile edit"):
                profiles.delete(arrangement, self.selection(saved))
        self.assertEqual(self.path.read_bytes(), external)
        self.assertEqual([p.read_bytes() for p in self.path.parent.glob("*.bak")], [original])

    def test_external_creation_cannot_be_clobbered_on_first_save(self):
        request = self.save_request()
        link = os.link
        external = b'{"version":1,"profiles":[]}\n'
        def create_before_link(source, target, **kwargs):
            self.path.write_bytes(external)
            return link(source, target, **kwargs)
        with patch.object(profiles.os, "link", side_effect=create_before_link):
            with self.assertRaises(ValueError):
                profiles.save(arrangement, request)
        self.assertEqual(self.path.read_bytes(), external)

    def test_external_edit_while_loading_refuses_stale_draft(self):
        saved = self.save()
        original = self.path.read_bytes()
        validate = arrangement.validate
        def validate_with_edit(request, current=None):
            result = validate(request, current)
            self.path.write_bytes(original + b"\n")
            return result
        with patch.object(arrangement, "validate", side_effect=validate_with_edit):
            with self.assertRaisesRegex(ValueError, "changed while loading"):
                profiles.load(arrangement, self.selection(saved))


if __name__ == "__main__":
    unittest.main()
