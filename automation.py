"""One guarded restoration attempt per settled connection episode, without a daemon."""
import contextlib
import hashlib
import json
import math
import os
import time

import configuration as config
import profiles

SETTLE_SECONDS = 2


def topology(current):
    """Connection identity/availability only: our own geometry never rearms an episode."""
    outputs = []
    for kind, key in (("enabled", "monitors"), ("disabled", "disabledMonitors"),
                      ("unavailable", "unavailableMonitors")):
        for monitor in current.get(key, []):
            outputs.append([kind, *[monitor.get(field) for field in
                                   ("name", "make", "model", "serial")]])
    outputs.extend([["mirrored", name] for name in current.get("mirroredOutputs", [])])
    encoded = sorted(json.dumps(output, sort_keys=True, allow_nan=False) for output in outputs)
    return hashlib.sha256(json.dumps(encoded).encode()).hexdigest()


def read_episode(root):
    try:
        fd = os.open(root / "automation.json", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        return json.load(stream)


def consume(api, root, episode, message, status="skipped"):
    episode.update(consumed=True, status=status, message=message)
    api.atomic(root, "automation.json", episode)


def suppress(api, root, message="Automatic restoration skipped for this connection: manual operation."):
    """Caller owns the runtime lock. Never arm startup after a manual operation."""
    episode = read_episode(root) or {"signature": None, "lastEvent": None}
    episode.pop("token", None)
    consume(api, root, episode, message)


def require_current(api, current):
    monitors = profiles.require_supported(api, current)
    key = profiles.automation_key({"monitors": monitors})
    # A rollback baseline must itself be usable, not an overlap/gap or invalid scale.
    api.validate({"baseline": current,
                  "positions": [{field: monitor[field] for field in api.POSITION} for monitor in monitors],
                  "workspaces": []}, current)
    return key


def candidate(api, document, current):
    key = require_current(api, current)
    matches = []
    for profile in document["profiles"]:
        if not profile.get("automatic", False):
            continue
        try:
            saved_key = profiles.automation_key(profile)
        except ValueError:
            continue
        if saved_key == key:
            matches.append(profile)
    if len(matches) != 1:
        raise ValueError("No automatic profile matches this complete display combination." if not matches
                         else "Multiple automatic profiles match this display combination; refusing to choose.")
    profile = matches[0]
    if {workspace["monitor"] for workspace in profile["workspaces"]} != set(range(len(profile["monitors"]))):
        raise ValueError("Automatic restoration requires a saved positive workspace on every display.")
    return profile, key


def request_for(api, profile, current):
    plan, match, _ = profiles.draft_for(api, profile, current)
    if match != "hardware":
        raise ValueError("Automatic restoration requires unique complete hardware identities.")
    return {"baseline": current, "positions": plan["positions"], "workspaces": plan["workspaces"],
            "profileWorkspaces": plan["profileWorkspaces"]}


@contextlib.contextmanager
def profile_guard(state):
    """Caller owns runtime lock; hold immutable opt-in through mutation or commit."""
    with profiles.locked_store() as path:
        data = profiles.read_store(path)
        metadata = state["automaticProfile"]
        profiles.check_request({"id": metadata["id"], "revision": metadata["revision"]},
                               data, ("id", "revision"))
        profile = profiles.selected(profiles.document_from(data), metadata)
        if not profile.get("automatic", False) or profiles.automation_key(profile) != metadata["key"]:
            raise ValueError("The selected automatic profile is no longer enabled for these displays.")
        yield path, data


@contextlib.contextmanager
def worker_request(api, state):
    with profile_guard(state) as (path, data):
        metadata = state["automaticProfile"]
        current = api.snapshot()
        api.compare_outputs(state["baseline"], current, catalog=True)
        profile, key = candidate(api, profiles.document_from(data), current)
        if profile["id"] != metadata["id"] or key != metadata["key"]:
            raise ValueError("The selected automatic profile is no longer enabled for these displays.")
        request = request_for(api, profile, current)
        if profiles.read_store(path) != data:
            raise ValueError("Saved profiles changed before automatic restoration.")
        yield request


def active_result(api, root, state):
    api.expire_unstarted(root, state)
    result = {**api.response(state), "status": state["state"]}
    if state["state"] not in api.TERMINAL:
        result["retryAfterMs"] = 250
    return result


def check(api, request):
    if (not isinstance(request, dict) or set(request) != {"event", "blocked"}
            or type(request["event"]) is not bool or type(request["blocked"]) is not bool):
        raise ValueError("Automatic check requires event and blocked booleans.")
    root = api.runtime_dir()
    with api.locked(root):
        now = time.monotonic()
        episode = read_episode(root)
        # Poll the worker even if discovery is broken or the panel is open.
        active = root / "active.json"
        if active.exists():
            state = api.load(root, json.loads(active.read_text())["token"])
            api.expire_unstarted(root, state)
            if state["state"] not in api.TERMINAL:
                episode = episode or {"signature": None, "lastEvent": None}
                if request["event"]:
                    episode["lastEvent"] = now
                consume(api, root, episode, "Automatic restoration skipped: an arrangement trial is active.")
                if state.get("automatic", False):
                    episode["token"] = state["token"]
                    api.atomic(root, "automation.json", episode)
                    return active_result(api, root, state)
                return {"ok": True, "status": "skipped", "message": episode["message"]}
        try:
            current = api.snapshot()
            signature = topology(current)
        except Exception as error:
            episode = episode or {"signature": None, "lastEvent": None}
            consume(api, root, episode, f"Automatic restoration skipped: {error}")
            return {"ok": True, "status": "skipped", "message": episode["message"]}
        if episode is None:
            episode = {"signature": signature, "lastEvent": None, "consumed": False,
                       "settleUntil": now + SETTLE_SECONDS}
        changed = episode["signature"] != signature
        last_event = episode.get("lastEvent")
        new_event = request["event"] and (changed or last_event is None or now - last_event >= SETTLE_SECONDS)
        if new_event:
            episode = {"signature": signature, "lastEvent": now, "consumed": False,
                       "settleUntil": now + SETTLE_SECONDS}
        elif not episode["consumed"] and (changed or request["event"]):
            episode["settleUntil"] = now + SETTLE_SECONDS
        episode["signature"] = signature
        if request["event"]:
            episode["lastEvent"] = now
        if request["blocked"]:
            consume(api, root, episode, "Automatic restoration skipped: an arrangement panel is open.")
        try:
            api.require_no_forget(root)
        except ValueError as error:
            consume(api, root, episode, f"Automatic restoration skipped: {error}")
        if episode["consumed"]:
            api.atomic(root, "automation.json", episode)
            if episode.get("token"):
                return active_result(api, root, api.load(root, episode["token"]))
            return {"ok": True, "status": episode.get("status", "idle"),
                    "message": episode.get("message", "Automatic restoration is idle.")}
        remaining = episode["settleUntil"] - now
        if remaining > 0:
            api.atomic(root, "automation.json", episode)
            return {"ok": True, "status": "settling", "message": "Waiting for display connections to settle.",
                    "retryAfterMs": max(1, math.ceil(remaining * 1000))}
        # Consume before preflight/launcher: errors and killed launchers never loop.
        consume(api, root, episode, "Automatic restoration was attempted for this connection.")
        try:
            with profiles.locked_store() as path:
                data = profiles.read_store(path)
                profile, key = candidate(api, profiles.document_from(data), current)
                draft = request_for(api, profile, current)
                metadata = {"id": profile["id"], "revision": config.digest(data),
                            "key": key, "name": profile["name"]}
                if profiles.read_store(path) != data:
                    raise ValueError("Saved profiles changed before automatic restoration.")
                result = api.begin_locked(root, draft, automatic_profile=metadata)
            episode["token"] = result["token"]
            api.atomic(root, "automation.json", episode)
            return active_result(api, root, api.load(root, result["token"]))
        except Exception as error:
            consume(api, root, episode, f"Automatic restoration skipped: {error}")
            return {"ok": True, "status": "skipped", "message": episode["message"]}
