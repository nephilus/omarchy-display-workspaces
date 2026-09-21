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
  readonly property var automationCoordinator: workspaceWidget && workspaceWidget.cursorTracker ? workspaceWidget.cursorTracker : null
  readonly property string autoStatus: automationCoordinator ? automationCoordinator.autoStatus : "idle"
  readonly property string autoMessage: automationCoordinator ? automationCoordinator.autoMessage : ""
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
  readonly property bool forgetReady: !busy && !previewLocked && !configurationBusy && !profileBusy && profileCandidate === null && !statusProcess.running && !validationProcess.running && configurationRevision !== ""
  property var forgetCandidate: null
  property var profileEntries: []
  property string profileRevision: ""
  property string profileCatalogMessage: ""
  property string profileFeedback: ""
  property bool profileFeedbackIsError: false
  property string profileFeedbackDetails: ""
  property bool profileDetailsExpanded: false
  property bool automaticFeedbackDismissed: false
  property string profileForm: ""
  property string selectedProfileId: ""
  property string profileNameText: ""
  property int profileGeneration: 0
  property bool profilePending: false
  property bool completingProfile: false
  property var profileCandidate: null
  readonly property bool profileActive: profileProcess.running || completingProfile
  readonly property bool profileBusy: profilePending || profileActive
  readonly property var selectedProfile: profileEntries.filter(function(entry) { return entry.id === selectedProfileId })[0] || null
  readonly property bool profileReady: opened && liveBaseline !== null && !busy && !profileBusy && !configurationBusy
    && !previewLocked && forgetCandidate === null && !statusProcess.running && !validationProcess.running
    && !validationTimer.running && profileRevision !== ""
  readonly property bool profileNameValid: profileNameText.trim().length > 0 && profileNameText.trim().length <= 80
  readonly property bool profileManaging: profileForm !== "" || profileCandidate !== null
  readonly property bool automaticFailure: autoStatus === "failed" || (trialAutomatic && messageIsError && message !== "")
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
    var position = positions.filter(function(p) { return p.name === m.name })[0]
    return position && position.enabled && !liveUnavailableMonitors.some(function(bad) { return bad.name === m.name })
  })
  readonly property var workspaceDisplays: workspaceColumns()
  // Presentation edits update bindings without recreating columns and dropdowns.
  readonly property string workspaceDisplayOrder: JSON.stringify(workspaceDisplays.map(function(m) { return m.name }))
  readonly property string unavailableMessage: {
    var bad = unavailableMonitors.slice()
    liveUnavailableMonitors.forEach(function(m) {
      if (!bad.some(function(existing) { return existing.name === m.name })) bad.push(m)
    })
    return bad.map(function(m) { return m.name + ": " + m.reason }).join("\n")
  }
  property var positions: []
  property var draft: []
  property var profileWorkspaces: null
  property var removeWorkspaces: []
  property var geometryNotices: ({})
  property bool undockCandidate: false
  property bool redockCandidate: false
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
  property bool trialAutomatic: false
  property string trialProfileName: ""
  property int secondsRemaining: 0
  property var queuedRequest: null
  property bool completingRequest: false
  property bool refreshPending: false
  property int topologyRevision: 0
  property string topologyNotice: ""
  readonly property string monitorHealth: healthSignature(Hyprland.monitors.values)
  readonly property bool requestPending: requestProcess.running || completingRequest || queuedRequest !== null
  readonly property bool trialActive: token !== "" && (trialState === "starting" || trialState === "pending")
  readonly property bool busy: requestPending || trialActive || refreshPending || profileActive
  readonly property bool previewLocked: trialActive || (requestProcess.running && requestProcess.action === "begin")
  readonly property bool preferencesEditable: liveBaseline !== null && !busy && !profileBusy && forgetCandidate === null && profileCandidate === null
  readonly property bool editable: preferencesEditable && !layoutBlocked
  readonly property var selectedDisplay: displays.filter(function(m) { return m.name === selectedName })[0] || null
  readonly property var selectedPosition: positions.filter(function(p) { return p.name === selectedName })[0] || null
  readonly property var selectedGeometry: logicalGeometry(selectedDisplay, selectedPosition)
  readonly property var selectedModeOptions: displayModeOptions(selectedDisplay)
  readonly property var selectedScaleOptions: displayScaleOptions(selectedDisplay, selectedPosition)
  readonly property string panelScreen: anchor && anchor.QsWindow.window && anchor.QsWindow.window.screen ? anchor.QsWindow.window.screen.name : ""

  function close() {
    if (!previewLocked || trialAutomatic) controller.hide()
  }

  function request(action, payload) {
    if (action === "keep" && trialAutomatic) return
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
    catch (error) { return { ok: false, message: "Could not read the display service response. No confirmation was sent. Open the panel again to recover an active trial's status; automatic trials may still keep after verification." } }
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
        || profileActive || (requestPending && requestProcess.action === "forget")) return
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
  function refreshProfiles() {
    if (profileProcess.running && !completingProfile && profileProcess.action !== "profiles") {
      profileFeedback = "Profile context changed during the operation. Refreshing saved profiles; review the result before retrying."
      profileFeedbackIsError = false
    }
    profileCandidate = null
    profileForm = ""
    profileMenu.close()
    profileGeneration++
    profilePending = true
    if (opened) profileTimer.restart()
  }
  function loadProfiles() {
    if (!opened || !profilePending || profileTimer.running || profileActive || busy || configurationBusy
        || previewLocked || forgetCandidate !== null || statusProcess.running || validationProcess.running || validationTimer.running) return
    profilePending = false
    profileProcess.action = "profiles"
    profileProcess.generation = profileGeneration
    profileProcess.command = ["python3", Qt.resolvedUrl("apply.py").toString().replace("file://", ""), "profiles"]
    profileProcess.running = true
  }
  function adoptProfiles(result) {
    var previous = selectedProfile
    profileCandidate = null
    profileRevision = result.revision
    profileEntries = result.profiles
    var id = result.savedId || selectedProfileId
    var entry = result.profiles.filter(function(profile) { return profile.id === id })[0] || result.profiles[0] || null
    selectedProfileId = entry ? entry.id : ""
    if (result.savedId || !previous || !entry || previous.id !== entry.id || previous.name !== entry.name)
      profileNameText = entry ? entry.name : ""
    profileCatalogMessage = ""
  }
  function selectProfile(id) {
    if (!profileReady || profileManaging) return
    profileCandidate = null
    selectedProfileId = id
    profileNameText = selectedProfile ? selectedProfile.name : ""
    profileDetailsExpanded = false
  }
  function beginProfileForm(form) {
    if (!profileReady || profileManaging || layoutBlocked || (form === "update" && !selectedProfile)) return
    profileForm = form
    profileNameText = form === "update" ? selectedProfile.name : ""
    profileFeedback = ""
    profileDetailsExpanded = false
    Qt.callLater(function() { profileNameInput.forceActiveFocus(); profileNameInput.selectAll() })
  }
  function suppressAutomation() {
    if (workspaceWidget && typeof workspaceWidget.requestAutomationCheck === "function")
      workspaceWidget.requestAutomationCheck(false, true)
  }
  function startProfile(action, payload) {
    if (!profileReady || payload.revision !== profileRevision) return
    profileCandidate = null
    profileFeedbackTimer.stop()
    profileFeedbackDetails = ""
    profileFeedback = action === "profile-load" ? "Preparing layout…"
      : action === "profile-auto" ? "Updating automatic restoration…"
      : action === "profile-delete" ? "Deleting profile…" : "Saving live arrangement…"
    profileFeedbackIsError = false
    profileProcess.action = action
    profileProcess.generation = profileGeneration
    profileProcess.draftRevision = revision
    profileProcess.command = ["python3", Qt.resolvedUrl("apply.py").toString().replace("file://", ""), action, JSON.stringify(payload)]
    profileProcess.running = true
  }
  function saveCurrentProfile() {
    if (!profileReady || profileForm !== "new" || !profileNameValid || profileCandidate !== null) return
    startProfile("profile-save", { name: profileNameText.trim(), baseline: liveBaseline, revision: profileRevision })
  }
  function prepareProfile(action) {
    if (!profileReady || profileCandidate !== null || !selectedProfile
        || (action === "profile-save" && (profileForm !== "update" || !profileNameValid))
        || (action === "profile-load" && !selectedProfile.canLoad)
        || (action === "profile-auto" && !selectedProfile.automatic && !selectedProfile.canAutomate)) return
    var entry = selectedProfile
    var edited = hasDisplayEdits() || profileWorkspaces !== null || draft.some(function(w) { return w.source !== w.target })
    if (action === "profile-load" && !edited) {
      startProfile(action, { id: entry.id, revision: profileRevision })
      return
    }
    var name = profileNameText.trim()
    var warning = action === "profile-auto" ? (entry.automatic
      ? "Turn off automatic restoration for “" + entry.name + "”? Live displays and your draft stay unchanged."
      : "Restore “" + entry.name + "” on reconnect or session start without asking?\nChanges keep automatically after 20 seconds of checks. You can Revert during that time; recovery is not guaranteed. Only enable an arrangement you have checked physically.")
      : action === "profile-delete" ? "Delete “" + entry.name + "”? A backup is kept. Live displays and your draft stay unchanged."
      : action === "profile-save" ? "Update “" + entry.name + "” from the current LIVE arrangement"
        + (name !== entry.name ? " and rename it to “" + name + "”" : "") + "?\nDraft edits are not saved. Automatic restoration turns off. A backup is kept."
      : "Discard your draft edits and review “" + entry.name + "”? Live displays stay unchanged."
    profileCandidate = { action: action, id: entry.id, revision: profileRevision, name: entry.name,
      replacementName: name, enabled: !entry.automatic, generation: profileGeneration, draftRevision: revision, warning: warning }
  }
  function confirmProfile() {
    var candidate = profileCandidate
    if (!profileReady || !candidate) return
    if (!selectedProfile || candidate.id !== selectedProfile.id || candidate.name !== selectedProfile.name
        || candidate.revision !== profileRevision || candidate.generation !== profileGeneration
        || candidate.draftRevision !== revision) {
      profileCandidate = null
      profileFeedback = "The selection or draft changed. Review the profile and confirm again."
      profileFeedbackIsError = true
      return
    }
    var payload = { id: candidate.id, revision: candidate.revision }
    if (candidate.action === "profile-save") {
      payload.name = candidate.replacementName
      payload.baseline = liveBaseline
    }
    if (candidate.action === "profile-auto") payload.enabled = candidate.enabled
    startProfile(candidate.action, payload)
  }
  function profileResponse(action, result) {
    if (!result.ok) {
      var failure = result.message || "Profile operation failed. Close and reopen the panel before trying again."
      profileFeedbackTimer.stop()
      if (action === "profiles") {
        profileRevision = ""
        profileCatalogMessage = failure
      } else {
        profileFeedback = failure
        profileFeedbackIsError = true
        refreshProfiles()
      }
      return
    }
    if (action === "profile-load") {
      if (profileProcess.draftRevision !== revision) {
        profileFeedback = "The draft changed while loading. Load the profile again after reviewing your edits."
        profileFeedbackIsError = true
        return
      }
      adoptSnapshot(result.baseline)
      positions = result.positions.map(function(p) { return Object.assign({ enabled: true, modeOptions: [] }, p) })
      adoptWorkspacePlan(result)
      geometryNotices = ({})
      message = ""
      messageIsError = false
      topologyNotice = ""
      page = 0
      changed()
      profileFeedback = "Profile ready to review. Preview and Apply to keep it for this session."
      profileFeedbackIsError = false
      profileFeedbackDetails = result.message || ""
      profileFeedbackTimer.restart()
      refreshProfiles()
    } else {
      adoptProfiles(result)
      if (action !== "profiles") profileForm = ""
      if (action !== "profiles") {
        profileFeedback = action === "profile-auto" ? "Automatic restoration updated."
          : action === "profile-delete" ? "Profile deleted." : "Live arrangement saved."
        profileFeedbackDetails = result.message || ""
        if (result.backupPaths && result.backupPaths.length) profileFeedbackDetails += "\nBackups: " + result.backupPaths.join(", ")
        profileFeedbackIsError = false
        profileFeedbackTimer.restart()
      }
    }
  }
  onPageChanged: {
    profileMenu.close()
    if (page !== 2) { profileCandidate = null; profileForm = "" }
  }
  onSelectedProfileIdChanged: profilePicker.value = selectedProfileId
  onAutoMessageChanged: automaticFeedbackDismissed = false
  onForgetCandidateChanged: if (forgetCandidate === null) Qt.callLater(loadProfiles)
  Connections {
    target: root.workspaceWidget
    function onSettingsChanged() { root.refreshPresentation(); root.refreshConfiguration() }
    function onDisplayRanksChanged() { root.refreshPresentation() }
  }
  onAutomationCoordinatorChanged: Qt.callLater(observeAutomatic)
  Connections {
    target: root.automationCoordinator
    function onAutoResultChanged() { root.observeAutomatic() }
  }
  function observeAutomatic() {
    var result = automationCoordinator ? automationCoordinator.autoResult : null
    if (!result || !result.automatic || !result.token) return
    var state = result.state || result.status
    if (trialAutomatic && token === result.token) {
      receiveState(result)
      refreshWhenSafe()
      loadProfiles()
    } else if ((state === "starting" || state === "pending")
        && (opened || workspaceWidget === automationCoordinator) && !requestPending && !profileActive) {
      request("resume")
    }
  }
  Connections {
    target: Hyprland
    function onRawEvent(event) {
      if (event.name === "configreloaded" || event.name === "monitoradded" || event.name === "monitoraddedv2"
          || event.name === "monitorremoved" || event.name === "monitorremovedv2")
        { root.refreshConfiguration(); root.refreshProfiles() }
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
      var position = positions.filter(function(p) { return p.name === m.name })[0]
      return liveFault ? Object.assign({}, m, { reason: liveFault.reason })
        : position && !position.enabled ? Object.assign({}, m, { reason: "Display will be disabled." }) : m
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
    refreshProfiles()
    if (liveBaseline && snapshotSignature(liveBaseline) === monitorHealth && !refreshPending) return
    topologyRevision++
    invalidateValidation()
    refreshPending = true
    validationMessage = "Displays changed. Refreshing…"
    topologyTimer.restart()
  }
  function refreshWhenSafe() {
    if (!opened || !refreshPending || topologyTimer.running || trialActive || requestPending || profileActive || statusProcess.running || validationProcess.running) return
    snapshot()
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
    profileWorkspaces = null
    removeWorkspaces = []
    undockCandidate = false
    redockCandidate = false
    liveBaseline = result
    displays = result.monitors.concat(result.restorableMonitors || []).map(function(m) {
      return Object.assign({}, m, { label: m.name, icon: "\uf108" })
    })
    unavailableMonitors = (result.unavailableMonitors || []).map(function(m) { return Object.assign({}, m, { label: m.name, icon: "\uf108" }) })
    refreshPresentation()
    positions = displays.map(function(m) {
      return { name: m.name, x: m.x, y: m.y, width: m.width, height: m.height, refreshRate: m.refreshRate,
        scale: m.scale, transform: m.transform, enabled: m.disabled !== true, modeOptions: m.modeOptions || [] }
    })
    geometryNotices = ({})
    draft = result.workspaces.filter(function(w) { return w.id > 0 }).map(function(w) {
      return { id: w.id, name: w.name, windows: w.windows, source: w.monitor, target: w.monitor }
    }).sort(function(a, b) { return a.id - b.id })
    if (!displays.some(function(m) { return m.name === selectedName })) selectedName = displays.length ? displays[0].name : ""
    changed()
  }
  function adoptWorkspacePlan(value) {
    profileWorkspaces = value.profileWorkspaces ? value.profileWorkspaces.map(function(w) {
      return { id: w.id, target: w.target }
    }) : null
    removeWorkspaces = (value.removeWorkspaces || []).slice()
    var desired = profileWorkspaces || []
    var moves = value.workspaces || []
    var cards = draft.map(function(w) {
      var target = desired.filter(function(m) { return m.id === w.id })[0]
        || moves.filter(function(m) { return m.id === w.id })[0]
      return Object.assign({}, w, { target: target ? target.target : w.source })
    })
    desired.forEach(function(w) {
      if (!cards.some(function(card) { return card.id === w.id }))
        cards.push({ id: w.id, name: String(w.id), windows: 0, source: null, target: w.target })
    })
    draft = cards.sort(function(a, b) { return a.id - b.id })
  }
  function snapshot() {
    if (requestPending || trialActive || profileActive || statusProcess.running || validationProcess.running) return
    invalidateValidation()
    refreshPending = true
    validationMessage = "Loading current displays…"
    request("snapshot")
  }
  function plan(commit) {
    return { baseline: liveBaseline, uiScreen: panelScreen, positions: positions,
      profileWorkspaces: profileWorkspaces, undock: commit === "undock",
      commit: commit || "",
      workspaces: draft.filter(function(w) { return w.source !== null && w.source !== w.target }).map(function(w) {
        return { id: w.id, source: w.source, target: w.target }
      }) }
  }
  function logicalGeometry(display, position) {
    if (!display || !position) return null
    var rotated = position.transform % 2 !== 0
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
    if (!editable || !selectedPosition || !selectedPosition.enabled) return
    positions = positions.map(function(p) { return p.name === selectedName ? Object.assign({}, p, fields) : p })
    var notices = Object.assign({}, geometryNotices)
    notices[selectedName] = notice
    geometryNotices = notices
    message = ""
    messageIsError = false
    topologyNotice = ""
    changed()
  }
  function setDisplayEnabled(name, enabled) {
    if (!editable || profileWorkspaces !== null) return
    var position = positions.filter(function(p) { return p.name === name })[0]
    if (!position || position.enabled === enabled) return
    var enabledPositions = positions.filter(function(p) { return p.enabled && p.name !== name })
    if (!enabled && enabledPositions.length === 0) {
      message = "Keep at least one display enabled."
      messageIsError = true
      return
    }
    var destination = enabledPositions.filter(function(p) { return p.name.indexOf("eDP-") === 0 })[0]
      || enabledPositions[0]
    positions = positions.map(function(p) {
      return p.name === name ? Object.assign({}, p, { enabled: enabled }) : p
    })
    if (!enabled) draft = draft.map(function(w) {
      return w.target === name ? Object.assign({}, w, { target: destination.name }) : w
    })
    undockCandidate = false
    redockCandidate = false
    message = ""
    messageIsError = false
    topologyNotice = ""
    changed()
  }
  function prepareUndock() {
    if (!editable || profileWorkspaces !== null) return
    var internal = positions.filter(function(p) { return p.enabled && p.name.indexOf("eDP-") === 0 })[0]
    var external = positions.filter(function(p) { return p.enabled && p.name.indexOf("eDP-") !== 0 })
    if (!internal || external.length === 0) {
      message = !internal ? "Undock safely requires a healthy enabled internal display."
        : "No enabled external displays need to be disabled."
      messageIsError = true
      return
    }
    if (!undockCandidate) {
      undockCandidate = true
      message = "Disable all external displays and move their workspaces to " + internal.name
        + "? This takes effect immediately after confirmation."
      messageIsError = false
      return
    }
    var names = external.map(function(p) { return p.name })
    positions = positions.map(function(p) {
      return names.indexOf(p.name) >= 0 ? Object.assign({}, p, { enabled: false }) : p
    })
    draft = draft.map(function(w) {
      return names.indexOf(w.target) >= 0 ? Object.assign({}, w, { target: internal.name }) : w
    })
    undockCandidate = false
    message = "Undocking safely…"
    messageIsError = false
    request("begin", plan("undock"))
  }
  function prepareRedock() {
    if (!editable || !liveBaseline || !(liveBaseline.restorableMonitors || []).length) return
    if (!redockCandidate) {
      redockCandidate = true
      undockCandidate = false
      message = "Re-enable every display disabled by this session? This takes effect immediately."
      messageIsError = false
      return
    }
    var saved = liveBaseline.redockMonitors || []
    positions = positions.map(function(p) {
      var original = saved.filter(function(m) { return m.name === p.name })[0]
      return original ? {
        name: original.name, x: original.x, y: original.y, width: original.width, height: original.height,
        refreshRate: original.refreshRate, scale: original.scale, transform: original.transform,
        enabled: true, modeOptions: original.modeOptions || p.modeOptions || []
      } : Object.assign({}, p, { enabled: true })
    })
    var savedWorkspaces = liveBaseline.redockWorkspaces || []
    draft = draft.map(function(w) {
      var savedWorkspace = savedWorkspaces.filter(function(item) { return item.id === w.id })[0]
      return savedWorkspace ? Object.assign({}, w, { target: savedWorkspace.monitor }) : w
    })
    redockCandidate = false
    message = "Re-enabling all displays…"
    messageIsError = false
    request("begin", plan("redock"))
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
      var original = liveBaseline.monitors.concat(liveBaseline.restorableMonitors || [])
        .filter(function(m) { return m.name === p.name })[0]
      return !original || p.enabled !== (original.disabled !== true)
        || ["x", "y", "width", "height", "refreshRate", "scale", "transform"].some(function(field) {
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
    if (!editable || removeWorkspaces.indexOf(id) >= 0) return
    draft = draft.map(function(w) { return Object.assign({}, w, { target: w.id === id ? connector : w.target }) })
    if (profileWorkspaces !== null)
      profileWorkspaces = profileWorkspaces.map(function(w) {
        return w.id === id ? { id: w.id, target: connector } : w
      })
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
    if (result.automatic !== undefined) trialAutomatic = result.automatic === true
    if (result.profileName !== undefined) trialProfileName = result.profileName || ""
    if (result.token) token = result.token
    if (result.state === "pending" && trialState !== "pending") {
      Hyprland.refreshMonitors()
      Hyprland.refreshWorkspaces()
    }
    if (result.state) trialState = result.state
    secondsRemaining = Math.max(0, Number(result.secondsRemaining || 0))
    if (trialState === "kept" || trialState === "reverted" || trialState === "failed") {
      token = ""
      profileWorkspaces = null
      removeWorkspaces = []
      Hyprland.refreshMonitors()
      Hyprland.refreshWorkspaces()
      if (trialState === "kept") root.close()
      else if (root.opened) refreshPending = true
    } else if (trialActive && !trialAutomatic && !root.opened) {
      root.open()
    }
  }
  function response(action, result) {
    if (!result.ok) {
      message = result.message || "Display operation failed. Close and reopen the panel before trying again."
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
      var profileDraft = profileWorkspaces !== null ? Object.assign(plan(), { removeWorkspaces: removeWorkspaces.slice() }) : null
      if (liveBaseline && snapshotSignature(liveBaseline) !== snapshotSignature(result)) {
        var edited = profileDraft !== null || draft.some(function(w) { return w.source !== w.target }) || hasDisplayEdits()
        topologyNotice = profileDraft !== null ? "Displays changed. Profile draft retained; review its targets before Preview."
          : edited ? "Displays changed. Pending edits were reset." : "Displays changed. Layout refreshed."
      }
      refreshPending = false
      adoptSnapshot(result)
      if (profileDraft !== null) {
        positions = profileDraft.positions.map(function(p) { return Object.assign({ enabled: true, modeOptions: [] }, p) })
        adoptWorkspacePlan(profileDraft)
        changed()
      }
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
      if (result.token && (root.opened || result.uiScreen === panelScreen
          || (result.automatic && workspaceWidget === automationCoordinator))) {
        adoptSnapshot(result.baseline)
        positions = result.plan.positions.map(function(p) { return Object.assign({ enabled: true, modeOptions: [] }, p) })
        adoptWorkspacePlan(result.plan)
        changeCount = result.plan.displayChanges + result.plan.workspaceChanges
        receiveState(result)
        refreshPending = snapshotSignature(result.baseline) !== monitorHealth
      } else if (root.opened) {
        profileWorkspaces = null
        removeWorkspaces = []
        refreshPending = true
      }
    } else receiveState(result)
  }
  onOpenedChanged: {
    forgetCandidate = null
    refreshProfiles()
    if (opened) {
      refreshConfiguration()
      if (!trialActive) {
        recoveryTimer.stop()
        message = ""
        topologyNotice = ""
        topologyRevision++
        invalidateValidation()
        refreshPending = true
        if (!requestPending && !profileActive) request("resume")
      }
    } else if (previewLocked && !trialAutomatic) {
      Qt.callLater(function() { if (root.previewLocked && !root.trialAutomatic) root.open() })
    }
  }

  // A display reconfiguration can recreate the bar and all its plugin objects.
  // Recover the independent worker's token rather than losing confirmation.
  Timer {
    id: recoveryTimer
    interval: 250
    running: true
    onTriggered: {
      if (!root.panelScreen || root.requestPending || root.profileActive) { restart(); return }
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
      if (action === "begin" || action === "forget") root.suppressAutomation()
      Qt.callLater(function() {
        root.completingRequest = false
        if (root.queuedRequest && !requestProcess.running) {
          var next = root.queuedRequest
          root.queuedRequest = null
          root.request(next.action, next.payload)
        }
        root.observeAutomatic()
        root.refreshWhenSafe()
        root.loadConfiguration()
        root.loadProfiles()
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
          root.configurationMessage = result.message || "Could not load display configuration. Close and reopen the panel to retry."
        }
      }
      Qt.callLater(function() {
        root.completingConfiguration = false
        root.loadConfiguration()
        root.loadProfiles()
      })
    }
  }
  Timer {
    id: profileTimer
    interval: 200
    onTriggered: root.loadProfiles()
  }
  Timer {
    id: profileFeedbackTimer
    interval: 6000
    onTriggered: if (!root.profileFeedbackIsError) root.profileFeedback = ""
  }
  Timer {
    interval: 6000
    running: root.opened && !root.automaticFeedbackDismissed
      && ["kept", "reverted", "skipped"].indexOf(root.autoStatus) !== -1
    onTriggered: root.automaticFeedbackDismissed = true
  }
  Process {
    id: profileProcess
    objectName: "profile-process"
    property string action: ""
    property int generation: 0
    property int draftRevision: 0
    stdout: StdioCollector { id: profileOutput; waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(code, status) {
      root.completingProfile = true
      if (generation === root.profileGeneration && root.opened)
        root.profileResponse(action, root.decode(profileOutput.text))
      if (action !== "profiles") root.suppressAutomation()
      Qt.callLater(function() {
        root.completingProfile = false
        root.observeAutomatic()
        root.refreshWhenSafe()
        root.loadConfiguration()
        root.loadProfiles()
      })
    }
  }
  Timer {
    id: validationTimer
    interval: 140
    onTriggered: {
      if (!root.liveBaseline || root.layoutBlocked || root.trialActive || root.refreshPending || !root.opened) return
      if (validationProcess.running || root.requestPending || root.profileActive) { restart(); return }
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
      Qt.callLater(root.loadProfiles)
      if (revision !== root.revision || root.layoutBlocked || root.trialActive || root.refreshPending || root.requestPending || root.profileActive) {
        Qt.callLater(root.refreshWhenSafe)
        return
      }
      var result = root.decode(validationOutput.text)
      root.valid = result.ok === true
      root.validationMessage = result.message || (root.valid ? "Layout is safe to try." : "Invalid layout.")
      root.changeCount = root.valid ? Number(result.displayChanges || 0) + Number(result.workspaceChanges || 0) : 0
      if (root.valid) root.removeWorkspaces = (result.removeWorkspaces || []).slice()
    }
  }
  Timer {
    interval: 250
    repeat: true
    running: root.trialActive && !root.trialAutomatic
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
      Qt.callLater(root.loadProfiles)
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
        Button { objectName: "profiles-tab"; text: "Profiles"; Accessible.name: "Profiles"; selected: root.page === 2; verticalPadding: Style.space(4); onClicked: root.page = 2 }
        Item { Layout.fillWidth: true }
        Button {
          objectName: "dock-transition"
          readonly property bool canRedock: root.liveBaseline !== null
            && (root.liveBaseline.restorableMonitors || []).length > 0
          text: canRedock ? (root.redockCandidate ? "Confirm redock" : "Redock safely…")
            : (root.undockCandidate ? "Confirm undock" : "Undock safely…")
          verticalPadding: Style.space(4)
          enabled: root.editable && root.profileWorkspaces === null
          opacity: enabled ? 1 : 0.45
          onClicked: canRedock ? root.prepareRedock() : root.prepareUndock()
        }
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
          : root.configurationBusy && !root.configurationRevision ? "Loading configured and saved displays…" : ""
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
            Text { text: "Display"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
            ComboBox {
              id: displayPicker
              objectName: "display-picker"
              Layout.fillWidth: true
              model: root.displays
              textRole: "label"
              currentIndex: {
                for (var i = 0; i < model.length; ++i) if (model[i].name === root.selectedName) return i
                return -1
              }
              enabled: root.editable && count > 0
              onActivated: function(index) { if (model[index]) root.selectedName = model[index].name }
            }
            Button {
              objectName: "display-enabled-toggle"
              text: root.selectedPosition && root.selectedPosition.enabled ? "Disable" : "Enable"
              verticalPadding: Style.space(4)
              enabled: root.editable && root.selectedPosition !== null && root.profileWorkspaces === null
              opacity: enabled ? 1 : 0.45
              onClicked: root.setDisplayEnabled(root.selectedName, !root.selectedPosition.enabled)
            }
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
              enabled: root.editable && root.selectedPosition !== null && root.selectedPosition.enabled
                && root.selectedDisplay !== null && (root.selectedDisplay.modeOptions || []).length > 0
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
              enabled: root.editable && root.selectedPosition !== null && root.selectedPosition.enabled && count > 0
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
        Column {
          objectName: "profiles-page"
          width: parent.width
          spacing: Style.space(8)
          visible: root.page === 2
          RowLayout {
            width: parent.width
            spacing: Style.space(8)
            Dropdown {
              id: profilePicker
              objectName: "profile-picker"
              Accessible.name: "Saved profile"
              Layout.fillWidth: true
              Layout.minimumWidth: 0
              showLabel: false
              options: root.profileEntries.map(function(entry) { return { value: entry.id, label: entry.name } })
              value: root.selectedProfileId
              enabled: root.profileReady && !root.profileManaging && root.profileEntries.length > 0
              onChanged: function(value) {
                root.selectProfile(value)
                profilePicker.value = root.selectedProfileId
              }
            }
            Button {
              objectName: "profile-new"
              text: "+ New"
              Accessible.name: "Save a new profile from the current live arrangement"
              bordered: true
              focusable: true
              verticalPadding: Style.space(4)
              enabled: root.profileReady && !root.profileManaging && !root.layoutBlocked
              opacity: enabled ? 1 : 0.45
              onClicked: root.beginProfileForm("new")
            }
            Button {
              id: profileMore
              objectName: "profile-more"
              text: "More"
              Accessible.name: "More profile actions"
              bordered: true
              focusable: true
              verticalPadding: Style.space(4)
              enabled: root.profileReady && !root.profileManaging && root.selectedProfile !== null
              opacity: enabled ? 1 : 0.45
              onClicked: profileMenu.open()
              onEnabledChanged: if (!enabled) profileMenu.close()
              Menu {
                id: profileMenu
                objectName: "profile-menu"
                x: profileMore.width - width
                y: profileMore.height
                width: Math.max(profileUpdateItem.implicitWidth, profileDeleteItem.implicitWidth) + leftPadding + rightPadding
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                palette.windowText: Color.foreground
                palette.text: Color.foreground
                palette.highlight: Color.accent
                palette.highlightedText: Color.background
                background: BorderSurface {
                  color: Color.popups.background
                  radius: Style.cornerRadius
                  borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Style.normalBorderWidth)
                }
                MenuItem {
                  id: profileUpdateItem
                  objectName: "profile-update"
                  text: "Update from current…"
                  enabled: !root.layoutBlocked
                  onTriggered: root.beginProfileForm("update")
                }
                MenuItem {
                  id: profileDeleteItem
                  objectName: "profile-delete"
                  text: "Delete…"
                  onTriggered: root.prepareProfile("profile-delete")
                }
              }
            }
          }
          RowLayout {
            width: parent.width
            spacing: Style.space(8)
            Text {
              objectName: "profile-summary"
              Layout.fillWidth: true
              Layout.minimumWidth: 0
              text: root.selectedProfile
                ? root.selectedProfile.displayCount + " displays · " + root.selectedProfile.workspaceCount + " workspaces"
                : root.profileBusy ? "Loading profiles…" : "No saved profiles. Choose New to save this live arrangement."
              color: Color.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.Wrap
            }
            Button {
              objectName: "profile-match-status"
              visible: root.selectedProfile !== null
              text: root.profileBusy ? "Checking…"
                : !root.selectedProfile || !root.selectedProfile.canLoad ? "Unavailable"
                : root.selectedProfile.match === "hardware" ? "Ready" : "Check displays"
              Accessible.name: text + ". Show compatibility details"
              selected: true
              focusable: true
              foreground: root.selectedProfile && (!root.selectedProfile.canLoad || root.selectedProfile.match !== "hardware") ? Color.urgent : Color.foreground
              verticalPadding: Style.space(2)
              onClicked: root.profileDetailsExpanded = !root.profileDetailsExpanded
            }
          }
          Text {
            width: parent.width
            visible: root.selectedProfile !== null && !root.profileBusy
              && (!root.selectedProfile.canLoad || root.selectedProfile.match !== "hardware")
            text: root.selectedProfile && root.selectedProfile.canLoad
              ? "Port-dependent match — verify the displays before Preview."
              : "This profile is unavailable on the current displays. Check Details."
            color: Color.urgent
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }
          Button {
            objectName: "profile-details-toggle"
            text: root.profileDetailsExpanded ? "Hide details" : "Details"
            visible: root.selectedProfile !== null || root.profileFeedbackDetails !== "" || root.autoMessage !== ""
            focusable: true
            verticalPadding: Style.space(2)
            onClicked: root.profileDetailsExpanded = !root.profileDetailsExpanded
          }
          Text {
            objectName: "profile-details"
            width: parent.width
            visible: root.profileDetailsExpanded
            text: [
              root.selectedProfile ? root.selectedProfile.reason || "" : "",
              root.selectedProfile && !root.selectedProfile.automatic && !root.selectedProfile.canAutomate
                ? "Automatic restoration: " + root.selectedProfile.automaticReason : "",
              root.profileFeedbackDetails ? "Last profile operation:\n" + root.profileFeedbackDetails : "",
              root.autoMessage ? "Automatic restoration:\n" + root.autoMessage : ""
            ].filter(function(part) { return part !== "" }).join("\n\n")
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
          }
          PanelSeparator { width: parent.width; visible: root.selectedProfile !== null }
          RowLayout {
            width: parent.width
            spacing: Style.space(8)
            visible: root.selectedProfile !== null
            ColumnLayout {
              Layout.fillWidth: true
              Layout.minimumWidth: 0
              spacing: Style.space(3)
              Text {
                text: "Restore automatically"
                color: Color.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.body
              }
              Text {
                objectName: "profile-automatic-status"
                Layout.fillWidth: true
                text: root.selectedProfile && !root.selectedProfile.automatic && !root.selectedProfile.canAutomate
                  ? "Not available for this profile. Check Details."
                  : "When this display combination reconnects."
                color: Color.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.Wrap
              }
            }
            Text {
              text: root.selectedProfile && root.selectedProfile.automatic ? "On" : "Off"
              color: Color.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
            ToggleSwitch {
              objectName: "profile-automatic"
              Accessible.role: Accessible.CheckBox
              Accessible.name: "Restore selected profile automatically"
              Accessible.checked: checked
              checked: root.selectedProfile !== null && root.selectedProfile.automatic
              activeFocusOnTab: enabled
              hasCursor: activeFocus
              enabled: root.profileReady && !root.profileManaging && root.selectedProfile !== null
                && (root.selectedProfile.automatic || root.selectedProfile.canAutomate)
              opacity: enabled ? 1 : 0.45
              Keys.onSpacePressed: if (enabled) root.prepareProfile("profile-auto")
              Keys.onReturnPressed: if (enabled) root.prepareProfile("profile-auto")
              onToggled: root.prepareProfile("profile-auto")
            }
          }
          PanelSeparator { width: parent.width; visible: root.profileForm !== "" }
          Text {
            objectName: "profile-save-explanation"
            width: parent.width
            visible: root.profileForm !== ""
            text: root.profileForm === "update"
              ? "Update from the current live arrangement, not draft edits. Automatic restoration will turn off."
              : "Save the current live arrangement, not draft edits."
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }
          RowLayout {
            width: parent.width
            spacing: Style.space(8)
            visible: root.profileForm !== ""
            Text { text: "Profile name"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
            TextField {
              id: profileNameInput
              objectName: "profile-name"
              Accessible.name: "Profile name, up to 80 characters"
              Layout.fillWidth: true
              Layout.minimumWidth: 0
              text: root.profileNameText
              placeholderText: "Name the current live arrangement"
              maximumLength: 80
              selectByMouse: true
              enabled: root.profileReady && root.profileCandidate === null
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              onTextEdited: root.profileNameText = text
            }
          }
          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: root.profileCandidate !== null
            Text {
              objectName: "profile-confirmation-warning"
              Accessible.name: text
              width: parent.width
              text: root.profileCandidate ? root.profileCandidate.warning : ""
              color: Color.urgent
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
            }
          }
        }
        Text {
          objectName: "profile-workspace-actions"
          Accessible.name: text
          width: parent.width
          visible: root.page === 1 && root.profileWorkspaces !== null
          text: "Preview restores all saved workspaces and retains them for this session. Extra empty workspaces are removed; populated extras and extras with unknown window counts are preserved."
            + (root.removeWorkspaces.length ? "\nRemove empty workspaces on Preview: " + root.removeWorkspaces.join(", ") + "." : "")
          color: Color.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
        }
        Item {
          width: parent.width
          height: columns.implicitHeight
          visible: root.page === 1
          Row {
            id: columns
            spacing: Style.space(8)
            Repeater {
              model: JSON.parse(root.workspaceDisplayOrder)
              delegate: Rectangle {
                id: displayColumn
                required property var modelData
                required property int index
                readonly property var displayData: root.workspaceDisplays.find(function(m) { return m.name === modelData })
                  || ({ name: modelData, label: modelData, icon: "\uf108" })
                objectName: "display-" + modelData
                z: root.dragOrigin === modelData ? 1 : 0
                width: (content.width - columns.spacing * Math.max(0, root.workspaceDisplays.length - 1)) / Math.max(1, root.workspaceDisplays.length)
                height: chips.y + Math.max(Style.space(32), chips.implicitHeight) + Style.space(8)
                color: drop.containsDrag ? Color.menu.selectedBackground : "transparent"
                border.color: drop.containsDrag ? Color.accent : Color.foreground
                border.width: 1
                radius: Style.space(6)
                TextField {
                  objectName: "display-name-" + displayColumn.modelData
                  x: Style.space(8); y: Style.space(6)
                  width: parent.width - Style.space(16)
                  height: Style.space(30)
                  text: displayColumn.displayData.label
                  enabled: root.preferencesEditable && root.workspaceWidget !== null
                  selectByMouse: true
                  maximumLength: 64
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                  onEditingFinished: {
                    var connector = displayColumn.modelData, label = text
                    text = Qt.binding(function() { return displayColumn.displayData.label })
                    if (!root.preferencesEditable || label.trim() === displayColumn.displayData.label) return
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
                  objectName: "display-icon-" + displayColumn.modelData
                  x: Style.space(8); y: Style.space(40)
                  width: parent.width - Style.space(16)
                  rowHeight: Style.space(30)
                  value: displayColumn.displayData.icon
                  options: root.displayIcons.some(function(choice) { return choice.value === value })
                    ? root.displayIcons : root.displayIcons.concat([{ label: value + "  Custom icon", value: value }])
                  enabled: root.preferencesEditable && root.workspaceWidget !== null
                  opacity: enabled ? 1 : 0.45
                  onChanged: function(icon) {
                    var connector = displayColumn.modelData
                    Qt.callLater(function() {
                      // Dropdown selection assigns value; restore the live settings binding.
                      iconPicker.value = Qt.binding(function() { return displayColumn.displayData.icon })
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
                    objectName: "display-left-" + displayColumn.modelData
                    text: "‹"
                    verticalPadding: Style.space(2)
                    enabled: root.preferencesEditable && root.workspaceWidget !== null && displayColumn.index > 0
                    opacity: enabled ? 1 : 0.45
                    onClicked: {
                      var connector = displayColumn.modelData
                      Qt.callLater(function() { if (root.preferencesEditable && root.workspaceWidget) root.workspaceWidget.moveDisplayOrder(connector, -1) })
                    }
                  }
                  Text {
                    Layout.fillWidth: true
                    text: displayColumn.displayData.icon + " " + displayColumn.modelData
                    color: Color.foreground
                    horizontalAlignment: Text.AlignHCenter
                    font.family: Style.font.family
                    font.pixelSize: Style.font.bodySmall
                  }
                  Button {
                    objectName: "display-right-" + displayColumn.modelData
                    text: "›"
                    verticalPadding: Style.space(2)
                    enabled: root.preferencesEditable && root.workspaceWidget !== null && displayColumn.index < root.workspaceDisplays.length - 1
                    opacity: enabled ? 1 : 0.45
                    onClicked: {
                      var connector = displayColumn.modelData
                      Qt.callLater(function() { if (root.preferencesEditable && root.workspaceWidget) root.workspaceWidget.moveDisplayOrder(connector, 1) })
                    }
                  }
                }
                DropArea {
                  id: drop
                  anchors.fill: parent
                  enabled: root.editable && !displayColumn.displayData.reason
                  keys: ["workspace-arrangement"]
                  onDropped: function(event) {
                    var id = event.source.workspaceId, destination = displayColumn.modelData
                    event.acceptProposedAction()
                    Qt.callLater(function() { root.stage(id, destination) })
                  }
                }
                Text {
                  id: columnFault
                  x: Style.space(8); y: Style.space(108)
                  width: parent.width - Style.space(16)
                  visible: text !== ""
                  text: displayColumn.displayData.reason || ""
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
                    model: root.draft.filter(function(w) { return w.target === displayColumn.modelData })
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
                          objectName: "workspace-action-" + slot.modelData.id
                          text: slot.modelData.name + (slot.modelData.source === null ? " · create on Preview"
                            : root.removeWorkspaces.indexOf(slot.modelData.id) >= 0 ? " · remove on Preview"
                            : " · " + (slot.modelData.windows === undefined || slot.modelData.windows === null ? "unknown windows"
                              : slot.modelData.windows + (slot.modelData.windows === 1 ? " window" : " windows")))
                          color: Color.background
                          font.family: Style.font.family
                          font.pixelSize: Style.font.body
                        }
                        MouseArea {
                          id: mouse
                          anchors.fill: parent
                          enabled: root.editable && !displayColumn.displayData.reason && root.removeWorkspaces.indexOf(slot.modelData.id) < 0
                          preventStealing: true
                          drag.target: chip
                          cursorShape: drag.active ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                          onPressed: root.dragOrigin = displayColumn.modelData
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
          objectName: "profile-operation-status"
          Accessible.name: text
          width: parent.width
          visible: text !== ""
          text: root.trialActive && root.trialAutomatic
            ? "Restoring " + (root.trialProfileName ? "“" + root.trialProfileName + "”" : "saved arrangement")
              + (root.trialState === "starting" ? "…" : " — keeps in " + root.secondsRemaining + "s")
            : root.automaticFailure ? (root.trialAutomatic && root.messageIsError ? root.message : root.autoMessage)
            : root.page === 2 && root.profileCatalogMessage ? root.profileCatalogMessage
            : !root.previewLocked && root.profileFeedback ? root.profileFeedback
            : root.page !== 2 || root.automaticFeedbackDismissed ? ""
            : root.autoStatus === "kept" ? "Automatic restore complete."
            : root.autoStatus === "reverted" ? "Automatic restore reverted."
            : root.autoStatus === "skipped" ? "Automatic restore skipped. Check Details." : ""
          color: root.automaticFailure || root.profileFeedbackIsError || (root.page === 2 && root.profileCatalogMessage !== "") ? Color.urgent : Color.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
        }
        Text {
          id: statusLabel
          width: parent.width
          visible: text !== ""
          text: root.trialAutomatic && root.automaticFailure ? ""
            : root.messageIsError && root.message ? root.message
            : root.trialActive ? ""
            : root.previewLocked ? "Starting preview…"
            : root.page !== 2 && !root.valid ? root.validationMessage : ""
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
            Layout.minimumWidth: 0
            Layout.fillWidth: true
            text: root.trialActive ? (root.trialAutomatic ? "" : "Reverts in " + root.secondsRemaining + "s")
              : root.page === 2 ? (root.profileManaging ? "" : "Review before applying.")
              : root.changeCount > 0 ? root.changeCount + " pending" : ""
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
          Button {
            objectName: "profile-save-current"
            visible: root.page === 2 && root.profileForm === "new" && root.profileCandidate === null && !root.trialActive
            text: "Save profile"
            Accessible.name: "Save current live arrangement as a new profile"
            selected: true
            focusable: true
            verticalPadding: Style.space(4)
            enabled: root.profileReady && root.profileNameValid && !root.layoutBlocked
            opacity: enabled ? 1 : 0.45
            onClicked: root.saveCurrentProfile()
          }
          Button {
            objectName: "profile-update-review"
            visible: root.page === 2 && root.profileForm === "update" && root.profileCandidate === null && !root.trialActive
            text: "Review update"
            selected: true
            focusable: true
            verticalPadding: Style.space(4)
            enabled: root.profileReady && root.profileNameValid && !root.layoutBlocked && root.selectedProfile !== null
            opacity: enabled ? 1 : 0.45
            onClicked: root.prepareProfile("profile-save")
          }
          Button {
            objectName: "profile-confirm"
            visible: root.page === 2 && root.profileCandidate !== null
            text: root.profileCandidate && root.profileCandidate.action === "profile-delete" ? "Confirm delete"
              : root.profileCandidate && root.profileCandidate.action === "profile-save" ? "Confirm update"
              : root.profileCandidate && root.profileCandidate.action === "profile-auto"
                ? (root.profileCandidate.enabled ? "Turn on" : "Turn off") : "Discard and review"
            Accessible.name: text
            focusable: true
            verticalPadding: Style.space(4)
            enabled: root.profileReady && root.profileCandidate !== null
            opacity: enabled ? 1 : 0.45
            onClicked: root.confirmProfile()
          }
          Button {
            objectName: "cancel"
            text: root.trialActive ? "Revert" : root.page === 2 && !root.profileManaging ? "Close" : "Cancel"
            verticalPadding: Style.space(4)
            focusable: true
            enabled: !requestProcess.running
            Accessible.name: root.page === 2 && root.profileCandidate !== null ? "Cancel profile confirmation" : text
            onClicked: {
              if (root.trialActive) root.request("revert", root.token)
              else if (root.page === 2 && root.profileManaging) {
                root.profileCandidate = null
                root.profileForm = ""
              }
              else root.close()
            }
          }
          Button {
            objectName: "profile-load"
            visible: root.page === 2 && !root.profileManaging && !root.trialActive
            text: "Review layout"
            Accessible.name: "Review selected profile as a draft without applying live changes"
            selected: true
            focusable: true
            verticalPadding: Style.space(4)
            enabled: root.profileReady && root.selectedProfile !== null && root.selectedProfile.canLoad
            opacity: enabled ? 1 : 0.45
            onClicked: root.prepareProfile("profile-load")
          }
          Button {
            objectName: "preview"
            visible: root.page !== 2
            text: "Preview (20s)"
            verticalPadding: Style.space(4)
            enabled: root.editable && root.valid && root.changeCount > 0 && !validationProcess.running
            opacity: enabled ? 1 : 0.45
            onClicked: root.apply()
          }
          Button {
            objectName: "apply"
            visible: root.page !== 2 && !(root.trialActive && root.trialAutomatic)
            text: "Apply"
            verticalPadding: Style.space(4)
            enabled: !root.trialAutomatic && !requestProcess.running && root.trialState === "pending" && root.trialActive && root.secondsRemaining > 0
            opacity: enabled ? 1 : 0.45
            onClicked: root.request("keep", root.token)
          }
        }
      }
    }
  }
