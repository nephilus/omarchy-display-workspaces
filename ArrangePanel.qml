import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import qs.Ui
import qs.Commons

Panel {
  id: root
  moduleName: "display.workspaces"
  ipcTarget: "display.workspaces"
  property var workspaceWidget: null
  property var anchor: workspaceWidget
  property var configurationEntries: []
  property string configurationRevision: ""
  property string configurationMessage: ""
  property string forgetFeedback: ""
  property bool forgetFeedbackIsError: false
  property int configurationGeneration: 0
  property bool configurationPending: false
  property bool completingConfiguration: false
  readonly property bool configurationBusy: configurationPending || configurationProcess.running || completingConfiguration
  readonly property var selectedConfiguration: configurationEntries.filter(function(entry) { return entry.key === configurationPicker.value })[0] || null
  readonly property bool forgetReady: !busy && !previewLocked && !configurationBusy && !statusProcess.running && !validationProcess.running && configurationRevision !== ""
  property var forgetCandidate: null
  property var liveBaseline: null
  property var displays: []
  property var unavailableMonitors: []
  // Stable geometry content prevents routine IPC updates from rebuilding editors.
  readonly property string unavailableGeometry: JSON.stringify(Hyprland.monitors.values.filter(function(m) {
    return m && normalOutput(m.lastIpcObject) && geometryFailure(m.lastIpcObject) !== ""
  }).map(function(m) {
    return { name: m.name, reason: geometryFailure(m.lastIpcObject) }
  }))
  readonly property var liveUnavailableMonitors: JSON.parse(unavailableGeometry)
  readonly property bool layoutBlocked: unavailableMonitors.length > 0 || liveUnavailableMonitors.length > 0
  readonly property var mapDisplays: displays.filter(function(m) {
    return !liveUnavailableMonitors.some(function(bad) { return bad.name === m.name })
  })
  readonly property var workspaceDisplays: workspaceColumns()
  readonly property string unavailableMessage: {
    var bad = unavailableMonitors.slice()
    liveUnavailableMonitors.forEach(function(m) {
      if (!bad.some(function(existing) { return existing.name === m.name })) bad.push(m)
    })
    return bad.map(function(m) { return m.name + ": " + m.reason }).join("\n")
  }
  property var positions: []
  property var draft: []
  property var geometryNotices: ({})
  property string message: ""
  property bool messageIsError: false
  property string validationMessage: "Loading current displays…"
  property bool valid: false
  property int changeCount: 0
  property int revision: 0
  property string dragOrigin: ""
  readonly property var displayIcons: [
    { label: "\uf109  Laptop", value: "\uf109" },
    { label: "\uf108  Monitor", value: "\uf108" },
    { label: "\uf26c  Television", value: "\uf26c" },
    { label: "\uf120  Terminal", value: "\uf120" },
    { label: "\uf233  Server", value: "\uf233" },
    { label: "\uf11b  Gaming", value: "\uf11b" },
    { label: "\uf015  Home", value: "\uf015" },
    { label: "\uf0b1  Work", value: "\uf0b1" }
  ]
  property string selectedName: ""
  property int page: 0
  property string token: ""
  property string trialState: ""
  property int secondsRemaining: 0
  property var queuedRequest: null
  property bool completingRequest: false
  property bool refreshPending: false
  property int topologyRevision: 0
  property string topologyNotice: ""
  readonly property string monitorHealth: healthSignature(Hyprland.monitors.values)
  readonly property bool requestPending: requestProcess.running || completingRequest || queuedRequest !== null
  readonly property bool trialActive: token !== "" && (trialState === "starting" || trialState === "pending")
  readonly property bool busy: requestPending || trialActive || refreshPending
  readonly property bool previewLocked: trialActive || (requestProcess.running && requestProcess.action === "begin")
  readonly property bool preferencesEditable: liveBaseline !== null && !busy && forgetCandidate === null
  readonly property bool editable: preferencesEditable && !layoutBlocked
  readonly property var selectedDisplay: displays.filter(function(m) { return m.name === selectedName })[0] || null
  readonly property var selectedPosition: positions.filter(function(p) { return p.name === selectedName })[0] || null
  readonly property var selectedGeometry: logicalGeometry(selectedDisplay, selectedPosition)
  readonly property var selectedModeOptions: displayModeOptions(selectedDisplay)
  readonly property var selectedScaleOptions: displayScaleOptions(selectedDisplay, selectedPosition)
  readonly property string panelScreen: anchor && anchor.QsWindow.window && anchor.QsWindow.window.screen ? anchor.QsWindow.window.screen.name : ""

  function close() {
    if (!previewLocked) controller.hide()
  }

  function request(action, payload) {
    if (action === "keep" && (layoutBlocked || refreshPending)) return
    // Destructive actions are never queued behind another operation.
    if (action === "forget" && (!forgetReady || !payload || payload.revision !== configurationRevision)) return
    if (requestProcess.running || completingRequest) { queuedRequest = { action: action, payload: payload }; return }
    if (action === "snapshot") requestProcess.topologyRevision = topologyRevision
    requestProcess.action = action
    var args = ["python3", Qt.resolvedUrl("apply.py").toString().replace("file://", ""), action]
    if (payload !== undefined && payload !== null) args.push(typeof payload === "string" ? payload : JSON.stringify(payload))
    requestProcess.command = args
    requestProcess.running = true
  }
  function decode(text) {
    try { return JSON.parse(text) }
    catch (error) { return { ok: false, message: "Could not read the display service response. No confirmation was sent; an active trial will revert automatically." } }
  }
  function refreshPresentation() {
    if (!workspaceWidget) return
    var monitors = workspaceWidget.orderedMonitors()
    var names = monitors.map(function(m) { return m.name })
    function present(m) {
      var live = monitors.filter(function(value) { return value.name === m.name })[0]
      var label = workspaceWidget.displaySettings(live || { name: m.name, lastIpcObject: m })
      return Object.assign({}, m, { label: label.name || m.name, icon: label.icon || "\uf108" })
    }
    function order(a, b) { return names.indexOf(a.name) - names.indexOf(b.name) }
    displays = displays.map(present).sort(order)
    unavailableMonitors = unavailableMonitors.map(present).sort(order)
  }
  function refreshConfiguration() {
    forgetCandidate = null
    configurationGeneration++
    configurationPending = true
    if (opened) configurationTimer.restart()
  }
  function loadConfiguration() {
    if (!opened || !configurationPending || configurationTimer.running || configurationProcess.running || completingConfiguration
        || (requestPending && requestProcess.action === "forget")) return
    configurationPending = false
    configurationProcess.generation = configurationGeneration
    configurationProcess.running = true
  }
  function adoptConfiguration(result) {
    if (configurationRevision !== result.revision) forgetCandidate = null
    configurationRevision = result.revision
    configurationEntries = result.entries
    configurationMessage = ""
  }
  function configurationLabel(entry) {
    var label = entry.name || entry.connector || entry.selector || "Display rule"
    if (entry.icon) label = entry.icon + " " + label
    if (entry.connector && entry.connector !== entry.name && entry.name) label += " · " + entry.connector
    label += entry.configured ? (entry.saved ? " · rule + customization" : " · rule only") : " · customization only"
    return label + (entry.removable ? "" : " · cannot forget")
  }
  function forgetWarning(entry) {
    var removal = entry.configured
      ? "Remove this monitor rule" + (entry.saved ? " and its saved customization" : "") + "?"
      : "Remove this saved display customization?"
    var risk = !entry.configured ? ""
      : entry.connected === false ? " Its mode/layout may change when reconnected."
      : " This display may be active; removing its rule can change its mode/layout immediately."
    return removal + risk + " Changed files are backed up first. Click Confirm forget to proceed."
  }
  function forgetSelected() {
    if (!forgetReady) return
    var entry = selectedConfiguration
    if (!entry || !entry.removable) return
    if (!forgetCandidate) {
      forgetFeedback = ""
      forgetCandidate = { key: entry.key, revision: configurationRevision, warning: forgetWarning(entry) }
      return
    }
    var candidate = forgetCandidate
    forgetCandidate = null
    if (candidate.key !== entry.key || candidate.revision !== configurationRevision) {
      refreshConfiguration()
      return
    }
    invalidateValidation()
    forgetFeedback = "Forgetting display; backing up changed files…"
    forgetFeedbackIsError = false
    message = ""
    messageIsError = false
    request("forget", { key: candidate.key, revision: candidate.revision })
  }
  Connections {
    target: root.workspaceWidget
    function onSettingsChanged() { root.refreshPresentation(); root.refreshConfiguration() }
    function onDisplayRanksChanged() { root.refreshPresentation() }
  }
  Connections {
    target: Hyprland
    function onRawEvent(event) {
      if (event.name === "configreloaded" || event.name === "monitoradded" || event.name === "monitoraddedv2"
          || event.name === "monitorremoved" || event.name === "monitorremovedv2")
        root.refreshConfiguration()
    }
  }
  function normalOutput(state) {
    return !state || (!state.disabled && (state.mirrorOf === undefined || state.mirrorOf === null || state.mirrorOf === "" || state.mirrorOf === "none"))
  }
  function geometryFailure(state) {
    if (!state) return "Display geometry is unavailable."
    var fields = [
      ["width", 1, 65536, false], ["height", 1, 65536, false],
      ["scale", 0.1, 16, false], ["transform", 0, 7, true],
      ["x", -32768, 32768, true], ["y", -32768, 32768, true]
    ]
    for (var i = 0; i < fields.length; ++i) {
      var field = fields[i], value = state[field[0]]
      if (typeof value !== "number" || !isFinite(value) || value < field[1] || value > field[2]
          || (field[3] && Math.floor(value) !== value)) return "Invalid display " + field[0] + "."
    }
    if (Math.round(state.width / state.scale) < 1 || Math.round(state.height / state.scale) < 1)
      return "Display has zero logical size."
    return ""
  }
  function healthSignature(monitors) {
    return JSON.stringify(monitors.filter(function(m) { return m && normalOutput(m.lastIpcObject) }).map(function(m) {
      return [m.name, geometryFailure(m.lastIpcObject) === "" ? "usable" : "unusable"]
    }).sort(function(a, b) { return String(a[0]).localeCompare(String(b[0])) }))
  }
  function snapshotSignature(result) {
    return JSON.stringify(result.monitors.map(function(m) { return [m.name, "usable"] })
      .concat((result.unavailableMonitors || []).map(function(m) { return [m.name, "unusable"] }))
      .sort(function(a, b) { return String(a[0]).localeCompare(String(b[0])) }))
  }
  function workspaceColumns() {
    var columns = displays.concat(unavailableMonitors).map(function(m) {
      var liveFault = liveUnavailableMonitors.filter(function(bad) { return bad.name === m.name })[0]
      return liveFault ? Object.assign({}, m, { reason: liveFault.reason }) : m
    })
    draft.forEach(function(w) {
      if (columns.some(function(m) { return m.name === w.target })) return
      var known = liveBaseline ? (liveBaseline.disabledMonitors || []).filter(function(m) { return m.name === w.target })[0] : null
      var label = workspaceWidget ? workspaceWidget.displaySettings({ name: w.target, lastIpcObject: known || {} }) : {}
      columns.push({ name: w.target, label: label.name || w.target || "Unassigned", icon: label.icon || "\uf108",
        reason: known ? "Display is disabled." : "Display is unavailable for arrangement." })
    })
    var names = workspaceWidget ? workspaceWidget.orderedMonitors().map(function(m) { return m.name }) : []
    return columns.sort(function(a, b) {
      var ai = names.indexOf(a.name), bi = names.indexOf(b.name)
      return (ai < 0 ? names.length : ai) - (bi < 0 ? names.length : bi)
    })
  }
  function invalidateValidation() {
    revision++
    valid = false
    changeCount = 0
    validationTimer.stop()
  }
  onMonitorHealthChanged: {
    if (!opened) return
    if (liveBaseline && snapshotSignature(liveBaseline) === monitorHealth && !refreshPending) return
    topologyRevision++
    invalidateValidation()
    refreshPending = true
    validationMessage = "Displays changed. Refreshing…"
    topologyTimer.restart()
  }
  function refreshWhenSafe() {
    if (!opened || !refreshPending || topologyTimer.running || trialActive || requestPending || statusProcess.running || validationProcess.running) return
    snapshot()
  }
  function detectDisplays() {
    if (busy || statusProcess.running || validationProcess.running) return
    refreshConfiguration()
    invalidateValidation()
    topologyNotice = ""
    message = ""
    messageIsError = false
    refreshPending = true
    validationMessage = "Detecting displays…"
    Hyprland.refreshMonitors()
    Hyprland.refreshWorkspaces()
    topologyTimer.restart()
  }
  Timer {
    id: topologyTimer
    interval: 180
    onTriggered: root.refreshWhenSafe()
  }
  // Some mode failures recover without a connector event. Observe only while
  // this panel reports a fault; health transitions, not position updates, refresh drafts.
  Timer {
    interval: 1000
    repeat: true
    running: root.opened && root.layoutBlocked
    onTriggered: Hyprland.refreshMonitors()
  }
  function adoptSnapshot(result) {
    liveBaseline = result
    displays = result.monitors.map(function(m) { return Object.assign({}, m, { label: m.name, icon: "\uf108" }) })
    unavailableMonitors = (result.unavailableMonitors || []).map(function(m) { return Object.assign({}, m, { label: m.name, icon: "\uf108" }) })
    refreshPresentation()
    positions = displays.map(function(m) {
      return { name: m.name, x: m.x, y: m.y, width: m.width, height: m.height, refreshRate: m.refreshRate, scale: m.scale }
    })
    geometryNotices = ({})
    draft = result.workspaces.filter(function(w) { return w.id > 0 }).map(function(w) {
      return { id: w.id, name: w.name, windows: w.windows, source: w.monitor, target: w.monitor }
    }).sort(function(a, b) { return a.id - b.id })
    if (!displays.some(function(m) { return m.name === selectedName })) selectedName = displays.length ? displays[0].name : ""
    changed()
  }
  function snapshot() {
    if (requestPending || trialActive || statusProcess.running || validationProcess.running) return
    invalidateValidation()
    refreshPending = true
    validationMessage = "Loading current displays…"
    request("snapshot")
  }
  function plan() {
    return { baseline: liveBaseline, uiScreen: panelScreen, positions: positions, workspaces: draft.filter(function(w) { return w.source !== w.target }).map(function(w) { return { id: w.id, source: w.source, target: w.target } }) }
  }
  function logicalGeometry(display, position) {
    if (!display || !position) return null
    var rotated = display.transform % 2 !== 0
    return {
      width: Math.round((rotated ? position.height : position.width) / position.scale),
      height: Math.round((rotated ? position.width : position.height) / position.scale)
    }
  }
  function sameMode(a, b) {
    return a && b && a.width === b.width && a.height === b.height
      && Math.abs(a.refreshRate - b.refreshRate) <= 0.01
  }
  function modeValue(mode) {
    return mode.width + "x" + mode.height + "@" + mode.refreshRate
  }
  function displayModeOptions(display) {
    if (!display) return []
    var options = display.modeOptions || []
    if (options.some(function(option) { return sameMode(option, display) })) return options
    return [{
      value: modeValue(display),
      label: display.width + " × " + display.height + " @ " + Number(display.refreshRate.toFixed(3)) + " Hz (current; not advertised)",
      width: display.width, height: display.height, refreshRate: display.refreshRate
    }].concat(options)
  }
  function modeOptionIndex(options, position) {
    for (var i = 0; i < options.length; ++i) {
      if (sameMode(options[i], position)) return i
    }
    return -1
  }
  function scaleFits(mode, scale) {
    if (!mode || typeof scale !== "number" || !isFinite(scale) || scale < 0.1 || scale > 16) return false
    var quantized = Math.round(scale * 120) / 120
    var width = mode.width / quantized, height = mode.height / quantized
    return Math.abs(scale - quantized) <= 0.000001
      && width >= 1 && height >= 1
      && Math.abs(width - Math.round(width)) <= 0.000001
      && Math.abs(height - Math.round(height)) <= 0.000001
  }
  function displayScaleOptions(display, position) {
    if (!display || !position) return []
    var values = [1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4].filter(function(scale) { return scaleFits(position, scale) })
    if (scaleFits(position, display.scale) && values.indexOf(display.scale) < 0) values.push(display.scale)
    if (values.indexOf(position.scale) < 0) values.push(position.scale)
    return values.sort(function(a, b) { return a - b }).map(function(scale) {
      return { value: scale, label: Number(scale.toFixed(6)) + "×" }
    })
  }
  function scaleOptionIndex(options, position) {
    if (!position) return -1
    for (var i = 0; i < options.length; ++i) {
      if (options[i].value === position.scale) return i
    }
    return -1
  }
  function editDisplayGeometry(fields, notice) {
    if (!editable || !selectedPosition) return
    positions = positions.map(function(p) { return p.name === selectedName ? Object.assign({}, p, fields) : p })
    var notices = Object.assign({}, geometryNotices)
    notices[selectedName] = notice
    geometryNotices = notices
    message = ""
    messageIsError = false
    topologyNotice = ""
    changed()
  }
  function selectDisplayMode(value) {
    if (!editable || !selectedDisplay || !selectedPosition || !(selectedDisplay.modeOptions || []).length) return
    var option = selectedModeOptions.filter(function(choice) { return choice.value === value })[0]
    if (!option) return
    // Selecting the active mode preserves its exact refresh rate, not the rounded catalog rate.
    var mode = sameMode(option, selectedDisplay) ? selectedDisplay : option
    var scale = selectedPosition.scale
    var notice = ""
    if (!scaleFits(mode, scale)) {
      scale = 1
      notice = "Scale reset to 1× because the previous scale does not fit this resolution. Positions are unchanged; use relative placement to repair gaps or overlaps."
    }
    editDisplayGeometry({ width: mode.width, height: mode.height, refreshRate: mode.refreshRate, scale: scale }, notice)
  }
  function selectDisplayScale(scale) {
    if (!editable || !selectedPosition || !scaleFits(selectedPosition, scale)
        || !selectedScaleOptions.some(function(option) { return option.value === scale })) return
    editDisplayGeometry({ scale: scale }, "")
  }
  function hasDisplayEdits() {
    return liveBaseline && positions.some(function(p) {
      var original = liveBaseline.monitors.filter(function(m) { return m.name === p.name })[0]
      return !original || ["x", "y", "width", "height", "refreshRate", "scale"].some(function(field) {
        return original[field] !== p[field]
      })
    })
  }
  function changed() {
    revision++
    valid = false
    changeCount = 0
    validationMessage = layoutBlocked ? "Layout changes are blocked until all enabled displays have usable geometry." : "Checking layout…"
    if (liveBaseline && !layoutBlocked && !trialActive && !refreshPending) validationTimer.restart()
  }
  function moveDisplay(name, x, y) {
    if (!editable) return
    positions = positions.map(function(p) { return p.name === name ? Object.assign({}, p, { x: Math.round(x), y: Math.round(y) }) : p })
    selectedName = name
    message = ""
    messageIsError = false
    topologyNotice = ""
    changed()
  }
  function placeRelative(direction, reference) {
    if (!editable || !selectedDisplay || !selectedPosition || !reference || reference.name === selectedName) return
    var position = positions.filter(function(p) { return p.name === reference.name })[0]
    if (!position) return
    var dimensions = logicalGeometry(selectedDisplay, selectedPosition)
    var referenceDimensions = logicalGeometry(reference, position)
    var x = position.x, y = position.y
    if (direction === "left") x -= dimensions.width
    else if (direction === "right") x += referenceDimensions.width
    else if (direction === "above") y -= dimensions.height
    else y += referenceDimensions.height
    moveDisplay(selectedName, x, y)
  }
  function stage(id, connector) {
    if (!editable) return
    draft = draft.map(function(w) { return Object.assign({}, w, { target: w.id === id ? connector : w.target }) })
    message = ""
    topologyNotice = ""
    changed()
  }
  function apply() {
    if (!editable || !valid || !changeCount || validationProcess.running) return
    valid = false
    message = "Starting 20-second preview…"
    messageIsError = false
    request("begin", plan())
  }
  function receiveState(result) {
    if (result.message) {
      message = result.message
      messageIsError = result.ok === false || result.state === "failed"
    }
    if (result.token) token = result.token
    if (result.state === "pending" && trialState !== "pending") {
      Hyprland.refreshMonitors()
      Hyprland.refreshWorkspaces()
    }
    if (result.state) trialState = result.state
    secondsRemaining = Math.max(0, Number(result.secondsRemaining || 0))
    if (trialState === "kept" || trialState === "reverted" || trialState === "failed") {
      token = ""
      Hyprland.refreshMonitors()
      Hyprland.refreshWorkspaces()
      if (trialState === "kept") root.close()
      else if (root.opened) refreshPending = true
    } else if (trialActive && !root.opened) {
      root.open()
    }
  }
  function response(action, result) {
    if (!result.ok) {
      message = result.message || "Display operation failed. Detect displays before trying again."
      if (action === "snapshot" && requestProcess.topologyRevision === topologyRevision) refreshPending = false
      messageIsError = true
      if (action === "forget") {
        forgetFeedback = message
        forgetFeedbackIsError = true
        message = ""
        messageIsError = false
        refreshConfiguration()
        refreshPending = true
        Hyprland.refreshMonitors()
        Hyprland.refreshWorkspaces()
      }
      if (result.state) receiveState(result)
      return
    }
    if (action === "snapshot") {
      if (requestProcess.topologyRevision !== topologyRevision) return
      if (liveBaseline && snapshotSignature(liveBaseline) !== snapshotSignature(result)) {
        var edited = draft.some(function(w) { return w.source !== w.target }) || hasDisplayEdits()
        topologyNotice = edited ? "Displays changed. Pending edits were reset." : "Displays changed. Layout refreshed."
      }
      refreshPending = false
      adoptSnapshot(result)
    }
    else if (action === "forget") {
      forgetCandidate = null
      adoptConfiguration(result)
      forgetFeedback = result.message || "Display forgotten."
      if (result.backupPaths && result.backupPaths.length) forgetFeedback += "\nBackups: " + result.backupPaths.join(", ")
      forgetFeedbackIsError = false
      refreshConfiguration()
      refreshPending = true
      Hyprland.refreshMonitors()
      Hyprland.refreshWorkspaces()
    }
    else if (action === "resume") {
      if (result.token && (root.opened || result.uiScreen === panelScreen)) {
        adoptSnapshot(result.baseline)
        positions = result.plan.positions.map(function(p) { return Object.assign({}, p) })
        draft = draft.map(function(w) {
          var move = result.plan.workspaces.filter(function(m) { return m.id === w.id })[0]
          return Object.assign({}, w, { target: move ? move.target : w.source })
        })
        changeCount = result.plan.displayChanges + result.plan.workspaceChanges
        receiveState(result)
        refreshPending = snapshotSignature(result.baseline) !== monitorHealth
      } else if (root.opened) refreshPending = true
    } else receiveState(result)
  }
  onOpenedChanged: {
    forgetCandidate = null
    if (opened) {
      refreshConfiguration()
      if (!trialActive) {
        recoveryTimer.stop()
        message = ""
        topologyNotice = ""
        topologyRevision++
        invalidateValidation()
        refreshPending = true
        if (!requestPending) request("resume")
      }
    } else if (previewLocked) {
      Qt.callLater(function() { if (root.previewLocked) root.open() })
    }
  }

  // A display reconfiguration can recreate the bar and all its plugin objects.
  // Recover the independent worker's token rather than losing confirmation.
  Timer {
    id: recoveryTimer
    interval: 250
    running: true
    onTriggered: {
      if (!root.panelScreen || root.requestPending) { restart(); return }
      if (!root.trialActive) root.request("resume")
    }
  }

  Process {
    id: requestProcess
    property string action: ""
    property int topologyRevision: 0
    stdout: StdioCollector { id: responseOutput; waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(code, status) {
      root.completingRequest = true
      root.response(action, root.decode(responseOutput.text))
      Qt.callLater(function() {
        root.completingRequest = false
        if (root.queuedRequest && !requestProcess.running) {
          var next = root.queuedRequest
          root.queuedRequest = null
          root.request(next.action, next.payload)
        }
        root.refreshWhenSafe()
        root.loadConfiguration()
      })
    }
  }
  Timer {
    id: configurationTimer
    interval: 200
    onTriggered: root.loadConfiguration()
  }
  Process {
    id: configurationProcess
    property int generation: 0
    command: ["python3", Qt.resolvedUrl("apply.py").toString().replace("file://", ""), "configuration"]
    stdout: StdioCollector { id: configurationOutput; waitForEnd: true }
    onExited: function(code, status) {
      root.completingConfiguration = true
      if (generation === root.configurationGeneration) {
        var result = root.decode(configurationOutput.text)
        if (result.ok) root.adoptConfiguration(result)
        else {
          root.configurationRevision = ""
          root.configurationMessage = result.message || "Could not load display configuration. Use Detect displays to retry."
        }
      }
      Qt.callLater(function() {
        root.completingConfiguration = false
        root.loadConfiguration()
      })
    }
  }
  Timer {
    id: validationTimer
    interval: 140
    onTriggered: {
      if (!root.liveBaseline || root.layoutBlocked || root.trialActive || root.refreshPending || !root.opened) return
      if (validationProcess.running || root.requestPending) { restart(); return }
      validationProcess.revision = root.revision
      validationProcess.command = ["python3", Qt.resolvedUrl("apply.py").toString().replace("file://", ""), "validate", JSON.stringify(root.plan())]
      validationProcess.running = true
    }
  }
  Process {
    id: validationProcess
    property int revision: 0
    stdout: StdioCollector { id: validationOutput; waitForEnd: true }
    onExited: function(code, status) {
      if (revision !== root.revision || root.layoutBlocked || root.trialActive || root.refreshPending || root.requestPending) {
        Qt.callLater(root.refreshWhenSafe)
        return
      }
      var result = root.decode(validationOutput.text)
      root.valid = result.ok === true
      root.validationMessage = result.message || (root.valid ? "Layout is safe to try." : "Invalid layout.")
      root.changeCount = root.valid ? Number(result.displayChanges || 0) + Number(result.workspaceChanges || 0) : 0
    }
  }
  Timer {
    interval: 250
    repeat: true
    running: root.trialActive
    onTriggered: {
      if (statusProcess.running || requestProcess.running) return
      statusProcess.command = ["python3", Qt.resolvedUrl("apply.py").toString().replace("file://", ""), "status", root.token]
      statusProcess.running = true
    }
  }
  Process {
    id: statusProcess
    stdout: StdioCollector { id: statusOutput; waitForEnd: true }
    onExited: function(code, status) {
      Qt.callLater(root.refreshWhenSafe)
      if (!root.trialActive) return
      var result = root.decode(statusOutput.text)
      if (result.state) root.receiveState(result)
      else { root.message = result.message; root.messageIsError = true }
    }
  }

  ArrangePopup {
    id: popup
    anchorItem: root.anchor
    owner: root
    bar: root.bar
    open: root.opened
    backgroundColor: Qt.rgba(Color.popups.background.r, Color.popups.background.g, Color.popups.background.b, 0.9)
    padding: Style.space(10)
    readonly property real horizontalContentInset: padding * 2 + Border.left(borderSpec) + Border.right(borderSpec)
    readonly property real naturalWidth: Math.max(Style.space(600), Math.min(Style.space(740), availableCardWidth - horizontalContentInset))
    readonly property real fittedScale: Math.min(1,
      Math.max(1, availableCardWidth - horizontalContentInset - 1) / naturalWidth,
      Math.max(1, availableCardHeight - verticalContentInset - 1) / Math.max(1, content.implicitHeight))
    contentScale: fittedScale
    contentWidth: Math.ceil(naturalWidth * contentScale + horizontalContentInset)
    contentHeight: Math.ceil(content.implicitHeight * contentScale + verticalContentInset)

    Column {
      id: content
      width: popup.naturalWidth
      spacing: Style.space(8)
      RowLayout {
        width: parent.width
        spacing: Style.space(4)
        Button { objectName: "displays-tab"; text: "Displays"; selected: root.page === 0; verticalPadding: Style.space(4); onClicked: root.page = 0 }
        Button { objectName: "workspaces-tab"; text: "Workspaces"; selected: root.page === 1; verticalPadding: Style.space(4); onClicked: root.page = 1 }
        Item { Layout.fillWidth: true }
        Button { objectName: "detect-displays"; text: "Detect displays"; verticalPadding: Style.space(4); enabled: !root.busy && !statusProcess.running && !validationProcess.running; onClicked: root.detectDisplays() }
      }
      RowLayout {
        width: parent.width
        spacing: Style.space(6)
        visible: root.page === 1 && root.configurationEntries.length > 0
        onVisibleChanged: if (!visible) root.forgetCandidate = null
        Dropdown {
          id: configurationPicker
          objectName: "configuration-display-picker"
          Layout.fillWidth: true
          rowHeight: Style.space(30)
          enabled: root.forgetReady && root.forgetCandidate === null
          options: root.configurationEntries.map(function(entry) {
            return { value: entry.key, label: root.configurationLabel(entry) }
          })
          onOptionsChanged: {
            root.forgetCandidate = null
            if (!options.some(function(option) { return option.value === value })) value = options.length ? options[0].value : ""
          }
          onChanged: { root.forgetCandidate = null; root.forgetFeedback = "" }
        }
        Button {
          objectName: "forget-display"
          text: root.forgetCandidate ? "Confirm forget" : "Forget display"
          verticalPadding: Style.space(4)
          enabled: root.forgetReady && root.selectedConfiguration !== null && root.selectedConfiguration.removable
          opacity: enabled ? 1 : 0.45
          onClicked: root.forgetSelected()
        }
        Button {
          objectName: "cancel-forget"
          text: "Cancel"
          visible: root.forgetCandidate !== null
          verticalPadding: Style.space(4)
          onClicked: root.forgetCandidate = null
        }
      }
      Text {
        objectName: "forget-display-warning"
        width: parent.width
        visible: root.page === 1 && text !== ""
        text: root.forgetCandidate ? root.forgetCandidate.warning
          : root.forgetFeedback ? root.forgetFeedback
          : root.configurationMessage ? root.configurationMessage
          : root.selectedConfiguration && !root.selectedConfiguration.removable
            ? root.selectedConfiguration.reason || "This rule cannot be removed safely."
          : root.configurationBusy ? "Loading configured and saved displays…" : ""
        color: root.forgetCandidate || (root.forgetFeedback ? root.forgetFeedbackIsError : root.configurationMessage || (root.selectedConfiguration && !root.selectedConfiguration.removable)) ? Color.urgent : Color.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
      }
        Column {
          width: parent.width
          spacing: Style.space(6)
          visible: root.page === 0
          DisplayMap {
            id: map
            objectName: "display-map"
            width: parent.width
            height: Style.space(180)
            displays: root.mapDisplays
            positions: root.positions.filter(function(p) { return root.mapDisplays.some(function(m) { return m.name === p.name }) })
            editable: root.editable
            selectedName: root.selectedName
            onSelected: function(name) { root.selectedName = name }
            onPositionEdited: function(name, x, y) { root.moveDisplay(name, x, y) }
          }
          RowLayout {
            width: parent.width
            spacing: Style.space(6)
            Text { text: "Resolution"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
            ComboBox {
              id: modePicker
              objectName: "display-mode"
              Layout.fillWidth: true
              Layout.minimumWidth: 0
              model: root.selectedModeOptions
              textRole: "label"
              valueRole: "value"
              currentIndex: root.modeOptionIndex(model, root.selectedPosition)
              enabled: root.editable && root.selectedPosition !== null && root.selectedDisplay !== null
                && (root.selectedDisplay.modeOptions || []).length > 0
              onActivated: function(index) {
                var option = model[index]
                if (option) root.selectDisplayMode(option.value)
              }
            }
            Text { text: "Scale"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
            ComboBox {
              id: scalePicker
              objectName: "display-scale"
              Layout.preferredWidth: Style.space(110)
              model: root.selectedScaleOptions
              textRole: "label"
              valueRole: "value"
              currentIndex: root.scaleOptionIndex(model, root.selectedPosition)
              enabled: root.editable && root.selectedPosition !== null && count > 0
              onActivated: function(index) {
                var option = model[index]
                if (option) root.selectDisplayScale(option.value)
              }
            }
          }
          Text {
            objectName: "display-geometry-notice"
            width: parent.width
            visible: text !== ""
            text: root.geometryNotices[root.selectedName] || (root.selectedDisplay && !(root.selectedDisplay.modeOptions || []).length
              ? "No advertised modes. The current mode is preserved; resolution selection is unavailable." : "")
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
          }
          RowLayout {
            width: parent.width
            Text { text: "X"; color: Color.foreground; font.family: Style.font.family }
            SpinBox {
              objectName: "display-x"
              from: -32768; to: 32768; editable: true
              value: root.selectedPosition ? root.selectedPosition.x : 0
              enabled: root.editable && root.selectedPosition !== null
              onValueModified: root.moveDisplay(root.selectedName, value, root.selectedPosition.y)
            }
            Text { text: "Y"; color: Color.foreground; font.family: Style.font.family }
            SpinBox {
              objectName: "display-y"
              from: -32768; to: 32768; editable: true
              value: root.selectedPosition ? root.selectedPosition.y : 0
              enabled: root.editable && root.selectedPosition !== null
              onValueModified: root.moveDisplay(root.selectedName, root.selectedPosition.x, value)
            }
            Text {
              objectName: "display-logical-size"
              Layout.fillWidth: true
              text: root.selectedGeometry ? root.selectedGeometry.width + " × " + root.selectedGeometry.height + " logical pixels" : ""
              color: Color.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              horizontalAlignment: Text.AlignRight
              elide: Text.ElideRight
            }
          }
          Flow {
            width: parent.width
            spacing: Style.space(5)
            ComboBox {
              id: referenceDisplay
              objectName: "display-reference"
              model: root.displays.filter(function(m) { return m.name !== root.selectedName })
              textRole: "label"
              enabled: root.editable && count > 0
              width: Style.space(145)
            }
            Repeater {
              model: [ { label: "Left of", direction: "left" }, { label: "Right of", direction: "right" }, { label: "Above", direction: "above" }, { label: "Below", direction: "below" } ]
              delegate: Button {
                required property var modelData
                objectName: "display-place-" + modelData.direction
                text: modelData.label
                verticalPadding: Style.space(4)
                enabled: root.editable && referenceDisplay.currentIndex >= 0
                onClicked: root.placeRelative(modelData.direction, referenceDisplay.model[referenceDisplay.currentIndex])
              }
            }
          }
          Text {
            objectName: "display-session-only"
            width: parent.width
            text: "Resolution, scale and layout changes are session-only. Preview before Apply; positions stay explicit."
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }
        }
        Item {
          width: parent.width
          height: columns.implicitHeight
          visible: root.page === 1
          Row {
            id: columns
            spacing: Style.space(8)
            Repeater {
              model: root.workspaceDisplays
              delegate: Rectangle {
                id: displayColumn
                required property var modelData
                required property int index
                objectName: "display-" + modelData.name
                z: root.dragOrigin === modelData.name ? 1 : 0
                width: (content.width - columns.spacing * Math.max(0, root.workspaceDisplays.length - 1)) / Math.max(1, root.workspaceDisplays.length)
                height: chips.y + Math.max(Style.space(32), chips.implicitHeight) + Style.space(8)
                color: drop.containsDrag ? Color.menu.selectedBackground : "transparent"
                border.color: drop.containsDrag ? Color.accent : Color.foreground
                border.width: 1
                radius: Style.space(6)
                TextField {
                  objectName: "display-name-" + displayColumn.modelData.name
                  x: Style.space(8); y: Style.space(6)
                  width: parent.width - Style.space(16)
                  height: Style.space(30)
                  text: displayColumn.modelData.label
                  enabled: root.preferencesEditable && root.workspaceWidget !== null
                  selectByMouse: true
                  maximumLength: 64
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                  onEditingFinished: {
                    if (!root.preferencesEditable || text.trim() === displayColumn.modelData.label) return
                    var connector = displayColumn.modelData.name, label = text
                    Qt.callLater(function() {
                      if (!root.preferencesEditable || !root.workspaceWidget) return
                      if (!root.workspaceWidget.renameDisplay(connector, label)) {
                        root.message = "Enter a non-empty display name; settings could not be saved."
                        root.messageIsError = true
                      }
                    })
                  }
                }
                Dropdown {
                  id: iconPicker
                  objectName: "display-icon-" + displayColumn.modelData.name
                  x: Style.space(8); y: Style.space(40)
                  width: parent.width - Style.space(16)
                  rowHeight: Style.space(30)
                  value: displayColumn.modelData.icon
                  options: root.displayIcons.some(function(choice) { return choice.value === value })
                    ? root.displayIcons : root.displayIcons.concat([{ label: value + "  Custom icon", value: value }])
                  enabled: root.preferencesEditable && root.workspaceWidget !== null
                  opacity: enabled ? 1 : 0.45
                  onChanged: function(icon) {
                    var connector = displayColumn.modelData.name
                    Qt.callLater(function() {
                      if (!root.preferencesEditable || !root.workspaceWidget) return
                      if (!root.workspaceWidget.setDisplayIcon(connector, icon)) {
                        root.message = "Display icon could not be saved."
                        root.messageIsError = true
                      }
                    })
                  }
                }
                RowLayout {
                  x: Style.space(6); y: Style.space(74)
                  width: parent.width - Style.space(16)
                  Button {
                    objectName: "display-left-" + displayColumn.modelData.name
                    text: "‹"
                    verticalPadding: Style.space(2)
                    enabled: root.preferencesEditable && root.workspaceWidget !== null && displayColumn.index > 0
                    opacity: enabled ? 1 : 0.45
                    onClicked: {
                      var connector = displayColumn.modelData.name
                      Qt.callLater(function() { if (root.preferencesEditable && root.workspaceWidget) root.workspaceWidget.moveDisplayOrder(connector, -1) })
                    }
                  }
                  Text {
                    Layout.fillWidth: true
                    text: displayColumn.modelData.icon + " " + displayColumn.modelData.name
                    color: Color.foreground
                    horizontalAlignment: Text.AlignHCenter
                    font.family: Style.font.family
                    font.pixelSize: Style.font.bodySmall
                  }
                  Button {
                    objectName: "display-right-" + displayColumn.modelData.name
                    text: "›"
                    verticalPadding: Style.space(2)
                    enabled: root.preferencesEditable && root.workspaceWidget !== null && displayColumn.index < root.workspaceDisplays.length - 1
                    opacity: enabled ? 1 : 0.45
                    onClicked: {
                      var connector = displayColumn.modelData.name
                      Qt.callLater(function() { if (root.preferencesEditable && root.workspaceWidget) root.workspaceWidget.moveDisplayOrder(connector, 1) })
                    }
                  }
                }
                DropArea {
                  id: drop
                  anchors.fill: parent
                  enabled: root.editable && !displayColumn.modelData.reason
                  keys: ["workspace-arrangement"]
                  onDropped: function(event) {
                    var id = event.source.workspaceId, destination = displayColumn.modelData.name
                    event.acceptProposedAction()
                    Qt.callLater(function() { root.stage(id, destination) })
                  }
                }
                Text {
                  id: columnFault
                  x: Style.space(8); y: Style.space(108)
                  width: parent.width - Style.space(16)
                  visible: text !== ""
                  text: displayColumn.modelData.reason || ""
                  color: Color.urgent
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                  wrapMode: Text.WordWrap
                }
                Column {
                  id: chips
                  x: Style.space(8); y: columnFault.y + (columnFault.visible ? columnFault.implicitHeight + Style.space(6) : 0)
                  spacing: Style.space(4)
                  Repeater {
                    model: root.draft.filter(function(w) { return w.target === displayColumn.modelData.name })
                    delegate: Item {
                      id: slot
                      required property var modelData
                      width: displayColumn.width - Style.space(16)
                      height: Style.space(32)
                      z: mouse.drag.active ? 10 : 0
                      Rectangle {
                        id: chip
                        property int workspaceId: slot.modelData.id
                        objectName: "workspace-" + workspaceId
                        width: slot.width; height: slot.height
                        color: slot.modelData.source !== slot.modelData.target ? Color.accent : Color.foreground
                        radius: Style.space(5)
                        opacity: mouse.drag.active ? 0.8 : 1
                        Drag.active: mouse.drag.active
                        Drag.source: chip
                        Drag.keys: ["workspace-arrangement"]
                        Drag.hotSpot.x: width / 2
                        Drag.hotSpot.y: height / 2
                        Text {
                          anchors.centerIn: parent
                          text: slot.modelData.name + " · " + slot.modelData.windows + (slot.modelData.windows === 1 ? " window" : " windows")
                          color: Color.background
                          font.family: Style.font.family
                          font.pixelSize: Style.font.body
                        }
                        MouseArea {
                          id: mouse
                          anchors.fill: parent
                          enabled: root.editable && !displayColumn.modelData.reason
                          preventStealing: true
                          drag.target: chip
                          cursorShape: drag.active ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                          onPressed: root.dragOrigin = displayColumn.modelData.name
                          onReleased: { chip.Drag.drop(); chip.x = 0; chip.y = 0; root.dragOrigin = "" }
                          onCanceled: { chip.Drag.cancel(); chip.x = 0; chip.y = 0; root.dragOrigin = "" }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        Text {
          objectName: "unavailable-displays"
          width: parent.width
          visible: root.unavailableMessage !== ""
          text: root.unavailableMessage
          color: Color.urgent
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }
        Text {
          id: statusLabel
          width: parent.width
          visible: text !== ""
          text: root.messageIsError && root.message ? root.message
            : root.trialActive ? ""
            : root.previewLocked ? "Starting preview…"
            : !root.valid ? root.validationMessage : ""
          color: (root.messageIsError && root.message) || (!root.valid && !root.previewLocked && !root.refreshPending && root.liveBaseline && !validationTimer.running && !validationProcess.running) ? Color.urgent : Color.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }
        Text {
          width: parent.width
          visible: root.topologyNotice !== "" && !root.previewLocked
          text: root.topologyNotice
          color: Color.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }
        RowLayout {
          id: footer
          width: parent.width
          spacing: Style.space(4)
          Text {
            Layout.fillWidth: true
            text: root.trialActive ? "Reverts in " + root.secondsRemaining + "s"
              : root.changeCount > 0 ? root.changeCount + " pending" : ""
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
          Button {
            objectName: "cancel"
            text: root.trialActive ? "Revert" : "Cancel"
            verticalPadding: Style.space(4)
            enabled: !requestProcess.running
            onClicked: { if (root.trialActive) root.request("revert", root.token); else root.close() }
          }
          Button {
            objectName: "preview"
            text: "Preview (20s)"
            verticalPadding: Style.space(4)
            enabled: root.editable && root.valid && root.changeCount > 0 && !validationProcess.running
            opacity: enabled ? 1 : 0.45
            onClicked: root.apply()
          }
          Button {
            objectName: "apply"
            text: "Apply"
            verticalPadding: Style.space(4)
            enabled: !requestProcess.running && !root.layoutBlocked && !root.refreshPending && root.trialState === "pending" && root.trialActive && root.secondsRemaining > 0
            opacity: enabled ? 1 : 0.45
            onClicked: root.request("keep", root.token)
          }
        }
      }
    }
  }
