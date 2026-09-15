import QtQuick
import QtQuick.Controls as Controls
import Quickshell
import qs.Commons
import qs.Ui
import "ScheduleText.js" as ScheduleText

// The popup behind the bar icon. Step one of the design: a list of saved
// routines, or an empty state, and the one action a fresh install needs:
// creating a routine. Everything else is added back deliberately later.
Panel {
  id: root
  objectName: "routinesPanel"
  moduleName: "omakron.routines"
  manageIpc: false
  property Item anchorItem: null
  property var hostWidget: null
  property var routines: []
  property var defaults: ({})
  property bool connected: false
  property bool loaded: false
  property bool editing: false
  property string error: ""
  property var runNotes: ({})  // routine id -> what Run now just did

  readonly property color dim: Qt.darker(Color.foreground, 1.4)
  readonly property string sampleState: setting("sampleState", "")
  readonly property bool samples: sampleState !== ""
  readonly property var sampleRows: [
    {name: "Morning repository report", schedule_kind: "cron", cron: "0 9 * * 1-5", timezone: "UTC"},
    {name: "Folder summary", schedule_kind: "manual", cron: null, timezone: null}
  ]
  readonly property var rows: samples ? (sampleState === "ready" ? sampleRows : []) : routines
  readonly property bool loading: samples ? sampleState === "loading" : (!loaded && error === "")
  readonly property bool disconnected: samples ? sampleState === "error" : (!connected && loaded)

  function dismiss() {
    editing = false
    hostWidget ? hostWidget.close() : close()
  }
  function refresh() {
    if (!samples && !bridge.busy && !editing) bridge.request("editor_defaults", {})
  }
  function startNew() {
    error = ""
    editing = true
  }
  function runNow(routine) {
    if (samples || bridge.busy) return
    error = ""
    bridge.request("run_now", {routine_id: routine.id, idempotency_key: "popup:" + routine.id + ":" + Date.now()})
  }
  function scheduleText(routine) {
    var when = routine.schedule_kind === "manual" ? "Runs when you ask" : ScheduleText.describe(routine.cron)
    return routine.source ? when + " · from a skill file" : when
  }
  onOpenedChanged: if (opened) refresh()

  ServiceClient {
    id: bridge
    onResult: function(op, params, data) {
      root.connected = true
      if (op === "editor_defaults") { root.defaults = data; bridge.request("list_routines", {}) }
      else if (op === "list_routines") { root.routines = data.routines; root.loaded = true; root.error = "" }
      else if (op === "run_now") {
        var notes = Object.assign({}, root.runNotes)
        notes[params.routine_id] = data.created ? "Queued just now" : "Already queued"
        root.runNotes = notes
      }
    }
    onFailure: function(op, code, message) {
      if (code === "unreachable") root.connected = false
      root.loaded = true
      if (!root.editing) root.error = message
    }
  }

  KeyboardPanel {
    id: popup
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: root.editing && editor.item ? editor.item : newRoutine
    contentWidth: fittedContentWidth(Style.space(root.editing ? 660 : 400))
    contentHeight: fittedContentHeight(column.implicitHeight, Style.space(root.editing ? 860 : 560))

    FocusScope {
      anchors.fill: parent
      Keys.onEscapePressed: {
        if (root.editing) { root.editing = false; newRoutine.forceActiveFocus() }
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

            Column {
              width: parent.width
              spacing: Style.space(2)
              Text {
                text: "Routines"
                color: Color.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.title
                font.bold: true
              }
              Caption { text: "Scheduled Claude Code routines" }
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
              visible: !root.loading && !root.disconnected && root.rows.length === 0
              text: "No routines yet."
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

            Column {
              width: parent.width
              visible: root.rows.length > 0
              spacing: Style.space(8)
              Repeater {
                model: root.rows
                Row {
                  id: row
                  required property var modelData
                  width: parent.width
                  spacing: Style.space(8)
                  Column {
                    width: parent.width - runButton.width - parent.spacing
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Style.space(2)
                    Body { width: parent.width; text: row.modelData.name; elide: Text.ElideRight; wrapMode: Text.NoWrap }
                    Caption {
                      width: parent.width
                      text: root.scheduleText(row.modelData) + (root.runNotes[row.modelData.id] ? " · " + root.runNotes[row.modelData.id] : "")
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
        }
      }
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
