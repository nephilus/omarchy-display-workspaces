"""Explicit live-layout profiles; loading only prepares a validated draft."""
import contextlib
import fcntl
import math
import os
from pathlib import Path
import re
import secrets
import stat
import json

import configuration as config

VERSION = 1
MAX_BYTES = 2 * 1024 * 1024
MAX_PROFILES = 128
MAX_MONITORS = 32
MAX_WORKSPACES = 4096
ID = re.compile(r"[0-9a-f]{32}\Z")
IDENTITY = ("make", "model", "serial")
GEOMETRY = ("x", "y", "width", "height", "refreshRate", "scale", "transform")


def store_path(create=False):
    base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
    if not base.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be an absolute path.")
    # Reject symlink ancestors as well as a symlink at the store itself.
    for parent in reversed((base, *base.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if not create:
                return None
            parent.mkdir(mode=0o700)
            info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Unsafe profile directory: {parent}")
    root = base / "display-workspaces"
    for directory in (base, root):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(f"Unsafe profile directory: {directory}")
    return root / "profiles.json"


def safe_file(info, path):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise ValueError(f"Unsafe profile file: {path}")


def read_store(path):
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        safe_file(info, path)
        if info.st_size > MAX_BYTES:
            raise ValueError("Profile store is too large; nothing was changed.")
        data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Profile store is too large; nothing was changed.")
        return data


@contextlib.contextmanager
def locked_store(create=False):
    path = store_path(create)
    if path is None:
        yield None
        return
    lock_path = path.parent / ".profiles.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        safe_file(os.fstat(fd), lock_path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.fstat(fd).st_ino != lock_path.lstat().st_ino:
            raise ValueError("Profile lock changed; refresh and try again.")
        yield path
    finally:
        os.close(fd)


def fields(value, expected, label):
    if not isinstance(value, dict) or value.keys() != set(expected):
        raise ValueError(f"Invalid {label} fields in profile store.")


def text(value, label, maximum=512, empty=True):
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value)
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError(f"Invalid {label}.")
    return value


def number(value, label, minimum, maximum, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not minimum <= value <= maximum or not math.isfinite(value)
            or integer and not isinstance(value, int)):
        raise ValueError(f"Invalid {label} in profile store.")


def validate_document(document):
    fields(document, ("version", "profiles"), "document")
    if type(document["version"]) is not int or document["version"] != VERSION:
        raise ValueError("Unsupported profile store version; nothing was changed.")
    records = document["profiles"]
    if not isinstance(records, list) or len(records) > MAX_PROFILES:
        raise ValueError("Invalid profile list or too many profiles.")
    ids, names = set(), set()
    for profile in records:
        fields(profile, ("id", "name", "monitors", "workspaces"), "profile")
        if not isinstance(profile["id"], str) or not ID.fullmatch(profile["id"]) or profile["id"] in ids:
            raise ValueError("Invalid or duplicate profile ID.")
        name = text(profile["name"], "profile name", 80, False)
        if name != name.strip() or name in names:
            raise ValueError("Profile names must be unique and trimmed.")
        ids.add(profile["id"])
        names.add(name)
        monitors = profile["monitors"]
        if not isinstance(monitors, list) or not 1 <= len(monitors) <= MAX_MONITORS:
            raise ValueError("Invalid profile monitor list.")
        connectors = set()
        for monitor in monitors:
            fields(monitor, (*IDENTITY, "connector", *GEOMETRY), "monitor")
            for key in IDENTITY:
                text(monitor[key], "monitor " + key)
            connector = text(monitor["connector"], "monitor connector", 512, False)
            if connector in connectors:
                raise ValueError("Duplicate profile monitor connector.")
            connectors.add(connector)
            for key in ("x", "y"):
                number(monitor[key], key, -32768, 32768, True)
            for key in ("width", "height"):
                number(monitor[key], key, 1, 65536, True)
            number(monitor["refreshRate"], "refresh rate", 0.001, 1000)
            number(monitor["scale"], "scale", 0.1, 16)
            number(monitor["transform"], "transform", 0, 7, True)
        workspaces = profile["workspaces"]
        if not isinstance(workspaces, list) or len(workspaces) > MAX_WORKSPACES:
            raise ValueError("Invalid profile workspace list.")
        workspace_ids = set()
        for workspace in workspaces:
            fields(workspace, ("id", "monitor"), "workspace")
            number(workspace["id"], "workspace ID", 1, 2147483647, True)
            number(workspace["monitor"], "workspace monitor index", 0, len(monitors) - 1, True)
            if workspace["id"] in workspace_ids:
                raise ValueError("Duplicate profile workspace ID.")
            workspace_ids.add(workspace["id"])
    return document


def document_from(data):
    if data is None:
        return {"version": VERSION, "profiles": []}
    try:
        document = json.loads(data, object_pairs_hook=config.strict_object)
        return validate_document(document)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise ValueError("Malformed profile store; repair it manually before continuing.") from error


def require_supported(api, current):
    api.require_usable_outputs(current)
    if current.get("disabledMonitors"):
        raise ValueError("Profiles do not support disabled outputs. Enable or disconnect them first.")
    if current.get("mirroredOutputs"):
        raise ValueError("Profiles do not support mirrored outputs.")
    monitors = api.indexed(current.get("monitors"), "name", "display")
    if not monitors:
        raise ValueError("No usable enabled displays are connected.")
    if len(monitors) > MAX_MONITORS:
        raise ValueError("Too many connected displays for a profile.")
    return list(monitors.values())


def identity(monitor):
    return tuple(text("" if monitor.get(key) is None else monitor[key], "monitor " + key)
                 for key in IDENTITY)


def match_monitors(api, profile, current):
    live = require_supported(api, current)
    saved = profile["monitors"]
    if len(saved) != len(live):
        raise ValueError(f"Profile needs {len(saved)} enabled display(s); {len(live)} are connected. Missing or extra displays block loading.")
    old_keys, new_keys = [identity(m) for m in saved], [identity(m) for m in live]
    mapping, used = {}, set()
    weak = False
    # Unique complete serial identities are independent of connector placement.
    for index, key in enumerate(old_keys):
        if key[2] and old_keys.count(key) == new_keys.count(key) == 1:
            target = new_keys.index(key)
            mapping[index] = live[target]
            used.add(target)
    # A unique make/model without serial is useful, but not strong hardware proof.
    for index, key in enumerate(old_keys):
        if index in mapping or key[2] or not all(key[:2]):
            continue
        if (sum(candidate[:2] == key[:2] for candidate in old_keys) == 1
                and sum(candidate[:2] == key[:2] for candidate in new_keys) == 1
                and new_keys.count(key) == 1):
            target = new_keys.index(key)
            if target not in used:
                mapping[index] = live[target]
                used.add(target)
                weak = True
    # Duplicated/partial identities require the remembered port, never list order.
    for index, monitor in enumerate(saved):
        if index in mapping:
            continue
        candidates = [target for target, connected in enumerate(live)
                      if target not in used and connected["name"] == monitor["connector"]
                      and not any(a and b and a != b for a, b in zip(old_keys[index], new_keys[target]))]
        if len(candidates) != 1:
            raise ValueError(f"Display {monitor['connector']}: hardware is missing, changed, or ambiguous; connector fallback cannot resolve it safely.")
        target = candidates[0]
        mapping[index] = live[target]
        used.add(target)
        weak = True
    return mapping, ("port-dependent" if weak else "hardware")


def draft_for(api, profile, current):
    mapping, match = match_monitors(api, profile, current)
    positions = [{"name": mapping[index]["name"], **{key: monitor[key] for key in GEOMETRY}}
                 for index, monitor in enumerate(profile["monitors"])]
    existing = api.indexed(current.get("workspaces"), "id", "live workspace")
    moves, missing = [], []
    for workspace in profile["workspaces"]:
        wid = workspace["id"]
        if wid not in existing:
            missing.append(wid)
            continue
        source, target = existing[wid]["monitor"], mapping[workspace["monitor"]]["name"]
        if source != target:
            moves.append({"id": wid, "source": source, "target": target})
    result = api.validate({"baseline": current, "positions": positions, "workspaces": moves}, current)
    for position in result["positions"]:
        position["logicalWidth"], position["logicalHeight"] = api.logical_size(position, position["transform"])
    reason = ("Unique hardware identities match; connector changes are supported." if match == "hardware" else
              "Port-dependent / weak identity match: serial identity is missing or duplicated. Verify the physical displays before Preview.")
    return result, match, reason, missing


def live_or_error(api):
    try:
        return api.snapshot(), None
    except Exception as error:
        return None, f"Live displays are unavailable: {error}"


def listing(api, document, data, current=None, error=None):
    if current is None and error is None:
        current, error = live_or_error(api)
    entries = []
    for profile in document["profiles"]:
        match, reason, can_load = "unavailable", error, False
        if current is not None:
            try:
                _, match, reason, missing = draft_for(api, profile, current)
                can_load = True
                if missing:
                    reason += " Missing saved workspaces will be skipped: " + ", ".join(map(str, missing)) + "."
            except (ValueError, KeyError, TypeError) as failure:
                match, reason = "unavailable", str(failure)
        entries.append({"id": profile["id"], "name": profile["name"],
                        "displayCount": len(profile["monitors"]), "workspaceCount": len(profile["workspaces"]),
                        "canLoad": can_load, "match": match, "reason": reason})
    return {"ok": True, "revision": config.digest(data), "profiles": entries,
            "message": error or "Saved profiles loaded. Choose a profile manually; nothing is applied automatically."}


def catalog(api):
    data = read_store(store_path())
    return listing(api, document_from(data), data)


def check_request(request, data, required, optional=()):
    if (not isinstance(request, dict) or not set(required) <= request.keys()
            or request.keys() - set(required) - set(optional)):
        raise ValueError("Invalid profile request.")
    if not isinstance(request["revision"], str) or request["revision"] != config.digest(data):
        raise ValueError("Saved profiles changed. Refresh the profile list and confirm again.")


def selected(document, request):
    profile = next((p for p in document["profiles"] if p["id"] == request["id"]), None)
    if profile is None:
        raise ValueError("The selected profile no longer exists. Refresh the profile list.")
    return profile


def write_document(path, original, document):
    validate_document(document)
    data = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Profile store would be too large; nothing was changed.")
    if read_store(path) != original:
        raise ValueError("Concurrent profile edit detected; nothing was changed.")
    backups = []
    if original is not None:
        backup = path.with_name(path.name + ".profile-" + secrets.token_hex(12) + ".bak")
        config.write_bytes(backup, original)
        config.sync_directory(path.parent)
        backups.append(str(backup))
    temporary = path.parent / (".profiles-" + secrets.token_hex(12))
    try:
        config.write_bytes(temporary, data)
        if read_store(path) != original:
            raise ValueError("Concurrent profile edit detected; refusing to overwrite it.")
        if original is None:
            # Atomic no-clobber creation: an external editor may create the file.
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        config.sync_directory(path.parent)
    except Exception as error:
        suffix = " Backups: " + ", ".join(backups) if backups else ""
        raise ValueError(str(error) + suffix) from error
    finally:
        temporary.unlink(missing_ok=True)
    return data, backups


def placements(api, current):
    workspaces = api.indexed(current.get("workspaces"), "id", "workspace")
    return {wid: workspace.get("monitor") for wid, workspace in workspaces.items()}


def save(api, request):
    with locked_store(create=True) as path:
        original = read_store(path)
        document = document_from(original)
        check_request(request, original, ("name", "baseline", "revision"), ("id",))
        if not isinstance(request["name"], str):
            raise ValueError("Invalid profile name.")
        name = text(request["name"].strip(), "profile name", 80, False)
        replacing = selected(document, request) if "id" in request else None
        if any(p["name"] == name and p is not replacing for p in document["profiles"]):
            raise ValueError("That profile name already exists. Select it and explicitly replace it instead.")
        baseline = request["baseline"]
        if not isinstance(baseline, dict):
            raise ValueError("Missing live baseline. Refresh the panel.")
        current = api.snapshot()
        monitors = require_supported(api, current)
        api.compare_outputs(baseline, current, catalog=True)
        if placements(api, baseline) != placements(api, current):
            raise ValueError("Workspace placements changed. Refresh the panel before saving live state.")
        # Validation is deliberately offline: saving live state needs no exact Lua rule.
        api.validate({"baseline": current,
                      "positions": [{"name": m["name"], **{key: m[key] for key in GEOMETRY}} for m in monitors],
                      "workspaces": []}, current)
        indices = {monitor["name"]: index for index, monitor in enumerate(monitors)}
        workspaces = []
        for wid, connector in placements(api, current).items():
            if not isinstance(wid, int) or isinstance(wid, bool):
                raise ValueError("Invalid live workspace ID.")
            if wid <= 0:
                continue
            if connector not in indices:
                raise ValueError(f"Workspace {wid} belongs to an unavailable display.")
            workspaces.append({"id": wid, "monitor": indices[connector]})
        profile = {"id": replacing["id"] if replacing else secrets.token_hex(16), "name": name,
                   "monitors": [{**dict(zip(IDENTITY, identity(m))), "connector": m["name"],
                                 **{key: m[key] for key in GEOMETRY}} for m in monitors],
                   "workspaces": sorted(workspaces, key=lambda workspace: workspace["id"])}
        if replacing:
            document["profiles"][document["profiles"].index(replacing)] = profile
        else:
            document["profiles"].append(profile)
        data, backups = write_document(path, original, document)
        result = listing(api, document, data, current)
        result.update(savedId=profile["id"], backupPaths=backups,
                      message=f"Saved current live state as {name}. Draft edits were not saved; no live changes were made.")
        return result


def delete(api, request):
    with locked_store() as path:
        original = read_store(path)
        document = document_from(original)
        check_request(request, original, ("id", "revision"))
        profile = selected(document, request)
        document["profiles"].remove(profile)
        data, backups = write_document(path, original, document)
        result = listing(api, document, data)
        result.update(backupPaths=backups, message=f"Deleted profile {profile['name']}. Live displays were not changed.")
        return result


def load(api, request):
    with locked_store() as path:
        original = read_store(path)
        document = document_from(original)
        check_request(request, original, ("id", "revision"))
        profile = selected(document, request)
        current = api.snapshot()
        result, _, reason, missing = draft_for(api, profile, current)
        if read_store(path) != original:
            raise ValueError("Saved profiles changed while loading. Refresh and try again.")
        message = f"Loaded {profile['name']} into the draft only. {reason} Preview and Apply to change this session."
        if missing:
            message += " Skipped missing workspace IDs: " + ", ".join(map(str, missing)) + ". No workspaces were created."
        return {"ok": True, "profileId": profile["id"], "baseline": current,
                "positions": result["positions"], "workspaces": result["workspaces"], "message": message}
