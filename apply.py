#!/usr/bin/env python3
"""Hyprland layout trials and configuration edits owned by independent workers."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time

import configuration as display_config
import profiles as display_profiles
import automation as display_automation
import output_owner

CONFIRM_SECONDS = 20
COORD_LIMIT = 32768
TERMINAL = {"kept", "reverted", "failed"}
IDENTITY = ("id", "name", "description", "make", "model", "serial")
GEOMETRY = ("width", "height", "scale", "transform", "refreshRate", "disabled", "mirrorOf")
POSITION = ("name", "x", "y", "width", "height", "refreshRate", "scale", "transform")
SETTINGS = ("currentFormat", "colorManagementPreset", "sdrBrightness", "sdrSaturation",
            "sdrMinLuminance", "sdrMaxLuminance", "vrr")
TOKEN_RE = re.compile(r"[0-9a-f]{48}\Z")


def run(args, timeout=4):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or f"{args[0]} failed.")
    return result.stdout.strip()


def query(kind, *args):
    return json.loads(run(["hyprctl", "-j", kind, *args]))


def lua_string(value):
    # Lua and JSON differ for control/unicode escapes. Decimal byte escapes are unambiguous.
    return '"' + ''.join(chr(b) if 32 <= b < 127 and b not in (34, 92)
                         else f"\\{b:03d}" for b in str(value).encode("utf-8")) + '"'


def lua_eval(code):
    result = run(["hyprctl", "eval", code])
    if result and result != "ok":
        raise ValueError(f"Hyprland rejected the change: {result[:400]}")


def numeric(value, label, minimum=None, maximum=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Invalid {label}.")
    if integer and value != int(value):
        raise ValueError(f"{label} must be a whole number.")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{label} is out of range.")
    return int(value) if integer else value


def indexed(items, key, label):
    if not isinstance(items, list):
        raise ValueError(f"Invalid {label} list.")
    result = {}
    for item in items:
        if not isinstance(item, dict) or key not in item:
            raise ValueError(f"Invalid {label}.")
        value = item[key]
        if not isinstance(value, (str, int)) or isinstance(value, bool) or value in result:
            raise ValueError(f"Invalid or duplicate {label}.")
        result[value] = item
    return result


def same_fields(before, after, keys):
    for key in keys:
        a, b = before.get(key), after.get(key)
        if key in ("refreshRate", "scale") and isinstance(a, (int, float)) and isinstance(b, (int, float)):
            tolerance = 0.010000001 if key == "refreshRate" else 0.000001
            if not math.isfinite(a) or not math.isfinite(b) or abs(a - b) > tolerance:
                return False
        elif a != b:
            return False
    return True


def mode_options(available):
    options = {}
    for text in available if isinstance(available, list) else []:
        if not isinstance(text, str):
            continue
        match = re.fullmatch(r"(\d{1,5})x(\d{1,5})@(\d{1,4}(?:\.\d{1,6})?)(?:Hz)?", text.strip())
        if not match:
            continue
        width, height, refresh = int(match[1]), int(match[2]), float(match[3])
        if not 1 <= width <= 65536 or not 1 <= height <= 65536 or not 0 < refresh <= 1000:
            continue
        value = f"{width}x{height}@{refresh:.12g}"
        options[value] = {"value": value, "label": f"{width} × {height} @ {refresh:.12g} Hz",
                          "width": width, "height": height, "refreshRate": refresh}
    return sorted(options.values(), key=lambda mode: (mode["width"], mode["height"], mode["refreshRate"]), reverse=True)


def logical_size(position, transform):
    scale = numeric(position.get("scale"), "display scale", 0.1, 16)
    quantized = round(scale * 120) / 120
    if abs(scale - quantized) > 0.000001:
        raise ValueError("Display scale must use compositor 1/120 increments.")
    width, height = position["width"] / quantized, position["height"] / quantized
    if any(abs(value - round(value)) > 0.000001 or value < 1 for value in (width, height)):
        raise ValueError("Display scale must produce integral logical pixel dimensions.")
    return (round(height), round(width)) if transform % 2 else (round(width), round(height))


def expected_outputs(baseline, positions):
    targets = indexed(positions, "name", "display target")
    enabled = []
    for monitor in baseline["monitors"]:
        expected = {**monitor, **targets.get(monitor["name"], {})}
        expected["logicalWidth"], expected["logicalHeight"] = logical_size(expected, expected["transform"])
        enabled.append(expected)
    return {**baseline, "monitors": enabled}


def snapshot():
    outputs = query("monitors", "all")
    monitors, disabled, mirrors, unavailable = [], [], [], []
    for raw in outputs:
        monitor = dict(raw)
        if monitor.get("disabled", False):
            disabled.append(monitor)
            continue
        if monitor.get("mirrorOf", "none") not in (None, "", "none"):
            mirrors.append(monitor["name"])
            continue
        try:
            width = numeric(monitor.get("width"), "display width", 1, 65536, True)
            height = numeric(monitor.get("height"), "display height", 1, 65536, True)
            scale = numeric(monitor.get("scale"), "display scale", 0.1, 16)
            transform = numeric(monitor.get("transform"), "display rotation", 0, 7, True)
            numeric(monitor.get("x"), "display X coordinate", -COORD_LIMIT, COORD_LIMIT, True)
            numeric(monitor.get("y"), "display Y coordinate", -COORD_LIMIT, COORD_LIMIT, True)
            numeric(monitor.get("refreshRate"), "display refresh rate", 0.001, 1000)
            if transform % 2:
                width, height = height, width
            # Hyprland rounds transformed pixels divided by scale to logical pixels.
            monitor["logicalWidth"] = numeric(math.floor(width / scale + 0.5), "logical display width", 1)
            monitor["logicalHeight"] = numeric(math.floor(height / scale + 0.5), "logical display height", 1)
        except ValueError as error:
            # Keep identity/presentation only, never invalid geometry or arbitrary
            # nested IPC values that could leak NaN/Infinity into strict JSON.
            record = {}
            for key in IDENTITY:
                value = monitor.get(key)
                record[key] = value if (
                    value is None or isinstance(value, (str, bool, int))
                    or isinstance(value, float) and math.isfinite(value)) else None
            record["name"] = str(monitor.get("name", "Unknown output"))
            record["reason"] = f"Display geometry is unavailable: {error}"
            unavailable.append(record)
            continue
        monitor["modeOptions"] = mode_options(monitor.get("availableModes"))
        monitors.append(monitor)
    result = {"ok": True, "message": "Live layout loaded.", "monitors": monitors,
              "disabledMonitors": disabled, "mirroredOutputs": mirrors, "unavailableMonitors": unavailable,
              "workspaces": query("workspaces"), "activeWindow": query("activewindow").get("address", ""),
              "session": os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")}
    return result


def require_usable_outputs(*snapshots):
    unavailable = {}
    for state in snapshots:
        for name, monitor in indexed(state.get("unavailableMonitors", []), "name", "unavailable display").items():
            unavailable[name] = f"{name}: {monitor.get('reason', 'Display geometry is unavailable.')}"
    if unavailable:
        raise ValueError(" ".join(unavailable.values()) + " Repair the output and refresh before arranging.")


def compare_outputs(before, after, catalog=False):
    require_usable_outputs(before, after)
    if before.get("session") != after.get("session"):
        raise ValueError("The compositor session changed. Refresh the panel.")
    if before.get("mirroredOutputs") or after.get("mirroredOutputs"):
        raise ValueError("Mirrored displays are not supported by Arrange.")
    old = indexed(before.get("monitors"), "name", "display")
    new = indexed(after.get("monitors"), "name", "display")
    if not old or old.keys() != new.keys():
        raise ValueError("Connected displays changed. Refresh the panel.")
    keys = IDENTITY + GEOMETRY + SETTINGS + ("x", "y")
    for name, monitor in old.items():
        if not same_fields(monitor, new[name], keys):
            raise ValueError(f"Display {name} changed. Refresh the panel.")
        if catalog and monitor.get("modeOptions", []) != new[name].get("modeOptions", []):
            raise ValueError(f"Display {name} advertised modes changed. Refresh the panel.")
    old_disabled = indexed(before.get("disabledMonitors", []), "name", "disabled display")
    new_disabled = indexed(after.get("disabledMonitors", []), "name", "disabled display")
    if old_disabled.keys() != new_disabled.keys() or any(
            any(m.get(k) != new_disabled[n].get(k) for k in IDENTITY + ("disabled",))
            for n, m in old_disabled.items()):
        raise ValueError("Disabled displays changed. Refresh the panel.")
    return new


def check_monitor_rules(names, *, allow_new=False):
    # Never execute user Lua to discover rules. Existing exact rules can be
    # merged without replacing their policy; missing rules need the separate
    # field-selective output-management backend, never a guessed Lua rule.
    loaded = run(["hyprctl", "repl",
                  'return package.loaded["hypr.monitors"], package.searchpath("hypr.monitors", package.path)'])
    parts = loaded.split("\t")
    if len(parts) != 2 or parts[0] != "true":
        raise ValueError("Cannot establish exact monitor rules safely; display changes are unavailable.")
    source = display_config.read_file(Path(parts[1]))
    if source is None:
        raise ValueError("The loaded monitor configuration is unavailable.")
    rules = display_config.monitor_rules(source.decode("utf-8"))
    if any(rule["reason"] for rule in rules):
        raise ValueError("Monitor rules must use plain exact connector declarations for safe display changes.")
    outputs = {rule["selector"] for rule in rules}
    missing = set(names) - outputs
    if missing and not allow_new:
        raise ValueError("No safe exact monitor rule for " + ", ".join(sorted(missing)) + ".")
    return sorted(missing)


def validate(request, current=None, *, allow_new=True):
    if not isinstance(request, dict) or not isinstance(request.get("baseline"), dict):
        raise ValueError("Missing live baseline. Refresh the panel.")
    baseline = request["baseline"]
    live_validation = current is None
    current = snapshot() if current is None else current
    monitors = compare_outputs(baseline, current, catalog=True)
    active = dict(monitors)
    positions = indexed(request.get("positions"), "name", "display target")
    if positions.keys() != active.keys():
        raise ValueError("Provide exactly one target for every enabled display.")
    normalized, sizes = [], {}
    for name, position in positions.items():
        monitor = active[name]
        target = {"name": name,
                  "x": numeric(position.get("x"), "X coordinate", -COORD_LIMIT, COORD_LIMIT, True),
                  "y": numeric(position.get("y"), "Y coordinate", -COORD_LIMIT, COORD_LIMIT, True),
                  "width": numeric(position.get("width"), "display width", 1, 65536, True),
                  "height": numeric(position.get("height"), "display height", 1, 65536, True),
                  "refreshRate": numeric(position.get("refreshRate"), "display refresh rate", 0.001, 1000),
                  "scale": numeric(position.get("scale"), "display scale", 0.1, 16),
                  "transform": numeric(position.get("transform"), "display rotation", 0, 7, True)}
        mode_keys = ("width", "height", "refreshRate")
        if same_fields(monitor, target, mode_keys):
            target.update({key: monitor[key] for key in mode_keys})
        else:
            advertised = next((mode for mode in monitor.get("modeOptions", [])
                               if same_fields(mode, target, mode_keys)), None)
            if advertised is None:
                raise ValueError(f"Display {name}: requested mode is not advertised. Refresh the panel.")
            target.update({key: advertised[key] for key in mode_keys})
        sizes[name] = logical_size(target, target["transform"])
        target["scale"] = round(target["scale"] * 120) / 120
        normalized.append(target)
    enabled_names = set(active)
    min_x, min_y = min(p["x"] for p in normalized), min(p["y"] for p in normalized)
    for position in normalized:
        position["x"] -= min_x
        position["y"] -= min_y
        width, height = sizes[position["name"]]
        if position["x"] + width > COORD_LIMIT or position["y"] + height > COORD_LIMIT:
            raise ValueError("The display layout is too large.")
    edges = {p["name"]: set() for p in normalized}
    for index, a in enumerate(normalized):
        aw, ah = sizes[a["name"]]
        for b in normalized[index + 1:]:
            bw, bh = sizes[b["name"]]
            dx = min(a["x"] + aw, b["x"] + bw) - max(a["x"], b["x"])
            dy = min(a["y"] + ah, b["y"] + bh) - max(a["y"], b["y"])
            if dx > 0 and dy > 0:
                raise ValueError(f"Displays {a['name']} and {b['name']} overlap.")
            if (dx == 0 and dy > 0) or (dy == 0 and dx > 0):
                edges[a["name"]].add(b["name"])
                edges[b["name"]].add(a["name"])
    reached, pending = set(), [normalized[0]["name"]]
    while pending:
        name = pending.pop()
        if name not in reached:
            reached.add(name)
            pending.extend(edges[name] - reached)
    if reached != enabled_names:
        raise ValueError("Displays must share an edge; gaps and corner-only contact are not allowed.")
    changes = indexed(request.get("workspaces"), "id", "workspace move")
    old_ws = indexed(baseline.get("workspaces"), "id", "baseline workspace")
    live_ws = indexed(current["workspaces"], "id", "live workspace")
    moves = []
    for wid, change in changes.items():
        numeric(wid, "workspace ID", 1, 2147483647, True)
        source, target = change.get("source"), change.get("target")
        if (not isinstance(source, str) or not isinstance(target, str)
                or source not in active or target not in enabled_names):
            raise ValueError("A workspace refers to a disabled or missing display.")
        if wid not in old_ws or wid not in live_ws or old_ws[wid]["monitor"] != source or live_ws[wid]["monitor"] != source:
            raise ValueError(f"Workspace {wid} moved or disappeared. Refresh the panel.")
        if source == target:
            raise ValueError("Include only changed workspace placements.")
        moves.append({"id": wid, "source": source, "target": target})
    profile = request.get("profileWorkspaces")
    removals = []
    if profile is not None:
        saved = indexed(profile, "id", "profile workspace")
        if not saved:
            raise ValueError("Provide the complete nonempty saved workspace list.")
        profile = []
        for wid, placement in saved.items():
            numeric(wid, "profile workspace ID", 1, 2147483647, True)
            target = placement.get("target")
            if not isinstance(target, str) or target not in enabled_names:
                raise ValueError("A saved workspace refers to a disabled or missing display.")
            old, live = old_ws.get(wid), live_ws.get(wid)
            if (old is None) != (live is None) or old and old["monitor"] != live["monitor"]:
                raise ValueError(f"Saved workspace {wid} changed. Refresh the panel.")
            if wid in changes and changes[wid]["target"] != target:
                raise ValueError(f"Workspace {wid} has conflicting draft targets.")
            profile.append({"id": wid, "target": target})
        for wid, old in old_ws.items():
            if wid <= 0 or wid in saved:
                continue
            live = live_ws.get(wid)
            if live is not None and live["monitor"] != old["monitor"]:
                raise ValueError(f"Extra workspace {wid} moved externally. Refresh the panel.")
            if live is None and not workspace_may_expire(baseline, wid):
                raise ValueError(f"Extra workspace {wid} disappeared. Refresh the panel.")
        removals = sorted(wid for wid, workspace in live_ws.items()
                          if wid > 0 and wid not in saved
                          and type(workspace.get("windows")) is int and workspace["windows"] == 0)
        if any(move["id"] in removals for move in moves):
            raise ValueError("An empty extra workspace cannot be moved and retired together.")
        retained = {w["id"]: w["monitor"] for w in live_ws.values() if w["id"] > 0 and w["id"] not in removals}
        retained.update({move["id"]: move["target"] for move in moves})
        retained.update({placement["id"]: placement["target"] for placement in profile})
        if set(retained.values()) != enabled_names:
            raise ValueError("Keep at least one saved or populated workspace on every enabled display.")
    count = sum(not same_fields(position, active[position["name"]], POSITION)
                for position in normalized)
    if count and live_validation:
        missing = check_monitor_rules(active, allow_new=allow_new)
        if missing:
            output_owner.preflight(sys.modules[__name__], current, normalized)
    workspace_count = len({move["id"] for move in moves}
                          | {placement["id"] for placement in profile or []} | set(removals))
    return {"ok": True, "positions": normalized, "workspaces": moves, "profileWorkspaces": profile,
            "removeWorkspaces": removals, "displayChanges": count,
            "workspaceChanges": workspace_count,
            "message": (f"{count} display change(s), {workspace_count} workspace change(s)."
                        + (f" Restore/retain {len(profile)} saved workspace(s); retire {len(removals)} empty extra(s)."
                           if profile is not None else ""))}


def workspace_rules():
    rules = query("workspacerules")
    if not isinstance(rules, list) or any(
            not isinstance(rule, dict) or not isinstance(rule.get("workspaceString"), str)
            or type(rule.get("enabled")) is not bool
            or "persistent" in rule and type(rule["persistent"]) is not bool for rule in rules):
        raise ValueError("Cannot inspect live workspace rules safely.")
    enabled = [rule["workspaceString"] for rule in rules if rule["enabled"]]
    if len(enabled) != len(set(enabled)):
        raise ValueError("Duplicate enabled workspace selectors prevent safe profile restoration.")
    return rules


def exact_workspace_id(selector):
    # Hyprland 0.56.2 parses numeric selectors with decimal stoi, while
    # replaceOrAdd compares the original selector string, not the numeric ID.
    if re.fullmatch(r"0*[1-9][0-9]*", selector) and len(selector.lstrip("0")) <= 10:
        wid = int(selector)
        if wid <= 2147483647:
            return wid
    return None


def prepare_profile(state, rules):
    plan = state["plan"]
    affected = {w["id"] for w in plan["profileWorkspaces"]} | set(plan["removeWorkspaces"])
    persistent = []
    for rule in rules:
        if not rule["enabled"] or not rule.get("persistent"):
            continue
        wid = exact_workspace_id(rule["workspaceString"])
        if wid is None and not rule["workspaceString"].startswith("special"):
            raise ValueError("A broad or named persistent workspace rule prevents safe profile restoration.")
        if wid in affected:
            persistent.append(rule)
    occupied = {rule["workspaceString"] for rule in rules}
    pins, restore_pins = [], []
    originals = indexed(state["baseline"]["workspaces"], "id", "workspace")
    restoration = [{"id": wid, "target": originals[wid]["monitor"]} for wid in plan["removeWorkspaces"]]
    for workspace in plan["profileWorkspaces"] + restoration:
        selector = "0" + str(workspace["id"])
        while selector in occupied:
            selector = "0" + selector
        occupied.add(selector)
        entry = {**workspace, "selector": selector}
        (pins if workspace in plan["profileWorkspaces"] else restore_pins).append(entry)
    return {"beforeRules": rules, "persistent": persistent, "pins": pins, "restorePins": restore_pins}


def profile_key(state):
    return lua_string("_display_workspaces_" + state["token"])


def profile_status(state):
    # The compositor journal survives Python worker death. Handles expire on
    # configuration reload, so a replacement worker must not reacquire rules by
    # selector and accidentally take ownership of a user's new declarations.
    code = (f"local j=rawget(_G,{profile_key(state)}); "
            "if not j then return 'missing' end; "
            "for _,r in pairs(j.pins) do if r:is_enabled()~=true then return 'changed' end end; "
            "for _,r in pairs(j.rules) do if r:is_enabled()~=true then return 'changed' end end; "
            "local ids={}; for id in pairs(j.cleaned) do ids[#ids+1]=id end; "
            "table.sort(ids); return 'ids:'..table.concat(ids,',')")
    result = run(["hyprctl", "repl", code])
    if result in ("missing", "changed"):
        raise ValueError("Profile workspace rules were reloaded or changed externally.")
    if not result.startswith("ids:"):
        raise ValueError("Cannot inspect the compositor's profile journal.")
    try:
        return {int(wid) for wid in result[4:].split(",") if wid}
    except ValueError as error:
        raise ValueError("Cannot inspect the compositor's profile journal.") from error


def expected_profile_rules(state, cleaned):
    journal = state["profileJournal"]
    saved = {pin["id"] for pin in journal["pins"]}
    changed = {rule["workspaceString"] for rule in journal["persistent"]
               if exact_workspace_id(rule["workspaceString"]) in saved | set(cleaned)}
    rules = [dict(rule, persistent=False) if rule["enabled"] and rule["workspaceString"] in changed else rule
             for rule in journal["beforeRules"]]
    return rules + [{"workspaceString": pin["selector"], "enabled": True, "persistent": True,
                     "monitor": pin["target"]} for pin in journal["pins"]]


def require_profile_rules(state, cleaned):
    if workspace_rules() != expected_profile_rules(state, cleaned):
        raise ValueError("Workspace rules changed externally; refusing a stale profile transaction.")


def apply_profile(state):
    journal, baseline = state["profileJournal"], state["baseline"]
    if workspace_rules() != journal["beforeRules"]:
        raise ValueError("Workspace rules changed before profile restoration.")
    current = snapshot()
    compare_outputs(expected_outputs(baseline, state["plan"]["positions"]), current)
    saved = {pin["id"] for pin in journal["pins"]}
    code = ''.join("do " + monitor_guard(monitor) + "end; " for monitor in current["monitors"])
    for workspace in current["workspaces"]:
        if workspace["id"] <= 0:
            continue
        code += (f"do local w=hl.get_workspace({workspace['id']}); "
                 f"assert(w and w.monitor and w.monitor.name=={lua_string(workspace['monitor'])}, "
                 "'Workspace changed before profile mutation'); end; ")
    present = {workspace["id"] for workspace in current["workspaces"]}
    for wid in saved - present:
        code += f"assert(not hl.get_workspace({wid}), 'Saved workspace appeared before profile mutation'); "
    code += (f"assert(rawget(_G,{profile_key(state)})==nil, 'Profile journal already exists'); "
             f"local j={{pins={{}},rules={{}},cleaned={{}},temporary={{}}}}; _G[{profile_key(state)}]=j; ")
    for rule in journal["persistent"]:
        if exact_workspace_id(rule["workspaceString"]) not in saved:
            continue
        selector = lua_string(rule["workspaceString"])
        code += (f"j.rules[{selector}]=hl.workspace_rule({{workspace={selector}}}); "
                 f"hl.workspace_rule({{workspace={selector},persistent=false}}); ")
    for pin in journal["pins"]:
        selector = lua_string(pin["selector"])
        code += (f"j.pins[{selector}]=hl.workspace_rule({{workspace={selector},"
                 f"monitor={lua_string(pin['target'])},persistent=true}}); ")
    code += "hl.exec_scheduled_prop_refresh_immediately();"
    lua_eval(code)


def cleanup_profile(state, root=None):
    cleaned = profile_status(state)
    require_profile_rules(state, cleaned)
    current = snapshot()
    compare_outputs(expected_outputs(state["baseline"], state["plan"]["positions"]), current)
    plan = state["plan"]
    # Workspace moves can create empty replacements after the initial plan.
    # Journal these backend-derived candidates before touching them as well.
    originals = indexed(state["baseline"]["workspaces"], "id", "workspace")
    saved = {placement["id"] for placement in plan["profileWorkspaces"]}
    replacements = [w for w in current["workspaces"] if w["id"] > 0 and w["id"] not in originals
                    and w["id"] not in saved and type(w.get("windows")) is int and w["windows"] == 0]
    sources = {str(wid): originals[wid]["monitor"] for wid in plan["removeWorkspaces"] if wid in originals}
    sources.update(state["profileJournal"].get("cleanupSources", {}))
    sources.update({str(w["id"]): w["monitor"] for w in replacements})
    plan["removeWorkspaces"] = sorted(set(plan["removeWorkspaces"]) | {w["id"] for w in replacements})
    state["profileJournal"]["cleanupSources"] = sources
    if root is not None:
        with locked(root):
            latest = load(root, state["token"])
            state["request"] = latest.get("request", "")
            save(root, state)
    retired = set(plan["removeWorkspaces"])
    retained = {w["id"]: w["monitor"] for w in current["workspaces"] if w["id"] > 0 and w["id"] not in retired}
    code = ''.join("do " + monitor_guard(monitor) + "end; " for monitor in current["monitors"])
    code += f"local j=rawget(_G,{profile_key(state)}); assert(j, 'Profile journal lost'); "
    for wid in plan["removeWorkspaces"]:
        source = sources[str(wid)]
        replacement = next((rid for rid, target in retained.items() if target == source), None)
        if replacement is None:
            raise ValueError(f"No retained workspace can replace empty workspace {wid} on {source}.")
        code += (f"do local w=hl.get_workspace({wid}); "
                 # Both conditions are live compositor facts, evaluated in the
                 # same event-loop callback as persistence and visibility changes.
                 f"if not w or (w.windows==0 and w.is_empty and w.monitor "
                 f"and w.monitor.name=={lua_string(source)}) then "
                 f"j.cleaned[{wid}]=true; ")
        for rule in state["profileJournal"]["persistent"]:
            if exact_workspace_id(rule["workspaceString"]) != wid:
                continue
            selector = lua_string(rule["workspaceString"])
            code += (f"j.rules[{selector}]=hl.workspace_rule({{workspace={selector}}}); "
                     f"hl.workspace_rule({{workspace={selector},persistent=false}}); ")
        code += (f"if w and w.visible then local r=hl.get_workspace({replacement}); "
                 "assert(r and r.monitor==w.monitor, 'Cleanup replacement changed'); "
                 "w.monitor:set_workspace({workspace=r.name}) end; end; end; ")
    code += "hl.exec_scheduled_prop_refresh_immediately();"
    lua_eval(code)


def verify_trial(state):
    plan = state["plan"]
    if state.get("outputOwner"):
        output_owner.verify(sys.modules[__name__], runtime_dir(), state["outputOwner"])
    cleaned = set()
    if plan.get("profileWorkspaces") is not None:
        cleaned = profile_status(state)
        if not cleaned <= set(plan["removeWorkspaces"]):
            raise ValueError("The profile cleanup journal is inconsistent.")
        require_profile_rules(state, cleaned)
    verify_arrangement(state["baseline"], plan["positions"], plan["workspaces"],
                       plan.get("profileWorkspaces"), cleaned)


def settle_trial(state):
    deadline = time.monotonic() + 3
    while True:
        try:
            verify_trial(state)
            return
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def runtime_dir():
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    info = base.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("A private user runtime directory is required.")
    session = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not session:
        raise ValueError("No Hyprland session is available.")
    path = base / ("display-workspaces-arrange-" + hashlib.sha256(session.encode()).hexdigest()[:16])
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe arrangement runtime directory.")
    return path


@contextlib.contextmanager
def locked(root, name="state.lock", blocking=True):
    fd = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def atomic(root, name, value):
    temporary = root / ("." + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / name)
    finally:
        temporary.unlink(missing_ok=True)


def load(root, token):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ValueError("Invalid trial token.")
    fd = os.open(root / (token + ".json"), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        return json.load(stream)


def save(root, state):
    atomic(root, state["token"] + ".json", state)


def response(state):
    remaining = max(0, math.ceil(state.get("deadline", 0) - time.monotonic())) if state["state"] == "pending" else 0
    return {"ok": state["state"] != "failed", "token": state["token"], "state": state["state"],
            "secondsRemaining": remaining, "message": state["message"],
            "automatic": state.get("automatic", False), "profileName": state.get("profileName", "")}


def expire_unstarted(root, state):
    # A launcher can die between reservation and systemd-run. This phase has
    # never mutated anything, and late workers see the terminal state under lock.
    if state["state"] == "starting" and state["phase"] == "prepared" and time.monotonic() - state["created"] > 15:
        state.update(state="failed", message="Watchdog did not start; nothing changed.")
        save(root, state)


def resume():
    root = runtime_dir()
    with locked(root):
        active = root / "active.json"
        if not active.exists():
            return {"ok": True}
        state = load(root, json.loads(active.read_text())["token"])
        expire_unstarted(root, state)
        if state["state"] in TERMINAL:
            return {"ok": True}
        return {**response(state), "baseline": state["baseline"], "plan": state["plan"],
                "uiScreen": state.get("uiScreen", state["baseline"]["monitors"][0]["name"])}

def resume_refresh():
    """Release stale geometry ownership and report fresh post-suspend health."""
    root = runtime_dir()
    with locked(root):
        display_automation.suppress(sys.modules[__name__], root)
        active = root / "active.json"
        if active.exists():
            state = load(root, json.loads(active.read_text())["token"])
            expire_unstarted(root, state)
            if state["state"] not in TERMINAL:
                state["request"] = "revert"
                save(root, state)
                return {"ok": False, "status": "reverting",
                        "message": "System resumed during a display preview; reverting before refresh."}
        try:
            require_no_forget(root)
            output_owner.release_after_resume(sys.modules[__name__], root)
            current = snapshot()
        except Exception as error:
            return {"ok": False, "status": "blocked",
                    "message": f"Post-resume display refresh is blocked: {error}"}
        healthy = bool(current["monitors"]) and not current.get("unavailableMonitors")
        return {**current, "resumeHealthy": healthy, "status": "ready" if healthy else "blocked",
                "message": ("Displays settled after resume."
                            if healthy else "Displays are not healthy after resume; layout automation remains blocked.")}



def begin(request):
    root = runtime_dir()
    with locked(root):
        display_automation.suppress(sys.modules[__name__], root)
        return begin_locked(root, request)


def begin_locked(root, request, *, automatic_profile=None):
    """Caller owns runtime lock; unattended authorization is never read from user JSON."""
    require_no_forget(root)
    require_no_preview(root)
    current = snapshot()
    plan = validate(request, current)
    monitor_backend = "rules"
    if plan["displayChanges"]:
        missing = check_monitor_rules((m["name"] for m in current["monitors"]),
                                      allow_new=automatic_profile is None)
        if missing or output_owner.active(sys.modules[__name__], root):
            output_owner.preflight(sys.modules[__name__], current, plan["positions"])
            monitor_backend = "output-management"
    if not plan["displayChanges"] and not plan["workspaceChanges"]:
        raise ValueError("There are no arrangement changes to try.")
    if not shutil.which("systemd-run") or not shutil.which("hyprctl"):
        raise ValueError("systemd-run and hyprctl are required for safe rollback.")
    token = secrets.token_hex(24)
    ui_screen = request.get("uiScreen")
    if not isinstance(ui_screen, str) or ui_screen not in {m["name"] for m in current["monitors"]}:
        ui_screen = next((m["name"] for m in current["monitors"] if m.get("focused")), current["monitors"][0]["name"])
    state = {"token": token, "state": "starting", "message": "Starting 20-second preview…",
             "baseline": current, "plan": plan, "request": "", "phase": "prepared",
             "created": time.monotonic(), "deadline": 0, "mutationStarted": False, "uiScreen": ui_screen,
             "automatic": automatic_profile is not None, "monitorBackend": monitor_backend}
    if automatic_profile is not None:
        state.update(automaticProfile=automatic_profile, profileName=automatic_profile["name"],
                     message=f"Starting guarded automatic restoration of {automatic_profile['name']}…")
    if plan["profileWorkspaces"] is not None:
        state["profileJournal"] = prepare_profile(state, workspace_rules())
    save(root, state)
    atomic(root, "active.json", {"token": token})
    args = ["systemd-run", "--user", "--quiet", "--collect", "--service-type=exec",
            f"--unit=display-workspaces-arrange-{token}", "--property=Restart=on-failure",
            "--property=RestartSec=1", "--property=StartLimitIntervalSec=0",
            "--property=TimeoutStopSec=25", "--property=UMask=0077"]
    for name in ("HOME", "XDG_CONFIG_HOME", "HYPRLAND_INSTANCE_SIGNATURE", "XDG_RUNTIME_DIR",
                 "WAYLAND_DISPLAY", "PATH", "DBUS_SESSION_BUS_ADDRESS"):
        if name in os.environ:
            args.append(f"--setenv={name}={os.environ[name]}")
    args.extend([sys.executable, str(Path(__file__).resolve()), "worker", token])
    try:
        run(args, timeout=10)
    except Exception as error:
        # A late worker cannot mutate until this lock is released, then sees failure.
        state.update(state="failed", message=f"Watchdog could not start; nothing changed. {error}")
        save(root, state)
    return response(state)


def control(command, token):
    root = runtime_dir()
    with locked(root):
        state = load(root, token)
        expire_unstarted(root, state)
        if state["state"] not in TERMINAL and command in ("keep", "revert"):
            if command == "keep" and state.get("automatic", False):
                return {**response(state), "ok": False,
                        "message": "Automatic restoration must complete its full 20-second observation. Revert remains available."}
            if command == "keep" and state["state"] != "pending":
                return {**response(state), "ok": False, "message": "Wait until the trial is ready before keeping it."}
            if command == "keep":
                verify_trial(state)
            # A rollback request is irreversible, including racing keep/revert callers.
            if state.get("request") != "revert":
                state["request"] = command
                save(root, state)
        return response(state)


def same_identity(a, b):
    return all(a.get(key) == b.get(key) for key in IDENTITY)


def monitor_guard(monitor):
    name = lua_string(monitor["name"])
    # Resolve and check in the same Lua evaluation as mutation: no stale connector rule
    # is installed after a hot-unplug between a Python query and compositor processing.
    return (f"local m=hl.get_monitor({name}); "
            f"assert(m and m.id=={monitor['id']} and m.serial=={lua_string(monitor.get('serial', ''))} "
            f"and m.width=={monitor['width']} and m.height=={monitor['height']} "
            f"and m.x=={monitor['x']} and m.y=={monitor['y']} "
            f"and math.abs(m.scale-{monitor['scale']})<=0.000001 and m.transform=={monitor['transform']} "
            f"and math.abs(m.refresh_rate-{monitor['refreshRate']})<=0.010000001 "
            f"and not m.is_mirror, 'Display changed before mutation'); ")


def set_positions(baseline, positions, force=False, recovery=False, owner=None):
    current = snapshot()
    if owner is not None:
        if not recovery:
            compare_outputs(baseline, current, catalog=True)
        output_owner.apply(sys.modules[__name__], runtime_dir(), current, positions, owner)
        return
    if not recovery:
        compare_outputs(baseline, current, catalog=True)
    original = indexed(baseline["monitors"], "name", "display")
    live = indexed(current["monitors"], "name", "display")
    selected = []
    for position in positions:
        old, now = original[position["name"]], live.get(position["name"])
        if now is None or not same_identity(old, now):
            raise ValueError(f"Display {old['name']} disconnected or was replaced.")
        if not same_fields(old, now, GEOMETRY + ("x", "y")):
            raise ValueError(f"Display {old['name']} changed geometry; refusing a stale rule.")
        if force or not same_fields(now, position, POSITION):
            selected.append(position)
    if not selected:
        return
    pieces = []
    for position in selected:
        now = live[position["name"]]
        # Exact-selector merges preserve unexposed ICC/HDR/VRR policy.
        fields = {"output": position["name"], "position": f"{position['x']}x{position['y']}",
                  "mode": f"{position['width']}x{position['height']}@{position['refreshRate']}",
                  "scale": position["scale"], "transform": position["transform"]}
        encoded = ','.join(f"{key}={lua_string(value) if isinstance(value, str) else value}" for key, value in fields.items())
        pieces.append((monitor_guard(now), f"hl.monitor({{{encoded}}}); "))
    check_monitor_rules(position["name"] for position in selected)
    # Preflight ALL outputs in the compositor before installing ANY rule.
    lua_eval(''.join("do " + guard + "end; " for guard, _ in pieces) + ''.join(code for _, code in pieces))




def workspace_may_expire(baseline, wid):
    # Hyprland may release empty, nonpersistent workspaces when they stop being
    # visible. Missing lifecycle metadata must remain a strict failure.
    return any(w["id"] == wid and type(w.get("windows")) is int and w["windows"] == 0
               and w.get("ispersistent") is False for w in baseline["workspaces"])


def move_workspace(baseline, wid, target, expected_source=None, recovery=False, expected=None):
    originals = indexed(baseline["monitors"], "name", "display")
    may_expire = workspace_may_expire(baseline, wid)
    current = snapshot()
    if not recovery:
        require_usable_outputs(expected or baseline, current)
    live = indexed(current["monitors"], "name", "display")
    workspace = indexed(current["workspaces"], "id", "workspace").get(wid)
    if target not in live or not same_identity(originals[target], live[target]):
        raise ValueError(f"Workspace {wid}: original display {target} is unavailable.")
    if workspace is None:
        if may_expire:
            return
        raise ValueError(f"Workspace {wid} disappeared.")
    if expected_source is not None and workspace["monitor"] != expected_source:
        raise ValueError(f"Workspace {wid} moved externally.")
    if workspace["monitor"] == target:
        return
    source = lua_string(workspace["monitor"])
    code = monitor_guard(live[target]) + f"local w=hl.get_workspace({wid}); "
    if may_expire:
        code += "if not w then return end; "
    lua_eval(code + f"assert(w and w.monitor and w.monitor.name=={source}, 'Workspace source changed'); "
             f"local r=hl.dispatch(hl.dsp.workspace.move({{workspace={wid},monitor={lua_string(target)}}})); "
             "assert(not r or r.ok~=false, 'Workspace move failed')")


def restore_view(baseline, recovery=False):
    if not recovery:
        require_usable_outputs(baseline, snapshot())
    # Do not move a workspace merely to make it visible again. A moved active
    # workspace cannot remain visible on its former display.
    code = []
    for monitor in baseline["monitors"]:
        wid = monitor.get("activeWorkspace", {}).get("id", 0)
        if wid <= 0:
            continue
        name = lua_string(monitor["name"])
        code.append(f"do local m=hl.get_monitor({name}); local w=hl.get_workspace({wid}); "
                    f"if m and m.id=={monitor['id']} and m.serial=={lua_string(monitor.get('serial', ''))} "
                    "and w and w.monitor==m then m:set_workspace({workspace=w.name}) end end;")
    focused = next((m for m in baseline["monitors"] if m.get("focused")), None)
    if focused:
        name = lua_string(focused["name"])
        code.append(f"do local m=hl.get_monitor({name}); if m and m.id=={focused['id']} "
                    f"and m.serial=={lua_string(focused.get('serial', ''))} then hl.dispatch(hl.dsp.focus({{monitor=m}})) end end;")
    address = baseline.get("activeWindow", "")
    if re.fullmatch(r"0x[0-9a-fA-F]+", address):
        code.append(f"do local w=hl.get_window({lua_string('address:' + address)}); "
                    "if w then hl.dispatch(hl.dsp.focus({window=w})) end end;")
    if code:
        lua_eval(''.join(code))


def verify_arrangement(baseline, positions, moves, profile=None, cleaned=()):
    current = snapshot()
    compare_outputs(expected_outputs(baseline, positions), current)
    workspaces = indexed(current["workspaces"], "id", "workspace")
    expected = {w["id"]: w["monitor"] for w in baseline["workspaces"] if w["id"] > 0}
    expected.update({move["id"]: move["target"] for move in moves})
    saved = {placement["id"]: placement["target"] for placement in profile or []}
    expected.update(saved)
    for wid in cleaned:
        workspace = workspaces.get(wid)
        if workspace is None:
            expected.pop(wid, None)
        elif type(workspace.get("windows")) is int and workspace["windows"] == 0:
            raise ValueError(f"Empty extra workspace {wid} did not retire.")
    for wid, target in expected.items():
        if wid not in saved and wid not in workspaces and workspace_may_expire(baseline, wid):
            continue
        if workspaces.get(wid, {}).get("monitor") != target:
            raise ValueError(f"Workspace {wid} did not retain its intended display {target}.")
        if wid in saved and workspaces[wid].get("ispersistent") is not True:
            raise ValueError(f"Saved workspace {wid} was not retained for the session.")


def settle(baseline, positions, moves):
    deadline = time.monotonic() + 3
    while True:
        try:
            verify_arrangement(baseline, positions, moves)
            return
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def rollback_profile(state):
    journal = state["profileJournal"]
    current, rules = snapshot(), workspace_rules()
    live = indexed(current["monitors"], "name", "display")
    workspaces = indexed(current["workspaces"], "id", "workspace")
    original = indexed(state["baseline"]["workspaces"], "id", "workspace")
    targets = {pin["id"]: pin["target"] for pin in journal["pins"]}
    targets.update({move["id"]: move["target"] for move in state["plan"]["workspaces"]})
    errors, code = [], f"local j=rawget(_G,{profile_key(state)}); "
    for pin in journal["pins"]:
        expected = {"workspaceString": pin["selector"], "enabled": True, "persistent": True, "monitor": pin["target"]}
        actual = next((rule for rule in rules if rule["workspaceString"] == pin["selector"] and rule["enabled"]), None)
        if actual is None:
            continue
        if actual != expected:
            errors.append(f"Workspace {pin['id']} session rule changed externally; preserving it.")
            continue
        selector = lua_string(pin["selector"])
        code += (f"assert(j and j.pins[{selector}] and j.pins[{selector}]:is_enabled()~=nil, "
                 "'Profile rule handle expired; preserving reloaded rules'); "
                 f"j.pins[{selector}]:set_enabled(false); ")
    for rule in journal["persistent"]:
        actual = next((r for r in rules if r["workspaceString"] == rule["workspaceString"] and r["enabled"]), None)
        if actual == rule:
            continue
        if actual != dict(rule, persistent=False):
            errors.append(f"Workspace rule {rule['workspaceString']} changed externally; preserving it.")
            continue
        wid = exact_workspace_id(rule["workspaceString"])
        workspace = workspaces.get(wid)
        allowed = {targets.get(wid), original.get(wid, {}).get("monitor")}
        if workspace and workspace["monitor"] not in allowed:
            errors.append(f"Workspace {wid} moved externally; preserving its placement and rule.")
            continue
        selector = lua_string(rule["workspaceString"])
        code += (f"assert(j and j.rules[{selector}] and j.rules[{selector}]:is_enabled()==true, "
                 "'Original workspace rule handle expired; preserving reloaded rules'); "
                 f"hl.workspace_rule({{workspace={selector},persistent=true}}); ")
    # Recreate retired original empties only while the compositor still owns the
    # cleanup journal. A temporary pin keeps each alive until focus is restored.
    for pin in journal["restorePins"]:
        if pin["id"] in workspaces:
            continue
        monitor = live.get(pin["target"])
        before = next(m for m in state["baseline"]["monitors"] if m["name"] == pin["target"])
        if monitor is None or not same_identity(before, monitor):
            errors.append(f"Workspace {pin['id']}: original display is unavailable.")
            continue
        selector = lua_string(pin["selector"])
        existing = next((rule for rule in rules if rule["workspaceString"] == pin["selector"] and rule["enabled"]), None)
        expected = {"workspaceString": pin["selector"], "enabled": True, "persistent": True, "monitor": pin["target"]}
        if existing is not None and existing != expected:
            errors.append(f"Workspace {pin['id']} restoration rule changed externally; preserving it.")
            continue
        code += (f"if j and j.cleaned[{pin['id']}] then do {monitor_guard(monitor)}"
                 f"if not hl.get_workspace({pin['id']}) then "
                 f"if j.temporary[{selector}] then "
                 f"assert(j.temporary[{selector}]:is_enabled()~=nil, 'Restoration rule expired'); "
                 f"j.temporary[{selector}]:set_enabled(true) else "
                 f"j.temporary[{selector}]=hl.workspace_rule({{workspace={selector},"
                 f"monitor={lua_string(pin['target'])},persistent=true}}) end end end end; ")
    code += "hl.exec_scheduled_prop_refresh_immediately();"
    lua_eval(code)
    return errors


def finish_profile_rollback(state):
    rules = workspace_rules()
    code = f"local j=rawget(_G,{profile_key(state)}); "
    for pin in state["profileJournal"]["restorePins"]:
        expected = {"workspaceString": pin["selector"], "enabled": True, "persistent": True, "monitor": pin["target"]}
        if expected not in rules:
            continue
        selector = lua_string(pin["selector"])
        code += (f"if j and j.temporary[{selector}] and j.temporary[{selector}]:is_enabled()~=nil "
                 f"then j.temporary[{selector}]:set_enabled(false) end; ")
    code += "hl.exec_scheduled_prop_refresh_immediately();"
    lua_eval(code)


def rollback(state):
    baseline, errors = state["baseline"], []
    # Restore compatible outputs together while preserving unrelated external edits.
    try:
        current = snapshot()
        if baseline.get("session") != current.get("session"):
            return ["The compositor session changed; refusing to restore an old arrangement."]
        live = indexed(current["monitors"], "name", "display")
        targets = indexed(expected_outputs(baseline, state["plan"]["positions"])["monitors"], "name", "display")
        positions = []
        for monitor in baseline["monitors"]:
            now = live.get(monitor["name"])
            if now is None or not same_identity(monitor, now):
                errors.append(f"Display {monitor['name']} disconnected or was replaced.")
            elif not any(same_fields(expected, now, GEOMETRY + ("x", "y"))
                         for expected in (monitor, targets[monitor["name"]])):
                errors.append(f"Display {monitor['name']} changed externally; preserving its geometry.")
            else:
                positions.append({key: monitor[key] for key in POSITION})
        if positions and state["plan"]["displayChanges"]:
            set_positions(current, positions, force=True, recovery=True, owner=state.get("outputOwner"))
    except Exception as error:
        errors.append(str(error))
    if state.get("profileJournal"):
        try:
            errors.extend(rollback_profile(state))
        except Exception as error:
            errors.append(str(error))
    # Moving an active workspace may also move/create a replacement. Restore all
    # original surviving workspaces, not only the ones explicitly requested.
    for workspace in baseline["workspaces"]:
        if workspace["id"] <= 0 or workspace["monitor"] not in {m["name"] for m in baseline["monitors"]}:
            continue
        try:
            if state.get("profileJournal"):
                actual = indexed(snapshot()["workspaces"], "id", "workspace").get(workspace["id"])
                intended = {pin["id"]: pin["target"] for pin in state["profileJournal"]["pins"]}
                intended.update({move["id"]: move["target"] for move in state["plan"]["workspaces"]})
                if actual and actual["monitor"] not in (workspace["monitor"], intended.get(workspace["id"])):
                    raise ValueError(f"Workspace {workspace['id']} moved externally; preserving its placement.")
            move_workspace(baseline, workspace["id"], workspace["monitor"], recovery=True)
        except Exception as error:
            errors.append(str(error))
    try:
        restore_view(baseline, recovery=True)
    except Exception as error:
        errors.append(f"Focus restoration: {error}")
    if state.get("profileJournal"):
        try:
            finish_profile_rollback(state)
        except Exception as error:
            errors.append(str(error))
    positions = [{key: monitor[key] for key in POSITION} for monitor in baseline["monitors"]]
    moves = [{"id": w["id"], "target": w["monitor"]} for w in baseline["workspaces"] if w["id"] > 0]
    try:
        settle(baseline, positions, moves)
    except Exception as error:
        errors.append(str(error))
    return list(dict.fromkeys(errors))


def announce_automatic(state):
    if not state.get("automatic", False):
        return
    # The worker journal and panel remain authoritative if desktop notifications fail.
    try:
        print(f"Automatic profile {state.get('profileName', '')}: {state['message']}", flush=True)
        run(["notify-send", "--app-name=Display Workspaces",
             "--urgency=critical" if state["state"] == "failed" else "--urgency=normal",
             "Automatic display restoration", state["message"]], timeout=1)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def worker(root, token):
    with locked(root, "worker.lock"):
        with locked(root):
            state = load(root, token)
            if state["state"] in TERMINAL:
                return
            recover = state["phase"] != "prepared"
            state["phase"] = "applying" if not recover else "rolling-back"
            save(root, state)
        reason = "Trial interrupted; restoring the previous arrangement."
        failed = recover
        try:
            if not recover:
                # Hold the runtime lock throughout preflight and first mutation:
                # an expired launcher or profile edit cannot steal phase ownership.
                with locked(root):
                    state = load(root, token)
                    if state.get("request") == "revert":
                        state.update(state="reverted", phase="finished", message="Trial cancelled; nothing changed.")
                        save(root, state)
                        return
                    request = {"baseline": state["baseline"], "positions": state["plan"]["positions"],
                               "workspaces": state["plan"]["workspaces"],
                               "profileWorkspaces": state["plan"].get("profileWorkspaces")}
                    authorization = (display_automation.worker_request(sys.modules[__name__], state)
                                     if state.get("automatic", False) else contextlib.nullcontext(request))
                    with authorization as request:
                        fresh = validate(request, allow_new=not state.get("automatic", False))
                        if fresh["profileWorkspaces"] is not None:
                            if not set(fresh["removeWorkspaces"]) <= set(state["plan"]["removeWorkspaces"]):
                                raise ValueError("The empty workspace set changed; refresh the profile draft.")
                            if workspace_rules() != state["profileJournal"]["beforeRules"]:
                                raise ValueError("Workspace rules changed before preview.")
                        if state.get("automatic", False):
                            # Pair the saved selection with its fresh pre-mutation
                            # baseline, including workspace moves made during launch.
                            state["baseline"] = request["baseline"]
                            state["plan"] = fresh
                            state["profileJournal"] = prepare_profile(state, workspace_rules())
                            data = display_profiles.read_store(display_profiles.store_path())
                            if display_config.digest(data) != state["automaticProfile"]["revision"]:
                                raise ValueError("Saved profiles changed before automatic restoration.")
                        if state.get("monitorBackend") == "output-management":
                            state["outputOwner"] = output_owner.ensure(sys.modules[__name__], root)
                            state["outputOwnerHadLayout"] = output_owner.call(root, {"command": "status"})["holding"]
                        state["mutationStarted"] = True
                        save(root, state)
                        if state["plan"]["displayChanges"]:
                            # Pin unchanged outputs too: 'auto' rules may otherwise move them.
                            set_positions(state["baseline"], state["plan"]["positions"], force=True,
                                          owner=state.get("outputOwner"))
                        if state["plan"].get("profileWorkspaces") is not None:
                            apply_profile(state)
                if state["plan"]["displayChanges"] or state["plan"].get("profileWorkspaces") is not None:
                    # Installing monitor rules can reapply persistent workspace
                    # bindings. Reconcile the complete intended allocation after
                    # our own display mutation, not only explicit workspace moves.
                    targets = {w["id"]: w["monitor"] for w in state["baseline"]["workspaces"] if w["id"] > 0}
                    targets.update({move["id"]: move["target"] for move in state["plan"]["workspaces"]})
                    targets.update({placement["id"]: placement["target"]
                                    for placement in state["plan"].get("profileWorkspaces") or []})
                    intended = expected_outputs(state["baseline"], state["plan"]["positions"])
                    for wid, target in targets.items():
                        move_workspace(state["baseline"], wid, target, expected=intended)
                else:
                    for move in state["plan"]["workspaces"]:
                        move_workspace(state["baseline"], move["id"], move["target"], move["source"])
                restore_view(expected_outputs(state["baseline"], state["plan"]["positions"]))
                if state["plan"].get("profileWorkspaces") is not None:
                    cleanup_profile(state, root)
                settle_trial(state)
                with locked(root):
                    latest = load(root, token)
                    state.update(request=latest.get("request", ""), state="pending", phase="waiting",
                                 deadline=time.monotonic() + CONFIRM_SECONDS,
                                 message=(f"Restoring {state['profileName']} automatically. Verifying for 20 seconds, then keeping for this session. Open the arrangement panel to Revert."
                                          if state.get("automatic", False)
                                          else "Preview only. Click Apply within 20 seconds to keep this arrangement."))
                    save(root, state)
                announce_automatic(state)
                while True:
                    with locked(root):
                        state = load(root, token)
                        if state.get("request") == "revert":
                            reason = "Previous arrangement restored."
                            break
                        expired = time.monotonic() >= state["deadline"]
                        automatic = state.get("automatic", False)
                        if expired and not automatic:
                            reason = "Preview expired without Apply; previous arrangement restored."
                            break
                        if (automatic and expired) or (not automatic and state.get("request") == "keep"):
                            authorization = (display_automation.profile_guard(state) if automatic
                                             else contextlib.nullcontext(None))
                            with authorization as guard:
                                verify_trial(state)
                                if automatic:
                                    path, original = guard
                                    if display_profiles.read_store(path) != original:
                                        raise ValueError("Saved profiles changed before automatic confirmation.")
                                elif time.monotonic() >= state["deadline"]:
                                    reason = "Preview expired without Apply; previous arrangement restored."
                                    break
                                if state.get("outputOwner"):
                                    output_owner.finish(root, state["outputOwner"], keep=True)
                                state.update(state="kept", phase="finished",
                                             message=(f"Automatic profile {state['profileName']} kept after 20 seconds of verification; session only."
                                                      if automatic else "Arrangement applied for this session only."))
                                save(root, state)
                                return
                    # Detect unplug/external changes during the countdown, not just on Keep.
                    verify_trial(state)
                    time.sleep(0.2)
        except Exception as error:
            failed = True
            reason = f"Trial failed: {error}"
        with locked(root):
            state = load(root, token)
            state["phase"] = "rolling-back"
            save(root, state)
        errors = rollback(state) if state["mutationStarted"] else []
        if state.get("outputOwner"):
            try:
                output_owner.finish(root, state["outputOwner"], keep=False,
                                    retain=state.get("outputOwnerHadLayout", False))
            except Exception as error:
                errors.append(f"Session layout ownership: {error}")
        with locked(root):
            state.update(state="failed" if failed or errors else "reverted", phase="finished")
            if errors:
                state["message"] = reason + " Rollback incomplete: " + " ".join(errors)
            elif failed:
                state["message"] = reason + (" Previous arrangement restored." if state["mutationStarted"] else " Nothing changed.")
            else:
                state["message"] = reason
            save(root, state)


def require_no_preview(root):
    active = root / "active.json"
    if active.exists():
        previous = load(root, json.loads(active.read_text())["token"])
        expire_unstarted(root, previous)
        if previous["state"] not in TERMINAL:
            raise ValueError("An arrangement preview is active. Keep it or wait for rollback before another operation.")


def require_no_forget(root):
    active = root / "forget-active.json"
    if not active.exists():
        return
    state = load(root, json.loads(active.read_text())["token"])
    if "result" in state:
        return
    if state["phase"] == "prepared" and time.monotonic() - state["created"] > 15:
        state["result"] = {"ok": False, "message": "Forget worker did not start; nothing changed."}
        save(root, state)
        return
    raise ValueError("A display configuration change is active. Wait for it to finish.")


def forget(request):
    root = runtime_dir()
    with locked(root):
        display_automation.suppress(sys.modules[__name__], root)
        require_no_preview(root)
        require_no_forget(root)
        # Preflight supplies immediate stale-selection feedback; worker repeats it.
        display_config.prepare(sys.modules[__name__], request)
        if not shutil.which("systemd-run") or not shutil.which("hyprctl"):
            raise ValueError("systemd-run and hyprctl are required for safe configuration changes.")
        token = secrets.token_hex(24)
        state = {"token": token, "phase": "prepared", "created": time.monotonic(), "request": request}
        save(root, state)
        atomic(root, "forget-active.json", {"token": token})
        args = ["systemd-run", "--user", "--quiet", "--collect", "--service-type=exec",
                f"--unit=display-workspaces-forget-{token}", "--property=Restart=on-failure",
                "--property=RestartSec=1", "--property=StartLimitIntervalSec=0",
                "--property=TimeoutStopSec=25", "--property=UMask=0077"]
        for name in ("HOME", "XDG_CONFIG_HOME", "HYPRLAND_INSTANCE_SIGNATURE", "XDG_RUNTIME_DIR",
                     "WAYLAND_DISPLAY", "PATH", "DBUS_SESSION_BUS_ADDRESS"):
            if name in os.environ:
                args.append(f"--setenv={name}={os.environ[name]}")
        args.extend([sys.executable, str(Path(__file__).resolve()), "forget-worker", token])
        try:
            run(args, timeout=10)
        except Exception as error:
            state["result"] = {"ok": False, "message": f"Forget worker could not start; nothing changed. {error}"}
            save(root, state)
            return state["result"]
    # Do not own the worker or its lock: destroying this caller cannot stop edits.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        current = load(root, token)
        if "result" in current:
            return current["result"]
        time.sleep(0.1)
    return {"ok": False, "message": "The independent Forget worker is still running. Its notification will report the result; do not retry yet."}


def forget_worker(root, token):
    with locked(root):
        state = load(root, token)
        if "result" in state:
            return
        try:
            require_no_preview(root)
            if state["phase"] == "prepared":
                _, originals, replacements = display_config.prepare(sys.modules[__name__], state["request"])
                if originals[0] != replacements[0]:
                    output_owner.release(sys.modules[__name__], root)
            result = display_config.transact(sys.modules[__name__], root, state)
        except Exception as error:
            result = {"ok": False, "message": str(error)}
        state.update(phase="finished", result=result)
        save(root, state)
    # Always notify: shell.json watchers may already have destroyed the caller.
    message = result["message"]
    if result.get("backupPaths"):
        message += "\nBackups:\n" + "\n".join(result["backupPaths"])
    with contextlib.suppress(Exception):
        run(["notify-send", "--app-name=Display configuration",
             "--urgency=normal" if result["ok"] else "--urgency=critical",
             "Display forgotten" if result["ok"] else "Could not forget display", message])


def interrupted(signum, frame):
    raise RuntimeError("The watchdog was interrupted.")


def main():
    if len(sys.argv) < 2:
        raise ValueError("Expected snapshot, resume, validate, begin, status, keep, or revert.")
    command = sys.argv[1]
    if command == "output-owner" and len(sys.argv) == 2:
        output_owner.serve(sys.modules[__name__], runtime_dir())
        return {"ok": True, "message": "Session layout owner stopped."}
    if command == "release-layout" and len(sys.argv) == 2:
        root = runtime_dir()
        with locked(root):
            require_no_preview(root)
            require_no_forget(root)
            display_automation.suppress(sys.modules[__name__], root)
            output_owner.release(sys.modules[__name__], root)
        return {"ok": True, "message": "Session layout ownership released; configuration can reapply on reload."}
    if command == "snapshot" and len(sys.argv) == 2:
        return snapshot()
    if command == "configuration" and len(sys.argv) == 2:
        return display_config.configuration(sys.modules[__name__])
    if command == "profiles" and len(sys.argv) == 2:
        return display_profiles.catalog(sys.modules[__name__])
    if command in ("profile-save", "profile-delete", "profile-load", "profile-auto") and len(sys.argv) == 3:
        if len(sys.argv[2]) > 1048576:
            raise ValueError("Profile request is too large.")
        request = json.loads(sys.argv[2], object_pairs_hook=display_config.strict_object)
        operation = {"profile-save": display_profiles.save, "profile-delete": display_profiles.delete,
                     "profile-load": display_profiles.load, "profile-auto": display_profiles.set_automatic}[command]
        root = runtime_dir()
        with locked(root):
            display_automation.suppress(sys.modules[__name__], root)
            require_no_preview(root)
            require_no_forget(root)
            return operation(sys.modules[__name__], request)
    if command == "forget" and len(sys.argv) == 3:
        if len(sys.argv[2]) > 1048576:
            raise ValueError("Forget request is too large.")
        return forget(json.loads(sys.argv[2]))
    if command == "forget-worker" and len(sys.argv) == 3:
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        forget_worker(runtime_dir(), sys.argv[2])
        return {"ok": True, "message": "Forget worker finished."}
    if command == "resume" and len(sys.argv) == 2:
        return resume()
    if command == "resume-refresh" and len(sys.argv) == 2:
        return resume_refresh()
    if command == "auto-check" and len(sys.argv) == 3:
        if len(sys.argv[2]) > 4096:
            raise ValueError("Automatic check request is too large.")
        return display_automation.check(sys.modules[__name__],
                                        json.loads(sys.argv[2], object_pairs_hook=display_config.strict_object))
    if command in ("validate", "begin") and len(sys.argv) == 3:
        if len(sys.argv[2]) > 1048576:
            raise ValueError("Arrangement request is too large.")
        request = json.loads(sys.argv[2])
        return validate(request) if command == "validate" else begin(request)
    if command in ("status", "keep", "revert") and len(sys.argv) == 3:
        return control(command, sys.argv[2])
    if command == "worker" and len(sys.argv) == 3:
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        root = runtime_dir()
        worker(root, sys.argv[2])
        finished = load(root, sys.argv[2])
        announce_automatic(finished)
        return {"ok": True, "message": "Watchdog finished.", "result": response(finished)}
    raise ValueError("Invalid arrangement command or arguments.")


if __name__ == "__main__":
    try:
        result = main()
    except Exception as error:
        if len(sys.argv) > 1 and sys.argv[1] in ("worker", "forget-worker"):
            # Nonzero exit restarts the independent service; persisted phase forces
            # rollback instead of re-applying after an unexpected process failure.
            print(json.dumps({"ok": False, "message": str(error)}))
            sys.exit(1)
        result = {"ok": False, "message": str(error)}
    print(json.dumps(result, allow_nan=False))
    sys.exit(0 if result["ok"] else 1)
