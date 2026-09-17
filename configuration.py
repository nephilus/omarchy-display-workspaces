"""Non-executing monitor catalog and guarded, recoverable configuration edits."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

PLUGIN = "display.workspaces"
EXACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def tokens(source):
    """Lua lexical boundaries, retaining source offsets; never evaluate user code."""
    result, pos = [], 0
    while pos < len(source):
        start = pos
        if source[pos].isspace():
            pos += 1
            continue
        comment = source.startswith("--", pos)
        if comment:
            pos += 2
        long = re.match(r"\[(=*)\[", source[pos:])
        if long:
            endmark = "]" + long[1] + "]"
            begin = pos + len(long[0])
            end = source.find(endmark, begin)
            if end < 0:
                raise ValueError("Unterminated Lua long string/comment; monitor configuration is not editable.")
            pos = end + len(endmark)
            if not comment:
                result.append(("string", source[begin:end].removeprefix("\n"), start, pos))
            continue
        if comment:
            end = source.find("\n", pos)
            pos = len(source) if end < 0 else end
            continue
        if source[pos] in "\"'":
            quote, value = source[pos], bytearray()
            pos += 1
            while pos < len(source) and source[pos] != quote:
                if source[pos] != "\\":
                    value.extend(source[pos].encode("utf-8"))
                    pos += 1
                    continue
                pos += 1
                escape = re.match(r"[0-9]{1,3}", source[pos:])
                if escape and int(escape[0]) <= 255:
                    value.append(int(escape[0]))
                    pos += len(escape[0])
                elif source[pos:pos + 1] in ("\\", "\"", "'"):
                    value.extend(source[pos].encode())
                    pos += 1
                elif source[pos:pos + 1] in "abfnrtv" and pos < len(source):
                    value.append(dict(a=7, b=8, f=12, n=10, r=13, t=9, v=11)[source[pos]])
                    pos += 1
                else:
                    # Unknown escapes remain non-exact, but preserve lexical boundaries.
                    value.extend(b"\x00")
                    pos += 1
            if pos >= len(source):
                raise ValueError("Unterminated Lua string; monitor configuration is not editable.")
            pos += 1
            result.append(("string", value.decode("utf-8", errors="replace"), start, pos))
            continue
        word = re.match(r"[A-Za-z_][A-Za-z_0-9]*|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", source[pos:])
        if word:
            pos += len(word[0])
            result.append(("word", word[0], start, pos))
        else:
            pos += 1
            result.append(("symbol", source[start:pos], start, pos))
    return result


def monitor_rules(source):
    items = tokens(source)
    values = [t[1] if t[0] != "string" else None for t in items]
    complex_source = any(v in {"if", "for", "while", "repeat", "function", "dofile", "require", "goto"}
                         for v in values)
    rules = []
    for index in range(len(items) - 2):
        dotted = values[index:index + 3] == ["hl", ".", "monitor"]
        bracketed = (values[index:index + 2] == ["hl", "["] and
                     items[index + 2][0:2] == ("string", "monitor") and values[index + 3:index + 4] == ["]"])
        if not dotted and not bracketed:
            continue
        start = items[index][2]
        call = index + (3 if dotted else 4)
        end = items[call - 1][3]
        selector, fields, safe = None, {}, False
        j = call
        if values[j:j + 2] == ["(", "{"]:
            j += 2
            safe = True
            while j < len(items) and values[j] != "}":
                if j + 2 >= len(items) or items[j][0] != "word" or values[j + 1] != "=":
                    safe = False
                    break
                field = values[j]
                j += 2
                negative = values[j] == "-"
                if negative:
                    j += 1
                if j >= len(items):
                    safe = False
                    break
                item = items[j]
                literal = item[0] == "string" or item[1] in {"true", "false", "nil"} or bool(re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", item[1]))
                if not literal or (negative and not item[1][:1].isdigit()) or field in fields:
                    if field == "output":
                        selector = None
                    safe = False
                    break
                fields[field] = item[1]
                if field == "output" and item[0] == "string":
                    selector = item[1]
                j += 1
                if values[j:j + 1] in ([","], [";"]):
                    j += 1
                elif values[j:j + 1] != ["}"]:
                    if field == "output":
                        selector = None
                    safe = False
                    break
            safe = safe and values[j:j + 2] == ["}", ")"]
            if safe:
                end = items[j + 1][3]
        # Even unsupported tables can expose a literal selector without interpreting it.
        if not safe and values[call:call + 1] == ["("]:
            depth, finish = 0, call
            for finish in range(call, len(items)):
                if values[finish] == "(":
                    depth += 1
                elif values[finish] == ")":
                    depth -= 1
                    if depth == 0:
                        break
            end = items[finish][3]
            # Only use a selector already proven while walking the table prefix.
        previous = items[index - 1][3] if index else 0
        after = next((n for n in range(call, len(items)) if items[n][2] >= end), len(items))
        if values[after:after + 1] == [";"]:
            end = items[after][3]
            after += 1
        following = items[after][2] if after < len(items) else len(source)
        isolated = (index == 0 or "\n" in source[previous:start]) and (following == len(source) or "\n" in source[end:following])
        reason = ""
        if not safe or not isolated or complex_source or bracketed:
            reason = "Dynamic, conditional, or non-standalone Lua monitor declaration; edit monitors.lua manually."
        elif not isinstance(selector, str) or not EXACT.fullmatch(selector):
            reason = "Only literal exact connector selectors can be removed safely; edit monitors.lua manually."
        rules.append({"selector": selector, "start": start, "end": end, "reason": reason})
    # An alias or reassignment of hl/monitor can change what apparently plain calls mean.
    for i, value in enumerate(values):
        if value == "hl" and values[i + 1:i + 2] == ["="]:
            for rule in rules:
                rule["reason"] = "The hl API is reassigned; edit monitors.lua manually."
    return rules


def config_paths():
    home = Path.home()
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    if not base.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be an absolute path.")
    paths = [base / "hypr/monitors.lua", base / "omarchy/shell.json"]
    for path in paths:
        for parent in (path.parent, base):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError(f"Unsafe configuration directory: {parent}")
    return paths


def read_file(path):
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise ValueError(f"Unsafe configuration directory: {path.parent}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ValueError(f"Unsafe configuration file: {path}")
        return stream.read()


def digest(data):
    return hashlib.sha256(b"missing" if data is None else b"present\0" + data).hexdigest()


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key}; edit shell.json manually.")
        result[key] = value
    return result


def saved_records(document):
    if not isinstance(document, dict):
        raise ValueError("shell.json must contain an object.")
    layout = document.get("bar", {}).get("layout", {})
    if not isinstance(layout, dict):
        raise ValueError("Invalid bar layout in shell.json.")
    groups = list(layout.values()) + [document.get("plugins", [])]
    result = []
    for group in groups:
        if not isinstance(group, list):
            raise ValueError("Invalid plugin list in shell.json.")
        for plugin in group:
            if not isinstance(plugin, dict) or plugin.get("id") != PLUGIN:
                continue
            records = plugin.get("displays", [])
            if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                raise ValueError("Invalid saved display preferences; edit shell.json manually.")
            for i, record in enumerate(records):
                result.append((records, i, record))
    return result


def catalog(api):
    paths = config_paths()
    originals = [read_file(path) for path in paths]
    source = (originals[0] or b"").decode("utf-8")
    document = json.loads(originals[1], object_pairs_hook=strict_object) if originals[1] is not None else {}
    records = saved_records(document)
    rules = monitor_rules(source)
    try:
        live = api.query("monitors", "all")
        if not isinstance(live, list) or any(not isinstance(m, dict) or not isinstance(m.get("name"), str) for m in live):
            raise ValueError("Invalid monitor discovery")
        identities = [{key: m.get(key) if isinstance(m.get(key), str) else "" for key in ("name", "make", "model", "serial")} for m in live]
        identities.sort(key=lambda m: (m["name"], m["make"], m["model"], m["serial"]))
    except Exception:
        identities = None
    generation = json.dumps([*[digest(data) for data in originals], identities], sort_keys=True).encode()
    revision = hashlib.sha256(generation).hexdigest()
    entries = {}
    ambiguous_connectors = set()

    def entry(group, selector=None, connector=""):
        if group not in entries:
            entries[group] = {"key": hashlib.sha256(group.encode()).hexdigest(), "selector": selector,
                              "connector": connector, "name": connector or "Dynamic monitor rule", "icon": "",
                              "configured": False, "saved": False,
                              "connected": None if identities is None else any(m["name"] == connector for m in identities),
                              "removable": True, "reason": "", "rules": [], "records": []}
        return entries[group]

    def refuse(item, reason):
        item.update(removable=False, reason=reason)

    for i, rule in enumerate(rules):
        selector = rule["selector"]
        exact = isinstance(selector, str) and EXACT.fullmatch(selector)
        item = entry("connector:" + selector if exact else f"rule:{i}", selector, selector if exact else "")
        item["configured"] = True
        item["rules"].append(rule)
        if rule["reason"]:
            refuse(item, rule["reason"])
    for number, (_, _, record) in enumerate(records):
        connector = record.get("connector") if isinstance(record.get("connector"), str) else ""
        known = identities or []
        matches = [m["name"] for m in known if record.get("make") and record.get("model") and
                   m["make"] == record["make"] and m["model"] == record["model"] and
                   (not record.get("serial") or m["serial"] == record["serial"])]
        ambiguity = len(matches) > 1
        target = matches[0] if len(matches) == 1 else connector
        # Two different configured connectors cannot be collapsed using a hardware guess.
        if target != connector and "connector:" + connector in entries:
            ambiguity = True
        item = entry("connector:" + target if target else f"saved:{number}", connector=target)
        item["saved"] = True
        item["records"].append(number)
        for field in ("name", "icon"):
            if isinstance(record.get(field), str) and record[field]:
                item[field] = record[field]
        if ambiguity:
            refuse(item, "Saved hardware identity matches multiple displays or configured connectors; edit preferences manually.")
            ambiguous_connectors.update(matches + [connector])
    for item in entries.values():
        if item["connector"] in ambiguous_connectors:
            refuse(item, "Saved hardware identity is ambiguous; edit preferences manually.")
        selected = [records[i][2] for i in item["records"]]
        identities_saved = {json.dumps([r.get("make", ""), r.get("model", ""), r.get("serial", "")], sort_keys=True) for r in selected}
        if len(identities_saved) > 1:
            refuse(item, "Conflicting saved hardware identities share this connector; edit preferences manually.")
    if any(rule["reason"] and (not rule["selector"] or not EXACT.fullmatch(rule["selector"])) for rule in rules):
        for item in entries.values():
            if item["configured"] or item["saved"]:
                refuse(item, "A dynamic or broad monitor rule may also target this display; edit monitors.lua manually.")
    public = [{key: value for key, value in item.items() if key not in {"rules", "records"}} for item in entries.values()]
    return {"ok": True, "revision": revision, "entries": public}, (paths, originals, source, document, records, entries)


def configuration(api):
    return catalog(api)[0]


def prepare(api, request):
    if not isinstance(request, dict) or not isinstance(request.get("key"), str) or not isinstance(request.get("revision"), str):
        raise ValueError("Forget requires a catalog key and revision.")
    public, private = catalog(api)
    if public["revision"] != request["revision"]:
        raise ValueError("Display configuration or hardware changed. Detect displays and confirm again.")
    paths, originals, source, document, records, entries = private
    item = next((item for item in entries.values() if item["key"] == request["key"]), None)
    if item is None:
        raise ValueError("The selected display no longer exists. Detect displays again.")
    if not item["removable"]:
        raise ValueError(item["reason"])
    for rule in sorted(item["rules"], key=lambda rule: rule["start"], reverse=True):
        source = source[:rule["start"]] + source[rule["end"]:]
    selected = set(item["records"])
    for i in reversed(range(len(records))):
        if i in selected:
            values, index, _ = records[i]
            del values[index]
    replacements = [source.encode("utf-8") if item["rules"] else originals[0],
                    (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8") if selected else originals[1]]
    return paths, originals, replacements


def write_bytes(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace_checked(path, expected, data):
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(12))
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
        write_bytes(temporary, data, mode)
        if read_file(path) != expected:
            raise ValueError(f"Concurrent edit detected; refusing to overwrite {path}.")
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def reload_configuration(api):
    result = api.run(["hyprctl", "reload"])
    if result and result != "ok":
        raise ValueError("Hyprland reload rejected: " + result[:400])
    errors = api.run(["hyprctl", "configerrors"])
    if errors.strip():
        raise ValueError("Hyprland configuration errors: " + errors[:800])


def restore(api, journal):
    errors = []
    lua_changed = False
    for record in reversed(journal):
        path = Path(record["path"])
        try:
            current = read_file(path)
            if digest(current) == record["before"]:
                continue
            if digest(current) != record["after"]:
                raise ValueError(f"Concurrent edit preserved at {path}; recover the selected rule/preferences manually from {record['backup']}.")
            original = read_file(Path(record["backup"]))
            if original is None or digest(original) != record["before"]:
                raise ValueError(f"Backup unavailable or modified: {record['backup']}")
            replace_checked(path, current, original)
            lua_changed |= record["lua"]
        except Exception as error:
            errors.append(str(error))
    if lua_changed:
        try:
            reload_configuration(api)
        except Exception as error:
            errors.append("Restored file reload failed: " + str(error))
    return errors


def transact(api, root, state):
    if state["phase"] != "prepared":
        errors = restore(api, state.get("journal", []))
        backups = ", ".join(record["backup"] for record in state.get("journal", []))
        raise ValueError("Forget was interrupted; original files restored where safe." +
                         (" Recovery incomplete: " + " ".join(errors) if errors else "") +
                         (" Backups: " + backups if backups else ""))
    paths, originals, replacements = prepare(api, state["request"])
    journal = []
    for i, (path, original, replacement) in enumerate(zip(paths, originals, replacements)):
        if original == replacement:
            continue
        backup = path.with_name(path.name + ".forget-" + state["token"] + ".bak")
        write_bytes(backup, original)
        sync_directory(path.parent)
        journal.append({"path": str(path), "backup": str(backup), "before": digest(original),
                        "after": digest(replacement), "lua": i == 0})
    state.update(phase="writing", journal=journal)
    api.save(root, state)
    expected = originals[:]
    try:
        for i, (path, original, replacement) in enumerate(zip(paths, originals, replacements)):
            if original == replacement:
                continue
            if [read_file(p) for p in paths] != expected:
                raise ValueError("Configuration changed concurrently; forgetting was cancelled.")
            replace_checked(path, original, replacement)
            expected[i] = replacement
        if any(record["lua"] for record in journal):
            reload_configuration(api)
        if [read_file(p) for p in paths] != replacements:
            raise ValueError("Configuration changed while reloading; forgetting was not confirmed.")
        result = configuration(api)
        result.update(message="Display configuration and saved preferences forgotten.", backupPaths=[record["backup"] for record in journal])
        return result
    except Exception as error:
        errors = restore(api, journal)
        backups = " Backups: " + ", ".join(record["backup"] for record in journal)
        suffix = " Recovery incomplete: " + " ".join(errors) if errors else " Original files restored."
        raise ValueError(str(error) + suffix + backups) from error
