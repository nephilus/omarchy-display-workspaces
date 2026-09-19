import QtQuick
import QtQuick.Layouts
import Quickshell.Hyprland
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "display.workspaces"

  property var displayRanks: ({})

  // Quickshell has no global pointer-position API. Elect one live bar widget
  // to query Hyprland; every monitor's bar observes that same result.
  readonly property var cursorTracker: {
    var widgets = bar && typeof bar.moduleWidgets === "function" ? bar.moduleWidgets(moduleName) : []
    return widgets.length ? widgets[0] : null
  }
  property point cursorPosition: Qt.point(0, 0)
  property bool cursorKnown: false
  readonly property bool cursorQueryRunning: cursorQuery.running
  readonly property string cursorDisplayName: cursorKnown ? displayAt(cursorPosition.x, cursorPosition.y) : ""

  onCursorTrackerChanged: {
    cursorKnown = false
    if (cursorTracker === root) topologyRefresh.restart()
    else {
      cursorQuery.running = false
      topologyRefresh.stop()
    }
  }
  Component.onDestruction: {
    cursorQuery.running = false
    topologyRefresh.stop()
  }

  // Only the elected widget queries topology. Refreshing the models does not
  // schedule another refresh; paired legacy/v2 events share this debounce.
  Timer {
    id: topologyRefresh
    interval: 150
    onTriggered: {
      if (root.cursorTracker !== root) return
      Hyprland.refreshMonitors()
      Hyprland.refreshWorkspaces()
    }
  }

  Connections {
    target: Hyprland
    enabled: root.cursorTracker === root
    function onRawEvent(event) {
      if (event.name === "monitoradded" || event.name === "monitoraddedv2"
          || event.name === "monitorremoved" || event.name === "monitorremovedv2"
          || event.name === "configreloaded")
        topologyRefresh.restart()
    }
  }

  function displayAt(x, y) {
    if (!finiteNumber(x) || !finiteNumber(y)) return ""
    var monitors = Hyprland.monitors.values
    for (var i = 0; i < monitors.length; ++i) {
      var monitor = monitors[i]
      if (!monitor) continue
      var state = monitor.lastIpcObject
      if (!state || state.disabled || state.dpmsStatus === false) continue
      if (!finiteNumber(state.width) || state.width < 1 || state.width > 65536
          || !finiteNumber(state.height) || state.height < 1 || state.height > 65536
          || !finiteNumber(state.scale) || state.scale < 0.1 || state.scale > 16
          || !finiteNumber(state.transform) || state.transform < 0 || state.transform > 7
          || Math.floor(state.transform) !== state.transform
          || !finiteNumber(state.x) || !finiteNumber(state.y)) continue
      // Hyprland reports mode pixels, but cursorpos and x/y are logical.
      // Odd transforms (including flipped rotations) swap the mode axes.
      var rotated = state.transform % 2 !== 0
      var width = Math.round((rotated ? state.height : state.width) / state.scale)
      var height = Math.round((rotated ? state.width : state.height) / state.scale)
      if (width < 1 || height < 1) continue
      if (x >= state.x && x < state.x + width
          && y >= state.y && y < state.y + height) return monitor.name
    }
    return ""
  }
  function finiteNumber(value) {
    return typeof value === "number" && isFinite(value)
  }

  function pollCursor() {
    if (cursorTracker !== root || cursorQuery.running) return
    // A departing polling owner may still be terminating its last query.
    var widgets = bar.moduleWidgets(moduleName)
    for (var i = 0; i < widgets.length; ++i) {
      if (widgets[i] !== root && widgets[i].cursorQueryRunning) return
    }
    cursorQuery.running = true
  }

  Timer {
    id: cursorPoll
    interval: 100
    repeat: true
    triggeredOnStart: true
    running: root.cursorTracker === root && Hyprland.monitors.values.length > 0
    onTriggered: root.pollCursor()
    onRunningChanged: if (!running) cursorQuery.running = false
  }

  Process {
    id: cursorQuery
    command: ["hyprctl", "-j", "cursorpos"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (root.cursorTracker !== root) return
        try {
          var position = JSON.parse(text)
          if (typeof position.x !== "number" || typeof position.y !== "number"
              || !isFinite(position.x) || !isFinite(position.y)) {
            root.cursorKnown = false
            return
          }
          if (position.x !== root.cursorPosition.x || position.y !== root.cursorPosition.y)
            root.cursorPosition = Qt.point(position.x, position.y)
          root.cursorKnown = true
        } catch (error) {
          root.cursorKnown = false
        }
      }
    }
    onExited: function(exitCode, exitStatus) {
      if (exitCode !== 0 || exitStatus !== 0) root.cursorKnown = false
    }
  }

  Timer {
    interval: 1000
    running: cursorQuery.running
    onTriggered: {
      root.cursorKnown = false
      cursorQuery.signal(9)
    }
  }

  function refreshDisplayOrder() {
    var entries = root.setting("displays", [])
    var ranks = {}
    Hyprland.monitors.values.forEach(function(m) {
      if (!m || typeof m.name !== "string") return
      var index = entries.indexOf(displaySettings(m))
      if (index >= 0) ranks[m.name] = index
    })
    if (JSON.stringify(displayRanks) !== JSON.stringify(ranks)) displayRanks = ranks
  }
  onSettingsChanged: refreshDisplayOrder()
  Component.onCompleted: {
    refreshDisplayOrder()
    // Bar recreation can happen between the event and ownership registration.
    if (cursorTracker === root) topologyRefresh.restart()
  }
  Connections {
    target: Hyprland.monitors
    function onValuesChanged() { root.refreshDisplayOrder() }
  }

  function saveDisplays(entries) {
    if (!bar || !bar.shell || typeof bar.shell.updateEntryInline !== "function") return false
    var next = Object.assign({}, settings, { id: moduleName, displays: entries })
    settings = next
    bar.shell.updateEntryInline(moduleName, next)
    return true
  }

  function displayEntry(monitor) {
    var identity = monitor.lastIpcObject || {}
    var entry = Object.assign({}, displaySettings(monitor), { connector: monitor.name })
    // A transient missing IPC identity must not erase a saved hardware match.
    var keys = ["make", "model", "serial"]
    for (var i = 0; i < keys.length; ++i) {
      var key = keys[i]
      if (typeof identity[key] === "string") entry[key] = identity[key]
    }
    return entry
  }

  function renameDisplay(connector, name) {
    name = String(name).trim()
    if (!name) return false
    var monitor = Hyprland.monitors.values.filter(function(m) { return m && m.name === connector })[0]
    if (!monitor) return false
    var entries = root.setting("displays", []).slice()
    var index = entries.indexOf(displaySettings(monitor))
    if (index >= 0 && entries[index].name === name) return true
    var entry = Object.assign(displayEntry(monitor), { name: name })
    if (index < 0) entries.push(entry)
    else entries[index] = entry
    return saveDisplays(entries)
  }

  function setDisplayIcon(connector, icon) {
    var monitor = Hyprland.monitors.values.filter(function(m) { return m && m.name === connector })[0]
    if (!monitor || !icon) return false
    var entries = root.setting("displays", []).slice()
    var index = entries.indexOf(displaySettings(monitor))
    if (index >= 0 && entries[index].icon === icon) return true
    var entry = Object.assign(displayEntry(monitor), { icon: icon })
    if (index < 0) entries.push(entry)
    else entries[index] = entry
    return saveDisplays(entries)
  }

  function moveDisplayOrder(connector, direction) {
    var monitors = orderedMonitors()
    var index = monitors.findIndex(function(m) { return m.name === connector })
    var destination = index + direction
    if (index < 0 || destination < 0 || destination >= monitors.length) return false
    var moved = monitors.splice(index, 1)[0]
    monitors.splice(destination, 0, moved)
    var existing = root.setting("displays", [])
    var matched = monitors.map(function(m) { return displaySettings(m) })
    var entries = monitors.map(function(m) { return displayEntry(m) })
    // Keep disconnected displays' names and identity mappings.
    entries = entries.concat(existing.filter(function(entry) { return matched.indexOf(entry) < 0 }))
    return saveDisplays(entries)
  }
  // Inline shell.json settings: iconOnly and displays [{make, model, serial,
  // connector, name, icon}]. Unique hardware identity wins; connector is fallback.
  function displaySettings(monitor) {
    if (!monitor || typeof monitor.name !== "string") return {}
    var entries = root.setting("displays", [])
    var monitors = Hyprland.monitors.values
    for (var i = 0; i < entries.length; i++) {
      var entry = entries[i]
      if (!entry.make || !entry.model) continue
      var matches = []
      for (var j = 0; j < monitors.length; j++) {
        var candidate = monitors[j] && monitors[j].lastIpcObject
        if (!candidate) continue
        if (candidate.make === entry.make && candidate.model === entry.model
            && (!entry.serial || candidate.serial === entry.serial)) matches.push(monitors[j].name)
      }
      if (matches.length === 1 && matches[0] === monitor.name) return entry
    }
    for (var k = 0; k < entries.length; k++) {
      if (entries[k].connector === monitor.name) return entries[k]
    }
    return { name: monitor.name, icon: monitor.name.indexOf("eDP") === 0 ? "\uf109" : "\uf108" }
  }

  function orderedMonitors() {
    return Hyprland.monitors.values.filter(function(m) { return m && typeof m.name === "string" && m.name !== "" }).sort(function(a, b) {
      var ar = root.displayRanks[a.name] === undefined ? Infinity : root.displayRanks[a.name]
      var br = root.displayRanks[b.name] === undefined ? Infinity : root.displayRanks[b.name]
      if (ar !== br) return ar - br
      var ax = finiteNumber(a.x) ? a.x : Infinity, bx = finiteNumber(b.x) ? b.x : Infinity
      var ay = finiteNumber(a.y) ? a.y : Infinity, by = finiteNumber(b.y) ? b.y : Infinity
      if (ax !== bx) return ax - bx
      if (ay !== by) return ay - by
      if (finiteNumber(a.id) && finiteNumber(b.id) && a.id !== b.id) return a.id - b.id
      return a.name.localeCompare(b.name)
    })
  }

  function workspaceById(id) {
    var values = Hyprland.workspaces.values
    for (var i = 0; i < values.length; i++) {
      if (values[i] && values[i].id === id) return values[i]
    }
    return null
  }

  function workspacesFor(monitor) {
    if (!monitor) return []
    var values = Hyprland.workspaces.values
    return values.filter(function(workspace) {
      return workspace && workspace.monitor && workspace.monitor.id === monitor.id && workspace.id > 0
    }).map(function(workspace) { return workspace.id }).sort(function(a, b) { return a - b })
  }

  implicitWidth: groups.implicitWidth
  implicitHeight: groups.implicitHeight

  ArrangePanel {
    id: arranger
    bar: root.bar
    settings: root.settings
    workspaceWidget: root
  }

  GridLayout {
    id: groups
    columns: root.vertical ? 1 : Math.max(1, monitorRepeater.count)
    columnSpacing: Style.space(7)
    rowSpacing: Style.space(2)

    Repeater {
      id: monitorRepeater
      model: root.orderedMonitors()
      delegate: GridLayout {
        id: group
        required property var modelData
        readonly property string connector: modelData ? modelData.name : ""
        readonly property var display: root.displaySettings(modelData)
        columns: root.vertical ? 1 : 2
        columnSpacing: 0
        rowSpacing: 0

        WidgetButton {
          id: displayButton
          bar: root.bar
          active: group.connector !== "" && root.cursorTracker !== null && root.cursorTracker.cursorDisplayName === group.connector
          text: group.display.name || group.connector
          labelVisible: false
          implicitWidth: root.vertical ? root.barSize : displayLabel.implicitWidth + scaledHorizontalMargin * 2
          Row {
            id: displayLabel
            anchors.centerIn: parent
            spacing: Style.space(6)
            readonly property color labelColor: displayButton.active ? displayButton.activeColor : displayButton.foreground
            Text {
              objectName: "menubar-icon-" + group.connector
              width: Style.space(22)
              text: group.display.icon || "\uf108"
              horizontalAlignment: Text.AlignHCenter
              color: displayLabel.labelColor
              font.family: displayButton.fontFamily
              font.pixelSize: displayButton.fontSize
              renderType: Text.NativeRendering
            }
            Text {
              objectName: "menubar-name-" + group.connector
              visible: !root.setting("iconOnly", false) && !root.vertical
              text: displayButton.text
              color: displayLabel.labelColor
              font.family: displayButton.fontFamily
              font.pixelSize: displayButton.fontSize
              renderType: Text.NativeRendering
            }
          }
          tooltipText: (group.display.name || group.connector) + " · " + group.connector + "\n" + (group.modelData && group.modelData.lastIpcObject ? group.modelData.lastIpcObject.description || "" : "") + "\nRight-click to arrange displays and workspaces"
          horizontalMargin: 4
          fixedHeight: root.barSize
          onPressed: function(button) {
            if (button === Qt.RightButton) { arranger.toggle(); return }
            if (button === Qt.LeftButton && group.modelData && group.modelData.activeWorkspace) group.modelData.activeWorkspace.activate()
          }
        }

        GridLayout {
          columns: root.vertical ? 1 : Math.max(1, workspaceRepeater.count)
          columnSpacing: 0
          rowSpacing: 0
          Repeater {
            id: workspaceRepeater
            model: root.workspacesFor(group.modelData)
            delegate: WidgetButton {
              id: workspaceButton
              required property int modelData
              readonly property var workspace: root.workspaceById(modelData)
              readonly property bool shown: !!group.modelData && !!group.modelData.activeWorkspace && group.modelData.activeWorkspace.id === modelData
              readonly property bool focused: Hyprland.focusedWorkspace !== null && Hyprland.focusedWorkspace.id === modelData
              readonly property string workspaceName: workspace ? workspace.name : String(modelData)
              bar: root.bar
              text: shown ? "[" + workspaceName + "]" : workspaceName
              tooltipText: (group.display.name || group.connector) + " · Workspace " + workspaceName + (focused ? " · Keyboard focus" : shown ? " · Visible" : "")
              active: focused
              dimmed: !shown && (!workspace || workspace.toplevels.values.length === 0)
              horizontalMargin: 4
              fixedHeight: root.barSize
              onPressed: function(button) {
                if (button !== Qt.LeftButton) return
                if (workspace) workspace.activate()
              }
              Rectangle {
                anchors.bottom: parent.bottom
                anchors.horizontalCenter: parent.horizontalCenter
                width: parent.width - Style.space(8)
                height: Style.space(2)
                color: workspaceButton.activeColor
                visible: workspaceButton.focused
              }
            }
          }
        }
      }
    }
  }
}
