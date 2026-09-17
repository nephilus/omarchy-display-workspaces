# Display Workspaces for Omarchy

A multi-monitor workspace widget for the Omarchy Quickshell bar, with display names and icons, cursor-following highlights, and a compact display/workspace arrangement panel.

**Plugin ID:** `dctlab.workspaces` · **Version:** 1.8.0

The historical plugin ID is intentionally retained so existing users keep their saved settings. The GitHub repository owner does not need to match the plugin ID.

## Install from GitHub

Yes: this repository is a normal Omarchy shell plugin. Its `manifest.json` is at the repository root, so you can paste its GitHub URL into Omarchy's plugin-add prompt, or run:

```bash
omarchy plugin add https://github.com/nephilus/omarchy-display-workspaces.git --enable
```

Review the trust warning and choose a bar section when prompted. To explicitly place it on the left:

```bash
omarchy plugin enable dctlab.workspaces --section left
```

If you want it to replace the stock workspace widget, disable the stock widget after confirming the new one works:

```bash
omarchy plugin disable omarchy.workspaces
```

This requires the **Omarchy Quickshell plugin system and Hyprland's Lua API**. It is not a standalone Quickshell configuration, Waybar module, or plugin for older `hyprland.conf`-only setups. Tested on Omarchy **4.0.3-1**, Hyprland **0.56.2**, and Quickshell **0.3.1**; compatibility with other releases is not guaranteed.

See [installation and updates](docs/INSTALLATION.md) for requirements, noninteractive installation, removal, and existing local clones.

## Features

- Workspace groups per display, with manually editable names, Nerd Font icons, and column order.
- Green display highlight follows the cursor; workspace buttons retain focus/visibility indicators.
- Hotplug-aware discovery and a manual **Detect displays** action.
- Compact, translucent **Displays** and **Workspaces** tabs, opened by right-clicking a display label.
- Live monitor positioning and workspace moves with a **20-second Preview**, explicit **Apply**, and an independent rollback worker.
- Healthy displays remain usable in the panel when another output reports invalid geometry; unsafe layout actions are blocked.
- **Forget display** lists both configured monitor rules and saved customizations, including unnamed or disconnected entries.

## Important behavior

**Apply keeps an arrangement for the current session only.** It does not write the arrangement into `monitors.lua`; there are no saved layout profiles. Names, icons, and display order save immediately in Omarchy's shell settings.

**Forget is a configuration edit, not merely hiding a display.** After a second confirmation, it removes the selected supported monitor rule and matching saved customization, backs up changed files, and reloads Hyprland when a rule changes. Removing an active rule can change its mode or layout immediately. It does not physically disconnect a monitor, and Hyprland may still detect it.

Dynamic, broad, conditional, or ambiguous monitor rules are shown but cannot be removed automatically. Failed reloads trigger guarded restoration; concurrent external edits are preserved and reported for manual recovery.

Read [usage, safety, and recovery](docs/USAGE.md) before changing monitor rules. Plugins execute unsandboxed code as your user: review code before installation.

## Development

```bash
git clone https://github.com/nephilus/omarchy-display-workspaces.git
cd omarchy-display-workspaces
omarchy plugin validate .
python3 -m unittest test_apply test_configuration
```

There is no build step or Python package installation. The Python backend uses the standard library. Backend regression tests use synthetic data and temporary files; they do not rearrange your real displays.

- `Workspaces.qml`: bar widget and saved presentation settings.
- `ArrangePanel.qml`, `ArrangePopup.qml`, `DisplayMap.qml`: native arrangement UI.
- `apply.py`: discovery, validation, session preview/rollback, and detached workers.
- `configuration.py`: nonexecuting Lua rule discovery, merged catalog, guarded configuration edits and recovery.

## License and credits

[MIT](LICENSE). Derived from [Omarchy](https://github.com/basecamp/omarchy), including its workspace widget and keyboard-panel implementation. The upstream copyright notice is retained. This is a third-party plugin, not an official Omarchy release.
