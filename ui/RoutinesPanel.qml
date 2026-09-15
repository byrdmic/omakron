import QtQuick
import QtQuick.Controls as Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "ScheduleText.js" as ScheduleText

// The popup behind the bar icon. Four views: the list of routines with each
// one's latest run, a routine with its run history, one run with its result
// and log, and settings. While any run is open the panel polls the service
// every two seconds, so pressing Run now shows the run start, run, and end.
Panel {
  id: root
  objectName: "routinesPanel"
  moduleName: "omakron.routines"
  manageIpc: false
  property Item anchorItem: null
  property var hostWidget: null
  property var routines: []
  property var defaults: ({})
  property var settings: ({})
  property bool connected: false
  property bool loaded: false
  property bool editing: false
  property string view: "list"  // list, routine, run, settings
  property var routine: null  // the routine open in the routine view
  property var runs: []
  property var run: null  // the run open in the run view; a summary until the detail arrives
  property string error: ""
  property string settingsNote: ""
  property real nowMs: Date.now()

  readonly property color dim: Qt.darker(Color.foreground, 1.4)
  readonly property string sampleState: setting("sampleState", "")
  readonly property bool samples: sampleState !== ""
  readonly property var sampleRows: [
    {name: "Morning repository report", schedule_kind: "cron", cron: "0 9 * * 1-5", timezone: "UTC", latest_run: null},
    {name: "Folder summary", schedule_kind: "manual", cron: null, timezone: null, latest_run: null}
  ]
  readonly property var rows: samples ? (sampleState === "ready" ? sampleRows : []) : routines
  readonly property bool loading: samples ? sampleState === "loading" : (!loaded && error === "")
  readonly property bool disconnected: samples ? sampleState === "error" : (!connected && loaded)
  readonly property bool anyOpen: (run && ScheduleText.isOpen(run.status))
    || routines.some(function(r) { return r.latest_run && ScheduleText.isOpen(r.latest_run.status) })

  function dismiss() {
    editing = false
    hostWidget ? hostWidget.close() : close()
  }
  function refresh() {
    if (!samples && !bridge.busy && !editing) bridge.request("editor_defaults", {})
  }
  function sync() {
    if (!samples && !bridge.busy && !editing) bridge.request("dashboard", {})
  }
  function startNew() {
    error = ""
    editing = true
  }
  function runNow(target) {
    if (samples || bridge.busy) return
    error = ""
    bridge.request("run_now", {routine_id: target.id, idempotency_key: "popup:" + target.id + ":" + Date.now()})
  }
  function openRoutine(target) {
    routine = target
    runs = []
    view = "routine"
    if (!samples) bridge.request("list_runs", {routine_id: target.id, limit: 30})
  }
  function openRun(summary) {
    run = summary
    view = "run"
    showPrompt = false
    showLog = false
    if (!samples) bridge.request("get_run", {run_id: summary.id})
  }
  function openSettings() {
    settingsNote = ""
    logDir.text = settings.log_dir || ""
    view = "settings"
  }
  function saveSettings() {
    settingsNote = ""
    bridge.request("output_settings", {log_dir: logDir.text.trim()})
  }
  function back() {
    error = ""
    if (view === "run" && routine) { view = "routine"; run = null }
    else { view = "list"; run = null; routine = null }
    sync()
    backButton.visible ? backButton.forceActiveFocus() : newRoutine.forceActiveFocus()
  }
  function scheduleText(target) {
    var when = target.schedule_kind === "manual" ? "Runs when you ask" : ScheduleText.describe(target.cron)
    return target.source ? when + " · from a skill file" : when
  }
  function statusColor(status) {
    if (status === "succeeded") return Color.accent
    if (status === "failed" || status === "timed_out" || status === "interrupted") return Color.urgent
    return Color.muted
  }
  onOpenedChanged: if (opened) { nowMs = Date.now(); refresh() }

  // Live updates while something is running.
  Timer {
    interval: 2000
    repeat: true
    running: root.opened && !root.editing && !root.samples && root.anyOpen
    onTriggered: { root.nowMs = Date.now(); root.sync() }
  }

  ServiceClient {
    id: bridge
    onResult: function(op, params, data) {
      root.connected = true
      if (op === "editor_defaults") { root.defaults = data; bridge.request("dashboard", {}) }
      else if (op === "dashboard") {
        root.routines = data.routines
        root.settings = data.output_settings
        root.loaded = true
        root.error = ""
        if (root.view === "routine" && root.routine) {
          for (var i = 0; i < data.routines.length; i++) if (data.routines[i].id === root.routine.id) root.routine = data.routines[i]
          bridge.request("list_runs", {routine_id: root.routine.id, limit: 30})
        } else if (root.view === "run" && root.run) bridge.request("get_run", {run_id: root.run.id})
      }
      else if (op === "list_runs") {
        root.runs = data.runs
        if (root.view === "run" && root.run) bridge.request("get_run", {run_id: root.run.id})
      }
      else if (op === "get_run") root.run = data.run
      else if (op === "run_now") { root.nowMs = Date.now(); bridge.request("dashboard", {}) }
      else if (op === "output_settings") {
        root.settings = data
        root.settingsNote = data.log_dir ? "Saved. New runs are logged under " + data.log_dir + "." : "Saved. New runs are logged in the default folder."
      }
    }
    onFailure: function(op, code, message) {
      if (code === "unreachable") root.connected = false
      root.loaded = true
      if (op === "output_settings") root.settingsNote = message
      else if (!root.editing) root.error = message
    }
  }

  // Opens a run's folder in the file manager.
  Process {
    id: opener
    command: ["xdg-open", root.run && root.run.folder ? root.run.folder : "."]
  }
  // Omarchy's folder chooser for the log folder.
  Process {
    id: folderChooser
    command: ["omarchy-file-select", "--directory", "--title", "Log folder"]
    stdout: StdioCollector { id: chosenFolder; waitForEnd: true }
    onExited: function(code) {
      var path = chosenFolder.text.trim()
      if (code === 0 && path !== "") logDir.text = path
      Qt.callLater(function() {
        if (!root.opened) { root.hostWidget ? root.hostWidget.open() : root.open() }
        logDir.forceActiveFocus()
      })
    }
  }

  KeyboardPanel {
    id: popup
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: root.editing && editor.item ? editor.item : (root.view === "list" ? newRoutine : backButton)
    contentWidth: fittedContentWidth(Style.space(root.editing ? 660 : (root.view === "list" ? 440 : 560)))
    contentHeight: fittedContentHeight(column.implicitHeight, Style.space(root.editing ? 860 : 640))

    FocusScope {
      anchors.fill: parent
      Keys.onEscapePressed: {
        if (root.editing) { root.editing = false; newRoutine.forceActiveFocus() }
        else if (root.view !== "list") root.back()
        else root.dismiss()
      }
      Controls.ScrollView {
        id: scroll
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        Column {
          id: column
          width: scroll.availableWidth
          spacing: Style.space(12)

          Loader {
            id: editor
            width: parent.width
            active: root.editing
            visible: active
            sourceComponent: RoutineEditor {
              width: editor.width
              defaults: root.defaults
              client: bridge
              connected: root.connected
              onRevealRequested: function(item) {
                Qt.callLater(function() {
                  var y = item.mapToItem(column, 0, 0).y
                  var flick = scroll.contentItem
                  if (y < flick.contentY) flick.contentY = y
                  else if (y + item.height > flick.contentY + scroll.availableHeight)
                    flick.contentY = y + item.height - scroll.availableHeight
                })
              }
              onReopenRequested: Qt.callLater(function() {
                if (!root.opened) { root.hostWidget ? root.hostWidget.open() : root.open() }
              })
              onSaved: function(routine) {
                root.editing = false
                root.refresh()
                newRoutine.forceActiveFocus()
              }
              onCanceled: { root.editing = false; newRoutine.forceActiveFocus() }
            }
            onLoaded: Qt.callLater(function() { editor.item.focusFirst() })
          }

          Column {
            width: parent.width
            visible: !root.editing
            spacing: Style.space(12)

            // Header: title, and either the settings gear or a Back button.
            Row {
              width: parent.width
              spacing: Style.space(8)
              Button {
                id: backButton
                objectName: "backView"
                visible: root.view !== "list"
                anchors.verticalCenter: parent.verticalCenter
                iconText: "󰁍"
                tooltipText: "Back"
                focusable: true
                onClicked: root.back()
              }
              Column {
                width: parent.width - (backButton.visible ? backButton.width + parent.spacing : 0) - (gear.visible ? gear.width + parent.spacing : 0)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)
                Text {
                  width: parent.width
                  elide: Text.ElideRight
                  text: root.view === "list" ? "Routines"
                      : root.view === "settings" ? "Settings"
                      : root.view === "routine" && root.routine ? root.routine.name
                      : root.run ? (root.run.name || "Run") : "Run"
                  color: Color.foreground
                  font.family: Style.font.family
                  font.pixelSize: Style.font.title
                  font.bold: true
                }
                Caption {
                  width: parent.width
                  text: root.view === "list" ? "Scheduled Claude Code routines"
                      : root.view === "settings" ? "Where run logs are kept"
                      : root.view === "routine" && root.routine ? root.scheduleText(root.routine)
                      : root.run ? ScheduleText.moment(root.run.started_at || root.run.enqueued_at) : ""
                }
              }
              Button {
                id: gear
                objectName: "openSettings"
                visible: root.view === "list"
                anchors.verticalCenter: parent.verticalCenter
                iconText: "󰒓"
                tooltipText: "Settings"
                focusable: true
                enabled: root.samples || root.connected
                onClicked: root.openSettings()
              }
            }

            PanelSeparator { width: parent.width }

            Body {
              visible: root.loading
              text: "Loading your routines."
              color: root.dim
              topPadding: Style.space(12)
              bottomPadding: Style.space(12)
              horizontalAlignment: Text.AlignHCenter
            }
            Body {
              visible: root.disconnected
              text: "The service is not running.\nStart omakron.service, then open this panel again."
              color: root.dim
              topPadding: Style.space(12)
              bottomPadding: Style.space(12)
              horizontalAlignment: Text.AlignHCenter
            }
            Body {
              visible: root.error !== "" && !root.disconnected
              text: root.error
              color: Color.urgent
            }

            // ---------------------------------------------------------- list
            Column {
              width: parent.width
              visible: root.view === "list" && !root.loading && !root.disconnected
              spacing: Style.space(12)
              Body {
                visible: root.rows.length === 0
                text: "No routines yet."
                color: root.dim
                topPadding: Style.space(12)
                bottomPadding: Style.space(12)
                horizontalAlignment: Text.AlignHCenter
              }
              Column {
                width: parent.width
                visible: root.rows.length > 0
                spacing: Style.space(4)
                Repeater {
                  model: root.rows
                  Item {
                    id: row
                    required property var modelData
                    width: parent.width
                    height: rowContent.implicitHeight + Style.space(12)
                    Rectangle {
                      anchors.fill: parent
                      radius: Style.cornerRadius
                      color: rowHover.hovered ? Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.06) : "transparent"
                    }
                    HoverHandler { id: rowHover; cursorShape: Qt.PointingHandCursor }
                    TapHandler { onTapped: root.openRoutine(row.modelData) }
                    Row {
                      id: rowContent
                      anchors.left: parent.left
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      anchors.leftMargin: Style.space(8)
                      anchors.rightMargin: Style.space(8)
                      spacing: Style.space(8)
                      Column {
                        width: parent.width - runButton.width - parent.spacing
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Style.space(2)
                        Body { width: parent.width; text: row.modelData.name; elide: Text.ElideRight; wrapMode: Text.NoWrap }
                        Caption { width: parent.width; text: root.scheduleText(row.modelData) }
                        Row {
                          spacing: Style.space(6)
                          Dot { status: row.modelData.latest_run ? row.modelData.latest_run.status : ""; anchors.verticalCenter: parent.verticalCenter }
                          Caption { width: rowContent.width - runButton.width - Style.space(22); text: ScheduleText.lastRun(row.modelData.latest_run, root.nowMs) }
                        }
                      }
                      Button {
                        id: runButton
                        objectName: "runNow"
                        anchors.verticalCenter: parent.verticalCenter
                        text: "Run now"
                        iconText: "󰐊"
                        fontSize: Style.font.bodySmall
                        focusable: true
                        bordered: true
                        enabled: !root.samples && root.connected && !bridge.busy
                        onClicked: root.runNow(row.modelData)
                      }
                    }
                  }
                }
              }
              PanelSeparator { width: parent.width }
              Button {
                id: newRoutine
                objectName: "newRoutine"
                width: parent.width
                text: "New routine"
                iconText: "󰐕"
                focusable: true
                bordered: true
                enabled: root.samples || root.connected
                onClicked: root.startNew()
              }
            }

            // ------------------------------------------------------- routine
            Column {
              width: parent.width
              visible: root.view === "routine" && root.routine !== null
              spacing: Style.space(12)
              Row {
                spacing: Style.space(8)
                Button {
                  text: "Run now"
                  iconText: "󰐊"
                  focusable: true
                  bordered: true
                  enabled: !root.samples && root.connected && !bridge.busy
                  onClicked: root.runNow(root.routine)
                }
                Caption {
                  anchors.verticalCenter: parent.verticalCenter
                  width: implicitWidth
                  text: root.routine && root.routine.source ? root.routine.source : ""
                }
              }
              Caption { text: "Runs, newest first. Click one for its result and log."; visible: root.runs.length > 0 }
              Body {
                visible: root.runs.length === 0
                text: "No runs yet."
                color: root.dim
                topPadding: Style.space(12)
                bottomPadding: Style.space(12)
                horizontalAlignment: Text.AlignHCenter
              }
              Column {
                width: parent.width
                spacing: Style.space(2)
                Repeater {
                  model: root.runs
                  Item {
                    id: runRow
                    required property var modelData
                    width: parent.width
                    height: runContent.implicitHeight + Style.space(10)
                    Rectangle {
                      anchors.fill: parent
                      radius: Style.cornerRadius
                      color: runHover.hovered ? Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.06) : "transparent"
                    }
                    HoverHandler { id: runHover; cursorShape: Qt.PointingHandCursor }
                    TapHandler { onTapped: root.openRun(runRow.modelData) }
                    Row {
                      id: runContent
                      anchors.left: parent.left
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      anchors.leftMargin: Style.space(8)
                      anchors.rightMargin: Style.space(8)
                      spacing: Style.space(8)
                      Dot { status: runRow.modelData.status; anchors.verticalCenter: parent.verticalCenter }
                      Column {
                        width: parent.width - Style.space(16)
                        spacing: Style.space(1)
                        Body {
                          width: parent.width
                          text: ScheduleText.statusWord(runRow.modelData.status) + " · " + ScheduleText.moment(runRow.modelData.started_at || runRow.modelData.enqueued_at)
                          elide: Text.ElideRight
                          wrapMode: Text.NoWrap
                        }
                        Caption {
                          width: parent.width
                          text: (runRow.modelData.trigger === "scheduled" ? "Scheduled" : "Run by hand")
                            + (runRow.modelData.duration_s !== null && runRow.modelData.duration_s !== undefined ? " · took " + ScheduleText.duration(runRow.modelData.duration_s) : "")
                            + (runRow.modelData.problems && runRow.modelData.problems.length ? " · " + runRow.modelData.problems[0] : "")
                        }
                      }
                    }
                  }
                }
              }
            }

            // ----------------------------------------------------------- run
            Column {
              width: parent.width
              visible: root.view === "run" && root.run !== null
              spacing: Style.space(10)
              Row {
                spacing: Style.space(8)
                Dot { status: root.run ? root.run.status : ""; anchors.verticalCenter: parent.verticalCenter; size: 10 }
                Body {
                  width: implicitWidth
                  text: root.run ? ScheduleText.statusWord(root.run.status) : ""
                  font.bold: true
                }
              }
              Column {
                width: parent.width
                spacing: Style.space(2)
                Caption { text: root.run && root.run.started_at ? "Started " + ScheduleText.moment(root.run.started_at) : "Queued " + (root.run ? ScheduleText.moment(root.run.enqueued_at) : "") }
                Caption { visible: root.run && root.run.ended_at ? true : false; text: root.run && root.run.ended_at ? "Ended " + ScheduleText.moment(root.run.ended_at) + (root.run.duration_s !== null && root.run.duration_s !== undefined ? " · took " + ScheduleText.duration(root.run.duration_s) : "") : "" }
                Caption { visible: root.run && (root.run.resolved_model || root.run.requested_model) ? true : false; text: root.run ? "Model " + (root.run.resolved_model || root.run.requested_model) : "" }
                Caption { visible: root.run && root.run.folder ? true : false; text: root.run && root.run.folder ? root.run.folder : "" }
              }
              Column {
                width: parent.width
                visible: root.run && root.run.problems && root.run.problems.length > 0 ? true : false
                spacing: Style.space(2)
                Repeater {
                  model: root.run && root.run.problems ? root.run.problems : []
                  Body { required property var modelData; text: modelData; color: Color.urgent }
                }
                Caption { visible: root.run && root.run.failure && root.run.failure.help ? true : false; text: root.run && root.run.failure ? root.run.failure.help : "" }
              }
              Caption { visible: root.run && ScheduleText.isOpen(root.run.status) ? true : false; text: "Still going. This page updates on its own." }
              Column {
                width: parent.width
                visible: root.run && root.run.result_text ? true : false
                spacing: Style.spacing.labelGap
                Caption { text: "Result"; font.bold: true }
                BorderSurface {
                  id: resultFrame
                  width: parent.width
                  height: Math.min(Style.space(260), resultText.implicitHeight + Style.space(16))
                  radius: Style.cornerRadius
                  readonly property var spec: Border.controlSpec("normal", Color.foreground, Color.accent)
                  color: Style.controlFill(false, false, Color.foreground, Color.accent)
                  borderSpec: spec
                  Controls.ScrollView {
                    anchors.fill: parent
                    anchors.margins: Border.top(resultFrame.spec)
                    clip: true
                    Controls.TextArea {
                      id: resultText
                      readOnly: true
                      text: root.run && root.run.result_text ? root.run.result_text : ""
                      wrapMode: TextEdit.Wrap
                      selectByMouse: true
                      color: Color.foreground
                      selectionColor: Style.selectionFillFor(Color.foreground, Color.accent)
                      selectedTextColor: Color.foreground
                      font.family: Style.font.family
                      font.pixelSize: Style.font.body
                      leftPadding: Style.spacing.controlPaddingX
                      rightPadding: Style.spacing.controlPaddingX
                      topPadding: Style.spacing.inputPaddingY
                      bottomPadding: Style.spacing.inputPaddingY
                      background: null
                    }
                  }
                }
              }
              Row {
                spacing: Style.space(8)
                Button {
                  text: "Open log folder"
                  iconText: "󰉋"
                  focusable: true
                  bordered: true
                  enabled: root.run && root.run.folder ? true : false
                  onClicked: opener.running = true
                }
                Button {
                  text: root.showPrompt ? "Hide prompt" : "Show prompt"
                  iconText: root.showPrompt ? "󰅃" : "󰅀"
                  focusable: true
                  visible: root.runPrompt !== ""
                  onClicked: root.showPrompt = !root.showPrompt
                }
                Button {
                  text: root.showLog ? "Hide log" : "Show log"
                  iconText: root.showLog ? "󰅃" : "󰅀"
                  focusable: true
                  visible: root.run && root.run.log ? true : false
                  onClicked: root.showLog = !root.showLog
                }
              }
              Column {
                width: parent.width
                visible: root.showPrompt && root.runPrompt !== ""
                spacing: Style.spacing.labelGap
                Caption { text: root.run && root.run.routine_snapshot && root.run.routine_snapshot.source ? "Prompt, as read from the skill file at launch" : "Prompt"; font.bold: true }
                BorderSurface {
                  id: promptFrame
                  width: parent.width
                  height: Math.min(Style.space(260), promptText.implicitHeight + Style.space(16))
                  radius: Style.cornerRadius
                  readonly property var spec: Border.controlSpec("normal", Color.foreground, Color.accent)
                  color: Style.controlFill(false, false, Color.foreground, Color.accent)
                  borderSpec: spec
                  Controls.ScrollView {
                    anchors.fill: parent
                    anchors.margins: Border.top(promptFrame.spec)
                    clip: true
                    Controls.TextArea {
                      id: promptText
                      readOnly: true
                      text: root.runPrompt
                      wrapMode: TextEdit.Wrap
                      selectByMouse: true
                      color: Color.foreground
                      selectionColor: Style.selectionFillFor(Color.foreground, Color.accent)
                      selectedTextColor: Color.foreground
                      font.family: Style.font.family
                      font.pixelSize: Style.font.body
                      leftPadding: Style.spacing.controlPaddingX
                      rightPadding: Style.spacing.controlPaddingX
                      topPadding: Style.spacing.inputPaddingY
                      bottomPadding: Style.spacing.inputPaddingY
                      background: null
                    }
                  }
                }
              }
              BorderSurface {
                id: logFrame
                visible: root.showLog && root.run && root.run.log ? true : false
                width: parent.width
                height: Style.space(300)
                radius: Style.cornerRadius
                readonly property var spec: Border.controlSpec("normal", Color.foreground, Color.accent)
                color: Style.controlFill(false, false, Color.foreground, Color.accent)
                borderSpec: spec
                Controls.ScrollView {
                  anchors.fill: parent
                  anchors.margins: Border.top(logFrame.spec)
                  clip: true
                  Controls.TextArea {
                    readOnly: true
                    text: root.run && root.run.log ? root.run.log : ""
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    color: root.dim
                    selectionColor: Style.selectionFillFor(Color.foreground, Color.accent)
                    selectedTextColor: Color.foreground
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    leftPadding: Style.spacing.controlPaddingX
                    rightPadding: Style.spacing.controlPaddingX
                    topPadding: Style.spacing.inputPaddingY
                    bottomPadding: Style.spacing.inputPaddingY
                    background: null
                  }
                }
              }
            }

            // ------------------------------------------------------ settings
            Column {
              width: parent.width
              visible: root.view === "settings"
              spacing: Style.space(10)
              Column {
                width: parent.width
                spacing: Style.spacing.labelGap
                Caption { text: "Log folder"; font.bold: true }
                Row {
                  width: parent.width
                  spacing: Style.space(6)
                  TextField {
                    id: logDir
                    objectName: "field_log_dir"
                    width: parent.width - logDirButton.width - parent.spacing
                    placeholderText: "Empty keeps the default under ~/.local/state/omakron/runs"
                    Accessible.name: "Log folder"
                    Keys.onReturnPressed: root.saveSettings()
                  }
                  Button {
                    id: logDirButton
                    iconText: "󰉋"
                    tooltipText: "Choose a folder"
                    focusable: true
                    bordered: true
                    enabled: !folderChooser.running
                    onClicked: folderChooser.running = true
                  }
                }
                Caption { text: "Every run gets its own folder here: the transcript, the result, and log.md with what happened and when. Runs already recorded stay where they are." }
              }
              Caption { visible: text !== ""; text: root.settingsNote; color: root.settingsNote.indexOf("Saved") === 0 ? root.dim : Color.urgent }
              Row {
                spacing: Style.space(8)
                Button {
                  text: "Save"
                  focusable: true
                  bordered: true
                  enabled: root.connected && !bridge.busy
                  onClicked: root.saveSettings()
                }
              }
            }
          }
        }
      }
    }
  }

  property bool showLog: false
  property bool showPrompt: false
  // The prompt text the run actually sent. Empty until the detail reply arrives.
  readonly property string runPrompt: run && run.routine_snapshot && run.routine_snapshot.prompt ? run.routine_snapshot.prompt : ""

  // A status dot. An open run breathes: a slow, gentle fade, nothing that flashes.
  component Dot: Rectangle {
    id: dot
    property string status: ""
    property int size: 8
    readonly property bool live: ScheduleText.isOpen(status)
    width: size
    height: size
    radius: size / 2
    color: root.statusColor(status)
    opacity: status === "" ? 0.35 : 1
    SequentialAnimation on opacity {
      running: dot.live && root.opened
      loops: Animation.Infinite
      onStopped: dot.opacity = dot.status === "" ? 0.35 : 1
      NumberAnimation { from: 1; to: 0.3; duration: 900; easing.type: Easing.InOutSine }
      NumberAnimation { from: 0.3; to: 1; duration: 900; easing.type: Easing.InOutSine }
    }
  }
  component Body: Text {
    width: column.width
    wrapMode: Text.Wrap
    textFormat: Text.PlainText
    color: Color.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }
  component Caption: Text {
    width: column.width
    wrapMode: Text.Wrap
    textFormat: Text.PlainText
    color: root.dim
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }
}
